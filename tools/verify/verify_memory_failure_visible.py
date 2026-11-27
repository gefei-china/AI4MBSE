# -*- coding: utf-8 -*-
# CI-OPTIONAL: 本门禁通过伪造 sqlite3.OperationalError 注入真故障，依赖特定 Python/sqlite3 版本的异常文案（"database is locked"/"no such column"）；跨 CI 环境文案漂移会假红。开发机 7/7 + 2 变异通过。
"""记忆检索的「失败不得伪装成没有」门禁（2026-11-27）。

## 它防的是什么
`MemoryService.search()` 与 `search_core()` 最外层原先是 **`except Exception: return []`**。
这是全仓最危险的一处兜底，因为它的对外表现是「**没有相关记忆**」——而这是个**看起来像真的值**，
会被上层当事实继续推理：
  · `recall_memories` 拿不到同域命中 ⇒ 触发跨域兜底（多一次全库扫描，看起来"跨域生效"）；
  · `_build_memory_hint` 看到 0 条 ⇒ 整块【长期记忆】不注入；
  · 用户与排查者看到的是"这平台没记住过东西"，而不是"检索坏了"。

实测踩到两次（都在本次记忆分型里）：① 只读连接/夹具缺列 ② 验证冷层时用短 query
导致的行为差异 —— 两次都差点被误判成"记忆功能坏了 / 冷层不可检索"。
同族：`consolidate()` 的"缺列伪装成没东西可合并"、`forget()` 的"异常伪装成没有可降的"。

## 判据（每条都要能在变异后翻红）
  S1 真故障（`database is locked`）⇒ search 必须登记降级（`memory_service.search`）
  S2 真故障 ⇒ search_core 路径上必须留下降级痕迹（`_cols_of` 或 `search_core`，任一即算）
  S3 夹具缺列（`no such column`）⇒ **不得**新增 `_cols_of` 降级（合法场景，否则日志刷成噪音）
  S4 结构判据：`search` / `search_core` 的代码体里，最外层 except 必须含 `degrade` 或 `logger`
     （只判行为会被"某条路径没走到"漏掉；两条一起才既防漏又防假）
  M1 把 search 的 except 改回裸 `return []` ⇒ S1 必须翻红
  M2 把 `_cols_of` 的"缺列豁免"去掉（缺列也登记）⇒ S3 必须翻红

用法： ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_memory_failure_visible.py
"""
import ast
import io
import os
import sqlite3
import sys
import textwrap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.chdir(ROOT)

SRC_PATH = os.path.join(ROOT, "services", "memory_service.py")

PASS, FAIL = [], []


def chk(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name,
                           ("  — " + str(detail)) if detail and not ok else ""))


class _Locked:
    """真故障：库锁（既不是缺列也不是缺表 ⇒ 应当登记）。"""

    def execute(self, *a, **k):
        raise sqlite3.OperationalError("database is locked")

    def commit(self):
        pass


class _NoCol:
    """夹具/老库：缺列（合法场景 ⇒ `_cols_of` 不应登记）。"""

    def execute(self, *a, **k):
        raise sqlite3.OperationalError("no such column: tier")

    def commit(self):
        pass


def _fresh_degrade():
    """清空降级全局统计，让本组断言不受之前调用的污染。"""
    from core import degrade as D
    with D._G_LOCK:                      # noqa: SLF001  门禁需要重置全局态
        D._G_STATS.clear()
    try:
        D.reset_current()
    except Exception:
        pass
    return D


def _where_counts(D):
    st = D.snapshot()
    return {k: v["count"] for k, v in (st.get("where") or {}).items()}


def probe(ms=None):
    """返回各判据布尔值。ms=None 用产品模块。"""
    if ms is None:
        from services.memory_service import MemoryService as ms
    D = _fresh_degrade()
    out = {}

    ms.search(_Locked(), "a", "query", top_k=5)
    w = _where_counts(D)
    # S1 只认 search **自身**的登记：真故障下 _cols_of 也会登记，    # 若把两者相加，M1（删 search 登记）会被 _cols_of 兜住而翻不红 —— 判据就没牙了。
    out["s1"] = w.get("memory_service.search", 0) >= 1
    out["w_locked_search"] = w

    D = _fresh_degrade()
    ms.search_core(_Locked(), "a", top_k=2)
    w2 = _where_counts(D)
    out["s2"] = (w2.get("memory_service.search_core", 0)
                 + w2.get("memory_service._cols_of", 0)) >= 1
    out["w_locked_core"] = w2

    D = _fresh_degrade()
    ms.search(_NoCol(), "a", "query", top_k=5)
    w3 = _where_counts(D)
    out["s3"] = w3.get("memory_service._cols_of", 0) == 0
    out["w_nocol"] = w3
    return out


