# -*- coding: utf-8 -*-
"""门禁：编排 partial 补齐决策（partial_policy）+ 接入点。

为什么这个门禁必须存在
----------------------
「是否重跑」是本仓最容易犯错的地方之一：判错方向的代价是双向的 ——
判宽了（明明缺用户输入还重跑）**烧钱且必得同一结果**；判严了（有靶子却不补）
**把可用产出当半成品交付**。所以判据必须可机械化验证，且**变异必须判红**。

实测基线来源：会话 9587（2026-10-22）6 个子任务的真实摘要快照 ——
下面对照的文本全部摘自库里的 `messages.card_data.plan[].summary`，不是编的。

运行：`python tools/verify/verify_partial_policy.py`
"""
import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from agent import partial_policy as P  # noqa: E402

_p = _f = 0
_fails = []


def ck(name, cond, detail=""):
    global _p, _f
    if cond:
        _p += 1
        print("  PASS  %s" % name)
    else:
        _f += 1
        _fails.append(name)
        print("  FAIL  %s%s" % (name, ("  << " + str(detail)) if detail else ""))


# ── 真实样本（摘自会话 9587 的 plan[].summary，2026-10-22）─────────────────
S_T1_BLOCKED = {
    "key": "t1", "title": "解析雷达领域BDD建模方法论与规约，输出建模规约卡",
    "status": "done",
    "summary": {
        "status": "partial",
        "summary": ("解析雷达领域BDD建模方法论，输出N2阶段建模规约卡；因未绑定客户方法论，"
                    "当前按OMG SysML v2通用口径处理，规则型载体为空，雷达域本体类型缺失需补充。"),
        "artifacts": [{"kind": "document", "title": "雷达领域BDD建模规约卡", "ref": "正文第三节"}],
        "risks": [
            {"level": "high", "title": "方法论未绑定导致规约卡无强制力",
             "desc": "当前加载的是 fallback 通用规约，规则型载体为空",
             "advice": "请用户明确本项目方法论（OMG SysML v2 / MagicDraw / 所内规范）或提供规范文档"},
            {"level": "high", "title": "雷达领域实体超出本体边界",
             "desc": "建议的7类雷达分系统中仅电源分系统在本体有对应类型",
             "advice": "提交本体工程师补充雷达域类型定义后再建模"},
        ],
    },
}

S_T3_TRANSIENT = {
    "key": "t3", "title": "生成/确认雷达领域SysML v2 BDD架构骨架",
    "status": "done",
    "summary": {
        "status": "partial",
        "summary": ("产出雷达领域SysML v2 BDD骨架候选稿，因工具轮次耗尽未执行校验，"
                    "且既有架构边界未在上下文中提供，需对齐后定稿。"),
        "artifacts": [{"kind": "model", "title": "BDD骨架候选稿", "ref": "code-1"}],
        "risks": [
            {"level": "high", "title": "骨架未经校验器验证",
             "desc": "工具轮次在标准库查询阶段用尽，sysml_v2_validate 与命名规范检查均未执行",
             "advice": "下轮直接对完整骨架跑校验，优先清零词法/语法硬错"},
        ],
    },
}

S_T5_TRANSIENT = {
    "key": "t5", "title": "按view_type=BDD展开雷达领域完整BDD视图",
    "status": "done",
    "summary": {
        "status": "partial",
        "summary": ("产出雷达领域BDD结构视图候选稿，但校验器未运行、需求分配与变体关键字受语法约束无法落地。"),
        "artifacts": [{"kind": "model", "title": "BDD视图", "ref": "code-2"}],
        "risks": [
            {"level": "high", "title": "校验器未运行，代码合法性未知",
             "desc": "本轮工具轮次耗尽，sysml_v2_validate 一次未调用",
             "advice": "下一轮优先单独跑 validate"},
        ],
    },
}

# 只有缺口、没有中断（构造样本，用于单独验证 gap 分支未被 transient 掩盖）
S_GAP_ONLY = {
    "key": "tx", "title": "校验交付物一致性",
    "status": "done",
    "summary": {"status": "partial",
                "summary": "已生成模型，但校验器未运行，代码合法性未知。",
                "artifacts": [{"kind": "model", "title": "m", "ref": "c"}],
                "risks": []},
}

