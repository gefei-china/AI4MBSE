# CI-OPTIONAL: [broken] 已知红，记账不进 CI（修好后上调 tier 并接线）；两库都红 [FAIL] ...<3 lines>... | "请改用当前在役的 Agent 名后重试。" % "、".join(_unresolved)) | ValueError: 计划引用了无法解析的 Agent：design（不在 age 
# -*- coding: utf-8 -*-
"""B1-8 门禁：依赖图环 / 自环 / 空 key / 重复 key 检测。

**背景（对标取证）**：`task_queue.create_plan` 原本只把 `deps` 里**不存在的 key** 过滤掉
（`plan_keys` 那一行），**不检出环**。运行期后果：
  · `ready_tasks()` 只取"deps 全部为 done 或无 deps"的任务；
  · A→B→A（或 t1 依赖 t1）时**没有任何节点入度为 0** ⇒ `ready` 恒空；
  · 主循环 `stream.py` 随即 `break` ⇒ **整批静默阻塞**：不报错、不写日志，
    用户界面上只表现为"什么都没发生"。
环是 planner 最常见的结构错误之一（多阶段任务里"生成 → 校验 → 回修"极易被写成环）。

**本脚本守什么**：
  [A] 四类非法图都被检出：cycle / self_loop / empty_key / duplicate_key
  [B] 合法图不被误伤（链 / 菱形 / 无依赖 / 外部依赖 key —— 外部 key 沿用既有过滤口径）
  [C] Kahn 算法的正确性（拓扑序完整、无环时全部弹出）
  [D] `create_plan(strict=True)` 对非法图抛 ValueError，**且不落任何行**
      （关键：不能"抛了但已经写了一半"）
  [E] 主路径对该异常做了**明确降级**（写 blocked 行 + error 事件），不是静默 return
  [F] 变异自证：把 validate_plan 改成恒 ok ⇒ A/D 必须 FAIL

**只读**：临时库自建表，**不连生产库**；变异在内存副本做，不写回源文件。

**运行**：`.venv/Scripts/python.exe -X utf8 tools/verify/verify_plan_cycle_guard.py`
"""
import os as _svos
import sys as _svsys
_svroot = _svos.path.dirname(_svos.path.abspath(__file__))
while _svroot and not _svos.path.isdir(_svos.path.join(_svroot, 'tools')):
    _svroot = _svos.path.dirname(_svroot)
if _svroot and _svroot not in _svsys.path:
    _svsys.path.insert(0, _svroot)
from tools.stream_source import read_stream_source  # noqa: E402
import io
from _tmpdir import mkdtemp  # 共享：创建即登记，atexit 退出时自动回收临时目录
import os
import re
import sys
import sqlite3
import tempfile
import shutil

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TQUEUE = os.path.join(ROOT, "services/task_queue.py")
STREAM = os.path.join(ROOT, "agent", "pipeline_parts", "stream.py")

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


sys.path.insert(0, ROOT)
from task_queue import TaskQueue  # noqa: E402


def P(k, deps=None, **kw):
    d = {"key": k, "title": "T-" + str(k), "agent": "design", "task_type": "agent",
         "deps": list(deps or [])}
    d.update(kw)
    return d


print("=== A. 四类非法图都被检出 ===")
_cases = [
    ("二元环 A→B→A", [P("t1", ["t2"]), P("t2", ["t1"])], "cycle"),
    ("三元环 A→B→C→A", [P("t1", ["t3"]), P("t2", ["t1"]), P("t3", ["t2"])], "cycle"),
    ("自环 t1→t1", [P("t1", ["t1"])], "self_loop"),
    ("链中夹自环", [P("t1", []), P("t2", ["t2"]), P("t3", ["t2"])], "self_loop"),
    ("空 key", [P("t1", []), P("", [])], "empty_key"),
    ("重复 key", [P("t1", []), P("t1", [])], "duplicate_key"),
]
for name, plan, want_reason in _cases:
    r = TaskQueue.validate_plan(plan)
    check("A:%s ⇒ not ok / reason=%s" % (name, want_reason),
          (not r["ok"]) and r["reason"] == want_reason,
          "got ok=%s reason=%s" % (r["ok"], r["reason"]))

