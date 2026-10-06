# 本体编辑区对齐 WebProtege 改造清单

> 目标：把本体模型页（kb-c）的编辑区，从「左栏 4 态混叠」重构成 WebProtege 式的
> 「顶部 Tab（实体维度） + 左栏树（唯一选择源） + 中栏投影（编辑⇄图谱） + 右栏详情」，
> 并补齐 **IRI 主轴**（每个类/属性都有系统生成的 IRI，展示于编辑区顶部）。

---

## 0. 核心交互契约（三轮澄清后固化，不可违背）

1. **左栏 = 唯一选择源**：左栏树永远是「当前实体维度（TopTab）的导航树」，谁被选中，中栏就投影谁。
2. **中栏 = 选中节点的投影**：中栏不是孤立区域，编辑模式（字段表单）与图谱模式（1-hop 邻域子图）都是**同一个选中节点**的两种呈现。
3. **编辑⇄图谱只作用于中栏**：切换时左栏树、右栏详情**不动**，只改中栏内部两个容器的显示。
4. **焦点永不清空**：切 TopTab / 切编辑⇄图谱，`ontSelected` 保持不变；中栏不存在无焦点的空态。
5. **IRI 是每个实体的锚点**：系统自动生成（namespace + 实体名），可在新建/编辑时覆盖。

---

## 1. 现状 → 目标 对照

| 维度 | 现状 | 目标（对齐 WebProtege） |
|---|---|---|
| 顶部 Tab | `本体模型维护 / 图谱 / RDF` 三个**呈现形式** Tab（L460-463） | `类 / 对象属性 / 数据属性 / 个体 / 注释 / 变更历史` 六个**实体维度** Tab；RDF 收进工具行 |
| 左栏 | `类/对象属性/数据属性` 三个 `ont-vtab` + 一个隐藏的 `graf`（L528-531） | 由 TopTab 决定显示哪个树（去掉「图谱」这个左栏 tab） |
| 中栏 | 仅 Description（编辑），图谱是独立页面态 | 中栏顶部加 `编辑 ⇄ 图谱` 两段式 toggle，图谱作为中栏内子视图 |
| 状态模型 | `ontView ∈ {classes,props,dprops,graf}` 四态混叠（L9789） | 拆两层：`ontTopTab`（实体维度）× `ontPaneMode`（编辑/图谱），正交 |
| IRI | 无（类只有 `name`） | `ontology_types.iri` 列 + 自动生成 + 编辑区顶部展示 |
| 个体 | 无个体视图 | 新增「个体」Tab（P1，可延后） |

---

## 2. 改造分阶段

### 🔴 P0-1：引入 IRI 主轴（数据层 + 后端 + 前端头部）

#### 2.1.1 数据层（`database/schema.py` L188 + `database/migrations.py` L382）

- `ontology_types` 新增列：`iri TEXT`（`UNIQUE`，允许 NULL 兼容老数据）。
- 新增单行配置表 `ontology_meta`（key-value）：

```sql
CREATE TABLE IF NOT EXISTS ontology_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
-- 种子：namespace_prefix = 'http://example.org/mbse#', iri_strategy = 'hash-name'
```

- 迁移（`database/migrations.py` 追加到 `_migrate_columns`）：

```python
_add("ontology_types", "iri", "TEXT")   # 可空，回填后再建唯一索引
# 回填：UPDATE ontology_types SET iri = <namespace> || '#' || <slugified_name> WHERE iri IS NULL;
# 冲突追加 _2/_3
# 回填完成后：CREATE UNIQUE INDEX idx_ont_iri ON ontology_types(iri) WHERE iri IS NOT NULL;
```

#### 2.1.2 后端 IRI 生成器（`ontology_semantics.py` 新增）

```python
import re, uuid

def slugify(name: str) -> str:
    # 中文保留，空格/特殊字符 → '_'；用于 IRI 尾段
    s = re.sub(r'[^\w\u4e00-\u9fff-]', '_', name.strip())
    return s or 'entity'

def make_iri(namespace: str, name: str, strategy: str = 'hash-name',
             exists=lambda iri: False) -> str:
    ns = namespace.rstrip('#/')
    if strategy == 'uuid':
        return f"{ns}#{uuid.uuid4().hex[:12]}"
    # hash-name：保证唯一
    base = f"{ns}#{slugify(name)}"
    cand, i = base, 2
    while exists(cand):
        cand = f"{base}_{i}"; i += 1
    return cand
```

