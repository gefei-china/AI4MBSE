# -*- coding: utf-8 -*-
"""P0-a（2026-10-24）能力统一注册处：capability_registry 逻辑投影层。

背景（《产品全面评估与优化方案-2026-10-23》4.1.1）：tools/skills/plugins/mcp_servers/prompts
五类能力各建表各度量，调用度量断链（plugin_call_logs 0 行、use_stats 空白、零调度不可查）。
本模块提供**逻辑统一层**——不动任何源表结构，权威数据仍在源表，本层只投影：

  capability_id   tool:{name} | skill:{id} | plugin:{plugin_id} | mcp:{name} | prompt:{id}
                  （mcp:{name}:{tool} 工具级已预留，但实测 mcp_servers.capabilities 只有
                   {"tools":{"listChanged":false}}、**无工具名清单** ⇒ 不展开、不造假，
                   待探测数据带清单后由 reconcile 自动补投影）

三条刷新路径（全部幂等）：
  1. 启动全量：reconcile_capability_registry(conn)——删幽灵 + 五类全量投影
  2. 运行时增量：refresh_registry_row(conn, table, row_id)——studio_repo._sync 挂点
  3. 插件侧：plugin_system/store/crud.py 与 legacy_sync 后调同款 refresh

度量（call_count/fail_count/last_used_at）由 aggregate_capability_metrics 从
tool_call_logs 只读聚合而来（搭车 alert_evaluator 300s 周期），**不双写**——
skills.use_stats 是读-改-写覆盖更新（无行级锁），聚合绕开它防丢更新。

设计纪律（全部承自产品既有，不新创）：
  - 不造假数据：指标缺数据不补零、mcp 无清单不展开、历史行匹配不上留空
  - 只提议不自动执行：长尾清单只进周报，不停用任何能力
  - 权威不动：registry 是投影，源表删行 registry 才删（reconcile 保证）
"""
import json
import re

# ── capability_id 构造（唯一口径，审计脚本与门禁共用）──────────────────────

def tool_cid(name) -> str:
    return "tool:" + str(name)


def skill_cid(sid) -> str:
    return "skill:" + str(sid)


def plugin_cid(pid) -> str:
    return "plugin:" + str(pid)


def mcp_server_cid(name) -> str:
    return "mcp:" + str(name)


def mcp_tool_cid(server, tool) -> str:
    return "mcp:%s:%s" % (server, tool)


def prompt_cid(pid) -> str:
    return "prompt:" + str(pid)


# ── 值域映射（按 2026-10-24 真库实测取值设计，不按想象）──────────────────

def scope_to_visibility(scope) -> str:
    """源表 scope → private|team|public。实测取值：public/platform/global/personal/private/team。"""
    s = (scope or "").strip().lower()
    if s in ("public", "platform", "global"):
        return "public"
    if s == "team":
        return "team"
    return "private"      # personal/private/缺省


def norm_version(v, default: str = "1.0.0") -> str:
    """version 归一：空 / '0.0.1'（plugins 未设置缺省）⇒ default。其余原样保留
    （tools 实测 'v1.0'/'v1.2'、skills 'v1'/'v1.0'——不强行改写存量格式）。"""
    v = (v or "").strip()
    return default if (not v or v == "0.0.1") else v


def tool_risk(side_effect, risk_level) -> str:
    """tools → read|write|destructive。实测 side_effect∈{read,write,destructive}、
    risk_level∈{low,medium,high}；destructive 判定看 side_effect 或 risk=high。"""
    se = (side_effect or "").strip().lower()
    rl = (risk_level or "").strip().lower()
    if se == "read":
        return "read"
    if se == "destructive" or rl == "high":
        return "destructive"
    return "write"


def skill_risk(skill_type) -> str:
    """skills → read|write。实测 skill_type∈{prompt,text2json,package}：
    prompt=提示词注入（read）；text2json/package 有执行/产出语义，保守按 write。"""
    return "write" if (skill_type or "").strip().lower() in ("text2json", "package") else "read"


def skill_status(status, enabled) -> str:
    """skills 双字段（status + enabled）→ registry 启停态。
    published+enabled=1 才 active；published+enabled=0 = 发布过但停用（status=disabled）。"""
    return "active" if (str(status) == "published" and int(enabled or 0) == 1) else "disabled"


