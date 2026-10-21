# -*- coding: utf-8 -*-
"""P4 replanning（2026-10-21，执行中重规划第四阶段）回归验证 —— 零 LLM、零服务、零生产库写。

**为什么需要它**（设计文档 v2 §7 P4 / §4.4 / §4.6）：P1~P3 的所有触发都是
    **失败后置**的 —— 任务失败了、用户改主意了，才反应。但工程现实里最隐蔽的
    一类失败是**缓慢漂移**：每一步都"成功"，整体却离 goal 越来越远。本阶段补
    三层能力，本门禁钉住它们不退化：

    S  静态契约：健康巡检/早停/三动作/落库四模块入口齐备、纯函数纪律、
        配置双登记、DAG 挂点锚点、前端渲染、CI 注册
    U1 巡检节奏（probe_due）：每 N 任务或每 M 秒取先到者；双 ≤0 ⇒ 永不到期
    U2 M1 partial 密度：窗口 <3 不判（噪声不是信号）；占比 ≥50% 才 hit
    U3 M2 冲突密度：detect_conflicts ≥1 即 hit（强信号，复用既有事实源）
    U4 M4 预算偏离：预计剩余 > 剩余预算 ×120% ⇒ hit；算不出 ≠ 健康（n/a）
    U5 ★ M4（stop）优先于 M1/M2（refine）：时间不够时精修救不了交付
    U6 提前收敛（保守到苛刻）：required 全 done + 零 failed + 无标注不猜；
        汇总记 full 非 partial（主动放弃 ≠ 没做成）
    U7 LLM 巡检输出严格三条（非 JSON/非布尔 match/false 无理由 ⇒ 拒收）
    U8 health_meta 键集与 P1/P2/P3 decision_meta 一致（前端面板不空白）
    U9 ★ P4 三动作解析：replace 前缀配对 / insert 留 lineage 壳 / skip 须理由 /
        allow 外动作整份拒收
    U10 ★ P4 计划层三道防线：skip ⊄ 失效传播集 ⇒ T4；insert 无 directive ⇒ T4；
        Q3 双轨（insert 必须挂指令原文）
    U11 ★ **P3 行为逐字不变**：`allow_actions` 默认 ("replace",) ⇒ insert/skip
        在解析层即被整份拒收；提示词默认输出与 P3 同文
    U12 replan_decisions 表契约：DDL 可真执行、字段齐备、两索引、
        stream.py 写入旁路（异常不阻断编排）
    U13 ★ **零行为变更**：三闸默认关 ⇒ 巡检/早停段整体 no-op
    M  变异自证：去 M4 优先 / 删零 failed 判据 / 放开 allow 外动作 /
        Q3 insert 去 directive 校验 / skip 不从 kept 排除 / 节奏双闸失效
        —— 每个变异都必须被本门禁抓住，否则断言是空转的

**为什么 M1 阈值是 50% 而不是更敏感的值**（真库标定，见《巡检与早停阈值标定
    报告-20261021》）：全库 partial 基线 10.0%，50% = 5 倍远离噪声。设 20%
    会让"每 5 个任务 1 个 partial"这类设计使然的稀疏缺失持续误报巡检
    （Task #43 教训：按需不触发 ≠ 失效，反过来误报 ≠ 健康）。

用法：
    <repo>\\.venv\\Scripts\\python.exe -X utf8 tools/verify/verify_replan_p4.py
退出码：全绿 0 / 有失败或空转 1。
"""
import json
import os
import re
import sqlite3
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

HEALTH_SRC = os.path.join(ROOT, "services", "replan_health.py")
DIR_SRC = os.path.join(ROOT, "services", "replan_directive.py")
REDRAW_SRC = os.path.join(ROOT, "services", "replan_redraw.py")
SCHEMA_SRC = os.path.join(ROOT, "database", "schema.py")
STREAM_SRC = os.path.join(ROOT, "agent", "pipeline_parts", "stream.py")
CONFIG_SRC = os.path.join(ROOT, "core", "config.py")
JS_SRC = os.path.join(ROOT, "static", "js", "mods", "11-pipeline.js")
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


def T(k, deps=None, st="planned", **kw):
    """任务行工厂（与 P3 门禁同款形状：deps/config 为 JSON 字符串）。"""
    d = {"id": abs(hash(k)) % 10000, "task_key": k,
         "deps": json.dumps(deps or []), "status": st, "title": "任务" + k,
         "agent_id": "design", "seq": 0, "context": "原始上下文",
         "expected_output": "", "config": "{}", "error": "", "error_class": ""}
    d.update(kw)
    return d


def four_ts():
    """标准四段图：t1 done、t2 failed、t3/t4 planned（t3←t2←t1，t4←t3）。

    失效传播集 = {t2, t3, t4}（t1 是上游，**不在**集内 —— skip 越界用例的基准）。
    """
    return [T("t1", [], "done"), T("t2", ["t1"], "failed"),
            T("t3", ["t2"]), T("t4", ["t3"])]


def done_item(k, summary):
    return {"task_key": k, "summary": summary}


