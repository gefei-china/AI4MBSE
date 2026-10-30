# -*- coding: utf-8 -*-
"""评测报告仓储（2026-10-22）：`eval_reports` 读取 + 门禁接线状态。

## 为什么要有它

评估发现三件事，本模块是它们的数据面：

1. **两类评测写进同一张表，此前无类型列** —— 只能靠 `created_by` 字段名反推
   类型（脚本自己的注释里就警告过这个脆弱口径）。`eval_kind` 列已补上
   （见 `database/migrations/misc.py`），本模块按类型分组读。
2. **doc 域检索主指标此前只存在于 JSON 文件里** —— 库表只有 entity/relation
   两列，看板与趋势分析只能靠 `docs/评测/` 下的单份快照。已补 6 个 doc_* 列。
3. **门禁接线状态无人可见** —— `tools/verify/verify_gate_wiring.py` 知道
   "哪个门禁进了 CI、哪个带理由豁免"，但那是命令行门禁的内部知识，
   产品侧看不到 ⇒ "现在有什么在拦着我" 这个问题没有答案。

## 纪律

- **本模块不写任何 SQL 到 router 层**（`verify_layering.py` 卡 router 里的
  `conn.execute("SELECT...")` 增量）。router 只调本模块。
- **门禁状态靠解析 CI 配置**，不靠"跑一遍门禁"—— 跑门禁要分钟级，
  页面不能等。解析结果与 `verify_gate_wiring.py` 同源同口径（同一份
  `tools/verify/*.py` 扫描 + 同一份 `ci.yml` 执行行匹配），保证页面显示的
  与门禁自己判的**一致**（不一致会出现"页面说全接了、门禁说漏了"）。
- **读不到就说读不到**：老库未迁移时如实返回 `migrated=False`，
  不静默返回空列表（静默空 = "没跑过评测"，是假绿）。
"""
from __future__ import annotations

import glob
import json
import os
import re

#: 评测类型 → 中文名（前端直接用，避免前端各写一份映射）
EVAL_KIND_LABELS = {
    "rag_retrieval": "RAG 检索评测",
    "memory_quality": "记忆质量评测",
    "intent_routing": "意图路由评测",
    "doc_extract": "文档抽取评测",
    "generation_quality": "生成质量评测",
    "trace_consistency": "Trace 一致性评测",
    "": "历史记录（未标注类型）",
}

#: 各类型的"主指标"列 —— 前端按类型取不同列渲染，避免"记忆评测没有
#: doc_recall_at_5"被渲染成 0（那会被读成"召回 0 分"）
#: 第四元是 (阈值键, 方向)：方向 "high"=越高越好，"low"=越低越好。
#: 阈值**不在前端写死**（前端只渲染不计算，见 15-kb.js 文件头纪律），
#: 由本模块按 core/config.py 的 eval_gates 逐指标判定 status。
KIND_METRIC_COLUMNS = {
    "rag_retrieval": [
        ("doc_recall_at_5", "recall@5", "high", "retrieval_min_recall_at_5"),
        ("doc_mrr_at_10", "MRR@10", "high", None),
        ("doc_ndcg_at_10", "nDCG@10", "high", None),
        ("doc_pool_recall", "pool_recall", "high", None),
        ("doc_rerank_drop_rate", "重排丢对率", "low", "retrieval_max_rerank_drop"),
        ("doc_recall_miss_rate", "召回缺失率", "low", None),
    ],
    "memory_quality": [
        ("entity_f1", "命中率", "high", None),
    ],
    "intent_routing": [
        ("entity_f1", "准确率", "high", "intent_min_accuracy"),
    ],
    "doc_extract": [
        ("entity_f1", "F1", "high", None),
    ],
    "generation_quality": [
        ("gen_citation_valid_rate", "引用编号有效率", "high", "generation_min_citation_valid"),
        ("gen_citation_usage_rate", "引用使用率", "high", "generation_min_citation_usage"),
        ("gen_no_citation_factual_rate", "无据声称密度", "low", "generation_max_no_citation_factual"),
        # gen_grounded_entity_rate 刻意**不配阈值**：当前样本仅 2 个可核查实体，
        # 负对照拉不开差距 ⇒ 不具判别力（详见 eval_generation_quality.py 文件头）。
        # 进趋势线供观察，等 claim 密度上来再升级为门禁指标。
        ("gen_grounded_entity_rate", "实体接地率", "high", None),
        # LLM 忠实率（2026-10-22 晚 断链③语义层）：judge 自证 + 样本足量
        # 才判定；未启用 --llm-judge 时该列为 NULL ⇒ 前端显示「不适用」。
        ("gen_faithful_rate", "LLM 忠实率", "high", "generation_min_faithful"),
    ],
    # Trace 一致性（2026-10-22 断链②）：strong 样本 = 生产人工裁决，
    # 指标 = 当前路由与裁决的一致率 / 旧错重犯率。n=42 实测 0.881。
    "trace_consistency": [
        ("trace_agreement_rate", "裁决一致率", "high", "trace_min_agreement"),
        # 重错率**配阈值但常驻 SKIP**：当前生产 13 条驳回全是"非用户原话
        # 不应入集"的池有效性裁决，路由纠错类 n=0 ⇒ 无分母。阈值留着，
        # 等出现真负样本同一份代码自动生效（与 grounded 的条件升级同构）。
        ("trace_repeat_mistake_rate", "重错率", "low", "trace_max_repeat_mistake"),
    ],
}

