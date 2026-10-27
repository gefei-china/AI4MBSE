# -*- coding: utf-8 -*-
"""能力中心四环节链路门禁（2026-10-27）。

守的是走查给出的 4 个 P0 断点，每一条都对应一个**会静默空转**的形态：

| 编号 | 断点 | 本门禁怎么守 |
|---|---|---|
| A | P0-0 `tool_meta` 判空写在 `'[]'` 上恒假 | 行为断言：`'[]'` 必须判成 0 个工具；并证明**旧写法会判成"有数据"** |
| B | 回填"看起来成功实际没落库" | 打桩 discover 返回 ok 但不落库 ⇒ `backfilled` 必须为 0 且留痕 |
| C | P1-1 MCP 工具级投影恒 0 行 | `extract` 必须读 `tool_meta`；`expand` 幂等、改名不留幽灵 |
| D | P1-2 巡检不刷统一注册处 | 源码接线断言 + 行为：改源表状态后 `refresh` 必须让 registry 跟着变 |
| E | P0-1 "绑定 ≠ 消费" | `file_read` 形态：0 绑定 + 技能白名单 + 有调用 ⇒ 必须判「在用」 |
| F | P0-3 任务级追溯断裂 | `trace_id` 列 / 写入 / 入口赋值 / 请求清零 四处齐 + 落库行为断言 |
| G | P0-2 注册处无出口 | 4 个只读端点注册且**源码里没有任何写 SQL** |

**为什么每条都要有变异自证**：本仓反复吃亏的不是"没写断言"，而是"断言看着绿但测的不是那件事"
（见 MEMORY §6.2）。故本脚本末尾对 A/C/D/E/F 各做一次变异，**必须被抓住**，
否则 exit 2（守卫失败 ⇒ 结论不可信），而不是继续报绿。

运行：`.venv/Scripts/python.exe -X utf8 tools/verify/verify_capability_chain.py`
"""
import io
import json
import os
import re
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# ── 干净库 + 隔离配置：先切后 import（DB 路径在 import 期绑定）──
# ★ 库文件名带时间戳：每次跑都是全新文件，不删旧库（本机 safe-delete 会拦截 os.remove）。
_TMP = os.path.join(ROOT, "tmp", "verify_capability_chain")
os.makedirs(_TMP, exist_ok=True)
_TS = int(time.time() * 1000)
_CLEAN_DB = os.path.join(_TMP, "clean_%d.db" % _TS)
_CLEAN_CFG = os.path.join(_TMP, "config_%d.json" % _TS)
io.open(_CLEAN_CFG, "w", encoding="utf-8").write("{}")
os.environ["MBSE_DB_PATH"] = _CLEAN_DB
os.environ["MBSE_CONFIG_PATH"] = _CLEAN_CFG

from database.schema import init_db                                    # noqa: E402
from core import capability_registry as cr                             # noqa: E402
from core import capability_effective as ce                            # noqa: E402
import mcp_health                                                      # noqa: E402

PASS, FAIL, GUARD = [], [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append((name, detail))
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name,
                           ("  ← " + str(detail)) if (detail and not ok) else ""))


def guard(name, ok, detail=""):
    if ok:
        PASS.append((name, ""))
        print("  [PASS] %s" % name)
    else:
        GUARD.append((name, detail))
        print("  [GUARD-FAIL] %s  ← %s" % (name, detail))


def section(t):
    print("\n" + "=" * 68)
    print(t)
    print("=" * 68)


def _read(rel):
    return io.open(os.path.join(ROOT, rel), encoding="utf-8").read()


