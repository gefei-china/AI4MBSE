"""知识图谱 / 实体的**统一写入口**（P0-0）。

## 为什么需要这个模块

改造前，同一个「写一个实体」在系统里有 **20+ 个入口各写一遍**（`entities.py` 6、
`graph.py` 7、`graph_workspace.py` 7 …），每个入口自己决定要不要校验类型、要不要过本体约束、
要不要留痕。Palantir 的做法相反：**默认只允许通过 Action 编辑**（Object Type 的
"only allow edits via actions"），其他写入方式明确标为不推荐用于新用途。

后果（实测，2026-11-21）：
  · `PUT /api/knowledge/entities/{id}` 与 `POST /api/knowledge/graph/nodes` 都调
    `validate_node`；而 `POST /api/knowledge/entities` **只查类型存在性、不校验属性**
    ⇒ 同一份本体，两条写路径两把尺子；
  · 执行后核验（P0-3）无处可挂 —— 挂在 Agent 工具链上只覆盖 Agent 路径，
    前端在图谱编辑器里直接 `api(PUT ...)` 全部绕过。

## 本模块负责什么（且只负责这些）

1. **本体类型门禁** —— 类型必须已在本体声明（空类型放行，历史行为）
2. **属性约束校验** —— `OntologyValidator.validate_node`（与图谱入口同一把尺子）
3. **写入** —— 委托 `KnowledgeRepo`（版本化 fork / 历史行仍由仓储负责，不重复实现）
4. **留痕** —— `graph_edit_logs` + `audit_logs`（哈希链）
5. **执行后核验（post-condition）** —— 回读并断言「写的东西真的落成了那样」：
   版本行存在且 `is_current=1`、名称/类型/属性一致。**这是 P0-3 的挂点。**

## 判据纪律

「校验」只在这里做一次。任何端点**不得**在自己函数体里再写一份 `validate_node` 或
类型检查 —— 那会让两条路径再次漂移。门禁 `verify_single_write_entry.py` 会扫源码断言。

## 返回约定

成功：`{"ok": True, "id": ..., "post": {"verified": True, "checks": [...]}}`
失败：`{"ok": False, "stage": "...", "error": "..."}`
  - `stage="type_gate"` / `"validate"` / `"write"` / `"not_found"` —— **写入未发生**
  - `stage="post_condition"` 且 `applied=True` —— **写入已发生，但效果未能证实**。
    这是最需要暴露的一类：接口返回 200、库里却不是用户以为的样子。

## A9（2026-11-22）：核验第 4 态 `unknown`

改造前只有 `verified: bool`，查询异常（锁等待超时/连接断）与"回读到了但不一致"
都落到 `verified=False`。调用方据此判断"失败并重试"，而**请求已发出时写没写成
客户端根本无法区分** —— 重试会造成重复实体/重复关系。现在 `post` 增加
`verify_status` 四态（`verified`/`failed`/`unknown`/`skipped`）：

  · `verified` —— 回读成功且全部断言通过；
  · `failed`   —— 回读成功但有断言不成立（**确定未达成**，可安全重做）；
  · `unknown`  —— 查询异常/回读不到（**可能已生效**，`retry_safe=False`，先查询确认）；
  · `skipped`  —— 本操作未配置后置条件（保留态，当前统一入口不使用）。

`verified` 布尔**保留且语义不变**（仅 `verify_status=="verified"` 为真），
所有既有调用方的 `if not post["verified"]` 与前端 `post` 透传零回归。
"""
from __future__ import annotations

import json

#: 执行后核验的四态（A9）。`skipped` 为保留态：未配置后置条件时不可把"没验"
#: 伪装成"验过"，但目前统一入口的每次写都带后置断言，故不产生该状态。
VERIFY_STATUSES = ("verified", "failed", "unknown", "skipped")

#: unknown 的对外硬提示：必须让模型/调用方看到"不要重试"，不能只靠布尔 False
UNKNOWN_HINT = "效果无法判定（可能已写入），不要重试，先查询确认当前状态"

