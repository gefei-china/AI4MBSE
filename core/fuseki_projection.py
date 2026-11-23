# -*- coding: utf-8 -*-
"""P0-0 第九批（2026-11-23）**写后增量投影**：消除 Fuseki 镜像的陈旧窗口。

## 为什么不是"写完就全量重灌"

第八批只解决了**启动时刻**对齐：从一次写入到下次重启之间，SPARQL CQ 查的
仍是旧镜像。但 `sync_to_fuseki_with_time()` 是 `CLEAR DEFAULT` + 全量 INSERT，
每次实体改名都全量重灌 1700+ 三元组，代价与写入频率线性相关，不可接受。

## 做法：精确三元组差量（不是按 subject 一把清）

每个实体 / 关系对应一组 N-Triples 行，生成器**复用全量同步的同一套纯函数**
（`services.sparql_sync.entity_lines` / `relation_line` / `ontology_type_lines`），
不在本模块另写一份 IRI/转义规则。写入前抓 `before` 快照，写入提交后抓 `after`：

    DELETE DATA { before - after }
    INSERT DATA { after - before }

为什么用精确差集而不是 `DELETE WHERE { <iri> ?p ?o }`：
关系三元组的 subject 是**源实体**（`<src> <rel> <dst> .`），按 subject 清除
会连带删掉同一源实体上的其它边；精确差集只动真正变化的行，也**不会碰**
用户经 `POST /api/sparql/update` 写入的同图其它三元组。

## 事务边界（关键）

SQLite 请求级连接在 `core.deps.db_session` 结束时才 `commit()`。若在写入口
内部直接推 Fuseki，推的是**未提交**数据；一旦请求后续抛异常 rollback，
Fuseki 里就留下幽灵三元组。因此本模块分两段：

  · `capture_before()` / `mark()` —— 写入口内，只登记写前快照到连接属性；
  · `flush()` —— 由 `db_session` 在 **commit 成功之后**调用，现场读 after 并差量更新。

异常 rollback 时 pending 随连接关闭丢弃，**绝不投影**。
非 HTTP 调用方（门禁/脚本）可显式调 `flush(conn)`。

## 四态（与 A9 同口径）

`ok` / `disabled` / `no_pending` / `failed`。投影失败**不阻断已提交的写入**，
只在返回值里给出原因——"图库没跟上"与"业务写失败"是两件事，不能混为一谈。

## 边界

只投影 base 三元组；W3C Time 时态三元组（含 bnode）仍需全量同步或既有
时态链路维护。本模块不提供失败重放队列（Fuseki 短暂不可达时靠下次启动
的 autosync 全量对齐兜底）。
"""
from __future__ import annotations

#: ⚠️ sqlite3.Connection 既不能 setattr（无 __dict__）也不能 weakref（实测），
#: 只能以连接对象为 key 存模块级字典。每个请求连接不同，写入不冲突；
#: `flush()` / `discard()` 必须无条件 pop，否则长跑进程会积累连接引用。
_PENDING = {}


def projection_snapshot(conn, kind: str, obj_id) -> set:
    """读某对象当前的 base 三元组集合（与全量 builder 同一公式）。

    `entities` 同一 id 可跨分支存在多行；全量 builder 遍历所有
    `is_current=1 AND status<>'deprecated'` 行，这里必须同口径，否则
    删除本分支后会误删其它分支仍需要的三元组。
    """
    from services.sparql_sync import (
        _exec_dict, entity_lines, relation_line, ontology_type_lines,
    )
    if not obj_id:
        return set()
    if kind == "entity":
        rows = _exec_dict(
            conn,
            "SELECT id, name, entity_type, properties, branch, "
            "valid_from, valid_to, is_current FROM entities "
            "WHERE id=? AND status<>'deprecated' AND is_current=1",
            (obj_id,))
        lines = []
        for d in rows:
            lines.extend(entity_lines(d))
    elif kind == "edge":
        rows = _exec_dict(
            conn,
            "SELECT source_id, target_id, relation_type, properties FROM relations "
            "WHERE id=? AND status<>'deprecated'",
            (obj_id,))
        lines = [relation_line(d) for d in rows]
    elif kind == "ontology_type":
        rows = _exec_dict(conn, "SELECT * FROM ontology_types WHERE id=?", (obj_id,))
        lines = []
        for d in rows:
            lines.extend(ontology_type_lines(conn, d))
    else:
        lines = []
    return {ln.strip() for ln in lines if ln.strip().endswith(" .")}


