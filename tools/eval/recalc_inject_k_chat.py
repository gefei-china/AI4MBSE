# -*- coding: utf-8 -*-
"""用**已跑完的真实对话**重算 inject_k A/B 判定（不重跑 LLM）。

## 为什么要单独有这个脚本

`eval_inject_k_chat_ab.py` 第一版的上下文判定写错了（只用"按 chunk_index 回查原文"），
而检索的**近邻合并**会把多个 chunk 拼成一条、`chunk_index` 只留第一个
⇒ 回查拿到的比 citations 里的还短 ⇒ 锚点丢失 ⇒ **假 ungrounded**。

实测影响：`conv=9954`（inject_k=5）被判成 ungrounded，实际是 grounded，
差一点就据此把"5 档更好"的结论改成"两档无差别"。

重跑 LLM 有两个坏处：① 每次十几分钟；② **引入新的模型波动**，
让"判定逻辑修正"和"模型随机性"两个变化混在一起，无法归因。
⇒ 用库里**同一批回答**重算，只改判定逻辑，变量唯一。

## 用法

  ./.venv/Scripts/python.exe -X utf8 tools/eval/recalc_inject_k_chat.py \
      --baseline tools/verify/baselines/inject_k_chat_round1.json
"""
import os
import re
import sys
import json
import argparse
import sqlite3

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

REFUSAL_PAT = re.compile(
    r"(没有(?:相关|找到|检索到|提及)|未(?:找到|提及|检索到|包含)|不存在(?:相关)?|"
    r"知识库中(?:并)?(?:没有|未)|无相关(?:内容|章节|要求)|无法(?:从|在)知识库)")

# ══════════════════════════════════════════════════════════════════
# 锚点修正表（旧锚点 → (上下文锚点, 答案关键词列表)）
#
# ## 为什么必须把两个判据拆开（这是本脚本存在的主要理由）
# 同一个 `anchor` 之前**同时**承担两件事，而这两件事对文本形态的要求正好相反：
#   · 判「答案对不对」→ 必须**宽松**：模型一定会改写措辞，字面比对必然假阴性
#   · 判「依据在不在」→ 必须**严格**：要用该章节的原文短语，才能确认这条证据真的进来了
# 混用一个字符串的后果（实测，4/17 条被误判"失败"，其实回答全是对的）：
#   1. 语序型锚点 `整车需求VR为输出`：库中原文"以整车需求VR为输出"，
#      模型答"以**整车黑盒需求（VR）**为输出" ⇒ 字面不匹配 ⇒ 误判 missed。
#   2. `子系统需求SSR为输入` 同理（模型答"输入是子系统需求（SSR）"）。
#   3. 锚点选偏：`制动功率` 在库里是"正**制动功率**区"（BSFC 适用条件），
#      与"关注哪些功率指标"无关；正确答案在"功率需求观测…牵引、附件和总功率需求"。
# ⇒ 现在：`ctx_anchor` 只用于判依据（严格），`answer_keys` 只用于判答案（宽松）。
# ══════════════════════════════════════════════════════════════════
ANCHOR_FIX = {
    # 旧锚点: (上下文锚点, [答案关键词])
    "整车需求VR为输出": ("整车需求VR为输出", ["整车黑盒需求", "VR"]),
    "子系统需求SSR为输入": ("子系统需求SSR为输入", ["子系统需求", "SSR"]),
    "制动功率": ("功率需求观测", ["牵引", "附件", "总功率需求"]),
    "3 秒": ("3 秒", ["3 秒", "3秒", "3s"]),
    "99.9%": ("99.9%", ["99.9"]),
    "网络会话加密": ("网络会话加密", ["会话加密"]),
    "公有云训练": ("公有云训练", ["公有云"]),
    "互联数据接口": ("互联数据接口", ["互联数据接口", "数据接口"]),
    "中间结果预览": ("中间结果预览", ["结果预览"]),
    "多模态交互": ("多模态交互", ["多模态"]),
    "多源异构数据": ("多源异构数据", ["多源异构"]),
}


def _keys_of(old_anchor):
    return ANCHOR_FIX.get(old_anchor, (old_anchor, [old_anchor]))


def _db():
    con = sqlite3.connect(os.path.join(_ROOT, "mbse.db"))
    con.row_factory = sqlite3.Row
    return con


def _row(con, cid):
    return con.execute(
        "SELECT content, card_data FROM messages WHERE conversation_id=? AND role='assistant' "
        "ORDER BY id LIMIT 1", (cid,)).fetchone()


