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
from core import runtime_state
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
    jobs_router,           # 2026-10-04 P0-C 异步作业队列（长任务提交即返回 job_id）
    proposals_router,      # 2026-10-18 P1-3 配置提案（失败轨迹→修订提议，只提议不自动应用）
    curriculum_router,     # 2026-10-19 P2-2 失败驱动任务提案（EnvBuilder 式自造课程，只提议不自动执行）
    agent_archive_router,  # 2026-10-19 P2-3 Agent 档案库/种群（变体台账 + 死路记忆，只记录不切换生效版本）
    flow_topology_router,  # 2026-10-20 P2-4 画布拓扑台账（结构体检 + 同构去重，只读不改拓扑）
    evolution_report_router,  # 2026-10-20 自进化周报（回答「这周系统替我做了什么、我该做什么」）
    evaluation_router,        # 2026-10-22 评测管理（评测历史 / 趋势 / 可信度 / 门禁接线）
    diagnostics_router,       # 2026-10-26 P0-8 上下文归因只读诊断（让 token 构成可见）
    capabilities_router,      # 2026-10-27 P0-2 能力账本（统一注册处 + 声明/调用 2×2 + 任务级追溯）
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
    # ★ 2026-10-30（P0-2）：默认**开**，但配了三道成本闸（单轮 1 批 / 每日 5 次 /
    #   不重跑 failed 任务）。开关口径见 config.orchestration.auto_resume_*。
    try:
        if config.as_bool("orchestration", "auto_resume_enabled", True):
            from core.orch_supervisor import start_resume_loop
            start_resume_loop(
                interval_sec=float(config.get("orchestration", "auto_resume_interval_s", 300)),
                max_runs=int(config.get("orchestration", "auto_resume_max_runs", 1)),
                stale_s=int(config.get("orchestration", "resume_stale_s", 1800)),
                include_failed=bool(config.get("orchestration", "auto_resume_include_failed", False)),
            )
        else:
            print("[startup] 编排自动重投守护未启用（orchestration.auto_resume_enabled=off，"
                  "崩溃批次需人工点继续）", flush=True)
    except Exception as _e:
        print("[startup] 自动重投守护跳过：%s" % str(_e)[:120], flush=True)

    # ── P0-C（2026-10-04）：异步作业队列 worker 守护 ──
    # 为什么默认**开**：与 orch_supervisor 的auto_resume 刻意相反。
    #   orch_supervisor 会**自己决定**重投哪些编排批次 → 会真的调 LLM 烧额度，故默认关；
    #   job worker 只执行**用户显式提交**的作业（有人点了上传/入库才排队），
    #   无人提交时空转（每轮一次 SELECT，成本可忽略）⇒ 默认开才是能力可用。
    # 关掉它（jobs.worker_enabled=false）的唯一合理场景：单机排障，
    # 此时提交仍会成功（作业留在 queued），只是没人执行 —— 端点会把这点明示出来。
    try:
        from core import job_queue as _jq
        from core.job_handlers import register_all as _reg_jobs
        _kinds = _reg_jobs()
        if config.as_bool("jobs", "worker_enabled", True):
            _n = _jq.start_workers(
                count=int(config.get("jobs", "worker_count", 1) or 1),
                interval_s=float(config.get("jobs", "worker_interval_s", 2)),
                lease_s=int(config.get("jobs", "worker_lease_s", 900)))
            print("[startup] 作业队列 worker 已启动：%d 个 / 已注册 %d 种作业（%s）"
                  % (_n, len(_kinds), ",".join(_kinds)), flush=True)
        else:
            print("[startup] 作业队列 worker 未启用（jobs.worker_enabled=off）——"
                  "提交仍会成功但作业留在 queued 无人执行；已注册 %d 种作业" % len(_kinds),
                  flush=True)
    except Exception as _e:
        print("[startup] 作业队列 worker 启动失败（长任务将只能同步跑）：%s" % str(_e)[:160],
              flush=True)

    # MCP-D1：后台健康巡检线程（每 5 分钟对 sse/http 服务器做 initialize 握手探测）
    try:
        from mcp_health import start_health_loop
        from repositories.studio_repo import StudioRepo as _StudioRepo
        # ★ 2026-10-16（米爸报「MCP 为啥不可用」· 已定位）：factory 必须给 **repo 对象**，
        #   不是裸连接。改前写 `lambda: get_db()` —— get_db() 返回 sqlite3.Connection，
        #   而 check_all() 要调 `repo.rows(...)` / `repo.update_mcp_status(...)`
        #   ⇒ 每轮 AttributeError，被 _loop 的 `except: pass` 吞掉
        #   ⇒ **巡检自服务启动起从未成功执行过一次**，全部 MCP 的 last_check 停在
        #      2026-10-14 08:25（那次是手动「测试」按钮走的 StudioRepo 正确路径）。
        #   后果：zhiyuan-dev-mcp 实测能连通（online 2551ms），库里却僵尸 offline；
        #   而 `AgentRepo.bound_tools_for()` 对 mcp 要求 `status=='online' and enabled`
        #   ⇒ 绑了也取不到 ⇒「MCP 不可用」的假象。
        #   ⚠️ `db_session` 依赖注入返回的是 conn，本处与之同源，勿再改回裸连接。
        start_health_loop(lambda: _StudioRepo(get_db()),
                          interval_sec=float(config.get("mcp", "health_interval", 300)))
        print("[startup] MCP 健康巡检线程已启动（间隔 %ss）"
              % config.get("mcp", "health_interval", 300), flush=True)
    except Exception as _e:
        # 静默 pass 会让「巡检没跑」完全不可见 —— 这条链上已栽过一次（见上方注释）。
        print("[startup] MCP 健康巡检启动失败（MCP 状态将停滞，绑定的 MCP 可能取不到）："
              "%s: %s" % (type(_e).__name__, str(_e)[:160]), flush=True)
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
    # ── 2026-10-30：运行时状态初始化（优雅关闭 / 并发闸 / 墙钟预算的配置快照）──
    try:
        _rs = runtime_state.init()
        print("[startup] 运行时状态就绪：并发上限 %s 路 / 排队 %ss / 单轮墙钟 %ss / 关闭排空 %ss"
              % (_rs["max_concurrent_streams"], _rs["queue_wait_s"],
                 _rs["chat_deadline_s"], _rs["shutdown_drain_s"]), flush=True)
    except Exception as _e:
        print("[startup] 运行时状态初始化失败（退化用默认值）：%s" % str(_e)[:120], flush=True)

    yield

    # ══════════════════════════════════════════════════════════════════════
    # 关闭收尾（2026-10-30 新增 —— 此前 `yield` 之后**一行代码都没有**）
    # ══════════════════════════════════════════════════════════════════════
    # 为什么必须有（《运行时评估》缺口①）：
    #   改前 uvicorn 收到 SIGTERM 即退出，而 SSE 的 worker 线程是 `daemon=True`
    #   ⇒ 进程消失时在途编排被硬杀，`route_persist_gone` 兜底**根本不会执行**
    #   ⇒ 用户看到"跑到一半没了，刷新只看到自己的输入"（conv 9754 就是这么丢的）。
    #
    # 三个设计取舍：
    #   ① **先置 draining 再做任何等待**：入口端点据此立即拒新请求，
    #      否则"边收尾边接新活"会让 in-flight 计数永远归不了零；
    #   ② **等待是有界的**（`runtime.shutdown_drain_s`，默认 30s）：行业阈值区间
    #      20~45s，超过 45s 容易被上层（K8s gracePeriod / nginx proxy_read_timeout）
    #      直接 SIGKILL，反而连收尾都做不完；
    #   ③ **排空不可能等到长任务跑完**（实测批次 avg 918s）—— 所以这里只是
    #      "给正在收尾的流一点时间"，真正的产出保全靠各自 deadline 与断点固化。
    #      行业原文："if you have endpoints that run longer than 10 seconds,
    #      the problem can't be solved by increasing the timeout"。
    import asyncio as _asyncio
    import time as _time

    _drain_budget = float(runtime_state._cfg("shutdown_drain_s") or 30)
    _inflight_at_signal = runtime_state.inflight_count()
    runtime_state.begin_drain()
    print("[shutdown] 收到关闭信号：新请求将被拒；在途 %d 路流，最多等 %.0fs"
          % (_inflight_at_signal, _drain_budget), flush=True)
    _t0 = _time.monotonic()
    while runtime_state.inflight_count() > 0 and (_time.monotonic() - _t0) < _drain_budget:
        await _asyncio.sleep(0.2)
    _left = runtime_state.inflight_count()
    _waited = _time.monotonic() - _t0
    if _left:
        print("[shutdown] ⚠️ 排空预算耗尽，仍有 %d 路流未结束（已等 %.1fs）—— "
              "它们靠各自 deadline 兜底固化，本进程即将退出（未固化部分依赖 "
              "`route_persist_gone`，若进程被 SIGKILL 则该兜底不执行）"
              % (_left, _waited), flush=True)
    else:
        print("[shutdown] 在途流已全部结束（%.1fs）" % _waited, flush=True)

    # ── 停后台守护线程 ────────────────────────────────────────────────
    # 顺序：先停"会产生新工作"的（作业队列），再停"只做观测"的（告警、续跑守护）。
    # 每个独立 try：某个停不掉不能阻断其它收尾（收尾阶段最忌讳"一处异常全停"）。
    try:
        from core import job_queue as _jq
        _jq.stop_workers()
        print("[shutdown] 作业队列 worker 已停止", flush=True)
    except Exception as _e:
        print("[shutdown] 作业队列停止失败（忽略）：%s" % str(_e)[:100], flush=True)
    try:
        from core import alert_evaluator as _ae
        _ae.stop_alert_loop()
        print("[shutdown] 告警评估器已停止", flush=True)
    except Exception as _e:
        print("[shutdown] 告警评估器停止失败（忽略）：%s" % str(_e)[:100], flush=True)
    try:
        from core import orch_supervisor as _osv
        _osv.stop_resume_loop()
        print("[shutdown] 编排续跑守护已停止", flush=True)
    except Exception as _e:
        print("[shutdown] 编排续跑守护停止失败（忽略）：%s" % str(_e)[:100], flush=True)
    # MCP 健康巡检线程无 stop 入口（`mcp_health.start_health_loop` 只起不停）——
    # 它是 daemon 线程且只读库、不发外部调用以外的副作用，进程退出即随之消失，
    # 故此处不做处理（避免为了一致性去改一个与本次目标无关的模块）。


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
    jobs_router,  # 2026-10-04 P0-C 异步作业队列（长任务提交即返回 job_id）
    proposals_router,  # 2026-10-18 P1-3 配置提案（失败轨迹→修订提议，只提议不自动应用）
    curriculum_router,  # 2026-10-19 P2-2 失败驱动任务提案（失败证据→该练什么，只提议不自动执行）
    agent_archive_router,  # 2026-10-19 P2-3 Agent 档案库/种群（记住死路，避免重走；只记录不切换生效版本）
    flow_topology_router,  # 2026-10-20 P2-4 画布拓扑台账（记住结构缺陷与同构冗余；只读不改拓扑）
    evolution_report_router,  # 2026-10-20 自进化周报（回答「这周系统替我做了什么、我该做什么」）
    evaluation_router,        # 2026-10-22 评测管理（评测历史 / 趋势 / 可信度 / 门禁接线）
    diagnostics_router,       # 2026-10-26 P0-8 上下文归因只读诊断
    capabilities_router,      # 2026-10-27 P0-2 能力账本（all read-only）
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


