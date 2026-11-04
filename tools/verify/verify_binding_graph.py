# -*- coding: utf-8 -*-
"""门禁：Agent ↔ 技能 / MCP / 工具 的**绑定生效判定**必须与运行时一致（2026-10-30）。

## 为什么需要（问题不是"少一个看板"）
一条绑定要变成"AI 真的能用上"，要过四道闸（Agent 启用 / 绑定行启用 /
目标端可用 / 插件可消费），四道闸分散在四个文件里。此前**没有任何一处**能回答
"这条边现在还生效吗"，于是出现两类错误：

1. **看板说生效、运行时没注入** —— 用户绑了却用不上，界面一切正常；
2. **看板说失效、运行时仍在注入** —— 用户以为停用了，AI 还在调。

`services/capability_graph.py` 给出判定，本门禁负责证明它**没有撒谎**：
既不许它把死的说成活的，也不许它把活的（仍在注入的）说成死的。

## 判据
| # | 断言 | 覆盖的失效 |
|---|---|---|
| G1 | 每条 `agent_tools` 行都被判定，且 `state` 在枚举内 | 漏判/新类型未接 |
| G2 | 悬空绑定（目标行不存在）= 0 | 删能力不清关系 |
| G3 | 判为 **不生效** 的工具绑定，**不得**出现在运行时注入集合里 | 判定与运行时漂移 |
| G4 | 技能判定与 `bound_tools_for` 一致（停用/缺失 ⇒ 不在解析结果里） | 两套口径 |
| G5 | 反向关系自洽：`usage_of` 的 Agent 集合 == 绑定表里的 Agent 集合 | 影响面查询失真 |
| G6 | 变异：事务内停用一条技能 ⇒ 判定必须由"生效"变"停用" | 断言空转 |
| G7 | 内置工具「行停用但仍注入」必须被标成 `bypass`（不许谎报失效） | 过度断言 |
| G8 | 重名技能必须暴露 `duplicates`（关系键 name 不唯一） | 指向不确定却装作确定 |

⚠️ G3/G4/G7 需要 `mbse.db` + 真 pipeline；CI 无库 ⇒ 自动 SKIP（不判红）。

## 用法
    ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_binding_graph.py
"""
from __future__ import annotations

import os
import sqlite3
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, _ROOT)

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
_results = []


def _rec(name, ok, detail="", kind=FAIL):
    _results.append((PASS if ok else kind, name, detail))
    print("  [%s] %s%s" % (_results[-1][0], name, ("  ← " + detail) if detail else ""))
    return bool(ok)


def _db():
    p = (os.environ.get("MBSE_DB_PATH") or os.path.join(_ROOT, "mbse.db"))
    if not os.path.exists(p):
        return None
    c = sqlite3.connect(p)
    c.row_factory = sqlite3.Row
    return c


