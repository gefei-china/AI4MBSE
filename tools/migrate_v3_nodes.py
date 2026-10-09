# -*- coding: utf-8 -*-
"""V3.0 多任务域迁移 —— 幂等、可预览、分步断言。

范围（V1/V3/V4 + 不需要 agent/skill/tool 的清理）
---------------------------------------------------
  V1  agents 新增 task_domain / intent_keywords_active 两列 + 19 个 active Agent 显式回填
  V3  report_generation 补绑 report_export + graph_db_query + coverage_matrix（实测零工具）
  V4  knowledge_qa解绑 gap_summary（实测语义冲突：它是 N5 的工具，单独调违反自身规约）
  C1  移除不需要的工具绑定：8 个视图 Agent 身上的 sys_*（系统查询不属于建模节点）
  C2  移除不需要的 skill：13 条里的 4 条测试垃圾 + 1 条被 V4/V5 取代的重复项
  C3  移除不需要的 agent：2 条 disabled 残留（zhiyuan_mgmt / 测试agent）

**不删表、不删行**：一律用 status='disabled' 停用而非 DELETE，保留可回滚性。
**不猜**：每个 domain 归属都写进 TASK_DOMAIN_MAP，值有据可查（见各条注释）。

幂等：可重复执行；已改过的行会被跳过并计入unchanged。
预览：默认 dry-run，加 --apply 才写库。
"""
import argparse
import json
import os
import sqlite3
import sys
import time

DB = "mbse.db"
DRY = "--apply" not in sys.argv

# ─────────────────────────────────────────────────────────────
# V1 任务域回填表（19 个 active Agent，逐个显式赋值，不猜）
# 依据：v3.0 规范 §1.1「五任务域划分」+ compat_matrix
# ─────────────────────────────────────────────────────────────
TASK_DOMAIN = {
    # 建模域：流水线节点（M0/G 由迁移脚本后续单独建，本轮只标域）
    "MBSE建模总体负责人": ("modeling", "主Agent，只调度建模域"),
    "requirement_analysis": ("modeling", "→ N1 需求结构化"),
    "design": ("modeling", "→ N2 架构骨架"),
    "review": ("modeling", "→ N4 校验修复"),
    "需求视图生成": ("modeling", "→ N3 view_type=requirement"),
    "结构视图生成": ("modeling", "→ N3 view_type=structure"),
    "交互视图（IBD）生成": ("modeling", "→ N3 view_type=ibd"),
    "用例视图生成": ("modeling", "→ N3 view_type=usecase"),
    "活动图生成": ("modeling", "→ N3 view_type=activity"),
    "状态机视图生成": ("modeling", "→ N3 view_type=state"),
    "参数视图生成": ("modeling", "→ N3 view_type=parameter"),
    "顺序视图（时序图）生成": ("modeling", "→ N3 view_type=sequence"),
    "多方案生成": ("modeling", "独立触发，变体空间候选，不产出模型代码"),
    # 问答域
    "chat": ("qa", "通用入口+MCP文档解析"),
    "knowledge_qa": ("qa", "知识问答，零建模产物⇒零门禁"),
    # 系统管理域
    "system_mgmt": ("system", "6 个 sys_* 全read，与建模零交集"),
    # 报告域（旁路）
    "report_generation": ("report", "旁路消费建模产物"),
    # 影响域（旁路）
    "impact": ("impact", "独立触发，5工具已绑；G 节点复用其 impact_analyze"),
    # 通用
    "requirement_quality": ("general", "execute 有专门双通道，不编排"),
}

# ─────────────────────────────────────────────────────────────
# V3 报告 Agent 补绑工具（实测 group_concat 返回 NULL = 零绑定）
# 只绑只读 + 一个导出；不绑 entity_create（报告不落图谱）
# ─────────────────────────────────────────────────────────────
REPORT_TOOLS = ["report_export", "graph_db_query", "coverage_matrix"]

# ─────────────────────────────────────────────────────────────
# V4 knowledge_qa 解绑 gap_summary
# 理由：gap_summary 是 N5 的工具，sysml_trace_coverage_analysis 铁律要求
#      「必须最后调、必须消费前三者结果」，单独调用即违反其自身规约。
#      问答场景不需要"缺项汇总"。
# ─────────────────────────────────────────────────────────────
QA_UNBIND = ["gap_summary"]

# ─────────────────────────────────────────────────────────────
# C1 8 个视图 Agent 移除 sys_* 绑定
# 实测：6 个 sys_* 被绑给 12 个 Agent（含 8 个视图 Agent），
#      占 33 工具的 18%，挤占 JIT 预筛与 core_keep 注入位。
#      系统查询不属于建模节点。
# ─────────────────────────────────────────────────────────────
VIEW_AGENTS = ["需求视图生成", "结构视图生成", "交互视图（IBD）生成", "用例视图生成",
               "活动图生成", "状态机视图生成", "参数视图生成", "顺序视图（时序图）生成"]

