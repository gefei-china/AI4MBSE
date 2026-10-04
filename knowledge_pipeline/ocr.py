"""OCR 兜底能力：把 PDF 页 / 图片转成文本（离线 ONNX 引擎，不联网）。

**定位**：本模块只提供"识别能力"（引擎 + 渲染 + 识别 + 置信度指标），
**不做"什么时候该识别 / 结果能不能用"的业务判断** —— 判据与合并逻辑在 `extract.py`。

★ 2026-09-21 补：**置信度明细是本模块的二等公民转正**。
原先 `recognize()` 只拼文本、把 RapidOCR 的 score 整条丢弃 →
低质截图（doc 811：均分 0.595、95.7% 行 <0.7）的乱码**被当正文入库并污染 RAG**。
现在 `*_scored()` 系列返回 `(text, quality)`，老签名保留为兼容壳（返回类型不变）。

**依赖**（2026-09-21 接入，破 AGENTS.md 铁律 4 的第 1 例，理由见 requirements.txt）：
- `rapidocr-onnxruntime`：离线 ONNX 引擎，**模型内置包内**（det 2.4M + rec 11M + cls 0.57M），
  **零网络下载**、CPU 推理、无 torch / 无 paddle。授权 Apache-2.0（可商用私有化）。
- `pypdfium2` / `pillow`：**工程既有依赖**（pdfplumber 传递依赖），负责 PDF→位图，零新增。

**降级纪律**（对齐 AGENTS.md 坑 11「降级必须留痕」）：
引擎缺失 / 加载失败 / 单页识别异常，一律**不抛给调用方**，返回空串并 `logger.warning` 留痕；
由调用方读 `available()` 的原因决定落库状态（这样前端能显示"为什么不行"而不是笼统失败）。
"""

import io
import logging
import threading
import time

logger = logging.getLogger(__name__)

# 引擎懒加载单例（线程安全：FastAPI 多线程下避免并发重复加载 46MB 的 onnxruntime session）
_ENGINE = None
_ENGINE_ERR = ""
_ENGINE_LOCK = threading.Lock()

# 已知的图片扩展名（extract.py 与本模块共用同一份口径，避免两处漂移）
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tiff", ".tif")

# 「低置信行」的分界线（用于 low_ratio 统计）。
# 实测基线（2026-09-21，见 tmp/ocr_probe/bench_score_baseline.py）：
#   清晰中文图 / 扫描 PDF / 混合 PDF 图页 → 均分 0.870~0.936、低分行 **0%**
#   低质截图（doc 811）                  → 均分 0.595~0.675、低分行 51.9%~95.7%
# 两类分离度极大，0.7 是天然切点。
LOW_SCORE = 0.7


def _cfg(key: str, default):
    """读 extract.* 配置（config 不可用时回退默认值，不抛错）。"""
    try:
        from core import config
        return config.get("extract", key, default)
    except Exception:
        return default


def enabled() -> bool:
    """OCR 总开关（config: extract.ocr_enabled，默认开）。"""
    v = _cfg("ocr_enabled", True)
    if isinstance(v, str):
        return v.strip().lower() not in ("0", "false", "no", "off", "")
    return bool(v)


def available() -> tuple:
    """OCR 引擎是否可用 → (可用: bool, 原因: str)。

    首次调用会真正加载引擎（实测约 0.28s，模型在主进程内存中）。失败后**缓存**原因，
    避免逐页重试刷屏；重新安装依赖需重启服务（与工程"改后端需重启"的约定一致）。
    """
    global _ENGINE, _ENGINE_ERR
    if _ENGINE is not None:
        return True, ""
    if not enabled():
        return False, "OCR 已在配置中关闭（extract.ocr_enabled=false）"
    with _ENGINE_LOCK:
        if _ENGINE is not None:
            return True, ""
        if _ENGINE_ERR:
            return False, _ENGINE_ERR
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError as e:
            _ENGINE_ERR = (f"未安装 OCR 引擎（rapidocr-onnxruntime: {e}）；"
                           f"离线安装：pip install rapidocr-onnxruntime")
            logger.warning("OCR 不可用：%s", _ENGINE_ERR)
            return False, _ENGINE_ERR
        try:
            t0 = time.time()
            _ENGINE = RapidOCR()
            logger.info("OCR 引擎就绪（加载 %.2fs，模型内置包内、无网络下载）", time.time() - t0)
            return True, ""
        except Exception as e:
            _ENGINE_ERR = f"OCR 引擎加载失败：{type(e).__name__}: {e}"
            logger.warning("OCR 不可用：%s", _ENGINE_ERR)
            return False, _ENGINE_ERR