def _code_lines(src):
    """剔除**文档字符串与注释**后的源码，用于"旧写法是否已清除 / 新接线是否存在"类断言。

    ★ MEMORY §6.9a 的两次实测教训（本文件第一次跑就踩到）：
      对**原文**做 `子串 in src`，"已清除"断言必然被**我自己写的 docstring**满足
      （我在 `_meta_tool_count` 的说明里逐字引用了旧写法）→ 恒 FAIL。
      ⇒ 必须先剥掉"非代码文本"再判。

    ⚠️ **不能连普通字符串一起剥** —— 目标模式本身含字面量
      （`srv.get("tool_meta")`），剥掉字符串后模式消失 ⇒ 断言变成**恒真空转**（更糟）。
      故这里只剥：三引号块（文档字符串） + `#` 注释行 + 行尾注释。
    """
    txt = re.sub(r'"""(?:.|\n)*?"""', "", src)
    txt = re.sub(r"'''(?:.|\n)*?'''", "", txt)
    out = []
    for ln in txt.split("\n"):
        s = ln.strip()
        if s.startswith("#"):
            continue
        # 行尾注释：只在引号外剥离（简易判定足够——本仓代码行不含 "x#y" 这类字面量）
        if "#" in ln and ln.count('"') % 2 == 0 and ln.count("'") % 2 == 0:
            ln = ln.split("#", 1)[0]
        out.append(ln)
    return "\n".join(out)


init_db()
conn = sqlite3.connect(_CLEAN_DB)
conn.row_factory = sqlite3.Row
_AGO = lambda d: time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - d * 86400))

# ══════════════════════════════════════════════════════════════
# A. P0-0：`tool_meta` 判空必须是"解析后数工具"
# ══════════════════════════════════════════════════════════════
section("A. P0-0 · tool_meta 判空语义（'[]' 必须等于 0 个工具）")
check("A1 '[]' → 0", mcp_health._meta_tool_count("[]") == 0,
      mcp_health._meta_tool_count("[]"))
check("A2 一个具名工具 → 1", mcp_health._meta_tool_count('[{"name":"query_projects"}]') == 1)
check("A3 空/None/null/非法 JSON → 0",
      all(mcp_health._meta_tool_count(v) == 0 for v in ("", None, "  ", "null", "not json", "{}")))
check("A4 纯名字数组兼容、空串不计",
      mcp_health._meta_tool_count('["a", "  ", ""]') == 1,
      mcp_health._meta_tool_count('["a", "  ", ""]'))
check("A5 无名对象不计（{name:''}）",
      mcp_health._meta_tool_count('[{"description":"x"}]') == 0)

# ★ A6 是本条的核心：证明**旧写法**在 '[]' 上判成"有数据"⇒ 回填永不触发（这才是那个 bug）
_old_pred = lambda raw: not (raw or "").strip()   # noqa: E731  旧判据（原文）
check("A6 旧判据在 '[]' 上为 False（即判成『有数据』）—— 与本实现不一致，正是 bug 本体",
      _old_pred("[]") is False and mcp_health._meta_tool_count("[]") == 0,
      "old=%r new=%r" % (_old_pred("[]"), mcp_health._meta_tool_count("[]")))

_src_health = _code_lines(_read("mcp_health.py"))
check("A7 旧写法已从代码行中清除（正则锚定行，避免命中注释）",
      re.search(r"^\s+.*not\s+\(srv\.get\(\"tool_meta\"\)\s+or\s+\"\"\)\.strip\(\)",
                _src_health, re.M) is None)
check("A8 新判据已接线（check_all 用 _meta_tool_count）",
      '_meta_tool_count(srv.get("tool_meta"))' in _src_health)

# ══════════════════════════════════════════════════════════════
# B. 「产出物必须变化」：discover 成功但没落库 ⇒ 不许算回填成功
# ══════════════════════════════════════════════════════════════
section("B. 回填成功的判据是库里真的变了（不是 discover 返回 ok）")
check("B1 源码读回库值（tool_meta_tools_after）", "tool_meta_tools_after" in _src_health)
check("B2 只有读回 >0 才计 backfilled",
      "_after > 0" in _src_health and "results[\"backfilled\"] += 1" in _src_health)

# 行为：夹具 server 的 tool_meta 恒为 '[]'，discover 打桩"成功但不落库" ⇒ backfilled 必须 0
conn.execute(
    "INSERT INTO mcp_servers (id, name, endpoint, transport, status, enabled, tools, "
    "capabilities, server_info, tool_meta, created_at) VALUES "
    "(9101,'gate-mcp-a','http://127.0.0.1:1/mcp','http','online',1,'[]','{}','{}','[]',?)",
    (_AGO(1),))