# ══════════════════════════════════════════════════════════════════════════
def t_static():
    print("\n=== S. 静态契约 ===")
    from services import replan_health as RH
    from services import replan_redraw as RR
    from services import replan_directive as RD
    ok = True

    # ── S1 模块入口 ──
    ok &= _rec("S1a replan_health.py 存在且九个入口齐备",
               all(hasattr(RH, n) for n in (
                   "probe_due", "partial_density", "conflict_density",
                   "budget_deviation", "health_probe", "early_stop_plan",
                   "llm_health_prompt", "parse_llm_health", "health_meta")))
    hsrc = _read(HEALTH_SRC)
    ok &= _rec("S1b ★ 纯函数纪律：health 模块无 sqlite/requests/get_db/open(",
               not any(x in hsrc for x in
                       ("import sqlite", "import requests", "get_db", "open(")))
    ok &= _rec("S1c M1~M4 常量按真库标定值（M1=50%且N≥3 / M4=120% / 预算1800s）",
               RH.M1_RATIO == 0.5 and RH.M1_MIN_WINDOW == 3
               and RH.M4_RATIO == 1.2 and RH.DEFAULT_TIME_BUDGET_SEC == 1800)
    ok &= _rec("S1d ★ LEVEL_LIGHT 与 replan_redraw 同串（跨模块一致，"
               "否则巡检精修会发出规则版不认的级别）",
               RH.LEVEL_LIGHT == RR.LEVEL_LIGHT == "T2-light")
    ok &= _rec("S1e 三动作常量齐备（replace/skip/insert + 全集）",
               (RD.ACTION_REPLACE, RD.ACTION_SKIP, RD.ACTION_INSERT)
               == ("replace", "skip", "insert")
               and RD.ALL_LLM_ACTIONS == ("replace", "skip", "insert"))
    ok &= _rec("S1f ★ P3 旧解析函数逐字保留（P3 门禁 124 项按它断言，"
               "改签名会让 P3 回归一夜全红）",
               hasattr(RD, "parse_llm_plan") and hasattr(RD,
                                                         "parse_llm_plan_actions"))
    rsrc = _read(REDRAW_SRC)
    ok &= _rec("S1g quality_gates 有 insert_ok 参数（Q3 双轨）"
               "且 plan_llm_redraw 有 allow_actions",
               "insert_ok=False" in rsrc and "allow_actions=(\"replace\",)"
               in rsrc)

    # ── S2 replan_decisions 表 ──
    ssrc = _read(SCHEMA_SRC)
    ok &= _rec("S2a replan_decisions DDL 在 schema.py（真库 SQL 聚合的事实源）",
               "CREATE TABLE IF NOT EXISTS replan_decisions" in ssrc)
    ok &= _rec("S2b 两索引齐备（会话维度 + 动作维度）",
               "idx_rpd_conv_run" in ssrc and "idx_rpd_action" in ssrc)

    # ── S3 前端渲染 ──
    jsrc = _read(JS_SRC)
    ok &= _rec("S3a ★ 修订卡渲染「跳过 skipped」清单（P4 新字段有落点）",
               "ev.skipped||[]" in jsrc and "sec('跳过'" in jsrc)
    ok &= _rec("S3b ★ 修订卡渲染「插入 inserted」清单",
               "ev.inserted||[]" in jsrc and "sec('插入'" in jsrc)
    ok &= _rec("S3c ★ early_stop 用人话徽章（不把内部级别号 T0 暴露给用户）",
               "ev.action === 'early_stop'" in jsrc
               and "提前收敛（目标已达成）" in jsrc)
    ok &= _rec("S3d ★ 修订卡接 T3 路径的 added 字段（否则 LLM 重画的"
               "「新增」清单在前端是空的）",
               "ev.new_tasks||ev.added||[]" in jsrc)
    ok &= _rec("S3e ★ skipped 子任务卡有专属渲染（灰徽章，不与失败混色）",
               "const skipSt = s.status==='skipped';" in jsrc
               and '<span class="st g">skipped</span>' in jsrc
               and "⊘ 主动放弃" in jsrc)
    ok &= _rec("S3f 计划清单/DAG 认 skipped 状态（d-skip 灰虚线节点）",
               "n.status==='skipped'?'d-skip'" in jsrc
               and ".d-skip" in jsrc)

    # ── S4 stream.py 挂点 ──
    st = _read(STREAM_SRC)
    ok &= _rec("S4a ★ 巡检/早停挂点在迭代边界且在总闸+分闸内"
               "（默认全关 ⇒ 整体 no-op）",
               "_RH_OK and _rp_enabled and (_rp_hp or _rp_early)" in st)
    ok &= _rec("S4b ★ 挂点段整体 try/except（观测旁路铁律：异常只丢记录）",
               "except Exception:" in st
               and st[st.index("_RH_OK and _rp_enabled"):].split("ready = "
                       "TaskQueue.ready_tasks")[0].count("except Exception:") >= 2)
    ok &= _rec("S4c ★ early_stop 先于巡检（它可能直接终结循环，"
               "顺序反了会先精修再收敛，多一次无谓 UPDATE）",
               st.index("_rh.early_stop_plan(") < st.index("_rh.probe_due("))
    ok &= _rec("S4d ★ M4 stop ⇒ block 剩余并 break（不静默继续烧预算）",
               "_m4_stopped = True" in st)
    ok &= _rec("S4e ★ 提前收敛的 cancel 只动 planned/ready（终态不碰）",
               'in ("planned", "ready")' in st)
    ok &= _rec("S4f ★ skipped 不算缺失（early_stop 的设计核心："
               "主动放弃 ≠ 没做成）",
               'not in ("done", "skipped")' in st)
    ok &= _rec("S4g ★ T3 路径透传 allow_actions（P4 三动作接线点）",
               st.count("allow_actions=_rp_llm_acts") >= 2)
    ok &= _rec("S4h ★ 决策落库 _write_decision 旁路（异常不阻断编排）",
               "def _write_decision(" in st
               and "INSERT INTO replan_decisions" in st)
    ok &= _rec("S4i ★ 执行事实标注在反思修订后**幂等重追加**"
               "（reflection 整体重写 orch_content，不补回就把"
               "「主动放弃≠没做成」的可见性弄丢 —— 2026-10-21 探针 C7 实测）",
               'if _early_stopped and "目标已达成提前收敛" not in '
               'orch_content:' in st
               and 'if _m4_stopped and "健康巡检" not in orch_content:' in st)

    # ── S5 配置双登记 ──
    csrc = _read(CONFIG_SRC)
    keys7 = ["health_probe", "health_probe_llm", "early_stop",
             "probe_every_tasks", "probe_every_sec", "probe_time_budget_sec",
             "llm_actions"]
    ok &= _rec("S5a P4 七键全部出现在 DEFAULT_CONFIG",
               all(('"%s"' % k) in csrc for k in keys7))
    ok &= _rec("S5b ★ P4 七键全部登记进 CONFIG_SCHEMA（可被设置页/REST 修改）",
               all(('"%s"' % k) in csrc[csrc.index("CONFIG_SCHEMA"):]
                   for k in keys7))
    ok &= _rec("S5c ★ 三闸默认关（health_probe/health_probe_llm/early_stop "
               "皆 False）+ llm_actions 默认锁 replace",
               '"health_probe": False' in csrc
               and '"health_probe_llm": False' in csrc
               and '"early_stop": False' in csrc
               and '"llm_actions": "replace"' in csrc)
    return ok


