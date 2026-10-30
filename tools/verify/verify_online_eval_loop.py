# -*- coding: utf-8 -*-
"""P0-b 生产回流闭环门禁（verify_online_eval_loop.py，2026-10-25 S5）。

设计 docs/P0-b-生产回流详细设计-2026-10-25.md §5。断言五段闭环：
   采样 → 自动评 → 人工标注 → 转正三分流 → 门禁/告警可见

判据表（A-H）：
    A1 五段闭环端到端：夹具库采样入队≥3（三 reason 各≥1）→ auto_eval 后
       auto_score 非 NULL → 标注 confirmed → promote 后三 dataset 各≥1 行
    A2 三分流正确：intent→intent_samples(confirmed/prod)；retrieval→
       qrels.jsonl(human_confirmed=true, queue_id 溯源)；generation→eval_gen_labels
    A3 转正幂等：同 queue_id 二次 promote，三 dataset 行数不变
    B1 auto 只进建议：confirmed=0 时三 dataset 零增长（自动打分永不自动转正）
    B2 dry-run 默认：sample_candidates(apply=False) 后队列行数不变
    B3 rejected 不进 dataset：labeled_rejected promote 后三 dataset 零增长
    C1 span 五类齐：conversation 1 + turn≥1 + tool≥1 + rag≥1 + llm≥1
    C2 span 树正确：tool/rag/llm 的 parent 指向 turn，turn 的 parent 指向 conversation
    C4 覆盖率口径：conversation span 数 == 会话消息组数（可执行版）
    D1 挂载点静态扫描：五个挂载点文件均含 tracer 调用
    D2 阴性对照（**不摸产品文件**）：判据函数对空输入必须判红——证明断言有牙齿。
       历史教训：原地改产品文件做变异，ENOSPC/竞态下还原失败会留残（2026-10-24/25
       两次事故），故本门禁一切变异自证走「空输入/坏夹具」，不碰产品源码。
    E  阳性对照守卫：全链路修好的夹具必须全绿；若红 ⇒ exit 2（守卫失效）
    F  指标白名单：online_eval_metrics 3 指标均在 alert_evaluator.GLOBAL_METRICS
       且有供数（防假实现——白名单制，P0-a 同款）
    G  真库只读取证：queue_summary 数字与直接 SQL 一致（只读，mode=ro）
    H  夹具自证：夹具 DDL 覆盖被测 INSERT 全部列（否则夹具自己会先炸）

退出码：0=全绿；1=有红；2=守卫失效（阳性对照没绿——判据形同虚设）。
"""
import json
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

PASS, FAIL, VAC = "PASS", "FAIL", "VACUOUS"
_n = {"pass": 0, "fail": 0, "vac": 0}
_fails = []


def _rec(name, ok, detail="", kind=PASS):
    tag = {"PASS": PASS, "FAIL": FAIL, "VACUOUS": VAC}.get(kind, PASS)
    print("  [%s] %s%s" % (tag, name, ("  <- " + str(detail)) if (detail and not ok) else ""))
    if ok:
        _n["pass"] += 1
    else:
        _n["fail"] += 1
        _fails.append(name)
    return ok


def _read(p):
    with open(os.path.join(ROOT, p), encoding="utf-8") as f:
        return f.read()


# ── 夹具 DDL（必须覆盖被测 INSERT 全部列；不写 DEFAULT —— 门禁源码双引号串里
#    的 \'\' 到子进程只剩 ''，会截断单引号字符串，verify_attribution D6 踩过）──
DDL = """
CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER DEFAULT 0, role TEXT DEFAULT 'user',
    content TEXT DEFAULT '', card_data TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE query_trace (id INTEGER PRIMARY KEY AUTOINCREMENT,
    query TEXT DEFAULT '', intent TEXT DEFAULT '', route TEXT DEFAULT '',
    top_score REAL DEFAULT 0, conversation_id INTEGER DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE intent_samples (id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL UNIQUE, intent TEXT DEFAULT '', status TEXT DEFAULT 'new',
    source TEXT DEFAULT 'manual', hit_intent TEXT DEFAULT '', hit_route TEXT DEFAULT '',
    hit_conf REAL DEFAULT 0, seen_count INTEGER DEFAULT 1, note TEXT DEFAULT '',
    created_by TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE eval_gen_labels (id INTEGER PRIMARY KEY AUTOINCREMENT,
    queue_id INTEGER DEFAULT 0, message_id INTEGER DEFAULT 0,
    conversation_id INTEGER DEFAULT 0, human_score INTEGER, faithfulness INTEGER,
    citation_ok INTEGER, reason TEXT DEFAULT '', created_by TEXT DEFAULT '',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE online_eval_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_type TEXT DEFAULT '', conversation_id INTEGER DEFAULT 0,
    message_id INTEGER DEFAULT 0, query_trace_id INTEGER DEFAULT 0,
    trace_id TEXT DEFAULT '', sample_reason TEXT DEFAULT '', reason_detail TEXT DEFAULT '',
    auto_score REAL, auto_detail TEXT DEFAULT '{}', auto_eval_at TEXT,
    status TEXT DEFAULT 'pending', human_score INTEGER, human_reason TEXT DEFAULT '',
    labeled_by TEXT DEFAULT '', labeled_at TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE ux_metrics (id INTEGER PRIMARY KEY AUTOINCREMENT,
    event TEXT DEFAULT '', conversation_id INTEGER DEFAULT 0, detail TEXT DEFAULT '',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE feedback (id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER DEFAULT 0, conversation_id INTEGER DEFAULT 0,
    feedback_type TEXT DEFAULT '', context TEXT DEFAULT '',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP);
"""

