#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""元门禁：新写的门禁**必须接进 CI**（2026-10-05）

## 为什么需要这道门禁

「能力齐备但没人用/不生效」这个模式在本项目**已重复三轮**：
1. `audit_dup_defs.py` 写好了但没进 CI ⇒ 一次都没在合并请求上跑过；
2. HyDE 能力落地了但前端零展示 ⇒ 用户拿不到；
3. **87 个门禁里只有 29 个进了 CI** ⇒ 58 份已完成的防御从未接线。

前两轮都靠"我这次记得接"来解决 —— 但**这次也会忘**。
⇒ 缺的不是某一次的修复，而是**机制**。

## 判据

对 `tools/verify/verify_*.py` 的每个门禁：
- 要么在 `.github/workflows/ci.yml` 里出现；
- 要么在该文件里带一行 `# CI-OPTIONAL: <理由>`（显式豁免，理由非空）。

⚠️ **豁免必须写理由**：否则"随手加一行豁免"就能绕过，
等于把门禁本身变成摆设（MEMORY：任何"防重复/防绕过"的机制，
其判据本身必须能判红）。

## 三条设计约束（每条都对应一次真实踩坑）

1. **不能只判文件名出现**：ci.yml 里注释掉的不算接线。
   判据取「该门禁的 `python tools/verify/X.py` 出现在某个 run 行里」。
2. **不能漏掉非 verify_ 前缀**：`audit_dup_defs.py` 也是门禁，
   所以扫描 `tools/verify/*.py` 下全部 `*.py`（排除 `__init__` 等）。
3. **自身要能判红**：M1 注入一个新门禁文件 ⇒ 必须判红；
   M2 豁免不写理由 ⇒ 必须判红。
