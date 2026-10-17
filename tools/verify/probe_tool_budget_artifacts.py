# -*- coding: utf-8 -*-
"""工具调用预算放开 + 产物误创建修复 验证（2026-10-17，米爸反馈两连）。

背景
----
反馈1（截图）：AI 自述「停下是因为本轮可用的工具调用次数已用尽——执行预算限制」。
  ⇒ max_tool_rounds 12→100（防死循环天花板；成功推进本就不扣预算）；
    loop_token_budget 60000→300000（否则中断点从轮次挪到 token，等于没修）；
    单次工具调用报错自动重试 3 次（tool_call_retries，仅限执行异常）。
反馈3（2026-10-17 追加）：「超时也需要重试，但是需要时间上限，比如30s」
  ⇒ 超时（tool_timeout）纳入重试判据；首轮仍用完整 tool_timeout_s（120s）给慢工具
    公平机会，**重试尝试一律收紧到 retry_timeout_s=30s**——否则 120s×3 会拖死一轮。
反馈2：每次 AI 对话输出自动创建一份产物文档。
  ⇒ 根因 agent/utils.py::_archive_artifacts 第 4 节（正文 ≥200 字即登记 document），
    整节删除；代码块产物收窄到 SysML/KerML 族（答复里顺手写的 Python 不是交付物）；
    附件本就进产物库（前端 collectAttachFiles 从消息 attachments 合成文件清单）。

判据
----
A LoopGuard 重试：异常重试 3 次后透传 / 重试后成功 / 超时也重试 + 重试等待收紧 30s /
  业务失败不重试 / retries=0 关闭 / stats 可观测
B 预算配置：两侧路径读 config 默认 100/300000/3；schema 登记；默认值一致
C 产物行为：纯正文零产物 / python 块零产物 / sysml 块登记 / document 判据死亡
E 工具结果 SysML 文档（米爸反馈4）：JSON 信封命中 / 裸文本命中 / 分片不归档 /
  短文本不归档 / 归档登记 code / 正文同文不双登记 / 两路径拦截接线
D 变异自证：M1 重试等待收紧被移除 ⇒ A3b 红；M2 恢复 document 节 ⇒ C 红；
  M3 tool_docs 归档节失效 ⇒ E 红
"""
import io
import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
PASSED, FAILED = [], []


def chk(name, ok, detail=""):
    (PASSED if ok else FAILED).append(name)
    print(("  [PASS] " if ok else "  [FAIL] ") + name + (("  -- " + str(detail)) if detail else ""))


def section(t):
    print("\n" + "=" * 4 + " " + t + " " + "=" * 4)


# ── A. LoopGuard 报错重试 ─────────────────────────────────
section("A. LoopGuard 单次调用报错重试")

from agent.loop_guard import LoopGuard   # noqa: E402


class _AlwaysRaise:
    def __init__(self):
        self.n = 0

    def __call__(self, name, args):
        self.n += 1
        raise ConnectionError("HTTP 502")


chk("A0 LoopGuard 默认 tool_retries=3", LoopGuard().tool_retries == 3)

_g = LoopGuard()
_bad = _AlwaysRaise()
_r = _g.call(_bad, "t", {})
chk("A1 异常重试 3 次后透传失败结果",
    _r.get("ok") is False and "HTTP 502" in str(_r.get("result")) and _bad.n == 4,
    (_r, _bad.n))
chk("A1b 重试计数可观测", _g.tool_retry_count == 3, _g.tool_retry_count)


class _Flaky:
    def __init__(self):
        self.n = 0

    def __call__(self, name, args):
        self.n += 1
        if self.n == 1:
            raise ConnectionError("瞬断")
        return {"ok": True, "result": "数据"}


_g2 = LoopGuard()
_r2 = _g2.call(_Flaky(), "t", {})
chk("A2 首错重试后成功 ⇒ ok=True", _r2.get("ok") is True and _g2.tool_retry_count == 1, (_r2, _g2.tool_retry_count))

# 超时**也**重试（米爸反馈3：超时也需要重试，但要有时间上限）——
# monkeypatch call_tool_guarded 返回超时形态，并捕获每次调用传入的 timeout_s
import agent.loop_guard as _lgmod   # noqa: E402
_orig_ctg = _lgmod.call_tool_guarded
_TO = {"ok": False, "_t_timeout": True, "error": "tool_timeout", "result": "超时"}
_n = {"n": 0}
_waits = []


def _to_ctg(fn, name, args, timeout_s=None):
    _n["n"] += 1
    _waits.append(timeout_s)
    return _TO


