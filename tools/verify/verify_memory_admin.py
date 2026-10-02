# -*- coding: utf-8 -*-
"""AI 记忆管理自检（2026-10-02）：agent_memory 的「可见 + 可删」。

覆盖五层，每层都有**独立的变异自证**（变异直接作用于被测模块的**源码文本**再 exec，
不是在测试里重抄一份判据 —— 后者会让"源码被改坏"照样全绿）：

  A 列表与统计（list / stats / 筛选 / 分页）
  B 删除语义（hard_delete 真删 ≠ soft_delete 软删 / restore / purge_forgotten）
  C 老库降级（缺 tier 列 → 按 P1-18 规则**推断展示值**，不写库，零行为漂移）
  D 变异（3 条：硬删退化成软删 / 软删丢幂等条件 / 统计丢 forgotten 过滤）
  E 真实生产库**只读**交叉验证（取数层与直接 SQL 计数一致）

隔离：`MBSE_DB_PATH` 指向临时目录（写入类断言零污染）；生产库仅只读打开。
"""
import io
import json
import os
import re
import sqlite3
import sys
import tempfile
import types

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_TMP = tempfile.mkdtemp(prefix="verify_memory_admin_")
os.environ["MBSE_DB_PATH"] = os.path.join(_TMP, "t.db")
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from database import init_db                                    # noqa: E402
from repositories.memory_admin_repo import MemoryAdminRepo       # noqa: E402

PASS, FAIL = [], []


def chk(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s%s" % ("PASS" if cond else "FAIL", name,
                         ("  <- " + str(detail)) if detail else ""), flush=True)


def _conn():
    c = sqlite3.connect(os.environ["MBSE_DB_PATH"])
    c.row_factory = sqlite3.Row
    return c


# ══════════════════════════════════════════════════════════════════════════════
# 夹具：临时库跑真迁移（不手抄 DDL —— 列由生产代码生成）
# ══════════════════════════════════════════════════════════════════════════════
print("== 0 前置：临时库初始化 ==")
init_db()
_c = _conn()
_cols = {r[1] for r in _c.execute("PRAGMA table_info(agent_memory)")}
chk("0.1 真迁移已补齐关键列（tier/forgotten/activation/access_count）",
    {"tier", "forgotten", "activation", "access_count"} <= _cols, sorted(_cols))

# 造数：6 条覆盖 core/recall/archival × forgotten × mem_type × agent
_SEED = [
    ("a1", "preference", "用户偏好中文回复", "core", 0, 0),
    ("a1", "fact", "电池容量 5000mAh", "recall", 0, 3),
    ("a1", "fact", "支持 6S 电池", "recall", 0, 0),
    ("a2", "experience", "建模先跑覆盖性分析", "recall", 0, 0),
    ("a2", "fact", "端口号 8443", "recall", 1, 0),        # 已软删
    ("a1", "experience", "旧报告正文", "archival", 1, 0),  # 已软删
]
for aid, mt, content, tier, fg, ac in _SEED:
    _c.execute("INSERT INTO agent_memory (agent_id, mem_type, content, tier, forgotten, "
               "access_count, activation) VALUES (?,?,?,?,?,?,1.0)",
               (aid, mt, content, tier, fg, ac))
_c.commit()
_c.close()


def _fresh():
    return _conn()


# ══════════════════════════════════════════════════════════════════════════════
# A 组：列表与统计
# ══════════════════════════════════════════════════════════════════════════════
print("\n== A 列表与统计 ==")
c = _fresh()
r = MemoryAdminRepo(c)
_d = r.list()
chk("A1 默认只列存活记忆（forgotten=0）→ 4 条", _d["total"] == 4, _d["total"])
chk("A2 include_forgotten=1 → 6 条",
    r.list(include_forgotten=1)["total"] == 6)
_st = _d["stats"]
chk("A3 stats.total=6 / alive=4 / forgotten=2",
    (_st["total"], _st["alive"], _st["forgotten"]) == (6, 4, 2), _st)
chk("A4 stats.by_tier 只算存活：core=1 recall=3",
    _st["by_tier"].get("core") == 1 and _st["by_tier"].get("recall") == 3, _st["by_tier"])