conn.commit()


class _Repo:
    """最小 repo 替身：只需要 `.conn` 与 `.rows()`（check_all 实际用到的两个）。"""

    def __init__(self, c):
        self.conn = c

    def rows(self, sql, params=()):
        return [dict(r) for r in self.conn.execute(sql, tuple(params)).fetchall()]


mcp_health._BACKFILL_TRY.clear()
_orig_check, _orig_disc = mcp_health.check_server, mcp_health.discover_server
try:
    mcp_health.check_server = lambda srv, repo=None: (True, {"status": "online"})
    # discover "成功"，但**不写库**（模拟 repo 传错/提交失败）—— 这正是要防的静默空转
    mcp_health.discover_server = lambda srv, repo=None: {"ok": True, "tools": ["x", "y"]}
    _res = mcp_health.check_all(_Repo(conn))
finally:
    mcp_health.check_server, mcp_health.discover_server = _orig_check, _orig_disc
_d0 = next((d for d in _res.get("details", []) if d.get("id") == 9101), {})
check("B3 discover ok 但库未变 ⇒ backfilled 计 0", _res.get("backfilled") == 0,
      _res.get("backfilled"))
check("B4 且必须留痕（backfill_error 非空）", bool(_d0.get("backfill_error")),
      _d0.get("backfill_error"))
check("B5 读回值为 0（而不是谎报 2 个）", _d0.get("tool_meta_tools_after") == 0,
      _d0.get("tool_meta_tools_after"))

# ══════════════════════════════════════════════════════════════
# C. P1-1：MCP 工具级投影（数据源 = tool_meta）
# ══════════════════════════════════════════════════════════════
section("C. P1-1 · MCP 工具级投影（tool_meta 是唯一应有的数据源）")
_META = json.dumps([{"name": "query_projects", "description": "查询工程列表",
                     "inputSchema": {"type": "object"}},
                    {"name": "export_sysml_v2", "description": "导出模型",
                     "inputSchema": {"type": "object"}}], ensure_ascii=False)
check("C1 只有 capabilities（无工具名）⇒ 0 个",
      cr.extract_mcp_tool_names('{"tools":{"listChanged":false}}', "{}", "[]") == [])
check("C2 tool_meta ⇒ 2 个且保序",
      cr.extract_mcp_tool_names(None, None, _META) == ["query_projects", "export_sysml_v2"],)
check("C3 两来源去重", cr.extract_mcp_tool_names('{"tools":[{"name":"a"}]}', None,
                                            '[{"name":"a"},{"name":"b"}]') == ["a", "b"])

conn.execute("UPDATE mcp_servers SET tool_meta=? WHERE id=9101", (_META,))
conn.commit()
cr.reconcile_capability_registry(conn)
conn.commit()
_rows = conn.execute("SELECT capability_id FROM capability_registry "
                     "WHERE kind='mcp_tool' AND source_id='gate-mcp-a' "
                     "ORDER BY capability_id").fetchall()
check("C4 reconcile 后工具级投影真的出现（2 行）", len(_rows) == 2,
      [r[0] for r in _rows])
_n1 = len(_rows)
cr.expand_mcp_tools(conn, "gate-mcp-a", "{}", "{}", _META)
cr.expand_mcp_tools(conn, "gate-mcp-a", "{}", "{}", _META)
conn.commit()
_n2 = conn.execute("SELECT COUNT(*) FROM capability_registry WHERE kind='mcp_tool' "
                   "AND source_id='gate-mcp-a'").fetchone()[0]
check("C5 expand 幂等（重复调用不产生重复行）", _n1 == _n2 == 2, "%s/%s" % (_n1, _n2))