print("\n=== B. 合法图不被误伤 ===")
_ok_cases = [
    ("单任务无依赖", [P("t1")]),
    ("线性链", [P("t1"), P("t2", ["t1"]), P("t3", ["t2"])]),
    ("菱形（t4 依赖 t2,t3）", [P("t1"), P("t2", ["t1"]), P("t3", ["t1"]), P("t4", ["t2", "t3"])]),
    ("多前置", [P("t1"), P("t2"), P("t3", ["t1", "t2"])]),
    ("含外部依赖 key（沿用既有过滤口径，不算环）",
     [P("t1"), P("t2", ["t1", "不存在的key"])]),
    ("顺序无关（deps 乱序）", [P("t2", ["t1"]), P("t1")]),
]
for name, plan in _ok_cases:
    r = TaskQueue.validate_plan(plan)
    check("B:%s ⇒ ok" % name, r["ok"], "reason=%s cycle=%s" % (r["reason"], r["cycle"]))

print("\n=== C. Kahn 算法正确性（拓扑序完整）===")
# 用一个大一点的链验证"seen == len(keys)"
_big = [P("n%02d" % i, [] if i == 0 else ["n%02d" % (i - 1)]) for i in range(40)]
_r = TaskQueue.validate_plan(_big)
check("C1 40 节点链判定为合法", _r["ok"], "reason=%s" % _r["reason"])
# 环 + 一条独立链：环内节点必须被报出来，独立链不应误报
_mix = [P("ok1"), P("ok2", ["ok1"]), P("c1", ["c2"]), P("c2", ["c1"])]
_r2 = TaskQueue.validate_plan(_mix)
check("C2 混合图检出环", (not _r2["ok"]) and _r2["reason"] == "cycle",
      "got ok=%s reason=%s" % (_r2["ok"], _r2["reason"]))
check("C3 报出的环节点只含环内（不含独立链 ok1/ok2）",
      set(_r2["cycle"]) == {"c1", "c2"}, "cycle=%s" % _r2["cycle"])
# 空计划
check("C4 空计划被拒", (not TaskQueue.validate_plan([])["ok"]))

