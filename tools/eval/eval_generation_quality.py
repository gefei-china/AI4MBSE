#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""生成质量评测器（2026-10-22，2026-10-22 晚加 LLM 语义层）—— 补上断链③
「无生成质量指标」。

## 两层结构（2026-10-22 晚起）

- **确定性层**（默认，秒级）：citation 编号/使用率/无据声称密度/接地信号。
  改提示词、改引用规则后每次都跑这条。
- **LLM 语义层**（`--llm-judge`，约 1 分钟）：faithfulness 忠实度评判 —
  —"答案有没有曲解证据"只有语义层能答。只进 nightly 与显式运行
  （模型有温度、依赖服务可用性，不是确定性断言）。三态记账 + judge
  合成对照自证 + 两项豁免口径（工具执行结果/会话上下文），详见
  JUDGE_SYSTEM_PROMPT 与 run_llm_judge 上方的模块头。

## 为什么要有它

评估报告的核心结论是「度量能力强、闭环能力弱」，三条结构性断链之一即：
**faithfulness / groundedness 全仓零命中** —— 这套系统评测检索（recall/MRR/
nDCG）、评测路由（intent 准确率）、评测记忆（entity F1），唯独**不评测生成
质量**：答案有没有依据、引用的编号对不对、事实声称有没有证据附着。
RAG 三件套（检索→生成→引用）里，前两环有指标，第三环是黑的。

而生成质量的原料其实早已落库（2026-10-22 实测）：
- `messages` 表 505 条中，187 条助手消息的 `card_data.citations` 带完整
  引用块（含 `content` 正文、`source_doc`、`section`、`confidence_level`）；
- 引用编号【来源n】与 citations 数组 1 基对齐（`agent/utils._citations_payload`），
  正文 [n] 标引即"这条声称指向哪块证据"的机器可读信号。

## 指标设计（四项，实测基线见文件末）

| 指标 | 语义 | 门禁？ |
|---|---|---|
| `citation_valid_rate` | 正文 [n] 全部落在 citations 范围内（编号不错位） | 是（=1.0） |
| `citation_usage_rate` | 带引用的答案里实际用了 [n] 的比例（软约束可见化） | 是（≥0.25） |
| `no_citation_factual_rate` | 无引用答案中含可核查实体的比例（无据声称密度） | 是（≤0.30） |
| `grounded_entity_rate` | 答案实体在引用块并集中的比例（faithfulness 信号） | **否**（见下） |

**为什么 grounded 不门禁**（这是本工具最重要的纪律，写在这里免得后来人
误以为是漏配）：实测 RAG 纯问答域 32 条答案只含 **2 个**可核查实体
（0.000 接地率）——不是幻觉严重，而是**当前 RAG 路径几乎不产生可确定性
核查的事实声称**；历史语料里那些"未接地实体"逐条查证后全部有合法来源
（多轮附件正文 / 用户粘贴的需求原文 / 编排任务上下文）。在一个只有 2 个
实体、且负对照（池化其它答案的引用）都无法拉开差距的样本上设阈值，
要么恒绿（假绿）要么恒红（狼来了）—— 两者都比不门禁更糟。故：
算出来、报出来、进历史趋势，等claim 密度上来（`n_entities ≥ 30`）且
负对照能拉开量级时才升级为门禁指标。届时只需在 verify 门禁里改一行。

## 口径与边界（读报告前必须知道的三件事）

1. **只评 text 类 RAG 问答**。`card_impact`（图谱影响分析）的证据主语是
   知识图谱（如 FCR-0 是图实体），`card_candidates`/`card_review` 同理，
   块接地对它们不适用——用块接地评卡片会得到 100% 假未接地。
2. **排除 Mock 与附件会话**。`used_mock=true` 的答案不是系统能力证据
   （与 trace2dataset 同口径）；上传过附件的会话，其实证据主语是附件正文
   （附件上下文跨轮持续，故按**会话级**判定，不是只看前一条用户消息——
   第一版只看前一条，漏掉了"这轮才提问、附件是三轮前上传的"）。
3. **确定性接地是信号不是裁决**。实体来源可能是会话历史、任务提示词、
   模型自身知识——后两者里前者合法、后者才是风险。逐条明细在
   `ungrounded_examples`，供人工复核；**不要**把它当自动判幻听的依据。

## 用法

  # 评测（只读生产库）+ 打印报告
  .venv/Scripts/python.exe -X utf8 tools/eval/eval_generation_quality.py

  # 指定库 / 输出 / 落库
  .venv/Scripts/python.exe -X utf8 tools/eval/eval_generation_quality.py \
      --db mbse.db --out tmp/p21/eval_gen.json --save

  # 跳过黄金样本自证（不推荐：自证防"指标算错却全绿"）
  .venv/Scripts/python.exe -X utf8 tools/eval/eval_generation_quality.py --no-fixture

落库后由 `tools/verify/verify_generation_quality_gate.py` 门禁消费。
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sqlite3
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# ★ 直接运行本脚本时 sys.path[0] 是 tools/eval/，不是仓库根 ⇒ `from llm
#   import llm_client`（LLM 评判层）会 ModuleNotFoundError。与 trace2dataset
#   同构：入口即把 ROOT 插进去（import 顺序即正确性）。
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# ── 实体模式（高精度优先：只抽"可机器核查"的声称）───────────────────────
# ID 类：REQ-BC-001 / BR-8 / MG-6 / FCR-0（1-5 位大写前缀 + 数字编号）。
#   刻意不收「」引号词——实测它会捞出一堆对话性短语（"需要您确认建模
#   信息后继续"），假未接地工厂；faithfulness 信号宁缺毋滥。
# 数值类：带单位的数值（38dBi / 2Gbps / 99.9% / 500ms）——可对照的指标声称。
_ENT_ID = re.compile(r"\b[A-Z]{1,5}-\d+[A-Za-z]?\b")
_ENT_NUM = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:dBi|Gbps|Mbps|GHz|MHz|kHz|万路|万用户|ms|°C|%|dB)\b")

