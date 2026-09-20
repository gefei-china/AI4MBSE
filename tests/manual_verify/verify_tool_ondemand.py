"""工具按需调用（P0-按需工具）闭环验证。

问题1：意图匹配 Agent 后不应把绑定工具全部执行/注入，应按实际需要匹配。
三项优化验证：
T1. JIT 语义预筛阈值 8→2：小工具集（≥2）也按输入语义裁剪，写工具（entity_create）被裁、读核心工具（graph_retrieve）保底保留
T2. 空回退：全弱相关 → 不瘦身，维持全量注入（保底可靠）
T3. 白名单模式：core_keep 不额外注入，注入集合 ⊆ 白名单（委派最小权限）
T4. system prompt 工具约束：含「工具使用约束 / 一次最多调用 2 个 / 禁止连环调用」
T5. 弱相关标记：graph_retrieve 对无关查询返回 weak=True（引导收敛）
T6. 全弱收敛：ReAct 循环全部工具结果弱相关 → 提前 break，不再进入第 2 轮探测
T7. 执行追踪：Mock 工具模拟下只执行被调用的工具（graph_retrieve），entity_create 不执行
"""
import json
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["MBSE_LLM_FORCE_MOCK"] = "1"
_TEST_DB = os.path.join(os.path.dirname(__file__), "_tool_ondemand_test.db")
if os.path.exists(_TEST_DB):
    os.remove(_TEST_DB)
os.environ["MBSE_DB_PATH"] = _TEST_DB

from database import db_conn, init_db  # noqa: E402
from agent import AgentPipeline  # noqa: E402
import agent as agent_mod  # noqa: E402

init_db()

PASS = 0
FAIL = 0


