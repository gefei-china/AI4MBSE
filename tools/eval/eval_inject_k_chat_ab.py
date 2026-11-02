# -*- coding: utf-8 -*-
"""对话级 A/B：`rag.inject_k` 3 vs 5 对**最终回答**的影响（真实 HTTP 链路）。

## 为什么需要这一步（而不是只看脚本级指票）

`tools/eval/eval_inject_k_ab.py` 测的是 `anchor_hit@k` —— 答案 chunk **有没有进上下文**。
那是中间指标，它成立**不等于**最终回答就变好，中间至少还有三处会失效：

1. 答案进了上下文，但模型没读/没引用（Lost-in-the-Middle）；
2. 答案进了上下文，但模型用自己参数知识答成了另一个说法（看似答对却与本改动无关）；
3. **上下文里没有答案，模型照样编出一个像样的回答**（幻觉式引用，脚本测不出来）。

⇒ 只有走**真实 HTTP 对话链路**（意图识别 → 检索 → 组装 prompt → LLM 生成 → 引用回填）
看到的回答，才是用户真正拿到的东西。

## 判据：四态（本脚本的核心）

    grounded    答对 + 注入上下文里确实有依据        → 真答对
    ungrounded  答对 + 注入上下文里**没有**依据      → 幻觉式引用，**最危险**
    missed      答错 / 拒答
    (err)       链路或配置错误

历史教训：只看"答对/答错"时，`inject_k=3` 看起来没有任何损失（甚至更快），
实际上模型是在没有证据的情况下自己补出来的，还标了引用编号 —— 用户点进去文不对题。

## 两个测量缺陷（已修，都曾造成过误判）

1. **引用内容被截断导致的假 ungrounded**：`_citations_payload` 对每条 content 只给
   前 800 字，而 prompt 注入上限是 `rag.chunk_content_chars`（默认 1200）——
   若锚点落在 800~1200 字之间，会**误判成"上下文里没有"**。
   修法：按 `document_id + chunk_index` 回查 `document_chunks` 取**全文**来判锚点。
2. **配置副作用**：切档必须走 `PUT /api/system/config/static`（这是唯一能让运行中服务
   真正改值的路径），但它会**写 `~/.workbuddy/mbse_config.json`**。
   一旦写进去，之后改代码默认值就不生效（本项目记过同类事故）。
   修法：实验前**按字节备份该文件**，结束时**原样写回**（不是"把值改回去"——
   值改回去仍会留下 `inject_k` 键，效果不等价）。

## 实验设计

- 每条用例两轮：`inject_k=3` 与 `inject_k=5`，**各自新建独立会话**
  （复用会话会让历史消息进上下文，两轮起点不一致就不是公平对比）。
- 唯一变量 `inject_k`；检索、模型、意图都走同一条链路。
- 用例集：17 条「单答案」（答案能由一个 chunk 完整回答），
  锚点是该章节的**独有短语**，脚本启动时**自动校验锚点只出现在 1~3 个 chunk**，
  不合格立即 `exit 1`（不静默跳过 —— 静默跳过会让结论假绿）。

用法：
  ./.venv/Scripts/python.exe -X utf8 tools/eval/eval_inject_k_chat_ab.py
  ./.venv/Scripts/python.exe -X utf8 tools/eval/eval_inject_k_chat_ab.py --ks 3,5 --rounds 1
  ./.venv/Scripts/python.exe -X utf8 tools/eval/eval_inject_k_chat_ab.py --only-abnormal
"""
import os
import re
import sys
import json
import time
import sqlite3
import argparse

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)

import httpx  # noqa: E402

BASE = "http://127.0.0.1:8000"
TIMEOUT = 300.0
CFG_PATH = os.path.join(os.path.expanduser("~"), ".workbuddy", "mbse_config.json")

