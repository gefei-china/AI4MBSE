# CI-OPTIONAL: [local] tier=local（见 tools/gates_manifest.json）：需真实服务 / 真 LLM key / 生产真库金标或随包运行时资产，CI 无服务无 secret ⇒ 跑不了；本机已实测通过，改法须同时改清单 tier 并接线，不允许只删这行
"""意图识别归属 A/B 实验（2026-11-07，米爸第 3/4 问的实证部分）。

## 要回答什么
Q3：意图识别放在「主 Agent 内」还是「单独维护一个子 Agent」，哪个效率/准确度更高？
Q4：把当前 22 个 Agent 候选池按能力簇聚合，能不能同时提升效率与准确度？

## 实验矩阵（2 × 2，唯一变量原则）
维度一 · **路由归属**（两种都要真实调 LLM）：
  · `dedicated` = 独立专用分类器（轻量 prompt，只输出意图 JSON，可缓存/可单测）
  · `in_main`   = 主 Agent 内嵌决策（prompt 里塞角色+全部候选+委派规则，输出决策+理由）
维度二 · **候选池**：
  · `flat22` = 当前 22 个 active Agent 逐条列出（现状）
  · `cluster8` = 按能力簇聚合成 8 个互斥族

再加一条**基线** `current` = 平台现有 IntentRouter.detect（确定性规则/语义优先，
只有无信号时才走 LLM），记录它是否触发 LLM 往返、耗时、命中率。

## 判据（为什么按"族"判准确，而不是按具体 Agent）
现状的病根正是"22 选 1"：候选池本身不互斥（design 与 工程建模方案设计Agent 的
display_name 与 prompt 逐字相同）。按具体 Agent 判准会把这种结构性歧义算成模型错误。
因此 gold label 用**能力族**；输出只要落到正确的族即算对。这同时正是 Q4 方案的目标形态。

## 诚实边界
- B/C/D 三组是**同一 LLM、同一 temperature、同一 gold set、同样 1 次调用**的对照，
  差别只在 prompt 构造。它们不是"已上线的架构"，是决策依据。
- 主 Agent 内嵌组测的是**路由决策这一次调用的开销**，不含后续答复生成；
  真实系统里主 Agent 还要继续生成，故它的实际成本只会更高，不会更低。
- 结果落到 tmp/intent_ab_result.json，脚本只统计不改写任何业务数据。
"""
from __future__ import annotations

import json
import os
import re
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from llm import llm_client  # noqa: E402

# ── 能力簇：把 22 个 active Agent 归到 8 个互斥族（与"策略即数据"一致，可外置）──
CLUSTERS = {
    "requirement": {
        "title": "需求工程",
        "desc": "把诉求转成需求规格；评审已写好的需求质量；把长文本拆成需求条目",
        "members": ["requirement_analysis", "requirement_quality", "requirement_structuring"],
        "hint": "模糊诉求要定清楚、验收标准怎么写、需求模糊词检测、需求条目化",
    },
    "design": {
        "title": "设计与建模",
        "desc": "系统架构方案设计、多方案权衡、骨架/视图生成、方法论选择",
        "members": ["design", "工程建模方案设计Agent", "多方案设计", "多方案生成",
                    "architecture_skeleton", "view_expansion", "methodology_resolver"],
        "hint": "设计架构、出方案对比、生成 BDD/活动图/时序图等视图、建模方法论",
    },
    "verify": {
        "title": "校验与评审",
        "desc": "模型预评审打分、语法校验修复、需求-模型追溯核验",
        "members": ["review", "model_validation_repair", "trace_verification"],
        "hint": "预评审打分、检查语法错、校验修复循环、追溯矩阵/覆盖率",
    },
    "change": {
        "title": "变更与影响",
        "desc": "分析变更波及范围、删除/替换的安全门评估",
        "members": ["impact", "change_safety_gate"],
        "hint": "改了X会影响什么、影响范围、删除影响分析、变更安全门",
    },
    "report": {
        "title": "报告与导出",
        "desc": "把分析结果组装成工程报告并导出 md/docx/pdf",
        "members": ["report_generation"],
        "hint": "生成报告、导出报告、整理成文档",
    },
    "knowledge": {
        "title": "知识与问答",
        "desc": "解释 MBSE/SysML v2 概念、规范条款、平台操作与跨域兜底问答",
        "members": ["knowledge_qa", "chat"],
        "hint": "什么是X、X和Y区别、规范怎么要求、平台菜单在哪、闲聊",
    },
    "platform": {
        "title": "平台数据读写",
        "desc": "通过 MCP 读取/写入智源平台工程、分支、模型与源码，模型发布入库",
        "members": ["智源对接agent", "智源数据读写agent", "model_release"],
        "hint": "读取工程V2代码、查询projectId、导出SysML源码、模型落库发布",
    },
    "orchestration": {
        "title": "编排总控",
        "desc": "跨阶段任务拆解与多 Agent 协同（主 Agent，不直接承接用户意图）",
        "members": ["MBSE建模总体负责人"],
        "hint": "完整建模流水线、端到端交付、多阶段协同",
    },
}