#: 本入口支持的写操作
#: `create` / `update` —— 实体写入（走 `entity_versions` + `entities`）
#: `delete`            —— 实体删除（**核验是反向断言：行必须消失**）
#: `edge_create`       —— 关系写入（走 `relations`）
#: `edge_delete`       —— 关系删除（同样反向断言）
WRITE_OPS = ("create", "update", "delete", "edge_create", "edge_update", "edge_delete")

#: 已迁移到统一入口的写端点（门禁 G2/G3 据此断言；`tools/verify/verify_single_write_entry.py`）
MIGRATED_ENDPOINTS = (
    "POST /api/knowledge/entities",
    "PUT /api/knowledge/entities/{entity_id}",
    "POST /api/knowledge/graph/nodes",
    "PUT /api/knowledge/graph/nodes/{nid}",
    "DELETE /api/knowledge/graph/nodes/{nid}",
    "POST /api/knowledge/graph/edges",
    "PUT /api/knowledge/graph/edges/{eid}",
    "DELETE /api/knowledge/graph/edges/{eid}",
)


def check_entity_type(conn, etype: str):
    """类型必须已在本体声明。返回 None=通过；str=错误文案。

    空类型放行 —— 历史行为（存量数据里确实有无类型实体），要收紧需先做数据治理，
    不在本次范围内，写在这里是为了让「放行是决定，不是遗漏」显式可见。
    """
    etype = (etype or "").strip()
    if not etype:
        return None
    from ontology_semantics import GraphStore
    v = GraphStore(conn).validator
    if etype not in v._load_types():
        known = v.entity_types()
        if known:
            return f"实体类型 '{etype}' 不在本体中（可用: {', '.join(known[:8])}）——请先在本体模型声明该类型"
        return "本体未配置实体类型，请先在本体模型声明"
    return None


def validate_props(conn, entity_type: str, properties: dict, branch: str,
                   entity_id: str = None) -> list:
    """属性约束校验（唯一实现；端点不得另写一份）。

    `exclude_id` 必须传：唯一性只在分支内判定，且比对时要排除自身，
    否则编辑已有实例时它自己就在库里，每次保存都被判「违反唯一约束」。
    """
    from ontology_semantics import GraphStore
    try:
        return GraphStore(conn).validator.validate_node(
            entity_type, properties or {}, branch or "dev", exclude_id=entity_id) or []
    except Exception as e:
        # 校验器异常**不能**当成"通过"：那等于把校验关掉了还不说。
        return [f"本体校验器异常（写入被拒绝）：{str(e)[:160]}"]


def _load_props(v) -> dict:
    if isinstance(v, dict):
        return v
    try:
        d = json.loads(v or "{}")
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _post_result(checks: list, op: str, *, indeterminate: bool = False) -> dict:
    """把核验项归一成四态返回。

    `indeterminate=True` 表示**回读本身不可信**（查询抛异常/回读不到行），
    此时即便 checks 里有 `ok=False` 也不能判 failed —— 那是"没验成"，
    不是"验出来不对"。这是 A9 的核心区分。
    """
    if indeterminate or not checks:
        status = "unknown"
    elif all(c["ok"] for c in checks):
        status = "verified"
    else:
        status = "failed"
    return {"verified": status == "verified", "verify_status": status,
            "op": op, "checks": list(checks)}


def _post_error(prefix: str, post: dict) -> str:
    """按四态生成对外错误文案；unknown 必须显式写"不要重试"。"""
    bad = "; ".join(f"{c['check']}={c['detail']}" for c in post.get("checks", [])
                    if not c["ok"])[:300]
    if post.get("verify_status") == "unknown":
        return f"{prefix}；{UNKNOWN_HINT}" + (f"：{bad}" if bad else "")
    return f"{prefix}：{bad}" if bad else prefix


