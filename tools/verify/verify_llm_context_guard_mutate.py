"""变异测试：证明两处自检的断言不是空转（2026-09-20，第二轮扩充）。

# ── CI 豁免（2026-10-05 标注，理由已实测）──────────────────
# CI-OPTIONAL: C 实测本地红 ⇒ 需先修
#   分类：A=需服务在跑/ B=需密钥或写真库/ C=实测就红需先修。
#   依据见 docs/遗留优化项-第二轮盘点-20261005.md；
#   由 tools/verify/verify_gate_wiring.py 强制要求（要么接线，要么写理由）。

流程（每个变异）：备份源文件 -> 注入变异 -> 跑对应自检 -> **必须 FAIL** -> 还原 -> 字节 diff 校验。

M1~M3：context_window 守卫（verify_llm_context_guard.py）
M4    ：D10 路由「无生产调用点」标注（verify_llm_context_guard.py）
M5    ：I4d 评审窗口探针改回写死（verify_orch_summary_budget.py）—— 门禁盲区是否真的被补上
"""
import os
import shutil
import subprocess

ROOT = r"C:\Users\gefei\WorkBuddy\2026-08-04-19-05-52\mbse_system"
PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
V_GUARD = os.path.join(ROOT, "tools", "verify", "verify_llm_context_guard.py")
V_BUDGET = os.path.join(ROOT, "tools", "verify", "verify_orch_summary_budget.py")
TMP = os.path.join(ROOT, "tmp", "llm_ctx", "mutbak")
os.makedirs(TMP, exist_ok=True)

GUARD_BLOCK = (
    "        if mt > cw:\n"
    "            if _guard == \"clamp\":\n"
    "                mt = cw\n"
    "            if _guard != \"off\":      # off = 完全不介入（含不告警），否则「静默」语义不成立\n"
    "                _warn_context_once(cfg, cw, mt, _est_in, _guard, \"max_tokens 超过 context_window\")\n"
    "        elif _guard != \"off\" and _est_in > 0 and _est_in + mt > cw:\n"
    "            _warn_context_once(cfg, cw, mt, _est_in, _guard, \"估算输入 + max_tokens 超过 context_window\")\n"
)

MUTATIONS = [
    ("M1 复原「静默截断」（整段守卫退回一句）", "llm/providers/openai_compat.py", V_GUARD,
     GUARD_BLOCK,
     "        if mt > cw:\n            mt = cw\n"),

    ("M2 保留三档但删掉留痕（证明 [2]/[5.1] 不是空转）", "llm/providers/openai_compat.py", V_GUARD,
     GUARD_BLOCK,
     "        if mt > cw:\n"
     "            if _guard == \"clamp\":\n"
     "                mt = cw\n"
     "        elif _guard != \"off\" and _est_in > 0 and _est_in + mt > cw:\n"
     "            pass\n"),

    ("M3 去掉默认 provider 的 ORDER BY（证明 [4.x] 不是空转）", "llm/__init__.py", V_GUARD,
     "WHERE model_type='chat' AND is_default=1 AND status='active' ORDER BY priority DESC, id ASC LIMIT 1\").fetchone()\n        conn.close()",
     "WHERE model_type='chat' AND is_default=1 AND status='active'\").fetchone()\n        conn.close()"),

    ("M4 删掉 LLMRouter 的「无生产调用点」警示（证明 [7.3] 不是空转）", "llm/__init__.py", V_GUARD,
     "\u65e0\u751f\u4ea7\u8c03\u7528\u70b9",          # 无生产调用点
     "\u5df2\u63a5\u5165\u7f16\u6392\u94fe\u8def"),    # 已接入编排链路

    ("M5 I4d 探针改回写死 22409（证明门禁盲区确实被补上）",
     "tools/verify/verify_orch_summary_budget.py", V_BUDGET,
     "    def check_eval_window(clip_fn, probe_len=None):",
     "    def check_eval_window(clip_fn, probe_len=22409):"),
]


def run_verify(verify):
    p = subprocess.run([PY, "-X", "utf8", verify], cwd=ROOT, capture_output=True)
    out = (p.stdout or b"").decode("utf-8", "replace") + (p.stderr or b"").decode("utf-8", "replace")
    fails = [ln.strip() for ln in out.splitlines() if ln.strip().startswith("FAIL")]
    summ = [ln for ln in out.splitlines() if ("SUMMARY" in ln or "断言汇总" in ln)]
    return p.returncode, fails, (summ[-1] if summ else "?")


print("=== 基线（未变异）===")
for _v, _n in ((V_GUARD, "guard"), (V_BUDGET, "budget")):
    _rc, _f, _s = run_verify(_v)
    print("   %-7s rc=%d  %s  fails=%d" % (_n, _rc, _s, len(_f)))
    if _rc != 0:
        print("!! 基线未全绿，变异测试无意义")
        raise SystemExit(1)

bad = 0
for name, rel, verify, old, new in MUTATIONS:
    fp = os.path.join(ROOT, rel)
    bak = os.path.join(TMP, rel.replace("/", "__").replace("\\", "__") + ".orig")
    shutil.copy2(fp, bak)
    orig = open(fp, "rb").read()
    ob, nb = old.encode("utf-8"), new.encode("utf-8")
    n = orig.count(ob)
    if n != 1:
        print("[%s] 变异注入失败：命中数=%d（期望 1）" % (name, n))
        bad += 1
        shutil.copy2(bak, fp)
        continue
    open(fp, "wb").write(orig.replace(ob, nb))
    try:
        rc2, fails2, summ2 = run_verify(verify)
        caught = rc2 != 0
        print("\n[%s]" % name)
        print("   变异后：rc=%d  %s  fails=%d  -> %s" % (rc2, summ2, len(fails2),
                                                    "已被断言抓住 ✅" if caught else "!! 未被抓住（断言空转）"))
        for f in fails2[:6]:
            print("     ", f[:130])
        if not caught:
            bad += 1
    finally:
        shutil.copy2(bak, fp)
        same = open(fp, "rb").read() == orig
        print("   还原：字节一致=%s" % same)
        if not same:
            bad += 1

print("\n=== 基线复跑（确认树已还原）===")
for _v, _n in ((V_GUARD, "guard"), (V_BUDGET, "budget")):
    _rc, _f, _s = run_verify(_v)
    print("   %-7s rc=%d  %s" % (_n, _rc, _s))
    if _rc != 0:
        bad += 1

print("\n=== 变异测试结果 ===", "ALL CAUGHT / CLEAN" if bad == 0 else "!! 有 %d 项异常" % bad)
raise SystemExit(0 if bad == 0 else 1)
