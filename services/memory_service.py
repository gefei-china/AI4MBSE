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
import logging
import math
import re
import time

logger = logging.getLogger("mbse.memory")

from services.knowledge_engine import VectorEngine


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
    def _has_verify_col(conn) -> bool:
        """P0-3（2026-11-11）：是否有 verify_state 列（离线 Dreaming 的事实校验结果）。

        与 `_has_tier_col` 同款守卫：老库/夹具缺列时**不拼**该条件，逐字回到旧召回。
        """
        try:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(agent_memory)").fetchall()]
            return "verify_state" in cols
        except Exception:
            return False

    @staticmethod
    def _no_contradicted_clause(conn) -> str:
        """被流水证伪的笔记一律不注入（错误记忆级联的第一道闸）。

        `COALESCE(...,'unverifiable')` 是必需的：存量行 NULL 表示"还没验过"，
        不是 contradicted，不能因为加了一列就静默丢掉 281 条历史记忆。
        """
        return (" AND COALESCE(verify_state,'unverifiable')!='contradicted'"
                if MemoryService._has_verify_col(conn) else "")

    @staticmethod
    def _has_superseded_col(conn) -> bool:
        """探测 agent_memory 是否已有 superseded_by 列（D1 冲突消解；缺列 → False，逐字回到旧召回）。

        ⚠️ 与 `_has_tier_col` 同一套守卫思路：**老库未跑迁移时不能拼该条件**，
        否则 `no such column: superseded_by` 会让检索主链路整体报错。
        """
        try:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(agent_memory)").fetchall()]
            return "superseded_by" in cols
        except Exception:
            return False

    #: 存量行（scope_type=''）可见性策略的合法取值。见 `_legacy_policy`。
    LEGACY_POLICIES = ("visible", "windowed", "hidden")

    @staticmethod
    def _legacy_policy() -> tuple:
        """读 `memory.legacy_policy` / `memory.legacy_cut_ts` → (policy, cut_ts)。

        ★ 为什么默认 visible：本工程 `agent_memory` 有大量 `scope_type=''` 的存量/旁路行
          （历史遗留 + 画布节点 + 人工 API 不传 scope），改动前它们**无条件**按 agent_id 兜底可见。
          默认改成 hidden 会一次性清空这部分召回 ⇒ 属行为漂移，必须由运维显式切换。
        ★ 未知取值一律回落 visible（fail-safe）：配置写错时宁可保持旧行为，
          也不能因一个拼写错误让召回静默变窄——那种故障在监控上看不出（召回变少 ≠ 报错）。
        """
        try:
            from core import config as _cfg
            p = str(_cfg.get("memory", "legacy_policy", "visible") or "visible").strip().lower()
            cut = str(_cfg.get("memory", "legacy_cut_ts", "") or "").strip()
        except Exception:
            return "visible", ""
        if p not in MemoryService.LEGACY_POLICIES:
            return "visible", ""
        return p, cut

    # ── P2 事件埋点（2026-11-27）──────────────────────────────────────────
    #: 事件类型白名单。**故意收窄**：只记"生命周期改变"，不记读与命中
    #: （record_access 每次召回都会触发，记进事件表会把周报淹掉 —— 那是访问日志的活）。
    EVENT_KINDS = ("created", "dedup_hit", "archived", "forgotten", "revived",
                   "promote", "demote", "merged", "superseded")

    @staticmethod
    def record_event(conn, kind: str, memory_id: int = 0, agent_id: str = "",
                     detail: str = "") -> None:
        """写一条生命周期事件（append-only）。失败**绝不抛出** —— 埋点不能挡住维护主链路。

        ⚠️ 表不存在（老库未迁移 / 单表夹具）时静默跳过：本仓纪律是
        "新依赖必须带缺表兼容"，否则夹具库会在第一次维护时抛异常，
        被外层 `except` 吞成"维护返回 0"（看起来像"没什么可维护"）。
        """
        if kind not in MemoryService.EVENT_KINDS:
            return
        try:
            conn.execute(
                "INSERT INTO memory_events (kind, memory_id, agent_id, detail) VALUES (?,?,?,?)",
                (kind, int(memory_id or 0), str(agent_id or "")[:60], str(detail or "")[:200]))
        except Exception as exc:  # noqa: BLE001
            # 埋点失败不得挡住维护/写入，但**必须留痕**：否则周报的"tier 流动"会静默变 0，
            # 而看起来像"这周没有任何流动"（本仓"静默兜底看起来像真的值"同族）。
            logger.warning("memory_events 写入失败（kind=%s）：%s: %s",
                           kind, type(exc).__name__, str(exc)[:120])

    # ── P1-7 双时态（2026-11-27）──────────────────────────────────────────
    @staticmethod
    def _has_validity_col(conn) -> bool:
        """探测 agent_memory 是否已有 valid_from/valid_to（老库/夹具缺列 → False，逐字回到旧召回）。

        与 `_has_tier_col` / `_has_superseded_col` 同一套守卫纪律：
        **缺列时绝不拼该条件**，否则 `no such column` 会让检索主链路整体报错。
        """
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(agent_memory)").fetchall()}
            return "valid_from" in cols and "valid_to" in cols
        except Exception:
            return False

    @staticmethod
    def _valid_now_clause(conn) -> str:
        """"当前有效"过滤片段：`valid_to` 为空 = 未被取代/未过期。

        ⚠️ 为什么仍要这一条（库里已有 `superseded_by` 排除）：两列语义**不重叠** ——
        `superseded_by` 记"被谁取代"，`valid_to` 记"何时失效"。人工/运维可能只关有效期
        而不改取代链（例如按时间到期的口径），只查一列会漏。缺列 ⇒ 空串（零行为漂移）。
        """
        return (" AND (valid_to IS NULL OR TRIM(valid_to)='')"
                if MemoryService._has_validity_col(conn) else "")

    @staticmethod
    def _archive_mode(conn) -> bool:
        """遗忘判据的落点：True=降冷层（tier='archival'，forgotten 保持 0）；False=硬遗忘（forgotten=1）。

        P1-6（2026-11-27）。为什么默认归档：`forgotten` 一个字段同时承担了「不再召回」与
        「这条没用了」两种含义 ⇒ 冷层既不可检索、也不能被 dream 复活。拆开后**召回口径不变**
        （archival 与 forgotten 一样不进 `search`），但冷层可被
        `search(include_archival=True)` 检索、可被 dream 复活、可人工回捞。
        配置 `memory.forget_archives`（默认 True）；**无 tier 列**的老库/夹具强制 False（零漂移）。
        """
        if not MemoryService._has_tier_col(conn):
            return False
        try:
            from core import config as _cfg
            return bool(_cfg.get("memory", "forget_archives", True))
        except Exception:
            return True

    @staticmethod
    def near_duplicate(conn, agent_id: str, mem_topic: str, vec, ver: str,
                       threshold: float = 0.95, exclude_id: int = 0) -> dict:
        """写前近重复探测（P0-3，2026-11-27）：返回 `{id, sim, tier, reason}` 或 `{}`。

        判据（对齐方案 P0-3「同 agent + 同 topic，余弦 ≥0.95 ⇒ 只记访问不新增」）：
          · 同 `agent_id`；`mem_topic` 非空时**要求同 topic**（空 topic 视为未知 ⇒ 不限 topic，
            因为"空==空"会把全库互不相关的条都算候选，而 0.95 已足够严）；
          · 同 `embed_version` 且都有向量（不同版本的向量**物理不可比**，这是既有设计）；
          · `forgotten=0`，但**故意包含 archival**：冷层里已有同一条事实时，正确动作是
            复活（tier 回 recall），不是再写一条新的 —— 否则冷层会成为"重复的坟墓"。
        无真向量（vec 为空/无 ver）⇒ 直接返回 {}（bigram 口径 0.95 不成立，宁可不判）。

        ⚠️ 这是**写入闸**，不是清理器：命中时**只**记访问 + 复活，**不改 content**
        （与 `apply_candidates` 的"只改状态"同一条纪律：影子语义、可回退）。
        """
        if not vec or not ver or not agent_id:
            return {}
        try:
            where = ["agent_id=?", "forgotten=0", "embed_version=?",
                     "COALESCE(embedding,'')<>''", "COALESCE(embedding,'')<>'[]'"]
            params: list = [agent_id, ver]
            if str(mem_topic or "").strip():
                where.append("mem_topic=?")
                params.append(str(mem_topic).strip())
            if exclude_id:
                where.append("id<>?")
                params.append(int(exclude_id))
            cols = "id, content, tier, embedding, embed_version, mem_topic"
            rows = conn.execute(
                "SELECT " + cols + " FROM agent_memory WHERE " + " AND ".join(where)
                + " ORDER BY id DESC LIMIT 400", params).fetchall()
        except Exception as exc:  # noqa: BLE001
            # 探测失败 ⇒ 不拦写入（宁可有重复，不可丢沉淀），但要让"去重闸坏了"可诊断
            logger.warning("近重复探测失败（本次不拦写）：%s: %s",
                           type(exc).__name__, str(exc)[:120])
            return {}
        qn = math.sqrt(sum(x * x for x in vec)) or 1.0
        best, best_s = {}, 0.0
        for r in rows:
            try:
                sv = json.loads(r["embedding"]) if isinstance(r["embedding"], str) else (r["embedding"] or [])
            except Exception as exc:  # noqa: BLE001
                logger.warning("id=%s 的 embedding 解析失败（跳过该行）：%s",
                               r.get("id"), type(exc).__name__)
                continue
            if not sv or len(sv) != len(vec):
                continue
            vn = math.sqrt(sum(y * y for y in sv)) or 1.0
            s = sum(a * b for a, b in zip(vec, sv)) / (qn * vn)
            if s > best_s:
                best_s, best = s, dict(r)
        if best and best_s >= float(threshold):
            return {"id": int(best["id"]), "sim": round(best_s, 4),
                    "tier": (best.get("tier") or ""), "reason": "near_duplicate"}
        return {}

    @staticmethod
    def set_tier(conn, memory_id: int, tier: str) -> bool:
        """分层迁移（`core` / `recall` / `archival`）—— **只改层，不改 content**。

        与 `revive_memory` 的分工：`set_tier` 是通用搬层（dream 的 promote/demote 用），
        `revive_memory` 是"冷层回活跃"（同时清 forgotten 与 valid_to，语义更强）。
        非法层级 / 无 tier 列 / 行不存在 ⇒ False（不静默假装成功）。
        """
        if tier not in ("core", "recall", "archival"):
            return False
        if not MemoryService._has_tier_col(conn):
            return False
        try:
            if tier == "archival":
                try:
                    cur = conn.execute(
                        "UPDATE agent_memory SET tier=?, archived_at=datetime('now','localtime') "
                        "WHERE id=?", (tier, int(memory_id)))
                except Exception:
                    cur = conn.execute("UPDATE agent_memory SET tier=? WHERE id=?",
                                       (tier, int(memory_id)))
            else:
                cur = conn.execute("UPDATE agent_memory SET tier=? WHERE id=?",
                                   (tier, int(memory_id)))
            conn.commit()
            if cur.rowcount:
                MemoryService.record_event(
                    conn,
                    "archived" if tier == "archival" else ("promote" if tier == "core" else "demote"),
                    int(memory_id), "", "->" + tier)
            return cur.rowcount > 0
        except Exception:
            return False

    @staticmethod
    def revive_memory(conn, memory_id: int) -> bool:
        """把冷层条目复活回 recall（P0-3/P1-6 共用）。

        「复活」= tier 回 recall + forgotten=0，**不改 content**。
        为什么需要：遗忘/归档是**软删**，正确的再出现信号是"这条又被写/被用到了"，
        此时应当归位而不是再插一条副本（那正是重复膨胀的来源）。
        """
        try:
            if not memory_id:
                return False
            _tc = MemoryService._has_tier_col(conn)
            if _tc:
                cur = conn.execute(
                    "UPDATE agent_memory SET tier='recall', forgotten=0, "
                    "valid_to=NULL WHERE id=?", (int(memory_id),))
            else:
                cur = conn.execute("UPDATE agent_memory SET forgotten=0 WHERE id=?",
                                   (int(memory_id),))
            conn.commit()
            if cur.rowcount:
                MemoryService.record_event(conn, "revived", int(memory_id), "", "cold->recall")
            return cur.rowcount > 0
        except Exception:
            return False

    @staticmethod
    def _scope_filter(conn, agent_id: str, scopes, legacy_policy: str = "", cut_ts: str = ""):
        """构造作用域复合过滤 → (clause, params, idx_of)。

        scopes 为**有序**槽位 [(type, id), ...]（优先级高→低），如
        [('project', pid), ('user', uname), ('agent', intent), ('global', '')]。
        存量行（scope_type=''）按 agent 槽召回 —— 保证老记忆不静默消失。
        返回 clause 形如 " AND ( (...=?) OR ... OR (scope_type='' AND agent_id=?) )"；
        无作用域/缺列 → ("", [], {})（调用方沿用旧 SQL）。

        legacy_policy / cut_ts：存量行槽位的可见性（2026-11-05 新增，默认走配置）。
          · "visible"  —— 同 agent 的存量行全部可见（**改动前行为，逐字不变**）
          · "windowed" —— 仅 `created_at >= cut_ts` 的存量行可见；cut_ts 空 ⇒ 等价 hidden
          · "hidden"   —— 存量行完全不出现（严格按作用域隔离）
        ⚠️ 显式传参优先（供门禁/实验用）；不传则读配置。两者都没有 ⇒ visible。
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
        # 存量行槽位（策略由 legacy_policy 决定；默认 visible = 改动前行为）
        _pol = legacy_policy if legacy_policy in MemoryService.LEGACY_POLICIES else ""
        _cut = cut_ts
        if not _pol:
            _pol, _cut = MemoryService._legacy_policy()
        if _pol == "visible":
            parts.append("(scope_type='' AND agent_id=?)")
            params.append(agent_id)
        elif _pol == "windowed" and _cut:
            parts.append("(scope_type='' AND agent_id=? AND created_at>=?)")
            params.extend([agent_id, _cut])
        # hidden，或 windowed 但未给切点 ⇒ 不拼存量行槽位（严格隔离）
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

    @staticmethod
    def _source_credibility(source: str) -> float:
        """来源可信度权重（P0#2 融合排序五维之一）。

        按 `memory.source_credibility`（逗号分隔 `key=value`）映射；未配置/解析失败/未知来源 → 1.0（中性，不漂移）。
        设计：**默认中性**。待后续按 provenance 区分来源可信度（如 push<llm_agent<rule_agent）时，
        仅改配置即可让融合排序生效，无需动代码。
        """
        try:
            from core import config as _cfg
            raw = str(_cfg.get("memory", "source_credibility", "") or "").strip()
        except Exception:
            raw = ""
        if not raw:
            return 1.0
        _map = {}
        for _pair in raw.split(","):
            if "=" not in _pair:
                continue
            _k, _v = _pair.split("=", 1)
            try:
                _map[_k.strip()] = float(_v.strip())
            except ValueError:
                pass
        return _map.get((source or "").strip(), 1.0)

    # ── 读：语义检索 ──
    @staticmethod
    def search(conn, agent_id: str, query: str = "", top_k: int = 5,
               mem_type: str = "", max_age_days: int = 90, mem_topic: str = "",
               scopes: list | None = None, any_scope: bool = False,
               include_archival: bool = False) -> list:
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
            # P1-6（2026-11-27）：`include_archival=True` 时**不排除**冷层 —— 供管理端回捞、
            #   dream 复活候选、以及"我确定看过这条但召不回来"的排查用。默认 False = 逐字旧行为。
            _tc = MemoryService._has_tier_col(conn)
            _no_arch = ("" if include_archival
                        else (" AND (tier IS NULL OR tier != 'archival')" if _tc else ""))
            # ── D1-c 召回期仲裁（2026-10-07）──
            # 被标记 superseded_by>0 的旧条**不进召回**（模型看不到已被取代的旧口径）。
            # ⚠️ 用 `COALESCE(superseded_by,0)=0` 而非 `superseded_by=0`：
            #    存量/夹具行的 NULL 必须当作"未被取代"，否则会被 NULL 比较静默漏掉。
            # ⚠️ 用列存在性守卫：老库若尚未跑迁移（无该列），**不拼这个条件**，
            #    逐字回到旧行为——不能因为缺列就让整个检索报错（与 ix_am_scope 纪律一致）。
            if MemoryService._has_superseded_col(conn):
                _base_extra = " AND COALESCE(superseded_by,0)=0"
            else:
                _base_extra = ""
            # P0-3：被流水证伪的笔记不进召回。列缺失时该片段为空 ⇒ 零行为漂移。
            _base_extra += MemoryService._no_contradicted_clause(conn)
            # P1-7 双时态：有效期已关闭（valid_to 非空）的口径不再召回。
            # 存量行 valid_to 全 NULL ⇒ 本片段零行为漂移（可回滚：置空该列即恢复）。
            _base_extra += MemoryService._valid_now_clause(conn)
            if not query:
                # 作用域生效时不再用 agent_id 作唯一约束（user/global 槽需跨 Agent 复用）
                if any_scope:
                    _base = "forgotten=0" + _no_arch + _base_extra
                else:
                    _base = (("forgotten=0" + _clause + _no_arch + _base_extra) if _clause else "agent_id=? AND forgotten=0" + _no_arch + _base_extra)
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
                _base = "mem_type!='session' AND forgotten=0" + _no_arch + _base_extra
            else:
                _base = (("mem_type!='session' AND forgotten=0" + _clause + _no_arch + _base_extra) if _clause else "agent_id=? AND mem_type!='session' AND forgotten=0" + _no_arch + _base_extra)
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
                # ── P0#2 融合排序五维（2026-10-28）：语义(s) × 时效(decay) × 作用域(_boost)
                #    × 相关性(relevance) × 可信度(source_credibility) ──
                # relevance 列已存（deposit 落 extra.get('relevance',1.0)），默认 1.0 中性不漂移；
                # source_credibility 按来源映射（config `memory.source_credibility`，默认全 1.0 中性）。
                # 两者默认中性 ⇒ **零行为漂移**；待按 provenance 填真实 relevance / 配 credibility 即生效。
                rel = float(r.get("relevance") or 1.0) or 1.0
                # P0-3：Dreaming 写入的逐条可信度优先；NULL = 尚未评估 ⇒ 沿用
                # 按 source 映射的函数值（默认 1.0 中性），零行为漂移。
                _raw_cred = r.get("source_credibility")
                cred = (float(_raw_cred) if _raw_cred is not None
                        else MemoryService._source_credibility(r.get("source") or ""))
                scored.append((s * decay * _boost * rel * cred, s, r))
            scored.sort(key=lambda x: x[0], reverse=True)
            out = [{**dict(r), "score": round(_raw, 3)} for _fin, _raw, r in scored[:top_k]]
            # P2：命中记访问（激活度随使用上调，供遗忘引擎评估）
            # ★ 2026-10-28 修复「噪音霸榜自锁」（生产库实证）：
            #   此前 `record_access` **不区分命中质量**，任何被 search 命中的条目都 +1 access / +0.1 activation。
            #   实测后果（mbse.db 172 条活跃）：
            #     id=170「你好，我是 MBSE 平台的通用助手。你发送的「123」我这边没有识别到具体意图…」
            #     ⇒ `_is_noise=True`（纯平台话术），却 access=1752、activation 封顶 2.0，
            #       稳居活跃条 activation 第 1（第 2 名 843，差 2 倍以上）。
            #   形成闭环：它被命中 → 使用证据上涨 → activation 更高 → 更容易再被命中（正反馈）；
            #   同时 `access_count>forget_min_access` 使其**永不满足遗忘条件** ⇒ 一条噪音被永久保留。
            #   修法：记账前先过**与下游同口径**的噪音闸门（`memory_recall._is_noise`），
            #   噪音命中**不计入使用证据**。
            #   ⚠️ 这是**纯统计修复，零排序/注入漂移**：闸门不影响本轮 `out`（排序已定），
            #     而下游（`recall_memories._collect` / `_build_memory_hint`）本就会丢弃噪音。
            #     收益：噪音不再自我强化，并回归「可被遗忘引擎正常淘汰」的状态。
            try:
                from agent.memory_recall import _is_noise as _access_noise_gate
            except Exception:
                def _access_noise_gate(_t):
                    return False
            for it in out[:3]:
                if _access_noise_gate(str(it.get("content") or "")):
                    continue
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
            # P0-3：core 是**无条件注入**，一条被证伪的笔记会污染每一个后续会话
            # （Anthropic 原文明确的 injected-context 风险），故这里同样排除。
            _base += MemoryService._no_contradicted_clause(conn)
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
            _vc = MemoryService._has_validity_col(conn) # P1-7 双时态（缺列 → 不拼）
            _tier = "core" if mem_type == "preference" else "recall"
            # ── P0-3 写前近重复闸（2026-11-27）────────────────────────────────
            # 为什么放在 INSERT **之前**：写完再删/合会把 id 序列打乱，且"先脏后清"
            #   在崩溃窗口里会留下重复。写到门口就挡住最省事。
            # 命中后的动作（对齐方案验收「只 record_access 旧条、不新增」）：
            #   ① 记访问（这条被再次确认，使用证据 +1，供遗忘/分层引擎判活）；
            #   ② 若命中冷层（archival）⇒ 复活回 recall（**不是**再插一条新副本）。
            # 返回既有 id 而不是 None：调用方据此知道"这次沉淀落到哪条"，
            #   与"内容为空/被安全扫描拦下"（返回 None）是两回事，不能混。
            _dd_on, _dd_sim = True, 0.95
            try:
                from core import config as _cfg_dd
                _dd_on = bool(_cfg_dd.get("memory", "dedup_enabled", True))
                _dd_sim = float(_cfg_dd.get("memory", "dedup_sim", 0.95) or 0.95)
            except Exception:
                pass
            if _dd_on:
                _dup = MemoryService.near_duplicate(conn, agent_id, mem_topic, vec, ver, _dd_sim)
                if _dup:
                    MemoryService.record_access(conn, _dup["id"])
                    MemoryService.record_event(conn, "dedup_hit", _dup["id"], agent_id,
                                               "sim=%.4f topic=%s" % (_dup.get("sim") or 0, mem_topic))
                    if str(_dup.get("tier") or "") == "archival":
                        MemoryService.revive_memory(conn, _dup["id"])
                    return _dup["id"]
            cur = conn.execute(
                "INSERT INTO agent_memory (agent_id, mem_type, content, embedding, embed_version, source, relevance, activation, mem_topic"
                + (", tier" if _tc else "")
                + (", scope_type, scope_id" if _sc else "")
                + (", valid_from" if _vc else "") + ") "
                "VALUES (?,?,?,?,?,?,?,1.0,?"
                + (",?" if _tc else "")
                + (",?,?" if _sc else "")
                + (", datetime('now')" if _vc else "") + ")",
                (agent_id, mem_type, c,
                 json.dumps(vec, ensure_ascii=False) if vec else "[]",
                 ver if vec else "",
                 source, extra.get("relevance", 1.0), mem_topic)
                + ((_tier,) if _tc else ())
                + ((st, sid) if _sc else ()))
            conn.commit()
            MemoryService.record_event(conn, "created", int(cur.lastrowid or 0), agent_id,
                                       "type=%s topic=%s" % (mem_type, mem_topic))
            # ── P1-7 写时"口径取代"（2026-11-27）───────────────────────────────
            # 与近重复闸的分工：≥0.95 = **同一条事实的另一种说法**（去重）；
            # 0.85~0.95 且**新内容自身宣告"旧的不作数"** = **新旧口径**（取代）。
            # 判据方向是实测定下的：互斥对字面高度重叠（实测 0.7252 bigram / 真向量重复区 0.94+），
            #   所以只能靠 ①相似 ②互斥词 两条同时成立来分，语义"不像"反而漏判（见 detect_conflicts）。
            # ⚠️ 只在**新内容含互斥断言词**时取代：那是说话者在宣告"旧口径已废"，
            #   是本条路径最窄、最可辩护的信号；仅"相似"一律不动（避免把互补经验挤掉）。
            _sp_on, _sp_sim = True, 0.85
            try:
                from core import config as _cfg_sp
                _sp_on = bool(_cfg_sp.get("memory", "supersede_on_deposit", True))
                _sp_sim = float(_cfg_sp.get("memory", "supersede_sim", 0.85) or 0.85)
            except Exception:
                pass
            if _sp_on and vec and ver and _has_conflict_marker(c):
                _old = MemoryService.near_duplicate(
                    conn, agent_id, mem_topic, vec, ver, _sp_sim,
                    exclude_id=int(cur.lastrowid or 0))
                if _old:
                    MemoryService.mark_superseded(conn, _old["id"], int(cur.lastrowid))
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
               max_unused_days: int = 0, min_access: int = 0,
               max_unused_access: int | None = None) -> int:
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

        ★★ 2026-11-27（真机预演推翻原判据）：判据 1 必须带**价值维度**，不能只看时间。
        实测（生产库 275 条活跃，`max_unused_days=30` 预演）会归档 28 条，其中：
          · id=278「链路需求分析」**access=69**、id=317「平台查询排障」**access=62**、
            id=380（access=49）、id=279（access=42）、id=122（access=40）、id=384（access=35）…
          · 28 条里 **25 条有访问记录**，只有 3 条零访问 —— 它们不是"陈旧"，是"最近一个月
            没问到相关任务"。
        ⇒ 纯时间判据在这里**会误伤最值钱的记忆**（高频经验恰是旧条目，因为新条目还没被用起来）。
        这不是"阈值调大一点"能解决的：60/90 天档预演都是 **0 条**，说明库里根本没有真正的陈旧数据，
        唯一能动的就是"久未用 + 低频"这一档。
        ⇒ 判据 1 改为 `age_days >= max_unused_days AND access_count <= max_unused_access`
          （后者默认读 `memory.forget_max_access`，默认 2；传 -1/None 以外的负数表示"不限制"，
           即回滚到只看时间的旧行为）。
        ⚠️ **默认仍零行为漂移**：`max_unused_days` 默认 0 ⇒ 判据 1 不启用，冷层保持 0；
          启用时必须同时给 `forget_max_access`，否则等于恢复"按日期轮换"。

        参数：
          max_unused_days   —— 主判据：多少天未被访问；**0/None = 该判据不启用**（防误配全删）
          max_unused_access —— 主判据的价值维度：仅当 `access_count <= 此值` 才降层。
                               `None` → 读配置 `memory.forget_max_access`（默认 2）；
                               `-1` → 不限制（回滚到"只看时间"的旧行为）
          min_access        —— 低价值判据的访问次数上限（默认 0 = 从未访问过）
          max_age_days      —— 低价值判据的年龄门槛（沿用原参数名，语义扩展为"陈旧门槛"）

        返回遗忘条数。异常返回 0（不阻断）。
        """
        import time as _t
        try:
            where = "WHERE forgotten=0"
            params: list = []
            # ★★ 2026-11-27 实测缺陷：解耦后归档行的 `forgotten` **就是 0**，
            #   而这里只按 forgotten=0 选 ⇒ **已经是冷层的行每跑一次就被"再归档"一次**。
            #   实测：同一批行连跑两次 forget，每次都新增 2 条 archived 事件
            #   （事件表 7→9→11），周报的「本周归档数」被自己刷成假数。
            #   6h 调度下这就等于每天 4 次、每次若干条 —— 一上线周报立刻失真。
            #   ⇒ 必须排除既已在冷层的行（冷层是终态，直到被 revive）。
            if MemoryService._has_tier_col(conn):
                where += " AND (tier IS NULL OR tier != 'archival')"
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
            # 判据 1 的价值维度：未显式传参时读配置（默认 2 = 只降"几乎没人用"的）
            if max_unused_access is None:
                try:
                    from core import config as _cfg2
                    max_unused_access = int(_cfg2.get("memory", "forget_max_access", 2))
                except Exception:
                    max_unused_access = 2
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
                # 判据 1（主）：久未访问 **且** 低频使用（2026-11-27 加价值维度）
                # ⚠️ 只看时间会误伤高频旧经验（实测 id=278 access=69 也会被判冷层）。
                if (max_unused_days and max_unused_days > 0 and age_days >= max_unused_days
                        and (max_unused_access is None or max_unused_access < 0
                             or _access <= int(max_unused_access))):
                    reason = f"unused_{int(age_days)}d_access{_access}"
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
                    # P2（2026-11-27）事件埋点：周报的"tier 流动"只能靠这里，
                    #   `agent_memory` 只存当前状态，答不了"本周降了几条、复活几条"。
                    MemoryService.record_event(
                        conn, "archived" if MemoryService._archive_mode(conn) else "forgotten",
                        r["id"], "", reason)
                    # ── P1-6（2026-11-27）遗忘与归档**解耦** ────────────────────
                    # 旧实现两句连着写（forgotten=1 + tier='archival'）⇒ 一个字段同时承担
                    #   「不再召回」和「这条没用了」，冷层因此既不可检索、也不能被复活，
                    #   "曾有用但当前不相关"的经验只能永久消失。
                    # 现在默认只降层（tier='archival'，forgotten 保持 0）：
                    #   召回口径**不变**（search 本来就排除 archival），
                    #   但冷层可被 `search(include_archival=True)` 检索、可被 dream 复活、可人工回捞。
                    if MemoryService._archive_mode(conn):
                        # archived_at 是 P1-6 新列：夹具/老库可能没有 ⇒ 退回"只降层"，
                        # 但**不得**因为一次 UPDATE 失败就把这条静默漏掉（计数照记）。
                        try:
                            conn.execute(
                                "UPDATE agent_memory SET tier='archival', "
                                "archived_at=datetime('now','localtime') WHERE id=?", (r["id"],))
                        except Exception:
                            conn.execute("UPDATE agent_memory SET tier='archival' WHERE id=?",
                                         (r["id"],))
                    else:
                        # 硬遗忘档（`memory.forget_archives=False` 回滚用）：保持旧语义
                        # —— forgotten=1 即退出召回，tier 同步标 archival 仅为台账一致。
                        conn.execute("UPDATE agent_memory SET forgotten=1 WHERE id=?", (r["id"],))
                        if MemoryService._has_tier_col(conn):
                            try:
                                conn.execute("UPDATE agent_memory SET tier='archival' WHERE id=?", (r["id"],))
                            except Exception:
                                pass
                    n += 1
            conn.commit()
            return n
        except Exception as exc:  # noqa: BLE001
            # ★ 2026-11-27 实测踩到：这里原先**静默 return 0**，于是任何内部异常
            #   （我自己的探针把 row_factory 设成 None ⇒ `r["last_accessed_at"]` 抛 TypeError）
            #   都表现为"降冷层 0 条"，与"库里确实没有该降的"**完全同形**。
            #   这与 consolidate 那次"静默 return 0 把缺列伪装成没东西可合并"是同一型。
            #   ⇒ 保留不抛（维护不能因一条脏数据整体失败），但必须留痕。
            logger.warning("forget 执行异常（本次降冷层未生效，**不是**没有可降的）：%s: %s",
                           type(exc).__name__, str(exc)[:160])
            return 0

    # ── P2：合并引擎——内容相似度高且同类型 → 保留较新，旧标记遗忘（防重复膨胀）──
    @staticmethod
    def consolidate(conn, agent_id: str = "", threshold: float = 0.85) -> int:
        """合并相似记忆，返回合并条数。

        P1b-3 修正：真向量优先（复用存储 embedding，同版本余弦，阈值 0.85 对齐 Mem0 语义合并）；
        bigram 降级时用 0.5（bigram 稀疏余弦对改写句普遍 0.4-0.65，原 0.85 导致合并从不触发）。

        ⚠️⚠️ **2026-11-27（P0-2 副产物）修正：真向量口径的阈值不能沿用 0.85。**
        起因：把 `run_maintenance` 接进 6h 作业后，在**生产库副本**上实跑一次维护 ——
        `merged=165`（275 条活跃 → 只剩 110）。60% 的记忆被当"重复"软删。
        取证与定标（同一副本，真向量 1024 维）：
          · ≥0.95 的近重复 **13 对**、0.90~0.95 **25 对**、0.85~0.90 还有上百对；
          · 也就是说 0.85 在**真向量**口径下根本不是"重复"，而是"同一领域的两件事"。
        定标依据（既有实测表，见 `detect_conflicts` docstring）：
        **重复区在真向量下是 0.94~0.9986** ⇒ 阈值取 `memory.consolidate_dense_sim`（默认 0.94）。
        bigram 路仍用 0.5（那是另一套量纲，已按 bigram 标定，别混用）。
        ⇒ **两路各自用自己标定过的阈值**，这才是"同口径"的真正含义。

        另：合并动作从 `forgotten=1`（软删）改为 `mark_superseded`（有取代链）。
        两者对召回等效（都被排除），但软删后 `restore` 只能整条复活、无法回答"被谁取代"；
        而 `superseded_by` 是**可回溯、可回退**的影子语义（与 dream merge 同一条纪律）。
        """
        try:
            where = "WHERE forgotten=0"
            params: list = []
            # P1-6（2026-11-27）：冷层不参与合并。
            # ⚠️ 必须显式排除：归档行的 forgotten 现在是 0（解耦后），若仍按旧判据取全量，
            #   consolidate 会把冷层当"重复"再打一次 forgotten=1 ⇒ 冷层被**二次清除**，
            #   与"可被 dream 复活"直接矛盾（"新口径只改一半"最容易漏）。
            if MemoryService._has_tier_col(conn):
                where += " AND (tier IS NULL OR tier != 'archival')"
            # ★★ 2026-11-27 实测缺陷（第二次真机验证）：**没有排除已被取代的行** ⇒
            #   同一批 12 对每跑一次就被"再合并"一次。后果比"多写几条事件"严重得多：
            #     · `mark_superseded` 会再执行 `valid_to=datetime('now')` ⇒
            #       **把"这条何时失效"一路往后推**，双时态（P1-7）的时间线被自己的维护任务篡改；
            #     · `memory_events` 每次 +24 条 ⇒ 周报「本周合并数」翻倍、且随调度频率线性膨胀；
            #     · 6h 调度下一天 4 次，一周就是 672 条假事件把真流动淹掉。
            #   实测取证：连跑两次 maintain，事件 36 → 84，merged 恒为 12。
            #   ⇒ 合并的输入面必须与「召回面」同口径：已被取代的不再参与（与 detect_conflicts 一致）。
            if MemoryService._has_superseded_col(conn):
                where += " AND COALESCE(superseded_by,0)=0"
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
            # ── 阈值：两路各用自己标定过的量纲（2026-11-27 修正）──
            # ⚠️ 原实现只有一个 `eff_threshold` 变量，并在 bigram 分支里**就地改成 0.5**：
            #   `if not used_dense: eff_threshold = 0.5` —— 这个赋值**会一直生效到后续所有对**，
            #   于是只要库里有一条没有真向量的行（实测 13 条）参与过比较，
            #   之后**所有真向量对都按 0.5 合并** ⇒ 生产库副本上 275 条活跃被合并 165 条（60%）。
            #   ⇒ 阈值必须**逐对计算**，不能跨迭代复用（与"判据要用自己构造的基准"同源）。
            _bigram_thr = 0.5          # bigram 口径（稀疏余弦，已标定）
            _dense_thr = 0.94
            try:
                from core import config as _cfg_c
                _dense_thr = float(_cfg_c.get("memory", "consolidate_dense_sim", 0.94) or 0.94)
            except Exception:
                _dense_thr = 0.94
            ve = VectorEngine()
            _has_sup = MemoryService._has_superseded_col(conn)
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
                    thr = _dense_thr if used_dense else _bigram_thr
                    if s >= thr:
                        # 保留较新（id 大者），旧条打"被取代"标记（**不软删、不改正文**）
                        _old = a["id"] if b["id"] > a["id"] else b["id"]
                        _new = b["id"] if b["id"] > a["id"] else a["id"]
                        if _has_sup:
                            if MemoryService.mark_superseded(conn, _old, _new):
                                MemoryService.record_event(conn, "merged", _old, "",
                                                           "into=%d sim=%.3f" % (_new, s))
                                merged += 1
                        else:
                            conn.execute("UPDATE agent_memory SET forgotten=1 WHERE id=?", (_old,))
                            merged += 1
                        if b["id"] > a["id"]:
                            break
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
        ② **相似度 ≥ `sim_high`**（默认 0.5）**且任一方含互斥断言词**。

        ⚠️⚠️ **两处与实施计划 §5 D1 相反的地方，都是实测推翻的（2026-10-07）**：

        **(1) 判据②的方向是「相似度高」，计划书写的「低相似」是错的。**
        计划书假设"互斥事实讲法不同⇒ 字面不像"。实测（bigram 口径）：
        「vc 格式为 A,B」vs「vc 格式**不再是** A,B，已废弃」相似度 **0.7252**——
        **互斥事实改的正是那几个关键词，两条必然字面高度重叠。**
        ⇒ 「相似度低」与判据①「同键」自相矛盾，会把真实互斥对全部漏判。

        **(2) 但「0.5」这个阈值本身在真向量口径下未标定（诚实标注，别当实测值引用）。**
        上面的 0.7252 是 **bigram 降级口径**。生产库实测：
        - 真向量**已生效**（144 行里 129 行 `embed_version='openai-compat'`）；
        - 真向量下同键 33 对，相似度**全在 0.85+**（重复区 0.9428~0.9986）；
        - **其中没有一对是互斥的** ⇒ 真实互斥的相似度档位**在生产数据里还不存在样本**。
        ⇒ 0.5 是"用降级夹具定的、能跑"的默认值，**不是标定值**。
        真实阈值应等生产出现第一个互斥对后重标（开 `memory.conflict_detect_enabled` 观察）。

        **与 `consolidate()` 的分工（真向量口径下实测清晰）**：
        | 内容关系 | 真向量相似度 | 归谁管 |
        |---|---|---|
        | 重复（同一结论两种措辞） | 0.94~0.9986 | `consolidate()`（阈值 0.85）|
        | **互斥**（新口径否定旧口径） | **无样本** | 本函数 |
        ⇒ 两引擎在真向量口径下**不重叠**；只有在 **bigram 降级**时才会重叠
        （降级阈值 0.5 < 互斥对 0.7252）—— 那种情况下consolidate 会用
        `forgotten=1` 把互斥对一起软删掉（本函数标记 `superseded_by` 更保守）。
        """
        try:
            where = ["forgotten=0", "COALESCE(superseded_by,0)=0"]
            params: list = []
            # P1-6（2026-11-27）：冷层不参与冲突检测 —— 冷层与活跃层"同键+高相似"是常态
            #   （同一事实被归档前就有多个措辞），把它算进冲突会把活跃条误标 superseded。
            if MemoryService._has_tier_col(conn):
                where.append("(tier IS NULL OR tier != 'archival')")
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
                from services.knowledge_engine import VectorEngine
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
    def mark_verify_state(conn, memory_id: int, state: str,
                          evidence_ref: str = "", note: str = "",
                          credibility: float | None = None) -> bool:
        """P0-3：写一条笔记的事实校验结果（三态 + 证据引用）。

        只由**离线 Dreaming 候选包经 HIL 批准**或人工调用；本函数不会被任何
        在线链路自动触发（防止"LLM 评 LLM 再自动喂回"的自我强化循环）。
        state ∈ verified | contradicted | unverifiable | needs_review；非法值直接拒绝。
        `needs_review`（2026-11-11 加）：规则法判不明但**确有邻近否定**的中间态 ——
        它**不参与**召回排除（只有 contradicted 排除），只排队等人工看。
        `evidence_ref` 指向 transcript_fts 的 (kind, ref_id)，如 `trace#1215`。
        """
        if state not in ("verified", "contradicted", "unverifiable", "needs_review"):
            return False
        if not MemoryService._has_verify_col(conn):
            return False
        try:
            conn.execute(
                "UPDATE agent_memory SET verify_state=?, evidence_ref=?, evidence_note=?, "
                "source_credibility=COALESCE(?, source_credibility) WHERE id=?",
                (state, evidence_ref or "", note or "", credibility, int(memory_id)))
            conn.commit()
            return True
        except Exception:
            return False

    @staticmethod
    def _supersede_sql(has_valid_to: bool) -> str:
        """取代语句的唯一构造点（供 `mark_superseded` 与门禁变异共用）。

        抽成独立函数的原因很实际：门禁 `verify_memory_conflict_gate` 的 M3 变异
        （把"影子标记"改成"真删"）要能**稳定命中**这一句。此前 SQL 直接内联在
        `mark_superseded` 里，加了一列（valid_to）就把锚点打断 —— 门禁会以
        "变异锚点未命中"报红，看起来像实现坏了，实际是判据耦合了文本形态。
        """
        return ("UPDATE agent_memory SET superseded_by=?, superseded_at=datetime('now','localtime')"
                + (", valid_to=datetime('now')" if has_valid_to else "")
                + " WHERE id=? AND forgotten=0")

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
            # P1-7 双时态：取代同时**关闭旧条有效期**（valid_to，UTC 口径，与 valid_from 同源）。
            # ⚠️ 只写标记、不改 content —— 影子语义（可回退、可审计），与既有纪律一致。
            _vc = MemoryService._has_validity_col(conn)
            cur = conn.execute(MemoryService._supersede_sql(_vc),
                               (int(new_id), int(old_id)))
            conn.commit()
            if cur.rowcount:
                MemoryService.record_event(conn, "superseded", int(old_id), "",
                                           "by=%d" % int(new_id))
            return cur.rowcount > 0
        except Exception:
            return False

    # ── P2：周期维护入口（遗忘 + 合并，幂等）──
    @staticmethod
    def maintain(conn, agent_id: str = "") -> dict:
        """记忆维护：遗忘 + 合并 + 冲突标记。返回 {"forgotten": n, "merged": m, "conflicts": c}。

        ⚠️ 2026-09-29：遗忘判据已修（见 `forget` 的 docstring —— 原公式结构性永不触发）。
        新增两个配置：`memory.max_unused_days`（主判据，默认 0=不启用时效遗忘）、
        `memory.forget_min_access`（低价值判据的访问次数上限，默认 0）。
        ⚠️ 默认值刻意**保守**（时效遗忘默认关）：修完根因不等于要立刻批量清库 ——
        先把"能遗忘"的能力接上，阈值由配置逐步放开，避免旧库一次性被清空。
        """
        from core import config as _cfg
        forget_n = 0
        merge_n = 0
        conflict_n = 0
        if _cfg.as_bool("memory", "forget_enabled", True):
            forget_n = MemoryService.forget(
                conn, agent_id,
                threshold=float(_cfg.get("memory", "forget_threshold", 0.2)),
                max_age_days=int(_cfg.get("memory", "max_age_days", 90)),
                max_unused_days=int(_cfg.get("memory", "max_unused_days", 0)),
                min_access=int(_cfg.get("memory", "forget_min_access", 0)),
                max_unused_access=int(_cfg.get("memory", "forget_max_access", 2)))
        # ── D1 冲突消解接线（2026-10-07）──
        # ⚠️⚠️ **顺序是实质性的：必须在 consolidate 之前跑**（2026-10-07 实测踩到）。
        #  `consolidate()` 在 **bigram 降级口径**下阈值降到 0.5，而互斥对的相似度
        #  实测 0.7252 ⇒ **consolidate 会把互斥对当"重复"用 forgotten=1 软删掉**。
        #  若冲突检测在其后跑，`detect_conflicts` 的 `WHERE forgotten=0`
        #  已把旧条排除 ⇒ 恒返回 0 组（实测：merged=1 / conflicts=0）。
        #  ⇒ 顺序颠倒会让整个冲突通道**静默失效**，且返回值上看不出异常。
        #  标记后（superseded_by>0）再走 consolidate：被取代的旧条相似度虽高，
        #  但 consolidate 不看该列 ⇒ 可能重复标记 —— 故 detect 放在前面已足够，
        #  且幂等（第二次跑时旧条已被 superseded 排除）。
        # ⚠️ **默认关**（`memory.conflict_detect_enabled`）：它会**改数据**（标superseded_by），
        #   属线上不可逆操作，必须显式打开。判据本身已实测零误伤（verify gate D5）。
        if _cfg.as_bool("memory", "conflict_detect_enabled", False):
            try:
                _groups = MemoryService.detect_conflicts(
                    conn, agent_id,
                    sim_high=float(_cfg.get("memory", "conflict_detect_sim_high", 0.5)))
                for _g in _groups:
                    # 保留较新（id 大者）——与 consolidate 的"保留较新"口径一致
                    _old, _new = min(_g), max(_g)
                    if MemoryService.mark_superseded(conn, _old, _new):
                        conflict_n += 1
            except Exception:
                # 冲突检测异常不得阻断遗忘/合并（它是最"可失败"的一环）
                conflict_n = 0
        merge_n = MemoryService.consolidate(conn, agent_id,
                                            threshold=float(_cfg.get("memory", "consolidate_threshold", 0.85)))
        # P1-6（2026-11-27）：把「降冷层」与「硬遗忘」分开报，否则运维看到的
        # `forgotten` 会把归档也算进去（数字看着像"删了"，实际只是降层、还能复活）。
        if MemoryService._archive_mode(conn):
            return {"forgotten": 0, "archived": forget_n,
                    "merged": merge_n, "conflicts": conflict_n}
        return {"forgotten": forget_n, "archived": 0,
                "merged": merge_n, "conflicts": conflict_n}

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

    # P2（2026-11-11）：对强模型**给目标，不给死规则**。
    # Anthropic CL-Bench 实测：给强模型下"只记失败"这类硬限制，记忆带来的增益
    # 反而下降（Opus 5 −0.234 / Opus 4.7 −0.150 / Sonnet 4.6 −0.020）；
    # 对弱模型给"先读记忆再验证假设"的方向引导却能近乎翻倍。
    # 故把原 prompt 的「以下一律 worth=false」清单改成目标式宽指引，
    # 显著性判断交回模型，事后由 Dreaming 的剪枝/降级负责收敛。
    # ⚠️ 安全底线不交给模型：提示注入/凭据由 MemoryScanner 在落库前硬拦；
    #    无 LLM 时的规则降级 `_extract_worthy()` 也保持硬门（那是兜底，不是对强模型的限制）。
    _DEPOSIT_SOFT_PROMPT = (
        "你是 MBSE 领域记忆管理员。记忆是写给**未来会话**的：写下一条规则，"
        "等于预测这个模式以后还会用到，也等于预测未来的会话可以不再复核就依赖它。\n"
        "请自行判断下面这份任务产出里，哪些内容真正值得跨会话留存"
        "（可复用的方法与口径、反直觉的坑与实证、稳定偏好、已确认的约定）。"
        "把判断依据想清楚再下结论；宁可漏记，也不要留下会被后续会话当作事实的猜测。\n"
        '只输出 JSON：{"worth": true/false, "mem_type": "fact", "memory": "一句话提炼", '
        '"mem_topic": "主题标签（4~10 字，如 建模规范/链路预算方法论）"}，'
        "记忆内容 30~100 字，不要其他文字。")
    _DEPOSIT_HARD_PROMPT = (
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
        "记忆内容 30~100 字，不要其他文字。")

    @staticmethod
    def _deposit_prompt() -> str:
        try:
            from core import config as _cfg
            soft = bool(_cfg.get("memory", "salience_soft_guidance", True))
        except Exception:
            soft = True
        return (MemoryService._DEPOSIT_SOFT_PROMPT if soft
                else MemoryService._DEPOSIT_HARD_PROMPT)

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
                [{"role": "system", "content": MemoryService._deposit_prompt()},
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
            from services.knowledge_engine import VectorEngine as _VE
            ve = _VE()
        return ve._cosine(ve._vector((a or {}).get("content") or ""),
                          ve._vector((b or {}).get("content") or ""))
    except Exception:
        return None
