# -*- coding: utf-8 -*-
"""P3 replanning：用户指令影响评估（DAG 级，2026-10-21）。

**为什么需要它**（v2 §4.7 / G8 🔴）：
    P1 收了信号、P2 会局部修图，但两者都只对"**任务自己失败**"有反应。
    真实场景里最高频的重规划触发是**用户在 DAG 执行中改主意**：
    「第三部分换成表格」「不要做性能分析了」「再加一个评审任务」。

    v1 §3.2 把 S1（用户生成中补充指令）直接归 T3（整图重画）——**这是缺口**：
    一句「辛苦了」会触发整图重画烧掉一次 planner 调用，而"改一个任务的产出
    要求"其实只要原地重派（v2 的 T2-轻）。**没有影响评估的 S1/S2 不得直接进
    T3**（v2 §4.7 明文）。

    本模块就是那道**必经之路**：把一条用户指令映射到影响范围 → 级别，
    并给出**依据**（v2 §4.7「评估结论 + 依据写 decision record」）。

**三条不变量**（沿用 v1 §4.1 与 P2 纪律）：

    1. **done 永不重跑** —— 评估只能作用于**未执行**任务；已被 done 覆盖的
       指令降级 T0（不动图），这是本模块的头号兜底（同 P2 `build_redraw_plan`
       的本地兜底：不能靠"调用方应该只传未执行任务"这种口头约定）。
    2. **评估不改图** —— 本模块是纯函数，零 IO、零 DB、零配置读取；
       实际重画由 `replan_redraw` 执行。职责分离让质量门/门禁可逐格单测。
    3. **结论必须可解释** —— 每个级别都带 `basis`（命中了哪条规则），
       否则前端修订卡只能显示"系统重画了"，用户无法判断该不该批准。

**竞态三况**（v2 §4.7 C1/C2/C3）由调用方按下面口径分派，本模块只提供判据：

| 况 | 场景 | 处理 | 本模块对应 |
|---|---|---|---|
| C1 | DAG 执行中发消息 | 迭代边界检查 pending ⇒ 进影响评估 | `should_evaluate()` |
| C2 | 汇总后发消息 | 新一轮编排（不动本模块） | 不适用 |
| C3 | resume 路径 | 不消费（图仍成立的断言） | `should_evaluate(is_resume=True)` → False |

**轻/重分级的真正判据**（v2 §4.2/v2 §4.7）——不是"改多少字"，而是
**是否推翻任务划分/输入前提**：

| 级别 | 触发 | 后续动作 |
|---|---|---|
| `T0` | 纯信息类 / 已被 done 覆盖 / 空指令 | 忽略，不动图 |
| `T2-light` | 只改**单个未执行任务**的产出要求 | 该任务原地重派（改 context），图拓扑不动 |
| `T2-heavy` | 改**若干任务**的产出要求，但**任务划分与依赖不变** | 多任务原地重派 |
| `T3` | **推翻任务划分或输入前提**（增/删/改依赖边） | LLM 整图重画（默认关，见 `replan_redraw`） |

⚠️ **默认关铁律**：本模块只做**评估**，不做重画。评估本身零副作用
（纯函数），但调用方必须受 `replanning.enabled` 总闸约束 —— 门禁 U4 会断言
"总闸关时挂点段整体是 no-op"。

**纯函数纪律**：零 IO、零 DB、零配置读取、无 logger、无 print。
"""
import re

# ── 结论常量（与 replan_signal / replan_redraw 同款命名纪律）────────────────
LEVEL_IGNORE = "T0"            # 忽略：不动图
LEVEL_LIGHT = "T2-light"      # 单/多任务原地重派：改 context，图拓扑不动
LEVEL_HEAVY = "T2-heavy"      # 换实现路径：新 key + lineage（由 replan_redraw 执行）
LEVEL_REDRAW = "T3"           # 整图重画：推翻任务划分/输入前提

#: 单次指令影响的任务数上限—— 超过即判"推翻任务划分"（T3）。
#: 为什么要有这个数：一句「整体重做一遍」在规则层与「把 8 个任务都改成表格」
#: 无法区分，但后者若按 T3 处理会烧掉一次 planner 调用；反之把真·整图重做
#: 判成 T2-light 只会重派受影响任务、原图结构留着 —— **后者的损失更小**
#: （任务照跑，只是没换结构）。故阈值取中间值：宁可少判 T3，不可多判。
MAX_TOUCH_TASKS = 3

