# -*- coding: utf-8 -*-
"""查询分数口径分离门禁（2026-11-09）。

## 这条门禁防的是「把两种量纲塞进一列然后当同一件事用」
`query_trace.top_score` 历史上是"按 route 择一取首条分数"，但两种 route 的量纲
完全不同：

| route  | 分数含义                      | 实测量纲        |
|--------|------------------------------|-----------------|
| graph  | `graph_confidence` 多因子加权 | 0 ~ 1（样本 0.96）|
| vector | 融合检索分（chunk_hits）      | 0 ~ 0.63（样本 0.195）|

更糟的是**旧实现根本没取对**：`graph_results` 的元素是图谱实体（字段 id/name/type…），
既没有 `score` 也没有 `conf`，图路的真分数是独立变量 `graph_confidence`。
于是 `_f0.get("score") or _f0.get("conf") or 0` 对 graph/mixed 恒等于 **0**。

这不是"数字不准"，而是**方向反了**：
  · `core/online_eval._cand_low_conf` 按 `ORDER BY top_score ASC` 挑"最没底的查询"，
    恒为 0 的行永远排榜首 ⇒ 每次采样都采同一批 ⇒ **采样偏置且看不出来**；
  · 排障时看到 graph 路由 top_score=0，会以为"图谱检索质量为零"。

## 判据（每条都能变红）
  P1  graph/mixed 路由的 top_score 必须等于 `graph_confidence`，**不得为 0**
  P2  源码：rag.py 中图路取分**不得**再从 `_hit_src[0]` 上找 score/conf
  P3  `NULL`（该路无结果）与 `0.0`（有结果但分数为 0）必须可区分
  P4  三路原值都记，不择一
  P5  历史行回填：vector>0 → legacy_fusion；top=0 → legacy_zero
  P6  回填幂等（二次运行 0 变化）
  P7  旧库升级自愈：无新列的老表跑迁移能补齐
  P8  消费方 `_cand_low_conf` 不得再引用绝对值阈值 `low_conf_threshold`
  P9  `legacy_zero`（缺值冒充的低分）不得进入低置信采样池
  P10 **跨量纲**：graph 组的"最差"必须入池，尽管它的绝对值高于 chunk 组最高值
  P11 组内相对位次按**实际取出条数**归一（首条 0.00、末条 >0）

## 变异自证
  M1  `_LOW_CONF_KINDS` 里加回 `legacy_zero`        → P9 必须红
  M2  跨组按绝对分数排序（= 旧的 ORDER BY top_score）→ P10 必须红
"""
import inspect
import os
import re
import sqlite3
import sys
import tempfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.chdir(ROOT)

from database.migrations.glossary import _migrate_glossary_tables  # noqa: E402
from glossary import trace_query  # noqa: E402

PASS, FAIL, SKIPPED = [], [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s%s" % ("PASS" if cond else "FAIL", name,
                         ("  <- " + str(detail)) if detail else ""), flush=True)


def skip(name, why):
    """显式 SKIP：与 PASS 分开计数并打印原因。

    为什么不能静默略过：本工程已归档过「0 违规 vs 门禁没跑在日志上不可区分」，
    这里同理 —— 一条断言"因为环境缺失没跑"必须看得出没跑，不能长得像通过。
    """
    SKIPPED.append(name)
    print("  SKIP %s  <- %s" % (name, why), flush=True)


def _fresh_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    _migrate_glossary_tables(conn)     # 直接用真迁移建表，避免手写 DDL 与 schema 漂移
    return conn, path


def _cols(conn, table="query_trace"):
    return [r[1] for r in conn.execute("PRAGMA table_info(%s)" % table).fetchall()]


print("[1] 表结构", flush=True)
_conn, _path = _fresh_db()
_cols_all = _cols(_conn)
check("P0 query_trace 具备 score_kind / top_graph / top_chunk / top_vec 四列",
      all(c in _cols_all for c in ("score_kind", "top_graph", "top_chunk", "top_vec")),
      "实际列=%s" % _cols_all)

print("[2] 落库口径", flush=True)
# 模拟 agent/rag.py 在 graph 路由下的 trace 字典（数值取自实测 #6885）
_tid_g = trace_query(_conn, "转发器和接收机之间是什么关系", {
    "route": "graph", "domain": "all", "hit_count": 5, "latency_ms": 120,
    "top_score": 0.96, "score_kind": "graph_conf",
    "top_graph": 0.96, "top_chunk": None, "top_vec": None,
    "detail": {},
})
_row_g = _conn.execute("SELECT * FROM query_trace WHERE id=?", (_tid_g,)).fetchone()
check("P1 graph 路由 top_score 落的是 graph_confidence（不是 0）",
      _tid_g > 0 and abs(float(_row_g["top_score"]) - 0.96) < 1e-6,
      "top_score=%s score_kind=%s" % (_row_g["top_score"], _row_g["score_kind"]))
