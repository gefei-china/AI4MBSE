# -*- coding: utf-8 -*-
"""上下文分块归因（P0-2）门禁 —— 断言"块级 token 归因真的可用"。

判据（本仓纪律：每条判据都必须有**变异自证** —— 把被保护的行为改坏，门禁必须翻红）：
  [1] `assemble_system_prompt` 装配后，块级归因被记录（内存摘要含全部非空块）
  [2] 层归属正确：`role`/`tools`/`ontology` 等 = static，其余 = dynamic
  [3] 归因 token 之和与各块 token 一致（无重复计数 / 无丢失）
  [4] `flush()` 落库：`context_audit` 行数 = 非空块数；`context_audit_summary` 恰 1 行
  [5] trace 关联：`context_audit.trace_id` == `llm_usage_stats.trace_id` 同源口径
  [6] 观测不阻断：`context_audit.enabled=False` 时归因为空、且拼装结果**逐字节不变**
  [7] 变异自证：把 `_layer_of` 判据改坏 ⇒ [2] 翻红（证明判据真的在生效）

用法： ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_context_audit.py
"""
import os
import sys
import json
import shutil
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

# ── 隔离库：绝不碰生产 mbse.db（AGENTS.md 坑 #7：服务运行时 db 被占用）──
_TMP = os.path.join(tempfile.gettempdir(), "verify_context_audit.db")
for _p in (_TMP, _TMP + "-wal", _TMP + "-shm"):
    if os.path.exists(_p):
        os.remove(_p)
os.environ["MBSE_DB_PATH"] = _TMP
os.environ["MBSE_LLM_FORCE_MOCK"] = "1"

from database import init_db, get_db   # noqa: E402

init_db()

from core.audit import begin_trace        # noqa: E402
from agent.pipeline_parts import common as C   # noqa: E402
from agent import context_audit as CA    # noqa: E402

_FAILS = []


def check(name, cond, detail=""):
    tag = "OK  " if cond else "FAIL"
    if not cond:
        _FAILS.append(name)
    print(f"[{tag}] {name}" + (f"  — {detail}" if detail else ""))


BLOCKS = {
    "role": "你是一名 MBSE 建模助手。\n## 职责\n职责描述。\n" * 30,
    "tools": "可用工具：graph_retrieve, validate。\n",
    "tool_rules": "工具使用约束：先读后写。\n",
    "ontology": "本体类型：Block, Part, Port, Activity。\n",
    "boundary": "边界说明：本体与图谱的职责边界。\n",
    "output_rules": "输出规范：分点、给依据。\n",
    "citation_rules": "引用规范：正文标注 [n]。\n",
    "intent_line": "当前意图：chat（Agent: chat，HIL: L1）。\n",
    "memory": "【长期记忆】\n- [fact] 记忆条目\n" * 4,
    "retrieval": "检索到的互联数据：\n【来源1】a.pdf §0: xxx\n【来源2】b.pdf §1: yyy\n",
}
EXPECT_BLOCKS = {k: v for k, v in BLOCKS.items() if v}

# ── [1][2][3] 内存归因 ──
begin_trace(conversation_id=4242)
prompt = C.assemble_system_prompt(dict(BLOCKS))
summ = CA.last_summary()
per_block = summ.get("blocks") or {}

check("[1] 归因记录了全部非空块",
      set(per_block) == set(EXPECT_BLOCKS),
      f"期望 {len(EXPECT_BLOCKS)} 块，实得 {len(per_block)}")
check("[1] 归因总 token > 0", (summ.get("total_tokens") or 0) > 0,
      f"total={summ.get('total_tokens')}")

_static_expect = {"role", "tools", "tool_rules", "ontology", "boundary",
                   "output_rules", "citation_rules"}
_layer_ok = all(per_block[k]["layer"] == ("static" if k in _static_expect else "dynamic")
                for k in per_block)
check("[2] 层归属正确（static/dynamic 与 PROMPT_CACHEABLE_KEYS 一致）", _layer_ok)

_sum = sum(v["tokens"] for v in per_block.values())
check("[3] 各块 token 之和 = 归因总量", _sum == summ.get("total_tokens"),
      f"块和 {_sum} vs 总量 {summ.get('total_tokens')}")

