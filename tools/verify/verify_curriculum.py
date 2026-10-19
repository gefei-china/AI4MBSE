# -*- coding: utf-8 -*-
"""P2-2 门禁：失败驱动的任务自生成（curriculum，自进化调研路线图 P2 第二项）。

═══════════════════════════════════════════════════════════════
动机（2026-10-18《自进化 AI 平台调研评估》路线图 P2 第二项）
═══════════════════════════════════════════════════════════════
EnvBuilder / AgentGym / AgentEvolver Self-Questioning：agent 自己造课程——
从失败里长出针对弱点的练习任务。本门禁守 core/curriculum 的证据口径与
「只提议不自动执行」铁律：证据诚实（空窗口不凑任务）、弱点身份进指纹
（不同弱点互不当重复）、人裁决留痕、三层合起来无创建任务的路径。

判据结构（承 verify_sandbox / verify_config_proposal / verify_misevolution
的五段式）：
  A 纯函数    difficulty 分档 + 指纹（弱点身份进指纹）+ 裁决合法性 +
              四规则 match 行为（含诚实分母：空窗口不产任务）
  B 配置      curriculum 组双登记 + 键一致 + 引擎零写语句 + 口径真被消费
              （影子配置变异自证）
  C 端到端    干净临时库（import 前切 MBSE_DB_PATH/MBSE_CONFIG_PATH）：
              合成失败证据 → 已知提案 → 空窗口诚实 ⇒ 指纹去重 → 裁决
              三态 → ★第二连接可见（repo 自带 commit）→ ★全表快照零副作用
              （除队列外任何表行数零变化）→ 端点行为级（含冷库显式失败）
              → 变异自证
  D 真库      只读取证：证据包五源可取、结构完整（不断言具体值）
  E 接线      迁移三处 + 端点路由注册 + repo 只碰自家表 + 三层无任务
              创建路径（静态）+ 配置注释含铁律依据

运行：python tools/verify/verify_curriculum.py
"""
import io
import json
import os
import re
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# ── 干净库 + 隔离配置：先切后 import（core.config/DB 路径 import 期绑定）──
_TMP = os.path.join(ROOT, "tmp", "verify_curriculum")
os.makedirs(_TMP, exist_ok=True)
_CLEAN_DB = os.path.join(_TMP, "clean.db")
_CLEAN_CFG = os.path.join(_TMP, "config.json")
for _p in (_CLEAN_DB, _CLEAN_CFG):
    if os.path.exists(_p):
        os.remove(_p)
io.open(_CLEAN_CFG, "w", encoding="utf-8").write("{}")
os.environ["MBSE_DB_PATH"] = _CLEAN_DB
os.environ["MBSE_CONFIG_PATH"] = _CLEAN_CFG

_results = []


def check(name, ok, detail=""):
    _results.append((name, bool(ok), detail))
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name,
                           ("  ← " + str(detail)) if (detail and not ok) else ""))


def section(t):
    print("\n== " + t + " " + "=" * max(0, 60 - len(t)))


def _read(rel):
    return io.open(os.path.join(ROOT, rel), encoding="utf-8").read()


def _n_rows(conn, table):
    return int(conn.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0])


# ═══════════════════════════════════════════════════════════════
section("A 纯函数：难度分档 + 指纹 + 裁决 + 四规则行为")
# ═══════════════════════════════════════════════════════════════
from core import curriculum as cu                       # noqa: E402

# A1 difficulty_of：保守分档（只按证据量，无任务先验）
check("A1a 0/2 次 ⇒ low", cu.difficulty_of(0) == "low" and cu.difficulty_of(2) == "low")
check("A1b 3/7 次 ⇒ medium", cu.difficulty_of(3) == "medium" and cu.difficulty_of(7) == "medium")
check("A1c ≥8 次 ⇒ high", cu.difficulty_of(8) == "high" and cu.difficulty_of(20) == "high")
check("A1d 非数值 ⇒ low（不猜）", cu.difficulty_of("abc") == "low" and cu.difficulty_of(None) == "low")

# A2 task_fingerprint：弱点身份必须进指纹（P1-3 教训：否则不同弱点互相当重复）
_f_same1 = cu.task_fingerprint("weakness_repeat", "regression_hardening", "tool_call|design|timeout|evil_tool")
_f_same2 = cu.task_fingerprint("weakness_repeat", "regression_hardening", "tool_call|design|timeout|evil_tool")
_f_diff_w = cu.task_fingerprint("weakness_repeat", "regression_hardening", "tool_call|design|timeout|other_tool")
_f_diff_r = cu.task_fingerprint("badcase_recurrence", "regression_hardening", "tool_call|design|timeout|evil_tool")
check("A2a 同输入同指纹", _f_same1 == _f_same2 and len(_f_same1) == 16)
check("A2b 弱点不同 ⇒ 指纹不同（证据不互吃）", _f_same1 != _f_diff_w)
check("A2c 规则不同 ⇒ 指纹不同", _f_same1 != _f_diff_r)

# A3 can_decide：只有 pending 可决（终态不重开，与 #18 状态机纪律一致）
check("A3a pending 可决 / 已决不可决",
      cu.can_decide(cu.TASK_PENDING) and not cu.can_decide(cu.TASK_ACCEPTED)
      and not cu.can_decide(cu.TASK_REJECTED) and not cu.can_decide(""))
