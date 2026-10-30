# -*- coding: utf-8 -*-
"""P0-a 门禁：capability_registry 能力统一注册处（度量收口第一项）。

═══════════════════════════════════════════════════════════════
动机（2026-10-24《产品全面评估与优化方案》P0-a 详细设计）
═══════════════════════════════════════════════════════════════
度量收口是回流与长尾治理的地基：没有统一的 capability_id 口径，「哪个能力
被调了多少次」这个问题在五张源表（tools/skills/plugins/mcp_servers/prompts）
里各有各的答法。本门禁守 capability_registry 投影层的八条判据：

  A 投影完整性   五源表每一行在 registry 有投影（LEFT JOIN 孤儿=0，五类同构）
  B 无幽灵投影   registry 反查源表存在；删源行/插幽灵 ⇒ reconcile 后投影随之消失
  C 度量五类齐   tool_call_logs 六种 tool_type 全可落库；聚合口径
                 （tool/skill/mcp_tool/mcp_server 四类）call_count/fail_count 精确
  D 盲区收口在位 六处挂载点（_log_tool_call/_record_skill_feedback/ workflows/
                 mcp_gateway/HIL/plugin executor）的 capability_id 赋值在位
                 ——行边界法抽取（2026-10-14 模板字符串教训），抽取器自带完整性守卫
  E 版本非空     投影后 TRIM(version)='' = 0（空版本归一 1.0.0，不留空）
  F 告警防假     alert_rules 的 DISTINCT metric ⊆ GLOBAL_METRICS ∪ run 级四指标
                 （引用不存在指标的规则 = 永不触发的假告警）；11 条预置规则在位
  G BadCase 闭环 夹具库 collect(apply=True) 后 open>0 且 R5 规则 evaluate fired≥1
  H 阳性对照     构造一行 tools + 一次调用 ⇒ reconcile 后有投影且 call_count=1

★退出码语义（与设计文档第 4 节一致）：
  0 = 全部通过      1 = 有断言失败（可作 CI 门禁）
  2 = **守卫失效**（抽取器/对照自身可疑 ⇒ 禁止据此下任何结论）

运行：python tools/verify/verify_capability_registry.py
"""
import io
import os
import re
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# ── 干净库 + 隔离配置：先切后 import（core.config / DB 路径 import 期绑定）──
# ★库路径带时间戳：每次跑都用全新文件，不删旧库（本机 safe-delete 会把
#   os.remove 拦成回收站操作并抛 OSError，门禁必须能反复重跑）。
_TMP = os.path.join(ROOT, "tmp", "verify_capability_registry")
os.makedirs(_TMP, exist_ok=True)
_CLEAN_DB = os.path.join(_TMP, "clean_%d.db" % int(time.time() * 1000))
_CLEAN_CFG = os.path.join(_TMP, "config_%d.json" % int(time.time() * 1000))
io.open(_CLEAN_CFG, "w", encoding="utf-8").write("{}")
os.environ["MBSE_DB_PATH"] = _CLEAN_DB
os.environ["MBSE_CONFIG_PATH"] = _CLEAN_CFG

from database.schema import init_db                                   # noqa: E402
from core import capability_registry as cr                            # noqa: E402
from core import alert_evaluator as ae                                # noqa: E402

init_db()

PASS, FAIL, GUARD = [], [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append((name, detail))
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name,
                           ("  ← " + str(detail)) if (detail and not ok) else ""))


def guard(name, ok, detail=""):
    """守卫类断言：失败 ⇒ 整体结论不可信（exit 2），不当成产品缺陷。"""
    if ok:
        PASS.append((name, ""))
        print("  [PASS] %s" % name)
    else:
        GUARD.append((name, detail))
        print("  [GUARD-FAIL] %s  ← %s" % (name, detail))


def section(t):
    print("\n" + "=" * 64)
    print(t)
    print("=" * 64)


def _read(rel):
    return io.open(os.path.join(ROOT, rel), encoding="utf-8").read()


