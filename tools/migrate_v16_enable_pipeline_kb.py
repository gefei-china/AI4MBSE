"""migrate_v16_enable_pipeline_kb —给流水线 N0~N6 **启用**知识库消费（配窄 scope）。

## 背景（2026-10-09 实测）

现状：8 个流水线节点**全部 `kb_required=0` / `kb_scope={}`**
⇒ 建模主链**从不读知识库**。广汽方法论（documents.id=812，180 chunks）
就在库里，但没有任何节点会消费它。

⇒ 直接后果（实测可解释）：模型产出的 SysML **语法对**（checker判 pass）
但**不符合客户方法论** —— 方法论在知识库、通用语法在 skill，两套资产从未接线。

本脚本**只改配置**（`agents.kb_required` / `agents.kb_scope`），不改一行代码。

## ⚠️ 动手前先查清三件事（都是本轮实测踩到的坑）

**① `kb_scope.mode` 是死字段**
`rag.py` 实际只消费 `branches` / `docs` / `include_deprecated` /
`include_ai_generated` / `project_id` / `intent` / `memory_scopes`
（实测 `grep -o "scope.get(...)"` 全量列举）。
⇒ 现有 3 个 Agent 配的 `"mode": "all_release"` **从未被任何代码读取**，
它们实际靠 `branches` 缺省值工作。
**本脚本不再写 `mode`**（写了也是自欺），只写真正生效的 `branches` + `docs`。

**② 文档白名单必须用库里的准确文件名，否则会静默放宽**
`rag.py:477 _resolve_scope_docs` 会把不存在的文件名剔除，
**全部失效时"放宽为不限文档"**（实测 `design` 配的
`知识库_运行视角_视图规范元素要求.md`，库里实际是 `...(1).md`
⇒ 求交为空 ⇒ **静默变成读全库**，只打一行 warn）。

⇒ 本脚本的每个文档名都**先过 `_resolve_scope_docs` 实测**，
   确认 `warn is None`（即真的过滤生效）才写入。

**③ `docs` 重复项会被去重**
现有 `design.kb_scope.docs` 里同一个文件名列了两次
⇒ 本脚本统一 `sorted(set(...))`。

## 节点 → 知识库映射（按职责，一句话能说清为什么）

| 节点 | 消费什么 | 依据 |
|---|---|---|
| N0 methodology_resolver | 广汽方法论 + 视图规范 2篇 | 方法论解析节点，只该读方法论本身 |
| N1 requirement_structuring | 广汽方法论 + Requirements Guide | 需求条目化要看方法论的需求分层 + OMG 需求写作指南 |
| N2 architecture_skeleton | 广汽方法论 + 视图规范 2 篇 | 骨架层（M0-M3 里的 M3）是方法论的核心 |
| N3 view_expansion | 视图规范 2 篇 + 常见语法错误 | 视图节点只关心"该有哪些要素" |
| N4 model_validation_repair | 常见语法错误与修正 | 校验修复节点，与错误清单强相关 |
| N5 trace_verification | 广汽方法论（IR→VR→SR→SSR→AR 追溯链） | 追溯矩阵要按方法论的追溯层级 |
| G change_safety_gate | （不启用）| 变更安全门是**结构性分析**，靠图谱关系而非文档 |
| N6 model_release | （不启用）| 发布清单已由 `sysml_release_checklist` skill 承载 |

⚠️ **刻意收窄**：不启用 G/N6 是因为它们的工作依据在**图结构与规则**，
不在文档。**全开= 无差别召回 7269 chunks = 每轮都在稀释注意力**
（这正是前几轮踩过的"窗口里放太多反而更差"的同一个错误）。

## 幂等 / 回滚

- 配置整体覆盖式更新，脚本会打印改动前原值；
- 回滚：本脚本输出的「改动前原值」照抄即可；
- 脚本自带 `--dry-run`：只校验不写库。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# ── 文档名必须与库里filename **逐字一致**（rag.py:477 会剔除不匹配项并静默放宽）──
DOC_GAC = "广汽方法论-260813.docx"                     # id=812, 180 chunks
DOC_VIEW_ELEMENTS = "知识库_运行视角_视图规范元素要求(1).md"   # id=817, 30 chunks
DOC_VIEW_RELATIONS = "知识库_运行视角_视图间关联关系(1).md"   # id=818, 22 chunks
DOC_SYNTAX_ERR = "SysML_v2_常见语法错误与修正(1).md"        # id=816, 101 chunks
DOC_OMG_REQ = "Guide to writing Requirements.pdf"      # id=793, 855 chunks

#: 节点 → 消费范围（只写真实生效的字段：branches + docs）
SCOPES = {
    "methodology_resolver": [DOC_GAC, DOC_VIEW_ELEMENTS, DOC_VIEW_RELATIONS],
    "requirement_structuring": [DOC_GAC, DOC_OMG_REQ],
    "architecture_skeleton": [DOC_GAC, DOC_VIEW_ELEMENTS, DOC_VIEW_RELATIONS],
    "view_expansion": [DOC_VIEW_ELEMENTS, DOC_VIEW_RELATIONS, DOC_SYNTAX_ERR],
    "model_validation_repair": [DOC_SYNTAX_ERR],
    "trace_verification": [DOC_GAC],
    # change_safety_gate / model_release 刻意不启用（见上方说明）
}
ENABLE = list(SCOPES)


def verify_whitelist(conn, docs):
    """★ 用 rag 的**真实**求交逻辑校验白名单，确认不会静默放宽。

    ⚠️ 2026-10-09 自踩：`db_conn()` 是 `@contextmanager`，不是连接对象。
       第一版直接 `conn = db_conn()` 传给本函数 ⇒ 内部 `conn.execute`
       抛 AttributeError ⇒ **被 `_resolve_scope_docs` 的 try/except 自愈兜住**
       ⇒ `warn` 恒为 None、`eff` 原样返回 ⇒ **校验全部"假通过"**。
    ⇒ 这里额外断言返回类型，确保真的跑过（见 `verify_whitelist`内）。
    """
    from agent.rag import GraphRAG
    eff, warn = GraphRAG()._resolve_scope_docs(conn, list(docs))
    # 自检：真跑过才会产出 list；异常兜底路径会原样返回传入对象
    assert isinstance(eff, list), f"求交未真正执行（传入对象被原样返回）：{type(eff)}"
    return eff, warn


def run(conn, args) -> int:
    print("=" * 72)
    print("① 文档名实测校验（rag._resolve_scope_docs 真实逻辑）")
    print("=" * 72)
    all_docs = sorted({d for v in SCOPES.values() for d in v})
    bad = []
    for d in all_docs:
        eff, warn = verify_whitelist(conn, [d])
        ok = (warn is None) and (eff == [d])
        print(f"  [{'OK  ' if ok else 'FAIL'}] {d[:48]:50} 生效={eff == [d]}")
        if not ok:
            bad.append((d, warn))
    if bad:
        print("\n[FAIL] 以下文档名无法精确命中（会被静默剔除并放宽为读全库）：")
        for d, w in bad:
            print(f"   - {d}  warn={w}")
        print("\n⇒ 拒绝写入。先用库里准确文件名修正 SCOPES。")
        return 1
    print(f"  全部 {len(all_docs)} 个文档名可精确命中 ✓")

    print("\n" + "=" * 72)
    print("② 改动前原值（回滚照抄）")
    print("=" * 72)
    before = {}
    for n in ENABLE:
        r = conn.execute(
            "SELECT kb_required, kb_scope FROM agents WHERE name=?", (n,)).fetchone()
        before[n] = (r[0], r[1])
        print(f"  {n:28} kb_required={r[0]} kb_scope={r[1]}")

    print("\n" + "=" * 72)
    print("③ 改动后取值")
    print("=" * 72)
    payload = {}
    for n in ENABLE:
        docs = sorted(set(SCOPES[n]))          # 去重 + 稳定序
        payload[n] = {"branches": ["release"], "docs": docs}
        print(f"  {n:28} kb_required=1")
        print(f"    {'':28} docs({len(docs)})={docs}")

    if args.dry_run:
        print("\n[dry-run] 未写库")
        return 0

    print("\n" + "=" * 72)
    print("④ 写库")
    print("=" * 72)
    for n in ENABLE:
        conn.execute(
            "UPDATE agents SET kb_required=1, kb_scope=? WHERE name=?",
            (json.dumps(payload[n], ensure_ascii=False), n))
        print(f"  [写入] {n}")
    conn.commit()

    # ── 出口断言：写库后必须仍能精确命中（不能写完就算）──
    print("\n" + "=" * 72)
    print("⑤ 出口断言（写库后复测白名单仍精确）")
    print("=" * 72)
    problems = []
    for n in ENABLE:
        r = conn.execute(
            "SELECT kb_required, kb_scope FROM agents WHERE name=?", (n,)).fetchone()
        if not r[0]:
            problems.append(f"{n} kb_required 未生效")
            continue
        sc = json.loads(r[1] or "{}")
        eff, warn = verify_whitelist(conn, sc.get("docs") or [])
        exact = (warn is None) and set(eff) == set(sc.get("docs") or [])
        print(f"  [{'OK  ' if exact else 'FAIL'}] {n:28} kb_required={r[0]} "
              f"docs生效={len(eff)}/{len(sc.get('docs') or [])}")
        if not exact:
            problems.append(f"{n} 白名单未精确生效 warn={warn}")

    if problems:
        print("\n[FAIL]")
        for p in problems:
            print("   -", p)
        return 1

    print("\n✅ 全部通过：6 个节点已启用知识库消费，白名单精确生效")
    print("   ⚠️ G change_safety_gate / N6 model_release **刻意未启用**")
    print("      （依据在图结构与发布清单 skill，不在文档）")
    print("\n⚠️ 启用 ≠ 已生效：还需实测一次真实调用，确认 prompt 里真的带上了检索内容。")
    print("   LLM 余额耗尽（402）时无法验证——那时会静默回落 Mock，结论不可信。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只校验不写库")
    args = ap.parse_args()

    from database import db_conn

    # ★ db_conn() 是 @contextmanager，必须 with 使用。
    #   第一版直接 `conn = db_conn()` 当连接对象 ⇒ 内部 AttributeError
    #   ⇒ 被 _resolve_scope_docs 的 try/except 自愈兜住 ⇒ 校验"假通过"。
    with db_conn() as conn:
        return run(conn, args)


if __name__ == "__main__":
    sys.exit(main())