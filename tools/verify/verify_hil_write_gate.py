# -*- coding: utf-8 -*-
"""★ 写前确认（HITL）统一闸的**离线闭环测试** —— 不联网、不调 LLM、不起服务。

只测「统一闸的判定与入队行为」这一层：
  · 放行/拦截判定是否正确（按策略档位）
  · 破坏性 MCP 工具**真的被拦**（改动前是零拦截）
  · 批次合并是否生效（同批只 1 张卡，batch_count 累加）
  · 执行幂等是否生效（同一确认单只执行一次）
  · 乐观锁是否生效（坐标不一致时不执行）
  · 回灌文案是否含"不要重复发起"与"原请求复述"

用法： ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_hil_write_gate.py
"""
import json
import os
import sqlite3
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)
os.chdir(REPO)

PASS = 0
FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] %s" % label)
    else:
        FAIL += 1
        print("  [FAIL] %s | %s" % (label, detail))


def section(t):
    print("\n=== %s ===" % t)


# ── 用**独立临时库**跑，绝不污染生产库 ──
TMP_DB = os.path.join(REPO, "tmp", "hil_gate_test.db")
os.makedirs(os.path.dirname(TMP_DB), exist_ok=True)
if os.path.exists(TMP_DB):
    os.remove(TMP_DB)
_src = sqlite3.connect("file:mbse.db?mode=ro", uri=True)
_dst = sqlite3.connect(TMP_DB)
_src.backup(_dst)
_src.close()
_dst.close()

os.environ["MBSE_DB_PATH"] = TMP_DB
os.environ["MBSE_DB"] = TMP_DB

from agent.pipeline import AgentPipeline          # noqa: E402
from hil_service import (HILService, classify_action, build_resume_text,  # noqa: E402
                         exec_idem_key)
from agent.mcp_tool_meta import mcp_effect_level  # noqa: E402
from database import db_conn                      # noqa: E402


def _pipe():
    p = AgentPipeline()
    p._load_db_agents()
    p._tool_user = {"id": 1, "role_name": "系统管理员", "username": "tester"}
    p._tool_intent_ctx = {"intent": "智源对接agent"}
    p._tool_agent_ctx = {"agent": "智源对接agent"}
    p._tool_conv_ctx = 0
    p._tool_whitelist = None
    p._tool_perm_gate_disabled = True     # 本测试只测闸，不测 RBAC
    return p


def _gate(pipe, name, args, batch=None):
    """直接调统一闸（跳过白名单/Hook/RBAC 那些前置闸）。"""
    pipe._hil_batch_override = batch
    return pipe._hil_gate(name, args, pipe._tool_agent_ctx, pipe._tool_conv_ctx, "mcp")


# ══════════════════════════════════════════════════════════════════════
section("A. 写类分类（双源）")
# ══════════════════════════════════════════════════════════════════════
CASES = [
    # (工具名, 期望 kind, 期望 match_key)
    ("query_projects", "read", "read"),
    ("get_model_delete_impact", "read", "read"),     # 名字含 delete 但实为只读
    ("preview_model_changes", "read", "read"),
    ("export_sysml_v2", "read", "read"),
    ("create_model_elements", "write", "mcp_write"),
    ("set_model_expression", "write", "mcp_write"),   # 改动前是漏判
    ("update_model_elements", "write", "mcp_write"),
    ("delete_model_elements", "destructive", "mcp_destructive"),
    ("remove_view_presentations", "destructive", "mcp_destructive"),
    ("import_sysml_text", "destructive", "mcp_destructive"),
    ("apply_model_changes", "destructive", "mcp_destructive"),
    ("undo_model_operation", "destructive", "mcp_destructive"),
]
for nm, ek, em in CASES:
    c = classify_action(nm, "")     # side_effect 空 ⇒ 走 MCP 兜底
    check("[A] %-30s ⇒ %s / %s" % (nm, ek, em),
          c["kind"] == ek and c["match_key"] == em,
          "实得 %s / %s" % (c["kind"], c["match_key"]))

