# AI4MBSE — mbse_system

面向 MBSE（基于模型的系统工程）的**私有化离线部署**平台：知识库摄取 → 本体 / 知识图谱 → SysML v2 模型导入 → 需求追溯 → 影响分析 → Agent 流水线。

> **给 AI 编码助手：请先读 [`AGENTS.md`](AGENTS.md)** —— 它是「改哪个功能只需看哪几个文件」的唯一入口（含功能→文件映射表与 15 个必踩坑）。本 README 只做仓库级导航，不重复其内容。

---

## 技术栈

| 层 | 选型 | 落点 |
|---|---|---|
| 后端 | FastAPI + SQLite | `main.py` 只做装配；业务在 `routers/` / `services/` / `repositories/` |
| 前端 | **原生 JS：无框架、无构建、无打包** | `static/index.html` + `static/js/mods/01..37-*.js` |
| 知识图谱 | RDF / OWL / SHACL + SPARQL | `graph_db.py` `triple_store.py` `ontology_*.py` `sparql/`；可选 Apache Jena Fuseki（`docker-compose.fuseki.yml`） |
| 模型导入 | SysML v2 | `sysml_importer.py` `sysml_profile.py` `sysml_models/` |
| LLM | 多 provider，失败可降级 Mock（留 WARNING 痕迹） | `llm/` |
| 文档摄取 | 分片 + 向量索引 | `knowledge_pipeline/` `vector_index.py` |

## 规模

代码侧数字取自 `master`（`01d7d2d`）实测，非文档转述；**会随提交变化的量一律附口径**（本仓约定：数字必须连口径一起引用）：

- 跟踪文件 **492** 个 —— 口径 `git ls-files` 计数（含本 README 与 `requirements.txt` 入库后）
- Python **336** 个文件 / **75,214** 行 —— 口径 `.py` 文件数 / 逐文件 `read_bytes().decode('utf-8').splitlines()` 累计
- `static/index.html` = **200,435 B / 1,541 行**，含 **42** 个 `<script src>`、**0** 个内联 `<script>` / `<style>`
- 前端模块 `static/js/mods/` = **37** 个 `.js`，合计 **1,752,219 B**
- 目录规模：`tests/` 71 · `docs/` 69 · `static/` 57 · `tools/` 51 · `routers/` 46 · `agent/` 20 · `services/` 19 · `repositories/` 16 · `plugin_system/` 15 · `database/` 13 · `models/` 12 · `core/` 9 · `workflows/` 7 · `knowledge_pipeline/` 6 · `llm/` 6 · `sysml_models/` 6

## 快速开始

```powershell
# 0) 建虚拟环境（仓库不含 .venv）
python -m venv .venv

# 1) 依赖
.venv\Scripts\python.exe -m pip install -r requirements.txt

# 2) 配置（可选）：复制模板后按需填写
Copy-Item mbse_config.example.json "$env:USERPROFILE\.workbuddy\mbse_config.json"
#   优先级：环境变量 > 配置文件 > 内置默认值
#   API key / token 建议用环境变量注入，避免明文落盘（根目录 .env 已在 .gitignore 中）

# 3) 起服务
$env:PYTHONIOENCODING = "utf-8"
.venv\Scripts\python.exe -X utf8 main.py            # 默认 127.0.0.1:8000

# 健康检查
Invoke-WebRequest http://127.0.0.1:8000/api/dashboard -UseBasicParsing
```

- 前端是**静态资源且带 no-cache** → 改前端**不用重启服务**，刷新即生效；改后端需重启。
- 数据库只有一个 `mbse.db`，服务运行时被占用 → 只读查询请用 `file:...?mode=ro`。

## 测试

```powershell
.venv\Scripts\python.exe -m pytest      # 配置见 pytest.ini：testpaths=tests
```

- `tests/manual_verify/` 下的脚本**需真实服务 / 浏览器**，不参与自动收集（`pytest.ini` 的 `norecursedirs` 已排除）。
- `tools/verify/` 是各阶段的**门禁与不变量校验**脚本；其中若干**按源码文本断言精确子串**，改动被断言的文件时保持文本逐字不变。

## 工程铁律（全文见 `AGENTS.md` §1 / §3）

1. **前端模块在全局作用域**，页面用内联 `onclick="fn()"` 调用 → **改函数名 / 挪文件会静默失效**，改完必须浏览器验证。
2. `index.html` 已无内联 `<script>` / `<style>` → 新增逻辑一律新建 `static/js/mods/NN-xxx.js` 并加 `<script src>`（注意加载位置：依赖页面 DOM 的模块须放在该 DOM 之后）。
3. 新增 / 修改后端功能**必须走 `services/` + `repositories/`**，不在 router 里直接写 SQL（存量历史债只做新老划断，规模与口径见 `AGENTS.md` 铁律 3）。
4. **私有化离线部署**：不引入外部 CDN / 构建链 / 新依赖。

## 文档

| 想看 | 读这个 |
|---|---|
| AI 助手唯一入口（功能→文件映射 + 15 个坑 + 起服务验证） | [`AGENTS.md`](AGENTS.md) |
| **文档索引**（全部 `docs/*.md`，按主题机械分组） | [`docs/README.md`](docs/README.md) |
| 代码优化计划与执行记录（**活文档**，代码体积 / 结构治理 / 移除清单） | [`docs/代码优化方案-20260917.md`](docs/代码优化方案-20260917.md) |
| 遗留事项核查（**时点审计**，对某个 HEAD 做「文档声明 vs 实测」对拍，事后不回改） | [`docs/代码优化遗留事项核查-20260918.md`](docs/代码优化遗留事项核查-20260918.md) |
| 版本控制与提交约定 | [`docs/版本控制使用约定-20260918.md`](docs/版本控制使用约定-20260918.md) |

> 引用本仓数字时**必须连同口径与时点一起引用**；`基线` 一词在本工程有 5 种含义，见方案文档开头的「术语约定」。

## 版本控制约定

- 分支 `master`；标签 `baseline-20260918`（S0–S5 优化完成快照）、`nav-consolidate-20260918`。
- **一次提交只装一件事**；改用 `git add <具体路径>`，**不要用 `git add -A`**（并行改动会把别人的改动卷进你的提交）。
- **`.gitignore` 模式必须用前导 `/` 锚定根目录**：不带 `/` 的模式在**任意深度**匹配 —— 曾因此把全部 14 个包的 `__init__.py` 一并忽略，导致**全新克隆无法 import**。新增规则后请用 `git check-ignore -v <关键文件>` 反查。
- **`__init__.py` 必须入库**（承载 re-export 与 router 装配，缺了克隆即崩）。
- 本仓库 `core.autocrlf=false`（源码是混合换行，开启自动转换会造成全量 diff）。
- 引用代码优先写 `模块.符号`，不要只写行号（行号随结构变更持续失效）。
