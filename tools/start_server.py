# -*- coding: utf-8 -*-
"""以「脱离当前进程」的方式常驻启动 MBSE 服务（--daemon）。

★ 为什么需要这个（2026-10-10 实测踩坑）：
  直接 `python main.py &` 或后台跑，进程会**在启动它的 shell 退出时被一起回收**。
  本机实测：`nohup` 在 Git Bash 下无效、`Start-Process` 被安全策略拦、
  裸 `DETACHED_PROCESS`（不加 CREATE_NO_WINDOW）也会闪退。
  ⇒ 可行组合 = **CREATE_NO_WINDOW | DETACHED_PROCESS | close_fds=True**
  （同一方案已在 tools/verify/fuseki_start.py 验证过）。

用法：
  python tools/start_server.py            # 前台阻塞（调试用，Ctrl+C 退出）
  python tools/start_server.py --daemon   # 脱离启动（本机唯一可靠的长驻方式）
  python tools/start_server.py --stop     # 停掉 8000 端口上的服务
"""
import os
import subprocess
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8000


def _port_alive() -> bool:
    """端口是否有服务在监听。"""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(1.5)
    try:
        return s.connect_ex(("127.0.0.1", PORT)) == 0
    finally:
        s.close()


def _http_ok() -> bool:
    import urllib.request
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/openapi.json" % PORT, timeout=5) as r:
            return r.status == 200
    except Exception:
        return False


def stop():
    if not _port_alive():
        print("[stop] 端口 %d 无服务" % PORT)
        return 0
    # 找到占用端口的 PID 再杀（连带子进程用 /T）
    out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True).stdout
    pids = set()
    for line in out.splitlines():
        if ":%d " % PORT in line and "LISTENING" in line:
            pids.add(line.split()[-1])
    for pid in pids:
        r = subprocess.run(["taskkill", "/PID", pid, "/T", "/F"],
                           capture_output=True, text=True)
        print("[stop] taskkill %s -> %s" % (pid, (r.stdout or r.stderr).strip()))
    time.sleep(2)
    print("[stop] 端口仍存活?" , _port_alive())
    return 0


def daemon():
    if _port_alive():
        print("[daemon] 端口 %d 已在监听，先 --stop 再启动" % PORT)
        return 1
    py = os.path.join(_ROOT, ".venv", "Scripts", "python.exe")
    if not os.path.exists(py):
        py = sys.executable
    log_path = os.path.join(_ROOT, "logs", "server_daemon.log")
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    log = open(log_path, "ab", buffering=0)
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | \
        getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
    proc = subprocess.Popen(
        [py, "main.py"], cwd=_ROOT, stdout=log, stderr=subprocess.STDOUT,
        creationflags=flags, close_fds=True,
    )
    print("[daemon] PID=%d（已脱离本进程）日志=%s" % (proc.pid, log_path))
    for _ in range(40):          # 最多等 80s
        time.sleep(2)
        if _http_ok():
            print("[daemon] ready: http://127.0.0.1:%d  (%ds)" % (PORT, _ * 2 + 2))
            return 0
    print("[daemon] 80s 内未就绪，请查日志")
    return 1


def foreground():
    py = os.path.join(_ROOT, ".venv", "Scripts", "python.exe")
    if not os.path.exists(py):
        py = sys.executable
    p = subprocess.Popen([py, "main.py"], cwd=_ROOT)
    try:
        p.wait()
    except KeyboardInterrupt:
        p.terminate()
    return 0


if __name__ == "__main__":
    if "--stop" in sys.argv:
        sys.exit(stop())
    if "--daemon" in sys.argv:
        sys.exit(daemon())
    sys.exit(foreground())
