# -*- coding: utf-8 -*-
"""「意图确认」选择题卡**真 Chrome 渲染回归**（2026-09-26）。

## 为什么
用户要求：意图确定不了时要**和用户确认**（选择题 + 其他/自定义），不要自己硬选一个。
该交互复用内容级澄清卡（`clarifyCardInnerHtml`，11-pipeline.js）。本脚本用**真 Chrome**
渲染这张卡，确认它与"选项列表 + 其他输入 + 提交/跳过"的形态一致、且没有 undefined。

夹具全部取自真实系统（不手写）：
- 卡片 HTML：`static/js/mods/11-pipeline.js` 的 `clarifyCardInnerHtml` 原样内联
- 载荷：向**运行中的服务**发一条"确定不了"的句子，取真实 `clarify_ask` 事件
- 样式：tokens.css + app.css + chat.css

用法（需服务已在跑）：.venv/Scripts/python.exe -X utf8 tools/verify/preview_intent_confirm_card.py
"""
import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
API = "http://127.0.0.1:8000"
QUERY = "帮我看看这个项目的预算"     # 实测会落 llm_weak → 触发意图确认

out = REPO / "tmp" / "preview"
out.mkdir(parents=True, exist_ok=True)
fails, passes = [], []


def chk(cond, msg):
    (passes if cond else fails).append(msg)
    print(("  [PASS] " if cond else "  [FAIL] ") + msg)


import httpx  # noqa: E402
cid = httpx.post(API + "/api/conversations", json={"title": "意图确认卡渲染"}, timeout=20).json().get("id")
payload = None
try:
    with httpx.stream("POST", API + "/api/conversations/%s/chat/stream" % cid,
                      json={"message": QUERY, "branch": "personal"},
                      timeout=httpx.Timeout(120, read=120)) as r:
        cur = None
        for line in r.iter_lines():
            if line.startswith("event: "):
                cur = line[7:]
            elif line.startswith("data: ") and cur == "clarify_ask":
                payload = json.loads(line[6:])
                break
finally:
    try:
        httpx.delete(API + "/api/conversations/%s" % cid, timeout=10)
    except Exception:
        pass
if not payload:
    print("⛔ 未取到 clarify_ask 载荷（服务未运行 / 该句未触发确认），退出")
    sys.exit(2)
print("载荷：title=%s 选项=%d" % (payload.get("title"), len(payload["questions"][0]["options"])))

pipe_src = (REPO / "static/js/mods/11-pipeline.js").read_text(encoding="utf-8")
core = (REPO / "static/js/mods/01-core.js").read_text(encoding="utf-8")
esc_src = "\n".join(l for l in core.split("\n") if re.match(r"^function esc[A-Za-z]*\(", l))
assert esc_src.count("function esc") == 2
m = re.search(r"(function clarifyCardInnerHtml\(qs, title\)\{.*?\n\})", pipe_src, re.S)
assert m, "未取到 clarifyCardInnerHtml"
card_fn = m.group(1)
css = "\n".join((REPO / "static/css" / f).read_text(encoding="utf-8")
                for f in ("tokens.css", "app.css", "chat.css"))

TEMPLATE = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"><title>意图确认卡夹具</title>
<style>__CSS__</style></head>
<body style="padding:14px;background:#fff;font-family:system-ui,'Microsoft YaHei',sans-serif;">
<script>__ESC__</script>
<script>__CARD_FN__</script>
<div id="host" style="max-width:760px;"></div>
<h3 style="font:600 12px system-ui;color:#888;margin:16px 0 6px;">↑ 以上为真 Chrome 渲染结果（生产 clarifyCardInnerHtml 原样内联 + 服务真实 clarify_ask 载荷）</h3>
<script>
// 与 renderClarifyAsk 同款容器/样式（stream-ai .body 内的卡）
(function(){
  try{
    var qs = __PAYLOAD__.questions, title = __PAYLOAD__.title;
    var card = document.createElement('div');
    card.id = 'clarify-ask'; card.className = 'clarify-ask-card';
    card.style.cssText = 'border:1px solid #d3e3fb;background:#f0f6ff;border-radius:8px;padding:10px 12px;font-size:12px;margin-top:10px;';
    card.innerHTML = clarifyCardInnerHtml(qs, title);
    document.getElementById('host').appendChild(card);
    var d = document.createElement('div'); d.id = 'render-probe';
    d.textContent = card.innerHTML;
    document.body.appendChild(d);
  }catch(e){
    // 渲染失败必须可见（否则断言只会看到"空产物"，定位不到真因）
    var err = document.createElement('div'); err.id = 'render-probe';
    err.textContent = 'RENDER_ERROR: ' + (e && (e.message||e));
    document.body.appendChild(err);
  }
})();
</script></body></html>"""
html = (TEMPLATE.replace("__CSS__", css).replace("__ESC__", esc_src)
                .replace("__CARD_FN__", card_fn)
                .replace("__PAYLOAD__", json.dumps(payload, ensure_ascii=False)))
for t in ("__CSS__", "__ESC__", "__CARD_FN__", "__PAYLOAD__"):
    assert t not in html, "夹具 token 未替换: " + t
hp = out / "intent_confirm_card.html"
hp.write_text(html, encoding="utf-8")

prof = out / "_chrome_profile"
prof.mkdir(exist_ok=True)
base = [CHROME, "--headless=new", "--disable-gpu", "--no-first-run", "--hide-scrollbars",
        "--virtual-time-budget=5000", "--user-data-dir=%s" % prof, "--window-size=900,560"]
dom = subprocess.run(base + ["--dump-dom", hp.as_uri()],
                     capture_output=True, text=True, encoding="utf-8", timeout=120).stdout
(out / "intent_confirm_card.dom.txt").write_text(dom, encoding="utf-8")
shot = out / "intent_confirm_card.png"
subprocess.run(base + ["--screenshot=%s" % shot, hp.as_uri()], capture_output=True, timeout=120)
_re = re.search(r'<div id="render-probe">(.*?)</div>', dom, re.S)
render = _re.group(1) if _re else ""

print("── 断言 ──")
chk(payload["title"] in dom, "T1 卡片标题用事件带的标题（不再硬编码「确认建模信息」）")
chk(render.count('type="radio"') >= 4, "T2 选项为单选按钮（实际 %d 个）" % render.count('type="radio"'))
chk('value="__custom__"' in render and "其他 / 自定义" in render, "T3 有「其他 / 自定义」入口")
chk('class="cq-custom"' in render and "可直接在此输入你的答复" in render, "T4 常显补充输入框（可自由作答）")
chk("clarifyAnswerSend()" in render and "clarifySkipSend()" in render, "T5 提交/跳过按钮齐备")
chk("（我的猜测）" in render, "T6 系统当前猜测如实标注（不假装确定）")
chk("undefined" not in render and "NaN" not in render, "T7 渲染产物无 undefined / NaN")
chk(shot.exists() and shot.stat().st_size > 2000, "T8 截图已产出")
print("\nDOM: %s\n截图: %s" % (out / "intent_confirm_card.dom.txt", shot))
print("结果：%d 通过 / %d 失败" % (len(passes), len(fails)))
if fails:
    print("失败项：\n  " + "\n  ".join(fails))
    sys.exit(1)