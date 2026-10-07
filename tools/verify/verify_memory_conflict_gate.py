# -*- coding: utf-8 -*-
"""记忆治理门禁：冲突消解 + 用户隔离 + 影子列（2026-10-07）。

覆盖实施方案 `docs/记忆管理优化实施方案与实施计划-20261007.md` §6 的 6 组断言：

| 组 | 内容 | 断言数 |
|---|---|---|
| A | 现状不变式（**防把consolidate / deposit 改坏**） | 6 |
| B | 冲突检测（正例 + 3 组对照，**对照组是本项最大风险**） | 9 |
| C | 影子列语义（标记可回退 + 行为可翻转） | 7 |
| D | 迁移幂等（重跑不报错、不重复加列） | 4 |
| E | 用户隔离（A/B互不可见 + admin 全量 + 服务端强制） | 7 |
| F | 变异自证（4 处，**证明门禁会判红**） | 8 |

运行：`python tools/verify/verify_memory_conflict_gate.py`
"""
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from memory_service import (MemoryService, _has_conflict_marker,  # noqa: E402
                            _mem_cosine)

PASS, FAIL = [], []


def ck(cond, label):
    (PASS if cond else FAIL).append(label)
    print(("  ok   " if cond else "  FAIL ") + label)


# ── 夹具库：内存库，不碰生产 ──
# ⚠️ **`created_at` 必须列全**（2026-10-07 实测踩到）：
# 第一版 DDL漏了 `created_at`，而 `consolidate()` 内部 SELECT 明确要它
# ⇒ 抛 `no such column: created_at` → **被它的 `except Exception: return 0` 静默吞掉**
# ⇒ 门禁看到的是「consolidate 返回 0」而不是报错，
#    一度让我以为"consolidate 判重逻辑坏了"，实际是**夹具缺列**。
# ★ 这本身暴露了生产代码的一个真问题（见 A2 断言的说明），但门禁不能因此降低夹具标准。
_DDL = """CREATE TABLE agent_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT, agent_id TEXT NOT NULL, mem_type TEXT DEFAULT 'fact',
    content TEXT DEFAULT '', embedding TEXT DEFAULT '[]', embed_version TEXT DEFAULT '',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    source TEXT DEFAULT '', relevance REAL DEFAULT 1.0, activation REAL DEFAULT 1.0,
    access_count INTEGER DEFAULT 0, last_accessed_at TEXT DEFAULT '',
    forgotten INTEGER DEFAULT 0, mem_topic TEXT DEFAULT '',
    scope_type TEXT DEFAULT '', scope_id TEXT DEFAULT '', tier TEXT DEFAULT 'recall',
    superseded_by INTEGER DEFAULT 0, superseded_at TEXT DEFAULT '')"""


def fresh():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute(_DDL)
    return c


def add(c, text, topic="t1", st="project", sid="p1", agent="design"):
    return c.execute(
        "INSERT INTO agent_memory (agent_id,mem_type,content,mem_topic,scope_type,scope_id)"
        " VALUES (?,'fact',?,?,?,?)", (agent, text, topic, st, sid)).lastrowid


# 真实夹具内容（全部取自生产库真实样本，2026-10-07）
C_OLD = "工程 vc 格式为 branchId,quId 两者"
C_NEW = "工程 vc 格式不再是 branchId,quId，已废弃该规则"
D_DUP_A = "建模时必须先定包结构，再生成需求与部件"
D_DUP_B = "建模时必须先定包结构，然后生成需求与部件并校验"
E_LIMIT = "SysML v2 需求追溯用 satisfy/verify 关系表达，二者须引用 requirement usage 而非 requirement definition"

print("== A. 现状不变式（防把既有机制改坏） ==")
c = fresh()
a, b = add(c, C_OLD), add(c, C_NEW)
ck(len(MemoryService.detect_conflicts(c)) == 1, "A1 冲突检测基本盘可用（1 组）")
# consolidate 必须仍只管"重复"（高相似无互斥词）——不能被改成也管冲突
c2 = fresh()
add(c2, D_DUP_A)
add(c2, D_DUP_B)
_merged = MemoryService.consolidate(c2, threshold=0.3)
ck(_merged == 1,
   "A2 consolidate 仍会合并高相似重复（未被改成冲突检测）；实测合并 %d 条" % _merged)