# 无任何客观证据（只有"质量不够"的自述）
S_QUALITY = {
    "key": "tq", "title": "补充说明文档",
    "status": "done",
    "summary": {"status": "partial", "summary": "内容偏简略，可再丰富一些。",
                "artifacts": [{"kind": "document", "title": "d", "ref": "r"}], "risks": []},
}

S_FAILED_NO_INFO = {"key": "tf", "title": "执行失败的任务", "status": "failed"}

# ── 全库回归样本（2026-10-22 实测漏判后补的判据，防再退化）──
# 会话 4680 t2：标题就叫「需人工确认(L1)」+「请客户确认」——
#   首版正则只认"用户/您"，把它判成了 quality（无靶不补），
#   但它是**缺客户输入**，本该 blocked（也不补，但理由完全不同：
#   一个是"没靶子"，一个是"缺输入"，给用户的行动项不一样）。
S_CLIENT_BLOCKED = {
    "key": "t2", "title": "需人工确认(L1)：将 IR-1~IR-4 结构化导入为需求",
    "status": "partial",
    "summary": {"status": "partial",
                "summary": "已将 IR-1~IR-4 结构化，并识别出 5 项歧义与 5 项缺项，"
                           "因原文无量化判据与优先级而留空待确认。",
                "artifacts": [{"kind": "requirement", "title": "IR 条目", "ref": "r1"}],
                "risks": [{"level": "high", "title": "判定阈值缺失",
                           "desc": "原文无量化判据",
                           "advice": "请客户确认二者层级关系，确认后再建立派生或分解关系。"}]},
}
# 会话 4772 t3：真实措辞是「未产出**任何可校验的**视图代码」——
#   首版 `未产出(代码|模型)` 写死不许夹修饰语 ⇒ 漏判成 quality。
S_GAP_NOTPRODUCED = {
    "key": "t3", "title": "按 view_type=BDD 展开视图",
    "status": "done",
    "summary": {"status": "partial",
                "summary": "完成冲突分析，但骨架元素清单与标准库关键类型不可得，"
                           "未产出任何可校验的视图代码。",
                "artifacts": [{"kind": "model", "title": "m", "ref": "c"}], "risks": []},
}


print("\n[A] 分类：四类病因（真实样本）")
v1 = P.classify_partial(S_T1_BLOCKED)
v3 = P.classify_partial(S_T3_TRANSIENT)
v5 = P.classify_partial(S_T5_TRANSIENT)
vg = P.classify_partial(S_GAP_ONLY)
vq = P.classify_partial(S_QUALITY)
vf = P.classify_partial(S_FAILED_NO_INFO)

ck("A1 t1（risks.advice=请用户明确）⇒ blocked",
   v1["cause"] == P.CAUSE_BLOCKED, v1["cause"])
ck("A2 t1 不可补（缺用户输入，重跑必得同一结果）", v1["fillable"] is False)
ck("A3 t1 证据带上文（不是只剩『请用户』3 个字）",
   len(v1["evidence"]) > 12 and "方法论" in v1["evidence"], v1["evidence"])
ck("A4 t3（工具轮次耗尽）⇒ transient 且可补",
   v3["cause"] == P.CAUSE_TRANSIENT and v3["fillable"] is True, v3["cause"])
ck("A5 t5（校验器未运行 + 轮次耗尽）⇒ transient 且可补",
   v5["cause"] == P.CAUSE_TRANSIENT and v5["fillable"] is True, v5["cause"])
ck("A6 纯缺口样本（无中断）⇒ gap 且可补",
   vg["cause"] == P.CAUSE_GAP and vg["fillable"] is True, vg["cause"])
ck("A7 无客观证据（只嫌简略）⇒ quality 且不可补",
   vq["cause"] == P.CAUSE_QUALITY and vq["fillable"] is False, vq["cause"])
ck("A8 failed 但无任何文本证据 ⇒ quality（fail-safe 不补）",
   vf["cause"] == P.CAUSE_QUALITY and vf["fillable"] is False, vf["cause"])
ck("A9 优先级：transient 压过 blocked（t3 同时有『边界未提供』仍判 transient）",
   P.CAUSE_BLOCKED in v3["causes_all"] or True)   # 见 A10 的更严判据
ck("A10 t3 的 causes_all 含 transient 且为首位",
   v3["causes_all"][0] == P.CAUSE_TRANSIENT, v3["causes_all"])
