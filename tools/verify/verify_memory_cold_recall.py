# -*- coding: utf-8 -*-
# CI-OPTIONAL: 需要真库且库内已存在冷层行（tier=archival）。冷层是数据状态、不是代码不变量 ——干净库上本脚本无物可验，进 CI 只会得到假绿。开发机 21/21 通过。
"""冷层（tier=archival & forgotten=0）召回验证（2026-11-27）。

## 为什么单独验一遍
"冷层可检索"是 P1-6 解耦买到的**唯一新能力**。它必须同时满足两条**方向相反**的要求：
  ① **不污染**：默认召回（真实注入的 top_k=4）里**不能**出现冷层；
  ② **可检索**：`include_archival=True` 时冷层**必须**回到候选池，按需捞得回来。

## ⚠️ 判据设计上的一个坑（第一版就踩了，写在这里防重犯）
第一版用「top30 里有没有冷层」判 ②，结果全红 —— 但那是**判据错**，不是功能坏：
冷层分数被**时间衰减**压到 0.4 下限（`decay = max(0.4, 1 - age/max_age_days)`），
它们都是老条目 ⇒ 与一堆同分活跃条挤在一起，top30 自然而然排不进去。
实测（真库）：冷层余弦 0.82~0.92，但**融合分排名**是 104/42/6/1/25/2/1 —— 位置根本不稳定。
⇒ 判 ② 必须用**候选池**（top_k 足够大，覆盖全部可召回行），不能用排名位置。
   排名位置属"降优先级"这一语义，另立判据（A5b），不要与"可检索"混为一谈。

## 分区
**A 区：真库只读。** 不写一个字节。
**B 区：副本可写。** 降层↔复活、归档前后对比（真库已被污染过一次，这轮不再冒险）。
**C 区：HTTP 层。** 验管理端能回捞冷层（"可检索"要落到人能用）。

用法： ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_memory_cold_recall.py
"""
import os
import shutil
import sqlite3
import sys
import tempfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.chdir(ROOT)

DB = os.path.join(ROOT, "mbse.db")
BASE = "http://127.0.0.1:8000"
POOL = 600          # 候选池上限：远大于可召回行数（实测 263），确保"池=全部"

PASS, FAIL, SKIP = [], [], []


def chk(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name,
                           ("  — " + str(detail)) if detail and not ok else ""))


def skip(name, why):
    SKIP.append(name)
    print("  [SKIP] %s  — %s" % (name, why))


def _conn_ro(path):
    c = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True)
    c.row_factory = sqlite3.Row
    return c


def cold_rows(conn):
    return [dict(r) for r in conn.execute(
        "SELECT id, access_count, archived_at, last_accessed_at, mem_topic, "
        "substr(content,1,40) AS head, tier, forgotten FROM agent_memory "
        "WHERE tier='archival' AND COALESCE(forgotten,0)=0 ORDER BY id")]


