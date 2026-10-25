"""统一种子注册入口（P1-4，2026-08-31）：收敛 5 个分散注册源为单一编排。

背景（审计结论 E4）：工具/技能/Agent 注册此前分散在 5 处：
  1. database._seed_builtin_tools        —— 内置 file_*/graph_* 等原子工具（init_db 幂等）
  2. sysadmin_tools.seed                 —— 系统管理域只读工具 + system_mgmt Agent（lifespan 调用）
  3. register_http_tools（ZHIYUAN_TOOL_DEFS + integrations/*.json）—— HTTP 集成工具（配置驱动）
  4. register_zhiyuan_tools              —— 智源旧注册（已被 http 体系迁移，保留兼容调用）
  5. register_skills                     —— 内置技能包（文件操作/行业调研/报告生成）

本模块提供：
- seed_all(conn)：一次性执行全部幂等注册（main.py lifespan 只调本入口）
- seed_http_tools(conn)：仅 HTTP 集成工具（供需要时单独刷新）
- seed_skills(conn)：仅内置技能包
- seed_sysadmin(conn)：仅系统管理工具/Agent
- seed_builtin(conn)：仅内置原子工具

全部幂等（INSERT OR IGNORE / ON CONFLICT DO UPDATE），可重复执行。
"""
import sqlite3


def seed_builtin(conn: sqlite3.Connection) -> dict:
    """内置原子工具（file_*/graph_*/report_* 等）。"""
    try:
        from database import _seed_builtin_tools
        _seed_builtin_tools(conn)
        return {"builtin_tools": "ok"}
    except Exception as e:
        return {"builtin_tools": f"skip({e})"}


def seed_sysadmin(conn: sqlite3.Connection) -> dict:
    """系统管理域只读工具 + system_mgmt Agent（幂等种子）。"""
    try:
        from sysadmin_tools import seed as _seed_sys
        _seed_sys(conn)
        return {"sysadmin": "ok"}
    except Exception as e:
        return {"sysadmin": f"skip({e})"}


def seed_http_tools(conn: sqlite3.Connection) -> dict:
    """HTTP 集成工具（智源 5 接口 + integrations/*.json 配置驱动）。"""
    try:
        from register_http_tools import main as _main_http
        # register_http_tools.main() 自建连接（独立 DB 连接，与传入 conn 解耦）；
        # 调用后无需传回。返回统计字符串。
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            _main_http()
        return {"http_tools": "ok", "detail": buf.getvalue().strip().splitlines()[-1] if buf.getvalue().strip() else ""}
    except Exception as e:
        return {"http_tools": f"skip({e})"}


def seed_skills(conn: sqlite3.Connection) -> dict:
    """内置技能包（文件操作/行业调研/报告生成）。"""
    try:
        from register_skills import main as _main_skills
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            _main_skills()
        return {"skills": "ok"}
    except Exception as e:
        return {"skills": f"skip({e})"}


def seed_zhiyuan_legacy(conn: sqlite3.Connection) -> dict:
    """智源旧注册（兼容保留：已被 register_http_tools 覆盖，幂等空操作）。"""
    try:
        from register_zhiyuan_tools import main as _main_zy
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            _main_zy()
        return {"zhiyuan_legacy": "ok"}
    except Exception as e:
        return {"zhiyuan_legacy": f"skip({e})"}


def seed_graph_db_tools(conn: sqlite3.Connection) -> dict:
    """图数据库只读工具（graph_db_query / graph_db_stats，P2 图查询消费侧）。"""
    try:
        from register_graph_db_tools import seed as _seed_gdb
        return _seed_gdb(conn)
    except Exception as e:
        return {"graph_db_tools": f"skip({e})"}


def seed_sysml_check_tools(conn: sqlite3.Connection) -> dict:
    """SysML v2 校验工具 + 修复编排 Skill（2026-09-19：AI 建模自校验闭环）。

    注册 `sysml_v2_validate` 工具、绑定建模 Agent、写入「SysML v2 校验与修复」技能。
    幂等；`register_sysml_check_tools.main()` 自建连接（与传入 conn 解耦，同 seed_http_tools）。
    """
    try:
        from register_sysml_check_tools import main as _main_sc
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            _main_sc()
        return {"sysml_check_tools": "ok"}
    except Exception as e:
        return {"sysml_check_tools": f"skip({e})"}