# ── gold set：12 条，覆盖 8 族；期望值是**能力族** ──
GOLD = [
    ("我有个需求描述得很模糊：系统要快速响应。这条需求怎么定？验收标准怎么写？", "requirement"),
    ("这份需求里有哪些模糊词和不可验证的表述？出个问题清单", "requirement"),
    ("把这段用户访谈记录条目化成需求清单，每条给 ID", "requirement"),
    ("帮我设计一下无人机地面控制站的系统架构，给两套方案对比", "design"),
    ("按八视图顺序生成这个模型的 BDD 和活动图代码", "design"),
    ("建模前该按哪套方法论走？给我个规约卡", "design"),
    ("这个模型语法上有问题吗？帮我预评审一下打个分", "verify"),
    ("生成需求到模型的追溯矩阵，看看有没有孤儿元素", "verify"),
    ("改了发动机的接口定义，会影响哪些部件？给我影响范围", "change"),
    ("批量删除这些包之前，先做一下变更安全门评估", "change"),
    ("把刚才的分析整理成一份报告导出来", "report"),
    ("SysML v2 里 part def 和 part 有什么区别？", "knowledge"),
    ("这个平台怎么新建一个工程？在哪个菜单", "knowledge"),
    ("通过 MCP 读取工程 AI验证006 的 V2 代码", "platform"),
    ("把校验通过的模型发布入库，生成新版本", "platform"),
]

# flat22：真实库里的 22 个 active Agent（display_name + 描述摘要），只取标题+一句话
FLAT22 = [
    ("MBSE建模总体负责人", "MBSE 建模流水线的总控与多 Agent 编排"),
    ("多方案设计", "多方案设计与对比"),
    ("智源对接agent", "智源平台 MCP 读写与 SysML v2 源码导出"),
    ("架构骨架生成Agent", "按方法论生成包结构与骨架代码"),
    ("变更安全门Agent", "删除/替换前的安全门与影响评估"),
    ("通用问答Agent", "未被专门 Agent 认领的兜底问答与平台操作指引"),
    ("工程建模方案设计Agent", "多方案架构设计、设计权衡与选型"),
    ("变更影响Agent", "沿依赖链做变更影响范围分析"),
    ("知识问答Agent", "MBSE/SysML v2 概念解释与规范条款解读"),
    ("方法论解析Agent", "建模方法论与规约解析"),
    ("模型发布Agent", "模型落库与版本发布"),
    ("模型校验修复Agent", "语法诊断与自动修复循环"),
    ("报告生成Agent", "工程报告组装与导出"),
    ("需求分析Agent", "模糊诉求转可验收的需求规格"),
    ("需求质量分析Agent", "检测模糊词/不可验证表述，出质量清单"),
    ("需求结构化Agent", "长文本拆分为需求条目"),
    ("预评审Agent", "四类校验与可评审性评分"),
    ("追溯核验Agent", "需求-模型追溯矩阵与覆盖率核验"),
    ("视图展开Agent", "按八视图规范展开 SysML v2 代码"),
    ("多方案生成", "变体空间候选提议与对比筛选"),
    ("工程建模方案设计Agent(重复)", "多方案架构设计与选型（与上面重复）"),
    ("智源数据读写agent", "通过 MCP 查询/修改平台数据"),
]