chk("A5 stats.by_agent（存活）：a1=3 a2=1",
    _st["by_agent"].get("a1") == 3 and _st["by_agent"].get("a2") == 1, _st["by_agent"])
chk("A6 stats.accessed=1（仅 1 条 access_count>0）", _st["accessed"] == 1, _st["accessed"])
chk("A7 按 agent_id 筛选生效", r.list(agent_id="a2")["total"] == 1)
chk("A8 按 tier 筛选生效", r.list(tier="core")["total"] == 1)
chk("A9 按 mem_type 筛选生效", r.list(mem_type="fact")["total"] == 2)
chk("A10 关键词筛选生效", r.list(q="电池")["total"] == 2,
    r.list(q="电池")["total"])
_pg = r.list(limit=2, offset=0)
chk("A11 分页 limit 生效（items 2 条但 total 仍是 4）",
    len(_pg["items"]) == 2 and _pg["total"] == 4, (len(_pg["items"]), _pg["total"]))
chk("A12 缺 tier 列时用推断值填 tier/tier_label（不写库）",
    all("tier" in i and "tier_label" in i for i in _pg["items"]))
# 排序：偏好类优先
_first_types = [i["mem_type"] for i in r.list()["items"]]
chk("A13 排序把 preference 放最前", _first_types[0] == "preference", _first_types)
c.close()


# ══════════════════════════════════════════════════════════════════════════════
# B 组：删除语义
# ══════════════════════════════════════════════════════════════════════════════
print("\n== B 删除语义（真删 vs 软删，刻意区分）==")
c = _fresh()
r = MemoryAdminRepo(c)
_mid = c.execute("SELECT id FROM agent_memory WHERE content LIKE '%5000mAh%'").fetchone()[0]
chk("B1 hard_delete 前该行存在", r.get(_mid) is not None)
chk("B2 hard_delete 返回 True", r.hard_delete(_mid) is True)
chk("B3 hard_delete 后**真的没了**（get None 且 SQL 也查不到）",
    r.get(_mid) is None and c.execute("SELECT COUNT(*) FROM agent_memory WHERE id=?",
                                      (_mid,)).fetchone()[0] == 0)
chk("B4 hard_delete 不存在的 id → False", r.hard_delete(999999) is False)

_sid = c.execute("SELECT id FROM agent_memory WHERE forgotten=0 LIMIT 1").fetchone()[0]
chk("B5 soft_delete 的目标行原为存活（确认软删走的是有效路径）",
    c.execute("SELECT forgotten FROM agent_memory WHERE id=?", (_sid,)).fetchone()[0] == 0)
chk("B6 soft_delete 存活行 → True", r.soft_delete(_sid) is True)
chk("B7 软删后默认列表不含它、但行仍在（forgotten=1）",
    r.get(_sid) is not None and r.get(_sid)["forgotten"] == 1
    and all(i["id"] != _sid for i in r.list()["items"]))
chk("B8 对已软删的行再软删 → False（幂等，不重复计数）", r.soft_delete(_sid) is False)
chk("B9 restore 恢复 → True 且 forgotten 回 0", r.restore(_sid) is True
    and r.get(_sid)["forgotten"] == 0)
chk("B10 对未软删的行 restore → False", r.restore(_sid) is False)

_before_alive = r.stats()["alive"]
_fg_n = r.stats()["forgotten"]
_purged = r.hard_delete_forgotten()
chk("B11 purge_forgotten 只删已软删行，条数正确", _purged == _fg_n, (_purged, _fg_n))
chk("B12 purge 后存活行一条不少", r.stats()["alive"] == _before_alive)
chk("B13 purge 后 forgotten 归零", r.stats()["forgotten"] == 0)
c.close()


