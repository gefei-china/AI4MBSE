"""P0-3 一次性处置：online_eval_queue 的 46 条陈年 pending（2026-10-29 采样残留）。

实测口径（2026-11-15，mbse.db 只读）：46 条全 pending，
其中 generation 6 条有 message_id（可评）、intent 20 + retrieval 20 条无 message_id。
`core/online_eval.py:475` 已明确：intent/retrieval 样本**没有答案体**，
转正走 dataset 不需 auto_score —— 它们不是「漏评」，是设计上不可评。
⇒ 处置分两步，且都留痕、可单步回滚：
  ① 对 6 条可评样本补跑 `auto_evaluate_pending(apply=True)`（确定性指标，不调 LLM）；
  ② 全部 46 条显式驳回 `status='dismissed'` + 理由/操作者/时间，pending 清零。
     **不转 gold、不删行**（保留历史采样事实）。

默认 dry-run；--apply 才写库并落两份留痕文件（JSON 明细 + MD 说明，含回滚 SQL）。
写库前请确认已有全库备份（本批：backups/mbse.db.bak-before-p0-20261115_231655.db）。
"""
import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
DB = os.path.join(ROOT, "mbse.db")
OUT_JSON = os.path.join(ROOT, "docs", "评测", "online_eval_queue_p0_disposition-20261115.json")
OUT_MD = os.path.join(ROOT, "docs", "评测", "online_eval_queue_p0_disposition-20261115.md")

REASON = ("P0-3 显式驳回：2026-10-29 一次性采样残留，auto_evaluate_pending 从未被生产调用；"
          "可评样本已补跑确定性评分，无答案体的 intent/retrieval 样本按 core/online_eval.py:475 "
          "的设计不需 auto_score；不转 gold、不删行，留痕后清空 pending")
LABELLED_BY = "p0_eval_governance"


def _ro():
    c = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
    c.row_factory = sqlite3.Row
    return c


