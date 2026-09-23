"""知识库域：/api/knowledge/*"""
import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from core.deps import db_session, current_user, require_permission, require_any_permission
from core import typevocab
from knowledge_engine import QueryRouter  # P1: 双引擎路由统计
from repositories.knowledge_repo import KnowledgeRepo
from repositories.commit_repo import CommitRepo
from core.audit import audit
from core.branch_rules import check_writable, is_release_family
from models import (
    EntityIn,
    BatchReviewIn,
    GraphNodeIn,
    GraphEdgeIn,
    OntologyTypeIn,
    V2GExtractIn,
    V2GConfirmIn,
    V2GRejectIn,
    V2GUpdateIn,
    SysMLIn,
    MergeIn,
    RetrieveIn,
)

router = APIRouter(tags=["知识库"])

RELEASE_BRANCH = "release"

# 写操作权限：设计师（kb_review:modify）与知识工程师（kb_ontology:edit）双角色放行
WRITE_PERMS = [("kb_review", "modify"), ("kb_ontology", "edit")]

# ---- 模块级常量（拆分自原文件中部）----
_FALLBACK_STOP_WORDS = {
    # 中文常见停用词（检索 query 中的高频虚词/系统词）
    "的", "了", "是", "和", "与", "在", "我", "你", "他", "她", "它", "我们", "你们", "他们",
    "这", "那", "一个", "这个", "那个", "什么", "怎么", "如何", "为什么", "请", "帮我", "帮",
    "一下", "有", "没有", "吗", "呢", "吧", "啊", "哦", "嗯", "还", "就", "都", "很", "也",
    "及", "或", "等", "对", "从", "向", "把", "被", "让", "要", "会", "能", "可以", "不能",
    "检索", "查询", "搜索", "知识", "文档", "库",
    # 英文常见停用词
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "of", "for", "to", "in",
    "on", "at", "with", "by", "from", "what", "how", "why", "when", "where", "which", "who",
    "please", "help", "me", "my", "you", "your", "it", "this", "that", "and", "or", "not",
    "do", "does", "did", "can", "could", "should", "would", "about", "into", "over", "under",
    "between", "i", "we", "they", "he", "she",
}

# ---- 模块级私有 helper（拆分自原文件中部，供各分片共用）----

def _actor(user) -> str:
    """审计操作人：优先取当前登录用户显示名；匿名请求兜底保留原默认名「王工」。"""
    return (user or {}).get("display_name") or "王工"


def _is_release_branch(conn, branch: str | None) -> bool:
    """判定分支是否为「已发布(release)」分支：查 branches 表 branch_type，兼容字面 'release'/'release/xxx'。"""
    if not branch:
        return False
    if branch == RELEASE_BRANCH or branch.startswith(RELEASE_BRANCH + "/"):
        return True
    btype = ""
    try:
        row = conn.execute("SELECT branch_type FROM branches WHERE name=?", (branch,)).fetchone()
        if row is not None:
            btype = row["branch_type"] or ""
    except Exception:
        btype = ""
    # P0-2：与 core.branch_rules 共用同一判据，避免两套语义漂移
    return is_release_family(branch, btype)


def _release_guard(conn, branch: str | None) -> str | None:
    """直写门：实体/关系/文档写入前的分支保护校验（**9 处**调用共用：entities 2 / graph 5 / graph_query 2）。

    P0-2（2026-09-23）：由「release 硬编码只读」改为读分支保护规则 writable
    （core.branch_rules.check_writable）；默认行为不变 —— release 仍只读、其余分支可写。
    """
    return check_writable(conn, branch)


def _entity_dup_warning(conn, name: str, threshold: float = 0.62) -> list:
    """按归一化名精确 + Levenshtein 模糊双通道查已有实体（排除 deprecated）。"""
    if not name or not str(name).strip():
        return []
    from text_normalize import normalize_name
    from entity_resolver import _fuzzy_ratio
    nm = normalize_name(str(name).strip())
    # P0-1 修正：消歧须跨分支查（同 id 多分支行按 name 去重）——personal 草稿也可能与待建实体重名
    rows = conn.execute(
        "SELECT id, name, entity_type, branch, status FROM entities "
        "WHERE status != 'deprecated' ORDER BY created_at DESC LIMIT 5000").fetchall()
    warns, seen = [], set()
    for r in rows:
        eid, ename = str(r["id"]), r["name"] or ""
        if not ename or eid in seen:
            continue
        if normalize_name(ename) == nm:
            score = 1.0
        else:
            score = _fuzzy_ratio(nm, normalize_name(ename))
            if score < threshold:
                continue
        seen.add(eid)
        warns.append({"id": eid, "name": ename, "entity_type": r["entity_type"],
                      "branch": r["branch"], "score": round(score, 2)})
        if len(warns) >= 5:
            break
    return warns