check("A3b 三态常量齐全", cu.TASK_STATUSES == ("pending", "accepted", "rejected")
      and cu.TASK_TERMINAL == ("accepted", "rejected"))


def _evidence(patterns=None, badcases=None, kc=None, unattr=0):
    return {"patterns": patterns or {}, "badcases": badcases or [],
            "kind_coverage": kc or {}, "unattributed_count": unattr}


_CFG = cu.get_settings()

# A4 weakness_repeat：复发达下限才产任务；分档随 count
_pat = {"first_bad_patterns": [{"source": "tool_call", "intent": "design",
                                "kind": "timeout", "actor": "evil_tool",
                                "count": 2, "conversations": 2,
                                "first_seen": "2026-10-18 10:00:00",
                                "last_seen": "2026-10-19 10:00:00",
                                "sample_steps": ["tool_call:1", "tool_call:2"]}]}
_d = cu.propose_from_evidence(_evidence(patterns=_pat), settings=_CFG)
check("A4a 复发 2 次（=下限）⇒ 1 条回归巩固任务",
      len(_d) == 1 and _d[0]["rule"] == "weakness_repeat"
      and _d[0]["task_kind"] == "regression_hardening"
      and _d[0]["weakness"] == "tool_call|design|timeout|evil_tool", _d)
check("A4b count=2 ⇒ difficulty=low；建议含样本步骤",
      _d[0]["difficulty"] == "low" and "tool_call:1" in _d[0]["suggestion"], _d[0])
_pat_low = {"first_bad_patterns": [dict(_pat["first_bad_patterns"][0], count=1)]}
check("A4c 复发 1 次（<下限）⇒ 不产任务（单发不立任务）",
      cu.propose_from_evidence(_evidence(patterns=_pat_low), settings=_CFG) == [])
_pat_hi = {"first_bad_patterns": [dict(_pat["first_bad_patterns"][0], count=9)]}
_d_hi = cu.propose_from_evidence(_evidence(patterns=_pat_hi), settings=_CFG)
check("A4d count=9 ⇒ difficulty=high", _d_hi and _d_hi[0]["difficulty"] == "high", _d_hi)

# A5 badcase_recurrence：open 且复发达下限
_bc = [{"kind": "llm_call_failed", "subject": "prov-a", "title": "配额",
        "severity": "high", "occurrences": 4, "status": "open",
        "first_seen_at": "2026-10-15 10:00:00", "last_seen_at": "2026-10-19 10:00:00"}]
_d5 = cu.propose_from_evidence(_evidence(badcases=_bc), settings=_CFG)
check("A5a open 且 4 次（≥3）⇒ 1 条复现任务",
      len(_d5) == 1 and _d5[0]["task_kind"] == "badcase_reproduction"
      and _d5[0]["weakness"] == "llm_call_failed|prov-a"
      and _d5[0]["difficulty"] == "medium", _d5)
_bc_low = [dict(_bc[0], occurrences=2)]
check("A5b 2 次（<3）⇒ 不产任务",
      cu.propose_from_evidence(_evidence(badcases=_bc_low), settings=_CFG) == [])
_bc_resolved = [dict(_bc[0], status="resolved")]
check("A5c 非 open ⇒ 不产任务（规则内自带 status 判据，不依赖调用方预过滤）",
      cu.propose_from_evidence(_evidence(badcases=_bc_resolved), settings=_CFG) == [])

# A6 kind_coverage_gap：低于下沿才产；**空窗口不产**（诚实分母）
_kc_low = {"bad_steps": 6, "known_kind": 2, "unknown_kind": 4, "known_pct": 33.3}
_d6 = cu.propose_from_evidence(_evidence(kc=_kc_low), settings=_CFG)
check("A6a 覆盖率 33.3% < 50% ⇒ 1 条补分类任务",
      len(_d6) == 1 and _d6[0]["task_kind"] == "failure_classification"
      and _d6[0]["weakness"] == "kind_coverage", _d6)
_kc_ok = {"bad_steps": 6, "known_kind": 5, "unknown_kind": 1, "known_pct": 83.3}
check("A6b 覆盖率 83.3% ≥ 下沿 ⇒ 不产任务",
      cu.propose_from_evidence(_evidence(kc=_kc_ok), settings=_CFG) == [])
_kc_empty = {"bad_steps": 0, "known_kind": 0, "unknown_kind": 0, "known_pct": None}
check("A6c ★空窗口（bad_steps=0, pct=None）⇒ 不产任务（空窗口不是 0% 覆盖率）",
      cu.propose_from_evidence(_evidence(kc=_kc_empty), settings=_CFG) == [])

# A7 unattributed_gap：结局坏零坏步达下限
_d7 = cu.propose_from_evidence(_evidence(unattr=3), settings=_CFG)
check("A7a 3 个 unattributed 会话 ⇒ 1 条补埋点任务",
      len(_d7) == 1 and _d7[0]["task_kind"] == "instrumentation"
      and _d7[0]["difficulty"] == "medium", _d7)
