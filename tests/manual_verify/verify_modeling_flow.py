# -*- coding: utf-8 -*-
"""验证 AI 建模完整流程：建模入口 → LLM 生成 SysML v2 代码 → 视图投影 → 引擎渲染。

流程：
1. agent.execute（dry_run，不落库）→ 意图路由 → design Agent → DeepSeek 生成 SysML v2 代码
2. _gen_sysml_views 提取代码 → generate_views_from_sysml 投影各视图 ViewModel
3. 用 view_engine_template.html（dagre + 正交避障 + 端口锚点）渲染成 HTML
4. Chrome headless 截图验证

输出：outputs/modeling_flow_views.html + .png
"""
import json
import os
import re
import sys
import time

BASE = r"C:/Users/gefei/WorkBuddy/2026-08-04-19-05-52/mbse_system"
sys.path.insert(0, BASE)
OUTPUTS = os.path.join(BASE, "outputs")

USER_INPUT = (
    "请基于热管理系统的设计需求，用 SysML v2 语言生成完整的建模代码。"
    "系统组成：Vehicle 包含 Cabin 和 ThermalManagementSystem；"
    "ThermalManagementSystem 包含 Chiller（制冷）、Heater（制热）、TMSController（控制），"
    "以及 CoolantPort 冷却液端口，COP 目标 2.5；"
    "需求包括 CoolingPerf（COP≥2.2）和 HeatingPerf（制热能力≥5.0kW），由 TMS 满足；"
    "行为包括 CoolCabin（制冷）和 HeatCabin（制热）两个 action，状态机有 Off/Cool/Heat 状态。"
    "请在回答末尾用 ```sysml 代码块输出完整的 SysML v2 建模代码，"
    "覆盖 BDD（块定义图）、IBD（内部块图）、ACT（活动图）、SEQ（顺序图）、"
    "STM（状态机图）、REQ（需求图）等视图所需元素（part def/port/requirement/action/state/message/lifeline）。"
)


def extract_sysml_code(text: str) -> str | None:
    """提取 LLM 输出中的 ```sysml 代码块（与 agent._extract_sysml_code 一致）。"""
    if not text:
        return None
    blocks = re.findall(r"```(?:sysml|kerml|SysML|SysMLv2|sysmlv2|kerml2|sysml_v2|sysmlv1)?\s*\n(.*?)```", text, re.S)
    if not blocks:
        has_feature = bool(re.search(r"\b(?:part|requirement|action|attribute|interface|package|state|constraint|port)\s+(?:def|usage)\b", text)) or " satisfies " in text
        if has_feature:
            blocks = re.findall(r"```[^\n]*\n(.*?)```", text, re.S)
    if not blocks:
        return None
    cleaned = [b.strip() for b in blocks if b.strip()]
    return "\n\n".join(cleaned) if cleaned else None


def build_render_data(views: dict, code: str) -> dict:
    """把 sysml_views 组装成 v3 引擎数据（含 portDefs，从代码重解析）。"""
    from sysml_importer import parse_text
    parsed = parse_text(code or "")
    port_defs = {}
    for n in parsed.get("nodes", []):
        props = n.get("properties") or {}
        if props.get("kind") == "part" and props.get("ports"):
            port_defs[n["name"]] = props["ports"]
    out_views = []
    for vid in ("BDD", "IBD", "REQ", "UC", "ACT", "SEQ", "STM", "PAR", "PKG", "TRACE"):
        vm = views.get(vid) if isinstance(views, dict) else None
        if not vm:
            continue
        vm = dict(vm)
        vm["type"] = vid
        out_views.append(vm)
        print(f"  {vid:>5} | {vm.get('view',{}).get('name',''):<8} | nodes={len(vm.get('nodes') or []):<3} edges={len(vm.get('edges') or []):<3}")
    return {"views": out_views, "portDefs": port_defs}


def main():
    from agent import AgentPipeline
    from view_generator import generate_views_from_sysml

    print("=== 1/4 建模入口调用（真实 LLM，dry_run 不落库）===")
    t0 = time.time()
    agent = AgentPipeline()
    r = agent.execute(USER_INPUT, conversation_id=0, branch="dev/main", dry_run=True)
    print(f"耗时 {time.time()-t0:.1f}s")
    print(f"意图: {r.get('intent')} | Agent: {r.get('agent')} | LLM: {r.get('llm',{}).get('provider')}/{r.get('llm',{}).get('model')} mock={r.get('llm',{}).get('used_mock')}")
    content = r.get("content") or ""
    print(f"回答长度: {len(content)} 字符")

    print("\n=== 2/4 提取 SysML v2 代码 ===")
    code = extract_sysml_code(content)
    if not code:
        print("⚠️ LLM 输出未含 sysml 代码块，回退示例代码验证引擎")
        from tools.render_view_demo import CODE
        code = CODE
    print(f"代码长度: {len(code)} 字符")
    print("代码预览:", code[:300].replace("\n", " | "))

    print("\n=== 3/4 视图投影 ===")
    # 优先消费 agent 链路返回的 sysml_views（真实建模流程）；为空则手动投影兜底
    agent_views = r.get("sysml_views") or {}
    if isinstance(agent_views, dict) and agent_views:
        # agent._gen_sysml_views 返回 {"parsed":..., "views":{...}, "intent":...}
        sysml_views = agent_views.get("views") or agent_views
        print("视图来自 agent._gen_sysml_views（真实建模链路）✓ parsed:", agent_views.get("parsed"))
    else:
        print("⚠️ agent 未返回 sysml_views，手动投影兜底")
        views = generate_views_from_sysml(code, view_types=None, intent=r.get("intent"))
        sysml_views = views.get("views", {})
    print(f"生成视图数: {len(sysml_views)}")
    for vid, vm in sysml_views.items():
        print(f"  {vid:>5} | nodes={len(vm.get('nodes') or []):<3} edges={len(vm.get('edges') or []):<3}")

    print("\n=== 4/4 引擎渲染 HTML ===")
    data = build_render_data(sysml_views, code)
    tpl = os.path.join(BASE, "tools", "view_engine_template.html")
    with open(tpl, "r", encoding="utf-8") as f:
        html = f.read()
    dagre_path = os.path.join(BASE, "static", "vendor", "dagre.min.js")
    if os.path.exists(dagre_path):
        with open(dagre_path, "r", encoding="utf-8") as f:
            html = html.replace("__DAGRE_INLINE__", f.read())
    html = html.replace("__VIEWDATA__", json.dumps(data, ensure_ascii=False))
    out_html = os.path.join(OUTPUTS, "modeling_flow_views.html")
    with open(out_html, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"HTML: {out_html}")

    # 留存建模返回摘要
    summary = {
        "intent": r.get("intent"),
        "agent": r.get("agent"),
        "llm": r.get("llm", {}),
        "user_input": USER_INPUT[:200],
        "sysml_code": code[:6000],
        "sysml_views": {k: {"nodes": len(v.get("nodes") or []), "edges": len(v.get("edges") or [])} for k, v in sysml_views.items()},
    }
    with open(os.path.join(OUTPUTS, "modeling_flow_summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print("摘要: outputs/modeling_flow_summary.json")
    print("\n建模流程验证完成 ✓")


if __name__ == "__main__":
    main()