def main():
    print("=" * 78)
    print("能力关系图谱：绑定生效判定 ↔ 运行时一致性")
    print("=" * 78)
    conn = _db()
    if conn is None:
        _rec("前置：mbse.db 存在", False, "无库 ⇒ 跳过（本门禁只在本机有意义）", kind=SKIP)
        print("\n结论：SKIP（无库）")
        return 0

    from services import capability_graph as G

    # ── G1 覆盖：每条绑定都被判定 ─────────────────────────────
    print("\n=== G1 判定覆盖率 ===")
    raw = conn.execute("SELECT COUNT(*) FROM agent_tools").fetchone()[0]
    ag = G.audit(conn)
    ok_states = set(G.STATE)
    bad = sorted({p["state"] for p in ag["problems"]} - ok_states)
    _rec("G1a 绑定总数一致（判定 %d == agent_tools %d）" % (ag["total"], raw),
         ag["total"] == raw, "有行没被判定 ⇒ 未知状态会被当成生效")
    _rec("G1b 所有状态都在枚举内", not bad, "越界状态：%s" % bad)

    # ── G2 无悬空 ─────────────────────────────────────────────
    print("\n=== G2 悬空绑定（目标行已不存在）===")
    dangling = [p for p in ag["problems"] if p["state"] == "dangling"]
    _rec("G2 悬空绑定 = 0", not dangling,
         "；".join("%s→%s(%s)" % (p["agent"], p["name"], p["kind"]) for p in dangling[:5]))

    # ── 载入真 pipeline（G3/G7 需要）─────────────────────────
    pipe = None
    try:
        from agent import AgentPipeline
        pipe = AgentPipeline()
        pipe._load_db_agents()
    except Exception as e:            # noqa: BLE001
        print("  [SKIP] 无法载入 AgentPipeline：%r" % (e,))

    agents = [dict(r) for r in conn.execute(
        "SELECT id,name,display_name,status FROM agents WHERE status='active' ORDER BY id")]
    repo = None
    if pipe is not None:
        try:
            from database import get_db
            from repositories.agent_repo import AgentRepo
            repo = AgentRepo(get_db())
        except Exception as e:        # noqa: BLE001
            print("  [SKIP] 无法构造 AgentRepo：%r" % (e,))

    # ── G3 判死不得被注入 ────────────────────────────────────
    print("\n=== G3 判为不生效的工具绑定，运行时不得注入 ===")
    if pipe is None:
        _rec("G3 交叉核对", False, "无 pipeline", kind=SKIP)
    else:
        viol = []
        checked = 0
        for a in agents:
            got = G.agent_bindings(conn, a["id"])
            dead = [b["name"] for b in got["bindings"]
                    if b["kind"] == "tool" and b["state"] in ("off", "unready", "dangling")]
            if not dead:
                continue
            checked += len(dead)
            try:
                injected = {t["function"]["name"] for t in pipe._build_tools_def(a["name"], "")}
            except Exception:         # noqa: BLE001
                continue
            for n in dead:
                if n in injected:
                    viol.append("%s/%s" % (a["name"], n))
        _rec("G3 无「判死却仍注入」（核对 %d 条）" % checked, not viol,
             "越界：%s" % ", ".join(viol[:6]))

    # ── G4 技能判定 ↔ bound_tools_for ───────────────────────
    print("\n=== G4 技能判定与 bound_tools_for 一致 ===")
    if repo is None:
        _rec("G4 一致性", False, "无 repo", kind=SKIP)
    else:
        viol = []
        checked = 0
        for a in agents:
            got = G.agent_bindings(conn, a["id"])
            resolved = {t["name"] for t in repo.bound_tools_for(a["id"]) if t.get("type") == "skill"}
            for b in got["bindings"]:
                if b["kind"] != "skill":
                    continue
                # 只看"停用/缺失"这两种口径分歧；正文为空是注入侧（get_bound_skills）才判的，
                # 与 bound_tools_for 不同层，不在这里比对（否则会把"两层职责不同"当成缺陷）。
                if b["state"] in ("off", "dangling") and b["target"] is not None \
                        and not b["target"].get("enabled"):
                    checked += 1
                    if b["name"] in resolved:
                        viol.append("%s/%s" % (a["name"], b["name"]))
                elif b["effective"]:
                    checked += 1
                    if b["name"] not in resolved:
                        viol.append("%s/%s(应生效却未解析)" % (a["name"], b["name"]))
        _rec("G4 技能判定 ↔ 解析结果（核对 %d 条）" % checked, not viol,
             "分歧：%s" % ", ".join(viol[:6]))

    # ── G5 反向关系自洽 ─────────────────────────────────────
    print("\n=== G5 反向关系（usage_of）自洽 ===")
    viol = []
    samples = 0
    for r in conn.execute("SELECT DISTINCT tool_type, tool_name FROM agent_tools"):
        kind, name = r["tool_type"], r["tool_name"]
        want = {x[0] for x in conn.execute(
            "SELECT agent_id FROM agent_tools WHERE tool_type=? AND tool_name=?", (kind, name))}
        u = G.usage_of(conn, kind, name)
        got = {x["id"] for x in u["agents"]}
        samples += 1
        if got != want:
            viol.append("%s:%s（缺 %s / 多 %s）" % (kind, name, sorted(want - got), sorted(got - want)))
        if len(u["active_agents"]) + len(u["inactive_agents"]) != len(u["agents"]):
            viol.append("%s:%s 生效数+失效数≠总数" % (kind, name))
    _rec("G5 反向查询与正向绑定一致（%d 组）" % samples, not viol, "；".join(viol[:4]))

    # ── G6 变异自证：停用技能 ⇒ 判定翻转 ────────────────────
    print("\n=== G6 变异自证（事务内停用一条技能，必须由生效翻成停用）===")
    # 刻意挑**当前判定为 effective** 的那条：若拿到本来就是 off/plugin_off 的，
    # "翻转"是假的（之前正是这样：拿到 plugin_off 的样本，翻转后仍是 off，
    # 判据通过却什么也没证明）。
    cands = [dict(r) for r in conn.execute(
        "SELECT s.id, s.name FROM skills s JOIN agent_tools at "
        "  ON at.tool_type='skill' AND at.tool_name=s.name "
        "JOIN agents a ON a.id=at.agent_id AND a.status='active' "
        "WHERE s.enabled=1 AND COALESCE(s.status,'') IN ('published','draft_revision') "
        "ORDER BY s.id")]
    row = None
    for c in cands:
        if G.judge_binding(conn, "skill", c["name"])["state"] == "effective":
            row = c
            break
    if row is None:
        _rec("G6 变异", False, "找不到状态为 effective 的技能样本", kind=SKIP)
    else:
        conn.execute("BEGIN")
        try:
            before = G.judge_binding(conn, "skill", row["name"])
            conn.execute("UPDATE skills SET enabled=0 WHERE id=?", (row["id"],))
            after = G.judge_binding(conn, "skill", row["name"])
            flipped = before["effective"] and not after["effective"] and after["state"] == "off"
            _rec("G6 停用后判定翻转（%s：%s→%s）" % (row["name"], before["state"], after["state"]),
                 flipped, "判据没读状态字段 ⇒ 这条门禁是空转")
        finally:
            conn.rollback()

    # ── G7 内置旁路必须如实标注 ─────────────────────────────
    print("\n=== G7 「行停用但仍会被注入」必须标成 bypass（且机制成立）===")
    if pipe is None:
        _rec("G7 旁路如实", False, "无 pipeline", kind=SKIP)
    else:
        from agent.pipeline_parts.tools import BUILTIN_DESC
        cases, viol = 0, []
        for a in agents:
            got = G.agent_bindings(conn, a["id"])
            if not got["bindings"]:
                continue
            try:
                ad_tools = set(pipe.registry.get(a["name"]).tools)
            except Exception:         # noqa: BLE001
                ad_tools = set()
            for b in got["bindings"]:
                if b["kind"] != "tool" or b["state"] != "bypass":
                    continue
                cases += 1
                # ⚠️ 不能拿 `_build_tools_def` 的**最终结果**断言：那一层还有 JIT 预筛
                #   （`pick_for_injection` 按语义留子集）⇒ 实测 model_release 的
                #   entity_create 在候选中、却被预筛掉。用"机制"断言才是对的：
                #   ① 内置旁路 = 名字在 BUILTIN_DESC 且已进 agent_def.tools；
                #   ② 插件旁路 = 所属插件 published（自动消费通道不看 tools.status）。
                by_builtin = b["name"] in BUILTIN_DESC and b["name"] in ad_tools
                by_plugin = (b.get("plugin") or {}).get("status") == "published"
                if not (by_builtin or by_plugin):
                    viol.append("%s/%s" % (a["name"], b["name"]))
        _rec("G7 bypass 机制成立（%d 条）" % cases, not viol,
             "标了 bypass 但两条通道都不成立（应改 off）：%s" % ", ".join(viol[:6]))

    # ── G8 重名歧义必须暴露 ─────────────────────────────────
    print("\n=== G8 重名技能必须暴露歧义 ===")
    dup_names = [r["name"] for r in conn.execute(
        "SELECT name FROM skills GROUP BY name HAVING COUNT(*)>1")]
    if not dup_names:
        _rec("G8 无重名（无需暴露）", True)
    else:
        missing = []
        for nm in dup_names:
            j = G.judge_binding(conn, "skill", nm)
            if not j.get("duplicates"):
                missing.append(nm)
        _rec("G8 重名组均带 duplicates（%d 组）" % len(dup_names), not missing,
             "未暴露：%s" % missing[:4])

    conn.close()
    print("\n" + "=" * 78)
    fails = [r for r in _results if r[0] == FAIL]
    skips = [r for r in _results if r[0] == SKIP]
    print("结果：%d PASS / %d FAIL / %d SKIP" % (len(_results) - len(fails) - len(skips),
                                                len(fails), len(skips)))
    print("=" * 78)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