_d7b = cu.propose_from_evidence(_evidence(unattr=6), settings=_CFG)
check("A7b ≥5 个 ⇒ difficulty=high", _d7b and _d7b[0]["difficulty"] == "high", _d7b)
check("A7c 0 个 ⇒ 不产任务",
      cu.propose_from_evidence(_evidence(unattr=0), settings=_CFG) == [])

# A8 排序确定 + 截断 + enabled + 规则异常不连坐
_multi = _evidence(patterns={"first_bad_patterns": [
    {"source": "tool_call", "intent": "design", "kind": "timeout", "actor": "evil_tool",
     "count": 2, "conversations": 2, "sample_steps": []},
    {"source": "tool_call", "intent": "design", "kind": "", "actor": "kb_search",
     "count": 9, "conversations": 9, "sample_steps": []}]},
    badcases=_bc, kc=_kc_low, unattr=1)
_d8a = cu.propose_from_evidence(_multi, settings=_CFG)
_d8b = cu.propose_from_evidence(_multi, settings=_CFG)
check("A8a 同输入同输出（排序确定性）",
      [d["fingerprint"] for d in _d8a] == [d["fingerprint"] for d in _d8b], _d8a)
check("A8b 排序 = 难度档倒序（high 的 kb_search 在前）",
      _d8a[0]["weakness"] == "tool_call|design||kb_search"
      and _d8a[0]["difficulty"] == "high", [d["weakness"] for d in _d8a])
_d8c = cu.propose_from_evidence(_multi, settings=dict(_CFG, max_proposals=2))
check("A8c max_proposals 截断生效", len(_d8c) == 2, len(_d8c))
check("A8d enabled=False ⇒ 零草案",
      cu.propose_from_evidence(_multi, settings=dict(_CFG, enabled=False)) == [])
_bad_type = _evidence(badcases=[{"kind": "x", "subject": "y", "occurrences": "not-a-number"}])
check("A8e 单条证据脏数据不连坐（occurrences 非数值 ⇒ 该条不产，不崩）",
      cu.propose_from_evidence(_bad_type, settings=_CFG) == [])

# A9 注册表完整
check("A9a 四规则齐备且名字唯一",
      len(cu._RULES) == 4 and len({r.name for r in cu._RULES}) == 4,
      [r.name for r in cu._RULES])
check("A9b 规则规格完整（label/desc/match 齐）",
      all(r.label and r.desc and callable(r.match) for r in cu._RULES))
check("A9c 任务种类常量覆盖四规则的产出",
      set(cu.TASK_KINDS) == {"regression_hardening", "badcase_reproduction",
                             "failure_classification", "instrumentation"})

# ═══════════════════════════════════════════════════════════════
section("B 配置：双登记 + 引擎零写 + 口径被消费")
# ═══════════════════════════════════════════════════════════════
from core import config as _cfg                                    # noqa: E402

_dflt = _cfg.DEFAULT_CONFIG.get("curriculum") or {}
_sch = _cfg.CONFIG_SCHEMA.get("curriculum") or {}
check("B1 curriculum 组在 DEFAULT_CONFIG", bool(_dflt))
check("B2 curriculum 组在 CONFIG_SCHEMA", bool(_sch))
check("B3 两处键集合一致", set(_dflt) == set(_sch),
      "defaults-only=%s schema-only=%s" % (set(_dflt) - set(_sch), set(_sch) - set(_dflt)))
check("B4 组内 6 键（1 开关 + 4 口径 + 1 上限）", len(_dflt) == 6, sorted(_dflt))
check("B4b enabled 是 bool 型（as_bool 口径）", _sch.get("enabled", {}).get("type") == "bool")
# B5 引擎零写语句（静态：代码区无写 SQL 关键词）
_eng_src = _read(os.path.join("core", "curriculum.py"))
_code = re.sub(r'""".*?"""', "", _eng_src, flags=re.S)      # 去 docstring 防误伤
check("B5a 引擎无写语句（INSERT/UPDATE/DELETE/CREATE/DROP/ALTER/REPLACE）",
      not re.search(r"\b(INSERT|UPDATE|DELETE|CREATE|DROP|ALTER|REPLACE)\b", _code))
check("B5b 引擎所有 SQL 都是 SELECT 开头",
      all(s.strip().upper().startswith("SELECT")
          for s in re.findall(r'"(SELECT[^"]*)"', _eng_src)))
# B6 口径真被消费：影子配置覆盖 min_pattern_count=99 ⇒ 复发 2 次的模式不再产任务
#   （同时验证 P1-4 影子层对本组生效；生产进程不设 ⇒ 行为零变化）
_old_shadow = os.environ.get("MBSE_SHADOW_CONFIG")
try:
    os.environ["MBSE_SHADOW_CONFIG"] = '{"curriculum": {"min_pattern_count": 99}}'
    _cfg.reload()
    _d6s = cu.propose_from_evidence(_evidence(patterns=_pat), settings=None)
    check("B6a 影子 min_pattern_count=99 ⇒ 复发 2 次不产任务（口径真被消费）",
          _d6s == [], _d6s)
    _st6 = cu.get_settings()
    check("B6b get_settings 读到影子值 99", _st6["min_pattern_count"] == 99, _st6)
    check("B6c 影子未覆盖的键仍取默认（分层合并，不是整体替换）",
          _st6["min_badcase_occurrences"] == 3, _st6)
    os.environ["MBSE_SHADOW_CONFIG"] = '{"curriculum": {"no_such_key": 1}}'
    _raised = False
    try:
        _cfg.reload()
    except ValueError:
        _raised = True
    check("B6d 未知键 fail-loud（typo 不静默失效）", _raised)
