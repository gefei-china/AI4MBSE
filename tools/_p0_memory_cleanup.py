"""P0-4：`agent_memory` 数据一致性清理（42 行 forgotten/tier 冲突 + 7 组重复）。

⚠️ 取证先行（2026-11-15 真库只读实测，不凭方案文档的旧数字下笔）：
  · `forgotten=1` 共 97 行：archival 55 / **recall 42**；`forgotten=0` 共 197 行：core 5 / recall 192。
    ⇒ 42 行冲突里**没有 core**，不影响 `search_core` 的常驻注入。
  · 7 组重复共 19 行，**全部 forgotten=1 且 tier=archival**，且 `superseded_by` 全库为 0。
  · `memory_service.search()` 的 `_base` 恒含 `forgotten=0` + `(tier IS NULL OR tier!='archival')`
    + `COALESCE(superseded_by,0)=0`；`search_core()` 同样含 `tier='core' AND forgotten=0`。
    ⇒ 这两项清理对「模型实际可见的记忆集合」应当是**零行为变化**。

处置（不删任何行，全部可单步回滚）：
  ① 42 行 `forgotten=1 AND tier='recall'` → `tier='archival'`（遗忘即归档，P1-18 的设计语义）。
  ② 7 组重复：每组保留 1 行主记录（未遗忘优先 → 访问数高优先 → id 小优先），
     其余 12 行写 `superseded_by=<主行 id>` + `superseded_at=<UTC 时刻>`。
     不物理删除：这些是历史沉淀资产，且标记后检索侧本就按 superseded_by 排除（零变化）。
  ③ **不在本批动** `verify_state`（294/294 unverifiable）、`evidence_ref`、跨域可见性
     —— 属 P1-5 记忆质量闭环，需评估集支撑，越界改就是拿统计错误当行为变更。

硬证据（写库前在**真库副本**上跑，磁盘真库一字不动）：
  · A/B：清理前后分别用 `MemoryService.search(any_scope=True)` + `search_core()` 取可见 id 集合，
    断言**完全相同**。
  · 变异 M2：在副本上故意把一条活跃记忆也改成 archival，A/B 必须抓到差异 —— 证明
    「零行为变化」不是空转断言（§6.2）。
  · 备份 `sqlite3.Connection.backup()` 到 backups/，逐行 before/after + 单步回滚 SQL 落 JSON。

跑法：./.venv/Scripts/python.exe -X utf8 tools/_p0_memory_cleanup.py [--apply]
"""
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from memory_service import MemoryService  # noqa: E402

DB = REPO / "mbse.db"
BACKUPS = REPO / "backups"
TMP = REPO / "tmp"
OUT = REPO / "docs" / "评测" / "p0-4-memory-cleanup-20261115.json"

PASS, FAIL = [], []


def ck(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(("PASS " if ok else "FAIL ") + name + ((" | " + str(detail)) if detail else ""))


def build_plan(conn):
    """只读派生完整处置计划（单一真源；dry-run 与 apply 用同一份）。"""
    conn.row_factory = sqlite3.Row
    conflict = [dict(r) for r in conn.execute(
        "SELECT id, tier, forgotten, superseded_by, superseded_at FROM agent_memory "
        "WHERE forgotten=1 AND tier!='archival' ORDER BY id")]
    groups = [dict(r) for r in conn.execute(
        "SELECT content, COUNT(*) n, GROUP_CONCAT(id) ids FROM agent_memory "
        "GROUP BY content HAVING n>1 ORDER BY MIN(id)")]
    dup_rows, dup_fix, keepers = [], [], []
    for g in groups:
        ids = [int(x) for x in g["ids"].split(",")]
        rows = [dict(r) for r in conn.execute(
            "SELECT id, forgotten, tier, access_count, superseded_by, superseded_at "
            "FROM agent_memory WHERE id IN (%s)" % ",".join("?" * len(ids)), ids)]
        rows.sort(key=lambda r: (int(r["forgotten"] or 0), -int(r["access_count"] or 0), r["id"]))
        keep = rows[0]["id"]
        keepers.append({"content": g["content"][:80], "n": g["n"], "keep_id": keep,
                        "dup_ids": [r["id"] for r in rows[1:]]})
        dup_rows.extend(rows)
        for r in rows[1:]:
            dup_fix.append({"id": r["id"], "keep_id": keep,
                            "before": {"superseded_by": r["superseded_by"],
                                       "superseded_at": r["superseded_at"]},
                            "forgotten": r["forgotten"], "tier": r["tier"]})
    return {"tier_fix": conflict, "dup_fix": dup_fix, "groups": keepers}


def apply_plan(conn, plan, ts):
    for r in plan["tier_fix"]:
        conn.execute("UPDATE agent_memory SET tier='archival' WHERE id=?", (r["id"],))
    for r in plan["dup_fix"]:
        conn.execute("UPDATE agent_memory SET superseded_by=?, superseded_at=? WHERE id=?",
                     (r["keep_id"], ts, r["id"]))
    conn.commit()


def visible_ids(conn):
    """模型实际能看到的记忆 id 集合（两路：全局召回 + core 常驻注入）。"""
    conn.row_factory = sqlite3.Row
    a = sorted(r["id"] for r in MemoryService.search(
        conn, "", query="", top_k=100000, any_scope=True))
    core = set()
    for (aid,) in conn.execute("SELECT DISTINCT agent_id FROM agent_memory"):
        for r in MemoryService.search_core(conn, aid, top_k=100000):
            core.add(r["id"])
    return {"search_any": a, "search_core": sorted(core)}


def conflicts(conn):
    return int(conn.execute(
        "SELECT COUNT(*) FROM agent_memory WHERE forgotten=1 AND tier!='archival'"
    ).fetchone()[0])


def snapshot_counts(conn):
    return {t: int(conn.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0])
            for t in ("agent_memory", "skills", "tools", "agents", "eval_reports")}


