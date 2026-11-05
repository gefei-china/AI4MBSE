# -*- coding: utf-8 -*-
"""JIT 工具预筛空回退改 top-N（P0-5）门禁。

背景（实测，生产库快照）：
  改动前「帮我把活动图生成出来」⇒ **24 个工具 / 7,487 字符 / 4,162 token**，
  而该任务实际只需 sysml 视图类工具；同族意图载荷波动 5→24 个。
  根因：全部候选都低于阈值 ⇒ `chosen` 为空 ⇒ 走"空回退全量注入"分支。
  ⇒ 把「阈值」当成了「开关」。阈值是**质量线**，不是**开关**。

判据（每条带**变异自证**）：
  [1] 弱相关场景不再全量注入：工具数 ≤ 回退 top-N + core_keep
  [2] core_keep 保底不受影响：读类核心工具在弱相关场景**仍被注入**
  [3] 强相关场景行为不变：命中阈值的场景仍按阈值筛（与改动前一致）
  [4] `rank` 的 top_k ≥ 回退 N（否则 scored 提前截断 ⇒ 回退 N 形同虚设）
  [5] 回滚开关：`jit_fallback_top_n=0` ⇒ 退回改动前的全量行为
  [6] 变异自证：把回退 N 设为 0 ⇒ 工具数暴涨（证明 [1] 真在生效）

用法： ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_jit_tool_fallback.py
"""
import os
import sys
import shutil
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

_TMP = os.path.join(tempfile.gettempdir(), "verify_jit_fallback.db")
for _p in (_TMP, _TMP + "-wal", _TMP + "-shm"):
    if os.path.exists(_p):
        os.remove(_p)
os.environ["MBSE_DB_PATH"] = _TMP
os.environ["MBSE_LLM_FORCE_MOCK"] = "1"

from database import init_db, get_db        # noqa: E402
init_db()

from agent.pipeline_parts.tools import ToolMixin   # noqa: E402
from agent.registry import AgentRegistry           # noqa: E402
from core import config as _cfg                    # noqa: E402

_FAILS = []


def check(name, cond, detail=""):
    tag = "OK  " if cond else "FAIL"
    if not cond:
        _FAILS.append(name)
    print(f"[{tag}] {name}" + (f"  — {detail}" if detail else ""))


# ── 造一批工具（弱相关 query 下全部低于阈值 ⇒ 触发空回退）──
con = get_db()
_TOOLS = [
    ("sysml_v2_validate", "校验 SysML v2 模型规范性与一致性", 1),
    ("sysml_v2_lint", "SysML v2 静态检查", 1),
    ("sysml_ast_extract", "从 SysML 源提取 AST", 1),
    ("sysml_stdlib_meta", "查询 SysML 标准库元数据", 1),
    ("sysml_v2_project_check", "工程级 SysML v2 检查", 1),
    ("coverage_matrix", "需求架构覆盖矩阵分析", 1),
    ("gap_summary", "覆盖缺口汇总", 1),
    ("modeling_coverage", "建模覆盖率分析", 1),
    ("requirement_itemize", "需求条目化拆解", 1),
    ("scene_coverage", "场景覆盖分析", 1),
    ("trace_chain_check", "追溯链校验", 1),
    ("file_list", "列出工作区文件", 1),
    ("file_read", "读取工作区文件", 1),
    ("methodology_profile_load", "加载方法论画像", 1),
]
for name, desc, builtin in _TOOLS:
    # 幂等：内置种子可能已存在同名工具 ⇒ 先删后建，避免 UNIQUE 冲突
    con.execute("DELETE FROM tools WHERE name=?", (name,))
    # ⚠️ source 必须是 **'local'**（非 builtin）：DB 工具候选的 SQL 条件是
    #   `t.source!='builtin'`（tools.py:215，刻意排除内置工具 —— 内置候选走 builtin_desc 字典）。
    #   用 'builtin' 造数据 ⇒ 工具进不了候选集 ⇒ 门禁失去意义（JIT 分支根本不会执行）。
    con.execute(
        "INSERT INTO tools (name, source, description, status, builtin, kind, input_schema) "
        "VALUES (?,?,?, 'active', ?, 'builtin', ?)",
        (name, "local", desc, builtin, '{"type":"object","properties":{}}'))
con.commit()

# ── 关键：候选集来自 **agent_tools 绑定**（`registry.get_bound_mcp` / DB 侧 bound_tools），
#    不是"全库 tools"。故必须把测试工具绑到 chat agent 上，否则候选集为空 ⇒
#    门禁会因"工具没进候选"而假绿/假红。
_ag = con.execute("SELECT id FROM agents WHERE name='chat' LIMIT 1").fetchone()
if not _ag:
    con.execute(
        "INSERT INTO agents (name, display_name, description, system_prompt, status) "
        "VALUES ('chat','通用助手','test','', 'active')")
    con.commit()
    _ag = con.execute("SELECT id FROM agents WHERE name='chat' LIMIT 1").fetchone()
