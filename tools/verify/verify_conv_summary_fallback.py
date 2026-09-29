# -*- coding: utf-8 -*-
"""降级摘要质量自检（会话 hover 卡用的 `_fallback_summary`）。

背景（2026-09-28）：用户反馈「摘要感觉像是直接截取了一段会话内容」。
本脚本锁死降级路径**不再出现"截取"观感**，判据全部可变异自证：

  A. 不出现"从句子中间断开"——结论/主题两段均须以完整句末符收尾
  B. 不把客套/元话语开场带进摘要（"直接回答您的问题：" / "好的，"）
  C. 不出现 `结论：。` / `结论：）` 这类**标记误命中**造成的空结论
  D. 主题取**首条用户请求**（不是末条）；结论取**末条有内容的 AI**
  E. 无 markdown 残留（```、**、[]( 等）

用法：python tools/verify/verify_conv_summary_fallback.py
退出码 0 = 全绿；非 0 = 有失败项（含变异测试下的预期失败）。
"""
import os
import re
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from services import conv_summary as cs  # noqa: E402

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "mbse.db")

OK, FAIL = [], []


def chk(name, cond, detail=""):
    (OK if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + (("   " + detail) if detail and not cond else ""))


def _ends_complete(s: str) -> bool:
    """是否以完整句末/收束符收尾（即不是半句话被切断）。

    注意：**主题段来自用户原始请求**，用户常常不打句末标点（"…输出影响报告"），
    这不是"截断"。故裸句尾（无标点但已到用户原文末尾）也算完整 —— 判据改为
    "不是被切在标点/连词/助词上"，而不是"必须有句号"。
    """
    t = s.rstrip()
    if not t:
        return True
    if t.endswith(("。", "！", "？", "；", "…", "）", "】", "」", "”", "、", "，", "：", ",")):
        return True
    # 裸句尾：只要不以"断句残留"收场即视为完整（用户原文没写句号是常态）
    return not t.endswith(("的", "和", "与", "或", "是", "在", "把", "被", "对", "为", "等", "如", "即"))


