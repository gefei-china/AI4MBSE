# -*- coding: utf-8 -*-
"""意图识别「DB 自定义 Agent 强特异关键词优先」的自检（P1-13，步骤 B 代码侧）。

背景：实测「生成需求视图/结构视图/参数视图」全被 detect 里的 builtin 建模强信号正则
`(生成|...).{0,24}(视图|...)` 劫持到 design，用户自建 Agent（需求视图生成等）即使配了
intent_keywords 也永远轮不到（db 关键词匹配在强信号正则之后）。

修法：在 builtin 强信号之前插入「db 层强特异命中（≥ _DB_KW_PRIORITY_MIN_SCORE = 1.0，
即至少一个 ≥4 字非泛词）」的优先返回。

═══════════════════════════════════════════════════════════════════════════
【2026-11-12 重构：门禁与真库数据解耦】
原版把「db 层」直接取自真库 `agents` 表，于是**真库数据一改、门禁就飘红**，且与被测
机制完全无关：真库后来回填的 `view_expansion` Agent 配了
「生成视图 / 需求视图 / 结构视图 / 参数视图」，与原版合成用例的前提（这些词在 db 层
无人认领）直接冲突 ⇒ F1/F2/F2b/M1/M1b 五条集体变红（真库口径与 CI 干净库口径都红）。

重构后分两段，口径互不污染：
  [F][M] 机制段 —— db 层用**受控合成数据**（不读真库），断言「db 强特异词优先 /
        阈值守卫 / builtin 守卫不被误伤」等**代码机制**。合成层刻意包含一个
        「视图家族」干扰 Agent（模拟真库 view_expansion），用来验证
        **用户显式配置的强特异词能压过干扰 Agent 的同族词**——这正是原版缺失的覆盖。
  [T] 真库段 —— 只断言**稳定不变量**（关键词跨 Agent 无冲突、db 层纯净自洽、
        批量探测不异常），不把任何具体词的路由结果写死 ⇒ 真库增删 Agent 不再让门禁飘红。
        真库不可用（干净库 / 表不存在）时该段整体 SKIP，不计 FAIL。

⇒ 干净库（CI 口径：MBSE_DB_PATH 指向空库 + init_db）与真库口径下门禁都必须全绿。
"""
import json
import os
import sqlite3
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.chdir(ROOT)

from agent.intent import IntentRouter  # noqa: E402

PASS, FAIL, SKIP = [], [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s%s" % ("PASS" if cond else "FAIL", name,
                         ("  <- " + str(detail)) if detail else ""), flush=True)


def skip(name, detail=""):
    SKIP.append(name)
    print("  SKIP %s%s" % (name, ("  <- " + str(detail)) if detail else ""), flush=True)


# ── 合成 db 层（机制段专用，与真库无关）────────────────────────────────────
# 结构与真库 agents 表对齐：name / display_name / description / keywords。
# ⚠️ 「视图家族」干扰 Agent 刻意复刻真库 view_expansion 的**同族词**形态
#   （需求视图/结构视图/参数视图/生成视图…）—— 用来验证「用户显式配的强特异词
#   压得过干扰 Agent」这一原先缺失的关键覆盖。
_SYNTH_AGENTS = [
    ("design", "工程建模方案设计Agent", "SysML 建模与代码生成",
     ["建模、方案", "设计", "架构", "方案设计", "sysmlv2代码", "代码生成", "模型生成"]),
    ("impact", "变更影响Agent", "变更影响分析",
     ["变更", "影响", "变更影响", "影响分析", "影响范围", "波及", "改动"]),
    ("report_generation", "报告生成Agent", "生成各类报告",
     ["报告", "文档", "汇报", "导出", "生成报告", "分析报告", "评审报告"]),
    ("review", "预评审Agent", "模型与需求预评审",
     ["校验", "评审", "检查", "预评审", "评分", "validate"]),
    ("knowledge_qa", "知识问答Agent", "知识库检索与讲解",
     ["知识库", "资料", "文档里", "查一下", "介绍", "说明", "什么是", "原理"]),
    # 干扰 Agent：同族词覆盖面广（模拟真库 view_expansion）
    ("view_expansion", "视图展开Agent", "多类型视图展开生成",
     ["视图展开生成", "活动视图生成", "需求视图", "活动视图", "状态机视图", "用例视图",
      "时序视图", "参数视图", "结构视图", "交互视图", "视图展开", "生成视图",
      "建模视图", "活动图", "用例图", "参数图", "结构图"]),
    # 被测目标 Agent（合成层自建，真库里未必存在同名 Agent）
    ("需求视图生成", "需求视图生成Agent", "生成需求视图", ["需求视图生成"]),
    ("结构视图生成", "结构视图生成Agent", "生成结构视图", ["结构视图生成"]),
    ("参数视图生成", "参数视图生成Agent", "生成参数视图", ["参数视图生成"]),
    # 纯泛词代理：关键词全是泛词（视图/建模/生成）⇒ 不得触发 db 强特异优先
    ("泛词代理", "泛词代理Agent", "占位", ["视图", "建模", "生成"]),
]


