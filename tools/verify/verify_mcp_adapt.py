# CI-OPTIONAL: [local] tier=local（见 tools/gates_manifest.json）：需真实服务 / 真 LLM key / 生产真库金标或随包运行时资产，CI 无服务无 secret ⇒ 跑不了；本机已实测通过，改法须同时改清单 tier 并接线，不允许只删这行
# -*- coding: utf-8 -*-
"""门禁 · MCP 升级/变更时的副作用标注自适应（2026-11-06）

用户问：「如果后面调整了 MCP 的工具，或者增删 mcp 或者 tool，怎么适配」

答案的**可执行判据**就在这里 —— 用内存库真跑 `sync_mcp_tools`，覆盖五种变更：

  ① 平台新增工具      → 自动登记并按契约定档（读/写各一）
  ② 工具语义从读变写  → auto 项自动改档（这是手工标注做不到的）
  ③ 人工覆写后再升级  → manual 项**必须保持原判**（不能被平台语义带跑）
  ④ 平台删除工具      → 标 deprecated，**不物理删除**（保绑定引用）
  ⑤ 工具重新出现      → 从 deprecated 回到 active

另含 UI/文案三处回归（同一轮用户反馈）：
  F. Agent 编辑弹窗标题不再是写死的「新建 Agent」
  G. 「管理能力（审核/范围/下架）」空面板已移除，保留轻量跳转
  H. 同名技能清理后仍只有 1 份，且绑定未被级联误删

用法：./.venv/Scripts/python.exe -X utf8 tools/verify/verify_mcp_adapt.py
"""
import io
import os
import re
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


from agent.mcp_tool_sync import sync_mcp_tools   # noqa: E402

# ── 建一张与真库同构的 tools 表 ──
c = sqlite3.connect(":memory:")
c.row_factory = sqlite3.Row
c.execute("""CREATE TABLE tools (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL,
  source TEXT DEFAULT 'local', mcp_server_id INTEGER, description TEXT DEFAULT '',
  status TEXT DEFAULT 'active', input_schema TEXT DEFAULT '{}', version TEXT DEFAULT 'v1.0',
  side_effect TEXT DEFAULT 'read', risk_level TEXT DEFAULT 'low',
  side_effect_source TEXT DEFAULT 'auto', rule_version INTEGER DEFAULT 0)""")


def snap(name):
    r = c.execute("SELECT side_effect, status, side_effect_source FROM tools WHERE name=?",
                  (name,)).fetchone()
    return dict(r) if r else None


v1 = [{"name": "query_projects", "inputSchema": {"properties": {"k": {}}}},
      {"name": "create_model_elements", "inputSchema": {"properties": {"approvalId": {}}}},
      {"name": "delete_model_elements", "inputSchema": {"properties": {"approvalId": {}}}}]
s1 = sync_mcp_tools(c, 161, v1)
c.commit()
rec("A0 初始三档各一（read/write/destructive）",
    s1["counts"] == {"read": 1, "write": 1, "destructive": 1}, s1["counts"])

# ① 新增工具
v2 = v1 + [{"name": "query_new_report", "inputSchema": {"properties": {"k": {}}}},
           {"name": "apply_new_batch", "inputSchema": {"properties": {"commands": {}}}}]
s2 = sync_mcp_tools(c, 161, v2)
c.commit()
rec("A1 新增2个自动插入", s2["inserted"] == 2, s2["inserted"])
rec("A2 新增读工具判 read", (snap("query_new_report") or {}).get("side_effect") == "read")
rec("A3 新增批量命令工具判 write（commands 契约）",
    (snap("apply_new_batch") or {}).get("side_effect") == "write")

# ② 语义从读变写（平台给 query_projects 加了 approvalId）
v3 = [{"name": "query_projects", "inputSchema": {"properties": {"approvalId": {}}}}] + v2[1:]
sync_mcp_tools(c, 161, v3)
c.commit()
rec("A4 平台语义变更 → auto 项自动从 read 改 write",
    (snap("query_projects") or {}).get("side_effect") == "write", snap("query_projects"))

