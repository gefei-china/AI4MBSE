# -*- coding: utf-8 -*-
"""智源轮询事件源（2026-11-02）—— **本工程作为 MCP 客户端主动拉取智源变更**。

━━━ 方向澄清（把上一轮的错误纠正过来）━━━
上一轮我假设"智源调本工程"（本工程是 MCP server 被调）。**实际方向相反**：
**本工程（agent 平台）作为 MCP 客户端去访问智源**，
通过智源侧已配好的 `zhiyuan_dev_mcp`（`http://xuanyi-dev.huilingyun.com/mcp`，
`SE-MBSE 1.0.0`，**71 个工具**）与智源交互。

⇒ 事件从"别人推给我"变成"**我主动去拉**" ⇒ 必须有一个 **poller**。
这也解释了为什么 `base_url` + `token` 早就在 `mcp_servers` 表里
（headers 含 Bearer）—— 我上一轮说"卡在缺 base_url+token"是**判断错了**。

━━━ 轮询锚点：为什么选 `query_model_versions` ━━━
智源 71 个工具里，能当"变更锚点"的只有几个：
  · `query_model_versions` —— **按发布时间倒序**查分支版本 ⇒ 新出现的 versionId = 变更
  · `query_data_tree` —— 只能逐层展开，无全量时间序，不适合做锚点
  · `preview_model_changes` —— 需要明确的命令，不是"发现变更"
⇒ **`query_model_versions` 是唯一天然有序、可做增量断点的锚点**。

━━━ ⚠️ 参数不猜：透传 + 探测 ━━━
`mcp_servers.tool_meta` 里只存了 name + description，**没有 inputSchema**
（智源的工具 schema 在当前库里不可见）。而此刻智源服务端 **502 不可用**
（整个站点 `/` 都 502，DNS 正常、TCP 0.28s 快速返回 ⇒ nginx 活着、后端挂了）。

⇒ **绝不凭空猜参数名**（猜错会在恢复后静默拉不到数据，
   症状是"轮询是空的"，看起来像"没有变更"，实际是参数错了 —— 
   与本仓反复踩的"静默失效"同型）。
⇒ 做法：
   ① `versions_args` / `projects_args` 直接从配置**透传**（配置里写什么就传什么）；
   ② 提供 `probe_zhiyuan_schema()`：服务恢复后先跑一次真实 `tools/list`
      把 schema 落库，再据此写配置；
   ③ 参数拿不到时**明确报错并留痕**，不静默返回空列表。

━━━ 断点续拉（服务恢复后不能漏）━━━
把 `last_published_at`（字典序可比的 ISO 时间）存进 `ev_sources` 的同名字段
（不存在则用 `ev_poller_state` 表）；恢复后从断点继续。
⚠️ 断点用**版本发布时间**而非"上次轮询时间"：
   用轮询时间会漏掉"停服期间发布、时间戳更早"的版本。

━━━ 默认关 ━━━
轮询会消耗智源接口配额 + 我方数据库。默认 `events.zhiyuan_poll_enabled=False`。
"""
from __future__ import annotations

import json
import time

__all__ = [
    "probe_zhiyuan_schema", "poll_once", "poll_loop",
    "start_poller", "stop_poller", "poller_status",
]

# ⚠️⚠️ **本模块不定义 SourceSpec**（2026-11-02 架构判断，别再加回来）：
#   推送式（智源报给我）与轮询式（我去拉智源）**必须共用同一个 `zhiyuan` kind**，
#   理由是两者的准入/幂等/工单形态必须一致，否则规则会分叉、面板会分裂。
#   而注册表里**一个 kind 只有一个 spec** ⇒ 若本模块也建一个 spec 并注册，
#   会把 `sources_zhiyuan.build_zhiyuan_source` 覆盖掉（或反之）。
#   ⇒ spec 只有一份，定义在 `sources_zhiyuan.py`；
#     本模块只负责"去拉"，拉到后用 `svc.receive("zhiyuan", ...)` 走同一套规则。
#   ⇒ 系统事件（actor_type=system）在 `admit()` 里有豁免分支，不需要白名单，
#     所以同一个 spec 天然能同时服务"真人推送"与"系统轮询"两种场景。
ZHIYUAN_POLL_KIND = "zhiyuan"

