# -*- coding: utf-8 -*-
"""PDF 水印字符层过滤门禁（2026-11-12，doc 847 实证；米爸反馈2「只能识别封面和章节」）。

背景
----
doc 847《面向网络总体的AI赋能MBSE系统设计技术研究项目技术要求》（星网受控 PDF）：
文本层**每页恒定混入 432 个水印字符**（STSong-Light 20.4~26.3pt，字符集仅
`()-02368AEFJMS` 14 个符号、每个重复 567 次，原始文本里呈现为
`) ) )\\nM M M\\nE E E…` 的三连网格）⇒ 裸抽全进 chunk ⇒ 163 个 chunk 全污染
（每 chunk 仅 53~114 字符）⇒ AI 会话里「只解析出目录级标题，第 4~8 章具体
条款与指标识别不到」（用户截图澄清卡原话）。
→ `knowledge_pipeline/extract.py::_pdf_text_layer` 按四合一判据剔除水印字符层。

判据（W = 本文件断言）
----
W1 doc 847 真文件：8 个 STSong-Light 簇全命中、删 9072 字符（432×21 页）、
   正文关键条款（★[FR-KG-1]…）保留、水印网格残留清零、文档家具
   （「中国星网受控信息」16pt 横幅）不误伤；
W2 配置开关：watermark_filter_enabled=False ⇒ 回到裸抽（水印网格回到文本，
   35913 字符），还原后过滤重新生效；
W3 误杀防护：干净规范（ISO 29148，ArialMT 9.4pt 主体）过滤路径文本与裸抽
   **完全一致**（0 字符误删）；
W4 合成 PDF 靶场（reportlab，六种元素各锁定一条判据）：默认只删假水印；
   函数级内存孪生注入「关掉任判据」的错写法 ⇒ 对应受保护元素被误删
   ⇒ W4 断言红（证明四合一判据各自有牙、互不依赖）。

判据口径（宁漏勿误——误删正文条款比漏删水印更糟）：
  A 字体基名 ≠ 主体字体基名；B 字号 ≥ 主体字号 × 1.5；
  C 文档级高重复（distinct/total ≤ 0.1 且 total ≥ 20）；D 跨页 ≥ max(3, 20% 页数)。
"""
import inspect
import io
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

DOC847 = ROOT / "data/uploads/847_Print_面向网络总体的AI赋能MBSE系统设计技术研究项目技术要求.pdf"
DOC846 = ROOT / "data/uploads/846_ISO_29148-2011_Requirements_Engineering.pdf"
PASSED, FAILED = [], []


def chk(name, ok, detail=""):
    (PASSED if ok else FAILED).append(name)
    print(("  [PASS] " if ok else "  [FAIL] ") + name + (("  -- " + str(detail)) if detail else ""))


def section(t):
    print("\n" + "=" * 4 + " " + t + " " + "=" * 4)


def _cfg_set(key, value):
    from core import config
    orig = config._CONFIG["extract"].get(key)
    config._CONFIG["extract"][key] = value
    return orig


def _cfg_restore(key, orig):
    from core import config
    config._CONFIG["extract"][key] = orig