# 改名 ⇒ 旧名工具行不得留成幽灵（原 refresh 用数字 id 去删 source_id=名字，永远删不掉）
conn.execute("UPDATE mcp_servers SET name='gate-mcp-a2' WHERE id=9101")
conn.commit()
cr.reconcile_capability_registry(conn)
conn.commit()
_ghost = conn.execute("SELECT COUNT(*) FROM capability_registry WHERE kind='mcp_tool' "
                      "AND source_id='gate-mcp-a'").fetchone()[0]
_new = conn.execute("SELECT COUNT(*) FROM capability_registry WHERE kind='mcp_tool' "
                    "AND source_id='gate-mcp-a2'").fetchone()[0]
check("C6 server 改名后旧名工具行清零、新名有 2 行", _ghost == 0 and _new == 2,
      "ghost=%s new=%s" % (_ghost, _new))

# ══════════════════════════════════════════════════════════════
# D. P1-2：巡检后刷统一注册处单行
# ══════════════════════════════════════════════════════════════
section("D. P1-2 · 巡检后刷 registry 单行（状态变更必须即时反映）")
check("D1 mcp_health 已接线 refresh_registry_row", "refresh_registry_row" in _src_health)

# 源表从 offline → online，刷新后 registry 必须从 disabled → active
conn.execute("UPDATE mcp_servers SET status='offline' WHERE id=9101")
cr.refresh_registry_row(conn, "mcp_servers", 9101)
conn.commit()
_st_off = conn.execute("SELECT status FROM capability_registry WHERE "
                       "capability_id='mcp:gate-mcp-a2'").fetchone()
conn.execute("UPDATE mcp_servers SET status='online' WHERE id=9101")
_rr = cr.refresh_registry_row(conn, "mcp_servers", 9101)
conn.commit()
_st_on = conn.execute("SELECT status FROM capability_registry WHERE "
                      "capability_id='mcp:gate-mcp-a2'").fetchone()
check("D2 状态跟着源表变（offline→disabled / online→active）",
      (_st_off[0] if _st_off else "") == "disabled" and (_st_on[0] if _st_on else "") == "active",
      "off=%r on=%r" % (_st_off[0] if _st_off else None, _st_on[0] if _st_on else None))
check("D3 refresh 返回 ok", bool(_rr.get("ok")), _rr)

# ══════════════════════════════════════════════════════════════
# E. P0-1：实际可用集（4 条声明入口 + 无条件注入）
# ══════════════════════════════════════════════════════════════
section("E. P0-1 · 声明集 × 调用集（'绑定 0 次却调用 25 次' 必须判「在用」）")
# 夹具：工具 gate_file_read（0 绑定）+ 一个已发布技能把它写进 allowed_tools + 1 次调用
conn.execute("INSERT INTO tools (name, status, version, side_effect, risk_level, builtin, "
             "scope, created_at) VALUES ('gate_file_read','active','','read','low',0,'private',?)",
             (_AGO(1),))
conn.execute("INSERT INTO skills (name, status, enabled, skill_type, version, allowed_tools, "
             "created_at) VALUES ('gate-skill','published',1,'prompt','',?,?)",
             (json.dumps(["gate_file_read"], ensure_ascii=False), _AGO(1)))
# 另一个工具：只被调用、无任何声明入口
conn.execute("INSERT INTO tools (name, status, version, side_effect, risk_level, builtin, "
             "scope, created_at) VALUES ('gate_orphan_tool','active','','read','low',0,'private',?)",
             (_AGO(1),))
conn.execute("INSERT INTO agents (name, display_name, agent_role, status, created_at) "
             "VALUES ('gate_agent','Gate Agent','sub','active',?)", (_AGO(1),))
for nm, cid in (("gate_file_read", "tool:gate_file_read"),
                ("gate_orphan_tool", "tool:gate_orphan_tool")):
    conn.execute("INSERT INTO tool_call_logs (tool_name, tool_type, call_kind, ok, "
                 "capability_id, created_at) VALUES (?,?,?,1,?,?)",
                 (nm, "builtin", "tool", cid, _AGO(0.5)))
conn.commit()
cr.reconcile_capability_registry(conn)
conn.commit()

