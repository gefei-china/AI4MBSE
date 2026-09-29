# AGENTS.md — AI4MBSE 项目导航（给 AI 编码助手看的唯一入口）

> 2026-09-17 建立。**改代码前先读本文件**，它告诉你"改哪个功能只需看哪几个文件"，
> 避免通读 1,541 行 index.html 与全部 40 个阻塞脚本（那是当前 token 成本的最大来源）。
> 禁读区见 `.cursorignore` / `.traeignore` / `.aiignore`（三份内容一致）。

---

## 1. 技术栈与铁律

- 后端：FastAPI + SQLite（`main.py` 只做装配；业务在 `routers/` / `services/` / `repositories/`）
- 前端：**原生 JS，无框架、无构建、无打包**（`static/index.html` + `static/js/mods/01..37-*.js`）
- 铁律 1：前端模块在**全局作用域**，页面用内联 `onclick="fn()"` 调函数 → **改函数名/挪文件会静默失效**，改完必须浏览器验证。
- 铁律 2：**`index.html` 已无任何内联 `<script>` / `<style>`**（2026-09-18 S6 迁完，220,607 → 200,435 字节 / **1,541 行**，42 个 `<script src>`，内联计数均为 0）。
  ⚠️ 行数只作参考：**本文件与各类文档中的行号引用会随每次结构变更持续失效**，定位请写 `模块.符号`（实测 `esc` 在 `01-core.js:16`、`escA` 在 `:18`、`window.toast` 在 `:70`）。
  新增逻辑一律新建 `static/js/mods/NN-xxx.js` 并加 `<script src>`（**注意加载位置**：依赖页面 DOM 的模块要放在该 DOM 之后）。
- 铁律 3：新增/修改后端功能**必须走 `services/` + `repositories/`**，不要在 router 里直接写 SQL（存量 ≈971 处是历史债，只做新老划断）。
- 铁律 4：**私有化离线部署**，不引入外部 CDN / 构建链 / 新依赖。

## 2. 功能 → 文件映射（改这个功能，只看这几处）

| 我要改… | 前端 | 后端 |
|---|---|---|
| 导航/路由/面包屑/标题 | `js/mods/02-shell.js`、`index.html`（mainnav） | — |
| 命令面板 KBar / 全局快捷键 / 主题 | `js/mods/37-kbar.js` | — |
| toast / 错误浮层 / 全局错误兜底 / alert 桥接 | `js/mods/01-core.js`（唯一实现，S6-4 起） | — |
| 知识域**五个**顶层 Tab（数据看板 / 资料库 / 图谱工作区 / 本体模型 / 术语词典），默认落点=数据看板(kb-a) | `index.html`（`#kbhub-tabs` → chip `kbhub-tab-a/e/d/c/t`）、`js/mods/15-kb.js`（`loadKBTab`） | `routers/knowledge_parts/*` |
| 会话列表 / 新建任务 / 角色快捷 / **澄清作答与中断固化**（`/clarify-answer` 支持 `free_text`、`/messages/partial`、消息接口回传 `pending_clarify`） | `js/mods/03-chat.js`、`12-chatsend.js`、`11-pipeline.js`（澄清卡/提示条/执行详情） | `routers/conversations.py`、`repositories/conversation_repo.py` |
| 左侧「项目」组（项目 = 任务容器；数据来源 = 本地工作空间 / 远端 SSH；项目内任务列表） | `js/mods/41-projects.js`、`index.html`（`#gnav-project`）、`css/gnav.css`（§16/16.1） | `routers/projects.py`、`repositories/project_repo.py`、`database/migrations/columns.py`（`projects.source/workspace/remote`） |
| 消息渲染 / Markdown / 卡片 | `js/mods/05-markdown.js`、`06-cards.js` | `routers/conversations.py` |
| 归一确认（人在回路） | `js/mods/07-norm.js`、`06-cards.js`（入口按钮） | `norm_apply.py`、`routers/conversations.py` |
| 文档库（文档上传/列表/生命周期） | `js/mods/20-docs.js` | `routers/meta.py`（`/api/documents/*` 共 20 个端点）、`knowledge_pipeline/ingest.py` |
| 知识库页装载 / 分页组件 | `js/mods/15-kb.js` | `routers/knowledge_parts/*` |
| 本体模型 / 术语词典 | `js/mods/21-ontology.js`、`23-ontform.js`、`18-glossary.js` | `ontology_*.py`、`routers/glossary.py` |
| 知识图谱（视图/编辑/推理） | `js/mods/25-graphview.js`、`26-grapheditor.js`、`34-graphtabs.js`、`22-ontgraph.js` | `graph_db.py`、`triple_store.py`、`routers/graph_workspace.py` |
| 分支 / MR / 合并 | `js/mods/27-branch.js`、`24-graph.js` | `repositories/branch_repo.py`、`routers/branches.py` |
| 能力中心（Agent/技能/工具/插件） | `js/mods/28-studio.js`、`30-agents.js`、`36-capability.js` | `plugin_system/*`（插件数据访问层在 **`plugin_system/store/`**，2026-09-18 由 `store.py` 拆包）、`routers/plugins.py`、`routers/studio_parts/*` |
| 影响分析 / 仿真 / 一致性 | `js/mods/09-impact.js` | `services/impact_engine.py` |
| 报告 | `js/mods/13-reports.js` | `report_generator.py`、`routers/reports.py` |
| 用户/角色/权限 | `js/mods/31-admin.js` | `routers/users.py` |
| 审计 / 运行监控 | `js/mods/19-governance.js`、`31-admin.js` | `routers/meta.py`、`governance.py` |
| Agent 流水线 / 提示词装配 | （运行时，无 UI） | `agent/pipeline_parts/*`（`stream.py` 为 SSE 主路径；`common.py` 是唯一 shared） |
| SysML 解析 / 导入 | `js/mods/14-sysml.js`、`08-sysmlview.js` | `sysml_importer.py`、`sysml_profile.py` |
| 视图→图谱（v2g） | `js/mods/17-v2g.js` | `vector2graph.py` |
| 建表 / 补列 / 数据迁移 | — | `database/migrations/*`（S7-3 起按域分 8 个文件）、`database/schema.py`（**唯一定序编排者**，顺序即语义） |
| 配置 / 参数 | `js/mods/35-ctxconfig.js` | `core/config.py` |

## 3. 必知的 51 个坑（都是踩过的）