#### 2.1.3 后端 API（`PUT /api/knowledge/ontology/types/{id}` 与 `POST` 新建）

- 新建类型时：若 body 未带 `iri`，则 `make_iri(namespace, name, strategy)` 自动生成。
- `PUT` 允许单独改 `iri`（需校验唯一性）；IRI 变更时同步更新 `ontology_owl.py` 导出与 SHACL 引用。

#### 2.1.4 前端编辑区顶部 IRI 行（`renderOntDesc` L10213 头部 + `renderOntDescAttr` L10397）

将现有标题行改为 WebProtege 式头部（含 IRI 行）：

```js
// renderOntDesc 内，替代 h 变量开头的标题 div：
h = `<div class="ont-edit-header">
  <div class="ont-edit-title">
    <span class="ont-edit-kind">${isRel?'ObjectProperty':'Class'}</span>
    <span class="ont-edit-name">${esc(t.name)}</span>
    <button class="icon-btn" onclick="ontEditTypeById(${t.id})" title="属性表/约束编辑（右侧滑窗）">✏️</button>
    <button class="icon-btn" style="color:var(--red)" onclick="deleteOntType(${t.id})" title="删除">🗑</button>
  </div>
  <div class="ont-edit-iri">
    <span class="ont-iri-label">IRI</span>
    <span class="ont-iri-val" title="双击修改 IRI" ondblclick="editEntityIri(${t.id})">${esc(t.iri||'（未生成）')}</span>
    <button class="icon-btn" onclick="copyEntityIri(${t.id})" title="复制 IRI">📋</button>
    <button class="icon-btn" onclick="editEntityIri(${t.id})" title="修改 IRI">🔗</button>
  </div>
</div>`;
```

新增两个前端函数：

```js
function copyEntityIri(id){ const t=(ontData.types||[]).find(x=>x.id===id); if(!t||!t.iri) return; navigator.clipboard?.writeText(t.iri); toast('已复制 IRI'); }
async function editEntityIri(id){
  const t=(ontData.types||[]).find(x=>x.id===id); if(!t) return;
  promptDialog({title:'修改 IRI', message:'修改 IRI 会同步更新导出与校验引用：', placeholder:t.iri||''}).then(async v=>{
    const iri=(v||'').trim(); if(!iri||iri===t.iri) return;
    const r=await api('/api/knowledge/ontology/types/'+id, {method:'PUT', body:JSON.stringify({...t, iri})});
    if(r&&r.error){ toast('修改失败：'+r.error); return; }
    toast('✅ IRI 已更新'); loadOntology();
  });
}
```

#### 2.1.5 CSS（`static/css/app.css` 追加）

```css
.ont-edit-header{border-bottom:1px solid var(--line);padding:8px 20px 10px;background:#fff;}
.ont-edit-title{display:flex;align-items:center;gap:8px;flex-wrap:wrap;}
.ont-edit-kind{font-size:11px;color:var(--mut);text-transform:uppercase;letter-spacing:.4px;}
.ont-edit-name{font-size:19px;font-weight:600;color:var(--blue-d);}
.ont-edit-iri{display:flex;align-items:center;gap:6px;margin-top:6px;font-size:11.5px;color:var(--mut);}
.ont-iri-label{background:#f5f5ee;border:1px solid var(--line);padding:1px 7px;border-radius:4px;font-size:10.5px;font-weight:600;letter-spacing:.3px;}
.ont-iri-val{font-family:'Cascadia Code',Consolas,monospace;color:var(--blue-d);cursor:pointer;word-break:break-all;}
.ont-iri-val:hover{text-decoration:underline;}
```

**验收**：任一已有类被选中后，编辑区顶部出现 `Class: 名称` + `IRI: http://...#名称`；双击可改，改后导出文件同步。

---

### 🔴 P0-2：顶部 Tab 重构为「实体维度」（页面级）

