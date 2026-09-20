"""跨模块公共工具（原 main.py 通用工具区）：审计日志 + SQLite 行转换。

从 main.py 抽出，供所有 routers 复用；本模块只依赖 database 基础设施。
"""
from database import db_conn


def rows_to_list(rows):
    """sqlite3.Row 列表 → dict 列表（JSON 序列化友好）。"""
    return [dict(r) for r in rows]


def audit(user, event, detail, result="success", conn=None, branch=""):
    """写入审计日志，统一审计口径。

    - conn 传入（推荐，P2 后路由持有请求级连接）：审计与业务写
      在同一事务提交，避免 SQLite 写锁冲突（database is locked）。
    - conn 缺省：独立开连接即时提交（兼容旧调用方）。
    - branch：分支归属（2026-09-14）——图谱/推理等域事件记录操作分支，
      供历史 Tab / 审计查询按分支过滤；全局事件（登录/LLM 等）留空。
    """
    sql = "INSERT INTO audit_logs (user_name, event_type, detail, result, branch) VALUES (?,?,?,?,?)"
    if conn is not None:
        conn.execute(sql, (user, event, detail, result, branch or ""))
        return
    with db_conn() as conn:
        conn.execute(sql, (user, event, detail, result, branch or ""))


def audit_user(user) -> str:
    """审计归属：当前登录用户显示名；未识别请求标记为「未登录」。

    user 为 core.deps.current_user 依赖返回值（dict | None）。
    替代历史写死的 audit("admin"/"王工", ...) 调用，使审计日志可信。
    """
    return (user or {}).get("display_name") or (user or {}).get("username") or "未登录"