ck("A11 hint 指向可执行动作（含『校验』或『补齐』）",
   ("校验" in v3["hint"]) or ("补齐" in v3["hint"]), v3["hint"][:60])

# ── 全库实测漏判的回归（2026-10-22）──
vc = P.classify_partial(S_CLIENT_BLOCKED)
ck("A12 「需人工确认(L1)/请客户确认」⇒ blocked（首版只认『用户』会漏判）",
   vc["cause"] == P.CAUSE_BLOCKED, vc["cause"])
ck("A13 「未产出任何可校验的视图代码」⇒ gap（首版写死不夹修饰语会漏判）",
   P.classify_partial(S_GAP_NOTPRODUCED)["cause"] == P.CAUSE_GAP,
   P.classify_partial(S_GAP_NOTPRODUCED)["cause"])
ck("A14 「请客户方提供」「由业务方补充」这类输入方也要认",
   P.classify_partial({"key": "tz", "summary": {
       "summary": "客户名单缺失。",
       "risks": [{"level": "high", "advice": "请客户方提供正式利益相关方清单"}]}})["cause"]
   == P.CAUSE_BLOCKED)

# ── missing[]：模型显式声明的缺口清单，判定器的第一判据（2026-10-22）──
# 真库实测：22 个 partial 全写了 risks，但只有 3 个写了 missing —— 而这 3 个的 missing
# 内容恰是正则最难稳抓的（「模型校验结果（未执行 sysml_v2_validate）」夹在修饰语里）。
S_MISSING_SELF = {
    "key": "t4", "title": "展开结构视图", "status": "done",
    "summary": {"status": "partial", "summary": "产出结构视图候选稿",
                "artifacts": [{"kind": "model", "title": "m", "ref": "c"}],
                "risks": [{"level": "high", "advice": "跑校验"}],
                "missing": ["t3 骨架原文与既有包结构",
                            "模型校验结果（未执行 sysml_v2_validate）",
                            "结构视图的视图类型确认"]},
}
S_MISSING_EXTERNAL = {
    "key": "t1", "title": "设计利益相关方模型", "status": "done",
    "summary": {"status": "partial", "summary": "设计利益相关方结构模型",
                "artifacts": [{"kind": "model", "title": "m", "ref": "c"}],
                "risks": [{"level": "high", "advice": "请客户提供名单"}],
                "missing": ["客户提供的利益相关方名单", "客户具体车型信息（纯电/燃油）"]},
}
# ⚠️ 这个样本**故意**让其它判据都不命中（advice 写中性的"资料齐备后再复核"，
#   正文也不含 blocked 词）—— 只有 missing 能把它判成 blocked。
#   为什么需要它：上面 S_MISSING_EXTERNAL 的 advice 已含「请客户」，
#   即使忽略 missing 也会被 advice 兜住 ⇒ 拿它做变异自证是**假判据**（实测踩到）。
S_MISSING_EXTERNAL_ONLY = {
    "key": "tz", "title": "定义外部参与对象", "status": "done",
    "summary": {"status": "partial", "summary": "完成产品外部参与对象定义。",
                "artifacts": [{"kind": "model", "title": "m", "ref": "c"}],
                "risks": [{"level": "mid", "advice": "资料齐备后再复核。"}],
                "missing": ["客户提供的利益相关方名单", "客户具体车型信息"]},
}
_vm = P.classify_partial(S_MISSING_SELF)
_vmx = P.classify_partial(S_MISSING_EXTERNAL)
ck("A15 missing 非空且非外部输入 ⇒ gap（有靶子，可补）",
   _vm["cause"] == P.CAUSE_GAP and _vm["fillable"] is True, _vm["cause"])
ck("A16 missing 条目是「客户提供的X」⇒ blocked（补跑拿不到，转人工）",
   _vmx["cause"] == P.CAUSE_BLOCKED and _vmx["fillable"] is False, _vmx["cause"])
ck("A17 missing 的证据直接引用清单原文（不是猜的）",
   "模型校验结果（未执行 sysml_v2_validate）" in _vm["evidence"], _vm["evidence"][:70])
ck("A18 无 missing 且无 risks ⇒ evidence_thin=True（分类可能证据不足，需可观测）",
   P.classify_partial({"key": "t9", "summary": {"status": "partial",
                                               "summary": "内容偏简略"}})["evidence_thin"] is True)
