/* 我的反馈（2026-11-06 P0 · 可感知性）
   把点踩后的空头 toast「将用于改进」变成可追踪的编号 + 状态 + 时间线。
   数据源：GET /api/my-feedback、GET /api/my-feedback/{id}（后端按 created_by 隔离）。
   纪律：不做全表兜底、不臆造事件、空态如实说明；轮询只刷列表区，不打断已展开的详情。
*/
(function(){
  var _poll = null;

  function _esc(s){
    return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;')
      .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }
  function _stop(){
    if(_poll){ clearInterval(_poll); _poll = null; }
  }
  function _cls(st){
    if(st==='verified') return 'ok';
    if(st==='expired') return 'r';
    if(st==='promoted') return 'w';
    if(st==='proposed'||st==='sampled') return 'b';
    return 'w';
  }
  function _inFlight(st){
    return ['new','sampled','proposed','promoted'].indexOf(st) >= 0;
  }

  async function loadMyFeedback(){
    var box = document.getElementById('mfb-body');
    if(!box) { _stop(); return; }
    var d;
    try { d = await api('/api/my-feedback'); }
    catch(e){ box.innerHTML = '<div class="note" style="color:var(--red);">反馈加载失败：'+_esc(e&&e.message||e)+'</div>'; _stop(); return; }
    if(d && d.error){ box.innerHTML = '<div class="note" style="color:var(--red);">'+_esc(d.error)+'</div>'; _stop(); return; }
    var head = document.getElementById('mfb-head');
    var items = (d && d.items) || [];
    var labels = (d && d.state_labels) || {};
    if(head) head.textContent = items.length + ' 条 · ' + _esc((d&&d.actor)||'');
    if(d && d.anonymous){
      box.innerHTML = '<div class="note">未登录：登录后才能查看自己的反馈处理进度（不做全表兜底）。</div>';
      _stop(); return;
    }
    if(!items.length){
      box.innerHTML = '<div class="note">你还没有提交过反馈。对助手回答点「👍 有帮助 / 👎 有问题」后，'
        + '这里会出现 feedback#N 编号与处理进度。</div>';
      _stop(); return;
    }
    var counts = {};
    items.forEach(function(x){ counts[x.state||'new'] = (counts[x.state||'new']||0)+1; });
    var sum = Object.keys(counts).map(function(k){
      return '<span class="st '+_cls(k)+'" style="margin-right:8px;">'+_esc(labels[k]||k)+' '+counts[k]+'</span>';
    }).join('');
    var pending = counts['new']||0;
    var banner = pending
      ? '<div class="note" style="margin-bottom:10px;">有 <b>'+pending+'</b> 条反馈仍停在「待采样」。'
        + '当前链路无周期调度器；开启自动触发器后，这里会随状态推进自动更新（每 15 秒刷新）。</div>'
      : '<div class="note" style="margin-bottom:10px;">全部反馈已进入下游链路；状态变化时本页每 15 秒自动刷新。</div>';
    var rows = items.map(function(x){
      var st = x.state||'new';
      return '<tr>'
        + '<td style="font-size:12.5px;white-space:nowrap;">'+_esc(String(x.created_at_local||x.created_at||'').slice(5,16))+'</td>'
        + '<td><b>feedback#'+_esc(x.id)+'</b><div style="font-size:12px;color:var(--mut);">消息 #'+_esc(x.message_id)+'</div></td>'
        + '<td>'+(x.quality==='down'?'👎 有问题':(x.quality==='up'?'👍 有帮助':_esc(x.feedback_type||'评审')))+'</td>'
        + '<td><span class="st '+_cls(st)+'">'+_esc(labels[st]||st)+'</span></td>'
        + '<td style="font-size:12.5px;color:var(--mut);">'+_esc(x.link_kind?(x.link_kind+' #'+x.link_id):'—')+'</td>'
        + '<td><button class="btn sm" onclick="openFeedbackDetail('+Number(x.id)+')">查看进度</button></td>'
        + '</tr>';
    }).join('');
    var list = document.getElementById('mfb-list');
    if(list){
      list.innerHTML = '<div style="margin-bottom:8px;">'+sum+'</div>'+banner
        + '<table class="t"><tr><th>时间</th><th>编号</th><th>信号</th><th>状态</th><th>当前关联</th><th>操作</th></tr>'
        + rows + '</table>';
    }
    var active = items.some(function(x){ return _inFlight(x.state||'new'); });
    if(active && !_poll){
      _poll = setInterval(function(){
        if(!document.getElementById('mfb-body')) { _stop(); return; }
        loadMyFeedback();
      }, 15000);
    } else if(!active && _poll){ _stop(); }
  }

  async function openFeedbackDetail(id){
    var box = document.getElementById('mfb-detail');
    if(!box) return;
    box.innerHTML = '<div class="loading">加载中…</div>';
    var d;
    try { d = await api('/api/my-feedback/'+id); }
    catch(e){ box.innerHTML = '<div class="note" style="color:var(--red);">加载失败</div>'; return; }
    if(!d || d.error || !d.item){
      box.innerHTML = '<div class="note" style="color:var(--red);">'+_esc((d&&d.error)||'反馈不存在或不属于当前用户')+'</div>';
      return;
    }
    var it = d.item, labels = d.state_labels||{}, tl = d.timeline||[];
    var h = '<div class="panel" style="margin-top:14px;"><div class="ph">feedback#'+_esc(it.id)
      + ' <span class="st '+_cls(it.state||'new')+'">'+_esc(it.state_label||labels[it.state]||it.state)+'</span>'
      + '<span style="flex:1"></span><button class="btn sm ghost" onclick="document.getElementById(\'mfb-detail\').innerHTML=\'\'">收起</button>'
      + '</div><div class="pb">';
    h += '<div style="font-size:13.5px;color:var(--mut);margin-bottom:8px;">消息 #'+_esc(it.message_id)
      + ' · 会话 #'+_esc(it.conversation_id||'—')+' · '+_esc(it.quality||it.feedback_type||'')
      + ' · 提交于 '+_esc(it.created_at_local||it.created_at||'')+'</div>';
    h += '<div style="font-size:13.5px;font-weight:500;margin-bottom:6px;">处理时间线（仅列真实发生的事）</div>';
    h += tl.map(function(x){
      return '<div style="display:flex;gap:10px;padding:6px 0;border-bottom:1px solid var(--line);">'
        + '<span style="font-size:12px;color:var(--mut);white-space:nowrap;min-width:120px;">'+_esc(String(x.at||'—').slice(5,16))+'</span>'
        + '<span style="flex:1;"><b style="font-size:13.5px;">'+_esc(x.title)+'</b>'
        + (x.detail?'<div style="font-size:12px;color:var(--mut);">'+_esc(x.detail)+'</div>':'')
        + '</span></div>';
    }).join('');
    if(tl.some(function(x){ return x.kind==='pending'; })){
      h += '<div class="note" style="margin-top:10px;">链路卡点：这条反馈尚未被任何下游消费。'
        + '原因不是「没有被记录」，而是<b>没有周期调度器把采样器叫醒</b>；'
        + '开启自动触发器后状态会推进到「已采样」。</div>';
    }
    h += '</div></div>';
    box.innerHTML = h;
  }

  window.loadMyFeedback = loadMyFeedback;
  window.openFeedbackDetail = openFeedbackDetail;
})();
