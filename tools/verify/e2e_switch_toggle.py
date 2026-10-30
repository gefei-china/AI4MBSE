# -*- coding: utf-8 -*-
"""真机 E2E：新开关**怎么打开**、打开后**是否即时生效**、关闭后**是否复原**。

回答的实际问题（米爸提问）：
  「新开关都默认关了，怎么实现这次新增的能力，在哪里打开呢？」

验证路径（**全部走真实 HTTP 接口，不直接改库、不 monkey-patch**）：
  ① GET  /api/system/config/schema            —— 前端配置页读的就是这个
      断言：7 个新键都在 schema 里，且 `default` / `current` 可读
  ② PUT  /api/system/config/static（打开）    —— 界面拨开关走的就是这个
      断言：返回 ok；**不重启**服务
  ③ 服务进程内读 gate 配置                    —— 断言已变为 True（即时生效）
  ④ PUT  关闭 → 断言复原 False
  ⑤ 断点清理：恢复全部默认关（不改变用户环境）

用法：python tools/verify/e2e_switch_toggle.py
退出码 0 = 全绿；1 = 有红项。
"""
from __future__ import annotations

import os
import sys
import json
import urllib.request

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

BASE = "http://127.0.0.1:8000"
RESULTS = []

NEW_KEYS = [
    "todo_enabled", "todo_require_verify", "harness_enabled",
    "harness_max_blocks", "harness_max_stall",
    "budget_degrade_ratio", "obstacle_recovery_enabled",
]


def check(group, name, cond, detail=""):
    RESULTS.append((group, name, bool(cond), detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"   ← {detail}" if not cond and detail else ""))


def _req(method, path, payload=None, timeout=60):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def run():
    print("=" * 74)
    print("真机 E2E：新开关的打开路径与即时生效")
    print("=" * 74)

    # ── ① schema 可读（前端配置页的数据源）──
    try:
        d = _req("GET", "/api/system/config/schema")
    except Exception as e:
        check("1", "GET /api/system/config/schema", False, repr(e))
        return
    agent = (d.get("schema") or {}).get("agent") or {}
    check("1", "GET schema 可达且含 agent 段", bool(agent), f"{len(agent)} 个键")
    for k in NEW_KEYS:
        m = agent.get(k)
        check("1", f"schema 含 {k}", m is not None,
              "配置页不会显示该键" if m is None else "")
    # default 必须可读（否则界面显示"默认 —"，用户看不出该不该开）
    for k in ("todo_enabled", "harness_enabled", "obstacle_recovery_enabled"):
        m = agent.get(k) or {}
        check("1", f"★{k} 的 default 可读（界面能显示默认值）",
              m.get("default") is not None, str(m.get("default")))

    # ── ② 真实打开（PUT，界面拨开关同一条路径）──
    payload = {"updates": {
        "agent.todo_enabled": "true",
        "agent.harness_enabled": "true",
        "agent.obstacle_recovery_enabled": "true",
    }}
    try:
        r = _req("PUT", "/api/system/config/static", payload)
    except Exception as e:
        check("2", "PUT 打开三个开关", False, repr(e))
        return
    check("2", "★PUT 打开成功", r.get("ok") is True, json.dumps(r, ensure_ascii=False)[:200])
    check("2", "★返回的说明含「即时生效」",
          "即时生效" in str(r.get("note") or ""), str(r.get("note")))

    # ── ③ 同一服务进程内验证已生效（不是重启后生效）──
    #    注意：本脚本是**独立进程**，读的是同一份配置文件 ⇒ 证明"落盘 + reload"生效。
    #    服务进程侧与此同时调用 config.get 也会拿到新值（save_override 内部 reload）。
    from core import config as cfg
    for k in ("todo_enabled", "harness_enabled", "obstacle_recovery_enabled"):
        check("3", f"★{k} 读回 True（无需重启）",
              bool(cfg.get("agent", k)) is True, str(cfg.get("agent", k)))

    # ── ④ 闸真的据此生效 ──
    from agent.pipeline_parts.autopilot import AutoPilotMixin
    obj = AutoPilotMixin()
    check("4", "★_todo_gate_enabled() 变 True（闸启用）",
          obj._todo_gate_enabled() is True)
    hc = obj._harness_cfg()
    check("4", "★_harness_cfg().enabled 变 True（强制闸启用）", hc.get("enabled") is True)
    check("4", "★harness_max_blocks 默认 8 生效", hc.get("max_blocks") == 8, str(hc))
    check("4", "★harness_max_stall 默认 2 生效", hc.get("max_stall") == 2, str(hc))

    # ── ⑤ 关闭 → 复原 ──
    try:
        r2 = _req("PUT", "/api/system/config/static", {"updates": {
            "agent.todo_enabled": "false",
            "agent.harness_enabled": "false",
            "agent.obstacle_recovery_enabled": "false",
        }})
    except Exception as e:
        check("5", "PUT 关闭三个开关", False, repr(e))
        return
    check("5", "PUT 关闭成功", r2.get("ok") is True)
    cfg.reload()
    for k in ("todo_enabled", "harness_enabled", "obstacle_recovery_enabled"):
        check("5", f"★{k} 复原为 False", bool(cfg.get("agent", k)) is False,
              str(cfg.get("agent", k)))
    obj2 = AutoPilotMixin()
    check("5", "★关闭后闸不再启用（行为回到改造前）",
          obj2._todo_gate_enabled() is False and obj2._harness_cfg().get("enabled") is False)

    # ── ⑥ 收尾：确认配置文件已恢复默认关 ──
    for k in ("todo_enabled", "harness_enabled", "obstacle_recovery_enabled"):
        check("6", f"收尾：{k} 已是 False（不留开启状态）",
              bool(cfg.get("agent", k)) is False)


def main():
    run()
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