finally:
    if _old_shadow is None:
        os.environ.pop("MBSE_SHADOW_CONFIG", None)
    else:
        os.environ["MBSE_SHADOW_CONFIG"] = _old_shadow
    _cfg.reload()
_st6b = cu.get_settings()
check("B6e 影子撤除后口径复位（补丁不污染后续断言）",
      _st6b["min_pattern_count"] == 2, _st6b)

# ═══════════════════════════════════════════════════════════════
section("C 端到端：干净库合成证据 → 提案 → 去重 → 裁决 → 零副作用 → 端点")
# ═══════════════════════════════════════════════════════════════
from database import init_db, get_db                             # noqa: E402

init_db()
_db_cm = get_db()
_conn = _db_cm.__enter__()
_conn.row_factory = sqlite3.Row

# C1 表结构
_tbls = {r[0] for r in _conn.execute(
    "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
check("C1a curriculum_items 已建", "curriculum_items" in _tbls)
_cols = [r[1] for r in _conn.execute(
    "PRAGMA table_info(curriculum_items)").fetchall()]
for _want in ("rule", "task_kind", "title", "weakness", "suggestion", "difficulty",
              "evidence_json", "fingerprint", "status"):
    check("C1b 列 %s 在位" % _want, _want in _cols, _cols)
_fp_cols = [r for r in _conn.execute("PRAGMA table_info(curriculum_items)").fetchall()
            if r[1] == "fingerprint"]
check("C1c fingerprint 列 NOT NULL + UNIQUE",
      bool(_fp_cols) and _fp_cols[0][3] == 1, _fp_cols)


def _ts(off):
    # 窗内基串：本地时区 now-2day+off（归因引擎 cutoff 同为本地时区，口径一致）
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 2 * 86400 + off))


def _tool(cid, name, ok, at, kind="", intent="design"):
    _conn.execute(
        "INSERT INTO tool_call_logs (intent, tool_name, tool_type, call_kind, arguments, "
        "result, ok, latency_ms, conversation_id, run_id, task_key, failure_kind, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (intent, name, "builtin", "tool", "{}", "结果", 1 if ok else 0,
         10, cid, 0, "", kind, at))


# C2 合成失败证据（全部落在 90 天归因窗口内）：
#   conv 901/902  evil_tool timeout ×2      ⇒ 模式 count=2（weakness_repeat/low）
#   conv 903-906  kb_search 无 kind ×4      ⇒ 模式 count=4（weakness_repeat/medium）
#                                              且覆盖率 known=2/unknown=4=33%（补分类）
#   conv 907      步骤全好 + 结局坏         ⇒ unattributed=1（补埋点）
for _cid in (901, 902):
    _conn.execute("INSERT INTO conversations (id, title, intent) VALUES (?,?,?)",
                  (_cid, "复发会话", "design"))
    _tool(_cid, "evil_tool", False, _ts(1), kind="timeout")
for _cid in (903, 904, 905, 906):
    _conn.execute("INSERT INTO conversations (id, title, intent) VALUES (?,?,?)",
                  (_cid, "无类型会话", "design"))
    _tool(_cid, "kb_search", False, _ts(2), kind="")
_conn.execute("INSERT INTO conversations (id, title, intent) VALUES (907,'缺口会话','design')")
_tool(907, "ok_tool", True, _ts(3))
_conn.execute("INSERT INTO agent_tasks (run_id, task_key, title, status, "
              "conversation_id, created_at) VALUES (1,'tk907','子任务','failed',907,?)",
              (_ts(4),))
_conn.commit()

# 坏样本台账：open×4（产任务）/ open×2（不产）/ resolved×5（不产，非 open）
_conn.execute("INSERT INTO bad_cases (kind, subject, title, severity, occurrences, "
              "status, first_seen_at, last_seen_at) VALUES "
              "('llm_call_failed','prov-a','配额失败','high',4,'open',?,?)",
              (_ts(0), _ts(5)))
_conn.execute("INSERT INTO bad_cases (kind, subject, title, severity, occurrences, "
              "status, first_seen_at, last_seen_at) VALUES "
              "('llm_call_failed','prov-b','偶发','low',2,'open',?,?)",
              (_ts(0), _ts(5)))
_conn.execute("INSERT INTO bad_cases (kind, subject, title, severity, occurrences, "
              "status, first_seen_at, last_seen_at) VALUES "
              "('user_negative_feedback','u1','已修','medium',5,'resolved',?,?)",
              (_ts(0), _ts(5)))
_conn.commit()

_ev = cu.collect_evidence(_conn)
_pats = _ev["patterns"]
_fbp = _pats.get("first_bad_patterns") or []
check("C2a collect_evidence 取到首坏模式（evil_tool + kb_search 两条）",
      len(_fbp) == 2, [(p.get("actor"), p.get("count")) for p in _fbp])
check("C2b evil_tool 模式 count=2（两会话各贡献首坏一次）",
      any(p.get("actor") == "evil_tool" and p.get("count") == 2
          for p in _pats.get("first_bad_patterns") or []),
      [(p.get("actor"), p.get("count")) for p in _pats.get("first_bad_patterns") or []])
