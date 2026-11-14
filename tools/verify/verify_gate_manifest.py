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
  G10    **nightly 不得在 PR 上执行**：含 nightly 门禁的 job 必须带
         `schedule` 闸门（`if:`）。2026-11-04 实测：拆了 job 但没加 `if:`，
         每个 PR 仍跑 21+14 步，且任一红就会让整个 run 判红。

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

#: G8 的**显式豁免**：脚本里出现外部地址，但不在 pr 执行路径上。
#: ⚠️ 2026-11-05：G8 原来是"全文正则一票否决"，无法区分「引用」与「依赖」——
#:   本仓已为此栽过（它给 verify_gate_manifest 自己做过自排除，但没推广）。
#:   三条实测都是**引用而非依赖**，理由必须可核验（不是"我知道它没问题"）：
#:     · t_live() 段：由 `--live` 开关保护，默认不执行（代码里写明 "CI 不执行"）
#:     · 反面测试：故意指向不可达端口，验证"返回 False 而非抛异常"
#: 豁免不是免检：这些脚本仍要在 gate_runner 的干净环境里**实测绿**才算数。
EXT_DEP_EXEMPT = {
    "verify_orch_span.py":
        "外部地址只在 t_live() 内，由 --live 开关保护，默认不执行（CI 不触发）",
    "verify_replan_p1.py":
        "同上：t_live() opt-in 段，默认不执行",
    "verify_alert_evaluator.py":
        "反面测试：故意指向 http://127.0.0.1:1/nope，验证不可达时返回 False 而非抛异常",
}


def rec(name, ok, detail=""):
    _results.append((PASS if ok else FAIL, name, "" if ok else str(detail)[:260]))
    return ok


def load():
    with open(MANIFEST, encoding="utf-8") as f:
        return json.load(f)


#: **门禁类**前缀：有 pass/fail 判据、可反复运行、结论可复现。
GATE_PREFIXES = ("verify_", "audit_", "e2e_")

#: **工具类**前缀：一次性操作或只读分析，没有「通过/失败」语义。
#: 为什么必须显式列出（2026-11-09 修）：原 `gate_files()` 只枚举 `verify_`/`audit_`，
#: 于是 `e2e_*.py`（10 个真机端到端门禁）**根本不进论域** ⇒ G5「无漏网」恒绿。
#: 这是「判据的论域有缺口，判据再严也管不到」——与本仓「单文件校验遮蔽真实错误」同族。
#: 修法不是无限扩大论域（把 `patch_*`/`fix_*` 这类一次性脚本也塞进清单），
#: 而是**显式二分类**：每个文件要么是门禁（必须在清单里）、要么命中工具前缀（须有理由）。
TOOL_PREFIXES = {
    "_": "被其它脚本 import 的辅助模块（下划线开头，不独立运行）",
    "ab_": "A/B 对照实验脚本（一次性，需人工判读结果）",
    "analyze_": "只读分析工具（输出报告给人看，无通过/失败语义）",
    "backfill_": "历史数据回填脚本（一次性执行）",
    "bench_": "基准测量脚本（产出数值，不设阈值）",
    "check_": "轻量人工巡检脚本（无门禁接线意图）",
    "clean_": "清理脚本（一次性维护操作）",
    "cleanup_": "清理脚本（一次性维护操作）",
    "compare_": "两组结果对比工具（人工判读）",
    "dedupe_": "去重维护脚本（一次性）",
    "diagnose_": "故障诊断脚本（人工排障用）",
    "eval_": "评测运行器（产出指标，门禁化在 tools/eval 与 verify_*）",
    "experiment_": "实验脚本（探索性，结论不稳定）",
    "extract_": "抽取工具（供其它流程消费，无判据）",
    "fix_": "一次性修复脚本（执行后即失效）",
    "fuseki_": "外部图库联调脚本（需 Fuseki 服务）",
    "kb_": "知识库维护脚本（一次性）",
    "patch_": "补丁脚本（一次性）",
    "payload_": "浏览器断言的 eval 载荷（由 verify_*.js 读取后在页面里执行，不独立运行；2026-11-13 论域含 .js 后补齐）",
    "preview_": "预览页生成工具（产出静态 HTML）",
    "probe_": "探针脚本（临时观测，非稳定判据）",
    "prune_": "裁剪脚本（一次性维护）",
    "restore_": "恢复脚本（一次性）",
    "retrieval_": "检索调参工具（人工判读）",
    "run_": "运行入口脚本（无判据）",
    "shot_": "截屏工具（产出图片证据）",
    "smoke_": "冒烟脚本（覆盖过窄，未达门禁标准）",
    "strip_": "裁剪脚本（一次性）",
    "sysml_": "SysML 处理工具（供流程调用）",
    "view_": "视图工具（产出数据，门禁化在 verify_*）",
}


