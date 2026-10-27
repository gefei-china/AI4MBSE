"""MCP-D1：MCP Server 健康巡检与动态发现核心逻辑。

- check_server(srv)：单服务器轻量探测（initialize 握手），复用 StudioRepo 落库状态/延迟/错误
- discover_server(srv)：全量动态发现（initialize + tools/list + resources/list + prompts/list）
- 后台巡检：start_health_loop() 启动 daemon 线程，按 interval_sec 轮询全部 sse/http 服务器

安全边界：仅对 HTTP(S) endpoint 发起 JSON-RPC 请求；stdio 不自动拉起子进程。
"""
import json
import threading
import time

#: 机会式 tool_meta 回填的冷却窗口（秒）。`check_all` 对「online 但 tool_meta 为空」的
#: server 补跑一次 discover；失败后 1 小时内不再重试 ⇒ 不放大故障、又保证最终会补上。
#: 成功回填后 `tool_meta` 非空 ⇒ 触发条件自然失效（数据自驱，无需额外状态）。
_BACKFILL_COOLDOWN = 3600.0
_BACKFILL_TRY: dict = {}      # {server_id: 上次尝试的 time.time()}


def _save_tool_meta(tools: list) -> str:
    """把 `list_tools()` 的 description/inputSchema 序列化（延迟导入，避免循环依赖）。"""
    try:
        from agent.mcp_tool_meta import save_tool_meta
        return save_tool_meta(tools)
    except Exception:
        try:
            return json.dumps([
                {"name": str(t.get("name") or ""),
                 "description": str(t.get("description") or "")[:600],
                 "inputSchema": t.get("inputSchema") or {}}
                for t in (tools or []) if isinstance(t, dict) and t.get("name")
            ], ensure_ascii=False)
        except Exception:
            return ""


def _norm_transport(t: str | None) -> str:
    """传输方式归一（★ B5-4：前端 `streamable_http` vs 后端 `http` 三方枚举不一致）。

    本模块有 **4 处**用 `transport in ("sse","http")` 做分支判断，
    而前端下拉给的默认值是 `streamable_http` ⇒ 这些判断会全部走不进来
    （表现为"界面配好了，健康巡检/动态发现却说不自动拉起"）。
    归一后与 `MCPClient.__init__` 用同一张映射表，避免两处各写一份。
    ⚠️ 兜底分支也**统一小写**：库里若存了 `'SSE'`，不归一就会在
    `in ("sse","http")` 处落空（大小写敏感）—— 同"枚举不一致"族的第4种表现。
    """
    return {"streamable_http": "http", "streamablehttp": "http",
            "https": "http", "": "http"}.get((t or "").strip().lower(),
                                             (t or "sse").strip().lower())


def _make_client(srv: dict):
    from mcp_client import MCPClient
    # ★ B5-2：带上认证头（否则健康探测对需认证的 MCP 永远 offline）
    _h = {}
    try:
        import json as _j
        _h = _j.loads(srv.get("headers") or "{}") or {}
    except Exception:
        _h = {}
    return MCPClient(
        endpoint=srv.get("endpoint", ""),
        transport=_norm_transport(srv.get("transport")),
        timeout=int(srv.get("timeout") or 15),
        headers=_h if isinstance(_h, dict) else {},
    )


def check_server(srv: dict, repo=None):
    """轻量健康探测：initialize 握手 → 更新状态/延迟/last_error。返回 (ok, detail)。"""
    import time as _t
    t0 = _t.time()
    transport = _norm_transport(srv.get("transport"))
    sid = srv["id"]
    if transport in ("sse", "http"):
        try:
            client = _make_client(srv)
            client.initialize(force=True)
            latency = int((_t.time() - t0) * 1000)
            if repo is not None:
                repo.update_mcp_status(sid, "online", latency)
            return True, {"status": "online", "latency_ms": latency,
                          "protocol": client.protocol_version}
        except Exception as e:
            latency = int((_t.time() - t0) * 1000)
            if repo is not None:
                repo.update_mcp_error(sid, f"{type(e).__name__}: {str(e)[:150]}")
            return False, {"status": "offline", "latency_ms": latency, "error": str(e)[:200]}
    return False, {"status": "unknown", "note": f"transport {transport} 仅校验命令不自动拉起"}