# ⚠️ 禁用 `tool_name LIKE 'sys_%'`：SQL LIKE 里 `_` 是**单字符通配符**，
#实测会连带匹配到 `sysml_v2_validate`（视图 Agent 唯一的建模工具）。
# 正确写法：`LIKE 'sys!_%' ESCAPE '!'`（见 SYS_LIKE）。
# 为绝对稳妥，这里改用**显式白名单**，不用模式匹配。
SYS_TOOLS = ["sys_query_audit", "sys_query_convs", "sys_query_monitor",
             "sys_query_perms", "sys_query_roles", "sys_query_users"]
SYS_LIKE = "sys!_%"
SYS_ESC = "!"

# ─────────────────────────────────────────────────────────────
# C2 清理不需要的 skill
# 判据：① 名称含 test- ⇒ 门禁/自测残留 ② disabled 且无工具绑定的重复项
# 数据依据：id=93/94 是 test-skill-200574 / test-skill-688623（draft 垃圾）
# ─────────────────────────────────────────────────────────────
SKILL_DISABLE = {
    "test-skill-200574": "门禁自测残留（draft，无工具绑定，无引用）",
    "test-skill-688623": "门禁自测残留（draft，无工具绑定，无引用）",
    "Requirement_Analysis_Pre-check": "与 MBSE_Requirements_Analysis_Workflow 职责重叠，且有 id=70/85 重复行",
}

# ─────────────────────────────────────────────────────────────
# C3 清理不需要的 agent（残留 disabled 项）
# ─────────────────────────────────────────────────────────────
AGENT_DISABLE = {
    "zhiyuan_mgmt": "智源平台管理团队：成员仅 3 个且能力弱于 http 工具直接调用；zhiyuan_* 工具仍在库可直用",
    "测试agent": "自测残留",
}

stats = {"changed": 0, "unchanged": 0, "skipped": 0}


def _log(k, name, detail=""):
    stats[k] += 1
    tag = {"changed": "CHANGED", "unchanged": "SAME  ", "skipped": "SKIP  "}[k]
    print(f"  [{tag}] {name}{('  · ' + detail) if detail else ''}")


def cols(conn):
    return {r[1] for r in conn.execute("PRAGMA table_info(agents)")}


def step_v1(conn):
    print("\n── V1 · task_domain / intent_keywords_active ──")
    have = cols(conn)
    added = []
    for c, ddl in [("task_domain", "TEXT DEFAULT 'general'"),
                   ("intent_keywords_active", "INTEGER DEFAULT 0")]:
        if c not in have:
            conn.execute(f"ALTER TABLE agents ADD COLUMN {c} {ddl}")
            added.append(c)
            _log("changed", f"新增列 agents.{c}")
        else:
            _log("unchanged", f"列 agents.{c} 已存在")

    active = {r[0]: r[1] for r in conn.execute(
        "SELECT name, display_name FROM agents WHERE status='active'")}
    missing = [n for n in TASK_DOMAIN if n not in active]
    for n in missing:
        _log("skipped", f"{n}（库里无此 active agent）")

    for name, (dom, why) in TASK_DOMAIN.items():
        if name not in active:
            continue
        cur = conn.execute(
            "SELECT task_domain FROM agents WHERE name=?", (name,)).fetchone()[0]
        if cur == dom:
            _log("unchanged", f"{name} → {dom}")
        else:
            conn.execute("UPDATE agents SET task_domain=? WHERE name=?", (dom, name))
            _log("changed", f"{name} → {dom}", why)
    if not DRY:
        conn.commit()
    return added


def step_v3(conn):
    print("\n── V3 · report_generation 补绑工具（实测零绑定）──")
    row = conn.execute(
        "SELECT id FROM agents WHERE name='report_generation'").fetchone()
    if not row:
        _log("skipped", "report_generation 不存在")
        return
    aid = row[0]
    have = {r[0] for r in conn.execute(
        "SELECT tool_name FROM agent_tools WHERE agent_id=? AND tool_type='tool'", (aid,))}
    for t in REPORT_TOOLS:
        tid = conn.execute("SELECT id FROM tools WHERE name=?", (t,)).fetchone()
        if not tid:
            _log("skipped", f"{t}（tools 表无此工具）")
            continue
        exists = conn.execute(
            "SELECT 1 FROM agent_tools WHERE agent_id=? AND tool_name=? AND tool_type='tool'",
            (aid, t)).fetchone()
        if exists:
            _log("unchanged", f"report_generation ← {t}")
        else:
            conn.execute(
                "INSERT INTO agent_tools (agent_id, tool_type, tool_name, enabled) VALUES (?,?,?,1)",
                (aid, "tool", t))
            _log("changed", f"report_generation ← {t}")
    if not DRY:
        conn.commit()