check("C2c kind_coverage=2/6≈33.3%（空 kind 计入 unknown，不伪装）",
      _ev["kind_coverage"].get("known_kind") == 2
      and _ev["kind_coverage"].get("unknown_kind") == 4
      and abs((_ev["kind_coverage"].get("known_pct") or 0) - 33.3) < 0.05,
      _ev["kind_coverage"])
check("C2d unattributed=1（conv 907 步骤全好但结局坏）",
      _ev["unattributed_count"] == 1, _ev["unattributed_count"])
check("C2e 坏样本只取 open（resolved 不入证据）",
      len(_ev["badcases"]) == 2
      and all(b["status"] == "open" for b in _ev["badcases"]),
      [(b["kind"], b["subject"], b["status"]) for b in _ev["badcases"]])

# C3 generate：五条提案（2 弱点复发 + 1 坏样本 + 1 覆盖率 + 1 埋点）
_out = cu.generate(_conn)
check("C3a generate 新建 5 条任务提案",
      _out["created"] == 5 and len(_out["proposals"]) == 5, _out.get("created"))
_by_rule = {}
for _r in _out["proposals"]:
    _by_rule.setdefault(_r["rule"], []).append(_r)
check("C3b 四规则全部命中（weakness×2 + badcase + coverage + unattributed）",
      set(_by_rule) == {"weakness_repeat", "badcase_recurrence",
                        "kind_coverage_gap", "unattributed_gap"}
      and len(_by_rule["weakness_repeat"]) == 2,
      {k: len(v) for k, v in _by_rule.items()})
check("C3c 难度分档落库（evil_tool 2 次 low；kb_search/坏样本/覆盖率/埋点 medium）",
      {r["weakness"]: r["difficulty"] for r in _out["proposals"]}
      == {"tool_call|design|timeout|evil_tool": "low",
          "tool_call|design||kb_search": "medium",
          "llm_call_failed|prov-a": "medium",
          "kind_coverage": "medium",
          "step_coverage": "medium"},
      {r["weakness"]: r["difficulty"] for r in _out["proposals"]})
check("C3d evidence_json 可回溯（evil_tool 提案带 sample_steps）",
      any(r["weakness"] == "tool_call|design|timeout|evil_tool"
          and isinstance(r.get("evidence"), dict)
          and r["evidence"].get("count") == 2
          for r in _out["proposals"]))
check("C3e evidence_summary 摘要诚实（扫会话/模式/坏样本/覆盖率）",
      _out["evidence_summary"]["first_bad_patterns"] == 2
      and _out["evidence_summary"]["open_badcases"] == 2
      and _out["evidence_summary"]["unattributed"] == 1,
      _out["evidence_summary"])

# C4 空窗口诚实：schema 完整但零失败证据的库 ⇒ created=0，不凑数
#   （schema.init_db 无 db_path 参数且 DB_PATH import 期绑定——用 backup API
#     从干净库拷结构+种子，再清空全部失败证据行；连接直传引擎，不经 get_db）
_CLEAN_DB2 = os.path.join(_TMP, "empty.db")
if os.path.exists(_CLEAN_DB2):
    os.remove(_CLEAN_DB2)
_c_empty = sqlite3.connect(_CLEAN_DB2)
_c_empty.row_factory = sqlite3.Row
_conn.backup(_c_empty)
for _t in ("tool_call_logs", "llm_usage_stats", "flow_run_steps", "flow_runs",
           "agent_tasks", "orch_checkpoints", "bad_cases", "conversations"):
    try:
        _c_empty.execute("DELETE FROM %s" % _t)
    except sqlite3.Error:
        pass
_c_empty.commit()
try:
    _out_empty = cu.generate(_c_empty)
    check("C4a 零失败证据库 ⇒ created=0（空窗口不是「该造点任务凑数」）",
          _out_empty["created"] == 0 and _out_empty["proposals"] == [], _out_empty)
    check("C4b 空库 generate 的 evidence_summary 全零（诚实空摘要）",
          _out_empty["evidence_summary"]["sessions_scanned"] == 0
          and _out_empty["evidence_summary"]["open_badcases"] == 0
          and _out_empty["evidence_summary"]["unattributed"] == 0,
          _out_empty["evidence_summary"])
finally:
    _c_empty.close()

# C5 指纹去重：同证据二次 generate ⇒ created=0 skipped=5（含已决不重开语义预演）
_out2 = cu.generate(_conn)
check("C5a 二次 generate 同指纹 ⇒ created=0 skipped=5（不重复入队）",
      _out2["created"] == 0 and _out2["skipped"] == 5,
      (_out2.get("created"), _out2.get("skipped")))
# C5b 弱点身份进指纹的行为级证据：换一个执行体的复发 ⇒ 新任务（不互吃）
for _cid in (908, 909):
    _conn.execute("INSERT INTO conversations (id, title, intent) VALUES (?,?,?)",
                  (_cid, "另一执行体", "design"))
    _tool(_cid, "other_tool", False, _ts(6), kind="timeout")
