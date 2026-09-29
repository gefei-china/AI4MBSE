# -*- coding: utf-8 -*-
"""「设置 → 意图样本」页面**真 Chrome 渲染 + 交互回归**（2026-09-26）。

## 为什么这么做（沿用本仓既有约定，见 tools/verify/preview_ctxconfig_page.py 的说明）
本机 agent-browser 反复被 SIGTERM → 仓库的合规做法是「**生产模块源码原样内联**（改了就不算证据）
+ 真数据渲染」。本脚本在此之上再进一步：用**真 Chrome**（headless）渲染 + 截图 + 模拟点击，
因为"布局对不对""点击后有没有反应"这两件事，DOM 桩无法证明。

## 夹具从哪来（不手写，全部取自真实系统）
- 面板 HTML 骨架：**从 static/index.html 原样抽出** `#st-intent-samples` 那段（不复制粘贴，避免漂移）
- 业务数据：`GET /api/intent-samples` 的**真实返回**
- 评测结果：真跑一次 `POST /api/intent-samples/run-eval` 的返回（含准确率/混淆矩阵/错例）
- 模块：`static/js/mods/42-intent-samples.js` 原样内联；仅 `api`/`toast` 打桩（本页只读，不写库）

## 断言
render1（默认态）：统计徽章 / 列表行 / 状态徽章 / 系统判定对照列 / 无 undefined
render2（交互态）：切换筛选为"已确认"→ 列表按 `status=confirmed` 重新取数（**校验请求串**）；
                    点「运行评测」→ 评测卡片出 n/accuracy/macro-F1/混淆矩阵/错例
用法（需服务已在跑）：
    .venv/Scripts/python.exe -X utf8 tools/verify/preview_intent_samples_page.py
"""
import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
API = "http://127.0.0.1:8000"

out_dir = REPO / "tmp" / "preview"
out_dir.mkdir(parents=True, exist_ok=True)
fails, passes = [], []


def chk(cond, msg):
    (passes if cond else fails).append(msg)
    print(("  [PASS] " if cond else "  [FAIL] ") + msg)


# ── 1) 夹具：真实接口返回（只读，不改库）──
import httpx  # noqa: E402
try:
    LIST = httpx.get(API + "/api/intent-samples?limit=200", timeout=30).json()
    EVAL = httpx.post(API + "/api/intent-samples/run-eval", json={}, timeout=300).json()
except Exception as e:
    print("⛔ 服务不可用（%s）→ 先起服务再跑本脚本" % str(e)[:80])
    sys.exit(2)
print("夹具：列表 total=%s  stats=%s" % (LIST.get("total"), LIST.get("stats", {}).get("by_status")))
print("夹具：评测 n=%s accuracy=%s macro_f1=%s" % (EVAL.get("n"), EVAL.get("accuracy"), EVAL.get("macro_f1")))

# ── 2) 面板 HTML：从 index.html 原样抽出（不复制，避免与真实页面漂移）──
idx = (REPO / "static" / "index.html").read_text(encoding="utf-8")
m = re.search(r'(<div class="subpage" id="st-intent-samples".*?)\n      </div>\n', idx, re.S)
assert m, "index.html 里找不到 #st-intent-samples 面板骨架"
panel_html = m.group(1)
# 夹具完整性自证：页面要挂载的容器 id 必须都在（缺了会渲染成空/undefined，正是本夹具该拦的失败模式）
for _need in ("intent-sample-body", "is-btn-eval", "is-f-status", "is-btn-suggest"):
    assert _need in panel_html, "抽出的面板骨架缺少 " + _need
print("夹具：面板骨架 %d 字符，容器 id 齐备" % len(panel_html))

# ── 3) 组页面：真实样式 + 真实模块源码 + 真实数据 ──
core = (REPO / "static/js/mods/01-core.js").read_text(encoding="utf-8")
mod = (REPO / "static/js/mods/42-intent-samples.js").read_text(encoding="utf-8")
esc_src = "\n".join(l for l in core.split("\n") if re.match(r"^function esc[A-Za-z]*\(", l))
assert esc_src.count("function esc") == 2, "未取到 esc/escA"
css = "\n".join((REPO / "static/css" / f).read_text(encoding="utf-8")
                for f in ("tokens.css", "app.css", "chat.css"))

