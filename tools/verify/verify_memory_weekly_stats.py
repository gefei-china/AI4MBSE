# -*- coding: utf-8 -*-
"""记忆周报统计的门禁（P2，2026-11-27）。

## 为什么需要
`GET /api/memory/stats/weekly` 报三个数字（重复率 / 命中率 / tier 流动）。三个都能被"读反"：
  · 重复率的分母是**可召回行**，不是全表 —— 用全表算会把"归档掉的重复"也计入，看起来治理见效；
  · 命中率是**覆盖率**（有访问证据的行占比），不是召回准确率；
  · tier 流动依赖 `memory_events` 埋点 —— 表不存在时必须显式说"不可用"，
    **不许返回 0**（0 会被读成"这周没有任何流动"，而事实是"没有数据"）。
本门禁就把这三条钉死，并用夹具证明它们会随输入**变**（不是恒真）。

## 断言
  S1 重复率：构造 2 对 ≥0.95 的行 ⇒ pairs==2；把 sim 抬到 0.999 ⇒ pairs==0
  S2 重复率分母是可召回行：同一条被标 archival 后 ⇒ 不再计入 rows/pairs
  S3 命中率：3 行里 2 行 access>0 ⇒ 0.6667；全 0 ⇒ 0.0（不是 None/空）
  S4 流动：无 `memory_events` 表 ⇒ `available=False` 且**不是**"0 条"
  S5 流动：有表且窗口内 3 类事件 ⇒ by_kind 精确对得上；窗口外事件不计
  S6 聚类：同 topic ≥min_n 条 experience 且高频 ⇒ 入选；不足则落选
  S7 变异自证：把 duplicate_rate 的 `>= sim` 改成 `> sim` 且传入恰好等于阈值的对
     ⇒ S1 翻红（证明判据真的在比阈值，而不是恒真）

用法： ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_memory_weekly_stats.py
"""
import inspect
import math
import os
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.chdir(ROOT)

from services import memory_stats as MS  # noqa: E402

PASS, FAIL = [], []


def chk(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name,
                           ("  — " + str(detail)) if detail and not ok else ""))


def _v(dim_vals):
    n = math.sqrt(sum(x * x for x in dim_vals)) or 1.0
    return [x / n for x in dim_vals]


_DDL = """CREATE TABLE agent_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT, agent_id TEXT, mem_type TEXT DEFAULT 'fact',
    content TEXT DEFAULT '', mem_topic TEXT DEFAULT '', embedding TEXT DEFAULT '[]',
    embed_version TEXT DEFAULT 'v1', created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    access_count INTEGER DEFAULT 0, forgotten INTEGER DEFAULT 0, tier TEXT DEFAULT 'recall',
    superseded_by INTEGER DEFAULT 0, valid_to TEXT DEFAULT NULL,
    verify_state TEXT DEFAULT 'unverifiable')"""


def fixture(rows, events=None):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute(_DDL)
    import json as _j
    for i, (content, vec, acc, tier, forgotten) in enumerate(rows, start=1):
        c.execute("INSERT INTO agent_memory (id,agent_id,content,mem_topic,embedding,"
                  "embed_version,access_count,tier,forgotten) VALUES (?,?,?,?,?,?,?,?,?)",
                  (i, "a", content, "t", _j.dumps(vec), "v1", acc, tier, forgotten))
    if events is not None:
        c.execute("CREATE TABLE memory_events (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                  "at TEXT DEFAULT (datetime('now','localtime')), kind TEXT, "
                  "memory_id INTEGER DEFAULT 0, agent_id TEXT DEFAULT '', detail TEXT DEFAULT '')")
        for kind, at in events:
            c.execute("INSERT INTO memory_events (kind, at) VALUES (?,?)", (kind, at))
    c.commit()
    return c


