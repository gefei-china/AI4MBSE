"""legacy ↔ plugins 双向同步桥（P1，2026-09-16）

背景（为什么需要它）
------------------
本系统存在两套事实源，这是既有架构约束：
  - **运行时事实源**：旧表 `skills / tools / mcp_servers / agents / prompts`
    —— pipeline、registry、agent_repo 直接读它们，AI 能否调用取决于这些表
  - **管理事实源**：`plugins` 表
    —— 能力中心（市场 / 我安装的 / 审核 / 范围分配）全部基于它

两者原先只在「迁移时」建立过一次联系，运行期互不相通，导致：
  ① 能力中心「停用 / 下架 / 删除」→ 只改 plugins → **AI 照样调用**（安全隐患）
  ② 旧表单「新建能力」→ 只写旧表 → **能力中心看不到**（管理盲区）

本模块实现双向桥，映射键沿用迁移时写入的 `manifest.runtime.legacy_table / legacy_id`。

方向与语义
----------
  sync_from_legacy(conn, table, row_id)      旧表 → plugins   （业务操作后同步管理视图）
  sync_to_legacy(conn, plugin_id, available) plugins → 旧表    （管理动作同步为运行时可用性）

⚠️ 状态字段语义差异（踩过坑，务必区分）
  - `skills`      ：可用 = status='published' **且** enabled=1
  - `tools`       ：可用 = status='active'
  - `mcp_servers` ：可用 = **enabled=1**；其 `status` 是 *连接态*（online/offline），
                    由健康巡检维护，**绝不能被管理动作覆写**
  - `agents`      ：可用 = status='active'
  - `prompts`     ：可用 = status='published'

设计约束
--------
  - **同步失败绝不阻断主流程**：调用方一律 try/except 包裹（与 store._sync_deps_safe 同思路）
  - 不删除旧表数据；plugins 侧删除只做软删（status='removed'），保留审计
"""
import json
import re
import sqlite3
from datetime import datetime

# 旧表 ↔ 能力类型
TABLE_KIND = {"skills": "skill", "tools": "tool", "mcp_servers": "mcp",
              "agents": "agent", "prompts": "prompt"}
KIND_TABLE = {v: k for k, v in TABLE_KIND.items()}

# capabilities 槽位
SLOT = {"skill": "skills", "tool": "tools", "mcp": "mcp",
        "agent": "agents", "prompt": "prompts"}

# 反向写回：表 → [(列, 可用值, 不可用值)]
#   不可用值为 None 表示**下架时不改该列**（保留原值，避免破坏发布记录）。
#
#   ⚠️ 各表「不可用」的取值以既有代码为准，不能臆造：
#      tools / agents 用 'disabled'（见 migrations.py:1273 把 deprecated/inactive 统一为 disabled；
#      agents.py 的 disable 路由亦写 'disabled'），此前误写成 'inactive' 已修正。
#   mcp_servers 只动 enabled，不动 status（status 是连接态，由健康巡检维护）；
#   skills 的可用性是 status='published' 且 enabled=1 双条件 ——
#     上架时必须一并置 published（能力中心已发布它），下架时只关 enabled。
AVAILABILITY_WRITE = {
    "skills":      [("enabled", 1, 0), ("status", "published", None)],
    "tools":       [("status", "active", "disabled")],
    "mcp_servers": [("enabled", 1, 0)],
    "agents":      [("status", "active", "disabled")],
    "prompts":     [("status", "published", "disabled")],
}

# ── 覆盖面自检（2026-09-16）──────────────────────────────────────────
# 把「新增能力类型必须同步补映射」从文档约定变成代码断言。
# 背景：管理面(plugins) 与运行面(旧表) 分离，本模块是唯一的桥。历史上新增 tool/agent/prompt
# 类型时曾漏补映射，退化成「在能力中心管了、运行时却没生效」。此自检使任何新类型在未补
# TABLE_KIND / SLOT / AVAILABILITY_WRITE 时立刻暴露，而不是等上线后才发现。
#   用法：测试里断言 legacy_sync.self_check()["ok"]；排查时直接打印它。
_BUNDLE_EXEMPT = "bundle"     # 组合类型：由 skills + mcp 槽位承载，无独立旧表