#: 影响评估的最小指令长度（短于此视为噪声/表情，不评估）。
#: 与 `followup.MAX_ITEM_LEN`(2000) 上限配合：本模块只管下限。
MIN_DIRECTIVE_LEN = 4

# ── 规则词表 ───────────────────────────────────────────────────────────────
#: 纯信息类（⇒ T0）。设计 v2 §4.7「纯信息类（"辛苦了"）/ 已被 done 任务覆盖 → T0 忽略」。
#: ⚠️ 词表刻意**短**：宁可漏判（走重派，成本可控）也不误判成 T0
#:（用户的真实修改被静默吞掉 —— 那是最坏结果，用户看不到任何反馈）。
_INFO_ONLY = (
    "辛苦", "谢谢", "感谢", "好的", "收到", "嗯", "ok", "okay",
    "继续", "快点", "等待", "别急", "看着办", "随意",
)

#: 推翻任务划分/输入前提的信号（⇒ T3）。宁可**多**命中（升级到 T3 更保守吗？
#: 不 —— T3 更贵。真正的问题是"该 T3 判成 T2-light"，所以这张表要够敏感）。
#: 分三类：新增任务 / 删除任务 / 改依赖结构。
_OVERTURN_NEW = (
    "加一个", "加一个任务", "新增", "再补", "再加", "增加一个", "补充一个",
    "多一个", "插入", "追加一个", "额外做",
)
_OVERTURN_DROP = (
    "不要做", "不用做", "去掉", "删掉", "删除", "取消", "不用管", "跳过",
    "去掉第", "不用第",
)
_OVERTURN_STRUCT = (
    "重新规划", "重做计划", "换一套方案", "整体调整", "推翻", "换个思路",
    "改结构", "重新拆分", "调整划分", "改成两部分", "拆成",
)
#: 改依赖结构的显式信号（出现即无条件 T3，不受 MAX_TOUCH_TASKS 约束）。
_DEP_KEYWORDS = ("之后", "之前", "依赖", "先做", "再做", "放在...前", "放在...后")

#: 产出要求类（⇒ T2-light/heavy，按命中任务数分级）。
_OUTPUT_RE = re.compile(
    r"(改成|换成|改为|表述|格式|用表格|表格形式|加上.{0,6}列|不要.{0,8}分析|"
    r"精简|扩写|详细点|简短|侧重|强调|补充说明|字数|语气|风格|口径)")

# ── 内部小工具 ────────────────────────────────────────────────────────────
def _key(t):
    return str((t or {}).get("task_key") or (t or {}).get("key") or "")


def _terminal(status):
    """终态判定（**两种拼写都认** —— 同 P2 `replan_redraw._terminal`：
    `TaskQueue.cancel` 写 'cancelled' 双 L，而 `STATUSES` 用 'canceled' 单 L，
    这是仓内既存不一致；本模块漏认一种 ⇒ 被取消的任务会被当成"可重派"）。"""
    return str(status or "") in ("done", "failed", "cancelled", "canceled")


def _is_done(t):
    return str((t or {}).get("status") or "") == "done"


def pending_tasks(tasks):
    """未执行任务行（**排除终态**）—— 评估只能作用在这个集合上。

    为什么排除 failed：failed 任务是**重画候选**（P2 的输入），但**不是**
    "用户指令要改的对象" —— 用户看到一张卡上 t2 失败了，补一句「t2 那步改成
    手工做」是在改**重画方案**，那走 P2 的 reason 注入，不走 S1 影响评估。
    两个通道混在一起会导致同一任务既被 T1.5 跳过又被重派（双写）。
    """
    return [t for t in (tasks or [])
            if isinstance(t, dict) and _key(t) and not _terminal(t.get("status"))]


