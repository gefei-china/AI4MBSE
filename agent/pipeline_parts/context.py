# -*- coding: utf-8 -*-
"""AgentPipeline Mixin：流程匹配、上下文组装与重排。

由 tools/split_pipeline.py 从 agent/pipeline.py 机械切分而成；⚠️ 切分脚本**已一次性执行完毕、不可重跑**—— 此后本文件按普通源码维护（方法体与其它模块一样可直接改）。"""
from .common import *

# ★ 2026-11-15：`source_anchor` / `_clause_re` / `_CLAUSE_SHAPE` / `_short` 已
#   **上移到 `agent/utils.py`**（单一真源）—— 引用登记簿（也在 utils）要用同一套
#   锚点算式与同一份截断规则；留在本文件会变成"工具路径与注入路径各写一份"。


def _doc_short(name: str, cap: int = 28) -> str:
    """文档短名（去扩展名 + 词边界截断）—— 编号对照表要短，否则比正文还长。"""
    s = re.sub(r"\.(pdf|docx?|pptx?|xlsx?|md|txt)$", "", str(name or "").strip(), flags=re.I)
    return _short(s, cap)


class ContextMixin:
    """流程匹配、上下文组装与重排。"""

    # ★ 2026-11-05：`_flow_tokens` / `_match_flows` 随画布工作流一并移除。
    #   它们只服务"匹配已保存的画布流程"，而画布已明确舍弃、前端 0 处消费
    #   （`matched_flows` 字段前端从未读取）。调用方保留**字段形状**（恒返回 []），
    #   以免既有 API 消费者因缺键而报错。
    @staticmethod
    def _render_ontology_context(oc: dict, limit: int = 900) -> str:
        """把 get_context() 的结构化结果渲染成模型可读文本（2026-11-24 A1）。

        纪律：只陈述查到的对象/关系/约束，不推断；类型未注册显式标注
        （防模型把未注册类型当合法语义）；硬上限限长，防大邻域打爆 prompt。
        """
        obj = oc.get("object") or {}
        lines = [f"【本体上下文 · 对象 {obj.get('name','')}（{obj.get('entity_type','')}）】"]
        attrs = oc.get("attributes") or {}
        if attrs:
            lines.append("属性：" + json.dumps(attrs, ensure_ascii=False))
        rels = oc.get("relations") or []
        if rels:
            seg = "；".join(
                f"{r.get('direction','')} {r.get('relation_type','')}→{r.get('other_name','')}"
                f"[{r.get('other_type','') or '未标类型'}{'，本体未注册' if not r.get('other_registered') else ''}]"
                for r in rels[:8])
            lines.append(f"关系（{len(rels)} 条，列前 8）：" + seg)
        tc = oc.get("type_constraints") or {}
        if tc.get("registered"):
            cons = tc.get("constraints") or {}
            if cons:
                lines.append("类型约束：" + json.dumps(cons, ensure_ascii=False))
            if tc.get("parent"):
                lines.append("父类：" + str(tc["parent"]))
        else:
            lines.append(f"⚠ 类型「{tc.get('name','')}」未在本体注册 —— 不得据此推断其语义")
        tris = oc.get("triples") or []
        if tris:
            seg = "；".join(f"{t.get('subject','')} {t.get('predicate','')} {t.get('object','')}" for t in tris[:5])
            lines.append(f"已确认事实（{len(tris)} 条，列前 5）：" + seg)
        lines.append("以上为本体查询所得事实；未列出不代表不存在，不得编造未给出的关系。")
        out = "\n".join(lines)
        return out if len(out) <= limit else out[:limit - 20] + "\n…（已截断）"

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
            # 2026-11-24 A1：把 Ontology Query Service 的对象级上下文真正送进 prompt。
            # 此前 _entity_link 挂在 _ontology_context 上却全仓无读取 —— 「取到但没用上」。
            # 一处改动同时覆盖流式(stream.py)与非流式(execute.py)两条路径。
            _oc = e.get("_ontology_context")
            if _oc and _oc.get("found"):
                try:
                    parts.append(self._render_ontology_context(_oc))
                except Exception:
                    pass  # 渲染失败不拖垮主检索链（本体上下文是增强，不是必需）
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
            # ★ 2026-11-01（P0-2）文档级背景：命中已出现的文档若有 LLM 摘要就注入一段。
            #   为什么需要：chunk 命中给的是**局部条文**，模型常缺全局语境而误判
            #   （实测案例：把"技术要求书 7.4 节"当成全部，忽略 11.1 的相关约束）。
            #   为什么放【来源n】之前：先建立"这是哪份文档、讲什么"的框架，再看具体条文，
            #   与人的阅读顺序一致；放后面会让模型先入为主地只认片段。
            #   为什么只给命中文档：避免把无关文档的摘要当噪声塞进来。
            _ds = retrieval.get("doc_summaries") or []
            if _ds:
                _lines = []
                for _d in _ds[:3]:
                    _t = str((_d or {}).get("summary") or "").strip()
                    if _t:
                        _lines.append("《%s》：%s" % (str((_d or {}).get("source_doc") or "")[:40],
                                                    _t[:220]))
                if _lines:
                    parts.append("【文档背景 · 命中文档的概要（用于建立语境，不是引用依据）】\n"
                                 + "\n".join(_lines))
            # ★ 2026-11-01（P0-1 降级告知）：检索降级时必须**明确告诉模型"这批分数不可信"**。
            #   为什么必要：embedding 服务故障时向量分系统性塌陷（实测 0.07~0.29），
            #   命中仍可能是对的（字面匹配），但**排序与置信不再可靠**。
            #   不告知 ⇒ 模型会把"分数低"当成"内容不相关"，从而拒用正确材料、或反过来
            #   把噪声当权威。两种情况都是"看起来正常、实则错"的失效。
            #   只加一行提示，不减少任何注入内容（宁多给、不误删）。
            _lc = retrieval.get("low_conf") or {}
            if _lc.get("degraded"):
                parts.append(
                    "【检索状态：降级】本次向量检索降级为字面匹配（embedding 服务不可用），"
                    "以下片段的**相似度分数与排序不可靠**，请以内容本身是否切题为准，"
                    "并在回答中说明「本次检索为降级模式」。")
            elif _lc.get("dropped"):
                parts.append(
                    "【检索状态：已过滤低相关】本次共命中 %s 条，已过滤 %s 条低于相关度门槛（%s）的结果，"
                    "以下为保留的 %s 条。" % (_lc.get("nominal"), _lc.get("dropped"),
                                            _lc.get("floor"), _lc.get("kept")))
            # ── P0-3（2026-10-26）：检索注入**多样性约束** ──
            # 问题（实测，生产快照 6 个 query）：3 个注入槽实际只换来 1.14~2.48 条信息。
            #   根因：语料 7177 chunk 只来自 14 个文档（top2 占 64.3%），
            #   且相邻 chunk 字符集 Jaccard 中位数 0.659 / p90 0.816
            #   （切片 overlap=90 所致）⇒ top3 常落在同一文档的连续 §，语义高度重复。
            # 做法（**无损去重**，对齐 Deep Agents「先无损、后有损」）：
            #   ① per-doc 配额（同一文档最多占 N 槽）② 同文档近邻 chunk 合并（保留 §a-b 坐标）
            #   ③ 槽位不足时从 RRF 池补位（不因约束导致信息变少）
            # ⚠️ 编号语义不变：合并后条目仍占**一个**【来源n】位，编号顺序 = 注入顺序，
            #    与前端 citations 数组 1 基对齐（context.py:187 硬约束）。
            _sel = list(chunk_hits)
            _div_stats = {}
            try:
                from core import config as _cfg2
                _per_doc = int(_cfg2.get("context_audit", "retrieval_per_doc_max", 2) or 0)
                _merge = bool(_cfg2.get("context_audit", "retrieval_merge_neighbor", True))
                if _per_doc > 0 or _merge:
                    # ★ 2026-11-01：改用**共用选择器** `select_for_injection`，
                    #   与 `agent/utils._citations_payload`（界面引用数据）同源。
                    #   此前两边各调一次 `select_diverse`，虽然参数相同，但一旦
                    #   某一边单独改参数就会让【来源n】与 citations[n] 指向不同条目
                    #   （实测已发生：位次 3/4/5 全错位）。共用函数 ⇒ 结构上不会错位。
                    from agent.retrieval_diversity import select_for_injection
                    _sel, _div_stats = select_for_injection(
                        chunk_hits, inject_k=_inject_k, per_doc_max=_per_doc,
                        merge_neighbor=_merge)
            except Exception:
                _sel = list(chunk_hits[:_inject_k])
                _div_stats = {}
            _anchor_rows = []
            # ★ 2026-11-15：**编号改由引用登记簿分配**（不再自己 enumerate）。
            #   为什么必须改：原先【来源n】是"注入顺序号"，而 citations 数组也按
            #   "注入顺序"生成 —— 两边各自数，一旦某一边多算/少算就整体错位
            #   （本仓已发生过：位次 3/4/5 全错位）。现在**两边都读登记簿** ⇒ 结构上不可能错位。
            #   同时登记簿让**工具检索**的命中也能拿到编号（见 tools.py 的 graph_retrieve），
            #   —— 这才是 Skill 硬门槛要求的主路径（真机 conv 999999033：走工具路径时
            #      citations 为空、正文 5 个 [n] 全部越界、点不动）。
            _reg = getattr(self, "_citation_registry", None)
            if _reg is None:
                _reg = []
                self._citation_registry = _reg
            _entries = register_citations(_reg, _sel[:_inject_k])
            for _e in _entries:
                c = _e.get("chunk") or {}
                _doc = _e.get("doc") or c.get("source_doc", "")
                # 锚点由登记簿在**登记时**算好（单一真源）；此处只做兜底，不再各算一份。
                _anchor = _e.get("anchor") or source_anchor(c)
                _mf = c.get("merged_from")
                _tag = _anchor + (f"（合并 {len(_mf)} 段）" if _mf and len(_mf) > 1 else "")
                parts.append(f"【来源{_e['n']}】{_doc} {_tag}: {c.get('content','')}")
                _anchor_rows.append(f"[{_e['n']}] {_doc_short(_doc)} {_anchor}")
            # ★ 2026-11-14：**可照抄的编号对照表**（本轮关键改动）。
            #   为什么必须给：模型真正要引的是条款号（§5.2.7），而它从正文里就能读到；
            #   但"这条条款属于第几个来源"需要额外一次映射推理 —— 长文生成时它直接放弃。
            #   把映射**预先算好摆出来**，写 `§5.2.7 [2]` 就变成"抄"而不是"想"。
            if _anchor_rows:
                parts.append("【可引用编号对照（正文标 [n] 就按这张表）】\n"
                             + "\n".join(_anchor_rows))
            # ★ 2026-11-02：**显式给出可引用编号范围**。
            #   实测缺陷（会话 9986）：注入 5 条【来源1-5】，模型却在正文写了 [6][7][8] ——
            #   这三条在 citations 里不存在，用户点它们没有任何反应（"死编号"）。
            #   根因是此前只隐含约定"编号与 citations 对齐"，**没有告诉模型范围**；
            #   模型在多段长回答里会自行续编号。
            #   为什么用提示而不是后处理删标记：删标记会改动模型原文（且代码块里的 [n] 难区分），
            #   而"告诉它范围"是从源头减少发生，两者代价不同 —— 先用零副作用的那种。
            _n_src = min(len(_sel), _inject_k)
            if _n_src > 0:
                parts.append(
                    "（引用规则：正文中的 [n] **只能取 1–%d**，分别对应上面的【来源n】；"
                    "**不得写超出该范围的编号**，也不要为未列出的材料编号。"
                    "若要用的证据不在这 %d 条里，请直说「现有材料未覆盖」。"
                    # ★ 2026-11-14：条款号与来源序号**必须成对出现**。
                    #   实测模型只写 §5.2.7（41~63 次/条）而 `[n]` 为 0 —— 因为它把
                    #   "引标准条款"当成常识引用，没意识到还要给出处序号。
                    #   这里把要求变成**可直接照抄的样式**（§x.y [n]），代价接近 0。
                    "**若正文写出了条款号（如 §5.2.7 / §4.1.22），必须在条款号后紧跟"
                    "该条款所在片段的来源序号，写成 `§5.2.7 [2]` 这种样式**；"
                    "只写条款号不写序号，用户点不到证据。）"
                    % (_n_src, _n_src))
            # 归因：把"名义条数 vs 有效条数"喂给 context_audit（可观测）
            if _div_stats:
                try:
                    from agent.context_audit import record_blocks
                    record_blocks({}, phase="retrieval", extra={
                        "retrieval_nominal": _div_stats.get("nominal", 0),
                        "retrieval_selected": _div_stats.get("selected", 0),
                        "retrieval_effective": _div_stats.get("effective", 0),
                        "retrieval_merged": _div_stats.get("merged", 0),
                        "retrieval_docs": _div_stats.get("docs", 0),
                    })
                except Exception:
                    pass
        else:
            for d in retrieval["vector_docs"][:_inject_k]:
                parts.append(f"[向量] {d.get('filename','')} ({d.get('parse_status','')})")
        # P0（2026-09-29）记忆召回消费：已沉淀的经验/决策（跨会话结论）。
        # ⚠️ 刻意**不编号**（不占用【来源n】）——那些编号与前端 citations 一一对齐，
        #    是「可引用的原文证据」；记忆是 LLM 提炼产物，编进去会让模型把记忆当文档引用
        #    （用户需求里「幻觉写进知识库被引用」的正是此坑）。故独立成块 + 显式禁止作事实来源。
        memory_hits = retrieval.get("memory_hits") or []
        if memory_hits:
            # ★ P0-4（2026-10-26）：统一截断口径。
            #   改动前本处**完全不截断**（直接取 content），而 memory.py 侧截 160 字 ——
            #   同一记忆在两个块里长度不同（长版 + 短版），短版被长版完全覆盖 ⇒ 冗余。
            #   现统一为 `context_audit.memory_max_chars`（默认 200，与 recall_memories 一致）。
            try:
                from core import config as _cfg3
                _mem_cap = int(_cfg3.get("context_audit", "memory_max_chars", 200) or 200)
                _mem_block = str(_cfg3.get("context_audit", "memory_context_block", "digest")
                                 or "digest").strip().lower()
            except Exception:
                _mem_cap = 200
                _mem_block = "digest"
            # ★ P1-5（2026-11-27）：**正文只注入一次**。
            #   实测：`memory_single_source=True` 之后，本块与 `_build_memory_hint` 的
            #   【长期记忆】块消费的是**同一个 `memory_hits` 列表** ⇒ 同一批记忆在一份
            #   system prompt 里出现两次（各截 200 字），纯重复。按 275 条库、每轮 4 条命中算，
            #   重复段约 400~800 字 ≈ 3~6% 的 prompt，全是白付的钱与"两处口径不一致"的风险。
            #   ⇒ 默认改为 digest：本块只报**条数 + 主题**（导航用，不占正文预算），
            #     正文由长期记忆块承载；`memory_context_block="full"` 可逐字回退旧行为。
            if _mem_block == "full":
                _ml = [f"· [{h.get('mem_type','')}] {str(h.get('content') or '')[:_mem_cap]}"
                       for h in memory_hits[:4]]
                parts.append(
                    "【已沉淀记忆（跨会话经验/决策，**仅供对齐参考，不得作为事实依据引用**，"
                    "与本次任务无关时必须忽略；不可据此编造需求或参数）】\n" + "\n".join(_ml))
            else:
                _topics = []
                for h in memory_hits[:4]:
                    _t = str(((h.get("meta") or {}).get("mem_topic")) or "").strip()
                    if _t and _t not in _topics:
                        _topics.append(_t)
                _nav = ("｜主题：" + "、".join(x[:12] for x in _topics[:4])) if _topics else ""
                parts.append(
                    "【已沉淀记忆 %d 条%s（仅供对齐参考，**不得作为事实依据引用**，"
                    "与本次任务无关时必须忽略；正文不在此重复）】" % (len(memory_hits), _nav))
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
