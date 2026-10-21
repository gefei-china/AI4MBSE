# -*- coding: utf-8 -*-
"""P2 replanning：执行中差量重画算法（纯函数，2026-10-21）。

**为什么需要**（v2 §4.10 / v1 §4.2，对标差距 G4/G5）：
    P1 把信号收上来了（`replan_signal`），但信号本身不改变图 —— 一个任务失败后，
    主循环在收敛点只能 `block()` 全部剩余任务然后 break，**用户的计划就此作废**。
    P2 补上这一步：**在证据充分时局部修图，而不是整批冻结**。

**四条不变量**（v1 §4.1，缺一条就不做 —— 本模块以结构约束内建）：
    1. done 永不重跑（幂等，防重复写实体 —— 本仓 #50/#51 附件重建教训同源）；
    2. key 稳定：T2-重产出**新 key**（`t4_r1`）+ `config.lineage`，不用同 key 覆盖
       （覆盖会让"为什么这条任务没了"不可追溯 —— validate_plan 注释确立的同款纪律）；
    3. 新图必过 `validate_plan`（Q1），strict，不过就整个方案作废（T4），
       绝不"尽力而为落库"；
    4. 增量约束：重画只允许动**失效子图**，done 的结论进 context 引用而非重跑。

**轻/重两级的真正区别（v2 §4.2）** —— 不是"改多少字段"，而是**动不动图**：

| 级别 | 动作 | 图拓扑 | key | 适用 |
|---|---|---|---|---|
| T2-轻 | **原地重派** failed 任务：同 key、deps 不变、context 注入失败原因、retry_count+1 | 不动 | 不变 | 失败原因明确（timeout/校验失败），换上下文即可换条路 |
| T2-重 | **新 key 替代** failed 任务（`t4_r1`）+ 下游 deps 重映射 | 下游依赖边改写 | 新 | 需要换 Agent / 换实现路径 |

    ⚠️ 轻量级**绝不 create_plan 追行**：`agent_tasks` 对 `(run_id, task_key)`
    无唯一约束，同 key 追一行会让 `ready_tasks` 派发两次 ⇒ 必须走原地 UPDATE。
    这条是 P2 最容易踩的坑，门禁 G2-d 有对应断言。

**为什么第一版是"规则版"而非 LLM 版**（v1 §4.2 末尾的设计选择，v2 沿用）：
    LLM 重画的不确定性会直接冲击本仓"确定性保持"铁律（Mock 环境行为漂移）。
    规则版靠拼装（把失败原因注入 context 重派）拿到主要收益，零 LLM 调用。
    LLM 版（`llm_redraw`）归 T3，默认关。

**上下文口径（G10）**：被替换 key 的 offload（共享产物工作区引用）**不**传给新任务
    —— 其产物前提已被推翻；done key 的 offload 照常传。"曾尝试方向"来自失败任务
    的 error 摘要，显式写进 context 防"原样再撞一次"。

**纯函数纪律**：零 IO、零 DB、零配置读取（开关由调用方传入）——
    质量门必须可被门禁逐格单测（G2）与内存孪生变异（G3）。配置读取在调用方
    （`core.config.get("replanning", ...)`）。

**动作语义**（v2 §4.2 矩阵 P2 可执行格）：

| 动作 | 级别 | 含义 | 调用方动作 |
|---|---|---|---|
| `none` | T0/T4 | 不重画（总闸关 / 无重画对象 / 质量门不过 / 超上限 / 无进展） | 原逻辑 block+break |
| `skip` | T1.5 | 叶子失效任务标记为主动跳过（省一次重画判定） | 决策留痕（不改终态） |
| `replan` | T2-轻 | 原地重派 failed 任务（UPDATE context/status） | UPDATE + 重派 |
| `replan` | T2-重 | 新 key 替代 + 下游重映射 | create_plan + continue |
"""
import json

# ── 动作与级别常量（与 replan_signal 同款命名纪律）─────────────────────────
ACTION_NONE = "none"
ACTION_SKIP = "skip"
ACTION_REPLAN = "replan"

