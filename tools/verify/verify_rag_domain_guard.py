# CI-OPTIONAL: [local] tier=local（见 tools/gates_manifest.json）：夹具要从**真库 mbse.db** 复制 document_chunks 的真实 DDL（手写 DDL 会与 schema 漂移，测出来的「口径一致」是假的）；CI 无库 ⇒ 跑不了，故本门禁**失败而非跳过**（skipped 与 passed 不可区分）。本机已实测通过，改法须同时改清单 tier 并接线，不允许只删这行
# -*- coding: utf-8 -*-
"""检索侧「域约束可用性」前置校验门禁（2026-11-08）。

## 这条门禁真正防的是什么
不是"某个域名写错了"，而是 **校验口径与检索口径漂移**。

真库实测（`mbse.db`）：
    glossary            : satellite_comms(3) / 能力中心(15)  → 库内 0 chunk
    glossary_concepts   : satellite_comms(30) / loitering_munition(12) → 库内 0 chunk
合计 60+ 条术语指向 **0 chunk 的域**。任何被这些术语 `force_domain` 的查询，
第一步必然零命中，只能靠 P1-7 的 `_retry_without_domain` 事后撤域补救。

实证一条（`错误 3.1：混淆 part 和 ref part`，query_trace 2026-10-16）：
    同查询、同 route、同 domain，**4 分钟内出现 20 → 0 → 20**
    detail.glossary = ["PA"]  ← Glossary 把 part 的缩写判成卫星通信术语
    而含 `ref part` 的 17 个 chunk 全部在 `sysml_norm`
⇒ "零命中"里混着「知识没有」和「域误判把知识滤掉了」两种，必须分开。
（这也是上一轮"覆盖度判定会不会误伤"讨论里最硬的一条反例。）

## 判据为什么不写具体域名
写 `assert usable("satellite_comms") is False` 是在测**当前数据**，不是测代码；
文档一导入就假红。所以本门禁只用**夹具自造的三个域**（有数据 / 只有已下线数据 /
不存在），判据是数据驱动的，新增域自动生效。

## P2 是本门禁的核心
对夹具里每个域，同时问两个函数：
    _domain_usable(...)                  ← 校验侧结论
    bool(hybrid_search(domain=X).hits)   ← 检索侧事实
断言两者**逐项相等**。任一侧单独改动（校验漏了 lifecycle、或检索加了新的过滤）
都会让这条红 —— 这正是"两套实现口径必须同源"的可执行版本。

## 变异自证（缺一条就可能是空转断言）
  M1 `_domain_usable` 恒 return True（= 退回修复前"从不校验"）→ P2 的 retired/ghost 必须红
  M2 砍掉 lifecycle 子句（= 口径漂移的典型形态）        → P1c 与 P2 必须红
"""
import inspect
import os
import sqlite3
import sys
import tempfile
import textwrap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.chdir(ROOT)

from agent.rag import GraphRAG  # noqa: E402
from knowledge_engine import hybrid_search  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s%s" % ("PASS" if cond else "FAIL", name,
                         ("  <- " + str(detail)) if detail else ""), flush=True)


# ── 夹具：真库 DDL + 自造三态域 ────────────────────────────────────────────
# 域的三态是刻意选的，覆盖「可用 / 有数据但全下线 / 压根没有」：
#   live     : 1 条 stored      → 应可用
#   retired  : 1 条 deprecated  → 默认应**不可用**（检索也看不到）
#   ghost    : 0 条             → 应不可用
_FIXTURE_ROWS = [
    (1, 0, "part def Vehicle { ref part driver : Person; }", "live.pdf", "live", "stored"),
    (2, 1, "ref part 表示引用而非组合 ref part", "retired.pdf", "retired", "deprecated"),
]
_PROBE = "ref part"


def _build_fixture():
    """返回 (conn, path)。DB 不存在时返回 (None, None) —— 由调用方判 FAIL。"""
    db = os.path.join(ROOT, "mbse.db")
    if not os.path.exists(db):
        return None, None
    src = sqlite3.connect(db)
    try:
        row = src.execute(
            "SELECT sql FROM sqlite_master WHERE name='document_chunks' AND type='table'"
        ).fetchone()
    finally:
        src.close()
    if not row or not row[0]:
        return None, None

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(row[0])
    ins = ("INSERT INTO document_chunks (document_id, chunk_index, content, source_doc, "
           "domain, branch, lifecycle_status, embed_version) VALUES (?,?,?,?,?,?,?,?)")
    for doc_id, idx, content, doc, dom, life in _FIXTURE_ROWS:
        conn.execute(ins, (doc_id, idx, content, doc, dom, "global", life, "bigram-tf"))
    conn.commit()
    return conn, path


print("[1] 夹具构造（真库 DDL，三态域）", flush=True)
_conn, _path = _build_fixture()
check("F1 真库与 document_chunks DDL 可用（local 层门禁要求本机有库）",
      _conn is not None, "mbse.db 缺失或无 document_chunks，本门禁不做「跳过」处理")
if _conn is None:
    print("\nFAIL: 夹具无法构造，后续判据全部未执行", flush=True)
    sys.exit(1)


# ── P1：纯函数三态 ────────────────────────────────────────────────────────
print("[2] _domain_usable 三态判定", flush=True)
_usable = lambda d, docs=None, inc=False: GraphRAG._domain_usable(_conn, d, docs, inc)  # noqa: E731

