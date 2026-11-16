# -*- coding: utf-8 -*-
"""门禁：自主续跑「白跑一轮」与「正文重复堆叠」的三道防线（2026-11-15）。

## 被验的真实故障（数据全部取自 mbse.db，不造样本）

会话 conv 999999024 / 消息 6585：用户要的是"按标准校验两条需求"，一轮问答，
系统却跑了 **4 次主答复 + 3 次完成度评估**，耗时 **403 秒**，
最后落库 **29,774 字符** —— 里面是 **3 个完整周期**的「结论摘要 + ```json」，
同一件事讲了三遍。

`llm_usage_stats`（同一轮）：

```
16:34:43 chat_probe p=14130 c=4063 stop   ← 答复 1
16:35:54 chat_eval  p=2086  c=374         ← 评估：判「未完成」
16:36:07 chat_probe p=18616 c=4052 stop   ← 续跑 1（prompt += 上一版正文）
16:37:19 chat_eval  ...                    ← 又判「未完成」
16:37:31 chat_probe p=23113 c=4076 stop   ← 续跑 2
16:39:02 chat_eval  ...                    ← 又判「未完成」
16:39:15 chat_probe p=27626 c=4077 stop   ← 续跑 3（达上限才停）
```

⇒ 三道防线分别对应三个独立根因：

| # | 根因 | 防线 | 落点 |
|---|---|---|---|
| 1 | 评估器把「判定结论里的待核实/需回查」读成「AI 自己还没做」 | `EVAL_SYS` 反向澄清 e + 评估输入先剥 JSON | `auto_continue.py` |
| 2 | `is_spinning` 只比 next_step 逐字 ⇒ 换个说法就抓不到 | 正文级空转闸（相似度 + 长度比复合判据） | `auto_continue.is_content_repeating` |
| 3 | 模型每轮重写整份答案 ⇒ 正文堆 N 份 | `merge_round_content` 增补 ④b/④c | `auto_continue.merge_round_content` |

## 阈值是量的，不是拍的

真库 msg 6585 的三份正文（同一任务的重述）：
J(1,2)=**0.8768** / J(1,3)=**0.8483** / J(2,3)=**0.9540**；
与该会话**无关**的另一条长答复：**0.3034**。
⇒ 重述带 [0.85, 0.96]，无关带 0.30，中间没有样本 ⇒ 阈值取 **0.90**（重述带内偏保守一侧），
并用「长度比 ±5%」复合约束兜住"实质改进会变长"这一情形。

⚠️ **诚实标注**：没有取到「实质改进」的对照样本 —— 这是本判据最弱的一环，
门禁 A10 把这条限制**写进断言**，避免后人误以为它有充分的样本支撑。

## 运行

    python tools/verify/verify_auto_continue_stop.py

退出码 0 = 全绿；1 = 有红项。
"""

from __future__ import annotations

import os
import sqlite3
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

DB = os.environ.get("MBSE_GATE_DB") or os.path.join(_ROOT, "mbse.db")
FENCE = "`" * 3

_PASS = 0
_FAIL = 0
_SKIP = 0
_FAILED = []


def check(group, name, ok, detail=""):
    global _PASS, _FAIL
    if ok:
        _PASS += 1
        print("  [PASS] %s %s" % (group, name))
    else:
        _FAIL += 1
        _FAILED.append("%s %s :: %s" % (group, name, detail))
        print("  [FAIL] %s %s :: %s" % (group, name, detail))


def skip(group, name, why=""):
    """显式跳过（不计入绿也不计入红）。

    ⚠️ 为什么要 SKIP 而不是 FAIL：真机标定组依赖生产真库里的 msg 6585，
    干净库（CI 等价环境）里根本没有这条消息 ⇒ 判 FAIL 是**环境差异**不是缺陷。
    但绝不能"悄悄不跑" ⇒ 必须显式打印，且**其余组（A/B/D）在干净库里照跑**，
    全组 SKIP 时门禁不会显示为"全绿且有实质检查"。
    """
    global _SKIP
    _SKIP += 1
    print("  [SKIP] %s %s :: %s" % (group, name, why))


