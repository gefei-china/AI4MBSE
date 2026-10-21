# -*- coding: utf-8 -*-
"""P3 replanning（2026-10-21，执行中重规划第三阶段）回归验证 —— 零 LLM、零服务、零生产库写。

**为什么需要它**（设计文档 v2 §7 P3 / §4.7 / §4.8）：P1 收信号、P2 局部修图，
    但两者都只对"**任务自己失败**"有反应。真实场景里最高频的重规划触发是
    **用户在 DAG 执行中改主意**（v2 §4.7 的 S1/S2）。本门禁钉住七件事不退化：

    S  静态契约：三模块入口齐备、纯函数纪律、挂点存在、配置双登记、CI 注册
    U1 G8 影响评估逐格：前置闸（resume/已消费/空/过短/纯信息/无待办）、
        T2-light/heavy/T3 分级、T0 忽略、done 永不重跑
    U2 ★ **S1/S2 不得直接进 T3**（v2 §4.7 明文约束的执行点）：
        产出要求类指令 ⇒ T2-light；新增/删除/改划分 ⇒ T3
    U3 G10 可解释性：每次评估产出 `basis`（依据），不只给级别
    U4 ★ **零行为变更**（P3 头号纪律）：`user_directive`/`user_confirm_redraw`
        默认关 ⇒ 挂点段与确认卡段整体 no-op
    U5 确认卡：TTL 超时**维持原计划**（v1 R10）、三动作、非法动作兜底、
        阻塞等待不持锁（死锁防线）
    U6 T3 LLM 重画（默认关）：过 Q1~Q5、replace 范围锁定、
        deps 重映射、非 JSON ⇒ T4
    M  变异自证：删挂点 / 放开 T3 / 关 TTL 兜底 / 去掉 replace 锁定 /
        删质量门 —— 每个变异都必须被本门禁抓住
    X  接线正确性：轻量级重派走 UPDATE（不追行）、DAG 级只读不消费 followup

**为什么"不得直接进 T3"要有专门一节**（U2）：v1 §3.2 把 S1 直接归 T3，
    v2 §4.7 明确修正为「没有影响评估的 S1/S2 不得直接进 T3」。若某次改动
    让「加一个任务」以外的所有指令都走 T3，**功能上看不出错**（图确实被
    调整了），但成本与可解释性崩塌 —— 用户一句话烧掉一次 planner 调用。

**为什么"replace 范围锁定"要单测**（U6）：拍板⑤ 明确 LLM 版三动作里
    本阶段只做 replace。顺序配对式的"兜底"会把 insert 从后门放进来，
    而且 Q1~Q5 **全绿**、日志无异常（2026-10-21 实施期真 bug）。

用法：
    <repo>\\.venv\\Scripts\\python.exe -X utf8 tools/verify/verify_replan_p3.py
退出码：全绿 0 / 有失败或空转 1。
"""
import json
import os
import sys
import threading
import time as _t2

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

DIR_SRC = os.path.join(ROOT, "services", "replan_directive.py")
CONF_SRC = os.path.join(ROOT, "services", "replan_confirm.py")
REDRAW_SRC = os.path.join(ROOT, "services", "replan_redraw.py")
FOLLOWUP_SRC = os.path.join(ROOT, "agent", "followup.py")
STREAM_SRC = os.path.join(ROOT, "agent", "pipeline_parts", "stream.py")
CONFIG_SRC = os.path.join(ROOT, "core", "config.py")
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


def _has_bare_except(src, start_marker, end_marker):
    """区间内是否以裸 `except Exception: pass` 收尾（观测旁路铁律的判据）。

    为什么不直接找固定字符串：挂点段的缩进随代码演进会变，写死字符串的断言
    会在重构时**假红**，也会在有人删掉 except 时被误判通过。改为"区间内出现
    except Exception 收尾"这个语义判据。
    """
    try:
        i = src.index(start_marker)
        j = src.index(end_marker, i)
    except ValueError:
        return False
    seg = src[i:j]
    return "except Exception:" in seg and "pass" in seg


def T(k, deps=None, st="planned", **kw):
    d = {"id": abs(hash(k)) % 10000, "task_key": k,
         "deps": json.dumps(deps or []), "status": st, "title": "任务" + k,
         "agent_id": "design", "seq": 0, "context": "原始上下文",
         "expected_output": "", "config": "{}", "error": "", "error_class": ""}
    d.update(kw)
    return d


def three_ts():
    """标准三段图：t1 done、t2 planned、t3 planned（t3 deps t2）。"""
    ts = [T("t1", [], "done"), T("t2", ["t1"]), T("t3", ["t2"])]
    return ts


