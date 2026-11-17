# -*- coding: utf-8 -*-
"""live 报告不得套用 offline 限制说明门禁（2026-11-17）。

真机事故：36 次真实 LLM 调用跑出的合格 live 基线（eval_reports.id=34），
报告里 `limitations[0]` 仍写着「offline_replay 不重新调用 LLM：
结果是历史交付物的回放基线」—— 直接让读者误判这份基线的性质。
根因：`golden_e2e` 无论 mode 都套同一个 `LIMITATIONS` 常量
（本仓"同一语义两处各写一份迟早错位"的同族：一处按 mode 分支、一处不分支）。

断言：
  A. 纯函数 limitations_for：live 与 offline 各说各话
  B. 报告生成路径确实调用它
  C. 真库至少有一份合格 live 报告的限制说明不含 "offline_replay"

用法：``python tools/verify/verify_golden_live_limitations.py``
退出码：0=全绿 / 1=有失败
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tools", "eval"))

import golden_e2e as G  # noqa: E402

FAILS: list = []
N = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global N
    N += 1
    if cond:
        print("  [OK]   %s" % name)
    else:
        print("  [FAIL] %s%s" % (name, ("  —— " + detail) if detail else ""))
        FAILS.append(name)


def case_pure() -> None:
    print("\nA. 纯函数：live / offline 各说各话")
    live = G.limitations_for("live")
    off = G.limitations_for("offline_replay")
    check("A1 live 限制说明不含 offline_replay",
          not any("offline_replay" in x for x in live), str(live[:1]))
    check("A2 live 明说按 conv-map 评新建会话",
          any("新建会话" in x and "conv-map" in x for x in live), str(live[:1]))
    check("A3 live 说明采样波动（不是假装确定）",
          any("采样" in x for x in live), str(live[:1]))
    check("A4 offline 仍保留原说明（不破坏旧报告口径）",
          any("offline_replay" in x for x in off), str(off[:1]))
    check("A5 两条路径共享其余 3 条限制（不各写一份）",
          list(live[1:]) == list(off[1:]) and len(live) == len(off) == 4,
          "live=%d off=%d" % (len(live), len(off)))

    src = io.open(os.path.join(REPO, "tools", "eval", "golden_e2e.py"),
                  encoding="utf-8").read()
    check("A6 报告生成路径调用 limitations_for(mode)",
          '"limitations": limitations_for(mode)' in src, "报告仍套静态 LIMITATIONS")


def case_db() -> None:
    print("\nB. 真库：至少一份合格 live 报告的说法正确")
    db = os.environ.get("MBSE_DB_PATH") or os.path.join(REPO, "mbse.db")
    if not os.path.exists(db):
        check("B0 生产库存在", False, db)
        return
    c = sqlite3.connect("file:%s?mode=ro" % db.replace("\\", "/"), uri=True)
    try:
        rows = c.execute("SELECT id, detail FROM eval_reports WHERE eval_kind='golden_e2e' "
                         "AND is_golden=1 ORDER BY id DESC").fetchall()
        live_rows = []
        for rid, detail in rows:
            try:
                d = json.loads(detail or "{}")
            except Exception:
                continue
            if d.get("mode") == "live":
                live_rows.append((rid, d.get("limitations") or []))
        print("    合格 live 报告：%s（共 %d 份）"
              % ([rid for rid, _ in live_rows], len(live_rows)))
        check("B1 存在 live 报告", bool(live_rows), "无")
        good = [rid for rid, lim in live_rows
                if not any("offline_replay" in str(x) for x in lim)]
        check("B2 ★ 至少一份 live 报告未误用 offline 限制说明", bool(good),
              "误用份数=%d" % (len(live_rows) - len(good)))
    finally:
        c.close()


def main() -> int:
    print("live 报告限制说明门禁")
    print("=" * 64)
    case_pure()
    case_db()
    print("\n" + "=" * 64)
    if FAILS:
        print("结论：%d/%d 通过，%d 条失败：" % (N - len(FAILS), N, len(FAILS)))
        for f in FAILS:
            print("  - %s" % f)
        return 1
    print("结论：全部通过（%d/%d）" % (N, N))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
