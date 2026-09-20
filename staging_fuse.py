# -*- coding: utf-8 -*-
"""写前融合闸（Staging Fusion Gate）—— Task #7。

诊断报告 §4.2 P0-P1 架构改造核心：在 confirm_candidates 之前插入本闸，
把「写后全量消歧」改为「写前批内融合」，流程：

    批内 Blocking（_canopy_key 分桶）
      → 四路加权相似度（名称 / 向量 / 属性 / 类型）
      → 三阈值分流（auto-merge / review_queue / new_entity）
      → 批次质量闸（抽检精度 <90% 整批驳回）

关键决策（与诊断报告一致）：
1. 只作用于 entity 候选（candidate_kind='entity' 或 entity_type!='关系候选'）；
   关系候选消歧已有 P0-A（rel_matching_status + rel_match_rel_id）。
2. 阈值与权重入 settings 表（key 前缀 staging_fuse.*），修 C7 硬编码问题；
   读不到时用代码内默认值（向后兼容老库）。
3. auto_merge：仅把 dup 候选标 merged + 写审计 log，keep 候选保持 pending
   走正常 confirm —— 写前融合不触碰 entities 主表，天然可回滚。
4. review_queue：只写审计 log（keep_id/dup_id 为候选 id），不改候选的
   matching_status —— 避免与现有 P0-C「与已有实体重复」语义冲突、防止
   align 悬空引用；人工从 log 队列调 merge_entities（写后调和已复用）。
5. 幂等：staging_fuse_log 中已出现过的候选 id 跳过；老数据懒回填
   normalized_key / _canopy_key / candidate_kind / mention_json。

用法（融合闸由 confirm_candidates 自动触发，也可独立调用）：
    from staging_fuse import fuse_batch, quality_gate, set_fuse_config
    r = fuse_batch(conn, batch_id="B-xxx", operator="王工")
    q = quality_gate(conn, "B-xxx")
"""
import json
import logging

logger = logging.getLogger(__name__)

# ── 默认配置（settings 表可覆盖）─────────────────────────────────
_DEFAULTS = {
    "enabled": "1",                 # 0=关闭融合闸（向后兼容直通）
    "auto_threshold": "0.9",        # ≥ 自动合并
    "review_threshold": "0.7",      # [review, auto) 人工队列；< review 新实体
    "quality_gate": "0.9",          # 抽检精度下限
    "weights": '{"name":0.45,"vector":0.30,"props":0.15,"type":0.10}',
}
_PREFIX = "staging_fuse."

# 已知实体类型集（type 路评分用；不在集内视为未知类型，中性 0.5）
_KNOWN_TYPES = {"需求", "功能", "部件", "接口", "约束", "风险", "决策",
                "经验", "事实", "Part", "Requirement", "Function",
                "Interface", "Constraint", "Context", "System"}


# ═══════════════ 1. 配置（settings 表，修 C7） ═══════════════

def get_fuse_config(conn) -> dict:
    """读融合闸配置：settings 表优先，缺失用默认值。返回 {enabled, auto, review, quality, weights}。"""
    rows = conn.execute(
        "SELECT key, value FROM settings WHERE key LIKE ?", (_PREFIX + "%",)).fetchall()
    kv = {r["key"][len(_PREFIX):]: r["value"] for r in rows}
    weights = json.loads(kv.get("weights", _DEFAULTS["weights"]))
    return {
        "enabled": kv.get("enabled", _DEFAULTS["enabled"]) != "0",
        "auto": float(kv.get("auto_threshold", _DEFAULTS["auto_threshold"])),
        "review": float(kv.get("review_threshold", _DEFAULTS["review_threshold"])),
        "quality": float(kv.get("quality_gate", _DEFAULTS["quality_gate"])),
        "weights": weights,
    }


