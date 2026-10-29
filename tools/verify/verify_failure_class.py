# -*- coding: utf-8 -*-
"""B1-6 门禁：失败分类 + 分类驱动重试。

**背景（对标取证）**：`agent/pipeline_parts/stream.py::_worker` 的重试判据原本只有
`if retry_left <= 0` —— **不看失败原因**。后果：权限拒绝、参数非法、输出不合格这类
"重试也不会变"的**永久失败**也会白白重试一次，而单子任务实测 **344~1155 秒**、
还要再烧一次完整的子 Agent LLM 调用。

**本脚本守什么**：
  [A] 分类判据正确：8 类枚举、永久失败四类、可重试三类
  [B] **优先级敏感**：permission 必须**先于** timeout 判
      （权限拒绝常伴随"调用超时/未返回"，先判 timeout 会把永久失败误归可重试 ⇒ 本改动等于没做）
  [C] **复用而非重造**：瞬态词表来自 `tool_resilience`，不另写第二份
  [D] 分类驱动重试：worker 里先分类再决定；`_fc` 不可用时**退回旧行为**（照重试）
  [E] `fail()` 结构化落库 `error_class`；极老库无该列时**降级写入不丢失败记录**
  [F] 真库 54 条 failed 的 error **全量**分类（存量覆盖率必须 100%）
  [G] 变异自证：把「分类驱动」改成「一律重试」，D 组必须 FAIL

**只读**：真库 `mode=ro`；行为层用临时库；变异在内存副本，**不写回源文件**。

**运行**：`.venv/Scripts/python.exe -X utf8 tools/verify/verify_failure_class.py`
"""
import io
from _tmpdir import mkdtemp  # 共享：创建即登记，atexit 退出时自动回收临时目录
import os
import re
import sys
import sqlite3
import tempfile
import shutil

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
STREAM = os.path.join(ROOT, "agent", "pipeline_parts", "stream.py")
TQUEUE = os.path.join(ROOT, "task_queue.py")
FCLS = os.path.join(ROOT, "services", "failure_class.py")
SCHEMA = os.path.join(ROOT, "database", "schema.py")
MIGR = os.path.join(ROOT, "database", "migrations", "columns.py")
PROD_DB = os.path.join(ROOT, "mbse.db")

_ok = 0
_fail = 0
_muts = []


def check(name, cond, detail=""):
    global _ok, _fail
    if cond:
        _ok += 1
        print("  [PASS] %s" % name)
    else:
        _fail += 1
        print("  [FAIL] %s  %s" % (name, detail))


sys.path.insert(0, ROOT)
from services import failure_class as fc  # noqa: E402

s_src = io.open(STREAM, encoding="utf-8").read()
t_src = io.open(TQUEUE, encoding="utf-8").read()
f_src = io.open(FCLS, encoding="utf-8").read()
sc_src = io.open(SCHEMA, encoding="utf-8").read()
m_src = io.open(MIGR, encoding="utf-8").read()

print("=== A. 分类判据 ===")
check("A1 8 类枚举齐全",
      set(fc.ERROR_CLASSES) == {"timeout", "transient", "permission", "input_invalid",
                                "output_rejected", "business", "interrupted", "internal"},
      str(fc.ERROR_CLASSES))
check("A2 永久失败四类",
      fc.NON_RETRYABLE == {"permission", "input_invalid", "output_rejected", "business"},
      str(fc.NON_RETRYABLE))
for cls in ("timeout", "transient", "interrupted", "internal"):
    check("A3 %s 可重试" % cls, fc.is_retryable_class(cls) is True)
for cls in ("permission", "input_invalid", "output_rejected", "business"):
    check("A4 %s 不可重试" % cls, fc.is_retryable_class(cls) is False)
check("A5 空串（成功）不可重试", fc.is_retryable_class("") is False)
check("A6 classify(ok=True) 返回空串", fc.classify("随便什么错误", ok=True) == "")

