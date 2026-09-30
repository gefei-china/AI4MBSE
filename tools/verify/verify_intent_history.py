# -*- coding: utf-8 -*-
"""P0-5 历史联合召回（v2 定稿）+ 语义意见喂澄清卡 —— 常驻断言 + 变异自证。

## 修的是什么
`detect(text, conn, prev_intent)` 原只吃**上轮意图名**，历史原话虽在 `messages` 表里完整可得却没用。
本批把"最近几条用户原话"净化后带进 **LLM 兜底段**（只喂这一路）。

## 三条硬约束（全部来自实测，不是设计推断）
1. **只喂 LLM 一路**：把历史拼进 detect 的 `text` → 规则层 SP-R 报告路由以 **0.95 高置信**劫持
   （历史含"输出影响报告"，6/6 追问全变 report_generation）；拼进语义层则 **5/5 返回空**
   （意图级顶分仅 0.44 << dense 采纳阈 0.69）→ 语义一路坚持用原文。
2. **必须净化**：真实会话（325）user 消息里【澄清补充】卡片续答占一半以上，且**最新几条恰是它**
   → 不净化常常一条有效历史都不剩。
3. **带历史的结论不得按原文写意图缓存**：缓存 key 只用原文，否则下一轮**没有**上文的同一句话
   会命中它（本仓"缓存毒化"有前例）。

## 走过的弯路（记录以免重蹈）—— 三轮，全部由实测推翻
**弯路 A：用"语义意图级 top1 与带历史结论一致"当背书。**
  理由曾是"语义吃原文、不受历史污染"。实测推翻：追问句原文的语义 top1 恒为 `knowledge_qa@0.37`
  这类**噪声** → 它与 LLM 的正确结论必然不一致 → 门在**最需要它的场景下必然失效**。
  根因：追问句本身没有语义信号，拿它的语义意见当背书 = 拿噪声当判据。

**弯路 B：用"LLM 自报置信 ≥ 门槛"当采纳门（v1，取 0.80 / 0.75）。**
  实测**否决**，两条证据：
  (1) v1 的标定是**截断分布**：`probe_gate_calib` 只在 route=='llm_history' 时记分，而"采纳"
      本身就是"分 ≥ 门槛"的后果 → 它报出的"最低分 0.80"**就是门槛值本身**
      （最小值==门槛 = 截断签名）。噪声侧同病：未采纳时显示值被 `min(conf,0.5)` 钳位，
      真实的 0.55~0.90 全被压成 0.50，看不见。
  (2) 换成**直连 `detect_llm` 取未截断分**（`probe_gate_calib2`）后：
      同话题侧 9/9 判出 impact、分 [0.62 … 0.95]（下限 0.62）；
      噪声侧  9 次里 **7 次给出非 chat 结论**、分 [0.55 … 0.90]（上限 0.90）。
      → 分离带**倒挂**（0.90 > 0.62），**不存在安全阈值**：任何阈值都会误采 5~7/9 的噪声结论。

**弯路 C（最要紧）：这道门**收益本来就是 0**。**
  `probe_gate_v3` 带 prev_intent 实测：同话题追问 + prev=impact 时意图 **6/6 都是 impact** ——
  其中 2/6 走 `inherit`、4/6 走这道门。即"用上文改写方向"的能力**在正确场景下贡献为零**
  （inherit 已经答对），却要在**错误场景下**（历史与当前问题无关）承担改错方向的风险。

## v2 定稿判据（只保留"不可能改变方向"的部分）
低置信分支：`有上文 ∧ 非 chat ∧ llm_intent == prev_intent ∧ conf ≥ 0.55` → `route=llm_history`。
  采纳的意图**恒等于 prev_intent** = "不采纳时 `inherit` 会给的同一个意图" → **不可能改向**。
高置信分支（≥0.85）：**保留 LLM 结论**（真·话题转向不能丢），但把它标成 `llm_history`
  + `confirmed = (llm_intent == prev_intent)`：
    一致 → 不打扰（与旧行为可见差异为零）；不一致 → 出"可改选"细条。
  ⚠️ 必须有这一步 —— v1 在这里直接 `route='llm'`（conf≥0.85 → `needs_clarify=False`）是
    **静默路径**：实测噪声上文能给出非 chat 结论且自报分高达 0.90/0.95 → 静默走错。
无 `prev_intent` 时**不带上下文**（提示词与旧版逐字节一致 → 零回归）。
**"话题转向"能力明确不做**：它是唯一可能改方向的部分，实测不可靠；要复活它得先找到
  独立于 LLM 自报分的佐证（显式相关性判别 / P0-2 的域边界描述），不能靠调阈值。

## 断言分组
A 净化（纯函数）｜C 采纳门**确定性**判据（stub 掉 LLM，零抖动）｜C-real 真实 LLM 只做**不变式**观测
｜B 缓存防线｜D meta/入口重置/取历史/澄清卡候选｜E 变异自证（M1~M8，只认"新增 FAIL"）

⚠️ **本脚本把 LLM 从"判据"降级为"观测对象"** —— 初版 C1 用真实 LLM 判"是否采纳"，
结果同一输入 4 连败、而下一段又成功，表现为"时 P 时 F"（LLM 抖动 0.62~0.95）。
机制层全部改用 stub（固定返回）后判定 100% 确定；真实 LLM 只跑"不变式"（见 C-real）。

用法：.venv/Scripts/python.exe -X utf8 tools/verify/verify_intent_history.py
"""
import os
import sys

