# -*- coding: utf-8 -*-
"""规划 vs 落地 链路闭环核查 —— 回答「是否都替换完成、绑定与数据链路是否闭环」。

**这个脚本自己就是判据**：不信任任何文档/记忆里的"已完成"声明，
一律从 `mbse.db` 读真实状态再判定。

五组判据
--------
① **落地率**：规划里的 8 节点 / 15 skill / 8 工具，各自落库几个（附缺失清单）
② **绑定完整性**：每个 Agent 该有的工具绑定是否齐（尤其 N2/N3/N4 的 validate+autofix）
③ **引用有效性**：所有 skill 的 `allowed_tools` 引用的工具**是否真实存在**
   （引用不存在的工具 = 该 skill 的约束是空的，属静默失效）
④ **数据链路闭环**：M0→N6 的**产物传递**是否有承载物
   —— 判据：节点产物流转是否只能靠自由文本（`agents.output_schema` 是否已填）
⑤ **可运行性**：路由池 / 团队名册 / 工具路由三个入口是否都认这些新资产

用法
----
    ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_pipeline_closure.py
退出码 0 = 闭环无缺口；非 0 = 有缺口（逐条列出）。
"""
import json
import os
import sqlite3
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
DB = os.path.join(_ROOT, "mbse.db")

# ── 规划（来自 data/modeling_nodes.json v3.1.0，不手写避免漂移）────
REG = os.path.join(_ROOT, "data", "modeling_nodes.json")


def load_plan():
    with open(REG, encoding="utf-8") as f:
        return json.load(f)


def jload(v, default):
    try:
        return json.loads(v) if v else default
    except Exception:
        return default


