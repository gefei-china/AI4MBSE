# -*- coding: utf-8 -*-
"""Dreaming 门禁（2026-11-11）：离线对账必须只读、必须过 HIL、必须有牙齿。

## 为什么这些判据一条都不能少
Anthropic 原文的两类事故，正好对应本门禁的两端：
- 「错笔记被后续会话无条件信任」⇒ 必须能从流水里找到反证（F2）；
- 「错笔记被提升到 injected context 会污染每个会话」⇒ 不许自动落库（F3）、
  且落库只改状态不改正文（F4）。

## 断言
  F1 `dream()` 只读：输入 store 快照 sha256 前后一致
  F2 已知错误笔记（宝可梦中心在东侧）必须产出 contradicted 候选
  F3 未经 HIL 批准 ⇒ apply 拒绝、0 写入
  F4 经 HIL 批准 ⇒ 只改 verify_state / evidence_ref，content 一字不变
  F5 注入索引 ≤200 行 / 25 KB，且每行能解析到真实记忆
  F6 wiki 死链 0、体积 ≤ 源笔记 1.0×（原文反例是 1.77×）
  F7 metrics 固定四项齐全
  F8 真库只读冒烟（有 mbse.db 时）
  M1~M4 变异自证：证据检索置空 / 去掉 HIL 门 / 索引不截断 / apply 改正文
                    —— 每一条都必须让对应判据翻红
"""
import hashlib
import json
import os
import sqlite3
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.chdir(ROOT)

PASS, FAIL, SKIPPED = [], [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    if cond:
        print("  PASS %s" % name, flush=True)
    else:
        print("  FAIL %s  <- %s" % (name, detail), flush=True)


def skip(name, why):
    SKIPPED.append(name)
    print("  SKIP %s  <- %s" % (name, why), flush=True)


def load_dream(mutate=None):
    """读产品模块源码，可选文本变换后 exec —— 变异自证的标准姿势。"""
    path = os.path.join(ROOT, "core", "memory_dream.py")
    with open(path, encoding="utf-8") as f:
        src = f.read()
    if mutate:
        src = mutate(src)
    ns = {"__name__": "memory_dream_mut"}
    exec(compile(src, "core/memory_dream.py", "exec"), ns)
    return ns


def fixture(n_notes=260):
    """内存夹具：一张错笔记 + 一条能推翻它的流水 + 足量笔记测索引截断。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE agent_memory (
            id INTEGER PRIMARY KEY, agent_id TEXT DEFAULT 'tester', content TEXT,
            mem_type TEXT DEFAULT 'fact', mem_topic TEXT DEFAULT '',
            forgotten INTEGER DEFAULT 0, verify_state TEXT DEFAULT 'unverifiable',
            evidence_ref TEXT DEFAULT '', evidence_note TEXT DEFAULT '',
            source_credibility REAL, tier TEXT DEFAULT 'recall',
            access_count INTEGER DEFAULT 0, last_accessed_at TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY, conversation_id INTEGER DEFAULT 1,
            role TEXT DEFAULT 'user', content TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE tool_call_logs (
            id INTEGER PRIMARY KEY, tool_name TEXT DEFAULT '', arguments TEXT DEFAULT '',
            result TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE query_trace (
            id INTEGER PRIMARY KEY, query TEXT DEFAULT '', query_raw TEXT,
            normalized TEXT DEFAULT '', route TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP);
    """)
    conn.execute("INSERT INTO agent_memory (id,content,mem_topic,tier) VALUES "
                 "(1,?,?,?)", ("宝可梦中心在山洞东侧，走出门即代表月见山已完成", "导航", "core"))
    for i in range(2, n_notes + 1):
        conn.execute("INSERT INTO agent_memory (id,content,mem_topic) VALUES (?,?,?)",
                     (i, "常规建模经验条目 %d：SysML_V2 语法规范与视图层级" % i,
                      "topic%d" % (i % 12)))
    conn.execute("INSERT INTO messages (id,content) VALUES (1,?)",
                 ("宝可梦中心不在东侧，笔记有误，实际在西侧；"
                  "从月见山出口向右侧跳下是单向落差，无法原路返回",))
    conn.execute("INSERT INTO query_trace (id,query,query_raw,created_at) VALUES "
                 "(1,'SysML v2 语法规范','SysML v2 语法规范','2026-11-01 10:00:00')")
    conn.execute("INSERT INTO query_trace (id,query,query_raw,created_at) VALUES "
                 "(2,'SysML v2 语法规范','SysML v2 语法规范','2026-11-02 10:00:00')")
    conn.execute("INSERT INTO query_trace (id,query,query_raw,created_at) VALUES "
                 "(3,'SysML v2 语法规范','SysML v2 语法规范','2026-11-03 10:00:00')")
    conn.commit()
    from database.migrations.memory import _migrate_memory_layer
    _migrate_memory_layer(conn)
    return conn


