# -*- coding: utf-8 -*-
"""诊断：`run-eval` 首次调用比第二次多约 8s，**瓶颈在哪**（2026-09-26）。

## 背景
服务端实测：重启后首次 `POST /api/intent-samples/run-eval` 约 12.6s，第二次约 4.8s。
语义层候选集已确认被预热（日志：52 条已缓存；真实对话路径意图阶段仅 1.06s），
所以这 8s **不是**候选集向量化。本脚本在**进程内**复刻 run-eval 的两遍流程并逐条计时，
把 8s 落到"哪几条用例 + 走哪条路"上。

## 复刻要点（与 `routers/intent_samples.py::run_eval` 保持一致）
- `AgentPipeline()` + `_load_db_agents()`（真实路由池）
- **绕开意图缓存**（`_cache_get/_cache_set` 置空）——否则第二遍全命中 cache，测不出真实差异
- 逐条 `rt.detect(text)` 并 `get_last_meta()` 取 route

用法：.venv/Scripts/python.exe -X utf8 tools/verify/diagnose_run_eval_latency.py
"""
import os
import sys
import time

ROOT = r"C:\Users\gefei\WorkBuddy\2026-08-04-19-05-52\mbse_system"
sys.path.insert(0, ROOT)
os.chdir(ROOT)
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "tests", "manual_verify"))

from agent.pipeline import AgentPipeline  # noqa: E402
from agent.intent import IntentRouter  # noqa: E402
from intent_cases import BUILTIN_PAIRS  # noqa: E402


def one_pass(label, cases):
    t0 = time.time()
    pipe = AgentPipeline()
    t_build = time.time() - t0
    t0 = time.time()
    pipe._load_db_agents()
    t_agents = time.time() - t0
    rt = pipe.router
    og, os_ = IntentRouter._cache_get, IntentRouter._cache_set
    IntentRouter._cache_get = lambda self, *a, **k: None      # 绕开缓存（同 run-eval）
    IntentRouter._cache_set = lambda self, *a, **k: None
    rows = []
    try:
        for text, _want in cases:
            t1 = time.time()
            got = rt.detect(text)
            dt = time.time() - t1
            rows.append({"text": text, "intent": got, "route": rt.get_last_meta().get("route", ""), "sec": dt})
    finally:
        IntentRouter._cache_get, IntentRouter._cache_set = og, os_
    total = time.time() - t0
    print("\n===== %s =====" % label)
    print("  构造 pipeline=%.2fs  _load_db_agents=%.2fs  44 条 detect=%.2fs  合计≈%.2fs"
          % (t_build, t_agents, total, t_build + t_agents + total))
    print("  最慢 6 条：")
    for r in sorted(rows, key=lambda x: -x["sec"])[:6]:
        print("    %.2fs  route=%-14s intent=%-20s 「%s」" % (r["sec"], r["route"], r["intent"], r["text"][:34]))
    by_route = {}
    for r in rows:
        by_route.setdefault(r["route"], [0, 0.0])
        by_route[r["route"]][0] += 1
        by_route[r["route"]][1] += r["sec"]
    print("  按 route 汇总：")
    for k, (n, s) in sorted(by_route.items(), key=lambda x: -x[1][1]):
        print("    %-14s n=%-3d 合计=%.2fs  均=%.2fs" % (k, n, s, s / n))
    return rows, total


cases = list(BUILTIN_PAIRS)
rows1, tot1 = one_pass("第 1 遍（冷）", cases)
rows2, tot2 = one_pass("第 2 遍（热）", cases)

print("\n===== 差异归因 =====")
print("  首遍 detect 合计 %.2fs / 次遍 %.2fs / 差值 %.2fs" % (tot1, tot2, tot1 - tot2))
d1 = {r["text"]: r["sec"] for r in rows1}
d2 = {r["text"]: r["sec"] for r in rows2}
diff = sorted(((d1[t] - d2[t], t, d1[t], d2[t], next(r["route"] for r in rows1 if r["text"] == t))
               for t in d1), reverse=True)[:6]
print("  差值最大的 6 条（首遍 - 次遍）：")
for dv, t, a, b, route in diff:
    print("    %+.2fs  route=%-14s 冷 %.2fs → 热 %.2fs  「%s」" % (dv, route, a, b, t[:30]))

# LLM provider 冷启动假设的直接验证：同一句连续两次走 LLM 兜底
from llm import llm_client  # noqa: E402
t0 = time.time()
r1 = llm_client.chat([{"role": "user", "content": "回复一个字：好"}], _intent="diagnose")
t1 = time.time() - t0
t0 = time.time()
r2 = llm_client.chat([{"role": "user", "content": "回复一个字：好"}], _intent="diagnose")
t2 = time.time() - t0
print("\n===== LLM 侧（provider 握手假设）=====")
print("  首次 chat=%.2fs  第二次 chat=%.2fs  差值=%.2fs" % (t1, t2, t1 - t2))
print("  (若差值接近上面那 8s，则 8s ≈ 评测集里几条 LLM 兜底用例的**首次 provider 握手**)")
print("\n[RESULT]" + __import__("json").dumps(
    {"detect_pass1": round(tot1, 2), "detect_pass2": round(tot2, 2), "llm_first": round(t1, 2),
     "llm_second": round(t2, 2)}, ensure_ascii=False))