# 模板用**字面 token + replace** 组装（不用 %-formatting）：CSS 里有 `width:100%` 这类百分号，
# 一旦走 `%` 格式化就会抛 "unsupported format character"（上一个夹具已踩过）。
TEMPLATE = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"><title>意图样本页面夹具</title>
<style>__CSS__</style></head>
<body class="panel" style="padding:12px;background:#fff;font-family:system-ui,'Microsoft YaHei',sans-serif;">
__PANEL__
<h3 style="font:600 12px system-ui;color:#888;margin:16px 0 6px;">↑ 以上为真 Chrome 渲染结果（生产 42-intent-samples.js 与真实接口数据）</h3>
<script>__ESC__</script>
<script>
// ── 打桩 api / toast（本页只读：不产生任何写库请求）──
window.__apiCalls = [];
const __LIST = @@FIX_LIST@@, __EVAL = @@FIX_EVAL@@;
async function api(path, opts){
  window.__apiCalls.push(path + (opts && opts.method ? ' [' + opts.method + ']' : ''));
  if(path.indexOf('/api/intent-samples/run-eval') === 0) return __EVAL;
  if(path.indexOf('/api/intent-samples?') === 0) return __LIST;
  if(path.indexOf('/api/intent-samples') === 0) return {ok:false, reason:'stubbed-readonly'};
  throw new Error('unexpected api: ' + path);
}
function toast(m){ window.__toasts = (window.__toasts||[]).concat([String(m)]); }
toast.error = toast; toast.success = toast;
window.__probe = function(id){
  var d = document.createElement('div'); d.id = id;
  d.textContent = document.getElementById('intent-sample-body').innerHTML
                + '<!--EVAL-->' + ((document.getElementById('is-eval')||{}).innerHTML || '')
                + '<!--CALLS-->' + JSON.stringify(window.__apiCalls||[]);
  document.body.appendChild(d);
};
</script>
<script>__MOD__</script>
<script>
(async function(){
  // ⚠️ 必须激活面板：`.subpage{display:none} / .subpage.on{display:block}`（app.css），
  //    不激活则面板高度为 0 —— 截图里只剩探针文本，"布局复核"就是假的（首版即如此，已修）。
  var _sp = document.getElementById('st-intent-samples');
  if (_sp) { _sp.className += ' on'; _sp.style.display = 'block'; }
  await loadIntentSamples();
  __probe('render-probe');
  __EXTRA_JS__
})();
</script></body></html>"""
BASE = (TEMPLATE.replace("@@FIX_LIST@@", json.dumps(LIST, ensure_ascii=False))
                .replace("@@FIX_EVAL@@", json.dumps(EVAL, ensure_ascii=False))
                .replace("__CSS__", css)
                .replace("__PANEL__", panel_html)
                .replace("__ESC__", esc_src)
                .replace("__MOD__", mod))
# 自证：三个 token 都已替换掉（漏一个就会把 token 当 JS/HTML 渲染出来，是夹具最该拦的失败模式）
for _t in ("@@FIX_LIST@@", "@@FIX_EVAL@@", "__CSS__", "__PANEL__", "__ESC__", "__MOD__"):
    assert _t not in BASE, "夹具 token 未替换: " + _t
PLACEHOLDER = "__EXTRA_JS__"


def render(fname, extra_js, dump_name, shot_name):
    hp = out_dir / fname
    hp.write_text(BASE.replace(PLACEHOLDER, extra_js), encoding="utf-8")
    prof = out_dir / "_chrome_profile"
    prof.mkdir(exist_ok=True)
    base = [CHROME, "--headless=new", "--disable-gpu", "--no-first-run", "--hide-scrollbars",
            "--virtual-time-budget=8000", "--user-data-dir=%s" % prof,
            "--window-size=1180,1500"]
    dom = subprocess.run(base + ["--dump-dom", hp.as_uri()],
                         capture_output=True, text=True, encoding="utf-8", timeout=180).stdout
    (out_dir / dump_name).write_text(dom, encoding="utf-8")
    subprocess.run(base + ["--screenshot=%s" % (out_dir / shot_name), hp.as_uri()],
                   capture_output=True, timeout=180)
    return dom, out_dir / dump_name, out_dir / shot_name


def probe_of(dom, pid="render-probe"):
    mm = re.search(r'<div id="%s">(.*?)</div>' % pid, dom, re.S)
    return mm.group(1) if mm else ""


# ── 4a) 默认态 ──
print("── 4a) 默认态渲染 ──")
dom1, dump1, shot1 = render("intent_samples_page.html", "", "intent_samples.dom.txt",
                            "intent_samples.png")
body1 = probe_of(dom1)
st = LIST.get("stats", {}).get("by_status", {})
chk(all(str(v) in body1 for v in st.values()) if st else False,
    "T1 统计徽章显示各状态计数（%s）" % st)
chk(body1.count("已确认") >= 1 and "待确认" in body1,
    "T2 状态徽章文案渲染（已确认/待确认；口径必须显眼）")
# ⚠️ 探针是把 innerHTML 当**文本**承载的 → 标签在探针里是转义形态（&lt;tr&gt;），
#    所以行数要数 `&lt;tr` 而不是 `<tr`（首版就在这里误判成"0 行"）。
rows = body1.count("&lt;tr")
chk(rows >= 10, "T3 列表渲染 ≥10 行（实际 %d 行）" % rows)
_chk_text = (LIST.get("items") or [{}])[0].get("text", "")[:8]
chk(_chk_text and _chk_text in body1, "T4 行内显示真实说法文本（首行「%s」）" % _chk_text)
_sug = next((i for i in (LIST.get("items") or []) if i.get("status") in ("suggested", "new")
             and i.get("hit_intent")), None)
chk(_sug is not None and _sug["hit_intent"] in body1,
    "T5 系统判定对照列可见（hit_intent=%s 与人工标签分列）" % (_sug or {}).get("hit_intent"))
chk("undefined" not in body1 and "NaN" not in body1,
    "T6 渲染产物无 undefined / NaN")
chk(shot1.exists() and shot1.stat().st_size > 3000, "T7 截图已产出（可人工复核布局）")

# ── 4b) 交互态：切筛选 + 点「运行评测」──
print("── 4b) 交互态（切筛选为「已确认」→ 列表重取；点「运行评测」→ 出评测卡片）──")
EXTRA = """
try{
  // 模拟人工操作：把筛选下拉切到"已确认"，触发 onchange 的 isFilterChanged()
  document.getElementById('is-f-status').value = 'confirmed';
  isFilterChanged();
  await new Promise(r=>setTimeout(r,400));
  // 模拟点击「运行评测」按钮（onclick=isRunEval）
  await isRunEval();
} catch(e){ window.__err = String(e); }
__probe('render-probe2');
"""
dom2, dump2, shot2 = render("intent_samples_page_interact.html", EXTRA,
                            "intent_samples_interact.dom.txt", "intent_samples_interact.png")
body2 = probe_of(dom2, "render-probe2")
# 请求串要在**探针**里看（探针=渲染产物+运行时调用记录）：整份 DOM 里也含模块源码的字符串字面量，
# 直接对整份 DOM 断言 '/api/intent-samples?...confirmed' 会命中源码而误判（T4 同款陷阱）。
# 且必须 findall（探针里记录的是**调用数组**：首条是初始加载，筛选后的那条在后面）。
_calls = re.findall(r'/api/intent-samples\?[^\s"<>\[\]]*', body2)
chk(any("status=confirmed" in c for c in _calls),
    "T8 切换筛选后请求串带 status=confirmed（实际调用：%s）" % _calls)
chk(("n=%s" % EVAL.get("n")) in body2 or str(EVAL.get("n")) in body2,
    "T9 评测卡片显示样本量 n=%s" % EVAL.get("n"))
chk(str(EVAL.get("accuracy")) in body2 or ("%.1f%%" % (EVAL.get("accuracy", 0) * 100)) in body2,
    "T10 评测卡片显示准确率（%s）" % EVAL.get("accuracy"))
chk(("macro" in body2.lower()) or (str(EVAL.get("macro_f1")) in body2),
    "T11 评测卡片显示 macro-F1（%s）" % EVAL.get("macro_f1"))
chk(("混淆" in body2) or ("期望" in body2), "T12 评测卡片含混淆矩阵/逐类指标")
chk("undefined" not in body2 and "NaN" not in body2, "T13 交互后渲染产物无 undefined / NaN")
chk(shot2.exists() and shot2.stat().st_size > 3000, "T14 交互态截图已产出")

print("\nDOM: %s\n截图: %s\n     %s" % (dump1, shot1, shot2))
print("结果：%d 通过 / %d 失败" % (len(passes), len(fails)))
if fails:
    print("失败项：\n  " + "\n  ".join(fails))
    sys.exit(1)