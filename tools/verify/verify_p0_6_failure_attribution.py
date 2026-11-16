"""P0-6 常驻门禁：失败归因全覆盖 + 子任务必含规则 + 交接厚度口径勘误。

判「门有没有牙齿」只看这一节：
  [S] 静态：4 个终态写入点都写 error_class、3 个重排队出口都清旧标签、
      必含规则接进 build_subtask_context 且零围栏、老库降级分支仍在。
  [B] 行为：临时库夹具跑 cancel / cancel_run / fail / retry，含**老库无该列**降级；
      「改动前孪生体」证明 include_rules=False 时输出逐字节相同。
  [D] 真库：失败/取消任务 error_class 非空率 ≥95%，取值都在 ERROR_CLASSES 内，
      cancelled 一律 interrupted；留痕文件逐条可回滚。
  [G] Golden Set v0 12 例与必含规则逐例对齐（生产真源 ← 评测集，数据集不改）。
  [T] 交接厚度口径：量的是落库列 agent_tasks.context，运行时快照是另一份文本
      —— 方案里「交接极薄」是**指标错配**，本组把两份口径都量出来防止再误读。
  [M] 变异自证：每处判据都做「撤回旧写法 ⇒ 必须判红」，防空转/假绿。

跑法：./.venv/Scripts/python.exe -X utf8 tools/verify/verify_p0_6_failure_attribution.py
口径：2026-11-15 P0-6；真库只读，夹具全在内存/临时目录；不调 LLM。
"""
import ast
import inspect
import json
import os
import sqlite3
import sys
import tempfile
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

import services.failure_class as FC  # noqa: E402
import core.subtask_rules as SR  # noqa: E402
import workflows.planner as P  # noqa: E402
from task_queue import TaskQueue  # noqa: E402

PASS, FAIL = [], []