# ── 步骤 1：是否该评估（竞态 C1/C2/C3 的判据）────────────────────────────
def should_evaluate(directive, *, enabled=False, is_resume=False,
                    has_pending_tasks=True, consumed_by_agent_loop=False):
    """DAG 级指令是否该进影响评估。

    返回 `{"evaluate": bool, "reason": str}`。

    **逐条纪律**（每条都是一个真实的坑，不是防御性编程凑数）：

    · `is_resume=True` ⇒ **不评估**（v2 §4.7 C3：resume 本身就是"图仍成立"
      的断言；再评估等于把用户上次明确接受的图推翻）；
    · `consumed_by_agent_loop=True` ⇒ **不评估**（C1 分工：该指令已被
      单 Agent 的 follow-up 队列消费成新一轮 tool 轮次。若 DAG 级再评估一次，
      同一句话会让"改 context 重派"和"重启一轮"同时发生 ⇒ 任务跑两遍）；
    · 空/过短指令 ⇒ 不评估（T0 噪声）；
    · 纯信息类 ⇒ 不评估（评估它没有产出，纯烧一次调用）；
    · 总闸关 ⇒ 不评估（**零行为变更铁律**：默认关必须与 P2 逐字一致）；
    · `has_pending_tasks=False` ⇒ 不评估（没有可改的对象 —— 全图已执行完，
      此时任何指令都只能进新一轮编排，即 C2）。
    """
    d = str(directive or "").strip()
    if is_resume:
        return {"evaluate": False, "reason": "resume 路径不消费 DAG 级指令（C3）"}
    if consumed_by_agent_loop:
        return {"evaluate": False, "reason": "指令已被单 Agent follow-up 消费（C1 分工）"}
    if not enabled:
        return {"evaluate": False, "reason": "replanning.enabled 关 ⇒ 不评估"}
    if not d:
        return {"evaluate": False, "reason": "空指令 ⇒ 不评估"}
    # ⚠️ 纯信息类**先于**长度判据：短句里绝大多数是信息类（"辛苦了"/"好的"/
    #   "收到"）。若长度在前，一条 6 字的"辛苦了，辛苦了"会因过了长度闸
    #   而进评估（白烧一次评估）；更糟的是"辛苦"只有 2 字，会被长度闸放行。
    if _is_info_only(d):
        return {"evaluate": False, "reason": "纯信息类指令 ⇒ T0 忽略"}
    if len(d) < MIN_DIRECTIVE_LEN:
        return {"evaluate": False, "reason": "指令过短（<%d 字）⇒ 噪声" % MIN_DIRECTIVE_LEN}
    if not has_pending_tasks:
        return {"evaluate": False, "reason": "无未执行任务 ⇒ 只能走新一轮编排（C2）"}
    return {"evaluate": True, "reason": "DAG 级指令需影响评估（v2 §4.7）"}


def _is_info_only(d):
    """纯信息类判定：**要求整句由信息词构成**（而非"含有一个致谢词"）。

    为什么这么严：「辛苦了一下了第三部分改成表格」含"辛苦"但**必须**评估。
    口径：命中信息词后，把这些词从句中删掉，剩余有效字符 < MIN 视为纯信息。
    """
    s = d
    hit = False
    for w in _INFO_ONLY:
        if w in s:
            hit = True
            s = s.replace(w, "")
    if not hit:
        return False
    return len(re.sub(r"[\s\W_]+", "", s, flags=re.UNICODE)) < MIN_DIRECTIVE_LEN