def set_fuse_config(conn, enabled=None, auto_threshold=None, review_threshold=None,
                    quality_gate=None, weights=None, operator: str = "system") -> dict:
    """写融合闸配置到 settings 表（幂等 upsert）。None 的字段不更新。"""
    updates = {}
    if enabled is not None:
        updates["enabled"] = "1" if enabled else "0"
    if auto_threshold is not None:
        updates["auto_threshold"] = str(float(auto_threshold))
    if review_threshold is not None:
        updates["review_threshold"] = str(float(review_threshold))
    if quality_gate is not None:
        updates["quality_gate"] = str(float(quality_gate))
    if weights is not None:
        updates["weights"] = json.dumps(weights, ensure_ascii=False)
    if not updates:
        return {"updated": 0}
    for k, v in updates.items():
        conn.execute(
            "INSERT INTO settings (key, value, description, updated_at) "
            "VALUES (?,?,?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP",
            (_PREFIX + k, v, f"staging_fuse.{k}"))
    conn.commit()
    logger.info("staging_fuse 配置更新: %s (operator=%s)", updates, operator)
    return {"updated": len(updates), "config": get_fuse_config(conn)}


# ═══════════════ 2. 相似度（四路加权） ═══════════════

def _props_overlap(pa: dict, pb: dict) -> float:
    """属性重叠率：键集合交集权重 ×2，值也一致加额外权重。空-空=1.0，单方空=0.3。"""
    if not pa and not pb:
        return 1.0
    if not pa or not pb:
        return 0.3
    ka, kb = set(pa.keys()), set(pb.keys())
    common = ka & kb
    if not common:
        return 0.1
    val_hit = sum(1 for k in common if str(pa[k]) == str(pb[k]))
    raw = (2 * len(common) + val_hit) / (len(ka) + len(kb))
    return round(min(1.0, raw), 4)


def _type_score(ta: str, tb: str) -> float:
    """类型一致性：归一化相等 → 1.0；均在已知类型集 → 0.5；否则 0.2。"""
    if not ta and not tb:
        return 1.0
    na, nb = ta.strip().lower(), tb.strip().lower()
    if na == nb:
        return 1.0
    if na in {t.lower() for t in _KNOWN_TYPES} and nb in {t.lower() for t in _KNOWN_TYPES}:
        return 0.5
    return 0.2


def _four_way(conn, a, b, weights: dict) -> dict:
    """四路加权相似度。a/b 为 v2g_candidates 行（sqlite3.Row/dict）。返回 {score, name, vector, props, type}。"""
    from entity_resolver import fuzzy_score, normalize_full
    from knowledge_engine import VectorEngine

    name_a, name_b = str(a["entity_name"] or ""), str(b["entity_name"] or "")
    type_a, type_b = str(a["entity_type"] or ""), str(b["entity_type"] or "")
    try:
        props_a = json.loads(a["properties"] or "{}") if isinstance(a.get("properties"), str) else (a.get("properties") or {})
    except Exception:  # noqa: BLE001
        props_a = {}
    try:
        props_b = json.loads(b["properties"] or "{}") if isinstance(b.get("properties"), str) else (b.get("properties") or {})
    except Exception:  # noqa: BLE001
        props_b = {}

    # ① 名称路：归一化完全相等 → 1.0（别名/简全称已展开）；否则模糊分
    if normalize_full(name_a) == normalize_full(name_b) and normalize_full(name_a):
        name_s = 1.0
    else:
        name_s = round(fuzzy_score(name_a, name_b), 4)
    # ② 向量路：名称+属性 上下文 bigram 余弦
    ve = VectorEngine()
    va = ve._vector(name_a + " " + json.dumps(props_a, ensure_ascii=False))
    vb = ve._vector(name_b + " " + json.dumps(props_b, ensure_ascii=False))
    vec_s = round(ve._cosine(va, vb), 4)
    # ③ 属性路
    prop_s = _props_overlap(props_a, props_b)
    # ④ 类型路
    type_s = _type_score(type_a, type_b)

    w = weights or _DEFAULTS_WEIGHTS()
    score = round(w["name"] * name_s + w["vector"] * vec_s + w["props"] * prop_s + w["type"] * type_s, 4)
    return {"score": score, "name": name_s, "vector": vec_s, "props": prop_s, "type": type_s}


def _DEFAULTS_WEIGHTS() -> dict:
    return json.loads(_DEFAULTS["weights"])