#### 2.2.1 替换 `#ont-subtab-row`（L460-463）

把「本体模型维护 / 图谱 / RDF」三个呈现 Tab 改为：

```html
<div class="subtab" id="ont-subtab-row" style="padding:6px 10px 0;">
  <span class="on" data-top="classes" onclick="switchOntTop(this,'classes')">类</span>
  <span data-top="props" onclick="switchOntTop(this,'props')">对象属性</span>
  <span data-top="dprops" onclick="switchOntTop(this,'dprops')">数据属性</span>
  <span data-top="individuals" onclick="switchOntTop(this,'individuals')">个体</span>
  <span data-top="comments" onclick="switchOntTop(this,'comments')">注释</span>
  <span data-top="changes" onclick="switchOntTop(this,'changes')">变更历史</span>
  <span style="flex:1"></span>
  <!-- 保留 导入/导出/版本历史/⚙设置 按钮，RDF/OWL 收进「导出」下拉或独立按钮 -->
</div>
```

> 说明：RDF/OWL 不再作为一级 Tab，改为工具行按钮（或保留在「⚙」下拉），避免与实体维度 Tab 语义混淆。

#### 2.2.2 状态拆分（L9789）

```js
let ontTopTab = 'classes';        // 实体维度：classes|props|dprops|individuals|comments|changes
let ontPaneMode = 'edit';         // 中栏视图：edit|graf
const ontPaneByTab = { classes:'edit', props:'edit', dprops:'edit', individuals:'edit', comments:'edit', changes:'edit' };
// 旧 ontView 保留为「兼容别名」或全局删除（改造后期清理）
```

#### 2.2.3 `switchOntTop(el, tab)`（新增，替代 `switchOntTab` 的实体维度部分）

```js
function switchOntTop(el, tab){
  ontTopTab = tab;
  el.parentNode.querySelectorAll('[data-top]').forEach(x=>x.classList.remove('on'));
  el.classList.add('on');
  // 左栏树由 TopTab 决定
  renderOntTree();
  // 中栏按该 Tab 记忆的 PaneMode 渲染
  ontPaneMode = ontPaneByTab[tab] || 'edit';
  switchOntPane(ontPaneMode);
  // 右栏按 TopTab 分流
  if(tab==='comments'){ renderOntComments(); return; }
  if(tab==='changes'){ renderOntChanges(); return; }
  if(tab==='individuals'){ renderOntIndividuals(); return; }
  renderOntSide();
}
```

**验收**：点「对象属性」Tab，左栏立即变为对象属性列表（不再是左栏内小 tab），中栏投影当前选中属性；点「类」Tab 恢复类树。

---

### 🔴 P0-3：左栏收敛为「唯一选择源」

#### 2.3.1 移除左栏 4 态 tab（L528-531）

删除 `<span class="ont-vtab" ...>` 的「图谱」入口（`ont-view-graf`），左栏只保留标题 + 过滤框 + 树。左栏树内容**完全由 `ontTopTab` 决定**：

```js
// renderOntTree() L10032 改造：不再判断 isGraf，直接按 ontTopTab 分流
const view = ontTopTab;   // 原：isGraf ? ontGrafDim : ontView
const clk = name => `selectOntType('${ontJs(name)}')`;
```

#### 2.3.2 `selectOntType` 简化（L9855）

```js
function selectOntType(name){
  const t=(ontData.types||[]).find(x=>x.name===name); if(!t) return;
  ontSelected={kind:'type', id:t.id, name};
  renderOntTree();         // 树高亮跟随
  switchOntPane(ontPaneMode); // 中栏投影当前选中（编辑或图谱）
  renderOntSide();
}
```

> `keepView` 参数废弃；原「图谱语境点节点看浮层」逻辑收敛到 `ontPaneMode==='graf'` 分支。

#### 2.3.3 焦点下钻保留

`ontFocus` / `ontFocusSet` / `ontFocusBreadcrumb` / `ont-focus-bar`（L556-567、L9931-9934）逻辑**整体保留**，仅在 `ontPaneMode==='graf'` 时生效。

**验收**：左栏无论中栏是编辑还是图谱，都稳定显示当前实体维度的树；点击树节点，中栏投影切换，树高亮不闪烁。