1. **`esc` / `escA` 定义在 `static/js/mods/01-core.js`**（2026-09-18 S6-1 从 `08-sysmlview.js:151/153` 迁入，因它是**最先加载**的模块），却被 35 个文件约 1,700 处调用 → 拆它、或调整模块加载顺序前，必须先确认它仍最先加载并做浏览器回归。
2. **`toast` 只有一份实现，在 `01-core.js`**（2026-09-18 S6-4 合并）。它是「可直接调用 + 挂方法」的混合体：`toast('x')` / `toast('x', 2000)` / `toast.success('x')` 都行。**不要**再在别处定义 toast，更**不要把普通对象赋给 `window.toast`** —— 历史事故：那样会覆盖函数声明，全站 700+ 处 `toast('...')` 抛 TypeError，表现为「点保存没反应、无任何提示」。同批迁入的还有 `errOverlay`、全局 error / unhandledrejection 兜底、`alert` 桥接。
3. **面板高度多为内联样式**（如 `07-norm.js` 的 `#nr-rows` 340px）→ 用 CSS 改高度必须 `!important`。
4. **`.msg` 带 `content-visibility`**，会成为 `position:fixed` 的包含块 → 会话内浮层想铺满视口，必须把节点搬到 `<body>` 再按锚点归位。
5. **导航列是 column flex**：`.gnav nav`（主导航）必须 `flex:0 0 auto`，否则放宽任务列表会把它压出滚动条、剪掉最后几项。
6. **静态资源 no-cache**：改前端**不用重启服务**，刷新即生效；改后端需重启。
7. **数据库只有一个 `mbse.db`**，服务运行时被占用 → 只读查询请用 `file:...?mode=ro`；**不要**在服务运行时做破坏性写库操作。
8. **不要用 bash 工具**（本机 shim 缺 coreutils）；用 PowerShell。
   **写文件**：一律 `| Out-File <路径> -Encoding utf8`，**禁用 `>` 与 `>>`** —— PS 5.1 下两者都按 **UTF-16LE** 写（实测写新文件均以 `FF FE` 开头）。
   ⚠️ **`>>` 比 `>` 更危险**：它会把已有 UTF-8 文件追加成**混合编码**（实测头部仍是 `EF BB BF`、尾部却是 `79 00` 的 UTF-16LE 字节）——而**只检查头部 BOM 会误判为正常**。
   另：PS 5.1 的 `Out-File -Encoding utf8` 产出的是 **UTF-8 带 BOM**；需要**无 BOM** 时（如 git 提交信息文件）必须用
   `[System.IO.File]::WriteAllText($p,$s,(New-Object System.Text.UTF8Encoding($false)))`。
   **读/数文件**：⚠️ **`Get-Content` 与 `Measure-Object -Line` 各有一个独立陷阱，叠加后行数会严重偏低**（本文件与 `docs/代码优化方案-20260917.md` 里「74 个模块 / 1,529 行 / 1334 行 / 20 个无调用点函数」等数字全是被污染口径的产物，**勿再引用**）：
   - **陷阱①（主因）`Measure-Object -Line` 会跳过空行** —— 与编码无关。实测一个人造 7 行（含 3 空行）的文件，`Get-Content f | Measure-Object -Line` 计为 **4**。
   - **陷阱②`Get-Content` 对 UTF-8 无 BOM 文件误判编码** —— 不加 `-Encoding UTF8` 时按 ANSI/GBK 解码，多字节字符被拆开、顺带吞掉换行符。实测 `sysml_importer.py`：`(Get-Content).Count` = **1604**，加 `-Encoding UTF8` 后 = **1710**。CRLF 文件常侥幸不受影响（`\r` 兜底），**但那是侥幸、不是保证**。`-Raw` 同样绕不过。
   - **正确姿势**：`[IO.File]::ReadAllLines($p,[Text.Encoding]::UTF8).Count`，或 Python `len(open(p,'rb').read().decode('utf-8','replace').splitlines())`。
   - **交叉验证法（一次锁定两个机制）**：`Get-Content -Encoding UTF8 <f> | Measure-Object -Line` = 真实行数 − 空行数。实测 sysml_importer：1710 − 88 = **1622** ✓。
9. **工具名必须是 ASCII**：`tools` 表的工具名会作为 `function.name` 发给模型，协议要求 `^[a-zA-Z0-9_-]+$`。曾有一个中文名工具（「知识库查询」）导致 **整批 tools 载荷被 400 拒绝 → 静默回落 Mock**。`_build_tools_def` 已加护栏（剔除非法名 + 告警），但新增工具请直接用英文名。
10. **LLM 采样参数优先级**：`显式传参 > DB llm_providers 配置 > 内置默认`（`llm/providers/openai_compat.py`）。调用 `llm_client.chat(...)` 时可直接传 `model`/`temperature`/`max_tokens` —— 历史上 broker 曾把它们「具名 + `**kwargs`」重复传递，导致一传就 `TypeError` 并被静默吞成 Mock，已修但改动此处务必回归 `tools/verify/verify_s4_llm_params.py`。
11. **降级必须留痕**：LLM 调用失败会回落 Mock（用户无感）。`llm/__init__.py` 的降级分支已加 WARNING（含调用点/intent/provider/异常）；排查"AI 回答怪怪的"时**先看服务日志有没有这条 WARNING**，再查 `llm_usage_stats.used_mock`。
12. **迁移 CSS/HTML 块不要用正则跨行匹配**：`<style>(.*?)</style>` 会命中内联 JS 字符串里的标签，删出未闭合标签（浏览器会把后续内容当 CSS 吞掉）。**按行定位**（开/闭标签独占一行）。
13. **注释必须闭合，且要验证"规则出现在解析后的样式表里"**：只读文件内容会漏掉"整段规则被未闭合注释吞掉"的静默失效——查 `document.styleSheets` 的 `cssRules` 才算数。
14. **`.gitignore` 的模式必须锚定根目录**：不带前导 `/` 的模式在**任意深度**匹配。`_*.py` 曾把全部 14 个包的 `__init__.py` 一并忽略（tracked=0），导致**全新克隆无法复现项目**（2026-09-18 修复为 `/_*.py`）。新增忽略规则后，请用 `git check-ignore -v <你不想被忽略的关键文件>` 反查一遍。
15. **拆前端死代码，禁止"按候选清单直接删"，必须过三步判定**（2026-09-18 S5 复检的结论，血泪）：
   ① **可达性**（`tools/verify/analyze_frontend_reach.py`：从 index.html 内联调用/顶层代码/其它 js/`window.X=` 四类种子做调用图闭包）；
   ② **悬空引用硬检查**（把被删名当整词搜**保留**语料）；
   ③ **有无*可达*的后继实现** —— **无活后继 ⇒ 一律不删**，就地标注「断链/未接线，留待产品决策」。
   - **旧法（全语料计数==1）不会多删、但会漏报**「只被死函数引用的连锁死代码」→ 两法口径不可混用。
   - **证明"没删错"的唯一硬标准是 A/B**：`git worktree add --detach <dir> HEAD` 后同一份分析逻辑跑两棵树，**可达数必须完全相等**。
   - **扫描器必须按 `{}` 深度 + 帧栈词法器**，不能用括号计数、也不能用"定义之间的空隙"近似：本仓有**套娃模板串**（`10-chatinput.js`）会漂移；漏采顶层代码会把 `document.addEventListener('click', …closeToolPop())` 里的活函数判死。
   - **检查器自身有两个必踩假阳性**：ⓐ 被删函数的名字**出现在注释里**（含工具/skill 自己的注释）会被判成断链（同 `ONT_ATTR_ESC` 事故）→ 搜索前按行屏蔽注释；ⓑ `tmp/` 冒烟脚本里 `typeof foo` 是**"断言已删除"的探针**、`tools/verify/_fe_*.json` 是分析器产物 → 语料必须**分级统计**，否则"删除后断言不存在"的测试永远 FAIL。
   - **整块失效的功能簇不要只删一半**：删函数时一并清掉它独占的模块级变量与上方注释（例：删 `artApplyWidth/artDragMove/artDragEnd` 时须同清 `let _artDrag`），否则留下误导性残骸。
16. **`tools/split_*.py` 是「一次性生成器」，重跑会静默覆盖整个源码目录**（2026-09-23 标注，隐患实测）：
    `split_pipeline.py` → `agent/pipeline_parts/*.py`；`split_router_knowledge.py` → `routers/knowledge_parts/*.py`；
    `split_router_studio.py` → `routers/studio_parts/*.py`（更早的 `split_index_html.py` 依赖已移走的 `_archive/bak/`，**再跑即报错**）。
    - ⚠️ **每个分片头部的原话「由 … 机械切分，勿手工编辑」已作废、且是反向误导**：分片如今就是**普通源码**，几个月来一直在被直接手工修改（修 bug、加断言、改口径都在分片里）。照那句话理解 → 会以为"改了也没用/要从生成器改"，**方向完全反了**。
    - **真正的风险是重跑**：生成器的输入（`agent/pipeline.py` 等）早已退化成 10~105 行的**薄入口**，重跑会以薄入口为输入产出**空/错误分片**并覆盖 `*_parts/`，**静默丢掉此后全部手工改动**。三个生成器的 docstring 顶部已补「⚠️ 已执行完毕、不可重跑」，`*_parts/` 下 **35 个 `.py`**（13+10+12，含 `__init__.py`/`common.py`/`shared.py` 等伴随文件）的头部口径也已同步改写。
    - **正确姿势**：① 要改分片就**直接改分片**；② 需要新的拆分时**新写一个只跑一次的脚本**，并在 docstring 顶部写明「⚠️ 已执行完毕、不可重跑」；③ 拆分前备份留在 `_archive/bak/*.pre-split`，别指望"用脚本重生成"当回滚手段。