check("P1b graph 路由 score_kind='graph_conf'",
      _row_g["score_kind"] == "graph_conf", "实际=%r" % _row_g["score_kind"])

# 零命中：三路全无结果 ⇒ top_* 必须是 NULL，而不是 0
_tid_z = trace_query(_conn, "红烧肉怎么做", {
    "route": "vector", "domain": "all", "hit_count": 0, "latency_ms": 40,
    "top_score": 0, "score_kind": "",
    "top_graph": None, "top_chunk": None, "top_vec": None,
    "detail": {},
})
_row_z = _conn.execute("SELECT * FROM query_trace WHERE id=?", (_tid_z,)).fetchone()
_vals = (_row_z["top_graph"], _row_z["top_chunk"], _row_z["top_vec"])
check("P3 该路无结果记 NULL（与「有结果但 0 分」可区分）",
      all(v is None for v in _vals), "top_graph/chunk/vec=%r" % (_vals,))
check("P3b 无分数时 score_kind 为空串（不是伪造成某种 kind）",
      (_row_z["score_kind"] or "") == "", "实际=%r" % _row_z["score_kind"])

# mixed：图 + 文本同时有值 ⇒ 三路都记，不择一
_tid_m = trace_query(_conn, "宽带通信载荷", {
    "route": "mixed", "domain": "all", "hit_count": 1, "latency_ms": 90,
    "top_score": 0.3, "score_kind": "graph_conf",
    "top_graph": 0.3, "top_chunk": 0.136, "top_vec": 0.136,
    "detail": {},
})
_row_m = _conn.execute("SELECT * FROM query_trace WHERE id=?", (_tid_m,)).fetchone()
check("P4 mixed 路由三路原值都落库（择一即信息丢失）",
      abs(_row_m["top_graph"] - 0.3) < 1e-6 and abs(_row_m["top_chunk"] - 0.136) < 1e-6,
      "graph=%s chunk=%s vec=%s" % (_row_m["top_graph"], _row_m["top_chunk"], _row_m["top_vec"]))

print("[3] 源码：图路不得从实体对象上找分数", flush=True)
_rag_src = open(os.path.join(ROOT, "agent", "rag.py"), encoding="utf-8").read()
# ⚠️ 只扫**非注释行**：注释里刻意保留了旧写法作为反面说明（"旧实现：`_f0.get(...)`"），
#    若连注释一起扫，这条判据会因为"记录了历史错误"而变红 —— 那是假红。
_rag_code = "\n".join(ln for ln in _rag_src.splitlines()
                      if ln.strip() and not ln.strip().startswith("#"))
# 旧写法：_f0.get("score") or _f0.get("conf") or 0 —— graph_results 的元素没有这两个字段
_old_pat = re.compile(r"""_f0\.get\(\s*["']score["']\s*\)\s*or\s*_f0\.get\(\s*["']conf["']""")
_old_hit = _old_pat.search(_rag_code)
check("P2 rag.py 代码中不再出现「从 _hit_src[0] 找 score/conf」的旧写法",
      _old_hit is None,
      ("命中旧写法（graph 路由会恒为 0）：%r" % _old_hit.group(0)) if _old_hit else "")
_new_hit = re.search(r"_top_graph\s*=\s*round\(float\(graph_confidence", _rag_code)
check("P2b rag.py 的图路取值来自 graph_confidence",
      _new_hit is not None,
      "" if _new_hit is not None else
      "未找到 `_top_graph = round(float(graph_confidence...`（图路分数来源丢了）")

print("[4] 历史行回填", flush=True)
_conn.commit()
# 造 3 条「升级前」的行：score_kind 为 ''，只能从 route/top_score 反推
_conn.executemany(
    "INSERT INTO query_trace (query, route, top_score, score_kind) VALUES (?,?,?,'')",
    [("旧-向量有分", "vector", 0.05),
     ("旧-混合零值", "mixed", 0.0),
     ("旧-向量零命中", "vector", 0.0)])
_conn.commit()
_migrate_glossary_tables(_conn)
_got = {r["query"]: r["score_kind"] for r in
        _conn.execute("SELECT query, score_kind FROM query_trace WHERE query LIKE '旧-%'")}
check("P5 vector 且有分 → legacy_fusion", _got.get("旧-向量有分") == "legacy_fusion",
      "实际=%r" % _got.get("旧-向量有分"))
check("P5b top=0（含 mixed 的 bug 产物）→ legacy_zero",
      _got.get("旧-混合零值") == "legacy_zero" and _got.get("旧-向量零命中") == "legacy_zero",
      "实际=%r" % _got)
