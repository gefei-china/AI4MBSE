# -*- coding: utf-8 -*-
"""启动 Fuseki（Windows / Git Bash 可靠版，2026-10-10；2026-11-22 收敛到运行时单一真源）。

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

## 2026-11-22 起
端点 / 数据集名 / TDB2 目录 / 启动参数全部由 `core.fuseki_runtime` 单一真源提供；
本脚本只保留 CLI 分发（前台 / 脱离常驻 / 停止）。
主服务 lifespan 也调用同一个 `ensure_running()`，两边不会再各写一份参数。

## 用法
    ./.venv/Scripts/python.exe -X utf8 tools/verify/fuseki_start.py            # 前台阻塞（--mem）
    ./.venv/Scripts/python.exe -X utf8 tools/verify/fuseki_start.py --daemon   # 后台常驻
    ./.venv/Scripts/python.exe -X utf8 tools/verify/fuseki_start.py --persistent# TDB2 持久化
    ./.venv/Scripts/python.exe -X utf8 tools/verify/fuseki_start.py --stop     # 停
"""
from __future__ import annotations

import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core import fuseki_runtime as fr  # noqa: E402

# 兼容旧引用（门禁/脚本按这些名字取常量）；真值只在 fr 里。
JAVA = fr.JAVA
JAR = fr.JAR
PORT = str(fr.DEFAULT_PORT)
DS = fr.DEFAULT_DATASET
LOC = fr.TDB2_DIR


def _base() -> str:
    # ⚠️ ping/datasets 要的是**服务根** `http://host:port`，不是含数据集名的端点。
    #    曾直接把 resolve_endpoint() 传进去 ⇒ 打 /mbse/$/ping 恒失败，
    #    表现为"明明活着却以为没起"（2026-11-22 抓到）。
    return fr.endpoint_parts(fr.resolve_endpoint())[0]


def _ping(timeout=3) -> str | None:
    return "OK" if fr.ping(_base(), timeout=timeout) else None


def _datasets() -> list[str]:
    return fr.datasets(_base())


def stop() -> int:
    killed = fr.stop().get("stopped") or []
    print(f"已停止 {len(killed)} 个 Fuseki 进程：{killed}")
    return 0


def main() -> int:
    if "--stop" in sys.argv:
        return stop()

    persistent = "--persistent" in sys.argv
    if persistent:
        fr.ensure_persistent_dir()   # Fuseki 6.2 不会自建 --loc 目录
    ep = fr.resolve_endpoint()
    args = fr.build_command(ep, persistent=persistent)

    if _ping():
        print(f"⚠️ 端口 {PORT} 已有服务在跑；先--stop 再启动")
        print("   数据集：", _datasets())
        return 1

    print("启动命令：", " ".join(args))
    if "--daemon" in sys.argv:
        pid = fr._spawn(args)
        print(f"PID={pid}（已脱离本进程）")
        for _ in range(24):          # 最多等 48s
            time.sleep(2)
            if _ping():
                break
        print("ping:", _ping() or "（未响应）")
        print("数据集：", _datasets())
        return 0 if _ping() else 1

    # 前台阻塞
    import subprocess
    p = subprocess.Popen(args, cwd=_ROOT)
    try:
        p.wait()
    except KeyboardInterrupt:
        p.terminate()
    return 0


if __name__ == "__main__":
    sys.exit(main())