17. **导航列两个分组都是「可收缩」flex 项时，会互相抢空间 → 视觉重叠**（2026-09-24 实测，左侧「项目 / 任务」两组的报障根因）：
   `.gnav` 是 column flex，`#gnav-project{flex:0 1 auto}` 与 `#gnav-task{flex:1 1 auto}` **都是 shrink=1**，
   而任务组有 80 条会话 → 它的 `flex-basis` 极大，收缩量按 `(shrink × basis)` 分摊到**所有**可收缩项 →
   **项目组被一并压扁到只剩一行高**（实测 projBodyH 只有 1 行 31px），它自己的 `overflow` 又把这一行切成半行 →
   渲染出来就是"项目行与下方任务项叠在一起"。
   **规避**：同一列里「按内容定高的组」必须 `flex:0 0 auto`（不参与剩余空间的收缩分摊），
   由需要占满剩余的组独占 `flex:1 1 auto` + `min-height:0` + 内部 `overflow-y:auto`。
   **判据**：不要只用 `getBoundingClientRect().bottom <= next.top` 断言"没重叠"（这只能查出真实交叠），
   要**同时量出分组自身高度与内容高度**（`clientHeight` vs `scrollHeight`）——"被压扁成一行 + 内部裁切"
   在矩形相交判据下是**检测不到的假绿**。
18. **绝对定位的坐标基准是祖先的 padding box（已含 border），不是 border box 的外沿**（2026-09-24 实测）：
   给 `.gp-it{border-left:3px solid transparent;padding-left:14px}` 的行内箭头写 `left:3px` 时，
   箭头盒实际落在 3(border)+3(offset)=6px → 右缘 20px，**压住 17px 起点的行文字**（实测 `arrInsideRow=false`）。
   **规避**：写 `left:0` 即为"紧贴内容区左缘"；改完用矩形包含断言（`arr.right <= name.left`）自证，别靠肉眼。
   **配套纪律**：行内装饰元素（树形展开箭头等）**不要占文档流宽度** —— 否则它会把行文字整列推右
   （本次实测推右 **34px**，即"项目名与任务标题不同列"的直接原因）；一律绝对定位进左侧边槽，
   让两组行的文字左缘落在同一基准（`.task-it` 的 `border-left 3px + padding 14px` = 17px）。
19. **「删除父对象」必须连它的指针一起收拾**（2026-09-24 实测，删项目踩到）：
   `settings.default_project_id` 是「当前工程」指针，删掉被指向的项目后若不清空，指针就**悬空**——
   现象是顶栏「⚠ 未匹配工程」、前端「当前工程」高亮与项目组自动展开**全部失效**（`/api/projects/default`
   因 `get_project()` 查不到而返回 `unset`），而调用侧只看到"功能莫名不工作"。
   **规避**：任何 delete 端点先查「我是不是别人指向的目标」，是则**一并清空/改指**，并把这件事
   写进返回体与审计（`cleared_default`），让前端能把后果提示给用户。置空是既有合法状态
   （见 `routers/projects.py::get_default_project` 的 unset 语义），不要自作主张改指别的对象。
20. **「只在浏览器内存里收口」的状态，刷新即丢**（2026-09-25 实测，中断输出丢失的根因）：
   流被中止时前端 `finalizeStopped` 只做 `renderMessage({id:0, ...})` —— **没有落库**；后端在客户端
   断开时 `raise GeneratorExit` 也直接放行（`routers/conversations.py` 的 `event_stream`）。
   于是"停止生成"/"流式中直接发新消息"两条路径都是：AI 已吐出的内容**只存在于当前 DOM**，刷新即无。
   **规避**：凡是"用户可见且应该保留"的产出，收口时必须走后端固化一次（本仓：前端
   `commitPartialStream()` → `POST /api/conversations/{id}/messages/partial`，并用返回的真实
   `message_id` 重渲染 DOM，`dataset.committedId` 做**幂等**防重复写）。
   **判据**：断言不能只看 DOM，必须"刷新后再查一次"或直查库（本次用 `messages` 表行数 + id 一致性自证）。
21. **对话级「等待用户」状态必须双向显式化**（2026-09-25 实测，"没有输入入口"的根因）：
   服务端 `conversations.pending_clarify` 与前端入口是**两份独立状态**，任一侧缺失都会静默失灵：
   · 前端侧曾把自定义输入框藏在「其他…」单选之后（`display:none`）→ 用户看不到任何可输入的地方；
   · 历史渲染层（`05-markdown.js`）把澄清消息**一律**渲染成只读文字（"已作答后继续"）→
     刷新/重开会话后彻底没有作答入口，而挂起仍在 → 用户只能打新消息，**挂起永不清除、AI 反复追问**。
   **规避**：① 输入控件默认可见；② 历史渲染依据服务端状态决定"可作答卡 / 只读摘要"（本仓：
   `GET /api/conversations/{id}/messages` 回传 `pending_clarify` → 前端 `setPendingClarify()`）；
   ③ 在**主输入框上方**给出显式提示条（"AI 正在等待你的确认…直接在下方输入即可"），并让主输入框的
   提交在挂起状态下自动走澄清作答（后端 `clarify-answer` 支持 `free_text`）——把"入口"从卡片内部
   扩展到用户本来就会用的那个输入框。**只加一个入口，不作数。**
22. **同一份数据的"流式当时"与"刷新后还原"是两条渲染路径，必须成对维护**（2026-09-25 实测，
   一次"是否验证过前后端一致性"的追问当场查出的 3 个真缺口）：
   会话消息的执行过程有两条产出路径 ——
   ① 流式当时：`procRender()` 写 DOM（含 `_procDetailBtn()`、`.proc > .proc-block` 形状）；
   ② 刷新后：`05-markdown.js` 走 `process_html`（内存里那份 HTML）**或** `06-cards.js::procBlocksHtml(card_data.exec)`
   还原（**无 `.proc` 包裹**、且当时没注入详情按钮）。
   三条实测症状：**A.** 刷新后「🔍 详情」按钮消失（只有路径①注入）；**B.** 面板静默不打开
   （选择器写死 `.proc .proc-block`，路径②取不到任何块 → `total=0` 直接 return）；
   **C.** 技能/意图/模型显示为 0/空（`exec` 只含 reasoning/agent/tools，`skill_hits`/`intent`/`provider`
   **只在 `card_data` 里**，而面板当时只从 DOM 刮）。
   **规避**：① 渲染函数不要各写各的，收口条/按钮这类公共部件抽成共享函数（`_procDetailBtn()`）并在**两条路径都调用**；
   ② 数据以**落库原文**为准（`renderMessage` 里把 `card_data` 按消息 id 缓存 → 面板读缓存，不猜 DOM）；
   ③ 选择器按**语义**取（`.proc-block`）而非按某一实现的包裹层；
   ④ **验证必须用真实产出**：合成夹具只能证明"能渲染"，证明不了"与库一致"——本轮正是先用真实一轮
   （DeepSeek-V3 非 Mock，`tools/verify/verify_chat_contract_e2e.py`）才照出上述三条。
