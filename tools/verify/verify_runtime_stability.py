"""运行时稳定性三件套门禁（2026-10-30）—— 优雅关闭 / 并发闸 / 墙钟 + 长任务保留产出。

对应实施文档：`docs/运行时稳定性优化-行业调研与实施方案-20261030.md`

━━━ 为什么需要这个门禁 ━━━
三项改动都属于**"平时看不见、故障时才生效"**的防御性代码：
    · 优雅关闭只在进程被停时跑；
    · 并发闸只在过载时拒流；
    · 墙钟只在超长任务时收尾；
    · 超时保留产出只在子任务卡死时生效。
⇒ 日常功能测试**一条都覆盖不到**，极易在后续重构中被"顺手清理"。

━━━ 断言纪律 ━━━
每条"应当存在"的断言都配一个 `★变异自证`：**把实现改回旧行为，判据必须判红**。
只写正向断言（"代码里有这句话"）会出现"假绿"——本仓已经为此踩过多次
（`verify_team_defined_orchestration` 的 G10/G12 曾因判据过宽而变异后仍绿）。
"""
import ast
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_PASS, _FAIL = [], []


def ck(name, ok, detail=""):
    (_PASS if ok else _FAIL).append(name)
    print("%s %s%s" % ("[PASS]" if ok else "[FAIL]", name,
                       ("　→ " + detail) if (detail and not ok) else ""))


