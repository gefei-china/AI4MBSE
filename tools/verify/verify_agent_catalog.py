"""自检：Agent 流水线角色目录（`core/agent_catalog.py`）。

口径：裸跑；只读生产库（不写），另用内存库夹具验兜底与缓存隔离。

断言分五组：
1. **DB 读取正确**（与改造前硬编码集合逐一对照 —— 这是"零行为变化"的证据）；
2. **兜底可用**（DB 不可读时仍返回可用集合，且不含已发现的幽灵名）；
3. **缓存语义**（命中 / 失效 / 显式 conn 不污染缓存 / 性能）；
4. **四处接线**（源码级断言：各处确实改读目录，且旧硬编码不再作为主路径）；
5. **变异自证**（改坏实现 ⇒ 判据必须翻红）。
"""

from __future__ import annotations

import io
import os
import re
import sqlite3
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from core import agent_catalog as cat  # noqa: E402
from core.agent_catalog import (  # noqa: E402
    FALLBACK_CODE_STAGES,
    FALLBACK_ORCH_DEFAULT,
    FALLBACK_STAGE_INTENTS,
    FALLBACK_STANDARD_INTENTS,
    TTL_SECONDS,
)

PASS = 0
FAIL = 0
FAILED: list[str] = []


def check(label: str, cond: bool, detail: object = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ✓ %s" % label)
    else:
        FAIL += 1
        FAILED.append(label)
        print("  ✗ %s   %s" % (label, ("｜" + str(detail)) if detail != "" else ""))


def read_src(rel: str) -> str:
    return io.open(os.path.join(REPO, rel), encoding="utf-8").read()


def code_only(src: str) -> str:
    """剔注释后的源码（断"某写法已不存在"时必须排除说明文字）。"""
    out = []
    for line in src.splitlines():
        i = line.find("#")
        out.append(line if i < 0 else line[:i])
    return "\n".join(out)


DB = os.path.join(REPO, "mbse.db")
HAS_DB = os.path.exists(DB)


def _mem_conn(agents_rows=None, with_agents=True):
    """内存库夹具：`agents_rows` = [(name, status, builtin, agent_role, stage_order, is_code, is_orch)]"""
    c = sqlite3.connect(":memory:")
    if with_agents:
        c.execute("""CREATE TABLE agents (
            name TEXT, status TEXT, builtin INTEGER, agent_role TEXT,
            stage_order INTEGER, is_code_stage INTEGER, is_orch_default INTEGER)""")
        for r in (agents_rows or []):
            c.execute("INSERT INTO agents VALUES (?,?,?,?,?,?,?)", r)
        c.commit()
    return c


# ══════════════════════════════════════════════════════════════════
print("=" * 70)
print("[1] DB 读取正确（与改造前硬编码集合逐一对照）")
print("=" * 70)

if not HAS_DB:
    print("  （跳过：生产库不存在）")
else:
    cat.invalidate()
    snap = cat.snapshot(None, force=True)
    check("1.1 来源是 DB 而非兜底", snap.get("source") == "db", snap.get("source"))
    check("1.2 三个列都在", all((snap.get("columns") or {}).values()), snap.get("columns"))

    # 改造前 intent.py 提示词里的 8 个阶段（硬编码）
    OLD_STAGES = ["methodology_resolver", "requirement_structuring", "architecture_skeleton",
                  "view_expansion", "model_validation_repair", "trace_verification",
                  "change_safety_gate", "model_release"]
    check("1.3 阶段意图 = 8 个且顺序与旧提示词逐项一致",
          list(snap["stage_intents"]) == OLD_STAGES, snap["stage_intents"])

    # 改造前 stream._CODE_STAGE_INTENTS 里**真实存在**的 4 个（另 2 个是幽灵名）
    OLD_CODE = {"design", "architecture_skeleton", "view_expansion", "model_validation_repair"}
    check("1.4 产码阶段 = 旧清单中真实存在的 4 个",
          set(snap["code_stages"]) == OLD_CODE, sorted(snap["code_stages"]))
    check("1.5 产码阶段**不含幽灵名**（multi_design / sysml_view_generation）",
          not ({"multi_design", "sysml_view_generation"} & set(snap["code_stages"])),
          sorted(snap["code_stages"]))

    OLD_ORCH = ["requirement_analysis", "design", "impact", "review",
                "report_generation", "knowledge_qa"]
    check("1.6 默认编排池 = 旧 _ORCH_AGENTS 且**顺序一致**（零行为变化）",
          list(snap["orch_default"]) == OLD_ORCH, snap["orch_default"])

    # 标准意图必须覆盖全部 builtin=1（这是 tests/test_intent_explainability.py 的守护语义）
    c = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
    builtin = {r[0] for r in c.execute("SELECT name FROM agents WHERE builtin=1 AND status='active'")}
    mains = {r[0] for r in c.execute(
        "SELECT name FROM agents WHERE agent_role='main' AND status='active' "
        "AND COALESCE(builtin,0)=0")}
    c.close()
    check("1.7 标准意图覆盖全部 builtin=1 的 active Agent",
          builtin and builtin <= set(snap["standard_intents"]),
          sorted(builtin - set(snap["standard_intents"])))
    check("1.8 标准意图**不含**非内置主 Agent（那是执行主体，不是意图）",
          not (mains & set(snap["standard_intents"])), sorted(mains))


print()
print("=" * 70)
print("[2] 兜底（DB 不可读时仍可用）")
print("=" * 70)

_c = _mem_conn(with_agents=False)      # 完全没有 agents 表
_fb = cat.snapshot(_c)
_c.close()
check("2.1 无 agents 表 ⇒ source=fallback", _fb.get("source") == "fallback", _fb.get("source"))
check("2.2 兜底阶段意图 = 8 个", len(_fb["stage_intents"]) == 8, _fb["stage_intents"])
check("2.3 兜底产码阶段不含幽灵名",
      not ({"multi_design", "sysml_view_generation"} & set(_fb["code_stages"])),
      sorted(_fb["code_stages"]))
check("2.4 兜底编排池 = 6 个", len(_fb["orch_default"]) == 6, _fb["orch_default"])
check("2.5 兜底常量与模块常量一致",
      _fb["stage_intents"] == FALLBACK_STAGE_INTENTS
      and _fb["orch_default"] == FALLBACK_ORCH_DEFAULT
      and set(_fb["code_stages"]) == set(FALLBACK_CODE_STAGES)
      and set(_fb["standard_intents"]) == set(FALLBACK_STANDARD_INTENTS))

# 有 agents 表但**全空** ⇒ 也应回落兜底（否则拿到空集 = 当场把能力关掉）
_c = _mem_conn(agents_rows=None)
_e = cat.snapshot(_c)
_c.close()
check("2.6 agents 表存在但无行 ⇒ 回落兜底（不返回空集）",
      _e.get("source") == "fallback" and _e["orch_default"], _e.get("source"))

# 有行但 is_code_stage 全 0 ⇒ code_stages 为空是**真实结果**，不该被兜底掩盖
_c = _mem_conn([("design", "active", 1, "sub", None, 0, 1)])
_z = cat.snapshot(_c)
_c.close()
check("2.7 有行但未打产码标 ⇒ code_stages 为空（如实反映，不用兜底掩盖）",
      _z.get("source") == "db" and not _z["code_stages"], (_z.get("source"), _z["code_stages"]))


print()
print("=" * 70)
print("[3] 缓存语义")
print("=" * 70)

cat.invalidate()
_a = cat.snapshot(None, force=True)
_b = cat.snapshot(None)
check("3.1 TTL 内两次调用返回**同一对象**（缓存命中）", _a is _b)

cat.invalidate()
_d = cat.snapshot(None)
check("3.2 invalidate 后重新取（对象不同）", _d is not _a)

# 显式 conn 不得污染默认缓存
cat.invalidate()
_base = cat.snapshot(None, force=True)
_c = _mem_conn([("only_one", "active", 0, "sub", 0, 1, 1)])
_tmp = cat.snapshot(_c)
_c.close()
_after = cat.snapshot(None)
check("3.3 显式 conn 的查询结果不污染默认缓存",
      _after is _base and "only_one" not in _after["orch_default"],
      (_after is _base, _after["orch_default"]))

cat.invalidate()
cat.standard_intents()   # 预热：首次调用含"打开连接 + 一次查询"，不该算进缓存读的成本
_t0 = time.time()
for _ in range(2000):
    cat.standard_intents()
_ms = (time.time() - _t0) * 1000
check("3.4 2000 次读取 < 50ms（缓存足够快，落库路径无往返）", _ms < 50, "%.1fms" % _ms)

check("3.5 TTL 是有限正数", 0 < TTL_SECONDS <= 600, TTL_SECONDS)


print()
print("=" * 70)
print("[4] 四处接线（源码级）")
print("=" * 70)

_s = code_only(read_src("agent/pipeline_parts/session.py"))
check("4.1 session 的落库标签改读目录",
      "agent_catalog import standard_intents" in _s.replace("  ", " "), "")

_s = code_only(read_src("agent/pipeline_parts/stream.py"))
check("4.2 stream 产码判定改读目录", "_code_stages()" in _s and "agent_catalog" in _s)
check("4.3 stream 不再引用幽灵名 multi_design / sysml_view_generation",
      "multi_design" not in _s and "sysml_view_generation" not in _s,
      [w for w in ("multi_design", "sysml_view_generation") if w in _s])

_s = code_only(read_src("agent/intent.py"))
check("4.4 intent 的粒度规则改读目录",
      "agent_catalog import stage_intents" in _s.replace("  ", " "), "")
check("4.5 intent 的提示词不再有硬编码阶段串",
      "methodology_resolver/requirement_structuring" not in _s)

_s = code_only(read_src("agent/pipeline_parts/skills.py"))
check("4.6 _ORCH_AGENTS 已改为属性（读目录 + 兜底）",
      "_ORCH_AGENTS_FALLBACK" in _s and "def _ORCH_AGENTS" in _s and "@property" in _s)

_s = code_only(read_src("core/plan_guard.py"))
check("4.7 plan_guard 的阶段序优先读 agents.stage_order",
      "agents.stage_order" in read_src("core/plan_guard.py")
      and "snapshot as _cat_snap" in _s.replace("  ", " "), "")
check("4.8 plan_guard 只在目录真读表时才标 agents.stage_order（防把兜底标成表）",
      '== "db"' in _s or "== 'db'" in _s, "")

# 旧硬编码若仍存在，必须只是**兜底常量**（出现在 fallback 命名的变量里）
_s = read_src("agent/pipeline_parts/stream.py")
check("4.9 stream 的旧常量已重命名/标注为兜底",
      "_CODE_STAGE_INTENTS = {" in _s and "兜底" in _s.split("_CODE_STAGE_INTENTS = {")[0][-400:])


print()
print("=" * 70)
print("[5] 变异自证（改坏实现 ⇒ 判据必须翻红）")
print("=" * 70)

_SRC = read_src("core/agent_catalog.py")


def _mutate(old: str, new: str):
    if _SRC.count(old) != 1:
        return None, _SRC.count(old)
    ns = {"__name__": "mutant_catalog"}
    try:
        exec(compile(_SRC.replace(old, new, 1), "<mutant>", "exec"), ns)
    except Exception as e:
        return None, "exec异常:%s" % str(e)[:60]
    return ns, 1


def _mut_check(label, old, new, fn):
    ns, hit = _mutate(old, new)
    check("%s 锚点唯一命中" % label, hit == 1, "命中=%s" % hit)
    if not ns:
        check(label, False, "变异体未生成")
        return
    try:
        caught = bool(fn(ns))
    except Exception:
        caught = True
    check(label, caught)


# M1：阶段意图判据失效（不再收集 stage_order 行）⇒ 阶段集合变空
# ⚠️ 夹具必须**同时带 orch=1**：否则变异后四类集合全空 ⇒ snapshot 回落兜底（8 个阶段），
#    反而"看起来正常" —— 变异就被兜底掩盖了（假绿）。这是写变异用例最容易踩的坑。
_M1_OLD = "            if so is not None:\n                stage_pairs.append((int(so), name))\n                std.add(name)"
_M1_NEW = "            if False:\n                stage_pairs.append((int(so), name))\n                std.add(name)"
_mut_check("M1 变异「不收集阶段」被抓住", _M1_OLD, _M1_NEW,
           lambda ns: not ns["snapshot"](_mem_conn([("a", "active", 0, "sub", 3, 0, 1)]))["stage_intents"])

# M2：产码标记被忽略 ⇒ code_stages 恒空（同样必须让 orch 非空，避免被兜底掩盖）
_M2_OLD = "            if is_code:\n                code.add(name)"
_M2_NEW = "            if False:\n                code.add(name)"
_mut_check("M2 变异「忽略产码标记」被抓住", _M2_OLD, _M2_NEW,
           lambda ns: not ns["snapshot"](_mem_conn([("x", "active", 0, "sub", None, 1, 1)]))["code_stages"])

# M3：兜底里塞回幽灵名 ⇒ 2.3 判据翻
_M3_OLD = 'FALLBACK_CODE_STAGES = frozenset({\n    "design", "architecture_skeleton", "view_expansion", "model_validation_repair",\n})'
if _SRC.count(_M3_OLD) != 1:
    # 锚点可能因格式不同不命中 —— 用更小的唯一片段
    _M3_OLD = '"design", "architecture_skeleton", "view_expansion", "model_validation_repair",\n})'
    _M3_NEW = '"design", "architecture_skeleton", "view_expansion", "model_validation_repair", "multi_design",\n})'
else:
    _M3_NEW = _M3_OLD.replace('"model_validation_repair",', '"model_validation_repair", "multi_design",')
_mut_check("M3 变异「兜底塞回幽灵名」被抓住", _M3_OLD, _M3_NEW,
           lambda ns: "multi_design" in set(ns["FALLBACK_CODE_STAGES"]))

# M4：显式 conn 也走缓存 ⇒ 3.3 的隔离判据翻
_M4_OLD = "    if conn is not None:\n        got = _query(conn)"
_M4_NEW = "    if False:\n        got = _query(conn)"
_mut_check("M4 变异「显式 conn 走缓存」被抓住", _M4_OLD, _M4_NEW,
           lambda ns: (ns["snapshot"](_mem_conn([("only", "active", 0, "sub", 0, 1, 1)]))["orch_default"]
                       == ns["snapshot"](None)["orch_default"]))


print()
print("=" * 70)
print("汇总：PASS = %d / FAIL = %d" % (PASS, FAIL))
if FAILED:
    print("失败项：")
    for f in FAILED:
        print("  -", f)
print("=" * 70)
sys.exit(0 if FAIL == 0 else 1)
