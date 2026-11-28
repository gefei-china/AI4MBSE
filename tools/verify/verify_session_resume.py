# -*- coding: utf-8 -*-
# CI-OPTIONAL: [nightly] tier=nightly（见 tools/gates_manifest.json）：2026-11-24 A4 补登记：刷新断连续跑三条硬约束 + 前端接线静态断言（纯离线）。历史存量脚本此前漏登记，守卫 G5a/G5b 点名
"""验证「刷新断连续跑」（第二档）的三条硬约束 + 接线完整性。

纯离线：不启服务、不连真库（DetachedRegistry 用假 task，前端用静态源码断言）。
运行：  <repo>/.venv/Scripts/python.exe tools/verify/verify_session_resume.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

_RES = []


def rec(name, ok, detail=""):
    _RES.append(bool(ok))
    print("%s %s%s" % ("[OK]  " if ok else "[FAIL]", name,
                       ("  —— " + detail) if detail else ""))


def main():
    from services.detached_stream import should_detach, DetachedRegistry, get_registry

    # ── 约束3：主动停止绝不续跑（最高优先级，穷举） ────────────────────
    rec("S1 主动停止+开关开+有run ⇒ 不续跑",
        should_detach(True, True, 999) is False)
    rec("S2 主动停止+开关关+有run ⇒ 不续跑",
        should_detach(True, False, 999) is False)
    rec("S3 未停止+开关开+有run ⇒ 续跑",
        should_detach(False, True, 999) is True)
    rec("S4 未停止但无run（帧日志关） ⇒ 不续跑",
        should_detach(False, True, 0) is False)
    rec("S5 未停止但总开关关 ⇒ 不续跑",
        should_detach(False, False, 999) is False)

    # ── 停止标记：一次性 + 时间窗 ────────────────────────────────────
    r = DetachedRegistry()
    r.mark_stopped(7, now=100.0)
    rec("M1 停止标记在窗口内命中", r.consume_stopped(7, window_s=30, now=120.0) is True)
    rec("M2 标记一次性消费（第二次为空）", r.consume_stopped(7, now=121.0) is False)
    r.mark_stopped(8, now=100.0)
    rec("M3 超窗标记作废", r.consume_stopped(8, window_s=30, now=200.0) is False)
    r.mark_stopped(9, now=100.0)
    r.clear_stopped(9)
    rec("M4 新一轮开始清旧标记", r.consume_stopped(9, now=101.0) is False)

    # ── 约束1：60s 无人认领自动收口（watcher 判据） ────────────────────
    r2 = DetachedRegistry()
    r2.register(42, 7, task=None, agen=None, now=0.0)
    rec("G1 59s 未到期", r2.expired_ids(60, now=59.0) == [])
    rec("G2 60s 到期", r2.expired_ids(60, now=60.0) == [42])
    rec("G3 touch 认领后续时钟", r2.touch(42, now=60.0) is True
        and r2.expired_ids(60, now=60.0) == [])
    rec("G4 认领后再过 60s 到期", r2.expired_ids(60, now=120.0) == [42])
    rec("G5 不存在的 run touch 返回 False", r2.touch(999, now=1.0) is False)
    rec("G6 按会话查 detached", len(r2.find_by_conv(7)) == 1)

    # ── 竞态覆盖：先断连后点停止 ⇒ /interrupt 能反查并 cancel ──────────
    class _FakeTask:
        def __init__(self):
            self.cancelled = False

        def cancel(self):
            self.cancelled = True

    r3 = get_registry()
    r3._runs.clear()
    r3._stopped_convs.clear()
    ft = _FakeTask()
    r3.register(77, 11, task=ft, agen=None, now=0.0)
    # 模拟会话级 /interrupt 的动作
    r3.mark_stopped(11)
    for d in r3.find_by_conv(11):
        d.reason = "user_stop"
        if d.task is not None:
            d.task.cancel()
    rec("R1 先断连后停止 ⇒ 后台任务被 cancel", ft.cancelled is True)
    rec("R2 停止原因记为 user_stop", r3.get(77).reason == "user_stop")
    rec("R3 停止标记已写（断连判据可读）", r3.consume_stopped(11) is True)
    r3._runs.clear()

    # ── 接线完整性：后端 ─────────────────────────────────────────────
    conv_src = open(os.path.join(ROOT, "routers", "conversations.py"), encoding="utf-8").read()
    rec("B1 chat_stream 断连走 should_detach", "_should_detach(" in conv_src)
    rec("B2 判定下沉到 SSE 包装器 detach_ctx", "detach_ctx={" in conv_src)
    rec("B2b detach 判定后回传内层收口避让标记", "_detach_claimed[0] = True" in conv_src)
    rec("B2c 帧落库下沉 on_frame", '"on_frame": lambda raw:' in conv_src)
    rec("B2d detach 时跳过 run 终态误标", "if _detach_claimed[0]" in conv_src)
    rec("B2e detach 时跳过「已中止」兜底固化", "not _detach_claimed[0]" in conv_src)
    rec("B3 grace 后台收口 watcher", "_detach_start_watcher(" in conv_src)
    rec("B3b on_done 写 run=done", "_detach_mark_done(" in conv_src)
    rec("B4 subscribe 每次 poll touch", "_reg.touch(run_id)" in conv_src)
    rec("B5 live-run 探测端点存在", '/live-run' in conv_src)
    rec("B6 live-run 判 detached registry", "reg.get(rid)" in conv_src)

    sse_src = open(os.path.join(ROOT, "core", "sse.py"), encoding="utf-8").read()
    rec("B10 SSE 包装器接受 detach_ctx", "detach_ctx=None" in sse_src)
    rec("B11 detach 时保活 worker（跳过 stop/close）", "_want_detach" in sse_src and "return          # ★ 关键" in sse_src)
    rec("B12 后台 drain 调 on_frame 落库", 'detach_ctx.get("on_frame")' in sse_src)
    rec("B13 worker_done 供判定整轮是否结束", 'box["worker_done"]' in sse_src)

    orch_src = open(os.path.join(ROOT, "routers", "orchestration.py"), encoding="utf-8").read()
    rec("B7 run 级 /interrupt 写停止标记+取消 detached",
        "mark_stopped(_d.conv_id)" in orch_src and "_d.task.cancel()" in orch_src)
    rec("B8 会话级 /interrupt 写停止标记+遍历 detached",
        "_reg.mark_stopped(conv_id)" in orch_src and "find_by_conv(conv_id)" in orch_src)

    cfg_src = open(os.path.join(ROOT, "core", "config.py"), encoding="utf-8").read()
    rec("B9 配置节 session_resume.detach_grace_s=60",
        '"detach_grace_s": 60' in cfg_src or '"detach_grace_s": 60.0' in cfg_src)

    # ── 约束2：续连不重复输出（前端静态断言） ────────────────────────
    send_src = open(os.path.join(ROOT, "static", "js", "mods", "12-chatsend.js"),
                    encoding="utf-8").read()
    rec("F1 记录 run-seq 游标", "_recordLiveRun(_cid, +_m[1], +_m[2])" in send_src)
    rec("F2 终态到达清游标", "window._sseTerminal" in send_src and "_clearLiveRun(_cid)" in send_src)
    rec("F3 续显带游标订阅", "last_event_id=${encodeURIComponent(lastEventId)}" in send_src)
    rec("F4 续显用 fetch+getReader 增量读流", "resp.body.getReader()" in send_src)
    rec("F5 续显收口后重载历史（替换式，不重复）",
        "selectConv(convId)" in send_src and "liveReplay" in send_src)
    rec("F6 续显期间发新消息先停旧 run", "window._liveReplayRunId" in send_src
        and "/interrupt" in send_src)
    rec("F7 错误文案不再断言后台必停", "本轮可能在后台继续执行" in send_src)
    chat_src = open(os.path.join(ROOT, "static", "js", "mods", "03-chat.js"),
                    encoding="utf-8").read()
    # ★ 2026-11-28 判据升级（run 205 真机复盘）：续显探测已收敛为单一入口 _autoResumeRun。
    #   原判据断言 "_resumeLiveStream(id, area)" 出现 ≥2 次 —— 那是**实现细节**，
    #   重构后次数变 0 就误报。现按语义断言：两个入口（空消息分支 / 渲染完成分支）
    #   都必须挂上探测，且只能有一个入口负责“要不要续”，避免同一 run 起两个 SSE。
    rec("F8 会话加载后挂载续显探测",
        "function _autoResumeRun(" in send_src
        and "_autoResumeRun(id, area)" in chat_src
        and "maybeOfferRunReplay(id, area)" in chat_src)
    rec("F9 自动续显以游标或服务端帧日志为触发依据（非末条消息猜测）",
        "const rec = _getLiveRun(convId);" in send_src
        and "if(!rec || !(Number(rec.run_id)>0)) return" in send_src
        and "seed && Number(seed.run_id) > 0" in send_src)
    rec("F10 正文已固化⇒重载历史（不弹按钮/不重复）",
        "if(rc && rc.found)" in send_src and "_clearLiveRun(convId);" in send_src)
    rec("F11 active=false 不再直接 return（消除手动按钮根因）",
        "if(!info || !info.active){" not in send_src)
    # ★ 2026-11-28 判据升级：由“有游标时让位”升级为“**能自动续就不许弹按钮**”。
    #   原判据只覆盖“有 sessionStorage 游标”这一种情况，漏掉“游标丢了但服务端帧日志还在”
    #   —— 那正是真机 run 205 的现场（第二次刷新后游标被清 ⇒ 退化成手动按钮 ⇒
    #   无人认领 ⇒ 60s grace 到期 ⇒ 该轮被 cancelled）。
    rec("F12 能自动续显时手动回放入口必须让位（含游标丢失、靠 recovery 兜底的路径）",
        "if(await _autoResumeRun(convId, area)) return;" in send_src)
    # ★ 2026-11-28 新增：三条防回归判据（run 205 的根因与修复点）
    rec("F20 续显异常不得清游标（根因：一次刷新失败即永久失去续订凭据）",
        "console.warn('[live-resume] 续显中断，保留游标待下次校正" in send_src
        and "续显失败静默收口" not in send_src)
    rec("F21 续显探测只有单一入口（防同一 run 起两个 SSE 重复追加 token）",
        chat_src.count("_resumeLiveStream(id, area)") == 0
        and "_autoResumeRun" in send_src)
    rec("F22 recovery 作为权威兜底（用 stream_run_id + stream_last_seq 重建游标）",
        "stream_run_id" in send_src and "stream_last_seq" in send_src
        and "_resumeLiveStream(convId, area, {run_id: rid" in send_src)

    # ── 刷新后停留在原会话（不回欢迎页）─────────────────────────────
    rec("F13 selectConv 记住最后会话", "localStorage.setItem('mbse_last_conv_id'" in chat_src)
    rec("F14 新建任务/删除时清记录",
        chat_src.count("localStorage.removeItem('mbse_last_conv_id')") >= 2)
    rec("F15 启动恢复回调存在且只执行一次",
        "_restoreLastConvOnReload" in chat_src and "_done = true" in chat_src)
    rec("F16 恢复走 goToConversation（含切页+加载）",
        "goToConversation(Number(last))" in chat_src)

    # ── 全页面路由恢复（不止 AI 会话页）──────────────────────────────
    shell_src = open(os.path.join(ROOT, "static", "js", "mods", "02-shell.js"),
                     encoding="utf-8").read()
    search_src = open(os.path.join(ROOT, "static", "js", "mods", "33-search.js"),
                      encoding="utf-8").read()
    rec("F17 go() 记录当前路由到 localStorage",
        "localStorage.setItem('mbse_current_route'" in shell_src)
    rec("F18 启动入口恢复路由（不再无条件 go('ai')）",
        "_bootRoute" in search_src and "localStorage.getItem('mbse_current_route')" in search_src
        and "if(!restored) go('ai');" in search_src)
    rec("F19 会话恢复在目标非 AI 页时让位",
        "_routeP && _routeP !== 'ai'" in chat_src)

    ok = all(_RES)
    print("\n=== 结果：%d/%d 通过 ===" % (sum(_RES), len(_RES)))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