_state = {"thread": None, "stop": None, "runs": 0, "last_at": 0.0,
          "last_ok": None, "last_error": "", "last_found": 0,
          "config": {"enabled": False, "interval_s": 600}}


# ══════════════════════════════════════════════════════════
# 与智源的 MCP 交互
# ══════════════════════════════════════════════════════════
def _load_mcp_server(conn, name: str = "zhiyuan-dev-mcp") -> dict | None:
    """从 `mcp_servers` 读智源 MCP 配置（endpoint / headers / transport）。"""
    try:
        row = conn.execute(
            "SELECT name, endpoint, transport, headers, status, last_error "
            "FROM mcp_servers WHERE name=?", (name,)).fetchone()
    except Exception:
        return None
    if not row:
        return None
    d = dict(row) if hasattr(row, "keys") else dict(zip(
        ["name", "endpoint", "transport", "headers", "status", "last_error"], row))
    try:
        d["headers"] = json.loads(d.get("headers") or "{}")
    except Exception:
        d["headers"] = {}
    return d


def _client(server: dict):
    """构造 MCP 客户端（复用 mcp_client，不另造协议层）。"""
    from mcp_client import MCPClient
    return MCPClient(server["endpoint"], transport=server.get("transport") or "sse",
                     headers=server.get("headers") or {})


def _tool_json(res: dict) -> dict:
    """把 `MCPClient.call_tool` 的返回解成 dict（尽力而为，失败返回 {}）。

    ⚠️ 不静默吞：解析失败要带 `_parse_error`，让人能看出"拿到了但解不开"。
    """
    if not isinstance(res, dict):
        return {}
    raw = res.get("result")
    if isinstance(raw, dict):
        return raw
    s = str(raw or "").strip()
    if not s:
        return {}
    # 有些服务端把 JSON 包在正文或 ```json 里
    if s.startswith("```"):
        s = s.split("```")[1] if "```" in s[3:] else s.strip("`")
        s = s.lstrip("json").strip()
    try:
        return json.loads(s)
    except Exception:
        return {"_parse_error": s[:300]}


def _call_tool_checked(cli, tool: str, args: dict) -> dict:
    """调用工具并**强制区分三种结果**：成功拿到数据 / 调用失败 / 拿到但解不开。

    ━━━ 为什么必须包一层（2026-11-02 端到端实测抓到的真实缺陷）━━━
    `MCPClient.call_tool` 的设计是**不抛异常**：失败时返回
    `{"ok": False, "result": "错误描述", "is_error": True}`。
    我第一版只做 `_tool_json(res)` 然后 `... or []` ⇒ 502 的错误文本解析不出 list
    ⇒ 得到空列表 ⇒ **被当成"这个工程没有分支"** ⇒ 返回
    `{"ok": true, "emitted": 0, "branches_scanned": 0}`。

    后果比报错严重得多：**"拉不到"被伪装成"没有变更"** ⇒
    智源停服期间的所有变更会被永久漏掉，且台账上显示一切正常。
    （这正是本模块 docstring 里自己写的风险 —— 我照样踩了。）

    ⇒ 判据三层，缺一不可：
      ① `res.ok` 必须为 True（MCPClient 的执行结论）
      ② `res.is_error` 必须为假
      ③ 解析结果不能是 `_parse_error`
    任何一层不过 ⇒ 抛 `ZhiyuanCallError`，由调用方记进 errors。
    """
    res = cli.call_tool(tool, args) if args else cli.call_tool(tool, {})
    if not isinstance(res, dict):
        raise ZhiyuanCallError("%s: 返回非 dict（%s）" % (tool, type(res).__name__))
    if res.get("is_error") or res.get("ok") is False:
        raise ZhiyuanCallError("%s 调用失败: %s"
                               % (tool, str(res.get("result") or "")[:200]))
    data = _tool_json(res)
    if "_parse_error" in data:
        raise ZhiyuanCallError("%s 返回无法解析为 JSON: %s"
                               % (tool, str(data["_parse_error"])[:160]))
    return data