# ── 2026-10-30：健康检查（liveness / readiness 必须分开）────────────────────
# 为什么不能合并成一个端点（行业共识，不是风格问题）：
#   关闭过程中需要「`/ready` 返 503 让负载均衡摘流量」而「`/live` 仍 200 让进程别被杀」。
#   合并 ⇒ 要摘流量就得让 liveness 也失败 ⇒ 编排器直接重启进程 ⇒ 在途编排被硬杀。
@app.get("/api/health/live")
def health_live():
    """存活探针：进程与事件循环还活着就 200（不依赖 DB / LLM / 任何外部资源）。"""
    return {"status": "alive", "draining": runtime_state.is_draining()}


@app.get("/api/health/ready")
def health_ready():
    """就绪探针：关闭中返 503（让上游摘流量），否则 200。

    ⚠️ 刻意**不**检查 DB/LLM 可用性 —— 就绪探针判"能否接流量"，
    依赖健康属于另一条链路（`/api/dashboard` 与告警评估器）。
    把依赖探进 readiness 会导致"LLM 抖动 → 全体摘流量 → 雪崩"。
    """
    from fastapi.responses import JSONResponse as _JR
    if runtime_state.is_draining():
        return _JR(status_code=503, content={"status": "draining",
                                            "inflight": runtime_state.inflight_count(),
                                            "elapsed_s": round(runtime_state.drain_elapsed(), 1)})
    return {"status": "ready", **runtime_state.stats()}


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
    # ★ 2026-10-30（P0 缺口①）：`timeout_graceful_shutdown` **必须显式给**。
    #   改前调用是 `uvicorn.run(app, host=..., port=...)` —— 不设该参数时 uvicorn
    #   收到 SIGTERM 会**立即**退出，不等待在途请求 ⇒ 长任务被硬杀、断点固化来不及跑。
    #   （行业原文："Without an explicit parameter it will stop the process right after
    #    receiving SIGTERM, without waiting for current requests to finish."）
    #   取值与 lifespan 的排空预算同源（`runtime.shutdown_drain_s`，默认 30s），
    #   避免两处口径漂移导致"uvicorn 先杀、lifespan 还在等"。
    try:
        _drain_s = int(runtime_state._cfg("shutdown_drain_s") or 30)
    except Exception:
        _drain_s = 30
    uvicorn.run(app, host=config.HOST, port=config.PORT,
                timeout_graceful_shutdown=_drain_s)