def _synth_router(extra_kw=None, drop=None):
    """构造一个只含合成 db 层的 IntentRouter。

    extra_kw: {agent_name: [新关键词]} —— **整表替换**该 Agent 的关键词（原版语义）。
    drop:     {agent_name} —— 该 Agent 整体不注册（用于隔离干扰）。
    """
    drop = drop or set()
    rt = IntentRouter()
    sem = []
    for name, disp, desc, kws in _SYNTH_AGENTS:
        if name in drop:
            continue
        kws = list((extra_kw or {}).get(name, kws))
        rt.register_keywords(name, kws)
        sem.append({"name": name, "text": f"{disp} {desc} {' '.join(kws)}"})
    rt.set_semantic_index(sem)
    return rt


def detect(rt, text):
    return rt.detect(text, conn=None)


# ── [F] 功能语义（机制段：合成 db 层）──────────────────────────────────────
print("[F] DB 强特异关键词优先（合成 db 层，与真库数据解耦）")

# F1 纯泛词「视图」无 db 强特异命中 → 仍走 builtin 建模强信号 design（不误抢）。
#     隔离口径：**剔除所有含强特异词的 Agent**（视图家族干扰 Agent + 目标 Agent），
#     只留纯泛词代理 ⇒ db 层无任何 ≥4 字非泛词命中。
_SYNTH_SPECIFIC = {a[0] for a in _SYNTH_AGENTS
                   if any(len(k) >= 4 and k not in IntentRouter._GENERIC_KW
                          for k in a[3])}
_rt_a = _synth_router(drop=_SYNTH_SPECIFIC)
check("F1 纯泛词「视图」无 db 强特异命中 → 仍 design（不误抢）",
      detect(_rt_a, "生成视图") == "design",
      "intent=%s" % detect(_rt_a, "生成视图"))

# F2 配强特异关键词 → 命中自建 Agent（新能力），且**压过同族干扰 Agent**。
#     干扰隔离口径：view_expansion 保留其余同族词，但**摘掉**它对
#     「需求视图/生成需求视图」的认领 —— 模拟"用户已把自己的 Agent 配成唯一归属方"。
_rt_b = _synth_router(extra_kw={
    "需求视图生成": ["需求视图", "生成需求视图"],
    "结构视图生成": ["结构视图", "生成结构视图"],
    "view_expansion": [k for k in _SYNTH_AGENTS[5][3]
                        if k not in ("需求视图", "结构视图", "参数视图", "生成视图")],
})
check("F2 配强特异关键词 → 命中自建 Agent（压过同族干扰 Agent）",
      detect(_rt_b, "生成需求视图") == "需求视图生成",
      "intent=%s" % detect(_rt_b, "生成需求视图"))
check("F2b 第二条自建 Agent 同样命中",
      detect(_rt_b, "生成结构视图") == "结构视图生成",
      "intent=%s" % detect(_rt_b, "生成结构视图"))