23. **往一个"在 HTML 里不存在"的 id 写文本 = 静默无效**（2026-09-25 实测，AI 状态行缺失的根因）：
   `#chat-status` / `#chat-conv-status` 这两个 id 在 `index.html` **根本不存在**，但
   `03-chat.js` / `11-pipeline.js` / `12-chatsend.js` / `13-reports.js` 共十余处都在写它们
   （"正在识别意图并调度 Agent…"、"已停止生成"、"调用失败"、"意图置信度 X%"、"已加载"、"未选择"）——
   全部是空操作，表现为"AI 在做什么"完全不可见（对标 Codex / Claude Code 的实时状态行缺失）。
   **规避**：① 新增会写入的 id 前，先 `grep` 一遍 `index.html` 确认元素真实存在（**空表推不出能力存在**，
   同理"代码里有人写"也推不出"元素存在"）；② 修复不必逐个改十几处写入点——补上真实元素 +
   用 `MutationObserver` 观察文本自动显隐，一处收口（本次做法）。
   **判据**：`document.getElementById(id)` 为 null 时写入不报错，所以**只有浏览器断言能发现**
   （本次断言 `#chat-status-bar` 存在 + 写入后 `display !== 'none'`）。
24. **泛词会劫持意图路由，且「首个命中即 return」没有翻盘机会**（2026-09-25 实测，
   用户报障「MBSE建模方法论介绍」→ 建模方案设计Agent 的根因）：
   `IntentRouter.INTENTS["design"]` 里含本体泛词 **"建模"**（MBSE 场景下几乎每句都有），而匹配是
   `any(k in t for k in keywords)` + **第一个命中的意图直接 `return 0.95`**（`agent/intent.py`）——
   没有特异性加权、没有竞争比较；而 `knowledge_qa` 当时只有（知识库/资料/文档里/查一下/检索），
   **"介绍/说明/是什么/区别/包含哪些"这类说明信号完全无覆盖**；字典顺序上 design 又远早于 knowledge_qa。
   实测 5 条说明类提问里 4 条被误判 design（conf 0.95），用户侧表现就是"匹配到了我不想要的 agent"。
   **规避（本次做法）**：加一层**说明类前置判定** = 含说明信号（介绍/说明/是什么/区别/原理/概述/包含哪些/方法论…）
   **且不含动作信号**（生成/设计/输出/创建/校验/分析一下/帮我做…）→ 判 `knowledge_qa`（route='explain', conf=0.8）。
   **两个关键位置约束**：① 判定必须放在**意图缓存 `_cache_get()` 之前** —— 该输入早已被误判写入缓存，
   放在规则层之后会被旧缓存挡住、新逻辑永不生效（本仓"缓存毒化"有前例）；
   ② 条件要**双向**（说明词 + 无动作词），只加说明词会抢走"生成建模方案"这类正例（正例误伤＝白跑一次建模）。
   **判据**：`tests/manual_verify/verify_intent_explain.py`（21 项：8 说明类正例 + 8 动作类反例 + 4 判定器边界 + 1 缓存穿透）
   + 既有 `verify_intent_enhance_d12.py`（28/28）+ 真机 done 事件 `intent/agent` 字段核对。
25. **迁移里的"裸 SQL"必须自己判存在性**（2026-09-25 实测）：`_add()` 内有判表守卫，但紧跟其后的
   `conn.execute("UPDATE project_ingest_logs …")` 没有 → 在没有该表的库（测试临时库）上 `init_db()`
   直接抛 `no such table`，**整个初始化被打断**，且报错点离真实原因很远（表现为"某个测试一跑就崩"）。
   **规避**：迁移里凡"非 _add 的裸语句"一律先 `sqlite_master` 判表/判列；**判据：新库初始化不报错**。
26. **评测/调参先"绕开缓存"，否则 A/B 是假绿**（2026-09-25 实测，差点得出错误结论）：
   `IntentRouter.detect()` 第一层就是**意图缓存**（`intent_cache`，键=文本+指纹）。我给"评测集"跑
   A/B（旧关键词层 vs 新竞争打分）时**没清缓存** → 第二轮全部 `route=cache`，两次结果**逐例完全相同**
   （accuracy 0.931 / 0.931），差点写成"改动无差异"。清缓存后才发现真正的错例在**别的层**
   （`explain` 判定过宽、P0-2 建模正则过宽）。**判据：评测报告里若大量出现 `route=cache`，结论作废。**
   配套：`tests/manual_verify/eval_intent_routing.py` 每例 detect 前 `DELETE FROM intent_cache`。
27. **泛词防御必须"降权 + 共现"两半都做**（2026-09-25 实测）：
   只做"单个泛词不构成信号"会**过度收紧** ——「请设计…架构方案」命中的 `方案/设计/架构` 恰好全是泛词
   → 判无信号 → 下沉 LLM，**结果正确但白付一次调用**（实测 `route=llm`，改造后 `route=rule_scored`）。
   正确形态：泛词各 0.25 分（降权）、**单个泛词=无信号**、**泛词共现（≥2）算信号（0.5/词）** ——
   这正是调研里"泛词应降权**或要求共现**"的完整版。**判据：正例不应因为"命中的词恰好都是泛词"而掉到 LLM 层**
   （用 `route` 字段可观测：预期 `rule_scored` 的句子不该出现 `route=llm`）。
28. **"阈值调不动"往往是索引答词表错了，不是阈值错了**（2026-09-25 实测，语义层接不住弱信号的根因）：
   症状是"弱信号句掉 LLM"；第一反应是调 `embedding.intent_threshold_dense`，但采样后发现语义层 top1
   常是**子 Agent 名**（「结构视图生成」「参数交互视图（IBD）生成」）而不是路由器要的意图名 ——
   `_semantic_index` 由 Agent 名+描述拼成，其中大半是细粒度子 Agent。**答词表不一致时，阈值怎么调都错**：
   调松就路由到子 Agent（错得更隐蔽），调紧就掉 LLM。修法：给每个**粗粒度意图**补示例 utterance
   （`IntentRouter._SEMANTIC_UTTERANCES`，**逐条**入索引，拼成一段会被平均掉）。
   **判据：先看 `top1` 是不是意图名，再谈阈值**（`tests/manual_verify/calibrate_intent_semantic.py` 会逐句打印 top1）。
29. **dense 余弦是"量纲压缩"的，比值门槛（`top1 ≥ k×top2`）不能沿用 bigram 的口径**（2026-09-25 实测）：
   bigram 下 `1.5x/1.15x` 是有效守卫；dense 下正确句的 top1/top2 常是 `0.8673/0.8029 ≈ 1.08` ——
   1.5x **几乎不可达**。叠加"低绝对门槛 0.49 + 严比值 1.5x"＝**双重否决**，弱信号句全军覆没。
   标定结论是**反过来分配**：抬绝对门槛（0.49→0.64）、放比值守卫（1.5/1.15→1.05/1.05）。
   倍率已提为配置（`embedding.intent_lead_weak/intent_lead_strong`），**必须与阈值联合标定**。
30. **语义索引改文本会造成"旧缓存挡住新逻辑"，指纹必须包含索引文本**（2026-09-25 实测）：
   路由指纹原本只放索引的 `name`；补示例 utterance 只改 `text` → 指纹不变 → 旧 `intent_cache` 继续命中，
   新索引**永不生效**。同一类坑在本仓已出现三次（explain 前置被旧缓存挡、规则表 epoch、本次）。
   **判据：凡改变"参与判定的事实"（词表、索引文本、规则），都必须让指纹跟着变。**
31. **验证前必须确认服务是「新进程」——端口占用会让新服务起不来，请求静默打到旧代码上**（2026-09-25 实测）：
   改完后端 `Stop-Process` 只杀了父 shell，uvicorn 的 python 子进程仍占着 8000；新服务 `[Errno 10048]`
   退出，而请求**照常成功**（旧代码应答）→ 得到一份"看着通过、其实没验证到新代码"的假证据
   （实测：SSE 里没有新加的 `multi_intent` 事件、弱信号句仍走旧路由）。
   **判据：① 起服务后先看日志有没有 `error while attempting to bind`；② 用"只有新代码才有"的
   特征（新事件/新 route 值）核对，而不是只看 HTTP 200；③ 用
   `Get-NetTCPConnection -LocalPort 8000 -State Listen | Select OwningProcess` 找到并杀掉残留 PID。**