def plugin_status(status) -> str:
    """plugins 8 态 → 启停态：published/private 可用，其余不可用。"""
    return "active" if str(status) in ("published", "private") else "disabled"


def plugin_lifecycle(status) -> str:
    """plugins 8 态 → draft|published|deprecated。
    draft/submitted/rejected=draft；published/private/disabled=published（disabled 是停用不是下架）；
    deprecated/removed=deprecated。"""
    s = str(status)
    if s in ("draft", "submitted", "rejected"):
        return "draft"
    if s in ("deprecated", "removed"):
        return "deprecated"
    return "published"


def frontmatter_author(fm) -> str:
    """skills.frontmatter 是 YAML 文本（不是 JSON）⇒ 不用 json_extract，简易正则取 author。
    实测样本：'author: 平台内置'。取不到留空——不编造发布者。"""
    if not fm:
        return ""
    m = re.search(r"(?m)^author:\s*(.+?)\s*$", str(fm))
    if not m:
        return ""
    return m.group(1).strip().strip('"').strip("'")


def extract_mcp_tool_names(capabilities, server_info):
    """从 mcp_servers.capabilities / server_info 解析工具名清单。
    实测 capabilities={"tools":{"listChanged":false}}——**无工具名** ⇒ 返回 []，
    调用方据此跳过 mcp_tool 投影（不造假占位行）。
    兼容结构：{"tools": {"name": {...}}} / {"tools": [{"name": ...}]}。"""
    names = []
    for doc in (capabilities, server_info):
        if not doc:
            continue
        try:
            data = json.loads(doc) if isinstance(doc, str) else doc
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        tools = data.get("tools")
        if isinstance(tools, dict):
            for k, v in tools.items():
                if k in ("listChanged",):
                    continue
                if isinstance(v, (dict, list)):
                    names.append(str(k))
        elif isinstance(tools, list):
            for t in tools:
                if isinstance(t, dict) and t.get("name"):
                    names.append(str(t["name"]))
                elif isinstance(t, str):
                    names.append(t)
    # 去重保序
    seen, out = set(), []
    for n in names:
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


# ── 投影 SQL 骨架 ─────────────────────────────────────────────────────────
# ON CONFLICT 只更新投影字段；度量字段（call_count/fail_count/last_used_at）与
# created_at **不覆盖**——度量归聚合器，created_at 保留首次投影时间（≈能力存在时间）。

_UPSERT_TAIL = """
ON CONFLICT(capability_id) DO UPDATE SET
    display_name=excluded.display_name, status=excluded.status,
    lifecycle=excluded.lifecycle, version=excluded.version,
    publisher=excluded.publisher, visibility=excluded.visibility,
    risk_level=excluded.risk_level, builtin=excluded.builtin,
    updated_at=CURRENT_TIMESTAMP
"""

_VIS_SQL = """CASE lower(COALESCE(scope,''))
    WHEN 'public' THEN 'public' WHEN 'platform' THEN 'public'
    WHEN 'global' THEN 'public' WHEN 'team' THEN 'team' ELSE 'private' END"""


def _ver_sql(col: str) -> str:
    return ("CASE WHEN TRIM(COALESCE(%s,'')) IN ('','0.0.1') THEN '1.0.0' ELSE %s END"
            % (col, col))


