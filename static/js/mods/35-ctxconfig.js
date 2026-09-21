// ── 设置·AI 上下文管理（st-ctx tab）──
// 2026-09-14：T6 token 预算 + 历史压缩 + 语义拉回（embedding/bigram 双路）配置入口。
// 2026-09-21 P1-4b：**按耦合度重组为 7 个参数簇**（原 4 个松散分区），簇内项强相关、
//   必须同屏调（改一个必须看另一个），并为每簇提供"耦合关系可视化 + 约束校验"。
//   依据：docs/AI上下文配置项-依据与行业对标-20260921.md（含各项行业对标值与理想区间）。
// 数据源：GET /api/system/config/schema（core/config.py CONFIG_SCHEMA，含当前值/默认值）；
// 保存：PUT /api/system/config/static → core.config.save_override（写 ~/.workbuddy/mbse_config.json 并即时 reload）。
// 每项配置附「📖 解读」与「⚖️ 调整影响」，字段定义是唯一事实源。

// ══ 参数簇定义 ══════════════════════════════════════════════════════════════
// viz：该簇顶部渲染的耦合关系视图（funnel/weights/order/budget/split/dual）
// checks：该簇的约束校验器 key
const CTX_CFG_CLUSTERS = [
  {
    id: 'funnel', title: '🎯 检索漏斗分层（四层逐级收窄）', viz: 'funnel', checks: ['ladder'],
    hint: '🔗 这四项是<b>同一条漏斗</b>的四个截面，改任何一个都会改变其余层的实际容量，必须一起看：'
      + '每路召回 → RRF 融合池 → 送 LLM 重排 → 注入 prompt。行业标准是「广召回 20–50 → 重排 20–50 → 窄注入 3–8」。',
  },
  {
    id: 'route', title: '🧭 图谱路由判定（三因子加权 + 阈值）', viz: 'weights', checks: ['weights'],
    hint: '🔗 三个权重先合成「图谱置信度」，再与路由阈值比较决定走向：≥阈值走纯图路由（跳过向量检索），'
      + '否则向量混合检索补召。<b>权重合计必须 ≈1.0</b>，否则置信度量纲失衡、阈值失去意义。',
  },
  {
    id: 'conf', title: '🏷️ 命中置信分级（展示用序关系）', viz: 'order', checks: ['order'],
    hint: '🔗 两项定义「高/中/低」两处分界，<b>必须满足 高 > 中</b>。仅影响引用与 recall_reason 的展示，不改检索排序。',
  },
  {
    id: 'fuse', title: '🔀 融合公式与补召', viz: 'none', checks: ['hyde'],
    hint: '🔗 RRF 是融合主公式，HyDE 是叠加其上的补充分。<b>补充分权重设 0 与关闭补召开关语义重叠</b>，'
      + '两者只留一个表达即可（建议用开关）。',
  },
  {
    id: 'budget', title: '📦 上下文 Token 预算（两套口径）', viz: 'budget', checks: ['budget'],
    hint: '🔗 检索区与历史区<b>共享同一个上下文窗口</b>，此消彼长。此处提供两套口径：'
      + '绝对值（token 数）与占比制（窗口百分比）。<b>占比 > 0 时覆盖绝对值</b>（换模型自适应），占比 = 0 时用绝对值。',
  },
  {
    id: 'split', title: '📜 历史注入内分配（瓜分历史区预算）', viz: 'split', checks: [],
    hint: '🔗 这六项<b>共同瓜分历史区预算</b>，注入顺序与占比是固定结构：当前话题原文（约 50%）→ 语义拉回片段（用至 75%）'
      + '→ 其他话题摘要（剩余）。单条消息截断与建模工作记忆是前置硬上限。',
  },
  {
    id: 'dual', title: '🔍 语义判定双路阈值（同量纲对照）', viz: 'dual', checks: ['dual'],
    hint: '🔗 embedding 与 bigram 两路<b>量纲完全不同</b>（BGE 类中文模型相关文本 0.5+、无关 0.2 以下；bigram 量纲小得多），'
      + '所以同一判定要两套阈值。下表把「同一判定的两条路」并排，便于对照调。',
  },
  {
    id: 'vision', title: '🖼 会话图片附件（视觉通道）', viz: 'none', checks: [],
    hint: '🔗 这是「AI 建模」里传<b>架构图 / 连线图 / 截图</b>的通路：图片以多模态内容随消息一起送给模型，'
      + '由模型自己看图（<b>而不是 OCR</b> —— 架构图的语义在拓扑里，OCR 只给出几个孤立词，对建模几乎无价值）。'
      + '<b>需两处同时满足才生效</b>：① 本组总开关开启；② 会话所选的模型已在「模型配置」里勾选「支持图片理解」。'
      + '任一不满足都会在响应里<b>明确写明原因</b>（不静默丢弃）。',
  },
  {
    id: 'ocr', title: '📄 文档解析 OCR（扫描件 / 图片兜底 + 结果质量门禁）', viz: 'none', checks: [],
    hint: '🔗 这是<b>文档库</b>里传扫描版 PDF / 图片的通路（与上一组的"会话看图"是两条不同的路：'
      + '这里要的是<b>可检索的文字</b>，会话那边要的是<b>模型理解拓扑</b>）。'
      + '核心策略是「<b>文本层优先 + 稀疏页补 OCR</b>」，<b>绝不可无差别全量 OCR</b>（慢 50~200 倍，且版面顺序反而更差）。'
      + '⑤⑥ 两项是<b>结果质量门禁</b>，双条件同时满足才入库 —— 防止低质截图被 OCR 成几百字错字后混进向量库、'
      + '再被检索召回去误导回答（2026-09-21 实测事故）。',
  },
];

