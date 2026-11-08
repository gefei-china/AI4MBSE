# -*- coding: utf-8 -*-
"""查询 trace 记录「用户原话」门禁（2026-11-08）。

## 这条门禁防的是「样本里的问法不是用户说的话」
`query_trace.query` 一直写着"原始用户输入"，但实际内容是**两轮改造后**的串：

```
用户原话「宽带载荷」
  → pipeline 拼装：query = user_input + kb_hint + 槽位实体 + 附件前 600 字
  → agent/rag.py  : query = Glossary 归一化( query )   # 宽带载荷 → 宽带通信载荷
  → 落库          : query_trace.query = 上面这个串
```

后果不是"日志不好看"，而是**评测集被污染**：
  · `core/online_eval._query_of` 转正时把 `query_trace.query` 写进
    `qrels.jsonl` / `intent_samples` ⇒ 评测集里的"标准问法"是另一句话；
  · 标注员在队列里看到的是替换后的词 ⇒ 按错误的问法判"检索命中没命中"；
  · 而且这种失真**在数据里看不出来** —— 它长得跟"用户就是这么问的"一样。

历史行（6.5k 条）的原话**不可恢复**：上游在调 retrieve 之前就已拼装，进函数后
又被覆盖，任何字段都还原不出用户输入。所以本次**不改 `query` 的语义**，而是
新增 `query_raw` 单存原话 —— 若把 `query` 改成原话，同一列在新旧行上含义不同，
而表里没有任何东西标出分界（「同一语义多处各写一份，最后必然对不上」的反向坑）。

## 判据（每条都能变红）
  P0   新建库 / 老库升级后都有 query_raw 列
  P0b  历史行补列后是 NULL（不是 ''）——「没记录」与「记了空串」必须可区分
  P1   原话、检索串、归一化串三者各落各的列（`宽带载荷` / `宽带通信载荷` 不互相覆盖）
  P2   没传 query_raw ⇒ 回退为入参 query（**不得写 NULL**，否则"漏接线"长成"历史行"）
  P3   传空串/空白 ⇒ 同样回退（不写 ''）
  P4   超长截断 500（与 query 列同口径）
  P5   rag.retrieve：原话捕获必须在 Glossary 覆盖 query **之前**，且在函数体最前
  P6   两处 pipeline 调用都传 query_raw=user_input（漏一处 = 一半样本没原话）
  P7   trace 字典传的是 query_raw 而不是复用 query
  P8   queue_rows 附原话 + trace_raw_missing；历史行标记为缺失而非冒充
  P9   老库（无 query_raw 列）调 queue_rows **不抛**，且仍标记缺失
  P10  _query_of 优先原话；历史行退回 query 且 src='retrieval'（转正源头）
  P11  /api/knowledge/trace 能按**原话**检索到；老库缺列不 500
  P12  真库端到端：retrieve(query_raw=X) → 最新 trace 的 query_raw == X

## 变异自证
  M1  复刻旧 trace 写入（不写 query_raw）      → P1/P2 必须红
  M2  把原话捕获挪到归一化之后（源码文本变换）  → P5 必须红
  M3  复刻旧 _query_of（只读 query）            → P10 必须红
"""
import os
import sqlite3
import sys
import tempfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.chdir(ROOT)

from database.migrations.glossary import _migrate_glossary_tables      # noqa: E402
from database.migrations.misc import _migrate_online_eval_tables     # noqa: E402
from glossary import trace_query                                      # noqa: E402

PASS, FAIL, SKIPPED = [], [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s%s" % ("PASS" if cond else "FAIL", name,
                         ("  <- " + str(detail)) if detail else ""), flush=True)


def skip(name, why):
    SKIPPED.append(name)
    print("  SKIP %s  <- %s" % (name, why), flush=True)


def _fresh_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    _migrate_glossary_tables(conn)
    _migrate_online_eval_tables(conn)
    return conn, path


def _cols(conn, table="query_trace"):
    return [r[1] for r in conn.execute("PRAGMA table_info(%s)" % table).fetchall()]


def _raw_of(conn, tid):
    return conn.execute("SELECT * FROM query_trace WHERE id=?", (tid,)).fetchone()


print("[1] 表结构", flush=True)
_conn, _p1 = _fresh_db()
_c_new = _cols(_conn)
check("P0 新建库 query_trace 有 query_raw 列", "query_raw" in _c_new, "列=%s" % _c_new)

