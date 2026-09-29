"""文本抽取：按格式分派（txt/md 直读、pdf/pdfplumber、docx/python-docx、csv/json）。

**2026-09-21 修复（P0 + P1）—— 三个静默缺陷**：

1. **图片伪装成解析成功**：原实现对图片 `return "[图片文件：…需 OCR 能力…]"`（46 字符占位串），
   该串**非空** → 通过 `ingest.py` 的空值守卫 → 照常分块 + 向量化 →
   **垃圾 chunk 污染向量库**，且 `parse_status='completed'`，从数据上完全看不出问题。
   → 现改为：有 OCR 能力则真正识别；无能力则返回**空串**并给出可操作原因。

2. **扫描版 PDF 静默失败**：原 `_extract_pdf` 只抽 pdfplumber 文本层，且 `except → return ""`，
   把"库坏了 / 文件损坏 / 加密"与"扫描件"混为一谈。
   → 现改为：**异常留痕**（logger.warning + 原因写进 meta），扫描件走 OCR。

3. **无 OCR 能力**：新增 `_extract_pdf_ex` / `_extract_image_ex`，接入 `knowledge_pipeline.ocr`
   （离线 ONNX，模型内置、零网络下载）。

**逐页判据（实测得出，见 `tmp/ocr_probe/probe_compare.py`）**：
- OCR 约 1.4~4.7 s/页，文本层 0.01~0.06 s/页 → **慢 50~200 倍**，**绝不可无差别全量 OCR**；
- 文本层稀疏页（272~364 字）OCR 得 193%~239%（**捞到图内文字**，是 OCR 的独占价值）；
- 文本层充足页（1650~2632 字）OCR 仅 80%~100%（**反而丢信息**：多栏顺序 / 连字符 / 上标）。
→ 策略：**文本层优先，仅对稀疏页补 OCR**（阈值 `extract.ocr_min_chars_per_page`，默认 100）。

**向后兼容**：`extract_text(filename, content) -> str` 签名与返回类型不变
（`ingest.py` / `search.py` / `tools/publish_release.py` 三处调用点无需改动）；
新增 `extract_text_ex(filename, content) -> (text, meta)` 供入库状态机读取诊断信息。
"""

import io
import logging
import os
import time

from . import ocr

logger = logging.getLogger(__name__)


def _cfg(key: str, default):
    """读 extract.* 配置（config 不可用时回退默认值）。"""
    try:
        from core import config
        return config.get("extract", key, default)
    except Exception:
        return default


def _ocr_meta(**over) -> dict:
    """构造 ocr 诊断块（入库侧把它写进 documents.pipeline_detail 留痕）。"""
    base = {
        "enabled": ocr.enabled(),
        "available": False,
        "pages_total": 0,
        "pages_ocr": 0,
        "chars_ocr": 0,
        "elapsed": 0.0,
        "note": "",
        "quality": None,      # {lines, avg_score, low_ratio} —— 仅在真正 OCR 后填
    }
    base.update(over)
    return base


def extract_text(filename: str, content) -> str:
    """按扩展名抽取纯文本；不支持格式返回空串（不抛错）。兼容 bytes 与 str 输入。

    支持：txt/md/csv/json/xml/yaml/log（文本）、docx/doc（段落+表格）、
    pdf（文本层 + 稀疏页 OCR）、xlsx/xls（表格转 Markdown）、pptx（文本）、
    图片（OCR 识别；无能力时返回空串而非占位串）。

    需要诊断信息（失败原因 / OCR 参与情况）时改用 `extract_text_ex`。
    """
    text, _meta = extract_text_ex(filename, content)
    return text


