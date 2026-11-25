"""adapters：对外部系统/存储的适配层（P1 分层归位，2026-11-25）。

图库（pyoxigraph/Fuseki）、向量索引、FTS、语义缓存、MCP、智源 HTTP、沙箱。
只允许依赖 `core.*`；**不得** import `services` / `agent` / `routers`。

⚠️ 注意与 `integrations/`（**配置数据目录**，放 `*.json` 外部集成清单）区分：
那个是数据，本包是代码。
"""