---

### 🔴 P0-4：中栏内「编辑⇄图谱」toggle

#### 2.4.1 中栏顶部加 toggle（`#ont-main` L537 内，`#ont-desc` 之前）

```html
<div id="ont-main">
  <div id="ont-pane-toggle" style="display:flex;align-items:center;gap:4px;padding:6px 12px;border-bottom:1px solid var(--line);">
    <!-- 由 renderOntPaneToggle() 注入两段式按钮 -->
  </div>
  <div id="ont-desc" ...></div>
  <div id="ont-rdfbar">...</div>
</div>
```

#### 2.4.2 `switchOntPane(mode)`（核心函数，新增）

```js
function renderOntPaneToggle(){
  const bar=document.getElementById('ont-pane-toggle'); if(!bar) return;
  bar.innerHTML = `
    <button class="ont-vseg ${ontPaneMode==='edit'?'on':''}" onclick="switchOntPane('edit')">📝 编辑</button>
    <button class="ont-vseg ${ontPaneMode==='graf'?'on':''}" onclick="switchOntPane('graf')">🕸 图谱</button>`;
}
function switchOntPane(mode){
  ontPaneMode = mode;
  ontPaneByTab[ontTopTab] = mode;
  const desc=document.getElementById('ont-desc');
  const graf=document.getElementById('ont-graph');
  if(mode==='edit'){
    if(desc) desc.style.display='';
    if(graf) graf.style.display='none';
    renderOntDesc();
    renderOntSide();
  } else {
    if(desc) desc.style.display='none';
    if(graf) graf.style.display='flex';
    // 图谱默认以左栏当前选中为焦点（1-hop 邻域）
    const t=ontCurType();
    if(t && t.type_kind==='entity'){ ontFocus=t.name; ontFocusBreadcrumb(); }
    renderOntGraph();
    renderOntSideDetail();
  }
  renderOntPaneToggle();
}
```

#### 2.4.3 废弃 `ontSyncViewPanes` 的全局显隐逻辑（L9917）

原 `ontSyncViewPanes()` 在 graf 时隐藏整个 `#ont-main`，改为**不再隐藏** `#ont-main`，只让 `switchOntPane` 控制其内部两个子容器。`#ont-graph` 从「order:3 的独立 flex 项」改为「`#ont-main` 内部与 `#ont-desc` 平级的子容器」，或保持独立但由 `switchOntPane` 直接切换 display。

> 布局建议：将 `#ont-graph` 移入 `#ont-main`，成为中栏的第二个子视图；这样「左栏恒定、中栏投影、右栏详情」三栏结构最干净。

**验收**：中栏顶部两段式按钮 `📝 编辑 / 🕸 图谱` 切换时，左栏树与右栏详情纹丝不动；图谱模式下中栏显示以选中节点为中心的子图，点「编辑」切回字段表单。

---

### 🟡 P1-5：个体 / 注释 / 变更历史 Tab（复用现有数据）

| Tab | 数据源 | 现状 |
|---|---|---|
| 个体 | `knowledge_entities` + `binding.entity_type` | 右栏仅显示 `binding.total` 计数（L10257），需新建 `renderOntIndividuals()` 列实例清单 |
| 注释 | `rdfs:comment` / 变更日志 | 右栏已有「注释」「变更历史」折叠（L10261、L10274），抽成独立 Tab 即可 |
| 变更历史 | `ontology_versions` + `knowledge_commits` | `toggleOntChangeLog(id)` 已有（L10274），抽成 Tab 级列表 |

**验收**：三个 Tab 都能作为左栏之外的独立页面展示，数据复用现有后端，无新表。

---

### 🟡 P2-6：Project Settings（命名空间 / IRI 策略）

- 本体页右上角加 `⚙` 按钮 → 右侧滑窗（复用 `.slide-panel` 组件）。
- 字段：`namespace_prefix`（默认 `http://example.org/mbse#`）、`iri_strategy`（`hash-name` / `uuid`）、`default_prefix`。
- 写 `ontology_meta` 表，影响后续所有 `make_iri()` 调用。

---

## 3. 改造顺序与依赖

