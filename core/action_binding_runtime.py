# -*- coding: utf-8 -*-
"""P0-1（2026-11-22 第五批次）绑定规则的**执行侧消费**。

## 职责边界
- `core/action_conditions.py`：纯函数规则表（判定/合流），不碰 DB。
- 本模块：按工具名读绑定 → 构造 post 回读快照 → 调纯函数 → 返回单一状态。
- `agent/pipeline_parts/tools.py::_exec_tool_call`：唯一出口，只挂两行调用。

## 为什么按「工具名」查绑定，而不是等 scope_types 上游
`_build_tools_def` 的两处调用都不传 `scope_types`（无上游生产方），
若执行侧也等它，pre/post 就永远不会被消费，P0-1 只剩配置壳。
绑定表本身 `(type_id, tool_name, enabled=1)` 已是管理员的显式意图表达：
**有绑定才消费，没绑定零回归**——它与"注入过滤默认关"是两个不同的面：
注入过滤决定工具是否展示给模型（开了没数据 ⇒ 工具集体消失）；
本模块决定模型已调用的工具是否要过条件（没配条件 ⇒ 完全无感）。

## 四态口径
复用 `action_conditions.VALID_STATUS`，不另写常量（同一语义只写一份）：
- `failed`   —— 证实条件不满足 ⇒ pre 拒绝执行 / post 把成功回执改判失败
- `unknown`  —— 读不到参数/快照 ⇒ 放行但显式告警，且 `retry_safe=False`
- `verified` —— 证实满足，透明通过
- `skipped`  —— 空条件（无约束），透明通过

## 旁路原则
任何查询异常一律返回 None（= 无约束），**绝不因为观测设施故障阻断工具执行**
（与 `core/idempotency.py` 的 fail-open 同口径）。
"""
from __future__ import annotations

import json
from typing import Any

from core import action_conditions as _ac


def _conn_or_own(conn=None):
    """返回 (conn, owned)；owned=True 时调用方负责关闭。"""
    if conn is not None:
        return conn, False
    from database import get_db
    return get_db(), True


def load_bindings(tool_name: str, conn=None) -> list[dict]:
    """查某工具全部启用绑定；无表/无绑定/异常一律返回空列表。

    ⚠️ 缺表也返回空：老库尚未迁移时不能让工具调用整体崩掉；
       门禁用 G1 单独守表结构（缺失给明确指引），不在这里 fail-closed。
    """
    if not tool_name:
        return []
    c, owned = _conn_or_own(conn)
    try:
        rows = c.execute(
            "SELECT type_id, tool_name, pre_condition, post_condition, enabled, "
            "       rule_version FROM ontology_type_actions "
            "WHERE tool_name=? AND enabled=1 ORDER BY type_id", (tool_name,)).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []
    finally:
        if owned:
            try:
                c.close()
            except Exception:
                pass


def _cell(row, key: str, default=None):
    """兼容 sqlite3.Row / dict 两种行对象。"""
    try:
        return row[key]
    except Exception:
        return default


def _entity_row(conn, eid: Any) -> dict | None:
    """回读实体最新版本；查不到返回 None（供 evaluate 判 failed）。"""
    if eid in (None, ""):
        return None
    try:
        row = conn.execute(
            "SELECT id, name, entity_type, properties, is_current FROM entity_versions "
            "WHERE id=? ORDER BY version_no DESC LIMIT 1", (str(eid),)).fetchone()
        if row is None:
            row = conn.execute(
                "SELECT id, name, entity_type, '{}' AS properties, 1 AS is_current "
                "FROM entities WHERE id=?", (str(eid),)).fetchone()
        if row is None:
            return None
        out = {k: _cell(row, k) for k in ("id", "name", "entity_type",
                                          "properties", "is_current")}
        p = out.get("properties")
        if isinstance(p, str):
            try:
                obj = json.loads(p)
                if isinstance(obj, dict):
                    out.update(obj)          # 展开属性，供 field_equals 直接取字段
            except Exception:
                pass
        return out
    except Exception:
        return None


def build_post_snapshot(conn, args: dict, res: dict, needed: set[str]) -> dict:
    """按规则声明的 key 构造回读快照。

    `action_conditions.evaluate()` 的 snapshot key 是**参数名**（如 `id`），
    不是 id 值；这里严格按 `needed` 填 key，填不到的 key 直接不出现
    ⇒ evaluate 返回 unknown（"无法证实"），而不是伪造一个 failed。

    仅当 needed 含 source/target 时才查 relations；查询失败时不写
    `snapshot["relations"]`，同样是 unknown 而非 failed。
    """
    snap: dict = {}
    if not needed:
        return snap
    args = args or {}
    res = res if isinstance(res, dict) else {}
    fallback_id = res.get("id") or res.get("entity_id") or res.get("node_id")
    for key in needed:
        val = args.get(key)
        if val in (None, "") and key in ("id", "entity_id", "node_id"):
            val = fallback_id
        if val in (None, ""):
            continue                     # 读不到 id ⇒ 不设 key ⇒ unknown
        row = _entity_row(conn, val)
        snap[key] = row                  # None ⇒ failed（证实不存在）

    if "source" in needed or "target" in needed:
        source = args.get("source") or args.get("source_id")
        target = args.get("target") or args.get("target_id")
        if source not in (None, "") or target not in (None, ""):
            try:
                sql = ("SELECT source_id, target_id, relation_type FROM relations WHERE 1=1")
                params = []
                if source not in (None, ""):
                    sql += " AND source_id=?"; params.append(str(source))
                if target not in (None, ""):
                    sql += " AND target_id=?"; params.append(str(target))
                rows = conn.execute(sql, params).fetchall()
                snap["relations"] = [
                    {"source": _cell(r, "source_id"), "target": _cell(r, "target_id"),
                     "type": _cell(r, "relation_type")} for r in rows]
            except Exception:
                pass                     # 不写 key ⇒ unknown
    return snap


def _eval_rows(rows: list[dict], direction: str, args: dict | None = None,
               snapshot: dict | None = None) -> list[dict]:
    out = []
    for b in rows:
        raw = b.get("%s_condition" % direction)
        if not raw:
            continue
        out.append(_ac.evaluate_rules(raw, args=args, snapshot=snapshot, direction=direction))
    return out


def check_pre(tool_name: str, args: dict | None, conn=None) -> dict | None:
    """执行前检查。返回 None（无约束）+ 合流状态 dict。

    本函数**不决定拒绝**——由 `_exec_tool_call` 按 `status` 决定：
    failed ⇒ 拒绝；unknown ⇒ 放行但告警（fail-open）。
    """
    rows = load_bindings(tool_name, conn)
    if not rows:
        return None
    return _ac.combine(_eval_rows(rows, "pre", args=args or {}))


def check_post(tool_name: str, args: dict | None, res: dict, conn=None) -> dict | None:
    """执行后检查：回读快照 + post 合流。无绑定/回读全失败 ⇒ None 或 unknown。"""
    rows = load_bindings(tool_name, conn)
    if not rows:
        return None
    needed = _ac.needed_keys([{"rule": b.get("post_condition")} for b in rows])
    c, owned = _conn_or_own(conn)
    try:
        snap = build_post_snapshot(c, args or {}, res, needed)
        return _ac.combine(_eval_rows(rows, "post", args=args or {}, snapshot=snap))
    finally:
        if owned:
            try:
                c.close()
            except Exception:
                pass
