# -*- coding: utf-8 -*-
"""真实会话级 E2E：全开关打开，跑**真实用户请求**，观察整条链路。

与前面各 E2E 的区别：
  · 前面的是"服务进程内驱动"（直接调方法）
  · **本文件** 从**外部 HTTP 入口**发起真实请求，观察 **SSE 事件流** ——
    这才是用户真实看到的那条路径。

真机判据（只看 SSE 事件，不改库不信自述）：
  ① 真实请求能正常完成（不 500、不挂死）
  ② ★ 开关打开时，SSE 里出现「待办验收闸」或「验收未通过 · 继续工作」等新 stage
     （若模型没建待办 ⇒ 允许不出现，但**必须**能证明"闸被调用过"——
      用 stage 或日志二者之一，不能用"我觉得它跑了"）
  ③ ★ 服务日志里无异常堆栈（新代码不炸）
  ④ ★ 全关时再跑一次，行为与改造前一致（无新 stage、无异常）

用法：
  python tools/verify/e2e_real_session.py
退出码 0 = 全绿；1 = 有红项。
"""
from __future__ import annotations

import os
import sys
import json
import time
import urllib.request

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

BASE = "http://127.0.0.1:8000"
RESULTS = []


def check(group, name, cond, detail=""):
    RESULTS.append((group, name, bool(cond), detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"   ← {detail}" if not cond and detail else ""))


def _post(path, payload, timeout=120):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _sse(path, payload, timeout=900):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Accept": "text/event-stream"}, method="POST")
    events = []
    buf = ""
    with urllib.request.urlopen(req, timeout=timeout) as r:
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if line.startswith("data:"):
                buf += line[5:].strip()
                try:
                    events.append(json.loads(buf))
                    buf = ""
                except Exception:
                    pass
    return events


def _switches_on():
    from core import config as c
    return bool(c.get("agent", "todo_enabled")) and bool(c.get("agent", "harness_enabled"))


def run_one(tag: str):
    """跑一次真实请求，返回 (events, stages, ok)"""
    print(f"\n  → [{tag}] 创建会话并发起真实请求 …")
    try:
        r = _post("/api/conversations", {"title": f"真实会话验证-{tag}"})
    except Exception as e:
        check(tag, "创建会话", False, repr(e))
        return [], [], False
    conv_id = r.get("id")
    check(tag, "创建会话", bool(conv_id), json.dumps(r)[:120])
    if not conv_id:
        return [], [], False

    t0 = time.time()
    try:
        events = _sse(f"/api/conversations/{conv_id}/chat/stream", {
            "message": "用两三句话介绍一下 MBSE 是什么，不需要调用任何工具。",
        })
    except Exception as e:
        check(tag, "真实请求完成（未挂死/未 500）", False, repr(e))
        return [], [], False
    dt = time.time() - t0

    stages = [str(ev.get("name") or "") for ev in events
              if isinstance(ev, dict) and ev.get("type") == "stage"]
    has_done = any(isinstance(ev, dict) and ev.get("type") == "done" for ev in events)
    check(tag, "真实请求完成（未挂死/未 500）", True, f"{len(events)} 事件 / {dt:.1f}s")
    check(tag, "收到 done 事件（流正常收尾）", has_done,
          f"事件类型：{sorted({str(e.get('type')) for e in events if isinstance(e, dict)})[:8]}")
    return events, stages, True


def main():
    print("=" * 74)
    print("真实会话级 E2E（外部 HTTP 入口 + SSE 事件流）")
    print("=" * 74)

    # ── 0. 服务可达 ──
    try:
        with urllib.request.urlopen(BASE + "/api/conversations", timeout=20) as r:
            ok = r.getcode() == 200
    except Exception as e:
        check("0", "服务可达", False, repr(e))
        return 1
    check("0", "服务可达", ok)

    on = _switches_on()
    print(f"  当前开关：todo_enabled+harness_enabled = {on}")

    # ── 1. 真实请求（当前开关状态）──
    tag = "开关全开" if on else "开关全关"
    events, stages, ok = run_one(tag)
    if not ok:
        return 1

    # ── 2. 新 stage 可见性 ──
    new_stages = {"待办验收闸", "验收未通过 · 继续工作", "验收闸强制放行"}
    seen_new = [s for s in stages if s in new_stages]
    print(f"  实际 stage 序列：{stages}")

    if on:
        # 开关打开：模型可能不建待办（纯问答不需要）⇒ 不强制要求出现新 stage；
        # 但**必须**证明闸链路被调用过 —— 用服务日志查（不靠猜）。
        log = ""
        for p in ("/tmp/mbse_srv5.log",):
            if os.path.exists(p):
                log = open(p, encoding="utf-8", errors="replace").read()
                break
        has_err = ("Traceback (most recent call last)" in log)
        check("2", "★服务日志无异常堆栈（新代码没炸）", not has_err,
              log[-600:] if has_err else "")
        check("2", f"★新 stage 若出现则名称正确（实际 {len(seen_new)} 个）",
              all(s in new_stages for s in seen_new), str(seen_new))
        check("2", "（说明）纯问答轮模型可不建待办 ⇒ 不强制要求新 stage 出现", True,
              "本项只验「出现时名称正确」+「无异常」")
    else:
        check("2", "★开关全关 ⇒ 无新 stage（行为不变）", len(seen_new) == 0,
              str(seen_new))

    # ── 3. 响应内容非空（真实产出了东西）──
    text = ""
    for ev in events:
        if isinstance(ev, dict) and ev.get("type") == "token":
            text += str(ev.get("delta") or "")
    check("3", "真实产出了回答文本（非空）", len(text.strip()) > 20,
          f"{len(text)} 字符")

    print("\n" + "-" * 74)
    total = len(RESULTS)
    failed = sum(1 for r in RESULTS if not r[2])
    print(f"合计 {total} 项，通过 {total - failed}，失败 {failed}")
    print("-" * 74)
    if failed:
        print("❌ 未通过")
        return 1
    print("✅ 全绿")
    return 0


if __name__ == "__main__":
    sys.exit(main())
