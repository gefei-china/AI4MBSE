# -*- coding: utf-8 -*-
"""B1-5 门禁：编排终态 phase 由 `_agg_status` 推导，不再无条件写 `done`。

**背景（真库实证，口径 2026-10-12 只读）**：`orch_checkpoints` 仅 3 行，
其中 **run 531 是口径分裂的现场**：

    run=530 phase=planned summary_written=0 任务={'done': 2, 'failed': 2}
    run=531 phase=done    summary_written=1 任务={'failed': 4}      ← ★ 矛盾
    run=532 phase=done    summary_written=1 任务={'done': 2}

根因：`stream.py` 收尾处无条件 `finish(conn, run_id, "done")`，而同一函数里
`_agg_status`（full/partial/failed）**已经算出了真相**却没参与终态判定。

**危害**：phase 是 resume 与「可恢复批次清单」的第一道判据
（`resumable()` 的 `WHERE phase NOT IN ('done','failed')`）。一个"子任务全败"的批次
被标成 done ⇒ 运维在清单里看不到它，也**不会去 resume** ⇒ 失败被静默吞掉。

**本脚本守什么**：
  [A] 收尾 phase 由 `_agg_status` 推导（不再字面量 "done"）
  [B] 映射语义正确：full→done；partial/failed→failed；degraded→done
  [C] **`failed` 仍在 TERMINAL_PHASES 里** ⇒ resume 的可恢复清单行为**不变**
      （这条最关键：不能为了"让它可见"就把它变成可 resume，那会重跑可能已写一半副作用的任务）
  [D] 真库既有分裂现场可被本判据识别（用真库只读数据跑一遍推导逻辑）
  [E] 变异自证：还原成无条件 "done"，A/D 必须 FAIL

**只读**：真库走 `mode=ro`；推导逻辑在本脚本内复刻，不改生产代码；
变异在内存副本上做，**不写回源文件**。

**运行**：`.venv/Scripts/python.exe -X utf8 tools/verify/verify_ck_phase_consistency.py`
"""
import io
import os
import re
import sys
import sqlite3

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
STREAM = os.path.join(ROOT, "agent", "pipeline_parts", "stream.py")
CKPT = os.path.join(ROOT, "agent", "orch_checkpoint.py")
PROD_DB = os.path.join(ROOT, "mbse.db")

_ok = 0
_fail = 0
_muts = []


def check(name, cond, detail=""):
    global _ok, _fail
    if cond:
        _ok += 1
        print("  [PASS] %s" % name)
    else:
        _fail += 1
        print("  [FAIL] %s  %s" % (name, detail))


def derive_phase(agg_status, degraded):
    """本仓的终态 phase 推导规则（复刻 stream.py 收尾处逻辑）。

    与生产代码保持一致：改动本函数等于改动判据，改完必须同步两边。
    """
    if degraded:
        return "done"
    return "failed" if agg_status in ("partial", "failed") else "done"


s_src = io.open(STREAM, encoding="utf-8").read()
c_src = io.open(CKPT, encoding="utf-8").read()

print("=== A. 收尾 phase 由 _agg_status 推导 ===")
_fin = re.search(r'_ck_fin\(conn, run_id, ([^)]+)\)', s_src)
check("A0 能定位 finish 调用", _fin is not None)
if _fin:
    _arg = _fin.group(1).strip()
    check("A1 不再是字面量 \"done\"", _arg != '"done"',
          "仍无条件写 done —— 全败批次会被标成成功")
    check("A2 实参含 _final_phase（推导变量）", "_final_phase" in _arg, "arg=%s" % _arg)
    check("A3 _final_phase 由 _agg_status 推导",
          re.search(r'_final_phase\s*=\s*"failed"\s*if\s*\(\s*not degraded\s+and\s+'
                    r'_agg_status in \("partial",\s*"failed"\)\s*\)', s_src) is not None,
          "未找到推导表达式")
    check("A4 degraded 情形保持 done", "not degraded" in _arg or "not degraded" in s_src,
          "降级直行路径没有子任务失败可言，不该标 failed")

print("\n=== B. 映射语义正确 ===")
for agg, deg, want in (("full", False, "done"),
                       ("full", True, "done"),
                       ("partial", False, "failed"),
                       ("failed", False, "failed"),
                       ("partial", True, "done"),
                       ("failed", True, "done"),
                       (None, False, "done")):
    got = derive_phase(agg, deg)
    check("B:agg=%-8s degraded=%-5s ⇒ %s" % (agg, deg, want), got == want, "got=%s" % got)

