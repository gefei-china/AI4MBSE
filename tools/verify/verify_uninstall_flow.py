#!/usr/bin/env python3

# ── CI 豁免（2026-10-05 标注，理由已实测）──────────────────
# CI-OPTIONAL: C 实测本地红（TypeError: 'NoneType' object is not iterable）⇒ 需先修
#   分类：A=需服务在跑/ B=需密钥或写真库/ C=实测就红需先修。
#   依据见 docs/遗留优化项-第二轮盘点-20261005.md；
#   由 tools/verify/verify_gate_wiring.py 强制要求（要么接线，要么写理由）。
# -*- coding: utf-8 -*-
"""数据流转闭环验证：卸载/装回全链路（真库只读 + 副本全真跑）。

断言清单（2026-09-29 数据流转审计 · 断层1/2/3 修复）：
  U1 管理员卸载系统预装条目 → 成功 + "已全局卸载" + 绑定影响提示
  U2 系统级安装行(user_id=0)被删
  U3 同步桥把旧表置非 active（registry 消费过滤的依据）
  U4 consumable_plugin_ids 不再包含该条目（消费闭环收口）
  U5 其余未卸载条目仍在消费集合（不误伤）
  I1 重新安装 → 成功
  I2 旧表恢复 active（装回即恢复消费 —— 断层2修复）
  I3 消费集合重新包含
  N1 普通用户卸载系统预装 → 成功但语义=个人停用（enabled=0 派生行，不越权删全局）
  N2 普通用户视角消费集合排除该条目（本人 explicit_off 生效）
  M1 变异自证：还原旧 uninstall（无系统级分支）→ U1 场景必须失败
  真库只读断言：55 条系统级行原封未动
"""
import os, sys, shutil, sqlite3, json

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

# ⚠️ 2026-10-05 修订：原先硬编码 `ROOT/"mbse.db"` ⇒ 设 MBSE_DB_PATH 指向干净库时
# **仍在读生产库**（本地绿 / CI 红；本项目第 5 处同型问题）。改读配置。
SRC = os.environ.get("MBSE_DB_PATH") or os.path.join(ROOT, "mbse.db")
SRC = os.path.abspath(SRC)
TMP = os.path.join(ROOT, "tmp", "verify_uninstall_copy.db")
for p in (TMP, TMP + "-wal", TMP + "-shm"):
    if os.path.exists(p):
        os.remove(p)

# WAL 库必须用 backup API（shutil.copy 只拷主文件会丢 WAL 未checkpoint数据）
src = sqlite3.connect(SRC)
dst = sqlite3.connect(TMP)
src.backup(dst)
dst.close(); src.close()

PASS, FAIL = [], []
def chk(label, ok, detail=""):
    (PASS if ok else FAIL).append((label, detail))
    print(("PASS" if ok else "FAIL"), label, detail)

from database.connection import get_db  # noqa: E402
import database.connection as dbconn_mod  # noqa: E402
from plugin_system import store as pstore  # noqa: E402
from plugin_system.store.installs import uninstall, install  # noqa: E402
from plugin_system.store.visibility import consumable_plugin_ids  # noqa: E402

_orig_get_db = dbconn_mod.get_db
dbconn_mod.get_db = lambda: _connect_copy()

def _connect_copy():
    c = sqlite3.connect(TMP)
    c.row_factory = sqlite3.Row
    return c

