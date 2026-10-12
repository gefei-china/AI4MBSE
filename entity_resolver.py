"""E-1~E-7：实体消歧与合并（行业标准四步法 + 三档阈值 + 审计可撤销）。

流水线：
  ① Blocking 分块（类型+名称前缀桶）→ ② 双判据评分（向量×0.7 + fuzzy×0.3）
  → ③ 三档决策（auto≥0.92 / flag 0.62~0.92 / new）→ ④ SAME_AS 状态机 → ⑤ 合并事务（可撤销）

对齐 Neo4j Agent Memory / Senzing / ERKG 标准。
"""
import json
import logging
import re
import uuid

from knowledge_engine import VectorEngine
# 归一化/打分/分桶单一实现已下沉 text_normalize（诊断 §4.3 P1-1 解循环依赖）；
# 本模块 re-export 保持既有调用方（staging_fuse / routers / services / verify 脚本）
# `from entity_resolver import normalize_name/fuzzy_score/normalize_full/_canopy_key` 零改动。
from text_normalize import (  # noqa: F401  (re-export 兼容)
    _ALIAS_TABLE, _SHORT_FULL_TABLE, _bigrams, _blocking_key, _canopy_key,
    fuzzy_score, normalize_full, normalize_name, _partial_ratio, _token_sort_ratio,
    strip_honorifics,
)

logger = logging.getLogger(__name__)


# ═══════════ 0. 阈值与开关（P1-1，2026-10-11） ═══════════
# 此前 0.92 / 0.62 是函数体内的裸字面量，既不走 core/config.py，也无法关闭自动合并；
# 而 staging_fuse 的同类阈值走 DB settings（0.9 / 0.7）——两处口径独立维护，容易漂移。
# 统一口径：优先读 DB settings（运行时可调），回落 core/config.py 默认值，再回落硬编码常量。

_ENTITY_MERGE_DEFAULTS = {"auto": 0.92, "flag": 0.62, "auto_merge_enabled": True}


def _entity_merge_thresholds(conn=None) -> dict:
    """读取实体归一/消歧阈值与自动合并总开关。

    返回 {auto, flag, auto_merge_enabled}。
    读取顺序：DB settings（staging_fuse，运行时可调）→ core/config.py → 内置默认。
    **任何层级异常都静默回落默认**（阈值读不到绝不能让消歧整体失效）。

    P1-1 说明：`auto_threshold`/`review_threshold` 与 staging_fuse 同名同义，
    复用同一份 DB settings 是为了消除"两处独立维护、口径漂移"的历史缺陷
    （此前 entity_resolver 用裸字面量 0.92/0.62，staging_fuse 用 0.9/0.7）。
    注意 auto > 1.0 即等价于关闭自动合并（staging_fuse 的既有语义）。
    """
    out = dict(_ENTITY_MERGE_DEFAULTS)
    # 1) core/config.py 的 entity_merge 分组（默认层：含总开关）
    #    ⚠️ 注意不能用 core.config.get_config()（无此函数），真实 API 是 get(section, key, default)。
    try:
        from core.config import get as _cfg_get  # type: ignore

        if _cfg_get("entity_merge", "auto_threshold") is not None:
            out["auto"] = float(_cfg_get("entity_merge", "auto_threshold"))
        if _cfg_get("entity_merge", "flag_threshold") is not None:
            out["flag"] = float(_cfg_get("entity_merge", "flag_threshold"))
        if _cfg_get("entity_merge", "auto_merge_enabled") is not None:
            out["auto_merge_enabled"] = bool(_cfg_get("entity_merge", "auto_merge_enabled"))
    except Exception:
        pass
    # 2) DB settings（staging_fuse，与写前融合闸同源；仅当其显式配置过才覆盖默认层）
    #    get_fuse_config 在 staging_fuse 模块且需 conn 参数——不传 conn 时整层跳过。
    if conn is not None:
        try:
            from staging_fuse import get_fuse_config  # type: ignore

            cfg = get_fuse_config(conn) or {}
            if cfg.get("auto") is not None:
                out["auto"] = float(cfg["auto"])
            if cfg.get("review") is not None:
                out["flag"] = float(cfg["review"])
            if out["auto"] > 1.0:
                out["auto_merge_enabled"] = False
        except Exception:
            pass
    # 3) 数值健壮性：flag 必须 < auto，否则决策树退化
    if out["flag"] >= out["auto"]:
        out["flag"] = min(_ENTITY_MERGE_DEFAULTS["flag"], out["auto"] - 1e-6)
    return out


def _insert_dup_candidate(conn, keep: dict, dup: dict, score: float,
                          vec_sim: float, fz_sim: float, nb_sim: float = 0.0,
                          method: str = "fuzzy", note: str = "") -> bool:
    """写一条 pending 消歧候选（幂等：已存在则不重复写）。返回 True=新写入。

    P1-1 抽出为函数：原先桶内 flag 分支与「自动合并关闭时的降级出口」需要同一份写库逻辑，
    内联复制两份必然漂移。
    """
    evidence = {
        "vec_score": round(vec_sim, 3),
        "fuzzy_score": round(fz_sim, 3),
        "nb_score": round(nb_sim, 3),
        "keep_name": keep["name"],
        "dup_name": dup["name"],
        "same_type": True,
        "branch": keep.get("branch") or "",
    }
    if note:
        evidence["note"] = note
    ex = conn.execute(
        "SELECT id FROM entity_dup_candidates WHERE keep_id=? AND dup_id=?",
        (keep["id"], dup["id"])).fetchone()
    if ex:
        return False
    conn.execute(
        "INSERT INTO entity_dup_candidates (keep_id,dup_id,entity_type,score,vec_score,fuzzy_score,method,evidence,status) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (keep["id"], dup["id"], dup.get("entity_type") or keep.get("entity_type") or "", score,
         round(vec_sim, 3), round(fz_sim, 3), method,
         json.dumps(evidence, ensure_ascii=False), "pending"))
    return True


# ═══════════ 1. 名称归一 + 字符串 fuzzy / Blocking 分块（已下沉 text_normalize） ═══════════
# normalize_name / normalize_full / fuzzy_score / _blocking_key / _canopy_key /
# _bigrams / _token_sort_ratio / _partial_ratio / strip_honorifics 的单一实现在
# text_normalize.py（诊断 §4.3 P1-1 解循环依赖），本模块顶部 re-export 保持调用方零改动。


