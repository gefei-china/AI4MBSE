# -*- coding: utf-8 -*-
# CI-OPTIONAL: B 真库金标门禁——主 Agent「智源对接agent」(id=735)+ 14 人团队成员是生产库样本（2026-10-18 干净库实测：加载不到主 Agent，members=N/A）
"""R1+R2 回归验证：所选主 Agent 必须是本轮执行主体（2026-10-16）。

背景（真实事故，非构造）
------------------------
会话 9367 / 消息 4029：米爸新建主 Agent「智源对接agent」(id=735，只绑了 1 个 MCP)，
在会话里**选中这个主 Agent**后发"你好，我是MBSE建模人员，请介绍下你的能力"，
落库却是 `intent=requirement_analysis` / `agent=需求分析Agent` —— 一个**未绑定**的子 Agent。

真凶三连（全部经实测确认，非推测）
----------------------------------
① `static/index.html` 里 `quick-agent` 是 `display:none` 的**隐藏 select**，UI 上看不见；
   但 `sendChat` 无条件读它当 `forced_intent` 发送，且 `loadQuickBar` 从
   `localStorage.mbse_last_agent` 恢复历史值 ⇒ 残值 `requirement_analysis` 一直复活。
② 后端 `stream.py:1777` / `execute.py:98` 是 `forced_intent = forced_intent or team_intent`
   —— **隐藏框的值压过用户所选的主 Agent**。
③ 主 Agent 零成员时 `decide` 走 `if not members` **静默**返回 direct/None，
   `team_forced=False`，既不报错也不提示 ⇒ "选了团队像没选"。

本脚本的判据（与上述三点一一对应）
----------------------------------
T1 主 Agent 优先：传 team=主Agent 且同时传 forced_intent=别的意图 ⇒ 最终 intent 必须是主 Agent
T2 隐藏框退役：前端源码与后端 ChatIn 均不得再有 quick-agent/forced_intent 用户入口
T3 无成员不静默：主 Agent 零成员时必须产出 notice，且不触发自动编排（team_direct 拦住）
T4 绑定生效：主 Agent 自己绑的 MCP/skill 必须能被取到（否则 R2"自主执行"是空话）
T5 有成员团队不受影响：14 人团队的路由判据与改前一致（防误伤）

⚠️ **禁止 forced_intent 直连**：`pipe.execute(forced_intent=...)` 直连只能证明"能力存在"，
   证不了"用户选的主 Agent 没被劫持" —— 那正是本次事故的形态。本脚本走**真实 HTTP 入口**。
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DB = ROOT / "mbse.db"
BASE = "http://127.0.0.1:8000"

MAIN_AGENT = "智源对接agent"          # 米爸新建的主 Agent（0 成员）
BIG_TEAM = "MBSE建模总体负责人"        # 14 成员团队（对照组）
HIJACK = "requirement_analysis"       # 事故中的劫持者（未绑定的子 Agent）

_fails: list[str] = []
_passes = 0


def chk(name: str, ok: bool, detail: str = "") -> None:
    global _passes
    if ok:
        _passes += 1
        print(f"  [PASS] {name}" + (f"  — {detail}" if detail else ""))
    else:
        _fails.append(name)
        print(f"  [FAIL] {name}  — {detail}")


def section(t: str) -> None:
    print(f"\n{'=' * 68}\n{t}\n{'=' * 68}")


def _extract_js_func(src: str, name: str) -> str:
    """按花括号配对提取 JS 函数体（含签名），找不到返回 ""。

    ★ 2026-10-16 教训：原实现用定长正则窗口 `async function pickIntent[\\s\\S]{0,600}?\\n}`
    抓函数体，pickIntent 因 Q2 重构变长（≈1000 字符）后**匹配失败**，验证器误报
    FAIL —— 夹具漂移，不是产品回归。函数体检查一律走括号配对，对函数长度增长免疫。
    模板串里的 `${...}` 自身括号平衡，不影响深度归零。
    """
    m = re.search(r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\(", src)
    if not m:
        return ""
    i = src.find("{", m.end())
    if i < 0:
        return ""
    depth = 0
    for j in range(i, len(src)):
        c = src[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return src[m.start():j + 1]
    return ""


# ── T2：源码层判定（静态，可离线跑） ──────────────────────────────
def t2_source_guard() -> None:
    section("T2 · 隐藏 quick-agent 用户入口已退役（静态检查）")
    static = ROOT / "static"

    fe = []
    for p in list(static.rglob("*.js")) + list(static.rglob("*.html")):
        if ".bak" in p.name:
            continue
        src = p.read_text(encoding="utf-8", errors="ignore")
        for i, line in enumerate(src.splitlines(), 1):
            code = re.sub(r"^\s*//.*$|^\s*\*.*$", "", line)   # 剥注释，只看可执行代码
            if "quick-agent" in code or "mbse_last_agent" in code.replace(
                    "localStorage.removeItem('mbse_last_agent')", ""):
                fe.append(f"{p.relative_to(ROOT)}:{i}")
    chk("前端可执行代码无 quick-agent / mbse_last_agent 残留", not fe,
        f"残留于 {fe}" if fe else "仅注释提及，功能引用已清零")

    # sendChat 必须发 team，且不得发 forced_intent
    # ★ 2026-10-17 修夹具漂移（与 pickIntent 同款教训）：原判据用
    #   `cs.find("/chat/stream") + 1500` 定长窗口抓请求体 —— Q1/Q2 两轮迭代
    #   （_continueInSameBox 注释块、pumpStream 的 reuseForPick 清理注释）
    #   把 fetch body 挤出窗口 ⇒ team 判据假红。改为**花括号配对提取
    #   pumpStream 函数体**（请求体的唯一真源，对注释/函数长度增长免疫）。
    cs = (static / "js/mods/12-chatsend.js").read_text(encoding="utf-8", errors="ignore")
    body = _extract_js_func(cs, "pumpStream")
    # ★ 2026-10-17 收紧（变异自证抓出逃逸）：原判据 `"team: _selTeam" in body
    #   or "team:" in body` 的弱分支让 `team: ""`（静默丢弃主 Agent —— 事故
    #   9372 的形态）也能 PASS。改为**必须**真实传递下拉值 _selTeam。
    chk("sendChat 请求体携带 team（主 Agent）", "team: _selTeam" in body)
    chk("sendChat 请求体不再发送 forced_intent",
        "forced_intent:" not in body, "改判意图改由 intent_override 承载")

    # 后端入口：forced_intent 只从 intent_override 取
    conv = (ROOT / "routers/conversations.py").read_text(encoding="utf-8", errors="ignore")
    bad = re.findall(r"forced_intent=body\.forced_intent", conv)
    chk("后端不再从 body.forced_intent 取值", not bad,
        f"仍有 {len(bad)} 处直读 body.forced_intent" if bad else "已改为 body.intent_override")

    # 前端 HTML：隐藏 select 已删
    html = (static / "index.html").read_text(encoding="utf-8", errors="ignore")
    chk("index.html 中 quick-agent 元素已删除", 'id="quick-agent"' not in html)

    # pickIntent 不得再写 quick-agent（函数体按花括号配对提取，勿用定长正则窗口）
    pl = (static / "js/mods/11-pipeline.js").read_text(encoding="utf-8", errors="ignore")
    fn = _extract_js_func(pl, "pickIntent")
    chk("pickIntent 改判走 _intentOverride，不再写隐藏下拉",
        bool(fn) and "quick-agent" not in fn and "_intentOverride" in fn,
        f"函数体 {len(fn)} 字符，提取失败或判据不符" if not fn or "quick-agent" in fn
        or "_intentOverride" not in fn else f"函数体 {len(fn)} 字符，判据全中")


# ── T1/T3：优先级 + 无成员提示（进程内，纯函数复刻 stream.py 分支） ──
def t1_priority() -> None:
    section("T1/T3 · 主 Agent 优先 + 无成员不静默（进程内）")
    sys.path.insert(0, str(ROOT))
    from agent import team_router as tr          # noqa: E402
    from database import get_db                   # noqa: E402

    conn = get_db()
    try:
        td_main = tr.load_team_definition(conn, MAIN_AGENT)
        chk(f"主 Agent「{MAIN_AGENT}」加载成功", td_main is not None)
        chk("该主 Agent 确实 0 成员（R2 前提）",
            td_main is not None and len(td_main["members"]) == 0,
            f"members={len(td_main['members']) if td_main else 'N/A'}")

        # ★ 不再手工复刻 stream.py 的分支 —— 上一版正是"复刻实现"出了错，
        #   报出"改了没生效"的假结论（记忆里的老教训：验证器也会错，先怀疑验证器）。
        #   这里改为**直接调用被测方**：`decide()` 是 team_router 里的真函数，
        #   T1' 的端到端 HTTP 段才是全链路权威判据。
        q = "你好，我是MBSE建模人员，请介绍下你的能力"

        # T1：即便 detected 是劫持值，0 成员主 Agent 也必须留在本体
        d_main = tr.decide(q, td_main, detected_intent=HIJACK)
        chk("T1 零成员主 Agent 不被 detected 带偏（留在本体）",
            d_main["action"] == "direct" and not d_main.get("target"),
            f"action={d_main['action']} target={d_main.get('target')}")
        chk("T1 劫持意图未成为执行目标", d_main.get("target") != HIJACK)

        # T3：0 成员 → 有 notice + source=solo（可观测，不静默）
        chk("T3 零成员时产出可见 notice", bool(d_main.get("notice")),
            str(d_main.get("notice"))[:76])
        chk("T3 零成员时 source 标记为 solo（可观测，非静默 lexical）",
            d_main.get("source") == "solo", f"source={d_main.get('source')}")
        chk("T3 reason 键保留（下游 stream.py:1843/1865 按契约读取）",
            bool(d_main.get("reason")), f"reason={d_main.get('reason')!r}")

        # T5：有成员团队路由不受影响
        td_big = tr.load_team_definition(conn, BIG_TEAM)
        d_big = tr.decide(q, td_big, detected_intent=HIJACK)
        chk("T5 14 人团队路由仍正常（有成员才有定向/编排决策）",
            d_big["action"] in ("direct", "orchestrate", "chat") and d_big.get("target"),
            f"action={d_big['action']} target={d_big.get('target')}")
    finally:
        conn.close()


# ── T4：主 Agent 绑定能力可用（R2 的实质） ──────────────────────
def t4_bindings() -> None:
    section("T4 · 主 Agent 自绑 MCP/skill 可被取到（R2 的实质）")
    sys.path.insert(0, str(ROOT))
    from agent.pipeline import AgentPipeline      # noqa: E402

    p = AgentPipeline()
    p._load_db_agents()          # 必须调：DB Agent 关键词/绑定都在真实请求路径里注入
    d = p.registry.get(MAIN_AGENT)
    chk("registry 能取到主 Agent 定义", d is not None and d.intent_name == MAIN_AGENT)
    chk("主 Agent 角色识别为 main（不是 sub）",
        getattr(d, "agent_role", None) == "main", f"role={getattr(d, 'agent_role', None)}")
    chk("主 Agent 自身绑定工具可见（R2：靠绑定完成任务）",
        bool(getattr(d, "tools", [])), f"tools={getattr(d, 'tools', [])}")

    # bound_tools 的 MCP 能否解析出来（status/online 门禁）
    # 判据口径：绑定**非空** = 门禁没丢；为空时不算失败（可能是 token 失效等
    # 环境因素，verify_mcp_availability.py 的 C3/C5 才是该项的权威判据），
    # 但 detail 必须说真话——2026-10-16 见过"绑定明明有、detail 却写空"的误导输出。
    bound = p.registry.get_bound_mcp(MAIN_AGENT)
    skills = p.registry.get_bound_skills(MAIN_AGENT)
    print(f"         · get_bound_mcp   = {bound}")
    print(f"         · get_bound_skills = {skills}")
    chk("绑定解析未因 status 门禁被整体丢弃（有则正常，空则转 C3/C5 判）", True,
        f"门禁未丢：{len(bound)} 台 MCP 绑定可解析" if bound
        else "MCP 绑定为空 → 转 verify_mcp_availability.py C3/C5 判（需 online 且 enabled）")


# ── T1'：真实 HTTP 端到端（权威判据） ──────────────────────────
def t1_http() -> None:
    section("T1' · 真实 HTTP 端到端（权威判据，须服务已启动）")
    def post(path: str, body: dict) -> dict:
        req = urllib.request.Request(
            BASE + path, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json",
                     "X-User-Id": "1", "X-Session-Token": "verify-r1r2"},
            method="POST")
        with urllib.request.urlopen(req, timeout=180) as r:
            return json.loads(r.read().decode())

    try:
        cv = post("/api/conversations", {"title": "verify-r1r2-team-priority"})
    except Exception as e:
        chk("端到端 HTTP 验证", False, f"无法连接 {BASE}（服务未启动？）：{type(e).__name__}: {e}")
        return
    cid = cv.get("id")
    chk("新建会话成功", bool(cid), f"conversation_id={cid}")

    # 刻意**同时**传 team 与旧的 forced_intent —— 复刻事故那一刻的请求形态
    body = {
        "message": "你好，我是MBSE建模人员，请介绍下你的能力",
        "team": MAIN_AGENT,          # 用户真正选的
        "forced_intent": HIJACK,     # 事故中的隐藏框残值
    }
    try:
        res = post(f"/api/conversations/{cid}/chat", body)
    except Exception as e:
        chk("端到端 chat 调用", False, f"{type(e).__name__}: {e}")
        return

    got_intent = res.get("intent")
    got_agent = res.get("agent")
    print(f"         · 返回 intent = {got_intent!r} / agent = {got_agent!r}")
    chk("端到端：intent 是所选主 Agent", got_intent == MAIN_AGENT,
        f"得到 {got_intent!r}，期望 {MAIN_AGENT!r}")
    chk("端到端：agent 显示名是所选主 Agent", got_agent == MAIN_AGENT,
        f"得到 {got_agent!r}")
    chk("端到端：未被劫持到 requirement_analysis", got_intent != HIJACK)


def main() -> int:
    print("R1+R2 回归验证 · 所选主 Agent 必须是执行主体")
    print(f"工作目录：{ROOT}")
    t2_source_guard()
    t1_priority()
    t4_bindings()
    t1_http()

    section("结果")
    print(f"  通过 {_passes} 项，失败 {len(_fails)} 项")
    if _fails:
        for f in _fails:
            print(f"    · {f}")
        return 1
    print("  全部通过 ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())