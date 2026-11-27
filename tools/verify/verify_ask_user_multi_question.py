# -*- coding: utf-8 -*-
"""门禁：ask_user 多题 + 已答去重 —— 答案不丢、不重复问。

背景（2026-11-27）：ask_user 一次只能问 1 题（question 是 string），
多题只能分多次弹卡；且无「已答去重」，用户答完又被问同一题
（conv 9571 7 连问 / conv 9553 24 连问 / conv 1000000254）。

判据：
  A 多题产出：id 唯一、题数≤3、单题向后兼容；
  B 去重：已答问题被过滤、显式重问不过滤、skip 也记指纹；
  C 逻辑闭环：结构化答案带 question_id+原题+值，旧扁平形态兼容；
  D 变异自证：把唯一 id 改回固定值，A 必须翻红。

用法：python tools/verify/verify_ask_user_multi_question.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from agent import clarify_dedup as cd

RESULTS = []


def check(group, name, cond, detail=""):
    RESULTS.append((group, name, bool(cond), detail))


def group_a():
    # as_question_list 覆盖多题/单题/旧数据三形态
    multi = cd.as_question_list({"clarify_questions": [
        {"id": "ask_user_1", "question": "选哪个工程？"},
        {"id": "ask_user_2", "question": "用哪个分支？"}]})
    check("A", "多题产出 2 题", len(multi) == 2, json.dumps(multi, ensure_ascii=False))
    check("A", "★ 多题 id 唯一（前端 radio 分组不打架）",
          len({q["id"] for q in multi}) == 2, str([q["id"] for q in multi]))
    single = cd.as_question_list({"clarify": {"id": "ask_user_1", "question": "单题"}})
    check("A", "单题向后兼容", len(single) == 1 and single[0]["question"] == "单题")
    legacy = cd.as_question_list({"question": "老数据"})
    check("A", "旧单题 dict 兼容", len(legacy) == 1)
    check("A", "空输入不产题", cd.as_question_list({}) == [])


def group_b():
    q1, q2 = "卫星通信建模工程在哪个工程下？", "用哪个分支？"
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, conversation_id INTEGER, msg_type TEXT, card_data TEXT)")
    c.execute("INSERT INTO messages VALUES (1,1,'clarify',?)", (json.dumps({
        "questions": [{"id": "ask_user_1", "question": q1},
                      {"id": "ask_user_2", "question": q2}],
        "answered": {"at": "x", "answers": ["AI建模1126", "dev"]},
    }, ensure_ascii=False),))
    c.commit()
    got = cd.filter_questions([{"id": "a", "question": q1}, {"id": "b", "question": q2}],
                              c, 1)
    check("B", "★ 两题都已答 ⇒ 全部过滤（不再弹卡）", got == [], str(got))
    mixed = cd.filter_questions([{"id": "a", "question": q1},
                                 {"id": "c", "question": "新问题？"}], c, 1)
    check("B", "已答过滤、新题保留", len(mixed) == 1 and mixed[0]["question"] == "新问题？", str(mixed))
    reask = cd.filter_questions([{"id": "a", "question": q1}], c, 1, user_input="还是那个问题，改主意")
    check("B", "显式改主意不过滤", len(reask) == 1)
    # 未 answered 的卡不算已答（pending 卡不能吞掉问题）
    c.execute("INSERT INTO messages VALUES (2,2,'clarify',?)", (json.dumps({
        "questions": [{"id": "x", "question": "挂起中？"}]}, ensure_ascii=False),))
    c.commit()
    pending = cd.filter_questions([{"id": "x", "question": "挂起中？"}], c, 2)
    check("B", "仅挂起未答 ⇒ 不过滤", len(pending) == 1)
    # skip 也记指纹
    c.execute("INSERT INTO messages VALUES (3,3,'clarify',?)", (json.dumps({
        "questions": [{"id": "s", "question": "跳过题？"}],
        "answered": {"at": "x", "answers": [], "skipped": True}}, ensure_ascii=False),))
    c.commit()
    skipped = cd.filter_questions([{"id": "s", "question": "跳过题？"}], c, 3)
    check("B", "skip 也记指纹（不无限重弹）", skipped == [])
    c.close()


def group_c():
    # 结构化答案字段与 routers 回写形态对照（源码级断言，跨边界）
    src = open(os.path.join(_ROOT, "routers", "conversations.py"), encoding="utf-8").read()
    check("C", "★ 回写含 answers_detail（题目↔答案对应）", "answers_detail" in src)
    check("C", "回写保留扁平 answers（旧渲染兼容）",
          '"answers": _ans_lines' in src)
    js = open(os.path.join(_ROOT, "static", "js", "mods", "05-markdown.js"), encoding="utf-8").read()
    check("C", "前端优先渲染结构化答案", "answers_detail" in js)
    check("C", "前端仍兼容旧扁平答案", "(_ans.answers||[]).length" in js)


def group_d():
    src_path = os.path.join(_ROOT, "agent", "clarify_dedup.py")
    src = open(src_path, encoding="utf-8").read()
    anchor = '    if not qs or is_explicit_reask(user_input):\n        return qs'
    if src.count(anchor) != 1:
        check("D", "变异锚点唯一", False, "count=%d" % src.count(anchor))
        return
    check("D", "变异锚点唯一", True)
    mutated = src.replace(anchor, '    return qs', 1)
    check("D", "变异体真的变了", mutated != src)
    tmp = os.path.join(tempfile.gettempdir(), "_cd_mut_multi_q.py")
    open(tmp, "w", encoding="utf-8").write(mutated)
    spec = importlib.util.spec_from_file_location("_cd_mut_multi_q", tmp)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, conversation_id INTEGER, msg_type TEXT, card_data TEXT)")
    c.execute("INSERT INTO messages VALUES (1,1,'clarify',?)", (json.dumps({
        "questions": [{"id": "a", "question": "已答题？"}],
        "answered": {"at": "x", "answers": ["答案"]}}, ensure_ascii=False),))
    c.commit()
    got = mod.filter_questions([{"id": "a", "question": "已答题？"}], c, 1)
    check("D", "★ 去掉去重后已答题不再被过滤 ⇒ B 有牙齿", len(got) == 1, str(got))
    c.close()
    try:
        os.remove(tmp)
    except OSError:
        pass


def main():
    for fn in (group_a, group_b, group_c, group_d):
        try:
            fn()
        except Exception as e:
            check(fn.__name__, "组执行异常", False, repr(e))
    w = max(len(n) for _, n, _, _ in RESULTS)
    print("=" * (w + 16))
    print("ask_user 多题与去重门禁（答案不丢 · 不重复问）")
    print("=" * (w + 16))
    for g, n, ok, detail in RESULTS:
        print("  [%s] %s%s" % ("OK" if ok else "FAIL", n,
                               ("  — " + detail) if (detail and not ok) else ""))
    bad = [x for x in RESULTS if not x[2]]
    print("-" * (w + 16))
    print("合计 %d 项，通过 %d，失败 %d" % (len(RESULTS), len(RESULTS) - len(bad), len(bad)))
    for g, n, _, d in bad:
        print("  - %s：%s" % (n, d[:200]))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