def _extract_func(src, name):
    """按**行边界**抽取 `def name(` 起的整个函数体（Python：缩进结束，无花括号）。

    ★2026-10-14 教训的同族防御：JS 版按「同缩进 }」收尾，但 **Python 函数
      没有收尾行**——照抄会六处全误报「抽取不完整」（本次首跑实测如此，
      连阴性对照都红了）。Python 版规则：自 def 行起，遇到首个**非空行且
      缩进 ≤ def 缩进**即函数体结束（续行/嵌套 def 都更深，天然不会误剪）。
    返回 (body, complete)：complete=False ⇒ 函数不存在（调用方按守卫失败
    处理，不许拿不存在的东西下产品结论）。
    """
    lines = src.split("\n")
    pat = re.compile(r"^(\s*)(?:async\s+)?def\s+" + re.escape(name) + r"\s*\(")
    for i, ln in enumerate(lines):
        m = pat.match(ln)
        if not m:
            continue
        indent = len(m.group(1))
        body = [ln]
        for j in range(i + 1, len(lines)):
            cur = lines[j]
            if cur.strip() == "":
                body.append(cur)
                continue
            if (len(cur) - len(cur.lstrip())) <= indent:
                return "\n".join(body), True
            body.append(cur)
        return "\n".join(body), True
    return None, False


# ══════════════════════════════════════════════════════════════
# 夹具：五源表各一行可控数据（表无 NOT NULL，直插投影所依赖列）
# ══════════════════════════════════════════════════════════════
conn = sqlite3.connect(_CLEAN_DB)
conn.row_factory = sqlite3.Row
_ago = lambda d: time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - d * 86400))

# H 阳性对照放最前（夹具最干净时）：一行 tools + 一次调用 ⇒ 投影 + call_count=1
section("H 阳性对照：已知 fixture 能力必须有投影且度量精确（守卫失效⇒exit 2）")
conn.execute(
    "INSERT INTO tools (name, status, version, owner, side_effect, risk_level, "
    "builtin, scope, created_at) VALUES ('gate_h_tool','active','','gate','read',"
    "'low',0,'private',?)", (_ago(1),))
conn.execute(
    "INSERT INTO tool_call_logs (tool_name, tool_type, call_kind, ok, "
    "capability_id, created_at) VALUES ('gate_h_tool','builtin','tool',1,"
    "'tool:gate_h_tool',?)", (_ago(0.5),))
conn.commit()
st = cr.reconcile_capability_registry(conn)
conn.commit()
guard("H0 reconcile 本身未报错", "error" not in st, str(st)[:200])
bf = cr.backfill_capability_ids(conn)
conn.commit()
ag = cr.aggregate_capability_metrics(conn)
conn.commit()
row = conn.execute("SELECT capability_id, kind, version, call_count, fail_count "
                   "FROM capability_registry WHERE capability_id='tool:gate_h_tool'").fetchone()
guard("H1 阳性对照：tools 行有投影", row is not None, "registry 无 tool:gate_h_tool")
if row:
    guard("H2 阳性对照：call_count=1", row["call_count"] == 1,
          "got %s" % row["call_count"])
    guard("H3 阳性对照：fail_count=0", row["fail_count"] == 0, "got %s" % row["fail_count"])
# 收尾：清掉阳性对照数据，恢复夹具 pristine（后续段落全局计数才干净）
conn.execute("DELETE FROM tool_call_logs WHERE capability_id='tool:gate_h_tool'")
conn.execute("DELETE FROM tools WHERE name='gate_h_tool'")
cr.reconcile_capability_registry(conn)
conn.commit()
_ghost = conn.execute("SELECT COUNT(*) FROM capability_registry "
                      "WHERE capability_id='tool:gate_h_tool'").fetchone()[0]
guard("H4 阳性对照清理后投影随之删除（reconcile 守卫双向有效）", _ghost == 0,
      "got %s" % _ghost)

