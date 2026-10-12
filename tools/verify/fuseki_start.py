# -*- coding: utf-8 -*-
"""启动 Fuseki（Windows / Git Bash 可靠版，2026-10-10）。

## 为什么不用 bash 直接跑（★ 踩了4 次坑）
`/mbse` 这个**数据集名在 Git Bash 里会被 MSYS2 转成 Windows 路径**：
    /mbse  →  C:/Users/.../PortableGit/mbse
⇒ Fuseki 注册的数据集名变成 `/C:/Users/.../PortableGit/mbse`，
   于是 `http://127.0.0.1:3030/mbse/query` 返回 **405 / 404**。
⇒ 用 MSYS2_ARG_CONV_EXCL="*" 也不可靠（实测仍被转换）。
⇒ **正解：用 Python 的 subprocess 启动**（Python 不做 MSYS 路径转换）。

## 另一个坑：`--base` 不是数据目录
Fuseki 6.2.0 的 `--base=DIR` 是**静态内容目录**（放 UI 文件），
**不是数据集目录** ⇒ 传了它，日志里`Fuseki Base` 会变成它，
  而数据目录回落到`run/`。
⇒ 持久化要用 `--loc=DIR`（TDB2 库）或 `--tdb2`；本脚本默认 `--mem`（免持久化，验证够用）。

## 用法
    ./.venv/Scripts/python.exe -X utf8 tools/verify/fuseki_start.py            # 前台阻塞
    ./.venv/Scripts/python.exe -X utf8 tools/verify/fuseki_start.py --daemon   # 后台常驻
    ./.venv/Scripts/python.exe -X utf8 tools/verify/fuseki_start.py --persistent# TDB2 持久化
    ./.venv/Scripts/python.exe -X utf8 tools/verify/fuseki_start.py --stop     # 停
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))

JAVA = os.path.join(_ROOT, "java-runtime", "bin", "java.exe")
JAR = os.path.join("fuseki", "jena-fuseki-server.jar")
PORT = "3030"
DS = "mbse"                      # ★ 纯 ASCII，无斜杠 ⇒ 不会被任何 shell 转换
LOC = os.path.join(_ROOT, "tmp", "fuseki_tdb2")
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _ping(timeout=3) -> str | None:
    try:
        with _OPENER.open(f"http://127.0.0.1:{PORT}/$/ping", timeout=timeout) as r:
            return r.read().decode("utf-8", "replace").strip()
    except Exception:
        return None


def _datasets() -> list[str]:
    try:
        with _OPENER.open(f"http://127.0.0.1:{PORT}/$/datasets", timeout=6) as r:
            import json
            return [d.get("ds.name") for d in json.loads(r.read().decode()).get("datasets", [])]
    except Exception:
        return []


def stop() -> int:
    #只杀本 jar 的进程（按命令行匹配，避免误杀别的 java）
    killed = 0
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='java.exe'\" | "
             "Where-Object { $_.CommandLine -like '*jena-fuseki-server.jar*' } | "
             "ForEach-Object { Stop-Process -Id $_.ProcessId -Force; $_.ProcessId }"],
            capture_output=True, text=True, timeout=30)
        killed = len([x for x in (out.stdout or "").split() if x.strip()])
    except Exception as e:
        print(f"停止失败：{type(e).__name__} {e}")
    print(f"已停止 {killed} 个 Fuseki 进程")
    return 0


def main() -> int:
    if "--stop" in sys.argv:
        return stop()

    args = [JAVA, "-Xmx1g", "-jar", JAR]
    if "--persistent" in sys.argv:
        os.makedirs(LOC, exist_ok=True)
        # ★--loc 必须是**绝对路径且用正斜杠**；用 os.path 转换避免 MSYS 介入
        args += ["--loc=" + LOC.replace("\\", "/")]
    else:
        args += ["--mem"]
    args += ["--port", PORT, "/" + DS]      # ★关键：/mbse 传给 java，不经 shell

    if _ping():
        print(f"⚠️ 端口 {PORT} 已有服务在跑；先--stop 再启动")
        print("   数据集：", _datasets())
        return 1

    print("启动命令：", " ".join(args))
    if "--daemon" in sys.argv:
        log = open(os.path.join(_ROOT, "logs", "fuseki.log"), "ab")
        # ★★ DETACHED_PROCESS（Windows 0x00000008）：让 java **脱离本进程**存活。
        #   踩过的坑：不加这个 flag 时，子进程会在**启动它的 shell 退出时被一起回收**
        #   （`nohup` 在 Git Bash 下无效；`Start-Process` 被安全策略拦）——
        #   现象是"日志显示 Start Fuseki成功、ping 也通，几秒后连不上"。
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | \
            getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        proc = subprocess.Popen(args, cwd=_ROOT, stdout=log, stderr=subprocess.STDOUT,
                                creationflags=flags, close_fds=True)
        print(f"PID={proc.pid}（已脱离本进程）")
        for _ in range(24):          # 最多等 48s
            time.sleep(2)
            if _ping():
                break
        print("ping:", _ping() or "（未响应）")
        print("数据集：", _datasets())
        return 0 if _ping() else 1

    # 前台阻塞
    p = subprocess.Popen(args, cwd=_ROOT)
    try:
        p.wait()
    except KeyboardInterrupt:
        p.terminate()
    return 0


if __name__ == "__main__":
    sys.exit(main())