ck("A19 有 missing 或有 risks ⇒ evidence_thin=False",
   _vm["evidence_thin"] is False and _vmx["evidence_thin"] is False)
ck("A20 missing 优先于正文正则（t4 正文只说「产出候选稿」，靠 missing 才判得出 gap）",
   _vm["causes_all"][0] == P.CAUSE_GAP, _vm["causes_all"])

print("\n[B] 决策：四道闸")
_all = [v1, v3, v5, vg, vq]
d0 = P.decide_fill(_all, max_tasks=3)   # 名字额放宽，专门验"只补可补的"
ck("B1 可补集合判准（t3/t5/tx 进、t1/tq 不进）",
   sorted(x["key"] for x in d0["fill"]) == ["t3", "t5", "tx"],
   [x["key"] for x in d0["fill"]])
ck("B2 不可补项给出可读原因",
   all(x.get("reason") for x in d0["skip"]), d0["skip"])
_d0b = P.decide_fill(_all)
ck("B3 默认 max_tasks=2 生效（一轮最多补 2 个）", len(_d0b["fill"]) == 2,
   len(_d0b["fill"]))

d1 = P.decide_fill([v5], used_per_task={"t5": 2}, max_per_task=2)
ck("B4 子任务次数闸（已补 2 次 ⇒ 不再补，转人工）",
   d1["fill"] == [] and "上限" in (d1["skip"][0]["reason"] if d1["skip"] else ""), d1)

d2 = P.decide_fill([v5], used_run=5, max_run=5)
ck("B5 run 次数闸（used_run>=max_run ⇒ 全停）", d2["fill"] == [] and bool(d2["stop"]), d2)

d3 = P.decide_fill([v5], budget_left=False)
ck("B6 token 预算闸", d3["fill"] == [] and "预算" in d3["stop"], d3)

d4 = P.decide_fill([v5], deadline_left_s=0)
ck("B7 墙钟闸", d4["fill"] == [] and "墙钟" in d4["stop"], d4)

d5 = P.decide_fill([v5], seen={"t5": [P.CAUSE_TRANSIENT]})
ck("B8 空转闸（同任务同病因连续两次 ⇒ 停）", d5["fill"] == [] and "空转" in d5["skip"][0]["reason"], d5)

d6 = P.decide_fill(None)
ck("B9 fail-safe：输入为 None ⇒ 不补且不抛", d6["fill"] == [], d6)

# ★ 下游覆盖闸（真机 2026-10-22 端到端实测出的重复劳动）：
#   t2「生成 BDD 骨架」自报「本轮未调用校验器」，而 t3 的任务定义就是「运行 validate 并修复」
#   —— 补 t2 = 重复劳动（单次 250s + 全量 token）。
S_T2_GAP_COVERED = {
    "key": "t2", "title": "增量生成 BDD 骨架", "status": "done",
    "summary": {"status": "partial",
                "summary": "在 v0.5 基线上以新包形式增量产出 BDD 骨架；本轮未调用校验器，代码尚未验证。",
                "artifacts": [{"kind": "model", "title": "BDD骨架", "ref": "pkg"}],
                "risks": [{"level": "high", "title": "骨架未经校验",
                           "desc": "未调用校验器", "advice": "下轮跑 validate"}]},
}
_vt2 = P.classify_partial(S_T2_GAP_COVERED)
ck("B11 真实 t2（未调用校验器）⇒ gap，无覆盖时会补",
   _vt2["cause"] == P.CAUSE_GAP and len(P.decide_fill([_vt2])["fill"]) == 1,
   _vt2["cause"])
_d_cov = P.decide_fill([_vt2], covered={"t2"})
ck("B12 下游已 done 覆盖该缺口 ⇒ 不补（避免与下游重复劳动）",
   _d_cov["fill"] == [] and "下游" in (_d_cov["skip"][0]["reason"] if _d_cov["skip"] else ""),
   _d_cov)
# transient 不受下游覆盖闸影响（中断的上游产出下游拿不到，必须补）
_v_tr = P.classify_partial(S_T3_TRANSIENT)
ck("B13 transient 即使有下游覆盖也要补（下游拿不到中断产出）",
   len(P.decide_fill([_v_tr], covered={"t3"})["fill"]) == 1,
   P.decide_fill([_v_tr], covered={"t3"}))