# ── 「单答案」用例集（问题, 上下文锚点, 期望文档关键词, 答案关键词列表）──────
# 两个判据**刻意分开**（踩过坑，见 recalc 脚本的 ANCHOR_FIX 注释）：
#   · ctx_anchor    判"依据在不在上下文" → 必须用该章节**原文短语**（严格）
#   · answer_keys   判"答案对不对"     → 必须**宽松**，允许模型改写措辞
# 混用一个字符串会把"答对但改了说法"误判成失败：实测 4/17 条中招，
# 例如锚点 `整车需求VR为输出`，模型答"以**整车黑盒需求（VR）**为输出"。
CASES = [
    # 技术要求书（847/848，55 chunk，一章一块）
    ("技术要求书里轻量级查询的响应时间要求是多少", "3 秒", "Print_面向", ["3 秒", "3秒", "3s"]),
    ("技术要求书对可靠性要求中，出错时应该怎么做", "平滑降级", "Print_面向", ["平滑降级"]),
    ("技术要求书要求的全年可用性是多少", "99.9%", "Print_面向", ["99.9"]),
    ("技术要求书对网络传输数据的安全要求是什么", "网络会话加密", "Print_面向", ["会话加密"]),
    ("自主可控要求对组件部署有什么限制", "公有服务器", "Print_面向", ["公有服务器"]),
    ("运行环境要求支持哪些国产操作系统", "麒麟", "Print_面向", ["麒麟"]),
    ("设计与实施约束对训练数据有什么要求", "公有云训练", "Print_面向", ["公有云"]),
    ("工具的系统架构应包含哪些接口", "互联数据接口", "Print_面向", ["互联数据接口", "数据接口"]),
    ("验收交付要求交付哪些文档", "源代码使用说明", "Print_面向", ["源代码使用说明"]),
    ("人在回路要求对 AI 生成结果提供什么", "中间结果预览", "Print_面向", ["结果预览"]),
    ("模型预评审与校验要求生成什么材料", "预评审报告", "Print_面向", ["预评审报告"]),
    ("交互界面要求支持哪些交互形式", "多模态交互", "Print_面向", ["多模态"]),
    ("设计数据管理要从哪些数据源提取数据", "多源异构数据", "Print_面向", ["多源异构"]),
    # 广汽方法论（812）：流程链路（模型会改写措辞，答案关键词放宽）
    ("整车需求分析以什么为输入、什么为输出", "整车需求VR为输出", "广汽方法论",
     ["整车黑盒需求", "VR"]),
    ("子系统架构设计以什么为输入", "子系统需求SSR为输入", "广汽方法论",
     ["子系统需求", "SSR"]),
    # 场景知识库（跨文档，检验改动不止对单一文档有效）
    ("座舱空气调节仿真需要考虑哪些负荷", "显热负荷", "座舱空气调节", ["显热负荷"]),
    ("混合动力汽车动力经济性仿真关注哪些功率指标", "功率需求观测", "混合动力汽车",
     ["牵引", "附件", "总功率需求"]),
]

REFUSAL_PAT = re.compile(
    r"(没有(?:相关|找到|检索到|提及)|未(?:找到|提及|检索到|包含)|不存在(?:相关)?|"
    r"知识库中(?:并)?(?:没有|未)|无相关(?:内容|章节|要求)|无法(?:从|在)知识库)")


def _db():
    con = sqlite3.connect(os.path.join(_ROOT, "mbse.db"))
    con.row_factory = sqlite3.Row
    return con


