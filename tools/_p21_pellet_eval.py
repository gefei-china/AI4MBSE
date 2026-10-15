# -*- coding: utf-8 -*-
"""P2-1 评估（2026-10-15）：Pellet DL 推理真库实测（只读）。

评估项（报告 §13 消费）：
① 依赖部署成本：owlready2+jpype1 pip 可装（已实测）；JRE 捆绑 373MB
② to_owl 导出耗时/产物规模
③ JVM 启动 + OWL 加载耗时
④ sync_reasoner_pellet 推理耗时 + inconsistent classes 检出
⑤ 结论对拍：与 _ontology_check（结构体检）结论是否互补/矛盾

只读：连接 file:ro，owlready2 World 内存操作，不写库。
"""
import io
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

T0 = time.perf_counter()

# ── ① 只读连接 + 结构体检基线 ────────────────────────────────────────────
conn = sqlite3.connect("file:mbse.db?mode=ro", uri=True)
conn.row_factory = sqlite3.Row
from routers.knowledge_parts.shared import _ontology_check  # noqa: E402

_t = time.perf_counter()
chk = _ontology_check(conn)
_chk_ms = (time.perf_counter() - _t) * 1000
print(f"[baseline] 结构体检: high={chk['high']} warn={chk['warn']} low={chk['low']} "
      f"info={chk['info']} (rule_version={chk.get('rule_version')}) 耗时 {_chk_ms:.0f}ms")

# ── ② to_owl 导出 ────────────────────────────────────────────────────────
from ontology_owl import to_owl  # noqa: E402

_t = time.perf_counter()
xml, warns = to_owl(conn)
_export_ms = (time.perf_counter() - _t) * 1000
print(f"[export] to_owl: {len(xml)/1024:.0f} KB, {len(warns)} 条导出警告, 耗时 {_export_ms:.0f}ms")
for w in warns[:5]:
    print("   warn:", w[:110])

# ── ③ owlready2 加载（JVM 交给 jpype 启动，java 由捆绑 JRE 提供） ─────────
from services.swrl_engine import ensure_jvm_home  # noqa: E402
ensure_jvm_home()

import owlready2  # noqa: E402

_t = time.perf_counter()
world = owlready2.World()
onto = world.get_ontology("http://mbse/onto/eval").load(fileobj=io.BytesIO(xml.encode("utf-8")))
_load_ms = (time.perf_counter() - _t) * 1000
n_cls = len(list(onto.classes()))
n_prop = len(list(onto.object_properties())) + len(list(onto.data_properties()))
print(f"[load] owlready2 World 加载: {n_cls} 类 / {n_prop} 属性, 耗时 {_load_ms:.0f}ms")

# ── ④ Pellet 推理（含 JVM 冷启动——jpype 首次 startJVM 计入本轮） ─────────
_t = time.perf_counter()
with onto:
    try:
        sync = owlready2.sync_reasoner_pellet(world, infer_property_values=True)
        _reason_ms = (time.perf_counter() - _t) * 1000
        # owlready2 0.51：inconsistent 时抛 OwlReadyInconsistentOntologyError；
        # consistent 时可能返回 None（无 inconsistent_classes 可报）——无异常即一致。
        incon = list(getattr(sync, "inconsistent_classes", None) or [])
        print(f"[reason] Pellet sync: 耗时 {_reason_ms:.0f}ms（含 JVM 冷启动）→ CONSISTENT"
              + (f"，inconsistent classes: {len(incon)}" if incon else ""))
    except Exception as e:
        _reason_ms = (time.perf_counter() - _t) * 1000
        msg = str(e)
        if "OwlReadyInconsistentOntologyError" in type(e).__name__ or "inconsistent" in msg.lower():
            print(f"[reason] Pellet 判 INCONSISTENT ({_reason_ms:.0f}ms): "
                  f"{[l for l in msg.splitlines() if 'ERROR' in l][:2]}")
        else:
            print(f"[reason] Pellet 异常 ({_reason_ms:.0f}ms): {type(e).__name__}: {e}")
        sys.exit(2)

# ── ⑤ 稳定性：第二次 sync（JVM 已热）─────────────────────────────────────
_t = time.perf_counter()
with onto:
    sync2 = owlready2.sync_reasoner_pellet(world, infer_property_values=True)
_hot_ms = (time.perf_counter() - _t) * 1000
print(f"[reason] 第二次 sync（JVM 热）: {_hot_ms:.0f}ms → CONSISTENT")

total = time.perf_counter() - T0
print(f"\n[summary] 总耗时 {total:.1f}s；导出 {_export_ms:.0f}ms / 加载 {_load_ms:.0f}ms / "
      f"推理冷 {_reason_ms:.0f}ms / 推理热 {_hot_ms:.0f}ms / 结构体检 {_chk_ms:.0f}ms")
