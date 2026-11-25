# -*- coding: utf-8 -*-
"""P0-2 / P1-3 门禁：流水 FTS 与统一 notes 视图真的可用。

  T1 索引行数与三张源表逐类对齐
  T2 新写流水由触发器自动入索引（不改任何写路径）
  T3 UPDATE 后不产生重复索引行（既有 knowledge_fts 的 UPDATE 触发器的坑）
  T4 v_memory_notes 把多类台账收敛成统一字段
  T5 记忆层迁移幂等（第二次不新增列、不重复重建）
  M1/M2 变异自证：去掉 UPDATE 触发器的删旧 / 不建 INSERT 触发器

⚠️ 为什么必须单列这个门禁：`core/memory_dream.py` 的取证能力完全建立在
   transcripts 可检索上。索引一旦 stale，Dreaming 会"安静地一条证据都找不到"，
   而所有上层判据仍显示通过 —— 属于典型的静默降级。
"""
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("[OK  ] " if cond else "[FAIL] ") + name +
          ("  — " + str(detail) if detail and not cond else ""), flush=True)


def load_fts(mutate=None):
    path = os.path.join(ROOT, "adapters/transcript_fts.py")
    with open(path, encoding="utf-8") as f:
        src = f.read()
    if mutate:
        src = mutate(src)
    ns = {"__name__": "transcript_fts_mut"}
    exec(compile(src, "adapters/transcript_fts.py", "exec"), ns)
    return ns


