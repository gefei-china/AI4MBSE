# -*- coding: utf-8 -*-
"""记忆分层（core / recall / archival）的自检（P1-18）。

验证 memory_service 的三层读写语义：
- deposit：preference 恒 core，其余 recall
- forget：遗忘同步标 archival
- search：常规召回排除 archival
- search_core：只取 core 层（无条件常驻注入的取数面）

用临时库（mkdtemp）建最小 agent_memory 表（含 tier 列），不污染真库；含变异自证。
"""
import os
import sqlite3
import sys
import tempfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.chdir(ROOT)

from memory_service import MemoryService  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s%s" % ("PASS" if cond else "FAIL", name,
                         ("  <- " + str(detail)) if detail else ""), flush=True)


def _fixture():
    """建最小 agent_memory 表（含 tier 列）的临时库连接。"""
    d = tempfile.mkdtemp(prefix="memtier_")
    conn = sqlite3.connect(os.path.join(d, "t.db"))
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE agent_memory (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        agent_id TEXT NOT NULL,
        mem_type TEXT DEFAULT 'fact',
        content TEXT DEFAULT '',
        embedding TEXT DEFAULT '[]',
        embed_version TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        source TEXT DEFAULT 'agent',
        relevance REAL DEFAULT 1.0,
        activation REAL DEFAULT 1.0,
        access_count INTEGER DEFAULT 0,
        last_accessed_at TEXT DEFAULT '',
        forgotten INTEGER DEFAULT 0,
        mem_topic TEXT DEFAULT '',
        scope_type TEXT DEFAULT '',
        scope_id TEXT DEFAULT '',
        tier TEXT DEFAULT 'recall'
    )""")
    conn.commit()
    return conn


print("[F] 三层读写语义")

conn = _fixture()
try:
    # F1: deposit preference → core
    _id1 = MemoryService.deposit(conn, "test_agent", "用户偏好中文输出", mem_type="preference")
    _r1 = conn.execute("SELECT tier, mem_type FROM agent_memory WHERE id=?", (_id1,)).fetchone()
    check("F1 deposit preference → tier=core", _r1["tier"] == "core", dict(_r1))

    # F2: deposit fact/experience → recall
    _id2 = MemoryService.deposit(conn, "test_agent", "链路预算按 xx 计算", mem_type="fact")
    _id3 = MemoryService.deposit(conn, "test_agent", "先解析需求再建模", mem_type="experience")
    _r2 = conn.execute("SELECT tier FROM agent_memory WHERE id=?", (_id2,)).fetchone()
    _r3 = conn.execute("SELECT tier FROM agent_memory WHERE id=?", (_id3,)).fetchone()
    check("F2 deposit fact/experience → tier=recall",
          _r2["tier"] == "recall" and _r3["tier"] == "recall",
          "fact=%s exp=%s" % (_r2["tier"], _r3["tier"]))

    # F3: forget → archival（同时 forgotten=1）
    MemoryService.forget(conn, agent_id="test_agent", max_unused_days=1,
                         max_age_days=0, min_access=0)
    # 刚写入的 memory last_accessed_at 为空 → 回退 created_at → age≈0，不会遗忘。
    # 手动构造一条"久未访问"的再 forget，验证 archival 标记。
    conn.execute("UPDATE agent_memory SET created_at='2020-01-01 00:00:00', last_accessed_at='2020-01-01 00:00:00' WHERE id=?", (_id2,))
    conn.commit()
    n = MemoryService.forget(conn, agent_id="test_agent", max_unused_days=1, max_age_days=0, min_access=0)
    _r2b = conn.execute("SELECT tier, forgotten FROM agent_memory WHERE id=?", (_id2,)).fetchone()
    check("F3 forget → tier=archival 且 forgotten=1",
          _r2b["tier"] == "archival" and _r2b["forgotten"] == 1,
          "forgotten=%s tier=%s (遗忘 %d 条)" % (_r2b["forgotten"], _r2b["tier"], n))

    # F4: search 排除 archival —— 加一条 recall 的同类记忆，证明 search 能召回 recall 却排除 archival
    _id4 = MemoryService.deposit(conn, "test_agent", "链路预算按功率谱密度估算", mem_type="fact")
    _hits = MemoryService.search(conn, "test_agent", query="链路预算", top_k=10)
    _hit_ids = {h["id"] for h in _hits}
    check("F4 search 召回 recall 的 _id4、排除 archival 的 _id2",
          _id4 in _hit_ids and _id2 not in _hit_ids, "hit_ids=%s" % sorted(_hit_ids))

    # F5: search_core 只取 core 层
    _core = MemoryService.search_core(conn, "test_agent", top_k=5)
    _core_ids = {h["id"] for h in _core}
    check("F5 search_core 只取 core（含 preference 的 _id1，不含 recall）",
          _id1 in _core_ids and _id3 not in _core_ids, "core_ids=%s" % sorted(_core_ids))

    # F6: 无 tier 列（老库）→ search_core 返回空（零漂移）
    conn.execute("ALTER TABLE agent_memory RENAME TO agent_memory_old")
    conn.execute("""CREATE TABLE agent_memory (
        id INTEGER PRIMARY KEY AUTOINCREMENT, agent_id TEXT, mem_type TEXT DEFAULT 'fact',
        content TEXT DEFAULT '', embedding TEXT DEFAULT '[]', created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        source TEXT DEFAULT 'agent', relevance REAL DEFAULT 1.0, embed_version TEXT DEFAULT '',
        activation REAL DEFAULT 1.0, access_count INTEGER DEFAULT 0, last_accessed_at TEXT DEFAULT '',
        forgotten INTEGER DEFAULT 0, mem_topic TEXT DEFAULT '', scope_type TEXT DEFAULT '', scope_id TEXT DEFAULT ''
    )""")
    conn.commit()
    _core_old = MemoryService.search_core(conn, "test_agent", top_k=5)
    check("F6 无 tier 列（老库）→ search_core 返回空（零漂移）", _core_old == [])
finally:
    conn.close()


print("[M] 变异自证（写侧分层非空转）")
import inspect
import textwrap

_src = textwrap.dedent(inspect.getsource(MemoryService.deposit)).replace("\r\n", "\n")


def _twin(mutate, label):
    mut = mutate(_src)
    check("M%s 变异锚点命中" % label, mut != _src, "未命中")
    ns = {"json": __import__("json"), "MemoryService": MemoryService}
    exec(compile(mut, "<deposit_twin>", "exec"), ns)
    return ns["deposit"]


# M1: 写侧分层被破坏（preference 不再进 core）→ search_core 取不到 preference
_fn1 = _twin(lambda s: s.replace('_tier = "core" if mem_type == "preference" else "recall"',
                                 '_tier = "recall"'), "1")
conn2 = _fixture()
try:
    _id = _fn1(conn2, "test_agent", "用户偏好中文输出", mem_type="preference")
    _r = conn2.execute("SELECT tier FROM agent_memory WHERE id=?", (_id,)).fetchone()
    check("M1 撤销写侧分层 → preference 落 recall（被抓住）", _r["tier"] == "recall", dict(_r))
finally:
    conn2.close()


# ── 汇总 ───────────────────────────────────────────────────────
print()
print("=" * 78)
print("PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
for f in FAIL:
    print("  FAIL:", f)
raise SystemExit(1 if FAIL else 0)
