# -*- coding: utf-8 -*-
"""P1-4 工具结果 offload 验证（tool_offload.py + tool_result_fetch handler + 双路径接入）。

验证六件事：
1. 契约：init_db 后 tool_result_offloads 表存在；
2. save→fetch 往返一致；fetch 不存在 id → 空串；fetch 非法 id → 空串；
3. model_side_content：≤cap 全量回填（旧行为）；>cap 自动 offload——引用块含 id/原长/工具名/
   重读指引，长度 << 全文；offload 故障 → 回退旧式截断（不阻断）；
4. ensure_fetch_tool：tools_def 注入幂等；whitelist 放行幂等；tools_def=None 时只放行白名单；
5. 变异自证：patch save_offload 注入故障 → offload 标记断言必须挂（证明断言真测被测逻辑）；
6. handler 接入：源码静态断言（tools.py 含 tool_result_fetch 分支；stream/execute 调 model_side_content）。

用法：直接跑（MBSE_DB_PATH 指向临时库，零网络）。
"""
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="verify_tool_offload_")
os.environ["MBSE_DB_PATH"] = os.path.join(_TMP, "verify.db")

sys.path.insert(0, r"C:/Users/gefei/WorkBuddy/2026-08-04-19-05-52/mbse_system")
os.chdir(r"C:/Users/gefei/WorkBuddy/2026-08-04-19-05-52/mbse_system")

from database import init_db
init_db()

import sqlite3
from agent.pipeline_parts import tool_offload as to

FAILURES = []


def check(name, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    print(f"[{tag}] {name}" + (f" —— {detail}" if detail else ""))
    if not cond:
        FAILURES.append(name)


# ── 1. 契约 ─────────────────────────────────────────────────────────────
con = sqlite3.connect(os.environ["MBSE_DB_PATH"])
tables = [r[0] for r in con.execute(
    "SELECT name FROM sqlite_master WHERE type='table' AND name='tool_result_offloads'")]
con.close()
check("契约：tool_result_offloads 表已建", tables == ["tool_result_offloads"])

# ── 2. save→fetch 往返 ─────────────────────────────────────────────────
BIG = "检索命中数据行：" + "，".join(f"条目{i}" for i in range(2000))  # ~1.5 万字符
oid = to.save_offload("graph_retrieve", BIG, conversation_id=42)
check("save→fetch 往返一致", to.fetch_offload(oid) == BIG)
check("fetch 不存在 id → 空串", to.fetch_offload(99999) == "")
check("fetch 非法 id → 空串", to.fetch_offload(0) == "" and to.fetch_offload(-1) == "")

# ── 3. model_side_content ──────────────────────────────────────────────
# 3a. ≤cap：旧行为（全量）
content, off = to.model_side_content("glossary_lookup", {"ok": True, "result": "短结果"}, 1, cap=3000)
check("≤cap：全量回填且不 offload", content == {"ok": True, "result": "短结果"} and off is False)

# 3b. >cap：引用块 + 可寻址（⚠️ 本次 save 会产生**新** id——须从引用块解析后再回查，不能用 §2 的旧 id）
result_big = {"ok": True, "result": BIG}
content, off = to.model_side_content("graph_retrieve", result_big, 42, cap=3000)
ref = content["result"]
check(">cap：标记 offloaded", off is True)
import re as _re
_m = _re.search(r"offload #(\d+)", ref)
check(">cap：引用块含 offload 编号（可解析）", bool(_m), ref[:60])
if _m:
    new_oid = int(_m.group(1))
    check(">cap：按引用块 id 可取回全文（可寻址召回）", to.fetch_offload(new_oid) == BIG)
    check(">cap：引用块含原长/工具名", f"原 {len(BIG)} 字符" in ref and "graph_retrieve" in ref)
    check(">cap：引用块含重读指引", "tool_result_fetch" in ref and f'"id": {new_oid}' in ref)
    check(">cap：note 里 id 与引用块一致（模型视角可解析）", f"id={new_oid}" in content.get("note", ""))
check(">cap：引用块含头部摘要（可判断是否重读）", "条目0" in ref)
check(">cap：引用块远小于全文", len(ref) < 1200, f"{len(BIG)}→{len(ref)}")
check(">cap：truncated 标记与 note", content.get("truncated") is True and "offload" in content.get("note", ""))

# 3c. offload 故障 → 回退旧式截断
_orig_save = to.save_offload
to.save_offload = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("db down"))
try:
    content, off = to.model_side_content("x", result_big, 0, cap=3000)
finally:
    to.save_offload = _orig_save
