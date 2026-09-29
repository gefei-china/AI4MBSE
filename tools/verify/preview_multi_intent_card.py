# -*- coding: utf-8 -*-
"""多意图卡片**前端渲染回归**（2026-09-25/26）：真 Chrome 渲染 + 真实 SSE 载荷 + 生产源码不改写。

## 为什么这么做（本仓的既有约定 + 现实约束）
`tools/verify/preview_ctxconfig_page.py` 的注释记录了本机的现实：agent-browser 反复被 SIGTERM，
于是本仓的页面验证合规做法是「**生产模块源码原样内联**（改了就不算证据）+ 真数据 → 渲染出等价页面」。
本脚本沿用该约定，但把最后一步从"node DOM 桩"升级为**真 Chrome（headless）渲染 + 截图**：
  · 布局/计算样式/CSS 变量由真浏览器求值（DOM 桩无法证明布局对不对）
  · 载荷来自**正在跑的服务**产出的真实 `multi_intent` SSE 事件（不手写夹具，避免"夹具对了、契约错了"）

## 断言什么（改了 `procAddMultiIntent` 或 SSE 契约就会红）
T1 `#proc-box` 里出现 1 个 `proc-block multi`（多意图条目）
T2 标题为「多意图分解（3 个阶段）」、状态徽章为「3 段」
T3 3 个 stage chip，且**文案保序**：`1. 进行需求分析 → requirement_analysis` …
T4 无 `undefined` / `[object Object]`（后者是 raw_subtasks 被改成 dict 时的典型症状）
T5 截图产出（可人工复核布局），且 JS 无报错

用法（需服务已在跑）：
    .venv/Scripts/python.exe -X utf8 tools/verify/preview_multi_intent_card.py
    ... --fixture '{"sequence":[...],"raw_subtasks":[...]}'   # 离线复现渲染
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
QUERY = "提供一段需求，进行需求分析、方案设计、代码校验"

ap = argparse.ArgumentParser()
ap.add_argument("--repo", default=str(REPO))
ap.add_argument("--api", default="http://127.0.0.1:8000")
ap.add_argument("--fixture", default=None, help='直接给载荷 JSON，跳过服务采样')
ap.add_argument("--out", default=None)
args = ap.parse_args()
repo = Path(args.repo)
out_dir = Path(args.out) if args.out else repo / "tmp" / "preview"
out_dir.mkdir(parents=True, exist_ok=True)
html_path = out_dir / "multi_intent_card.html"
shot_path = out_dir / "multi_intent_card.png"
dom_path = out_dir / "multi_intent_card.dom.txt"

fails, passes = [], []


def chk(cond, msg):
    (passes if cond else fails).append(msg)
    print(("  [PASS] " if cond else "  [FAIL] ") + msg)


# ── 1) 载荷：优先用**正在跑的服务**产出的真实事件 ──
payload, cleaned = None, []
if args.fixture:
    payload = json.loads(args.fixture)
    print("载荷来源：命令行 --fixture")
else:
    import httpx
    cid = httpx.post(args.api + "/api/conversations", json={"title": "多意图渲染回归"}, timeout=20).json().get("id")
    cleaned.append(cid)
    with httpx.stream("POST", args.api + "/api/conversations/%s/chat/stream" % cid,
                      json={"message": QUERY, "branch": "personal"},
                      timeout=httpx.Timeout(60, read=60)) as r:
        for line in r.iter_lines():
            if line.startswith("event: "):
                cur = line[7:]
            elif line.startswith("data: ") and cur == "multi_intent":
                payload = json.loads(line[6:])
                break            # 拿到即断连：不触发后续真编排
    print("载荷来源：服务 %s 的真实 SSE 事件（会话 %s，拿到即断连）" % (args.api, cid))
if not payload:
    print("⛔ 未取到 multi_intent 载荷（服务未运行或该句未判多意图），退出")
    sys.exit(2)
print("  载荷 = %s" % json.dumps(payload, ensure_ascii=False))

# ── 2) 组页面：真实样式 + 真实模块源码（原样内联，零改写）──
core = (repo / "static/js/mods/01-core.js").read_text(encoding="utf-8")
pipe = (repo / "static/js/mods/11-pipeline.js").read_text(encoding="utf-8")
# esc/escA 从 01-core.js **逐字**取出（01-core.js 顶部有 localStorage/DOM 的 IIFE，整份内联会干扰夹具；
# 这里只取渲染模板真正依赖的两个转义函数，且不修改一个字符）
esc_src = "\n".join(l for l in core.split("\n")
                    if re.match(r"^function esc[A-Za-z]*\(", l))
assert esc_src.count("function esc") == 2, "未取到 esc/escA 的定义"
css = "\n".join((repo / "static/css" / f).read_text(encoding="utf-8")
                for f in ("tokens.css", "app.css", "chat.css"))

html = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>多意图卡片渲染夹具</title><style>%s</style></head>
<body style="padding:14px;background:#fff;font-family:system-ui,'Microsoft YaHei',sans-serif;">
<div id="proc-box"></div><div id="chat-area" style="height:200px;"></div>
<h3 style="font:600 13px system-ui;color:#333;margin:14px 0 6px;">↑ 以上为真 Chrome 渲染结果（生产 11-pipeline.js 原样内联）</h3>
<script>%s</script>
<script>%s</script>
<script>
// 与 SSE 派发处同一入口（static/js/mods/11-pipeline.js 的 evType==='multi_intent' 分支调的就是它）
procAddMultiIntent(%s);
// 探针：把**渲染产物**本身（proc-box 的 innerHTML）以纯文本形式落到 DOM 里，供断言使用。
// 为什么需要它：本页为实现"生产源码原样内联"必然把 11-pipeline.js 源码也放进文档，而源码里
// 本来就有 `undefined` 字样 —— 直接在整份 DOM 上断言 undefined 会**误判**（首版就踩了）。
window.__probe = function(id){
  var d = document.createElement('div'); d.id = id;
  d.textContent = document.getElementById('proc-box').innerHTML;
  document.body.appendChild(d);
};
__probe('render-probe');
__EXTRA_JS__
</script>
</body></html>""" % (css, esc_src, pipe, json.dumps(payload, ensure_ascii=False))