# ══════════════════════════════════════════════════════════════════════════
def t_static():
    print("\n=== S. 静态契约 ===")
    ok = True
    dsrc = _read(DIR_SRC)

    ok &= _rec("S1a 影响评估模块九个入口齐备",
               all(("def " + m) in dsrc for m in (
                   "should_evaluate", "assess_impact", "pending_tasks",
                   "retask_context", "directive_meta", "llm_redraw_prompt",
                   "parse_llm_plan")))
    ok &= _rec("S1b ★ 纯函数纪律：影响评估模块无 sqlite/requests/get_db/open(",
               not any(t in dsrc for t in ("sqlite3", "requests", "urllib.request",
                                           "get_db", "open(")))
    ok &= _rec("S1c ★ 确认卡模块同样零 IO/零 DB（可被门禁逐格单测）",
               not any(t in _read(CONF_SRC) for t in ("sqlite3", "get_db",
                                                      "requests")))
    ok &= _rec("S1d 四级常量齐备（T0/T2-light/T2-heavy/T3）",
               all(x in dsrc for x in ("LEVEL_IGNORE", "LEVEL_LIGHT",
                                       "LEVEL_HEAVY", "LEVEL_REDRAW")))
    ok &= _rec("S1e ★ replace 范围锁定：解析层**不做顺序兜底配对**",
               "顺序兜底" not in dsrc.replace("顺序兜底**把", "")
               or "不做顺序兜底" in dsrc)
    ok &= _rec("S1f T3 入口在 replan_redraw 内（与规则版共用质量门）",
               "def plan_llm_redraw(" in _read(REDRAW_SRC)
               and "quality_gates(" in _read(REDRAW_SRC))
    ok &= _rec("S1g followup 新增只读快照 API（peek_text）",
               "def peek_text(" in _read(FOLLOWUP_SRC))
    # 前端与 REST：确认卡要有渲染函数 + 应答端点，否则"用户能不能否决"是假的
    with open(os.path.join(ROOT, "static", "js", "mods", "11-pipeline.js"),
              encoding="utf-8") as f:
        js = f.read()
    ok &= _rec("S1h ★ 前端有确认卡渲染函数 + SSE 分支",
               "function procAddRedrawConfirm(" in js
               and "evType === 'redraw_confirm'" in js)
    rsrc = _read(os.path.join(ROOT, "routers", "conversations.py"))
    ok &= _rec("S1i ★ REST 有 redraw-confirm 应答端点（按钮有落点）",
               "/redraw-confirm" in rsrc and "def redraw_confirm(" in rsrc)
    ok &= _rec("S1j ★ 端点做归属校验（代批他人重画 = 越权写操作）",
               "_own_conv" in rsrc[rsrc.index("def redraw_confirm("):][:600])
    ok &= _rec("S1k ★ 前端计划修订卡补 T3 级别标签",
               "'T3':'LLM 整图重画" in js)
    return ok


def t_config():
    print("\n=== S2. 配置双登记（P3 四键）===")
    ok = True
    src = _read(CONFIG_SRC)
    for k in ("user_directive", "user_confirm_redraw", "confirm_ttl_sec",
              "directive_max_touch"):
        ok &= _rec("S2a %s 出现在 DEFAULT_CONFIG" % k,
                   '"%s"' % k in src)
    # 白名单纪律：不登记进 SCHEMA 的键连设置页都改不了（B4-5 教训）
    ok &= _rec("S2b ★ 四键全部登记进 CONFIG_SCHEMA（可被设置页/REST 修改）",
               src.count('"user_directive"') >= 2
               and src.count('"user_confirm_redraw"') >= 2
               and src.count('"confirm_ttl_sec"') >= 2
               and src.count('"directive_max_touch"') >= 2)
    ok &= _rec("S2c ★ 默认关：user_directive/user_confirm_redraw 皆 False",
               '"user_directive": False' in src
               and '"user_confirm_redraw": False' in src)
    ok &= _rec("S2d llm_redraw 仍默认关（P3 不改此默认值）",
               '"llm_redraw": False' in src)
    ok &= _rec("S2e ★ 确认卡 TTL 默认 60s（v2 §4.8）",
               '"confirm_ttl_sec": 60' in src)
    # 零行为变更的**配置面**保证
    from core import config as C
    d = C.DEFAULT_CONFIG.get("replanning", {})
    ok &= _rec("S2f ★ 运行期取默认值：P3 两闸皆关",
               d.get("user_directive") is False
               and d.get("user_confirm_redraw") is False
               and d.get("llm_redraw") is False)
    return ok