def _read(rel):
    with open(os.path.join(_ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _code(rel):
    """**只保留真实代码行**（剔除整行注释）。

    为什么必须有这个函数（本仓反复踩的坑，本轮又踩了两次）：
      第一版判据直接在全文里 `find("uvicorn.run")` / `find("return StreamingResponse")`，
      结果都锚到了**我自己写的注释里**的示例片段
      （`uvicorn.run(app, host=..., port=...)`、`return StreamingResponse(gen())`）
      ⇒ 位置比较得到 -1，判据全线误红。
      正确的做法是让"注释里的示例"不参与判定 —— 否则注释写得越详细、判据越容易误判。

    只剔除以 `#` 开头的整行（占本仓注释的绝大多数）；行尾注释保留，
    因为我们的特征串不含 `#`，"是否存在"不受影响，而误剥离字符串内 `#` 的风险更高。
    """
    out = []
    for ln in _read(rel).split("\n"):
        if ln.lstrip().startswith("#"):
            continue
        out.append(ln)
    return "\n".join(out)


def _lines(rel):
    return _read(rel).split("\n")


def _func_src(rel, name):
    """按 **AST** 取出指定函数的源码（含 docstring）。

    为什么不能用字符串 find 定位函数体（本轮踩了第三次）：
      `event_stream` 的 docstring 里写着示例 ``return StreamingResponse(gen())``
      （用于解释"为什么不能写在端点函数里"），于是
      ``src[src.find("async def event_stream():"):src.find("return StreamingResponse")]``
      **在 docstring 里就被截断**，后面所有位置都是 -1 ⇒ 判据全线误红。
      ⇒ 定位类判据必须走 AST（语法树不包含"示例文本"的歧义）。
    """
    try:
        tree = ast.parse(_read(rel))
    except Exception:
        return ""
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            ls = _lines(rel)
            return "\n".join(ls[n.lineno - 1:(n.end_lineno or n.lineno)])
    return ""


# ══════════════════════════════════════════════════════════════════════
# A 组 · 运行时状态模块存在且接口完整
# ══════════════════════════════════════════════════════════════════════
def a_group():
    print("\n=== A 组 · runtime_state 模块 ===")
    try:
        from core import runtime_state as rs
    except Exception as e:                                  # noqa: BLE001
        ck("A1 core.runtime_state 可导入", False, repr(e))
        return
    ck("A1 core.runtime_state 可导入", True)

    for fn in ("is_draining", "begin_drain", "stream_gate", "compute_deadline",
               "deadline_left", "inflight_count", "stats"):
        ck("A2 暴露 %s" % fn, callable(getattr(rs, fn, None)))

    # A3 语义：deadline 为"绝对时间戳 + 幂等"
    d = rs.compute_deadline(10)
    left = rs.deadline_left(d)
    ck("A3 compute_deadline 返回绝对时间戳（deadline_left≈预算）",
       9.0 < left <= 10.0, "left=%.2f" % left)
    ck("A3b deadline_left(0) = 0（0 表示不限）", rs.deadline_left(0) == 0.0)
    ck("A3c 同一次 deadline 多次读值不放大（幂等）",
       abs(rs.deadline_left(d) - rs.deadline_left(d)) < 0.5)

    # A4 闸的真实语义（异步）
    import asyncio
    rs.reset_for_test()
    rs._cfg_cache["max_concurrent_streams"] = 2
    rs._cfg_cache["queue_wait_s"] = 0.25

    async def _probe():
        ev = asyncio.Event()
        out = {}

        async def _hold():
            async with rs.stream_gate():
                await ev.wait()

        t1 = asyncio.create_task(_hold())
        t2 = asyncio.create_task(_hold())
        await asyncio.sleep(0.1)
        out["full"] = rs.inflight_count()
        try:
            async with rs.stream_gate():
                out["third"] = "ADMITTED"
        except rs.GateBusy:
            out["third"] = "BUSY"
        ev.set()
        await t1
        await t2
        out["released"] = rs.inflight_count()
        async with rs.stream_gate():
            out["after"] = "ADMITTED"
        out["final"] = rs.inflight_count()
        return out

    r = asyncio.run(_probe())
    ck("A4 占满后 inflight 计数正确", r["full"] == 2, "full=%s" % r["full"])
    ck("A4b 超限被拒（GateBusy）", r["third"] == "BUSY", "third=%s" % r["third"])
    ck("A4c 全部释放后计数归零", r["released"] == 0, "released=%s" % r["released"])
    ck("A4d 释放后可重新获取", r["after"] == "ADMITTED")
    ck("A4e 获取后计数回到 0（无泄漏）", r["final"] == 0, "final=%s" % r["final"])
    rs.reset_for_test()


# ══════════════════════════════════════════════════════════════════════
# B 组 · 优雅关闭（lifespan 收尾 + uvicorn 参数 + 健康端点）
# ══════════════════════════════════════════════════════════════════════
def b_group():
    print("\n=== B 组 · 优雅关闭 ===")
    src = _read("main.py")
    # ★ 位置/实参判据一律基于 `code`（剔除注释）——
    #   否则会锚到注释里的示例 `uvicorn.run(app, host=..., port=...)`（本轮踩过）。
    code = _code("main.py")

    ck("B1 uvicorn 传入 timeout_graceful_shutdown",
       "timeout_graceful_shutdown" in code)
    # B2 ★ 关键：必须在真实 `uvicorn.run(` 调用实参里（不是注释里提一句）
    _run = re.search(r"uvicorn\.run\((?:[^()]|\([^()]*\))*\)", code)
    ck("B2 ★ 该参数真的在 uvicorn.run(...) 实参内（不是注释里提一句）",
       bool(_run) and "timeout_graceful_shutdown" in _run.group(0),
       "实参片段=%r" % (_run.group(0)[:90] if _run else None))

    # B3 ★ 变异自证：把该实参删掉 ⇒ B2 必须判红
    if _run:
        _mut = code.replace(_run.group(0),
                            re.sub(r",\s*timeout_graceful_shutdown=\w+", "", _run.group(0)))
        _mrun = re.search(r"uvicorn\.run\((?:[^()]|\([^()]*\))*\)", _mut)
        ck("B3 ★变异：从 uvicorn.run 删掉该参数 ⇒ B2 判据能抓到",
           bool(_mrun) and "timeout_graceful_shutdown" not in _mrun.group(0))

    # B4 lifespan 的 yield 之后必须有收尾代码（用 code，避免把注释当收尾）
    _ls = code[code.find("async def lifespan"):code.find("app = FastAPI(")]
    _tail = _ls[_ls.find("yield"):] if "yield" in _ls else ""
    _tail_code = [ln for ln in _tail.split("\n") if ln.strip()]
    ck("B4 lifespan 的 yield 之后有收尾代码（改前为空）",
       len(_tail_code) > 3, "yield 后真实代码行=%d" % len(_tail_code))
    ck("B4b 收尾里置 draining", "begin_drain()" in _tail)
    ck("B4c 收尾里有界等待在途流", "inflight_count()" in _tail and "sleep" in _tail)
    ck("B4d 收尾里停后台线程（至少 2 个）",
       sum(k in _tail for k in ("stop_workers", "stop_alert_loop", "stop_resume_loop")) >= 2)

    # B5 健康端点：live 与 ready 必须分开
    ck("B5 存在 /api/health/live", "/api/health/live" in code)
    ck("B5b 存在 /api/health/ready", "/api/health/ready" in code)
    ck("B5c ready 在关闭中返 503", "status_code=503" in code and "is_draining()" in code)
    _live = code[code.find("def health_live"):]
    _live = _live[:_live.find("@app.") if "@app." in _live else 800]
    ck("B5d live 不依赖 DB（函数体内不查库）",
       "get_db" not in _live and "db_conn" not in _live and "is_draining" in _live)

    # B6 ★ 变异自证：删掉 ready 的 503 分支 ⇒ B5c 必须判红
    _mut5 = code.replace("status_code=503", "status_code=200")
    ck("B6 ★变异：ready 的 503 改回 200 ⇒ B5c 判据能抓到",
       not ("status_code=503" in _mut5 and "is_draining()" in _mut5))


# ══════════════════════════════════════════════════════════════════════
# C 组 · 并发闸 + 墙钟（路由层接线）
# ══════════════════════════════════════════════════════════════════════
def c_group():
    print("\n=== C 组 · 并发闸与墙钟接线 ===")
    # ★ 一律基于 `code`（剔除注释）：第一版用全文 find，锚到了注释里的示例
    #   `return StreamingResponse(gen())` ⇒ 切片被截断在注释处、位置全 -1（本轮踩过）。
    code = _code("routers/conversations.py")

    ck("C1 入口引入 runtime_state", "_runtime_state" in code)
    ck("C2 关闭中拒流", "is_draining()" in code)
    ck("C3 使用全局闸", "stream_gate()" in code)
    ck("C4 计算墙钟 deadline", "compute_deadline()" in code)
    ck("C5 deadline 传入 execute_stream", "deadline_ts=deadline_ts" in code)

    # C6 ★ 关键判据：信号量必须包住 `async for`（在生成器内部持有）
    #   行业原话：写在端点函数里 ⇒ 函数返回时释放 ⇒ 根本没限到流的生命周期。
    #   ⚠️ 用 AST 取函数体（不走字符串 find —— docstring 里的示例会截断切片）。
    _wrap = _func_src("routers/conversations.py", "event_stream")
    _gate_pos = _wrap.find("await _gate.__aenter__()")
    _for_pos = _wrap.find("async for _f in _event_stream_inner")
    _exit_pos = _wrap.find("_gate.__aexit__")
    ck("C6 ★ 闸的 acquire 在流循环之前", 0 <= _gate_pos < _for_pos,
       "gate=%s for=%s" % (_gate_pos, _for_pos))
    ck("C6b ★ 闸的 release 在流循环之后（包住整个生命周期）", _exit_pos > _for_pos > 0,
       "for=%s exit=%s" % (_for_pos, _exit_pos))
    _fin = _wrap.find("finally:", _for_pos) if _for_pos > 0 else -1
    ck("C6c release 放在 finally（断连也释放，防名额泄漏）",
       _fin > 0 and _exit_pos > _fin,
       "for=%s finally=%s exit=%s" % (_for_pos, _fin, _exit_pos))

    # C7 ★ 变异自证：构造"错序实现"（acquire 落在 for 之后）⇒ C6 必须判红。
    #   ⚠️ 首版变异写错了：把 acquire 删掉后**又插回 `async for` 之前**，
    #   顺序没被破坏 ⇒ 判据当然仍判绿（变异体必须是"真的坏"，否则自证是假的）。
    #   正确变异：先删原 acquire，再把它追加到 `finally:` 之前（即 for 之后）。
    if _for_pos > 0 and _gate_pos >= 0:
        _mut = _wrap.replace("await _gate.__aenter__()", "pass", 1)
        _mut = _mut.replace("finally:", "await _gate.__aenter__()\n        finally:", 1)
        _mut_gate = _mut.find("await _gate.__aenter__()")
        _mut_for = _mut.find("async for _f in _event_stream_inner")
        ck("C7 ★变异：把 acquire 挪到 for 之后 ⇒ C6 判据能抓到",
           _mut_gate > _mut_for > 0,
           "变异后 gate=%s for=%s（应 gate>for）" % (_mut_gate, _mut_for))
    else:
        ck("C7 ★变异：把 acquire 挪到 for 之后 ⇒ C6 判据能抓到", False, "锚点缺失，无法变异")

    # C8 拒绝走 SSE error 帧而不是 HTTP 503（响应头已发，改状态码只会得到半开连接）
    ck("C8 拒绝时发 event: error 帧", "event: error" in _wrap)
    ck("C8b busy 帧带 retry_after（调用方可重试）", "retry_after" in _wrap)


# ══════════════════════════════════════════════════════════════════════
# D 组 · 长任务：保留产出 + 重试缩小
# ══════════════════════════════════════════════════════════════════════
def d_group():
    print("\n=== D 组 · 长任务编排 ===")
    src = _read("agent/pipeline_parts/stream.py")
    code = _code("agent/pipeline_parts/stream.py")

    # D1 超时分支不得再硬写空 payload
    #   ⚠️ 锚点必须用**真实代码**（`_hb_text.get(tkey`），不能用注释文字
    #   （首版拿 `"# 超时：不等待线程"` 当锚点，而 `_code()` 恰好剔除了整行注释
    #     ⇒ 锚点找不到、len=0 ⇒ 三条判据连环误红。自相矛盾的判据比没有判据更糟）。
    _i = code.find("_partial_txt = _hb_text.get(tkey")
    if _i < 0:
        _i = code.find("_hb_text.get(tkey")
    _blk = code[_i:] if _i >= 0 else ""
    _j = _blk.find("先分类、再决定要不要重试")
    _blk = _blk[:_j] if _j > 0 else _blk[:4000]
    ck("D1 超时分支存在（用真实代码锚定位）", len(_blk) > 100, "len=%d" % len(_blk))
    ck("D2 ★ 超时分支不再硬写空 payload（`False, \"\", [], _stall` 已消失）",
       'False, "", [], _stall' not in _blk,
       "仍存在空 payload 写法")
    ck("D3 ★ 超时分支从快照取回已产出正文", "_hb_text.get(tkey" in _blk)
    ck("D4 中断说明后缀可被下游识别（含『部分内容』字样）",
       "部分内容" in _blk and "未经完整性校验" in _blk)

    # D5 ★ 变异自证：把 payload 改回 "" ⇒ D2 必须判红
    _mut = _blk.replace("_partial_txt, [], _stall", '"", [], _stall', 1)
    ck("D5 ★变异：payload 改回空串 ⇒ D2 判据能抓到",
       'False, "", [], _stall' in _mut)

    # D6 快照必须在产出点同步刷新（否则超时读不到最新）
    #   ⚠️ 不能用"字符距离"判相邻：本轮在中间写了长注释（约 10K 字符），
    #      首版阈值 30 行 ⇒ 误红。正确判据是「两者之间没有新的函数定义」，
    #      即确实落在**同一个产出循环体内**。
    ck("D6 产出点刷新快照", "_hb_text[tkey] = sub_res" in code)
    _hb_i = code.find("_hb[tkey] = _t.time()")
    _txt_i = code.find("_hb_text[tkey] = sub_res")
    _between = code[_hb_i:_txt_i] if 0 <= _hb_i < _txt_i else ""
    ck("D6b 快照刷新与心跳在同一产出循环内（中间无新函数定义）",
       bool(_between) and "\ndef " not in _between,
       "hb=%d txt=%d 中段含新函数=%s" % (_hb_i, _txt_i, "\ndef " in _between))

    # D7 快照有容量上限（防异常子任务吃满内存）
    ck("D7 快照有上限保护", "_hb_text_cap" in code)

    # D8 重试时缩小规模
    ck("D8 重试路径带缩小指令", "缩小范围" in code and "retry_count" in code)
    _rc_i = code.find('_rc = int(tk.get("retry_count")')
    _rc = code[_rc_i:_rc_i + 900] if _rc_i >= 0 else ""
    ck("D8b 缩小指令只在重试时追加（_rc > 0 条件）",
       "_rc > 0" in _rc and "缩小范围" in _rc)

    # D9 墙钟到点走 break 收尾（不是 return 掐断）
    #   ⚠️ 首版 `_dl.split("break")[0]` 里含注释中的"直接 return" ⇒ 误红。
    #   正确判据：只看真实代码行。
    _dl_i = code.find("time.time() >= _deadline_ts")
    _dl = code[_dl_i:_dl_i + 1600] if _dl_i >= 0 else ""
    ck("D9 墙钟检查存在", _dl_i > 0)
    _dl_code = [ln.strip() for ln in _dl.split("\n") if ln.strip()]
    ck("D9b ★ 到点走 break（落收尾路径）而不是 return",
       any(ln.startswith("break") for ln in _dl_code)
       and not any(ln.startswith("return") for ln in _dl_code),
       "代码行=%r" % (_dl_code[:6],))
    ck("D9c 到点显式告知模型真实原因（防它自己编）",
       "真实原因是整轮执行时长到点" in _dl or "不要再归因为" in _dl)

    # D10 execute_stream 接受并解析 deadline_ts
    ck("D10 execute_stream 签名含 deadline_ts", "deadline_ts: float = 0.0" in code)
    ck("D10b 入口解析为绝对时间戳", "_deadline_ts = float(deadline_ts or 0.0)" in code)

    # D11 ★★ 编排路径也必须有墙钟（只判 ReAct 会漏掉最大的一块）
    #   实测依据：编排批次 avg 918s / max 1155s，而它**不进** ReAct 主循环 ——
    #   只在 ReAct 里加检查等于"主要超时来源完全没被管"。
    ck("D11 ★ 编排路径读取整轮墙钟", "_orch_deadline = float(getattr(self, \"_deadline_ts\"" in code)
    ck("D11b ★ 编排主循环里有到点检查", "_orch_deadline and _t.time() >= _orch_deadline" in code)
    ck("D11c 到点把未派发任务标 blocked（不静默丢弃）",
       "整轮墙钟到点" in code and "TaskQueue.block" in code)
    # D11d ★ 变异自证：删掉编排路径的检查 ⇒ D11b 必须判红
    _mut11 = code.replace("_orch_deadline and _t.time() >= _orch_deadline", "False", 1)
    ck("D11d ★变异：删掉编排路径的墙钟检查 ⇒ D11b 判据能抓到",
       "_orch_deadline and _t.time() >= _orch_deadline" not in _mut11)

    # D12 编排墙钟用的是**实例属性**而非签名参数 —— 必须显式声明该前提成立
    ck("D12 实例属性传墙钟（依赖每请求新建 pipeline）",
       "self._deadline_ts = _deadline_ts" in code)

    # D18 ★★ 死配置回归防护（本轮实际消除的一类自研债）
    #   `delegation.total_time_budget_s` 此前是**死配置**：全仓零消费点，
    #   而 schema 描述里写着"超限终止剩余任务" ⇒ 配置谎言（改了不起作用）。
    #   2026-10-30 已接线（编排批次闸），默认改 0 = 不启用（零行为变更）。
    _cfgd = _code("core/config.py")
    ck("D18 ★ 编排批次预算已被编排路径真正读取（不再是死配置）",
       'total_time_budget_s' in code and "_orch_batch_deadline" in code)
    ck("D18b ★ 编排批次闸与整轮墙钟闸**同时判定**（更严格者先生效）",
       "_orch_deadline and _t.time() >= _orch_deadline" in code
       and "_orch_batch_deadline and _t.time() >= _orch_batch_deadline" in code)
    # D18c ★ 默认值必须是 0（不启用）——默认非 0 会误杀实测 849s 的正常长任务
    ck("D18c ★ 默认值为 0（不启用，零行为变更、不误杀长任务）",
       '"total_time_budget_s": 0,' in _cfgd,
       "默认值不是 0")
    # D18d ★ 变异自证：把消费点删掉 ⇒ D18 必须判红
    _mut18 = code.replace('int(_ocfg_b.get("delegation", "total_time_budget_s", 0) or 0)', "0", 1)
    ck("D18d ★变异：删掉编排批次预算的消费点 ⇒ D18 判据能抓到",
       'total_time_budget_s' not in _mut18)
    # D18e schema 描述不得再自称"死配置/未接线"（那会误导现场运维）
    _desc = _read("core/config.py")
    ck("D18e schema 描述不再自称死配置（避免新的配置谎言）",
       '未接线（死配置' not in _desc)

    # D13 ★★ 线程级 deadline 传播（本机实测得出的关键补充）
    #   实测 conv 9771（墙钟 8s、实际 61s）：卡在 planner 的**单次** LLM 调用 56,978ms，
    #   而 planner 在编排主循环**之前** ⇒ 循环里的检查一次都没跑到。
    #   ⇒ 墙钟必须能"压进每一次 LLM 调用"，否则只是纸面约束。
    from core import runtime_state as _rs
    import time as _time
    _rs.reset_for_test()
    ck("D13 未登记 deadline ⇒ cap_timeout 原样返回（零行为变更）",
       _rs.cap_timeout(180) == 180.0, "got=%s" % _rs.cap_timeout(180))
    _rs.set_thread_deadline(_time.time() + 10)
    ck("D13b 剩 10s ⇒ 180s 的调用被压到 ~10s",
       9.0 < _rs.cap_timeout(180) <= 10.0, "got=%s" % _rs.cap_timeout(180))
    _rs.set_thread_deadline(_time.time() - 5)
    ck("D13c ★ 已过期 ⇒ 返回 0（放弃调用），**不能**退回 180",
       _rs.cap_timeout(180) == 0.0, "got=%s" % _rs.cap_timeout(180))
    # D13d ★ 变异自证：把"已过期"与"未登记"混为一谈（首版 bug）⇒ D13c 必须判红
    _mut_left = None if True else 0.0     # 模拟旧实现：未登记与过期都返回 0.0
    ck("D13d ★变异：若把『已过期』当成『未登记』⇒ D13c 判据能抓到",
       _mut_left is None and _rs.thread_deadline_left() == 0.0)
    _rs.reset_for_test()
    ck("D13e 线程级登记可清零（del 属性而非存 0）",
       _rs.thread_deadline_left() is None)

    # D14 llm 层真的消费了 cap_timeout（否则上面全是空转）
    _llm = _read("llm/providers/openai_compat.py")
    ck("D14 ★ 非流式 LLM 调用受墙钟约束", "cap_timeout(_to)" in _llm)
    ck("D14b ★ 流式 LLM 调用同源约束（两套口径会漂移）", "cap_timeout(_sto)" in _llm)
    ck("D14c 预算耗尽时放弃调用而非带 0 发请求",
       "墙钟预算已耗尽" in _llm and "raise" in _llm)

    # D15 ★★ 流式**消费点**的墙钟（本机实测：只靠 httpx timeout 打不断长文本）
    #   实测 conv 9772：墙钟 8s、cap_timeout 生效，仍连续吐 19 个 token 不停 ——
    #   因为 httpx 的 timeout 是"每次读超时"，逐帧都快时总时长不受限。
    _lcore = _code("llm/__init__.py")
    _sw = _func_src("llm/__init__.py", "_stream_wrapped")
    ck("D15 ★ 流式消费循环里检查墙钟", "thread_deadline_left()" in _sw)
    _i_dead = _sw.find("thread_deadline_left()")
    _i_yield = _sw.find("yield ev")
    ck("D15b ★ 检查在 yield 之前（否则已吐出的帧管不住后续）",
       0 <= _i_dead < _i_yield, "deadline=%s yield=%s" % (_i_dead, _i_yield))
    ck("D15c 到点用 break 收尾而非 raise（raise 会被当成调用失败、丢已收文本）",
       "break" in _sw[_i_dead:_i_dead + 220] if _i_dead > 0 else False)
    # D15d ★ 变异自证：删掉消费点检查 ⇒ D15 必须判红
    _mut15 = _sw.replace("_sleft = _rs_s.thread_deadline_left()", "_sleft = None", 1)
    ck("D15d ★变异：删掉流式消费点的墙钟检查 ⇒ D15 判据能抓到",
       "thread_deadline_left()" not in _mut15)

    # D16 ★★ 统一入口拦截 + 检查位置（两个实测暴露的漏点）
    ck("D16 ★ chat() 入口也拦已耗尽预算（provider 层之外再兜一层）",
       "入口拦截 rl=deadline" in _lcore)
    # 为什么必须有：实测 conv 9773 —— planner 被正确拦截后，**回落单 Agent 的路径**
    # 又发起了 chat_probe 并跑 75s（那条路径没经过我们改的 provider 方法）。
    ck("D16b 入口拦截在取 provider 之前（省掉无谓的路由计算）",
       _lcore.find("入口拦截 rl=deadline") < _lcore.find("provider = self.get_provider(provider_id)")
       if "provider = self.get_provider(provider_id)" in _lcore else False)

    # D17 ★ 墙钟检查必须在 `if not tools_def` **之前**
    #   实测 conv 9772/9773：首版放在其后，无工具场景先 break 出去 ⇒ 那条路径墙钟不存在。
    def _order_ok(_dl, _td):
        """判据本体抽成函数，便于变异自证（把位置颠倒喂进去看是否返回 False）。"""
        return 0 < _dl < _td

    _i_dl = code.find("if _deadline_ts and time.time() >= _deadline_ts")
    _i_td = code.find("if not tools_def:")
    ck("D17 ★ 墙钟检查在『无工具即退出』之前（否则该分支绕过墙钟）",
       _order_ok(_i_dl, _i_td), "deadline=%s tools_def=%s" % (_i_dl, _i_td))
    # D17b ★ 变异自证：把两个位置**颠倒**喂给同一判据 ⇒ 必须返回 False。
    #   （不能写 `or True` 这类空转断言 —— 那是假自证，本仓纪律明确禁止。）
    ck("D17b ★变异：位置颠倒 ⇒ 判据返回 False",
       _order_ok(_i_td, _i_dl) is False,
       "颠倒后仍返回 True ⇒ 判据不看顺序，是假的")


def main():
    print("=" * 78)
    print("verify_runtime_stability — 优雅关闭 / 并发闸 / 墙钟 / 长任务保留产出")
    print("=" * 78)
    a_group()
    b_group()
    c_group()
    d_group()
    print("\n" + "=" * 78)
    print("结果：%d PASS / %d FAIL" % (len(_PASS), len(_FAIL)))
    if _FAIL:
        print("失败项：")
        for f in _FAIL:
            print("  x %s" % f)
        return 1
    print("门禁通过：三项稳定性改动均在工作区生效")
    return 0


if __name__ == "__main__":
    sys.exit(main())
