"""智源（MBSE 平台 CopilotSysmlv2AiController）SysMLv2 AI 接口 REST 客户端。

将平台暴露的 5 个 AI 接口封装为系统工具（source='zhiyuan'），供
AgentPipeline（LLM function calling）与 ToolExecutor（DAG 流程）统一执行：

| 工具名                     | 接口                          | 用途                     |
|---------------------------|-------------------------------|--------------------------|
| zhiyuan_sysmlv2_gen       | POST /api/project/ai/sysmlv2/gen     | 从建模数据生成 SysMLv2 源码 |
| zhiyuan_sysmlv2_check     | POST /api/project/ai/sysmlv2/check   | 检测 SysMLv2 文本语法      |
| zhiyuan_sysmlv2_import    | POST /api/project/ai/sysmlv2/import  | 覆盖导入 SysMLv2 文本      |
| zhiyuan_project_list      | POST /api/project/ai/project/list    | 查询工程列表（含导入工程）  |
| zhiyuan_project_tree      | POST /api/project/ai/project/tree    | 查询当前分支包结构树       |

接口约定（见 sysmlv2_AI_接口对接清单.md v1.2）：
- 全部 POST + application/json，请求体含必填 vc（branchId,queryType[,versionNumber]）
- 响应为平台统一外层 Result<T>：{code, message, data}，code==0 表示成功
- 鉴权由平台网关承担，本客户端按配置附带 Bearer token / 自定义 header

配置（系统级，统一由 core.config 管理，不再依赖工程内 json）：
- 环境变量：ZHIYUAN_BASE_URL / ZHIYUAN_API_TOKEN（部署覆盖，最高优先级）
- 系统级配置：~/.workbuddy/mbse_config.json 的 zhiyuan 分组（base_url/token/headers/timeout/max_response）
- 默认值：base_url 为空（未配置）时 exec_tool 返回结构化配置错误（LLM 可恢复，不抛 500）
"""
import json

BASE_PATH = "/api/project/ai"

# 响应截断上限（防止打爆 LLM context）——可被配置项 zhiyuan.max_response 覆盖
DEFAULT_MAX_RESPONSE = 8000

# ── 工具定义（注册脚本与执行器共用的事实源）──
TOOL_DEFS = [
    {
        "name": "zhiyuan_sysmlv2_gen",
        "description": "从 MBSE 平台建模数据生成 SysMLv2 源码文本（智源 /api/project/ai/sysmlv2/gen）。packageDataId 为空时按整个工程生成，非空时按指定包生成",
        "side_effect": "read",
        "risk_level": "low",
        "version": "v1.2",
        "input_schema": {
            "type": "object",
            "properties": {
                "vc": {"type": "string", "description": "版本控制上下文，格式 branchId,queryType[,versionNumber]，如 1,0（草稿）/ 100,1,5（已发布 v5）"},
                "packageDataId": {"type": "integer", "description": "目标包 dataId；为空时按整个工程生成（可选）"},
            },
            "required": ["vc"],
        },
    },
    {
        "name": "zhiyuan_sysmlv2_check",
        "description": "检测 SysMLv2 文本语法（智源 /api/project/ai/sysmlv2/check，OMG SysMLInteractive 解析器）。返回 valid 与结构化错误列表（行列/偏移/消息）",
        "side_effect": "read",
        "risk_level": "low",
        "version": "v1.2",
        "input_schema": {
            "type": "object",
            "properties": {
                "vc": {"type": "string", "description": "版本控制上下文，格式 branchId,queryType[,versionNumber]，如 1,0"},
                "targetPackageDataId": {"type": "integer", "description": "目标包 dataId（保留并替换其内容）"},
                "text": {"type": "string", "description": "待检测的 SysMLv2 文本"},
            },
            "required": ["vc", "targetPackageDataId", "text"],
        },
    },
    {
        "name": "zhiyuan_sysmlv2_import",
        "description": "覆盖导入 SysMLv2 文本到 MBSE 平台（智源 /api/project/ai/sysmlv2/import）。保留目标包及其 dataId，删除其子树后写入新顶层元素；仅支持草稿上下文（vc queryType=0），需分支 PROJECT_EDIT 权限",
        "side_effect": "write",
        "risk_level": "medium",
        "version": "v1.2",
        "input_schema": {
            "type": "object",
            "properties": {
                "vc": {"type": "string", "description": "版本控制上下文，必须为草稿上下文，格式 branchId,0"},
                "text": {"type": "string", "description": "SysMLv2 文本（UTF-8，换行符 \\n）"},
                "targetPackageDataId": {"type": "integer", "description": "目标包 dataId；为空时使用当前工程根包（可选）"},
            },
            "required": ["vc", "text"],
        },
    },
    {
        "name": "zhiyuan_project_list",
        "description": "查询 MBSE 平台当前工程列表（智源 /api/project/ai/project/list，含当前工程 + 导入工程）。导入工程返回 vc（branchId,1,version）可直接回填复用",
        "side_effect": "read",
        "risk_level": "low",
        "version": "v1.2",
        "input_schema": {
            "type": "object",
            "properties": {
                "vc": {"type": "string", "description": "版本控制上下文，格式 branchId,queryType[,versionNumber]，如 1,0"},
            },
            "required": ["vc"],
        },
    },
    {
        "name": "zhiyuan_project_tree",
        "description": "查询 MBSE 平台当前分支包结构树（智源 /api/project/ai/project/tree）。返回嵌套 AiTreeDTO（dataId/name/qualifiedName/children/isLeaf/projectId）",
        "side_effect": "read",
        "risk_level": "low",
        "version": "v1.2",
        "input_schema": {
            "type": "object",
            "properties": {
                "vc": {"type": "string", "description": "版本控制上下文，格式 branchId,queryType[,versionNumber]，如 1,0"},
            },
            "required": ["vc"],
        },
    },
]

