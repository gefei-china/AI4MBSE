"""migrate_v25_sync_plan_contracts_and_skill_tools —— 真库两处漂移的收口（2026-10-26）。

## 触发原因（2026-10-26 实测；两项都是 `verify_pipeline_closure` 的红项）

### ① skill 的工具引用空转（③ 引用有效性 → 红）

`skills` 表 id=95「智源平台管理」的 `allowed_tools` 有 13 项，其中 **7 项在 `tools` 表不存在**：

    query_projects / query_data_tree / query_branches / query_model_versions
    export_sysml_v2 / validate_sysml_text / import_sysml_text

智源工具早已迁到 `zhiyuan_*` 体系（现存 6 个：`zhiyuan_project_list` / `zhiyuan_project_tree` /
`zhiyuan_sysmlv2_gen` / `zhiyuan_sysmlv2_check` / `zhiyuan_sysmlv2_import` / `mbse_pull_ingest`），
skill 侧没跟着改 ⇒ **指向不存在的工具 = 该 skill 的工具约束空转**（静默失效，
界面上看不出任何异常，只是约束根本没生效）。

### ② M0 节点契约缺失（④ 数据链路闭环 → 红）

`agents.methodology_resolver`（M0）的 `input_schema` / `output_schema` 在真库是 `'{}'`
（空对象）⇒ 入口无契约、出口无契约 ⇒ 产物无承载物，链路不闭环。

实测 8 个规划节点里 **7 个与规划文件 `data/modeling_nodes.json` 完全一致、仅 M0 是空**
⇒ M0 是**漏同步的个例**，不是"规划变了"。证据：N1/N2/N3/N4/N5/N6/G 的 in/out 全部 `same`。

## 修法（两项都幂等：再跑一次 0 改动）

① **删失效引用，不补空引用**：`allowed_tools` 里只保留 `tools` 表中真实存在的名字。
   （若反过来"给不存在的工具建行"，等于凭空造 7 个没有实现的工具注册 ⇒ 更坏。）
② **M0 契约从规划文件同步**：仅当「库里为空/`{}`」且「规划里有定义」时才写入 ⇒
   不会覆盖任何已同步过的值；规划文件缺失时**跳过并明确提示**（`data/` 是 .gitignore 的，
   别的机器上可能没有这个文件 ⇒ 脚本不许崩、不许猜）。

## ⚠️ 不在同步范围：`capabilities`（别按规划覆盖）

`capabilities` 受 `agent/registry.py` 的 `_CAPABILITIES_ENUM`（23 词白名单）硬约束，
落库时不在白名单的词会被**静默丢弃**；规划文件里的描述性词表（如 "SysML v2 视图展开"
"八视图固定顺序依赖"）**根本存不进去**。真库 8 个节点的 caps 全部是枚举内词
（如 `view_expansion` = ["系统建模", "工程建模"]）⇒ **这不是漂移，是枚举约束下的正常形态**。
真按规划覆盖 = 写入的值当场被静默丢弃，且看起来"同步过了"——本脚本明确不碰它。

## 判据（出口断言，跑完必查）

① 全库 `skills.allowed_tools` 中**不存在**指向 `tools` 表缺失项的引用（0 处）
② 8 个规划节点的 `input_schema`/`output_schema` 全部非空，且与规划**逐字段相等**
③ 幂等：再跑一次改动数为 0

用法：
```
./.venv/Scripts/python.exe -X utf8 tools/migrate_v25_sync_plan_contracts_and_skill_tools.py --dry-run
./.venv/Scripts/python.exe -X utf8 tools/migrate_v25_sync_plan_contracts_and_skill_tools.py
```
"""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

PLAN = os.path.join(ROOT, "data", "modeling_nodes.json")


def _jlist(v, default=None):
    if v is None:
        return list(default or [])
    if isinstance(v, (list, tuple)):
        return list(v)
    try:
        r = json.loads(v)
        return r if isinstance(r, list) else list(default or [])
    except Exception:
        return list(default or [])


def _jobj(v):
    if isinstance(v, dict):
        return v
    try:
        r = json.loads(v or "{}")
        return r if isinstance(r, dict) else {}
    except Exception:
        return {}


def collect_skill_fixes(conn):
    """返回 [(skill_id, name, 旧列表, 新列表, 失效名)]。"""
    tool_names = {r[0] for r in conn.execute("SELECT name FROM tools")}
    out = []
    for sid, name, allowed in conn.execute(
            "SELECT id, name, allowed_tools FROM skills").fetchall():
        at = _jlist(allowed)
        bad = [t for t in at if t not in tool_names]
        if bad:
            out.append((sid, name, at, [t for t in at if t in tool_names], bad))
    return out