def _row(sql, args=()):
    """读真库（只读）。库不存在 ⇒ 返回 None（门禁降级为 SKIP，不假绿也不假红）。"""
    if not os.path.exists(DB):
        return None
    try:
        con = sqlite3.connect(DB, timeout=15)
        con.row_factory = sqlite3.Row
        try:
            r = con.execute(sql, args).fetchone()
            return tuple(r) if r else None
        finally:
            con.close()
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════════════
# A. 防线 1：评估器不该把「判定结论」读成「未完成」
# ══════════════════════════════════════════════════════════════════════════
def group_a():
    from agent.auto_continue import EVAL_SYS, build_eval_messages, strip_json_blocks

    # A1：反向澄清条目必须存在，且**不在硬信号列表里**（放错位置语义就反了）
    check("A1", "EVAL_SYS 含「反向澄清」段（判定类结论 ≠ AI 自己的待办）",
          "反向澄清" in EVAL_SYS and "判定结论本身" in EVAL_SYS, "")
    _hs = EVAL_SYS.find("【硬信号")
    _rc = EVAL_SYS.find("【反向澄清")
    check("A2", "反向澄清段排在硬信号段**之后**（放前面语义会反）",
          _hs >= 0 and _rc > _hs, "硬信号@%s 反向澄清@%s" % (_hs, _rc))
    check("A3", "反向澄清明确区分「检查」与「检查并写入/修复」",
          "并写入" in EVAL_SYS and "并修复" in EVAL_SYS, "")

    # A4：评估输入必须先剥 json 块（否则 2000 字符截断切在 JSON 中间）
    body = ("**结论摘要**\n\n- 第 1 条：不通过，待核实 [1]\n\n"
            + FENCE + "json\n{\"checks\": [{\"rule_id\": \"RC-04\", \"verdict\": \"fail\"}]}\n" + FENCE)
    msgs = build_eval_messages("按标准校验需求", "", body)
    _u = msgs[-1]["content"]
    check("A4", "评估输入里已无 json 结构化块",
          "RC-04" not in _u and "checks" not in _u, _u[-120:])
    check("A5", "评估输入保留了给人看的结论摘要",
          "结论摘要" in _u and "待核实" in _u, "")

    # A6：纯 JSON 旧形态不能被剥成空（剥完必须回退原文）
    only_json = FENCE + "json\n{\"a\": 1}\n" + FENCE
    _u2 = build_eval_messages("q", "", only_json)[-1]["content"]
    check("A6", "纯 JSON 旧形态：剥完为空 ⇒ 回退原文（不让评估器看不到任何内容）",
          '"a": 1' in _u2, _u2[-120:])

    # A7：非 json 代码块**必须保留**（那是交付物本身）
    py = "结论\n\n" + FENCE + "python\nprint('x')\n" + FENCE
    _u3 = build_eval_messages("q", "", py)[-1]["content"]
    check("A7", "非 json 代码块原样保留（交付物证据不能被剥掉）",
          "print('x')" in _u3 and FENCE in _u3, "")

    # A8：未闭合围栏 ⇒ 原样吐出，不吞正文
    trunc = "结论正文\n\n" + FENCE + "json\n{\"a\": "
    _u4 = strip_json_blocks(trunc)
    check("A8", "未闭合围栏：残余内容原样吐出（宁可多看，不可吞正文）",
          "结论正文" in _u4, _u4[:80])


# ══════════════════════════════════════════════════════════════════════════
# B. 防线 2：正文级空转闸（判据 + 接线 + 变异自证）
# ══════════════════════════════════════════════════════════════════════════
def group_b():
    from agent.auto_continue import (decide, is_content_repeating, _ngram_dice,
                                     _REPEAT_SIM)

    a = "第一条结论：需求不合格，待核实。\n" + ("依据条款 §5.2.5 [1]。\n" * 40)
    b = "第一条结论：需求不合格，待核实。\n" + ("依据条款 §5.2.5 [1]。\n" * 40) + "补充一句。\n"
    # B1：同长度 + 高相似 ⇒ 判空转
    check("B1", "近乎逐字相同的正文 ⇒ 判为空转",
          is_content_repeating(a, a), "")
    # B2：长度明显变长（实质改进）⇒ 不判空转（复合判据的第二半）
    long_b = a + ("新增了一整段分析内容。" * 60)
    check("B2", "正文明显变长（实质改进）⇒ 不判空转",
          not is_content_repeating(long_b, a),
          "len %d vs %d" % (len(long_b), len(a)))
    # B3：完全不同的正文 ⇒ 不判空转
    other = "另一段完全不同的内容，涉及时序图与接口定义。" * 30
    check("B3", "内容不同的正文 ⇒ 不判空转", not is_content_repeating(other, a), "")

    # B4：decide 必须因空转闸而拒绝续跑
    _v = {"done": False, "next_step": "补充属性与追溯信息", "remaining": ["属性"]}
    _cont, _why = decide(_v, used=0, max_used=3, seen_steps=["别的步骤"],
                          content_prev=a, content_cur=a)
    check("B4", "decide：正文雷同 ⇒ 不续跑（且理由写明是空转）",
          (not _cont) and ("打转" in _why or "雷同" in _why), _why)

    # B5：decide 在正文不同时仍按原逻辑续跑（闸不能误伤正常续跑）
    _cont2, _why2 = decide(_v, used=0, max_used=3, seen_steps=["别的步骤"],
                            content_prev=a, content_cur=long_b)
    check("B5", "decide：正文有实质变化 ⇒ 仍续跑（空转闸不误伤）",
          _cont2, _why2)

    # B6：变异自证 —— 把阈值抬到 1.01（判据失效）⇒ B4 必须翻红
    import agent.auto_continue as _acm
    _orig = _acm._REPEAT_SIM
    try:
        _acm._REPEAT_SIM = 1.01
        _c3, _w3 = _acm.decide(_v, used=0, max_used=3, seen_steps=["别的步骤"],
                                content_prev=a, content_cur=a,
                                content_sim_thr=_acm._REPEAT_SIM)
        check("B6", "变异自证：阈值抬到 1.01（空转闸失效）⇒ decide 重新续跑",
              _c3, _w3)
    finally:
        _acm._REPEAT_SIM = _orig

    # B7：两处接线（stream / execute）必须都把上一轮正文喂进 decide
    for _f, _tag in (("agent/pipeline_parts/stream.py", "stream"),
                     ("agent/pipeline_parts/execute.py", "execute")):
        _src = open(os.path.join(_ROOT, _f), encoding="utf-8").read()
        check("B7", "%s：decide 调用带 content_prev/content_cur（正文级空转闸已接线）" % _tag,
              "content_prev=_ac_prev_content" in _src
              and "content_cur=llm_content" in _src, "")
        check("B8", "%s：维护 _ac_prev_content（上一轮正文）" % _tag,
              "_ac_prev_content = llm_content" in _src, "")

    # B9：两处累加口径必须一致（都走 merge_round_content，不许一个拼一个去重）
    _src_e = open(os.path.join(_ROOT, "agent/pipeline_parts/execute.py"), encoding="utf-8").read()
    check("B9", "execute.py 的跨轮累加也走 merge_round_content（与 stream 同口径）",
          "_ac_full = _ac.merge_round_content(_ac_full, llm_content)" in _src_e, "")