```
P0-1 (IRI 主轴) ─────────────┐
                              ├─► P0-4 (中栏 toggle) ─► P1-5 (个体/注释/变更 Tab)
P0-2 (顶部实体维度 Tab) ──────┤                              │
                              │                              ▼
P0-3 (左栏收敛) ──────────────┘                         P2-6 (Settings)
```

- P0-1 与 P0-2/P0-3 可并行（一个动后端+头部，一个动布局），但 P0-4 依赖 P0-2/P0-3 的状态拆分完成。
- 建议先做 **P0-1 + P0-4 的最小切片**（IRI 展示 + 中栏 toggle），快速看到 WebProtege 效果；再铺 P0-2/P0-3 的顶部 Tab 与左栏收敛。

---

## 4. 风险与回滚

| 风险 | 缓解 |
|---|---|
| `ontView` 四态 → 两层状态，改动面大、易回归 | 分阶段提交，每阶段保留 `ontView` 兼容别名；图谱页（kb-d 实例图谱）不受影响（独立 subpage） |
| IRI 回填冲突（中文名 slugify 后重复） | `make_iri` 带 `exists` 回调自动追加 `_2/_3`；回填脚本幂等 |
| 左栏「图谱」tab 移除后老用户习惯丢失 | 保留中栏「🕸 图谱」按钮作为唯一入口，并加 tooltip 引导 |
| 个体 Tab 数据量大 | 个体列表分页加载（复用 `binding` 计数先行） |

---

## 5. 关键文件与行号索引

| 文件 | 行号 | 内容 |
|---|---|---|
| `static/index.html` | 458-492 | kb-c subpage + 顶部 `#ont-subtab-row` |
| `static/index.html` | 495 | `#ont-pane-edit` 工作台 |
| `static/index.html` | 524-535 | 左栏 `#ont-left`（含 4 态 tab） |
| `static/index.html` | 537-545 | 中栏 `#ont-main` |
| `static/index.html` | 547-550 | 右栏 `#ont-side` |
| `static/index.html` | 551-585 | 图谱画布 `#ont-graph` |
| `static/index.html` | 9095 | `switchOntTab()`（呈现形式 tab） |
| `static/index.html` | 9789 | `ontView` 四态声明 |
| `static/index.html` | 9855 | `selectOntType(name, keepView)` |
| `static/index.html` | 9917 | `ontSyncViewPanes()` |
| `static/index.html` | 9936 | `ontLeftTab(view)` |
| `static/index.html` | 9989 | `ontSetView(v)` |
| `static/index.html` | 10032 | `renderOntTree()` |
| `static/index.html` | 10072 | `ontPutType()`（写回，需加 iri） |
| `static/index.html` | 10091 | `ontCurType()` |
| `static/index.html` | 10197 | `renderOntDesc()`（头部加 IRI 行） |
| `static/index.html` | 10394 | `renderOntDescAttr()` |
| `static/index.html` | 10241 | `renderOntSide()` |
| `static/index.html` | 9955 | `renderOntSideDetail()` |
| `static/index.html` | 10874 | `renderOntGraph()` |
| `database/schema.py` | 188-195 | `ontology_types` 建表 |
| `database/migrations.py` | 382-385 | `ontology_types` 补列 |
| `ontology_semantics.py` | 全文 | 语义层（加 `make_iri`） |
| `ontology_owl.py` | 全文 | 导出（IRI 落三元组） |

---

## 6. 待拍板决策点（阻塞项）

| # | 决策点 | 建议 | 阻塞范围 |
|---|---|---|---|
| D-1 | IRI 生成策略默认值 | `hash-name` | P0-1 |
| D-2 | 是否做「个体」Tab | 建议做（P1） | P1-5 |
| D-3 | 「变更历史」是否入顶部 Tab | 建议入（复用现成） | P0-2 |
| D-4 | 是否做 Project Settings ⚙ | 建议做（P2） | P2-6 |
| D-5 | 多继承（parent_id 单列 → 多对多） | 不在本轮，另开任务 | 无 |

> 其中 D-1 是 P0-1 开工的前置条件；D-2/D-3 影响 P0-2 的 Tab 数量；建议米爸先拍 D-1 和 D-3。
