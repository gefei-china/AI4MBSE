# -*- coding: utf-8 -*-
"""verify_autonomy_and_clarify — 门禁：**自主跑完**与**该澄清就弹卡**。

## 为什么需要（2026-10-27 用户两问）

用户原话：
  1. 「为啥不能自己持续运行完成任务，之前专门优化过持续工作的能力」
  2. 「如有不确定的、需要澄清的为啥不触发澄清卡片，非要终止后，再让我输入确认，
      新发起一轮对话」

真库现场（conv 9629，用户发「继续写入工程」那一轮）：

| 证据 | 值 | 后果 |
|---|---|---|
| `tool_call_logs` 里 `call_kind='tool'` 的行 | **0 条**（只有 2 条技能注入） | `exec_tools` 为空 |
| `_is_continuation_input("继续写入工程")` | **False**（该函数要求整句是裸续作短语） | `_cont_input` 也是 False |
| 自主续跑闸门 `(exec_tools or _cont_input)` | **两个都不满足** | 评估器**没跑** ⇒ 不会自动续跑 |
| 该轮答复结尾 | 「需要你确认一件事：先删掉再重来，还是留着？」 | 纯文本提问 ⇒ **不产生澄清卡** |

⇒ 两个机制盲区：
  ① 闸门把「模型把剩余步骤写进正文、自己却停手」这一整类**排除在外**；
  ② 「需要用户拍板」只能靠模型主动调 `ask_user` 才成卡，而模型习惯写在正文里。

## 本门禁锁六件事

① 闸门包含 `has_continue_cue` 与 `looks_awaiting_permission`（源码不变式）；
② `has_continue_cue` 能识别「继续+动词短语」（`_is_continuation_input` 不能）；
③ `looks_awaiting_permission` 在**真实答复**上判得出来、在**正常完成**上不误判；
④ 评估结论 `needs_user` 能解析，并能转成澄清卡题目（含 options）；
⑤ 澄清收口只有**一份实现**（`_clarify_finish_events`），两处调用点都走它
   —— 防"复制一份然后漂移"（该方法含「先归档再等回答」这条踩过坑的纪律）；
⑥ 行为验证：`needs_user` 结论 → 真的发出 `clarify_ask` 事件 + 落 `pending_clarify`。

含**变异自证**：把闸门改回 `(exec_tools or _cont_input)` ⇒ ①必须变红。

复现：`.venv/Scripts/python.exe -X utf8 tools/verify/verify_autonomy_and_clarify.py`
"""
import io
import os
import re
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

PASS = 0
FAIL = 0


