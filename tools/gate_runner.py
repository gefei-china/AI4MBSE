#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""门禁执行器：按**清单**跑门禁，保证「声明的都真的跑过」，并产出可机读报告。

## 为什么需要它（2026-11-04 盘点实证）

`ci.yml` 里写着 193 条 `python tools/verify/xxx.py`，看起来覆盖很全。但实测
GitHub Actions run（master #33 / PR #43）：

    总步数 187~208，成功 11，失败 1，**跳过 175~197**

GitHub Actions 遇到失败步骤即中止后续步骤 ⇒ **第一个红门禁把后面 190+ 个全部挡掉**。
⇒ 「写在 ci.yml 里」≠「跑过」。这三年里 90% 的门禁**一次都没在 CI 上执行过**。

本执行器把这件事翻转过来：
  · 清单（`tools/gates_manifest.json`）声明了哪些门禁属于哪一层；
  · 每次运行**逐条跑完**（除非 `--fail-fast`），单条超时只影响自己；
  · 结束时断言 `已尝试条数 == 清单条数`，不等则退出码 2（**执行完整性**失败）；
  · 产出 JSON 报告，供元门禁与趋势对比消费。

## 分层（tier）

| tier | 含义 | 跑在哪 |
|---|---|---|
| `pr`       | 快、确定性、零外部依赖；**必须全绿** | 每次 push / PR |
| `nightly`  | 慢（>15s）或依赖较大存量数据，但确定性 | 夜间 + 手动 |
| `local`    | 需真实服务 / 真实 LLM / 生产真库金标；**CI 跑不了** | 开发机，带理由 |

`local` 必须写 `reason`：没有理由的排除等于把门禁变成摆设。

## 用法

    python tools/gate_runner.py                    # 跑 pr 层
    python tools/gate_runner.py --tier pr,nightly   # 跑两层
    python tools/gate_runner.py --list              # 只列不跑（供元门禁对账）
    python tools/gate_runner.py --only s4           # 只跑名字含 s4 的
    python tools/gate_runner.py --json out.json     # 产出报告
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST = os.path.join(ROOT, "tools", "gates_manifest.json")
TIERS = ("pr", "nightly", "local")


def load_manifest(path=MANIFEST):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def select(manifest, tiers):
    """按层筛选，保持清单顺序；返回 [(entry, abspath)]。"""
    out = []
    for e in manifest.get("gates") or []:
        if e.get("tier") not in tiers:
            continue
        rel = e.get("script") or ""
        out.append((e, os.path.join(ROOT, "tools", "verify", rel)))
    return out


def _decode(b):
    return (b or b"").decode("utf-8", "replace")


def run_one(path, timeout, db_path=None):
    env = {k: v for k, v in os.environ.items()}
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    # 2026-11-09：凡由门禁运行器派生的进程一律标记为 verify 来源。
    # 为什么必须做：pr 层与债务门禁里确有脚本会真实调用
    # `request_invariant.assert_before_dispatch`，默认会写生产台账
    # `logs/request_invariant.jsonl`；而「shadow 确认无误报后切 enforce」的
    # 就绪判据只看真实流量，混入 CI 事件会凑出假 READY。
    # 单点标记覆盖全部门禁，无需逐个脚本改（子进程会继承）。
    env["MBSE_INVARIANT_ORIGIN"] = "verify"
    if db_path:
        # ★★ 2026-11-15 实测事故：本仓**两种库路径口径并存**，只设一个会让隔离对另一半失效。
        #   · 多数门禁读 `core.config.DB_PATH` ⇒ 环境变量是 **MBSE_DB_PATH**
        #   · 少数门禁自己在模块顶部取库路径 ⇒ 读的是 **MBSE_GATE_DB**
        #     （verify_citation_registry / verify_citation_clause_link / verify_auto_continue_stop …）
        #   只设 MBSE_DB_PATH 时，后者**仍然连真库** ——
        #   做"干净库对照"会得到 **「全 PASS」的假绿**（本轮实测踩到：
        #   `MBSE_GATE_DB=/tmp/_no_such.db verify_artifact_naming.py` 是 33 全 PASS，
        #   因为该门禁读的是 MBSE_DB_PATH，那个变量没被改 ⇒ 一直在连真库）。
        #   两个都设，口径就对上了；不新增第三种。
        env["MBSE_DB_PATH"] = db_path
        env["MBSE_GATE_DB"] = db_path
    t0 = time.time()
    try:
        r = subprocess.run([sys.executable, "-X", "utf8", path], cwd=ROOT,
                           capture_output=True, env=env, timeout=timeout)
        rc, out, err = r.returncode, _decode(r.stdout), _decode(r.stderr)
    except subprocess.TimeoutExpired as e:
        rc, out, err = 124, _decode(e.stdout), _decode(e.stderr)
    ms = int((time.time() - t0) * 1000)
    lines = [l.strip() for l in (out + "\n" + err).splitlines() if l.strip()]
    fails = [l for l in lines if l.startswith("FAIL") or "[FAIL]" in l][:3]
    tb = ""
    for i, l in enumerate(lines):
        if "Traceback (most recent call last)" in l:
            tb = " | ".join(lines[i:][-3:])[:400]
            break
    status = "PASS" if rc == 0 else ("TIMEOUT" if rc == 124 else "FAIL")
    return {"rc": rc, "ms": ms, "status": status, "fails": fails, "tb": tb,
            "tail": lines[-2:] if lines else []}