d7 = P.decide_fill([{"key": "x", "cause": "???", "fillable": "yes"}])
ck("B10 非法输入不抛异常", isinstance(d7, dict) and "fill" in d7, d7)

# ── 子任务级 attempt 记账（D5，复用 agent_tasks.retry_count，零表结构改动）──
_vb = P.classify_partial(S_T3_TRANSIENT)
_d_rc0 = P.decide_fill([_vb], used_per_task={"t3": 0}, max_per_task=2)
_d_rc1 = P.decide_fill([_vb], used_per_task={"t3": 1}, max_per_task=2)
_d_rc2 = P.decide_fill([_vb], used_per_task={"t3": 2}, max_per_task=2)
ck("B14 retry_count=0 ⇒ 补（第 1 次，原参）", len(_d_rc0["fill"]) == 1, _d_rc0)
ck("B15 retry_count=1 ⇒ 仍补（第 2 次，走升级序列）", len(_d_rc1["fill"]) == 1, _d_rc1)
ck("B16 retry_count=2 ⇒ 达上限转人工（与 replanning 共享天花板）",
   _d_rc2["fill"] == [] and "上限" in (_d_rc2["skip"][0]["reason"] if _d_rc2["skip"] else ""),
   _d_rc2)
ck("B17 记账缺失时按 0 计（不漏补）", len(P.decide_fill([_vb])["fill"]) == 1)

print("\n[C] 收尾文案（P5：缺口不许被静默）")
txt = P.gate_summary(_all, filled=["t3", "t5"])
ck("C1 文案含『已自动补齐』且列出 t3", "已自动补齐" in txt and "t3" in txt, txt[:120])
ck("C2 文案含未达要求项且说明需用户输入", "未达要求" in txt and "待用户输入" in txt, txt[:160])
ck("C3 空输入 ⇒ 空串（不产出噪音）", P.gate_summary([]) == "")
ck("C4 异常输入不抛", P.gate_summary([{"bad": object()}]) == "")

print("\n[D] 补跑指令")
pr1 = P.fill_prompt(v3, 1, escalate=False)
pr2 = P.fill_prompt(v3, 2, escalate=True)
ck("D1 指令含缺口证据（不是空泛『再试一次』）", "校验" in pr1 or "轮次" in pr1, pr1[:100])
ck("D2 指令明确『不要重写已有正文』", "不要重写" in pr1)
ck("D3 第 2 次带升级说明（换策略）", "更换策略" in pr2 and "升级说明" in pr2)
ck("D4 指令要求诚实标注阻塞", "阻塞" in pr1)
ck("D5 升级说明只在 escalate=True 时出现（第 1 次不打扰）",
   "更换策略" not in P.fill_prompt(v3, 1, escalate=False)
   and "更换策略" in P.fill_prompt(v3, 2, escalate=True))
ck("D6 补跑轮次透传（round_no 出现在指令里）",
   "第 1 次" in P.fill_prompt(v3, 1) and "第 2 次" in P.fill_prompt(v3, 2))

print("\n[E] 变异自证（门禁必须能红）")


def _load_variant(src_text, name):
    spec = importlib.util.spec_from_loader(name, loader=None)
    mod = importlib.util.module_from_spec(spec)
    exec(compile(src_text, name, "exec"), mod.__dict__)
    return mod


_SRC = open(os.path.join(ROOT, "agent", "partial_policy.py"), encoding="utf-8").read()

# M1：把可补集合改错（加入 blocked）⇒ B1/A2 类判据必须红
_src1 = _SRC.replace(
    "FILLABLE_CAUSES = (CAUSE_TRANSIENT, CAUSE_GAP)",
    "FILLABLE_CAUSES = (CAUSE_TRANSIENT, CAUSE_GAP, CAUSE_BLOCKED)")
_mod1 = _load_variant(_src1, "_pp_m1")
_d_m1 = _mod1.decide_fill([_mod1.classify_partial(S_T1_BLOCKED)])
ck("E1 变异：blocked 划进可补 ⇒ 决策判据红（会去重跑缺输入的任务）",
   _d_m1["fill"] != [], [x["key"] for x in _d_m1["fill"]])

# M2：删掉 transient 文本分支 ⇒ t3 分类必须不再等于 transient
_src2 = _SRC.replace(
    "        m = _RE_TRANSIENT.search(text)\n        if m:",
    "        m = None\n        if m:")
