// ── 设置·AI 上下文管理（st-ctx tab）──
// 2026-09-14：T6 token 预算 + 历史压缩 + 语义拉回（embedding/bigram 双路）配置入口。
// 数据源：GET /api/system/config/schema（core/config.py CONFIG_SCHEMA，含当前值/默认值）；
// 保存：PUT /api/system/config/static → core.config.save_override（写 ~/.workbuddy/mbse_config.json 并即时 reload）。
// 每项配置附「📖 解读」与「⚖️ 调整影响」，字段定义是唯一事实源。

const CTX_CFG_SECTIONS = [
  { id: 'budget', title: '📦 上下文 Token 预算（T6，token 驱动，优先于字符版）',
    hint: 'Prompt 按「System 区 → 检索数据区 → 历史区」分区裁剪防超窗。token 项填 0 = 回退对应字符版配置（旧行为）；留空 = 不修改。' },
  { id: 'history', title: '📜 历史压缩与话题注入',
    hint: '会话历史按话题分组注入：当前话题原文（占历史预算 50%）→ 语义拉回（至 75%）→ 其他话题 LLM 摘要（剩余）。' },
  { id: 'semantic', title: '🔍 语义出口（embedding / bigram 双路）',
    hint: '语义拉回与当前话题判定共用 embedding 接口（llm_providers 中 model_type=embedding 的 provider）；API 失败自动降级 bigram 词面匹配。两路余弦量纲不同，阈值独立配置。' },
  { id: 'rag', title: '🔎 检索与 RAG（GraphRAG 路由 / 混合检索 / 重排）',
    hint: '知识库检索链路：图谱实体链接优先 → 图谱置信度 < 路由阈值时走向量混合检索（BM25+向量 RRF 融合 + HyDE 补召）→ LLM 相关性重排。保存后下一次提问即生效，无需重启。' },
];

