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

## 第十批：失败重放队列

Fuseki 短暂不可达时，差量投影失败但业务写已提交。若只告警，就要等下次
重启 autosync 全量重灌才能补回来 —— 这段窗口里 CQ 严格档只能 skip。
`graph_db.fuseki_replay_queue` 打开后，失败项**持久化**到
`fuseki_projection_replay` 表（UNIQUE(kind,obj_id) 合并 before），在下次
flush / 服务启动时按「尝试次数少优先、id 小优先」重放，成功后删行。

⚠️ 队列存的是 **before 快照，不是 (before,after) 差量**：重放时按**当前 DB**
重算 after。原因：若 t1 失败（L1→L2）、t2 成功（L2→L3），回放 t1 的旧差量
会把 L2 重新插回 Fuseki，与 L3 并存 ⇒ 同一实体两个 label。存 before 后
回放变成 `delta(before, 当前 after)`，t2 已成功时是纯 no-op，天然幂等且
与重放顺序无关。

## 第十一批：时态三元组也进差量

快照含 8 条 W3C Time 行（与 `build_valid_intervals_ttl` 同一纯函数）。
Fuseki 会重命名 bnode label，故删除不能按原 label 精确匹配；改为按**命名
区间 IRI**模式删除整个区间 + 一跳邻居，再把 after 中该实体的时态块整块重插。
只插变化的那一行会留下没有 `<inv> hasBeginning` 指向的孤儿 bnode。
"""
from __future__ import annotations

import json
import re
import sqlite3

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
    from services.temporal_sparql import entity_interval_lines
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
            # 第十一批：时态三元组进快照，写后差量一并维护（不再等全量重灌）。
            if d.get("valid_from") or d.get("valid_to"):
                lines.extend(entity_interval_lines(d))
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


_INTERVAL_RE = re.compile(r"<http://mbse/ontology/[^>\s]+/interval>")


def _split_temporal_deletes(to_del: list, to_ins: list, before=None, after=None) -> tuple:
    """时态三元组按**整块**换，不做逐行差量。

    Fuseki 会重命名 bnode label ⇒ `DELETE DATA { _:safe_x_ba ... }` 匹配不到。
    更关键的是：只插变化的那一行会产生**孤儿 bnode**（没有 `<inv> hasBeginning`
    指向它，下次再删就找不到）。因此任一有效期字段变化 ⇒
      · 按命名区间 IRI 模式删除整个区间 + 一跳邻居；
      · 把 after 中该实体的**全部**时态行重新插入。
    返回 (精确删除行, 插入行, 模式删除语句)。
    """
    from urllib.parse import unquote
    from services.sparql_sync import SPARQL_PREFIXES
    from services.temporal_sparql import HAS_VALID, bnode_labels

    def _eid_of(ln):
        ent = ln.split(" ", 1)[0]
        if not ent.startswith("<http://mbse/ontology/"):
            return ""
        return unquote(ent[len("<http://mbse/ontology/"):-1])

    def _inv_of(eid):
        return "<http://mbse/ontology/" + eid + "/interval>"

    label2inv = {}
    for ln in (before or set()):
        if HAS_VALID in ln:
            eid = _eid_of(ln)
            ba, bb = bnode_labels(eid)
            label2inv[ba] = label2inv[bb] = _inv_of(eid)

    def _affected(ln):
        if HAS_VALID in ln:
            m = _INTERVAL_RE.search(ln)
            return m.group(0) if m else None
        if "time#" in ln:
            m = _INTERVAL_RE.search(ln)
            if m:
                return m.group(0)
            return next((v for k, v in label2inv.items() if k in ln), None)
        if "ontology/validFrom>" in ln or "ontology/validTo>" in ln:
            eid = _eid_of(ln)
            return _inv_of(eid) if eid else None
        return None

    changed = set(to_del) | set(to_ins)
    invs = {v for ln in changed if (v := _affected(ln))}
    if not invs:
        return list(to_del), list(to_ins), []
    affected_eids = {_eid_of(ln) for ln in changed
                     if "ontology/validFrom>" in ln or "ontology/validTo>" in ln
                     or HAS_VALID in ln}
    affected_eids |= {e for e in {_eid_of(ln) for ln in (before or set()) if HAS_VALID in ln}
                      if _inv_of(e) in invs}
    full_temporal = {ln for ln in (after or set()) if "time#" in ln or HAS_VALID in ln}
    # bnode label 是 id 的清洗形式（中文→下划线），不能只用原始 eid 做包含匹配，
    # 否则「整块重插」会漏掉 4 条 bnode 主语行 ⇒ 留下孤儿 bnode / 旧日期。
    anchors = set()
    for e in affected_eids:
        if not e:
            continue
        anchors.add(e)
        ba, bb = bnode_labels(e)
        anchors.add(ba)
        anchors.add(bb)
    add_ins = {ln for ln in full_temporal if any(a in ln for a in anchors)}
    # 链接行主语/宾语都是命名 IRI，必须精确删（模式删除按 inv 主语，碰不到它）
    exact_del = [ln for ln in to_del if "time#" not in ln]
    inserts = {ln for ln in to_ins if "time#" not in ln and HAS_VALID not in ln} | add_ins
    # ⚠️ DELETE 模板里必须用**已绑定的变量**：写 `DELETE { ?s ?p ?o }` 而 WHERE 只绑定
    # 了 ?p/?o 时 ?s 未绑定，Fuseki 静默跳过整条删除（实测 204 但三元组不动）。
    patterns = [
        SPARQL_PREFIXES + " DELETE { %s ?p ?o . %s ?p1 ?s . ?s ?p2 ?o2 } "
        "WHERE { { %s ?p ?o } UNION { %s ?p1 ?s . ?s ?p2 ?o2 } }"
        % (inv, inv, inv, inv)
        for inv in sorted(invs)
    ]
    return exact_del, sorted(inserts), patterns


def apply_delta(cli, before: set, after: set, timeout: float = 30.0) -> dict:
    """把差量推到 Fuseki。差集为空则零网络调用（幂等）。"""
    to_del, to_ins = delta(before, after)
    if not to_del and not to_ins:
        return {"ok": True, "deleted": 0, "inserted": 0, "calls": 0, "patterns": 0}
    from services.sparql_sync import FusekiUnavailable, _chunk_lines
    exact_del, to_ins, patterns = _split_temporal_deletes(to_del, to_ins, before, after)
    calls = 0
    try:
        # 模式删除必须先跑：它要靠 <inv> 的一跳邻居定位 bnode，
        # 若先精确删掉 <inv> 自身，邻居就找不到了。
        for q in patterns:
            cli.update(q, timeout=timeout)
            calls += 1
        for op, lines in (("DELETE", exact_del), ("INSERT", to_ins)):
            for chunk in _chunk_lines("\n".join(lines)):
                if chunk.strip():
                    cli.update(_update_statement(op, chunk.splitlines()), timeout=timeout)
                    calls += 1
    except FusekiUnavailable as ex:
        return {"ok": False, "error": "Fuseki 不可达: %s" % str(ex)[:200],
                "deleted": len(to_del), "inserted": len(to_ins), "calls": calls,
                "patterns": len(patterns)}
    except Exception as ex:
        return {"ok": False, "error": "%s: %s" % (type(ex).__name__, str(ex)[:200]),
                "deleted": len(to_del), "inserted": len(to_ins), "calls": calls,
                "patterns": len(patterns)}
    return {"ok": True, "deleted": len(to_del), "inserted": len(to_ins), "calls": calls,
            "patterns": len(patterns)}


# ══════════════════════════════════════════════════════════════════════════
# 第十批（2026-11-23）失败重放队列
#
# 存 before、重放现场重算 after（见模块 docstring）。表由 database/schema.py
# 建、database/migrations/ontology.py 幂等补老库；这里只做读写，DDL 不抄第二份。
# ══════════════════════════════════════════════════════════════════════════

def replay_enabled() -> bool:
    """失败重放队列开关（默认关；与 fuseki_writesync 独立登记）。"""
    from core.config import get as _cfg_get
    return bool(_cfg_get("graph_db", "fuseki_replay_queue"))


def _json_lines(raw) -> set:
    """before_json → N-Triples 行集合；坏数据按空集合（重放只会删不掉，不会误插）。"""
    try:
        v = json.loads(raw or "[]")
        if not isinstance(v, list):
            return set()
        return {str(x) for x in v}
    except Exception:
        return set()


def pending_rows(conn, limit: int = 100) -> list:
    """按「少试优先、id 小优先」取待重放项；表不存在（老库未迁移）返回空。"""
    try:
        rows = conn.execute(
            "SELECT id, kind, obj_id, before_json, attempts, last_error, created_at, updated_at "
            "FROM fuseki_projection_replay ORDER BY attempts ASC, id ASC LIMIT ?",
            (limit,)).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []


def pending_count(conn) -> int:
    try:
        return int(conn.execute(
            "SELECT COUNT(*) FROM fuseki_projection_replay").fetchone()[0])
    except sqlite3.OperationalError:
        return 0


def queue_status(conn) -> dict:
    """运维可见性：开关 + 队列条数 + 明细（四态纪律： degraded 必须可见）。"""
    rows = pending_rows(conn, limit=1000)
    return {"enabled": replay_enabled(), "writesync_enabled": enabled(),
            "pending": pending_count(conn),
            "entries": [{"id": r["id"], "kind": r["kind"], "obj_id": r["obj_id"],
                         "attempts": r["attempts"], "last_error": r["last_error"],
                         "created_at": r["created_at"], "updated_at": r["updated_at"],
                         "before_lines": len(_json_lines(r["before_json"]))}
                        for r in rows]}


def enqueue(conn, kind: str, obj_id, before, error: str = "") -> dict:
    """登记失败项。UNIQUE(kind,obj_id) 合并时取 before 并集（保留最早快照）。

    独立于业务写的事务：只在 flush（已 commit）之后调用，自行 commit。
    队列写失败只回 ok=False，绝不改变"业务写已成功"的结论。
    """
    lines = sorted(set(before or set()))
    try:
        row = conn.execute(
            "SELECT id, before_json FROM fuseki_projection_replay WHERE kind=? AND obj_id=?",
            (str(kind), str(obj_id))).fetchone()
        payload = json.dumps(lines, ensure_ascii=False)
        if row:
            merged = sorted(_json_lines(row["before_json"]) | set(lines))
            conn.execute(
                "UPDATE fuseki_projection_replay SET before_json=?, last_error=?, "
                "updated_at=datetime('now','localtime') WHERE id=?",
                (json.dumps(merged, ensure_ascii=False), str(error)[:300], row["id"]))
            action = "merged"
        else:
            conn.execute(
                "INSERT INTO fuseki_projection_replay "
                "(kind,obj_id,before_json,attempts,last_error) VALUES (?,?,?,0,?)",
                (str(kind), str(obj_id), payload, str(error)[:300]))
            action = "inserted"
        conn.commit()
        return {"ok": True, "action": action}
    except Exception as ex:
        try:
            conn.rollback()
        except Exception:
            pass
        return {"ok": False, "error": "%s: %s" % (type(ex).__name__, str(ex)[:160])}


def replay(conn, cli=None, limit: int = 100) -> dict:
    """重放历史失败项。每项现场重算 after ⇒ 幂等、顺序无关、不会复活旧 label。"""
    if not replay_enabled():
        return {"status": "disabled"}
    rows = pending_rows(conn, limit)
    if not rows:
        return {"status": "no_pending", "pending": 0}
    if cli is None:
        try:
            from core.fuseki_freshness import _default_client
            cli = _default_client()
        except Exception as ex:
            return {"status": "failed", "reason": "客户端构造失败: %s" % str(ex)[:160],
                    "pending": len(rows)}
    done, failed, details = 0, 0, []
    for row in rows:
        try:
            before = _json_lines(row["before_json"])
            after = projection_snapshot(conn, row["kind"], row["obj_id"])
            r = apply_delta(cli, before, after)
        except Exception as ex:
            r = {"ok": False, "error": "%s: %s" % (type(ex).__name__, str(ex)[:160])}
        try:
            if r.get("ok"):
                conn.execute("DELETE FROM fuseki_projection_replay WHERE id=?", (row["id"],))
                done += 1
            else:
                conn.execute(
                    "UPDATE fuseki_projection_replay SET attempts=attempts+1, last_error=?, "
                    "updated_at=datetime('now','localtime') WHERE id=?",
                    (str(r.get("error") or "")[:300], row["id"]))
                failed += 1
            conn.commit()
        except Exception:
            pass
        details.append({"row_id": row["id"], "kind": row["kind"], "obj_id": row["obj_id"],
                        "ok": bool(r.get("ok")), "error": r.get("error", ""),
                        "deleted": r.get("deleted", 0), "inserted": r.get("inserted", 0)})
    return {"status": "failed" if failed else "ok", "replayed": done, "failed": failed,
            "pending": pending_count(conn), "results": details}


def flush(conn, cli=None) -> dict:
    """commit 之后统一投影。返回四态字典，任何异常都不抛给业务路径。"""
    pending = _PENDING.pop(conn, None)  # 先摘走，避免重入/重复投影与引用泄漏

    # 第十批：先补历史欠账，再投本次差量。队列开关默认关 ⇒ 零额外网络。
    replay_result = {"status": "skipped"}
    if replay_enabled():
        try:
            replay_result = replay(conn, cli=cli)
        except Exception as ex:
            replay_result = {"status": "failed",
                             "reason": "重放异常: %s: %s" % (type(ex).__name__, str(ex)[:160])}
    if not pending:
        out = {"status": "no_pending"}
        if replay_result.get("status") != "skipped":
            out["replay"] = replay_result
        return out
    if not enabled():
        return {"status": "disabled", "objects": len(pending), "replay": replay_result}
    if cli is None:
        try:
            from core.fuseki_freshness import _default_client
            cli = _default_client()
        except Exception as ex:
            return {"status": "failed", "reason": "客户端构造失败: %s" % str(ex)[:160],
                    "objects": len(pending), "replay": replay_result}
    results, failed, enqueued = [], 0, 0
    for (kind, obj_id), rec in sorted(pending.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        try:
            after = projection_snapshot(conn, kind, obj_id)
            r = apply_delta(cli, rec["before"], after)
        except Exception as ex:
            r = {"ok": False, "error": "%s: %s" % (type(ex).__name__, str(ex)[:160])}
        if not r.get("ok"):
            failed += 1
            if replay_enabled():
                # 已提交的业务写不回滚；把投影欠账落库，下次 flush/启动补。
                try:
                    eq = enqueue(conn, kind, obj_id, rec["before"], r.get("error", ""))
                    enqueued += 1 if eq.get("ok") else 0
                except Exception:
                    pass
        results.append({"kind": kind, "id": obj_id,
                        "deleted": r.get("deleted", 0), "inserted": r.get("inserted", 0),
                        "ok": bool(r.get("ok")), "error": r.get("error", "")})
    return {"status": "failed" if failed else "ok", "objects": len(pending),
            "failed": failed, "enqueued": enqueued, "results": results,
            "replay": replay_result}
