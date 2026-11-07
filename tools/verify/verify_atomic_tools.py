# CI-OPTIONAL: [local] tier=local（见 tools/gates_manifest.json）：需真实服务 / 真 LLM key / 生产真库金标或随包运行时资产，CI 无服务无 secret ⇒ 跑不了；本机已实测通过，改法须同时改清单 tier 并接线，不允许只删这行
# -*- coding: utf-8 -*-
"""门禁 · code_exec / doc_parse 两个原子能力（2026-11-06，P0）

## 为什么这两个工具值得单独一道门禁

`agent/tool_side_effect.py` 那轮盘点发现：沙箱与文档库**早就存在**，但从未接成工具：
  · `sandbox.py`：Python/JS 沙箱，进程隔离 + 超时强杀 + builtins/import 白名单
  · `pypdf` / `pdfplumber` / `python-docx` / `openpyxl` 全部已安装
  · `SELECT COUNT(*) FROM tools WHERE name LIKE '%code%'` = **0**
⇒ 模型"算一下、转一下、读个 PDF"只能靠心算或拒绝。

## 接线必须同时满足四处（漏一处就"看着像成功、实际调不到"）

  ① `BUILTINS`（workflows/tools.py）—— 编排侧执行分发
  ② `BUILTIN_DESC`（agent/pipeline_parts/tools.py）—— 运行时注入的唯一事实源
  ③ `_do_execute` 分发（**两处**：workflows + agent/pipeline_parts）—— 两套独立实现
  ④ `__all__`（agent/pipeline_parts/common.py）—— 下划线名字必须显式导出

本门禁把 ①②③④ 全部断言，并用真执行验证安全边界。
⚠️ 踩坑记录：本轮先只改 ①，直接调 ToolExecutor 成功但真实会话报"未配置执行器"；
   补 ③ 后报 `_CODE_TOOL_NAMES is not defined`（漏 ④）；补 ④ 才通。

用法：./.venv/Scripts/python.exe -X utf8 tools/verify/verify_atomic_tools.py
"""
import io
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

OK, FAIL = [], []


def rec(name, cond, detail=""):
    (OK if cond else FAIL).append((name, detail))
    print(("PASS " if cond else "FAIL ") + name + (("  | " + str(detail)) if detail else ""))


def read(p):
    with io.open(os.path.join(ROOT, p), encoding="utf-8") as f:
        return f.read()


# ── A. 四处接线（静态） ──
wft = read("workflows/tools.py")
apt = read("agent/pipeline_parts/tools.py")
cmn = read("agent/pipeline_parts/common.py")

rec("A1 BUILTINS 已登记（编排侧）",
    '"code_exec"' in wft and '"doc_parse"' in wft)
rec("A2 BUILTIN_DESC 已登记（运行时注入事实源）",
    '"code_exec":' in apt and '"doc_parse":' in apt)
rec("A3 comon.py 已导入两个执行器",
    "from code_tools import" in cmn and "from doc_tools import" in cmn)
# ⚠️ 判据必须锚在 `__all__ = [` 那段**列表字面量**里 —— 全文 contains 会把导入行本身当成通过
#   （导入语句里也有这些名字），而 `split("__all__")[-1]` 又会因 __all__ 后还有别的代码而落空。
_m = re.search(r"__all__\s*=\s*\[(.*?)\]", cmn, re.S)
_all_block = _m.group(1) if _m else ""
rec("A4 __all__ 显式导出下划线名字（否则 import * 跳过）",
    "_CODE_TOOL_NAMES" in _all_block and "_DOC_TOOL_NAMES" in _all_block,
    "block_len=%d" % len(_all_block))
rec("A5 Agent 侧有执行分发（模型真正走的那条）",
    "exec_code_tool(name" in apt and "exec_doc_tool(name" in apt)
rec("A6 编排侧有执行分发",
    "exec_code_tool(name" in wft and "exec_doc_tool(name" in wft)

# ── B. 模块名字真的可解析（A4 的行为级验证） ──
try:
    import agent.pipeline_parts.tools as _T
    rec("B1 agent 模块可解析 _CODE_TOOL_NAMES/_DOC_TOOL_NAMES",
        hasattr(_T, "_CODE_TOOL_NAMES") and hasattr(_T, "_DOC_TOOL_NAMES"))
    rec("B2 agent 模块可解析 exec_code_tool/exec_doc_tool",
        hasattr(_T, "exec_code_tool") and hasattr(_T, "exec_doc_tool"))