ROOT = "C:/Users/gefei/WorkBuddy/2026-08-04-19-05-52/mbse_system"
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from agent.pipeline import AgentPipeline   # noqa: E402
from database import get_db                # noqa: E402

OK, FAIL = [], []


def chk(name, cond, ev=""):
    (OK if cond else FAIL).append(name)
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  <- " + ev) if ev else ""))


HIST_TOPIC = [
    "对巡飞弹做参数变更影响分析：推进剂加注量与巡飞速度调整，影响深度3层",
    "巡飞弹参数变更的影响范围要覆盖到总体指标层",
    "请对巡飞弹做一次参数变更影响分析，输出影响结论",
]
HIST_NOISY = ["请生成一份关于宽带通信系统的分析报告",
              "帮我看看这个项目的预算",
              "用一句话介绍热管理系统"]
Q_FOLLOW = "那它的风险呢"                      # 追问句：单发判不出（LLM 只给 chat@0.2~0.35）
Q_BAD = "帮我看看这个系统的接口设计是否合理"     # 会走到语义段（用于制造 _last_sem_alt）
Q_RULE = "对当前模型进行预评审校验"             # 规则命中，不经语义段
Q_STRONG = "对这个系统做总体设计"               # 高置信语义（对照：仍应写缓存）


# ───────────────────────── 基础设施 ─────────────────────────
def run(router, text, history=None, prev=None):
    """一次完整 detect；返回 (意图, meta, 该 query 在 intent_cache 的行数)。"""
    c = get_db()
    try:
        c.execute("DELETE FROM intent_cache")
        c.commit()
        got = router.detect(text, conn=c, prev_intent=prev, history=history)
        m = router.get_last_meta()
        row = c.execute("SELECT count(*) FROM intent_cache WHERE query=?", (text,)).fetchone()[0]
        return got, m, row
    finally:
        c.close()


class _StubLLM:
    """替身：固定返回 (intent, conf)，并记录每次调用收到的 text/context。"""

    def __init__(self, ret):
        self.ret = ret
        self.calls = []

    def __call__(self, text, context=""):
        self.calls.append({"text": text, "context": context})
        return self.ret


class _PatchLLM:
    """with _PatchLLM(rt, ("impact", 0.70)) as stub: ...（退出时还原）"""

    def __init__(self, router, ret):
        self.router = router
        self.stub = _StubLLM(ret)

    def __enter__(self):
        self._orig = self.router.detect_llm
        self.router.detect_llm = self.stub
        return self.stub

    def __exit__(self, *a):
        self.router.detect_llm = self._orig
        return False


def stub_run(router, ret, text=Q_FOLLOW, history=HIST_TOPIC, prev="impact"):
    """确定性运行：LLM 返回固定值。返回 (got, meta, rows, stub)。"""
    with _PatchLLM(router, ret) as stub:
        got, m, rows = run(router, text, history=history, prev=prev)
    return got, m, rows, stub


pipe = AgentPipeline()
pipe._load_db_agents()          # 必须：否则语义索引为空，本测试判据面整体不存在
rt = pipe.router
CTX_TOPIC = rt._sanitize_history(HIST_TOPIC, Q_FOLLOW)
CTX_NOISY = rt._sanitize_history(HIST_NOISY, Q_FOLLOW)
GATE = float(rt._cfg_get("history_llm_min", 0.55))

