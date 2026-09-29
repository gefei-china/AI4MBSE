# -*- coding: utf-8 -*-
"""暗色下新增区块的文字对比度取证（节点在文档内，故 getComputedStyle 有效）。

背景：暗色截图里「治理红线」第 2 行的指标名看起来偏暗，肉眼判断不可靠 →
按本仓纪律，改为读真实计算样式并计算相对亮度对比度（WCAG 4.5:1 为正文阈值）。
"""
import sys

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8000"

JS = r"""
() => {
  function rgb(s){ const m=(s||'').match(/[\d.]+/g)||[]; return m.slice(0,3).map(Number); }
  function lum(c){ const f=v=>{v/=255; return v<=0.03928? v/12.92 : Math.pow((v+0.055)/1.055,2.4);};
    return 0.2126*f(c[0])+0.7152*f(c[1])+0.0722*f(c[2]); }
  function ratio(a,b){ const l1=lum(a),l2=lum(b); const hi=Math.max(l1,l2),lo=Math.min(l1,l2);
    return (hi+0.05)/(lo+0.05); }
  function effBg(el){ let n=el; while(n){ const bg=rgb(getComputedStyle(n).backgroundColor);
      if(getComputedStyle(n).backgroundColor!=='rgba(0, 0, 0, 0)') return bg; n=n.parentElement; }
    return [255,255,255]; }
  const out=[];
  document.querySelectorAll('#kb-redline .todo-item').forEach((row,i)=>{
    const b=row.querySelector('b');
    const cs=getComputedStyle(b);
    out.push({sel:'#kb-redline .todo-item['+i+'] b', text:(b.innerText||'').slice(0,20),
      color:cs.color, bg:'rgb('+effBg(row).join(',')+')',
      ratio:+ratio(rgb(cs.color), effBg(row)).toFixed(2)});
  });
  document.querySelectorAll('#kb-gov .asset').forEach((card,i)=>{
    const n=card.querySelector('.n'), l=card.querySelector('.l span');
    const bg=effBg(card);
    out.push({sel:'#kb-gov .asset['+i+'] .n', text:(n.innerText||'').slice(0,12),
      color:getComputedStyle(n).color, bg:'rgb('+bg.join(',')+')',
      ratio:+ratio(rgb(getComputedStyle(n).color), bg).toFixed(2)});
    if(l) out.push({sel:'#kb-gov .asset['+i+'] .l', text:(l.innerText||'').slice(0,14),
      color:getComputedStyle(l).color, bg:'rgb('+bg.join(',')+')',
      ratio:+ratio(rgb(getComputedStyle(l).color), bg).toFixed(2)});
  });
  const s=document.querySelector('#kb-stats .asset .n');
  if(s){ const bg=effBg(s.parentElement);
    out.push({sel:'#kb-stats .asset .n', text:(s.innerText||'').slice(0,10),
      color:getComputedStyle(s).color, bg:'rgb('+bg.join(',')+')',
      ratio:+ratio(rgb(getComputedStyle(s).color), bg).toFixed(2)}); }
  return out;
}
"""

with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1680, "height": 1050})
    pg.goto(BASE + "/", wait_until="networkidle", timeout=60000)
    pg.wait_for_timeout(1500)
    pg.evaluate("() => { try { go('kb','kb-a'); } catch(e){} }")
    pg.wait_for_timeout(3500)
    pg.evaluate("() => setTheme('dark')")
    pg.wait_for_timeout(1500)
    rows = pg.evaluate(JS)
    b.close()

bad = [r for r in rows if r["ratio"] < 4.5]
print(f"暗色下取证 {len(rows)} 个文本节点，对比度 < 4.5:1 的有 {len(bad)} 个")
for r in rows:
    flag = "❌" if r["ratio"] < 4.5 else "✅"
    print(f"  {flag} {r['ratio']:>5}:1  {r['sel']:<34} color={r['color']:<20} bg={r['bg']:<18} «{r['text']}»")
sys.exit(1 if bad else 0)