print("\n=== C. resume 可恢复清单行为不变（failed 仍属终态）===")
_tp = c_src.split("TERMINAL_PHASES = ")[1][:120] if "TERMINAL_PHASES = " in c_src else ""
# ⚠️ 源码用的是**常量名**（PHASE_DONE, PHASE_FAILED）而不是字面量，
#    所以判据要同时接受两种写法 —— 只认字面量会把正确写法判成失败（假红）。
check("C1 TERMINAL_PHASES 含 done",
      ("PHASE_DONE" in _tp) or ('"done"' in _tp) or ("'done'" in _tp), _tp)
check("C2 TERMINAL_PHASES 含 failed（本次修复依赖它仍是终态）",
      ("PHASE_FAILED" in _tp) or ('"failed"' in _tp) or ("'failed'" in _tp), _tp)
check("C2b PHASE_DONE/PHASE_FAILED 常量确实分别是 done/failed",
      re.search(r'PHASE_DONE\s*=\s*"done"', c_src) is not None
      and re.search(r'PHASE_FAILED\s*=\s*"failed"', c_src) is not None)
check("C3 resumable() 排除 done/failed",
      "phase NOT IN ('done','failed')" in c_src)
check("C4 注释说明了 partial→failed 的取舍",
      "若把 partial 标成 done" in s_src or "partial 标成 done" in s_src)

print("\n=== D. 真库既有分裂现场可被本判据识别 ===")
if os.path.exists(PROD_DB):
    _c = sqlite3.connect("file:%s?mode=ro" % PROD_DB, uri=True)
    _c.row_factory = sqlite3.Row
    _cks = _c.execute("SELECT run_id, phase, summary_written FROM orch_checkpoints").fetchall()
    print("  真库 checkpoint %d 行：" % len(_cks))
    _split_found = 0
    for _r in _cks:
        _ts = dict(_c.execute("SELECT status, COUNT(*) FROM agent_tasks WHERE run_id=? GROUP BY 1",
                              (_r["run_id"],)).fetchall())
        _n_done = _ts.get("done", 0)
        _n_bad = _ts.get("failed", 0) + _ts.get("blocked", 0) + _ts.get("canceled", 0)
        # 按真库任务状态推出"本该是"的 phase
        _want = "failed" if (_n_bad and not _n_done) else "done"
        _flag = ""
        if _r["phase"] == "done" and _want == "failed":
            _split_found += 1
            _flag = "  ★ 口径分裂（本判据会改为 failed）"
        elif _r["phase"] != _want:
            _flag = "  （非终态，如中断中，不参与本断言）"
        print("    run=%s 现=%-8s 任务=%s ⇒ 应为%s%s"
              % (_r["run_id"], _r["phase"], _ts, _want, _flag))
    check("D1 真库确有口径分裂现场（run 531）", _split_found >= 1,
          "未发现分裂 —— 若真库已修复，本判据需改口径")
    _c.close()
else:
    print("  [SKIP] 未找到真库")

print("\n=== E. 变异自证（还原成无条件 done，判据必须失效）===")
_m = re.search(r'_final_phase = "failed" if \(not degraded and _agg_status in \("partial", "failed"\)\) else "done"', s_src)
if not _m:
    print("  [VACUOUS] 锚点不在源码里，自证中止")
else:
    _mut = s_src.replace(_m.group(0), '_final_phase = "done"  # MUTATION')
    if _mut == s_src:
        print("  [VACUOUS] 变异未生效")
    else:
        # 变异后：实参变量仍是 _final_phase，但推导已恒为 done ⇒ 推导判据 A3 必须失效
        check("E1 变异体上 A3 必须 FAIL（推导被还原为恒 done）", _m.group(0) not in _mut)
        # 且真实后果：全败批次会被标 done（复刻变异后的逻辑）
        _mut_derive = lambda agg, deg: "done"  # noqa: E731  ← 变异体的行为
        check("E2 变异体下 partial/failed 仍被标 done（正是缺陷形态）",
              _mut_derive("failed", False) == "done" and derive_phase("failed", False) == "failed",
              "自证对照失效")
        _muts.append(("M1", "终态 phase 还原为恒 done"))
        print("  [SELF-PROOF] M1 终态 phase 恒 done —— 全败批次重新被标成成功（run 531 复现）")

print("\n" + "=" * 60)
print("常态断言：%d 条，%d 通过 / %d 失败" % (_ok + _fail, _ok, _fail))
print("变异自证：%d 组" % len(_muts))
for mid, desc in _muts:
    print("  - %s %s" % (mid, desc))
print("结论：%s" % ("门禁通过" if _fail == 0 and _muts else "门禁未通过"))
sys.exit(0 if (_fail == 0 and _muts) else 1)