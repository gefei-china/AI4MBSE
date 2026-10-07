"""权限矩阵迁移（P0-6/P0-2，2026-10-06）—— Agent 工具链 RBAC 与 HIL 批准授权。

**为什么需要这个迁移（背景见 docs/Agent生产化Harness对照核查-20261006.md P0-1）**：
`core/deps.py` 抽出 `check_tool_perm` 给 Agent 工具链用后，
`roles.permissions` 里若没有 `agent_tool` 域，**所有非 admin 角色会被全量403**。

⚠️ 本工程的现实陷阱（实测2026-10-06）：
`SELECT role_id, COUNT(*) FROM users GROUP BY 1` ⇒ **role_id=80（设计师）承担 100% 用户**。
若只加门不授权，上线当天**所有人**（含米爸本人）调不动任何 Agent 工具——
这是「加门」与「授权」必须同一批交付的直接原因，两者拆开做必出事。

**授权口径（刻意从宽，逐步收紧）**：
- `agent_tool:read`授予 **preset** 角色的全部（设计师/知识工程师/系统管理员）；
  读是MBSE 的主工作面（检索图谱/读文件），按现状全开=不改变既有行为。
- `agent_tool:write` 同样授予 preset：写类工具**另有三道既有闸**
  （HIL 人工确认 / destructive 硬拒 / 编排子任务暂存），本项是第二道锁而非唯一锁。
- **custom 角色一律不授**（实测有 8 个「载荷审评专家…」custom 角色 permissions 为 `{}`）。
  依据 §7.3-1b 的纪律：不能因为"数据是空"就假设"业务不需要"——
  但这里恰恰相反，**缺权限是安全默认**，custom 角色需管理员显式授权才可用 Agent 工具，
  这符合最小权限原则。**这不是"没做"，是刻意的安全默认。**
- `hil:approve` 授予 preset —— HIL 批准端点此前**无任何权限门且`decided_by` 硬编码「王工」**，
  任何登录用户都能批准任意确认单（批准即触发写操作自动执行）。

幂等：JSON 逐 domain合并，已存在的 op 不动；可重复执行。
"""
import json


#: 需要授权的 domain → ops。
#: 键是 roles.permissions 里的 domain 名，与 core/deps.py 的判定处一一对应。
_GRANTS = {
    "agent_tool": ["read", "write"],   # Agent 工具链：读 / 写
    # hil 分两个位：**view 与 approve 必须成对授予**——确认单是写操作的唯一闸门，
    # 能看（列表）就等于能批（批准即触发写执行）。只授 approve 会让列表 403，
    # 只授 view 会让批准无门；两者一起给才是完整闭环。
    "hil": ["view", "approve"],
}

#: 只授给preset 角色。custom 保持最小权限（安全默认，需显式授权）。
_PRESET_ONLY = True


def _migrate_agent_tool_perm(conn):
    """给 preset 角色补 `agent_tool` / `hil` 权限（幂等）。

    ⚠️ 为什么要显式做这一步而不是靠种子数据：`roles` 表的 preset 行是
    `seeds.py` 早期写入的，**已落库的角色不会被新种子覆盖**（种子幂等靠
    "存在即跳过"）。所以新增权限位必须靠迁移补，否则改种子对老库无效。
    """
    c = conn.cursor()
    rows = c.execute("SELECT id, name, type, permissions FROM roles").fetchall()
    changed = 0
    for r in rows:
        rid, name, rtype, raw = r[0], r[1], (r[2] or ""), (r[3] or "{}")
        if _PRESET_ONLY and (rtype or "") != "preset":
            continue
        try:
            perms = json.loads(raw)
            if not isinstance(perms, dict):
                perms = {}
        except Exception:
            perms = {}
        dirty = False
        for dom, want in _GRANTS.items():
            cur = list(perms.get(dom) or [])
            for op in want:
                if op not in cur:
                    cur.append(op)
                    dirty = True
            perms[dom] = cur
        if dirty:
            c.execute("UPDATE roles SET permissions=? WHERE id=?",
                      (json.dumps(perms, ensure_ascii=False), rid))
            changed += 1
            print(f"[init_db] 迁移:角色 #{rid} {name} 补权限 {sorted(_GRANTS)}")
    if changed:
        conn.commit()
    return changed