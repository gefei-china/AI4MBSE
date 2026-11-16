"""P0-6 一次性：把 93 条「失败/取消但无归因」的存量行**如实派生**补齐。

为什么不叫「回填伪造」：
  · 这 93 行的 `error` 文本**全部非空**（2026-11-15 全库实测 0 条空），
    是当时真实写入的观测；本脚本只把「已有文本 + status」按
    `services.failure_class.classify_terminal` 这条**现成、可复现**的规则转成结构化类别。
  · 不新增枚举、不猜原因、不给空文本硬塞 `internal`
    （`classify()` 对空文本会返回 internal ⇒ 那才是伪造观测）。
  · 留痕逐条记 id / old / derived / basis，并标 `method=offline_replay`
    —— 与运行时写入的归因**明确区分**。

⚠️ 与 `docs/遗留项S1S2S4S5实施记录-20261029.md:261`「历史存量不编造回填」的边界：
   那条纪律针对的是「`error` 为空 / 无法回溯」的行。本批目标集合里这类行 **0 条**，
   故不触犯；若有 1 条 error 为空，脚本立即中止且不写库（见断言 A3）。

默认 dry-run；加 `--apply` 才写库。写库前 `sqlite3.backup()` 全库备份。
跑法：./.venv/Scripts/python.exe -X utf8 tools/_p0_6_error_class_backfill.py [--apply]
"""
import collections
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DB = REPO / "mbse.db"
OUT = REPO / "docs" / "评测" / "p0-6-error-class-backfill-20261115.json"
BACKUP_DIR = REPO / "backups"
APPLY = "--apply" in sys.argv

sys.path.insert(0, str(REPO))
from services.failure_class import ERROR_CLASSES, classify_terminal  # noqa: E402


def snapshot(c):
    return {
        "objects": c.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type IN ('table','view')").fetchone()[0],
        "agent_tasks": c.execute("SELECT COUNT(*) FROM agent_tasks").fetchone()[0],
        "eval_reports": c.execute("SELECT COUNT(*) FROM eval_reports").fetchone()[0],
        "skills": c.execute("SELECT COUNT(*) FROM skills").fetchone()[0],
        "agents": c.execute("SELECT COUNT(*) FROM agents").fetchone()[0],
        "config_history": c.execute("SELECT COUNT(*) FROM config_history").fetchone()[0],
    }


