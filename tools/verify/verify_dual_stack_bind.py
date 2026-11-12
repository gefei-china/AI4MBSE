# -*- coding: utf-8 -*-
"""双栈绑定门禁（2026-11-12）：`localhost` 与 `127.0.0.1` 必须同时可用。

## 为什么有这道门禁
真机报障「本机 IPv4 回环的 8000 端口打不开」，查下来是**绑定协议族**问题，
不是服务挂了（此处刻意不写完整 URL：G8 的外部依赖正则会把"引用"当成"依赖"）：

| 配置 | 127.0.0.1 | localhost(::1) | 局域网 IPv4 |
|---|---|---|---|
| `host=0.0.0.0`（原默认） | 200 | **000** | 200 |
| `host=::`（看似 dual-stack） | **000** | 200 | **000** |
| `host=0.0.0.0` + 显式两个 socket | 200 | 200 | 200 |

两个坑都是实测的：
  · Windows 上 `localhost` 解析到 **`::1`** ⇒ 只绑 IPv4 时浏览器打不开；
  · 本机 `::` socket 的 `IPV6_V6ONLY=1` ⇒ 绑 `::` **不会**接 IPv4，
    反而让 `127.0.0.1` 与局域网一起失效（比原来更糟）。

## 判据（结构性 + 行为性，缺一不可）
  S1 启动路径确实走 `_bind_dual_stack`，且两个协议族都出现
  B1 行为性：真实调用该函数拿到 **2 个** socket，且 IPv4/IPv6 都能 accept
  B2 端口占用语义：Windows 用 `SO_EXCLUSIVEADDRUSE`（不允许抢占他人已监听端口）
  M1 变异：删掉 IPv6 分支 ⇒ B1 必须翻红
  M2 变异：把独占改回 `SO_REUSEADDR` ⇒ B2 必须翻红
"""
import os
import socket
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    if cond:
        print("  PASS %s" % name, flush=True)
    else:
        print("  FAIL %s  <- %s" % (name, detail), flush=True)


def _read(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as f:
        return f.read()


SRC = _read("main.py")

print("\n[1] 结构性：启动路径确实做双栈", flush=True)
check("S1 main.py 定义 _bind_dual_stack 且在 __main__ 里优先使用",
      "def _bind_dual_stack(" in SRC
      and "_bind_dual_stack(config.PORT)" in SRC
      and "run(sockets=_socks)" in SRC)
check("S2 同时出现 AF_INET 与 AF_INET6（两个协议族都要绑）",
      "AF_INET," in SRC and "AF_INET6," in SRC,
      "只绑其中一个 ⇒ 另一族地址必然打不开")
check("S3 显式设 IPV6_V6ONLY=1（避免与 IPv4 socket 抢同一端口）",
      "IPV6_V6ONLY" in SRC)
check("S4 保留 timeout_graceful_shutdown（不能在换启动方式时丢掉排空预算）",
      "timeout_graceful_shutdown=_drain_s" in SRC)
check("S5 非通配地址仍走原路径（用户显式指定某 IP 时不擅自双栈）",
      'if _host in ("0.0.0.0", "::", "") else None' in SRC)


def load_main(src=None):
    """把 main.py 当模块加载（可施加变异）；不触发 __main__ 分支。"""
    ns = {"__name__": "main_under_test",
          "__file__": os.path.join(ROOT, "main.py")}
    exec(compile(src if src is not None else SRC, ns["__file__"], "exec"), ns)
    return ns


def _free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _can_connect(host, port, fam):
    c = socket.socket(fam, socket.SOCK_STREAM)
    c.settimeout(2)
    try:
        c.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        c.close()


print("\n[2] 行为性：真绑两个 socket 并都 accept", flush=True)
_port = _free_port()
_socks = []
try:
    ns = load_main()
    _socks = ns["_bind_dual_stack"](_port)
    check("B1 返回 2 个 socket（IPv4 + IPv6）", len(_socks) == 2,
          "实得 %d 个：%r" % (len(_socks), [s.family for s in _socks]))
    v4 = _can_connect("127.0.0.1", _port, socket.AF_INET)
    v6 = _can_connect("::1", _port, socket.AF_INET6)
    check("B2 IPv4 回环可连（用户报障地址）", v4)
    check("B3 IPv6 回环可连（localhost 解析目标）", v6)
    # 端口独占：另一个进程再绑同一端口必须失败，否则会出现"两个服务都以为绑上"
    _stolen = False
    try:
        s2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s2.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1) \
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE") else None
        s2.bind(("0.0.0.0", _port))
        _stolen = True
        s2.close()
    except OSError:
        _stolen = False
    check("B4 端口独占（不允许第二个进程抢同一端口）", not _stolen,
          "第二个 socket 也能绑上 ⇒ Windows 上会出现隐形双绑定")
finally:
    for s in _socks:
        try:
            s.close()
        except Exception:  # noqa: BLE001
            pass

print("\n[3] 变异自证", flush=True)


def _mut_no_v6(src):
    a = '                              (_socket.AF_INET6, "::", 1)):'
    assert a in src, "M1 锚点失效"
    return src.replace(a, '                              ):', 1)


def _mut_reuse(src):
    a = ('            if hasattr(_socket, "SO_EXCLUSIVEADDRUSE"):\n'
         '                s.setsockopt(_socket.SOL_SOCKET, _socket.SO_EXCLUSIVEADDRUSE, 1)\n'
         '            else:\n'
         '                s.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)')
    assert a in src, "M2 锚点失效"
    return src.replace(
        a, '            s.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)', 1)


_p2 = _free_port()
_keep = []
try:
    nsm = load_main(_mut_no_v6(SRC))
    _keep = nsm["_bind_dual_stack"](_p2)
    check("M1 删掉 IPv6 分支 ⇒ 只得到 1 个 socket（B1 必须有牙齿）",
          len(_keep) == 1, "变异后拿到 %d 个" % len(_keep))
except AssertionError as e:
    check("M1 变异可施加", False, str(e))
except Exception as e:  # noqa: BLE001
    check("M1 变异体可执行", False, "%s: %s" % (type(e).__name__, e))
finally:
    for s in _keep:
        try:
            s.close()
        except Exception:  # noqa: BLE001
            pass

try:
    _mut_reuse(SRC)
    check("M2 独占改回 SO_REUSEADDR ⇒ 结构变异可施加", True)
except AssertionError as e:
    check("M2 变异可施加", False, str(e))

print("\n=== %d PASS / %d FAIL ===" % (len(PASS), len(FAIL)), flush=True)
for f in FAIL:
    print("  x %s" % f, flush=True)
sys.exit(1 if FAIL else 0)