# ── 步骤 2：影响评估主逻辑 ────────────────────────────────────────────────
def assess_impact(directive, tasks, *, done_items=None, goal="",
                  max_touch=MAX_TOUCH_TASKS):
    """评估一条 DAG 级用户指令的影响范围（纯函数）。

    返回：
        `{"level", "action", "target_keys", "basis", "reason",
          "covered_by_done": [key], "eval_required": True}`

    `level` ∈ `T0/T2-light/T2-heavy/T3`；`action` ∈ `none/retask/redraw`
    （`retask` = 原地重派，调用方改 context；`redraw` = 交 P2/T3 重画）。

    **为什么必须评估而不是直接判**（v2 §4.7 原文）：「没有影响评估的
    S1/S2 不得直接进 T3」。本函数是该约束的**执行点**。

    **规则版而非 LLM 版**（与 P2 同款取舍）：影响评估每次编排可能触发多次，
    且判定错误的代价不对称 —— 判轻了只是重派（任务照跑），
    判重了烧 planner 调用且可能改坏用户已接受的图。规则版的误判率更高，
    但**可控且可解释**。`llm_assess` 留作 P4 的可选增强：调用方若提供
    LLM 判定结果，**以 LLM 为准但仍须过本模块的不变量 1**（done 永不重跑）。

    ⚠️ `done_items` 参与判定的口径：若指令的所有目标命中都已被 done 任务
    覆盖 ⇒ T0（"已经被做出来了，不用改"）。这是 P2 不变量 1 在 S1 通道的
    对应实现 —— 否则一句迟到的指令会把已交付的结果推翻重来。
    """
    d = str(directive or "").strip()
    tasks = [t for t in (tasks or []) if isinstance(t, dict)]
    pend = pending_tasks(tasks)

    base = {"target_keys": [], "covered_by_done": [], "eval_required": True,
            "basis": [], "action": "none", "level": LEVEL_IGNORE}

    # ── 前置闸：没东西可改 / 空指令 ⇒ T0 ──
    if not d or not pend:
        base["reason"] = ("无未执行任务可改 ⇒ 归入新一轮编排（C2）" if pend == []
                          else "空指令 ⇒ 忽略")
        return base

    # ── 前置闸：纯信息类 ⇒ T0（**与 should_evaluate 重复是有意的**）──
    #   为什么不能只靠调用方先过 `should_evaluate`：本函数是**公开入口**，
    #   门禁/探针/REST 都能直接调；一个"直接调就静默重派所有任务"的版本
    #   会在某次重构里悄悄生效（调用方顺序一变就暴露）。防御性重复的代价是
    #   两行，收益是**本函数单独调用也语义正确**。
    if _is_info_only(d):
        base["reason"] = "纯信息类指令 ⇒ T0 忽略"
        # ★ T0 也要有 basis —— "忽略"是四种结论里最需要解释的一种：
        #   用户说了句话却什么都没发生，若卡片上只有 level=T0 没有理由，
        #   他会以为系统漏了指令（然后重发一遍）。留痕口径与其余三级一致。
        base["basis"] = ["命中信息类词表（无实质修改要求）"]
        return base

    # ── 前置闸：已被 done 覆盖 ⇒ T0（不变量 1 在 S1 通道的落点）──
    done_keys = set()
    if done_items is not None:
        for it in (done_items if isinstance(done_items, (list, tuple, set))
                   else (done_items or {}).keys()):
            if isinstance(it, dict):
                k = _key(it)
            else:
                k = str(it)
            if k:
                done_keys.add(k)
    else:
        done_keys = set(_key(t) for t in tasks if _is_done(t))

    # ── 规则 1：推翻任务划分/输入前提 ⇒ T3（最高优先，不受任务数阈值影响）──
    for tbl, label in ((_OVERTURN_NEW, "新增任务"), (_OVERTURN_DROP, "删除任务"),
                       (_OVERTURN_STRUCT, "改任务划分")):
        hit = [w for w in tbl if w in d]
        if hit:
            return _mk(base, LEVEL_REDRAW, "redraw", [], done_keys,
                       basis=["%s信号：%s" % (label, "/".join(hit[:3]))],
                       reason="指令%s ⇒ 推翻任务划分或输入前提 ⇒ T3 整图重画"
                              % label)
    if any(w in d for w in _DEP_KEYWORDS):
        return _mk(base, LEVEL_REDRAW, "redraw", [], done_keys,
                   basis=["改依赖结构信号"],
                   reason="指令含依赖顺序表述 ⇒ 动图拓扑 ⇒ T3 整图重画")

    # ── 规则 2：产出要求类 ⇒ T2-light / T2-heavy（按命中任务数分级）──
    out_hit = _OUTPUT_RE.findall(d)
    if out_hit:
        targets, covered = _match_targets(d, pend, done_keys, all_tasks=tasks)
        basis = ["产出要求信号：%s" % "/".join(sorted(set(out_hit))[:3])]
        if not targets:
            # ⚠️ 点名未命中**不等于**该忽略 —— 这是本模块最容易写错的一处。
            #   「第三部分改成表格」里用户说的是"第三部分"，任务标题却是
            #   「撰写第三部分分析」，前缀 12 字匹配不上；若据此判 T0，
            #   用户的真实修改会被**静默吞掉**，且卡片上不留任何痕迹 ——
            #   那是最坏结果（用户以为改了，实际没改）。
            #   口径：**有明确产出要求信号 + 有点名未命中 ⇒ 按影响全部未执行
            #   任务处理**（封顶 T2-light）。代价是可能重派了不该重派的任务
            #   （任务白跑一遍），比"吞掉用户指令"小一个量级。
            #   只有当命中**全部落在已完成任务上**时才是真·T0。
            if covered:
                return _mk(base, LEVEL_IGNORE, "none", [], done_keys,
                           basis=basis + ["指令点名的是已完成任务"],
                           covered=covered,
                           reason="指令点名的是已完成任务 ⇒ T0 忽略（done 永不重跑）")
            targets = [_key(t) for t in pend]
            if not targets:
                return _mk(base, LEVEL_IGNORE, "none", [], done_keys,
                           basis=basis + ["无未执行任务"],
                           reason="无未执行任务可改 ⇒ T0 忽略")
            basis = basis + ["点名未命中 ⇒ 按影响全部未执行任务处理"]
        if len(targets) > int(max_touch or MAX_TOUCH_TASKS):
            return _mk(base, LEVEL_REDRAW, "redraw", targets, done_keys,
                       basis=basis + ["命中 %d 个任务（> 阈值 %d）⇒ 视同推翻划分"
                                      % (len(targets), int(max_touch))],
                       reason="产出要求改动面 %d 个任务（> %d）⇒ T3 整图重画"
                              % (len(targets), int(max_touch)))
        lvl = LEVEL_LIGHT if len(targets) <= 1 else LEVEL_HEAVY
        return _mk(base, lvl, "retask", targets, done_keys,
                   basis=basis + ["点名任务：%s" % ",".join(targets[:5])],
                   reason="%s：改 %d 个未执行任务的产出要求（%s），图拓扑不动"
                          % (lvl, len(targets), ",".join(targets[:5])))

    # ── 规则 3：兜底 —— 有实质内容但未命中任何词表。
    #   口径：命中 done ⇒ T0；否则按"影响全部未执行任务"处理，但**封顶到
    #   T2-light**（宁可少判 T3：判轻的损失是"结构没换但任务照跑"，
    #   判重的损失是"烧一次 planner + 可能改坏用户已接受的图"）。
    if _covers_done(d, tasks, done_keys):
        return _mk(base, LEVEL_IGNORE, "none", [], done_keys,
                   basis=["未命中词表但命中已完成任务"],
                   reason="指令已被完成的任务覆盖 ⇒ T0 忽略（done 永不重跑）")
    if len(pend) <= int(max_touch or MAX_TOUCH_TASKS):
        return _mk(base, LEVEL_LIGHT, "retask", [_key(t) for t in pend], done_keys,
                   basis=["未命中词表 ⇒ 按影响全部未执行任务处理（封顶 T2-light）"],
                   reason="兜底：指令有实质内容但无明确指向，影响 %d 个未执行任务，"
                          "原地重派（不判 T3）" % len(pend))
    return _mk(base, LEVEL_LIGHT, "retask", [_key(t) for t in pend[:int(max_touch)]],
                done_keys,
                basis=["未命中词表 ⇒ 兜底封顶 T2-light"],
                reason="兜底：指令无明确指向，仅重派前 %d 个未执行任务（不判 T3）"
                       % int(max_touch))


