"""插件能力 → 运行时条目投影（skill / mcp / prompt / agent / tool）。"""
import json

from skills.parser import parse_frontmatter  # P2-2：frontmatter 解析唯一实现
from plugin_system.store.files import read_server_json, read_skill_md
from plugin_system.store.queries import get_plugin
from plugin_system.store.visibility import consumable_plugin_ids


# ── P0-5：插件能力 → Agent 运行时展开（市场统一后，插件技能可被对话自动触发）──
_SKILL_JSON_FIELDS = ("triggers", "dependencies", "allowed_roles", "allowed_tools",
                      "references", "examples", "scripts")


def skill_entry_from_plugin(conn, plugin_id):
    """插件行（type=skill/bundle）→ 技能池条目（SKILL.md frontmatter 展开）。

    返回与 agent/pipeline._global_skill_pool 相同结构的 dict（name=plugin_id 保证唯一，
    避免与 skills 表技能重名冲突；label.zh_CN 参与语义匹配文本）。
    无 SKILL.md 或非技能型插件 → None。MCP 型插件由 mcp_entry_from_plugin 处理。
    """
    p = get_plugin(conn, plugin_id)
    if not p:
        return None
    if p["type"] not in ("skill", "bundle"):
        return None
    md = read_skill_md(plugin_id)
    if not md:
        return None
    fm = parse_frontmatter(md)          # P2-2：统一解析（含 triggers 列表/priority）
    import re as _re
    _m = _re.match(r"^---\s*\n.*?\n---\s*\n?(.*)$", md, _re.S)
    body = _m.group(1).strip() if _m else md.strip()
    try:
        manifest = json.loads(p.get("manifest_json") or "{}")
    except Exception:
        manifest = {}
    label = manifest.get("label") or {}
    desc = fm.get("description") or p.get("description") or label.get("zh_CN") or p.get("name")
    entry = {
        "type": "skill",                         # 与 skills 表条目同构（get_bound_skills 过滤键）
        "name": plugin_id,                       # 唯一键（语义匹配文本含中文名，不影响命中）
        "display_name": label.get("zh_CN") or p.get("name"),
        "plugin_id": plugin_id,
        "source": "plugin",
        "description": desc,
        "triggers": fm.get("triggers") or [],
        "content": body or desc,
        "frontmatter": md[:400],
        "allowed_tools": fm.get("allowed_tools") or [],
        "references": [], "examples": [], "scripts": [],
        "version": p.get("current_version") or "",
        "skill_type": "plugin",
    }
    return entry


def mcp_entry_from_plugin(conn, plugin_id):
    """插件行（type=mcp/bundle）→ MCP 工具条目（server.json 描述）。"""
    p = get_plugin(conn, plugin_id)
    if not p:
        return None
    if p["type"] not in ("mcp", "bundle"):
        return None
    server = read_server_json(plugin_id)
    if not server:
        return None
    return {
        "type": "mcp",
        "name": p["plugin_id"],
        "display_name": p["name"],
        "source": "plugin",
        "desc": f"MCP 插件（{p['name']}）",
        "endpoint": server.get("base_url") or "",
        "transport": server.get("transport") or "streamable_http",
        "tools": server.get("tools") or [],
        "params": {},
    }


def plugin_manifest(conn, plugin_id) -> tuple:
    """(插件行, manifest dict)；不存在 → (None, {})。"""
    p = get_plugin(conn, plugin_id)
    if not p:
        return None, {}
    try:
        return p, json.loads(p.get("manifest_json") or "{}")
    except Exception:
        return p, {}


