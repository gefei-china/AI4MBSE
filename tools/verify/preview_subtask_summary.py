# -*- coding: utf-8 -*-
"""子任务卡「交付摘要」**真机 + 真 Chrome** 回归（2026-09-26）。

## 验什么
用户报障：每张子任务卡下那行是"一、对上游意图识别结果的承接"这类**内部交接语**，且被硬截 60 字。
本脚本用**真实编排产出的 subtask 事件载荷**，确认：
  1) 后端已补 `ui_summary`（面向用户的一句话），且**不含**内部词/未渲染 markdown；
  2) 协议 `summary` 仍在（下游承接靠它，不能被清洗）；
  3) 真 Chrome 渲染 `subtaskCardHtml` → 摘要行**单行省略**（不再顶成多行）、无 `#`/`**` 裸露。

用法（需服务已在跑）：
    .venv/Scripts/python.exe -X utf8 tools/verify/preview_subtask_summary.py
"""
import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
API = "http://127.0.0.1:8000"
QUERY = "帮我进行工程建模，生成结构视图（BDD），并生成对应的 sysml 代码"   # 已知会走两阶段编排

out = REPO / "tmp" / "preview"
out.mkdir(parents=True, exist_ok=True)
fails, passes = [], []


def chk(cond, msg):
    (passes if cond else fails).append(msg)
    print(("  [PASS] " if cond else "  [FAIL] ") + msg)


import httpx  # noqa: E402
CACHE = out / "subtask_payloads.json"
cid = None
if "--from-cache" in sys.argv:
    # 编排一次要几分钟；载荷已缓存（含 ui_summary）→ 只重跑渲染断言（改夹具/改样式时用这个）
    payloads = json.loads(CACHE.read_text(encoding="utf-8"))
    print("载荷来源：本地缓存 %s（%d 条）" % (CACHE, len(payloads)))
else:
    cid = httpx.post(API + "/api/conversations", json={"title": "子任务摘要回归"}, timeout=20).json().get("id")
    payloads = []
    try:
        with httpx.stream("POST", API + "/api/conversations/%s/chat/stream" % cid,
                          json={"message": QUERY, "branch": "personal"},
                          timeout=httpx.Timeout(420, read=420)) as r:
            cur = None
            for line in r.iter_lines():
                if line.startswith("event: "):
                    cur = line[7:]
                elif line.startswith("data: ") and cur == "subtask":
                    d = json.loads(line[6:])
                    # ⚠️ 只收 **done** 态载荷：`run` 态时子任务还没产出，`sub_res` 为空 → ui_summary 必为空串
                    #    （首版没过滤，抓到 10 条 run 态，看到 ui_summary='' 差点误判成"清洗把内容清没了"）
                    if d.get("title") and d.get("status") == "done":
                        payloads.append(d)
                        if len(payloads) >= 2:
                            break
    except Exception as e:
        print("（流中断/超时，已收到 %d 个子任务载荷：%s）" % (len(payloads), str(e)[:60]))
print("拿到 %d 个真实 subtask 载荷（done 态）" % len(payloads))
if not payloads:
    print("⛔ 未取到载荷（服务未运行或本轮未走编排），退出")
    sys.exit(2)
# 缓存：编排一次要几分钟，抖动时不必重跑（也便于人工复核原始载荷）
(out / "subtask_payloads.json").write_text(json.dumps(payloads, ensure_ascii=False, indent=1), encoding="utf-8")

print("── 1. 后端字段 ──")
BAD = ("承接", "上游", "本任务", "职责是", "执行计划")
for p in payloads:
    ui = p.get("ui_summary") or ""
    proto = (p.get("summary") or {}).get("summary") if isinstance(p.get("summary"), dict) else ""
    print("   · %-16s ui_summary=%r" % ((p.get("title") or "")[:16], ui[:56]))
    chk("ui_summary" in p, "有 ui_summary 字段（键存在，允许为空串）")
    chk((not ui) or (not any(w in ui for w in BAD) and "#" not in ui and "**" not in ui),
        "ui_summary 不含内部交接语/未渲染 markdown")
    chk((proto is None) or isinstance(proto, str),
        "协议 summary 未被清洗（下游承接依赖它）")

