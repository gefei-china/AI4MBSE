# -*- coding: utf-8 -*-
"""doc_parse —— 读取 PDF / DOCX / XLSX / CSV 等二进制文档的文本（2026-11-06）

## 为什么需要

`.venv` 里 pypdf 6.19 / pdfplumber 0.11 / python-docx / openpyxl 3.1 都已安装，
但**没有任何工具包装它们** ⇒ 模型拿到一个 PDF 只能看文件名。
「网页资料收集 + 本地文件处理」这两条链路的最后一步都卡在这里：
下载到 PDF 却读不了内容。

## 与 file_read 的分工

| 工具 | 读什么 |
|---|---|
| `file_read` | 纯文本（UTF-8） |
| `doc_parse` | **二进制容器**：PDF / DOCX / XLSX / CSV/TSV |

## 安全边界

· 只读（`side_effect='read'`），不写不改
· 只解析文本，不执行文档里的宏 / 脚本 / 外链
· 单次提取有字符上限（默认 20 万），超长自动截断并明确告知
· 路径复用 `file_tools` 的关键文件保护口径：**拒绝读平台自身运行时文件**
  （`.db` / `config.py` / `main.py`），避免"读文档"变成"拖库"
"""
from __future__ import annotations

import io
import os
import re

__all__ = ["exec_doc_tool", "DOC_TOOL_NAMES", "DOC_TOOL_DEFS"]

DOC_TOOL_NAMES = ("doc_parse",)

MAX_CHARS = 200_000

SUPPORTED_EXT = (".pdf", ".docx", ".xlsx", ".xlsm", ".csv", ".tsv", ".txt", ".md", ".json")

DOC_TOOL_DEFS = [
    {
        "name": "doc_parse",
        "description": (
            "提取文档正文文本（支持 PDF / Word .docx / Excel .xlsx / CSV / TSV）。"
            "用途：读取下载或上传的 PDF 报告、Word 技术文档、Excel 数据表，"
            "把内容取成文本后再分析。返回纯文本（表格按行用制表符拼接）。"
            "限制：仅提取文本与表格，不解析图片、公式、图表；不执行文档里的宏。"
            "纯文本文件请直接用 file_read。"
        ),
        "side_effect": "read",
        "risk_level": "low",
        "version": "v1.0",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文档绝对路径或相对路径"},
                "max_chars": {"type": "integer",
                              "description": "最多返回字符数，默认 200000"},
                "pages": {"type": "string",
                          "description": "仅 PDF：页码范围如 '1-5' 或 '3'；不传=全部"},
                "sheets": {"type": "string",
                           "description": "仅 Excel：工作表名，多个用逗号分隔；不传=全部"},
                "max_rows": {"type": "integer",
                             "description": "仅 Excel/CSV：每个表最多行数，默认 1000"},
            },
            "required": ["path"],
        },
    },
]


def _resolve(p: str) -> str:
    if not p or not str(p).strip():
        raise ValueError("path 不能为空")
    return os.path.abspath(os.path.expanduser(str(p).strip()))


def _guard(path: str) -> None:
    """拒绝读取平台自身运行时文件 —— 与 file_tools 的关键文件保护同口径。

    为什么在读侧也要拦：`doc_parse` 支持 .txt/.csv，若不加限制，
    "解析一个 csv" 就可以指向 `mbse.db`（SQLite 文件虽解析失败，
    但也可能被当成 .csv 读出一堆二进制噪声，反而误导模型）。
    """
    try:
        from file_tools import _is_critical       # 复用同一份判据，避免两套口径漂移
        if _is_critical(path):
            raise ValueError("拒绝读取平台运行时关键文件（数据库/配置/主程序）")
    except ImportError:
        pass
    except ValueError:
        raise
    except Exception:
        pass


def _parse_pdf(path: str, pages: str = "") -> str:
    """优先 pdfplumber（表格还原更好），失败回落 pypdf。"""
    pgs = None
    if pages:
        pgs = []
        for seg in str(pages).split(","):
            seg = seg.strip()
            if not seg:
                continue
            if "-" in seg:
                a, b = seg.split("-", 1)
                try:
                    pgs.extend(range(int(a), int(b) + 1))
                except ValueError:
                    pass
            else:
                try:
                    pgs.append(int(seg))
                except ValueError:
                    pass
    try:
        import pdfplumber
        out = []
        with pdfplumber.open(path) as pdf:
            total = len(pdf.pages)
            idx = pgs if pgs else range(1, total + 1)
            for i in idx:
                if i < 1 or i > total:
                    continue
                pg = pdf.pages[i - 1]
                out.append("--- 第 %d/%d 页 ---\n%s" % (i, total, pg.extract_text() or ""))
        if out:
            return "\n".join(out)
    except Exception:
        pass
    import pypdf
    out = []
    with open(path, "rb") as f:
        rd = pypdf.PdfReader(f)
        total = len(rd.pages)
        idx = pgs if pgs else range(1, total + 1)
        for i in idx:
            if i < 1 or i > total:
                continue
            out.append("--- 第 %d/%d 页 ---\n%s" % (i, total, rd.pages[i - 1].extract_text() or ""))
    return "\n".join(out)


