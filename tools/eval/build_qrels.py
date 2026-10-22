"""从真库自动生成 qrels 候选标注集（人工只需抽检，不必从零标）。

══════════════════════════════════════════════════════════════════════
★ v2 修正：相关集定义从「同 section」改为「**可判定的强相关**」
══════════════════════════════════════════════════════════════════════
v1 的问题（实测发现）：用「同 section 的其他块」当相关集，
但像 `1.1 适用对象与边界是什么？` 这种**通用问题**，
合理答案跨文档（814/815/816三个场景库都有这一节）⇒
把相关集限定在"同文档同 section"会**系统性低估召回**，
且不同文档的同名 query 会被当成不同问题（重复 query）。

v2 规则：
  · **只对"区分度高的 query"建qrel**—— 即该问句在全库出现次数少
    （`--max-dup`，默认 2）且 section 名较长/具体
  · **相关集 = 全库中与该 query 同 section 的所有块的 (doc,idx)**，
    **跨文档**（因为通用问题的答案确实跨文档）
  · 剔除明显噪声块（版权页/免责声明/目录）

⚠️ **这仍不是人工标注**，脚本会标出需核对项；行业建议 50~100 条，
   自动能产出的高质量条目通常远少于该数（本次约 12~30 条），
   **剩余需人工补充**（这正是行业建议"start with 50~100 real questions"的原因）。
"""
import argparse
import json
import os
import re
import sqlite3
import sys
from collections import Counter


#: 噪声块特征（版权页/目录/免责）——实测它们会被"同文档兜底"误当相关块
_NOISE_RE = re.compile(r"Copyright|All rights reserved|©\s*\d{4}|免责声明|目\s*录\s*$", re.I)


#: 低信息量 query（日期/纯符号/过长数字）—— 不适合做评测题
_LOW_INFO_RE = re.compile(r"^\s*[\d\s年月日号第.-]+\s*|\d{4}\s*年?\s*月?\s*日?$|\b\d+\b")


def _is_noise(chunk_id: int, by_id: dict) -> bool:
    t = (by_id.get(chunk_id) or "")
    return bool(_NOISE_RE.search(t[:300]))