// ══ 字段定义（按簇归属）══════════════════════════════════════════════════════
const CTX_CFG_FIELDS = [
  // ── 簇 1：检索漏斗分层 ──
  { cluster:'funnel', sec:'rag', key:'rag.recall_k', type:'int', label:'① 每路召回宽度 (recall_k)',
    desc:'BM25 与向量<b>各自</b>召回的条数，两者都进 RRF 融合。0 = 回落旧行为 top_k×2。',
    impact:'调大：融合池候选更全、RRF 排序更稳（只增检索计算，不增 LLM 调用）；调小：更快，但融合可能缺好候选。行业下界 20。' },
  { cluster:'funnel', sec:'rag', key:'rag.top_k', type:'int', label:'② RRF 融合池 (top_k)',
    desc:'RRF 融合后保留的候选数，即<b>送进 LLM 重排的池子</b>。',
    impact:'必须 ≥ 重排窗口，否则重排无选择空间（这正是 09-21 修掉的倒挂）。调大：重排有得挑、注入更准；调小：重排退化为摆设。' },
  { cluster:'funnel', sec:'rag', key:'rag.rerank_enabled', type:'bool', label:'③ 启用 LLM 重排',
    desc:'融合池 → LLM 相关性打分（0-10）重排。LLM 不可用/超时/解析失败自动静默回退原排序。',
    impact:'开启：注入段落更相关（每次检索多一次 LLM 调用，延迟 +2~5s）；关闭：纯 RRF 排序，下方重排窗口项失去意义。' },
  { cluster:'funnel', sec:'rag', key:'rag.rerank_max_candidates', type:'int', label:'③ 重排窗口上限',
    desc:'送 LLM 打分的候选条数上限（其余按原排序排在后面）。0 = 全部候选。',
    impact:'应 ≤ 融合池 top_k，否则上限形同虚设（这是此前的倒挂缺陷）。行业甜点区 20–50，本工程按注入数×2~3 取值即可。' },
  { cluster:'funnel', sec:'rag', key:'rag.inject_k', type:'int', label:'④ 注入 prompt 条数',
    desc:'最终写入提示词【来源n】的片段条数，也是前端引用编号 [n] 的上限。',
    impact:'调大：依据更全但 prompt 更长（对齐 Lost-in-the-Middle：关键内容宜少而精、放头部）；调小：更聚焦。行业常见 3–5。' },
  { cluster:'funnel', sec:'rag', key:'rag.fallback_top_k', type:'int', label:'降级路径条数',
    desc:'混合检索异常（如 embedding 服务不可用）时，降级 search_chunks 兜底的条数。',
    impact:'独立于上面的漏斗，一般保持 5；仅在降级路径检索质量不满意时调整。' },

  // ── 簇 2：图谱路由判定 ──
  { cluster:'route', sec:'rag', key:'rag.route_threshold', type:'float', label:'路由阈值',
    desc:'图谱置信度 ≥ 该阈值 → 纯图路由（直接用图谱命中，跳过向量检索）；低于则向量混合检索补召。',
    impact:'调低：更容易走纯图路由（响应快，但图谱弱命中也会独占答案依据）；调高：更多提问走向量检索（依据更全，延迟略增）。' },
  { cluster:'route', sec:'rag', key:'rag.w_coverage', type:'float', label:'权重·实体覆盖度',
    desc:'三因子之一：命中实体数 min(命中数/5, 1) 的权重。',
    impact:'调大：命中实体数量对路由判定的影响更强（多实体命中更容易独占依据）。' },
  { cluster:'route', sec:'rag', key:'rag.w_relations', type:'float', label:'权重·关系连接度',
    desc:'三因子之一：命中实体的关系连接数 min(关系数/3, 1) 的权重。',
    impact:'调大：子图完整的强命中更可信（带丰富关系的命中更容易独占答案依据）。' },
  { cluster:'route', sec:'rag', key:'rag.w_typing', type:'float', label:'权重·类型完整度',
    desc:'三因子之一：命中实体中有类型标注（且类型≠名称）的占比权重。',
    impact:'调大：类型标注规范的图谱更容易达到路由阈值。三因子权重合计应保持 ≈1.0。' },

  // ── 簇 3：命中置信分级 ──
  { cluster:'conf', sec:'rag', key:'rag.confidence_high', type:'float', label:'「高」分界',
    desc:'引用上显示「置信 高」的最低真向量相似度（0.70 与中文 embedding 经验表「明显相关」档起界吻合）。',
    impact:'仅影响展示口径，不影响检索排序。换 embedding 模型后量纲变了才需重标。' },
  { cluster:'conf', sec:'rag', key:'rag.confidence_mid', type:'float', label:'「中」分界',
    desc:'低于此值显示「置信 低」。当前 0.45 落在经验表「关联很弱」档内，可接受。',
    impact:'同上，仅影响展示。必须小于「高」分界。' },

  // ── 簇 4：融合公式与补召 ──
  { cluster:'fuse', sec:'rag', key:'rag.rrf_k', type:'int', label:'RRF 融合常数 (k)',
    desc:'Reciprocal Rank Fusion 公式 Σ1/(k+rank) 中的 k，调和 BM25 排名与向量排名的贡献。',
    impact:'调小：头部排名（rank#1、#2）优势放大；调大：排名差异被抹平、两路更"平权"。<b>行业通用 60，一般不动</b>。' },
  { cluster:'fuse', sec:'rag', key:'rag.hyde_enabled', type:'bool', label:'HyDE 补召开关',
    desc:'用 chunk 的假设问题（hyde_questions/hyde_embedding）与查询匹配，用户措辞≠文档措辞时补召回。',
    impact:'⚠️ 文献提示：EACL 2024 实测「查询扩展收益与检索器强度<b>负相关</b>」，强检索器上可能加噪；'
      + '本工程索引期 HyDE 使向量化请求数翻倍。<b>建议做 A/B 后再定，或改条件触发</b>。' },
  { cluster:'fuse', sec:'rag', key:'rag.hyde_weight', type:'float', label:'HyDE 补充分权重',
    desc:'HyDE 相似分叠加进融合分的权重（0~0.2）。默认 0.05 = 弱辅助，只起打破平局作用。',
    impact:'调大：HyDE 命中对排序影响更强；<b>设 0 等效关闭补充分</b>（此时建议直接用上方开关表达，避免两处语义重叠）。' },

  // ── 簇 5：上下文 Token 预算 ──
  { cluster:'budget', sec:'context', key:'context.budget_window_tokens', type:'int', label:'窗口基准 (token)',
    desc:'占比制的分母：当前 LLM provider 的上下文窗口大小（换模型时同步改）。',
    impact:'只影响占比制的计算结果。当前 provider 实测窗口 65536。' },
  { cluster:'budget', sec:'context', key:'context.budget_retrieval_tokens', type:'int', label:'检索区预算 (绝对值)',
    desc:'知识库检索结果注入 prompt 的 token 上限（超限从尾部裁剪，保留最相关头部）。占比为 0 时生效。',
    impact:'调大：检索依据更全；调小：只保留最相关头部。实测检索段实际用量约 458 token，当前值留有余量。' },
  { cluster:'budget', sec:'context', key:'context.budget_retrieval_ratio', type:'float', label:'检索区占比',
    desc:'检索区预算 = 窗口基准 × 本比例。<b>>0 时覆盖左侧绝对值</b>；0 = 关闭占比制。',
    impact:'换成大窗口模型时不用手改绝对值（行业做法：Codex 按窗口 50% 设压缩阈值）。建议 0.05~0.10。' },
  { cluster:'budget', sec:'context', key:'context.budget_history_tokens', type:'int', label:'历史区预算 (绝对值)',
    desc:'会话历史注入总预算：当前话题原文占 50%，语义拉回片段用至 75%，剩余给其他话题摘要。占比为 0 时生效。',
    impact:'调大：长对话更连贯、旧话题细节更多；调小：更省 token，旧话题靠摘要兜底。' },
  { cluster:'budget', sec:'context', key:'context.budget_history_ratio', type:'float', label:'历史区占比',
    desc:'历史区预算 = 窗口基准 × 本比例。<b>>0 时覆盖左侧绝对值</b>；0 = 关闭占比制。',
    impact:'同上，适配换模型场景。建议 0.03~0.08。' },

  // ── 簇 6：历史注入内分配 ──
  { cluster:'split', sec:'context', key:'context.topic_current_max_msgs', type:'int', label:'当前话题原文条数',
    desc:'当前话题逐字注入的最大消息数（倒序填充至历史预算的 50%）。',
    impact:'调大：当前讨论原文更完整、指代（"刚才那个"）更可靠；调小：省预算给语义拉回与摘要。' },
  { cluster:'split', sec:'context', key:'context.topic_boundary_keep', type:'int', label:'话题切换边界保留',
    desc:'刚切话题且新话题很短时，补注入上一话题末尾 N 条原文，保证切换语义连续。',
    impact:'调大：切换更自然；调小：更"干净"，但可能丢上一话题的关键收尾。' },
  { cluster:'split', sec:'context', key:'context.topic_retrieve_topk', type:'int', label:'语义拉回条数',
    desc:'切回旧话题/回指历史时，从全部历史中语义检索并注入原文的片段条数（用至历史预算的 75%）。',
    impact:'调大："之前说的XX"类回指命中更多；调小：省预算，但可能漏拉关键历史。' },
  { cluster:'split', sec:'context', key:'context.history_summary_chars', type:'int', label:'分话题摘要上限 (字符)',
    desc:'其他话题由 LLM 压缩生成的摘要长度上限（保留决策/实体/约束/待办，丢客套话）。',
    impact:'调大：摘要细节更全；调小：更精炼但可能丢细节。摘要按话题缓存、段变化才重算，不影响响应速度。' },
  { cluster:'split', sec:'context', key:'context.history_msg_max_chars', type:'int', label:'单条消息截断 (字符)',
    desc:'参与注入/摘要的单条历史消息截断上限；超长消息（如粘贴整篇文档）只取前缀。',
    impact:'调大：粘贴长文时上下文更完整；调小：防单条长消息挤占整个历史预算。' },
  { cluster:'split', sec:'context', key:'context.model_context_chars', type:'int', label:'建模工作记忆 (字符)',
    desc:'MBSE 特有：注入当前模型状态（活跃实体/视图/最近变更/会话产物）的字符上限，来自模型库实时查询。',
    impact:'调大：AI 对模型现状感知更全；调小：省预算但可能漏掉已建元素导致重复建模。' },

  // ── 簇 7：语义判定双路阈值 ──
  { cluster:'dual', sec:'embedding', key:'embedding.enabled', type:'bool', label:'语义出口总开关',
    desc:'开启后语义判定走真 embedding（需 llm_providers 配向量模型且 api_key 非空）；关闭强制 bigram 词面匹配。',
    impact:'开启：语义理解强（"水冷板"能匹配"液冷板"），每次请求多一次 embedding API 调用（有缓存）；'
      + '关闭：零外部依赖、离线确定性回归可用，但只能词面匹配 —— 此时下方 dense 列全部失效。' },
  { cluster:'dual', sec:'context', key:'context.topic_sim_threshold', type:'float', label:'话题切分·bigram 路',
    desc:'判「相邻用户消息是否仍属同一话题」的相似度阈值，低于则开新话题段（决定话题分段粒度）。'
      + '⚠️ 这是 <b>bigram 路独有项</b>——话题组匹配与语义拉回才有双路，本项没有 dense 版本。',
    impact:'调高：更容易开新话题（话题段更碎、语义拉回范围更小）；调低：更容易并入当前话题（历史更连贯但可能混入旧题内容）。' },
  { cluster:'dual', sec:'context', key:'context.topic_group_match_dense', type:'float', label:'话题组匹配·dense 路',
    desc:'embedding 路判定「当前输入属于哪个话题组」的最低余弦（决定切回旧话题时能否命中旧组），低于则视为新话题。',
    impact:'✅ 2026-09-21 已标定修订：原 0.30 实测<b>判定通过率 100%</b>（等于不设限）；'
      + '分位等价映射值 0.6023 → 按行业经验表「有一定关联」档取<b>下沿 0.50</b>。' },
  { cluster:'dual', sec:'context', key:'context.topic_retrieve_threshold', type:'float', label:'语义拉回·bigram 路',
    desc:'bigram 降级路的语义拉回最低相似度（含话题域加权后），低于不注入。',
    impact:'调高：拉回更少更准；调低：召回更多但可能混入弱相关内容。仅 bigram 路生效。' },
  { cluster:'dual', sec:'context', key:'context.topic_retrieve_threshold_dense', type:'float', label:'语义拉回·dense 路',
    desc:'embedding 路的语义拉回最低余弦。中文 embedding 相关文本通常 0.5+、无关 0.2 以下。',
    impact:'✅ 2026-09-21 已标定修订：原 0.35 实测<b>通过率 99.3%</b>（≈形同虚设，几乎每次都拉满 top-k）；'
      + '等价映射值 0.6022（下界，bigram 路话题域加权未复现）→ 取下沿 0.50。' },
  { cluster:'dual', sec:'context', key:'context.semantic_fallback_gate_dense', type:'float', label:'流程补召门·dense 路',
    desc:'流程匹配语义补召的触发门（dense 路）。✅ <b>已用分位等价映射标定</b>：'
      + '实测 bigram 0.5 的等价 dense 分位 = 0.7855，取 0.79。',
    impact:'这是本组<b>唯一有标定依据</b>的 dense 阈值，可作其余两项的标定参照锚。一般不动。' },

  // ── 簇 8：会话图片附件（视觉通道）2026-09-21 新增 ──
  { cluster:'vision', sec:'vision', key:'vision.enabled', type:'bool', label:'视觉通道总开关',
    desc:'开启后，「AI 建模」会话里上传的图片会作为<b>多模态内容</b>随消息一起送给模型（而非仅内联展示）。',
    impact:'关 = 与改动前行为逐字节等价（图片只展示、模型看不见）。<b>开启前必须先确认</b>所选模型已在「模型配置」里勾选「支持图片理解」，否则图片不会被注入。' },
  { cluster:'vision', sec:'vision', key:'vision.max_images', type:'int', label:'单次最多注入图片数',
    desc:'一次请求里最多带几张图（超出部分跳过并留痕）。',
    impact:'调大：可一次给多张图，但视觉 token 线性增长、挤占上下文预算；调小：更省，可能漏看部分图。默认 2。' },
  { cluster:'vision', sec:'vision', key:'vision.max_side', type:'int', label:'图片长边像素上限',
    desc:'超过则等比缩小后再编码。<b>该值直接决定视觉 token 量</b>（约 长×宽÷750）。',
    impact:'调大：小字/细线更清楚，token 更多；调小：省 token，但架构图上的标注文字可能糊掉。默认 1280。' },
  { cluster:'vision', sec:'vision', key:'vision.max_bytes', type:'int', label:'单图字节上限',
    desc:'重编码后的字节上限，超过则该图跳过并留痕（不阻断其余图）。',
    impact:'一般无需调整；上游对 base64 请求体有硬限制时调小。默认 4MB（4×1024×1024）。' },

  // ── 簇：文档解析 OCR（扫描件 / 图片兜底 + 结果质量门禁）────────────────────
  // ⚠️ 纪律（2026-09-21 补）：后端注册 CONFIG_SCHEMA **不等于界面可见** ——
  // 字段清单是前端硬编码的，只改后端会让界面一片空白（本组此前正是漏了这一处，
  // 导致「OCR 总开关」这个故障回退开关在界面上根本改不了）。
  { cluster:'ocr', sec:'extract', key:'extract.ocr_enabled', type:'bool', label:'① OCR 兜底总开关',
    desc:'对<b>扫描版 PDF（无文本层）</b>与<b>图片</b>启用离线 OCR（RapidOCR / ONNX，模型内置、零网络）。',
    impact:'关闭 = 完全回到接入 OCR 前的行为（图片与扫描件判"解析失败"并给出原因），用于 A/B 对比与故障回退。默认开。' },
  { cluster:'ocr', sec:'extract', key:'extract.ocr_min_chars_per_page', type:'int', label:'② 稀疏页判定阈值（字/页）',
    desc:'某页 PDF 文本层字数<b>低于</b>此值即视为"稀疏页"，该页才补 OCR；其余页直接用文本层。',
    impact:'★ 这是"要不要付 OCR 成本"的决定性判据：文本层 0.01~0.06 s/页，OCR 1.4~4.7 s/页（慢 50~200 倍）。'
      + '调大：更多页走 OCR（更慢、可能引入识别误差）；调小：省时间，但图内文字可能漏掉。默认 100。' },
  { cluster:'ocr', sec:'extract', key:'extract.ocr_max_pages', type:'int', label:'③ 单次入库 OCR 页数上限',
    desc:'稀疏页超过此数时，只 OCR 前 N 页（在解析详情里留痕说明丢弃了哪些页）。0 = 不限。',
    impact:'防超大扫描件拖垮入库（实测 691 页规范全量 OCR 要 30~50 分钟）。默认 50。' },
  { cluster:'ocr', sec:'extract', key:'extract.ocr_render_scale', type:'float', label:'④ PDF 渲染倍率',
    desc:'PDF 页渲染成位图的放大倍率（2.0 ≈ 1224×1584 px）。<b>仅影响 PDF</b>，图片走原图。',
    impact:'★ 实测：放大<b>并不能</b>提升质量（2x/3x 反而从 0.595 降到 0.645/0.623，且耗时翻倍）—— '
      + '识别率瓶颈在字形清晰度而非像素数。默认 2.0 是识别率与耗时的平衡点，一般不动。' },
  { cluster:'ocr', sec:'extract', key:'extract.ocr_min_avg_score', type:'float', label:'⑤ 结果门禁：平均置信度下限',
    desc:'OCR 结果的<b>逐行平均置信度</b>低于此值 → 判"质量不合格"，<b>不入库</b>（文档显示解析失败并写明原因）。',
    impact:'★ 这是防"乱码污染检索"的关键闸门：OCR 对低质截图照样能吐出几百字，只是全是错字。'
      + '实测基线：清晰图/扫描件 0.87~0.94，低质截图 0.60~0.68 → 默认 0.70 留出足够余量。'
      + '调低会放更多可疑文本进库（可能被检索召回去误导回答）。' },
  { cluster:'ocr', sec:'extract', key:'extract.ocr_max_low_score_ratio', type:'float', label:'⑥ 结果门禁：低置信行占比上限 %',
    desc:'OCR 结果中<b>置信度 <0.7 的行</b>占比超过此值 → 判"质量不合格"，不入库。与上一项是<b>双条件</b>，需同时满足。',
    impact:'★ 兜住"平均看着还行、实际一半行是错的"这种情况（实测某截图灰度后均分 0.675 已接近阈值，'
      + '但其低置信行占 51.9%，正是靠这一项挡下）。默认 50%。' },
  { cluster:'ocr', sec:'extract', key:'extract.ocr_grayscale', type:'bool', label:'⑦ 识别前转灰度',
    desc:'OCR 前把图像转为灰度。',
    impact:'实测<b>又快又好</b>：均分 0.595 → 0.675，耗时还降约 0.14 s/页。默认开，无需关闭。' },
];