# ══════════════════════════════════════════════════════════════════════════════
# C 组：老库降级（缺 tier 列）
# ══════════════════════════════════════════════════════════════════════════════
print("\n== C 老库降级（缺 tier 列）==")
# 从生产 schema.py **抽原文 DDL**（不是手抄）建最小基表 —— 老库形态（无 tier 列）
_schema_src = io.open(os.path.join(ROOT, "database/schema.py"), encoding="utf-8").read()
_m = re.search(r"(CREATE TABLE IF NOT EXISTS agent_memory \(.*?\))", _schema_src, re.S)
chk("C1 能从 schema.py 抽到 agent_memory 的原始 DDL", bool(_m))
_c2path = os.path.join(_TMP, "old.db")
c2 = sqlite3.connect(_c2path)
c2.row_factory = sqlite3.Row
c2.execute(_m.group(1))
c2.execute("ALTER TABLE agent_memory ADD COLUMN forgotten INTEGER DEFAULT 0")
c2.execute("ALTER TABLE agent_memory ADD COLUMN access_count INTEGER DEFAULT 0")
c2.execute("ALTER TABLE agent_memory ADD COLUMN activation REAL DEFAULT 1.0")
c2.executemany("INSERT INTO agent_memory (agent_id, mem_type, content, forgotten) VALUES (?,?,?,?)",
               [("a1", "preference", "偏好中文", 0), ("a1", "fact", "事实甲", 0),
                ("a1", "fact", "旧事乙", 1)])
c2.commit()
chk("C2 裸库确实**没有** tier 列（确认测的是降级路径）",
    "tier" not in {x[1] for x in c2.execute("PRAGMA table_info(agent_memory)")})
r2 = MemoryAdminRepo(c2)
_d2 = r2.list()
_byid = {i["content"]: i for i in _d2["items"]}
chk("C3 缺列时按规则推断：preference→core",
    _byid["偏好中文"]["tier"] == "core", _byid["偏好中文"]["tier"])
chk("C4 缺列时按规则推断：普通事实→recall",
    _byid["事实甲"]["tier"] == "recall", _byid["事实甲"]["tier"])
_st2 = r2.stats()
chk("C5 缺列时 stats.by_tier 仍非空（SQL 推断，不返回空分布）",
    bool(_st2["by_tier"]) and _st2["by_tier"].get("core") == 1, _st2["by_tier"])
chk("C6 降级路径不写库（表结构未变）",
    "tier" not in {x[1] for x in c2.execute("PRAGMA table_info(agent_memory)")})
c2.close()


# ══════════════════════════════════════════════════════════════════════════════
# D 组：变异（作用于源码文本再 exec）
# ══════════════════════════════════════════════════════════════════════════════
print("\n== D 变异自证 ==")
# 前置：显式造一条 forgotten=1（M2 需要它；**不隐式依赖 M1 的执行** —— 否则 M1 一变，M2 就静默失效）
_c0 = _fresh()
_c0.execute("UPDATE agent_memory SET forgotten=1 WHERE id=(SELECT MIN(id) FROM agent_memory "
            "WHERE forgotten=0)")
_c0.commit()
_c0.close()

_REPO_PATH = os.path.join(ROOT, "repositories/memory_admin_repo.py")
_SRC = io.open(_REPO_PATH, encoding="utf-8").read()


def _variant(pairs):
    src = _SRC
    for a, b in pairs:
        if a not in src:
            return None
        src = src.replace(a, b)
    mod = types.ModuleType("mem_repo_mut")
    mod.__dict__["__file__"] = _REPO_PATH
    exec(compile(src, "<mut>", "exec"), mod.__dict__)
    return mod


# M1：硬删退化成软删（这正是"被遗忘权"最危险的退化 —— 界面说删了，数据还在）
_M1 = _variant([("cur = self.conn.execute(\"DELETE FROM agent_memory WHERE id=?\", (mid,))",
                 "cur = self.conn.execute(\"UPDATE agent_memory SET forgotten=1 WHERE id=?\", (mid,))")])
if _M1:
    c3 = _fresh()
    r3 = _M1.MemoryAdminRepo(c3)
    _id3 = c3.execute("SELECT id FROM agent_memory WHERE forgotten=0 LIMIT 1").fetchone()[0]
    _ok3 = r3.hard_delete(_id3)
    _still = c3.execute("SELECT COUNT(*) FROM agent_memory WHERE id=?", (_id3,)).fetchone()[0]
    chk("M1 硬删退化成软删 → B3「行真的没了」被打破（行仍在库）",
        _ok3 is True and _still == 1, "rowcount=%s 残留=%s" % (_ok3, _still))
    c3.close()
