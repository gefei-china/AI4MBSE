# -*- coding: utf-8 -*-
"""P0-2b（2026-10-04 整改）：编排自动重投守护。

## 为什么需要它（不是"锦上添花"，是批次 1~3 留下的最后一环）
批次 1 只会把中断的编排判成 `failed`，批次 2/3 补上了 checkpoint + 断点续跑
（`POST /api/orchestration/runs/{id}/resume`）。但**恢复要人点**：
- 实测孤儿批次 16 批 / 最老 14.1 天，`agent_tasks` 里 41 条任务一直躺着，
  没有任何机制告诉任何人"它死了"；
- 会话打开时前端给一条"继续编排"提示条，但用户不点，它就一直不动。

所以缺的只是**一个闹钟**：周期性扫描失活批次 → 走**与人工点按钮完全相同**的
判定与执行路径 → 自动重投。

## 为什么不写"自动恢复"而叫"守护 + 显式开关"
自动重投有真实代价（可能重复消耗 LLM 额度）。因此：
- `config.orchestration.auto_resume.enabled` **默认 off** —— 与批次 1 的孤儿回收
  （`run_registry.reap_on_startup=True`，启动时无条件跑）刻意不同：
  回收只改状态、零副作用；重投会真的调 LLM。
- 开启时也只处理「心跳已超时 + 非终态 + 仍有未完成任务 + 未超重投上限」的批次，
  由 `orch_checkpoint.prepare_resume` 判定，**本模块不重复实现任何策略**。

## 关键实现约束（都是踩过的坑）
1. **必须复用 `execute_stream(resume_run_id=...)`**：它才是唯一正确的驱动路径
   （绕过意图识别与 planner 启发式，沿检查点直达）。若守护自己另写一套执行逻辑，
   就会与路由版本漂移 —— 这正是 2026-10-04 修掉的 `/chat/stream` 回归的同型错误。
2. **触发前必须 `touch`**：否则两个守护轮次之间会重复触发同一批次。
   `prepare_resume` 里的 `is_alive` 闸门只看心跳，`touch` 之后本轮就跳过了。
3. **执行必须放在独立 daemon 线程**：单轮编排实测 344~1155 s，若在守护扫描线程里
   同步跑，扫描线程会被占住，整轮守护停摆。
4. **绝不在守护里吞异常而不留痕迹**：每次重投写一条审计（复用 `audit()`），
   否则"为什么这个批次又被重跑了"无从追查。
"""
import threading
import time

__all__ = ["scan_once", "start_resume_loop", "stop_resume_loop", "supervisor_status"]

_loop_thread = None
_loop_stop = threading.Event()
_state = {
    "started_at": 0.0,
    "scans": 0,
    "resumed": 0,
    "failed": 0,
    "skipped_alive": 0,
    "last_scan_at": 0.0,
    "last_error": "",
    "inflight": {},        # run_id -> 线程启动时间戳（防同批次并发重投）
    "last_runs": [],       # 最近若干次动作，供 /api/monitor 展示
}
_MAX_HISTORY = 20


def _record(action: str, run_id: int, detail: str = "") -> None:
    """把一次守护动作记进内存历史（供可观测；进程重启即丢，是刻意的）。"""
    _state["last_runs"].append({
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "action": action,
        "run_id": int(run_id or 0),
        "detail": (detail or "")[:200],
    })
    del _state["last_runs"][:-_MAX_HISTORY]


def supervisor_status() -> dict:
    """守护状态快照（供 /api/monitor/orchestration-supervisor）。"""
    inflight = dict(_state.get("inflight") or {})
    return {
        "running": bool(_loop_thread and _loop_thread.is_alive()),
        "uptime_s": round(time.time() - _state["started_at"], 1) if _state["started_at"] else 0.0,
        "scans": _state["scans"],
        "resumed": _state["resumed"],
        "failed": _state["failed"],
        "last_scan_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(_state["last_scan_at"]))
        if _state["last_scan_at"] else "",
        "last_error": _state["last_error"],
        "inflight": inflight,
        "recent": list(reversed(_state["last_runs"])),
    }