def collect_schema_fixes(conn, plan):
    """返回 [(agent_name, key, 旧值, 新值)]；只补「库里为空且规划里有定义」的。"""
    out = []
    for n in plan.get("nodes", []):
        a = n.get("agent") or {}
        nm = a.get("name")
        if not nm:
            continue
        row = conn.execute(
            "SELECT input_schema, output_schema FROM agents WHERE name=?", (nm,)).fetchone()
        if row is None:
            continue
        for key, want in (("input_schema", a.get("input_schema") or {}),
                          ("output_schema", a.get("output_schema") or {})):
            if not want:
                continue
            live = _jobj(row[0] if key == "input_schema" else row[1])
            if not live:                      # 空 / '{}' ⇒ 漏同步
                out.append((nm, key, row[0] if key == "input_schema" else row[1],
                            json.dumps(want, ensure_ascii=False)))
    return out


def verify(conn, plan):
    """出口断言；返回 (是否通过, 明细行)。"""
    lines, ok = [], True
    tool_names = {r[0] for r in conn.execute("SELECT name FROM tools")}
    dangling = []
    for sid, name, allowed in conn.execute(
            "SELECT id, name, allowed_tools FROM skills").fetchall():
        for t in _jlist(allowed):
            if t not in tool_names:
                dangling.append("%s→%s" % (name, t))
    if dangling:
        ok = False
        lines.append("  [FAIL] 仍有失效工具引用 %d 处: %s" % (len(dangling), dangling[:8]))
    else:
        lines.append("  [OK  ] 全部 skill 的 allowed_tools 引用均存在")

    if plan:
        for n in plan.get("nodes", []):
            a = n.get("agent") or {}
            nm = a.get("name")
            row = conn.execute(
                "SELECT input_schema, output_schema FROM agents WHERE name=?", (nm,)).fetchone()
            if row is None:
                continue
            for key, want in (("input_schema", a.get("input_schema") or {}),
                              ("output_schema", a.get("output_schema") or {})):
                if not want:
                    continue
                live = _jobj(row[0] if key == "input_schema" else row[1])
                if live != want:
                    ok = False
                    lines.append("  [FAIL] %s.%s 与规划不一致（live=%s）"
                                 % (nm, key, "空" if not live else "非空但不等"))
        lines.append("  [OK  ] 8 个规划节点的 input/output schema 逐字段核对完毕"
                     if ok else "")
    else:
        lines.append("  [SKIP] 规划文件不存在（data/ 未纳 git），跳过 schema 核对")
    return ok, lines


def main() -> int:
    ap = argparse.ArgumentParser(description="v25：清理 skill 失效工具引用 + 同步 M0 契约")
    ap.add_argument("--dry-run", action="store_true", help="只打印不写（默认真改）")
    args = ap.parse_args()

    from core.config import DB_PATH
    from database.schema import backup_db
    import sqlite3

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        plan = None
        if os.path.exists(PLAN):
            with open(PLAN, encoding="utf-8") as f:
                plan = json.load(f)
        else:
            print("[warn] 规划文件不存在：%s（data/ 在 .gitignore 里，别的机器可能没有）"
                  % os.path.relpath(PLAN, ROOT))

        skill_fixes = collect_skill_fixes(conn)
        schema_fixes = collect_schema_fixes(conn, plan) if plan else []

        print("=== 待改动 ===")
        for _sid, name, old, new, bad in skill_fixes:
            print("  [skill] %s：allowed_tools %d → %d 项，剔除失效 %s"
                  % (name, len(old), len(new), bad))
        for nm, key, old, new in schema_fixes:
            print("  [agent] %s.%s：%r → 规划定义" % (nm, key, old))
        if not skill_fixes and not schema_fixes:
            print("  无改动（已幂等）")

        if args.dry_run:
            print("\n[dry-run] 未写库。去掉 --dry-run 执行。")
            return 0

        if skill_fixes or schema_fixes:
            bak = backup_db(tag="pre-v25-contracts")
            print("\n[backup] %s" % os.path.relpath(bak, ROOT))
            for _sid, _name, _old, new, _bad in skill_fixes:
                conn.execute("UPDATE skills SET allowed_tools=? WHERE id=?",
                             (json.dumps(new, ensure_ascii=False), _sid))
            for nm, key, _old, new in schema_fixes:
                conn.execute("UPDATE agents SET %s=? WHERE name=?" % key, (new, nm))
            conn.commit()
            print("[apply] 已提交：skill %d 条 / agent %d 条"
                  % (len(skill_fixes), len(schema_fixes)))

        ok, lines = verify(conn, plan)
        print("\n=== 出口断言 ===")
        for ln in lines:
            if ln:
                print(ln)
        # 幂等自证：再算一次待改动，应为 0
        again_s = collect_skill_fixes(conn)
        again_a = collect_schema_fixes(conn, plan) if plan else []
        print("  [%s] 幂等：再算一次待改动 = skill %d / agent %d"
              % ("OK  " if not (again_s or again_a) else "FAIL",
                 len(again_s), len(again_a)))
        if again_s or again_a:
            ok = False
        return 0 if ok else 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