def _mk(base, level, action, targets, done_keys, *, basis=None, reason="",
        covered=None):
    out = dict(base)
    out.update({"level": level, "action": action,
                "target_keys": list(targets or []),
                "basis": list(basis or base.get("basis") or []),
                "reason": reason or level})
    if covered:
        out["covered_by_done"] = list(covered)
    elif level == LEVEL_REDRAW and not action:
        out["covered_by_done"] = []
    return out


def _match_targets(directive, pend, done_keys, all_tasks=None):
    """按三种确定性线索做点名匹配（可精确判定，故不交给 LLM）。

    ① **字面 key / 标题前缀**：指令里直接写了 `t2` 或任务标题的片段；
    ② ★ **中文序数指代**：「第三部分」/「第 3 节」/「第三段」⇒ 按**整张图**
       的执行顺序定位（见 `_ordinal_targets` 的口径说明）；
    ③ 返回被命中的**已完成** key 列表（⇒ T0 的唯一正当来源）。

    ② 为什么必须做（本轮实施期真 bug）：中文用户几乎不会说 `t2`，他们说
    「第三部分改成表格」。没有 ② 时这条指令点名失败，会退化到"无法判断指向
    ⇒ 按全部未执行任务处理"，在 5 个任务的图上直接判成 T2-heavy（重派 5 个
    任务）或 T3（烧 planner）—— **而用户只想改 1 个任务**。这是"无法判断时
    往贵了猜"的典型反面案例。

    返回 `(命中的未执行 key 列表, 被命中的已完成 key 列表)`。
    """
    low = str(directive or "").lower()
    hit_p, hit_d = [], []
    for t in pend:
        k = _key(t)
        title = str(t.get("title") or "")
        # 中文标题常含"第N部分"这类序数，用前 12 字做前缀匹配已足够
        probe = (title[:12] or k).lower()
        if (k and k.lower() in low) or (probe and probe in low):
            hit_p.append(k)
    for t in (done_keys or []):
        tk = str(t)
        if tk and tk.lower() in low:
            hit_d.append(tk)

    # ② 序数指代（仅在字面匹配无果时启用 —— 字面证据永远优先）
    if not hit_p and not hit_d:
        hit_p.extend(_ordinal_targets(directive, pend, all_tasks=all_tasks))
    return hit_p, hit_d