def t_unit():
    print("\n=== U1. G8 影响评估：前置闸与分级 ===")
    from services import replan_directive as RD
    ok = True
    ts = three_ts()
    pend = RD.pending_tasks(ts)

    ok &= _rec("U1a pending_tasks 排除 done/终态（不变量 1 入口）",
               [t["task_key"] for t in pend] == ["t2", "t3"])
    ok &= _rec("U1b ★ 终态两种拼写都认（cancelled 双 L / canceled 单 L）",
               RD.pending_tasks([T("a", st="cancelled"), T("b", st="canceled")]) == [])

    print("\n--- U1c 前置闸逐条（每条都有一个真实的坑）---")
    g = RD.should_evaluate("第三部分改成表格", enabled=True, has_pending_tasks=True)
    ok &= _rec("U1c1 正常指令 ⇒ 评估", g.get("evaluate") is True)
    ok &= _rec("U1c2 ★ resume 不消费（C3）",
               RD.should_evaluate("改成表格", enabled=True,
                                  is_resume=True)["evaluate"] is False)
    ok &= _rec("U1c3 ★ 已被单 Agent 消费 ⇒ 不评估（C1 分工，防同一句跑两遍）",
               RD.should_evaluate("改成表格", enabled=True,
                                  consumed_by_agent_loop=True)["evaluate"] is False)
    ok &= _rec("U1c4 总闸关 ⇒ 不评估（零行为变更）",
               RD.should_evaluate("改成表格", enabled=False)["evaluate"] is False)
    ok &= _rec("U1c5 空指令 ⇒ 不评估",
               RD.should_evaluate("", enabled=True)["evaluate"] is False)
    ok &= _rec("U1c6 过短 ⇒ 不评估",
               RD.should_evaluate("改", enabled=True)["evaluate"] is False)
    ok &= _rec("U1c7 ★ 纯信息类 ⇒ 不评估（T0）",
               RD.should_evaluate("辛苦了", enabled=True)["evaluate"] is False)
    ok &= _rec("U1c8 无未执行任务 ⇒ 不评估（走 C2 新一轮）",
               RD.should_evaluate("改成表格", enabled=True,
                                  has_pending_tasks=False)["evaluate"] is False)

    print("\n--- U1d ★ 纯信息类：含信息词但非纯信息 ⇒ 仍须评估 ---")
    ok &= _rec("U1d1 「辛苦了，第三部分改成表格」⇒ 评估（不是信息类）",
               RD.should_evaluate("辛苦了，第三部分改成表格",
                                  enabled=True)["evaluate"] is True)
    ok &= _rec("U1d2 ★ assess_impact 独立调用也判 T0（防御性重复，非依赖调用方）",
               RD.assess_impact("辛苦了", ts)["level"] == RD.LEVEL_IGNORE)

    print("\n--- U2. ★ S1/S2 不得直接进 T3（v2 §4.7）---")
    ok &= _rec("U2a 产出要求类 ⇒ T2-light（不是 T3）",
               RD.assess_impact("第三部分改成表格形式", ts)["level"]
               == RD.LEVEL_LIGHT)
    ok &= _rec("U2b ★ 新增任务信号 ⇒ T3",
               RD.assess_impact("再加一个性能评审任务", ts)["level"]
               == RD.LEVEL_REDRAW)
    ok &= _rec("U2c 删除任务信号 ⇒ T3",
               RD.assess_impact("不要做性能分析了", ts)["level"]
               == RD.LEVEL_REDRAW)
    ok &= _rec("U2d 改任务划分 ⇒ T3",
               RD.assess_impact("整体重新规划一下", ts)["level"]
               == RD.LEVEL_REDRAW)
    ok &= _rec("U2e ★ 改依赖顺序 ⇒ T3",
               RD.assess_impact("把评审放在建模之前做", ts)["level"]
               == RD.LEVEL_REDRAW)
    ok &= _rec("U2f ★ T3 结论 action=redraw（与 T2 的 retask 区分）",
               RD.assess_impact("再加一个任务", ts)["action"] == "redraw"
               and RD.assess_impact("第三部分改成表格", ts)["action"] == "retask")

    print("\n--- U2g ★ 点名未命中不得判 T0（否则静默吞掉用户真实修改）---")
    ts2 = [T("t1", [], "done"), T("t2", ["t1"], title="撰写第三部分分析"),
           T("t3", ["t2"])]
    # ① 标题**不含**序数 ⇒ 靠序数指代定位到图中第 3 行（t3，**不是** t2 ——
    #    序数按整张图数，前两行已完成时"第三部分"就是 t3）
    a = RD.assess_impact("第三部分改成表格", ts2)
    ok &= _rec("U2g1 ★ 序数指代按整张图数 ⇒ 命中第 3 行 t3，且判 T2-light",
               a["level"] == RD.LEVEL_LIGHT and a["target_keys"] == ["t3"],
               "level=%s targets=%s" % (a["level"], a["target_keys"]))
    # ② 标题**含**序数且字面能对上 ⇒ 字面证据优先（命中 t2 而非 t3）
    a2 = RD.assess_impact("撰写第三部分分析改成表格", ts2)
    ok &= _rec("U2g2 ★ 字面标题匹配优先于序数指代",
               a2["target_keys"] == ["t2"], str(a2["target_keys"]))
    # ③ 序数指向已完成任务 ⇒ 不返回（走"无法判断"，绝不改 done）
    a3 = RD.assess_impact("第一部分改成表格", [T("t1", [], "done", title="撰写需求分析"),
                                              T("t2", ["t1"]), T("t3", ["t2"])])
    ok &= _rec("U2g3 ★ 序数指向已完成任务 ⇒ 不重跑（不变量 1）",
               "t1" not in (a3["target_keys"] or []), str(a3["target_keys"]))
    # ④ 序数越界 ⇒ 落"无法判断"路径，不猜
    a4 = RD.assess_impact("第九部分改成表格", ts2)
    ok &= _rec("U2g4 ★ 序数超出图范围 ⇒ 不猜（判据不臆造）",
               "t9" not in (a4["target_keys"] or []), str(a4["target_keys"]))

    print("\n--- U2h ★ done 永不重跑（不变量 1 在 S1 通道）---")
    a2 = RD.assess_impact("任务t1改成表格", ts)
    ok &= _rec("U2h1 点名已完成任务 ⇒ T0（不动图）",
               a2["level"] == RD.LEVEL_IGNORE and not a2["target_keys"])
    ok &= _rec("U2h2 ★ 任何级别的 target_keys 都不含 done key",
               all("t1" not in (RD.assess_impact(d, ts)["target_keys"] or [])
                   for d in ("第三部分改成表格", "t1和t2都改成表格", "再加一个任务")))

    print("\n--- U2i 命中任务数超阈值 ⇒ 视同推翻划分 ---")
    many = [T("t%d" % i, [], st=("done" if i == 0 else "planned")) for i in range(8)]
    a3 = RD.assess_impact("所有部分都改成表格形式", many, max_touch=3)
    ok &= _rec("U2i1 8 个未执行任务命中 > 阈值 3 ⇒ T3",
               a3["level"] == RD.LEVEL_REDRAW)

    print("\n--- U3. G10 可解释性（basis 依据必填）---")
    for d in ("第三部分改成表格", "再加一个任务", "辛苦了"):
        aa = RD.assess_impact(d, ts)
        ok &= _rec("U3 「%s」产出非空 basis" % d[:10], bool(aa.get("basis")))
    m = RD.directive_meta(RD.assess_impact("再加一个任务", ts))
    ok &= _rec("U3x ★ directive_meta 键集与 P1/P2 decision_meta 一致",
               all(k in m for k in ("action", "level", "reason", "signals",
                                    "round", "tokens", "latency_ms")))

    print("\n--- retask_context（原地重派载荷）---")
    c = RD.retask_context("原始上下文", "改成表格", level=RD.LEVEL_LIGHT)
    ok &= _rec("U3y ★ 追加而非覆盖（保留用户写过的输入）",
               "原始上下文" in c and "改成表格" in c)
    ok &= _rec("U3z ★ 截断到 1000 字（与 P2 _inject 同上限，避免口径漂移）",
               len(RD.retask_context("x" * 2000, "改成表格")) == 1000)
    return ok