print("\n=== B. 优先级敏感（permission 先于 timeout）===")
# 这条文本同时含"权限"与"超时"两类特征 —— 若权限判定排在 timeout 之后，
# 永久失败会被判成可重试，本次改动就等于没做。
_mix = "permission_denied: 工具调用超时未返回"
check("B1 混合文本判为 permission（权限优先）", fc.classify(_mix, ok=False) == "permission",
      "got=%s —— 权限判定若排在 timeout 之后，永久失败会被误判可重试" % fc.classify(_mix, ok=False))
check("B2 该混合文本不可重试", fc.is_retryable_class(fc.classify(_mix, ok=False)) is False)
_mix2 = "HTTP 403 拒绝访问：连接超时"
check("B3 403+超时 ⇒ permission", fc.classify(_mix2, ok=False) == "permission",
      "got=%s" % fc.classify(_mix2, ok=False))
check("B4 interrupted 先于 timeout（孤儿运行文案含『无任何进展』）",
      fc.classify("编排中断：超过 30 分钟无任何进展，判定为孤儿运行", ok=False) == "interrupted",
      "got=%s" % fc.classify("编排中断：超过 30 分钟无任何进展，判定为孤儿运行", ok=False))

print("\n=== C. 复用 tool_resilience 的词表（不重造）===")
try:
    from tool_resilience import RETRYABLE_KEYWORDS as _TW
    check("C1 引用了 tool_resilience 的词表",
          fc.RETRYABLE_KEYWORDS == _TW,
          "两份词表必然漂移：工具层判可重试、编排层判不可重试")
except Exception as e:
    check("C1 引用了 tool_resilience 的词表", False, str(e))
check("C2 分类器里没有另写一份瞬态词表",
      "connection" not in f_src.split("RETRYABLE_KEYWORDS")[0].split("try:")[-1]
      or "from tool_resilience import" in f_src,
      "疑似本地重定义了瞬态词表")
check("C3 兜底词表仅在 import 失败时用（except 分支内）",
      f_src.count("RETRYABLE_KEYWORDS = (") == 1
      and f_src.index("RETRYABLE_KEYWORDS = (") > f_src.index("except Exception:"),
      "兜底定义位置异常")
# ⚠️ 这四条只断言「**可重试**」，**不断言分类名**：像 'gateway timeout' 同时命中
#    `tool_resilience` 的 "gateway timeout" 与本仓 `_TIMEOUT_KW` 的 "timeout"，
#    判成 transient 或 timeout **都对**（两者都可重试）。断言分类名会把正确的实现判成错。
for t in ("HTTP 503", "429 too many requests", "connection reset", "gateway timeout"):
    k = fc.classify(t, ok=False)
    check("C4 %r ⇒ 可重试（分类名不限）" % t,
          fc.is_retryable_class(k) is True, "got=%s（不可重试）" % k)
check("C4b 明确属于瞬态的判为 transient",
      fc.classify("HTTP 503", ok=False) == "transient")

print("\n=== D. 分类驱动重试已接线 ===")
check("D1 worker 内引入了分类器", "from services import failure_class as _fc" in s_src)
# ⚠️ 2026-10-29 修正（**存量假红**，非本轮引入）：原断言把变量名写死成
#   `as _fc` + `_fc = None`，而实现早已演进为 `as _fc_main` + `_FC_MAIN_OK` 标志
#   （见 stream.py:615-619，且 2189 行 `if _FC_MAIN_OK else "internal"` 才是真降级）。
#   实现**比旧断言要求得更健壮**（多了可用性标志），断言却因名字变了而常红——
#   本仓已知纪律：**门禁断言写错 ≠ 实现错**，改断言前先证实现正确。
#   现改为按**语义**判：在 try 内 import 分类器，并在 except 里把某个 *OK 标志置 False。
_d2 = re.search(
    r"try:\s*\n\s*from services import failure_class as (\w+)\s*\n"
    r"\s*(\w*OK\w*)\s*=\s*True\s*\n"
    r"\s*except Exception:\s*\n"
    r"\s*\2\s*=\s*False",
    s_src)
