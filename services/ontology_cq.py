# -*- coding: utf-8 -*-
"""CQ（能力问题 Competency Questions）引擎 —— P1-4（2026-10-15）。

定位（对齐行业分层，见 docs/本体模型管理模块-…-20261014.md §4.2 共识 3）：
  · verification（机械查结构）= 体检 17 条规则（core/ontology_rules 单一真源）；
  · validation（回答业务问题）= 本文件 —— 「这个本体是否建对了域」的唯一防线，
    业界用 SPARQL ASK 实现（TopBraid/Protégé 生态）。

为什么 kind 仅 'sql'（SQLite）而不是 SPARQL：
  数据真源在 SQLite（entities/relations）；Fuseki 图谱是投影、且私有化部署下
  可能不在线 —— 发布门禁不能依赖一个可能不在线的服务。预留 kind 字段，
  将来 Fuseki 常驻后可加 'sparql' 档（显式扩展，不静默混用）。

纪律（本仓库实测教训，勿删）：
  · 只读白名单：仅放行单条 SELECT/WITH…SELECT；拒 PRAGMA/ATTACH/写操作/多语句
    —— CQ 是用户可登记的输入，等价于一个受控查询面（防注入）；
  · 执行异常 → status='skip' 并附原因（静默少查=假绿，同 _ontology_check 的 degraded）；
  · CQ fail ≠ 拦截发布（R1 拍板：validation≠verification，红可能是有意未完成），
    只随门禁 payload / 体检报告下发 + 落 ontology_cq_logs 留痕可追责；
  · record=False 的探针调用（验证脚本）不落日志（同 /validate record=0 的 P0-2 教训）。
"""
from __future__ import annotations

import re

# 行数上限：CQ 是「有没有」问题，不是导出 —— 截断防大结果集打爆回包
MAX_ROWS = 200

# 只读白名单之外一律拒绝（词边界匹配，防 SELECT 里夹写操作）
_FORBIDDEN = {
    "insert", "update", "delete", "drop", "alter", "create", "replace",
    "vacuum", "reindex", "analyze", "pragma", "attach", "detach",
}

SEEDS = [
    # 追踪链三大关系的端点完整性（真库预演：SATISFIES 54 / TRACE 6 / VERIFIED_BY 45 边，悬空 0）
    ("SATISFIES 满足关系的两端实体是否都存在（无悬空满足边）",
     """SELECT r.id, r.source_id, r.target_id FROM relations r WHERE r.relation_type='SATISFIES' AND (
 NOT EXISTS(SELECT 1 FROM entities e WHERE e.id=r.source_id AND e.branch=r.branch AND e.status!='deprecated') OR
 NOT EXISTS(SELECT 1 FROM entities e WHERE e.id=r.target_id AND e.branch=r.branch AND e.status!='deprecated'))""",
     "empty"),
    ("TRACE 追溯关系的两端实体是否都存在（无悬空追溯边）",
     """SELECT r.id, r.source_id, r.target_id FROM relations r WHERE r.relation_type='TRACE' AND (
 NOT EXISTS(SELECT 1 FROM entities e WHERE e.id=r.source_id AND e.branch=r.branch AND e.status!='deprecated') OR
 NOT EXISTS(SELECT 1 FROM entities e WHERE e.id=r.target_id AND e.branch=r.branch AND e.status!='deprecated'))""",
     "empty"),
    ("VERIFIED_BY 验证关系的两端实体是否都存在（无悬空验证边）",
     """SELECT r.id, r.source_id, r.target_id FROM relations r WHERE r.relation_type='VERIFIED_BY' AND (
 NOT EXISTS(SELECT 1 FROM entities e WHERE e.id=r.source_id AND e.branch=r.branch AND e.status!='deprecated') OR
 NOT EXISTS(SELECT 1 FROM entities e WHERE e.id=r.target_id AND e.branch=r.branch AND e.status!='deprecated'))""",
     "empty"),
    ("存量关系的关系类型是否全部在本体注册（无未注册关系类型）",
     "SELECT DISTINCT relation_type FROM relations "
     "WHERE relation_type NOT IN (SELECT name FROM ontology_types)",
     "empty"),
    ("存量实体的实体类型是否全部在本体注册（无未注册实体类型）",
     "SELECT DISTINCT entity_type FROM entities WHERE status!='deprecated' "
     "AND entity_type NOT IN (SELECT name FROM ontology_types WHERE type_kind='entity')",
     "empty"),
    ("需求满足/追溯/验证三类追踪关系是否有数据（追踪链已建立）",
     "SELECT COUNT(*) AS n FROM relations "
     "WHERE relation_type IN ('SATISFIES','TRACE','VERIFIED_BY')",
     "nonempty"),
]


