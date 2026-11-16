"""CI-OPTIONAL: [local] P0-1/P0-2 常驻门禁：端到端 Golden Set + 架构级 A/B 跑批器。

为什么是 local：本门禁要连真库 mbse.db 重算语料指纹（干净 checkout 无该库），
且不调用 LLM、不写库。跑一次约 1~3 秒。

判据分三层：
  [A] 静态契约：数据集 schema/指纹/覆盖、判据类型登记、A/B 脚本的单变量与重跑守卫
  [B] 行为：subprocess 跑 run_arch_ab.py self-test（10 条含 4 个变异）、离线 compare 真文件
  [M] 变异自证：把评分器判据改成恒真 → 必须抹掉「空会话 vs 真会话」的区分度
      （证明判据不是空转断言，与 §6.2 的纪律同源）

裸跑：./.venv/Scripts/python.exe -X utf8 tools/verify/verify_arch_ab.py
"""
import hashlib
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools", "eval"))

import golden_e2e as G  # noqa: E402
import run_arch_ab as AB  # noqa: E402

PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
SET_PATH = os.path.join(ROOT, "docs", "评测", "datasets", "golden_e2e_v0.json")
BASELINE = os.path.join(ROOT, "docs", "评测", "golden_e2e_v0_baseline.json")
AB_SRC = os.path.join(ROOT, "tools", "eval", "run_arch_ab.py")
GOLD_SRC = os.path.join(ROOT, "tools", "eval", "golden_e2e.py")
BUILD_SRC = os.path.join(ROOT, "tools", "eval", "build_golden_e2e.py")
MANIFEST = os.path.join(ROOT, "tools", "gates_manifest.json")

PASS, FAIL = 0, 0


def ck(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS", name)
    else:
        FAIL += 1
        print("  FAIL", name, detail)


def main():
    print("[A] 静态契约")
    ck("A1 Golden Set 数据集存在", os.path.isfile(SET_PATH))
    ds = G.load_dataset()
    ck("A2 schema 与版本", ds["schema"] == "p0/golden-e2e@1" and ds["version"] == "v0")
    fp = hashlib.sha1(json.dumps(ds["cases"], ensure_ascii=False,
                                 sort_keys=True).encode("utf-8")).hexdigest()[:16]
    ck("A3 语料指纹只依赖 cases（未被时间污染）",
       fp == ds["dataset_id"], "file=%s recomputed=%s" % (ds["dataset_id"], fp))
    chains = {x["chain"] for x in ds["cases"]}
    ck("A4 三条链路全覆盖且用例数 >=10",
       chains == {"single_agent", "orchestrated", "model_and_validate"} and len(ds["cases"]) >= 10,
       str(chains))
    ck("A5 每例含 input/intent/规则/可判定验收条件/来源",
       all(x.get("input_source", {}).get("message_id") and x.get("intent")
           and x.get("inviolable_rules") and x.get("checks")
           and x.get("expected_deliverable_kind") and x.get("conv_id")
           for x in ds["cases"]))
    ck("A6 判据类型全部登记在 CHECKERS",
       {c["type"] for x in ds["cases"] for c in x["checks"]} <= set(G.CHECKERS))

    src = open(AB_SRC, encoding="utf-8").read()
    gsrc = open(GOLD_SRC, encoding="utf-8").read()
    ck("A7 A/B 有单变量守卫", "违反单变量纪律" in src and "_cfg_diff(a, b)" in src)
    ck("A8 A/B 有重跑守卫", "MIN_REPEAT" in src and "重跑次数不足" in src)
    ck("A9 A/B 有「原本成功→失败」清单",
       "原本成功 → 变更失败" in src and '"regressions"' in src)
    ck("A10 落库写 is_golden=1 且 golden_set_id",
       re.search(r"is_golden,\s*golden_set_id", gsrc) is not None and
       "VALUES (0,?,?,?,?,1,?,?)" in gsrc)
    ck("A11 报告显式声明 offline 不调 LLM", "不重新调用 LLM" in gsrc)
    man = json.load(open(MANIFEST, encoding="utf-8"))
    entry = [g for g in man["gates"] if g["script"] == "verify_arch_ab.py"]
    ck("A12 已登记 gates_manifest（local + 非空 reason）",
       len(entry) == 1 and entry[0]["tier"] == "local" and bool(entry[0].get("reason")),
       str(entry))
    ck("A13 基线报告已产出", os.path.isfile(BASELINE) and
       json.load(open(BASELINE, encoding="utf-8"))["dataset_id"] == ds["dataset_id"])

    print("[B] 行为")
    r = subprocess.run([PY, "-X", "utf8", AB_SRC, "self-test"],
                       cwd=ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=120)
    ck("B1 self-test rc=0 且 10 条全绿",
       r.returncode == 0 and "0 FAIL" in (r.stdout or ""),
       "rc=%s out=%s err=%s" % (r.returncode, (r.stdout or "")[-300:], (r.stderr or "")[-200:]))
    # 用真库跑一遍离线 compare（不写库、不调 LLM）
    r2 = subprocess.run([PY, "-X", "utf8", BUILD_SRC], cwd=ROOT,
                        capture_output=True, text=True, encoding="utf-8",
                        errors="replace", timeout=120)
    m = re.search(r"dataset_id=\s*([0-9a-f]{16})", r2.stdout or "")
    ck("B2 语料未漂移（重派生指纹一致）",
       r2.returncode == 0 and bool(m) and m.group(1) == ds["dataset_id"],
       "got=%s expect=%s rc=%s" % (m.group(1) if m else "?", ds["dataset_id"], r2.returncode))
    c = G._ro_conn()
    try:
        rep = G.score_dataset(c, ds)
    finally:
        c.close()
    ck("B3 基线 12 例全部可判定（无未知判据）",
       rep["case_count"] == 12 and
       all(not any("未知判据类型" in x["evidence"] for x in row["checks"])
           for row in rep["rows"]))

    print("[M] 变异自证：判据全空转必须抹掉区分度")
    # ⚠️ 只改一个判据不够：其余判据仍会正常 FAIL，空会话照样被抓住，
    #    变异体行为没变 ⇒ 假绿（§6.2「变异必须真正改变被观察行为」）。
    #    要模拟「评分器整体假绿」，必须把**全部**判据替换成恒真。
    orig_map = dict(G.CHECKERS)
    c = G._ro_conn()
    try:
        good = G.score_case(c, ds["cases"][0])
        bad = G.score_case(c, ds["cases"][0], 999999999)
        baseline_distinct = good["passed"] and not bad["passed"]
        G.CHECKERS.update({k: (lambda *_a, **_k: (True, "MUTANT always true"))
                           for k in list(G.CHECKERS)})
        bad_m = G.score_case(c, ds["cases"][0], 999999999)
        mutated_distinct = good["passed"] and not bad_m["passed"]
    finally:
        G.CHECKERS.clear()
        G.CHECKERS.update(orig_map)
        c.close()
    ck("M1 基线判据能区分空会话", baseline_distinct)
    ck("M2 变异（全判据恒真）后空会话被判通过 ⇒ 断言有牙齿",
       baseline_distinct and not mutated_distinct and bad_m["passed"] is True,
       "mutated passed=%s checks=%s" % (bad_m["passed"], bad_m["checks"][:1]))
    c = G._ro_conn()
    try:
        restored = G.score_case(c, ds["cases"][0], 999999999)
    finally:
        c.close()
    ck("M3 变异已还原（不留污染）", not restored["passed"])

    print("RESULT: %d PASS / %d FAIL" % (PASS, FAIL))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
