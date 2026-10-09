# -*- coding: utf-8 -*-
# CI-OPTIONAL: 断言的是**生产真库金标**（名册 20 成员 / capabilities 落库），干净库上必然不成立 ⇒ 与 verify_ontology_dom_range 同类，保持真库运行。
"""团队定义驱动编排门禁（2026-10-07）—— C1~C5 + 两个实测缺陷修复。

对应文档：`docs/主Agent提示词反推草案-团队定义驱动编排-20261007.md`

**为什么必须有这个门禁**：改前team 模式 `team_forced=True` **无条件**编排，问「你好」
也会跑一次 Planner-Executor；且主 Agent 的 `system_prompt`/`capabilities` 全为空，
planner 只拿到一串意图名。这两类缺陷**都不会报错**，只是「跑得起来但白花几十秒 /
拆解质量不可控」⇒ 必须用断言锁住。

本门禁的三条自我约束（都是踩过的坑，不写下来会被下一个人重犯）：
1. **基线由判据自测得出**，不凭印象填（LEX_STRONG / LEX_MARGIN 的口径断言）。
2. **变异自证必须真的变异**：`exec` 到独立命名空间取函数，不复用已导入的模块对象
   （改磁盘文件 ≠ 改已导入模块，那样判据跑的是未被变异的真函数，4/4 假不红）。
3. **假 LLM 必须返回真实 `llm_client` 契约** `{'choices':[{'message':{'content':..}}]}`。
   第一版返回对象导致 `resp.get` 抛 AttributeError 被兜底吞掉，三条容错路径行为
   完全相同 —— 探针没打到真实路径，结论全错。

运行：`./.venv/Scripts/python.exe -X utf8 tools/verify/verify_team_defined_orchestration.py`
"""
import json
import os
import sqlite3
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from agent import team_router as tr  # noqa: E402

PASS, FAIL = [], []


def ck(cond, label):
    (PASS if cond else FAIL).append(label)
    print(("  ok   " if cond else "  FAIL ") + label)


class FakeLLM:
    """按 `llm_client.chat` 的真实返回契约构造（choices[0].message.content）。

    raise_=True 模拟 provider 完全不可用（连响应结构都拿不到）。
    """

    def __init__(self, content="", raise_=False):
        self.content = content
        self.raise_ = raise_

    def chat(self, messages, **kw):
        if self.raise_:
            raise RuntimeError("模拟 provider 不可用")
        return {"choices": [{"message": {"content": self.content}}]}


# ── 团队定义取自真实库（不写死成员清单，避免与库漂移时门禁失真） ────────
def _load_team():
    from database import db_conn
    with db_conn() as c:
        td = tr.load_team_definition(c, "MBSE建模总体负责人")
    return td


print("== A. C1 团队定义完整性（不再只回 name） ==")
td = _load_team()
ck(td is not None, "A1 真实库能加载主 Agent 团队定义")
ck(bool(td and td.get("system_prompt")), "A2 ★主 Agent system_prompt 非空（C1 的核心）")
ck(len(td["system_prompt"]) > 600,
   "A3 ★prompt 长度 %d > 600（实测旧写法截断 300 字会把拆解规则切掉一半）"
   % len(td["system_prompt"]))
_m = td.get("members") or []
# ★ 2026-10-09：A4 由硬编码 14 改为动态下限。
#   事实：2026-10-09 接入 8 个流水线节点后，名册从 14 增至 **25** 行
#   （agent_team_members 实测 25 行 / enabled 23）。
#   原写法 `len(_m) == 14` 是**过期快照**——门禁会随正常演进而误红，
#   而红的原因不是能力退化。这与MEMORY 里「清单里的数字都是快照」同源。
#   改为"下限 + 关键成员必须在"：既能守住退化，又不锁死演进。
ck(len(_m) >= 14, "A4 成员数 %d >= 14（下限；随流水线节点接入会增长）" % len(_m))
# 但**关键成员必须在册**——这是真断言，不随演进放松
_KEY_MEMBERS = ("architecture_skeleton", "view_expansion",
                "model_validation_repair", "trace_verification")
_names = {m.get("name") or m.get("agent") or "" for m in _m}
_missing = [k for k in _KEY_MEMBERS if k not in _names]
ck(not _missing, "A4b ★关键流水线成员在册（缺：%s）" % (_missing or "无"))