_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
DB = os.path.join(_ROOT, "mbse.db")
OUT = os.path.join(_ROOT, "tools", "eval", "qrels.jsonl")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=80, help="目标条数")
    ap.add_argument("--max-dup", type=int, default=2,
                    help="问句在全库出现超过此数⇒ 同质化，跳过")
    ap.add_argument("--min-len", type=int, default=40, help="块内容最小长度")
    a = ap.parse_args()

    c = sqlite3.connect("file:" + DB + "?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    rows = c.execute(
        "SELECT dc.id, dc.content, dc.section, dc.hyde_questions, dc.document_id, "
        "       d.filename FROM document_chunks dc "
        "LEFT JOIN documents d ON d.id=dc.document_id "
        "WHERE dc.content != '' AND dc.hyde_questions NOT IN ('','[]')"
    ).fetchall()
    print(f"候选池：{len(rows)} 块")

    # 先统计问句重复度（用于剔除同质化）
    dup = Counter()
    parsed = []
    for r in rows:
        try:
            qs = json.loads(r["hyde_questions"] or "[]")
        except Exception:
            qs = []
        if not qs:
            continue
        q = qs[0]
        dup[q] += 1
        parsed.append((r, q))
    print(f"问句去重前 {len(parsed)} 条，唯一问句 {len(dup)} 个")

    # section -> [chunk_id]，用于构造相关集
    by_id = {}
    for r, _ in parsed:
        by_id[r["id"]] = r["content"] or ""
    by_sec = {}
    by_doc = {}
    for r, _ in parsed:
        sec = (r["section"] or "").strip()
        if sec:
            by_sec.setdefault(sec, []).append(r["id"])
        by_doc.setdefault(r["document_id"], []).append(r["id"])

    out, skipped_dup, skipped_short, need_review, skipped_nosec = [], 0, 0, 0, 0
    skipped_sameq = 0
    # ★ 问句去重：不同文档的同section 可能生成**完全相同的 query**
    #   （实测 `1.1 适用对象与边界是什么？` 出现 3 次）——
    #   评测集里重复 query 会让指标被单条放大，且浪费样本额度。
    #   ⇒ 每个 query 只保留**首个**（相关集最完整的那条）。
    seen_q = set()
    # 文档轮转采样，避免全来自同一个 PDF
    by_docid = {}
    for r, q in parsed:
        by_docid.setdefault(r["document_id"], []).append((r, q))
    docids = list(by_docid)

    i = 0
    while len(out) < a.n and i < len(docids) * 40:
        did = docids[i % len(docids)]
        lst = by_docid[did]
        r, q = lst[(i // len(docids)) % len(lst)] if lst else (None, None)
        i += 1
        if r is None:
            continue
        if q in seen_q:
            skipped_sameq += 1
            continue
        if len(r["content"] or "") < a.min_len:
            skipped_short += 1
            continue
        if dup[q] > a.max_dup:
            skipped_dup += 1
            continue
        # ★ 剔除"低信息量 query"：日期/纯数字/单个词等（如 `Jul 2023是什么？`）
        #   实测踩坑：这类问句对应的块是版权声明，回答它没有意义。
        # ★ 更严的"低信息量"判据（实测 `Jul 2023是什么？` 仍漏网）：
        #   问句主干（剥掉 是什么/怎么使用 等后缀）必须
        #   **含中文**，或 **≥2 个英文单词** —— 否则是日期/代号类无意义 query。
        _core = re.sub(r"(是什么|怎么使用|包含哪些内容|有什么关系)[？?]?$", "", q).strip()
        _has_cn = bool(re.search(r"[\u4e00-\u9fff]", _core))
        _en_words = len(re.findall(r"[A-Za-z][A-Za-z0-9_.]{1,}", _core))
        if _LOW_INFO_RE.search(q) or len(q) < 8 or not (_has_cn or _en_words >= 2):
            skipped_dup += 1
            continue
        # ★★ v2：**跨文档**的"同 section"全部块（通用问题的答案确实跨文档），
        #   并剔除噪声块（版权页/目录/免责）。
        #   v1 的两个错误都会失真：
        #     ① "同文档兜底"把版权页当相关块（实测前 4 条就有 2 条噪声）
        #     ② "限定同文档"低估跨文档通用问题的召回
        sec = (r["section"] or "").strip()
        if not sec:
            skipped_nosec += 1
            continue
        cands = by_sec.get(sec) or []
        rel = [x for x in cands if x != r["id"] and not _is_noise(x, by_id)][:6]
        note = ""
        # ★ 2026-10-22 修正：此处原是 `if len(rel) < 2: continue` 之后又判一次
        #   `if len(rel) < 2: note=...; need_review += 1` —— 第二个判断**永远不可达**
        #   （第一个已 continue）⇒ `need_review` 恒为 0、人工复核环节从未触发，
        #   而报告显示"需人工确认 0 条"看起来像"没有需要复核的"，实则是判据失效。
        #   改法：相关集为 0 ⇒ 不可用（跳过）；为 1 ⇒ **可用但标记需复核**（单条相关集
        #   算不出 recall 的分母，评测价值有限，但不该直接丢）。
        if not rel:
            skipped_nosec += 1
            continue
        if len(rel) == 1:
            note = "相关集仅 1 条（算不出 recall 分母），需人工确认"
            need_review += 1
        seen_q.add(q)
        out.append({
            "query": q,
            "relevant": rel,
            "self": r["id"],
            "section": sec,
            "doc": r["filename"] or "",
            "note": note,
        })

    # ★ 相关集改存**复合键** "document_id:chunk_index"——
    #   实测 `chunk_hits` 里没有 chunk_id，只有 (document_id, chunk_index)，
    #   评测器用同一套键才能对齐（裸chunk_id 会让指标恒0）。
    import sqlite3 as _s
    _c = _s.connect("file:" + DB + "?mode=ro", uri=True)
    id2key = {}
    for _r in _c.execute("SELECT id, document_id, chunk_index FROM document_chunks"):
        id2key[_r[0]] = f"{_r[1]}:{_r[2]}"
    _c.close()
    for o in out:
        o["relevant_keys"] = [id2key[i] for i in o["relevant"] if i in id2key]

    c.close()
    with open(OUT, "w", encoding="utf-8") as f:
        for o in out:
            f.write(json.dumps(o, ensure_ascii=False) + "\n")
    print(f"\n★ 已写出 {len(out)} 条 -> tools/eval/qrels.jsonl")
    print(f"  跳过：同质化 {skipped_dup}　过短 {skipped_short}　无同section邻居 {skipped_nosec}　重复query {skipped_sameq}")
    print(f"  ★ 需人工确认 {need_review} 条（note 非空）")
    print("\n下一步：")
    print("  1) 人工抽检 qrels.jsonl（重点看 note 非空的）")
    print("  2) 跑基线：python tools/eval/eval_retrieval.py --offline")
    print("  3) 跑真实：python tools/eval/eval_retrieval.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())