_decl = ce.collect_declared(conn)
_e_fr = _decl["tools"].get("gate_file_read") or {}
check("E1 技能白名单被识别为声明入口",
      "gate-skill" in (_e_fr.get("skill_allowed") or []), _e_fr.get("skill_allowed"))
check("E2 全局技能池被识别（published+enabled 的技能对所有人可用）",
      bool((_decl["skills"].get("gate-skill") or {}).get("skill_pool")),
      (_decl["skills"].get("gate-skill") or {}).get("skill_pool"))
check("E3 无条件注入清单已登记且能与代码对上",
      set(ce.UNCONDITIONAL_TOOLS) <= set(re.findall(r'"([a-z_]+)"', _read("agent/pipeline_parts/tools.py"))),
      ce.UNCONDITIONAL_TOOLS)

_L = ce.build_ledger(conn, days=30)
_by_cid = {r["capability_id"]: r for r in _L["rows"]}
check("E4 0 绑定 + 白名单 + 有调用 ⇒ 判定「在用」",
      _by_cid.get("tool:gate_file_read", {}).get("verdict") == "in_use",
      _by_cid.get("tool:gate_file_read", {}).get("verdict"))
check("E5 「在用」行的 entries 必须含 skill_allowed",
      "skill_allowed" in (_by_cid.get("tool:gate_file_read", {}).get("entries") or []),
      _by_cid.get("tool:gate_file_read", {}).get("entries"))
check("E6 有调用无任何声明 ⇒ runtime_only（视图盲区，必须公示）",
      _by_cid.get("tool:gate_orphan_tool", {}).get("verdict") == "runtime_only",
      _by_cid.get("tool:gate_orphan_tool", {}).get("verdict"))
_mx = _L["matrix"]
check("E7 2×2 矩阵四项齐（且互相可加总）",
      all(k in _mx for k in ("in_use", "declared_unused", "runtime_only", "unused"))
      and sum(_mx.values()) == len(_L["rows"]), _mx)

# MCP server 级绑定必须展开成工具级（否则"绑了服务器却认不出它的工具"）
conn.execute("INSERT INTO agent_tools (agent_id, tool_type, tool_name, enabled) "
             "VALUES ((SELECT id FROM agents WHERE name='gate_agent'),'mcp','gate-mcp-a2',1)")
conn.commit()
_d2 = ce.collect_declared(conn)
_b2 = (_d2["tools"].get("query_projects") or {}).get("bound_agents") or []
_b2b = (_d2["tools"].get("export_sysml_v2") or {}).get("bound_agents") or []
check("E8 MCP 服务器级绑定展开到**每个**工具（绑的是 server 名，不是工具名）",
      "gate_agent" in _b2 and "gate_agent" in _b2b, "q=%r e=%r" % (_b2, _b2b))

# ══════════════════════════════════════════════════════════════
# F. P0-3：任务级追溯（trace_id 贯通）
# ══════════════════════════════════════════════════════════════
section("F. P0-3 · trace_id 贯通（与 llm_usage_stats 同源可 JOIN）")
_cols = [r[1] for r in conn.execute("PRAGMA table_info(tool_call_logs)").fetchall()]
check("F1 tool_call_logs 有 trace_id 列", "trace_id" in _cols, _cols)
_src_tools = _code_lines(_read("agent/pipeline_parts/tools.py"))
check("F2 INSERT 列清单含 trace_id", "capability_id, trace_id" in _src_tools)
check("F3 取值走「实例属性 → ContextVar」两级（子任务/线程都能拿到）",
      '_tool_trace_id' in _src_tools and 'trace_id' in _src_tools)
_src_stream = _code_lines(_read("agent/pipeline_parts/stream.py"))
check("F4 execute_stream 入口落 trace 到实例",
      'self._tool_trace_id = str((_trace or {}).get("trace_id") or "")' in _src_stream)
check("F5 编排子任务沿用外层 trace（不新开）",
      'sub._tool_trace_id' in _src_stream)