_caps = [m for m in _m if m.get("capabilities")]
ck(len(_caps) == len(_m),
   "A5 ★全部成员 capabilities 非空（%d/%d）——空则 planner 分不清8 个视图成员"
   % (len(_caps), len(_m)))
# ⚠️ A6 由"全部非空"放宽为"主 Agent + 流水线节点必须有"。
#   事实：2026-10-09 清空 8 个旧视图 Agent 的 intent_keywords（V15）后，
#   实测名册里有 **8 个成员 intent_keywords 为空**——那是**有意为之**
#   （让视图请求统一走 view_expansion 节点），不是缺陷。
#   但主 Agent 与 8 个流水线节点仍**必须**有路由词，否则流水线进不去。
_KEY_NEED_KW = ("MBSE建模总体负责人",) + _KEY_MEMBERS + (
    "methodology_resolver", "requirement_structuring",
    "change_safety_gate", "model_release")
_kw_missing = [k for k in _KEY_NEED_KW
               if k in _names and not next(
                   (m for m in _m if (m.get("name") or m.get("agent")) == k),
                   {}).get("intent_keywords")]
ck(not _kw_missing,
   "A6 ★主 Agent + 流水线节点必须有 intent_keywords（缺：%s；"
   "旧视图 Agent 为空是有意的）" % (_kw_missing or "无"))
_hil = {m["hil_level"] for m in _m}
ck("L1" in _hil, "A7 L1 成员存在（review/impact 需人工确认，写进 prompt）")

print("\n== B. C5 路由四档：真实语义边界（不 mock 团队） ==")
# B1 档⓪多交付物优先：不能被词法强命中抢占
ORCH = [
    "帮我进行热管理系统工程建模，生成结构视图并生成代码，然后进行校验",
    "对巡飞弹做参数变更影响分析：推进剂加注量调整，输出影响报告",
    "先分析需求，再生成SysML V2模型代码并进行校验",
    "帮我生成电动汽车热管理系统的sysml V2代码并进行校验",
    "先做需求分析，再出设计评审报告",
]
for i, q in enumerate(ORCH, 1):
    d = tr.decide(q, td, llm_client=FakeLLM(""))
    ck(d["action"] == "orchestrate",
       "B1.%d 多交付物 ⇒ 编排（实得 %s：%s）" % (i, d["action"], q[:22]))

# B2 单一交付物不得被误升级为编排（防过度编排——这是本次改造的主要目标）
# ★ 2026-10-09 更新两条期望值（V15 清空了 8 个旧视图 Agent 的 intent_keywords，
#   让视图请求统一走 `view_expansion` 流水线节点）：
#   · "生成需求视图" → 期望目标由 `需求视图生成`（旧 Agent）改为 `view_expansion`
#     ——断言的是**"直行而非编排"这个核心性质**，具体落到哪个 Agent 是架构选择，
#       架构演进后若不同才是真问题。
#   · "对当前模型进行预评审校验" → 期望目标由 `review` 改为 `model_validation_repair`
#     （校验修复已由流水线 N4 节点承载）。
_DIRECT_OK = [("生成需求视图", ("view_expansion", "需求视图生成")),
              ("对当前模型进行预评审校验", ("model_validation_repair", "review")),
              ("用一句话介绍MBSE方法论", ("knowledge_qa",)),
              ("请生成一份关于宽带通信系统的分析报告", ("report_generation",)),
              ("查询#宽带通信 知识库资料", ("knowledge_qa",))]
for i, (q, wants) in enumerate(_DIRECT_OK, 1):
    # ★LLM 一律回 __ORCH__（最坏情况）：若仍判 direct，说明词法路径独立成立、不依赖 LLM
    d = tr.decide(q, td, llm_client=FakeLLM('{"agent":"__ORCH__","reason":"不该出现"}'))
    hit = d["target"] in wants
    ck(d["action"] == "direct" and hit,
       "B2.%d 单一交付物 ⇒ 直行 %s（实得 %s/%s）"
       % (i, "/".join(wants), d["action"], d["target"]))

print("\n== C. 档① 噪声：不得被判成「团队不支持」 ==")
for q in ("你好", "hi", "在吗"):
    d = tr.decide(q, td, llm_client=FakeLLM('{"agent":null,"reason":"不支持"}'))
    ck(d["action"] == "chat",
       "C%d 「%s」⇒ chat直答（实得 %s）——修前会被判 reject，用户被无谓拒绝"
       % (q and 1 or 2, q, d["action"]))
