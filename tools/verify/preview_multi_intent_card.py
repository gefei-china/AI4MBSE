# -*- coding: utf-8 -*-
"""多意图卡片**前端渲染回归**（2026-09-25/26）：真 Chrome 渲染 + 真实 SSE 载荷 + 生产源码不改写。

## 为什么这么做（本仓的既有约定 + 现实约束）
`tools/verify/preview_ctxconfig_page.py` 的注释记录了本机的现实：agent-browser 反复被 SIGTERM，
于是本仓的页面验证合规做法是「**生产模块源码原样内联**（改了就不算证据）+ 真数据 → 渲染出等价页面」。
本脚本沿用该约定，但把最后一步从"node DOM 桩"升级为**真 Chrome（headless）渲染 + 截图**：
  · 布局/计算样式/CSS 变量由真浏览器求值（DOM 桩无法证明布局对不对）
  · 载荷来自**正在跑的服务**产出的真实 `multi_intent` SSE 事件（不手写夹具，避免"夹具对了、契约错了"）

## 断言什么（改了 `procAddMultiIntent` 或 SSE 契约就会红）
T0 渲染期间**无未捕获 JS 异常**（防止"静默失败"再次伪装成渲染不达标）
T1 `#proc-box` 里出现 1 个 `proc-block multi`（多意图条目）
T2 标题为「多意图分解（3 个阶段）」、状态徽章为「3 段」
T3 3 个 stage chip，且**文案保序**：`1. 进行需求分析 → requirement_analysis` …
T4 无 `undefined` / `[object Object]`（后者是 raw_subtasks 被改成 dict 时的典型症状）
T5 截图产出（可人工复核布局），且 JS 无报错

## 2026-10-23 修复记录（本门禁此前 4 个失败全是**验证器**的错，非产品缺陷）
现场：T4 / T4b / T6b / T6c 红，但整份 DOM 里 chips 明明渲染成功。
根因两条，都是"症状与功能没写完全一致"的经典：
① **宿主依赖缺失**：`procRender()` 末尾调用 `stickBottom()`（定义在 `03-chat.js`，
   夹具未内联）→ `ReferenceError` **抛在同一个 `<script>` 块内** → 同一块后面的
   探针代码**从未执行** → 探针文本为空 → 四个断言全被误判为失败。
   （chips 之所以在 DOM 里：`b.innerHTML = h` 在抛错**之前**已执行。）
   修法：补 `stickBottom`/`toast` 垫片 + `window.onerror` 捕获。
② **探针通道被转义**：探针原用 `textContent` 承载 innerHTML，dump-dom 会把 `"` 序列
   化成 `&quot;` → 即便探针执行了，`class="hil l0"` 也永远数不到。
   修法：探针改用 `data-b64`（base64）传递，Python 侧解码。
此外新增 T0 断言（无未捕获 JS 异常）——今后再有同类静默失败，门禁会**响**。

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

# ★ 2026-10-23 修：宿主依赖垫片 + JS 错误捕获（本门禁此前 4 个"失败"全是验证器的错）
#   现场：11-pipeline.js 的 procRender() 末尾调用 stickBottom()（定义在 03-chat.js，
#   夹具未内联）→ ReferenceError **抛在同一个 <script> 块内** → 同一块后面的探针
#   代码从未执行 → 探针文本为空 → T4/T4b/T6b/T6c 全被误判为失败。
#   症状迷惑性极强：chips 明明渲染进了 DOM（b.innerHTML=h 在抛错之前已执行），
#   唯独探针 div 不存在。
#   两条修法（都不改生产代码）：
#   ① 垫片：补上被测渲染路径真正需要的宿主全局（stickBottom/toast）。这些是**滚动与
#      提示**行为，与本门禁断言的多意图卡片渲染无关，no-op 不会掩盖任何被测行为。
#   ② 错误捕获：window.onerror 收集未捕获异常并写进 DOM，新增 T0 断言"渲染期间
#      无未捕获 JS 错误"——今后再有同类"静默失败"，门禁会**响**而不是给出假红。
#   （教训同源：验证器出错时，症状与"功能没写"完全一致。）
STUB_JS = """
window.__errs = [];
window.onerror = function(m, s, l, c, e){ window.__errs.push(String(m)+' @'+l+':'+c); };
window.addEventListener('unhandledrejection', function(e){
  window.__errs.push('unhandledrejection: '+String(e.reason));
});
// —— 宿主垫片（被测路径的最小依赖；行为无关，仅避免 ReferenceError 中断脚本块）——
window.stickBottom = window.stickBottom || function(){};
window.toast = window.toast || function(){};
"""
# ⚠️ 顺序要求（反证二实测发现）：**本块必须排在生产脚本之前**。
#   否则生产脚本（如 procRender 末尾的 stickBottom）抛的 ReferenceError 发生在
#   onerror hook 装上之前 → 异常被浏览器吞掉 → T0 反而 PASS（假绿）。
#   正解：垫片 + hook 都在最前 ⇒ 生产脚本里的未定义全局被当场抓住。
css = "\n".join((repo / "static/css" / f).read_text(encoding="utf-8")
                for f in ("tokens.css", "app.css", "chat.css"))

html = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>多意图卡片渲染夹具</title><style>%s</style></head>
<body style="padding:14px;background:#fff;font-family:system-ui,'Microsoft YaHei',sans-serif;">
<div id="proc-box"></div><div id="chat-area" style="height:200px;"></div>
<h3 style="font:600 13px system-ui;color:#333;margin:14px 0 6px;">↑ 以上为真 Chrome 渲染结果（生产 11-pipeline.js 原样内联）</h3>
<script>
%s
</script>
<script>%s</script>
<script>%s</script>
<script>
// ⚠️ 2026-10-23：调用点**必须 try/catch**（不能只靠 window.onerror）。
//   现场：生产脚本里抛的未捕获异常会让同块的探针代码整块不执行（探针 div 根本不
//   存在），而 headless dump-dom 下 window.onerror **不一定**被触发（实测 E=[]）。
//   ⇒ 只有在调用点就地捕获，才能既拿到错误、又保证探针继续跑完。
//   （这正是"静默失败伪装成渲染不达标"的病根，必须在夹具层堵死。）
try {
  procAddMultiIntent(%s);
} catch(e){
  window.__errs.push('procAddMultiIntent: '+(e && e.message ? e.message : String(e)));
}
// 探针：把**渲染产物**本身（proc-box 的 innerHTML）以 **base64** 落到 DOM 属性里，供断言使用。
// 为什么需要它：本页为实现"生产源码原样内联"必然把 11-pipeline.js 源码也放进文档，而源码里
// 本来就有 `undefined` 字样 —— 直接在整份 DOM 上断言 undefined 会**误判**（首版就踩了）。
// ⚠️ 为什么用 base64 而不是 textContent：dump-dom 会把文本节点里的 `"` 序列化成 `&quot;`，
//    于是 `class="hil l0"` 永远数不到（T4b 假红的第二个来源）。base64 无需转义，原样还原。
// ⚠️ 2026-10-23：探针必须**独立成块**放在 procAddMultiIntent 之后 —— 若与调用同块，
//    前一个块的异常会中断本块，探针永不执行（表现为"DOM 里有 chips 但探针 div 不存在"，
//    极像渲染失败）。这里还把未捕获异常一并回填，供 T0 断言。
window.__probe = function(id){
  var d = document.createElement('div'); d.id = id;
  try{
    var raw = document.getElementById('proc-box').innerHTML;
    d.setAttribute('data-b64', btoa(unescape(encodeURIComponent(raw))));
  }catch(e){ window.__errs.push('probe: '+e.message); }
  document.body.appendChild(d);
};
try { __probe('render-probe'); } catch(e){ window.__errs.push('probe: '+e.message); }
__EXTRA_JS__
// 错误回填（供 T0 断言）：**必须放在 __EXTRA_JS__ 之后**，才能捕获点击交互等
// 变体代码抛出的异常（否则变体里的错只会被吞掉、门禁仍然全绿）。
try{
  var ed=document.createElement('div'); ed.id='jserrs';
  ed.setAttribute('data-b64', btoa(unescape(encodeURIComponent(JSON.stringify(window.__errs||[])))));
  document.body.appendChild(ed);
}catch(e){}
</script>
</body></html>""" % (css, STUB_JS, esc_src, pipe, json.dumps(payload, ensure_ascii=False))

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
    """取探针承载的**渲染产物**（base64 解码，避开 dump-dom 的实体转义与内联源码噪声）。"""
    m = re.search(r'<div id="%s" data-b64="([^"]*)"' % pid, dom_text)
    if not m:
        return ""
    import base64
    try:
        return base64.b64decode(m.group(1)).decode("utf-8", "replace")
    except Exception:
        return ""


def errs_of(dom_text):
    """取页面回填的未捕获 JS 异常列表（base64 编码的 JSON）。"""
    m = re.search(r'<div id="jserrs" data-b64="([^"]*)"', dom_text)
    if not m:
        return []
    import base64
    try:
        return json.loads(base64.b64decode(m.group(1)).decode("utf-8", "replace"))
    except Exception:
        return ["<errs 解析失败>"]


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
errs = errs_of(dom) + errs_of(dom2)
chk(not errs, "T0 渲染期间无未捕获 JS 异常（%s）" % (errs[:3] if errs else "无"))
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