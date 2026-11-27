# -*- coding: utf-8 -*-
"""verify_mock_scope —— 锁住「真实使用中永不使用 Mock」。

## 需求（用户原话，2026-11-27）

> mock 数据在真实使用中要不使用，这个我之前就要求过。

之前已有一条门禁 `verify_no_mock_fallback.py` 管的是**静默回落**（失败时偷偷给 Mock 内容）。
它管不住本次事故的形态：**开关被运行时打开**。

事故实证（conv 1000000246）：
  10:19 用户发起真实建模任务
  10:20 一个验证脚本在同一个服务进程上 `PUT llm.force_mock=true`
  10:21 起该会话 11 次 LLM 调用 `used_mock=1`（3~10ms / 固定 161 token）
  ⇒ 模型被换成样板 ⇒ 待办 2 次无进展 ⇒ 闸门判"卡死"强制放行 ⇒ 841 字无关内容交付

根因：`llm.force_mock` 是**进程级全局设置，且没有作用域**。修法是给它加作用域 ——
只有**进程启动时显式声明**的测试态（`MBSE_TEST_MODE=1`）才允许生效。

## 判据

M1 静态 · 全仓只有 `core/test_mode.py` 与 `llm/__init__.py` 可以读 `llm.force_mock`；
   其余消费点必须走 `core.test_mode.force_mock_active()`（单一真源，防各处各写一套）
M2 静态 · 配置写入接口必须**拒绝**在服务态写 `llm.force_mock=true`（409）
M3 行为 · 服务态（不设 MBSE_TEST_MODE）+ 配置 force_mock=true ⇒
   `force_mock_active()` 为 False，且 `llm_client.chat()` **抛 LLMUnavailableError**
   （不返回 Mock 文本 = 不交付编造内容）
M4 行为 · 测试态（MBSE_TEST_MODE=1）+ 同一配置 ⇒ Mock 生效（回归测试通道保留）
M5 行为 · 运行中的服务：`/api/system/config.guards.test_mode=false`，且
   `PUT llm.force_mock=true` 返回 409 且生效值未变（拿真 HTTP 验，不看源码）

M3/M4 用**子进程**跑：`is_test_mode()` 是进程级且启动即定死，同进程内无法既当服务态
又当测试态 —— 用 monkeypatch 假造"测试态"会验成一个不存在的场景。

用法：.venv/Scripts/python.exe -X utf8 tools/verify/verify_mock_scope.py
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(_HERE))
PY = sys.executable

_fails, _passes, _skips = [], [], []


def chk(tag, ok, detail=""):
    (_passes if ok else _fails).append(tag)
    print(("  PASS " if ok else "  FAIL ") + tag + ("  | " + detail if detail else ""))
    return ok


def skip(tag, detail=""):
    _skips.append(tag)
    print("  SKIP " + tag + ("  | " + detail if detail else ""))


def _walk_py():
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames
                       if d not in (".venv", ".git", "node_modules", "__pycache__",
                                    "tmp", "outputs", "logs", "data", "static")]
        for fn in filenames:
            if fn.endswith(".py"):
                yield os.path.join(dirpath, fn)


FORCE_RE = re.compile(r"""as_bool\(\s*["']llm["']\s*,\s*["']force_mock["']""")


def m1_single_source():
    offenders = []
    for p in _walk_py():
        rel = os.path.relpath(p, ROOT).replace("\\", "/")
        if rel in ("core/test_mode.py", "llm/__init__.py"):
            continue
        if rel.startswith("tests/") or rel.startswith("tools/verify/"):
            continue
        try:
            src = open(p, encoding="utf-8").read()
        except Exception:
            continue
        if FORCE_RE.search(src):
            offenders.append(rel)
    chk("M1 force_mock 只有单一消费点", not offenders,
        "这些文件仍直接读配置：" + ", ".join(offenders) if offenders else
        "其余消费点均走 core.test_mode.force_mock_active()")


def m2_write_guard():
    p = os.path.join(ROOT, "routers", "config.py")
    src = open(p, encoding="utf-8").read()
    has_409 = "409" in src and "llm.force_mock" in src
    has_reason = "MBSE_TEST_MODE" in src or "测试态" in src
    chk("M2 写入接口拒绝服务态开 mock", has_409 and has_reason,
        "含 409 拒绝 + 说明改法" if (has_409 and has_reason) else "缺 409 或缺少说明")


BEHAVIOR = r'''
import json, os, sys
sys.path.insert(0, %(root)r)
from core import test_mode
active = test_mode.force_mock_active()
out = {"test_mode": test_mode.is_test_mode(), "source": test_mode.source(),
       "active": active, "kind": None, "text": ""}
from llm import llm_client
llm_client.get_default_provider = lambda: {"id": 9, "name": "stub", "model_name": "m", "api_key": ""}
try:
    resp = llm_client.chat([{"role": "user", "content": "你好，介绍一下需求工程"}])
    out["kind"] = "returned"
    out["text"] = str((resp.get("choices") or [{}])[0].get("message", {}).get("content") or "")[:80]
except Exception as e:
    out["kind"] = type(e).__name__
    out["text"] = str(e)[:120]
print("RESULT " + json.dumps(out, ensure_ascii=False))
'''


def _behavior(env_extra, force_mock=True):
    env = dict(os.environ)
    env.pop("MBSE_TEST_MODE", None)
    env.pop("MBSE_LLM_FORCE_MOCK", None)
    env.update(env_extra)
    env["MBSE_SHADOW_CONFIG"] = json.dumps({"llm": {"force_mock": bool(force_mock)}})
    env["PYTHONIOENCODING"] = "utf-8"
    p = subprocess.run([PY, "-X", "utf8", "-c", BEHAVIOR % {"root": ROOT}],
                       capture_output=True, text=True, env=env, timeout=180)
    for line in (p.stdout or "").splitlines():
        if line.startswith("RESULT "):
            return json.loads(line[7:])
    raise RuntimeError("子进程无结果：rc=%s\n%s\n%s" % (p.returncode, p.stdout[-800:], p.stderr[-800:]))


def m3_production_no_mock():
    r = _behavior({})
    chk("M3.1 服务态识别", r["test_mode"] is False, "source=%s" % r["source"])
    chk("M3.2 配置写了 true 也不生效", r["active"] is False,
        "force_mock_active=%s（应 False）" % r["active"])
    chk("M3.3 不返回 Mock 内容", r["kind"] == "LLMUnavailableError",
        "实际出口：%s %s" % (r["kind"], r["text"]))
    chk("M3.4 没有假装成功", "Mock 回答" not in (r["text"] or ""), r["text"][:60])


def m4_test_mode_still_works():
    r = _behavior({"MBSE_TEST_MODE": "1"})
    chk("M4.1 测试态识别", r["test_mode"] is True, "source=%s" % r["source"])
    chk("M4.2 Mock 生效（回归通道保留）", r["active"] is True and r["kind"] == "returned",
        "%s %s" % (r["kind"], r["text"][:50]))
    chk("M4.3 内容是 Mock（可识别）", "Mock" in (r["text"] or ""), r["text"][:60])


def m5_live_api():
    import httpx
    base = os.environ.get("MBSE_BASE", "http://127.0.0.1:8000").rstrip("/")
    H = {"X-User-Id": os.environ.get("MBSE_UID", "3")}
    try:
        cfg = httpx.get(base + "/api/system/config", headers=H, timeout=8).json()
    except Exception as e:
        skip("M5 运行中服务的写入闸", "服务不可达：%s" % e)
        return _m5_isolated()
    guards = cfg.get("guards") or {}
    if not guards:
        skip("M5 运行中服务的写入闸",
             "该服务进程还是旧代码（无 guards 段）——重启后本项才会生效；"
             "**不要**在旧进程上验证写入闸（会把开关真的打开）。改验独立实例")
        return _m5_isolated()
    if guards.get("test_mode"):
        skip("M5 运行中服务的写入闸", "当前服务本身是测试态，不适用；改验独立实例")
        return _m5_isolated()
    before = ((cfg.get("static") or {}).get("llm") or {}).get("force_mock")
    r = httpx.put(base + "/api/system/config/static",
                  json={"updates": {"llm.force_mock": True}}, headers=H, timeout=15)
    after = ((httpx.get(base + "/api/system/config", headers=H, timeout=8).json()
              .get("static") or {}).get("llm") or {}).get("force_mock")
    chk("M5.1 服务态写入被拒 409", r.status_code == 409, "HTTP %s" % r.status_code)
    chk("M5.2 生效值未变", after == before, "force_mock %r → %r" % (before, after))


def _m5_isolated():
    """独立起一个**服务态**实例（不设 MBSE_TEST_MODE），验两件事：

    1. 配置文件里明明写着 `force_mock=true`，但服务态下 `/api/system/config.guards`
       报告 `force_mock_effective=false` —— 这是"真实使用中不使用 mock"的可核口径；
    2. 通过 API 想把开关打开 ⇒ 409 拒绝，且生效值不变。

    为什么用独立实例：在生产服务上验证"能不能打开"这件事本身，一旦代码有 bug
    就等于真的打开了 —— 验证动作不能有副作用。
    """
    import importlib.util
    import httpx
    path = os.path.join(_HERE, "verify_degraded_direct_flow.py")
    spec = importlib.util.spec_from_file_location("_vdf_iso", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    srv = mod.IsolatedServer()
    try:
        ok, detail = srv.start(test_mode=False)
        if not chk("M5.0 服务态实例启动", ok, detail):
            print(mod._tail(srv.log))
            return
        H = {"X-User-Id": os.environ.get("MBSE_UID", "3")}
        cfg = httpx.get(srv.base + "/api/system/config", headers=H, timeout=10).json()
        g = cfg.get("guards") or {}
        fm = ((cfg.get("static") or {}).get("llm") or {}).get("force_mock")
        chk("M5.1 配置写着 true 但服务态不生效", fm is True and g.get("force_mock_effective") is False,
            "config force_mock=%r / effective=%r / source=%s"
            % (fm, g.get("force_mock_effective"), g.get("source")))
        r = httpx.put(srv.base + "/api/system/config/static",
                      json={"updates": {"llm.force_mock": True}}, headers=H, timeout=15)
        chk("M5.2 写入被拒 409", r.status_code == 409,
            "HTTP %s %s" % (r.status_code, (r.text or "")[:80]))
        cfg2 = httpx.get(srv.base + "/api/system/config", headers=H, timeout=10).json()
        chk("M5.3 拒绝后仍不生效",
            (((cfg2.get("static") or {}).get("llm") or {}).get("force_mock") is True)
            and (cfg2.get("guards") or {}).get("force_mock_effective") is False,
            "still_effective=%s" % (cfg2.get("guards") or {}).get("force_mock_effective"))
    finally:
        srv.stop()


def main() -> int:
    print("== M1/M2 静态 ==")
    m1_single_source()
    m2_write_guard()
    print("== M3 服务态（子进程）==")
    m3_production_no_mock()
    print("== M4 测试态（子进程）==")
    m4_test_mode_still_works()
    print("== M5 运行中服务（真 HTTP）==")
    m5_live_api()
    bad = len(_fails)
    print("\n合计 %d 项，PASS %d，FAIL %d，SKIP %d"
          % (len(_passes) + bad + len(_skips), len(_passes), bad, len(_skips)))
    if _fails:
        print("失败项：" + "; ".join(_fails))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
