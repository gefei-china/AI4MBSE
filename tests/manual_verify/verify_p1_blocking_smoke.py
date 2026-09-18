# -*- coding: utf-8 -*-
"""P1-2 Blocking 化冒烟测试（Task #16）。

覆盖：
- SINGLE_OK            单候选消歧：精确/归一全等 dup_high、跨前缀重叠 dup_suspect（bigram 索引）、
                       包含 dup_suspect、空串/无匹配 none、deprecated 过滤
- BATCH_OK             批量 _disambiguate_many 与单次 _disambiguate 结果一致性（key 一律 strip 后取）
- STRONG_RULE_MERGE_OK detect_and_save_candidates 强规则合并（normalize_full 全等+同类型 → auto-merge）
- BUCKET_LIMIT_OK      桶上限 200：超限成员记 buckets_dropped，不 O(n²) 爆炸
- CONFLICT_TRUNCATE_OK conflict_detect 同归一名大组 >200 截断（skipped 计数）
- NO_CYCLE_OK          导入链单向：text_normalize ← entity_resolver ← vector2graph（源码级检查）

运行：python tests/manual_verify/verify_p1_blocking_smoke.py
"""
import os
import re
import sys
import tempfile

# ── 必须在任何业务模块 import 之前指定临时库 ──
_TMP_DB = os.path.join(tempfile.gettempdir(), "mbse_p1_blocking_smoke.db")
for _p in (_TMP_DB, _TMP_DB + "-wal", _TMP_DB + "-shm"):
    if os.path.exists(_p):
        os.remove(_p)
os.environ["MBSE_DB_PATH"] = _TMP_DB

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)

from database.schema import get_db, init_db  # noqa: E402
from entity_resolver import conflict_detect, detect_and_save_candidates  # noqa: E402
from vector2graph import _disambiguate, _disambiguate_many  # noqa: E402

init_db()
conn = get_db()

PASS = []
FAIL = []


def check(label: str, cond: bool, detail: str = ""):
    if cond:
        PASS.append(label)
        print(f"  [PASS] {label}")
    else:
        FAIL.append(label)
        print(f"  [FAIL] {label}  {detail}")


def wipe():
    """外键安全清空：先删子表再删 entities。"""
    for t in ("knowledge_conflicts", "entity_dup_candidates", "entity_merges",
              "v2g_candidates", "relations", "entities"):
        conn.execute(f"DELETE FROM {t}")
    conn.commit()


def insert(eid: str, name: str, etype: str, props: str = "{}",
           status: str = "reviewed", branch: str = "dev"):
    conn.execute(
        "INSERT INTO entities (id, name, entity_type, properties, status, branch) "
        "VALUES (?,?,?,?,?,?)", (eid, name, etype, props, status, branch))


# ═══════════════════════════ 场景 A：单候选消歧 ═══════════════════════════
print("\n== A. SINGLE_OK：单候选消歧 ==")
wipe()
insert("e1", "转发器", "system")
insert("e2", "转发器 ", "system")          # 尾空格 → 归一全等
insert("e3", "高轨卫星系统", "system")      # 跨前缀重叠基准
insert("e4", "卫星转发器", "component")     # 包含基准（已存在）
insert("e5", "高通量卫星", "system")
insert("e6", "高通量卫星", "component")     # 同归一名、不同类型
insert("e7", "已废弃实体", "system", status="deprecated")
conn.commit()

check("A1 精确全等 → dup_high",
      _disambiguate(conn, "转发器") == ("dup_high", "e1"),
      str(_disambiguate(conn, "转发器")))
check("A2 带空白归一全等 → dup_high(e1)",
      _disambiguate(conn, " 转发器 ") == ("dup_high", "e1"),
      str(_disambiguate(conn, " 转发器 ")))
check("A3 同名自身 → dup_high",
      _disambiguate(conn, "高轨卫星系统") == ("dup_high", "e3"),
      str(_disambiguate(conn, "高轨卫星系统")))
check("A4 跨前缀重叠(高轨/低轨, 重叠83%) → dup_suspect(bigram 索引)",
      _disambiguate(conn, "低轨卫星系统") == ("dup_suspect", "e3"),
      str(_disambiguate(conn, "低轨卫星系统")))
check("A5 包含(星载转发器⊃转发器) → dup_suspect(e1 先见)",
      _disambiguate(conn, "星载转发器") == ("dup_suspect", "e1"),
      str(_disambiguate(conn, "星载转发器")))
check("A6 空串 → none",
      _disambiguate(conn, "") == ("none", ""),
      str(_disambiguate(conn, "")))
check("A7 deprecated 实体被过滤 → none",
      _disambiguate(conn, "已废弃实体") == ("none", ""),
      str(_disambiguate(conn, "已废弃实体")))
check("A8 完全无关 → none",
      _disambiguate(conn, "完全无关实体XYZ") == ("none", ""),
      str(_disambiguate(conn, "完全无关实体XYZ")))

# ═══════════════════════════ 场景 B：批量 vs 单次一致性 ═══════════════════════════
print("\n== B. BATCH_OK：批量消歧与单次一致 ==")
names = ["转发器", " 转发器 ", "低轨卫星系统", "星载转发器",
         "已废弃实体", "完全无关实体XYZ", ""]
