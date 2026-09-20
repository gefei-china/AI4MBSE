"""变异测试：证明 verify_llm_context_guard.py 的断言不是空转（2026-09-20）。

流程（每个变异）：备份源文件 -> 注入变异 -> 跑自检 -> **必须 FAIL** -> 还原 -> `diff` 校验字节一致。
"""
import os
import shutil
import subprocess
import sys

ROOT = r"C:\Users\gefei\WorkBuddy\2026-08-04-19-05-52\mbse_system"
PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
VERIFY = os.path.join(ROOT, "tools", "verify", "verify_llm_context_guard.py")
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

M1 = ("M1 复原「静默截断」（整段守卫退回一句）", "llm/providers/openai_compat.py",
      GUARD_BLOCK,
      "        if mt > cw:\n            mt = cw\n")

M2 = ("M2 保留三档但删掉留痕（证明 [2]/[5.1] 不是空转）", "llm/providers/openai_compat.py",
      GUARD_BLOCK,
      "        if mt > cw:\n"
      "            if _guard == \"clamp\":\n"
      "                mt = cw\n"
      "        elif _guard != \"off\" and _est_in > 0 and _est_in + mt > cw:\n"
      "            pass\n")

M3 = ("M3 去掉默认 provider 的 ORDER BY（证明 [4.x] 不是空转）", "llm/__init__.py",
      "WHERE model_type='chat' AND is_default=1 AND status='active' ORDER BY priority DESC, id ASC LIMIT 1\").fetchone()\n        conn.close()",
      "WHERE model_type='chat' AND is_default=1 AND status='active'\").fetchone()\n        conn.close()")


def run_verify():
    p = subprocess.run([PY, "-X", "utf8", VERIFY], cwd=ROOT, capture_output=True)
    out = (p.stdout or b"").decode("utf-8", "replace") + (p.stderr or b"").decode("utf-8", "replace")
    fails = [ln.strip() for ln in out.splitlines() if ln.strip().startswith("FAIL ")]
    summ = [ln for ln in out.splitlines() if ln.startswith("SUMMARY")]
    return p.returncode, fails, (summ[0] if summ else "?")


print("=== 基线（未变异）===")
rc, fails, summ = run_verify()
print("   rc=%d  %s  fails=%d" % (rc, summ, len(fails)))
if rc != 0:
    print("!! 基线未全绿，变异测试无意义")
    raise SystemExit(1)

bad = 0
for name, rel, old, new in (M1, M2, M3):
    fp = os.path.join(ROOT, rel)
    bak = os.path.join(TMP, rel.replace("/", "__") + ".orig")
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
        rc2, fails2, summ2 = run_verify()
        caught = rc2 != 0
        print("\n[%s]" % name)
        print("   变异后：rc=%d  %s  fails=%d  -> %s" % (rc2, summ2, len(fails2),
                                                    "已被断言抓住 ✅" if caught else "!! 未被抓住（断言空转）"))
        for f in fails2[:8]:
            print("     ", f[:120])
        if not caught:
            bad += 1
    finally:
        shutil.copy2(bak, fp)
        same = open(fp, "rb").read() == orig
        print("   还原：字节一致=%s" % same)
        if not same:
            bad += 1

print("\n=== 基线复跑（确认树已还原）===")
rc3, fails3, summ3 = run_verify()
print("   rc=%d  %s" % (rc3, summ3))
if rc3 != 0:
    bad += 1

print("\n=== 变异测试结果 ===", "ALL CAUGHT / CLEAN" if bad == 0 else "!! 有 %d 项异常" % bad)
raise SystemExit(0 if bad == 0 else 1)