# ③ 人工覆写后再升级，manual 不能被带跑
c.execute("UPDATE tools SET side_effect='destructive', risk_level='high', "
          "side_effect_source='manual' WHERE name='query_projects'")
c.commit()
v4 = [{"name": "query_projects", "inputSchema": {"properties": {"k": {}}}}] + v2[1:]
sync_mcp_tools(c, 161, v4)
c.commit()
rec("A5 人工覆写后平台改回只读 → 保持 manual/destructive",
    (snap("query_projects") or {}).get("side_effect") == "destructive"
    and (snap("query_projects") or {}).get("side_effect_source") == "manual",
    snap("query_projects"))

# ④ 平台删除工具
v5 = [t for t in v4 if t["name"] != "delete_model_elements"]
s5 = sync_mcp_tools(c, 161, v5)
c.commit()
rec("A6 平台移除的工具标 deprecated（不删行）",
    (snap("delete_model_elements") or {}).get("status") == "deprecated", snap("delete_model_elements"))
rec("A7 移除计数正确", s5["deprecated"] == 1, s5["deprecated"])

# ⑤ 工具恢复
sync_mcp_tools(c, 161, v4)
c.commit()
rec("A8 工具重新出现 → 回到 active（标注不丢）",
    (snap("delete_model_elements") or {}).get("status") == "active"
    and (snap("delete_model_elements") or {}).get("side_effect") == "destructive",
    snap("delete_model_elements"))
rec("A9 全程无物理删除（条数 = 5）",
    c.execute("SELECT COUNT(*) FROM tools").fetchone()[0] == 5)

# ── F/G. UI 文案两处 ──
core = read("static/js/mods/01-core.js")
ag = read("static/js/mods/30-agents.js")
cap = read("static/js/mods/36-capability.js")
rec("F1 Agent 弹窗标题有 id（可切换）", 'id="agent-modal-title"' in core)
rec("F2 编辑态设「编辑 Agent「…」」", "编辑 Agent「" in ag)
rec("F3 新建态重置为「新建 Agent」", "t.textContent = '新建 Agent'" in ag)
rec("F4 未误用 window._agentsCache（let 不挂 window）",
    # ⚠️ 判据要匹配**实际用法**，不能只看文本里有没有这个词 ——
    #   代码注释里本身就写着"不能写 window._agentsCache"这句解释，
    #   用 `"window._agentsCache" not in ag` 会把解释文字判成违规（第一版即假红）。
    not re.search(r"window\._agentsCache\s*(?:\|\||=|\.)", ag))
rec("G1 管理能力按钮已移除", "cap-${page}-mbtn" not in cap)
rec("G2 管理能力空面板已移除", "-manage\" style=\"margin-top:12px;\"" not in cap)
rec("G3 保留治理跳转（功能未丢）", "治理见「插件市场」" in cap and "capJumpManage()" in cap)
rec("G4 死函数 capToggleManage 已删", "function capToggleManage" not in cap)

# ── J. 启动不得覆盖人工编辑的技能正文（2026-11-06 实测事故）──
#   现象：界面把「智源平台管理」正文合并成 5744 字（v1.4），重启后变回内置原文
#         4958 字（v1.3），而 `updated_at` 仍是编辑时刻（那条 UPDATE 不 SET updated_at）
#         ⇒ 连"被改过"都看不出来。根因：main.py:80 每次启动 seed_all → register_skills.main()。
#   修法：默认不覆盖 content/version，只补元数据；`--force-content` 才覆盖。
_rk = read("services/tools/register_skills.py")
rec("J1 存在 _FORCE_CONTENT 开关且默认关",
    "_FORCE_CONTENT = \"--force-content\" in sys.argv" in _rk)
rec("J2 默认分支不写 content/version",
    "UPDATE skills SET description=?, skill_type=?, triggers=?, category=?, " in _rk
    and "已更新元数据（保留人工正文）" in _rk)
