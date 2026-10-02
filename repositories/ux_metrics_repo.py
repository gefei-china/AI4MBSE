# -*- coding: utf-8 -*-
"""UX 埋点仓储（2026-10-03）：ux_metrics 事件表 + 六指标聚合（评估规范 §11）。

## 为什么要有它（产品口径）
UX 交互规范 v2.0 §11 的核心论点：**规范只能逐条核对，回答不了"做到什么程度、
这个迭代有没有变好"** —— 需要可观测指标。本模块是最小数据面：
前端行为事件（任务完成/失败、主动中断、澄清回答、滚动抢夺守护）批量落库，
summary 端点聚合出规范 §11 定义的比率（任务完成率 / 中断率 / 澄清命中率 /
滚动抢夺恒 0 守护）。

## 指标口径（与规范 §11 对齐）
- 任务完成率   = task_done / (task_done + task_error)        目标 ≥ 85%
- 主动中断率   = interrupt / (task_done + task_error)        15–25% 健康（≈0% 没人细看，>40% 方向常跑偏）
- 澄清命中率   = clarify_answered / clarify_shown            目标 ≥ 90%（skipped 不算命中）
- 滚动抢夺     = scroll_force 中 reason ∉ {'user','clarify'}  恒为 0（守护指标：目前不存在该路径，
               一旦 >0 说明有人在锁定态抢了滚动条 —— 规范 §8.4 红线）
- confirm_shown/confirm_decided：HIL 确认闸门曝光/处置（附赠，观察内联确认转化）

## 纪律
- **遥测绝不能影响主链路**：前端批量 + 静默丢弃；后端写端点不校验权限（与
  intent_samples 的设置页写端点同纪律），但事件名白名单 + 单批上限，防灌水。
- 无用户维度：与 agent_memory 同款已知缺口（多用户部署前补 user 列）。
"""
from collections import Counter

#: 事件名白名单 —— 拼 SQL 前必须校验（防任意字符串进库/进聚合键）
EVENTS = ("task_done", "task_error", "interrupt",
          "clarify_shown", "clarify_answered", "clarify_skipped",
          "confirm_shown", "confirm_decided",
          "scroll_force")

#: 滚动强制的合法 reason（§8.4：用户显式动作 / 行动单元出现），其余一律计抢夺
_SCROLL_LEGAL_REASONS = ("user", "clarify")


class UxMetricsRepo:
    def __init__(self, conn):
        self.conn = conn
        self._ensure()

    def _ensure(self):
        """幂等建表（低价值高频写入不值得进 schema 迁移链，repo 内自持）。"""
        self.conn.execute("""CREATE TABLE IF NOT EXISTS ux_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event TEXT NOT NULL,
            conversation_id INTEGER DEFAULT 0,
            detail TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now','localtime'))
        )""")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_uxm_event_time ON ux_metrics(event, created_at)")

    # ── 写入 ──
    def insert_batch(self, events) -> int:
        """批量插入（单批上限 50，白名单外静默丢弃）。"""
        rows = []
        for ev in events[:50]:
            if not isinstance(ev, dict):
                continue
            name = str(ev.get("event") or "")
            if name not in EVENTS:
                continue
            try:
                cid = int(ev.get("conversation_id") or 0)
            except Exception:
                cid = 0
            rows.append((name, cid, str(ev.get("detail") or "")[:500]))
        if not rows:
            return 0
        self.conn.executemany(
            "INSERT INTO ux_metrics(event, conversation_id, detail) VALUES (?,?,?)", rows)
        return len(rows)

    # ── 聚合 ──
    def summary(self, days: int = 7) -> dict:
        days = max(1, min(int(days or 7), 90))
        cnt = Counter()
        for r in self.conn.execute(
                "SELECT event, COUNT(*) n FROM ux_metrics "
                "WHERE created_at >= datetime('now', ?) GROUP BY event",
                (f'-{days} days',)):
            cnt[r[0]] = r[1]

        done, err = cnt.get("task_done", 0), cnt.get("task_error", 0)
        task_total = done + err
        interrupt = cnt.get("interrupt", 0)
        shown, answered, skipped = (cnt.get("clarify_shown", 0),
                                    cnt.get("clarify_answered", 0),
                                    cnt.get("clarify_skipped", 0))
        # 滚动抢夺守护：reason 合法性在 detail JSON 里，SQL 端全量取、Python 端判
        steal = 0
        for (detail,) in self.conn.execute(
                "SELECT detail FROM ux_metrics WHERE event='scroll_force' "
                "AND created_at >= datetime('now', ?)", (f'-{days} days',)):
            import json as _j
            try:
                if (  _j.loads(detail or "{}").get("reason") not in _SCROLL_LEGAL_REASONS):
                    steal += 1
            except Exception:
                steal += 1   # detail 解析不了按可疑计

        def _rate(a, b):
            return round(a / b, 4) if b else None

        return {
            "days": days,
            "tasks": {"done": done, "error": err, "total": task_total,
                      "completion_rate": _rate(done, task_total)},
            "interrupt": {"count": interrupt, "rate": _rate(interrupt, task_total)},
            "clarify": {"shown": shown, "answered": answered, "skipped": skipped,
                        "hit_rate": _rate(answered, shown)},
            "confirm_gate": {"shown": cnt.get("confirm_shown", 0),
                             "decided": cnt.get("confirm_decided", 0)},
            "scroll_steal": {"count": steal, "legal": 0 if steal == 0 else "检查 scroll_force 的 reason"},
        }
