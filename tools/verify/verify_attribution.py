# -*- coding: utf-8 -*-
"""P1-2 门禁：步骤级归因器（core/attribution.py + monitor 接线 + 写入点修复）。

**背景（2026-10-18《自进化 AI 平台调研评估》路线图 P1-2）**：AgentEvolver
三机制里的 Self-Attributing——逐步 GOOD/BAD 归因，过程+结果双通道奖励。本系统
原 error_profile 只到会话级计数，回答不了「哪一步开始坏的」；而步骤级原料
（llm_usage_stats.call_failed/failure_kind、tool_call_logs.ok/failure_kind、
flow_run_steps.status + 结局证据 agent_tasks/orch_checkpoints/flow_runs）早已
全部落库。本门禁守护：

  A. 纯函数与注册表：3 个步骤源统一九列、排序确定性（同秒并列 SOURCE_RANK）、
     首坏步+级联标记、三态 verdict、信号不翻标签、step_key 下钻保留；
  B. 配置消费：attribution 组六键双登记 + 变异自证（窗口/聚合下限/会话内
     截断/总开关四个口径都必须真被消费，不被常量旁路）；
  C. 干净库端到端：混合会话归因、同秒并列、unattributed_failure 不伪装、
     空会话、flow_step 源、窗外证据、patterns 只聚首坏步（级联不放大）、
     kind 覆盖率诚实统计、max_steps 截断事实明示；
  D. 真库只读 smoke：引擎计数与裸 SQL 双通道对账、patterns 聚合自洽、
     以及 tool_call_logs 写入点修复的**行为级证明**（子进程独立库：
     失败调用落非空 kind、成功留空——嵌套 classify bug 不复发）；
  E. 接线断言：monitor 路由注册（运行期）+ config 双登记 + 模块零写语句
     （静态）+ 两个写入点无嵌套 classify。

双环境口径：干净库自建 MBSE_DB_PATH 临时空库（import 前切库）；真库段只读；
D6 子进程另用独立库（database 的 DB_PATH import 期绑定，同进程改不了）。
★D6 夹具建表 DDL 必须覆盖 `_log_tool_call` INSERT 的**全部列**——P0-a S2
给 INSERT 加了 capability_id，夹具漏列即 INSERT 失败、行空、D6 假红
（2026-10-24 实测）。另注意引号双层解析：门禁源码双引号串里的 \'\' 到子进程
只剩 ''，会把子进程的单引号字符串提前截断（SQL 变 `DEFAULT ,`）⇒ 夹具列
一律不写 DEFAULT，值由被测代码显式给。
"""
import os
import shutil
import sqlite3
import sys
import time as _time


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
_TMP = os.path.join(ROOT, "tmp", "verify_attribution")
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


