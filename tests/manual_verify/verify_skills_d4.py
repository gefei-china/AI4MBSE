"""D4 Skill 分层结构闭环验证：工具白名单（allowed_tools）+ 渐进披露（references/examples/scripts）。

场景：
T1. API 创建含 D4 字段的 Skill → 断言落库
T2. 仓储读取 → 4 个 D4 字段解析为 list 且值正确
T3. 发布 Skill（全局技能池可达）
T4. 全局技能池解析（bug 修复验证：JSON 字符串必须解析为 list，否则白名单按字符迭代）
T5. 绑定技能 bound_tools_for 含 D4 字段（bug 修复验证）
T6. _build_skill_prompt 渐进披露 → 📄📝⚙🔒 标记 + 白名单并集
T7. execute(skill_name=...) → _skill_allowed_tools 权威注入 + _tool_whitelist 生效
T8. 白名单外工具调用被拒绝（最小权限防御）
T9. 委派白名单 ∩ skill 白名单 = 交集（双限制取更严）
"""
import os
import sys
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["MBSE_LLM_FORCE_MOCK"] = "1"
_TEST_DB = os.path.join(os.path.dirname(__file__), "_d4_test.db")
if os.path.exists(_TEST_DB):
    os.remove(_TEST_DB)
os.environ["MBSE_DB_PATH"] = _TEST_DB

from database import init_db, get_db
init_db()

PASS = 0
FAIL = 0


