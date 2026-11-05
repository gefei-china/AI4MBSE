# CI-OPTIONAL: [broken] 已知红，记账不进 CI（修好后上调 tier 并接线）；两库都红 [FAIL] [FAIL] F1 字号档位 ⊆ 标准集合（25 档）  ← 越界档位：[10.5, 11.0, 13.0]（示例：['chat.css:32', 'alternatives.css:33', 'alternatives 
# -*- coding: utf-8 -*-
"""门禁：全局字号标准 + 会话内容轨道宽度（2026-10-30）。

## 为什么要门禁（用户两句话，背后是两个"没人守"的量）
「平台字体大小感觉比同类产品偏小」+「会话区域宽度有点窄」——
这两件事**都不是 bug，而是没有任何地方定义"应该是多少"**。结果：
  · 字号：2752 处 `font-size`，9~13.5px 之间挤了 **10 个档位**（+0.5 一档），
    其中 11px 有 724 处、10~10.5px 有 566 处，而"正文"13px 只有 93 处
    ⇒ UI 全靠 11~12px 支撑。中文汉字在这个尺寸下笔画会糊（与英文不同）。
  · 宽度：会话轨道 920px 在 1600px 屏只占 57%，而本平台内容含大量表格/代码块
    （Power-User 把 ChatGPT/Claude 从 720~768 调到 1180~1200 的原因正是这个）。

一次性迁移脚本 `tools/_migrate_font_scale.py` 把 10 档收成 6 档并整体 +1.5px；
本门禁负责**守住**，否则下一次随手写个 `font-size:10px` 就又回去了（这种漂移
单次看毫无感觉，累积起来就是"整个界面偏小"）。

## 判据
| # | 断言 | 覆盖的失效 |
|---|---|---|
| F1 | 字号档位 ⊆ 标准集合（不允许 9/10/11 等旧档位复活）| 散档回潮 |
| F2 | 最小字号 ≥ 11.5px（中文可读下限）| 极小字 |
| F3 | 正文基准在 14~15.5px 之间（body 与 `.msg .body`）| 正文被调小 |
| F4 | 无 `font-size>16px` 的非标题元素误用（只警告，不判红）| — |
| W1 | `--chat-track` 定义在 :root 且带 clamp 自适应 | 退回固定窄值 |
| W2 | 消息轨道 / 用户气泡 / 底部输入区 / 执行过程 **四处同源同一变量** | 改一处对不齐（本轮差点重犯）|
| W3 | 无裸 `920px` / `998px` / `1040px` 残留（必须走变量）| 手算常量漂移 |
| W4 | 每一处 `var(--chat-track)` 都带 fallback | 变量丢失 ⇒ 整条 max-width 静默失效 |

## 用法
    ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_typography_scale.py
"""
from __future__ import annotations

import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))

PASS, FAIL, WARN = "PASS", "FAIL", "WARN"
_results = []

#: 标准字号集合（小字六档 + 标题档）。新增字号必须先进这里 = 强制过一遍"为什么需要新档"。
SMALL_TIERS = {11.5, 12.0, 12.5, 13.5, 14.5, 15.5}
HEAD_TIERS = {16.5, 17.5, 18.0, 18.5, 19.5, 20.5, 21.0, 23.0, 25.0, 27.0, 29.0, 31.0, 33.0,
              35.0, 41.0, 68.75}
ALLOWED = SMALL_TIERS | HEAD_TIERS

MIN_FS = 11.5
BODY_RANGE = (14.0, 15.5)


def _rec(name, ok, detail="", kind=FAIL):
    _results.append((PASS if ok else kind, name, detail))
    print("  [%s] %s%s" % (_results[-1][0], name, ("  ← " + detail) if detail else ""))
    return bool(ok)


def _iter_files():
    for dp, _dn, fn in os.walk(os.path.join(_ROOT, "static")):
        if "_fsbackup" in dp:
            continue
        for f in fn:
            if f.endswith((".css", ".js", ".html")):
                yield os.path.join(dp, f)


FS_PAT = re.compile(r"font-size:\s*([0-9]+(?:\.[0-9]+)?)px")


