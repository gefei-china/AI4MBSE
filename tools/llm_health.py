# -*- coding: utf-8 -*-
"""LLM 调用健康度巡检 CLI（P0-a / P0-b，2026-10-02）。

用法：
    python tools/llm_health.py                 # 近 7 天全量
    python tools/llm_health.py --days 30
    python tools/llm_health.py --problems-only # 只看非 ok 的 intent
    python tools/llm_health.py --json          # 机器可读（供定时任务/前端消费）

退出码：`alert`（含契约违规）→ 1；其余 → 0。可直接挂到定时巡检/CI 上当判据。
口径实现全部在 `core/llm_health.py`（本文件只做取数 + 渲染，**不重复写判据**）。
"""
import argparse
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
os.chdir(ROOT)


def main() -> int:
    ap = argparse.ArgumentParser(description="LLM 调用健康度巡检")
    ap.add_argument("--days", type=int, default=7, help="统计窗口天数（默认 7）")
    ap.add_argument("--problems-only", action="store_true", help="只显示非 ok 的 intent")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    from core.llm_health import format_report, health_report, to_json
    from database import get_db

    conn = get_db()
    try:
        rep = health_report(conn, days=args.days)
    finally:
        try:
            conn.close()
        except Exception:
            pass

    if args.json:
        print(to_json(rep))
    else:
        print(format_report(rep, only_problem=args.problems_only))
    return 1 if rep["status"] == "alert" else 0


if __name__ == "__main__":
    raise SystemExit(main())
