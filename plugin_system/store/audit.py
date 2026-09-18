"""审计、审核队列与版本/授权/调用日志的读取。"""
import json


def log_audit(conn, user, plugin_id, action, detail=None, ip=""):
    user_name = (user or {}).get("display_name") or (user or {}).get("username") or "未登录"
    user_id = (user or {}).get("id") or 0
    conn.execute(
        """INSERT INTO plugin_audit_logs (user_id, user_name, plugin_id, action, detail_json, ip)
           VALUES (?,?,?,?,?,?)""",
        (user_id, user_name, plugin_id, action, json.dumps(detail or {}, ensure_ascii=False), ip))


def log_call(conn, plugin_id, tool_name, params, status, latency_ms=0, tokens=0):
    conn.execute(
        """INSERT INTO plugin_call_logs (plugin_id, tool_name, params_snapshot, status, latency_ms, tokens)
           VALUES (?,?,?,?,?,?)""",
        (plugin_id, tool_name, json.dumps(params or {}, ensure_ascii=False)[:2000],
         status, latency_ms, tokens))


def pending_reviews(conn):
    """待审列表。

    P1-7（2026-09-16）：审核对象已从「发布」改为「上架」——
      · scope='pending_public'  上架申请（新语义，主路径）
      · status='submitted'      历史/兼容（旧语义：发布需审核）

    2026-09-17（P1-3）：每条附 `review_kind`，供前端区分同一「通过」按钮的两种后果 ——
      · 'share'         上架审核：通过 → scope=public（**条目进入市场**）
      · 'publish_legacy' 旧发布审核：通过 → status=published（**不上架**，仅恢复可用）
    此前两者混装且无标记，管理端渲染成同一个按钮，点击效果不同（P1-3）。
    """
    rows = conn.execute(
        """SELECT * FROM plugins
           WHERE status='submitted' OR scope='pending_public'
           ORDER BY updated_at ASC""").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["review_kind"] = ("share" if d.get("scope") == "pending_public"
                            else "publish_legacy")
        out.append(d)
    return out


def list_audit(conn, plugin_id="", limit=100):
    sql = "SELECT * FROM plugin_audit_logs"
    params = []
    if plugin_id:
        sql += " WHERE plugin_id=?"
        params.append(plugin_id)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def list_versions(conn, plugin_id):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM plugin_versions WHERE plugin_id=? ORDER BY id DESC", (plugin_id,)).fetchall()]


def list_reviews(conn, plugin_id):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM plugin_reviews WHERE plugin_id=? ORDER BY id DESC", (plugin_id,)).fetchall()]


def list_grants(conn, plugin_id):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM plugin_grants WHERE plugin_id=?", (plugin_id,)).fetchall()]


def list_call_logs(conn, plugin_id="", limit=100):
    sql = "SELECT * FROM plugin_call_logs"
    params = []
    if plugin_id:
        sql += " WHERE plugin_id=?"
        params.append(plugin_id)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in conn.execute(sql, params).fetchall()]