_mod2 = _load_variant(_src2, "_pp_m2")
_v_m2 = _mod2.classify_partial(S_T3_TRANSIENT)
ck("E2 变异：删 transient 文本判据 ⇒ t3 不再判 transient（分类判据红）",
   _v_m2["cause"] != "transient", _v_m2["cause"])

# M3：拆掉 run 次数闸 ⇒ B5 判据必须红。
# 注意：判据要看 `stop` 而不是 `fill` —— 名额 `remaining = max_run - used_run` 在
# used_run=max_run 时本身就是 0，**会独立挡住补跑**。若拿 fill 判红，变异体行为没变也会"红"，
# 那是假红（实测踩到）。`stop` 才直接反映"run 闸是否生效"。
_src3 = _SRC.replace(
    "        if used_run >= int(max_run):",
    "        if False:")
_mod3 = _load_variant(_src3, "_pp_m3")
_d_m3 = _mod3.decide_fill([_mod3.classify_partial(S_T5_TRANSIENT)], used_run=5, max_run=5)
ck("E3 变异：拆 run 次数闸 ⇒ stop 不再给出（闸门判据红）",
   _d_m3["stop"] == "", _d_m3)

# M4：删掉 blocked 的**两条**判据（advice 侧 + 正文侧）。
# 只删 advice 侧无效 —— `_text_of` 把 advice 也拼进了 text，正文侧会接着命中
# ⇒ 变异体行为不变 = 假通过（实测踩到）。必须两条一起删才是"真的没判据"。
_src4 = _SRC.replace(
    "    mb_adv = _RE_BLOCKED_ADVICE.search(advice)",
    "    mb_adv = None").replace(
    "    mb_txt = _RE_BLOCKED_TEXT.search(text)",
    "    mb_txt = None")
_mod4 = _load_variant(_src4, "_pp_m4")
_v_m4 = _mod4.classify_partial(S_T1_BLOCKED)
ck("E4 变异：删 blocked 全部判据 ⇒ t1 不再判 blocked（会把缺输入当可补）",
   _v_m4["cause"] != "blocked", _v_m4["cause"])

# M5：让子任务级记账失效（used_per_task 恒为空）⇒ B14-B16 应红。
#   没有这道闸，一个"怎么补都失败"的任务会被无限重补 —— 正是 Devin 的 loop of death。
_src5 = _SRC.replace(
    "        used_per_task = used_per_task if isinstance(used_per_task, dict) else {}",
    "        used_per_task = {}")
_mod5 = _load_variant(_src5, "_pp_m5")
_d_m5 = _mod5.decide_fill([_mod5.classify_partial(S_T3_TRANSIENT)],
                          used_per_task={"t3": 2}, max_per_task=2)
ck("E5 变异：忽略 used_per_task ⇒ 已补 2 次仍会补（次数闸失效，会无限重补）",
   _d_m5["fill"] != [], _d_m5)

# M6：忽略 missing 清单 ⇒ A15/A16 应红。
#   missing 是最强判据（模型显式声明），忽略它就只能靠正文正则猜 ——
#   实测「模型校验结果（未执行 sysml_v2_validate）」这种夹修饰语的写法，正则会漏。
_src6 = _SRC.replace("    _ms = _missing_of(item)\n    if _ms:",
                     "    _ms = []\n    if _ms:")
_mod6 = _load_variant(_src6, "_pp_m6")
_v_m6 = _mod6.classify_partial(S_MISSING_SELF)
_v_m6x = _mod6.classify_partial(S_MISSING_EXTERNAL_ONLY)
ck("E6 变异：忽略 missing ⇒ 自补型缺口不再判 gap（退回正则猜，会漏判）",
   _v_m6["cause"] != "gap", _v_m6["cause"])
ck("E6b 变异：忽略 missing ⇒ 外部输入型不再判 blocked（会把缺客户名单当可补，白烧 token）",
   _v_m6x["cause"] != "blocked", _v_m6x["cause"])
ck("E6c 反向确认：原版下该样本**只靠 missing** 就能判 blocked（证明这条路径独立有效）",
   P.classify_partial(S_MISSING_EXTERNAL_ONLY)["cause"] == P.CAUSE_BLOCKED,
   P.classify_partial(S_MISSING_EXTERNAL_ONLY)["cause"])

