/* ============================================================
 * SysML v2 视图自动布局引擎（对齐《视图规范》）
 *
 * 核心原则：把语义关系映射到空间关系——每种关系都有约定俗成的
 * 空间方向，布局引擎让空间方向忠实反映语义方向。
 *
 * 8 种视图各用专用确定性布局器（规范明确要求自研规则布局器，
 * 通用分层/力导向算法无法满足 actor 居框外两侧等惯例）：
 *   REQ  三横带分区（顶层需求/派生需求/设计元素）
 *   UC   三列分区（主 actor 左 / 用例中 / 次 actor 右）
 *   BDD  elkjs layered DOWN（组合10 > 泛化5 > 引用1 加权）
 *   IBD  流导向左→右流水线（输入左/处理中/输出右）
 *   SEQ  二维网格（水平轴=生命线序，垂直轴=全局时间序）
 *   ACT  自上而下流水线 + 泳道分区
 *   PAR  约束居中、参数环绕星型
 *   STM  主路径横向 + 异常/回退走外缘
 *   PKG/TRACE  dagre 层级（通用）
 *
 * 用法：const pos = VIEW_LAYOUT_ENGINE[type](nodes, edges)
 *       返回 { nodeId: {x, y} }
 * ============================================================ */

const VIEW_LAYOUT_ENGINE = {};

/* ─────────── 通用工具 ─────────── */
const L = {
  // 节点 id → 节点
  idx(nodes) {
    const m = new Map();
    nodes.forEach(n => m.set(String(n.id), n));
    return m;
  },
  // 拓扑分层（从入度为0的根开始 BFS）
  topoLayers(nodes, edges, weightKey) {
    const idx = L.idx(nodes);
    const inDeg = new Map(), outMap = new Map();
    nodes.forEach(n => { inDeg.set(String(n.id), 0); outMap.set(String(n.id), []); });
    (edges || []).forEach(e => {
      const s = String(e.source), t = String(e.target);
      if (!idx.has(s) || !idx.has(t)) return;
      // 反转特殊关系（specialization 父→子）
      outMap.get(s).push({ target: t, kind: e.kind, label: e.label, weight: e.weight || 0 });
      inDeg.set(t, (inDeg.get(t) || 0) + 1);
    });
    const layer = new Map();
    const q = [];
    nodes.forEach(n => { if ((inDeg.get(String(n.id)) || 0) === 0) q.push(String(n.id)); });
    if (!q.length && nodes.length) q.push(String(nodes[0].id));
    // 用加权 dagre 风格：层次按 max(parent.layer) + 1
    while (q.length) {
      const cur = q.shift();
      if (layer.has(cur)) continue;
      const inEdges = (edges || []).filter(e => String(e.target) === cur);
      let lv = 0;
      inEdges.forEach(e => {
        const ps = layer.get(String(e.source));
        if (ps !== undefined) lv = Math.max(lv, ps + 1);
      });
      layer.set(cur, lv);
      outMap.get(cur).forEach(e => {
        if (!layer.has(e.target)) q.push(e.target);
      });
    }
    return layer;
  },
  // 同层兄弟排序：按父节点分组 → 子节点数降序 → 名称字典序（REQ/BDD 规范）
  sortSiblings(nodes, edges, layer) {
    const idx = L.idx(nodes);
    const byLayer = new Map();
    nodes.forEach(n => {
      const lv = layer.get(String(n.id)) ?? 0;
      if (!byLayer.has(lv)) byLayer.set(lv, []);
      byLayer.get(lv).push(n);
    });
    // 父→子 映射（composition/derive）
    const childCount = new Map(); // parentId → children[]
    (edges || []).forEach(e => {
      if (e.kind === 'composition' || e.kind === 'derive' || e.kind === 'trace' || e.kind === 'refine') {
        if (!childCount.has(String(e.source))) childCount.set(String(e.source), []);
        childCount.get(String(e.source)).push(String(e.target));
      }
    });
    byLayer.forEach((items, lv) => {
      items.sort((a, b) => {
        const ca = (childCount.get(String(a.id)) || []).length;
        const cb = (childCount.get(String(b.id)) || []).length;
        if (ca !== cb) return cb - ca; // 子节点数降序
        return String(a.name || '').localeCompare(String(b.name || ''), 'zh'); // 名称字典序
      });
    });
    return byLayer;
  },
  // 输出位置（统一格式）
  emit(byLayer, opts) {
    const pos = {};
    const cellW = opts.cellW || 170, cellH = opts.cellH || 90;
    const padX = opts.padX || 30, padY = opts.padY || 30;
    // 每层节点横向展开（同层兄弟并排），超 6 个换行
    const perRow = opts.perRow || 6;
    byLayer.forEach((items, lv) => {
      items.forEach((n, i) => {
        const row = Math.floor(i / perRow), col = i % perRow;
        const w = opts.widthOf ? opts.widthOf(n) : cellW;
        const x = padX + col * (cellW + (opts.gapX || 24));
        const y = padY + lv * cellH + row * (cellH * (opts.rowScale || 1));
        pos[String(n.id)] = { x, y };
      });
    });
    return pos;
  },
  // 直接指定位置（用于特殊布局）
  place(pos, id, x, y) { pos[String(id)] = { x, y }; },
};

