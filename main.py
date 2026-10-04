"""FastAPI 装配入口 - AI-enabled MBSE Design Tool backend.

职责仅限：创建 app、注册各功能域 router、挂载静态文件、启动初始化。
业务代码已按功能域拆分至 routers/ 包（见 routers/__init__.py）。
新增功能域流程：新建 routers/xxx.py → routers/__init__.py 注册 → 本文件 include 一行。
"""
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse

from database import init_db, get_db
from core import config
from core.rate_limit import RateLimitMiddleware
from routers import (
    auth_router,
    dashboard_router,
    users_router,
    conversations_router,
    knowledge_router,
    branches_router,
    studio_router,
    llm_router,
    meta_router,
    projects_router,
    views_router,
    config_router,
    glossary_router,
    glossary_io_router,
    monitor_router,
    reports_router,
    artifacts_router,
    impact_router,
    sysml_versions_router,
    plugins_router,
    graph_db_router,
    graph_workspace_router,
    governance_router,
    swrl_router,  # P1-①/②（2026-09-11）SWRL 规则管理
    sparql_router,  # P2-①/②（2026-09-11）SPARQL 1.1 Endpoint
    data_sources_router,  # P0-4（2026-09-20）数据源注册管理（db/api/file 三类源接入）
    coverage_router,  # SRS-GN-CO（2026-09-20）覆盖性分析呈现端点
    doc_folders_router,  # 2026-09-21 文档目录树（基于文件的管理 P0-b）
    mcp_gateway_router,  # P0-1（2026-09-24）领域能力 MCP 服务端
    intent_samples_router,  # 2026-09-26 意图样本池（设置页维护评测集）
    memory_admin_router,  # 2026-10-02 AI 记忆管理（设置页可见 + 可删）
    ux_metrics_router,  # 2026-10-03 UX 埋点：§11 六指标采集与聚合
    orchestration_router,  # 2026-10-03 P0-2 编排检查点 + 断点续跑
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期（替代已弃用的 @app.on_event("startup")，语义等价）。"""
    init_db()
    # P1-4（2026-10-02）：工具结果 offload 过期清理（TTL 默认 30 天，context.offload_ttl_days）
    try:
        from agent.pipeline_parts import tool_offload as _toff
        _n = _toff.cleanup_expired(days=int(config.get("context", "offload_ttl_days", 30)))
        if _n:
            print(f"[startup] offload 过期清理：删除 {_n} 行", flush=True)
    except Exception:
        pass  # 清理失败不得影响启动（TTL 下次启动再清）
    # P1-4（2026-08-31）：统一种子注册入口（内置工具/系统管理/HTTP 集成/技能包/智源旧注册 幂等编排）
    try:
        from seed_registry import seed_all as _seed_all
        from database import db_conn as _db_conn
        with _db_conn() as _c:
            _seed_all(_c)
    except Exception:
        pass
    # P1-5（2026-09-06）：FTS5 全文索引初始化（虚表+触发器幂等；空索引自动重建）
    try:
        from fts_search import ensure_fts
        ensure_fts(get_db())
    except Exception:
        pass
    # P0-d（2026-10-03 评估）：周期告警评估器 —— 现状只有 run 结束时的一次性评估，
    #   非编排流量（会话直答/意图识别等绝大多数真实请求）的 LLM 异常、成本突增、延迟劣化、
    #   以及登录失败突增、审计链破损，此前都处于无守护状态。
    try:
        from core.alert_evaluator import start_alert_loop
        start_alert_loop(interval_sec=float(config.get("monitor", "alert_interval", 300)),
                         cooldown_min=int(config.get("monitor", "alert_cooldown_min", 30)))
    except Exception as _e:
        print("[startup] 周期告警评估器跳过: %s" % str(_e)[:120], flush=True)
    # P0-2b（2026-10-04）：编排自动重投守护 —— 让"崩溃的编排自己好"，不再等人点继续。
    # 默认关（会真的调 LLM）；开关口径见 config.orchestration.auto_resume_*。
    try:
        if config.as_bool("orchestration", "auto_resume_enabled", False):
            from core.orch_supervisor import start_resume_loop
            start_resume_loop(
                interval_sec=float(config.get("orchestration", "auto_resume_interval_s", 300)),
                max_runs=int(config.get("orchestration", "auto_resume_max_runs", 3)),
                stale_s=int(config.get("orchestration", "resume_stale_s", 1800)),
                include_failed=bool(config.get("orchestration", "auto_resume_include_failed", False)),
            )
        else:
            print("[startup] 编排自动重投守护未启用（orchestration.auto_resume_enabled=off，"
                  "崩溃批次需人工点继续）", flush=True)
    except Exception as _e:
        print("[startup] 自动重投守护跳过：%s" % str(_e)[:120], flush=True)

    # MCP-D1：后台健康巡检线程（每 5 分钟对 sse/http 服务器做 initialize 握手探测）
    try:
        from mcp_health import start_health_loop
        start_health_loop(lambda: get_db(), interval_sec=float(config.get("mcp", "health_interval", 300)))
    except Exception:
        pass
    # 2026-09-26：语义层**后台预热** —— 候选集向量是惰性算的，此前这笔开销（实测约 8.6s）全落在
    #   第一个真实请求上；这里在启动时构造一次路由（触发 set_semantic_index → 守护线程预热），
    #   让向量化发生在用户开口之前。整体 try/except：预热失败不得影响启动。
    try:
        from agent.pipeline import AgentPipeline as _AgentPipeline
        _wp = _AgentPipeline()
        _wp._load_db_agents()     # user=None：与"内置能力"路由池一致；真实私有 Agent 由各请求自建索引
        print("[startup] 语义层后台预热已启动", flush=True)
    except Exception as _e:
        print("[startup] 语义层预热跳过：%s" % str(_e)[:120], flush=True)
    # ── P0-2（2026-10-03 整改）：孤儿编排任务回收 ──
    # 对标 LangGraph checkpointer / Temporal timeout 的**第一步**：在没有独立 worker 之前，
    # 先保证"任何一场永远不会结束的运行都有一个明确的结局"（见 core/run_registry.py 的说明）。
    # 回收只改非终态且已停滞的任务状态，**不触碰任何执行路径**，失败也不阻断启动。
    try:
        if bool(config.get("runtime", "reap_on_startup", True)):
            from core.run_registry import reap_orphans
            from database import db_conn as _db_conn2
            with _db_conn2() as _c2:
                _res = reap_orphans(_c2, dry_run=bool(config.get("runtime", "reap_dry_run", False)))
            if _res.get("reaped"):
                print("[startup] 孤儿编排任务回收：%d 条 / %d 个批次（TTL=%ds）"
                      % (_res["reaped"], len(_res.get("run_ids") or []), _res["ttl_s"]), flush=True)
            if _res.get("error"):
                print("[startup] 孤儿回收异常（已跳过）：%s" % _res["error"], flush=True)
    except Exception as _e:
        print("[startup] 孤儿回收跳过：%s" % str(_e)[:120], flush=True)
    yield


app = FastAPI(title=config.APP_TITLE, version=config.APP_VERSION, lifespan=lifespan)


# ── P0-a（2026-10-03）请求溯源上下文中间件：《运行监控与审计日志差距评估》P0-a ──
# 把 IP / User-Agent / request_id 绑定到 ContextVar，core.audit.audit() 自动写入
# audit_logs（此前 ip_address 列存在但从不落盘 —— 审计六要素缺 Source）。
#
# 刻意不用 starlette 的 BaseHTTPMiddleware：它会在响应侧建 anyio 任务组并包装 body，
# 对本工程的 SSE 流式输出（/api/chat/stream、工具事件流）存在已知干扰。
# 纯 ASGI 中间件只是包装 send，对流式响应完全透明。
class AuditRequestContextMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":          # websocket / lifespan 直接透传
            await self.app(scope, receive, send)
            return
        try:
            from starlette.requests import Request as _Req
            from starlette.datastructures import MutableHeaders as _MH
            from core.audit import bind_request
            ctx = bind_request(_Req(scope))
            rid = ctx.get("request_id") or ""

            async def _send(message):
                if rid and message.get("type") == "http.response.start":
                    _MH(scope=message)["X-Request-Id"] = rid
                await send(message)
        except Exception:
            _send = send   # 上下文绑定失败不应影响请求本身（审计降级，业务照常）
        try:
            await self.app(scope, receive, _send)
        finally:
            try:
                from core.audit import bind_request as _bind, clear_trace as _clear_trace
                _bind(None)          # 清理，避免跨请求串号
                _clear_trace()       # P0-c：链路上下文同请求生命周期
            except Exception:
                pass


app.add_middleware(AuditRequestContextMiddleware)

# ── P1-1（2026-10-03 整改）：入口限流 ──
# 纯 ASGI（同审计中间件的理由：不用 BaseHTTPMiddleware，避免包装 SSE 响应体）。
# 注意中间件**注册顺序 = 执行顺序的逆序**，这里注册在审计之后 ⇒ 限流比审计**先**执行，
# 于是被限掉的请求不会留下"看起来发生过"的审计痕迹（限流不是业务事件）。
app.add_middleware(RateLimitMiddleware)


# ── 注册功能域路由（URL 契约与重构前完全一致）──
for _router in (
    auth_router,
    dashboard_router,
    users_router,
    conversations_router,
    knowledge_router,
    branches_router,
    studio_router,
    llm_router,
    meta_router,
    projects_router,
    views_router,
    config_router,
    glossary_router,
    monitor_router,
    reports_router,
    artifacts_router,
    impact_router,
    sysml_versions_router,
    plugins_router,
    graph_db_router,
    graph_workspace_router,
    governance_router,
    glossary_io_router,
    swrl_router,  # P1-①/②（2026-09-11）SWRL 规则管理
    sparql_router,  # P2-①/②（2026-09-11）SPARQL 1.1 Endpoint
    data_sources_router,  # P0-4（2026-09-20）数据源注册管理（db/api/file 三类源接入）
    coverage_router,  # SRS-GN-CO（2026-09-20）覆盖性分析呈现端点
    doc_folders_router,  # 2026-09-21 文档目录树（基于文件的管理 P0-b）
    mcp_gateway_router,  # P0-1（2026-09-24）领域能力 MCP 服务端：对外供给领域能力，供外部 harness 消费
    intent_samples_router,  # 2026-09-26 意图样本池：设置页维护评测集 + 一键跑分
    memory_admin_router,  # 2026-10-02 AI 记忆管理：设置页可见 + 可删（评估报告 §4 P0）
    ux_metrics_router,  # 2026-10-03 UX 埋点：§11 六指标采集与聚合
    orchestration_router,  # 2026-10-03 P0-2 编排检查点 + 断点续跑（LangGraph checkpointer 等价物）
):
    app.include_router(_router)


# ═══════════════ 静态文件与 SPA 入口 ═══════════════
static_dir = config.STATIC_DIR
if os.path.exists(static_dir):
    # S4(C1)：静态文件 no-cache——避免浏览器缓存旧版 index.html（米爸反馈"看的还是旧版"）
    from starlette.staticfiles import StaticFiles as _SM

    class NoCacheStaticFiles(_SM):
        def file_response(self, *a, **kw):
            resp = super().file_response(*a, **kw)
            resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            resp.headers["Pragma"] = "no-cache"
            return resp

    app.mount("/static", NoCacheStaticFiles(directory=static_dir), name="static")


@app.get("/", response_class=HTMLResponse)
def index():
    index_path = os.path.join(static_dir, "index.html")
    if os.path.exists(index_path):
        with open(index_path, "r", encoding="utf-8") as f:
            return HTMLResponse(f.read(), headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
            })
    return HTMLResponse("<h1>MBSE AI System</h1><p>Frontend not found. API docs: <a href='/docs'>/docs</a></p>")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=config.HOST, port=config.PORT)