# ───────────────────────── A. 净化（纯函数，确定性）─────────────────────────
# ⚠️ 判据写成"函数 + 结果字典"，是为了让**基线与被测变异共用同一份判据代码**：
#    初版 A1~A8 是一行行独立 chk()，到 M4 变异自证时只能靠我**手写复刻**一遍净化口径去判定
#    （`"【澄清补充】" in out4` 之类），等于"用我另写的一份逻辑验收源码"——源码被变异、
#    复刻版不变 → 变异自证假绿（本仓老坑）。注意样本刻意给**两条相同**的长句以验去重。
A_INPUT = ["【澄清补充】用户已补充以下建模信息（请据此继续）：问题和回答…",
           "[任务上下文快照] 编排子任务内部文本",
           "帮我看看这个项目的预算",
           "帮我看看这个项目的预算",
           "   ",
           "超长" * 200,
           "对巡飞弹做参数变更影响分析：推进剂加注量与巡飞速度调整，影响深度3层",
           "对巡飞弹做参数变更影响分析：推进剂加注量与巡飞速度调整，影响深度3层",
           "导出最近审计日志"]
A_CUR = "帮我看看这个项目的预算"


def run_A(r):
    """对给定 router 跑一遍 A 组净化断言 —— 基线 rt 与变异体**共用这一份判据**。"""
    f = r._sanitize_history
    out = f(A_INPUT, A_CUR)
    _long = f(["甲" * 200, "乙" * 200, "丙" * 200])
    res = {
        "A1 剔含【澄清补充】的续答文本": "【澄清补充】" not in out,
        "A2 剔编排子任务内部文本（[任务上下文快照]）": "任务上下文快照" not in out,
        "A3 剔超长（> _HISTORY_ITEM_MAX=200）": "超长超长" not in out,
        "A4 剔重复（同句只留一次）": out.count("对巡飞弹做参数变更影响分析") == 1,
        "A5 剔与当前句相同的": "预算" not in out,
        "A6 只取最近 3 条（_HISTORY_MAX_ITEMS）": len([x for x in out.split(" ") if x]) <= 3,
        "A7 空 / None / 非字符串 → 空串": (f(None) == "" and f([]) == ""
                                          and f(["  ", 123, {}]) == ""),
        "A8 总量截断 ≤ _HISTORY_TOTAL_MAX=300": len(_long) <= 300,
    }
    return out, res


print("── A. _sanitize_history 净化（纯函数，零 LLM）──")
a_out, a_res = run_A(rt)
print("     输出 =", repr(a_out))
for _k, _v in a_res.items():
    chk(_k, _v)

# ─────────────────── C. 采纳门（确定性：LLM 用 stub 固定）───────────────────
print("── C. 采纳门（stub LLM，确定性；真值表覆盖每个边界）──")
_g, _m, _r, _stub = stub_run(rt, ("impact", 0.70))
chk("C0 前置：语义层未截断，确已走到 LLM 段（stub 被调用）",
    len(_stub.calls) == 1, f"调用次数={len(_stub.calls)}")
chk("C10 净化后的上文确实喂给了 LLM（context 非空且等于净化串）",
    _stub.calls[0]["context"] == CTX_TOPIC and bool(CTX_TOPIC),
    f"收到={_stub.calls[0]['context'][:36]!r}…")

print(f"     确认路径：got={_g} route={_m.get('route')} conf={_m.get('confidence')} "
      f"confirmed={_m.get('confirmed')} clarify={_m.get('needs_clarification')}")
chk("C7 与 prev 一致 + 弱置信(0.70) → 采纳为 llm_history", _m.get("route") == "llm_history",
    f"实得={_m.get('route')}")
chk("C7b 采纳的意图 == prev_intent（**不可能改变方向**）", _g == "impact", f"实得={_g}")
chk("C7c meta 标记 used_history=True", _m.get("used_history") is True)
chk("C7d 两路一致 → confirmed=True 且不弹「可改选」细条", _m.get("confirmed") is True
    and _m.get("needs_clarification") is False,
    f"confirmed={_m.get('confirmed')} clarify={_m.get('needs_clarification')}")