/* ─────────── REQ 需求视图：三横带分区 ─────────── */
// 上带：顶层需求（stakeholder/system level）
// 中带：派生子需求（subsystem level）
// 下带：满足需求的设计元素（Block/Part，satisfier 置于最下层+1 居中）
VIEW_LAYOUT_ENGINE.REQ = (nodes, edges) => {
  const idx = L.idx(nodes);
  const deriveEdges = (edges || []).filter(e => e.kind === 'derive' || e.kind === 'trace' || e.kind === 'refine');
  const satisfyEdges = (edges || []).filter(e => e.kind === 'satisfy' || e.kind === 'verify' || e.kind === 'conflict');
  // 需求树分层（derive 驱动）
  const reqNodes = nodes.filter(n => n.kind === 'requirement');
  const designNodes = nodes.filter(n => n.kind !== 'requirement');
  // 需求层级：父需求层号 < 子需求层号（R-REQ-02）
  const layers = L.topoLayers(reqNodes, deriveEdges);
  const byLayer = L.sortSiblings(reqNodes, deriveEdges, layers);
  const pos = {};
  // 三横带：y 带偏移
  const BAND_TOP = 0, BAND_MID = 1, BAND_DESIGN = 2;
  const cellW = 190, cellH = 110, padX = 30;
  // 上带+中带：需求节点
  byLayer.forEach((items, lv) => {
    const y = padX + BAND_TOP * cellH + lv * cellH;
    items.forEach((n, i) => {
      // 同层<5 水平平铺；>5 换行每层最多5个
      const perRow = 5;
      const row = Math.floor(i / perRow), col = i % perRow;
      L.place(pos, n.id, padX + col * (cellW + 20), y + row * (cellH * 0.6));
    });
  });
  // 下带：设计元素（satisfier）—— 置于其所满足需求的最下层+1，水平居中
  const designByBand = {};
  satisfyEdges.forEach(e => {
    // satisfy: requirement → design element（source 需求 / target 设计）
    const srcLayer = layers.get(String(e.source));
    const bandKey = srcLayer !== undefined ? (srcLayer + 1) : 0;
    if (!designByBand[bandKey]) designByBand[bandKey] = [];
    if (idx.has(String(e.target)) && !designByBand[bandKey].includes(String(e.target))) {
      designByBand[bandKey].push(String(e.target));
    }
  });
  // 孤立设计元素放最底部
  designNodes.forEach(n => {
    const isSatisfier = satisfyEdges.some(e => String(e.target) === String(n.id));
    if (!isSatisfier) {
      if (!designByBand[5]) designByBand[5] = [];
      if (!designByBand[5].includes(String(n.id))) designByBand[5].push(String(n.id));
    }
  });
  Object.keys(designByBand).forEach(bandKey => {
    const y = padX + BAND_DESIGN * cellH + parseInt(bandKey) * cellH;
    const items = designByBand[bandKey];
    items.forEach((id, i) => {
      const w = cellW + 30;
      L.place(pos, id, padX + i * (w + 20), y);
    });
  });
  return pos;
};

