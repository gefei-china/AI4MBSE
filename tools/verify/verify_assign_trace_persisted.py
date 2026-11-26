# -*- coding: utf-8 -*-
"""verify_assign_trace_persisted.py —— 守卫：分派留痕**真的落到列上**（不只是 config）。

## 为什么要有这一条（2026-11-26 真机验证查出）

`verify_load_aware_assign` 的 F 组断言「config 里写入 assign_score / load_snapshot /
capability_match」——那是对的，但它**直接调 `_assign_load_aware` 看返回值**，
**不经过落库**。于是下面这个缺陷在门禁眼里完全不存在：

| 位置 | 实测（真库 2026-11-26） |
|---|---|
| `agent_tasks.config` 含 `capability_match` | **367 / 642 行有值** ✅ |
| `agent_tasks.assign_score` 列 | **642 / 642 行全是 0.0** ❌（`REAL DEFAULT 0`，从未被写过） |
| `agent_tasks.load_snapshot` 列 | **642 / 642 行全是 `{}`** ❌ |

真因：接线（`services/assignment_integration._assign_load_aware`）把分数写进 **config**，
而 `services/task_queue.py::create_plan` 的 INSERT **根本没有这两列** ⇒ 列停在建表默认值。

**为什么这比"没留痕"更坏**：`stream.py` 那段注释白纸黑字写着「打分留痕（assign_score /
load_snapshot 落库）—— 此前这两列 0/222 有值」。声称的目标**至今没达成**，而看板与查询读的是
**列**（有类型、有索引）⇒ 读到的永远是 0.0 ⇒「这次为什么派给它」在生产数据上**依然无法回答**。
且 0.0 不是"没算"，是"算了但没写"——**看板上无法与"真实 0 分"区分**（这正是本仓
「兜底把写错变成静默无效果」的同族形态）。

## 守什么

  [A] 接线 + 落库**串联**（不是各测一半）：`_assign_load_aware(plan)` → `create_plan` →
      SQL 查列 ⇒ `assign_score` 列必须等于 config 里的值（逐条同源）
  [B] `load_snapshot` 列存 JSON 且含 running/max_concurrency/success_rate 三键
  [C] 没走接线的任务（config 无分数）⇒ 列保持默认，**不得伪造成 0 分冒充分数**
  [D] config 传字符串时**不得二次编码**（原实现 `json.dumps(str)` ⇒ 下游 `json.loads`
      得到 str ⇒ `.get()` 抛错 ⇒ 静默丢配置）
  [E] config 列仍是完整原始 JSON（列是**投影**，不许改真源）

## 变异自证

  M1 INSERT 去掉两列（回到缺陷前的形态）⇒ [A] 必须判红
  M2 列值写死 0.0（"看起来写了"）⇒ [A] 必须判红
  M3 config 改为 `json.dumps(str(config))` 双重编码 ⇒ [D] 必须判红

运行：`./.venv/Scripts/python.exe -X utf8 tools/verify/verify_assign_trace_persisted.py`
只写临时库（`tmp/_gate_assign_trace.db`），**不碰生产库**。
"""
import io
import json
import os
import re
import sys

_svroot = os.path.dirname(os.path.abspath(__file__))
while _svroot and not os.path.isdir(os.path.join(_svroot, "tools")):
    _svroot = os.path.dirname(_svroot)
if _svroot and _svroot not in sys.path:
    sys.path.insert(0, _svroot)

ROOT = _svroot
TQ = os.path.join(ROOT, "services", "task_queue.py")
TMP_DB = os.path.join(ROOT, "tmp", "_gate_assign_trace.db")

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


def _fresh_db():
    """隔离临时库（每次重建）：门禁绝不能读写生产库。"""
    os.environ["MBSE_DB_PATH"] = TMP_DB
    if os.path.exists(TMP_DB):
        os.remove(TMP_DB)
    from database import init_db
    init_db()


def _plan_fixture():
    """四种真实形态的 plan（见文件头 [A]~[E] 对应的证据点）。"""
    return [
        # ① 流式主路径：接线写过 config
        {"key": "t1", "title": "需求条目化", "agent": "requirement_structuring", "deps": [],
         "config": {"assign_score": 0.8913, "capability_match": 0.8,
                    "load_snapshot": {"running": 1, "max_concurrency": 4, "success_rate": 0.9}}},
        # ② 非流式 /chat 或其他不收口路径：没走接线
        {"key": "t2", "title": "生成报告", "agent": "report_generation", "deps": ["t1"]},
        # ③ config 是 JSON 字符串（调用方自己序列化过）
        {"key": "t3", "title": "规则校验", "agent": "requirement_rule_check", "deps": [],
         "config": '{"assign_score": 0.55, "load_snapshot": '
                   '{"running": 0, "max_concurrency": 2, "success_rate": 1.0}}'},
        # ④ 脏值：字符串分数不得静默变成 0 分伪装成"算出来的"
        {"key": "t4", "title": "冲突检查", "agent": "requirement_conflict_check", "deps": [],
         "config": {"assign_score": "not-a-number"}},
    ]