def post_check(conn, entity_id: str, branch: str, name: str, entity_type: str,
               properties: dict, op: str) -> dict:
    """执行后核验：回读并断言「写的东西真的落成了那样」。

    检查项（每项独立，失败即记录，不做"首错即停"—— 一次拿全，便于定位）：
      1. `version_row` —— `entity_versions` 里有 (id, branch) 且 `is_current=1`
      2. `name`        —— 回读名称一致
      3. `type`        —— 回读类型一致
      4. `props`       —— 回读属性里**本次提交的每个键**值一致（只比提交过的键，
                          不让仓储补写的派生字段造成假红）
    """
    checks = []
    indeterminate = False  # A9：回读不可信 ⇒ unknown，不能当 failed 触发重试

    def _add(key, ok, detail=""):
        checks.append({"check": key, "ok": bool(ok), "detail": detail})

    row = None
    try:
        row = conn.execute(
            "SELECT name, entity_type, properties, is_current FROM entity_versions "
            "WHERE id=? AND branch=? ORDER BY version_no DESC LIMIT 1",
            (entity_id, branch or "dev")).fetchone()
    except Exception as e:
        indeterminate = True
        _add("version_row", False, f"查询失败：{str(e)[:120]}")

    if row is None:
        # 写入已提交却回读不到版本行：可能是仓储没落、也可能是读快照滞后。
        # 客户端无法区分 ⇒ 归 unknown，提示先查询确认，禁止盲目重试。
        indeterminate = True
    else:
        _add("version_row", bool(row["is_current"]),
             "" if row["is_current"] else f"最新版本行 is_current={row['is_current']}")
        _add("name", (row["name"] or "") == (name or ""),
             f"{row['name']!r} != {name!r}" if (row["name"] or "") != (name or "") else "")
        _add("type", (row["entity_type"] or "") == (entity_type or ""),
             f"{row['entity_type']!r} != {entity_type!r}"
             if (row["entity_type"] or "") != (entity_type or "") else "")
        stored = _load_props(row["properties"])
        diffs = []
        for k, v in (properties or {}).items():
            if str(stored.get(k)) != str(v):
                diffs.append(f"{k}: {v!r}→{stored.get(k)!r}")
        _add("props", not diffs, "; ".join(diffs[:6]))

    return _post_result(checks, op, indeterminate=indeterminate)


def post_check_deleted(conn, entity_id: str, branch: str) -> dict:
    """删除的执行后核验——**反向断言**：这些行必须不存在。

    为什么不能复用 `post_check`：它的语义是"回读到、且与提交一致"。
    删除场景下"回读不到"才是成功；若直接复用，`row is None` 会让 checks 为空
    ⇒ `verified` 恒 False（假红），或者更糟——有人为了提高通过率把空 checks 判成 True
    ⇒ **删除失败也报成功**。两个方向都是错的，所以单独写、单独测。
    """
    checks = []
    indeterminate = False

    def _add(key, ok, detail=""):
        checks.append({"check": key, "ok": bool(ok), "detail": detail})

    b = branch or "dev"
    try:
        e = conn.execute("SELECT 1 FROM entities WHERE id=? AND branch=?",
                         (entity_id, b)).fetchone()
        _add("entity_gone", e is None, "" if e is None else f"entities 仍存在 id={entity_id}@{b}")
        r = conn.execute("SELECT count(*) FROM relations WHERE branch=? AND "
                         "(source_id=? OR target_id=?)", (b, entity_id, entity_id)).fetchone()
        _add("edges_gone", (r[0] if r else 0) == 0,
             "" if (r[0] if r else 0) == 0 else f"仍有 {r[0]} 条关联边挂在 {entity_id}@{b}")
    except Exception as ex:
        indeterminate = True
        _add("query", False, f"查询失败：{str(ex)[:120]}")

    return _post_result(checks, "delete", indeterminate=indeterminate)