def t_config_runtime():
    print("\n=== S6. 运行期默认值 ===")
    from core.config import get as _get
    ok = True
    ok &= _rec("S6a health_probe 运行期默认 False（零行为变更第一闸）",
               _get("replanning", "health_probe", True) is False)
    ok &= _rec("S6b health_probe_llm 运行期默认 False",
               _get("replanning", "health_probe_llm", True) is False)
    ok &= _rec("S6c early_stop 运行期默认 False",
               _get("replanning", "early_stop", True) is False)
    ok &= _rec("S6d llm_actions 运行期默认 replace（P3 行为）",
               _get("replanning", "llm_actions", "skip,insert") == "replace")
    ok &= _rec("S6e 巡检节奏默认 3 任务 / 60 秒 / 预算 1800s",
               _get("replanning", "probe_every_tasks", 0) == 3
               and _get("replanning", "probe_every_sec", 0) == 60
               and _get("replanning", "probe_time_budget_sec", 0) == 1800)
    return ok


# ══════════════════════════════════════════════════════════════════════════
def t_health():
    print("\n=== U1~U8. 健康巡检逐格 ===")
    from services import replan_health as RH
    ok = True

    # ── U1 巡检节奏 ──
    ok &= _rec("U1a 默认参数：1 个任务完成不到期（等 3 个或 60 秒）",
               RH.probe_due(1, 5) is False)
    ok &= _rec("U1b 任务数到点（3 个）⇒ 到期（不等秒数）",
               RH.probe_due(3, 5) is True)
    ok &= _rec("U1c 秒数到点（60s）⇒ 到期（任务数先到者）",
               RH.probe_due(0, 60) is True)
    ok &= _rec("U1d 秒数超过（61s）⇒ 到期", RH.probe_due(0, 61) is True)
    ok &= _rec("U1e ★ 两个上限都 ≤0 ⇒ 永不到期（等价关节奏；"
               "防「配置写 0 想关掉」结果变成「每次必检」）",
               RH.probe_due(99, 9999, every_tasks=0, every_sec=0) is False)
    ok &= _rec("U1f 单边 ≤0 另一边仍生效（tasks=0 只看秒数）",
               RH.probe_due(99, 61, every_tasks=0, every_sec=60) is True
               and RH.probe_due(99, 59, every_tasks=0, every_sec=60) is False)
    ok &= _rec("U1g 负值/缺省不崩（配置漂移防御）",
               RH.probe_due(-1, -1, every_tasks=-5, every_sec=-5) is False)

    # ── U2 M1 partial 密度 ──
    full = done_item("a", {"status": "full"})
    part = done_item("b", {"status": "partial"})
    miss = done_item("c", {"status": "full", "missing": ["接口文档"]})
    ok &= _rec("U2a 窗口 <3 ⇒ 不 hit（2 个任务 1 个 partial 是噪声不是信号）",
               RH.partial_density([full, part], window=3)["hit"] is False)
    ok &= _rec("U2b 窗口内 3/3 partial ⇒ hit",
               RH.partial_density([part, part, part], window=3)["hit"] is True)
    ok &= _rec("U2c 2/3 partial（66% ≥ 50%）⇒ hit",
               RH.partial_density([full, part, part], window=3)["hit"] is True)
    ok &= _rec("U2d 1/3 partial（33% < 50%）⇒ 不 hit",
               RH.partial_density([full, full, part], window=3)["hit"] is False)
    ok &= _rec("U2e ★ missing 非空也算「自报缺东西」（口径同 S5，不另造判据）",
               RH.partial_density([full, miss, miss], window=3)["hit"] is True)
    ok &= _rec("U2f ★ 窗口取**最近** N 个（老 partial 不替现在的状态背锅）",
               RH.partial_density([part, part, part, full, full, full],
                                  window=3)["hit"] is False)
    ok &= _rec("U2g 空输入不崩且不 hit",
               RH.partial_density([], window=3)["hit"] is False)

    # ── U3 M2 冲突密度 ──
    ok &= _rec("U3a 无 claims ⇒ 0 冲突不 hit",
               RH.conflict_density([full, full])["hit"] is False)
    conf = [
        done_item("x", {"status": "partial", "claims": [
            {"subject": "卫星链路预算", "text": "支持卫星链路预算扩容",
             "kind": "fact"}]}),
        done_item("y", {"status": "partial", "claims": [
            {"subject": "卫星链路预算", "text": "不支持卫星链路预算扩容",
             "kind": "fact"}]}),
    ]
    m2 = RH.conflict_density(conf)
    ok &= _rec("U3b ★ 同一对象相反断言 ⇒ ≥1 即 hit（强信号，无解决跟踪 ⇒ "
               "一律按未解决，保守）",
               m2["hit"] is True and m2["conflicts"] >= 1,
               str(m2)[:120])
    ok &= _rec("U3c 冲突归因带 subject（可解释）",
               bool(m2.get("subjects")))

    # ── U4 M4 预算偏离 ──
    ok &= _rec("U4a 无耗时样本 ⇒ n/a（算不出 ≠ 健康）",
               RH.budget_deviation([], 3, 100)["hit"] is False
               and RH.budget_deviation([], 3, 100)["ratio"] is None)
    ok &= _rec("U4b 无剩余任务 ⇒ n/a",
               RH.budget_deviation([60000], 0, 100)["hit"] is False)
    ok &= _rec("U4c 预算 ≤0 ⇒ n/a（配置写 0 表示不判）",
               RH.budget_deviation([60000], 3, 0)["hit"] is False)
    ok &= _rec("U4d 3 任务 ×60s=180s vs 预算 100s（180%>120%）⇒ hit",
               RH.budget_deviation([60000, 60000, 60000], 3, 100)["hit"] is True)
    ok &= _rec("U4e 恰好 120% 边界 ⇒ 不 hit（严格大于，防边界抖动误报）",
               RH.budget_deviation([40000, 40000], 3, 100)["hit"] is False)
    ok &= _rec("U4f 119% ⇒ 不 hit",
               RH.budget_deviation([39800, 39800], 3, 100)["hit"] is False)

    # ── U5 health_probe 主判定 ──
    ok &= _rec("U5a ★ enabled=False ⇒ 恒 none（零行为变更）",
               RH.health_probe([part], [], enabled=False)["action"] == "none")
    ok &= _rec("U5b 无已完成任务 ⇒ 无巡检对象",
               RH.health_probe([], [], enabled=True)["action"] == "none")
    # M4 命中：剩余 3 任务 ×60s = 180s > 100s×1.2
    d_m4 = RH.health_probe([full, full, full], four_ts(), enabled=True,
                           latencies_ms=[60000, 60000, 60000],
                           remaining_tasks=3, budget_left_sec=100)
    ok &= _rec("U5c ★ M4 命中 ⇒ action=stop（S9 语义：该停，不重画）",
               d_m4["action"] == "stop", "action=%s" % d_m4["action"])
    # M1+M2+M4 同时命中 ⇒ stop 优先（时间不够时精修救不了交付）
    d_both = RH.health_probe([part, part, part] + conf, four_ts(),
                             enabled=True,
                             latencies_ms=[60000, 60000, 60000],
                             remaining_tasks=3, budget_left_sec=100)
    ok &= _rec("U5d ★★ M1/M2/M4 同时超阈 ⇒ stop 优先于 refine"
               "（本门禁的核心断言之一）",
               d_both["action"] == "stop", "action=%s" % d_both["action"])
    d_m1 = RH.health_probe([full, part, part], four_ts(), enabled=True,
                           latencies_ms=[1000], remaining_tasks=1,
                           budget_left_sec=9999)
    ok &= _rec("U5e M1 超阈（无 M4）⇒ action=refine + level=T2-light",
               d_m1["action"] == "refine" and d_m1["level"] == "T2-light",
               "%s/%s" % (d_m1["action"], d_m1["level"]))
    d_m2 = RH.health_probe(conf, four_ts(), enabled=True,
                           latencies_ms=[1000], remaining_tasks=1,
                           budget_left_sec=9999)
    ok &= _rec("U5f M2 超阈（无 M1/M4）⇒ action=refine",
               d_m2["action"] == "refine")
    d_ok = RH.health_probe([full, full, full], four_ts(), enabled=True,
                           latencies_ms=[1000], remaining_tasks=1,
                           budget_left_sec=9999)
    ok &= _rec("U5g 全不超阈 ⇒ none（计划仍健康）",
               d_ok["action"] == "none" and d_ok["level"] == "T0")
    ok &= _rec("U5h metrics 键集含 M1/M2/M4（阈值调参排障靠原始值）",
               set(d_ok.get("metrics") or {}) == {"M1_partial", "M2_conflict",
                                                  "M4_budget"})
    d_llm = RH.health_probe([full, full, full], four_ts(), enabled=True,
                            latencies_ms=[1000], remaining_tasks=1,
                            budget_left_sec=9999,
                            llm_verdict={"match": False, "reason": "跑偏"})
    ok &= _rec("U5i ★ LLM 巡检判不匹配 ⇒ refine（规则 + LLM 双通道）",
               d_llm["action"] == "refine")
    d_llm2 = RH.health_probe([full, full, full], four_ts(), enabled=True,
                             latencies_ms=[1000], remaining_tasks=1,
                             budget_left_sec=9999,
                             llm_verdict={"match": True, "reason": ""})
    ok &= _rec("U5j LLM 巡检判匹配 ⇒ 不触发（信息不足时 LLM 宁可不报）",
               d_llm2["action"] == "none")

    # ── U6 提前收敛 ──
    req = json.dumps({"required": True})
    ts_all_done = [T("t1", [], "done", config=req),
                   T("t2", ["t1"], "planned"),
                   T("t3", ["t2"], "planned")]
    ok &= _rec("U6a enabled=False ⇒ 恒 none（零行为变更）",
               RH.early_stop_plan(ts_all_done, enabled=False)["action"] == "none")
    ts_no_req = [T("t1", [], "done"), T("t2", ["t1"], "planned")]
    ok &= _rec("U6b ★ 无 required 标注 ⇒ 不猜目标边界（没有标注就没有判据）",
               RH.early_stop_plan(ts_no_req, enabled=True)["action"] == "none")
    ts_req_pending = [T("t1", [], "planned", config=req),
                      T("t2", ["t1"], "planned")]
    ok &= _rec("U6c required 未全 done ⇒ 目标未达成，不收敛",
               RH.early_stop_plan(ts_req_pending, enabled=True)["action"]
               == "none")
    ts_req_failed = [T("t1", [], "done", config=req),
                     T("t2", ["t1"], "planned"),
                     T("t9", [], "failed")]
    ok &= _rec("U6d ★ 存在 failed ⇒ 不提前收敛（失败走 T4/blocked，"
               "不与「目标达成」混流）",
               RH.early_stop_plan(ts_req_failed, enabled=True)["action"]
               == "none")
    d_es = RH.early_stop_plan(ts_all_done, enabled=True)
    ok &= _rec("U6e ★ required 全 done + 零 failed + 有非必需剩余 ⇒ early_stop",
               d_es["action"] == "early_stop"
               and d_es["cancel_keys"] == ["t2", "t3"],
               str(d_es.get("cancel_keys")))
    ok &= _rec("U6f cancel_keys 绝不含 required 任务（收口只放弃非必需）",
               "t1" not in d_es.get("cancel_keys", []))
    ts_all_req = [T("t1", [], "done", config=req),
                  T("t2", ["t1"], "planned", config=req)]
    ok &= _rec("U6g 剩余全 required ⇒ 无需收敛（cancel 集会空）",
               RH.early_stop_plan(ts_all_req, enabled=True)["action"] == "none")
    ts_cfg_str = [T("t1", [], "done", config=req),
                  T("t2", ["t1"], "planned", config='{"required": false}')]
    ok &= _rec("U6h config 为 JSON 字符串也认（库里存的就是字符串形态；"
               "t1 required 走字符串解析 ⇒ t2 可被收敛）",
               RH.early_stop_plan(ts_cfg_str, enabled=True)["cancel_keys"]
               == ["t2"])
    ok &= _rec("U6i required 但 status=cancelled（单/双 L）不算 done"
               " ⇒ 不收敛（终态≠达成）",
               RH.early_stop_plan([T("t1", [], "cancelled", config=req),
                                   T("t2", ["t1"], "planned")],
                                  enabled=True)["action"] == "none")

    # ── U7 LLM 巡检输出解析 ──
    ok &= _rec("U7a 非 JSON ⇒ error",
               RH.parse_llm_health("不是 JSON")[1] != "")
    ok &= _rec("U7b match 非布尔 ⇒ error",
               RH.parse_llm_health('{"match": "yes"}')[1] != "")
    ok &= _rec("U7c ★ match=false 但无理由 ⇒ 拒收（不可解释的信号不用）",
               RH.parse_llm_health('{"match": false}')[1] != "")
    v7d, e7d = RH.parse_llm_health(
        '{"match": false, "reason": "剩余任务达不成目标"}')
    ok &= _rec("U7d match=false 有理由 ⇒ 接受",
               e7d == "" and v7d["match"] is False)
    v7e, e7e = RH.parse_llm_health('{"match": true}')
    ok &= _rec("U7e match=true 无理由 ⇒ 接受（宁可漏报的保守侧）",
               e7e == "" and v7e["match"] is True)

    # ── U8 提示词与 meta ──
    pr, hint = RH.llm_health_prompt("建成卫星仿真系统", four_ts(),
                                    done_summaries=["结论甲", "结论乙"],
                                    directive="把评审也加上")
    ok &= _rec("U8a 巡检提示词含目标/任务图/已完成摘要/指令四要素",
               all(x in pr for x in ("建成卫星仿真系统", "t2", "结论甲",
                                     "把评审也加上")))
    ok &= _rec("U8b schema_hint 要求 match 布尔（可校验）",
               hint.get("match") == "bool")
    meta = RH.health_meta(d_m1)
    from services.replan_signal import decision_meta as _dm
    base = _dm(d_m1)
    ok &= _rec("U8c ★ health_meta 键集 ⊇ decision_meta 键集"
               "（前端 trace 面板按这套键渲染，另起一套会「什么都没有」）",
               set(base).issubset(set(meta)), str(set(base) - set(meta)))
    ok &= _rec("U8d metrics 挂载在 meta 上（M1/M2/M4 原始值可查）",
               "metrics" in meta)
    return ok


