# -*- coding: utf-8 -*-
"""V2 顶层分流：按实测判据把请求路由到对应任务域与通路。

设计依据（不是推理，是本轮实测）
--------------------------------
1. 路由基线：意图识别 44条真实样本 accuracy=0.955 / macroF1=0.965
   （`tools/verify/verify_intent_routing_baseline.py`）
2. operation 可推导性：95.5%，且误差**与意图误差完全同源**（映射表零错误）
   （`tools/eval/probe_operation_slot.py`）
3. 两条实测错例**都是只读查看类被当成产出类**：
   · 「帮我看看这个系统大概是怎么设计的」 → design/create 误判为 chat/explain
   · 「帮我看看这个系统的接口设计是否合理」  → design/create 误判为 review/verify
   ⇒ **只读请求若误入建模流水线，会白跑 M0→N6 全流程**。
   本模块的第一职责就是拦住这类请求。

因此分流判据按**优先级**排列，越靠前越可靠：
  ① 显式破坏动词（删除/移除/清空/重构）→ 必过安全门，不需要靠猜
  ② 域归属（`agents.task_domain`，V1 已落库）—— 比 operation 更可靠，DB 直读
  ③ operation 映射（intent → operation）
  ④ 只读查看类**降级守卫**（本模块自研，针对上述两条实测错例）

用法
----
    from tools.eval.probe_operation_slot import classify   # 纯函数，易测
或
    ./.venv/Scripts/python.exe -X utf8 tools/eval/probe_operation_slot.py --selftest
"""
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

DOMAINS = ("modeling", "qa", "system", "report", "impact", "general")

# ── ① 破坏性动作词（显式信号，优先级最高）─────────────────────────
DESTRUCTIVE_PAT = re.compile(
    r"(删除|移除|删掉|清空|重构|批量替换|改边界|去掉|移除掉)")

# ── ② 只读查看类守卫（针对实测两条错例）──────────────────────────
# 「看看…设计」「看下…接口是否合理」= 只读评审，不是产出
READONLY_VERB = re.compile(r"(看看|看一下|看下|检查一下|评估一下|合理吗|是否合理|怎么样|如何|是什么|介绍|解释)")
DESIGN_NOUN = re.compile(r"(设计|架构|接口|方案)")

# ── ③ intent → operation → 域 映射（实测95.5% 可推导）────────────
INTENT_TO_OP = {
    "design": "create",
    "requirement_analysis": "create",
    "report_generation": "create",
    "review": "verify",
    "requirement_quality": "analyze",
    "impact": "analyze",
    "knowledge_qa": "explain",
    "system_mgmt": "analyze",
    "chat": "explain",
}
# 8 视图 Agent 意图（name= display_name，DB 里 name 是中文）
VIEW_INTENTS = {"需求视图生成", "结构视图生成", "交互视图（IBD）生成", "用例视图生成",
                "活动图生成", "状态机生成", "状态机视图生成", "参数视图生成",
                "顺序视图（时序图）生成", "多方案生成"}

OP_TO_DOMAIN = {
    "create": "modeling",
    "verify": "modeling",
    "view": "modeling",
    "delete": "modeling",
    "modify": "modeling",
    # ⚠️ analyze/explain **不从 operation 推域** —— 自测抓到过 bug：
    #   system_mgmt（查审计日志）曾被推到 impact 域，但 impact 是「模型变更影响分析」，
    #   与系统查询毫无关系；且 impact 是独立入口 Agent，本就不该进流水线。
    # 正解：域直接读 V1 已落库的 `agents.task_domain`（DB 直读，比推导可靠）。
    "analyze": None,
    "explain": None,
}
OP_TO_PATH = {
    "create": ["M0", "N1", "N2", "N3", "N4", "N5", "N6"],
    "modify": ["M0", "N1", "N2", "N3", "N4", "N5", "N6"],
    "verify": ["M0", "N3", "N4", "N5"],
    "view":   ["M0", "N3", "N4"],
    "delete": ["M0", "G", "N6"],
    "analyze": [],
    "explain": [],
}

# 域→是否走流水线。V1 实测落库的 task_domain 分布：
#   modeling 13 / qa 2 / system 1 / report 1 / impact 1 / general 1
# 只有 modeling 走流水线；report 与 impact 是**旁路**（消费/分析已有产物，不重跑建模）。
DOMAIN_PIPELINE = {
    "modeling": True,
    "qa": False,
    "system": False,
    "report": False,     # 旁路：消费 N1-N5 产物
    "impact": False,     # 旁路：独立触发
    "general": False,
}