def main():
    from core import attribution as A

    # ── A. 纯函数与注册表 ───────────────────────────────────────────
    print("== A. 纯函数与注册表（3 步骤源 / 排序确定 / 首坏+级联 / 三态 verdict）==")
    names = [s.name for s in A._SPECS]
    check("A1 步骤源注册表 3 个（llm_call / tool_call / flow_step）",
          names == ["llm_call", "tool_call", "flow_step"], f"got={names}")
    unified = all(all(k in s.sql for k in
                      ("step_key", " AS at", " AS ok", " AS kind", " AS actor",
                       " AS intent", " AS run_id", " AS task_key", "meta_json"))
                  for s in A._SPECS)
    check("A2 三源统一输出九列（引擎零特判，新源=登记一条）", unified)
    tie = [{"at": "2026-10-01 10:00:00", "source": "tool_call", "source_id": 5},
           {"at": "2026-10-01 10:00:00", "source": "llm_call", "source_id": 9},
           {"at": "2026-10-01 10:00:00", "source": "flow_step", "source_id": 1},
           {"at": "2026-10-01 09:59:59", "source": "tool_call", "source_id": 2}]
    ordered = A.order_steps(tie)
    check("A3 ★order_steps 确定性：时间→源次序（llm→tool→flow）→id，同秒不平赌",
          [s["source_id"] for s in ordered] == [2, 9, 5, 1]
          and [s["source"] for s in ordered] == ["tool_call", "llm_call",
                                                 "tool_call", "flow_step"],
          f"got={[(s['at'], s['source'], s['source_id']) for s in ordered]}")
    seq_steps = [{"label": "GOOD"}, {"label": "BAD"}, {"label": "GOOD"},
                 {"label": "BAD"}, {"label": "BAD"}]
    out, fb = A.attribute_steps(seq_steps)
    check("A4 首坏步+级联：首坏 is_first_bad、其后 BAD 全 after_first_bad",
          fb == 2 and [s["seq"] for s in out] == [1, 2, 3, 4, 5]
          and out[1]["is_first_bad"] and not out[1]["after_first_bad"]
          and out[3]["after_first_bad"] and not out[3]["is_first_bad"]
          and out[4]["after_first_bad"] and not out[0]["after_first_bad"],
          f"got fb={fb}")
    check("A5 verdict_of 三态：坏步⇒attributed / 只坏结局⇒unattributed_failure / 全好⇒clean",
          A.verdict_of(2, 0) == "attributed"
          and A.verdict_of(0, 3) == "unattributed_failure"
          and A.verdict_of(0, 0) == "clean"
          and set(A.VERDICTS) == {"clean", "attributed", "unattributed_failure"})
    sig = A._marker_signals({"used_mock": 1, "finish_reason": "length",
                             "retry_count": 2, "fallback_used": 1})
    check("A6 ★信号不翻标签：mock/截断/重试/回落是信号不是失败",
          sig == {"mock": True, "truncated": True, "retried": True, "fallback": True},
          f"got={sig}")
    check("A7 新状态词不静默：不在好/坏集的 flow 状态打 unknown_status 信号",
          A._marker_signals({"status": "brand_new_state"}).get("unknown_status") is True
          and A._marker_signals({"status": "done"}).get("unknown_status") is None
          and A._marker_signals({}) == {})
    row = {"step_key": "llm_call:42", "at": "2026-10-01 10:00:00", "ok": 0,
           "kind": " timeout ", "actor": "p/m", "intent": "chat", "run_id": "3",
           "task_key": "tk", "meta_json": '{"a": 1}'}
    s = A._row_to_step(row)
    check("A8 _row_to_step 契约：label 由 ok 推导、kind strip、step_key 保留下钻、"
          "run_id 转 int、meta JSON 解析",
          s["source"] == "llm_call" and s["source_id"] == 42
          and s["step_key"] == "llm_call:42" and s["label"] == "BAD"
          and s["kind"] == "timeout" and s["run_id"] == 3 and s["meta"] == {"a": 1},
          f"got={s}")
    bad = A._row_to_step(dict(row, meta_json="not-json{"))
    check("A9 meta JSON 坏兜底不炸（raw 截断进 meta，步骤仍可用）",
          isinstance(bad["meta"], dict) and "raw" in bad["meta"], f"got={bad['meta']}")
    check("A10 _in_window：空时间戳视为窗外（无法证明新鲜的证据不取）",
          A._in_window("2026-10-01 10:00:00", "2026-10-01 09:00:00")
          and A._in_window("2026-10-01 10:00:00", "2026-10-01 10:00:00")
          and not A._in_window("", "2026-10-01 10:00:00")
          and not A._in_window("2026-10-01 08:00:00", "2026-10-01 10:00:00"))

    # ── B. 配置双登记 + 默认口径 ────────────────────────────────────
    print("\n== B. 配置消费（attribution 组六键双登记 + 默认口径）==")
    import core.config as CFG
    dc = CFG.DEFAULT_CONFIG.get("attribution") or {}
    keys6 = ("enabled", "window_days", "min_occurrences", "max_patterns",
             "max_steps", "session_scan_limit")
    check("B1 DEFAULT_CONFIG.attribution 六键齐全", all(k in dc for k in keys6),
          f"got={sorted(dc)}")
    sch = CFG.CONFIG_SCHEMA.get("attribution") or {}
    check("B2 CONFIG_SCHEMA.attribution 登记（前端契约，类型齐全）",
          all(k in sch for k in keys6)
          and all(sch[k]["type"] == "int" for k in keys6 if k != "enabled")
          and sch["enabled"]["type"] == "bool",
          f"got={sorted(sch)}")
    st = A.get_settings()
    check("B3 get_settings 默认口径（90 天 / 首坏 2 现 / 50 模式 / 500 步 / 200 会话）",
          st == {"enabled": True, "window_days": 90, "min_occurrences": 2,
                 "max_patterns": 50, "max_steps": 500, "session_scan_limit": 200},
          f"got={st}")

    # ── C. 干净库端到端 ─────────────────────────────────────────────
    print("\n== C. 干净库端到端（混合归因 / 并列 / 缺口 / 聚合只聚首坏 / 截断）==")
    from database import init_db, get_db
    init_db()
    conn = get_db()
    conn.row_factory = sqlite3.Row
    marks = {}

    def ts(off):
        # 窗内基串：本地时区 now-2day+off（引擎 cutoff 同为本地时区，口径一致；
        # -2 天间距对任意时区都确定性地落在 90 天窗口内、掉出 M1 的 1 天窗口）
        return _time.strftime("%Y-%m-%d %H:%M:%S",
                              _time.localtime(_time.time() - 2 * 86400 + off))

    def llm(cid, ok, kind, at, intent="chat", prov="p1", model="m1",
            mock=0, fr="stop", rc=0, fbu=0):
        conn.execute(
            "INSERT INTO llm_usage_stats (provider_name, model_name, intent, used_mock, "
            "finish_reason, retry_count, fallback_used, conversation_id, run_id, created_at, "
            "call_failed, failure_kind, failure_msg) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (prov, model, intent, mock, fr, rc, fbu, cid, 0, at,
             0 if ok else 1, kind, "" if ok else ("boom:" + kind)))

    def tool(cid, name, ok, at, kind="", intent="chat", tt="builtin"):
        conn.execute(
            "INSERT INTO tool_call_logs (intent, tool_name, tool_type, call_kind, arguments, "
            "result, ok, latency_ms, conversation_id, run_id, task_key, failure_kind, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (intent, name, tt, "tool", "{}", "结果文本", 1 if ok else 0,
             10, cid, 0, "", kind, at))

    def seed_main():
        # conv 100：混合会话（首坏 + 级联 + 三信号）
        conn.execute("INSERT INTO conversations (id, title, intent) VALUES (100,'混合会话','chat')")
        llm(100, True, "", ts(0))                          # seq1 GOOD
        llm(100, False, "timeout", ts(1))                  # seq2 BAD ← 首坏
        tool(100, "kb_search", True, ts(2))                # seq3 GOOD
        tool(100, "zhiyuan_project_list", False, ts(3))    # seq4 BAD 级联（无 kind）
        llm(100, True, "", ts(4), mock=1)                  # seq5 GOOD + mock 信号
        llm(100, True, "", ts(5), fr="length")             # seq6 GOOD + 截断信号
        llm(100, True, "", ts(6), rc=2)                    # seq7 GOOD + 重试信号
        # conv 200：同秒并列（平局裁决靶）
        conn.execute("INSERT INTO conversations (id, title, intent) VALUES (200,'同秒会话','chat')")
        llm(200, True, "", ts(10))
        tool(200, "graph_db_nlquery", False, ts(10))       # 与 llm 同一秒
        # conv 300：结局坏但步骤全好（unattributed 靶：失败发生在埋点步骤之外）
        conn.execute("INSERT INTO conversations (id, title, intent) VALUES (300,'缺口会话','chat')")
        llm(300, True, "", ts(20))
        conn.execute("INSERT INTO agent_tasks (run_id, task_key, title, status, "
                     "conversation_id, created_at) VALUES (1,'tk1','子任务','failed',300,?)",
                     (ts(21),))
        conn.execute("INSERT INTO orch_checkpoints (run_id, conversation_id, phase, created_at) "
                     "VALUES (1,300,'failed',?)", (ts(22),))
        conn.execute("INSERT INTO flow_runs (flow_id, flow_name, status, conversation_id, "
                     "created_at) VALUES ('f1','流水线','failed',300,?)", (ts(23),))
        # conv 400：空会话（clean 靶）
        conn.execute("INSERT INTO conversations (id, title, intent) VALUES (400,'空会话','chat')")
        # conv 500：flow_step 源（failed 节点 → BAD，kind=status）
        conn.execute("INSERT INTO conversations (id, title, intent) VALUES (500,'流水线会话','chat')")
        conn.execute("INSERT INTO flow_runs (id, flow_id, flow_name, status, conversation_id, "
                     "created_at) VALUES (9001,'f1','需求分析','completed',500,?)", (ts(30),))
        conn.execute("INSERT INTO flow_run_steps (run_id, seq, node_id, node_type, node_label, "
                     "status, latency_ms) VALUES (9001,1,'n1','llm','需求解析','done',10)")
        conn.execute("INSERT INTO flow_run_steps (run_id, seq, node_id, node_type, node_label, "
                     "status, latency_ms) VALUES (9001,2,'n2','tool','实体抽取','failed',20)")
        # conv 600：窗外证据（patterns 窗口靶；attribute_session 完整取证含它）
        old = _time.strftime("%Y-%m-%d %H:%M:%S",
                             _time.localtime(_time.time() - 100 * 86400))
        marks["old"] = old
        conn.execute("INSERT INTO conversations (id, title, intent) VALUES (600,'窗外会话','chat')")
        llm(600, False, "quota", old)
        # conv 700/710：级联三连（只聚首坏步靶）
        for cid, base in ((700, 40), (710, 50)):
            conn.execute("INSERT INTO conversations (id, title, intent) "
                         "VALUES (%d,'级联会话','chat')" % cid)
            for i in range(3):
                llm(cid, False, "balance", ts(base + i), intent="", prov="p2", model="m2")
        conn.commit()

    def seed_800():
        # conv 800：max_steps 截断靶（600 个好步骤，全 GOOD 隔离截断语义）
        conn.execute("INSERT INTO conversations (id, title, intent) VALUES (800,'截断会话','chat')")
        for i in range(600):
            llm(800, True, "", ts(1000 + i))
        marks["first800"] = ts(1000)
        conn.commit()

    seed_main()

    # C1 混合会话归因
    r100 = A.attribute_session(conn, 100)
    c1 = r100["counts"]
    check("C1 混合会话：7 步 / 5 好 / 2 坏（首坏 1 + 级联 1），首坏=llm timeout",
          c1 == {"total": 7, "good": 5, "bad": 2, "bad_first": 1, "bad_cascade": 1,
                 "mock": 1, "truncated": 1, "retried": 1, "fallback": 0,
                 "unknown_status": 0}
          and r100["verdict"] == "attributed"
          and r100["first_bad"]["source"] == "llm_call"
          and r100["first_bad"]["kind"] == "timeout"
          and r100["first_bad"]["seq"] == 2,
          f"got={c1} verdict={r100['verdict']}")
    sig_steps = [s for s in r100["steps"] if s["signals"]]
    check("C2 ★mock/截断/重试是信号不是失败（三步全 GOOD 且信号在）",
          len(sig_steps) == 3 and all(s["label"] == "GOOD" for s in sig_steps)
          and {k for s in sig_steps for k, v in s["signals"].items() if v}
          == {"mock", "truncated", "retried"},
          f"got={[(s['label'], s['signals']) for s in sig_steps]}")
    tb = [s for s in r100["steps"] if s["source"] == "tool_call"
          and s["label"] == "BAD"][0]
    check("C3 工具失败历史无 kind ⇒ kind='' 如实记（不伪装成有类型）",
          tb["kind"] == "" and tb["meta"].get("result_head"), f"got kind={tb['kind']!r}")
    # C4 同秒并列
    r200 = A.attribute_session(conn, 200)
    check("C4 ★同秒并列确定性：llm（rank 0）排在 tool 前，首坏是 tool 步",
          [s["source"] for s in r200["steps"]] == ["llm_call", "tool_call"]
          and r200["first_bad"]["source"] == "tool_call"
          and r200["counts"]["bad_first"] == 1 and r200["counts"]["bad_cascade"] == 0,
          f"got={[(s['source'], s['at']) for s in r200['steps']]}")
    # C5 unattributed_failure
    r300 = A.attribute_session(conn, 300)
    check("C5 ★结局坏但零坏步骤 ⇒ unattributed_failure（不伪装成 clean）",
          r300["verdict"] == "unattributed_failure" and r300["outcome_bad"] == 3
          and r300["outcome"] == {"agent_task": 1, "orch_run": 1, "flow_run": 1}
          and r300["counts"]["bad"] == 0,
          f"got={r300['verdict']} outcome={r300['outcome']}")
    # C6 空会话
    r400 = A.attribute_session(conn, 400)
    check("C6 空会话 ⇒ clean、零步骤（不炸）",
          r400["verdict"] == "clean" and r400["counts"]["total"] == 0
          and r400["first_bad"] is None and r400["outcome_bad"] == 0,
          f"got={r400['verdict']}")
    # C7 flow_step 源
    r500 = A.attribute_session(conn, 500)
    fs = [(s["actor"], s["label"], s["kind"]) for s in r500["steps"]]
    check("C7 flow_step 源：failed 节点 → BAD（kind=status）、done → GOOD，at 继承 run 时间",
          fs == [("需求解析", "GOOD", ""), ("实体抽取", "BAD", "failed")]
          and r500["first_bad"]["actor"] == "实体抽取"
          and r500["steps"][0]["at"] != "" and r500["verdict"] == "attributed",
          f"got={fs}")
    # C7b 窗外证据：会话详情完整取证（不过窗口）
    r600 = A.attribute_session(conn, 600)
    check("C7b 会话详情完整取证：-100 天的窗外失败也在（排查要看全部历史）",
          r600["counts"]["total"] == 1 and r600["counts"]["bad"] == 1
          and r600["steps"][0]["kind"] == "quota", f"got={r600['counts']}")

    # C8-C13 patterns（默认口径；conv800 尚未插——聚合数字保持可读）
    p = A.collect_patterns(conn)
    lt = p["label_totals"]
    check("C8 patterns 窗口内 6 会话（空会话/窗外不入）：18 步 / 8 好 / 10 坏 "
          "= 首坏 5 + 级联 5",
          lt == {"total": 18, "good": 8, "bad": 10, "bad_first": 5, "bad_cascade": 5}
          and p["sessions_scanned"] == 6 and p["sessions_with_first_bad"] == 5
          and p["window_cutoff"] != "",
          f"got={lt} scanned={p['sessions_scanned']}")
    kc = p["kind_coverage"]
    check("C9 ★kind 覆盖率诚实统计：known(8)+unknown(2)=bad_steps(10)，"
          "tool 历史空 kind 记 unknown",
          kc["bad_steps"] == 10 and kc["known_kind"] == 8 and kc["unknown_kind"] == 2
          and kc["known_pct"] == 80.0, f"got={kc}")
    check("C10 ★级联不放大：700/710 各 3 连坏只出 1 个首坏模式"
          "（min_occurrences=2 滤掉只现 1 次的 timeout/flow 模式）",
          len(p["first_bad_patterns"]) == 1
          and p["first_bad_patterns"][0]["kind"] == "balance"
          and p["first_bad_patterns"][0]["count"] == 2
          and p["first_bad_patterns"][0]["conversations"] == 2,
          f"got={p['first_bad_patterns']}")
    pat = p["first_bad_patterns"][0]
    check("C11 首坏模式可溯源（sample_steps 带 step_key + first/last_seen 有序）",
          len(pat["sample_steps"]) >= 1 and all(":" in k for k in pat["sample_steps"])
          and pat["first_seen"] <= pat["last_seen"], f"got={pat}")
    check("C12 unattributed 清单：conv300 结局坏被列出（埋点缺口可见）",
          p["unattributed_sessions"]["count"] == 1
          and p["unattributed_sessions"]["samples"] == [300],
          f"got={p['unattributed_sessions']}")
    check("C13 窗外失败不入模式（quota 模式不存在 = conv600 被窗口挡下）",
          all(pt["kind"] != "quota" for pt in p["first_bad_patterns"]))

    # C14/C15 截断（conv800 插在 patterns 断言之后，避免污染聚合数字）
    seed_800()
    r800 = A.attribute_session(conn, 800)
    check("C14 max_steps=500 截断：600 步只留最早 500，截断事实明示"
          "（truncated + dropped + 首步即最早）",
          r800["counts"]["total"] == 500 and r800["truncated"] is True
          and r800["dropped_steps"] == 100
          and r800["steps"][0]["at"] == marks["first800"],
          f"got total={r800['counts']['total']} dropped={r800['dropped_steps']}")
    check("C15 截断不翻标签（被丢的是尾部；本例全好 ⇒ clean）",
          r800["verdict"] == "clean" and r800["counts"]["bad"] == 0,
          f"got={r800['verdict']}")
    conn.close()

    # ── B' 变异自证（口径必须真被消费）──────────────────────────────
    print("\n== B'. 变异自证（四个口径逐一变异，断言必须转红）==")
    # 重建干净库：C 段种子 + conv800 的痕迹全部清掉，四个变异从同一基线出发。
    # get_db 每次新建连接读同一 DB_PATH（import 期绑定，改环境变量已无效），
    # 删文件 + WAL 边车后重新 init 是唯一干净的基线重置方式。
    for _sfx in ("", "-wal", "-shm"):
        try:
            os.remove(_CLEAN_DB + _sfx)
        except OSError:
            pass
    from database import init_db as _init2, get_db as _get2
    _init2()
    conn2 = _get2()
    conn2.row_factory = sqlite3.Row
    _conn_backup = conn
    conn = conn2          # seed_main/seed_800/llm/tool 闭包改指向 conn2

    # 重建 marks（seed_main 依赖 marks['old']，seed_800 依赖 marks['first800']）
    marks.clear()
    seed_main()
    seed_800()
    _og, _ob = CFG.get, CFG.as_bool

    def _mut(section, key, value, is_bool=False):
        if is_bool:
            CFG.as_bool = lambda s, k, d=False, _v=value: (
                _v if (s, k) == (section, key) else _ob(s, k, d))
        else:
            CFG.get = lambda s, k, d=None, _v=value: (
                _v if (s, k) == (section, key) else _og(s, k, d))

    try:
        # M1 window_days=1 ⇒ -2 day 种子全部掉出窗口
        _mut("attribution", "window_days", 1)
        m1 = A.collect_patterns(conn2)
        check("M1 ★window_days=1 ⇒ 零会话入窗、零聚合（窗口口径真被消费）",
              m1["sessions_scanned"] == 0 and m1["first_bad_patterns"] == []
              and m1["label_totals"]["bad"] == 0, f"got={m1['label_totals']}")
        CFG.get = _og
        # M2 min_occurrences=99 ⇒ 首现 2 次的 balance 模式也被滤掉
        _mut("attribution", "min_occurrences", 99)
        m2 = A.collect_patterns(conn2)
        check("M2 ★min_occurrences=99 ⇒ 模式全滤掉（聚合下限真被消费）",
              m2["first_bad_patterns"] == [] and m2["sessions_with_first_bad"] == 5,
              f"got={len(m2['first_bad_patterns'])} patterns")
        CFG.get = _og
        # M3 max_steps=3 ⇒ conv800 截到最早 3 步
        _mut("attribution", "max_steps", 3)
        m3 = A.attribute_session(conn2, 800)
        check("M3 ★max_steps=3 ⇒ 只留最早 3 步（会话内截断口径真被消费）",
              m3["counts"]["total"] == 3 and m3["dropped_steps"] == 597
              and m3["truncated"], f"got={m3['counts']['total']}/{m3['dropped_steps']}")
        CFG.get = _og
        # M4 enabled=False ⇒ collect_patterns 返空不报错
        _mut("attribution", "enabled", False, is_bool=True)
        m4 = A.collect_patterns(conn2)
        check("M4 ★enabled=False ⇒ 返 enabled=False、零聚合（总开关真被消费）",
              m4["enabled"] is False and m4["first_bad_patterns"] == []
              and m4["sessions_scanned"] == 0, f"got={m4}")
    finally:
        CFG.get, CFG.as_bool = _og, _ob
        conn = _conn_backup
        conn2.close()

    # ── D. 真库只读 smoke ──────────────────────────────────────────
    print("\n== D. 真库只读 smoke（裸 SQL 对账 + 聚合自洽 + 写入点行为级证明）==")
    _prod = os.path.join(ROOT, "mbse.db")
    if not os.path.exists(_prod):
        skip("D1-D5b 真库 smoke", "mbse.db 不存在（CI 环境）")
    else:
        pc = sqlite3.connect("file:%s?mode=ro" % _prod.replace("\\", "/"), uri=True)
        pc.row_factory = sqlite3.Row
        # 靶会话：报告实例 9336（在且有步骤），否则失败步骤最多的会话
        tgt = 9336
        n0 = pc.execute("SELECT (SELECT COUNT(*) FROM llm_usage_stats WHERE conversation_id=?) "
                        "+ (SELECT COUNT(*) FROM tool_call_logs WHERE conversation_id=?)",
                        (tgt, tgt)).fetchone()[0]
        if not n0:
            row = pc.execute(
                "SELECT conversation_id cid, "
                "(SELECT COUNT(*) FROM llm_usage_stats l WHERE l.conversation_id = t.conversation_id) "
                "+ (SELECT COUNT(*) FROM tool_call_logs k WHERE k.conversation_id = t.conversation_id) n "
                "FROM (SELECT DISTINCT conversation_id FROM llm_usage_stats WHERE conversation_id > 0 "
                "UNION SELECT DISTINCT conversation_id FROM tool_call_logs WHERE conversation_id > 0) t "
                "ORDER BY n DESC LIMIT 1").fetchone()
            tgt = row["cid"] if row else 0
        if not tgt:
            skip("D1-D5b 真库 smoke", "真库无任何带步骤的会话")
        else:
            r = A.attribute_session(pc, tgt)
            la = pc.execute("SELECT COUNT(*) FROM llm_usage_stats "
                            "WHERE conversation_id=?", (tgt,)).fetchone()[0]
            lf = pc.execute("SELECT COALESCE(SUM(call_failed),0) FROM llm_usage_stats "
                            "WHERE conversation_id=?", (tgt,)).fetchone()[0]
            ta = pc.execute("SELECT COUNT(*) FROM tool_call_logs "
                            "WHERE conversation_id=?", (tgt,)).fetchone()[0]
            tf = pc.execute("SELECT COALESCE(SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END),0) "
                            "FROM tool_call_logs WHERE conversation_id=?", (tgt,)).fetchone()[0]
            print("  真库靶会话 #%d：llm %d 调用/%d 失败, tool %d 调用/%d 失败 → 引擎 %d 步 / "
                  "%d 坏（首坏 %d + 级联 %d）"
                  % (tgt, la, lf, ta, tf, r["counts"]["total"], r["counts"]["bad"],
                     r["counts"]["bad_first"], r["counts"]["bad_cascade"]))
            check("D1 ★引擎计数 == 裸 SQL 对账（total/bad/good 三通道 + 首坏+级联=坏）",
                  r["counts"]["total"] == la + ta and r["counts"]["bad"] == lf + tf
                  and r["counts"]["good"] == (la + ta) - (lf + tf)
                  and r["counts"]["bad_first"] + r["counts"]["bad_cascade"] == r["counts"]["bad"],
                  f"engine={r['counts']} raw=({la}+{ta}, {lf}+{tf})")
            check("D2 verdict 三态取值合法且与坏步数自洽",
                  r["verdict"] in A.VERDICTS
                  and (r["verdict"] == "attributed") == (r["counts"]["bad"] > 0),
                  f"got={r['verdict']}")
            p = A.collect_patterns(pc)
            lt, kc = p["label_totals"], p["kind_coverage"]
            check("D3 真库 patterns 聚合自洽（首坏+级联=坏；known+unknown=坏步；"
                  "有首坏会话 ≤ 枚举会话）",
                  lt["bad_first"] + lt["bad_cascade"] == lt["bad"]
                  and kc["known_kind"] + kc["unknown_kind"] == kc["bad_steps"]
                  and p["sessions_with_first_bad"] <= p["sessions_scanned"],
                  f"got={lt} {kc}")
            print("  真库 patterns：枚举 %d 会话 / %d 有首坏步 / kind 覆盖 %s / "
                  "unattributed %d 个"
                  % (p["sessions_scanned"], p["sessions_with_first_bad"], kc,
                     p["unattributed_sessions"]["count"]))
            check("D4 kind 覆盖率百分比与计数一致（诚实统计不断言）",
                  kc["known_pct"] == (round(100.0 * kc["known_kind"] / kc["bad_steps"], 1)
                                      if kc["bad_steps"] else None),
                  f"got={kc['known_pct']}")
            check("D5 真库模式受 min_occurrences/max_patterns 约束且可溯源",
                  all(pt["count"] >= int(A.get_settings()["min_occurrences"])
                      for pt in p["first_bad_patterns"])
                  and len(p["first_bad_patterns"]) <= int(A.get_settings()["max_patterns"])
                  and all(pt["sample_steps"] for pt in p["first_bad_patterns"]),
                  f"got={len(p['first_bad_patterns'])} patterns")
            check("D5b unattributed 清单结构（count == len(samples) ≤ 10）",
                  p["unattributed_sessions"]["count"]
                  == len(p["unattributed_sessions"]["samples"])
                  and len(p["unattributed_sessions"]["samples"]) <= 10)
        pc.close()

    # D6 写入点修复的行为级证明（独立子进程 + 独立库）
    print("\n== D6. tool_call_logs 写入点行为级证明（子进程独立库）==")
    import json as _json
    import subprocess
    import tempfile
    _d6dir = tempfile.mkdtemp(prefix="verify_attr_d6_")
    _d6db = os.path.join(_d6dir, "d6.db")
    _child = (
        "import os, sys, sqlite3, json\n"
        "db = sys.argv[1]\n"
        "os.environ['MBSE_DB_PATH'] = db\n"
        "sys.path.insert(0, sys.argv[2])\n"
        "c = sqlite3.connect(db)\n"
        "c.execute('CREATE TABLE tool_call_logs (id INTEGER PRIMARY KEY, intent TEXT, agent_name TEXT, tool_name TEXT NOT NULL, tool_type TEXT, arguments TEXT, result TEXT, ok INTEGER, latency_ms INTEGER, conversation_id INTEGER, user_name TEXT, run_id INTEGER, task_key TEXT, failure_kind TEXT, call_kind TEXT, capability_id TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP)')\n"
        "c.commit(); c.close()\n"
        "from agent.pipeline_parts.tools import ToolMixin\n"
        "class Dummy: pass\n"
        "d = Dummy(); d._tool_user = None; d._tool_run_ctx = 7; d._tool_task_key = 'tk1'\n"
        "ToolMixin._log_tool_call(d, 'zhiyuan_project_list', 'mcp', {'q': 1},\n"
        "    {'ok': False, 'error': 'HTTP 503 Service Unavailable'}, {'intent': 'knowledge_qa'},\n"
        "    {'agent': '知识工程师'}, 9336, 0.0)\n"
        "ToolMixin._log_tool_call(d, 'sysml_check', 'builtin', {},\n"
        "    {'ok': False, 'result': 'permission_denied: 无权限执行该校验'}, {'intent': 'review'},\n"
        "    {'agent': '设计师'}, 9336, 0.0)\n"
        "ToolMixin._log_tool_call(d, 'ok_tool', 'builtin', {},\n"
        "    {'ok': True, 'result': 'done'}, {'intent': 'chat'}, {'agent': 'a'}, 100, 0.0)\n"
        "c = sqlite3.connect(db); c.row_factory = sqlite3.Row\n"
        "print(json.dumps([dict(r) for r in c.execute('SELECT tool_name, ok, failure_kind, run_id, task_key FROM tool_call_logs')], ensure_ascii=False))\n")
    _cp = subprocess.run([sys.executable, "-c", _child, _d6db, ROOT],
                         capture_output=True, text=True, encoding="utf-8", timeout=180)
    try:
        _rows = _json.loads(_cp.stdout.strip().splitlines()[-1]) if _cp.stdout.strip() else []
    except Exception:
        _rows = []
    by = {r["tool_name"]: r for r in _rows}
    check("D6 ★写入点修复行为级证明：失败调用落非空 kind、成功留空"
          "（嵌套 classify bug 不复发），run_id/task_key 归因列同在",
          bool(by.get("zhiyuan_project_list", {}).get("failure_kind"))
          and bool(by.get("sysml_check", {}).get("failure_kind"))
          and by.get("ok_tool", {}).get("failure_kind") == ""
          and by.get("zhiyuan_project_list", {}).get("run_id") == 7
          and by.get("zhiyuan_project_list", {}).get("task_key") == "tk1",
          f"rc={_cp.returncode} rows={_rows} err={_cp.stderr[-300:]}")
    shutil.rmtree(_d6dir, ignore_errors=True)

    # ── E. 接线完整性 ───────────────────────────────────────────────
    print("\n== E. 接线完整性（monitor 路由 + config 双登记 + 零写语句 + 写入点）==")
    _mon = open(os.path.join(ROOT, "routers", "monitor.py"), encoding="utf-8").read()
    check("E1 monitor.py：session 详情挂 attribution 段 + patterns 端点 + 单段降级",
          "/api/monitor/attribution/patterns" in _mon
          and "_attr.attribute_session(conn, cid)" in _mon
          and "_attr.collect_patterns(conn, settings=st)" in _mon
          and 'out["attribution"] = {"ok": False' in _mon)
    import importlib
    _m = importlib.import_module("routers.monitor")
    _paths = [getattr(r, "path", "") for r in _m.router.routes]
    check("E2 patterns 端点真注册在 router（运行期断言，非字符串猜）",
          "/api/monitor/attribution/patterns" in _paths,
          f"got={[p for p in _paths if 'attribution' in p]}")
    _cf = open(os.path.join(ROOT, "core", "config.py"), encoding="utf-8").read()
    check("E3 core/config.py 双登记（DEFAULT_CONFIG + CONFIG_SCHEMA）",
          _cf.count('"attribution"') >= 2, f"count={_cf.count('\"attribution\"')}")
    _sql_all = "\n".join([s.sql for s in A._SPECS]
                         + [s.enum_sql for s in A._SPECS]
                         + [o.sql for o in A._OUTCOME_SPECS])
    import re as _re
    _write_kw = [w for w in ("INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "REPLACE")
                 if _re.search(r"\b%s\b" % w, _sql_all, _re.I)]
    check("E4 ★模块零写语句（实际执行的 SQL 全集静态断言：只读取材）",
          not _write_kw, f"发现写语句关键词: {_write_kw}")
    _tl = open(os.path.join(ROOT, "agent", "pipeline_parts", "tools.py"),
               encoding="utf-8").read()
    _mm = open(os.path.join(ROOT, "agent", "pipeline_parts", "memory.py"),
               encoding="utf-8").read()
    # 剥注释再查：修复说明注释里引用旧写法 `_fc(classify(...))` 是文档，不是代码
    # ★2026-10-24 判据同步：产品 2026-10-21 已把别名 `_fc` 改为 `_fc_classify`
    #   （原别名与 _build_tools_def 的 DB 连接局部变量重名，踩 e2e G0 危险名集合，
    #   代码里有注释为证）——门禁字符串停在旧别名 ⇒ HEAD 起就假红。意图不变：
    #   无嵌套调用 + 单次调用显式 ok=False + 导入在位。
    _tl_code = "\n".join(l for l in _tl.splitlines()
                         if not l.strip().startswith("#"))
    check("E5 ★tools.py 写入点无嵌套 classify（双重调用 bug 不复发）且显式 ok=False",
          "_fc_classify(classify(" not in _tl_code
          and "_fkind = _fc_classify(" in _tl_code
          and "ok=False)" in _tl_code
          and "from services.failure_class import classify as _fc_classify" in _tl_code,
          f"code 中仍残留嵌套调用: {[l.strip() for l in _tl_code.splitlines() if '_fc_classify(classify(' in l][:2]}")
    check("E6 memory.py 技能日志写入点补 failure_kind 列（第二个写入点不漏）",
          "failure_kind" in _mm)

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
    print("全部通过 —— 步骤级归因器（3 源注册表 + 首坏/级联/三态 verdict + 六键配置变异自证 "
          "+ patterns 只聚首坏 + kind 诚实覆盖 + 真库对账 + 写入点行为级证明 + 接线）。")


if __name__ == "__main__":
    main()
