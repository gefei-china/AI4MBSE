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
        # ★ 可指定重排专用 provider（2026-10-10）：
        #   实测默认 provider（ds-v4-flash-neibu/109）当时**整体不可用**
        #   （所有 intent 一律31.7s 超时、ct=0），导致重排 100% 静默降级。
        #   ⇒ 重排对延迟更敏感（每次查询都要跑），应能独立指定一个更稳的 provider。
        _pid = 0
        try:
            _pid = int(_cfg.get("rag", "rerank_provider_id", 0) or 0)   # ★ rag 组（与 SCHEMA/DEFAULT 一致）
        except Exception:
            _pid = 0
        # ★★ 这里**必须用 `_cfg.get(...)`，不能写 `_("rag", ...)`**——
        #   `_` 在本函数里**没有定义**（那是配置文件里的组名，不是函数）⇒
        #   会抛 `NameError: name '_' is not defined`，而症状是
        #   **静默回退原排序**（表现为"开关开了但顺序一字未变"），排查花了 6 轮。
        resp = llm_client.chat(
            [{"role": "system", "content": (
                "你是检索重排器。给定用户查询与若干候选片段，逐段评估与查询的相关性（0=无关，10=直接回答查询）。"
                '只输出 JSON：{"ratings":[{"i":候选序号,"score":0到10}]}，必须覆盖全部候选，不要其他文字。')},
             {"role": "user", "content": f"查询：{query}\n\n候选片段：\n{segs}"}],
            _intent="rag_rerank",
            **({"provider_id": _pid} if _pid else {}))
        msg = (resp.get("choices") or [{}])[0].get("message", {}) or {}
        try:
            m = json.loads(msg.get("content") or "{}")
        except Exception as e:
            # ★★ 失败必须留痕（2026-10-10）：原先 `except: return hits[:top_k]`
            #   把"provider 整体不可用"这类**系统性故障**完全静默，
            #   表现为"开关开了但顺序一字未变"，排查花了 4 轮。
            import logging
            logging.getLogger(__name__).warning(
                "llm_rerank 解析失败（provider_id=%s）: %s | content 前 80=%r",
                _pid or "(默认)", type(e).__name__, str(e)[:80],
                str(msg.get("content") or "")[:80])
            return hits[:top_k]
        scores = {int(x["i"]): float(x["score"]) for x in (m.get("ratings") or [])
                  if isinstance(x, dict) and str(x.get("i", "")).lstrip("-").isdigit()}
        if not scores:
            #★ 也要留痕（2026-10-10）："scores 为空"与"解析失败"是两种不同故障，
            #   但都表现为"顺序未变"，不打印就无法区分。
            import logging
            logging.getLogger(__name__).warning(
                "llm_rerank 未拿到评分（provider_id=%s）"
                "|ratings=%r|content=%r",
                _pid or "(默认)", (m.get("ratings") or [])[:3],
                str(msg.get("content") or "")[:80])
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
    except Exception as e:
        #★★ 最外层也留痕（2026-10-10 排查 6 轮才找到这里）：
        #   排序段之后的任何异常都被这一行静默吞掉，症状同样是"顺序未变"。
        import logging
        logging.getLogger(__name__).warning(
            "llm_rerank 排序段异常（provider_id=%s）: %s: %s",
            _pid or "(默认)", type(e).__name__, str(e)[:160])
        return hits[:top_k]
