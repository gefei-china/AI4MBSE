"""Glossary 术语归一化 + 域过滤 + RRF + Trace 回归测试（P0-P2 验收）。

运行：python test_glossary_routing.py
验收口径（对齐 docs/industry-research-rag-query-optimization.md）：
- P0-1：任意 glossary 词归一化后走对 intent + 强制 domain
- P0-2：sysml_norm 域检索不混入 satellite_comms（V波段不污染）
- P0-3：重复文档段去重（113/116 入库 chunk 去重）
- P0-4：召回原因回显（recall_reason 非空且含术语说明）
- P1-1：弱置信语义输入降级 chat（不硬路由）
- P1-2：RRF 融合生效（hybrid 返回 rrf=True）
- P2-2：查询 Trace 落库（query_trace 有记录）
- P2-3：低置信度 domain 分类进 review 队列
"""
import sys

# 2026-09-17 S2：同上（见 test_extended_features.py 注释）——改用 reconfigure，
# 避免重包装 stdout 导致旧包装器 GC 时关闭底层 buffer，进而打断 pytest 输出。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def main():
    from database import get_db
    from glossary import GlossaryMatcher, infer_domain
    from agent import IntentRouter
    from knowledge_engine import hybrid_search

    conn = get_db()

    print("\n=== P0-1: Glossary 归一化 ===")
    gm = GlossaryMatcher(conn)
    res = gm.resolve("v2代码规范")
    check("v2代码规范 归一化为 SysML_V2", res["normalized"] == "SysML_V2代码规范",
          res["normalized"])
    check("v2代码规范 强制 domain=sysml_norm", res["force_domain"] == "sysml_norm",
          res["force_domain"])
    check("v2代码规范 强制 intent=knowledge_qa", res["force_intent"] == "knowledge_qa",
          res["force_intent"])
    check("boost=1.8", abs(res["boost"] - 1.8) < 0.01, str(res["boost"]))
    # 泛化：新词 ibd / tms
    r_ibd = gm.resolve("ibd是什么")
    check("ibd → sysml_norm", r_ibd["force_domain"] == "sysml_norm", r_ibd["force_domain"])
    r_tms = gm.resolve("tms热管理")
    check("tms → thermal_mgmt", r_tms["force_domain"] == "thermal_mgmt", r_tms["force_domain"])
    # 保底：v波段 映射 satellite_comms（防 V2 与 V波段 混淆的兜底）
    r_vb = gm.resolve("V波段")
    check("V波段 → satellite_comms", r_vb["force_domain"] == "satellite_comms",
          r_vb["force_domain"])

    print("\n=== P0-2: domain 过滤（V波段不污染 SysML） ===")
    hits = hybrid_search(conn, "v2代码规范", top_k=5, domain="sysml_norm", glossary_boost=1.8)["hits"]
    docs = [h["source_doc"] for h in hits]
    check("sysml_norm 域命中 107", any("SysML_v2" in d for d in docs), str(docs))
    check("sysml_norm 域无 V波段 污染",
          not any("V波段" in h["content"] and "SysML" not in h["content"] for h in hits))
    check("sysml_norm 域全部命中 sysml_norm",
          all(h.get("domain") == "sysml_norm" for h in hits), str([h.get("domain") for h in hits]))

    print("\n=== P0-3: 分块去重 ===")
    for did in (113, 116):
        total, distinct = conn.execute(
            "SELECT COUNT(*) t, COUNT(DISTINCT content) d FROM document_chunks WHERE document_id=?",
            (did,)).fetchone()
        check(f"doc#{did} 去重后 total==distinct", total == distinct, f"{total} vs {distinct}")

    print("\n=== P0-4: 召回原因回显 ===")
    check("hit 带 recall_reason", all(h.get("recall_reason") for h in hits),
          str([h.get("recall_reason") for h in hits[:2]]))
    check("recall_reason 含术语说明", any("术语" in (h.get("recall_reason") or "") for h in hits))

    print("\n=== P1-1: 路由置信度分级 ===")
    r = IntentRouter()
    r.set_semantic_index([
        {"name": "knowledge_qa", "text": "知识库 资料 文档 检索 查询 知识问答 领域知识"},
        {"name": "design", "text": "方案设计 架构 设计 系统设计 方案"},
        {"name": "chat", "text": "聊天 闲聊 问候 打招呼 天气 你好 谢谢"},
    ])
    check("弱置信『随便聊聊』→ chat（不硬路由）", r.detect("随便聊聊", conn=conn) == "chat",
          r.detect("随便聊聊", conn=conn))
    check("『v2代码规范』→ knowledge_qa（glossary 强制）",
          r.detect("v2代码规范", conn=conn) == "knowledge_qa")

    print("\n=== P1-2: RRF 融合 ===")
    hy = hybrid_search(conn, "v2代码规范", top_k=5, domain="sysml_norm")
    check("hybrid 返回 rrf=True", hy.get("rrf") is True)
    check("hit 含 vec/bm25 双分", all("vec_score" in h and "bm25_score" in h for h in hy["hits"]))

    print("\n=== P2-2: 查询 Trace ===")
    from agent import GraphRAG
    GraphRAG().retrieve("v2代码规范", branch="dev")
    tr = conn.execute(
        "SELECT id, query, normalized, intent, domain, hit_count FROM query_trace "
        "WHERE query LIKE '%v2%' ORDER BY id DESC LIMIT 1").fetchone()
    check("query_trace 落库", tr is not None)
    if tr:
        check("trace 记录归一化", tr["normalized"] == "SysML_V2代码规范", tr["normalized"])
        check("trace 记录 domain=sysml_norm", tr["domain"] == "sysml_norm", tr["domain"])

    print("\n=== P2-3: domain review 队列 ===")
    q = conn.execute("SELECT COUNT(*) n FROM domain_review_queue WHERE status='pending'").fetchone()
    check("review 队列存在 pending 条目", (q["n"] or 0) > 0, str(q["n"]))
    d, s = infer_domain(filename="某临时测试文档.txt", return_score=True)
    check("未命中规则 → unknown + 低置信(0.2)", d == "unknown" and abs(s - 0.2) < 0.01, f"{d}@{s}")

    print("\n=== 工作流双通道匹配（词法+语义） ===")
    from agent import AgentPipeline
    agent = AgentPipeline()
    f_v2 = agent._match_flows("v2代码规范")
    check("v2代码规范 → 命中 SysML V2 工作流(#74)",
          any(f["id"] == 74 and "SysML" in f["name"] for f in f_v2), str(f_v2))
    f_chat = agent._match_flows("随便聊聊")
    check("随便聊聊 → 不误报工作流（语义不做独立召回）", len(f_chat) == 0, str(f_chat))
    f_react = agent._match_flows("ReAct求解")
    check("ReAct求解 → 命中 ReAct 相关工作流(#128/#130)",
          any(f["id"] in (128, 130) for f in f_react), str(f_react))
    f_need = agent._match_flows("生成SysML需求视图")
    check("生成SysML需求视图 → 命中 #74 且分数最高",
          f_need and f_need[0]["id"] == 74, str(f_need))

    conn.close()
    print(f"\n===== 结果: PASS {PASS} / FAIL {FAIL} =====")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())