def uncovered_types() -> list:
    """PLUGIN_TYPES 中尚未被旧表映射覆盖的类型（bundle 除外）。"""
    try:
        from plugin_system.manifest import PLUGIN_TYPES
    except Exception:
        return []
    bad = []
    for t in PLUGIN_TYPES:
        if t == _BUNDLE_EXEMPT:
            continue
        tbl = KIND_TABLE.get(t)          # 注意：AVAILABILITY_WRITE 以「旧表名」为键，不是 kind
        if not tbl or t not in SLOT or tbl not in AVAILABILITY_WRITE:
            bad.append(t)
    return bad


def self_check(strict: bool = False) -> dict:
    """同步桥覆盖面自检。strict=True 时对未覆盖类型抛异常（供测试/CI）。"""
    bad = uncovered_types()
    if bad and strict:
        raise AssertionError(
            "legacy_sync 未覆盖能力类型：%s —— 新增类型时必须同步补 TABLE_KIND / SLOT / "
            "AVAILABILITY_WRITE，否则能力中心对该类型的管理动作不会传递到运行时"
            % ", ".join(bad))
    return {"ok": not bad, "uncovered": bad,
            "kinds": sorted(KIND_TABLE),                    # 已覆盖的能力类型
            "tables": {v: k for k, v in sorted(KIND_TABLE.items())}}   # kind -> 旧表名

LOCAL_PREFIX = "com.mbse.local."      # 本地新建能力的 id 命名空间（区别于 legacy./mbse.personal.）
AUTHOR_NAME = "本地创建"
_RE_PATH_SAFE = re.compile(r"^[a-zA-Z0-9_./-]+$")


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ────────────────────────── 工具 ──────────────────────────

def _slug(name, rid, kind):
    """旧表名 → plugins.name（须匹配 ^[a-z][a-z0-9-]{0,63}$）。

    中文名 ASCII 化后会退化成空串，此时用 `<kind>-<id>` 兜底（与迁移脚本一致）。
    """
    s = re.sub(r"[^a-z0-9-]+", "-", str(name or "").lower()).strip("-")
    s = re.sub(r"-{2,}", "-", s)
    if len(s) < 3 or s == kind or not re.match(r"^[a-z]", s):
        s = "%s-%s" % (kind, rid)
    return s[:60]


def _semver(v):
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", str(v or ""))
    if m:
        return "%s.%s.%s" % (m.group(1), m.group(2), m.group(3) or "0")
    return "1.0.0"


def _desc(kind, row):
    d = (row.get("description") or "").strip()
    if d:
        return d[:1024]
    nm = (row.get("name") or "").strip()
    if kind == "prompt":
        scen = (row.get("scenario") or "").strip()
        content = (row.get("content") or "").strip()
        return ("[%s] %s" % (scen or "通用", content[:120])).strip() or ("提示词：%s" % nm)
    if kind == "mcp":
        return "MCP 服务：%s（%s）" % (nm, row.get("endpoint") or "-")
    return "%s：%s" % ({"skill": "技能", "tool": "工具", "agent": "Agent"}.get(kind, "能力"), nm)


def _category(kind, row):
    c = (row.get("category") or "").strip()
    if c:
        return c
    if kind == "agent":
        return {"main": "编排 Agent", "sub": "专业 Agent"}.get(
            (row.get("agent_role") or "").strip(), "Agent")
    return {"skill": "自定义技能", "tool": "自定义工具", "mcp": "自定义 MCP",
            "prompt": "提示词模板"}.get(kind, "自定义")


def is_available(table, row):
    """旧表行在运行时是否可用（口径与各消费方 SQL 的 WHERE 严格一致）。"""
    if table == "skills":
        return str(row.get("status")) == "published" and int(row.get("enabled") or 0) == 1
    if table == "tools":
        return str(row.get("status")) == "active"
    if table == "mcp_servers":
        return int(row.get("enabled") or 0) == 1
    if table == "agents":
        return str(row.get("status")) == "active"
    if table == "prompts":
        return str(row.get("status")) == "published"
    return True


def find_plugin_by_legacy(conn, table, row_id):
    """按 runtime.legacy_table / legacy_id 反查 plugins.plugin_id。"""
    cur = conn.execute("SELECT plugin_id, manifest_json FROM plugins WHERE status!='removed'")
    for r in cur.fetchall():
        try:
            rt = (json.loads(r["manifest_json"] or "{}").get("runtime") or {})
        except Exception:
            continue
        if rt.get("legacy_table") == table and str(rt.get("legacy_id")) == str(row_id):
            return r["plugin_id"]
    return ""