# ══════════════════════════════════════════════════════════════════════════
def t_actions():
    print("\n=== U9~U11. LLM 三动作（解析层 + 计划层 + 质量门）===")
    from services import replan_directive as RD
    from services import replan_redraw as RR
    import task_queue
    V = task_queue.TaskQueue.validate_plan
    ok = True
    ts = four_ts()

    three_raw = json.dumps({"tasks": [
        {"action": "replace", "key": "t2_r1", "title": "B替代", "deps": ["t1"]},
        {"action": "insert", "key": "t9", "title": "补充评审", "deps": ["t1"]},
        {"action": "skip", "key": "t3", "reason": "产出对下游非关键"},
    ]}, ensure_ascii=False)

    # ── U9 解析层 ──
    nt, sk, err = RD.parse_llm_plan_actions(three_raw, ["t2"], allow=("replace",
                                            "skip", "insert"))
    ok &= _rec("U9a 三动作混合解析：2 新任务 + 1 skip，无错误",
               err == "" and len(nt) == 2 and len(sk) == 1,
               "err=%s nt=%d sk=%d" % (err[:40], len(nt), len(sk)))
    ok &= _rec("U9b ★ replace 前缀配对：t2_r1 ⇒ lineage.from=t2",
               any(t["key"] == "t2_r1"
                   and (t.get("config") or {}).get("lineage", {}).get("from")
                   == "t2" for t in nt))
    ok &= _rec("U9c ★ insert 留 lineage 壳（action=insert，directive 由"
               "计划层填）",
               any(t["key"] == "t9"
                   and (t.get("config") or {}).get("lineage", {}).get("action")
                   == "insert" for t in nt))
    ok &= _rec("U9d skip 条目带 key+reason",
               bool(sk) and sk[0]["key"] == "t3" and bool(sk[0]["reason"]))
    _, _, e_allow = RD.parse_llm_plan_actions(three_raw, ["t2"])
    ok &= _rec("U9e ★ 默认 allow=replace：insert/skip ⇒ **整份拒收**"
               "（P3 行为逐字保持，提示词约束失效时不「尽力而为」）",
               e_allow != "" and "整份拒收" in e_allow, e_allow[:60])
    _, _, e_skip = RD.parse_llm_plan_actions(
        json.dumps({"tasks": [{"action": "skip", "key": "t3"}]}),
        ["t2"], allow=("replace", "skip"))
    ok &= _rec("U9f ★ skip 无理由 ⇒ 拒（没有理由的 skip 不可解释）",
               e_skip != "" and "理由" in e_skip)
    _, _, e_ins = RD.parse_llm_plan_actions(
        json.dumps({"tasks": [{"action": "insert", "title": "无 key"}]}),
        ["t2"], allow=("replace", "insert"))
    ok &= _rec("U9g insert 缺 key/title ⇒ 拒", e_ins != "")
    nt_p3, sk_p3, e_p3 = RD.parse_llm_plan_actions(
        json.dumps({"tasks": [{"key": "t2_r1", "title": "无 action 字段",
                               "deps": []}]}), ["t2"])
    ok &= _rec("U9h action 缺省按 replace（P3 时代输出向后兼容）",
               e_p3 == "" and len(nt_p3) == 1 and not sk_p3
               and (nt_p3[0].get("config") or {}).get("lineage", {}).get(
                   "from") == "t2")
    _, _, e_bad = RD.parse_llm_plan_actions("非 JSON", ["t2"])
    ok &= _rec("U9i 非 JSON ⇒ error（解析层就拒，不给质量门背锅）",
               e_bad != "")

    # ── U10 计划层三道防线 ──
    d3 = RR.plan_llm_redraw(ts, ["t2"], llm_raw=three_raw, enabled=True,
                            directive="把性能评审也加上", round_no=1,
                            validate=V, allow_actions=("replace", "skip",
                                                       "insert"))
    ok &= _rec("U10a 三动作全合法 ⇒ action=replan / level=T3",
               d3["action"] == "replan" and d3["level"] == "T3",
               "%s/%s" % (d3["action"], d3["level"]))
    ok &= _rec("U10b skip 清单进 decision（stream.py 走同一套应用分支）",
               d3.get("skip") == ["t3"], str(d3.get("skip")))
    ok &= _rec("U10c ★ 被 skip 的任务**不在 kept**（否则 Q5 误杀 + "
               "cancelled 行被重复派发）",
               "t3" not in [t.get("task_key") or t.get("key")
                            for t in d3.get("kept") or []])
    ok &= _rec("U10d ★ insert 的 lineage.directive 被填上指令原文"
               "（每个新任务都要证明自己为什么存在）",
               any(t["key"] == "t9"
                   and (t.get("config") or {}).get("lineage", {}).get(
                       "directive") == "把性能评审也加上"
                   for t in d3.get("new_tasks") or []))
    ok &= _rec("U10e 下游 deps 里指向被 skip 的边被移除（t4 不再等 t3）",
               all("t3" not in (t.get("deps") or [])
                   for t in d3.get("kept") or []))
    # skip 越界：t1 是 done 上游，不在失效传播集 {t2,t3,t4}
    d_bad_skip = RR.plan_llm_redraw(ts, ["t2"], llm_raw=json.dumps({"tasks": [
        {"action": "skip", "key": "t1", "reason": "不需要了"}]}),
        enabled=True, directive="d", round_no=1, validate=V,
        allow_actions=("replace", "skip", "insert"))
    ok &= _rec("U10f ★ skip 越界（失效传播集之外）⇒ T4"
               "（跳过无人失败的上游 = 静默抽掉下游输入前提）",
               d_bad_skip["action"] == "none" and d_bad_skip["level"] == "T4"
               and "传播集" in d_bad_skip["reason"],
               d_bad_skip["reason"][:60])
    # insert 无 directive
    d_no_dir = RR.plan_llm_redraw(ts, ["t2"], llm_raw=json.dumps({"tasks": [
        {"action": "insert", "key": "t9", "title": "补充", "deps": []}]}),
        enabled=True, directive="", round_no=1, validate=V,
        allow_actions=("replace", "skip", "insert"))
    ok &= _rec("U10g ★ insert 无指令原文 ⇒ T4（没有指令依据的新任务 = "
               "借机塞私货）",
               d_no_dir["action"] == "none" and "指令原文" in d_no_dir["reason"],
               d_no_dir["reason"][:60])
    # skip-only 合法
    d_skip_only = RR.plan_llm_redraw(ts, ["t2"], llm_raw=json.dumps({"tasks": [
        {"action": "skip", "key": "t3", "reason": "非关键"}]}),
        enabled=True, directive="d", round_no=1, validate=V,
        allow_actions=("replace", "skip", "insert"))
    ok &= _rec("U10h skip-only（无新任务）也合法 ⇒ replan（删任务本身"
               "可以是一次完整重画）",
               d_skip_only["action"] == "replan"
               and d_skip_only.get("skip") == ["t3"])

    # ── U11 P3 行为逐字不变 ──
    d_p3 = RR.plan_llm_redraw(ts, ["t2"], llm_raw=three_raw, enabled=True,
                              directive="把性能评审也加上", round_no=1,
                              validate=V)
    ok &= _rec("U11a ★ 默认 allow=replace 时，含 insert/skip 的输出"
               "整份 T4（P3 行为不变）",
               d_p3["action"] == "none" and d_p3["level"] == "T4",
               d_p3["reason"][:60])
    from services.replan_directive import llm_redraw_prompt as _lrp
    pr_def, _ = _lrp("目标", ts)
    pr_p3, _ = _lrp("目标", ts, allow_actions=("replace",))
    ok &= _rec("U11b ★ 提示词默认输出与 allow=replace 逐字一致"
               "（P3 提示词零漂移）", pr_def == pr_p3)
    ok &= _rec("U11c ★ 默认提示词**不含** skip/insert 约束段"
               "（锁 replace 时连提都不许提）",
               "允许 skip" not in pr_def and "允许 insert" not in pr_def)
    pr_all, _ = _lrp("目标", ts, allow_actions=("replace", "skip", "insert"))
    ok &= _rec("U11d 放开后提示词才写 skip/insert 约束（含各自纪律）",
               "允许 skip" in pr_all and "允许 insert" in pr_all
               and "每个 skip 必须给理由" in pr_all)

    # ── U11e Q3 双轨（直接调 quality_gates 逐格）──
    def _nt(key, lin):
        return {"key": key, "title": "t", "deps": [], "agent": "design",
                "config": {"lineage": lin}}

    ins_nt = _nt("t9", {"action": "insert", "directive": "指令原文"})
    ins_nd = _nt("t9", {"action": "insert", "directive": ""})
    rep_nt = _nt("t2_r1", {"from": "t2"})
    rep_bad = _nt("t2_r1", {"from": "t1"})
    ok &= _rec("U11e1 insert + directive + insert_ok=True ⇒ Q3 过",
               RR.quality_gates([], [ins_nt], [], failed_keys={"t2"},
                                insert_ok=True)["Q3"] is True)
    ok &= _rec("U11e2 ★ insert 无 directive ⇒ Q3 拦（insert_ok=True 也拦）",
               RR.quality_gates([], [ins_nd], [], failed_keys={"t2"},
                                insert_ok=True)["Q3"] is False)
    ok &= _rec("U11e3 ★ insert_ok=False（P3 口径）下 insert 无 from ⇒ Q3 拦",
               RR.quality_gates([], [ins_nt], [], failed_keys={"t2"},
                                insert_ok=False)["Q3"] is False)
    ok &= _rec("U11e4 replace from ∈ failed ⇒ Q3 过（原判据不变）",
               RR.quality_gates([], [rep_nt], [], failed_keys={"t2"},
                                insert_ok=True)["Q3"] is True)
    ok &= _rec("U11e5 replace from ∉ failed ⇒ Q3 拦（塞私货后门仍关着）",
               RR.quality_gates([], [rep_bad], [], failed_keys={"t2"},
                                insert_ok=True)["Q3"] is False)
    return ok


