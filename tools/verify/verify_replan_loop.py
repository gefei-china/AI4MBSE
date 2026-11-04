"""重规划决策器（services/replan_loop.py）自检 —— 含**变异自证**与**双路径一致性**。

口径：裸跑；不启服务、不调 LLM、不写生产库（内存 sqlite 模拟）。

覆盖四层：
  1. 闸门（21 个键 / 默认值 / 0 不被吞 / 类型归一 / 读不到配置即保守兜底）
  2. 决策（早停优先于巡检 / 节奏闸 / required 语义 / none 也留痕）
  3. **双路径一致性**（流式与非流式必须用同一闸门出处、同一决策函数）
  4. 状态口径（`pending_count` 的 cancelled 拼写回归）

⚠️ 第 3 层是本脚本存在的主要理由：决策纯函数早已共享，**闸门与节奏曾经各写一份**。
   没有这道门，下次往 `gates()` 加键、另一条路径不读，仍然会静默漂移。
"""

import io
import os
import re
import sqlite3
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

from services import replan_loop as RL           # noqa: E402
from services import replan_health as RH         # noqa: E402

PASS = 0
FAIL = 0
FAILED = []


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ✓ %s" % label)
    else:
        FAIL += 1
        FAILED.append(label)
        print("  ✗ %s   %s" % (label, detail))


def read_src(rel):
    return io.open(os.path.join(REPO, rel), encoding="utf-8").read()


class FakeCfg:
    """最小配置对象：只实现 `get(section, key, default)`。"""

    def __init__(self, data=None):
        self.d = data or {}

    def get(self, sec, key, default=None):
        return self.d.get((sec, key), default)


def T(key, status, required=False, **kw):
    t = {"task_key": key, "status": status, "config": {"required": required}}
    t.update(kw)
    return t


def D(key, status="partial", **kw):
    it = {"task_key": key, "status": "done", "summary": {"status": status}}
    it.update(kw)
    return it


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


print("=" * 74)
print("[1] 闸门：唯一出处、21 键、默认值、0 不被吞")
print("=" * 74)

_g = RL.gates(FakeCfg())
check("1.1 闸门键数与 GATE_DEFAULTS 一致（21）",
      len([k for k in _g if k != "source"]) == len(RL.GATE_DEFAULTS),
      "%d vs %d" % (len([k for k in _g if k != "source"]), len(RL.GATE_DEFAULTS)))
check("1.2 默认全关（enabled=False，巡检/早停/重画全 False）",
      _g["enabled"] is False and _g["health_probe"] is False
      and _g["early_stop"] is False and _g["heavy_redraw"] is False)
check("1.3 默认 retry_once=True（唯一开着的动作）", _g["retry_once"] is True)
check("1.4 source=config", _g["source"] == "config", _g["source"])

_g0 = RL.gates(FakeCfg({("replanning", "probe_every_tasks"): 0}))
check("1.5 probe_every_tasks=0 **保留 0**（不被 `or 3` 吞成 3）",
      _g0["probe_every_tasks"] == 0, _g0["probe_every_tasks"])
_g0b = RL.gates(FakeCfg({("replanning", "probe_every_tasks"): None}))
check("1.6 probe_every_tasks=None 回落 3", _g0b["probe_every_tasks"] == 3,
      _g0b["probe_every_tasks"])

_gs = RL.gates(FakeCfg({("replanning", "enabled"): "true",
                        ("replanning", "early_stop"): "1",
                        ("replanning", "health_probe"): "off"}))
check("1.7 字符串布尔归一（'true'/'1'→True，'off'→False）",
      _gs["enabled"] is True and _gs["early_stop"] is True
      and _gs["health_probe"] is False, _gs)


class BoomCfg:
    def get(self, *a, **kw):
        raise RuntimeError("config broken")


_gb = RL.gates(BoomCfg())
check("1.8 配置读取抛异常 ⇒ 保守兜底（全关）+ source=fallback",
      _gb["enabled"] is False and _gb["health_probe"] is False
      and _gb["source"] == "fallback", _gb.get("source"))
check("1.9 llm_actions 逗号串解析（默认 replace）",
      RL.llm_actions({"llm_actions": "replace,append"}) == ("replace", "append")
      and RL.llm_actions({}) == ("replace",))

print()
print("=" * 74)
print("[2] available()：总闸在前的语义（上一版建议错的根因）")
print("=" * 74)


def mk(**gates):
    base = {"enabled": False, "health_probe": False, "early_stop": False,
            "probe_every_tasks": 1, "probe_every_sec": 3600,
            "probe_time_budget_sec": 0, "health_probe_llm": False}
    base.update(gates)
    return RL.ReplanLoop(gate_cfg=base, clock=Clock())