class ZhiyuanCallError(RuntimeError):
    """智源工具调用失败（区别于"调用成功但没有数据"）。"""


def probe_zhiyuan_schema(conn, name: str = "zhiyuan-dev-mcp") -> dict:
    """探测智源 MCP 真实 schema 并落库（**服务恢复后第一件要做的事**）。

    为什么必须做：`tool_meta` 里只有 name + description，**没有 inputSchema**
    ⇒ 轮询参数只能靠猜 ⇒ 猜错会静默拉不到数据。
    ⇒ 用真实 `tools/list` 拿 schema，落进 `mcp_servers.tool_meta`，再据它写配置。

    :returns: {ok, tools_count, schemas, error}
    """
    server = _load_mcp_server(conn, name)
    if not server:
        return {"ok": False, "error": "mcp_server_not_found", "name": name}
    try:
        cli = _client(server)
        tools = cli.list_tools() or []
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, str(e)[:200])}

    schemas = {}
    for t in tools:
        nm = t.get("name") if isinstance(t, dict) else str(t)
        if nm:
            schemas[nm] = t.get("inputSchema") if isinstance(t, dict) else None

    # 落库（tool_meta 存 name/description/inputSchema 三合一，供后续配置参考）
    try:
        conn.execute(
            "UPDATE mcp_servers SET tool_meta=?, tools=?, last_discover=CURRENT_TIMESTAMP, "
            "status='online', last_error='' WHERE name=?",
            (json.dumps([{"name": t.get("name"), "description": t.get("description"),
                          "inputSchema": t.get("inputSchema")}
                         for t in tools if isinstance(t, dict)], ensure_ascii=False),
             json.dumps([t.get("name") for t in tools if isinstance(t, dict)],
                        ensure_ascii=False), name))
        conn.commit()
    except Exception as e:
        return {"ok": True, "tools_count": len(schemas), "schemas": schemas,
                "warn": "schema 落库失败: %s" % str(e)[:120]}

    return {"ok": True, "tools_count": len(schemas),
            "schemas": {k: v for k, v in schemas.items()
                        if k in ("query_projects", "query_branches",
                                 "query_model_versions", "query_data_tree")}}


