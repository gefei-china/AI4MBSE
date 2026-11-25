# -*- coding: utf-8 -*-
"""verify_orch_interrupt —— 编排「可中断」策略（2026-11-10）。

## 为什么要有这个门禁
本仓此前**没有用户可用的中断**：
  · `TaskQueue.cancel_run` 能力早就写好（含 run 维度与协作式探针），但**全仓零调用点**；
  · 前端「停止」只 abort 浏览器这侧的 SSE ⇒ 服务端生成器被 GeneratorExit 掐断，
    子任务行留在 running/ready，只能等 30 分钟的孤儿回收 ⇒ 用户以为停了，后台还在烧钱；
  · 39 个真实编排 run 的中位耗时 648 s、87% 超 5 分钟 —— 这期间用户无法改向。

## 最要紧的一条判据：中断 ≠ 取消
两者都把任务置 `cancelled`，但**checkpoint.phase 不同**，而"能不能续跑"只认 phase：
  · `interrupted` ⇒ 这批是**暂停**，resume 时把 cancelled 任务重排队；
  · `cancelled`   ⇒ 这批**不要了**，绝不能因为用户点了续跑就原地复活。
若把判据写成"任务状态是 cancelled 就重排"，两条语义就合并了 —— 那是把销毁当暂停。

## 判据
### I 组 · 中断语义
  I1 `request_interrupt` 把未终态任务置 cancelled，且**不动已完成任务**
  I2 checkpoint 变 `interrupted`（不是 cancelled），并在 `orch_interrupts` 留一行
  I3 幂等：重复中断不报错、第二次 cancelled 数 = 0，但**仍留台账**（"我又催了一次"也是信息）
  I4 终态批次（done/failed）拒绝中断（不报错成 200 假成功）
  I5 `kind='cancel'` 走硬取消语义（phase='cancelled'）
  I6 补充指令落库且可读出（`latest_instruction`）

### S 组 · 探针
  S1 `stop_requested` 对 interrupted / cancelled 为 True，对 running/planned 为 False
  S2 空 run_id / 不存在的批次 / 表缺列 ⇒ False 且**不抛**（它跑在热路径上）

### R 组 · 续跑（本门禁的核心）
  R1 interrupted 批次的 cancelled 任务 ⇒ `prepare_resume` 判为 requeue
  R2 ★ cancelled 批次（硬取消）的同名任务 ⇒ 仍是 skip（不得复活）
  R3 `apply_resume(instruction=...)` 把补充指令写进**被重排任务**的上下文
  R4 `consume_instruction` 只给一次（第二次返回空 ⇒ 不会重复注入）
  R5 `resumable()` 把 interrupted 批次的 cancelled 任务计入 pending（否则永远不出现）

### W 组 · 接线
  W1 主循环在**派发边界**检查停止（扫源码：`_int_stop` 出现在主循环体内）
  W2 中断后聚合状态降为 partial/failed（不得报"完整"）
  W3 端点齐备：run 级 interrupt + 会话级 interrupt + resume 接受 instruction
  W4 前端「停止」先调中断接口再 abort（顺序不能反）

### M 组 · 变异自证
  M1 把 `phase == interrupted` 的放行条件去掉 ⇒ R1 必须翻红
  M2 把放行条件改成"无条件放行 cancelled" ⇒ R2 必须翻红
  M3 把主循环里的停止检查去掉（源码手术）⇒ W1 必须翻红

用法（必须用项目 venv）
    ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_orch_interrupt.py
"""
from __future__ import annotations
import os as _svos
import sys as _svsys
_svroot = _svos.path.dirname(_svos.path.abspath(__file__))
while _svroot and not _svos.path.isdir(_svos.path.join(_svroot, 'tools')):
    _svroot = _svos.path.dirname(_svroot)
if _svroot and _svroot not in _svsys.path:
    _svsys.path.insert(0, _svroot)
from tools.stream_source import read_stream_source  # noqa: E402
from tools.module_source import read_module_source  # noqa: E402
from _tmpdir import mkdtemp

import inspect
import os
import sqlite3
import sys
import textwrap
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))