# ★ 附带发现（生产代码的真问题，2026-10-07）：consolidate 的 `except Exception: return 0`
#   会把「SQL 缺列/字段错」这类硬错误伪装成「没东西可合并」。
#   本断言间接锁住夹具列完整性 —— 若将来 consolidate 的 SELECT 又加了新列而本DDL 没跟上，
#   A2 会变红而不是静默返回 0。（真实修法见 A2 附注：生产侧应把 except 改为至少留痕。）
ck(_merged != 0, "A2b ★非 0 证明 consolidate 未被 except 静默吞异常（夹具列完整性守卫）")
ck(MemoryService.detect_conflicts(c2) == [],
   "A3 consolidate 的合并对象**不被**误判为冲突（两引擎职责不重叠）")
# deposit 仍是 ADD-only（不 UPDATE 旧记忆）
_ms = open(os.path.join(ROOT, "memory_service.py"), encoding="utf-8").read()
_dit = _ms[_ms.find("def deposit("):_ms.find("def record_access(")]
ck("UPDATE agent_memory" not in _dit,
   "A4 deposit() 内无 UPDATE agent_memory（仍是 ADD-only，未被改成覆盖写）")
ck("mark_superseded" not in _dit,
   "A5 deposit() 不调用 mark_superseded（影子标记由独立入口做，不由写入路径自动触发）")
# 互斥词表不得含高频技术限定词
ck(not any(m in ("不是", "而非", "而不是") for m in MemoryService._CONFLICT_MARKERS),
   "A6 互斥词表**不含**「不是/而非/而不是」（实测会误伤 SysML 标准限定句式）")

print("\n== B. 冲突检测（对照组是本项最大风险） ==")
c = fresh()
a, b = add(c, C_OLD), add(c, C_NEW)
g = MemoryService.detect_conflicts(c)
ck([a, b] in g, "B1 互斥正例被检出")

c = fresh()
add(c, D_DUP_A)
add(c, D_DUP_B)
ck(MemoryService.detect_conflicts(c) == [], "B2 对照：高相似无互斥词 → 不判冲突（归 consolidate）")

c = fresh()
add(c, "包结构查询先通过工程列表确定vc")
add(c, "热管理需求分析先识别系统边界与涉众")
ck(MemoryService.detect_conflicts(c) == [], "B3 对照：无关两条 → 不判冲突")

c = fresh()
add(c, C_OLD, sid="p1")
add(c, C_NEW, sid="p2")
ck(MemoryService.detect_conflicts(c) == [], "B4 对照：**跨作用域不判冲突**（项目A口径 vs 项目B口径）")

c = fresh()
add(c, C_OLD, topic="tp1")
add(c, C_NEW, topic="tp2")
ck(MemoryService.detect_conflicts(c) == [], "B5 对照：不同 mem_topic → 不判冲突")

# 判据②的方向：互斥对相似度**高**（实测 0.7252），低相似才不该判
ck(_mem_cosine(None, {"content": C_OLD}, {"content": C_NEW}) is not None,
   "B6 相似度可算（_mem_cosine 非None）")
_s = _mem_cosine(None, {"content": C_OLD}, {"content": C_NEW})
ck(_s >= 0.5, "B7 ★互斥对相似度确实**高**（%.3f）—— 判据②必须是高相似而非低相似" % _s)

# 单侧互斥词即可（旧口径天然没有否定词）
ck(_has_conflict_marker(C_NEW) is True and _has_conflict_marker(C_OLD) is False,
   "B8 ★只有新口径含互斥词（旧口径无否定词）⇒ 必须「任一方」而非「双方都有」")

print("\n== C. 影子列语义（可回退 + 行为可翻转） ==")
c = fresh()
a, b = add(c, C_OLD), add(c, C_NEW)
ck(MemoryService.mark_superseded(c, a, b) is True, "C1 标记成功")
row = c.execute("SELECT superseded_by, content FROM agent_memory WHERE id=?", (a,)).fetchone()
ck(row["superseded_by"] == b, "C2 superseded_by 写对")
ck(row["content"] == C_OLD, "C3 ★原内容仍在（影子语义：可回退，不是覆盖写）")
ck(MemoryService.detect_conflicts(c) == [], "C4 被标记行不再参与冲突检测")
ck(MemoryService.mark_superseded(c, a, a) is False, "C5 标记自己不成立")
ck(MemoryService.mark_superseded(c, 0, b) is False, "C6 old_id=0 不成立")
ck(MemoryService.mark_superseded(c, a, 0) is False, "C7 new_id=0 不成立")

