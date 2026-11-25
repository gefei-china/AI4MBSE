# -*- coding: utf-8 -*-
"""元能力：让模型**通过会话**管理自己的能力资产（agent / skill / tool / capability）。

2026-11-07，回答用户问题「是否可以实现 agent skill 工具等的新增修改、绑定、差选、使用、修改、移除」。

## 现状盘点（这是本模块存在的理由）

| 层 | 现状 |
|---|---|
| HTTP API | **齐全**：`/api/studio/agents`、`/skills`、`/tools`、`/mcp-servers` 的 CRUD 都在 |
| 模型侧工具 | **零**：106 个 active 工具里没有一个能操作能力资产（只有 6 个 `sys_query_*` 查用户/角色/审计） |

⇒ 结果是"平台什么都能配，但只能人点鼠标配"。用户说"给我加个能力"，
AI 只能告诉他去哪一页点哪个按钮 —— 这正是本轮要消掉的断层。

## 两个工具、一条边界

| 工具 | 档位 | 动作 | 是否入确认队列 |
|---|---|---|---|
| `capability_query` | read | list / get / diff | 否（读不产生副作用） |
| `capability_apply` | write | create / update / bind / unbind / enable / disable / delete | **是** |

为什么拆两个而不是一个带 `action` 参数的：HIL 写前确认闸是**按工具名取档**的
（`tools.side_effect`）⇒ 单工具会让 `list` 也进确认队列，用户每次"查一下有哪些技能"
都要点一次批准 ⇒ 确认疲劳（OWASP ASI09 点名的失效模式），反而让人闭眼点通过。
与既有 `sys_query_*`（只读）与写工具分离的惯例一致。

## 「使用」怎么体现

用户问题里的"使用"有两层：
1. **让能力对某个 Agent 生效** ⇒ `action=bind`（写 `agent_tools`）+ `enable`
2. **立刻验证它能跑** ⇒ 绑定后可让 Agent 在下一轮调用；本轮不自动跑
   （自动跑等于"绑定即执行"，与"批准的是配置变更"不是一回事）

## 为什么 capability（自注册代码能力）走同一套

`kind='capability'` 的 create 会多做四件事：
① AST 静态审查（`core/code_guard`）② 落盘到 `data/workspace/capabilities/<name>/`
③ 副作用**重新派生**（`agent/tool_side_effect.derive`，不采信模型自报）
④ 注册为 `tools` 行（`source='capability'`）

## 一条不可绕过的纪律：提案 ≠ 批准

写动作**从不直接执行**。`capability_apply` 只做一件事：写一条 `hil_confirmations`
（`action='cap_<kind>_<act>'`），把待执行的计划放进 payload。
真正的执行在 `execute()`，由**用户在会话里点批准**触发（`hil_service.execute_confirmation`
识别 `cap_` 前缀后转到这里）。

⇒ 模型无法自己给自己发权限：它能提，不能批。「同一主体既提又批」是这个体系里
   最容易被做错的地方（审批退化成形式）。
"""
from __future__ import annotations

import json
import os
import re
import shutil

__all__ = [
    "KINDS", "ACTIONS", "READ_ACTIONS",
    "query", "preview", "propose", "execute",
    "META_ACTION_PREFIX",
]

KINDS = ("agent", "skill", "tool", "capability")
READ_ACTIONS = ("list", "get", "diff")
WRITE_ACTIONS = ("create", "update", "bind", "unbind", "enable", "disable", "delete")
ACTIONS = READ_ACTIONS + WRITE_ACTIONS

#: 确认单 action 前缀 —— `hil_service.execute_confirmation` 据此分派到本模块
META_ACTION_PREFIX = "cap_"

#: 名字合法性（运行时会当 OpenAI function.name / 路由键用）
_NAME_RE = re.compile(r"^[A-Za-z0-9_\-\u4e00-\u9fff][A-Za-z0-9_\-. \u4e00-\u9fff]{0,63}$")


def _conn():
    from database import get_db
    return get_db()


def _row2dict(r):
    return dict(r) if r is not None else None


# ══════════════════════════════════════════════════════════════════════
# 只读：list / get / diff
# ══════════════════════════════════════════════════════════════════════

def _list_agents(conn, kw="", limit=100):
    sql = ("SELECT id, name, display_name, status, agent_role, builtin, hil_level, "
           "       (SELECT COUNT(*) FROM agent_tools t WHERE t.agent_id=agents.id) AS binds "
           "FROM agents")
    args = []
    if kw:
        sql += " WHERE name LIKE ? OR display_name LIKE ? OR description LIKE ?"
        args = ["%%%s%%" % kw] * 3
    sql += " ORDER BY agent_role='main' DESC, id LIMIT ?"
    args.append(int(limit))
    return [_row2dict(r) for r in conn.execute(sql, args).fetchall()]


def _list_skills(conn, kw="", limit=100):
    sql = ("SELECT id, name, version, status, category, source, builtin, enabled, "
           "       length(content) AS content_len, scripts, package_path "
           "FROM skills")
    args = []
    if kw:
        sql += " WHERE name LIKE ? OR description LIKE ? OR triggers LIKE ?"
        args = ["%%%s%%" % kw] * 3
    sql += " ORDER BY status='published' DESC, id LIMIT ?"
    args.append(int(limit))
    out = []
    for r in conn.execute(sql, args).fetchall():
        d = _row2dict(r)
        try:
            d["scripts"] = json.loads(d.get("scripts") or "[]")
        except Exception:
            d["scripts"] = []
        d.pop("package_path", None)       # 路径不外泄（也不给模型机会拼路径）
        out.append(d)
    return out


