"""D12 意图识别增强（P0 置信度三级决策 + 会话级 DST + 意图级缓存）闭环验证。

T1. 高置信规则路由：meta.route=rule / confidence=0.95 / 无需澄清
T2. 意图级缓存：同文本二次 detect → route=cache；intent_cache 表落库、hit_count 递增
T3. 缓存指纹失效：DB Agent 关键词变化 → 指纹变化 → 缓存不命中
T4. 会话级意图继承：无信号文本 + prev_intent=design → design（route=inherit，需澄清）
T5. 无信号且无 prev → chat（route=chat）
T6. DST 落库往返：_save/_load_conversation_dst 一致性
T7. 槽位跨轮合并：_merge_slots（entities 并集 / goal 覆盖）
T8. execute_stream 澄清事件：会话 current_intent=design + 无信号追问 → clarify 事件（intent=design）
T9. execute_stream stage done 携带 route/confidence
T10. execute 结果携带 intent_route/intent_confidence（dry_run 验证）
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["MBSE_LLM_FORCE_MOCK"] = "1"
_TEST_DB = os.path.join(os.path.dirname(__file__), "_d12_test.db")
if os.path.exists(_TEST_DB):
    os.remove(_TEST_DB)
os.environ["MBSE_DB_PATH"] = _TEST_DB

from database import db_conn, init_db  # noqa: E402
from agent import AgentPipeline  # noqa: E402

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


def new_conv(conn, title="验证会话"):
    cur = conn.execute("INSERT INTO conversations (title, intent) VALUES (?, '')", (title,))
    conn.commit()
    return cur.lastrowid


pipe = AgentPipeline()
pipe._load_db_agents()

print("== T1: 高置信规则路由 ==")
r1 = pipe.router.detect("帮我生成设计模型的 sysml 代码")
m1 = pipe.router.get_last_meta()
chk("T1 规则命中 design", r1 == "design", f"intent={r1}")
chk("T1 route=rule", m1["route"] == "rule", f"route={m1['route']}")
chk("T1 confidence=0.95", m1["confidence"] == 0.95, f"conf={m1['confidence']}")
chk("T1 无需澄清", m1["needs_clarification"] is False, f"clarify={m1['needs_clarification']}")

# P1-31（2026-10-02）：本组用例（T2/T3）测的**正是缓存机制**，而缓存已**默认停用**
#   （实测收益 ≈ 0：真库近 7 天仅 18 条用户消息；intent_detect 的 1605 次/7 天 >95% 是评测刷的）。
#   故此处**显式开启**作为前提 —— 这符合"断言不得耦合本次部署恰好配了什么"的纪律。
#   ⚠️ 别把 T2/T3 一删了事：机制仍在（`intent.cache_enabled=True` 可开），只是默认关。
from core import config as _cfg  # noqa: E402
_cfg._CONFIG.setdefault("intent", {})["cache_enabled"] = True

print("== T2: 意图级缓存 ==")
r2a = pipe.router.detect("帮我生成设计模型的 sysml 代码")
r2b = pipe.router.detect("帮我生成设计模型的 sysml 代码")
m2 = pipe.router.get_last_meta()
chk("T2 二次命中缓存", r2b == "design" and m2["route"] == "cache", f"intent={r2b} route={m2['route']}")
with db_conn() as conn:
    row = conn.execute(
        "SELECT intent, route, confidence, hit_count FROM intent_cache WHERE query_hash=?",
        (__import__("hashlib").md5("帮我生成设计模型的 sysml 代码".encode("utf-8")).hexdigest(),)).fetchone()
chk("T2 缓存落库", row is not None, f"row={row}")
chk("T2 高置信才缓存", row and row["confidence"] == 0.95, f"conf={row and row['confidence']}")
chk("T2 hit_count 递增", row and row["hit_count"] >= 1, f"hit={row and row['hit_count']}")

print("== T3: 缓存指纹失效 ==")
pipe.router.register_keywords("custom_intent_xyz", ["星际密钥词触发"])
r3 = pipe.router.detect("帮我生成设计模型的 sysml 代码")
m3 = pipe.router.get_last_meta()
chk("T3 指纹变化缓存不命中", m3["route"] != "cache", f"route={m3['route']}")
chk("T3 仍正确路由 design", r3 == "design", f"intent={r3}")

print("== T4: 会话级意图继承 ==")
r4 = pipe.router.detect("再详细一点", prev_intent="design")
m4 = pipe.router.get_last_meta()
chk("T4 无信号继承 design", r4 == "design", f"intent={r4}")
chk("T4 route=inherit", m4["route"] == "inherit", f"route={m4['route']}")
chk("T4 继承视为中置信需澄清", m4["needs_clarification"] is True, f"clarify={m4['needs_clarification']}")

print("== T5: 无信号无上下文 → chat ==")
r5 = pipe.router.detect("再详细一点")
m5 = pipe.router.get_last_meta()
chk("T5 回落 chat", r5 == "chat", f"intent={r5}")
chk("T5 route=chat", m5["route"] == "chat", f"route={m5['route']}")

print("== T6: DST 落库往返 ==")
with db_conn() as conn:
    cid = new_conv(conn)
pipe._save_conversation_dst(cid, "design", {"goal": "生成 BDD", "entities": ["卫星"]})
dst = pipe._load_conversation_dst(cid)
chk("T6 意图往返", dst["intent"] == "design", f"intent={dst['intent']}")
chk("T6 槽位往返", dst["slots"].get("goal") == "生成 BDD", f"slots={dst['slots']}")

print("== T7: 槽位跨轮合并 ==")
merged = pipe._merge_slots(
    {"goal": "旧目标", "entities": ["卫星"], "constraints": ["c1"]},
    {"goal": "新目标", "entities": ["卫星", "终端"]},
)
chk("T7 goal 新值覆盖", merged["goal"] == "新目标", f"goal={merged['goal']}")
chk("T7 entities 并集", sorted(merged["entities"]) == ["卫星", "终端"], f"entities={merged['entities']}")
chk("T7 旧约束保留", merged["constraints"] == ["c1"], f"constraints={merged['constraints']}")

print("== T8: execute_stream 澄清事件 ==")
with db_conn() as conn:
    cid2 = new_conv(conn, "澄清验证")
    conn.execute("UPDATE conversations SET current_intent='design' WHERE id=?", (cid2,))
    conn.commit()
events = list(pipe.execute_stream("再详细一点", cid2, dry_run=True))
clarify = [e for e in events if e.get("type") == "clarify"]
chk("T8 产生 clarify 事件", len(clarify) == 1, f"n={len(clarify)} events={[e.get('type') for e in events]}")
chk("T8 clarify 意图继承 design", clarify and clarify[0]["intent"] == "design", f"ev={clarify[:1]}")
chk("T8 clarify 含候选", clarify and len(clarify[0].get("candidates") or []) > 0, f"cands={clarify and clarify[0].get('candidates')}")

print("== T9: stage done 携带置信度 ==")
stages = [e for e in events if e.get("type") == "stage" and e.get("name") == "意图识别" and e.get("status") == "done"]
chk("T9 stage 含 route", stages and stages[0].get("route") == "inherit", f"stage={stages[:1]}")
chk("T9 stage 含 confidence", stages and stages[0].get("confidence") == 0.55, f"stage={stages[:1]}")

print("== T10: execute 结果携带意图元数据 ==")
res = pipe.execute("帮我生成设计模型的 sysml 代码", cid2, dry_run=True)
chk("T10 结果含 intent_route", res.get("intent_route") in ("rule", "cache"), f"route={res.get('intent_route')}")
chk("T10 结果含 intent_confidence", res.get("intent_confidence") == 0.95, f"conf={res.get('intent_confidence')}")
chk("T10 结果含 needs_clarification", res.get("needs_clarification") is False, f"cl={res.get('needs_clarification')}")

print(f"\n=== D12 意图识别增强验证: {PASS} 通过 / {FAIL} 失败 ===")
sys.exit(1 if FAIL else 0)