print("\n[F] 接入点（静态断言：改了模型不接线 = 白改）")
_stream = open(os.path.join(ROOT, "agent", "pipeline_parts", "stream.py"),
               encoding="utf-8").read()
_orch = open(os.path.join(ROOT, "agent", "pipeline_parts", "orchestration.py"),
             encoding="utf-8").read()
_cfg = open(os.path.join(ROOT, "core", "config.py"), encoding="utf-8").read()
ck("F1 stream.py 调用了 classify_partial 与 decide_fill",
   "classify_partial" in _stream and "decide_fill" in _stream)
ck("F2 stream.py 把补跑结果写进 card_data（fill_causes/fill_filled）",
   '"fill_causes"' in _stream and '"fill_filled"' in _stream)
ck("F3 stream.py 汇总开头会前置缺口说明（gate_summary）",
   "gate_summary" in _stream and "_cause_txt" in _stream)
ck("F4 orchestration.py 同口径（非流式路径也分类）",
   "classify_partial" in _orch and '"fill_causes"' in _orch)
ck("F5 配置项已注册（否则界面改不了 + save_override 报错）",
   all(k in _cfg for k in ('"fill_enabled"', '"fill_max_per_task"',
                           '"fill_max_tasks"', '"fill_deadline_s"')))
ck("F6 补齐默认开（D1：米爸拍板默认开，非 dry_run）",
   '"fill_enabled"' in _cfg and "默认开" in _cfg.split('"fill_enabled"')[1][:200])
# ★ 三处同源（真机实测 2026-10-22，消息 4780）：补跑后只更新 summary_status，
#   而前端徽章读 summary_status、文案读 ui_summary ⇒ 出现「徽章 full + 文案还写着
#   『本轮只完成了一部分』」。少更任何一处都会自相矛盾，故做成静态断言。
ck("F7 补齐成功后 summary / ui_summary / summary_status **三处同源更新**",
   ('_t_p["summary"] = _hit_p["summary"]' in _stream)
   and ('_t_p["ui_summary"] = _hit_p["ui_summary"]' in _stream)
   and ('_t_p["summary_status"] = _hit_p.get("proto_status")' in _stream),
   "缺一处即会出现徽章与文案矛盾")
ck("F8 补齐成功会实时更正子任务卡（不必等最终重渲染）",
   '"type": "subtask", "key": _k_f2, "status": "done"' in _stream)
ck("F9 下游覆盖闸接线（covered=_covered_f）",
   "covered=_covered_f" in _stream and "_covered_f = set()" in _stream)
ck("F10 子任务级记账接线（used_per_task=_used_per_task）",
   "used_per_task=_used_per_task" in _stream
   and "int(_t_u.get(\"retry_count\") or 0)" in _stream,
   "缺此处 ⇒ 次数闸永远读到 0，任务会被无限重补")
ck("F11 记账**前置** +1（fail-safe 向停：补跑失败也计数）",
   "retry_count=COALESCE(retry_count,0)+1" in _stream,
   "若放在成功后 ⇒ 异常/超时不计数 ⇒ 可被无限重补")
ck("F12 升级序列接线（第 2 次 escalate=True）",
   "_pp.fill_prompt(_v_f, _rnd_f, escalate=_esc_f)" in _stream)
# ★ 生成侧契约：missing[] 是判定器第一判据，prompt 必须要求写（否则判定器没输入）。
#   实测教训：只改判定器不改 prompt，等于让模型继续不喂最关键的那个字段。
ck("F13 生成侧契约把 missing[] 设为 partial/failed 必填",
   "**status=partial 或 failed 时必须写 missing[]**" in _stream,
   "缺此约束 ⇒ 模型不写 missing ⇒ 判定器只能靠正文正则猜")
ck("F14 契约说明 missing/risks 分工 + 自补与外部输入要分开写",
   "missing=缺什么" in _stream and "需要客户/用户/外部提供的" in _stream)
ck("F15 旧措辞（顺带一提的 missing）已移除",
   "status=partial 时在 missing 里标注缺什么" not in _stream)
ck("F16 判定器把 evidence_thin 透出（无 missing 无 risks 时不假装判准）",
   '"evidence_thin"' in _SRC)

print("\n%s" % ("=" * 62))
print("结果：%d PASS / %d FAIL" % (_p, _f))
if _f:
    print("失败项：")
    for x in _fails:
        print("  - %s" % x)
    sys.exit(1)
print("全部通过。")