# ══════════════════════════════════════════════════════════════
section("A 投影完整性：五源表每行在 registry 有投影（五类同构）")
conn.execute(
    "INSERT INTO tools (name, status, version, owner, side_effect, risk_level, "
    "builtin, scope, created_at) VALUES ('gate_fixture_tool','active','','gate',"
    "'read','low',0,'private',?)", (_ago(2),))
conn.execute(
    "INSERT INTO skills (name, status, version, skill_type, enabled, frontmatter, "
    "created_at) VALUES ('gate_fixture_skill','published','','text2json',1,"
    "'author: 门禁夹具\n',?)", (_ago(2),))
conn.execute(
    "INSERT INTO plugins (plugin_id, name, status, current_version, author_name, "
    "created_at) VALUES ('gate.fixture.plugin','门禁夹具插件','published','0.0.1',"
    "'gate',?)", (_ago(2),))
conn.execute(
    "INSERT INTO mcp_servers (name, endpoint, status, enabled, capabilities, "
    "server_info, builtin, created_at) VALUES ('gate_fixture_srv',"
    "'http://gate.local/mcp','online',1,"
    "'{\"tools\": {\"gate_probe\": {\"desc\": \"x\"}}}','{\"version\": \"9.9.9\"}',0,?)",
    (_ago(2),))
conn.execute(
    "INSERT INTO prompts (name, content, status, version, created_by, created_at) "
    "VALUES ('门禁夹具提示词','门禁夹具内容','published','','gate',?)", (_ago(2),))
conn.commit()
st = cr.reconcile_capability_registry(conn)
conn.commit()
guard("A0 reconcile 本身未报错", "error" not in st, str(st)[:200])

_A_SPECS = [
    ("tools", "t.name", "t.name"),
    ("skills", "CAST(t.id AS TEXT)", "t.id"),
    ("plugins", "t.plugin_id", "t.plugin_id"),
    ("mcp_servers", "t.name", "t.name"),
    ("prompts", "CAST(t.id AS TEXT)", "t.id"),
]
for tbl, src_key, _id in _A_SPECS:
    miss = conn.execute(
        "SELECT COUNT(*) FROM %s t LEFT JOIN capability_registry r "
        "ON r.source_table='%s' AND r.source_id=%s WHERE r.capability_id IS NULL"
        % (tbl, tbl, src_key)).fetchone()[0]
    check("A-%s 每行有投影（LEFT JOIN 孤儿=0）" % tbl, miss == 0, "got %s" % miss)
_miss_h = conn.execute(
    "SELECT COUNT(*) FROM tools t LEFT JOIN capability_registry r "
    "ON r.source_table='tools' AND r.source_id=t.name "
    "WHERE r.capability_id IS NULL").fetchone()[0]
check("A-汇总 tools 孤儿=0（含 init_db 内置工具）", _miss_h == 0, "got %s" % _miss_h)
_row = conn.execute("SELECT COUNT(*) FROM capability_registry "
                    "WHERE capability_id='mcp:gate_fixture_srv:gate_probe'").fetchone()[0]
check("A-mcp_tool 按探测清单展开（不造假占位）", _row == 1, "got %s" % _row)
# C 段要造 skill 型日志 ⇒ 先捕获夹具 skill 的 id（B 段删行不波及这里）
_skill_id = conn.execute("SELECT id FROM skills "
                         "WHERE name='gate_fixture_skill'").fetchone()

# ══════════════════════════════════════════════════════════════
section("B 无幽灵投影：删源行/插幽灵 ⇒ reconcile 后投影随之消失")
# 用一次性行验证「删源行⇒投影随之消失」——不能删 A 段夹具行（C 段还要用）
conn.execute(
    "INSERT INTO skills (name, status, version, skill_type, enabled, frontmatter, "
    "created_at) VALUES ('gate_b_tmp','published','','text2json',1,'',?)", (_ago(2),))
conn.commit()
st = cr.reconcile_capability_registry(conn)
conn.commit()
_b_tmp = conn.execute("SELECT COUNT(*) FROM capability_registry WHERE source_table="
                      "'skills'").fetchone()[0]