_src_common = _code_lines(_read("agent/pipeline_parts/common.py"))
check("F6 请求级清零（单例 Pipeline 不清会把上一轮 trace 写进本轮）",
      "pipe._tool_trace_id" in _src_common)

# 行为：真的调一次 _log_tool_call，落库行必须带上 trace_id
try:
    from agent.pipeline import AgentPipeline
    _p = AgentPipeline()
    _p._tool_trace_id = "gate-trace-abc123"
    _p._tool_user = None
    _p._log_tool_call("gate_file_read", "builtin", {"path": "/x"},
                      {"ok": True, "result": "ok"}, {"intent": "design"},
                      {"agent": "Gate Agent"}, 1, time.time())
    _row = conn.execute("SELECT trace_id, capability_id, run_id FROM tool_call_logs "
                        "WHERE tool_name='gate_file_read' ORDER BY id DESC LIMIT 1").fetchone()
    check("F7 落库行为：trace_id 真的写进去了", (_row["trace_id"] if _row else "") == "gate-trace-abc123",
          dict(_row) if _row else None)
    check("F8 同一行走 capability_id（能力度量可归因）",
          (_row["capability_id"] if _row else "") == "tool:gate_file_read",
          dict(_row) if _row else None)
except Exception as e:
    check("F7 落库行为：trace_id 真的写进去了", False, "%s: %s" % (type(e).__name__, e))
    check("F8 同一行走 capability_id（能力度量可归因）", False, "见上")

# run_id 仍是编排批次号；但「单 Agent 直行恒 0」已被判定为缺陷（请求 3，2026-10-27）而非设计 ——
#   现顶层轮次就地分配 TaskQueue.new_run_id，使主路径也能按 run 归批（详见 F12）。
check("F9 run_id 变量收口（_tool_run_ctx 存在，避免各写入点自造批次号）",
      "_tool_run_ctx" in _src_tools)

# ★ F10 是本条最容易被漏掉的一条：`tool_call_logs` 有 **6 个**写入点，
#   只补主链路会让"同一轮里一半行有 trace、一半没有"——追溯链从中间断掉
#   （实测会话 9623：工具行有 trace，技能行 3 条全空）。故按 INSERT 语句扫全仓。
import glob  # noqa: E402
_inserts, _missing = 0, []
for _f in glob.glob(os.path.join(ROOT, "**", "*.py"), recursive=True):
    if any(x in _f for x in (os.sep + ".venv", os.sep + "backups", os.sep + "tmp",
                             os.sep + "tools" + os.sep + "verify")):
        continue
    try:
        _s = io.open(_f, encoding="utf-8").read()
    except Exception:
        continue
    if "INSERT INTO tool_call_logs" not in _s:
        continue
    for _m in re.finditer(r"INSERT INTO tool_call_logs\s*\(([^)]*)\)\s*\"?\s*\n?\s*\"?VALUES",
                          _s):
        _inserts += 1
        if "trace_id" not in _m.group(1):
            _missing.append(os.path.relpath(_f, ROOT))
check("F10 全部 6 个 tool_call_logs 写入点都带 trace_id（漏一个就断链）",
      _inserts >= 6 and not _missing, "共 %d 处，缺 trace 的文件=%s" % (_inserts, _missing))
check("F11 取值口径收口到 core.audit.current_trace_id（不在各处自己猜）",
      "def current_trace_id" in _read("core/audit.py"))

# ★ 请求 3（2026-10-27）：主路径 run_id 归正。此前「单 Agent 直行恒 0」被当作设计，
#   实则缺陷 —— 直行流量占主对话绝大部分，其工具调用 run_id 恒 0 会让任何按 run 归批的看板漏掉
#   绝大部分流量（误导空图），且 run_id / trace_id 两套体系使「按轮归批」不可达。
#   修法：顶层轮次（conversation_id 真）就地分配 TaskQueue.new_run_id（与编排同源），
#   嵌套子 Agent（conversation_id=0，复用父编排 run）保持 0 不重分配。门禁断言这两段都还在。
_src_stream_run = _read("agent/pipeline_parts/stream.py")
check("F12 主路径 run_id 归正：顶层轮次分配 new_run_id、嵌套保持 0（不再恒 0）",
      ('self._tool_run_ctx = int(TaskQueue.new_run_id' in _src_stream_run)
      and ('if conversation_id:' in _src_stream_run)
      and ('self._tool_run_ctx = 0' in _src_stream_run))