def _list_tools(conn, kw="", limit=100):
    sql = ("SELECT id, name, source, kind, side_effect, side_effect_source, status, "
           "       builtin, mcp_server_id, (SELECT COUNT(*) FROM agent_tools t "
           "       WHERE t.tool_name=tools.name AND t.tool_type='tool') AS binds "
           "FROM tools")
    args = []
    if kw:
        sql += " WHERE name LIKE ? OR description LIKE ?"
        args = ["%%%s%%" % kw] * 2
    sql += " ORDER BY builtin DESC, name LIMIT ?"
    args.append(int(limit))
    return [_row2dict(r) for r in conn.execute(sql, args).fetchall()]


def query(kind: str, action: str, target: str = "", keyword: str = "",
          limit: int = 100) -> dict:
    """只读查询。返回 `{"ok", "result"(str), "data"(dict)}`（与 file_tools 同形态）。"""
    kind = str(kind or "").strip().lower()
    action = str(action or "").strip().lower()
    if kind not in KINDS:
        return {"ok": False, "result": "kind 只支持 %s，收到：%r" % ("/".join(KINDS), kind)}
    if action not in READ_ACTIONS:
        return {"ok": False, "result": "capability_query 只做只读动作 %s；"
                                       "写动作请用 capability_apply（会走人工确认）"
                                       % ("/".join(READ_ACTIONS))}
    conn = _conn()
    try:
        if action == "list":
            if kind == "agent":
                data = {"agents": _list_agents(conn, keyword, limit)}
            elif kind == "skill":
                data = {"skills": _list_skills(conn, keyword, limit)}
            elif kind == "tool":
                data = {"tools": _list_tools(conn, keyword, limit)}
            else:  # capability
                data = {"capabilities": [
                    r for r in _list_tools(conn, keyword, limit)
                    if (r.get("source") or "") == "capability"]}
            _n = sum(len(v) for v in data.values())
            return {"ok": True, "data": data,
                    "result": json.dumps({"count": _n, **data}, ensure_ascii=False)[:20000]}

        if action == "get":
            if not target:
                return {"ok": False, "result": "get 需要 target（名字或 id）"}
            d = _get_one(conn, kind, target)
            if not d:
                return {"ok": False, "result": "找不到 %s：%s" % (kind, target)}
            return {"ok": True, "data": d,
                    "result": json.dumps(d, ensure_ascii=False, default=str)[:20000]}

        # diff：这个能力绑到了谁 / 谁没有（"差选"）
        if kind == "tool" or kind == "capability":
            if not target:
                return {"ok": False, "result": "diff 需要 target（工具名）"}
            bound = [r[0] for r in conn.execute(
                "SELECT a.name FROM agent_tools t JOIN agents a ON a.id=t.agent_id "
                "WHERE t.tool_name=? AND t.tool_type='tool' AND a.status='active' "
                "ORDER BY a.name", (target,)).fetchall()]
            all_agents = [r[0] for r in conn.execute(
                "SELECT name FROM agents WHERE status='active' ORDER BY name").fetchall()]
            unbound = [a for a in all_agents if a not in bound]
            return {"ok": True,
                    "data": {"target": target, "bound": bound, "unbound": unbound},
                    "result": json.dumps({"target": target, "已绑定": bound,
                                          "未绑定": unbound}, ensure_ascii=False)[:8000]}
        # skill 的 diff
        bound = [r[0] for r in conn.execute(
            "SELECT a.name FROM agent_tools t JOIN agents a ON a.id=t.agent_id "
            "WHERE t.tool_name=? AND t.tool_type='skill' AND a.status='active' "
            "ORDER BY a.name", (target,)).fetchall()]
        all_agents = [r[0] for r in conn.execute(
            "SELECT name FROM agents WHERE status='active' ORDER BY name").fetchall()]
        unbound = [a for a in all_agents if a not in bound]
        return {"ok": True, "data": {"target": target, "bound": bound, "unbound": unbound},
                "result": json.dumps({"target": target, "已绑定技能": bound,
                                      "未绑定": unbound}, ensure_ascii=False)[:8000]}
    finally:
        conn.close()


def _get_one(conn, kind: str, target: str):
    t = str(target or "").strip()
    is_id = t.isdigit()
    if kind == "agent":
        r = conn.execute("SELECT * FROM agents WHERE %s=?" % ("id" if is_id else "name"),
                         (int(t) if is_id else t,)).fetchone()
        if not r:
            r = conn.execute("SELECT * FROM agents WHERE display_name=? LIMIT 1", (t,)).fetchone()
        if not r:
            return None
        d = _row2dict(r)
        d["binds"] = [dict(x) for x in conn.execute(
            "SELECT tool_type, tool_name, enabled FROM agent_tools WHERE agent_id=? "
            "ORDER BY tool_type, tool_name", (d["id"],)).fetchall()]
        d.pop("system_prompt", None) if len(str(d.get("system_prompt") or "")) < 0 else None
        return d
    if kind == "skill":
        r = conn.execute("SELECT id, name, version, status, category, description, "
                         "triggers, allowed_tools, scripts, enabled, builtin, source, "
                         "length(content) AS content_len FROM skills WHERE %s=?"
                         % ("id" if is_id else "name"),
                         (int(t) if is_id else t,)).fetchone()
        return _row2dict(r)
    r = conn.execute("SELECT * FROM tools WHERE %s=?" % ("id" if is_id else "name"),
                     (int(t) if is_id else t,)).fetchone()
    if not r:
        return None
    d = _row2dict(r)
    d["binds"] = [dict(x) for x in conn.execute(
        "SELECT a.name, a.display_name FROM agent_tools t JOIN agents a ON a.id=t.agent_id "
        "WHERE t.tool_name=? AND t.tool_type='tool'", (d["name"],)).fetchall()]
    return d