# ⚠️ 变体注入用 **字符串替换** 而不是 `%` 再格式化一次：CSS 里有 `width:100%` 这类百分号，
#    再走一次 %-formatting 会直接抛 "unsupported format character"（夹具会建不出来）。
PLACEHOLDER = "__EXTRA_JS__"


def render(fname, extra_js, dump_name, shot_name):
    """用 `extra_js` 变体生成一个夹具页，真 Chrome 跑出 DOM + 截图。"""
    hp = out_dir / fname
    hp.write_text(html.replace(PLACEHOLDER, extra_js), encoding="utf-8")
    dp, sp = out_dir / dump_name, out_dir / shot_name
    prof = out_dir / "_chrome_profile"
    prof.mkdir(exist_ok=True)
    base = [CHROME, "--headless=new", "--disable-gpu", "--no-first-run", "--hide-scrollbars",
            "--user-data-dir=%s" % prof, "--window-size=1000,420"]
    d = subprocess.run(base + ["--dump-dom", hp.as_uri()],
                       capture_output=True, text=True, encoding="utf-8", timeout=120).stdout
    dp.write_text(d, encoding="utf-8")
    subprocess.run(base + ["--screenshot=%s" % sp, hp.as_uri()], capture_output=True, timeout=120)
    return hp, d, dp, sp


def probe_of(dom_text, pid="render-probe"):
    """取探针承载的**渲染产物**文本（避开内联源码里的 undefined 字样）。"""
    m = re.search(r'<div id="%s">(.*?)</div>' % pid, dom_text, re.S)
    return m.group(1) if m else ""


