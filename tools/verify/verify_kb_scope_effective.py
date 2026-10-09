"""verify_kb_scope_effective —门禁：所有带 `kb_scope.docs` 的 Agent，其白名单**真的**在过滤。
# CI-OPTIONAL: 断言的是生产库里的 kb_scope 配置与 documents 文件名；干净库无这些数据 ⇒ 判红无意义（应与配置变更同批在真库跑）。

## 为什么需要这道门禁（2026-10-09 实测踩坑）

`rag.py:_resolve_scope_docs` 有**自愈逻辑**：白名单里的文件名一个都匹配不上时，
它会**剔除全部并放宽为"不限文档"**（=该Agent 读全库7269 chunks），
只打一行 `kb_scope_warn`。

实测踩到的三个坑：
  ① `design.kb_scope.docs` 配的是 `...视图规范元素要求.md`，
     库里实际是 `...(1).md` ⇒ 静默变成读全库。
     且原配置**同一份文档列了两次**（去重后只剩1 个错的）。
  ② `impact` / `knowledge_qa` / `chat` 配的 `"mode":"all_release"`
     是**死字段**，`rag.py` 从未读取它。
  ③ 校验脚本把 `db_conn()`（`@contextmanager`）当连接对象用 ⇒ 内部
     `AttributeError` 被自愈 `try/except` 兜住 ⇒ **校验"假通过"**。

⇒ 「字段非空」不等于「过滤生效」。必须**用 rag 的真实检索链路**跑一遍，
   断言召回的 chunk **全部落在白名单内**。

## 判据（缺一即FAIL）

① 对**所有** `kb_required=1` 且 `docs` 非空的 Agent：
   · `_resolve_scope_docs` 求交结果精确等于配置值，且 `warn is None`
   · 真实检索召回的 chunk **全部**落在白名单内（零泄漏）
   · 至少召回 ≥1 个 chunk（否则等于没接上）
② `docs` 不得有重复项（同一份列两次是配置错误的信号）
③ 全库扫描：任何"配了 docs 但求交失效"的 Agent 都判红——
   **「以为有限制、实际读全库」比「明确读全库」危险得多**
④ 区分两种"读全库"（这是本门禁最容易误报的地方）：
   · **假收窄** = 配了 `docs` 但求交失效 ⇒ 判红（如原 design）
   · **明确读全库** = 有意如此（通用问答/影响分析需要全量材料）
     ⇒ 合法，但必须在 `INTENT_ALL` 白名单里**显式声明**，
     不允许"因为没配所以默认全读"这种隐式状态。
     （`impact` / `knowledge_qa` / `chat` 属此类）
⑤ 流水线 8 节点里，刻意未启用的 G/N6 必须仍为 `kb_required=0`
   （防止被误开成读全库）

用法：`.venv/Scripts/python.exe -X utf8 tools/verify/verify_kb_scope_effective.py`
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

#: 流水线节点 → 代表性查询（按节点职责选词，测"该节点该看到什么"）
CASES = [
    ("methodology_resolver", "M0 M1 M2 M3 方法论分层 建模约定"),
    ("requirement_structuring", "需求条目化 requirement writing 指南"),
    ("architecture_skeleton", "M3 架构视角 骨架 部件端口"),
    ("view_expansion", "SysML v2 视图要素与建模规范"),
    ("model_validation_repair", "SysML v2 常见语法错误与修正"),
    ("trace_verification", "IR VR SR SSR AR 追溯链 需求分解层级"),
    # 通用 Agent（V18 修复 design 后纳入）
    ("design", "活动视图有哪些必备元素与视图间关联关系"),
]

#: 刻意不启用知识库的节点（依据在图结构/规则，不在文档）
EXPECT_OFF = ("change_safety_gate", "model_release")


def main() -> int:
    from agent.rag import GraphRAG

    db = os.path.join(ROOT, "mbse.db")
    cc = sqlite3.connect(db)
    cc.row_factory = sqlite3.Row
    g = GraphRAG()

    problems = []
    print("=" * 72)
    print("① 启用节点的 kb_scope 是否真的在过滤")
    print("=" * 72)
    total_hits = 0
    for name, q in CASES:
        row = cc.execute(
            "SELECT kb_required, kb_scope FROM agents WHERE name=?", (name,)).fetchone()
        if not row:
            problems.append(f"{name}: Agent 不存在")
            print(f"  [FAIL] {name:26} Agent 不存在")
            continue
        if not row["kb_required"]:
            problems.append(f"{name}: kb_required 未启用")
            print(f"  [FAIL] {name:26} kb_required=0")
            continue
        scope = json.loads(row["kb_scope"] or "{}")
        allow = list(scope.get("docs") or [])
        # ④ 白名单非空：kb_required=1 但 docs=[] 等于读全库 = 假收窄
        if not allow:
            problems.append(f"{name}: docs 白名单为空（=读全库，假收窄）")
            print(f"  [FAIL] {name:26} docs=[]")
            continue
        # ② 去重
        dup = [k for k, v in Counter(allow).items() if v > 1]
        if dup:
            problems.append(f"{name}: docs 有重复项 {dup}")
            print(f"  [FAIL] {name:26} docs 重复={dup}")
            continue

        # ① 求交必须精确且 warn 为空
        eff, warn = g._resolve_scope_docs(cc, allow)
        exact = (warn is None) and set(eff) == set(allow)
        if not exact:
            problems.append(f"{name}: 求交未精确生效 warn={warn}")

        r = g.retrieve(q, kb_scope=scope)
        hits = r.get("chunk_hits") or []
        total_hits += len(hits)
        srcs = Counter()
        for h in hits:
            if isinstance(h, dict):
                srcs[h.get("source_doc") or h.get("doc")
                     or h.get("filename") or "?"] += 1
            else:
                srcs[str(h)[:40]] += 1
        # ③ 零泄漏
        leak = {k: v for k, v in srcs.items() if k not in set(allow)}
        # ② 至少召回 1 个
        got = bool(hits)

        ok = exact and (not leak) and got
        if not ok:
            if not got:
                problems.append(f"{name}: 召回 0 chunk（等于没接上）")
            if leak:
                problems.append(f"{name}: 召回越出白名单 {list(leak)}")
        print(f"  [{'OK  ' if ok else 'FAIL'}] {name:26} 召回={len(hits):2} "
              f"精确={exact} 泄漏={list(leak)[:2] or '无'}")
        for k, v in srcs.most_common(3):
            print(f"         {v:2}x {k[:56]}")

    print("\n" + "=" * 72)
    print("② 全库扫描：还有没有别的 Agent 在静默读全库")
    print("=" * 72)
    rows = cc.execute(
        "SELECT name, kb_required, kb_scope FROM agents "
        "WHERE kb_required=1 AND kb_scope IS NOT NULL AND kb_scope NOT IN ('','{}')"
    ).fetchall()
    scanned = {n for n, _ in CASES}

    # ★ 明确声明「本Agent 就该读全库」的（通用问答/影响分析需要全量材料）。
    #   判据：`kb_scope` 里**既无 docs 也无 branches**（=真读全库），
    #   且历史上没有配过mode（mode 是死字段，见下）。
    #   ⚠️ 区别于「假收窄」：假收窄 = 配了 docs 但求交失效 ⇒ 以为有限制实际全读；
    #     这里是真的想全读。两者危害不同，不能混为一谈。
    INTENT_ALL = {"impact", "knowledge_qa", "chat"}

    others = [r for r in rows if r["name"] not in scanned]
    print(f"  带 kb_scope 配置但未在上面逐条验证的 Agent：{len(others)} 个")
    for r in others:
        name = r["name"]
        sc = json.loads(r["kb_scope"] or "{}")
        docs = sc.get("docs") or []
        branches = sc.get("branches") or []

        if name in INTENT_ALL:
            ok = not docs and not branches
            print(f"    [{'OK  ' if ok else 'FAIL'}] {name:24} 明确读全库"
                  f"（通用问答/影响分析，需全量材料）")
            if not ok:
                problems.append(
                    f"{name}: 标为读全库，却配了 docs/branches ⇒ 意图与配置矛盾")
            # 死字段提示（不判红：是历史遗留，清理属另一次改动）
            if sc.get("mode"):
                print(f"         [提示] 含死字段 mode={sc['mode']!r}"
                      f"（rag.py 从未读取；'看起来有限制'实为无限制）")
            continue

        if not docs:
            problems.append(f"{name}: kb_required=1 但无 docs/branches"
                            f"（读全库；若为有意需加入 INTENT_ALL 白名单并说明理由）")
            print(f"    [FAIL] {name:24} 无 docs/branches ⇒ 读全库且未声明意图")
            continue
        eff, warn = g._resolve_scope_docs(cc, docs)
        exact = (warn is None) and set(eff) == set(docs)
        if not exact:
            print(f"    [FAIL] {name:24} 求交未精确生效 warn={warn}")
            problems.append(f"{name}: 白名单未精确生效 warn={warn}")
        else:
            print(f"    [OK  ] {name:24} docs={len(docs)} 份，求交精确")
        if sc.get("mode"):
            print(f"         [提示] 含死字段 mode={sc['mode']!r}（建议清理）")

    print("\n" + "=" * 72)
    print("③ 刻意未启用的流水线节点必须仍为 0")
    print("=" * 72)
    for name in EXPECT_OFF:
        row = cc.execute(
            "SELECT kb_required FROM agents WHERE name=?", (name,)).fetchone()
        v = row["kb_required"] if row else None
        ok = not v
        if not ok:
            problems.append(f"{name}: 被误启用 kb_required={v}")
        print(f"  [{'OK  ' if ok else 'FAIL'}] {name:26} kb_required={v}（期望 0）")

    print("\n" + "=" * 72)
    print(f"汇总：召回 {total_hits} 个 chunk，问题 {len(problems)} 项")
    print("=" * 72)
    for p in problems:
        print("  - " + p)
    cc.close()
    if problems:
        print("\n[FAIL] kb_scope 未真正生效")
        return 1
    print("\n✅ 白名单精确生效：所有启用 Agent 的召回内容都落在各自声明的文档内")
    return 0


if __name__ == "__main__":
    sys.exit(main())