many = _disambiguate_many(conn, names)
b_ok = True
b_detail = ""
for n in names:
    exp = _disambiguate(conn, n)
    got = many.get(str(n).strip(), ("none", ""))  # key 一律 strip 后取
    if got != exp:
        b_ok = False
        b_detail += f"name={n!r} many={got!r} single={exp!r}; "
check("B1 批量 == 逐个单次（含空串/带空白）", b_ok, b_detail)
check("B2 many key 为 strip 后值",
      many.get("转发器") == ("dup_high", "e1"),
      str(many.get("转发器")))
check("B3 批量覆盖 dup_suspect",
      many.get("低轨卫星系统") == ("dup_suspect", "e3"),
      str(many.get("低轨卫星系统")))

# ═══════════════════════════ 场景 C：强规则合并 ═══════════════════════════
print("\n== C. STRONG_RULE_MERGE_OK：detect_and_save_candidates ==")
# 复用 A 场景数据：e2('转发器 ') 与 e1('转发器') normalize_full 全等+同 type → 强规则合并
insert("e8", "Ka频段通信载荷", "system")
insert("e9", "Ka 频段 通信载荷", "system")   # 空格差异 → normalize_full 全等
conn.commit()
ret = detect_and_save_candidates(conn)
check("C1 auto_merged ≥ 2 (e2→e1, e9→e8)",
      ret.get("auto_merged", 0) >= 2, str(ret))
st = {r["id"]: r["status"] for r in conn.execute(
    "SELECT id, status FROM entities").fetchall()}
check("C2 e2 已被合并为 deprecated", st.get("e2") == "deprecated", str(st.get("e2")))
check("C3 e9 已被合并为 deprecated", st.get("e9") == "deprecated", str(st.get("e9")))
check("C4 keep 实体保留",
      st.get("e1") == "reviewed" and st.get("e8") == "reviewed",
      str({k: st.get(k) for k in ("e1", "e8")}))
check("C5 返回结构含 buckets_dropped 键", "buckets_dropped" in ret, str(ret))

# ═══════════════════════════ 场景 D：桶上限 ═══════════════════════════
print("\n== D. BUCKET_LIMIT_OK：桶上限 200 ==")
wipe()
for i in range(1, 211):                     # 210 个同名同类型 → 同一 canopy 桶
    insert(f"p{i:03d}", "平台实体A", "sat_platform")
conn.commit()
ret = detect_and_save_candidates(conn)
check("D1 buckets_dropped == 10（210-200 超限）",
      ret.get("buckets_dropped", 0) == 10, str(ret))
check("D2 强规则合并 209 个同名 dup",
      ret.get("auto_merged", 0) == 209, str(ret))

# ═══════════════════════════ 场景 E：conflict_detect 大组截断 ═══════════════════════════
print("\n== E. CONFLICT_TRUNCATE_OK：大组 >200 截断 ==")
wipe()
for i in range(1, 211):
    insert(f"c{i:03d}", "C实体", "conflict_type",
           props=f'{{"val":"v{i}"}}')
conn.commit()
ret = conflict_detect(conn)
check("E1 skipped == 10（210-200 截断）",
      ret.get("skipped", 0) == 10, str(ret))
check("E2 detected > 0（前 200 对属性互异）",
      ret.get("detected", 0) > 0, str(ret))

# ═══════════════════════════ 场景 F：导入链单向 ═══════════════════════════
print("\n== F. NO_CYCLE_OK：导入链单向 ==")
src_dir = _BASE  # mbse_system 根目录（text_normalize.py 所在）
tn = open(os.path.join(src_dir, "text_normalize.py"), encoding="utf-8").read()
er = open(os.path.join(src_dir, "entity_resolver.py"), encoding="utf-8").read()
v2g = open(os.path.join(src_dir, "vector2graph.py"), encoding="utf-8").read()
check("F1 text_normalize 不依赖 entity_resolver/vector2graph",
      not re.search(r"(?:from|import)\s+(entity_resolver|vector2graph)", tn))
check("F2 entity_resolver 不再反向导入 vector2graph",
      not re.search(r"(?:from\s+vector2graph\s+import|import\s+vector2graph)", er))
check("F3 entity_resolver → text_normalize 单向",
      bool(re.search(r"from\s+text_normalize\s+import", er)))
check("F4 vector2graph → entity_resolver（仅 re-export _normalize_mentions）",
      bool(re.search(r"from\s+entity_resolver\s+import\s+_normalize_mentions", v2g)))
check("F5 vector2graph → text_normalize 单向",
      bool(re.search(r"from\s+text_normalize\s+import", v2g)))
check("F6 entity_resolver 有 _normalize_mentions 实现（尾部）",
      "def _normalize_mentions" in er)

# ═══════════════════════════ 汇总 ═══════════════════════════
print("\n" + "=" * 56)
print(f"PASS {len(PASS)} / {len(PASS) + len(FAIL)}")
if FAIL:
    print("FAILED:", FAIL)
    sys.exit(1)
print("ALL GREEN ✅")
sys.exit(0)
