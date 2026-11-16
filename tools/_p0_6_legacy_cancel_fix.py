"""P0-6 补充处置：把 3 条历史「用户中断 ⇒ internal」误分类归一为 interrupted。

这 3 条（id 1910/1946/1958，2026-11-10）在 P0-6 回填**之前**就带着 error_class='internal'，
不属于「空归因」集合，所以 `_p0_6_error_class_backfill.py` 没碰它们。
但按本批建立的 `classify_terminal` 语义，cancelled 一律 interrupted，
「用户中断」被兜底成 internal 是分类器对人为终止的误判（见 failure_class.py 模块头）。

⚠️ 不新增备份：复用回填脚本 apply 前生成的全库备份（路径从主留痕 JSON 读），
   并**先验证备份里这 3 条确实是 internal** ⇒ 回滚源有效，零额外 534 MB。

默认 dry-run；加 --apply 才写库。写后把结果追加进主留痕 JSON 的
`legacy_cancel_fix` 字段，并同步 after.dist（保持留痕与真库自洽）。
"""
import collections
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DB = REPO / "mbse.db"
LEAVE = REPO / "docs" / "评测" / "p0-6-error-class-backfill-20261115.json"
APPLY = "--apply" in sys.argv

sys.path.insert(0, str(REPO))
from services.failure_class import classify_terminal  # noqa: E402


def main():
    lv = json.loads(LEAVE.read_text(encoding="utf-8"))
    bak = REPO / lv["backup"]
    if not bak.is_file():
        print("!! 主留痕指向的备份不存在：%s" % bak)
        return 1

    c = sqlite3.connect("file:%s?mode=ro" % DB.as_posix(), uri=True)
    c.row_factory = sqlite3.Row
    rows = [dict(r) for r in c.execute(
        "SELECT id,status,error,error_class FROM agent_tasks "
        "WHERE status='cancelled' AND error_class!='interrupted' ORDER BY id")]
    before_dist = dict(c.execute(
        "SELECT error_class, COUNT(*) FROM agent_tasks "
        "WHERE status IN ('failed','cancelled') GROUP BY 1"))
    c.close()

    problems = []
    if len(rows) != 3:
        problems.append("目标行数变化：期望 3（2026-11-15 口径），实际 %d" % len(rows))
    for r in rows:
        if classify_terminal(r["status"], r["error"]) != "interrupted":
            problems.append("id=%s 按 classify_terminal 不是 interrupted，不擅自改" % r["id"])
        if "中断" not in (r["error"] or "") and "取消" not in (r["error"] or ""):
            problems.append("id=%s 的 error 不是人为终止文案，不擅自改：%r"
                            % (r["id"], r["error"]))
        if r["error_class"] != "internal":
            problems.append("id=%s 的旧值不是 internal，需人工复核" % r["id"])

    # 回滚源有效性：备份里这 3 条必须仍是 internal
    b = sqlite3.connect("file:%s?mode=ro" % bak.as_posix(), uri=True)
    b.row_factory = sqlite3.Row
    bak_vals = {r["id"]: r["error_class"] for r in b.execute(
        "SELECT id,error_class FROM agent_tasks WHERE id IN (%s)"
        % ",".join("?" * len(rows)), [r["id"] for r in rows])} if rows else {}
    b.close()
    if any(bak_vals.get(r["id"]) != "internal" for r in rows):
        problems.append("备份里这 3 条不全是 internal ⇒ 回滚源不可信：%s" % bak_vals)

    print("=== %s ===" % ("APPLY 判定" if APPLY else "DRY-RUN 判定"))
    print("目标行：", [(r["id"], r["error"], r["error_class"]) for r in rows])
    print("before_dist =", before_dist)
    print("备份 =", bak.name, "备份内旧值 =", bak_vals)
    for p in problems:
        print("  !!", p)
    if problems:
        print("RESULT: %d FAIL —— 不写库" % len(problems))
        return 1
    if not APPLY:
        print("DRY-RUN（未写库）；加 --apply 执行")
        return 0

    w = sqlite3.connect(str(DB))
    try:
        w.executemany("UPDATE agent_tasks SET error_class='interrupted' WHERE id=?",
                      [(r["id"],) for r in rows])
        w.commit()
        after_dist = dict(w.execute(
            "SELECT error_class, COUNT(*) FROM agent_tasks "
            "WHERE status IN ('failed','cancelled') GROUP BY 1"))
        left = w.execute(
            "SELECT COUNT(*) FROM agent_tasks WHERE status='cancelled' "
            "AND error_class!='interrupted'").fetchone()[0]
        if left:
            raise RuntimeError("写后仍有 %d 条 cancelled 非 interrupted" % left)
    except Exception:
        w.rollback()
        raise
    finally:
        w.close()

    lv["legacy_cancel_fix"] = {
        "applied_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "reason": "cancelled 被历史兜底成 internal；按 classify_terminal 语义归一",
        "backup_reused": lv["backup"],
        "rows": [{"id": r["id"], "error": r["error"],
                  "old": r["error_class"], "new": "interrupted"} for r in rows],
        "rollback_sql": ["UPDATE agent_tasks SET error_class='internal' WHERE id=%d;" % r["id"]
                         for r in rows],
        "note": "这 3 条不在 93 行空归因集合内；完整回滚 = 主 rollback_sql(93 条清空) + 本段(3 条还原)",
    }
    lv["after"]["dist"] = after_dist
    lv["notes"] = lv.get("notes", []) + [
        "2026-11-15 补充：3 条历史 cancelled/internal 已归一，真库 after.dist 以本文件为准",
    ]
    LEAVE.write_text(json.dumps(lv, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("AFTER dist =", after_dist)
    print("RESULT: APPLY OK；cancelled 全部 interrupted，留痕已同步")
    return 0


if __name__ == "__main__":
    sys.exit(main())
