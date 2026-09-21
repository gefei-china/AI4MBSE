# -*- coding: utf-8 -*-
"""AgentPipeline Mixin：附件加载、历史检索、话题标注与上下文预算。

由 tools/split_pipeline.py 从 agent/pipeline.py 机械切分，勿手工编辑方法体。"""
from .common import *


def ctx_budget_tokens(kind: str, fallback_char_key: str, fallback: int) -> int:
    """P1-4b（2026-09-21）上下文预算取值：**占比制优先 → 绝对值 → 字符版兜底**。

    kind ∈ {'retrieval','history'}。占比键 `context.budget_{kind}_ratio` > 0 时，
    以「budget_window_tokens × 占比」为准（换模型自适应，依据调研 §4.3）；
    否则回落 T6 绝对值键 `budget_{kind}_tokens`（0 则再回落字符版键）。
    与既有「token 项填 0 回退字符版」的兼容模式同构。任何异常返回 fallback。
    """
    try:
        from core import config as _cfg
        ratio = float(_cfg.get("context", f"budget_{kind}_ratio", 0) or 0)
        if ratio > 0:
            win = int(_cfg.get("context", "budget_window_tokens", 65536) or 65536)
            return max(16, int(win * ratio))
        return (int(_cfg.get("context", f"budget_{kind}_tokens", 0))
                or int(_cfg.get("context", fallback_char_key, fallback)))
    except Exception:
        return fallback


