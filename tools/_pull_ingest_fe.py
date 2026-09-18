# -*- coding: utf-8 -*-
"""前端：14-sysml.js 版本历史头部加「⇩ 智源拉取」按钮 + 对话流回执卡"""
p = 'static/js/mods/14-sysml.js'
src = open(p, encoding='utf-8').read()

# ① 工程入库按钮旁加「⇩ 智源拉取」
anchor = 'onclick="projectIngestWizard()"'
assert anchor in src, 'ingest btn anchor missing'
old_btn = src[src.find(anchor) - 200:src.find(anchor)]
# 找到含 projectIngestWizard 的完整 button 元素行
import re
m = re.search(r'<button[^>]*onclick="projectIngestWizard\(\)"[^>]*>[^<]*</button>', src)
assert m, 'ingest button element not found'
new_btn = m.group(0).replace('projectIngestWizard()', 'zhiyuanPullIngest()') \
    .replace('>', ' title="从智源拉取当前工程建模数据，后端直接转三元组入个人图库（对话流回执）">', 1) \
    .replace('>📦 工程入库<', '>⇩ 智源拉取<')
src = src.replace(m.group(0), m.group(0) + '\n          ' + new_btn, 1)

# ② 追加拉取函数 + 对话流回执卡
src += '''

// ══ 2026-09-11：智源拉取直通入库（AI 建模对话流回执，不进数据治理面板） ══
async function zhiyuanPullIngest(){
  if(!(await confirmDialog('从智源拉取当前工程建模数据？\\n后端将直接：解析 → 候选化 → 融合闸 → 转三元组 → 入库个人分支图库。\\n（不产生数据治理审核面板待办，回执在本对话流显示）'))) return;
  toast('⇩ 正在从智源拉取建模数据并直通入库…');
  let r;
  try{
    r = await api('/api/knowledge/sysml/pull-ingest', {method:'POST', body:JSON.stringify({})});
  }catch(e){ toast('拉取失败：'+(e.message||'')); return; }
  if(!r || r.error){ toast('拉取失败：'+((r&&r.error)||'')); _zhiyuanReceipt(r); return; }
  _zhiyuanReceipt(r);
  if(_artConv) loadSysmlVersionsBar(_artConv);
}
// 回执卡：插入当前对话流底部（AI 建模流程内可见）
function _zhiyuanReceipt(r){
  const area = document.getElementById('chat-area');
  const s = r.stats || {};
  const row = (k,v,c)=>'<div style="display:flex;justify-content:space-between;border-bottom:1px dashed var(--line);padding:3px 0;"><span style="color:var(--mut);">'+k+'</span><b style="color:'+(c||'var(--ink)')+';">'+v+'</b></div>';
  let body;
  if(r.error){
    body = '<div style="font-size:12px;color:#b91c1c;">✕ 拉取入库失败：'+esc(r.error)+'</div>';
  } else {
    body = '<div style="font-size:12px;line-height:1.8;">'
      + '<div style="margin-bottom:6px;"><span class="st ok" style="font-size:12px;padding:3px 12px;">✅ 智源拉取直通入库完成</span>'
      + '<span style="font-size:10.5px;color:var(--mut);margin-left:8px;">批次 '+esc(r.batch_id||'')+' · 分支 '+esc(r.target_branch||'personal')+' · 耗时 '+((s.elapsed_ms/1000)||0).toFixed(1)+'s</span></div>'
      + '<div style="padding:8px 12px;border:1px solid var(--line);border-radius:8px;background:var(--bg);">'
      + row('模型来源', esc(r.model_name||'智源'))
      + row('候选（实体+关系）', s.candidates||0)
      + row('生成三元组', '+'+(s.triples_staged||0), '#7c3aed')
      + row('自动批准并落图：实体 / 关系', (s.entities_written||0)+' / '+(s.relations_written||0), '#1d4ed8')
      + row('融合闸：自动合并 / 人工队列', (s.auto_merged||0)+' / '+(s.review_queue||0))
      + row('本体校验拒绝', s.rejected||0, s.rejected?'var(--amb)':'')
      + '</div>'
      + (r.text_preview?('<details style="margin-top:6px;"><summary style="cursor:pointer;font-size:10.5px;color:var(--mut);">智源回读文本预览（前 400 字符）</summary><pre style="margin:4px 0 0;padding:6px 8px;background:#0f172a;color:#e2e8f0;border-radius:5px;font-size:10px;overflow-x:auto;white-space:pre-wrap;">'+esc(r.text_preview)+'</pre></details>'):'')
      + '</div>';
  }
  if(!area){ toast(r.error? '拉取失败' : '✅ 智源拉取入库完成'); return; }
  const card = document.createElement('div');
  card.className = 'msg ai';
  card.innerHTML = '<div class="msg-inner"><div class="body" style="border-color:#C9B8F5;background:#FBF9FF;"><div style="font-size:11px;color:#7c3aed;font-weight:600;margin-bottom:6px;">⇩ 智源拉取直通入库</div>'+body+'</div></div>';
  area.appendChild(card);
  area.scrollTop = area.scrollHeight;
}
'''
open(p, 'w', encoding='utf-8').write(src)
print('frontend OK')
