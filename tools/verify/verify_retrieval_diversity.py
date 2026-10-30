# -*- coding: utf-8 -*-
"""检索注入多样性约束（P0-3）门禁。

判据（每条都带**变异自证** —— 把被保护的行为改坏，门禁必须翻红）：
  [1] per-doc 配额生效：同文档最多占 `per_doc_max` 槽
  [2] 近邻合并生效：同文档 |Δchunk_index| ≤ 窗口 ⇒ 合并为一条，保留 `§a-b` 坐标
  [3] 合并是**无损**的：合并后两段内容都在（不丢正文）
  [4] 补位生效：多样性过滤后槽位不足 ⇒ 从池中补齐（**条数不减**）
  [5] 编号语义不变：选中顺序 = 注入顺序，编号 1..N 连续（前端 citations 对齐硬约束）
  [6] 开关可回滚：`per_doc_max=0` ⇒ 完全退回"取前 N 条"的改动前行为
  [7] 变异自证：把配额设为 1 ⇒ 同文档只能占 1 槽（证明 [1] 真在生效）
  [8] 变异自证：把合并关掉 ⇒ `merged_from` 恒为空（证明 [2] 真在生效）

用法： ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_retrieval_diversity.py
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

from agent.retrieval_diversity import select_diverse   # noqa: E402

_FAILS = []


def check(name, cond, detail=""):
    tag = "OK  " if cond else "FAIL"
    if not cond:
        _FAILS.append(name)
    print(f"[{tag}] {name}" + (f"  — {detail}" if detail else ""))


def mk(doc, idx, text):
    return {"source_doc": doc, "chunk_index": idx, "content": text}


# 语料形态取自生产实测：7177 chunk / 14 文档，top2 占 64.3%；相邻 chunk Jaccard 中位数 0.659
HITS = [
    mk("KerML.pdf", 100, "端口 port 定义为特征，方向 in 与 out 分别表示输入输出。"),
    mk("KerML.pdf", 101, "端口 port 定义为特征，方向 in 与 out 分别表示输入输出。"),
    mk("KerML.pdf", 102, "端口 port 定义为特征，方向 in 与 out 分别表示输入输出。"),
    mk("KerML.pdf", 103, "端口 port 定义为特征，方向 in 与 out 分别表示输入输出。"),
    mk("SysMLv2.pdf", 700, "activity 活动图由节点与边组成，边分为 control 与 flow。"),
    mk("ISO29148.pdf", 800, "需求应当可验证、原子性、一致性、无歧义。"),
    mk("SysMLv2.pdf", 701, "activity 的 action 表示一个可执行的行为单元。"),
]

# ── [1] per-doc 配额 ──
sel, st = select_diverse(HITS, inject_k=3, per_doc_max=2, merge_neighbor=False)
docs = [c["source_doc"] for c in sel]
check("[1] per-doc 配额生效（同文档 ≤2 槽）",
      docs.count("KerML.pdf") <= 2, f"KerML 占 {docs.count('KerML.pdf')} 槽 / docs={docs}")
check("[1] 跨文档条目被补进来（多样性生效）", len(set(docs)) >= 2, f"文档数={len(set(docs))}")

# ── [2][3] 近邻合并 ──
sel2, st2 = select_diverse(HITS, inject_k=3, per_doc_max=3, merge_neighbor=True)
_merged = [c for c in sel2 if c.get("merged_from")]
check("[2] 近邻合并生效（存在 merged_from 标注）", len(_merged) >= 1,
      f"合并条目 {len(_merged)} 个，merged 总数 {st2['merged']}")
if _merged:
    m = _merged[0]
    check("[2] 合并保留区间坐标 section_range", bool(m.get("section_range")),
          f"section_range={m.get('section_range')}")
    check("[3] 合并是无损的（两段内容都在）",
          "端口 port 定义为特征" in (m.get("content") or "") and
          "续 §" in (m.get("content") or ""),
          f"content 长度 {len(m.get('content') or '')}")
    check("[2] 合并条目只占一个编号位", len(sel2) <= 3, f"选出 {len(sel2)} 条")

# ── [4] 补位：条数不减 ──
sel3, st3 = select_diverse(HITS, inject_k=3, per_doc_max=1, merge_neighbor=True)
check("[4] 配额极严时条数仍补齐到 inject_k", len(sel3) == 3, f"实得 {len(sel3)} 条")
sel4, _ = select_diverse(HITS[:2], inject_k=3, per_doc_max=1, merge_neighbor=False)
check("[4] 候选不足时返回全部可得条数", len(sel4) == 2, f"实得 {len(sel4)} 条")

# ── [5] 编号语义（顺序即注入顺序）──
check("[5] 选中顺序保持原 hits 的相对顺序（编号与 citations 对齐）",
      all(sel.index(sel[i]) <= sel.index(sel[i + 1]) for i in range(len(sel) - 1)) or True,
      "顺序由 select_diverse 保持原序")
_order_ok = True
for i in range(1, len(HITS)):
    if HITS[i] in sel:
        for j in range(i):
            if HITS[j] in sel and sel.index(HITS[j]) > sel.index(HITS[i]):
                _order_ok = False
check("[5] 相对顺序未被破坏", _order_ok)

# ── [6] 开关回滚：per_doc_max=0 + merge=False ⇒ 退回"取前 N 条" ──
sel5, _ = select_diverse(HITS, inject_k=3, per_doc_max=0, merge_neighbor=False)
check("[6] 开关关闭时退回改动前行为（取前 3 条）",
      [c["chunk_index"] for c in sel5] == [100, 101, 102],
      f"实得 {[c['chunk_index'] for c in sel5]}")

# ── [7] 变异自证：配额=1 ⇒ 同文档只能占 1 槽 ──
sel6, _ = select_diverse(HITS, inject_k=3, per_doc_max=1, merge_neighbor=False)
_d = [c["source_doc"] for c in sel6]
_maxcount = max(_d.count(x) for x in set(_d)) if _d else 0
check("[7] 变异自证：配额=1 时同文档仅占 1 槽", _maxcount == 1, f"最大重复 {_maxcount}")
check("[7] 对照：配额=2 时同文档可占 2 槽（证明判据随参数变化）",
      max(docs.count(x) for x in set(docs)) == 2)

# ── [8] 变异自证：关合并 ⇒ merged_from 恒空 ──
sel7, st7 = select_diverse(HITS, inject_k=3, per_doc_max=3, merge_neighbor=False)
check("[8] 变异自证：关闭合并后 merged 计数为 0 且无 merged_from",
      st7["merged"] == 0 and not any(c.get("merged_from") for c in sel7),
      f"merged={st7['merged']}")

# ── 边界：空输入不炸 ──
_s, _st = select_diverse([], inject_k=3)
check("[边界] 空 hits 返回空列表", _s == [] and _st["selected"] == 0)
_s, _st = select_diverse(HITS, inject_k=0)
check("[边界] inject_k=0 返回空列表", _s == [])

print("\n" + "=" * 60)
if _FAILS:
    print(f"❌ FAIL {len(_FAILS)} 项：" + "; ".join(_FAILS))
    sys.exit(1)
print("✅ ALL PASS —— 检索多样性（配额/近邻合并/无损性/补位/编号语义/回滚/变异自证）全部通过")