#: 未设门禁阈值的指标给"参考线"（仅用于三色提示，不参与门禁判定）
_REFERENCE_LINES = {
    "doc_mrr_at_10": ("high", 0.70),
    "doc_ndcg_at_10": ("high", 0.60),
    "doc_pool_recall": ("high", 0.95),
    "doc_recall_miss_rate": ("low", 0.15),
}

#: `eval_reports` 里用于"该类型存在哪些指标列"的探测（判断库是否已迁移）
_DOC_COLUMNS = (
    "doc_recall_at_5", "doc_mrr_at_10", "doc_ndcg_at_10",
    "doc_pool_recall", "doc_rerank_drop_rate", "doc_recall_miss_rate",
)

#: 生成质量指标列（2026-10-22 断链③；NULL=未测，与 doc_* 的 DEFAULT 0 语义不同）
_GEN_COLUMNS = (
    "gen_citation_valid_rate", "gen_citation_usage_rate",
    "gen_grounded_entity_rate", "gen_no_citation_factual_rate",
    "gen_faithful_rate",
)

#: Trace 一致性指标列（2026-10-22 断链②；NULL=未测/无分母，语义同 gen_*）
_TRACE_COLUMNS = (
    "trace_agreement_rate", "trace_repeat_mistake_rate",
)


def _gate_thresholds():
    """从 core.config.eval_gates 读门禁阈值（唯一真源）。

    读不到就返回空 dict —— 此时所有指标 status 为 ""（前端按中性灰渲染），
    **不静默按"看起来还行"上色**。理由：阈值读不到时给绿是最危险的
    假绿（让人以为达标了）。
    """
    try:
        from core.config import get as _g
        out = {}
        for k in ("retrieval_min_recall_at_5", "retrieval_min_route_acc",
                  "retrieval_min_route_ok", "retrieval_max_rerank_drop",
                  "intent_min_accuracy", "routing_threshold_acc",
                  "routing_threshold_f1", "generation_min_citation_valid",
                  "generation_min_citation_usage",
                  "generation_max_no_citation_factual",
                  "trace_min_agreement", "trace_max_repeat_mistake",
                  "generation_min_faithful"):
            v = _g("eval_gates", k, None)
            if v is not None:
                out[k] = float(v)
        return out
    except Exception:
        return {}


def _metric_status(value, direction, gate_key, thresholds):
    """单个指标的三色判定。

    - 有门禁阈值 ⇒ 严格按阈值判 ok / alert（**这是真正在拦回归的线**）
    - 无门禁阈值但有参考线 ⇒ ok / warn（仅参考，不参与门禁）
    - 值缺失 ⇒ ""（前端显示「不适用」，不参与判定）
    - 阈值读不到 ⇒ ""（中性灰，见 _gate_thresholds 的说明）

    ⚠ 方向语义：direction="low" 表示"越低越好"（如重排丢对率）。
    """
    if value is None or value == "":
        return ""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return ""
    thr = thresholds.get(gate_key) if gate_key else None
    if thr is not None:
        if direction == "low":
            return "ok" if num <= thr else "alert"
        return "ok" if num >= thr else "alert"
    ref = _REFERENCE_LINES.get(_CURRENT_COLUMN[0]) if _CURRENT_COLUMN else None
    if ref:
        rdir, rthr = ref
        if rdir == "low":
            return "ok" if num <= rthr else "warn"
        return "ok" if num >= rthr else "warn"
    return ""


#: _metric_status 需要知道当前列名（用于查参考线）——用可变容器避免改签名
_CURRENT_COLUMN = [None]