_before = sorted(_got.items())
_migrate_glossary_tables(_conn)
_after = sorted({r["query"]: r["score_kind"] for r in
                 _conn.execute("SELECT query, score_kind FROM query_trace WHERE query LIKE '旧-%'")}.items())
check("P6 回填幂等（二次运行 0 变化）", _before == _after, "%s -> %s" % (_before, _after))

print("[5] 旧库升级自愈", flush=True)
fd, _p2 = tempfile.mkstemp(suffix=".db")
os.close(fd)
_old = sqlite3.connect(_p2)
_old.row_factory = sqlite3.Row
_old.execute("""CREATE TABLE query_trace (
    id INTEGER PRIMARY KEY AUTOINCREMENT, query TEXT NOT NULL,
    normalized TEXT DEFAULT '', intent TEXT DEFAULT '', route TEXT DEFAULT '',
    domain TEXT DEFAULT '', hit_docs TEXT DEFAULT '[]', hit_count INTEGER DEFAULT 0,
    top_score REAL DEFAULT 0, latency_ms INTEGER DEFAULT 0,
    detail TEXT DEFAULT '{}', created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
_old.execute("INSERT INTO query_trace (query, route, top_score) VALUES ('老库行','vector',0.07)")
_old.commit()
_migrate_glossary_tables(_old)          # CREATE TABLE IF NOT EXISTS 是空操作，必须靠 ALTER 补列
_cols_old = _cols(_old)
check("P7 老表（无新列）跑迁移能补齐四列",
      all(c in _cols_old for c in ("score_kind", "top_graph", "top_chunk", "top_vec")),
      "补后列=%s" % _cols_old)
_r_old = _old.execute("SELECT score_kind FROM query_trace WHERE query='老库行'").fetchone()
check("P7b 老库行同时被回填（不会留 '' 变成采样黑洞）",
      _r_old and _r_old["score_kind"] == "legacy_fusion", "实际=%r" % (_r_old and _r_old["score_kind"]))
_old.close()

print("[6] 消费侧：跨量纲不得用绝对阈值", flush=True)
import core.online_eval as OE  # noqa: E402

_oe_src = inspect.getsource(OE._cand_low_conf)
# 注释里可以提到 low_conf_threshold（说明废弃原因），但代码里不得出现
_code_lines = [ln for ln in _oe_src.splitlines()
               if ln.strip() and not ln.strip().startswith("#")]
_still_uses = [ln.strip() for ln in _code_lines if "low_conf_threshold" in ln]
check("P8 _cand_low_conf 不再用 low_conf_threshold 做判断",
      not _still_uses,
      ("仍在用绝对值阈值：%s" % _still_uses) if _still_uses else "")

# ── 跨量纲夹具 ────────────────────────────────────────────────────────
# graph 组 [0.1, 0.3, 0.6, 0.9]；chunk 组 [0.01, 0.02, 0.03, 0.04]
# 正常实现：graph 组的"最差"=0.1 必需入池 —— 尽管 0.1 比 chunk 组**所有值都大**。
# 旧实现（绝对阈值 0.036）：0.1 > 0.036 ⇒ 永不入池 ⇒ 图路的低质量查询被系统性漏采。
_conn2, _p3 = _fresh_db()
_rows_x = ([("x-g%d" % i, "graph", v, "graph_conf") for i, v in enumerate([0.1, 0.3, 0.6, 0.9])]
           + [("x-c%d" % i, "vector", v, "chunk_score") for i, v in enumerate([0.01, 0.02, 0.03, 0.04])]
           + [("x-z%d" % i, "mixed", 0.0, "legacy_zero") for i in range(4)])
_conn2.executemany(
    "INSERT INTO query_trace (query, route, top_score, score_kind) VALUES (?,?,?,?)", _rows_x)
_conn2.commit()


def _run_low_conf(mod=OE, cap=20, q=0.25):
    fails = []
    rows = mod._cand_low_conf(_conn2, ("-1 day",), q, cap, fails)
    return rows, fails, " ".join(r.get("reason_detail", "") for r in rows)


_rows_lc, _fails_lc, _txt_lc = _run_low_conf()
check("P9 failures 为空（查询本身没坏）", not _fails_lc, str(_fails_lc))
check("P9b legacy_zero 不进低置信采样池（缺值不是最低分）",
      "kind=legacy_zero" not in _txt_lc, _txt_lc[:200])
check("P10 跨量纲：graph 组最差（0.1）入池，尽管它比 chunk 组最高值（0.04）还大",
      "kind=graph_conf" in _txt_lc and "top_score=0.1000" in _txt_lc, _txt_lc[:200])
check("P10b chunk 组最差（0.01）同样入池",
      "top_score=0.0100" in _txt_lc, _txt_lc[:200])
# 位次：两组各 4 条、q=0.25 ⇒ 各组取 1 条，位次都应 0.00
_rels = [float(m) for m in re.findall(r"组内位次=([\d.]+)", _txt_lc)]
check("P11 组内相对位次按实际取出条数归一（单条组为 0.00）",
      _rels and all(abs(v) < 1e-9 for v in _rels), "位次=%r" % _rels)

print("[7] 变异自证", flush=True)
# M1：把 legacy_zero 放回白名单 ⇒ 「缺值当最低分」的旧错误回来
_orig_kinds = OE._LOW_CONF_KINDS
try:
    OE._LOW_CONF_KINDS = tuple(_orig_kinds) + ("legacy_zero",)
    _, _, _txt_m1 = _run_low_conf()
    _m1_red = "kind=legacy_zero" in _txt_m1
    check("M1 白名单加回 legacy_zero → P9b 必须变红", _m1_red,
          "" if _m1_red else "变异体仍未取到 legacy_zero（该判据没有牙齿）")
finally:
    OE._LOW_CONF_KINDS = _orig_kinds


# M2：**精确复刻被替换掉的旧实现**（单一绝对阈值 0.036 + ORDER BY top_score ASC）。
# 为什么用复刻而不是改几个字符串：旧实现是一条 SQL 的语义（"所有量纲可比较"），
# 字符串替换只能改掉表象；复刻才能回答"换回旧代码，这两条判据会不会红"。
def _legacy_low_conf(conn, window_arg, quantile, cap, failures):
    try:
        rows = conn.execute(
            "SELECT id, query, intent, route, top_score, score_kind FROM query_trace "
            "WHERE top_score < ? AND created_at >= datetime('now', ?) "
            "ORDER BY top_score ASC, id ASC LIMIT ?",
            (0.036, window_arg[0], int(cap))).fetchall()
    except Exception as e:                                     # noqa: BLE001
        failures.append("low_conf: %s" % type(e).__name__)
        return []
    return [{"sample_type": "retrieval", "conversation_id": 0, "message_id": 0,
             "query_trace_id": int(r["id"]), "trace_id": "", "sample_reason": "low_conf",
             "reason_detail": "top_score=%.4f kind=%s route=%s" % (
                 float(r["top_score"] or 0), r["score_kind"] or "-", r["route"] or "-")}
            for r in rows]


_mod_legacy = type("Legacy", (), {"_cand_low_conf": staticmethod(_legacy_low_conf)})
_, _, _txt_old = _run_low_conf(_mod_legacy)
_old_takes_zero = "kind=legacy_zero" in _txt_old          # P9b 应红
_old_misses_graph = "top_score=0.1000" not in _txt_old     # P10 应红
check("M2 复刻旧实现（绝对阈值 0.036）→ P9b 与 P10 同时变红",
      _old_takes_zero and _old_misses_graph,
      "" if (_old_takes_zero and _old_misses_graph) else
      "变异体行为未回退（legacy_zero 入池=%s / graph 被漏=%s），说明判据无牙齿"
      % (_old_takes_zero, _old_misses_graph))

print("[8] 真库端到端（无库则 SKIP）", flush=True)
_db = os.path.join(ROOT, "mbse.db")
if not os.path.exists(_db):
    skip("P2c 真实 graph 路由落库 top_score>0", "无 mbse.db（CI 环境）")
else:
    try:
        from agent.rag import GraphRAG
        _rc = sqlite3.connect(_db)
        _rc.row_factory = sqlite3.Row
        _g = GraphRAG()
        _g.retrieve("转发器和接收机之间是什么关系", branch="dev", kb_scope={})
        _last = _rc.execute("SELECT route, top_score, score_kind FROM query_trace "
                            "ORDER BY id DESC LIMIT 1").fetchone()
        if _last is None:
            skip("P2c 真实 graph 路由落库 top_score>0", "trace 表为空")
        elif _last["route"] not in ("graph", "mixed"):
            skip("P2c 真实 graph 路由落库 top_score>0",
                 "本次路由为 %s（依赖真库实体数据，无法稳定复现 graph 路）" % _last["route"])
        else:
            check("P2c 真实 graph/mixed 路由落库 top_score>0 且 kind=graph_conf",
                  float(_last["top_score"] or 0) > 0 and _last["score_kind"] == "graph_conf",
                  "route=%s top_score=%s kind=%s" % (_last["route"], _last["top_score"], _last["score_kind"]))
    except Exception as _e:                                    # noqa: BLE001
        skip("P2c 真实 graph 路由落库 top_score>0", "运行异常：%s: %s" % (type(_e).__name__, _e))

# ── 收尾 ────────────────────────────────────────────────────────────────
for _p in (_path, _p2, _p3):
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