LEVEL_SKIP = "T1.5"
LEVEL_LIGHT = "T2-light"    # 轻量级：同 key 原地重派，拓扑不动
LEVEL_HEAVY = "T2-heavy"    # 重量级：新 key + lineage，下游依赖边重映射
LEVEL_NONE = "T0"
LEVEL_ESCALATE = "T4"       # 升级人工
LEVEL_LLM = "T3"            # ★ P3 新增：LLM 整图重画（动作仅 replace，skip/insert 留 P4）

#: 逆向可达的防御性跳数上限（环由 Q1 兜底，但本函数不能因此死循环）。
_MAX_HOPS = 64


# ── 行归一化小工具（DB 行是 dict，deps/config 可能是 JSON 字符串）──────────
def _key(t):
    return str((t or {}).get("task_key") or (t or {}).get("key") or "")


def _deps(t):
    d = (t or {}).get("deps")
    if isinstance(d, str):
        try:
            d = json.loads(d or "[]")
        except Exception:
            return []
    return [str(x) for x in d] if isinstance(d, (list, tuple)) else []


def _cfg(t):
    c = (t or {}).get("config")
    if isinstance(c, str):
        try:
            c = json.loads(c or "{}")
        except Exception:
            return {}
    return c if isinstance(c, dict) else {}


def _terminal(status):
    """终态判定（**含两种拼写** —— `TaskQueue.cancel` 写 'cancelled'（双 L），
    而 `pending_count`/`STATUSES` 用 'canceled'（单 L），这是仓内既存不一致；
    本模块必须两种都认，否则被取消的任务会被当成"还要跑"而重画）。"""
    return str(status or "") in ("done", "failed", "cancelled", "canceled")


def _norm_error_text(text, _norm=None):
    if _norm is None:
        try:
            from services.replan_signal import norm_error_text as _n
            _norm = _n
        except Exception:
            def _norm(x):
                return str(x or "")[:160]
    return _norm(text or "")


# ── 步骤 1：失效子图识别 ──────────────────────────────────────────────────
def propagation_set(tasks, failed_keys):
    """失效传播集 = failed 本身 ∪ 其**全部下游**（deps 图逆向可达）。

    为什么传播：下游任务的输入前提就是上游产物 —— 上游失败后留在图上只会 block。
    （真库基线：失效传播代价目前为 0，但那是"失败全在叶子"的巧合，不能当契约。）

    防环：逐跳 frontier（有界 BFS）+ `_MAX_HOPS` 上限；环由 Q1 结构门兜底。
    """
    downstream = {}
    for t in (tasks or []):
        if not isinstance(t, dict):
            continue
        tk = _key(t)
        if not tk:
            continue
        for d in _deps(t):
            downstream.setdefault(d, []).append(tk)

    out = set(str(k) for k in (failed_keys or []) if str(k))
    frontier, hops = list(out), 0
    while frontier and hops < _MAX_HOPS:
        nxt = []
        for k in frontier:
            for dk in downstream.get(k, ()):
                if dk not in out:
                    out.add(dk)
                    nxt.append(dk)
        frontier, hops = nxt, hops + 1
    return out


def skip_candidates(tasks, failed_keys):
    """T1.5 skip 集：failed 中**无下游**的任务（叶子失效）。

    为什么只认叶子（v2 §4.2「skip 的规则版边界」）：叶子失败今天已经不会冻结别人
    （`ready_tasks` 不会恒空），skip 的增量价值仅是"明确标记为主动跳过而非失败"，
    省下一次重画判定。**有下游的失效任务绝不能 skip** —— 跳过它等于把下游的
    输入前提静默抽掉，比 block 更危险。
    """
    has_downstream = set()
    for t in (tasks or []):
        if isinstance(t, dict):
            for d in _deps(t):
                has_downstream.add(d)
    return sorted(k for k in (str(x) for x in (failed_keys or []))
                  if k and k not in has_downstream)


# ── 步骤 2/4：规则版重画（T2-轻 原地重派 / T2-重 新 key）──────────────────
def _inject(reason, *, round_no, level, old_key=""):
    if level == LEVEL_LIGHT:
        head = "【重规划 r%d·轻】上次失败原因：%s" % (int(round_no or 1),
                                                reason or "未提供")
    else:
        head = ("【重规划 r%d·重】替代任务（原 %s 失败：%s）"
                % (int(round_no or 1), old_key, reason or "未提供"))
    return (head + "。请**换一条实现路径**达成同一产出，不要重复上次的做法；"
                   "若判定原目标本身不可达成，请在产出里显式说明理由。")