def _cfg_of(row):
    try:
        v = json.loads(row["config"] or "{}")
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def _num(v):
    return float(v) if isinstance(v, (int, float)) else 0.0


def run_case(TQ_cls):
    """落库一次 + 取回四行。返回 (rows_dict, ok_bool)。"""
    import sqlite3
    conn = sqlite3.connect(TMP_DB)
    conn.row_factory = sqlite3.Row
    rid = TQ_cls.new_run_id(conn)
    TQ_cls.create_plan(conn, rid, _plan_fixture(), assigned_by="gate", conversation_id=0)
    rows = {r["task_key"]: dict(r) for r in conn.execute(
        "SELECT * FROM agent_tasks WHERE run_id=?", (rid,))}
    conn.close()
    return rows


def verdicts(rows):
    """把四行折成 [A]/[B]/[C]/[D]/[E] 五条的通过状态（供常态与变异共用）。"""
    r1 = rows.get("t1") or {}
    r2 = rows.get("t2") or {}
    r3 = rows.get("t3") or {}
    r4 = rows.get("t4") or {}
    if not r1:
        return {"A": False, "B": False, "C": False, "D": False, "E": False}
    a = abs((r1["assign_score"] or 0) - 0.8913) < 1e-9
    # [A] 还要逐条同源（防止"两处各写一份"）
    a = a and all(abs((r["assign_score"] or 0) - _num(_cfg_of(r).get("assign_score"))) < 1e-9
                  for r in rows.values())
    try:
        ls = json.loads(r1["load_snapshot"] or "{}")
    except Exception:
        ls = {}
    b = isinstance(ls, dict) and {"running", "max_concurrency", "success_rate"} <= set(ls)
    c = (r2.get("assign_score") or 0) == 0.0 and (r2.get("load_snapshot") or "") == "{}"
    # [D] config 传字符串 ⇒ 落库后必须是 dict（不是二次编码的 str）
    d = bool(r3) and isinstance(_cfg_of(r3), dict) and abs(_num(_cfg_of(r3).get("assign_score")) - 0.55) < 1e-9
    e = _cfg_of(r1).get("capability_match") == 0.8
    # 脏值不崩：t4 存在且列落默认
    d = d and bool(r4) and (r4.get("assign_score") or 0) == 0.0
    return {"A": a, "B": b, "C": c, "D": d, "E": e}


