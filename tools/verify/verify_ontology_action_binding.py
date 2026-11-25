# -*- coding: utf-8 -*-
"""本体驱动动作的机制验证（2026-11-24）。

结论要回答的问题：本体能否「执行/约束动作」？
  · 机制：`ontology_type_actions` 绑定 + `action_conditions` 纯函数 + `_exec_tool_call` 挂 check_pre/check_post
  · 数据：绑定表 0 行 ⇒ 当前零约束

本脚本在**真库副本**上插入一条绑定，证明「一旦有绑定，前置条件真的会拦截」。
不写生产库。

判据：
  M1 无绑定时 check_pre 返回 None（无约束，零回归）
  M2 绑定 field_not_empty 后，空参数 ⇒ failed（会被 _exec_tool_call 拒绝）
  M3 同一绑定，非空参数 ⇒ verified（放行）
  M4 post 条件 entity_exists：实体不存在 ⇒ failed
  M5 post 条件 entity_exists：实体存在 ⇒ verified
  M6 非法规则体被 validate 拒绝（不能配就生效，防脏规则）
  M7 生产库绑定数仍为 0（未污染）
"""
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  OK  " if cond else " FAIL ") + name + (f"  {detail}" if detail else ""))


def main():
    src_db = ROOT / "mbse.db"
    if not src_db.exists():
        print("SKIP: 真库不存在")
        return 0

    from core import action_conditions as ac
    from core import action_binding_runtime as abr

    # 生产库基线
    prod = sqlite3.connect(str(src_db))
    prod_base = prod.execute("SELECT COUNT(*) FROM ontology_type_actions").fetchone()[0]
    prod.close()

    tmp = Path(tempfile.gettempdir()) / ("mbse_action_verify_%d.db" % os.getpid())
    if tmp.exists():
        tmp.unlink()
    shutil.copyfile(str(src_db), str(tmp))
    c = sqlite3.connect(str(tmp))
    c.row_factory = sqlite3.Row

    try:
        # 选一个真实类型与 write 工具
        t = c.execute("SELECT id,name FROM ontology_types WHERE type_kind='entity' "
                      "AND status!='deprecated' LIMIT 1").fetchone()
        tool = c.execute("SELECT name FROM tools WHERE COALESCE(side_effect,'read')='write' LIMIT 1").fetchone()
        if not t or not tool:
            print("SKIP: 缺类型或 write 工具")
            return 0
        TID, TNAME, TOOL = t["id"], t["name"], tool["name"]
        print(f"  夹具：类型「{TNAME}」(id={TID}) + 工具「{TOOL}」")

        print("\n[1] 无绑定 = 无约束（零回归）")
        r0 = abr.check_pre(TOOL, {"x": ""}, conn=c)
        check("M1 无绑定返回 None", r0 is None, f"result={r0}")

        # 插入 pre 绑定：field_not_empty(arg=x)
        pre = {"kind": "field_not_empty", "arg": "x"}  # 键名是 kind，不是 rule
        c.execute("INSERT INTO ontology_type_actions (type_id, tool_name, pre_condition, "
                  "post_condition, enabled, note) VALUES (?,?,?,?,1,?)",
                  (TID, TOOL, __import__("json").dumps(pre), "", "verify-20261124"))
        c.commit()

        print("\n[2] pre 条件生效")
        r_empty = abr.check_pre(TOOL, {"x": ""}, conn=c)
        check("M2 空参数 ⇒ failed（应被拒绝执行）",
              bool(r_empty) and r_empty.get("status") == "failed",
              f"status={r_empty.get('status') if r_empty else None} reason={str((r_empty or {}).get('reason'))[:60]}")
        r_ok = abr.check_pre(TOOL, {"x": "有值"}, conn=c)
        check("M3 非空参数 ⇒ verified（放行）",
              bool(r_ok) and r_ok.get("status") == "verified",
              f"status={r_ok.get('status') if r_ok else None}")

        print("\n[3] post 回读条件生效")
        c.execute("UPDATE ontology_type_actions SET pre_condition='', post_condition=?, "
                  "note='verify-post' WHERE type_id=? AND tool_name=?",
                  (__import__("json").dumps({"kind": "entity_exists", "arg": "id"}), TID, TOOL))
        c.commit()
        r_miss = abr.check_post(TOOL, {"id": "__NO_SUCH_ENTITY__"}, {}, conn=c)
        check("M4 post: 实体不存在 ⇒ failed",
              bool(r_miss) and r_miss.get("status") == "failed",
              f"status={r_miss.get('status') if r_miss else None}")
        real = c.execute("SELECT id FROM entities WHERE status!='deprecated' LIMIT 1").fetchone()
        r_hit = abr.check_post(TOOL, {"id": real["id"]}, {}, conn=c)
        check("M5 post: 实体存在 ⇒ verified",
              bool(r_hit) and r_hit.get("status") == "verified",
              f"status={r_hit.get('status') if r_hit else None} id={real['id']}")

        print("\n[4] 脏规则被拒（不能配就生效）")
        bad = ac.validate({"kind": "entity_type_in", "arg": "x"})  # 有 kind、缺 types
        check("M6a 缺必填字段的规则被拒",
              any("缺字段 types" in e or "types" in e for e in bad), f"errors={bad[:2]}")
        bad2 = ac.validate({"rule": "entity_exists", "arg": "id"})  # 用错键名
        check("M6b 用错键名(rule 而非 kind)必须被拒",
              any("缺 kind" in e for e in bad2), f"errors={bad2[:2]}")
        bad3 = ac.validate({"kind": "no_such_rule"}, direction="pre")
        check("M6c 未登记规则被拒", any("unknown_kind" in e for e in bad3), f"errors={bad3[:1]}")

        print("\n[5] 执行链接线")
        src = Path("agent/pipeline_parts/tools.py").read_text(encoding="utf-8")
        check("M7a check_pre 挂在执行出口", "action_binding_runtime as _abr" in src and "_abr.check_pre(name, arguments)" in src)
        check("M7b check_post 挂在执行出口", "_abr.check_post(name, arguments, res)" in src)
    finally:
        c.close()
        try:
            tmp.unlink()
        except Exception:
            pass

    prod = sqlite3.connect(str(src_db))
    now = prod.execute("SELECT COUNT(*) FROM ontology_type_actions").fetchone()[0]
    prod.close()
    check("M7 生产库未被污染", now == prod_base, f"{prod_base} → {now}")

    print(f"\n结果：{len(PASS)} pass / {len(FAIL)} fail")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
