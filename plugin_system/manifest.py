"""manifest 校验器：统一插件模型（Plugin = Skill + MCP 统一抽象）的唯一契约。

对齐设计方案 §3.2/§3.3（OpenAI Plugins / Claude Skills / Dify manifest 规范）：
- 必填：id（反向域名）、name（小写连字符 ≤64）、description（≤1024，触发匹配核心）、
  version（semver）、capabilities（skills/mcp 至少一项）
- 可选：label/author/icon/license/permissions/runtime/resource，缺失时按最小权限补齐默认值
- permissions 最小权限声明：tools 白名单 / models / storage / network.domains，缺省全关

P0 用途：POST /api/plugins 创建、PUT 更新时校验；打包上传（P2）复用同一入口。
"""
import re

# ── 常量 ──
# 权威类型枚举（单一真理源）：此处是唯一定义处，其余模块一律 import 引用，
# 禁止再写字面量元组。2026-09-16 修复：此前本处为 (skill|mcp|bundle)，而
# routers/studio_parts/shared.py 的 _VALID_MARKET_KIND 为 (skill|mcp|tool)，
# 两边不一致导致 tool 类能力永远无法通过校验入库、市场 kind=tool 恒为空。

# 可创建的类型（bundle = 多能力混合包）
PLUGIN_TYPES = ("skill", "mcp", "tool", "agent", "prompt", "bundle")
# 市场对外暴露的类型（bundle 属内部组合类型，不在市场单列展示）
MARKET_TYPES = ("skill", "mcp", "tool", "agent", "prompt")
# capabilities 槽位：至少一项非空；槽位与 type 一一对应
#   skill→skills / mcp→mcp / tool→tools / agent→agents / prompt→prompts
CAP_SLOTS = ("skills", "mcp", "tools", "agents", "prompts")
# type → 必填的 capabilities 槽位（新增类型严格，存量类型保持宽松以免冲击编辑路径）
_TYPE_REQUIRED_SLOT = {"skill": "skills", "mcp": "mcp", "tool": "tools",
                       "agent": "agents", "prompt": "prompts"}

MAX_NAME_LEN = 64
MAX_DESC_LEN = 1024
MAX_ID_LEN = 128

# 校验规则
_RE_ID = re.compile(r"^[a-z][a-z0-9]*(\.[a-z0-9][a-z0-9-]*)+$")          # 反向域名 com.zhiyuan.mbse-modeling
_RE_NAME = re.compile(r"^[a-z][a-z0-9-]{0,63}$")                          # 小写连字符，≤64
_RE_VERSION = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")   # semver 严格三段（允许 0.x 预发布期）
_RE_PATH = re.compile(r"^[a-zA-Z0-9_./-]+$")                              # capabilities 路径白名单（防路径注入）
_RE_TOOL_NAME = re.compile(r"^[a-z\u4e00-\u9fa5][a-z0-9_\u4e00-\u9fa5]{0,63}$")
# 工具名规则：小写字母/数字/下划线开头，**允许中文**。
# 2026-09-16：存量 tools 表存在中文工具名（如「知识库查询」），校验器不应拒绝真实数据。
# 仍禁止空格/连字符/特殊符号，避免注入与 shell 拼接风险。


def validate_version(version):
    """semver 严格校验：x.y.z，返回 (ok, error)。"""
    if not isinstance(version, str) or not _RE_VERSION.match(version.strip()):
        return False, f"version 必须为 semver 三段式（如 1.0.0），收到: {version!r}"
    return True, ""


def _default_permissions():
    """最小权限声明：一切默认关闭（对齐 Dify permission 最小化原则）。"""
    return {
        "tools": [],
        "models": {"enabled": False, "llm": False},
        "storage": {"enabled": False},
        "network": {"domains": []},
    }


def _check_permissions(perms):
    """permissions 白名单字段校验（未知字段丢弃，不阻断）。"""
    out = _default_permissions()
    if not isinstance(perms, dict):
        return out
    tools = perms.get("tools")
    if isinstance(tools, list):
        clean = [t for t in tools if isinstance(t, str) and len(t) <= 128]
        out["tools"] = clean
    for section in ("models", "storage"):
        v = perms.get(section)
        if isinstance(v, dict) and isinstance(v.get("enabled"), bool):
            out[section]["enabled"] = v["enabled"]
            if section == "models" and isinstance(v.get("llm"), bool):
                out[section]["llm"] = v["llm"]
    domains = (perms.get("network") or {}).get("domains") if isinstance(perms.get("network"), dict) else None
    if isinstance(domains, list):
        out["network"]["domains"] = [d for d in domains if isinstance(d, str) and len(d) <= 255]
    return out


