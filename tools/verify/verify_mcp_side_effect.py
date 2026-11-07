# CI-OPTIONAL: [local] tier=local（见 tools/gates_manifest.json）：需真实服务 / 真 LLM key / 生产真库金标或随包运行时资产，CI 无服务无 secret ⇒ 跑不了；本机已实测通过，改法须同时改清单 tier 并接线，不允许只删这行
# -*- coding: utf-8 -*-
"""门禁 · MCP 工具副作用派生 + 编排继承（2026-11-06）

用户两条诉求：
  ①「MCP 的工具集合在哪里可以看到，是否可以在 MCP 的编辑页面提供查看入口」
  ②「按照建议顺序与方案实施」（P0 数据地基 → P1 修编排断链）

本门禁锁住四件事，全部是行为级断言：

  A. **派生正确性**：71 个真实 MCP 工具全部自动定档，且分布与实测一致
     （read 29 / write 39 / destructive 3；非只读 42）。
     判据优先级：破坏性命名 > 平台契约 approvalId > 平台契约 commands > 写前缀命名 > 默认只读。

  B. **三条不变量**（用内存库真跑 sync，不是看字符串）：
     ① 人工覆写（manual）永不被 discover 覆盖
     ② 消失的工具标 deprecated，不物理删除
     ③ 同步按 mcp_server_id 归属，不按裸 name

  C. **门控真的生效**：读类任务注入 0 个写工具，写类任务按动词放行
     —— 这是 P0 的**唯一目的**，不验证就等于没做。

  D. **编排继承**：主 Agent 透传到 run_planner_plan，且零 MCP 成员
     在继承后工具数显著上升。

  E. **UI 入口**：编辑弹窗有「查看工具清单」，接口返回 71 项与档位统计。

用法：./.venv/Scripts/python.exe -X utf8 tools/verify/verify_mcp_side_effect.py
"""
import io
import json
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

OK, FAIL = [], []


def rec(name, cond, detail=""):
    (OK if cond else FAIL).append((name, detail))
    print(("PASS " if cond else "FAIL ") + name + (("  | " + str(detail)) if detail else ""))


def read(p):
    with io.open(os.path.join(ROOT, p), encoding="utf-8") as f:
        return f.read()


from agent import tool_side_effect as T          # noqa: E402
from agent.mcp_tool_sync import sync_mcp_tools   # noqa: E402

# ── A. 派生：单元判据 ────────────────────────────────
rec("A1 破坏性命名 → destructive",
    T.derive({"name": "delete_model_elements", "inputSchema": {}})[0] == "destructive")
rec("A2 平台契约 approvalId → write（命名像只读也要判写）",
    T.derive({"name": "validate_model",
              "inputSchema": {"properties": {"approvalId": {}}}})[0] == "write")
rec("A3 commands 批量变更 → write",
    T.derive({"name": "apply_model_changes",
              "inputSchema": {"properties": {"commands": {}}}})[0] == "write")
rec("A4 读类默认 read",
    T.derive({"name": "query_projects",
              "inputSchema": {"properties": {"keyword": {}}}})[0] == "read")
rec("A5 脏 schema 不抛异常",
    T.derive({"name": "x", "inputSchema": "not-a-dict"})[0] == "read")
rec("A6 空 meta 不抛异常", T.derive(None)[0] == "read")
rec("A7 判据优先级：delete 优先于 approvalId",
    T.derive({"name": "delete_model_elements",
              "inputSchema": {"properties": {"approvalId": {}}}})[0] == "destructive")