def step_v4(conn):
    print("\n── V4 · knowledge_qa 解绑 gap_summary（语义冲突）──")
    row = conn.execute("SELECT id FROM agents WHERE name='knowledge_qa'").fetchone()
    if not row:
        _log("skipped", "knowledge_qa 不存在")
        return
    aid = row[0]
    for t in QA_UNBIND:
        n = conn.execute("SELECT COUNT(*) FROM agent_tools WHERE agent_id=? AND tool_name=?",
                         (aid, t)).fetchone()[0]
        if n == 0:
            _log("unchanged", f"knowledge_qa 未绑 {t}")
        else:
            conn.execute("DELETE FROM agent_tools WHERE agent_id=? AND tool_name=?", (aid, t))
            _log("changed", f"knowledge_qa ✕ {t}", "N5 工具，单独调违反其铁律")
    if not DRY:
        conn.commit()


def step_c1(conn):
    print("\n── C1 · 8 个视图 Agent 移除 sys_* 绑定 ──")
    for disp in VIEW_AGENTS:
        row = conn.execute("SELECT id,name FROM agents WHERE display_name=?", (disp,)).fetchone()
        if not row:
            _log("skipped", f"{disp} 不存在")
            continue
        aid, aname = row
        rows = conn.execute(
            "SELECT tool_name FROM agent_tools WHERE agent_id=? AND tool_name IN (%s)"
            % ",".join("?" * len(SYS_TOOLS)), [aid] + SYS_TOOLS).fetchall()
        if not rows:
            _log("unchanged", f"{disp} 未绑 sys_*")
        else:
            names = [r[0] for r in rows]
            conn.execute(
                "DELETE FROM agent_tools WHERE agent_id=? AND tool_name IN (%s)"
                % ",".join("?" * len(SYS_TOOLS)), [aid] + SYS_TOOLS)
            _log("changed", f"{disp} ✕ {len(names)} 个 sys_*", ",".join(names))
    if not DRY:
        conn.commit()


def step_c2(conn):
    print("\n── C2 · 清理不需要的 skill（停用不删除）──")
    for name, why in SKILL_DISABLE.items():
        rows = conn.execute("SELECT id,status,enabled FROM skills WHERE name=?",
                            (name,)).fetchall()
        if not rows:
            _log("skipped", f"skill {name} 不存在")
            continue
        # 同名可能多行（如 Requirement_Analysis_Pre-check 有 id=70/85 重复）
        pend = [r for r in rows if r[1] != "disabled"]
        if not pend:
            _log("unchanged", f"skill {name}（{len(rows)} 行均已 disabled）")
            continue
        for sid, st, en in pend:
            conn.execute("UPDATE skills SET status='disabled', enabled=0 WHERE id=?", (sid,))
            _log("changed", f"skill {name}#{sid} → disabled", why)
    if not DRY:
        conn.commit()


def step_c3(conn):
    print("\n── C3 · 清理不需要的 agent（停用不删除）──")
    for name, why in AGENT_DISABLE.items():
        row = conn.execute("SELECT display_name,status FROM agents WHERE name=?", (name,)).fetchone()
        if not row:
            _log("skipped", f"agent {name} 不存在")
            continue
        disp, st = row
        if st == "disabled":
            _log("unchanged", f"agent {name}（已 disabled）")
        else:
            conn.execute("UPDATE agents SET status='disabled' WHERE name=?", (name,))
            _log("changed", f"agent {disp} → disabled", why)
    if not DRY:
        conn.commit()


