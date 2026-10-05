# -*- coding: utf-8 -*-
"""盘点 tools/verify/ 下哪些门禁**从未接进 CI**（2026-10-05）。

## 为什么需要这个脚本

盘点发现：`tools/verify/` 共**87 个门禁**，CI 里只有 **29 个**
⇒ **58 个已写好的门禁一次都没在合并请求上跑过**，
其中 40 个当前是绿的、24 个带变异自证（已证明断言可被击穿）。

这与「重复定义自检写了但没进 CI」是**完全同型**的缺陷，
且已在本项目重复出现三轮（重复定义门禁 / HyDE 未接前端 / 大量端点无分页保护）。
⇒ 光盘点没用，还要有门禁强制"新门禁必须进 CI"。

## 一条踩过的坑（别误判成"装饰品"）

我一度把 7 个门禁判成「装饰品」，依据是"源码里没有 `sys.exit(1)`"。
**这是错的** —— 动态注入验证（强制把 `sys.exit(main())` 改成 `sys.exit(1)`）
显示38 个同形态门禁**全部**能正确传播非零退出码；再看它们的收尾写法，
都是 `sys.exit(1 if FAIL else 0)` / `sys.exit(0 if FAIL == 0 else 1)`。

⇒ **判"一个门禁有没有用"必须看「断言红了 rc 会不会变」**，
不能只看源码里有没有某个字符串（这与MEMORY 里
「查了消费点再定性」同源）。

## 用法
    # 默认只盘点（秒级）：列出未进 CI 的门禁清单
    .venv/Scripts/python.exe tools/audit_gate_ci_coverage.py

    # 加 --run 才会**真跑**一遍并按绿/红/崩/超时分类（约 5~10 分钟）
    .venv/Scripts/python.exe tools/audit_gate_ci_coverage.py --run
"""
import glob
import io
import os
import re
import subprocess
import sys
import time

ROOT = r"C:\Users\gefei\WorkBuddy\2026-08-04-19-05-52\mbse_system"
os.chdir(ROOT)
PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
CI = io.open(os.path.join(ROOT, ".github", "workflows", "ci.yml"), encoding="utf-8").read()

all_gates = sorted(glob.glob(os.path.join(ROOT, "tools", "verify", "verify_*.py")))
notin = []
for f in all_gates:
    n = os.path.basename(f)[:-3]
    if ("%s.py" % n) not in CI:
        notin.append(f)

print("未进 CI 的门禁：%d 个（CI 里现有 %d 个）"
      % (len(notin), len(all_gates) - len(notin)))

# 只列清单（默认，秒级）
if "--run" not in sys.argv:
    print("未进 CI 的门禁：%d 个" % len(notin))
    print("（加 --run 真跑一遍并按绿/红/崩/超时分类）")
    for f in notin:
        print("  %s" % os.path.basename(f)[:-3])
    raise SystemExit(0)

# 真跑（每个限时 120s），记录 rc /耗时 / 是否超时
rows = []
for f in notin:
    t0 = time.time()
    try:
        r = subprocess.run([PY, "-X", "utf8", f], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=120)
        rc, out = r.returncode, (r.stdout or "")
    except subprocess.TimeoutExpired:
        rc, out = -9, "__TIMEOUT__"
    el = time.time() - t0
    tail = ""
    for ln in out.splitlines():
        if any(k in ln for k in ("结论", "断言总数", "PASS", "合计")):
            tail = ln.strip()
            if "FAIL" in ln or "未通过" in ln:
                break
    rows.append({"name": os.path.basename(f)[:-3], "rc": rc, "ms": el,
                 "tail": tail[:110], "out": out})

GREEN = [r for r in rows if r["rc"] == 0]
RED = [r for r in rows if r["rc"] == 1]
CRASH = [r for r in rows if r["rc"] not in (0, 1) and r["rc"] != -9]
TMO = [r for r in rows if r["rc"] == -9]

print("\n=== 跑得动且绿（接进 CI 即零成本护栏）%d 个 ===" % len(GREEN))
for r in sorted(GREEN, key=lambda x: -x["ms"])[:30]:
    print("  %5.1fs  %-42s %s" % (r["ms"], r["name"], r["tail"][:60]))

print("\n=== 跑得动但红（接进去会阻塞流水线，需先修）%d 个 ===" % len(RED))
for r in RED[:20]:
    print("  %5.1fs  %-42s %s" % (r["ms"], r["name"], r["tail"][:60]))

print("\n=== 崩了/依赖缺失（rc=%s）%d 个 ===" % (
    ",".join(sorted(set(str(r["rc"]) for r in CRASH))) or "-", len(CRASH)))
for r in CRASH[:12]:
    last = [l for l in r["out"].splitlines() if l.strip()][-1:] if r["out"] else []
    print("  %-42s %s" % (r["name"], (last[0] if last else "")[:80]))

print("\n=== 超时 >120s（CI 里不适合）%d 个 ===" % len(TMO))
for r in TMO:
    print("  %-42s" % r["name"])

print("\n=== 汇总 ===")
print(" 绿 %d / 红 %d / 崩 %d / 超时 %d  （共 %d）"
      % (len(GREEN), len(RED), len(CRASH), len(TMO), len(rows)))
print("\n=== 建议 ===")
print("  可直接进 CI 的绿灯门禁：%d 个（合计耗时 %.0fs）"
      % (len(GREEN), sum(r["ms"] for r in GREEN) / 1000))
slow = [r for r in GREEN if r["ms"] > 15000]
print("  其中耗时 >15s 的 %d 个：%s"
      % (len(slow), [(r["name"], "%.0fs" % (r["ms"] / 1000)) for r in slow]))