def snapshot(c):
    return {
        "pending": c.execute("SELECT COUNT(*) FROM online_eval_queue WHERE status='pending'").fetchone()[0],
        "dismissed": c.execute("SELECT COUNT(*) FROM online_eval_queue WHERE status='dismissed'").fetchone()[0],
        "auto_scored": c.execute("SELECT COUNT(*) FROM online_eval_queue WHERE auto_score IS NOT NULL").fetchone()[0],
        "rows": [dict(r) for r in c.execute(
            "SELECT id,sample_type,message_id,sample_reason,status,created_at "
            "FROM online_eval_queue WHERE status='pending' ORDER BY id")],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    c = _ro()
    try:
        before = snapshot(c)
    finally:
        c.close()
    print("BEFORE pending=%d dismissed=%d auto_scored=%d" %
          (before["pending"], before["dismissed"], before["auto_scored"]))
    if before["pending"] != 46:
        print("!! 期望 46 条 pending，实测 %d —— 口径已变，拒绝执行（需重新盘点）" % before["pending"])
        return 2
    ids = [r["id"] for r in before["rows"]]
    print("ids=%s" % ids)

    # dry-run 也跑一次确定性评分（只读、不写）
    from core.online_eval import auto_evaluate_pending
    c2 = sqlite3.connect(DB)
    c2.row_factory = sqlite3.Row  # core/online_eval.py:472 会 dict(row)，裸连接必炸
    try:
        dry = auto_evaluate_pending(c2, limit=50, apply=False)
    finally:
        c2.close()
    print("auto_evaluate dry-run: scanned=%d scored=%d failed=%d skipped_no_message=%d" %
          (dry["scanned"], dry["scored"], dry["failed"], dry["skipped_no_message"]))
    if dry["failed"]:
        print("!! 有评分失败，先查原因再处置：", dry["failures"][:3])
        return 2

    if not args.apply:
        print("DRY-RUN（未写库）；加 --apply 执行")
        return 0

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    try:
        applied = auto_evaluate_pending(conn, limit=50, apply=True)
        print("auto_evaluate apply: scored=%d failed=%d" % (applied["scored"], applied["failed"]))
        if applied["failed"]:
            conn.rollback()
            print("!! 评分失败，已回滚，不驳回")
            return 2
        conn.execute(
            "UPDATE online_eval_queue SET status='dismissed', human_reason=?, "
            "labeled_by=?, labeled_at=CURRENT_TIMESTAMP "
            "WHERE status='pending' AND id IN (%s)" % ",".join("?" * len(ids)),
            [REASON, LABELLED_BY] + ids)
        conn.commit()
        after = snapshot(conn)
        after["auto_scored"] = conn.execute(
            "SELECT COUNT(*) FROM online_eval_queue WHERE auto_score IS NOT NULL").fetchone()[0]
        # 自证：pending 清零、dismissed 恰为 46、6 条有分数、总行数不变
        assert after["pending"] == 0, after
        assert after["dismissed"] == 46, after
        assert after["auto_scored"] == 6, after
        assert len(after["rows"]) == 0, "仍有 pending 行"
        total = conn.execute("SELECT COUNT(*) FROM online_eval_queue").fetchone()[0]
        assert total == 46, total
        detail = [dict(r) for r in conn.execute(
            "SELECT id,sample_type,message_id,sample_reason,auto_score,status,"
            "labeled_by,labeled_at,created_at FROM online_eval_queue ORDER BY id")]
    finally:
        conn.close()
    print("AFTER pending=%d dismissed=%d auto_scored=%d total=46" %
          (after["pending"], after["dismissed"], after["auto_scored"]))

    payload = {
        "disposition": "P0-3 online_eval_queue pending 清零（显式驳回，非删除）",
        "at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "source_db": "mbse.db", "backup": "backups/mbse.db.bak-before-p0-20261115_231655.db",
        "before": {"pending": 46, "dismissed": 0, "auto_scored": 0},
        "auto_evaluate": {"scanned": applied["scanned"], "scored": applied["scored"],
                          "failed": applied["failed"], "skipped_no_message": applied["skipped_no_message"],
                          "note": "确定性指标，不调用 LLM"},
        "after": {"pending": 0, "dismissed": 46, "auto_scored": 6, "total": 46},
        "reason": REASON, "labelled_by": LABELLED_BY,
        "rollback_sql": ("UPDATE online_eval_queue SET status='pending', human_reason='', "
                         "labeled_by=NULL, labeled_at=NULL, auto_score=NULL, "
                         "auto_detail='{}', auto_eval_at=NULL WHERE id BETWEEN 1 AND 46;"),
        "rows": detail,
    }
    os.makedirs(os.path.dirname(OUT_JSON), exist_ok=True)
    with open(OUT_JSON, "w", encoding="utf-8", newline="\n") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")
    md = """# P0-3：online_eval_queue 陈年 pending 处置记录

**时点**：2026-11-15（库内 UTC）；**库**：`mbse.db`；**写库前备份**：`backups/mbse.db.bak-before-p0-20261115_231655.db`（表数 182 / 关键表行数逐项一致）

## 事实与判据

| 项 | 处置前 | 处置后 |
|---|---:|---:|
| `status='pending'` | 46 | **0** |
| `status='dismissed'` | 0 | **46** |
| `auto_score IS NOT NULL` | 0 | **6** |
| 总行数 | 46 | 46（**未删行**） |

46 条全部创建于 2026-10-29 02:35~02:41 UTC，此后 17 天无新增，`auto_eval_at` 全 NULL。
分布：`generation 6`（有 `message_id`）/ `intent 20` + `retrieval 20`（无 `message_id`）。

`core/online_eval.py:475` 已明确写出：intent/retrieval 样本**没有答案体**，转正走 dataset
不需 `auto_score`，跳过但不算失败 ⇒ **这 40 条不是「漏评」，是设计上不可评**。
真正空转的是 `auto_evaluate_pending` 在生产侧**从未以 apply=True 被调用**（唯一带 apply 的
`job_handlers.py:560` 是 `sample_candidates` 采样，不是评分）。

## 处置

1. 对 6 条可评样本补跑 `auto_evaluate_pending(apply=True)`：确定性指标，不调 LLM，
   `scanned=6 / scored=6 / failed=0 / skipped_no_message=40`。
2. 全部 46 条置 `status='dismissed'`，写 `human_reason` / `labeled_by=p0_eval_governance` /
   `labeled_at`。**不转 gold、不删行**（保留历史采样事实）。
3. 逐条明细见 `online_eval_queue_p0_disposition-20261115.json`。

## 回滚（单步）

```sql
UPDATE online_eval_queue SET status='pending', human_reason='', labeled_by=NULL,
       labeled_at=NULL, auto_score=NULL, auto_detail='{}', auto_eval_at=NULL
WHERE id BETWEEN 1 AND 46;
```

## 未做 / 局限

- 未把这 46 条转成 golden/标注集：它们是一次性采样，内容质量需人工复核，本批不替用户拍板。
- 未启用「定时自动评分」：生产侧采样与评分仍是两条独立触发链；本批只清历史欠账，
  新增样本由 `routers/monitor.py` 的评分动作或后续调度处理。
- 本次 `auto_score` 只代表确定性指标（引用/事实性公式），**不是**内容质量的人工标注。
"""
    with open(OUT_MD, "w", encoding="utf-8", newline="\n") as f:
        f.write(md)
    print("WROTE", OUT_JSON, OUT_MD)
    return 0


if __name__ == "__main__":
    sys.exit(main())
