# -*- coding: utf-8 -*-
"""零召回成因分型「只观测、不决策」门禁（2026-11-08）。

## 这条门禁防的是「把信号偷偷变成门禁」
上一轮讨论过：「问题不在知识库就判不合格」会带来三重负面作用，最重的一重是
**报错方向反过来** —— 把「检索失败」判成「知识缺失」，从此没人去修召回。
（实证：`错误 3.1：混淆 part 和 ref part`，库里 17 个 chunk，却零命中。）

所以 `agent/retrieval_gap.py` 被设计成**纯观测**：只往 `query_trace.detail` 写一个标签，
不改 route / 不改 hits / 不触发反问 / 不阻断生成。

本门禁的核心是 **P8/P9 两条反向判据**：
    P8  `_gap_info` 在 `agent/rag.py` 里除了「赋值」与「写 detail」之外**不得出现**
        —— 一旦有人写 `if _gap_info:` / `_gap_info.get("kind") == ...`，立刻变红。
    P9  `retrieval_gap` 不得被任何 `return` / `continue` / 条件表达式消费。
判据必须反向写：如果只断言「分类正确」，那么把分类接进门禁**反而全部通过**，
等于用门禁为「误伤用户」背书。

## 四态为什么不是三态
`recall_miss` 需要**强证据**（连续短语在库内且文档频次低）。
实测反例：`如何用 Python 写一个网页爬虫` → `Python` 在 OMG 规范里出现（顺带提及），
若按单词命中就断言 recall_miss，等于把域外问题"救"成有答案
（`query_keywords.py` 记过同一坑：gate 拒答准确率 1.000 → 0.800）。
故单词命中只给 `indeterminate`（承认分不出来），不硬塞进前三态。

## 变异自证
  M1 `_phrases` 恒返回 []（= 砍掉强证据来源）      → P2 必须红
  M2 去掉「文档频次低」约束（`0 < df <= df_cap`） → P3 必须红（Python 被误判成 recall_miss）
"""
import inspect
import os
import sqlite3
import sys
import tempfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.chdir(ROOT)

import agent.retrieval_gap as RG  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s%s" % ("PASS" if cond else "FAIL", name,
                         ("  <- " + str(detail)) if detail else ""), flush=True)


# ── 夹具 ────────────────────────────────────────────────────────────────
# document_chunks 优先用真库 DDL（本机），缺失时用最简 DDL 兜底（CI 无库也能跑）。
# 本模块只 SELECT content / name / intent / aliases，最简 DDL 足够，
# 因此本门禁可以进 pr 层 —— 与 `verify_rag_domain_guard`（必须真 DDL 才能和
# hybrid_search 对齐口径）不同。
_DDL_FALLBACK = """
CREATE TABLE document_chunks (
    id INTEGER PRIMARY KEY, document_id INTEGER, chunk_index INTEGER,
    content TEXT DEFAULT '', source_doc TEXT DEFAULT '', domain TEXT DEFAULT '',
    branch TEXT DEFAULT 'global', lifecycle_status TEXT DEFAULT 'stored',
    embed_version TEXT DEFAULT '', embedding TEXT
);
CREATE TABLE ontology_types (id TEXT PRIMARY KEY, name TEXT);
CREATE TABLE intent_taxonomy (intent TEXT PRIMARY KEY, aliases TEXT DEFAULT '[]');
"""

_ROWS = [
    # 短语 ref part 出现 2 次 ⇒ df=2（低），是强证据
    (1, "part def Vehicle { ref part driver : Person; }"),
    (2, "ref part 表示引用而非组合"),
    # 只有孤立单词 python ⇒ 弱证据（模拟"顺带提及"）
    (3, "The basic notation for successions uses Python style syntax"),
]


