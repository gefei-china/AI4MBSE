# -*- coding: utf-8 -*-
# CI-OPTIONAL: [local] tier=local（见 tools/gates_manifest.json）：2026-11-20 补登记：历史漏入清单，当前按 local 手工跑；是否晋 pr 需先确认无外部依赖并同步 ci.yml（G6a）
"""attempt 结构化留痕门禁（2026-11-19）—— 守「结构对齐标杆」的不变式。

## 守什么

  1. 产生时打标：execute.py / stream.py 都在 harness 闸**之前** record，
     kind 由 `_at_kind` 按产生时信号判定（不靠下游从文本猜）；
  2. 正文单一事实源：`content` = 收敛后的最终版，历次原文只进 `exec.attempts`；
  3. 向后兼容：单次生成不写 attempts；旧数据 `read_attempts()` = []；
  4. 前端消费：历史回放把 attempts 渲染进过程区（`<details>`），
     **正文没有任何折叠/限高**（verify_answer_no_inner_scroll 已守，这里再守标记）；
  5. 变异自证：拿掉 kind 信号、拿掉 attach、把折叠改成正文限高 ⇒ 必须翻红。

用法：.venv\\Scripts\\python.exe -X utf8 tools/verify/verify_attempt_trace.py
退出码：全绿 0 / 有失败 1。
"""
import os as _svos
import sys as _svsys
_svroot = _svos.path.dirname(_svos.path.abspath(__file__))
while _svroot and not _svos.path.isdir(_svos.path.join(_svroot, 'tools')):
    _svroot = _svos.path.dirname(_svroot)
if _svroot and _svroot not in _svsys.path:
    _svsys.path.insert(0, _svroot)
from tools.stream_source import read_stream_source  # noqa: E402
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

PASS, FAIL = "PASS", "FAIL"
_results = []


def ck(tag, ok, detail=""):
    _results.append((PASS if ok else FAIL, tag))
    print("  %-5s %s%s" % (PASS if ok else FAIL, tag, ("  | " + str(detail)) if detail else ""))


