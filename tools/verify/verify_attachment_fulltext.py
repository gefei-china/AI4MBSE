# -*- coding: utf-8 -*-
"""附件全文消费门禁（2026-11-12；米爸反馈1「上传附件无法正常解析识别消费」+ 反馈2
「从产物文档选文件发会话，只能识别封面和章节，具体内容无法识别到」）。

背景（真库取证）
----
doc 847 的用户原话（澄清卡）：「附件《…项目技术要求》的正文（第4~8章具体条款与
指标）在当前上下文中只解析出目录级标题」。代码级两条根因：
  ① `history.py` doc_id 路径整篇截断 `_txt[:2500]` ⇒ att_text（前 4 块进
     prompt）与 retrieval_att（全文召回候选池）**都只剩文档头部**（封面+目录），
     第 4~8 章条款两条通道都拿不到；
  ② /static/ 路径的 PDF 分支**内联 pdfplumber 裸抽** ⇒ 绕过 extract 的水印过滤
     （星网受控 PDF 每页 432 个 STSong-Light 水印字符照样进附件文本），且与
     知识库入库口径不一致（同一文件「库里一份、附件里另一份」）。

修复
----
  ① doc_id 路径改 2500 字/块、≤40 块、块头带序号（【文件名#2】）——与 /static/
     路径同口径；`_load_doc_chunks` 补章节上下文（section 列：条款号只在里面）；
  ② PDF 分支改走 `knowledge_pipeline.extract.pdf_text_layer`（水印过滤后仍只抽
     文本层，不触发 OCR——聊天轮付不起 1.4~4.7s/页）。

判据（H = 本文件断言）
----
H1 doc_id 路径（doc 847 真库）：分段块 + 头/中/后段条款全覆盖 + 章节上下文
   + 0 水印残留；
H2 /static/ PDF 路径（合成水印 PDF）：走过滤抽取，正文在、水印网格不在；
H3 变异自证（函数级内存孪生，不写生产文件）：
   M1 分段换回整篇截断 ⇒ H1 红（后段条款丢失）；
   M2 换回内联裸抽 ⇒ H2 红（水印残留）。
"""
import inspect
import io
import sys
import tempfile
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

PASSED, FAILED = [], []


def chk(name, ok, detail=""):
    (PASSED if ok else FAILED).append(name)
    print(("  [PASS] " if ok else "  [FAIL] ") + name + (("  -- " + str(detail)) if detail else ""))


def section(t):
    print("\n" + "=" * 4 + " " + t + " " + "=" * 4)


def _build_wm_pdf() -> bytes:
    """合成水印 PDF（reportlab，venv 已有）：3 页，每页 Helvetica 11pt 正文
    （6 行长段落，使正文簇字符数压过水印簇——否则 `_pdf_watermark_clusters`
    的 most_common(1) 会把水印错认成主体签名、判据 A 反过来保护水印）
    + STSongStd-Light 24pt 高重复假水印「FAKEWM8260」。"""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    _W, H = A4
    for pno in range(3):
        c.setFont("Helvetica", 11)
        for li in range(6):
            line = ("Requirement clause body text page%d line%d for attachment probe. " % (pno, li)) * 2
            c.drawString(50, H - 60 - li * 18, line)
        c.setFont("STSong-Light", 24)
        for i in range(8):
            c.drawString(60, 200 + i * 30, "FAKEWM8260" * 3)
        c.showPage()
    c.save()
    return buf.getvalue()


def _twin_load_att(mut_src: str, static_dir):
    """函数级内存孪生：变异 `_load_attachment_text` 源码后编译进**模块全局副本**
    （STATIC_DIR 指向临时目录），返回驱动函数。不写生产文件。

    注意 dedent：`inspect.getsource` 取到的是**类方法**源码（def 行带 4 空格
    类级缩进），直接 exec 会 IndentationError——先 textwrap.dedent 再编译。
    """
    import agent.pipeline_parts.history as H
    g = dict(vars(H))
    g["STATIC_DIR"] = static_dir
    exec(compile(textwrap.dedent(mut_src), "<att_load_mut>", "exec"), g)
    return g["_load_attachment_text"]