_aid = _ag[0]
for name, _d, _b in _TOOLS:
    con.execute("DELETE FROM agent_tools WHERE agent_id=? AND tool_name=?", (_aid, name))
    con.execute(
        "INSERT INTO agent_tools (agent_id, tool_type, tool_name, enabled) "
        "VALUES (?, 'tool', ?, 1)", (_aid, name))
# core_keep 里的读类核心工具也要在候选集中（否则「保底」无从谈起）
for _n in ("graph_retrieve", "validate", "impact_analyze"):
    con.execute("DELETE FROM tools WHERE name=?", (_n,))
    con.execute(
        "INSERT INTO tools (name, source, description, status, builtin, kind, input_schema) "
        "VALUES (?,?,?, 'active', 1, 'builtin', '{\"type\":\"object\",\"properties\":{}}')",
        (_n, "local", "读类核心工具：检索/校验/影响分析"))
    con.execute("DELETE FROM agent_tools WHERE agent_id=? AND tool_name=?", (_aid, _n))
    con.execute(
        "INSERT INTO agent_tools (agent_id, tool_type, tool_name, enabled) "
        "VALUES (?, 'tool', ?, 1)", (_aid, _n))
con.commit()
con.close()


class _Pipe(ToolMixin):
    def __init__(self):
        self.registry = AgentRegistry()
        # ★ 必须显式从 DB 加载：候选集来自 `registry._db_meta[...]['bound_tools']`，
        #   不 load_from_db ⇒ 绑定信息为空 ⇒ 候选集为空 ⇒ 门禁失去意义。
        try:
            _c = get_db()
            try:
                self.registry.load_from_db(_c, {"role_name": "admin", "role": "admin"})
            finally:
                _c.close()
        except Exception as e:
            print("   (registry.load_from_db 失败：%s)" % e)
        self._tool_whitelist = None


pipe = _Pipe()
USER = {"role_name": "admin", "role": "admin"}
# 弱相关 query：与任何工具名/描述都无明显语义关联 ⇒ 触发空回退
WEAK_Q = "帮我把活动图生成出来"
FALLBACK_N = 8


def build(q):
    return pipe._build_tools_def("chat", q, USER)


# ── [1] 弱相关不再全量 ──
_sec = _cfg._CONFIG.setdefault("context_audit", {})
_old_fb = _sec.get("jit_fallback_top_n", 8)
try:
    _sec["jit_fallback_top_n"] = FALLBACK_N
    td = build(WEAK_Q)
    names = [x["function"]["name"] for x in td]
    _core = {"graph_retrieve", "validate", "impact_analyze"}
    check("[1] 弱相关场景工具数受回退 N 约束（不全量）",
          len(names) <= FALLBACK_N + len(_core) + 2,
          f"工具数 {len(names)}（候选 14）")
    check("[1] 工具数显著少于改动前的全量（24）", len(names) < len(_TOOLS),
          f"{len(names)} vs 全量 {len(_TOOLS)}")

    # ── [2] core_keep 保底不受影响 ──
    check("[2] core_keep 读类核心工具在弱相关场景仍被注入",
          ("graph_retrieve" in names) and ("impact_analyze" in names) and ("validate" in names),
          f"names={names[:8]}")

    # ── [3] 强相关场景行为不变 ──
    _td_strong = build("coverage_matrix 覆盖矩阵分析")
    _n_strong = [x["function"]["name"] for x in _td_strong]
    check("[3] 强相关场景仍命中语义工具（未被误伤）",
          "coverage_matrix" in _n_strong, f"names={_n_strong[:6]}")

    # ── [5] 回滚开关：N=0 ⇒ 退回全量 ──
    _sec["jit_fallback_top_n"] = 0
    _td_full = build(WEAK_Q)
    _n_full = [x["function"]["name"] for x in _td_full]
    check("[5] jit_fallback_top_n=0 时退回全量行为",
          len(_n_full) > len(names), f"全量 {len(_n_full)} > topN {len(names)}")

    # ── [6] 变异自证 ──
    _sec["jit_fallback_top_n"] = FALLBACK_N
    _td_back = build(WEAK_Q)
    _n_back = [x["function"]["name"] for x in _td_back]
    check("[6] 变异自证：开关一开一关，工具数随之变化（证明 [1]/[5] 真在生效）",
          len(_n_back) != len(_n_full), f"{len(_n_back)} vs {len(_n_full)}")
finally:
    _sec["jit_fallback_top_n"] = _old_fb

# ── [4] rank 的 top_k ≥ 回退 N（源码层判据：不得被 top_k 截断）──
_src = open(os.path.join(_ROOT, "agent", "pipeline_parts", "tools.py"), encoding="utf-8").read()
check("[4] rank 的 top_k 已随回退 N 调整（max(jit_top_n, _fb_n)）",
      "_rank_k = max(jit_top_n, _fb_n)" in _src,
      "否则 scored 提前截断，回退 N 形同虚设")

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
print("✅ ALL PASS —— JIT 空回退 top-N（收紧/core_keep 保底/强相关不变/回滚/变异自证）全部通过")