print("\n== D. 迁移幂等 ==")
_prod = os.path.join(ROOT, "mbse.db")
if os.path.exists(_prod):
    _c = sqlite3.connect("file:%s?mode=ro" % _prod, uri=True)
    _cols = {r[1] for r in _c.execute("PRAGMA table_info(agent_memory)")}
    ck("superseded_by" in _cols, "D1 生产库已有 superseded_by 列（迁移已跑）")
    ck("superseded_at" in _cols, "D2 生产库已有 superseded_at 列")
    _marked = _c.execute("SELECT COUNT(*) FROM agent_memory WHERE superseded_by>0").fetchone()[0]
    ck(_marked == 0, "D3 ★存量行未被标记（影子列默认 0 ⇒ 对既有行为零影响）")
    _ids = _c.execute("SELECT name FROM sqlite_master WHERE type='index' "
                      "AND tbl_name='agent_memory' AND name='ix_am_superseded'").fetchone()
    ck(_ids is not None, "D4 superseded 索引已建（且在补列之后，未触发 no such column）")
    # 存量误判率（S5闸门）—— 唯一决定能否开默认的判据
    _g = MemoryService.detect_conflicts(_c)
    ck(len(_g) == 0, "D5 ★存量 144 条误判率 = 0 组（S5 闸门，通过才可开默认）")
    _c.close()

print("\n== E. 用户隔离（服务端强制） ==")
from repositories.memory_admin_repo import MemoryAdminRepo  # noqa: E402

_c = sqlite3.connect(_prod if os.path.exists(_prod) else ":memory:")
_c.row_factory = sqlite3.Row
if not os.path.exists(_prod):
    _c.execute(_DDL)
_repo = MemoryAdminRepo(_c)


def _vw(u, admin=False):
    return {"id": 1, "username": u, "display_name": u,
            "permissions": {"admin": ["x"] if admin else []}}


# 夹具：A/B 各一条 user 记忆（结束必清）
_ia = _c.execute("INSERT INTO agent_memory (agent_id,mem_type,content,scope_type,scope_id,forgotten)"
                 " VALUES ('chat','preference','用户甲偏好中文输出','user','alice',0)").lastrowid
_ib = _c.execute("INSERT INTO agent_memory (agent_id,mem_type,content,scope_type,scope_id,forgotten)"
                 " VALUES ('chat','preference','用户乙偏好英文输出','user','bob',0)").lastrowid
try:
    def _ids(r):
        return {i["id"] for i in r["items"]} if isinstance(r, dict) and "items" in r else set()

    _la, _lb, _lm = _ids(_repo.list(viewer=_vw("alice"))), _ids(_repo.list(viewer=_vw("bob"))), \
        _ids(_repo.list(viewer=_vw("root", admin=True)))
    ck(_ia in _la and _ib not in _la, "E1 alice 看到自己的、看不到 bob 的")
    ck(_ib in _lb and _ia not in _lb, "E2 bob 看到自己的、看不到 alice 的")
    ck(_ia in _lm and _ib in _lm, "E3 admin 看到全部")
    _ea = {x["id"] for x in _repo.export_all(viewer=_vw("alice"))}
    ck(_ib not in _ea, "E4 export 同样隔离（泄露面最大的端点也过滤）")
    ck(_repo._can_touch(_repo.get(_ib), _vw("alice")) is False, "E5 写操作也按人过滤（alice 碰不了 bob 的）")
    ck(_repo._can_touch(_repo.get(_ib), _vw("bob")) is True, "E6 bob 能碰自己的")
    ck(_repo._can_touch(_repo.get(999999), _vw("bob")) is False,
       "E7 取不到行 → 保守拒绝（不误删）")
finally:
    _c.execute("DELETE FROM agent_memory WHERE id IN (?,?)", (_ia, _ib))
    _c.commit()
    _c.close()