def chk(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  → {detail}")


def main():
    # ── T1: API 创建含 D4 字段的 Skill ──
    print("== T1: API 创建含 D4 字段的 Skill ==")
    from fastapi.testclient import TestClient
    from main import app
    client = TestClient(app)
    payload = {
        "name": "D4白名单技能", "description": "D4 分层结构验证技能（工具白名单 + 渐进披露）",
        "skill_type": "tool_call", "category": "验证",
        "triggers": ["白名单", "progressive", "渐进披露"],
        "content": "命中本技能时：1) 只能调用工具白名单内的工具；2) 参考文档/示例/脚本按需读取，正文摘要优先。",
        "frontmatter": "{}",
        "allowed_tools": ["graph_retrieve"],
        "references": ["models/architecture.md", {"title": "SysML v2 指南", "path": "docs/sysml-guide.md"}],
        "examples": ["生成 BDD 视图示例", "生成 IBD 视图示例"],
        "scripts": ["scripts/preprocess.py", "scripts/graph_export.py"],
    }
    r = client.post("/api/studio/skills", json=payload)
    chk("创建成功 status=200", r.status_code == 200, r.text[:200])
    sid = r.json().get("id")
    chk("返回 id", bool(sid), r.text[:200])

    # ── T2: 仓储读取 D4 字段 ──
    print("== T2: 仓储读取 D4 字段 ==")
    from repositories.studio_repo import StudioRepo
    conn = get_db()
    sk = StudioRepo(conn).get_skill(sid)
    conn.close()
    chk("allowed_tools 解析为 list", isinstance(sk["allowed_tools"], list)
        and sk["allowed_tools"] == ["graph_retrieve"], str(sk.get("allowed_tools")))
    refs = [str(x) if isinstance(x, str) else str(x.get("title") or x.get("path"))
            for x in sk["references"]]
    chk("references 解析为 list 且含文件与字典条目", "models/architecture.md" in refs
        and "SysML v2 指南" in refs, str(refs))
    chk("examples 解析为 list", isinstance(sk["examples"], list)
        and len(sk["examples"]) == 2, str(sk.get("examples")))
    chk("scripts 解析为 list", isinstance(sk["scripts"], list)
        and sk["scripts"][0] == "scripts/preprocess.py", str(sk.get("scripts")))

    # ── T3: 发布 Skill ──
    print("== T3: 发布 Skill（进入全局技能池）==")
    r = client.post(f"/api/studio/skills/{sid}/publish")
    chk("发布 status=200", r.status_code == 200, r.text[:200])

    # ── T4: 全局技能池解析（bug 修复验证）──
    print("== T4: 全局技能池 JSON 字段解析（bug 修复验证）==")
    from agent import AgentPipeline
    pipe = AgentPipeline()
    pool = pipe._global_skill_pool()
    sk_pool = next((s for s in pool if s["name"] == "D4白名单技能"), None)
    chk("技能池命中已发布 Skill", sk_pool is not None, str([s["name"] for s in pool])[:200])
    chk("allowed_tools 为 list（非 JSON 字符串）", sk_pool is not None
        and isinstance(sk_pool["allowed_tools"], list), str(sk_pool.get("allowed_tools"))[:200])
    chk("references 为 list", sk_pool is not None and isinstance(sk_pool["references"], list),
        str(sk_pool.get("references"))[:200])

    # ── T5: 绑定技能 bound_tools_for 含 D4 字段（bug 修复验证）──
    print("== T5: 绑定技能 bound_tools_for 含 D4 字段（bug 修复验证）==")
    conn = get_db()
    conn.execute(
        "INSERT INTO agent_tools (agent_id, tool_type, tool_name) "
        "SELECT id, 'skill', ? FROM agents WHERE name='design'", ("D4白名单技能",))
    conn.commit()
    from repositories.agent_repo import AgentRepo
    design_id = conn.execute("SELECT id FROM agents WHERE name='design'").fetchone()["id"]
    binds = AgentRepo(conn).bound_tools_for(design_id)
    conn.close()
    b = next((x for x in binds if x["type"] == "skill" and x["name"] == "D4白名单技能"), None)
    chk("绑定技能含 allowed_tools 字段", b is not None and isinstance(b.get("allowed_tools"), list)
        and b["allowed_tools"] == ["graph_retrieve"], str(b)[:300])
    chk("绑定技能含 references/examples/scripts 字段", b is not None
        and isinstance(b.get("references"), list) and isinstance(b.get("examples"), list)
        and isinstance(b.get("scripts"), list), str({k: b.get(k) for k in ("references", "examples", "scripts")})[:300])

    # ── T6: _build_skill_prompt 渐进披露 + 白名单并集 ──
    print("== T6: _build_skill_prompt 渐进披露 + 白名单并集 ==")
    pipe._load_db_agents()
    prompt = pipe._build_skill_prompt("design", "用白名单技能做渐进披露的检索", user=None)
    chk("命中 Skill 并注入", "【Skill 已触发：D4白名单技能" in prompt, prompt[:200])
    chk("📄 参考文档清单披露", "📄 参考文档（需要时按需读取）" in prompt
        and "models/architecture.md" in prompt, prompt[:500])
    chk("📝 示例清单披露", "📝 示例（需要时按需读取）" in prompt
        and "生成 BDD 视图示例" in prompt, prompt[:500])
    chk("⚙ 脚本清单披露", "⚙ 脚本（需要时执行）" in prompt
        and "scripts/preprocess.py" in prompt, prompt[:500])
    chk("🔒 工具白名单披露", "🔒 工具白名单（仅可调用）：graph_retrieve" in prompt, prompt[:500])
    chk("白名单并集收集", getattr(pipe, "_skill_allowed_tools", None) == {"graph_retrieve"},
        str(getattr(pipe, "_skill_allowed_tools", None)))

    # ── T7: execute(skill_name=...) 权威白名单注入 ──
    print("== T7: execute(skill_name=...) 权威白名单注入 ==")
    pipe2 = AgentPipeline()
    pipe2.execute("按 D4白名单技能 执行一次检索", 0, forced_intent="chat",
                  skill_name="D4白名单技能", dry_run=True)
    chk("_skill_allowed_tools 权威注入", pipe2._skill_allowed_tools == {"graph_retrieve"},
        str(pipe2._skill_allowed_tools))
    chk("_tool_whitelist 生效", set(pipe2._tool_whitelist or []) == {"graph_retrieve"},
        str(pipe2._tool_whitelist))

    # ── T8: 白名单外工具调用被拒绝 ──
    print("== T8: 白名单外工具调用被拒绝（最小权限防御）==")
    pipe2._tool_intent_ctx = {"intent": "chat"}
    pipe2._tool_agent_ctx = {"agent": "chat"}
    pipe2._tool_conv_ctx = 0
    r_deny = pipe2._exec_tool_call("entity_create", {"name": "恶意写入"})
    chk("白名单外工具被拒绝", r_deny.get("ok") is False and "不在本次" in r_deny.get("result", ""),
        str(r_deny)[:200])
    r_allow = pipe2._exec_tool_call("graph_retrieve", {"query": "宽带通信载荷"})
    chk("白名单内工具可执行", r_allow.get("ok") is not False, str(r_allow)[:200])

    # ── T9: 委派白名单 ∩ skill 白名单 = 交集 ──
    print("== T9: 委派白名单 ∩ skill 白名单（双限制取更严）==")
    pipe3 = AgentPipeline()
    pipe3.execute("交集验证：委派 graph_retrieve+entity_create 但技能仅授权 graph_retrieve",
                  0, forced_intent="chat", skill_name="D4白名单技能",
                  tools_whitelist=["graph_retrieve", "entity_create"], dry_run=True)
    wl = set(pipe3._tool_whitelist or [])
    chk("交集 = {graph_retrieve}", wl == {"graph_retrieve"}, str(wl))

    # ── T10: 无白名单 skill（空 allowed_tools）不限制 ──
    print("== T10: 空白名单 Skill 不触发限制 ==")
    r10 = client.post("/api/studio/skills", json={
        "name": "D4开放技能", "description": "未声明白名单的普通技能",
        "skill_type": "text2json", "triggers": ["开放技能"],
        "content": "普通技能正文。", "frontmatter": "{}",
        "allowed_tools": [], "references": [], "examples": [], "scripts": [],
    })
    chk("创建开放技能成功", r10.status_code == 200, r10.text[:200])
    client.post(f"/api/studio/skills/{r10.json().get('id')}/publish")
    pipe._load_db_agents()
    p10 = pipe._build_skill_prompt("chat", "触发 开放技能 看看", user=None)
    chk("开放技能无🔒标记", "🔒" not in p10, p10[:300])
    chk("无白名单时 _skill_allowed_tools 为 None/空", not (pipe._skill_allowed_tools or set()),
        str(pipe._skill_allowed_tools))

    print(f"\n===== D4 RESULT: PASS={PASS} FAIL={FAIL} =====")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
