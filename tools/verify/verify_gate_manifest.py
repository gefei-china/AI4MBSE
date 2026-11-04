#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""元门禁：门禁清单必须**完整、可执行、不许作弊**（2026-11-04 盘点新增）。

## 为什么需要它（这次盘点的核心结论）

`verify_gate_wiring` 已经要求「每个门禁要么进 CI、要么写豁免」。但它只回答
**"写在 ci.yml 里了吗"**，回答不了 **"跑过吗"**。实测 GitHub Actions：

    master #33：187 步 → 成功 11 / 失败 1 / **跳过 175**
    PR    #43：208 步 → 成功 10 / 失败 1 / **跳过 197**

⇒ 第一个红门禁把后面 190+ 个全部挡掉。三年里绝大多数门禁**一次都没执行过**，
而 `verify_gate_wiring` 一直是绿的（它数的是 yml 行数）。这是本工程最典型的
「判据面选错了」——判的是声明，不是事实。

本门禁把三件事变成可断言的：

  G1~G4  清单自身完整（每条文件存在、tier 合法、非 pr 层必带理由、无重复）
  G5     覆盖完整：`tools/verify/` 下的每个门禁都必须被分类（不许有漏网）
  G6     **清单↔CI 绑定**：清单里每个 `pr` 条目都必须在 ci.yml 的执行行里
         （防「清单说它该在 pr 层，CI 却没跑它」）；同时断言
         `continue-on-error` 的步骤数 ≤ budget.broken
         （**已知红必须记账**，不能随手加个容错就完事）
  G7     pr 层不许为空（防「把门禁全部降级成 nightly/local」这种一键变绿）
  G8     pr 层不许含外部依赖（防把需要服务/LLM 的门禁塞进 pr，让 CI 恒红）
  G9     **债务上限**：broken / backlog 条数不得超过 budget（只降不升）
         —— 把红门禁搬出 CI 换绿灯是最容易也最坏的作弊；账必须记下来。

## 为什么需要 broken / backlog（而不是"搬走就绿了"）

盘点实测：193 条进 CI 的门禁里，**52 条在 CI 等价口径下就是红的**（不是环境差异）。
把它们从 CI 挪走确实能立刻变绿，但那是把缺陷藏起来。本清单用 `broken` 显式记账：
带失败签名、数量受 budget 约束、`gate_runner --tier broken` 随时复现。
⇒ 债务可见、可追踪、只能变少。

## 变异自证（每条判据都要能判红）

  M1 删掉一条 pr 条目      ⇒ G5/G6 必须红
  M2 pr 改 local 但不写理由 ⇒ G3 必须红
  M3 加一条不存在的文件     ⇒ G2 必须红
  M4 清空 pr 层            ⇒ G7 必须红