def extract_text_ex(filename: str, content):
    """抽取纯文本 + 诊断元信息 → (text, meta)。

    meta 结构：
        {
          "ok": bool,          # 是否拿到可用文本（False 时 text 为空串）
          "kind": str,         # text | office | pdf | image | unknown
          "reason": str,       # ok=False 时可直接落到 documents.error_msg 的可操作原因
          "ocr": {             # OCR 参与明细（未参与时 pages_ocr=0，note 说明原因）
              "enabled", "available", "pages_total", "pages_ocr",
              "chars_ocr", "elapsed", "note"
          },
        }
    """
    if isinstance(content, str):
        content = content.encode("utf-8", errors="replace")
    ext = os.path.splitext(filename or "")[1].lower()

    if ext in (".txt", ".md", ".json", ".xml", ".yaml", ".yml", ".log"):
        return content.decode("utf-8", errors="replace"), {
            "ok": True, "kind": "text", "reason": "", "ocr": _ocr_meta()}
    if ext == ".csv":
        return _extract_csv_table(content), {
            "ok": True, "kind": "text", "reason": "", "ocr": _ocr_meta()}
    if ext in (".docx", ".doc"):
        return _wrap_office(_extract_docx(content), "office",
                            "Word 文档解析失败（文件可能损坏，或为不支持的旧版 .doc 二进制格式）")
    if ext == ".pdf":
        return _extract_pdf_ex(content)
    if ext in (".xlsx", ".xls"):
        return _wrap_office(_extract_xlsx(content), "office",
                            "Excel 解析失败（请确认文件为有效 xlsx/xls）")
    if ext in (".pptx", ".ppt"):
        return _wrap_office(_extract_pptx(content), "office",
                            "PPT 解析失败（请确认文件为有效 pptx）")
    if ext in ocr.IMAGE_EXTS:
        return _extract_image_ex(content, ext)

    return "", {
        "ok": False, "kind": "unknown",
        "reason": (f"不支持的文件类型 {ext or '(无扩展名)'}；支持文本类(txt/md/csv/json/xml/yaml/log)、"
                   f"Word(docx)、PDF、Excel(xlsx/xls)、PPT(pptx)、图片(png/jpg/webp/...)"),
        "ocr": _ocr_meta(),
    }


def _wrap_office(text: str, kind: str, fail_reason: str):
    """Office 抽取结果包装：空结果给出明确原因，而不是让上游只看到"空文本"。"""
    text = text or ""
    if text.strip():
        return text, {"ok": True, "kind": kind, "reason": "", "ocr": _ocr_meta()}
    return "", {"ok": False, "kind": kind, "reason": fail_reason, "ocr": _ocr_meta()}


# ── 图片：无 OCR 能力时返回空串（而非占位串），由 meta.reason 说明原因 ──