ALIAS = {
    "需求工程": "requirement", "需求": "requirement",
    "设计与建模": "design", "设计": "design", "建模": "design",
    "校验与评审": "verify", "校验": "verify", "评审": "verify",
    "变更与影响": "change", "变更": "change", "影响": "change",
    "报告与导出": "report", "报告": "report",
    "知识与问答": "knowledge", "知识": "knowledge", "问答": "knowledge",
    "平台数据读写": "platform", "平台": "platform",
    "编排总控": "orchestration", "编排": "orchestration",
}
# 具体 Agent 名 → 族（兼容模型直接回 Agent 名）
for _k, _v in CLUSTERS.items():
    for _m in _v["members"]:
        ALIAS[_m] = _k
ALIAS.update({
    "通用问答Agent": "knowledge", "知识问答Agent": "knowledge",
    "需求分析Agent": "requirement", "需求质量分析Agent": "requirement",
    "需求结构化Agent": "requirement", "架构骨架生成Agent": "design",
    "视图展开Agent": "design", "方法论解析Agent": "design",
    "工程建模方案设计Agent": "design", "工程建模方案设计Agent(重复)": "design",
    "多方案设计": "design", "多方案生成": "design", "变更影响Agent": "change",
    "变更安全门Agent": "change", "预评审Agent": "verify",
    "模型校验修复Agent": "verify", "追溯核验Agent": "verify",
    "报告生成Agent": "report", "智源对接agent": "platform",
    "智源数据读写agent": "platform", "模型发布Agent": "platform",
    "MBSE建模总体负责人": "orchestration",
})


def _norm(raw: str) -> str:
    """把模型输出归一到族名。**严格口径**：只认结构化 JSON 里的 intent 字段。

    ⚠️ 首版在这里踩了「验证器自己造假红」：找不到 JSON 时把整段正文当候选，
    再用 ALIAS 里的短词（"需求"/"设计"/"建模"）做子串模糊匹配 ⇒ 几乎必然
    随机命中某一族，把**解析失败**伪装成**分类错误**（in_main/flat22 首版
    acc=40% 里大部分是这种伪错）。判据必须能区分：
      · `parse_fail`（没输出可解析的 JSON）—— 是提示词/预算问题
      · `wrong`（输出了 JSON 但族选错）—— 才是分类准确度问题
    """
    s = str(raw or "").strip()
    hits = re.findall(r'"(?:intent|cluster|族|能力簇|category)"\s*:\s*"([^"]+)"', s)
    if not hits:
        return ""                      # 解析失败 ⇒ 算错，但单独统计，不当分类错
    cand = hits[-1].strip()            # 主 Agent 可能中途举例，取最后一个决策
    if cand in CLUSTERS:
        return cand
    if cand in ALIAS:
        return ALIAS[cand]
    # 只允许类别名/族标题做精确前缀匹配，禁止对整段正文模糊扫词
    for k, v in ALIAS.items():
        if len(k) >= 4 and (cand.startswith(k) or k.startswith(cand)):
            return v
    return ""


def _catalog(pool: str) -> str:
    if pool == "flat22":
        return "\n".join("- %s：%s" % (n, d) for n, d in FLAT22)
    return "\n".join(
        "- %s（%s）：%s｜典型说法：%s" % (k, v["title"], v["desc"], v["hint"])
        for k, v in CLUSTERS.items())