// ══ 耦合约束校验器 ═══════════════════════════════════════════════════════════
// 返回 [{level:'err'|'warn'|'ok', text}]；level=err 会阻止保存
const CTX_CHECKS = {
  ladder(v) {
    const r = [], rk = v['rag.recall_k'], tk = v['rag.top_k'],
          rc = v['rag.rerank_max_candidates'], ik = v['rag.inject_k'], on = v['rag.rerank_enabled'];
    const num = x => typeof x === 'number' && isFinite(x);
    if (num(rk) && rk > 0 && num(tk) && tk > rk) r.push({ level:'err', text:`融合池 top_k(${tk}) > 每路召回 recall_k(${rk})：池子不可能大于召回来源` });
    if (num(tk) && num(ik) && ik > tk) r.push({ level:'err', text:`注入条数 inject_k(${ik}) > 融合池 top_k(${tk})：注入不出池子` });
    if (on && num(rc) && rc > 0 && num(tk) && rc > tk) r.push({ level:'err', text:`重排窗口(${rc}) > 融合池 top_k(${tk})：上限形同虚设，重排无选择空间（即此前的倒挂缺陷）` });
    if (on && num(rc) && num(ik) && rc > 0 && rc < ik) r.push({ level:'warn', text:`重排窗口(${rc}) < 注入条数(${ik})：注入的片段未全部经过重排` });
    if (!r.length) r.push({ level:'ok', text:'漏斗四层自洽' });
    return r;
  },
  weights(v) {
    const a = +v['rag.w_coverage'] || 0, b = +v['rag.w_relations'] || 0, c = +v['rag.w_typing'] || 0;
    const s = a + b + c, r = [];
    if (Math.abs(s - 1.0) > 0.02) r.push({ level:'err', text:`三因子合计 = ${s.toFixed(2)}，偏离 1.0：置信度量纲失衡，路由阈值将失去原有含义` });
    if (Math.abs(s - 1.0) <= 0.02) r.push({ level:'ok', text:`三因子合计 = ${s.toFixed(2)}，量纲自洽` });
    return r;
  },
  order(v) {
    const h = +v['rag.confidence_high'], m = +v['rag.confidence_mid'], r = [];
    if (h <= m) r.push({ level:'err', text:`「高」分界(${h}) 必须大于「中」分界(${m})，否则等级判定会全部落进同一档` });
    else r.push({ level:'ok', text:`序关系正确（高 ${h} > 中 ${m}）` });
    return r;
  },
  hyde(v) {
    const on = v['rag.hyde_enabled'], w = +v['rag.hyde_weight'], r = [];
    if (!on && w > 0) r.push({ level:'warn', text:`补召开关已关闭，但权重 ${w} 仍 >0：权重不会生效（开关优先），建议一并置 0 以免误导` });
    if (on && w === 0) r.push({ level:'warn', text:'开关开启但权重为 0：等效关闭，建议直接用开关表达' });
    if (!r.length) r.push({ level:'ok', text:'开关与权重语义一致' });
    return r;
  },
  budget(v) {
    const win = +v['context.budget_window_tokens'] || 0, rt = +v['context.budget_retrieval_tokens'] || 0,
          ht = +v['context.budget_history_tokens'] || 0, rr = +v['context.budget_retrieval_ratio'] || 0,
          hr = +v['context.budget_history_ratio'] || 0, r = [];
    const effR = rr > 0 ? Math.round(win * rr) : rt, effH = hr > 0 ? Math.round(win * hr) : ht;
    if (win > 0 && effR + effH > win) r.push({ level:'err', text:`检索区 + 历史区 = ${effR + effH} token，已超窗口基准 ${win}` });
    r.push({ level:'info', text:`实际生效：检索区 ${effR} token（${rr > 0 ? '占比制' : '绝对值'}）`
      + ` ｜ 历史区 ${effH} token（${hr > 0 ? '占比制' : '绝对值'}）`
      + (win > 0 ? ` ｜ 合计占窗口 ${(((effR + effH) / win) * 100).toFixed(1)}%` : '') });
    return r;
  },
  dual(v) {
    const on = v['embedding.enabled'], r = [];
    if (!on) r.push({ level:'warn', text:'语义出口已关闭：右侧 dense 路三项全部失效，实际只按 bigram 阈值判定' });
    const ds = ['context.topic_group_match_dense', 'context.topic_retrieve_threshold_dense'];
    const low = ds.filter(k => (+v[k] || 0) < 0.5);
    if (on && low.length) r.push({ level:'warn', text:`dense 路有 ${low.length} 项低于 0.5（中文 embedding 经验表「关联很弱」档）：`
      + `${low.map(k => k.split('.').pop() + '=' + v[k]).join('、')}，偏松，建议分位等价映射重标` });
    if (!r.length) r.push({ level:'ok', text:'双路阈值均在合理档位' });
    return r;
  },
};