# 地板值边界（含等号）
_gb, _mb, _, _ = stub_run(rt, ("impact", GATE))
chk(f"C4 地板值边界：conf == {GATE}（等号）→ 采纳（>= 而非 >）",
    _mb.get("route") == "llm_history", f"实得={_mb.get('route')}")
_gb2, _mb2, _, _ = stub_run(rt, ("impact", GATE - 0.001))
chk(f"C4b conf == {GATE - 0.001:.3f}（差一毫）→ 不采纳，回落 inherit",
    _mb2.get("route") == "inherit" and _gb2 == "impact",
    f"route={_mb2.get('route')} got={_gb2}")

# 方向守卫（**核心**）：结论与 prev 不一致 → 一律不采纳
_gn, _mn, _, _ = stub_run(rt, ("knowledge_qa", 0.70), history=HIST_NOISY,
                          prev="report_generation")
print(f"     方向守卫：噪声上文 + stub=knowledge_qa@0.70 + prev=report_generation → "
      f"{_gn}/{_mn.get('route')}")
chk("C6 结论与 prev 不一致 → **不得采纳**（意图恒为 prev，杜绝改向）",
    _mn.get("route") != "llm_history" and _gn == "report_generation",
    f"route={_mn.get('route')} got={_gn}")

# chat 在**弱置信分支**一律不采纳（弱置信分支的守卫含 `llm_intent != "chat"`）
_gc, _mc, _, _ = stub_run(rt, ("chat", 0.70), prev="impact")
chk("C5 弱置信下 LLM 结论为 chat → 不采纳（回落 inherit，不改向）",
    _mc.get("route") != "llm_history" and _gc == "impact",
    f"route={_mc.get('route')} got={_gc}")
# 高置信下的 chat 是**旧行为**（LLM 明确说"就是闲聊"）→ 只加确认标记、不改意图
#   （这条同时是"没顺手改动高置信分支语义"的零回归断言，勿按 C5 的口径误判）
_gch, _mch, _, _ = stub_run(rt, ("chat", 0.95), prev="impact")
chk("C5b 高置信 chat 仍采纳为 chat（旧行为不变），但因结论≠prev → 带确认细条",
    _gch == "chat" and _mch.get("route") == "llm_history"
    and _mch.get("needs_clarification") is True,
    f"got={_gch} route={_mch.get('route')} clarify={_mch.get('needs_clarification')}")

# 无历史 → 与旧行为零差异
_gh, _mh, _, _sh = stub_run(rt, ("impact", 0.70), history=None, prev="impact")
chk("C8 不带历史（history=None）→ 走 inherit，绝不进 llm_history",
    _mh.get("route") == "inherit" and _mh.get("used_history") is False,
    f"route={_mh.get('route')} used={_mh.get('used_history')}")
chk("C8b 无 prev_intent 时连上下文都不带（提示词与旧版逐字节一致 → 零回归）",
    _sh.calls[0]["context"] == "", f"实得={_sh.calls[0]['context']!r}")

# 高置信分支：保留结论但如实标注"是否确认"
_g9, _m9, _r9, _ = stub_run(rt, ("impact", 0.90))
chk("C9 高置信 + 与 prev 一致 → confirmed，不打扰（可见行为与旧版 `llm` 相同）",
    _m9.get("route") == "llm_history" and _m9.get("confirmed") is True
    and _m9.get("needs_clarification") is False,
    f"route={_m9.get('route')} confirmed={_m9.get('confirmed')}")
chk("C9c 高置信 + 有上文 → 不落缓存（防缓存毒化）", _r9 == 0, f"行数={_r9}")
_g9b, _m9b, _, _ = stub_run(rt, ("knowledge_qa", 0.90), prev="report_generation")
print(f"     高置信但结论 ≠ prev：{_g9b}/{_m9b.get('route')} "
      f"clarify={_m9b.get('needs_clarification')}")
chk("C9b 高置信 + 结论 ≠ prev → **必须 needs_clarify=True**（撤回 v1 的静默 route='llm'）",
    _m9b.get("route") == "llm_history" and _m9b.get("needs_clarification") is True
    and _m9b.get("confirmed") is False,
    f"route={_m9b.get('route')} clarify={_m9b.get('needs_clarification')} "
    f"confirmed={_m9b.get('confirmed')}")