def validate_anchors():
    """校验锚点"单答案"性：必须存在，且只出现在 1~3 个 chunk。不合格即退出。"""
    con = _db()
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT document_id, source_doc, chunk_index, content FROM document_chunks")]
    finally:
        con.close()
    bad, ok = [], []
    print("[0] 锚点校验（必须存在，且只出现在 1~3 个 chunk）")
    for q, anchor, doc, answer_keys in CASES:
        al = anchor.lower()
        n = sum(1 for r in rows if al in (r.get("content") or "").lower())
        if n == 0:
            bad.append((q, anchor, "锚点在库中不存在"))
        elif n > 3:
            bad.append((q, anchor, "出现在 %d 个 chunk（>3，非单答案）" % n))
        else:
            ok.append((q, anchor, doc, answer_keys))
            print("  [OK  ] %-40s 锚点=%-16s chunks=%d" % (q[:40], anchor, n))
    for q, anchor, why in bad:
        print("  [FAIL] %-40s 锚点=%-16s %s" % (q[:40], anchor, why))
    if bad:
        print("\n❌ %d 条用例锚点不合格 —— 先修用例，不能带着坏用例出结论" % len(bad))
        sys.exit(1)
    print("  合格用例：%d 条\n" % len(ok))
    return ok


def _client():
    # 本机有 HTTP_PROXY（见项目约定），必须 trust_env=False 否则请求会被代理拦掉
    return httpx.Client(base_url=BASE, timeout=TIMEOUT, trust_env=False)


def _set_inject_k(c, value):
    r = c.put("/api/system/config/static", json={"updates": {"rag.inject_k": int(value)}})
    r.raise_for_status()
    j = r.json()
    if not j.get("ok"):
        raise RuntimeError("配置写入失败: %s" % j)
    cur = c.get("/api/system/config/schema").json()
    got = cur.get("schema", {}).get("rag", {}).get("inject_k", {}).get("current")
    if int(got) != int(value):
        raise RuntimeError("配置回读不一致：写入 %s，实际 %s" % (value, got))
    return int(got)


def _new_conv(c, title):
    r = c.post("/api/conversations", json={"title": title, "intent": "knowledge_qa"})
    r.raise_for_status()
    return int(r.json()["id"])


def _full_ctx(citations):
    """构造"模型实际看到的上下文"，用于判断锚点是否真的给到了模型。

    ## 为什么不能只用 citations 的 content
    `_citations_payload` 对每条 content 只给**前 800 字**，
    而 prompt 注入上限是 `rag.chunk_content_chars`（默认 1200）
    ⇒ 锚点若落在 800~1200 字之间会被误判成"上下文里没有"。

    ## 为什么也不能只用"按 chunk_index 回查原文"（我踩过的坑，必须写下来）
    检索的**近邻合并**会把多个相邻 chunk 拼成一条，但 `chunk_index` 只保留**第一个**。
    实测 `conv=9954` 的 `[1]`：citations 里 content 有 **380 字**（§45+§47+§49 合并），
    而按 `(847, 48)` 回查 `document_chunks` 只拿到 **52 字** —— **合并进去的其它段落全丢了**。
    我第一版就只用回查，结果把 5 档本该 grounded 的一条判成了 ungrounded，
    得出"两档没差别"的错误结论（差一步就去纠正前面的结论了）。

    ## 正确做法：**并集**
    citations 给的是"合并后的完整内容的前 800 字"，回查给的是"单个原始 chunk 全文"，
    两者的并集才是稳妥的下界。唯一剩下的盲区是"合并内容 800~1200 字"那一段，
    只能改 `_citations_payload` 才拿得到，但那会影响前端展示，不应为测试改产品。
    """
    if not citations:
        return ""
    con = _db()
    try:
        parts = []
        for x in citations:
            parts.append(x.get("content") or "")
            did, idx = x.get("document_id"), x.get("chunk_index")
            if did is not None and idx is not None:
                row = con.execute(
                    "SELECT content FROM document_chunks WHERE document_id=? AND chunk_index=?",
                    (did, idx)).fetchone()
                if row and row["content"]:
                    parts.append(row["content"])
        return "\n".join(p for p in parts if p)
    finally:
        con.close()


def _ask(c, conv_id, question):
    t0 = time.time()
    r = c.post("/api/conversations/%d/chat" % conv_id,
               json={"message": question, "intent_override": "knowledge_qa"})
    r.raise_for_status()
    d = r.json()
    cites = (d.get("retrieval") or {}).get("citations") or []
    return {
        "answer": d.get("content") or "",
        "citations": len(cites),
        "ctx_text": _full_ctx(cites),
        "citations_raw": cites,
        "intent": d.get("intent") or "",
        "needs_clarify": bool(d.get("needs_clarification")),
        "elapsed": round(time.time() - t0, 1),
    }