def main():
    apply = "--apply" in sys.argv
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    ro = sqlite3.connect("file:%s?mode=ro" % DB.as_posix(), uri=True)
    plan = build_plan(ro)
    before_counts = snapshot_counts(ro)
    n_conf = conflicts(ro)
    n_dups = len(plan["groups"])
    ro.close()

    print("BEFORE 冲突行=%d 重复组=%d 待标记副本=%d rows=%s"
          % (len(plan["tier_fix"]), n_dups, len(plan["dup_fix"]), before_counts))
    ck("P1 计划与实测冲突数一致", len(plan["tier_fix"]) == n_conf == 42,
       "plan=%d sql=%d" % (len(plan["tier_fix"]), n_conf))
    ck("P2 冲突行里没有 core（不影响常驻注入）",
       all(r["tier"] != "core" for r in plan["tier_fix"]))
    ck("P3 重复组数=7 / 待标记副本=12 / 与主行集合不重叠",
       n_dups == 7 and len(plan["dup_fix"]) == 12
       and not ({r["id"] for r in plan["tier_fix"]} &
                {r["id"] for r in plan["dup_fix"]}))
    ck("P4 重复副本全部已是 forgotten=1 + archival（标记不改变可见性）",
       all(r["forgotten"] == 1 and r["tier"] == "archival" for r in plan["dup_fix"]))

    if not apply:
        print("DRY-RUN（未写盘）；加 --apply 执行：备份 → 副本 A/B → 真库清理 → 留痕")
        print("示例：", plan["groups"][:2])
        print("RESULT: %d PASS / %d FAIL" % (len(PASS), len(FAIL)))
        return 1 if FAIL else 0

    # ── ① 备份（保持「清理前」原样，不拿它做 A/B 沙箱）──
    BACKUPS.mkdir(exist_ok=True)
    TMP.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    bak = BACKUPS / ("mbse.db.bak-before-p0-4-memory-%s.db" % stamp)
    src = sqlite3.connect("file:%s?mode=ro" % DB.as_posix(), uri=True)
    dst = sqlite3.connect(str(bak))
    src.backup(dst)
    n_src = src.execute("SELECT COUNT(*) FROM sqlite_master WHERE type IN ('table','view')").fetchone()[0]
    n_dst = dst.execute("SELECT COUNT(*) FROM sqlite_master WHERE type IN ('table','view')").fetchone()[0]
    dst.close()
    src.close()
    ck("B1 全库备份成功（清理前快照，未被动过）",
       n_src == n_dst and bak.stat().st_size > 0,
       "%s size=%d objects=%d" % (bak.name, bak.stat().st_size, n_dst))

    # ── ② 副本 A/B：证明「零行为变化」，并做变异证明判据有牙齿 ──
    # 内存副本：从「清理前备份」backup 一份，避免再落 534 MB 磁盘副本。
    bak_ro = sqlite3.connect("file:%s?mode=ro" % bak.as_posix(), uri=True)
    ab = sqlite3.connect(":memory:")
    bak_ro.backup(ab)
    bak_ro.close()
    before_ids = visible_ids(ab)
    apply_plan(ab, plan, ts)
    after_ids = visible_ids(ab)
    ck("A1 副本 A/B：清理前后可见记忆 id 集合完全相同（零行为变化）",
       before_ids == after_ids,
       "search_any %d→%d / core %d→%d" % (len(before_ids["search_any"]),
                                          len(after_ids["search_any"]),
                                          len(before_ids["search_core"]),
                                          len(after_ids["search_core"])))
    ck("A2 副本清理后冲突数归零 / superseded_by 非零 12 行",
       conflicts(ab) == 0
       and ab.execute("SELECT COUNT(*) FROM agent_memory WHERE COALESCE(superseded_by,0)>0").fetchone()[0] == 12)

    # 变异 M2：故意误伤一条活跃记忆，A/B 判据必须翻红
    victim = ab.execute(
        "SELECT id FROM agent_memory WHERE forgotten=0 AND tier!='archival' "
        "ORDER BY id LIMIT 1").fetchone()[0]
    ab.execute("UPDATE agent_memory SET tier='archival' WHERE id=?", (victim,))
    ab.commit()
    mutated_ids = visible_ids(ab)
    ck("M1 变异（误把 1 条活跃记忆改成 archival）⇒ A/B 判据必须抓到差异",
       mutated_ids != before_ids,
       "victim=%d before=%d mutated=%d" % (victim, len(before_ids["search_any"]),
                                           len(mutated_ids["search_any"])))
    # 变异 M1（静态）：只清一半冲突行，conflicts() 仍应 >0
    ab2 = sqlite3.connect(":memory:")
    ab2.execute("CREATE TABLE agent_memory(id INTEGER, forgotten INT, tier TEXT)")
    ab2.executemany("INSERT INTO agent_memory VALUES(?,?,?)",
                    [(1, 1, "recall"), (2, 1, "recall"), (3, 0, "recall")])
    ck("M2 变异（冲突数判据：库里仍有冲突时不得报 0）", conflicts(ab2) == 2)
    ab2.close()
    ab.close()
    sandbox_path = "内存副本（由清理前备份 backup 得到，未落盘）"

    # ── ③ 真库执行（与副本完全同一份 plan）──
    w = sqlite3.connect(str(DB))
    try:
        apply_plan(w, plan, ts)
        after_counts = snapshot_counts(w)
        ck("A3 真库冲突行归零", conflicts(w) == 0, "before=%d after=%d" % (n_conf, conflicts(w)))
        ck("A4 真库 superseded_by 非零 12 行（每组只留 1 行未被取代）",
           w.execute("SELECT COUNT(*) FROM agent_memory WHERE COALESCE(superseded_by,0)>0").fetchone()[0] == 12)
        ck("A5 所有表行数未变（没有物理删除）", after_counts == before_counts,
           "%s -> %s" % (before_counts, after_counts))
        ck("A6 core 层 forgotten=0 的 5 行原封不动",
           w.execute("SELECT COUNT(*) FROM agent_memory WHERE tier='core' AND forgotten=0").fetchone()[0] == 5)
    finally:
        w.close()

    # ── ④ 逐行留痕 + 单步回滚 SQL ──
    rollback = ["UPDATE agent_memory SET tier='recall' WHERE id=%d;" % r["id"]
                for r in plan["tier_fix"]]
    for r in plan["dup_fix"]:
        old = r["before"]["superseded_at"]
        lit = "NULL" if old is None else "'%s'" % str(old).replace("'", "''")
        rollback.append("UPDATE agent_memory SET superseded_by=%s, superseded_at=%s WHERE id=%d;"
                        % (int(r["before"]["superseded_by"] or 0), lit, r["id"]))
    out = {
        "schema": "p0/memory-cleanup@1",
        "applied_at_utc": ts,
        "evidence": {
            "before": {"conflict_rows": n_conf, "duplicate_groups": n_dups,
                       "duplicate_rows": sum(g["n"] for g in plan["groups"]),
                       "superseded_by_nonzero": 0, **before_counts},
            "after": {"conflict_rows": 0, "duplicate_groups_still_physical": n_dups,
                      "superseded_by_nonzero": len(plan["dup_fix"]), **after_counts},
            "ab_proof": {"visible_before": {k: len(v) for k, v in before_ids.items()},
                         "visible_after": {k: len(v) for k, v in after_ids.items()},
                         "identical": before_ids == after_ids,
                         "mutation_caught": mutated_ids != before_ids,
                         "sandbox_db": sandbox_path},
            "why_zero_behavior_change":
                "memory_service.search 的 _base 恒含 forgotten=0 + 排除 archival + "
                "COALESCE(superseded_by,0)=0；search_core 同样含 tier='core' AND forgotten=0。"
                "本批只动 forgotten=1 的行，故两路可见集合不变。",
        },
        "backup": str(bak), "backup_objects": n_dst,
        "tier_fix": [{"id": r["id"], "from": r["tier"], "to": "archival",
                      "forgotten": r["forgotten"]} for r in plan["tier_fix"]],
        "duplicate_groups": plan["groups"],
        "dup_fix": plan["dup_fix"],
        "rollback_sql": rollback,
        "not_done_p1_5": ["verify_state 294/294 unverifiable", "evidence_ref 全空",
                          "跨域可见记忆命中率量化", "记忆质量 A/B（需评估集）"],
        "caveat": "不删任何行；重复组仍物理存在，靠 superseded_by 区分主行/副本。"
                  "回滚优先用 backup 整库恢复（若之后又有其它写入），或逐行执行 rollback_sql。",
    }
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8", newline="\n")
    print("WROTE", OUT)
    print("RESULT: %d PASS / %d FAIL" % (len(PASS), len(FAIL)))
    if FAIL:
        print("FAILED:", *FAIL, sep="\n  - ")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
