"""migrate_v12_bind_view_skills —把 8 个视图 skill **绑定**到 N3 `view_expansion` Agent。

## 为什么要绑（2026-10-08 实测根因，不是"加限制"）

N3 的`view_expansion` Agent 绑了 **6 个工具、0 个 skill** ⇒ 8 个视图 skill
只能进 `_global_skill_pool` 靠 `_semantic_skill_match`（bigram）**猜**。实测后果：

    requirement  总命中=0 视图skill=[]          ← 完全没命中，模型拿不到任何视图正文
    structure    总命中=0 视图skill=[]          ← 同上
    usecase/activity/ibd/sequence/state/parameter 命中 1 个（靠运气，且顺序不稳）

bigram 匹配还依赖 embedding，本机`httpx` 缺失 ⇒ `embedding API 调用失败，降级 bigram`
（实测每次调用都打这条）⇒ 匹配质量进一步不稳。

⇒ 模型在生成 requirement/structure 视图时**手里没有任何视图专属正文**，
只能凭通用知识写 ⇒ 这正是内容层判据里 parameter/sequence 产出 state 内容的根因。

## 为什么"绑定"是减少限制而不是增加限制

`view_type` 是 `input_schema` 里已声明的**枚举入参**（8 值），是确定性的事实。
把它接进 skill 绑定，等于把"语义猜测"换成"按枚举取对应正文"：

- **不加**任何新的门禁/校验/提示词约束；
- 只是让**已经写好的 8 份正文**在需要时确定性地到达模型；
- `get_bound_skills` 已有的机制：`bound` 命中失败时只给一行描述，不命中不注入正文
  ⇒ 未命中的 7 个视图不会污染上下文。

## 幂等 / 回滚

- `INSERT OR IGNORE`（`AgentRepo.add_tool`）⇒ 重复跑不报错、不重复插；
- 回滚：`DELETE FROM agent_tools WHERE agent_id=? AND tool_type='skill'
  AND tool_name LIKE 'sysml_view_generation_%'`。

⚠️ 只改 `agent_tools` 一张表（绑定关系），**不动 skills / plugins 内容**
⇒ 无需走 `legacy_sync` 同步桥（那是内容/启停状态变更才需要的）。
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

AGENT = "view_expansion"
#: 与 agents.input_schema.properties.view_type.enum 严格一致（8 值，顺序无关）
VIEW_SKILLS = [
    "sysml_view_generation_requirement",
    "sysml_view_generation_structure",
    "sysml_view_generation_usecase",
    "sysml_view_generation_activity",
    "sysml_view_generation_ibd",
    "sysml_view_generation_sequence",
    "sysml_view_generation_state",
    "sysml_view_generation_parameter",
]


def main() -> int:
    from database import db_conn
    from repositories.agent_repo import AgentRepo

    with db_conn() as conn:
        repo = AgentRepo(conn)
        row = repo.one("SELECT id, name FROM agents WHERE name=?", (AGENT,))
        if not row:
            print(f"[FAIL] Agent 不存在：{AGENT}")
            return 1
        aid = row["id"]
        print(f"Agent {AGENT} (id={aid})")

        # ── 前置校验：skill 必须真实存在且已发布，否则绑定成"指向空气"──
        missing = []
        for nm in VIEW_SKILLS:
            s = repo.one("SELECT id, status, enabled, length(content) ln "
                         "FROM skills WHERE name=?", (nm,))
            if not s:
                missing.append(f"{nm}(不存在)")
            elif s["status"] != "published" or not s["enabled"]:
                missing.append(f"{nm}(status={s['status']},enabled={s['enabled']})")
            elif not (s["ln"] or 0):
                missing.append(f"{nm}(正文为空)")
        if missing:
            print("[FAIL] 以下 skill 不可绑定，绑定后会指向空气：")
            for m in missing:
                print("   -", m)
            return 1
        print(f"前置校验通过：{len(VIEW_SKILLS)} 个 skill 均 published+enabled+有正文")

        # ── 绑定 ──
        # ⚠️ `add_tool` 返回的是 `BaseRepo.execute` 的**累计影响行数**（`SELECT changes()`），
        #    **不是本次插入行数** —— 首次跑出「新增 68468 条」这种数就是被它骗了
        #    （实测 agent_tools 实际只有 110 行）。所以这里不拿它的返回值当计数，
        #    改为绑定后按实际行数核对。
        for nm in VIEW_SKILLS:
            repo.add_tool(aid, "skill", nm)

        cur = repo.rows("SELECT id, tool_type, tool_name FROM agent_tools WHERE agent_id=?",
                        (aid,))
        sk = sorted(r["tool_name"] for r in cur if r["tool_type"] == "skill")
        tl = sorted(r["tool_name"] for r in cur if r["tool_type"] == "tool")
        print(f"绑定后：skill {len(sk)} 个 / tool {len(tl)} 个")
        for x in sk:
            print("   skill:", x)

        # ── 出口断言：8 个视图 skill 必须全部在列 ──
        assert set(VIEW_SKILLS) <= set(sk), "绑定后缺少视图 skill"
        assert len(tl) == 6, f"工具绑定被意外改动：{tl}"
        # 幂等/无重复：同名绑定不得重复插
        dup = repo.rows("SELECT tool_name, count(*) n FROM agent_tools "
                        "WHERE agent_id=? AND tool_type='skill' GROUP BY tool_name HAVING n>1",
                        (aid,))
        assert not dup, f"出现重复绑定：{[d['tool_name'] for d in dup]}"

        # ── 读回验证：registry 能否真的取到（绑定 ≠ 生效，必须走一遍消费方）──
    from agent.registry import AgentRegistry
    from database import db_conn as _dbc
    reg = AgentRegistry()
    with _dbc() as c2:
        reg.load_from_db(c2)
    bound = reg.get_bound_skills(AGENT)
    got = {b.get("name") for b in bound}
    print(f"\nregistry.get_bound_skills → {len(bound)} 个")
    assert set(VIEW_SKILLS) <= got, f"registry 读不到全部视图 skill：缺 {set(VIEW_SKILLS) - got}"
    # 内容必须非空：bound_tools_for 只收content 非空的条目
    empty = [b["name"] for b in bound if not (b.get("content") or "").strip()]
    assert not empty, f"这些绑定 skill 正文为空（registry 不会返回）：{empty}"
    print("registry 读回验证通过（含正文非空校验）")
    return 0


if __name__ == "__main__":
    sys.exit(main())