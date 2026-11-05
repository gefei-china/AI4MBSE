"""自检：计划语义校验（Plan Guard）。

口径：裸跑；不启服务、不调 LLM、不写生产库（配置加载用内存库夹具）。

断言分三类：
1. **正向**：每条规则在"该报"的输入上必须报，且 severity/code 正确；
2. **反向**：在"不该报"的输入上必须**沉默**（防误报 —— 误报会让人关掉整个校验）；
3. **变异自证**：把实现改坏，判据必须翻红（防"假绿"，本仓已因此吃过亏）。

⚠️ 变异自证的铁律（2026-10-28 教训）：判据要抽成 lambda，正确实现求值为 True、
   变异实现必须为 False。写成"变异后仍应通过"等于自证形同虚设。
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from core.plan_guard import (  # noqa: E402
    DEFAULT_MODE,
    DEFAULT_REQUIRED_STAGES,
    DEFAULT_STAGE_ORDER,
    MODE_BLOCK,
    MODE_OFF,
    MODE_WARN,
    GUARD_VERSION,
    SEVERITY_ERROR,
    SEVERITY_WARN,
    load_pipeline_config,
    mentioned_from_stage_hint,
    render_violations,
    validate_pipeline_plan,
)

PASS = 0
FAIL = 0
FAILED: list[str] = []


def check(label: str, cond: bool, detail: object = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ✓ %s" % label)
    else:
        FAIL += 1
        FAILED.append(label)
        print("  ✗ %s   %s" % (label, ("｜" + str(detail)) if detail != "" else ""))


def codes(res: dict) -> list[str]:
    return [v.get("code") for v in (res.get("violations") or [])]


ACTIVE = {"requirement_structuring", "architecture_skeleton", "view_expansion",
          "model_validation_repair", "trace_verification", "model_release",
          "methodology_resolver", "design", "report_generation", "review"}


def fake_resolve(name: str) -> str:
    n = str(name or "").strip()
    return n if n in ACTIVE else ""


def T(key, agent, deps=None, required=False, task_type="agent"):
    return {"key": key, "title": key, "agent": agent, "task_type": task_type,
            "deps": list(deps or []), "config": {"required": required}}


PLAN_OK = [
    T("t1", "requirement_structuring", [], True),
    T("t2", "architecture_skeleton", ["t1"], True),
    T("t3", "view_expansion", ["t2"], False),
]


def guard(plan, **kw):
    kw.setdefault("stage_order", DEFAULT_STAGE_ORDER)
    kw.setdefault("required_stages", DEFAULT_REQUIRED_STAGES)
    kw.setdefault("resolve", fake_resolve)
    kw.setdefault("mode", MODE_WARN)
    return validate_pipeline_plan(plan, **kw)


print("=" * 70)
print("[1] 正向：该报的必须报")
print("=" * 70)

_r = guard(PLAN_OK)
check("1.1 合法计划通过（ok=True / 零违规）", _r["ok"] and not _r["violations"],
      codes(_r))
check("1.2 stage_sequence 与计划阶段一致",
      _r["stage_sequence"] == ["requirement_structuring", "architecture_skeleton", "view_expansion"],
      _r["stage_sequence"])
check("1.3 stats 口径（tasks/agent_tasks/stage_tasks）",
      _r["stats"]["tasks"] == 3 and _r["stats"]["agent_tasks"] == 3
      and _r["stats"]["stage_tasks"] == 3, _r["stats"])

# 逆序：architecture_skeleton(2) 依赖 view_expansion(3)
_plan_rev = [T("a1", "architecture_skeleton", [], True),
             T("a2", "view_expansion", [], False),
             T("a3", "architecture_skeleton", ["a2"], True)]
_r = guard(_plan_rev)
check("1.4 阶段逆序被报为 error", "stage.order_reversed" in codes(_r),
      codes(_r))
check("1.5 逆序 severity=error 且 ok=False",
      any(v["severity"] == SEVERITY_ERROR for v in _r["violations"]) and not _r["ok"],
      _r["violations"])
check("1.6 逆序违规带 tasks/stages 定位",
      any(set(v.get("tasks") or []) == {"a3", "a2"} for v in _r["violations"]),
      [v.get("tasks") for v in _r["violations"]])

# 完备性
_r = guard([T("t1", "requirement_structuring", [], True)])
check("1.7 缺必需阶段被报 warn", "stage.required_missing" in codes(_r), codes(_r))
check("1.8 缺必需阶段不阻断（ok 仍为 True）", _r["ok"] is True, _r["ok"])

# 覆盖性
_r = guard(PLAN_OK, mentioned_stages=["model_release"])
check("1.9 用户点名阶段未覆盖被报 warn",
      "stage.mentioned_missing" in codes(_r), codes(_r))

# 路由
_r = guard([T("t1", "不存在的Agent", [], True)])
check("1.10 路由解析不到被报 error", "route.unresolved" in codes(_r), codes(_r))

_r = guard([T("t1", "", [], True)])
check("1.11 agent 为空被报 warn（走意图识别，属合法用法）",
      "route.missing" in codes(_r)
      and all(v["severity"] == SEVERITY_WARN for v in _r["violations"] if v["code"] == "route.missing"),
      codes(_r))

_r = guard([T("t1", "design", [], True, task_type="llm")])
check("1.12 非 agent 类型声明了 agent 被报 warn（llm/tool 不读取 agent）",
      "route.ignored_by_type" in codes(_r), codes(_r))

# ★ 2026-11-04（react 方案 A）：react 改为委托主链路后**真的会读 agent** ⇒ 不得再报"声明失效"
_r = guard([T("t1", "design", [], True, task_type="react")])
check("1.12b react 已能读 agent，不再报「声明失效」（防假告警）",
      "route.ignored_by_type" not in codes(_r), codes(_r))

# 外部依赖
_r = guard([T("t1", "requirement_structuring", ["t99"], True)])
check("1.13 依赖计划外 key 被报 warn", "deps.external" in codes(_r), codes(_r))

# 必需标注
_r = guard([T("t1", "requirement_structuring", [], False)])
check("1.14 无任何 required=true 被报 warn", "plan.no_required" in codes(_r), codes(_r))

# 空计划
_r = guard([])
check("1.15 空计划被报 error", "plan.empty" in codes(_r) and not _r["ok"], codes(_r))


print()
print("=" * 70)
print("[2] 反向：不该报的必须沉默（防误报）")
print("=" * 70)

# 同阶段任务互依赖 —— 不构成逆序
_r = guard([T("b1", "view_expansion", [], False),
            T("b2", "view_expansion", ["b1"], False)])
check("2.1 同阶段内任务互依赖不算逆序", "stage.order_reversed" not in codes(_r), codes(_r))

# 非阶段 Agent 不参与阶段序
_r = guard([T("c1", "report_generation", ["c2"], True),
            T("c2", "model_release", [], True),
            T("c3", "requirement_structuring", ["c1"], True)])
check("2.2 非阶段 Agent 不参与逆序判定",
      not any(v["code"] == "stage.order_reversed" and "report_generation" in str(v)
              for v in _r["violations"]), codes(_r))

# 不传 resolve ⇒ 不校验路由（不猜）
_r = guard([T("t1", "完全瞎写的名字", [], True)], resolve=None)
check("2.3 不传 resolve 时不报路由问题（不猜）",
      "route.unresolved" not in codes(_r), codes(_r))

# 不配置 stage_order ⇒ 不校验阶段序（不猜顺序）
_r = guard(_plan_rev, stage_order=[])
check("2.4 未配置 stage_order 时不报逆序（不猜顺序）",
      "stage.order_reversed" not in codes(_r), codes(_r))
check("2.5 未配置时 notes 留痕（否则'没报错'会被误读为'没问题'）",
      any("pipeline_stage_order" in n for n in _r["notes"]), _r["notes"])

# 未配置必需阶段 ⇒ 不报完备性
_r = guard([T("t1", "view_expansion", [], True)], required_stages=[])
check("2.6 未配置 required_stages 时不报完备性",
      "stage.required_missing" not in codes(_r), codes(_r))

# 点名阶段不在 stage_order 里 ⇒ 不报（不猜）
_r = guard(PLAN_OK, mentioned_stages=["某个不在表里的阶段"])
check("2.7 点名阶段不在阶段表内时不报（不猜）",
      "stage.mentioned_missing" not in codes(_r), codes(_r))

# required 为字符串 "true" 也算（真实 LLM 输出常见）
_r = guard([{"key": "t1", "agent": "requirement_structuring", "task_type": "agent",
             "deps": [], "config": {"required": "true"}},
            {"key": "t2", "agent": "architecture_skeleton", "task_type": "agent",
             "deps": ["t1"], "config": {"required": "true"}}])
check("2.8 config.required 为字符串时也识别（LLM 输出常见）",
      "plan.no_required" not in codes(_r), codes(_r))


print()
print("=" * 70)
print("[3] 三档模式")
print("=" * 70)

_hard = [T("t1", "不存在的Agent", [], True)]

_r = guard(_hard, mode=MODE_OFF)
check("3.1 mode=off 零违规", _r["violations"] == [], _r["violations"])
check("3.2 mode=off 不阻断", _r["blocked"] is False, _r["blocked"])

_r = guard(_hard, mode=MODE_WARN)
check("3.3 mode=warn 报错但不阻断", not _r["ok"] and _r["blocked"] is False,
      (_r["ok"], _r["blocked"]))

_r = guard(_hard, mode=MODE_BLOCK)
check("3.4 mode=block 且存在 error 时阻断", _r["blocked"] is True, _r["blocked"])

_r = guard(PLAN_OK, mode=MODE_BLOCK)
check("3.5 mode=block 但无 error 时不阻断", _r["blocked"] is False, _r["blocked"])

_r = guard(_hard, mode="随便写")
check("3.6 非法 mode 按 warn 处理并留痕",
      _r["mode"] == MODE_WARN and any("非法 mode" in n for n in _r["notes"]),
      (_r["mode"], _r["notes"]))


print()
print("=" * 70)
print("[4] 与结构层的分工（不重复、不越界）")
print("=" * 70)

# 有环的计划：guard 不负责检环（TaskQueue.validate_plan 负责）
_cyclic = [T("x1", "requirement_structuring", ["x2"], True),
           T("x2", "architecture_skeleton", ["x1"], True)]
_r = guard(_cyclic)
check("4.1 guard 不报 cycle（结构层职责，不重复实现）",
      not any("cycle" in (c or "") for c in codes(_r)), codes(_r))

from task_queue import TaskQueue  # noqa: E402

_v = TaskQueue.validate_plan(_cyclic)
check("4.2 结构层确实能检出同一个环（分工成立）",
      _v.get("ok") is False and _v.get("reason") == "cycle", _v)

_v = TaskQueue.validate_plan(PLAN_OK)
check("4.3 合法计划结构层也通过（两层不互相误伤）", _v.get("ok") is True, _v)


print()
print("=" * 70)
print("[5] 配置加载（内存库夹具，不碰生产库）")
print("=" * 70)

check("5.1 默认阶段序 = 8 个建模阶段（与 agents.stage_order 同源）",
      DEFAULT_STAGE_ORDER == ["methodology_resolver", "requirement_structuring",
                              "architecture_skeleton", "view_expansion",
                              "model_validation_repair", "trace_verification",
                              "change_safety_gate", "model_release"], DEFAULT_STAGE_ORDER)
check("5.2 默认必需阶段 = 需求结构化 + 架构骨架",
      DEFAULT_REQUIRED_STAGES == ["requirement_structuring", "architecture_skeleton"],
      DEFAULT_REQUIRED_STAGES)
check("5.3 默认档位 = warn（不改变现有行为）", DEFAULT_MODE == "warn", DEFAULT_MODE)


def _mem_conn(rows):
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE generate_rules (rule_key TEXT, rule_value TEXT, description TEXT)")
    for k, v in rows:
        c.execute("INSERT INTO generate_rules VALUES (?,?,'')", (k, v))
    c.commit()
    return c


_c = _mem_conn([])
_cfg = load_pipeline_config(_c)
check("5.4 空表 ⇒ 回落默认且 source=default",
      _cfg["stage_order"] == DEFAULT_STAGE_ORDER and _cfg["mode"] == DEFAULT_MODE
      and _cfg["source"] == "default", _cfg)
_c.close()

_c = _mem_conn([("pipeline_stage_order", json.dumps(["a", "b", "c"])),
                ("pipeline_required_stages", json.dumps(["a"])),
                ("pipeline_guard_mode", "block")])
_cfg = load_pipeline_config(_c)
check("5.5 读到 DB 配置 ⇒ 覆盖默认",
      _cfg["stage_order"] == ["a", "b", "c"] and _cfg["required_stages"] == ["a"]
      and _cfg["mode"] == "block", _cfg)
check("5.6 source 标明实际来源（可审计）", _cfg["source"].startswith("db:"), _cfg["source"])
_c.close()

_c = _mem_conn([("pipeline_stage_order", "{坏 JSON"),
                ("pipeline_guard_mode", "不合法档位")])
_cfg = load_pipeline_config(_c)
check("5.7 坏 JSON / 非法档位 ⇒ 各自回退默认，不影响其它项",
      _cfg["stage_order"] == DEFAULT_STAGE_ORDER and _cfg["mode"] == DEFAULT_MODE, _cfg)
_c.close()


print()
print("=" * 70)
print("[6] 辅助函数")
print("=" * 70)

check("6.1 stage_hint 解析「意图（原句）」",
      mentioned_from_stage_hint(["requirement_structuring（先做需求分析）",
                                 "design（再设计方案）"]) == ["requirement_structuring", "design"],
      mentioned_from_stage_hint(["requirement_structuring（先做需求分析）"]))
check("6.2 stage_hint 解析裸意图名",
      mentioned_from_stage_hint(["design", "review"]) == ["design", "review"])
check("6.3 stage_hint 去重且忽略空值",
      mentioned_from_stage_hint(["design", "design", "", None]) == ["design"])

_viol = [{"severity": "error", "message": "M1"}, {"severity": "warn", "message": "M2"},
         {"severity": "warn", "message": "M3"}, {"severity": "warn", "message": "M4"},
         {"severity": "warn", "message": "M5"}]
_s = render_violations(_viol)
check("6.4 render 只展示前 4 项并给出总数",
      "M4" in _s and "M5" not in _s and "共 5 项" in _s, _s)
check("6.5 render 用 ✗/! 区分 error 与 warn", "✗ M1" in _s and "! M2" in _s, _s)
check("6.6 空违规 render 为空串", render_violations([]) == "")
check("6.7 版本号已定义", bool(GUARD_VERSION))


print()
print("=" * 70)
print("[7] 变异自证（改坏实现 ⇒ 判据必须翻红）")
print("=" * 70)

_SRC = io.open(os.path.join(REPO, "core", "plan_guard.py"), encoding="utf-8").read()


def _mutate(old: str, new: str):
    """把 plan_guard.py 的片段替换后 exec 出孪生体；锚点必须唯一命中。"""
    if _SRC.count(old) != 1:
        return None, _SRC.count(old)
    mutated = _SRC.replace(old, new, 1)
    ns = {"__name__": "mutant_plan_guard"}
    try:
        exec(compile(mutated, "<mutant>", "exec"), ns)
    except Exception as e:
        return None, "exec异常:%s" % str(e)[:60]
    return ns, 1


def _mut_check(label, old, new, fn):
    ns, hit = _mutate(old, new)
    check("%s 锚点唯一命中" % label, hit == 1, "命中=%s" % hit)
    if not ns:
        check(label, False, "变异体未生成")
        return
    try:
        caught = bool(fn(ns))
    except Exception:
        caught = True   # 抛异常也算抓住
    check(label, caught)


# M1：逆序判据反向（< 改 >）⇒ 真正的逆序不再被报
_M1_OLD = "                if stage_index.get(sx.lower(), 0) < stage_index.get(sy.lower(), 0):"
_M1_NEW = "                if stage_index.get(sx.lower(), 0) > stage_index.get(sy.lower(), 0):"
_mut_check("M1 变异「逆序判据反向」被抓住", _M1_OLD, _M1_NEW,
           lambda ns: "stage.order_reversed" not in [
               v["code"] for v in ns["validate_pipeline_plan"](
                   _plan_rev,
                   stage_order=ns["DEFAULT_STAGE_ORDER"],
                   resolve=fake_resolve)["violations"]])

# M2：off 模式失效（不再早退）⇒ off 也变得会报错
_M2_OLD = '    if md == MODE_OFF:\n        notes.append("mode=off：跳过全部语义校验（逐字节回旧行为）")'
_M2_NEW = '    if md == "___never___":\n        notes.append("mode=off：跳过全部语义校验（逐字节回旧行为）")'
_mut_check("M2 变异「off 档失效」被抓住", _M2_OLD, _M2_NEW,
           lambda ns: len(ns["validate_pipeline_plan"](
               _hard, stage_order=ns["DEFAULT_STAGE_ORDER"], resolve=fake_resolve,
               mode="off")["violations"]) > 0)

# M3：不再报"声明了 agent 但类型不读"⇒ 该告警消失
_M3_OLD = "            if raw_agent and tt in (\"llm\", \"tool\"):\n                _add(\"route.ignored_by_type\""
_M3_NEW = "            if False and raw_agent and tt in (\"llm\", \"tool\"):\n                _add(\"route.ignored_by_type\""
_mut_check("M3 变异「不报声明失效」被抓住", _M3_OLD, _M3_NEW,
           lambda ns: "route.ignored_by_type" not in [
               v["code"] for v in ns["validate_pipeline_plan"](
                   [T("t1", "design", [], True, task_type="llm")],
                   stage_order=ns["DEFAULT_STAGE_ORDER"], resolve=fake_resolve)["violations"]])

# M4：规则覆盖失效（`pipeline_stage_order` 不再被采纳）⇒ 显式覆盖被静默忽略
# ⚠️ 原 M4 盯的是 `DEFAULT_STAGE_ORDER` 被清空 —— 阶段序改为优先读 `agents.stage_order`
#    之后，代码常量只是**最后兜底**，清空它已不影响"有规则时"的行为 ⇒ 该判据失去意义。
#    改成盯**优先级**：显式规则必须压过表与兜底。
_M4_OLD = "    so = _json_list(RULE_KEY_STAGE_ORDER)\n    if so:"
_M4_NEW = "    so = _json_list(RULE_KEY_STAGE_ORDER)\n    if False:"
_mut_check("M4 变异「规则覆盖失效」被抓住", _M4_OLD, _M4_NEW,
           lambda ns: ns["load_pipeline_config"](
               _mem_conn([(ns["RULE_KEY_STAGE_ORDER"], json.dumps(["a", "b"]))]))["stage_order"] != ["a", "b"])

# M5：blocked 恒 False ⇒ 拦截档失效
_M5_OLD = '        "blocked": bool(mode == MODE_BLOCK and not ok),'
_M5_NEW = '        "blocked": False,'
_mut_check("M5 变异「blocked 恒 False」被抓住", _M5_OLD, _M5_NEW,
           lambda ns: ns["validate_pipeline_plan"](
               _hard, stage_order=ns["DEFAULT_STAGE_ORDER"], resolve=fake_resolve,
               mode="block")["blocked"] is False)


print()
print("=" * 70)
print("[9] 接线覆盖：流式与非流式**两条**建计划路径都必须调用校验器")
print("=" * 70)

_s_stream = io.open(os.path.join(REPO, "agent/pipeline_parts/stream.py"), encoding="utf-8").read()
_s_planner = io.open(os.path.join(REPO, "workflows/planner.py"), encoding="utf-8").read()

check("9.1 非流式路径（_planner_core）调用校验器", "validate_pipeline_plan(" in _s_planner)
# ⚠️ 这条曾经是假绿：校验器只接在 `_planner_core`，而**流式主路径不走它**
#    （`_stream_orchestrated_flow` 自己建计划）⇒ 真实用户的 SSE 会话从未校验过。
check("9.2 **流式主路径**（_stream_orchestrated_flow）也调用校验器",
      "validate_pipeline_plan as _validate_pg" in _s_stream
      and "_validate_pg(" in _s_stream, "")
check("9.3 两条路径都用 mode=block 时真终止（抛 ValueError 进早退通道）",
      "计划语义校验未通过（mode=block）" in _s_stream
      and "if _pg_blocked:" in _s_planner)
check("9.4 校验器异常必须留痕（不能静默当成无障碍）",
      "计划语义校验异常" in _s_stream and "计划语义校验异常" in _s_planner)

print()
print("=" * 70)
print("汇总：PASS = %d / FAIL = %d" % (PASS, FAIL))
if FAILED:
    print("失败项：")
    for f in FAILED:
        print("  -", f)
print("=" * 70)
sys.exit(0 if FAIL == 0 else 1)