def post_check_edge(conn, edge_id, branch: str, source_id: str = None,
                    target_id: str = None, relation_type: str = None, op: str = "edge_create") -> dict:
    """边的执行后核验。

    创建：`relations` 里行存在且（给了的话）源/目标/关系类型一致。
    删除：行必须消失。
    """
    checks = []
    indeterminate = False

    def _add(key, ok, detail=""):
        checks.append({"check": key, "ok": bool(ok), "detail": detail})

    # ★ 2026-11-21 真机抓到的假红：提交时传中文别名「包含」，
    #   KnowledgeRepo.create_relation 经 core.relmap.canonical 落库为「CONTAINS」，
    #   核验却拿原始「包含」比对 ⇒ 写入成功却被判 failed。
    #   规范化是 core/relmap.py 的单一事实来源，核验必须复用同一把尺子。
    if relation_type:
        try:
            from core.relmap import canonical as _rel_canon
            _c = _rel_canon(relation_type)
            if _c is not None:
                relation_type = _c
        except Exception:
            pass  # 映射模块不可用 ⇒ 保持原值，由比对给出失败，不把异常当通过

    row = None  # 查询抛异常时也要有定义；A9 把异常归 unknown 而非早退
    try:
        row = conn.execute("SELECT source_id, target_id, relation_type, branch "
                           "FROM relations WHERE id=?", (edge_id,)).fetchone()
    except Exception as ex:
        indeterminate = True
        _add("query", False, f"查询失败：{str(ex)[:120]}")

    if op == "edge_delete":
        _add("edge_gone", row is None, "" if row is None else f"relations 仍存在 id={edge_id}")
    else:
        if row is None:
            indeterminate = True  # 建/改后回读不到 ⇒ 无法判定，不能当确定失败
            _add("edge_row", False, f"relations 中查不到 id={edge_id}")
        else:
            _add("edge_row", True)
            # ⚠️ detail 必须**只在失败时**填：`_add` 无论 ok 真假都会把 detail 写进结果。
            # 首次上线时我在这里漏了条件化，成功时也输出 `'G-S1' != 'G-S1'`，
            # 看起来像"不相等"——虽然 ok/verified 是对的，但会误导排障方向。
            if source_id is not None:
                _ok = (row["source_id"] or "") == source_id
                _add("source", _ok, "" if _ok else f"{row['source_id']!r} != {source_id!r}")
            if target_id is not None:
                _ok = (row["target_id"] or "") == target_id
                _add("target", _ok, "" if _ok else f"{row['target_id']!r} != {target_id!r}")
            if relation_type is not None:
                _ok = (row["relation_type"] or "") == relation_type
                _add("relation_type", _ok,
                     "" if _ok else f"{row['relation_type']!r} != {relation_type!r}")
            if branch:
                _ok = (row["branch"] or "dev") == (branch or "dev")
                _add("branch", _ok, "" if _ok else f"{row['branch']!r} != {branch!r}")

    return _post_result(checks, op, indeterminate=indeterminate)


