/* api.js —— API 桩层：函数签名 / 参数 / 返回形状即"未来真实接口"，内部返回 mock。
   之后接后端时只需把实现换成 fetch(...)，调用方与 UI 不用改。 */

const delay = (ms) => new Promise((r) => setTimeout(r, ms));

/**
 * 分块列表（文档库）
 * TODO: replace with → GET /api/documents/chunks?linked=none&limit=8&offset=0
 * 响应形状：{ code:0, data:[{id,document_id,chunk_index,source_doc,section,content,linked_entity_ids}], total }
 * 说明：`linked=none` 即本次要新增的**筛选参数**（现状该接口无此参数，故无法承接看板下钻）。
 */
async function fetchChunks({ unlinkedOnly = false, limit = 8 } = {}) {
  await delay(unlinkedOnly ? 520 : 420);
  const all = DB.chunks;
  const data = unlinkedOnly ? all.filter((c) => c.linked === 0) : all;
  return { code: 0, data: data.slice(0, limit), total: unlinkedOnly ? DB.scope.global.chunks : all.length };
}

/**
 * v2g 候选列表（治理中心）
 * TODO: replace with → GET /api/knowledge/v2g/candidates?status=candidate&limit=8
 * 响应形状：{ code:0, data:[{id,entity_name,entity_type,source_doc,status}], total, summary:{triples,v2g} }
 * 说明：`status` 即本次要新增的**筛选参数**（现状该接口无此参数）。
 */
async function fetchCandidates({ status = '', limit = 8 } = {}) {
  await delay(480);
  const data = status ? DB.candidates.filter((c) => c.status === status) : DB.candidates;
  return { code: 0, data: data.slice(0, limit), total: data.length, summary: DB.candidateSummary };
}
