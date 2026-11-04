# -*- coding: utf-8 -*-
"""流水线配置资产自举：把「agents / tools / skills / 绑定 / 团队」做成可重建的**快照**。

── 为什么需要这个模块（2026-11-04，米爸「后续手动新增的 agent 还会出问题吗」）──────

本轮实测（同一份代码，两个库）：

| 项 | 干净库（`init_db()` 一条命令） | 真库 | 差 |
|---|---|---|---|
| agents | **7** | 30 | 23 |
| tools | **14** | 45 | 31 |
| skills | **0** | 42 | 42 |

差距全部来自 **25 个手工迁移脚本**（`tools/migrate_v*.py`），它们：
  · 不在 `init_db()` 调用链里 ⇒ 空库永远长不到真库的样子；
  · 其中 10 个还**硬编码 `DB = "mbse.db"`**（无视 `MBSE_DB_PATH`）⇒
    迁移永远改开发机的真库，CI 上连到的是空库/不存在的库。
  ⇒ 结论：**环境不可重建**。CI 的绿灯与真库状态无关，
    "门禁通过"不代表"这台机器上的产品是对的"。

深层原因不是懒，而是**能力以数据形态存在**（agent 的 prompt/schema/绑定都写在 DB 行里），
而数据没有版本化载体 ⇒ 只能靠一次性脚本搬运，搬完就与代码脱钩。

── 本模块的做法 ──────────────────────────────────────────────────────────
把「配置类资产」导出为**随代码走的 JSON 快照**，`init_db()` 末尾据此 **UPSERT** 回库：
  · 键用**业务名**（agent.name / tool.name / skill.name），不用自增 id ⇒ 跨库可移植；
  · 关联表（agent_tools / agent_team_members）导出**名字**，导入时再解析 id；
  · 只搬**声明性列**，排除运行时列（使用次数/安装数/时间戳）⇒ 快照稳定、不无谓漂移；
  · UPSERT 语义 = **库里已存在则不覆盖用户改动**？—— 不。
    这里刻意选择**以快照为准**（覆盖），因为快照是"代码的一部分"；
    若允许库里改而不回流，就又回到"环境不可重建"。
    ⚠️ 但 `status` / `enabled` 这两个**运行期开关**不去覆盖（管理员停用不该被自举打开）。

── 配套的闭环 ─────────────────────────────────────────────────────────────
单靠自举还不够：**手动新增 agent 后忘了重新导出**，快照就又落后。
⇒ `tools/verify/verify_asset_snapshot_sync.py` 做漂移检测（新增/删除/关键字段变更即红），
  提示"快照过时，请运行 tools/export_asset_snapshot.py"。这才是闭环。
"""
from __future__ import annotations

import json
import os

#: 快照文件（随代码提交）
SNAPSHOT_REL = os.path.join("data", "bootstrap", "pipeline_assets.json")

#: 表 → (业务键列, 排除的列)
#: 排除项一律是**运行时/统计/自增**列 —— 它们不属于"配置"，纳入快照只会制造无谓漂移。
SPEC = {
    "agents": {
        "key": "name",
        "exclude": {"id", "created_at", "updated_at",
                    # 运行期开关：管理员停用某 agent 不该被自举重新打开
                    "status"},
    },
    "tools": {
        "key": "name",
        "exclude": {"id", "created_at", "status",
                    # 统计/用户行为
                    "pinned", "install_count"},
    },
    "skills": {
        "key": "name",
        "exclude": {"id", "created_at", "updated_at", "status",
                    # 统计
                    "use_stats", "feedback_notes", "pinned", "install_count"},
    },
}

#: 不进快照的行（**运行产物 / 测试残留**，不是配置）。
#: 判据要给理由，否则以后没人敢改：
#:   · `skills.source ∈ ai_deposit/ai_improve` = AI 自沉淀/自改进的产物，
#:     属运行期演化结果，随时间变化 ⇒ 纳入快照会让快照永远在漂移；
#:     它们的价值应通过知识库/技能市场管理，不是通过"随代码发布"。
#:   · `name` 含「测试」且 builtin=0 = 本机联调残留，跨环境无意义。
def _keep_row(table: str, rec: dict) -> bool:
    if table == "skills":
        if str(rec.get("source") or "manual") != "manual":
            return False
    if table == "agents":
        nm = str(rec.get("name") or "")
        if not rec.get("builtin") and "测试" in nm:
            return False
    return True

