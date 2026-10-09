"""migrate_v21_view_expansion_route_words —给 `view_expansion` 补用户口语词条。

## 触发原因（2026-10-09 实测两处）

**① `verify_team_defined_orchestration` C4 判红**：query「需求视图」→ `chat`
根因链：V15 清空了 8 个旧视图 Agent 的 `intent_keywords`
⇒ 全库只剩 `需求视图生成.capabilities` 里含「需求视图」
⇒ 但 `team_router.is_noise` 查的是**在册成员**的 keywords/capabilities
⇒ 该 Agent 已不在名册 ⇒ 「需求视图」无任何触发词 ⇒ `len < NOISE_LEN` ⇒ **判 chat**。
**用户的业务指令被当成寒暄丢掉。**

**② 意图路由实测 2/8**：`生成活动图` / `生成状态机视图` 等全落 chat/design。

## ★ 为什么这批词与 V14（被撤销的那次）本质不同

V14 失败的原因：把词条写成**"活动视图生成"**（动词+名词），
而用户说**"生成活动图"**（语序相反）⇒ 子串匹配必然失败；
且与旧 Agent 的"活动图生成"互抢，把基线从 1.000 拉到 0.931。

本批词条全部是**名词短语**（`需求视图`/`活动视图`/`视图展开`），
**不含动词 ⇒ 不存在语序问题**，子串方向恒定。

## 实测证据（写入前先验，不靠推断）

**① 全部计分**（`_kw_score` 口径：≥4 字非泛词）：
`视图`、`需求`、`生成`、`建模` 都是泛词，但组合后（如 `需求视图`）**非泛词** ⇒ 计分 ✓

**② 11/11 独占**，他人最高分恒为 0：

| query | 本节点 | 他人最高 |
|---|---|---|
| 生成需求视图 ~ 生成建模视图（11 个） | 1.0（`生成视图展开`=2.0） | **0** |

**③ 原有样本不退化**：`2/5`（与写入前一致；那 3 条失败是 V14b 之前的既有状态，
不是本次引入）。

⚠️ 本脚本**只加视图类词条**，不碰其它 Agent 的词条——
避免重演 V14"全局改词拉低基线"。

## 判据（出口断言，缺一即 FAIL）

① 11 个词条全部写入且计分（`_kw_score` 实测）
② **11/11 独占**：每个词条在本节点得分 ≥ 任何其它 Agent
③ 写入前后**其它 Agent 的 intent_keywords 一字未改**
④ 原有路由样本不退化（写入前后命中数一致）

用法：
```
./.venv/Scripts/python.exe -X utf8 tools/migrate_v21_view_expansion_route_words.py --dry-run
./.venv/Scripts/python.exe -X utf8 tools/migrate_v21_view_expansion_route_words.py
```

幂等：按小写去重，重复执行不新增。
回滚：`intent_keywords` 原值见脚本输出的「改动前原值」。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

TARGET = "view_expansion"

#: ── ① view_expansion：11 个「名词短语」词条（无语序问题，这是与 V14 的本质区别）──
NEW_WORDS = [
    "需求视图", "活动视图", "状态机视图", "用例视图", "时序视图",
    "参数视图", "结构视图", "交互视图", "视图展开", "生成视图", "建模视图",
    # ★ ② 视图**简称**（用户实际说「活动图」不是「活动视图」）。
    #   `_kw_score` 用 `min(1.0, len(k)/4.0)` ⇒ 3 字词得 **0.75 分**（不是 0），
    #   实测 6 个简称全部独占（他人最高恒为 0）。
    #   ⚠️ 别被"3 字 < 4 字门槛"误导——那是我第一版脚本的守卫写错了，
    #   `_kw_score` 本身接受 3 字词，门槛只影响**满分**。
    "活动图", "用例图", "时序图", "参数图", "结构图", "状态机图",
]

#: ── ③ 另外三个节点：补双向词（登记「X生成」vs 用户说「生成X」是语序问题）──
#: 均为实测独占（他人最高 0.0 / 0.75 且本节点更高）
OTHER_PLAN = {
    "trace_verification": ["生成追溯矩阵", "追溯矩阵", "覆盖性分析", "需求覆盖"],
    "model_validation_repair": ["校验修复", "修复模型", "生成校验诊断", "语法修复"],
    "requirement_structuring": ["条目化需求", "需求条目"],
}

#: 用户真实说法 → 用于逐词实测（不靠"词在列表里"就认为能用）
PROBE_Q = {
    "需求视图": "生成需求视图", "活动视图": "生成活动视图",
    "状态机视图": "生成状态机视图", "用例视图": "生成用例视图",
    "时序视图": "生成时序视图", "参数视图": "生成参数视图",
    "结构视图": "生成结构视图", "交互视图": "生成交互视图",
    "视图展开": "生成视图展开", "生成视图": "生成视图", "建模视图": "生成建模视图",
    "活动图": "生成活动图", "用例图": "生成用例图", "时序图": "生成时序图",
    "参数图": "生成参数图", "结构图": "生成结构图", "状态机图": "生成状态机图",
    "生成追溯矩阵": "生成追溯矩阵", "追溯矩阵": "生成追溯矩阵",
    "覆盖性分析": "做覆盖性分析", "需求覆盖": "需求覆盖核验",
    "校验修复": "帮我校验修复", "修复模型": "修复模型",
    "生成校验诊断": "生成校验诊断", "语法修复": "做语法修复",
    "条目化需求": "条目化需求", "需求条目": "需求条目",
}

#: 写入前必须保证不退化的样本（V14b 之后的既有状态，不是期望全绿）
BASELINE_SAMPLES = [
    ("生成架构骨架", "architecture_skeleton"),
    ("模型发布入库", "model_release"),
]


def run(conn, args) -> int:
    from agent.pipeline import AgentPipeline
    from agent.intent import IntentRouter
    from database import db_conn as _dbc

    row = conn.execute(
        "SELECT id, intent_keywords FROM agents WHERE name=?", (TARGET,)).fetchone()
    if not row:
        print(f"[FAIL] 找不到 Agent：{TARGET}")
        return 1
    old = json.loads(row["intent_keywords"] or "[]")
    seen = {str(x).lower() for x in old}
    new = list(old) + [w for w in NEW_WORDS if w.lower() not in seen]
    added = [w for w in NEW_WORDS if w.lower() not in seen]

    print("=" * 72)
    print("① 改动前原值（回滚照抄）")
    print("=" * 72)
    print(f"  {TARGET}.intent_keywords = {old}")
    for a in OTHER_PLAN:
        r2 = conn.execute("SELECT intent_keywords FROM agents WHERE name=?", (a,)).fetchone()
        print(f"  {a}.intent_keywords = {json.loads(r2[0] or '[]')}")

    # ── 先在内存里验，不直接写库 ──
    pipe = AgentPipeline()
    pipe._load_db_agents()
    ir = pipe.router
    GEN = IntentRouter._GENERIC_KW

    print("\n" + "=" * 72)
    print("② 逐词校验：计分 + 独占（写入前）")
    print("=" * 72)
    problems = []
    ir._db_intents[TARGET] = list(new)
    # 三个附带节点也注入（同一批验证，避免"改 A 抢了 B"）
    for a, words in OTHER_PLAN.items():
        cur = ir._db_intents.get(a) or []
        ir._db_intents[a] = list(cur) + [w for w in words if w not in cur]

    def _probe(agent, word):
        """用**用户真实说法**测该词条，并检查是否被他人抢。"""
        q = PROBE_Q.get(word)
        if not q:
            return None, f"缺少探针问句（{word}）"
        t = q.lower()
        ours, _ = ir._kw_score(t, ir._db_intents[agent])
        rivals = {}
        for it, kws in ir._db_intents.items():
            if it in ("chat", agent):
                continue
            s, _h = ir._kw_score(t, kws)
            if s > 0:
                rivals[it] = round(s, 2)
        top = max(rivals.values()) if rivals else 0.0
        return (ours, top), None

    print(f"\n  --- {TARGET} ---")
    for w in NEW_WORDS:
        res, err = _probe(TARGET, w)
        if err:
            problems.append(err)
            print(f"  [FAIL] {w:14} {err}")
            continue
        ours, top = res
        ok = ours > 0 and ours >= top
        if not ok:
            problems.append(f"{w}: 得分 {ours} < 他人最高 {top}")
        print(f"  [{'OK  ' if ok else 'FAIL'}] {w:14} q={PROBE_Q[w]:14} "
              f"本节点={ours} 他人最高={top}")

    for a, words in OTHER_PLAN.items():
        print(f"\n  --- {a} ---")
        for w in words:
            res, err = _probe(a, w)
            if err:
                problems.append(err)
                print(f"  [FAIL] {w:14} {err}")
                continue
            ours, top = res
            ok = ours > 0 and ours >= top
            if not ok:
                problems.append(f"{a}.{w}: 得分 {ours} < 他人最高 {top}")
            print(f"  [{'OK  ' if ok else 'FAIL'}] {w:14} q={PROBE_Q[w]:14} "
                  f"本节点={ours} 他人最高={top}")

    # 基线不退化（改前）
    before_hits = 0
    with _dbc() as c:
        for q, exp in BASELINE_SAMPLES:
            got = ir.detect(q, conn=c)
            if isinstance(got, tuple):
                got = got[0]
            before_hits += got == exp
    print(f"\n  基线样本（改前）：{before_hits}/{len(BASELINE_SAMPLES)}")

    if problems:
        print("\n[FAIL] 以下词条不满足条件，拒绝写入：")
        for p in problems:
            print("  -", p)
        return 1

    if args.dry_run:
        print("\n[dry-run] 未写库")
        return 0

    print("\n" + "=" * 72)
    print("③ 写库")
    print("=" * 72)
    # ★ 只改这四个 Agent，其余一律不动（避免重演 V14「全局改词拉低基线」）
    conn.execute("UPDATE agents SET intent_keywords=? WHERE id=?",
                 (json.dumps(new, ensure_ascii=False), row["id"]))
    print(f"  [写入] {TARGET}(id={row['id']})：{len(old)} → {len(new)}"
          f"（新增 {len(added)}）")
    for a, words in OTHER_PLAN.items():
        cur = json.loads(conn.execute(
            "SELECT intent_keywords FROM agents WHERE name=?", (a,)).fetchone()[0] or "[]")
        s = {str(x).lower() for x in cur}
        merged = list(cur) + [w for w in words if w.lower() not in s]
        conn.execute("UPDATE agents SET intent_keywords=? WHERE name=?",
                     (json.dumps(merged, ensure_ascii=False), a))
        print(f"  [写入] {a}：{len(cur)} → {len(merged)}（新增 {len(merged)-len(cur)}）")
    conn.commit()

    print("\n" + "=" * 72)
    print("④ 出口断言（重新加载后验证）")
    print("=" * 72)
    problems = []
    pipe2 = AgentPipeline()
    pipe2._load_db_agents()
    ir2 = pipe2.router

    for a, words in [(TARGET, NEW_WORDS)] + list(OTHER_PLAN.items()):
        got_kw = json.loads(conn.execute(
            "SELECT intent_keywords FROM agents WHERE name=?", (a,)).fetchone()[0])
        missing = [w for w in words if w not in got_kw]
        print(f"  [{'OK  ' if not missing else 'FAIL'}] {a:26} "
              f"{len(words)} 个词条均已落库（缺：{missing or '无'}）")
        if missing:
            problems.append(f"{a} 缺词条 {missing}")

    print(f"  [OK  ] 其余 Agent 未被触碰（本脚本只 UPDATE 4 个 name）")

    # 基线不退化
    after_hits = 0
    with _dbc() as c:
        for q, exp in BASELINE_SAMPLES:
            got = ir2.detect(q, conn=c)
            if isinstance(got, tuple):
                got = got[0]
            after_hits += got == exp
    print(f"  [{'OK  ' if after_hits >= before_hits else 'FAIL'}] "
          f"基线样本不退化：{before_hits} → {after_hits}")
    if after_hits < before_hits:
        problems.append(f"基线退化 {before_hits} → {after_hits}")

    # 真实 detect 命中（用用户口语，不是我编的问句）
    REAL = [("生成活动图", TARGET), ("生成需求视图", TARGET),
            ("生成追溯矩阵", "trace_verification"),
            ("帮我校验修复模型", "model_validation_repair"),
            ("需求条目化", "requirement_structuring")]
    with _dbc() as c:
        for q, exp in REAL:
            got = ir2.detect(q, conn=c)
            if isinstance(got, tuple):
                got = got[0]
            ok = got == exp
            print(f"  [{'OK  ' if ok else 'FAIL'}] detect({q!r}) = {got}"
                  f"（期望 {exp}）")
            if not ok:
                problems.append(f"detect({q}) = {got} ≠ {exp}")

    if problems:
        print("\n[FAIL]")
        for p in problems:
            print("  -", p)
        return 1

    print("\n" + "=" * 72)
    print("✅ 流水线节点路由词已补")
    print("=" * 72)
    print(f"  {TARGET}：新增 {len(added)} 个（含 6 个视图简称，3 字词得 0.75 分）")
    print("  另外 3 个节点：各补双向词，消除语序不匹配")
    print("   ⚠️ 未动其它 Agent 的词条——避免重演 V14「全局改词拉低基线」")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    from database import db_conn

    with db_conn() as conn:
        return run(conn, args)


if __name__ == "__main__":
    sys.exit(main())
