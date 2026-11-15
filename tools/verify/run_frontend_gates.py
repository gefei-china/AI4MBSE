# CI-OPTIONAL: [local] 前端门禁**统一执行器**：本机跑（需 8000 服务 + Chromium）；CI 不跑，只做工具类脚本
# -*- coding: utf-8 -*-
"""前端门禁统一执行器（2026-11-13）

## 为什么需要它
`tools/verify/` 下已有 20+ 个 `verify_*.js`（真实浏览器门禁），但：
  ① 它们**没有任何统一入口** —— 每轮回归靠人手点某几个，全跑一遍没人做过；
  ② 清单（`gates_manifest.json`）此前只收 `.py`，这些脚本连"是否接线"都无人管。
②已在本轮修（G5 论域扩到 `.js` + 变异 M8 锁死）。本脚本补 ①：**一条命令跑全部**。

## 判据口径（三态，绝不含糊）
  PASS  脚本退出码 0
  FAIL  脚本退出码非 0（含断言失败）
  SKIP  脚本退出码 2（约定：**环境不可用**，如服务没起/浏览器起不来）
        ⇒ SKIP 不算通过、也不算失败，但会在汇总里单列并计入 `skipped`；
          只有 `--strict` 时 SKIP 才当失败（默认不当，因为"机器没装浏览器"不是代码缺陷）。

## 用法
  ./.venv/Scripts/python.exe -X utf8 tools/verify/run_frontend_gates.py
  ... --only verify_hil_ask_card,verify_chat_scroll      # 逗号分隔子串匹配
  ... --list                                             # 只列清单，不执行
  ... --strict                                           # SKIP 视为失败
  ... --timeout 900
退出码：0 全绿（SKIP 允许）／1 有 FAIL／2 一个都没跑（配置错误）
"""
import argparse
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
VDIR = os.path.join(ROOT, "tools", "verify")
MANIFEST = os.path.join(ROOT, "tools", "gates_manifest.json")

#: 前端门禁的**唯一识别口径**：`verify_*.js`（与 verify_gate_wiring.scan_gates 一致）
def discover():
    out = []
    for n in sorted(os.listdir(VDIR)):
        if n.startswith("verify_") and n.endswith(".js"):
            out.append(n)
    return out


def declared_js():
    """清单里登记的 .js 门禁（用于交叉核对：发现未登记的新脚本要提醒）。"""
    try:
        with open(MANIFEST, encoding="utf-8") as f:
            m = json.load(f)
    except (OSError, ValueError):
        return set()
    return {g.get("script") for g in m.get("gates") or [] if str(g.get("script", "")).endswith(".js")}


def node_exe():
    """定位 node：环境变量 > PATH。

    ★ 2026-11-15（verify_gate_portability P5 判红）：
      原实现第三级是**硬编码本机绝对路径** `C:/Users/<本机用户名>/.workbuddy/.../node/versions`。
      那一条有三个问题，与 `verify_agent_name_unify.py` 里已经踩过并删掉的是同一个：
        ① **泄露本机用户名**（隐私）；
        ② 本机永远有、CI 永远没有 ⇒ 给本地和 CI **两套行为**
           （本地静默用备用 node 通过，CI 一律 SKIP，问题被掩盖）；
        ③ WorkBuddy 升级后版本目录名会漂移（22.22.2-3 → -6 实测失效）。
      既然有 PATH 兜底、且调用方本就能用 `MBSE_NODE` 显式指定，
      硬编码这一级没有存在理由 ⇒ 删除。没有 node 时返回 None，
      由 main() 报 SKIP(rc=2) 并说清原因（SKIP 是"环境没有"，不是通过）。
    """
    import shutil
    p = os.environ.get("MBSE_NODE") or ""
    if p and os.path.isfile(p):
        return p
    return shutil.which("node") or shutil.which("node.exe") or None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="逗号分隔的子串过滤（匹配脚本名）")
    ap.add_argument("--list", action="store_true", help="只列脚本，不执行")
    ap.add_argument("--strict", action="store_true", help="SKIP 也算失败")
    ap.add_argument("--timeout", type=int, default=600)
    args = ap.parse_args()

    scripts = discover()
    if args.only:
        keys = [k.strip() for k in args.only.split(",") if k.strip()]
        scripts = [s for s in scripts if any(k in s for k in keys)]
    if not scripts:
        print("没有匹配的前端门禁脚本（--only 写错了？）")
        return 2
    if args.list:
        declared = declared_js()
        for s in scripts:
            print(("  [清单]" if s in declared else "  [未登记]") + " " + s)
        return 0

    node = node_exe()
    if not node:
        print("找不到 node（PATH 与本机随包路径都没命中）⇒ 无法执行前端门禁")
        return 2

    declared = declared_js()
    undeclared = [s for s in scripts if s not in declared]
    if undeclared:
        print("⚠ 以下前端门禁未在 gates_manifest.json 登记（G5b 会判红）：%s" % undeclared)

    results = []
    t_all = time.time()
    for s in scripts:
        path = os.path.join(VDIR, s)
        t0 = time.time()
        try:
            p = subprocess.run([node, path], cwd=ROOT, timeout=args.timeout,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            rc = p.returncode
            tail = p.stdout.decode("utf-8", "replace").strip().splitlines()[-3:]
        except subprocess.TimeoutExpired:
            rc, tail = 124, ["timeout %ds" % args.timeout]
        except OSError as e:
            rc, tail = 125, [str(e)]
        state = "PASS" if rc == 0 else ("SKIP" if rc == 2 else "FAIL")
        results.append({"script": s, "rc": rc, "state": state,
                        "seconds": round(time.time() - t0, 1), "tail": tail})
        print("  [%-4s] %-42s rc=%-3d %6.1fs  %s"
              % (state, s, rc, time.time() - t0, (tail[-1] if tail else "")[:90]))

    n_pass = sum(1 for r in results if r["state"] == "PASS")
    n_skip = sum(1 for r in results if r["state"] == "SKIP")
    n_fail = sum(1 for r in results if r["state"] == "FAIL")
    elapsed = round(time.time() - t_all, 1)

    print("\n" + "=" * 70)
    print("前端门禁汇总：PASS %d / FAIL %d / SKIP %d（共 %d，%ss）"
          % (n_pass, n_fail, n_skip, len(results), elapsed))
    if n_fail:
        print("失败清单：%s" % [r["script"] for r in results if r["state"] == "FAIL"])
    if n_skip:
        print("跳过清单（环境不可用）：%s" % [r["script"] for r in results if r["state"] == "SKIP"])

    out = os.path.join(ROOT, "outputs", "_run_frontend_gates.json")
    try:
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            json.dump({"when": time.strftime("%Y-%m-%d %H:%M:%S"), "node": node,
                       "pass": n_pass, "fail": n_fail, "skip": n_skip,
                       "elapsed_s": elapsed, "results": results}, f, ensure_ascii=False, indent=1)
    except OSError:
        pass

    if n_fail or (args.strict and n_skip):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