def _dprop_proj(row) -> dict:
    """把本体属性行投影为实例编辑器要的字段集合。"""
    try:
        props = json.loads(row["properties"] or "{}") or {}
    except Exception:
        props = {}
    try:
        cons = json.loads(row["constraints"] or "{}") or {}
    except Exception:
        cons = {}
    return {
        "name": row["name"],
        # P1-8（2026-09-07）：类型声明统一走 typevocab 规范化输出（xsd:*），兼容
        # properties.type 与 constraints.xsd_type 两种历史写法——实例编辑器与
        # xsd 校验器此前只认其一，导致独立属性声明的类型到不了消费端。
        "type": (typevocab.xsd_of(
            (props.get("type") if isinstance(props, dict) and isinstance(props.get("type"), str) else None)
            or cons.get("xsd_type")) or "xsd:string"),
        "required": bool(cons.get("required") or (isinstance(props, dict) and props.get("required"))),
        "allowed_values": cons.get("allowed_values") or [],
        "domain_classes": cons.get("domain_classes") or [],
        # P0-3（2026-09-07）：单位/量纲维度（存储于 constraints.unit / quantity_kind）
        "unit": cons.get("unit") or "",
        "quantity_kind": cons.get("quantity_kind") or "",
        "description": row["description"] or "",
    }


def _log_graph_edit(conn, op: str, node_id: str, payload: dict, operator: str) -> None:
    conn.execute(
        "INSERT INTO graph_edit_logs (op, node_id, payload, operator) VALUES (?,?,?,?)",
        (op, node_id, json.dumps(payload, ensure_ascii=False), operator))
    conn.commit()


def _fallback_topics(conn, days: int = 30, top: int = 20) -> list:
    """近 N 天检索 fallback（route=vector/mixed 或 reason 含 no_hit）query 的中/英词频 TOP N。

    简单按正则拆词：中文取连续 ≥2 字片段，英文/数字取 ≥2 字符 token，
    过滤常见停用词后计数（英文统一小写归一）。
    """
    import re
    from collections import Counter
    rows = conn.execute(
        "SELECT query FROM query_routing_stats "
        "WHERE created_at >= datetime('now', ?) AND (route IN ('vector','mixed') OR reason LIKE '%no_hit%')",
        (f"-{days} days",)).fetchall()
    counter = Counter()
    for r in rows:
        q = (r["query"] or "").strip()
        if not q:
            continue
        for w in re.findall(r"[\u4e00-\u9fa5]{2,}|[A-Za-z0-9]{2,}", q):
            wl = w.lower()
            if wl in _FALLBACK_STOP_WORDS:
                continue
            counter[wl] += 1
    return [{"word": w, "count": c} for w, c in counter.most_common(top)]


def _validate_ont_parent(conn, parent_id, type_kind, exclude_id=None):
    """P0-1：校验父类型合法（父存在 + kind 兼容 + 不成环）。返回错误列表（空=通过）。"""
    errs = []
    if not parent_id:
        return errs
    if type_kind == "relation":
        errs.append("关系类型不支持父类型（subClassOf 仅实体/属性类型）")
        return errs
    parent = conn.execute("SELECT id, name, type_kind FROM ontology_types WHERE id=?", (parent_id,)).fetchone()
    if not parent:
        errs.append(f"父类型不存在: id={parent_id}")
        return errs
    if parent["type_kind"] != type_kind:
        errs.append(f"父类型 '{parent['name']}' 类型种类({parent['type_kind']})与当前({type_kind})不一致")
        return errs
    # 环检测：沿父链向上，遇 exclude_id（自身）即环
    seen, cur = set(), parent_id
    while cur:
        if exclude_id is not None and cur == exclude_id:
            errs.append(f"父类型 '{parent['name']}' 形成循环层级（子类链回指自身）")
            break
        if cur in seen:
            errs.append("父类型层级存在循环引用")
            break
        seen.add(cur)
        row = conn.execute("SELECT parent_id FROM ontology_types WHERE id=?", (cur,)).fetchone()
        cur = row["parent_id"] if row else None
    return errs