# ── 3) 真 Chrome：默认态（折叠）+ 展开态（点击交互）──
print("── 3a) 默认态（不点击）──")
html_path, dom, dom_path, shot_path = render(
    "multi_intent_card.html", "", "multi_intent_card.dom.txt", "multi_intent_card.png")
print("夹具页面：%s（%d KB）" % (html_path, html_path.stat().st_size // 1024))
print("── 3b) 点击折叠态（2026-09-26 起默认已展开 → 点一下变折叠；仍调生产入口 toggleProc）──")
EXPAND_JS = ("var _h=document.querySelector('#proc-box .proc-head'); if(_h) toggleProc(_h);"
             "__probe('render-probe2');")
_, dom2, dom2_path, shot2_path = render(
    "multi_intent_card_toggled.html", EXPAND_JS, "multi_intent_card_toggled.dom.txt",
    "multi_intent_card_toggled.png")

# ── 4) 断言（对真浏览器产出的 DOM）──
print("── 断言 ──")
render_txt = probe_of(dom)
chk("proc-block multi" in dom, "T1 时间线出现多意图条目（proc-block multi）")
chk(re.search(r"多意图分解（3 个阶段）", dom) is not None, "T2a 标题为「多意图分解（3 个阶段）」")
chk(re.search(r">3 段<", dom) is not None, "T2b 状态徽章为「3 段」")
chips = re.findall(r'1\. 进行需求分析<span[^>]*> → </span>requirement_analysis', dom)
chk(len(chips) == 1, "T3a 第 1 段 chip 文案保序（进行需求分析 → requirement_analysis）")
seq_txt = re.findall(r'([123])\. ([^<]*)<span[^>]*> → </span>([a-z_]+)', dom)
chk([(i, t, s) for i, t, s in seq_txt] ==
    [("1", "进行需求分析", "requirement_analysis"), ("2", "方案设计", "design"), ("3", "代码校验", "review")],
    "T3b 三段 chip 全量保序对齐（%s）" % (seq_txt,))
chk(bool(render_txt) and "undefined" not in render_txt and "[object Object]" not in render_txt,
    "T4 渲染产物无 undefined / [object Object]（raw_subtasks 契约未被破坏）")
chk(render_txt.count('class="hil l0"') == 3,
    "T4b 渲染产物恰含 3 个 stage chip（实际 %d）" % render_txt.count('class="hil l0"'))
chk("proc-block multi collapsed" not in dom,
    "T6a 默认态即为**展开**（2026-09-26 按用户决定由折叠改为展开：识别结果必须可见）")
render2 = probe_of(dom2, "render-probe2")
chk("proc-block multi collapsed" in render2 or "proc-block multi running" in render2,
    "T6b 点击卡片头后**折叠**（toggleProc 仍可交互，不是只读展示）")
chk(render2.count('class="hil l0"') == 3 and "进行需求分析" in render2,
    "T6c 折叠后 3 段 chip 仍在 DOM 内（文案不丢，展开即见）")
chk(shot_path.exists() and shot_path.stat().st_size > 2000
    and shot2_path.exists() and shot2_path.stat().st_size > 2000,
    "T5 两张截图已产出（默认态 + 展开态，可人工复核布局）")
if render_txt and "undefined" in render_txt:
    i = render_txt.find("undefined")
    print("      ↳ 现场：…%s…" % render_txt[max(0, i - 60):i + 40])

for cid in cleaned:      # 不留痕
    try:
        import httpx
        httpx.delete("%s/api/conversations/%s" % (args.api, cid), timeout=10)
    except Exception:
        pass

print("\nDOM: %s\n截图: %s" % (dom_path, shot_path))
print("结果：%d 通过 / %d 失败" % (len(passes), len(fails)))
if fails:
    print("失败项：\n  " + "\n  ".join(fails))
    sys.exit(1)