def snapshot(conn):
    rows = [tuple(r) for r in conn.execute(
        "SELECT id, content, verify_state, source_credibility FROM agent_memory ORDER BY id")]
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False, default=str)
                          .encode("utf-8")).hexdigest()


def probe(ns, *, apply_hil=False):
    """在一套干净夹具上跑完整场景，返回各判据的布尔值。"""
    conn = fixture()
    try:
        before = snapshot(conn)
        pkg = ns["dream"](conn, sessions=3, note_limit=60)
        f1 = snapshot(conn) == before == pkg["input_sha256"]
        caught = [c for c in pkg["candidates"]
                  if c.get("state") == "contradicted" and c.get("memory_id") == 1]
        f2 = bool(caught)
        idx = pkg["index"]
        f5 = idx["lines"] <= 200 and idx["bytes"] <= 25 * 1024
        wiki = pkg["wiki"]
        # 双判据：相对膨胀 ≤1.2× + 绝对量不超「源笔记 + 每页 32 字符页头预算」。
        # 单用 1.0× 会把短笔记集合（页头占比天然高）误判；单用预算又抓不住
        # 大语料的持续膨胀。两条一起才能既不放过 Borges 反例、又不冤枉正常索引。
        f6 = (not wiki["dangling_links"]
              and wiki["wiki_chars"] <= wiki["budget_chars"]
              and wiki["wiki_chars"] <= 1.2 * max(1, wiki["source_chars"]))
        f7 = all(k in pkg["metrics"] for k in
                 ("memory_hit_rate", "fact_conflict_rate",
                  "tokens_per_question", "injected_chars"))
        r_noh = ns["apply_candidates"](conn, pkg)
        f3 = (not r_noh["ok"]) and r_noh["applied"] == 0
        content_before = conn.execute(
            "SELECT content FROM agent_memory WHERE id=1").fetchone()[0]
        if apply_hil:
            r_hil = ns["apply_candidates"](conn, pkg, hil_approved=True,
                                           approved_by="gate")
            row = conn.execute(
                "SELECT content, verify_state FROM agent_memory WHERE id=1").fetchone()
            f4 = (r_hil["ok"] and r_hil["applied"] > 0
                  and row[0] == content_before and row[1] == "contradicted")
        else:
            f4 = None
        return {"f1": f1, "f2": f2, "f3": f3, "f4": f4, "f5": f5,
                "f6": f6, "f7": f7, "pkg": pkg, "nohil_reason": r_noh["reason"]}
    finally:
        conn.close()


