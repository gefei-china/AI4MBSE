# -*- coding: utf-8 -*-
"""verify_memory_evidence_chain.py —— 记忆证据链必须真的通电。

## 真机事故（2026-11-16）

`agent_memory` 有完整的证据链字段，`services/memory_dream.py` 有完整的三态判定
（`_verify_claim`：否定优先两遍判定 + 数值冲突分级）。**但真机 295 条记忆
全部 `unverifiable`、`evidence_ref` 全空 —— 校验机制建好了没通电。**

查清不是能力缺失：`dream()` 扫 80 条就产出 84 个候选、75 条带 `evidence_ref`
（如 `messages#5057`）。缺的是**没有 apply 入口** —— `apply_candidates` 要求
`hil_approved=True`，而全仓没有调用方。

## 本门禁锁什么

A 组（能力非空转，只读）：
  A1 `dream()` 能产出 correct 候选（≥1）—— 证明校验链路真的在工作
  A2 候选带 evidence_ref（格式 `kind#ref_id`）
  A3 `dream()` **只读**：调用前后 agent_memory 的 content/verify_state sha 一致

B 组（落库安全性，用副本）：
  B1 不带 `hil_approved` ⇒ 一个字节都不写（HIL_REQUIRED）
  B2 落库只改 verify_state/evidence_ref/evidence_note/source_credibility
     （content/tier/forgotten/access_count 的 sha 必须一致）
  B3 默认分级不含 contradicted（它会被召回侧排除 ⇒ 误判即丢真记忆）
  B4 变异自证：把状态过滤放开到 all 时 B3 判据必须翻红

C 组（接线）：
  C1 落地工具存在且默认 dry-run
  C2 `--apply` 必须带 `--by`（留痕纪律）

用法： ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_memory_evidence_chain.py
退出码：0 全绿（无库时 A/B 组 SKIP）／1 有失败
"""
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
DB = os.environ.get("MBSE_GATE_DB") or os.environ.get("MBSE_DB_PATH") or os.path.join(ROOT, "mbse.db")
TOOL = os.path.join(ROOT, "tools", "memory_dream_apply.py")

#: 落库允许改的列（其余列必须一字不动）
#: ★ 2026-11-27（P0-1）：`tier` / `superseded_by` / `superseded_at` / `valid_to` /
#:   `archived_at` 也属**允许改的状态列**，因为 apply_candidates 现在真的会落
#:   merge（superseded_by）/ promote·demote·revive（tier）—— 这正是"候选包四分之三动作
#:   此前是死的"那条缺陷的修复。它们仍属"状态"，不是"内容"。
ALLOWED_COLS = {"verify_state", "evidence_ref", "evidence_note", "source_credibility",
                "tier", "superseded_by", "superseded_at", "archived_at", "valid_to"}
#: 落库前后必须逐字节一致的列（影子语义的**核心**：不改内容、不删行）
#: ⚠️ `forgotten` 从本组移出（2026-11-27）：revive 的语义就是"冷层回流"，
#:   对存量 `forgotten=1` 的归档行必须置 0 —— 否则"复活"名不副实。
#:   代价是这一组不再覆盖生命周期列，故**另设 B2b**：行数必须不变（不许删行），
#:   且 `content` 必须一字不动（真正不可逆的东西只有它）。
FROZEN_COLS = ("content", "mem_type", "access_count")

_PASS, _FAIL, _SKIP = [], [], []


def chk(name, ok, detail=""):
    (_PASS if ok else _FAIL).append(name)
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name,
                           ("　— " + str(detail)) if detail and not ok else ""))


def skip(name, why):
    _SKIP.append(name)
    print("  [SKIP] %s :: %s" % (name, why))


def frozen_hash(conn):
    """对'不该动的列'算 sha256。"""
    rows = conn.execute(
        "select id, %s from agent_memory order by id" % ",".join(FROZEN_COLS)).fetchall()
    raw = json.dumps([tuple(r) for r in rows], ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode()).hexdigest(), len(rows)