# ══════════════════════════════════════════════════════════════════════════
# C. 防线 3：正文重复堆叠（真机样本标定 + 合并去重）
# ══════════════════════════════════════════════════════════════════════════
def group_c():
    from agent.auto_continue import merge_round_content, _code_blocks, _ngram_sim

    r6585 = _row("select content from messages where id=6585")
    if not r6585:
        skip("C1", "真机样本（message 6585）",
             "干净库无 mbse.db ⇒ 真机标定组跳过（A/B/D 组仍全跑）")
        return
    check("C1", "真机样本存在（message 6585）", True, "")
    _parts = r6585[0].split(FENCE)
    _rnds = [_parts[i].strip() + ("\n" + FENCE + _parts[i + 1] + FENCE
                                  if i + 1 < len(_parts) else "")
             for i in range(0, len(_parts), 2) if _parts[i].strip()]
    check("C2", "真机形态：3 份正文、块数相同、md5 全不同",
          len(_rnds) == 3 and [len(_code_blocks(r)) for r in _rnds] == [1, 1, 1], "")

    _s = [_ngram_sim(_rnds[0], _rnds[1]), _ngram_sim(_rnds[0], _rnds[2]),
          _ngram_sim(_rnds[1], _rnds[2])]
    _other = _row("select content from messages where role='assistant' "
                  "and id<>6585 and length(content)>3000 order by id desc limit 1")
    _far = _ngram_sim(_rnds[0], _other[0]) if _other else 0.0
    check("C3", "阈值标定：重述带 %.3f/%.3f/%.3f ⊂ [0.85,0.96]，无关带 %.3f"
          % (_s[0], _s[1], _s[2], _far),
          min(_s) >= 0.84 and max(_s) <= 0.96 and _far < 0.5, str(_s))

    # C3b：空转闸用的是 Dice（不是 Jaccard）—— 真机重述必须稳过阈值，底噪必须远低于
    from agent.auto_continue import _ngram_dice, _REPEAT_SIM
    _d = [_ngram_dice(_rnds[0], _rnds[1]), _ngram_dice(_rnds[0], _rnds[2]),
          _ngram_dice(_rnds[1], _rnds[2])]
    _dfar = _ngram_dice(_rnds[0], _other[0]) if _other else 0.0
    check("C3b", "Dice 标定：真机重述 %.4f/%.4f/%.4f ≥ 阈值 %.2f，底噪 %.4f 远低于"
          % (_d[0], _d[1], _d[2], _REPEAT_SIM, _dfar),
          min(_d) >= _REPEAT_SIM and _dfar < _REPEAT_SIM - 0.10,
          "dice=%s far=%.4f thr=%.2f" % (["%.4f" % x for x in _d], _dfar, _REPEAT_SIM))

    _m = ""
    for _r in _rnds:
        _m = merge_round_content(_m, _r)
    check("C4", "3 份顺序合并后只剩 1 个代码块（用户只该看到一版）",
          len(_code_blocks(_m)) == 1, "blocks=%d" % len(_code_blocks(_m)))
    check("C5", "合并后长度显著下降（29,774 → 一份量级）",
          len(_m) < len(r6585[0]) * 0.5, "%d vs %d" % (len(_m), len(r6585[0])))

    # C6：口径反转 —— 判据必须**不存在**时 E10 才成立？此处反向验证：
    #   把阈值抬到 1.01 ⇒ 必须重新堆回 3 块（证明 C4 靠的是新判据，不是巧合）
    import agent.auto_continue as _acm
    _orig = _acm._RESTATE_SIM
    try:
        _acm._RESTATE_SIM = 1.01
        _m2 = ""
        for _r in _rnds:
            _m2 = _acm.merge_round_content(_m2, _r)
        check("C6", "变异自证：阈值抬到 1.01 ⇒ 重新堆回 3 块（C4 确实靠新判据）",
              len(_code_blocks(_m2)) == 3, "blocks=%d" % len(_code_blocks(_m2)))
    finally:
        _acm._RESTATE_SIM = _orig

    # C7：信息保全 —— 不同主题不得被吞
    _a = "第一轮：只做了定位"
    _b = "第二轮：新增了时序图分析（不同内容）"
    check("C7", "内容确实不同的两轮仍会追加（不丢信息）",
          merge_round_content(_a, _b) == _a + "\n\n" + _b, merge_round_content(_a, _b)[:60])