# 正文引用标记：[n]（与前端 citations 数组 1 基对齐，见 prompt.py 引用规范）
_CITE_MARK = re.compile(r"\[(\d+)\]")

DEFAULT_DB = os.path.join(ROOT, "mbse.db")
DEFAULT_OUT = os.path.join(ROOT, "tmp", "p21", "eval_generation_quality.json")

#: 附件列的"空"的各种写法（不同版本前端写过 '[]' / 'null' / ''）
_ATT_EMPTY = ("", "[]", "null", "{}", "None")


def _entities(text):
    """抽取可核查实体（ID + 带单位数值）。宁缺毋滥：假阳性会淹没真信号。"""
    out = set()
    for rx in (_ENT_ID, _ENT_NUM):
        out |= set(m.group(0).strip() for m in rx.finditer(text or ""))
    return out


def _citation_markers(body):
    """正文里的 [n] 引用标记（数字列表）。"""
    return [int(m.group(1)) for m in _CITE_MARK.finditer(body or "")]


def _citation_context(citations):
    """引用块正文并集（接地质检的"证据海"）。"""
    return " ".join((c.get("content") or "") for c in citations or [])


# ══════════════════════════════════════════════════════════════════════
# 核心：对一组答案算四项指标（生产数据与黄金样本**同走此函数**——
# 自证必须验的是真身，不能另开一条代码路径，否则Fixture 证明不了任何事）
# ══════════════════════════════════════════════════════════════════════
def evaluate_answers(answers):
    """answers: [{id, body, citations:[{content,...}]}] → 指标 dict。

    citations 为空列表 = 无引用答案（进 no_citation_factual 分母）。
    返回四项指标 + 计数 + 池化负对照。任何分母为 0 的指标返 None
    （不是 0——"没测到"与"测到 0 分"必须可区分，否则看板会把前者渲染成
    后者，那是假绿）。
    """
    n_ans = len(answers)
    with_cit = [a for a in answers if a.get("citations")]
    without_cit = [a for a in answers if not a.get("citations")]

    # ── citation_valid_rate：正文 [n] 必须全部落在引用范围内 ──
    total_marks = valid_marks = 0
    answers_using = 0
    cits_without_content = 0     # schema 探针：见下
    for a in with_cit:
        marks = _citation_markers(a.get("body"))
        n_cit = len(a.get("citations") or [])
        if marks:
            answers_using += 1
            total_marks += len(marks)
            valid_marks += sum(1 for n in marks if 1 <= n <= n_cit)
        # schema 探针：引用块没有 content 正文 ⇒ 接地质检对这套引用**静默
        # 失效**（实体海是空的， grounded 率会假性冲到 1.0 —— 典型的
        # "看起来全绿"路径）。计数进报告，门禁要求为 0。
        cits_without_content += sum(
            1 for c in (a.get("citations") or []) if not (c.get("content") or ""))
    citation_valid_rate = (valid_marks / total_marks) if total_marks else None
    citation_usage_rate = (answers_using / len(with_cit)) if with_cit else None

    # ── grounded_entity_rate：答案实体在"自己的"引用并集里的比例 ──
    # ── no_citation_factual_rate：无引用答案中含可核查实体的比例 ──
    total_ent = grounded_ent = 0
    per_answer = []
    for a in with_cit:
        ctx = _citation_context(a.get("citations"))
        ents = _entities(a.get("body"))
        g = sum(1 for e in ents if e in ctx)
        total_ent += len(ents)
        grounded_ent += g
        if ents or _citation_markers(a.get("body")):
            per_answer.append({
                "id": a.get("id"), "kind": "with_citations",
                "n_entities": len(ents), "n_grounded": g,
                "ungrounded": sorted(e for e in ents if e not in ctx),
                "n_markers": len(_citation_markers(a.get("body"))),
            })
    grounded_entity_rate = (grounded_ent / total_ent) if total_ent else None

    factual_nocit = 0
    for a in without_cit:
        ents = _entities(a.get("body"))
        if ents:
            factual_nocit += 1
            per_answer.append({
                "id": a.get("id"), "kind": "no_citations",
                "n_entities": len(ents), "n_grounded": 0,
                "ungrounded": sorted(ents), "n_markers": 0,
            })
    no_citation_factual_rate = (
        factual_nocit / len(without_cit)) if without_cit else None

    # ── 池化负对照：每条答案对"其它全部答案"的引用并集算接地率 ──
    # 判别力检验：真实接地率必须**显著高于**池化接地率，否则该指标在这批
    # 样本上没在测"答案↔自己证据"的对齐关系（可能是实体太通用，或证据
    # 主语根本不在 citations 里）。拉不开 ⇒ 门禁判 SKIP 而非通过。
    # ⚠ 池子必须**排除本条自己的引用** —— 第一版把全部答案（含自己）的
    #   引用并集当池子，自己的证据当然在自己的池子里 ⇒ 对照恒等于真值、
    #   discriminates 恒 False。是黄金样本 F4F5 抓出来的（合成数据上
    #   真值 1.0 / 含己池 1.0 / 排己池 0.0，一眼可见）。
    pooled_total = pooled_grounded = 0
    for a in with_cit:
        own_ids = {id(c) for c in (a.get("citations") or [])}
        others_ctx = _citation_context(
            [c for b in with_cit if b is not a
             for c in (b.get("citations") or []) if id(c) not in own_ids])
        ents = _entities(a.get("body"))
        pooled_total += len(ents)
        pooled_grounded += sum(1 for e in ents if e in others_ctx)
    control_rate = (pooled_grounded / pooled_total) if pooled_total else None
    true_rate = grounded_entity_rate
    discriminates = (
        true_rate is not None and control_rate is not None
        and true_rate >= control_rate + 0.15)

    return {
        "n_answers": n_ans,
        "n_with_citations": len(with_cit),
        "n_without_citations": len(without_cit),
        "citation_valid_rate": citation_valid_rate,
        "citation_usage_rate": citation_usage_rate,
        "grounded_entity_rate": grounded_entity_rate,
        "no_citation_factual_rate": no_citation_factual_rate,
        "total_markers": total_marks,
        "valid_markers": valid_marks,
        "answers_using_citations": answers_using,
        "citations_without_content": cits_without_content,
        "n_entities": total_ent,
        "n_grounded_entities": grounded_ent,
        "control": {"pooled_grounded_rate": control_rate,
                    "discriminates": discriminates},
        "per_answer": per_answer,
    }