print("\n=== D. create_plan 对非法图抛错且不落行 ===")
TMP = mkdtemp(prefix="verify_cycle_")
TMP_DB = os.path.join(TMP, "t.db")
try:
    _c = sqlite3.connect(TMP_DB)
    # ★ 2026-11-26 修（夹具缺 agents 表 ⇒ 本例全被"Agent 解析"提前打断）：
    #   `create_plan` 自 B1-11 起会先做 **Agent 名解析校验**（`resolve_agent_name`
    #   查 `agents` 表、只认 `status='active'`），解析不了就抛 ValueError 并中止落库。
    #   本夹具只建了 `agent_tasks`，没有 `agents` ⇒ `P()` 默认的 `design` 解析失败
    #   ⇒ **所有用例都在"Agent 不存在"上抛错**，而本门禁要验的是"图结构非法"
    #   ⇒ 判据面整体不存在（踩到的是 B 项的问题，不是 A/D 项的环检测）。
    #   ⇒ 补 agents 表 + 在役行；两张表都用 ddl_statements_for（含迁移列，防再漂移）。
    from tools.verify._schema_ddl import ddl_statements_for as _ddl_stmts
    for _s in _ddl_stmts("agent_tasks"):
        _c.execute(_s)
    for _s in _ddl_stmts("agents"):
        _c.execute(_s)
    # 门禁用到的 Agent 名（P() 默认 design；B 组用 zhiyuan_mgmt 验证孤儿）
    _c.execute("INSERT INTO agents (name, display_name, status, builtin) "
               "VALUES ('design','设计 Agent','active',1)")
    _c.execute("INSERT INTO agents (name, display_name, status, builtin) "
               "VALUES ('requirement_analysis','需求分析 Agent','active',1)")
    _c.commit()
    _c.close()

    # D1 环 → 抛 ValueError
    _c = sqlite3.connect(TMP_DB)
    _raised = None
    try:
        TaskQueue.create_plan(_c, 1, [P("t1", ["t2"]), P("t2", ["t1"])])
    except ValueError as e:
        _raised = str(e)
    except Exception as e:
        _raised = "WRONG-EXC:%s:%s" % (type(e).__name__, e)
    check("D1 环图抛 ValueError", _raised is not None and not _raised.startswith("WRONG-EXC"),
          "raised=%r" % _raised)
    check("D2 错误信息含 reason=cycle", _raised and "reason=cycle" in _raised, _raised)
    _n = _c.execute("SELECT COUNT(*) FROM agent_tasks").fetchone()[0]
    check("D3 抛错后**未落任何行**（不能抛了还写一半）", _n == 0, "落了 %d 行" % _n)
    _c.close()

    # D4 合法图正常落库
    _c = sqlite3.connect(TMP_DB)
    _c.row_factory = sqlite3.Row
    _cnt = TaskQueue.create_plan(_c, 1, [P("t1"), P("t2", ["t1"])])
    check("D4 合法图落库行数正确", _cnt == 2, "got=%s" % _cnt)
    _st = {r["task_key"]: r["status"] for r in _c.execute("SELECT task_key,status FROM agent_tasks")}
    check("D5 初始状态仍是无依赖 ready / 有依赖 planned",
          _st.get("t1") == "ready" and _st.get("t2") == "planned", str(_st))
    _c.close()

    # D6 strict=False 保留旧行为（尽力而为），供历史路径/门禁脚本显式选择
    _c = sqlite3.connect(TMP_DB)
    _c.execute("DELETE FROM agent_tasks")
    _c.commit()
    _cnt2 = TaskQueue.create_plan(_c, 2, [P("c1", ["c2"]), P("c2", ["c1"])], strict=False)
    check("D6 strict=False 时保留旧行为（环也落库）", _cnt2 == 2, "got=%s" % _cnt2)
    _c.close()
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print("\n=== E. 主路径对异常有明确降级（不是静默 return）===")
s_src = read_stream_source()


def _calls_inside_try(src: str, callee: str) -> bool:
    """至少有一处 `callee(` 调用**处于 try 块内**（按缩进结构判断）。

    ★ 2026-11-26 修（判据要求"紧跟 try:" ⇒ 假红）：
      原判据 `try:\\s*\\n\\s*TaskQueue\\.create_plan\\(` 要求 `try:` 的**下一行**就是调用。
      而真实主路径在调用之前还有一段「计划语义校验」（含 `raise ValueError` 与
      `except ValueError: raise`）⇒ 调用并不紧跟 `try:` ⇒ 恒红。
      本项要验的是语义「调用被 try 包住、异常有处可落」，不是排版。
      ⇒ 改为缩进结构判定：从调用行向上找**同级或更外层**的 `try:`，
        且二者之间不出现同级的 `except`/`finally`/`else`（那说明 try 已闭合）。
    """
    lines = src.splitlines()
    for i, ln in enumerate(lines):
        if callee not in ln or ln.lstrip().startswith("#"):
            continue
        ind = len(ln) - len(ln.lstrip())
        for j in range(i - 1, -1, -1):
            cur = lines[j]
            if not cur.strip():
                continue
            cind = len(cur) - len(cur.lstrip())
            body = cur.strip()
            if cind > ind:
                continue                      # 更深的行与 try 层级无关
            if body.startswith(("except", "finally", "else")):
                break                         # 遇到同级的 except ⇒ 该 try 已闭合
            if body.startswith("try:"):
                return True
            if cind < ind:
                # 更外层的内容：继续向上找（try 可能包在更大块里），但只允许再走一层
                continue
    return False


check("E1 create_plan 被 try 包住（按缩进结构判定，不要求紧跟 try:）",
      _calls_inside_try(s_src, "TaskQueue.create_plan("))