def metric_status_for(kind, column, value, thresholds=None):
    """按类型 + 列名判定某指标的状态。"""
    thr = thresholds if thresholds is not None else _gate_thresholds()
    cols = KIND_METRIC_COLUMNS.get(kind, KIND_METRIC_COLUMNS.get("", []))
    for col, _lbl, direction, gate_key in cols:
        if col == column:
            _CURRENT_COLUMN[0] = column
            try:
                return _metric_status(value, direction, gate_key, thr)
            finally:
                _CURRENT_COLUMN[0] = None
    _CURRENT_COLUMN[0] = column
    try:
        return _metric_status(value, "high", None, thr)
    finally:
        _CURRENT_COLUMN[0] = None


def metric_columns_for(kind, conn=None):
    """某类型要展示的指标列（供后端组装给前端）。

    静态列来自 KIND_METRIC_COLUMNS（单一真源）；给 conn 时**并上快照表里
    该类型实际观测到的键**（O3 扩展通道）—— 新增指标只评测器侧写一行，
    前端即自动透出，零迁移零改映射。观测键缺静态列信息时：label=键名、
    direction/gate 取该键最近一行的落库快照。
    """
    cols = KIND_METRIC_COLUMNS.get(kind, KIND_METRIC_COLUMNS.get("", []))
    thr_map = _gate_thresholds()
    out = []
    for c, l, d, g in cols:
        item = {"key": c, "label": l, "direction": d, "gate_key": g}
        # T2（前端就地编辑）：当前阈值随列下发（阈值唯一真源 eval_gates）
        if g:
            item["threshold"] = thr_map.get(g)
        out.append(item)
    if conn is not None:
        seen = {c["key"] for c in out}
        try:
            rows = conn.execute(
                "SELECT metric_key, direction, gate_key FROM eval_metrics "
                "WHERE eval_kind=? AND metric_key NOT IN (%s) "
                "GROUP BY metric_key" % ",".join("?" * len(seen)) if seen else
                "SELECT metric_key, direction, gate_key FROM eval_metrics "
                "WHERE eval_kind=? GROUP BY metric_key",
                ([kind] + sorted(seen)) if seen else [kind]).fetchall()
            for r in rows:
                out.append({"key": r["metric_key"], "label": r["metric_key"],
                            "direction": r["direction"] or "high",
                            "gate_key": r["gate_key"] or ""})
        except Exception:
            pass  # 快照表不可用 ⇒ 退回静态列（主键路不连坐）
    return out


def _columns(conn, table="eval_reports"):
    try:
        return {r[1] for r in conn.execute("PRAGMA table_info(%s)" % table).fetchall()}
    except Exception:
        return set()


def is_migrated(conn):
    """库是否已具备 eval_kind 与 doc 域指标列。"""
    cols = _columns(conn)
    return "eval_kind" in cols and all(c in cols for c in _DOC_COLUMNS)


