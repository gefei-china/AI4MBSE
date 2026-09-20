"""沙箱执行器：Skill / MCP 两类插件的最小执行（对齐设计方案 §5.1 运行时层）。

D-3（决策 C：先子进程后容器）：P0 采用「子进程隔离 + 环境裁剪 + 超时限制」，
P1 升级为容器。执行方式统一经 _run_script()：subprocess 内运行，与主进程隔离，
崩溃/死循环不影响服务。

- run_skill(plugin, args)：读取插件目录 SKILL.md（frontmatter + 正文），
  在子进程内解析并返回可执行指令包（name/description/allowed_tools/instructions）。
- run_mcp(plugin, tool, params)：读取 server.json（base_url/transport/auth/tools），
  子进程内发起 streamable_http JSON-RPC 调用（initialize → tools/call），零外部依赖。
"""
import json
import os
import subprocess
import sys
import time

from . import store

# 子进程超时（秒）：网络调用放宽，本地解析收紧
SCRIPT_TIMEOUT = 20
MCP_TIMEOUT = 15
# 子进程环境裁剪：仅保留最小 PATH/系统变量，剔除可能引发副作用的密钥
_SAFE_ENV = {
    "PATH": os.environ.get("PATH", ""),
    "TEMP": os.environ.get("TEMP", ""),
    "TMP": os.environ.get("TMP", ""),
    "PYTHONIOENCODING": "utf-8",
}


def _run_script(code, timeout=SCRIPT_TIMEOUT, argv=None):
    """子进程隔离执行 python 代码。返回 {ok, stdout, stderr, error, latency_ms}。

    argv：透传给子进程的 sys.argv（[code, *argv]）；payload 经此传入（修复 P2-1：
    原实现从未传参，子进程 sys.argv[1] 必 IndexError，Skill/MCP 执行器实际不可用）。
    """
    t0 = time.time()
    try:
        cmd = [sys.executable, "-c", code] + (list(argv or []))
        proc = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=timeout,
            env=_SAFE_ENV, cwd=os.path.dirname(os.path.abspath(__file__)),
        )
        latency = int((time.time() - t0) * 1000)
        if proc.returncode != 0:
            return {"ok": False, "stdout": proc.stdout[:2000], "stderr": proc.stderr[:2000],
                    "error": proc.stderr.strip()[-500:] or "exit code %s" % proc.returncode,
                    "latency_ms": latency}
        return {"ok": True, "stdout": proc.stdout[:4000], "stderr": proc.stderr[:1000],
                "error": "", "latency_ms": latency}
    except subprocess.TimeoutExpired:
        return {"ok": False, "stdout": "", "stderr": "", "error": f"执行超时（>{timeout}s）",
                "latency_ms": int((time.time() - t0) * 1000)}
    except Exception as e:
        return {"ok": False, "stdout": "", "stderr": "", "error": str(e),
                "latency_ms": int((time.time() - t0) * 1000)}


# ─────────────────────────── Skill 执行器 ───────────────────────────
def run_skill(plugin, args=None):
    """Skill 插件执行：解析 SKILL.md → 返回指令包（供工坊注入上下文/执行工具）。

    SKILL.md 缺失时降级：用 manifest.description 生成最小指令包（便于 P0 无文件调试）。
    """
    plugin_id = plugin.get("plugin_id") or plugin.get("id")
    args = args or {}
    md = store.read_skill_md(plugin_id)
    if md is None:
        # 降级：清单内联描述生成指令包（P0 允许无文件创建调试）
        manifest = json.loads(plugin.get("manifest_json") or "{}")
        md = (
            "---\nname: %s\ndescription: %s\nallowed-tools: %s\n---\n%s"
            % (manifest.get("name", ""), manifest.get("description", ""),
               " ".join((manifest.get("permissions") or {}).get("tools", [])),
               "（该插件尚未上传 SKILL.md，使用清单 description 作为指令源）")
        )
    # 在子进程内解析 frontmatter + 正文（隔离解析失败风险；P2-2 复用 skills.parser 唯一实现）
    code = (
        "import sys, json\n"
        "sys.path.insert(0, %r)\n"
        "md = json.loads(sys.argv[1])\n"
        "from skills.parser import parse_frontmatter\n"
        "import re\n"
        "m = re.match(r'^---\\s*\\n(.*?)\\n---\\s*\\n?(.*)$', md, re.S)\n"
        "if not m:\n"
        "    print(json.dumps({'frontmatter': {}, 'body': md.strip()}, ensure_ascii=False)); sys.exit(0)\n"
        "kv = parse_frontmatter(md)\n"
        "print(json.dumps({'frontmatter': kv, 'body': m.group(2).strip()}, ensure_ascii=False))\n"
    ) % os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    r = _run_script(code, timeout=8, argv=[json.dumps(md, ensure_ascii=False)])
    if not r["ok"]:
        return {"ok": False, "error": r["error"], "latency_ms": r["latency_ms"]}
    try:
        parsed = json.loads(r["stdout"].strip().splitlines()[-1])
    except Exception:
        parsed = {"frontmatter": {}, "body": md[:2000]}
    return {
        "ok": True,
        "kind": "skill",
        "plugin_id": plugin_id,
        "name": parsed.get("frontmatter", {}).get("name") or plugin.get("name"),
        "description": parsed.get("frontmatter", {}).get("description", ""),
        "allowed_tools": (parsed.get("frontmatter", {}).get("allowed-tools") or "").split(),
        "instructions": parsed.get("body", "")[:6000],
        "args_received": args,
        "latency_ms": r["latency_ms"],
    }