check("2.1 enabled=False + health_probe=True ⇒ **不跑**（总闸在前）",
      mk(health_probe=True).available() is False)
check("2.2 enabled=True + health_probe=True ⇒ 跑",
      mk(enabled=True, health_probe=True).available() is True)
check("2.3 enabled=True + early_stop=True ⇒ 跑",
      mk(enabled=True, early_stop=True).available() is True)
check("2.4 enabled=True 但两个分闸都关 ⇒ 不跑",
      mk(enabled=True).available() is False)

print()
print("=" * 74)
print("[3] 决策：早停优先、required 语义、节奏闸、none 留痕")
print("=" * 74)

# 3.1 早停：两必需已完成 + 一个非必需待跑 ⇒ 取消后者
_recs = []
_lp = mk(enabled=True, early_stop=True, health_probe=True)
_decs = _lp.decide([D("t1", "ok"), D("t2", "ok")],
                   [T("t1", "done", True), T("t2", "done", True), T("t3", "planned", False)],
                   latencies_ms=[1000, 1200])
check("3.1 必需全完成 ⇒ 产出 early_stop 决策",
      len(_decs) == 1 and _decs[0]["action"] == "early_stop",
      [d["action"] for d in _decs])
check("3.2 cancel_keys 只含非必需待跑任务（t3）",
      _decs[0]["cancel_keys"] == ["t3"] if _decs else False,
      _decs[0]["cancel_keys"] if _decs else None)
check("3.3 早停**优先于**巡检（同边界不再产出 health 决策）",
      len(_decs) == 1, len(_decs))

# 3.4 无 required 标注 ⇒ 不猜
_d2 = mk(enabled=True, early_stop=True).decide(
    [D("t1", "ok")], [T("t1", "done"), T("t2", "planned")])
check("3.4 无 required 标注 ⇒ 不早停（不猜目标边界）",
      not [d for d in _d2 if d["action"] == "early_stop"], [d["action"] for d in _d2])

# 3.5 有 failed ⇒ 不早停
_d3 = mk(enabled=True, early_stop=True).decide(
    [D("t1", "ok")], [T("t1", "done", True), T("t9", "failed", False), T("t2", "planned", False)])
check("3.5 存在 failed ⇒ 不早停（走失败路径）",
      not [d for d in _d3 if d["action"] == "early_stop"], [d["action"] for d in _d3])

# 3.6 节奏：每 1 个任务判一次 ⇒ 1 个 done 即巡检
_d4 = mk(enabled=True, health_probe=True, probe_every_tasks=1).decide(
    [D("t1", "ok")], [T("t1", "done", True), T("t2", "planned", False)])
check("3.6 probe_every_tasks=1 ⇒ 完成 1 个即巡检",
      len(_d4) == 1 and _d4[0]["source"] == "health", [d["source"] for d in _d4])

# 3.7 节奏：每 3 个 ⇒ 1 个 done 不巡检
_d5 = mk(enabled=True, health_probe=True, probe_every_tasks=3).decide(
    [D("t1", "ok")], [T("t1", "done", True), T("t2", "planned", False)])
check("3.7 probe_every_tasks=3 ⇒ 完成 1 个不巡检（节奏未到）", _d5 == [], _d5)

# 3.8 时间节奏：clock 推进 61s 即到期
_clk = Clock(1000.0)
_lp8 = RL.ReplanLoop(gate_cfg={"enabled": True, "health_probe": True,
                               "early_stop": False, "probe_every_tasks": 0,
                               "probe_every_sec": 60, "probe_time_budget_sec": 0},
                     clock=_clk)
_lp8.decide([], [])
_clk.t += 61
_d8 = _lp8.decide([], [T("t1", "planned", True)])
check("3.8 按时间节奏：60s 到期即巡检（probe_every_tasks=0 时走秒）",
      len(_d8) == 1 and _d8[0]["source"] == "health", [d["source"] for d in _d8])

# 3.9 健康 ⇒ action=none 但**仍返回**（"为什么不动"也要可见）
_d9 = mk(enabled=True, health_probe=True, probe_every_tasks=1).decide(
    [D("t1", "ok")], [T("t1", "done", True), T("t2", "planned", False)])
check("3.9 巡检结论为 none 时也返回决策（不静默）",
      len(_d9) == 1 and _d9[0]["action"] == "none", [d["action"] for d in _d9])

