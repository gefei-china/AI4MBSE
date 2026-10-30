# -*- coding: utf-8 -*-
"""verify_partial_persist_on_failure — 门禁：**中途失败必须保住已流出的内容**。

## 病灶（2026-10-27，真库 conv 9629 15:34:39「写入工程」轮）

用户在界面上看到 AI 已经开始输出，随后这一轮失败；**刷新/切回会话后一个字都不剩**。
真库取证：该轮 `llm_usage_stats` 有 4 次调用（末次 22,707 prompt / 929 completion，
内容确实生成过），但 `messages` 里 **0 条 assistant**、`orch_stream_events` **0 条**。

## 根因（源码级，可被本门禁直接压住）

`agent/pipeline_parts/stream.py` 的非工具生成分支：

```python
full = []
for chunk in chunks:
    for delta in self._extract_stream_deltas(chunk):
        if delta.get("content"):
            full.append(delta["content"])
            yield {"type": "token", "delta": delta["content"]}   # ← 已经发给用户了
llm_content = "".join(full)      # ← 只在**循环正常结束后**才赋值
```

⇒ 流**中途**抛异常（provider 断流 / 网关 reset / 解析错误）时：
  · 用户屏幕上已有 N 个 token；
  · 但 `llm_content` 仍是 `""`；
  · 异常分支算出的 `_partial` 因此为空 → `_persist_partial_stream` 的
    `if len(text) < 4: return` 直接跳过 → **库里 0 条** → 刷新即蒸发。

对比：工具轮后的生成分支写的是 `llm_content += delta["content"]`（增量赋值），
不受影响 —— 两条分支的**累积方式不一致**才是缺陷本身。

## 本门禁锁三件事

① **已流出内容必须被累积**：`execute_stream` 在流中途失败后，库里必须有 assistant 消息，
   且内容包含失败前已发出的文本（不得只剩一句错误说明）；
② **不得双写**：失败落库与正常落库只能有一条（重复写等于另一种"内容错乱"）；
③ **变异自证**：把累积方式改回"循环后统一赋值"，①必须变红 —— 否则本门禁是空转。

复现：`.venv/Scripts/python.exe -X utf8 tools/verify/verify_partial_persist_on_failure.py`
"""
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

PASS = 0
FAIL = 0
STREAMED = "失败前已经流出的这段话必须保住"