def _build_synth_pdf() -> bytes:
    """合成靶场 PDF（reportlab 已在 venv，零新依赖）。六种元素各锁定一条判据：

      ① 正文 Helvetica 11pt（主体签名）          —— 任何情形不许动
      ② 假水印 STSongStd-Light 24pt 高重复跨页    —— 默认必须删
      ③ 标题 Helvetica-Bold 18pt 非重复          —— C 判据保护（默认保留）
      ④ 页脚 Courier 9pt 高重复跨页              —— B 判据保护（字号小）
      ⑤ 局部横幅 STSongStd-Light 20pt 仅第 1 页   —— D 判据保护（不跨页）
      ⑥ 同字体大水印 Helvetica 24pt 高重复跨页     —— A 判据保护（同主体字体）

    ⑤ 必须与 ② **不同字号**（20 vs 24）——过滤按 (字体,字号) 簇删除，同簇的
    局部横幅会被 ② 的跨页性连带删掉，D 判据就失去了专属靶子。
    """
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    _W, H = A4
    body = ("Systems engineering body text paragraph one. "
            "The quick brown fox jumps over the lazy dog repeatedly. ")
    for pno in range(3):
        y = H - 60
        c.setFont("Helvetica", 11)                     # ① 正文（主体签名）
        for line in range(6):
            c.drawString(50, y, body + "line%d page%d" % (line, pno))
            y -= 16
        c.setFont("STSong-Light", 24)                  # ② 假水印（删）
        for i in range(8):
            c.drawString(60, 200 + i * 30, "FAKEWM8260" * 3)
        c.setFont("Helvetica-Bold", 18)                # ③ 标题（C 保护）
        c.drawString(50, y - 20, "Chapter One Introduction To Systems Modeling")
        c.setFont("Courier", 9)                        # ④ 页脚（B 保护）
        for i in range(5):
            c.drawString(50, 40 + i * 10, "PG0000001" * 2)
        if pno == 0:
            c.setFont("STSong-Light", 20)              # ⑤ 局部横幅（D 保护）
            for i in range(20):
                c.drawString(300, 400 + i * 12, "LOCALWMBANNER")
        c.setFont("Helvetica", 24)                     # ⑥ 同字体大水印（A 保护）
        for i in range(6):
            c.drawString(300, 600 + i * 30, "SAMEFONT-WM-000" * 2)
        c.showPage()
    c.save()
    return buf.getvalue()


def _twin_text_layer(mut_cluster_src: str):
    """函数级内存孪生：把变异后的 `_pdf_watermark_clusters` 源码编译进**模块全局
    副本**，再编译 `_pdf_text_layer` ⇒ 驱动函数整体走变异判据。
    不写生产文件（承 verify_orch_span.py M 段纪律）。"""
    import knowledge_pipeline.extract as EX
    g = dict(vars(EX))
    exec(compile(mut_cluster_src, "<wm_cluster_mut>", "exec"), g)
    exec(compile(inspect.getsource(EX._pdf_text_layer), "<wm_layer_mut>", "exec"), g)
    return g["_pdf_text_layer"]