class HistoryMixin:
    """附件加载、历史检索、话题标注与上下文预算。"""

    def _load_attachment_text(self, attachments):
        """返回 (parsed_blocks, parsed_count, skipped_names)。parsed_blocks 为 ["[文件名]\n内容…"] 列表。"""
        blocks, parsed, skipped = [], 0, []
        for a in attachments or []:
            url = (a.get("url") or "") if isinstance(a, dict) else ""
            fname = (a.get("filename") or url.split("/")[-1] or "附件") if isinstance(a, dict) else "附件"
            if not url.startswith("/static/"):
                continue
            rel = url.replace("/static/", "")
            path = os.path.join(STATIC_DIR, rel)
            if not os.path.exists(path):
                skipped.append(fname)
                continue
            ext = os.path.splitext(fname)[1].lower()
            text = ""
            try:
                if ext in (".txt", ".md", ".csv", ".json", ".xml", ".log", ".sysml", ".yaml", ".yml"):
                    raw = open(path, "rb").read()
                    for enc in ("utf-8", "gbk", "latin-1"):
                        try:
                            text = raw.decode(enc)
                            break
                        except Exception:
                            continue
                elif ext == ".pdf":
                    import pdfplumber
                    with pdfplumber.open(path) as pdf:
                        text = "\n".join((p.extract_text() or "") for p in pdf.pages[:20])
                elif ext == ".docx":
                    import docx
                    d = docx.Document(path)
                    text = "\n".join(p.text for p in d.paragraphs[:300])
                elif ext == ".xlsx":
                    import openpyxl
                    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
                    rows = []
                    for ws in wb.worksheets[:3]:
                        for row in ws.iter_rows(max_row=50, values_only=True):
                            rows.append(" | ".join(str(c) for c in row if c is not None))
                    text = "\n".join(rows)
            except Exception:
                skipped.append(fname)
                continue
            text = (text or "").strip()
            if not text:
                skipped.append(fname)
                continue
            # 全文按段分块（每块 ≤2500 字符），保证长文档后半部分同样参与检索与上下文注入
            for i, seg in enumerate(text[i:i + 2500] for i in range(0, len(text), 2500)):
                # 2026-09-17 S3：解析上限维持 40（曾试降到 8，但该值决定附件语义召回候选池 retrieval_att，
                #                 降它只省解析耗时、不省 token，故回退；token 侧改由 _ATT_INJECT_CAP 在注入点封顶）
                if len(blocks) >= 40:
                    break
                head = f"【{fname}】" if i == 0 else f"【{fname}#{i + 1}】"
                blocks.append(f"{head}\n{seg}")
            parsed += 1
        return blocks, parsed, skipped

    # ── 会话历史注入 v2：话题感知组装（P0+P1，替代纯时间窗口）──
    #   ① 当前话题原文段（最近 N 条）+ 上一话题切换边界（2 条）→ 原文逐字
    #   ② 语义拉回：与当前输入相关、未被原文注入的历史片段（top-k 原文，解决切回旧话题）
    #   ③ 其他话题：分话题独立摘要（scope=topic:{label}，消除混合摘要稀释，缓存复用）
    #   预算驱动：①占 50% → ②到 75% → ③剩余（不足则低优先级停，不送全量）
    #   话题打标失败/无话题 → 回退 _load_history_legacy（三层窗口）
    def _load_history(self, conversation_id, limit=None, cur_input=None):
        from core import config as _cfg
        msg_max = int(_cfg.get("context", "history_msg_max_chars", 1500))
        summary_chars = int(_cfg.get("context", "history_summary_chars", 800))
        cur_max = int(_cfg.get("context", "topic_current_max_msgs", 8))
        boundary = int(_cfg.get("context", "topic_boundary_keep", 2))
        pull_topk = int(_cfg.get("context", "topic_retrieve_topk", 6))
        pull_th = float(_cfg.get("context", "topic_retrieve_threshold", 0.18))
        budget = int(_cfg.get("context", "budget_history_chars", 3000))
        try:
            conn = get_db()
            rows = conn.execute(
                "SELECT id, role, content FROM messages WHERE conversation_id=? AND role IN ('user','assistant') "
                "ORDER BY id", (conversation_id,)).fetchall()
            topics = self._tag_topics(conn, conversation_id)
            conn.close()
        except Exception:
            return []
        if not rows:
            return []
        msgs = [{"id": r["id"], "role": r["role"], "content": (r["content"] or "")[:msg_max],
                 "topic": topics.get(r["id"], "")} for r in rows]
        if not any(m["topic"] for m in msgs):  # 打标失败 → 回退旧三层窗口
            return self._load_history_legacy(conversation_id, limit)
        # 按话题顺序分组（保持原顺序）
        groups = []
        for m in msgs:
            t = m["topic"]
            if groups and groups[-1]["topic"] == t:
                groups[-1]["msgs"].append(m)
            else:
                groups.append({"topic": t, "msgs": [m]})
        # 当前话题判定：当前输入未落库（execute 先回复后落库），不能用最后一组；
        # 用当前输入与各组代表向量相似度匹配（真 embedding 优先，dense 阈值独立量纲；
        # bigram 降级：≥0.12 取最佳组 → 切回旧话题时命中旧组；否则视为新话题取最后一组兜底）
        from collections import Counter
        from knowledge_engine import VectorEngine
        _ve = VectorEngine()
        cur_group = groups[-1]
        if cur_input:
            best_g, best_s = None, 0.0
            dense_th = float(_cfg.get("context", "topic_group_match_dense", 0.30))
            # dense：每组代表 = 段首 3 条消息（各截 200 字）拼接，与当前输入同批向量化
            reprs = [" ".join((m["content"] or "")[:200] for m in g["msgs"][:3]) for g in groups]
            scores, backend = self._semantic_scores(reprs, cur_input)
            if backend == "dense":
                for g, s in zip(groups, scores):
                    if s > best_s:
                        best_s, best_g = s, g
                if best_g is not None and best_s >= dense_th:
                    cur_group = best_g
            else:  # bigram 降级：段内全部消息 bigram 累加为段代表向量
                qv = _ve._vector(cur_input)
                for g in groups:
                    tv = Counter()
                    for m in g["msgs"]:
                        tv.update(_ve._vector(m["content"]))
                    s = _ve._cosine(qv, tv) if tv else 0.0
                    if s > best_s:
                        best_s, best_g = s, g
                if best_g is not None and best_s >= 0.12:
                    cur_group = best_g
        raw_list = []
        pull_blocks = []
        sum_blocks = []
        used = 0
        # T6：预算改 token 驱动（budget_history_tokens 优先，旧字符配置兜底）
        # P1-4b：改为占比制优先（context.budget_history_ratio > 0 时按窗口比例算）
        from core.token_counter import count_tokens as _ct
        budget_tok = ctx_budget_tokens("history", "budget_history_chars", 3000)
        raw_cap = int(budget_tok * 0.5)
        pull_cap = int(budget_tok * 0.75)

        def _clip(text, cap_tokens):
            """按 token 上限裁剪文本（字符比例逼近，中英混排近似）。"""
            if not text or _ct(text) <= cap_tokens:
                return text
            ratio = max(len(text) / max(_ct(text), 1), 0.5)
            cut = text[:int(max(cap_tokens, 16) * ratio)]
            for _ in range(2):
                if _ct(cut) <= cap_tokens:
                    return cut
                cut = cut[:int(len(cut) * cap_tokens / max(_ct(cut), 1))]
            return cut

        def push_raw(m):
            nonlocal used
            if used >= raw_cap or not m["content"]:
                return
            c = _clip(m["content"], max(raw_cap - used, 16))
            raw_list.append({"role": m["role"], "content": c, "id": m["id"]})
            used += _ct(c)

        # ① 当前话题原文（最近 cur_max 条，倒序填充至 50% 预算）
        for m in reversed(cur_group["msgs"][-cur_max:]):
            push_raw(m)
        # 切换边界：当前组很短（正在切换话题）时补其前一组最后 boundary 条原文，保证语义连续
        if len(cur_group["msgs"]) <= 2 and cur_group is not groups[0]:
            gi = groups.index(cur_group)
            for m in reversed(groups[gi - 1]["msgs"][-boundary:]):
                push_raw(m)
        raw_list.reverse()  # 还原时间顺序（最近的在最后）
        used_ids = {m["id"] for m in raw_list if m.get("id")}
        used_ids.update(m["id"] for m in cur_group["msgs"][-cur_max:])
        # ② 语义拉回（query=当前输入+当前话题，排除已注入原文；同话题域加权）
        query = f"{cur_input or ''} {cur_group['topic']}"
        rest = [m for m in msgs if m["id"] not in used_ids]
        pulled = self._search_history(rest, query, pull_topk, pull_th, cur_topic=cur_group["topic"])
        for p in pulled:
            if used >= pull_cap or not p["content"]:
                break
            c = _clip(p["content"], max(pull_cap - used, 16))
            pull_blocks.append({"role": "system", "content": f"【相关历史片段（相关度 {p['score']}）】\n{c}"})
            used += _ct(c) + 24
        # ③ 其他话题分话题摘要（剩余预算，低优先级不足则停；排除当前话题组避免重复）
        for g in groups:
            if g is cur_group:
                continue
            if used >= budget_tok:
                break
            seg = [m for m in g["msgs"] if m["id"] not in used_ids]
            if not seg:
                continue
            s = self._history_summary(conversation_id, f"topic:{g['topic'][:40]}", seg, summary_chars)
            if not s:
                continue
            c = _clip(s, max(0, budget_tok - used))
            if not c:
                break
            sum_blocks.append({"role": "system", "content": f"【话题：{g['topic']} 摘要】\n{c}"})
            used += _ct(c)
        # 输出按时间序：摘要（最老）→ 拉回（中间）→ 当前话题原文（最近，紧邻当前输入）
        return sum_blocks + pull_blocks + raw_list

    def _tag_topics(self, conn, conversation_id):
        """会话消息话题打标（惰性全量重算+回写 messages.topic，规则确定性）。
        相邻用户消息与「当前话题段代表向量」（段内全部消息 bigram 累加）余弦 < 阈值 → 话题切换
        （以承接词开头则继承当前话题）；助手消息继承前一条用户消息话题并并入段代表向量。
        返回 {message_id: topic}。
        """
        from collections import Counter
        from core import config as _cfg
        from knowledge_engine import VectorEngine
        threshold = float(_cfg.get("context", "topic_sim_threshold", 0.15))
        label_chars = int(_cfg.get("context", "topic_label_chars", 14))
        rows = conn.execute(
            "SELECT id, role, content, topic FROM messages WHERE conversation_id=? AND role IN ('user','assistant') "
            "ORDER BY id", (conversation_id,)).fetchall()
        if not rows:
            return {}
        mapping = {r["id"]: (r["topic"] or "") for r in rows}
        # 无未打标消息 → 直接复用
        if all(t for t in mapping.values()):
            return mapping
        ve = VectorEngine()
        # 延续词：承接式追问继承当前话题；回指词（刚才/前面/之前）不在此列，
        # 其与段代表向量相似度低时自然开新话题 → 语义拉回负责把回指的历史原文拉回
        carry = ("继续", "接着", "还有", "另外", "再说", "那", "再")
        cur_topic = ""
        topic_vec = None  # 段代表向量（段内消息 bigram 累加，随对话增长）
        last_user_id = None
        for r in rows:
            content = (r["content"] or "").strip()
            if r["role"] == "user":
                vec = ve._vector(content) if content else None
                if topic_vec is None:
                    cur_topic = content[:label_chars] or "话题1"
                    topic_vec = Counter(vec) if vec else Counter()
                elif vec is not None:
                    sim = ve._cosine(vec, topic_vec)
                    # 切换需「相似度低 + 无共同 bigram 词」双重条件：短承接句（仅 1 个共享业务词）
                    # 相似度天然低但语义承接（如"链路余量校核"承接"链路预算"），默认偏不切，
                    # 宁多合并（靠语义拉回兜底）也不误切
                    shared = bool(set(vec.keys()) & set(topic_vec.keys()))
                    if sim < threshold and not shared and not content.startswith(carry):
                        cur_topic = content[:label_chars] or cur_topic
                        topic_vec = Counter(vec)  # 新话题段
                    else:
                        topic_vec.update(vec)  # 承接：并入段代表
                last_user_id = r["id"]
            else:  # assistant 继承前一条用户消息话题，并入段代表
                cur_topic = mapping.get(last_user_id, cur_topic) if last_user_id is not None else cur_topic
                if topic_vec is not None:
                    topic_vec.update(ve._vector(content))
            mapping[r["id"]] = cur_topic
            conn.execute("UPDATE messages SET topic=? WHERE id=?", (cur_topic, r["id"]))
        conn.commit()
        return mapping

    def _semantic_scores(self, texts, query):
        """统一语义出口：真 embedding（texts+query 同批向量化保维度一致）→ bigram 降级。

        返回 (scores, backend)：backend='dense' 时 scores 与 texts 对齐（embedding 余弦 0~1）；
        否则 (None, 'bigram')，调用方走 VectorEngine bigram 打分。
        embedding.enabled=False（Mock/离线确定性）强制 bigram；Embedder 自带 probe/query 缓存。
        """
        try:
            from core import config as _cfg_s
            if not _cfg_s.as_bool("embedding", "enabled", True):
                return None, "bigram"
            if not query or not texts:
                return None, "bigram"
            from knowledge_pipeline import Embedder
            from database import get_db
            conn = get_db()
            try:
                ed = Embedder(conn)
            finally:
                conn.close()
            all_texts = list(texts) + [query]
            vecs, version = ed.embed_with_version(all_texts, batch_size=0)
            if version != "openai-compat" or not vecs or len(vecs) != len(all_texts):
                return None, "bigram"
            import math as _math
            qv = vecs[-1]
            qn = _math.sqrt(sum(x * x for x in qv)) or 1.0
            scores = []
            for v in vecs[:-1]:
                if not v or len(v) != len(qv):
                    scores.append(0.0)
                    continue
                vn = _math.sqrt(sum(x * x for x in v)) or 1.0
                scores.append(sum(a * b for a, b in zip(qv, v)) / (qn * vn))
            return scores, "dense"
        except Exception:
            return None, "bigram"

    def _search_history(self, msgs, query, topk=6, threshold=0.15, cur_topic=""):
        """会话内语义拉回：真 embedding 优先（同批向量化余弦），bigram 降级。

        dense 路：embedding 余弦独立量纲，阈值用 topic_retrieve_threshold_dense（默认 0.35）；
        话题域加权不再需要（稠密向量对措辞差异鲁棒，query 已拼当前话题文本）。
        bigram 路：余弦打分 + 话题域加权（候选 topic 与当前话题 ts≥0.2 加 0.25*ts，
        补偿细节词在长 query 中被稀释）；候选截最近 120 条控 embedding 成本。
        """
        from core import config as _cfg
        from core import config as _cfg_s
        cands = [(m, (m.get("content") or "")[:400]) for m in msgs if (m.get("content") or "")[:400]]
        if not query or not cands:
            return []
        cands = cands[-120:]  # 成本护栏：拉回只需 top-k，候选过多按时间取最近
        dense_th = float(_cfg_s.get("context", "topic_retrieve_threshold_dense", 0.35))
        scores, backend = self._semantic_scores([c for _, c in cands], query)
        if backend == "dense":
            scored = [(s, m) for (m, _), s in zip(cands, scores) if s >= dense_th]
        else:
            from knowledge_engine import VectorEngine
            ve = VectorEngine()
            qv = ve._vector(query or "")
            if not qv:
                return []
            tqv = ve._vector(cur_topic) if cur_topic else None
            scored = []
            for m, c in cands:
                s = ve._cosine(qv, ve._vector(c))
                if tqv:
                    mt = ve._vector(m.get("topic") or "")
                    ts = ve._cosine(tqv, mt) if mt else 0.0
                    if ts >= 0.2:
                        s += 0.25 * ts  # 同话题域加权（上限约 +0.25）
                if s >= threshold:
                    scored.append((s, m))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [{"id": m["id"], "role": m["role"], "content": m["content"][:300], "score": round(s, 3)}
                for s, m in scored[:topk]]

    # ── 会话历史注入 v1（旧三层窗口，话题打标失败时兜底）──
    def _load_history_legacy(self, conversation_id, limit=None):
        from core import config as _cfg
        limit = limit or int(_cfg.get("context", "history_immediate_turns", 6))
        mid_turns = int(_cfg.get("context", "history_mid_turns", 20))
        msg_max = int(_cfg.get("context", "history_msg_max_chars", 1500))
        summary_chars = int(_cfg.get("context", "history_summary_chars", 800))
        try:
            conn = get_db()
            rows = conn.execute(
                "SELECT id, role, content FROM messages WHERE conversation_id=? AND role IN ('user','assistant') "
                "ORDER BY id", (conversation_id,)).fetchall()
            conn.close()
        except Exception:
            return []
        if not rows:
            return []
        msgs = []
        for r in rows:
            c = (r["content"] or "")[:msg_max]
            msgs.append({"id": r["id"], "role": r["role"], "content": c})
        immediate = msgs[-(limit * 2):] if limit else msgs
        older = msgs[:-(limit * 2)] if (limit and len(msgs) > limit * 2) else []
        out = []
        if older:
            mid_seg = older[-(mid_turns * 2):]
            hist_seg = older[:-(mid_turns * 2)] if len(older) > mid_turns * 2 else []
            mid_sum = self._history_summary(conversation_id, "mid", mid_seg, summary_chars)
            if mid_sum:
                out.append({"role": "system", "content": f"【更早对话摘要】\n{mid_sum}"})
            if hist_seg:
                hist_sum = self._history_summary(conversation_id, "hist", hist_seg, summary_chars)
                if hist_sum:
                    out.append({"role": "system", "content": f"【历史对话概览】\n{hist_sum}"})
        for m in immediate:
            out.append({"role": m["role"], "content": m["content"]})
        return out

    def _history_summary(self, conversation_id, scope, seg, max_chars=800):
        """生成/复用某段历史的摘要（缓存锚点 = 段内最大消息 id，段变化自动重算）。"""
        if not seg:
            return ""
        anchor = seg[-1]["id"]
        try:
            conn = get_db()
            row = conn.execute(
                "SELECT summary FROM conversation_summaries WHERE conversation_id=? AND scope=? AND anchor_id=?",
                (conversation_id, scope, anchor)).fetchone()
            if row and row["summary"]:
                return row["summary"][:max_chars]
            text = "\n".join(f"{'用户' if m['role'] == 'user' else '助手'}: {m['content'][:300]}" for m in seg)
            summary = self._llm_summarize(text, scope)
            conn.execute(
                "INSERT OR REPLACE INTO conversation_summaries (conversation_id, scope, anchor_id, summary) "
                "VALUES (?,?,?,?)",
                (conversation_id, scope, anchor, summary))
            conn.commit()
            conn.close()
            return summary[:max_chars]
        except Exception:
            return ""

    def _llm_summarize(self, text, scope):
        """LLM 摘要（保留决策/实体/待办，丢弃客套）；Mock/失败 → 规则降级（首句拼接）。"""
        try:
            from llm import llm_client
            prompt = ("将以下对话历史压缩为简洁摘要：保留关键决策、实体、约束与待办；"
                      "丢弃客套与重复内容。输出 200 字以内。\n\n对话历史：\n" + text[:4000])
            resp = llm_client.chat([{"role": "user", "content": prompt}], _intent="history_summary")
            s = ((resp.get("choices") or [{}])[0].get("message", {}) or {}).get("content") or ""
            s = s.strip()
            if len(s) > 30:
                return s
        except Exception:
            pass
        parts = []
        for line in text.split("\n"):
            if line.startswith(("用户:", "助手:")) and line[3:].strip():
                parts.append(line[:80])
        return "；".join(parts[:10]) or ""

    @staticmethod
    def _truncate_budget(text: str, max_chars: int, keep_head: bool = True) -> str:
        """P1a-2 Token 预算裁剪（字符版兜底）：超限时保留头部（检索数据相关性在头部，对齐 Lost-in-the-Middle 经验）。

        粗估：1 中文字 ≈ 1.5 token，字符上限 ≈ token 预算 × 2/3。
        """
        if not text or max_chars <= 0 or len(text) <= max_chars:
            return text
        if keep_head:
            return text[:max_chars] + "\n…（超预算已裁剪）"
        return "…（超预算已裁剪）\n" + text[-max_chars:]

    @staticmethod
    def _truncate_tokens(text: str, max_tokens: int, keep_head: bool = True, model_hint: str | None = None) -> str:
        """T6 Token 驱动裁剪：按 token 估算超限后以字符近似比例截断并复测（最多 2 次迭代，防中英混排误差）。

        返回裁剪后文本；超限时附裁剪标记。未超限原样返回。
        """
        from core.token_counter import count_tokens
        if not text or max_tokens <= 0:
            return text
        n = count_tokens(text, model_hint)
        if n <= max_tokens:
            return text
        # 裁剪标记自身也占预算：预留标记 token 开销，避免"裁到上限+标记"最终超限
        marker = "\n…（超预算已裁剪）" if keep_head else "…（超预算已裁剪）\n"
        budget = max_tokens - count_tokens(marker, model_hint)
        if budget < 16:
            budget = max_tokens  # 预算过小时退回原值（标记开销可忽略）
        # 粗比例：字符数 / token 数 → 目标字符数；逐轮逼近（token 数非严格线性）
        ratio = max(len(text) / max(n, 1), 0.5)
        target_chars = int(budget * ratio)
        for _ in range(2):
            cut = text[:target_chars] if keep_head else text[-target_chars:]
            cn = count_tokens(cut, model_hint)
            if cn <= budget:
                return (cut + marker) if keep_head else (marker + cut)
            target_chars = int(target_chars * budget / max(cn, 1))
        cut = text[:target_chars] if keep_head else text[-target_chars:]
        return (cut + marker) if keep_head else (marker + cut)

    def _apply_context_budget(self, system_prompt: str, retrieval_text: str, history_len: int) -> str:
        """P1a-2/T6 上下文预算分区：检索区与历史区分别裁剪，防超窗（对齐 Levelop Context Spec）。

        T6 升级：以 token 预算为准（budget_retrieval_tokens / budget_history_tokens），
        旧字符配置（budget_retrieval_chars 等）保留为估算回退路径兜底。
        P1-4b：占比制优先（context.budget_{retrieval,history}_ratio > 0 时按窗口比例算）。
        返回裁剪后的 system_prompt。检索区保留头部（最相关），历史区保留尾部（最近）。
        """
        try:
            from core import config as _cfg
            from core.token_counter import count_tokens
            retr_tok = ctx_budget_tokens("retrieval", "budget_retrieval_chars", 4000)
            hist_tok = ctx_budget_tokens("history", "budget_history_chars", 3000)
            if retrieval_text:
                system_prompt = system_prompt.replace(
                    retrieval_text,
                    self._truncate_tokens(retrieval_text, retr_tok, keep_head=True),
                    1)
            # 历史区：system_prompt 中「更早对话摘要/历史对话概览」两块截断（token 驱动）
            def _cut_hist(m, half):
                seg = m.group(2)
                if count_tokens(seg) <= (hist_tok // 2 if half else hist_tok):
                    return m.group(1) + seg
                return m.group(1) + self._truncate_tokens(seg, hist_tok // 2 if half else hist_tok, keep_head=True)
            system_prompt = re.sub(
                r"(【更早对话摘要】\n)(.{1,8000}?)(?=\n【历史对话概览】|$)",
                lambda m: _cut_hist(m, True), system_prompt, flags=re.S)
            system_prompt = re.sub(
                r"(【历史对话概览】\n)(.{1,8000}?)(?=\n【|$)",
                lambda m: _cut_hist(m, False), system_prompt, flags=re.S)
            return system_prompt
        except Exception:
            return system_prompt

    # ── 工作流匹配：词法 + 语义双通道 + RRF 融合（行业对齐：Voiceflow 混合检索 / juejin RRF / 百度漏斗式）──