# ══════════════════════════════════════════════════════════════════════════
def t_decisions_table():
    print("\n=== U12. replan_decisions 表契约 ===")
    ssrc = _read(SCHEMA_SRC)
    ok = True
    m = re.search(r"CREATE TABLE IF NOT EXISTS replan_decisions \((.*?)\)\"\"\"",
                  ssrc, re.S)
    ok &= _rec("U12a DDL 可抽取", m is not None)
    if m:
        ddl = "CREATE TABLE IF NOT EXISTS replan_decisions (%s)" % m.group(1)
        try:
            conn = sqlite3.connect(":memory:")
            conn.executescript(ddl)
            cols = [r[1] for r in
                    conn.execute("PRAGMA table_info(replan_decisions)")]
            idx = [r[1] for r in conn.execute(
                "PRAGMA index_list(replan_decisions)")]
            conn.execute(
                "INSERT INTO replan_decisions (conversation_id, run_id, "
                "source, round, signals, level, action, reason, gates, "
                "metrics, tokens, latency_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (1, 2, "health", 0, '["S10:M1"]', "T2-light", "refine",
                 "r", "{}", '{"M1_partial": {}}', 0, 12))
            n = conn.execute("SELECT COUNT(*) FROM replan_decisions"
                             ).fetchone()[0]
            conn.close()
            ok &= _rec("U12b ★ DDL 在内存库真执行 + 12 字段 INSERT 成功",
                       n == 1, "cols=%s" % cols)
            ok &= _rec("U12c 字段齐备（source/round/signals/level/action/"
                       "reason/gates/metrics/tokens/latency_ms）",
                       all(c in cols for c in ("conversation_id", "run_id",
                                               "source", "round", "signals",
                                               "level", "action", "reason",
                                               "gates", "metrics", "tokens",
                                               "latency_ms", "created_at")),
                       str(cols))
        except Exception as e:
            ok &= _rec("U12b DDL 执行失败", False, str(e)[:120])
    # 写入侧契约（stream.py）
    st = _read(STREAM_SRC)
    ok &= _rec("U12d 七类决策点都落库（t1_fail/stall/redraw/directive/"
               "health/early_stop/t3_redraw；1 def + 7 调用 = 8 处）",
               all(('"%s"' % s) in st
                   for s in ("t1_fail", "stall", "redraw", "directive",
                             "health", "early_stop", "t3_redraw"))
               and st.count("_write_decision(") == 8,
               "calls=%d" % st.count("_write_decision("))
    ok &= _rec("U12e ★ signals 归一化两种形状（信号 dict 列表 / basis 列表）",
               'isinstance(_sigs[0], dict)' in st
               and 'decision.get("basis")' in st)
    _wd = st[st.index("def _write_decision"):][:3000]
    ok &= _rec("U12f ★ 落库整体 try/except 旁路（写失败只丢记录不阻断编排）",
               _wd.count("except Exception:") >= 1
               and "conn.commit()" in _wd)
    ok &= _rec("U12g ★ 早停汇总记 full 非 partial（主动战略放弃 ≠ 没做成）",
               "_early_stopped" in st and "full" in
               st[st.index("if _early_stopped:"):][:600])
    return ok