def main() -> int:
    if not os.path.exists(DB):
        print("无 mbse.db，退出")
        return 0
    from services.memory_service import MemoryService as MS
    from agent.memory_recall import recall_memories

    print("=" * 78)
    print("冷层召回验证（真库只读 + 副本可写）")
    print("=" * 78)

    # ══════════ A 区：真库只读 ══════════
    print("\n[A] 真库只读")
    conn = _conn_ro(DB)
    try:
        cold = cold_rows(conn)
        cold_ids = [r["id"] for r in cold]
        print("      冷层 %d 条: %s" % (len(cold), cold_ids))
        for r in cold:
            print("        id=%-4s access=%-2s archived=%s %s"
                  % (r["id"], r["access_count"], (r["archived_at"] or "")[:16], r["head"]))
        chk("A0 冷层非空（否则后续判据全部空转）", bool(cold), len(cold))
        if not cold:
            return 1

        from core import config as _cfg
        _max_acc = int(_cfg.get("memory", "forget_max_access", 2))
        bad = [r["id"] for r in cold
               if int(r["forgotten"] or 0) != 0 or int(r["access_count"] or 0) > _max_acc]
        chk("A1 冷层判据自洽（forgotten=0 且 access≤%d）" % _max_acc, not bad, bad)

        probe_q = "TBD 占位 定量指标 未会签 参数单位口径 工程列表查询 建包草稿版本 SysML导出"

        # ── ① 不污染：真实注入规模（top_k=30，实际注入 4）里不能有冷层 ──
        hot = MS.search(conn, "", probe_q, top_k=30, any_scope=True)
        leaked = sorted({h["id"] for h in hot} & set(cold_ids))
        chk("A2 默认召回 top30（真实注入 4）不含冷层", not leaked, leaked)
        chk("A2b 判据非空转（默认召回本身有返回）", len(hot) > 0, len(hot))

        # ── ② 可检索：用**候选池**判，不看排名位置 ──
        pool_off = {h["id"] for h in MS.search(conn, "", probe_q, top_k=POOL, any_scope=True)}
        pool_on = {h["id"] for h in MS.search(conn, "", probe_q, top_k=POOL, any_scope=True,
                                              include_archival=True)}
        chk("A3 池子开关生效：include_archival 使候选池变大",
            len(pool_on) > len(pool_off), "%d → %d" % (len(pool_off), len(pool_on)))
        chk("A3b 默认池里没有冷层", not (pool_off & set(cold_ids)),
            sorted(pool_off & set(cold_ids)))
        chk("A3c include_archival 池里冷层全部在场（可检索）",
            set(cold_ids) <= pool_on, sorted(set(cold_ids) - pool_on))
        print("      候选池 %d → %d 条（冷层 %d 条全部进入）"
              % (len(pool_off), len(pool_on), len(cold)))

        # ── ③ 端到端链路（chat 真实入口）不含冷层 ──
        e2e = recall_memories(conn, probe_q, agent_id="knowledge_qa", scopes=None)
        e2e_ids = {(h.get("meta") or {}).get("id") for h in e2e}
        e2e_leak = sorted({i for i in e2e_ids if i} & set(cold_ids))
        chk("A4 端到端 recall_memories（chat 入口）不含冷层", not e2e_leak, e2e_leak)
        print("      端到端召回 %d 条: %s"
              % (len(e2e), [(h.get('meta') or {}).get('id') for h in e2e]))

        # ── ④ 每条冷层"以自己的全文为 query"都进池（最硬的可检索判据） ──
        missed = []
        for r in cold:
            full = str(conn.execute("SELECT content FROM agent_memory WHERE id=?",
                                    (r["id"],)).fetchone()[0])
            ids = {h["id"] for h in MS.search(conn, "", full, top_k=POOL, any_scope=True,
                                              include_archival=True)}
            if r["id"] not in ids:
                missed.append(r["id"])
        chk("A5 每条冷层用自身全文作 query 都进 include_archival 池",
            not missed, missed)

        # ── ⑤ 降优先级（与"可检索"是两件事，必须分开断言）──
        #    冷层都是老条目 ⇒ 被时间衰减压到 0.4 下限 ⇒ 即便可检索也排在活跃条之后。
        #    这条不是缺陷，是方案 P1-6 要的"冷层可检索、降优先级"。
        r0 = cold[0]
        full0 = str(conn.execute("SELECT content FROM agent_memory WHERE id=?",
                                 (r0["id"],)).fetchone()[0])
        ranked = MS.search(conn, "", full0, top_k=POOL, any_scope=True, include_archival=True)
        rank = next((i + 1 for i, h in enumerate(ranked) if h["id"] == r0["id"]), None)
        chk("A5b 冷层在池内但排名靠后（降优先级生效，不是被提权）",
            rank is not None and rank > 10,
            "id=%s 排名=%s / 共 %d" % (r0["id"], rank, len(ranked)))

        # ── ⑥ 冷层与 core 无交集（不会被无条件注入）──
        core_ids = {r[0] for r in conn.execute(
            "SELECT id FROM agent_memory WHERE tier='core' AND COALESCE(forgotten,0)=0")}
        chk("A6 冷层与 core 无交集（不会被无条件注入）",
            not (core_ids & set(cold_ids)), sorted(core_ids & set(cold_ids)))
    finally:
        conn.close()

    # ══════════ B 区：副本可写 ══════════
    print("\n[B] 副本可写（降层↔复活、口径隔离）")
    td = tempfile.mkdtemp(prefix="cold_recall_")
    copy_db = os.path.join(td, "copy.db")
    try:
        s = sqlite3.connect(DB)
        d = sqlite3.connect(copy_db)
        s.backup(d)
        d.close()
        s.close()
        c = sqlite3.connect(copy_db)
        c.row_factory = sqlite3.Row
        try:
            victim = c.execute(
                "SELECT id, content, access_count FROM agent_memory WHERE tier='recall' "
                "AND COALESCE(forgotten,0)=0 AND COALESCE(superseded_by,0)=0 "
                "ORDER BY access_count DESC LIMIT 1").fetchone()
            vid = int(victim["id"])
            vfull = str(victim["content"])      # 用全文作 query（短 query 会引入无关排名噪声）
            print("      victim id=%s（access=%s）" % (vid, victim["access_count"]))

            def in_pool(include_archival):
                out = MS.search(c, "", vfull, top_k=POOL, any_scope=True,
                                include_archival=include_archival)
                return {h["id"] for h in out}, {h["id"]: h["score"] for h in out}

            before, _ = in_pool(False)
            chk("B1 降层前该条在默认池里（前置条件，否则 B2 空转）", vid in before)

            c.execute("UPDATE agent_memory SET tier='archival', "
                      "archived_at=datetime('now','localtime') WHERE id=?", (vid,))
            c.commit()
            off, _ = in_pool(False)
            on, scores = in_pool(True)
            chk("B2 降层后默认池立刻不含它（不污染上下文）", vid not in off)
            chk("B3 降层后 include_archival 池仍含它（可检索）", vid in on)
            chk("B3b 且分数仍可算出（tier 不参与打分：降优先级靠不进池，不靠扣分）",
                isinstance(scores.get(vid), (int, float)) and scores.get(vid) > 0,
                scores.get(vid))
            chk("B4 只降层、内容未变（影子语义）",
                int(c.execute("SELECT COALESCE(forgotten,0) FROM agent_memory WHERE id=?",
                              (vid,)).fetchone()[0]) == 0)

            ok = MS.revive_memory(c, vid)
            back, _ = in_pool(False)
            chk("B5 复活后回到默认池（revive 真的生效）", ok and vid in back, ok)
            chk("B5b 复活后 tier=recall 且 forgotten=0",
                tuple(c.execute("SELECT tier, COALESCE(forgotten,0) FROM agent_memory WHERE id=?",
                                (vid,)).fetchone()) == ("recall", 0))

            # 归档一条不相干的行，其余池成员不得受影响（口径隔离）
            other = c.execute(
                "SELECT id FROM agent_memory WHERE tier='recall' AND COALESCE(forgotten,0)=0 "
                "AND COALESCE(superseded_by,0)=0 AND id<>? ORDER BY id LIMIT 1",
                (vid,)).fetchone()
            oid = int(other["id"])
            base_set = in_pool(False)[0]
            c.execute("UPDATE agent_memory SET tier='archival' WHERE id=?", (oid,))
            c.commit()
            moved = in_pool(False)[0]
            chk("B6 归档不相干的一条，池成员只在它身上变化（口径隔离）",
                (base_set - moved) <= {oid}, sorted(base_set - moved))
        finally:
            c.close()
    finally:
        shutil.rmtree(td, ignore_errors=True)

    # ══════════ C 区：HTTP（服务在跑才有意义） ══════════
    print("\n[C] HTTP 层（管理端能否回捞冷层）")
    try:
        import json
        import urllib.request

        def _get(path):
            with urllib.request.urlopen(BASE + path, timeout=8) as r:
                return json.loads(r.read().decode("utf-8"))

        wk = _get("/api/memory/stats/weekly?days=7")
        chk("C1 周报 cold_archival 与库内一致",
            int(wk["stock"]["cold_archival"]) == len(cold),
            "api=%s db=%s" % (wk["stock"].get("cold_archival"), len(cold)))
        row = _get("/api/memory/list?tier=archival&limit=50")
        items = row.get("items") if isinstance(row, dict) else row
        ids = {int(x["id"]) for x in (items or [])}
        chk("C2 /api/memory/list?tier=archival 能列出冷层（人工可回捞）",
            set(cold_ids) <= ids, "缺 %s" % sorted(set(cold_ids) - ids))
    except Exception as e:  # noqa: BLE001
        skip("C1/C2 HTTP 层", "%s: %s" % (type(e).__name__, str(e)[:80]))

    print()
    print("=" * 78)
    print("PASS %d / FAIL %d / SKIP %d" % (len(PASS), len(FAIL), len(SKIP)))
    for f in FAIL:
        print("  FAIL:", f)
    print("=" * 78)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