def main(argv=None):
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--tier", default="pr", help="逗号分隔：pr,nightly,local 或 all")
    ap.add_argument("--list", action="store_true", help="只列将执行的脚本名（不跑）")
    ap.add_argument("--only", default="", help="只跑脚本名含该子串的")
    ap.add_argument("--json", default="", help="报告写入路径")
    ap.add_argument("--fail-fast", action="store_true")
    ap.add_argument("--timeout-cap", type=int, default=0, help="覆盖清单里的超时上限")
    ap.add_argument("--isolate-db", action="store_true",
                    help="每个门禁用 base 库的一份独立副本（消除锁竞争与顺序污染）")
    ap.add_argument("--base-db", default="", help="隔离模式下的源库（默认取 MBSE_DB_PATH）")
    args = ap.parse_args(argv)

    if not os.path.isfile(MANIFEST):
        print("[gate_runner] ✗ 清单不存在: %s" % MANIFEST)
        return 2
    manifest = load_manifest()
    tiers = TIERS if args.tier.strip() == "all" else tuple(
        t.strip() for t in args.tier.split(",") if t.strip())
    bad = [t for t in tiers if t not in TIERS]
    if bad:
        print("[gate_runner] ✗ 未知 tier: %s（可选 %s）" % (bad, list(TIERS)))
        return 2

    items = select(manifest, tiers)
    if args.only:
        items = [(e, p) for e, p in items if args.only in (e.get("script") or "")]

    if args.list:
        for e, _p in items:
            print(e.get("script"))
        print("# total=%d tier=%s" % (len(items), ",".join(tiers)), file=sys.stderr)
        return 0

    declared = len(items)
    print("=" * 78)
    print("门禁执行：tier=%s  声明 %d 条  cwd=%s" % (",".join(tiers), declared, ROOT))
    print("=" * 78)

    results, missing = [], []
    t_all = time.time()
    for i, (e, path) in enumerate(items, 1):
        name = e.get("script") or "?"
        if not os.path.isfile(path):
            missing.append(name)
            print("  [%3d/%d] MISSING  %-46s（清单声明但文件不存在）" % (i, declared, name[:46]))
            results.append({"script": name, "status": "MISSING", "rc": -1, "ms": 0,
                            "fails": [], "tb": ""})
            if args.fail_fast:
                break
            continue
        cap = e.get("timeout") or 180
        if args.timeout_cap:
            cap = min(cap, args.timeout_cap)

        # 隔离模式：每个门禁拿一份**独立的库副本**
        # 为什么：实测同一份干净库被 120 个门禁串行共用时，出现
        #   `sqlite3.OperationalError: database is locked`（无判据意义的随机红）。
        # 锁竞争 + 上一个门禁的残留写都会污染下一个（顺序依赖）。
        # 隔离后每个门禁从**同一份基准状态**出发，结论可复现。
        #
        # ⚠️ 用**单一固定路径反复覆盖**，不要每步新建+删除：
        #   一条轮内删 100+ 个临时文件会触发宿主机的批量删除保护
        #   （实测：跑到第 38 条被打断，[safe-delete] 需要人工确认）。
        #   复用同一路径 ⇒ 全程只删 2~3 次（开跑前清一次、收尾清一次）。
        _iso = None
        if args.isolate_db:
            base = args.base_db or os.environ.get("MBSE_DB_PATH") or ""
            # ★★★ 2026-11-15 实测事故：`--isolate-db` **静默失效**，200 个门禁共用真库
            #   ⇒ 顺序污染造出 6 个**假红**（verify_misevolution / verify_model_profile_fields /
            #   verify_multi_project_scope / verify_p1_project_attribution / verify_setting_keys_guard
            #   + 1），报错是 `SyntaxError: unterminated ...`（前序门禁写的脏数据被后序读出来 exec）。
            #   这 6 个**单独跑全部全绿**，且 CI 是"每门禁独立进程 + 新建空库"⇒ 本地红、CI 绿。
            #   根因：`MBSE_DB_PATH` 未设置时 `base=""` ⇒ 原实现只是**跳过复制**、
            #   悄悄退化成"所有门禁共用默认真库"，用户看不到任何提示。
            #   ⇒ 请求了隔离就必须真隔离：拿不到基准库**直接报错退出**，
            #     不再"静默降级"（静默兜底不是稳健，是把错误变成不可见）。
            if not base:
                print("[gate_runner] ✗ --isolate-db 需要基准库，但 MBSE_DB_PATH 未设置。\n"
                      "    请显式指定，例如：MBSE_DB_PATH=mbse.db python tools/gate_runner.py "
                      "--tier pr --isolate-db\n"
                      "    （不指定会静默退化成“所有门禁共用真库” ⇒ 顺序污染 ⇒ 假红）")
                return 2
            if not os.path.isfile(base):
                print("[gate_runner] ✗ --isolate-db 的基准库不存在: %s" % base)
                return 2
            _iso = os.path.join(ROOT, "tmp", "_gate_iso.db")
            if i == 1:                      # 只在开跑时清一次残留
                for _suf in ("", "-wal", "-shm"):
                    if os.path.isfile(_iso + _suf):
                        os.remove(_iso + _suf)
            for _suf in ("-wal", "-shm"):    # 覆盖前清掉上一轮的侧车文件
                if os.path.isfile(_iso + _suf):
                    os.remove(_iso + _suf)
            shutil.copy2(base, _iso)

        r = run_one(path, cap, db_path=_iso)
        r["script"] = name
        r["tier"] = e.get("tier")
        r["isolated"] = bool(_iso)
        results.append(r)
        print("  [%3d/%d] %-7s %6.1fs  %-46s %s" % (
            i, declared, r["status"], r["ms"] / 1000.0, name[:46],
            (r["fails"][0] if r["fails"] else "")[:60]), flush=True)
        if args.fail_fast and r["status"] != "PASS":
            print("  --fail-fast：已停止（后续 %d 条未执行）" % (declared - i))
            break

    elapsed = time.time() - t_all
    executed = len(results)
    passed = sum(1 for r in results if r["status"] == "PASS")
    failed = [r for r in results if r["status"] == "FAIL"]
    timeout = [r for r in results if r["status"] == "TIMEOUT"]

    print("=" * 78)
    print("汇总：声明 %d / 已执行 %d / 通过 %d / 失败 %d / 超时 %d / 缺失 %d  用时 %.1fs"
          % (declared, executed, passed, len(failed), len(timeout), len(missing), elapsed))
    if failed or timeout:
        print("-" * 78)
        for r in failed + timeout:
            print("  ✗ %-9s %-44s %s" % (r["status"], r["script"][:44],
                                          (r["fails"][0] if r["fails"] else r.get("tb", ""))[:70]))
    print("=" * 78)

    # 执行完整性：声明的必须都尝试过（除 --fail-fast 主动中断）
    integrity_ok = (executed == declared)
    if not integrity_ok and not args.fail_fast:
        print("★ 执行完整性失败：声明 %d 条但只执行 %d 条（有门禁被静默跳过）"
              % (declared, executed))

    if args.json:
        p = args.json if os.path.isabs(args.json) else os.path.join(ROOT, args.json)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            json.dump({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "tier": list(tiers),
                       "declared": declared, "executed": executed, "passed": passed,
                       "failed": [r["script"] for r in failed],
                       "timeout": [r["script"] for r in timeout],
                       "missing": missing, "ms_total": int(elapsed * 1000),
                       "results": results}, f, ensure_ascii=False, indent=1)
        print("报告：%s" % p)

    if missing or not integrity_ok:
        return 2
    return 0 if (not failed and not timeout) else 1


if __name__ == "__main__":
    sys.exit(main())