def t_confirm():
    print("\n=== U5. 确认卡（v2 §4.8 / v1 R10）===")
    from services import replan_confirm as CF
    ok = True
    dec = {"action": "replan", "level": "T2-heavy", "reason": "替代 t2",
           "new_tasks": [{"key": "t2_r1"}], "dropped": ["t2"],
           "updates": [], "remap": {"t2": "t2_r1"}}
    p = CF.build_payload(dec, run_id=7, round_no=1)
    ok &= _rec("U5a payload 三动作齐备（批准/维持/补充）",
               len(p["actions"]) == 3
               and {a["id"] for a in p["actions"]} ==
               {CF.ACTION_APPROVE, CF.ACTION_KEEP, CF.ACTION_SUPPLEMENT})
    ok &= _rec("U5b ★ 超时语义显式下发（timeout_behavior=keep_original）",
               p["timeout_behavior"] == "keep_original")
    ok &= _rec("U5c payload 带 diff 三要素（added/dropped/retask）",
               p["added"] == ["t2_r1"] and p["dropped"] == ["t2"])
    ok &= _rec("U5d 默认 TTL=60s（v2 §4.8）", p["ttl_sec"] == 60)

    cid = CF.open_(9, p, ttl_sec=60)
    ok &= _rec("U5e open_ 返回 confirm_id", bool(cid))
    ok &= _rec("U5f get 不消费（可重复查看）",
               bool(CF.get(9)) and bool(CF.get(9)))
    ok &= _rec("U5g ★ 非法 confirm_id ⇒ 查不到（防误批别的卡）",
               CF.get(9, "rr9-99999999") is None)
    r = CF.resolve(9, cid, CF.ACTION_APPROVE)
    ok &= _rec("U5h 批准 ⇒ action=approve", r["action"] == CF.ACTION_APPROVE)
    ok &= _rec("U5i 取走即清空（不重复决议）",
               CF.pending_count(9) == 0
               and CF.resolve(9, None, CF.ACTION_APPROVE)["ok"] is False)
    CF.open_(9, p)
    r2 = CF.resolve(9, None, "乱按的按钮")
    ok &= _rec("U5j ★ 非法动作按『维持原计划』兜底（最保守）",
               r2["action"] == CF.ACTION_KEEP)

    print("\n--- U5k ★ TTL 过期 ⇒ 维持原计划（v1 R10，绝不自动批准）---")
    cid2 = CF.open_(10, p, ttl_sec=0.01)
    ok &= _rec("U5k1 open_ 成功", bool(cid2))
    import time as _T
    _T.sleep(0.05)
    r3 = CF.resolve(10, cid2, CF.ACTION_APPROVE)
    ok &= _rec("U5k2 ★ 过期时忽略 approve ⇒ 维持原计划",
               r3["action"] == CF.ACTION_KEEP and r3["expired"] is True,
               "action=%s" % r3["action"])
    ok &= _rec("U5k3 过期仍返回 item（决策留痕可查）", r3["item"] is not None)

    print("\n--- U5l 阻塞等待不得持锁（死锁防线 + 应答可见性）---")
    # ⚠️ 必须用**真时钟**（`open_` 写 expires 用的是真 time，跨时钟比较会
    #   让假 now 直接越过真 expires ⇒ wait_for 首轮就判超时 ⇒ 测不到等待）。
    #   等待时长取 0.3s，`poll_s=0.05` ⇒ 至少 6 轮，足够在途中应答。
    CF.open_(11, p, ttl_sec=0.3)
    calls = {"n": 0}
    got = {}

    def _real_sleep(_s):
        calls["n"] += 1
        if calls["n"] == 2:
            # ★ 从"另一路"应答。若 wait_for 持锁，这里永远拿不到锁（死锁）。
            got["r"] = CF.resolve(11, None, CF.ACTION_APPROVE)
    r4 = CF.wait_for(11, poll_s=0.05, _sleep=_real_sleep)
    ok &= _rec("U5l1 ★ wait_for 期间 resolve 可成功（无死锁）",
               bool(got.get("r")) and got["r"]["action"] == CF.ACTION_APPROVE,
               "resolve=%s" % got.get("r"))
    ok &= _rec("U5l2 ★ ★ 应答后 wait_for 必须返回 approve"
               "（不能因队列已空而返回 ok=False —— 那会让用户"
               "「点了批准却没重画」）",
               r4["ok"] and r4["action"] == CF.ACTION_APPROVE,
               "action=%s reason=%s" % (r4.get("action"), r4.get("reason")))
    ok &= _rec("U5l3 应答后立即返回（不空等到 TTL）", calls["n"] <= 4,
               "轮询 %d 次" % calls["n"])

    print("\n--- U5n ★ 跨线程竞态（单线程假时钟测不到，行为探针抓到的真 bug）---")
    # ① 竞争窗口：应答在 `wait_for` 取 `_last_seen` **之前**完成
    #    （REST 线程恰好先跑完）⇒ 入口必须先查 `_DECIDED`，否则报"无决策"。
    CF.open_(31, p, ttl_sec=10)
    box31 = {}
    _t31 = threading.Thread(target=lambda: box31.update(
        r=CF.resolve(31, None, CF.ACTION_APPROVE)))
    _t31.start()
    r31 = CF.wait_for(31, poll_s=0.05, _sleep=lambda _s: _t31.join(timeout=1))
    _t31.join(timeout=1)
    ok &= _rec("U5n1 ★ 应答早于 wait_for 取卡 ⇒ 仍取回 approve",
               r31["action"] == CF.ACTION_APPROVE and r31["ok"],
               "action=%s" % r31["action"])
    # ② 等待途中应答
    CF.open_(32, p, ttl_sec=10)
    box32 = {}

    def _ap32():
        _t2.sleep(0.15)
        box32["r"] = CF.resolve(32, None, CF.ACTION_APPROVE)
    _th32 = threading.Thread(target=_ap32)
    _th32.start()
    r32 = CF.wait_for(32, poll_s=0.03)
    _th32.join(timeout=2)
    ok &= _rec("U5n2 ★ 等待途中应答 ⇒ approve", r32["action"] == CF.ACTION_APPROVE,
               "action=%s" % r32["action"])
    # ③ keep / supplement 两态不被吞
    for cid_, act_ in ((33, CF.ACTION_KEEP), (34, CF.ACTION_SUPPLEMENT)):
        CF.open_(cid_, p, ttl_sec=10)
        b_ = {}
        th_ = threading.Thread(target=lambda: b_.update(
            r=CF.resolve(cid_, None, act_)))
        th_.start()
        w_ = CF.wait_for(cid_, poll_s=0.03)
        th_.join(timeout=2)
        ok &= _rec("U5n %s 应答态保真" % act_, w_["action"] == act_,
                   "action=%s" % w_["action"])

    print("\n--- U5m 会话隔离（agent 是模块级单例，挂全局=跨会话泄漏）---")
    CF.open_(20, p)
    CF.open_(21, p)
    ok &= _rec("U5m1 两会话互不可见", CF.get(20)["reason"] != CF.get(21)["reason"]
               or CF.get(20) is not CF.get(21))
    CF.drop(20)
    ok &= _rec("U5m2 drop 只清本会话", CF.get(20) is None and CF.get(21) is not None)
    CF.drop(21)
    return ok