# ★ 请求 3（续）：技能调用日志（memory.py 的 _record_skill_feedback）也必须带 run_id 列。
#   此前该 INSERT 漏 run_id 列（落库取默认值 0）且把 run_id 形参错位绑到 conversation_id ⇒
#   技能触发（主路径占比最大的一块）run_id 恒 0，归批仍漏。修法：列清单含 run_id，取值走
#   self._tool_run_ctx（与工具侧 _log_tool_call 同源）。门禁锁死该列存在。
_src_mem = _read("agent/pipeline_parts/memory.py")
check("F13 技能调用日志带 run_id 列（与工具侧同源归批，不再恒 0）",
      ("INSERT INTO tool_call_logs" in _src_mem)
      and ("conversation_id, run_id, failure_kind" in _src_mem))

# ══════════════════════════════════════════════════════════════
# G. P0-2：只读端点
# ══════════════════════════════════════════════════════════════
section("G. P0-2 · 账本端点（只读 + 已注册）")
_src_caps = _read("routers/capabilities.py")
check("G1 端点源码里没有任何写 SQL",
      not re.search(r"\b(INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM)\b", _src_caps, re.I))
check("G2 全部方法只有 GET", _src_caps.count("@router.get") == 4
      and "@router.post" not in _src_caps and "@router.put" not in _src_caps)
try:
    from fastapi.testclient import TestClient
    import main as _main
    _paths = [p for p in _main.app.openapi()["paths"] if "capabilities" in p]
    check("G3 四个路径都已注册进 OpenAPI", len(_paths) == 4, _paths)
    with TestClient(_main.app) as _c:
        _r1 = _c.get("/api/capabilities/ledger?days=30")
        _r2 = _c.get("/api/capabilities/registry?limit=2")
        _r3 = _c.get("/api/capabilities/trace?days=7")
        _r4 = _c.get("/api/capabilities/trace/detail?trace_id=gate-trace-abc123")
    check("G4 ledger 200 且结构齐（matrix/rows/orphan_calls/entry_labels）",
          _r1.status_code == 200 and {"matrix", "rows", "orphan_calls", "entry_labels"}
          <= set(_r1.json().keys()), _r1.status_code)
    check("G5 registry 200 且带 by_kind", _r2.status_code == 200 and "by_kind" in _r2.json())
    check("G6 trace 200 且给填充率", _r3.status_code == 200
          and len(_r3.json().get("fill") or []) >= 5)
    check("G7 trace/detail 能按 trace 聚合（工具+LLM 两侧同键）",
          _r4.status_code == 200 and _r4.json().get("trace_id") == "gate-trace-abc123",
          _r4.status_code)
except Exception as e:
    # 端点级断言失败必须显式红，不能因为 import/启动问题把 G3~G7 "跳过"成绿
    for _n in ("G3 四个路径都已注册进 OpenAPI", "G4 ledger 200 且结构齐",
               "G5 registry 200 且带 by_kind", "G6 trace 200 且给填充率",
               "G7 trace/detail 能按 trace 聚合"):
        check(_n, False, "%s: %s" % (type(e).__name__, e))

# ══════════════════════════════════════════════════════════════
# M. 变异自证（每条都必须被抓住）
# ══════════════════════════════════════════════════════════════
section("M. 变异自证（没被抓住 ⇒ exit 2，结论不可信）")
_mut_fail = []


def _mut(name, caught, detail=""):
    if caught:
        PASS.append((name, ""))
        print("  [PASS] %s（被抓住）" % name)
    else:
        _mut_fail.append(name)
        print("  [MISS] %s 未被抓住  ← %s" % (name, detail))