def reset_engine() -> None:
    """清空引擎缓存（仅测试用：装完依赖无需重启即可重试）。"""
    global _ENGINE, _ENGINE_ERR
    with _ENGINE_LOCK:
        _ENGINE = None
        _ENGINE_ERR = ""


def _to_array(img):
    """PIL.Image / ndarray → ndarray；PIL 时按配置转灰度。

    ★ 灰度是实测结论（2026-09-21）：doc 811 截图原图均分 0.595 → 灰度 0.675，
    且耗时还降 0.14s（"又快又好"）。放大 2x/3x 反而更差（0.645/0.623，耗时翻倍），别做。
    """
    import numpy as _np
    if isinstance(img, _np.ndarray):
        return img
    try:
        if _cfg("ocr_grayscale", True):
            img = img.convert("L").convert("RGB")   # 引擎要 3 通道，转 L 后回 RGB
    except Exception:
        pass
    return _np.array(img)


def _quality(scores) -> dict:
    """把逐行置信度压成质量指标（门禁判据在上层 extract.py，本层只给数据）。

    ⚠️ 为什么必须有这个：RapidOCR 的 score 原先**被整条丢弃**，
    于是"273 字乱码（均分 0.595、95.7% 行 <0.7）"也会当成正文入库 → 污染向量库与 RAG。
    """
    n = len(scores)
    if not n:
        return {"lines": 0, "avg_score": 0.0, "low_ratio": 100.0, "min_score": 0.0}
    low = sum(1 for s in scores if s < LOW_SCORE)
    return {
        "lines": n,
        "avg_score": round(sum(scores) / n, 3),
        "low_ratio": round(100.0 * low / n, 1),   # 低置信行占比（%）
        "min_score": round(min(scores), 3),
    }


def recognize_scored(img) -> tuple:
    """识别单张图 → (text, quality)。

    quality = {lines, avg_score, low_ratio, min_score}；不可用/无结果时 quality 全零值。
    **业务门禁（判 failed 还是入库）在 extract.py**，本层不越权。
    """
    ok, _why = available()
    if not ok:
        return "", _quality([])
    try:
        arr = _to_array(img)
        res, _elapse = _ENGINE(arr)
        if not res:
            return "", _quality([])
        lines, scores = [], []
        for item in res:
            # RapidOCR 返回每项 [box, text, score]，score 是 str（勿直接 :.2f 格式化）
            txt = item[1] if len(item) > 1 else ""
            if txt and str(txt).strip():
                lines.append(str(txt).strip())
                try:
                    scores.append(float(item[2]))
                except Exception:
                    scores.append(0.0)
        return "\n".join(lines), _quality(scores)
    except Exception as e:
        logger.warning("OCR 识别失败（该页跳过，不阻断入库）：%s: %s", type(e).__name__, e)
        return "", _quality([])


def recognize(img) -> str:
    """识别单张图 → 文本（按识别行序拼接）。不可用 / 失败返回空串并留痕。

    img 接受 PIL.Image / numpy.ndarray。
    ⚠️ 兼容壳：需要置信度明细请用 `recognize_scored()` —— 只认文本会重演
    "乱码当正文入库"的事故（见模块 docstring 与 extract.py 的质量门禁）。
    """
    text, _q = recognize_scored(img)
    return text


def recognize_image_bytes_scored(content: bytes) -> tuple:
    """识别图片字节流 → (text, quality)。"""
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(content))
        img.load()
        return recognize_scored(img)
    except Exception as e:
        logger.warning("图片解码失败（无法 OCR）：%s: %s", type(e).__name__, e)
        return "", _quality([])


def recognize_image_bytes(content: bytes) -> str:
    """识别图片字节流 → 文本（解析失败返回空串并留痕）。兼容壳。"""
    text, _q = recognize_image_bytes_scored(content)
    return text