32. **"识别出来了"≠"驱动了决策"——新信号必须接到消费方**（2026-09-25 实测，多意图的真实缺口）：
   多意图做成两级切分后，`detect_multi` 已能输出 3 阶段序列，但 `_needs_orchestration` 的连词规则
   只认「和/以及/与/且/并」、**不认顿号清单**，于是「提供一段需求，进行需求分析、方案设计、代码校验」
   被判"不需要编排"，识别结果**无处可用**；同理 `stage_hint` 原只传意图名，丢掉了每阶段在做什么。
   **判据：给新信号找消费方并验证"用户可见的变化"**（实测：SSE 出现 `multi_intent` + 3 个 subtask
   按 `requirement_analysis → design → review` 顺序执行，而不是靠 LLM 复杂度判定碰巧为真）。
33. **证据脚本必须"可复现"：基线要写死，不能读"当前配置"**（2026-09-25 实测）：
   `calibrate_intent_semantic.py` 首版把"当前生效值"当基线；标定落地后重跑，它把**已改好的值**
   当成待标定的坏值 → 输出"标定后 27/29"的**反向结论**。标定/对照脚本必须自带固定基线
   （本仓：`_BEFORE` 常量），并在结尾 `emb_patch(**cur)` 还原现场、不留副作用。
   **判据：同一脚本隔天重跑，结论与推荐值必须一致。**
34. **多意图子句映射也要防泛词**：L1 的 `_CLAUSE_INTENTS` 含裸泛词，用于"按阶段拆句"时还行，
   做**并列清单**切分时会把「提供一段需求」这类**背景句**判成一个阶段（实测踩到）。
   L2 用独立的严格词集（`_CLAUSE_SPECIFIC`），且"整段映射得上"也可能内含两个阶段
   （「变更影响分析以及生成报告」会整段被 impact 吞掉）→ 细切**只在能切出 ≥2 个不同意图时才采用**。
35. **新增一张表要接「三处」，少一处就起不来服务**（2026-09-26 实测，`NameError` 当场报）：
   ① `database/migrations/<域>.py` 里定义 `_migrate_xxx(conn)`；
   ② `database/migrations/__init__.py` 的 `from .<域> import (...)` 与 `__all__`；
   ③ `database/schema.py` 顶部的 `from .migrations import (...)` **显式导入**，再到 `init_db()` 里按序调用。
   只做①，启动即 `NameError: name '_migrate_xxx' is not defined` → `Application startup failed`。
   **判据：起服务后先看日志有没有 startup failed；新增 router 同理要接 `routers/__init__.py` +
   main.py 的 `from routers import (...)` 与 `for _router in (...)` 两处。**
36. **页面验证夹具的三个坑**（2026-09-26 实测，三个都真实踩到、都造成过**假结论**）：
   ① `.subpage{display:none}`（app.css）—— 夹具不显式激活（`.on` 或 `display:block`）则面板高度为 0，
      截图里只剩夹具自己的说明文本，**"布局复核"是假的**（首版截图就是这么骗过我的）；
   ② 探针把 `innerHTML` 当**文本**承载 → 标签变转义形态（`&lt;tr&gt;`）：用 `<tr` 数行会得 **0 行**，
      而页面其实渲染正常（首版把 T3 判 FAIL）。要么数 `&lt;tr`，要么断言文本而不数标签；
   ③ 夹具模板里 CSS 含 `width:100%` 时，**不能**再走一次 `%`-formatting（`unsupported format character`）
      → 用 token + `str.replace` 组装，并对"token 是否全部替换"加断言。
   **判据：夹具产出的截图必须能看见真实布局；若截图里只有夹具文字，先怀疑①。**
37. **评测集从硬编码搬进库时，"机器猜的"与"人标的"必须分列存**（2026-09-26）：
   `intent_samples.intent` = 人工标签、`hit_intent/hit_route/hit_conf` = 采集当时的系统判定，
   且**评测只消费 `status='confirmed'`** —— 拿 `suggested`（系统自己的判定）当标签去评测系统，
   指标永远 100%，是纯自我循环（`tools/eval/build_evalset.py` 早已写明"弱标注须人工复核"）。
   同批查出：`intent` 配置组**此前在 DEFAULT_CONFIG 里根本不存在**，于是
   `eval_intent_routing.py --scored 0 / --generic 0` 两个 A/B 开关读到的永远是 default → **两次运行必然同结论**
   （与"没清缓存"同类的假绿）。**判据：凡是做成开关的东西，都要先证明"改它真的会改行为"。**
38. **"低置信"必须一路贯穿到"不得硬选具体意图"**（2026-09-26，扩集后评测抓出并修）：
   LLM 兜底原逻辑是：`conf < 0.85` 且**有**上一轮意图 → 继承；**没有**上一轮意图 → 就用这个低置信猜测去路由。
   实测两条生产真实说法「帮我看看这个项目的预算」「帮我测算一下这个项目的成本」被 LLM 以 **0.60**
   猜成 `report_generation`（"预算/成本"联想到"报告"）→ 用户只是问预算，却收到一张报告澄清卡并真去跑报告 Agent。
   修法：`conf < 0.85` 一律**不硬选** —— 有上下文继承，否则回落 `chat`（`route='llm_weak'`，`conf` 取 <0.7
   从而不入意图缓存）。这与既有两条防线同源（`fused_conflict` 走澄清、requirement_quality 的 LLM 猜测直接降级）。
   **判据：扩集评测抓到的错例，先看 `route`/`confidence` —— `route=llm` 且 `conf<0.85` 的错例基本是同一类，
   别去调阈值（阈值管不到 LLM 层）。**
   副作用（明说）：确实属于某意图、但 LLM 只给 0.6~0.8 的句子会落到 chat；若出现真实损失，
   就把 `llm_weak` 接进澄清条（当前只管"不硬选"）。
39. **意图"确定不了"要停下来问，而不是自己挑一个**（2026-09-26，用户明确要求；配套 `intent.confirm_when_unsure`）：
   触发面（`_should_confirm_intent`）：`llm_weak` / `fused_conflict` / `semantic_weak` / `llm` 且 conf<0.85 /
   完全无信号但**像在求助**（含"帮我/请/怎么/有没有"等）。
   **不问**：`inherit`（追问续写，问了反而打扰）/ 规则命中 / `fused`（两路互证）/ 高置信 / 纯寒暄 /
   **含 `CLARIFY_RESUME_MARK` 的续答消息**（不加这条会"问→答→又问"死循环）。
   实现上**复用内容级澄清那张卡**（`clarifyCardInnerHtml`）与它的挂起/续跑机制
   （`_persist_clarify` → `/clarify-answer` → 带【澄清补充】的 `resume_text` → 前端 `sendResume`），
   不要另造一套状态；要新增的是"第 1.4 阶段"：命中即 `clarify_ask` + `done` 并 **return（不执行）**。
   两个易错点：① 卡的标题原为硬编码"确认建模信息"，复用后必须允许事件带 `title`；
   ② **选项文本要带该意图的特异词**（如"方案设计/建模"）——选项会被原样拼进续答文本再识别一次，
   只写"设计"这种泛词会因"泛词不单独构成信号"而再次落空。
   判据：真机跑一条 `llm_weak` 句 → SSE 只应有 `stage/clarify_ask/done`（**无 agent/subtask** = 没硬选执行），
   作答后 `resume_text` 含【澄清补充】且续跑意图 == 用户所选，且**不再重复弹卡**。
