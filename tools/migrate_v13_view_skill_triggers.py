"""migrate_v13_view_skill_triggers — 给 8 个视图 skill 的 `triggers` 补上确定性入口。

## 为什么（2026-10-08 实测根因）

`_build_skill_prompt` 的第一级匹配是 **triggers 关键词子串命中**（`skills.py:140`），
第二级才是 bigram 语义。而实测这批 skill 的 triggers 状况：

    activity   triggers = []                ← 空！完全没有确定性入口
    requirement ['需求视图','需求层次','利益相关方','requirement def']
    structure  ['结构视图','块定义图','BDD','部件定义','层级组成']
    ...
    6/8 个**不含自己的英文枚举值**（parameter / sequence 含）

而 N3 的调用形态是 `请生成 {view_type} 视图`，`view_type` 就是
`requirement|structure|usecase|activity|ibd|sequence|state|parameter` 这个**英文枚举**：

- `requirement` 命中的是 `'requirement def'`？**否**（子串 "requirement" ≠ "requirement def" 的反向包含）
  ⇒ 第一级不命中，只能落到 bigram；
- 本机 `httpx` 缺失 ⇒ embedding 全程降级 bigram（日志里每轮都打
  「embedding API 调用失败，降级 bigram」）⇒ 第二级也不稳；
- 实测绑定前 requirement/structure **直接 0 命中**，模型手里没有任何视图正文。

## 为什么"补触发词"是减少限制而不是增加限制

不新增任何门禁/校验/提示词约束，只是把**已经存在的确定性事实**
（`view_type` 枚举值 = skill 名后缀）登记进已有的 triggers 字段：

- triggers 只是**路由索引**，命中后注入的正文一个字都没变；
- 不命中也只是不注入（既有行为），不会把无关视图塞进上下文；
- 相比"再加一层 view_type 强制校验"，这是零新增机制的做法。

##幂等 / 回滚

- 按 `name` 精确匹配、逐个并集去重⇒ 重复跑不产生重复项；
- 回滚：`UPDATE skills SET triggers=<原值> WHERE name LIKE 'sysml_view_generation_%'`。
  （⚠️ 本脚本跑前会把原值打印出来，照抄即可回滚。）

## 只改 skills.triggers

`triggers` 不参与插件可消费判定（`consumable_filter`只看 status/enabled/安装态）
⇒ 无需走 `legacy_sync` 同步桥。
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PREFIX = "sysml_view_generation_"

#: view_type 枚举 → 补充的确定性触发词（英文枚举值 + 中文常用说法 + 已有中文别名）
#:
#: ★ 口径（2026-10-08 实测踩坑后确立）：**英文缩写一律不进 triggers**。
#: `_build_skill_prompt` 的第一级匹配是**无边界子串命中**（`t in low`），而：
#:     'uc' in '请生成 structure 视图'.lower()  →  **True**   （str-uc-ture）
#:     'ibd' / 'act' / 'seq' / 'par' / 'stm' / 'req' 这类3~4字母缩写同理危险
#:                （如 'act' in 'transaction'、'par' in 'part'）
#: ⇒ 第一版给 usecase 加了 'UC'，实测直接让 structure 误命中 usecase 正文。
#: **缩写只在 prompt 的枚举清单里对模型可见，不做机器路由索引。**
EXTRA_TRIGGERS = {
    "requirement": ["requirement", "需求视图", "需求图", "requirement def"],
    "structure":   ["structure", "结构视图", "块定义图", "BDD", "部件定义"],
    "usecase":     ["usecase", "use case", "用例视图", "用例图"],
    "activity":    ["activity", "活动视图", "活动图", "action"],
    "ibd":         ["ibd", "内部块图", "交互视图"],
    "sequence":    ["sequence", "时序图", "时序视图", "顺序图"],
    "state":       ["state", "状态机", "状态视图", "状态图"],
    "parameter":   ["parameter", "参数视图", "参数图"],
}


#: 第一版误加的短缩写（3~4字母及以下），必须从库里**移除**。
#: 理由见EXTRA_TRIGGERS 上方口径：无边界子串匹配下短缩写必然误命中。
UNSAFE_SHORT = {"uc", "act", "seq", "par", "stm", "req"}


def main() -> int:
    from database import db_conn

    with db_conn() as conn:
        rows = conn.execute(
            "SELECT name, triggers FROM skills WHERE name LIKE ? ORDER BY name",
            (PREFIX + "%",)).fetchall()
        if not rows:
            print("[FAIL] 没找到任何视图 skill")
            return 1

        print("=== 改动前原值（回滚照抄）===")
        for r in rows:
            print(f"  {r['name']:38} {r['triggers']}")

        changed = 0
        for r in rows:
            name = r["name"]
            vt = name[len(PREFIX):]
            extra = EXTRA_TRIGGERS.get(vt)
            if not extra:
                continue                      # guide 之类的非视图 skill 不动
            try:
                cur = json.loads(r["triggers"] or "[]")
                if not isinstance(cur, list):
                    cur = []
            except Exception:
                cur = []
            # ① 先剔除危险短缩写（修第一版留下的坑）
            kept = [x for x in cur if str(x).lower() not in UNSAFE_SHORT]
            # ② 大小写不敏感去重（触发匹配会 lower()，避免 "IBD"/"ibd" 两条并存）
            seen = {str(x).lower() for x in kept}
            new = list(kept)
            for t in extra:
                if str(t).lower() not in seen:
                    new.append(t)
                    seen.add(str(t).lower())
            if new != cur:
                conn.execute("UPDATE skills SET triggers=? WHERE name=?",
                             (json.dumps(new, ensure_ascii=False), name))
                changed += 1
                print(f"  [UPDATE] {name:38} -> {new}")
        conn.commit()
        print(f"\n更新 {changed} 个 skill")

        # ── 出口断言：每个视图 skill 的 triggers 必须含自己的英文枚举值 ──
        bad = []
        for r in conn.execute(
                "SELECT name, triggers FROM skills WHERE name LIKE ?",
                (PREFIX + "%",)).fetchall():
            name = r["name"]
            vt = name[len(PREFIX):]
            if vt not in EXTRA_TRIGGERS:
                continue
            try:
                tr = [str(x).lower() for x in json.loads(r["triggers"] or "[]")]
            except Exception:
                tr = []
            if vt.lower() not in tr:
                bad.append(name)
        assert not bad, f"这些 skill 的 triggers 仍不含自己的 view_type：{bad}"
        print("出口断言通过：8 个视图 skill 的 triggers 均含自己的 view_type 枚举值")

    # ── 真消费方验证：第一级触发匹配必须能命中（不靠 bigram）──
    from agent.registry import AgentRegistry
    from database import db_conn as _dbc
    reg = AgentRegistry()
    with _dbc() as c2:
        reg.load_from_db(c2)
    pool = reg.get_bound_skills("view_expansion")
    print(f"\n=== 第一级（triggers 子串）命中验证，候选池 {len(pool)} 个绑定 skill ===")
    print("   （判据：既能命中自己，也**不能命中别人**——子串匹配最典型的失败模式）")
    ok = 0
    cross = []
    for vt in EXTRA_TRIGGERS:
        low = f"请生成 {vt} 视图".lower()
        hits = []
        for s in pool:
            for t in (s.get("triggers") or []):
                if str(t).lower() and str(t).lower() in low:
                    hits.append(s["name"])
                    break
        want = PREFIX + vt
        others = [h for h in hits if h != want]
        good = (want in hits) and not others
        ok += good
        if others:
            cross.append((vt, others))
        print(f"  [{'OK  ' if good else 'MISS'}] view_type={vt:12} 命中={hits}")
    assert ok == len(EXTRA_TRIGGERS), (
        f"第一级触发不精确：{len(EXTRA_TRIGGERS)-ok} 个不合格；串味={cross}")
    print(f"→ {ok}/{len(EXTRA_TRIGGERS)} 视图由第一级触发**精确**命中"
          f"（命中自己且不误命中他人，无需语义猜测）")
    return 0


if __name__ == "__main__":
    sys.exit(main())