def fetch_history(conn, eval_kind="", limit=20):
    """按类型分组取评测历史。

    返回 `{ok, kinds, counts, history, note, migrated}`。
    `history[kind]` 每项含各指标 + `trustworthy`（有负对照且无漂移才为 True）。
    """
    cols = _columns(conn)
    if not cols:
        return {"ok": False, "error": "eval_reports 表不存在（请先跑 init_db）",
                "kinds": [], "counts": {}, "history": {}, "migrated": False}

    if "eval_kind" not in cols:
        return {"ok": False,
                "error": "eval_reports 尚未迁移 eval_kind 列，请先跑 init_db 完成迁移",
                "kinds": [], "counts": {}, "history": {}, "migrated": False}

    lim = max(1, min(int(limit or 20), 100))
    kinds = [k.strip() for k in (eval_kind or "").split(",") if k.strip()]
    where = ("WHERE eval_kind IN (%s)" % ",".join("?" * len(kinds))) if kinds else ""

    sel = ["id", "eval_kind", "doc_name", "entity_f1", "relation_f1",
           "created_by", "model_version", "created_at"]
    sel += [c for c in _DOC_COLUMNS if c in cols]
    sel += [c for c in _GEN_COLUMNS if c in cols]
    sel += [c for c in _TRACE_COLUMNS if c in cols]
    if "detail" in cols:
        sel.append("detail")

    rows = conn.execute(
        "SELECT %s FROM eval_reports %s ORDER BY created_at DESC, id DESC"
        % (", ".join(sel), where), kinds).fetchall()

    by_kind = {}
    thr = _gate_thresholds()
    for r in rows:
        k = (r["eval_kind"] or "") or ""
        bucket = by_kind.setdefault(k, [])
        if len(bucket) >= lim:
            continue
        item = {
            "id": r["id"],
            "doc_name": r["doc_name"],
            "entity_f1": r["entity_f1"],
            "relation_f1": r["relation_f1"],
            "created_by": r["created_by"],
            "model_version": r["model_version"],
            "created_at": r["created_at"],
        }
        for c in _DOC_COLUMNS:
            if c in cols:
                item[c] = r[c]
        for c in _GEN_COLUMNS:
            if c in cols:
                item[c] = r[c]
        for c in _TRACE_COLUMNS:
            if c in cols:
                item[c] = r[c]
        # 逐指标三色状态（阈值来自 eval_gates 唯一真源）
        item["metric_status"] = {
            c: metric_status_for(k, c, item.get(c), thr)
            for c, _l, _d, _g in KIND_METRIC_COLUMNS.get(k, KIND_METRIC_COLUMNS.get("", []))
        }
        # 从 detail JSON 里取负对照与漂移标记 —— 决定这个分数能不能当依据
        drift, ctrl = None, None
        if "detail" in cols:
            try:
                obj = json.loads(r["detail"] or "{}")
                drift = obj.get("fingerprint_drift") or None
                nc = obj.get("negative_control") or {}
                ctrl = nc.get("recall") if isinstance(nc, dict) else None
            except Exception:
                # detail 不是合法 JSON（别的评测存的是另一种结构）：
                # 解析失败只说明"取不到负对照"，不该让整个接口失败。
                drift, ctrl = None, None
        item["fingerprint_drift"] = drift
        item["negative_control"] = ctrl
        # ★ trustworthy = 有负对照 且 无漂移。没有负对照就没有
        #   "指标在测真实对齐关系"的证据（那是本工程评测体系的核心纪律）。
        item["trustworthy"] = bool(ctrl) and not drift
        # O3：快照表里的键值并入 item（静态列没有的键在此透出 —— 扩展通道）。
        # 一次一查（每报告 ≤10 行），避免跨报告大 JOIN 的复杂度。
        for m in fetch_metrics(conn, r["id"]):
            mk = m["metric_key"]
            if mk not in item or item.get(mk) is None:
                item[mk] = m["value"]
            item.setdefault("metric_status", {})[mk] = m["status"] or \
                item["metric_status"].get(mk, "")
        bucket.append(item)

    return {
        "ok": True,
        "kinds": sorted(by_kind.keys()),
        "counts": {k: len(v) for k, v in by_kind.items()},
        "history": by_kind,
        "migrated": True,
        "note": ("" if by_kind else
                 "暂无评测记录。生成方式："
                 "tools/eval/build_evalset.py → tools/eval/run_eval.py --control "
                 "→ tools/eval/save_report.py"),
    }


# ══════════════════════════════════════════════════════════════════════
# 指标快照表（2026-10-23 评估 O3 / P0-3）：指标的扩展通道
# ══════════════════════════════════════════════════════════════════════
def write_metric_rows(conn, report_id, kind, values):
    """把一次评测的指标写进 eval_metrics 快照表（每指标一行）。

    values: {metric_key: value}（None 的键跳过 —— NULL=未测，区别于 0 分）。
    direction / gate_key 来自 metric_columns_for(kind)（单一真源）；threshold
    与 status 在**落库时**算好存快照（阈值后来改了不影响历史三色）。

    表不存在（老库未迁移）⇒ 静默跳过并返回 0 —— 快照表是扩展通道不是主键路，
    固定列照常写；不能因为辅助通道失败让整个评测落库失败。
    """
    try:
        conn.execute("SELECT 1 FROM eval_metrics LIMIT 1")
    except Exception:
        return 0
    spec = {c["key"]: c for c in metric_columns_for(kind)}
    thr_map = _gate_thresholds()
    n = 0
    for key, val in (values or {}).items():
        if val is None:
            continue
        s = spec.get(key, {"direction": "high", "gate_key": ""})
        gate_key = s.get("gate_key") or ""
        threshold = thr_map.get(gate_key) if gate_key else None
        status = metric_status_for(kind, key, val, thr_map)
        conn.execute(
            "INSERT INTO eval_metrics (report_id, eval_kind, metric_key, value, "
            "direction, gate_key, threshold, status) VALUES (?,?,?,?,?,?,?,?)",
            (report_id, kind or "", key, val,
             s.get("direction") or "high", gate_key, threshold, status))
        n += 1
    return n


def fetch_metrics(conn, report_id):
    """单份报告的指标快照行（下钻端点用；表不存在返回空列表）。"""
    try:
        rows = conn.execute(
            "SELECT metric_key, value, direction, gate_key, threshold, status "
            "FROM eval_metrics WHERE report_id=? ORDER BY id", (report_id,)).fetchall()
    except Exception:
        return []
    return [dict(r) for r in rows]