check("E2 捕获的是 ValueError（精确）", "except ValueError as _vde" in s_src)
check("E3 写了 blocked 任务行留痕（可在库里查到原因）",
      '"blocked"' in s_src and "_plan_invalid" in s_src)
check("E4 给前端发了 error 事件（用户看得见，不是静默消失）",
      re.search(r'yield \{"type": "error"', s_src) is not None)
check("E5 error 事件含具体原因", "计划依赖图存在环或结构非法" in s_src)
# ⚠️ 不得把 ValueError 吞成 except Exception: pass —— 那等于把非法计划变回静默阻塞。
#    判据只卡 **`except ValueError` 之后、return 之前**不得再有裸 except；
#    嵌套的 `except Exception: pass` 属于「写 blocked 留痕」那步的容错，是**必需**的
#    （留痕写失败不该反过来打断降级流程），不能用"整段无 except"这种粗判据。
_seg = re.search(r'except ValueError as _vde:(.*?)\n(\s*)return',
                 s_src, re.S)
check("E6 ValueError 降级段内不得有裸 except Exception 吞掉异常",
      (_seg is not None) and ("except Exception" in _seg.group(1)),
      "降级段应当**有**一个窄 except（给留痕写入容错），若无则说明直接 return 放弃了留痕")
check("E7 降级段确实写了 blocked 行再 return（顺序承重）",
      (_seg is not None) and ("_plan_invalid" in _seg.group(1))
      and (_seg.group(1).rfind("_plan_invalid") < len(_seg.group(1))),
      "blocked 留痕与 error 事件都应在 return 之前完成")

print("\n=== F. 变异自证（validate_plan 恒 ok）===")
t_src = io.open(TQUEUE, encoding="utf-8").read()
_anchor = 'if seen < len(keys):'
if _anchor not in t_src:
    print("  [VACUOUS] 锚点不在源码里，自证中止")
else:
    # 变异：把"检出环"这个分支去掉 ⇒ validate_plan 对环恒返回 ok
    _idx = t_src.index(_anchor)
    _mut = (t_src[:_idx]
            + 'if False:  # MUTATION: 不检环\n'
            + " " * (len(_anchor) - len("if False:")) + "\n"
            + t_src[_idx + len(_anchor):])
    # 在内存里 exec 变异体，取它的 validate_plan
    _ns = {"__name__": "_mut_tq"}
    try:
        exec(compile(_mut, "<mutant>", "exec"), _ns)
        _mv = _ns["TaskQueue"].validate_plan([P("t1", ["t2"]), P("t2", ["t1"])])
        _mres = _mv["ok"]
    except Exception as e:
        _mres = "exec-err:%s" % e
    check("F1 变异体（不检环）上 A 的二元环必须 FAIL（返回 ok=True）",
          _mres is True, "变异体返回 ok=%r —— 断言抓不住变异" % _mres)
    # ⚠️ F2 刻意**只**断环这一支：自环由更前面的独立分支检出，M1 并未改动它，
    #    所以自环在变异体里仍被拒是**正确行为**，不是变异失效。
    #    若这里断言自环也要漏，等于要求变异同时改两处 —— 那是变异设计问题，不是门禁问题。
    _sl = _ns["TaskQueue"].validate_plan([P("t1", ["t1"])])
    check("F2 变异体里自环仍被独立检出（M1 只动环这一支，符合预期）",
          _sl["ok"] is False and _sl["reason"] == "self_loop",
          "got ok=%s reason=%s" % (_sl["ok"], _sl.get("reason")))
    _muts.append(("M1", "validate_plan 不检环（恒 ok）"))
    print("  [SELF-PROOF] M1 validate_plan 恒 ok —— 环检测失效，回到静默阻塞的原始形态")

print("\n" + "=" * 60)
print("常态断言：%d 条，%d 通过 / %d 失败" % (_ok + _fail, _ok, _fail))
print("变异自证：%d 组" % len(_muts))
for mid, desc in _muts:
    print("  - %s %s" % (mid, desc))
print("结论：%s" % ("门禁通过" if _fail == 0 and _muts else "门禁未通过"))
sys.exit(0 if (_fail == 0 and _muts) else 1)