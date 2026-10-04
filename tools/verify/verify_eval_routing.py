#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""P1-8：意图路由评测门禁 —— 把"改完跌分才发现"变成"合并请求就红"（2026-10-04）

## 为什么阈值锚在**干净库**基线上（这是本脚本最容易做错的地方）

MEMORY 里记着一次实测教训：CI 门禁在本机跑出accuracy=1.000，差点把
0.55 定成阈值；在干净库复测才发现 **本机 1.000 / CI 0.828** ——
因为**关键词规则表、Agent 关键词、样本池都是数据，不在 git 里**。

本次同样先测两组数字再定阈值：

| 环境 | 样本来源 | accuracy | macro-F1 |泛词陷阱 |
|---|---|---|---|---|
| 本机现有库 | pool(44 条已确认样本) | **0.9773** | 0.9818 | n=0 → null |
| **干净库（CI 等价）** | builtin(29) | **0.9310** | 0.9158 | 0.9000 (n=10) |

⇒ **两组不是同一道题**：本机多了 15 条真实样本（用户确认的说法）。
拿 0.977当阈值 ⇒ CI 永远达不到（假红）；拿 0.9773 当"基线"去算余量更糟。
⇒ **阈值只能锚干净库**，且必须**自建干净临时库**跑（不碰生产库）。

## 四条设计约束（每条都对应一次真实踩坑）

1. **自建干净临时库 + 自证库路径已切**：MEMORY 记着"环境变量设置得太晚，
   脚本在生产库上跑了一遍完整迁移"。本脚本起**子进程**并注入 `MBSE_DB_PATH`，
   再**打印子进程解析到的 DB_PATH** 与目标比对，不一致直接 exit 2。
2. **两次运行一致性断言**：`intent_cache` 若不清，第二次全部命中 cache，
   A/B 得到完全相同的假结论（脚本注释里已记）。本门禁跑两遍并断言
   `accuracy/macro_f1/generic_trap_acc` **逐位相同** ⇒ 防随机红绿。
   注意这是"确定性部分"，模型相关的波动**不该**进 CI 阻断
   （对标 LangSmith/Dify：确定性进 PR 阻断、模型相关 nightly 跑）。
3. **阈值取基线 − 明显余量，只挡塌陷不追最优**：基线 0.9310 ⇒ 阈值 0.86。
   余量 0.07 ≈ 2/29，正好容忍 1 个样例的边界抖动；再大就变成"什么都拦不住"。
4. **数据不足报 null 不报 0**：`generic_trap_acc` 在 builtin 集里有 10 例，
   但若哪天样本池回落导致 n=0，必须报 null（否则会被读成"全部判错"）。

## 变异自证

M1：把阈值提到 0.99（高于基线）⇒ 必须判红
M2：伪造第二遍结果（不一致）⇒ 一致性断言必须判红
M3：篡改 accuracy 字段（-0.05）⇒ 阈值断言必须判红

用法：
  .venv/Scripts/python.exe tools/verify/verify_eval_routing.py
  .venv/Scripts/python.exe tools/verify/verify_eval_routing.py --runs 1# 快速自测（不跑一致性）
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
EVAL = os.path.join(ROOT, "tests", "manual_verify", "eval_intent_routing.py")
SCHEMA = os.path.join(ROOT, "database", "schema.py")

# ── 阈值（锚定干净库实测基线 0.9310，余量 0.07）──────────────────────────
BASELINE_ACC = 0.9310
BASELINE_F1 = 0.9158
THRESHOLD_ACC = 0.86# 只挡塌陷
THRESHOLD_F1 = 0.84
RUNS = 2                                        # 跑两遍验一致性

PASS, FAIL = "PASS", "FAIL"
_results = []
_MUT_ROWS = []
_IN_MUT = [False]


def _rec(name, ok, detail=""):
    row = (PASS if ok else FAIL, name, "" if ok else str(detail)[:220])
    (_MUT_ROWS if _IN_MUT[0] else _results).append(row)
    return bool(ok)


def _py():
    for c in (os.path.join(ROOT, ".venv", "Scripts", "python.exe"), sys.executable):
        if os.path.exists(c):
            return c
    return sys.executable