def detect_and_save_candidates(conn) -> dict:
    """E-1/E-2：blocking 分块 → 双判据评分 → 三档决策 → 候选落库。

    返回 {candidates, auto_merged, ...}
    """
    ve = VectorEngine()
    # 1) 取所有非废弃实体
    #    P0-1 修复（2026-10-11）：SELECT 补 branch 列。此前漏查 branch 是整条链路的缺陷源头 ——
    #    下游按 branch 分组/按 branch 取数，却从没读到过 branch。
    #    对照：run_rule_preprocess 的同名查询一直含 branch（#732），两处口径相反。
    rows = conn.execute(
        "SELECT id, name, entity_type, properties, status, branch FROM entities WHERE status!='deprecated'"
    ).fetchall()
    if len(rows) < 2:
        return {"candidates": 0, "auto_merged": 0, "merge_failed": 0}

    # 2) Canopy 分块（P1-2）：同类型 + normalize_full 前 4 字桶（别名/简全称已展开，粒度更稳）
    #    P0-1 修复（2026-10-11）：bucket 键补 branch，跨分支同 id 不再同桶配对（否则必然自合并失败）。
    _BUCKET_LIMIT = 200  # 桶上限：防极端桶（空名/泛化前缀）O(n²) 爆炸，超限记 dropped
    buckets, bucket_dropped = {}, 0
    for r in rows:
        key = _canopy_key(r["entity_type"], r["name"], branch=r["branch"])
        lst = buckets.setdefault(key, [])
        if len(lst) < _BUCKET_LIMIT:
            lst.append(dict(r))
        else:
            bucket_dropped += 1
    if not buckets:
        return {"candidates": 0, "auto_merged": 0, "merge_failed": 0,
                "buckets_dropped": bucket_dropped}

    candidates, auto_merged = [], 0
    seen = set()  # (branch, keep_id, dup_id) 去重
    ve_cache = {}  # id → vector
    nb_map = _load_neighbor_sets(conn, [r["id"] for r in rows])  # P2 图邻域特征

    # P1-1（2026-10-11）：阈值与自动合并开关配置化（此前 0.92/0.62 是裸字面量，
    # 与 staging_fuse 的 DB settings 0.9/0.7 两处独立维护、口径不一致）。
    # 必须在强规则合并**之前**读取：强规则路径也要受开关约束（关=只建候选不合并）。
    thresholds = _entity_merge_thresholds(conn)
    AUTO_T = thresholds["auto"]        # ≥ 该值自动合并
    FLAG_T = thresholds["flag"]        # [flag, auto) 建候选待人工审
    AUTO_MERGE_ON = thresholds["auto_merge_enabled"]

    # 3) 强规则合并（P1-2）：normalize_full 全等 + 同类型 + **同分支** → 直接 auto-merge，免两两打分。
    #    归一完全相同的名称，双判据必然满分（等价旧 auto-merge 分支），确定性更高。
    #
    #    P0-1 修复（2026-10-11）：分组键补 branch。
    #    根因：entities 主键是复合键 (id, branch)，同一 id 可在 dev/personal/release 三分支各存一份。
    #    原分组键 (entity_type, norm_name) 不含 branch ⇒ 把「同一实体的三个分支副本」当成
    #    「三个不同实体的重复项」喂给 merge_entities；而 merge_entities 默认 branch='personal'
    #    且按 `WHERE id=? AND branch=?` 取数 ⇒ keep/dup 取不到行时返回 {"ok":False,"error":"实体不存在"}，
    #    同 id 时更早命中 `keep_id == dup_id` →「不能合并自身」。
    #    该失败是**返回值错误而非异常**，走不到 except，导致 auto_merged 被无条件累加成
    #    「122 次合并成功」而库内零变化（实测 auto_merged=122 / entity_merges 仍 8 行）。
    #    对照：本模块 run_rule_preprocess 的 by_norm 分组键**一直含 branch**（#672），此前两处口径相反。
    by_full = {}
    for r in rows:
        by_full.setdefault(
            (r["branch"], r["entity_type"], normalize_full(r["name"])), []).append(dict(r))
    merged_dups = set()
    merge_failed = 0  # P0-2：合并失败数（此前失败被静默吞掉，只留下虚高的成功计数）
    for (branch, et, nf), group in by_full.items():
        if len(group) < 2:
            continue
        keep = group[0]
        for dup in group[1:]:
            # P1-1：强规则路径同样受自动合并总开关约束。
            #   开关关闭时不合并，但**必须留候选**——否则"归一后全等"这条最确定的线索
            #   会被静默丢弃，比假合并更难排查。
            if not AUTO_MERGE_ON:
                _insert_dup_candidate(conn, keep, dup, 1.0, 1.0, 1.0,
                                      nb_sim=0.0, method="norm_exact",
                                      note="auto_merge_disabled:norm_exact")
                continue
            # P0-2：只在 merge_entities 真正返回 ok 时才计成功；失败单独计数（不再进 except 黑洞）。
            res = merge_entities(conn, keep["id"], dup["id"], operator="auto",
                                 score=1.0, method="norm_exact", branch=branch)
            if res.get("ok"):
                auto_merged += 1
                merged_dups.add(dup["id"])
            else:
                merge_failed += 1
                logger.warning("强规则合并失败 %s→%s (branch=%s): %s",
                               dup["id"], keep["id"], branch, res.get("error"))

    # 4) 桶内配对 → 双判据评分（已强规则合并的 dup 跳过）

    def _vec(doc: dict):
        if doc["id"] not in ve_cache:
            ve_cache[doc["id"]] = ve._vector(doc["name"] + " " + (doc.get("properties") or "{}"))
        return ve_cache[doc["id"]]

    # P1-1（2026-10-11）：阈值已在上方读取（强规则路径也要用）。
    for bucket_items in buckets.values():
        if len(bucket_items) < 2:
            continue
        for i in range(len(bucket_items)):
            for j in range(i + 1, len(bucket_items)):
                a, b = bucket_items[i], bucket_items[j]
                if a["id"] in merged_dups or b["id"] in merged_dups:
                    continue  # 已强规则合并（dup 已废弃），不再参与配对
                # P0-1：跨分支实体不配对（同一 id 的分支副本不是重复项）
                if a["branch"] != b["branch"]:
                    continue
                key = (a["branch"], *tuple(sorted([a["id"], b["id"]])))
                if key in seen:
                    continue
                seen.add(key)
                # 双判据 + P2 图邻域（邻居有交集时三分量加权，否则保持原双判据）
                vec_sim = ve._cosine(_vec(a), _vec(b))
                fz_sim = fuzzy_score(a["name"], b["name"])
                nb_sim = _jaccard(nb_map.get(a["id"]) or set(), nb_map.get(b["id"]) or set())
                score = round(0.55 * vec_sim + 0.25 * fz_sim + 0.20 * nb_sim, 3) if nb_sim > 0 \
                    else round(0.7 * vec_sim + 0.3 * fz_sim, 3)
                method = "both" if vec_sim > 0.3 and fz_sim > 0.2 else ("embedding" if vec_sim >= fz_sim else "fuzzy")
                # keep: 优先已评/字段多
                keep, dup = (a, b) if (a.get("status") == "reviewed" or b.get("status") != "reviewed") else (b, a)
                # --- 三档决策 ---
                if score >= AUTO_T:
                    # auto-merge（P1-1：受总开关约束；关时降级为建候选，不静默丢弃）
                    if AUTO_MERGE_ON:
                        # P0-2：只在真正返回 ok 时才计成功
                        res = merge_entities(conn, keep["id"], dup["id"], operator="auto",
                                             score=score, method=method, branch=a["branch"])
                        if res.get("ok"):
                            auto_merged += 1
                            merged_dups.add(dup["id"])
                        else:
                            merge_failed += 1
                            logger.warning("桶内自动合并失败 %s→%s (branch=%s score=%s): %s",
                                           dup["id"], keep["id"], a["branch"], score, res.get("error"))
                    else:
                        # 开关关闭：进人工队列（不丢线索）
                        _insert_dup_candidate(conn, keep, dup, score, vec_sim, fz_sim,
                                              nb_sim=nb_sim, method=method, note="auto_merge_disabled")
                elif score >= FLAG_T:
                    # flag：建候选（pending 待审）
                    _insert_dup_candidate(conn, keep, dup, score, vec_sim, fz_sim,
                                          nb_sim=nb_sim, method=method)
                # else：score < FLAG_T 忽略
    conn.commit()
    return {"candidates": conn.execute(
        "SELECT COUNT(*) FROM entity_dup_candidates WHERE status='pending'").fetchone()[0],
            "auto_merged": auto_merged, "merge_failed": merge_failed,
            "buckets_dropped": bucket_dropped,
            "thresholds": {"auto": AUTO_T, "flag": FLAG_T, "auto_merge_enabled": AUTO_MERGE_ON}}


# ═══════════ 3. 候选列表（给前端） ═══════════

def list_dup_candidates(conn, status: str | None = None, limit: int = 50) -> list:
    q = "SELECT * FROM entity_dup_candidates"
    params = []
    if status:
        q += " WHERE status=?"
        params.append(status)
    q += " ORDER BY score DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in conn.execute(q, params).fetchall()]


# ═══════════ 4. 确认/驳回候选 ═══════════

def review_candidate(conn, cid: int, action: str, operator: "", branch: str = "personal",str = "知识工程师") -> dict:
    """确认=合并 / 驳回=标记 rejected。

    修复：合并失败必须透传 error（此前静默 ok=True 导致前端误报成功、候选卡在 pending）；
    源实体缺失/废弃的孤儿候选自动驳回清理，避免待审队列永久卡死。
    """
    row = conn.execute("SELECT * FROM entity_dup_candidates WHERE id=?", (cid,)).fetchone()
    if not row:
        return {"ok": False, "error": "候选不存在"}
    if row["status"] != "pending":
        return {"ok": False, "error": f"该候选已处理（{row['status']}）"}
    if action == "confirm":
        keep = conn.execute(
            "SELECT id FROM entities WHERE id=? AND branch=? AND status!='deprecated'", (row["keep_id"], branch)).fetchone()
        dup = conn.execute(
            "SELECT id FROM entities WHERE id=? AND branch=? AND status!='deprecated'", (row["dup_id"], branch)).fetchone()
        if not keep or not dup:
            # 源实体已删除/废弃：无法合并，自动驳回清理（留痕），防止待审队列卡死
            conn.execute(
                "UPDATE entity_dup_candidates SET status='rejected', reviewed_by=?, reviewed_at=CURRENT_TIMESTAMP WHERE id=?",
                (operator, cid))
            conn.commit()
            return {"ok": True, "action": "reject", "auto_rejected": True,
                    "reason": "源实体不存在或已废弃，无法合并，已自动驳回清理"}
        result = merge_entities(conn, row["keep_id"], row["dup_id"],
                                operator=operator, score=row["score"], method=row["method"],
                                save_audit=True)
        if not result.get("ok"):
            return {"ok": False, "error": result.get("error", "合并失败")}
        conn.execute("UPDATE entity_dup_candidates SET status='merged', reviewed_by=?, reviewed_at=CURRENT_TIMESTAMP WHERE id=?",
                     (operator, cid))
    elif action == "reject":
        conn.execute("UPDATE entity_dup_candidates SET status='rejected', reviewed_by=?, reviewed_at=CURRENT_TIMESTAMP WHERE id=?",
                     (operator, cid))
    elif action == "relate":
        # P2-7（2026-10-11）：「相关但不是同一实体」——**不合并、两个实体都保留**。
        # 依据 Senzing `Possibly related` 与 Neo4j 官方警告（母公司 vs 子公司不该因
        # 共享品牌而合并）。MBSE 场景的典型层级关系：部件 ⊂ 分系统、载荷 ⊂ 平台。
        #
        # 落库方式：状态置 'related'（不复用 rejected，否则与"判为重复但拒绝"混淆，
        # 无法区分用户的两种否定理由 —— 这对后续标定阈值很有价值）。
        # ⚠️ 该状态值不在 v2g/entity_dup_candidates 的既有枚举里，属新增，
        #    读侧（前端筛选/指标）默认展示即可，无需迁移。
        conn.execute(
            "UPDATE entity_dup_candidates SET status='related', reviewed_by=?, "
            "reviewed_at=CURRENT_TIMESTAMP WHERE id=?", (operator, cid))
    else:
        return {"ok": False, "error": "未知操作"}
    conn.commit()
    return {"ok": True, "action": action}