def _build():
    db = os.path.join(ROOT, "mbse.db")
    ddl = None
    if os.path.exists(db):
        src = sqlite3.connect(db)
        try:
            r = src.execute("SELECT sql FROM sqlite_master WHERE name='document_chunks' "
                            "AND type='table'").fetchone()
            ddl = r[0] if r else None
        finally:
            src.close()
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    if ddl:
        conn.execute(ddl)
        conn.execute("CREATE TABLE IF NOT EXISTS ontology_types (id TEXT PRIMARY KEY, name TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS intent_taxonomy "
                     "(intent TEXT PRIMARY KEY, aliases TEXT DEFAULT '[]')")
    else:
        conn.executescript(_DDL_FALLBACK)
    ins = ("INSERT INTO document_chunks (document_id, chunk_index, content, source_doc, "
           "domain, branch, lifecycle_status, embed_version) VALUES (1,?,?,'d.pdf',"
           "'sysml_norm','global','stored','bigram-tf')")
    for idx, content in _ROWS:
        conn.execute(ins, (idx, content))
    conn.execute("INSERT INTO ontology_types (id,name) VALUES ('t1','转发器')")
    conn.execute("INSERT INTO intent_taxonomy (intent,aliases) VALUES ('kb_query','[]')")
    conn.commit()
    return conn, path, bool(ddl)


print("[1] 夹具", flush=True)
_conn, _path, _real_ddl = _build()
check("F1 夹具可用（真库 DDL 优先，缺失时用最简 DDL）", _conn is not None,
      "source=%s" % ("mbse.db" if _real_ddl else "fallback DDL"))


