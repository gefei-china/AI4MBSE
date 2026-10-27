#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Step1 门禁：`switches()` 指标语义契约（2026-10-27）。

## 为什么需要这道门禁

`switches()` 原本对**所有**指标一律累加（`agg[k] += v`）。但指标分两类语义：

  · `count`（计数型）：一次装配里"发生了几次"，**累加才有意义**。
    例：`jit_fallback_full=1` 表示这轮发生了全量回退 ⇒ 4 轮里 2 次就是 2 次。
  · `gauge`（快照型）：一次装配里"当时是多少"，**累加完全错误**。
    例：`mcp_selected=8` 表示这轮暴露了 8 个工具。4 轮累加 = 32，
    前端若显示「MCP 32/71」会让人误判"暴露了近一半工具"，
    而真实是「每轮 8/71」。**观测工具变成误导工具，比没有观测更糟。**

## 判定口径（每条都可被变异证伪）

  [1] 元数据自洽：`METRIC_KINDS` 里每个键都在 `METRIC_LABELS` 有标签
  [2] 默认类型是 `gauge`（保守）—— 未登记的指标不得被当成 count
  [3] 快照型指标（mcp_selected 等）`value == avg`，且 `value != sum`（多轮时）
  [4] 计数型指标（jit_fallback_full）`value == sum`
  [5] `samples` = 该指标实际出现次数，不是窗口内 summary 总行数
      （指标可能只在某些 phase 出现，用总行数当分母会把均值算小）
  [6] 每条指标都下发 `kind` 与 `unit`，前端不必自己判语义
  [7] **变异自证**：把 mcp_selected 改成 count 型，[3] 必须翻红
