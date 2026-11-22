# -*- coding: utf-8 -*-
"""P0-1 G1 门禁：对象类型 ↔ Action 绑定表的结构、引用与规则合法性。

判据（对应方案 §5 G1）：
  [1] 表结构 8 列 + 复合主键 (type_id, tool_name)
  [2] 每个 type_id 命中 ontology_types.id（软引用一致性）
  [3] 每个 tool_name 命中 tools.name（软引用一致性）
  [4] 无重复主键（DB 约束 + 实查）
  [5] 每条 pre/post_condition 能通过 core.action_conditions 规则表校验
      （非法 kind / 方向错配 / 缺字段即红）
  [6] rule_version 口径与 core.action_conditions.RULE_VERSION 对齐（可空=历史行）
  [7] enabled 只有 0/1
  [8] 变异自证：注入一条坏引用 + 一条非法规则 ⇒ [2][5] 必须变红

真实库只读扫描（不写入生产库）；变异在内存里对查询结果做等价模拟，证明判据非空转。
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

import sqlite3

from core.action_conditions import RULE_VERSION, validate as _validate

_FAILS = []
# 生产库只读
_DB = os.environ.get("MBSE_DB_PATH") or os.path.join(_ROOT, "mbse.db")


def check(name, cond, detail=""):
    tag = "OK  " if cond else "FAIL"
    if not cond:
        _FAILS.append(name)
    print(f"[{tag}] {name}" + (f"  — {detail}" if detail else ""))


con = sqlite3.connect("file:%s?mode=ro" % _DB.replace("\\", "/"), uri=True)
con.row_factory = sqlite3.Row

# 缺表 = 尚未跑迁移 ⇒ 明确指引，不崩溃（不静默当通过）
_exists = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                      "AND name='ontology_type_actions'").fetchone()
if not _exists:
    print("NO_TABLE 库 %s 未建立 ontology_type_actions；请先启动服务触发 init_db 迁移" % _DB)
    con.close()
    sys.exit(2)

# [1] 表结构
cols = [(r[1], r[2], r[5]) for r in con.execute("PRAGMA table_info(ontology_type_actions)")]
check("[1] 表存在且 8 列", len(cols) == 8,
      "cols=%s" % [c[0] for c in cols])
want_cols = {"type_id", "tool_name", "pre_condition", "post_condition",
             "enabled", "note", "rule_version", "created_at"}
check("[1] 列名集合正确", {c[0] for c in cols} == want_cols)
pk = tuple(c[0] for c in sorted(cols, key=lambda x: x[2]) if c[2])
check("[1] 复合主键 (type_id, tool_name)", pk == ("type_id", "tool_name"), "pk=%s" % (pk,))

rows = [dict(r) for r in con.execute("SELECT * FROM ontology_type_actions")]
type_ids = {r[0] for r in con.execute("SELECT id FROM ontology_types")}
tool_names = {r[0] for r in con.execute("SELECT name FROM tools")}

# [2][3] 软引用一致性
bad_type = [r for r in rows if r["type_id"] not in type_ids]
bad_tool = [r for r in rows if r["tool_name"] not in tool_names]
check("[2] type_id 全部命中 ontology_types", not bad_type,
      "bad=%s" % [r["type_id"] for r in bad_type][:5])
check("[3] tool_name 全部命中 tools", not bad_tool,
      "bad=%s" % [r["tool_name"] for r in bad_tool][:5])

# [4] 无重复主键
keys = [(r["type_id"], r["tool_name"]) for r in rows]
check("[4] 无重复主键", len(keys) == len(set(keys)))

# [5] pre/post 规则合法（方向匹配）
bad_rules = []
for r in rows:
    for col, direction in (("pre_condition", "pre"), ("post_condition", "post")):
        raw = r.get(col)
        if not raw:
            continue
        problems = _validate(raw, direction)
        if problems:
            bad_rules.append("%s/%s: %s" % (r["tool_name"], col, "; ".join(problems)))
check("[5] pre/post_condition 规则合法", not bad_rules,
      "bad=%s" % bad_rules[:3])

# [6] rule_version 口径（空=历史行，允许）
bad_ver = [r["tool_name"] for r in rows
           if r.get("rule_version") and r["rule_version"] != RULE_VERSION]
check("[6] rule_version 与单一真源对齐", not bad_ver,
      "want=%s bad=%s" % (RULE_VERSION, bad_ver[:5]))

# [7] enabled 只有 0/1
bad_en = [r["tool_name"] for r in rows if r.get("enabled") not in (0, 1)]
check("[7] enabled 取值合法", not bad_en, "bad=%s" % bad_en[:5])

# [8] 变异自证：对 rows 注入坏引用 + 非法规则，判据必须能判红
mut_bad_type = rows + [{"type_id": -999, "tool_name": "x"}]
mut_detected_type = any(r["type_id"] not in type_ids for r in mut_bad_type)
check("M8 变异：坏 type_id 能被 [2] 判红", mut_detected_type)
mut_bad_rule = _validate('{"kind":"no_such_kind"}', "post")
check("M8 变异：非法 kind 能被 [5] 判红", bool(mut_bad_rule))

con.close()
print("\n结论：%s（FAIL %d，检查 %d 条绑定）" %
      ("全绿" if not _FAILS else "有红", len(_FAILS), len(rows)))
sys.exit(1 if _FAILS else 0)