def chk(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  → {detail}")


def new_conv(conn, title="工具验证会话"):
    cur = conn.execute("INSERT INTO conversations (title, intent) VALUES (?, '')", (title,))
    conn.commit()
    return cur.lastrowid


pipe = AgentPipeline()
# 不调用 _load_db_agents：用内置 DEFINITIONS（design = graph_retrieve + entity_create，2 个候选触发阈值 2）

print("== T1: JIT 语义预筛（阈值 2）——检索意图裁剪写工具 ==")
tools_def = pipe._build_tools_def("design", "请问知识库里关于宽带通信的资料有哪些")
injected = {t["function"]["name"] for t in tools_def}
print(f"    注入工具: {sorted(injected)}")
chk("T1 graph_retrieve 保留（读核心工具保底）", "graph_retrieve" in injected, f"injected={injected}")
chk("T1 注入数量不超过候选 2", len(tools_def) <= 2, f"n={len(tools_def)}")
# 检索意图下写工具（创建实体）应被语义裁剪（bigram 无实质相关）
chk("T1 写工具 entity_create 被裁剪", "entity_create" not in injected,
    f"entity_create 仍在注入（语义未区分）→ injected={injected}")

print("== T2: 无写意图（含闲聊全弱）→ 写工具不注入，读工具保底 ==")
tools_def2 = pipe._build_tools_def("design", "今天天气真不错 完全无关 的 闲聊 文本 xyz")
injected2 = {t["function"]["name"] for t in tools_def2}
print(f"    注入工具: {sorted(injected2)}")
chk("T2 无写意图不注入写工具", "entity_create" not in injected2,
    f"injected={injected2}")
chk("T2 读核心工具 graph_retrieve 保底保留", "graph_retrieve" in injected2,
    f"injected={injected2}")

print("== T2b: 显式写意图 → 写工具注入 ==")
tools_def2b = pipe._build_tools_def("design", "帮我创建几个新实体并入库到知识库")
injected2b = {t["function"]["name"] for t in tools_def2b}
print(f"    注入工具: {sorted(injected2b)}")
chk("T2b 写意图激活 entity_create 注入", "entity_create" in injected2b,
    f"injected={injected2b}")

print("== T3: 白名单模式（委派最小权限，core_keep 不保底） ==")
pipe._tool_whitelist = ["graph_retrieve"]
try:
    tools_def3 = pipe._build_tools_def("design", "帮我创建几个新实体到知识库")
    injected3 = {t["function"]["name"] for t in tools_def3}
    chk("T3 注入集合为白名单子集", injected3 == {"graph_retrieve"},
        f"injected={injected3}（entity_create 应被白名单拒绝）")
finally:
    pipe._tool_whitelist = None

print("== T4: system prompt 工具使用约束 ==")
captured = {}
_orig_chat = agent_mod.llm_client.chat  # patch 前保存原函数引用


def _spy_chat(messages, **kw):
    if not kw.get("stream"):
        captured["system"] = messages[0]["content"] if messages else ""
    return _orig_chat(messages, **kw)


with mock.patch.object(agent_mod.llm_client, "chat", autospec=True,
                       side_effect=lambda *a, **kw: _spy_chat(*a, **kw)):
    with db_conn() as conn:
        cid = new_conv(conn)
    events = list(pipe.execute_stream("帮我生成设计模型的 sysml 代码", cid, dry_run=True))
sp = captured.get("system", "")
chk("T4 system prompt 含「工具使用约束」", "工具使用约束" in sp,
    f"prompt 长度={len(sp)}")
chk("T4 含「一次最多调用 2 个」", "一次最多调用 2 个" in sp)
chk("T4 含「禁止反复/连环调用工具」", "禁止反复/连环调用工具" in sp)

print("== T5: graph_retrieve 弱相关标记 ==")
r5 = pipe._exec_tool_call("graph_retrieve", {"query": "完全不相关的 xyz 查询"})
print(f"    result={str(r5.get('result'))[:80]}")
chk("T5 空库弱相关返回 weak=True", bool(r5.get("weak")) is True, f"weak={r5.get('weak')}")

print("== T6: 全弱收敛（ReAct 不连环调用） ==")
pipe._tool_whitelist = ["graph_retrieve"]  # 只注入 1 个工具，Mock 稳定调用 graph_retrieve
try:
    with db_conn() as conn:
        cid2 = new_conv(conn, "收敛验证")
    events = list(pipe.execute_stream("帮我生成设计模型的 sysml 代码 调用工具", cid2, dry_run=True))
finally:
    pipe._tool_whitelist = None
reasonings = [e.get("delta", "") for e in events if e.get("type") == "reasoning"]
tools_done = [e for e in events if e.get("type") == "tool" and e.get("status") == "done"]
weak_reason = [d for d in reasonings if "本轮工具结果均与任务弱相关" in d]
round2 = [d for d in reasonings if "第 2 轮" in d]
print(f"    reasoning 条数={len(reasonings)}，工具执行={[(e.get('name'), e.get('ok')) for e in tools_done]}")
chk("T6 产生全弱收敛 reasoning", len(weak_reason) >= 1, f"reasonings={reasonings[:4]}")
chk("T6 不进入第 2 轮探测", len(round2) == 0, f"round2={round2}")
chk("T6 仅执行被调用工具（graph_retrieve 1 次）",
    [e.get("name") for e in tools_done] == ["graph_retrieve"],
    f"executed={[e.get('name') for e in tools_done]}")

print("== T7: 执行追踪——未调用工具不执行 ==")
exec_names = {e.get("name") for e in tools_done}
chk("T7 无 entity_create 执行", "entity_create" not in exec_names,
    f"executed={exec_names}")

print("== T8: execute（非流式）路径全弱收敛 ==")
call_log = []


def _spy_exec(messages, **kw):
    call_log.append({"tools": kw.get("tools"), "stream": kw.get("stream")})
    return _orig_chat(messages, **kw)


with mock.patch.object(agent_mod.llm_client, "chat", autospec=True,
                       side_effect=lambda *a, **kw: _spy_exec(*a, **kw)):
    with db_conn() as conn:
        cid3 = new_conv(conn, "execute收敛")
    r8 = pipe.execute("帮我生成设计模型的 sysml 代码 调用工具", cid3, dry_run=True,
                      tools_whitelist=["graph_retrieve"])
chk("T8 非流式收敛后有正文", bool(r8.get("content")), f"content={str(r8.get('content'))[:40]}")
_chat_calls = [c for c in call_log if c["tools"] is not None and not c["stream"]]
_final_calls = [c for c in call_log if c["tools"] is None and not c["stream"]]
chk("T8 工具探测仅 1 轮", len(_chat_calls) == 1, f"n={len(_chat_calls)}（探测轮工具调用数）")
chk("T8 收敛后走无工具最终生成", len(_final_calls) >= 1, f"n={len(_final_calls)}")

print(f"\n结果: PASS={PASS} FAIL={FAIL}")
sys.exit(0 if FAIL == 0 else 1)