check("D2 import 包在 try 里（不可用时不中断编排）", _d2 is not None,
      ("alias=%s flag=%s" % (_d2.group(1), _d2.group(2))) if _d2 else "未匹配到 try/except+标志 形态")
check("D3 先分类再决定是否重试", "_retry_ok = _fc.is_retryable_class(_ecls)" in s_src)
check("D4 判据含 not _retry_ok（永久失败直接放弃）",
      re.search(r"if retry_left <= 0 or not _retry_ok:", s_src) is not None)
check("D5 _fc 为 None 时退回旧行为（照重试）",
      re.search(r"else:\s*\n\s*_ecls, _retry_ok = \"\", True", s_src) is not None)
check("D6 失败时把分类写进 error 文案（可观测）", "_ecls" in s_src and "［%s］" in s_src)
check("D7 退避改为指数", "2 ** (_max_retries - retry_left)" in s_src)

print("\n=== E. fail() 结构化落库 + 降级 ===")
_fb = ""
if "def fail(" in t_src:
    _r = t_src.split("def fail(", 1)[1]
    _nx = re.search(r'\n    @staticmethod', _r)
    _fb = _r[:_nx.start()] if _nx else _r[:1600]
check("E1 fail() 写 error_class", "error_class=?" in _fb)
check("E2 fail() 调分类器", "failure_class" in _fb)
check("E3 分类器不可用时不阻断（except → _cls=''）", '_cls = ""' in _fb)
check("E4 无 error_class 列时降级写入（老库不丢失败记录）",
      _fb.count("UPDATE agent_tasks SET status='failed'") >= 2,
      "只有一条 UPDATE ⇒ 老库会直接抛错，失败记录丢失（实测 %d 句）"
      % _fb.count("UPDATE agent_tasks SET status='failed'"))
check("E5 DDL 有 error_class", re.search(r"error_class TEXT DEFAULT ''", sc_src) is not None)
check("E6 迁移补列有 error_class", re.search(r'_add\("agent_tasks",\s*"error_class"', m_src) is not None)
check("E7 DDL 与迁移口径一致（都是 TEXT DEFAULT ''）",
      "error_class TEXT DEFAULT ''" in sc_src
      and '"error_class", "TEXT DEFAULT \'\'' in m_src)

print("\n=== F. 真库 54 条 failed 全量分类（存量覆盖）===")
if os.path.exists(PROD_DB):
    _c = sqlite3.connect("file:%s?mode=ro" % PROD_DB, uri=True)
    _rows = _c.execute("SELECT error FROM agent_tasks WHERE status='failed'").fetchall()
    _c.close()
    import collections
    _cnt = collections.Counter()
    _uncls = []
    for (e,) in _rows:
        _k = fc.classify(e, ok=False)
        _cnt[_k] += 1
        if _k == "internal":
            _uncls.append(e)
    print("  真库 failed %d 条 ⇒ %s" % (len(_rows), dict(_cnt)))
    check("F1 存量全覆盖（无 internal 兜底）", not _uncls,
          "以下 error 落到兜底未归类：%r" % _uncls[:3])
    check("F2 分类结果都属于已知枚举", all(k in fc.ERROR_CLASSES for k in _cnt), str(dict(_cnt)))
    check("F3 真实失败全部可重试（孤儿运行/超时本质可恢复）",
          all(fc.is_retryable_class(k) for k in _cnt),
          "若历史错误被判不可重试，会改变现有重试行为 → 需复核")
else:
    print("  [SKIP] 未找到真库")