def main():
    ro = sqlite3.connect("file:%s?mode=ro" % DB.as_posix(), uri=True)
    ro.row_factory = sqlite3.Row
    before = snapshot(ro)
    rows = [dict(r) for r in ro.execute(
        "SELECT id, status, error, error_class FROM agent_tasks "
        "WHERE status IN ('failed','cancelled') "
        "AND (error_class IS NULL OR error_class='') ORDER BY id")]
    dist_before = {k: v for k, v in ro.execute(
        "SELECT COALESCE(NULLIF(error_class,''),'(空)'), COUNT(*) FROM agent_tasks "
        "WHERE status IN ('failed','cancelled') GROUP BY 1")}
    total_terminal = ro.execute(
        "SELECT COUNT(*) FROM agent_tasks WHERE status IN ('failed','cancelled')").fetchone()[0]
    ro.close()

    problems = []
    # A1/A2：目标集合必须与方案口径一致（129 终态 / 93 空归因）——数字带口径与时点
    if total_terminal != 129:
        problems.append("终态任务数变化：期望 129（2026-11-15 口径），实际 %d" % total_terminal)
    if len(rows) != 93:
        problems.append("空归因行数变化：期望 93（2026-11-15 口径），实际 %d" % len(rows))
    # A3：**绝不编造** —— 任何 error 为空都中止
    empty_err = [r["id"] for r in rows if not (r["error"] or "").strip()]
    if empty_err:
        problems.append("存在 error 为空的行，无法如实派生 ⇒ 中止：%s" % empty_err[:10])

    plan = []
    for r in rows:
        cls = classify_terminal(r["status"], r["error"] or "")
        if cls not in ERROR_CLASSES:
            problems.append("id=%s 派生出非法类别 %r" % (r["id"], cls))
        if r["status"] == "cancelled":
            basis = "status=cancelled ⇒ interrupted（只由状态决定，不读文本）"
        else:
            basis = "classify(%r, ok=False) ⇒ %s" % ((r["error"] or "")[:60], cls)
        plan.append({"id": r["id"], "status": r["status"],
                     "error": r["error"], "old": r["error_class"] or "",
                     "derived": cls, "basis": basis})

    derived_dist = dict(collections.Counter(p["derived"] for p in plan))
    # A4：语义契约 —— cancelled 绝不能落到 internal
    bad_cancel = [p for p in plan if p["status"] == "cancelled" and p["derived"] != "interrupted"]
    if bad_cancel:
        problems.append("cancelled 被派生成非 interrupted：%s" % bad_cancel[:3])
    # A5：failed 的派生必须与独立重放一致（防两份实现漂移）
    from services.failure_class import classify as _raw
    mismatch = [p for p in plan if p["status"] == "failed"
                and p["derived"] != _raw(p["error"] or "", ok=False)]
    if mismatch:
        problems.append("failed 派生与 classify() 独立重放不一致：%s" % mismatch[:3])

    print("=== DRY-RUN 判定 ===" if not APPLY else "=== APPLY 判定 ===")
    print("终态任务 %d / 空归因 %d / 派生分布 %s" % (total_terminal, len(rows), derived_dist))
    print("空 error 行数 = %d（必须为 0）" % len(empty_err))
    print("before snapshot =", before, " | before dist =", dist_before)
    for p in problems:
        print("  !!", p)
    if problems:
        print("RESULT: %d FAIL —— 不写库" % len(problems))
        return 1
    print("判定：%d PASS / 0 FAIL" % (6,))

    if not APPLY:
        print("DRY-RUN（未写库）；加 --apply 执行")
        return 0

    # ── 写库前全库备份 ──
    BACKUP_DIR.mkdir(exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    bak = BACKUP_DIR / ("mbse.db.bak-before-p0-6-error-class-%s.db" % ts)
    src = sqlite3.connect("file:%s?mode=ro" % DB.as_posix(), uri=True)
    dst = sqlite3.connect(str(bak))
    src.backup(dst)
    dst.close()
    src.close()
    chk = sqlite3.connect("file:%s?mode=ro" % bak.as_posix(), uri=True)
    after_bak = snapshot(chk)
    chk.close()
    if after_bak != before:
        print("!! 备份与源库不一致，中止：%s vs %s" % (after_bak, before))
        return 1
    print("BACKUP %s (%d bytes) 表/行数逐项一致" % (bak.name, bak.stat().st_size))

    c = sqlite3.connect(str(DB))
    try:
        c.executemany("UPDATE agent_tasks SET error_class=? WHERE id=?",
                      [(p["derived"], p["id"]) for p in plan])
        c.commit()
        c.row_factory = sqlite3.Row
        left = c.execute(
            "SELECT COUNT(*) FROM agent_tasks WHERE status IN ('failed','cancelled') "
            "AND (error_class IS NULL OR error_class='')").fetchone()[0]
        dist_after = {k: v for k, v in c.execute(
            "SELECT error_class, COUNT(*) FROM agent_tasks "
            "WHERE status IN ('failed','cancelled') GROUP BY 1")}
        snap_after = snapshot(c)
        print("AFTER left_empty=%d dist=%s snapshot=%s" % (left, dist_after, snap_after))
        if left != 0:
            raise RuntimeError("写后仍有 %d 行无归因" % left)
        if snap_after != before:
            raise RuntimeError("写库改变了其它表行数：%s vs %s" % (snap_after, before))
        if sum(dist_after.values()) != total_terminal:
            raise RuntimeError("终态行数变化")
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()

    payload = {
        "schema": "p0/error-class-backfill@1",
        "applied_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "method": "offline_replay",
        "source": "agent_tasks.error（当时真实写入的文本） + status",
        "single_source": "services/failure_class.classify_terminal",
        "backup": str(bak.relative_to(REPO)).replace("\\", "/"),
        "scope": "全库单时点 2026-11-15；仅 status IN ('failed','cancelled') 且 error_class 为空的行",
        "counts": {
            "terminal_tasks": total_terminal,
            "empty_before": len(rows),
            "empty_after": 0,
            "nonempty_rate_before": round((total_terminal - len(rows)) / total_terminal, 4),
            "nonempty_rate_after": 1.0,
        },
        "before": {"dist": dist_before, "snapshot": before},
        "after": {"dist": dist_after, "snapshot": snap_after},
        "derived_dist": derived_dist,
        "checks": [
            "A1 终态数 129", "A2 空归因 93", "A3 error 为空 0 条（否则中止、绝不编造）",
            "A4 cancelled 全部 interrupted", "A5 failed 派生与 classify() 独立重放一致",
            "A6 派生值全部落在 ERROR_CLASSES 8 类内", "A7 写库前后其它表行数逐项一致",
            "A8 写后空归因 = 0",
        ],
        "rows": plan,
        "rollback_sql": ["UPDATE agent_tasks SET error_class='' WHERE id=%d;" % p["id"]
                         for p in plan],
        "caveat": (
            "离线派生归因 ≠ 运行时观测：类别完全由当时已落库的 error 文本与 status 复算得出，"
            "留痕可逐条复核、可单步回滚。不新增枚举、不改动 classify() 词表、不影响重试决策；"
            "结构性缺口（cancel/cancel_run/用户中断三个出口不写归因）已在本批代码侧修复。"
        ),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("LEAVE %s (%d bytes)" % (OUT.relative_to(REPO), OUT.stat().st_size))
    print("RESULT: APPLY OK —— 失败/取消任务 error_class 非空率 100.00%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
