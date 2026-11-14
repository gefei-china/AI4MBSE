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
import json
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
# ★ 2026-11-13：前端门禁（`tools/verify/verify_*.js`）此前**完全在论域之外** ——
#   本函数只 enumerate `*.py`，而 manifest 的 `all_py_files()` 同样只扫 `.py`
#   ⇒ 22 个真实浏览器门禁「有没有接线」从来没人管，`G5 no-漏网` 恒绿。
#   这与本仓「判据的论域有缺口，判据再严也管不到」是同一条教训（见 e2e_ 那次）。
#   JS 用不了 `#` 注释，故豁免前缀必须多认一个 `//`，否则扩了论域也无法写豁免。
OPT_PREFIXES = (OPT_PREFIX, "// CI-OPTIONAL:")


def _rec(name, ok, detail=""):
    row = (PASS if ok else FAIL, name, "" if ok else str(detail)[:220])
    (_MUT_ROWS if _IN_MUT[0] else _results).append(row)
    return bool(ok)


def scan_gates(root=ROOT):
    """门禁脚本清单：(无扩展名 stem, 绝对路径)。

    ★ 2026-11-13 论域扩展：原来只扫 `*.py` ⇒ 22 个 `verify_*.js` 前端门禁
      永远不进 W1（"要么接线要么豁免"）的判据范围，等于**前端回归没人守**。
      现在 `.py` 与 `.js` 同一口径扫（前缀规则相同：verify_ / audit_）。
    """
    out = []
    vd = os.path.join(root, "tools", "verify")
    files = sorted(glob.glob(os.path.join(vd, "*.py"))) + \
            sorted(glob.glob(os.path.join(vd, "*.js")))
    for p in files:
        n = os.path.basename(p)
        stem, ext = os.path.splitext(n)
        if n.startswith("__") or n == "audit_dup_defs.py":
            pass  # audit_dup_defs 也是门禁，见下
        if not (stem.startswith("verify_") or stem.startswith("audit_")):
            continue
        out.append((stem, p))
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
    """读该门禁的 CI-OPTIONAL 豁免理由（非空才算有效豁免）。

    同时认 `# CI-OPTIONAL:`（Python/yml）与 `// CI-OPTIONAL:`（JS）——
    2026-11-13 起论域含 `.js`，不认 `//` 就等于"扩了检查范围却不给豁免通道"。
    """
    try:
        with open(path, encoding="utf-8") as f:
            for ln in f:
                s = ln.strip()
                for p in OPT_PREFIXES:
                    if s.startswith(p):
                        return s[len(p):].strip()
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
        # ★ 2026-11-13：按**真实扩展名**拼路径（原来硬拼 `.py` ⇒ `.js` 门禁永远判"未接线"）
        ext = os.path.splitext(path)[1]
        wired = ("tools/verify/%s%s" % (name, ext)) in cmds or \
                ("tools/%s%s" % (name, ext)) in cmds
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
    print("\n=== W2 manifest 的 pr 层必须都真的在 CI 执行行里 ===")
    cmds = " ".join(ci_commands(root))
    # ⚠️ 2026-11-05 重构：原来这里是一份**硬编码的历史名单**（2026-10-05 那批 38 个）。
    #    它每轮都在制造摩擦：清单降级一条，这里就要手改一条，忘改就红；
    #    而且"名单里有"≠"现在该在 CI 里"（曾接线过 ≠ 现在还能接）。
    #    现在直接读清单 —— **清单是唯一真源**，本断言只做"清单 pr ⊆ ci.yml"的对账。
    #    （与 verify_gate_manifest 的 G6a 同向，但这里是逐 job 级的独立复核。）
    try:
        with open(os.path.join(root, "tools", "gates_manifest.json"), encoding="utf-8") as f:
            _m = json.load(f)
    except (OSError, ValueError) as e:
        return _rec("W2a 清单可读", False, str(e))
    # ★ 2026-11-13：直接用清单里的 `script` 全名（含扩展名）对账。
    #   原实现 `g["script"][:-3]` 再拼回 `.py` —— 论域含 `.js` 后，`.js` 条目会被
    #   拼成 `xxx.js.py` ⇒ pr 层的 JS 门禁**永远判缺失**（假红）。
    must = [g["script"] for g in _m.get("gates") or [] if g.get("tier") == "pr"]
    missing = [m for m in must if ("tools/verify/%s" % m) not in cmds]
    ok = _rec("W2a 清单 pr 层的 %d 个门禁全部在 ci.yml 执行行里" % len(must),
              not missing, "缺：%s" % missing[:8])
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