# ══════════════════════════════════════════════════════════════════════
# 写：先预览（给人看的摘要），再提案（入确认队列）
# ══════════════════════════════════════════════════════════════════════

def preview(kind: str, action: str, spec: dict) -> str:
    """生成**给人看的**计划摘要（确认卡上显示这段）。

    为什么不能只给 JSON：`_impact_preview` 的 docstring 第一句就是理由 ——
    "只给 JSON 让人批 = 没让人批"。人看不懂就会闭眼点通过。
    """
    s = spec or {}
    k, a = str(kind), str(action)
    if a == "create":
        if k == "capability":
            return ("注册新能力「%s」：%s；脚本 %d 字符，副作用将按代码自动派生；"
                    "落盘到工作沙箱 data/workspace/capabilities/%s/"
                    % (s.get("name") or "?", (s.get("description") or "")[:80],
                       len(str(s.get("script") or "")), s.get("name") or "?"))
        if k == "agent":
            return ("新建 Agent「%s」（角色 %s）：%s"
                    % (s.get("display_name") or s.get("name") or "?",
                       s.get("agent_role") or "sub", (s.get("description") or "")[:80]))
        if k == "skill":
            return ("新建技能「%s」：%s；正文 %d 字符"
                    % (s.get("name") or "?", (s.get("description") or "")[:80],
                       len(str(s.get("content") or ""))))
        return "新建工具「%s」：%s" % (s.get("name") or "?", (s.get("description") or "")[:80])
    if a == "update":
        fields = ", ".join(sorted((s.get("fields") or {}).keys()))
        return "修改 %s「%s」的字段：%s" % (k, s.get("target") or "?", fields or "（无）")
    if a == "bind":
        ags = s.get("agents") or []
        return "把 %s「%s」绑定到 %d 个 Agent：%s" % (
            k, s.get("target") or "?", len(ags), "、".join(ags[:8]) + ("…" if len(ags) > 8 else ""))
    if a == "unbind":
        ags = s.get("agents") or []
        return "解绑 %s「%s」与 %d 个 Agent：%s" % (
            k, s.get("target") or "?", len(ags), "、".join(ags[:8]) + ("…" if len(ags) > 8 else ""))
    if a in ("enable", "disable"):
        return "%s %s「%s」" % ("启用" if a == "enable" else "停用", k, s.get("target") or "?")
    if a == "delete":
        return ("**删除**%s「%s」" % (k, s.get("target") or "?")) + (
            "（将同时清理其绑定关系）" if k in ("agent", "skill", "tool") else "")
    return "%s %s" % (a, k)


def propose(kind: str, action: str, spec: dict, agent_id: str = "",
            conversation_id: int = 0, run_id: int = 0, task_key: str = "",
            user=None) -> dict:
    """把一个写动作**入确认队列**（不执行）。

    返回 `{"ok", "confirmation_id", "preview", "result"}`。
    """
    kind = str(kind or "").strip().lower()
    action = str(action or "").strip().lower()
    if kind not in KINDS:
        return {"ok": False, "result": "kind 只支持 %s，收到：%r" % ("/".join(KINDS), kind)}
    if action not in WRITE_ACTIONS:
        return {"ok": False, "result": "capability_apply 只做写动作 %s；"
                                       "查询请用 capability_query"
                                       % ("/".join(WRITE_ACTIONS))}
    spec = dict(spec or {})
    _err = _validate(kind, action, spec)
    if _err:
        return {"ok": False, "result": _err}

    pv = preview(kind, action, spec)
    # ★ 2026-11-07：把发起者一并带进 payload —— 注册完要**绑给发起者**。
    #   真机实测（会话 990512）：批准注册了 `live_bmi_calc`，下一轮模型却**没调用它**，
    #   因为"注册"只是写了 tools 行，没绑任何 Agent ⇒ 注入侧看不见。
    #   "加个能力给自己用"才是用户的原话语义 ⇒ 默认绑给发起者（spec.agents 显式给了就按给的）。
    payload = {"kind": kind, "action": action, "spec": spec,
               "proposer_agent": str(agent_id or "")}
    act = "%s%s_%s" % (META_ACTION_PREFIX, kind, action)
    try:
        from services.hil_service import HILService
        from database import get_db
        conn = get_db()
        try:
            rec = HILService.queue_confirmation(
                conn, agent_id=str(agent_id or ""), action=act, payload=payload,
                preview=pv, conversation_id=int(conversation_id or 0),
                run_id=int(run_id or 0), task_key=str(task_key or ""),
                tool_type="builtin", level="confirm")
        finally:
            conn.close()
    except Exception as e:
        return {"ok": False, "result": "入确认队列失败：%s: %s" % (type(e).__name__, str(e)[:200])}
    cid = (rec or {}).get("id")
    return {"ok": True, "confirmation_id": cid, "preview": pv,
            "result": ("⏸ 已提交人工确认（确认单 #%s）：%s\n"
                       "**在你点批准之前不会做任何改动。**不要重复提交。" % (cid, pv))}