# 工具名 → 客户端方法名
_HANDLER_MAP = {
    "zhiyuan_sysmlv2_gen": "sysmlv2_gen",
    "zhiyuan_sysmlv2_check": "sysmlv2_check",
    "zhiyuan_sysmlv2_import": "sysmlv2_import",
    "zhiyuan_project_list": "project_list",
    "zhiyuan_project_tree": "project_tree",
}


def _load_config() -> dict:
    """从系统级配置模块读取智源配置（core.config，环境变量 > ~/.workbuddy/mbse_config.json > 默认值）。"""
    from core import config as _cfg
    return {
        "base_url": _cfg.ZHIYUAN_BASE_URL,
        "token": _cfg.ZHIYUAN_API_TOKEN,
        "headers": _cfg.get("zhiyuan", "headers", {}) or {},
        "timeout": int(_cfg.get("zhiyuan", "timeout", 15)),
        "max_response": int(_cfg.get("zhiyuan", "max_response", DEFAULT_MAX_RESPONSE)),
        "default_vc": _cfg.get("zhiyuan", "default_vc", "") or "",
    }


def _config_error(msg: str) -> dict:
    return {"ok": False, "result": msg}


class ZhiyuanClient:
    """智源 SysMLv2 AI 接口客户端（轻量 REST，无外部依赖）。

    timeout/max_response 默认取系统配置（core.config 的 zhiyuan 分组）。
    """

    TIMEOUT = 15

    def __init__(self, base_url: str = "", token: str = "", headers: dict | None = None,
                 timeout: int | None = None, max_response: int | None = None):
        self.base_url = (base_url or "").rstrip("/")
        self.token = token or ""
        self.extra_headers = headers or {}
        self.timeout = timeout if timeout is not None else self.TIMEOUT
        self.max_response = max_response if max_response is not None else DEFAULT_MAX_RESPONSE

    # ── 鉴权 / 请求头 ──
    def _headers(self) -> dict:
        h = {"Content-Type": "application/json;charset=UTF-8"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        for k, v in (self.extra_headers or {}).items():
            h[k] = v
        return h

    # ── 核心 POST：统一 Result<T> 解析 + 截断 ──
    def _post(self, path: str, body: dict) -> dict:
        if not self.base_url:
            return _config_error(
                "智源平台未配置 base_url：请设置环境变量 ZHIYUAN_BASE_URL（与 ZHIYUAN_API_TOKEN），"
                "或在系统级配置 ~/.workbuddy/mbse_config.json 的 zhiyuan 分组中配置 base_url/token"
            )
        url = self.base_url + path
        try:
            data = self._post_json(url, body)
        except Exception as e:
            return {"ok": False, "result": f"智源接口调用失败: {e}"}
        # 平台统一外层 Result<T>：实测成功码 code==200（或 0）；业务失败 HTTP 仍为 200，code 非成功码
        code = data.get("code") if isinstance(data, dict) else None
        ok_codes = (0, 200, "0", "200")
        if code not in ok_codes and data.get("msg") not in ("success", None):
            return {"ok": False, "result": f"智源业务失败(code={code}): {data.get('message') or data.get('msg') or '未知错误'}"}
        payload = data.get("data", data) if isinstance(data, dict) else data
        text = json.dumps(payload, ensure_ascii=False)
        if len(text) > self.max_response:
            text = text[: self.max_response] + f"…(已截断，总长 {len(json.dumps(payload, ensure_ascii=False))} 字符)"
        return {"ok": True, "result": text}

    # ── HTTP POST（httpx 优先，缺失时用标准库 urllib 兜底，保证零外部依赖可用）──
    def _post_json(self, url: str, body: dict) -> dict:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        try:
            import httpx
            resp = httpx.post(url, json=body, headers=self._headers(), timeout=self.timeout)
            resp.raise_for_status()
            return resp.json()
        except ImportError:
            import urllib.request
            import urllib.error
            req = urllib.request.Request(url, data=payload, headers=self._headers(),
                                         method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    return json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                raise RuntimeError(f"HTTP {e.code}: {e.read().decode('utf-8', 'ignore')[:200]}")
        except Exception:
            raise

    # ── 5 个接口方法 ──
    def sysmlv2_gen(self, vc: str, package_data_id: int | None = None) -> dict:
        body = {"vc": vc}
        if package_data_id is not None:
            body["packageDataId"] = package_data_id
        return self._post(f"{BASE_PATH}/sysmlv2/gen", body)

    def sysmlv2_check(self, vc: str, target_package_data_id: int, text: str) -> dict:
        return self._post(f"{BASE_PATH}/sysmlv2/check", {
            "vc": vc,
            "targetPackageDataId": target_package_data_id,
            "text": text,
        })

    def sysmlv2_import(self, vc: str, text: str, target_package_data_id: int | None = None) -> dict:
        body = {"vc": vc, "text": text}
        if target_package_data_id is not None:
            body["targetPackageDataId"] = target_package_data_id
        return self._post(f"{BASE_PATH}/sysmlv2/import", body)

    def project_list(self, vc: str) -> dict:
        return self._post(f"{BASE_PATH}/project/list", {"vc": vc})

    def project_tree(self, vc: str) -> dict:
        return self._post(f"{BASE_PATH}/project/tree", {"vc": vc})


def exec_tool(name: str, arguments: dict | None = None) -> dict:
    """统一执行入口（AgentPipeline / ToolExecutor 复用）。

    按工具名映射到客户端方法；参数做安全提取（缺失字段由平台侧校验兜底）。
    vc 自动回填：LLM 未传 vc 时用配置的 default_vc（当前工程草稿分支），agent 开箱即用。
    返回标准化 {ok, result}——result 为字符串，供 LLM 消费。
    """
    arguments = dict(arguments or {})
    method = _HANDLER_MAP.get(name)
    if not method:
        return {"ok": False, "result": f"未知智源工具: {name}"}
    cfg = _load_config()
    client = ZhiyuanClient(cfg["base_url"], cfg["token"], cfg["headers"] or {},
                           timeout=cfg.get("timeout"), max_response=cfg.get("max_response"))
    # vc 自动回填：参数缺失/为空 → default_vc
    if not str(arguments.get("vc") or "").strip():
        if not cfg.get("default_vc"):
            return {"ok": False, "result": "智源工具缺少 vc 参数，且未配置 zhiyuan.default_vc（请配置当前工程分支上下文）"}
        arguments["vc"] = cfg["default_vc"]
    try:
        if method == "sysmlv2_gen":
            pid = arguments.get("packageDataId") or arguments.get("package_data_id")
            return client.sysmlv2_gen(str(arguments.get("vc", "")), int(pid) if pid is not None else None)
        if method == "sysmlv2_check":
            return client.sysmlv2_check(
                str(arguments.get("vc", "")),
                int(arguments.get("targetPackageDataId") or arguments.get("target_package_data_id") or 0),
                str(arguments.get("text", "")),
            )
        if method == "sysmlv2_import":
            pid = arguments.get("targetPackageDataId") or arguments.get("target_package_data_id")
            return client.sysmlv2_import(
                str(arguments.get("vc", "")),
                str(arguments.get("text", "")),
                int(pid) if pid is not None else None,
            )
        if method == "project_list":
            return client.project_list(str(arguments.get("vc", "")))
        if method == "project_tree":
            return client.project_tree(str(arguments.get("vc", "")))
    except Exception as e:
        return {"ok": False, "result": f"智源工具「{name}」调用失败: {str(e)[:200]}"}
    return {"ok": False, "result": f"智源工具「{name}」参数缺失"}


# 全局单例（与其它模块风格一致）
zhiyuan_client = ZhiyuanClient()