const CTX_CFG_FIELDS = [
  // ── 预算区 ──
  // 注：原 'context.budget_system_tokens' 项已于 2026-09-19 移除 —— 该配置在 core/config.py
  //     里**只有声明、没有消费点**（_apply_context_budget 只裁检索区与历史区），且实测
  //     system 区非检索部分已达 5,991 token > 其 4,000 上限，物理上无法生效，留着只会误导调参。
  //     依据：docs/SysML-v2-生成端硬约束与向量化链路修复-实测报告-20260919.md §5-④
  { sec:'budget', key:'context.budget_retrieval_tokens', type:'int', label:'检索数据区预算 (token)',
    desc:'知识库检索结果注入 prompt 的 token 上限（超限从尾部裁剪，保留最相关头部）。',
    impact:'调大：检索依据更全、引用更可信；调小：只保留最相关头部（对齐 Lost-in-the-Middle：重要内容放头部召回率更高）。' },
  { sec:'budget', key:'context.budget_history_tokens', type:'int', label:'历史区预算 (token)',
    desc:'会话历史注入总预算：当前话题原文占 50%，语义拉回片段用至 75%，剩余给其他话题摘要。',
    impact:'调大：长对话更连贯、旧话题细节保留更多；调小：更省 token，但仅剩当前话题最近几条原文（旧话题靠摘要兜底）。' },
  // ── 历史压缩区 ──
  { sec:'history', key:'context.history_msg_max_chars', type:'int', label:'单条消息截断 (字符)',
    desc:'参与注入/摘要的单条历史消息截断上限；超长消息（如粘贴整篇文档）只取前缀。',
    impact:'调大：粘贴长文时上下文更完整；调小：防止单条长消息挤占整个历史预算。' },
  { sec:'history', key:'context.history_summary_chars', type:'int', label:'分话题摘要上限 (字符)',
    desc:'其他话题由 LLM 压缩生成的摘要长度上限（保留决策/实体/约束/待办，丢客套话）。',
    impact:'调大：摘要细节更全；调小：摘要更精炼，但可能丢细节。摘要按话题缓存，段变化才重算，不影响响应速度。' },
  { sec:'history', key:'context.topic_current_max_msgs', type:'int', label:'当前话题原文条数',
    desc:'当前话题逐字注入的最大消息数（倒序填充至历史预算的 50%）。',
    impact:'调大：当前讨论的上下文原文更完整、指代（"刚才那个"）更可靠；调小：省预算给历史拉回与摘要。' },
  { sec:'history', key:'context.topic_boundary_keep', type:'int', label:'话题切换边界保留 (条)',
    desc:'刚切换话题且新话题很短时，补注入上一话题末尾 N 条原文，保证切换语义连续。',
    impact:'调大：话题切换时上下文衔接更自然；调小：切换更"干净"，但可能丢失上一话题的关键收尾。' },
  { sec:'history', key:'context.topic_retrieve_topk', type:'int', label:'语义拉回条数 (top-k)',
    desc:'切回旧话题/回指历史时，从全部历史中语义检索并注入原文的片段条数。',
    impact:'调大："之前说的XX"类回指命中更多；调小：省预算，但可能漏拉关键历史。' },
  { sec:'history', key:'context.model_context_chars', type:'int', label:'建模工作记忆 (字符)',
    desc:'MBSE 特有：注入当前模型状态（活跃实体/视图/最近变更/会话产物）的字符上限，来自模型库实时查询。',
    impact:'调大：AI 对模型现状感知更全（实体多时更明显）；调小：省预算但可能漏掉已建元素导致重复建模。' },
  // ── 语义出口区 ──
  { sec:'semantic', key:'embedding.enabled', type:'bool', label:'语义出口总开关',
    desc:'开启后语义拉回/当前话题判定走真 embedding（需 llm_providers 配置向量模型且 api_key 非空）；关闭强制 bigram 词面匹配。',
    impact:'开启：语义理解强（"水冷板"能匹配"液冷板"类近义表述），每次请求多一次 embedding API 调用（有缓存）；关闭：零外部依赖、Mock/离线确定性回归可用，但只能词面匹配。' },
  { sec:'semantic', key:'context.topic_retrieve_threshold', type:'float', label:'拉回阈值 (bigram 路)',
    desc:'bigram 降级路的语义拉回最低相似度（含话题域加权后），低于不注入。',
    impact:'调高：拉回更少更准；调低：召回更多但可能混入弱相关内容。仅 bigram 路生效（embedding 关闭或 API 失败时）。' },
  { sec:'semantic', key:'context.topic_retrieve_threshold_dense', type:'float', label:'拉回阈值 (embedding 路)',
    desc:'真 embedding 路的语义拉回最低余弦。量纲与 bigram 完全不同（BGE 类中文模型相关文本通常 0.5+，无关 0.2 以下），勿与 bigram 阈值混用。',
    impact:'调高：拉回更少更准；调低：召回更多。建议用真实会话验证后微调（默认 0.35 为中等偏严）。' },
  { sec:'semantic', key:'context.topic_group_match_dense', type:'float', label:'话题组匹配阈值 (embedding 路)',
    desc:'判定"当前输入属于哪个话题组"的最低余弦（当前输入 vs 各话题段代表文本）。低于阈值视为新话题。',
    impact:'调低：更容易命中旧话题组（切回旧话题时原文注入更准）；调高：更容易判为新话题（历史靠语义拉回兜底）。' },
  // ── 检索与 RAG 区（P1-4，2026-09-21）──
  { sec:'rag', key:'rag.route_threshold', type:'float', label:'检索路由阈值',
    desc:'图谱置信度 ≥ 该阈值时走纯图路由（直接用图谱命中，跳过向量检索）；低于则向量混合检索补召。图谱置信度 = 实体覆盖度×权重 + 关系连接度×权重 + 类型完整度×权重。',
    impact:'调低：更容易走纯图路由（响应快，但图谱弱命中也会独占答案依据）；调高：更多提问走向量检索（依据更全，但延迟略增）。' },
  { sec:'rag', key:'rag.top_k', type:'int', label:'混合检索条数 (top-k)',
    desc:'向量混合检索（BM25+向量 RRF 融合）返回的 chunk 条数。消费侧只取前 3 条注入 prompt，建议 3~6。',
    impact:'调大：候选更多、LLM 重排有更多选择，但检索与重排延迟增加；调小：更快，但可能漏掉关键段落。' },
  { sec:'rag', key:'rag.fallback_top_k', type:'int', label:'兜底检索条数',
    desc:'混合检索异常（如 embedding 服务不可用）时，降级 search_chunks 兜底检索的条数。',
    impact:'一般保持默认 5；仅在降级路径检索质量不满意时调整。' },
  { sec:'rag', key:'rag.rrf_k', type:'int', label:'RRF 融合常数 (k)',
    desc:'Reciprocal Rank Fusion 公式 Σ1/(k+rank) 中的 k，调和 BM25 排名与向量排名的贡献。行业常用 60。',
    impact:'调小：头部排名（rank#1、#2）优势放大；调大：排名差异被抹平、两路更"平权"。一般不动。' },
  { sec:'rag', key:'rag.hyde_enabled', type:'bool', label:'HyDE 补召开关',
    desc:'Reverse HyDE：用 chunk 的假设问题（hyde_questions/hyde_embedding）与查询匹配，用户措辞≠文档措辞时补召回。',
    impact:'开启：措辞差异大的提问召回更好（每次多一次向量计算）；关闭：省延迟，召回退回纯 RRF。' },
  { sec:'rag', key:'rag.hyde_weight', type:'float', label:'HyDE 补充分权重',
    desc:'HyDE 相似分叠加进融合分的权重（0~0.2；默认 0.05 = 弱辅助，只起打破平局作用）。',
    impact:'调大：HyDE 命中对排序影响更强；设 0 等效关闭 HyDE 补充分（保留开关语义请直接用 HyDE 开关）。' },
  { sec:'rag', key:'rag.w_coverage', type:'float', label:'图谱置信权重·实体覆盖',
    desc:'图谱置信度三因子之一：命中实体覆盖度 min(命中数/5,1) 的权重（默认 0.50）。',
    impact:'调大：命中实体数量对"是否走纯图路由"的影响更强。' },
  { sec:'rag', key:'rag.w_relations', type:'float', label:'图谱置信权重·关系连接',
    desc:'图谱置信度三因子之一：命中实体的关系连接度 min(关系数/3,1) 的权重（默认 0.30）。',
    impact:'调大：带丰富关系的强命中更可信（子图完整的命中更容易独占答案依据）。' },
  { sec:'rag', key:'rag.w_typing', type:'float', label:'图谱置信权重·类型完整',
    desc:'图谱置信度三因子之一：命中实体类型标注完整度（有类型且类型≠名称）的权重（默认 0.20）。',
    impact:'调大：类型标注规范的图谱更容易达到路由阈值。三因子权重和建议保持合计 ≈1.0。' },
  { sec:'rag', key:'rag.confidence_high', type:'float', label:'命中置信·高 分界',
    desc:'检索命中置信等级（高/中/低，展示在引用与 recall_reason 里）的「高」分界，按真向量相似度口径（默认 0.70）。',
    impact:'仅影响展示与可解释性，不影响检索排序。与 embedding 模型相关（不同模型量纲不同）。' },
  { sec:'rag', key:'rag.confidence_mid', type:'float', label:'命中置信·中 分界',
    desc:'置信等级「中」分界，低于则为「低」（默认 0.45）。',
    impact:'同上，仅影响展示。' },
  { sec:'rag', key:'rag.rerank_enabled', type:'bool', label:'LLM 重排开关',
    desc:'RRF 候选 → LLM 相关性打分（0-10）重排，取前 top-k。LLM 不可用/超时/解析失败自动静默回退原排序。',
    impact:'开启：最终注入 prompt 的段落更相关（每次检索多一次 LLM 调用，延迟 +2~5s）；关闭：纯 RRF 排序直接用。' },
  { sec:'rag', key:'rag.rerank_max_candidates', type:'int', label:'重排候选上限',
    desc:'送 LLM 打分的候选条数上限（其余按原排序保底置后）。默认 8。',
    impact:'调大：重排覆盖更全但 prompt 更长、延迟更高；调小：更快但重排可能漏掉真命中。' },
];

