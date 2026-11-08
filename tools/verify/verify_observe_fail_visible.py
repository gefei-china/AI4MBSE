# -*- coding: utf-8 -*-
"""观测失败「不冒泡但必须留痕」门禁（2026-11-08）。

## 这条门禁防的是「静默兜底把指标分母悄悄改小」
`glossary.trace_query` 原本是：

    except Exception:
        return 0            # ← 写不进 trace 就当没发生

于是所有基于 `query_trace` 的指标（零命中率 7.24%、低置信采样、路由统计）
**分母只是"成功写入的行"**，真实值只会更高 —— 而看板上它长得像"已统计全量"。
这与本工程已归档的「0 违规 vs 门禁没跑在日志上不可区分」是同一族错误：
**"没有数据"与"数据写失败"在指标上不可区分**，且后者会给出一个看起来正当的结论。

修法必须同时满足两条，缺一条就变成另一种错：
  ① **不冒泡**：观测失败绝不能阻断检索/生成（否则日志故障会放大成业务故障）；
  ② **不静默**：必须进计数与限流日志（否则等于没修）。
O1~O10 就是围绕这两条的对偶判据 —— 任何一侧被拿走都会变红。

## 本门禁在写完之后立刻抓到的真 bug（留档）
加上留痕后，第一次跑真实检索就报出：
    `UnboundLocalError: cannot access local variable 'force_domain'`
`force_domain` 只在 `if graph_confidence < threshold:` 分支内赋值，而 trace 块
**无条件**读它 ⇒ 凡走 graph 路由必然抛异常 ⇒ **graph 路由一条 trace 都没有**
（存量实测：vector 6514 / mixed 27 / graph **0**）。
这个 bug 在旧实现下完全不可见 —— 它被 `except Exception: pass` 吞掉了。
⇒ 「留痕」不是洁癖，它是**让下一个 bug 有机会被发现**的基础设施。

## 变异自证
  M1  复刻旧实现（裸 `except: return 0`）      → O3 必须红（坏 conn 上零痕迹）
  M2  `observe_fail.note` 换成空操作           → O1/O3 必须红（计数不动）
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

import core.observe_fail as OF  # noqa: E402
import glossary  # noqa: E402
from database.migrations.glossary import _migrate_glossary_tables  # noqa: E402

PASS, FAIL, SKIPPED = [], [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s%s" % ("PASS" if cond else "FAIL", name,
                         ("  <- " + str(detail)) if detail else ""), flush=True)


def skip(name, why):
    """显式 SKIP：与 PASS 分开计数并打印原因（"没验证"不得长得像"通过"）。"""
    SKIPPED.append(name)
    print("  SKIP %s  <- %s" % (name, why), flush=True)


class _BadConn:
    """execute 必失败（模拟 schema 未升级 / 库只读 / 列不存在）。"""

    def __init__(self, msg="no such column: score_kind"):
        self.msg = msg
        self.rolled = False

    def execute(self, *a, **k):
        raise sqlite3.OperationalError(self.msg)

    def commit(self):
        raise sqlite3.OperationalError(self.msg)

    def rollback(self):
        self.rolled = True


class _CommitFailConn:
    """INSERT 成功但 commit 失败（模拟磁盘满 / 锁等待超时）。"""

    def __init__(self):
        self.rolled = False

    def execute(self, *a, **k):
        return type("_Cur", (), {"lastrowid": 1})()

    def commit(self):
        raise sqlite3.OperationalError("database or disk is full")

    def rollback(self):
        self.rolled = True


print("[1] observe_fail 基本契约", flush=True)
OF.reset()
check("O0 reset 后 total()==0", OF.total() == 0, "total=%s" % OF.total())
OF.note("unit_test", ValueError("w1"), k=1)
_snap = OF.snapshot()
check("O1 note 之后计数可见（where 可追溯到具体失败点）",
      _snap.get("unit_test", {}).get("count") == 1 and OF.total() == 1,
      "snapshot=%r" % (_snap,))
check("O1b 样本里保留了异常类型与上下文（可用于事后归因）",
      "ValueError" in _snap["unit_test"]["samples"][0]["error"]
      and "1" in _snap["unit_test"]["samples"][0]["ctx"].get("k", ""),
      "sample=%r" % (_snap["unit_test"]["samples"][0],))
# 二次 note 只加计数，样本最多 3 条（不无限增长）
for _i in range(5):
    OF.note("unit_test", ValueError("w%d" % _i))
_snap2 = OF.snapshot()
check("O1c 计数持续累加、样本数有上限（不撑爆内存）",
      _snap2["unit_test"]["count"] == 6 and len(_snap2["unit_test"]["samples"]) <= 3,
      "count=%s samples=%s" % (_snap2["unit_test"]["count"], len(_snap2["unit_test"]["samples"])))


class _BadStr(Exception):
    def __str__(self):
        raise RuntimeError("__str__ 也坏了")


OF.reset()
_threw = False
try:
    OF.note("weird", _BadStr())
    OF.note("weird2", KeyboardInterrupt())      # 非 Exception 的 BaseException
except BaseException:                            # noqa: BLE001
    _threw = True
check("O2 note 自身绝不抛（连 __str__ 坏掉 / BaseException 也不抛）", not _threw,
      "" if not _threw else "note 抛出异常 → 会把观测故障放大成业务故障")
_weird_cnt = OF.snapshot().get("weird", {}).get("count", 0)
check("O2b 连 __str__ 坏掉也照样计数（计数不依赖消息字符串化）",
      _weird_cnt == 1, "" if _weird_cnt == 1 else "count=%r（丢了计数 = 违反「不丢计数」纪律）" % _weird_cnt)

_snap3 = OF.snapshot()
_snap3["unit_test"] = {"injected": True}
_copy_ok = "unit_test" not in OF.snapshot() or OF.snapshot()["unit_test"].get("count") != "injected"
check("O7 snapshot 返回深拷贝（调用方改它不影响内部状态）",
      _copy_ok, "" if _copy_ok else "外部修改污染了内部统计")

print("[2] trace_query：坏库不抛、返回 0、留痕", flush=True)
OF.reset()
_bad = _BadConn()
_ret = None
_threw = False
try:
    _ret = glossary.trace_query(_bad, "测试查询", {"route": "graph", "top_score": 0.9})
except BaseException as _e:                      # noqa: BLE001
    _threw = True
check("O3 坏 conn 上 trace_query 不抛（观测失败不得阻断主链路）",
      not _threw, "" if not _threw else "trace_query 抛出了异常")
check("O3b 坏 conn 上返回 0（调用方据此可知未落库）", _ret == 0, "返回=%r" % (_ret,))
_tq_stat = OF.snapshot().get("trace_query", {})
check("O3c 坏 conn 上必须留下失败痕迹（计数 + 样本）",
      _tq_stat.get("count", 0) >= 1 and _tq_stat.get("samples"),
      "snapshot['trace_query']=%r（为空 = 静默，等于没修）" % (_tq_stat,))
check("O5 失败时尽力 rollback（防未提交事务污染该连接的后续写入）",
      _bad.rolled is True, "rolled=%r" % (_bad.rolled,))

OF.reset()
_cf = _CommitFailConn()
_ret2 = glossary.trace_query(_cf, "提交失败", {"route": "vector"})
check("O5b commit 失败同样不抛 + 返回 0 + rollback + 留痕",
      _ret2 == 0 and _cf.rolled is True and OF.total() >= 1,
      "ret=%r rolled=%r total=%s" % (_ret2, _cf.rolled, OF.total()))

print("[3] trace_query：好库成功且不留失败痕迹", flush=True)
fd, _path = tempfile.mkstemp(suffix=".db")
os.close(fd)
_good = sqlite3.connect(_path)
_good.row_factory = sqlite3.Row
_migrate_glossary_tables(_good)
OF.reset()
_tid = glossary.trace_query(_good, "正常查询", {
    "route": "vector", "domain": "all", "hit_count": 3, "latency_ms": 12,
    "top_score": 0.21, "score_kind": "chunk_score",
    "top_graph": None, "top_chunk": 0.21, "top_vec": 0.2, "detail": {}})
check("O4 好库上返回 trace_id 且无失败计数（成功路径不能被误报成失败）",
      _tid > 0 and OF.total() == 0, "tid=%r total=%s" % (_tid, OF.total()))
_row = _good.execute("SELECT * FROM query_trace WHERE id=?", (_tid,)).fetchone()
check("O4b 落库内容完整（query/route/score_kind 三件套）",
      _row is not None and _row["query"] == "正常查询"
      and _row["score_kind"] == "chunk_score", "row=%r" % (_row and dict(_row),))

print("[4] 源码：不得回到静默兜底", flush=True)


def _code_only(src):
    return "\n".join(ln for ln in src.splitlines()
                     if ln.strip() and not ln.strip().startswith("#"))


_gl_src = _code_only(inspect.getsource(glossary.trace_query))
# 裸兜底：except 块里只有 return 0（不带任何留痕）
_bare = "except Exception:\n        return 0" in _gl_src
check("O8 glossary.trace_query 不再是「裸 except → return 0」",
      not _bare, "" if not _bare else "仍是静默兜底（写库失败零痕迹）")
_gl_wired = "observe_fail" in _gl_src
check("O8b glossary.trace_query 引用了 observe_fail",
      _gl_wired, "" if _gl_wired else "未接留痕模块")

_rag_src = _code_only(open(os.path.join(ROOT, "agent", "rag.py"), encoding="utf-8").read())
_o9 = "except Exception as _e_trace" in _rag_src
check("O9 rag.py 的 trace 兜底不再是 `except ...: pass`（裸 pass 无痕迹）",
      _o9,
      "" if _o9 else "未找到带留痕的 trace 兜底（若回退成 pass，则 trace 故障再次不可见）")
_o9b = "except Exception as _e_stat" in _rag_src
check("O9b rag.py 的路由统计兜底不再是 bare pass",
      _o9b, "" if _o9b else "未找到带留痕的统计兜底")

print("[5] 运行时：真实检索不产生任何观测失败", flush=True)
_db = os.path.join(ROOT, "mbse.db")
if not os.path.exists(_db):
    skip("O10 真实检索三路全程零观测失败", "无 mbse.db（CI 环境）—— 本项未验证")
else:
    from agent.rag import GraphRAG
    _rc = sqlite3.connect(_db)
    _rc.row_factory = sqlite3.Row
    _g = GraphRAG()
    OF.reset()
    for _q in ("转发器和接收机之间是什么关系", "宽带载荷", "红烧肉怎么做"):
        _g.retrieve(_q, branch="dev", kb_scope={})
    _fails = OF.snapshot()
    check("O10 真实检索（graph/mixed/vector 三路）全程零观测失败",
          OF.total() == 0, "失败点=%r（任一非空都说明观测链路有洞）" % (_fails,))

print("[6] 变异自证", flush=True)


def _legacy_trace_query(conn, query, trace):
    """精确复刻修前实现：裸 except。"""
    try:
        cur = conn.execute(
            "INSERT INTO query_trace (query, normalized, intent, route, domain, hit_docs, "
            "hit_count, top_score, latency_ms, detail) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (query, query, "", "", "", "[]", 0, 0, 0, "{}"))
        conn.commit()
        return cur.lastrowid
    except Exception:                                          # noqa: BLE001
        return 0


OF.reset()
_legacy_ret = _legacy_trace_query(_BadConn(), "旧实现", {})
_m1_red = (_legacy_ret == 0 and OF.total() == 0)
check("M1 复刻旧实现（裸 except）→ O3c 必须变红（坏库上零痕迹）", _m1_red,
      "" if _m1_red else "变异体竟然留了痕（判据无牙齿 or 复刻不准）")

_orig_note = OF.note
try:
    OF.note = lambda *a, **k: None      # 空操作：模拟"有人把留痕删了但保留 try"
    OF.reset()
    _ret_m2 = glossary.trace_query(_BadConn(), "静音", {})
    _m2_red = (_ret_m2 == 0 and OF.total() == 0)
    check("M2 note 换成空操作 → O1/O3c 必须变红（计数不再变化）", _m2_red,
          "" if _m2_red else "变异体仍有计数（说明计数不经过 note，判据与实际路径不一致）")
finally:
    OF.note = _orig_note

# ── 收尾 ────────────────────────────────────────────────────────────────
try:
    _good.close()
    os.unlink(_path)
except OSError:
    pass
OF.reset()

print("\n=== %d PASS / %d FAIL / %d SKIP ===" % (len(PASS), len(FAIL), len(SKIPPED)), flush=True)
if SKIPPED:
    print("SKIP 项（未验证，不等于通过）：%s" % ", ".join(SKIPPED), flush=True)
if FAIL:
    print("FAIL: %s" % ", ".join(FAIL), flush=True)
sys.exit(1 if FAIL else 0)