def all_py_files():
    """tools/verify 下全部可执行脚本（不含 __init__ 等）。"""
    out = []
    for p in sorted(glob.glob(os.path.join(ROOT, "tools", "verify", "*.py"))):
        n = os.path.basename(p)
        if n.startswith("__"):
            continue
        out.append(n)
    return out


def all_frontend_files():
    """tools/verify 下的前端门禁脚本（`*.js`）。

    ★ 2026-11-13 论域扩展（**这条是本文件最容易被后人改回去的地方**）：
      原 `all_py_files()` 只扫 `.py`，而 `tools/verify/` 下已有 20+ 个
      `verify_*.js` 真实浏览器门禁 ⇒ 它们**根本不在 G5 的论域内**，
      「无漏网」判据对前端恒绿。这与 2026-11-09 那次「e2e_*.py 不在论域内」
      是同一个缺陷形态：**判据再严，论域有缺口就管不到**。
      配套：G5a/G5b 用本函数；变异 M8 锁死「不许再缩回只扫 .py」。
    """
    out = []
    for p in sorted(glob.glob(os.path.join(ROOT, "tools", "verify", "*.js"))):
        n = os.path.basename(p)
        if n.startswith("__"):
            continue
        out.append(n)
    return out


def all_gate_candidates():
    """G5 的完整论域：后端（.py）+ 前端（.js）。"""
    return all_py_files() + all_frontend_files()


def tool_prefix_of(name: str):
    """返回该文件命中的工具前缀；未命中返回 None。

    下划线前缀单独处理：只认「单下划线开头且不是双下划线」（双下划线已在
    all_py_files 里排除），避免 `_probe_x` 与 `__x` 混为一谈。
    """
    for p in TOOL_PREFIXES:
        if p == "_":
            if name.startswith("_") and not name.startswith("__"):
                return p
        elif name.startswith(p):
            return p
    return None


def gate_files():
    """门禁类文件（有 pass/fail 语义）。G5 与变异 M1/M6/M8 都用它。

    ★ 2026-11-13：论域含前端 `.js`（见 `all_frontend_files` 的说明）。
    """
    return [n for n in all_gate_candidates() if n.startswith(GATE_PREFIXES)]


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


def ci_jobs():
    """job 名 → {"if": 条件表达式, "gates": 该 job 会执行的门禁脚本名集合}。

    ── 为什么需要（2026-11-04 实测事故）──────────────────────────────
    把 nightly 门禁"拆成独立 job"**并不等于**"不再在 PR 上跑"：
    两个 nightly job 当时都没有 `if:` ⇒ 每次 PR 都会并行跑 21+14 步，
    而只要任一 nightly 门禁红，**整个 run 照样判红**（并行 ≠ 不阻断）。
    `ci_steps()` 只统计主 job（到 nightly-evals 为止），看不到这个面，
    所以 G6 一直是绿的 —— 与"元门禁只数 yml 里有没有这行"是同一类盲区。

    实现保持**逐行正则**（不引 PyYAML）：CI 环境不一定装 yaml，
    且本文件既有判据都按行口径，多一条依赖就多一个"CI 上 import 失败"的风险。
    """
    path = os.path.join(ROOT, ".github", "workflows", "ci.yml")
    with open(path, encoding="utf-8") as f:
        lines = f.readlines()

    jobs, cur, in_jobs = {}, None, False
    for ln in lines:
        s = ln.rstrip("\n")
        if s.rstrip() == "jobs:":
            in_jobs = True
            continue
        if not in_jobs:
            continue
        m = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", s)
        if m:
            cur = m.group(1)
            jobs[cur] = {"if": "", "gates": set()}
            continue
        if cur is None:
            continue
        t = s.strip()
        if t.startswith("#"):
            continue
        mi = re.match(r"^    if:\s*(.+)$", s)
        if mi:
            jobs[cur]["if"] = mi.group(1).strip()
        mg = re.search(r"python\b[^\n]*?tools/verify/([a-z_0-9]+)\.py", t)
        if mg:
            jobs[cur]["gates"].add(mg.group(1) + ".py")
    return jobs