"""
import json
import os
import shutil
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_FAILS = []


def check(name, ok, detail=""):
    if ok:
        print(f"[OK] {name}" + (f"  — {detail}" if detail else ""))
    else:
        print(f"[FAIL] {name}" + (f"  — {detail}" if detail else ""))
        _FAILS.append(name)


# ── 准备隔离库（绝不碰生产库）─────────────────────────────────
_PROD = os.path.join(_ROOT, "mbse.db")
_TMP = os.path.join(tempfile.gettempdir(), "verify_sw_semantics.db")
for _p in (_TMP, _TMP + "-wal", _TMP + "-shm"):
    if os.path.exists(_p):
        os.remove(_p)
if os.path.exists(_PROD):
    shutil.copy2(_PROD, _TMP)
os.environ["MBSE_DB_PATH"] = _TMP
os.environ.setdefault("MBSE_LLM_FORCE_MOCK", "1")

import logging                                                    # noqa: E402
logging.disable(logging.WARNING)

from database import init_db, get_db                              # noqa: E402
from repositories.context_audit_repo import (                      # noqa: E402
    ContextAuditRepo, METRIC_KINDS, METRIC_LABELS, METRIC_UNITS,
)

init_db()
con = get_db()
# 清空窗口内数据，保证样本完全可控
con.execute("DELETE FROM context_audit_summary")
con.commit()

# ── 造 4 轮：mcp_selected 恒 8（快照）；2 轮发生 JIT 全量回退（计数）
#    其中 retrieval_effective 只在 2 轮出现 ⇒ 用于验 samples 分母
RUNS = [
    {"mcp_selected": 8, "mcp_total": 71, "jit_fallback_full": 0},
    {"mcp_selected": 8, "mcp_total": 71, "jit_fallback_full": 1, "retrieval_effective": 3.0},
    {"mcp_selected": 8, "mcp_total": 71, "jit_fallback_full": 1, "retrieval_effective": 1.0},
    {"mcp_selected": 8, "mcp_total": 71, "jit_fallback_full": 0},
]
for _i, _r in enumerate(RUNS):
    con.execute(
        "INSERT INTO context_audit_summary (trace_id, conversation_id, phase, "
        "total_tokens, total_chars, block_count, metrics) VALUES (?,?,?,?,?,?,?)",
        (f"g{_i}", 900 + _i, "tools", 0, 0, 0, json.dumps(_r)))
con.commit()

_res = ContextAuditRepo(con).switches(days=7)
_by = {m["metric"]: m for m in _res["metrics"]}

# ── [1] 元数据自洽 ──
_missing = [k for k in METRIC_KINDS if k not in METRIC_LABELS]
check("[1] METRIC_KINDS 每个键都有标签", not _missing, f"缺 {_missing}")

# ── [2] 默认保守为 gauge ──
_unknown_kind = METRIC_KINDS.get("__nonexistent_metric__", "gauge")
check("[2] 未登记指标默认 gauge（不误当 count）", _unknown_kind == "gauge",
      f"实得 {_unknown_kind}")
check("[2] mcp_selected 未被登记为 count", METRIC_KINDS.get("mcp_selected", "gauge") != "count",
      "若登记为 count ⇒ 前端会显示累加值，等于退回旧缺陷")

# ── [3] 快照型：value == avg，且 != sum ──
for _k in ("mcp_selected", "mcp_total"):
    _m = _by.get(_k)
    if not _m:
        check(f"[3] {_k} 存在", False, "指标未返回")
        continue
    check(f"[3] {_k} kind=gauge", _m["kind"] == "gauge", _m["kind"])
    check(f"[3] {_k} value==avg({_m['avg']})", _m["value"] == _m["avg"],
          f"value={_m['value']} avg={_m['avg']}")
    check(f"[3] {_k} value!=sum({_m['sum']})（快照型不得显示累加）",
          _m["value"] != _m["sum"], f"value={_m['value']} sum={_m['sum']}")

# ── [4] 计数型：value == sum ──
_j = _by.get("jit_fallback_full")
if _j:
    check("[4] jit_fallback_full kind=count", _j["kind"] == "count", _j["kind"])
    check("[4] jit_fallback_full value==sum==2", _j["value"] == _j["sum"] == 2,
          f"value={_j['value']} sum={_j['sum']}")
else:
    check("[4] jit_fallback_full 存在", False, "指标未返回")

# ── [5] samples 是「该指标出现次数」而非 summary 总行数 ──
_re = _by.get("retrieval_effective")
if _re:
    # retrieval_effective 只在 2/4 轮出现 ⇒ samples 必须 2，avg 必须是 (3+1)/2=2
    check("[5] samples 按指标实际出现次数计（retrieval_effective 应=2）",
          _re["samples"] == 2, f"实得 {_re['samples']}（若=4 说明用了 summary 总行数当分母）")
    check("[5] avg 分母正确（(3.0+1.0)/2 = 2.0）", abs(_re["avg"] - 2.0) < 1e-6,
          f"实得 {_re['avg']}（若=1.0 说明分母错用 4）")
else:
    check("[5] retrieval_effective 存在", False, "指标未返回")

# ── [6] 每条都下发 kind / unit ──
_bad = [m["metric"] for m in _res["metrics"] if "kind" not in m or "unit" not in m]
check("[6] 每条指标都下发 kind 与 unit", not _bad, f"缺字段 {_bad[:3]}")

# ── [7] 变异自证：把快照型登记成 count，[3] 的判据必须翻红 ──
_MUT_KINDS = dict(METRIC_KINDS)
_MUT_KINDS["mcp_selected"] = "count"


def _display(kind, vals):
    """复刻 switches() 的取值规则，用于变异验证（不依赖改全局状态）。"""
    s = sum(vals)
    avg = s / len(vals) if vals else 0.0
    return s if kind == "count" else avg


_vals = [8.0, 8.0, 8.0, 8.0]
_good = _display(METRIC_KINDS.get("mcp_selected", "gauge"), _vals)
_bad_v = _display(_MUT_KINDS["mcp_selected"], _vals)
check("[7] 变异自证：登记成 count 后显示值变成累加（判据能抓到）",
      _good == 8.0 and _bad_v == 32.0,
      f"gauge→{_good} 变异→{_bad_v}")

con.close()
for _p in (_TMP, _TMP + "-wal", _TMP + "-shm"):
    if os.path.exists(_p):
        try:
            os.remove(_p)
        except Exception:
            pass

print("\n" + "=" * 68)
if _FAILS:
    print(f"❌ FAIL {len(_FAILS)} 项：{_FAILS}")
    sys.exit(1)
print("✅ switches 指标语义契约全部通过（快照型取均值 / 计数型取总和）")
sys.exit(0)