def _pick_keep(a, b) -> tuple:
    """keep 选择：信息更丰富者（confidence 高 / properties 键多 / 名称长）。返回 (keep, dup)。"""
    def _richness(r):
        try:
            props = json.loads(r["properties"] or "{}") if isinstance(r.get("properties"), str) else (r.get("properties") or {})
            pn = len(props) if isinstance(props, dict) else 0
        except Exception:  # noqa: BLE001
            pn = 0
        return (float(r["confidence"] or 0) * 0.4 + pn * 0.3 + len(str(r["entity_name"] or "")) * 0.01)
    return (a, b) if _richness(a) >= _richness(b) else (b, a)


# ═══════════════ 3. 融合主入口 ═══════════════

def _backfill(conn, row) -> None:
    """懒回填 4 个 Staging 新列（老数据/落库点未写的兜底）。"""
    from entity_resolver import normalize_full, _canopy_key
    name = str(row["entity_name"] or "")
    etype = str(row["entity_type"] or "")
    kind = "relation" if etype == "关系候选" else "entity"
    if not row["candidate_kind"]:
        conn.execute("UPDATE v2g_candidates SET candidate_kind=? WHERE id=?",
                     (kind, row["id"]))
    if not row["mention_json"] or row["mention_json"] == "{}":
        mention = {"name": name, "entity_type": etype,
                   "rel_source": row["rel_source"] or "",
                   "rel_target": row["rel_target"] or "",
                   "rel_type": row["rel_type"] or ""}
        conn.execute("UPDATE v2g_candidates SET mention_json=? WHERE id=?",
                     (json.dumps(mention, ensure_ascii=False), row["id"]))
    if not row["normalized_key"]:
        conn.execute("UPDATE v2g_candidates SET normalized_key=? WHERE id=?",
                     (normalize_full(name), row["id"]))
    if not row["_canopy_key"]:
        conn.execute("UPDATE v2g_candidates SET _canopy_key=? WHERE id=?",
                     (_canopy_key(etype, name), row["id"]))