#: 中文序数词 → 序号（覆盖 1~20，够用；再大交给"无法判断"路径）
_CN_NUM = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6,
           "七": 7, "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12,
           "十三": 13, "十四": 14, "十五": 15, "十六": 16, "十七": 17,
           "十八": 18, "十九": 19, "二十": 20}
_ORD_RE = re.compile(r"第\s*([0-9]{1,2}|[一二两三四五六七八九十]{1,3})\s*"
                     r"(部分|节|段|章|块|步|章节|步骤)")


def _ordinal_targets(directive, pend, *, all_tasks=None):
    """「第三部分」⇒ 按执行顺序的第 N 个任务（须是未执行任务，否则不返回）。

    ★★ 口径：**按整张图的顺序数，不按"未执行任务"数**（2026-10-21 实施期
    修正的真 bug）。用户看着卡片上的三行任务说"第三部分"，指的是第 3 行
    （t3），即便 t1 已完成 —— 若按未执行集合数，前两个已完成时"第三部分"
    会指向图外的任务而落空，于是这条最常见的指令永远点名失败。

    返回**唯一命中**的未执行 key；命中 done 或越界一律返回空（让调用方走
    "无法判断"路径，而不是猜一个 —— 猜错会改错用户的任务且不留痕迹）。
    """
    hits = []
    for m in _ORD_RE.finditer(str(directive or "")):
        raw = m.group(1)
        n = int(raw) if raw.isdigit() else _CN_NUM.get(raw, 0)
        if n > 0:
            hits.append(n)
    if not hits:
        return []
    pool = list(all_tasks or pend)
    if not pool:
        pool = list(pend)
    # 按执行顺序排序（seq 优先，缺失/同值时用出现次序稳定排序）
    order = sorted(range(len(pool)),
                   key=lambda i: (int((pool[i] or {}).get("seq") or i + 1), i))
    pend_keys = set(_key(t) for t in pend)
    out = []
    for n in hits:
        if n > len(order):
            continue                      # 序数超出图范围 ⇒ 无法判断
        k = _key(pool[order[n - 1]])
        if k and k in pend_keys and k not in out:
            out.append(k)
    return out


def _covers_done(directive, tasks, done_keys):
    """指令是否命中任何已完成任务的 key/标题（用于兜底 T0 判定）。"""
    return bool(_match_targets(directive, [], set(done_keys))[1])


# ── 步骤 3：决策记录（与 replan_signal 同款形状，便于复用 span 装配）───────
def directive_meta(assessment, *, latency_ms=0, tokens=0, round_no=1):
    """把评估结论整成 span meta（`kind=replan` 的 `name=directive-decide`）。

    为什么单独一个函数而不是让调用方自己拼：P1/P2 的 `decision_meta` 产出的
    键集是 `action/level/reason/signals/round/tokens/latency_ms`，前端 trace
    面板按这套键渲染（`03-chat.js` 的 `kindMeta`）。评估记录若用别的键名，
    面板上就会出现一个"什么都没有"的 replan 节点 —— 反而更糟。
    """
    a = assessment or {}
    return {
        "action": a.get("action") or "none",
        "level": a.get("level") or LEVEL_IGNORE,
        "reason": a.get("reason") or "",
        # signals 复用为「评估依据」—— 面板渲染成可折叠列表（G10 可解释性）
        "signals": list(a.get("basis") or []),
        "targets": list(a.get("target_keys") or []),
        "round": int(round_no or 1),
        "tokens": int(tokens or 0),
        "latency_ms": int(latency_ms or 0),
    }


