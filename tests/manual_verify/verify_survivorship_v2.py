# -*- coding: utf-8 -*-
"""S1/S2 验证：谓词/属性归一断言 + survivorship 挂靠端到端（内存库）。

运行：python tests/manual_verify/verify_survivorship_v2.py
"""
import json
import sqlite3
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

PASS, FAIL = [], []
def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"  [{detail}]" if detail else ""))
    (PASS if cond else FAIL).append(name)

# ═══════════ 1. 纯函数断言：normalize_predicate ═══════════
from text_normalize import normalize_predicate, normalize_prop_key

check("包含→CONTAINS", normalize_predicate("包含") == "CONTAINS")
check("contains→CONTAINS", normalize_predicate("Contains") == "CONTAINS")
check("满足→SATISFIES", normalize_predicate("满足") == "SATISFIES")
check("satisfy→SATISFIES", normalize_predicate("satisfy") == "SATISFIES")
check("连接→CONNECTS", normalize_predicate("连 接") == "CONNECTS")   # 带空格折叠
check("CONTAINS 幂等", normalize_predicate("CONTAINS") == "CONTAINS")
check("未知谓词原样返回", normalize_predicate("MAGIC_REL") == "MAGIC_REL")
check("空串安全", normalize_predicate("") == "")
check("extra_lookup 动态扩展",
      normalize_predicate("超级关系", {"超级关系": "SUPER"}) == "SUPER")

# ═══════════ 2. 纯函数断言：normalize_prop_key ═══════════
check("band→频段", normalize_prop_key("band") == "频段")
check("throughput→数据速率", normalize_prop_key("throughput") == "数据速率")
check("desc→描述", normalize_prop_key("desc") == "描述")
check("热控方案幂等", normalize_prop_key("热控方案") == "热控方案")
check("未知 key 原样返回", normalize_prop_key("custom_x") == "custom_x")

# ═══════════ 3. survivorship 端到端（内存库最小 schema） ═══════════
from vector2graph import _survivorship_apply

con = sqlite3.connect(":memory:")
con.row_factory = sqlite3.Row
con.executescript("""
CREATE TABLE entities (
    id TEXT PRIMARY KEY, name TEXT, entity_type TEXT,
    properties TEXT, status TEXT DEFAULT 'active', source_doc TEXT
);
CREATE TABLE graph_edit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, op TEXT, node_id TEXT,
    payload TEXT, operator TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE v2g_candidates (
    id INTEGER PRIMARY KEY, entity_name TEXT, entity_type TEXT,
    properties TEXT, source_doc TEXT
);
INSERT INTO entities (id, name, entity_type, properties, status, source_doc) VALUES (
    'ENT-1', '电源分系统', '部件',
    '{"电压":"28V","aliases":["power subsystem"]}', 'active', 'doc_a.docx');
INSERT INTO v2g_candidates VALUES (9, '电源', '部件',
    '{"band":"Ku","desc":"为整星供电的子系统"}', '广汽方法论.docx');
""")
con.commit()

cand = con.execute("SELECT * FROM v2g_candidates WHERE id=9").fetchone()
res = _survivorship_apply(con, "ENT-1", cand, operator="王工")

row = con.execute("SELECT name, properties, source_doc FROM entities WHERE id='ENT-1'").fetchone()
props = json.loads(row["properties"])
check("属性补齐（band→频段）", props.get("频段") == "Ku", f"频段={props.get('频段')}")
check("属性补齐（desc→描述）", props.get("描述") == "为整星供电的子系统")
check("冲突值不覆盖（保留方优先）", props.get("电压") == "28V" if "电压" in props else "电压" not in cand[3])
check("别名并入（电源）", "电源" in (props.get("aliases") or []))
check("返回值 props_merged 正确", set(res["props_merged"]) == {"频段", "描述"}, str(res["props_merged"]))
check("返回值 alias_added", res["alias_added"] is True)
log = con.execute(
    "SELECT op, node_id, payload FROM graph_edit_logs ORDER BY id DESC LIMIT 1").fetchone()
pl = json.loads(log["payload"])
check("lineage 审计落库", log["op"] == "survivorship_align" and pl.get("candidate") == "电源"
      and set(pl.get("props_merged") or []) == {"频段", "描述"})
# 内部字段不参与择优
con.execute("UPDATE v2g_candidates SET properties='{\"matched_word\":\"x\",\"source_frag\":\"y\"}' WHERE id=9")
res2 = _survivorship_apply(con, "ENT-1", con.execute("SELECT * FROM v2g_candidates WHERE id=9").fetchone(), "王工")
check("内部元数据不参与择优", res2["props_merged"] == [])

print(f"\n{'ALL PASS (' + str(len(PASS)) + ')' if not FAIL else 'FAILED: ' + str(FAIL)}")
sys.exit(0 if not FAIL else 1)