def fuse_batch(conn, batch_id: str | None = None, selected_ids: list | None = None,
               operator: str = "system") -> dict:
    """写前融合闸主入口：批内 Blocking → 四路相似 → 三阈值分流。

    参数：
      batch_id     仅融合指定批次的 pending entity 候选（batch 模式）
      selected_ids 仅融合指定候选 id（跨批次勾选模式，与 batch_id 互斥，优先）
      operator     审计操作人（默认 system）

    返回 {processed, auto_merged, review_queue, new_entities, skipped, config, log_n}
    """
    cfg = get_fuse_config(conn)
    if not cfg["enabled"]:
        return {"processed": 0, "auto_merged": 0, "review_queue": 0,
                "new_entities": 0, "skipped": 0, "log_n": 0, "config": cfg,
                "disabled": True}
    if cfg["auto"] <= cfg["review"]:
        logger.warning("staging_fuse 配置异常: auto(%.2f)<=review(%.2f)，闸门跳过本批",
                       cfg["auto"], cfg["review"])
        return {"processed": 0, "auto_merged": 0, "review_queue": 0,
                "new_entities": 0, "skipped": 0, "log_n": 0, "config": cfg}

    # 1) 取候选：仅 entity 候选（关系候选走 P0-A 消歧）
    if selected_ids:
        ph = ",".join("?" * len(selected_ids))
        cands = conn.execute(
            f"SELECT * FROM v2g_candidates WHERE id IN ({ph}) AND status='pending' "
            "AND entity_type!='关系候选'", selected_ids).fetchall()
    elif batch_id:
        cands = conn.execute(
            "SELECT * FROM v2g_candidates WHERE batch_id=? AND status='pending' "
            "AND entity_type!='关系候选'", (batch_id,)).fetchall()
    else:
        return {"processed": 0, "auto_merged": 0, "review_queue": 0,
                "new_entities": 0, "skipped": 0, "log_n": 0, "config": cfg,
                "error": "需指定 batch_id 或 selected_ids"}
    if len(cands) < 2:
        return {"processed": len(cands), "auto_merged": 0, "review_queue": 0,
                "new_entities": 0, "skipped": 0, "log_n": 0, "config": cfg}

    # 2) 幂等去重：已出现在审计 log 的候选跳过
    done = {r["candidate_id"] for r in conn.execute(
        "SELECT DISTINCT candidate_id FROM staging_fuse_log WHERE candidate_id>0").fetchall()}
    cands = [c for c in cands if c["id"] not in done]
    if len(cands) < 2:
        return {"processed": len(cands), "auto_merged": 0, "review_queue": 0,
                "new_entities": 0, "skipped": 0, "log_n": 0, "config": cfg}

    # 3) 懒回填 Staging 列 + Blocking 分桶（同桶内两两比较，避 O(n²)）
    for c in cands:
        _backfill(conn, c)
    buckets = {}
    for c in cands:
        key = c["_canopy_key"] or "##"
        buckets.setdefault(key, []).append(c)
    if conn.in_transaction is False:
        conn.execute("BEGIN IMMEDIATE")  # 事务守卫（F4 模式）

    auto_merged, review_queue, log_n, processed = 0, 0, 0, 0
    w = cfg["weights"]
    for key, items in buckets.items():
        if len(items) < 2:
            continue
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                a, b = items[i], items[j]
                if a["id"] in done or b["id"] in done:
                    continue
                sim = _four_way(conn, a, b, w)
                score = sim["score"]
                processed += 1
                if score >= cfg["auto"]:
                    keep, dup = _pick_keep(a, b)
                    reason = (f"四路加权 {sim['name']:.2f}/{sim['vector']:.2f}/"
                              f"{sim['props']:.2f}/{sim['type']:.2f} ≥ auto({cfg['auto']})")
                    conn.execute(
                        "UPDATE v2g_candidates SET status='merged', reject_reason=?, "
                        "matching_status='dup_high', match_entity_id=? WHERE id=?",
                        (reason, keep["id"], dup["id"]))
                    conn.execute(
                        "INSERT INTO staging_fuse_log (batch_id, candidate_id, action, keep_id, dup_id, score, reason, operator) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (dup["batch_id"], dup["id"], "auto_merge", keep["id"], dup["id"], score, reason, operator))
                    done.add(dup["id"])
                    auto_merged += 1
                    log_n += 1
                elif score >= cfg["review"]:
                    reason = (f"四路加权 {sim['name']:.2f}/{sim['vector']:.2f}/"
                              f"{sim['props']:.2f}/{sim['type']:.2f} ∈ [review({cfg['review']}), auto({cfg['auto']}))")
                    keep, _ = _pick_keep(a, b)
                    conn.execute(
                        "INSERT INTO staging_fuse_log (batch_id, candidate_id, action, keep_id, dup_id, score, reason, operator) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (a["batch_id"], a["id"], "review_queue", keep["id"],
                         b["id"] if keep["id"] == a["id"] else a["id"], score, reason, operator))
                    done.add(a["id"])
                    done.add(b["id"])
                    review_queue += 1
                    log_n += 1
                # else: score < review → 新实体（保持 pending，无日志）
    conn.commit()
    return {"processed": processed, "auto_merged": auto_merged,
            "review_queue": review_queue, "new_entities": processed - auto_merged - review_queue,
            "skipped": len(cands) - processed, "log_n": log_n, "config": cfg}


# ═══════════════ 4. 批次质量闸（<90% 整批驳回） ═══════════════