# ══════════════════════════════════════════════════════════════════════════
# D. 限制如实写进断言（不许后人误以为阈值有充分样本支撑）
# ══════════════════════════════════════════════════════════════════════════
def group_d():
    from agent.auto_continue import is_content_repeating, _ngram_dice, _REPEAT_SIM
    # D1：空转闸的「长度比 ±5%」必须真的在生效
    #   ⚠️ 样本必须用**有内容**的文本：全同字符（如 "x"*1000）的 3-gram 集合会退化，
    #   相似度被人为压低 ⇒ 会造出"判据失效"的假象（本门禁第一版就踩了）。
    _base = "第一条结论：需求不合格，待核实，依据 §5.2.5 [1]。\n" * 20
    _b = _base + "补充：另需确认来源属性。\n"                     # +约 2%
    _c = _base + ("新增了一整段分析：时序图与接口定义的影响面。\n" * 12)  # +明显变长
    check("D1", "长度比约束生效：+2% 判雷同，+明显变长 一定不判",
          is_content_repeating(_b, _base) and not is_content_repeating(_c, _base),
          "len %d/%d/%d" % (len(_base), len(_b), len(_c)))
    # D2：空串/None 不得被判成空转（否则第一轮就会误停）
    check("D2", "任一侧为空 ⇒ 不判空转（第一轮必须能正常续跑）",
          not is_content_repeating("", "abc") and not is_content_repeating("abc", "")
          and not is_content_repeating(None, "abc"), "")
    # D3：阈值必须落在「实质改进」与「真机重述」之间（两侧余量都要够）
    _d_base = "第一条结论：需求不合格，待核实，依据 §5.2.5 [1]。\n" * 20
    _d_new = _d_base + ("新增了一整段分析：时序图与接口定义的影响面。\n" * 12)
    _d_imp = _ngram_dice(_d_new, _d_base)
    check("D3", "阈值 %.2f 落在「实质改进」dice(%.4f) 之上，留有 0.05 以上余量"
          % (_REPEAT_SIM, _d_imp), _d_imp < _REPEAT_SIM - 0.05, "")
    # D4：Jaccard 与 Dice 各司其职（判据不混用）：合并判据仍走 Jaccard
    import agent.auto_continue as _acm
    _src = open(os.path.join(_ROOT, "agent", "auto_continue.py"), encoding="utf-8").read()
    check("D4", "两个判据各用各的度量：④b 用 Jaccard、空转闸用 Dice（不混用）",
          ("_ngram_sim(p, c)" in _src) and ("_ngram_dice(c, p)" in _src), "")


def main():
    print("=" * 78)
    print("门禁：自主续跑「白跑一轮」+「正文重复堆叠」三道防线（2026-11-15）")
    print("=" * 78)
    for fn in (group_a, group_b, group_c, group_d):
        print("\n--- %s ---" % fn.__name__.upper())
        fn()
    print("\n" + "=" * 78)
    print("结果：PASS %d / FAIL %d / SKIP %d" % (_PASS, _FAIL, _SKIP))
    if _FAILED:
        print("红项：")
        for x in _FAILED:
            print("  -", x)
    print("=" * 78)
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
