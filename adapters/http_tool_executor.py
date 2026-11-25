"""通用 HTTP 工具执行器：配置驱动，适配不同建模软件/外部系统接口，零代码接入。

工具在 tools 表注册（kind='http'，config 存 HTTP 调用描述），运行时由本模块
按 config 渲染请求并调用，返回标准化 {ok, result} 供 AgentPipeline / ToolExecutor 消费。
外部系统接入 = 写一段 JSON 配置 + 注册，不再需要开发业务代码。

config JSON 结构（tools.config）：
{
  "method": "POST",                        // 默认 POST
  "url": "https://{cfg:modeling.base_url}/api/model/open",   // 支持占位符
  "headers": {"Authorization": "Bearer {cfg:modeling.token}", "X-Project": "{args:project}"},
  "body": {"name": "{args:name}", "attrs": "{args:attrs,optional}"},
  "query": {"vc": "{args:vc}"},            // URL 查询参数（可选）
  "data_path": "data",                     // 响应 data 提取点路径（默认 data，无此键回退整体）
  "timeout": 15,                           // 覆盖全局默认（integration.timeout）
  "max_response": 8000                     // 覆盖全局默认（integration.max_response）
}

占位符：
- {cfg:section.key}    从系统配置 core.config 读取（连接参数统一维护：endpoint/token/超时）
- {args:key}           从工具调用参数读取；缺失报错
- {args:key,optional}  缺失时渲染为空串；渲染结果为空串的键从 body/headers/query 移除

参数缺省值（config.defaults）：
- "defaults": {"vc": "{cfg:zhiyuan.default_vc}"}   // 调用参数缺失的键先按 defaults 补齐（值支持 cfg 占位符），
                                                     再参与渲染——LLM 未传 vc 等上下文参数时自动回落系统配置

响应约定（通用兼容）：
- 非 JSON → 原样文本
- JSON 含 code 且非 0 → 业务失败（message/msg/error）
- 成功 → 按 data_path 提取；提取结果为纯文本（如 SysML 源码）原样返回，结构数据 JSON 序列化
"""
import json
import re

_RE_CFG = re.compile(r"\{cfg:([A-Za-z0-9_.]+)\}")
_RE_ARG = re.compile(r"\{args:([A-Za-z0-9_.]+?)(?:,([a-z]+)(?:,([a-z]+))?)?\}")


def _coerce_arg(raw, type_flag: str):
    """按类型 flag 转换参数值（int/float/bool；无 flag 保持原值）。"""
    if type_flag == "int":
        return int(str(raw).strip())
    if type_flag == "float":
        return float(str(raw).strip())
    if type_flag == "bool":
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    return raw


def _parse_arg_flags(m) -> tuple:
    """解析占位符 flags：(key, type_flag, optional)。"""
    key, f1, f2 = m.group(1), m.group(2), m.group(3)
    optional = (f1 == "optional") or (f2 == "optional")
    type_flag = f1 if f1 and f1 != "optional" else (f2 if f2 and f2 != "optional" else "")
    return key, type_flag, optional

DEFAULT_DATA_PATH = "data"


def _cfg_value(path: str) -> str:
    """{cfg:section.key} → core.config 读取；未配置返回空串。"""
    parts = path.split(".")
    if len(parts) < 2:
        return ""
    from core import config as _syscfg
    v = _syscfg.get(parts[0], ".".join(parts[1:]), "")
    return str(v if v is not None else "")


def _render_str(s: str, arguments: dict) -> str:
    """渲染字符串模板（URL/header/query 文本场景）：先替换 {cfg:...}，再替换 {args:...}。

    必填参数缺失抛 KeyError；optional 缺失渲染为空串（上层映射据此移除该键）。
    类型 flag（int/float/bool）转换后转回文本。
    """
    s = _RE_CFG.sub(lambda m: _cfg_value(m.group(1)), s)

    out = []
    pos = 0
    for m in _RE_ARG.finditer(s):
        out.append(s[pos:m.start()])
        key, type_flag, optional = _parse_arg_flags(m)
        if key in arguments and arguments[key] is not None:
            out.append(str(_coerce_arg(arguments[key], type_flag)))
        elif optional:
            out.append("")
        else:
            raise KeyError(key)
        pos = m.end()
    out.append(s[pos:])
    return "".join(out)


def _render_value(v, arguments: dict):
    """递归渲染模板值（dict/list/str/标量）。

    body 场景：整个字符串为单个占位符时返回原生类型（如 {args:packageDataId,int} → int），
    组合模板走 _render_str 文本渲染。
    """
    if isinstance(v, dict):
        return _render_mapping(v, arguments)
    if isinstance(v, list):
        return [_render_value(x, arguments) for x in v]
    if isinstance(v, str):
        m = re.fullmatch(r"\{args:([A-Za-z0-9_.]+?)(?:,([a-z]+)(?:,([a-z]+))?)?\}", v.strip())
        if m:
            key, type_flag, optional = _parse_arg_flags(m)
            if key in arguments and arguments[key] is not None:
                return _coerce_arg(arguments[key], type_flag)
            if optional:
                return ""
            raise KeyError(key)
        return _render_str(v, arguments)
    return v