def _prompt(style: str, pool: str, text: str) -> list:
    cat = _catalog(pool)
    if style == "dedicated":
        # ⚠️ 必须显式列出合法 key：首版只说"归到最合适的类别"，模型会自造
        #   "系统架构设计" 这类标签（探活实测），被严格判据算解析失败 ⇒ 不公平。
        keys = ("、".join(n for n, _ in FLAT22) if pool == "flat22"
                else "、".join(CLUSTERS.keys()))
        sys_p = (
            "你是 MBSE 平台的意图分类器。把用户这句话归到下面唯一最合适的类别，"
            "只输出一个 JSON：{\"intent\":\"类别名\",\"confidence\":0.0~1.0}，不要任何其他文字、不要解释。\n"
            "intent 必须**原样**取自这个合法清单，不要自造名称：" + keys + "\n\n"
            "可选类别：\n" + cat)
        return [{"role": "system", "content": sys_p},
                {"role": "user", "content": text}]
    # in_main：主 Agent 内嵌决策 —— 必须把角色、全部候选、委派规则、答复要求都塞进同一次调用
    # ⚠️ 缩进必须是 4 空格（函数体）：曾误缩进成 8 空格 ⇒ 变成 dedicated 分支里的死代码，
    #    in_main 走到最后 sys_p 未定义 ⇒ 30 条全部 UnboundLocalError 被 except 吞成 parse_fail。
    keys = ("、".join(n for n, _ in FLAT22) if pool == "flat22"
            else "、".join(CLUSTERS.keys()))
    sys_p = (
        "你是 MBSE 智能平台的主 Agent（MBSE建模总体负责人）。你精通需求工程、SysML v2 建模、"
        "模型校验、变更影响分析、报告生成与知识问答。\n"
        "你的职责：先理解用户这句话，判断它属于哪个能力方向，决定是自己直接答复还是"
        "委派给对应的专门 Agent，并给出委派理由与下一步动作。\n\n"
        "下面是当前平台上可用的能力清单，委派时从中选一个：\n" + cat + "\n\n"
        "工作要求：① 先复述你对用户诉求的理解；② 说明这属于哪类能力、为什么；"
        "③ 明确给出要委派的对象或自己处理；④ 不要臆造平台没有的能力。"
        "最后另起一行输出且只输出一次 JSON："
        "{\"intent\":\"类别名\",\"confidence\":0.0~1.0}，"
        "intent 必须原样取自：" + keys)
    return [{"role": "system", "content": sys_p},
            {"role": "user", "content": text}]


def _one(style: str, pool: str, text: str, expect: str) -> dict:
    t0 = time.time()
    try:
        r = llm_client.chat(_prompt(style, pool, text), _intent="intent_ab_probe",
                            max_tokens=420 if style == "dedicated" else 1400,
                            temperature=0.0)
        u = r.get("usage") or {}
        msg = ((r.get("choices") or [{}])[0].get("message") or {})
        content = msg.get("content") or ""
        got = _norm(content)
        return {"style": style, "pool": pool, "text": text[:34], "expect": expect,
                "got": got, "hit": got == expect, "parse_fail": not got,
                "wrong": bool(got) and got != expect,
                "finish": ((r.get("choices") or [{}])[0].get("finish_reason") or ""),
                "ms": int((time.time() - t0) * 1000),
                "pt": int(u.get("prompt_tokens") or 0),
                "ct": int(u.get("completion_tokens") or 0),
                "raw": content[-260:]}
    except Exception as e:
        return {"style": style, "pool": pool, "text": text[:34], "expect": expect,
                "got": "", "hit": False, "parse_fail": True, "wrong": False,
                "finish": "exception",
                "ms": int((time.time() - t0) * 1000),
                "pt": 0, "ct": 0, "raw": "ERR %s: %s" % (type(e).__name__, str(e)[:100])}