def _drive(run_id: int, conv_id: int, branch: str, conversation_id_owner: int = 0) -> None:
    """在一个自建 daemon 线程里跑完一次恢复执行（**不复用路由的 SSE 壳**）。

    与路由的区别只有一处：路由把事件流给客户端，本守护**消费掉即可**
    （无人观看；但落库、审计、phase 收尾全由 execute_stream 自己做，与人工恢复一致）。
    """
    from agent import AgentPipeline
    agent = AgentPipeline()
    n = 0
    for _ev in agent.execute_stream("", conv_id, branch or "dev", None, [],
                                    resume_run_id=int(run_id),
                                    scope=None, user=None):
        n += 1
    _record("resumed", run_id, "自动重投执行完成，事件 %d 条" % n)


def scan_once(conn=None, *, max_runs: int = 3, stale_s: int | None = None,
              include_failed: bool = False) -> dict:
    """扫描一轮并触发自动重投。**返回本轮动作明细**（不抛异常，便于测试与监控）。

    :param max_runs: 单轮最多重投几个批次（防一次性把 LLM 额度打爆）。
                     **传 0 =纯预演**（走完全部判定但一个都不触发），这是
                     上线前确认"会投哪些批次"的安全口径 —— 不能用"传 1 再看返回"
                     代替，那会真的调 LLM。
    :param stale_s:  失活阈值（秒）。**必须大于单轮编排最长耗时**，否则会把正在跑
                     的批次误判为死掉 —— 这是本守护最危险的误判，默认取编排
                     stale 阈值的同一口径。
    :param include_failed: 是否连 `failed` 任务一并重跑（默认否：可能副作用已落一半）
    """
    res = {"scanned": 0, "resumed": [], "eligible": [], "skipped": [], "already_running": [],
           "maxed_out": [], "unrecoverable": []}
    if conn is not None:
        return _scan_conn(conn, res, max_runs=max_runs, stale_s=stale_s,
                          include_failed=include_failed)
    # db_conn 是 @contextmanager（提交/回滚/关闭三件事都由它兜住），
    # 所以统一走 `with` —— 不要手写 __enter__/__exit__，那会漏掉异常回滚。
    from database import db_conn
    with db_conn() as c:
        return _scan_conn(c, res, max_runs=max_runs, stale_s=stale_s,
                          include_failed=include_failed)