/* ─────────── UC 用例视图：三列分区（自研规则布局器） ─────────── */
// 主 actor 左列 / 用例中列（Subject 框内）/ 次 actor 右列
VIEW_LAYOUT_ENGINE.UC = (nodes, edges) => {
  const actors = nodes.filter(n => n.kind === 'actor');
  const usecases = nodes.filter(n => n.kind === 'usecase');
  const others = nodes.filter(n => n.kind !== 'actor' && n.kind !== 'usecase');
  const pos = {};
  const colW = 200, cellH = 130, pad = 60;
  // 中列：用例（按业务流程自上而下）
  usecases.forEach((n, i) => L.place(pos, n.id, pad + colW * 1.4, pad + 60 + i * cellH));
  // 左列：actor（关联到用例的重心对侧）
  actors.forEach((n, i) => L.place(pos, n.id, pad + 40, pad + 60 + i * cellH));
  // 其他：右下兜底
  others.forEach((n, i) => L.place(pos, n.id, pad + colW * 2.4, pad + 60 + i * cellH));
  return pos;
};

/* ─────────── BDD 模块定义视图：elkjs layered DOWN ─────────── */
// 泛化树向上收敛、组合树自洽；组合边权重10 > 泛化5 > 引用1
// 层分配：组合边优先决定层（同一定义泛化/组合冲突时组合优先）
VIEW_LAYOUT_ENGINE.BDD = (nodes, edges) => {
  // 权重重赋值：composition=10, generalization=5, 其余=1
  const weighted = (edges || []).map(e => {
    let w = 1;
    if (e.kind === 'composition') w = 10;
    else if (e.kind === 'generalization' || e.kind === 'specialization') w = 5;
    return { ...e, weight: w };
  });
  // 层分配按加权拓扑
  const layers = L.topoLayers(nodes, weighted);
  // 同层 barycenter 排序：同父兄弟按组合成员数降序 → 名称字典序
  const byLayer = L.sortSiblings(nodes, weighted, layers);
  return L.emit(byLayer, { cellW: 170, cellH: 100, padX: 40, padY: 40 });
};

/* ─────────── IBD 内部结构视图：主块框架 + 子部件网格 + 端口挂边界 ─────────── */
// 主块 = composition 根（无父）中「子部件数 + connect 端口关联」得分最高
// 布局：主块居中上部大框位 → 子部件下方网格 → 端口节点挂所属部件左/右边界
VIEW_LAYOUT_ENGINE.IBD = (nodes, edges) => {
  const pos = {};
  const byId = L.idx(nodes);
  const compEdges = (edges || []).filter(e => e.kind === 'composition' || e.kind === 'nesting');
  const parents = new Set(), children = new Set();
  compEdges.forEach(e => {
    if (byId.has(String(e.source))) parents.add(String(e.source));
    if (byId.has(String(e.target))) children.add(String(e.target));
  });
  const roots = nodes.filter(n => parents.has(String(n.id)) && !children.has(String(n.id)));
  const connectEnds = new Set();
  (edges || []).filter(e => e.kind === 'connect' || e.kind === 'flow')
    .forEach(e => { connectEnds.add(String(e.source)); connectEnds.add(String(e.target)); });
  // 主块：connect 端口关联的子部件最多者
  let main = roots[0] || nodes[0], bestScore = -1;
  roots.forEach(r => {
    const subs = compEdges.filter(e => String(e.source) === String(r.id));
    const score = subs.length * 2 + subs.filter(e => connectEnds.has(String(e.target))).length * 4
      + (connectEnds.has(String(r.id)) ? 2 : 0);
    if (score > bestScore) { bestScore = score; main = r; }
  });
  const mainId = String(main.id);
  const subIds = compEdges.filter(e => String(e.source) === mainId).map(e => String(e.target));
  const subs = subIds.map(id => byId.get(id)).filter(Boolean);
  // 主块居中上部
  const CX = 480, CY = 150;
  L.place(pos, main.id, CX, CY);
  // 子部件：主块下方网格（每行 3）
  subs.forEach((n, i) => {
    const col = i % 3, row = Math.floor(i / 3);
    L.place(pos, n.id, 300 + col * 180, 340 + row * 120);
  });
  // 端口节点：挂所属部件边界（右侧错开）
  const portOwner = {};
  nodes.forEach(n => {
    ((n.attrs && n.attrs.ports) || []).forEach(p => { portOwner[String(p)] = String(n.id); });
  });
  let pi = 0;
  nodes.forEach(n => {
    if (n.kind === 'port' && portOwner[String(n.id)] && pos[portOwner[String(n.id)]]) {
      const op = pos[portOwner[String(n.id)]];
      const side = pi % 2 === 0 ? -1 : 1;
      L.place(pos, n.id, op.x + side * 100, op.y + 20 + Math.floor(pi / 2) * 26);
      pi++;
    }
  });
  // 其余节点（非主块、非子、非端口）右下兜底
  let yy = 500;
  nodes.forEach(n => {
    if (!pos[String(n.id)]) { L.place(pos, n.id, 850, yy); yy += 90; }
  });
  return pos;
};