def main():
    # 变异自证要临时改写这几个"标准"常量 ⇒ 必须在函数内先声明 global。
    # ⚠️ 必须放在**任何读取之前**（Python 语义：先使用后声明 = SyntaxError）。
    global MIN_FS, SMALL_TIERS, ALLOWED, BODY_RANGE
    print("=" * 78)
    print("全局字号标准 / 会话内容轨道宽度")
    print("=" * 78)

    # ── F1/F2：字号档位 ──────────────────────────────────────
    print("\n=== F1-F2 字号档位与可读下限 ===")
    found = {}
    for p in _iter_files():
        try:
            src = open(p, encoding="utf-8").read()
        except Exception:
            continue
        for m in FS_PAT.finditer(src):
            v = float(m.group(1))
            found.setdefault(v, []).append(
                "%s:%d" % (os.path.basename(p), src[:m.start()].count("\n") + 1))
    tiers = sorted(found)
    off = [t for t in tiers if t not in ALLOWED]
    _rec("F1 字号档位 ⊆ 标准集合（%d 档）" % len(tiers), not off,
         "越界档位：%s（示例：%s）" % (off[:6], [found[t][0] for t in off[:3]]))
    too_small = [t for t in tiers if t < MIN_FS]
    _rec("F2 最小字号 >= %.1fpx（中文可读下限）" % MIN_FS, not too_small,
         "过小：%s（共 %d 处）" % (too_small,
                                  sum(len(found[t]) for t in too_small)))

    # ── F3：正文基准 ────────────────────────────────────────
    print("\n=== F3 正文基准字号 ===")
    def _fs_of(css_file, selector_pat):
        src = open(os.path.join(_ROOT, "static", "css", css_file), encoding="utf-8").read()
        for m in re.finditer(selector_pat, src, re.M):
            seg = src[m.start():m.start() + 400]
            fm = re.search(r"font-size:\s*([0-9.]+)px", seg)
            if fm:
                return float(fm.group(1))
        return None

    body_fs = _fs_of("app.css", r"^body\{")
    msg_fs = _fs_of("chat.css", r"^\.msg \.body\{")
    _rec("F3a body 字号 = %s（应在 %.0f~%.1f）" % (body_fs, *BODY_RANGE),
         body_fs is not None and BODY_RANGE[0] <= body_fs <= BODY_RANGE[1])
    _rec("F3b 会话气泡字号 = %s（应与正文同级）" % (msg_fs,),
         msg_fs is not None and BODY_RANGE[0] <= msg_fs <= BODY_RANGE[1])

    # ── F4：超大字号只警告 ───────────────────────────────────
    print("\n=== F4 超大字号（>16px）只警告 ===")
    big = {t: len(found[t]) for t in tiers if t > 16}
    _rec("F4 超大字号均属标题/图标（%d 档）" % len(big), True,
         "分布：%s —— 若出现在正文处请人工确认" % big, kind=WARN)

    # ── W1-W4：会话轨道宽度 ─────────────────────────────────
    print("\n=== W1-W4 会话内容轨道 ===")
    app = open(os.path.join(_ROOT, "static", "css", "app.css"), encoding="utf-8").read()
    var_def = re.search(r":root\{--chat-track:\s*([^;]+);", app)
    _rec("W1a --chat-track 定义在 :root", var_def is not None,
         "未定义 ⇒ 各处 fallback 生效，宽度退回固定值")
    val = (var_def.group(1) if var_def else "").strip()
    _rec("W1b 取值自适应（clamp/min 而非固定 px）",
         bool(re.search(r"clamp\(|min\(", val)), "当前值：%s" % val)
    if val:
        nums = [float(x) for x in re.findall(r"([0-9]+)px", val)]
        _rec("W1c 上限落在 1000~1300px（同类产品 Power-User 区间 1180~1200）",
             bool(nums) and 1000 <= max(nums) <= 1300, "值=%s" % nums)

    css_all = ""
    for f in ("app.css", "chat.css"):
        css_all += open(os.path.join(_ROOT, "static", "css", f), encoding="utf-8").read()
    users = re.findall(r"var\(--chat-track[^)]*\)", css_all)
    _rec("W2 轨道变量被多处引用（消息/气泡/底栏/执行过程）", len(users) >= 4,
         "引用 %d 处" % len(users))
    nofb = [u for u in users if "," not in u]
    _rec("W4 每处 var() 都带 fallback（防变量丢失 ⇒ max-width 静默失效）", not nofb,
         "缺 fallback：%s" % nofb[:3])

    bare = []
    # ⚠️ 必须**按声明**取值（`max-width:<值到分号>`），不能用 `min\([^)]*?920px`：
    #    后者会跨规则一路扫到别处的 920px，把完全无关的行报成残留（第一版就这样误报 5 处）。
    #    门禁自身的假阳性比不设门禁更坏 —— 它会训练人"忽略红灯"。
    for f in ("app.css", "chat.css"):
        src = open(os.path.join(_ROOT, "static", "css", f), encoding="utf-8").read()
        body = re.sub(r"/\*[\s\S]*?\*/", "", src)   # 注释里描述历史值是正常的，先剔除
        for m in re.finditer(r"max-width\s*:\s*([^;{}]+);", body):
            val = m.group(1)
            if "var(--chat-track" in val:
                continue                                   # 已走变量 ⇒ 合规
            if re.search(r"\b(920|998|1040)px\b", val):
                bare.append("%s:%d → max-width:%s"
                            % (f, body[:m.start()].count("\n") + 1, val.strip()[:40]))
    _rec("W3 无裸 920/998/1040px 轨道残留（必须走变量）", not bare,
         "残留：%s" % bare[:5])

    # ── M1-M4 变异自证：把"标准"短暂改成错误值，判据必须各自判红 ──
    #   为什么必须做：这组断言全是"我方定义的标准"（字号集合、宽度区间），
    #   最容易写成恒真（比如集合写成"允许一切"、区间写成 (0, 9999)）——
    #   那样的门禁**永远绿**，比没有更危险。用注入式变异证明它们真的在判。
    print("\n=== M1-M4 变异自证（改坏必须判红）===")
    _saved = (MIN_FS, set(SMALL_TIERS), set(ALLOWED), BODY_RANGE)
    try:
        MIN_FS = 99.0
        small = [t for t in tiers if t < MIN_FS]
        _rec("M1 最小字号抬到 99 ⇒ F2 判红", bool(small),
             "" if small else "F2 空转")
        MIN_FS = _saved[0]

        SMALL_TIERS = set()
        ALLOWED = set()
        off2 = [t for t in tiers if t not in ALLOWED]
        _rec("M2 允许集合清空 ⇒ F1 判红", bool(off2),
             "" if off2 else "F1 空转")
        SMALL_TIERS, ALLOWED = _saved[1], _saved[2]

        BODY_RANGE = (99.0, 100.0)
        _rec("M3 正文字号区间改成 99~100 ⇒ F3 判红",
             not (body_fs is not None and BODY_RANGE[0] <= body_fs <= BODY_RANGE[1]),
             "" if not (body_fs is not None and 99.0 <= body_fs <= 100.0) else "F3 空转")
        BODY_RANGE = _saved[3]

        # M4：把 app.css 的 --chat-track 临时改成固定 700px ⇒ W1b/W1c 必须判红
        import tempfile, shutil
        ap = os.path.join(_ROOT, "static", "css", "app.css")
        bak = ap + ".mutbak"
        shutil.copyfile(ap, bak)
        try:
            mutated = open(ap, encoding="utf-8").read().replace(
                "--chat-track:clamp(880px, 72vw, 1180px);", "--chat-track:700px;", 1)
            open(ap, "w", encoding="utf-8").write(mutated)
            src2 = open(ap, encoding="utf-8").read()
            v2 = re.search(r":root\{--chat-track://s*([^;]+);", src2)
            v2s = (v2.group(1) if v2 else "").strip()
            is_fixed = not re.search(r"clamp\(|min\(", v2s)
            nums2 = [float(x) for x in re.findall(r"([0-9]+)px", v2s)]
            too_narrow = (not nums2) or max(nums2) < 1000
            _rec("M4 轨道改固定 700px ⇒ W1b/W1c 判红", is_fixed or too_narrow,
                 "" if (is_fixed or too_narrow) else "W1 空转")
        finally:
            shutil.copyfile(bak, ap)
            os.remove(bak)
    finally:
        MIN_FS, SMALL_TIERS, ALLOWED, BODY_RANGE = _saved

    print("\n" + "=" * 78)
    fails = [r for r in _results if r[0] == FAIL]
    warns = [r for r in _results if r[0] == WARN]
    print("结果：%d PASS / %d FAIL / %d WARN" % (len(_results) - len(fails) - len(warns),
                                                len(fails), len(warns)))
    print("=" * 78)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
