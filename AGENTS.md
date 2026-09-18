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
| 知识域**五个**顶层 Tab（知识浏览 / 文档库 / 知识图谱 / 本体模型 / 术语词典） | `index.html`（`#kbhub-tabs` → chip `kbhub-tab-a/e/d/c/t`）、`js/mods/15-kb.js`（`loadKBTab`） | `routers/knowledge_parts/*` |
| 会话列表 / 新建任务 / 角色快捷 | `js/mods/03-chat.js`、`12-chatsend.js` | `routers/conversations.py` |
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

## 3. 必知的 15 个坑（都是踩过的）

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
- 后端改动：`python -m py_compile <files>` + `tools/verify/` 下的脚本；pytest 用 `pytest.ini`（**指向独立测试库，不要连生产 `mbse.db`**）。
- **结构治理（拆分/搬运）必须自证"纯搬运"**：改动前用 `ast.unparse` 把每个顶层函数的源码序列化成指纹，改动后重新生成并**逐字符比对**；`database/migrations.py` → `database/migrations/`（S7-3）即用此法证明 49 个函数零差异。
- ⚠️ **`tools/verify/` 下有若干脚本按"源码文本"断言**（`verify_token_budget.py` / `verify_s4_prompt.py` / `verify_s4_toolname.py` / `verify_s4_calls.py` / `verify_p1_blocking_smoke.py` / `verify_p1_perf.py`）。它们对 `agent/pipeline_parts/*.py` 断言**精确子串**（含局部变量名与切片写法）。**拆这些文件时被断言的方法必须留在原文件且文本逐字不变**（禁止改名、禁止重排、禁止重命名局部变量）。

## 4.1 版本控制（2026-09-18 起必须遵守）

- 仓库已有 git 基线：`master`，标签 **`baseline-20260918`**（= S0–S5 优化完成后的快照）与 **`nav-consolidate-20260918`**；工作区干净。
- ⚠️ **跟踪文件数一律现算，不写死**：本条原写「当前 476 个跟踪文件」，因 `e744982` 拆 `plugin_system/store.py` 为包后未同步而失真（476 → 487）。请用 `git ls-files | wc -l` **现算**（2026-09-18 为 **492**）。同类风险同本文件开头对行号引用的告诫。
- **动高风险代码前先提交**，再开分支：`git switch -c refactor/xxx`；每完成一小步就 commit（提交信息写清"改了什么 + 验证了什么"）。
- **一次提交只装一件事**：改用 `git add <具体路径>`，**不要用 `git add -A`**——多人在同一仓库并行改动时，`-A` 会把别人的改动卷进你的提交。
- 回滚单文件：`git checkout -- <file>`；看基线差异：`git diff baseline-20260918 --stat`。
- **不要把运行期产物交给 git**：`java-runtime/`、`fuseki/`、`data/`、`tmp/`、`outputs/`、`screenshots/`、`static/uploads/`、`static/skill_packages/`、`tools/legacy/`、`*.db`、`*.log`、`*.bak-*`、`*.pres5` 均已在 `.gitignore`（新增此类目录前先补忽略规则，并**记得锚定根目录**，见坑 14）。
- **`__init__.py` 必须入库**：包初始化文件承载着 re-export 与 router 装配，缺了它克隆即崩。反查：`git ls-files '*__init__.py'` 的数量应与磁盘一致。
- **文档/注释引用代码时优先写 `模块.符号`，不要只写行号**：行号随每次结构变更失真（本文件此前就因 `esc/escA` 迁移而留过一条错事实）。必须给行号时，请同时给出符号名。
- 本仓库 `core.autocrlf=false`（源码是混合换行，开启自动转换会造成全量 diff）。
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
  内容改为页内顶层 Tab（`#kbhub-tabs` 共 **5** 个：知识浏览 / 文档库 / 知识图谱 / 本体模型 / 术语词典）；
  建模入口统一由「＋ 新建任务」承担。
  术语词典是 `kb-c` 的**显式子态**：进入前须置 `window._kbCtxTerms = true`，`15-kb.js` 据此分流；
  其余入口（深链 / 侧栏 / 角色快捷）一律落「本体模型」。详见 `docs/导航收敛-术语词典与本体模型并入知识中心-20260918.md`。
- 消息级「⚠ 需要确认 · SysML 产物入库」审批卡 —— 2026-09-17 移除，入库真实入口是 **图谱工作区 · 版本历史 →「📦 工程入库」**。
- 单版本「入库」入口（`14-sysml.js` 顶部注释）—— 归档动作已收敛为工程级一次性操作。
- `register_zhiyuan_agent.py` —— 2026-09-17 删除（一次性脚本，效果已落库 agents id=275；权威定义见 `docs/_archive/zhiyuan_mgmt-agent-snapshot-20260917.json`）。
- 「合并队列」页与「模型配置」独立页 —— 已并入 `kb-d` / `studio/st-model`。
- `database/migrations.py` 单文件 —— 2026-09-18 S7-3 拆为 `database/migrations/` 包（按域 8 文件 + `__init__.py` 显式 re-export 全部 49 个符号）；**不要再往单文件里加迁移**。
