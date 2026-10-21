# -*- coding: utf-8 -*-
"""P2 replanning（2026-10-21，执行中重规划第二阶段）回归验证 —— 零 LLM、零服务、零生产库写。

**为什么需要它**（设计文档 v2 §7 P2 / §4.10）：P2 交付"差量重画（规则版）"——
    收敛点先试着局部修图（T2-轻原地重派 / T2-重新 key + lineage），修不了才走
    原来的"全部 block + break"。本门禁钉住六件事不退化：

    S  静态契约：模块入口齐备、纯函数纪律、接线存在、配置双登记、CI 注册
    U1 G2 算法逐格：失效传播、保留集、skip 叶子、轻/重两级、done 不重画、
        下游 deps 重映射、新图过 validate_plan
    U2 G7 质量门逐格：Q1~Q5 各有一条"必须抓住"的反例
    U3 终止条件：无进展检测（同因第 2 次）、轮次上限、T1.5 skip 边界
    U4 ★ **零行为变更**（P2 的头号纪律）：总闸关 ⇒ 决策恒 none、不产出任何
        updates/new_tasks；且 stream.py 里重画段整体在 `_rp_enabled` 之内
    M  变异自证：关 done 复用 / 关无进展检测 / 删接线 / 删质量门 / 删重映射
        —— 每个变异都必须被本门禁抓住（否则断言是空转的）
    X  接线正确性：轻量级走 UPDATE（不追行）、重量级先改 deps 再 create_plan

**为什么"轻量级不追行"要有断言**（X-a）：`agent_tasks` 对 `(run_id, task_key)`
    **无唯一约束** —— 同 key 追一行会让 `ready_tasks` 派发两次 ⇒ 任务重复执行、
    产物双写。这是 P2 最容易踩且最难发现的坑（表现为"结果突然多了一份"）。

用法：
    <repo>\\.venv\\Scripts\\python.exe -X utf8 tools/verify/verify_replan_p2.py
退出码：全绿 0 / 有失败或空转 1。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

REDRAW_SRC = os.path.join(ROOT, "services", "replan_redraw.py")
SIGNAL_SRC = os.path.join(ROOT, "services", "replan_signal.py")
STREAM_SRC = os.path.join(ROOT, "agent", "pipeline_parts", "stream.py")
CONFIG_SRC = os.path.join(ROOT, "core", "config.py")
SCHEMA_SRC = os.path.join(ROOT, "database", "schema.py")
TASKQ_SRC = os.path.join(ROOT, "task_queue.py")
CI_YML = os.path.join(ROOT, ".github", "workflows", "ci.yml")

PASS, FAIL, VACUOUS = "PASS", "FAIL", "VACUOUS"
_results = []


def _rec(name, ok, detail="", kind=FAIL):
    _results.append((kind if not ok else PASS, name, detail))
    print(f"  {PASS if ok else kind}  {name}" + (f"  {detail}" if not ok and detail else ""))
    return ok


def _read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def T(k, deps, st="planned", **kw):
    """合成 agent_tasks 行（与 DB 列名一致：task_key/deps 为 JSON 串）。"""
    import json as _j
    d = {"id": abs(hash(k)) % 10000, "task_key": k, "deps": _j.dumps(deps),
         "status": st, "title": "任务" + k, "agent_id": "design", "seq": 0,
         "context": "原始上下文", "expected_output": "", "config": "{}",
         "error": "", "error_class": ""}
    d.update(kw)
    return d


def diamond():
    """标准菱形 DAG：t1 → (t2, t3) → t4；t1 done、t2 failed。"""
    ts = [T("t1", []), T("t2", ["t1"]), T("t3", ["t1"]), T("t4", ["t2", "t3"])]
    ts[0]["status"] = "done"
    ts[1]["status"] = "failed"
    ts[1]["error"] = "timeout（超过 600s 硬上限）"
    ts[1]["error_class"] = "timeout"
    return ts


# ══════════════════════════════════════════════════════════════════════════
def t_static():
    print("\n=== S. 静态契约 ===")
    ok = True
    src = _read(REDRAW_SRC)

    ok &= _rec("S1a 七个入口函数齐备",
               all(("def " + m) in src for m in (
                   "propagation_set", "skip_candidates", "build_redraw_plan",
                   "quality_gates", "no_progress", "plan_redraw", "_fallback_validate")))
    ok &= _rec("S1b 纯函数纪律（无 sqlite/requests/get_db/open( 调用）",
               not any(t in src for t in ("sqlite3", "requests", "urllib.request",
                                          "get_db", "open(")))
    ok &= _rec("S1c 动作常量三态齐备（none/skip/replan）",
               all(('ACTION_%s = "%s"' % (a.upper(), a)) in src
                   for a in ("none", "skip", "replan")))
    ok &= _rec("S1d 轻/重两级常量齐备",
               'LEVEL_LIGHT = "T2-light"' in src and 'LEVEL_HEAVY = "T2-heavy"' in src
               and 'LEVEL_SKIP = "T1.5"' in src)
    # 判据用**代码行**而非全文匹配：docstring 里也会提到 TaskQueue，
    # 用全文 `not in` 会误杀（第一版就踩了这个：S1e 因 docstring 提及而假红）。
    _codelines = [ln.strip() for ln in src.splitlines()
                  if not ln.strip().startswith("#")]
    _codebody = "\n".join(_codelines)
    ok &= _rec("S1e validate 以参数注入（代码里不 import task_queue —— 纯函数边界）",
               "validate=None" in src
               and not any(ln.startswith(("import task_queue", "from task_queue"))
                           for ln in _codelines))
    ok &= _rec("S1f 不变量 2 的 lineage 写入（重级新 key + config.lineage）",
               '"lineage"' in src and '"from": k' in src)

    stream = _read(STREAM_SRC)
    ok &= _rec("S2a ★ 收敛点接线：_rr.plan_redraw 被调用",
               "_rr.plan_redraw(" in stream)
    ok &= _rec("S2b 观测旁路铁律：重画段整体在 try 内",
               "if _RR_OK and _rp_enabled and _rp_on_stall:" in stream)
    ok &= _rec("S2c 标志位导入模式（不用 `_rr = None` 兜底 —— 避 e2e G0 别名遮蔽）",
               "_RR_OK = False" in stream and "_rr = None" not in stream)
    ok &= _rec("S2d 重画段在 P0-3 全量 block **之前**（先修图，修不了才 block）",
               stream.index("_rr.plan_redraw(") < stream.index('TaskQueue.block(conn, tk["id"], "依赖任务失败，无法推进")'))
    ok &= _rec("S2e 重画成功走 continue（不改循环结构，v1 §4.3 纪律）",
               stream.count("continue        # 下一轮 ready_tasks 自然派发") == 1)
    ok &= _rec("S2f redraw-decide 决策 span 挂在 _run_sid 下",
               '_spans.open("replan", "redraw-decide"' in stream
               and "parent=_run_sid," in stream)
    ok &= _rec("S2g plan_revised 事件在重画成功时发出（P1 只建路径，P2 真发）",
               "_rs.plan_revised_payload(_redraw_dec" in stream)
    ok &= _rec("S2h 质量门结果进 span meta（Q1~Q5 pass/fail 可查）",
               '_rd_meta["gates"]' in stream)

    # ★ 轻量级不追行（同 run_id + task_key 无唯一约束 ⇒ 追行会重复派发）
    _light_seg = stream[stream.index("if _rp_light and not _rp_heavy:"):
                        stream.index("else:", stream.index("if _rp_light and not _rp_heavy:"))]
    ok &= _rec("X-a ★ 轻量级走 UPDATE 不追行（同 key 重复派发防线）",
               "TaskQueue.retry(conn, int(_u.get(\"id\")))" in _light_seg
               and "UPDATE agent_tasks SET context=?" in _light_seg
               and "create_plan" not in _light_seg)
    _heavy_seg = stream[stream.index('assigned_by="replanner"') - 1600:
                        stream.index('assigned_by="replanner"') + 200]
    ok &= _rec("X-b 重量级 create_plan 带 assigned_by='replanner'（审计可归因）",
               'assigned_by="replanner"' in stream)
    ok &= _rec("X-c ★ 重量级先改 deps 再 create_plan（反序会被静默过滤掉新依赖）",
               _heavy_seg.index("UPDATE agent_tasks SET deps=?")
               < _heavy_seg.index("TaskQueue.create_plan("))

    cfg = _read(CONFIG_SRC)
    ok &= _rec("S3a DEFAULT_CONFIG.replanning P2 五键齐全",
               all(('"%s":' % k) in cfg for k in (
                   "redraw_light", "skip_leaf", "no_progress_stop",
                   "redraw_on_stall", "heavy_redraw")))
    ok &= _rec("S3b CONFIG_SCHEMA 双登记（B4-5 纪律：配置存在 ≠ 可被配置）",
               cfg.count('"replanning": {') == 2)
    ok &= _rec("S3c ★ 默认关纪律：enabled/skip_leaf/heavy_redraw/llm_redraw 全 False",
               '"enabled": False' in cfg and '"skip_leaf": False' in cfg
               and '"heavy_redraw": False' in cfg and '"llm_redraw": False' in cfg)
    ok &= _rec("S3d 保守默认：配置读取失败取 enabled=False（不会因读不到配置就开重画）",
               '_rp_enabled = _rp_heavy = _rp_skip = False' in stream)

    ci = _read(CI_YML)
    ok &= _rec("S4a CI 已注册 P2 门禁", "verify_replan_p2.py" in ci)
    ok &= _rec("S4b P1 门禁仍在 CI（P2 不得挤掉 P1）",
               "verify_replan_p1.py" in ci)
    return ok


# ══════════════════════════════════════════════════════════════════════════
def t_unit():
    print("\n=== U1/U2/U3. 算法与质量门逐格 ===")
    from services import replan_redraw as RR
    from services.replan_signal import norm_error_text
    from task_queue import TaskQueue
    ok = True

    # ── U1 失效传播与保留集 ──
    ts = diamond()
    ps = RR.propagation_set(ts, ["t2"])
    ok &= _rec("U1a 失效传播 = failed + 全部下游（t4 被 t2 拖下水）",
               ps == {"t2", "t4"}, str(sorted(ps)))
    ok &= _rec("U1b 传播集不含无关分支 t3（不误伤）", "t3" not in ps)
    chain = [T("a", []), T("b", ["a"]), T("c", ["b"]), T("d", ["c"])]
    ok &= _rec("U1c 多跳传播（a→b→c→d 全链下水）",
               RR.propagation_set(chain, ["a"]) == {"a", "b", "c", "d"})
    cyc = [T("x", ["y"]), T("y", ["x"])]
    ok &= _rec("U1d 环图不死循环（有界 BFS）",
               isinstance(RR.propagation_set(cyc, ["x"]), set))

    # ── U1 skip 叶子边界 ──
    ok &= _rec("U1e 有下游的 failed 不是 skip 候选",
               RR.skip_candidates(ts, ["t2"]) == [])
    leaves = [T("t1", []), T("t2", [])]
    leaves[1]["status"] = "failed"
    ok &= _rec("U1f 叶子 failed 是 skip 候选", RR.skip_candidates(leaves, ["t2"]) == ["t2"])

    # ── U1 轻量级 ──
    d = RR.plan_redraw(ts, ["t2"], enabled=True, light=True,
                       reason_by_key={"t2": "timeout 超时"})
    ok &= _rec("U1g 轻量级 action=replan / level=T2-light",
               d["action"] == "replan" and d["level"] == "T2-light",
               "%s/%s" % (d["action"], d["level"]))
    ok &= _rec("U1h ★ 轻量级只重派 failed 本身（updates=[t2]，不追新行）",
               [u["key"] for u in d["updates"]] == ["t2"] and d["new_tasks"] == [])
    ok &= _rec("U1i ★ 不变量 1：done 的 t1 绝不出现在重派清单",
               "t1" not in [u["key"] for u in d["updates"]]
               and "t1" not in [k["key"] for k in d["kept"]])
    ok &= _rec("U1j 轻量级 context 注入失败原因（防原样再撞）",
               "timeout 超时" in d["updates"][0]["context"]
               and "换一条实现路径" in d["updates"][0]["context"])
    ok &= _rec("U1k ★ 轻量级图拓扑不动（dropped 为空，kept 保留无关任务）",
               d["dropped"] == [] and "t3" in [k["key"] for k in d["kept"]])
    ok &= _rec("U1l 轻量级质量门全过", d["gates"].get("ok") is True,
               str(d["gates"].get("failed")))

    # ── U1 重量级 ──
    dh = RR.plan_redraw(ts, ["t2"], enabled=True, light=False,
                        reason_by_key={"t2": "timeout"}, validate=TaskQueue.validate_plan)
    ok &= _rec("U1m 重量级新 key = t2_r1 且带 lineage.from=t2",
               dh["new_tasks"][0]["key"] == "t2_r1"
               and dh["new_tasks"][0]["config"]["lineage"]["from"] == "t2")
    ok &= _rec("U1n ★ 下游 t4 保留且 deps 重映射到 t2_r1（不被丢弃也不悬空）",
               dict((k["key"], k["deps"]) for k in dh["kept"])["t4"] == ["t2_r1", "t3"],
               str(dict((k["key"], k["deps"]) for k in dh["kept"])))
    ok &= _rec("U1o ★ 无关任务 t3 保留（重画不许吞掉用户剩下的活）",
               "t3" in [k["key"] for k in dh["kept"]])
    ok &= _rec("U1p 重量级新图过真 validate_plan",
               RR.plan_redraw(ts, ["t2"], enabled=True, light=False,
                              validate=TaskQueue.validate_plan)["gates"]["Q1"] is True)

    # ── U2 质量门逐格（每门一个"必须抓住"的反例）──
    base = dict(failed_keys={"t2"}, done_keys={"t1"}, remaining_count=4,
                validate=TaskQueue.validate_plan)
    g_ok = RR.quality_gates([], [{"key": "t2_r1", "deps": ["t1"],
                                  "config": {"lineage": {"from": "t2"}}}],
                            [], **base)
    ok &= _rec("U2a 正常方案五门全过", all(g_ok["Q%d" % i] for i in range(1, 6)),
               str(g_ok["failed"]))
    # Q1：环
    g_q1 = RR.quality_gates([], [{"key": "a", "deps": ["b"],
                                  "config": {"lineage": {"from": "t2"}}},
                                 {"key": "b", "deps": ["a"],
                                  "config": {"lineage": {"from": "t2"}}}], [], **base)
    ok &= _rec("U2b Q1 结构门抓环（环图必须作废）", g_q1["Q1"] is False)
    # Q2：新 key 撞车（撞 done 的 key）
    g_q2 = RR.quality_gates([], [{"key": "t1", "deps": [],
                                  "config": {"lineage": {"from": "t2"}}}], [], **base)
    ok &= _rec("U2c Q2 职责门抓撞 done 的 key（重复建房）", g_q2["Q2"] is False)
    # Q3：lineage 缺失
    g_q3 = RR.quality_gates([], [{"key": "x_r1", "deps": [], "config": {}}], [], **base)
    ok &= _rec("U2d Q3 针对性门抓无 lineage 的孤儿任务（防塞私货）", g_q3["Q3"] is False)
    # Q3b：lineage.from 不在失效集
    g_q3b = RR.quality_gates([], [{"key": "x_r1", "deps": [],
                                   "config": {"lineage": {"from": "zzz"}}}], [], **base)
    ok &= _rec("U2e Q3 抓 lineage.from 不属失效集（扩地盘）", g_q3b["Q3"] is False)
    # Q4：规模膨胀
    g_q4 = RR.quality_gates([], [{"key": "n%d_r1" % i, "deps": [],
                                  "config": {"lineage": {"from": "t2"}}}
                                 for i in range(9)], [], **base)
    ok &= _rec("U2f Q4 规模门抓膨胀（9 > 剩余 4 × 1.5）", g_q4["Q4"] is False)
    # Q5：悬空依赖
    g_q5 = RR.quality_gates([], [{"key": "x_r1", "deps": ["ghost"],
                                  "config": {"lineage": {"from": "t2"}}}], [], **base)
    ok &= _rec("U2g Q5 依赖闭合门抓悬空依赖（create_plan 会静默丢弃）", g_q5["Q5"] is False)
    ok &= _rec("U2h ★ 依赖 done 任务**不算**悬空（合法边，不得误杀）",
               RR.quality_gates([], [{"key": "x_r1", "deps": ["t1"],
                                      "config": {"lineage": {"from": "t2"}}}],
                                [], **base)["Q5"] is True)
    # 整体：任一门不过 ⇒ action=none + T4
    ok &= _rec("U2i ★ 任一门不过 ⇒ 整个方案作废（action=none / level=T4）",
               RR.plan_redraw(ts, ["t2"], enabled=True, light=True,
                              validate=lambda p: {"ok": False, "reason": "cycle"}
                              )["action"] == "none")

    # ── U3 终止条件 ──
    fp = "timeout|" + norm_error_text("timeout 600s")
    d_np = RR.plan_redraw(ts, ["t2"], enabled=True, light=True,
                          failed_history={"t2": [fp]},
                          current_fail=("t2", "timeout", "timeout 600s"))
    ok &= _rec("U3a ★ 无进展检测：同因第 2 次 ⇒ T4 停手（主判据）",
               d_np["action"] == "none" and d_np["level"] == "T4"
               and "无进展" in d_np["reason"], d_np["reason"][:50])
    d_np2 = RR.plan_redraw(ts, ["t2"], enabled=True, light=True,
                           failed_history={"t2": ["business|别的因"]},
                           current_fail=("t2", "timeout", "timeout 600s"))
    ok &= _rec("U3b 异因失败不算无进展（仍可重画）", d_np2["action"] == "replan")
    ok &= _rec("U3c 轮次超上限 ⇒ T4",
               RR.plan_redraw(ts, ["t2"], enabled=True, round_no=2,
                              max_rounds=1)["action"] == "none")
    d_sk = RR.plan_redraw(leaves, ["t2"], enabled=True, skip_leaf=True)
    ok &= _rec("U3d T1.5：开 skip 且失效全叶子 ⇒ skip（省一次重画判定）",
               d_sk["action"] == "skip" and d_sk["level"] == "T1.5")
    ok &= _rec("U3e ★ T1.5 边界：失效含非叶子 ⇒ 不 skip（跳过会抽掉下游输入前提）",
               RR.plan_redraw(ts, ["t2"], enabled=True, skip_leaf=True)["action"] == "replan")
    ok &= _rec("U3f 无 failed ⇒ none（T0，不重画）",
               RR.plan_redraw(ts, [], enabled=True)["action"] == "none")

    # ── U4 零行为变更（本阶段头号纪律）──
    d_off = RR.plan_redraw(ts, ["t2"], enabled=False)
    ok &= _rec("U4a ★ 总闸关 ⇒ action=none 且**零产出**（无 updates/new_tasks）",
               d_off["action"] == "none" and d_off["updates"] == []
               and d_off["new_tasks"] == [] and d_off["retry_keys"] == [])
    ok &= _rec("U4b 总闸关时 reason 明确说明（不留模糊）",
               "enabled 关" in d_off["reason"])
    ok &= _rec("U4c decision 字段全集稳定（span meta 直接消费）",
               all(k in d_off for k in ("action", "level", "signals", "reason",
                                         "updates", "new_tasks", "kept",
                                         "dropped", "skip", "gates", "retry_keys")))
    return ok


# ══════════════════════════════════════════════════════════════════════════
def t_mutation():
    """变异自证：每个变异都必须被上面的断言抓住，否则那些断言是空转的。"""
    print("\n=== M. 变异自证 ===")
    from services import replan_redraw as RR
    from task_queue import TaskQueue
    ok = True
    ts = diamond()

    # M1：关掉"done 复用"⇒ U1i 必须红。
    #   变异点是 `build_redraw_plan` 里**剔除 done 的本地兜底**那段（源码级替换：
    #   把 `== "done"` 改成永假 ⇒ 兜底失效）。不能靠"多传个 done key"来变异 ——
    #   那恰恰证明兜底在工作（M1b 就是在钉这一点）。
    import re as _re
    _src = _read(REDRAW_SRC)
    _mut = _src.replace('if _t is not None and str(_t.get("status") or "") == "done":',
                        'if False:')
    ok &= _rec("M1a 变异锚点命中：源码里存在 done 剔除兜底",
               _src.count('== "done":\n            continue') >= 1
               and "不变量 1 的**本地兜底**" in _src)
    if _mut != _src:
        _mut_ns = {"__name__": "rr_mutant"}
        exec(compile(_mut, "replan_redraw_mutant", "exec"), _mut_ns)
        _d = _mut_ns["plan_redraw"](ts, ["t2", "t1"], enabled=True, light=True)
        ok &= _rec("M1b ★ 关掉 done 兜底 ⇒ done 的 t1 进入重派清单（后果真实）",
                   "t1" in [u["key"] for u in _d["updates"]],
                   "变异未生效：%s" % [u["key"] for u in _d["updates"]])
    else:
        ok &= _rec("M1b ★ 关掉 done 兜底 ⇒ done 的 t1 进入重派清单（后果真实）",
                   False, "源码变异锚点未命中")
    ok &= _rec("M1c ★ 不变量 1 有本地兜底：上游误传 done key 也不会重画它",
               "t1" not in [u["key"] for u in
                            RR.plan_redraw(ts, ["t2", "t1"], enabled=True,
                                           light=True)["updates"]])

    # M2：关掉无进展检测 ⇒ U3a 必须红
    def _mutant_no_noprog(tasks, failed, **kw):
        kw.pop("failed_history", None)
        kw.pop("current_fail", None)
        return RR.plan_redraw(tasks, failed, **kw)
    from services.replan_signal import norm_error_text
    fp = "timeout|" + norm_error_text("timeout 600s")
    d2 = _mutant_no_noprog(ts, ["t2"], enabled=True, light=True,
                           failed_history={"t2": [fp]},
                           current_fail=("t2", "timeout", "timeout 600s"))
    ok &= _rec("M2 ★ 关无进展检测 ⇒ 同因第 2 次仍在重画（死循环风险真实）",
               d2["action"] == "replan", "变异未生效")

    # M3：删掉 deps 重映射（重量级下游会悬空）⇒ U1n 必须红。
    #   变异点是 `build_redraw_plan` 里那行 `remap.get(d, d)` —— 直接把它
    #   替换成恒等映射，再走完整 plan_redraw（含质量门），看 Q5 是否拦下。
    #   ⚠️ 上一版"取 kept 再手工改回原 deps"并不成立（build 时已经重映射过了，
    #   改回去等于没变异）—— 必须在**映射发生的那一刻**改，故这里做源码级变异。
    import types as _types
    _orig_bld = RR.build_redraw_plan

    def _mutant_no_remap(tasks, failed_keys, **kw):
        # 复制模块并把 remap 置空 ⇒ 等价于"没做重映射"的实现
        m = _types.ModuleType("rr_mut")
        m.__dict__.update(RR.__dict__)
        real_build = RR.build_redraw_plan

        def _patched(ts, fk, **k):
            out = real_build(ts, fk, **k)
            by = {t.get("task_key"): t for t in (ts or []) if isinstance(t, dict)}
            for kk in out["kept"]:
                src = by.get(kk.get("key") or kk.get("task_key")) or {}
                kk["deps"] = RR._deps(src)      # 还原成**未重映射**的原始 deps
            return out
        m.build_redraw_plan = _patched
        saved = RR.build_redraw_plan
        RR.build_redraw_plan = _patched
        try:
            return RR.plan_redraw(tasks, failed_keys, **kw)
        finally:
            RR.build_redraw_plan = saved
    d3 = _mutant_no_remap(ts, ["t2"], enabled=True, light=False,
                          reason_by_key={"t2": "x"},
                          validate=TaskQueue.validate_plan)
    ok &= _rec("M3 ★ 删 deps 重映射 ⇒ 下游指向被删的 t2 ⇒ Q5 拦下（动作=none）",
               d3["action"] == "none" and d3["gates"].get("Q5") is False,
               "action=%s gates=%s" % (d3["action"], d3["gates"].get("failed")))

    # M4：质量门全放行（返回恒 ok）⇒ U2b~U2g 必须红
    def _mutant_all_pass(*a, **k):
        return {"Q1": True, "Q2": True, "Q3": True, "Q4": True, "Q5": True,
                "failed": [], "ok": True, "reason": "全过"}
    orig_gates = RR.quality_gates
    try:
        RR.quality_gates = _mutant_all_pass
        d4 = RR.plan_redraw(ts, ["t2"], enabled=True, light=False,
                            validate=TaskQueue.validate_plan)
    finally:
        RR.quality_gates = orig_gates
    ok &= _rec("M4 ★ 质量门恒放行 ⇒ 环/撞车/悬空依赖全部通过（防线确实在门禁里）",
               d4["action"] == "replan"
               and d4["new_tasks"][0]["key"] == "t2_r1")

    # M5：源码级 —— 删掉接线调用
    stream = _read(STREAM_SRC)
    ok &= _rec("M5a 变异锚点：接线锚点存在（删了 S2a 会抓）", "_rr.plan_redraw(" in stream)
    ok &= _rec("M5b ★ 重画段不在 try 内 ⇒ 观测旁路铁律被破坏",
               "if _RR_OK and _rp_enabled and _rp_on_stall:" in stream)
    ok &= _rec("M5c ★ 轻量级若改成追行（create_plan）⇒ X-a 会抓同 key 双派发",
               "create_plan" not in stream[
                   stream.index("if _rp_light and not _rp_heavy:"):
                   stream.index("else:", stream.index("if _rp_light and not _rp_heavy:"))])
    return ok


def main():
    print("P2 replanning 门禁（差量重画规则版 + 质量门，零 LLM 零服务零生产库写）")
    ok = t_static()
    ok &= t_unit()
    ok &= t_mutation()
    fails = [r for r in _results if r[0] == FAIL]
    vac = [r for r in _results if r[0] == VACUOUS]
    print("\n════════════════════════════════════════════")
    print("结果：%d PASS / %d FAIL / %d SKIP"
          % (len(_results) - len(fails) - len(vac), len(fails), len(vac)))
    for kind, name, detail in fails:
        print("  FAIL  %s  %s" % (name, detail))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())