def _log_ont_change(conn, type_id, action, before, after, user):
    """FR-KG-4 补 G7：本体类型变更留痕（before/after 为整行 dict，JSON 序列化落库）。

    与 audit 同风格：跟随请求级事务，由 db_session teardown 统一提交。
    """
    conn.execute(
        "INSERT INTO ontology_change_logs (type_id, action, before, after, operator) VALUES (?,?,?,?,?)",
        (type_id, action,
         json.dumps(before, ensure_ascii=False, default=str),
         json.dumps(after, ensure_ascii=False, default=str),
         _actor(user)))


def _ont_props_keys(d):
    """取类型 properties 的键集合（兼容 dict 或 JSON 字符串）。"""
    if isinstance(d, str):
        try:
            d = json.loads(d or "{}")
        except Exception:
            return set()
    return set((d or {}).keys()) if isinstance(d, dict) else set()


def _ont_version_bump(conn, change_type, before=None, after=None, summary="", operator=""):
    """本体整体版本自动递增：读当前版本 → 按变更语义计算新版本 → 写版本链新行。

    跟随请求级事务（调用方 db_session teardown 统一 commit）；无历史版本时基线 v1.0.0。

    ⚠️ 2026-09-08 版本语义收敛：编辑路径（add/update/delete/import/apply）已不再调用本函数——
    编辑态只写 ontology_change_logs 变更留痕；版本号仅在发布（/version/publish 按 diff 定 SemVer、
    /version/release 发布既有行）时生成升级。函数保留供历史数据修复/回填脚本使用。
    """
    row = conn.execute(
        "SELECT major, minor, patch FROM ontology_versions ORDER BY id DESC LIMIT 1").fetchone()
    major, minor, patch = (row["major"], row["minor"], row["patch"]) if row else (1, 0, 0)
    before = dict(before) if isinstance(before, dict) else (dict(before) if before else {})
    after = dict(after) if isinstance(after, dict) else (dict(after) if after else {})
    if change_type == "delete":
        major, minor, patch = major + 1, 0, 0
    elif change_type == "add":
        major, minor, patch = major, minor + 1, 0
    elif change_type == "update":
        if before.get("name") != after.get("name"):
            major, minor, patch = major + 1, 0, 0      # 重命名 = 破坏性
        elif _ont_props_keys(after.get("properties")) - _ont_props_keys(before.get("properties")):
            major, minor, patch = major, minor + 1, 0  # 新增属性 = 非破坏性增强
        else:
            major, minor, patch = major, minor, patch + 1
    elif change_type in ("apply", "import"):
        major, minor, patch = major, minor + 1, 0
    conn.execute(
        "INSERT INTO ontology_versions (version_label, major, minor, patch, change_type, summary, operator)"
        " VALUES (?,?,?,?,?,?,?)",
        (f"v{major}.{minor}.{patch}", major, minor, patch, change_type,
         (summary or "")[:200], operator or ""))


def _normalize_relation_domain_range(conn, body) -> list:
    """P1-8（2026-09-07，P0-2 双轨归一）：relation 的 constraints.domain/range
    （单值旧写法，UI 历史产出）归一进 allowed_values.src/tgt（列表，唯一事实源，
    SHACL 消费方），原键移除并校验指向存在性。返回错误列表（空=通过）。"""
    cons = body.constraints if isinstance(body.constraints, dict) else {}
    if body.type_kind != "relation":
        for k in ("domain", "range"):   # 非关系类型误带 → 移除防脏写
            cons.pop(k, None)
        return []
    dom, rng = cons.pop("domain", None), cons.pop("range", None)
    if not dom and not rng:
        return []
    ent_names = {r["name"] for r in conn.execute(
        "SELECT name FROM ontology_types WHERE type_kind='entity'").fetchall()}
    errs = [f"定义域/值域指向不存在的实体类型: {v}"
            for v in (dom, rng) if v and v not in ent_names]
    if errs:
        return errs

    def _as_list(x):
        if x is None:
            return []
        return [x] if isinstance(x, str) else list(x)

    av = cons.get("allowed_values") if isinstance(cons.get("allowed_values"), dict) else {}
    if dom:
        merged = _as_list(av.get("src"))
        if dom not in merged:
            merged.append(dom)
        av["src"] = merged
    if rng:
        merged = _as_list(av.get("tgt"))
        if rng not in merged:
            merged.append(rng)
        av["tgt"] = merged
    if av:
        cons["allowed_values"] = av
    return []


