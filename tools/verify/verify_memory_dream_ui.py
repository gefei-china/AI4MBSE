# -*- coding: utf-8 -*-
"""Dreaming 闭环末端门禁（2026-11-12）：效果可测、界面可点、数字不编造。

上一批的教训是「函数级判据测不出没人调用」：`memory_dream.py` 功能齐全、
门禁全绿，但全仓零调用方。本门禁守的是**闭环最后两环**：

  U1 前端真的引用 preview / apply / persist / ab-report（不只是后端有端点）
  U2 HTML 面板里这四个入口都有对应按钮/容器
  U3 前端不提供「全部应用」、不把 needs_review 混进可勾选项（保守判据的 UI 防线）
  U4 apply 回传 fingerprint + note_limit（后端按同一窗口重算指纹）
  E1 双臂评测工具的 A/B 是**同一评测集**且 B 臂不落盘（rollback 复证）
  E2 报告里未实测项不拿 0 冒充（unit_note 与 interpretation 必须在）
  M1~M3 变异自证：删掉前端引用 / 去掉 note_limit 回传 / B 臂改成不 rollback
"""
import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    if cond:
        print("  PASS %s" % name, flush=True)
    else:
        print("  FAIL %s  <- %s" % (name, detail), flush=True)


def _read(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as f:
        return f.read()


JS = _read("static/js/mods/44-memory.js")
HTML = _read("static/index.html")
API = _read("routers/memory_admin.py")
EVAL = _read("tools/eval/eval_memory_dream_ab.py")

print("\n[1] 前端接线（端点要有真实调用方）", flush=True)
for ep in ("/api/memory/dream/preview", "/api/memory/dream/apply",
           "/api/memory/dream/persist", "/api/memory/dream/ab-report"):
    check("U1 前端引用 %s" % ep, ep in JS, "44-memory.js 里找不到该路径")
check("U1b 后端确实实现了这 4 个端点",
      all(('@router.get("%s")' % ep) in API or ('@router.post("%s")' % ep) in API
          for ep in ("/api/memory/dream/preview", "/api/memory/dream/apply",
                     "/api/memory/dream/persist", "/api/memory/dream/ab-report")),
      "后端路由定义缺失")

for fn in ("memDreamPreview()", "memDreamApply()",
           "memDreamPersist()", "memDreamAB()"):
    check("U2 HTML 面板有 %s 入口" % fn, fn in HTML)

check("U3 前端只对 contradicted 生成勾选框（不批量批准 needs_review）",
      "c.state === 'contradicted'" in JS
      and "review" in JS and "不提供勾选" in JS,
      "判据过宽会在这里失守：需人工项不得混入可勾选项")
check("U3b apply 前必须二次确认，且勾选为空时拒绝",
      "confirmDialog" in JS and "请至少勾选一条候选" in JS)

check("U4 apply 回传 fingerprint + note_limit",
      "fingerprint: _memDreamPkg.fingerprint" in JS
      and "note_limit: _memDreamPkg.note_limit" in JS,
      "缺任一都会让后端 409（上一轮真机抓到的缺陷）")

print("\n[2] 双臂评测口径（不编造数字）", flush=True)
check("E1 评测集复用既有真实用例（T/M/C/N 四组），不另造 query",
      "eval_memory_quality" in EVAL
      and all(x in EVAL for x in ("Q.T_CASES", "Q.M_CASES", "Q.C_CASES", "Q.N_CASES")))
check("E2 A/B 用同一 measure() 与同一 cases",
      EVAL.count("measure(conn, cases, noise)") >= 3,
      "A/B/rollback 复证三处必须调用同一函数")
check("E3 B 臂用未提交事务 + rollback，且明确不调 apply_candidates",
      'conn.execute("BEGIN")' in EVAL and "conn.rollback()" in EVAL
      and "apply_candidates" in EVAL,
      "B 臂若 commit 会把影子状态写进真库")
check("E4 rollback 后复测 A 臂，作为'没写脏'的自证",
      "rollback_clean" in EVAL and "arm_a_after_rollback" in EVAL)
check("E5 报告标注计量单位（字符而非 token）与 hypothetical 性质",
      "unit_note" in EVAL and "hypothetical" in EVAL
      and "不得据此宣称" in EVAL)

report = os.path.join(ROOT, "data", "memory_dream_ab.json")
if os.path.exists(report):
    try:
        r = json.load(open(report, encoding="utf-8"))
        check("E6 实测报告 rollback_clean=True 且 delta 来自真实 A/B 数值",
              r.get("rollback_clean") is True
              and r["delta"]["memory_hit_rate"] ==
              round(r["arm_b_shadow_hypothetical"]["memory_hit_rate"] -
                    r["arm_a_baseline"]["memory_hit_rate"], 4))
        print("  · 实测：A=%s / B=%s / delta=%s / 影子状态=%s（证伪 %s / 需人工 %s）" %
              (r["arm_a_baseline"]["memory_hit_rate"],
               r["arm_b_shadow_hypothetical"]["memory_hit_rate"], r["delta"],
               r["shadow_states"], r["shadow_contradicted"],
               r["shadow_needs_review"]), flush=True)
    except Exception as exc:  # noqa: BLE001
        check("E6 实测报告可解析", False, "%s: %s" % (type(exc).__name__, exc))
else:
    print("  SKIP E6 暂无实测报告（运行 tools/eval/eval_memory_dream_ab.py 生成）",
          flush=True)

print("\n[3] 变异自证", flush=True)


def _mut_js(src):
    a = "    const d = await api('/api/memory/dream/preview?note_limit=300');"
    assert a in src, "M1 锚点失效"
    return src.replace(a, "    const d = {};", 1)


def _mut_js_limit(src):
    a = "                             note_limit: _memDreamPkg.note_limit,"
    assert a in src, "M2 锚点失效"
    return src.replace(a, "", 1)


def _mut_eval(src):
    a = "            conn.rollback()"
    assert a in src, "M3 锚点失效"
    return src.replace(a, "            pass  # 变异：不 rollback", 1)


for label, mutate, target in (
    ("M1 前端不再引用 preview ⇒ U1 翻红", _mut_js, JS),
    ("M2 前端不回传 note_limit ⇒ U4 翻红", _mut_js_limit, JS),
):
    try:
        bad = mutate(target)
        check(label, "/api/memory/dream/preview" not in bad
              if label.startswith("M1") else "note_limit: _memDreamPkg.note_limit" not in bad,
              "变异后判据仍绿 ⇒ 抓不住回归")
    except AssertionError as e:
        check(label, False, str(e))

try:
    check("M3 B 臂不 rollback ⇒ E3 翻红",
          "conn.rollback()" not in _mut_eval(EVAL))
except AssertionError as e:
    check("M3 变异可施加", False, str(e))

print("\n=== %d PASS / %d FAIL ===" % (len(PASS), len(FAIL)), flush=True)
for f in FAIL:
    print("  x %s" % f, flush=True)
sys.exit(1 if FAIL else 0)