#: 关联表：导出名字而非 id（跨库移植的前提）
REL_SPEC = {
    "agent_tools": {
        "table": "agent_tools",
        "fields": ("tool_type", "tool_name", "enabled"),
        "owner_col": "agent_id", "owner_ref": "agents",
        "target_col": None, "target_ref": None,
    },
    "agent_team_members": {
        "table": "agent_team_members",
        "fields": ("enabled",),
        "owner_col": "main_agent_id", "owner_ref": "agents",
        "target_col": "sub_agent_id", "target_ref": "agents",
    },
}


def _q(name: str) -> str:
    return '"%s"' % str(name).replace('"', '""')


def snapshot_path(base_dir: str | None = None) -> str:
    if base_dir is None:
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_dir, SNAPSHOT_REL)


# ── 导出 ────────────────────────────────────────────────────────────────

def _dedupe_by_key(table: str, rows: list, key: str) -> tuple:
    """按业务键去重。返回 (去重后 rows, 被合并的同名键列表)。

    ★ 2026-11-04 实测：真库 `skills` 存在**同名多行**（`行业调研` ×3、`Requirement_Analysis_Pre-check` ×4、
      `智源平台管理` ×3），共 42 行 / 33 个唯一名。
      快照以 name 为键 ⇒ 同名行**无法表达**，导入时按 name UPSERT 会合并 ⇒
      "快照 30 行、库 28 行"这种对不上（门禁 A4b 抓到）。

      两个选择：① 导出时静默取一条（**掩盖数据缺陷**）；② 显式取一条并**报告同名冲突**（本实现）。
      取哪一条：`updated_at` 最新（无该列则取 id 最大）—— 最可能是当前意图；
        同名的历史残留应人工清理（本模块只如实报告，不替你删）。
    """
    seen, out, dups = {}, [], []
    for r in sorted(rows, key=lambda d: (str(d.get("updated_at") or ""), d.get("id") or 0)):
        k = str(r.get(key) or "")
        if k in seen:
            if k not in dups:
                dups.append(k)
            seen[k] = r            # 后来者（updated_at 更大）胜出
        else:
            seen[k] = r
    out = list(seen.values())
    return out, dups


def export_snapshot(conn, path: str | None = None, dry_run: bool = False) -> dict:
    """把配置类资产导出为快照文件。返回统计 {table: rows}。

    dry_run=True ⇒ **不写盘**，只统计（预览用）。
    """
    out = {"schema_version": 1, "tables": {}, "relations": {}}
    stats = {}
    for table, meta in SPEC.items():
        cols = [r[1] for r in conn.execute("PRAGMA table_info(%s)" % _q(table))
                if r[1] not in meta["exclude"]]
        rows = [dict(r) for r in conn.execute(
            "SELECT %s FROM %s" % (",".join(_q(c) for c in cols), _q(table)))]
        rows = [r for r in rows if _keep_row(table, r)]      # 过滤运行产物/测试残留
        # 按业务键去重（同名多行见 _dedupe_by_key 说明）：快照以 name 为键，同名无法表达
        rows, dups = _dedupe_by_key(table, rows, meta["key"])
        if dups:
            stats.setdefault("_duplicates", {})[table] = dups
        rows.sort(key=lambda d: str(d.get(meta["key"]) or ""))
        out["tables"][table] = rows
        stats[table] = len(rows)

    # 关联表：id → name
    for rel, rs in REL_SPEC.items():
        owner_names = {r[0]: r[1] for r in conn.execute("SELECT id, name FROM agents")}
        fields = list(rs["fields"])
        sel = ["%s" % rs["owner_col"]] + ([rs["target_col"]] if rs["target_col"] else []) + fields
        rows = []
        for r in conn.execute("SELECT %s FROM %s" % (",".join(sel), _q(rs["table"]))):
            d = dict(r)
            rec = {"agent": owner_names.get(d[rs["owner_col"]], "")}
            if rs["target_col"]:
                rec["target"] = owner_names.get(d[rs["target_col"]], "")
            for f in fields:
                rec[f] = d.get(f)
            if rec["agent"]:
                rows.append(rec)
        rows.sort(key=lambda d: (str(d.get("agent")), str(d.get("target")),
                                 str(d.get("tool_type")), str(d.get("tool_name"))))
        out["relations"][rel] = rows
        stats[rel] = len(rows)

    if dry_run:
        return stats

    p = path or snapshot_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        json.dump(out, f, ensure_ascii=False, indent=1, sort_keys=False)
        f.write("\n")
    return stats