40. **展示层不要直接复用"协议字段"**（2026-09-26，用户报障"AI 每次输出结尾总有这段"）：
   子任务卡下那行摘要原先直接显示 `summary.summary`，而它是 `normalize_text` 取的**子 Agent 输出原文前 N 字**，
   开头正是"一、对上游意图识别结果的承接""本任务承接 t2 的执行计划，职责是…"——**写给上游看的内部交接语**，
   前端再 `slice(0,60)` 硬切，断在词中间（实测"职责是**产"）。用户问"这是必须的嘛、价值是什么"。
   正确做法：**"有一行交付摘要"保留**（编排下过程可审计），但**新增一份面向用户的副本** `ui_summary`
   （`services/subtask_protocol.ui_summary()`：跳 `#` 标题行、剔内部协作词、去 markdown、按句收口 + `…`；
   全被剔干净就**返回空**，宁可没有这行），而 `normalize_text` 的 `summary` **原样保留** ——
   它要作为"上游快照"喂下游子任务（Task 9/10），清洗它会破坏子 Agent 的承接能力。
   两个实现细节：① **标题行必须在"去 markdown 标记之前"判掉**，否则 `## 交付说明` 会被当成正文返回（实测踩过）；
   ② 截断要按标点收口并补 `…`（只 slice + 去尾标点会得到一句"没有省略号的残句"）；
   ③ **同名字段可能在多处字面构造**：`subtask` 的"完成"事件是 `yield {"type": "subtask", ...}` 的独立字面量，
   与 `orch_tasks.append({...})` **不是同一份** —— 只改后者，真机上事件里根本没这个键（实测踩到）。
   判据：加字段后**读真机事件**验证，不要只 grep 代码；
   ④ 内联生产函数做夹具时要连带它引用的**模块级变量**（`subtaskCardHtml` → `capTagsFor` → `_agentsCache`），
   否则 RENDER_ERROR 指向的是"夹具缺件"而不是产品缺陷。
   判据：真机跑一次编排 → `subtask` 事件带 `ui_summary` 且**不含** 承接/上游/职责是 与裸 `#`/`**`。
41. **编排子任务的内容要"边产边吐"，不能等 future 完成再统一吐**（2026-09-26，用户要求"不要放在最后统一输出"）：
   旧实现：worker 把 token/reasoning/tool 攒进 `evs`，主循环在 `_ac(_futs)` 里**完成后**才
   `for ev in evs: yield ev` → 子任务执行期间界面一片空白，完成瞬间一次性涌出。
   现改为 `queue.Queue` 通道：worker 边产边 `_evq.put()`，主循环 `while _pending` 里**先 drain 队列再收 future**
   （本轮无完成项就 `sleep(0.05)` 让出 CPU）。四个必须做对的点：
   ① **删掉**原来的 `for ev in evs: yield ev` —— 否则每个事件吐两遍（重复渲染）；
   ② 循环结束后**再 drain 一次**（最后一个子任务可能在"检测到完成"之后仍有事件入队）；
   ③ 前端**无需改**：它已按 `key` 归位（`procAddThinking(delta, round, key)` 与 `_subChildrenHtml(key)`），
      只要事件及时到达就会落在对应子任务卡下；
   ④ 改这段后务必 `py_compile`（大块缩进重排极易错位），并真机看**事件顺序**。
   判据：SSE 序列里**首个子任务的 `done` 之前**必须已出现它的 token/reasoning/tool 事件（= 就地实时）。
42. **"默认折叠"只能在首次创建时置位**（2026-09-26）：子任务卡默认折叠（用户反馈"占用太多区域"）时，
   `collapsed:true` 若跟着每次 run/done 事件一起下发，会把用户**手动展开**的卡反复折回去。
   做法：`const _isNew = !_procS.timeline.some(x=>x.id===id); if(_isNew) patch.collapsed = true;`
   配套：折叠态仍要能看到关键信息 —— 把"面向用户的一句话摘要"上提到 head 的 `sub` 位
   （`.proc-title` 已有单行省略），否则一折叠就把内容全藏了。
43. **采样钩子要排除"系统自己造出来的输入"**（2026-09-26，查样本池时发现 15 条候选里 9 条是垃圾）：
   采集钩子放在**通用流式入口**（`execute_stream` 的意图识别之后），而**编排子任务也走这个入口**
   （`_worker` 用 `sub.execute_stream("[任务上下文快照]…")` 跑子任务）→ 内部上下文被当成"用户原话"采进池。
   实测污染：`[任务上下文快照] - 任务定义：key=t1 …` ×8 条（另 1 条是测试残留 `(续跑)`）。
   修法**双保险**：① 入口判 `getattr(self, "_orch_subtask", False)`（worker 打的子任务标记）→ 不采集；
   ② `intent_sample_repo._is_system_wrapper()` 把 `【澄清补充】/【系统】/[任务上下文快照]/[任务]` 前缀全部拦掉
   （采集与"从历史回填"两条路都过它）。判据：`collect("[任务上下文快照]…")` 必须返回 False 且库里查不到该行。
   **通用教训：任何"记录用户输入"的钩子，都要先问"这条输入是用户打的，还是系统拼的"。**
44. **两级切分必须共用"收口"与"映射表"**（2026-09-26，多意图评测集驱动，一次修掉 5 个错例）：
   多意图的 L1（阶段连词）与 L2（并列清单）此前是**两套语义**：L2 有收口（过滤未映射片段、
   同一意图只留首个、不足 2 个不同意图不算多意图）+ 严格映射；**L1 什么都没有**，直接用宽松映射
   且不去重、不判数量。实测三类错：
   ① 「先生成结构视图，再生成参数视图」→ `['design','design']`（同一意图被当两个阶段）；
   ② 「先看看再想想」→ `['看看','想想']`（**映射失败时原样返回文本**，片段被当成意图名）；
   ③ 「做需求质量评审」→ requirement_analysis（`_CLAUSE_SPECIFIC` **根本没有 requirement_quality 条目**，
      被"评审"抢走；另「再出一份评审报告」被判 review —— 与主判定的"报告优先"冲突）。
   修法：抽出 `_finalize_tasks()` 由 L1/L2 **共用**，L1 改用 `_clause_to_intent_strict`，
   并补 `_CLAUSE_SPECIFIC` 的 requirement_quality 条目 + 把 report_generation 提到 review 之前。
   判据（`eval_multi_intent.py`，18 例含正/负/边界）：**accuracy / P / R / F1 与阶段序列完全匹配率**。
   修前 0.889/0.800/-/0.889、序列 0.625 → 修后全 1.000（既有 21 项行为断言与 44 条意图集无回归）。
   **通用教训：同一功能有两条并列实现路径时，先问"它们的语义一致吗"——不一致时同一句话会因走哪条路径而结论不同，
   这是最难排查的一类 bug；"收口/映射/词表"这类规则必须收敛到一处。**
45. **惰性初始化的开销要挪到"用户开口之前"（后台预热）**（2026-09-26）：
   语义层的候选集向量是**惰性**算的，这笔开销此前全落在第一个真实请求上。进程内隔离测量（决定性）：
   `_ensure_embedder` **0.02s**（不是瓶颈）/ 首次 `rank`（52 条候选向量化）**4.48s** / 第二次 **0.00s**。
   预热的三个必须做对的点：
   ① **后台守护线程**（`semantic.prewarm()`）：失败只退化成原冷启动，绝不影响启动与请求；
   ② **按文本指纹去重**：`set_semantic_index` 每个请求都会走到，无守卫会反复起线程
      （cache key 必须与 `_embed_candidates` 完全一致 = `md5("\\n".join(同序文本))`）；
   ③ **必须可观测**：首版 `if not ed: return` 是静默空转，服务端日志里什么都看不到，
      只能靠人肉推断"预热到底有没有生效"（实测踩到）→ 改成成功/跳过/未生效三种都打日志。
   判据：启动日志出现 `[semantic-prewarm] 完成：N 条候选已缓存（backend=…）`，且真实弱信号句的
   **意图阶段耗时 ~1s**（预热前要等首次向量化）。
   ⚠️ 同类"首次请求慢"要**分路径量**：服务端首次 `run-eval` 比其他调用多约 8s，预热生效后**依然存在**
   → 说明它不是候选集那笔（很可能与评测集里那几条走 LLM 的用例的**首次 provider 握手**有关，未验证）。
   教训：**归因要靠隔离测量**，不要看到"首次慢"就默认是自己刚改的那块。
   **追查结论（同日晚些）**：那笔 8s 定位到 `detect` 层（首遍 8.94s / 次遍 1.86s），**LLM 已证伪**（冷 0.70s vs 热 0.83s）。
   真根因是**预热命中不上**：两侧索引文本不一致（尾随换行 `'账号角色管理\n'` vs `'账号角色管理'`）
   → `md5(join(texts))` 这个 cache key 不同 → 预热填的 key 请求侧取不到（实测预热 fp=91242a36 / 请求 fp=cd37d412，
   **条数都是 52**）。两处修：① `_embed_candidates` **先 strip 再算 key、并用同一份规范文本向量化**
   （只去空白、不排序 —— 顺序仍重要，向量与 items 一一对应）；② `agents` 查询补 `ORDER BY name`
   （无 ORDER BY 时行序不保证稳定，索引/指纹都会抖）。效果：首次 `run-eval` 13.20s → **8.99s**，
   且请求侧日志**不再出现"候选缓存未命中"**。
   **通用教训：凡是用"内容指纹"做缓存键的，先问"这份内容会不会因无关因素（空白/行序/大小写）而漂移"——
   漂移一次，缓存就永久失效，且表现为"预热/缓存明明在跑却毫无效果"。**
   ⚠️ 剩余未归因：首次 8.99s vs 热 3.48s 仍差约 5.5s（下一轮继续插桩）。

