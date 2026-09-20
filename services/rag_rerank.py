# -*- coding: utf-8 -*-
"""RAG LLM Rerank（V2.6，用户拍板新增）：对 hybrid_search 的 RRF 候选做 LLM 相关性重排。

- 配置开关：~/.workbuddy/mbse_config.json → "rag": {"rerank_enabled": true}（默认开启）
- 模型：复用系统默认 LLM（deepseek），零新增依赖；LLM 不可用/超时/解析失败 → 静默回退原排序
- 打分：0-10 相关性分，写入 hit["rerank_score"]，hit["reranked"]=True（可观测）
"""
import json


def llm_rerank(query: str, hits: list, top_k: int = 5, max_candidates: int = 8,
               timeout: int = 25) -> list:
    """按 LLM 相关性分重排 hits（原列表元素就地补 rerank_score/reranked 字段）。

    返回排序后的列表（截断到 top_k）。任何异常/关闭 → 原样返回（静默降级，不阻断检索主链路）。
    """
    if not hits or len(hits) <= 1:
        return hits
    try:
        from core import config as _cfg
        if not _cfg.get("rag", "rerank_enabled", True):
            return hits[:top_k]
    except Exception:
        pass
    try:
        from llm import llm_client
        cand = hits[:max_candidates]
        segs = "\n".join(
            f"[{i}] 章节：{h.get('section', '') or '-'}\n内容：{(h.get('content') or '')[:300]}"
            for i, h in enumerate(cand))
        resp = llm_client.chat(
            [{"role": "system", "content": (
                "你是检索重排器。给定用户查询与若干候选片段，逐段评估与查询的相关性（0=无关，10=直接回答查询）。"
                '只输出 JSON：{"ratings":[{"i":候选序号,"score":0到10}]}，必须覆盖全部候选，不要其他文字。')},
             {"role": "user", "content": f"查询：{query}\n\n候选片段：\n{segs}"}],
            _intent="rag_rerank")
        msg = (resp.get("choices") or [{}])[0].get("message", {}) or {}
        m = json.loads(msg.get("content") or "{}")
        scores = {int(x["i"]): float(x["score"]) for x in (m.get("ratings") or [])
                  if isinstance(x, dict) and str(x.get("i", "")).lstrip("-").isdigit()}
        if not scores:
            return hits[:top_k]
        for i, h in enumerate(cand):
            s = scores.get(i)
            if s is not None:
                h["rerank_score"] = round(max(0.0, min(10.0, s)), 2)
                h["reranked"] = True
            else:
                h["rerank_score"] = 0.0
                h["reranked"] = False
        reranked = sorted(cand, key=lambda h: h.get("rerank_score", 0.0), reverse=True) + hits[max_candidates:]
        return reranked[:top_k]
    except Exception:
        return hits[:top_k]