def load_snapshot(path: str | None = None) -> dict | None:
    p = path or snapshot_path()
    if not os.path.isfile(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


# ── 导入（自举）──────────────────────────────────────────────────────────

def bootstrap_assets(conn, path: str | None = None, dry_run: bool = False) -> dict:
    """把快照 UPSERT 进库（幂等）。返回统计。

    **以快照为准**（覆盖库里同名的声明性字段），因为快照随代码版本走；
    但 `status` / `enabled` 类运行期开关**不覆盖**（见模块头说明）。
    """
    snap = load_snapshot(path)
    out = {"ok": False, "reason": "", "inserted": 0, "updated": 0, "unchanged": 0,
           "relations": 0, "skipped": 0, "ambiguous": 0, "ambiguous_keys": []}
    if not snap:
        out["reason"] = "no_snapshot"
        return out

    try:
        _bootstrap_tables(conn, snap, out, dry_run)
        _bootstrap_relations(conn, snap, out, dry_run)
        if not dry_run:
            conn.commit()
        out["ok"] = True
    except Exception as e:                      # 自举失败必须可见（不静默）
        out["reason"] = "%s: %s" % (type(e).__name__, str(e)[:160])
        try:
            conn.rollback()
        except Exception:
            pass
    return out


def _bootstrap_tables(conn, snap: dict, out: dict, dry_run: bool) -> None:
    for table, meta in SPEC.items():
        rows = (snap.get("tables") or {}).get(table) or []
        if not rows:
            continue
        key = meta["key"]
        # 只写"库里存在的列"（老库可能缺列）—— 缺列跳过而不是整批失败
        live_cols = {r[1] for r in conn.execute("PRAGMA table_info(%s)" % _q(table))}
        for rec in rows:
            kv = str(rec.get(key) or "")
            if not kv:
                out["skipped"] += 1
                continue
            cols = [c for c in rec.keys() if c in live_cols and c != key]
            # ⚠️⚠️ 2026-11-04 **实测事故修复**：原写法 `UPDATE ... WHERE name=?` 会在
            #   **同名多行**时命中**全部**同名行，把它们的内容统一覆盖成快照那一条。
            #   真库实证（本轮造成，已记录）：`行业调研` 5 行 content 全被改成同一份（1040 字符）、
            #   `智源平台管理` 3 行全变 4958 字符 —— **5 条不同的 skill 变成 5 条副本**。
            #   根因：skills 表**没有 name 唯一约束**，而快照以 name 为键 ⇒ 语义上无法定位"是哪一行"。
            #   ⇒ 现在：**同名多行一律跳过**（计入 ambiguous），并显式报告要求人工清理。
            #      "跳过"是安全侧：自举是补齐环境，不是替人裁决同名冲突该留哪条。
            _dup_n = conn.execute(
                "SELECT COUNT(*) FROM %s WHERE %s=?" % (_q(table), _q(key)), (kv,)).fetchone()[0]
            if _dup_n > 1:
                out["ambiguous"] = out.get("ambiguous", 0) + 1
                out.setdefault("ambiguous_keys", []).append("%s:%s" % (table, kv))
                continue
            cur = 1 if _dup_n == 1 else 0
            if cur:
                if not cols:
                    out["unchanged"] += 1
                    continue
                if dry_run:
                    out["updated"] += 1
                    continue
                sets = ",".join("%s=?" % _q(c) for c in cols)
                conn.execute("UPDATE %s SET %s WHERE %s=?" % (_q(table), sets, _q(key)),
                             tuple(rec.get(c) for c in cols) + (kv,))
                out["updated"] += 1
            else:
                if dry_run:
                    out["inserted"] += 1
                    continue
                allc = [key] + cols
                conn.execute("INSERT INTO %s (%s) VALUES (%s)" % (
                    _q(table), ",".join(_q(c) for c in allc), ",".join("?" * len(allc))),
                    tuple(rec.get(c) for c in allc))
                out["inserted"] += 1


def _bootstrap_relations(conn, snap: dict, out: dict, dry_run: bool) -> None:
    id_of = {r[1]: r[0] for r in conn.execute("SELECT id, name FROM agents")}
    for rel, rs in REL_SPEC.items():
        rows = (snap.get("relations") or {}).get(rel) or []
        if not rows:
            continue
        fields = list(rs["fields"])
        live_cols = {r[1] for r in conn.execute("PRAGMA table_info(%s)" % _q(rs["table"]))}
        fields = [f for f in fields if f in live_cols]
        for rec in rows:
            aid = id_of.get(str(rec.get("agent") or ""))
            if not aid:                       # 快照引用的 agent 不在库 ⇒ 跳过并计数
                out["skipped"] += 1
                continue
            sel = "%s=?" % rs["owner_col"]
            params = [aid]
            ins_cols, ins_vals = [rs["owner_col"]], [aid]
            if rs["target_col"]:
                tid = id_of.get(str(rec.get("target") or ""))
                if not tid:
                    out["skipped"] += 1
                    continue
                sel += " AND %s=?" % rs["target_col"]
                params.append(tid)
                ins_cols.append(rs["target_col"])
                ins_vals.append(tid)
            for f in fields:
                sel += " AND %s=?" % _q(f)
                params.append(rec.get(f))
            if conn.execute("SELECT 1 FROM %s WHERE %s" % (_q(rs["table"]), sel),
                            tuple(params)).fetchone():
                continue
            if dry_run:
                out["relations"] += 1
                continue
            for f in fields:
                ins_cols.append(f)
                ins_vals.append(rec.get(f))
            conn.execute("INSERT INTO %s (%s) VALUES (%s)" % (
                _q(rs["table"]), ",".join(_q(c) for c in ins_cols), ",".join("?" * len(ins_cols))),
                tuple(ins_vals))
            out["relations"] += 1


# ── 漂移检测（供门禁）────────────────────────────────────────────────────

def snapshot_drift(conn, path: str | None = None) -> dict:
    """比对快照与库，返回差异。用于"新增了 agent 但忘了导出快照"的告警。

    返回 {"ok": bool, "missing_in_db": [...], "missing_in_snapshot": [...],
           "field_diff": [...], "relation_drift": [...]}
      · missing_in_db        : 快照有、库无 ⇒ 自举没生效 / 库被删
      · missing_in_snapshot  : 库有、快照无 ⇒ **手动新增但没导出**（最常见）
      · field_diff           : 同名但声明性字段不一致 ⇒ 改了没导出 / 导出了没自举
    """
    snap = load_snapshot(path)
    res = {"ok": False, "reason": "", "missing_in_db": [], "missing_in_snapshot": [],
           "field_diff": [], "relation_drift": []}
    if not snap:
        res["reason"] = "no_snapshot"
        return res
    # 只看"随代码走的资产"：与导出用**同一套过滤**（否则测试残留/运行产物会让门禁永远红）。
    # 实测反例：真库有 `测试agent`（builtin=0, disabled），若按 builtin=0 纳入比较，
    # 快照里没有它 ⇒ 门禁天天红 ⇒ 又一次"狼来了"。
    try:
        live_rows = [dict(r) for r in conn.execute("SELECT * FROM agents")]
    except Exception:
        live_rows = []
    live_names = {r.get("name") for r in live_rows if _keep_row("agents", r)}
    snap_names = {str(r.get("name")) for r in ((snap.get("tables") or {}).get("agents") or [])}
    res["missing_in_db"] = sorted(snap_names - {r.get("name") for r in live_rows})
    res["missing_in_snapshot"] = sorted(n for n in (live_names - snap_names) if n)

    # 关键字段比对（只比"声明性且高影响"的几列，避免噪声）
    KEY_COLS = ("system_prompt", "hil_level", "agent_role", "task_domain",
                "stage_order", "input_schema", "output_schema", "capabilities")
    live_cols = {r[1] for r in conn.execute("PRAGMA table_info(agents)")}
    KEY_COLS = [c for c in KEY_COLS if c in live_cols]
    live = {r["name"]: dict(r) for r in conn.execute(
        "SELECT name,%s FROM agents" % ",".join(KEY_COLS))}
    for rec in ((snap.get("tables") or {}).get("agents") or []):
        nm = str(rec.get("name"))
        if nm not in live:
            continue
        for c in KEY_COLS:
            a, b = rec.get(c), live[nm].get(c)
            if str(a if a is not None else "") != str(b if b is not None else ""):
                res["field_diff"].append("%s.%s" % (nm, c))

    res["ok"] = not (res["missing_in_db"] or res["missing_in_snapshot"] or res["field_diff"])
    if not res["ok"] and not res["reason"]:
        res["reason"] = "drift"
    return res