"""
from __future__ import annotations

import glob
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MANIFEST = os.path.join(ROOT, "tools", "gates_manifest.json")
RUNNER = os.path.join(ROOT, "tools", "gate_runner.py")
TIERS = ("pr", "nightly", "local", "broken", "backlog")
NEED_REASON = ("nightly", "local", "broken", "backlog")

PASS, FAIL = "PASS", "FAIL"
_results = []
_mut = []

# 外部依赖信号：出现在 pr 层的门禁里 ⇒ CI（无服务/无 secret）上必然红
EXT_DEP = re.compile(
    r"127\.0\.0\.1:8000|localhost:8000|https?://127\.0\.0\.1|"
    r"api_key\s*=\s*['\"][A-Za-z0-9]{16,}|os\.environ\[['\"]OPENAI_API_KEY['\"]\]|"
    r"EventSource|playwright|agent-browser")


def rec(name, ok, detail=""):
    _results.append((PASS if ok else FAIL, name, "" if ok else str(detail)[:260]))
    return ok


def load():
    with open(MANIFEST, encoding="utf-8") as f:
        return json.load(f)


def gate_files():
    out = []
    for p in sorted(glob.glob(os.path.join(ROOT, "tools", "verify", "*.py"))):
        n = os.path.basename(p)
        if n.startswith("__"):
            continue
        if n.startswith("verify_") or n.startswith("audit_"):
            out.append(n)
    return out


def ci_steps():
    """ci.yml 里**真正会执行**的门禁脚本名 + continue-on-error 的步骤数。

    判据取「非注释行里出现 python tools/verify/X.py」，与 verify_gate_wiring 同口径。
    （ci.yml 里注释掉的不算执行 —— 本仓踩过：注释里的命令行被当成了接线。）
    """
    path = os.path.join(ROOT, ".github", "workflows", "ci.yml")
    with open(path, encoding="utf-8") as f:
        lines = f.readlines()

    # 只统计主 job（tests-and-guards）里的**步骤级**容错：
    #   ① nightly-evals 是独立 job，本身 job 级就有 continue-on-error（有意设计），
    #      那不是"已知红容错"，算进来会虚高预算；
    #   ② 步骤级是 8 空格缩进，job 级是 4 空格 —— 缩进是唯一的区分信号。
    end = len(lines)
    for i, ln in enumerate(lines):
        if ln.rstrip() == "  nightly-evals:":
            end = i
            break
    main = lines[:end]

    names, n_soft = set(), 0
    for ln in main:
        s = ln.strip()
        if s.startswith("#"):
            continue
        # ⚠️ 必须容忍 `python -X utf8 tools/verify/x.py`（本仓大量用这种形态）
        #    和 `python tools/verify/x.py`。早先只匹配后者 ⇒ 把 L1431 那条
        #    （`python -X utf8 ...`）漏成"清单说该跑、CI 里没有"的假红。
        m = re.search(r"python\b[^\n]*?tools/verify/([a-z_0-9]+)\.py", s)
        if m:
            names.add(m.group(1) + ".py")
        if ln.startswith("        continue-on-error:") and "true" in s.lower():
            n_soft += 1
    return names, n_soft


def check_all(m):
    gates = m.get("gates") or []

    # G1 结构
    rec("G1 清单结构与版本合法",
        isinstance(gates, list) and len(gates) > 0 and m.get("version") == 1,
        "gates=%s version=%s" % (len(gates), m.get("version")))

    # G2 文件存在
    miss = [g.get("script") for g in gates
            if not os.path.isfile(os.path.join(ROOT, "tools", "verify", str(g.get("script"))))]
    rec("G2 清单里每个 script 都真实存在", not miss, "缺失: %s" % miss[:8])

    # G3 tier 合法 + 非 pr 层必须带理由（没有理由的排除等于把门禁变成摆设）
    illegal, noreason = [], []
    for g in gates:
        t = g.get("tier")
        if t not in TIERS:
            illegal.append((g.get("script"), t))
        elif t in NEED_REASON and not str(g.get("reason") or "").strip():
            noreason.append(g.get("script"))
    rec("G3a tier 取值合法（%s）" % "/".join(TIERS), not illegal, illegal[:8])
    rec("G3b 非 pr 层必须写理由（local/nightly/broken/backlog）", not noreason, noreason[:8])

    # G4 无重复
    names = [g.get("script") for g in gates]
    dup = sorted({n for n in names if names.count(n) > 1})
    rec("G4 清单无重复条目", not dup, dup[:8])

    # G5 覆盖完整
    classified = set(names)
    unclassified = [f for f in gate_files() if f not in classified]
    rec("G5 tools/verify 下每个门禁都被分类（无漏网）", not unclassified,
        "未分类 %d 个: %s" % (len(unclassified), unclassified[:8]))

    # G6 清单↔CI 绑定：pr 层必须真的被 CI 执行（不是"清单里写着"）
    in_ci, n_soft = ci_steps()
    declared = {g.get("script") for g in gates if g.get("tier") == "pr"}
    not_in_ci = sorted(declared - in_ci)
    rec("G6a 清单 pr 层的每条都在 ci.yml 执行行里", not not_in_ci,
        "清单说该跑、CI 里没有 %d 条: %s" % (len(not_in_ci), not_in_ci[:8]))

    # G6b 已知红必须记账：CI 里继续容错的步骤数不得超过预算
    #     注：同一门禁可能有两种调用模式（如 verify_layering 自检 + 基线检查），
    #     故预算分两项：broken=门禁条目数，tolerated_steps=ci.yml 容错步数上限。
    budget = m.get("budget") or {}
    cap_broken = int(budget.get("broken") or 0)
    cap_steps = int(budget.get("tolerated_steps") or cap_broken)
    rec("G6b ci.yml 里 continue-on-error 的步骤数 ≤ 预算",
        n_soft <= cap_steps,
        "ci.yml 容错 %d 步 > 预算 %d（修好了要一起下调 budget）" % (n_soft, cap_steps))

    # G7 pr 层非空
    rec("G7 pr 层不得为空（否则等于关掉门禁）", len(declared) > 0, "pr=%d" % len(declared))

    # G8 pr 层不含外部依赖
    #   ⚠️ 必须排除**本文件自己**：它定义了 EXT_DEP 这个正则，
    #      源码里必然含 `127.0.0.1:8000` 等字面量 ⇒ 不自排除就会自命中（假红）。
    #      这与"门禁检查自己的源码"是同族问题：判据要区分「引用」与「定义」。
    ext = []
    for g in gates:
        if g.get("tier") != "pr":
            continue
        if g.get("script") == "verify_gate_manifest.py":
            continue
        p = os.path.join(ROOT, "tools", "verify", str(g.get("script")))
        try:
            src = open(p, encoding="utf-8", errors="replace").read()
        except Exception:
            continue
        if EXT_DEP.search(src):
            ext.append(g.get("script"))
    rec("G8 pr 层不含需要服务/LLM/浏览器的门禁", not ext, "疑似外部依赖: %s" % ext[:8])

    # G9 债务上限：broken / backlog 只降不升（防「搬走换绿」）
    budget = m.get("budget") or {}
    from collections import Counter
    cnt = Counter(g.get("tier") for g in gates)
    over = []
    for k in ("broken", "backlog"):
        cap = budget.get(k)
        if cap is None:
            over.append("%s 未登记 budget" % k)
        elif cnt[k] > int(cap):
            over.append("%s=%d > budget=%s" % (k, cnt[k], cap))
    rec("G9 债务（broken/backlog）不超过 budget（只降不升）", not over,
        "；".join(over) + "（真修好了就把 budget 下调，别只改数字）")


def mutations(m):
    """变异自证：改坏清单，判据必须红。"""
    import copy
    base = copy.deepcopy(m)

    def rerun(mm):
        _results.clear()
        check_all(mm)
        return [r for r in _results if r[0] == FAIL]

    try:
        # M1 删掉一条 pr
        mm = copy.deepcopy(base)
        pr = [g for g in mm["gates"] if g.get("tier") == "pr"]
        if pr:
            mm["gates"].remove(pr[0])
            f = rerun(mm)
            _mut.append(("M1 删一条 pr 条目 ⇒ G5/G6 判红",
                         any(x[1].startswith("G5") or x[1].startswith("G6") for x in f),
                         "、".join(x[1][:26] for x in f[:3])))

        # M2 pr 改 local 但不写理由
        mm = copy.deepcopy(base)
        pr = [g for g in mm["gates"] if g.get("tier") == "pr"]
        if pr:
            pr[0]["tier"] = "local"
            pr[0].pop("reason", None)
            f = rerun(mm)
            _mut.append(("M2 pr→local 无理由 ⇒ G3b 判红",
                         any(x[1].startswith("G3b") for x in f),
                         "、".join(x[1][:26] for x in f[:3])))

        # M3 加一条不存在的文件
        mm = copy.deepcopy(base)
        mm["gates"].append({"script": "__ghost_gate_for_mutation__.py", "tier": "pr"})
        f = rerun(mm)
        _mut.append(("M3 加幽灵文件 ⇒ G2 判红",
                     any(x[1].startswith("G2") for x in f),
                     "、".join(x[1][:26] for x in f[:3])))

        # M4 清空 pr
        mm = copy.deepcopy(base)
        for g in mm["gates"]:
            if g.get("tier") == "pr":
                g["tier"] = "nightly"
                g["reason"] = "mutation"
        f = rerun(mm)
        _mut.append(("M4 清空 pr 层 ⇒ G7 判红",
                     any(x[1].startswith("G7") for x in f),
                     "、".join(x[1][:26] for x in f[:3])))

        # M5 债务越界（把 budget 调低到实际值以下）⇒ G9 判红
        mm = copy.deepcopy(base)
        mm.setdefault("budget", {})
        from collections import Counter as _C
        c = _C(g.get("tier") for g in mm["gates"])
        if c["broken"] > 0:
            mm["budget"]["broken"] = c["broken"] - 1
            f = rerun(mm)
            _mut.append(("M5 broken 超 budget ⇒ G9 判红",
                         any(x[1].startswith("G9") for x in f),
                         "、".join(x[1][:26] for x in f[:3])))
    finally:
        _results.clear()
        check_all(base)


def main():
    print("=" * 76)
    print("门禁清单元门禁（完整性 / 执行完整性 / 防作弊）")
    print("=" * 76)
    if not os.path.isfile(MANIFEST):
        print("  [FAIL] 清单不存在: %s" % MANIFEST)
        return 1
    m = load()
    check_all(m)
    for st, name, detail in _results:
        print("  [%s] %s%s" % (st, name, ("  <- " + detail) if detail else ""))
    n_fail = sum(1 for r in _results if r[0] == FAIL)

    print("\n--- 变异自证 ---")
    mutations(m)
    for name, ok, detail in _mut:
        print("  [%s] %s%s" % (PASS if ok else FAIL, name, ("  <- " + detail) if detail else ""))
    n_mut_fail = sum(1 for x in _mut if not x[1])

    print("\n" + "=" * 76)
    if n_fail or n_mut_fail or not _mut:
        print("结论：门禁未通过（常态 %d 条失败 / 变异 %d 组未判红）" % (n_fail, n_mut_fail))
        return 1
    print("结论：全部通过（常态 %d 条 + 变异 %d 组全部按预期判红）" % (len(_results), len(_mut)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
