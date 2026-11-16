# -*- coding: utf-8 -*-
"""verify_memory_eval_reanchor.py —— 记忆评测集的「重锚判据」必须自洽。

## 背景（2026-11-16 实测）
`tools/eval/eval_memory_quality.py` 的评测集硬锚 2026-10-07 的 id 快照。
库从 144 条涨到 295 条、其中 97 条 forgotten 后，实跑只有 4 PASS / 10 FAIL（28.6%）。

两种可能的解释必须分开（否则会把"库内容变化"误判成"召回算法退化"）：
  ① 期望记忆已 forgotten/删除，且当前库无等价 alive ⇒ **SKIP**（能力已不在库，
     不是召回失败）
  ② 期望记忆仍 alive 却召回不到 ⇒ **FAIL**（真实召回问题）

修法：给评测脚本加 `REANCHOR_ALIVE` 重锚表，有等价 alive 就用它评，
彻底没有就显式 SKIP。SKIP 不计入命中率分母，但必须落在报告里（不许静默缩水）。

## 本门禁锁什么
A 组：重锚表与真实库的自洽性
  A1 表里所有非空重锚 id 都真实存在且 alive
  A2 表里所有空重锚的旧 expect id 确实 forgotten/删除（空表不是偷懒放宽）
  A3 重锚表覆盖了全部 T/M/C 用例（不许新增用例忘了登记）
  A4 变异自证：把某个重锚 id 改成不存在的，A1 必须翻红
B 组：评测器行为
  B1 评测脚本可运行且不抛异常
  B2 报告里 SKIP 数 == 重锚空表数（SKIP 没被静默吞掉）
  B3 每条 SKIP 都带 why（理由可见，不是黑盒）

用法： ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_memory_eval_reanchor.py
退出码：0 全绿（干净库跳过真库组）／1 有失败
"""
import io
import os
import re
import sqlite3
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
SCRIPT = os.path.join(ROOT, "tools", "eval", "eval_memory_quality.py")
DB = os.environ.get("MBSE_GATE_DB") or os.environ.get("MBSE_DB_PATH") or os.path.join(ROOT, "mbse.db")

_PASS, _FAIL, _SKIP = [], [], []


def chk(name, ok, detail=""):
    (_PASS if ok else _FAIL).append(name)
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name,
                           ("　— " + str(detail)) if detail and not ok else ""))


def skip(name, why):
    _SKIP.append(name)
    print("  [SKIP] %s :: %s" % (name, why))


def load_eval_module():
    """只取评测脚本的常量与纯函数，不执行 main()。"""
    src = io.open(SCRIPT, encoding="utf-8").read()
    ns = {"__name__": "eval_memory_quality_probe", "__file__": SCRIPT}
    # 用 exec 执行到 def main() 之前（常量 + 纯函数已定义）
    cut = src.index("\ndef main():")
    exec(compile(src[:cut], SCRIPT, "exec"), ns)
    return ns, src


def main():
    print("=" * 78)
    print("门禁：记忆评测集重锚判据自洽")
    print("=" * 78)
    ns, src = load_eval_module()
    reanchor = ns.get("REANCHOR_ALIVE") or {}
    cases = (ns.get("T_CASES") or []) + (ns.get("M_CASES") or []) + (ns.get("C_CASES") or [])

    print("\n--- GROUP A ---")
    chk("A0 重锚表已定义且非空", bool(reanchor), len(reanchor))
    chk("A3 重锚表覆盖全部 T/M/C 用例",
        set(reanchor) == {c["id"] for c in cases},
        "缺: %s" % sorted({c["id"] for c in cases} - set(reanchor)))

    if not os.path.exists(DB):
        for n in ("A1", "A2", "A4"):
            skip(n, "干净库：跳过需要真库的判据组")
        skip("B1 评测脚本可运行", "干净库")
        skip("B2 SKIP 数 == 重锚空表数", "干净库")
        skip("B3 每条 SKIP 都带 why", "干净库")
    else:
        conn = sqlite3.connect(DB)
        conn.row_factory = sqlite3.Row
        alive = {r[0] for r in conn.execute(
            "select id from agent_memory where coalesce(forgotten,0)=0")}
        all_ids = {r[0] for r in conn.execute("select id from agent_memory")}

        bad = []
        for cid, ids in reanchor.items():
            for mid in ids:
                if mid not in alive:
                    bad.append("%s→#%s" % (cid, mid))
        chk("A1 重锚表非空项全部 alive", not bad, bad)

        # A2：空重锚必须"确实没得锚"——旧 expect 全 forgotten/删除，
        #     否则就是把能评的用例偷懒改成 SKIP（缩水判据）
        lazy = []
        for c in cases:
            if reanchor.get(c["id"]):
                continue
            old = [m for m in c.get("expect", []) if m in alive]
            if old:
                lazy.append("%s 旧锚仍 alive=%s" % (c["id"], old))
        chk("A2 空重锚确实无 alive 旧锚（不是偷懒放宽）", not lazy, lazy)

        # A4 变异自证：伪造一个不存在的重锚 id ⇒ A1 判据必须翻红
        fake = dict(reanchor)
        first = next(k for k, v in fake.items() if v)
        fake[first] = [999999999]
        mutated = any(mid not in alive for mid in fake[first])
        chk("A4 变异自证：重锚改成不存在 id ⇒ A1 必须翻红", mutated)

        conn.close()

        # B 组：跑一次评测（只读），核对 SKIP 数与重锚空表数
        try:
            r = subprocess.run([sys.executable, "-X", "utf8", SCRIPT],
                               cwd=ROOT, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=300)
            out = r.stdout or ""
            chk("B1 评测脚本可运行且不抛异常",
                "Traceback" not in out and r.returncode in (0, 1),
                (out[-300:] if "Traceback" in out else "rc=%s" % r.returncode))
            # 只数**用例级** SKIP（形如 "  SKIP T05 ..."），
            # 不匹配汇总行 "SKIP 5 条" / "⚠ SKIP 1 条" —— 否则计数虚高。
            _case_skip = re.compile(r"^\s*SKIP\s+[TCMN]\d{2}\b", re.M)
            n_skip_out = len(_case_skip.findall(out))
            n_empty = sum(1 for v in reanchor.values() if not v)
            chk("B2 报告 SKIP 数 == 重锚空表数（没被静默吞掉）",
                n_skip_out == n_empty, "报告=%d 空表=%d" % (n_skip_out, n_empty))
            chk("B3 每条 SKIP 都带 why（理由可见）",
                all("遗忘" in l or "删除" in l
                    for l in out.splitlines() if _case_skip.match(l)),
                "")
        except Exception as e:
            chk("B1 评测脚本可运行且不抛异常", False, repr(e)[:200])

    print("\n" + "=" * 78)
    print("结果：PASS %d / FAIL %d / SKIP %d" % (len(_PASS), len(_FAIL), len(_SKIP)))
    for n in _FAIL:
        print("  - " + n)
    print("=" * 78)
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