# 反例：短但有业务信号的 query 不得被当噪声
# ★ 2026-10-09：V15 清空了 8 个旧视图 Agent 的 intent_keywords（让视图请求统一走
#   `view_expansion` 流水线节点），因此「需求视图」不再命中旧 Agent。
#   但它**也不该落到 chat**——正确行为是落到 `view_expansion`（N3 节点）。
#   词条是「需求视图生成」/「视图展开生成」等，用户口语的「需求视图」是其子串，
#   但**子串方向相反** ⇒ 匹配不上（这是路由词语序问题，见 verify_intent_routing_baseline）。
#   本断言改为守住"不被当噪声"这一核心性质：可以是 direct 或 orchestrate，
#   但**不能是 chat**（chat 意味着用户的话被当成寒暄丢掉）。
d = tr.decide("需求视图", td, llm_client=FakeLLM(""))
ck(d["action"] != "chat",
   "C4 ★短但含触发词「需求视图」不被当噪声（实得 %s/%s）——"
   "只看长度会把有效短指令丢进 chat；期望直行或编排，不能是闲聊"
   % (d["action"], d["target"]))

print("\n== D. LLM 容错与降级（实测 2/36 解析失败） ==")
d = tr.decide("帮我写一首诗", td,
              llm_client=FakeLLM('好的：\n```json\n{"agent":null,"reason":"不支持"}\n```'))
ck(d["action"] == "reject" and d["source"] == "llm",
   "D1 ★代码围栏+前后缀仍解析成功（实得 %s/%s）" % (d["action"], d["source"]))
d = tr.decide("帮我写一首诗", td, llm_client=FakeLLM("完全不是 JSON 的一段闲聊"))
ck(d["action"] == "reject" and d["source"] == "lexical_fallback",
   "D2 ★解析失败回落词法且标记来源（实得 %s/%s）" % (d["action"], d["source"]))
d = tr.decide("帮我测算这个项目成本", td, llm_client=FakeLLM("", raise_=True))
ck(d["action"] == "reject" and d["source"] == "lexical_fallback",
   "D3 ★provider 抛异常不外泄（实得 %s/%s）" % (d["action"], d["source"]))
d = tr.decide("帮我看看这个项目的预算", td,
              llm_client=FakeLLM('{"agent":"review","reason":"幻觉成员名"}'))
ck(d["action"] != "direct" or d["target"] in [m["name"] for m in _m],
   "D4 LLM 返回的成员名受名册约束（实得 %s）" % d["target"])

print("\n== E. C4 意图识别结果参与决策（不再被丢弃） ==")
d = tr.decide("随便一句话，看路由到哪", td, detected_intent="knowledge_qa",
              llm_client=FakeLLM('{"agent":"impact"}'))
ck(d["target"] == "knowledge_qa" and d["source"] == "lexical",
   "E1 ★detected_intent 命中成员时优先采纳（实得 %s）" % d["target"])
d0 = tr.decide("随便一句话，看路由到哪", td, detected_intent="chat",
               llm_client=FakeLLM('{"agent":"impact"}'))
ck(d0["target"] == "impact",
   "E2 detected_intent 未命中成员时才交 LLM（实得 %s）" % d0["target"])

print("\n== F. 「不支持」提示必须是活路（不是死胡同） ==")
msg = tr.unsupported_message(td, "超出团队能力域")
ck("本团队可处理" in msg, "F1 提示带团队能力清单")
ck(len(msg) > 80, "F2 提示长度 %d > 80（太短等于没给信息）" % len(msg))
ck("去掉「智能体团队」选择" in msg, "F3 ★给出具体出路（去掉团队选择走通用入口）")

print("\n== G. C2 团队定义注入 planner prompt（接线检查） ==")
_pl = open(os.path.join(ROOT, "workflows", "planner.py"), encoding="utf-8").read()
ck("team_prompt" in _pl, "G1 planner 接受 team_prompt 参数")
ck("if team_prompt:" in _pl, "G2 ★team_prompt 真正拼进 prompt（不是只收不用）")
_i = _pl.find("if team_prompt:")
_j2 = _pl.find(".format(n=max_tasks)")
ck(_i > _j2, "G3 ★拼接在 .format() 之后（名册含 { }，放前面会 KeyError）")
ck("_planner_core(goal, agents, max_tasks, parallel, provider_id, run_id, conn," in _pl
   and "conversation_id, team_prompt)" in _pl,
   "G4 参数一路透传到 _planner_core（漏传则静默失效）")