const CTX_LV_STYLE = {
  err:  'background:#fdeaea;border-color:#e6a6a6;color:#a33;',
  warn: 'background:#fdf6e3;border-color:#e8d49a;color:#8a6d1f;',
  ok:   'background:#eef7ee;border-color:#b7d8b7;color:#2f6b2f;',
  info: 'background:var(--blue-l,#f4f8ff);border-color:var(--line);color:var(--mut);',
};

let _ctxSchema = {};

// ══ 取值 ═══════════════════════════════════════════════════════════════════
function ctxSchemaVal(key) {
  const [s, k] = key.split('.');
  const m = ((_ctxSchema[s] || {})[k]) || {};
  return m.current;
}
/** 读某字段「将生效」的值：输入框有内容取输入值，留空则取当前生效值（供校验用）。 */
function ctxFieldVal(f) {
  const el = document.getElementById('ctx-' + f.key);
  if (el) {
    if (f.type === 'bool') return f.type === 'bool' ? el.value === '1' : null;
    const raw = String(el.value).trim();
    if (raw !== '') return Number(raw);
  }
  const cur = ctxSchemaVal(f.key);
  if (f.type === 'bool') return cur === true || cur === 'true' || cur === 1;
  return typeof cur === 'number' ? cur : Number(cur);
}
function ctxReadVals() {
  const v = {};
  for (const f of CTX_CFG_FIELDS) v[f.key] = ctxFieldVal(f);
  return v;
}