# ══════════════════════════════════════════════════════════════════════
# LLM 评判层（2026-10-22 晚）：faithfulness 语义信号（断链③的深层）
# ══════════════════════════════════════════════════════════════════════
# 确定性指标（citation 编号/使用率/接地/无据声称）回答的是"引用体系有没
# 有在好好工作"，回答不了"答案有没有曲解证据"——那是语义层，只有 LLM
# 能判。本层补上它，但守住四条纪律：
#
# 1. **opt-in**（--llm-judge）：确定性路径保持秒级；LLM 层只进 nightly
#    与显式运行（模型输出有温度、且依赖服务可用性，不是确定性断言）。
# 2. **三态计数**：faithful / unfaithful / **判不了**（调用失败或解析
#    失败）—— 判不了绝不是"算 unfaithful"（把环境故障读成质量差），
#    也绝不是"算 faithful"（假绿）。每种失败都带原因进记账。
# 3. **judge 自证**：每次运行附带两对合成对照（逐字引用 ⇒ 应 faithful；
#    追加证据中不存在的具体声称 ⇒ 应 unfaithful）。对照不过 ⇒ judge
#    本身坏了（prompt/解析/模型），门禁判红而不是信它的分数。
# 4. **chat_fn 依赖注入**：夹具用 canned 响应验解析与聚合（不依赖真
#    LLM），真路径才插 llm_client.chat。
JUDGE_SYSTEM_PROMPT = (
    "你是 RAG 答案忠实度评判员。给你一段【答案】和它引用的【证据块】，"
    "判断答案中的事实性声称是否有证据支撑。只输出一个 JSON 对象，不要任何"
    '其他文字，格式：{"faithful": true或false, '
    '"unsupported": ["无支撑的声称原文", ...], "reason": "一句话说明"}。\n"'
    "评判标准：\n"
    "- 答案与证据一致，或答案是一般性方法论/流程性表述（无需证据支撑）"
    "⇒ faithful=true，unsupported 为空数组；\n"
    "- 答案包含证据中不存在或与证据矛盾的具体事实（条款号、数值、实体名、"
    "结论性判断）⇒ faithful=false，并把无支撑的声称**逐条原文**列入 "
    "unsupported；\n"
    "- **工具执行结果不算无支撑**：答案中来自工具运行的具体数值/计数/诊断"
    "（如模型校验器的词法/语法/语义计数、错误清单、行号）是工具产出而非"
    "知识库声称，证据块不覆盖它们是正常的 —— 这类内容不计入 unsupported，"
    "在 reason 注明“含工具执行结果”即可；\n"
    "- **会话上下文不算无支撑**：答案中来自本会话的上下文性声称（用户身份/"
    "称呼、产品/项目名、上轮结论、会话状态如“输入缺失/上轮TBD/某视图生成"
    "失败”、记忆边界自述）有合法来源（会话历史/记忆），证据块不覆盖是正常"
    "的 —— 不计入 unsupported，reason 注明“会话上下文”即可；\n"
    "- 证据为空或与问题无关导致无法判断 ⇒ faithful=false，reason 说明"
    "“无法判断”（宁可误判也不放过）。"
)

#: 单条评判的正文/证据截断上限（控成本；超限在记录里标注，不静默丢）
_JUDGE_BODY_CHARS = 1500
_JUDGE_CIT_CHARS = 400


def parse_judge_response(text):
    """解析 LLM 评判响应。返回 {ok, faithful, unsupported, reason}。

    ok=False = 解析失败（**三态中的第三态**：调用方必须把它计成
    "判不了"，不是 faithful 也不是 unfaithful）。
    宽容但严格： faithful 接受 true/false/"true"/"false"/1/0 五种写法；
    JSON 提取失败 ⇒ ok=False（绝不猜）。
    """
    import re as _re
    m = _re.search(r"\{[\s\S]*\}", text or "")
    if not m:
        return {"ok": False, "faithful": None, "unsupported": [], "reason": ""}
    try:
        import json as _json
        obj = _json.loads(m.group(0))
    except Exception:
        return {"ok": False, "faithful": None, "unsupported": [], "reason": ""}
    if not isinstance(obj, dict) or "faithful" not in obj:
        return {"ok": False, "faithful": None, "unsupported": [], "reason": ""}
    fv = obj.get("faithful")
    if isinstance(fv, bool):
        faithful = fv
    elif isinstance(fv, str) and fv.strip().lower() in ("true", "false"):
        faithful = fv.strip().lower() == "true"
    elif isinstance(fv, (int, float)) and fv in (0, 1):
        faithful = bool(fv)
    else:
        return {"ok": False, "faithful": None, "unsupported": [], "reason": ""}
    uns = obj.get("unsupported")
    unsupported = [str(u) for u in uns] if isinstance(uns, list) else []
    return {"ok": True, "faithful": faithful, "unsupported": unsupported,
            "reason": str(obj.get("reason") or "")}


def _judge_prompt(body, citations):
    """组评判提示词（截断上限见常量；证据按 [n] 编号与正文口径一致）。"""
    b = (body or "")[:_JUDGE_BODY_CHARS]
    lines = []
    for i, c in enumerate(citations or [], start=1):
        lines.append("[%d] %s" % (i, (c.get("content") or "")[:_JUDGE_CIT_CHARS]))
    ev = "\n".join(lines) if lines else "（无引用证据）"
    return "【答案】\n%s\n\n【证据块】\n%s" % (b, ev)