# ═══════════ 5. 合并（属性融合 + 关系重指 + aliases + 审计） ═══════════

def merge_entities(conn, keep_id: str, dup_id: str, operator: str = "知识工程师",
                   score: float = 0.0, method: str = "", save_audit: bool = True,
                   branch: str = "personal") -> dict:
    """合并：dup → keep（E-6+E-7：aliases 记录 + entity_merges 审计 + 可撤销）。

    F3 修复：整函数包写事务（BEGIN IMMEDIATE + 异常整体回滚），
    避免多步 UPDATE 中途失败留下「半合并」脏数据（B4）。
    """
    keep = conn.execute("SELECT * FROM entities WHERE id=? AND branch=?", (keep_id, branch)).fetchone()
    dup = conn.execute("SELECT * FROM entities WHERE id=? AND branch=?", (dup_id, branch)).fetchone()
    if not keep or not dup:
        return {"ok": False, "error": "实体不存在"}
    if keep_id == dup_id:
        return {"ok": False, "error": "不能合并自身"}

    in_tx = conn.in_transaction
    if not in_tx:
        conn.execute("BEGIN IMMEDIATE")
    try:
        keep_props = json.loads(keep["properties"] or "{}")
        dup_props = json.loads(dup["properties"] or "{}")
        merged = dict(keep_props)
        new_keys = []
        for k, v in dup_props.items():
            if k not in merged:
                merged[k] = v
                new_keys.append(k)

        # E-6: aliases 记录
        aliases = list(keep_props.get("aliases") or [])
        if dup["name"] not in aliases and dup["name"] != keep["name"]:
            aliases.append(dup["name"])
        merged["aliases"] = aliases

        conn.execute("UPDATE entities SET properties=? WHERE id=? AND branch=?",
                     (json.dumps(merged, ensure_ascii=False), keep_id, branch))

        # 关系重指向
        rc1 = conn.execute("UPDATE relations SET source_id=? WHERE source_id=? AND status!='deprecated'",
                           (keep_id, dup_id)).rowcount
        rc2 = conn.execute("UPDATE relations SET target_id=? WHERE target_id=? AND status!='deprecated'",
                           (keep_id, dup_id)).rowcount
        total_rel = rc1 + rc2

        # dup 软删除
        conn.execute("UPDATE entities SET status='deprecated' WHERE id=? AND branch=?", (dup_id, branch))

        # P2（2026-09-07）entity_aliases 写入侧：dup 本名 → keep 别名；
        # 已指向 dup 的历史别名一并重定向到 keep（合并后 mention 仍可召回 canonical）。
        if dup["name"] != keep["name"]:
            write_entity_alias(conn, keep_id, dup["name"], branch=keep["branch"] or "dev",
                               source_doc=dup["source_doc"] or "",
                               source_type="entity_merge", created_by=operator)
        conn.execute("UPDATE entity_aliases SET entity_id=? WHERE entity_id=?", (keep_id, dup_id))

        # 审计：graph_edit_logs + entity_merges
        conn.execute(
            "INSERT INTO graph_edit_logs (op, node_id, payload, operator) VALUES (?,?,?,?)",
            ("merge_entity", dup_id,
             json.dumps({"into": keep_id, "props_merged": new_keys, "aliases": aliases, "score": score},
                        ensure_ascii=False), operator))
        if save_audit:
            conn.execute(
                "INSERT INTO entity_merges (keep_id,dup_id,score,method,operator,props_merged,relations_redirected,status) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (keep_id, dup_id, score, method, operator,
                 json.dumps(new_keys, ensure_ascii=False), total_rel, "merged"))
        if not in_tx:
            conn.commit()
    except Exception as e:
        if not in_tx:
            conn.rollback()
        logger.error("merge_entities 合并失败（已整体回滚）: keep=%s dup=%s err=%s",
                     keep_id, dup_id, e)
        return {"ok": False, "error": f"合并失败，已整体回滚: {e}"}
    return {"ok": True, "keep": keep_id, "dup": dup_id, "merged_props": new_keys,
            "relations_redirected": total_rel, "name": keep["name"]}


# ═══════════ 5.5 关系去重合并（P0-C：审核队列重复三元组治理） ═══════════

def merge_relation(conn, keep_id: int, dup_id: int, operator: str = "知识工程师") -> dict:
    """P0-C 关系去重合并：dup 边 → keep 边（审核队列一键合并重复三元组）。

    - 属性合并：dup 独有属性并入 keep（keep 优先）
    - 来源文档合并：source_doc 去重追加
    - dup 软删除留痕（deprecated，reviewed_by=operator）
    - 审计：graph_edit_logs + CommitRepo 打点（kind=merge，失败仅打印不阻断）
    """
    keep = conn.execute("SELECT * FROM relations WHERE id=?", (keep_id,)).fetchone()
    dup = conn.execute("SELECT * FROM relations WHERE id=?", (dup_id,)).fetchone()
    if not keep or not dup:
        return {"ok": False, "error": "keep/dup 关系不存在"}
    if keep_id == dup_id:
        return {"ok": False, "error": "不能合并自身"}
    if dup["status"] == "deprecated":
        return {"ok": False, "error": f"dup 关系 #{dup_id} 已废弃"}

    keep_props = json.loads(keep["properties"] or "{}")
    dup_props = json.loads(dup["properties"] or "{}")
    new_keys = []
    for k, v in dup_props.items():
        if k not in keep_props:
            keep_props[k] = v
            new_keys.append(k)
    docs = set()
    if keep["source_doc"]:
        docs.add(keep["source_doc"])
    if dup["source_doc"]:
        docs.add(dup["source_doc"])
    conn.execute("UPDATE relations SET properties=?, source_doc=? WHERE id=?",
                 (json.dumps(keep_props, ensure_ascii=False),
                  ", ".join(sorted(d for d in docs if d)), keep_id))
    conn.execute(
        "UPDATE relations SET status='deprecated', reviewed_by=?, reviewed_at=CURRENT_TIMESTAMP WHERE id=?",
        (operator, dup_id))

    # 审计留痕（graph_edit_logs + CommitRepo 打点，失败仅打印不阻断业务）
    conn.execute(
        "INSERT INTO graph_edit_logs (op, node_id, payload, operator) VALUES (?,?,?,?)",
        ("merge_relation", str(dup_id),
         json.dumps({"into": keep_id, "props_merged": new_keys, "keep_source": keep["source_doc"] or ""},
                    ensure_ascii=False), operator))
    try:
        from repositories.commit_repo import CommitRepo
        row = conn.execute("SELECT branch FROM relations WHERE id=?", (keep_id,)).fetchone()
        CommitRepo(conn).create_commit(
            row["branch"] or "dev", "merge",
            f"关系去重合并：#{dup_id} → #{keep_id}",
            {"relations": [keep_id, dup_id]},
            {"keep_id": keep_id, "dup_id": dup_id, "props_merged": new_keys}, actor=operator)
    except Exception as e:
        print(f"[commit_repo] relation merge 提交打点失败（不阻断业务）: {e}")
    conn.commit()
    return {"ok": True, "keep_id": keep_id, "dup_id": dup_id,
            "deprecated": dup_id, "merged_props": new_keys}


# ═══════════ 6. 合并撤销 ═══════════