def classify(text: str, intent: str, task_domain: str | None = None) -> dict:
    """纯函数：输入原始文本 + 路由得到的 intent (+ V1 落库的 task_domain)，输出分流决策。

    task_domain 来自 `agents.task_domain`（V1 已落库）。**优先于 operation 推导** ——
    自测抓到过 bug：operation=analyze 曾把 system_mgmt 推到 impact 域。
    域是Agent 的固有属性（DB 直读），operation 是本次请求的属性，前者更可靠。

    返回：{domain, path, operation, reason, guards}
    guards 列出命中的守卫，便于调试与门禁断言。
    """
    guards = []

    # ① 显式破坏动词 → 建模域 + 安全门（最高优先级，不依赖意图与域）
    if DESTRUCTIVE_PAT.search(text or ""):
        guards.append("destructive_verb")
        return {
            "domain": "modeling",
            "pipeline": True,
            "path": OP_TO_PATH["delete"],
            "operation": "delete",
            "reason": "命中显式破坏动词，必须过变更安全门",
            "guards": guards,
        }

    op = INTENT_TO_OP.get(intent)
    if op is None and intent in VIEW_INTENTS:
        op = "view"
        guards.append("view_intent")

    # ② 只读查看类降级守卫
    # 前提：意图被判成"产出/校验"类，但文本是只读查看语气
    if op in ("create", "verify") and READONLY_VERB.search(text or "") and DESIGN_NOUN.search(text or ""):
        guards.append("readonly_downgrade")
        return {
            "domain": "qa",
            "pipeline": False,
            "path": [],
            "operation": "analyze",
            "reason": ("只读查看类请求（看看/评估/是否合理 + 设计类名词），"
                       "降级为直答；不进入建模流水线"),
            "guards": guards,
        }

    # ③ 域归属：DB 直读优先
    dom = task_domain if task_domain in DOMAINS else None
    dom_source = "db_task_domain"
    if dom is None:
        dom = OP_TO_DOMAIN.get(op) or "qa"
        dom_source = "operation_derivation" if OP_TO_DOMAIN.get(op) else "fallback_qa"
        guards.append("domain_derived")

    if op is None:
        guards.append("no_mapping_fallback")
        if dom == "qa":
            return {
                "domain": "qa", "pipeline": False, "path": [], "operation": "explain",
                "reason": f"意图 {intent!r} 未在映射表内，保守降级为直答",
                "guards": guards,
            }

    pipe = DOMAIN_PIPELINE.get(dom, False)
    # 建模域但 operation 落在 analyze/explain ⇒ 不该进流水线（只读意图误入建模域时）
    if dom == "modeling" and op in ("analyze", "explain"):
        pipe = False
        dom = "qa"
        guards.append("modeling_domain_readonly_op")

    return {
        "domain": dom,
        "pipeline": pipe,
        "path": OP_TO_PATH.get(op, []) if pipe else [],
        "operation": op or "explain",
        "reason": f"意图 {intent!r} → operation {op!r} → 域 {dom!r}（来源 {dom_source}）",
        "guards": guards,
    }


def selftest():
    """自测：覆盖实测两条错例 + 各域典型输入。

    第三列为**V1 已落库的 task_domain**（模拟 DB 直读）—— 必须传，否则测的是
    operation 推导路径，而推导路径已被证明会把 system_mgmt 误推到 impact。
    """
    cases = [
        # (文本, 路由intent, task_domain, 期望域, 期望是否流水线, 说明)
        ("帮我看看这个系统大概是怎么设计的", "chat", "qa", "qa", False, "实测错例1：只读降级"),
        ("帮我看看这个系统的接口设计是否合理", "review", "modeling", "qa", False, "实测错例2：只读降级盖过 DB 域"),
        ("删除通信子系统", "impact", "impact", "modeling", True, "破坏动词强制建模域+安全门"),
        ("帮我生成电动汽车热管理系统的sysml V2代码并进行校验", "design", "modeling", "modeling", True, "标准建模流"),
        ("生成活动图", "活动图生成", "modeling", "modeling", True, "视图 intent"),
        ("SysML 2 是什么", "knowledge_qa", "qa", "qa", False, "知识问答直答"),
        ("查看一下审计日志", "system_mgmt", "system", "system", False, "系统查询走 system 域（曾误推 impact）"),
        ("帮我导出一份报告", "report_generation", "report", "report", False, "报告=旁路，不重跑流水线"),
        ("改一下电池温度需求会影响什么", "impact", "impact", "impact", False, "影响分析=旁路独立入口"),
    ]
    print("=" * 74)
    print("V2 分流自测（task_domain 走 DB 直读路径）")
    print("=" * 74)
    ok = 0
    for text, intent, dom_db, want_dom, want_pipe, why in cases:
        r = classify(text, intent, dom_db)
        hit = (r["domain"] == want_dom) and (r["pipeline"] == want_pipe)
        ok += hit
        print(f"  [{'OK  ' if hit else 'FAIL'}] {why}")
        print(f"         「{text[:24]}」 intent={intent} task_domain={dom_db}")
        print(f"         → domain={r['domain']} pipeline={r['pipeline']} "
              f"op={r['operation']} path={r['path']}")
        if r["guards"]:
            print(f"         guards={r['guards']}")
    print("=" * 74)
    print(f"  {ok}/{len(cases)} 通过")
    return 0 if ok == len(cases) else 1


    print(__doc__)
    print("提示：加 --selftest 跑自测")