# ── P0/P1b/P2 优化回归（需求质量 / 模型质量 / 知识回流）──
def main_extended():
    global PASS, FAIL
    from requirement_quality import analyze_requirements, build_quality_report
    from model_quality import check_model_quality
    from knowledge_reflow import reflow_from_text
    from database import get_db
    conn = get_db()

    print("\n=== P0: 需求质量分析 ===")
    rq = analyze_requirements("系统应快速响应。载荷必须可靠工作，传输时延不超过500ms。")
    check("模糊词检测命中", rq["stats"]["fuzzy"] >= 1, str(rq["stats"]))
    check("缺量化检测命中", rq["stats"]["no_measure"] >= 1, str(rq["stats"]))
    check("质量评分 0-100", 0 <= build_quality_report("测试需求", use_llm=False)["score"] <= 100)

    print("\n=== P1b-1: 模型质量三件套 ===")
    good = "package X { requirement def Req_A { id = 'R1' } requirement Req_A { $verify t1 } }"
    mq = check_model_quality(good)
    check("好模型约束通过", mq["checks"][0]["pass"], mq["checks"][0]["detail"])
    check("好模型 verify 识别", mq["stats"]["verify"] >= 1, str(mq["stats"]))
    bad = "package T { requirement Req_X { text='快速' } part def V { part e : Engine; } }"
    mq2 = check_model_quality(bad)
    check("差模型识别悬空引用", not mq2["checks"][2]["pass"], mq2["checks"][2]["detail"])

    print("\n=== P2-1: 知识回流 ===")
    rf = reflow_from_text(conn, "系统必须满足IP67防护等级。接口采用CAN总线通信。", source="test-reg")
    check("回流产生候选", rf["candidates"] >= 1, str(rf))
    conn.execute("DELETE FROM v2g_candidates WHERE batch_id=?", (rf["batch_id"],))
    conn.commit()

    conn.close()
    print(f"\n===== 扩展结果: PASS {PASS} / FAIL {FAIL} =====")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    # 兼容扩展：main() 走原回归；main_extended() 走新能力回归（需求质量/模型质量/知识回流）
    import os
    if os.environ.get("TEST_EXTENDED") == "1":
        sys.exit(main_extended())
    sys.exit(main())