def validate_manifest(manifest):
    """校验并标准化 manifest（对齐设计方案 §3.2 必填规则）。

    返回 (ok, errors, normalized)。normalized 补齐默认字段，可直接落库 manifest_json。
    - 校验失败时 errors 为可读错误列表（按字段分组），normalized 为 None。
    """
    errors = []
    if not isinstance(manifest, dict):
        return False, ["manifest 必须是 JSON 对象"], None

    # ── id：反向域名，唯一（唯一性由上层 store 检查）──
    pid = manifest.get("id")
    if not isinstance(pid, str) or not _RE_ID.match(pid.strip()):
        errors.append("id 必须为反向域名（如 com.zhiyuan.mbse-modeling），且仅含小写字母/数字/连字符")
    elif len(pid) > MAX_ID_LEN:
        errors.append(f"id 长度不能超过 {MAX_ID_LEN}")

    # ── name：小写连字符 ≤64 ──
    name = manifest.get("name")
    if not isinstance(name, str) or not _RE_NAME.match(name.strip()):
        errors.append(f"name 必须为小写连字符命名（a-z/0-9/-），长度 ≤{MAX_NAME_LEN}")
    elif len(name) > MAX_NAME_LEN:
        errors.append(f"name 长度不能超过 {MAX_NAME_LEN}")

    # ── description：触发匹配核心 ──
    desc = manifest.get("description")
    if not isinstance(desc, str) or not desc.strip():
        errors.append("description 必填（≤1024 字符，是 Agent 触发匹配的唯一依据，需含场景关键词）")
    elif len(desc) > MAX_DESC_LEN:
        errors.append(f"description 长度不能超过 {MAX_DESC_LEN}")

    # ── version：semver ──
    version = manifest.get("version")
    ok_v, err_v = validate_version(version)
    if not ok_v:
        errors.append(err_v)

    # ── type：skill | mcp | bundle（默认 bundle）──
    ptype = manifest.get("type", "bundle")
    if ptype not in PLUGIN_TYPES:
        errors.append(f"type 必须是 {'/'.join(PLUGIN_TYPES)} 之一")

    # ── capabilities：五个槽位至少一项非空；各槽位元素按类型校验 ──
    caps = manifest.get("capabilities")
    slots_txt = "/".join(CAP_SLOTS)
    if not isinstance(caps, dict):
        errors.append(f"capabilities 必填：{{{slots_txt}}} 至少一项")
    else:
        vals = {k: (caps.get(k) or []) for k in CAP_SLOTS}
        if all(isinstance(v, list) for v in vals.values()):
            if not any(vals.values()):
                errors.append(f"capabilities.{slots_txt} 至少一项非空（type=bundle 时多项可并存）")
            # 新增类型严格：必须声明与 type 对应的槽位。
            # skill/mcp 保持宽松以免冲击存量编辑路径；tool/agent/prompt 为新增，可严格。
            req = _TYPE_REQUIRED_SLOT.get(ptype)
            if req and not vals.get(req):
                errors.append(f"type={ptype} 的插件必须声明 capabilities.{req}（至少一项）")
            for s in vals["skills"]:
                p = s.get("path") if isinstance(s, dict) else None
                if not isinstance(p, str) or not _RE_PATH.match(p):
                    errors.append(f"skills 项 path 不合法: {p!r}（仅允许字母/数字/_./-）")
            for m in vals["mcp"]:
                p = m.get("server") if isinstance(m, dict) else None
                if not isinstance(p, str) or not _RE_PATH.match(p):
                    errors.append(f"mcp 项 server 不合法: {p!r}（仅允许字母/数字/_./-）")
            for t in vals["tools"]:
                n = t.get("name") if isinstance(t, dict) else None
                if not isinstance(n, str) or not _RE_TOOL_NAME.match(n):
                    errors.append(f"tools 项 name 不合法: {n!r}（仅允许小写字母/数字/_，≤64）")
            for a in vals["agents"]:
                n = a.get("name") if isinstance(a, dict) else None
                if not isinstance(n, str) or not str(n).strip():
                    errors.append(f"agents 项 name 必填且非空: {n!r}")
            for pm in vals["prompts"]:
                n = pm.get("name") if isinstance(pm, dict) else None
                if not isinstance(n, str) or not str(n).strip():
                    errors.append(f"prompts 项 name 必填且非空: {n!r}")
        else:
            errors.append(f"capabilities.{slots_txt} 必须为数组")

    if errors:
        return False, errors, None

    # ── 标准化（补齐默认字段）──
    pid = pid.strip()
    name = name.strip()
    now_iso = _now_iso()
    normalized = {
        "id": pid,
        "name": name,
        "label": manifest.get("label") or {"zh_CN": name, "en_US": name},
        "description": desc.strip(),
        "version": str(version).strip(),
        "type": ptype,
        "author": manifest.get("author") or {},
        "icon": manifest.get("icon", ""),
        "license": manifest.get("license", "Proprietary"),
        "created_at": manifest.get("created_at", now_iso),
        "capabilities": caps,
        # manifest v2 依赖契约：借 Cordis inject 语义（capabilities/tools/models 三类）
        "dependencies": manifest.get("dependencies") or {},
        "permissions": _check_permissions(manifest.get("permissions")),
        "runtime": manifest.get("runtime") or {},
        "resource": manifest.get("resource") or {},
    }
    # ── agent 专属语义字段（2026-09-16）──
    # 主/子角色 + 所属主 Agent 列表，驱动前端的「编排 Agent / 专业 Agent」上下分栏。
    # ⚠️ normalized 是硬白名单：新字段不加进来会被**静默丢弃**。
    #    同一个坑 dependencies 已经踩过一次，这次务必带上回归断言（见 tmp/verify_p0_fix.py 第 8 组）。
    if ptype == "agent":
        _role = str(manifest.get("agent_role") or "").strip()
        if _role in ("main", "sub"):
            normalized["agent_role"] = _role
        _parents = manifest.get("parent_agents")
        if isinstance(_parents, list) and _parents:
            normalized["parent_agents"] = [str(p).strip() for p in _parents if str(p).strip()]
    # ── 自携带内容字段（2026-09-16，P1-6「安装即可消费」）──
    # 纯插件能力（无旧表承载）要能被运行时消费，必须自带语义内容：
    #   prompt → content（提示词正文）；agent → system_prompt + intent_keywords（角色与路由）
    # 缺这些字段，能力中心新建的插件就只是「空壳元数据」，运行时拿不到任何可用内容。
    # ⚠️ 再强调：normalized 是硬白名单，新字段不加进来会被**静默丢弃**
    #    （dependencies、agent_role 已各踩过一次，见 tmp/verify_p0_fix.py 第 8/9 组断言）。
    _content = str(manifest.get("content") or "").strip()
    if _content:
        normalized["content"] = _content[:20000]
    _sp = str(manifest.get("system_prompt") or "").strip()
    if _sp:
        normalized["system_prompt"] = _sp[:20000]
    _kws = manifest.get("intent_keywords")
    if isinstance(_kws, list) and _kws:
        normalized["intent_keywords"] = [str(k).strip() for k in _kws if str(k).strip()][:50]
    return True, [], normalized


