# -*- coding: utf-8 -*-
"""遗忘引擎验证（2026-09-29）。

证明目标（三条，缺一不可）：
  A. **旧公式已死**：原 `activation * max(0.4, 1-age/max_age)` 对任何正常沉淀行都
     >= 0.4 > 0.2 → 恒不触发。用 fixture 行复算旧公式，断言"按旧判据一条都不该忘"。
  B. **新判据真的会开火**：造一条 last_accessed_at = 100 天前的行，
     `forget(max_unused_days=30)` 必须把它标记 forgotten=1；且"刚访问过"的行**不受牵连**。
  C. **变异测试**：把判据 1 的 `if max_unused_days and max_unused_days > 0` 改成恒假
     （模拟"判据写了但没接上"），断言 B 必须 FAIL；还原后必须重新 PASS。

纪律提醒（本项目已踩过）：
  - 夹具建在**数据源侧**（真库），不靠前端/内存造状态；
  - 否定式断言必须让"坏行为"有可观测后果（B 里放了对照行，不是空库）；
  - 验证删除结果要**开新连接**（旧连接读旧事务视图会假 FAIL）；
  - 变异后逐文件 diff 校验还原。
"""
import os
import sqlite3
import sys
import tempfile
import time
import shutil

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from memory_service import MemoryService  # noqa: E402

PASS = []
FAIL = []


def ck(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  -> " + str(extra)) if extra else ""))


def _mkdb():
    """建最小库 + 让生产代码自己补 schema（不手抄 DDL，见项目纪律）。"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS agent_memory (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        agent_id TEXT DEFAULT '',
        content TEXT DEFAULT '',
        mem_type TEXT DEFAULT 'fact',
        activation REAL DEFAULT 1.0,
        access_count INTEGER DEFAULT 0,
        created_at TEXT,
        last_accessed_at TEXT,
        forgotten INTEGER DEFAULT 0,
        embedding TEXT,
        embed_version TEXT,
        scope_type TEXT DEFAULT '',
        scope_id TEXT DEFAULT ''
    )""")
    conn.commit()
    return path, conn


def _ins(conn, content, created_days_ago, last_access_days_ago=None, access=0, activation=1.0):
    def _ts(d):
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - d * 86400))
    conn.execute(
        "INSERT INTO agent_memory (agent_id, content, mem_type, activation, access_count,"
        " created_at, last_accessed_at, forgotten) VALUES (?,?,?,?,?,?,?,0)",
        ("chat", content, "experience", activation, access,
         _ts(created_days_ago), _ts(last_access_days_ago if last_access_days_ago is not None else created_days_ago)))
    conn.commit()
    return conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]


def _forgotten(conn, mid):
    """开新连接查（避免旧事务视图假 FAIL）。"""
    p = conn.execute("PRAGMA database_list").fetchone()[2]
    c2 = sqlite3.connect(p)
    try:
        return int(c2.execute("SELECT forgotten FROM agent_memory WHERE id=?", (mid,)).fetchone()[0])
    finally:
        c2.close()