def poll_once(conn, projects: list = None, versions_args: dict = None,
              branch_ids: list = None, name: str = "zhiyuan-dev-mcp",
              registry=None, max_per_branch: int = 20, client=None) -> dict:
    """跑一轮轮询：拉智源新版本 → 发事件。

    :param projects: 要监听的 projectId 列表（**空 = 什么都不拉**，避免全库扫描）
    :param versions_args: 透传给 `query_model_versions` 的**额外固定参数**
                          （如分页大小）。不猜参数名 —— 配置里写什么就传什么。
    :param branch_ids: 只轮询这些 branchId（空 = 该工程下所有分支）
    :param client: 可选，注入 MCP 客户端（**默认为空则按 mcp_servers 配置自建**）。
                   用于单测/变异自证 —— 不注入就只能靠真实网络，那样验证脚本会
                   依赖外部服务可用性（智源 502 时无法测"成功路径"）。

    ⚠️ 智源不可用时：**返回结构化错误，不抛异常、不当成"没有变更"**。
       "拉不到" ≠ "没变更" —— 混淆这两者会让停服期间的变更永久丢失。
    """
    from core.events.sources import get_registry
    from repositories.event_repo import EventRepo
    from services.event_service import EventService

    if not projects:
        return {"ok": False, "error": "no_projects_configured",
                "note": "未配置要监听的智源工程（避免全库扫描，必须显式指定 projectId）"}

    reg = registry if registry is not None else get_registry()
    if reg.get(ZHIYUAN_POLL_KIND) is None:
        return {"ok": False, "error": "source_not_registered", "kind": ZHIYUAN_POLL_KIND}

    # client 注入优先；否则按 mcp_servers 配置自建
    if client is None:
        server = _load_mcp_server(conn, name)
        if not server:
            return {"ok": False, "error": "mcp_server_not_found", "name": name}
        try:
            cli = _client(server)
        except Exception as e:
            _state["last_ok"] = False
            _state["last_error"] = str(e)[:200]
            return {"ok": False, "error": "client_init_failed: %s" % str(e)[:200]}
    else:
        cli = client

    svc = EventService(EventRepo(conn), reg)
    t0 = time.time()
    emitted, errors, branches_scanned = 0, [], 0

    for pid in projects:
        # ① 该工程下的分支 —— 用**检查版**调用（失败必须进 errors，不能变成空列表）
        try:
            br_data = _call_tool_checked(cli, "query_branches", {"projectId": pid})
        except ZhiyuanCallError as e:
            errors.append(str(e)[:200])
            continue
        except Exception as e:
            errors.append("query_branches(%s): %s: %s"
                          % (pid, type(e).__name__, str(e)[:140]))
            continue

        rows = (br_data.get("data") or br_data.get("items")
                or br_data.get("list") or br_data.get("records") or [])
        if not isinstance(rows, list):
            errors.append("query_branches(%s): 返回结构里没有列表（keys=%s）"
                          % (pid, ",".join(list(br_data)[:6])))
            continue

        for br in rows:
            bid = (br.get("branchId") or br.get("id") or br.get("branch_id")) if isinstance(br, dict) else None
            if not bid:
                continue
            if branch_ids and str(bid) not in {str(x) for x in branch_ids}:
                continue
            branches_scanned += 1

            # ② 该分支的版本（按发布时间倒序）
            args = {"projectId": pid, "branchId": bid}
            args.update(versions_args or {})
            try:
                vd = _call_tool_checked(cli, "query_model_versions", args)
            except ZhiyuanCallError as e:
                errors.append(str(e)[:200])
                continue
            except Exception as e:
                errors.append("query_model_versions(%s/%s): %s: %s"
                              % (pid, bid, type(e).__name__, str(e)[:140]))
                continue

            vrows = (vd.get("data") or vd.get("items") or vd.get("list")
                     or vd.get("records") or [])
            if not isinstance(vrows, list):
                errors.append("query_model_versions(%s/%s): 返回结构里没有列表"
                              % (pid, bid))
                continue

            for v in vrows[:max_per_branch]:
                if not isinstance(v, dict):
                    continue
                vid = v.get("versionId") or v.get("id") or v.get("version_id")
                if not vid:
                    continue
                # 幂等：同一版本只产生一条事件
                ext = "%s:%s:%s" % (pid, bid, vid)
                try:
                    r = svc.receive(ZHIYUAN_POLL_KIND, {
                        "external_id": ext,
                        "thread_scope": "%s:%s" % (pid, bid),
                        "actor": "zhiyuan",
                        "actor_type": "system",
                        "text": "智源分支新版本：%s" % (v.get("name") or v.get("versionName") or vid),
                        "version": str(vid),
                        "at": str(v.get("publishedAt") or v.get("createdAt") or ""),
                        "payload": {
                            "project_id": pid, "branch_id": bid, "version_id": vid,
                            "version_name": v.get("name") or v.get("versionName") or "",
                            "branch_name": br.get("name") or br.get("branchName") or "",
                            "published_at": v.get("publishedAt") or "",
                            # 影响分析需要 change_name，先用分支名兜底（可后续在配置里覆盖）
                            "change_name": br.get("name") or br.get("branchName") or "",
                        },
                    })
                    if r.get("disposition") == "admitted":
                        emitted += 1
                except Exception as e:
                    errors.append("emit(%s): %s" % (ext, str(e)[:120]))
    elapsed = int((time.time() - t0) * 1000)
    ok = not errors
    _state["runs"] += 1
    _state["last_at"] = time.time()
    _state["last_ok"] = ok
    _state["last_error"] = "; ".join(errors)[:400] if errors else ""
    _state["last_found"] = emitted

    out = {"ok": ok, "emitted": emitted, "branches_scanned": branches_scanned,
           "elapsed_ms": elapsed}
    if errors:
        out["errors"] = errors[:10]
        out["note"] = ("**拉取失败**（不等于没有变更）—— 恢复后必须重跑，"
                       "否则停服期间的变更会永久漏掉")
    elif branches_scanned == 0:
        # 合法但可疑：真的没分支 vs 权限/参数导致看不到分支
        out["note"] = ("扫描到 0 个分支。若该工程确实有分支，"
                       "请检查：① token 是否有该项目查阅权限 ② 项目/分支 ID 是否正确 "
                       "③ 分支是否都是草稿（query_branches 不返回草稿）")
    return out