def _ontology_check(conn) -> dict:
    """本体一致性校验（内部复用）：循环继承 / 悬空 parent / 孤立类 / 关系缺 dom-range / 重名。

    2026-09-02 抽取：validate 端点与发布流程（release 前置拦截）共用。
    """
    rows = conn.execute("SELECT id, name, type_kind, parent_id, constraints FROM ontology_types").fetchall()
    ents = conn.execute("SELECT entity_type, COUNT(*) c FROM entities WHERE status!='deprecated' GROUP BY entity_type").fetchall()
    inst_cnt = {r["entity_type"]: r["c"] for r in ents}
    by_id = {str(r["id"]): r for r in rows}
    issues = []
    for t in rows:
        if t["type_kind"] in ("entity", "attribute"):
            seen = {str(t["id"])}; cur = t["parent_id"]; hop = 0
            while cur and hop < 50:
                if str(cur) in seen:
                    issues.append({"type": "cycle", "severity": "high", "name": t["name"], "message": "循环继承（→ … → 自身）"}); break
                seen.add(str(cur)); pp = by_id.get(str(cur)); cur = pp["parent_id"] if pp else None; hop += 1
    for t in rows:
        if t["parent_id"] and str(t["parent_id"]) not in by_id:
            issues.append({"type": "dangling_parent", "severity": "high", "name": t["name"],
                           "message": f"悬空父类：parent_id={t['parent_id']} 不存在"})
    for t in rows:
        if t["type_kind"] != "entity":
            continue
        has_inst = inst_cnt.get(t["name"], 0) > 0
        has_child = any(str(x["parent_id"] or "") == str(t["id"]) for x in rows)
        if not has_inst and not has_child:
            issues.append({"type": "isolated", "severity": "low", "name": t["name"], "message": "孤立类：无实例且无子类"})
    ent_names = {x["name"] for x in rows if x["type_kind"] == "entity"}
    for t in rows:
        if t["type_kind"] != "relation":
            continue
        try:
            c = json.loads(t["constraints"] or "{}")
        except Exception:
            c = {}
        # 2026-09-02 修复：定义域/值域实际存于 allowed_values.src/tgt（兼容旧字段 domain/range）
        av = c.get("allowed_values") or {}
        dom = c.get("domain") if c.get("domain") is not None else av.get("src")
        rng = c.get("range") if c.get("range") is not None else av.get("tgt")

        def _norm(v):
            vals = v if isinstance(v, list) else ([v] if v else [])
            return [str(x).strip() for x in vals if str(x).strip() and str(x) != "?"]

        dom_l, rng_l = _norm(dom), _norm(rng)
        if not dom_l and not rng_l:
            issues.append({"type": "missing_dom_range", "severity": "low", "name": t["name"], "message": "对象属性未定义定义域/值域"})
        else:
            bad = sorted({x for x in dom_l + rng_l if x not in ent_names})
            if bad:
                issues.append({"type": "bad_dom_range", "severity": "high", "name": t["name"],
                               "message": f"定义域/值域指向不存在的实体类型: {'、'.join(bad)}"})
    seen_names = {}
    for t in rows:
        key = (t["type_kind"], t["name"])
        if key in seen_names:
            issues.append({"type": "duplicate", "severity": "high", "name": t["name"], "message": f"重名类型：{t['name']}（{t['type_kind']}）出现多次"})
        seen_names[key] = t["id"]
    return {"ok": True, "total": len(rows), "issues": issues,
            "high": sum(1 for x in issues if x["severity"] == "high"),
            "low": sum(1 for x in issues if x["severity"] == "low")}