def write_entity(conn, *, op: str, entity_type: str = "", name: str = "", properties: dict = None,
                 entity_id: str = None, branch: str = "dev", actor: str = "",
                 knowledge_category: str = "", x=None, y=None, status: str = "",
                 log_op: str = "", validate: bool = True, extra_detail: str = "",
                 audit_event: str = "") -> dict:
    """统一写入口：门禁 → 校验 → 写入 → 留痕 → 执行后核验。

    ⚠️ 调用方**不得**在此之外再调 `repo.create_entity` / `repo.update_entity` /
    `GraphStore.create_node`，也不要再自己调 `validate_node` —— 重复就等于两把尺子。

    `status`：创建时的状态。**必须由调用方显式传**，因为两条路径语义不同：
      · `POST /entities`（API 直写）→ `candidate`（进审核队列，仓储默认值）
      · `POST /graph/nodes`（工程师在图库里手动建）→ `reviewed`（创建即确认）
    默认空字符串 = 用仓储默认值，保持各路径原有行为不变。
    """
    if op not in WRITE_OPS:
        return {"ok": False, "stage": "type_gate", "error": f"不支持的写操作 {op!r}"}
    branch = branch or "dev"
    properties = properties or {}
    if not entity_id:
        return {"ok": False, "stage": "not_found", "error": f"{op} 操作缺少实体 id"}

    # ① 本体类型门禁（删除不需要：类型已存在，删的时候再校验类型没意义）
    if op != "delete":
        terr = check_entity_type(conn, entity_type)
        if terr:
            return {"ok": False, "stage": "type_gate", "error": terr}

    # ② 属性约束校验（创建与更新**同一把尺子** —— 改前 create 路径没有这一关）
    if validate and op in ("create", "update"):
        errs = validate_props(conn, entity_type, properties, branch,
                              entity_id=entity_id if op == "update" else None)
        if errs:
            return {"ok": False, "stage": "validate", "error": "; ".join(errs[:6])}

    # ③ 写入（历史/版本行由仓储负责，此处不重复实现）
    from repositories.knowledge_repo import KnowledgeRepo
    repo = KnowledgeRepo(conn)
    forked = False
    if op == "create":
        _kw = {"knowledge_category": knowledge_category or "",
               "created_by": actor or "system"}
        if status:
            _kw["status"] = status
        repo.create_entity(
            entity_id, name, entity_type,
            json.dumps(properties, ensure_ascii=False), branch, **_kw)
        if x is not None or y is not None:
            conn.execute("UPDATE entities SET graph_x=?, graph_y=? WHERE id=? AND branch=?",
                         (x, y, entity_id, branch))
    elif op == "update":
        res = repo.update_entity(
            entity_id, name, entity_type,
            json.dumps(properties, ensure_ascii=False), branch,
            knowledge_category=knowledge_category or "", changed_by=actor or "")
        if not res.get("ok"):
            return {"ok": False, "stage": "not_found", "error": res.get("error") or "实体不存在"}
        forked = bool(res.get("forked"))
    else:  # delete
        # 与既有 `graph_delete_node` 同序：先删同分支关联边，再删本分支版本行。
        # **只删本分支**（其它分支版本保留）—— 这是版本化语义，不是疏漏。
        conn.execute("DELETE FROM relations WHERE branch=? AND (source_id=? OR target_id=?)",
                     (branch, entity_id, entity_id))
        cur = conn.execute("DELETE FROM entities WHERE id=? AND branch=?", (entity_id, branch))
        if cur.rowcount == 0:
            return {"ok": False, "stage": "not_found",
                    "error": f"节点不存在: {entity_id}（分支 {branch}）"}

    # ④ 留痕：图谱编辑日志（含 trace_id，便于与对话轮次关联）+ 审计（哈希链）
    try:
        from core.audit import with_trace as _wt
        _cols, _vals = _wt(conn, "graph_edit_logs",
                           ["op", "node_id", "payload", "operator"],
                           [log_op or f"entity_{op}", entity_id or "",
                            json.dumps({"name": name, "type": entity_type, "x": x, "y": y},
                                       ensure_ascii=False), actor or "system"])
        conn.execute("INSERT INTO graph_edit_logs (%s) VALUES (%s)"
                     % (", ".join(_cols), ",".join("?" * len(_cols))), tuple(_vals))
    except Exception:
        # 留痕失败不阻断写入（与既有 `_log_graph_edit` 口径一致），但不得静默吞掉"写入成功"的结论
        pass
    try:
        from core.audit import audit as _audit
        _verb = {"create": "创建", "update": "更新", "delete": "删除"}.get(op, op)
        _audit(actor or "system", audit_event or f"entity_{op}",
               f"{_verb}实体: {name or entity_id} ({entity_id})"
               f"［统一写入口］" + (("；" + extra_detail) if extra_detail else ""),
               conn=conn, branch=branch)
    except Exception:
        pass

    # ⑤ 执行后核验（P0-3 挂点）—— 删除走**反向断言**，语义完全不同
    post = (post_check_deleted(conn, entity_id, branch) if op == "delete"
            else post_check(conn, entity_id, branch, name, entity_type, properties, op))
    if not post["verified"]:
        return {"ok": False, "stage": "post_condition", "applied": True,
                "id": entity_id, "post": post,
                "verify_status": post["verify_status"], "retry_safe": False,
                "error": _post_error("写入已提交，但效果核验未通过（接口不会再报成功）", post)}
    return {"ok": True, "id": entity_id, "forked": forked, "post": post}