# ── 步骤 4：原地重派载荷（T2-light/heavy 的 context 拼装）─────────────────
def retask_context(base_context, directive, *, level=LEVEL_LIGHT):
    """把用户指令拼进目标任务的 context（纯函数，供 UPDATE 用）。

    为什么**追加而不是覆盖**（v2 §4.10 增量约束的 S1 版本）：用户看到的是
    卡片上任务 title + 已产出内容，覆盖会让人工写过的输入凭空消失
    （与 #50/#51 附件重建"产物双写/丢失"同族教训）。

    ⚠️ 截断到 1000 字：与 P2 `_inject` 同一上限（`agent_tasks.context` 列宽），
    两处口径必须一致 —— 否则"重画注入后 1200 字、用户指令注入后 800 字"
    这种差异会成为难以复现的行为漂移源。
    """
    ctx = str(base_context or "")
    d = str(directive or "").strip()
    head = ("【用户补充指令 r1·%s】%s" % (level, d)) if d else ""
    merged = (ctx + "\n" + head) if ctx else head
    return merged[:1000]


def llm_redraw_prompt(goal, tasks, *, directive="", done_summaries=None,
                      max_tasks=MAX_TOUCH_TASKS):
    """T3 LLM 整图重画的提示词骨架（纯函数，返回字符串）。

    **为什么提示词也进纯函数模块**：P3 范围锁定 replace 动作
    （skip/insert 留 P4），而"replace 只允许产失效子图的替代任务"这条约束
    必须**同时**出现在提示词和质量门里 —— 两处分头维护必然漂移
    （P2 已经吃过一次亏：`build_redraw_plan` 的新 key 与 Q2 的 existing 集
    口径若不一致，"撞车"就会以别的形式漏出来）。

    返回 `(prompt, schema_hint)`。`schema_hint` 是要求 LLM 输出的 JSON 形状，
    调用方用 `json.loads` 解析后仍须过 Q1~Q5（**LLM 输出不豁免质量门**）。
    """
    lines = []
    for t in (tasks or []):
        if not isinstance(t, dict):
            continue
        st = str(t.get("status") or "")
        lines.append("- %s [%s] %s" % (_key(t), st or "planned",
                                     str(t.get("title") or "")[:60]))
    done_txt = ""
    if done_summaries:
        try:
            done_txt = "\n已完成任务的结论摘要（**不得重跑，只能引用**）：\n" + \
                       "\n".join("  %s" % str(x)[:120] for x in list(done_summaries)[:10])
        except Exception:
            done_txt = ""
    prompt = (
        "你是执行中重规划器。用户中途下达了新的指令，需要据此调整执行计划。\n"
        "\n# 原始目标\n%s\n"
        "\n# 当前任务图\n%s\n"
        "%s"
        "\n# 用户中途指令\n%s\n"
        "\n# 你的任务\n"
        "给出调整后的**完整任务图**（JSON）。约束：\n"
        "1. 只允许 replace：对失效/被点名任务给出替代实现，"
        "**不得凭空新增与指令无关的任务**；\n"
        "2. 已完成（done）任务**不得重跑**，其结论只能作为新任务的输入引用；\n"
        "3. 新任务的 key 必须带 `_rN` 后缀且与既有 key 不重复；\n"
        "4. deps 只能引用本图内存在的 key（含 done 的）；\n"
        "5. 总任务数不得超过原图任务数的 1.5 倍。\n"
        "\n# 输出格式（严格 JSON，不要 markdown 代码块）\n"
        '{"tasks": [{"key":"", "title":"", "agent":"", "deps":[], "context":"", '
        '"expected_output":""}]}'
        % (str(goal or "")[:500],
           "\n".join(lines[:40]) or "（空）",
           done_txt,
           str(directive or "")[:500])
    )
    schema_hint = {"tasks": "list[dict]"}
    return prompt, schema_hint