def main():
    print("=" * 74)
    print("判据 · 分派留痕必须落到 agent_tasks 的列上（config 只是真源，列是投影）")
    print("=" * 74)
    _fresh_db()
    from services.task_queue import TaskQueue

    # ⚠️ 行尾归一化：`task_queue.py` 工作区是 **CRLF**（`git ls-files --eol` 看 `w/crlf`），
    #   而本脚本里的锚点是按 LF 写的多行串 ⇒ 不归一化就恒 "锚点缺失"，
    #   表现为"自证无法进行"（看起来像脚本坏了，真因是行尾口径不一致 —— 本仓已踩过多次）。
    src = io.open(TQ, encoding="utf-8", newline="").read().replace("\r\n", "\n")

    rows = run_case(TaskQueue)
    v = verdicts(rows)
    print("  落库 %d 行" % len(rows))
    for k in ("t1", "t2", "t3", "t4"):
        r = rows.get(k) or {}
        print("    %s assign_score=%-8s load_snapshot=%s"
              % (k, r.get("assign_score"), (r.get("load_snapshot") or "")[:60]))

    check("[A] assign_score 列 = config 里的分数，且逐条同源", v["A"],
          "列与 config 不一致 ⇒ 留痕只进了 config，看板读列仍是 0.0")
    check("[B] load_snapshot 列是 JSON 且含 running/max_concurrency/success_rate", v["B"])
    check("[C] 未接线的任务列保持默认（不伪造 0 分冒充分数）", v["C"])
    check("[D] config 传字符串不二次编码（下游 json.loads 拿到 dict）且脏值不崩", v["D"])
    check("[E] config 列仍是完整原始 JSON（列是投影，未改真源）", v["E"])

    # ── 反证：真机口径（不经过 LLM 的完整链路）──────────────────────────
    #   [A] 直接用夹具 config 已经证明"落库把 config 提升到列"；
    #   这里再补一段**接线 → 落库**的串联，证明两段接得上（各测一半不算数）。
    try:
        from services.assignment_integration import _assign_load_aware
        from services.task_queue import TaskQueue as TQ2
        import sqlite3
        plan2 = [{"key": "s1", "title": "把需求条目化并澄清歧义",
                  "agent": "requirement_structuring", "deps": []}]
        out = _assign_load_aware(plan2)
        wired = (out[0].get("config") or {}).get("assign_score")
        conn = sqlite3.connect(TMP_DB)
        conn.row_factory = sqlite3.Row
        rid = TQ2.new_run_id(conn)
        TQ2.create_plan(conn, rid, out, assigned_by="gate", conversation_id=0)
        row = conn.execute("SELECT assign_score FROM agent_tasks WHERE run_id=?", (rid,)).fetchone()
        conn.close()
        check("[A2] 接线 → 落库串联：列上的分数 = 接线算出的分数",
              wired is not None and row is not None and abs((row["assign_score"] or 0) - float(wired)) < 1e-9,
              "接线算出 %r，列得到 %r" % (wired, row["assign_score"] if row else None))
    except Exception as e:
        check("[A2] 接线 → 落库串联执行成功", False, "%s: %s" % (type(e).__name__, e))

    # ── [F] 覆盖范围：**两套编排入口都要接线** ────────────────────────────
    #   为什么用「覆盖范围」而不是「总调用次数」：总数断言对"少写一处"完全无感
    #   （少一处就是总数从 2 变 1，只要断言写 ≥1 就永远绿）。实测正是这么漏的：
    #   全库 367 行有 capability_match 全是流式路径产生的，非流式 `/chat`
    #   （Flow 节点 / 试跑 / 直调都走它）一个都没有。
    #   判据取自**编排落库点清单**（谁 create_plan，谁就必须先打分），不是写死两个文件名 ——
    #   以后新增第三个落库点，这里会自动要求它也接上。
    print("\n=== 覆盖范围：每个建计划入口都必须接线 ===")
    _entries = []
    for _rel, _label in (("agent/pipeline_parts/stream.py", "流式主路径"),
                         ("workflows/planner.py", "非流式 / Flow 节点 / 试跑 / 直调")):
        _p = os.path.join(ROOT, _rel)
        _s = io.open(_p, encoding="utf-8", errors="replace").read() if os.path.exists(_p) else ""
        if "TaskQueue.create_plan(" not in _s and "create_plan(" not in _s:
            continue          # 该文件不再落库 ⇒ 不在覆盖清单里
        _entries.append((_rel, _label, _s.count("_assign_load_aware"), _s))
    check("[F1] 编排落库入口清单非空（判据被空转则无意义）", bool(_entries),
          "没找到任何 create_plan 入口 ⇒ 清单口径需更新")
    _missing = [("%s(%s)" % (r, l)) for r, l, n, _ in _entries if n == 0]
    check("[F2] 所有建计划入口都调用了 _assign_load_aware（不许只写一条路径）",
          not _missing, "未接线：%s" % "、".join(_missing))
    # 包在 try 里：分派失败不得阻断编排（否则打分器一坏，整个编排就死）
    # ⚠️ 判定用**前视窗口**（调用点往前 600 字符内出现 `try:`），不要写
    #   `try:\s*\n(\s*.*\n)*?…` 这种正则 —— 在 8000 行的 stream.py 上回溯爆炸，
    #   实测表现为门禁**卡死无输出**（SIGTERM 后才停），看起来像脚本坏了。
    _notry = []
    for _r, _l, _n, _s in _entries:
        _i = _s.find("_assign_load_aware(")
        if _i < 0:
            continue
        if "try:" not in _s[max(0, _i - 600):_i]:
            _notry.append(_r)
    check("[F3] 每处接线都包在 try 里（打分器坏不得让编排失败）", not _notry,
          "未包 try：%s" % "、".join(_notry))
    for _r, _l, _n, _ in _entries:
        print("    %-38s %-28s 接线 %d 处" % (_r, _l, _n))

    # ── [G] 缺列兼容：自建夹具表（没有这两列）不得崩，且必须留 warning ──────────
    #   为什么守这条：本轮加列后，`verify_expected_output_backstop` 立刻从绿变崩
    #   （它自建最小表：`table agent_tasks has no column named assign_score`）。
    #   「加依赖就有兼容成本」是必然的，但**静默退回**会让留痕悄悄失效 ——
    #   所以要求：不崩 + 留 warning（warning 内容指明"只写 config"）。
    import sqlite3 as _sq
    _mini = os.path.join(ROOT, "tmp", "_gate_assign_minimal.db")
    if os.path.exists(_mini):
        os.remove(_mini)
    _mc = _sq.connect(_mini)
    _mc.execute("CREATE TABLE agent_tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INT, "
                "task_key TEXT, title TEXT, agent_id TEXT, agent_name_snapshot TEXT, task_type TEXT, "
                "config TEXT, deps TEXT, status TEXT, assigned_by TEXT, seq INT, context TEXT, "
                "expected_output TEXT, conversation_id INT)")
    _mc.commit()
    _warns = []
    try:
        import logging as _lg

        class _Cap(_lg.Handler):
            def emit(self, rec):
                _warns.append(rec.getMessage())

        _h = _Cap()
        _lg.getLogger("mbse.planner").addHandler(_h)
        try:
            from services.task_queue import TaskQueue as _TQ3
            _TQ3.create_plan(_mc, 1, [{"key": "m1", "title": "最小表落库", "agent": "",
                                       "deps": [],
                                       "config": {"assign_score": 0.9}}], strict=False)
            _crashed = False
            _err = ""
        finally:
            _lg.getLogger("mbse.planner").removeHandler(_h)
    except Exception as e:
        _crashed, _err = True, "%s: %s" % (type(e).__name__, e)
    finally:
        _mc.close()
    check("[G1] 表缺 assign_score/load_snapshot 列时不崩（自建夹具表兼容）", not _crashed, _err)
    check("[G2] 缺列时留 warning 说明「只写 config」（不许静默丢留痕）",
          any("assign_score" in w and "config" in w for w in _warns),
          "warnings=%s" % [w[:60] for w in _warns[:3]])

    # ── 变异自证 ──────────────────────────────────────────────────────
    print("\n=== 变异自证 ===")
    # ⚠️ 锚点选择原则：**短、唯一、语义明确**。
    #   本轮第一版把锚点写成整段 INSERT（列名 + 占位符 + 两行值参数），
    #   结果我把实现改成「有列 / 缺列」两分支后锚点全部失效 ⇒ 自证中止。
    #   现在三个变异各打一个**单点**：
    #     M1 强制走缺列分支（语义等价于"没加这两列"）
    #     M2 列值写死 0.0（"看起来写了"）
    #     M3 config 落库改回 json.dumps(原值)（二次编码）
    _m1_anchor = "            if _has_trace_cols:"
    _m2_anchor = ("                        (float(_col_score) "
                  "if isinstance(_col_score, (int, float)) else 0.0),")
    _m3_anchor = 't.get("task_type", "agent"), json.dumps(_cfg_for_cols, ensure_ascii=False),'
    _miss = [n for n, a in (("M1", _m1_anchor), ("M2", _m2_anchor), ("M3", _m3_anchor))
             if a not in src]
    if _miss:
        check("M 变异锚点存在于源码（自证前提）", False,
              "锚点缺失 %s ⇒ 自证中止（不静默跳过）" % _miss)
    else:
        def _mut_run(label, mut_src, judge_keys, desc):
            """exec 变异源码到独立命名空间 → 落库 → 看对应判据是否翻红。"""
            ns = {"__name__": "_mut_task_queue"}
            try:
                exec(compile(mut_src, "<mutant>", "exec"), ns)
            except Exception as e:
                check("M%s 变异体可执行" % label, False, "%s: %s" % (type(e).__name__, e))
                return
            try:
                mrows = run_case(ns["TaskQueue"])
            except Exception as e:
                check("M%s 变异体可落库" % label, False, "%s: %s" % (type(e).__name__, e))
                return
            mv = verdicts(mrows)
            red = [k for k in judge_keys if not mv[k]]
            print("  M%s 变异体：assign_score=%s"
                  % (label, (mrows.get("t1") or {}).get("assign_score")))
            check("M%s %s ⇒ 判据 %s 必须翻红" % (label, desc, "/".join(judge_keys)),
                  bool(red), "变异体下 %s 仍全绿 ⇒ 判据抓不住这个变异" % "/".join(judge_keys))
            if red:
                _muts.append(("M%s" % label, desc))

        _mut_run("1", src.replace(_m1_anchor, "            if False:  # MUTATION: 回到没加列", 1),
                 ["A"], "强制走缺列分支（列不写，回到缺陷形态）")
        _mut_run("2", src.replace(_m2_anchor, "                        0.0,  # MUTATION: 列写死 0 分", 1),
                 ["A"], "列值写死 0.0（假留痕）")
        _mut_run("3", src.replace(_m3_anchor,
                                  't.get("task_type", "agent"), json.dumps(t.get("config") or {}, ensure_ascii=False),', 1),
                 ["D"], "config 改回 json.dumps(原值) ⇒ 字符串入参二次编码")

    print("\n" + "=" * 74)
    print("常态断言：%d 条，%d 通过 / %d 失败" % (_ok + _fail, _ok, _fail))
    print("变异自证：%d 组" % len(_muts))
    for mid, desc in _muts:
        print("  - %s %s" % (mid, desc))
    print("结论：%s" % ("门禁通过" if _fail == 0 and _muts else "门禁未通过"))
    return 0 if (_fail == 0 and _muts) else 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