def build_redraw_plan(tasks, failed_keys, *, reason_by_key=None, round_no=1,
                      light=True, agent_override=None):
    """构建重画方案（纯函数）。

    返回：
        `{"updates": [...], "new_tasks": [...], "kept": [...],
          "dropped": [...], "skip": [...], "remap": {old:new}}`

    · `updates`（**仅轻量级**）：原地重派清单 `[{"id", "key", "context"}]` ——
      调用方对既有行做 UPDATE（status 回 planned/ready + context 覆盖 +
      retry_count+1），**不追行**（追行会让同 key 派发两次）；
    · `new_tasks`（**仅重量级**）：新 key 替代任务，含 `config.lineage`；
    · `kept`：保留集 K 中未执行任务（重量级下 deps 已重映射到新 key）；
    · `dropped`：从图上移除的 key（重量级 = failed 本身；轻量级为空 —— 图不动）；
    · `skip`：T1.5 叶子跳过集。
    """
    reason_by_key = reason_by_key or {}
    agent_override = agent_override or {}
    by_key = {_key(t): t for t in (tasks or []) if isinstance(t, dict)}
    # ★ 不变量 1 的**本地兜底**（不信任调用方只传 failed）：即使上游误把 done 的
    #   key 塞进 failed_keys，这里也会剔除 —— "done 永不重跑"是本模块的硬约束，
    #   不能靠"调用方应该只传失败任务"这种口头约定（v1 §4.1 不变量 1）。
    failed = set()
    for k in (failed_keys or []):
        k = str(k or "")
        if not k:
            continue
        _t = by_key.get(k)
        if _t is not None and str(_t.get("status") or "") == "done":
            continue            # done ⇒ 绝不重画（幂等铁律）
        failed.add(k)
    invalid = propagation_set(tasks, failed)
    skips = skip_candidates(tasks, failed)

    updates, new_tasks, remap = [], [], {}

    if light:
        # ── T2-轻：只重派 failed 本身（下游在它重新 done 后自然解锁）──
        for k in sorted(failed):
            t = by_key.get(k)
            if t is None:
                continue
            ctx = str(t.get("context") or "")
            inject = _inject(reason_by_key.get(k), round_no=round_no,
                             level=LEVEL_LIGHT, old_key=k)
            updates.append({"id": t.get("id"), "key": k,
                            "context": ((ctx + "\n" + inject) if ctx else inject)[:1000]})
        # 图拓扑完全不动 ⇒ dropped 为空，kept = 全部未执行任务（deps 原样）
        kept = []
        for t in (tasks or []):
            if not isinstance(t, dict) or not _key(t):
                continue
            if _key(t) in invalid or _terminal(t.get("status")):
                continue
            nt = dict(t)
            nt["key"] = _key(t)     # 归一化：与 new_tasks 同一形状（见重量级分支注释）
            kept.append(nt)
        return {"updates": updates, "new_tasks": [], "kept": kept,
                "dropped": [], "skip": skips, "remap": remap}

    # ── T2-重：新 key 替代 failed + 下游 deps 重映射 ──
    for k in sorted(failed):
        t = by_key.get(k)
        old_ctx = str(t.get("context") or "") if t else ""
        inject = _inject(reason_by_key.get(k), round_no=round_no,
                         level=LEVEL_HEAVY, old_key=k)
        new_key = "%s_r%d" % (k, int(round_no or 1))
        remap[k] = new_key
        new_tasks.append({
            "key": new_key,
            "title": str((t or {}).get("title") or k),
            "agent": str(agent_override.get(k)
                         or (t or {}).get("agent_id") or (t or {}).get("agent") or ""),
            "task_type": str((t or {}).get("task_type") or "agent"),
            "deps": [remap.get(d, d) for d in _deps(t)],
            "seq": int((t or {}).get("seq") or 0),
            "context": ((inject + "\n" + old_ctx) if old_ctx else inject)[:1000],
            "expected_output": str((t or {}).get("expected_output") or "")[:500],
            "config": dict(_cfg(t), lineage={"from": k, "round": int(round_no or 1),
                                             "reason": reason_by_key.get(k) or "",
                                             "level": LEVEL_HEAVY}),
        })

    # 保留集 = 全图 - **failed 本身**；其中未执行任务的 deps 指向 failed 的改指新 key。
    #   ⚠️ 注意这里刻意**不**排除失效传播里的下游任务：它们没失败，只是"依赖的前提
    #   被换掉了"—— 丢掉它们等于让用户剩下的活凭空消失（比 block 更糟）。
    #   真正需要处理的是它们的 deps 边：remap 把 t2 换成 t2_r1，任务本身原样保留。
    kept = []
    for t in (tasks or []):
        if not isinstance(t, dict):
            continue
        k = _key(t)
        if not k or k in failed or _terminal(t.get("status")):
            continue
        nt = dict(t)
        nt["deps"] = [remap.get(d, d) for d in _deps(t)]
        nt["key"] = k          # 归一化：调用方（stream.py UPDATE）按 key 定位，不必
        nt["deps_json"] = json.dumps(nt["deps"], ensure_ascii=False)
        kept.append(nt)

    return {"updates": [], "new_tasks": new_tasks, "kept": kept,
            "dropped": sorted(failed), "skip": skips, "remap": remap}