# ─────────────────────────── MCP 执行器 ───────────────────────────
def run_mcp(plugin, tool, params=None):
    """MCP 插件执行：server.json 描述 → streamable_http JSON-RPC 调用。

    支持 transport: streamable_http / http / sse（sse 仅探测，P1 接入 sse-client）。
    子进程隔离执行，超时 15s；调用失败结构化返回（不抛异常）。
    """
    plugin_id = plugin.get("plugin_id") or plugin.get("id")
    params = params or {}
    server = store.read_server_json(plugin_id)
    if server is None:
        # 降级：manifest.capabilities.mcp[0] 内联 server 信息
        manifest = json.loads(plugin.get("manifest_json") or "{}")
        caps = manifest.get("capabilities") or {}
        mcps = caps.get("mcp") or []
        if mcps and isinstance(mcps[0], dict) and mcps[0].get("server") and isinstance(mcps[0]["server"], dict):
            server = mcps[0]["server"]
        else:
            return {"ok": False, "error": "缺少 server.json（MCP 插件需配置 base_url/tools）", "latency_ms": 0}

    base_url = server.get("base_url") or ""
    transport = server.get("transport") or "streamable_http"
    if not base_url:
        return {"ok": False, "error": "server.json 缺少 base_url", "latency_ms": 0}

    known_tools = server.get("tools") or []
    code = (
        "import sys, json, urllib.request, urllib.error, time\n"
        "base_url, tool, params, known_tools = json.loads(sys.argv[1])\n"
        "t0 = time.time()\n"
        "def post(payload, to=12):\n"
        "    req = urllib.request.Request(base_url, data=json.dumps(payload).encode(),\n"
        "        headers={'Content-Type': 'application/json'}, method='POST')\n"
        "    with urllib.request.urlopen(req, timeout=to) as resp:\n"
        "        body = resp.read().decode('utf-8', 'replace')\n"
        "        ct = resp.headers.get('Content-Type', '')\n"
        "    # streamable_http 可能返回多行 JSON（SSE 风格），逐行解析取最后完整 JSON\n"
        "    import re\n"
        "    if 'text/event-stream' in ct:\n"
        "        cands = [l for l in body.splitlines() if l.startswith('data:')]\n"
        "        body = cands[-1][5:].strip() if cands else body\n"
        "    return json.loads(body)\n"
        "def fail(msg, **kw):\n"
        "    out = {'ok': False, 'error': str(msg)[:500], 'latency_ms': int((time.time()-t0)*1000), 'degraded': True}\n"
        "    out.update(kw); print(json.dumps(out)); sys.exit(0)\n"
        "try:\n"
        "    tools = known_tools\n"
        "    if not tools:\n"
        "        # P2-1：无缓存工具列表时才做 initialize+tools/list（超时分级 8s），有缓存直接 tools/call 省 2 轮握手\n"
        "        post({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {\n"
        "            'protocolVersion': '2024-11-05', 'capabilities': {}, 'clientInfo': {'name': 'zhiyuan-studio', 'version': '0.1.0'}}}, to=8)\n"
        "        tools_resp = post({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list', 'params': {}}, to=8)\n"
        "        tools = [t['name'] for t in tools_resp.get('result', {}).get('tools', [])]\n"
        "    if tools and tool not in tools:\n"
        "        fail('工具 %s 不在 MCP 服务器工具列表: %s' % (tool, tools), tools=tools)\n"
        "    call = post({'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': tool, 'arguments': params}}, to=15)\n"
        "    res = call.get('result', call)\n"
        "    print(json.dumps({'ok': True, 'result': res, 'tools': tools, 'latency_ms': int((time.time()-t0)*1000), 'degraded': False}, ensure_ascii=False)); sys.exit(0)\n"
        "except urllib.error.HTTPError as e:\n"
        "    fail('HTTP %s: %s' % (e.code, e.read().decode('utf-8','replace')[:500]))\n"
        "except Exception as e:\n"
        "    fail(e)\n"
    )
    payload = [base_url, tool, params, known_tools]
    r = _run_script(code, timeout=MCP_TIMEOUT, argv=[json.dumps(payload, ensure_ascii=False)])
    if not r["ok"]:
        return {"ok": False, "error": r["error"], "latency_ms": r["latency_ms"]}
    try:
        out = json.loads(r["stdout"].strip().splitlines()[-1])
    except Exception:
        return {"ok": False, "error": "执行器输出不可解析", "stdout": r["stdout"][:500], "latency_ms": r["latency_ms"]}
    out["kind"] = "mcp"
    out["plugin_id"] = plugin_id
    out["tool"] = tool
    return out


def execute(conn, plugin, tool, params=None, user=None):
    """统一入口：按插件类型分发执行，并写 plugin_call_logs（审计闭环 P0）。"""
    ptype = plugin.get("type") or "bundle"
    t0 = time.time()
    if ptype in ("skill", "bundle") and (tool in ("skill", "skill/run", "") or ptype == "skill"):
        result = run_skill(plugin, params)
    else:
        result = run_mcp(plugin, tool, params)
    latency = result.get("latency_ms", int((time.time() - t0) * 1000))
    store.log_call(conn, plugin.get("plugin_id"), tool or ptype, params,
                   "success" if result.get("ok") else "error", latency)
    store.log_audit(conn, user, plugin.get("plugin_id"), "run",
                    {"tool": tool or ptype, "ok": result.get("ok")})
    conn.commit()
    return result