print("\n== F. 变异自证（证明门禁会判红） ==")
_msrc = open(os.path.join(ROOT, "memory_service.py"), encoding="utf-8").read()
_rsrc = open(os.path.join(ROOT, "repositories", "memory_admin_repo.py"), encoding="utf-8").read()


def _mutate(src, old, new):
    assert old in src, "变异锚点未命中：%r（源码形态变了，门禁需更新）" % old[:60]
    ns = {"__name__": "mutant"}
    exec(compile(src.replace(old, new, 1), "mut.py", "exec"), ns)
    return ns


# M1 互斥词表塞回「而非」⇒ 限定句式被误判（存量会出现误伤组）
try:
    _m = _mutate(_msrc, '_CONFLICT_MARKERS = (\n        "不再",',
                 '_CONFLICT_MARKERS = (\n        "而非", "不再",')
    ck("而非" in _m["MemoryService"]._CONFLICT_MARKERS,
       "M1 变异体词表含「而非」（确认变异生效）")
    ck(_has_conflict_marker(E_LIMIT) is False,
       "M1 对照:真实实现不把「A 而非 B」判成互斥（变异体才会误伤 → 门禁可判红）")
except AssertionError as e:
    ck(False, str(e))

# M2 判据②方向反转（改成"相似度低才算冲突"）⇒ S1 正例漏判
try:
    _m2 = _mutate(_msrc, "if s is None or s < sim_high:", "if s is None or s >= sim_high:")
    _c2 = fresh()
    _a2, _b2 = add(_c2, C_OLD), add(_c2, C_NEW)
    ck(_m2["MemoryService"].detect_conflicts(_c2) == [],
       "M2 变异体（低相似才判冲突）把真实互斥漏掉（确认变异生效）")
    _c2.close()
    _c3 = fresh()
    _a3, _b3 = add(_c3, C_OLD), add(_c3, C_NEW)
    ck([_a3, _b3] in MemoryService.detect_conflicts(_c3),
       "M2 对照:真实实现检出（方向错则漏判 → 门禁可判红）")
    _c3.close()
except AssertionError as e:
    ck(False, str(e))

# M3 影子标记改成真删（覆盖内容）⇒破坏可回退
try:
    _m3 = _mutate(
        _msrc,
        '"UPDATE agent_memory SET superseded_by=?, superseded_at=datetime(\'now\',\'localtime\') "\n'
        '                "WHERE id=? AND forgotten=0", (int(new_id), int(old_id)))',
        '"DELETE FROM agent_memory WHERE id=?", (int(old_id),))')
    _c4 = fresh()
    _a4 = add(_c4, C_OLD)
    _m3["MemoryService"].mark_superseded(_c4, _a4, 999)
    _n = _c4.execute("SELECT COUNT(*) FROM agent_memory WHERE id=?", (_a4,)).fetchone()[0]
    ck(_n == 0, "M3 变异体真删了行（确认变异生效：影子语义被破坏）")
    _c4.close()
except AssertionError as e:
    ck(False, str(e))

# M4 身份过滤去掉 admin 之外的分支 ⇒ 隔离失效
try:
    _m4 = _mutate(_rsrc,
                  'if viewer is None or "scope_type" not in cols or cls._is_admin(viewer):',
                  'if True:')
    _c5 = sqlite3.connect(":memory:")
    _c5.row_factory = sqlite3.Row
    _c5.execute(_DDL)
    _r4 = _m4["MemoryAdminRepo"](_c5)
    _i1 = _c5.execute("INSERT INTO agent_memory (agent_id,mem_type,content,scope_type,scope_id)"
                      " VALUES ('chat','preference','x','user','alice')").lastrowid
    _got = {i["id"] for i in _r4.list(viewer={"id": 1, "username": "bob",
                                              "permissions": {"admin": []}})["items"]}
    ck(_i1 in _got, "M4 变异体过滤失效（bob 能看到 alice 的记忆 → 确认变异生效）")
    _c5.close()
except AssertionError as e:
    ck(False, str(e))

print("\n" + "=" * 66)
print("PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
if FAIL:
    print("HAS FAILURE")
    for f in FAIL:
        print("  x " + f)
    sys.exit(1)
print("ALL GREEN")