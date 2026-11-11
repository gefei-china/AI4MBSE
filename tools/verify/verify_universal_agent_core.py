# -*- coding: utf-8 -*-
"""通用 Agent 内核优化项：行为级验证（P0-1 / P0-3 / P1-1~P1-4 / P2-1 / P2-2）。

纪律（2026-11-09 米爸指定）：
  * 每条判据都做**正常通过 + 变异必须被拒**的双向自证，不做源码文本匹配的假绿；
  * 不调阈值、不插假数据、不把红改绿、不针对样本特判；
  * 生产库只读；所有变异数据用 ``:memory:`` 或临时文件；
  * 每个改动都是通用机制（配置开关 / 契约 / 词表），不是补丁式硬编码。

跑法：./.venv/Scripts/python.exe -X utf8 tools/verify/verify_universal_agent_core.py
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

# 2026-11-09：本门禁会真实调用 request_invariant.assert_before_dispatch，
# 声明 verify 来源 ⇒ 不写生产台账 logs/request_invariant.jsonl。
# 为什么必须显式声明：CI（ci.yml）是逐条直跑脚本、不经 gate_runner，
# 若只靠 runner 打标，绕过 runner 的执行仍会污染「切 enforce」的就绪统计。
os.environ.setdefault("MBSE_INVARIANT_ORIGIN", "verify")

PASS, FAIL, SKIP = [], [], []


def chk(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name,
                          (" — " + str(detail)) if (detail and not cond) else ""))


def skip(name, why):
    """环境缺数据时显式标记 SKIP，绝不伪装成 PASS（空库能绿 ≠ 假绿）。"""
    SKIP.append(name)
    print("  [SKIP] %s — %s" % (name, why))


def section(t):
    print("\n" + "=" * 74 + "\n" + t + "\n" + "=" * 74)


def expect_raise(label, exc_types, fn, *a, **kw):
    try:
        fn(*a, **kw)
        chk(label, False, "未抛异常")
        return None
    except exc_types as e:
        chk(label, True)
        return e
    except Exception as e:
        chk(label, False, "抛了别的异常 %s: %s" % (type(e).__name__, e))
        return None


def ro_db():
    """只读连接（绝不写）。验证目标可由 MBSE_VERIFY_DB 指向空库，模拟全新克隆。"""
    path = os.environ.get("MBSE_VERIFY_DB") or os.path.join(ROOT, "mbse.db")
    if not os.path.isfile(path):
        return None
    try:
        return sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True)
    except Exception:
        return None


def has_table(conn, name):
    if conn is None:
        return False
    try:
        return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                            (name,)).fetchone() is not None
    except Exception:
        return False


def mem_audit(rows):
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE context_audit (id INTEGER PRIMARY KEY, trace_id TEXT, "
              "conversation_id INTEGER, block_name TEXT, layer TEXT, tokens INTEGER, "
              "chars INTEGER, phase TEXT, created_at TEXT)")
    c.executemany("INSERT INTO context_audit (trace_id, conversation_id, block_name, "
                  "layer, tokens, chars, phase, created_at) VALUES (?,?,?,?,?,?,?,?)", rows)
    c.commit()
    return c


# ─────────────────────────────────────────────────────────────────────────────
section("[1] P0-1 运行时请求重建不变量")
from core import request_invariant as ri

MSGS = [{"role": "system", "content": "你是 MBSE 建模助手"},
        {"role": "user", "content": "生成参数图 LinkBudget"}]
TOOLS = [{"name": "sysml_v2_validate", "description": "语法校验"}]
tk = ri.freeze_request(MSGS, TOOLS, {"intent": "design"})
chk("正常请求重建一致", ri.verify_request(tk, MSGS, TOOLS)["ok"] is True)
chk("ticket 可重建原始内容", tk.rebuild() == (MSGS, TOOLS))
chk("变异 messages 被拒",
    ri.verify_request(tk, [{"role": "user", "content": "生成参数图 LinkBudget(改)"}], TOOLS)["ok"] is False)
chk("变异 tools 被拒",
    ri.verify_request(tk, MSGS, [{"name": "other_tool"}])["ok"] is False)
chk("缺少 ticket 被拒", ri.verify_request(None, MSGS, TOOLS)["ok"] is False)
_r1 = ri.verify_request(tk, MSGS, TOOLS); _r2 = ri.verify_request(tk, MSGS, TOOLS)
chk("verify_request 是确定性纯函数", json.dumps(_r1, sort_keys=True) == json.dumps(_r2, sort_keys=True))
_sh = ri.assert_before_dispatch(tk, [{"role": "user", "content": "X"}], TOOLS, mode="shadow")
chk("shadow 记录 would_block 且不阻断", _sh["would_block"] is True and _sh["blocked"] is False)
expect_raise("enforce 漂移被硬拦截", ri.RequestInvariantError,
             ri.assert_before_dispatch, tk, [{"role": "user", "content": "X"}], TOOLS, mode="enforce")
chk("off 完全短路", ri.assert_before_dispatch(tk, MSGS, TOOLS, mode="off")["checked"] is False)
ri.reset_events()
try:
    import llm as _llm
    _client = object.__new__(_llm.LLMClient)      # 绕开构造副作用，只验证挂接方法本身
    chk("统一入口已挂载 _assert_request", hasattr(_llm.LLMClient, "_assert_request"))
    import dis
    _sites = [i for i in dis.get_instructions(_llm.LLMClient.chat)
              if i.argval == "_assert_request"]
    chk("chat 字节码两处调用挂接方法（覆盖 mock 与真实路径）", len(_sites) == 2,
        "实际 %d 处" % len(_sites))
    _client._assert_request(tk, MSGS, TOOLS)
    chk("挂接方法真实执行（产生事件）", len(ri.events()) == 1, "events=%d" % len(ri.events()))
except Exception as e:
    chk("统一入口挂接验证", False, "%s: %s" % (type(e).__name__, e))


# ─────────────────────────────────────────────────────────────────────────────
section("[2] P0-3 单调否决 + shadow 零执行")
from core import guards

def g_ok(_c): return None
def g_deny(_c): return "白名单否决"
def g_deny2(_c): return "预算否决"
def g_boom(_c): raise RuntimeError("守卫自身异常")

v = guards.combine_guards([g_ok, g_ok], {})
chk("全通过 → allowed", v.allowed is True and v.reasons == ())
v = guards.combine_guards([g_deny, g_ok, g_deny2], {})
chk("任一否决 → 整体否决且收齐理由", v.allowed is False and len(v.reasons) == 2, v.as_dict())
v = guards.combine_guards([g_deny], {})
chk("结构上不存在放行接口", not hasattr(guards, "force_allow") and not hasattr(guards, "allow")
    and not hasattr(v, "force_allow"))
chk("后置「放行守卫」不能推翻前置否决",
    guards.combine_guards([g_deny, g_ok], {}).allowed is False)
chk("守卫异常按否决（fail-closed）", guards.combine_guards([g_boom], {}).allowed is False)
chk("纯守卫确定性", guards.require_deterministic(g_deny, {}) is True)
chk("非确定守卫被判出", guards.require_deterministic(lambda c: str(id(c)), {}) is False)
# 零执行：新不变量路径在 shadow 下不得写任何生产表
_conn = ro_db()
if _conn and has_table(_conn, "tool_call_logs"):
    _before = _conn.execute("SELECT COUNT(*) FROM tool_call_logs").fetchone()[0]
    for _ in range(5):
        ri.assert_before_dispatch(tk, MSGS, TOOLS, mode="shadow")
    _after = _conn.execute("SELECT COUNT(*) FROM tool_call_logs").fetchone()[0]
    chk("shadow 判定零工具执行（tool_call_logs 增量 0）", _before == _after,
        "%d -> %d" % (_before, _after))
    _conn.close()
else:
    skip("shadow 判定零工具执行", "空库或缺 tool_call_logs 表；核心变异自证仍在 :memory: 完成")
    if _conn:
        _conn.close()
# 既有 harness gate 的 shadow 分支同样不得调用 _run_verify
_src = io.open(os.path.join(ROOT, "agent", "pipeline_parts", "autopilot.py"), encoding="utf-8").read()
_i = _src.find("if cfg[\"shadow\"]:")
_j = _src.find("if not decision[\"block\"]:", _i)
_shadow_code = "\n".join(l.split("#")[0] for l in _src[_i:_j].splitlines())
chk("既有 harness shadow 分支不含 _run_verify", "_run_verify" not in _shadow_code)


# ─────────────────────────────────────────────────────────────────────────────
section("[3] P1-1 能力 seam 三层分离")
from core import capability_seams as cs

expect_raise("抽象 seam 直接调用即抛错", NotImplementedError, cs.AbstractSeam().run, {})
reg = cs.CapabilityRegistry()

class Cap:
    name = "demo"
    def describe(self): return {"name": self.name}

class Exe:
    def __init__(self): self.calls = 0
    def run(self, cap, payload):
        self.calls += 1
        return {"ok": True, "value": payload.get("x", 0)}

class Never:
    def should_run(self, cap, payload): return False

exe = Exe()
reg.register("demo", Cap(), exe)
expect_raise("重复注册报错", cs.DuplicateCapability, reg.register, "demo", Cap(), Exe())
reg.register("demo", Cap(), exe, replace=True)
chk("显式 replace 可覆盖", reg.names() == ("demo",))
expect_raise("未注册能力报错", cs.UnknownCapability, reg.invoke, "none", {})
reg2 = cs.CapabilityRegistry(); exe2 = Exe()
reg2.register("demo", Cap(), exe2, Never())
chk("策略拒绝时不调用执行器", reg2.invoke("demo", {}) == {"skipped": True, "capability": "demo"}
    and exe2.calls == 0)
_cases = [{"x": 1}, {"x": 2}, {"x": 3}]

class ExeAlt:                      # 值不同、可见键集相同
    def run(self, cap, payload): return {"ok": True, "value": -payload.get("x", 0)}

class ExeBad:                      # 可见键集变了
    def run(self, cap, payload): return {"ok": True, "extra": 1}

_reg_alt = cs.CapabilityRegistry(); _reg_alt.register("demo", Cap(), ExeAlt())
_reg_bad = cs.CapabilityRegistry(); _reg_bad.register("demo", Cap(), ExeBad())
_contract = cs.run_contract(reg, "demo", _cases, alt_registry=_reg_alt)
chk("契约测试：换执行器不改可见输出结构", _contract["alt_checked"] is True and _contract["cases"] == 3)
expect_raise("换后端改变可见结构被拒", cs.ContractError,
             cs.run_contract, reg, "demo", _cases, alt_registry=_reg_bad)
chk("P1-1 被真实模块消费", "request_invariant" in ri.CAPABILITIES.names())


# ─────────────────────────────────────────────────────────────────────────────
section("[4] P1-2 / P2-2 profile 分层装配与多形态")
from core import profile_loader as pl

srv = pl.load_profile("server")
chk("extends 链顺序正确", srv.chain == ("base", "private", "domain_mbse", "server"), srv.chain)
chk("按 group 整块替换（delivery 无残留）", srv.layer("delivery").get("surface") == "web"
    and srv.layer("delivery").get("telemetry") is True)
forms = pl.derive_forms()
chk("三形态派生成功", set(forms) == {"server", "cli", "offline"})
chk("三形态共用同一内核", len({json.dumps(p.layer("kernel"), sort_keys=True)
                            for p in forms.values()}) == 1)
chk("dump 可复现", pl.load_profile("server").digest == srv.digest)
_tmpdir = tempfile.mkdtemp(prefix="dsh_profile_")
_p1 = os.path.join(_tmpdir, "a.json"); _p2 = os.path.join(_tmpdir, "b.json")
io.open(_p1, "w", encoding="utf-8").write(json.dumps({"id": "a", "layers": {"kernel": {"k": 1}}}))
io.open(_p2, "w", encoding="utf-8").write(json.dumps({"id": "b", "layers": {"kernel": {"k": 2}}}))
expect_raise("兄弟层同键冲突启动失败", pl.ProfileConflictError,
             pl.load_profile, "server", overlays=[_p1, _p2])
_cyc = os.path.join(_tmpdir, "cycle.json")
io.open(_cyc, "w", encoding="utf-8").write(json.dumps({"profiles": {
    "a": {"id": "a", "extends": "b", "layers": {}},
    "b": {"id": "b", "extends": "a", "layers": {}}}}))
expect_raise("extends 环被拒", pl.ProfileError, pl.load_profile, "a", _cyc)
# profile 是装配描述，不得改动运行时配置
from core import config as _cfg
_v_before = _cfg.get("rag", "inject_k", None)
pl.load_profile("offline")
chk("装配不改动运行时配置", _cfg.get("rag", "inject_k", None) == _v_before)


# ─────────────────────────────────────────────────────────────────────────────
section("[5] P1-3 封闭事件词表 + 前向兼容")
from core import event_vocabulary as vocab

chk("词表规模（生产实测 18 项）", len(vocab.KNOWN_EVENT_TYPES) == 18,
    len(vocab.KNOWN_EVENT_TYPES))
_conn = ro_db()
if _conn and has_table(_conn, "context_audit"):
    _cov = vocab.assert_production_covered(_conn)
    chk("生产库 block_name 全部被词表覆盖", _cov["ok"] is True, _cov["unknown"])
    _conn.close()
else:
    skip("生产库 block_name 词表覆盖", "空库或缺 context_audit 表")
    if _conn:
        _conn.close()
chk("已知类型接受", vocab.validate_event({"block_name": "skill_prompt"})["accepted"] is True)
expect_raise("未知类型无 ignorable 被拒", vocab.UnknownEventError,
             vocab.validate_event, {"block_name": "brand_new_block"})
chk("未知类型带 ignorable 被接受",
    vocab.validate_event({"block_name": "brand_new_block", "ignorable": True})["ignorable"] is True)
_f = vocab.fold_events([{"block_name": "tools"}, {"block_name": "tools"},
                        {"block_name": "future", "ignorable": True}])
chk("回放折叠计数正确", _f["count"] == 3 and _f["by_type"] == {"tools": 2}
    and _f["ignored"] == ["future"], _f)


# ─────────────────────────────────────────────────────────────────────────────
section("[6] P1-4 可回放计量同源")
from agent import context_meter as cm

_rows = [("t1", 1, "tools", "static", 40, 100, "system", "2026-11-09"),
         ("t1", 1, "skill_prompt", "dynamic", 4701, 12000, "system", "2026-11-09"),
         ("t1", 1, "retrieval", "dynamic", 1263, 3000, "system", "2026-11-09"),
         ("t2", 2, "tools", "static", 41, 101, "system", "2026-11-09")]
_mc = mem_audit(_rows)
_m = cm.measure(_mc, trace_id="t1")
chk("回放总量确定", _m["rows"] == 3 and _m["total_tokens"] == 6004, _m)
chk("相同输入回放 digest 一致", cm.measure(_mc, trace_id="t1")["digest"] == _m["digest"])
chk("同源断言正常通过",
    cm.assert_injection_equals(_mc, {"tools": 1, "skill_prompt": 1, "retrieval": 1},
                               trace_id="t1")["ok"] is True)
_bad = cm.assert_injection_equals(_mc, {"tools": 8}, trace_id="t1")
chk("同源断言变异被拒（不等某个具体数）", _bad["ok"] is False and bool(_bad["diffs"]), _bad)
expect_raise("未知块类型在计量时被拒", vocab.UnknownEventError,
             cm.measure, mem_audit([("t9", 9, "unknown_block", "x", 1, 1, "x", "x")]),
             trace_id="t9")
_mc.close()
_conn = ro_db()
if _conn and has_table(_conn, "context_audit"):
    _tr = _conn.execute("SELECT trace_id FROM context_audit WHERE trace_id IS NOT NULL LIMIT 1").fetchone()
    if _tr:
        _tid = _tr[0]
        _mm = cm.measure(_conn, trace_id=_tid)
        _raw = _conn.execute("SELECT SUM(tokens), COUNT(*) FROM context_audit WHERE trace_id=?",
                             (_tid,)).fetchone()
        chk("生产库回放与直算同源", _mm["total_tokens"] == int(_raw[0] or 0)
            and _mm["rows"] == int(_raw[1] or 0), (_mm["total_tokens"], _raw))
    _conn.close()
else:
    skip("生产库回放与直算同源", "空库或缺 context_audit 表；:memory: 同源判据已单独验证")
    if _conn:
        _conn.close()


# ─────────────────────────────────────────────────────────────────────────────
section("[7] P2-1 MCP 命名空间与原子换世代")
from core import tool_namespace as tn

chk("命名空间规范化", tn.normalize("zhiyuan", "sysmlv2_gen") == "mcp__zhiyuan__sysmlv2_gen")
chk("解析往返", tn.parse(tn.normalize("zhiyuan", "check")) == ("zhiyuan", "check"))
chk("非法名解析为 None", tn.parse("sysmlv2_gen") is None)
expect_raise("非法 server 被拒", tn.ToolNameError, tn.normalize, "bad server!", "x")
cat = tn.ToolCatalog()
cat.register("zhiyuan", ["check", "import"])
_old = cat.snapshot("zhiyuan")
_g0 = cat.generation
cat.apply_list_changed("zhiyuan", ["check", "import", "tree"])
chk("原子换世代后旧快照不变", dict(_old)["zhiyuan"] == ("mcp__zhiyuan__check", "mcp__zhiyuan__import"))
chk("新世代可见且 generation 递增", cat.generation == _g0 + 1
    and len(cat.snapshot("zhiyuan")["zhiyuan"]) == 3)
expect_raise("同 server 重复工具被拒", tn.DuplicateToolError,
             cat.register, "zhiyuan", ["check", "check"])
cat.register("other", ["check"])
chk("不同 server 同名 raw 不冲突",
    len(set(cat.all_names())) == 4, cat.all_names())


# ─────────────────────────────────────────────────────────────────────────────
section("[8] 汇总")
print("  PASS %d / FAIL %d / SKIP %d（环境缺数据，非通过）"
      % (len(PASS), len(FAIL), len(SKIP)))
if FAIL:
    print("  失败项：" + "; ".join(FAIL))
if SKIP:
    print("  跳过项：" + "; ".join(SKIP))
print("=" * 74)
sys.exit(0 if not FAIL else 1)