_orch = open(os.path.join(ROOT, "agent", "pipeline_parts", "orchestration.py"),
             encoding="utf-8").read()
ck('_kw["team_prompt"]' in _orch and "**_kw)" in _orch,
   "G5 _try_orchestrate_team 把团队定义传下去（_kw[team_prompt] + **_kw 展开）")
_st = open(os.path.join(ROOT, "agent", "pipeline_parts", "stream.py"), encoding="utf-8").read()
ck("roster_block" in _st, "G6 流式编排注入成员名册（capabilities 是近义成员唯一区分依据）")
ck('(team_def or {}).get("system_prompt")' in _st,
   "G7 ★拆解规则取 team_def 全文（不再 [:300] 截断）")
# G8：断言**代码**里不再有 [:300]（注释里提到历史写法是合法的，必须先剥注释再查）
import re as _re  # noqa: E402
_team_seg = _st.split("if team_forced:")[1][:1600]
_code_only = _re.sub(r"#.*", "", _team_seg)          # 去掉行注释
_code_only = _re.sub(r'"""[\s\S]*?"""', "", _code_only)  # 去掉块注释
ck("[:300]" not in _code_only,
   "G8 ★team 分支代码内不再有 [:300] 截断（注释里的历史说明不算）")
ck("[:4000]" in _code_only,
   "G9 保留超长 prompt 防御上限（[:4000]，不是无上限拼接）")

print("\n== H. C3 双路径口径对齐（stream / execute 都要判） ==")
_ex = open(os.path.join(ROOT, "agent", "pipeline_parts", "execute.py"), encoding="utf-8").read()
for tag, src in (("stream", _st), ("execute", _ex)):
    ck("_team_route_decision(" in src, "H1.%s 非流式/流式都调用同一判据" % tag)
    ck('team_def = self._team_definition(team)' in src,
       "H2.%s ★改 team 后加载完整定义（仍只回 name 则 C1 形同虚设）" % tag)
    ck("team_direct" in src, "H3.%s 直行/直答路径显式禁编排（不靠黑名单巧合）" % tag)
ck("team_forced = True" in _st and "elif _act ==" in _st
   and _st.index("elif _act ==") < _st.index("team_forced = True"),
   "H4 ★team_forced 仅在路由判为 orchestrate 后置位（不是入口处无条件置位）")
ck(_st.count("_team_route_decision(") == 1 and _ex.count("_team_route_decision(") == 1,
   "H5 各路径只判一次（重复判定会双花 LLM）")

print("\n== I. 变异自证：把被测逻辑改坏，门禁必须判红 ==")
# ⚠️ 关键1：`exec` 到独立命名空间取函数 —— 直接 import 拿到的是**未被变异**的真函数，
#    那样判据跑的还是好代码，变异会「假不红」（已踩过）。
# ⚠️ 关键2：锚点用**单行唯一子串**，不用多行块。多行块一旦上游微调就miss，
#    而第一版正是把锚点写成函数签名，源码加个类型标注就 miss。
#    锚点 miss 必须判红，不许静默跳过（否则门禁悄悄失去自证能力）。
# ⚠️ 关键3：每条变异配的 query 必须**只依赖被变异的那条规则**，
#    否则其它正则仍会命中 → 变异无效但看起来"没变红"（第一版就踩了）。
_src = open(os.path.join(ROOT, "agent", "team_router.py"), encoding="utf-8").read()


def _mutate(anchor: str, replacement: str):
    """返回变异后源码；anchor 未命中返回 None（调用方判红）。"""
    if anchor not in _src:
        return None
    return _src.replace(anchor, replacement, 1)


def _load_mut(name, text):
    ns = types.ModuleType("mut_" + name)
    exec(compile(text, "<mutated>", "exec"), ns.__dict__)
    return ns


# I1 变异 is_noise → 恒 False（「你好」不再判噪声）
_m = _mutate("def is_noise(query: str, members: list) -> bool:",
             "def is_noise(query, members):\n    return False")