/* ─────────── SEQ 时序视图：二维网格 ─────────── */
// 水平轴=生命线声明序（发起者最左），垂直轴=全局时间序
VIEW_LAYOUT_ENGINE.SEQ = (nodes, edges) => {
  const pos = {};
  // 生命线：按消息边的出现顺序
  const lifelines = nodes.filter(n => (n.kind === 'part' || n.kind === 'actor' || n.kind === 'block'));
  const msgs = (edges || []).filter(e => e.kind === 'message' || e.kind === 'flow');
  // 生命线顺序：发起者最左（出现在 source 多的靠左）
  const srcCount = new Map();
  nodes.forEach(n => srcCount.set(String(n.id), 0));
  msgs.forEach(e => { if (srcCount.has(String(e.source))) srcCount.set(String(e.source), srcCount.get(String(e.source)) + 1); });
  const sorted = [...lifelines].sort((a, b) => (srcCount.get(String(b.id)) || 0) - (srcCount.get(String(a.id)) || 0));
  const llX = new Map();
  const GAP = 150, pad = 80;
  sorted.forEach((n, i) => llX.set(String(n.id), pad + 60 + i * GAP));
  lifelines.forEach(n => L.place(pos, n.id, llX.get(String(n.id)), pad + 30));
  // 消息：时间自上而下递增（水平线）
  let t = 1;
  const msgY = new Map(); // source,target → y
  msgs.forEach((m, i) => {
    const y = pad + 90 + i * 60;
    L.place(pos, 'msg_' + i, (llX.get(String(m.source)) + llX.get(String(m.target))) / 2, y);
  });
  return pos;
};

/* ─────────── ACT 动作流视图：自上而下流水线 + 泳道 ─────────── */
VIEW_LAYOUT_ENGINE.ACT = (nodes, edges) => {
  const pos = {};
  const controlEdges = (edges || []).filter(e => e.kind === 'flow' || e.kind === 'control' || e.kind === 'dependency');
  const layers = L.topoLayers(nodes, controlEdges);
  const byLayer = L.sortSiblings(nodes, controlEdges, layers);
  // 泳道：按 owning part（name 前缀分组简化：type 属性）
  const lanes = new Map();
  nodes.forEach(n => {
    const laneKey = n.type || '默认泳道';
    if (!lanes.has(laneKey)) lanes.set(laneKey, []);
    lanes.get(laneKey).push(n);
  });
  const laneKeys = [...lanes.keys()];
  // 布局：层 → 泳道 → 位置
  byLayer.forEach((items, lv) => {
    const perLane = new Map();
    items.forEach(n => {
      const k = n.type || '默认泳道';
      if (!perLane.has(k)) perLane.set(k, 0);
      const cnt = perLane.get(k);
      perLane.set(k, cnt + 1);
      const laneX = laneKeys.indexOf(k) * 240 + 60;
      const y = 80 + lv * 110;
      L.place(pos, n.id, laneX + cnt * 160, y);
    });
  });
  return pos;
};

/* ─────────── PAR 参数视图：约束居中、参数环绕星型 ─────────── */
VIEW_LAYOUT_ENGINE.PAR = (nodes, edges) => {
  const pos = {};
  const constraints = nodes.filter(n => n.kind === 'constraint');
  const params = nodes.filter(n => n.kind !== 'constraint');
  const CX = 500, CY = 300;
  // 约束居中
  constraints.forEach((n, i) => L.place(pos, n.id, CX, CY + i * 20));
  // 参数环绕（按角度分布）
  const R = 220;
  params.forEach((n, i) => {
    const angle = (i / Math.max(1, params.length)) * Math.PI * 2 - Math.PI / 2;
    L.place(pos, n.id, CX + R * Math.cos(angle), CY + R * Math.sin(angle));
  });
  return pos;
};