async function loadCtxConfig() {
  const body = document.getElementById('ctx-cfg-body');
  if (!body) return;
  let schema = {};
  try {
    const d = await api('/api/system/config/schema');
    schema = d.schema || {};
  } catch (e) {
    body.innerHTML = '<div class="loading">加载失败：' + (e.message || e) + '</div>';
    return;
  }
  const secOf = k => k.split('.')[0], keyOf = k => k.split('.')[1];
  body.innerHTML = CTX_CFG_SECTIONS.map(s => `
    <div class="panel" style="margin-bottom:12px;">
      <div class="ph">${s.title}</div>
      <div class="pb">
        ${s.hint ? `<div style="font-size:11px;color:var(--mut);margin-bottom:8px;">💡 ${s.hint}</div>` : ''}
        ${CTX_CFG_FIELDS.filter(f => f.sec === s.id).map(f => {
          const meta = ((schema[secOf(f.key)] || {})[keyOf(f.key)]) || {};
          const cur = meta.current, def = meta.default;
          let input;
          if (f.type === 'bool') {
            const on = (cur === true || cur === 'true' || cur === 1);
            input = `<select id="ctx-${f.key}" style="border:1px solid var(--line);border-radius:6px;padding:4px 8px;font-size:12px;width:100px;">
              <option value="1"${on ? ' selected' : ''}>开启</option><option value="0"${on ? '' : ' selected'}>关闭</option></select>`;
          } else {
            input = `<input id="ctx-${f.key}" type="number" step="${f.type === 'float' ? '0.05' : '1'}" value="${cur ?? ''}"
              placeholder="留空=不改" style="border:1px solid var(--line);border-radius:6px;padding:4px 8px;font-size:12px;width:100px;">`;
          }
          return `<div style="display:grid;grid-template-columns:200px 110px 1fr;gap:12px;padding:8px 0;border-bottom:1px dashed var(--line);align-items:start;">
            <div style="font-size:12px;font-weight:600;">${f.label}
              <div style="font-weight:400;font-size:10.5px;color:var(--mut);font-family:monospace;">${f.key}</div></div>
            <div>${input}
              <div style="font-size:10px;color:var(--mut);margin-top:3px;">默认 ${def ?? '—'}</div></div>
            <div style="font-size:11px;line-height:1.55;color:var(--mut);">
              <div>📖 ${f.desc}</div>
              <div style="color:var(--amb);margin-top:2px;">⚖️ ${f.impact}</div></div>
          </div>`;
        }).join('')}
      </div>
    </div>`).join('');
}

async function saveCtxConfig() {
  const updates = {};
  for (const f of CTX_CFG_FIELDS) {
    const el = document.getElementById('ctx-' + f.key);
    if (!el) continue;
    if (f.type === 'bool') { updates[f.key] = el.value === '1'; continue; }
    const v = String(el.value).trim();
    if (v === '') continue;              // 留空 = 不修改该项
    updates[f.key] = v;                  // int/float 由后端 _coerce 校验（填 0 = token 项回退字符版）
  }
  if (!Object.keys(updates).length) { toast('没有修改的配置项'); return; }
  try {
    const r = await api('/api/system/config/static', { method: 'PUT', body: JSON.stringify({ updates }) });
    if (r && r.ok === false) throw new Error(r.error || '保存失败');
    toast('✅ AI 上下文配置已保存并即时生效（' + Object.keys(updates).length + ' 项）');
    loadCtxConfig();
  } catch (e) {
    toast('保存失败：' + (e.message || e));
  }
}