def _ocr_quality_gate(quality: dict) -> tuple:
    """★ OCR 结果质量门禁 → (是否可用, 人类可读原因)。

    为什么必须有：RapidOCR 对低质截图照样能"吐出"几百字，只是**全是错字**
    （实测 doc 811：均分 0.595、95.7% 行 <0.7，"宽体客机"→"更体客机"）。
    只看"有没有字"（`if text.strip()`）会让这些乱码以 `parse_status=completed`
    入库 → 进向量库 → **被检索召回**（实测 query「宽体客机 定义」命中该乱码 chunk）。
    这是"静默污染"的升级版：占位串没了，换成"看起来像正文的乱码"，更难发现。

    判据（双条件，实测基线见 `tmp/ocr_probe/bench_score_baseline.py`）：
      ① avg_score >= extract.ocr_min_avg_score（默认 0.70）
      ② low_ratio <= extract.ocr_max_low_score_ratio（默认 50%，即低置信行占比）
    正例（清晰中文图 / 扫描 PDF / 混合 PDF 图页）实测 0.870~0.936、低分行 0% —— 余量 0.17；
    反例（低质截图）0.595~0.675、低分行 51.9%~95.7% —— **两维同时被挡，冗余安全**。

    第三分支（2026-09-29 用户拍板）：**密集文本低质放行**——
    行数 >= extract.ocr_dense_min_lines（默认 30）且 avg >= extract.ocr_dense_min_avg（默认 0.65）
    → 放行并打"低质放行"标（note + ocr.degraded，质量分如实落 documents.quality_score）。
    动机：doc 819 密集界面截图 84 行、均分 0.696（差 0.004 被拒）——行数多本身就是
    "真实内容结构"的证据，纯图形/照片不会吐出几十行；真乱码（doc 811：0.595/95.7%）仍被挡。
    """
    min_avg = _safe_float(_cfg("ocr_min_avg_score", 0.70), 0.70)
    max_low = _safe_float(_cfg("ocr_max_low_score_ratio", 50.0), 50.0)
    dense_lines = _safe_float(_cfg("ocr_dense_min_lines", 30.0), 30.0)
    dense_avg = _safe_float(_cfg("ocr_dense_min_avg", 0.65), 0.65)
    q = quality or {}
    # ⚠️ 坑：这里**绝不能**写 `q.get("low_ratio") or 100.0` —— `0.0` 是**合法且最好**的值
    # （低置信行 0%），而 `or` 会把 0.0 当 falsy 顶成 100% → 把最清晰的样本误杀成"全部低置信"。
    # 实测踩到：清晰中文图 quality={avg:0.87, low:0.0} 被判"低置信行 100% > 50%"而失败。
    def _num(key, default):
        v = q.get(key)
        return float(v) if v is not None else default
    avg = _num("avg_score", 0.0)
    low = _num("low_ratio", 100.0)
    lines = int(q.get("lines") or 0)
    if lines and avg >= min_avg and low <= max_low:
        return True, ""
    # 2026-09-29 密集文本低质放行（用户拍板）：见 docstring 第三分支
    if lines >= dense_lines and avg >= dense_avg:
        return True, (f"低质放行（密集文本）：{lines} 行、均分 {avg:.3f}、低置信行 {low:.1f}% "
                      f"未达常规门槛（均分 {min_avg:.2f} / 低置信 ≤{max_low:.0f}%），"
                      f"但行数 ≥{dense_lines:.0f} 判为真实内容结构，放行入库")
    # 2026-09-29 修复：原模板把两个条件**都**硬印出来（"均分 x < 门槛、低置信行 y% > 上限"），
    # 实测 doc 819 低置信 48.8% 本已达标（≤50%）却被印成 "49% > 50%"，误导排障。
    # 现只列**真正未达标**的条件，且均分给 3 位小数（0.696 vs 0.70 的 0.004 级差距靠 .2f 看不见）。
    _why = []
    if not lines:
        _why.append("未识别出文本行")
    else:
        if avg < min_avg:
            _why.append(f"均分 {avg:.3f} < 门槛 {min_avg:.2f}")
        if low > max_low:
            _why.append(f"低置信行 {low:.1f}% > 上限 {max_low:.0f}%")
    return False, (f"OCR 识别质量过低（{lines} 行、{'、'.join(_why)}）—— "
                   f"疑似纯图形/照片或分辨率过低；未入库以免污染检索"
                   f"（如需放行可在 config 调 extract.ocr_min_avg_score / ocr_max_low_score_ratio 后重试）")


def _extract_image_ex(content: bytes, ext: str):
    info = _ocr_meta(pages_total=1)
    ok, why = ocr.available()
    info["available"] = ok
    if not ok:
        info["note"] = f"OCR 不可用：{why}"
        return "", {
            "ok": False, "kind": "image",
            "reason": f"图片（{ext}）需 OCR 能力才能提取文本，当前不可用 —— {why}",
            "ocr": info,
        }
    t0 = time.time()
    text, quality = ocr.recognize_image_bytes_scored(content)
    info["pages_ocr"] = 1
    info["elapsed"] = round(time.time() - t0, 3)
    info["chars_ocr"] = len(text)
    info["quality"] = quality          # 均分 / 低置信行占比 / 行数，前端详情面板可查
    if not text.strip():
        return "", {
            "ok": False, "kind": "image",
            "reason": "图片 OCR 未识别出文本（可能是纯图形/照片，或分辨率过低）",
            "ocr": info,
        }
    # ★ 质量门禁：低质结果判失败并给出可操作原因，绝不让它以 completed 入库
    usable, reason = _ocr_quality_gate(quality)
    if not usable:
        info["note"] = reason
        return "", {"ok": False, "kind": "image", "reason": reason, "ocr": info}
    if reason:
        # 密集文本低质放行：note 说明放行原因（详情面板可见），degraded 标记供下游统计
        info["note"] = reason
        info["degraded"] = True
    return text, {"ok": True, "kind": "image", "reason": "", "ocr": info}