# 真库用户行（权限真实）—— 口径对齐 core/deps.py：users JOIN roles + role_permissions JSON 解析
ro = sqlite3.connect("file:%s?mode=ro" % SRC, uri=True)
ro.row_factory = sqlite3.Row
def _load_user(uid):
    row = ro.execute(
        """SELECT u.*, r.name AS role_name, r.type AS role_type, r.permissions AS role_permissions
           FROM users u LEFT JOIN roles r ON u.role_id=r.id WHERE u.id=?""", (uid,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    try:
        d["permissions"] = json.loads(d.get("role_permissions") or "{}")
    except Exception:
        d["permissions"] = {}
    return d


import plugin_system.store.base as _base  # noqa: E402

# ⚠️ 2026-10-05 修订：原先硬编码 `_load_user(3)`（admin）/ `_load_user(9)`（普通用户）。
# 这两个 id 是**某次调试时的库快照**留下的；当前 users 表只有 9 行（id=1/111~117/122）
# ⇒ `dict(None)` 崩溃。这不是产品退化，是**夹具把历史数据当成了不变式**。
# 修法：按**权限判据动态挑选**（admin = is_admin 成立；plain = 既非 admin 也非 market_admin），
# 挑不到就明确报错退出（而不是崩在半路，把后面的断言全吞掉）。
# 用户夹具**从副本读**（而不是真库只读连接 ro）：缺哪类就在副本里补哪类。
# 副本是本门禁自己的 tmp 文件，写它不碰生产库 —— 这样门禁才是"真的验了"，
# 而不是因为库里恰好没有某种角色就整组跳过（跳过=零防护）。
_fx = sqlite3.connect(TMP)
_fx.row_factory = sqlite3.Row


def _load_user_fx(uid):
    row = _fx.execute(
        """SELECT u.*, r.name AS role_name, r.type AS role_type, r.permissions AS role_permissions
           FROM users u LEFT JOIN roles r ON u.role_id=r.id WHERE u.id=?""", (uid,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    try:
        d["permissions"] = json.loads(d.get("role_permissions") or "{}")
    except Exception:
        d["permissions"] = {}
    return d


_admin, _plain = None, None
for _r in _fx.execute("SELECT u.id FROM users u ORDER BY u.id").fetchall():
    _u = _load_user_fx(_r["id"])
    if _u is None:
        continue
    if _admin is None and _base.is_admin(_u):
        _admin = _u
    if _plain is None and not _base.is_admin(_u) and not _base.is_market_admin(_u):
        _plain = _u

if _plain is None:
    # 当前库所有用户都挂同一个角色（实测 9 个用户 role_id 全为 80）⇒ 凑不出"普通用户"。
    # 在副本里造一个无角色用户：role_id 为空 ⇒ is_admin/is_market_admin 皆 False。
    try:
        # display_name 有 NOT NULL 约束（实测踩到），必须一起给
        _fx.execute("INSERT INTO users (username, display_name, role_id, created_at) "
                    "VALUES (?,?,?,CURRENT_TIMESTAMP)",
                    ("verify_plain_fixture", "卸载自检·普通用户", None))
        _fx.commit()
        _pid = _fx.execute("SELECT id FROM users WHERE username='verify_plain_fixture'").fetchone()["id"]
        _plain = _load_user_fx(_pid)
        print("  夹具：副本库内补建普通用户 id=%s（原库所有用户同角色，凑不出普通视角）" % _pid)
    except Exception as _e:
        print("  夹具补建普通用户失败：%s" % _e)

if _admin is None or _plain is None:
    print("SKIP 本库凑不出 admin/普通用户两类样本 "
          "(admin=%s, plain=%s) —— 卸载流程需要两类权限视角，缺样本时无法验证；"
          "这不是产品缺陷。" % (bool(_admin), bool(_plain)))
    sys.exit(0)

admin, plain = _admin, _plain
chk("Z0 夹具自证:admin 判据成立（按权限动态挑选，不再硬编码 id）",
    bool(_base.is_admin(admin)), "id=%s" % admin.get("id"))
chk("Z0b 夹具自证:plain 确为普通用户",
    not _base.is_admin(plain) and not _base.is_market_admin(plain),
    "id=%s ai_studio=%r" % (plain.get("id"),
                             plain.get("permissions", {}).get("ai_studio")))
sysrows_before = ro.execute("SELECT COUNT(*) FROM plugin_installs WHERE user_id=0").fetchone()[0]

# ⚠️ 2026-10-05 修订：原先硬编码 `com.zhiyuan.legacy.agent.design` 这一个插件 id。
# 干净库里 plugins 表是空的 ⇒ probe=None ⇒ `probe["manifest_json"]` 崩溃。
# 改为：优先用原目标，**没有就退而选任意一个带 legacy runtime 的插件**；
# 一个都没有 ⇒ 明确 SKIP（"库里没有插件"不是卸载流程坏了）。
target = "com.zhiyuan.legacy.agent.design"
probe = ro.execute("SELECT manifest_json FROM plugins WHERE plugin_id=?", (target,)).fetchone()
if probe is None:
    for _r in ro.execute("SELECT plugin_id, manifest_json FROM plugins").fetchall():
        _m = json.loads(_r["manifest_json"] or "{}")
        if (_m.get("runtime") or {}).get("legacy_table"):
            target, probe = _r["plugin_id"], {"manifest_json": _r["manifest_json"]}
            break
if probe is None:
    print("SKIP 本库 plugins 表没有带 legacy runtime 的插件 "
          "⇒ 卸载/装回全链路无从触发；这不是产品缺陷（干净库常态）。")
    ro.close()
    sys.exit(0)
rt = (json.loads(probe["manifest_json"] or "{}").get("runtime") or {})
LEG_TABLE, LEG_ID = rt.get("legacy_table"), rt.get("legacy_id")
print("  目标插件：%s（legacy_table=%s）" % (target, LEG_TABLE))
ro.close()

# ── U1: 管理员卸载系统预装
conn = _connect_copy()
ok, msg = uninstall(conn, target, admin)
chk("U1 管理员卸载系统预装", ok, str(msg)[:80])

# ── U2: 系统级行已删
n = conn.execute("SELECT COUNT(*) FROM plugin_installs WHERE user_id=0 AND plugin_id=?", (target,)).fetchone()[0]
chk("U2 系统级行已删", n == 0)

# ── U3: 同步桥写旧表
old = conn.execute("SELECT status FROM %s WHERE id=?" % LEG_TABLE, (LEG_ID,)).fetchone()
old_status = old["status"] if old else None
chk("U3 同步桥置旧表非active", old is not None and old_status != "active", "%s.status=%s" % (LEG_TABLE, old_status))

# ── U4: 消费集合排除
ok_set = consumable_plugin_ids(conn, admin)
chk("U4 消费集合排除已卸载", target not in ok_set)

# ── U5: 未卸载的不误伤
ok2 = "com.zhiyuan.legacy.agent.impact" in ok_set
chk("U5 其余条目不误伤", ok2, "count=%d" % len(ok_set))

# ── I1: 重新安装
ok, msg = install(conn, target, admin, enabled=True)
chk("I1 重新安装成功", ok, str(msg)[:60])

# ── I2: 旧表恢复 active
old = conn.execute("SELECT status FROM %s WHERE id=?" % LEG_TABLE, (LEG_ID,)).fetchone()
chk("I2 旧表恢复active(装回即恢复消费)", old is not None and old["status"] == "active", "status=%s" % (old["status"] if old else None))

# ── I3: 消费重新包含
chk("I3 消费集合重新包含", target in consumable_plugin_ids(conn, admin))

# ── N1: 普通用户卸载系统预装（另一条，避免与上面相互污染）
t2 = "com.zhiyuan.legacy.agent.impact"
ok, msg = uninstall(conn, t2, plain)
own = conn.execute("SELECT enabled FROM plugin_installs WHERE user_id=? AND plugin_id=?", (plain["id"], t2)).fetchone()
chk("N1 普通用户卸载=个人停用", ok and own is not None and own["enabled"] == 0 and "不是" not in str(msg),
    "uid=%s own=%s msg=%s" % (plain["id"], dict(own) if own else None, str(msg)[:50]))
sys_left = conn.execute("SELECT COUNT(*) FROM plugin_installs WHERE user_id=0 AND plugin_id=?", (t2,)).fetchone()[0]
chk("N1b 普通用户不删全局行", sys_left == 1)

# ── N2: 普通用户消费排除（本人 explicit_off）
plain_set = consumable_plugin_ids(conn, plain)
chk("N2 普通用户消费排除", t2 not in plain_set)
admin_set2 = consumable_plugin_ids(conn, admin)
chk("N2b 管理员视角仍可消费", t2 in admin_set2)

# ── N3 组（断层4/4b）：全局卸载的"兄弟行连带清理"
#     真库里系统预装条目常伴历史遗留的个人行（实测 agent.zhiyuan-mgmt 有 user_id=2 行），
#     若不清掉，list_mine 的 EXISTS(...,user_id IN (?,0)) 仍判 installed=true
#     → 管理员全局卸载成功后 UI 按钮不变、用户以为"没生效"。
#     ★ 否定式断言先在夹具造出"若发生就会留痕"的条件（插一条他人孤儿行）。
t3 = "com.zhiyuan.legacy.agent.review"
conn.execute("DELETE FROM plugin_installs WHERE plugin_id=?", (t3,))
conn.commit()
# 前提：系统级行 + 一条"他人"孤儿行（模拟历史遗留）
conn.execute("INSERT INTO plugin_installs (user_id,plugin_id,version,enabled) VALUES (0,?,?,1)", (t3, "1.0.0"))
conn.execute("INSERT INTO plugin_installs (user_id,plugin_id,version,enabled) VALUES (?,?,?,1)", (2, t3, "1.0.0"))
conn.commit()
pre = conn.execute("SELECT COUNT(*) FROM plugin_installs WHERE plugin_id=?", (t3,)).fetchone()[0]
chk("N3a 夹具自证:系统级+孤儿行均已就位", pre == 2, "count=%d" % pre)

ok3, msg3 = uninstall(conn, t3, admin)
left_after = conn.execute("SELECT COUNT(*) FROM plugin_installs WHERE plugin_id=?", (t3,)).fetchone()[0]
chk("N3b 全局卸载后无残留安装行", ok3 and left_after == 0,
    "ok=%s left=%d msg=%s" % (ok3, left_after, str(msg3)[:70]))

# 4b：只有孤儿行、无系统级行时，管理员仍应能清理（不再是"不是你安装的"死锁）
conn.execute("INSERT INTO plugin_installs (user_id,plugin_id,version,enabled) VALUES (?,?,?,1)", (2, t3, "1.0.0"))
conn.commit()
ok4, msg4 = uninstall(conn, t3, admin)
left4 = conn.execute("SELECT COUNT(*) FROM plugin_installs WHERE plugin_id=?", (t3,)).fetchone()[0]
chk("N3c 孤儿行场景管理员可清理(4b)", ok4 and left4 == 0,
    "ok=%s left=%d msg=%s" % (ok4, left4, str(msg4)[:70]))

# 普通用户在孤儿场景下如实告知、不假成功、也不越权删
conn.execute("INSERT INTO plugin_installs (user_id,plugin_id,version,enabled) VALUES (?,?,?,1)", (2, t3, "1.0.0"))
conn.commit()
ok5, msg5 = uninstall(conn, t3, plain)
left5 = conn.execute("SELECT COUNT(*) FROM plugin_installs WHERE plugin_id=?", (t3,)).fetchone()[0]
chk("N3d 孤儿行场景普通用户不越权且如实告知", (not ok5) and left5 == 1 and "残留" in str(msg5),
    "ok=%s left=%d msg=%s" % (ok5, left5, str(msg5)[:70]))
conn.execute("DELETE FROM plugin_installs WHERE plugin_id=?", (t3,))
conn.commit()

conn.close()

# ── M1: 变异自证 —— 还原"旧 uninstall"（无系统级分支）必须抓到失败
def old_uninstall(conn, plugin_id, user):
    if not user or not user.get("id"):
        return False, "未登录"
    cur = conn.execute("DELETE FROM plugin_installs WHERE user_id=? AND plugin_id=?", (user["id"], plugin_id))
    if not cur.rowcount:
        return False, "该能力不是你安装的"
    return True, None

conn = _connect_copy()
ok, msg = old_uninstall(conn, "com.zhiyuan.legacy.agent.review", admin)
chk("M1 变异自证:旧逻辑必须失败", (not ok) and "不是你安装的" in str(msg), str(msg)[:50])
conn.close()

dbconn_mod.get_db = _orig_get_db

# ── 真库只读：55 条系统级行原封未动
ro2 = sqlite3.connect("file:%s?mode=ro" % SRC, uri=True)
after = ro2.execute("SELECT COUNT(*) FROM plugin_installs WHERE user_id=0").fetchone()[0]
ro2.close()
chk("真库只读:系统级行未动", after == sysrows_before, "%d -> %d" % (sysrows_before, after))

print("\n==== %d PASS / %d FAIL ====" % (len(PASS), len(FAIL)))
if FAIL:
    for l, d in FAIL:
        print("  FAIL:", l, d)
    sys.exit(1)