def _validate(kind: str, action: str, spec: dict) -> str:
    """入队前的静态校验。返回错误字符串（空=通过）。

    ⚠️ 这里**只做能在入队前判的**（名字合法性、必填项、代码审查）。
       依赖库状态的校验（名字是否已存在、Agent 是否存在）留到执行时 ——
       因为确认单可能挂很久，期间状态会变；在入队时判过、执行时不复查，
       就会出现"批准了但对象已不存在"。
    """
    if action in ("create", "update", "bind", "unbind", "enable", "disable", "delete"):
        if action != "create" and not str(spec.get("target") or "").strip():
            return "%s 需要 target（目标名字或 id）" % action
    if action in ("bind", "unbind"):
        ags = spec.get("agents")
        if not isinstance(ags, list) or not ags:
            return "%s 需要 agents（Agent 名字数组），例如 [\"design\"]" % action

    if action == "create":
        nm = str(spec.get("name") or "").strip()
        if not nm:
            return "create 需要 name"
        if not _NAME_RE.match(nm):
            return ("name 不合法：%r。允许中英文/数字/_/-/.，不超过 64 字符"
                    "（它会被当作运行时标识，非法名字会被静默剔除）" % nm)
        if kind in ("agent", "tool") and not str(spec.get("description") or "").strip():
            return "%s 的 description 必填（模型靠它判断何时调用）" % kind
        if kind == "capability":
            script = str(spec.get("script") or "")
            if not script.strip():
                return "capability 需要 script（Python 源码，需定义 run(**kwargs)）"
            from core.code_guard import audit_code, summarize
            _a = audit_code(script)
            if not _a.get("ok"):
                _bl = "；".join("第%s行 %s" % (b.get("line"), b.get("detail"))
                                for b in (_a.get("blocks") or [])[:5])
                return "脚本未通过静态审查：%s（%s）" % (summarize(_a), _bl)
            _w = _a.get("warns") or []
            if _w:
                spec["_audit_warns"] = _w[:20]
            # 名字不能与既有工具重名（tools.name UNIQUE）
            spec.setdefault("name", nm)
    return ""


# ══════════════════════════════════════════════════════════════════════
# 执行（批准后由 hil_service.execute_confirmation 调用）
# ══════════════════════════════════════════════════════════════════════

def execute(payload: dict, user=None) -> dict:
    """执行一笔元能力写动作。返回 `{"ok", "result"}`。

    ⚠️ **执行前必须重新校验**（不只是信任入队时的校验）：
       确认单可能挂几小时，期间对象可能被删、被改名、被别的流程占用。
       "入队时通过、执行时不复查"就是"批准了却做不成，且不知道为什么"。
    """
    p = payload or {}
    kind = str(p.get("kind") or "").strip().lower()
    action = str(p.get("action") or "").strip().lower()
    spec = dict(p.get("spec") or {})
    if kind not in KINDS:
        return {"ok": False, "result": "未知 kind：%r" % kind}
    if action not in WRITE_ACTIONS:
        return {"ok": False, "result": "未知 action：%r" % action}
    fn = {
        ("capability", "create"): _create_capability,
        ("tool", "create"): _create_tool,
        ("skill", "create"): _create_skill,
        ("agent", "create"): _create_agent,
    }.get((kind, action))
    if fn:
        # ★ 2026-11-07：create 默认绑给**发起者**（spec.agents 显式给了就按给的）。
        #   语义依据：用户说"给我加个能算 XX 的工具"——"给我"就是绑给当前 Agent；
        #   不绑的话注册完模型看不见它（真机实测 990512：注册成功但下一轮没调用）。
        if not (spec.get("agents") or []) and p.get("proposer_agent"):
            spec["agents"] = [str(p.get("proposer_agent"))]
        return fn(spec, user)
    if action == "update":
        return _update(kind, spec)
    if action in ("bind", "unbind"):
        return _bind(kind, spec, unbind=(action == "unbind"))
    if action in ("enable", "disable"):
        return _toggle(kind, spec, enable=(action == "enable"))
    if action == "delete":
        return _delete(kind, spec)
    return {"ok": False, "result": "未实现：%s/%s" % (kind, action)}


# ── 定位辅助 ──────────────────────────────────────────────────────

def _resolve_id(conn, kind: str, target: str):
    """把 target（id 或名字）解析成 (id, 正式名, 行)。找不到返回 (None, "", None)。"""
    t = str(target or "").strip()
    if not t:
        return None, "", None
    table = {"agent": "agents", "skill": "skills",
             "tool": "tools", "capability": "tools"}[kind]
    if t.isdigit():
        r = conn.execute("SELECT * FROM %s WHERE id=?" % table, (int(t),)).fetchone()
        if r:
            return r["id"], r["name"], r
    if kind == "agent":
        r = conn.execute("SELECT * FROM agents WHERE name=? LIMIT 1", (t,)).fetchone()
        if not r:
            r = conn.execute("SELECT * FROM agents WHERE display_name=? LIMIT 1", (t,)).fetchone()
    else:
        r = conn.execute("SELECT * FROM %s WHERE name=? ORDER BY "
                         "CASE status WHEN 'published' THEN 0 ELSE 1 END, id LIMIT 1"
                         % table, (t,)).fetchone()
    if not r:
        return None, "", None
    return r["id"], r["name"], r