def t_llm():
    print("\n=== U6. T3 LLM 整图重画（默认关 / replace 锁定）===")
    from services import replan_redraw as RR
    from services import replan_directive as RD
    import task_queue
    V = task_queue.TaskQueue.validate_plan
    ok = True
    ts = [T("t1", [], "done"), T("t2", ["t1"], "failed", error="timeout"),
          T("t3", ["t2"])]

    good = json.dumps({"tasks": [{"key": "t2_r1", "title": "B替代",
                                  "deps": ["t1"], "context": "换路径"}]},
                      ensure_ascii=False)

    print("\n--- U4. ★ 零行为变更（默认关）---")
    d0 = RR.plan_llm_redraw(ts, ["t2"], llm_raw=good, enabled=False, validate=V)
    ok &= _rec("U4a llm_redraw 关 ⇒ 动作 none 且不产新任务",
               d0["action"] == "none" and not d0["new_tasks"])

    print("\n--- U6a 正常路径 + 质量门 + deps 重映射 ---")
    d = RR.plan_llm_redraw(ts, ["t2"], llm_raw=good, enabled=True,
                           round_no=1, validate=V)
    ok &= _rec("U6a1 动作 replan / 级别 T3",
               d["action"] == "replan" and d["level"] == "T3")
    ok &= _rec("U6a2 ★ 过 Q1~Q5（同规则版同一质量门）",
               d["gates"].get("ok") is True, str(d["gates"].get("failed")))
    ok &= _rec("U6a3 ★ 下游 deps 重映射 t2 → t2_r1",
               any(k["key"] == "t3" and "t2_r1" in (k["deps"] or [])
                   for k in d["kept"]),
               str([(k["key"], k["deps"]) for k in d["kept"]]))
    ok &= _rec("U6a4 remap 方向是 旧→新（不是恒等）",
               d.get("remap") == {"t2": "t2_r1"}, str(d.get("remap")))
    ok &= _rec("U6a5 failed key 进 dropped", d["dropped"] == ["t2"])

    print("\n--- U6b ★ replace 范围锁定（拍板⑤：insert/skip 留 P4）---")
    ins = json.dumps({"tasks": [{"key": "perf_review", "title": "凭空加的",
                                 "deps": []}]}, ensure_ascii=False)
    d1 = RR.plan_llm_redraw(ts, ["t2"], llm_raw=ins, enabled=True,
                            round_no=1, validate=V)
    ok &= _rec("U6b1 ★ 凭空新增任务 ⇒ T4（不从后门放 insert）",
               d1["action"] == "none" and d1["level"] == "T4",
               "level=%s" % d1["level"])
    mix = json.dumps({"tasks": [{"key": "t2_r1", "title": "替代", "deps": ["t1"]},
                                {"key": "perf_review", "title": "顺手加的",
                                 "deps": []}]}, ensure_ascii=False)
    d2 = RR.plan_llm_redraw(ts, ["t2"], llm_raw=mix, enabled=True,
                            round_no=1, validate=V)
    ok &= _rec("U6b2 ★ 混合输出（1 替代 + 1 新增）⇒ 整个方案作废 T4",
               d2["action"] == "none" and d2["level"] == "T4")

    print("\n--- U6c 输出不可用 ⇒ T4（绝不尽力而为落库）---")
    for name, raw in (("非 JSON", "抱歉我不能"), ("空 tasks", "{}"),
                      ("缺 key", json.dumps({"tasks": [{"title": "无key"}]})),
                      ("deps 非数组",
                       json.dumps({"tasks": [{"key": "t2_r1", "title": "x",
                                              "deps": "t1"}]}))):
        dd = RR.plan_llm_redraw(ts, ["t2"], llm_raw=raw, enabled=True,
                                round_no=1, validate=V)
        ok &= _rec("U6c %s ⇒ 动作 none" % name, dd["action"] == "none",
                   "level=%s reason=%s" % (dd["level"], dd["reason"][:50]))

    print("\n--- U6d 撞车由质量门拦（Q1/Q2）---")
    # ⚠️ 用例必须让**两个任务都带 _rN 后缀**（都算合法替代），否则会先被
    #   replace 范围锁定拦下（更早的防线），根本走不到 Q1/Q2 —— 那样这条
    #   断言就在测别的东西了。
    dup = json.dumps({"tasks": [{"key": "t2_r1", "title": "替代", "deps": ["t1"]},
                                {"key": "t2_r1", "title": "又一个替代",
                                 "deps": ["t1"]}]}, ensure_ascii=False)
    d3 = RR.plan_llm_redraw(ts, ["t2"], llm_raw=dup, enabled=True,
                            round_no=1, validate=V)
    ok &= _rec("U6d ★ 同一输出内 key 重复 ⇒ Q1 拦下",
               d3["action"] == "none"
               and "Q1" in (d3["gates"].get("failed") or []),
               "level=%s failed=%s" % (d3["level"], d3["gates"].get("failed")))

    # 悬空依赖 ⇒ Q5 拦（deps 指向不存在的 key）
    dang = json.dumps({"tasks": [{"key": "t2_r1", "title": "替代",
                                  "deps": ["t_ghost"]}]}, ensure_ascii=False)
    d3b = RR.plan_llm_redraw(ts, ["t2"], llm_raw=dang, enabled=True,
                             round_no=1, validate=V)
    ok &= _rec("U6d2 ★ deps 指向不存在的 key ⇒ Q5 拦下",
               d3b["action"] == "none"
               and "Q5" in (d3b["gates"].get("failed") or []),
               "failed=%s" % d3b["gates"].get("failed"))

    print("\n--- U6e lineage 指向（Q3 判据来源）---")
    nt, perr = RD.parse_llm_plan(good, ["t2"], round_no=1)
    ok &= _rec("U6e1 ★ 正常输出解析出 lineage.from=t2（指向被替代的旧 key）",
               bool(nt) and not perr
               and nt[0]["config"]["lineage"]["from"] == "t2",
               str(nt[0]["config"].get("lineage") if nt else perr))
    nt2, _ = RD.parse_llm_plan(ins, ["t2"], round_no=1)
    ok &= _rec("U6e2 ★ 无法证明是替代 ⇒ **不填** lineage（让 Q3 拦）",
               bool(nt2) and not nt2[0]["config"].get("lineage"),
               str(nt2[0]["config"] if nt2 else ""))

    print("\n--- U6f 轮次上限 ---")
    d4 = RR.plan_llm_redraw(ts, ["t2"], llm_raw=good, enabled=True,
                            round_no=3, max_rounds=1, validate=V)
    ok &= _rec("U6f 轮次超限 ⇒ T4", d4["action"] == "none" and d4["level"] == "T4")

    print("\n--- U6g 提示词必须写死 replace 约束（防「只在文档里约束」）---")
    pr, _ = RD.llm_redraw_prompt("做个系统", ts, directive="改成表格")
    ok &= _rec("U6g1 提示词含 replace 约束与 done 不重跑约束",
               "replace" in pr and "不得重跑" in pr)
    ok &= _rec("U6g2 提示词含 JSON 输出格式（可解析）", "\"tasks\"" in pr)
    return ok