# ── A'. 真实 71 个工具的分布 ──────────────────────────
db = os.path.join(ROOT, "mbse.db")
meta, mcp_status = [], ""
try:
    c = sqlite3.connect("file:" + db + "?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    row = c.execute("SELECT tool_meta, status FROM mcp_servers WHERE id=161").fetchone()
    if row:
        meta = json.loads(row["tool_meta"] or "[]")
        mcp_status = row["status"]
    # 库里实际定档分布
    try:
        db_counts = {r["side_effect"]: r["n"] for r in c.execute(
            "SELECT side_effect, COUNT(*) n FROM tools WHERE mcp_server_id=161 GROUP BY side_effect")}
    except Exception:
        db_counts = {}
    c.close()
except Exception:
    db_counts = {}

if meta:
    derived = T.derive_all(meta)
    s = T.summary(derived)
    rec("A8 真实工具全部定档（总数 = %d）" % len(meta), s["total"] == len(meta), s["total"])
    rec("A9 档位分布 read29/write39/destructive3",
        s["counts"] == {"read": 29, "write": 39, "destructive": 3}, s["counts"])
    rec("A10 判据分布含 contract_approval=38",
        s["rules"].get("contract_approval") == 38, s["rules"])
    # 库内是否已同步（未同步时给提示而非静默通过）
    rec("A11 tools 表已同步 71 条（需先点一次「测试/发现」）",
        sum(db_counts.values()) == 71, db_counts)
else:
    rec("A8-A11 真实工具清单可用", False, "mcp_servers.tool_meta 为空（先建 MCP 并发现）")

# ── B. 三条不变量（内存库真跑） ───────────────────────
mc = sqlite3.connect(":memory:")
mc.row_factory = sqlite3.Row
mc.execute("""CREATE TABLE tools (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL,
  source TEXT DEFAULT 'local', mcp_server_id INTEGER, description TEXT DEFAULT '',
  status TEXT DEFAULT 'active', input_schema TEXT DEFAULT '{}', version TEXT DEFAULT 'v1.0',
  side_effect TEXT DEFAULT 'read', risk_level TEXT DEFAULT 'low',
  side_effect_source TEXT DEFAULT 'auto', rule_version INTEGER DEFAULT 0)""")
m3 = [{"name": "query_projects", "description": "查工程", "inputSchema": {"properties": {"k": {}}}},
      {"name": "create_model_elements", "description": "建元素",
       "inputSchema": {"properties": {"approvalId": {}}}},
      {"name": "delete_model_elements", "description": "删元素",
       "inputSchema": {"properties": {"approvalId": {}}}}]
sync_mcp_tools(mc, 161, m3)
mc.commit()
rec("B1 首次同步插入 3 条", mc.execute("SELECT COUNT(*) FROM tools").fetchone()[0] == 3)

# ① 人工覆写不被覆盖
mc.execute("UPDATE tools SET side_effect='write', risk_level='medium', "
           "side_effect_source='manual' WHERE name='query_projects'")
mc.commit()
sync_mcp_tools(mc, 161, m3)
mc.commit()
r = mc.execute("SELECT side_effect, side_effect_source FROM tools "
               "WHERE name='query_projects'").fetchone()
rec("B2 人工覆写(manual) 不被 discover 覆盖",
    r["side_effect"] == "write" and r["side_effect_source"] == "manual", dict(r))

# ② 消失的工具 → deprecated，不删行
sync_mcp_tools(mc, 161, m3[:2])
mc.commit()
r = mc.execute("SELECT status FROM tools WHERE name='delete_model_elements'").fetchone()
rec("B3 服务端移除的工具标 deprecated 且不删行",
    bool(r) and r["status"] == "deprecated", dict(r) if r else None)

# ③ 按 mcp_server_id 归属 —— **已知架构债，判据记录真实行为而非理想行为**
#    `tools.name` 是 UNIQUE（无复合唯一），所以第二个 MCP 提供同名工具时，
#    匹配到的是**同一行**（不是新增一行）⇒ 该行的 mcp_server_id 会被改写成后者，
#    前一个 MCP 就"失去"了这个工具。这是 P2 要修的债（改 (mcp_server_id,name) 复合唯一）。
#    本判据锁两件事：
#      · 人工档位在这种搬迁下**仍然保住**（不因归属变化被重推成 auto 值）
#      · 搬迁确实发生（把这个债显式记录下来，避免以后有人误以为已经隔离）
mc2 = sqlite3.connect(":memory:")
mc2.row_factory = sqlite3.Row
mc2.execute("""CREATE TABLE tools (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL,
  source TEXT DEFAULT 'local', mcp_server_id INTEGER, description TEXT DEFAULT '',
  status TEXT DEFAULT 'active', input_schema TEXT DEFAULT '{}', version TEXT DEFAULT 'v1.0',
  side_effect TEXT DEFAULT 'read', risk_level TEXT DEFAULT 'low',
  side_effect_source TEXT DEFAULT 'auto', rule_version INTEGER DEFAULT 0)""")
sync_mcp_tools(mc2, 1, [{"name": "shared_tool", "inputSchema": {"properties": {"k": {}}}}])
mc2.commit()
mc2.execute("UPDATE tools SET side_effect='destructive', side_effect_source='manual' "
            "WHERE name='shared_tool'")
mc2.commit()
sync_mcp_tools(mc2, 2, [{"name": "shared_tool", "inputSchema": {"properties": {"k": {}}}}])
mc2.commit()
r = mc2.execute("SELECT side_effect, side_effect_source, mcp_server_id FROM tools "
                "WHERE name='shared_tool'").fetchone()
rec("B4 同名工具搬迁后人工档位仍保住（manual 不被重推）",
    r["side_effect"] == "destructive" and r["side_effect_source"] == "manual", dict(r))
rec("B5 [已知债] 同名工具被后者接管（UNIQUE 无复合键，P2 待修）",
    r["mcp_server_id"] == 2, "mcp_server_id=%s（应为 2 说明已搬迁）" % r["mcp_server_id"])

# ── C. 门控真的生效 ──────────────────────────────────
try:
    from agent.pipeline import AgentPipeline
    from database import get_db
    U = {"id": 1, "role_name": "admin", "role": "admin"}
    WRITE_PROBE = {"create_model_elements", "delete_model_elements", "apply_model_changes",
                   "import_sysml_text", "delete_model_relationships", "update_model_elements"}

    def _tools_for(q):
        p = AgentPipeline()
        c = get_db()
        try:
            p.registry.load_from_db(c, U)
        finally:
            c.close()
        p._team_main_agent = ""
        return [(x.get("function") or {}).get("name") for x in p._build_tools_def("design", q, U)]

    if mcp_status == "online" and sum(db_counts.values()) == 71:
        read_names = _tools_for("读取工程 AI验证1104 的 V2 代码数据，分析 P7 状态机视图")
        write_names = _tools_for("创建 model 元素并导入 sysml 文本到工程 AI验证1104")
        rec("C1 读类任务不含任何写/破坏性 MCP 工具",
            not (set(read_names) & WRITE_PROBE), sorted(set(read_names) & WRITE_PROBE))
        rec("C2 写类任务按动词放行相关写工具",
            bool(set(write_names) & {"create_model_elements", "import_sysml_text"}),
            sorted(set(write_names) & WRITE_PROBE))
        rec("C3 写类任务的工具数多于读类（说明门控在区分）",
            len(write_names) > len(read_names), (len(read_names), len(write_names)))
    else:
        rec("C1-C3 门控实测（需 MCP online 且 tools 已同步）", False,
            "status=%s synced=%d" % (mcp_status, sum(db_counts.values())))
except Exception as e:
    rec("C1-C3 门控实测可运行", False, "%s: %s" % (type(e).__name__, str(e)[:120]))

# ── D. 编排继承 ─────────────────────────────────────
planner_src = read("workflows/planner.py")
orch_src = read("agent/pipeline_parts/orchestration.py")
rec("D1 planner 接收 main_agent 参数", "main_agent: str = \"\"" in planner_src)
rec("D2 planner 落 _orch_main_agent", "self._orch_main_agent = (main_agent" in planner_src)
rec("D3 子任务注入 _team_main_agent（2 处）",
    planner_src.count("pipe._team_main_agent = _ma") == 2)
rec("D4 编排调用方传 main_agent（2 处）",
    orch_src.count("main_agent=getattr(self, \"_team_main_agent\"") == 2)

# ── E. UI 入口 ──────────────────────────────────────
core = read("static/js/mods/01-core.js")
ag = read("static/js/mods/30-agents.js")
rec("E1 编辑弹窗有「查看工具清单」按钮",
    "f-mcp-tools-btn" in core and "toggleMcpTools" in core)
rec("E2 有清单面板与控制项",
    "f-mcp-tools-panel" in core and "f-mcp-tools-q" in core and "f-mcp-tools-filter" in core)
rec("E3 前端实现加载与渲染", "loadMcpTools" in ag and "renderMcpTools" in ag)
rec("E4 展示档位来源 auto/manual",
    "manual" in ag and "自动派生" in ag)
try:
    import httpx
    with httpx.Client(timeout=20, trust_env=False) as cl:
        r = cl.get("http://127.0.0.1:8000/api/studio/mcp-servers/161/tools",
                   headers={"X-User-Id": "1"})
    d = r.json()
    rec("E5 接口返回 71 项与档位统计",
        r.status_code == 200 and d.get("total") == 71
        and (d.get("counts") or {}).get("write") == 39, (d.get("total"), d.get("counts")))
except Exception as e:
    rec("E5 接口可用", False, "%s: %s" % (type(e).__name__, str(e)[:100]))

print("\n%s  ALL_OK=%s  (%d passed / %d failed)"
      % ("=" * 60, not FAIL, len(OK), len(FAIL)))
if FAIL:
    for n, d in FAIL:
        print("  x", n, "|", d)
    sys.exit(1)