def main():
    print("=" * 84)
    print("attempt 结构化留痕门禁（2026-11-19）")
    print("  repo=%s" % ROOT)
    print("=" * 84)

    from core import attempt_trace as AT

    # ── A 语义判定（产生时打标，不靠猜）────────────────────────────────────
    print("\n[A] kind 在产生时按信号判定")
    ck("A1 首次 ⇒ answer", AT.kind_of(0) == AT.KIND_ANSWER)
    ck("A2 harness 续跑 ⇒ retry", AT.kind_of(1, harness_retry=True) == AT.KIND_RETRY)
    ck("A3 用户/截断续写 ⇒ continuation",
       AT.kind_of(1, continuation_signal=True) == AT.KIND_CONTINUATION)
    ck("A4 显式优先：两信号同真取 continuation",
       AT.kind_of(2, continuation_signal=True, harness_retry=True) == AT.KIND_CONTINUATION)
    ck("A5 无关信号不误判为 continuation",
       AT.kind_of(3, harness_retry=True) == AT.KIND_RETRY)

    # ── B record 结构与幂等 ────────────────────────────────────────────────
    print("\n[B] attempt 记录：结构、幂等、不丢原文")
    a = []
    a = AT.record(a, "第一次交付", index=0)
    a = AT.record(a, "第二次重跑", index=1, harness_retry=True)
    a = AT.record(a, "第三次续写", index=2, continuation_signal=True)
    ck("B1 三次 kind 依次 answer/retry/continuation",
       [x["kind"] for x in a] == ["answer", "retry", "continuation"],
       [x["kind"] for x in a])
    ck("B2 index 连续且有序", [x["index"] for x in a] == [0, 1, 2])
    ck("B3 原文逐字保存", [x["content"] for x in a] == ["第一次交付", "第二次重跑", "第三次续写"])
    ck("B4 chars 与原文长度一致", all(x["chars"] == len(x["content"]) for x in a))
    ck("B5 同 index 重入是更新非追加",
       len(AT.record(a, "覆盖版", index=1, harness_retry=True)) == 3
       and AT.record(a, "覆盖版", index=1, harness_retry=True)[1]["content"] == "覆盖版")
    ck("B6 空内容不记（失败轮无交付）", AT.record(a, "", index=9) == a)
    ck("B7 不改入参（纯函数）", len(a) == 3)
    ck("B8 超长原文带 truncated 标记",
       AT.record([], "x" * (AT._MAX_ATTEMPT_CHARS + 10), index=0)[0].get("truncated") is True)

    # ── C attach / read 的向后兼容 ─────────────────────────────────────────
    print("\n[C] 向后兼容：单次不写、旧数据可读")
    cd = json.dumps({"intent": "design", "exec": {"reasoning": "r"}})
    out1 = AT.attach(cd, a[:1])
    ck("C1 只 1 次 attempt ⇒ 原样返回（字符串逐字节一致）", out1 == cd)
    ck("C2 0 次 attempt ⇒ 原样返回", AT.attach(cd, []) == cd)
    out3 = json.loads(AT.attach(cd, a))
    ck("C3 多次 ⇒ 注入 exec.attempts", len(out3["exec"]["attempts"]) == 3)
    ck("C4 注入不破坏原有 exec 字段", out3["exec"].get("reasoning") == "r")
    ck("C5 不破坏其他 card 字段", out3.get("intent") == "design")
    ck("C6 dict 入参返回 dict",
       isinstance(AT.attach({"a": 1}, a), dict))
    ck("C7 旧数据（无 attempts）读出空列表", AT.read_attempts(cd) == [])
    ck("C8 新数据读出 3 条", len(AT.read_attempts(json.dumps(out3))) == 3)
    ck("C9 损坏 JSON 不抛异常", AT.read_attempts("{broken") == [])
    ck("C10 attempts 非数组时不抛", AT.read_attempts('{"exec":{"attempts":"x"}}') == [])

    # ── D 后端接线：两条路径同口径、位置正确 ───────────────────────────────
    print("\n[D] 后端：execute.py 与 stream.py 同口径")
    ex = open(os.path.join(ROOT, "agent", "pipeline_parts", "execute.py"),
              encoding="utf-8").read()
    st = read_stream_source()
    for label, src in (("execute", ex), ("stream", st)):
        ck("D1 %s 初始化 _ac_attempts" % label, "_ac_attempts = []" in src)
        ck("D1b %s 调用同一 record 函数" % label,
           "from core.attempt_trace import record as _at_rec" in src)
        ck("D1c %s 调用同一 attach 函数" % label,
           "from core.attempt_trace import attach as _at_attach" in src)
        ck("D1d %s 用 _at_kind 而非自己猜" % label,
           "kind_of as _at_kind" in src and "kind=_at_kind" in src)
        ck("D1e %s 留痕失败可见（不静默）" % label,
           "attempt 留痕失败" in src and "_atl.getLogger" in src)
    # 位置：record 必须在 harness 闸改写 llm_content 之前（否则存的是闸反馈不是模型原文）
    i_ex_rec = ex.find("_ac_attempts = _at_rec")
    i_ex_hgate = ex.find("if _hgate.get(\"block\"):", i_ex_rec - 2000)
    ck("D2 execute 记录点在 harness 闸之前",
       0 < i_ex_rec < i_ex_hgate, "rec@%d hgate@%d" % (i_ex_rec, i_ex_hgate))
    ck("D3 两路径都记在收口（_ac_full/_full_content 赋值）之前",
       ex.find("_ac_attempts = _at_rec") < ex.find("llm_content = _ac_full")
       and st.find("_ac_attempts = _at_rec") < st.find("_full_content = _ac.merge_round_content"))
    # ★ 关键不变式：content 必须是 merge 后的全量，**不能取最后一次 attempt**。
    #   真库 msg#7039 的最后一次 attempt（a3）里**没有提示词模板** ——
    #   若有人把收口改成 `llm_content = 最后一次`，用户要的成品就丢了。
    ck("D4 直行收口用 merge 全量（不是最后一次 attempt）",
       "llm_content = _ac_full" in ex)
    ck("D4b 流式收口用 merge 全量（不是最后一次 attempt）",
       "_full_content = _ac.merge_round_content" in st)

    # 功能级反证：拿真库三次 attempt 回放，证明"取最后一次"会丢提示词
    import sqlite3
    from agent.auto_continue import merge_round_content as _merge
    DB = os.path.join(ROOT, "mbse.db")
    if os.path.exists(DB):
        con = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
        con.row_factory = sqlite3.Row
        try:
            raw7039 = con.execute("SELECT content FROM messages WHERE id=7039").fetchone()
        finally:
            con.close()
        if raw7039:
            t = raw7039["content"]
            b2 = t.find("## 结论：17 项待办中，只有 3 项")
            b3 = t.find("## 结论：17 项待办在当前条件下无法完成")
            if b2 > 0 and b3 > b2:
                p1, p2, p3 = t[:b2], t[b2:b3], t[b3:]
                ck("D5 反证成立：最后一次 attempt 不含提示词",
                   "# 角色" not in p3)
                _m = _merge(_merge(_merge("", p1), p2), p3)
                ck("D5b merge 全链路保住了提示词（D4 承重）",
                   "# 角色" in _m)

    # ── E 前端：进过程区，正文不折叠 ───────────────────────────────────────
    print("\n[E] 前端：历史尝试进过程区，正文保持不折叠")
    js = open(os.path.join(ROOT, "static", "js", "mods", "06-cards.js"),
              encoding="utf-8").read()
    ck("E1 历史回放读取 ex.attempts", "Array.isArray(ex.attempts)" in js)
    ck("E2 渲染成 proc step（cls:'attempts'）", "cls:'attempts'" in js)
    ck("E3 用原生 details 折叠（不是正文容器）", "<details" in js and "</details>" in js)
    ck("E4 kind 标签与后端一致（首次/续写/重跑）",
       all(k in js for k in ("answer:'首次'", "continuation:'续写'", "retry:'重跑'")))
    ck("E5 老消息无字段时不渲染该 step", "if(_attempts.length > 1)" in js)
    # 正文不得被 attempts 逻辑加上限高（另有 verify_answer_no_inner_scroll 全量守）
    ck("E6 attempts 代码段没给正文加 max-height",
       "max-height" not in js[js.find("const _attempts"):js.find("if(ex.agent)")])

    # ── F 变异自证 ─────────────────────────────────────────────────────────
    print("\n[F] 变异自证：拿掉关键因子结论必须翻转")
    ck("F1 基线：多次生成会写 attempts", "attempts" in json.loads(AT.attach(cd, a))["exec"])
    ck("F2 变异：只给 1 条时不写（attach 承重）",
       "attempts" not in json.loads(AT.attach(cd, a[:1]))["exec"])

    # 变异：把 kind_of 的信号分支废掉 ⇒ retry 被误记成 answer
    _orig = AT.kind_of
    try:
        AT.kind_of = lambda i, **kw: AT.KIND_ANSWER
        mut = AT.record([], "x", index=1, harness_retry=True)
        ck("F3 变异：kind_of 恒返回 answer ⇒ retry 被抹掉（判据承重）",
           mut[0]["kind"] == AT.KIND_ANSWER)
    finally:
        AT.kind_of = _orig
    ck("F3b 恢复后 retry 重新生效",
       AT.record([], "x", index=1, harness_retry=True)[0]["kind"] == AT.KIND_RETRY)

    # 变异：前端若把 details 换成正文限高，E6 必须翻红
    mut_js = js.replace("white-space:pre-wrap;font-size:12px", "max-height:340px;font-size:12px")
    ck("F4 变异：attempts 段加 max-height ⇒ E6 等价判据翻红",
       "max-height" in mut_js[mut_js.find("const _attempts"):mut_js.find("if(ex.agent)")])

    fails = [t for s, t in _results if s == FAIL]
    print("\n" + "-" * 84)
    print("合计 %d 项（PASS %d / FAIL %d）"
          % (len(_results), len(_results) - len(fails), len(fails)))
    for t in fails:
        print("  · FAIL " + t)
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
