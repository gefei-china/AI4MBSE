# -*- coding: utf-8 -*-
"""Fuseki 运行时单一真源（2026-11-22，P1-2 收尾）。

把此前散在四处的同一组事实收敛到一处：

| 事实 | 此前散落位置 |
|---|---|
| 端点 `http://127.0.0.1:3030/mbse` | `services/ontology_cq.py`、`graph_db.py`、`tools/verify/fuseki_start.py` 注释 |
| 数据集名 `mbse` | `fuseki_start.py::DS`、各文档 |
| TDB2 目录 / 启动参数 | `fuseki_start.py::main()` |
| 是否随主服务拉起 | 无（此前必须人工启动） |

**为什么要收敛**：同一语义四处各写一份，改一处不改其余 ⇒
"主服务自拉起点了 dev、CQ 查的是 mbse"这类错位，且症状出现在查询侧而非配置侧。

## 契约

- `resolve_endpoint()` —— 唯一端点读取入口（config 优先，回落本模块默认值）。
- `build_command()` —— 唯一启动参数构造入口。持久档**必须** `--update --loc=<abs>`
  （漏 `--update` 时 TDB2 只读，`/update` 恒 405，见 docs/Fuseki图库可写与持久化方案）。
- `ensure_running()` —— 幂等：活着就返回 `already_running`，**绝不重复拉起**；
  关掉开关就返回 `disabled`，绝不偷偷启动；启动失败返回结构化状态，
  **不抛异常给主服务**（由图库/门禁自行决定降级，且降级≠通过）。
- `stop()` —— 只停自己记在 PID 文件里的进程；PID 文件丢失时才按 jar 命令行匹配。

⚠️ 本模块**不导入 `core.config` 于模块顶层**（避免 import 环与配置副作用），
   只在函数内惰性读取。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 3030
DEFAULT_DATASET = "mbse"
DEFAULT_ENDPOINT = "http://%s:%d/%s" % (DEFAULT_HOST, DEFAULT_PORT, DEFAULT_DATASET)

JAVA = os.path.join(ROOT, "java-runtime", "bin", "java.exe")
JAR = os.path.join("fuseki", "jena-fuseki-server.jar")
TDB2_DIR = os.path.join(ROOT, "tmp", "fuseki_tdb2")
PID_FILE = os.path.join(ROOT, "tmp", "fuseki.pid")
LOG_FILE = os.path.join(ROOT, "logs", "fuseki.log")

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

_STATUS = (
    "already_running", "started", "disabled", "missing_runtime",
    "port_occupied", "dataset_missing", "start_failed",
)


# ── 端点 ─────────────────────────────────────────────────────────
def resolve_endpoint() -> str:
    """唯一端点入口：config `graph_db.endpoint` 优先，空/畸形回落本模块默认值。"""
    ep = ""
    try:
        from core import config as _cfg
        ep = str(_cfg.get("graph_db", "endpoint") or "").strip()
    except Exception:
        ep = ""
    if ep:
        try:
            base, ds = endpoint_parts(ep)
            if base.startswith("http") and ds:
                return ep.rstrip("/")
        except ValueError:
            pass
    return DEFAULT_ENDPOINT


def endpoint_parts(endpoint: str | None = None) -> tuple[str, str]:
    """`http://host:port/ds` → `(base_url, dataset)`；畸形则 ValueError（不静默兜底）。"""
    ep = (endpoint or DEFAULT_ENDPOINT).rstrip("/")
    base, sep, ds = ep.rpartition("/")
    if not sep or not base.startswith("http") or not ds or "/" in ds:
        raise ValueError("Fuseki 端点必须是 http(s)://host:port/dataset，收到 %r" % ep)
    return base, ds


def _config_bool(key: str, default: bool = False) -> bool:
    try:
        from core import config as _cfg
        return bool(_cfg.get("graph_db", key, default))
    except Exception:
        return default


# ── 探活 ─────────────────────────────────────────────────────────
def ping(base_url: str, timeout: float = 3.0) -> bool:
    try:
        with _OPENER.open(base_url.rstrip("/") + "/$/ping", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def datasets(base_url: str, timeout: float = 6.0) -> list[str]:
    try:
        with _OPENER.open(base_url.rstrip("/") + "/$/datasets", timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        # Fuseki 的 ds.name 带前导斜杠（"/mbse"），而端点末段是 "mbse"。
        # 不归一会让"已活着"被误判成 dataset_missing 并拒绝复用现有实例
        # （真机 2026-11-22 抓到）。
        return [str(d.get("ds.name") or "").lstrip("/") for d in data.get("datasets", [])]
    except Exception:
        return []


def _port_open(host: str, port: int, timeout: float = 0.6) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        return s.connect_ex((host, port)) == 0
    finally:
        s.close()


# ── 启动参数（唯一构造处）─────────────────────────────────────────
def build_command(endpoint: str | None = None, persistent: bool = True) -> list[str]:
    """构造 java 命令。**持久档必须是 `--update --loc=<abs>`，不得有 `--mem`**。"""
    _base, ds = endpoint_parts(endpoint)
    args = [JAVA, "-Xmx1g", "-jar", JAR]
    if persistent:
        # 绝对路径 + 正斜杠：Git Bash 的 MSYS2 会改写以 / 开头的参数。
        # 这里由 Python subprocess 传数组，不经 shell；路径转正在 Python 侧完成。
        loc = os.path.abspath(TDB2_DIR).replace("\\", "/")
        args += ["--update", "--loc=" + loc]
    else:
        args += ["--mem"]
    args += ["--port", str(DEFAULT_PORT), "/" + ds]
    return args


def ensure_persistent_dir() -> str:
    """TDB2 目录必须**预先存在**：Fuseki 6.2 不会自动创建 --loc 目录，
    缺了就直接 `ERROR Directory does not exist` 并退出（2026-11-22 真机抓到）。
    `build_command()` 保持纯函数，目录准备放这里。"""
    path = os.path.abspath(TDB2_DIR)
    os.makedirs(path, exist_ok=True)
    return path


def _spawn(args: list[str]) -> int:
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    log = open(LOG_FILE, "ab", buffering=0)
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | \
        getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
    proc = subprocess.Popen(args, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                            creationflags=flags, close_fds=True)
    os.makedirs(os.path.dirname(PID_FILE), exist_ok=True)
    with open(PID_FILE, "w", encoding="ascii") as f:
        f.write(str(proc.pid))
    return proc.pid


def _read_pid() -> int | None:
    try:
        with open(PID_FILE, encoding="ascii") as f:
            return int(f.read().strip())
    except Exception:
        return None


def stop() -> dict:
    """只停本模块记录的 PID；PID 文件丢失时才按 jar 命令行匹配（最后手段）。"""
    pid = _read_pid()
    killed: list[int] = []
    if pid:
        try:
            r = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=30)
            if r.returncode == 0:
                killed.append(pid)
        except Exception:
            pass
    if not pid:
        # 兼容历史实例：fuseki_start.py --daemon 起的进程没写 PID 文件。
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_Process -Filter \"Name='java.exe'\" | "
                 "Where-Object { $_.CommandLine -like '*jena-fuseki-server.jar*' } | "
                 "ForEach-Object { Stop-Process -Id $_.ProcessId -Force; $_.ProcessId }"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=30)
            killed = [int(x) for x in (out.stdout or "").split() if x.strip().isdigit()]
        except Exception:
            pass
    try:
        os.remove(PID_FILE)
    except OSError:
        pass
    return {"stopped": killed}


def ensure_running(endpoint: str | None = None, autostart: bool | None = None,
                   timeout_s: float = 40.0) -> dict:
    """幂等确保 Fuseki 可用。**任何失败都返回结构化状态，不抛给调用方。**

    状态语义（调用方必须区分，skip/absent ≠ pass）：
      already_running —— 已有实例且数据集匹配，未启动新进程
      started         —— 本次拉起并确认可查
      disabled        —— 开关关闭，未做任何动作（正常默认）
      missing_runtime / port_occupied / dataset_missing / start_failed —— 异常，需排查
    """
    ep = endpoint or resolve_endpoint()
    try:
        base, ds = endpoint_parts(ep)
    except ValueError as e:
        return {"status": "start_failed", "endpoint": ep, "error": str(e)}

    if ping(base):
        found = datasets(base)
        # 双侧归一：Fuseki /$/datasets 的 ds.name 带前导斜杠，而有些调用方
        # （门禁 monkeypatch / 历史脚本）会把原样值传进来。只在一侧归一仍会误判。
        found_norm = [str(x).lstrip("/") for x in found]
        if found and ds not in found_norm:
            return {"status": "dataset_missing", "endpoint": ep, "dataset": ds,
                    "datasets": found, "error": "端口已有 Fuseki，但注册的数据集不含 %r" % ds}
        return {"status": "already_running", "endpoint": ep, "dataset": ds,
                "datasets": found_norm, "pid": _read_pid()}

    if autostart is None:
        autostart = _config_bool("fuseki_autostart", False)
    if not autostart:
        return {"status": "disabled", "endpoint": ep, "dataset": ds}

    if not os.path.exists(JAVA) or not os.path.exists(os.path.join(ROOT, JAR)):
        return {"status": "missing_runtime", "endpoint": ep, "java": JAVA, "jar": JAR}
    host = ep.split("//", 1)[-1].split("/", 1)[0].split(":")[0]
    if _port_open(host, DEFAULT_PORT):
        return {"status": "port_occupied", "endpoint": ep,
                "error": "端口 %d 有服务但 /$/ping 不通，不抢占别人的进程" % DEFAULT_PORT}

    persistent = _config_bool("fuseki_persistent", True)
    try:
        if persistent:
            ensure_persistent_dir()
        args = build_command(ep, persistent=persistent)
        pid = _spawn(args)
    except Exception as e:
        return {"status": "start_failed", "endpoint": ep, "error": "%s: %s"
                % (type(e).__name__, str(e)[:200])}

    deadline = time.monotonic() + max(1.0, timeout_s)
    while time.monotonic() < deadline:
        if ping(base):
            found = datasets(base)
            found_norm = [str(x).lstrip("/") for x in found]
            if found and ds not in found_norm:
                return {"status": "dataset_missing", "endpoint": ep, "dataset": ds,
                        "datasets": found, "pid": pid}
            return {"status": "started", "endpoint": ep, "dataset": ds, "pid": pid,
                    "persistent": persistent, "datasets": found_norm}
        time.sleep(0.5)
    return {"status": "start_failed", "endpoint": ep, "pid": pid,
            "error": "%.0fs 内 /$/ping 未响应，查 %s" % (timeout_s, LOG_FILE)}


def describe() -> dict:
    ep = resolve_endpoint()
    base, ds = endpoint_parts(ep)
    alive = ping(base)
    return {
        "endpoint": ep, "dataset": ds, "alive": alive,
        "datasets": datasets(base) if alive else [],
        "pid": _read_pid(),
        "persistent_dir": os.path.abspath(TDB2_DIR),
        "autostart": _config_bool("fuseki_autostart", False),
        "persistent": _config_bool("fuseki_persistent", True),
    }