// ══ 簇内耦合视图 ════════════════════════════════════════════════════════════
const _bar = (n, max, color) => {
  const w = max > 0 ? Math.max(Math.min(n / max, 1), 0.02) * 100 : 2;
  return `<div style="height:9px;border-radius:4px;background:${color};width:${w}%;min-width:3px;"></div>`;
};

function ctxViz(c, v) {
  const g = 'display:grid;grid-template-columns:118px 1fr 62px;gap:8px;align-items:center;font-size:11px;';
  if (c.viz === 'funnel') {
    const rk = +v['rag.recall_k'] || 0, tk = +v['rag.top_k'] || 0,
          rc = +v['rag.rerank_max_candidates'] || 0, ik = +v['rag.inject_k'] || 0,
          on = v['rag.rerank_enabled'], base = Math.max(rk, tk, rc, ik, 1);
    const rows = [
      ['每路召回', rk, '#4a7ebb', `BM25 + 向量 各 ${rk} 条`],
      ['融合池', tk, '#3f8f6f', `RRF 融合后 ${tk} 条`],
      ['送重排', on ? (rc || tk) : 0, '#8a6dbb', on ? `LLM 打分前 ${rc || tk} 条` : '重排已关闭'],
      ['注入 prompt', ik, '#c07a3a', `【来源1..${ik}】`],
    ];
    return `<div style="border:1px solid var(--line);border-radius:8px;padding:10px 12px;background:var(--bg2,#fafbfc);margin-bottom:10px;">`
      + `<div style="font-size:11px;color:var(--mut);margin-bottom:7px;">漏斗容量（条）</div>`
      + rows.map(([n, val, col, note]) => `<div style="${g}margin-bottom:6px;">`
          + `<div style="color:var(--mut);">${n}</div>${_bar(val, base, col)}`
          + `<div style="text-align:right;font-weight:600;">${on || n !== '送重排' ? val : '—'}</div></div>`
          + `<div style="grid-column:2 / 4;font-size:10px;color:var(--mut);margin:-3px 0 5px;">${note}</div>`).join('')
      + `</div>`;
  }
  if (c.viz === 'weights') {
    const a = +v['rag.w_coverage'] || 0, b = +v['rag.w_relations'] || 0, cc = +v['rag.w_typing'] || 0;
    const s = a + b + cc, th = +v['rag.route_threshold'] || 0;
    const w = x => `${(x / Math.max(s, 0.01) * 100).toFixed(0)}%`;
    return `<div style="border:1px solid var(--line);border-radius:8px;padding:10px 12px;background:var(--bg2,#fafbfc);margin-bottom:10px;">`
      + `<div style="font-size:11px;color:var(--mut);margin-bottom:6px;">置信度合成式 → 与路由阈值比较</div>`
      + `<div style="display:flex;height:16px;border-radius:5px;overflow:hidden;margin-bottom:6px;">`
      + `<div style="width:${w(a)};background:#4a7ebb;color:#fff;font-size:10px;text-align:center;line-height:16px;">覆盖 ${a}</div>`
      + `<div style="width:${w(b)};background:#3f8f6f;color:#fff;font-size:10px;text-align:center;line-height:16px;">关系 ${b}</div>`
      + `<div style="width:${w(cc)};background:#8a6dbb;color:#fff;font-size:10px;text-align:center;line-height:16px;">类型 ${cc}</div></div>`
      + `<div style="font-size:11px;color:var(--mut);">合计 <b style="color:var(--fg,#222);">${s.toFixed(2)}</b>`
      + ` ｜ 判定：置信度 ≥ <b style="color:var(--fg,#222);">${th}</b> → 纯图路由，否则向量补召</div>`
      + `</div>`;
  }
  if (c.viz === 'order') {
    const h = +v['rag.confidence_high'] || 0, m = +v['rag.confidence_mid'] || 0;
    return `<div style="border:1px solid var(--line);border-radius:8px;padding:10px 12px;background:var(--bg2,#fafbfc);margin-bottom:10px;">`
      + `<div style="display:flex;height:16px;border-radius:5px;overflow:hidden;margin-bottom:5px;">`
      + `<div style="width:${Math.min(h, 1) * 100}%;background:#3f8f6f;"></div>`
      + `<div style="width:${Math.max(Math.min(h - m, 1) * 100, 0)}%;background:#c9a227;"></div>`
      + `<div style="flex:1;background:#b96a5a;"></div></div>`
      + `<div style="display:flex;justify-content:space-between;font-size:10px;color:var(--mut);">`
      + `<span>0</span><span>中 ${m}</span><span>高 ${h}</span><span>1.0</span></div>`
      + `<div style="font-size:11px;color:var(--mut);margin-top:5px;">绿=高 黄=中 红=低（仅展示口径，不影响排序）</div></div>`;
  }
  if (c.viz === 'budget') {
    const win = +v['context.budget_window_tokens'] || 0, rt = +v['context.budget_retrieval_tokens'] || 0,
          ht = +v['context.budget_history_tokens'] || 0, rr = +v['context.budget_retrieval_ratio'] || 0,
          hr = +v['context.budget_history_ratio'] || 0;
    const eR = rr > 0 ? Math.round(win * rr) : rt, eH = hr > 0 ? Math.round(win * hr) : ht;
    const base = win || (eR + eH) || 1;
    return `<div style="border:1px solid var(--line);border-radius:8px;padding:10px 12px;background:var(--bg2,#fafbfc);margin-bottom:10px;">`
      + `<div style="font-size:11px;color:var(--mut);margin-bottom:6px;">预算占用（窗口基准 ${win || '—'} token）</div>`
      + `<div style="display:flex;height:16px;border-radius:5px;overflow:hidden;background:var(--line);">`
      + `<div style="width:${(eR / base) * 100}%;background:#4a7ebb;"></div>`
      + `<div style="width:${(eH / base) * 100}%;background:#3f8f6f;"></div></div>`
      + `<div style="font-size:11px;color:var(--mut);margin-top:5px;">`
      + `<span style="color:#4a7ebb;">■</span> 检索区 ${eR}（${rr > 0 ? '占比 ' + rr : '绝对值'}）`
      + ` ｜ <span style="color:#3f8f6f;">■</span> 历史区 ${eH}（${hr > 0 ? '占比 ' + hr : '绝对值'}）`
      + ` ｜ 合计 ${(((eR + eH) / base) * 100).toFixed(1)}%</div></div>`;
  }
  if (c.viz === 'split') {
    const msgs = +v['context.topic_current_max_msgs'] || 0, keep = +v['context.topic_boundary_keep'] || 0,
          topk = +v['context.topic_retrieve_topk'] || 0, sum = +v['context.history_summary_chars'] || 0,
          msgc = +v['context.history_msg_max_chars'] || 0, modc = +v['context.model_context_chars'] || 0;
    return `<div style="border:1px solid var(--line);border-radius:8px;padding:10px 12px;background:var(--bg2,#fafbfc);margin-bottom:10px;">`
      + `<div style="font-size:11px;color:var(--mut);margin-bottom:6px;">历史区预算的固定分配结构</div>`
      + `<div style="display:flex;height:16px;border-radius:5px;overflow:hidden;margin-bottom:5px;">`
      + `<div style="width:50%;background:#4a7ebb;color:#fff;font-size:9.5px;text-align:center;line-height:16px;">当前话题原文 ≤50%</div>`
      + `<div style="width:25%;background:#3f8f6f;color:#fff;font-size:9.5px;text-align:center;line-height:16px;">语义拉回→75%</div>`
      + `<div style="flex:1;background:#c9a227;color:#fff;font-size:9.5px;text-align:center;line-height:16px;">其他话题摘要</div></div>`
      + `<div style="font-size:11px;color:var(--mut);line-height:1.7;">`
      + `当前话题原文 ≤ <b>${msgs}</b> 条（+切换边界保留 <b>${keep}</b> 条）`
      + ` ｜ 语义拉回 <b>${topk}</b> 条 ｜ 摘要上限 <b>${sum}</b> 字符<br>`
      + `前置硬上限：单条消息 <b>${msgc}</b> 字符 ｜ 建模工作记忆 <b>${modc}</b> 字符（独立来源、同样占预算）</div></div>`;
  }
  if (c.viz === 'dual') {
    const on = v['embedding.enabled'];
    const dim = on ? '' : 'opacity:.4;';
    // 值行：bigram/dense 都可配
    const row = (label, bk, dk) => `<tr>`
      + `<td style="padding:4px 8px;color:var(--mut);">${label}</td>`
      + `<td style="padding:4px 8px;text-align:center;font-weight:600;">${+v[bk] || 0}</td>`
      + `<td style="padding:4px 8px;text-align:center;font-weight:600;${dim}">${+v[dk] || 0}</td></tr>`;
    // 死值行：bigram 路为代码硬编码、页面不可配
    const hardRow = (label, bTxt, dk) => `<tr>`
      + `<td style="padding:4px 8px;color:var(--mut);">${label}</td>`
      + `<td style="padding:4px 8px;text-align:center;color:var(--mut);">${bTxt}</td>`
      + `<td style="padding:4px 8px;text-align:center;font-weight:600;${dim}">${+v[dk] || 0}</td></tr>`;
    return `<div style="border:1px solid var(--line);border-radius:8px;padding:10px 12px;background:var(--bg2,#fafbfc);margin-bottom:10px;">`
      + `<div style="font-size:11px;color:var(--mut);margin-bottom:6px;">同一判定的两条路（量纲不同，数值不可互抄）</div>`
      + `<table style="width:100%;border-collapse:collapse;font-size:11.5px;">`
      + `<tr style="color:var(--mut);font-size:10.5px;">`
      + `<th style="text-align:left;padding:3px 8px;">判定</th>`
      + `<th style="padding:3px 8px;">bigram 路</th>`
      + `<th style="padding:3px 8px;">embedding 路</th></tr>`
      + hardRow('话题组匹配', '0.12（硬编码）', 'context.topic_group_match_dense')
      + row('语义拉回', 'context.topic_retrieve_threshold', 'context.topic_retrieve_threshold_dense')
      + hardRow('流程补召门', '0.5（硬编码）', 'context.semantic_fallback_gate_dense')
      + `${on ? '' : '<tr><td colspan="3" style="padding:5px 8px;color:#8a6d1f;font-size:10.5px;">总开关已关闭 → embedding 列全部不生效，仅按 bigram 判定</td></tr>'}`
      + `</table>`
      + `<div style="font-size:10.5px;color:var(--mut);margin-top:6px;line-height:1.6;">`
      + `✅ 三项 dense 阈值现已<b>全部按分位等价映射标定</b>（脚本 <code>tools/_topic_threshold_calibrate.py</code>）：`
      + `流程补召门 0.79（等价点 0.7855）｜话题组匹配 0.50（等价点 0.6023，原值 0.30 通过率 100%）｜`
      + `语义拉回 0.50（等价点 0.6022 下界，原值 0.35 通过率 99.3%）。</div></div>`;
  }
  return '';
}