def main() -> int:
    plan = load_plan()
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    q = lambda s, *a: conn.execute(s, a).fetchall()
    gaps = []

    print("=" * 76)
    print("规划 vs 落地 链路闭环核查（一切以数据库实测为准）")
    print("=" * 76)

    # ── ① 落地率 ────────────────────────────────────────────────
    print("\n── ① 节点落地率 ──")
    plan_nodes = {n["key"]: n for n in plan["nodes"]}
    live = {r["name"]: r for r in q(
        "SELECT name, display_name, id, stage_order, task_domain, status, "
        "input_schema, output_schema FROM agents WHERE status='active'")}
    miss_nodes = []
    for key, n in sorted(plan_nodes.items(), key=lambda x: x[1]["stage_order"]):
        nm = n["agent"]["name"]
        if nm in live:
            print(f"  [OK  ] {key:3} {nm:28} stage={live[nm]['stage_order']} "
                  f"domain={live[nm]['task_domain']}")
        else:
            miss_nodes.append(nm)
            print(f"  [MISS] {key:3} {nm:28} 规划有、库里无")
    if miss_nodes:
        gaps.append(f"节点未落地 {len(miss_nodes)} 个: {miss_nodes}")

    print("\n── ①b Skill 落地率 ──")
    skills = {r["name"]: r for r in q(
        "SELECT id, name, status, enabled, allowed_tools, allowed_roles, "
        "dependencies, skill_type, category FROM skills")}
    miss_sk = []
    for s in plan["skills"]:
        nm = s["name"]
        row = skills.get(nm)
        if not row:
            miss_sk.append(nm)
            print(f"  [MISS] {nm}")
        elif row["status"] != "published" or not row["enabled"]:
            print(f"  [OFF ] {nm:44} status={row['status']} enabled={row['enabled']}")
            miss_sk.append(nm + "(未发布)")
        else:
            at = jload(row["allowed_tools"], [])
            ar = jload(row["allowed_roles"], [])
            dep = jload(row["dependencies"], [])
            print(f"  [OK  ] {nm:44} tools={len(at)} roles={len(ar)} deps={len(dep)}")
    if miss_sk:
        gaps.append(f"skill 未落地/未发布 {len(miss_sk)} 个")

    print("\n── ①c 工具落地率 ──")
    tools = {r["name"]: r for r in q(
        "SELECT name, side_effect, risk_level, status FROM tools")}
    miss_tools = []
    for t in plan["tools"]:
        nm = t["name"]
        if nm in tools:
            print(f"  [OK  ] {nm:28} {tools[nm]['side_effect']:6} {tools[nm]['risk_level']}")
        else:
            miss_tools.append(nm)
            print(f"  [MISS] {nm:28} 规划有、库里无")
    if miss_tools:
        gaps.append(f"工具未落地 {len(miss_tools)} 个: {miss_tools}")

    # ── ② 绑定完整性 ────────────────────────────────────────────
    print("\n── ② 关键节点绑定完整性（对照注册表的 bind_tools）──")
    for key, n in sorted(plan_nodes.items(), key=lambda x: x[1]["stage_order"]):
        nm = n["agent"]["name"]
        if nm not in live:
            continue
        want = n["agent"].get("bind_tools") or []
        got = [r["tool_name"] for r in q(
            "SELECT t.tool_name FROM agent_tools t WHERE t.agent_id=? AND t.tool_type='tool'",
            live[nm]["id"])]
        lack = [w for w in want if w not in got]
        extra = [g for g in got if g not in want]
        mark = "OK  " if not lack else "MISS"
        if lack:
            gaps.append(f"{nm} 缺绑定 {lack}")
        print(f"  [{mark}] {key:3} {nm:28} 期望{len(want)} 实际{len(got)}"
              f"{'  缺:' + str(lack) if lack else ''}"
              f"{'  多:' + str(extra) if extra else ''}")

    # ── ③ 引用有效性（静默失效检测）────────────────────────────
    print("\n── ③ Skill 的 allowed_tools 引用有效性（引用不存在=约束空转）──")
    bad_ref = 0
    for nm, row in skills.items():
        for t in jload(row["allowed_tools"], []):
            if t not in tools:
                print(f"  [FAIL] skill「{nm}」引用了不存在的工具: {t}")
                bad_ref += 1
    if bad_ref:
        gaps.append(f"{bad_ref} 处 skill 工具引用失效")
    else:
        print("  [OK  ] 全部 skill 的 allowed_tools 引用均有效")

    # ── ④ 数据链路闭环 ─────────────────────────────────────────
    print("\n── ④ 数据链路闭环：节点契约是否可承载产物传递 ──")
    empty_out, empty_in = [], []
    for key, n in sorted(plan_nodes.items(), key=lambda x: x[1]["stage_order"]):
        nm = n["agent"]["name"]
        if nm not in live:
            continue
        io = live[nm]
        want_out = n["agent"].get("output_schema") or {}
        got_out = jload(io["output_schema"], {})
        want_in = n["agent"].get("input_schema") or {}
        got_in = jload(io["input_schema"], {})
        ok_out = bool(want_out) and bool(got_out)
        ok_in = bool(want_in) and bool(got_in)
        if not ok_out:
            empty_out.append(nm)
        if not ok_in:
            empty_in.append(nm)
        print(f"  [{'OK  ' if ok_out else 'MISS'}] {key:3} {nm:28} "
              f"input={'有' if ok_in else '空'} output={'有' if ok_out else '空'}")
    if empty_out:
        gaps.append(f"output_schema 为空（产物无契约，链路不闭环）: {empty_out}")
    if empty_in:
        gaps.append(f"input_schema 为空（入口无契约）: {empty_in}")

    # ── ⑤ 可运行性：三入口 ─────────────────────────────────────
    print("\n── ⑤ 可运行性：三个入口是否认新资产 ──")
    # 5.1 路由池：active Agent 能否被 _load_db_agents 装载
    n_active = q("SELECT COUNT(*) c FROM agents WHERE status='active'")[0]["c"]
    print(f"  路由池：active Agent {n_active} 个")
    # 5.2 团队名册一致性
    main = q("SELECT id, system_prompt FROM agents WHERE name='MBSE建模总体负责人'")
    if main:
        import re
        sp = main[0]["system_prompt"] or ""
        roster = re.findall(r"^\|\s*([^|]+?)\s*\|[^|]+\|[^|]+\|$", sp, re.M)
        # 排除表头/分隔行/说明行 —— 否则 `---` 会被当成成员（实测踩过）
        roster = [x.strip() for x in roster
                  if x.strip() and not x.startswith("成员")
                  and "人工确认" not in x and set(x.strip()) != {"-"}]
        mem = q("""SELECT a.display_name FROM agent_team_members tm
                  JOIN agents a ON a.id=tm.sub_agent_id
                  WHERE tm.main_agent_id=? AND tm.enabled=1 AND a.status='active'""",
                main[0]["id"])
        mem_d = {m["display_name"] for m in mem}
        in_roster_not_team = [x for x in roster if x not in mem_d]
        in_team_not_roster = [x for x in mem_d if x not in roster]
        print(f"  团队名册：名册 {len(roster)} / 团队 {len(mem_d)}")
        if in_roster_not_team:
            print(f"    [FAIL] 名册有但不在团队: {in_roster_not_team}")
            gaps.append(f"名册有但不在团队: {in_roster_not_team}")
        if in_team_not_roster:
            print(f"    [WARN] 团队有但不在名册（旁路能力，可接受）: {in_team_not_roster}")
        if not in_roster_not_team:
            print("    [OK  ] 名册全部在团队内")
    # 5.3 工具路由：sysml_v2_ 前缀能否分发到 autofix
    try:
        sys.path.insert(0, _ROOT)
        import sysml_check_tools as ct
        r = ct.exec_tool("sysml_v2_autofix", {"code": "package P{ import X::*; part def V; }"})
        print(f"  工具路由：sysml_v2_autofix 可达={r.get('ok')} changed={r.get('changed')}")
        if not r.get("ok"):
            gaps.append("sysml_v2_autofix 路由不可达")
    except Exception as e:
        print(f"  工具路由：[FAIL] {type(e).__name__}: {e}")
        gaps.append(f"工具路由异常 {e}")

    # ── 汇总 ──────────────────────────────────────────────────
    print("\n" + "=" * 76)
    if gaps:
        print(f"❌ 链路存在 {len(gaps)} 类缺口：")
        for i, g in enumerate(gaps, 1):
            print(f"   {i}. {g}")
    else:
        print("✅ 链路闭环无缺口")
    print("=" * 76)
    conn.close()
    return 0 if not gaps else 1


if __name__ == "__main__":
    sys.exit(main())