# ── 步骤 5：质量门 Q1~Q5（v2 §4.5；任一不过 ⇒ 整个方案作废 ⇒ T4）──────────
def quality_gates(updates, new_tasks, kept, *, failed_keys=None, expand_ratio=1.5,
                  remaining_count=0, validate=None, done_keys=None, full_plan=None):
    """五门质量门。返回 `{"Q1".."Q5": bool, "failed": [...], "ok": bool, "reason": str}`。

    `validate` 注入 `TaskQueue.validate_plan`（纯函数纪律：不 import task_queue；
    测试可传假实现做变异自证）。

    `done_keys` / `full_plan` 解决一个**容易写错的口径**：依赖闭合门（Q5）与结构门
    （Q1）必须按"**重画后的完整图**"判，不能只看新增行 ——
    · 轻量级下图不变，依赖指向 done 任务完全合法（done 仍在图上，是被依赖的**结果**）；
    · 重量级下 done 任务同样留在图里当被依赖方。
    把 done key 漏出合法集合 ⇒ Q5 误杀所有"依赖已完成任务"的重画（本仓常见图）。
    """
    res = {}
    failed = set(str(k) for k in (failed_keys or []) if str(k))
    new_tasks = list(new_tasks or [])
    kept = list(kept or [])
    done_keys = set(str(k) for k in (done_keys or []) if str(k))

    # Q1 结构门：整张候选图过 validate_plan（环/自环/重复 key/悬空依赖）
    if full_plan is not None:
        plan = [{"key": str(t.get("key") or t.get("task_key") or ""),
                 "title": str(t.get("title") or ""),
                 "agent": t.get("agent_id") or t.get("agent") or "",
                 "deps": _deps(t)} for t in full_plan]
    else:
        plan = [{"key": str(t.get("key") or t.get("task_key") or ""),
                 "title": str(t.get("title") or ""),
                 "agent": t.get("agent_id") or t.get("agent") or "",
                 "deps": _deps(t)} for t in (new_tasks + kept)]
    try:
        vd = (validate or _fallback_validate)(plan)
        res["Q1"] = bool(vd and vd.get("ok"))
        res["_detail"] = str((vd or {}).get("reason") or "")
    except Exception as e:
        res["Q1"], res["_detail"] = False, "validate 异常：%s" % e

    # Q2 职责门：新 key 不与任何既有 key 相同（撞车=重复建房，比漏重画更糟）。
    #   不变量 1 在此一并兜底：done 的 key 也不许出现在新任务里。
    existing = set(failed) | done_keys
    for t in kept:
        existing.add(str(t.get("key") or t.get("task_key") or ""))
    for u in (updates or []):
        existing.add(str(u.get("key") or ""))
    new_keys = [str(t.get("key") or "") for t in new_tasks]
    res["Q2"] = all(k and k not in existing for k in new_keys)

    # Q3 针对性门：每个新任务必须有 lineage 且 `from` ∈ 失效集
    #   （防"借机塞私货/扩地盘" —— 规则版的等价风险是"重画顺手多建任务"）
    ok3 = True
    for t in new_tasks:
        lin = _cfg(t).get("lineage")
        if not isinstance(lin, dict) or not lin.get("from") or str(lin["from"]) not in failed:
            ok3 = False
            break
    res["Q3"] = ok3

    # Q4 规模门：新任务数 ≤ 原图剩余任务数 × expand_ratio
    base = int(remaining_count or 0)
    res["Q4"] = True if base <= 0 else len(new_tasks) <= base * float(expand_ratio or 1.5)

    # Q5 依赖闭合门：新/保留任务 deps ⊆ {保留 key} ∪ {新 key} ∪ {done key}。
    #   `create_plan` 会**静默丢弃**不存在的依赖 —— 重画场景必须显式断言，
    #   否则"依赖被静默删掉"会变成一条查不到来源的断链。
    valid_keys = {str(t.get("key") or "") for t in new_tasks}
    valid_keys |= {str(t.get("key") or t.get("task_key") or "") for t in kept}
    valid_keys |= done_keys          # 依赖已完成任务是合法的（见 docstring 口径）
    ok5 = True
    for t in new_tasks + kept:
        for d in _deps(t):
            if str(d) not in valid_keys:
                ok5 = False
                break
        if not ok5:
            break
    res["Q5"] = ok5

    bad = [g for g in ("Q1", "Q2", "Q3", "Q4", "Q5") if not res.get(g)]
    res["failed"] = bad
    res["ok"] = not bad
    res["reason"] = ("质量门全过" if not bad else
                     "质量门不过：%s%s" % ("/".join(bad),
                                     ("（%s）" % res["_detail"]) if "Q1" in bad else ""))
    return res