def t_wiring():
    print("\n=== X/S3. 接线正确性 ===")
    ok = True
    st = _read(STREAM_SRC)
    fu = _read(FOLLOWUP_SRC)

    ok &= _rec("X1a ★ DAG 级挂点存在且在迭代边界",
               "_rdir.assess_impact(" in st and "_rdir.should_evaluate(" in st)
    ok &= _rec("X1b ★ 挂点整体在 `user_directive` 闸内（零行为变更）",
               "if _RDIR_OK and _rp_uidir and _rp_enabled and conversation_id:" in st)
    ok &= _rec("X1c ★ 挂点段整体 try/except（观测旁路铁律）",
               _has_bare_except(st, "if _RDIR_OK and _rp_uidir",
                                "ready = TaskQueue.ready_tasks(conn, run_id)"))
    ok &= _rec("X2a ★ DAG 级用 peek_text **只读**、不调 pop（不抢单 Agent 队列）",
               "_fup_dag.peek_text(" in st)
    ok &= _rec("X2b ★ stream.py 内不得出现 `_fup_dag.pop(`",
               "_fup_dag.pop(" not in st)
    ok &= _rec("X2c ★ peek_text 不清队列（followup.py 源码级）",
               "def peek_text(" in fu and "_QUEUES.pop" not in
               fu[fu.index("def peek_text("):fu.index("def peek_text(") + 900])
    ok &= _rec("X3 ★ 原地重派走 UPDATE 不追行（同 key 派发两次的防线）",
               "retry_count=COALESCE(retry_count,0)+1" in st
               and "create_plan" not in
               st[st.index("_d_applied = 0"):
                  st.index("_d_applied = 0") + 1200])
    ok &= _rec("X4a 确认卡挂在重画执行**之前**（wait_for 出现在 _applied 应用之前）",
               "_rcfm.wait_for(" in st
               and st.index("_rcfm.wait_for(") < st.index("_rd_act = \"none\"\n"
                                                         "                                                    _redraw_dec = None"))
    ok &= _rec("X4b ★ 确认卡段在 `user_confirm_redraw` 闸内（默认关=零行为变更）",
               "if _RCFM_OK and _rp_confirm and conversation_id:" in st)
    ok &= _rec("X4c ★ 非 approve ⇒ 判 none（维持原计划，不改图）",
               "!= _rcfm.ACTION_APPROVE" in st)
    ok &= _rec("X5 每次评估（含 T0）都落 span 决策记录",
               "\"directive-decide\"" in st and "_rdir.directive_meta(" in st)
    ok &= _rec("X6 ★ 同指令只评估一次（_directive_seen 去重）",
               "_directive_seen" in st)
    # ★ X6b：T3 的 replace 范围基准必须是**全部非终态任务**，
    #   不能是编排侧"猜"出来的 target_keys —— 否则 LLM 合法替代被判越界，
    #   表现为"llm_redraw 开了但永远 T4"（本轮实施期真 bug）。
    _t3seg = ""
    try:
        _t3seg = st[st.index("_d_fail = []"):st.index("_d_fail = []") + 900]
    except Exception:
        pass
    ok &= _rec("X6b ★ T3 范围基准 = 全部非终态任务（非 target_keys）",
               "_d_fail = []" in st
               and "cancelled" in _t3seg and "canceled" in _t3seg,
               "T3 段未按非终态集合推导 _d_fail")
    ok &= _rec("X6c ★ T3 真的调 LLM（不是只判定不执行）",
               "_rr.plan_llm_redraw(" in st
               and "llm_client.chat(" in _t3seg + st[st.index("_d_fail = []"):
                                                  st.index("_d_fail = []") + 2500])
    ok &= _rec("X6d ★ T3 落库先改下游 deps 再 create_plan（不丢依赖）",
               "UPDATE agent_tasks SET deps=?" in st
               and st.index("UPDATE agent_tasks SET deps=?",
                            st.index("_d_fail = []")) <
               st.index("TaskQueue.create_plan(", st.index("_d_fail = []")))
    # ★ X6e：子任务嵌套编排（conversation_id=0）不得写主会话消息 ——
    #   `messages.conversation_id` 有指向 conversations(id) 的外键，用 0 插入
    #   必然 FOREIGN KEY 失败 ⇒ "编排流式失败，回落单 Agent 直行"，
    #   且子任务编排产出整段丢失（既存缺陷，2026-10-21 修）。
    _msgseg = ""
    try:
        _msgseg = st[st.index("INSERT INTO messages (conversation_id, role"):
                      st.index("INSERT INTO messages (conversation_id, role") + 120]
    except Exception:
        pass
    ok &= _rec("X6e ★ 无效会话号不写主会话消息（子任务嵌套不再触发 FK 失败）",
               "if not conversation_id:" in st
               and "msg_id = 0" in st
               and st.index("if not conversation_id:") < st.index(
                   "INSERT INTO messages (conversation_id, role"),
               "未找到会话号守卫")
    ok &= _rec("X6f ★ 会话号守卫不得 return（否则丢掉 stage/agent/done 事件）",
               "if not conversation_id:" in st
               and not [ln for ln in
                        st[st.index("if not conversation_id:"):
                           st.index("msg_id = 0",
                                    st.index("if not conversation_id:"))].split("\n")
                        if "return" in ln and not ln.strip().startswith("#")],
               "守卫块内出现 return")
    ok &= _rec("X7 可选依赖走导入成功标志位（同 P1/P2 纪律，不 X=None 兜底）",
               "_RDIR_OK = True" in st and "_RCFM_OK = True" in st)
    ok &= _rec("S3 CI 已注册 P3 门禁",
               "verify_replan_p3.py" in _read(CI_YML))
    return ok