# ── PDF：文本层优先 + 稀疏页补 OCR ──

def _extract_pdf_ex(content: bytes):
    info = _ocr_meta()
    try:
        import pdfplumber
    except ImportError as e:
        logger.warning("PDF 解析不可用（缺 pdfplumber）：%s", e)
        return "", {
            "ok": False, "kind": "pdf",
            "reason": f"PDF 解析依赖 pdfplumber 未安装：{e}",
            "ocr": info,
        }

    limit = _pdf_max_pages()
    try:
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            pages = pdf.pages[:limit] if limit else pdf.pages
            layer = [(p.extract_text() or "").strip() for p in pages]
    except Exception as e:
        # 留痕：原实现 `except: return ""` 会把"文件损坏/加密/库异常"伪装成"空文件"
        logger.warning("PDF 文本层抽取失败: %s: %s", type(e).__name__, e)
        return "", {
            "ok": False, "kind": "pdf",
            "reason": f"PDF 打开/解析失败（{type(e).__name__}: {e}）—— 文件可能损坏、加密或格式异常",
            "ocr": info,
        }

    info["pages_total"] = len(layer)
    threshold = _safe_int(_cfg("ocr_min_chars_per_page", 100), 100)
    sparse = [i for i, t in enumerate(layer) if len(t) < threshold]

    # 文本层充足 → 直接用文本层，不付 OCR 的时间成本（实测快 50~200 倍）
    if not sparse:
        text = "\n".join(t for t in layer if t)
        info["note"] = f"文本层充足（每页 >= {threshold} 字），未使用 OCR"
        if text.strip():
            return text, {"ok": True, "kind": "pdf", "reason": "", "ocr": info}
        return "", {"ok": False, "kind": "pdf",
                    "reason": "PDF 无文本内容（未提取到任何字符）", "ocr": info}

    ok, why = ocr.available()
    info["available"] = ok
    if not ok:
        info["note"] = f"{len(sparse)}/{len(layer)} 页文本层稀疏，但 OCR 不可用：{why}"
        text = "\n".join(t for t in layer if t)
        if text.strip():
            # 部分页有文本 → 仍入库（总比没有好），缺失部分已在 ocr.note 留痕
            return text, {"ok": True, "kind": "pdf", "reason": "", "ocr": info}
        return "", {
            "ok": False, "kind": "pdf",
            "reason": (f"扫描版 PDF：{len(layer)} 页均无文本层，需 OCR 能力，"
                       f"而当前不可用 —— {why}"),
            "ocr": info,
        }

    cap = ocr.max_ocr_pages()
    todo = sparse if not cap else sparse[:cap]
    info["pages_ocr"] = len(todo)
    if cap and len(sparse) > cap:
        info["note"] = (f"稀疏页共 {len(sparse)} 页，受 extract.ocr_max_pages={cap} 限制，"
                        f"本次仅 OCR 前 {cap} 页")

    got, elapsed = ocr.recognize_pdf_pages_scored(content, todo)
    info["elapsed"] = elapsed

    merged = list(layer)
    added = 0
    rejected = []            # ★ 因质量不达标被丢弃的页（不并入正文，只留痕）
    qsum = {"lines": 0, "avg_score": [], "low_ratio": []}
    for i, item in got.items():
        t, quality = item
        if not (t and t.strip()) or not (0 <= i < len(merged)):
            continue
        usable, _why = _ocr_quality_gate(quality)
        if not usable:
            rejected.append(i + 1)     # 页面号按人类习惯（1 起）
            continue
        # 该页有文本层 → 追加（图内文字是文本层的增量）；无 → 用 OCR 结果
        merged[i] = (merged[i] + "\n" + t) if merged[i].strip() else t
        added += len(t)
        qsum["lines"] += int(quality.get("lines") or 0)
        qsum["avg_score"].append(float(quality.get("avg_score") or 0))
        qsum["low_ratio"].append(float(quality.get("low_ratio") or 0))
    info["chars_ocr"] = added
    if qsum["avg_score"]:
        info["quality"] = {
            "lines": qsum["lines"],
            "avg_score": round(sum(qsum["avg_score"]) / len(qsum["avg_score"]), 3),
            "low_ratio": round(sum(qsum["low_ratio"]) / len(qsum["low_ratio"]), 1),
        }
    if rejected:
        info["rejected_pages"] = rejected
        prev = info["note"]
        info["note"] = ((prev + "；" if prev else "") +
                        f"第 {','.join(str(x) for x in rejected)} 页 OCR 质量未达门禁，已丢弃未并入正文")

    text = "\n".join(t for t in merged if t.strip())
    if not text.strip():
        return "", {
            "ok": False, "kind": "pdf",
            "reason": ("扫描版 PDF：无文本层，且 OCR 结果全部未通过质量门禁"
                       + (f"（第 {','.join(str(x) for x in rejected)} 页被丢弃）" if rejected else "")
                       + "（可能是纯图形页或图片质量过低）"),
            "ocr": info,
        }
    if not info["note"]:
        info["note"] = f"OCR 补充 {len(todo)} 页，新增 {added} 字"
    return text, {"ok": True, "kind": "pdf", "reason": "", "ocr": info}