def rollback_merge(conn, merge_id: int, operator: str = "知识工程师") -> dict:
    """E-7：撤销合并——恢复 dup 状态、移除补入属性、关系回指。

    B1 修复：原单条 UPDATE 把关系两端同时置为 dup_id，会把「仅一端指向 keep」
    的关系错误双写（自环 dup→dup，二次损坏）。现分两条 UPDATE 分别回指
    source 端点 / target 端点；整函数包写事务（任一步失败整体回滚）。
    补充修复：回滚时同时移除合并追加的 aliases（dup 原名），保证 E-7 撤销语义完整。
    已知边界：无法区分「合并时被重指」与「原本就指向 keep」的关系，极端场景
    可能过度回指——完整保真需在合并时快照被重指关系 id 列表（P2 跟进）。
    """
    row = conn.execute("SELECT * FROM entity_merges WHERE id=?", (merge_id,)).fetchone()
    if not row or row["status"] != "merged":
        return {"ok": False, "error": "合并不存在或已撤销"}

    in_tx = conn.in_transaction
    if not in_tx:
        conn.execute("BEGIN IMMEDIATE")
    try:
        # 恢复 dup 状态
        conn.execute("UPDATE entities SET status='candidate' WHERE id=?", (row["dup_id"],))
        # 关系回指 dup：source 端点与 target 端点分开回写（B1：防双端双写）
        conn.execute("UPDATE relations SET source_id=? WHERE source_id=? AND status!='deprecated'",
                     (row["dup_id"], row["keep_id"]))
        conn.execute("UPDATE relations SET target_id=? WHERE target_id=? AND status!='deprecated'",
                     (row["dup_id"], row["keep_id"]))
        # 属性移除补入项
        keep_props = json.loads(conn.execute(
            "SELECT properties FROM entities WHERE id=?", (row["keep_id"],)).fetchone()["properties"] or "{}")
        for k in json.loads(row["props_merged"] or "[]"):
            keep_props.pop(k, None)
        # 移除合并时追加的 aliases 项（dup 原名）——E-7 可撤销语义完整化
        dup_row = conn.execute("SELECT name FROM entities WHERE id=?", (row["dup_id"],)).fetchone()
        if dup_row:
            aliases = [a for a in keep_props.get("aliases") or [] if a != dup_row["name"]]
            if aliases:
                keep_props["aliases"] = aliases
            else:
                keep_props.pop("aliases", None)
        conn.execute("UPDATE entities SET properties=? WHERE id=?",
                     (json.dumps(keep_props, ensure_ascii=False), row["keep_id"]))
        conn.execute("UPDATE entity_merges SET status='rolled_back', rollback_at=CURRENT_TIMESTAMP WHERE id=?",
                     (merge_id,))
        conn.execute(
            "INSERT INTO graph_edit_logs (op, node_id, payload, operator) VALUES (?,?,?,?)",
            ("rollback_merge", row["dup_id"],
             json.dumps({"from": row["keep_id"], "merge_id": merge_id}, ensure_ascii=False), operator))
        if not in_tx:
            conn.commit()
    except Exception as e:
        if not in_tx:
            conn.rollback()
        logger.error("rollback_merge 撤销失败（已整体回滚）: merge_id=%s err=%s", merge_id, e)
        return {"ok": False, "error": f"撤销失败，已整体回滚事务: {e}"}
    return {"ok": True, "rollback": merge_id}


# ═══════════ 6.5 冲突消解（P0-1：融合第四步，v2.0 §2.4） ═══════════

# 加权分：可信度×0.5 + 时效性×0.3 + 权威性×0.2（参数集中在模块顶部便于调整）
_CONFIDENCE_DEFAULT = 0.7
_AUTHORITY_WEIGHTS = {"manual": 0.9, "sysml_import": 0.9, "vector": 0.8,
                      "ai_generated": 0.7, "": 0.7}
# 冲突消解忽略的属性（溯源/内部字段不构成"事实冲突"）
_CONFLICT_SKIP_KEYS = {"aliases", "source_frag", "source_mentions", "confidence"}


def _side_score(created_at: str, source_type: str, props_json: str) -> float:
    """一侧事实的可信度加权分：可信度(属性/默认) ×0.5 + 时效性 ×0.3 + 权威性 ×0.2。"""
    import datetime as _dt
    # 时效性：距今天数 → [0,1]（365 天线性衰减）
    rec = 0.5
    try:
        t = _dt.datetime.fromisoformat(str(created_at or "").replace("Z", "").replace(" ", "T"))
        days = max(0, (_dt.datetime.now() - t).days)
        rec = max(0.0, 1.0 - days / 365.0)
    except Exception as e:
        logger.debug("created_at 解析失败，时效按默认 0.5: %s (%s)", created_at, e)
    # 可信度：优先属性内 confidence（抽取/评估写入），否则默认 0.7
    conf = _CONFIDENCE_DEFAULT
    try:
        v = json.loads(props_json or "{}").get("confidence")
        if isinstance(v, (int, float)) and 0 <= v <= 1:
            conf = float(v)
    except Exception as e:
        logger.debug("props_json 解析失败，置信度按默认 0.7: %s", e)
    auth = _AUTHORITY_WEIGHTS.get(source_type or "", 0.7)
    return round(0.5 * conf + 0.3 * rec + 0.2 * auth, 3)


def conflict_detect(conn) -> dict:
    """P0-1 冲突检测（融合第四步）：检测矛盾事实并写入 knowledge_conflicts。

    两类冲突（同一分支内比较，跨分支版本副本不算冲突）：
    - entity_attr：同表面归一名称 + 同类型的实体对（不同逻辑 id），属性同 key 值不同（均非空）
    - relation_attr：同 (source_id, relation_type, target_id) 的关系对，属性同 key 值不同
    幂等：同对同 key 已有 pending 记录则跳过（增量检测）。
    返回 {detected, skipped}
    """
    detected, skipped = 0, 0

    def _conflict_insert(kind, a, b, key, va, vb, a_score, b_score):
        nonlocal detected, skipped
        if kind == "entity_attr":
            id_a, id_b = min(a["id"], b["id"]), max(a["id"], b["id"])
            rel_a = rel_b = 0
        else:
            id_a = id_b = ""
            rel_a, rel_b = min(a["id"], b["id"]), max(a["id"], b["id"])
        ex = conn.execute(
            "SELECT id FROM knowledge_conflicts WHERE kind=? AND entity_id_a=? AND entity_id_b=?"
            " AND relation_id_a=? AND relation_id_b=? AND attr_key=? AND status='pending'",
            (kind, id_a, id_b, rel_a, rel_b, key)).fetchone()
        if ex:
            skipped += 1
            return
        conn.execute(
            "INSERT INTO knowledge_conflicts (kind, entity_id_a, entity_id_b, relation_id_a,"
            " relation_id_b, attr_key, value_a, value_b, score_a, score_b, evidence) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (kind, id_a, id_b, rel_a, rel_b, key, str(va)[:500], str(vb)[:500],
             a_score, b_score, json.dumps({
                 "a": {"name": a.get("name") or "", "source_doc": a.get("source_doc") or "",
                       "source_type": a.get("source_type") or "", "created_at": a.get("created_at") or ""},
                 "b": {"name": b.get("name") or "", "source_doc": b.get("source_doc") or "",
                       "source_type": b.get("source_type") or "", "created_at": b.get("created_at") or ""},
             }, ensure_ascii=False)))
        detected += 1

    # 1) 实体级：同分支 + 同归一名称 + 同类型 → 属性值冲突（Row→dict 便于 .get 兼容）
    ents = [dict(r) for r in conn.execute(
        "SELECT * FROM entities WHERE status!='deprecated'").fetchall()]
    # P1 类型限定归一：把候选实体类型传入，glossary 绑定 entity_type 的词条仅对匹配类型折叠
    norms = _normalize_mentions(conn, [e["name"] for e in ents],
                                types={e["name"]: e.get("entity_type", "") for e in ents})
    by_key = {}
    for e in ents:
        by_key.setdefault((e["branch"], norms.get(e["name"], e["name"]),
                           e["entity_type"]), []).append(e)
    for (branch, nk, et), group in by_key.items():
        if len(group) < 2:
            continue
        # 桶上限（P1-2）：同归一名同类型极端大组（>200）截断配对，保底可用性
        if len(group) > 200:
            skipped += len(group) - 200
            group = group[:200]
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = group[i], group[j]
                if a["id"] == b["id"]:      # 同逻辑实体跨分支版本 → 跳过
                    continue
                pa, pb = json.loads(a["properties"] or "{}"), json.loads(b["properties"] or "{}")
                for k in set(pa) & set(pb):
                    if k in _CONFLICT_SKIP_KEYS:
                        continue
                    va, vb = pa[k], pb[k]
                    if not va or not vb or str(va) == str(vb):
                        continue
                    _conflict_insert("entity_attr", a, b, k, va, vb,
                                     _side_score(a["created_at"], a["source_type"], a["properties"]),
                                     _side_score(b["created_at"], b["source_type"], b["properties"]))
    # 2) 关系级：同分支 + 同三元组 → 属性值冲突（relations 无 source_type 列，用 dict 兼容）
    rels = conn.execute(
        "SELECT * FROM relations WHERE status!='deprecated'").fetchall()
    rel_dicts = [dict(r) for r in rels]
    by_triple = {}
    for r in rel_dicts:
        by_triple.setdefault((r["branch"], r["source_id"], r["relation_type"],
                              r["target_id"]), []).append(r)
    for (branch, sid, rt, tid), group in by_triple.items():
        if len(group) < 2:
            continue
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = group[i], group[j]
                if a["id"] == b["id"]:
                    continue
                pa, pb = json.loads(a["properties"] or "{}"), json.loads(b["properties"] or "{}")
                for k in set(pa) & set(pb):
                    if k in _CONFLICT_SKIP_KEYS:
                        continue
                    va, vb = pa[k], pb[k]
                    if not va or not vb or str(va) == str(vb):
                        continue
                    _conflict_insert("relation_attr", a, b, k, va, vb,
                                     _side_score(a["created_at"], a.get("source_type", "vector"), a["properties"]),
                                     _side_score(b["created_at"], b.get("source_type", "vector"), b["properties"]))
    conn.commit()
    return {"detected": detected, "skipped": skipped}