def discover_server(srv: dict, repo=None):
    """全量动态发现：initialize + 三原语 list → 落库。返回结果 dict。"""
    t0 = time.time()
    transport = _norm_transport(srv.get("transport"))
    sid = srv["id"]
    if transport not in ("sse", "http"):
        note = f"transport {transport} 不自动拉起子进程，跳过动态发现"
        return {"ok": False, "note": note}
    try:
        client = _make_client(srv)
        handshake = client.initialize(force=True)
        tools = client.list_tools()
        resources, prompts = [], []
        # 按能力声明按需拉取（MCP 规范：capabilities 中以键存在即表示支持，值可为空 dict）
        caps = client.capabilities or {}
        if "resources" in caps:
            try:
                resources = client.list_resources()
            except Exception:
                resources = []
        if "prompts" in caps:
            try:
                prompts = client.list_prompts()
            except Exception:
                prompts = []
        latency = int((time.time() - t0) * 1000)
        if repo is not None:
            repo.update_mcp_discovery(
                sid, "online", latency,
                json.dumps([t.get("name") for t in tools], ensure_ascii=False),
                json.dumps(caps, ensure_ascii=False),
                handshake.get("protocolVersion", ""),
                json.dumps(handshake.get("serverInfo", {}), ensure_ascii=False),
                json.dumps(resources, ensure_ascii=False),
                json.dumps(prompts, ensure_ascii=False),
                # ★ P0-6（2026-10-26）：**同时存下 description 与 inputSchema**。
                #   此前只存 name（上面那行）⇒ 落库即丢 schema ⇒ 注入时每个 MCP 工具的
                #   description 退化成同一句模板、parameters 退化成宽松空壳
                #   （实测 71 个工具 description 去重后仅 1 种）⇒
                #     ① JIT 语义预筛零区分度（不得不把 MCP 工具列入 core_keep 禁裁剪）
                #     ② 模型不知道工具能做什么、参数怎么传
                #   `mcp_client.list_tools()` 本就返回 [{name, description, inputSchema}]，
                #   这次把丢掉的东西存回来。`tools` 字段保持原样（既有消费点不受影响）。
                tool_meta=_save_tool_meta(tools),
            )
        return {
            "ok": True, "status": "online", "latency_ms": latency,
            "protocol": handshake.get("protocolVersion", ""),
            "server_info": handshake.get("serverInfo", {}),
            "capabilities": caps,
            "tools": [t.get("name") for t in tools],
            "resources": resources,
            "prompts": prompts,
        }
    except Exception as e:
        latency = int((time.time() - t0) * 1000)
        if repo is not None:
            repo.update_mcp_error(sid, f"{type(e).__name__}: {str(e)[:150]}")
        return {"ok": False, "status": "offline", "latency_ms": latency, "error": str(e)[:200]}


def check_all(repo):
    """批量巡检全部 sse/http 服务器。返回汇总。

    ★ 2026-10-16：结尾**自提交**。BaseRepo.execute 只执行 SQL 不提交，而本函数的
    两个调用方提交时机不同（后台线程 / Depends(db_session)），漏一层就白跑 ——
    实测现象：巡检跑通、日志无异常，但库里 status/last_check 永远停在旧值。
    `db_session` 路径的 commit 是幂等空操作，不会重复提交。

    ★ P0-6（2026-10-26）：额外做**机会式 tool_meta 回填**（见下方 inline 注释）。
      返回值新增 `backfilled`（本轮成功回填的 server 数）与 `details[*].tool_meta_backfilled`。
    """
    results = {"checked": 0, "online": 0, "offline": 0, "backfilled": 0, "details": []}
    for srv in repo.rows("SELECT * FROM mcp_servers"):
        if _norm_transport(srv.get("transport")) not in ("sse", "http"):
            continue
        ok, detail = check_server(srv, repo=repo)
        results["checked"] += 1
        results["online" if ok else "offline"] += 1
        # ★ P0-6（2026-10-26）：**机会式回填 tool_meta**。
        #   背景：`check_server` 只做 initialize 握手，**不调 tools/list**
        #   ⇒ 本列新增后，存量 server 的 tool_meta 恒为空 ⇒ 注入侧
        #     永远走"无元数据"分支（全量 71 个 + 模板描述）⇒ 本次改动**表面生效实际空转**。
        #   这正是本仓反复吃亏的形态（"看起来绿但没干活"，见 MEMORY.md）。
        #   策略：**仅当** ①本次探测 online ②tool_meta 为空 ③传输可自动拉起 时，
        #   补跑一次完整 discover。成功 ⇒ tool_meta 非空 ⇒ 后续不再触发（数据自驱、无状态）。
        #   ⚠️ 失败时 tool_meta 仍为空 ⇒ 若不加冷却会**每轮巡检都打一次 server**
        #     （默认 5 分钟一次，对一个持续 tools/list 报错的 server 是长期噪声）⇒
        #     加 1 小时冷却窗口，既保证"最终会补上"，又不放大故障。
        _sid = srv["id"]
        if (ok and not (srv.get("tool_meta") or "").strip()
                and (time.time() - _BACKFILL_TRY.get(_sid, 0.0) >= _BACKFILL_COOLDOWN)):
            _BACKFILL_TRY[_sid] = time.time()
            try:
                _dr = discover_server(srv, repo=repo)
                if _dr.get("ok"):
                    results["backfilled"] += 1
                    detail["tool_meta_backfilled"] = True
                    print(f"[mcp-health] 已回填 {srv.get('name','')} 的工具元数据"
                          f"（{len(_dr.get('tools') or [])} 个工具：description + inputSchema）",
                          flush=True)
                else:
                    detail["tool_meta_backfilled"] = False
                    detail["backfill_error"] = str(_dr.get("error") or _dr.get("note") or "")[:150]
            except Exception as e:
                # 回填失败**不阻断巡检**，但必须留痕（否则又是一次静默空转）
                detail["tool_meta_backfilled"] = False
                detail["backfill_error"] = f"{type(e).__name__}: {str(e)[:120]}"
                print(f"[mcp-health] 回填工具元数据失败 {srv.get('name','')}："
                      f"{type(e).__name__}: {str(e)[:120]}", flush=True)
        detail["id"] = srv["id"]
        detail["name"] = srv.get("name", "")
        results["details"].append(detail)
    try:
        conn = getattr(repo, "conn", None)
        if conn is not None and hasattr(conn, "commit"):
            conn.commit()
    except Exception as e:
        # ★ 2026-10-16：提交失败**绝不静默**。实测教训：某调用方在自己连接上写了却不提交
        #   （未提交事务持有 SQLite 写锁），本函数的 commit 被阻塞到超时；
        #   旧写法 `except: pass` 让"巡检跑通了但库里没更新"再次隐身 ——
        #   这与本次要修的「factory 传错 + 不 commit」是同一族故障。
        #   details 仍照常返回（调用方能看到探测结果），但落库失败必须留痕。
        print(f"[mcp-health] 巡检结果落库失败（commit 被阻塞？探测结果未持久化）："
              f"{type(e).__name__}: {str(e)[:150]}", flush=True)
    return results