def write_edge(conn, *, op: str, edge_id=None, source_id: str = "", target_id: str = "",
               relation_type: str = "", props: dict = None, branch: str = "dev",
               actor: str = "", log_op: str = "", validate: bool = True,
               extra_detail: str = "", audit_event: str = "") -> dict:
    """边的统一写入口：校验 → 写入 → 留痕 → 执行后核验。

    关系类型门禁与实体**同一把尺子**：源/目标类型 + 关系类型约束由
    `OntologyValidator.validate_edge` 判（唯一实现，端点不得另写）。
    """
    if op not in ("edge_create", "edge_update", "edge_delete"):
        return {"ok": False, "stage": "type_gate", "error": f"不支持的边操作 {op!r}"}
    branch = branch or "dev"
    props = props or {}

    from repositories.knowledge_repo import KnowledgeRepo
    if op == "edge_create":
        if not source_id or not target_id:
            return {"ok": False, "stage": "not_found", "error": "边创建缺少 source_id / target_id"}
        # 引用完整性：源/目标必须与关系**同分支**存在（跨分支回退会产生图谱永不渲染的死行，
        # 2026-09-04 清理过 33 条）—— 与 `GraphStore.create_edge` 同口径，此处不重复实现该校验，
        # 直接委托它，避免第三条判定路径。
        from ontology_semantics import GraphStore
        ok, res = GraphStore(conn).create_edge(
            source_id, target_id, relation_type, props, branch)
        if not ok:
            # GraphStore 返回的错误里已区分「类型不合法」与「端点不存在」，这里不再二次分类，
            # 一律归 validate（语义 = 未通过校验，写入未发生）
            return {"ok": False, "stage": "validate", "error": "; ".join(res or [])[:400]}
        edge_id = res
    elif op == "edge_update":
        # P0-0 第五轮（2026-11-22）：前端图谱编辑器直调的 PUT edges/{eid} 原来自写
        # UPDATE + 自己的 canonical/validate_edge，是 A1「前端直连绕过核验」的活样本。
        # 这里把判定与写入收进统一入口：关系名归一、源/目标类型门禁、写入、留痕、
        # 审计、post_check 与建边/删边同一套；端点只保留 release_guard 与 404/500 语义。
        if edge_id is None:
            return {"ok": False, "stage": "not_found", "error": "边更新缺少 edge_id"}
        row = conn.execute(
            "SELECT r.branch, r.source_id, r.target_id, "
            "s.entity_type stype, t.entity_type ttype FROM relations r "
            "LEFT JOIN entities s ON s.id=r.source_id AND s.branch=r.branch "
            "LEFT JOIN entities t ON t.id=r.target_id AND t.branch=r.branch "
            "WHERE r.id=?", (edge_id,)).fetchone()
        if not row:
            return {"ok": False, "stage": "not_found", "error": "edge not found"}
        branch = row["branch"] or branch
        source_id = row["source_id"] or source_id
        target_id = row["target_id"] or target_id

        from core.relmap import canonical as _rel_canon
        _c = _rel_canon(relation_type)
        if _c is None:
            return {"ok": False, "stage": "type_gate",
                    "error": f"关系类型已废弃且无 canonical 映射: {relation_type}"}
        relation_type = _c

        from ontology_semantics import GraphStore
        errs = GraphStore(conn).validator.validate_edge(
            row["stype"] or "", relation_type, row["ttype"] or "")
        if errs:
            return {"ok": False, "stage": "validate", "error": "; ".join(errs)[:400]}

        conn.execute(
            "UPDATE relations SET relation_type=?, properties=?, status='candidate', "
            "reviewed_by='', reviewed_at='' WHERE id=?",
            (relation_type, json.dumps(props or {}, ensure_ascii=False), edge_id))
    else:  # edge_delete
        if edge_id is None:
            return {"ok": False, "stage": "not_found", "error": "边删除缺少 edge_id"}
        row = conn.execute("SELECT branch FROM relations WHERE id=?", (edge_id,)).fetchone()
        if not row:
            return {"ok": False, "stage": "not_found", "error": "edge not found"}
        branch = row["branch"] or branch
        conn.execute("DELETE FROM relations WHERE id=?", (edge_id,))

    # 留痕 + 审计
    _node = (f"{source_id}--{relation_type}--{target_id}" if op == "edge_create" else str(edge_id))
    try:
        _log_graph_edit_safe(conn, log_op or ("edge_add" if op == "edge_create" else "edge_delete"),
                             _node, props if op == "edge_create" else {}, actor)
    except Exception:
        pass
    try:
        from core.audit import audit as _audit
        # ⚠️ 三态文案不能写成 create/其它 二元：2026-11-22 真机抓到 edge_update
        # 被记成「图谱删除边: 442」（event_type 对、detail 错 ⇒ 审计回放会误导）。
        _detail = ("图谱新增边: %s" % _node if op == "edge_create"
                   else "图谱修正边: %s" % _node if op == "edge_update"
                   else "图谱删除边: %s" % edge_id)
        _audit(actor or "system", audit_event or ("graph_edge_add" if op == "edge_create"
                                                  else "graph_edge_update" if op == "edge_update"
                                                  else "graph_edge_delete"),
               _detail + "［统一写入口］" + (("；" + extra_detail) if extra_detail else ""),
               conn=conn, branch=branch)
    except Exception:
        pass

    post = post_check_edge(conn, edge_id, branch, source_id or None, target_id or None,
                           relation_type or None, op=op)
    if not post["verified"]:
        return {"ok": False, "stage": "post_condition", "applied": True,
                "id": edge_id, "post": post,
                "verify_status": post["verify_status"], "retry_safe": False,
                "error": _post_error("写入已提交，但效果核验未通过（接口不会再报成功）", post)}
    return {"ok": True, "id": edge_id, "post": post}