def nightly_violations(jobs, nightly_gates):
    """在 PR/push 上会执行的 job 里，若含 nightly 门禁且无 schedule 闸门 ⇒ 违规。"""
    bad = []
    for name, info in jobs.items():
        if not (info.get("gates") & nightly_gates):
            continue
        if "schedule" not in str(info.get("if") or ""):
            bad.append("%s(if=%r)" % (name, info.get("if") or ""))
    return bad


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

    # G5 覆盖完整 —— 论域是**全部** tools/verify 脚本（.py + .js），不是只认几个前缀
    # （2026-11-09 修：原来只枚举 verify_/audit_，10 个 e2e_*.py 在论域外 ⇒ 恒绿）
    # （2026-11-13 再修：原来只扫 .py，20+ 个 verify_*.js 前端门禁在论域外 ⇒ 同样恒绿）
    classified = set(names)
    every = all_gate_candidates()
    unclassified = [f for f in every if f not in classified and tool_prefix_of(f) is None]
    rec("G5a 每个 tools/verify 脚本（.py+.js）都被分类或命中工具前缀（无漏网）",
        not unclassified,
        "未分类 %d 个: %s" % (len(unclassified), unclassified[:8]))
    # 门禁类前缀必须全部进清单：工具白名单绝不能把真门禁"吃掉"
    gate_missing = [f for f in gate_files() if f not in classified]
    rec("G5b 门禁类前缀（%s）全部登记在清单" % "/".join(GATE_PREFIXES),
        not gate_missing,
        "清单缺 %d 个: %s" % (len(gate_missing), gate_missing[:8]))
    # 反向：工具白名单里的前缀不得与门禁前缀重叠（防止一个前缀同时是两类）
    overlap = [p for p in TOOL_PREFIXES if p in GATE_PREFIXES]
    rec("G5c 工具前缀与门禁前缀不重叠", not overlap, overlap)

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
        if EXT_DEP.search(src) and g.get("script") not in EXT_DEP_EXEMPT:
            ext.append(g.get("script"))
    rec("G8 pr 层不含需要服务/LLM/浏览器的门禁", not ext, "疑似外部依赖: %s" % ext[:8])

    # G8b 豁免必须写理由（否则"加个文件名"就能绕过 —— 与 W1a 同一纪律）
    def _exempt_violations(d):
        return [k for k, v in d.items() if not str(v).strip()]

    bad_ex = _exempt_violations(EXT_DEP_EXEMPT)
    rec("G8b G8 的每条豁免都写了非空理由", not bad_ex, bad_ex[:6])

    # G8c 判据自证：合成两例必须一绿一红（防豁免机制把门禁变成恒真）
    _syn = {"a.py": "", "b.py": "有理由"}
    _viol = _exempt_violations(_syn)
    rec("G8c 判据自证（豁免有理由=绿 / 无理由=红）",
        ("a.py" in _viol) and ("b.py" not in _viol),
        "违规=%s（期望只有 a.py）" % _viol)

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

    # G10 nightly 门禁不得在 PR 上执行（2026-11-04 事故：见 ci_jobs 的注释）
    nightly_gates = {g.get("script") for g in gates if g.get("tier") == "nightly"}
    tier_of = {g.get("script"): g.get("tier") for g in gates}
    jobs = ci_jobs()
    bad = nightly_violations(jobs, nightly_gates)
    rec("G10 nightly 门禁所在 job 必须带 schedule 闸门（否则 PR 也会跑）",
        not bad, "缺 if: 的 job: %s" % bad[:4])

    # G10b 判据自证：合成两例，必须一绿一红（防判据写成恒真 / 恒假）
    _syn_pass = not nightly_violations(
        {"n": {"if": "github.event_name == 'schedule'", "gates": {"x.py"}}}, {"x.py"})
    _syn_fail = bool(nightly_violations(
        {"n": {"if": "", "gates": {"x.py"}}}, {"x.py"}))
    rec("G10b 判据自证（有 schedule 闸门=绿 / 无 if=红）",
        _syn_pass and _syn_fail,
        "有闸门判绿=%s 无 if 判红=%s" % (_syn_pass, _syn_fail))

    # G11 **反向一致**：CI 主 job 里只能有 pr 层（G6a 只管 pr⊆CI，这个方向没人管）
    #    2026-11-05 实测：主 job 跑着 213 个门禁脚本，其中 53 个不是 pr 层
    #    （broken 38 + local 12 + backlog 3）——「broken 记账不进 CI」这条约定
    #    只写在文档里，代码里从没生效，于是 CI 一边跑 38 条已知红一边判红。
    main = jobs.get("tests-and-guards") or {}
    non_pr = sorted(s for s in (main.get("gates") or set())
                    if tier_of.get(s) not in (None, "pr"))
    rec("G11 ci.yml 主 job 只跑 pr 层（清单是唯一真源）", not non_pr,
        "非 pr 层却在跑 %d 条: %s" % (len(non_pr), non_pr[:6]))

    # G11b 判据自证：合成样本必须能抓出 non-pr
    _t = {"x.py": "broken", "y.py": "pr"}
    _bad = [s for s in {"x.py", "y.py"} if _t.get(s) not in (None, "pr")]
    rec("G11b 判据自证（合成 broken 样本被判红）", _bad == ["x.py"], "got=%s" % _bad)


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

        # M6 **论域漏洞回归**：清单里删掉一条 e2e_ 门禁 ⇒ G5b 必须判红。
        # 为什么单列这条：2026-11-09 实测发现 `gate_files()` 只枚举 verify_/audit_，
        # 10 个 e2e_*.py 根本不在论域内 ⇒ G5「无漏网」恒绿。这条变异锁死那个缺陷：
        # 只要有人把枚举范围再缩窄（或漏登记 e2e），M6 立即变红。
        mm = copy.deepcopy(base)
        _drop = [g for g in mm["gates"] if str(g.get("script", "")).startswith("e2e_")]
        if _drop:
            mm["gates"] = [g for g in mm["gates"] if not str(g.get("script", "")).startswith("e2e_")]
            f = rerun(mm)
            _mut.append(("M6 删掉 e2e_ 门禁 ⇒ G5b 判红（论域不得缩窄）",
                         any(x[1].startswith("G5b") for x in f),
                         "、".join(x[1][:26] for x in f[:3])))
        # M7 **工具白名单不得吃掉门禁**：把一个 verify_ 门禁从清单移除，
        # 它不命中任何工具前缀 ⇒ G5a 必须判红（而不是被静默放过）。
        mm = copy.deepcopy(base)
        _vg = [g for g in mm["gates"] if str(g.get("script", "")).startswith("verify_")]
        if _vg:
            mm["gates"] = [g for g in mm["gates"] if g.get("script") != _vg[0]["script"]]
            f = rerun(mm)
            _mut.append(("M7 删掉 verify_ 门禁 ⇒ G5a 判红",
                         any(x[1].startswith("G5a") for x in f),
                         "、".join(x[1][:26] for x in f[:3])))
        # M8 **前端论域不得缩窄**（2026-11-13 新增）：把清单里唯一的 `.js` 门禁删掉，
        #    G5b 必须判红。为什么单列：`all_py_files()` 只扫 `.py` 的那个缺口，
        #    让 20+ 个 `verify_*.js` 长期不被 G5 管到；只要有人把论域改回「只扫 .py」，
        #    或者把 `.js` 从清单里删掉，这条变异立即变红。
        mm = copy.deepcopy(base)
        _js = [g for g in mm["gates"] if str(g.get("script", "")).endswith(".js")]
        if _js:
            mm["gates"] = [g for g in mm["gates"] if not str(g.get("script", "")).endswith(".js")]
            f = rerun(mm)
            _mut.append(("M8 删掉 .js 前端门禁 ⇒ G5b 判红（前端论域不得缩窄）",
                         any(x[1].startswith("G5b") for x in f),
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