def build_manifest(kind, row, table, row_id):
    """旧表行 → 合法 manifest（与迁移脚本同构，id/name 用本地命名空间）。"""
    name = (row.get("name") or "").strip()
    slug = _slug(name, row_id, kind)
    caps = {v: [] for v in SLOT.values()}
    if kind == "skill":
        caps["skills"] = [{"path": "SKILL.md"}]
    elif kind == "tool":
        caps["tools"] = [{"name": name}]        # tool 必须声明运行时工具名（P0 校验要求）
    elif kind == "mcp":
        caps["mcp"] = [{"server": "server.json"}]
    elif kind == "agent":
        caps["agents"] = [{"name": name}]
    elif kind == "prompt":
        caps["prompts"] = [{"name": name}]
    return {
        "id": LOCAL_PREFIX + kind + "." + slug,
        "name": slug,
        "label": {"zh_CN": name, "en_US": slug, "category": _category(kind, row)},
        "description": _desc(kind, row),
        "version": _semver(row.get("version")),
        "type": kind,
        "author": {"name": AUTHOR_NAME},
        "icon": "",
        "license": "Proprietary",
        "capabilities": caps,
        "dependencies": {},
        "permissions": {"tools": [], "models": {"enabled": kind == "agent", "llm": kind == "agent"},
                        "storage": {"enabled": False}, "network": {"domains": []}},
        "runtime": {"legacy_table": table, "legacy_id": row_id},
        "resource": {},
    }


def _unique_name(conn, base, row_id, kind):
    """确保 plugins.name 唯一（DB 与应用层都要求）。冲突时加 id 后缀。"""
    cand = base
    if not conn.execute("SELECT 1 FROM plugins WHERE name=? AND status!='removed'", (cand,)).fetchone():
        return cand
    cand = "%s-%s" % (base, row_id)
    if not conn.execute("SELECT 1 FROM plugins WHERE name=? AND status!='removed'", (cand,)).fetchone():
        return cand
    i = 2
    while conn.execute("SELECT 1 FROM plugins WHERE name=? AND status!='removed'",
                       ("%s-%s-%d" % (base, row_id, i),)).fetchone():
        i += 1
    return "%s-%s-%d" % (base, row_id, i)


# ────────────────────────── 方向 A：旧表 → plugins ──────────────────────────