def main():
    dbp, conn = _mkdb()
    print("fixture db:", dbp)

    print("\n== 夹具自证 ==")
    mid_old = _ins(conn, "旧经验：包结构必须先定下来再生成需求条目", 200, last_access_days_ago=100)
    mid_new = _ins(conn, "新经验：刚用过的结论，不应被遗忘", 3, last_access_days_ago=0, access=5)
    mid_fresh = _ins(conn, "沉淀 2 天、无人访问的条目（在 90 天门槛内）", 2)
    total = conn.execute("SELECT COUNT(*) c FROM agent_memory WHERE forgotten=0").fetchone()["c"]
    ck("夹具自证：3 条未遗忘行、含 1 条 100 天未访问 + 2 条对照", total == 3, total)

    print("\n== A. 旧公式已死（数学穷举对照）==")
    rows = conn.execute("SELECT id, activation, access_count, created_at, last_accessed_at FROM agent_memory").fetchall()
    now = time.time()
    old_hits = []
    for r in rows:
        la = r["last_accessed_at"] or r["created_at"]
        ts = time.mktime(time.strptime(la, "%Y-%m-%d %H:%M:%S"))
        age = max(0, (now - ts) / 86400)
        decayed = float(r["activation"] or 0) * max(0.4, 1.0 - age / 90)
        if decayed < 0.2:
            old_hits.append(r["id"])
    ck("A. 旧 activation 公式对全部 3 行都不触发（含 100 天未访问行）",
       len(old_hits) == 0, f"old_hits={old_hits}")

    print("\n== B. 新判据真的开火 ==")
    n = MemoryService.forget(conn, max_unused_days=30)
    print(f"  forget(max_unused_days=30) -> {n}")
    ck("B1. 100 天未访问的旧经验被遗忘", _forgotten(conn, mid_old) == 1)
    ck("B2. 今天访问过的新经验未被牵连", _forgotten(conn, mid_new) == 0)
    ck("B3. 2 天龄的条目未被牵连（未达门槛）", _forgotten(conn, mid_fresh) == 0)
    ck("B4. 返回条数 == 1", n == 1, n)

    print("\n== B'. 判据 2：陈旧且从未被访问 ==")
    mid_stale = _ins(conn, "沉淀后 120 天从没被任何人用过的条目", 120, last_access_days_ago=120, access=0)
    mid_used = _ins(conn, "沉淀 120 天但被用过 7 次的条目", 120, last_access_days_ago=120, access=7)
    n2 = MemoryService.forget(conn, max_age_days=90, min_access=0)
    print(f"  forget(max_age_days=90, min_access=0) -> {n2}")
    ck("B'1. 陈旧且零访问 → 遗忘", _forgotten(conn, mid_stale) == 1)
    ck("B'2. 陈旧但有使用证据 → 保留", _forgotten(conn, mid_used) == 0)
    ck("B'3. 关闭时效判据时旧经验不再被追加遗忘", n2 == 1, n2)

    print("\n== B''. 零值防误配 ==")
    mid_x = _ins(conn, "任意条目", 999, last_access_days_ago=999)
    n3 = MemoryService.forget(conn, max_unused_days=0, max_age_days=0)
    ck("B''1. max_unused_days=0 且 max_age_days=0 → 一条也不删（防误配清库）",
       n3 == 0 and _forgotten(conn, mid_x) == 0, n3)

    print("\n== C. 变异测试：还原「真正的坏行为」= forget 直接返回 0 ==")
    # 说明：第一版变异只掐断判据 1，结果 C1 未抓到 —— 因为判据 2（max_age_days=90 默认值）
    # 是**冗余防线**，照样吃掉 100 天未访问的行。按项目纪律：有冗余防线时，
    # 变异必须还原"真正的坏行为"本身（旧版死代码 = 一条都删不掉），而不是摘一道防线。
    target = os.path.join(_ROOT, "memory_service.py")
    bak = target + ".mutbak"
    shutil.copy2(target, bak)
    src = open(target, encoding="utf-8").read()
    anchor = "        import time as _t\n        try:\n            where = \"WHERE forgotten=0\""
    if src.count(anchor) != 1:
        ck("C0. 变异锚点唯一命中", False, f"count={src.count(anchor)}")
    else:
        ck("C0. 变异锚点唯一命中", True)
        # 还原死代码形态：函数入口直接返回 0，不做任何标记
        mutated = src.replace(anchor, "        import time as _t\n        return 0\n        try:\n            where = \"WHERE forgotten=0\"")
        open(target, "w", encoding="utf-8").write(mutated)
        try:
            import importlib
            dbp2, conn2 = _mkdb()
            m_old = _ins(conn2, "旧经验", 200, last_access_days_ago=100)
            import memory_service as _ms2
            importlib.reload(_ms2)
            n_bad = _ms2.MemoryService.forget(conn2, max_unused_days=30)
            fired = _forgotten(conn2, m_old) == 1
            conn2.close()
            ck("C1. 还原死代码后，旧经验不再被遗忘（说明 B 确实由新判据驱动，非空转断言）",
               (fired is False) and n_bad == 0, f"fired={fired} n={n_bad}")
        finally:
            os.replace(bak, target)
            import importlib
            import memory_service as _ms
            importlib.reload(_ms)
            print("  [restored] memory_service.py 已还原")

    print("\n== C2. 变异测试（补）：摘掉判据 1，验证判据 2 的唯一能力 ==")
    # 防"冗余防线掩盖缺陷":造一条**不满足判据2**（有使用证据）但久未访问的行，
    # 此时只有判据 1 能救它 → 掐断判据 1 必须导致漏删。
    src2 = open(target, encoding="utf-8").read()
    anchor1 = "                if max_unused_days and max_unused_days > 0 and age_days >= max_unused_days:"
    if src2.count(anchor1) != 1:
        ck("C2-0. 判据1 锚点唯一命中", False, f"count={src2.count(anchor1)}")
    else:
        ck("C2-0. 判据1 锚点唯一命中", True)
        shutil.copy2(target, bak)
        open(target, "w", encoding="utf-8").write(src2.replace(anchor1, "                if False:"))
        try:
            import importlib
            dbp4, conn4 = _mkdb()
            m_only1 = _ins(conn4, "久未访问但用过很多次的行（仅判据1可删）", 200,
                           last_access_days_ago=100, access=9)
            import memory_service as _ms4
            importlib.reload(_ms4)
            n_c2 = _ms4.MemoryService.forget(conn4, max_unused_days=30, max_age_days=90, min_access=0)
            hit = _forgotten(conn4, m_only1) == 1
            conn4.close()
            ck("C2-1. 掐断判据1 → 该行漏删（证明判据1 不可替代）", hit is False, f"n={n_c2} hit={hit}")
        finally:
            os.replace(bak, target)
            import importlib
            import memory_service as _ms
            importlib.reload(_ms)
            print("  [restored] memory_service.py 已还原")

    print("\n== C'. 还原后基线复跑 ==")
    dbp3, conn3 = _mkdb()
    m1 = _ins(conn3, "旧经验", 200, last_access_days_ago=100)
    m2 = _ins(conn3, "新经验", 3, last_access_days_ago=0)
    import importlib
    import memory_service as _ms
    importlib.reload(_ms)
    n4 = _ms.MemoryService.forget(conn3, max_unused_days=30)
    ck("C'1. 还原后新判据恢复开火", n4 == 1 and _forgotten(conn3, m1) == 1 and _forgotten(conn3, m2) == 0, n4)
    conn3.close()

    conn.close()
    try:
        os.remove(dbp)
    except Exception:
        pass

    print("\n" + "=" * 60)
    print(f"PASS {len(PASS)} / FAIL {len(FAIL)}")
    if FAIL:
        for f in FAIL:
            print("  FAIL:", f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