def write_inferred_entity(conn, *, entity_id: str, name: str, entity_type: str,
                          branch: str = "dev", actor: str = "", project_id: str = "") -> dict:
    """推理并入专用写入口（P0-0 第四轮）：**保留宽松类型门禁**，其余与严格入口同治理。

    为什么不能直接调 `write_entity(strict=True)`：
      · 推断出的类型可能不在本体（实测兜底类型 `实体` 不在 54 类型里），
        强套类型门禁会让推理并入大面积失败；
      · 但原实现直接 `INSERT INTO entities` —— **不写 entity_versions、不留痕、
        没有 post_check**，这才是真缺口。

    因此这里只把「类型门禁」从**拒绝**降为**告警并写进审计**，
    写入 / 版本行 / 留痕 / 执行后核验全部复用同一套基础设施：
      · 写入委托 `KnowledgeRepo.create_entity`（它自己会 `_ensure_initial_version`）
      · 留痕 `graph_edit_logs` + 审计（哈希链），detail 显式标注「推断宽松档」
      · 核验 `post_check`（回读版本行 name/type/props 一致）
    返回约定与 `write_entity` 相同，另带 `warnings`。
    """
    branch = branch or "dev"
    warnings = []
    try:
        terr = check_entity_type(conn, entity_type)
    except Exception as e:
        # 干净测试库可能没有 ontology_types 表；宽松档不能因「查不了本体」而阻断并入
        # （严格档在 write_entity 里仍会照常拒绝，不受这里影响）。
        terr = f"本体类型表不可用（宽松档不阻断）: {str(e)[:120]}"
    if terr:
        warnings.append("推断类型门禁告警（未阻断）: " + terr)

    from repositories.knowledge_repo import KnowledgeRepo
    repo = KnowledgeRepo(conn)
    kw = {"status": "reviewed", "source_type": "reasoning",
          "created_by": actor or "system"}
    if project_id:
        kw["project_id"] = project_id
    try:
        repo.create_entity(entity_id, name or entity_id, entity_type or "实体",
                           "{}", branch, **kw)
    except Exception as e:
        return {"ok": False, "stage": "write", "error": f"推断实体写入失败: {str(e)[:200]}"}

    _write_inferred_trace(conn, "entity", entity_id, actor,
                          {"name": name, "type": entity_type, "branch": branch},
                          warnings, branch)
    post = post_check(conn, entity_id, branch, name, entity_type or "实体", {}, "create")
    if not post["verified"]:
        return {"ok": False, "stage": "post_condition", "applied": True,
                "id": entity_id, "post": post, "warnings": warnings,
                "verify_status": post["verify_status"], "retry_safe": False,
                "error": _post_error("推断实体已写入但效果核验未通过", post)}
    return {"ok": True, "id": entity_id, "post": post, "warnings": warnings}