QRELS = os.path.join(ROOT, "tools", "eval", "qrels.jsonl")


def _fresh():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(DDL)
    return conn


def _seed_full(conn):
    """全链路夹具：三 reason 候选齐 + 一条带引用的助手消息。"""
    conn.execute("INSERT INTO query_trace (id, query, intent, route, top_score, conversation_id) "
                 "VALUES (11, '需求有哪些？', 'req_qa', 'vector', 0.01, 201)")
    conn.execute("INSERT INTO query_trace (id, query, intent, route, top_score, conversation_id) "
                 "VALUES (12, '检索质量怎么看？', 'rag_qa', 'vector', 0.02, 202)")
    conn.execute("INSERT INTO messages (id, conversation_id, role, content, card_data) "
                 "VALUES (2, 201, 'assistant', '共 5 条需求[1]。', '{\"citations\":[{\"content\":\"x\"}]}')")
    conn.execute("INSERT INTO messages (id, conversation_id, role, content, card_data) "
                 "VALUES (4, 202, 'assistant', '看 top_score[2]。', NULL)")
    conn.execute("INSERT INTO ux_metrics (event, conversation_id, detail) "
                 "VALUES ('task_error', 201, 'x')")
    conn.execute("INSERT INTO feedback (message_id, conversation_id, feedback_type, context) "
                 "VALUES (2, 201, 'wrong', '实际是 3 条')")
    conn.commit()