46. **页面级验证的夹具必须在每次跑前重置**（2026-09-27 实测，一次"看起来产品坏了"的假失败）：
    `tmp/verify_docfolders_ui.js` 的 payload 会**故意**移动/改名（t9 改名、t11 排序、t12 拖拽、t13 面板移动），
    中途任一步失败就把漂移留给下一轮：实测上一轮 t12 被拒后留下「FE-动力系统 挂在 FE-热管理 下」，
    于是下一轮 t1 的「层级缩进 / 双计数 / 可拖拽」断言**全部假失败**（现象是 `rootPad=null`、`leafRow=null`，
    看起来像产品坏了，其实是夹具脏了）。
    **规避**：runner 在 `open` 之前先跑一遍 `tmp/e2e/prepare_frontend_data.py`（自带 clean + 重建，幂等），
    并把"重置成功"本身作为**第一条断言**（U-1）—— 前置失败时后续断言一律不可信。
    **通用教训**：凡"会改数据"的验证，要么自带重置，要么断言**相对量**（集合差），不要断言绝对值。
47. **断言"面板已关闭"要看 class，不能看里面的元素还在不在**（2026-09-27 实测）：
    `openPanel` / `closePanel`（`02-shell.js`）只切 `#panel-detail` 的 `.open` 类与 `#overlay`，
    **不清 `#panel-body` 的 innerHTML** → 关闭后里面的 `#dft-move-sel` 仍能被 `getElementById` 找到，
    于是 `!document.getElementById('dft-move-sel')` 恒为 false（"点了取消面板没关"的假失败）。
    同类：滑窗里的按钮要查 `#panel-body button`，**根本不存在 `#panel` 这个元素**。
    **判据**：`document.getElementById('panel-detail').classList.contains('open') === false`。
48. **"拖到行的上/下 1/3"要允许跨层级**（2026-09-27 实测，本轮自己的设计缺陷）：
    第一版把上/下 1/3 限定为"必须与目标同级"，结果**嵌套目录拖不回根级**（根级没有可落的行 →
    用户只能去右键菜单绕一圈）。改为"插到**目标所在层级**的那一行"（`parent_id = 目标.parent_id`，
    见 `40-docfolders.js::docFolderDropAllowed`），只需保证目标层级的父不是自己、也不在自己的子树里
    （物化路径前缀判定）。
    **通用教训**：树形拖拽的"插入到行间"语义天然是"进入该行所在层级"；把它限制成"同级重排"，
    会凭空造出一个**做不到的操作**（而 UI 上完全看不出来）。

49. **拖拽态必须"无论如何都能复位"，否则整片区域点击被静默吞掉**（2026-09-28 实测）：
    `40-docfolders.js` 的 `_dftDragging` 原来只在**本容器**的 `dragend` 里复位。若拖拽在容器之外结束
    （Escape 取消 / 落到窗口外 / dragstart 之后没有配套 dragend），它**卡在 true** → `_dftSuppressing()`
    恒真 → 目录树后续**所有点击被吞**（现象：点行内 ✕ 毫无反应、确认框都不弹，"树突然坏了"）。
    **规避**：抽出 `docDftEndDrag()`，三处都要调 —— 容器级 `dragend`、每次 `drop` 完成后、
    以及 **document 级 `dragend`（capture）兜底**（覆盖"在本容器之外结束"）。
    **判据**：只发 `dragstart` 不发 `dragend`，随后点击必须仍然生效（本轮 E2g 就是这么测的）。
50. **"先清高亮、再读落点区" = 落点判断静默失效**（2026-09-28 实测）：
    `drop` 处理器原先是"开头 `docDftClearZones()` → 再读 `row.dataset.dftZone`"，而 clear 会
    `delete row.dataset.dftZone` → 读到的恒为 `undefined`，只能退回按 `ev.clientY` 重算，
    而 **drop 事件的 clientY 在部分浏览器会被改写为 0**（"dragover 记录落点区"这套机制等于白做）。
    **规避**：先读 `zone` 与 `dataTransfer` 载荷，**再**清高亮，最后复位拖拽态。
    **通用教训**：任何"边读边清"的处理器，先问一句"我清的东西，后面还要不要读"。
51. **断言里不要持有"点击前抓到的 DOM 引用"**（2026-09-28 实测，一次假失败）：
    该前端是**全量 innerHTML 重建**（`renderDocFolders()`），点击 ＋ 就会重建整棵树 ——
    点击前 `querySelector` 拿到的 `prow` 已**脱离文档**，`row.previousElementSibling === prow`
    恒为 false（看起来像"输入行没插在父节点后面"）。
    **规避**：跨一次重渲染的比对一律用**稳定标识**（`data-fid` / `data-dftkey`）现查现比，
    不要缓存元素引用。同族坑见本文件第 22 条（两条渲染路径必须成对维护）。

## 4. 起服务 / 验证

```powershell
# 起服务（后台）
Set-Location "C:\Users\gefei\WorkBuddy\2026-08-04-19-05-52\mbse_system"
$env:PYTHONIOENCODING="utf-8"; & ".\.venv\Scripts\python.exe" -X utf8 main.py
# 健康检查
Invoke-WebRequest http://127.0.0.1:8000/api/dashboard -UseBasicParsing
```

- **前端改动必须浏览器验证**（不要只看代码）：`tmp/verify_*.js` 里有现成模板，用 agent-browser 在**一个 node 进程内**串行跑 open→eval→screenshot→close（跨 PowerShell 调用会丢会话）。
- 关键断言：`#mainnav` 导航项与高亮、`.page.on` 页面类名、`#br-cur` 面包屑、`ab("errors")` 为空。
- ⚠️ **验证 payload 的选择器必须限定作用域**：同一类容器可能**同时存在多份**（例：左侧「项目」组里
  每个展开的项目都有自己的 `.gp-tasks`，首屏还会自动展开一个）。`container.querySelector('.gp-tasks')`
  会命中**第一个**（可能是别人的空任务区）→ 断言假失败、甚至 `getComputedStyle(null)` 直接抛错
  （2026-09-24 实测）。正确做法：先定位目标行，再 `rowEl().parentElement.querySelector(...)` 取它自己的块。