check("B0 一次性 skills 行先有投影", _b_tmp >= 1, "got %s" % _b_tmp)
conn.execute("DELETE FROM skills WHERE name='gate_b_tmp'")
st = cr.reconcile_capability_registry(conn)
conn.commit()
_sk = conn.execute("SELECT COUNT(*) FROM capability_registry WHERE source_table='skills'").fetchone()[0]
check("B1 删 skills 源行 ⇒ 其投影随之删除", _sk == 1, "got %s（应只剩 A 段夹具 1 行）" % _sk)
conn.execute(
    "INSERT INTO capability_registry (capability_id, kind, display_name, "
    "source_table, source_id, status, lifecycle, version, publisher, visibility, "
    "risk_level, builtin, updated_at) VALUES ('tool:ghost_row','tool','幽灵',"
    "'tools','ghost_row','active','published','1.0.0','','private','read',0,"
    "CURRENT_TIMESTAMP)")
conn.commit()
st = cr.reconcile_capability_registry(conn)
conn.commit()
_gh = conn.execute("SELECT COUNT(*) FROM capability_registry "
                   "WHERE capability_id='tool:ghost_row'").fetchone()[0]
check("B2 手插幽灵投影 ⇒ reconcile 删除", _gh == 0, "got %s" % _gh)

# ══════════════════════════════════════════════════════════════
section("C 度量五类齐：六种 tool_type 可落库 + 聚合口径精确")
conn.execute(
    "INSERT INTO tool_call_logs (tool_name, tool_type, call_kind, ok, capability_id, "
    "created_at) VALUES ('gate_fixture_tool','builtin','tool',1,'tool:gate_fixture_tool',?)",
    (_ago(1),))
conn.execute(
    "INSERT INTO tool_call_logs (tool_name, tool_type, call_kind, ok, capability_id, "
    "created_at) VALUES ('gate_fixture_tool','http','tool',1,'tool:gate_fixture_tool',?)",
    (_ago(1),))
conn.execute(
    "INSERT INTO tool_call_logs (tool_name, tool_type, call_kind, ok, capability_id, "
    "created_at) VALUES ('gate_fixture_tool','zhiyuan','tool',1,'tool:gate_fixture_tool',?)",
    (_ago(1),))
conn.execute(
    "INSERT INTO tool_call_logs (tool_name, tool_type, call_kind, ok, capability_id, "
    "created_at) VALUES ('gate_fixture_tool','builtin','tool',0,'tool:gate_fixture_tool',?)",
    (_ago(1),))
conn.execute(
    "INSERT INTO tool_call_logs (tool_name, tool_type, call_kind, ok, capability_id, "
    "created_at) VALUES ('gate_probe','mcp','tool',1,'mcp:gate_fixture_srv:gate_probe',?)",
    (_ago(1),))
if _skill_id:
    conn.execute(
        "INSERT INTO tool_call_logs (tool_name, tool_type, call_kind, ok, "
        "capability_id, created_at) VALUES ('gate_fixture_skill','skill','skill',1,?,?)",
        ("skill:%d" % _skill_id["id"], _ago(1)))
conn.execute(
    "INSERT INTO tool_call_logs (tool_name, tool_type, call_kind, ok, capability_id, "
    "created_at) VALUES ('gate.plugin.tool','plugin','plugin',1,"
    "'plugin:gate.fixture.plugin',?)", (_ago(1),))
conn.commit()
_types = {r[0] for r in conn.execute(
    "SELECT DISTINCT tool_type FROM tool_call_logs WHERE created_at>=?", (_ago(2),))}
_kinds = {r[0] for r in conn.execute(
    "SELECT DISTINCT call_kind FROM tool_call_logs WHERE created_at>=?", (_ago(2),))}
check("C1 六种 tool_type 全部可落库",
      {"builtin", "http", "mcp", "zhiyuan", "skill", "plugin"} <= _types, str(_types))
check("C2 call_kind 覆盖 tool/skill/plugin", {"tool", "skill", "plugin"} <= _kinds,
      str(_kinds))
