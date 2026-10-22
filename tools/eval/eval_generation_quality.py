#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""生成质量评测器（2026-10-22）—— 补上断链③「无生成质量指标」。

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
    return all(c["ok"] for c in cases), cases


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
    args = ap.parse_args()

    print("=" * 74)
    print("生成质量评测（citation 完整性 + 使用率 + 接地信号）")
    print("=" * 74)

    fixture_ok, fixture_cases = (True, [])
    if not args.no_fixture:
        print("\n=== 黄金样本自证（F1~F5）===")
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
    gen_* 四列见 database/migrations/misc.py 的迁移注释。
    """
    sys.path.insert(0, ROOT)
    from database.connection import get_db
    s = report["summary"]
    conn = get_db()
    try:
        cur = conn.execute(
            "INSERT INTO eval_reports (doc_id, doc_name, entity_f1, relation_f1, "
            "detail, created_by, model_version, is_golden, eval_kind, "
            "gen_citation_valid_rate, gen_citation_usage_rate, "
            "gen_grounded_entity_rate, gen_no_citation_factual_rate) "
            "VALUES (0,?,?,?,?,?,?,0,?,?,?,?,?)",
            ("生成质量评测 (n=%d 条 RAG 问答)" % s["n_answers"],
             s["citation_valid_rate"], s["citation_usage_rate"],
             json.dumps(report, ensure_ascii=False),
             "tools/eval/eval_generation_quality",
             "gen-eval %s" % report["db"],
             "generation_quality",
             s["citation_valid_rate"], s["citation_usage_rate"],
             s["grounded_entity_rate"], s["no_citation_factual_rate"]))
        conn.commit()
        print("eval_reports 落库成功: id=%d [eval_kind=generation_quality]" % cur.lastrowid)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