function ctxChecksHtml(c, v) {
  const out = [];
  for (const k of (c.checks || [])) {
    if (CTX_CHECKS[k]) out.push(...CTX_CHECKS[k](v));
  }
  if (!out.length) return '';
  return `<div style="margin-top:8px;display:flex;flex-direction:column;gap:4px;">`
    + out.map(x => `<div style="font-size:11px;padding:5px 9px;border:1px solid;border-radius:6px;${CTX_LV_STYLE[x.level]}">`
        + `${x.level === 'ok' ? '✅' : x.level === 'err' ? '⛔' : x.level === 'warn' ? '⚠️' : 'ℹ️'} ${x.text}</div>`).join('')
    + `</div>`;
}

// ══ 渲染 ═══════════════════════════════════════════════════════════════════
async function loadCtxConfig() {
  const body = document.getElementById('ctx-cfg-body');
  if (!body) return;
  let schema = {};
  try {
    const d = await api('/api/system/config/schema');
    schema = d.schema || {};
    _ctxSchema = schema;
  } catch (e) {
    body.innerHTML = '<div class="loading">加载失败：' + (e.message || e) + '</div>';
    return;
  }
  const secOf = k => k.split('.')[0], keyOf = k => k.split('.')[1];
  body.innerHTML = CTX_CFG_CLUSTERS.map(c => {
    const fields = CTX_CFG_FIELDS.filter(f => f.cluster === c.id);
    const n = fields.length;
    return `
    <div class="panel" style="margin-bottom:12px;">
      <div class="ph">${c.title}
        <span style="font-size:10.5px;font-weight:400;color:var(--mut);margin-left:8px;">🔗 ${n} 项耦合参数</span></div>
      <div class="pb">
        ${c.hint ? `<div style="font-size:11px;color:var(--mut);margin-bottom:9px;line-height:1.65;">${c.hint}</div>` : ''}
        <div id="ctx-viz-${c.id}">${ctxViz(c, ctxReadVals())}</div>
        ${fields.map(f => {
          const meta = ((schema[secOf(f.key)] || {})[keyOf(f.key)]) || {};
          const cur = meta.current, def = meta.default;
          let input;
          if (f.type === 'bool') {
            const on = (cur === true || cur === 'true' || cur === 1);
            input = `<select id="ctx-${f.key}" onchange="ctxRecheck()" style="border:1px solid var(--line);border-radius:6px;padding:4px 8px;font-size:12px;width:100px;">
              <option value="1"${on ? ' selected' : ''}>开启</option><option value="0"${on ? '' : ' selected'}>关闭</option></select>`;
          } else {
            input = `<input id="ctx-${f.key}" type="number" step="${f.type === 'float' ? '0.01' : '1'}" value="${cur ?? ''}"
              oninput="ctxRecheck()" placeholder="留空=不改"
              style="border:1px solid var(--line);border-radius:6px;padding:4px 8px;font-size:12px;width:100px;">`;
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
        <div id="ctx-ck-${c.id}">${ctxChecksHtml(c, ctxReadVals())}</div>
      </div>
    </div>`;
  }).join('');
}