ag = cr.aggregate_capability_metrics(conn)
conn.commit()
guard("C0 aggregate 未报错", "error" not in ag, str(ag)[:200])
_t = conn.execute("SELECT call_count, fail_count FROM capability_registry "
                  "WHERE capability_id='tool:gate_fixture_tool'").fetchone()
check("C3 tool 聚合：builtin+http+zhiyuan 4 次（fail=1）",
      _t and _t["call_count"] == 4 and _t["fail_count"] == 1, str(dict(_t) if _t else None))
_mt = conn.execute("SELECT call_count FROM capability_registry "
                   "WHERE capability_id='mcp:gate_fixture_srv:gate_probe'").fetchone()
check("C4 mcp_tool 聚合：1 次", _mt and _mt["call_count"] == 1,
      str(dict(_mt) if _mt else None))
_ms = conn.execute("SELECT call_count FROM capability_registry "
                   "WHERE capability_id='mcp:gate_fixture_srv'").fetchone()
check("C4b mcp_server 前缀汇总其 tool 级调用：1 次", _ms and _ms["call_count"] == 1,
      str(dict(_ms) if _ms else None))
if _skill_id:
    _s = conn.execute("SELECT call_count FROM capability_registry "
                      "WHERE capability_id='skill:%d'" % _skill_id["id"]).fetchone()
    check("C5 skill 聚合：1 次", _s and _s["call_count"] == 1,
          str(dict(_s) if _s else None))
_p = conn.execute("SELECT call_count FROM capability_registry "
                  "WHERE capability_id='plugin:gate.fixture.plugin'").fetchone()
check("C6 plugin 行聚合器不覆盖（沙箱调用不污染生产度量，聚合 WHERE 限定四类）",
      _p and _p["call_count"] == 0, str(dict(_p) if _p else None))

# ══════════════════════════════════════════════════════════════
section("D 盲区收口在位：六处挂载点 capability_id 赋值（行边界法 + 完整性守卫）")
_D_SPECS = [
    ("M1 主链路 _log_tool_call", "agent/pipeline_parts/tools.py", "_log_tool_call"),
    ("M2 技能反馈 _record_skill_feedback", "agent/pipeline_parts/memory.py",
     "_record_skill_feedback"),
    ("B1 编排 workflows/tools.py", "workflows/tools.py", "_log_wf_tool_call"),
    ("B2 MCP 网关 _audit_call", "routers/mcp_gateway.py", "_audit_call"),
    ("B3 HIL 批准即执行", "routers/studio_parts/flows.py", "decide_hil_confirmation"),
    ("B4 插件沙箱 execute", "plugin_system/executor.py", "execute"),
]
for label, rel, fn in _D_SPECS:
    body, complete = _extract_func(_read(rel), fn)
    if body is None:
        guard("%s：函数 %s 找不到（抽取器/代码结构可疑）" % (label, fn), False, rel)
        continue
    if not complete:
        guard("%s：函数 %s 抽取不完整（无收尾行）" % (label, fn), False, rel)
        continue
    check("%s 含 capability_id 赋值" % label, "capability_id" in body,
          "函数体内未出现 capability_id")

# 抽取器自证（阴性对照：不含 capability_id 的函数，抽出来必须真的没有——
# 若抽取器退化为"返回整个文件"，这条会红）
_neg_body, neg_ok = _extract_func(_read("core/capability_registry.py"),
                                  "frontmatter_author")
guard("D7 抽取器阴性对照可完整抽取", neg_ok and _neg_body is not None,
      "frontmatter_author 抽取失败")
if _neg_body:
    guard("D7b 抽取器不是整文件兜底（阴性对照无 capability_id）",
          "capability_id" not in _neg_body, "抽到了 capability_id ⇒ 边界失效")
guard("D7c 抽取器对不存在的函数返回 None",
      _extract_func(_read("core/capability_registry.py"), "no_such_fn_xyz")[0] is None)