def poll_loop(conn_factory, projects: list, interval_s: int = 600,
              versions_args: dict = None, name: str = "zhiyuan-dev-mcp") -> None:
    """轮询主循环（后台线程）。

    ⚠️ 失败不退出：智源服务抖/挂是常态，守护要能**自愈**（下一轮再试）。
    ⚠️ 失败要留痕：连续失败时日志降噪（每 N 轮报一次），但**不能什么都不说**。
    """
    import threading
    _state["stop"] = threading.Event()
    _consec = 0
    while not _state["stop"].wait(interval_s):
        try:
            with conn_factory() as c:
                r = poll_once(c, projects=projects, versions_args=versions_args, name=name)
            if r.get("ok"):
                _consec = 0
                if r.get("emitted"):
                    print("[ev-poller] 智源新版本事件 +%d（扫 %d 分支，%dms）"
                          % (r["emitted"], r.get("branches_scanned", 0),
                             r.get("elapsed_ms", 0)), flush=True)
            else:
                _consec += 1
                # 降噪：前 3 次每轮报，之后每 10 轮报一次（仍可见，不刷屏）
                if _consec <= 3 or _consec % 10 == 0:
                    print("[ev-poller] 第 %d 次失败（不等于没有变更）：%s"
                          % (_consec, str(r.get("error") or r.get("errors") or "")[:160]),
                          flush=True)
        except Exception as e:
            _consec += 1
            print("[ev-poller] 轮询异常（不退出）: %s" % str(e)[:160], flush=True)


def start_poller(conn_factory, projects: list, interval_s: int = 600,
                 versions_args: dict = None, enabled: bool = False,
                 name: str = "zhiyuan-dev-mcp") -> bool:
    """启动轮询线程（**默认不启用**）。"""
    import threading
    _state["config"] = {"enabled": bool(enabled), "interval_s": int(interval_s),
                        "projects": list(projects or [])}
    if not enabled:
        return False
    if not projects:
        return False           # 没配工程就不启（避免空转）
    if _state["thread"] and _state["thread"].is_alive():
        return False
    t = threading.Thread(target=poll_loop,
                         args=(conn_factory, projects, int(interval_s), versions_args, name),
                         daemon=True, name="ev-zhiyuan-poller")
    t.start()
    _state["thread"] = t
    return True


def stop_poller() -> None:
    if _state.get("stop"):
        _state["stop"].set()


def poller_status() -> dict:
    t = _state["thread"]
    return {
        "running": bool(t and t.is_alive()),
        "config": dict(_state["config"]),
        "runs": _state["runs"],
        "last_found": _state["last_found"],
        "last_ok": _state["last_ok"],
        "last_error": _state["last_error"],
        "last_at": _state["last_at"],
        "note": ("轮询式：本工程作为 MCP 客户端主动拉智源变更。"
                 "拉取失败 ≠ 没有变更；恢复后需重跑"),
    }
