# -*- coding: utf-8 -*-
"""清理**已过期**的 pending HIL 确认单（统一置为 rejected）。

语义依据（不是"随便关掉"）
------------------------
`hil_confirmations.timeout_at` 是确认单的**有效窗口**：超时后该写操作已失去上下文
（会话/编排早已结束，模型当时的定位与版本可能全部失效）。
此时**批准执行**才是错的 —— 会在几小时后突然写入一个过期模型/文件，且无人预期。

`HILService.decide(approve=False)` 正是官方语义：`status='rejected'` + 记决策人与时间，
**不执行任何写操作**。因此本脚本只调它，**不裸写 SQL**（并发保护/乐观锁/审计字段都走既有实现）。

用法
----
    # 预览（默认，不写库）
    ./.venv/Scripts/python.exe tools/cleanup_expired_hil.py

    # 执行
    ./.venv/Scripts/python.exe tools/cleanup_expired_hil.py --apply

安全设计
--------
  · 只处理 `status='pending'` **且** `timeout_at` 非空**且** 已早于当前时间；
    未过期的一条都不碰（`--apply` 也会重新算一遍，不用缓存名单）；
  · 执行前把受影响行的完整快照写到 `outputs/hil_expired_cleanup_<ts>.json`，
    便于追溯"清掉了什么"（不可逆操作必须留证）；
  · `decided_by` 统一记为 `system:expired_cleanup`，与人工决策可区分。
"""
import argparse
import json
import os
import sys
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from database import get_db                      # noqa: E402
from hil_service import HILService               # noqa: E402

DECIDED_BY = "system:expired_cleanup"

_SQL = ("SELECT * FROM hil_confirmations WHERE status='pending' "
        "AND timeout_at!='' AND timeout_at < datetime('now') ORDER BY id")


def main() -> int:
    ap = argparse.ArgumentParser(description="清理过期 HIL 确认单（置为 rejected）")
    ap.add_argument("--apply", action="store_true", help="真正写库（默认只预览）")
    ap.add_argument("--limit", type=int, default=0, help="最多处理几条（0=不限）")
    args = ap.parse_args()

    conn = get_db()
    try:
        rows = [dict(r) for r in conn.execute(_SQL)]
        if args.limit and args.limit > 0:
            rows = rows[:args.limit]

        print("=" * 70)
        print("过期 HIL 确认单清理  mode=%s" % ("APPLY（写库）" if args.apply else "预览"))
        print("=" * 70)
        print("待处理：%d 条（status=pending 且 timeout_at < now）\n" % len(rows))
        for d in rows:
            print("  #%-3d %-26s %-20s conv=%-6s 建=%s 超时=%s" % (
                d["id"], str(d.get("agent_id") or "-")[:26],
                str(d.get("action") or "-")[:20],
                d.get("conversation_id"), d.get("created_at"), d.get("timeout_at")))

        if not rows:
            print("\n没有需要清理的确认单。")
            return 0

        # 留证：把将被改动的行**完整**存档（不可逆操作必须可追溯）
        if args.apply:
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            outdir = os.path.join(_ROOT, "outputs")
            os.makedirs(outdir, exist_ok=True)
            p = os.path.join(outdir, "hil_expired_cleanup_%s.json" % ts)
            with open(p, "w", encoding="utf-8", newline="\n") as f:
                json.dump({"ts": ts, "decided_by": DECIDED_BY, "count": len(rows),
                           "rows": rows}, f, ensure_ascii=False, indent=1)
            print("\n清理前快照已存档：%s" % p)

        ok = 0
        failed = []
        for d in rows:
            if not args.apply:
                continue
            try:
                r = HILService.decide(conn, int(d["id"]), approve=False,
                                      decided_by=DECIDED_BY)
                st = r.get("status")
                if st == "rejected":
                    ok += 1
                elif st in ("approved", "rejected"):
                    pass        # 并发：别人已决策 ⇒ 不重复计
                else:
                    failed.append((d["id"], st))
            except Exception as e:
                failed.append((d["id"], "%s: %s" % (type(e).__name__, str(e)[:60])))

        if args.apply:
            left = conn.execute(
                "SELECT COUNT(*) FROM hil_confirmations WHERE status='pending' "
                "AND timeout_at!='' AND timeout_at < datetime('now')").fetchone()[0]
            print("\n结果：成功置为 rejected %d 条 / 失败 %d 条 / 仍残留过期 %d 条"
                  % (ok, len(failed), left))
            for cid, why in failed:
                print("  ✗ #%s  %s" % (cid, why))
            print("\n⚠️ 这些确认单涉及的写操作**没有执行**（这是正确行为：")
            print("   超时 1~4 天的写操作若被执行，会在无人预期时写入过期模型/文件）。")
            return 0 if (left == 0 and not failed) else 1
        else:
            print("\n（预览模式：未写库。加 --apply 才执行）")
            return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