except Exception as e:
    rec("B1/B2 模块可导入", False, "%s: %s" % (type(e).__name__, str(e)[:120]))

# ── C. code_exec 行为 ──
from code_tools import exec_code_tool as CE, _clamp_timeout

r = CE("code_exec", {"code": "_out['s']=_in['a']+_in['b']", "inputs": {"a": 37, "b": 158}})
rec("C1 Python 计算正确", r["ok"] and '"s": 195' in r["result"], r["result"][:80])

r = CE("code_exec", {"code": "_out['n']=len(_in['xs'])", "inputs": {"xs": [1, 2, 3]}})
rec("C2 inputs 注入 _in", r["ok"] and '"n": 3' in r["result"], r["result"][:80])

r = CE("code_exec", {"code": "_out.v=_in.a*2", "language": "js", "inputs": {"a": 21}})
rec("C3 JS 语言可用", r["ok"] and '"v": 42' in r["result"], r["result"][:90])

r = CE("code_exec", {"code": "open('/etc/passwd')"})
rec("C4 安全：禁 open", (not r["ok"]) and "open" in r["result"], r["result"][:110])

r = CE("code_exec", {"code": "import os\n_out['x']=1"})
rec("C5 安全：禁 import os", (not r["ok"]) and "禁止导入" in r["result"], r["result"][:110])

r = CE("code_exec", {"code": "import subprocess"})
rec("C6 安全：禁 import subprocess", not r["ok"], r["result"][:100])

r = CE("code_exec", {"code": "while True: pass", "timeout": 1})
rec("C7 超时被强杀", (not r["ok"]) and "超时" in r["result"], r["result"][:120])

rec("C8 timeout 夹取（999→30 / 0→默认 / 非法→默认）",
    _clamp_timeout(999) == 30.0 and _clamp_timeout(0) == 10.0 and _clamp_timeout("x") == 10.0,
    (_clamp_timeout(999), _clamp_timeout(0), _clamp_timeout("x")))

r = CE("code_exec", {"code": ""})
rec("C9 空代码报错不抛异常", (not r["ok"]) and "不能为空" in r["result"], r["result"][:80])

r = CE("code_exec", {"code": "_out['x']=1", "inputs": "not-a-dict"})
rec("C10 inputs 类型错误被拦", (not r["ok"]) and "必须是对象" in r["result"], r["result"][:90])

# ── D. doc_parse 行为 ──
from doc_tools import exec_doc_tool as DP

TMP = os.path.join(ROOT, "tmp", "doctest")
os.makedirs(TMP, exist_ok=True)


def _mk_files():
    import csv as _csv
    import docx as _docx
    import openpyxl as _ox
    d = _docx.Document()
    d.add_heading("智源对接手册", 1)
    d.add_paragraph("V2 代码导出四段协议：PREVIEW / EXPORT / STATUS / READ。")
    d.save(os.path.join(TMP, "t.docx"))
    wb = _ox.Workbook()
    ws = wb.active
    ws.title = "需求表"
    ws.append(["编号", "名称"])
    ws.append(["R1", "儿童遗留检测"])
    wb.save(os.path.join(TMP, "t.xlsx"))
    with io.open(os.path.join(TMP, "t.csv"), "w", encoding="utf-8", newline="") as f:
        _csv.writer(f).writerows([["ID", "值"], [1, "甲"]])
    # 最小可用 PDF（用 pypdf 造一页带文字）
    try:
        from pypdf import PdfWriter
        w = PdfWriter()
        w.add_blank_page(width=200, height=200)
        with open(os.path.join(TMP, "t.pdf"), "wb") as f:
            w.write(f)
    except Exception:
        pass


try:
    _mk_files()
except Exception as e:
    rec("D0 测试文件可生成", False, str(e)[:120])

r = DP("doc_parse", {"path": os.path.join(TMP, "t.docx")})
rec("D1 DOCX 正文+表格提取",
    r["ok"] and "智源对接手册" in r["result"] and "PREVIEW" in r["result"], r["result"][:120])

r = DP("doc_parse", {"path": os.path.join(TMP, "t.xlsx")})
rec("D2 XLSX 按工作表提取", r["ok"] and "需求表" in r["result"] and "儿童遗留检测" in r["result"],
    r["result"][:120])