# F2c 负对照：干扰 Agent 仍在时，**不属于**目标的同族词仍归干扰 Agent
#     （证明 F2 不是"把所有视图词都判给目标 Agent"的粗暴实现）。
check("F2c 同族词未归属目标 → 仍归干扰 Agent（未被粗暴改写）",
      detect(_rt_b, "生成活动视图") == "view_expansion",
      "intent=%s" % detect(_rt_b, "生成活动视图"))
check("F3 不误伤 impact 正例（内置强特异词仍优先）",
      detect(_rt_b, "分析变更影响") == "impact",
      "intent=%s" % detect(_rt_b, "分析变更影响"))
check("F4 不误伤 design 正例（无 db 强特异命中 → 仍走建模强信号）",
      detect(_rt_b, "生成 SysML 模型代码") == "design",
      "intent=%s" % detect(_rt_b, "生成 SysML 模型代码"))
# 报告命令守卫：SP-R 报告路由（「输出…报告」）优先于 db 关键词 —— 否则「输出影响报告」
# 会被 db 层 impact 的「变更影响」抢走（评测集曾抓到 report_generation→impact 错例）。
# ⚠️ 用例须**不含建模词**（视图/建模/代码），否则先被 design 建模强信号（458 行）抢走，测不到 SP-R。
_rt_d = _synth_router()
check("F5 报告命令（输出…报告）优先于 db 关键词（SP-R 守卫）",
      detect(_rt_d, "输出变更影响报告") == "report_generation",
      "intent=%s" % detect(_rt_d, "输出变更影响报告"))
# 复合任务守卫：建模/代码/校验 + 并列连词 → 走 design 编排，细粒度视图 Agent 不抢
# （P1-14 实测：合并 INTENTS 关键词后「建模+结构视图+代码+校验」被「结构视图生成」抢走）
_rt_e = _synth_router()
check("F6 复合任务（建模+视图+代码+校验）→ design，不被细粒度视图 Agent 抢",
      detect(_rt_e, "帮我进行工程建模，生成结构视图，并生成代码进行校验") == "design",
      "intent=%s" % detect(_rt_e, "帮我进行工程建模，生成结构视图，并生成代码进行校验"))


# ── [M] 阈值守卫负对照（机制段：合成 db 层）────────────────────────────────
print("[M] 阈值守卫（≥1.0 不是空转）")

# 2 字特异词「参数」score=0.5 < 1.0 → 正常应不触发 db 优先；阈值降到 0 则被抢。
# ⚠️ 直接测纯函数 `_db_kw_priority`，不走 detect —— 意图缓存是类级共享且指纹不含阈值，
#   走 detect 会被上一次缓存毒化（M1b 首版就踩了）。
# ⚠️ 双重隔离，否则测的就不是阈值而是别的东西：
#   ① 干扰 Agent view_expansion 的「参数视图」（4 字，1.0 分）必须摘掉，否则它压过
#      0.5 分的「参数」⇒ before 也被抢（这正是真库口径下的历史红项之一）；
#   ② 纯泛词代理「泛词代理」必须整条 drop —— 阈值降到 0 时它凭**纯泛词**同样够分，
#      会抢走胜出位（_db_kw_priority 阈值=0 的语义本就是"完全去掉守卫"，此时它该赢）。
#      本用例只回答"阈值这一个开关是否起作用"，故把它移出竞争。
_rt_m = _synth_router(
    extra_kw={
        "参数视图生成": ["参数"],
        "view_expansion": [k for k in _SYNTH_AGENTS[5][3]
                           if k not in ("需求视图", "结构视图", "参数视图", "生成视图")],
    },
    drop={"泛词代理"},
)
_t = "生成参数视图".lower()
_before = _rt_m._db_kw_priority(_t)
IntentRouter._DB_KW_PRIORITY_MIN_SCORE = 0.0
try:
    _after = _rt_m._db_kw_priority(_t)
finally:
    IntentRouter._DB_KW_PRIORITY_MIN_SCORE = 1.0