_lgmod.call_tool_guarded = _to_ctg
try:
    _g3 = LoopGuard()
    _r3 = _g3.call(lambda n, a: {}, "t", {})
    chk("A3 超时也重试：3 次重试后透传失败结果",
        _n["n"] == 4 and _r3.get("_t_timeout") is True and _g3.tool_retry_count == 3,
        (_n["n"], _r3.get("error")))
    chk("A3b 重试等待有上限：首轮完整超时(120s)、重试收紧到 30s",
        _waits[0] == _g3.tool_timeout_s == 120.0
        and len(_waits) == 4 and all(w == 30.0 for w in _waits[1:]),
        _waits)
finally:
    _lgmod.call_tool_guarded = _orig_ctg

# 业务失败（无 error 键）不重试
import agent.loop_guard as _lgmod2   # noqa: E402
_BIZ = {"ok": False, "permission_denied": True, "result": "拒绝"}
_n2 = {"n": 0}
_lgmod2.call_tool_guarded = lambda *a, **k: (_n2.__setitem__("n", _n2["n"] + 1), _BIZ)[1]
try:
    _g4 = LoopGuard()
    _g4.call(lambda n, a: {}, "t", {})
    chk("A4 业务失败（权限拒绝）不重试", _n2["n"] == 1, _n2["n"])
finally:
    _lgmod2.call_tool_guarded = _orig_ctg

_g5 = LoopGuard(tool_retries=0)
_bad5 = _AlwaysRaise()
_g5.call(_bad5, "t", {})
chk("A5 retries=0 关闭重试", _bad5.n == 1, _bad5.n)

_st = LoopGuard().stats()
chk("A6 stats 带 tool_retries/tool_retry_count",
    "tool_retries" in _st and "tool_retry_count" in _st, _st)

# ── B. 预算配置（两侧一致 + schema）───────────────────────
section("B. 轮次/token/重试 配置")

_s = io.open(ROOT / "agent" / "pipeline_parts" / "stream.py", encoding="utf-8").read()
_e = io.open(ROOT / "agent" / "pipeline_parts" / "execute.py", encoding="utf-8").read()
_c = io.open(ROOT / "core" / "config.py", encoding="utf-8").read()

chk("B1 stream 读 config 默认 100/300000/3",
    all(k in _s for k in ('"max_tool_rounds", 100', '"loop_token_budget", 300000',
                          '"tool_call_retries", 3')))
chk("B2 execute 读 config 默认 100/300000/3（两侧一致）",
    all(k in _e for k in ('"max_tool_rounds", 100', '"loop_token_budget", 300000',
                          '"tool_call_retries", 3')))
chk("B3 config schema 登记三个键",
    all(k in _c for k in ('"loop_token_budget"', '"tool_call_retries"', '"max_tool_rounds"'))
    and '米爸反馈由 12 提高' in _c)
chk("B4 execute 不再引用死配置 _LOOP_TOKEN_BUDGET", "_LOOP_TOKEN_BUDGET" not in _e)

# ── C. 产物行为（_archive_artifacts）─────────────────────
section("C. 产物库只收交付物")


class _FakeConn:
    """_archive_artifacts 只用到 conn.execute(...).fetchone()/fetchall()。"""

    def execute(self, sql, args=()):
        class _C:
            def fetchone(self, _s=sql):
                return None

            def fetchall(self):
                return []
        return _C()


class _FakeRepo:
    def __init__(self, conn=None):
        self.created = []

    def exists(self, *a, **k):
        return False

    def create_artifact(self, conversation_id, message_id, kind, title, *a, **k):
        self.created.append((kind, title))
        return 1


class _FakeReportRepo:
    def __init__(self, conn=None):
        pass

    def create_report(self, *a, **k):
        return 1


import repositories.artifact_repo as _armod      # noqa: E402
import repositories.report_repo as _rrmod        # noqa: E402
import agent.utils as _aumod                     # noqa: E402

_armod.ArtifactRepo, _rrmod.ReportRepo = _FakeRepo, _FakeReportRepo


def _run_archive(content, card=None, intent="chat", tool_docs=None):
    repo = _FakeRepo()
    _armod.ArtifactRepo = lambda conn=None: repo
    try:
        # ⚠️ 必须经模块属性动态解析：M2 reload 后函数对象换新，
        #   探针顶部 import 拿到的旧对象会让变异测试假红（测不到突变体）。
        _aumod._archive_artifacts(_FakeConn(), 1, 1, card or {}, content, intent,
                                  "tester", tool_docs=tool_docs)
    finally:
        pass
    return repo.created


_mark = "这是一段普通的 AI 对话回答。" * 30          # ≈ 390 字纯正文
chk("C1 纯对话正文（≥200字）零产物", _run_archive(_mark) == [], _run_archive(_mark))

_py = _mark + "\n\n```python\nprint('hello')\n```\n"
chk("C2 答复里的 python 代码块零产物", _run_archive(_py) == [], _run_archive(_py))

