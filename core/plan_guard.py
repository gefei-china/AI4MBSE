"""计划语义校验（Plan Guard）：在计划**落库前**校验阶段序 / 完备性 / 覆盖性 / 路由声明。

## 为什么需要（与既有校验的分工）

计划进入执行前已有两道校验，本模块**不与它们重叠**：

| 层 | 实现 | 覆盖 | 缺失后果 |
|---|---|---|---|
| **结构层** | `TaskQueue.validate_plan` | 空 key / 重复 key / 自环 / 依赖环 | `ready_tasks` 恒空 → 整批**静默阻塞** |
| **路由层** | `TaskQueue.create_plan`(strict) | agent 名解析不到在役 Agent | 静默回落「通用问答Agent」零工具，**任务照标 done** |
| **语义层** | **本模块** | 阶段序 / 必需阶段 / 用户点名阶段 / 外部依赖 / 声明与实际不符 | **能跑完，但跑的不是用户要的顺序** |

三层正交，都要有。结构层与路由层回答"能不能跑"，本层回答"跑得对不对"。

## 为什么语义校验不能只写在提示词里

`agents.id=160` 的 `system_prompt` 第 2 条写着「阶段顺序固定：…**不得跳步、不得倒序**」。
但全仓 `grep 阶段序` 命中的 11 处**全是往提示词拼字符串**，没有一处校验 ——
即模型真倒序了，代码不会拦，只按它自己给的 `deps` 拓扑执行。

这与「名册漂移导致整条流水线派给零工具 Agent、但任务照标 done」是同一类问题：
**约束只存在于提示词里，就等于没有约束**。

## 与 Capella / SysML v2 的对应关系（设计依据）

见 `docs/编排流水线与多方案权衡-标杆实现对标-20261103.md`：

| 本模块规则 | Capella 规则族 | 检查什么 |
|---|---|---|
| `stage.order_reversed` | `TC_*`（Transition Consistency） | 层间/阶段间迁移是否逆序 |
| `stage.required_missing` | `DCOM_*`（Completeness） | 该有的阶段有没有 |
| `stage.mentioned_missing` | `DCOV_*`（Coverage） | 用户点名的阶段是否被覆盖 |
| `route.unresolved` | 无对应（我们的路由特有） | 声明的执行主体是否在役 |

## 三档模式（默认 `warn`，**不改变现有行为**）

- `off`：不校验（逐字节回旧行为）
- `warn`（默认）：校验 → 违规进 `warnings` + `data.plan_guard` + 日志，**不阻断执行**
- `block`：存在 `error` 级违规 ⇒ 返回 `blocked=True`，调用方**不落库、不执行**

默认给 `warn` 是刻意的：先让违规**可见**（当前状态是"完全不可见"），
跑一段看真实分布之后再决定是否收紧到 `block`。

## 硬约束

1. **纯函数、零 IO**：DB 读取在 `load_pipeline_config`，本模块其余部分不碰连接、不写库。
2. **不改图**：只报问题，**不静默删边/改序**（静默删边等于替模型猜意图，且不可追溯）。
3. **不臆造**：`resolve` 不传就不做路由校验；`stage_order` 为空就不做阶段序校验
   （**没配置 ⇒ 不报错**，而不是猜一个顺序去判别人）。
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Dict, Iterable, List, Optional

#: 校验器版本（随规则语义变化递增；写入 `data.plan_guard.version` 便于审计回查）
GUARD_VERSION = "1.0"

RULE_KEY_STAGE_ORDER = "pipeline_stage_order"
RULE_KEY_REQUIRED_STAGES = "pipeline_required_stages"
RULE_KEY_MODE = "pipeline_guard_mode"

MODE_OFF = "off"
MODE_WARN = "warn"
MODE_BLOCK = "block"
VALID_MODES = (MODE_OFF, MODE_WARN, MODE_BLOCK)
DEFAULT_MODE = MODE_WARN

SEVERITY_ERROR = "error"
SEVERITY_WARN = "warn"

#: 阶段序的**兜底默认值** —— 与 `agents.stage_order` 的 8 个阶段同源同序
#: （该列 2026-10 由 `tools/migrate_v5_pipeline_nodes.py` 建好并已填充）。
#: ⚠️ 真实生效顺序的**首选来源是 `agents.stage_order`**（见 `load_pipeline_config`），
#:   本常量只在「DB 不可读」时生效 —— 改阶段序请改 Agent 表，不要改这里。
DEFAULT_STAGE_ORDER: List[str] = [
    "methodology_resolver",
    "requirement_structuring",
    "architecture_skeleton",
    "view_expansion",
    "model_validation_repair",
    "trace_verification",
    "change_safety_gate",
    "model_release",
]

#: 兜底默认：建模流水线的**必需阶段**（可跳过视图/发布，但需求与架构不可省）。
#: 刻意保守 —— 只列"缺了就一定不是完整建模"的两个。
DEFAULT_REQUIRED_STAGES: List[str] = [
    "requirement_structuring",
    "architecture_skeleton",
]


# ── 小工具 ────────────────────────────────────────────────────────────
def _norm(name: Any) -> str:
    """规范化名字用于比较（大小写/首尾空白不敏感，与 `resolve_agent_name` 同口径）。"""
    return str(name or "").strip()


def _stage_of(name: Any, stage_index: Dict[str, int], declared: Dict[str, str]) -> Optional[str]:
    """把任务声明的 agent 映射到阶段名；不是阶段则返回 None。

    `declared`：规范化 stage（lower）→ 原始 stage 写法，用于回显。
    """
    n = _norm(name).lower()
    if not n:
        return None
    return declared.get(n)


def mentioned_from_stage_hint(stage_hint: Any) -> List[str]:
    """从 `stage_hint` 解析"用户点名的阶段"。

    `stage_hint` 的格式是 `{意图}（{该阶段原句}）`（见
    `OrchestrMixin._build_stage_hint`）；也可能是裸意图名。
    取 `（` 之前的部分，去掉空白。
    """
    out: List[str] = []
    for raw in (stage_hint or []):
        s = str(raw or "").strip()
        if not s:
            continue
        token = re.split(r"[（(]", s, maxsplit=1)[0].strip()
        if token and token not in out:
            out.append(token)
    return out


def _deps_of(task: Dict[str, Any]) -> List[str]:
    d = task.get("deps")
    if isinstance(d, str):
        try:
            d = json.loads(d)
        except Exception:
            d = [x.strip() for x in d.split(",") if x.strip()]
    if not isinstance(d, (list, tuple)):
        return []
    return [str(x) for x in d if str(x).strip()]


def _is_required(task: Dict[str, Any]) -> bool:
    cfg = task.get("config")
    if isinstance(cfg, str):
        try:
            cfg = json.loads(cfg)
        except Exception:
            cfg = {}
    if not isinstance(cfg, dict):
        return False
    return bool(cfg.get("required"))


def _task_type(task: Dict[str, Any]) -> str:
    return str(task.get("task_type") or "agent").strip().lower() or "agent"


# ── 主校验入口 ────────────────────────────────────────────────────────
def validate_pipeline_plan(
    plan: Iterable[Any],
    *,
    stage_order: Optional[Iterable[str]] = None,
    required_stages: Optional[Iterable[str]] = None,
    mentioned_stages: Optional[Iterable[str]] = None,
    resolve: Optional[Callable[[str], str]] = None,
    mode: str = DEFAULT_MODE,
) -> Dict[str, Any]:
    """校验计划是否符合流水线语义。返回结构化结果（**不抛异常**）。

    参数
    ----
    plan : 计划任务列表。每项 dict，字段同 `TaskQueue.create_plan`
        （`key` / `agent` / `task_type` / `deps` / `config.required`）。
    stage_order : 阶段的合法先后顺序（索引越小越早）。空/None ⇒ **跳过阶段序与覆盖性校验**。
    required_stages : 该流水线的必需阶段。空/None ⇒ 跳过完备性校验。
    mentioned_stages : 用户在本轮明确点名的阶段。空/None ⇒ 跳过覆盖性校验。
    resolve : `name -> 权威 Agent 名`（解析不到返回 `""`）。
        不传 ⇒ 跳过路由校验（**不猜**）。
    mode : `off` / `warn` / `block`。非法值按 `warn` 处理。

    返回
    ----
    dict:
        {
          "version": "1.0",
          "mode": "warn",
          "ok": bool,          # 无 error 级违规
          "blocked": bool,     # mode=block 且 not ok
          "violations": [ {code, severity, message, fix, tasks, stages} ],
          "stats": {tasks, agent_tasks, stage_tasks, max_stage_index},
          "stage_sequence": [阶段名...],   # 按计划内出现顺序（去重）
          "notes": [...],      # 跳过原因等（**必须留痕**，否则"没报错"会被误读为"没问题"）
        }
    """
    md = str(mode or DEFAULT_MODE).strip().lower()
    notes: List[str] = []
    if md not in VALID_MODES:
        notes.append("非法 mode=%r，按 %s 处理" % (mode, DEFAULT_MODE))
        md = DEFAULT_MODE

    rows = [t for t in (plan or []) if isinstance(t, dict)]
    violations: List[Dict[str, Any]] = []

    def _add(code: str, severity: str, message: str, fix: str = "",
             tasks: Optional[List[str]] = None, stages: Optional[List[str]] = None) -> None:
        violations.append({
            "code": code,
            "severity": severity,
            "message": message,
            "fix": fix,
            "tasks": list(tasks or []),
            "stages": list(stages or []),
        })

    # 空计划：结构层也会拦（`empty_plan`），这里提前给一句人能读的话
    if not rows:
        _add("plan.empty", SEVERITY_ERROR,
             "计划为空：没有任何子任务",
             "重新生成计划；若反复为空，检查规划器提示词与模型输出是否被截断")
        return _result(md, violations, rows, [], notes, stage_tasks=0)

    order_list = [str(x).strip() for x in (stage_order or []) if str(x).strip()]
    stage_index: Dict[str, int] = {s.lower(): i for i, s in enumerate(order_list)}
    declared: Dict[str, str] = {s.lower(): s for s in order_list}

    req_list = [str(x).strip() for x in (required_stages or []) if str(x).strip()]
    men_list = [str(x).strip() for x in (mentioned_stages or []) if str(x).strip()]

    if md == MODE_OFF:
        notes.append("mode=off：跳过全部语义校验（逐字节回旧行为）")
        return _result(md, [], rows, [], notes, stage_tasks=0)
    if not order_list:
        notes.append("未配置 pipeline_stage_order：跳过阶段序与覆盖性校验（不猜顺序）")
    if not req_list:
        notes.append("未配置 pipeline_required_stages：跳过完备性校验")
    if not men_list:
        notes.append("本轮无用户点名阶段：跳过覆盖性校验")

    keys = [str(t.get("key") or "").strip() for t in rows]
    key_set = {k for k in keys if k}
    task_by_key = {str(t.get("key") or "").strip(): t for t in rows if str(t.get("key") or "").strip()}

    # ── 1) 路由声明校验 ──
    agent_tasks = 0
    for t in rows:
        k = str(t.get("key") or "").strip()
        tt = _task_type(t)
        raw_agent = _norm(t.get("agent"))
        if tt == "agent":
            agent_tasks += 1
            if not raw_agent:
                # 合法用法：`forced_intent=None` ⇒ 走意图识别（`create_plan` 注释已明确）
                _add("route.missing", SEVERITY_WARN,
                     "任务 %s 未声明执行主体（task_type=agent 但 agent 为空），将按意图识别分派" % k,
                     "如需确定性，显式指定 agent；否则忽略此告警",
                     tasks=[k])
            elif resolve is not None:
                try:
                    hit = resolve(raw_agent)
                except Exception as e:
                    hit = ""
                    notes.append("resolve(%r) 抛异常，按未解析处理：%s" % (raw_agent, str(e)[:80]))
                if not hit:
                    _add("route.unresolved", SEVERITY_ERROR,
                         "任务 %s 声明的 Agent「%s」解析不到在役 Agent" % (k, raw_agent),
                         "改用当前在役的 agents.name；照常执行会静默降级为零工具 Agent",
                         tasks=[k])
        else:
            # ⚠️ 只对**真正忽略 agent 的类型**告警：`llm` 走裸 LLM 调用、`tool` 走工具直调，
            #    两者都不读 `agent` ⇒ 声明了也是假的。
            #
            # ★ 2026-11-04（react 方案 A）：`react` **已从本条豁免**。
            #    改造前它确实不读 agent（docstring 却声称由 agent 驱动），
            #    现改为委托主链路执行，`agent` 真正生效（见 `planner.py::_run_task` 的 react 分支）。
            #    ⇒ 若继续对它告警，会把一个已经修好的语义标成"声明失效"，属于**假告警**。
            if raw_agent and tt in ("llm", "tool"):
                _add("route.ignored_by_type", SEVERITY_WARN,
                     "任务 %s 的 task_type=%s，但声明了 agent「%s」—— 该类型不读取 agent，声明不会生效"
                     % (k, tt, raw_agent),
                     "要么改 task_type=agent/react，要么删掉 agent 声明（否则审计会误以为派给了它）",
                     tasks=[k])

    # ── 2) 外部依赖（会被 `create_plan` 静默过滤 ⇒ 任务提前就绪）──
    for t in rows:
        k = str(t.get("key") or "").strip()
        for d in _deps_of(t):
            if d not in key_set:
                _add("deps.external", SEVERITY_WARN,
                     "任务 %s 依赖计划外的 %s —— 落库时会被静默过滤，该任务将提前就绪" % (k, d),
                     "补上被依赖的任务，或删掉这条 deps",
                     tasks=[k])

    # ── 3) 阶段序（对应 Capella TC_*）──
    #   语义：`X deps [Y]` ⇒ Y 先于 X。若 stage(X) 在阶段序里**早于** stage(Y)，
    #   等价于"后面的阶段挡着前面的阶段"，是逆序。
    stage_tasks = 0
    seq: List[str] = []
    for t in rows:
        st = _stage_of(t.get("agent"), stage_index, declared) if order_list else None
        if st:
            stage_tasks += 1
            if st not in seq:
                seq.append(st)
    if order_list:
        for t in rows:
            x = str(t.get("key") or "").strip()
            sx = _stage_of(t.get("agent"), stage_index, declared)
            if not sx:
                continue
            for d in _deps_of(t):
                dy = task_by_key.get(d)
                if not dy:
                    continue
                sy = _stage_of(dy.get("agent"), stage_index, declared)
                if not sy:
                    continue
                if stage_index.get(sx.lower(), 0) < stage_index.get(sy.lower(), 0):
                    _add("stage.order_reversed", SEVERITY_ERROR,
                         "阶段逆序：%s（%s）依赖了 %s（%s），但 %s 应早于 %s"
                         % (x, sx, d, sy, sx, sy),
                         "把这条 deps 反过来，或调整任务归属阶段",
                         tasks=[x, d], stages=[sx, sy])

    # ── 4) 完备性（对应 Capella DCOM_*）──
    if req_list:
        present_lower = {s.lower() for s in seq}
        missing = [s for s in req_list if s.lower() not in present_lower]
        if missing:
            _add("stage.required_missing", SEVERITY_WARN,
                 "计划缺少必需阶段：%s" % "、".join(missing),
                 "补上该阶段的任务；若本目标确实不需要，请调低 pipeline_required_stages",
                 stages=missing)

    # ── 5) 覆盖性（对应 Capella DCOV_*）──
    if men_list and order_list:
        present_lower = {s.lower() for s in seq}
        uncovered = [s for s in men_list
                     if s.lower() in stage_index and s.lower() not in present_lower]
        if uncovered:
            _add("stage.mentioned_missing", SEVERITY_WARN,
                 "用户点名的阶段未被覆盖：%s" % "、".join(uncovered),
                 "补上对应任务，或在答复中说明为何跳过",
                 stages=uncovered)

    # ── 6) 必需标注（early_stop 的前提）──
    if rows and not any(_is_required(t) for t in rows):
        _add("plan.no_required", SEVERITY_WARN,
             "计划中没有任何 config.required=true 的任务",
             "标注哪些任务是达成目标所必需的；否则提前收敛（early_stop）恒不动作",
             tasks=[str(t.get("key") or "") for t in rows][:10])

    return _result(md, violations, rows, seq, notes, stage_tasks=stage_tasks)


def _result(mode: str, violations: List[Dict[str, Any]], rows: List[Dict[str, Any]],
            seq: List[str], notes: List[str], stage_tasks: Optional[int] = None) -> Dict[str, Any]:
    """组装返回值。`stage_tasks` 由调用方传入 —— **不能**用 `seq` 重算：
    `seq` 是"计划内实际出现的顺序"，不是阶段定义顺序，拿它当索引表会得出错误计数。"""
    errors = [v for v in violations if v.get("severity") == SEVERITY_ERROR]
    ok = not errors
    if stage_tasks is None:
        stage_tasks = len(seq)
    return {
        "version": GUARD_VERSION,
        "mode": mode,
        "ok": ok,
        "blocked": bool(mode == MODE_BLOCK and not ok),
        "violations": violations,
        "stats": {
            "tasks": len(rows),
            "agent_tasks": sum(1 for t in rows if _task_type(t) == "agent"),
            "stage_tasks": stage_tasks,
            "errors": len(errors),
            "warnings": len(violations) - len(errors),
        },
        "stage_sequence": seq,
        "notes": notes,
    }


def render_violations(violations: Iterable[Dict[str, Any]], max_items: int = 4) -> str:
    """把违规压成一行**人能读**的摘要（用于 warnings / 日志 / 答复前缀）。

    超长时截断并给出总数 —— 只截断展示，不截断数据（完整清单在 `data.plan_guard`）。
    """
    vs = [v for v in (violations or []) if isinstance(v, dict)]
    if not vs:
        return ""
    parts = []
    for v in vs[:max_items]:
        tag = "✗" if v.get("severity") == SEVERITY_ERROR else "!"
        parts.append("%s %s" % (tag, v.get("message") or v.get("code")))
    if len(vs) > max_items:
        parts.append("…等共 %d 项（详见计划校验明细）" % len(vs))
    return "；".join(parts)


# ── DB 配置读取（唯一 IO 入口；纯函数区不受影响）─────────────────────
def load_pipeline_config(conn=None) -> Dict[str, Any]:
    """从 `generate_rules` 读流水线校验配置。

    读不到 / 解析失败 ⇒ 回退默认（**校验器仍可用**，不因配置缺失而失效）。
    返回：`{stage_order, required_stages, mode, source}`，`source` 标明实际来源便于审计。
    """
    cfg: Dict[str, Any] = {
        "stage_order": list(DEFAULT_STAGE_ORDER),
        "required_stages": list(DEFAULT_REQUIRED_STAGES),
        "mode": DEFAULT_MODE,
        "source": "default",
    }
    _owned = False
    try:
        if conn is None:
            from database import get_db
            conn = get_db()
            _owned = True
        rows = conn.execute(
            "SELECT rule_key, rule_value FROM generate_rules WHERE rule_key IN (?,?,?)",
            (RULE_KEY_STAGE_ORDER, RULE_KEY_REQUIRED_STAGES, RULE_KEY_MODE),
        ).fetchall()
        got = {r[0]: r[1] for r in rows}
    except Exception:
        return cfg
    finally:
        if _owned:
            try:
                conn.close()
            except Exception:
                pass

    src: List[str] = []

    def _json_list(key: str) -> Optional[List[str]]:
        raw = got.get(key)
        if raw in (None, ""):
            return None
        try:
            val = json.loads(raw)
        except Exception:
            return None
        if isinstance(val, (list, tuple)):
            return [str(x).strip() for x in val if str(x).strip()]
        return None

    so = _json_list(RULE_KEY_STAGE_ORDER)
    if so:
        cfg["stage_order"] = so
        src.append(RULE_KEY_STAGE_ORDER)
    else:
        # ★ 首选来源：`agents.stage_order`（每行一个阶段，索引即顺序）。
        #   该列 2026-10 已建好并填充 8 个阶段，是"阶段有哪些、先后如何"的**权威出处**；
        #   `generate_rules.pipeline_stage_order` 只在需要**临时覆盖**时才用它。
        #
        # ⚠️ 只有目录来源真的是 DB 时才记 `agents.stage_order` ——
        #   目录自身也可能回落兜底常量，那种情况下 `source` 必须仍是 `default`，
        #   否则审计时会误以为"读的是表"，把兜底默认当成实际配置（假可信比没来源更糟）。
        try:
            from core.agent_catalog import snapshot as _cat_snap
            _snap = _cat_snap(conn)
            _tbl = list(_snap.get("stage_intents") or [])
            if _tbl and _snap.get("source") == "db":
                cfg["stage_order"] = _tbl
                src.append("agents.stage_order")
        except Exception:
            pass
    rs = _json_list(RULE_KEY_REQUIRED_STAGES)
    if rs is not None:
        cfg["required_stages"] = rs
        src.append(RULE_KEY_REQUIRED_STAGES)
    md = str(got.get(RULE_KEY_MODE) or "").strip().lower()
    if md in VALID_MODES:
        cfg["mode"] = md
        src.append(RULE_KEY_MODE)
    cfg["source"] = "db:" + ",".join(src) if src else "default"
    return cfg