def _innermost_except_visible(fn_name: str) -> tuple:
    """结构判据：函数体里**最外层** `try` 的 except 处理器必须含 degrade / logger。

    为什么要结构判据：只判行为的话，若某条异常路径恰好没走到，判据会假绿；
    而此时"下次真出事仍然静默"。两条一起才既防漏又防假。
    """
    src = io.open(SRC_PATH, encoding="utf-8").read()
    tree = ast.parse(src)
    target = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == fn_name:
            target = node
            break
    if target is None:
        return False, "函数 %s 未找到" % fn_name
    # 取函数体内层级的第一个 Try（最外层）
    tries = [n for n in target.body if isinstance(n, ast.Try)]
    if not tries:
        return False, "函数体内无顶层 try"
    t = tries[0]
    if not t.handlers:
        return False, "顶层 try 无 except"
    names = set()
    for h in t.handlers:
        for n in ast.walk(h):
            if isinstance(n, ast.Attribute) and n.attr in ("note", "warning", "logger"):
                names.add(n.attr)
            if isinstance(n, ast.Name) and n.id == "logger":
                names.add("logger")
            if isinstance(n, ast.Name) and n.id == "_deg":
                names.add("degrade")
    ok = bool(names & {"note", "warning", "logger", "degrade"})
    return ok, sorted(names)


def main() -> int:
    print("=" * 78)
    print("门禁：记忆检索失败必须可见（不得伪装成「没有记忆」）")
    print("=" * 78)

    r = probe()
    print("\n[S] 行为判据")
    chk("S1 真故障（database is locked）⇒ search 登记降级", r["s1"], r["w_locked_search"])
    chk("S2 真故障 ⇒ search_core 路径留下降级痕迹", r["s2"], r["w_locked_core"])
    chk("S3 夹具缺列（no such column）⇒ _cols_of **不**登记（合法场景不刷日志）",
        r["s3"], r["w_nocol"])

    print("\n[S] 结构判据（防「某条路径没走到」导致假绿）")
    for fn in ("search", "search_core"):
        ok, names = _innermost_except_visible(fn)
        chk("S4 `%s` 最外层 except 含可见动作" % fn, ok, names)

    print("\n[M] 变异自证")
    src = io.open(SRC_PATH, encoding="utf-8").read()

    # M1：把 search 的降级登记去掉（"失败重新伪装成没有"）。
    #   ⚠️ 锚点用**单行** `_deg.note("memory_service.search"...)`：
    #   原锚点含 `except Exception: pass` 整段，实现把兜底改成 `logger.debug(...)` 后失效 ——
    #   「变异打空」会被误读成「判据抓不到」（本仓记过的同族坑）。
    #   只去掉登记（保留 warning + return []）足以让 S1 翻红，且变异体语法必然合法。
    # ⚠️ 不能把 `_deg.note(...)` 整行换成 `pass`：调用是**多行**的，
    #   删首行会让 `impact=...)` 续行悬空 ⇒ 变异体 SyntaxError ⇒ 被 catch 成"判据没翻红"
    #   —— 这是"变异打空"伪装成"判据无牙"的同族坑（本仓已记）。
    #   改为把 `_deg.note` 换成 no-op 函数 `_m1_noop`，参数原样保留，语法必然合法。
    m1_src = src.replace(
        '                _deg.note("memory_service.search", exc=exc, level=_deg.CRITICAL,',
        '                _m1_noop("memory_service.search", exc=exc, level=_deg.CRITICAL,', 1)
    m1_src = m1_src.replace(
        '                    _deg.note("memory_service._cols_of", exc=exc, level=_deg.DEGRADED,',
        '                    _m1_noop("memory_service._cols_of", exc=exc, level=_deg.DEGRADED,', 1)
    if m1_src == src:
        chk("M1 变异锚点命中", False, "锚点失效（实现已改写，判据需同步）")
    else:
        try:
            ns = dict(vars(__import__("services.memory_service", fromlist=["x"])))
            ns["__name__"] = "ms_m1"
            ns["_m1_noop"] = lambda *a, **k: None
            exec(compile(m1_src, SRC_PATH, "exec"), ns)
            rm1 = probe(ns["MemoryService"])
            chk("M1 改回裸 return [] ⇒ S1 翻红（失败重新伪装成没有）",
                rm1["s1"] is False, rm1["w_locked_search"])
        except Exception as e:  # noqa: BLE001
            chk("M1 改回裸 return [] ⇒ S1 翻红（失败重新伪装成没有）", False,
                "%s: %s" % (type(e).__name__, e))

    # M2：把 _cols_of 的"缺列豁免"去掉
    m2_src = src.replace(
        '''            _expected = (isinstance(exc, sqlite3.OperationalError)
                         and ("no such column" in _msg.lower()
                              or "no such table" in _msg.lower()))''',
        '''            _expected = False''', 1)
    if m2_src == src:
        chk("M2 变异锚点命中", False, "锚点失效")
    else:
        try:
            ns = dict(vars(__import__("services.memory_service", fromlist=["x"])))
            ns["__name__"] = "ms_m2"
            exec(compile(m2_src, SRC_PATH, "exec"), ns)
            rm2 = probe(ns["MemoryService"])
            chk("M2 去掉缺列豁免 ⇒ S3 翻红（夹具场景被当故障，日志会被刷成噪音）",
                rm2["s3"] is False, rm2["w_nocol"])
        except Exception as e:  # noqa: BLE001
            chk("M2 去掉缺列豁免 ⇒ S3 翻红（夹具场景被当故障，日志会被刷成噪音）", False,
                "%s: %s" % (type(e).__name__, e))

    print()
    print("=" * 78)
    print("PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("  FAIL:", f)
    print("=" * 78)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