# ══════════════════════════════════════════════════════════════════════════
def t_zero_change():
    print("\n=== U13. 零行为变更（P4 头号纪律）===")
    from services import replan_health as RH
    st = _read(STREAM_SRC)
    ok = True
    ok &= _rec("U13a 三闸默认取 False（运行期默认值，非写死的 True）",
               '_rp_hp = bool(_rcfg.get("replanning", "health_probe", False))'
               in st
               and '_rp_early = bool(_rcfg.get("replanning", "early_stop",'
               ' False))' in st)
    ok &= _rec("U13b ★ 挂点闸 = 总闸 + (health_probe 或 early_stop)"
               "（两者全关 ⇒ 段整体 no-op）",
               "_RH_OK and _rp_enabled and (_rp_hp or _rp_early)" in st)
    ok &= _rec("U13c 巡检模块不可用 ⇒ 整体退化为 P3 行为（_RH_OK 标志位）",
               "_RH_OK = False" in st and "_RH_OK = True" in st)
    ok &= _rec("U13d health_probe(enabled=False) 恒 none（纯函数层零变更）",
               RH.health_probe([{"summary": {"status": "partial"}}] * 5,
                               [], enabled=False)["action"] == "none")
    ok &= _rec("U13e early_stop_plan(enabled=False) 恒 none",
               RH.early_stop_plan([{"task_key": "t1", "status": "done",
                                    "config": '{"required": true}'}],
                                  enabled=False)["action"] == "none")
    ok &= _rec("U13f probe_due 双 ≤0 永不到期（配置层零变更）",
               RH.probe_due(100, 10000, every_tasks=0, every_sec=0) is False)
    ok &= _rec("U13g llm_actions 默认锁 replace ⇒ P3 T3 通道不变",
               '"llm_actions": "replace"' in _read(CONFIG_SRC))
    return ok