def _snapshot_diff(conn, version_id: int) -> dict:
    """2026-09-02 P1：版本 diff 摘要——对比该版本与相邻前一个已发布版本的快照。

    返回 {added:[类型名], removed:[类型名], dom_changed:N}（removed 覆盖改名=旧名消失）。
    """
    def _snap(vid):
        if vid is None:
            return set()
        return {(r["name"], r["type_kind"]) for r in conn.execute(
            "SELECT name, type_kind FROM ontology_version_snapshots WHERE version_id=?", (vid,)).fetchall()}

    cur = _snap(version_id)
    prev_v = conn.execute(
        "SELECT id FROM ontology_versions WHERE status='released' AND id<? ORDER BY id DESC LIMIT 1",
        (version_id,)).fetchone()
    prev = _snap(prev_v["id"] if prev_v else None)
    added = sorted({n for n, k in cur - prev})
    removed = sorted({n for n, k in prev - cur})
    dom_changed = 0
    if prev_v:
        # 域值域变更：对比同名同类型行的 allowed_values（复用 release 判定逻辑的宽松版）
        prev_rows = conn.execute(
            "SELECT name, type_kind, constraints FROM ontology_version_snapshots WHERE version_id=?",
            (prev_v["id"],)).fetchall()
        cur_rows = conn.execute(
            "SELECT name, type_kind, constraints FROM ontology_version_snapshots WHERE version_id=?",
            (version_id,)).fetchall()
        prev_map = {(r["name"], r["type_kind"]): r["constraints"] for r in prev_rows}
        cur_map = {(r["name"], r["type_kind"]): r["constraints"] for r in cur_rows}

        def _dr(c):
            try:
                c = json.loads(c or "{}")
            except Exception:
                c = {}
            av = c.get("allowed_values") or {}
            out = []
            for side in ("src", "tgt"):
                v = av.get(side)
                out.append(sorted({str(x).strip() for x in (v if isinstance(v, list) else ([v] if v else []))
                                   if str(x).strip() and str(x) != "?"}))
            return out
        for key in (set(prev_map) & set(cur_map)):
            if _dr(prev_map[key]) != _dr(cur_map[key]):
                dom_changed += 1
    return {"added": added, "removed": removed, "dom_changed": dom_changed}


def _count_dom_range_changed(prev_rows, cur_rows) -> int:
    """对比上一快照行与当前类型的定义域/值域，返回发生变更的类型数。"""
    def _norm(c):
        try:
            c = json.loads(c or "{}")
        except Exception:
            c = {}
        av = c.get("allowed_values") or {}
        out = []
        for side in ("src", "tgt"):
            v = av.get(side)
            out.append(sorted({str(x).strip() for x in (v if isinstance(v, list) else ([v] if v else []))
                               if str(x).strip() and str(x) != "?"}))
        return out
    cur_map = {(r["name"], r["type_kind"]): r["constraints"] for r in cur_rows}
    n = 0
    for p in prev_rows:
        key = (p["name"], p["type_kind"])
        if key in cur_map and _norm(p["constraints"]) != _norm(cur_map[key]):
            n += 1
    return n


def _snapshot_dom_range_diff(conn, prev_rows, cur_rows) -> bool:
    """对比上一已发布快照与当前类型的定义域/值域：有变化 → 破坏性（major）。"""
    def _key(name, kind):
        for r in cur_rows:
            if r["name"] == name and r["type_kind"] == kind:
                return r
        return None
    for p in prev_rows:
        r = _key(p["name"], p["type_kind"])
        if r is None:
            continue
        def _dr(row, side):
            try:
                c = json.loads(row["constraints"] or "{}")
            except Exception:
                c = {}
            av = c.get("allowed_values") or {}
            v = c.get("domain") if c.get("domain") is not None and side == "src" else (c.get("range") if side == "tgt" else av.get(side))
            if v is None and side in ("src", "tgt"):
                v = av.get(side)
            return sorted({str(x).strip() for x in (v if isinstance(v, list) else ([v] if v else [])) if str(x).strip() and str(x) != "?"})
        if _dr(p, "src") != _dr(r, "src") or _dr(p, "tgt") != _dr(r, "tgt"):
            return True
    return False