def judge_one(body, citations, chat_fn, model_label=""):
    """评判单条答案。任何异常都记进结果（三态：ok+faithful / ok+unfaithful
    / 判不了+原因），绝不向上抛（跑批器自己不能因单条失败而崩）。"""
    prompt = _judge_prompt(body, citations)
    try:
        resp = chat_fn([{"role": "system", "content": JUDGE_SYSTEM_PROMPT},
                        {"role": "user", "content": prompt}], _intent="gen_faithfulness")
        content = ((resp.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        parsed = parse_judge_response(content)
        if not parsed["ok"]:
            return {"ok": False, "verdict": "parse_failed",
                    "reason": "响应不含可解析 JSON", "raw_head": content[:120]}
        return {"ok": True, "verdict": "faithful" if parsed["faithful"] else "unfaithful",
                "unsupported": parsed["unsupported"], "reason": parsed["reason"]}
    except Exception as e:  # noqa: BLE001 —— 见 docstring：单条失败不杀全程
        return {"ok": False, "verdict": "call_failed",
                "reason": "%s: %s" % (type(e).__name__, str(e)[:120])}


def _control_pairs(answers):
    """构造两对合成评判对照（确定性、验证 judge 本身，见模块头纪律 3）。

    pos：答案逐字引用自己的证据块 ⇒ 任何正常的 judge 都应判 faithful；
    neg：在逐字引用后追加一条证据中不存在的具体声称（假条款号+假数值）
         ⇒ 应判 unfaithful。
    返回 (pos_pair, neg_pair, note)。采样不足或引用无正文 ⇒ note 非空
    （对照不建，自证判 SKIP 而不是假过）。
    """
    src = next((a for a in answers if (a.get("citations") or [])
                and any((c.get("content") or "").strip()
                        for c in a["citations"])), None)
    if not src:
        return None, None, "采样中无带正文引用的答案，对照不建"
    cit = next(c for c in src["citations"] if (c.get("content") or "").strip())
    quote = (cit.get("content") or "").strip()[:150]
    pos_pair = {"id": "__control_pos__",
                "body": "根据引用证据：%s" % quote,
                "citations": [cit], "expect": "faithful"}
    neg_pair = {"id": "__control_neg__",
                "body": "根据引用证据：%s 此外，证据确认 ZQ-0000 的指标为 000Gbps。"
                        % quote,
                "citations": [cit], "expect": "unfaithful"}
    return pos_pair, neg_pair, ""


def run_llm_judge(answers, chat_fn, limit=15, model_label=""):
    """LLM 评判一批带引用的答案。返回 llm_judge 段（见模块头四条纪律）。

    answers: load_production_answers 的产出（已排除 Mock/附件会话）。
    只评**带引用**的答案——无引用答案的"无据声称"已由确定性指标
    （no_citation_factual_rate）覆盖，再问 LLM 是重复计价。
    """
    pool = [a for a in answers if a.get("citations")]
    sampled = pool[:max(0, int(limit or 0))]
    seg = {"enabled": True, "model": model_label,
           "n_pool": len(pool), "n_sampled": len(sampled),
           "n_judged": 0, "n_faithful": 0, "n_unfaithful": 0,
           "n_failed": 0, "faithful_rate": None,
           "control": {"ran": False, "pos_ok": None, "neg_ok": None,
                       "ok": None, "note": ""},
           "unfaithful_examples": [], "per_answer": []}
    if not sampled:
        seg["control"]["note"] = "无带引用的答案可评"
        return seg

    per, n_faithful, n_unfaithful, n_failed = [], 0, 0, 0
    for a in sampled:
        r = judge_one(a.get("body"), a.get("citations"), chat_fn, model_label)
        rec = {"id": a.get("id"), "verdict": r["verdict"]}
        if r["ok"]:
            rec["unsupported"] = r.get("unsupported") or []
            rec["reason"] = r.get("reason") or ""
            if r["verdict"] == "faithful":
                n_faithful += 1
            else:
                n_unfaithful += 1
        else:
            n_failed += 1
            rec["reason"] = r.get("reason") or ""
        per.append(rec)
    seg["per_answer"] = per
    seg["n_faithful"], seg["n_unfaithful"], seg["n_failed"] = (
        n_faithful, n_unfaithful, n_failed)
    seg["n_judged"] = n_faithful + n_unfaithful
    seg["faithful_rate"] = (round(n_faithful / seg["n_judged"], 4)
                            if seg["n_judged"] else None)
    seg["unfaithful_examples"] = [
        {"id": r["id"], "unsupported": r.get("unsupported") or [],
         "reason": r.get("reason") or ""}
        for r in per if r["verdict"] == "unfaithful"][:20]

    # ── judge 自证：合成对照（纪律 3）────────────────────────────
    pos_pair, neg_pair, note = _control_pairs(sampled)
    if not pos_pair:
        seg["control"]["note"] = note
        return seg
    rp = judge_one(pos_pair["body"], pos_pair["citations"], chat_fn)
    rn = judge_one(neg_pair["body"], neg_pair["citations"], chat_fn)
    pos_ok = bool(rp["ok"] and rp["verdict"] == "faithful")
    neg_ok = bool(rn["ok"] and rn["verdict"] == "unfaithful")
    seg["control"] = {"ran": True, "pos_ok": pos_ok, "neg_ok": neg_ok,
                      "ok": pos_ok and neg_ok,
                      "note": "" if (pos_ok and neg_ok) else
                      "pos=%s neg=%s（judge  prompt/解析/模型可疑，分数不可信）"
                      % (rp["verdict"], rn["verdict"]),
                      "pos_detail": {"verdict": rp["verdict"],
                                     "reason": rp.get("reason", "")[:120]},
                      "neg_detail": {"verdict": rn["verdict"],
                                     "reason": rn.get("reason", "")[:120]}}
    return seg


# ══════════════════════════════════════════════════════════════════════
# 黄金样本自证（F1~F5）：合成已知 ground truth 的答案，断言**精确值**
# ══════════════════════════════════════════════════════════════════════
def _fixture_answers():
    """5 组合成答案，每组预期值由设计者手写（不依赖任何实测数据）。"""
    cits_a = [{"content": "REQ-1 规定带宽 2Gbps。", "source_doc": "a.md"},
              {"content": "BR-2 规定接口延迟 500ms。", "source_doc": "b.md"},
              {"content": "无关内容。", "source_doc": "c.md"}]
    cits_b = [{"content": "MG-1 是模型生成质量条款。", "source_doc": "d.md"}]
    return [
        # F1a：两个标记都在范围内 ⇒ valid=1.0
        {"id": "F1a", "body": "见 [1] 与 [3] 的规定。",
         "citations": cits_a, "expect": {"citation_valid_rate": 1.0,
                                          "citation_usage_rate": 1.0,
                                          "grounded_entity_rate": None,
                                          "no_citation_factual_rate": None}},
        # F1b：[4] 越界（只有 3 条引用）⇒ valid=0.5
        {"id": "F1b", "body": "见 [1] 与越界的 [4]。",
         "citations": cits_a, "expect": {"citation_valid_rate": 0.5,
                                          "citation_usage_rate": 1.0,
                                          "grounded_entity_rate": None,
                                          "no_citation_factual_rate": None}},
        # F2：实体 REQ-1 在自己的引用里、REQ-9 不在 ⇒ grounded=0.5
        {"id": "F2", "body": "REQ-1 已规定，REQ-9 待确认。",
         "citations": cits_a, "expect": {"grounded_entity_rate": 0.5,
                                          "citation_valid_rate": None,
                                          "no_citation_factual_rate": None}},
        # F3：无引用答案组——F3 无实体（不进分子）、F3b 含 BR-8（进分子）
        #   ⇒ 组率 0.5（expect 是**组**语义，不是单条语义）
        {"id": "F3", "body": "一般性说明，无实体。",
         "citations": [], "expect": {"no_citation_factual_rate": 0.5,
                                      "grounded_entity_rate": None,
                                      "citation_valid_rate": None}},
        {"id": "F3b", "body": "按 BR-8 执行。",
         "citations": [], "expect": {"no_citation_factual_rate": 0.5,
                                      "grounded_entity_rate": None,
                                      "citation_valid_rate": None}},
        # F4：接地率判别力——REQ-1 在自己引用里、别人引用里没有 ⇒ 可判别
        {"id": "F4", "body": "REQ-1 的规定如下。",
         "citations": cits_a, "expect": {"grounded_entity_rate": 1.0,
                                          "citation_valid_rate": None,
                                          "no_citation_factual_rate": None}},
        # F5：另一个答案，其引用里没有 REQ-1（供 F4 的池化对照做分母）
        {"id": "F5", "body": "MG-1 的规定如下。",
         "citations": cits_b, "expect": {"grounded_entity_rate": 1.0,
                                          "citation_valid_rate": None,
                                          "no_citation_factual_rate": None}},
    ]


def run_fixture():
    """跑黄金样本自证。返回 (ok, cases)。"""
    fx = _fixture_answers()
    # F1a/F1b/F2/F4/F5 单独成组算率；F3/F3b 合成一组算 no_citation 率
    cases = []

    def _case(cid, subset, key):
        r = evaluate_answers(subset)
        exp = next(f["expect"][key] for f in fx if f["id"] == cid)
        got = r.get(key)
        if exp is None:
            ok = got is None
        else:
            ok = got is not None and abs(got - exp) < 1e-9
        cases.append({"case": cid, "metric": key, "expect": exp,
                      "got": got, "ok": ok})

    _case("F1a", [fx[0]], "citation_valid_rate")
    _case("F1b", [fx[1]], "citation_valid_rate")
    _case("F2", [fx[2]], "grounded_entity_rate")
    _case("F3", [fx[3], fx[4]], "no_citation_factual_rate")
    _case("F4", [fx[5]], "grounded_entity_rate")
    _case("F5", [fx[6]], "grounded_entity_rate")
    # F4/F5 合起来：真实 2/2=1.0，池化（互为对方的引用）0/2=0.0 ⇒ 可判别
    r = evaluate_answers([fx[5], fx[6]])
    exp_disc = True
    got_disc = bool(r["control"]["discriminates"])
    cases.append({"case": "F4F5", "metric": "control.discriminates",
                  "expect": exp_disc, "got": got_disc, "ok": got_disc == exp_disc})
    # 用法率：F4/F5 无 [n] 标记 ⇒ 0.0
    r2 = evaluate_answers([fx[5], fx[6]])
    cases.append({"case": "F4F5", "metric": "citation_usage_rate",
                  "expect": 0.0, "got": r2["citation_usage_rate"],
                  "ok": abs(r2["citation_usage_rate"] - 0.0) < 1e-9})

    # ── F6~F9：LLM 评判层（canned 响应，零真 LLM 依赖）────────────
    cases.extend(_fixture_judge())
    return all(c["ok"] for c in cases), cases


def _fixture_judge():
    """LLM 评判层夹具：解析三态、聚合精确值、自证对照、失败记账。

    全部用 canned chat_fn（按 prompt 内容分派响应），**不调真 LLM** —
    — 夹具要验的是"解析与聚合算得对"，不是"模型判得准"（后者靠
    run_llm_judge 内置的合成对照在真跑时自证）。
    """
    cases = []

    def _c(cid, metric, expect, got, tol=1e-9):
        if expect is None or got is None:
            ok = expect is got
        else:
            ok = abs(float(got) - float(expect)) < tol
        cases.append({"case": cid, "metric": metric, "expect": expect,
                      "got": got, "ok": ok})

    # F6：解析三态 —— 合法 JSON / 字符串布尔 / 畸形响应
    p1 = parse_judge_response(
        '{"faithful": true, "unsupported": [], "reason": "一致"}')
    cases.append({"case": "F6a", "metric": "parse.ok",
                  "expect": True, "got": p1["ok"], "ok": p1["ok"] is True})
    cases.append({"case": "F6b", "metric": "parse.faithful",
                  "expect": True, "got": p1["faithful"],
                  "ok": p1["faithful"] is True})
    p2 = parse_judge_response(
        '前缀噪声 {"faithful": "false", "unsupported": ["X"]} 后缀噪声')
    cases.append({"case": "F6c", "metric": "parse.string_bool",
                  "expect": False, "got": p2["faithful"],
                  "ok": p2["ok"] is True and p2["faithful"] is False
                  and p2["unsupported"] == ["X"]})
    p3 = parse_judge_response("我无法输出 JSON。")
    cases.append({"case": "F6d", "metric": "parse.malformed_is_third_state",
                  "expect": False, "got": p3["ok"],
                  "ok": p3["ok"] is False and p3["faithful"] is None})
    p4 = parse_judge_response('{"faithful": "maybe"}')
    cases.append({"case": "F6e", "metric": "parse.bad_bool_is_third_state",
                  "expect": False, "got": p4["ok"],
                  "ok": p4["ok"] is False})

    # F7：聚合精确值 —— 3 条：A1 faithful / A2 unfaithful / A3 判不了
    #   ⇒ 率 = 1/2 = 0.5（**判失败不进分母**，这是三态计分的核心断言）
    _ans = [{"id": "A1", "body": "b1",
             "citations": [{"content": "REQ-1 规定带宽 2Gbps。"}]},
            {"id": "A2", "body": "b2",
             "citations": [{"content": "BR-2 规定延迟 500ms。"}]},
            {"id": "A3", "body": "b3",
             "citations": [{"content": "MG-1 是质量条款。"}]}]
    _script = {"A1": '{"faithful": true, "unsupported": [], "reason": "ok"}',
               "A2": '{"faithful": false, "unsupported": ["声称X"], "reason": "无据"}',
               "A3": "不是JSON"}

    def _chat_ok(msgs, _intent=""):
        body = msgs[-1]["content"]
        # 按答案正文片段分派（F7 的三条 body 互异，互不为子串）
        for a in _ans:
            if a["body"] in body:
                return {"choices": [{"message": {"content": _script[a["id"]]}}]}
        # 合成对照：neg 含 ZQ-0000 ⇒ unfaithful；pos 逐字引用 ⇒ faithful
        if "ZQ-0000" in body:
            return {"choices": [{"message": {
                "content": '{"faithful": false, "unsupported": '
                           '["ZQ-0000"], "reason": "证据中无此条款"}'}}]}
        return {"choices": [{"message": {
            "content": '{"faithful": true, "unsupported": [], "reason": "ok"}'}}]}

    seg = run_llm_judge(_ans, _chat_ok, limit=10)
    _c("F7a", "faithful_rate", 0.5, seg["faithful_rate"])
    cases.append({"case": "F7b", "metric": "three_state_counts",
                  "expect": "1/1/1", "got": "%d/%d/%d" % (
                      seg["n_faithful"], seg["n_unfaithful"], seg["n_failed"]),
                  "ok": (seg["n_faithful"], seg["n_unfaithful"], seg["n_failed"])
                  == (1, 1, 1)})
    # F7c：判失败不计入分子分母（第三态隔离 —— 环境故障不是质量差）
    cases.append({"case": "F7c", "metric": "failed_not_in_rate",
                  "expect": 2, "got": seg["n_judged"],
                  "ok": seg["n_judged"] == 2})
    # F7d：unfaithful 明细进 examples（供人工复核）
    cases.append({"case": "F7d", "metric": "unfaithful_examples",
                  "expect": 1, "got": len(seg["unfaithful_examples"]),
                  "ok": len(seg["unfaithful_examples"]) == 1
                  and seg["unfaithful_examples"][0]["id"] == "A2"})

    # F8：自证对照 —— pos 应 faithful、neg 应 unfaithful ⇒ ok=True
    cases.append({"case": "F8a", "metric": "control.ok",
                  "expect": True, "got": seg["control"]["ok"],
                  "ok": seg["control"]["ran"] is True
                  and seg["control"]["ok"] is True})
    # F8b：对照被污染（canned 全判 faithful）⇒ ok=False（judge 可疑）
    def _chat_all_faithful(msgs, _intent=""):
        return {"choices": [{"message": {
            "content": '{"faithful": true, "unsupported": [], "reason": "x"}'}}]}

    seg_bad = run_llm_judge(_ans, _chat_all_faithful, limit=10)
    cases.append({"case": "F8b", "metric": "control_contaminated_detected",
                  "expect": False, "got": seg_bad["control"]["ok"],
                  "ok": seg_bad["control"]["ok"] is False
                  and seg_bad["control"]["neg_ok"] is False})

    # F9：调用异常 ⇒ call_failed 记账（不抛、不猜）
    def _chat_boom(msgs, _intent=""):
        raise RuntimeError("ProxyError: 502")

    seg_boom = run_llm_judge(_ans, _chat_boom, limit=10)
    # ⚠ n_failed 只数**采样答案**（3 条）；合成对照的失败不进这个计数
    #   （对照是 judge 自证基础设施，它的成败看 control.ok/neg_ok）。
    #   第一版预期 5 是把对照失败也算进去——语义错，不是代码错。
    cases.append({"case": "F9a", "metric": "call_failed_counted",
                  "expect": 3, "got": seg_boom["n_failed"],
                  "ok": seg_boom["n_failed"] == 3
                  and seg_boom["n_judged"] == 0
                  and seg_boom["faithful_rate"] is None})
    cases.append({"case": "F9b", "metric": "all_failed_control_skipped",
                  "expect": False, "got": seg_boom["control"]["ok"],
                  "ok": seg_boom["control"]["ok"] is False})
    return cases


# ══════════════════════════════════════════════════════════════════════
# 生产库取材（只读）
# ══════════════════════════════════════════════════════════════════════
def _connect_ro(db_path):
    if not os.path.exists(db_path):
        raise SystemExit("找不到数据库: %s" % db_path)
    uri = "file:%s?mode=ro" % os.path.abspath(db_path).replace("\\", "/")
    return sqlite3.connect(uri, uri=True)


def load_production_answers(conn, limit=200):
    """取 RAG 问答域答案 + 全程记账（排除了什么、为什么）。

    返回 (answers, accounting)。accounting 里每一项都是**可见计数**——
    静默丢样本比丢错样本更糟（看板看不到被丢的，就会把残缺当全量）。
    """
    conn.row_factory = sqlite3.Row
    acc = {"assistant_text_total": 0, "sampled": 0, "mock_excluded": 0,
           "attachment_conv_excluded": 0, "card_types_excluded": 0,
           "unparsable_card_data": 0}

    rows = conn.execute(
        "SELECT id, conversation_id, content, card_data FROM messages "
        "WHERE role='assistant' AND msg_type='text' ORDER BY id DESC LIMIT ?",
        (limit,)).fetchall()
    acc["assistant_text_total"] = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE role='assistant' AND msg_type='text'"
    ).fetchone()[0]
    acc["sampled"] = len(rows)

    # 附件消息集合：**早于答案**的用户附件消息（附件上下文跨轮持续，但答案
    # 之后的附件不该株连此前的答案 —— 第一版按"会话里有任何附件"整会话
    # 排除，多株连了后上传场景；按 id 先后判定才准确）。
    att_msg_ids = {}
    for r in conn.execute(
            "SELECT id, conversation_id FROM messages "
            "WHERE role='user' AND attachments IS NOT NULL "
            "AND TRIM(attachments) NOT IN (?,?,?,?,?)",
            _ATT_EMPTY).fetchall():
        att_msg_ids.setdefault(r["conversation_id"], []).append(r["id"])

    answers = []
    for r in rows:
        try:
            cd = json.loads(r["card_data"] or "{}")
        except Exception:
            acc["unparsable_card_data"] += 1
            continue
        if not isinstance(cd, dict):
            acc["unparsable_card_data"] += 1
            continue
        if cd.get("used_mock"):
            acc["mock_excluded"] += 1     # Mock 不是系统能力证据（同 trace2dataset 口径）
            continue
        if any(mid < r["id"] for mid in att_msg_ids.get(r["conversation_id"], [])):
            acc["attachment_conv_excluded"] += 1
            continue
        cits = cd.get("citations") or []
        if not isinstance(cits, list):
            acc["unparsable_card_data"] += 1
            continue
        answers.append({"id": r["id"], "body": r["content"] or "",
                        "citations": cits})
    return answers, acc


def _db_fingerprint(conn):
    row = conn.execute(
        "SELECT COUNT(*) AS n, MIN(id) AS lo, MAX(id) AS hi FROM messages "
        "WHERE role='assistant' AND msg_type='text'").fetchone()
    return {"messages_text_n": row["n"], "id_range": [row["lo"], row["hi"]]}


def _gate(key, fallback):
    """惰性读 core.config.eval_gates（2026-10-22 P0-1 的教训：顶层 import
    会在设 MBSE_DB_PATH 前把 DB 路径绑死，污染干净库口径）。"""
    try:
        if ROOT not in sys.path:
            sys.path.insert(0, ROOT)
        from core.config import get as _g
        v = _g("eval_gates", key, None)
        return fallback if v is None else v
    except Exception:
        return fallback


def _top_ungrounded(per_answer, k=20):
    out = []
    for a in per_answer:
        for e in a.get("ungrounded") or []:
            out.append({"message_id": a["id"], "entity": e,
                        "kind": a["kind"]})
    return out[:k]


def main():
    ap = argparse.ArgumentParser(description="生成质量评测（faithfulness 信号）")
    ap.add_argument("--db", default=os.environ.get("MBSE_DB_PATH") or DEFAULT_DB)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--save", action="store_true",
                    help="把主指标落库 eval_reports（eval_kind=generation_quality）")
    ap.add_argument("--limit", type=int, default=200,
                    help="采样最近 N 条 text 答案（默认 200）")
    ap.add_argument("--no-fixture", action="store_true", help="跳过黄金样本自证")
    ap.add_argument("--llm-judge", action="store_true",
                    help="启用 LLM 忠实度评判（断链③语义层；默认关——确定性"
                         "路径保持秒级，LLM 层进 nightly 与显式运行）")
    ap.add_argument("--judge-limit", type=int, default=15,
                    help="LLM 评判采样条数上限（只评带引用的答案，默认 15）")
    args = ap.parse_args()

    print("=" * 74)
    print("生成质量评测（citation 完整性 + 使用率 + 接地信号%s）"
          % (" + LLM 忠实度评判" if args.llm_judge else ""))
    print("=" * 74)

    fixture_ok, fixture_cases = (True, [])
    if not args.no_fixture:
        print("\n=== 黄金样本自证（F1~F9）===")
        fixture_ok, fixture_cases = run_fixture()
        for c in fixture_cases:
            print("  [%s] %s %s expect=%s got=%s"
                  % ("PASS" if c["ok"] else "FAIL", c["case"], c["metric"],
                     c["expect"], c["got"]))
        print("自证结论：%s" % ("通过" if fixture_ok else "未通过 —— 指标算错，"
                               "生产数字一律不可信"))

    conn = _connect_ro(args.db)
    try:
        answers, acc = load_production_answers(conn, limit=args.limit)
        fp = _db_fingerprint(conn)
    finally:
        conn.close()

    print("\n=== 取材记账（排除了什么、为什么，全部可见）===")
    for k, v in acc.items():
        print("  %-26s %s" % (k, v))

    r = evaluate_answers(answers)
    if r["n_answers"] == 0:
        # 0 条样本不是"质量差"是"没数据/取材坏了" —— 与检索评测同口径：
        # 空库跑出的 recall≈0 没有意义，空样本算出的指标同样没有意义。
        # 这里显式失败并打印取材记账，让原因（Mock 全量？库被换？）一眼可见。
        print("\n✗ 无可评测答案（n=0）—— 取材记账见上。")
        print("  常见原因：库里没有 text 助手消息 / 全部被 Mock 或附件过滤排除。")
        print("  这不是'生成质量差'，是'没测到'；不写报告、不落库。")
        return 2
    print("\n=== 指标（n=%d 条 RAG 问答）===" % r["n_answers"])
    for k in ("citation_valid_rate", "citation_usage_rate",
              "grounded_entity_rate", "no_citation_factual_rate"):
        v = r.get(k)
        print("  %-26s %s" % (k, "n/a（分母为 0）" if v is None else "%.3f" % v))
    print("  %-26s %d / %d（有效标记/总标记）"
          % ("markers", r["valid_markers"], r["total_markers"]))
    print("  %-26s %d / %d（接地实体/可核查实体）"
          % ("entities", r["n_grounded_entities"], r["n_entities"]))
    print("  %-26s %d（schema 探针：无正文引用块）"
          % ("citations_without_content", r["citations_without_content"]))
    ctl = r["control"]
    print("  负对照（池化它答案引用）   %s ⇒ %s"
          % ("%.3f" % ctl["pooled_grounded_rate"]
             if ctl["pooled_grounded_rate"] is not None else "n/a",
             "可判别" if ctl["discriminates"] else "不具判别力（见文件头说明）"))

    # ── LLM 忠实度评判（opt-in；三态记账 + 合成对照自证）───────────
    judge_seg = {"enabled": False}
    if args.llm_judge:
        print("\n=== LLM 忠实度评判（faithfulness 语义层）===")
        from llm import llm_client

        def _chat(msgs, _intent=""):
            return llm_client.chat(msgs, _intent=_intent or "gen_faithfulness")

        judge_seg = run_llm_judge(answers, _chat, limit=args.judge_limit,
                                  model_label="")
        # model 标签事后从 stats 取（chat 内部才知路由到哪个 provider）
        judge_seg["model"] = (llm_client.stats.get("last_provider", "")
                              or "unknown")
        print("  %-26s %s（采样 %d / 带引用池 %d）"
              % ("faithful_rate", judge_seg["faithful_rate"],
                 judge_seg["n_sampled"], judge_seg["n_pool"]))
        print("  %-26s faithful %d / unfaithful %d / 判不了 %d"
              % ("三态记账", judge_seg["n_faithful"], judge_seg["n_unfaithful"],
                 judge_seg["n_failed"]))
        c = judge_seg["control"]
        print("  %-26s %s%s"
              % ("judge 自证（合成对照）",
                 "通过" if c["ok"] else ("未跑：%s" % c["note"] if not c["ran"]
                                        else "失败：%s" % c["note"]),
                 "" if c["ran"] and c["ok"] else ""))
        for ex in judge_seg["unfaithful_examples"][:5]:
            print("    - msg=%s 无据声称=%s（%s）"
                  % (ex["id"], (ex.get("unsupported") or [""])[0][:40],
                     (ex.get("reason") or "")[:40]))
        if judge_seg["n_failed"] and not judge_seg["n_judged"]:
            print("  ⚠ 全部评判失败（LLM 服务不可用？）—— 语义层没测到，"
                  "不是质量差；门禁据此判 SKIP")

    report = {
        "kind": "generation_quality",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "db": os.path.basename(args.db),
        "db_fingerprint": fp,
        "population": acc,
        "summary": {k: r[k] for k in (
            "n_answers", "n_with_citations", "n_without_citations",
            "citation_valid_rate", "citation_usage_rate",
            "grounded_entity_rate", "no_citation_factual_rate",
            "total_markers", "valid_markers", "answers_using_citations",
            "citations_without_content",
            "n_entities", "n_grounded_entities", "control")},
        "fixture_self_check": {"ok": fixture_ok, "cases": fixture_cases},
        "ungrounded_examples": _top_ungrounded(r["per_answer"]),
        "per_answer": r["per_answer"],
        "llm_judge": judge_seg,
    }
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with io.open(args.out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    print("\n报告: %s" % os.path.relpath(args.out, ROOT).replace("\\", "/"))

    if args.save:
        _save(report)
    return 0 if fixture_ok else 1


def _save(report):
    """主指标落库 eval_reports（eval_kind=generation_quality）。

    列语义：entity_f1 ← citation_valid_rate（门禁主指标），relation_f1 ←
    citation_usage_rate，doc_* 列不适用（那不是检索指标）⇒ None 而非 0
    （0 会被看板读成"测到 0 分"，是假绿）。
    gen_* 列见 database/migrations/misc.py 的迁移注释；gen_faithful_rate
    ← LLM 忠实率（未启用 LLM 评判时 NULL，不是 0）。
    """
    sys.path.insert(0, ROOT)
    from database.connection import get_db
    s = report["summary"]
    j = report.get("llm_judge") or {}
    conn = get_db()
    try:
        cur = conn.execute(
            "INSERT INTO eval_reports (doc_id, doc_name, entity_f1, relation_f1, "
            "detail, created_by, model_version, is_golden, eval_kind, "
            "gen_citation_valid_rate, gen_citation_usage_rate, "
            "gen_grounded_entity_rate, gen_no_citation_factual_rate, "
            "gen_faithful_rate) "
            "VALUES (0,?,?,?,?,?,?,0,?,?,?,?,?,?)",
            ("生成质量评测 (n=%d 条 RAG 问答)" % s["n_answers"],
             s["citation_valid_rate"], s["citation_usage_rate"],
             json.dumps(report, ensure_ascii=False),
             "tools/eval/eval_generation_quality",
             "gen-eval %s" % report["db"],
             "generation_quality",
             s["citation_valid_rate"], s["citation_usage_rate"],
             s["grounded_entity_rate"], s["no_citation_factual_rate"],
             j.get("faithful_rate") if j.get("enabled") else None))
        # O3：指标快照表（每指标一行，三色落库时算好；固定列照旧保留）
        from repositories.eval_report_repo import write_metric_rows
        write_metric_rows(conn, cur.lastrowid, "generation_quality", {
            "gen_citation_valid_rate": s["citation_valid_rate"],
            "gen_citation_usage_rate": s["citation_usage_rate"],
            "gen_grounded_entity_rate": s["grounded_entity_rate"],
            "gen_no_citation_factual_rate": s["no_citation_factual_rate"],
            "gen_faithful_rate": j.get("faithful_rate") if j.get("enabled") else None,
        })
        conn.commit()
        print("eval_reports 落库成功: id=%d [eval_kind=generation_quality]%s"
              % (cur.lastrowid,
                 " faithful_rate=%s" % j["faithful_rate"]
                 if j.get("enabled") else ""))
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