def main() -> int:
    from agent.pipeline import AgentPipeline
    pipe = AgentPipeline()
    DOC847_ATT = {
        "doc_id": 847,
        "filename": "Print_面向网络总体的AI赋能MBSE系统设计技术研究项目技术要求.pdf",
        "url": "/api/documents/847/download",
    }

    # ─────────────────────────────────────────────────────────────────────────
    section("H1 · doc_id 路径（doc 847 真库）：全文覆盖 + 章节上下文 + 零水印残留")
    blocks, parsed, skipped = pipe._load_attachment_text([dict(DOC847_ATT)])
    joined = "\n".join(blocks)
    chk("H1a 解析成功（parsed=1、skipped 空）", parsed == 1 and not skipped,
        f"parsed={parsed} skipped={skipped}")
    chk("H1b 分段注入（≥5 块，而非整篇 1 块）", len(blocks) >= 5, f"blocks={len(blocks)}")
    chk("H1c 块头带序号（【文件】/【文件#2】/【文件#3】）",
        blocks[0].startswith("【Print_面向网络总体的AI赋能MBSE系统设计技术研究项目技术要求.pdf】")
        and blocks[1].startswith("【Print_面向网络总体的AI赋能MBSE系统设计技术研究项目技术要求.pdf#2】")
        and blocks[2].startswith("【Print_面向网络总体的AI赋能MBSE系统设计技术研究项目技术要求.pdf#3】"),
        blocks[1].split("\n", 1)[0] if len(blocks) > 1 else "-")
    chk("H1d 头部条款覆盖（★[FR-KG-1] 设计数据管理）",
        "★[FR-KG-1]应支持从结构化数据库" in joined)
    chk("H1e 中段条款覆盖（★[FR-MQ-1] 模型问答）", "★[FR-MQ-1]应支持基于SysML" in joined)
    chk("H1f 后段条款覆盖（11.4 扩展性要求）", "11.4 扩展性要求" in joined)
    chk("H1g 章节上下文在文本里（5.3.3 模型问答）", "5.3.3 模型问答" in joined)
    chk("H1h 水印网格残留=0（#50 修复后 chunk 干净）", joined.count("- - -") == 0)
    chk("H1i 总覆盖规模 ≥ 10000 字符（旧实现仅 2542）", len(joined) >= 10000, f"len={len(joined)}")

    # ─────────────────────────────────────────────────────────────────────────
    section("H2 · /static/ PDF 路径（合成水印 PDF）：走过滤抽取")
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        (tdp / "wm.pdf").write_bytes(_build_wm_pdf())
        import agent.pipeline_parts.history as H
        orig_static = H.STATIC_DIR
        H.STATIC_DIR = str(tdp)
        try:
            b2, p2, s2 = pipe._load_attachment_text(
                [{"url": "/static/wm.pdf", "filename": "wm.pdf"}])
        finally:
            H.STATIC_DIR = orig_static
        j2 = "\n".join(b2)
        chk("H2a PDF 附件解析成功", p2 == 1 and not s2 and bool(j2), f"skipped={s2}")
        chk("H2b 正文句保留", "Requirement clause body text" in j2)
        chk("H2c 水印网格被滤掉（FAKEWM 不在文本中）", "FAKEWM" not in j2)
        # 对照：关掉 extract 的水印过滤 ⇒ 同一文件应漏出水印（证明靶子有效、
        #       H2c 不是"反正什么都抽不到"的假绿）
        from core import config as _cfg
        _orig_on = _cfg._CONFIG["extract"].get("watermark_filter_enabled")
        _cfg._CONFIG["extract"]["watermark_filter_enabled"] = False
        try:
            H.STATIC_DIR = str(tdp)
            b2r, _p2r, _s2r = pipe._load_attachment_text(
                [{"url": "/static/wm.pdf", "filename": "wm.pdf"}])
        finally:
            H.STATIC_DIR = orig_static
            _cfg._CONFIG["extract"]["watermark_filter_enabled"] = _orig_on
        chk("H2d 对照：关过滤时水印确实存在（证明 H2c 有牙）",
            "FAKEWM" in "\n".join(b2r))

    # ─────────────────────────────────────────────────────────────────────────
    section("H3 · 变异自证（函数级内存孪生）：两种旧写法各被抓一次")
    src = inspect.getsource(pipe._load_attachment_text)

    # M1：doc_id 路径换回「整篇截断 2500」
    m1_anchor = ("                    for i, seg in enumerate(_txt[j:j + 2500] "
                 "for j in range(0, len(_txt), 2500)):")
    m1 = src.replace(m1_anchor, "                    for i, seg in enumerate([_txt[:2500]]):")
    chk("M1a 变异锚点命中（分段循环行）", m1 != src)
    if m1 != src:
        fn1 = _twin_load_att(m1, orig_static)
        b1, p1, s1 = fn1(pipe, [dict(DOC847_ATT)])
        j1 = "\n".join(b1)
        chk("M1b 换回整篇截断 ⇒ H1f 抓住（后段条款 11.4 丢失）", "11.4 扩展性要求" not in j1,
            f"blocks={len(b1)} len={len(j1)}")
        chk("M1c 换回整篇截断 ⇒ H1b 抓住（只剩 1 块）", len(b1) == 1, f"blocks={len(b1)}")

    # M2：PDF 分支换回内联 pdfplumber 裸抽
    m2_anchor = ('                    from knowledge_pipeline.extract import pdf_text_layer as _pTL\n'
                 '                    text = _pTL(open(path, "rb").read(), max_pages=20)')
    m2 = src.replace(m2_anchor,
                     '                    import pdfplumber\n'
                     '                    with pdfplumber.open(path) as pdf:\n'
                     '                        text = "\\n".join((p.extract_text() or "") for p in pdf.pages[:20])')
    chk("M2a 变异锚点命中（PDF 分支过滤抽取行）", m2 != src)
    if m2 != src:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            (tdp / "wm.pdf").write_bytes(_build_wm_pdf())
            fn2 = _twin_load_att(m2, str(tdp))
            b3, p3, s3 = fn2(pipe, [{"url": "/static/wm.pdf", "filename": "wm.pdf"}])
            j3 = "\n".join(b3)
            chk("M2b 换回内联裸抽 ⇒ H2c 抓住（FAKEWM 残留）", "FAKEWM" in j3,
                f"len={len(j3)}")
            chk("M2c 裸抽仍能读到正文（变异只放过滤、不破坏解析）",
                "Requirement clause body text" in j3)

    print("\n" + "-" * 56)
    print(f"通过 {len(PASSED)} / {len(PASSED) + len(FAILED)}")
    if FAILED:
        print("失败项：")
        for f in FAILED:
            print("  - " + f)
    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