def _parse_docx(path: str) -> str:
    import docx
    d = docx.Document(path)
    out = []
    for p in d.paragraphs:
        t = (p.text or "").strip()
        if t:
            out.append(t)
    for ti, tb in enumerate(d.tables or []):
        out.append("--- 表格 %d ---" % (ti + 1))
        for row in tb.rows:
            out.append("\t".join((c.text or "").strip() for c in row.cells))
    return "\n".join(out)


def _parse_xlsx(path: str, sheets: str = "", max_rows: int = 1000) -> str:
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    want = [s.strip() for s in str(sheets).split(",") if s.strip()] if sheets else None
    out = []
    for ws in wb.worksheets:
        if want and ws.title not in want:
            continue
        out.append("--- 工作表：%s ---" % ws.title)
        n = 0
        for row in ws.iter_rows(values_only=True):
            n += 1
            if n > max_rows:
                out.append("…（超过 %d 行，已截断）" % max_rows)
                break
            cells = ["" if c is None else str(c) for c in row]
            if any(cells):
                out.append("\t".join(cells))
    try:
        wb.close()
    except Exception:
        pass
    return "\n".join(out)


def _parse_csv(path: str, max_rows: int = 1000) -> str:
    import csv
    out = []
    # 编码兜底：企业环境常见 GBK 导出的 CSV
    for enc in ("utf-8-sig", "utf-8", "gbk", "latin-1"):
        try:
            with io.open(path, "r", encoding=enc, newline="") as f:
                rd = csv.reader(f, delimiter="\t" if path.lower().endswith(".tsv") else ",")
                n = 0
                for row in rd:
                    n += 1
                    if n > max_rows:
                        out.append("…（超过 %d 行，已截断）" % max_rows)
                        break
                    out.append("\t".join(row))
            return "\n".join(out)
        except UnicodeDecodeError:
            out = []
            continue
    return "\n".join(out)


def exec_doc_tool(name: str, arguments: dict | None = None) -> dict:
    """统一入口：解析文档并返回 {"ok": bool, "result": str}。异常不抛出。"""
    args = arguments or {}
    if name not in DOC_TOOL_NAMES:
        return {"ok": False, "result": "未知文档工具: %s" % name}

    try:
        path = _resolve(args.get("path"))
    except Exception as e:
        return {"ok": False, "result": str(e)}
    if not os.path.isfile(path):
        return {"ok": False, "result": "文件不存在：%s" % path}
    try:
        _guard(path)
    except ValueError as e:
        return {"ok": False, "result": str(e)}

    ext = os.path.splitext(path)[1].lower()
    if ext not in SUPPORTED_EXT:
        return {"ok": False,
                "result": "不支持的格式 %s（支持 %s）；纯文本请用 file_read"
                          % (ext or "(无扩展名)", "/".join(SUPPORTED_EXT))}

    try:
        max_chars = int(args.get("max_chars") or MAX_CHARS)
        max_chars = max(1000, min(max_chars, MAX_CHARS))
    except (TypeError, ValueError):
        max_chars = MAX_CHARS
    try:
        max_rows = int(args.get("max_rows") or 1000)
        max_rows = max(1, min(max_rows, 10000))
    except (TypeError, ValueError):
        max_rows = 1000

    try:
        if ext == ".pdf":
            text = _parse_pdf(path, str(args.get("pages") or ""))
        elif ext == ".docx":
            text = _parse_docx(path)
        elif ext in (".xlsx", ".xlsm"):
            text = _parse_xlsx(path, str(args.get("sheets") or ""), max_rows)
        elif ext in (".csv", ".tsv"):
            text = _parse_csv(path, max_rows)
        else:                       # .txt/.md/.json 兜底走纯文本
            with io.open(path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
    except Exception as e:
        return {"ok": False, "result": "解析失败（%s）：%s: %s"
                                      % (ext, type(e).__name__, str(e)[:200])}

    text = text or ""
    # ★ 2026-11-06：**页标记不算正文**。
    #   实测（门禁 D4 抓出）：扫描版/图片型 PDF 每页也能抽出我加的
    #   `--- 第 1/1 页 ---` 这行标记，`text.strip()` 因此非空 ⇒ 走正常路径
    #   返回一段"只有页号、没有内容"的文本，模型只能自己猜"文档是空的"。
    #   正确做法：剥掉页标记后再判空 —— 那才是"有没有文字层"的真实判据，
    #   也才能给出"需要 OCR"这条可行动的提示。
    _body = re.sub(r"(?m)^---\s*第\s*\d+/\d+\s*页\s*---\s*$", "", text)
    _body = re.sub(r"(?m)^---\s*(工作表|表格)[：:].*?---\s*$", "", _body)
    if not _body.strip():
        # 空文本是个"值得说清"的结果：扫描版 PDF 就是这样（无文字层，需 OCR）
        hint = ("（未提取到文字：可能是扫描版/图片型 PDF，需要 OCR）"
                if ext == ".pdf" else "（未提取到文字：文档可能为空或内容为图片）")
        return {"ok": True, "result": "【%s】%s" % (os.path.basename(path), hint)}

    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars]
    head = "【%s】共 %d 字符%s\n" % (
        os.path.basename(path), len(text),
        ("，已截断至 %d 字符（可加 pages/sheets/max_chars 缩小范围）" % max_chars) if truncated else "")
    return {"ok": True, "result": (head + text)[:max_chars + 300]}