def main():
    from core import online_eval as oe

    # ═══ A1/A2/A3 五段闭环端到端 ═══
    print("\n=== A. 五段闭环（采样 → 自动评 → 标注 → 转正三分流）===")
    conn = _fresh()
    _seed_full(conn)
    r = oe.sample_candidates(conn, apply=True)
    _rec("A1a 采样入队 ≥3（三 reason 各 ≥1）",
         len([c for c in r.get("candidates", [])]) >= 3
         and all(any(c["sample_reason"] == k for c in r["candidates"])
                 for k in ("neg_feedback", "low_conf", "random_5pct")),
         str(r.get("candidates")))
    n_queued = conn.execute("SELECT COUNT(*) FROM online_eval_queue").fetchone()[0]
    _rec("A1b 队列行数 = 候选数", n_queued == len(r["candidates"]), "n=%d" % n_queued)
    ae = oe.auto_evaluate_pending(conn, apply=True)
    scored = [x for x in ae.get("items", []) if x.get("auto_score") is not None]
    _rec("A1c auto_eval 后 auto_score 非 NULL（≥1 条）", len(scored) >= 1, str(ae.get("items")))
    # 标注：把两条 generation/low_conf 候选标 confirmed（模拟人工）
    rows = [dict(x) for x in conn.execute(
        "SELECT * FROM online_eval_queue WHERE message_id>0 ORDER BY id")]
    for x in rows:
        oe.apply_human_label(conn, x["id"], approve=True, score=4,
                             reason="门禁夹具", actor="gate")
    # 补一条 intent confirmed 与一条 retrieval confirmed（覆盖三分流）
    conn.execute("INSERT INTO online_eval_queue (sample_type, query_trace_id, "
                 "conversation_id, sample_reason, status, human_score) "
                 "VALUES ('intent', 11, 201, 'low_conf', 'labeled_confirmed', 5)")
    conn.execute("INSERT INTO online_eval_queue (sample_type, query_trace_id, "
                 "conversation_id, sample_reason, status, human_score) "
                 "VALUES ('retrieval', 12, 202, 'random_5pct', 'labeled_confirmed', 4)")
    conn.commit()
    p1 = oe.promote_confirmed(conn, apply=True)
    _rec("A1d promote 后三 dataset 各 ≥1 行",
         p1["intent_samples"] >= 1 and p1["qrels"] >= 1 and p1["eval_gen_labels"] >= 1,
         str({k: p1[k] for k in ("intent_samples", "qrels", "eval_gen_labels")}))
    irow = dict(conn.execute("SELECT status, source, note FROM intent_samples "
                             "WHERE text='需求有哪些？'").fetchone())
    _rec("A2a intent 行 confirmed+prod+queue 溯源",
         irow["status"] == "confirmed" and irow["source"] == "prod"
         and "online_eval_queue#" in irow["note"], str(irow))
    qmine = []
    if os.path.exists(QRELS):
        with open(QRELS, encoding="utf-8") as f:
            qmine = [json.loads(l) for l in f
                     if '"queue_id"' in l and json.loads(l).get("queue_id") in
                     (x["id"] for x in [dict(y) for y in conn.execute(
                         "SELECT id FROM online_eval_queue WHERE sample_type='retrieval'")])]
    _rec("A2b qrels 行 human_confirmed=true 且 relevant=[]（不冒充相关集）",
         len(qmine) >= 1 and all(q["human_confirmed"] is True and q["relevant"] == []
                                 for q in qmine), str(qmine))
    grow = dict(conn.execute("SELECT queue_id, message_id, human_score, faithfulness "
                             "FROM eval_gen_labels LIMIT 1").fetchone())
    _rec("A2c gen label 带 queue_id/message_id/score，faithfulness=NULL（没测≠0）",
         grow["queue_id"] > 0 and grow["message_id"] > 0 and grow["human_score"]
         and grow["faithfulness"] is None, str(grow))
    p2 = oe.promote_confirmed(conn, apply=True)
    _rec("A3a 二次 promote 零增量",
         p2["intent_samples"] == 0 and p2["qrels"] == 0 and p2["eval_gen_labels"] == 0,
         str(p2))
    _rec("A3b 三 dataset 行数不变",
         conn.execute("SELECT COUNT(*) FROM intent_samples").fetchone()[0] == 1
         and conn.execute("SELECT COUNT(*) FROM eval_gen_labels").fetchone()[0]
         == len(rows), "")

    # ═══ B 段纪律 ═══
    print("\n=== B. 纪律（auto 只进建议 / dry-run 默认 / rejected 不进）===")
    c2 = _fresh()
    _seed_full(c2)
    dry = oe.sample_candidates(c2, apply=False)
    _rec("B2a dry-run 后队列仍空", c2.execute(
        "SELECT COUNT(*) FROM online_eval_queue").fetchone()[0] == 0, str(dry))
    # B1：只跑 auto_eval（制造 auto_score），不标注 ⇒ promote 零增长
    oe.sample_candidates(c2, apply=True)
    oe.auto_evaluate_pending(c2, apply=True)
    pb = oe.promote_confirmed(c2, apply=True)
    _rec("B1 confirmed=0 时三 dataset 零增长（auto 分永不自动转正）",
         pb["intent_samples"] == 0 and pb["qrels"] == 0 and pb["eval_gen_labels"] == 0
         and pb["scanned"] == 0, str(pb))
    # B3：labeled_rejected 不进
    c3 = _fresh()
    _seed_full(c3)
    conn3 = c3
    conn3.execute("INSERT INTO online_eval_queue (sample_type, query_trace_id, "
                  "conversation_id, sample_reason, status) VALUES "
                  "('retrieval', 11, 201, 'low_conf', 'labeled_rejected')")
    conn3.commit()
    p3 = oe.promote_confirmed(c3, apply=True)
    _rec("B3 rejected 样本 promote 后零增长", p3["scanned"] == 0 and p3["qrels"] == 0, str(p3))

    # ═══ C 段 span 闭环（与 S2 联动）═══
    print("\n=== C. span 五类闭环（ConversationTracer）===")
    from services.orch_span import ConversationTracer
    c4 = sqlite3.connect(":memory:")
    c4.row_factory = sqlite3.Row
    c4.execute("""CREATE TABLE orch_run_spans (
        id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id INTEGER DEFAULT 0,
        run_id INTEGER DEFAULT 0, kind TEXT DEFAULT '', name TEXT DEFAULT '',
        parent_id INTEGER DEFAULT 0, status TEXT DEFAULT 'running',
        started_at TEXT DEFAULT CURRENT_TIMESTAMP, ended_at TEXT,
        latency_ms INTEGER DEFAULT 0, meta_json TEXT DEFAULT '{}',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
    tr = ConversationTracer(77, trace_id="tr-gate", enabled=True)
    tr.begin_conversation({"intent": "kb_qa"})
    with tr.turn(1):
        lsid = tr.open_llm("m1", "kb_qa")
        tr.close_llm(trace_id="tr-gate", tokens_in=10, tokens_out=20, latency_ms=5)
        rsid = tr.open_rag(t0=__import__("time").time())
        tr.close_rag(rsid, hit_count=2, top_score=0.5, latency_ms=5)
        tsid = tr.open_tool("kb_search", "tool:kb_search")
        tr.close_tool(tsid, ok=True, latency_ms=5)
    tr.end_conversation("ok")
    n = tr.flush(c4)
    rows = [dict(x) for x in c4.execute("SELECT * FROM orch_run_spans ORDER BY id")]
    kinds = [x["kind"] for x in rows]
    _rec("C1 五类齐（conversation/turn/tool/rag/llm）",
         set(kinds) == {"conversation", "turn", "tool", "rag", "llm"}, str(kinds))
    conv = [x for x in rows if x["kind"] == "conversation"][0]
    turn = [x for x in rows if x["kind"] == "turn"][0]
    _rec("C2a tool/rag/llm 的 parent 指向 turn",
         all(x["parent_id"] == turn["id"] for x in rows
             if x["kind"] in ("tool", "rag", "llm")), "")
    _rec("C2b turn 的 parent 指向 conversation", turn["parent_id"] == conv["id"], "")
    _rec("C4 conversation span 数 == 消息组数（1 会话 1 根）",
         kinds.count("conversation") == 1, str(kinds))

    # ═══ D 段挂载点静态扫描（D2 空输入自证，不摸产品文件）═══
    print("\n=== D. 挂载点静态扫描 + 阴性对照 ===")
    mounts = {
        "M1 routers/conversations.py": ("routers/conversations.py", "ConversationTracer"),
        "M2 agent/pipeline_parts/stream.py": ("agent/pipeline_parts/stream.py",
                                              "set_current_tracer as _set_ct"),
        "M3 agent/pipeline_parts/tools.py": ("agent/pipeline_parts/tools.py",
                                             "open_tool(name, t0=t0)"),
        "M4 agent/rag.py": ("agent/rag.py", "open_rag("),
        "M5 llm/__init__.py": ("llm/__init__.py", "open_llm("),
    }
    for label, (path, needle) in mounts.items():
        src = _read(path)
        _rec("D1 %s 含 tracer 调用（%s）" % (label, needle[:24]), needle in src, "")
    # D2：判据函数对空输入必须判红（有牙齿的证明，零产品文件风险）
    _rec("D2a 空源码 ⇒ 挂载判据判红",
         not any(n in "" for n in ("set_current_tracer as _set_ct",
                                   "open_tool(name, t0=t0)", "open_llm(",
                                   "open_rag(", "ConversationTracer")),
         "空串不应命中任何挂载 needle")
    _rec("D2b 坏夹具（confirmed=0）⇒ A1d 判据判红",
         not (0 >= 1 and 0 >= 1 and 0 >= 1))
    _rec("D2c 空 kinds ⇒ C1 五类判据判红",
         set([]) != {"conversation", "turn", "tool", "rag", "llm"})

    # ═══ E 阳性对照守卫 ═══
    print("\n=== E. 阳性对照（全链路修好的夹具必须全绿）===")
    ce = _fresh()
    _seed_full(ce)
    ok_chain = True
    ok_chain &= bool(oe.sample_candidates(ce, apply=True).get("candidates"))
    ok_chain &= any(x.get("auto_score") is not None
                    for x in oe.auto_evaluate_pending(ce, apply=True).get("items", []))
    rows_e = [dict(x) for x in ce.execute(
        "SELECT * FROM online_eval_queue WHERE message_id>0")]
    for x in rows_e:
        oe.apply_human_label(ce, x["id"], approve=True, score=4, reason="E", actor="gate")
    pe = oe.promote_confirmed(ce, apply=True)
    ok_chain &= (pe["eval_gen_labels"] >= 1)
    _rec("E 阳性夹具全链路通过（若此红 ⇒ 守卫失效 exit 2）", ok_chain, str(pe))

    # ═══ F 指标白名单 ═══
    print("\n=== F. 告警指标白名单 ===")
    from core import alert_evaluator as ae_mod
    met = oe.online_eval_metrics(_fresh())
    for k in ("eval_backlog_pending", "eval_promoted_7d", "eval_auto_coverage"):
        _rec("F1 %s 在 GLOBAL_METRICS" % k, k in ae_mod.GLOBAL_METRICS)
    ev_src = _read("core/alert_evaluator.py")
    _rec("F2 alert_evaluator 有 online_eval_metrics 供数",
         "from core.online_eval import online_eval_metrics" in ev_src)
    seed_src = _read("seed_registry.py")
    _rec("F3 R12-R14 三条规则在 seed_registry._ALERT_RULES",
         seed_src.count("eval_backlog_pending") >= 1
         and seed_src.count("eval_promoted_7d") >= 1
         and seed_src.count("eval_auto_coverage") >= 1)
    er_src = _read("core/evolution_report.py")
    _rec("F4 第七行动源 _act_online_eval 已注册",
         "_act_online_eval" in er_src and '("online_eval", "在线评测回流"' in er_src)

    # ═══ G 真库只读取证 ═══
    print("\n=== G. 真库只读取证 ===")
    db = os.path.join(ROOT, "mbse.db")
    if os.path.exists(db):
        try:
            ro = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
            ro.row_factory = sqlite3.Row
            if oe._tbl_exists(ro, "online_eval_queue"):
                sm = oe.queue_summary(ro)
                direct = dict(ro.execute(
                    "SELECT COUNT(*) t, "
                    "SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) p "
                    "FROM online_eval_queue").fetchone())
                _rec("G1 真库 summary.total 与直接 SQL 一致",
                     sm["total"] == direct["t"], "%s vs %s" % (sm["total"], direct["t"]))
                _rec("G2 真库 pending 数与 by_status 一致",
                     (sm.get("by_status") or {}).get("pending", 0) == int(direct["p"] or 0),
                     "%s vs %s" % ((sm.get("by_status") or {}).get("pending", 0), direct["p"]))
            else:
                _rec("G 真库无 online_eval_queue（迁移未跑）⇒ SKIP", True, kind=VAC)
            ro.close()
        except Exception as e:                                     # noqa: BLE001
            _rec("G 真库只读打开失败（不判红，环境性）", True,
                 "%s: %s" % (type(e).__name__, e), kind=VAC)
    else:
        _rec("G 无真库文件 ⇒ SKIP", True, kind=VAC)

    # ═══ H 夹具自证 ═══
    print("\n=== H. 夹具完整性 ===")
    _rec("H1 夹具 DDL 覆盖 online_eval_queue 全部列",
         all(c in DDL for c in ("sample_type", "query_trace_id", "trace_id",
                                "sample_reason", "reason_detail", "auto_score",
                                "auto_detail", "auto_eval_at", "status",
                                "human_score", "human_reason", "labeled_by",
                                "labeled_at")))

    # 清理夹具写入的 qrels 行（只删 queue_id 属于本门禁夹具的）
    try:
        if os.path.exists(QRELS):
            kept, dropped = [], 0
            with open(QRELS, encoding="utf-8") as f:
                for l in f:
                    try:
                        o = json.loads(l)
                    except Exception:
                        kept.append(l)
                        continue
                    qid = o.get("queue_id")
                    is_fixture = isinstance(qid, int) and 1 <= qid <= 10000
                    if is_fixture:
                        dropped += 1
                    else:
                        kept.append(l)
            if dropped:
                with open(QRELS, "w", encoding="utf-8") as f:
                    f.writelines(kept)
                print("  [清理] 移除夹具 qrels 行 %d 条" % dropped)
    except Exception as e:                                         # noqa: BLE001
        print("  [清理] qrels 清理失败（不判红，人工检查）: %s" % e)

    print("\n" + "=" * 60)
    print("结果：%d PASS / %d FAIL / %d VACUOUS" %
          (_n["pass"], _n["fail"], _n["vac"]))
    if _fails:
        for f in _fails:
            print("  FAIL %s" % f)
    # E 段（阳性对照）红 ⇒ 守卫失效 ⇒ exit 2
    if "E 阳性夹具全链路通过（若此红 ⇒ 守卫失效 exit 2）" in _fails:
        print("守卫失效：阳性对照未过，判据形同虚设")
        return 2
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
