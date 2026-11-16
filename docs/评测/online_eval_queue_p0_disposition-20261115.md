# P0-3：online_eval_queue 陈年 pending 处置记录

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
