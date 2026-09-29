# Design Contract · AI4MBSE 知识看板 UX 预览（导航减层 + 筛选态下钻）

> 用途：两项 UX 改进的**高保真静态原型**（只出效果，不改工程代码）
> 交付：`mbse_system/docs/mockups/kb-ux-nav-drill/`（自包含、离线可打开）

## Style Tier & Aesthetic Direction
- style: **brand-themed（复刻既有系统）** —— 本原型的第一原则是"看起来就是这个系统的页面"，不做个人发挥
- aesthetic: 后台工程系统的**高信息密度克制风**：小字号、细边框、白面板 + 深色底、蓝色单一强调色
- tone keywords: 克制 / 清晰 / 高密度 / 层级可辨

## Tech Stack
`vanilla HTML + CSS + JS` / **pure-static**（零外部依赖、双击即可离线打开）

## Design Tokens（与 mbse_system/static/css/tokens.css 对齐，**不得自创**）
```
color.primary      #185FA5     （主色 / 选中实心 / 图标）
color.primary-deep #0C447C     （深蓝强调：数字、面板内强调文字）
color.primary-soft #E6F1FB     （浅蓝卡底）
color.text         #1a2332     （正文，页面底色上用；暗色下翻转 #e5e9f0）
color.text-sub     #5a6678     （面板内次级文字，**不随主题翻转**）
color.text-on-bg   #8a96ad     （暗色下**页面底色**上的次级文字）
color.border       #d5dce6
color.line         #e5e9ef     （表内分隔线）
color.bg           浅 #eef1f6 / 暗 #0e1626      （页面底色）
color.surface      #ffffff     （面板：**暗色下仍为白底**）
color.ok/warn/alert  #2F7D4F / #BA7517 / #C0392B（配浅底 #E7F5EC / #FAEEDA / #FDECEA）
font.body  -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif
font.mono  ui-monospace, Consolas, monospace
font.scale 11 / 12 / 14 / 18 / 24 (px)
radius     chip 999 / card 10 / panel 12
shadow     panel 0 1px 3px rgba(16,32,64,.06)
spacing    base 4px（4/8/12/16/24）
layout     sidebar 200px 固定；内容 max-width 1320px；指标卡网格 minmax(190px,1fr)
motion     视图切换 160ms ease-out；chip hover 120ms；加载骨架 pulse
```
**暗色铁律**：面板恒白底 ⇒ 面板内文字用 `#5a6678 / #0C447C`（不翻转）；
只有落在**页面底色**上的文字才用翻转色 `#e5e9f0 / #8a96ad`。

## Component Spec
- **chip**：圆角 999，padding 3px 10px，11px，白底 + 1px 边框；`hover` 边框转主色；`on`（实心）= 主色底白字
- **描边式选中（页内视图）**：白底 + 主色边框 + 深蓝文字 + 600 字重（与实心区分）
- **panel**：白底 + 1px `--border` + radius 12 + 轻阴影；`panel-hd` 高 38px，左侧标题右侧操作
- **filter-chip**：主色浅底 `#E6F1FB` + 深蓝文字 + 右侧 `×`（Lucide `x`，14px）；hover 加深边框
- **breadcrumb**：`知识中心 / 数据看板 › [指标名] → 文档库`，末段为主色；11px
- **list-row**：高 40px，hover 浅蓝底；左主文右次要信息
- **empty-state**：居中，Lucide `check-circle`（成功态）或 `inbox`（无数据），主文 12px + 次文 11px
- **loading**：3 行骨架，`pulse` 1.2s

## Icons
单图标库 **Lucide（内联 SVG，离线零依赖）**，stroke 1.75，size 14/16。
用到：`x`（清除筛选）、`chevron-right`（面包屑）、`chevron-down`（导航展开）、`check-circle`、`inbox`、`info`、`filter`、`corner-down-right`（下钻来源）。
例外说明：既有系统的导航 chip 使用 emoji（📊📄🛡🕸🧬📖 / 🧭🧪⚡🧾），为"贴合既有设计"**在导航位置保留原 emoji**（这是被复刻对象的设计语言）；**新增**的图标一律用 Lucide 内联 SVG，不新增 emoji。

## App Shell + 改进前/后
- 单一 `index.html`，顶部一个"预览控制条"（不属于被设计对象，仅用于切换）：
  `① 导航减层 | ② 筛选态下钻` × `改进前 | 改进后` × `浅色 | 暗色`
- shell：左侧 `aside.sidebar`（固定 200px）+ 右侧 `main`（面包屑行 + 内容）
- **改进前**：左侧一级导航 + 内容区顶部「知识中心」6 个内层 Tab + 其下看板簇 4 个 Tab（现状两层半）
- **改进后（主推 A）**：左侧「知识中心」展开为二级菜单（6 子项，当前项高亮+竖条），内容区顶部**只剩一排**看板簇 Tab；原顶部的「跳转子页」chips **删除**（左侧树已承担跳转）
- **备选 B（同页可切换）**：内容区顶部一排统一 segmented 控件（6 页面 + 分隔符 + 4 视图）
- active 规则：左侧子项 `.is-active`；视图 chip `.on`

## Page List（同一 HTML 内的两个视图）
1. `view-nav` 导航减层：before / after(A) / after(B) 三态对照
2. `view-drill` 筛选态下钻：场景 A 未链接分块（有数据 5758/5758 未链接）、场景 B 待审队列（真实空态 0 条）

## Mock Schema（见 mock.js，真实数据，非占位）
```js
DB.metrics   { key, name, value, unit, status, target, calc }   // 关键指标（含计算说明）
DB.scopes    release{entities:61,relations:83,published:61}/branch{entities:63}/global{entities_dedup:61,relations_dedup:83,documents:6,chunks:5758}
DB.chunks    分块列表（source_doc / section / 摘要 / linkedCount=0），用于场景 A
DB.candidates 候选（triples 0 + v2g 0 → 空态），用于场景 B
```
## API Stub
```
GET /api/documents/chunks?linked=none&limit=   → { code, data:[chunk], total }
GET /api/knowledge/v2g/candidates?status=candidate&limit= → { code, data:[cand], total }
```
（`api.js` 内带 `delay()` 与 `// TODO: replace with fetch(...)` 标注，形状即未来真实接口）