def recognize_pdf_pages_scored(content: bytes, page_indices, scale: float = None) -> tuple:
    """批量识别 PDF 指定页 → ({页号(0起): (text, quality)}, 耗时秒)。

    一次打开文档逐页处理（比逐页重开快得多）；单页失败只跳过该页。
    """
    ok, _why = available()
    if not ok:
        return {}, 0.0
    try:
        import pypdfium2 as pdfium
    except ImportError as e:
        logger.warning("PDF 渲染不可用（缺 pypdfium2）：%s", e)
        return {}, 0.0

    scale = float(scale if scale is not None else _cfg("ocr_render_scale", 2.0))
    want = sorted({int(i) for i in page_indices})
    out = {}
    t0 = time.time()
    doc = None
    try:
        doc = pdfium.PdfDocument(io.BytesIO(content))
        n = len(doc)
        for idx in want:
            if idx < 0 or idx >= n:
                continue
            try:
                pil = doc[idx].render(scale=scale).to_pil()
                out[idx] = recognize_scored(pil)
            except Exception as e:
                logger.warning("PDF 第 %d 页渲染/识别失败（跳过）：%s: %s", idx + 1, type(e).__name__, e)
                out[idx] = ("", _quality([]))
    except Exception as e:
        logger.warning("PDF 打开失败（无法 OCR）：%s: %s", type(e).__name__, e)
    finally:
        if doc is not None:
            try:
                doc.close()
            except Exception:
                pass
    return out, round(time.time() - t0, 3)


def recognize_pdf_pages(content: bytes, page_indices, scale: float = None) -> dict:
    """批量识别 PDF 指定页 → ({页号(0起): 文本}, 耗时秒)。兼容壳。

    ⚠️ 需要逐页置信度（做质量门禁）请用 `recognize_pdf_pages_scored()`。
    """
    got, elapsed = recognize_pdf_pages_scored(content, page_indices, scale)
    return {i: t for i, (t, _q) in got.items()}, elapsed


def max_ocr_pages() -> int:
    """**文档级**OCR 页数硬上限（config: extract.ocr_max_pages，默认 **0=不限**）。

    ⚠️ P1-3（2026-10-04）语义变更：原默认 50，实际效果是**静默丢弃**超出部分
    （用户传 200 页扫描 PDF 会得到"解析成功"、只索引前 50 页、界面毫无提示）。
    标杆做法（RAGFlow v0.25 分段解析 / Unstructured 分批 / MinerU 按页并行）
    一致：**页数不是产品语义，批大小才是** ⇒ 默认改为 0（不限），
    内存与时长由 `ocr_page_batch`（分段）+ `ocr_time_budget_sec`（预算+续跑）控制。

    保留本函数是因为 `extract.py` 仍读它做兼容判断；若被显式设为 >0
    （有人刻意要硬上限），`extract.py` 会**显式告警**而不是静默丢弃。
    """
    try:
        n = int(_cfg("ocr_max_pages", 0))
        return n if n > 0 else 0
    except Exception:
        return 0


def ocr_page_batch() -> int:
    """分段粒度：每批处理多少页（config: extract.ocr_page_batch，默认 50）。

    为什么分段而不是一次跑完：OCR 实测 1.4~4.7 s/页，691 页全量要 30~50 分钟。
    一次 `recognize_pdf_pages_scored` 会把**所有页的识别结果**累积在内存里
    （页图虽逐页释放，但文本 + 质量字典全留着）⇒ 500 页时字典本身就很可观。
    分段后每批结束即合并进主结果并释放该批，内存峰值与**总页数解耦**。
    """
    try:
        n = int(_cfg("ocr_page_batch", 50))
        return max(1, n) if n > 0 else 50
    except Exception:
        return 50


def ocr_time_budget_sec() -> int:
    """单文档 OCR 时间预算（秒，config: extract.ocr_time_budget_sec，默认 1800）。

    为什么要预算而不是硬上限：预算是**可续跑**的 —— 超预算时把已完成页的文本
    落盘（`documents.ocr_progress`），下次从断点继续；而硬上限只能"从头再来"。
    这正是 RAGFlow「分段解析 +惰性加载」解决大 PDF 的同一思路。
    """
    try:
        n = int(_cfg("ocr_time_budget_sec", 1800))
        return n if n > 0 else 0
    except Exception:
        return 1800