# 静默开关可回滚
_orig_cfg = rt._cfg_get               # staticmethod 经实例访问 => 普通函数
try:
    rt._cfg_get = lambda k, d: (False if k == "history_confirm_silent" else _orig_cfg(k, d))
    _go, _mo, _, _ = stub_run(rt, ("impact", 0.70))
finally:
    rt._cfg_get = _orig_cfg
print(f"     history_confirm_silent=False → clarify={_mo.get('needs_clarification')}")
chk("C11 `history_confirm_silent=False` 可回滚（confirmed 路径恢复弹条）",
    _mo.get("route") == "llm_history" and _mo.get("needs_clarification") is True,
    f"route={_mo.get('route')} clarify={_mo.get('needs_clarification')}")

# ─────────────────── B. 缓存防线（确定性：stub）───────────────────
print("── B. 缓存防线：带历史的结论不得按原文写缓存 ──")
chk("B2 带历史采纳的 query **不在** intent_cache（否则下轮无上文会命中它）",
    _m.get("route") == "llm_history" and _r == 0, f"route={_m.get('route')} 行数={_r}")
_gS, _mS, _rS, _ = stub_run(rt, ("impact", 0.90), history=None, prev=None)
chk("B3 对照：不带历史的高置信结论**仍**写缓存（没把缓存整体关掉）",
    _rS == 1, f"route={_mS.get('route')} 行数={_rS}")

# ─────────────────── C-real. 真实 LLM：只观测**不变式** ───────────────────
print("── C-real. 真实 LLM 端到端（**只观测不变式**，不拿抖动当判据）──")
REAL_N = 3
INV = {"quiet": 0, "loud": 0}


def _inv_check(tag, prev, history, n=REAL_N):
    """不变式：**静默（needs_clarification=False 且 route=llm_history）必须只在"意图==prev"时发生**。
    这条不依赖 LLM 给出什么分，因此不受抖动影响。"""
    bad = []
    rows = []
    for _ in range(n):
        g, m, _r = run(rt, Q_FOLLOW, history=history, prev=prev)
        quiet = (m.get("route") == "llm_history" and m.get("needs_clarification") is False)
        rows.append("%s/%s@%.2f%s" % (g, m.get("route"), float(m.get("confidence") or 0),
                                      "" if not quiet else "*"))
        if quiet and g != prev:
            bad.append(rows[-1])
        if quiet:
            INV["quiet"] += 1
        elif m.get("route") == "llm_history":
            INV["loud"] += 1
    print("     %s prev=%-17s : %s" % (tag, prev, "  |  ".join(rows)))
    return bad


_bad_pos = _inv_check("同话题", "impact", HIST_TOPIC)
_bad_neg = _inv_check("噪声  ", "report_generation", HIST_NOISY)
print("     （`*` = 静默采纳；两侧的分差 0.62~0.95 是 LLM 抖动，不作判据）")
chk("R1 真实链路上：静默采纳**只在**意图==prev_intent 时发生（0 例违反）",
    not _bad_pos and not _bad_neg, f"违例={_bad_pos + _bad_neg}")

# ─────────────────── D. meta / 入口重置 / 取历史 / 澄清卡候选 ───────────────────
print("── D. meta 字段、入口重置、会话取历史、澄清卡候选 ──")
chk("D1 meta 含 sem_alt / used_history / confirmed 三键",
    "sem_alt" in _m and "used_history" in _m and "confirmed" in _m, f"keys={sorted(_m)}")
_, mPre, _ = run(rt, Q_BAD)
print(f"     前置：{Q_BAD!r} -> sem_alt={mPre.get('sem_alt')}（用于制造「残留」）")
gR, mR, _ = run(rt, Q_RULE)
print(f"     规则命中 {Q_RULE!r} -> route={mR.get('route')} sem_alt={mR.get('sem_alt')}")
chk("D2 规则命中路径 sem_alt 为 None（入口重置生效，不读上一轮残留）",
    mR.get("sem_alt") is None, f"实得={mR.get('sem_alt')}")
chk("D3 _load_history_for_intent(0) → []", pipe._load_history_for_intent(0) == [])
_pool = pipe._load_history_for_intent(325)
chk("D4 真实会话 325 → 原始池非空，且净化后能拿到有效历史",
    len(_pool) > 0 and bool(rt._sanitize_history(_pool, "那它的风险呢")),
    f"池={len(_pool)} 条")


def _idx_of(opts, kw):
    for i, o in enumerate(opts):
        if kw in o:
            return i
    return 999


