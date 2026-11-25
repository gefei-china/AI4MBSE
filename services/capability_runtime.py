# -*- coding: utf-8 -*-
"""自注册能力（`tools.source='capability'`）的运行时执行器（2026-11-07，P2）。

## 它是闭环的最后一环

```
模型写代码 → capability_apply 提案 → 用户点批准 → 落盘沙箱 + 写 tools 行
   → ★ 本模块把它接到执行链上 → 下一轮会话模型直接调用
```

**没有本模块，前三步全白做**：`tools` 表里有一行、模型看得到、调用时却报
「未知工具」——这正是本仓踩过两次的同型事故（`code_exec` 注册了没接线、
HTTP 工具 `kind` 没写）。所以这一环必须显式存在并被打上门禁。

## 执行方式：与 `skill_script_run` 同一个受信通道

为什么不用 `sandbox.py`：自注册能力要发 HTTP、要读平台配置（`from core import config`），
沙箱的 import 白名单里没有 `os`、builtins 里没有 `open` ⇒ 一跑就失败。
⇒ 走真解释器子进程，靠 `core/workspace` 的 cwd + 环境变量收窄，
   靠 `core/code_guard` 在注册时静态审查。

## 安全要点

1. **路径从库里读，不接受调用方传入**（模型无法指定"跑哪个脚本"）
2. `script_path` 必须仍在工作沙箱内（防止库被篡改后指向任意文件）
3. cwd 固定为脚本所在目录；`MBSE_WORKSPACE` 环境变量给出沙箱根
4. 超时强杀、输出截断
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

__all__ = ["is_capability", "known", "exec_capability", "invalidate_cache",
           "list_capabilities", "DEFAULT_TIMEOUT", "MAX_TIMEOUT"]

DEFAULT_TIMEOUT = 30.0
MAX_TIMEOUT = 120.0
MAX_OUTPUT = 20_000

#: 进程内缓存：name → {"script_path", "workspace", ...}（None 表示"查过、不是能力"）
_CACHE: dict = {}


def invalidate_cache() -> None:
    """注册/删除能力后必须调 —— 否则要等进程重启才生效（"注册了但调不到"）。"""
    _CACHE.clear()


def _load_all() -> dict:
    """一次性把 source='capability' 的行读进缓存（**含停用行**）。

    ⚠️ 为什么要含停用行：只加载 active 的话，"已停用"与"从未注册"对调用方
       完全不可区分 ⇒ 模型看到「未注册的能力」会去**重新注册**，
       而实际只是被停用了。这与本仓反复踩的「查不到就当成没有」是同一族错误。
    """
    from database import get_db
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT name, config, side_effect, status FROM tools "
            "WHERE source='capability'").fetchall()
    finally:
        conn.close()
    out = {}
    for r in rows:
        cfg = {}
        try:
            cfg = json.loads(r["config"] or "{}")
        except Exception:
            cfg = {}
        out[r["name"]] = {"script_path": cfg.get("script_path") or "",
                          "side_effect": r["side_effect"] or "read",
                          "status": r["status"] or ""}
    return out


def is_capability(name: str) -> bool:
    """`name` 是否是**可执行的**自注册能力（供两套工具分发判定路由）。

    ⚠️ 停用的返回 False（不该被注入/路由），但 `why_not()` 能说清原因。
    """
    n = str(name or "")
    if not n:
        return False
    if not _CACHE:
        try:
            _CACHE.update(_load_all())
        except Exception:
            return False
    return (_CACHE.get(n) or {}).get("status") == "active"


def known(name: str) -> bool:
    """是否**存在**该能力（含停用）—— 用于给出准确报错，避免"停用"被当成"不存在"。"""
    if not _CACHE:
        try:
            _CACHE.update(_load_all())
        except Exception:
            return False
    return str(name or "") in _CACHE


def list_capabilities() -> list:
    if not _CACHE:
        try:
            _CACHE.update(_load_all())
        except Exception:
            return []
    return sorted(_CACHE.keys())


def _clamp(v) -> float:
    try:
        t = float(v)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT
    if t <= 0:
        return DEFAULT_TIMEOUT
    return min(max(t, 1.0), MAX_TIMEOUT)


_RUNNER = r'''
import base64, io, json, runpy, sys, traceback
payload = json.loads(base64.b64decode(sys.stdin.buffer.read()).decode("utf-8"))
path, argv = payload["path"], payload.get("args") or {}
sys.argv = [path]
_buf_out, _buf_err = io.StringIO(), io.StringIO()
_old_out, _old_err = sys.stdout, sys.stderr
sys.stdout, sys.stderr = _buf_out, _buf_err
ret = None
try:
    import importlib.util
    spec = importlib.util.spec_from_file_location("_capability", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_capability"] = mod
    spec.loader.exec_module(mod)
    fn = getattr(mod, "run", None)
    if not callable(fn):
        raise RuntimeError("该能力脚本没有定义 run(**kwargs)，无法调用")
    ret = fn(**(argv if isinstance(argv, dict) else {}))
    if ret is not None and not isinstance(ret, str):
        ret = json.dumps(ret, ensure_ascii=False, default=str)
    if ret is not None:
        print(ret)
except SystemExit as _se:
    # 与 skill_tools 同一条纪律：code ∈ (None, 0) 是正常退出，不是失败
    if getattr(_se, "code", None) not in (None, 0):
        sys.stdout, sys.stderr = _old_out, _old_err
        print("__ERR__" + json.dumps({"type": "SystemExit", "msg": str(_se.code),
              "stdout": _buf_out.getvalue()[-6000:], "stderr": _buf_err.getvalue()[-3000:],
              "traceback": ""}, ensure_ascii=False))
        raise SystemExit(0)
except BaseException as e:
    sys.stdout, sys.stderr = _old_out, _old_err
    print("__ERR__" + json.dumps({"type": type(e).__name__, "msg": str(e)[:800],
          "stdout": _buf_out.getvalue()[-6000:], "stderr": _buf_err.getvalue()[-3000:],
          "traceback": traceback.format_exc()[-1500:]}, ensure_ascii=False))
    raise SystemExit(0)
finally:
    sys.stdout, sys.stderr = _old_out, _old_err
print("__RES__" + json.dumps({"stdout": _buf_out.getvalue(),
      "return": (None if ret is None else str(ret)[:4000])}, ensure_ascii=False))
'''


def exec_capability(name: str, arguments: dict | None = None) -> dict:
    """执行一个自注册能力。返回 `{"ok", "result"}`（与 file_tools 同形态）。"""
    import base64

    from core import workspace

    n = str(name or "")
    if not is_capability(n):
        if known(n):
            return {"ok": False,
                    "result": "能力「%s」已**停用**（不是不存在）。"
                              "要恢复请用 capability_apply kind=capability action=enable；"
                              "**不要重新注册同名能力**。" % n}
        return {"ok": False, "result": "未注册的能力：%s（先用 capability_apply 注册）" % n}
    meta = _CACHE.get(n) or {}
    rel = str(meta.get("script_path") or "").strip()
    if not rel:
        return {"ok": False, "result": "能力「%s」缺少 script_path（注册时未落盘？）" % n}

    from core.workspace import resolve_in_root
    try:
        full = resolve_in_root(rel, must_exist=True)
    except PermissionError as e:
        return {"ok": False, "result": "拒绝执行：%s" % e}
    except FileNotFoundError:
        return {"ok": False,
                "result": "能力脚本文件已丢失：%s（可重新注册，或删除该能力）" % rel}

    args = arguments or {}
    if not isinstance(args, dict):
        return {"ok": False, "result": "参数必须是对象（键值对）"}
    timeout = _clamp(args.pop("__timeout__", None) if "__timeout__" in args else None)

    payload = base64.b64encode(json.dumps(
        {"path": full, "args": args}, ensure_ascii=False, default=str).encode()).decode()
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
           workspace.ENV_KEY: workspace.root()}
    try:
        p = subprocess.run([sys.executable, "-X", "utf8", "-c", _RUNNER],
                           input=payload.encode(), capture_output=True,
                           timeout=timeout, env=env, cwd=os.path.dirname(full))
    except subprocess.TimeoutExpired:
        return {"ok": False, "result": "能力「%s」执行超时（>%ss），已强制终止"
                                       % (n, timeout)}
    raw = (p.stdout or b"").decode("utf-8", "replace")
    err = (p.stderr or b"").decode("utf-8", "replace")
    if "__ERR__" in raw:
        try:
            e = json.loads(raw.split("__ERR__", 1)[1].split("__RES__")[0])
        except Exception:
            e = {"type": "Error", "msg": raw[:400]}
        return {"ok": False, "result": json.dumps(
            {"ok": False, "capability": n, "error": "%s: %s" % (e.get("type"), e.get("msg")),
             "stdout": (e.get("stdout") or "")[:MAX_OUTPUT],
             "stderr": (e.get("stderr") or err)[:4000],
             "traceback": (e.get("traceback") or "")[-1500:]},
            ensure_ascii=False)[:MAX_OUTPUT]}
    if "__RES__" not in raw:
        return {"ok": False, "result": json.dumps(
            {"ok": False, "capability": n,
             "error": "解释器异常退出（code=%s）" % p.returncode,
             "stdout": raw[:3000], "stderr": err[:3000]}, ensure_ascii=False)[:MAX_OUTPUT]}
    try:
        res = json.loads(raw.split("__RES__", 1)[1])
    except Exception:
        return {"ok": False, "result": "结果解析失败：%s" % raw[:400]}
    out = {"ok": True, "capability": n,
           "return": (res.get("return") or "")[:6000],
           "stdout": (res.get("stdout") or "")[:MAX_OUTPUT]}
    return {"ok": True, "result": json.dumps(out, ensure_ascii=False)[:MAX_OUTPUT]}