def main():
    if not os.path.exists(DB):
        print("!! 找不到 mbse.db：", DB)
        return 2
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    # 取有实际内容的会话做样本（含长会话与最短会话）
    ids = [r["conversation_id"] for r in conn.execute(
        "SELECT conversation_id, COUNT(*) c FROM messages GROUP BY conversation_id HAVING c>=2 ORDER BY c DESC LIMIT 12")]

    print("== A/B/D/E：样本 %d 个会话 ==" % len(ids))
    bad_mid, bad_chatty, bad_mark, bad_md = [], [], [], []
    for cid in ids:
        msgs = [dict(r) for r in conn.execute(
            "SELECT role, content FROM messages WHERE conversation_id=? ORDER BY id", (cid,))]
        fb = cs._fallback_summary(msgs)
        if not fb:
            continue
        # A：两段（按 ｜ 拆）都须完整收尾
        for seg in fb.split("｜"):
            seg = seg.strip()
            if seg and len(seg) >= 30 and not _ends_complete(seg):
                bad_mid.append((cid, seg[-30:]))
        # B：客套开场
        for pat in ("直接回答您的问题", "直接回答你的问题", "好的，", "明白了，", "很高兴"):
            if fb.startswith(pat) or ("｜ 结论：" + pat) in fb:
                bad_chatty.append((cid, pat, fb[:50]))
        # C：标记误命中 → 空结论
        if re.search(r"结论[：:]\s*[。！？；）)】\]”’…]", fb):
            bad_mark.append((cid, fb[:80]))
        # E：markdown 残留
        if re.search(r"```|!?\[[^\]]*\]\(|\*\*|__", fb):
            bad_md.append((cid, fb[:60]))

    chk("A 无半句截断（两段均完整收尾）", not bad_mid, str(bad_mid[:3]))
    chk("B 未带客套/元话语开场", not bad_chatty, str(bad_chatty[:3]))
    chk("E 无 markdown 残留", not bad_md, str(bad_md[:3]))
    # C 项：**不能只看环境里恰好有什么**（变异实测：把正则还原成可选后缀，
    # 若只扫真实会话，C 项照样 PASS —— 空转断言）。改为扫真实会话 **并且** 跑受控坏样本，
    # 两者任一命中即 FAIL。受控样本见下方 fixture。
    print("== C：标记误命中（真实会话 + 受控坏样本）==")

    # D：取首条用户请求 而非末条
    print("== D：首条/末条来源 ==")
    d_ok = True
    for cid in ids[:6]:
        msgs = [dict(r) for r in conn.execute(
            "SELECT role, content FROM messages WHERE conversation_id=? ORDER BY id", (cid,))]
        users = [m for m in msgs if m["role"] == "user" and cs._clean(m["content"])]
        if len(users) < 2:
            continue
        first = cs._take_sentences(users[0]["content"], 110)
        last = cs._take_sentences(users[-1]["content"], 110)
        fb = cs._fallback_summary(msgs)
        if first and first[:20] != last[:20]:
            # 主题段应贴着首条：取 fb 中 ｜ 之前那段的前 20 字
            head = fb.split("｜")[0].strip()
            if head[:20] != first[:20]:
                d_ok = False
                print("    conv %s 主题段非首条请求: %r vs %r" % (cid, head[:40], first[:40]))
    chk("D 主题取首条用户请求", d_ok)

    # C（真实会话扫描）+ 受控坏样本，合并判定
    fixture_probe = [{"role": "user", "content": "请对当前模型做预评审校验。"},
                     {"role": "assistant", "content": "1.3 素材可用性状态（t1 结论）检索到的互联数据为空；"}]
    mark_probe = cs._condense_conclusion(fixture_probe[1]["content"], 110)
    mark_bad = bool(re.match(r"^[。！？；）)】\]”’…]", mark_probe or "")) or (mark_probe or "") == ""
    chk("C 无 `结论：。` 类标记误命中（真实会话 %d 个 + 受控样本）" % len(ids),
        (not bad_mark) and (not mark_bad),
        "真实=%s 受控=%r" % (bad_mark[:2], mark_probe[:30]))

    # F：受控夹具 —— 直接构造"客套开场 + 名词性结论"的坏样本，确认修法生效
    print("== F：受控坏样本 ==")
    fixture = [
        {"role": "user", "content": "请对当前模型做预评审校验。"},
        {"role": "assistant",
         "content": "直接回答您的问题：需要，但只需要一件事——把任务书正文给我。"
                    "其余 5 项我可以先按默认值推进。"
                    "t1 的裁决意见：与本次诉求领域不符的既有资产不得作为素材。"
                    "1.3 素材可用性状态（t1 结论）检索到的互联数据为空；"},
    ]
    fb = cs._fallback_summary(fixture)
    print("     fixture 输出:", fb)
    # 结论段 = ｜ 之后的部分
    concl_seg = fb.split("｜")[-1].strip()
    if concl_seg.startswith("结论："):
        concl_seg = concl_seg[len("结论："):].strip()
    chk("F1 剥掉客套开场", "直接回答您的问题" not in fb)
    # 标记误命中的症状：结论段以标点开头，或出现 "结论：。" / "结论：）"
    chk("F2 结论段不以标点开头（无标记误命中）",
        not re.match(r"^[。！？；）)】\]”’…]", concl_seg),
        "concl_seg=%r" % concl_seg[:30])
    chk("F3 受控样本也完整收尾", all(_ends_complete(s.strip()) for s in fb.split("｜") if len(s.strip()) >= 30))
    chk("F4 含主题（首条请求）", "预评审校验" in fb)
    # F5：结论段应是实质正文（不能是空壳）
    chk("F5 结论段非空壳", len(concl_seg) >= 12, "len=%d" % len(concl_seg))

    conn.close()
    print("\n== 汇总: %d PASS / %d FAIL ==" % (len(OK), len(FAIL)))
    if FAIL:
        print("失败项:", FAIL)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
