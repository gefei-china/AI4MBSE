/* mock.js —— 单一数据源。
   全部为 mbse_system 真实库的实测值 / 真实文本（2026-09-27 只读取样），非 Lorem 占位。 */
const DB = {
  scope: {
    release: { entities: 61, relations: 83, published: 61, unreviewed: 0 },
    branch: { name: 'personal', entities: 63, relations: 83, candidate: 0, deprecated: 2, unpublishedDelta: 2 },
    global: { entitiesDedup: 61, relationsDedup: 83, documents: 6, chunks: 5758, rowsE: 183, rowsR: 249 },
  },

  /* 文档与分块：linked 全为 0 → 分块追溯覆盖率 0%（index 回归口径） */
  docs: [
    { name: 'OMG Systems Modeling Language™ V2 (1).pdf', chunks: 2606, linked: 0 },
    { name: 'Kernel Modeling Language™ (KerML™).pdf', chunks: 2011, linked: 0 },
    { name: 'Guide to writing Requirements.pdf', chunks: 855, linked: 0 },
    { name: 'SysML-v2-AI建模知识文档.md', chunks: 270, linked: 0 },
    { name: '验证用例（儿童遗留防护）.md', chunks: 16, linked: 0 },
    { name: '（未解析文档）', chunks: 0, linked: 0 },
  ],

  chunks: [
    { id: 16598, doc: 'OMG Systems Modeling Language™ V2 (1).pdf', i: 0, section: '',
      excerpt: '®An OMG Systems Modeling PublicationOMG Systems Modeling Language™®(SysML )Version 2.0Part 1: Language Specifi', linked: 0 },
    { id: 16599, doc: 'OMG Systems Modeling Language™ V2 (1).pdf', i: 1, section: '',
      excerpt: '8solutions CorporationCopyright © 2019-2025, AirbusCopyright © 2019-2025, Aras CorporationCopyright © 2019-202', linked: 0 },
    { id: 14587, doc: 'Kernel Modeling Language™ (KerML™).pdf', i: 0, section: '',
      excerpt: '®An OMG Systems Modeling PublicationKernel Modeling Language™ (KerML™)Version 1.0_____________________________', linked: 0 },
    { id: 19741, doc: 'Guide to writing Requirements.pdf', i: 0, section: '1 Jul 2023',
      excerpt: 'INCOSE-TP-2010-006-04 | VERS/REV: 4 | 1 April 2022Guide to Writing Requirements iCOPYRIGHT INFORMATIONThis INC', linked: 0 },
    { id: 19742, doc: 'Guide to writing Requirements.pdf', i: 1, section: '1 Jul 2023',
      excerpt: 'credit to the INCOSE Technical source. Abstraction is permitted withcredit to the source.INCOSE Use. Permissio', linked: 0 },
    { id: 19471, doc: 'SysML-v2-AI建模知识文档.md', i: 0, section: '# SysML v2 / KerML 文本建模知识文档',
      excerpt: '> **用途**：供 AI 模型生成 SysML v2 文本代码时使用的权威参考。读完本文档，应能写出**可直接通过 SysML v2 校验器**的模型代码。>> **版本**：v1.3.1（2026-09-19 —— ', linked: 0 },
    { id: 19472, doc: 'SysML-v2-AI建模知识文档.md', i: 1, section: '# SysML v2 / KerML 文本建模知识文档',
      excerpt: '合法关键字**，三种形式均可：`include use case <名>;` / `include use case <名> : <类型>;` / `include <引用>;`。**`extend` 在 SysML v', linked: 0 },
    { id: 14571, doc: '验证用例（儿童遗留防护）.md', i: 0, section: '# 汽车儿童遗留防护系统（CPDS）SysML V2 视图生成提示词集',
      excerpt: '**场景主线**：锁车下电 → 后排传感（毫米波+摄像头）→ 融合判定儿童遗留 → 三级响应（本地声光 → TSP远程通知 → 车窗/空调干预）→ 监护人响应解除。', linked: 0 },
    { id: 14572, doc: '验证用例（儿童遗留防护）.md', i: 1, section: '## 视图 1 · 需求图（Requirement）',
      excerpt: '```Markdown 你是SysML V2需求建模专家，精通MECE需求分解与ISO/IEC/IEEE 29148需求工程标准。请生成 sysml v2 需求图 场景描述：汽车儿童遗', linked: 0 },
    { id: 14575, doc: '验证用例（儿童遗留防护）.md', i: 4, section: '## 视图 3 · BDD（块定义图）',
      excerpt: '模块定义图：基于需求数据和用例数据，生成 SysML v2 块定义图。要求充分的分析并输出完备的架构数据，符合 MECE 法则', linked: 0 },
    { id: 14577, doc: '验证用例（儿童遗留防护）.md', i: 6, section: '## 视图 4 · 活动图（Activity）',
      excerpt: '```Markdown 你是SysML V2行为建模专家，精通活动分解、控制流建模与异常分支设计。请生成 sysml v2 顺序图（活动图） 【输入】- 用例：UC-01~UC-0', linked: 0 },
    { id: 14578, doc: '验证用例（儿童遗留防护）.md', i: 7, section: '## 视图 4 · 活动图（Activity）',
      excerpt: '活动图：基于模块定义图和需求数据，生成 SysML v2 活动图，描述遗留检测与应急响应流程，要求充分的分析并输出完备的活动图数据，符合 MECE 法则', linked: 0 },
  ],

  /* 待审候选：真实为空（triples 0 + v2g 0）→ 用于展示「下钻后空结果态」 */
  candidates: [],
  candidateSummary: { triples: 0, v2g: 0 },

  /* 关键指标：value / target / status / calc（calc 与后端 metric.calc 一致口径） */
  metrics: {
    chunk_trace: {
      name: '分块追溯覆盖率', value: 0, unit: '%', target: '≥ 90%', status: 'alert',
      calc: '来源：document_chunks。已链接 = linked_entity_ids 非 NULL、TRIM 后不为空且不为 "[]"。公式：覆盖率 = 已链接数 ÷ 总 chunk 数。未链接 ⇒ 抽取结果无来源，追溯链断裂。',
      hits: '5758 个分块全部未链接实体',
    },
    pending_candidates: {
      name: '未决候选（审核积压）', value: 0, unit: '条', target: '持续增长需扩审核产能', status: 'ok',
      calc: '来源：triples(status=candidate) + v2g_candidates(status=pending)。取值：两者相加。',
      hits: 'triples 0 + v2g 0',
    },
    health: { name: '知识健康分', value: 61, unit: '/100' },
  },
};