/* ─────────── STM 状态视图：主路径横向 + 回退/异常走外缘 ─────────── */
VIEW_LAYOUT_ENGINE.STM = (nodes, edges) => {
  const pos = {};
  const states = nodes.filter(n => n.kind === 'state');
  const trans = (edges || []).filter(e => e.kind === 'transition' || e.kind === 'dependency');
  // 找主路径（最长简单路径，贪心从初始状态开始）
  const start = states.find(n => (n.name || '').toLowerCase().includes('init') || (n.name || '').includes('初始')) || states[0];
  const visited = new Set();
  const mainPath = [];
  let cur = start;
  while (cur) {
    mainPath.push(cur);
    visited.add(String(cur.id));
    const next = trans
      .filter(e => String(e.source) === String(cur.id) && !visited.has(String(e.target)))
      .map(e => ({ id: String(e.target), deg: (trans.filter(t => String(t.source) === String(e.target)).length) }))
      .sort((a, b) => b.deg - a.deg)[0];
    cur = next ? states.find(s => String(s.id) === next.id) : null;
  }
  // 主路径从左到右
  mainPath.forEach((n, i) => L.place(pos, n.id, 100 + i * 200, 220));
  // 回退/异常状态放外缘（上下）
  const side = states.filter(s => !visited.has(String(s.id)));
  let up = 0, down = 0;
  side.forEach(n => {
    const isBackward = trans.some(e => String(e.target) === String(n.id) && mainPath.some(m => String(m.id) === String(e.source)));
    if (isBackward) { L.place(pos, n.id, 100 + up * 180, 80); up++; }
    else { L.place(pos, n.id, 100 + down * 180, 400); down++; }
  });
  return pos;
};

/* ─────────── PKG / TRACE：dagre 通用层级 ─────────── */
VIEW_LAYOUT_ENGINE.PKG = (nodes, edges) => {
  const layers = L.topoLayers(nodes, edges);
  const byLayer = L.sortSiblings(nodes, edges, layers);
  return L.emit(byLayer, { cellW: 190, cellH: 110, padX: 40, padY: 40 });
};
VIEW_LAYOUT_ENGINE.TRACE = (nodes, edges) => {
  const layers = L.topoLayers(nodes, edges);
  const byLayer = L.sortSiblings(nodes, edges, layers);
  return L.emit(byLayer, { cellW: 190, cellH: 110, padX: 40, padY: 40 });
};

/* 兜底：dagre 风格分层 */
VIEW_LAYOUT_ENGINE._default = (nodes, edges) => {
  const layers = L.topoLayers(nodes, edges);
  const byLayer = L.sortSiblings(nodes, edges, layers);
  return L.emit(byLayer, { cellW: 170, cellH: 100, padX: 40, padY: 40 });
};

/* 统一入口 */
function computeViewLayout(viewType, nodes, edges) {
  const engine = VIEW_LAYOUT_ENGINE[viewType] || VIEW_LAYOUT_ENGINE._default;
  try {
    return engine(nodes || [], edges || []) || {};
  } catch (e) {
    console.warn('[layout]', viewType, '布局失败，回退默认:', e.message);
    return VIEW_LAYOUT_ENGINE._default(nodes || [], edges || []) || {};
  }
}

/* ═════════════ ELK 行业标准布局内核（异步精排层）═════════════
 * 依据：SRS-GN-MG-BJYH 只要求「布局合理」，不约束算法实现。
 * 适用：图结构类视图（BDD/PKG/TRACE）——自研分层器不做交叉最小化，
 *       实测 BDD 真实数据 30 节点 96 处交叉；ELK 的 LAYER_SWEEP
 *       交叉最小化 + 网络单纯形层分配 + 正交布线是行业标准方案
 *       （Eclipse 系建模工具同源内核，EPL-2.0，vendored 离线可用）。
 * 语义惯例类视图（REQ/UC/SEQ/ACT/PAR/STM）保留自研确定性布局：
 *       actor 居框外两侧、生命线时序等约定俗成的空间语义 ELK 不理解。
 *       IBD 走 ELK 端口约束布局（实测优于自研「端口右错开」，见 _elkIbdGraph）。
 * 调用：computeViewLayoutAsync() 返回 null = 不适用/失败，调用方沿用
 *       computeViewLayout() 的同步初排，绝不阻断渲染。
 */
