# -*- coding: utf-8 -*-
"""原型：把周报渲染成一张独立 HTML 看板（价值感知的第一版可视形态）。

用法：
    python tools/proto_evolution_report.py            # 用真库副本跑
    python tools/proto_evolution_report.py --html out.html

产出的 HTML 是**单文件、零外部依赖**的（数据内联），可直接双击打开看效果。
本文件是原型，不接端点、不进 CI——先让形状和口径过一遍再决定产品化。
"""
import argparse
import io
import json
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# ── 准备：库副本 + 建表 + 跑各域引擎（让台账真的有数据可读）────────────────
_TMP = os.path.join(ROOT, "tmp", "proto_report")
os.makedirs(_TMP, exist_ok=True)
_REAL = os.path.join(ROOT, "mbse.db")
_COPY = os.path.join(_TMP, "report_copy.db")

import shutil                                                     # noqa: E402
if os.path.exists(_REAL) and not os.path.exists(_COPY):
    shutil.copyfile(_REAL, _COPY)          # 覆写即可，无需先删
os.environ["MBSE_DB_PATH"] = _COPY
os.environ["MBSE_CONFIG_PATH"] = os.path.join(_TMP, "config.json")
io.open(os.environ["MBSE_CONFIG_PATH"], "w", encoding="utf-8").write("{}")

from database.connection import get_db                           # noqa: E402
from database.schema import init_db                              # noqa: E402

init_db()
_conn = get_db()
_conn.row_factory = sqlite3.Row


def _bootstrap():
    """在副本上跑一遍各域的 scan/generate，让台账有真实数据可读。

    ★真库这些表是最近几天才建的、还没跑过流程 ⇒ 台账本来是空的。
    直接对空台账出周报会得到一张"全是无数据"的空表，看不出效果也
    看不出价值——**原型要在副本上先把流程跑一遍**（真库一个字都不动）。
    """
    out = []
    try:
        from core import curriculum as cu
        g = cu.generate(_conn)
        out.append("curriculum: created=%s skipped=%s" % (g.get("created"),
                                                          g.get("skipped")))
    except Exception as e:                                       # noqa: BLE001
        out.append("curriculum: 跳过 (%s: %s)" % (type(e).__name__, e))
    try:
        from core import agent_archive as aa
        s = aa.scan(_conn)
        out.append("archive: scanned=%s created=%s" % (s.get("scanned"),
                                                        s.get("created")))
    except Exception as e:                                       # noqa: BLE001
        out.append("archive: 跳过 (%s: %s)" % (type(e).__name__, e))
    try:
        from core import flow_topology as ft
        r = ft.scan(_conn)
        ft.record_scan(_conn, r)
        out.append("topology: canvases=%s groups=%s"
                   % (r["overview"].get("canvases"),
                      r["overview"].get("isomorphic_groups")))
    except Exception as e:                                       # noqa: BLE001
        out.append("topology: 跳过 (%s: %s)" % (type(e).__name__, e))
    return out


_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>自进化周报（原型）</title>
<style>
  :root{--bg:#F7F7F5;--card:#FFFFFF;--line:rgba(0,0,0,.10);--tx:#1F1F1D;
        --tx2:#5F5E5A;--ok:#0F6E56;--okbg:#E1F5EE;--warn:#854F0B;
        --warnbg:#FAEEDA;--unk:#5F5E5A;--unkbg:#F1EFE8;--red:#A32D2D;
        --redbg:#FCEBEB;--acc:#534AB7;--accbg:#EEEDFE}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--tx);
       font:14px/1.65 "PingFang SC","Microsoft YaHei",system-ui,sans-serif;
       padding:32px 20px}
  .wrap{max-width:1080px;margin:0 auto}
  h1{font-size:22px;font-weight:500;margin:0 0 6px}
  .q{color:var(--tx2);font-size:15px;margin:0 0 4px}
  .meta{color:var(--tx2);font-size:12px;margin:0 0 24px}
  .ovr{display:inline-flex;align-items:center;gap:8px;padding:8px 16px;
       border-radius:999px;font-size:13px;font-weight:500;margin-bottom:24px}
  .ovr.att{background:var(--warnbg);color:var(--warn)}
  .ovr.cln{background:var(--okbg);color:var(--ok)}
  .ovr.unk{background:var(--unkbg);color:var(--unk)}
  .sec{background:var(--card);border:1px solid var(--line);border-radius:14px;
       padding:20px 22px;margin-bottom:16px}
  .sh{display:flex;align-items:baseline;gap:10px;margin-bottom:14px}
  .sh h2{font-size:16px;font-weight:500;margin:0}
  .tag{font-size:12px;padding:3px 10px;border-radius:999px;white-space:nowrap}
  .t-att{background:var(--warnbg);color:var(--warn)}
  .t-cln{background:var(--okbg);color:var(--ok)}
  .t-unk{background:var(--unkbg);color:var(--unk)}
  .t-red{background:var(--redbg);color:var(--red)}
  .t-acc{background:var(--accbg);color:var(--acc)}
  .why{color:var(--tx2);font-size:13px;margin:0 0 12px}
  .sub{border-left:2px solid var(--line);padding:2px 0 2px 14px;margin:10px 0}
  .sub .h{font-size:13px;font-weight:500;margin-bottom:2px}
  .sub .r{font-size:13px;color:var(--tx2)}
  .sub .n{font-size:12px;color:var(--tx2);margin-top:4px;font-style:italic}
  .mt{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0 0}
  .m{background:var(--unkbg);border-radius:8px;padding:6px 11px;font-size:12px}
  .m b{font-weight:500;font-size:14px;margin-left:5px}
  ul.it{list-style:none;padding:0;margin:12px 0 0}
  ul.it li{border-top:1px solid var(--line);padding:9px 0;font-size:13px}
  ul.it li:first-child{border-top:none}
  .it-t{font-weight:500}
  .it-d{color:var(--tx2);font-size:12px;margin-top:3px}
  .it-p{color:var(--tx2);font-size:11px;margin-top:3px;font-family:ui-monospace,monospace}
  .foot{color:var(--tx2);font-size:12px;margin-top:20px;padding-top:14px;
        border-top:1px solid var(--line)}
  .src{font-size:11px;color:var(--tx2);margin-top:12px}
  h3{font-size:13px;font-weight:500;margin:18px 0 6px;color:var(--tx2)}