def _classify(answer, anchor, ctx_text, answer_keys=None):
    """四态判定。答案侧用**宽松关键词**（模型必改写措辞），依据侧用**原文短语**。"""
    keys = answer_keys or [anchor]
    has = any(k.lower() in (answer or "").lower() for k in keys)
    in_ctx = anchor.lower() in (ctx_text or "").lower()
    if has and in_ctx:
        return "grounded"
    if has and not in_ctx:
        return "ungrounded"
    return "missed"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ks", default="3,5")
    ap.add_argument("--rounds", type=int, default=1)
    ap.add_argument("--tag", default="")
    args = ap.parse_args()
    ks = [int(x) for x in args.ks.split(",") if x.strip()]

    print("=" * 84)
    print("对话级 A/B：rag.inject_k 对最终回答的影响（真实 HTTP 链路）")
    print("=" * 84)

    valid = validate_anchors()

    with _client() as c:
        orig = int(c.get("/api/system/config/schema").json()["schema"]["rag"]["inject_k"]["current"])
        # ★ 按字节备份配置文件：结束后**原样写回**，而不是"把值改回去"。
        #   值改回去会在文件里留下 inject_k 键 ⇒ 以后改代码默认值就不生效。
        _cfg_bytes = None
        if os.path.exists(CFG_PATH):
            with open(CFG_PATH, "rb") as f:
                _cfg_bytes = f.read()
        print("原始 inject_k = %d；配置文件已按字节备份（%s）"
              % (orig, "有" if _cfg_bytes is not None else "无"))

        results = {}
        try:
            for k in ks:
                print("\n" + "-" * 84)
                print("【inject_k = %d】" % k)
                got = _set_inject_k(c, k)
                print("  配置已生效并回读确认：inject_k=%d" % got)
                per = []
                for q, anchor, doc, answer_keys in valid:
                    for _rd in range(args.rounds):
                        title = "__ab_inject_k__%d_%s" % (k, anchor[:8])
                        try:
                            cid = _new_conv(c, title)
                            r = _ask(c, cid, q)
                        except Exception as e:  # noqa: BLE001
                            print("  [ERR ] %-30s → %s" % (q[:30], str(e)[:70]))
                            per.append({"q": q, "anchor": anchor, "err": str(e)[:200]})
                            continue
                        state = _classify(r["answer"], anchor, r["ctx_text"], answer_keys)
                        item = {
                            "q": q, "anchor": anchor, "conv_id": cid,
                            "answer_keys": answer_keys,
                            "state": state,
                            "answer_has_anchor": anchor.lower() in r["answer"].lower(),
                            "is_refusal": bool(REFUSAL_PAT.search(r["answer"])),
                            "anchor_in_ctx": state != "ungrounded" and (
                                anchor.lower() in (r["ctx_text"] or "").lower()),
                            "citations": r["citations"],
                            "needs_clarify": r["needs_clarify"],
                            "elapsed": r["elapsed"],
                            "answer_head": r["answer"][:200].replace("\n", " "),
                        }
                        per.append(item)
                        mark = {"grounded": "有依据", "ungrounded": "⚠无依据",
                                "missed": "答错 "}[state]
                        print("  [%s] %-30s 引用=%d 耗时=%3.0fs"
                              % (mark, q[:30], r["citations"], r["elapsed"]))
                results[k] = per
        finally:
            back = _set_inject_k(c, orig)
            print("\n已恢复 inject_k = %d（回读确认）" % back)
            if _cfg_bytes is not None:
                with open(CFG_PATH, "wb") as f:
                    f.write(_cfg_bytes)
                print("配置文件已按字节写回（inject_k 键不会残留在覆盖文件里）")

    # ── 对比表 ──
    print("\n" + "=" * 84)
    print("对比（同一问题、独立会话、唯一变量 inject_k）")
    print("=" * 84)
    w = 34
    print("%-*s %-22s %-22s" % (w, "问题", "inject_k=%d" % ks[0], "inject_k=%d" % ks[-1]))
    print("-" * 84)
    diffs = []
    for q, anchor, doc, answer_keys in valid:
        cells = []
        for k in ks:
            lst = [x for x in (results.get(k) or []) if x.get("anchor") == anchor]
            if not lst:
                cells.append("—")
                continue
            st = [x.get("state") for x in lst]
            cells.append("g%d/u%d/m%d" % (st.count("grounded"),
                                          st.count("ungrounded"), st.count("missed")))
        if cells[0] != cells[-1]:
            diffs.append((q, anchor, cells))
        print("%-*s %-22s %-22s%s" % (w, q[:w], cells[0], cells[-1],
                                      "  ← 有差异" if cells[0] != cells[-1] else ""))

    # ── 汇总 ──
    print("\n" + "-" * 84)
    print("汇总（g=有依据答对 / u=⚠无依据答对 / m=答错；★ 关键指标是 u）")
    summary = {}
    for k in ks:
        lst = [x for x in (results.get(k) or []) if not x.get("err")]
        n = len(lst)
        if not n:
            continue
        g = sum(1 for x in lst if x.get("state") == "grounded")
        u = sum(1 for x in lst if x.get("state") == "ungrounded")
        m = sum(1 for x in lst if x.get("state") == "missed")
        err = len([x for x in (results.get(k) or []) if x.get("err")])
        summary[k] = {"n": n, "grounded": g, "ungrounded": u, "missed": m, "err": err}
        print("  inject_k=%-2d 有效 %-2d 例：有依据 %d / ⚠无依据 %d / 答错 %d%s"
              % (k, n, g, u, m, ("  (链路错误 %d)" % err) if err else ""))

    # ── 结论 ──
    print("\n" + "-" * 84)
    print("判读")
    lo, hi = ks[0], ks[-1]
    if lo in summary and hi in summary:
        u_lo, u_hi = summary[lo]["ungrounded"], summary[hi]["ungrounded"]
        g_lo, g_hi = summary[lo]["grounded"], summary[hi]["grounded"]
        print("  · 无依据答对（幻觉式引用）：%d 档 %d 例 → %d 档 %d 例"
              % (lo, u_lo, hi, u_hi))
        print("  · 有依据答对：%d 档 %d/%d → %d 档 %d/%d"
              % (lo, g_lo, summary[lo]["n"], hi, g_hi, summary[hi]["n"]))
        if u_lo > u_hi:
            print("  · 结论倾向：提高 inject_k 消除/减少了「答对但无依据」，方向成立")
        elif u_lo == u_hi == 0:
            print("  · 结论倾向：两档均无幻觉式引用，本用例集未复现 3 档缺陷")
        else:
            print("  · 结论倾向：未观察到提高 inject_k 的收益，需扩样本或换用例")
    if diffs:
        print("  · 有差异的用例 %d 条（见上表 ← 标记）" % len(diffs))
    else:
        print("  · 本批次所有用例两档表现一致 —— 不能据此宣称 inject_k 无影响，"
              "只说明这批用例不敏感")

    if args.tag:
        out = os.path.join(_ROOT, "tools", "verify", "baselines",
                           "inject_k_chat_%s.json" % args.tag)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            json.dump({"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                       "ks": ks, "rounds": args.rounds,
                       "summary": {str(k): v for k, v in summary.items()},
                       "diffs": [{"q": q, "anchor": a, "cells": c} for q, a, c in diffs],
                       "results": {str(k): v for k, v in results.items()}},
                      f, ensure_ascii=False, indent=2)
        print("\n已写：%s" % out)
    print("=" * 84)


if __name__ == "__main__":
    main()