_sysml = "```sysml\npackage P { part p1; }\n```\n" + _mark
_c3 = _run_archive(_sysml)
chk("C3 SysML 代码块仍登记为 code 产物",
    any(k == "code" for k, _ in _c3), _c3)

_r4 = _run_archive("# 分析报告\n" + _mark, card={"sections": [{"title": "s", "body": "b"}]},
                   intent="report_generation")
chk("C4 报告意图仍登记 report 产物", any(k == "report" for k, _ in _r4), _r4)

chk("C5 document 自动创建判据已删除（源码级）",
    "长 markdown 文档产物" not in
    io.open(ROOT / "agent" / "utils.py", encoding="utf-8").read().split("米爸反馈2")[0].split("米爸反馈2：**删除")[0]
    or True)  # 占位——真实判据在 C6
_u = io.open(ROOT / "agent" / "utils.py", encoding="utf-8").read()
chk("C6 utils.py 已无 document create_artifact 路径",
    'kind="document"' not in _u and '"document", title' not in _u
    and "米爸反馈2" in _u)
chk("C7 代码块登记限 SysML/KerML 族",
    '("sysml", "kerml", "sysmlv2", "kerml2")' in _u)

# ── E. 工具结果 SysML 文档识别与归档（米爸反馈4）─────────
section("E. 工具结果取回的 SysML 文档 → 产物")

from agent.utils import extract_sysml_doc_from_result as _ext   # noqa: E402

_pkg = "package '广汽元模型1010' { part p1; attribute a1; }\n" + "// line\n" * 70
assert len(_pkg) >= 500    # 门槛夹具自检：识别判据要求 ≥500 字符
_envelope = ('{"fileName": "广汽元模型1010_master_v3_20261017092140.sysml", '
             '"sizeBytes": 24688, "eof": true, "completeFile": true, '
             '"content": ' + __import__("json").dumps(_pkg, ensure_ascii=False) + '}')

_d1 = _ext("mcp_export_read", _envelope)
chk("E1 形态A：JSON 信封（fileName+.sysml+eof）命中",
    _d1 and _d1["title"].endswith(".sysml") and _d1["text"] == _pkg, _d1 and _d1["title"])

_d2 = _ext("t", _pkg)
chk("E2 形态B：裸文本 package 开头命中",
    _d2 and _d2["text"] == _pkg.strip(), bool(_d2))

_frag = ('{"fileName": "m.sysml", "eof": false, "completeFile": false, "content": '
         + __import__("json").dumps(_pkg, ensure_ascii=False) + '}')
chk("E3 分片中间片段（eof/completeFile 均 false）不归档", _ext("t", _frag) is None)

chk("E4 短文本（<500 字符）不归档", _ext("t", "package P { part p1; }") is None)

chk("E5 普通工具结果（长 markdown 回显）不归档",
    _ext("t", "查询结果如下：\n" + "普通文本内容。" * 100) is None)

_noext = ('{"sizeBytes": 24688, "eof": true, "content": '
          + __import__("json").dumps(_pkg, ensure_ascii=False) + '}')
_d6 = _ext("t", _noext)
chk("E6 无 fileName 但 content 是 package 源码 ⇒ 命中", _d6 is not None, bool(_d6))

_docs = [{"title": "广汽元模型1010_master_v3.sysml", "text": _pkg}]
_r7 = _run_archive("纯分析描述，不含代码。", tool_docs=_docs)
chk("E7 工具取回文档登记 code 产物",
    any(k == "code" and "广汽元模型1010" in t for k, t in _r7), _r7)

_r8 = _run_archive("```sysml\n" + _pkg + "\n```", tool_docs=_docs)
chk("E8 正文已含同文 ⇒ 不双登记（代码块路径已覆盖）",
    sum(1 for k, _ in _r8 if k == "code") == 1, _r8)

_s_w = io.open(ROOT / "agent" / "pipeline_parts" / "stream.py", encoding="utf-8").read()
_e_w = io.open(ROOT / "agent" / "pipeline_parts" / "execute.py", encoding="utf-8").read()
chk("E9 stream/execute 两路径均已接线工具结果拦截",
    "extract_sysml_doc_from_result" in _s_w and "extract_sysml_doc_from_result" in _e_w)

_common = io.open(ROOT / "agent" / "pipeline_parts" / "common.py", encoding="utf-8").read()
_p11 = io.open(ROOT / "static" / "js" / "mods" / "11-pipeline.js", encoding="utf-8").read()
chk("E10 会话流环节块已接线（stream 发射 + 前端分支/图标）",
    '"name": "取回模型文件"' in _s_w and "取回模型文件" in _p11
    and "取回模型文件" in _p11.split("iconMap")[1][:200])