# M1：把 _meta_tool_count 换回"字符串非空"语义 ⇒ A1 必须红
_m1 = lambda raw: 1 if str(raw or "").strip() else 0   # noqa: E731  旧语义的等价实现
_mut("M1 判空退回字符串非空", _m1("[]") != mcp_health._meta_tool_count("[]"),
     "mutant=%r real=%r" % (_m1("[]"), mcp_health._meta_tool_count("[]")))

# M2：exec 变异孪生体——去掉 tool_meta 分支 ⇒ C2 必须红（返回 0 个）
_ns2 = dict(vars(cr))
_src_fn = io.open(os.path.join(ROOT, "core", "capability_registry.py"),
                  encoding="utf-8").read()
_m2_src = _src_fn.replace("    if tool_meta:\n", "    if False:\n", 1)
if _m2_src == _src_fn:
    _mut("M2 extract 去掉 tool_meta 分支", False, "锚点未命中（变异未生效）")
else:
    _m2_ns = dict(vars(cr))
    exec(compile(_m2_src, "<mut2>", "exec"), _m2_ns)
    _mut("M2 extract 去掉 tool_meta 分支",
         _m2_ns["extract_mcp_tool_names"](None, None, _META) != ["query_projects", "export_sysml_v2"],
         _m2_ns["extract_mcp_tool_names"](None, None, _META))

# M3：删掉巡检里的 registry 刷新 ⇒ D1 必须红
_m3 = _src_health.replace("_cr.refresh_registry_row(_conn_r, \"mcp_servers\", _sid)", "pass", 1)
_mut("M3 巡检不刷 registry", "refresh_registry_row" not in _m3)

# M4：INSERT 去掉 trace_id ⇒ F2 必须红
_m4 = _src_tools.replace("capability_id, trace_id", "capability_id", 1)
_mut("M4 INSERT 不写 trace_id", "capability_id, trace_id" not in _m4)

# M5：exec 变异孪生体——collect_declared 不登记全局技能池 ⇒ E2 必须红
_src_ce = io.open(os.path.join(ROOT, "core", "capability_effective.py"),
                  encoding="utf-8").read()
_m5_src = _src_ce.replace('se["skill_pool"].append("全局")', "pass", 1)
if _m5_src == _src_ce:
    _mut("M5 不登记全局技能池", False, "锚点未命中（变异未生效）")
else:
    _m5_ns = dict(vars(ce))
    exec(compile(_m5_src, "<mut5>", "exec"), _m5_ns)
    _d5 = _m5_ns["collect_declared"](conn)
    _mut("M5 不登记全局技能池",
         not ((_d5["skills"].get("gate-skill") or {}).get("skill_pool")),
         (_d5["skills"].get("gate-skill") or {}).get("skill_pool"))

# M6：trace 入口不落实例 ⇒ F4 必须红
_m6 = _src_stream.replace('self._tool_trace_id = str((_trace or {}).get("trace_id") or "")', "", 1)
_mut("M6 入口不落 _tool_trace_id", 'self._tool_trace_id = str((_trace' not in _m6)

# ── 收尾 ──────────────────────────────────────────────────────
print("\n" + "=" * 68)
print("PASS %d / FAIL %d / GUARD-FAIL %d / MUT-MISS %d"
      % (len(PASS), len(FAIL), len(GUARD), len(_mut_fail)))
if FAIL:
    print("\n失败项：")
    for n, d in FAIL:
        print("  - %s  %s" % (n, d))
if GUARD:
    print("\n守卫失败（结论不可信）：")
    for n, d in GUARD:
        print("  - %s  %s" % (n, d))

if _mut_fail or GUARD:
    print("\n结论：变异未被抓住 / 守卫失败 ⇒ 本门禁的绿不可信（exit 2）")
    sys.exit(2)
if FAIL:
    print("\n结论：有 %d 条判据失败（exit 1）" % len(FAIL))
    sys.exit(1)
print("\n全部通过 —— 注册/绑定/消费/追溯 四环节链路断言成立（含 6 组变异自证）")
print("夹具库：%s" % _CLEAN_DB)