# 3.10 M1 超阈 ⇒ refine（且带 refine_note）
_d10 = mk(enabled=True, health_probe=True, probe_every_tasks=1).decide(
    [D("a", "partial"), D("b", "partial"), D("c", "ok")],
    [T("a", "done", True), T("b", "done", False), T("c", "done", False),
     T("d", "planned", True)])
check("3.10 M1 partial 密度超阈 ⇒ refine",
      len(_d10) == 1 and _d10[0]["action"] == "refine", [d["action"] for d in _d10])
check("3.11 refine 带 refine_note（供调用方写入剩余任务 context）",
      bool(_d10 and _d10[0]["refine_note"]), _d10[0]["refine_note"] if _d10 else None)

# 3.13 窗口与节奏解耦：节奏=1 时，M1 仍需 ≥3 个样本（少样本占比是噪声）
_d13 = mk(enabled=True, health_probe=True, probe_every_tasks=1).decide(
    [D("a", "partial"), D("b", "partial")],
    [T("a", "done", True), T("b", "done", False), T("c", "planned", True)])
check("3.13 probe_every_tasks=1 时窗口仍 ≥3（2 个样本不判 M1）",
      len(_d13) == 1 and _d13[0]["action"] == "none", [d["action"] for d in _d13])

# 3.12 落痕：每条决策（含 none）都调 write_decision
_seen = []
_lp12a = RL.ReplanLoop(gate_cfg={"enabled": True, "health_probe": True, "early_stop": False,
                                 "probe_every_tasks": 1, "probe_every_sec": 3600,
                                 "probe_time_budget_sec": 0},
                       write_decision=lambda s, d: _seen.append((s, d.get("action"))),
                       clock=Clock())
_lp12a.decide([D("t1", "ok")], [T("t1", "done", True), T("t2", "planned", False)])
_lp12b = RL.ReplanLoop(gate_cfg={"enabled": True, "health_probe": False, "early_stop": True,
                                 "probe_every_tasks": 1, "probe_every_sec": 3600,
                                 "probe_time_budget_sec": 0},
                       write_decision=lambda s, d: _seen.append((s, d.get("action"))),
                       clock=Clock())
_lp12b.decide([D("t1", "ok"), D("t2", "ok")],
              [T("t1", "done", True), T("t2", "done", False), T("t3", "planned", False)])
check("3.12 决策全部留痕（含 none）且早停单独留痕",
      ("health", "none") in _seen and ("early_stop", "early_stop") in _seen, _seen)

print()
print("=" * 74)
print("[4] 双路径一致性：闸门同源、决策同源（本脚本的主要理由）")
print("=" * 74)

_s_stream = read_src("agent/pipeline_parts/stream.py")
_s_planner = read_src("workflows/planner.py")

check("4.1 流式路径从 services.replan_loop 读闸门",
      "from services.replan_loop import gates as _replan_gates" in _s_stream)
check("4.2 流式路径**不再**内联读 replanning 闸门（无 `_rcfg.get(\"replanning\"`）",
      '_rcfg.get("replanning"' not in _s_stream,
      "残留 %d 处" % _s_stream.count('_rcfg.get("replanning"'))
check("4.3 流式路径的 _rp_* 闸门变量全部由 _rl_gates 赋值",
      len(re.findall(r"_rl_gates\.get\(", _s_stream)) >= 20,
      len(re.findall(r"_rl_gates\.get\(", _s_stream)))
check("4.4 非流式路径使用同一个决策器（ReplanLoop）",
      "from services.replan_loop import ReplanLoop" in _s_planner)
check("4.5 非流式路径在**迭代边界**调用决策（decide + TaskQueue.tasks）",
      ".decide(_done_items, TaskQueue.tasks(conn, run_id)" in _s_planner)
check("4.6 两条路径都调 replan_health 的判定纯函数（无第二套判定）",
      # 判据打在「**有没有人另写一份 `def early_stop_plan`**」上，而不是
      # 「有没有直接调用」—— 直接调用共享纯函数恰恰是期望行为（流式路径就这么做）。
      "def early_stop_plan" not in _s_stream
      and "def early_stop_plan" not in _s_planner
      and "from services import replan_health" in _s_stream)
check("4.9 两条路径都真正用到早停判据（流式直调 / 非流式经决策器）",
      "early_stop_plan(" in _s_stream
      and "early_stop_plan(" in read_src("services/replan_loop.py") and
      "ReplanLoop(" in _s_planner)
check("4.7 决策器内部只调 replan_health 的纯函数（不复制判定）",
      "early_stop_plan" in read_src("services/replan_loop.py")
      and "health_probe" in read_src("services/replan_loop.py")
      and "probe_due" in read_src("services/replan_loop.py"))