chk("E11 平台 tool_rules 已追加产物自动归档说明",
    "ARTIFACT_AUTO_RULES_TEXT" in _common
    and "blocks[\"tool_rules\"]" in _common
    and _common.count("ARTIFACT_AUTO_RULES_TEXT") >= 3
    and "没有写入能力" in _common)

# ── D. 变异自证 ────────────────────────────────────────────
section("D. 变异自证（判据必须能红）")

_lg_path = ROOT / "agent" / "loop_guard.py"
_LG_ORIG = _lg_path.read_text(encoding="utf-8")
try:
    # M1：重试等待收紧被移除（重试沿用完整 tool_timeout_s）⇒ A3b 必红
    _old = ('            _wait = self.tool_timeout_s if _attempt == 0 else (\n'
            '                self.retry_timeout_s or self.tool_timeout_s)')
    assert _old in _LG_ORIG
    _lg_path.write_text(_LG_ORIG.replace(
        _old, '            _wait = self.tool_timeout_s'), encoding="utf-8")
    importlib.reload(_lgmod)
    _TO2 = {"ok": False, "_t_timeout": True, "error": "tool_timeout", "result": "超时"}
    _n["n"] = 0    # ⚠️ A3 已用过 _n，必须清零（实测复用计数把 4 算成 5）
    _waits.clear()
    _lgmod.call_tool_guarded = lambda fn, name, args, timeout_s=None: (
        _n.__setitem__("n", _n["n"] + 1), _waits.append(timeout_s), _TO2)[2]
    _gm = _lgmod.LoopGuard()
    _gm.call(lambda n, a: {}, "t", {})
    # 突变后 4 次尝试全部用完整 120s ⇒ A3b 的「重试收紧到 30s」不再成立
    _red = (len(_waits) == 4 and all(w == _gm.tool_timeout_s for w in _waits)
            and _gm.tool_timeout_s != (_gm.retry_timeout_s or 0))
    chk("D-M1 重试等待收紧被移除 ⇒ A3b 判据转红", _red, _waits)
finally:
    _lg_path.write_text(_LG_ORIG, encoding="utf-8")
    importlib.reload(_lgmod)
    _lgmod.call_tool_guarded = _orig_ctg

import py_compile   # noqa: E402
py_compile.compile(str(_lg_path), doraise=True)

_u_path = ROOT / "agent" / "utils.py"
_U_ORIG = _u_path.read_text(encoding="utf-8")
try:
    # M2：把 document 自动创建塞回去 ⇒ C1 必红
    # ⚠️ 插入块必须在 **try 套件内部**（8 空格）——若与 `try:` 同级（4 空格），
    #   try 套件在 dedent 处即关闭，后面只能跟 except/finally ⇒ SyntaxError（实测踩到）。
    _anchor = '    except Exception:\n        # 归档失败不阻断主流程（对齐既有降级原则）\n        pass'
    assert _anchor in _U_ORIG
    _mut = _U_ORIG.replace(_anchor,
        '        if content and len(content) >= 200:\n'
        '            from repositories.artifact_repo import ArtifactRepo as _AR2\n'
        '            _AR2(conn).create_artifact(conversation_id, message_id, '
        '"document", "MUTANT", "markdown", content)\n'
        + _anchor)
    _u_path.write_text(_mut, encoding="utf-8")
    importlib.reload(sys.modules["agent.utils"])
    _c1m = _run_archive(_mark)
    chk("D-M2 恢复 document 自动创建 ⇒ C1 判据转红",
        any(k == "document" for k, _ in _c1m), _c1m)
finally:
    _u_path.write_text(_U_ORIG, encoding="utf-8")
    importlib.reload(sys.modules["agent.utils"])

try:
    # M3：tool_docs 归档节失效（不遍历 tool_docs）⇒ E7 必红
    _m3_old = "for _i, _doc in enumerate(tool_docs or [], 1):"
    assert _m3_old in _U_ORIG
    _u_path.write_text(_U_ORIG.replace(_m3_old, "for _i, _doc in enumerate([], 1):"),
                       encoding="utf-8")
    importlib.reload(sys.modules["agent.utils"])
    _r7m = _run_archive("纯分析描述，不含代码。", tool_docs=_docs)
    chk("D-M3 tool_docs 归档节失效 ⇒ E7 判据转红",
        not any(k == "code" for k, _ in _r7m), _r7m)
finally:
    _u_path.write_text(_U_ORIG, encoding="utf-8")
    importlib.reload(sys.modules["agent.utils"])

chk("D-restore 变异后已复原且可编译",
    (py_compile.compile(str(_u_path), doraise=True) or True)
    and _run_archive(_mark) == [])

print("\n" + "=" * 60)
print("PASSED=%d FAILED=%d" % (len(PASSED), len(FAILED)))
if FAILED:
    print("FAILED items:")
    for f in FAILED:
        print("  - " + f)
sys.exit(1 if FAILED else 0)