def conflict_adjudicate(conn, conflict_id: int, decision: str,
                        operator: str = "知识工程师") -> dict:
    """P0-1 冲突裁决：decision = left | right | ignore。

    - left/right：采纳该侧值为规范值——把两侧对应属性统一为该值（数据一致化），
      记录裁决（decision/decided_by/decided_at）+ graph_edit_logs 审计留痕
    - ignore：保留两侧（记录"忽略"，不改动数据）
    加权分仅作建议展示，最终以人工裁决为准（对齐"消歧不自动合并"原则）。
    """
    row = conn.execute("SELECT * FROM knowledge_conflicts WHERE id=?", (conflict_id,)).fetchone()
    if not row:
        return {"ok": False, "error": "冲突记录不存在"}
    if row["status"] != "pending":
        return {"ok": False, "error": f"该冲突已处理（{row['status']}）"}
    if decision not in ("left", "right", "ignore"):
        return {"ok": False, "error": "决策必须为 left/right/ignore"}

    if decision == "ignore":
        conn.execute(
            "UPDATE knowledge_conflicts SET status='ignored', decided_by=?, decided_at=CURRENT_TIMESTAMP WHERE id=?",
            (operator, conflict_id))
        conn.commit()
        return {"ok": True, "action": "ignored"}

    canonical = row["value_a"] if decision == "left" else row["value_b"]
    if row["kind"] == "entity_attr":
        for eid in (row["entity_id_a"], row["entity_id_b"]):
            e = conn.execute("SELECT properties FROM entities WHERE id=?", (eid,)).fetchone()
            if not e:
                continue
            props = json.loads(e["properties"] or "{}")
            props[row["attr_key"]] = canonical
            conn.execute("UPDATE entities SET properties=? WHERE id=?",
                         (json.dumps(props, ensure_ascii=False), eid))
        subject = f"实体 {row['entity_id_a']}/{row['entity_id_b']} 属性 {row['attr_key']}"
    else:
        for rid in (row["relation_id_a"], row["relation_id_b"]):
            r = conn.execute("SELECT properties FROM relations WHERE id=?", (rid,)).fetchone()
            if not r:
                continue
            props = json.loads(r["properties"] or "{}")
            props[row["attr_key"]] = canonical
            conn.execute("UPDATE relations SET properties=? WHERE id=?",
                         (json.dumps(props, ensure_ascii=False), rid))
        subject = f"关系 {row['relation_id_a']}/{row['relation_id_b']} 属性 {row['attr_key']}"
    conn.execute(
        "UPDATE knowledge_conflicts SET status='resolved', decision=?, decided_by=?,"
        " decided_at=CURRENT_TIMESTAMP WHERE id=?",
        (decision, operator, conflict_id))
    conn.execute(
        "INSERT INTO graph_edit_logs (op, node_id, payload, operator) VALUES (?,?,?,?)",
        ("conflict_adjudicate", str(conflict_id),
         json.dumps({"subject": subject, "decision": decision, "canonical": canonical},
                    ensure_ascii=False), operator))
    conn.commit()
    return {"ok": True, "action": "resolved", "decision": decision, "canonical": canonical}


def list_conflicts(conn, status: str | None = None, limit: int = 100) -> list:
    """冲突列表（前端展示）：两侧对象名/属性/值/加权分/证据解析。"""
    q = "SELECT * FROM knowledge_conflicts"
    params = []
    if status:
        q += " WHERE status=?"
        params.append(status)
    q += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    out = []
    for r in conn.execute(q, params).fetchall():
        item = dict(r)
        try:
            item["evidence"] = json.loads(item.get("evidence") or "{}")
        except Exception:
            item["evidence"] = {}
        if item["kind"] == "entity_attr":
            item["name_a"] = item["name_b"] = ""
            ea = conn.execute("SELECT name FROM entities WHERE id=?", (item["entity_id_a"],)).fetchone()
            eb = conn.execute("SELECT name FROM entities WHERE id=?", (item["entity_id_b"],)).fetchone()
            item["name_a"], item["name_b"] = (ea["name"] if ea else "?"), (eb["name"] if eb else "?")
        else:
            ra = conn.execute("SELECT source_id, target_id, relation_type FROM relations WHERE id=?", (item["relation_id_a"],)).fetchone()
            rb = conn.execute("SELECT source_id, target_id, relation_type FROM relations WHERE id=?", (item["relation_id_b"],)).fetchone()
            item["name_a"] = f"关系#{item['relation_id_a']}"
            item["name_b"] = f"关系#{item['relation_id_b']}"
            item["triple"] = (f"{ra['source_id']}--{ra['relation_type']}--{ra['target_id']}" if ra else "?")
        out.append(item)
    return out


# ═══════════ 7. 旧接口兼容 ═══════════

# F5：已移除半死函数 find_duplicates（全库无调用方）——
# 候选数据统一走 entity_dup_candidates 查询接口（handle_review_action / list_candidates）。


# ═══════════ 8. 两段式校验：工序① 规则预处理（免费）＋ 工序② LLM 实体解析 ═══════════

# ── 8.1 别名表 / 简全称表 / 全量归一 / Canopy 键（已下沉 text_normalize） ──
# _ALIAS_TABLE / _SHORT_FULL_TABLE / normalize_full / _canopy_key 单一实现在
# text_normalize.py，本模块顶部 re-export 保持调用方零改动。


# ── 8.3 工序① 规则预处理：三态出口（auto-resolve / candidate pair / skip） ──

def run_rule_preprocess(conn, threshold: float = 0.62) -> dict:
    """工序① 规则预处理（免费）：归一化 → Canopy 分桶 → 桶内双判据 → 三态出口。

    三态：
    - auto-resolve（归一化后完全相等 或 别名命中规范名一致）：自动合并（留痕可撤销）
    - candidate pair（桶内相似度 ≥ threshold）：生成候选对（method=canopy，进工序② LLM 判定）
    - skip：无关联，不生成候选对（省 LLM 调用）

    返回 {input_n, auto_aligned, candidates, skipped, method:'rule_preprocess'}
    """
    rows = conn.execute(
        "SELECT id, name, entity_type, properties, status, branch FROM entities WHERE status!='deprecated'"
    ).fetchall()
    if len(rows) < 2:
        return {"input_n": len(rows), "auto_aligned": 0, "candidates": 0,
                "skipped": 0, "method": "rule_preprocess"}

    ve = VectorEngine()
    auto_aligned, candidates, skipped = 0, 0, 0
    seen = set()
    aligned_ids = set()  # 已被强规则自动合并（dup 已 deprecated）的 id，Canopy 阶段跳过
    ve_cache = {}

    def _vec(doc: dict):
        if doc["id"] not in ve_cache:
            ve_cache[doc["id"]] = ve._vector(doc["name"] + " " + (doc.get("properties") or "{}"))
        return ve_cache[doc["id"]]

    # 1) 归一化全量（含别名/简全称展开）
    norms = {}
    for r in rows:
        norms[r["id"]] = normalize_full(r["name"])

    # 2) 强规则：归一化后完全相等 → 自动对齐（无需 Canopy，直接配对）
    by_norm = {}
    for r in rows:
        by_norm.setdefault((r["branch"], r["entity_type"], norms[r["id"]]), []).append(dict(r))
    for (branch, et, nk), grp in by_norm.items():
        if len(grp) < 2:
            continue
        # keep：reviewed 优先，其次创建早
        grp_sorted = sorted(grp, key=lambda e: (e.get("status") != "reviewed", e.get("created_at") or ""))
        keep = grp_sorted[0]
        for dup in grp_sorted[1:]:
            key = tuple(sorted([keep["id"], dup["id"]]))
            if key in seen:
                continue
            seen.add(key)
            try:
                merge_entities(conn, keep["id"], dup["id"], operator="auto",
                               score=1.0, method="rule_normalize")
                aligned_ids.add(dup["id"])
                auto_aligned += 1
            except Exception:
                skipped += 1

    # 3) Canopy 分桶（未自动对齐的剩余实体）
    nb_map = _load_neighbor_sets(conn, [r["id"] for r in rows])  # P2 图邻域特征
    buckets = {}
    for r in rows:
        key = _canopy_key(r["entity_type"], r["name"])
        buckets.setdefault(key, []).append(dict(r))

    for bucket_items in buckets.values():
        if len(bucket_items) < 2:
            continue
        for i in range(len(bucket_items)):
            for j in range(i + 1, len(bucket_items)):
                a, b = bucket_items[i], bucket_items[j]
                if a["id"] == b["id"]:
                    continue
                key = tuple(sorted([a["id"], b["id"]]))
                if key in seen:
                    continue
                # 已被强规则自动合并（dup 已 deprecated）的实体跳过
                if a["id"] in aligned_ids or b["id"] in aligned_ids:
                    continue
                # 归一化后完全相等 → 已被强规则处理，跳过（避免重复）
                if norms[a["id"]] == norms[b["id"]]:
                    continue
                seen.add(key)
                vec_sim = ve._cosine(_vec(a), _vec(b))
                fz_sim = fuzzy_score(a["name"], b["name"])
                nb_sim = _jaccard(nb_map.get(a["id"]) or set(), nb_map.get(b["id"]) or set())
                score = round(0.55 * vec_sim + 0.25 * fz_sim + 0.20 * nb_sim, 3) if nb_sim > 0 \
                    else round(0.7 * vec_sim + 0.3 * fz_sim, 3)
                if score >= threshold:
                    method = "canopy"
                    keep, dup = (a, b) if (a.get("status") == "reviewed" or b.get("status") != "reviewed") else (b, a)
                    evidence = {
                        "vec_score": round(vec_sim, 3),
                        "fuzzy_score": round(fz_sim, 3),
                        "nb_score": round(nb_sim, 3),
                        "keep_name": keep["name"],
                        "dup_name": dup["name"],
                        "same_type": True,
                        "norm_a": norms[a["id"]],
                        "norm_b": norms[b["id"]],
                    }
                    ex = conn.execute(
                        "SELECT id FROM entity_dup_candidates WHERE keep_id=? AND dup_id=?",
                        (keep["id"], dup["id"])).fetchone()
                    if not ex:
                        conn.execute(
                            "INSERT INTO entity_dup_candidates (keep_id,dup_id,entity_type,score,vec_score,fuzzy_score,method,evidence,status) "
                            "VALUES (?,?,?,?,?,?,?,?,?)",
                            (keep["id"], dup["id"], a["entity_type"], score,
                             round(vec_sim, 3), round(fz_sim, 3), method,
                             json.dumps(evidence, ensure_ascii=False), "pending"))
                        candidates += 1
                else:
                    skipped += 1
    conn.commit()
    return {"input_n": len(rows), "auto_aligned": auto_aligned, "candidates": candidates,
            "skipped": skipped, "method": "rule_preprocess"}