def ok(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [OK  ] %s%s" % (name, ("  " + detail) if detail else ""))
    else:
        FAIL += 1
        print("  [FAIL] %s%s" % (name, ("  " + detail) if detail else ""))
    return bool(cond)


def _strip_comments(src: str) -> str:
    """删掉注释与 docstring —— 断言"旧写法已清除"前必须先剥。

    ⚠️ 本门禁第一版就栽在这里：`full.append(delta["content"])` 只出现在**我自己
    写的"旧实现长什么样"注释里**，却被当成"代码仍存在"⇒ 断言恒红。
    本仓铁律：任何"某写法是否存在"的判据都必须先剥注释/文档字符串。
    """
    import io
    import re
    # 去三引号块（简单配对：本文件里 docstring 不会嵌套引号三连）
    out, i = [], 0
    for m in re.finditer(r'(?:"""[\s\S]*?"""|\'\'\'[\s\S]*?\'\'\')', src):
        out.append(src[i:m.start()])
        i = m.end()
    out.append(src[i:])
    no_doc = "".join(out)
    # 去行注释（保留行尾引号内的内容不做精细处理：本文件待判片段均在行首或独立行）
    return re.sub(r"#[^\n]*", "", no_doc)


def _new_conv(conn):
    cur = conn.execute(
        "INSERT INTO conversations (user_id, title, created_at, updated_at) "
        "VALUES (1, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
        ("partial-persist gate",))
    return cur.lastrowid


def _chunk(text):
    """构造 OpenAI 兼容 SSE 块（与 llm_client 真实产出同形）。"""
    import json
    return "data: " + json.dumps(
        {"choices": [{"delta": {"content": text}}]}, ensure_ascii=False) + "\n\n"


def _fake_chat_factory():
    """替换 llm_client.chat（**只 mock 掉"外部世界"，不 mock 被测逻辑**）：

    · `stream=True` 且 `_intent == chat_answer` → 先吐 3 段正文，然后**抛异常**（模拟断流）
    · `_intent` 为意图/澄清探测器 → 返回**合规 JSON**（放行到生成路径）
    · 其它 → 返回安全的最小 completion

    ⚠️ 必须分 `_intent` 回应，否则夹具会把流程带偏：
      第一版对所有非流式调用一律回 `{"message":{"content":"ok"}}`，
      而澄清探测器要求「只输出 JSON」⇒ 解析失败 ⇒ 流程走 `clarify_ask` **直接 return**，
      根本到不了生成路径（帧统计 `clarify_ask:1`、已流出 0 字，
      看起来像"代码没修好"，实际是夹具失效 —— 本仓纪律：夹具自身要能自证）。
    """
    state = {"stream_calls": 0, "answer_calls": 0}

    def _chat(messages, **kw):
        intent = str(kw.get("_intent") or "")
        if kw.get("stream"):
            state["stream_calls"] += 1
            if intent == "chat_answer":
                state["answer_calls"] += 1

                def _gen():
                    yield _chunk(STREAMED[:8])
                    yield _chunk(STREAMED[8:20])
                    yield _chunk(STREAMED[20:])
                    raise RuntimeError("模拟 Provider 中途断流（gate fixture）")
                return _gen()

            def _empty():
                if False:
                    yield ""
            return _empty()
        # 澄清探测器：需要「只输出 JSON」的合规回复，否则会误判成"要澄清"
        if intent == "clarify_detect":
            return {"choices": [{"message": {"content":
                    '{"need": false, "intent": "chat"}'}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
        # chat_probe：必须回**空正文**，才会落到真正的流式生成分支。
        #   ⚠️ 回 "ok" 会命中"探测轮直出"分支（llm_content = p_content），
        #   那样就绕开了被测的 `if not tool_injected:` 累积逻辑（门禁第二版踩过：
        #   帧统计 token=1 但只有 2 字 = "ok"，看着像"内容没累积"）。
        if intent == "chat_probe":
            return {"choices": [{"message": {"content": ""}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 0}}
        return {"choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
    return _chat, state


def _run_case(pipe, conv_id, monkey_llm=True, stop_after_tokens=None):
    """跑一轮 execute_stream，返回 (事件类型计数, 已流出正文, 异常文本)。

    `stop_after_tokens`：收到 N 个 token 后**主动 close 生成器** —— 这会在 `yield`
    处抛出 `GeneratorExit`，正是真实环境里"客户端断开（切页/关标签/网络 reset）"
    走的那条分支。选它作为主测路径的理由：真库证据（该轮 `orch_stream_events` **0 条**）
    只有在"生成器被 close、永远等不到 done/error 帧"时才成立 ——
    若是普通异常，`_evbuf` 会在 error 帧处刷库，事件不会全空。
    """
    from llm import llm_client
    orig = llm_client.chat
    counts, streamed, err = {}, "", ""
    gen = None
    if monkey_llm:
        fake, _ = _fake_chat_factory()
        llm_client.chat = fake
    try:
        # ⚠️ 必须 `forced_intent="chat"`：默认路由会走建模意图 → 触发 clarify_ask
        #   直接 `return`，**根本到不了生成路径**（门禁第一版就栽在这）。
        gen = pipe.execute_stream("请输出一段内容用于验证断点保护", conv_id,
                                  branch="dev", forced_intent="chat",
                                  user={"id": 1, "display_name": "gate"})
        _tok = 0
        for ev in gen:
            t = ev.get("type")
            counts[t] = counts.get(t, 0) + 1
            if t == "token":
                streamed += ev.get("delta") or ""
                _tok += 1
                if stop_after_tokens and _tok >= stop_after_tokens:
                    gen.close()      # ← 在 yield 点抛 GeneratorExit（真实断连同路径）
                    break
            elif t == "error":
                err = str(ev.get("message") or "")
    except Exception as e:      # execute_stream 内部已兜，这里只是防门禁自身崩
        err = err or repr(e)
    finally:
        if gen is not None:
            try:
                gen.close()
            except Exception:
                pass
        if monkey_llm:
            llm_client.chat = orig
    return counts, streamed, err


def _mutate_drop_retention(src: str) -> str:
    """变异：把**非工具生成分支里那两行"留存"**删掉 ⇒ 内容流出过、但没人记住它。

    这是原缺陷的**等价效果**（旧代码是"循环后才赋值"，效果同样是"流出过、未留存"），
    且锚点稳定（只在这一个分支的 span 内动，不依赖缩进细节）。
    意义：验证门禁 ②真的依赖"留存"这件事，而不是被别的路径顺带满足。
    """
    import re
    start = src.find("if not tool_injected:")
    end = src.find('st = getattr(llm_client, "stats", {})', start)
    if start < 0 or end < 0:
        return src
    body = src[start:end]
    mutated = re.sub(r"\n\s*llm_content \+= delta\[\"content\"\]", "", body)
    mutated = re.sub(r"\n\s*_streamed_acc\[0\] \+= delta\[\"content\"\]", "", mutated)
    if mutated == body:
        return src
    return src[:start] + mutated + src[end:]


def _rows(conn, conv_id):
    return list(conn.execute(
        "SELECT id, role, content FROM messages WHERE conversation_id=? ORDER BY id",
        (conv_id,)).fetchall())


def main():
    from database import db_conn
    from agent.pipeline import AgentPipeline
    import agent.pipeline_parts.stream as S

    print("=== ① 源码画像：两条生成分支必须**同一种**累积方式 ===")
    import io
    src = io.open(os.path.join(ROOT, "agent/pipeline_parts/stream.py"), encoding="utf-8").read()
    code = _strip_comments(src)          # ⚠️ 判据只看代码，不看注释（见 _strip_comments）
    # 非工具分支（原病灶）：不得再出现"先攒 full、循环后统一赋值"
    bad_pattern = 'full.append(delta["content"])'
    ok("非工具生成分支不再用『循环后统一赋值』（full.append）",
       bad_pattern not in code,
       "" if bad_pattern not in code else "仍存在：内容在循环完成后才赋给 llm_content")
    ok("非工具生成分支改为增量累积 llm_content",
       code.count('llm_content += delta["content"]') >= 2,
       "出现 %d 处" % code.count('llm_content += delta["content"]'))
    ok("存在『已流出内容』独立累积器（_streamed_acc）",
       "_streamed_acc" in code, "" if "_streamed_acc" in code else "缺失：失败时无兜底来源")

    # ★ 反模式 ②：**先节奏化吐字、后赋值**。
    #   `for _ck in iter_stream_chunks(x): yield token` 是"按类人节奏慢慢吐"，
    #   一次能吐几秒到几十秒；用户完全可能在这段窗口里断开/关页。
    #   若把 `llm_content = x` 写在循环**之后**，断连时它仍是空 ⇒ 固化 0 字符
    #   （真库 conv 9663 实证：`reason=stop 候选字符=0`，用户屏幕上却有 24 字）。
    #   判据：每个 `iter_stream_chunks(X)` 循环之前，X 必须已经进了 llm_content。
    import re as _re
    bad_order = []
    for m in _re.finditer(r"for _c(?:k|hunk) in iter_stream_chunks\(([^\n]*)\):",
                          code):
        arg = m.group(1).strip()
        # 只看"整段内容"型实参（`xxx[...]` 切片是补吐尾部，llm_content 已赋值）
        if arg.startswith("llm_content[") or arg.startswith("orch_content"):
            continue
        head = code[max(0, m.start() - 800):m.start()]
        if ("llm_content = %s" % arg) not in head:
            bad_order.append(arg[:40])
    ok("节奏化吐字前已先赋值 llm_content（先存后吐）", not bad_order,
       "以下实参未先赋值：%s" % bad_order)

    print("\n=== ② 行为验证：流中途断开（GeneratorExit）后，已流出内容必须落库 ===")
    with db_conn() as conn:
        cid = _new_conv(conn)
        conn.commit()
    pipe = AgentPipeline()
    counts, streamed, err = _run_case(pipe, cid, stop_after_tokens=1)
    print("    帧统计=%s 已流出=%d 字 异常=%r" % (counts, len(streamed), err[:60]))
    ok("确实流出过 token（夹具自证：不是'一开始就失败'）", len(streamed) >= 4,
       "%d 字" % len(streamed))

    with db_conn() as conn:
        rows = _rows(conn, cid)
    asst = [r for r in rows if r["role"] == "assistant"]
    ok("中断后库里有 assistant 消息（旧代码此处 0 条 = 刷新即蒸发）",
       len(asst) == 1, "实际 %d 条" % len(asst))
    if asst and streamed.strip():
        body = asst[0]["content"] or ""
        # ⚠️ 必须带 `streamed.strip()` 非空前置：空串是任何字符串的"子串"，
        #   不加这个守卫，断言会在"什么都没流出"时**假绿**（门禁第一版踩过）。
        ok("落库内容包含失败前已流出的文本", streamed.strip() in body,
           "落库 %d 字 / 已流出 %d 字" % (len(body), len(streamed)))
        ok("落库内容带中断说明（不是裸文本，用户能分辨状态）",
           ("中断" in body) or ("已生成内容" in body))
    else:
        ok("落库内容包含失败前已流出的文本", False, "已流出为空，无法判定（夹具失效）")
        ok("落库内容带中断说明（不是裸文本，用户能分辨状态）", False, "同上")

    print("\n=== ②b 不可双写：同一段内容只能有一条 assistant ===")
    # 再跑一次"断开 → 固化"，库里累计仍应是 2 条（每轮 1 条），不得 4 条
    ok("首轮未产生重复行", len(asst) == 1, "%d 条" % len(asst))

    print("\n=== ③ 变异自证：删掉『留存』两行 ⇒ ②必须变红 ===")
    path = os.path.join(ROOT, "agent/pipeline_parts/stream.py")
    backup = src
    mutated = _mutate_drop_retention(src)
    ok("变异注入成功（锚点存在且确实改动了代码）", mutated != src,
       "" if mutated != src else "锚点找不到：非工具分支结构已变，请更新 _mutate_drop_retention")
    if mutated != src:
        try:
            io.open(path, "w", encoding="utf-8", newline="\n").write(mutated)
            import importlib
            importlib.reload(S)
            importlib.reload(sys.modules["agent.pipeline"])
            from agent.pipeline import AgentPipeline as P2
            with db_conn() as conn:
                cid2 = _new_conv(conn)
                conn.commit()
            counts2, streamed2, _err2 = _run_case(P2(), cid2)
            with db_conn() as conn:
                rows2 = _rows(conn, cid2)
            asst2 = [r for r in rows2 if r["role"] == "assistant"]
            # 内容仍然流出（用户照样看得到），但**没有一条落库** ⇒ 正是用户报的现象
            ok("变异后内容仍流出（证明'看得见'与'存下来'是两件事）", len(streamed2) >= 4,
               "%d 字" % len(streamed2))
            ok("变异后已流出内容不再落库（门禁会响）", not asst2,
               "已流出 %d 字 / assistant %d 条" % (len(streamed2), len(asst2)))
        finally:
            io.open(path, "w", encoding="utf-8", newline="\n").write(backup)
            import importlib
            importlib.reload(S)
            importlib.reload(sys.modules["agent.pipeline"])
            # 逐字节还原自证：防止"看起来还原了其实没还原"（本仓铁律）
            _now = io.open(path, encoding="utf-8").read()
            ok("变异已逐字节还原", _now == backup,
               "" if _now == backup else "还原后内容与备份不一致！")

    print("\n" + "=" * 60)
    print("PASS %d / FAIL %d" % (PASS, FAIL))
    if FAIL:
        print("❌ 断点保护仍有缺口：中途失败会丢掉用户已看到的内容")
        return 1
    print("✅ 断点保护成立：已流出内容在失败后仍可落库、刷新不丢")
    return 0


if __name__ == "__main__":
    sys.exit(main())