def _agent_ids(conn, names) -> tuple:
    """Agent 名字数组 → [(id, name)]，以及找不到的名字。"""
    found, missing = [], []
    for n in (names or []):
        r = conn.execute("SELECT id, name FROM agents WHERE name=? OR display_name=? LIMIT 1",
                         (str(n), str(n))).fetchone()
        if r:
            found.append((r["id"], r["name"]))
        else:
            missing.append(str(n))
    return found, missing


# ── create ────────────────────────────────────────────────────────

def _create_capability(spec: dict, user=None) -> dict:
    """注册一个模型自写的代码能力：审查 → 落盘沙箱 → 派生档位 → 写 tools 行 → 绑定。"""
    from core import code_guard, workspace
    from core.workspace import resolve_in_root

    name = str(spec.get("name") or "").strip()
    desc = str(spec.get("description") or "").strip()
    script = str(spec.get("script") or "")
    schema = spec.get("input_schema") or {"type": "object", "properties": {}}
    if not name or not script:
        return {"ok": False, "result": "name / script 必填"}
    if not desc:
        return {"ok": False, "result": "description 必填（模型靠它判断何时调用）"}

    # ① 复查（批准期间脚本可能被改过？不可能——payload 里的就是原文，
    #    但审查结论可能因规则版本变化而不同，重审成本很低，务必重审）
    audit = code_guard.audit_code(script)
    if not audit.get("ok"):
        return {"ok": False,
                "result": "脚本未通过静态审查（执行前复查）：%s" % code_guard.summarize(audit)}

    conn = _conn()
    try:
        # ② 重名检查（tools.name UNIQUE）
        if conn.execute("SELECT 1 FROM tools WHERE name=?", (name,)).fetchone():
            return {"ok": False, "result": "工具名已存在：%s（不能重复注册）" % name}

        # ③ 落盘到工作沙箱
        workspace.ensure_root("capabilities")
        try:
            d = resolve_in_root(os.path.join("capabilities", name), must_exist=False)
        except PermissionError as e:
            return {"ok": False, "result": str(e)}
        os.makedirs(d, exist_ok=True)
        main_py = os.path.join(d, "main.py")
        with open(main_py, "w", encoding="utf-8") as f:
            f.write(script)
        with open(os.path.join(d, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump({"name": name, "description": desc, "input_schema": schema,
                       "audit_warns": audit.get("warns") or [],
                       # ⚠️ 身份 dict 的键是 display_name / username，**没有 "name"**
                       #   ⇒ `.get("name")` 恒取默认值，审计里的"谁注册的"永远是假的。
                       #   统一走 audit_user（与 audit_logs.user_name 同口径）。
                       "registered_by": str(_registered_by(user))},
                      f, ensure_ascii=False, indent=1)

        # ④ 副作用派生（不采信模型自报）
        se, risk, rule = _derive_capability_effect(name, script, schema)

        # ⑤ 写 tools 行
        conn.execute(
            "INSERT INTO tools (name, source, description, status, input_schema, version, "
            "side_effect, risk_level, owner, kind, config, builtin, scope, "
            "side_effect_source, rule_version, origin) "
            "VALUES (?, 'capability', ?, 'active', ?, 'v0.1-ai', ?, ?, 'AI自注册', "
            "        'capability', ?, 0, 'private', 'auto', 1, 'ai_proposed')",
            (name, desc, json.dumps(schema, ensure_ascii=False), se, risk,
             json.dumps({"script_path": os.path.relpath(main_py, workspace.root()),
                         "workspace": workspace.root()}, ensure_ascii=False)))
        # ⑥ 绑定
        bound = []
        ags, missing = _agent_ids(conn, spec.get("agents") or [])
        for aid, aname in ags:
            conn.execute("INSERT OR IGNORE INTO agent_tools (agent_id, tool_type, tool_name, "
                         "enabled) VALUES (?, 'tool', ?, 1)", (aid, name))
            bound.append(aname)
        conn.commit()
    finally:
        conn.close()

    # ⑦ 让执行器认识它（注册进 code_tools 风格的动态执行表）
    try:
        from services.capability_runtime import invalidate_cache
        invalidate_cache()
    except Exception:
        pass
    return {"ok": True,
            "result": ("✅ 已注册能力「%s」（副作用 %s，判据 %s）\n脚本：%s\n绑定：%s\n"
                       "下一轮会话即可调用。"
                       % (name, se, rule, workspace.rel_of(main_py),
                          "、".join(bound) if bound else "（未绑定，仅注册）"))
            + ("\n⚠️ 它是**写类**能力：首次被调用时会进人工确认队列（与 MCP 写工具同一道闸）。"
               if se != "read" else
               "\n它是只读能力，调用不需要人工确认。")}



def _registered_by(user) -> str:
    """注册人：与本仓审计口径一致（display_name → username → 'ai'）。"""
    try:
        from core.audit import audit_user
        return audit_user(user) or "ai"
    except Exception:
        return str((user or {}).get("display_name") or (user or {}).get("username") or "ai")


def _derive_capability_effect(name: str, script: str, schema: dict) -> tuple:
    """派生副作用档位：代码特征 > 名字 > schema 契约 > 默认 read。

    为什么不能仅靠名字：`cap_fetch_weather` 听着是读，但脚本里可能 `open(...,'w')`。
    为什么不能仅靠代码：读文件的脚本也可能只读。⇒ 两者取**更严**。
    """
    se, risk, rule = "read", "low", "default_read"
    # ① schema 契约（与 MCP 同一判据：有 approvalId 参数 ⇒ 平台要求审批 ⇒ 写）
    try:
        from agent.tool_side_effect import derive as _d
        _d_se, _d_risk, _d_rule = _d({"name": name, "description": "",
                                      "inputSchema": schema or {}})
        if _d_se != "read":
            se, risk, rule = _d_se, _d_risk, "schema:" + _d_rule
    except Exception:
        pass
    # ② 代码特征（更硬的判据，取更严）
    _rank = {"read": 0, "write": 1, "destructive": 2}
    _code_se = "read"
    low = script.lower()
    if any(k in low for k in ("shutil.rmtree", "os.remove(", "os.unlink(", "os.rename(")):
        _code_se = "destructive"
    elif any(k in low for k in ("open(", ".write(", "requests.post", "requests.put",
                                "requests.delete", "requests.patch", "urllib.request.urlopen")):
        _code_se = "write"
    if _rank[_code_se] > _rank[se]:
        se = _code_se
        rule = rule + "+code_write" if _code_se == "write" else rule + "+code_destructive"
    risk = {"read": "low", "write": "medium", "destructive": "high"}[se]
    return se, risk, rule


def _create_tool(spec: dict, user=None) -> dict:
    """注册 HTTP 工具（与 Studio 表单同一路径：必须有 kind='http'，否则永远调不动）。"""
    from routers.studio_parts.tools import _derive_http_side_effect
    from repositories.studio_repo import StudioRepo

    name = str(spec.get("name") or "").strip()
    desc = str(spec.get("description") or "").strip()
    url = str(spec.get("url") or "").strip()
    method = str(spec.get("method") or "GET").strip().upper()
    if not (name and desc and url):
        return {"ok": False, "result": "name / description / url 必填"}
    conn = _conn()
    try:
        if conn.execute("SELECT 1 FROM tools WHERE name=?", (name,)).fetchone():
            return {"ok": False, "result": "工具名已存在：%s" % name}
        se, rule = _derive_http_side_effect(name, method, spec.get("side_effect") or "")
        cfg = {"method": method, "url": url, "timeout": int(spec.get("timeout") or 15)}
        for k in ("headers", "body", "query", "param_in", "defaults"):
            if spec.get(k):
                cfg[k] = spec[k]
        if spec.get("data_path"):
            cfg["data_path"] = spec["data_path"]
        repo = StudioRepo(conn)
        tid = repo.create_tool(name, desc, "http",
                               json.dumps(spec.get("input_schema")
                                          or {"type": "object", "properties": {}},
                                          ensure_ascii=False),
                               "v1.0", se, "medium" if se != "read" else "low",
                               "AI自注册", config=json.dumps(cfg, ensure_ascii=False),
                               kind="http")
        bound = []
        ags, _miss = _agent_ids(conn, spec.get("agents") or [])
        for aid, aname in ags:
            conn.execute("INSERT OR IGNORE INTO agent_tools (agent_id, tool_type, tool_name, "
                         "enabled) VALUES (?, 'tool', ?, 1)", (aid, name))
            bound.append(aname)
        conn.commit()
    finally:
        conn.close()
    return {"ok": True,
            "result": "✅ 已注册 HTTP 工具「%s」（%s %s，副作用 %s，判据 %s）；绑定：%s"
                      % (name, method, url, se, rule,
                         "、".join(bound) if bound else "（未绑定）")}


def _create_skill(spec: dict, user=None) -> dict:
    """新建技能（正文直接落库；不落盘技能包——包是给 scripts 用的）。"""
    name = str(spec.get("name") or "").strip()
    desc = str(spec.get("description") or "").strip()
    content = str(spec.get("content") or "")
    if not (name and content.strip()):
        return {"ok": False, "result": "name / content 必填"}
    trig = spec.get("triggers") or []
    conn = _conn()
    try:
        if conn.execute("SELECT 1 FROM skills WHERE name=? AND status='published'",
                        (name,)).fetchone():
            return {"ok": False, "result": "已存在同名已发布技能：%s" % name}
        conn.execute(
            "INSERT INTO skills (name, description, skill_type, triggers, category, content, "
            "frontmatter, status, version, source, allowed_tools, `references`, scripts, "
            "builtin, scope, enabled) "
            "VALUES (?,?,'package',?,?,?,?,'published','v1.0-ai','ai_propose','[]','[]','[]',"
            "        0,'private',1)",
            (name, desc, json.dumps(trig, ensure_ascii=False),
             str(spec.get("category") or "AI生成"), content, content))
        sid = conn.execute("SELECT id FROM skills WHERE name=? ORDER BY id DESC LIMIT 1",
                           (name,)).fetchone()[0]
        bound = []
        ags, _miss = _agent_ids(conn, spec.get("agents") or [])
        for aid, aname in ags:
            conn.execute("INSERT OR IGNORE INTO agent_tools (agent_id, tool_type, tool_name, "
                         "enabled) VALUES (?, 'skill', ?, 1)", (aid, name))
            bound.append(aname)
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "result": "✅ 已新建技能「%s」(id=%s)，%d 字符；绑定：%s"
                                  % (name, sid, len(content),
                                     "、".join(bound) if bound else "（未绑定，"
                                     "仍可从全局池按 triggers 命中）")}


def _create_agent(spec: dict, user=None) -> dict:
    name = str(spec.get("name") or "").strip()
    disp = str(spec.get("display_name") or name).strip()
    desc = str(spec.get("description") or "").strip()
    prompt = str(spec.get("system_prompt") or "")
    role = str(spec.get("agent_role") or "sub").strip()
    if role not in ("main", "sub"):
        return {"ok": False, "result": "agent_role 只支持 main / sub"}
    if not (name and desc):
        return {"ok": False, "result": "name / description 必填"}
    conn = _conn()
    try:
        if conn.execute("SELECT 1 FROM agents WHERE name=?", (name,)).fetchone():
            return {"ok": False, "result": "Agent 标识名已存在：%s" % name}
        conn.execute(
            "INSERT INTO agents (name, display_name, description, system_prompt, status, "
            "hil_level, kb_required, builtin, agent_role, task_domain, intent_keywords, "
            "created_at, updated_at) "
            "VALUES (?,?,?,?, 'active','L1',0,0,?,?,?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (name, disp, desc, prompt or desc, role,
             str(spec.get("task_domain") or "general"),
             json.dumps(spec.get("intent_keywords") or [], ensure_ascii=False)))
        aid = conn.execute("SELECT id FROM agents WHERE name=?", (name,)).fetchone()[0]
        bound = []
        # 绑工具 / 技能
        for tt, key in (("tool", "tools"), ("skill", "skills")):
            for tn in (spec.get(key) or []):
                conn.execute("INSERT OR IGNORE INTO agent_tools (agent_id, tool_type, "
                             "tool_name, enabled) VALUES (?, ?, ?, 1)", (aid, tt, str(tn)))
                bound.append("%s:%s" % (tt, tn))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True,
            "result": "✅ 已新建 Agent「%s」（标识名 %s，角色 %s）；绑定 %d 项%s"
                      % (disp, name, role, len(bound),
                         ("：" + "、".join(bound[:10])) if bound else "")}