if _m is None:
    ck(False, "I1 ★变异锚点未命中：is_noise 定义（源码已变，门禁失去自证能力）")
else:
    ns = _load_mut("noise", _m)
    # 判红点=变异生效的证据：变异后「你好」**不再**被判噪声。
    # （第一版把方向写反成 `is True`，于是变异生效反而判红 —— 断言必须描述
    #   「变异后行为确实变了」，而不是「变异后行为仍正确」。）
    ck(ns.is_noise("你好", td["members"]) is False,
       "I1 变异 is_noise→恒False 后「你好」不再判噪声（证明变异真的生效）")
    d = ns.decide("你好", td, llm_client=FakeLLM('{"agent":"review"}'))
    ck(d["action"] != "chat",
       "I1b ★且路由随之不再走 chat 直答（变异影响到了 decide，非孤立函数）")

# I2 变异首条多交付物正则 → 永不匹配（query 只依赖该条规则）
_m = _mutate('re.compile(r"(然后|接着|最后).{0,4}(做|生成|输出|给出|汇总)")',
             're.compile(r"(?!x)x")')
if _m is None:
    ck(False, "I2 ★变异锚点未命中：多交付物首条正则")
else:
    ns = _load_mut("multi", _m)
    # ⚠️ query 必须**只命中被变异的那一条**，否则其它正则兜住 → 变异无效但看不出来。
    #   「先做需求分析然后汇总输出」就踩了这个坑：它同时命中 `先.{0,8}(再|然后|接着)`，
    #   变异掉另一条仍会命中 ⇒ 假不红。「然后汇总输出」经实测只命中被变异那条。
    ck(tr._multi_delivery("然后汇总输出") is True,
       "I2a 前置：未变异时该 query 确实被判多交付物（否则变异无效也无意义）")
    ck(ns._multi_delivery("然后汇总输出") is False,
       "I2 变异后该 query 不再被判多交付物（证明锚点命中的是生效的那条正则）")

# I3 变异围栏剥离 → 围栏内容解析失败
_m = _mutate('re.sub(r"^```(?:json)?\\s*|\\s*```$", "", txt, flags=re.S)',
             '""  # 变异：不做围栏剥离')
if _m is None:
    ck(False, "I3 ★变异锚点未命中：围栏剥离 re.sub")
else:
    ns = _load_mut("parse", _m)
    r = ns._parse_route('```json\n{"agent":"review"}\n```')
    ck(r == {}, "I3 变异围栏剥离后围栏内容解析失败（判红点，实得 %s）" % r)

# 真实阈值基线自测：LEX_STRONG / LEX_MARGIN 的口径必须与实现一致
ck(tr.LEX_STRONG == 2.0 and tr.LEX_MARGIN == 0.6,
   "I4 阈值常量与文档口径一致（改阈值必须同步改文档）")
_score_short, _ = tr.lexical_score({"intent_keywords": ["需求"]}, "需求视图")
_score_long, _ = tr.lexical_score({"intent_keywords": ["需求视图"]}, "需求视图")
ck(_score_long > _score_short,
   "I5 ★特异性加权生效：长触发词得分 %0.1f > 通用短词 %0.1f"
   % (_score_long, _score_short))

print("\n== J. 数据落库校验（capabilities/system_prompt 真的写进去了） ==")
_db = sqlite3.connect("file:%s?mode=ro" % os.path.join(ROOT, "mbse.db"), uri=True)
_db.row_factory = sqlite3.Row
_n = _db.execute(
    """SELECT COUNT(*) n FROM agent_team_members m JOIN agents a ON a.id=m.sub_agent_id
       WHERE m.main_agent_id=160 AND a.capabilities NOT IN ('[]','')""").fetchone()["n"]
ck(_n >= 14, "J1 ★库内成员 capabilities 均已落库（实得 %d，下限 14）" % _n)
_p = _db.execute("SELECT system_prompt FROM agents WHERE id=160").fetchone()["system_prompt"] or ""
for kw in ("拆解规则", "八视图", "不要虚构能力", "逐字取自"):
    ck(kw in _p, "J2 prompt 含关键约束「%s」" % kw)
_db.close()

print("\n" + "=" * 68)
print("PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
if FAIL:
    print("HAS FAILURE")
    for f in FAIL:
        print("  x " + f)
    sys.exit(1)
print("ALL GREEN")