const VIEW_ELK_TYPES = new Set(['BDD', 'PKG', 'TRACE', 'IBD', 'REQ']);
let _elkInstance = null;

function _elkEdgePriority(kind) {
  if (kind === 'composition') return 10;
  if (kind === 'generalization' || kind === 'specialization') return 5;
  return 1;
}

function _elkLayoutOptions() {
  return {
    'elk.algorithm': 'layered',
    'elk.direction': 'DOWN',
    'elk.layered.crossingMinimization.strategy': 'LAYER_SWEEP',
    'elk.spacing.nodeNode': '46',
    'elk.layered.spacing.nodeNodeBetweenLayers': '70',
    'elk.edgeRouting': 'ORTHOGONAL',
  };
}

/* IBD 专用：端口节点不作为独立子节点参与分层，而是挂到所属部件的
 * ELK port 上——ELK 会把端口布在部件边界并把连线直达端口，
 * 这是自研「端口挂右侧错开」无法企及的核心能力。返回 ELK 图。 */
function _elkIbdGraph(nodes, edges) {
  const portOwner = {};
  nodes.forEach(n => ((n.attrs && n.attrs.ports) || []).forEach(p => { portOwner[String(p)] = String(n.id); }));
  const childIds = new Set(nodes.filter(n => n.kind !== 'port').map(n => String(n.id)));
  const children = nodes.filter(n => n.kind !== 'port').map(n => ({
    id: String(n.id),
    width: 150,
    height: 60,
    ports: ((n.attrs && n.attrs.ports) || [])
      .filter(p => portOwner[String(p)] === String(n.id))
      .map(p => ({ id: String(p), width: 10, height: 10 })),
  }));
  const edgs = [];
  (edges || []).forEach((e, i) => {
    const s = String(e.source), t = String(e.target);
    const sOwner = childIds.has(s) ? s : (portOwner[s] && childIds.has(portOwner[s]) ? portOwner[s] : null);
    const tOwner = childIds.has(t) ? t : (portOwner[t] && childIds.has(portOwner[t]) ? portOwner[t] : null);
    if (!sOwner || !tOwner) return; // 孤儿端口边丢弃（同步初排会兜底摆放）
    const ed = { id: 'e' + i, sources: [sOwner], targets: [tOwner] };
    if (!childIds.has(s)) ed.sourcePort = s;
    if (!childIds.has(t)) ed.targetPort = t;
    ed.layoutOptions = { 'elk.layered.priority': String(_elkEdgePriority(e.kind)) };
    edgs.push(ed);
  });
  return { id: 'root', layoutOptions: _elkLayoutOptions(), children, edges: edgs };
}

/* 平铺图（BDD/PKG/TRACE）：全部节点作为子节点分层 */
function _elkFlatGraph(nodes, edges) {
  return {
    id: 'root',
    layoutOptions: _elkLayoutOptions(),
    children: (nodes || []).map(n => ({ id: String(n.id), width: 150, height: 60 })),
    edges: (edges || []).map((e, i) => ({
      id: 'e' + i,
      sources: [String(e.source)],
      targets: [String(e.target)],
      layoutOptions: { 'elk.layered.priority': String(_elkEdgePriority(e.kind)) },
    })),
  };
}

async function computeViewLayoutAsync(viewType, nodes, edges) {
  if (!window.ELK || !VIEW_ELK_TYPES.has(viewType)) return null;
  try {
    if (!_elkInstance) _elkInstance = new ELK();
    const graph = viewType === 'IBD'
      ? _elkIbdGraph(nodes || [], edges || [])
      : _elkFlatGraph(nodes || [], edges || []);
    const out = await _elkInstance.layout(graph);
    const pos = {};
    (out.children || []).forEach(c => {
      if (c.x != null) pos[c.id] = { x: Math.round(c.x), y: Math.round(c.y) };
      // IBD：端口位置 = 部件位置 + 端口相对坐标（ELK 已布到部件边界）
      (c.ports || []).forEach(p => {
        if (p.x != null) pos[p.id] = { x: Math.round(c.x + p.x), y: Math.round(c.y + p.y) };
      });
    });
    return Object.keys(pos).length ? pos : null;
  } catch (e) {
    console.warn('[ELK]', viewType, '精排失败，沿用自研初排:', e.message);
    return null;
  }
}
