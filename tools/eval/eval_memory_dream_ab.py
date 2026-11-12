# -*- coding: utf-8 -*-
"""Dreaming 双臂影子评测（2026-11-12）：把 `metrics()` 里两个 None 换成实测值。

## 为什么需要它
`core/memory_dream.metrics()` 有四项指标，但 `memory_hit_rate` /
`tokens_per_question` 一直是 `None` —— 因为没人跑过评测。
上一批交付时如实报了"未实测"，本工具把它补上，且**不臆造任何数字**。

## 双臂定义（影子评估，B 臂不落盘）
- **A 臂 baseline**：当前 `agent_memory.verify_state` 原样，`MemoryService.search`
  在固定评测集上算召回命中率与注入字符数。
- **B 臂 shadow**：在**数据库副本**里把本次 Dreaming 候选的状态写入
  （等价于"HIL 全部批准"），跑同一评测；副本用完即弃，真库始终只读。
  ⚠️ B 臂是 hypothetical（假设全批准），**不是已发生的事实**；报告里明确标注。

## ⚠️ 2026-11-12 实测事故：原实现污染了真库，且 rollback_clean 是假绿
原实现"真库直连 + BEGIN → UPDATE verify_state → measure → ROLLBACK"。
`measure()` 会调 `MemoryService.search`，而 `search` 命中后调
`record_access()`，后者内部 `conn.commit()` **把显式 BEGIN 的事务提交了**
（受控实验：`in_transaction` 由 True 变 False）⇒ 后续 `rollback()` 是空操作，
173 条影子状态全部落进真库。

更隐蔽的是 `rollback_clean` 判据：它只比较 A 臂前后 `memory_hit_rate`，
而 `verify_state` **不参与召回排序** ⇒ 无论污染多严重，命中率恒不变 ⇒ 恒绿。
**代理变量对真实缺陷完全不敏感**，这正是"验证器自己错了"的典型形态。

修法（两层，缺一不可）：
  ① 整个评测在 `sqlite3.backup()` 出来的**副本**上跑，真库连接为 `mode=ro`，
     任何写都会抛异常；`measure()` 的 `record_access` 写也只落在副本里。
     B 臂仍直接 `UPDATE` 而**不调** `apply_candidates()`（后者内部也会 commit）。
  ② `db_untouched` 用**真库持久状态列的前后 sha256** 直接判"有没有写脏"，
     不再用命中率当代理；副本内的 `rollback_clean` 降为辅助信息。

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
import hashlib
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


def _ro_uri(db_path: str) -> str:
    return "file:%s?mode=ro" % db_path.replace("\\", "/")


def live_fingerprint(db_path: str) -> str:
    """真库**持久状态**的确定性指纹（只读连接）。

    只取我们绝不该被评测改动的列：`verify_state` / `evidence_ref` /
    `evidence_note`。刻意**不含** `access_count` / `last_accessed_at`
    —— 那两列是运行中的服务正常读写的使用统计，纳入会让判据假红；
    它们的写入由「副本隔离」负责，不靠指纹。
    """
    h = hashlib.sha256()
    conn = sqlite3.connect(_ro_uri(db_path), uri=True, timeout=30)
    try:
        for row in conn.execute(
                "SELECT id, COALESCE(verify_state,'unverifiable'), "
                "COALESCE(evidence_ref,''), COALESCE(evidence_note,'') "
                "FROM agent_memory ORDER BY id"):
            h.update(repr(tuple(row)).encode("utf-8"))
            h.update(b"\n")
    finally:
        conn.close()
    return h.hexdigest()


def _work_copy(db_path: str):
    """用 SQLite backup 造完整副本（含 FTS 虚表），返回 (连接, 路径)。

    `mode=ro` 源连接保证真库零写入；副本放在 `_tmpdir` 登记的临时目录里，
    由 atexit 回收（不手写批量删除，避开 safe-delete 钩子）。
    """
    sys.path.insert(0, os.path.join(ROOT, "tools", "verify"))
    from _tmpdir import mkdtemp  # noqa: WPS433  复用仓库统一的临时目录回收
    snap = os.path.join(mkdtemp(prefix="wb_eval_"), "snapshot.db")
    src = sqlite3.connect(_ro_uri(db_path), uri=True, timeout=30)
    work = sqlite3.connect(snap, timeout=30)
    try:
        src.backup(work)
    finally:
        src.close()
    work.row_factory = sqlite3.Row
    return work, snap


def run(db_path: str, note_limit: int = 300) -> dict:
    from core.memory_dream import dream, metrics
    out = {"schema": 2, "measured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
           "db": os.path.basename(db_path), "note_limit": note_limit}
    # ★ 主判据：评测前后真库持久状态必须逐字一致。直接测"有没有写脏"，
    #   不再用 memory_hit_rate 当代理（旧版因此恒绿，见文件头事故说明）。
    fp_before = live_fingerprint(db_path)
    out["db_fingerprint_before"] = fp_before
    conn, snap_path = _work_copy(db_path)
    out["snapshot_only"] = True
    out["work_db"] = "<sqlite backup copy, discarded>"
    cases, noise = _cases()
    out["cases"] = len(cases)
    out["noise_cases"] = len(noise)
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
        # ── B 臂：在**副本**里开未提交事务；即便 record_access 的 commit
        #    把事务提交了，写脏的也只是随即丢弃的副本，真库始终 mode=ro。──
        conn.execute("BEGIN")
        try:
            for mid, st in states:
                conn.execute("UPDATE agent_memory SET verify_state=? WHERE id=?", (st, mid))
            out["arm_b_shadow_hypothetical"] = measure(conn, cases, noise)
        finally:
            conn.rollback()
        out["arm_a_after_rollback"] = measure(conn, cases, noise)
        a = out["arm_a_baseline"]["memory_hit_rate"]
        a2 = out["arm_a_after_rollback"]["memory_hit_rate"]
        out["rollback_clean"] = (a == a2)   # 副本内的辅助信息，非落盘判据
        b = out["arm_b_shadow_hypothetical"]["memory_hit_rate"]
        out["delta"] = {
            "memory_hit_rate": round(b - a, 4),
            "tokens_per_question": round(
                out["arm_b_shadow_hypothetical"]["tokens_per_question"] -
                out["arm_a_baseline"]["tokens_per_question"], 2),
        }
        out["interpretation"] = (
            "B 臂为假设 HIL 全部批准的影子结果，只在数据库副本里发生、不落盘；"
            "差异来自『被证伪记忆不再进召回』。"
            "若 delta=0 属正常：当前没有或很少有 contradicted 强信号落库，"
            "说明机制安全但尚未产生可测收益，不得据此宣称『记忆变好了』。")
    finally:
        conn.close()
        out["db_fingerprint_after"] = live_fingerprint(db_path)
        out["db_untouched"] = (out["db_fingerprint_after"] == fp_before)
        out["db_fingerprint"] = out["db_fingerprint_after"]
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
    print("delta = %s | 真库零写入 = %s（副本内 rollback 干净 = %s）" %
          (r["delta"], r["db_untouched"], r["rollback_clean"]))
    print("报告 → %s" % args.json_out)
    # 主判据换成 db_untouched：rollback_clean 只反映副本内部一致性，
    # 用它当退出码就是上一轮「假绿」的原样复发。
    return 0 if r["db_untouched"] else 1


if __name__ == "__main__":
    sys.exit(main())