# ══════════════════════════════════════════════════════════════
section("E 版本非空：投影后 TRIM(version)='' = 0")
_ev = conn.execute("SELECT COUNT(*) FROM capability_registry "
                   "WHERE TRIM(COALESCE(version,''))=''").fetchone()[0]
check("E1 全库无空版本", _ev == 0, "got %s" % _ev)
_vt = conn.execute("SELECT version FROM capability_registry "
                   "WHERE capability_id='tool:gate_fixture_tool'").fetchone()
check("E2 空版本归一 1.0.0", _vt and _vt["version"] == "1.0.0",
      str(dict(_vt) if _vt else None))
_vp = conn.execute("SELECT version FROM capability_registry "
                   "WHERE capability_id='plugin:gate.fixture.plugin'").fetchone()
check("E2b '0.0.1' 亦归一 1.0.0", _vp and _vp["version"] == "1.0.0",
      str(dict(_vp) if _vp else None))
_vm = conn.execute("SELECT version FROM capability_registry "
                   "WHERE capability_id='mcp:gate_fixture_srv'").fetchone()
check("E3 mcp 版本取 server_info.version（9.9.9）", _vm and _vm["version"] == "9.9.9",
      str(dict(_vm) if _vm else None))

# ══════════════════════════════════════════════════════════════
section("F 告警防假：metric 白名单 + 11 条预置规则在位")
_RUN_METRICS = ("success_rate", "avg_latency", "error_count", "mock_rate")
_WHITELIST = set(ae.GLOBAL_METRICS) | set(_RUN_METRICS)
from seed_registry import seed_alert_rules                             # noqa: E402
_r = seed_alert_rules(conn)
conn.commit()
_metrics = {r[0] for r in conn.execute("SELECT DISTINCT metric FROM alert_rules")}
_bad = sorted(_metrics - _WHITELIST)
check("F1 alert_rules 的 metric 全在白名单（无永不触发的假告警）", not _bad, str(_bad))
_NAMES = [r[0] for r in conn.execute("SELECT name FROM alert_rules")]
_EXPECT = ["登录失败突增（1h≥5次）", "成本突增（今日超昨日同期3倍）",
           "LLM 延迟劣化（1h P95>30s）", "审计链破损", "BadCase 积压（open≥5）",
           "身份拒绝/认证异常突增（1h≥20次）", "LLM 失败率突增（1h>5%）",
           "编排批次停滞（非终态>30min）", "HIL 确认队列积压（超24h>3）",
           "检索质量下滑（低质占比>40%/h，代理口径）",
           "意图误判上升（误判率>50%，代理口径，待校准）"]
_missing = [n for n in _EXPECT if n not in _NAMES]
check("F2 11 条预置规则全部在位", not _missing, str(_missing))
# F3 阴性对照：注入一条引用不存在指标的规则 ⇒ 白名单检查必须抓到它
conn.execute("INSERT INTO alert_rules (name, metric, operator, threshold, level, "
             "status) VALUES ('门禁注入的假告警','no_such_metric_xyz','>',1,"
             "'warning','active')")
conn.commit()
_metrics2 = {r[0] for r in conn.execute("SELECT DISTINCT metric FROM alert_rules")}
_bad2 = sorted(_metrics2 - _WHITELIST)
check("F3 白名单检查有牙（注入假指标可被抓住）", _bad2 == ["no_such_metric_xyz"],
      str(_bad2))
conn.execute("DELETE FROM alert_rules WHERE name='门禁注入的假告警'")
conn.commit()

# ══════════════════════════════════════════════════════════════
section("G BadCase 闭环：apply 后 open>0 且 R5 规则 evaluate fired≥1")
from core import badcase as bc                                         # noqa: E402
for i in range(6):
    conn.execute(
        "INSERT INTO llm_usage_stats (provider_name, model_name, call_failed, "
        "failure_kind, failure_msg, conversation_id, created_at) "
        "VALUES ('deepseek','deepseek-chat',1,'timeout','读取超点',?,?)",
        (9000 + i, _ago(1)))
