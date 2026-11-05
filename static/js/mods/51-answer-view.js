/* 51-answer-view.js — AI 答复正文的「来源标注」呈现层（2026-10-29 建，2026-11-03 精化）
 *
 * 现职责**只有一件**：在正文上方插一行「答复来自：<Agent>」，
 * 回答「LLM 的输出都堆在这里，不清楚是哪个环节给的」。
 *
 * ── 曾经的「限高 + 区域内滚动」已移除（别加回来）───────────────────────────
 * 2026-10-29 曾给正文加 `max-height:60vh; overflow-y:auto` + 一行提示条，
 * 理由是「输出内容多，页面被拉得很长」。这正是本仓栽过两次的那类误判：
 * **把用户的症状（太长）当成自己的方案（限高内滚）**。
 * 2026-11-03 米爸澄清原话：
 *   「每一段 AI 正式内容输出不用滚动条（不用限制在固定的高度内）」
 * ⇒ 正文必须自然撑开、无任何内滚；滚动只由 `#chat-area` 单层承担。
 * ⇒ 与 2026-10-28 的「最终输出结果不要收起来，也不允许收起来」同一方向：
 *   要的是**能一直看全**，不是"能滚着看全"。
 *
 * ⚠️ 本模块的调用方保留（03-chat.js / 13-reports.js 都调 applyAnswerFrame）：
 *    接口不变，只是内部不再做任何高度干预。
 * ⚠️ 必须在**渲染收口之后**调用：finishStream 会用 renderMessage 整块替换 DOM，
 *    流式期间插入的节点会被清掉。幂等靠 `.ans-src` 是否存在判断。
 */
(function(){
  'use strict';

  /* 单条 AI 消息：加来源标注 + 必要时限高。幂等。 */
  function _frameOne(msgEl, meta){
    if(!msgEl || !msgEl.querySelector) return;
    var inner = msgEl.querySelector('.msg-inner');
    var body = msgEl.querySelector('.body');
    if(!inner || !body) return;

    /* ── ① 来源标注 ── */
    if(meta && meta.agent && !inner.querySelector('.ans-src')){
      var src = document.createElement('div');
      src.className = 'ans-src';
      var agent = String(meta.agent);
      var extra = [];
      if(meta.stage) extra.push(String(meta.stage));
      if(meta.hil) extra.push('HIL ' + String(meta.hil).replace(/^L/i, ''));
      src.textContent = '答复来自：' + agent + (extra.length ? '（' + extra.join(' · ') + '）' : '');
      /* 插在正文之前、执行过程之后 —— 让"过程"与"结论"在视觉上仍然分开 */
      body.parentNode.insertBefore(src, body);
    }

    /* ── ② 正文高度：**不干预**（2026-11-03 米爸：「不用限制在固定的高度内」）
       这里刻意什么都不做：不加 class、不设 max-height、不插提示条。
       正文自然撑开，滚动只发生在 #chat-area 单层。
       若有人想恢复限高，先去看本文件头部注释，别只改 CSS —— 两处是配套的。 */
  }

  /**
   * 对一个渲染根（元素或 document）内的所有 AI 消息应用呈现层。
   * @param {Element|Document} root 渲染根；传 document 表示全量扫描
   * @param {Object} meta 可选，**仅当 root 本身是一条 .msg 元素时生效**：{agent, stage, hil}
   *                      （批量扫描时不带 meta —— 历史消息没有当轮的 agent 上下文）
   */
  function applyAnswerFrame(root, meta){
    try{
      if(!root) return;
      /* 单条渲染：root 本身就是一个 .msg 元素 ⇒ 带上 meta */
      if(root.nodeType === 1 && root.classList && root.classList.contains('msg')){
        _frameOne(root, meta || null);
        return;
      }
      /* 批量：不做来源标注（无当轮 agent 上下文），也不做任何高度干预 */
      var nodes = (root.querySelectorAll ? root.querySelectorAll('.msg.ai') : []);
      for(var i = 0; i < nodes.length; i++) _frameOne(nodes[i], null);
    }catch(e){ /* 呈现层失败绝不能影响会话 */ }
  }

  window.applyAnswerFrame = applyAnswerFrame;
})();