# 五源表全量投影（SELECT 头固定，reconcile 与 refresh 共用，refresh 追加 WHERE）
_PROJECTIONS = {
    "tools": (
        """SELECT 'tool:' || name, 'tool', name, 'tools', name,
                  CASE WHEN status='active' THEN 'active' ELSE 'disabled' END,
                  'published', %s, COALESCE(owner,''), %s,
                  CASE WHEN COALESCE(side_effect,'read')='read' THEN 'read'
                       WHEN COALESCE(side_effect,'')='destructive' THEN 'destructive'
                       WHEN lower(COALESCE(risk_level,''))='high' THEN 'destructive'
                       ELSE 'write' END,
                  COALESCE(builtin,0), COALESCE(NULLIF(TRIM(created_at),''), CURRENT_TIMESTAMP), CURRENT_TIMESTAMP
           FROM tools WHERE true""" % (_ver_sql("version"), _VIS_SQL)),
    "skills": (
        """SELECT 'skill:' || id, 'skill', name, 'skills', CAST(id AS TEXT),
                  CASE WHEN status='published' AND COALESCE(enabled,1)=1 THEN 'active'
                       ELSE 'disabled' END,
                  CASE status WHEN 'published' THEN 'published'
                              WHEN 'disabled' THEN 'deprecated' ELSE 'draft' END,
                  %s, '', %s,
                  CASE WHEN lower(COALESCE(skill_type,'')) IN ('text2json','package')
                       THEN 'write' ELSE 'read' END,
                  COALESCE(builtin,0), COALESCE(NULLIF(TRIM(created_at),''), CURRENT_TIMESTAMP), CURRENT_TIMESTAMP
           FROM skills WHERE true""" % (_ver_sql("version"), _VIS_SQL)),
    "plugins": (
        """SELECT 'plugin:' || plugin_id, 'plugin', name, 'plugins', plugin_id,
                  CASE status WHEN 'published' THEN 'active' WHEN 'private' THEN 'active'
                              ELSE 'disabled' END,
                  CASE status WHEN 'draft' THEN 'draft' WHEN 'submitted' THEN 'draft'
                              WHEN 'rejected' THEN 'draft'
                              WHEN 'deprecated' THEN 'deprecated' WHEN 'removed' THEN 'deprecated'
                              ELSE 'published' END,
                  %s, COALESCE(author_name,''), %s,
                  'write', 0, COALESCE(NULLIF(TRIM(created_at),''), CURRENT_TIMESTAMP), CURRENT_TIMESTAMP
           FROM plugins WHERE true""" % (_ver_sql("current_version"), _VIS_SQL)),
    # 注：plugins 表实测**无 builtin 列**（tools/skills/mcp_servers 才有，见
    # migrations/plugins.py _migrate_builtin_flags 只覆盖三表）⇒ 恒投 0，不猜。
    "mcp_servers": (
        """SELECT 'mcp:' || name, 'mcp_server', name, 'mcp_servers', name,
                  CASE WHEN COALESCE(enabled,1)=1 AND status='online' THEN 'active'
                       ELSE 'disabled' END,
                  'published', '1.0.0', '', %s,
                  'read', COALESCE(builtin,0), COALESCE(NULLIF(TRIM(created_at),''), CURRENT_TIMESTAMP), CURRENT_TIMESTAMP
           FROM mcp_servers WHERE true""" % _VIS_SQL),
    "prompts": (
        """SELECT 'prompt:' || id, 'prompt', name, 'prompts', CAST(id AS TEXT),
                  CASE WHEN status='published' THEN 'active' ELSE 'disabled' END,
                  CASE status WHEN 'published' THEN 'published'
                              WHEN 'disabled' THEN 'deprecated' ELSE 'draft' END,
                  %s, COALESCE(created_by,''), 'private', 'read', 0, COALESCE(NULLIF(TRIM(created_at),''), CURRENT_TIMESTAMP), CURRENT_TIMESTAMP
           FROM prompts WHERE true""" % _ver_sql("version")),
}

# refresh 的源键 WHERE（studio_repo._sync 传的都是源表 id；plugins 走 plugin_id）
_REFRESH_WHERE = {
    "tools": "WHERE id=?",
    "skills": "WHERE id=?",
    "prompts": "WHERE id=?",
    "mcp_servers": "WHERE id=?",
    "plugins": "WHERE plugin_id=?",
}