_conn.commit()
_out3 = cu.generate(_conn)
check("C5b 新执行体复发 ⇒ 新任务入队（弱点身份进指纹，证据不互吃）",
      _out3["created"] == 1
      and _out3["proposals"][0]["weakness"] == "tool_call|design|timeout|other_tool",
      (_out3.get("created"), [p.get("weakness") for p in _out3.get("proposals") or []]))

# C6 裁决三态：pending → accepted；已决拒绝再决；rejected 同理
from repositories.curriculum_repo import CurriculumRepo          # noqa: E402
_repo = CurriculumRepo(_conn)
_first = _repo.list_proposals(status="pending", limit=1)[0]
_dec1 = cu.decide(_conn, _first["id"], "accepted", note="认领练习", decided_by="gate")
check("C6a pending → accepted 成功", _dec1.get("ok") is True, _dec1)
check("C6b 裁决留痕（reviewed_by/at/note 落库）",
      _repo.get_proposal(_first["id"])["status"] == "accepted"
      and _repo.get_proposal(_first["id"])["reviewed_by"] == "gate"
      and _repo.get_proposal(_first["id"])["review_note"] == "认领练习")
_dec2 = cu.decide(_conn, _first["id"], "rejected", note="再试", decided_by="gate")
check("C6c 已决（终态）拒绝再决", _dec2.get("ok") is False
      and "终态" in str(_dec2.get("error")), _dec2)
_dec3 = cu.decide(_conn, _first["id"], "bogus", note="", decided_by="gate")
check("C6d 非法决策值被拒（fail-loud）", _dec3.get("ok") is False, _dec3)
_dec4 = cu.decide(_conn, 999999, "accepted", note="", decided_by="gate")
check("C6e 不存在的 id ⇒ 明确报错", _dec4.get("ok") is False
      and "不存在" in str(_dec4.get("error")), _dec4)
_counts = _repo.count_by_status()
check("C6f count_by_status 分态计数（1 accepted + 其余 pending）",
      _counts.get("accepted") == 1 and _counts.get("pending") == 5, _counts)

# C7 ★repo 自带 commit（行为级）：第二连接可见（BaseRepo.execute 不 commit——
#   P1-4 在 sandbox_repo 实测踩过「无提交者 ⇒ 数据全丢」）
_c2nd = sqlite3.connect(_CLEAN_DB)
try:
    _vis = int(_c2nd.execute("SELECT COUNT(*) FROM curriculum_items "
                             "WHERE id=?", (_first["id"],)).fetchone()[0])
    _vis_st = _c2nd.execute("SELECT status FROM curriculum_items WHERE id=?",
                            (_first["id"],)).fetchone()[0]
    check("C7a 第二连接可见队列行 + 裁决状态（写方法自带 commit）",
          _vis == 1 and _vis_st == "accepted", (_vis, _vis_st))
finally:
    _c2nd.close()

# C8 ★全表快照零副作用：generate 前后除 curriculum_items 外任何表行数零变化
#   （不自动创建任务：agent_tasks/job_jobs/hil_confirmations/config_history
#     全都不许动——「只提议不自动执行」的行为级证明。快照取「真有新提案」
#     的那次 generate：新证据 third_tool 复发 ⇒ 只许队列多一行）
for _cid in (910, 911):
    _conn.execute("INSERT INTO conversations (id, title, intent) VALUES (?,?,?)",
                  (_cid, "第三执行体", "design"))
    _tool(_cid, "third_tool", False, _ts(7), kind="timeout")
_conn.commit()
_all_tables = [r[0] for r in _conn.execute(
    "SELECT name FROM sqlite_master WHERE type='table' "
    "AND name NOT LIKE 'sqlite_%'").fetchall()]
_snap_before = {t: _n_rows(_conn, t) for t in _all_tables}
_out4 = cu.generate(_conn)
_snap_after = {t: _n_rows(_conn, t) for t in _all_tables}
_dirty = {t: (_snap_before[t], _snap_after[t]) for t in _all_tables
          if _snap_before[t] != _snap_after[t]}
check("C8a ★零副作用：除 curriculum_items 外任何表行数零变化（不自动创建任务）",
      set(_dirty) <= {"curriculum_items"}, _dirty)
check("C8b 该次 generate 确实产出了新提案（third_tool 复发 ⇒ 队列 +1）",
      _out4["created"] == 1
      and _out4["proposals"][0]["weakness"] == "tool_call|design|timeout|third_tool",
      (_out4.get("created"), [p.get("weakness") for p in _out4.get("proposals") or []]))
check("C8c 再跑一次无新提案（指纹已全覆盖）", cu.generate(_conn)["created"] == 0)

# C9 端点行为级：路由注册 + 正常列表 + 冷库显式失败（P2-1 教训沿用）
from routers import curriculum as _cu_router                      # noqa: E402
_paths = {getattr(r, "path", "") for r in _cu_router.router.routes}
check("C9a GET /api/curriculum 路由已注册",
      "/api/curriculum" in _paths, sorted(_paths))
check("C9b POST /api/curriculum/generate 路由已注册",
      "/api/curriculum/generate" in _paths, sorted(_paths))
check("C9c POST /api/curriculum/{item_id}/decide 路由已注册",
      "/api/curriculum/{item_id}/decide" in _paths, sorted(_paths))