def _fallback_validate(plan):
    """兜底结构校验（无注入时）。真调用方一律传 `TaskQueue.validate_plan`。"""
    keys = [str(t.get("key") or "") for t in plan]
    if len(set(keys)) != len(keys):
        return {"ok": False, "reason": "duplicate_key"}
    if any(not k for k in keys):
        return {"ok": False, "reason": "empty_key"}
    for t in plan:
        if str(t.get("key") or "") in {str(d) for d in _deps(t)}:
            return {"ok": False, "reason": "self_loop"}
    return {"ok": True, "reason": ""}


# ── 步骤 5.5：无进展检测（v1 §3.3，终止条件主判据）───────────────────────
def no_progress(failed_history, key, error_class, error_text, *, _norm=None):
    """同 key 谱系上同因失败出现第 2 次 ⇒ 无进展 ⇒ 直接 T4。

    比轮次上限更根本：轮次上限是兜底，无进展检测是主判据（v1 §3.3）。
    `failed_history`：`{key: [指纹, ...]}`（调用方从库中 error/error_class 累积）。
    """
    fp = "%s|%s" % (str(error_class or ""), _norm_error_text(error_text, _norm))
    return fp in list(failed_history.get(str(key)) or [])


# ── 主入口 ────────────────────────────────────────────────────────────────
def plan_redraw(tasks, failed_keys, *, reason_by_key=None, round_no=1,
                enabled=False, light=True, skip_leaf=False, max_rounds=1,
                expand_ratio=1.5, failed_history=None, current_fail=None,
                agent_override=None, validate=None):
    """P2 主入口：差量重画判定（纯函数，v2 §4.10 全流程）。

    返回 decision dict（字段与 `replan_signal` 同款，可直接转 span meta）：
        `{"action", "level", "signals", "reason", "updates", "new_tasks",
          "kept", "dropped", "skip", "gates", "retry_keys"}`
    """
    failed = set(str(k) for k in (failed_keys or []) if str(k))

    # 0. 总闸关 ⇒ none（**零行为变更铁律**：默认关必须与 P1 完全一致）
    if not enabled:
        return _none("replanning.enabled 关 ⇒ 不重画（维持 P1 原逻辑）",
                     level=(LEVEL_ESCALATE if failed else LEVEL_NONE))
    if not failed:
        return _none("无 failed ⇒ 无重画需求", level=LEVEL_NONE)

    # 1. 无进展检测（主判据，先于规模/质量门 —— 同因再撞就别烧钱了）
    if current_fail and failed_history:
        k, ec, et = current_fail
        if no_progress(failed_history, k, ec, et):
            return _none("无进展检测：%s 同因失败第 2 次 ⇒ 停手升级人工（T4）" % k,
                         level=LEVEL_ESCALATE)

    # 2. 轮次上限（兜底）
    if int(round_no or 1) > int(max_rounds or 1):
        return _none("重画轮次 %d > 上限 %d ⇒ T4" % (int(round_no or 1),
                                                  int(max_rounds or 1)),
                     level=LEVEL_ESCALATE)

    # 3. 构建方案
    built = build_redraw_plan(tasks, failed, reason_by_key=reason_by_key,
                              round_no=round_no, light=light,
                              agent_override=agent_override)
    updates, new_tasks, kept = built["updates"], built["new_tasks"], built["kept"]

    # 3.5 T1.5 优先：开 skip_leaf 且**失效全是叶子** ⇒ 主动跳过，不进重画
    #   （设计 v2 §4.2：skip 的价值正是"省一次重画判定" —— 叶子失败不冻结任何人，
    #    没有重画对象可派；混合作业（部分叶子部分有下游）仍走重画，skip 集随决策留痕）
    if skip_leaf and built["skip"] and failed <= set(built["skip"]):
        return {"action": ACTION_SKIP, "level": LEVEL_SKIP, "signals": [],
                "reason": "失效任务均为叶子（%s）⇒ T1.5 主动跳过，不重画"
                          % ",".join(built["skip"]),
                "updates": [], "new_tasks": [], "kept": [],
                "dropped": [], "skip": built["skip"],
                "gates": {}, "retry_keys": []}

    # 4. 无可重画对象（failed 行不在 tasks 里查得到）⇒ T4
    if not updates and not new_tasks:
        return _none("失效子图无可重画对象（叶子且未开 skip）⇒ T4",
                     level=LEVEL_ESCALATE, dropped=built["dropped"],
                     skip=built["skip"], kept=kept)

    # 5. 质量门（任一不过 ⇒ 整个方案作废 ⇒ T4）
    remaining = sum(1 for t in (tasks or [])
                    if isinstance(t, dict) and not _terminal(t.get("status")))
    done_keys = set(_key(t) for t in (tasks or [])
                    if isinstance(t, dict) and str(t.get("status") or "") == "done")
    # 重画后的完整图：done 任务（留存被依赖）+ 新任务 + 保留集。
    #   轻量级下 `updates` 改的是既有行，故 full_plan 用**全量任务行**（图不变）；
    #   重量级下失效行被新 key 替代，故用 新任务 ∪ 保留集 ∪ done。
    if light:
        full_plan = [{"key": k, "deps": _deps(t),
                      "agent": t.get("agent_id") or "", "title": t.get("title") or ""}
                     for t in (tasks or []) if isinstance(t, dict)
                     for k in [_key(t)] if k]
    else:
        full_plan = ([{"key": str(t.get("key") or ""), "deps": t.get("deps") or [],
                       "agent": t.get("agent") or "", "title": t.get("title") or ""}
                      for t in new_tasks]
                     + [{"key": str(t.get("key") or t.get("task_key") or ""),
                         "deps": _deps(t), "agent": t.get("agent_id") or "",
                         "title": t.get("title") or ""} for t in kept]
                     + [{"key": k, "deps": _deps(t), "agent": t.get("agent_id") or "",
                         "title": t.get("title") or ""}
                        for t in (tasks or []) if isinstance(t, dict)
                        for k in [_key(t)] if k and k in done_keys])
    gates = quality_gates(updates, new_tasks, kept, failed_keys=failed,
                          expand_ratio=expand_ratio, remaining_count=remaining,
                          validate=validate, done_keys=done_keys,
                          full_plan=full_plan)
    if not gates.get("ok"):
        return _none(gates.get("reason"), level=LEVEL_ESCALATE,
                     dropped=built["dropped"], skip=built["skip"], gates=gates)

    lvl = LEVEL_LIGHT if light else LEVEL_HEAVY
    targets = updates if light else new_tasks
    return {"action": ACTION_REPLAN, "level": lvl, "signals": [],
            "reason": "%s 差量重画 %d 个任务（失效传播 %d 个，保留 %d 个）"
                      % (lvl, len(targets), len(propagation_set(tasks, failed)), len(kept)),
            "updates": updates, "new_tasks": new_tasks, "kept": kept,
            "dropped": built["dropped"], "skip": built["skip"], "gates": gates,
            "retry_keys": [str(t.get("key") or "") for t in targets]}