# ── 依赖契约解析（manifest v2，2026-09-16）──
# 借 Cordis inject 的声明式依赖语义，但保持纯函数、无 DB 依赖，便于单测。
# manifest.dependencies 结构：
#   {"capabilities": ["com.mbse.graph-query", {"ref": "com.x", "required": false}],
#    "tools": ["query_graph"], "models": ["default_llm"]}
# 单项支持字符串简写与 {ref, required} 对象两种写法（向后兼容）。
_DEP_KIND_MAP = {"capabilities": "capability", "tools": "tool",
                 "models": "model", "runtime": "runtime"}


def parse_dependencies(manifest: dict) -> list:
    """manifest.dependencies → 扁平依赖行（纯函数，不落库）。

    返回 [{provider_ref, provider_id, kind, required}]；非法/缺失一律忽略 ——
    依赖属元信息，不应阻断插件主流程。
    """
    deps = (manifest or {}).get("dependencies")
    if not isinstance(deps, dict):
        return []
    out, seen = [], set()
    for key, kind in _DEP_KIND_MAP.items():
        items = deps.get(key)
        if not isinstance(items, list):
            continue
        for it in items:
            ref, required = "", True
            if isinstance(it, str):
                ref = it.strip()
            elif isinstance(it, dict):
                ref = str(it.get("ref") or it.get("name") or "").strip()
                required = bool(it.get("required", True))
            if not ref:
                continue
            # capability 的 ref 本身就是 plugin_id（可直连 provider_id）；其余加前缀避免命名空间撞车
            provider_ref = ref if kind == "capability" else f"{kind}:{ref}"
            if provider_ref in seen:
                continue
            seen.add(provider_ref)
            out.append({"provider_ref": provider_ref,
                        "provider_id": ref if kind == "capability" else "",
                        "kind": kind, "required": 1 if required else 0})
    return out


def _now_iso():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