def _build_fallback():
    """用最简 DDL 再造一份 —— 提前验证 **CI 无库时**的路径，
    不然 `tier=pr` 的绿灯可能只是"本机有库"这一件事的副产品。"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    c.executescript(_DDL_FALLBACK)
    for idx, content in _ROWS:
        c.execute("INSERT INTO document_chunks (document_id, chunk_index, content, "
                  "source_doc, domain, branch, lifecycle_status, embed_version) "
                  "VALUES (1,?,?,'d.pdf','sysml_norm','global','stored','bigram-tf')",
                  (idx, content))
    c.execute("INSERT INTO ontology_types (id,name) VALUES ('t1','转发器')")
    c.execute("INSERT INTO intent_taxonomy (intent,aliases) VALUES ('kb_query','[]')")
    c.commit()
    return c, path


_fb, _fb_path = _build_fallback()
_fb_kinds = {
    "recall": RG.classify(_fb, "混淆 part 和 ref part", 0)["kind"],
    "indet": RG.classify(_fb, "如何用 Python 写一个网页爬虫", 0)["kind"],
    "gap": RG.classify(_fb, "宽带载荷", 0)["kind"],
    "ground": RG.classify(_fb, "转发器", 0)["kind"],
}
check("F2 最简 DDL 兜底路径下四态仍正确（CI 无 mbse.db 时的真实行为）",
      _fb_kinds == {"recall": RG.KIND_RECALL_MISS, "indet": RG.KIND_INDETERMINATE,
                    "gap": RG.KIND_GAP, "ground": RG.KIND_GROUNDING_MISS},
      _fb_kinds)
_fb.close()
try:
    os.unlink(_fb_path)
except OSError:
    pass


# ── P1~P6：四态判定 ───────────────────────────────────────────────────
print("[2] 四态判定", flush=True)
_r = RG.classify(_conn, "随便什么", 3)
check("P1 有命中 → ok（分型只对零命中成立）", _r["kind"] == RG.KIND_OK, _r)

_r = RG.classify(_conn, "混淆 part 和 ref part", 0)
check("P2 连续短语在库且 df 低 → recall_miss",
      _r["kind"] == RG.KIND_RECALL_MISS and _r["evidence"],
      _r)

_r = RG.classify(_conn, "如何用 Python 写一个网页爬虫", 0)
check("P3 只有孤立单词在库 → indeterminate（不硬判 recall_miss）",
      _r["kind"] == RG.KIND_INDETERMINATE, _r)

_r = RG.classify(_conn, "宽带载荷", 0)
check("P4 短语与词都不在库 → knowledge_gap", _r["kind"] == RG.KIND_GAP, _r)

_r = RG.classify(_conn, "转发器", 0)
check("P5 词被本体认识但正文无依据 → grounding_miss",
      _r["kind"] == RG.KIND_GROUNDING_MISS, _r)

_r = RG.classify(_conn, "   ", 0)
check("P6 空查询不崩，归 knowledge_gap", _r["kind"] == RG.KIND_GAP, _r)

check("P7 四态取值全部合法（防拼写漂移出第五态）",
      all(RG.is_kind(k) for k in (RG.KIND_OK, RG.KIND_RECALL_MISS,
                                  RG.KIND_GROUNDING_MISS, RG.KIND_GAP,
                                  RG.KIND_INDETERMINATE)))


# ── P8/P9：反向判据（本门禁的灵魂）──────────────────────────────────────
print("[3] 反向判据：分型结果不得参与任何决策", flush=True)
_src = open(os.path.join(ROOT, "agent", "rag.py"), encoding="utf-8").read()
_lines = _src.split("\n")
_allowed = ("_gap_info =", '"retrieval_gap"', "from agent.retrieval_gap import")
_bad = []
for i, line in enumerate(_lines, 1):
    s = line.strip()
    if "_gap_info" not in s and "retrieval_gap" not in s:
        continue
    if s.startswith("#") or any(a in s for a in _allowed):
        continue
    _bad.append((i, s))

check("P8 `_gap_info` 只有赋值与写 detail，没有读取/分支消费", not _bad, _bad[:3])

_consumed = []
for i, line in enumerate(_lines, 1):
    s = line.strip()
    if s.startswith("#"):
        continue
    if ("_gap_info" in s or "retrieval_gap" in s) and any(
            k in s for k in ("if ", "elif ", "return ", "continue", "while ", "not ")):
        _consumed.append((i, s))
check("P9 没有任何 if/return/continue 消费分型结果（否则就是把它变成门禁）",
      not _consumed, _consumed[:3])

check("P10 开关存在且默认开启（关闭只影响打标，不影响检索）",
      '"gap_classify_shadow"' in _src and "True)" in _src)


# ── M1/M2：变异自证 ───────────────────────────────────────────────────
print("[4] 变异自证（断言必须有牙齿）", flush=True)


def _twin(func, mutate, tag, ns_extra=None):
    s = inspect.getsource(func)
    s2 = mutate(s)
    if s2 == s:
        check("M%s 变异未生效（锚点没命中，属脚本缺陷）" % tag, False, "源码未被改动")
        return None
    ns = {}
    if ns_extra:
        ns.update(ns_extra)
    try:
        exec(compile(s2, "<mutant_%s>" % tag, "exec"), ns)
    except Exception as e:
        check("M%s 变异体无法执行" % tag, False, "%s: %s" % (type(e).__name__, e))
        return None
    return ns


# M1：砍掉短语探针（recall_miss 的唯一强证据来源）
def _mut_no_phrase(s):
    return s.replace("    segs = _segments(query)", "    return []\n    segs = _segments(query)")


_ns1 = _twin(RG._phrases, _mut_no_phrase, "1")
if _ns1 is not None:
    _ph = _ns1["_phrases"]("混淆 part 和 ref part")
    check("M1 `_phrases` 恒返回 [] → recall_miss 用例必须红",
          _ph == [], "变异体返回=%r" % (_ph,))

# M2：把「孤立单词命中」直接当作 recall_miss —— 这正是要防的误判
#     （`Python` 在规范里顺带提及，一旦判成 recall_miss 就等于把域外问题救成有答案）。
#     注：一开始我把 M2 写成「去掉 df_cap 约束」，实测**不生效** ——
#     classify 的词循环本就用 `if df > 0:` 收进 weak，df_cap 只作用于短语探针；
#     而 Python 的病因是"没有短语命中"，不是"频次约束太松"。
def _mut_word_is_recall(s):
    return s.replace(
        "            # 只记下来，供 indeterminate 用；**不**据此断言 recall_miss\n"
        '            weak.append({"probe": w, "df": df, "via": "word"})',
        '            return {"kind": KIND_RECALL_MISS, "evidence": [{"probe": w, "df": df}], '
        '"checked": checked}')


_ns2 = _twin(RG.classify, _mut_word_is_recall, "2",
             ns_extra={k: getattr(RG, k) for k in dir(RG) if k.isupper() or k.startswith("_")})
if _ns2 is not None:
    _cls = _ns2["classify"]
    _got = _cls(_conn, "如何用 Python 写一个网页爬虫", 0)["kind"]
    check("M2 把单词命中当 recall_miss → Python 被误判（P3 有牙齿）",
          _got == RG.KIND_RECALL_MISS, "变异体判定=%s" % _got)

_conn.close()
try:
    os.unlink(_path)
except OSError:
    pass

print("\n%s" % ("=" * 62))
print("PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
if FAIL:
    for f in FAIL:
        print("  FAILED: %s" % f)
    sys.exit(1)
print("ALL_OK")
