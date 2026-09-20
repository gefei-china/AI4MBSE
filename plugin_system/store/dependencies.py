"""能力依赖索引：落库、查询与影响面分析（plugin_dependencies 表）。"""
import sqlite3

from plugin_system.manifest import parse_dependencies  # 依赖契约解析
from plugin_system.store.queries import get_plugin


# ── P0-2：能力依赖索引（2026-09-16）──
# 依赖契约的解析（parse_dependencies）定义于 plugin_system.manifest —— manifest 是契约层，
# store 是持久层，依赖方向单向，避免循环 import。此处只负责落库与查询。
def _sync_deps_safe(conn, plugin_id: str, manifest: dict):
    """写依赖索引的内层封装：表缺失（未跑迁移的老库）时静默降级，绝不阻断主流程。"""
    try:
        sync_dependencies(conn, plugin_id, manifest)
    except sqlite3.OperationalError:
        pass


def _sync_legacy_safe(conn, plugin_id: str, available: bool):
    """plugins → 旧表可用性同步（同步桥方向 B，2026-09-16）。

    为什么必须有：AI 运行时读的是旧表（skills/tools/mcp_servers/agents/prompts），
    能力中心改的却是 plugins。缺了这一步，就会出现
    「管理员在能力中心下架了能力，AI 照旧调用」的安全隐患。

    - 只动旧表的可用性开关，不碰业务字段
    - 无 legacy 映射的插件（纯插件能力）内部会安全跳过
    - 任何异常一律吞掉：管理动作不应因同步失败而失败
    """
    try:
        from plugin_system.legacy_sync import sync_to_legacy_safe
        sync_to_legacy_safe(conn, plugin_id, available)
    except Exception:
        pass


def sync_dependencies(conn, plugin_id: str, manifest: dict) -> int:
    """按 manifest 重建该插件的依赖边（先删后插，幂等）。返回写入行数。"""
    rows = parse_dependencies(manifest)
    conn.execute("DELETE FROM plugin_dependencies WHERE consumer_id=?", (plugin_id,))
    for r in rows:
        resolved = 0
        if r["provider_id"]:
            hit = conn.execute(
                "SELECT 1 FROM plugins WHERE plugin_id=? AND status!='removed'",
                (r["provider_id"],)).fetchone()
            resolved = 1 if hit else 0
        conn.execute(
            """INSERT OR REPLACE INTO plugin_dependencies
               (consumer_id, provider_id, provider_ref, kind, required, resolved)
               VALUES (?,?,?,?,?,?)""",
            (plugin_id, r["provider_id"], r["provider_ref"], r["kind"], r["required"], resolved))
    conn.commit()
    return len(rows)


def list_dependencies(conn, plugin_id: str) -> list:
    """我依赖谁（安装依赖解析用）。"""
    return [dict(r) for r in conn.execute(
        "SELECT * FROM plugin_dependencies WHERE consumer_id=? ORDER BY kind, id",
        (plugin_id,)).fetchall()]


def list_dependents(conn, provider_id: str, required_only: bool = False) -> list:
    """谁依赖我（卸载前置检查入口）。"""
    sql = "SELECT * FROM plugin_dependencies WHERE provider_id=?"
    if required_only:
        sql += " AND required=1"
    return [dict(r) for r in conn.execute(sql + " ORDER BY id", (provider_id,)).fetchall()]


def dependency_impact(conn, plugin_id: str) -> dict:
    """停用/卸载前置检查：返回受影响消费者清单。

    - safe=True   无硬依赖，可直接停用
    - safe=False  blocking 非空，须二次确认（UX 层应列出完整 consumer 清单）
    返回结果可直接喂给 services/impact_engine 做可视化。
    """
    blocking, optional = [], []
    for r in list_dependents(conn, plugin_id):
        p = get_plugin(conn, r["consumer_id"])
        item = {"plugin_id": r["consumer_id"],
                "name": (p or {}).get("name") or r["consumer_id"],
                "status": (p or {}).get("status") or "",
                "kind": r["kind"], "provider_ref": r["provider_ref"]}
        (blocking if r["required"] else optional).append(item)
    return {"blocking": blocking, "optional": optional, "safe": not blocking}
