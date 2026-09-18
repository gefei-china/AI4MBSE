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


def seed_all(conn: sqlite3.Connection) -> dict:
    """统一种子编排：内置工具 → 系统管理 → HTTP 集成 → 技能包 → 智源旧注册 → 图数据库工具。
    
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
    try:
        conn.commit()
    except Exception:
        pass
    return results


if __name__ == "__main__":
    from database import get_db
    c = get_db()
    r = seed_all(c)
    c.close()
    for k, v in r.items():
        print(f"  {k}: {v}")