def validate_query(query: str) -> tuple[str, str]:
    """只读白名单校验。返回 (ok, err)。防注入的**判据**（不是装饰）：
    ① 去注释后单条语句；② 以 SELECT/WITH 开头；③ 无写操作/PRAGMA/ATTACH 词；
    ④ 无分号（拒多语句；分号在字符串字面量里也拒 —— 收紧面换安全，CQ 用不到）。"""
    q = re.sub(r"--[^\n]*", "", str(query or ""))
    q = q.strip()
    if not q:
        return False, "查询为空"
    if ";" in q:
        return False, "不允许分号（仅单条查询）"
    if not re.match(r"^(select|with)\b", q, re.I):
        return False, "仅允许 SELECT / WITH…SELECT 只读查询"
    for w in _FORBIDDEN:
        if re.search(r"\b" + w + r"\b", q, re.I):
            return False, f"只读白名单：不允许 {w.upper()}"
    return True, ""


def ensure_seed(conn) -> None:
    """内置种子 CQ（表空才补，幂等）。用户改/删种子后**不会**被重新塞回
    （只看表空 —— 用户的编辑是意图，不是待修复项）。"""
    n = conn.execute("SELECT COUNT(*) FROM ontology_questions").fetchone()[0]
    if n:
        return
    for q, sql, exp in SEEDS:
        conn.execute(
            "INSERT INTO ontology_questions (question, kind, query, expected, enabled, source, created_by) "
            "VALUES (?,?,?,?,1,'seed','system')", (q, "sql", sql, exp))
    conn.commit()


def run_cqs(conn, source: str = "", record: bool = True, qid: int | None = None) -> list[dict]:
    """执行 CQ（全部 enabled，或指定 qid）。返回 [{id, question, expected, status, row_count, message}]。

    status：pass=符合期望 / fail=不符合期望（业务问题答「不」）/ skip=执行不了（SQL 非法、
    查询报错等）—— skip 是降级不是通过，调用方必须把 skipped 展示出来。
    record=True 时逐题落 ontology_cq_logs（失败只登记不抛，同 _record_health_log 纪律）。
    """
    if qid is not None:
        rows = conn.execute("SELECT * FROM ontology_questions WHERE id=?", (qid,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM ontology_questions WHERE enabled=1 ORDER BY id").fetchall()
    out = []
    for r in rows:
        item = {"id": r["id"], "question": r["question"], "expected": r["expected"],
                "kind": r["kind"], "status": "skip", "row_count": 0, "message": ""}
        ok, err = validate_query(r["query"]) if r["kind"] == "sql" else (False, f"未知 kind: {r['kind']}")
        if not ok:
            item["message"] = f"查询未通过只读校验：{err}"
        else:
            try:
                cur = conn.execute(r["query"])
                data = cur.fetchmany(MAX_ROWS + 1)
                n = len(data[:MAX_ROWS])
                item["row_count"] = n
                if len(data) > MAX_ROWS:
                    item["message"] = f"结果超过 {MAX_ROWS} 行已截断（只判有无，不导出）"
                green = (n == 0) if r["expected"] == "empty" else (n > 0)
                item["status"] = "pass" if green else "fail"
                item["message"] = item["message"] or (
                    f"返回 {n} 行，期望{'为空' if r['expected'] == 'empty' else '非空'}"
                    + ("" if green else " —— 不符合"))
            except Exception as e:
                item["message"] = f"执行失败：{type(e).__name__}: {e}"
        out.append(item)
        if record:
            try:
                conn.execute(
                    "INSERT INTO ontology_cq_logs (question_id, source, status, row_count, message) "
                    "VALUES (?,?,?,?,?)",
                    (r["id"], source, item["status"], item["row_count"], item["message"] or ""))
                conn.commit()
            except Exception as _e:      # 日志写不进不影响结论（同 _record_health_log）
                try:
                    import logging
                    logging.getLogger(__name__).warning(
                        "[ontology_cq] 执行历史落库失败（不影响本次结论）: %s", _e)
                except Exception:
                    pass
    return out


def summarize(results: list[dict]) -> dict:
    """结果 → 摘要（随门禁 payload / 体检报告下发的紧凑形态）。"""
    return {"total": len(results),
            "failed": sum(1 for x in results if x["status"] == "fail"),
            "skipped": sum(1 for x in results if x["status"] == "skip"),
            "results": results}