def quality_gate(conn, batch_id: str | None, sample_n: int = 10) -> dict:
    """批次质量闸：抽检本批 auto_merge 结果，精度 < 下限则整批驳回并回滚。

    判官（严格版）：normalize_full(keep)==normalize_full(dup) 或 fuzzy_score≥0.95。
    不通过时：恢复被 merged 的候选为 pending（写前融合无主表污染，回滚天然安全），
    记录 batch_reject 审计事件。batch_id 为空（跨批次勾选）直接放行。
    返回 {pass, precision, sampled, auto_merged, reason}
    """
    cfg = get_fuse_config(conn)
    if not batch_id:
        return {"pass": True, "precision": 1.0, "sampled": 0,
                "auto_merged": 0, "reason": "无批次（跨批次勾选），跳过质量闸"}
    rows = conn.execute(
        "SELECT id, candidate_id, keep_id, dup_id, score FROM staging_fuse_log "
        "WHERE batch_id=? AND action='auto_merge' ORDER BY id DESC LIMIT ?",
        (batch_id, sample_n)).fetchall()
    if not rows:
        return {"pass": True, "precision": 1.0, "sampled": 0,
                "auto_merged": 0, "reason": "本批无 auto_merge 样本"}
    total_merged = conn.execute(
        "SELECT COUNT(*) FROM staging_fuse_log WHERE batch_id=? AND action='auto_merge'",
        (batch_id,)).fetchone()[0]

    from entity_resolver import fuzzy_score, normalize_full
    passed, failed_rows = 0, []
    for r in rows:
        # keep/dup 均为候选 id → 查候选名
        keep_name = dup_name = ""
        row = conn.execute("SELECT entity_name FROM v2g_candidates WHERE id=?", (r["keep_id"],)).fetchone()
        if row:
            keep_name = row["entity_name"]
        row = conn.execute("SELECT entity_name FROM v2g_candidates WHERE id=?", (r["dup_id"],)).fetchone()
        if row:
            dup_name = row["entity_name"]
        if (normalize_full(keep_name) and normalize_full(keep_name) == normalize_full(dup_name)) \
                or fuzzy_score(keep_name, dup_name) >= 0.95:
            passed += 1
        else:
            failed_rows.append(r)
    precision = round(passed / len(rows), 4) if rows else 1.0

    if precision >= cfg["quality"]:
        return {"pass": True, "precision": precision, "sampled": len(rows),
                "auto_merged": total_merged, "reason": f"抽检精度 {precision:.2%} ≥ {cfg['quality']:.0%}"}

    # 整批驳回：回滚全部 auto_merge 标记 + 审计 batch_reject
    if conn.in_transaction is False:
        conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "UPDATE v2g_candidates SET status='pending', reject_reason='', "
        "matching_status='none', match_entity_id='' "
        "WHERE status='merged' AND reject_reason LIKE '四路加权%' AND id IN "
        "(SELECT dup_id FROM staging_fuse_log WHERE batch_id=? AND action='auto_merge')",
        (batch_id,))
    conn.execute(
        "INSERT INTO staging_fuse_log (batch_id, candidate_id, action, keep_id, dup_id, score, reason, operator) "
        "VALUES (?,0,'batch_reject','','',?,?, 'system')",
        (batch_id, precision,
         f"质量闸抽检 {passed}/{len(rows)} 精度 {precision:.2%} < {cfg['quality']:.0%}，已回滚 {total_merged} 条 auto_merge"))
    conn.commit()
    logger.warning("staging_fuse 批次 %s 质量闸驳回: 精度 %.2f%% < %.0f%%，回滚 %d 条",
                   batch_id, precision * 100, cfg["quality"] * 100, total_merged)
    return {"pass": False, "precision": precision, "sampled": len(rows),
            "auto_merged": total_merged,
            "reason": f"抽检精度 {precision:.2%} < {cfg['quality']:.0%}，已整批驳回并回滚"}


# ═══════════════ 5. 状态查询（给前端/审计） ═══════════════

def fuse_status(conn, batch_id: str | None = None, limit: int = 50) -> dict:
    """查询融合闸运行状态：汇总 + 最近审计明细（batch_id 可选过滤）。"""
    where, params = "", []
    if batch_id:
        where, params = "WHERE batch_id=?", [batch_id]
    rows = conn.execute(
        f"SELECT action, COUNT(*) AS n FROM staging_fuse_log {where} GROUP BY action", params).fetchall()
    summary = {r["action"]: r["n"] for r in rows}
    recent = [dict(r) for r in conn.execute(
        f"SELECT * FROM staging_fuse_log {where} ORDER BY id DESC LIMIT ?",
        params + [limit]).fetchall()]
    return {"summary": summary, "recent": recent, "config": get_fuse_config(conn)}
