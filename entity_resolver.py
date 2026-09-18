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
    rows = conn.execute(
        "SELECT id, name, entity_type, properties, status FROM entities WHERE status!='deprecated'"
    ).fetchall()
    if len(rows) < 2:
        return {"candidates": 0, "auto_merged": 0}

    # 2) Canopy 分块（P1-2）：同类型 + normalize_full 前 4 字桶（别名/简全称已展开，粒度更稳）
    _BUCKET_LIMIT = 200  # 桶上限：防极端桶（空名/泛化前缀）O(n²) 爆炸，超限记 dropped
    buckets, bucket_dropped = {}, 0
    for r in rows:
        key = _canopy_key(r["entity_type"], r["name"])
        lst = buckets.setdefault(key, [])
        if len(lst) < _BUCKET_LIMIT:
            lst.append(dict(r))
        else:
            bucket_dropped += 1
    if not buckets:
        return {"candidates": 0, "auto_merged": 0, "buckets_dropped": bucket_dropped}

    candidates, auto_merged = [], 0
    seen = set()  # (keep_id, dup_id) 去重
    ve_cache = {}  # id → vector
    nb_map = _load_neighbor_sets(conn, [r["id"] for r in rows])  # P2 图邻域特征

    # 3) 强规则合并（P1-2）：normalize_full 全等 + 同类型 → 直接 auto-merge，免两两打分。
    #    归一完全相同的名称，双判据必然满分（等价旧 auto-merge 分支），确定性更高。
    by_full = {}
    for r in rows:
        by_full.setdefault((r["entity_type"], normalize_full(r["name"])), []).append(dict(r))
    merged_dups = set()
    for (et, nf), group in by_full.items():
        if len(group) < 2:
            continue
        keep = group[0]
        for dup in group[1:]:
            try:
                merge_entities(conn, keep["id"], dup["id"], operator="auto",
                               score=1.0, method="norm_exact")
                auto_merged += 1
                merged_dups.add(dup["id"])
            except Exception as e:
                logger.warning("强规则合并失败 %s→%s: %s", dup["id"], keep["id"], e)

    # 4) 桶内配对 → 双判据评分（已强规则合并的 dup 跳过）

    def _vec(doc: dict):
        if doc["id"] not in ve_cache:
            ve_cache[doc["id"]] = ve._vector(doc["name"] + " " + (doc.get("properties") or "{}"))
        return ve_cache[doc["id"]]

    for bucket_items in buckets.values():
        if len(bucket_items) < 2:
            continue
        for i in range(len(bucket_items)):
            for j in range(i + 1, len(bucket_items)):
                a, b = bucket_items[i], bucket_items[j]
                if a["id"] in merged_dups or b["id"] in merged_dups:
                    continue  # 已强规则合并（dup 已废弃），不再参与配对
                key = tuple(sorted([a["id"], b["id"]]))
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
                if score >= 0.92:
                    # auto-merge
                    merge_entities(conn, keep["id"], dup["id"], operator="auto", score=score, method=method)
                    auto_merged += 1
                elif score >= 0.62:
                    # flag：建候选（pending 待审）
                    evidence = {
                        "vec_score": round(vec_sim, 3),
                        "fuzzy_score": round(fz_sim, 3),
                        "nb_score": round(nb_sim, 3),
                        "keep_name": keep["name"],
                        "dup_name": dup["name"],
                        "same_type": True,
                    }
                    # upsert（防重复）
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
                # else：score < 0.62 忽略
    conn.commit()
    return {"candidates": conn.execute(
        "SELECT COUNT(*) FROM entity_dup_candidates WHERE status='pending'").fetchone()[0],
            "auto_merged": auto_merged, "buckets_dropped": bucket_dropped}


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

def review_candidate(conn, cid: int, action: str, operator: str = "知识工程师") -> dict:
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
            "SELECT id FROM entities WHERE id=? AND status!='deprecated'", (row["keep_id"],)).fetchone()
        dup = conn.execute(
            "SELECT id FROM entities WHERE id=? AND status!='deprecated'", (row["dup_id"],)).fetchone()
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
    else:
        return {"ok": False, "error": "未知操作"}
    conn.commit()
    return {"ok": True, "action": action}


# ═══════════ 5. 合并（属性融合 + 关系重指 + aliases + 审计） ═══════════

def merge_entities(conn, keep_id: str, dup_id: str, operator: str = "知识工程师",
                   score: float = 0.0, method: str = "", save_audit: bool = True) -> dict:
    """合并：dup → keep（E-6+E-7：aliases 记录 + entity_merges 审计 + 可撤销）。

    F3 修复：整函数包写事务（BEGIN IMMEDIATE + 异常整体回滚），
    避免多步 UPDATE 中途失败留下「半合并」脏数据（B4）。
    """
    keep = conn.execute("SELECT * FROM entities WHERE id=?", (keep_id,)).fetchone()
    dup = conn.execute("SELECT * FROM entities WHERE id=?", (dup_id,)).fetchone()
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

        conn.execute("UPDATE entities SET properties=? WHERE id=?",
                     (json.dumps(merged, ensure_ascii=False), keep_id))

        # 关系重指向
        rc1 = conn.execute("UPDATE relations SET source_id=? WHERE source_id=? AND status!='deprecated'",
                           (keep_id, dup_id)).rowcount
        rc2 = conn.execute("UPDATE relations SET target_id=? WHERE target_id=? AND status!='deprecated'",
                           (keep_id, dup_id)).rowcount
        total_rel = rc1 + rc2

        # dup 软删除
        conn.execute("UPDATE entities SET status='deprecated' WHERE id=?", (dup_id,))

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
    except Exception as e:
        logger.warning("glossary 加载失败，别名映射为空: %s", e)
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
    """
    alias = (alias_name or "").strip()
    eid = (entity_id or "").strip()
    if not alias or not eid:
        return False
    try:
        row = conn.execute("SELECT name FROM entities WHERE id=?", (eid,)).fetchone()
        if not row:
            return False
        if alias == (row["name"] or "").strip():
            return False  # 别名≠本名
        conn.execute(
            "INSERT OR IGNORE INTO entity_aliases "
            "(entity_id, alias_name, branch, source_doc, source_type, created_by) "
            "VALUES (?,?,?,?,?,?)",
            (eid, alias, branch or "dev", source_doc or "", source_type or "entity_merge",
             created_by or "system"))
        return conn.execute("SELECT changes()").fetchone()[0] > 0
    except Exception as e:  # noqa: BLE001
        logger.warning("write_entity_alias 跳过（不阻断业务）: %s", e)
        return False
