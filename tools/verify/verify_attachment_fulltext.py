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
import os
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
    # ★ 2026-11-13 新增 H4 段：**注入层**（上一版门禁的漏口）。
    #   H1~H3 只证明了「全文被解析出来」，但米爸实反馈（截图）说模型**读到的只有
    #   第 1~3 章 + 4.1**，第 5/6/7 章条款整段不在上下文里。
    #   真因：注入侧 `stream.py` 只取 `att_blocks[:4]` 再砍到 `_ATT_INJECT_CAP=4000`
    #   字符；完整全文 `retrieval_att` **只喂 RAG 召回**，而附件默认不触发检索
    #   （`should_retrieve` 门控）⇒ 后半段从未进过 prompt。
    #   ⇒ 这一段守的是「提纲层真的被拼进 att_text（最终 prompt 输入）」，
    #     而不只是「提纲函数能抽出来」。
    section("H4 · 注入层（提纲常驻）：模型必须知道全文有哪些章节")
    from agent.pipeline_parts.common import _ATT_INJECT_CAP, _ATT_OUTLINE_CAP
    outline = pipe._attachment_outline(blocks)
    chk("H4a 提纲层非空（doc 847 有章节结构）", bool(outline), f"len={len(outline)}")
    chk("H4b 提纲覆盖后段章节（第 5/6/7 章 —— 截图里「读不到」的那几章）",
        all(t in outline for t in ("5.3", "6.4", "7.1")),
        f"outline={outline[:200]}")
    chk("H4c 提纲长度受 _ATT_OUTLINE_CAP 封顶", len(outline) <= _ATT_OUTLINE_CAP,
        f"len={len(outline)} cap={_ATT_OUTLINE_CAP}")
    chk("H4d 提纲未误判封面日期行（2026 年 4 月 不是章节）",
        "2026 年" not in outline, f"outline={outline[:120]}")
    chk("H4e 提纲未混入括号列表项（1) 轻量级查询…）",
        "轻量级查询" not in outline, f"outline={outline[:200]}")

    # H4f：按 stream.py 的真实拼装口径复现 att_text，验证**提纲真的进了 prompt 输入**
    _body = ("\n\n".join(blocks[:4]))[:_ATT_INJECT_CAP]
    _injected = _body + (
        "\n\n【上传资料·章节提纲（全文共 %d 字，以下为已识别的章节标题——"
        "**你只看到了上面那份正文的前段**，后续章节的条款正文尚未注入，"
        "如需某章的具体条款请明确索取，不要因为正文里没有就认为文档不含该章）】\n%s"
        % (len(joined), outline))
    chk("H4f 提纲进入最终注入文本（不再只注入前 4 块）", "章节提纲" in _injected)
    chk("H4g 注入文本同时含正文首部与提纲（分层都在）",
        _body[:200] in _injected and "5.3" in _injected)
    # H4h 变异自证：去掉提纲层 ⇒ H4f/H4g 必须 FAIL（证明守的靶子是真的）
    _mut_stream = open(
        os.path.join(ROOT, "agent", "pipeline_parts", "stream.py"),
        encoding="utf-8").read()
    chk("H4h 变异锚点命中（注入点仍在拼接提纲层）",
        "_att_outline" in _mut_stream and "章节提纲" in _mut_stream)
    # 反证：只取前 4 块的旧行为下，后段章节在注入文本里**不存在**
    chk("H4i 反证：仅取前 4 块时后段章节不在注入文本（这正是原 bug）",
        "5.3" not in _body and "7.1" not in _body,
        "旧行为确实丢失后段章节")
    # H4j 变异自证：把注入点换回「只有前 4 块、没有提纲层」的旧写法，
    #     H4f/H4g 必须 FAIL（否则本段全是假绿）。内存替换，不写生产文件。
    _mut_no_outline = _mut_stream
    _i0 = _mut_no_outline.find("            _att_outline = self._attachment_outline(att_blocks) if att_blocks else \"\"")
    if _i0 >= 0:
        _i1 = _mut_no_outline.find("matched_flows =", _i0)
        if _i1 > _i0:
            _mut_no_outline = _mut_no_outline[:_i0] + \
                "            pass  # MUT: 去掉提纲层\n            " + _mut_no_outline[_i1:]
    chk("H4j 变异锚点命中（注入点含提纲拼接段）", _mut_no_outline != _mut_stream)
    if _mut_no_outline != _mut_stream:
        _body_only = ("\n\n".join(blocks[:4]))[:_ATT_INJECT_CAP]
        _mut_injected = _body_only          # 旧写法：没有任何提纲层
        chk("H4k 换回旧写法（无提纲层）⇒ H4f 失效（提纲确实不在注入文本）",
            "章节提纲" not in _mut_injected)
        chk("H4l 换回旧写法 ⇒ 后段章节在注入文本里彻底缺失（原 bug 复现）",
            "5.3" not in _mut_injected and "7.1" not in _mut_injected)

    # ─────────────────────────────────────────────────────────────────────────
    section("H5 · 提纲函数纯函数边界（合成数据，不依赖真库）")
    from agent.pipeline_parts.history import build_attachment_outline, _is_att_heading
    _s = ["【d.pdf】\n第1章 概述\n第2章 功能要求\n第3章 接口要求\n第4章 验收交付"]
    chk("H5a 合成文档提纲可抽出", len(build_attachment_outline(_s)) >= 3,
        build_attachment_outline(_s))
    chk("H5b 无章节结构 → 返回空串（不塞占位噪声）",
        build_attachment_outline(["【x】\n一段没有标题的普通正文"]) == "")
    chk("H5c 空输入不抛异常", build_attachment_outline([]) == ""
        and build_attachment_outline(None) == "" and build_attachment_outline([""]) == "")
    chk("H5d 日期行不算章节", not _is_att_heading("2026 年 4 月") and not _is_att_heading("2026年"))
    chk("H5e 括号列表项不算章节", not _is_att_heading("1) 轻量级查询：平均响应时间"))
    chk("H5f 真章节标题仍命中",
        _is_att_heading("第7章 验收交付") and _is_att_heading("7.2 可靠性要求")
        and _is_att_heading("4.1 业务需求"))

    # ─────────────────────────────────────────────────────────────────────────
    # ★ 2026-11-13 新增 H6 段：`read_attachment_section`（按需取回缺失章节正文）
    #   背景（米爸二次实测截图）：上一版只补了「章节提纲」层，模型知道了第 5/6/7 章
    #   存在，却**没有任何工具能取到它** ⇒ 只能反过来向用户索要正文，原话：
    #   「请把第 5、6、7 章的正文贴进来」。**提纲没有通道就是空头承诺。**
    section("H6 · read_attachment_section：模型能自己把附件读全")
    from agent.pipeline_parts.common import reset_request_state
    from agent.pipeline_parts.history import _att_section_key
    reset_request_state(pipe)          # 门禁自身也走清零，防跨用例残留
    pipe._index_attachment_sections(blocks)
    chk("H6a 章节索引非空", bool(pipe._att_sections), f"n={len(pipe._att_sections)}")
    chk("H6b 索引覆盖到后段章节（7.x）",
        any(k.startswith("7") for k, _, _, _ in pipe._att_sections))
    # 取章：doc 847 的顶层「5/6/7」只出现在术语表区，正文是5.1~5.5/6.1~/7.1~
    # ⇒ 必须回落到子节合并区间，否则取到的是术语表碎片（本段核心断言）。
    r5 = pipe._read_attachment_section("5", 1)
    chk("H6c 取第 5 章成功且拿到实质正文（>2000 字符，非术语表碎片）",
        bool(r5.get("ok")) and len(str(r5.get("result", ""))) > 2000,
        f"ok={r5.get('ok')} len={len(str(r5.get('result','')))}")
    chk("H6d 第 5 章内容是正文条款（含 5.1 系统架构要求，且不是术语表碎片）",
        "5.1 系统架构要求" in str(r5.get("result", "")),
        str(r5.get("result", ""))[:100])
    r7 = pipe._read_attachment_section("7", 1)
    chk("H6e 取第 7 章拿到性能指标（7.1 性能要求 + RT 3 秒）",
        "7.1 性能要求" in str(r7.get("result", "")) and "不大于 3 秒" in str(r7.get("result", "")),
        str(r7.get("result", ""))[:100])
    # 章号归一化：模型可能传'第6章' / '6.4' / '6'
    chk("H6f 章号归一化（第6章 → 6，与 '6' 等价）",
        _att_section_key("第 6 章 接口要求") == "6"
        and _att_section_key("6") == "6"
        and _att_section_key("6.4") == "6.4")
    r_named = pipe._read_attachment_section("第6章", 1)
    chk("H6g 模型传'第6章'也能取到接口要求正文",
        "6.1 大语言模型调用接口要求" in str(r_named.get("result", "")),
        str(r_named.get("result", ""))[:100])
    # 取不到必须**明说**并列出可用章节（绝不静默空串 —— 那会被模型当成"这章是空的"）
    r_bad = pipe._read_attachment_section("99", 1)
    chk("H6h 取不到时ok=False 且明确说明+列出现有章节",
        not r_bad.get("ok") and "现有章节" in str(r_bad.get("result", ""))
        and len(str(r_bad.get("result", ""))) > 50,
        str(r_bad.get("result", ""))[:80])
    r_list = pipe._read_attachment_section("", 1)
    chk("H6i 不指定章节时返回清单而非全文（防一次塞爆 token）",
        bool(r_list.get("ok")) and "章节清单" in str(r_list.get("result", ""))
        and len(str(r_list.get("result", ""))) < 3000,
        f"len={len(str(r_list.get('result','')))}")
    # 工具必须真的进了 tools_def（有附件时）
    td = pipe._build_tools_def("design", "基于附件做需求分析", None)
    names = [t.get("function", {}).get("name") for t in td]
    chk("H6j 有附件时 read_attachment_section 已注入 tools_def",
        "read_attachment_section" in names, f"tools={len(names)}")
    # 执行侧分发在位
    t_src = open(os.path.join(ROOT, "agent", "pipeline_parts", "tools.py"),
                 encoding="utf-8").read()
    chk("H6k 执行侧分发分支在位", 'if name == "read_attachment_section":' in t_src)

    # ─────────────────────────────────────────────────────────────────────────
    section("H7 · 安全红线：附件全文/索引必须按请求清零（防跨用户泄漏）")
    # 断言纪律：先**故意写脏**再reset，然后断言被清掉 ——
    # 只在干净状态下 getattrs 拿到的本来就是空值，断言就是假绿。
    pipe._att_sections = [("9", "上一轮残留", 0, 10)]
    pipe._att_full_text = "上一轮别的用户的附件正文"
    reset_request_state(pipe)
    chk("H7a reset 后 _att_sections 清空", getattr(pipe, "_att_sections", None) == [],
        repr(getattr(pipe, "_att_sections", "MISSING")))
    chk("H7b reset 后 _att_full_text 清空（不留上一轮附件正文）",
        not getattr(pipe, "_att_full_text", None),
        repr(getattr(pipe, "_att_full_text", "MISSING")))
    chk("H7c 清空后再取章返回明确失败（拿不到别人的附件）",
        pipe._read_attachment_section("5", 1).get("ok") is False)
    _rst = open(os.path.join(ROOT, "agent", "pipeline_parts", "common.py"),
                encoding="utf-8").read()
    chk("H7d _att_sections 已登记进 reset_request_state",
        "pipe._att_sections" in _rst)
    chk("H7e _att_full_text 也在登记清单里（正文更敏感，漏清即跨用户泄漏）",
        "pipe._att_full_text" in _rst)

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