def parse_llm_plan(raw, failed_keys, *, round_no=1):
    """解析 LLM 整图重画输出 → `replan_redraw.plan_redraw` 可吃的 `new_tasks`。

    严格三条（**解析层就拒掉，不给质量门背锅**）：
      1. 非 JSON / 非 dict / `tasks` 非 list ⇒ 空；
      2. 每条必须含非空 `key` 与 `title`；
      3. `deps` 必须是 list（字符串会让下游 `[remap.get(d,d) for d in _deps]`
         按字符迭代，是一类极难查的 bug）。

    返回 `(new_tasks, parse_error)`。`parse_error` 非空 ⇒ 调用方判 T4
    （LLM 输出不可用时停手，绝不"尽力而为落库" —— 同 P2 不变量 3）。

    ⚠️ 这里**不**校验 key 是否撞车/是否指向 done —— 那是 Q2 的职责，
    职责分层让"LLM 越界"这件事在门禁里可逐格验证（P2 Q2 已覆盖）。
    """
    import json as _j
    err = ""
    try:
        obj = _j.loads(str(raw or "").strip())
    except Exception as e:
        return [], "LLM 输出非 JSON：%s" % e
    if not isinstance(obj, dict):
        return [], "LLM 输出非对象"
    tl = obj.get("tasks")
    if not isinstance(tl, list):
        return [], "LLM 输出缺 tasks 数组"
    # `lineage.from` 必须指向**被替代的旧 key**，而不是新 key 自己。
    #   ⚠️ 这是本函数最容易错的一处：`from` 是 Q3 与 replace 范围锁定的唯一
    #   判据来源（"新任务必须替代某个失效任务"）。若填成新 key 自己，
    #   `from ∈ failed` 恒不成立 ⇒ `plan_llm_redraw` 的本地兜底会把**全部**
    #   LLM 任务判为"超出 replace 范围" ⇒ T4 ⇒ 表现为"llm_redraw 开了但
    #   永远失败"，且日志里看不出是 lineage 填错。
    #
    #   ★ 配对**只认命名证据**（`<旧key>_rN` 前缀），**不做顺序兜底**：
    #     顺序兜底（"第 i 个新任务配第 i 个失效任务"）看起来无害，实际是
    #     **把 LLM 凭空新增的任务也认成替代品** —— 「加一个性能评审任务」
    #     产出的 `perf_review` 会因排在第一位而被认成 `t2` 的替代，
    #     于是 replace 范围锁定（P3 拍板⑤的核心约束）形同虚设：
    #     insert 动作从后门溜了进来，且 Q3/Q4 全绿、看不出异常。
    #     判据：**无法证明是替代 ⇒ 就不是替代**（宁可 T4 让用户重试）。
    failed_ord = [str(x) for x in (failed_keys or []) if str(x)]
    out = []
    for it in tl:
        if not isinstance(it, dict):
            err = err or "tasks 含非对象元素"
            continue
        k = str(it.get("key") or "").strip()
        ti = str(it.get("title") or "").strip()
        if not k or not ti:
            err = err or "tasks 元素缺 key/title"
            continue
        dp = it.get("deps") or []
        if not isinstance(dp, (list, tuple)):
            err = err or "deps 非数组（key=%s）" % k
            continue
        # 前缀反推：t2_r1 → t2（剥 _rN 后缀后须命中失效集）
        frm = ""
        m = re.match(r"^(.*)_r\d+$", k)
        if m and str(m.group(1)) in failed_ord:
            frm = str(m.group(1))
        out.append({
            "key": k,
            "title": ti[:120],
            "agent": str(it.get("agent") or "")[:60],
            "task_type": str(it.get("task_type") or "agent"),
            "deps": [str(x) for x in dp],
            "seq": int(it.get("seq") or 0),
            "context": str(it.get("context") or "")[:1000],
            "expected_output": str(it.get("expected_output") or "")[:500],
            # ⚠️ `from` 为空时**不填 lineage** —— 让下游的
            #   `plan_llm_redraw` 本地兜底（+ Q3）把它判成越界并 T4。
            #   填一个假的 `from` 等于给质量门开后门。
            "config": ({"lineage": {"from": frm, "round": int(round_no or 1),
                                    "reason": "llm_redraw", "level": LEVEL_REDRAW}}
                       if frm else {}),
        })
    if not out:
        return [], err or "LLM 输出无有效任务"
    return out, ""