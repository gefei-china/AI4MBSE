# -*- coding: utf-8 -*-
"""Dreaming 闭环末端门禁（2026-11-12）：效果可测、界面可点、数字不编造。

上一批的教训是「函数级判据测不出没人调用」：`memory_dream.py` 功能齐全、
门禁全绿，但全仓零调用方。本门禁守的是**闭环最后两环**：

  U1 前端真的引用 preview / apply / persist / ab-report（不只是后端有端点）
  U2 HTML 面板里这四个入口都有对应按钮/容器
  U3 前端不提供「全部应用」、不把 needs_review 混进可勾选项（保守判据的 UI 防线）
  U4 apply 回传 fingerprint + note_limit（后端按同一窗口重算指纹）
  E1 双臂评测工具的 A/B 是**同一评测集**
  E2 报告里未实测项不拿 0 冒充（unit_note 与 interpretation 必须在）
  E3/E4 真库只读 + 评测跑副本；主判据是 db_untouched 状态指纹（不是命中率代理）
  E7 行为性：往副本里写脏，被测库指纹必须纹丝不动
  M1~M4 变异自证：删前端引用 / 去掉 note_limit 回传 / 去掉 rollback /
                    **副本改回直连**（M4 是 2026-11-12 真库污染事故的反证）
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
check("E3 真库连接只读 + 评测跑在 backup 副本上，且不调 apply_candidates",
      "mode=ro" in EVAL and ".backup(work)" in EVAL
      and "snapshot_only" in EVAL and "apply_candidates" in EVAL,
      "B 臂若直连真库，record_access 的 commit 会让 rollback 失效")
check("E4 主判据是 db_untouched（真库状态前后 sha256），不以命中率当代理",
      "db_untouched" in EVAL and "live_fingerprint" in EVAL
      and 'return 0 if r["db_untouched"]' in EVAL,
      "旧版只比 memory_hit_rate，而 verify_state 不参与排序 ⇒ 污染了也恒绿")
check("E5 报告标注计量单位（字符而非 token）与 hypothetical 性质",
      "unit_note" in EVAL and "hypothetical" in EVAL
      and "不得据此宣称" in EVAL)

report = os.path.join(ROOT, "data", "memory_dream_ab.json")
if os.path.exists(report):
    try:
        r = json.load(open(report, encoding="utf-8"))
        check("E6 实测报告：真库零写入 + schema2 副本口径 + delta 来自真实 A/B",
              r.get("db_untouched") is True
              and r.get("schema", 1) >= 2
              and r.get("snapshot_only") is True
              and r.get("db_fingerprint_before") == r.get("db_fingerprint_after")
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

print("\n[2b] 行为性：副本隔离真的挡得住写入（2026-11-12 真库污染事故）", flush=True)


def _load_eval(src):
    """把评测工具源码当模块加载（可施加变异）；不触发 __main__ 分支。"""
    ns = {"__name__": "eval_ab_under_test",
          "__file__": os.path.join(ROOT, "tools/eval/eval_memory_dream_ab.py")}
    exec(compile(src, ns["__file__"], "exec"), ns)  # noqa: S102 门禁自证用
    return ns


def _probe_db():
    import sqlite3
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _tmpdir import mkdtemp
    p = os.path.join(mkdtemp(prefix="wb_eval_probe_"), "probe.db")
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE agent_memory (id INTEGER PRIMARY KEY, content TEXT, "
              "verify_state TEXT DEFAULT 'unverifiable', "
              "evidence_ref TEXT DEFAULT '', evidence_note TEXT DEFAULT '')")
    for i in range(1, 6):
        c.execute("INSERT INTO agent_memory (id,content) VALUES (?,?)",
                  (i, "探针记忆 %d" % i))
    c.commit()
    c.close()
    return p


def _mut_eval_direct(src):
    """把 backup 副本改回「直连被测库」——精确复现污染事故的根因。"""
    a = ("    src = sqlite3.connect(_ro_uri(db_path), uri=True, timeout=30)\n"
         "    work = sqlite3.connect(snap, timeout=30)\n"
         "    try:\n"
         "        src.backup(work)\n"
         "    finally:\n"
         "        src.close()")
    assert a in src, "M4 锚点失效（副本构造已被改写）"
    return src.replace(a, "    work = sqlite3.connect(db_path, timeout=30)  # 变异：直连", 1)


def _write_through(ns, db_path):
    """走评测工具的副本连接写一次脏，返回写前后的真库状态指纹。"""
    before = ns["live_fingerprint"](db_path)
    work, _ = ns["_work_copy"](db_path)
    work.execute("UPDATE agent_memory SET verify_state='verified'")
    work.commit()
    work.close()
    return before, ns["live_fingerprint"](db_path)


try:
    _p = _probe_db()
    _b, _a = _write_through(_load_eval(EVAL), _p)
    check("E7 副本里写入后，被测库持久状态指纹不变（真库零写入）",
          _b == _a, "before=%s after=%s" % (_b[:12], _a[:12]))
    try:
        _b2, _a2 = _write_through(_load_eval(_mut_eval_direct(EVAL)), _p)
        check("M4 副本改回直连 ⇒ 被测库指纹必须变化（E7 的判据有牙齿）",
              _b2 != _a2, "变异后仍为 %s ⇒ 判据抓不住污染" % _a2[:12])
    except AssertionError as e:
        check("M4 变异可施加", False, str(e))
    except Exception as e:  # noqa: BLE001
        check("M4 变异体可执行", False, "%s: %s" % (type(e).__name__, e))
except Exception as exc:  # noqa: BLE001
    check("E7 行为性夹具可运行", False, "%s: %s" % (type(exc).__name__, exc))

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
    check("M3 B 臂去掉 rollback ⇒ 结构变异可施加（副本内仍保留该防线）",
          "conn.rollback()" not in _mut_eval(EVAL))
except AssertionError as e:
    check("M3 变异可施加", False, str(e))

print("\n=== %d PASS / %d FAIL ===" % (len(PASS), len(FAIL)), flush=True)
for f in FAIL:
    print("  x %s" % f, flush=True)
sys.exit(1 if FAIL else 0)
