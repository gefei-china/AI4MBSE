"""D10 LLM 智能路由（能力标签 + 优先级 + Token 预算降级）闭环验证。

T1. 标签路由：required_tags=["code"] 只命中带 code 标签的模型；无标签通配
T2. 优先级排序：同标签下 priority 高的模型被选中
T3. 预算降级：budget_tokens 已用超限 → exhausted 标记，次优模型接替
T4. 标签全不匹配：rejected 说明缺失标签，无候选
T5. chat(route_tags=...) 走智能路由，_meta 带 route_reason
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["MBSE_LLM_FORCE_MOCK"] = "1"
_TEST_DB = os.path.join(os.path.dirname(__file__), "_d10_test.db")
if os.path.exists(_TEST_DB):
    os.remove(_TEST_DB)
os.environ["MBSE_DB_PATH"] = _TEST_DB

from database import db_conn, init_db  # noqa: E402
from llm import llm_router, llm_client  # noqa: E402

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


def add_provider(conn, name, model, tags=None, priority=0, budget=0, is_default=0, key="sk-test"):
    tags = tags or []
    conn.execute(
        "INSERT INTO llm_providers (name, provider_type, base_url, api_key, model_name, "
        "model_type, max_tokens, context_window, temperature, is_default, status, tags, priority, budget_tokens) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (name, "openai", "https://example.com/v1", key, model, "chat", 8192, 8192, 0.3,
         is_default, "active", json.dumps(tags, ensure_ascii=False), priority, budget))
    conn.commit()  # 立即提交——llm_router 用新连接读取，with 块内未提交数据读不到
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


print("== T1: 标签路由 ==")
with db_conn() as conn:
    p1 = add_provider(conn, "代码模型", "code-1", tags=["code", "fast"])
    p2 = add_provider(conn, "通用模型", "general-1", tags=["chinese"], is_default=1)
    r = llm_router.route(required_tags=["code"], task_desc="生成代码")
    sel = r["selected"]
    chk("T1 命中带 code 标签模型", sel and sel["id"] == p1, f"selected={sel and (sel['id'], sel['name'])}")
    chk("T1 通配模型不误选", sel and sel["id"] != p2, f"selected={sel and sel['name']}")
    r2 = llm_router.route(task_desc="任意任务")
    chk("T1 无标签要求选默认模型", r2["selected"] and r2["selected"]["id"] == p2,
        f"selected={r2['selected'] and r2['selected']['name']}")
    chk("T1 reason 含选中说明", "选中" in r["reason"], r["reason"])

print("== T2: 优先级排序 ==")
with db_conn() as conn:
    add_provider(conn, "高优", "h", tags=["code"], priority=100)
    p2b = add_provider(conn, "中优", "m", tags=["code"], priority=50)
    add_provider(conn, "低优", "l", tags=["code"], priority=1)
    r = llm_router.route(required_tags=["code"])
    chk("T2 高优先模型选中", r["selected"] and r["selected"]["name"] == "高优",
        f"selected={r['selected'] and r['selected']['name']}")
    chk("T2 候选前三位按得分降序",
        [c["name"] for c in r["candidates"][:3]] == ["高优", "中优", "低优"],
        f"candidates={[c['name'] for c in r['candidates']]}")

print("== T3: Token 预算降级 ==")
with db_conn() as conn:
    p_budget = add_provider(conn, "预算模型", "b", tags=["code"], priority=1000, budget=100)
    p_fallback = add_provider(conn, "备用模型", "f", tags=["code"], priority=10, budget=0)
    conn.execute(
        "INSERT INTO llm_usage_stats (provider_id, provider_name, model_name, used_mock, total_tokens) "
        "VALUES (?,?,?,?,?)", (p_budget, "预算模型", "b", 0, 150))
    conn.commit()  # 预算用量立即提交，route 聚合查询才可见
    r = llm_router.route(required_tags=["code"])
    sel = r["selected"]
    chk("T3 预算耗尽被降级", sel is not None, r["reason"])
    chk("T3 最高分模型被预算剔除", sel and sel["id"] != p_budget, f"selected={sel and sel['name']}")
    chk("T3 exhausted 标记含预算模型",
        any(e["id"] == p_budget for e in r["exhausted"]), f"exhausted={r['exhausted']}")
    r_ign = llm_router.route(required_tags=["code"], ignore_budget=True)
    chk("T3 ignore_budget 恢复原选", r_ign["selected"] and r_ign["selected"]["id"] == p_budget,
        f"selected={r_ign['selected'] and r_ign['selected']['name']}")

print("== T4: 标签全不匹配 ==")
with db_conn() as conn:
    r = llm_router.route(required_tags=["vision", "agentic"])
    chk("T4 无候选", r["selected"] is None and not r["candidates"], str(r))
    chk("T4 全部被剔除（含未配置标签）",
        len(r["rejected"]) >= 5 and all(x.get("reason") for x in r["rejected"]),
        f"rejected={len(r['rejected'])}")

print("== T5: chat(route_tags) 走路由 ==")
with db_conn() as conn:
    resp = llm_client.chat([{"role": "user", "content": "写一段路由测试"}],
                           route_tags=["code"], _task_desc="生成代码")
    meta = resp.get("_meta", {})
    chk("T5 _meta 带 route_reason", meta.get("route_reason", "").startswith("智能路由"),
        str(meta.get("route_reason")))
    chk("T5 选中模型与路由一致", meta.get("provider") == "高优", f"provider={meta.get('provider')}")

print(f"\nD10 验证结果: {PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