# ── update / bind / toggle / delete ───────────────────────────────

_UPDATABLE = {
    "agent": {"display_name", "description", "system_prompt", "hil_level", "status",
              "intent_keywords", "task_domain", "agent_role"},
    "skill": {"description", "content", "version", "triggers", "category",
              "allowed_tools", "status", "enabled"},
    "tool": {"description", "input_schema", "side_effect", "status", "config"},
    "capability": {"description", "input_schema", "side_effect", "status"},
}


def _update(kind: str, spec: dict) -> dict:
    conn = _conn()
    try:
        tid, name, row = _resolve_id(conn, kind, spec.get("target"))
        if not tid:
            return {"ok": False, "result": "找不到 %s：%s" % (kind, spec.get("target"))}
        fields = dict(spec.get("fields") or {})
        # 白名单：不让模型改 builtin / source / mcp_server_id 这类来源属性
        allowed = _UPDATABLE.get(kind) or set()
        bad = [k for k in fields if k not in allowed]
        if bad:
            return {"ok": False,
                    "result": "不允许修改的字段：%s（可改：%s）"
                              % ("、".join(bad), "、".join(sorted(allowed)))}
        if not fields:
            return {"ok": False, "result": "fields 为空，没有要改的内容"}
        # 特殊校验
        if "side_effect" in fields and fields["side_effect"] not in ("read", "write", "destructive"):
            return {"ok": False, "result": "side_effect 只支持 read/write/destructive"}
        if kind in ("tool", "capability") and "side_effect" in fields:
            # 人工改档 ⇒ 标 manual，此后派生不再覆盖（与 MCP 同一语义）
            fields["side_effect_source"] = "manual"
        if "content" in fields and not str(fields["content"]).strip():
            return {"ok": False, "result": "技能正文不能改成空"}
        if "triggers" in fields and isinstance(fields["triggers"], list):
            fields["triggers"] = json.dumps(fields["triggers"], ensure_ascii=False)
        if "allowed_tools" in fields and isinstance(fields["allowed_tools"], list):
            fields["allowed_tools"] = json.dumps(fields["allowed_tools"], ensure_ascii=False)
        if "intent_keywords" in fields and isinstance(fields["intent_keywords"], list):
            fields["intent_keywords"] = json.dumps(fields["intent_keywords"], ensure_ascii=False)
        if "input_schema" in fields and isinstance(fields["input_schema"], dict):
            fields["input_schema"] = json.dumps(fields["input_schema"], ensure_ascii=False)
        if "config" in fields and isinstance(fields["config"], dict):
            fields["config"] = json.dumps(fields["config"], ensure_ascii=False)

        table = {"agent": "agents", "skill": "skills"}.get(kind, "tools")
        cols = [c for c in fields if _has_col(conn, table, c)]
        if not cols:
            return {"ok": False, "result": "这些字段在该表里都不存在：%s" % "、".join(fields)}
        sets = ", ".join("%s=?" % _q(c) for c in cols)
        conn.execute("UPDATE %s SET %s WHERE id=?" % (table, sets),
                     tuple(fields[c] for c in cols) + (tid,))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True,
            "result": "✅ 已修改 %s「%s」：%s" % (kind, name, "、".join(sorted(fields.keys())))}