# 老库：无 query_raw 的旧表（照 2026-11 之前的 DDL）
_fd, _p2 = tempfile.mkstemp(suffix=".db")
os.close(_fd)
_old = sqlite3.connect(_p2)
_old.row_factory = sqlite3.Row
_old.execute("""CREATE TABLE query_trace (
    id INTEGER PRIMARY KEY AUTOINCREMENT, query TEXT NOT NULL,
    normalized TEXT DEFAULT '', intent TEXT DEFAULT '', route TEXT DEFAULT '',
    domain TEXT DEFAULT '', hit_docs TEXT DEFAULT '[]', hit_count INTEGER DEFAULT 0,
    top_score REAL DEFAULT 0, latency_ms INTEGER DEFAULT 0,
    detail TEXT DEFAULT '{}', created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
_old.execute("INSERT INTO query_trace (query, normalized, route, top_score) "
             "VALUES ('宽带通信载荷','宽带通信载荷','vector',0.2)")
_old.commit()
check("P0b 老库确实没有该列（夹具有效）", "query_raw" not in _cols(_old), _cols(_old))
_migrate_glossary_tables(_old)
check("P0c 老库跑迁移补齐 query_raw", "query_raw" in _cols(_old), _cols(_old))
_r_old = _old.execute("SELECT query_raw FROM query_trace WHERE id=1").fetchone()
check("P0d 历史行补列后是 NULL（不是空串）", _r_old["query_raw"] is None,
      "实际=%r（写 '' 会让「没记录」与「记了空串」混为一谈）" % (_r_old["query_raw"],))

print("[2] 写入口径：三件事各落各的列", flush=True)
_tid = trace_query(_conn, "宽带通信载荷 卫星", {
    "normalized": "宽带通信载荷 卫星",
    "query_raw": "宽带载荷 卫星",
    "route": "vector", "hit_count": 3, "top_score": 0.21, "detail": {},
})
_row = _raw_of(_conn, _tid)
check("P1 原话落 query_raw（不是被替换后的词）",
      (_row["query_raw"] or "") == "宽带载荷 卫星", "实际=%r" % (_row["query_raw"],))
check("P1b 检索串落 query、归一化串落 normalized（互不覆盖）",
      (_row["query"] or "") == "宽带通信载荷 卫星" and
      (_row["normalized"] or "") == "宽带通信载荷 卫星",
      "query=%r normalized=%r" % (_row["query"], _row["normalized"]))

_tid2 = trace_query(_conn, "只给检索串", {"normalized": "只给检索串", "route": "vector"})
_row2 = _raw_of(_conn, _tid2)
check("P2 没传 query_raw ⇒ 回退为入参 query（不得写 NULL）",
      (_row2["query_raw"] or "") == "只给检索串",
      "实际=%r（NULL 会让「漏传」冒充「历史行」）" % (_row2["query_raw"],))

_tid3 = trace_query(_conn, "空白回退", {"normalized": "空白回退", "query_raw": "   ",
                                        "route": "vector"})
_row3 = _raw_of(_conn, _tid3)
check("P3 传空白 ⇒ 回退为 query（不写空串）",
      (_row3["query_raw"] or "") == "空白回退", "实际=%r" % (_row3["query_raw"],))

_long = "长" * 800
_tid4 = trace_query(_conn, "长查询", {"normalized": "长查询", "query_raw": _long})
_row4 = _raw_of(_conn, _tid4)
check("P4 query_raw 截断到 500（与 query 列同口径）",
      len(_row4["query_raw"] or "") == 500, "长度=%d" % len(_row4["query_raw"] or ""))

print("[3] rag.py / pipeline 接线", flush=True)
_rag_src = open(os.path.join(ROOT, "agent", "rag.py"), encoding="utf-8").read()


def _order_ok(src):
    """原话捕获是否在「函数体最前」且「归一化覆盖之前」。

    返回 (ok, capture_line, normalize_line, getdb_line)；
    任何一项找不到都算不通过 —— 找不到 ≠ 通过（「0 违规」与「门禁没跑」不可区分）。
    """
    cap = norm = getdb = None
    for i, ln in enumerate(src.splitlines(), 1):
        s = ln.strip()
        if s.startswith("#"):
            continue
        if cap is None and s.startswith("_query_raw = "):
            cap = i
        if norm is None and s.startswith('query = _glossary_res["normalized"]'):
            norm = i
        if getdb is None and s.startswith("conn = get_db()"):
            getdb = i
    ok = (cap is not None and norm is not None and getdb is not None
          and cap < norm and cap < getdb)
    return ok, cap, norm, getdb


_ok, _cap, _norm, _getdb = _order_ok(_rag_src)
check("P5 原话捕获在归一化覆盖之前、且在函数体最前（先于 get_db）",
      _ok, "capture=%s normalize=%s get_db=%s" % (_cap, _norm, _getdb))

# 两处 pipeline 调用都必须传 —— 漏一处会让"一半样本有原话、一半没有"，
# 而缺失的那一半在表里长得跟历史行一样，无法区分原因。
for _f, _label in (("agent/pipeline_parts/execute.py", "non-stream"),
                   ("agent/pipeline_parts/stream.py", "stream")):
    _src = open(os.path.join(ROOT, _f), encoding="utf-8").read()
    _hit = "query_raw=user_input" in _src
    check("P6 %s 调用 retrieve 时传了 query_raw=user_input" % _label,
          _hit, "" if _hit else "%s 未接 query_raw ⇒ 该路径的原话永远是拼装串" % _f)

_rag_code = "\n".join(ln for ln in _rag_src.splitlines()
                      if ln.strip() and not ln.strip().startswith("#"))
check('P7 trace 字典里显式传 "query_raw": _query_raw（不复用 query）',
      '"query_raw": _query_raw' in _rag_code,
      "" if '"query_raw": _query_raw' in _rag_code else "未找到该键 ⇒ 原话没进 trace")

print("[4] 消费侧：标注面 / 转正源头", flush=True)
import core.online_eval as OE  # noqa: E402

_tid_hist = _conn.execute(
    "INSERT INTO query_trace (query, normalized, query_raw, route) "
    "VALUES ('宽带通信载荷','宽带通信载荷',NULL,'vector')").lastrowid
_conn.execute("INSERT INTO online_eval_queue (sample_type, query_trace_id, sample_reason, "
              "status) VALUES ('retrieval',?,'low_conf','pending')", (_tid,))
_conn.execute("INSERT INTO online_eval_queue (sample_type, query_trace_id, sample_reason, "
              "status) VALUES ('retrieval',?,'low_conf','pending')", (_tid_hist,))
_conn.commit()
_rows, _total = OE.queue_rows(_conn, status="pending", limit=10)
_by_tid = {int(r.get("query_trace_id") or 0): r for r in _rows}
_r_new = _by_tid.get(_tid) or {}
_r_his = _by_tid.get(_tid_hist) or {}
check("P8 queue_rows 给新样本带用户原话（不是归一化串）",
      _r_new.get("trace_query_raw") == "宽带载荷 卫星",
      "trace_query_raw=%r trace_query=%r" % (_r_new.get("trace_query_raw"),
                                             _r_new.get("trace_query")))
check("P8b 历史样本标记 trace_raw_missing=True（不拿归一化串冒充原话）",
      _r_his.get("trace_raw_missing") is True and _r_his.get("trace_query_raw") is None,
      "missing=%r raw=%r" % (_r_his.get("trace_raw_missing"),
                             _r_his.get("trace_query_raw")))

# 老库（无 query_raw 列）：queue_rows 不得抛，且不得伪造原话
_fd, _p3 = tempfile.mkstemp(suffix=".db")
os.close(_fd)
_oldq = sqlite3.connect(_p3)
_oldq.row_factory = sqlite3.Row
# 队列表**跑真迁移**（手抄 DDL 迟早与 schema 漂移）；只有 query_trace 需要
# 手写"旧形态"——那正是本夹具要复现的东西（无 query_raw 列的老表）。
_migrate_online_eval_tables(_oldq)
_oldq.execute("""CREATE TABLE query_trace (
    id INTEGER PRIMARY KEY AUTOINCREMENT, query TEXT NOT NULL,
    normalized TEXT DEFAULT '', intent TEXT DEFAULT '', route TEXT DEFAULT '',
    domain TEXT DEFAULT '', hit_docs TEXT DEFAULT '[]', hit_count INTEGER DEFAULT 0,
    top_score REAL DEFAULT 0, latency_ms INTEGER DEFAULT 0,
    detail TEXT DEFAULT '{}', created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
_oldq.execute("INSERT INTO query_trace (query, normalized) VALUES ('宽带通信载荷','宽带通信载荷')")
_oldq.execute("INSERT INTO online_eval_queue (sample_type, query_trace_id) "
              "VALUES ('retrieval', 1)")
_oldq.commit()
try:
    _orows, _ = OE.queue_rows(_oldq, status="pending", limit=5)
    _e = None
except Exception as _ex:                                       # noqa: BLE001
    _orows, _e = [], _ex
check("P9 老库（无 query_raw 列）调 queue_rows 不抛异常",
      _e is None, "%s: %s" % (type(_e).__name__, _e) if _e else "")
check("P9b 老库样本同样标记缺失（不伪造原话）",
      bool(_orows) and _orows[0].get("trace_raw_missing") is True
      and _orows[0].get("trace_query_raw") is None,
      "" if _orows else "未取到行")

# 转正源头：qrels / intent_samples 里的 query 必须是原话
_q, _intent, _src = OE._query_of(_conn, {"query_trace_id": _tid})
check("P10 转正取 query 优先原话（qrels 里存的就是用户问的那句）",
      _q == "宽带载荷 卫星" and _src == "raw", "query=%r src=%r" % (_q, _src))
_q2, _, _src2 = OE._query_of(_conn, {"query_trace_id": _tid_hist})
check("P10b 历史行退回 query 且标 src='retrieval'（失真可追溯，不冒充 raw）",
      _q2 == "宽带通信载荷" and _src2 == "retrieval",
      "query=%r src=%r" % (_q2, _src2))

print("[5] trace 检索能按原话命中", flush=True)
from routers.glossary import trace_list  # noqa: E402

_res = trace_list(limit=10, query="宽带载荷", conn=_conn)
_ids_found = {int(it["id"]) for it in _res.get("items") or []}
check("P11 按**用户原话**能检索到（此前只搜 query/normalized，搜原话查不到）",
      _tid in _ids_found, "命中 id=%s（目标 %s）" % (sorted(_ids_found), _tid))
try:
    _res_old = trace_list(limit=5, query="宽带", conn=_oldq)
    _e11 = None
except Exception as _ex:                                       # noqa: BLE001
    _res_old, _e11 = {}, _ex
check("P11b 老库（缺 query_raw 列）调 trace 接口不 500，退回两列匹配",
      _e11 is None and bool(_res_old.get("items")),
      "%s: %s" % (type(_e11).__name__, _e11) if _e11 else "")

print("[6] 前端真的消费了（后端加字段 ≠ 页面能看到）", flush=True)
_js = open(os.path.join(ROOT, "static", "js", "mods", "31-admin.js"), encoding="utf-8").read()
check("P13 标注面板引用了 trace_query_raw（不是只让 API 返回就完事）",
      "trace_query_raw" in _js,
      "" if "trace_query_raw" in _js else "31-admin.js 未引用该字段 ⇒ 原话躺在 API 里没人看")
check("P13b 表头新增「查询」列", "<th>查询</th>" in _js,
      "" if "<th>查询</th>" in _js else "表头未加列，单元格渲染了也不显示")
# 这一条防"退化成拿归一化串冒充原话"：历史行的兜底展示必须带标记文案。
check("P13c 历史行兜底展示带「原话未记录」标记（不静默冒充）",
      "原话未记录" in _js,
      "" if "原话未记录" in _js else
      "兜底分支没有标记文案 ⇒ 标注员会把归一化后的词当成用户原话")

print("[7] 真库端到端（无库/无实体则 SKIP）", flush=True)
_db = os.path.join(ROOT, "mbse.db")
if not os.path.exists(_db):
    skip("P12 真实 retrieve(query_raw=…) 落库原话", "无 mbse.db（CI 环境）")
else:
    try:
        from agent.rag import GraphRAG
        _rc = sqlite3.connect(_db)
        _rc.row_factory = sqlite3.Row
        _RAW = "宽带载荷 raw-query-probe"
        GraphRAG().retrieve(_RAW + " 卫星", branch="dev", kb_scope={}, query_raw=_RAW)
        _last = _rc.execute("SELECT query, normalized, query_raw FROM query_trace "
                            "ORDER BY id DESC LIMIT 1").fetchone()
        if _last is None:
            skip("P12 真实 retrieve(query_raw=…) 落库原话", "trace 表为空")
        else:
            check("P12 真实链路落库原话 == 传入 query_raw，且 query 是另一串",
                  (_last["query_raw"] or "") == _RAW
                  and (_last["query"] or "") != _RAW,
                  "query_raw=%r query=%r" % (_last["query_raw"], _last["query"]))
        _rc.close()
    except Exception as _e:                                    # noqa: BLE001
        skip("P12 真实 retrieve(query_raw=…) 落库原话",
             "运行异常：%s: %s" % (type(_e).__name__, _e))

print("[8] 变异自证", flush=True)


# M1：复刻旧写入（INSERT 不带 query_raw 列）
def _legacy_trace_query(conn, query, trace):
    import json as _json
    conn.execute(
        "INSERT INTO query_trace (query, normalized, intent, route, domain, hit_docs, "
        "hit_count, top_score, latency_ms, detail) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (query[:500], (trace.get("normalized") or query)[:500], "", "vector", "",
         "[]", 0, 0, 0, _json.dumps({})))
    conn.commit()
    return conn.execute("SELECT MAX(id) FROM query_trace").fetchone()[0]


_tid_m1 = _legacy_trace_query(_conn, "宽带通信载荷", {"normalized": "宽带通信载荷"})
_m1_row = _raw_of(_conn, _tid_m1)
_m1_red = (_m1_row["query_raw"] is None)
check("M1 复刻旧写入（不记 query_raw）→ P1「原话落库」必须变红",
      _m1_red, "" if _m1_red else "旧写法居然也记了原话，说明 P1 没有牙齿")

# M2：把原话捕获挪到归一化之后（源码文本变换，再跑同一个判据函数）。
#   注：变换后的源码**不执行**，只做行号顺序判定 —— 目的是证明 P5 看的是
#   "顺序"而不是"存在"（若只判存在，这段挪动照样通过，判据就是空转）。
_mut = _rag_src.replace(
    "        _query_raw = query_raw if isinstance(query_raw, str) and query_raw.strip() else query\n",
    "", 1).replace(
    '                query = _glossary_res["normalized"] or query  # 归一化后检索（v2→SysML_V2）',
    '                query = _glossary_res["normalized"] or query  # 归一化后检索（v2→SysML_V2）\n'
    '                _query_raw = query_raw if isinstance(query_raw, str) and query_raw.strip() else query',
    1)
_m2_ok, _m2_cap, _m2_norm, _ = _order_ok(_mut)
check("M2 把原话捕获挪到归一化之后 → P5「捕获在覆盖之前」必须变红",
      (not _m2_ok) and _m2_cap is not None and _m2_cap > _m2_norm,
      "" if not _m2_ok else
      "变换后判据仍通过（capture=%s normalize=%s），说明 P5 只看存在性、不看顺序"
      % (_m2_cap, _m2_norm))


# M3：复刻旧 _query_of（只读 query，不看 query_raw）
def _legacy_query_of(conn, r):
    qtid = int(r.get("query_trace_id") or 0)
    if qtid:
        row = conn.execute("SELECT query, intent FROM query_trace WHERE id=?",
                           (qtid,)).fetchone()
        if row:
            return (row["query"] or ""), (row["intent"] or ""), "retrieval"
    return "", "", ""


_m3 = _legacy_query_of(_conn, {"query_trace_id": _tid})
_m3_red = (_m3[0] == "宽带通信载荷 卫星" and _m3[2] != "raw")
check("M3 复刻旧 _query_of（只读 query）→ P10「优先原话」必须变红",
      _m3_red, "" if _m3_red else "旧实现居然也取到原话，说明 P10 没有牙齿")

# ── 收尾 ────────────────────────────────────────────────────────────────
for _p in (_p1, _p2, _p3):
    try:
        os.unlink(_p)
    except OSError:
        pass

print("\n=== %d PASS / %d FAIL / %d SKIP ===" % (len(PASS), len(FAIL), len(SKIPPED)), flush=True)
if SKIPPED:
    print("SKIP 项（未验证，不等于通过）：%s" % ", ".join(SKIPPED), flush=True)
if FAIL:
    print("FAIL: %s" % ", ".join(FAIL), flush=True)
sys.exit(1 if FAIL else 0)