def t_mutation():
    print("\n=== M. 变异自证（每个变异都必须被抓，否则断言是空转的）===")
    from services import replan_directive as RD
    from services import replan_confirm as CF
    from services import replan_redraw as RR
    import task_queue
    V = task_queue.TaskQueue.validate_plan
    ok = True
    ts = three_ts()
    good = json.dumps({"tasks": [{"key": "t2_r1", "title": "B替代",
                                  "deps": ["t1"]}]}, ensure_ascii=False)

    # M1：把 T3 判据（新增/删除/改划分）全部删掉 ⇒ U2b~U2e 必须红
    saved = (RD._OVERTURN_NEW, RD._OVERTURN_DROP, RD._OVERTURN_STRUCT)
    try:
        RD._OVERTURN_NEW = RD._OVERTURN_DROP = RD._OVERTURN_STRUCT = ()
        a = RD.assess_impact("再加一个性能评审任务", ts)
        ok &= _rec("M1 ★ 删 T3 判据 ⇒ 「新增任务」不再判 T3（防线确实在）",
                   a["level"] != RD.LEVEL_REDRAW, "level=%s" % a["level"])
    finally:
        (RD._OVERTURN_NEW, RD._OVERTURN_DROP, RD._OVERTURN_STRUCT) = saved

    # M2：把 done 兜底去掉（pending_tasks 不过滤终态）⇒ U1a 必须红
    saved_pt = RD.pending_tasks
    try:
        RD.pending_tasks = lambda tasks: [t for t in (tasks or [])
                                          if isinstance(t, dict) and t.get("task_key")]
        p = RD.pending_tasks(ts)
        ok &= _rec("M2 ★ 去掉终态过滤 ⇒ done 任务进入可改集（防线确实在）",
                   "t1" in [x["task_key"] for x in p])
    finally:
        RD.pending_tasks = saved_pt

    # M3：把"点名未命中 ⇒ 按全部未执行处理"改成 ⇒ T0 ⇒ U2g1 必须红
    #   ⚠️ 变异输入**必须既不含序数也不含可匹配的标题/键**，否则走不到该分支
    #   （序数解析已能兜住"第三部分"这类指代，变异就成了空转 —— 早先版本
    #   正是踩了这个坑：断言"抓到了"，实际改的是另一条路径）。
    src = _read(DIR_SRC)
    mutant = src.replace("targets = [_key(t) for t in pend]\n            if not targets:",
                         "return _mk(base, LEVEL_IGNORE, 'none', [], done_keys,\n"
                         "                       basis=basis, reason='mutant')\n            if not targets:")
    if mutant != src:
        import types
        M = types.ModuleType("mutant_directive")
        try:
            exec(compile(mutant, "mutant", "exec"), M.__dict__)
            # 标题与指令毫无字面关联，也没有序数 ⇒ 真实实现判"按全部未执行处理"
            mts = [T("t1", [], "done", title="需求澄清"),
                   T("t2", ["t1"], title="建模与仿真"),
                   T("t3", ["t2"], title="报告撰写")]
            base_res = RD.assess_impact("表述改得口语一些", mts)
            a = M.assess_impact("表述改得口语一些", mts)
            ok &= _rec("M3a 基线：点名未命中 ⇒ 按未执行处理（不判 T0）",
                       base_res["level"] != "T0",
                       "level=%s" % base_res["level"])
            ok &= _rec("M3 ★ 点名未命中即 T0 ⇒ 用户真实修改被静默吞掉（防线在）",
                       a["level"] == "T0", "level=%s" % a["level"])
        except Exception as e:
            ok &= _rec("M3 变异体构造失败", False, str(e)[:80])
    else:
        ok &= _rec("M3 变异锚点存在", False, "锚点文本未命中（源码已变）")

    # M4：确认卡 TTL 过期改成"沿用请求动作"（自动批准）⇒ U5k2 必须红
    src_c = _read(CONF_SRC)
    mc = src_c.replace('    if expired:\n        return {"ok": True, "action": ACTION_KEEP,',
                       '    if expired:\n        return {"ok": True, "action": a,')
    ok &= _rec("M4a 变异锚点存在（TTL 兜底分支）", mc != src_c)
    if mc != src_c:
        import types
        M2 = types.ModuleType("mutant_confirm")
        M2.__dict__["__name__"] = "mutant_confirm"
        try:
            exec(compile(mc, "mutant", "exec"), M2.__dict__)
            M2.open_(77, {"level": "T2", "reason": "r"}, ttl_sec=0.01)
            import time as _T
            _T.sleep(0.05)
            r = M2.resolve(77, None, M2.ACTION_APPROVE)
            ok &= _rec("M4 ★ TTL 过期沿用 approve ⇒ 静默行使否决权（防线在）",
                       r["action"] == M2.ACTION_APPROVE,
                       "action=%s" % r["action"])
        except Exception as e:
            ok &= _rec("M4 变异体执行失败", False, str(e)[:80])

    # M5：去掉 replace 范围锁定（本地兜底恒放行）⇒ U6b1 必须红
    #   ⚠️ 变异对照必须**分离两层防线**：M5 隔离"replace 锁定"层（用塞假
    #   lineage 的等价变异），M6 隔离"质量门"层（用悬空 deps + 恒放行门）。
    #   早先版本两者都用同一个无后缀 key，结果先被 replace 锁定拦下 ——
    #   门禁看着"抓到了"，实际测的是别的一层（断言空转的典型形态）。
    dts = [T("t1", [], "done"), T("t2", ["t1"], "failed"), T("t3", ["t2"])]
    ins = json.dumps({"tasks": [{"key": "perf_review", "title": "凭空加的",
                                 "deps": []}]}, ensure_ascii=False)
    real = RD.parse_llm_plan

    def _fake_parse(raw, failed_keys, **kw):
        """等价变异：解析层给越界任务**塞一个合法 from**，绕过 replace 锁定。"""
        nt, e = real(raw, failed_keys, **kw)
        for t in nt:
            t.setdefault("config", {})["lineage"] = {"from": "t2"}
        return nt, e
    try:
        d_base = RR.plan_llm_redraw(dts, ["t2"], llm_raw=ins, enabled=True,
                                    round_no=1, validate=V)
        ok &= _rec("M5a 基线：真实实现拦下越界插入（replace 锁定层）",
                   d_base["action"] == "none" and d_base["level"] == "T4")
        d_mut = RR.plan_llm_redraw(dts, ["t2"], llm_raw=ins, enabled=True,
                                   round_no=1, validate=V, parse=_fake_parse)
        ok &= _rec("M5b ★ 塞假 lineage 绕过锁定 ⇒ 越界任务被放行"
                   "（证明 U6b1 拦的正是这一层，不是巧合）",
                   d_mut["action"] == "replan"
                   and "超出 replace 范围" not in d_mut["reason"],
                   "变异体 action=%s" % d_mut["action"])
    except Exception as e:
        ok &= _rec("M5 执行失败", False, str(e)[:80])

    # M6：质量门恒放行 ⇒ 悬空 deps 必被放过（证明 Q5 在门禁里）
    dang = json.dumps({"tasks": [{"key": "t2_r1", "title": "替代",
                                  "deps": ["t_ghost"]}]}, ensure_ascii=False)

    def _all_pass(*a, **k):
        """质量门恒放行变异体。"""
        return {"Q1": True, "Q2": True, "Q3": True, "Q4": True, "Q5": True,
                "failed": [], "ok": True, "reason": "全过"}

    ok &= _rec("M6a 基线：悬空 deps ⇒ Q5 拦下",
               RR.plan_llm_redraw(dts, ["t2"], llm_raw=dang, enabled=True,
                                  round_no=1, validate=V)["action"] == "none")
    saved_g = RR.quality_gates
    try:
        RR.quality_gates = _all_pass
        dd = RR.plan_llm_redraw(dts, ["t2"], llm_raw=dang, enabled=True,
                                round_no=1, validate=V)
        ok &= _rec("M6 ★ 质量门恒放行 ⇒ 悬空依赖被接受（防线在门禁里）",
                   dd["action"] == "replan", "action=%s" % dd["action"])
    finally:
        RR.quality_gates = saved_g

    # M7：源码级 —— 删掉挂点闸（挂点挪到闸外）
    st = _read(STREAM_SRC)
    ok &= _rec("M7a 挂点闸锚点存在", "_rp_uidir and _rp_enabled" in st)
    ok &= _rec("M7b ★ 确认卡闸锚点存在", "_RCFM_OK and _rp_confirm" in st)
    ok &= _rec("M7c ★ 原地重派不走 create_plan（不追行）",
               "create_plan" not in
               st[st.index("_d_applied = 0"):st.index("_d_applied = 0") + 1200])
    return ok


def main():
    print("P3 replanning 门禁（用户指令影响评估 + 确认卡 + T3 LLM 重画，"
          "零 LLM 零服务零生产库写）")
    ok = t_static()
    ok &= t_config()
    ok &= t_unit()
    ok &= t_confirm()
    ok &= t_llm()
    ok &= t_wiring()
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