# -*- coding: utf-8 -*-
"""编排评审评分/建议沉淀为 report 产物门禁（2026-11-18，G08 根因闭合）。

真机证据：G08 live 回放 conv 999999203 的 t2「打分评估+优化建议」done、
card_data.reflection.score=90、advice 非空，但 artifacts(kind=report)=0
⇒ golden_e2e G08 判据 artifact_kind_present(report) 不可达（11/12）。
根因不是判据错，而是产品把评审结果只写进 messages.card_data、从未物化。

修法：`review_report_signal(cd)` 纯函数识别「有编排 + 有实质评分/建议」，
`_archive_artifacts` 据此补走 report 归档；`orchestration.archive_review_report`
开关关掉 = 逐字节回到改动前。本门禁含变异自证，判据非空转。

用法：``python tools/verify/verify_review_report_archive.py``
退出码：0=全绿 / 1=有失败
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

from agent.utils import review_report_signal  # noqa: E402
from core import config as _cfg  # noqa: E402

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
    print("\nA. 纯函数：只认「编排 + 实质评分/建议」")
    sig = {"orchestrated_status": "partial",
           "reflection": {"enabled": True, "score": 90, "advice": "补充证据索引",
                          "rounds": 3, "issues": ["i1"], "criteria": "c"}}
    r = review_report_signal(sig)
    check("A1 命中编排评分/建议", isinstance(r, dict) and r["score"] == 90, str(r))
    check("A2 advice/rounds/issues/criteria 一并沉淀",
          r and r["advice"] == "补充证据索引" and r["rounds"] == 3
          and r["issues"] == ["i1"] and r["criteria"] == "c", str(r))
    check("A3 单 Agent（无编排）不误产 report",
          review_report_signal({"reflection": {"score": 90, "advice": "x"}}) is None)
    check("A4 无 reflection 不误产", review_report_signal({"orchestrated": True}) is None)
    check("A5 空评分/空建议不误产",
          review_report_signal({"orchestrated": True,
                                "reflection": {"score": None, "advice": "  "}}) is None)
    check("A6 禁用 reflection 不误产",
          review_report_signal({"orchestrated": True,
                                "reflection": {"enabled": False, "score": 90}}) is None)
    check("A7 score=0 也算实质结果（不是 falsy 漏判）",
          (review_report_signal({"orchestrated": True,
                                 "reflection": {"score": 0}}) or {}).get("score") == 0)
    check("A8 非 dict 入参不抛异常", review_report_signal("junk") is None)


def case_wiring() -> None:
    print("\nB. 代码接线 + 配置闸")
    import io
    s = io.open(os.path.join(REPO, "agent", "utils.py"), encoding="utf-8").read()
    check("B1 ★ _archive_artifacts 消费信号",
          "review_report_signal(cd)" in s and "if _review:" in s, "未接线")
    check("B2 评分/建议写进 report meta", 'report_meta["review"] = _review' in s)
    check("B3 默认开（config DEFAULT）",
          _cfg.get("orchestration", "archive_review_report", False) is True)
    cfg_src = io.open(os.path.join(REPO, "core", "config.py"), encoding="utf-8").read()
    check("B4 SCHEMA 已登记（可被设置页关闭）",
          cfg_src.count('"archive_review_report"') >= 2)
    check("B5 关闭闸即回退（纯函数仍可用、接线侧读配置）",
          "get(\"orchestration\", \"archive_review_report\", True)" in s)


def case_real_db() -> None:
    print("\nC. 真库根因证据（只读）")
    db = os.environ.get("MBSE_DB_PATH") or os.path.join(REPO, "mbse.db")
    if not os.path.exists(db):
        check("C1 生产库存在", False, db)
        return
    c = sqlite3.connect("file:%s?mode=ro" % db.replace("\\", "/"), uri=True)
    c.row_factory = sqlite3.Row
    try:
        row = c.execute("SELECT card_data FROM messages WHERE conversation_id=999999203 "
                        "AND role='assistant' ORDER BY id DESC LIMIT 1").fetchone()
        if not row:
            check("C1 live G08 会话存在", False, "conv 999999203 未找到")
            return
        cd = json.loads(row["card_data"] or "{}")
        refl = cd.get("reflection") or {}
        nreport = c.execute("SELECT COUNT(*) FROM artifacts WHERE conversation_id=999999203 "
                            "AND kind='report'").fetchone()[0]
        print("    live G08: score=%s advice=%s report产物=%d"
              % (refl.get("score"), bool(str(refl.get("advice") or "").strip()), nreport))
        check("C1 ★ 修复前现场：评分/建议已在 card 里、report 产物为 0",
              refl.get("score") is not None and nreport == 0,
              "score=%s nreport=%d" % (refl.get("score"), nreport))
        check("C2 ★ 修复后同一份 card 能被识别为应归档 report",
              isinstance(review_report_signal(cd), dict), "信号未命中")
        # 系统性：带评分/建议但无 report 的会话数（证明不是单例）
        rows = c.execute("""
            SELECT m.conversation_id,
                   (SELECT COUNT(*) FROM artifacts a WHERE a.conversation_id=m.conversation_id
                     AND a.kind='report') nr
            FROM messages m WHERE m.role='assistant' AND m.card_data LIKE '%reflection%'
        """).fetchall()
        gap = sum(1 for r in rows if r["nr"] == 0)
        print("    带 reflection 的 assistant 消息=%d，其中所在会话 report 产物=0 的有 %d 条"
              % (len(rows), gap))
        check("C3 缺口是系统性（≥10 条），不是 G08 单例", gap >= 10, "gap=%d" % gap)
    finally:
        c.close()


def case_mutation() -> None:
    print("\nD. 变异自证（判据有牙齿）")
    # 变异：把「必须编排」条件去掉 —— A3 必须翻红
    import inspect
    import textwrap
    src = textwrap.dedent(inspect.getsource(review_report_signal))
    mutated = src.replace(
        '    if not (cd.get("orchestrated") or cd.get("orchestrated_status")):',
        '    if False:', 1)
    ns = {}
    exec(compile(mutated.replace("review_report_signal", "_mut"), "<mut>", "exec"), ns)
    fn = ns["_mut"]
    check("M1 去掉编排门槛 ⇒ 单 Agent 被误判为应归档（A3 有牙齿）",
          fn({"reflection": {"score": 90, "advice": "x"}}) is not None)
    # 变异：把 score/advice 空判据去掉 —— A5 必须翻红
    mutated2 = src.replace(
        '    if score in (None, "") and not advice:', '    if False:', 1)
    ns2 = {}
    exec(compile(mutated2.replace("review_report_signal", "_mut2"), "<mut2>", "exec"), ns2)
    fn2 = ns2["_mut2"]
    check("M2 去掉实质判据 ⇒ 空 reflection 被误判（A5 有牙齿）",
          fn2({"orchestrated": True, "reflection": {"score": None, "advice": " "}}) is not None)


def main() -> int:
    print("编排评审评分/建议归档门禁（G08 根因闭合）")
    print("=" * 66)
    case_pure()
    case_wiring()
    case_real_db()
    case_mutation()
    print("\n" + "=" * 66)
    if FAILS:
        print("结论：%d/%d 通过，%d 条失败：" % (N - len(FAILS), N, len(FAILS)))
        for f in FAILS:
            print("  - %s" % f)
        return 1
    print("结论：全部通过（%d/%d）" % (N, N))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