# 4.8 闸门键集合覆盖：stream 用到的 _rp_* 变量都能在 gates() 里找到对应语义
_gate_keys = set(RL.GATE_DEFAULTS)
_missing = [k for k in ("enabled", "health_probe", "early_stop", "probe_every_tasks",
                        "probe_every_sec", "probe_time_budget_sec", "health_probe_llm",
                        "redraw_on_stall", "redraw_light", "heavy_redraw", "skip_leaf",
                        "no_progress_stop", "max_redraw_rounds", "redraw_expand_ratio",
                        "user_directive", "user_confirm_redraw", "confirm_ttl_sec",
                        "directive_max_touch", "llm_redraw", "retry_once", "llm_actions")
           if k not in _gate_keys]
check("4.8 21 个闸门键全部在 GATE_DEFAULTS（无遗漏）", not _missing, _missing)

print()
print("=" * 74)
print("[5] 状态口径回归：pending_count 必须排除 cancelled（两个 L）")
print("=" * 74)

_conn = sqlite3.connect(":memory:")
_conn.row_factory = sqlite3.Row
_conn.execute("CREATE TABLE agent_tasks (id INTEGER PRIMARY KEY, run_id INT, status TEXT)")
for _st in ("planned", "done", "failed", "cancelled", "canceled"):
    _conn.execute("INSERT INTO agent_tasks (run_id, status) VALUES (1, ?)", (_st,))
_conn.commit()

from task_queue import TaskQueue   # noqa: E402

_pc = TaskQueue.pending_count(_conn, 1)
check("5.1 pending_count 只数 planned（done/failed/cancelled/canceled 全排除）",
      _pc == 1, _pc)
check("5.2 源码同时排除两种拼写（防跨版本再分裂）",
      "'canceled','cancelled'" in read_src("task_queue.py").replace(" ", ""),
      "")

# 5.3 推迟非必需任务：早停要"来得及"取消它
_c5 = sqlite3.connect(":memory:")
_c5.row_factory = sqlite3.Row
_c5.execute("CREATE TABLE agent_tasks (id INTEGER PRIMARY KEY, run_id INT, task_key TEXT, "
            "status TEXT, deps TEXT, config TEXT, seq INT)")
_c5.execute("INSERT INTO agent_tasks (run_id,task_key,status,deps,config,seq) "
            "VALUES (1,'t1','ready','[]','{\"required\": true}',1)")
_c5.execute("INSERT INTO agent_tasks (run_id,task_key,status,deps,config,seq) "
            "VALUES (1,'t2','ready','[]','{\"required\": false}',2)")
_c5.commit()
_all = TaskQueue.ready_tasks(_c5, 1)
_req = TaskQueue.ready_tasks(_c5, 1, defer_optional=True)
check("5.3 defer_optional=False ⇒ 必需与非必需同批派出（旧行为）",
      [t["task_key"] for t in _all] == ["t1", "t2"], [t["task_key"] for t in _all])
check("5.4 defer_optional=True ⇒ 本轮只派必需任务（非必需留到下一边界）",
      [t["task_key"] for t in _req] == ["t1"], [t["task_key"] for t in _req])
_c5.execute("UPDATE agent_tasks SET config='{\"required\": false}' WHERE task_key='t1'")
_c5.commit()
_req2 = TaskQueue.ready_tasks(_c5, 1, defer_optional=True)
check("5.5 无必需任务可跑 ⇒ 不推迟（不会死锁）",
      [t["task_key"] for t in _req2] == ["t1", "t2"], [t["task_key"] for t in _req2])
check("5.6 两条路径都传了 defer_optional（否则扇出计划下早停仍是空转）",
      "defer_optional=bool(_rl is not None and _rl.g.get(\"early_stop\"))" in _s_planner
      and "defer_optional=bool(_rp_early)" in _s_stream)

print()
print("=" * 74)
print("[6] 变异自证：把实现改坏，判据必须翻")
print("=" * 74)


def mutate(src_rel, old, new):
    src = read_src(src_rel)
    if old not in src:
        return None
    return src.replace(old, new, 1)


def ns_of(src, name="mutant"):
    ns = {"__name__": name}
    exec(compile(src, "<mutant>", "exec"), ns)
    return ns


# M1：available() 忽略总闸 ⇒ 2.1 应翻
_m = mutate("services/replan_loop.py",
            '        if not g.get("enabled"):\n            return False',
            '        if False:\n            return False')
if _m:
    _ns = ns_of(_m)
    _lp_m = _ns["ReplanLoop"](gate_cfg={"enabled": False, "health_probe": True},
                              clock=Clock())
    check("M1 变异「忽略总闸」被抓住", _lp_m.available() is True)