# ══════════════════════════════════════════════════════════════════════
# 单行报告下钻（2026-10-22 晚）：总览 → 证据明细的通路
# ══════════════════════════════════════════════════════════════════════
def fetch_report_by_id(conn, row_id):
    """按行 id 取全量报告（detail JSON 解析后返回）。

    为什么单独一个函数而不是把 detail 塞进 fetch_history：history 是列表
    聚合（一次 20 行 × 每行 detail 可达 200KB），下钻是单行按需——列表
    接口背着全量 JSON 会把首页响应撑爆。分工即性能。

    返回 {ok, id, eval_kind, kind_label, doc_name, created_at, detail}；
    行不存在 / detail 非合法 JSON ⇒ ok=False + error（**不伪造空报告**——
    空报告会被前端渲染成"这条没问题"，比读不到更坏）。
    """
    try:
        rid = int(row_id)
    except (TypeError, ValueError):
        return {"ok": False, "error": "id 参数非法: %r" % (row_id,)}
    cols = _columns(conn)
    if not cols or "detail" not in cols:
        return {"ok": False, "error": "eval_reports 表不可用（请先跑 init_db）"}
    row = conn.execute(
        "SELECT id, eval_kind, doc_name, created_at, detail FROM eval_reports "
        "WHERE id=?", (rid,)).fetchone()
    if not row:
        return {"ok": False, "error": "评测报告不存在: id=%s" % rid}
    try:
        detail = json.loads(row["detail"] or "{}")
        if not isinstance(detail, dict):
            raise ValueError("detail 不是对象")
    except Exception as e:
        return {"ok": False, "error": "detail 解析失败: %s" % e,
                "id": rid, "eval_kind": row["eval_kind"]}
    kind = (row["eval_kind"] or "") or ""
    return {"ok": True, "id": row["id"], "eval_kind": kind,
            "kind_label": EVAL_KIND_LABELS.get(kind, kind or "未标注类型"),
            "doc_name": row["doc_name"], "created_at": row["created_at"],
            "metrics": fetch_metrics(conn, rid),
            "detail": detail}


# ══════════════════════════════════════════════════════════════════════
# 门禁接线状态
# ══════════════════════════════════════════════════════════════════════
def _exemption_reason(path):
    """读门禁文件头部的 CI-OPTIONAL 豁免理由（与 verify_gate_wiring 同口径）。"""
    try:
        with open(path, encoding="utf-8") as f:
            head = f.read(4000)
    except Exception:
        return None
    m = re.search(r"CI-OPTIONAL\s*[:：]\s*(.+)", head)
    return m.group(1).strip() if m else None


def fetch_gate_wiring(root):
    """门禁接线状态：哪些进了 CI、哪些带理由豁免、哪些漏了。

    与 `tools/verify/verify_gate_wiring.py` **同源同口径**（同一份扫描 +
    同一份 ci.yml 执行行匹配）⇒ 页面显示的与门禁自己判的一致。
    """
    vd = os.path.join(root, "tools", "verify")
    gates = []
    for p in sorted(glob.glob(os.path.join(vd, "*.py"))):
        n = os.path.basename(p)
        if not (n.startswith("verify_") or n.startswith("audit_")):
            continue
        gates.append((n[:-3], p))

    ci_path = os.path.join(root, ".github", "workflows", "ci.yml")
    ci_text = ""
    if os.path.exists(ci_path):
        try:
            with open(ci_path, encoding="utf-8") as f:
                ci_text = f.read()
        except Exception:
            ci_text = ""

    def wired(name):
        # 与 verify_gate_wiring 同一判据：出现在 **执行行** 里才算接线
        return ("tools/verify/%s.py" % name) in ci_text or ("tools/%s.py" % name) in ci_text

    rows = []
    for name, path in gates:
        w = wired(name)
        reason = _exemption_reason(path)
        if w:
            status = "wired"
        elif reason:
            status = "exempted"
        else:
            status = "unwired"
        rows.append({
            "name": name,
            "status": status,
            "exemption_reason": reason if status == "exempted" else None,
        })

    counts = {
        "total": len(rows),
        "wired": sum(1 for r in rows if r["status"] == "wired"),
        "exempted": sum(1 for r in rows if r["status"] == "exempted"),
        "unwired": sum(1 for r in rows if r["status"] == "unwired"),
    }
    return {"ok": True, "counts": counts, "gates": rows,
            "ci_present": bool(ci_text)}