c = classify_action("file_write", "write")
check("[A] 本地 side_effect=write ⇒ match_key=write（本地优先于 MCP 判据）",
      c["match_key"] == "write", str(c))

# ══════════════════════════════════════════════════════════════════════
section("B. 统一闸判定（按策略档位）")
# ══════════════════════════════════════════════════════════════════════
p = _pipe()
r = p._hil_gate("query_projects", {}, p._tool_agent_ctx, 0, "mcp")
check("[B] 只读工具 ⇒ 放行（返回 None）", r is None, str(r)[:120])

r = p._hil_gate("create_model_elements", {"name": "测试包"},
                p._tool_agent_ctx, 0, "mcp")
check("[B] 可逆写（档1=notify）⇒ 放行", r is None, str(r)[:160])

r = p._hil_gate("delete_model_elements", {"name": "旧包"},
                p._tool_agent_ctx, 0, "mcp")
check("[B] ★ 破坏性写 ⇒ **拦截**（改动前是零拦截）", r is not None, str(r)[:120])
check("[B] 拦截信号带 confirm_required",
      bool(r and r.get("confirm_required")), str(r)[:120])
check("[B] 确认单带完整参数快照",
      bool(r and r.get("confirm", {}).get("arguments", {}).get("name") == "旧包"),
      str(r)[:200])
check("[B] 确认单带影响面摘要（不是裸 JSON）",
      bool(r and "旧包" in (r.get("confirm", {}).get("preview") or "")),
      str(r.get("confirm", {}).get("preview"))[:120] if r else "")

# ══════════════════════════════════════════════════════════════════════
section("C. 批次合并（approval fatigue 防线）")
# ══════════════════════════════════════════════════════════════════════
with db_conn() as c:
    _before = c.execute("SELECT COUNT(*) FROM hil_confirmations").fetchone()[0]

p2 = _pipe()
p2._tool_trace_id = "trace-A"
r1 = p2._hil_gate("create_model_elements", {"name": "包1"},
                  p2._tool_agent_ctx, 0, "mcp")
p2._tool_trace_id = "trace-A"
# 可逆写在档1是 notify ⇒ 不会入队；用**破坏族**测批次（它确实入队）
p3 = _pipe()
p3._tool_trace_id = "trace-B"
a = p3._hil_gate("delete_model_elements", {"name": "X1"},
                 p3._tool_agent_ctx, 0, "mcp")
cid_a = int((a or {}).get("confirm", {}).get("id") or 0)
p3._tool_trace_id = "trace-B"
b = p3._hil_gate("delete_model_relationships", {"name": "X2"},
                 p3._tool_agent_ctx, 0, "mcp")
cid_b = int((b or {}).get("confirm", {}).get("id") or 0)
check("[C] 同批次同族 ⇒ 合并到同一张确认单", cid_a and cid_a == cid_b,
      "cid_a=%s cid_b=%s" % (cid_a, cid_b))
check("[C] 合并标记 batch_merged=True",
      bool((b or {}).get("confirm", {}).get("batch_merged")), str(b)[:160])

with db_conn() as c:
    row = c.execute("SELECT batch_count, batch_items FROM hil_confirmations WHERE id=?",
                     (cid_a,)).fetchone()
check("[C] batch_count 累加为 2", row and int(row["batch_count"]) == 2,
      str(dict(row)) if row else "无行")
items = json.loads((row or {})["batch_items"] or "[]") if row else []
check("[C] batch_items 累积了 2 条受影响对象（计划级摘要用）", len(items) == 2,
      str(items)[:200])

# ══════════════════════════════════════════════════════════════════════
section("D. 乐观锁（坐标过期时不执行）")
# ══════════════════════════════════════════════════════════════════════
# ⚠️ 用例必须带**嵌套** version 坐标 —— 智源 MCP 的真实 payload 形态是
#   {"version": {"projectId":..,"branchId":..,"versionId":..}, ...}
#   只放顶层 name 时 `_extract_version` 返回空 ⇒ 乐观锁按设计跳过 ⇒ 测不出东西。
from hil_service import _extract_version           # noqa: E402
_NESTED = {"name": "X", "version": {"projectId": "P1", "branchId": "B1", "versionId": "V-REAL"}}
check("[D] 递归提取嵌套 versionId（智源真实 payload 形态）",
      _extract_version(_NESTED) == "V-REAL", _extract_version(_NESTED))