check("故障回退：退化为旧式截断不阻断", off is False and content["result"] == BIG[:3000]
      and content.get("note") == "工具结果过长已截断，请基于以上内容作答")

# ── 4. ensure_fetch_tool ───────────────────────────────────────────────
tools_def = [{"type": "function", "function": {"name": "graph_retrieve"}}]
wl = ["graph_retrieve"]
to.ensure_fetch_tool(tools_def, wl)
check("注入：tools_def 含 fetch 定义", any(
    (t.get("function") or {}).get("name") == "tool_result_fetch" for t in tools_def))
check("注入：whitelist 已放行", "tool_result_fetch" in wl)
to.ensure_fetch_tool(tools_def, wl)
n_fetch = sum(1 for t in tools_def if (t.get("function") or {}).get("name") == "tool_result_fetch")
check("幂等：重复注入不重复", n_fetch == 1)
td2, wl2 = None, ["a"]
to.ensure_fetch_tool(td2, wl2)
check("tools_def=None：只放行白名单不炸", "tool_result_fetch" in wl2)

# ── 5. 变异自证 ────────────────────────────────────────────────────────
def _assert_offloaded():
    content, off = to.model_side_content("graph_retrieve", result_big, 42, cap=3000)
    assert off is True and "tool_result_fetch" in content["result"], ">cap 必须 offload 为引用块"


_orig_save2 = to.save_offload
try:
    to.save_offload = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("mutation"))
    try:
        _assert_offloaded()
        print("[FAIL] 变异自证：save_offload 故障注入后断言仍通过 ⇒ 断言空转")
        FAILURES.append("mutation:save_offload")
    except AssertionError:
        print("[PASS] 变异自证：save_offload 故障注入后断言如预期 FAIL")
finally:
    to.save_offload = _orig_save2
content, off = to.model_side_content("graph_retrieve", result_big, 42, cap=3000)
check("恢复后 offload 回绿", off is True)

# ── 6. handler/双路径静态断言 ──────────────────────────────────────────
import inspect
from agent.pipeline_parts import tools as _tools_mod
src_tools = inspect.getsource(_tools_mod)
check("接入：tools.py 含 tool_result_fetch 分支",
      'if name == "tool_result_fetch":' in src_tools and "fetch_offload" in src_tools)
from agent.pipeline_parts import stream as _stream_mod, execute as _execute_mod
check("接入：stream.py 调 model_side_content",
      "model_side_content" in inspect.getsource(_stream_mod))
check("接入：execute.py 调 model_side_content（补封顶缺口）",
      "model_side_content" in inspect.getsource(_execute_mod))
check("接入：ensure_fetch_tool 在两路径都被调用",
      "ensure_fetch_tool" in inspect.getsource(_stream_mod)
      and "ensure_fetch_tool" in inspect.getsource(_execute_mod))

# ── 7. 清理机制（P1-4 生命周期补全：TTL + 会话级联）────────────────────
from database import db_conn
oid_new = to.save_offload("t", "新数据" * 100, 555)
oid_old = to.save_offload("t", "旧数据" * 100, 556)
with db_conn() as c:
    c.execute("UPDATE tool_result_offloads SET created_at = datetime('now', '-40 days') WHERE id=?",
              (oid_old,))
    c.commit()
n_exp = to.cleanup_expired(days=30)
check("cleanup_expired：只删过期行（新行保留）",
      n_exp >= 1 and to.fetch_offload(oid_old) == "" and to.fetch_offload(oid_new) != "")
n_conv = to.cleanup_conversation(555)
check("cleanup_conversation：会话级联清空", n_conv == 1 and to.fetch_offload(oid_new) == "")
oid_a = to.save_offload("t", "A", 700)
oid_b = to.save_offload("t", "B", 701)
to.cleanup_conversation(700)
check("隔离：清 conv 700 不影响 701", to.fetch_offload(oid_a) == "" and to.fetch_offload(oid_b) == "B")
to.cleanup_conversation(701)  # 清测试残留
check("接入：conversations.py 删除端点已级联",
      "cleanup_conversation" in inspect.getsource(
          __import__("routers.conversations", fromlist=["x"])))
check("接入：main.py lifespan 已挂启动 TTL 清理",
      "cleanup_expired" in inspect.getsource(__import__("main")))

print()
if FAILURES:
    print(f"❌ {len(FAILURES)} 项失败：{FAILURES}")
    sys.exit(1)
print("✅ ALL PASS —— offload 契约/往返/双路径/幂等/清理/变异自证全部通过")
