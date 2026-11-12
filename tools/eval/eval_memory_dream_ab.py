# -*- coding: utf-8 -*-
"""Dreaming 双臂影子评测（2026-11-12）：把 `metrics()` 里两个 None 换成实测值。

## 为什么需要它
`core/memory_dream.metrics()` 有四项指标，但 `memory_hit_rate` /
`tokens_per_question` 一直是 `None` —— 因为没人跑过评测。
上一批交付时如实报了"未实测"，本工具把它补上，且**不臆造任何数字**。

## 双臂定义（影子评估，B 臂不落盘）
- **A 臂 baseline**：当前 `agent_memory.verify_state` 原样，`MemoryService.search`
  在固定评测集上算召回命中率与注入字符数。
- **B 臂 shadow**：在**同一连接的未提交事务**里，把本次 Dreaming 候选的状态
  写入（等价于"HIL 全部批准"），跑同一评测，然后 `ROLLBACK`。
  ⚠️ 直接 `UPDATE` 而**不调** `apply_candidates()`：后者内部会 `commit()`，
     会把影子状态写进真库、也让 rollback 失效。
  ⚠️ B 臂是 hypothetical（假设全批准），**不是已发生的事实**；报告里明确标注。

## 评测集来源
复用 `tools/eval/eval_memory_quality.py` 的 T/M/C/N 四组（2026-10-07 从生产库
真实记忆反推、用 id 锚定），不另造 query。噪音组 N 单独统计"漏放条数"。

## 用法
    .venv/Scripts/python.exe -X utf8 tools/eval/eval_memory_dream_ab.py \
        --db mbse.db --json-out data/memory_dream_ab.json
只读真库；除 --json-out 指定的报告外不写任何文件。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from copy import deepcopy

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)

DEFAULT_REPORT = os.path.join(ROOT, "data", "memory_dream_ab.json")
MAX_INJECT_CHARS = 200   # 与 context_audit.memory_max_chars 同一口径


def _cases():
    from tools.eval import eval_memory_quality as Q
    out = []
    for group, arr in (("T", Q.T_CASES), ("M", Q.M_CASES), ("C", Q.C_CASES)):
        for c in arr:
            out.append({"id": c["id"], "group": group, "q": c["q"],
                        "intent": c.get("intent") or "knowledge_qa",
                        "expect": list(c.get("expect") or [])})
    noise = [{"id": c["id"], "q": c["q"], "intent": c.get("intent") or "knowledge_qa",
              "forbid": list(c.get("forbid") or [])} for c in Q.N_CASES]
    return out, noise


def measure(conn, cases, noise) -> dict:
    """单臂测量：召回命中率 + 每问题注入字符数 + 噪音漏放数。"""
    from memory_service import MemoryService
    hits, total_expect, injected, per_case = 0, 0, 0, []
    for c in cases:
        exp = set(c["expect"])
        if not exp:
            continue
        try:
            rows = MemoryService.search(conn, c["intent"], c["q"], top_k=5)
        except Exception as exc:  # noqa: BLE001
            per_case.append({"id": c["id"], "error": type(exc).__name__})
            total_expect += len(exp)
            continue
        got = {int(r.get("id")) for r in rows if r.get("id") is not None}
        n_hit = len(exp & got)
        chars = sum(min(len(str(r.get("content") or "")), MAX_INJECT_CHARS)
                    for r in rows)
        hits += n_hit
        total_expect += len(exp)
        injected += chars
        per_case.append({"id": c["id"], "expect": sorted(exp), "got": sorted(got),
                         "hit": n_hit, "injected_chars": chars})
    noise_leaks = []
    for c in noise:
        try:
            rows = MemoryService.search(conn, c["intent"], c["q"], top_k=5)
        except Exception:
            continue
        got = {int(r.get("id")) for r in rows if r.get("id") is not None}
        leaks = sorted(got & set(c["forbid"]))
        if leaks:
            noise_leaks.append({"id": c["id"], "leaked_ids": leaks})
    n_q = max(1, len([c for c in cases if c["expect"]]))
    return {
        "memory_hit_rate": round(hits / max(1, total_expect), 4),
        "tokens_per_question": round(injected / n_q, 2),  # 本仓按字符计量，见下方 note
        "injected_chars_total": injected,
        "questions": n_q,
        "noise_leaks": noise_leaks,
        "per_case": per_case,
        "unit_note": "tokens_per_question 实为『平均每问题注入字符数』——"
                     "本仓注入预算按字符配置，未做 tokenize；不冒充 token 数",
    }


def build_shadow_states(conn, pkg) -> list:
    """把候选变成 (memory_id, state) 影子状态。只取 correct 类的合法状态。"""
    out = []
    for c in pkg.get("candidates") or []:
        if c.get("action") != "correct" or not c.get("memory_id"):
            continue
        st = c.get("state")
        if st in ("verified", "contradicted", "needs_review", "unverifiable"):
            out.append((int(c["memory_id"]), st))
    # 去重（同一 id 只留一条）
    return list(dict(out).items())


def run(db_path: str, note_limit: int = 300) -> dict:
    from core.memory_dream import dream, metrics
    conn = sqlite3.connect("file:%s?mode=ro" % db_path.replace("\\", "/"), uri=True)
    conn.row_factory = sqlite3.Row
    # mode=ro 不能开写事务 ⇒ 用可写连接但全程 rollback，绝不 commit
    conn.close()
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    cases, noise = _cases()
    out = {"schema": 1, "measured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
           "db": os.path.basename(db_path), "note_limit": note_limit,
           "cases": len(cases), "noise_cases": len(noise)}
    try:
        pkg = dream(conn, note_limit=note_limit)
        out["fingerprint"] = pkg["fingerprint"]
        out["stats"] = pkg["stats"]
        out["fact_conflict_rate"] = metrics(pkg)["fact_conflict_rate"]
        out["injected_index_chars"] = metrics(pkg)["injected_chars"]
        states = build_shadow_states(conn, pkg)
        out["shadow_states"] = len(states)
        out["shadow_contradicted"] = sum(1 for _, s in states if s == "contradicted")
        out["shadow_needs_review"] = sum(1 for _, s in states if s == "needs_review")

        out["arm_a_baseline"] = measure(conn, cases, noise)
        # ── B 臂：未提交事务 + 直接 UPDATE（不调 apply_candidates，避免 commit）──
        conn.execute("BEGIN")
        try:
            for mid, st in states:
                conn.execute("UPDATE agent_memory SET verify_state=? WHERE id=?", (st, mid))
            out["arm_b_shadow_hypothetical"] = measure(conn, cases, noise)
        finally:
            conn.rollback()
        # 复证 rollback 干净：再测一次 A，必须与首次一致
        out["arm_a_after_rollback"] = measure(conn, cases, noise)
        a = out["arm_a_baseline"]["memory_hit_rate"]
        a2 = out["arm_a_after_rollback"]["memory_hit_rate"]
        out["rollback_clean"] = (a == a2)
        b = out["arm_b_shadow_hypothetical"]["memory_hit_rate"]
        out["delta"] = {
            "memory_hit_rate": round(b - a, 4),
            "tokens_per_question": round(
                out["arm_b_shadow_hypothetical"]["tokens_per_question"] -
                out["arm_a_baseline"]["tokens_per_question"], 2),
        }
        out["interpretation"] = (
            "B 臂为假设 HIL 全部批准的影子结果，不落盘；"
            "差异来自『被证伪记忆不再进召回』。"
            "若 delta=0 属正常：当前没有或很少有 contradicted 强信号落库，"
            "说明机制安全但尚未产生可测收益，不得据此宣称『记忆变好了』。")
    finally:
        conn.close()
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(ROOT, "mbse.db"))
    ap.add_argument("--json-out", default=DEFAULT_REPORT)
    ap.add_argument("--note-limit", type=int, default=300)
    args = ap.parse_args()
    if not os.path.exists(args.db):
        print("db 不存在：%s" % args.db)
        return 2
    r = run(args.db, args.note_limit)
    os.makedirs(os.path.dirname(args.json_out), exist_ok=True)
    with open(args.json_out, "w", encoding="utf-8") as f:
        json.dump(r, f, ensure_ascii=False, indent=1)
    print("A 臂命中率 %s / 每问题注入 %s 字符" %
          (r["arm_a_baseline"]["memory_hit_rate"],
           r["arm_a_baseline"]["tokens_per_question"]))
    print("B 臂（影子，HIL 全批准假设）命中率 %s / 每问题注入 %s 字符" %
          (r["arm_b_shadow_hypothetical"]["memory_hit_rate"],
           r["arm_b_shadow_hypothetical"]["tokens_per_question"]))
    print("影子状态 %d 条（contradicted=%d / needs_review=%d）" %
          (r["shadow_states"], r["shadow_contradicted"], r["shadow_needs_review"]))
    print("delta = %s | rollback 干净 = %s" % (r["delta"], r["rollback_clean"]))
    print("报告 → %s" % args.json_out)
    return 0 if r["rollback_clean"] else 1


if __name__ == "__main__":
    sys.exit(main())