def _has_col(conn, table, col) -> bool:
    try:
        return col in {r[1] for r in conn.execute("PRAGMA table_info(%s)" % table).fetchall()}
    except Exception:
        return False


def _q(c: str) -> str:
    return '"%s"' % str(c).replace('"', '""')


def _bind(kind: str, spec: dict, unbind: bool = False) -> dict:
    """绑定 / 解绑。kind=agent 时是**反向**：把 target(agent) 绑上 spec.tools/skills。"""
    conn = _conn()
    try:
        tid, name, row = _resolve_id(conn, kind, spec.get("target"))
        if not tid and kind != "agent":
            return {"ok": False, "result": "找不到 %s：%s" % (kind, spec.get("target"))}

        # ── 反向：给某个 Agent 挂能力 ──
        if kind == "agent":
            if not tid:
                return {"ok": False, "result": "找不到 Agent：%s" % spec.get("target")}
            done = []
            for tt, key in (("tool", "tools"), ("skill", "skills")):
                for tn in (spec.get(key) or []):
                    tn = str(tn)
                    if unbind:
                        conn.execute("DELETE FROM agent_tools WHERE agent_id=? AND "
                                     "tool_type=? AND tool_name=?", (tid, tt, tn))
                    else:
                        # 存在性检查：绑一个不存在的名字 = 静默无效
                        _ok = conn.execute(
                            "SELECT 1 FROM %s WHERE name=?" % ("tools" if tt == "tool" else "skills"),
                            (tn,)).fetchone()
                        if not _ok:
                            return {"ok": False,
                                    "result": "要绑的 %s 不存在：%s（先 create 或换名字）" % (tt, tn)}
                        conn.execute("INSERT OR IGNORE INTO agent_tools (agent_id, tool_type, "
                                     "tool_name, enabled) VALUES (?, ?, ?, 1)", (tid, tt, tn))
                    done.append("%s:%s" % (tt, tn))
            conn.commit()
            return {"ok": True, "result": "✅ Agent「%s」%s %d 项：%s"
                                          % (name, "解绑" if unbind else "绑定",
                                             len(done), "、".join(done[:12]))}

        # ── 正向：把能力挂到多个 Agent ──
        tt = "skill" if kind == "skill" else "tool"
        ags, missing = _agent_ids(conn, spec.get("agents") or [])
        if not ags:
            return {"ok": False, "result": "没有可绑定的 Agent（找不到：%s）"
                                           % "、".join(missing or ["（空）"])}
        for aid, aname in ags:
            if unbind:
                conn.execute("DELETE FROM agent_tools WHERE agent_id=? AND tool_type=? "
                             "AND tool_name=?", (aid, tt, name))
            else:
                conn.execute("INSERT OR IGNORE INTO agent_tools (agent_id, tool_type, "
                             "tool_name, enabled) VALUES (?, ?, ?, 1)", (aid, tt, name))
        conn.commit()
        _msg = "✅ 已%s %s「%s」与 %d 个 Agent：%s" % (
            "解绑" if unbind else "绑定", kind, name, len(ags),
            "、".join(a for _, a in ags[:10]))
        if missing:
            _msg += "（跳过不存在的 Agent：%s）" % "、".join(missing[:5])
        return {"ok": True, "result": _msg}
    finally:
        conn.close()