def fixture(fts_ns=None):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE agent_memory (id INTEGER PRIMARY KEY, content TEXT,
            mem_topic TEXT, mem_type TEXT DEFAULT 'fact', forgotten INTEGER DEFAULT 0,
            tier TEXT DEFAULT 'recall', created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE messages (id INTEGER PRIMARY KEY, conversation_id INTEGER,
            role TEXT, content TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE tool_call_logs (id INTEGER PRIMARY KEY, tool_name TEXT,
            arguments TEXT, result TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE query_trace (id INTEGER PRIMARY KEY, query TEXT, query_raw TEXT,
            normalized TEXT, route TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE intent_samples (id INTEGER PRIMARY KEY, text TEXT, status TEXT,
            hit_intent TEXT, hit_conf REAL, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE bad_cases (id INTEGER PRIMARY KEY, kind TEXT, subject TEXT,
            status TEXT, occurrences INTEGER, last_src_id INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        -- 下面四张表按真库列名建（2026-11-11 真机核对）：
        -- 视图列映射写错时，整列读成空串，而"视图存在"照样通过 ⇒ 必须造行测非空率。
        CREATE TABLE agent_todos (id INTEGER PRIMARY KEY, conversation_id INTEGER,
            run_id TEXT, seq INTEGER, subject TEXT, status TEXT, verify TEXT,
            verify_result TEXT, note TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE online_eval_queue (id INTEGER PRIMARY KEY, sample_type TEXT,
            sample_reason TEXT, reason_detail TEXT, status TEXT, auto_score REAL,
            query_trace_id INTEGER, trace_id TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE config_proposals (id INTEGER PRIMARY KEY, rule TEXT,
            target_key TEXT, current_value TEXT, proposed_value TEXT, rationale TEXT,
            status TEXT, source_feedback_ids TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE orch_interrupts (id INTEGER PRIMARY KEY, run_id TEXT, kind TEXT,
            reason TEXT, instruction TEXT, cancelled_tasks INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP);
    """)
    conn.execute("INSERT INTO messages VALUES (1,1,'user','转发器EIRP 计算口径',datetime('now'))")
    conn.execute("INSERT INTO tool_call_logs VALUES (1,'calc','{}','结果 20 dBW',datetime('now'))")
    conn.execute("INSERT INTO query_trace VALUES (1,'ref part 语法',NULL,'ref part 语法','vector',datetime('now'))")
    conn.execute("INSERT INTO agent_memory (id,content,mem_topic) VALUES (1,'须按包取源码','建模')")
    # 每类都按**真库的真实取值形态**造：真库 intent_samples 562/562 都有 hit_intent，
    # 夹具若留空，T6 会误报"产品列映射错位"（实为夹具缺数据）。
    conn.execute("INSERT INTO intent_samples (id,text,status,hit_intent) "
                 "VALUES (1,'画一张BDD','confirmed','design')")
    conn.execute("INSERT INTO bad_cases (id,kind,subject,status) VALUES (1,'zero','检索零召回','open')")
    conn.execute("INSERT INTO agent_todos (id,subject,status,note) VALUES (1,'跑校验','completed','已完成')")
    conn.execute("INSERT INTO online_eval_queue (id,sample_type,sample_reason,status) "
                 "VALUES (1,'low_conf','低置信抽样','pending')")
    conn.execute("INSERT INTO config_proposals (id,rule,target_key,rationale,status) "
                 "VALUES (1,'r1','rag.top_k','命中率偏低','pending')")
    conn.execute("INSERT INTO orch_interrupts (id,run_id,kind,reason) VALUES "
                 "(1,'r1','user_stop','用户主动停止')")
    conn.commit()
    if fts_ns:
        fts_ns["ensure_transcript_fts"](conn)
    else:
        from database.migrations.memory import _migrate_memory_layer
        return conn, _migrate_memory_layer(conn)
    return conn, None


FTS = load_fts()
conn, mig = fixture()
try:
    print("\n[1] 迁移 / 索引 / 视图", flush=True)
    check("T5 迁移首次建出 FTS 且补齐 evidence 列",
          mig["fts"].get("rebuilt") is True
          and "evidence_ref" in mig["columns_added"], "实得 %r" % mig)
    from database.migrations.memory import _migrate_memory_layer
    mig2 = _migrate_memory_layer(conn)
    check("T5b 迁移幂等（第二次 0 新增列、0 重建）",
          not mig2["columns_added"] and not mig2["fts"].get("rebuilt"),
          "实得 %r" % mig2)
    st = FTS["fts_status"](conn)
    check("T1 索引逐类与源表对齐", st["aligned"] and st["by_kind"] == st["sources"],
          "实得 %r" % st)
    rows = [dict(r) for r in conn.execute(
        "SELECT kind, COUNT(*) n FROM v_memory_notes GROUP BY kind")]
    kinds = {r["kind"] for r in rows}
    check("T4 v_memory_notes 覆盖 ≥4 类台账且字段统一",
          len(kinds) >= 4 and
          {"kind", "subject", "claim", "evidence_ref", "status", "confidence", "created_at"}
          <= set(conn.execute("SELECT * FROM v_memory_notes LIMIT 1").fetchone().keys()),
          "实得 kinds=%r" % kinds)

    # ★ T6（2026-11-11 真机抓到的缺陷）：视图存在**不等于**字段读得出来。
    #   原列映射只取「第一个存在的列」，且把 todo 写成 `task_key`/`title`（真实列是
    #   `subject`/`note`）⇒ 真库 858 行 todo 的 subject/claim **全空**，视图照样 PASS。
    #   判据：每类台账造 1 行有值的数据，subject 与 claim 都必须非空。
    empties = []
    for r in conn.execute(
            "SELECT kind, COUNT(*) n, "
            "SUM(CASE WHEN TRIM(COALESCE(subject,''))='' THEN 1 ELSE 0 END) subj_empty, "
            "SUM(CASE WHEN TRIM(COALESCE(claim,''))='' THEN 1 ELSE 0 END) claim_empty "
            "FROM v_memory_notes GROUP BY kind"):
        if r["subj_empty"] or r["claim_empty"]:
            empties.append(dict(r))
    check("T6 每类台账的 subject / claim 都读得出（列映射未错位）", not empties,
          "有空列：%r" % empties)
    print("\n[2] 触发器同步（写入不改路径）", flush=True)
    conn.execute("INSERT INTO messages VALUES (2,1,'user','新增一条可检索流水',datetime('now'))")
    conn.commit()
    st2 = FTS["fts_status"](conn)
    check("T2 新写流水自动入索引", st2["aligned"] and st2["by_kind"]["messages"] == 2,
          "实得 %r" % st2)
    conn.execute("UPDATE messages SET content='改过的口径' WHERE id=2")
    conn.commit()
    st3 = FTS["fts_status"](conn)
    check("T3 UPDATE 不产生重复索引行", st3["aligned"] and st3["by_kind"]["messages"] == 2,
          "实得 %r" % st3)
    hits = FTS["search_transcripts"](conn, "改过的口径", limit=3)
    check("T2b 改后的文本可被检索到",
          any(h["ref_id"] == 2 for h in hits), "实得 %r" % hits)
finally:
    conn.close()

print("\n[3] 变异自证", flush=True)


def _m_no_delete_on_update(src):
    a = '                body = (f"DELETE FROM {_FTS} WHERE kind=\'{kind}\' AND ref_id=OLD.id; "'
    assert a in src, "M1 锚点失效"
    return src.replace(a,
                       '                body = (f"SELECT 1; "', 1)


def _m_no_insert_trigger(src):
    a = '                body = (f"INSERT INTO {_FTS} (kind, ref_id, title, body) VALUES "\n                        f"(\'{kind}\', NEW.id, {spec[\'title\']}, {spec[\'body\']});")'
    assert a in src, "M2 锚点失效"
    return src.replace(a, '                body = "SELECT 1;"', 1)


def _m_pick_first_only(src):
    """把 `_pick` 改回「只取第一个存在的列」，还原 2026-11-11 那个真机缺陷。"""
    a = "    return \"COALESCE(%s, %s)\" % (\", \".join(parts), default)"
    assert a in src, "M3 锚点失效（_pick 实现已被改写）"
    return src.replace(a, "    return parts[0] if parts else default", 1)


for label, mut, flag in (
    ("M1 UPDATE 触发器不删旧 ⇒ T3 翻红", _m_no_delete_on_update, "update"),
    ("M2 不建 INSERT 触发器 ⇒ T2 翻红", _m_no_insert_trigger, "insert"),
):
    try:
        ns = load_fts(mut)
        c2, _ = fixture(fts_ns=ns)
        try:
            if flag == "update":
                c2.execute("UPDATE messages SET content='变更' WHERE id=1")
                c2.commit()
                ok = ns["fts_status"](c2)["by_kind"]["messages"] == 1
            else:
                c2.execute("INSERT INTO messages VALUES (9,1,'user','新行',datetime('now'))")
                c2.commit()
                ok = ns["fts_status"](c2)["by_kind"]["messages"] == 2
            check(label, ok is False, "变异后判据仍绿 ⇒ 触发器缺陷抓不住")
        finally:
            c2.close()
    except AssertionError as e:
        check(label, False, str(e))
    except Exception as e:  # noqa: BLE001
        check(label, False, "%s: %s" % (type(e).__name__, e))

# M3 单独跑：`_pick` 在 migrations/memory.py，不是 adapters/transcript_fts.py，
# 必须把变异后的迁移模块整个 exec 一遍再建视图，才测得到列映射。
try:
    _mig_path = os.path.join(ROOT, "database", "migrations", "memory.py")
    with open(_mig_path, encoding="utf-8") as f:
        _mig_src = f.read()
    _mut_src = _m_pick_first_only(_mig_src)
    _ns_mig = {"__name__": "memory_migration_mut"}
    exec(compile(_mut_src, _mig_path, "exec"), _ns_mig)
    c3, _ = fixture()          # 先写真迁移建好视图
    try:
        _ns_mig["_migrate_memory_layer"](c3)   # 再用变异版重建视图
        _empty = [dict(r) for r in c3.execute(
            "SELECT kind, SUM(CASE WHEN TRIM(COALESCE(subject,''))='' THEN 1 ELSE 0 END) s, "
            "SUM(CASE WHEN TRIM(COALESCE(claim,''))='' THEN 1 ELSE 0 END) c "
            "FROM v_memory_notes GROUP BY kind") if r["s"] or r["c"]]
        check("M3 列映射只取首列（无回退链）⇒ T6 翻红", bool(_empty),
              "变异后仍全部非空 ⇒ 回退链没被真正测到")
    finally:
        c3.close()
except AssertionError as e:
    check("M3 变异可施加", False, str(e))
except Exception as e:  # noqa: BLE001
    check("M3 变异可施加", False, "%s: %s" % (type(e).__name__, e))

print("\n=== %d PASS / %d FAIL ===" % (len(PASS), len(FAIL)), flush=True)
for f in FAIL:
    print("  x %s" % f, flush=True)
sys.exit(1 if FAIL else 0)
