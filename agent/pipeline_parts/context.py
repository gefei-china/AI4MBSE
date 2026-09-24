# -*- coding: utf-8 -*-
"""AgentPipeline Mixin：流程匹配、上下文组装与重排。

由 tools/split_pipeline.py 从 agent/pipeline.py 机械切分而成；⚠️ 切分脚本**已一次性执行完毕、不可重跑**—— 此后本文件按普通源码维护（方法体与其它模块一样可直接改）。"""
from .common import *


class ContextMixin:
    """流程匹配、上下文组装与重排。"""

    @staticmethod
    def _flow_tokens(text):
        """中文 2-4 字滑动窗口 + 英文单词分词（中文无空格，直接 split 无法切词）。

        下划线兼容：'SysML_V2' 同时产出 'sysml_v2' 与 'sysml'/'v2'（Glossary 归一化产物可匹配原生写法）。
        """
        text = re.sub(r"[@#]", "", text or "")
        toks = set()
        for w in re.findall(r"[a-zA-Z][a-zA-Z0-9_]{1,}", text.lower()):
            toks.add(w)
            for part in w.split("_"):
                if len(part) >= 2:
                    toks.add(part)
        cjk = re.sub(r"[^\u4e00-\u9fff]", "", text)
        stop = set("的了是在和与及或把被让对从向为以于就都也很而但并且如果因为所以这些那些我们您请帮我")
        n = len(cjk)
        for i in range(n):
            for L in (2, 3, 4):
                w = cjk[i:i + L]
                if len(w) == L and not all(ch in stop for ch in w):
                    toks.add(w)
        return toks

    def _match_flows(self, user_input, limit=3):
        """匹配已保存工作流（名称/描述/节点标签）——双通道语义升级版。

        P0-2 行业对齐：
        - Glossary 归一化：输入先过术语表（v2→SysML_V2），跨写法对齐
        - 词法通道：name×3 / desc×2 / label×1 分级加权（名称最能代表流程主题）；
          名称完整命中 +5；阈值 ≥2（排除单个公共词误命中）
        - 语义通道：SemanticSearch（真 embedding 优先，bigram 降级），阈值 0.15
        - RRF 融合（k=60）：两通道按排名合并，免调参（juejin RAG 三板斧）
        """
        try:
            conn = get_db()
            rows = conn.execute(
                "SELECT id, name, description, nodes FROM agent_flows ORDER BY id DESC LIMIT 200").fetchall()
            conn.close()
            if not rows:
                return []
            # 1) Glossary 归一化（v2→SysML_V2 等跨写法对齐）
            norm_input = user_input
            try:
                from glossary import GlossaryMatcher
                _c2 = get_db()
                try:
                    norm_input, _ = GlossaryMatcher(_c2).normalize(user_input)
                finally:
                    _c2.close()
            except Exception:
                pass
            q_tokens = self._flow_tokens(norm_input)
            if not q_tokens:
                return []

            # 2) 词法通道：分级加权（name 3 > desc 2 > label 1）
            lex_scored = []  # (score, row, matched)
            for r in rows:
                name = r["name"] or ""
                desc = r["description"] or ""
                try:
                    nodes = json.loads(r["nodes"] or "[]")
                except Exception:
                    nodes = []
                labels = " ".join((n.get("label") or "") for n in nodes if isinstance(n, dict))
                qn = q_tokens & self._flow_tokens(name)
                qd = q_tokens & self._flow_tokens(desc)
                ql = q_tokens & self._flow_tokens(labels)
                score = len(qn) * 3 + len(qd) * 2 + len(ql)
                if name and name in norm_input:
                    score += 5  # 名称完整命中加权
                if score > 0:
                    lex_scored.append((score, r, sorted(list(qn | qd | ql))[:6]))
            # 阈值 ≥2：排除单个公共词（如只命中 label 的"设计"）误命中
            lex_top = [(s, r, m) for s, r, m in lex_scored if s >= 2]
            lex_top.sort(key=lambda x: -x[0])

            # 3) 语义通道：SemanticSearch（真向量优先 / bigram 降级）——**仅精排，不独立召回**
            #    行业漏斗式：词法召回（粗筛）→ 语义排序（细排）。词法零命中的候选不进最终结果，
            #    避免「随便聊聊」这类无关节被 bigram 0.3 分拉进来。
            sem_scored = []
            try:
                from semantic import rank_items
                sem_items = []
                for r in rows:
                    name = r["name"] or ""
                    desc = r["description"] or ""
                    try:
                        nodes = json.loads(r["nodes"] or "[]")
                    except Exception:
                        nodes = []
                    labels = " ".join((n.get("label") or "") for n in nodes if isinstance(n, dict))
                    sem_items.append({"id": r["id"], "text": f"{name} {desc} {labels}"})
                _sc = rank_items(norm_input, sem_items, top_k=20, threshold=0.0, key="text", conn=None)
                sem_scored = [(s, it) for s, it in _sc]
            except Exception:
                sem_scored = []
            sem_by_id = {it["id"]: s for s, it in sem_scored}
            # 语义补召：词法全零命中时，若语义第一名显著才放行 top1。
            # 2026-09-19：门槛按**本次实际走的路**选（两路余弦量纲不同，实测同批 top1 dense 0.51 / bigram 0.10）——
            #   bigram → 0.5（原口径）；dense → semantic_fallback_gate_dense（0.79 = 0.5 的分位等价值，分位等价标定）。
            try:
                import semantic as _sem
                _is_dense = _sem.last_backend() == "dense"
            except Exception:
                _is_dense = False
            if _is_dense:
                from core import config as _cfg
                _gate = float(_cfg.get("context", "semantic_fallback_gate_dense", 0.79) or 0.79)
            else:
                _gate = 0.5
            semantic_fallback = []
            if not lex_top and sem_scored and sem_scored[0][0] >= _gate:
                top_s, top_it = sem_scored[0]
                semantic_fallback = [(top_s, by_id.get(top_it["id"]))]

            # 4) 精排融合：词法分为主序，语义分做同分/相近排序微调
            #    final = 词法分（主） + 语义分归一化（次，≤1.0 权重）；词法零命中仅 semantic_fallback 兜底
            ranked = []
            for s, r, m in lex_top:
                fid = r["id"]
                sem = sem_by_id.get(fid, 0.0)
                # bigram 语义分 0-1，词法分通常 ≥2；语义只做小数位微调，不压过词法主序
                ranked.append((s + sem * 0.5, r, m, s, sem))
            for s, r in semantic_fallback:
                if r is not None:
                    ranked.append((s * 0.5 + 0.01, r, [], 0, s))  # 兜底置后
            ranked.sort(key=lambda x: -x[0])

            # 5) 组装结果（保留原返回结构：id/name/score/nodes/desc/matched）
            out = []
            for final_score, r, m, lex_s, sem_s in ranked[:limit]:
                try:
                    nodes = json.loads(r["nodes"] or "[]")
                except Exception:
                    nodes = []
                out.append({"id": r["id"], "name": r["name"] or "",
                            "score": round(final_score, 3), "nodes": len(nodes),
                            "desc": (r["description"] or "")[:60], "matched": m,
                            "lex_score": lex_s, "sem_score": round(sem_s, 3)})
            return out
        except Exception:
            return []

    def _build_context(self, retrieval, query=None):
        parts = []
        # P1：知识库检索后 LLM 重排（context.rerank 开关，粗筛→细排；失败返回原顺序）
        if query:
            try:
                from core import config as _cfg
                if _cfg.as_bool("context", "rerank", False) and retrieval.get("chunk_hits"):
                    retrieval["chunk_hits"] = self._rerank_hits(query, retrieval["chunk_hits"])
            except Exception:
                pass
        # 上传文档命中片段置顶（用户上传资料 = 首要依据）
        attachment_hits = retrieval.get("attachment_hits") or []
        if attachment_hits:
            for h in attachment_hits[:5]:
                tag = "语义" if h.get("mode") == "semantic" else (",".join(h.get("matched", [])[:3]) or "语义")
                parts.append(f"[上传资料·命中 {tag}] {h.get('content', '')}")
        for e in retrieval["entities"][:5]:
            props = json.loads(e["properties"]) if e["properties"] else {}
            cat = e.get("knowledge_category") or ""
            tag = f"[图谱·{cat}]" if cat else "[图谱]"
            parts.append(f"{tag} {e['name']}({e['entity_type']}): {json.dumps(props, ensure_ascii=False)}")
        for r in retrieval["relations"][:5]:
            parts.append(f"[关系] {r.get('source_name','')} --{r['relation_type']}--> {r.get('target_name','')}")
        # P0-3 知识分类消费：设计方法知识=规范约束源（AI 必须遵守）/ 设计资产=增量设计源（参考复用）
        knowledge = retrieval.get("knowledge") or {}
        if knowledge.get("method"):
            parts.append("【设计方法知识 · 规范约束源（AI 必须遵守，建模输出不得违反）】"
                         + "、".join(e["name"] for e in knowledge["method"][:5]))
        if knowledge.get("assets"):
            parts.append("【设计资产 · 增量设计源（可参考复用，按需融入方案）】"
                         + "、".join(e["name"] for e in knowledge["assets"][:5]))
        # 缺口A：分块命中优先（真实文档片段），旧文档粗匹配兜底
        # 引用可解释性：编号【来源n】与前端 citations 数组 1 基对齐，LLM 按 [n] 标注引用
        # P1-4b（2026-09-21）：注入条数配置化（rag.inject_k，默认 3 = 改动前硬编码值）
        chunk_hits = retrieval.get("chunk_hits") or []
        try:
            from core import config as _cfg
            _inject_k = int(_cfg.get("rag", "inject_k", 3)) or 3
        except Exception:
            _inject_k = 3
        if chunk_hits:
            for i, c in enumerate(chunk_hits[:_inject_k], 1):
                parts.append(f"【来源{i}】{c.get('source_doc','')} §{c.get('chunk_index',0)}: {c.get('content','')}")
        else:
            for d in retrieval["vector_docs"][:_inject_k]:
                parts.append(f"[向量] {d.get('filename','')} ({d.get('parse_status','')})")
        # 建模独立性：检索命中有限时追加指引——代码元素完整性不依赖检索数量
        n_entities = len(retrieval.get("entities") or [])
        n_chunks = len(retrieval.get("chunk_hits") or [])
        if n_entities < 3 and n_chunks < 2:
            parts.append("（知识库检索命中有限，仅作参考。生成 SysML v2 建模代码时，"
                         "包/部件/需求/端口/连接等元素应基于用户需求与领域最佳实践完整生成，"
                         "检索实体不构成元素数量上限，不得因检索为空或命中少而缩减模型）")
        return "\n".join(parts) if parts else "无相关互联数据"

    def _rerank_hits(self, query, hits, top_n=None, keep=None):
        """P1：知识库检索后 LLM 重排（对齐 RAG 多阶段「粗筛→细排」）。

        对 top_n 候选让 LLM 挑选最相关 keep 条置前，其余保底置后；
        LLM 不可用/解析失败 → 返回原 hits（不影响主流程）。
        """
        from core import config as _cfg
        top_n = top_n or int(_cfg.get("context", "rerank_top_n", 8))
        keep = keep or int(_cfg.get("context", "rerank_keep", 3))
        cands = hits[:top_n]
        if len(cands) <= keep or not query:
            return hits
        try:
            from llm import llm_client
            items_txt = "\n".join(
                f"[{i}] {c.get('source_doc', '')} §{c.get('chunk_index', 0)}: {(c.get('content') or '')[:180]}"
                for i, c in enumerate(cands))
            resp = llm_client.chat([
                {"role": "system", "content": "你是检索重排器。根据用户问题，从候选中挑选最相关的 3 条。"
                                              "只输出 JSON 数组字符串，如 [0,3,5]；无合适条目输出 []；不要其他文字。"},
                {"role": "user", "content": f"问题：{str(query)[:300]}\n候选：\n{items_txt}"}],
                _intent="rerank")
            content = ((resp.get("choices") or [{}])[0].get("message", {}) or {}).get("content") or ""
            idxs = self._parse_json_block(content)
            if isinstance(idxs, list) and idxs:
                chosen = [cands[i] for i in idxs if isinstance(i, int) and 0 <= i < len(cands)]
                if chosen:
                    rest = [c for c in cands if c not in chosen]
                    return chosen + rest
        except Exception:
            pass
        return hits

    def _get_card_type(self, intent):
        mapping = {
            "impact": "card_impact",
            "review": "card_review",
            "requirement_analysis": "card_candidates",
        }
        return mapping.get(intent, "text")


# Global instance