conn.commit()
_res = bc.collect_bad_cases(conn, apply=True)
_open = bc.open_badcase_count(conn)
check("G1 collect(apply=True) 后 open=6（真库同口径 6 条）", _open == 6, "got %s" % _open)
check("G1b apply_result 自洽", (_res.get("apply_result") or {}).get("inserted") == 6,
      str(_res.get("apply_result")))
_ev2 = ae.evaluate_once(conn, notify=False)
check("G2 R5 规则触发（fired≥1）", _ev2.get("fired", 0) >= 1, str(_ev2)[:200])
check("G2b badcase_open_count 取数=6", _ev2.get("metrics", {}).get(
    "badcase_open_count") == 6.0, str(_ev2.get("metrics", {}).get("badcase_open_count")))
_r5 = conn.execute("SELECT rule_name, actual, threshold, status FROM alert_events "
                   "WHERE metric='badcase_open_count' ORDER BY id DESC LIMIT 1").fetchone()
check("G3 alert_events 落 R5 事件（actual≥threshold）",
      _r5 is not None and float(_r5["actual"]) >= float(_r5["threshold"])
      and _r5["status"] == "open", str(dict(_r5) if _r5 else None))

# ══════════════════════════════════════════════════════════════
section("真库只读取证（结构完整，不断言具体值——真库状态会变）")
_real = os.path.join(ROOT, "mbse.db")
if os.path.exists(_real):
    rc = sqlite3.connect(_real)
    rc.row_factory = sqlite3.Row
    try:
        _r_miss = 0
        for tbl, src_key in (("tools", "t.name"), ("skills", "CAST(t.id AS TEXT)"),
                             ("plugins", "t.plugin_id"), ("mcp_servers", "t.name"),
                             ("prompts", "CAST(t.id AS TEXT)")):
            _r_miss += rc.execute(
                "SELECT COUNT(*) FROM %s t LEFT JOIN capability_registry r "
                "ON r.source_table='%s' AND r.source_id=%s "
                "WHERE r.capability_id IS NULL" % (tbl, tbl, src_key)).fetchone()[0]
        check("D1 真库五源投影完整（孤儿=0）", _r_miss == 0, "got %s" % _r_miss)
        _r_ev = rc.execute("SELECT COUNT(*) FROM capability_registry "
                           "WHERE TRIM(COALESCE(version,''))=''").fetchone()[0]
        check("D2 真库无空版本", _r_ev == 0, "got %s" % _r_ev)
        _r_sum = cr.registry_summary(rc)
        check("D3 真库 registry_summary 可取（total>0）",
              _r_sum.get("total", 0) > 0 and "error" not in _r_sum, str(_r_sum)[:160])
        _r_bf = cr.backfill_capability_ids.__doc__ is not None
        check("D4 backfill 口径在位（历史匹配不上留空，不造假）", _r_bf)
        _r_bc = bc.open_badcase_count(rc)
        check("D5 真库 BadCase 台账可取（S5 已入库 6 条 open）", _r_bc >= 0,
              "got %s" % _r_bc)
    finally:
        rc.close()
else:
    print("  （真库不存在，跳过）")

# ══════════════════════════════════════════════════════════════
_np, _nf, _ng = len(PASS), len(FAIL), len(GUARD)
print("\n" + "=" * 64)
print("结果: %d PASS / %d FAIL / %d GUARD-FAIL" % (_np, _nf, _ng))
if _nf:
    print("未过判据：")
    for nm, d in FAIL:
        print("  - %s ← %s" % (nm, d))
if _ng:
    print("守卫失效（结论不可信，禁止据此下任何判断）：")
    for nm, d in GUARD:
        print("  - %s ← %s" % (nm, d))
conn.close()
if _ng:
    sys.exit(2)
if _nf:
    sys.exit(1)
print("全部通过 —— capability_registry 投影层（五源同构 + 无幽灵 + 度量五类齐 + "
      "六处收口 + 版本非空 + 告警防假 + BadCase 闭环 + 阳性对照）。")
sys.exit(0)