def _run_on_clean_db(runs=RUNS):
    """在**全新干净临时库**上跑评测若干次，返回 (results[], env_dbpath)。

    每次 run 用**独立的新库** —— 复用同一个库会让 intent_cache / 向量索引
    带着上一次的状态，"两次一致"就变成"同一个库跑两遍当然一致"的废话。
    """
    env_base = dict(os.environ)
    env_base["PYTHONIOENCODING"] = "utf-8"
    env_base["PYTHONUTF8"] = "1"
    results, actual_paths = [], []

    for i in range(runs):
        tmpdir = tempfile.mkdtemp(prefix="eval_gate_%d_" % i)
        db = os.path.join(tmpdir, "clean.db")
        env = dict(env_base)
        env["MBSE_DB_PATH"] = db
        # 建库
        r = subprocess.run([_py(), "-X", "utf8", "-c",
                            "import sys;sys.path.insert(0,%r);"
                            "from database.schema import init_db;init_db()" % ROOT],
                           cwd=ROOT, env=env, capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            raise RuntimeError("init_db 失败: %s" % r.stderr[-400:])
        # ⚠️ 自证：子进程真的用了这个库吗（MEMORY：必须自证，否则等于在生产库上跑）
        rp = subprocess.run([_py(), "-X", "utf8", "-c",
                             "import sys,os;sys.path.insert(0,%r);"
                             "from core.config import DB_PATH;"
                             "print(os.path.abspath(DB_PATH))" % ROOT],
                            cwd=ROOT, env=env, capture_output=True, text=True, timeout=180)
        got = (rp.stdout or "").strip().splitlines()[-1] if rp.stdout else ""
        actual_paths.append(got)
        if os.path.abspath(db) not in got:
            raise RuntimeError("库路径未切：期望 %s 实得 %s" % (db, got))
        # 跑评测（--builtin 固定用内置集，避免样本池数据差异让阈值不可比）
        re_ = subprocess.run([_py(), "-X", "utf8", EVAL, "--builtin"],
                             cwd=ROOT, env=env, capture_output=True, text=True, timeout=1800)
        m = re.search(r"\[RESULT\](.*)", re_.stdout or "")
        if not m:
            raise RuntimeError("评测无 [RESULT] 行（rc=%s）：%s"
                               % (re_.returncode, (re_.stdout or re_.stderr)[-500:]))
        d = json.loads(m.group(1))
        d["_sample_source"] = "builtin" if "builtin" in (re_.stdout or "") else "pool"
        results.append(d)
        shutil.rmtree(tmpdir, ignore_errors=True)
    return results, actual_paths


# ── 判据（可注入变异体）：把"跑出来的结果"变成断言 ────────────────────────
def judge(results, paths=None, baseline_acc=BASELINE_ACC, baseline_f1=BASELINE_F1,
          thr_acc=THRESHOLD_ACC, thr_f1=THRESHOLD_F1, require_identical=True):
    ok = True
    if paths:
        ok &= _rec("E0 每次都在**各自的干净临时库**里跑（库路径已自证）",
                   len(set(paths)) == len(paths) and all(paths),
                   "paths=%s" % paths)
    for i, d in enumerate(results):
        ok &= _rec("E1 run%d accuracy=%.4f >= 阈值 %.2f（基线 %.4f）"
                   % (i + 1, d.get("accuracy", -1), thr_acc, baseline_acc),
                   d.get("accuracy", -1) >= thr_acc,
                   "acc=%s thr=%s" % (d.get("accuracy"), thr_acc))
        ok &= _rec("E2 run%d macro_f1=%.4f >= 阈值 %.2f（基线 %.4f）"
                   % (i + 1, d.get("macro_f1", -1), thr_f1, baseline_f1),
                   d.get("macro_f1", -1) >= thr_f1,
                   "f1=%s thr=%s" % (d.get("macro_f1"), thr_f1))
        # ⚠️ 数据不足报 null，不报 0（否则被读成"全部判错"）
        n = d.get("generic_trap_n", 0)
        ga = d.get("generic_trap_acc", None)
        if n == 0:
            ok &= _rec("E3 run%d 无泛词陷阱样本时该指标为 null（不是 0）" % (i + 1),
                       ga is None, "got=%s" % ga)
        else:
            ok &= _rec("E3 run%d 泛词陷阱子集 accuracy=%.4f (n=%d) 达标" % (i + 1, ga or 0, n),
                       (ga or 0) >= thr_acc, "ga=%s n=%s" % (ga, n))
        # 样本来源必须可复现 —— 否则两次跑分不同却不知为何
        ok &= _rec("E4 run%d 样本来源已打印且为 builtin（与阈值基线同一道题）" % (i + 1),
                   d.get("_sample_source") == "builtin", d.get("_sample_source"))
    if require_identical and len(results) >= 2:
        keys = ("accuracy", "macro_f1", "generic_trap_acc")
        same = all(results[0].get(k) == results[1].get(k) for k in keys)
        ok &= _rec("E5 两次运行逐位一致（accuracy/macro_f1/generic_trap_acc）",
                   same, "%s vs %s" % ([results[0].get(k) for k in keys],
                                       [results[1].get(k) for k in keys]))
        # 反证：必须真的跑了两遍，不能只有一份就宣称一致
        ok &= _rec("E5b 确实跑了 2 次（不是同一份数据自我比对）", len(results) >= 2,
                   "runs=%d" % len(results))
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=RUNS)
    a = ap.parse_args()

    # 前置：评测集与库自举脚本必须存在（否则门禁"通过"是因为没跑）
    ok = _rec("N1评测脚本存在", os.path.exists(EVAL), EVAL)
    ok &= _rec("N2 schema 存在（自建干净库要用）", os.path.exists(SCHEMA))
    ok &= _rec("N3 阈值低于基线（否则等于要求超越历史最优）",
               THRESHOLD_ACC < BASELINE_ACC and THRESHOLD_F1 < BASELINE_F1,
               "thr_acc=%s base=%s" % (THRESHOLD_ACC, BASELINE_ACC))
    if not ok:
        print("前置检查未过，终止")
        return 1

    print("在干净临时库上跑评测 ×%d（每遍独立新库）……" % a.runs)
    results, paths = _run_on_clean_db(a.runs)
    for i, d in enumerate(results):
        print("  run%d: acc=%.4f f1=%.4f trap=%s(n=%s) src=%s"
              % (i + 1, d["accuracy"], d["macro_f1"], d.get("generic_trap_acc"),
                 d.get("generic_trap_n"), d.get("_sample_source")))

    ok = judge(results, paths)

    print("\n" + "=" * 68)
    n_fail = sum(1 for r in _results if r[0] == FAIL)
    print("常态断言：%d 条，%d 通过 / %d 失败" % (len(_results), len(_results) - n_fail, n_fail))
    for st, name, detail in _results:
        print("  [%s] %s%s" % (st, name, ("  <- " + detail) if detail else ""))
    if n_fail:
        print("结论：门禁未通过")
        return 1

    # ── 变异自证（用注入的数据，不重跑评测，省时间）────────────────────
    print("\n--- 变异自证 ---")
    _MUT_ROWS.clear()
    base = results[0]
    second = results[1] if len(results) > 1 else dict(base)

    # M1：阈值提到 0.99（高于基线）⇒ 必判红
    _IN_MUT[0] = True
    _rec("M1 阈值抬到 0.99（高于基线）⇒ 判红",
         not judge([base], None, thr_acc=0.99, thr_f1=0.99, require_identical=False))
    _IN_MUT[0] = False

    # M2：伪造不一致的第二遍 ⇒ 一致性断言必须判红
    _IN_MUT[0] = True
    bad2 = dict(second)
    bad2["accuracy"] = round(bad2["accuracy"] - 0.07, 4)
    _rec("M2 两遍不一致 ⇒ 判红", not judge([base, bad2], None))
    _IN_MUT[0] = False

    # M3：accuracy 塌陷 −0.08 ⇒ 阈值断言必须判红
    _IN_MUT[0] = True
    low = dict(base)
    low["accuracy"] = round(low["accuracy"] - 0.08, 4)
    _rec("M3 accuracy 塌陷 0.08 ⇒ 判红",
         not judge([low], None, require_identical=False))
    _IN_MUT[0] = False

    # M4：样本池来源（换了题）⇒ 必须判红
    _IN_MUT[0] = True
    pool = dict(base)
    pool["_sample_source"] = "pool(44)"
    _rec("M4 样本来源换成 pool（换了题）⇒ 判红",
         not judge([pool], None, require_identical=False))
    _IN_MUT[0] = False

    # M5：无样本时把 null 报成 0.0 ⇒ 必须判红
    _IN_MUT[0] = True
    zero = dict(base)
    zero["generic_trap_n"] = 0
    zero["generic_trap_acc"] = 0.0
    _rec("M5 无样本却报 0.0（应 null）⇒ 判红",
         not judge([zero], None, require_identical=False))
    _IN_MUT[0] = False

    verdicts = [r for r in _MUT_ROWS if re.match(r"^M\d+\b", r[1])]
    subs = [r for r in _MUT_ROWS if not re.match(r"^M\d+\b", r[1])]
    n_red = sum(1 for r in verdicts if r[0] == PASS)
    print("变异组：%d，判红成功：%d（变异期子断言 %d 条，其中 %d 条转红）"
          % (len(verdicts), n_red, len(subs), sum(1 for r in subs if r[0] == FAIL)))
    for st, name, detail in subs:
        if st == FAIL:
            print("  [%s] %s" % (st, name))
    print("-" * 68)
    for st, name, detail in verdicts:
        print("  [%s] %s%s" % (st, name, ("  <- " + detail) if detail else ""))
    print("=" * 68)
    if n_red != len(verdicts) or not verdicts:
        print("结论：门禁未通过（%d/%d 变异未判红）" % (len(verdicts) - n_red, len(verdicts)))
        return 1
    print("结论：全部通过（常态 %d 条 + 变异 %d 组全部按预期判红）"
          % (len(_results), len(verdicts)))
    print("注：阈值锚**干净库**基线 %.4f（不是本机现有库的 %.4f —— 那是另一道题）"
          % (BASELINE_ACC, 0.9773))
    return 0


if __name__ == "__main__":
    sys.exit(main())
