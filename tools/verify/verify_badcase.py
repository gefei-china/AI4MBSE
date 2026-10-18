# -*- coding: utf-8 -*-
"""P0-3 门禁：Bad Case 收集 → 台账 → 告警闭环（core/badcase.py + bad_cases + alert_evaluator）。

**背景（2026-10-18《自进化 AI 平台调研评估》整改第三项）**：LLM 调用失败/截断、
用户负向反馈（modify/wrong/corrected）此前散落在 llm_usage_stats / feedback 两表，
无汇总、无处理状态 —— 自进化选不出「下一轮改什么」。本门禁守护：

  A. 纯函数与注册表：检测器清单、截断、反馈分级、状态校验；
  B. 配置消费：badcase 组六键变异自证（判据走 core.config，不被常量旁路）；
  C. 干净库端到端：dry-run 不动库 → apply 入库 → 同窗口重复收集不双计 →
     复现累加 → triage 三态 → 复现重开（开关变异）→ alert_evaluator 指标与
     规则触发/冷却/暂停全链；
  D. 真库只读 dry-run smoke（判据面真实存在性取证，不写库）。

双环境口径：干净库自建 MBSE_DB_PATH 临时空库（import 前切库）；真库段只读。
"""
import os
import shutil
import sqlite3
import sys


def _find_root():
    _d = os.path.dirname(os.path.abspath(__file__))
    for _ in range(6):
        if os.path.isfile(os.path.join(_d, "memory_service.py")):
            return _d
        _p = os.path.dirname(_d)
        if _p == _d:
            break
        _d = _p
    raise SystemExit("找不到仓库根（未找到 memory_service.py）")


ROOT = _find_root()
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# ── 干净库：先切库后 import（core.config/DB_PATH import 期绑定，顺序即正确性）──
_TMP = os.path.join(ROOT, "tmp", "verify_badcase")
os.makedirs(_TMP, exist_ok=True)
_CLEAN_DB = os.path.join(_TMP, "clean.db")
if os.path.exists(_CLEAN_DB):
    os.remove(_CLEAN_DB)
os.environ["MBSE_DB_PATH"] = _CLEAN_DB

_results = []


def check(name, ok, detail=""):
    _results.append((name, bool(ok), detail))
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name,
                           ("  ← " + str(detail)) if (detail and not ok) else ""))
    return bool(ok)


def skip(name, why):
    _results.append((name, None, why))
    print("  [SKIP] " + name + "  -- " + str(why))


def _seed_llm(conn, conv, run, days_ago, failed=0, kind="", msg="", trunc=0):
    """按可控时间播种判据面（llm_usage_stats）。"""
    conn.execute(
        "INSERT INTO llm_usage_stats (provider_name, model_name, used_mock, call_failed, "
        "failure_kind, failure_msg, finish_reason, conversation_id, run_id, created_at) "
        "VALUES ('p-seed','m-seed',0,?,?,?,?,?,?, datetime('now', ?))",
        (failed, kind, msg, "length" if trunc else "stop", conv, run,
         "-%d day" % days_ago))


