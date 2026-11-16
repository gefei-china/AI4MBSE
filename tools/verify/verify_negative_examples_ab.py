# CI-OPTIONAL: [local] tier=local（见 tools/gates_manifest.json）：需真实服务 / 真 LLM key / 生产真库金标或随包运行时资产，CI 无服务无 secret ⇒ 跑不了；本机已实测通过，改法须同时改清单 tier 并接线，不允许只删这行
"""代码级 Router 优化点 ②：**易混淆负例** A/B（2026-11-07 米爸第 14 轮第 4 问）。

## 依据
Eucalipse 生产路由 playbook：分类质量 80% 是数据、20% 是架构；
「加负例：看起来像但**绝不该**匹配的说法」是准确率的主要来源，
每意图 <20 条样本时误路由明显（本平台 48 条 confirmed 摊到 21 个 Agent = 2.3 条/意图）。
本仓真实错例（verify_router_calibration / verify_cluster_cost_eval 实测）：
  · 「帮我定清楚验收标准」→ 被判 requirement_quality（应 requirement_analysis）
  · 「系统要快速响应，这条需求写得合格吗」→ 被判 chat / requirement_analysis（应 quality）
  · 「生成追溯矩阵标出孤儿元素」→ 被判 design（应 trace_verification）
  · 「按八视图展开 BDD」→ 被判 design（应 view_expansion）
  · 「改了接口定义会影响什么」→ 曾被"定义"骗成知识问答（已由组合规则修）

## 实验
同一批 18 条 gold（含 6 条易混淆对），同一 LLM/温度，唯一变量 =
簇模式提示词里是否注入「反例对照」段。测准确率 / parse_fail / token。
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from llm import llm_client  # noqa: E402

# (文本, 期望族, 是否易混淆样本)
GOLD = [
    ("帮我定清楚验收标准", "requirement", 1),
    ("系统要快速响应，这条需求写得合格吗", "requirement", 1),
    ("把访谈记录条目化，每条带 ID", "requirement", 0),
    ("这条需求有哪些模糊词", "requirement", 0),
    ("生成追溯矩阵，标出孤儿元素", "verify", 1),
    ("按八视图顺序把 BDD 和活动图展开成代码", "design", 1),
    ("这段 V2 代码语法报错，帮我诊断并修到通过", "verify", 1),
    ("批量删除这些包之前先评估安全边界", "change", 1),
    ("改了发动机的接口定义，会影响哪些部件", "change", 1),
    ("帮我设计无人机地面站架构，给两套方案对比", "design", 0),
    ("这个模型帮我预评审一下打个分", "verify", 0),
    ("把刚才的分析整理成报告导出来", "report", 0),
    ("SysML v2 里 part def 和 part 有什么区别", "knowledge", 0),
    ("这个平台怎么新建工程，在哪个菜单", "knowledge", 0),
    ("通过 MCP 读取工程 AI验证006 的 V2 代码", "platform", 0),
    ("校验通过后把模型发布入库", "platform", 0),
    ("建模前该按哪套方法论走，给我规约卡", "design", 0),
    ("校验一下这段 V2 代码是否符合规范", "verify", 0),
]

NEG = """
## 易混淆反例（**必须按右侧判，不要按字面词判**）
- 「这条需求写得合格吗」「有哪些模糊词」→ requirement 族，但族内是**评审**（agent=requirement_quality）；
  「帮我定清楚验收标准」「怎么把模糊诉求写成规格」→ 同族但族内是**规格化**（agent=requirement_analysis）。
  「写得合格吗」不等于「帮我定」，【帮我定/怎么写/怎么拆】是生成，【合格吗/有哪些问题】是评审。
- 「改了 X 的**接口定义**会影响什么」→ change 族；**不要**因为出现「定义」就判 knowledge（那是名词不是"请解释定义"）。
- 「生成追溯矩阵/孤儿元素/覆盖核验」→ verify 族内 agent=trace_verification，**不是** design；
  「按八视图展开 BDD/活动图代码」→ design 族内 agent=view_expansion，**不是** report。