# ── 8.4 工序② LLM 实体解析：成对判定（消歧 + 对齐 + 冲突裁决 + 理由 + 置信度） ──

def resolve_pairs_llm(conn, pair_ids: list | None = None, limit: int = 20,
                      llm=None, operator: str = "知识工程师") -> dict:
    """工序② LLM 实体解析：仅对规则判不了的候选对做成对判定。

    - 只处理 status='pending' 且 method='canopy'（规则未决）的候选对
    - LLM 一次输出：verdict(merge|separate) + disambiguation 理由 + alignment(keep+reason)
      + conflict(字段级采纳) + confidence
    - 结果写入 entity_dup_candidates.evidence（llm_verdict 字段）供审核溯源
    - LLM 不可用（无 provider / 异常）→ 降级：标记 llm_verdict=null（人工直接裁决），不阻断

    返回 {judged, degraded, results: [{cid, verdict, confidence}]}
    """
    q = "SELECT * FROM entity_dup_candidates WHERE status='pending'"
    params = []
    if pair_ids:
        ph = ",".join("?" * len(pair_ids))
        q += f" AND id IN ({ph})"
        params.extend(pair_ids)
    q += " ORDER BY score DESC LIMIT ?"
    params.append(limit)
    rows = [dict(r) for r in conn.execute(q, params).fetchall()]
    if not rows:
        return {"judged": 0, "degraded": 0, "results": []}

    # 组装每对上下文（名称/类型/属性/来源 + 规则证据）
    pair_ctx = []
    for r in rows:
        keep = conn.execute("SELECT * FROM entities WHERE id=?", (r["keep_id"],)).fetchone()
        dup = conn.execute("SELECT * FROM entities WHERE id=?", (r["dup_id"],)).fetchone()
        if not keep or not dup or keep["status"] == "deprecated" or dup["status"] == "deprecated":
            continue
        pair_ctx.append({
            "cid": r["id"], "keep": dict(keep), "dup": dict(dup),
            "score": r["score"], "vec_score": r["vec_score"], "fuzzy_score": r["fuzzy_score"],
            "method": r["method"], "evidence": r["evidence"],
        })
    if not pair_ctx:
        return {"judged": 0, "degraded": 0, "results": []}

    from llm import llm_client
    _llm = llm or llm_client
    judged, degraded, results = 0, 0, []

    for pair in pair_ctx:
        k, d = pair["keep"], pair["dup"]
        k_props = json.loads(k["properties"] or "{}")
        d_props = json.loads(d["properties"] or "{}")
        prompt = (
            "你是知识图谱实体解析专家。请判断以下两个实体是否指向同一概念，并输出裁决。\n"
            "只输出 JSON，不要额外文字，格式：\n"
            '{"verdict":"merge|separate","disambiguation":"判断理由","alignment":{'
            '"keep":"实体A|实体B","reason":"保留方理由"},"conflict":[{'
            '"field":"属性名","take":"a|b|merge","reason":"采纳理由"}],"confidence":0.0-1.0}\n\n'
            f"实体A（候选保留方）：名称={k['name']} 类型={k['entity_type']} 属性={json.dumps(k_props, ensure_ascii=False)} 来源={k.get('source_doc','')}\n"
            f"实体B（候选合并方）：名称={d['name']} 类型={d['entity_type']} 属性={json.dumps(d_props, ensure_ascii=False)} 来源={d.get('source_doc','')}\n"
            f"规则预判：相似度={pair['score']}（向量{pair['vec_score']}/模糊{pair['fuzzy_score']}）\n"
            "裁决要点：①verdict=merge 表示同一实体应合并（消歧结论）；②alignment.keep 选字段更全/更权威的一方；"
            "③conflict 仅在两方对同一属性值冲突时列出，take 说明采纳哪侧；④confidence 为本次判定置信度。"
        )
        verdict = None
        try:
            resp = _llm.chat([
                {"role": "system", "content": "你是知识图谱实体解析专家，输出严格 JSON。"},
                {"role": "user", "content": prompt},
            ])
            content = resp["choices"][0]["message"]["content"] or ""
            m = re.search(r"\{[\s\S]*\}", content)
            if m:
                parsed = json.loads(m.group(0))
                v = str(parsed.get("verdict") or "").strip().lower()
                if v in ("merge", "separate"):
                    verdict = {
                        "verdict": v,
                        "disambiguation": str(parsed.get("disambiguation") or "")[:500],
                        "alignment": parsed.get("alignment") or {},
                        "conflict": parsed.get("conflict") or [],
                        "confidence": min(max(float(parsed.get("confidence") or 0.5), 0.0), 1.0),
                    }
        except Exception:
            verdict = None
        if verdict is None:
            # 降级：LLM 不可用/输出非法 → 保留 pending 人工裁决，标记 degraded
            degraded += 1
            verdict = {"verdict": "", "disambiguation": "", "alignment": {},
                       "conflict": [], "confidence": 0.0, "degraded": True}
        try:
            ev = json.loads(pair["evidence"] or "{}")
        except Exception:
            ev = {}
        ev["llm_verdict"] = verdict
        conn.execute("UPDATE entity_dup_candidates SET evidence=? WHERE id=?",
                     (json.dumps(ev, ensure_ascii=False), pair["cid"]))
        if verdict.get("verdict"):
            judged += 1
        results.append({"cid": pair["cid"], "verdict": verdict.get("verdict", ""),
                        "confidence": verdict.get("confidence", 0.0)})
    conn.commit()
    return {"judged": judged, "degraded": degraded, "results": results}


# ═══════════ 8.5 表面归一（P0-2，自 vector2graph 迁入解环） ═══════════

def _fuzzy_ratio(a: str, b: str) -> float:
    """P1.5：纯 Python Levenshtein 相似度（0~1）。fuzzy 词条匹配用，量小可控。"""
    if not a or not b:
        return 0.0
    la, lb = len(a), len(b)
    if abs(la - lb) > max(la, lb) // 3:   # 长度差粗筛剪枝
        return 0.0
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        cur = [i] + [0] * lb
        ca = a[i - 1]
        for j in range(1, lb + 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != b[j - 1]))
        prev = cur
    return 1 - prev[lb] / max(la, lb)


def _load_neighbor_sets(conn, entity_ids: list) -> dict:
    """P2 图邻域特征：批量加载实体的关系邻居集合。返回 {entity_id: set(neighbor_id)}。"""
    ids = list(entity_ids)
    nb = {i: set() for i in ids}
    if not ids:
        return nb
    ph = ",".join("?" * len(ids))
    try:
        for r in conn.execute(
                f"SELECT source_id, target_id FROM relations WHERE status!='deprecated' "
                f"AND (source_id IN ({ph}) OR target_id IN ({ph}))", ids + ids).fetchall():
            s, t = r["source_id"], r["target_id"]
            if s in nb:
                nb[s].add(t)
            if t in nb:
                nb[t].add(s)
    except Exception as e:
        logger.warning("邻居加载失败，图邻域特征降级为空: %s", e)
    return nb


