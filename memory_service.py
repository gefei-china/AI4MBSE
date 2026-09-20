"""M1 通用记忆服务：向量检索 + 摘要沉淀 + 会话推动（多 Agent 共享）。

相对旧实现（_exec_agent 取最近 5 条）的增强：
- search：bigram 向量相似度 + 时间衰减，按「与当前任务相关性」检索而非仅按时间序。
- deposit：主动写入（带 mem_type/source 溯源）。
- maybe_deposit：LLM 评估「产出是否值得沉淀」→ 提炼为结构化记忆；
  Mock/无 key 环境自动降级规则阈值，保持确定性可回归。
- push：会话级推动——用户输入+Agent 产出中提炼经验（仅真实 LLM 可用时生效）。

与 Hermes MEMORY 机制对齐的最小实现（零外部依赖，SQLite + VectorEngine）。
"""
import json
import math
import re
import time

from knowledge_engine import VectorEngine


class MemoryService:
    """Agent 长期记忆：写（deposit）+ 读（search）+ 评估沉淀（maybe_deposit）+ 推动（push）。

    P0 语义升级：deposit 写真 embedding（agent_memory.embedding + embed_version 列，
    复用 knowledge_pipeline.Embedder 可插拔真向量）；search 真向量优先 + 时间衰减，
    无向量/版本不匹配/零分时 bigram Counter 余弦兜底（保持离线确定性）。
    """

    MEM_TYPES = ("fact", "preference", "experience", "skill")  # 事实 / 偏好 / 经验 / 技能

    # ── 向量化助手（真 API 失败 → 返回 None，调用方走 bigram 兜底）──
    @staticmethod
    def _embed(conn, text: str):
        """返回 (vector_list|None, version)。version 为空表示无真向量。"""
        try:
            from knowledge_pipeline import Embedder
            vecs, version = Embedder(conn).embed_with_version([text], batch_size=0)
            if version != "bigram-tf" and vecs and vecs[0]:
                return vecs[0], version
        except Exception:
            pass
        return None, ""

    # ── 读：语义检索 ──
    @staticmethod
    def search(conn, agent_id: str, query: str = "", top_k: int = 5,
               mem_type: str = "", max_age_days: int = 90, mem_topic: str = "") -> list:
        """按语义相关度检索记忆（真向量优先 + 时间衰减；bigram 兜底）。

        mem_topic 非空 → 先按主题标签精确过滤（Auto Memory 索引，避免平铺误拉），再语义排序。
        query 为空 → 退化为最近 top_k 条（兼容旧行为）。
        返回 [{content, mem_type, score, created_at}]。
        """
        try:
            if not query:
                sql = "SELECT * FROM agent_memory WHERE agent_id=? AND forgotten=0"
                params: list = [agent_id]
                if mem_topic:
                    sql += " AND mem_topic=?"
                    params.append(mem_topic)
                sql += " ORDER BY id DESC LIMIT ?"
                params.append(top_k)
                rows = conn.execute(sql, params).fetchall()
                return [dict(r) for r in rows]
            sql = "SELECT * FROM agent_memory WHERE agent_id=? AND mem_type!='session' AND forgotten=0"
            params = [agent_id]
            if mem_type:
                sql += " AND mem_type=?"
                params.append(mem_type)
            if mem_topic:
                sql += " AND mem_topic=?"
                params.append(mem_topic)
            rows = conn.execute(sql, params).fetchall()
            if not rows:
                return []
            qvec, qver = MemoryService._embed(conn, query)
            scored = []
            now = time.time()
            for _row in rows:
                r = dict(_row)  # sqlite3.Row 无 .get，先转 dict
                text = (r.get("content") or "")[:400]
                if not text:
                    continue
                s = 0.0
                # 真向量路：库中同版本向量 + 查询向量
                if qvec and r.get("embed_version") == qver:
                    try:
                        sv = json.loads(r.get("embedding") or "[]") if isinstance(r.get("embedding"), str) else (r.get("embedding") or [])
                        if sv and len(sv) == len(qvec):
                            qn = math.sqrt(sum(x * x for x in qvec)) or 1.0
                            vn = math.sqrt(sum(x * x for x in sv)) or 1.0
                            s = sum(a * b for a, b in zip(qvec, sv)) / (qn * vn)
                    except Exception:
                        s = 0.0
                if s <= 0:  # bigram 兜底（无向量 / 版本不匹配 / 零分）
                    s = VectorEngine()._cosine(VectorEngine()._vector(query), VectorEngine()._vector(text))
                    if s <= 0:
                        continue
                try:
                    ts = time.mktime(time.strptime(r.get("created_at", ""), "%Y-%m-%d %H:%M:%S"))
                except Exception:
                    ts = now
                age_days = max(0, (now - ts) / 86400)
                decay = max(0.4, 1.0 - age_days / max_age_days)  # 时间衰减：越新权重越高
                scored.append((s * decay, r))
            scored.sort(key=lambda x: x[0], reverse=True)
            out = [{**dict(r), "score": round(s, 3)} for s, r in scored[:top_k]]
            # P2：命中记访问（激活度随使用上调，供遗忘引擎评估）
            for it in out[:3]:
                MemoryService.record_access(conn, it.get("id"))
            return out
        except Exception:
            return []

    # ── 写：主动沉淀 ──
    @staticmethod
    def deposit(conn, agent_id: str, content: str, mem_type: str = "fact",
                source: str = "agent", **extra) -> int | None:
        """写入长期记忆（content 截断防膨胀），同步写真 embedding。返回新记录 id。

        P2：每 maintain_every 次沉淀触发一次维护（遗忘+合并）。
        """
        try:
            if not content or not content.strip():
                return None
            # P0-6：记忆写入前威胁扫描（提示注入/凭据外泄/越权）——命中不落库 + audit 记录
            from core.security_scan import MemoryScanner
            if MemoryScanner.enabled():
                _scan = MemoryScanner.scan(content)
                if not _scan["safe"]:
                    try:
                        from core.audit import audit
                        _first = _scan["matched"][0]
                        audit("", "memory_blocked",
                              f"记忆拦截[{_first['type']}@{_first['rule']}]: {_first['snippet'][:60]}",
                              conn=conn)
                    except Exception:
                        pass
                    return None
            mem_type = mem_type if mem_type in MemoryService.MEM_TYPES else "fact"
            c = content.strip()[:800]
            mem_topic = (str(extra.get("mem_topic") or "")).strip()[:40]  # Auto Memory 主题索引
            vec, ver = MemoryService._embed(conn, c)
            cur = conn.execute(
                "INSERT INTO agent_memory (agent_id, mem_type, content, embedding, embed_version, source, relevance, activation, mem_topic) "
                "VALUES (?,?,?,?,?,?,?,1.0,?)",
                (agent_id, mem_type, c,
                 json.dumps(vec, ensure_ascii=False) if vec else "[]",
                 ver if vec else "",
                 source, extra.get("relevance", 1.0), mem_topic))
            conn.commit()
            # P2：周期维护（每 maintain_every 次沉淀）
            try:
                from core import config as _cfg
                every = int(_cfg.get("memory", "maintain_every", 50))
                if every > 0 and (cur.lastrowid % every == 0):
                    MemoryService.maintain(conn, agent_id)
            except Exception:
                pass
            return cur.lastrowid
        except Exception:
            return None

    # ── P2：命中记访问（激活度随使用上调，供遗忘引擎评估）──
    @staticmethod
    def record_access(conn, memory_id: int) -> None:
        try:
            if not memory_id:
                return
            conn.execute(
                "UPDATE agent_memory SET access_count=access_count+1, "
                "activation=MIN(2.0, activation+0.1), last_accessed_at=datetime('now','localtime') "
                "WHERE id=? AND forgotten=0", (memory_id,))
            conn.commit()
        except Exception:
            pass

    # ── P2：遗忘引擎——激活度低于阈值 → 软遗忘（forgotten=1，检索跳过，可恢复）──
    @staticmethod
    def forget(conn, agent_id: str = "", threshold: float = 0.2, max_age_days: int = 90) -> int:
        """按激活度+时效衰减标记遗忘记忆，返回遗忘条数（软删，不物理删除）。"""
        import time as _t
        try:
            where = "WHERE forgotten=0"
            params: list = []
            if agent_id:
                where += " AND agent_id=?"
                params.append(agent_id)
            rows = conn.execute(
                "SELECT id, activation, access_count, created_at, last_accessed_at FROM agent_memory " + where,
                params).fetchall()
            now = _t.time()
            n = 0
            for r in rows:
                act = r["activation"]
                # 时效衰减：越久未访问激活度越低
                la = r["last_accessed_at"] or r["created_at"] or ""
                try:
                    ts = _t.mktime(_t.strptime(la, "%Y-%m-%d %H:%M:%S"))
                except Exception:
                    ts = now
                age_days = max(0, (now - ts) / 86400)
                decayed = act * max(0.4, 1.0 - age_days / max_age_days)
                if decayed < threshold:
                    conn.execute("UPDATE agent_memory SET forgotten=1 WHERE id=?", (r["id"],))
                    n += 1
            conn.commit()
            return n
        except Exception:
            return 0

    # ── P2：合并引擎——内容相似度高且同类型 → 保留较新，旧标记遗忘（防重复膨胀）──
    @staticmethod
    def consolidate(conn, agent_id: str = "", threshold: float = 0.85) -> int:
        """合并相似记忆，返回合并条数。

        P1b-3 修正：真向量优先（复用存储 embedding，同版本余弦，阈值 0.85 对齐 Mem0 语义合并）；
        bigram 降级时用 0.5（bigram 稀疏余弦对改写句普遍 0.4-0.65，原 0.85 导致合并从不触发）。
        """
        try:
            where = "WHERE forgotten=0"
            params: list = []
            if agent_id:
                where += " AND agent_id=?"
                params.append(agent_id)
            rows = conn.execute(
                ("SELECT id, agent_id, mem_type, content, embedding, embed_version, created_at, forgotten "
                 "FROM agent_memory " + where + " ORDER BY id"), params).fetchall()
            if len(rows) < 2:
                return 0
            # 是否可走真向量：存在同版本 embedding 对
            import math as _m
            rows_dict = [dict(r) for r in rows]
            dense_pairs = {}
            for r in rows_dict:
                if r.get("embed_version") and r.get("embedding"):
                    try:
                        v = json.loads(r["embedding"]) if isinstance(r["embedding"], str) else (r["embedding"] or [])
                        if v:
                            dense_pairs[r["id"]] = (r["embed_version"], v)
                    except Exception:
                        pass
            # bigram 降级时阈值放宽（bigram 余弦量纲 0-1 且稀疏）
            eff_threshold = threshold
            ve = VectorEngine()
            merged = 0
            for i in range(len(rows_dict)):
                a = rows_dict[i]
                if a["forgotten"]:
                    continue
                for j in range(i + 1, len(rows_dict)):
                    b = rows_dict[j]
                    if b["forgotten"]:
                        continue
                    if b["agent_id"] != a["agent_id"] or b["mem_type"] != a["mem_type"]:
                        continue
                    s = 0.0
                    used_dense = False
                    pa, pb = dense_pairs.get(a["id"]), dense_pairs.get(b["id"])
                    if pa and pb and pa[0] == pb[0]:
                        va, vb = pa[1], pb[1]
                        if len(va) == len(vb):
                            qn = _m.sqrt(sum(x * x for x in va)) or 1.0
                            vn = _m.sqrt(sum(x * x for x in vb)) or 1.0
                            s = sum(x * y for x, y in zip(va, vb)) / (qn * vn)
                            used_dense = True
                    if not used_dense:
                        s = ve._cosine(ve._vector(a["content"] or ""), ve._vector(b["content"] or ""))
                        eff_threshold = 0.5  # bigram 降级阈值（对齐量纲）
                    if s >= eff_threshold:
                        # 保留较新（id 大者），旧标记遗忘
                        if b["id"] > a["id"]:
                            conn.execute("UPDATE agent_memory SET forgotten=1 WHERE id=?", (a["id"],))
                            merged += 1
                            break
                        else:
                            conn.execute("UPDATE agent_memory SET forgotten=1 WHERE id=?", (b["id"],))
                            merged += 1
            conn.commit()
            return merged
        except Exception:
            return 0

    # ── P2：周期维护入口（遗忘 + 合并，幂等）──
    @staticmethod
    def maintain(conn, agent_id: str = "") -> dict:
        from core import config as _cfg
        forget_n = 0
        merge_n = 0
        if _cfg.as_bool("memory", "forget_enabled", True):
            forget_n = MemoryService.forget(conn, agent_id,
                                            threshold=float(_cfg.get("memory", "forget_threshold", 0.2)))
        merge_n = MemoryService.consolidate(conn, agent_id,
                                            threshold=float(_cfg.get("memory", "consolidate_threshold", 0.85)))
        return {"forgotten": forget_n, "merged": merge_n}

    # ── 评估沉淀：LLM 提炼 or 规则阈值 ──
    @staticmethod
    def maybe_deposit(conn, agent_id: str, task_content: str,
                      task_query: str = "", min_len: int = 40) -> int | None:
        """执行/任务完成后评估是否值得沉淀。

        真实 LLM 可用 → 要求 LLM 输出 JSON {worth, mem_type, memory, mem_topic}，按提炼结果沉淀；
        Mock/无 key/解析失败 → 规则降级：内容足够长且有实质信息则沉淀为 fact（确定性）。
        """
        try:
            content = (task_content or "").strip()
            if len(content) < min_len:
                return None
            from llm import llm_client
            resp = llm_client.chat(
                [{"role": "system", "content": (
                    "你是 MBSE 领域记忆管理员。判断下面的任务产出是否值得沉淀为 Agent 长期经验"
                    "（fact 事实 / preference 偏好 / experience 经验 / skill 可复用方法）。"
                    '只输出 JSON：{"worth": true/false, "mem_type": "fact", "memory": "一句话提炼", '
                    '"mem_topic": "主题标签（4~10 字，如 建模规范/链路预算方法论）"}，'
                    "记忆内容 30~100 字，不要其他文字。")},
                 {"role": "user", "content": f"任务：{str(task_query)[:200]}\n产出：{content[:1200]}"}],
                _intent="memory_deposit",
            )
            msg = (resp.get("choices") or [{}])[0].get("message", {})
            text = msg.get("content") or ""
            m = re.search(r"\{\s*\"worth\"\s*:\s*(true|false)", text)
            if m and m.group(1) == "true":
                mt = re.search(r"\"mem_type\"\s*:\s*\"(\w+)\"", text)
                mem = re.search(r"\"memory\"\s*:\s*\"([^\"]+)\"", text)
                mtopic = re.search(r"\"mem_topic\"\s*:\s*\"([^\"]+)\"", text)
                mem_type = mt.group(1) if mt else "experience"
                memory = mem.group(1) if mem else content[:200]
                mem_topic = mtopic.group(1).strip() if mtopic else ""
                return MemoryService.deposit(conn, agent_id, memory, mem_type, source="llm_agent",
                                             mem_topic=mem_topic)
        except Exception:
            pass
        # 规则降级：有实质内容 → 沉淀为经验，主题取内容首词（确定性）
        if len(content) > 120 and not content.startswith(("抱歉", "我理解", "作为")):
            return MemoryService.deposit(conn, agent_id, content[:400], "experience", source="rule_agent",
                                         mem_topic=MemoryService._rule_topic(content))
        return None

    @staticmethod
    def _rule_topic(content: str) -> str:
        """规则降级主题：取内容前 10 字符（去除常见语气词），保证确定性。"""
        for _pre in ("关于", "针对", "我们", "需要", "请", "对", "在"):
            if content.startswith(_pre):
                content = content[len(_pre):]
                break
        return re.sub(r"[\s,，。；;：:、]+", "", content)[:10]

    # ── 会话级推动（Hermes 式定期评估）──
    @staticmethod
    def push(conn, agent_id: str, user_input: str, output: str) -> int | None:
        """会话结束推动：从用户输入+产出中提炼值得长期保留的偏好/经验。

        仅真实 LLM 可用时生效（Mock 返回普通文本无法解析 JSON → 跳过，确定性保持）。
        """
        try:
            from llm import llm_client
            resp = llm_client.chat(
                [{"role": "system", "content": (
                    "你是记忆沉淀器。根据对话判断是否存在值得长期记住的用户偏好或可复用经验。"
                    '若有输出 JSON：{"worth": true, "mem_type": "preference", "memory": "提炼内容", '
                    '"mem_topic": "主题标签（4~10 字）"}；'
                    "否则输出 {\"worth\": false}。只输出 JSON。")},
                 {"role": "user", "content": f"用户：{str(user_input)[:300]}\n助手产出：{str(output)[:600]}"}],
                _intent="memory_push",
            )
            msg = (resp.get("choices") or [{}])[0].get("message", {})
            text = msg.get("content") or ""
            m = re.search(r"\{\s*\"worth\"\s*:\s*(true|false)", text)
            if m and m.group(1) == "true":
                mt = re.search(r"\"mem_type\"\s*:\s*\"(\w+)\"", text)
                mem = re.search(r"\"memory\"\s*:\s*\"([^\"]+)\"", text)
                mtopic = re.search(r"\"mem_topic\"\s*:\s*\"([^\"]+)\"", text)
                mem_type = mt.group(1) if mt else "preference"
                memory = mem.group(1) if mem else ""
                mem_topic = mtopic.group(1).strip() if mtopic else ""
                if memory:
                    return MemoryService.deposit(conn, agent_id, memory, mem_type, source="push",
                                                 mem_topic=mem_topic)
        except Exception:
            pass
        return None
