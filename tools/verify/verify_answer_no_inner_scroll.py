#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_answer_no_inner_scroll.py —— 「AI 正式内容输出不得限高、不得内滚」门禁（2026-11-03）

## 需求原话（米爸）

「我说的是每一段 AI 正式内容输出不用滚动条（不用限制在固定的高度内）」

## 为什么单独一条门禁

2026-10-29 曾给正文加 `max-height:60vh; overflow-y:auto` + 一行「在本区域内滚动可查看全部」
提示条，理由是把「页面被拉得很长」当成缺陷、并把 2026-10-27 对**执行过程区**的要求
（「限制高度，可以滑动查看完整内容」）错误地套到了**正文**上。

实测（conv 9779，1600×900）这一天在页面里造成了 **15 条纵向内滚**，其中：
  · 正文 `.msg.ai .body` 8 条 —— 被锁在 540px（60vh），而内容最长 5385px
  · 代码块 `.code-fold .md-code` 4 条 —— 被锁在 340px，内容 3127px
  · 整合卡代码面板 `.svm-code` 4 条 —— 被锁在 440px，内容 4959px

⇒ 米爸看到的不是"一条滚动条"，而是**每一段内容各自一条**，所以"嵌入太多"。

## 本门禁守什么

A 正文与代码块**不得**限高/内滚
  A1 `.msg.ai .body` 任何规则都不得出现 `max-height`
  A2 不得存在 `.ans-capped` 选择器（旧限高 class）
  A3 不得存在 `.ans-cap-hint` 选择器（旧提示条）
  A4 `.code-fold .md-code` 不得有 `max-height`（chat.css 与 app.css 两处都要干净）
  A5 `.svm-code` 不得有 `max-height`
  A6 `.md-code` 必须保留 `overflow-x`（长代码行不换行，横向滚动是必需项）

B 反向断言：**范围不许扩大**（放开的是"正式内容"，不是"过程"与"画布"）
  B1 `.proc` 仍 `max-height:44vh`（执行过程区；米爸 2026-10-27 明确要求限高）
  B2 `.svm-canvas` 仍限高（图形视口，不是文字被截断）

C JS 侧不得再施加高度干预
  C1 `51-answer-view.js` 不含 `ans-capped` / `ans-cap-hint` / `CAP_PX`
  C2 `applyAnswerFrame` 仍导出（03-chat.js 与 13-reports.js 依赖该接口）
  C3 `05-markdown.js` 注释不得再声称"高度受 340px 约束"（注释与代码必须同向）

D 变异自证（证明断言是承重的，不是"怎么写都绿"）
  D1 把 `max-height:60vh` 加回 `.msg.ai .body` ⇒ A1 必红
  D2 把 `.ans-capped{...}` 加回 ⇒ A2 必红
  D3 把 `max-height:340px` 加回 `.code-fold .md-code` ⇒ A4 必红

E 运行时证据（可选，需 outputs/_no_inner_scroll_*.json；缺失则跳过而不假绿）
  E1 正文纵向内滚数 = 0
  E2 代码块纵向内滚数 = 0
  E3 全页纵向内滚元素总数 = 0（只剩 #chat-area 一层）
  E4 A/B：修复前确有 8 个正文内滚（证明改的是真靶子，不是空改）
  E5 内容完整性：数据库那条消息的尾部在页面文本中可见（放宽空白比较）