def _jaccard(s1: set, s2: set) -> float:
    """P2：邻居集合 Jaccard 相似度（0~1；任一方为空 = 0，不参与加权）。"""
    if not s1 or not s2:
        return 0.0
    union = s1 | s2
    return len(s1 & s2) / len(union) if union else 0.0


# P5（2026-10-12）跨语言消歧：中文判定用的字符类（区分 pref_label 是中文还是拉丁）。
_ZH_CH = re.compile(r"[\u4e00-\u9fff]")

# ★ 单一真源：现役词库的「同义词 → 规范名」查询片段。
# `_normalize_mentions`（工序①）与 `load_cross_ling_index`（P5 跨语言消歧）**必须共用**：
#   ① 口径一致是两条消歧链路可对比的前提（任一处改了另一处就漂移）；
#   ② 更硬的理由是**门禁变异锚点必须唯一** ——
#      `verify_entity_resolver` 的 M4/M8 变异按 `raw.replace(anchor, repl, 1)` 只替换**第一处**。
#      本轮曾在 load_cross_ling_index 里内联复制这段 SQL，导致第一处变成新函数、
#      变异打空，`_normalize_mentions` 的 E29/E32 两条断言**假性失效**（段函数异常中断）。
#      ⇒ 「复制一段 SQL 字符串」是门禁的隐形杀手，统一收口到这里。
_GLOSSARY_ALIAS_FROM = (
    "FROM glossary_terms t JOIN glossary_concepts c ON c.concept_id = t.concept_id ")
_GLOSSARY_ALIAS_WHERE = (
    "WHERE t.term IS NOT NULL AND TRIM(t.term) <> '' "
    "AND c.pref_label IS NOT NULL AND TRIM(c.pref_label) <> '' "
    "AND t.term <> c.pref_label "
    "AND c.concept_status = 'approved' "
    "AND COALESCE(c.replaced_by, '') = ''")


def _matching_gate(conn) -> str:
    """返回消歧可见性条件（SQL 片段）。

    🔴 2026-10-12：`term_status` 一列两义问题的正解 —— 「能否参与消歧匹配」改由
    独立列 `use_in_matching` 表达：
        NULL（存量默认）⇒ 沿用旧规则 `term_status='approved'` 参与
        1                 ⇒ 强制参与（无视 term_status，用于术语管理域已收录但
                             生命周期状态不是 approved 的词条，如 HTTP 导入的 admitted）
        0                 ⇒ 强制不参与（已收录但过于通用/不适合自动匹配，如 power、satellite）

    ⚠️ **PRAGMA 检测列是否存在**：未跑迁移的老库/直接调函数的脚本（如 verify_*）
    列不存在时**回落旧条件**，行为与迁移前逐字一致 ⇒ 迁移可安全后置，不产生行为跳变。
    （若直接写死引用新列，老库上会因「no such column」抛错，
      `_normalize_mentions` 的 except 会把它降级成读**废弃的旧表** glossary
      —— 那才是真正的行为断裂：现役词库突然失效且无人察觉。）
    """
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(glossary_terms)")}
    except Exception:  # noqa: BLE001  词库表都不存在 ⇒ 按旧规则
        return "AND t.term_status = 'approved'"
    if "use_in_matching" not in cols:
        return "AND t.term_status = 'approved'"
    return ("AND (t.use_in_matching = 1 "
            "OR (t.use_in_matching IS NULL AND t.term_status = 'approved'))")


def load_cross_ling_index(conn) -> dict:
    """构建**跨语言别名索引**：{规范名 normalize: [别名词原文, ...]}。

    🔴 为什么需要它（2026-10-12 实测的链路断点）：
    `_normalize_mentions` 其实**已经能把英文名折成中文规范名**
    （SQL 读 `glossary_terms JOIN glossary_concepts`，方向 `term → pref_label`），
    但它的返回值只用于报告里的 `normalized_name` **展示字段**；
    `_disambiguate_many` 是**独立重算**的，既不读该结果、也不查词库
    ⇒ **英文候选名与中文图库实体之间不存在任何比对通道**，
    真库实测 `_disambiguate_many` 对 60 个英文候选只产出 2 个 `dup_high`
    （且那 2 个是候选里恰好带中文名的「转发器」「宽带载荷」）。本函数补的就是这条通道。

    ★ **两个数据源合并**（缺一不可）：
      ① **DB 词库** `glossary_terms JOIN glossary_concepts` —— 可人工维护、可审计、可扩充；
      ② **`text_normalize._ALIAS_TABLE` 硬编码常量**（实测 49 条中 **28 条是跨语言**）。
         为什么必须并进来：该表的 key **保留空格**（`power subsystem`），
         而 AI 生成的候选名是**驼峰无空格**（`PowerSubsystem`），
         `normalize_full` 的查表因此**命不中**（实测改前 `PowerSubsystem` → none）。
         而本索引用 `normalize_name`（删空格）建键 ⇒ **驼峰名天然可用**，
         等于让这 28 条既有映射**零新增数据**即刻生效。
         ⚠️ DB 优先：同名 key 以 DB 词库为准（数据可维护 > 硬编码常量）。

    ⚠️ **只收跨语言词条**（term 与 pref_label 语言不同）：
    同语言的别名（如「代码规范」→「SysML V2 语法规范」）已被 `normalize_name`
    的精确匹配覆盖，额外入索引无收益；
    更重要的是**层级误配风险**：「转发器」是「有效载荷转发器」的子串，
    若把同语言的包含关系也灌进索引，等于把 P4 刚修好的层级判定又破坏一遍。

    语言判定用字符类而非 DB 的 `lang` 列：SQL 只在 `glossary_terms` 上有 lang，
    `glossary_concepts` **没有语言列**，无法直接比较两侧语言
    ⇒ 统一按「是否含中文」推断（zh / 非 zh 二分）。

    返回值里的值是**别名词原文**（不是 normalize 后的值）——
    下游建 bigram 索引需要原文走 `normalize_full`。
    读不到 DB 词库不算致命（回落仅用硬编码常量）；返回空 dict 时调用方按"无跨语言能力"降级。
    """
    out = {}
    db_rows = []
    try:
        db_rows = conn.execute(
            "SELECT t.term AS user_term, c.pref_label AS canonical_term "
            + _GLOSSARY_ALIAS_FROM + _GLOSSARY_ALIAS_WHERE
            + _matching_gate(conn)).fetchall()
    except Exception as e:  # noqa: BLE001  词库不可用 ⇒ 降级为"仅硬编码常量"，不阻断主流程
        logger.warning("load_cross_ling_index: 词库加载失败，仅用硬编码别名表: %s", e)

    def _is_cross(alias: str, canon: str) -> bool:
        """只保留**跨语言**词条（见 docstring 的层级误配风险说明）。"""
        return bool(_ZH_CH.search(canon)) != bool(_ZH_CH.search(alias))

    # ① DB 词库优先：全部收集，并记录已占用的别名键
    #    ⚠️ 用**位置索引**而非 r["col"]：sqlite3.Row 支持但普通 tuple 不支持，
    #    而本函数是通用件，不能要求调用方一定设了 `row_factory`。
    db_alias_keys = set()
    for r in db_rows:
        alias, canon = r[0], r[1]
        if not alias or not canon or not _is_cross(alias, canon):
            continue
        key = normalize_name(canon)
        if not key:
            continue
        db_alias_keys.add(normalize_name(alias))
        out.setdefault(key, []).append(alias)

    # ② 硬编码常量补齐：**只跳过 DB 已收录的同一个别名**，其余**全部**加入。
    #    ⚠️ 这里踩过一次坑：最初写成「同一 canonical 只保留第一个别名」，
    #    结果 dict 插入序里排最前的 `aocs` 把同 canonical 的
    #    `attitude control` / `attitude and orbit control` 全挤掉了。
    for _k, _v in _ALIAS_TABLE.items():
        if not _k or not _v or not _is_cross(_k, _v):
            continue
        if normalize_name(_k) in db_alias_keys:
            continue          # DB 优先，同一个别名不重复注入
        key = normalize_name(_v)
        if key:
            out.setdefault(key, []).append(_k)
    return out