def verify_cols_hash(conn):
    """对'可动列'算 sha256（用于证明 dream 只读）。"""
    rows = conn.execute(
        "select id, content, verify_state, coalesce(source_credibility,'') "
        "from agent_memory order by id").fetchall()
    raw = json.dumps([tuple(r) for r in rows], ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def group_a(conn):
    from core.memory_dream import dream
    h_before = verify_cols_hash(conn)
    pkg = dream(conn, note_limit=30)
    h_after = verify_cols_hash(conn)

    chk("A3 dream() 只读（content/verify_state/credibility 的 sha 前后一致）",
        h_before == h_after, "%s vs %s" % (h_before[:12], h_after[:12]))

    cs = [c for c in (pkg.get("candidates") or []) if c.get("action") == "correct"]
    chk("A1 dream() 能产出 correct 候选（证明校验链路在工作，不是空转）",
        len(cs) >= 1, "correct=%d" % len(cs))
    if not cs:
        skip("A2 候选带 evidence_ref", "无 correct 候选")
        return
    import re
    pat = re.compile(r"^[A-Za-z_]+#\d+$")
    bad = [c.get("evidence_ref") for c in cs if not pat.match(str(c.get("evidence_ref") or ""))]
    chk("A2 候选带 evidence_ref 且格式为 kind#ref_id",
        not bad, "不合格式: %s" % bad[:5])
    # 附带：状态分布（信息项，不设阈值——真实分布由数据决定）
    dist = {}
    for c in cs:
        dist[c.get("state")] = dist.get(c.get("state"), 0) + 1
    print("      [info] correct 状态分布: %s" % json.dumps(dist, ensure_ascii=False))


def group_b():
    """用副本验证落库安全性。"""
    if not os.path.exists(DB):
        for n in ("B1", "B2", "B3", "B4"):
            skip(n, "干净库：跳过落库安全性组")
        return
    td = tempfile.mkdtemp(prefix="gate_evidence_")
    dst = os.path.join(td, "copy.db")
    try:
        shutil.copy2(DB, dst)
        for suf in ("-wal", "-shm"):
            if os.path.exists(DB + suf):
                shutil.copy2(DB + suf, dst + suf)
        conn = sqlite3.connect(dst)
        conn.row_factory = sqlite3.Row
        from core.memory_dream import dream, apply_candidates

        # B1：不带 hil_approved ⇒ 零写入
        h0, h0_rows = frozen_hash(conn)
        pkg = dream(conn, note_limit=30)
        res = apply_candidates(conn, pkg, hil_approved=False)
        h1, _ = frozen_hash(conn)
        chk("B1 不带 hil_approved ⇒ 拒绝且零写入（HIL_REQUIRED）",
            res.get("ok") is False and res.get("reason") == "HIL_REQUIRED" and h0 == h1,
            "ok=%s reason=%s" % (res.get("ok"), res.get("reason")))

        # B2：带批准 ⇒ 只改允许的列
        res2 = apply_candidates(conn, pkg, hil_approved=True, approved_by="gate")
        h2, n2 = frozen_hash(conn)
        chk("B2 落库只改状态列（content/mem_type/access_count 的 sha 必须一致）",
            h2 == h0, "%s vs %s（applied=%s）" % (h0[:12], h2[:12], res2.get("applied")))
        # B2b：**行数不变**（P0-1 的 merge 用 superseded_by 而非 forgotten=1，
        #      更不许 DELETE —— 这是"影子语义可回退"的最后一道）
        chk("B2b 落库不删任何行（merge 用 superseded_by，不用软删/硬删）",
            n2 == h0_rows, "%s vs %s" % (n2, h0_rows))

        # B3/B4：分级过滤必须**真的在挡** contradicted（用构造包直接测，不靠"本次包内恰好没有"）
        #
        # ⚠️ 初版 B3 断言 `dist.get("contradicted",0)==0`，但本次候选包里本来就没有
        #   contradicted ⇒ 这条永远绿，等于没测。B4 也只检查了元组拼装。
        #   ⇒ 改成用**构造包**直接调 filter_by_states()，验证它真的会把
        #     contradicted 挡在默认分级之外（这才是有牙齿的判据）。
        from tools.memory_dream_apply import (LOW_RISK_STATES, ALL_STATES,
                                             normalize_states, filter_by_states)

        fake_pkg = {"candidates": [
            {"action": "correct", "memory_id": 1, "state": "verified"},
            {"action": "correct", "memory_id": 2, "state": "needs_review"},
            {"action": "correct", "memory_id": 3, "state": "contradicted"},
            {"action": "create", "memory_id": None, "state": None},
        ]}
        kept, dropped = filter_by_states(fake_pkg, normalize_states(",".join(LOW_RISK_STATES)))
        kept_states = {c.get("state") for c in kept["candidates"]
                       if c.get("action") == "correct"}
        chk("B3 默认分级过滤掉 contradicted，且保留 verified/needs_review",
            "contradicted" not in kept_states
            and {"verified", "needs_review"} <= kept_states
            and dropped == 1,
            "kept=%s dropped=%s" % (sorted(x for x in kept_states if x), dropped))
        chk("B3b 非 correct 动作（create）不被状态过滤丢弃",
            any(c.get("action") == "create" for c in kept["candidates"]), "")

        # B4 变异自证：把 contradicted 加进分级 ⇒ 它就会漏进来（证明 B3 的"挡"是真效果）
        kept2, dropped2 = filter_by_states(
            fake_pkg, normalize_states(",".join(tuple(LOW_RISK_STATES) + ("contradicted",))))
        kept2_states = {c.get("state") for c in kept2["candidates"]
                        if c.get("action") == "correct"}
        chk("B4 变异自证：分级加入 contradicted ⇒ 它漏进来 ⇒ B3 判据必然翻红",
            "contradicted" in kept2_states and dropped2 == 0,
            "kept=%s dropped=%s" % (sorted(x for x in kept2_states if x), dropped2))

        # B5：`--states all` 必须等价于 ALL_STATES（含 contradicted，需显式点名）
        chk("B5 --states all ⇒ 展开为 ALL_STATES（含 contradicted）",
            normalize_states("all") == set(ALL_STATES)
            and "contradicted" in normalize_states("all"), ALL_STATES)
        conn.close()
    finally:
        shutil.rmtree(td, ignore_errors=True)


def group_c():
    chk("C1 落地工具存在", os.path.isfile(TOOL), TOOL)
    if not os.path.isfile(TOOL):
        return
    src = open(TOOL, encoding="utf-8").read()
    chk("C1b 工具默认 dry-run（--apply 才写库）",
        'action="store_true"' in src and "if not args.apply" in src, "")
    chk("C2 --apply 必须带 --by（留痕纪律）",
        "--by" in src and "必须带 --by" in src, "")
    chk("C2b 工具按状态分级（LOW_RISK_STATES 存在）",
        "LOW_RISK_STATES" in src, "")


def main():
    print("=" * 78)
    print("门禁：记忆证据链通电（dream 能产出 + 落库只改状态 + 分级不误伤召回）")
    print("=" * 78)

    print("\n--- GROUP_A ---")
    if not os.path.exists(DB):
        for n in ("A1", "A2", "A3"):
            skip(n, "干净库")
    else:
        conn = sqlite3.connect(DB)
        conn.row_factory = sqlite3.Row
        try:
            group_a(conn)
        finally:
            conn.close()

    print("\n--- GROUP_B ---")
    group_b()

    print("\n--- GROUP_C ---")
    group_c()

    print("\n" + "=" * 78)
    print("结果：PASS %d / FAIL %d / SKIP %d" % (len(_PASS), len(_FAIL), len(_SKIP)))
    for n in _FAIL:
        print("  - " + n)
    print("=" * 78)
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