rec("J3 强制分支仍保留（--force-content）",
    "已更新（--force-content 覆盖正文）" in _rk)
try:
    import register_skills as _RS
    rec("J4 模块可导入且默认不强制", _RS._FORCE_CONTENT is False, _RS._FORCE_CONTENT)
except Exception as _e:
    rec("J4 register_skills 可导入", False, str(_e)[:100])

# ── I. 改档操作入口（用户：「显示标 manual 怎么操作」）──
#   上一版只做了 manual/auto 标签**展示**，没有操作入口 —— 用户看到标签却没法设置。
#   判据：清单每行有下拉（自动/只读/写/破坏性），选择即调 POST 接口，
#        选具体档位 → manual；选「自动」→ 交回派生。
rec("I1 清单每行有改档下拉（mcp-eff-sel）", "mcp-eff-sel" in ag)
rec("I2 下拉含四个选项（自动/只读/写/破坏性）",
    all(x in ag for x in ("['auto','自动']", "['read','只读']",
                          "['write','写']", "['destructive','破坏性']")))
rec("I3 改档调用 side-effect 接口",
    "/side-effect" in ag and "onMcpEffectChange" in ag)
rec("I4 改档后原地刷新（保留搜索/筛选/滚动）",
    "reloadMcpToolsKeepView" in ag)
rec("I5 面板说明含「怎么改档」指引", "怎么改档" in core)
try:
    import httpx as _hx
    with _hx.Client(timeout=20, trust_env=False) as _cl:
        _r = _cl.get("http://127.0.0.1:8000/api/studio/mcp-servers/161/tools",
                     headers={"X-User-Id": "1"})
    _d = _r.json()
    _it = [x for x in (_d.get("items") or []) if x.get("name") == "export_sysml_v2"]
    rec("I6 接口返回 side_effect_source 字段（供 UI 显示 manual/auto）",
        bool(_it) and "side_effect_source" in _it[0],
        _it[0] if _it else None)
except Exception as _e:
    rec("I6 接口可查", False, str(_e)[:100])

# ── H. 同名技能收敛 + 删除不误清绑定 ──
db = os.path.join(ROOT, "mbse.db")
try:
    d = sqlite3.connect("file:" + db + "?mode=ro", uri=True)
    d.row_factory = sqlite3.Row
    n_same = d.execute("SELECT COUNT(*) FROM skills WHERE name='智源平台管理'").fetchone()[0]
    rec("H1 「智源平台管理」已收敛为 1 份", n_same == 1, n_same)
    row = d.execute("SELECT status, length(content) clen FROM skills "
                    "WHERE name='智源平台管理'").fetchone()
    rec("H2 保留的是 published 且含输出铁律",
        bool(row) and row["status"] == "published", dict(row) if row else None)
    ct = d.execute("SELECT content FROM skills WHERE name='智源平台管理'").fetchone()[0]
    rec("H3 正文含「★ 输出铁律」", "★ 输出铁律" in ct)
    binds = d.execute("SELECT COUNT(*) FROM agent_tools WHERE tool_type='skill' "
                      "AND tool_name='智源平台管理'").fetchone()[0]
    rec("H4 删草稿未误清正式版绑定（应=3）", binds == 3, binds)
    d.close()
except Exception as e:
    rec("H1-H4 技能数据可查", False, "%s: %s" % (type(e).__name__, str(e)[:100]))

# 删除接口的源码判据：只有同名一份不剩时才清绑定
sk_src = read("routers/studio_parts/skills.py")
rec("H5 delete_skill 按同名剩余份数决定是否清绑定",
    "_others" in sk_src and "if _others:" in sk_src)

print("\n%s  ALL_OK=%s  (%d passed / %d failed)"
      % ("=" * 60, not FAIL, len(OK), len(FAIL)))
if FAIL:
    for n, dd in FAIL:
        print("  x", n, "|", dd)
    sys.exit(1)
