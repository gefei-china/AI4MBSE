# index.html 渐进式组件化路线图（P2-3 v1，2026-09-01）

> 背景：`static/index.html` 单文件 1.28MB（18920 行），内联 CSS ~60KB + 内联 JS ~1MB。
> 目标：不引入构建工具、保持私有化离线部署（零外部 CDN），渐进式拆分为可维护结构。

## 已完成（v1，本次落地）

| 步骤 | 内容 | 风险 |
|------|------|------|
| 1 | 主样式表（56.7KB）→ `static/css/app.css`，index.html 用 `<link>` 引用 | 低（纯样式外置，行为一致） |
| 2 | 建立 `static/css/`、`static/js/` 目录约定 | — |
| 3 | 外部库已本地化：`static/vendor/`（cytoscape/dagre）+ `static/view_layout_engine.js` | 既有 |

## 后续路线（按风险递增）

| 阶段 | 动作 | 收益 | 风险 |
|------|------|------|------|
| v2 | 两个小 `<style>` 块（4KB）并入 app.css | 样式单点维护 | 低 |
| v3 | 内联 JS 中**纯工具函数群**（`api()`/`esc()`/`toast()`/日期格式化等，约 30 个函数）提取到 `static/js/core.js`，置于内联 script 前加载 | 主 script 减重 ~5% | 中（需保加载顺序） |
| v4 | 按功能域分片：`static/js/plugins.js`（unified 插件市场）、`static/js/skills.js`（技能/草稿箱）、`static/js/agents.js`（Agent 团队/绑定）、`static/js/kb.js`（知识库） | 主 script 减重 40%+，功能域隔离 | 中高（函数互相引用，需逐片提取+回归） |
| v5 | 页面 DOM 组件化：Modal/Table/Chip 抽为可复用组件函数 | 新增功能不再改单文件 | 高（大重构，需大量回归） |
| v6 | （可选）引入 ES Module + 本地构建（保留离线部署） | 工程化 | 高，需重定部署 |

## 每阶段必做验证

1. `node --check` 提取的内联 JS（语法）
2. 起服务后逐页冒烟：总览 / AI 建模 / 本体模型 / 文档库 / 能力中心（Agent/技能/工具与MCP/插件市场）/ 草稿箱
3. 关键交互：插件安装、技能发布、Agent 绑定、术语词典
4. 回归脚本：`tools/verify_*.py`（market/artifacts/agent_arch/agent_team）

## 约束（私有化离线部署）

- 禁止引入 CDN 运行时依赖（现有 vendor 本地化先例：cytoscape/dagre）
- 静态文件路径全部走 `/static/...` 相对服务根
- 不引入构建步骤，产出物即部署物（当前 NoCache 静态服务已兼容外部 css/js）