def seed_all(conn: sqlite3.Connection) -> dict:
    """统一种子编排：内置工具 → 系统管理 → HTTP 集成 → 技能包 → 智源旧注册 → 图数据库工具
    → SysML 校验工具+技能 → **能力统一注册处全量对齐（P0-a）**。

    main.py lifespan 只调用本函数（替代原先分散的 sysadmin_tools.seed 调用）。
    单项失败不阻断其余（容错），返回各源结果字典供日志审计。
    """
    results = {}
    results.update(seed_builtin(conn))
    results.update(seed_sysadmin(conn))
    results.update(seed_http_tools(conn))
    results.update(seed_skills(conn))
    results.update(seed_zhiyuan_legacy(conn))
    results.update(seed_graph_db_tools(conn))
    results.update(seed_sysml_check_tools(conn))
    # ── P0-a（2026-10-24）：capability_registry 全量对齐放在所有能力注册之后 ──
    # 顺序约束：必须先有源表行、后投影。reconcile 幂等，每次启动重跑无副作用。
    try:
        from core import capability_registry as _cr
        results.update({"capability_registry": _cr.reconcile_capability_registry(conn)})
        results.update({"capability_backfill": _cr.backfill_capability_ids(conn)})
    except Exception as e:
        results.update({"capability_registry": f"skip({e})"})
    # ── P0-a：告警规则预置（10+1 条，metric 白名单制，幂等按 name 判存在）──
    results.update({"alert_rules": seed_alert_rules(conn)})
    try:
        conn.commit()
    except Exception:
        pass
    return results


# ── P0-a 告警规则预置（2026-10-24，设计见 docs/P0-a-度量收口详细设计 §3.4）──
# 引擎是 metric 白名单制：规则行只是数据，metric 必须在
# core/alert_evaluator.GLOBAL_METRICS 且有供数，否则永不触发也永不报错
# （"假实现"同型缺陷）。门禁 verify_capability_registry 断言这条不变式。
_ALERT_RULES = [
    # (name, metric, operator, threshold, level, status)
    ("登录失败突增（1h≥5次）", "auth_login_failed_1h", ">=", 5, "warning", "active"),
    ("成本突增（今日超昨日同期3倍）", "cost_today_vs_yday", ">=", 3, "warning", "active"),
    ("LLM 延迟劣化（1h P95>30s）", "llm_p95_latency_1h", ">", 30000, "warning", "active"),
    ("审计链破损", "audit_chain_ok", "<", 1, "critical", "active"),
    ("BadCase 积压（open≥5）", "badcase_open_count", ">=", 5, "warning", "active"),
    ("身份拒绝/认证异常突增（1h≥20次）", "auth_failed_1h", ">=", 20, "warning", "active"),
    ("LLM 失败率突增（1h>5%）", "llm_fail_rate_1h", ">", 5, "warning", "active"),
    ("编排批次停滞（非终态>30min）", "orch_stale_runs", ">=", 1, "warning", "active"),
    ("HIL 确认队列积压（超24h>3）", "hil_pending_old24h", ">", 3, "warning", "active"),
    ("检索质量下滑（低质占比>40%/h，代理口径）", "rag_low_quality_rate_1h", ">", 40, "warning", "active"),
    # 代理口径尚未校准（confirmed 集误判率≈68%），建而 paused——在位不造噪音，
    # P0-b 意图评测落库后校准阈值再激活。
    ("意图误判上升（误判率>50%，代理口径，待校准）", "intent_mismatch_rate", ">", 50, "warning", "paused"),
]

# 存量规则修正（一次性，幂等按旧 name 匹配）：原"登录失败突增（1h≥3次）"
# 的 metric=auth_failed_1h 实际 99.8% 统计的是身份拒绝（auth_identity_reject
# 491 条 vs auth_login failed 1 条）⇒ 更名+调阈值，语义归位给新的
# auth_login_failed_1h（真·登录失败，R1）。
_ALERT_RULE_LEGACY_FIX = (
    "登录失败突增（1h≥3次）",           # 旧 name（匹配用）
    "身份拒绝/认证异常突增（1h≥20次）",  # 新 name
    20,                                  # 新阈值（原 3 太灵：38 条告警全它）
)


def seed_alert_rules(conn: sqlite3.Connection) -> dict:
    """P0-a：预置 11 条告警规则（10 条体系化 + 1 条存量口径修正）。幂等。"""
    try:
        old_name, new_name, new_th = _ALERT_RULE_LEGACY_FIX
        cur = conn.execute(
            "UPDATE alert_rules SET name=?, threshold=?, updated_at=CURRENT_TIMESTAMP "
            "WHERE name=?", (new_name, new_th, old_name))
        fixed = cur.rowcount or 0
        added = 0
        for name, metric, op, th, level, status in _ALERT_RULES:
            exists = conn.execute("SELECT 1 FROM alert_rules WHERE name=?",
                                  (name,)).fetchone()
            if exists:
                continue
            conn.execute(
                "INSERT INTO alert_rules (name, metric, operator, threshold, level, status) "
                "VALUES (?,?,?,?,?,?)", (name, metric, op, th, level, status))
            added += 1
        return {"fixed_legacy": fixed, "added": added, "total_defined": len(_ALERT_RULES)}
    except Exception as e:
        return {"error": str(e)[:200]}


if __name__ == "__main__":
    from database import get_db
    c = get_db()
    r = seed_all(c)
    c.close()
    for k, v in r.items():
        print(f"  {k}: {v}")
