"""段落感知分块（中文友好，保留段落边界 + 重叠）：_is_complete_sentence/_tail_sentence/chunk_text/_chunk_table_rows/chunk_text_structured。"""
import json
import os
import re
import time
import uuid


def _is_complete_sentence(text: str) -> bool:
    """块是否为完整语义单元：以句尾标点结尾（。！？；;!?）。"""
    t = (text or "").rstrip()
    return bool(t) and t[-1] in "。！？；;!?"


def _tail_sentence(text: str, overlap: int, max_len: int | None = None) -> str:
    """句子级重叠：取 text 末尾最后一个完整句（含标点）。

    在末尾 max_len 窗口内找最后一个句子边界（。！？；；…\n），从边界后取整句；
    找不到句子边界则退化为尾部 overlap 字符（保持原有行为）。"""
    if not text:
        return ""
    if max_len is None:
        max_len = max(overlap * 2, 300)
    start = max(0, len(text) - max_len)
    window = text[start:]
    last_end = -1
    for m in re.finditer(r"[。！？；;！?\n]+", window):
        last_end = m.end()
    if last_end > 0:
        tail = window[last_end:].strip()
        if tail:
            return tail
    tail = text[-overlap:] if len(text) >= overlap else text
    return tail.lstrip("。！？；;！?，、 ")


def chunk_text(text: str, size: int = 600, overlap: int = 90, min_chunk: int = 50,
               sentence_overlap: bool = True) -> list:
    """段落感知分块 v2（中文友好，按字符计）。

    相比 v1（size=400/overlap=40）的改进：
    - 统一 flush 逻辑：段级路径与超长段"按句切"路径共用（v1 只有段级路径有重叠）
    - 句子级重叠：flush 时保留上一块末尾**完整句**（sentence_overlap=True，默认）；
      否则退化为尾部 overlap 字符
    - 块上限强制校验：任何路径产出的块长度 ≤ size + 末尾句（不再越界）
    - min_chunk：过滤语义碎片（默认 <50 字符的块丢弃）
    - 单句超长硬切：滑动步长 size-overlap，相邻硬切块天然带 overlap
    """
    text = re.sub(r"\n{3,}", "\n\n", text or "").strip()
    if not text:
        return []
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks, buf, buf_len = [], [], 0

    def _flush():
        """落一块 + 计算重叠尾巴（段级与句子级路径共用）。"""
        nonlocal buf, buf_len
        if not buf:
            return
        prev = "".join(buf)
        chunks.append(prev)
        tail = _tail_sentence(prev, overlap) if sentence_overlap else (
            prev[-overlap:] if len(prev) >= overlap else "")
        buf = [tail] if tail else []
        buf_len = len(tail)

    for para in paragraphs:
        if len(para) > size:
            # 超长段落：先按句切再组块
            sentences = re.split(r"(?<=[。！？；\n])", para)
            for sent in sentences:
                s = sent.strip()
                if not s:
                    continue
                if buf_len + len(s) > size and buf:
                    _flush()
                if len(s) > size:
                    # 单句仍超长：硬切（滑动窗口，相邻块重叠 = overlap）
                    for i in range(0, len(s), size - overlap):
                        chunks.append(s[i:i + size])
                else:
                    buf.append(s)
                    buf_len += len(s)
        else:
            if buf_len + len(para) > size and buf:
                _flush()
            buf.append(para)
            buf_len += len(para)
    _flush()
    out = [c.strip() for c in chunks if c and c.strip()]
    if min_chunk and min_chunk > 0:
        # 碎片过滤：无句尾标点的短块（<min_chunk）丢弃；
        # 完整句（语义单元，如 MBSE 一句话需求）允许短于 min_chunk（下限 10 字符）
        out = [c for c in out
               if len(c) >= min_chunk or (len(c) >= 10 and _is_complete_sentence(c))]
    return out