def reconcile_capability_registry(conn) -> dict:
    """全量对齐（幂等，可反复跑）：删幽灵 + 五类投影 + Python 侧补充 + mcp_tool 重展。

    返回 {'tools': n, 'skills': n, ...} 各源表投影行数与 {'dropped': n} 幽灵删除数。
    任何单源表异常不阻断其余（与 seed_all 同款容错），但错误收集进返回值的 'errors'。
    """
    stats, errors = {}, []
    try:
        # ① 删幽灵：registry 有投影但源表已无此行
        dropped = 0
        for table, key_sql in (("tools", "SELECT name FROM tools"),
                               ("skills", "SELECT CAST(id AS TEXT) FROM skills"),
                               ("plugins", "SELECT plugin_id FROM plugins"),
                               ("mcp_servers", "SELECT name FROM mcp_servers"),
                               ("prompts", "SELECT CAST(id AS TEXT) FROM prompts")):
            try:
                cur = conn.execute(
                    "DELETE FROM capability_registry WHERE source_table=? AND source_id NOT IN (%s)"
                    % key_sql, (table,))
                dropped += cur.rowcount or 0
            except Exception as e:
                errors.append("drop:%s:%s" % (table, str(e)[:80]))
        stats["dropped"] = dropped

        # ② mcp_tool 旧投影全清后按最新探测数据重展（无清单则自然为空——不造假）
        try:
            conn.execute("DELETE FROM capability_registry WHERE kind='mcp_tool'")
        except Exception:
            pass

        # ③ 五源表全量投影
        for table, select_sql in _PROJECTIONS.items():
            try:
                cur = conn.execute(
                    "INSERT INTO capability_registry (capability_id, kind, display_name, "
                    "source_table, source_id, status, lifecycle, version, publisher, "
                    "visibility, risk_level, builtin, created_at, updated_at) " + select_sql + _UPSERT_TAIL)
                stats[table] = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            except Exception as e:
                stats[table] = -1
                errors.append("project:%s:%s" % (table, str(e)[:120]))

        # ④ Python 侧补充：skills publisher（frontmatter 是 YAML，json_extract 无效）
        #    与 mcp version/server_info（非法 JSON 时 json_extract 会抛错，改 Python 解析）
        try:
            for row in conn.execute("SELECT id, frontmatter FROM skills").fetchall():
                author = frontmatter_author(row[1])
                if author:
                    conn.execute(
                        "UPDATE capability_registry SET publisher=?, updated_at=CURRENT_TIMESTAMP "
                        "WHERE capability_id=?", (author, skill_cid(row[0])))
        except Exception as e:
            errors.append("skill_author:%s" % str(e)[:80])
        try:
            for row in conn.execute("SELECT name, server_info FROM mcp_servers").fetchall():
                ver = ""
                try:
                    si = json.loads(row[1] or "{}")
                    ver = str(si.get("version") or "") if isinstance(si, dict) else ""
                except Exception:
                    ver = ""
                conn.execute(
                    "UPDATE capability_registry SET version=?, updated_at=CURRENT_TIMESTAMP "
                    "WHERE capability_id=?", (norm_version(ver), mcp_server_cid(row[0])))
        except Exception as e:
            errors.append("mcp_version:%s" % str(e)[:80])

        # ⑤ mcp_tool 展开（仅当探测数据真带工具名清单——实测当前两行都不带，跳过）
        try:
            for row in conn.execute(
                    "SELECT name, capabilities, server_info FROM mcp_servers").fetchall():
                for tname in extract_mcp_tool_names(row[1], row[2]):
                    conn.execute(
                        "INSERT INTO capability_registry (capability_id, kind, display_name, "
                        "source_table, source_id, status, lifecycle, version, publisher, "
                        "visibility, risk_level, builtin, updated_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
                        (mcp_tool_cid(row[0], tname), "mcp_tool", tname, "mcp_servers",
                         row[0], "active", "published", "1.0.0", "", "private",
                         "read", 0))
        except Exception as e:
            errors.append("mcp_tools:%s" % str(e)[:80])

        if errors:
            stats["errors"] = errors
        return stats
    except Exception as e:
        return {"error": str(e)[:200], "stats": stats}


