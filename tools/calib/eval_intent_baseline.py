#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""意图识别基线/回归评测 —— 用真库 intent_samples（期望答案非空、非 rejected）。

## 为什么必须先跑基线
意图识别改造（B4-5：规则给候选 + LLM 确认/否决）会改变**路由结果的产生方式**，
而 route 分布本身就是判据（`rule` 直返 vs `rule_confirmed`）。
⇒ 改造前后必须跑**同一批样本、同一口径**，否则"没改坏"这句话没有证据。

## 口径
· 样本：`intent_samples` 里 `intent` 非空且 `status != 'rejected'`
  （rejected = 已被人工否掉的样本，不该当标准答案）
· 生产库**只读**
· 输出 JSON：ok/n/准确率/route 分布/错例明细（供逐条 diff）

## 用法
    python tools/calib/eval_intent_baseline.py --out tmp/b4/intent_baseline.json
    python tools/calib/eval_intent_baseline.py --out tmp/b4/intent_after.json
"""
import argparse
import collections
import json
import os
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)


def load_samples(prod_db):
    c = sqlite3.connect("file:%s?mode=ro" % prod_db, uri=True)
    c.row_factory = sqlite3.Row
    rows = c.execute(
        "SELECT id, text, intent FROM intent_samples "
        "WHERE intent IS NOT NULL AND intent!='' AND status!='rejected' "
        "ORDER BY id"
    ).fetchall()
    c.close()
    return [{"id": r["id"], "text": r["text"], "expect": r["intent"]} for r in rows]


def run(samples, prod_db, use_pipeline=True):
    """⚠️ `use_pipeline=True`（默认，且必须）——**按产品的真实构造方式**建路由器。

    ## 为什么不能直接 `IntentRouter()`
    首版我就是 `IntentRouter()` 直接 new 的，拿到 **46.31%** 的"基线"，
    而线上真实路由远不止于此。根因（实测）：
      · `_db_intents` 空  → 关键词层无任何 db 信号（真实路径由
        `AgentPipeline.__init__` 的 `registry.load_from_db` + `register_keywords` 填充）
      · `_semantic_index` 空 → 语义层直接跳过（真实路径由
        `pipeline.py` 的 `SELECT ... FROM agents WHERE status='active'` 填充）
      ⇒ **三层判据里有两层压根没参与**，剩下的 LLM 档又因网络失败落 chat
      ⇒ 那个 46% 反映的是"评测脚本构造错了"，**不是产品能力**。

    这与 MEMORY 里「门禁验的是另一个库」同族：**判据环境 ≠ 生产环境**时，
    得出的数字方向可能对、量级完全错。
    """
    from database import get_db
    from agent.pipeline import AgentPipeline
    if use_pipeline:
        pipe = AgentPipeline()          # ★ 走真实构造
        # ★★ 还必须调 `_load_db_agents()` —— 它**不在 `__init__` 里**，而是
        #    「每次执行前」调（pipeline.py:53，注释原文如此）。
        #    不调 ⇒ `register_keywords` 从未执行 ⇒ `_db_intents` 空
        #      + `set_semantic_index` 从未执行 ⇒ **语义索引 0 条**。
        #    （实测：漏这一步时脚本打印「语义索引 0 条 / db 关键词意图 0 个」，
        #      而线上服务是有的 —— 又一次"判据环境 ≠ 生产环境"。）
        try:
            pipe._load_db_agents()
        except Exception as e:                   # noqa: BLE001
            print("  ⚠️ _load_db_agents 失败：%r" % e)
        r = pipe.router
    else:
        from agent.intent import IntentRouter
        r = IntentRouter()
    conn = sqlite3.connect("file:%s?mode=ro" % prod_db, uri=True)
    conn.row_factory = sqlite3.Row
    n_idx = len(getattr(r, "_semantic_index", []) or [])
    n_kw = len(getattr(r, "_db_intents", {}) or {})
    print("  构造：%s | 语义索引 %d 条 / db 关键词意图 %d 个"
          % ("AgentPipeline（真实路径）" if use_pipeline else "裸 IntentRouter（口径错）",
             n_idx, n_kw), flush=True)
    ok = 0
    bad = []
    routes = collections.Counter()
    slow = []
    for i, s in enumerate(samples, 1):
        t0 = time.time()
        try:
            got = r.detect(s["text"], conn=conn)
        except Exception as e:                      # noqa: BLE001
            got = "EXC:%s" % type(e).__name__
        dt = time.time() - t0
        if dt > 2.0:
            slow.append((s["text"][:30], round(dt, 1)))
        m = r._last_meta or {}
        routes[m.get("route") or "(none)"] += 1
        if got == s["expect"]:
            ok += 1
        else:
            bad.append({"text": s["text"], "expect": s["expect"],
                        "got": got, "route": m.get("route")})
        if i % 25 == 0:
            print("  …%d/%d（当前 %d 对）" % (i, len(samples), ok), flush=True)
    conn.close()
    return {"ok": ok, "n": len(samples),
            "accuracy": round(100.0 * ok / max(1, len(samples)), 2),
            "routes": dict(routes), "bad": bad, "slow": slow,
            "ctor": "pipeline" if use_pipeline else "bare",
            "n_semantic_index": n_idx, "n_db_intents": n_kw}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="tmp/b4/intent_eval.json")
    ap.add_argument("--db", default=os.path.join(ROOT, "mbse.db"))
    ap.add_argument("--bare", action="store_true",
                    help="用裸 IntentRouter（**口径错**，仅用于对照演示）")
    a = ap.parse_args()
    samples = load_samples(a.db)
    print("评测样本：%d 条" % len(samples), flush=True)
    res = run(samples, a.db, use_pipeline=not a.bare)
    print("\n★★ 准确率 %d/%d = %.2f%%" % (res["ok"], res["n"], res["accuracy"]))
    print("\nroute 分布：")
    for k, v in sorted(res["routes"].items(), key=lambda x: -x[1]):
        print("   %-20s %d" % (k, v))
    print("\n错例 %d 条（前 15）：" % len(res["bad"]))
    for b in res["bad"][:15]:
        print("   %-38s 期望=%-18s 实得=%-18s %s"
              % (b["text"][:36], b["expect"], b["got"], b["route"]))
    if res["slow"]:
        print("\n慢样本（>2s，走 LLM）%d 条：%s" % (len(res["slow"]), res["slow"][:5]))
    os.makedirs(os.path.dirname(os.path.join(ROOT, a.out)), exist_ok=True)
    p = os.path.join(ROOT, a.out) if not os.path.isabs(a.out) else a.out
    json.dump(res, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("\n已写 %s" % p)
    return 0


if __name__ == "__main__":
    sys.exit(main())