def _render_mapping(d: dict, arguments: dict) -> dict:
    """渲染映射；渲染结果为空串的字符串键被移除（空 token 的 header、optional 缺参等）。"""
    out = {}
    for k, v in d.items():
        rv = _render_value(v, arguments)
        if isinstance(v, str) and str(rv).strip() == "":
            continue
        out[k] = rv
    return out


def _get_tool_config(name: str) -> str | None:
    from database import get_db
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT config FROM tools WHERE name=? AND kind='http' AND status='active'", (name,)
        ).fetchone()
        return row["config"] if row else None
    finally:
        conn.close()


def _parse_response(resp_text: str, data_path: str, max_response: int) -> dict:
    """解析响应：Result<T> 兼容 + data_path 提取 + 截断。"""
    try:
        data = json.loads(resp_text)
    except Exception:
        return {"ok": True, "result": resp_text[:max_response]}
    # 平台统一外层 Result<T>：code 非成功码（0/200 成功）→ 业务失败（实测智源返回 code=200）
    if isinstance(data, dict) and data.get("code") not in (0, 200, None, "0", "200"):
        msg = data.get("message") or data.get("msg") or data.get("error") or "未知错误"
        return {"ok": False, "result": f"业务失败(code={data.get('code')}): {msg}"}
    # 按 data_path 提取；无对应键回退整体
    payload = data
    if isinstance(data, dict):
        cur = data
        for part in (data_path or "").split("."):
            if not part:
                continue
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                cur = None
                break
        payload = cur if cur is not None else data
    # 纯文本型 data（SysML 源码等）原样返回；结构数据序列化
    if isinstance(payload, (str, int, float, bool)) or payload is None:
        text = str(payload)
    else:
        text = json.dumps(payload, ensure_ascii=False)
    if len(text) > max_response:
        text = text[:max_response] + f"…(已截断，总长 {len(text)} 字符)"
    return {"ok": True, "result": text}


def exec_http_tool(name: str, arguments: dict | None = None) -> dict:
    """统一执行入口：按 tools.config 渲染请求 → 调用 → 标准化结果。"""
    arguments = arguments or {}
    config_json = _get_tool_config(name)
    if config_json is None:
        return {"ok": False, "result": f"未知 HTTP 集成工具: {name}"}
    try:
        cfg = json.loads(config_json or "{}") or {}
    except Exception as e:
        return {"ok": False, "result": f"工具「{name}」config 解析失败: {str(e)[:200]}"}

    # 参数缺省回落：config.defaults 中声明的键，调用参数缺失时先补默认值（值支持 cfg 占位符）
    defaults_tpl = cfg.get("defaults") or {}
    if isinstance(defaults_tpl, dict) and defaults_tpl:
        for dk, dv in defaults_tpl.items():
            if dk not in arguments or arguments[dk] is None or str(arguments[dk]).strip() == "":
                try:
                    rendered = _render_value(dv, {}) if isinstance(dv, str) else dv
                    if rendered not in ("", None):
                        arguments[dk] = rendered
                except KeyError:
                    pass  # 默认值自身引用缺失参数 → 忽略，保持原报错路径

    from core import config as _syscfg
    method = str(cfg.get("method") or "POST").upper()
    url_tpl = str(cfg.get("url") or "")
    headers_tpl = cfg.get("headers") or {}
    body_tpl = cfg.get("body")
    query_tpl = cfg.get("query") or {}
    data_path = str(cfg.get("data_path") or DEFAULT_DATA_PATH)

    # 渲染（参数缺失 → 结构化错误，LLM 可恢复）
    try:
        timeout_raw = _render_str(str(cfg.get("timeout") or ""), arguments)
        max_resp_raw = _render_str(str(cfg.get("max_response") or ""), arguments)
        timeout = int(timeout_raw) if timeout_raw.strip() else int(_syscfg.get("integration", "timeout", 15))
        max_response = int(max_resp_raw) if max_resp_raw.strip() else int(_syscfg.get("integration", "max_response", 8000))
        url = _render_str(url_tpl, arguments)
        headers = _render_mapping(headers_tpl, arguments)
        payload = _render_value(body_tpl, arguments) if body_tpl is not None else None
        params = _render_mapping(query_tpl, arguments) if query_tpl else None
    except KeyError as e:
        return {"ok": False, "result": f"工具「{name}」缺少必填参数: {e}"}
    if not url.strip() or not url.strip().startswith(("http://", "https://")):
        return {"ok": False,
                "result": f"工具「{name}」连接参数未配置：请配置 {url_tpl} 引用的配置项（如 modeling.base_url）"
                          f"或检查注册 config.url"}
    # 合并全局默认头（integration.headers），工具级 headers 优先
    for k, v in (_syscfg.get("integration", "headers", {}) or {}).items():
        headers.setdefault(k, v)
    if payload is not None and "Content-Type" not in headers:
        headers["Content-Type"] = "application/json;charset=UTF-8"

    import httpx
    try:
        _kwargs = {"params": params or None, "headers": headers, "timeout": timeout}
        if payload is not None:
            _kwargs["json"] = payload
        resp = getattr(httpx, method.lower())(url, **_kwargs)
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        return {"ok": False, "result": f"HTTP {e.response.status_code}: {url}"}
    except Exception as e:
        return {"ok": False, "result": f"调用「{name}」失败: {str(e)[:200]}"}
    return _parse_response(resp.text, data_path, max_response)


# 全局实例（与其它模块风格一致）
http_tool_executor = {"exec": exec_http_tool}