def _find_code_pos(src, needle):
    """返回 needle 在「代码行」中的下标（行首只有缩进），找不到返回 -1。

    ⚠️ 2026-11-25 实测：`src.find("while TaskQueue.pending_count(...)")` 命中的是
    stream.py:210 **文档字符串里引用的同一句话**（`"`while TaskQueue..."`），
    于是 seg 落在注释区、W1 判成"主循环没有停止检查" ⇒ 恒红（假红）。
    文本级判据必须先排除注释/字符串引用：真代码行的行首只能是缩进。
    """
    start = 0
    while True:
        i = src.find(needle, start)
        if i < 0:
            return -1
        ls = src.rfind("\n", 0, i) + 1
        if src[ls:i].strip() == "":
            return i
        start = i + 1
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

PASS, FAIL = "PASS", "FAIL"
_results, _MUT = [], []
_IN = [False]


def check(name, ok, detail=""):
    row = (PASS if ok else FAIL, name, "" if ok else str(detail)[:260])
    (_MUT if _IN[0] else _results).append(row)
    print("[%s] %s%s" % ("OK  " if ok else "FAIL", name,
                         ("　— " + str(detail)) if detail and not ok else ""))
    return bool(ok)


def _db(tag="i"):
    """最小夹具：orch_checkpoints / agent_tasks / orch_interrupts。"""
    d = mkdtemp(prefix="orchi_%s_" % tag)
    p = os.path.join(d, "t.db")
    c = sqlite3.connect(p)
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE orch_checkpoints (
        run_id INTEGER PRIMARY KEY, conversation_id INTEGER DEFAULT 0,
        phase TEXT DEFAULT 'planned', user_input TEXT DEFAULT '', intent TEXT DEFAULT '',
        branch TEXT DEFAULT 'dev', provider_id INTEGER DEFAULT 0,
        skill_name TEXT DEFAULT '', team TEXT DEFAULT '', plan_json TEXT DEFAULT '[]',
        params_json TEXT DEFAULT '{}', attempt_count INTEGER DEFAULT 0,
        summary_written INTEGER DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
    c.execute("""CREATE TABLE agent_tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER DEFAULT 0,
        task_key TEXT DEFAULT '', title TEXT DEFAULT '', status TEXT DEFAULT 'planned',
        deps TEXT DEFAULT '[]', context TEXT DEFAULT '', expected_output TEXT DEFAULT '',
        agent_id TEXT DEFAULT '', task_type TEXT DEFAULT 'agent', config TEXT DEFAULT '{}',
        seq INTEGER DEFAULT 1, error TEXT DEFAULT '', error_class TEXT DEFAULT '',
        result TEXT DEFAULT '', summary_json TEXT DEFAULT '', latency_ms INTEGER DEFAULT 0,
        retry_count INTEGER DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
    c.execute("""CREATE TABLE orch_interrupts (
        id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL,
        conversation_id INTEGER DEFAULT 0, kind TEXT DEFAULT 'interrupt',
        reason TEXT DEFAULT '', instruction TEXT DEFAULT '', requested_by TEXT DEFAULT '',
        cancelled_tasks INTEGER DEFAULT 0, consumed_at TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
    c.commit()
    return c, p, d


def _mk(c, run_id, phase="executing", statuses=("planned", "ready", "running", "done"), conv=1):
    c.execute("INSERT INTO orch_checkpoints (run_id, conversation_id, phase, plan_json) "
              "VALUES (?,?,?,'[]')", (run_id, conv, phase))
    ids = []
    for i, st in enumerate(statuses):
        ids.append(int(c.execute(
            "INSERT INTO agent_tasks (run_id, task_key, title, status, context) "
            "VALUES (?,?,?,?,'')", (run_id, "t%d" % (i + 1), "任务%d" % (i + 1), st)).lastrowid))
    c.commit()
    return ids


def i_group():
    print("\n=== I 组 · 中断语义 ===")
    from agent import orch_interrupt as oi

    c, p, d = _db("i")
    ids = _mk(c, 81001, statuses=("planned", "ready", "running", "done"))
    res = oi.request_interrupt(c, 81001, instruction="不要动前端，只补后端校验",
                               reason="方向错了", requested_by="tester")
    sts = [r[0] for r in c.execute(
        "SELECT status FROM agent_tasks WHERE run_id=81001 ORDER BY id")]
    check("I1 未终态任务全部 cancelled，已完成任务不受影响",
          sts == ["cancelled", "cancelled", "cancelled", "done"] and res.get("cancelled_tasks") == 3,
          {"sts": sts, "res": res})

    ph = c.execute("SELECT phase FROM orch_checkpoints WHERE run_id=81001").fetchone()[0]
    n_led = c.execute("SELECT COUNT(*) FROM orch_interrupts WHERE run_id=81001").fetchone()[0]
    check("I2 checkpoint=interrupted（不是 cancelled）+ 台账留痕",
          ph == "interrupted" and n_led == 1 and res.get("ok"), {"phase": ph, "ledger": n_led})

    res2 = oi.request_interrupt(c, 81001, reason="再催一次")
    n_led2 = c.execute("SELECT COUNT(*) FROM orch_interrupts WHERE run_id=81001").fetchone()[0]
    check("I3 幂等：重复中断 cancelled=0，但仍留一行台账（催过几次是信息）",
          res2.get("ok") and res2.get("cancelled_tasks") == 0 and n_led2 == 2,
          {"res": res2, "ledger": n_led2})

    _mk(c, 81002, phase="done", statuses=("done",))
    res3 = oi.request_interrupt(c, 81002, reason="x")
    check("I4 终态批次拒绝中断（不返回 ok=True 的假成功）",
          res3.get("ok") is False and "已结束" in (res3.get("error") or ""), res3)

    _mk(c, 81003, statuses=("running",))
    res4 = oi.request_interrupt(c, 81003, kind="cancel", reason="不要了")
    ph4 = c.execute("SELECT phase FROM orch_checkpoints WHERE run_id=81003").fetchone()[0]
    check("I5 kind=cancel 走硬取消（phase=cancelled，不留续跑资格）",
          ph4 == "cancelled" and res4.get("ok"), {"phase": ph4})

    check("I6 补充指令可读出（latest_instruction）",
          oi.latest_instruction(c, 81001) == "不要动前端，只补后端校验",
          oi.latest_instruction(c, 81001))
    c.close()


def s_group():
    print("\n=== S 组 · 停止探针 ===")
    from agent import orch_interrupt as oi
    c, p, d = _db("s")
    _mk(c, 82001, phase="running", statuses=("running",))
    _mk(c, 82002, phase="interrupted", statuses=("cancelled",))
    _mk(c, 82003, phase="cancelled", statuses=("cancelled",))
    check("S1 只有 interrupted / cancelled 判定为「要停」",
          oi.stop_requested(c, 82001) is False
          and oi.stop_requested(c, 82002) is True
          and oi.stop_requested(c, 82003) is True,
          {k: oi.stop_requested(c, k) for k in (82001, 82002, 82003)})
    # ★ 真机冒烟踩出来：`active_run` 首版把 interrupted 也算"在跑"
    #   ⇒ 对一批早已暂停的会话再点"停止"会回 ok:true（把无操作报成有操作）。
    check("S1b active_run 只认真的在跑（interrupted/cancelled 不算）",
          oi.active_run(c, 82001) == {} and bool(oi.active_run(c, 82002)) is False
          and bool(oi.active_run(c, 82003)) is False,
          {"running": oi.active_run(c, 82001), "interrupted": oi.active_run(c, 82002)})
    # ★ 浏览器实测后的修正：探针若走编排**主连接**，会与主连接持有的未提交写事务纠缠
    #   （本轮实测 3 个并发 run 时报 `database is locked`，服务无响应）。
    #   判据：主循环调用点必须传 None（= 让探针自己开独立短连接）。
    _srcp = read_stream_source().replace("\r\n", "\n")
    check("S2b 中断探针走独立连接（主循环调用点传 None，不用主连接）",
          "_int_stop(None, run_id)" in _srcp and "_int_stop(conn, run_id)" not in _srcp,
          "探针仍在用编排主连接")
    check("S2 空 id / 不存在批次 ⇒ False 且不抛（热路径安全）",
          oi.stop_requested(c, 0) is False and oi.stop_requested(c, 999999) is False)
    c.close()
    # 表缺失（老库）也要 False 而不是异常
    c2, p2, d2 = _db("s2")
    c2.execute("DROP TABLE orch_checkpoints")
    c2.commit()
    check("S2b 表缺失（老库）⇒ False 且不抛", oi.stop_requested(c2, 1) is False)
    c2.close()


def r_group():
    print("\n=== R 组 · 续跑（核心：暂停 ≠ 销毁）===")
    from agent import orch_interrupt as oi
    from agent.orch_checkpoint import prepare_resume, apply_resume, resumable

    c, p, d = _db("r")
    # 中断型：全部 cancelled（含 running 被协作式停下）
    _mk(c, 83001, statuses=("cancelled", "cancelled", "done"))
    oi.request_interrupt(c, 83001, instruction="补充：只改 BDD，不要动 IBD")
    dec = prepare_resume(c, 83001, stale_s=0)
    check("R1 interrupted 批次的 cancelled 任务被判为 requeue",
          dec.get("ok") and sorted(dec.get("requeue_ids") or []) == [1, 2],
          {"ok": dec.get("ok"), "rq": dec.get("requeue_ids"), "why": dec.get("reason")})

    # 硬取消型：同形状，但 phase=cancelled
    _mk(c, 83002, statuses=("cancelled", "cancelled", "done"))
    oi.request_interrupt(c, 83002, kind="cancel", reason="不要了")
    dec2 = prepare_resume(c, 83002, stale_s=0)
    check("R2 ★硬取消批次的 cancelled 任务仍是 skip（不得原地复活）",
          not dec2.get("ok") and not (dec2.get("requeue_ids") or []),
          {"ok": dec2.get("ok"), "rq": dec2.get("requeue_ids"),
           "why": dec2.get("reason"), "skip": dec2.get("skip")})

    _ins = oi.consume_instruction(c, 83001)
    n = apply_resume(c, dec, instruction=_ins)
    ctx = c.execute("SELECT context FROM agent_tasks WHERE run_id=83001 AND task_key='t1'"
                    ).fetchone()[0]
    check("R3 补充指令写进被重排任务的 context",
          n == 2 and "补充：只改 BDD" in (ctx or "") and "用户中断时补充的指令" in (ctx or ""),
          {"n": n, "ctx": (ctx or "")[:120]})

    check("R4 consume_instruction 只给一次（不会重复注入）",
          oi.consume_instruction(c, 83001) == "", oi.consume_instruction(c, 83001))

    # resumable()：interrupted 批次必须出现在可恢复清单里
    c.execute("UPDATE orch_checkpoints SET updated_at=datetime('now','-1 hour') WHERE run_id=83001")
    c.commit()
    lst = resumable(c, stale_s=1, requeue_classes=())
    check("R5 resumable() 把 interrupted 批次的 cancelled 计入 pending",
          any(int(x["run_id"]) == 83001 and int(x.get("pending") or 0) > 0 for x in lst),
          lst)
    # ★ 实测（浏览器）发现：interrupted 批次曾被"心跳还新 ⇒ 可能还在跑"的 age 过滤挡掉
    #   ⇒ 中断后 30 分钟内既看不到「继续编排」入口、续跑也会被 still_alive 拒。
    #   判据：用**很大的 stale_s**（模拟"刚中断"，心跳很新）时，interrupted 仍必须出现。
    c.execute("UPDATE orch_checkpoints SET updated_at=datetime('now') WHERE run_id=83001")
    c.commit()
    lst2 = resumable(c, stale_s=1800, requeue_classes=())
    check("R6 interrupted 批次**不受心跳阈值约束**（刚中断就该能续跑）",
          any(int(x["run_id"]) == 83001 for x in lst2),
          {"stale_s=1800 的清单": lst2})
    # ★★ 本轮实测事故（P0）：放宽 R6 后，**自动重投守护**复用了同一个 resumable()
    #   ⇒ 用户点停止 3 分钟后系统自己把批次重投跑起来（run 990223）。
    #   两个消费者需求相反：人工要看到、守护绝不能碰 ⇒ 必须有显式分叉。
    lst3 = resumable(c, stale_s=1800, requeue_classes=(), include_interrupted=False)
    check("R7 ★自动重投守护路径必须排除 interrupted（用户停止 ≠ 可自愈故障）",
          not any(int(x["run_id"]) == 83001 for x in lst3),
          {"include_interrupted=False 仍返回": [x.get("run_id") for x in lst3]})
    # ⚠️ P1 归位后该路径是 5 行垫片 ⇒ 直接读源码会把 R7b 判成恒红（实测）。
    #    read_module_source() 穿透垫片，读真实实现（services/orch_supervisor.py）。
    _sup = read_module_source(os.path.join(_ROOT, "core", "orch_supervisor.py"))
    check("R7b 守护调用点真的传了 include_interrupted=False（不是只加了参数）",
          "include_interrupted=False" in _sup, "守护仍会重投中断批次")
    c.close()


def w_group():
    print("\n=== W 组 · 接线 ===")
    # 走统一源码视图（stream.py 拆分契约：门禁不直读单文件路径）
    src = read_stream_source()
    i_loop = _find_code_pos(src, "while TaskQueue.pending_count(conn, run_id) > 0")
    seg = src[i_loop:i_loop + 2600] if i_loop > 0 else ""
    check("W1 主循环在派发边界检查停止（_int_stop 出现在主循环体内）",
          i_loop > 0 and "_int_stop(None, run_id)" in seg, seg[:160])
    check("W2 中断后聚合状态降为 partial/failed（不报「完整」）",
          "_interrupt_hit" in src and '_agg_status = "partial" if _agg_status == "full"' in src,
          "缺少中断降级")
    check("W2b 中断会写入一次性 SSE 事件（前端要能看见）",
          '"type": "interrupt"' in src and '"resume_hint"' in src)
    # ★ 真机踩出来的坑（2026-11-10 实跑）：中断后收尾仍把 checkpoint 写成 failed
    #   ⇒ `prepare_resume` 以 phase_terminal 拒绝 ⇒ "可续跑"是假的。
    #   判据必须落在"终态选择"上，而不是"有没有发 interrupt 事件"。
    check("W2c 中断批次的终态是 interrupted（不是 done/failed，否则续跑被 phase_terminal 拒）",
          '_final_phase = "interrupted"' in src,
          "收尾段缺少中断终态选择")

    # ★ 变量只在某条分支赋值 = 本仓反复崩过一整轮的地雷（`_gate_gaps` 前科）。
    #   收尾段无条件读 `_interrupt_hit`，而"计划为空⇒单 Agent 直行"分支不进主循环 ⇒
    #   必须在**函数顶层**初始化。判据用 AST（不是"文本里有这行"——
    #   缩进在 else 块里的赋值同样能匹配文本）。
    import ast as _ast
    _t = _ast.parse(src)
    _fn = [n for n in _ast.walk(_t) if isinstance(n, _ast.FunctionDef)
           and n.name == "_stream_orchestrated_flow"]
    _top = []
    if _fn:
        _top = [n.lineno for n in _fn[0].body
                if isinstance(n, _ast.Assign)
                and any(getattr(x, "id", "") == "_interrupt_hit" for x in n.targets)]
    check("W5 `_interrupt_hit` 在函数顶层初始化（降级分支也读它）",
          bool(_top), "只在 else 块赋值 ⇒ 计划为空时 NameError")

    # ★ 中断时 worker 会带着"前段内容"正常返回（tk_ok=True）。若照旧 `complete`，
    #   DB 变 done ⇒ resume 判 reuse ⇒ **半截内容被当成完整交付物**（用户永远拿不到补齐版）。
    check("W6 中断期间不把半截产出记成 done（任务行保持 cancelled，resume 才能重排）",
          "_tk_cancelled" in src and "if not _tk_cancelled:" in src,
          "缺少中断期 complete 守卫")

    ro = open(os.path.join(_ROOT, "routers", "orchestration.py"), encoding="utf-8").read()
    check("W3 端点齐备：run 级 + 会话级中断 + resume 收 instruction",
          "/runs/{run_id}/interrupt" in ro and "/conversations/{conv_id}/interrupt" in ro
          and "body: dict = None" in ro and "instruction=_ins" in ro,
          "端点或参数缺失")

    js = open(os.path.join(_ROOT, "static", "js", "mods", "12-chatsend.js"),
              encoding="utf-8").read().replace("\r\n", "\n")
    i_fetch = js.find("/interrupt`")
    i_abort = js.find("_streamAbort.abort()", i_fetch - 2000 if i_fetch > 0 else 0)
    check("W4 前端「停止」先调中断接口再 abort（顺序不能反）",
          0 < i_fetch < i_abort, {"fetch": i_fetch, "abort": i_abort})


def m_group():
    print("\n=== M 组 · 变异自证 ===")
    from agent import orch_checkpoint as oc

    _IN[0] = True
    src = textwrap.dedent(inspect.getsource(oc.prepare_resume))

    # 夹具：interrupted 批次（全 cancelled）
    c, p, d = _db("m")
    _mk(c, 84001, statuses=("cancelled", "cancelled"))
    c.execute("UPDATE orch_checkpoints SET phase='interrupted' WHERE run_id=84001")
    c.commit()

    # M1：去掉 interrupted 放行 ⇒ R1 必须翻红
    mut1 = src.replace(
        '_extra = INTERRUPTED_REQUEUE_STATUSES if phase == PHASE_INTERRUPTED else ()',
        '_extra = ()', 1)
    ns = dict(vars(oc))
    exec(compile(mut1, "<m1>", "exec"), ns)
    d1 = ns["prepare_resume"](c, 84001, stale_s=0)
    check("M1 去掉「phase==interrupted 才放行」⇒ 中断批次无法续跑（R1 翻红）",
          not (d1.get("requeue_ids") or []), d1.get("reason"))

    # M2：无条件放行 cancelled ⇒ 硬取消也会复活（R2 翻红）
    mut2 = src.replace(
        '_extra = INTERRUPTED_REQUEUE_STATUSES if phase == PHASE_INTERRUPTED else ()',
        '_extra = INTERRUPTED_REQUEUE_STATUSES', 1)
    c.execute("UPDATE orch_checkpoints SET phase='cancelled' WHERE run_id=84001")
    c.commit()
    ns2 = dict(vars(oc))
    exec(compile(mut2, "<m2>", "exec"), ns2)
    d2 = ns2["prepare_resume"](c, 84001, stale_s=0)
    check("M2 无条件放行 cancelled ⇒ 硬取消批次被复活（R2 翻红）",
          bool(d2.get("requeue_ids")), d2)

    # M3：把主循环的停止检查删掉 ⇒ W1 判据必须翻红
    srcs = read_stream_source()
    i = _find_code_pos(srcs, "while TaskQueue.pending_count(conn, run_id) > 0")
    seg = srcs[i:i + 2600]
    # ⚠️ 变异锚点必须跟实现同步：探针调用已改成 `_int_stop(None, run_id)`
    #   （独立连接纪律）。锚点写成旧串会让变异**根本没生效** ⇒ 断言空转通过。
    seg_mut = seg.replace("_int_stop(None, run_id)", "_int_stop_disabled(None, run_id)")
    check("M3 删掉主循环停止检查 ⇒ W1 判据翻红",
          "_int_stop(None, run_id)" in seg and "_int_stop(None, run_id)" not in seg_mut,
          "变异锚点与实现不一致（变异未生效 ⇒ 空转断言）")
    _IN[0] = False
    c.close()


def main():
    t0 = time.time()
    print("verify_orch_interrupt —— 编排可中断（暂停 ≠ 销毁）")
    i_group()
    s_group()
    r_group()
    w_group()
    m_group()
    n_pass = sum(1 for r in _results if r[0] == PASS)
    n_mut = sum(1 for r in _MUT if r[0] == PASS)
    n_fail = sum(1 for r in _results + _MUT if r[0] == FAIL)
    print("\n结果：%d PASS / %d FAIL　（其中变异自证 %d 条）　%.1fs"
          % (n_pass, n_fail, n_mut, time.time() - t0))
    for st, name, det in _results + _MUT:
        if st == FAIL:
            print("  ✗ %s　%s" % (name, det))
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