def _scan_conn(conn, res: dict, *, max_runs: int, stale_s: int | None,
               include_failed: bool) -> dict:
    """真正的扫描逻辑（连接由调用方持有 —— 便于测试注入内存库）。"""
    from agent.orch_checkpoint import prepare_resume, apply_resume, touch, resumable

    try:
        cands = resumable(conn, conversation_id=0, limit=50, stale_s=stale_s)
        res["scanned"] = len(cands)
        # 0 = 预演：走完**全部判定与分类**，但一个都不触发。
        # 为什么不早`continue`：预演的价值就在于看清"哪些能投、哪些不能、为什么"，
        # 早 continue 只会得到一个空列表，等于没信息。
        _budget = int(max_runs or 0)
        for it in cands:
            rid = int(it.get("run_id") or 0)
            if rid in (_state.get("inflight") or {}):
                res["already_running"].append(rid)
                continue
            dec = prepare_resume(conn, rid, include_failed=include_failed, stale_s=stale_s)
            reason = str(dec.get("reason") or "")
            if not dec.get("ok"):
                bucket = ("still_alive" if reason == "still_alive"
                          else "attempt_exhausted" if reason.startswith("attempt_exhausted")
                          else "unrecoverable")
                res[bucket].append({"run_id": rid, "why": reason})
                continue

            ck = dec.get("checkpoint") or {}
            conv_id = int(ck.get("conversation_id") or 0)
            if not conv_id:
                res["unrecoverable"].append({"run_id": rid, "why": "no_conversation"})
                continue

            mode = dec.get("mode") or "tasks"
            #分类已完成 —— 到这里才是"决定要不要动手"的岔路口
            res.setdefault("eligible", []).append(
                {"run_id": rid, "mode": mode, "conv_id": conv_id,
                 "phase": ck.get("phase") or "", "age_s": it.get("age_s"),
                 "pending": it.get("pending")})
            if len(res["resumed"]) >= _budget:
                res["skipped"].append({"run_id": rid,
                                       "why": ("preview_only" if _budget == 0
                                               else "max_runs_reached")})
                continue

            if mode == "tasks":
                applied = apply_resume(conn, dec)
                if not applied:
                    res["unrecoverable"].append({"run_id": rid, "why": "requeue_failed"})
                    continue
            # C-1 只重做汇总：一行任务都不动
            # ⚠️ touch 必须在触发**之前**：prepare_resume 的 is_alive 只看心跳，
            #    不 touch 的话下一个守护轮次会把同一批次再投一次（双跑）。
            touch(conn, rid, "executing")

            _state["inflight"][rid] = time.time()
            _state["resumed"] += 1
            res["resumed"].append(rid)
            _record("triggered", rid, "mode=%s conv=%s" % (mode, conv_id))

            def _target(rid=rid, conv_id=conv_id, ck=ck):
                try:
                    _drive(rid, conv_id, ck.get("branch") or "dev")
                except Exception as e:
                    _state["failed"] += 1
                    _record("failed", rid, str(e)[:160])
                    print("[resume-guard] 批次 %s 自动重投异常: %s" % (rid, str(e)[:160]), flush=True)
                finally:
                    _state["inflight"].pop(rid, None)
                    try:
                        _audit(rid, mode)
                    except Exception:
                        pass

            threading.Thread(target=_target, daemon=True,
                             name="orch-resume-%s" % rid).start()
    except Exception as e:
        _state["last_error"] = str(e)[:200]
        print("[resume-guard] 扫描异常（不中断守护）: %s" % str(e)[:160], flush=True)
    finally:
        _state["scans"] += 1
        _state["last_scan_at"] = time.time()
    return res


def _audit(run_id: int, mode: str) -> None:
    """写审计：为什么这个批次被自动重跑了。"""
    try:
        from database import db_conn, get_db
        from core.audit import audit
        with db_conn() as c:
            audit("system", "orch_auto_resume",
                  "自动重投编排批次 #%s（mode=%s）" % (run_id, mode), conn=c)
    except Exception:
        pass


def start_resume_loop(interval_sec: float = 300, *, max_runs: int = 3,
                      stale_s: int | None = None, include_failed: bool = False) -> bool:
    """启动自动重投守护（幂等：已启动则忽略）。线程 daemon=True，不阻塞退出。

    与 `start_alert_loop` 同款骨架 —— 复用本仓既有范本，而不是另立一套。
    """
    global _loop_thread
    if _loop_thread and _loop_thread.is_alive():
        return False
    _state["started_at"] = time.time()

    def _run():
        while not _loop_stop.wait(interval_sec):
            try:
                r = scan_once(max_runs=max_runs, stale_s=stale_s,
                              include_failed=include_failed)
                if r.get("resumed"):
                    print("[resume-guard] 自动重投批次 %s（剩余候选 %s 个）"
                          % (r["resumed"],
                             len(r.get("skipped") or []) + len(r.get("unrecoverable") or [])),
                          flush=True)
            except Exception as e:      # 双保险：scan_once 内部已兜，这里再兜一层
                _state["last_error"] = str(e)[:200]
                print("[resume-guard] 守护轮次异常（不中断）: %s" % str(e)[:160], flush=True)

    _loop_stop.clear()
    _loop_thread = threading.Thread(target=_run, daemon=True, name="orch-resume-guard")
    _loop_thread.start()
    print("[startup] 编排自动重投守护已启动（每 %ss，单轮最多 %s 批，失活阈值 %ss）"
          % (interval_sec, max_runs, stale_s if stale_s is not None else "(沿用检查点口径)"),
          flush=True)
    return True


def stop_resume_loop() -> None:
    _loop_stop.set()