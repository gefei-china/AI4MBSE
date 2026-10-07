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

    # ── 多维作用域助手（对齐 mem0 四维 scope + 复合过滤；缺列/无作用域 → 逐字沿用旧行为）──
    @staticmethod
    def _has_scope_cols(conn) -> bool:
        """探测 agent_memory 是否已有 scope_type/scope_id（老库/夹具库缺列 → False，走旧 SQL）。"""
        try:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(agent_memory)").fetchall()]
            return "scope_type" in cols and "scope_id" in cols
        except Exception:
            return False

    @staticmethod
    def _has_tier_col(conn) -> bool:
        """探测 agent_memory 是否已有 tier 列（P1-18 分层；老库/夹具库缺列 → False，走不分层路径）。"""
        try:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(agent_memory)").fetchall()]
            return "tier" in cols
        except Exception:
            return False

    @staticmethod
    def _scope_filter(conn, agent_id: str, scopes):
        """构造作用域复合过滤 → (clause, params, idx_of)。

        scopes 为**有序**槽位 [(type, id), ...]（优先级高→低），如
        [('project', pid), ('user', uname), ('agent', intent), ('global', '')]。
        存量行（scope_type=''）按 agent 槽召回 —— 保证老记忆不静默消失。
        返回 clause 形如 " AND ( (...=?) OR ... OR (scope_type='' AND agent_id=?) )"；
        无作用域/缺列 → ("", [], {})（调用方沿用旧 SQL）。
        """
        try:
            scopes = [(str(t), str(i or "")) for t, i in (scopes or []) if t]
        except Exception:
            return "", [], {}
        if not scopes or not MemoryService._has_scope_cols(conn):
            return "", [], {}
        parts, params = [], []
        for st, sid in scopes:
            if st == "global":
                parts.append("(scope_type='global' AND scope_id='')")
            else:
                parts.append("(scope_type=? AND scope_id=?)")
                params.extend([st, sid])
        parts.append("(scope_type='' AND agent_id=?)")   # 存量行
        params.append(agent_id)
        return " AND (" + " OR ".join(parts) + ")", params, {t: i for i, (t, _i) in enumerate(scopes)}

    @staticmethod
    def _scope_boosts(n: int) -> list:
        """作用域优先级加成序列（配置 memory.scope_boost，逗号分隔；不足则用末位补齐）。"""
        vals = []
        try:
            from core import config as _cfg
            raw = str(_cfg.get("memory", "scope_boost", "1.35,1.18,1.06,1.0") or "")
            vals = [float(x) for x in raw.split(",") if str(x).strip()]
        except Exception:
            vals = []
        if not vals:
            vals = [1.35, 1.18, 1.06, 1.0]
        while len(vals) < n:
            vals.append(vals[-1])
        return vals

    @staticmethod
    def _scope_boost_of(row: dict, idx_of: dict, agent_id: str) -> float:
        """软重排加成：按槽位序号取；存量行按 agent 槽；不在槽位内 → 最低档 1.0（仅打破近邻，不做硬分桶）。"""
        try:
            if not idx_of:
                return 1.0
            st = str(row.get("scope_type") or "")
            idx = idx_of.get("agent", len(idx_of)) if not st else idx_of.get(st, len(idx_of))
            boosts = MemoryService._scope_boosts(len(idx_of) + 1)
            return boosts[idx] if 0 <= idx < len(boosts) else 1.0
        except Exception:
            return 1.0

    # ── 读：语义检索 ──
    @staticmethod
    def search(conn, agent_id: str, query: str = "", top_k: int = 5,
               mem_type: str = "", max_age_days: int = 90, mem_topic: str = "",
               scopes: list | None = None, any_scope: bool = False) -> list:
        """按语义相关度检索记忆（真向量优先 + 时间衰减；bigram 兜底）。

        mem_topic 非空 → 先按主题标签精确过滤（Auto Memory 索引，避免平铺误拉），再语义排序。
        query 为空 → 退化为最近 top_k 条（兼容旧行为）。
        scopes（对齐 mem0 复合过滤）：有序作用域槽位 [(type, id), ...]（优先级高→低）。
          非空 → 作用域复合过滤 + 优先级软重排（不做硬分桶）；为空/None → **逐字沿用旧行为**（仅 agent_id）。
        any_scope（2026-09-29 新增）→ **不加任何 归属/作用域 过滤，全库语义检索**。
          用途：`agent/memory_recall.py` 的"跨域兜底" —— 当 intent 落在稀疏域（chat=1 条）
          而通用方法论在别的域时，同域召回必然为 0。此时需要一路"放弃归属过滤"的召回。
          ⚠️ 与 scopes 的语义区别：scopes 是**扩大 OR 集合**（project∪user∪agent∪global 槽），
             而库里大量行 `scope_type='project'` 只匹配特定槽 —— 实测 `scopes=[('global','')]`
             或 `[('agent','')]` 都召回 0 条。any_scope=True 才是真正"全库"。
          默认 False = **零行为漂移**（既有调用方逐字不变）。
        返回 [{content, mem_type, score, created_at, scope_type?, scope_id?}]。
        """
        try:
            _clause, _cparams, _idx_of = MemoryService._scope_filter(conn, agent_id, scopes)
            if any_scope:
                _clause, _cparams, _idx_of = "", [], {}
            # P1-18 分层：常规召回排除 archival（归档层不参与 query 相关性召回；存量 NULL tier 视为 recall）
            _tc = MemoryService._has_tier_col(conn)
            _no_arch = " AND (tier IS NULL OR tier != 'archival')" if _tc else ""
            if not query:
                # 作用域生效时不再用 agent_id 作唯一约束（user/global 槽需跨 Agent 复用）
                if any_scope:
                    _base = "forgotten=0" + _no_arch
                else:
                    _base = ("forgotten=0" + _clause + _no_arch) if _clause else "agent_id=? AND forgotten=0" + _no_arch
                sql = "SELECT * FROM agent_memory WHERE " + _base
                params: list = [] if any_scope else (list(_cparams) if _clause else [agent_id])
                if mem_topic:
                    sql += " AND mem_topic=?"
                    params.append(mem_topic)
                sql += " ORDER BY id DESC LIMIT ?"
                params.append(top_k)
                rows = conn.execute(sql, params).fetchall()
                return [dict(r) for r in rows]
            if any_scope:
                _base = "mem_type!='session' AND forgotten=0" + _no_arch
            else:
                _base = ("mem_type!='session' AND forgotten=0" + _clause + _no_arch) if _clause else "agent_id=? AND mem_type!='session' AND forgotten=0" + _no_arch
            sql = "SELECT * FROM agent_memory WHERE " + _base
            params = [] if any_scope else (list(_cparams) if _clause else [agent_id])
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
                # 作用域优先级软重排（mem0 式）：仅调整排序，不改写返回的 score（注入文案口径不变）
                _boost = MemoryService._scope_boost_of(r, _idx_of, agent_id)
                scored.append((s * decay * _boost, s, r))
            scored.sort(key=lambda x: x[0], reverse=True)
            out = [{**dict(r), "score": round(_raw, 3)} for _fin, _raw, r in scored[:top_k]]
            # P2：命中记访问（激活度随使用上调，供遗忘引擎评估）
            for it in out[:3]:
                MemoryService.record_access(conn, it.get("id"))
            return out
        except Exception:
            return []

    # ── P1-18 分层读：core 层常驻记忆（不依赖 query 相关性，注入时无条件附带）──
    @staticmethod
    def search_core(conn, agent_id: str, top_k: int = 2, scopes: list | None = None) -> list:
        """取 core 层记忆（常驻注入，不按 query 相关性过滤）。

        core = 用户偏好（preference）等关键、稳定、跨会话复用的记忆。这些记忆即使与当前
        query 无关也应保留在上下文里（对齐 Letta 的 core memory / Claude 的持久偏好）。
        按 activation 降序取 top_k；返回 [{content, mem_type, tier, scope_type?, scope_id?}]。
        tier 列不存在（老库/夹具）→ 返回空（不分层，零行为漂移）。
        """
        try:
            if not MemoryService._has_tier_col(conn):
                return []
            _clause, _cparams, _idx_of = MemoryService._scope_filter(conn, agent_id, scopes)
            _base = ("tier='core' AND forgotten=0" + _clause) if _clause else "agent_id=? AND tier='core' AND forgotten=0"
            sql = "SELECT * FROM agent_memory WHERE " + _base + " ORDER BY activation DESC, id DESC LIMIT ?"
            params = list(_cparams) if _clause else [agent_id]
            params.append(top_k)
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []

    # ── 写：主动沉淀 ──
    @staticmethod
    def deposit(conn, agent_id: str, content: str, mem_type: str = "fact",
                source: str = "agent", scope_type: str = "", scope_id: str = "", **extra) -> int | None:
        """写入长期记忆（content 截断防膨胀），同步写真 embedding。返回新记录 id。

        scope_type/scope_id：多维作用域（global/project/user/agent）。**ADD-only 不覆盖**——只追加，
        从不 UPDATE 旧记忆（对齐 mem0），去重/时效交给合并与软重排。
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
            st = (str(scope_type or "")).strip()[:20]
            sid = (str(scope_id or "")).strip()[:120]
            vec, ver = MemoryService._embed(conn, c)
            _sc = MemoryService._has_scope_cols(conn)   # 老库/夹具库缺列 → 走旧列集（fail-safe）
            _tc = MemoryService._has_tier_col(conn)     # P1-18 分层：preference 恒 core，其余 recall
            _tier = "core" if mem_type == "preference" else "recall"
            cur = conn.execute(
                "INSERT INTO agent_memory (agent_id, mem_type, content, embedding, embed_version, source, relevance, activation, mem_topic"
                + (", tier" if _tc else "")
                + (", scope_type, scope_id" if _sc else "") + ") "
                "VALUES (?,?,?,?,?,?,?,1.0,?"
                + (",?" if _tc else "")
                + (",?,?" if _sc else "") + ")",
                (agent_id, mem_type, c,
                 json.dumps(vec, ensure_ascii=False) if vec else "[]",
                 ver if vec else "",
                 source, extra.get("relevance", 1.0), mem_topic)
                + ((_tier,) if _tc else ())
                + ((st, sid) if _sc else ()))
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
    def forget(conn, agent_id: str = "", threshold: float = 0.2, max_age_days: int = 90,
               max_unused_days: int = 0, min_access: int = 0) -> int:
        """按「时效 + 使用证据」标记遗忘记忆，返回遗忘条数（软删，不物理删除，可恢复）。

        ⚠️ 2026-09-29 根因修复：原实现 `decayed = activation * max(0.4, 1 - age/max_age)`
        **在本工程下结构性永不触发**。实测与证明：
          - `deposit()` 里 activation 硬编码初值 **1.0**，且 `record_access` 只做 `+0.1`（上限 2.0）
            —— **activation 从不下降**；
          - 衰减因子的下界是 **0.4**（`max(0.4, ...)`）；
          - 故 `decayed ≥ 1.0 * 0.4 = 0.4`，而阈值 0.2 → `0.4 < 0.2` 恒假；
          - 实测：对生产库 118 条未遗忘记忆跑 `forget(threshold=0.2, max_age_days=90)`
            → **遗忘 0 条（0%）**。"遗忘引擎"此前是**死代码**。
        非空转证据（数学穷举）：唯有 activation < 0.5 时才可能触发，而没有任何代码路径能把它降到 0.5 以下。

        **修法**：判据不再依赖"只增不减的 activation"，改为**引入真实时间维度 + 使用证据**：
          1) **时效遗忘**：距「最后访问（无则创建）」超过 `max_unused_days` → 遗忘。
             这是主判据，直接对齐行业（LRU / TTL 式记忆淘汰），不像 activation 那样会自我抵消。
          2) **低价值遗忘**：`access_count <= min_access`（默认 0 = 从未被访问）
             且已超过 `max_age_days` → 遗忘。即「放了很久又从没用过」= 没价值。
          3) 原 activation 公式**保留**为附加通道（配置 `memory.forget_by_activation`
             默认**关**）：因为它对 activation < 0.5 的行仍有效，且关掉可避免
             "旧口径突然生效导致批量误删"。默认关 = 行为可预测、不惊群。

        参数：
          max_unused_days —— 主判据：多少天未被访问即遗忘；**0/None = 该判据不启用**（防误配全删）
          min_access      —— 低价值判据的访问次数上限（默认 0 = 从未访问过）
          max_age_days    —— 低价值判据的年龄门槛（沿用原参数名，语义扩展为"陈旧门槛"）

        返回遗忘条数。异常返回 0（不阻断）。
        """
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
            _by_act = False
            try:
                from core import config as _cfg
                _by_act = _cfg.as_bool("memory", "forget_by_activation", False)
            except Exception:
                _by_act = False
            n = 0
            for r in rows:
                la = r["last_accessed_at"] or r["created_at"] or ""
                try:
                    ts = _t.mktime(_t.strptime(la, "%Y-%m-%d %H:%M:%S"))
                except Exception:
                    ts = now           # 时间解析失败 → 视为"刚访问"，不因脏数据误删
                age_days = max(0, (now - ts) / 86400)
                _access = int(r["access_count"] or 0)
                reason = ""
                # 判据 1（主）：久未访问
                if max_unused_days and max_unused_days > 0 and age_days >= max_unused_days:
                    reason = f"unused_{int(age_days)}d"
                # 判据 2：陈旧且从未被用过
                elif (max_age_days and max_age_days > 0 and age_days >= max_age_days
                      and _access <= int(min_access or 0)):
                    reason = f"stale_{int(age_days)}d_access{_access}"
                # 判据 3（可选，默认关）：原 activation 通道
                elif _by_act:
                    decayed = float(r["activation"] or 0) * max(0.4, 1.0 - age_days / max(max_age_days, 1))
                    if decayed < threshold:
                        reason = f"activation_{round(decayed, 3)}"
                if reason:
                    conn.execute("UPDATE agent_memory SET forgotten=1 WHERE id=?", (r["id"],))
                    # P1-18 分层：遗忘同步标记 archival（软删与归档一致）
                    if MemoryService._has_tier_col(conn):
                        conn.execute("UPDATE agent_memory SET tier='archival' WHERE id=?", (r["id"],))
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
            _sc = MemoryService._has_scope_cols(conn)
            _cols = "id, agent_id, mem_type, content, embedding, embed_version, created_at, forgotten"
            if _sc:
                _cols += ", scope_type, scope_id"
            rows = conn.execute(
                ("SELECT " + _cols + " FROM agent_memory " + where + " ORDER BY id"), params).fetchall()
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
                    # 作用域隔离：只在同一 (scope_type, scope_id) 内合并（防跨工程/跨用户误合并）
                    if _sc and ((a.get("scope_type") or ""), (a.get("scope_id") or "")) != \
                            ((b.get("scope_type") or ""), (b.get("scope_id") or "")):
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
        except Exception as _ce:
            # ⚠️ 2026-10-07：**不再静默 return 0**。
            # 原实现 `except Exception: return 0` 会把「SQL 缺列 / 字段名写错 / schema 不一致」
            # 这类**硬错误伪装成"没东西可合并"** —— 实测做门禁时因夹具缺 `created_at`
            # 报 `no such column`，却被这句吞掉，表现为"consolidate 判重逻辑坏了"，
            # 排查方向被完全带偏（第一版误以为是相似度阈值问题）。
            # 本函数是**离线维护路径**（不是请求主链路），留一条 warning 不影响性能，
            # 却能让"合并功能坏了"变成可诊断。
            try:
                import logging as _lg
                _lg.getLogger("mbse.memory").warning(
                    "consolidate 失败（合并未执行）：%s: %s", type(_ce).__name__, str(_ce)[:160])
            except Exception:
                pass
            return 0

    # ══════════════════════════════════════════════════════════════════════
    # 冲突消解（D1，2026-10-07）—— 方案 A：影子状态 + 召回期仲裁
    #
    # ⚠️ **与 consolidate() 的区别（最容易混为一谈，务必分清）**：
    #   consolidate()：判据是**相似度高**（≥0.85） ⇒ 处理**重复**
    #                （"需求追溯链五环节"两种措辞）
    #   detect_conflicts()：判据是**相似度低但同键** ⇒ 处理**互斥**
    #                （"vc 格式 A" vs "vc 格式 B"）
    #   ⇒ **互斥事实的相似度天然偏低，永远达不到合并阈值，对 consolidate 零作用。**
    #
    # ⚠️ **为什么默认关闭**（`memory.conflict_enabled` 默认 False）：
    #   本判据**会误判**——把"互补的两条经验"当成冲突。
    #   存量 144 条里已有 id133/134/135 这类"措辞近似的重复"，
    #   若误判会把互补内容挤掉⇒ **必须先用 S5 存量误判率抽检验证再开**。
    # ══════════════════════════════════════════════════════════════════════

    #: 互斥断言词（判据②用，②筛出候选后才走这里 ⇒ 从稀到贵省 LLM 调用）。
    #:
    #: ⚠️⚠️ **2026-10-07 存量实测后大幅收窄**（这是本轮最重要的一次纠错）：
    #: 原本收录「不是 / 而非 / 而」等词，**实测在存量 144 条上造成 2 组误伤**：
    #:   - #285「…须引用 requirement usage **而非** requirement definition」
    #:   - #172「…两类专用关系表达」被同组另一条连带命中
    #: 「**A 而非 B**」是 SysML/建模领域的**标准精确限定句式**，不是"互斥声明"。
    #: ⇒ 凡表示「限定/排除/对比」的词一律剔除；**只保留明确指向"口径已变更"的**。
    #:
    #: 判据标准：**该词出现 ⇒ 说话者在宣告"旧的不作数了"**。
    #: 「不再/已废弃/已更正/已过时/已失效」满足；「不是/而非/而不是」不满足。
    _CONFLICT_MARKERS = (
        "不再", "已废弃", "已废止", "已更正", "更正为",
        "应改为", "已过时", "已失效", "已作废", "废止该规则",
    )

    @staticmethod
    def detect_conflicts(conn, agent_id: str = "", topic: str = "",
                         scope_type: str = "", scope_id: str = "",
                         sim_high: float = 0.5) -> list:
        """检测**互斥**记忆，返回冲突组列表 `[[id_a, id_b], ...]`（不写库）。

        两道判据（**同时满足**才判冲突）：
        ① **同键**：同 `mem_topic`（无topic 时按 agent）、同 `scope_type/scope_id`
           —— 不跨作用域，避免把"项目 A 的口径"与"项目 B 的口径"当冲突。
        ② **高相似**（`score >= sim_high`）**且任一方含互斥断言词**。

        ⚠️⚠️ **判据②的方向是"高相似"，与实施计划 §5 D1 写的"低相似"相反**——
        这是 2026-10-07 实测推翻的，**别按计划书的字面理解**：

        实测「工程 vc 格式为 branchId,quId」vs「工程 vc 格式**不再是** branchId,quId，
        已废弃该规则」→ bigram 相似度 **0.7252**（很高）。
        **原因很直白**：互斥事实改的正是那几个关键词，两条必然字面高度重叠。
        ⇒ 原方案「相似度低 ⇒ 才判冲突」与判据①「同键」**自相矛盾**，
           会把**所有真实互斥对全部漏判**（S1 正例实测0 检出）。

        **修正后的分工**（与 `consolidate()` 严丝合缝，不重叠）：
        | 内容关系 | 相似度 | 互斥词 | 归谁管 |
        |---|---|---|---|
        | 重复（同一结论两种措辞） | 高 | 无 | `consolidate()` |
        | **互斥（新口径否定旧口径）** | **高** | **有** | **`detect_conflicts()`** |
        | 无关/互补 | 低 | 无 | 都不管 |
        ⇒ 区分二者的**唯一可靠信号是互斥词**，不是相似度。
        """
        try:
            where = ["forgotten=0", "COALESCE(superseded_by,0)=0"]
            params: list = []
            if agent_id:
                where.append("agent_id=?")
                params.append(agent_id)
            if topic:
                where.append("mem_topic=?")
                params.append(topic)
            if scope_type:
                where.append("scope_type=?")
                params.append(scope_type)
            if scope_id:
                where.append("scope_id=?")
                params.append(scope_id)
            rows = conn.execute(
                "SELECT id, agent_id, mem_type, content, mem_topic, scope_type, scope_id, embedding, embed_version "
                "FROM agent_memory WHERE " + " AND ".join(where) + " ORDER BY id", params).fetchall()
            if len(rows) < 2:
                return []
            rows = [dict(r) for r in rows]
            # 按「同 topic 或同 agent」分组（无 topic 时按 agent 分，避免全库两两比）
            groups: dict = {}
            for r in rows:
                key = (r.get("scope_type") or "", r.get("scope_id") or "",
                       r.get("mem_topic") or ("__agent__:" + str(r.get("agent_id"))))
                groups.setdefault(key, []).append(r)
            try:
                from knowledge_engine import VectorEngine
                ve = VectorEngine()
            except Exception:
                ve = None
            out: list = []
            for _key, g in groups.items():
                if len(g) < 2:
                    continue
                for i in range(len(g)):
                    for j in range(i + 1, len(g)):
                        a, b = g[i], g[j]
                        # ② 高相似（同主题下高度重叠 ⇒ 可能是"同一件事的两种说法"）
                        s = _mem_cosine(ve, a, b)
                        if s is None or s < sim_high:
                            continue
                        # 互斥词（任一方）—— 这是与 consolidate 的**唯一**区分信号
                        if not (_has_conflict_marker(a["content"])
                                or _has_conflict_marker(b["content"])):
                            continue
                        out.append([a["id"], b["id"]])
            return out
        except Exception:
            return []

    @staticmethod
    def mark_superseded(conn, old_id: int, new_id: int) -> bool:
        """把 `old_id` 标记为被 `new_id` 取代（**只打标记，不删内容**）。

        ⚠️ **影子语义**：`superseded_by != 0` 表示"这条有更新的版本"，
        但**原内容仍在库里**，可观察 / 可回退 / 可审计。
        直接 UPDATE 覆盖旧内容会让误判不可逆（见类头注释）。
        """
        try:
            if not old_id or not new_id or int(old_id) == int(new_id):
                return False
            cur = conn.execute(
                "UPDATE agent_memory SET superseded_by=?, superseded_at=datetime('now','localtime') "
                "WHERE id=? AND forgotten=0", (int(new_id), int(old_id)))
            conn.commit()
            return cur.rowcount > 0
        except Exception:
            return False

    # ── P2：周期维护入口（遗忘 + 合并，幂等）──
    @staticmethod
    def maintain(conn, agent_id: str = "") -> dict:
        """记忆维护：遗忘 + 合并。返回 {"forgotten": n, "merged": m}。

        ⚠️ 2026-09-29：遗忘判据已修（见 `forget` 的 docstring —— 原公式结构性永不触发）。
        新增两个配置：`memory.max_unused_days`（主判据，默认 0=不启用时效遗忘）、
        `memory.forget_min_access`（低价值判据的访问次数上限，默认 0）。
        ⚠️ 默认值刻意**保守**（时效遗忘默认关）：修完根因不等于要立刻批量清库 ——
        先把"能遗忘"的能力接上，阈值由配置逐步放开，避免旧库一次性被清空。
        """
        from core import config as _cfg
        forget_n = 0
        merge_n = 0
        if _cfg.as_bool("memory", "forget_enabled", True):
            forget_n = MemoryService.forget(
                conn, agent_id,
                threshold=float(_cfg.get("memory", "forget_threshold", 0.2)),
                max_age_days=int(_cfg.get("memory", "max_age_days", 90)),
                max_unused_days=int(_cfg.get("memory", "max_unused_days", 0)),
                min_access=int(_cfg.get("memory", "forget_min_access", 0)))
        merge_n = MemoryService.consolidate(conn, agent_id,
                                            threshold=float(_cfg.get("memory", "consolidate_threshold", 0.85)))
        return {"forgotten": forget_n, "merged": merge_n}

    @staticmethod
    def run_maintenance(conn=None) -> dict:
        """**独立**维护入口（2026-09-29）—— 不再依赖 `deposit()` 触发。

        修复的第二个问题：`maintain()` 此前**只在 `deposit()` 内部**被调用（每 maintain_every 次写入触发一次）。
        后果：**只要系统不再写新记忆，过期经验就永远不被清理**（"只有倒垃圾时才扫地"）。
        实测：生产库 125 条记忆，7 条已遗忘，而遗忘引擎因公式缺陷一条也删不掉。

        本函数供外部调度调用（管理端点 / 定时任务 / 运维脚本），与写入解耦：
            from memory_service import MemoryService
            MemoryService.run_maintenance()
        返回 {"forgotten": n, "merged": m}；异常返回 {"forgotten": 0, "merged": 0, "error": ...}。
        """
        own = False
        try:
            if conn is None:
                from database import get_db
                conn = get_db()
                own = True
            res = MemoryService.maintain(conn)
            return res
        except Exception as e:
            return {"forgotten": 0, "merged": 0, "error": str(e)[:200]}
        finally:
            if own and conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    # ── 评估沉淀：LLM 提炼 or 规则阈值 ──
    #  2026-09-29 重写：原规则兜底 `len>120 and not startswith(抱歉/我理解/作为)` → 沉淀 content[:400]
    #  实测后果（生产库 118 条未遗忘记忆）：
    #    - 47%(56/118) 从未被检索命中过 → "沉淀即死"；
    #    - rule_agent 7 条里混入整篇报告正文（"# MBSE 任务汇总最终报告…"）、寒暄回复（"你好，我是…"）；
    #    - reflow 54 条的绝大多数是**图谱实体本身**（"IP67防护等级要求：系统必须满足…（来源 test-reg）"）
    #      —— 那是知识图谱的数据，不是"经验"，属于把知识库复制进记忆库。
    #  病根：**"长"被当成"值得记"**。长度只是"有实质内容"的弱代理，与"可复用"无关。
    #  修法见下方 _extract_worthy()：三重门（长度带 → 噪音门 → 经验信号门），宁可少沉淀。
    @staticmethod
    def _extract_worthy(content: str, min_len: int) -> tuple[bool, str]:
        """判断文本是否值得沉淀为长期经验。返回 (是否值得, 拒绝原因)。

        三重门（2026-09-29 新增）：
          门1 长度带：太短（<min_len）无信息；**过长（>600）是报告/文档正文，不是经验**
                     —— 经验应是一句可复用的判断/方法，而不是一段交付物。
          门2 噪音门：寒暄、自我介绍、兜底道歉、纯测试输入、图谱实体样式的"X：Y（来源 Z）"。
          门3 经验信号门：必须含有"可复用的判断/方法论"措辞（须/应/先…再/不要/避免/推荐/
                     否则/注意/原则/规范/流程/已确认/口径 等），否则只是陈述性事实 → 归 fact/不沉淀。
        """
        c = (content or "").strip()
        if len(c) < min_len:
            return False, "too_short"
        if len(c) > 600:
            return False, "too_long_looks_like_document"
        # 门2 噪音
        for pat in MemoryService._NOISE_RE:
            if re.search(pat, c, re.I):
                return False, "noise:" + pat
        # 整篇报告/文档特征（Markdown 标题开头 / 多行分段 / 引用块）
        if re.match(r"^#{1,3}\s", c) or c.count("\n") >= 3 or re.search(r"(?m)^\s*>", c):
            return False, "document_body"
        # 门3 经验信号：词表命中 或 结构模式命中（先…再… / 若…则…）
        _has_sig = any(k in c for k in MemoryService._VALUE_KEYS) or \
            any(re.search(p, c) for p in MemoryService._VALUE_RE)
        if not _has_sig:
            return False, "no_actionable_value"
        return True, ""

    # 噪音正则（寒暄/自介/测试残留/图谱实体样式）
    _NOISE_RE = (
        r"^(你好|您好|hi|hello|嗨)",
        r"我是.{0,16}(助手|智能体|AI 助手|通用助手)",
        r"^\s*(抱歉|我理解|作为)",
        r"我这边没有识别到",
        r"方便补充一下",
        r"^[^\n]{0,30}[：:]\s*[^\n]{0,40}（来源\s*[A-Za-z0-9_\-]+）\s*$",  # 图谱实体「X：Y（来源 test-reg）」
        r"^\s*\d{1,6}\s*$",                                          # 纯数字输入
        # ── Mock / 占位回声（2026-10-07 评测实测：id=276 `（Mock 回答）已收到你的消息：…`
        #    access=70、召回 score 0.520 排第1 ⇒ 两道闸全漏）──
        # 只锚**括号前缀**形态，不用宽泛的"含 Mock 就拒"（真经验里也可能出现该词）。
        r"^[(（【\[]\s*(Mock|MOCK|mock)\s*(回答|回复|响应)?\s*[)）\]】]",
    )
    # 经验"可复用性"信号词：出现任意一个才认为含有方法论/判断（否则只是事实陈述）
    # ⚠️ 2026-09-29 两轮教训（均有实测样本支撑）：
    #   轮1：**裸单字假阳性** —— 「允」用于"对**应**关系"、「先/再」用于"**先**进"，
    #       使「…之间关系与对应关系…」这类纯陈述被误判为"有经验价值"。
    #   轮2：**一刀切删单字又误杀真经验** —— 生产库里两条高价值记忆（vc 格式口径 / 包树查询方法）
    #       都靠「须按包取源码」「先…再…」承载，删掉后它们被判"无价值"。
    #   正解：**单字必须带邻接约束**（情态动词 + 动作动词才算方法论），
    #        另外补上"先…再…"这种**跨小句结构模式**（用正则，不是子串）。
    _VALUE_KEYS = (
        # 情态/义务（多字，安全）
        "必须", "不得", "禁止", "务必", "应该", "应当", "否则", "不建议", "不要",
        "避免", "注意", "推荐", "才能", "原则", "规范", "口径", "流程", "步骤",
        "方法论", "建议", "关键点", "要点", "前提", "约束条件",
        # 踩坑/实证
        "易在", "易被", "易于", "容易", "坑", "教训", "踩过", "实测", "已验证",
        "已确认", "结论是", "仅支持", "只能", "不支持", "区别在于", "相比之下", "优先",
        # 单字情态 + 动作（邻接约束，避免"对应/先进"式假阳性）
        "须按", "须先", "须在", "须携", "须经", "须用", "须以", "须通过",
        "需先", "需按", "需在", "需二", "需通", "需经", "需携带", "需要先",
    )
    # 结构型信号（正则）：先…再…、先…然后…、如果…就/则…、一旦…就…
    _VALUE_RE = (
        r"先[^。；;]{0,40}?[，,]\s*再",
        r"先[^。；;]{0,40}?然后",
        r"(如果|若|一旦)[^。；;]{0,60}?(就|则|便)",
    )

    @staticmethod
    def maybe_deposit(conn, agent_id: str, task_content: str,
                      task_query: str = "", min_len: int = 40,
                      scope_type: str = "", scope_id: str = "", user_scope=None) -> int | None:
        """执行/任务完成后评估是否值得沉淀。

        真实 LLM 可用 → 要求 LLM 输出 JSON {worth, mem_type, memory, mem_topic}，按提炼结果沉淀；
        Mock/无 key/解析失败 → 规则降级 `_extract_worthy()`（三重门，宁缺毋滥）。
        """
        try:
            content = (task_content or "").strip()
            if len(content) < min_len:
                return None
            from llm import llm_client
            resp = llm_client.chat(
                [{"role": "system", "content": (
                    "你是 MBSE 领域记忆管理员。判断下面的任务产出是否值得沉淀为 Agent 长期经验"
                    "（fact 事实 / preference 偏好 / experience 经验 / skill 可复用方法）。\n"
                    "**只有满足以下之一才 worth=true**："
                    "① 可复用的方法/步骤/口径；② 一条踩过的坑或反直觉事实；"
                    "③ 用户的稳定偏好；④ 已确认的关键约定（接口格式、命名规则、边界条件）。\n"
                    "**以下一律 worth=false**：寒暄/自我介绍/兜底道歉；整篇报告或文档正文；"
                    "知识图谱里已有的实体（如「某需求：系统必须满足…」）；"
                    "一次性任务的产出物内容；没有普适性的具体数值。\n"
                    "宁可漏记，不可错记——错误经验会在后续检索中被当参考传播。"
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
                # LLM 提炼结果仍需过噪音门（防 LLM 复读原文/记住寒暄）
                ok, why = MemoryService._extract_worthy(memory, min_len=12)
                if not ok:
                    return None
                # 偏好类记忆 → 用户作用域（跨 Agent 复用）；其余落默认作用域
                _st, _sid = scope_type, scope_id
                if mem_type == "preference" and user_scope:
                    _st, _sid = user_scope[0], user_scope[1]
                return MemoryService.deposit(conn, agent_id, memory, mem_type, source="llm_agent",
                                             mem_topic=mem_topic, scope_type=_st, scope_id=_sid)
        except Exception:
            pass
        # 规则降级：三重门通过才沉淀（确定性）；提炼为一句，不整段抄
        ok, why = MemoryService._extract_worthy(content, min_len=min_len)
        if ok:
            return MemoryService.deposit(conn, agent_id, MemoryService._condense(content), "experience",
                                         source="rule_agent",
                                         mem_topic=MemoryService._rule_topic(content),
                                         scope_type=scope_type, scope_id=scope_id)
        return None

    @staticmethod
    def _condense(content: str, limit: int = 200) -> str:
        """规则降级时的"一句话提炼"：取首个完整句/前 limit 字，避免整段抄入库。

        2026-09-29：原实现直接 `content[:400]` —— 把报告正文整段（含 Markdown 标题、
        "> 说明：…"引用块）塞进记忆，导致记忆库被交付物污染。改为**按句界截取首句**。
        """
        c = re.sub(r"\s+", " ", (content or "").strip())
        m = re.search(r"^(.{20,}?[。；;!?！？])", c)
        if m and len(m.group(1)) <= limit:
            return m.group(1).strip()
        return c[:limit].strip()


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
    def push(conn, agent_id: str, user_input: str, output: str,
             scope_type: str = "", scope_id: str = "") -> int | None:
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
                    # 2026-09-29：与 maybe_deposit 同口径 —— 提炼结果仍须过噪音门
                    ok, _why = MemoryService._extract_worthy(memory, min_len=8)
                    if not ok:
                        return None
                    return MemoryService.deposit(conn, agent_id, memory, mem_type, source="push",
                                                 mem_topic=mem_topic,
                                                 scope_type=scope_type, scope_id=scope_id)
        except Exception:
            pass
        return None


# ══════════════════════════════════════════════════════════════════════════
# 冲突消解的模块级辅助（D1，2026-10-07）
#放模块级而非类内staticmethod：`_CONFLICT_MARKERS` 需在函数里直接引用，
# 且这两个纯函数无状态、可被门禁直接import 做变异测试。
# ══════════════════════════════════════════════════════════════════════════

def _has_conflict_marker(text) -> bool:
    """内容里是否出现**互斥断言词**。

    ⚠️ 只认明确的"否定/更正/废弃"信号（`_CONFLICT_MARKERS`）。
    **刻意不收**"新版/更新后/改为"这类宽泛词——实测「新旧版本对比」类正常经验
    会命中，那是**互补**而非**互斥**，收了就是误判。
    （本工程既有教训：用分不开的判据做删除决策 = 不可预测的误杀。）
    """
    s = str(text or "")
    if not s:
        return False
    return any(m in s for m in MemoryService._CONFLICT_MARKERS)


def _mem_cosine(ve, a: dict, b: dict) -> float | None:
    """两条记忆的相似度。真向量优先（同 embed_version），降级 bigram。

    与 `consolidate()` **完全同口径**（同一对`_cosine`/`_vector` 调用）——
    ⚠️ 这是刻意的：冲突检测说"这俩不像"（低相似），
    而 consolidate 说"这俩重复"（高相似），**两者必须用同一把尺**，
    否则会出现"同一对内容被两个引擎给出矛盾结论"。
    返回 None = 算不出（调用方据此跳过，不硬判）。
    """
    import json as _j
    import math as _m
    try:
        va = (a or {}).get("embedding") or ""
        vb = (b or {}).get("embedding") or ""
        ea, eb = (a or {}).get("embed_version"), (b or {}).get("embed_version")
        if ea and eb and ea == eb and va and vb:
            xa = _j.loads(va) if isinstance(va, str) else list(va)
            xb = _j.loads(vb) if isinstance(vb, str) else list(vb)
            if xa and len(xa) == len(xb):
                qa = _m.sqrt(sum(x * x for x in xa)) or 1.0
                qb = _m.sqrt(sum(x * x for x in xb)) or 1.0
                return sum(x * y for x, y in zip(xa, xb)) / (qa * qb)
        if ve is None:
            from knowledge_engine import VectorEngine as _VE
            ve = _VE()
        return ve._cosine(ve._vector((a or {}).get("content") or ""),
                          ve._vector((b or {}).get("content") or ""))
    except Exception:
        return None