def refresh_registry_row(conn, table: str, row_id) -> dict:
    """单行刷新（运行时增量，studio_repo._sync 与 plugin crud 调用）。

    行已删（delete 后调用）⇒ 删 registry 投影；存在 ⇒ 单行 upsert。
    table ∈ {tools, skills, prompts, mcp_servers, plugins}；其余表忽略。
    返回 {'ok': True} 或 {'ok': False, 'error': ...}——**不静默**：调用方（_sync/crud）
    可忽略返回值，但测试与排障必须能看见失败。2026-10-24 实测教训：本函数最初的
    `except: pass` 把「双 WHERE 语法错误」吞掉，表现为"刷新了却没投影"的
    静默故障——静默兜底不是稳健，是把错误变成不可见。
    """
    if table not in _REFRESH_WHERE:
        return {"ok": True, "skipped": table}
    where = _REFRESH_WHERE[table]
    try:
        if table == "plugins":
            row = conn.execute(
                "SELECT plugin_id FROM plugins WHERE plugin_id=?", (row_id,)).fetchone()
        else:
            row = conn.execute("SELECT id FROM %s WHERE id=?" % table, (row_id,)).fetchone()
        if not row:
            # 源行已不存在 ⇒ 删投影（含其 mcp_tool 子行）
            if table == "mcp_servers":
                conn.execute("DELETE FROM capability_registry WHERE source_table='mcp_servers' "
                             "AND (source_id=? OR capability_id LIKE 'mcp:' || ? || ':%')",
                             (row_id, row_id))
            elif table == "plugins":
                conn.execute("DELETE FROM capability_registry WHERE source_table='plugins' "
                             "AND source_id=?", (row_id,))
            else:
                key_col = {"tools": "name"}.get(table)
                if key_col:
                    conn.execute("DELETE FROM capability_registry WHERE source_table=? "
                                 "AND source_id NOT IN (SELECT %s FROM %s)" % (key_col, table),
                                 (table,))
                else:
                    conn.execute("DELETE FROM capability_registry WHERE source_table=? "
                                 "AND source_id=?", (table, str(row_id)))
            return {"ok": True, "deleted": True}
        # 单行 upsert：全量投影 SQL 尾部的 `WHERE true` 是 SQLite upsert 解析分隔符
        # （INSERT...SELECT 后直接 ON CONFLICT 会被误判为 join ON，见 _PROJECTIONS 注释），
        # 此处**替换**为真实 WHERE，绝不能叠加成双 WHERE（2026-10-24 实测踩过）。
        select_sql = _PROJECTIONS[table].replace("WHERE true", "")
        conn.execute(
            "INSERT INTO capability_registry (capability_id, kind, display_name, "
            "source_table, source_id, status, lifecycle, version, publisher, "
            "visibility, risk_level, builtin, created_at, updated_at) "
            + select_sql + " " + where + " " + _UPSERT_TAIL,
            (row_id,))
        # mcp server 变动 ⇒ 重展其 tool 级（先删后展）
        if table == "mcp_servers":
            conn.execute("DELETE FROM capability_registry WHERE kind='mcp_tool' "
                         "AND source_id=?", (row_id,))
            srv = conn.execute("SELECT name, capabilities, server_info FROM mcp_servers "
                               "WHERE id=?", (row_id,)).fetchone()
            if srv:
                for tname in extract_mcp_tool_names(srv[1], srv[2]):
                    conn.execute(
                        "INSERT INTO capability_registry (capability_id, kind, display_name, "
                        "source_table, source_id, status, lifecycle, version, publisher, "
                        "visibility, risk_level, builtin, updated_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
                        (mcp_tool_cid(srv[0], tname), "mcp_tool", tname, "mcp_servers",
                         srv[0], "active", "published", "1.0.0", "", "private", "read", 0))
        # skills publisher 单行补充
        if table == "skills":
            fm = conn.execute("SELECT frontmatter FROM skills WHERE id=?", (row_id,)).fetchone()
            author = frontmatter_author(fm[0] if fm else "")
            if author:
                conn.execute("UPDATE capability_registry SET publisher=? WHERE capability_id=?",
                             (author, skill_cid(row_id)))
        # mcp version 单行补充
        if table == "mcp_servers":
            nm = conn.execute("SELECT name FROM mcp_servers WHERE id=?", (row_id,)).fetchone()
            si = conn.execute("SELECT server_info FROM mcp_servers WHERE id=?", (row_id,)).fetchone()
            ver = ""
            try:
                d = json.loads(si[0] or "{}") if si else {}
                ver = str(d.get("version") or "") if isinstance(d, dict) else ""
            except Exception:
                ver = ""
            if nm:
                conn.execute("UPDATE capability_registry SET version=? WHERE capability_id=?",
                             (norm_version(ver), mcp_server_cid(nm[0])))
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)[:160]}