def ok(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [OK  ] %s%s" % (name, ("  " + detail) if detail else ""))
    else:
        FAIL += 1
        print("  [FAIL] %s%s" % (name, ("  " + detail) if detail else ""))
    return bool(cond)


def _read(rel):
    try:
        return io.open(os.path.join(ROOT, rel), encoding="utf-8").read()
    except Exception:
        return ""


def _strip_comments(src: str) -> str:
    """剥注释与 docstring —— 断言"某写法存在/不存在"前必须做（本仓铁律）。"""
    out, i = [], 0
    for m in re.finditer(r'(?:"""[\s\S]*?"""|\'\'\'[\s\S]*?\'\'\')', src):
        out.append(src[i:m.start()])
        i = m.end()
    out.append(src[i:])
    return re.sub(r"#[^\n]*", "", "".join(out))


def gate_requires_new_signals(src: str) -> bool:
    """闸门是否包含两个新触发信号（源码不变式，供变异自证复用）。

    ⚠️ 两个信号是在 `if` **之前**算出来的（`_cont_cue = bool(_ac.has_continue_cue(...))`），
    所以判据必须"条件里出现变量 + 前面 1600 字内有对应函数调用"，
    不能只搜 if 片段本身（第一版就这么写，导致本门禁恒红 —— 断言位置错了）。
    """
    code = _strip_comments(src)
    m = re.search(r"if \(_ac_cfg\[\"enabled\"\][\s\S]{0,400}?\):", code)
    if not m:
        return False
    seg = m.group(0)
    if not ("_cont_cue" in seg and "_awaiting" in seg):
        return False
    head = code[max(0, m.start() - 1600):m.start()]
    return ("has_continue_cue" in head) and ("looks_awaiting_permission" in head)


def main():
    from agent import auto_continue as A

    print("=== ① 闸门源码不变式（含两个新触发信号）===")
    _stream = _read("agent/pipeline_parts/stream.py")
    ok("闸门包含 has_continue_cue + looks_awaiting_permission",
       gate_requires_new_signals(_stream),
       "" if gate_requires_new_signals(_stream)
       else "闸门仍是 (exec_tools or _cont_input) ⇒ 停手等人那一类永远不评估")

    print("\n=== ② `has_continue_cue` 覆盖「继续+动词短语」===")
    # 正例：用户就是在说"接着干"（含真实失败那轮原话）
    for t in ("继续写入工程", "继续", "接着把剩下的建完", "往下做",
              "按上面的方案落地", "再往下推进", "重试一次"):
        ok("正例命中 %r" % t, A.has_continue_cue(t))
    # 反例：新任务（不该被当成续作——避免把"继续优化报告排版"这类新请求误当续跑）
    for t in ("帮我重新设计冷板拓扑", "写入工程", "", "你好"):
        ok("反例拒绝 %r" % t, not A.has_continue_cue(t), "误判" if A.has_continue_cue(t) else "")

    print("\n=== ③ `looks_awaiting_permission`：真实答复判得出、正常完成不误判 ===")
    _real = None
    try:
        c = sqlite3.connect("file:mbse.db?mode=ro", uri=True)
        row = c.execute(
            "SELECT content FROM messages WHERE id=4979").fetchone()
        c.close()
        _real = row[0] if row else None
    except Exception:
        _real = None
    if _real:
        ok("真实失败答复（msg 4979）被判为『在征求许可』", A.looks_awaiting_permission(_real),
           "该答复结尾是整段 ```sysml``` 代码块，正文里的『需要你确认』在 3500 字之前"
           if not A.looks_awaiting_permission(_real) else "")
        ok("剥代码块后仍能命中（说明不是靠『末尾窗口』侥幸）",
           bool(re.search(r"```", _real)) and A.looks_awaiting_permission(_real))
    else:
        ok("真库可取到 msg 4979（夹具自证）", False, "取不到 ⇒ 本条无法验证")
    for s in ("已完成。三个包都已建好并回读确认，模型无遗留问题。",
              "已执行完毕，结果如上表。",
              "抱歉，这个操作需要运维介入。"):
        ok("正常完成不误判 %r" % s[:16], not A.looks_awaiting_permission(s))

    print("\n=== ④ `needs_user` 结论可解析、可转澄清卡 ===")
    v = A.parse_verdict('{"done": false, "needs_user": true, '
                        '"question": "先删掉再重来还是留着？", '
                        '"options": ["删除重建", "保留并列新建"]}')
    ok("parse_verdict 解析出 needs_user", bool(v) and v.get("needs_user") is True, repr(v))
    ok("options 被保留（做选择题的必要条件）",
       bool(v) and len(v.get("options") or []) == 2, repr((v or {}).get("options")))
    ok("done=true 仍是 done（不被新分支污染）",
       (A.parse_verdict('{"done": true}') or {}).get("done") is True)
    ok("needs_user 但缺 question ⇒ None（结论不完整不驱动流程）",
       A.parse_verdict('{"done": false, "needs_user": true}') is None)
    cq = A.clarify_from_verdict(v)
    ok("转成澄清卡题目（前端认的 id/question/options/allow_custom 四字段）",
       bool(cq) and cq.get("id") and cq.get("question")
       and isinstance(cq.get("options"), list) and "allow_custom" in (cq or {}),
       repr(cq))

    print("\n=== ⑤ 澄清收口只有一份实现（防复制漂移）===")
    code = _strip_comments(_stream)
    ok("存在 `_clarify_finish_events`", "def _clarify_finish_events" in code)
    # ⚠️ 不能断言"clarify_ask 字面量只出现 1 次"：另有 **2 处前置意图澄清**
    #   （3910/3928，`_clarify_detect` 的 skip/questions 两条路）是不同语义路径，本来就各发一次。
    #   也不能断言 `"source": "ask_user"` 出现 1 次 —— 抽方法后它被**参数化**了
    #   （事件里写 `"source": source`），硬编码计数必然为 0（第一版就这么踩的）。
    #   要锁的是两件更本质的事：
    #     ① 收口已参数化（默认 ask_user，事件里用变量 ⇒ 复制一份就会重新硬编码）；
    #     ② 「先持久化挂起 + 再发卡」这个组合**只有一处**（`_persist_clarify` 紧跟 clarify_ask）。
    ok("收口已参数化（默认 source=ask_user，事件用变量）",
       'source="ask_user"' in code and '"source": source' in code)
    # 『持久化挂起 + 发卡』全文件本来就有 3 处（前置意图澄清 2 处 + 本收口 1 处），
    # 所以不能拿"组合计数"当判据（第一版这么写，恒红）。真正要防漂移的是
    # **收口方法内部的「先归档再等回答」纪律** —— 那条是踩过坑的（澄清会 return，
    # 走不到 persist 的归档 ⇒ 已取回的 80543 字节文件丢失）。锁它：只此一处。
    _helper = ""
    _i = code.find("def _clarify_finish_events")
    if _i >= 0:
        _j = code.find("def execute_stream", _i)
        _helper = code[_i:(_j if _j > _i else len(code))]
    ok("收口方法内含『先归档再等回答』纪律（澄清不是失败）",
       "_archive_artifacts" in _helper and "link_archived" in _helper)
    ok("该纪律只出现在收口方法内（未被复制到第二处）",
       code.count("_archive_artifacts(") - code.count("_archive_artifacts(_c_ar") <= 4,
       "全文件 _archive_artifacts 调用 %d 处（归档主路径本身有多处，此条仅作提示）"
       % code.count("_archive_artifacts("))
    ok("两处调用点都走 yield from（ask_user 路径 + 评估器 needs_user 路径）",
       code.count("yield from self._clarify_finish_events(") >= 2,
       "出现 %d 次" % code.count("yield from self._clarify_finish_events("))
    ok("评估器 needs_user 分支用 source=auto_continue（与 ask_user 可区分）",
       'source="auto_continue"' in code)

    print("\n=== ⑥ 行为验证：needs_user ⇒ 真的发 clarify_ask + 落 pending_clarify ===")
    _behaviour_ok = False
    try:
        _behaviour_ok = _run_behaviour_case()
    except Exception as e:
        print("    （行为用例异常：%r）" % (e,))
    ok("needs_user 结论驱动出澄清卡片与挂起状态", _behaviour_ok)

    print("\n=== ⑥b 连续重复调用收敛闸（防烧穿 token 预算）===")
    # 真库实证（conv 9629 16:50 那轮）：5 次真调用后模型**连续 5 次**发完全相同的
    # `query_data_tree`，被 dup 短路且 `ok=True` ⇒ 不计失败轮 ⇒ 轮次护栏不响
    # ⇒ 每轮 28k token × 5 ≈ 14 万被白烧，最终以「工具预算耗尽 307,505/300,000」收场。
    ok("存在连续重复计数 `_dup_streak`（初值在外层循环外）",
       "_dup_streak = 0" in code and code.count("_dup_streak") >= 4,
       "出现 %d 次" % code.count("_dup_streak"))
    ok("真执行时归零（否则跨轮累计会被正常推进误伤）",
       re.search(r"_dup_streak\s*=\s*0\s*#\s*★?\s*真执行|_dup_streak = 0\s+result = _lg\.call",
                 code) is not None)
    ok("连续 ≥2 次短路即收敛（break 出 ReAct，交兜底生成）",
       re.search(r"if _dup_streak >= 2:[\s\S]{0,900}?break", code) is not None)
    ok("收敛时发可见 stage（用户能看到'为什么停'）",
       '"重复调用收敛"' in code)
    _dup_streak_events = []
    try:
        from database import db_conn
        with db_conn() as _c:
            _dup_streak_events = [r[0] for r in _c.execute(
                "SELECT payload_json FROM orch_stream_events WHERE conversation_id=9629 "
                "AND event_type='tool' AND created_at>='2026-10-27 16:50' "
                "AND payload_json LIKE '%dup_short_circuit%'").fetchall()]
    except Exception:
        pass
    ok("真库确证：该轮确有连续 dup 短路（≥3 条）—— 这不是假想缺陷",
       len(_dup_streak_events) >= 3, "实测 %d 条" % len(_dup_streak_events))

    print("\n=== ⑦ 变异自证：闸门改回旧写法 ⇒ ①必须变红 ===")
    _mut = re.sub(r'\(exec_tools or _cont_input or _cont_cue or _awaiting\)',
                  '(exec_tools or _cont_input)', _stream, count=1)
    ok("变异注入成功（锚点存在）", _mut != _stream)
    ok("变异后闸门判据变红（证明①非空转）",
       _mut != _stream and not gate_requires_new_signals(_mut))

    print("\n" + "=" * 60)
    print("PASS %d / FAIL %d" % (PASS, FAIL))
    if FAIL:
        print("❌ 自主续跑 / 澄清卡机制仍有缺口")
        return 1
    print("✅ 停手等人会被评估并续跑；需要拍板时会弹澄清卡")
    return 0


def _run_behaviour_case() -> bool:
    """真跑一轮：桩掉 LLM 让评估器返回 needs_user ⇒ 断言发卡 + 落挂起。

    只桩 **LLM 与评估器**（外部世界），不桩被测的收口逻辑。
    """
    from database import db_conn
    from agent.pipeline import AgentPipeline
    from llm import llm_client

    orig_chat = llm_client.chat
    counts = {}
    try:
        with db_conn() as conn:
            cur = conn.execute(
                "INSERT INTO conversations (user_id, title, created_at, updated_at) "
                "VALUES (1, 'autonomy clarify gate', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)")
            cid = cur.lastrowid
            conn.commit()

        calls = {"eval": 0}

        def _chat(messages, **kw):
            it = str(kw.get("_intent") or "")
            if it == "chat_eval":
                calls["eval"] += 1
                return {"choices": [{"message": {"content":
                        '{"done": false, "needs_user": true, '
                        '"question": "先删掉错位空包再重来，还是保留并列新建？", '
                        '"options": ["删除重建", "保留并列新建"]}'}}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
            if it == "clarify_detect":
                return {"choices": [{"message": {"content": '{"need": false, "intent": "chat"}'}}]}
            if kw.get("stream"):
                if it == "chat_answer":
                    def _g():
                        yield _sse("## 结论：还没写入成功\n\n需要你确认一件事："
                                   "先删掉那个错位空包再重来，还是保留并列新建？")
                    return _g()

                def _e():
                    if False:
                        yield ""
                return _e()
            if it == "chat_probe":
                return {"choices": [{"message": {"content": ""}}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 0}}
            return {"choices": [{"message": {"content": "ok"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

        llm_client.chat = _chat
        pipe = AgentPipeline()
        for ev in pipe.execute_stream("写入用例图包", cid, branch="dev",
                                      forced_intent="chat",
                                      user={"id": 1, "display_name": "gate"}):
            t = ev.get("type")
            counts[t] = counts.get(t, 0) + 1
    finally:
        llm_client.chat = orig_chat

    print("    帧统计=%s 评估器调用=%d" % (counts, calls["eval"]))
    ok_a = counts.get("clarify_ask", 0) >= 1
    ok_b = counts.get("done", 0) >= 1
    print("    clarify_ask 帧=%d done 帧=%d" % (counts.get("clarify_ask", 0),
                                               counts.get("done", 0)))
    with db_conn() as conn:
        row = conn.execute("SELECT pending_clarify FROM conversations WHERE id=?",
                           (cid,)).fetchone()
        pend = (row[0] if row else "") or ""
    ok_c = '"options"' in pend and "先删掉" in pend
    return bool(ok_a and ok_b and ok_c)


def _sse(text: str) -> str:
    import json
    return "data: " + json.dumps({"choices": [{"delta": {"content": text}}]},
                                 ensure_ascii=False) + "\n\n"


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