def prompt_entry_from_plugin(conn, plugin_id):
    """插件行（type=prompt）→ 提示词条目（与 prompts 表同构）。

    纯插件提示词的正文来自 manifest.content；未填则返回 None
    （不臆造内容 —— 宁可不可用，也不给运行时注入空模板）。
    """
    p, mf = plugin_manifest(conn, plugin_id)
    if not p or p["type"] != "prompt":
        return None
    content = (mf.get("content") or "").strip()
    if not content:
        return None
    label = mf.get("label") or {}
    return {
        "id": 0,
        "name": label.get("zh_CN") or p.get("name") or plugin_id,
        "scenario": mf.get("category") or "",
        "content": content,
        "plugin_id": plugin_id,
        "source": "plugin",
    }


def agent_entry_from_plugin(conn, plugin_id):
    """插件行（type=agent）→ Agent 定义片段。

    纯插件 Agent 必须自带 system_prompt（manifest.system_prompt）才有意义；
    缺失则返回 None —— 没有角色提示词的 Agent 参与路由只会污染意图分类。
    """
    p, mf = plugin_manifest(conn, plugin_id)
    if not p or p["type"] != "agent":
        return None
    sp = (mf.get("system_prompt") or "").strip()
    if not sp:
        return None
    caps = mf.get("capabilities") or {}
    arr = caps.get("agents") or []
    runtime_name = ""
    if arr and isinstance(arr[0], dict):
        runtime_name = (arr[0].get("name") or "").strip()
    kws = mf.get("intent_keywords") or []
    if not isinstance(kws, list):
        kws = []
    return {
        "name": runtime_name or p["plugin_id"],
        "plugin_id": plugin_id,
        "display_name": (mf.get("label") or {}).get("zh_CN") or p.get("name"),
        "description": p.get("description") or "",
        "system_prompt": sp,
        "tools": [t.get("name") for t in (caps.get("tools") or []) if isinstance(t, dict) and t.get("name")],
        "agent_role": mf.get("agent_role") or "sub",
        # 意图关键词决定该 Agent 能否被语义路由命中；缺省则只能被显式指定
        "intent_keywords": [str(k) for k in kws if str(k).strip()],
        "source": "plugin",
    }


def tool_entry_from_plugin(conn, plugin_id):
    """插件行（type=tool）→ 工具条目。

    仅当工具名存在时返回；`executable` 表示是否具备执行通道——
    有 legacy 映射（落到 tools 表执行器）或 manifest 声明 runtime.endpoint（HTTP 型）才为 True。
    无执行通道的纯元数据工具会被调用方跳过注入，避免诱导 LLM 调用必败工具。
    """
    p, mf = plugin_manifest(conn, plugin_id)
    if not p or p["type"] != "tool":
        return None
    caps = mf.get("capabilities") or {}
    names = [t.get("name") for t in (caps.get("tools") or [])
             if isinstance(t, dict) and t.get("name")]
    if not names:
        return None
    rt = mf.get("runtime") or {}
    has_legacy = bool(rt.get("legacy_table") and rt.get("legacy_id"))
    has_http = bool(str(rt.get("endpoint") or "").strip())
    return {
        "name": names[0],
        "names": names,
        "plugin_id": plugin_id,
        "display_name": (mf.get("label") or {}).get("zh_CN") or p.get("name"),
        "description": p.get("description") or "",
        "side_effect": rt.get("side_effect") or "read",
        "executable": bool(has_legacy or has_http),
        "source": "plugin",
    }


def plugin_ids_of_types(conn, kinds, user=None, any_user=False) -> list:
    """可消费的指定类型插件 ID 列表（供 plugins 侧展开成运行时条目）。"""
    if isinstance(kinds, str):
        kinds = (kinds,)
    ids = consumable_plugin_ids(conn, user, any_user=any_user)
    if not ids or not kinds:
        return []
    qs = ",".join("?" * len(ids))
    ks = ",".join("?" * len(kinds))
    rows = conn.execute(
        "SELECT plugin_id FROM plugins WHERE plugin_id IN (%s) AND type IN (%s)" % (qs, ks),
        tuple(ids) + tuple(kinds)).fetchall()
    return [r["plugin_id"] for r in rows]