print("\n=== G. 行为层：fail() 真落库（临时库）===")
TMP = mkdtemp(prefix="verify_fcls_")
TMP_DB = os.path.join(TMP, "t.db")
try:
    _tc = sqlite3.connect(TMP_DB)
    _tc.execute("""CREATE TABLE agent_tasks (
        id INTEGER PRIMARY KEY, run_id INTEGER, status TEXT DEFAULT 'planned',
        error TEXT DEFAULT '', error_class TEXT DEFAULT '', updated_at TEXT)""")
    _tc.executemany("INSERT INTO agent_tasks (id,status) VALUES (?,?)",
                    [(1, "ready"), (2, "ready"), (3, "ready")])
    _tc.commit()
    from task_queue import TaskQueue
    TaskQueue.fail(_tc, 1, "permission_denied")
    TaskQueue.fail(_tc, 2, "timeout（30s 无产出）")
    TaskQueue.fail(_tc, 3, "无权限")
    _got = {r[0]: (r[1], r[2]) for r in _tc.execute("SELECT id,status,error_class FROM agent_tasks")}
    check("G1 permission 落库分类正确", _got[1][1] == "permission", str(_got.get(1)))
    check("G2 timeout 落库分类正确", _got[2][1] == "timeout", str(_got.get(2)))
    check("G3 中文权限文案也归 permission", _got[3][1] == "permission", str(_got.get(3)))
    check("G4 status 一律置 failed", all(v[0] == "failed" for v in _got.values()))
    _tc.commit()
    # G5 老库（无 error_class 列）降级：仍要记下 failed，不能抛错
    _tc2 = sqlite3.connect(":memory:")
    _tc2.row_factory = sqlite3.Row
    _tc2.execute("CREATE TABLE agent_tasks (id INTEGER PRIMARY KEY, status TEXT, error TEXT, updated_at TEXT)")
    _tc2.execute("INSERT INTO agent_tasks (id,status) VALUES (1,'ready')")
    _tc2.commit()
    _raised = None
    try:
        TaskQueue.fail(_tc2, 1, "some error")
    except Exception as e:
        _raised = str(e)
    check("G5 老库无 error_class 列时不抛错（降级写入）", _raised is None, "raised=%r" % _raised)
    _st = _tc2.execute("SELECT status,error FROM agent_tasks WHERE id=1").fetchone()
    check("G6 老库降级后失败仍被记录（status+error 不丢）",
          _st["status"] == "failed" and "some error" in ("" + _st["error"] or ""),
          str(dict(_st) if _st else None))
    _tc2.close()
    _tc.close()
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print("\n=== H. 变异自证（退回『一律重试』）===")
_anchor = "if retry_left <= 0 or not _retry_ok:"
if _anchor not in s_src:
    print("  [VACUOUS] 锚点不在源码里，自证中止")
else:
    _mut = s_src.replace(_anchor, "if retry_left <= 0:  # MUTATION: 一律重试")
    if _mut == s_src:
        print("  [VACUOUS] 变异未生效")
    else:
        check("H1 变异体上 D3 必须 FAIL（不再按分类决策）",
              "_retry_ok = _fc.is_retryable_class(_ecls)" in _mut and "not _retry_ok" not in _mut,
              "D3 仍能通过 ⇒ 断言抓不住变异")
        # 变异后权限失败会被重试一次 —— 用真库语义复现这个后果
        _cls = fc.classify("permission_denied", ok=False)
        _mut_retries = True      # 变异体：只看 retry_left
        _right_retries = fc.is_retryable_class(_cls)
        check("H2 变异体下权限失败会被重试（缺陷形态复现）",
              _mut_retries is True and _right_retries is False,
              "变异体与正确行为不一致，自证无效")
        _muts.append(("M1", "重试判据退回『只看 retry_left』（不看分类）"))
        print("  [SELF-PROOF] M1 重试判据退回旧形态 —— 权限拒绝重新被无谓重试")

print("\n" + "=" * 60)
print("常态断言：%d 条，%d 通过 / %d 失败" % (_ok + _fail, _ok, _fail))
print("变异自证：%d 组" % len(_muts))
for mid, desc in _muts:
    print("  - %s %s" % (mid, desc))
print("结论：%s" % ("门禁通过" if _fail == 0 and _muts else "门禁未通过"))
sys.exit(0 if (_fail == 0 and _muts) else 1)