_r_list = _cu_router.list_proposals(status="", limit=10, conn=_conn)
check("C9d 端点正常返回（ok + 队列 + 分态计数 + 契约常量）",
      _r_list.get("ok") is True and len(_r_list.get("proposals") or []) == 7
      and _r_list.get("counts", {}).get("pending") == 6
      and _r_list.get("counts", {}).get("accepted") == 1
      and _r_list.get("task_kinds") == list(cu.TASK_KINDS),
      {k: _r_list.get(k) for k in ("ok", "counts")})
# 冷库（从未 init_db 的新连接）⇒ 显式 ok:False + error，不 500 不静默
_cold = sqlite3.connect(os.path.join(_TMP, "cold.db"))
try:
    try:
        _r_cold = _cu_router.list_proposals(status="", limit=10, conn=_cold)
    except Exception as _e:                # 回归形态：异常裸奔 ⇒ 记为失败而非崩掉门禁
        _r_cold = {"__raised__": repr(_e)}
    check("C9e ★冷库列表 ⇒ 显式 ok:False+error（不 500 不静默，P2-1 同型纪律）",
          isinstance(_r_cold, dict) and _r_cold.get("ok") is False
          and "curriculum_items" in str(_r_cold.get("error")), _r_cold)
    try:
        _r_cold_dec = _cu_router.decide_proposal(1, body={"decision": "accepted"},
                                                 conn=_cold, user=None)
    except Exception as _e:
        _r_cold_dec = {"__raised__": repr(_e)}
    # 裁决路径失败返 JSONResponse(500 + ok:False)——解析 body 断言语义
    _dec_body = (_r_cold_dec if isinstance(_r_cold_dec, dict)
                 else json.loads(_r_cold_dec.body))
    _dec_code = getattr(_r_cold_dec, "status_code", None)
    check("C9f ★冷库裁决 ⇒ 显式 ok:False+error（不裸 500，500 是诚实失败语义）",
          _dec_body.get("ok") is False and "error" in _dec_body
          and _dec_code == 500, (_dec_code, _dec_body))
finally:
    _cold.close()

# C10 变异自证（猴子补丁 + finally 恢复 + 复位验证）
_orig_diff = cu.difficulty_of
cu.difficulty_of = lambda n: "high"          # 变异：分档全 high
_mut = cu.propose_from_evidence(_evidence(patterns=_pat), settings=_CFG)
check("C10a 变异 difficulty_of ⇒ 提案难度随之变（口径真被消费）",
      _mut and _mut[0]["difficulty"] == "high", _mut)
finally_ok = True
cu.difficulty_of = _orig_diff
check("C10b 猴子补丁已复位", cu.difficulty_of is _orig_diff
      and cu.difficulty_of(2) == "low")
# 变异二：规则注册表摘掉一条 ⇒ 对应提案消失（注册表真是唯一产出来源）
_orig_rules = cu._RULES
try:
    cu._RULES = [r for r in _orig_rules if r.name != "unattributed_gap"]
    _mut2 = cu.propose_from_evidence(_evidence(unattr=3), settings=_CFG)
    check("C10c 变异：摘掉 unattributed_gap 规则 ⇒ 埋点任务消失（无暗壳产出）",
          _mut2 == [], _mut2)
finally:
    cu._RULES = _orig_rules
check("C10d 规则注册表已复位（四规则齐）", len(cu._RULES) == 4)

# ═══════════════════════════════════════════════════════════════
section("D 真库：只读取证证据包可取（不断言具体值——真库状态会变）")
# ═══════════════════════════════════════════════════════════════
_PROD_DB = os.path.join(ROOT, "mbse.db")
_pc = sqlite3.connect(_PROD_DB)
_pc.row_factory = sqlite3.Row
try:
    _ev_real = cu.collect_evidence(_pc)
    check("D1a 真库证据包结构完整（patterns/badcases/coverage/unattributed 四源）",
          set(_ev_real) >= {"patterns", "badcases", "kind_coverage", "unattributed_count"},
          sorted(_ev_real))
    _pr = _ev_real.get("patterns") or {}
    check("D1b patterns 子结构完整（窗口/会话/模式/覆盖率/unattributed）",
          {"window_cutoff", "sessions_scanned", "first_bad_patterns",
           "kind_coverage", "unattributed_sessions"} <= set(_pr),
          sorted(_pr))
    check("D1c 覆盖率字段自洽（bad_steps = known + unknown）",
          (lambda kc: (kc.get("known_kind") or 0) + (kc.get("unknown_kind") or 0)
           == (kc.get("bad_steps") or 0))(_ev_real.get("kind_coverage") or {}),
          _ev_real.get("kind_coverage"))
    _real_drafts = cu.propose_from_evidence(_ev_real, settings=_CFG)
    check("D2 真库证据可算出提案（数量不断言——取决于真库失败面）",
          isinstance(_real_drafts, list)
          and all(set(d) >= {"rule", "task_kind", "title", "weakness",
                             "suggestion", "difficulty", "fingerprint"}
                  for d in _real_drafts),
          "drafts=%d" % len(_real_drafts))
    # D3 真库队列只读：propose 是纯函数，不碰库（前后行数不变由 C8 已证，
    #     这里补一条真库级的「只读」证据：collect_evidence 不写任何表）
    _snap_real = {t: _n_rows(_pc, t) for t in
                  [r[0] for r in _pc.execute(
                      "SELECT name FROM sqlite_master WHERE type='table' "
                      "AND name NOT LIKE 'sqlite_%'").fetchall()]}
    cu.collect_evidence(_pc)
    _snap_real2 = {t: _n_rows(_pc, t) for t in _snap_real}
    check("D3 collect_evidence 对真库零写入（采集即读取）",
          _snap_real == _snap_real2,
          {t: (_snap_real[t], _snap_real2[t]) for t in _snap_real
           if _snap_real[t] != _snap_real2[t]})