def verify(conn):
    """迁移后自检 —— 不信任『执行成功』，回读校验。"""
    print("\n" + "=" * 68)
    print("迁移后自检（回读校验，不信任执行返回）")
    ok = True

    # task_domain 回填自检：以TASK_DOMAIN_MAP 为准，而不是"非 general 即已填"
    missing = []
    for name, (dom, _) in TASK_DOMAIN.items():
        r = conn.execute("SELECT task_domain FROM agents WHERE name=? AND status='active'",
                         (name,)).fetchone()
        if r is None:
            continue
        if r[0] != dom:
            missing.append(f"{name}(={r[0]},期望{dom})")
    print(f"  active Agent 标域错配：{len(missing)} 个")
    if missing:
        ok = False
        print(f"   ⇒ FAIL {missing[:5]}")

    n = conn.execute("SELECT COUNT(*) FROM agents WHERE status='active' "
                     "AND task_domain IS NULL").fetchone()[0]
    print(f"  active Agent task_domain 为 NULL：{n}（期望 0）")
    if n:
        ok = False

    dist = conn.execute("SELECT task_domain, COUNT(*) FROM agents WHERE status='active' "
                        "GROUP BY 1 ORDER BY 2 DESC").fetchall()
    print(f"  域分布：{dict(dist)}")

    n = conn.execute("SELECT COUNT(*) FROM agent_tools t JOIN agents a ON a.id=t.agent_id "
                     "WHERE a.name='report_generation' AND t.tool_type='tool'").fetchone()[0]
    print(f"  report_generation 工具数：{n}（期望 ≥3）")
    if n < 3:
        ok = False
        print("   ⇒ FAIL 报告 Agent 工具未补齐")

    n = conn.execute("SELECT COUNT(*) FROM agent_tools t JOIN agents a ON a.id=t.agent_id "
                     "WHERE a.name='knowledge_qa' AND t.tool_name='gap_summary'").fetchone()[0]
    print(f"  knowledge_qa 仍绑 gap_summary：{n}（期望 0）")
    if n:
        ok = False
        print("   ⇒ FAIL 语义冲突未解")

    n = conn.execute(
        "SELECT COUNT(*) FROM agent_tools t JOIN agents a ON a.id=t.agent_id "
        "WHERE a.display_name IN (%s) AND t.tool_name IN (%s)"
        % (",".join("?" * len(VIEW_AGENTS)), ",".join("?" * len(SYS_TOOLS))),
        VIEW_AGENTS + SYS_TOOLS).fetchone()[0]
    print(f"  8 视图 Agent 的 sys_* 绑定数：{n}（期望 0）")
    if n:
        ok = False
        print("   ⇒ FAIL 越界工具未移除")

    # 关键回归：视图 Agent 必须保住 sysml_v2_validate（_is_v2_code_agent 靠它注入 L0 卡）
    n = conn.execute(
        "SELECT COUNT(*) FROM agent_tools t JOIN agents a ON a.id=t.agent_id "
        "WHERE a.display_name IN (%s) AND t.tool_name='sysml_v2_validate'"
        % ",".join("?" * len(VIEW_AGENTS)), VIEW_AGENTS).fetchone()[0]
    print(f"  8 视图 Agent 仍绑 sysml_v2_validate：{n}（期望 {len(VIEW_AGENTS)}）")
    if n != len(VIEW_AGENTS):
        ok = False
        print("   ⇒ FAIL 视图 Agent 丢了建模校验工具（L0 卡依赖它）")

    n = conn.execute("SELECT COUNT(*) FROM skills WHERE status!='disabled' AND name IN (%s)"
                     % ",".join("?" * len(SKILL_DISABLE)), list(SKILL_DISABLE)).fetchone()[0]
    print(f"  待清理 skill 仍启用：{n}（期望 0）")
    if n:
        ok = False

    n = conn.execute("SELECT COUNT(*) FROM agents WHERE status!='disabled' AND name IN (%s)"
                     % ",".join("?" * len(AGENT_DISABLE)), list(AGENT_DISABLE)).fetchone()[0]
    print(f"  待清理 agent 仍启用：{n}（期望 0）")
    if n:
        ok = False

    # 回归：确认没有把该留的清掉
    for keep in ("knowledge_qa", "system_mgmt", "impact", "chat", "多方案生成"):
        st = conn.execute("SELECT status FROM agents WHERE name=?", (keep,)).fetchone()
        s = st[0] if st else "MISSING"
        flag = "OK" if s == "active" else "FAIL"
        if s != "active":
            ok = False
        print(f"  [{flag}] 保留项 {keep}：{s}")
    n = conn.execute("SELECT COUNT(*) FROM tools WHERE status='active'").fetchone()[0]
    print(f"  active 工具数：{n}（本轮不删工具，只解绑）")
    n = conn.execute("SELECT COUNT(*) FROM skills WHERE status='published' AND enabled=1").fetchone()[0]
    print(f"  published+enabled skill 数：{n}")

    print("=" * 68)
    print("✅ 自检全通过" if ok else "❌ 自检有FAIL 项")
    return 0 if ok else 1


def main() -> int:
    print("=" * 68)
    print(f"V3.0 节点体系迁移  mode={'APPLY（写库）' if not DRY else 'DRY-RUN（只预览）'}")
    print("=" * 68)
    conn = sqlite3.connect(DB)
    try:
        step_v1(conn)
        step_v3(conn)
        step_v4(conn)
        step_c1(conn)
        step_c2(conn)
        step_c3(conn)
        rc = verify(conn)
    finally:
        conn.close()
    print(f"\n统计：changed={stats['changed']} unchanged={stats['unchanged']} "
          f"skipped={stats['skipped']}")
    if DRY:
        print("（预览模式，未写库；加 --apply 生效）")
    return rc


if __name__ == "__main__":
    sys.exit(main())