def write_inferred_edge(conn, *, source_id: str, target_id: str, relation_type: str,
                        branch: str = "dev", actor: str = "", project_id: str = "") -> dict:
    """推理并入专用关系写入口：跳过 `GraphStore.create_edge` 的类型校验，
    但保留引用完整性（源/目标必须同分支存在）、版本/留痕/审计/post_check 同治理。
    """
    branch = branch or "dev"
    warnings = []
    for label, eid in (("源", source_id), ("目标", target_id)):
        if not conn.execute("SELECT 1 FROM entities WHERE id=? AND branch=?",
                            (eid, branch)).fetchone():
            return {"ok": False, "stage": "not_found",
                    "error": f"{label}节点在分支 {branch} 不存在: {eid}"}

    from repositories.knowledge_repo import KnowledgeRepo
    try:
        edge_id = KnowledgeRepo(conn).create_relation(
            source_id, target_id, relation_type, "{}", branch,
            created_by=actor or "system", project_id=project_id)
    except Exception as e:
        return {"ok": False, "stage": "write", "error": f"推断关系写入失败: {str(e)[:200]}"}

    _write_inferred_trace(conn, "edge", edge_id, actor,
                          {"source": source_id, "target": target_id, "type": relation_type},
                          warnings, branch)
    post = post_check_edge(conn, edge_id, branch, source_id, target_id,
                           relation_type, op="edge_create")
    if not post["verified"]:
        return {"ok": False, "stage": "post_condition", "applied": True,
                "id": edge_id, "post": post, "warnings": warnings,
                "verify_status": post["verify_status"], "retry_safe": False,
                "error": _post_error("推断关系已写入但效果核验未通过", post)}
    return {"ok": True, "id": edge_id, "post": post, "warnings": warnings}


def _write_inferred_trace(conn, kind, obj_id, actor, payload, warnings, branch):
    """推断并入的统一留痕：graph_edit_logs + 审计哈希链，宽松档告警写进 detail。"""
    tag = "推断并入（宽松类型门禁）"
    if warnings:
        tag += "；" + " / ".join(warnings)[:300]
    try:
        _log_graph_edit_safe(conn, f"inferred_{kind}_add", str(obj_id), payload, actor)
    except Exception:
        pass
    try:
        from core.audit import audit as _audit
        _audit(actor or "system", f"reasoning_merge_{kind}",
               f"{tag}：{obj_id}［统一写入口·推断通道］",
               conn=conn, branch=branch)
    except Exception:
        pass


def _log_graph_edit_safe(conn, op, node_id, payload, operator):
    """`graph_edit_logs` 留痕（与 `routers/knowledge_parts/shared.py::_log_graph_edit` 同实现）。"""
    from core.audit import with_trace as _wt
    _cols, _vals = _wt(conn, "graph_edit_logs", ["op", "node_id", "payload", "operator"],
                       [op, node_id or "", json.dumps(payload or {}, ensure_ascii=False),
                        operator or "system"])
    conn.execute("INSERT INTO graph_edit_logs (%s) VALUES (%s)"
                 % (", ".join(_cols), ",".join("?" * len(_cols))), tuple(_vals))