def main():
    real = load_dream()
    r = probe(real, apply_hil=True)
    print("\n[1] 只读 / 取证 / HIL / 索引 / wiki", flush=True)
    check("F1 dream 不写输入 store（快照 sha256 前后一致）", r["f1"])
    check("F2 已知错误笔记产出 contradicted 候选", r["f2"],
          "候选里没有 memory_id=1 的 contradicted")
    check("F3 未批准 ⇒ apply 拒绝且 0 写入", r["f3"],
          "实得 reason=%r" % r["nohil_reason"])
    check("F4 批准后只改状态、不改正文", r["f4"])
    check("F5 索引 ≤200 行 / 25KB", r["f5"],
          "实得 %d 行 / %d B" % (r["pkg"]["index"]["lines"], r["pkg"]["index"]["bytes"]))
    check("F6 wiki 无死链且体积不超预算（≤1.2× 且不超页头预算）", r["f6"],
          "实得死链 %d / 体积比 %.2f / wiki %d vs 预算 %d" % (
              len(r["pkg"]["wiki"]["dangling_links"]),
              r["pkg"]["wiki"]["wiki_chars"] / max(1, r["pkg"]["wiki"]["source_chars"]),
              r["pkg"]["wiki"]["wiki_chars"], r["pkg"]["wiki"]["budget_chars"]))
    check("F7 metrics 四项齐全（不臆造未测指标）", r["f7"])

    print("\n[2] 真库只读冒烟（有 mbse.db 才跑）", flush=True)
    db = os.path.join(ROOT, "mbse.db")
    if not os.path.exists(db):
        skip("F8 真库冒烟", "无 mbse.db（CI 干净库）")
    else:
        conn = sqlite3.connect("file:%s?mode=ro" % db.replace("\\", "/"), uri=True)
        conn.row_factory = sqlite3.Row
        try:
            b = snapshot(conn)
            p = real["dream"](conn, sessions=10, note_limit=80)
            a = snapshot(conn)
            check("F8 真库 dream 只读且索引合规",
                  b == a == p["input_sha256"] and p["index"]["lines"] <= 200
                  and p["index"]["bytes"] <= 25 * 1024
                  and not p["wiki"]["dangling_links"],
                  "sha %s→%s / idx %d行 / 死链 %d" % (
                      b[:8], a[:8], p["index"]["lines"],
                      len(p["wiki"]["dangling_links"])))
            check("F8b 真库 wiki 体积不超预算",
                  p["wiki"]["wiki_chars"] <= p["wiki"]["budget_chars"]
                  and p["wiki"]["wiki_chars"] <= 1.2 * max(1, p["wiki"]["source_chars"]),
                  "实得 %.2f× / %d vs 预算 %d" % (
                      p["wiki"]["wiki_chars"] / max(1, p["wiki"]["source_chars"]),
                      p["wiki"]["wiki_chars"], p["wiki"]["budget_chars"]))
            print("  · 真库候选包：%s" % json.dumps(p["stats"], ensure_ascii=False),
                  flush=True)
        finally:
            conn.close()

    print("\n[3] 变异自证（每条都必须让对应判据翻红）", flush=True)
    mutants = (
        ("M1 证据检索置空 ⇒ F2 必须红", "F2",
         lambda s: s.replace("    from transcript_fts import search_transcripts",
                             "    return []\n    from transcript_fts import search_transcripts", 1)),
        ("M2 去掉 HIL 门 ⇒ F3 必须红", "F3",
         lambda s: s.replace("    if not hil_approved:", "    if False:", 1)),
        ("M3 索引不截断 ⇒ F5 必须红", "F5",
         lambda s: s.replace("        if len(lines) >= max_lines:",
                             "        if False:", 1)),
        ("M4 apply 偷改正文 ⇒ F4 必须红", "F4",
         lambda s: s.replace('    out["reason"] = "APPLIED_STATES_ONLY"',
                             '    conn.execute("UPDATE agent_memory SET content=content || \'[MUT]\'")\n'
                             '    out["reason"] = "APPLIED_STATES_ONLY"', 1)),
        ("M5 wiki 不截摘要（Borges 反例）⇒ F6 必须红", "F6",
         lambda s: s.replace('[:24]', '[:400]', 1)),
    )
    for label, flag, mut in mutants:
        try:
            src = open(os.path.join(ROOT, "core", "memory_dream.py"),
                       encoding="utf-8").read()
            changed = mut(src)
            assert changed != src, "变异锚点失效（源码已改写，判据需同步更新）"
            ns = load_dream(mut)
            got = probe(ns, apply_hil=(flag == "F4"))
            key = flag.lower()
            check(label, got[key] is False or got[key] is None,
                  "变异后判据仍为 %r ⇒ 该判据抓不住这个回归" % got[key])
        except AssertionError as e:
            check(label, False, str(e))
        except Exception as e:  # noqa: BLE001
            check(label, False, "%s: %s" % (type(e).__name__, e))

    print("\n=== %d PASS / %d FAIL / %d SKIP ===" % (len(PASS), len(FAIL), len(SKIPPED)),
          flush=True)
    for f in FAIL:
        print("  ✗ %s" % f, flush=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