_q_no = pipe._intent_confirm_questions("随便说点啥", "chat")
_q_al = pipe._intent_confirm_questions("随便说点啥", "chat", {"intent": "system_mgmt", "score": 0.5})
i_no = _idx_of(_q_no[0]["options"], "系统管理")
i_al = _idx_of(_q_al[0]["options"], "系统管理")
print(f"     「系统管理」在选项里的位次：无 sem_alt -> {i_no} ｜ 有 sem_alt -> {i_al}")
chk("D5 语义意见进澄清卡排序（system_mgmt 位次前移且进前二）",
    i_al < i_no and i_al <= 1, f"无={i_no} 有={i_al}")

# ─────────────────── E. 变异自证 ───────────────────
print("── E. 变异自证（只认「新增 FAIL」；全部走 stub → 判定确定性）──")


def load_src():
    return open(os.path.join(ROOT, "agent", "intent.py"), encoding="utf-8").read().replace("\r\n", "\n")


def make_router(src):
    ns = {}
    exec(compile(src, "<mutant>", "exec"), ns)
    mm = ns["IntentRouter"]()
    mm._semantic_index = rt._semantic_index
    mm._db_intents = dict(rt._db_intents)
    return mm


BASE = load_src()

# M7 的锚点跨 3 行、且续行缩进容易在改动中漂移 → 用正则取**唯一**命中再替换，别硬编码空格数。
import re                                                       # noqa: E402
_M7_HIT = re.findall(
    r'return self\._done\(llm_intent, "llm_history", llm_conf, text, fp,\s*\n'
    r'\s*cacheable=False, used_history=True,\s*\n'
    r'\s*confirmed=\(llm_intent == prev_intent\)\)', BASE)
assert len(_M7_HIT) == 1, "M7 锚点应唯一，实得 %d 个" % len(_M7_HIT)

M_BEHAV = [
    ("M1 不把历史传给 LLM", "C10",
     BASE.replace("self.detect_llm(text, context=_hist_ctx)", "self.detect_llm(text)", 1)),
    # 注意：改 _cfg_get 的字面默认值**无效**（history_llm_min 已在 core/config.py 显式声明）
    # → 必须替换整个表达式。P0-1 的变异踩过同一个坑。
    ("M2 地板门抬到不可达 99", "C7",
     BASE.replace('float(self._cfg_get("history_llm_min", 0.55))', 'float(99.0)')),
    ("M3 去掉 cacheable=False（带历史也写缓存）", "B2",
     BASE.replace("cacheable=False, used_history=True, confirmed=True",
                  "cacheable=True, used_history=True, confirmed=True")),
    # M6/M7/M8：v2 新增的三道守卫，各自配目标断言
    ("M6 删方向守卫（结论不必与 prev 一致）", "C6",
     BASE.replace("\n                        and llm_intent == prev_intent", "", 1)),
    ("M7 撤回高置信堵洞（改回静默 route='llm'）", "C9b",
     BASE.replace(_M7_HIT[0], 'return self._done(llm_intent, "llm", llm_conf, text, fp)')),
    ("M8 confirmed 恒为 True（不一致也静默）", "C9b",
     BASE.replace("confirmed=(llm_intent == prev_intent))", "confirmed=True)", 1)),
]

chk("E0 基线（变异的目标断言此刻均为 PASS，否则变异自证无意义）",
    (len(_stub.calls) == 1 and _stub.calls[0]["context"] == CTX_TOPIC)
    and (_m.get("route") == "llm_history" and _r == 0)
    and (_mn.get("route") != "llm_history" and _gn == "report_generation")
    and (_m9b.get("needs_clarification") is True))