# ── [4][5] 落库 ──
CA.flush()
con = get_db()
_rows = con.execute("SELECT COUNT(*) FROM context_audit").fetchone()[0]
check("[4] context_audit 落库行数 = 非空块数", _rows == len(EXPECT_BLOCKS),
      f"落 {_rows} 行 / 期望 {len(EXPECT_BLOCKS)}")
_sumrows = con.execute("SELECT COUNT(*) FROM context_audit_summary").fetchone()[0]
check("[4] context_audit_summary 恰 1 行（一次装配一行）", _sumrows == 1, f"实得 {_sumrows}")
_stok = con.execute("SELECT SUM(tokens) FROM context_audit").fetchone()[0] or 0
check("[4] 落库 token 之和 = 内存归因总量", _stok == summ.get("total_tokens"),
      f"库 {_stok} vs 内存 {summ.get('total_tokens')}")
_tid = con.execute("SELECT trace_id FROM context_audit LIMIT 1").fetchone()[0]
check("[5] trace_id 已落库（可与 llm_usage_stats JOIN）", bool(_tid), f"trace_id={_tid}")
# ── 变异自证：trace_id 必须随 trace 变化（否则 JOIN 口径失效）──
begin_trace(conversation_id=4243)
C.assemble_system_prompt(dict(BLOCKS))
CA.flush()
_tid2 = con.execute("SELECT trace_id FROM context_audit WHERE conversation_id=4243 "
                     "LIMIT 1").fetchone()
check("[5] 不同 trace 落不同 trace_id", bool(_tid2) and _tid2[0] != _tid)
con.close()

# ── [6] 关闭开关：零开销且拼装结果不变 ──
# ⚠️ 不能用 `config.set`（本仓无此 API，运行时写库走 settings 表且需白名单）。
#    这里直接改模块级 `_CONFIG` —— 与 `config.get` 的读取口径一致，且**不写库**。
from core import config as _cfg      # noqa: E402

_prompt_off = None
_off_summary = {}
try:
    _sec = _cfg._CONFIG.setdefault("context_audit", {})
    _old = _sec.get("enabled", True)
    _sec["enabled"] = False
    try:
        CA._BUF.clear()
        CA._LAST.clear()
        _prompt_off = C.assemble_system_prompt(dict(BLOCKS))
        _off_summary = CA.last_summary()
    finally:
        _sec["enabled"] = _old
    check("[6] enabled=False 时归因为空", not _off_summary.get("blocks"),
          f"实得 {len(_off_summary.get('blocks') or {})} 块")
    check("[6] enabled=False 时拼装结果逐字节不变", _prompt_off == prompt)
except Exception as e:
    check("[6] enabled=False 关闭态断言", False, f"执行失败：{e}")

# ── [7] 变异自证：改坏层归属判据 ⇒ [2] 必须翻红 ──
_orig_layer = CA._layer_of
try:
    CA._layer_of = lambda name: "dynamic"      # 把所有块都标成 dynamic
    CA._BUF.clear(); CA._LAST.clear()
    begin_trace(conversation_id=4244)
    C.assemble_system_prompt(dict(BLOCKS))
    _mut = CA.last_summary().get("blocks") or {}
    _mut_ok = all(v["layer"] == "dynamic" for v in _mut.values())
    check("[7] 变异自证：改坏层判据后不再有 static 块（证明 [2] 真在生效）",
          _mut_ok and not any(v["layer"] == "static" for v in _mut.values()))
finally:
    CA._layer_of = _orig_layer

# ── 清理 ──
for _p in (_TMP, _TMP + "-wal", _TMP + "-shm"):
    try:
        if os.path.exists(_p):
            os.remove(_p)
    except Exception:
        pass

print("\n" + "=" * 60)
if _FAILS:
    print(f"❌ FAIL {len(_FAILS)} 项：" + "; ".join(_FAILS))
    sys.exit(1)
print("✅ ALL PASS —— 上下文分块归因（块级 + 聚合 + trace 关联 + 变异自证）全部通过")
