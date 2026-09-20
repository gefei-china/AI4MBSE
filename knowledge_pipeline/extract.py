"""文本抽取：按格式分派（txt/md 直读、pdf/pdfplumber、docx/python-docx、csv/json）。"""
import json
import os
import re
import time
import uuid


# 文本抽取：按格式分派（库缺失时优雅降级）
def extract_text(filename: str, content) -> str:
    """按扩展名抽取纯文本；不支持格式返回空串（不抛错）。兼容 bytes 与 str 输入。

    支持：txt/md/csv/json/xml/yaml/log（文本）、docx/doc（段落+表格）、
    pdf（文本层）、xlsx/xls（表格转 Markdown）、pptx（文本）、图片（占位提示需 OCR）。
    """
    if isinstance(content, str):
        content = content.encode("utf-8", errors="replace")
    ext = os.path.splitext(filename)[1].lower()
    if ext in (".txt", ".md", ".json", ".xml", ".yaml", ".yml", ".log"):
        return content.decode("utf-8", errors="replace")
    if ext == ".csv":
        return _extract_csv_table(content)
    if ext in (".docx", ".doc"):
        return _extract_docx(content)
    if ext == ".pdf":
        return _extract_pdf(content)
    if ext in (".xlsx", ".xls"):
        return _extract_xlsx(content)
    if ext in (".pptx", ".ppt"):
        return _extract_pptx(content)
    if ext in (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tiff"):
        return "[图片文件：已保存副本，需 OCR 能力提取文本（当前未启用，建议转 PDF/文本后上传）]"
    return ""


def _extract_csv_table(content: bytes) -> str:
    """CSV 表格化：保留表头与行列结构（信息不丢失）。"""
    try:
        import io
        import csv as _csv
        text = content.decode("utf-8", errors="replace")
        rows = list(_csv.reader(io.StringIO(text)))
        if not rows:
            return ""
        lines = []
        for i, row in enumerate(rows):
            cells = [c.strip() for c in row]
            lines.append(" | ".join(cells))
            if i == 0:
                lines.append(" | ".join(["---"] * len(cells)))  # Markdown 表头分隔
        return "\n".join(lines)
    except Exception:
        return content.decode("utf-8", errors="replace")


def _extract_xlsx(content: bytes) -> str:
    """Excel 转 Markdown 表格：保留 sheet 名 / 表头 / 行列（信息不丢失）。"""
    try:
        import io
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        parts = []
        for ws in wb.worksheets:
            parts.append(f"## Sheet: {ws.title}（{ws.max_row}行 x {ws.max_column}列）")
            rows = []
            for row in ws.iter_rows(values_only=True):
                cells = ["" if v is None else str(v).strip() for v in row]
                if any(cells):
                    rows.append(cells)
            if rows:
                parts.append(" | ".join(rows[0]))
                parts.append(" | ".join(["---"] * len(rows[0])))
                for r in rows[1:]:
                    parts.append(" | ".join(r))
            parts.append("")
        wb.close()
        return "\n".join(parts)
    except Exception:
        return "[Excel 解析失败：请确认文件为有效 xlsx/xls]"


def _extract_pptx(content: bytes) -> str:
    """PPT 文本抽取（每页标题+正文，保留页序）。"""
    try:
        import io
        from pptx import Presentation
        prs = Presentation(io.BytesIO(content))
        parts = []
        for i, slide in enumerate(prs.slides, 1):
            slide_parts = []
            for shape in slide.shapes:
                if shape.has_text_frame:
                    txt = "\n".join(p.text for p in shape.text_frame.paragraphs if p.text.strip())
                    if txt:
                        slide_parts.append(txt)
                if getattr(shape, "has_table", False):
                    for row in shape.table.rows:
                        cells = [c.text.strip() for c in row.cells if c.text.strip()]
                        if cells:
                            slide_parts.append(" | ".join(cells))
            if slide_parts:
                parts.append(f"## 第{i}页\n" + "\n".join(slide_parts))
        return "\n\n".join(parts)
    except Exception:
        return "[PPT 解析失败：请确认文件为有效 pptx]"

def _extract_docx(content: bytes) -> str:
    try:
        import io
        from docx import Document
        doc = Document(io.BytesIO(content))
        parts = [p.text for p in doc.paragraphs if p.text.strip()]
        for table in doc.tables:
            for row in table.rows:
                cells = [c.text.strip() for c in row.cells if c.text.strip()]
                if cells:
                    parts.append(" | ".join(cells))
        return "\n".join(parts)
    except Exception:
        return ""

def _pdf_max_pages() -> int:
    """PDF 抽取页数上限（config: extract.pdf_max_pages，默认 1200；<=0 表示不限制）。

    2026-09-19 修复：原实现硬编码 `pdf.pages[:50]`，使页数多的规范类文档只能入库约 17%
    （实测 SysML v2 官方规范 691 页 → 全量 1,229,264 字符，截 50 页仅 215,265 字符 = 17.5%；
     KerML 规范 454 页 → 937,324 字符，截 50 页仅 172,773 字符 = 18.4%）。
    全量抽取耗时分别仅 23.6s / 17.6s，原上限并非性能所需。改为可配置 + 提高默认值，
    同时保留上限防超大文件拖垮入库。
    """
    try:
        from core import config
        n = int(config.get("extract", "pdf_max_pages", 1200))
        return n if n > 0 else 0
    except Exception:
        return 1200


def _extract_pdf(content: bytes) -> str:
    try:
        import io
        import pdfplumber
        limit = _pdf_max_pages()
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            pages = []
            for page in (pdf.pages[:limit] if limit else pdf.pages):
                t = page.extract_text() or ""
                if t.strip():
                    pages.append(t)
        return "\n".join(pages)
    except Exception:
        return ""