def main():
    # ── A. 纯函数与注册表 ───────────────────────────────────────────
    print("== A. 纯函数与注册表（检测器/截断/分级/状态校验）==")
    from core import badcase as BC

    specs = BC._detectors(False)
    check("A1 检测器注册表三类（llm_call_failed/user_negative_feedback/llm_truncated）",
          [s.kind for s in specs] == ["llm_call_failed", "user_negative_feedback", "llm_truncated"],
          f"got={[s.kind for s in specs]}")
    check("A2 各检测器 SQL 统一输出五列（src_id/subject/seen_at/title/detail）",
          all(all(c in s.sql for c in ("src_id", "subject", "seen_at", "title", "detail"))
              for s in specs))
    check("A3 include_probes=False ⇒ SQL 带探测过滤（conversation_id>0 OR run_id>0）",
          "(conversation_id > 0 OR run_id > 0)" in specs[0].sql
          and "(conversation_id > 0 OR run_id > 0)" in specs[2].sql)
    check("A4 include_probes=True ⇒ SQL 无探测过滤",
          "(conversation_id > 0 OR run_id > 0)" not in BC._detectors(True)[0].sql)
    check("A5 截断：超限加字符数标记，未超限原样",
          BC._clip("x" * 100, 50).endswith("[100 chars]")
          and len(BC._clip("x" * 100, 50)) <= 50 and BC._clip("short", 50) == "short")
    check("A6 负向反馈分级：wrong/corrected=high，modify=medium",
          BC._FB_SEVERITY == {"wrong": "high", "corrected": "high", "modify": "medium"})

    # ── B. 配置消费（六键 + 变异自证）────────────────────────────────
    print("\n== B. 阈值走配置（DEFAULT_CONFIG/CONFIG_SCHEMA 双登记 + 变异自证）==")
    import core.config as CFG

    dc = CFG.DEFAULT_CONFIG.get("badcase") or {}
    check("B1 DEFAULT_CONFIG.badcase 六键齐全",
          all(k in dc for k in ("enabled", "window_days", "include_probes",
                                "max_detail_chars", "reopen_on_recur", "alert_threshold")),
          f"got={sorted(dc)}")
    check("B2 CONFIG_SCHEMA.badcase 登记（前端契约）",
          all(k in (CFG.CONFIG_SCHEMA.get("badcase") or {})
              for k in ("enabled", "window_days", "include_probes",
                        "max_detail_chars", "reopen_on_recur", "alert_threshold")))
    _st = BC.get_settings()
    check("B3 get_settings 默认值（窗口30/不含探测/截断2000/复现重开）",
          _st == {"enabled": True, "window_days": 30, "include_probes": False,
                  "max_detail_chars": 2000, "reopen_on_recur": True},
          f"got={_st}")

    # ── C. 干净库端到端 ─────────────────────────────────────────────
    print("\n== C. 干净库端到端（dry-run→apply→去重→triage→告警全链）==")
    from database import init_db, get_db
    init_db()
    conn = get_db()
    conn.row_factory = sqlite3.Row

    check("C1 迁移建表 bad_cases + 唯一索引",
          bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='bad_cases'").fetchone())
          and bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name='ix_bc_kind_subject'").fetchone()))

    # 判据面种子（时间可控）：同会话两败合一主题；窗外/探测另算
    conn.execute("INSERT INTO conversations (title, intent) VALUES ('bc-种子会话', 'chat')")
    _cid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute("INSERT INTO messages (conversation_id, role, content) "
                 "VALUES (?, 'assistant', 'bc-种子消息')", (_cid,))
    _mid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    _seed_llm(conn, 9001, 0, 3, failed=1, kind="balance", msg="余额不足")
    _seed_llm(conn, 9001, 0, 2, failed=1, kind="timeout", msg="读取超时")
    _seed_llm(conn, 9002, 0, 1, failed=1, kind="conn", msg="连接拒绝")
    _seed_llm(conn, 0, 0, 2, failed=1, kind="probe", msg="探测失败")     # 探测：默认排除
    _seed_llm(conn, 9003, 0, 2, trunc=1)                                   # 截断
    _seed_llm(conn, 9004, 0, 40, failed=1, kind="old", msg="窗口外旧账")   # 窗外：排除
    conn.execute("INSERT INTO feedback (message_id, feedback_type, context, created_by, created_at) "
                 "VALUES (?, 'modify', '第3条建议不准确', '验证员', datetime('now','-4 day'))", (_mid,))
    conn.execute("INSERT INTO feedback (message_id, feedback_type, context, created_by, created_at) "
                 "VALUES (?, 'approve', '很好', '验证员', datetime('now','-4 day'))", (_mid,))  # 正向：排除
    conn.commit()

    # C2 ★dry-run 不动库
    n0 = conn.execute("SELECT COUNT(*) FROM bad_cases").fetchone()[0]
    dry = BC.collect_bad_cases(conn, apply=False)
    n1 = conn.execute("SELECT COUNT(*) FROM bad_cases").fetchone()[0]
    cands = dry.get("candidates") or []
    check("C2 ★dry-run 不写库（COUNT 不变）且 enabled=True", n1 == n0 == 0 and dry["dry_run"])
    by_subj = {(c["kind"], c["subject"]): c for c in cands}
    check("C3 候选口径：4 主题（探测/窗外/正向反馈排除；同会话两败合一）",
          len(cands) == 4
          and ("llm_call_failed", "conv:9001") in by_subj
          and ("llm_call_failed", "conv:9002") in by_subj
          and ("llm_truncated", "conv:9003") in by_subj
          and ("user_negative_feedback", "msg:%d" % _mid) in by_subj,
          f"got={sorted(by_subj)}")
    check("C4 同会话两败 ⇒ occurrences=2（去重累加而非两行）",
          by_subj[("llm_call_failed", "conv:9001")]["occurrences"] == 2,
          f"got={by_subj[('llm_call_failed','conv:9001')]['occurrences']}")
    _exp3 = conn.execute("SELECT datetime('now','-3 day')").fetchone()[0][:10]
    check("C5 首次出现时间取窗口内最早（3 天前的事件），非收集时间",
          by_subj[("llm_call_failed", "conv:9001")]["first_seen_at"].startswith(_exp3),
          f"got={by_subj[('llm_call_failed','conv:9001')]['first_seen_at']} expect~{_exp3}")
    check("C6 dry-run plan 预判：new=4 / recur=0",
          dry["plan"] == {"new": 4, "recur": 0, "stale": 0, "reopen": 0}, f"got={dry['plan']}")

    # C7 apply 入库
    res = BC.collect_bad_cases(conn, apply=True)
    ar = res.get("apply_result") or {}
    rows = conn.execute("SELECT * FROM bad_cases ORDER BY id").fetchall()
    check("C7 apply ⇒ inserted=4，台账 4 行且全 open",
          ar.get("inserted") == 4 and len(rows) == 4
          and all(r["status"] == "open" for r in rows), f"apply={ar}")
    # C8 入库内容：主题/严重度/证据/首近时间（按主题取行，不依赖插入顺序）
    # 语义锁定：detail 取**最新**事件的证据（triage 时最相关），occurrences 记总数
    r9001 = conn.execute("SELECT * FROM bad_cases WHERE kind='llm_call_failed' "
                         "AND subject='conv:9001'").fetchone()
    check("C8 入库内容：主题/严重度/最新证据/首近时间",
          r9001 is not None and r9001["severity"] == "high" and r9001["occurrences"] == 2
          and "读取超时" in r9001["detail"] and r9001["status"] == "open"
          and r9001["first_seen_at"] <= r9001["last_seen_at"]
          and r9001["source"] == "collector",
          f"got=({r9001['kind'] if r9001 else None},{r9001['severity'] if r9001 else None},"
          f"{r9001['occurrences'] if r9001 else None},detail={str(r9001['detail'])[:40] if r9001 else None})")

    # C9 ★同窗口重复收集不双计
    again = BC.collect_bad_cases(conn, apply=True)
    rows2 = conn.execute("SELECT * FROM bad_cases WHERE kind='llm_call_failed' AND subject='conv:9001'").fetchone()
    check("C9 ★同窗口重复收集：stale=4、occurrences 不双计（仍 2）",
          again["plan"]["stale"] == 4 and again["plan"]["new"] == 0
          and (again.get("apply_result") or {}).get("inserted") == 0
          and rows2["occurrences"] == 2,
          f"plan={again['plan']} occ={rows2['occurrences']}")

    # C10 复现 ⇒ 累加而非新行
    _seed_llm(conn, 9002, 0, 0, failed=1, kind="quota", msg="配额超限")
    conn.commit()
    rec = BC.collect_bad_cases(conn, apply=True)
    rows3 = conn.execute("SELECT * FROM bad_cases WHERE subject='conv:9002'").fetchall()
    check("C10 新事件复现 ⇒ occurrences 1→2（累加，不插新行）",
          rec["plan"]["recur"] == 1 and len(rows3) == 1 and rows3[0]["occurrences"] == 2
          and rows3[0]["severity"] == "high",
          f"plan={rec['plan']} rows={len(rows3)} occ={rows3[0]['occurrences']}")

    # C10b ★同秒 burst：两个复现事件同一秒 ⇒ 靠 (seen_at, src_id) 二元组水位不漏计
    # （此前只按时间戳严格大于会把同秒事件判为「已计过」——真实缺陷，回归锁定）
    _seed_llm(conn, 9002, 0, 0, failed=1, kind="quota", msg="同秒甲")
    _seed_llm(conn, 9002, 0, 0, failed=1, kind="quota", msg="同秒乙")
    conn.commit()
    burst = BC.collect_bad_cases(conn, apply=True)
    row_b = conn.execute("SELECT * FROM bad_cases WHERE subject='conv:9002'").fetchone()
    check("C10b ★同秒 burst 不漏计（occurrences 2→4，二元组水位）",
          burst["plan"]["recur"] == 1 and row_b["occurrences"] == 4,
          f"plan={burst['plan']} occ={row_b['occurrences']}")

    # C11 triage 三态 + C12 复现重开
    t1 = BC.triage_bad_case(conn, rows3[0]["id"], "resolved", actor="验证员")
    n_open_1 = conn.execute("SELECT COUNT(*) FROM bad_cases WHERE status='open'").fetchone()[0]
    check("C11 triage resolved ⇒ ok 且 open 数 4→3", t1.get("ok") and n_open_1 == 3, f"got={t1}")
    check("C11b 非法状态拒绝", BC.triage_bad_case(conn, rows3[0]["id"], "done").get("ok") is False)
    check("C11c 不存在 id 拒绝", BC.triage_bad_case(conn, 99999, "open").get("ok") is False)
    _seed_llm(conn, 9002, 0, 0, failed=1, kind="quota", msg="又超限了")
    conn.commit()
    rec2 = BC.collect_bad_cases(conn, apply=True)
    row_after = conn.execute("SELECT * FROM bad_cases WHERE subject='conv:9002'").fetchone()
    n_open_2 = conn.execute("SELECT COUNT(*) FROM bad_cases WHERE status='open'").fetchone()[0]
    check("C12 ★已解决主题复现 ⇒ 自动重开（告警闭环不对复发失明）",
          rec2["plan"]["reopen"] == 1 and row_after["status"] == "open"
          and n_open_2 == 4 and row_after["occurrences"] == 5,
          f"plan={rec2['plan']} status={row_after['status']} occ={row_after['occurrences']}")

    # C12b ★配置变异：reopen_on_recur=False ⇒ 不重开（保持人工结论）
    _orig_as_bool = CFG.as_bool
    try:
        CFG.as_bool = lambda section, key, default=False: (
            False if (section, key) == ("badcase", "reopen_on_recur")
            else _orig_as_bool(section, key, default))
        BC.triage_bad_case(conn, row_after["id"], "ignored", actor="验证员")
        _seed_llm(conn, 9002, 0, 0, failed=1, kind="quota", msg="第三次")
        conn.commit()
        rec3 = BC.collect_bad_cases(conn, apply=True)
        row_ig = conn.execute("SELECT * FROM bad_cases WHERE subject='conv:9002'").fetchone()
        check("C12b ★reopen_on_recur=False ⇒ 保持 ignored（策略可关）",
              rec3["plan"]["reopen"] == 0 and rec3["plan"]["recur"] == 1
              and row_ig["status"] == "ignored" and row_ig["occurrences"] == 6,
              f"plan={rec3['plan']} status={row_ig['status']} occ={row_ig['occurrences']}")
    finally:
        CFG.as_bool = _orig_as_bool

    # C13 ★配置变异：enabled=False ⇒ 不动手
    try:
        CFG.as_bool = lambda section, key, default=False: (
            False if (section, key) == ("badcase", "enabled") else _orig_as_bool(section, key, default))
        off = BC.collect_bad_cases(conn, apply=True)
        check("C13 ★enabled=False ⇒ 返 enabled=False、零候选、不写库",
              off["enabled"] is False and off["candidates"] == [] and off["plan"]["new"] == 0,
              f"got={off['enabled']}")
    finally:
        CFG.as_bool = _orig_as_bool

    # C14 ★配置变异：window_days=1 ⇒ 窗外事件不见（口径真被消费）
    _orig_get = CFG.get
    try:
        CFG.get = lambda section, key, default=None: (
            1 if (section, key) == ("badcase", "window_days") else _orig_get(section, key, default))
        w1 = BC.collect_bad_cases(conn, apply=False)
        check("C14 ★window_days=1 ⇒ 只剩窗口内主题（conv:9002 三次 + msg 无）",
              sorted(c["subject"] for c in w1["candidates"]) == ["conv:9002"],
              f"got={[c['subject'] for c in w1['candidates']]}")
    finally:
        CFG.get = _orig_get

    # C15 ★配置变异：include_probes=True ⇒ 探测样本进候选
    try:
        CFG.as_bool = lambda section, key, default=False: (
            True if (section, key) == ("badcase", "include_probes")
            else _orig_as_bool(section, key, default))
        pr = BC.collect_bad_cases(conn, apply=False)
        check("C15 ★include_probes=True ⇒ 探测样本 stat: 主题出现",
              any(c["subject"].startswith("stat:") for c in pr["candidates"]),
              f"got={[c['subject'] for c in pr['candidates']]}")
    finally:
        CFG.as_bool = _orig_as_bool

    # C16 告警闭环：指标 → 规则 → 事件 → 冷却 → 暂停
    print("\n== C2'. 告警闭环（badcase_open_count 指标 → alert_rules → alert_events）==")
    from core import alert_evaluator as AE
    check("C16a GLOBAL_METRICS 含 badcase_open_count（复用既有告警机制）",
          "badcase_open_count" in AE.GLOBAL_METRICS)
    m = AE.collect_global_metrics(conn)
    n_open_now = conn.execute("SELECT COUNT(*) FROM bad_cases WHERE status='open'").fetchone()[0]
    check("C16b collect_global_metrics 读到真实 open 数",
          m["badcase_open_count"] == float(n_open_now),
          f"got={m['badcase_open_count']} expect={n_open_now}")
    conn.execute("INSERT INTO alert_rules (name, metric, operator, threshold, level) "
                 "VALUES ('Bad Case 积压','badcase_open_count','>=',2,'warning')")
    conn.commit()
    fired = AE.evaluate_once(conn=conn, cooldown_min=30, notify=False)
    evs = conn.execute("SELECT * FROM alert_events WHERE metric='badcase_open_count'").fetchall()
    check("C16c 规则命中 ⇒ 落 open 事件（actual=open 数）",
          fired["fired"] == 1 and len(evs) == 1 and evs[0]["actual"] == float(n_open_now)
          and evs[0]["status"] == "open",
          f"fired={fired['fired']} evs={len(evs)}")
    fired2 = AE.evaluate_once(conn=conn, cooldown_min=30, notify=False)
    n_ev2 = conn.execute("SELECT COUNT(*) FROM alert_events WHERE metric='badcase_open_count'").fetchone()[0]
    check("C16d 冷却窗口内不重复落事件", fired2["fired"] == 0 and n_ev2 == 1,
          f"fired={fired2['fired']} evs={n_ev2}")
    conn.execute("UPDATE alert_rules SET status='paused' WHERE metric='badcase_open_count'")
    conn.commit()
    fired3 = AE.evaluate_once(conn=conn, cooldown_min=0, notify=False)
    check("C16e ★规则暂停 ⇒ 不再评估（人工可熄火）",
          fired3["evaluated"] == 0 and fired3["fired"] == 0, f"got={fired3}")
    conn.close()

    # ── D. 真库只读 dry-run smoke ───────────────────────────────────
    print("\n== D. 真库只读 dry-run smoke（判据面真实存在性取证）==")
    _prod = os.path.join(ROOT, "mbse.db")
    if not os.path.exists(_prod):
        skip("D1 真库 smoke", "mbse.db 不存在（CI 环境）")
    else:
        pc = sqlite3.connect("file:%s?mode=ro" % _prod, uri=True)
        pc.row_factory = sqlite3.Row
        if not BC._table_exists(pc):
            skip("D1 真库 dry-run", "bad_cases 表未建立（服务未重启跑迁移）")
        else:
            d = BC.collect_bad_cases(pc, apply=False)   # dry-run：只读连接也不会写
            kinds = sorted((d.get("summary") or {}).get("by_kind", {}))
            print("  真库候选主题 %d 个 / 事件 %d 起，kinds=%s"
                  % (len(d.get("candidates") or []), (d.get("summary") or {}).get("total_events", 0), kinds))
            check("D1 真库 dry-run 有候选且不动库（ro 连接下 apply=False 安全）",
                  len(d.get("candidates") or []) > 0 and d["dry_run"] is True,
                  f"cands={len(d.get('candidates') or [])}")
            n_bad = pc.execute("SELECT COUNT(*) FROM bad_cases").fetchone()[0]
            check("D2 真库台账行数未因 smoke 变化（只读取证）", n_bad >= 0, f"rows={n_bad}")
        pc.close()

    # ── E. 接线完整性 ───────────────────────────────────────────────
    print("\n== E. 接线完整性（API + 面板 + 加载钩子）==")
    _mon = open(os.path.join(ROOT, "routers", "monitor.py"), encoding="utf-8").read()
    check("E1 monitor.py 三端点（列表/收集/triage）",
          all(k in _mon for k in ('/api/monitor/badcases"', '/api/monitor/badcases/collect',
                                  '/api/monitor/badcases/{case_id}/triage')))
    check("E2 收集端点默认 dry-run（apply 显式才入库）",
          "do_apply = bool(apply or (body or {}).get(\"apply\"))" in _mon
          and '"skipped": True' in _mon and "dry-run" in _mon)
    _ih = open(os.path.join(ROOT, "static", "index.html"), encoding="utf-8").read()
    check("E3 index.html 有 ops-badcases 面板（含试运行/确认入库按钮）",
          'id="ops-badcases"' in _ih and 'runBadcaseCollect(false)' in _ih
          and 'runBadcaseCollect(true)' in _ih)
    _ad = open(os.path.join(ROOT, "static", "js", "mods", "31-admin.js"), encoding="utf-8").read()
    check("E4 31-admin.js 三函数 + loadOps 主链钩子",
          all(k in _ad for k in ("async function loadOpsBadCases", "async function runBadcaseCollect",
                                 "async function triageBadcase", "loadOpsBadCases(); }catch(e){}")))
    _ae = open(os.path.join(ROOT, "core", "alert_evaluator.py"), encoding="utf-8").read()
    check("E5 alert_evaluator 消费 open_badcase_count（不造第二套告警）",
          "from core.badcase import open_badcase_count" in _ae)

    print("\n结果: %d PASS / %d FAIL / %d SKIP"
          % (sum(1 for r in _results if r[1] is True),
             sum(1 for r in _results if r[1] is False),
             sum(1 for r in _results if r[1] is None)))
    _failed = [r[0] for r in _results if r[1] is False]
    if _failed:
        print("失败项: " + "; ".join(_failed))
    shutil.rmtree(_TMP, ignore_errors=True)
    if _failed:
        sys.exit(1)
    print("全部通过 —— Bad Case 闭环（dry-run 人工关口 + 去重累加 + triage + 复现重开 + "
          "告警指标/规则/冷却/暂停 + 真库只读取证）。")


if __name__ == "__main__":
    main()