def main() -> int:
    import pdfplumber
    from knowledge_pipeline import extract

    # ─────────────────────────────────────────────────────────────────────────
    section("W1 · doc 847 真文件：水印簇识别 + 正文条款保留 + 残留清零")
    text, meta = extract.extract_text_ex(DOC847.name, DOC847.read_bytes())
    wm = (meta.get("ocr") or {}).get("watermark") or {}
    chk("W1a 识别出水印 meta（clusters/chars_removed/chars_total）", bool(wm.get("clusters")), str(wm)[:150])
    chk("W1b 8 个 STSong-Light 簇全部命中",
        len(wm.get("clusters") or []) == 8 and all(c0.startswith("STSong-Light@") for c0 in wm["clusters"]),
        str(wm.get("clusters")))
    chk("W1c 删除字符数 = 9072（每页 432 × 21 页）", wm.get("chars_removed") == 9072,
        f"removed={wm.get('chars_removed')}")
    chk("W1d 文档总字符 27099（留痕口径）", wm.get("chars_total") == 27099, f"total={wm.get('chars_total')}")
    chk("W1e 正文关键条款保留（★[FR-KG-1] 设计数据管理首条）",
        "★[FR-KG-1]应支持从结构化数据库" in text)
    chk("W1f 章节标题保留（5.3.1 设计数据管理）", "5.3.1 设计数据管理" in text)
    chk("W1g 摘要段保留", "本文件规范了面向网络总体的AI赋能MBSE系统设计技术研究项目的技术要求" in text)
    chk("W1h 文档家具不误伤（中国星网受控信息）", "中国星网受控信息" in text)
    chk("W1i 水印网格残留清零（'- - -'×270 / 'M M M'×133 → 0）",
        "- - -" not in text and "M M M" not in text and "S S S" not in text)
    chk("W1j 过滤后正文规模合理（15000 ≤ len ≤ 22000）", 15000 <= len(text) <= 22000, f"len={len(text)}")
    chk("W1k 页数口径（pages_total=21）", (meta.get("ocr") or {}).get("pages_total") == 21)

    # ─────────────────────────────────────────────────────────────────────────
    section("W2 · 配置开关：关 ⇒ 回到裸抽；还原后重新生效")
    orig = _cfg_set("watermark_filter_enabled", False)
    try:
        t_raw, m_raw = extract.extract_text_ex(DOC847.name, DOC847.read_bytes())
    finally:
        _cfg_restore("watermark_filter_enabled", orig)
    chk("W2a 关过滤 ⇒ 无水印 meta", not (m_raw.get("ocr") or {}).get("watermark"))
    chk("W2b 关过滤 ⇒ 水印网格回到文本（裸抽 35913 字符）",
        "- - -" in t_raw and len(t_raw) > 25000, f"len={len(t_raw)}")
    t_again, m_again = extract.extract_text_ex(DOC847.name, DOC847.read_bytes())
    chk("W2c 还原后过滤重新生效（chars_removed=9072）",
        ((m_again.get("ocr") or {}).get("watermark") or {}).get("chars_removed") == 9072)

    # ─────────────────────────────────────────────────────────────────────────
    section("W3 · 误杀防护：干净规范 A/B 一致（过滤路径 == 裸抽路径）")
    t_a, m_a = extract.extract_text_ex(DOC846.name, DOC846.read_bytes())
    orig = _cfg_set("watermark_filter_enabled", False)
    try:
        t_b, m_b = extract.extract_text_ex(DOC846.name, DOC846.read_bytes())
    finally:
        _cfg_restore("watermark_filter_enabled", orig)
    chk("W3a ISO29148（95 页，ArialMT 9.4pt 主体）无水印 meta",
        not (m_a.get("ocr") or {}).get("watermark"))
    chk("W3b 过滤路径文本与裸抽完全一致（0 字符误删）", t_a == t_b,
        f"len_a={len(t_a)} len_b={len(t_b)}")

    # ─────────────────────────────────────────────────────────────────────────
    section("W4 · 合成 PDF 靶场：四合一判据各自有牙（变异自证，内存孪生）")
    pdf = pdfplumber.open(io.BytesIO(_build_synth_pdf()))
    try:
        pages = pdf.pages
        layer, meta = extract._pdf_text_layer(pages)
        s_text = "\n".join(layer)
        chk("W4a 默认：假水印被删（FAKEWM 消失）", "FAKEWM" not in s_text)
        chk("W4b 默认：正文保留", "quick brown fox" in s_text)
        chk("W4c 默认：大字号标题保留（判据 C 保护）", "Chapter One Introduction" in s_text)
        chk("W4d 默认：小字号页脚保留（判据 B 保护）", "PG0000001" in s_text)
        chk("W4e 默认：局部横幅保留（判据 D 保护）", "LOCALWMBANNER" in s_text)
        chk("W4f 默认：同字体大水印保留（判据 A 保护）", "SAMEFONT-WM" in s_text)

        src = inspect.getsource(extract._pdf_watermark_clusters)
        muts = [
            ("M-A 关闭判据A（异字体）⇒ 同字体大水印被误删",
             "        if f == body_font:", "        if False:  # M-A", "SAMEFONT-WM"),
            ("M-B 关闭判据B（字号倍率）⇒ 小字号页脚被误删",
             "        if s < body_size * ratio:", "        if False:  # M-B", "PG0000001"),
            ("M-C 关闭判据C（高重复）⇒ 大字号标题被误删",
             "        if len(cc) / total > max_distinct:", "        if False:  # M-C",
             "Chapter One Introduction"),
            ("M-D 关闭判据D（跨页）⇒ 局部横幅被误删",
             "        if sum(1 for pc in per_page if pc.get(key)) < need_pages:",
             "        if False:  # M-D", "LOCALWMBANNER"),
        ]
        for name, anchor, repl, probe in muts:
            mut_src = src.replace(anchor, repl)
            chk(name + "（锚点命中）", mut_src != src)
            if mut_src == src:
                continue
            fn = _twin_text_layer(mut_src)
            m_text = "\n".join(fn(pages)[0])
            chk(name, probe not in m_text,
                f"probe={probe!r} 已被误删 ⇒ 对应断言抓住（判据有牙）")
            chk(name + " · 假水印仍被删（变异只放走靶子，不过滤整体失效）", "FAKEWM" not in m_text)
            chk(name + " · 正文仍保留", "quick brown fox" in m_text)
    finally:
        pdf.close()

    print("\n" + "-" * 56)
    print(f"通过 {len(PASSED)} / {len(PASSED) + len(FAILED)}")
    if FAILED:
        print("失败项：")
        for f in FAILED:
            print("  - " + f)
    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