/** 输入变化时刷新各簇的耦合视图与约束提示（不重渲染字段，避免打断输入）。 */
function ctxRecheck() {
  const v = ctxReadVals();
  for (const c of CTX_CFG_CLUSTERS) {
    const viz = document.getElementById('ctx-viz-' + c.id);
    if (viz) viz.innerHTML = ctxViz(c, v);
    const ck = document.getElementById('ctx-ck-' + c.id);
    if (ck) ck.innerHTML = ctxChecksHtml(c, v);
  }
}

/** 保存前校验：任一簇出现 err 级约束即阻断，并滚动定位到该簇。 */
function ctxValidate() {
  const v = ctxReadVals();
  for (const c of CTX_CFG_CLUSTERS) {
    const errs = [];
    for (const k of (c.checks || [])) {
      if (!CTX_CHECKS[k]) continue;
      errs.push(...CTX_CHECKS[k](v).filter(x => x.level === 'err'));
    }
    if (errs.length) {
      toast('⛔ 「' + c.title.replace(/^[^\s]+\s*/, '') + '」存在冲突配置：' + errs[0].text);
      const el = document.getElementById('ctx-ck-' + c.id);
      if (el && el.scrollIntoView) el.scrollIntoView({ behavior: 'smooth', block: 'center' });
      return false;
    }
  }
  return true;
}

async function saveCtxConfig() {
  if (!ctxValidate()) return;
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