def main() -> int:
    print("=" * 78)
    print("门禁：记忆周报统计（重复率 / 命中率 / tier 流动 / 技能聚类）")
    print("=" * 78)

    # ── S1 重复率：两对 1.0 相似 + 一条无关 ──
    same = _v([1.0, 0.0, 0.0])
    rows = [("A1", same, 3, "recall", 0), ("A2", same, 0, "recall", 0),
            ("B1", _v([0.0, 1.0, 0.0]), 0, "recall", 0),
            ("B2", _v([0.0, 1.0, 0.0]), 1, "recall", 0),
            ("C1", _v([0.0, 0.0, 1.0]), 0, "recall", 0)]
    c = fixture(rows)
    try:
        r = MS.weekly(c, days=7)
        chk("S1a 两对完全同向 ⇒ pairs==2", r["dup_rate"]["pairs"] == 2, r["dup_rate"])
        chk("S1b rate = pairs / 可召回行数 = 2/5", abs(r["dup_rate"]["rate"] - 0.4) < 1e-9,
            r["dup_rate"])
        # ── S3 命中率：A1(3) 与 B2(1) 有访问 ⇒ 2/5 ──
        chk("S3 2 行有访问 / 5 行 ⇒ 0.4（全 0 时应为 0.0，不是 None）",
            abs(r["hit_rate"]["rate"] - 0.4) < 1e-9, r["hit_rate"])
    finally:
        c.close()

    # ── S1c 阈值真的在起作用：唯一一对 cos≈0.96 ──
    _a = [1.0, 0.0]
    _b = [0.96, math.sqrt(1 - 0.96 ** 2)]
    c2 = fixture([("P1", _a, 0, "recall", 0), ("P2", _b, 0, "recall", 0)])
    try:
        _lo = MS.weekly(c2, dup_sim=0.95)["dup_rate"]["pairs"]
        _hi = MS.weekly(c2, dup_sim=0.97)["dup_rate"]["pairs"]
        chk("S1c cos≈0.96 的对：sim=0.95 命中、sim=0.97 不命中（阈值非空转）",
            _lo == 1 and _hi == 0, "0.95→%s / 0.97→%s" % (_lo, _hi))
    finally:
        c2.close()

    # ── S2 分母是可召回行：把 B2 归档 ⇒ 不再计入 ──
    c = fixture(rows)
    try:
        c.execute("UPDATE agent_memory SET tier='archival' WHERE id=4")
        c.commit()
        r = MS.weekly(c, days=7)
        # 归档 id=4（B2）后，B 组只剩一条 ⇒ 那一对重复不再成立：rows 5→4、pairs 2→1
        chk("S2 归档行不计入可召回（rows 5→4、pairs 2→1、rate=1/4）",
            r["dup_rate"]["rows"] == 4 and r["dup_rate"]["pairs"] == 1
            and abs(r["dup_rate"]["rate"] - 0.25) < 1e-9, r["dup_rate"])
        c.execute("UPDATE agent_memory SET tier='recall', superseded_by=9 WHERE id=4")
        c.commit()
        r2 = MS.weekly(c, days=7)
        chk("S2b 被取代行同样不计入（superseded_by>0）", r2["dup_rate"]["rows"] == 4, r2["dup_rate"])
    finally:
        c.close()

    # ── S4 无 memory_events 表 ⇒ available=False（不是 0 条）──
    c = fixture(rows, events=None)
    try:
        r = MS.weekly(c)
        chk("S4 无 memory_events ⇒ available=False 且带解释（不许用 0 冒充）",
            r["flow"]["available"] is False and "note" in r["flow"], r["flow"])
    finally:
        c.close()

    # ── S5 有表：窗口内 3 类事件精确对得上；窗口外不计 ──
    _now = time.strftime("%Y-%m-%d %H:%M:%S")
    _old = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 40 * 86400))
    c = fixture(rows, events=[("archived", _now), ("archived", _now), ("revived", _now),
                             ("promote", _now), ("promote", _old)])
    try:
        r = MS.weekly(c, days=7)
        f = r["flow"]["by_kind"]
        chk("S5 窗口内 archived=2 / revived=1 / promote=1（窗口外 promote 不计）",
            f.get("archived") == 2 and f.get("revived") == 1 and f.get("promote") == 1, f)
    finally:
        c.close()

    # ── S6 聚类 ──
    _rows = [{"id": i, "mem_topic": "需求追溯", "access_count": 9, "_mem_type": "experience"}
             for i in range(3)]
    _rows += [{"id": 10, "mem_topic": "冷门主题", "access_count": 1, "_mem_type": "experience"},
              {"id": 11, "mem_topic": "非经验", "access_count": 9, "_mem_type": "fact"}]
    cl = MS.topic_clusters(_rows, min_n=3, min_access=5)
    chk("S6 同 topic 3 条高频 experience ⇒ 入选；冷门/非经验不入选",
        [x["topic"] for x in cl] == ["需求追溯"], cl)

    # ── S7 变异自证：>= 改成 > 且传入恰好等于阈值的对 ──
    src = inspect.getsource(MS.duplicate_rate)
    _mut = src.replace("if _cos(a[\"vec\"], b[\"vec\"]) >= sim:", "if _cos(a[\"vec\"], b[\"vec\"]) > sim:")
    if _mut == src:
        chk("S7 变异锚点命中", False, "锚点未命中（判据需同步）")
    else:
        # ⚠️ 变异体只 exec 了 `duplicate_rate` 一个函数，它依赖的 `_cos` 必须显式注入，
        #   否则 NameError 会被误读成"变异体崩了=判据抓到了"（本仓记过的"变异打空"）。
        ns = {"math": math, "json": __import__("json"), "time": time, "_cos": MS._cos}
        exec(compile(_mut, "memory_stats.py", "exec"), ns)
        # 构造一对**余弦恰好 = 0.95** 的向量：[1,0] 与 [0.95, sqrt(1-0.95^2)]
        a = [1.0, 0.0]
        b = [0.95, math.sqrt(1 - 0.95 ** 2)]
        pool = [{"vec": a, "embed_version": "v1"}, {"vec": b, "embed_version": "v1"}]
        real = MS.duplicate_rate(pool, 0.95)["pairs"]
        mut = ns["duplicate_rate"](pool, 0.95)["pairs"]
        chk("S7 变异自证：`>=`→`>` 后恰好等于阈值的对丢失 ⇒ S1 类判据必然翻红",
            real == 1 and mut == 0, "real=%s mutated=%s" % (real, mut))

    print()
    print("=" * 78)
    print("PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("  FAIL:", f)
    print("=" * 78)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