def _normalize_mentions(conn, names: list, types: dict = None) -> dict:
    """P0-2 抽取后表面归一（融合第一步）：names → {raw: normalized}。

    - 格式统一：全角→半角 / 去空白/括号尾注 / 小写 / 去敬语（先生/女士/工程师等）
    - 别名映射：glossary 术语（term→name）与既有实体 properties.aliases 命中 → 标准名
    - 仅格式/称谓/别名层统一，不涉及身份判断（消歧在下一步）
    - 批量构建映射表（glossary + aliases 只查一次），供同批节点/关系候选复用
    normalized 仅作去重/消歧/确认定位的规范键，不替换 entity_name（原名保留溯源）。

    P1 类型限定归一（2026-08-28）：glossary 词条 provenance.entity_type 非空时为
    类型限定映射——仅当 types 提供的候选类型与之匹配（大小写不敏感相等）才应用；
    候选类型未知 ≠ 绑定类型 → 不折叠（宁可不折，防误合并）。types=None 时保持
    向后兼容（绑定词条照旧应用，供未接线调用方）。

    迁入说明（诊断 §4.3 P1-1）：原实现位于 vector2graph，conflict_detect 反向导入构成
    循环依赖；迁入本模块后依赖方向为 vector2graph → entity_resolver（单向，无环）。
    去敬语正则已下沉 text_normalize.strip_honorifics。
    """
    alias_map = {}
    scoped_map = {}   # P1 类型限定：norm_key -> [(ent_type_lower, canonical)]
    fuzzy_keys = []   # P1.5 match_type=fuzzy：仅支持全局（未绑定类型）词条参与近似匹配
    # P0-3 修复（2026-10-11）：改读现役术语表 glossary_concepts + glossary_terms。
    # 根因：此前读的是**旧表 glossary**，而该表已于 2026-09-07 废弃（norm_apply.py:191 明写）。
    # 真库旧表仅 10 行且 kind 全为 'intent'，再叠加下面的 kind IN ('entity','predicate','prop_key')
    # 过滤 ⇒ **恒命中 0 行**，别名映射表永远为空（现役两表合计 45 + 20 行却零引用）。
    # 新表结构为 concept/term 二元组：glossary_concepts.pref_label 是规范名，
    # glossary_terms.term 是同义词/别名，经 concept_id 关联 ⇒ 映射 direction 与旧表一致
    # （user_term → canonical_term ≡ term → pref_label）。
    try:
        rows_g = conn.execute(
            "SELECT t.term AS user_term, c.pref_label AS canonical_term, t.note AS note "
            + _GLOSSARY_ALIAS_FROM + _GLOSSARY_ALIAS_WHERE
            + _matching_gate(conn)).fetchall()
        for r in rows_g:
            key = normalize_name(r["user_term"])
            if key:
                alias_map.setdefault(key, r["canonical_term"])
    except Exception as e:
        # 读不到新表不算致命：回落旧表（老库可能仍只有旧表）
        logger.warning("glossary_terms/concepts 加载失败，回落旧表: %s", e)
        try:
            # P0 方案 v2 / 词典隔离：归一化只读归一类映射（entity/predicate/prop_key），
            # 不吃意图路由词（kind='intent'，如 v2→SysML_V2），防止意图词污染实体别名。
            # P1：provenance.entity_type 非空 → 类型限定映射，与候选实体类型匹配才生效。
            # P1.5：provenance.match_type='fuzzy' → 相似度≥0.85 近似匹配（仅全局词条）。
            for r in conn.execute(
                    "SELECT user_term, canonical_term, provenance FROM glossary WHERE active=1 "
                    "AND (kind IN ('entity','predicate','prop_key') OR kind IS NULL OR kind='')").fetchall():
                ent_type = ""
                match_type = "exact"
                try:
                    pv = json.loads(r["provenance"] or "{}")
                    ent_type = (pv.get("entity_type") or "").strip()
                    match_type = (pv.get("match_type") or "exact").strip().lower()
                except Exception:
                    ent_type = ""
                key = normalize_name(r["user_term"])
                if ent_type:
                    scoped_map.setdefault(key, []).append((ent_type.lower(), r["canonical_term"]))
                elif match_type == "fuzzy":
                    fuzzy_keys.append((key, r["canonical_term"]))
                else:
                    alias_map.setdefault(key, r["canonical_term"])
        except Exception as e2:
            logger.warning("glossary 加载失败，别名映射为空: %s", e2)
    try:
        for r in conn.execute(
                "SELECT name, properties FROM entities WHERE status!='deprecated'").fetchall():
            try:
                for a in json.loads(r["properties"] or "{}").get("aliases") or []:
                    alias_map.setdefault(normalize_name(a), r["name"])
            except Exception as e:
                logger.debug("实体别名解析失败，跳过: %s", e)
    except Exception as e:
        logger.warning("entities 别名加载失败，降级为仅 glossary: %s", e)
    typed = types is not None   # 调用方是否显式提供类型图：None=未接线调用方（绑定词条照旧应用，向后兼容）
    types = types or {}
    out = {}
    for n in names:
        s = strip_honorifics(normalize_name(str(n or "")))
        if not s:
            out[n] = s
            continue
        cand_type = str(types.get(n) or types.get(s) or "").strip().lower()
        norm = alias_map.get(s)
        if norm is None and s in scoped_map:
            if cand_type:
                for et, canonical in scoped_map[s]:
                    if et == cand_type:
                        norm = canonical
                        break
            elif not typed:
                norm = scoped_map[s][0][1]
        if norm is None and fuzzy_keys:
            # P1.5 fuzzy 兜底：exact 未命中时做近似匹配（相似度≥0.85，命中即停）
            best, best_r = None, 0.0
            for key, canonical in fuzzy_keys:
                ratio = _fuzzy_ratio(s, key)
                if ratio >= 0.80 and ratio > best_r:
                    best, best_r = canonical, ratio
            if best:
                norm = best
        out[n] = norm if norm else s
    return out


# ═══════════ P2（2026-09-07）：entity_aliases 写入侧 ═══════════
# 此前 entity_aliases 表（mention 原文 → canonical 的别名映射，可重放）只有
# sync-aliases 读取端点、无任何写入方（0 行空转）。本函数为统一写入入口：
# 调用点 ① confirm_candidates 对齐 survivorship ② merge_entities ③ 实体合并路由。


def write_entity_alias(conn, entity_id: str, alias_name: str, branch: str = "dev",
                       source_doc: str = "", source_type: str = "entity_merge",
                       created_by: str = "system") -> bool:
    """写入 mention 原文 → canonical 实体的别名映射（幂等，UNIQUE(entity_id,alias_name,branch)）。

    规则：
    - alias_name 与实体自身名相同 → 跳过（别名≠本名）
    - 空 alias / 空实体 id → 跳过
    - 表不存在（老库未迁移）→ 返回 False 不抛异常（不阻断业务主流程）
    - 实体不存在 → 跳过（防悬空别名）
    返回 True=已写入。

    P0-3（2026-10-11）：存在性检查改用**复合键 (id, branch)**。
    原实现只按 `WHERE id=?` 查，而 `entities` 主键是 (id, branch)、真库有 61 组同 id 跨三分支
    ⇒ ① 拿到的可能是**别的分支**那一行的 name，导致「别名≠本名」判定用错名字；
      ② branch 未显式传入时默认 'dev'，若实体只在 personal 分支存在会被误判为不存在。
    """
    alias = (alias_name or "").strip()
    eid = (entity_id or "").strip()
    if not alias or not eid:
        return False
    br = (branch or "").strip()
    try:
        # 优先按 (id, branch) 精确命中；未给 branch 时按 id 唯一命中（跨分支同名也算存在）
        if br:
            row = conn.execute(
                "SELECT name FROM entities WHERE id=? AND branch=? AND status!='deprecated'",
                (eid, br)).fetchone()
        else:
            row = conn.execute(
                "SELECT name FROM entities WHERE id=? AND status!='deprecated' LIMIT 1",
                (eid,)).fetchone()
        if not row:
            return False
        if alias == (row["name"] or "").strip():
            return False  # 别名≠本名
        conn.execute(
            "INSERT OR IGNORE INTO entity_aliases "
            "(entity_id, alias_name, branch, source_doc, source_type, created_by) "
            "VALUES (?,?,?,?,?,?)",
            (eid, alias, br or "dev", source_doc or "", source_type or "entity_merge",
             created_by or "system"))
        return conn.execute("SELECT changes()").fetchone()[0] > 0
    except Exception as e:  # noqa: BLE001
        logger.warning("write_entity_alias 跳过（不阻断业务）: %s", e)
        return False


def entity_aliases_for(conn, entity_id: str | None = None, limit: int = 200) -> list:
    """列出运行时别名（含实体本名与分支），供别名管理面板使用。

    2026-10-11 新增：`entity_aliases` 此前**只有写入、没有读取端点与 UI**（暗表），
    本函数是它的第一个读取出口。返回按 branch/entity_id 排序，悬空别名带 `_dangling` 标记
    （实体已不存在 —— 用户应能看见并清理，而不是让脏行静默留着）。
    """
    sql = ("SELECT a.entity_id, a.alias_name, a.branch, a.source_type, a.created_by, "
           "a.created_at, e.name AS entity_name "
           "FROM entity_aliases a LEFT JOIN entities e "
           "  ON e.id = a.entity_id AND e.branch = a.branch ")
    params = []
    if entity_id:
        sql += "WHERE a.entity_id=? "
        params.append(entity_id)
    sql += "ORDER BY a.branch, a.entity_id, a.created_at DESC LIMIT ?"
    params.append(int(limit or 200))
    out = []
    try:
        for r in conn.execute(sql, params).fetchall():
            d = dict(r)
            d["_dangling"] = not (d.get("entity_name") or "")
            out.append(d)
    except Exception as e:  # noqa: BLE001
        logger.warning("entity_aliases_for 读取失败（表可能缺失）: %s", e)
    return out