def ck(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(("PASS " if ok else "FAIL ") + name + ((" | " + str(detail)) if detail else ""))


def read(rel):
    return (REPO / rel).read_text(encoding="utf-8", errors="replace")


SOURCES = {
    "tq": read("task_queue.py"),
    "oi": read("agent/orch_interrupt.py"),
    "ro": read("routers/orchestration.py"),
    "oc": read("agent/orch_checkpoint.py"),
    "rr": read("core/run_registry.py"),
    "pl": read("workflows/planner.py"),
    "fc": read("services/failure_class.py"),
}

# ── [S] 静态纯判据：输入源码字典，返回问题清单（空 = 通过） ──────────────────
def static_problems(s):
    out = []
    # S1 单一真源：classify_terminal 存在，且 cancelled → interrupted / failed → classify
    if "def classify_terminal(" not in s["fc"]:
        out.append("S1 缺 classify_terminal")
    if 'st in ("cancelled", "canceled")' not in s["fc"] or 'return "interrupted"' not in s["fc"]:
        out.append("S1 cancelled 未映射 interrupted")
    if "return classify(error_text, ok=False)" not in s["fc"]:
        out.append("S1 failed 未委托 classify()")
    # S2 四个终态写入点（含既有 fail / reap_orphans）都必须写 error_class
    for key, label in (("tq", "TaskQueue"), ("oi", "orch_interrupt"),
                       ("rr", "run_registry")):
        if "error_class=?" not in s[key]:
            out.append("S2 %s 没有写 error_class 的 UPDATE" % label)
    if s["tq"].count("SET status='cancelled', error=?, error_class=?") != 2:
        out.append("S2 cancel / cancel_run 两处主路径不齐")
    # S3 三个重排队出口都清旧标签
    for key, label in (("tq", "retry"), ("ro", "rerun"), ("oc", "apply_resume")):
        if "error_class=''" not in s[key]:
            out.append("S3 %s 重排队未清 error_class" % label)
    # S4 必含规则接进运行时快照（单一真源 + 默认开 + 可关）
    if "include_rules: bool = True" not in s["pl"]:
        out.append("S4 build_subtask_context 未默认开启必含规则")
    if "from core.subtask_rules import render_rules" not in s["pl"]:
        out.append("S4 未引用 core.subtask_rules 单一真源")
    if "if include_rules:" not in s["pl"]:
        out.append("S4 缺可关闭开关")
    # S5 规则块零围栏、不含 kind 枚举
    if "```" in SR.render_rules():
        out.append("S5 规则块含围栏（会破坏快照围栏奇偶门禁）")
    # S6 老库降级：cancel / cancel_run / orch_interrupt 各有 2 条 cancelled UPDATE
    if s["tq"].count("SET status='cancelled', error=?,") < 4:
        out.append("S6 cancel / cancel_run 老库降级分支缺失")
    if s["oi"].count("SET status='cancelled', error=?,") != 2:
        out.append("S6 orch_interrupt 老库降级分支缺失")
    if "极老库可能尚无 error_class 列" not in s["oi"]:
        out.append("S6 orch_interrupt 降级注释缺失")
    return out


def main():
    src_bsc = textwrap.dedent(inspect.getsource(P.build_subtask_context))

    print("\n=== [S] 静态判据 ===")
    sp = static_problems(SOURCES)
    ck("S1~S6 静态判据全过（写入点/清空/规则/降级）", not sp, sp)

    print("\n=== [B] 行为夹具 ===")

    def b1_ok(fn):
        """纯判据：供 B1 基线与其变异 M6 共用（变异必须绑到同一条判据上）。"""
        return (fn("done", "x") == ""
                and fn("blocked", "x") == ""
                and fn("ready", "x") == ""
                and fn("cancelled", "用户取消") == "interrupted"
                and fn("canceled", "用户取消") == "interrupted"
                and fn("failed", "timeout") == "timeout"
                and fn("failed", "孤儿运行") == "interrupted")

    ck("B1 classify_terminal(done/failed/cancelled/blocked/ready) 五档",
       b1_ok(FC.classify_terminal))

    def fixture(with_col=True):
        c = sqlite3.connect(":memory:")
        # ⚠️ 夹具必须带 updated_at：生产所有终态 UPDATE 都写 CURRENT_TIMESTAMP，
        #   少了它主路径会抛 no such column 并静默走降级 ⇒ 断言全部假绿（本次实测踩过）。
        cols = ("id INTEGER PRIMARY KEY, run_id INTEGER, status TEXT DEFAULT 'planned', "
                "result TEXT DEFAULT '', error TEXT DEFAULT '', updated_at TEXT DEFAULT ''")
        if with_col:
            cols += ", error_class TEXT DEFAULT ''"
        c.execute("CREATE TABLE agent_tasks (%s)" % cols)
        if with_col:
            c.execute("INSERT INTO agent_tasks (id,run_id,status,error_class) VALUES (1,7,'ready','old_label')")
        else:
            c.executemany("INSERT INTO agent_tasks (id,run_id,status) VALUES (?,?,?)",
                          [(1, 7, "ready"), (2, 7, "ready")])
        c.commit()
        return c

    c = fixture()
    TaskQueue.cancel(c, 1, "用户点击停止")
    r1 = c.execute("SELECT status,error,error_class FROM agent_tasks WHERE id=1").fetchone()
    ck("B2 cancel ⇒ cancelled + interrupted", r1 == ("cancelled", "用户点击停止", "interrupted"), r1)
    again = TaskQueue.cancel(c, 1, "再来一次")
    r1b = c.execute("SELECT status,error FROM agent_tasks WHERE id=1").fetchone()
    ck("B3 cancel 幂等（二次返回 False 且不覆盖 error）",
       again is False and r1b == ("cancelled", "用户点击停止"), (again, r1b))
    c.execute("INSERT INTO agent_tasks (id,run_id,status) VALUES (2,7,'ready')")
    c.commit()
    TaskQueue.fail(c, 2, "timeout（240s 无产出）")
    r2 = c.execute("SELECT status,error_class FROM agent_tasks WHERE id=2").fetchone()
    ck("B4 fail ⇒ failed + timeout", r2 == ("failed", "timeout"), r2)
    TaskQueue.retry(c, 2)
    r2b = c.execute("SELECT status,error,error_class FROM agent_tasks WHERE id=2").fetchone()
    ck("B5 retry 清掉上一轮失败标签", r2b == ("ready", "", ""), r2b)
    c.close()

    c9 = fixture(with_col=True)
    c9.execute("INSERT INTO agent_tasks (id,run_id,status,error_class) VALUES (3,9,'ready','stale')")
    c9.commit()
    TaskQueue.cancel_run(c9, 9, "停滞超过 30 分钟，自动收敛")
    got9 = {r[0]: (r[1], r[2]) for r in c9.execute("SELECT id,status,error_class FROM agent_tasks")}
    ck("B6 cancel_run 整批写 interrupted", got9[3] == ("cancelled", "interrupted"), got9.get(3))
    c9.close()

    c_old = fixture(with_col=False)
    TaskQueue.cancel(c_old, 1, "用户取消")
    TaskQueue.fail(c_old, 2, "timeout")
    old_rows = list(c_old.execute("SELECT id,status,error FROM agent_tasks ORDER BY id"))
    ck("B7 老库无 error_class 列：cancel/fail 不抛且状态与 error 都落库",
       old_rows[0][1] == "cancelled" and "用户取消" in old_rows[0][2]
       and old_rows[1][1] == "failed" and "timeout" in old_rows[1][2], old_rows)
    c_old.close()

    # B8 零回归：exec「改动前孪生体」，断言 include_rules=False 逐字节相同
    old_src = _strip_rules_block(src_bsc).replace(
        ",\n                          include_rules: bool = True", "")
    ns = dict(vars(P))
    exec(compile(old_src, "<pre-p0-6>", "exec"), ns)
    old_fn = ns["build_subtask_context"]
    tk = {"task_key": "t1", "title": "生成 BDD", "expected_output": "SysML v2 代码", "deps": []}
    pre_items = [{"task_key": "t0", "title": "需求分析",
                  "result": "需求 R1：系统应支持 X。" * 30, "summary": ""}]
    for label, kw in (("空上游", {}), ("带上游", {"done_items": pre_items}),
                      ("带产物摘要", {"artifact_digest": "【会话产物】v0.1"})):
        a = P.build_subtask_context(tk, "目标", include_rules=False, **kw)
        b = old_fn(tk, "目标", **kw)
        ck("B8 零回归孪生体逐字节相同（%s）" % label, a == b,
           "len %d vs %d" % (len(a), len(b)))
    with_rules = P.build_subtask_context(tk, "目标")
    ck("B9 默认开启时规则段在快照里且围栏偶数",
       "必含规则" in with_rules and with_rules.count("```") % 2 == 0
       and "必含规则" not in P.build_subtask_context(tk, "目标", include_rules=False))

    print("\n=== [D] 真库事实（只读） ===")
    real = sqlite3.connect("file:%s?mode=ro" % (REPO / "mbse.db").as_posix(), uri=True)
    real.row_factory = sqlite3.Row
    dist = dict(real.execute(
        "SELECT COALESCE(NULLIF(error_class,''),'(空)') k, COUNT(*) n "
        "FROM agent_tasks WHERE status IN ('failed','cancelled') GROUP BY 1"))
    total = sum(dist.values())
    nonempty = total - dist.get("(空)", 0)
    rate = nonempty / total if total else 0.0
    ck("D1 失败/取消任务 error_class 非空率 ≥95%", rate >= 0.95,
       "%d/%d = %.4f 分布=%s" % (nonempty, total, rate, dist))
    ck("D2 非空取值全部落在 ERROR_CLASSES 8 类内",
       all(k in FC.ERROR_CLASSES for k in dist if k != "(空)"), dist)
    bad_cancel = real.execute(
        "SELECT COUNT(*) FROM agent_tasks WHERE status='cancelled' "
        "AND error_class!='interrupted'").fetchone()[0]
    ck("D3 cancelled 一律 interrupted（不是 internal）", bad_cancel == 0,
       "违例=%d" % bad_cancel)
    real.close()

    leave = REPO / "docs" / "评测" / "p0-6-error-class-backfill-20261115.json"
    if leave.is_file():
        lv = json.loads(leave.read_text(encoding="utf-8"))
        ck("D4 回填留痕：method=offline_replay / 93 行 / 逐条回滚 SQL / 口径自洽",
           lv.get("method") == "offline_replay"
           and len(lv.get("rows") or []) == 93
           and len(lv.get("rollback_sql") or []) == 93
           and lv.get("counts", {}).get("empty_after") == 0
           and all(p["derived"] in FC.ERROR_CLASSES for p in lv["rows"]))
    else:
        ck("D4 回填留痕存在", False, str(leave))

    print("\n=== [G] Golden Set 对齐 ===")
    ds = json.loads((REPO / "docs" / "评测" / "datasets" / "golden_e2e_v0.json").read_text(
        encoding="utf-8"))
    cases = ds["cases"]
    case_ids = [x["id"] for x in cases]
    ck("G1 必含规则与 Golden Set %d 例逐例对齐" % len(cases),
       not SR.audit_alignment(case_ids), SR.audit_alignment(case_ids))
    ck("G2 每例 inviolable_rules 非空且数据集指纹未变",
       all(x.get("inviolable_rules") for x in cases)
       and ds.get("dataset_id") == "eb245949e0d84ed1", ds.get("dataset_id"))

    print("\n=== [T] 交接厚度口径勘误 ===")
    real = sqlite3.connect("file:%s?mode=ro" % (REPO / "mbse.db").as_posix(), uri=True)
    col_avg = real.execute("SELECT ROUND(AVG(LENGTH(context)),1) FROM agent_tasks").fetchone()[0]
    sample = real.execute(
        "SELECT context FROM agent_tasks WHERE status='done' ORDER BY LENGTH(context) DESC LIMIT 1"
    ).fetchone()[0]
    real.close()
    # 带一个真实依赖重放：deps 为空时上游交付物段不会进快照，量出来会偏短、反而误导。
    tk_dep = {**tk, "deps": ["t0"]}
    runtime = P.build_subtask_context(tk_dep, "目标", done_items=pre_items,
                                     artifact_digest="【会话产物】v0.1")
    ck("T1 落库列均值仍处在方案记录的量级（<100 字符）",
       float(col_avg) < 100, "实测 %.1f 字符（方案 2026-11-15 记 58.7）" % float(col_avg))
    ck("T2 运行时快照是另一份文本（含任务定义/上游摘要/交付规范/必含规则）",
       all(k in runtime for k in ("任务定义", "上游交付物摘要", "交付规范", "必含规则"))
       and "必含规则" not in (sample or ""),
       "落库列最长=%d 字符 / 含 1 个上游依赖的运行时快照=%d 字符"
       % (len(sample or ""), len(runtime)))
    ck("T3 结论成立：不能用落库列断言「交接极薄」（指标错配）",
       float(col_avg) < 100 and len(runtime) > 3 * float(col_avg),
       "落库列均值 %.1f 字符 vs 快照 %d 字符（%.1f×）"
       % (float(col_avg), len(runtime), len(runtime) / float(col_avg)))

    print("\n=== [M] 变异自证（撤回旧写法 ⇒ 判据必须翻红） ===")
    # M1~M3：逐个摘掉写入点
    ck("M1 变异（cancel 摘掉归因）⇒ 静态判据判红",
       static_problems({**SOURCES, "tq": SOURCES["tq"].replace(
           "SET status='cancelled', error=?, error_class=?",
           "SET status='cancelled', error=?", 1)}))
    ck("M2 变异（cancel_run 摘掉归因）⇒ 静态判据判红",
       static_problems({**SOURCES, "tq": SOURCES["tq"].replace(
           "SET status='cancelled', error=?, error_class=?",
           "SET status='cancelled', error=?", 2)}))
    ck("M3 变异（orch_interrupt 摘掉归因）⇒ 静态判据判红",
       static_problems({**SOURCES, "oi": SOURCES["oi"].replace(
           "SET status='cancelled', error=?, error_class=?",
           "SET status='cancelled', error=?", 1)}))
    ck("M4 变异（retry 不清旧标签）⇒ 静态判据判红",
       static_problems({**SOURCES, "tq": SOURCES["tq"].replace(
           "SET status='ready', error='', error_class=''",
           "SET status='ready', error=''", 1)}))
    ck("M5 变异（去掉规则注入开关）⇒ 静态判据判红",
       static_problems({**SOURCES, "pl": SOURCES["pl"].replace(
           "    if include_rules:", "    if False:", 1)}))
    ct_src = textwrap.dedent(inspect.getsource(FC.classify_terminal))
    ct_mut = ct_src.replace('return "interrupted"', 'return "internal"', 1)
    ck("M6a 变异锚点命中（孪生体确实被改动）", ct_mut != ct_src)
    ns_ct = dict(vars(FC))
    exec(compile(ct_mut, "<mutant-ct>", "exec"), ns_ct)
    ck("M6 变异（classify_terminal 把 cancelled 判成 internal）⇒ B1 判红",
       not b1_ok(ns_ct["classify_terminal"]),
       ns_ct["classify_terminal"]("cancelled", "用户取消"))
    # M7：规则块加围栏 ⇒ audit_alignment 必须报问题
    orig_rules = SR.INVIOLABLE_RULES
    try:
        SR.INVIOLABLE_RULES = orig_rules + ("``` 坏规则",)
        ck("M7 变异（规则块加围栏）⇒ 对齐自校验判红", bool(SR.audit_alignment(case_ids)))
    finally:
        SR.INVIOLABLE_RULES = orig_rules
    # M8：真库非空率判据不空转 —— 造一行空归因，rate 必须掉到 0.5
    m = sqlite3.connect(":memory:")
    m.execute("CREATE TABLE agent_tasks(id INTEGER, status TEXT, error_class TEXT)")
    m.executemany("INSERT INTO agent_tasks VALUES (?,?,?)",
                  [(1, "failed", "timeout"), (2, "cancelled", "")])
    md = dict(m.execute(
        "SELECT COALESCE(NULLIF(error_class,''),'(空)') k, COUNT(*) n "
        "FROM agent_tasks WHERE status IN ('failed','cancelled') GROUP BY 1"))
    mrate = (sum(md.values()) - md.get("(空)", 0)) / sum(md.values())
    m.close()
    ck("M8 变异（真库判据注入一行空归因）⇒ 非空率跌破 95%", mrate < 0.95, mrate)
    # M9：Golden 对齐少登记一例 ⇒ 判红
    ck("M9 变异（对齐表删一例）⇒ 对齐判据判红",
       bool(SR.audit_alignment(case_ids[:-1])))

    print("\nRESULT: %d PASS / %d FAIL" % (len(PASS), len(FAIL)))
    if FAIL:
        print("FAILED:", *FAIL, sep="\n  - ")
    return 1 if FAIL else 0


def _strip_rules_block(src):
    """按行内容定位，删掉 P0-6 新增的 `if include_rules:` 块（缩进无关，§6.10 ④）。"""
    out, skipping = [], False
    for ln in src.splitlines(keepends=True):
        if ln.strip().startswith("if include_rules:"):
            skipping = True
            continue
        if skipping:
            if ln.startswith("    if artifact_digest:"):
                skipping = False
                out.append(ln)
            continue
        out.append(ln)
    return "".join(out)


if __name__ == "__main__":
    sys.exit(main())