def start_health_loop(repo_factory, interval_sec: float = 300.0):
    """启动后台健康巡检 daemon 线程（main.py startup 调用）。

    ★ 2026-10-16：`_loop` 的 `except Exception: pass` 是本次「MCP 不可用」事故的
    **放大器** —— 单是它不会造成故障，但它把 main.py 传错 factory 的 AttributeError
    变成**完全无声**：服务正常启动、日志无一行异常，而所有 MCP 的 status/last_check
    永远停在旧值（实测停在 2026-10-14 08:25）。这正是「静默兜底把错误变成不可见」的
    教科书形态（见 MEMORY.md「带兜底的函数最危险」）。
    现改为：**首圈失败必打印**（暴露启动即坏），同因连续失败按指数退避+定期重报，
    不同错误立即报 —— 既不刷屏，也不让任何一类错误永久隐身。
    """
    _state = {"err": None, "n": 0, "quiet_until": 0.0}

    def _report(e: BaseException, where: str) -> None:
        sig = f"{type(e).__name__}: {str(e)[:120]}"
        now = time.time()
        first = _state["err"] is None
        changed = _state["err"] != sig
        if first or changed or now >= _state["quiet_until"]:
            _state["err"], _state["n"], _state["quiet_until"] = sig, 0, now + 600.0
            print(f"[mcp-health] {where} 失败 {sig}（巡检将继续重试）", flush=True)
        else:
            _state["n"] += 1

    def _loop():
        while True:
            try:
                conn = repo_factory()
                try:
                    check_all(conn)
                    # ★ 2026-10-16：**必须 commit**。BaseRepo.execute 只执行 SQL 不提交，
                    #   请求路径靠 Depends(db_session) 结束时 commit；后台线程没有这层包装，
                    #   不显式 commit ⇒ 写完 status 就 close ⇒ 未提交事务被回滚
                    #   ⇒ 库里 status/last_check **永远不更新**（巡检白跑）。
                    #   这是「MCP 不可用」的第二根因：修好 factory 只够跑通，不 commit 仍不落库。
                    if hasattr(conn, "commit"):
                        conn.commit()
                finally:
                    # 关掉每轮新开的连接：不关 → 连接/游标泄漏（sqlite 文件句柄耗尽）
                    close = getattr(conn, "close", None)
                    if callable(close):
                        try:
                            close()
                        except Exception:
                            pass
                _state.update({"err": None, "n": 0})
            except Exception as e:
                _report(e, "巡检")
            time.sleep(interval_sec)

    t = threading.Thread(target=_loop, name="mcp-health-loop", daemon=True)
    t.start()
    return t