check("M1 阈值 1.0 时弱信号（2 字词 0.5 分）不触发 db 优先",
      _before is None, "before=%s" % _before)
check("M1b 阈值降到 0 → 弱信号也被抢（守卫失效被抓住）",
      _after == "参数视图生成", "after=%s" % _after)


# ── [T] 真库段：只断言稳定不变量（不写死任何具体词的路由结果）──────────────
print("[T] 真库不变量（数据体检；真库不可用则整段 SKIP）")


def _load_real_agents():
    """只读加载真库 active agents；不可用返回 None（干净库 / 表不存在 / 无权限）。"""
    path = os.environ.get("MBSE_DB_PATH", "mbse.db")
    if not os.path.exists(path):
        return None
    try:
        c = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
        c.row_factory = sqlite3.Row
        rows = [dict(r) for r in c.execute(
            "SELECT name, display_name, description, intent_keywords FROM agents "
            "WHERE status='active' ORDER BY name").fetchall()]
        c.close()
        return rows or None
    except Exception:
        return None


_REAL = _load_real_agents()
if not _REAL:
    skip("T1/T2/T3 真库不可用或无 active Agent（干净库口径）")
else:
    # T1：≥4 字非泛词关键词**不得跨 Agent 冲突** —— 冲突意味着两个 Agent 抢同一批词，
    #     路由结果随注册顺序漂移（真库实况：0 冲突）。
    _own = {}
    for r in _REAL:
        for k in json.loads(r["intent_keywords"] or "[]"):
            if len(k) >= 4 and k not in IntentRouter._GENERIC_KW:
                _own.setdefault(k, set()).add(r["name"])
    _conflict = {k: sorted(v) for k, v in _own.items() if len(v) > 1}
    check("T1 真库 ≥4 字强特异关键词无跨 Agent 冲突",
          not _conflict, "冲突=%s" % (_conflict or "无"))

    # T2：db 层**纯净自洽** —— 把某个 Agent 单独放进 db 层，用它自己的首个强特异词
    #     探测，必须命中自己（证明它配的词确实在 db 层可被认领）。
    _t2_bad = []
    for r in _REAL:
        kws = json.loads(r["intent_keywords"] or "[]")
        strong = [k for k in kws
                  if len(k) >= 4 and k not in IntentRouter._GENERIC_KW]
        if not strong:
            continue
        solo = IntentRouter()
        solo.register_keywords(r["name"], kws)
        got = solo.detect(strong[0], conn=None)
        if got != r["name"]:
            _t2_bad.append((r["name"], strong[0], got))
    check("T2 真库各 Agent 在纯净 db 层下自认领成功", not _t2_bad,
          "异常=%s" % (_t2_bad or "无"))

    # T3：全量 db 层批量探测不抛异常、不返回空（数据体检，不判对错）。
    _full = IntentRouter()
    _sem = []
    for r in _REAL:
        kws = json.loads(r["intent_keywords"] or "[]")
        _full.register_keywords(r["name"], kws)
        _sem.append({"name": r["name"],
                     "text": f"{r['display_name'] or r['name']} {r['description'] or ''} "
                             f"{' '.join(kws)}"})
    _full.set_semantic_index(_sem)
    _t3_bad = []
    for r in _REAL:
        for k in json.loads(r["intent_keywords"] or "[]"):
            if not k:
                continue
            try:
                got = _full.detect(k, conn=None)
                if not got:
                    _t3_bad.append((r["name"], k, got))
            except Exception as e:
                _t3_bad.append((r["name"], k, "EXC:%s" % e))
    check("T3 真库全量关键词批量探测无异常/无空结果", not _t3_bad,
          "异常=%s" % (_t3_bad[:5] or "无"))


# ── 汇总 ────────────────────────────────────────────────────────────────────
print()
print("=" * 78)
print("PASS %d / FAIL %d / SKIP %d" % (len(PASS), len(FAIL), len(SKIP)))
for f in FAIL:
    print("  FAIL:", f)
raise SystemExit(1 if FAIL else 0)