def _active_ont_rows(conn):
    """2026-09-02 快照消费：返回消费侧应使用的类型行（最新已发布 active 快照；无发布或无快照回退当前表）。

    消费类端点（语义注入/图谱 Schema/导出）用此读取；编辑/校验类（types 树/validate）仍读当前表。
    """
    v = conn.execute(
        "SELECT id FROM ontology_versions WHERE active=1 ORDER BY id DESC LIMIT 1").fetchone()
    if v:
        rows = conn.execute(
            "SELECT type_id AS id, name, type_kind, parent_id, properties, constraints, "
            "description, icon, color, iri FROM ontology_version_snapshots WHERE version_id=?",
            (v["id"],)).fetchall()
        if rows:  # active 版本有快照 → 消费快照
            return rows
        # active 版本无快照（快照治理上线前的存量 released）→ 回退当前表，避免消费空数据
    return conn.execute(
        "SELECT * FROM ontology_types").fetchall()


def _filter_ont_rows_for_type(rows, type_id: int):
    """2026-09-02 四轮调整：节点级 OWL/SHACL——过滤出所选类型及其直接语境。

    语境 = 父类链 + （关系）域/值域实体 + （实体）涉该实体的关系及其对端实体。
    过滤失败（找不到该类型）回退全量 rows。
    """
    import json as _json
    sel = next((r for r in rows if r["id"] == type_id), None)
    if not sel:
        return rows
    keep = {sel["name"]}
    by_id = {r["id"]: r for r in rows}
    p = sel["parent_id"]                     # 父类链
    while p and p in by_id:
        keep.add(by_id[p]["name"])
        p = by_id[p]["parent_id"]

    def _av(r):
        try:
            return (_json.loads(r["constraints"] or "{}").get("allowed_values") or {})
        except Exception:
            return {}

    if sel["type_kind"] == "relation":       # 关系：域/值域实体
        av = _av(sel)
        for k in ("src", "tgt"):
            v = av.get(k)
            if isinstance(v, str):
                keep.add(v)
            elif isinstance(v, list):
                keep.update(v)
    if sel["type_kind"] == "entity":         # 实体：涉该实体的关系 + 对端实体
        for r in rows:
            if r["type_kind"] != "relation":
                continue
            av = _av(r)
            s, t = av.get("src"), av.get("tgt")
            s = s if isinstance(s, str) else (s[0] if isinstance(s, list) and s else None)
            t = t if isinstance(t, str) else (t[0] if isinstance(t, list) and t else None)
            if sel["name"] in (s, t):
                keep.add(r["name"])
                keep.update(v for v in (s, t) if v)
    return [r for r in rows if r["name"] in keep]


def triples_stats(conn=Depends(db_session), user=Depends(current_user)):
    """三元组生命周期统计。"""
    from triple_store import stats
    return stats(conn)


def v2g_fuse_config(body: dict, conn=Depends(db_session),
                    user=Depends(require_permission("kb_config", "manage"))):
    """融合闸配置（修 C7 硬编码）：阈值/权重/开关入 settings 表，实时生效。

    body: {enabled?, auto_threshold?, review_threshold?, quality_gate?, weights?{name,vector,props,type}}"""
    from services.knowledge_service import KnowledgeService
    return KnowledgeService(conn).v2g_fuse_config(
        enabled=body.get("enabled"), auto_threshold=body.get("auto_threshold"),
        review_threshold=body.get("review_threshold"), quality_gate=body.get("quality_gate"),
        weights=body.get("weights"), actor=_actor(user))

__all__ = ['router', 'json', 'Optional', 'Depends', 'HTTPException', 'JSONResponse', 'db_session', 'current_user', 'require_permission', 'require_any_permission', 'typevocab', 'QueryRouter', 'KnowledgeRepo', 'CommitRepo', 'audit', 'EntityIn', 'BatchReviewIn', 'GraphNodeIn', 'GraphEdgeIn', 'OntologyTypeIn', 'V2GExtractIn', 'V2GConfirmIn', 'V2GRejectIn', 'V2GUpdateIn', 'SysMLIn', 'MergeIn', 'RetrieveIn', 'RELEASE_BRANCH', 'WRITE_PERMS', '_FALLBACK_STOP_WORDS', '_actor', '_is_release_branch', '_release_guard', '_entity_dup_warning', '_dprop_proj', '_log_graph_edit', '_fallback_topics', '_validate_ont_parent', '_log_ont_change', '_ont_props_keys', '_ont_version_bump', '_normalize_relation_domain_range', '_ontology_check', '_snapshot_diff', '_count_dom_range_changed', '_snapshot_dom_range_diff', '_active_ont_rows', '_filter_ont_rows_for_type', 'triples_stats', 'v2g_fuse_config']