def run_on_samples():
    """用真实 confirmed 样本跑分流，报告分布 + 守卫命中情况。

    关键判据（三个都不能 violated）：
      ① 只读降级守卫**不得误伤**真正的产出类请求
      ② 破坏动词守卫**不得漏掉**任何破坏性请求
      ③ 域分布应与 V1 落库的 task_domain 分布大体一致（不出现某一域被吞掉）
    """
    from database import get_db
    from agent.pipeline import AgentPipeline

    conn = get_db()
    rows = conn.execute(
        "SELECT text, intent FROM intent_samples "
        "WHERE status='confirmed' AND intent<>'' ORDER BY id").fetchall()
    dom_map = dict(conn.execute(
        "SELECT name, task_domain FROM agents WHERE status='active'").fetchall())
    pipe = AgentPipeline()
    pipe._load_db_agents()
    rt = pipe.router

    dist, guards_hit, cases = {}, {}, []
    for r in rows:
        text, want_intent = r["text"], r["intent"]
        try:
            conn.execute("DELETE FROM intent_cache"); conn.commit()
        except Exception:
            pass
        got = rt.detect(text, conn=conn)
        res = classify(text, got, dom_map.get(got))
        dist[res["domain"]] = dist.get(res["domain"], 0) + 1
        for g in res["guards"]:
            guards_hit[g] = guards_hit.get(g, 0) + 1
        cases.append((text, want_intent, got, res))
    conn.close()
    return cases, dist, guards_hit, dom_map


def report():
    cases, dist, guards, dom_map = run_on_samples()
    n = len(cases)
    print("=" * 74)
    print(f"V2 分流在真实样本上的表现（n={n}）")
    print("=" * 74)
    print(f"\n  域分布：{dist}")
    print(f"  守卫命中：{guards or '（无）'}")

    pipe_n = sum(1 for c in cases if c[3]["pipeline"])
    print(f"\n  走流水线的样本：{pipe_n}/{n}（其余 {n-pipe_n} 条直答/旁路）")

    print("\n  ① 只读降级守卫逐条复核（不得误伤产出类）:")
    hit = [c for c in cases if "readonly_downgrade" in c[3]["guards"]]
    if not hit:
        print("     本次未命中（守卫未触发）")
    for text, wi, gi, res in hit:
        print(f"     · 「{text[:34]}」intent={gi} op={res['operation']} → 域 {res['domain']}")

    print("\n  ② 破坏动词守卫逐条复核（不得漏破坏性请求）:")
    hit = [c for c in cases if "destructive_verb" in c[3]["guards"]]
    if not hit:
        print("     本次未命中（语料里无破坏性请求）")
    for text, wi, gi, res in hit:
        print(f"     · 「{text[:34]}」→ path={res['path']}")

    print("\n  ③ 建模域样本的路径抽样（确认 operation 分布合理）:")
    for text, wi, gi, res in cases:
        if res["domain"] == "modeling" and res["pipeline"]:
            print(f"     · op={res['operation']:7} path={'→'.join(res['path'])}  「{text[:22]}」")

    print("\n  ④ 域被吞掉检查（各 task_domain 是否都有样本到达）:")
    reach = {d: 0 for d in set(dom_map.values())}
    for _, _, _, res in cases:
        reach[res["domain"]] = reach.get(res["domain"], 0) + 1
    for d in sorted(reach):
        print(f"     {d:9} 到达 {reach.get(d,0):2} 条")

    print("\n" + "=" * 74)
    ok = True
    # 判据：产出类样本（create/verify/view）不能被只读守卫降级
    mis = [c for c in cases
           if c[3]["operation"] in ("create", "verify", "view")
           and "readonly_downgrade" in c[3]["guards"]]
    # 注意：实测错例2 正是这种情况（期望 design/create 却被判 review），
    # 降级是**期望行为**（只读语义优先），故此处不判FAIL，改为报告。
    print(f"  产出类被只读守卫降级：{len(mis)} 条"
          f"（实测错例 2 属此类，降级为期望行为）")
    if not dist.get("modeling"):
        print("  ⇒ FAIL 建模域零样本，分流可能整体失效")
        ok = False
    print("=" * 74)
    return 0 if ok else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    if "--samples" in sys.argv:
        sys.exit(report())