def _baseline_current() -> dict:
    """平台现状 IntentRouter.detect：确定性层优先，只有无信号时才走 LLM。"""
    from agent.pipeline import AgentPipeline
    p = AgentPipeline()
    p._load_db_agents()
    r = p.router
    rows, llm_calls = [], 0
    for text, _exp in GOLD:
        t0 = time.time()
        got = r.detect(text)
        m = r.get_last_meta()
        route = str(m.get("route") or "")
        is_llm = route.startswith("llm_") or "llm" in route
        llm_calls += int(is_llm)
        rows.append({"text": text[:34], "intent": got, "route": route,
                     "conf": m.get("confidence"),
                     "ms": int((time.time() - t0) * 1000), "used_llm": is_llm})
    return {"rows": rows, "llm_calls": llm_calls,
            "avg_ms": round(statistics.mean(x["ms"] for x in rows), 1)}


def main() -> int:
    out = {"gold_n": len(GOLD), "pools": {"flat22": len(FLAT22),
                                          "cluster8": len(CLUSTERS)},
           "groups": {}, "errors": []}
    print("gold=%d  pools: flat22=%d cluster8=%d" %
          (len(GOLD), len(FLAT22), len(CLUSTERS)), flush=True)

    try:
        out["baseline_current"] = _baseline_current()
        b = out["baseline_current"]
        print("baseline current: avg_ms=%s llm_calls=%d/%d" %
              (b["avg_ms"], b["llm_calls"], len(GOLD)), flush=True)
    except Exception as e:
        out["errors"].append("baseline: %s: %s" % (type(e).__name__, e))
        print("baseline ERR", e, flush=True)

    jobs = []
    for style in ("dedicated", "in_main"):
        for pool in ("flat22", "cluster8"):
            for text, expect in GOLD:
                jobs.append((style, pool, text, expect))
    print("LLM jobs =", len(jobs), flush=True)

    results = []
    t_start = time.time()
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(_one, *j): j for j in jobs}
        done = 0
        for f in as_completed(futs):
            results.append(f.result())
            done += 1
            if done % 15 == 0:
                print("  ... %d/%d (%.0fs)" % (done, len(jobs), time.time() - t_start),
                      flush=True)
    out["wall_s"] = round(time.time() - t_start, 1)

    for style in ("dedicated", "in_main"):
        for pool in ("flat22", "cluster8"):
            g = [x for x in results if x["style"] == style and x["pool"] == pool]
            n = len(g)
            out["groups"]["%s/%s" % (style, pool)] = {
                "n": n,
                "acc": round(sum(x["hit"] for x in g) / n, 3),
                "avg_ms": round(statistics.mean(x["ms"] for x in g)),
                "p50_ms": round(statistics.median(x["ms"] for x in g)),
                "avg_prompt_tok": round(statistics.mean(x["pt"] for x in g)),
                "avg_completion_tok": round(statistics.mean(x["ct"] for x in g)),
                "avg_total_tok": round(statistics.mean(x["pt"] + x["ct"] for x in g)),
                "extra_llm_calls": n,   # 每组每条固定 1 次额外 LLM 调用
                "parse_fail": sum(x.get("parse_fail", False) for x in g),
                "wrong": sum(x.get("wrong", False) for x in g),
                "truncated": sum(x.get("finish") == "length" for x in g),
                "miss": [{"text": x["text"], "expect": x["expect"], "got": x["got"],
                          "parse_fail": x.get("parse_fail"), "finish": x.get("finish"),
                          "raw": x["raw"][:130]} for x in g if not x["hit"]],
            }
            s = out["groups"]["%s/%s" % (style, pool)]
            print("%-10s/%-9s acc=%.1f%% (wrong=%d parse_fail=%d trunc=%d) avg=%dms p50=%dms tok=%d(p%d+c%d)" %
                  (style, pool, s["acc"] * 100, s["wrong"], s["parse_fail"],
                   s["truncated"], s["avg_ms"], s["p50_ms"],
                   s["avg_total_tok"], s["avg_prompt_tok"], s["avg_completion_tok"]),
                  flush=True)

    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "tmp", "intent_ab_result.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("saved ->", path, flush=True)
    print("ALL_OK=True" if not out["errors"] else "errors=%s" % out["errors"], flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