check("[D] 顶层 versionId 也能提取",
      _extract_version({"versionId": "V-TOP"}) == "V-TOP")

with db_conn() as c:
    _cid_lk = int(HILService.queue_confirmation(
        c, "智源对接agent", "delete_model_elements", _NESTED,
        preview="删除「X」", tool_type="mcp", level="confirm")["id"])
with db_conn() as c:
    r = HILService.decide(c, _cid_lk, True, "tester", expected_version="999999")
check("[D] 坐标不一致 ⇒ version_mismatch（不执行）",
      r.get("status") == "version_mismatch", str(r.get("status")))
with db_conn() as c:
    st = c.execute("SELECT status FROM hil_confirmations WHERE id=?",
                   (_cid_lk,)).fetchone()["status"]
check("[D] 不一致时确认单保持 pending（未被批掉）", st == "pending", st)
# 坐标一致 ⇒ 正常批准
with db_conn() as c:
    r2 = HILService.decide(c, _cid_lk, True, "tester", expected_version="V-REAL")
check("[D] 坐标一致 ⇒ 正常批准", r2.get("status") == "approved", str(r2.get("status")))
# 并发/重复决策 ⇒ 条件更新挡住（不重复执行）
with db_conn() as c:
    r3 = HILService.decide(c, _cid_lk, True, "tester")
check("[D] 重复决策 ⇒ 返回现状（条件更新防并发双批）",
      r3.get("status") == "approved" and not r3.get("raced") or r3.get("raced") is True,
      str(r3.get("status")))

# ══════════════════════════════════════════════════════════════════════
section("E. 执行幂等（点两下批准只执行一次）")
# ══════════════════════════════════════════════════════════════════════
k1 = exec_idem_key(42, {"a": 1})
k2 = exec_idem_key(42, {"a": 1})
k3 = exec_idem_key(43, {"a": 1})
check("[E] 同一确认单同参数 ⇒ 同幂等键", k1 == k2)
check("[E] 不同确认单 ⇒ 不同幂等键", k1 != k3)

# ══════════════════════════════════════════════════════════════════════
section("F. 回灌文案（D-2：让模型知道'已经执行了'）")
# ══════════════════════════════════════════════════════════════════════
t = build_resume_text("create_model_elements",
                       {"ok": True, "result": '{"status":"SUCCEEDED","dataId":"123"}'},
                       True, original_input="在通信子系统用例图包下新建一个子包")
check("[F] 含'已获用户批准并执行'", "已获用户" in t, t[:120])
check("[F] 含真实执行回执", "SUCCEEDED" in t, t[:200])
check("[F] ★ 含'不要重复发起'（否则模型会重发 ⇒ 重复包）",
      "不要重复发起" in t, t[:200])
check("[F] ★ 含原请求复述（messages 表不存 tool_calls ⇒ 上下文会丢）",
      "原请求" in t, t[:200])

t2 = build_resume_text("delete_model_elements", {}, False, reason="误操作")
check("[F] 拒绝分支含'拒绝'", "拒绝" in t2, t2[:120])
check("[F] 拒绝分支含'不要重试'", "不要重试" in t2, t2[:160])

# ══════════════════════════════════════════════════════════════════════
section("G. 策略表已接线（hil_policy 不再是'不接链路'）")
# ══════════════════════════════════════════════════════════════════════
src = open(os.path.join(REPO, "services/hil_policy.py"), encoding="utf-8").read()
check("[G] hil_policy 模块注释已更新（不再是'不接链路'）",
      "不接链路" not in src or "已接" in src or "2026-10-28" in src,
      "模块头注释未更新")
with db_conn() as c:
    n = c.execute(
        "SELECT COUNT(*) FROM hil_policies WHERE match_key IN ('mcp_write','mcp_destructive')"
    ).fetchone()[0]
check("[G] MCP 两条档位已播种（智源单独一档 = D-4）", n == 2, "实得 %d" % n)