def _ctx_variants(con, citations):
    """返回 (旧口径ctx, 新口径ctx, 是否发生合并丢内容)。"""
    c800 = "\n".join((x.get("content") or "") for x in citations)
    fulls = []
    merged_loss = False
    for x in citations:
        did, idx = x.get("document_id"), x.get("chunk_index")
        if did is None or idx is None:
            continue
        r = con.execute(
            "SELECT content FROM document_chunks WHERE document_id=? AND chunk_index=?",
            (did, idx)).fetchone()
        if r and r["content"]:
            fulls.append(r["content"])
            # citations 内容明显长于单个 chunk ⇒ 说明是合并过的，回查会丢内容
            if len(x.get("content") or "") > len(r["content"]) + 20:
                merged_loss = True
    old = "\n".join(fulls)
    new = "\n".join([c800] + fulls)
    return old, new, merged_loss


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline",
                    default=os.path.join(_ROOT, "tools", "verify", "baselines",
                                         "inject_k_chat_round1.json"))
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    with open(args.baseline, encoding="utf-8") as f:
        base = json.load(f)
    ks = [str(k) for k in base.get("ks") or []]

    print("=" * 84)
    print("用已有真实对话重算判定（修正上下文口径，不重跑 LLM）")
    print("=" * 84)
    print("基线文件：%s" % os.path.basename(args.baseline))

    con = _db()
    recalc = {}
    merged_count = 0
    changed = []
    try:
        for k in ks:
            per = []
            for it in base["results"].get(k) or []:
                if it.get("err"):
                    per.append(dict(it))
                    continue
                cid, anchor = it.get("conv_id"), it.get("anchor")
                row = _row(con, cid)
                if not row:
                    per.append(dict(it, err="会话缺失"))
                    continue
                ans = row["content"] or ""
                cit = json.loads(row["card_data"] or "{}").get("citations") or []
                old_ctx, new_ctx, ml = _ctx_variants(con, cit)
                if ml:
                    merged_count += 1
                ctx_anchor, answer_keys = _keys_of(anchor)
                # 答案判定：宽松（任一关键词出现即算答对）
                has = any(k.lower() in ans.lower() for k in answer_keys)
                ctx_has = ctx_anchor.lower() in new_ctx.lower()
                st_new = ("grounded" if (has and ctx_has)
                          else ("ungrounded" if has else "missed"))
                # 旧判定（错口径，仅用于展示"修正改了什么"）
                has_old = anchor.lower() in ans.lower()
                st_old = ("grounded" if (has_old and anchor.lower() in old_ctx.lower())
                          else ("ungrounded" if has_old else "missed"))
                if st_old != st_new:
                    changed.append((k, it.get("q"), anchor, st_old, st_new))
                d = dict(it)
                d.update({"state": st_new, "state_old": st_old,
                          "ctx_anchor": ctx_anchor,
                          "is_refusal": bool(REFUSAL_PAT.search(ans)),
                          "merged_content": ml})
                per.append(d)
            recalc[k] = per
    finally:
        con.close()

    # ── 判定逻辑修正影响的条目 ──
    print("\n[1] 判定口径修正的影响（旧=只回查原文 / 新=并集）")
    if changed:
        for k, q, a, so, sn in changed:
            print("  inject_k=%-2s %-30s 锚点=%-14s %s → %s" % (k, (q or "")[:30], a, so, sn))
    else:
        print("  无变化")
    print("  （含近邻合并、回查会丢内容的条目：%d 条）" % merged_count)

    # ── 对比表 ──
    print("\n" + "=" * 84)
    print("[2] 修正后的对比（g=有依据 / u=⚠无依据 / m=答错）")
    print("=" * 84)
    w = 34
    print("%-*s %-20s %-20s" % (w, "问题", "inject_k=%s" % ks[0], "inject_k=%s" % ks[-1]))
    print("-" * 84)
    anchors_order = []
    for it in recalc.get(ks[0]) or []:
        if it.get("anchor") not in anchors_order:
            anchors_order.append(it.get("anchor"))
    for a in anchors_order:
        cells = []
        q = ""
        for k in ks:
            lst = [x for x in (recalc.get(k) or []) if x.get("anchor") == a]
            if not lst:
                cells.append("—")
                continue
            q = q or (lst[0].get("q") or "")
            st = [x.get("state") for x in lst if not x.get("err")]
            cells.append("g%d/u%d/m%d" % (st.count("grounded"), st.count("ungrounded"),
                                          st.count("missed")))
        mark = "  ← 有差异" if cells[0] != cells[-1] else ""
        print("%-*s %-20s %-20s%s" % (w, q[:w], cells[0], cells[-1], mark))

    # ── 汇总 ──
    print("\n" + "-" * 84)
    print("[3] 汇总")
    summ = {}
    for k in ks:
        lst = [x for x in (recalc.get(k) or []) if not x.get("err")]
        n = len(lst)
        g = sum(1 for x in lst if x.get("state") == "grounded")
        u = sum(1 for x in lst if x.get("state") == "ungrounded")
        m = sum(1 for x in lst if x.get("state") == "missed")
        summ[k] = {"n": n, "grounded": g, "ungrounded": u, "missed": m}
        print("  inject_k=%-2s 有效 %-2d 例：有依据 %d / ⚠无依据 %d / 答错 %d"
              % (k, n, g, u, m))

    # ── 逐条根因（哪些是"召回失败"，不是 inject_k 能解决的）──
    print("\n" + "-" * 84)
    print("[4] 非 grounded 的逐条根因")
    for a in anchors_order:
        bads = []
        for k in ks:
            for x in (recalc.get(k) or []):
                if x.get("anchor") == a and x.get("state") != "grounded":
                    bads.append((k, x.get("state"), x.get("merged_content")))
        if not bads:
            continue
        q = next((x.get("q") for x in (recalc.get(k) for k in ks) for x in (x or [])
                  if x.get("anchor") == a), "")
        all_bad = len(bads) == len(ks)
        print("  · %-32s %s" % ((q or "")[:32], "两档均未命中 ⇒ 召回问题，调 inject_k 无效"
                                if all_bad else "仅低档未命中 ⇒ inject_k 相关"))
        for k, st, ml in bads:
            print("      inject_k=%-2s %s" % (k, st))

    # ── 落盘 ──
    out = args.out or os.path.join(_ROOT, "tools", "verify", "baselines",
                                   "inject_k_chat_round1_recalc.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"source": os.path.basename(args.baseline),
                   "ks": ks, "summary": summ,
                   "judgment_changed": [{"k": k, "q": q, "anchor": a,
                                         "old": so, "new": sn} for k, q, a, so, sn in changed],
                   "merged_affected": merged_count,
                   "results": recalc}, f, ensure_ascii=False, indent=2)
    print("\n已写：%s" % out)
    print("=" * 84)


if __name__ == "__main__":
    main()