- 后端改动：`python -m py_compile <files>` + `tools/verify/` 下的脚本；pytest 用 `pytest.ini`（**指向独立测试库，不要连生产 `mbse.db`**）。
- **结构治理（拆分/搬运）必须自证"纯搬运"**：改动前用 `ast.unparse` 把每个顶层函数的源码序列化成指纹，改动后重新生成并**逐字符比对**；`database/migrations.py` → `database/migrations/`（S7-3）即用此法证明 49 个函数零差异。
- ⚠️ **若干验证脚本按"源码文本"断言**，它们对 `agent/pipeline_parts/*.py` 断言**精确子串**（含局部变量名与切片写法）。**拆这些文件时被断言的方法必须留在原文件且文本逐字不变**（禁止改名、禁止重排、禁止重命名局部变量）。
  - 在 `tools/verify/`：`verify_token_budget.py`、`verify_s4_prompt.py`、`verify_s4_toolname.py`、`verify_s4_calls.py`。
  - 在 `tests/manual_verify/`（**注意不是 `tools/verify/`，此前本文件写错过**）：`verify_p1_blocking_smoke.py`、`verify_p1_perf.py`。后者会**以子进程**跑前者并校验其退出码，故前者的失败会连带后者报 `REG` 失败。
  - ✅ 实测（2026-09-23）：**只改模块级 docstring / 顶部注释**（不动任何函数体文本）**不会破坏这些断言**——改完 `*_parts/` 全部头注后，上列 4 个 `tools/verify` 脚本仍全绿（31/33/17/45 项）。风险只在改**函数体**。
  - 两处**存量失败（与本批次无关，2026-09-23 复核于 HEAD）**：`verify_s4_calls.py` 有 3 项关于 `agent/rag.py` top_k 的断言失败（45/48）；`verify_p1_blocking_smoke.py` 的 `C2/C3 e2/e9 已被合并为 deprecated` 失败（24/26，该脚本用**临时库** `%TEMP%/mbse_p1_blocking_smoke.db`，不碰生产库；`C1 auto_merged≥2` 通过 → 是"合并后未置 deprecated"的行为差异，非数据缺失）。**未修**，留待对应线处理。

## 4.1 版本控制（2026-09-18 起必须遵守）

- 仓库已有 git 基线：`master`，标签 **`baseline-20260918`**（= S0–S5 优化完成后的快照）与 **`nav-consolidate-20260918`**；工作区干净。
- ⚠️ **跟踪文件数一律现算，不写死**：本条原写「当前 476 个跟踪文件」，因 `e744982` 拆 `plugin_system/store.py` 为包后未同步而失真（476 → 487）。请用 `git ls-files | wc -l` **现算**（2026-09-18 为 **492**）。同类风险同本文件开头对行号引用的告诫。
- **动高风险代码前先提交**，再开分支：`git switch -c refactor/xxx`；每完成一小步就 commit（提交信息写清"改了什么 + 验证了什么"）。
- **一次提交只装一件事**：改用 `git add <具体路径>`，**不要用 `git add -A`**——多人在同一仓库并行改动时，`-A` 会把别人的改动卷进你的提交。
- 回滚单文件：`git checkout -- <file>`；看基线差异：`git diff baseline-20260918 --stat`。
- **不要把运行期产物交给 git**：`java-runtime/`、`fuseki/`、`data/`、`tmp/`、`outputs/`、`screenshots/`、`static/uploads/`、`static/skill_packages/`、`tools/legacy/`、`*.db`、`*.log`、`*.bak-*`、`*.pres5` 均已在 `.gitignore`（新增此类目录前先补忽略规则，并**记得锚定根目录**，见坑 14）。
- **`__init__.py` 必须入库**：包初始化文件承载着 re-export 与 router 装配，缺了它克隆即崩。反查：`git ls-files '*__init__.py'` 的数量应与磁盘一致。
- **文档/注释引用代码时优先写 `模块.符号`，不要只写行号**：行号随每次结构变更失真（本文件此前就因 `esc/escA` 迁移而留过一条错事实）。必须给行号时，请同时给出符号名。
- **换行符口径（2026-09-24 更正；此前本文件写「`core.autocrlf=false`」是错的，且这条更正**仓库文档一直没跟上**）**：实测生效值是 **`true`**，且**本仓与用户全局都没有设**——来源是**系统级** gitconfig（`git config --show-origin --get core.autocrlf` 可验）。**路径取决于用哪个 git**：本工具链 bash 里的 `git` 报 `…/PortableGit/versions/1.2.0/etc/gitconfig`，另一处实测报 `C:/Program Files/Git/etc/gitconfig` —— **两个都是系统级、值都是 `true`**，别以为其中一个是错的。
  - 机制：`git add` 做 CRLF→LF 归一（**入库 blob 全是 LF**；`git ls-files --eol` 实测 `i/crlf = 0`），`git checkout` 按 CRLF 写回工作区（工作区 114 个 CRLF + 17 个 mixed）。跨 Git 配置的协作方一对比就是**全量 diff**（即 `docs/代码优化遗留事项核查-20260918.md` D-2 那条「混合换行」，此处给出机制解释）。
  - ✅ **在本环境 `git add` 本身是安全的**（归一后入库），本批 41 个文件实测仅 **75 增 46 删**、单文件 1~3 行，**内容未被换行污染**。
  - ⚠️ **不要"照旧文档改回 false"**：那会让所有 CRLF 工作区文件**立刻显示为已修改**（blob 是 LF、工作区是 CRLF，取消归一后逐字节不等）。根治只能加 `.gitattributes`（`* text=auto eol=lf`；因 blob 已全 LF，**不动任何 blob**）；`git add --renormalize .` 属破坏性操作，**必须先问用户**。以上均**未擅自改**，已在 D-2 标为**待拍板**。
  - 判断"某文件 git 认为有没有变"，别信编辑器显示的换行符，用 `git diff --stat` 看规模。
- 完整约定与回滚演练见 `docs/版本控制使用约定-20260918.md`。

## 5. 文档索引（哪些是权威）

| 想看 | 读这个 |
|---|---|
| 代码优化计划（token 成本 / 结构治理 / 移除清单 / 执行记录） | `docs/代码优化方案-20260917.md` |
| 导航结构现状与残留审计 | `docs/导航结构与整合残留审计-20260917.md` |
| 资料库按文件结构管理设计方案 | `docs/资料库-文件结构管理设计方案-20260917.md` |
| 项目（工程）入口调研与设计方案 | `docs/项目工程入口-调研与设计方案-20260917.md` |
| 议题 3/4 实施记录 | `docs/议题3-4实施记录与交付索引-20260917.md` |
| 已过期/被取代的方案 | `docs/_archive/`（**不要**把归档文档当现状依据） |

## 6. 已废弃能力（不要再实现一遍）

- **独立导航项「术语词典」「本体模型」「AI 建模」已移除**（2026-09-18）：知识域收敛为单一导航项「知识中心」，
  内容改为页内顶层 Tab（`#kbhub-tabs` 共 **5** 个：数据看板 / 资料库 / 图谱工作区 / 本体模型 / 术语词典，默认落点 kb-a）；
  建模入口统一由「＋ 新建任务」承担。
  术语词典是 `kb-c` 的**显式子态**：进入前须置 `window._kbCtxTerms = true`，`15-kb.js` 据此分流；
  其余入口（深链 / 侧栏 / 角色快捷）一律落「本体模型」。详见 `docs/导航收敛-术语词典与本体模型并入知识中心-20260918.md`。
- 消息级「⚠ 需要确认 · SysML 产物入库」审批卡 —— 2026-09-17 移除，入库真实入口是 **图谱工作区 · 版本历史 →「📦 工程入库」**。
- 单版本「入库」入口（`14-sysml.js` 顶部注释）—— 归档动作已收敛为工程级一次性操作。
- `register_zhiyuan_agent.py` —— 2026-09-17 删除（一次性脚本，效果已落库 agents id=275；权威定义见 `docs/_archive/zhiyuan_mgmt-agent-snapshot-20260917.json`）。
- 「合并队列」页与「模型配置」独立页 —— 已并入 `kb-d` / `studio/st-model`。
- `database/migrations.py` 单文件 —— 2026-09-18 S7-3 拆为 `database/migrations/` 包（按域 8 文件 + `__init__.py` 显式 re-export 全部 49 个符号）；**不要再往单文件里加迁移**。