def _chunk_table_rows(rows: list, size: int, overlap: int, min_chunk: int,
                      sentence_overlap: bool, max_rows: int = 15) -> list:
    """表格行组块：行数 ≤ max_rows 整块保留；否则按每块 10-15 行分组。

    表格是结构化数据，按行组块而非按句切分（v1 会切碎表格行）。"""
    if not rows:
        return []
    if len(rows) <= max_rows:
        return [t.strip() for t in ["\n".join(rows)] if t.strip()]
    avg = max(len(r) for r in rows)
    per = max(1, min(max_rows, max(2, size // max(avg, 20))))
    out = []
    for i in range(0, len(rows), per):
        chunk = "\n".join(rows[i:i + per]).strip()
        if chunk:
            out.append(chunk)
    return out


def chunk_text_structured(text: str, size: int = 600, overlap: int = 90,
                          min_chunk: int = 50, sentence_overlap: bool = True,
                          table_max_rows: int = 15, code_block_chunk: bool = True,
                          doc_title: str = "") -> list:
    """结构感知分块 v2：识别标题行（# / 数字章节 / 中文章节）+ 代码块 + 表格。

    相比 v1 的改进：
    - 代码块（``` 围栏）独立成块，不被按句切碎（code_block_chunk）
    - markdown 表格连续行聚合组块（table_max_rows 内整块，超限按行分组）
    - embed_text：doc_title + section + content（Title-Enriched，供向量化，检索侧有章节上下文）
    - bm25_text：section + content（全文检索，与原行为一致）
    返回 [{content, section, bm25_text, embed_text}]
    """
    text = re.sub(r"\r\n", "\n", text or "")
    lines = text.split("\n")
    chunks, section = [], ""
    heading_re = re.compile(r"^\s*(#{1,6}\s+|第[一二三四五六七八九十0-9]+[章节部分]|\d+(\.\d+)*[\s、.])")
    code_re = re.compile(r"^\s*```")
    table_re = re.compile(r"^\s*\|.*\|\s*$")
    buf, table_rows = [], []

    def _emit(para: str):
        nonlocal chunks
        para = para.strip()
        if not para:
            return
        for c in chunk_text(para, size, overlap, min_chunk, sentence_overlap):
            chunks.append({
                "content": c,
                "section": section,
                "bm25_text": f"{section} {c}" if section else c,
                "embed_text": f"{doc_title} {section} {c}".strip() if doc_title or section else c,
            })

    def _emit_table():
        nonlocal table_rows
        if not table_rows:
            return
        parts = _chunk_table_rows(table_rows, size, overlap, min_chunk, sentence_overlap, table_max_rows)
        for c in parts:
            chunks.append({
                "content": c,
                "section": section,
                "bm25_text": f"{section} {c}" if section else c,
                "embed_text": f"{doc_title} {section} {c}".strip() if doc_title or section else c,
            })
        table_rows = []

    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        # 代码块：独立成块（不切片）
        if code_block_chunk and code_re.match(line):
            _emit("\n".join(buf))
            _emit_table()
            buf = []
            code = [line]
            i += 1
            while i < n and not code_re.match(lines[i]):
                code.append(lines[i])
                i += 1
            if i < n:
                code.append(lines[i])  # 闭合围栏
                i += 1
            body = "\n".join(code).strip()
            if len(body) >= 10:  # 代码块是完整语义单元，≥10 字符即保留（不套用 min_chunk 碎片规则）
                chunks.append({
                    "content": body,
                    "section": section,
                    "bm25_text": f"{section} {body}" if section else body,
                    "embed_text": f"{doc_title} {section} {body}".strip() if doc_title or section else body,
                })
            continue
        # 表格行：连续聚合
        if table_re.match(line):
            _emit("\n".join(buf))
            buf = []
            while i < n and table_re.match(lines[i]):
                table_rows.append(lines[i])
                i += 1
            continue
        # 普通行 / 标题行：先落已聚合的表格段（保持文档顺序），再处理
        _emit_table()
        if heading_re.match(line):
            if buf:
                _emit("\n".join(buf))
                buf = []
            section = line.strip()[:60]
            i += 1
            continue  # 标题行只作 section 标记，不进入 content
        buf.append(line)
        i += 1
    _emit("\n".join(buf))
    _emit_table()
    return chunks