check("P1a 有数据的域 → 可用", _usable("live") is True, _usable("live"))
check("P1b 只有已下线数据的域（默认口径）→ 不可用", _usable("retired") is False, _usable("retired"))
check("P1c 同一域显式 include_deprecated → 可用", _usable("retired", None, True) is True,
      _usable("retired", None, True))
check("P1d 库里不存在的域 → 不可用", _usable("ghost") is False, _usable("ghost"))
check("P1e 来源文档过滤到不存在文档 → 不可用", _usable("live", ["nope.pdf"]) is False,
      _usable("live", ["nope.pdf"]))
check("P1f 空域名 → 直接放行（不改变原行为）", GraphRAG._domain_usable(_conn, "", None, False) is True)


# ── P2：校验口径 == 检索口径（核心判据）────────────────────────────────────
print("[3] 口径一致性：校验结论必须与真实检索结果逐项相同", flush=True)
for dom in ("live", "retired", "ghost"):
    hits = hybrid_search(_conn, _PROBE, top_k=5, branches=None, domain=dom).get("hits") or []
    got = bool(hits)
    want = _usable(dom)
    check("P2 域 %-8r 校验=%-5s 检索命中=%-2d ⇒ 一致" % (dom, want, len(hits)),
          want == got, "usable=%s hits=%d" % (want, len(hits)))


# ── P3：接线断言（改了函数但没接进 retrieve = 白改）────────────────────────
print("[4] 接线：校验必须在 hybrid_search **之前**发生", flush=True)
_rag_src = open(os.path.join(ROOT, "agent", "rag.py"), encoding="utf-8").read()
_i_guard = _rag_src.find("self._domain_usable(conn, force_domain")
_i_hybrid = _rag_src.find("domain=force_domain if force_domain", _i_guard if _i_guard > 0 else 0)
check("P3a retrieve 里调用了 _domain_usable", _i_guard > 0, "offset=%d" % _i_guard)
check("P3b 调用位置在 hybrid_search 施加 domain 之前", 0 < _i_guard < _i_hybrid,
      "guard=%d hybrid=%d" % (_i_guard, _i_hybrid))
check("P3c 被跳过的域写进 trace detail（否则域误判在日志里消失）",
      '"domain_skipped": _domain_skipped' in _rag_src)
check("P3d 撤域重试的结果打 recall_via 标记（可统计域误判率）",
      'h["recall_via"] = "domain_retry"' in _rag_src)


# ── M1/M2：变异自证 ───────────────────────────────────────────────────────
print("[5] 变异自证（断言必须有牙齿）", flush=True)
_src = textwrap.dedent(inspect.getsource(GraphRAG._domain_usable))


def _twin(mutate, tag):
    s = mutate(_src)
    if s == _src:
        check("M%s 变异未生效（锚点没命中，属脚本缺陷）" % tag, False, "源码未被改动")
        return None
    ns = {"__name__": "mutant_%s" % tag}
    try:
        exec(compile(s, "<mutant_%s>" % tag, "exec"), ns)
    except Exception as e:  # 变异体语法都过不去 → 脚本缺陷，必须报出来
        check("M%s 变异体无法执行" % tag, False, "%s: %s" % (type(e).__name__, e))
        return None
    fn = ns.get("_domain_usable")
    return getattr(fn, "__func__", fn)


def _mut_always_true(s):
    """M1：恒返回 True —— 精确等价于「修复前从不校验」。

    ⚠️ 锚点缩进必须是 **8 空格**：`inspect.getsource` + `textwrap.dedent` 之后，
    `def` 顶格、`try:` 4、try 体内语句 8（这里曾写成 12 导致变异静默未生效）。
    """
    return s.replace("        return bool(row)", "        return True")


def _mut_drop_lifecycle(s):
    """M2：砍掉 lifecycle 子句 —— 「口径漂移」最典型的形态。"""
    return s.replace("lsql, lparams = _lifecycle_clause(inc_dep)",
                     'lsql, lparams = "", []')


_m1 = _twin(_mut_always_true, "1")
if _m1 is not None:
    _bad = [d for d in ("retired", "ghost") if _m1(_conn, d, None, False) is not False]
    check("M1 恒 True（退回不校验）→ retired/ghost 必须判错", bool(_bad), "漏判=%s" % _bad)

_m2 = _twin(_mut_drop_lifecycle, "2")
if _m2 is not None:
    # 砍掉 lifecycle 后，retired 域（唯一那行是 deprecated）会被当成"有数据"
    # ⇒ 变异体给出 **True**（错答案），从而证明 P1b 的 False 不是空转。
    _got2 = _m2(_conn, "retired", None, False)
    check("M2 砍掉 lifecycle → retired 域被误判为可用（P1b 有牙齿）",
          _got2 is True, "变异体返回=%s" % _got2)

_conn.close()
try:
    os.unlink(_path)
except OSError:
    pass  # Windows 上 connect 句柄可能仍被占用；临时文件残留不影响判据

print("\n%s" % ("=" * 62))
print("PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
if FAIL:
    for f in FAIL:
        print("  FAILED: %s" % f)
    sys.exit(1)
print("ALL_OK")
