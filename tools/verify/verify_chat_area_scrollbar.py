#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_chat_area_scrollbar.py —— 「#chat-area 最外层不展示滚动条」门禁（2026-11-03）。

## 需求原话（米爸）

「AI 正文输出区域，最外层，不要展示这个滚动条，不然滚动条嵌入太多，操作体验很差」

## 现场实测（1600×900，conv 10009）

| 层 | 元素 | 滚动条/占位宽 | scrollHeight / clientHeight |
|---|---|---|---|
| **最外（本次目标）** | `#chat-area` | **30px**（= 两侧各 15px 的 gutter 占位） | 886 / 659 |
| 中间（保留） | `.msg.ai .body.ans-capped` | 8px | 6917 / 538 |
| 最内（保留） | `.md-code` | 15px | 1968 / 323 |

⇒ 「嵌入太多」的实体是那 30px，来源不是滚动条本身，而是
`app.css` 里的 `#chat-area{scrollbar-gutter:stable both-edges;}` ——
它当初是为了修「Windows 滚动条 15px 只吃右侧 ⇒ 消息列与输入区中心线差 6.5px」，
用左右对称占位把中心线拉回来。滚动条不再展示后它不再占位，这 30px 就成了纯空白。

## 本门禁守什么（三条，缺一不可）

A CSS 契约
  A1 `#chat-area` 隐藏滚动条（`scrollbar-width:none` + `::-webkit-scrollbar{width:0}`）
  A2 `scrollbar-gutter` 必须是 `auto`（不能是 `stable both-edges`）—— 两者是同一件事的两面
  A3 **滚动能力不得被取消**（不能出现 `overflow-y:hidden` 之类）

B 静态自证（变异）
  B1 把 gutter 改回 `stable both-edges` ⇒ A2 必须变红
  B2 删掉 `::-webkit-scrollbar` 规则 ⇒ A1 必须变红

C 运行时证据（可选，需服务在跑；无真实浏览器时跳过而不假绿）
  C1 `offsetWidth - clientWidth == 0`（占位消失），旧值 30
  C2 滚动仍生效（`scrollTop` 能精确到位）

⚠️ 不守的东西（明确边界）：正文内部 8px / 代码块 15px 的滚动条是**有意保留**的 ——
   米爸说的是「最外层」。若哪天也要去掉，改本文件并补对应断言，别默默改宽。
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

_R = []


def ck(name, ok, detail=""):
    _R.append((name, bool(ok), detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"  | {detail}" if detail else ""))


def _css_checks(src: str) -> dict:
    """纯函数：从 CSS 源码算出四个判决（便于变异自证复用）。"""
    # 取所有 #chat-area{...} 声明块的并集（可能被拆成多块）
    blocks = re.findall(r"#chat-area\s*\{([^}]*)\}", src)
    joined = "\n".join(blocks)
    return {
        "has_hide_ff": bool(re.search(r"scrollbar-width\s*:\s*none", joined)),
        "has_hide_wk": bool(re.search(
            r"#chat-area::-webkit-scrollbar\s*\{[^}]*width\s*:\s*0", src)),
        "gutter_auto": bool(re.search(r"scrollbar-gutter\s*:\s*auto", joined)),
        "gutter_both": bool(re.search(r"scrollbar-gutter\s*:\s*stable\s+both-edges", joined)),
        # 滚动能力：只允许内联/外部给 auto|scroll，这里只查"有没有被 CSS 关掉"
        "overflow_hidden": bool(re.search(r"overflow(-y)?\s*:\s*hidden", joined)),
    }


def main() -> int:
    print("=" * 92)
    print("「#chat-area 最外层不展示滚动条」门禁")
    print("=" * 92)

    p = ROOT / "static" / "css" / "app.css"
    src = p.read_text(encoding="utf-8")
    v = _css_checks(src)

    # ── A CSS 契约 ──
    ck("A1a Firefox：scrollbar-width:none", v["has_hide_ff"])
    ck("A1b Chromium/Safari：#chat-area::-webkit-scrollbar{width:0}", v["has_hide_wk"])
    ck("A2 scrollbar-gutter 为 auto（两侧 15px 对称占位已去掉）", v["gutter_auto"])
    ck("A2b 不得残留 stable both-edges（否则白留 30px）", not v["gutter_both"])
    ck("A3 滚动能力未被取消（CSS 里没有 overflow:hidden 关掉它）", not v["overflow_hidden"])

    # 内联 overflow-y:auto 仍在（滚动能力的来源）
    idx = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    m = re.search(r'<div[^>]*id="chat-area"[^>]*>', idx)
    inline = m.group(0) if m else ""
    ck("A4 index.html 内联样式仍保留 overflow-y:auto（滚动能力真源）",
       "overflow-y:auto" in inline.replace(" ", ""))

    # ── B 静态变异自证 ──
    mut1 = src.replace("scrollbar-gutter:auto;", "scrollbar-gutter:stable both-edges;", 1)
    v1 = _css_checks(mut1)
    ck("B1 变异：gutter 改回 both-edges ⇒ A2 必红",
       mut1 != src and v1["gutter_both"] and not v1["gutter_auto"])
    mut2 = re.sub(r"#chat-area::-webkit-scrollbar\s*\{[^}]*\}", "", src, count=1)
    v2 = _css_checks(mut2)
    ck("B2 变异：删掉 ::-webkit-scrollbar 规则 ⇒ A1b 必红",
       mut2 != src and not v2["has_hide_wk"])

    # ── C 运行时证据（可选）──
    import json
    ev = ROOT / "outputs" / "_verify_scroll_after.json"
    evb = ROOT / "outputs" / "_ab_center_before.json"
    eva = ROOT / "outputs" / "_ab_center_after.json"
    if ev.exists() and eva.exists():
        after = json.loads(eva.read_text(encoding="utf-8"))[0]["data"]
        ck("C1 运行时：滚动条占位 = 0px（旧值 30px）",
           after.get("gutterPx") == 0, f"gutterPx={after.get('gutterPx')}")
        ck("C2 运行时：滚动仍生效（scrollTop 精确到位）",
           bool(after.get("scrollWorks")),
           f"{after.get('scrollBefore')}→{after.get('scrollTarget')} 实测 {after.get('scrollAfter')}")
        if evb.exists():
            before = json.loads(evb.read_text(encoding="utf-8"))[0]["data"]
            ck("C3 A/B 对照：旧值确实是 30px（证明改的是真靶子）",
               before.get("gutterPx") == 30, f"before={before.get('gutterPx')}")
            ck("C4 A/B 对照：消息列位置像素级不变（无布局位移回归）",
               before.get("msgLeft") == after.get("msgLeft")
               and before.get("msgCenter") == after.get("msgCenter"),
               f"before={before.get('msgLeft')}/{before.get('msgCenter')} "
               f"after={after.get('msgLeft')}/{after.get('msgCenter')}")
            ck("C5 A/B 对照：centerDelta 未被本次改动放大（既有值不变）",
               before.get("centerDelta") == after.get("centerDelta"),
               f"before={before.get('centerDelta')} after={after.get('centerDelta')}")
    else:
        print("  [SKIP] 无运行时证据（未跑真实浏览器），A/B 项不做假绿")

    fails = [r for r in _R if not r[1]]
    print("=" * 92)
    print(f"断言汇总：{len(_R) - len(fails)}/{len(_R)} 通过")
    for nm, _, dt in fails:
        print("  ✗ " + nm + (f"  | {dt}" if dt else ""))
    print("✅ ALL PASS" if not fails else "❌ 有红项")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