def _none(reason, *, level=LEVEL_ESCALATE, dropped=None, skip=None, kept=None,
          gates=None):
    return {"action": ACTION_NONE, "level": level, "signals": [], "reason": reason,
            "updates": [], "new_tasks": [], "kept": kept or [],
            "dropped": dropped or [], "skip": skip or [],
            "gates": gates or {}, "retry_keys": []}


# ── P3：T3 LLM 整图重画（默认关，动作范围锁定 replace）───────────────────
def plan_llm_redraw(tasks, failed_keys, *, llm_raw="", parse=None, enabled=False,
                    round_no=1, max_rounds=1, expand_ratio=1.5,
                    directive="", goal="", done_summaries=None, validate=None,
                    parse_error=""):
    """T3：把 LLM 产出的整图接进**与规则版同一套质量门**（P3 新增）。

    **为什么单独一个入口而不是给 `plan_redraw` 加 `llm_raw` 参数**：
    两者的前置条件差得远 —— 规则版的输入是"库里已有的失败任务"，LLM 版的
    输入是"用户指令（S1/S2）经影响评估后判定需推翻任务划分"。混进一个函数
    会让「LLM 版到底受不受 Q1~Q5 约束」这件事在代码里不可见 —— 而这正是
    本仓最容易被后续改动悄悄拆掉的防线（P2 的 Q2/Q5 就是这么保住"撞车/悬空
    依赖"两类事故的）。**同一个 `quality_gates` 调用点**是这条防线唯一的
    保证方式。

    **范围锁定 replace**（拍板⑤，v2 §6 决策表）：LLM 版只允许产出失效子图的
    替代任务；`skip`（删任务）与 `insert`（凭空加任务）留 P4 后评估。
    实现方式是**解析层即拒**（`parse` 注入 `replan_directive.parse_llm_plan`，
    由它保证只返回替换任务）**+ Q3 兜底**（每个新任务必须有
    `lineage.from ∈ failed`）。两道都在，才叫"锁定"—— 只靠提示词约束
    等于没有约束（v1 §4.10 反复出现的同一条纪律）。

    **默认关 ⇒ 零行为变更**：`enabled=False` 时本函数恒返回 none，
    且不调用 `parse`（LLM 调用已在编排层按开关前置拦截）。

    返回与 `plan_redraw` **同款** decision dict（含 `gates`），便于
    stream.py 走同一套应用分支与 span 装配。
    """
    failed = set(str(k) for k in (failed_keys or []) if str(k))

    if not enabled:
        return _none("replanning.llm_redraw 关 ⇒ 不做 LLM 整图重画", level=LEVEL_NONE)
    if not llm_raw and not parse_error:
        return _none("LLM 未产出可用结果 ⇒ 不重画（T4）", level=LEVEL_ESCALATE)
    if int(round_no or 1) > int(max_rounds or 1):
        return _none("重画轮次 %d > 上限 %d ⇒ T4" % (int(round_no or 1),
                                                  int(max_rounds or 1)),
                     level=LEVEL_ESCALATE)
    # 解析失败 ⇒ 直接 T4（绝不"尽力而为落库"，同不变量 3）
    if parse_error:
        return _none("LLM 输出不可用：%s ⇒ T4" % parse_error, level=LEVEL_ESCALATE)

    parse_fn = parse
    if parse_fn is None:
        try:
            from services.replan_directive import parse_llm_plan as parse_fn
        except Exception:
            return _none("replan_directive 不可用 ⇒ 无法校验 LLM 输出，T4",
                         level=LEVEL_ESCALATE)

    try:
        new_tasks, perr = parse_fn(llm_raw, sorted(failed), round_no=round_no)
    except Exception as e:
        return _none("LLM 输出解析异常：%s ⇒ T4" % e, level=LEVEL_ESCALATE)
    if perr or not new_tasks:
        return _none("LLM 输出无有效任务：%s ⇒ T4" % (perr or "空"), level=LEVEL_ESCALATE)

    # ★ 不变量 1 的第二道本地兜底：解析层已保证 `from ∈ failed`，这里再剔一次
    #   —— Q3 会查它，但 Q3 的语义是"新任务有 lineage"，而不是"lineage 指向
    #   合法失效集"。差别在于：若有人把 Q3 改成宽松版，这条兜底仍在。
    def _lin_from(t):
        lin = _cfg(t).get("lineage")
        return str((lin or {}).get("from") or "") if isinstance(lin, dict) else ""

    bad = [t for t in new_tasks if _lin_from(t) not in failed]
    if bad:
        return _none("LLM 产出了 %d 个非替代任务（lineage.from ∉ 失效集）⇒ "
                     "超出 replace 范围，T4" % len(bad), level=LEVEL_ESCALATE)

    # 保留集：与规则版重量级同口径 —— 全图 - failed 本身；未执行任务的 deps
    #   指向 failed 的改指**替代它的新 key**。
    #   ⚠️ 映射方向：`{old_key: new_key}`（下游 deps 里存的是**旧** key）。
    #     规则版由 `build_redraw_plan` 在生成新 key 时同时维护 remap；LLM 版的
    #     new key 由模型命名，只能在这里按 `lineage.from` 反查建立映射 ——
    #     若建成 `{new: new}` 恒等映射，下游 deps 会继续指向**已被删除的旧
    #     key**，Q5 因"旧 key 既不在保留集也不在 done 集"而拦下，整次重画必然
    #     失败（表现为"LLM 开了但永远 T4"）。这是 LLM 接线最容易错的一处。
    remap = {}
    for t in new_tasks:
        frm = _lin_from(t)
        if frm and frm not in remap:
            remap[frm] = str(t.get("key") or "")
    kept = []
    for t in (tasks or []):
        if not isinstance(t, dict):
            continue
        k = _key(t)
        if not k or k in failed or _terminal(t.get("status")):
            continue
        nt = dict(t)
        nt["deps"] = [remap.get(d, d) for d in _deps(t)]
        nt["key"] = k
        nt["deps_json"] = json.dumps(nt["deps"], ensure_ascii=False)
        kept.append(nt)

    remaining = sum(1 for t in (tasks or [])
                    if isinstance(t, dict) and not _terminal(t.get("status")))
    done_keys = set(_key(t) for t in (tasks or [])
                    if isinstance(t, dict) and str(t.get("status") or "") == "done")
    full_plan = ([{"key": str(t.get("key") or ""), "deps": t.get("deps") or [],
                   "agent": t.get("agent") or "", "title": t.get("title") or ""}
                  for t in new_tasks]
                 + [{"key": str(t.get("key") or t.get("task_key") or ""),
                     "deps": _deps(t), "agent": t.get("agent_id") or "",
                     "title": t.get("title") or ""} for t in kept]
                 + [{"key": k, "deps": _deps(t), "agent": t.get("agent_id") or "",
                     "title": t.get("title") or ""}
                    for t in (tasks or []) if isinstance(t, dict)
                    for k in [_key(t)] if k and k in done_keys])
    gates = quality_gates([], new_tasks, kept, failed_keys=failed,
                          expand_ratio=expand_ratio, remaining_count=remaining,
                          validate=validate, done_keys=done_keys,
                          full_plan=full_plan)
    if not gates.get("ok"):
        return _none(gates.get("reason"), level=LEVEL_ESCALATE,
                     dropped=sorted(failed), gates=gates)

    return {"action": ACTION_REPLAN, "level": LEVEL_LLM, "signals": [],
            "reason": "T3 LLM 整图重画：替代 %d 个失效任务（保留 %d 个，过 Q1~Q5）"
                      % (len(new_tasks), len(kept)),
            "updates": [], "new_tasks": new_tasks, "kept": kept,
            "dropped": sorted(failed), "skip": [], "remap": remap,
            "gates": gates,
            "retry_keys": [str(t.get("key") or "") for t in new_tasks]}