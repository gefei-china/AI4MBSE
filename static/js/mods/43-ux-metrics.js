/* UX 埋点采集（规范 §11 评估体系）—— 2026-10-03
 * 前端行为事件 → 批量上报 /api/ux-metrics；summary 聚合见 repositories/ux_metrics_repo.py。
 *
 * 纪律（遥测三不）：
 * · 不阻塞 —— 采集与上报全部 try/catch 静默，任何异常不影响主链路；
 * · 不重试 —— 上报失败直接丢批（防放大：遥测风暴比丢数据更伤）；
 * · 不打扰 —— 批量 10 条或 4s 定时合并为一次请求，pagehide 时 sendBeacon 兜底。
 *
 * 六指标数据源：
 *   task_done / task_error（handleSSE 收口）→ 完成率、中断率基数
 *   interrupt（stopStream）                → 主动中断率
 *   clarify_shown / clarify_answered / clarify_skipped → 澄清命中率
 *   confirm_shown / confirm_decided        → 内联确认闸门转化（附赠）
 *   scroll_force（reason: user|clarify）   → 抢滚动守护（其余 reason 恒 0，>0 即 §8.4 红线被破坏）
 */
let _uxBuf = [];
let _uxTimer = 0;

function uxTrack(event, detail){
  try{
    _uxBuf.push({event: event,
                 conversation_id: (typeof currentConvId !== 'undefined' && currentConvId) || 0,
                 detail: detail ? JSON.stringify(detail).slice(0, 500) : ''});
    if(_uxBuf.length >= 10) uxFlush();
    else if(!_uxTimer) _uxTimer = setTimeout(uxFlush, 4000);
  }catch(e){}
}

async function uxFlush(){
  if(_uxTimer){ clearTimeout(_uxTimer); _uxTimer = 0; }
  const batch = _uxBuf; _uxBuf = [];
  if(!batch.length) return;
  try{
    if(typeof navigator !== 'undefined' && navigator.sendBeacon){
      // 页面卸载场景 fetch 会被杀 —— Beacon 兜底；后端该端点无权限门，可直投
      const ok = navigator.sendBeacon('/api/ux-metrics',
        new Blob([JSON.stringify({events: batch})], {type: 'application/json'}));
      if(ok) return;
    }
    if(typeof api === 'function'){
      await api('/api/ux-metrics', {method: 'POST', body: JSON.stringify({events: batch})});
    }
  }catch(e){ /* 静默丢批（遥测纪律） */ }
}

if(typeof window !== 'undefined'){
  window.addEventListener('pagehide', ()=>{ if(_uxBuf.length) uxFlush(); });
}