- 「语法报错，帮我诊断修到通过」→ verify 族内 agent=model_validation_repair；
  「预评审打个分」→ 同族但 agent=review。
- 「校验通过后发布入库」→ platform 族内 agent=model_release，**不是** verify（"校验"只是前置条件）。
"""


def _build(with_neg: bool) -> str:
    from core.capability_clusters import clusters_for_llm_detailed
    base = (
        "你是 MBSE 助手意图分类器。只输出一个 JSON 对象，不要任何其他文字。\n"
        '格式：{"clusters":[{"id":"族id","agent":"族内成员名或留空",'
        '"confidence":0.0~1.0}], "confidence":0.0~1.0}\n'
        "规则：① 一句话涉及多类能力时可给多个簇，按主次排序，最多 3 个；"
        "② 只有明确点名某个阶段动作时才填 agent，必须原样取自族内可选成员清单；"
        "③ 全部不匹配时用 id=chat。\n\n"
        "可选能力族：\n" + clusters_for_llm_detailed())
    return base + (NEG if with_neg else "")


def _norm(raw: str) -> str:
    s = str(raw or "")
    hits = re.findall(r'"(?:id|intent|cluster)"\s*:\s*"([\w_]+)"', s)
    return hits[0] if hits else ""


def _one(with_neg, text, expect, confusable):
    t0 = time.time()
    try:
        r = llm_client.chat(
            [{"role": "system", "content": _build(with_neg)},
             {"role": "user", "content": text}],
            _intent="intent_neg_ab", max_tokens=260, temperature=0.0)
        u = r.get("usage") or {}
        content = ((r.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        return {"neg": with_neg, "text": text[:26], "expect": expect,
                "got": _norm(content), "hit": _norm(content) == expect,
                "confusable": confusable, "ms": int((time.time() - t0) * 1000),
                "tok": int(u.get("total_tokens") or 0),
                "parse_fail": not _norm(content)}
    except Exception as e:
        return {"neg": with_neg, "text": text[:26], "expect": expect, "got": "",
                "hit": False, "confusable": confusable, "ms": 0, "tok": 0,
                "parse_fail": True, "err": "%s: %s" % (type(e).__name__, str(e)[:80])}


def main() -> int:
    print("=" * 78)
    print("易混淆负例 A/B（18 条 gold × 2 组，唯一变量 = 提示词是否含反例段）")
    print("=" * 78, flush=True)
    jobs = [(ng, t, e, cf) for ng in (False, True) for t, e, cf in GOLD]
    rows = []
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = [ex.submit(_one, *j) for j in jobs]
        for f in as_completed(futs):
            rows.append(f.result())
    out = {}
    for ng in (False, True):
        g = [x for x in rows if x["neg"] is ng]
        cf = [x for x in g if x["confusable"]]
        out["neg" if ng else "base"] = {
            "n": len(g), "acc": round(sum(x["hit"] for x in g) / len(g), 3),
            "confusable_acc": round(sum(x["hit"] for x in cf) / len(cf), 3),
            "parse_fail": sum(x["parse_fail"] for x in g),
            "avg_tok": round(sum(x["tok"] for x in g) / len(g)),
            "miss": [{"text": x["text"], "exp": x["expect"], "got": x["got"]}
                     for x in g if not x["hit"]],
        }
        d = out["neg" if ng else "base"]
        print("%-6s acc=%.1f%% 易混淆子集=%.1f%% parse_fail=%d avg_tok=%d" %
              ("带负例" if ng else "基线", d["acc"] * 100,
               d["confusable_acc"] * 100, d["parse_fail"], d["avg_tok"]), flush=True)
        for m in d["miss"][:5]:
            print("    MISS %-28s exp=%-14s got=%s" % (m["text"], m["exp"], m["got"]),
                  flush=True)
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "tmp", "negative_examples_ab.json")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    json.dump(out, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("\n结果已存 %s" % p, flush=True)
    print("ALL_OK=True", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