# ══════════════════════════════════════════════════════════════════════
section("H. 建表 SQL 语法（py_compile 查不出来，必须真跑一次）")
# ══════════════════════════════════════════════════════════════════════
# ⚠️ 2026-10-28 实测踩到：schema.py 里补列时**漏了一个逗号**（`idem_key TEXT DEFAULT ''`
#   后面直接接下一列）⇒ `sqlite3.OperationalError: near "tool_type": syntax error`
#   ⇒ **服务起不来**。而 `py_compile` 完全查不出（SQL 只是字符串）。
#   ⇒ 必须拿内存库真跑一遍建表语句。
_schema_src = open(os.path.join(REPO, "database/schema.py"), encoding="utf-8").read()
_i = _schema_src.find("CREATE TABLE IF NOT EXISTS hil_confirmations")
_j = _schema_src.find(')""")', _i)
_sql = _schema_src[_i:_j + 1]
_mem = sqlite3.connect(":memory:")
try:
    _mem.execute(_sql)
    _cols = [r[1] for r in _mem.execute("PRAGMA table_info(hil_confirmations)")]
    check("[H] hil_confirmations 建表 SQL 语法正确（内存库真跑）",
          True, "")
    # task_key 由迁移补（不在建表语句里，符合预期）
    _need = ["tool_type", "tool_call_id", "level", "batch_id", "batch_count",
             "timeout_at", "result_json", "exec_idem_key", "batch_items"]
    _miss = [n for n in _need if n not in _cols]
    check("[H] 建表语句含全部 9 个新列", not _miss, "缺=%s" % _miss)
except Exception as _se:
    check("[H] hil_confirmations 建表 SQL 语法正确（内存库真跑）", False,
          "%s: %s" % (type(_se).__name__, str(_se)[:160]))
finally:
    _mem.close()