else:
    chk("M1 跳过（锚点失效）", False, "锚点未命中 → 变异失效，必须修")

# M2：软删丢掉幂等条件 `AND forgotten=0`
_M2 = _variant([("SET forgotten=1 WHERE id=? AND forgotten=0", "SET forgotten=1 WHERE id=?")])
if _M2:
    c4 = _fresh()
    r4 = _M2.MemoryAdminRepo(c4)
    _id4 = c4.execute("SELECT id FROM agent_memory WHERE forgotten=1 LIMIT 1").fetchone()[0]
    _r2 = r4.soft_delete(_id4)          # 只调一次（写进 detail 里的第二次调用会改变状态）
    chk("M2 软删丢幂等条件 → B8「对已软删行再软删返回 False」被打破",
        _r2 is True, "mutated=%s" % _r2)
    c4.close()
else:
    chk("M2 跳过（锚点失效）", False, "锚点未命中 → 变异失效，必须修")

# M3：统计的存活过滤被删（alive 会把已归档也算进去）
_M3 = _variant([("alive = one(\"SELECT COUNT(*) FROM agent_memory WHERE forgotten=0\") if has_fg else total",
                 "alive = total")])
if _M3:
    c5 = _fresh()
    _st3 = _M3.MemoryAdminRepo(c5).stats()
    _st4 = MemoryAdminRepo(c5).stats()
    chk("M3 统计丢 forgotten 过滤 → A3「alive=4」被打破（等于 total）",
        _st3["alive"] != _st4["alive"] and _st3["alive"] == _st3["total"],
        "mut_total=%s mut_alive=%s real_alive=%s" % (_st3["total"], _st3["alive"], _st4["alive"]))
    c5.close()
else:
    chk("M3 跳过（锚点失效）", False, "锚点未命中 → 变异失效，必须修")


# ══════════════════════════════════════════════════════════════════════════════
# E 组：真实生产库只读交叉验证
# ══════════════════════════════════════════════════════════════════════════════
print("\n== E 真实生产库只读交叉验证 ==")
_PROD = os.path.join(ROOT, "mbse.db")
if os.path.exists(_PROD):
    try:
        pc = sqlite3.connect("file:%s?mode=ro" % _PROD.replace("\\", "/"), uri=True)
        pc.row_factory = sqlite3.Row
        _sql_alive = pc.execute("SELECT COUNT(*) FROM agent_memory WHERE forgotten=0").fetchone()[0]
        _sql_total = pc.execute("SELECT COUNT(*) FROM agent_memory").fetchone()[0]
        _rp = MemoryAdminRepo(pc).list(limit=1000)
        chk("E1 list.total 与直接 SQL 计数一致（存活）", _rp["total"] == _sql_alive,
            (_rp["total"], _sql_alive))
        _stp = MemoryAdminRepo(pc).stats()
        chk("E2 stats.total/alive 与直接 SQL 一致",
            (_stp["total"], _stp["alive"]) == (_sql_total, _sql_alive),
            (_stp["total"], _stp["alive"], _sql_total, _sql_alive))
        # 交叉：by_tier 之和应等于 alive（tier 列存在的前提下）
        _sum_tier = sum((_stp["by_tier"] or {}).values())
        chk("E3 stats.by_tier 之和 == alive（分布无遗漏/不重复）", _sum_tier == _stp["alive"],
            (_sum_tier, _stp["alive"]))
        chk("E4 生产库 tier 列存在（P1-18 分层已落地）",
            "tier" in {r[1] for r in pc.execute("PRAGMA table_info(agent_memory)")})
        pc.close()
    except Exception as e:
        chk("E 生产库交叉验证执行失败", False, str(e))
else:
    print("  (跳过：无生产库)")


# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print("PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("  - " + f)
sys.exit(1 if FAIL else 0)
