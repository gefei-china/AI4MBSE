/* 51-answer-view.js — AI 答复正文的「来源标注 + 限高」呈现层（2026-10-29）
 *
 * 用户反馈（两个真实问题，同一块区域）：
 *   ① 「LLM的输出可以放在具体执行节点吗，不要都展示在这里，不清楚是什么环节输出的」
 *   ② 「这个区域因为输出内容较多，导致页面被拉的很长」
 *
 * ── 为什么是「标注 + 限高」，而不是「折叠」 ────────────────────────────────
 * `05-markdown.js` 里有一条**用户此前的明确要求**（2026-10-28 记录）：
 *   「最终输出结果不要收起来，也不允许收起来」⇒ 折叠范围严格限定 `role==='user'`。
 * 所以这里**不做折叠**（不隐藏任何内容），只做两件不违反该要求的事：
 *   · 来源标注：正文上方一行「答复来自：<Agent>」，解决"不清楚是什么环节输出的"；
 *   · 限高 + 区域内滚动：正文超过阈值时容器不再无限拉长页面，**内容一字不删**，
 *     用户在该区域内滚动即可看全（不是折叠、不是展开/收起）。
 *
 * ── 为什么不沿用子任务卡的「实时输出」通道 ──────────────────────────────
 * `11-pipeline.js` 的 token 分流已有此能力：带 `key` 的 token 进对应子任务卡
 * （`_procS.subOutput[key]` → `card.liveOutput`），不带 `key` 的才是最终正文。
 * 单 Agent 问答轮**没有子任务**，正文就是交付物本身 ⇒ 它落在正文区是正确行为，
 * 缺的是"这是谁给的"这一行标注，而不是把正文搬走。
 *
 * ⚠️ 幂等：重复调用不会重复插入标注/提示条（靠 data 标记）。
 * ⚠️ 必须在**渲染收口之后**调用：`finishStream` 会用 renderMessage 整块替换 DOM，
 *    流式期间插入的节点会被清掉。
 */
(function(){
  'use strict';

  var CAP_PX = 520;      // 正文超过该高度 → 限高（实测普通答复 < 300px，长答复 1000px+）

  function _px(el){
    try{ return el.getBoundingClientRect().height || 0; }catch(e){ return 0; }
  }

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

    /* ── ② 限高（内容不删，区域内滚动）── */
    if(body.dataset.ansCapDone === '1') return;
    if(_px(body) <= CAP_PX) return;
    body.dataset.ansCapDone = '1';
    body.classList.add('ans-capped');
    /* 底部渐隐提示：告诉用户"下面还有，在这里滚动即可"，避免误以为内容被截断 */
    var hint = document.createElement('div');
    hint.className = 'ans-cap-hint';
    hint.textContent = '内容较长 · 在本区域内滚动可查看全部（未截断）';
    body.parentNode.insertBefore(hint, body);
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
      /* 批量：只做限高，不做来源标注（无当轮上下文） */
      var nodes = (root.querySelectorAll ? root.querySelectorAll('.msg.ai') : []);
      for(var i = 0; i < nodes.length; i++) _frameOne(nodes[i], null);
    }catch(e){ /* 呈现层失败绝不能影响会话 */ }
  }

  window.applyAnswerFrame = applyAnswerFrame;
  window.ANS_CAP_PX = CAP_PX;
})();