def capture_before(conn, kind: str, obj_id) -> set:
    """写入前抓快照；失败返回空集合（不因图库投影拖垮业务写）。"""
    try:
        return projection_snapshot(conn, kind, obj_id)
    except Exception:
        return set()


def mark(conn, kind: str, obj_id, before: set) -> None:
    """登记待投影差量。同一对象多次写：保留**首次** before，after 由 flush 现场取。"""
    pending = _PENDING.setdefault(conn, {})
    key = (kind, str(obj_id))
    if key in pending:
        pending[key]["before"] |= set(before or set())
    else:
        pending[key] = {"before": set(before or set())}


def discard(conn) -> None:
    """丢弃 pending（异常回滚路径 / 测试清理 / 连接关闭）。"""
    _PENDING.pop(conn, None)


def enabled() -> bool:
    from core.config import get as _cfg_get
    return bool(_cfg_get("graph_db", "fuseki_writesync"))


def delta(before: set, after: set) -> tuple:
    """返回 (to_delete, to_insert)，均为排序后的列表（稳定可断言）。"""
    return sorted(set(before or set()) - set(after or set())), \
           sorted(set(after or set()) - set(before or set()))


def _update_statement(op: str, lines: list) -> str:
    from services.sparql_sync import SPARQL_PREFIXES
    return SPARQL_PREFIXES + " %s DATA { %s }" % (op, "\n".join(lines))


def apply_delta(cli, before: set, after: set, timeout: float = 30.0) -> dict:
    """把差量推到 Fuseki。差集为空则零网络调用（幂等）。"""
    to_del, to_ins = delta(before, after)
    if not to_del and not to_ins:
        return {"ok": True, "deleted": 0, "inserted": 0, "calls": 0}
    from services.sparql_sync import FusekiUnavailable, _chunk_lines
    calls = 0
    try:
        for op, lines in (("DELETE", to_del), ("INSERT", to_ins)):
            for chunk in _chunk_lines("\n".join(lines)):
                if chunk.strip():
                    cli.update(_update_statement(op, chunk.splitlines()), timeout=timeout)
                    calls += 1
    except FusekiUnavailable as ex:
        return {"ok": False, "error": "Fuseki 不可达: %s" % str(ex)[:200],
                "deleted": len(to_del), "inserted": len(to_ins), "calls": calls}
    except Exception as ex:
        return {"ok": False, "error": "%s: %s" % (type(ex).__name__, str(ex)[:200]),
                "deleted": len(to_del), "inserted": len(to_ins), "calls": calls}
    return {"ok": True, "deleted": len(to_del), "inserted": len(to_ins), "calls": calls}


def flush(conn, cli=None) -> dict:
    """commit 之后统一投影。返回四态字典，任何异常都不抛给业务路径。"""
    pending = _PENDING.pop(conn, None)  # 先摘走，避免重入/重复投影与引用泄漏
    if not pending:
        return {"status": "no_pending"}
    if not enabled():
        return {"status": "disabled", "objects": len(pending)}
    if cli is None:
        try:
            from core.fuseki_freshness import _default_client
            cli = _default_client()
        except Exception as ex:
            return {"status": "failed", "reason": "客户端构造失败: %s" % str(ex)[:160],
                    "objects": len(pending)}
    results, failed = [], 0
    for (kind, obj_id), rec in sorted(pending.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        try:
            after = projection_snapshot(conn, kind, obj_id)
            r = apply_delta(cli, rec["before"], after)
        except Exception as ex:
            r = {"ok": False, "error": "%s: %s" % (type(ex).__name__, str(ex)[:160])}
        if not r.get("ok"):
            failed += 1
        results.append({"kind": kind, "id": obj_id,
                        "deleted": r.get("deleted", 0), "inserted": r.get("inserted", 0),
                        "ok": bool(r.get("ok")), "error": r.get("error", "")})
    return {"status": "failed" if failed else "ok", "objects": len(pending),
            "failed": failed, "results": results}