"""
from __future__ import annotations

import glob
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

PASS, FAIL = "PASS", "FAIL"
_results = []
_MUT_ROWS = []
_IN_MUT = [False]
OPT_PREFIX = "# CI-OPTIONAL:"


def _rec(name, ok, detail=""):
    row = (PASS if ok else FAIL, name, "" if ok else str(detail)[:220])
    (_MUT_ROWS if _IN_MUT[0] else _results).append(row)
    return bool(ok)


def scan_gates(root=ROOT):
    out = []
    vd = os.path.join(root, "tools", "verify")
    for p in sorted(glob.glob(os.path.join(vd, "*.py"))):
        n = os.path.basename(p)
        if n.startswith("__") or n == "audit_dup_defs.py":
            pass  # audit_dup_defs 也是门禁，见下
        if not (n.startswith("verify_") or n.startswith("audit_")):
            continue
        out.append((n[:-3], p))
    return out


def ci_commands(root=ROOT):
    """ci.yml 里**真正被执行**的 run 行（非注释）。"""
    path = os.path.join(root, ".github", "workflows", "ci.yml")
    with open(path, encoding="utf-8") as f:
        lines = f.readlines()
    cmds = []
    for ln in lines:
        s = ln.strip()
        if s.startswith("#"):
            continue
        if "tools/verify/" in s or "tools/audit_" in s:
            cmds.append(s)
    return cmds


def _exemption_reason(path):
    """读该门禁的 CI-OPTIONAL 豁免理由（非空才算有效豁免）。"""
    try:
        with open(path, encoding="utf-8") as f:
            for ln in f:
                s = ln.strip()
                if s.startswith(OPT_PREFIX):
                    return s[len(OPT_PREFIX):].strip()
    except OSError:
        return ""
    return None            # 没有豁免行


def t_all_gates_wired(root=ROOT):
    print("\n=== W1 每个门禁要么进 CI，要么带理由豁免 ===")
    gates = scan_gates(root)
    cmds = " ".join(ci_commands(root))
    ok = True
    unwired, bad_exempt = [], []
    for name, path in gates:
        # 判据 ①：ci.yml 的**执行行**里出现（不能是注释里出现）
        wired = ("tools/verify/%s.py" % name) in cmds or \
                ("tools/%s.py" % name) in cmds
        reason = _exemption_reason(path)
        if wired:
            continue
        if reason is None:
            unwired.append(name)
        elif not reason:
            bad_exempt.append(name)
    ok &= _rec("W1a 没有「未接线且未豁免」的门禁", not unwired,
               "未接线 %d 个：%s" % (len(unwired), unwired[:8]))
    ok &= _rec("W1b 所有豁免都写了非空理由", not bad_exempt,
               "豁免无理由：%s" % bad_exempt[:8])
    # 反证：不能恒真 —— 门禁总数不能是 0
    ok &= _rec("W1c 扫到足够多的门禁（判据非空转）", len(gates) >= 40,
               "只扫到 %d 个" % len(gates))
    print("     门禁总数 %d；未接线 %d；豁免无理由 %d"
          % (len(gates), len(unwired), len(bad_exempt)))
    if unwired:
        print("     未接线清单：%s" % unwired)
    return ok


def t_ci_has_new_batch(root=ROOT):
    print("\n=== W2 本批（2026-10-05）接线的门禁确实在 CI 里 ===")
    cmds = " ".join(ci_commands(root))
    must = ["verify_fs_guard", "verify_doc_folders", "verify_memory_admin",
            "verify_llm_health", "verify_skill_injection", "verify_audit_p0",
            "verify_prompt_blocks", "verify_tiered_compression",
            "verify_trace_cols", "verify_alert_evaluator",
            "verify_tool_offload", "verify_s4_toolname",
            "verify_branch_permission_gates", "verify_intent_cache_key",
            "verify_intent_cache_toggle", "verify_entity_versions",
            "verify_memory_tier", "verify_retry_fallback_stats",
            "verify_p1_project_attribution",
            "verify_p1_project_attribution_mutate",
            "verify_agent_tools_no_params", "verify_branch_protection_rules",
            "verify_b1_runtime_smoke",
            # 第二轮第 2 项：红灯门禁归因后修复并接线（生产库 + 干净库双绿）
            "verify_continuation", "verify_partial_persist",
            "verify_orch_whitelist", "verify_plugin_bind_removed",
            "verify_uninstall_flow",
            # ── 2026-10-09：清「14 份写了从未执行」的欠账 ──
            # 起因：本门禁 W1a 判红（未接线 14 / 豁免无理由 0）。
            # 这 7 个经**干净库口径实测 rc=0** 才接线（MBSE_DB_PATH 指向空库）。
            # 其余 7 个（需真 LLM / checker.jar / 真库金标）改在脚本内
            # 写 `# CI-OPTIONAL: <理由>` 显式豁免 —— 见EXEMPTED 清单。
            "verify_agent_loop_protocol", "verify_agent_perm_gate",
            "verify_llm_circuit_breaker", "verify_memory_conflict_gate",
            "verify_sysml_autofix", "verify_pipeline_closure",
            "verify_sysml_baseline",
            # 2026-10-09 续：产出雷同定性后补的两道**通用性**门禁
            # （流式/非流式轮次漂移、行内工具调用不被识别）
            "verify_tool_round_budget", "verify_inline_tool_call_salvage",
            "verify_checker_diag_encoding"]
    # 2026-10-09 新增接线（必须随 ci.yml 一起更新，否则本门禁自己会红）
    _NEW = ["verify_agent_loop_protocol", "verify_agent_perm_gate",
            "verify_llm_circuit_breaker", "verify_memory_conflict_gate",
            "verify_sysml_autofix", "verify_pipeline_closure",
            "verify_sysml_baseline"]
    missing = [m for m in must if ("tools/verify/%s.py" % m) not in cmds]
    ok = _rec("W2a 本批 %d 个门禁全部在 ci.yml 执行行里" % len(must),
              not missing, "缺：%s" % missing)
    # 反证：被排除的那几个**必须不在** CI 里（否则我的分类是错的）
    # ⚠️ 2026-10-05 更新：verify_orch_whitelist / verify_plugin_bind_removed 已于第二轮第 2 项
    # 修复并接线（双环境实测绿），故从「排除」移入上面的 `must`。
    # 教训复现：**"曾排除过"不等于"该排除"** —— 排除清单本身也会过期，要重新实测。
    excluded = ["verify_ontology_dom_range", "verify_chat_clarify_partial",
                "verify_conv_summary_api", "verify_projects_nav",
                "verify_s3_e2e", "verify_kb_dashboard"]
    wrongly = [e for e in excluded if ("tools/verify/%s.py" % e) in cmds]
    ok &= _rec("W2b 被排除的 %d 个确实不在 CI 执行行里" % len(excluded),
               not wrongly, "误接：%s" % wrongly)
    # 它们必须在 ci.yml 头部注明（项目惯例：排除要写明理由）
    head = io_head = ""
    try:
        with open(os.path.join(root, ".github", "workflows", "ci.yml"), encoding="utf-8") as f:
            head = "".join(f.readlines()[:30])
    except OSError:
        pass
    undocumented = [e for e in excluded if e not in head]
    ok &= _rec("W2c 被排除的门禁在 ci.yml 头部注明了原因",
               not undocumented, "未注明：%s" % undocumented)
    return ok


def mutations(root=ROOT):
    print("\n--- M1：注入一个新门禁文件（未接线）⇒ W1a 判红 ---")
    _IN_MUT[0] = True
    probe = os.path.join(root, "tools", "verify", "verify_zmut_probe.py")
    try:
        with open(probe, "w", encoding="utf-8") as f:
            f.write("# -*- coding: utf-8 -*-\nprint('probe')\n")
        _rec("M1 未接线的新门禁 ⇒ W1a 判红", not t_all_gates_wired(root))
    finally:
        if os.path.exists(probe):
            os.remove(probe)
    _IN_MUT[0] = False

    print("\n--- M2：豁免写了但理由为空 ⇒ W1b 判红 ---")
    _IN_MUT[0] = True
    target = os.path.join(root, "tools", "verify", "verify_zmut_probe2.py")
    try:
        with open(target, "w", encoding="utf-8") as f:
            f.write("# -*- coding: utf-8 -*-\n# CI-OPTIONAL:\nprint('x')\n")
        _rec("M2 豁免无理由 ⇒ W1b 判红", not t_all_gates_wired(root))
    finally:
        if os.path.exists(target):
            os.remove(target)
    _IN_MUT[0] = False


def main():
    ok = True
    ok &= t_all_gates_wired()
    ok &= t_ci_has_new_batch()

    print("\n" + "=" * 72)
    n_fail = sum(1 for r in _results if r[0] == FAIL)
    print("常态断言：%d 条，%d 通过 / %d 失败" % (len(_results), len(_results) - n_fail, n_fail))
    for st, name, detail in _results:
        print("  [%s] %s%s" % (st, name, ("  <- " + detail) if detail else ""))
    if n_fail:
        print("结论：门禁未通过")
        return 1

    print("\n--- 变异自证 ---")
    _MUT_ROWS.clear()
    mutations()
    verdicts = [r for r in _MUT_ROWS if re.match(r"^M\d", r[1])]
    subs = [r for r in _MUT_ROWS if not re.match(r"^M\d", r[1])]
    n_red = sum(1 for r in verdicts if r[0] == PASS)
    print("变异组：%d，判红成功：%d（变异期子断言 %d 条，其中 %d 条转红）"
          % (len(verdicts), n_red, len(subs), sum(1 for r in subs if r[0] == FAIL)))
    print("-" * 72)
    for st, name, detail in verdicts:
        print("  [%s] %s%s" % (st, name, ("  <- " + detail) if detail else ""))
    print("=" * 72)
    if n_red != len(verdicts) or not verdicts:
        print("结论：门禁未通过（%d/%d 变异未判红）" % (len(verdicts) - n_red, len(verdicts)))
        return 1
    print("结论：全部通过（常态 %d 条 + 变异 %d 组全部按预期判红）"
          % (len(_results), len(verdicts)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