# 迁移模块同样必须能跑（老库升级路径）
try:
    from database.migrations.columns import _migrate_columns  # noqa: E402
    _mem2 = sqlite3.connect(":memory:")
    _mem2.execute("""CREATE TABLE hil_confirmations (
        id INTEGER PRIMARY KEY AUTOINCREMENT, agent_id TEXT DEFAULT '',
        conversation_id INTEGER DEFAULT 0, run_id INTEGER DEFAULT 0,
        action TEXT NOT NULL, payload TEXT DEFAULT '{}', preview TEXT DEFAULT '',
        status TEXT DEFAULT 'pending', decided_by TEXT DEFAULT '',
        decided_at TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
    _migrate_columns(_mem2)
    _c2 = [r[1] for r in _mem2.execute("PRAGMA table_info(hil_confirmations)")]
    _miss2 = [n for n in _need if n not in _c2]
    check("[H] 老库（13 列）走迁移后补齐 9 列", not _miss2, "缺=%s" % _miss2)
    _mem2.close()
except Exception as _me:
    check("[H] 老库（13 列）走迁移后补齐 9 列", False,
          "%s: %s" % (type(_me).__name__, str(_me)[:160]))

# ══════════════════════════════════════════════════════════════════════
section("I. 策略 conditions 键必须能在 context 里找到（否则静默放行）")
# ══════════════════════════════════════════════════════════════════════
# ⚠️ 2026-10-28 真机踩到的**最危险的坑**：
#   种子写 `conditions: {"tool_family": "mcp_write"}`，而 `_hil_gate` 传的 context 里
#   **没有 `tool_family`** ⇒ `hil_policy._match_conditions` 判定"缺字段 = 不命中"
#   ⇒ 候选为空 ⇒ `evaluate` 走 `no_policy` 分支**返回 auto 放行**
#   ⇒ **破坏性操作零拦截**，而**日志里一切正常**（策略表有行、裁决器不报错）。
#   ⇒ 必须断言：种子里每个 conditions 的键，都能在 _hil_gate 的 context 里找到。
import re as _re
_gate_src = open(os.path.join(REPO, "agent/pipeline_parts/tools.py"), encoding="utf-8").read()
_gi = _gate_src.find("def _hil_gate")
# ⚠️ 边界要取 `_hil_gate` **之后**的下一个 `    def `（不能取 `_build_tools_def` ——
#   它在 `_hil_gate` **之前**，那样切片是空串 ⇒ 两个断言假红。2026-10-28 踩到。）
_gj = -1
for _m in _re.finditer(r"\n    def ", _gate_src[_gi + 10:]):
    _gj = _gi + 10 + _m.start()
    break
_gate_seg = _gate_src[_gi:_gj] if _gj > _gi else _gate_src[_gi:]
check("[I] _hil_gate 代码段提取完整（非空且含 evaluate）",
      bool(_gate_seg) and "evaluate" in _gate_seg,
      "len=%d" % len(_gate_seg))
# 从 _hil_gate 里抽出 evaluate(...) 的 context={...} 字典键
_ctx_keys = set(_re.findall(r'"([a-z_]+)":', _gate_seg.split("context={")[1].split("}")[0])) \
    if "context={" in _gate_seg else set()
check("[I] 能从 _hil_gate 源码提取 context 键", bool(_ctx_keys), str(_ctx_keys))

_seed = json.load(open(os.path.join(REPO, "database/seeds_data/hil_policies.json"),
                       encoding="utf-8"))
_seed_cond_keys = set()
for _p in (_seed.get("policies") or []):
    if str(_p.get("decision_point") or "") == "tool_write":
        for _k in (_p.get("conditions") or {}):
            _seed_cond_keys.add(_k)
_miss_keys = sorted(_seed_cond_keys - _ctx_keys)
check("[I] ★ tool_write 策略的 conditions 键全在 context 里（缺则静默放行）",
      not _miss_keys,
      "缺失=%s（context 只有 %s）" % (_miss_keys, sorted(_ctx_keys)))

# 行为级复核：拿真实 match_key 跑一次 evaluate，确认**不会**落到 no_policy/auto
from services.hil_policy import load_policies as _lp2, evaluate as _ev2  # noqa: E402
with db_conn() as _c9:
    _pols9 = _lp2(_c9)
for _mk, _kind, _want_not in (("mcp_destructive", "destructive", "auto"),
                              ("mcp_write", "write", None),
                              ("read", "read", "confirm")):
    _d9 = _ev2(_pols9, decision_point="tool_write", match_key=_mk,
               context={"side_effect": _kind, "tool": "x",
                        "tool_family": _mk, "tool_type": "mcp"})
    if _want_not:
        check("[I] evaluate(%s) 不返回 %s" % (_mk, _want_not),
              _d9.get("level") != _want_not,
              "level=%s matched_by=%s" % (_d9.get("level"), _d9.get("matched_by")))
    else:
        check("[I] evaluate(%s) 命中策略（非 no_policy）" % _mk,
              _d9.get("matched_by") == "policy",
              "level=%s matched_by=%s" % (_d9.get("level"), _d9.get("matched_by")))

# ══════════════════════════════════════════════════════════════════════
section("J. 批次合并的**可执行性**（2026-10-28 真机清库暴露的两个缺陷）")
# ══════════════════════════════════════════════════════════════════════
# 背景（真机实测，清理 3 个平台测试包时暴露）：
#   X1 批次合并只合"计数"：`payload` 只存主单那 1 笔，`batch_items` 只存
#      {tool, preview, target} **没有完整参数** ⇒ 批准后第 2..N 笔**永不执行**，
#      而用户以为"批准了 N 项" ⇒ **静默少做**（数据丢失型）。
#   X2 `_batch_target` 不认 `dataId` ⇒ 删除类摘要渲染成「删除「（未命名对象）」× N」
#      ⇒ 用户**盲批**（OWASP ASI09："UI conceals the exact effect"）。
#   ⇒ 三条断言必须钉死。
from hil_service import _batch_target as _bt  # noqa: E402

# [J1] X2：只有 dataId 的删除类参数，摘要必须有信息量
for _args, _desc in (
        ({"dataId": "2115079578924417024", "requestId": "r1", "workspace": {"versionId": "v"}},
         "delete 类（只有 dataId）"),
        ({"dataId": "2115077874346688512"}, "delete 类（最小参数）"),
        ({"name": "通信子系统测试"}, "create 类（有 name）"),
        ({"elementIds": ["a", "b", "c"]}, "列表型"),
):
    _t = _bt(_args)
    check("[J1] _batch_target(%s) 非空" % _desc, bool(_t), repr(_t))

# [J2] X1：queue_confirmation 必须把**完整 args** 存进 batch_items（源码断言）
_hs_src = open(os.path.join(REPO, "hil_service.py"), encoding="utf-8").read()
check("[J2] batch_items 条目含完整 args（首条新建路径）",
      '"args": payload or {}' in _hs_src)
check("[J2] batch_items 条目含完整 args（批次合并路径）",
      _hs_src.count('"args": payload or {}') >= 2,
      "出现 %d 次" % _hs_src.count('"args": payload or {}'))
check("[J3] execute_confirmation 逐条执行批次项（不是只跑主 payload）",
      "_todo" in _hs_src and "_batch_items" not in _hs_src
      and 'json.loads(_bi)' in _hs_src,
      "未找到 batch_items 解析逻辑")
check("[J3] 批次执行结果逐项汇总（seq/tool/target/ok）",
      all(k in _hs_src for k in ('"seq"', '"target"', '"ok": _ok1')))

# [J4] 行为级：mock 掉 _exec_tool_call，验证 batch_items 的**每一项**都被执行
_mock_calls = []
try:
    _orig_exec = AgentPipeline._exec_tool_call

    def _fake_exec(self, name, arguments):
        _mock_calls.append({"tool": name, "did": (arguments or {}).get("dataId")})
        return {"ok": True, "result": "MOCK-OK %s" % (arguments or {}).get("dataId")}

    AgentPipeline._exec_tool_call = _fake_exec
    from hil_service import execute_confirmation as _ec
    with db_conn() as _c_j:
        _conf_j = HILService.queue_confirmation(
            _c_j, "测试Agent", "delete_model_elements",
            {"dataId": "D-AAA", "requestId": "j", "workspace": {"versionId": "v"}},
            preview="删除 D-AAA", tool_type="mcp", level="confirm",
            batch_id="j-batch")
        _cjid = int(_conf_j["id"])
        # 手工追加 2 条（模拟批次合并后的状态；带 args 才可执行）
        import json as _j
        _items = [
            {"tool": "delete_model_elements", "target": "#D-AAA",
             "args": {"dataId": "D-AAA", "requestId": "j1", "workspace": {"versionId": "v"}}},
            {"tool": "delete_model_elements", "target": "#D-BBB",
             "args": {"dataId": "D-BBB", "requestId": "j2", "workspace": {"versionId": "v"}}},
            {"tool": "delete_model_elements", "target": "#D-CCC",
             "args": {"dataId": "D-CCC", "requestId": "j3", "workspace": {"versionId": "v"}}},
        ]
        _c_j.execute("UPDATE hil_confirmations SET batch_count=3, batch_items=? WHERE id=?",
                     (_j.dumps(_items, ensure_ascii=False), _cjid))
        _c_j.commit()
        # 置为 approved（模拟用户已批准）
        _c_j.execute("UPDATE hil_confirmations SET status='approved' WHERE id=?", (_cjid,))
        _c_j.commit()
        _conf_full = HILService.get(_c_j, _cjid)
        _res_j = _ec(_c_j, _conf_full)
    check("[J4] ★ 批次 3 项**全部被执行**（X1 的核心）",
          len(_mock_calls) == 3, "实际执行 %d 次: %s" % (len(_mock_calls),
                                                    [x["did"] for x in _mock_calls]))
    check("[J4] 执行的是各自的参数（不是重复跑主 payload）",
          [x["did"] for x in _mock_calls] == ["D-AAA", "D-BBB", "D-CCC"],
          str([x["did"] for x in _mock_calls]))
    check("[J4] 汇总回执含批次成功计数", "3/3" in str(_res_j.get("result") or ""),
          str(_res_j.get("result") or "")[:120])
    check("[J4] ok=True（全部成功）", bool(_res_j.get("ok")), str(_res_j)[:160])
    # 执行幂等：再执行一次不应重复调用
    _before_n = len(_mock_calls)
    with db_conn() as _c_j2:
        _res_j2 = _ec(_c_j2, HILService.get(_c_j2, _cjid) or {})
    check("[J4] 重复执行被幂等挡住（不重复调用）",
          len(_mock_calls) == _before_n, "又多执行了 %d 次" % (len(_mock_calls) - _before_n))
    AgentPipeline._exec_tool_call = _orig_exec
except Exception as _je:
    try:
        AgentPipeline._exec_tool_call = _orig_exec
    except Exception:
        pass
    check("[J4] 批次逐条执行行为级验证", False,
          "%s: %s" % (type(_je).__name__, str(_je)[:200]))

# [J5] ★ 端到端闭环：**走真实生产路径**（queue_confirmation 入队两次 → 执行）
#   为什么必须有这一条：J4 是**手工构造** batch_items（带 args），
#   它只证明"执行器会跑 batch_items 里的所有项"，**证明不了"入队时真的存了 args"**。
#   实测：把 queue_confirmation 的 args 去掉，J2（源码断言）会红，但 J4 仍绿
#   ⇒ 缺这一条就有盲区（"存"与"跑"两个环节必须各有一条行为断言）。
try:
    _calls2 = []
    _orig_exec2 = AgentPipeline._exec_tool_call

    def _fake2(self, name, arguments):
        _calls2.append((arguments or {}).get("dataId"))
        return {"ok": True, "result": "OK-%s" % (arguments or {}).get("dataId")}

    AgentPipeline._exec_tool_call = _fake2
    from hil_service import execute_confirmation as _ec2
    with db_conn() as _c5:
        _b_id = "j5-%s" % id(_calls2)
        _r1 = HILService.queue_confirmation(
            _c5, "测试Agent", "delete_model_elements",
            {"dataId": "J5-AAA", "requestId": "j5a", "workspace": {"versionId": "v"}},
            preview="删除 A", tool_type="mcp", level="confirm", batch_id=_b_id)
        _r2 = HILService.queue_confirmation(
            _c5, "测试Agent", "delete_model_elements",
            {"dataId": "J5-BBB", "requestId": "j5b", "workspace": {"versionId": "v"}},
            preview="删除 B", tool_type="mcp", level="confirm", batch_id=_b_id)
        _cid5 = int(_r2.get("id") or 0)
        _row5 = HILService.get(_c5, _cid5) or {}
        _items5 = _row5.get("batch_items") or []
        if isinstance(_items5, str):
            _items5 = json.loads(_items5)
        _c5.execute("UPDATE hil_confirmations SET status='approved' WHERE id=?", (_cid5,))
        _c5.commit()
        _res5 = _ec2(_c5, HILService.get(_c5, _cid5) or {})
    check("[J5] ★ 生产路径入队后 batch_items 达 2 条", len(_items5) == 2,
          "实际 %d 条" % len(_items5))
    check("[J5] ★ 生产路径入队的每一条都带完整 args（X1 的入队侧）",
          len(_items5) == 2 and all((it.get("args") or {}).get("dataId") for it in _items5),
          str([bool(it.get("args")) for it in _items5]))
    check("[J5] ★ 端到端：这 2 条被**各自**执行（参数不串）",
          _calls2 == ["J5-AAA", "J5-BBB"], str(_calls2))
    check("[J5] 汇总回执 2/2", "2/2" in str(_res5.get("result") or ""),
          str(_res5.get("result") or "")[:100])
    AgentPipeline._exec_tool_call = _orig_exec2
except Exception as _e5:
    try:
        AgentPipeline._exec_tool_call = _orig_exec2
    except Exception:
        pass
    check("[J5] 端到端（入队→执行）行为级验证", False,
          "%s: %s" % (type(_e5).__name__, str(_e5)[:200]))

# ══════════════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print("PASS %d / FAIL %d" % (PASS, FAIL))
try:
    os.remove(TMP_DB)
except Exception:
    pass
sys.exit(0 if FAIL == 0 else 1)
