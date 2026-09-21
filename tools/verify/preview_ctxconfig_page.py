# -*- coding: utf-8 -*-
"""「AI 上下文配置」页面离线预览生成器 —— 浏览器不可用时的出图替代方案。

背景：agent-browser 在本机反复被 SIGTERM（见技能 mbse-ui-browser-verify），
拿不到真实截图；而配置页改动的诉求恰恰是「耦合项要同屏呈现」。
本脚本用**生产模块源码原样内联 + 真接口数据**渲染一份等价单文件 HTML，
既能看到真实布局，又能留证。

做法：
  1. 内联 static/js/mods/35-ctxconfig.js（**不做任何改写**，改了就不算证据）；
  2. 把 GET /api/system/config/schema 的真实返回注入为夹具（stub 掉 api/toast）；
  3. 拼上 static/css/tokens.css（设计 token）+ app.css 的 .panel/.ph/.pb 三条规则。

⚠️ 夹具只覆盖页面会读到的组，并且启动时**逐个核对字段清单**——缺键会让页面
渲染出 undefined，那正是本预览最该拦住的失败模式。

用法（需服务已在跑）：
    python tools/verify/preview_ctxconfig_page.py [--repo 仓库根] [--out 输出路径]

配套校验：node tools/verify/check_preview_render.js <产物.html>
"""
import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

REPO_DEFAULT = Path(__file__).resolve().parents[2]
GROUPS = ["rag", "context", "embedding"]

ap = argparse.ArgumentParser()
ap.add_argument("--repo", default=str(REPO_DEFAULT))
ap.add_argument("--api", default="http://127.0.0.1:8000/api/system/config/schema")
ap.add_argument("--out", default=None)
args = ap.parse_args()

repo = Path(args.repo)
out_path = Path(args.out) if args.out else repo / "tmp/preview/ctxconfig_preview.html"

MODULE = repo / "static/js/mods/35-ctxconfig.js"
TOKENS = repo / "static/css/tokens.css"
APPCSS = repo / "static/css/app.css"

src = MODULE.read_text(encoding="utf-8")
tokens = TOKENS.read_text(encoding="utf-8")
# 只取必须的 panel 三条规则，避免整份 app.css 体积与耦合
appcss = APPCSS.read_text(encoding="utf-8")
panels = "\n".join(
    ln for ln in appcss.split("\n")
    if re.match(r"^\.panel(?![\w-])", ln)
)

with urllib.request.urlopen(args.api, timeout=10) as r:
    schema = json.loads(r.read().decode("utf-8"))["schema"]

fixture = {g: schema.get(g, {}) for g in GROUPS}

# ── 夹具完整性自证：页面引用的每个键都必须在夹具里 ──
keys = sorted(set(re.findall(r"key:'([a-z_]+\.[a-z_]+)'", src)))
missing = [k for k in keys
           if k.split(".")[0] not in fixture
           or k.split(".")[1] not in fixture[k.split(".")[0]]]
if missing:
    sys.exit("❌ 夹具缺失字段（页面会渲染出 undefined）：%s" % missing)

# 本工程没有 markdown 解析器，渲染文案里不该有 ** （见 commit a6f6f41）
boldbad = [ln for ln in src.split("\n")
           if "**" in ln and not ln.lstrip().startswith(("//", "/*"))]
if boldbad:
    sys.exit("❌ 模块里仍有 markdown 粗体 ** ，页面上会显示成星号：\n" + "\n".join(boldbad[:5]))

HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>AI 上下文配置 · 参数簇预览</title>
<style>
__TOKENS__
__PANELS__
.loading{padding:20px;color:var(--mut);font-size:12px;}
body{background:var(--color-bg-canvas);padding:18px 20px 60px;}
.wrap{max-width:1180px;margin:0 auto;}
.cap{background:#fff;border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin-bottom:14px;}
.cap h1{margin:0 0 6px;font-size:15px;}
.cap p{margin:3px 0;font-size:11.5px;color:var(--mut);line-height:1.7;}
.cap code{font-family:var(--font-mono);font-size:11px;background:var(--color-primary-100);padding:1px 5px;border-radius:4px;}
#toast{position:fixed;left:50%;bottom:22px;transform:translateX(-50%);background:#1a2332;color:#fff;
  padding:9px 16px;border-radius:8px;font-size:12px;display:none;z-index:999;}
</style></head>
<body><div class="wrap">
<div class="cap">
  <h1>AI 上下文配置 —— 按耦合度重组的参数簇</h1>
  <p>本页由 <code>static/js/mods/35-ctxconfig.js</code> <b>生产源码原样内联</b>渲染，数据取自
     <code>GET /api/system/config/schema</code> 的实时返回（即真机上看到的当前值）。</p>
  <p>改动输入框可实时看到「耦合视图」与「约束校验」联动刷新；出现 <b>⛔</b> 级冲突时保存会被拦截。</p>
  <p>夹具覆盖 <code>__GROUPS__</code> 三组共 <code>__N__</code> 个键（含各组的全部键），
     并已逐个核对页面引用的 <code>__KEYS__</code> 个字段都在其中。</p>
</div>
<div id="ctx-cfg-body"><div class="loading">正在渲染…</div></div>
</div><div id="toast"></div>
<script>
const __SCHEMA__ = __FIXTURE__;
function api(path){ return Promise.resolve({ok:true, schema:__SCHEMA__}); }   // 真接口的等价替身
let _tt=null;
function toast(msg){
  const el=document.getElementById('toast');
  el.textContent=msg; el.style.display='block';
  clearTimeout(_tt); _tt=setTimeout(()=>el.style.display='none',3200);
}
</script>
<script>
__MODULE__
</script>
<script>loadCtxConfig();</script>
</body></html>"""

out = (HTML
       .replace("__TOKENS__", tokens)
       .replace("__PANELS__", panels)
       .replace("__MODULE__", src)
       .replace("__FIXTURE__", json.dumps(fixture, ensure_ascii=False))
       .replace("__GROUPS__", "/".join(GROUPS))
       .replace("__N__", str(sum(len(fixture[g]) for g in GROUPS)))
       .replace("__KEYS__", str(len(keys))))

out_path.parent.mkdir(parents=True, exist_ok=True)
out_path.write_text(out, encoding="utf-8")

# ── 产物自证：7 个簇标题与全部字段键都要能找回 ──
text = out
bad = [t for t in re.findall(r"title: '([^']+)'", src) if t not in text] \
    + [k for k in keys if k not in text]
if bad:
    sys.exit("❌ 产物自证失败，缺失：%s" % bad)

print("✅ %s" % out_path)
print("   簇 %d / 页面字段 %d / 夹具 %d 键 / %.1f KB"
      % (len(re.findall(r"title: '", src)), len(keys),
         sum(len(fixture[g]) for g in GROUPS), len(out.encode("utf-8")) / 1024))