else:
    check("M1 变异「忽略总闸」被抓住", False, "锚点未命中")

# M2：巡检不再要求 early_stop 优先 ⇒ 3.3 应翻（早停后仍巡检）
_m2 = mutate("services/replan_loop.py",
             "                    self._record(\"early_stop\", es)\n                    out.append(dec)\n                    return out",
             "                    self._record(\"early_stop\", es)\n                    out.append(dec)")
if _m2:
    _ns2 = ns_of(_m2)
    _lp2 = _ns2["ReplanLoop"](gate_cfg={"enabled": True, "health_probe": True,
                                        "early_stop": True, "probe_every_tasks": 1,
                                        "probe_every_sec": 3600,
                                        "probe_time_budget_sec": 0}, clock=Clock())
    _d2m = _lp2.decide([D("t1", "ok")],
                       [T("t1", "done", True), T("t2", "done", False), T("t3", "planned", False)])
    # 变异后：早停决策 + 巡检决策**都**出现（2 条）⇒ 说明优先级被破坏
    check("M2 变异「早停后缺 return ⇒ 巡检仍执行」被抓住",
          len(_d2m) == 2, [d["action"] for d in _d2m])
else:
    check("M2 变异「早停后不再返回（缺 return）」被抓住", False, "锚点未命中")

# M3：`_as_int` 退化为 `int(v or default)` ⇒ 1.5 应翻（0 被吞）
_m3 = mutate("services/replan_loop.py",
             "    if v is None or v == \"\":\n        return default\n    try:\n        return int(v)",
             "    try:\n        return int(v or default)")
if _m3:
    _ns3 = ns_of(_m3)
    _v = _ns3["_as_int"](0, 3)
    check("M3 变异「0 被 or 吞成默认值」被抓住", _v == 3, _v)
else:
    check("M3 变异「0 被 or 吞成默认值」被抓住", False, "锚点未命中")

# M4：pending_count 去掉 'cancelled' ⇒ 5.1 应翻
_m4 = mutate("task_queue.py",
             "\"AND status NOT IN ('done','failed','canceled','cancelled')\"",
             "\"AND status NOT IN ('done','failed','canceled')\"")
if _m4:
    _ns4 = ns_of(_m4)
    _c4 = sqlite3.connect(":memory:")
    _c4.row_factory = sqlite3.Row
    _c4.execute("CREATE TABLE agent_tasks (id INTEGER PRIMARY KEY, run_id INT, status TEXT)")
    for _st in ("planned", "done", "failed", "cancelled", "canceled"):
        _c4.execute("INSERT INTO agent_tasks (run_id, status) VALUES (1, ?)", (_st,))
    _c4.commit()
    _pc4 = _ns4["TaskQueue"].pending_count(_c4, 1)
    check("M4 变异「漏排 cancelled」被抓住", _pc4 == 2, _pc4)
else:
    check("M4 变异「漏排 cancelled」被抓住", False, "锚点未命中")

# M5：窗口与节奏重新绑死（window = probe_every_tasks）⇒ 3.10 应翻
_m5 = mutate("services/replan_loop.py",
             "            w = window if window is not None else max(int(every_tasks or 0), _rh.M1_MIN_WINDOW)",
             "            w = window if window is not None else int(every_tasks or 0)")
if _m5:
    _ns5 = ns_of(_m5)
    _lp5 = _ns5["ReplanLoop"](
        gate_cfg={"enabled": True, "health_probe": True, "early_stop": False,
                  "probe_every_tasks": 1, "probe_every_sec": 3600,
                  "probe_time_budget_sec": 0, "health_probe_llm": False},
        clock=Clock())
    _d5m = _lp5.decide([D("a", "partial"), D("b", "partial"), D("c", "ok")],
                       [T("a", "done", True), T("b", "done", False),
                        T("c", "done", False), T("d", "planned", True)])
    check("M5 变异「窗口绑死节奏 ⇒ M1 失效」被抓住",
          _d5m and _d5m[0]["action"] != "refine", [d["action"] for d in _d5m])
else:
    check("M5 变异「窗口绑死节奏 ⇒ M1 失效」被抓住", False, "锚点未命中")

print()
print("=" * 74)
print("汇总：PASS = %d / FAIL = %d" % (PASS, FAIL))
if FAILED:
    print("失败项：")
    for f in FAILED:
        print("  - %s" % f)
print("口径：裸跑；不启服务、不调 LLM、不写生产库（内存 sqlite 模拟）。")
sys.exit(0 if FAIL == 0 else 1)
