# 外部系统接口集成（配置驱动，零代码）

本目录用于接入**外部系统接口**（建模软件、智源平台、其他 REST 服务）。

## 接入新接口只需三步（不写业务代码）

1. **复制示例**：`cp modeling_software.example.json my_modeling.json`（去掉 `.example` 后缀，注册脚本才会识别）
2. **编辑定义**：按下面的格式填写接口描述与 HTTP 调用配置
3. **注册**：`python register_http_tools.py`（幂等，可重复执行）

注册后工具进入 `tools` 表（`kind='http'`），LLM function calling 与 DAG 流程自动可见；
若定义里给了 `bind_to`，自动绑定到对应 Agent。

## 定义文件格式

一个 JSON 文件可含多个工具：`{ "工具名": 定义, ... }`。

```json
{
  "my_tool": {
    "name": "my_tool",
    "source": "modeling",
    "description": "给 LLM 看的工具说明（含接口路径与用途）",
    "side_effect": "read",
    "risk_level": "low",
    "version": "v1.0",
    "bind_to": ["design"],
    "input_schema": { "type": "object", "properties": { "...": {...} }, "required": ["..."] },
    "config": {
      "method": "POST",
      "url": "{cfg:modeling.base_url}/api/model/open",
      "headers": { "Authorization": "Bearer {cfg:modeling.token}" },
      "body": { "project": "{args:project}", "mode": "{args:mode,optional}" },
      "query": { "vc": "{args:vc}" },
      "data_path": "data",
      "timeout": 15,
      "max_response": 8000
    }
  }
}
```

## config 字段说明

| 字段 | 说明 |
|---|---|
| `method` | GET/POST/PUT/DELETE（默认 POST） |
| `url` | 请求地址模板，支持占位符 |
| `headers` | 请求头模板；渲染后为空的头自动移除（如未配置 token 时不发 Authorization） |
| `body` | JSON 请求体模板（POST/PUT） |
| `query` | URL 查询参数模板（GET 常用） |
| `data_path` | 响应 data 提取点路径（默认 `data`，无此键回退整体；支持 `data.tree` 嵌套） |
| `timeout` / `max_response` | 覆盖全局默认（`integration.timeout` / `integration.max_response`） |

## 占位符

- `{cfg:分组.键}` —— 从系统级配置读取（连接参数统一维护，见 `~/.workbuddy/mbse_config.json`）
  - 智源平台：`zhiyuan.base_url` / `zhiyuan.token` / `zhiyuan.timeout` / `zhiyuan.max_response`
  - 新建模软件：建议在配置里加 `modeling` 分组：`{"modeling": {"base_url": "...", "token": "..."}}`
- `{args:参数}` —— 从工具调用参数读取；缺失时报错（LLM 会补全后重试）
- `{args:参数,optional}` —— 缺省时渲染为空串，该字段从 body/headers/query 中移除

## 响应约定（通用兼容）

- 非 JSON → 原样文本
- JSON 含 `code` 且非 0 → 视为业务失败（取 `message`/`msg`/`error`）
- 成功 → 按 `data_path` 提取；纯文本 data（如 SysML 源码）原样返回

## 内置：智源 SysMLv2 AI（5 个工具）

已内置在 `register_http_tools.py` 中（`ZHIYUAN_TOOL_DEFS`），运行注册脚本自动迁移为
`kind='http'` 配置驱动形态，行为与之前一致。绑定：design 全量、review 2 个。

## 注意

- 连接参数（endpoint/token/超时）一律放系统级配置，不放定义文件——避免密钥落盘与重复维护
- `*.example.json` 不会被注册（仅文档示例）
- 注册脚本幂等：重复执行只会更新元数据，不产生重复行