# ── 2. 真 Chrome 渲染卡片 ──
core = (REPO / "static/js/mods/01-core.js").read_text(encoding="utf-8")
pipe = (REPO / "static/js/mods/11-pipeline.js").read_text(encoding="utf-8")
esc_src = "\n".join(l for l in core.split("\n") if re.match(r"^function esc[A-Za-z]*\(", l))
fn = re.search(r"(function _procSumText\(t\)\{.*?\n\})", pipe, re.S).group(1)
fn2 = re.search(r"(function subtaskCardHtml\(s\)\{.*?\n\})", pipe, re.S).group(1)
# 夹具已踩坑：subtaskCardHtml 还调用 capTagsFor()，漏内联会 ReferenceError（首版即报 RENDER_ERROR）。
# 正则对参数名用 [^)]* 而非写死 `a`（capTagsFor 的形参其实叫 agentId，写死会取不到 → AttributeError）。
fn3 = re.search(r"(function capTagsFor\([^)]*\)\{.*?\n\})", pipe, re.S).group(1)
css = "\n".join((REPO / "static/css" / f).read_text(encoding="utf-8") for f in ("tokens.css", "app.css", "chat.css"))
TPL = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"><title>子任务卡夹具</title>
<style>__CSS__</style></head>
<body style="padding:14px;background:#fff;font-family:system-ui,'Microsoft YaHei',sans-serif;">
<script>__ESC__</script>
<script>var _agentsCache = [];</script>
<script>__FN__</script>
<script>__FN3__</script>
<script>__FN2__</script>
<div id="host" style="max-width:900px;"></div>
<h3 style="font:600 12px system-ui;color:#888;margin:16px 0 6px;">↑ 真 Chrome 渲染（生产 subtaskCardHtml 原样内联 + 真实编排载荷）</h3>
<script>
(function(){
  try{
    var ps = __PAYLOADS__;
    var host = document.getElementById('host');
    ps.forEach(function(p){ var d=document.createElement('div'); d.innerHTML = subtaskCardHtml(p); host.appendChild(d); });
    var pr=document.createElement('div'); pr.id='render-probe'; pr.textContent = host.innerHTML;
    document.body.appendChild(pr);
  }catch(e){
    var er=document.createElement('div'); er.id='render-probe';
    er.textContent='RENDER_ERROR: '+(e&&(e.message||e)); document.body.appendChild(er);
  }
})();
</script></body></html>"""
html = (TPL.replace("__CSS__", css).replace("__ESC__", esc_src).replace("__FN2__", fn2)
           .replace("__FN3__", fn3).replace("__FN__", fn).replace("__PAYLOADS__", json.dumps(payloads, ensure_ascii=False)))
for t in ("__CSS__", "__ESC__", "__FN__", "__FN2__", "__FN3__", "__PAYLOADS__"):
    assert t not in html, "token 未替换: " + t
hp = out / "subtask_summary.html"
hp.write_text(html, encoding="utf-8")
prof = out / "_chrome_profile"
prof.mkdir(exist_ok=True)
base = [CHROME, "--headless=new", "--disable-gpu", "--no-first-run", "--hide-scrollbars",
        "--virtual-time-budget=5000", "--user-data-dir=%s" % prof, "--window-size=1000,420"]
dom = subprocess.run(base + ["--dump-dom", hp.as_uri()], capture_output=True, text=True,
                     encoding="utf-8", timeout=120).stdout
shot = out / "subtask_summary.png"
subprocess.run(base + ["--screenshot=%s" % shot, hp.as_uri()], capture_output=True, timeout=120)
mm = re.search(r'<div id="render-probe">(.*?)</div>', dom, re.S)
render = mm.group(1) if mm else ""

print("── 2. 渲染断言 ──")
chk("RENDER_ERROR" not in render, "无渲染错误")
chk('class="proc-sum"' in render, "摘要行已按新样式渲染（单行省略）")
chk("white-space:nowrap" in render, "摘要行禁换行（不再顶成 2~3 行）")
chk("title=" in render, "悬停可见全文（title 属性）")
chk("undefined" not in render and "NaN" not in render, "无 undefined / NaN")
chk(shot.exists() and shot.stat().st_size > 2000, "截图已产出")
print("截图: %s" % shot)
try:
    httpx.delete(API + "/api/conversations/%s" % cid, timeout=10)
except Exception:
    pass
print("结果：%d 通过 / %d 失败" % (len(passes), len(fails)))
if fails:
    print("失败项：\n  " + "\n  ".join(fails))
    sys.exit(1)