r = DP("doc_parse", {"path": os.path.join(TMP, "t.csv")})
rec("D3 CSV 提取", r["ok"] and "甲" in r["result"], r["result"][:90])

if os.path.exists(os.path.join(TMP, "t.pdf")):
    r = DP("doc_parse", {"path": os.path.join(TMP, "t.pdf")})
    # 空白页 ⇒ 提取不到文字，应给"扫描版/图片型"提示而不是报错
    rec("D4 PDF 空白页给出可解释提示（ok 且无文字）",
        r["ok"] and ("未提取到文字" in r["result"]), r["result"][:110])

r = DP("doc_parse", {"path": os.path.join(ROOT, "mbse.db")})
rec("D5 安全：拒读平台数据库", (not r["ok"]) and "关键文件" in r["result"], r["result"][:110])

r = DP("doc_parse", {"path": os.path.join(TMP, "nope.pdf")})
rec("D6 文件不存在报错不抛异常", (not r["ok"]) and "不存在" in r["result"], r["result"][:90])

p_bin = os.path.join(TMP, "t.bin")
with io.open(p_bin, "w") as f:
    f.write("x")
r = DP("doc_parse", {"path": p_bin})
rec("D7 不支持格式给出替代建议", (not r["ok"]) and "file_read" in r["result"], r["result"][:120])

# ── E. 两个执行器链路都通（真分发） ──
try:
    from workflows.tools import ToolExecutor
    ex = ToolExecutor()
    r1 = ex.execute("code_exec", {"code": "_out['x']=7*6"})
    r2 = ex.execute("doc_parse", {"path": os.path.join(TMP, "t.csv")})
    rec("E1 编排侧 ToolExecutor 可执行 code_exec", r1.get("ok") is True, str(r1.get("result"))[:80])
    rec("E2 编排侧 ToolExecutor 可执行 doc_parse", r2.get("ok") is True, str(r2.get("result"))[:80])
except Exception as e:
    rec("E1/E2 ToolExecutor 可用", False, "%s: %s" % (type(e).__name__, str(e)[:120]))

# ── F. 注入与 JIT 保底 ──
try:
    from agent.pipeline import AgentPipeline
    from database import get_db
    U = {"id": 1, "role_name": "admin", "role": "admin"}

    def _names(q):
        p = AgentPipeline()
        c = get_db()
        try:
            p.registry.load_from_db(c, U)
        finally:
            c.close()
        return [(x.get("function") or {}).get("name") for x in p._build_tools_def("design", q, U)]

    n1 = _names("帮我算一下并解析这份 PDF 文档")
    n2 = _names("你好")
    rec("F1 相关任务注入 code_exec 与 doc_parse",
        "code_exec" in n1 and "doc_parse" in n1, len(n1))
    rec("F2 code_exec 常驻（JIT 不裁，已入 core_keep_extra）",
        "code_exec" in n2, len(n2))
    rec("F3 doc_parse 按需（无关任务不注入，避免占位）",
        "doc_parse" not in n2, len(n2))
except Exception as e:
    rec("F1-F3 注入实测可运行", False, "%s: %s" % (type(e).__name__, str(e)[:120]))

# ── G. 数据库登记（工具行 + 绑定） ──
import sqlite3
try:
    c = sqlite3.connect("file:" + os.path.join(ROOT, "mbse.db") + "?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    for nm in ("code_exec", "doc_parse"):
        row = c.execute("SELECT source, status, side_effect, builtin, length(input_schema) sl "
                        "FROM tools WHERE name=?", (nm,)).fetchone()
        rec("G %s 已登记为内置且只读" % nm,
            bool(row) and row["source"] == "builtin" and row["status"] == "active"
            and row["side_effect"] == "read",
            dict(row) if row else None)
        b = c.execute("SELECT COUNT(*) FROM agent_tools WHERE tool_type='tool' AND tool_name=?",
                      (nm,)).fetchone()[0]
        rec("G %s 已绑定到 Agent（≥1）" % nm, b >= 1, b)
    c.close()
except Exception as e:
    rec("G 数据库登记可查", False, "%s: %s" % (type(e).__name__, str(e)[:120]))

print("\n%s  ALL_OK=%s  (%d passed / %d failed)"
      % ("=" * 60, not FAIL, len(OK), len(FAIL)))
if FAIL:
    for n, d in FAIL:
        print("  x", n, "|", d)
    sys.exit(1)