for label, target, src in M_BEHAV:
    assert src != BASE, f"{label}: 变异未生效（替换没命中）"
    mr = make_router(src)
    if target == "C10":
        _, _, _, st = stub_run(mr, ("impact", 0.70))
        fail = st.calls[0]["context"] != CTX_TOPIC
        ev = f"context={st.calls[0]['context']!r}"
    elif target == "C7":
        _, mm, _, _ = stub_run(mr, ("impact", 0.70))
        fail = mm.get("route") != "llm_history"
        ev = f"route={mm.get('route')}"
    elif target == "B2":
        _, mm, rr, _ = stub_run(mr, ("impact", 0.70))
        fail = (mm.get("route") == "llm_history") and (rr != 0)
        ev = f"route={mm.get('route')} cached={rr}"
    elif target == "C6":
        gg, mm, _, _ = stub_run(mr, ("knowledge_qa", 0.70), history=HIST_NOISY,
                                prev="report_generation")
        fail = mm.get("route") == "llm_history" and gg != "report_generation"
        ev = f"route={mm.get('route')} got={gg}"
    else:   # C9b
        _, mm, _, _ = stub_run(mr, ("knowledge_qa", 0.90), prev="report_generation")
        fail = mm.get("needs_clarification") is False
        ev = f"route={mm.get('route')} clarify={mm.get('needs_clarification')}"
    print(f"     {label}: {ev}")
    chk(f"{label} → 目标断言（{target}）新增 FAIL", fail, ev)
    if target in ("C6", "C9b"):
        gg, mm, _, _ = stub_run(mr, ("impact", 0.70))
        chk(f"{label} → 对照项 C8/C7（无历史/一致路径）未受影响",
            mm.get("route") == "llm_history" and gg == "impact",
            f"route={mm.get('route')} got={gg}")

print("── E2. 两个「确定性」变异（就地执行，不依赖 LLM）──")
# M4：让 _sanitize_history **整体失效** —— 函数体前部短路 return，把字符串原样拼回。
#   两个失败过的写法（记下以免重蹈）：
#   ① 只替换**末尾** `return " ".join(_out[...])[:...]` —— 会被前面的 `if not _out: return ""`
#      短路径绕过（首条不合格时 _out 为空，根本走不到末尾）；
#   ② 短路 return 里带 `[:300]` 截断 —— 200 字的"超长"样本把预算吃完，后面的样本被截掉，
#      表现得像"变异没生效"。故此处**不带任何截断**。
M4_SRC = BASE.replace(
    '        if not history:\n'
    '            return ""\n'
    '        _cur = (current_text or "").strip()',
    '        if not history:\n'
    '            return ""\n'
    '        return " ".join([x for x in history if isinstance(x, str)])\n'
    '        _cur = (current_text or "").strip()', 1)
assert M4_SRC != BASE, "M4 变异未生效"
out4, res4 = run_A(make_router(M4_SRC))
_flip4 = [k.split()[0] for k, v in res4.items() if not v]
print("     M4 净化输出 =", repr(out4[:70]) + "…")
print("     M4 下转为 FAIL 的 A 项 =", _flip4)
chk("M4 净化失效 → 目标 A1~A5 全部新增 FAIL（走同一份 run_A 判据，非手写复刻）",
    all(not res4[k] for k in res4 if k.split()[0] in ("A1", "A2", "A3", "A4", "A5")),
    f"实得={_flip4}")
chk("M4 连带 A6~A8 也新增 FAIL（净化是整体失效，本就非定点）",
    all(not res4[k] for k in res4 if k.split()[0] in ("A6", "A7", "A8")),
    f"实得={_flip4}")

M5_SRC = BASE.replace(
    '        #   "本次意见"误用（可行性探针里已踩到：规则命中的那些行，alt 显示的全是上一轮的）。\n'
    '        self._last_sem_score = 0.0\n'
    '        self._last_sem_alt = None',
    '        #   "本次意见"误用（可行性探针里已踩到：规则命中的那些行，alt 显示的全是上一轮的）。\n'
    '        self._last_sem_score = 0.0', 1)
assert M5_SRC != BASE, "M5 变异未生效"
m5 = make_router(M5_SRC)
run(m5, Q_BAD)                     # 先制造一次"走语义段"（设置 _last_sem_alt）
_, mm5, _ = run(m5, Q_RULE)        # 规则命中：不改的话会读到上一轮残留
print(f"     M5 规则命中 -> sem_alt={mm5.get('sem_alt')}")
chk("M5 删入口重置 → D2 新增 FAIL（读到上一轮残留）", mm5.get("sem_alt") is not None,
    f"实得={mm5.get('sem_alt')}")

print(f"\n真实 LLM 观测：静默采纳 %d 次 / 需确认（llm_history 非静默）%d 次"
      % (INV["quiet"], INV["loud"]))
print(f"结果：{len(OK)} 通过 / {len(FAIL)} 失败")
if FAIL:
    print("失败项：\n  " + "\n  ".join(FAIL))
    sys.exit(1)