</style></head><body><div class="wrap">
<div id="root"></div>
</div>
<script>
const DATA = __DATA__;
function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
const TAGS={clean:['t-cln','清'],needs_attention:['t-att','待办'],insufficient_data:['t-unk','无数据']};
function tag(v){const t=TAGS[v]||['t-unk','?'];return '<span class="tag '+t[0]+'">'+t[1]+'</span>';}
function ovrTag(v){const map={clean:['cln','整体清'],needs_attention:['att','有待办'],insufficient_data:['unk','本周无事件']};const m=map[v]||map.insufficient_data;return '<div class="ovr '+m[0]+'">'+m[1]+'</div>';}
function metric(k,v){return '<div class="m">'+esc(k)+'<b>'+esc(v===null||v===undefined?'—':v)+'</b></div>';}
function render(){
  const d=DATA, h=[];
  h.push('<h1>'+esc(d.title)+'</h1>');
  h.push('<p class="q">'+esc(d.question)+'</p>');
  h.push('<p class="meta">数据源：真库副本（只读）· 口径：既有台账统计，无数据记「本周无」而非 0</p>');
  h.push(ovrTag(d.overall));
  for(const s of d.sections){
    h.push('<div class="sec">');
    h.push('<div class="sh">'+tag(s.verdict)+'<h2>'+esc(s.title)+'</h2></div>');
    if(s.subblocks&&s.subblocks.length){
      for(const b of s.subblocks){
        h.push('<div class="sub"><div class="h">'+tag(b.verdict)+' '+esc(b.title)+'</div>');
        h.push('<div class="r">'+esc(b.reason)+'</div>');
        if(b.note) h.push('<div class="n">'+esc(b.note)+'</div>');
        h.push('</div>');
      }
    } else {
      h.push('<p class="why">'+esc(s.reason)+'</p>');
    }
    const m=s.metrics||{};const keys=Object.keys(m);
    if(keys.length){
      h.push('<div class="mt">');
      for(const k of keys){
        const v=m[k];
        if(v===null||v===undefined||typeof v==='number'||typeof v==='boolean') h.push(metric(k,v));
      }
      h.push('</div>');
    }
    const listKeys=keys.filter(k=>Array.isArray(m[k])&&m[k].length&&typeof m[k][0]==='object');
    for(const k of listKeys){
      h.push('<h3>'+esc(k)+'</h3><ul class="it">');
      for(const it of m[k].slice(0,6)){
        const parts=Object.entries(it).filter(([kk,vv])=>typeof vv!=='object');
        h.push('<li><div class="it-t">'+esc(parts.map(([kk,vv])=>kk+'='+vv).join(' · '))+'</div>');
        const t=it.title||it.suggestion||it.reason;
        if(t&&t!==undefined) h.push('<div class="it-d">'+esc(t)+'</div>');
        h.push('</li>');
      }
      h.push('</ul>');
    }
    const items=s.items||[];
    if(items.length){
      h.push('<h3>明细</h3><ul class="it">');
      for(const it of items.slice(0,6)){
        if(typeof it==='object'){
          const t=it.title||it.action||'';
          const sub=it.suggestion||it.preview||'';
          h.push('<li>'+(t?'<div class="it-t">'+esc(t)+'</div>':''));
          if(sub) h.push('<div class="it-d">'+esc(sub)+'</div>');
          h.push('</li>');
        } else h.push('<li>'+esc(it)+'</li>');
      }
      h.push('</ul>');
    }
    h.push('</div>');
  }
  h.push('<div class="foot">'+esc(d.disclaimer)+'</div>');
  document.getElementById('root').innerHTML=h.join('');
}
render();
</script></body></html>
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--html", default=os.path.join(ROOT, "outputs",
                                                  "evolution_report_proto.html"))
    ap.add_argument("--no-bootstrap", action="store_true",
                    help="不在副本上跑各域 scan（看空台账效果）")
    args = ap.parse_args()

    if not args.no_bootstrap:
        print("副本上预热各域台账：")
        for line in _bootstrap():
            print("  ·", line)

    from core.evolution_report import build_report, render_text
    report = build_report(_conn)
    print()
    print(render_text(report))

    os.makedirs(os.path.dirname(args.html), exist_ok=True)
    html = _HTML.replace("__DATA__", json.dumps(report, ensure_ascii=False))
    io.open(args.html, "w", encoding="utf-8").write(html)
    print("\nHTML 看板已生成：%s" % args.html)


if __name__ == "__main__":
    main()