finally:
    _pc.close()

# ═══════════════════════════════════════════════════════════════
section("E 接线：迁移三处 + 路由 + repo 边界 + 无任务创建路径 + 配置依据")
# ═══════════════════════════════════════════════════════════════
_mig_src = _read(os.path.join("database", "migrations", "misc.py"))
_mig_init = _read(os.path.join("database", "migrations", "__init__.py"))
_sch_src = _read(os.path.join("database", "schema.py"))
check("E1a 迁移函数在 misc.py", "def _migrate_curriculum_p22" in _mig_src)
check("E1b migrations/__init__ import + __all__ 双登记",
      "_migrate_curriculum_p22" in _mig_init
      and _mig_init.count("_migrate_curriculum_p22") >= 2)
check("E1c schema.py import + 调用点双登记",
      _sch_src.count("_migrate_curriculum_p22") >= 2)
# E2 repo 只碰自家表 + 两个写方法各带 commit
_repo_src = _read(os.path.join("repositories", "curriculum_repo.py"))
_repo_tables = set(re.findall(r"(?:FROM|INTO|UPDATE)\s+([a-z_]+)", _repo_src))
check("E2a repo 只碰 curriculum_items（队列是唯一落点）",
      _repo_tables <= {"curriculum_items"}, sorted(_repo_tables))
check("E2b 两个写方法（insert/decide）各带一次 commit",
      _repo_src.count("self.conn.commit()") == 2
      and "def insert_proposal" in _repo_src and "def decide_proposal" in _repo_src,
      _repo_src.count("self.conn.commit()"))
# E3 三层无任务创建路径（静态）：引擎/repo/router 不写任务表、不碰任务队列/
#   HIL——「只提议不自动执行」的结构性证明。禁令牌取**代码形态**（裸表名会
#   误伤建议文案里对证据面的正当引用，如「结局证据坏（agent_tasks…）」）。
_forbidden = ("INSERT INTO agent_tasks", "INSERT INTO job_jobs",
              "INSERT INTO flow_runs", "INSERT INTO orch_checkpoints",
              "UPDATE agent_tasks", "UPDATE job_jobs",
              "task_queue", "job_queue", "jq.submit",
              "hil_confirmations", "HILService", "hil_service")
_hits = []
for _rel in ("core/curriculum.py", "repositories/curriculum_repo.py",
             "routers/curriculum.py"):
    _s = _read(_rel)
    _s = re.sub(r'""".*?"""', "", _s, flags=re.S)          # 剥 docstring（文案可谈表名）
    _s = re.sub(r"#[^\n]*", "", _s)                        # 剥行注释（同上）
    for _f in _forbidden:
        if _f in _s:
            _hits.append("%s:%s" % (_rel, _f))
check("E3 ★三层无任务创建/执行路径（任务表写入/队列/HIL 全不碰）",
      not _hits, _hits)
# E4 路由已装配进 app（main.py include 一行 + routers/__init__ 导出）
_main_src = _read(os.path.join("main.py"))
_rinit_src = _read(os.path.join("routers", "__init__.py"))
check("E4a main.py include curriculum_router", "curriculum_router" in _main_src)
check("E4b routers/__init__ 导出 curriculum_router",
      "curriculum_router" in _rinit_src
      and _rinit_src.count("curriculum_router") >= 2)
# E5 配置注释含铁律依据（可追溯）
_cfgpy_src = _read(os.path.join("core", "config.py"))
check("E5 config.py curriculum 组注释含「只提议」铁律依据",
      "只提议" in _cfgpy_src and "curriculum" in _cfgpy_src)
check("E5b 引擎模块头含 misevolution 滑坡依据（为什么不许自动执行）",
      "misevolution" in _eng_src and "只提议" in _eng_src)

# ═══════════════════════════════════════════════════════════════
# 收尾：关连接、清临时目录
# ═══════════════════════════════════════════════════════════════
try:
    _db_cm.__exit__(None, None, None)
except Exception:
    pass

_np = sum(1 for _, ok, _ in _results if ok)
_nf = sum(1 for _, ok, _ in _results if ok is False)
_ns = sum(1 for _, ok, _ in _results if ok is None)
print("\n================================================================")
print("结果: %d PASS / %d FAIL / %d SKIP / 共 %d" % (_np, _nf, _ns, len(_results)))
if _nf:
    print("❌ 失败项：")
    for _n, _ok, _d in _results:
        if _ok is False:
            print("  - %s  ← %s" % (_n, _d))
    sys.exit(1)
print("全部通过 —— 失败驱动的任务自生成（四规则证据口径 + 弱点身份指纹 + "
      "只提议不自动执行 + 零副作用 + 端点显式失败）。")