# ══════════════════════════════════════════════════════════════════════════
def t_mutation():
    print("\n=== M. 变异自证（每个变异都必须被抓，否则断言是空转的）===")
    from services import replan_health as RH
    from services import replan_redraw as RR
    import task_queue
    V = task_queue.TaskQueue.validate_plan
    ok = True
    ts = four_ts()
    three_raw = json.dumps({"tasks": [
        {"action": "replace", "key": "t2_r1", "title": "B替代", "deps": ["t1"]},
        {"action": "insert", "key": "t9", "title": "补充评审", "deps": ["t1"]},
        {"action": "skip", "key": "t3", "reason": "产出对下游非关键"},
    ]}, ensure_ascii=False)
    ALL = ("replace", "skip", "insert")

    # M1：去掉 M4 优先（M4 hit 也走 refine）⇒ U5d 必须红
    hsrc = _read(HEALTH_SRC)
    mc1 = hsrc.replace('    if m4.get("hit"):\n        sigs.append',
                       '    if False:\n        sigs.append')
    ok &= _rec("M1a 变异锚点存在（M4 优先分支）", mc1 != hsrc)
    if mc1 != hsrc:
        M1m = types.ModuleType("mutant_health1")
        try:
            exec(compile(mc1, "mutant", "exec"), M1m.__dict__)
            part3 = [{"summary": {"status": "partial"}}] * 3
            d = M1m.health_probe(part3, ts, enabled=True,
                                 latencies_ms=[60000, 60000, 60000],
                                 remaining_tasks=3, budget_left_sec=100)
            ok &= _rec("M1 ★ 去 M4 优先 ⇒ 预算超支只精修不停手"
                       "（时间不够时「改上下文」救不了交付，防线在）",
                       d["action"] != "stop", "action=%s" % d["action"])
        except Exception as e:
            ok &= _rec("M1 变异体执行失败", False, str(e)[:80])

    # M2：删"零 failed"判据 ⇒ 有 failed 时也提前收敛 ⇒ U6d 必须红
    mc2 = hsrc.replace('    if failed:\n        return {"action": ACTION_NONE,'
                       ' "cancel_keys": [], "required_keys": required,\n'
                       '                "reason": "存在 failed',
                       '    if False:\n        return {"action": ACTION_NONE,'
                       ' "cancel_keys": [], "required_keys": required,\n'
                       '                "reason": "存在 failed')
    ok &= _rec("M2a 变异锚点存在（零 failed 分支）", mc2 != hsrc)
    if mc2 != hsrc:
        M2m = types.ModuleType("mutant_health2")
        try:
            exec(compile(mc2, "mutant", "exec"), M2m.__dict__)
            ts_f = [T("t1", [], "done", config=json.dumps({"required": True})),
                    T("t2", ["t1"], "planned"),
                    T("t9", [], "failed")]
            d = M2m.early_stop_plan(ts_f, enabled=True)
            ok &= _rec("M2 ★ 删零 failed 判据 ⇒ 有失败也「目标达成」"
                       "（失败被收敛动作吞掉，防线在）",
                       d["action"] == "early_stop",
                       "action=%s" % d["action"])
        except Exception as e:
            ok &= _rec("M2 变异体执行失败", False, str(e)[:80])

    # M3：allow 外动作不再整份拒收（改为跳过该条）⇒ U9e/U11a 必须红
    dsrc = _read(DIR_SRC)
    mc3 = dsrc.replace(
        '        if act not in allow_set:\n'
        '            return [], [], ("LLM 产出 %s 动作，不在允许集 %s 内 ⇒ 整份拒收"\n'
        '                            % (act or "空", ",".join(sorted(allow_set))))',
        '        if act not in allow_set:\n            continue')
    ok &= _rec("M3a 变异锚点存在（allow 整份拒收）", mc3 != dsrc)
    if mc3 != dsrc:
        M3m = types.ModuleType("mutant_dir3")
        M3m.__dict__["__name__"] = "mutant_dir3"
        try:
            exec(compile(mc3, "mutant", "exec"), M3m.__dict__)
            _, _, e = M3m.parse_llm_plan_actions(three_raw, ["t2"])
            ok &= _rec("M3 ★ allow 外动作改为跳过 ⇒ insert 在 replace-only "
                       "下混进计划（P3 范围锁定被拆，防线在）",
                       e == "", "err=%s" % e[:60])
        except Exception as e:
            ok &= _rec("M3 变异体执行失败", False, str(e)[:80])

    # M4：Q3 insert 轨去掉 directive 校验 ⇒ U11e2 必须红
    rsrc = _read(REDRAW_SRC)
    mc4 = rsrc.replace(
        '        if is_insert and insert_ok:\n'
        '            if not str(lin.get("directive") or "").strip():\n'
        '                ok3 = False\n'
        '                break',
        '        if is_insert and insert_ok:\n            pass')
    ok &= _rec("M4a 变异锚点存在（Q3 insert directive 校验）", mc4 != rsrc)
    if mc4 != rsrc:
        M4m = types.ModuleType("mutant_redraw4")
        M4m.__dict__["__name__"] = "mutant_redraw4"
        try:
            exec(compile(mc4, "mutant", "exec"), M4m.__dict__)
            g = M4m.quality_gates(
                [], [{"key": "t9", "title": "t", "deps": [],
                      "config": {"lineage": {"action": "insert",
                                             "directive": ""}}}],
                [], failed_keys={"t2"}, insert_ok=True)
            ok &= _rec("M4 ★ Q3 insert 轨去 directive 校验 ⇒ 无指令依据的"
                       "新任务过门（塞私货后门被拆，防线在）",
                       g["Q3"] is True, "Q3=%s" % g["Q3"])
        except Exception as e:
            ok &= _rec("M4 变异体执行失败", False, str(e)[:80])

    # M5：skip 不从 kept 排除 ⇒ U10c 必须红（Q5 误杀 + 重复派发）
    mc5 = rsrc.replace(
        '        if not k or k in failed or k in skip_keys or _terminal(t.get("status")):',
        '        if not k or k in failed or _terminal(t.get("status")):')
    ok &= _rec("M5a 变异锚点存在（kept 排除 skip_keys）", mc5 != rsrc)
    if mc5 != rsrc:
        M5m = types.ModuleType("mutant_redraw5")
        M5m.__dict__["__name__"] = "mutant_redraw5"
        try:
            exec(compile(mc5, "mutant", "exec"), M5m.__dict__)
            d = M5m.plan_llm_redraw(ts, ["t2"], llm_raw=three_raw,
                                    enabled=True, directive="把评审也加上",
                                    round_no=1, validate=V,
                                    allow_actions=ALL)
            kept_keys = [t.get("task_key") or t.get("key")
                         for t in d.get("kept") or []]
            ok &= _rec("M5 ★ skip 不从 kept 排除 ⇒ 被跳任务留在保留集"
                       "（Q5 误杀/重复派发，防线在）",
                       "t3" in kept_keys, "kept=%s" % kept_keys)
        except Exception as e:
            ok &= _rec("M5 变异体执行失败", False, str(e)[:80])

    # M6：节奏双闸失效（任一 <=0 也判 True）⇒ U1e 必须红
    mc6 = hsrc.replace('    if et <= 0 and es <= 0:\n        return False',
                       '    if et <= 0 and es <= 0:\n        return True')
    ok &= _rec("M6a 变异锚点存在（节奏双 ≤0 永假）", mc6 != hsrc)
    if mc6 != hsrc:
        M6m = types.ModuleType("mutant_health6")
        try:
            exec(compile(mc6, "mutant", "exec"), M6m.__dict__)
            ok &= _rec("M6 ★ 双 ≤0 改为永真 ⇒ 配置写 0 想关掉反而每次必检"
                       "（「关」的语义被拆，防线在）",
                       M6m.probe_due(1, 1, every_tasks=0, every_sec=0) is True)
        except Exception as e:
            ok &= _rec("M6 变异体执行失败", False, str(e)[:80])
    return ok


def main():
    print("P4 replanning 门禁（健康巡检 S10 + 提前收敛 + LLM 三动作 + "
          "决策落库，零 LLM 零服务零生产库写）")
    ok = t_static()
    ok &= t_config_runtime()
    ok &= t_health()
    ok &= t_actions()
    ok &= t_decisions_table()
    ok &= t_zero_change()
    ok &= t_mutation()
    n_pass = sum(1 for r in _results if r[0] == PASS)
    n_fail = len(_results) - n_pass
    print("\n════════════════════════════════════════════")
    print("结果：%d PASS / %d FAIL" % (n_pass, n_fail))
    for kind, n, d in _results:
        if kind != PASS:
            print("  %s  %s  %s" % (kind, n, d))
    sys.exit(0 if n_fail == 0 else 1)


if __name__ == "__main__":
    main()