"""
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
_R = []


def ck(name, ok, detail=""):
    _R.append((name, bool(ok), detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"  | {detail}" if detail else ""))


def _has_maxh(css: str, selector_pat: str) -> bool:
    """该选择器命中的任何声明块里是否出现 max-height（含 max-height:none）。"""
    for m in re.finditer(selector_pat + r"\s*\{([^}]*)\}", css):
        if re.search(r"max-height", m.group(1), re.I):
            return True
    return False


def _judge(chat_css: str, app_css: str) -> dict:
    return {
        # A 正文与代码块不得限高
        "body_maxh": _has_maxh(chat_css, r"\.msg\.ai\s+\.body[^{,]*"),
        "capped_cls": bool(re.search(r"\.ans-capped", chat_css)),
        "hint_cls": bool(re.search(r"\.ans-cap-hint", chat_css)),
        "mdc_chat": _has_maxh(chat_css, r"\.code-fold\s+\.md-code"),
        "mdc_app": _has_maxh(app_css, r"\.code-fold\s+\.md-code"),
        "svmcode": _has_maxh(app_css, r"\.svm-code(?!\s*copy)"),
        # A6 横向滚动必须保留
        "mdx_keep": bool(re.search(r"\.md-code\s*\{[^}]*overflow-x\s*:\s*auto", app_css)),
        # B 反向：过程区与画布仍限高
        "proc_capped": bool(re.search(r"\.proc\s*\{[^}]*max-height\s*:\s*44vh", app_css))
                        or bool(re.search(r"\.proc\s*\{[^}]*max-height\s*:\s*44vh", chat_css)),
        "canvas_capped": bool(re.search(
            r"\.sv-card\s+\.svm-canvas\s*\{[^}]*max-height", app_css)),
    }


def main() -> int:
    print("=" * 96)
    print("「AI 正式内容输出不限高、不内滚」门禁")
    print("=" * 96)

    chat = (ROOT / "static" / "css" / "chat.css").read_text(encoding="utf-8")
    app = (ROOT / "static" / "css" / "app.css").read_text(encoding="utf-8")
    ansjs = (ROOT / "static" / "js" / "mods" / "51-answer-view.js").read_text(encoding="utf-8")
    mdjs = (ROOT / "static" / "js" / "mods" / "05-markdown.js").read_text(encoding="utf-8")

    v = _judge(chat, app)

    # ── A 正文与代码块不得限高 ──
    ck("A1 正文 .msg.ai .body 无限高", not v["body_maxh"],
       "发现 max-height ⇒ 正文会被压成内滚")
    ck("A2 无 .ans-capped 选择器（旧限高 class 已清除）", not v["capped_cls"])
    ck("A3 无 .ans-cap-hint 选择器（旧提示条已清除）", not v["hint_cls"])
    ck("A4a chat.css 中 .code-fold .md-code 无限高", not v["mdc_chat"])
    ck("A4b app.css 中 .code-fold .md-code 无限高（后加载，会覆盖 chat.css）", not v["mdc_app"])
    ck("A5 .svm-code（整合卡代码面板）无限高", not v["svmcode"])
    ck("A6 .md-code 仍保留 overflow-x（长代码行横向滚动不可丢）", v["mdx_keep"])

    # ── B 反向：范围不许扩大 ──
    ck("B1 执行过程区 .proc 仍限高 44vh（米爸 2026-10-27 的要求）", v["proc_capped"])
    ck("B2 图形视口 .svm-canvas 仍限高（那是画布，不是文字截断）", v["canvas_capped"])

    # ── C JS 侧 ──
    ck("C1a 51-answer-view.js 不再加 ans-capped", "ans-capped" not in ansjs)
    ck("C1b 51-answer-view.js 不再插 ans-cap-hint", "ans-cap-hint" not in ansjs)
    ck("C1c 51-answer-view.js 不再有 CAP_PX 阈值", "CAP_PX" not in ansjs)
    ck("C2 applyAnswerFrame 接口仍在（调用方依赖）",
       "window.applyAnswerFrame" in ansjs)
    # ⚠️ 判「是否仍**声称**受约束」，不判「是否出现 340px 字样」——
    #    修好后的注释里会引用旧值作历史说明（这恰恰是要保留的），机械查数字会假红。
    ck("C3 05-markdown.js 注释不再声称代码块受 340px 约束",
       "高度仍受" not in mdjs)

    # ── D 变异自证 ──
    m1 = chat.replace(
        "/* 来源标注", ".msg.ai .body{max-height:60vh;overflow-y:auto;}\n/* 来源标注", 1)
    ck("D1 变异：把 max-height:60vh 加回正文 ⇒ A1 必红",
       m1 != chat and _judge(m1, app)["body_maxh"])
    m2 = chat.replace(
        "/* 来源标注", ".ans-capped{max-height:60vh;}\n/* 来源标注", 1)
    ck("D2 变异：把 .ans-capped 加回 ⇒ A2 必红",
       m2 != chat and _judge(m2, app)["capped_cls"])
    m3 = re.sub(r"\.code-fold\s+\.md-code\s*\{[^}]*\}",
                ".code-fold .md-code{max-height:340px;overflow:auto;}",
                app, count=1)
    ck("D3 变异：把 max-height:340px 加回代码块 ⇒ A4b 必红",
       m3 != app and _judge(chat, m3)["mdc_app"])

    # ── E 运行时证据（可选）──
    aft = ROOT / "outputs" / "_no_inner_scroll_final.json"
    bef = ROOT / "outputs" / "_no_inner_scroll_before.json"
    if aft.exists():
        pa = json.loads(aft.read_text(encoding="utf-8"))[0]["probe"]
        if isinstance(pa, str):
            pa = json.loads(pa)
        nb = len([x for x in pa.get("bodies", []) if x.get("vScroll")])
        nm = len([x for x in pa.get("mdCodes", []) if x.get("vScroll")])
        ck("E1 运行时：正文纵向内滚 = 0", nb == 0, f"实测 {nb} 条")
        ck("E2 运行时：代码块纵向内滚 = 0", nm == 0, f"实测 {nm} 条")
        ck("E3 运行时：全页纵向内滚元素 = 0（只剩 #chat-area 一层）",
           len(pa.get("innerScrollers") or []) == 0,
           f"实测 {len(pa.get('innerScrollers') or [])} 个")
        ck("E5 运行时：长正文尾部在页面文本中可见（未截断）",
           pa.get("tailFoundLoose") is True,
           f"最长正文 {pa.get('longestBodyLen')} 字")
        if bef.exists():
            pb = json.loads(bef.read_text(encoding="utf-8"))[0]["probe"]
            if isinstance(pb, str):
                pb = json.loads(pb)
            nbb = len([x for x in pb.get("bodies", []) if x.get("vScroll")])
            ck("E4 A/B 对照：修复前确有正文内滚（证明改的是真靶子）",
               nbb > 0, f"before 内滚正文 {nbb} 条 → after 0 条")
    else:
        print("  [SKIP] 运行时证据缺失（outputs/_no_inner_scroll_final.json）——跳过，不判绿")

    print("-" * 96)
    bad = [r for r in _R if not r[1]]
    print(f"断言汇总：{len(_R) - len(bad)}/{len(_R)} 通过")
    for n, _, d in bad:
        print("  FAIL " + n + (f"  | {d}" if d else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