def _toggle(kind: str, spec: dict, enable: bool) -> dict:
    conn = _conn()
    try:
        tid, name, row = _resolve_id(conn, kind, spec.get("target"))
        if not tid:
            return {"ok": False, "result": "找不到 %s：%s" % (kind, spec.get("target"))}
        if kind == "agent":
            conn.execute("UPDATE agents SET status=? WHERE id=?",
                         ("active" if enable else "disabled", tid))
        elif kind == "skill":
            conn.execute("UPDATE skills SET enabled=?, status=? WHERE id=?",
                         (1 if enable else 0, "published" if enable else "disabled", tid))
        else:
            conn.execute("UPDATE tools SET status=? WHERE id=?",
                         ("active" if enable else "disabled", tid))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "result": "✅ 已%s %s「%s」"
                                  % ("启用" if enable else "停用", kind, name)}


def _delete(kind: str, spec: dict) -> dict:
    conn = _conn()
    try:
        tid, name, row = _resolve_id(conn, kind, spec.get("target"))
        if not tid:
            return {"ok": False, "result": "找不到 %s：%s" % (kind, spec.get("target"))}
        if row and (row["builtin"] if "builtin" in row.keys() else 0):
            return {"ok": False,
                    "result": "「%s」是内置 %s，不能删除（可以 disable 停用）" % (name, kind)}
        # 影响面：删除前算清谁在用（与 Studio 的 refs 一致口径）
        if kind == "agent":
            n = conn.execute("SELECT COUNT(*) FROM agent_team_members WHERE main_agent_id=? "
                             "OR sub_agent_id=?", (tid, tid)).fetchone()[0]
            conn.execute("DELETE FROM agent_tools WHERE agent_id=?", (tid,))
            conn.execute("DELETE FROM agents WHERE id=?", (tid,))
        elif kind == "skill":
            n = conn.execute("SELECT COUNT(*) FROM agent_tools WHERE tool_type='skill' "
                             "AND tool_name=?", (name,)).fetchone()[0]
            conn.execute("DELETE FROM agent_tools WHERE tool_type='skill' AND tool_name=?",
                         (name,))
            conn.execute("DELETE FROM skills WHERE id=?", (tid,))
        else:
            n = conn.execute("SELECT COUNT(*) FROM agent_tools WHERE tool_type='tool' "
                             "AND tool_name=?", (name,)).fetchone()[0]
            conn.execute("DELETE FROM agent_tools WHERE tool_type='tool' AND tool_name=?",
                         (name,))
            conn.execute("DELETE FROM tools WHERE id=?", (tid,))
            if kind == "capability":
                try:
                    from core import workspace
                    d = os.path.join(workspace.root(), "capabilities", name)
                    if workspace.is_in_root(d) and os.path.isdir(d):
                        shutil.rmtree(d, ignore_errors=True)
                except Exception:
                    pass
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "result": "✅ 已删除 %s「%s」（同时清理 %d 条绑定）" % (kind, name, n)}