def _extract_pdf(content: bytes) -> str:
    """（兼容壳）只返回文本 —— 保留 2026-08 拆包以来的对外导入契约。

    消费者：`knowledge_pipeline/__init__.py` 的 re-export、`tmp/v2docs/pdf_recap.py`。
    新代码请用 `_extract_pdf_ex`（带诊断 meta 与 OCR 参与明细）；
    本函数语义等同旧版：拿不到文本即返回空串（不抛错）。
    """
    text, _meta = _extract_pdf_ex(content)
    return text


def _safe_int(v, default: int) -> int:
    try:
        return int(v)
    except Exception:
        return default


def _safe_float(v, default: float) -> float:
    try:
        return float(v)
    except Exception:
        return default


# ── 各格式基础抽取（失败一律返回空串 + 留痕，由上层 meta 给原因）──

def _extract_csv_table(content: bytes) -> str:
    """CSV 表格化：保留表头与行列结构（信息不丢失）。"""
    try:
        import io as _io
        import csv as _csv
        text = content.decode("utf-8", errors="replace")
        rows = list(_csv.reader(_io.StringIO(text)))
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
    """Excel 转 Markdown 表格：保留 sheet 名 / 表头 / 行列（信息不丢失）。失败返回空串。"""
    try:
        import io as _io
        from openpyxl import load_workbook
        wb = load_workbook(_io.BytesIO(content), read_only=True, data_only=True)
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
    except Exception as e:
        # 原实现返回 "[Excel 解析失败：…]" 占位串 → 非空 → 会进向量库。改为留痕 + 空串。
        logger.warning("Excel 解析失败: %s: %s", type(e).__name__, e)
        return ""


def _extract_pptx(content: bytes) -> str:
    """PPT 文本抽取（每页标题+正文，保留页序）。失败返回空串并留痕。"""
    try:
        import io as _io
        from pptx import Presentation
        prs = Presentation(_io.BytesIO(content))
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
    except Exception as e:
        logger.warning("PPT 解析失败: %s: %s", type(e).__name__, e)
        return ""


def _extract_docx(content: bytes) -> str:
    """Word 段落 + 表格文本。失败返回空串并留痕。"""
    try:
        import io as _io
        from docx import Document
        doc = Document(_io.BytesIO(content))
        parts = [p.text for p in doc.paragraphs if p.text.strip()]
        for table in doc.tables:
            for row in table.rows:
                cells = [c.text.strip() for c in row.cells if c.text.strip()]
                if cells:
                    parts.append(" | ".join(cells))
        return "\n".join(parts)
    except Exception as e:
        logger.warning("Word 解析失败: %s: %s", type(e).__name__, e)
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
