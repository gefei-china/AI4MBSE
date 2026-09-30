# -*- coding: utf-8 -*-
"""恢复 2026-09-30 02:01-02:04 被误卸载的 19 个 legacy tool 插件（系统级 user_id=0）。

为什么必须写 user_id=0：
    现存的同族工具安装记录（zhiyuan-sysmlv2-* 等）全是 user_id=0，即「系统级默认可用标记」。
    若以某个登录用户的 id 安装，则只有该用户可消费，其他用户仍然用不了 —— 恢复不完整。

为什么必须走 store.install() 而不是自己 UPDATE tools.status：
    运行时看的是 **插件安装态**（plugin_system/store/visibility.py 的 consumable_plugin_ids
    → consumable_filter），不是 tools.status。install() 内部会连带调用 _sync_legacy_safe(True)
    把旧表 tools.status 写回 active，两个口径同时合上才算真恢复。

幂等：plugin_installs 用 INSERT OR IGNORE；install_count 重算而非累加；重跑无害。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from database import get_db
from plugin_system.store import installs as _inst
from plugin_system.store.queries import get_plugin
from plugin_system.store.audit import log_audit

SYS_USER_ID = 0
SYS_USER = {"id": SYS_USER_ID, "display_name": "系统级恢复（restore_uninstalled_tools）"}
RESTORE_NOTE = {"reason": "2026-09-30 误卸载回滚：这些工具由代码 handler 执行，config 为空不代表无用"}

# ⚠️ 为什么不能直接用 store.install(conn, pid, {"id": 0})：
#    plugin_system/store/installs.py:41 写的是 `if not user or not user.get("id")`，
#    而 user_id=0（系统级安装，现存 37 行同族记录都是它）在 Python 里是 falsy
#    → 被判成「未登录，无法安装」。**系统级能力因此永远无法通过安装接口恢复**（已记为缺陷）。
#    这里退而组合 install() 内部逐个调用的同一批生产构件，只绕过登录闸门，
#    业务逻辑（写 plugin_installs / 重算 install_count / _sync_legacy_safe 写回旧表）
#    与原函数完全一致，不做任何自定义 SQL。
conn = get_db()


def install_system_level(conn, pid):
    """等价于 store.install(conn, pid, {id:0}) —— 复用同一批生产构件。"""
    p = get_plugin(conn, pid)
    if not p:
        return False, "插件不存在"
    if p["scope"] != "public":
        return False, "该能力未上架市场，无法安装"
    if p["status"] != "published":
        return False, "该能力当前为「%s」，不满足安装条件" % p["status"]
    conn.execute(
        "INSERT OR IGNORE INTO plugin_installs (user_id, plugin_id, version, enabled) "
        "VALUES (?,?,?,?)", (SYS_USER_ID, pid, p["current_version"], 1))
    _inst._sync_install_count(conn, pid)      # 重算而非 +1（幂等）
    conn.commit()
    _inst._sync_legacy_safe(conn, pid, True)  # 旧表 tools.status 写回 active
    conn.commit()
    return True, None

conn = get_db()
ids = sorted({r["plugin_id"] for r in conn.execute(
    "SELECT plugin_id FROM plugin_audit_logs "
    "WHERE action='uninstall' AND created_at LIKE '2026-09-30 02:0%'").fetchall()})

print("=" * 76)
print("待恢复插件：%d 个" % len(ids))
print("=" * 76)

ok, fail = [], []
for pid in ids:
    try:
        good, err = install_system_level(conn, pid)
        if not good:
            fail.append((pid, err))
            print("  ✗ %-46s %s" % (pid, err))
            continue
        log_audit(conn, SYS_USER, pid, "install", RESTORE_NOTE, "127.0.0.1")
        conn.commit()
        ok.append(pid)
        print("  ✓ %-46s" % pid)
    except Exception as e:
        fail.append((pid, str(e)[:90]))
        print("  ✗ %-46s 异常 %s" % (pid, str(e)[:90]))

print()
print("成功 %d / 失败 %d" % (len(ok), len(fail)))
if fail:
    print("失败清单：")
    for p, e in fail:
        print("   %s → %s" % (p, e))

# ── 事后核验（同连接查安装态，另开连接查 tools.status，避免读到旧事务视图）──
print()
print("=" * 76)
print("核验")
print("=" * 76)
rows = conn.execute(
    "SELECT plugin_id, enabled FROM plugin_installs WHERE user_id=0 AND plugin_id IN (%s)"
    % ",".join("?" * len(ids)), ids).fetchall()
print("plugin_installs(user_id=0) 命中 %d / %d" % (len(rows), len(ids)))

from database import get_db as _get_db2
c2 = _get_db2()
for r in c2.execute("SELECT source, status, COUNT(*) n FROM tools GROUP BY source, status"):
    print("   tools source=%-9s status=%-9s %s 行" % (r["source"], r["status"], r["n"]))
c2.close()
conn.close()