def sync_from_legacy(conn, table, row_id):
    """旧表行 upsert 到 plugins（供旧表单 CRUD 后调用）。返回 (ok, 说明)。"""
    kind = TABLE_KIND.get(table)
    if not kind:
        return False, "不支持的表: %s" % table
    row = conn.execute("SELECT * FROM %s WHERE id=?" % table, (row_id,)).fetchone()
    pid = find_plugin_by_legacy(conn, table, row_id)

    # 旧表行已删除 → plugins 侧软删，保持两侧一致
    if row is None:
        if pid:
            conn.execute("UPDATE plugins SET status='removed', updated_at=? WHERE plugin_id=?",
                         (_now(), pid))
            return True, "旧表行不存在，已同步软删 plugins"
        return False, "旧表行不存在"
    d = dict(row)

    available = is_available(table, d)
    status = "published" if available else "disabled"
    scope = "public" if str(d.get("scope") or "").lower() == "public" else "personal"
    desc = _desc(kind, d)
    cat = _category(kind, d)

    if pid:
        # 更新：同步可读字段（name 不变，避免破坏引用）
        mf_row = conn.execute("SELECT manifest_json FROM plugins WHERE plugin_id=?", (pid,)).fetchone()
        mf = json.loads((mf_row["manifest_json"] if mf_row else "") or "{}")
        mf["description"] = desc
        mf.setdefault("label", {})
        mf["label"]["zh_CN"] = (d.get("name") or "").strip() or mf["label"].get("zh_CN", "")
        mf["label"]["category"] = cat
        mf["version"] = _semver(d.get("version"))
        if kind == "agent":
            role = (d.get("agent_role") or "").strip()
            if role in ("main", "sub"):
                mf["agent_role"] = role
        rt = mf.setdefault("runtime", {})
        rt["legacy_table"] = table
        rt["legacy_id"] = row_id
        conn.execute(
            "UPDATE plugins SET description=?, category=?, status=?, scope=?, current_version=?, "
            "manifest_json=?, updated_at=? WHERE plugin_id=?",
            (desc, cat, status, scope, mf["version"],
             json.dumps(mf, ensure_ascii=False), _now(), pid))
        return True, "已更新 %s" % pid

    # 新建：构造合法 manifest 并入库
    mf = build_manifest(kind, d, table, row_id)
    mf["name"] = _unique_name(conn, mf["name"], row_id, kind)
    mf["id"] = LOCAL_PREFIX + kind + "." + mf["name"]
    if kind == "agent":
        role = (d.get("agent_role") or "").strip()
        if role in ("main", "sub"):
            mf["agent_role"] = role
    # 复用契约层校验（非法则不同步，避免脏数据进管理视图）
    try:
        from plugin_system.manifest import validate_manifest
        okv, errs, normalized = validate_manifest(mf)
        if not okv:
            return False, "manifest 校验失败: " + "; ".join(errs)
    except Exception as e:
        return False, "校验异常: %s" % e

    conn.execute(
        """INSERT INTO plugins (plugin_id, name, namespace, type, scope, status, current_version,
           manifest_json, author_id, author_name, icon, category, description)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (normalized["id"], normalized["name"], "personal", normalized["type"], scope, status,
         normalized["version"], json.dumps(normalized, ensure_ascii=False),
         0, AUTHOR_NAME, normalized.get("icon", ""), cat, desc))
    conn.execute(
        "INSERT INTO plugin_versions (plugin_id, version, manifest_json, status) VALUES (?,?,?,?)",
        (normalized["id"], normalized["version"],
         json.dumps(normalized, ensure_ascii=False), "published" if available else "submitted"))
    # 写一条系统级安装记录（user_id=0）：与 55 条内置能力的处理一致，
    # 使 list_mine 的 `EXISTS(... user_id IN (?,0))` 命中 ——
    # 否则本地新建的能力 author_id=0 不属于任何人、scope=personal 又不进市场，
    # 会在能力中心「市场」和「我安装的」两个区都看不到（管理盲区）。
    conn.execute(
        "INSERT OR IGNORE INTO plugin_installs (user_id, plugin_id, version, enabled) VALUES (0,?,?,1)",
        (normalized["id"], normalized["version"]))
    return True, "已创建 %s" % normalized["id"]


# ────────────────────────── 方向 B：plugins → 旧表 ──────────────────────────

def sync_to_legacy(conn, plugin_id, available):
    """把 plugins 的可用性写回旧表（供能力中心启停/下架/删除后调用）。

    只动「可用性开关」，不碰旧表的业务字段（名称/正文/绑定关系）。
    无 legacy 映射的插件（纯插件能力）直接跳过 —— 不具备写回目标。
    """
    row = conn.execute("SELECT manifest_json FROM plugins WHERE plugin_id=?", (plugin_id,)).fetchone()
    if not row:
        return False, "插件不存在"
    try:
        rt = (json.loads(row["manifest_json"] or "{}").get("runtime") or {})
    except Exception:
        rt = {}
    table, lid = rt.get("legacy_table"), rt.get("legacy_id")
    if not table or not lid:
        return False, "无 legacy 映射，跳过"
    pairs = AVAILABILITY_WRITE.get(table)
    if not pairs:
        return False, "不支持的表: %s" % table
    sets, params = [], []
    for col, on_v, off_v in pairs:
        if available:
            sets.append("%s=?" % col)
            params.append(on_v)
        elif off_v is not None:       # 下架且该列需要改；off_v=None 表示保留原值
            sets.append("%s=?" % col)
            params.append(off_v)
    if not sets:
        return False, "无需要写入的列"
    params.append(lid)
    cur = conn.execute("UPDATE %s SET %s WHERE id=?" % (table, ", ".join(sets)), params)
    if not cur.rowcount:
        return False, "旧表记录不存在: %s:%s" % (table, lid)
    return True, "已写回 %s:%s available=%s" % (table, lid, available)


# ────────────────────────── 安全包装（供调用方使用）──────────────────────────

def sync_from_legacy_safe(conn, table, row_id):
    """外层包装：任何异常都吞掉，绝不阻断主业务流程。"""
    try:
        return sync_from_legacy(conn, table, row_id)
    except Exception as e:
        return False, "sync_from_legacy 异常: %s" % e


def sync_to_legacy_safe(conn, plugin_id, available):
    try:
        return sync_to_legacy(conn, plugin_id, available)
    except Exception as e:
        return False, "sync_to_legacy 异常: %s" % e