def backfill_capability_ids(conn) -> dict:
    """历史 tool_call_logs 行回填 capability_id（一次性，幂等）。

    只回填**能确定匹配**的行：skill 行按 name 反查 skills.id（重名取 MIN(id)）；
    tool 行按 name 匹配 tools。mcp 历史行（tool_type='mcp'，日志未存 server 名）
    与匹配不上的行**留空**——不造假映射（新行走 M1 挂载时填准确 mcp:{server}:{tool}）。
    """
    res = {"skill": 0, "tool": 0}
    try:
        cur = conn.execute(
            "UPDATE tool_call_logs SET capability_id = 'skill:' || ("
            "  SELECT MIN(id) FROM skills s WHERE s.name = tool_call_logs.tool_name) "
            "WHERE call_kind='skill' AND capability_id='' "
            "AND tool_name IN (SELECT name FROM skills)")
        res["skill"] = cur.rowcount or 0
    except Exception:
        pass
    try:
        cur = conn.execute(
            "UPDATE tool_call_logs SET capability_id = 'tool:' || tool_name "
            "WHERE call_kind<>'skill' AND capability_id='' "
            "AND tool_name IN (SELECT name FROM tools)")
        res["tool"] = cur.rowcount or 0
    except Exception:
        pass
    return res


def aggregate_capability_metrics(conn) -> dict:
    """度量聚合：从 tool_call_logs 只读聚合到 registry（搭车 alert_evaluator 周期）。

    call_count 对 mcp_server 用前缀匹配汇总其 tool 级调用（mcp:{name}:%）。
    last_used_at 用 COALESCE 保底——无调用的行不被清空。
    """
    try:
        cur = conn.execute(
            "UPDATE capability_registry SET "
            "  call_count = (SELECT COUNT(*) FROM tool_call_logs l "
            "                 WHERE l.capability_id = capability_registry.capability_id "
            "                    OR l.capability_id LIKE capability_registry.capability_id || ':%'), "
            "  fail_count = (SELECT COUNT(*) FROM tool_call_logs l "
            "                 WHERE (l.capability_id = capability_registry.capability_id "
            "                    OR l.capability_id LIKE capability_registry.capability_id || ':%') "
            "                 AND l.ok=0), "
            "  last_used_at = COALESCE((SELECT MAX(l.created_at) FROM tool_call_logs l "
            "                 WHERE l.capability_id = capability_registry.capability_id "
            "                    OR l.capability_id LIKE capability_registry.capability_id || ':%'), "
            "                last_used_at), "
            "  updated_at = CURRENT_TIMESTAMP "
            "WHERE kind IN ('tool','skill','mcp_tool','mcp_server')")
        return {"updated": cur.rowcount or 0}
    except Exception as e:
        return {"error": str(e)[:160]}


def longtail_capabilities(conn, days: int = 90) -> list:
    """长尾清单：active 但自首次投影起超过 days 天零调用的能力。
    只列清单（周报出口），**不含任何自动停用动作**——延续自进化纪律。"""
    try:
        return [dict(zip(("capability_id", "kind", "display_name", "version",
                          "created_at", "publisher"), r)) for r in conn.execute(
            "SELECT capability_id, kind, display_name, version, created_at, publisher "
            "FROM capability_registry "
            "WHERE status='active' AND call_count=0 "
            "AND julianday('now') - julianday(created_at) > ? "
            "ORDER BY kind, capability_id", (float(days),)).fetchall()]
    except Exception:
        return []


def zero_dispatch_agents(conn) -> list:
    """零调度 Agent 清单（口径同 core/lifecycle.py:135-143 usage_sql：
    agent_tasks.agent_id 存 Agent 名而非数字 id）。"""
    try:
        return [dict(zip(("id", "name", "status"), r)) for r in conn.execute(
            "SELECT a.id, a.name, a.status FROM agents a "
            "LEFT JOIN agent_tasks t ON t.agent_id = a.name "
            "GROUP BY a.id HAVING COUNT(t.id) = 0 ORDER BY a.id").fetchall()]
    except Exception:
        return []


def registry_summary(conn) -> dict:
    """注册处概览（监控/周报出口）：按 kind 计数 + 度量覆盖情况。"""
    out = {}
    try:
        out["by_kind"] = {r[0]: r[1] for r in conn.execute(
            "SELECT kind, COUNT(*) FROM capability_registry GROUP BY kind").fetchall()}
        out["total"] = sum(out["by_kind"].values())
        out["with_calls"] = conn.execute(
            "SELECT COUNT(*) FROM capability_registry WHERE call_count>0").fetchone()[0]
        out["empty_version"] = conn.execute(
            "SELECT COUNT(*) FROM capability_registry WHERE TRIM(COALESCE(version,''))=''").fetchone()[0]
    except Exception as e:
        out["error"] = str(e)[:160]
    return out
