"""一次性完整性校准：幽灵上架 / 孤儿安装 / 计数漂移。"""
from plugin_system.store.queries import _sync_install_count


def reconcile_integrity(conn) -> dict:
    """一次性完整性校准（幂等，可重复执行）。返回各项修复数量。

    2026-09-17 四刀修复的收尾：本次改造把「软删除连带清理」「计数按行重算」的
    守卫补进了写路径，但**历史遗留的脏数据不会自动消失**。本函数负责把已经产生的
    三类不一致抹平，供启动或运维手动调用（不改业务语义，只让数据自洽）：

      ① 幽灵上架  scope='public' AND status='removed'
         —— 软删旧实现只改 status 不收回可见范围，条目已从 get_plugin() 消失，
            却仍能被市场查询/withdraw_share 命中（真实库 2 条 e2e 残留）。
      ② 孤儿安装  plugin_installs 指向已删除/不存在的插件
         —— 让「我安装的」区出现永远装不上的幽灵条目（真实库 2 组）。
      ③ 计数漂移  plugins.install_count != 实际行数
         —— 市场排序 ORDER BY install_count DESC 与卡片展示都读它（真实库 15 条）。
    """
    ghost = conn.execute(
        "UPDATE plugins SET scope='personal' WHERE scope='public' AND status='removed'").rowcount
    orphan = conn.execute(
        "DELETE FROM plugin_installs WHERE plugin_id IN ("
        "  SELECT i.plugin_id FROM plugin_installs i"
        "  LEFT JOIN plugins p ON p.plugin_id=i.plugin_id"
        "  WHERE p.plugin_id IS NULL OR p.status='removed')").rowcount
    # ③ 全量同步：逐条重算（数量级为插件数，可接受）
    ids = [r["plugin_id"] for r in conn.execute("SELECT plugin_id FROM plugins").fetchall()]
    for pid in ids:
        _sync_install_count(conn, pid)
    conn.commit()
    return {"ghost_scope_fixed": ghost, "orphan_installs_removed": orphan,
            "install_counts_synced": len(ids)}


def count_install_drift(conn) -> int:
    """计数列的失真条数（审计用，只读）。"""
    return conn.execute(
        "SELECT COUNT(*) FROM plugins p WHERE p.install_count != "
        "(SELECT COUNT(*) FROM plugin_installs i WHERE i.plugin_id=p.plugin_id)"
    ).fetchone()[0]
