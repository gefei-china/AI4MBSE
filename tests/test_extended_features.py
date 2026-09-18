"""P0/P1b/P2 优化回归入口：需求质量 / 模型质量 / 知识回流 / 记忆合并。

运行：python test_extended_features.py
"""
import sys

# 2026-09-17 S2：原写法 `sys.stdout = io.TextIOWrapper(sys.stdout.buffer, ...)` 会重包装 stdout，
# 旧包装器被 GC 时连带关闭底层 buffer → 任何测试运行器（pytest）随后的输出都会写进已关闭的文件。
# 改用非破坏性 reconfigure（仅调整编码，不替换对象、不持有 buffer 所有权）。
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
    from requirement_quality import analyze_requirements, build_quality_report
    from model_quality import check_model_quality
    from knowledge_reflow import reflow_from_text
    from memory_service import MemoryService
    from database import get_db

    conn = get_db()

    print("\n=== P0: 需求质量分析 ===")
    rq = analyze_requirements("系统应快速响应。载荷必须可靠工作，传输时延不超过500ms。")
    check("模糊词检测命中", rq["stats"]["fuzzy"] >= 1, str(rq["stats"]))
    check("缺量化检测命中", rq["stats"]["no_measure"] >= 1, str(rq["stats"]))
    check("质量评分 0-100", 0 <= build_quality_report("测试需求", use_llm=False)["score"] <= 100)
    check("建议给出量化替代", any("500ms" in (it["suggestion"] or "") or "替换" in (it["suggestion"] or "")
                                   for it in rq["issues"]), str(rq["issues"][:1]))

    print("\n=== P1b-1: 模型质量三件套 ===")
    good = "package X { requirement def Req_A { id = 'R1' } requirement Req_A { $verify t1 } }"
    mq = check_model_quality(good)
    check("好模型约束通过", mq["checks"][0]["pass"], mq["checks"][0]["detail"])
    check("好模型 verify 识别", mq["stats"]["verify"] >= 1, str(mq["stats"]))
    bad = "package T { requirement Req_X { text='快速' } part def V { part e : Engine; } }"
    mq2 = check_model_quality(bad)
    check("差模型识别悬空引用", not mq2["checks"][2]["pass"], mq2["checks"][2]["detail"])
    check("差模型分数低于好模型", mq2["score"] < mq["score"], f"{mq2['score']} < {mq['score']}")

    print("\n=== P1b-3: 记忆合并引擎 ===")
    conn.execute("DELETE FROM agent_memory WHERE agent_id='reg-test'")
    conn.commit()
    MemoryService.deposit(conn, 'reg-test', '电池热管理采用液冷方案，流量3L/min', 'fact')
    MemoryService.deposit(conn, 'reg-test', '电池热管理是液冷，流量3升每分钟', 'fact')
    merged = MemoryService.consolidate(conn, 'reg-test', threshold=0.85)
    check("相似记忆被合并", merged >= 1, str(merged))
    conn.execute("DELETE FROM agent_memory WHERE agent_id='reg-test'")
    conn.commit()

    print("\n=== P2-1: 知识回流 ===")
    rf = reflow_from_text(conn, "系统必须满足IP67防护等级。接口采用CAN总线通信。", source="test-reg")
    check("回流产生候选", rf["candidates"] >= 1, str(rf))
    conn.execute("DELETE FROM v2g_candidates WHERE batch_id=?", (rf["batch_id"],))
    conn.commit()

    print("\n=== P1a-2: Token 预算裁剪 ===")
    from agent import AgentPipeline
    a = AgentPipeline()
    long_retr = "检索到的互联数据：" + "这是一段很长的检索数据内容" * 800
    sys_p = "角色块\n" + long_retr + "\n结尾"
    out = a._apply_context_budget(sys_p, long_retr, 10)
    check("超预算被裁剪", len(out) < len(sys_p), f"{len(out)} < {len(sys_p)}")
    check("裁剪有标记", "超预算已裁剪" in out)
    short = "短内容"
    check("未超预算不裁剪", a._truncate_budget(short, 100) == short)
    # T6：裁剪标记计入预算（裁到上限 + 标记后不得超限）
    from core.token_counter import count_tokens
    _tc = "这是一段需要被裁剪的中文长文本" * 200
    check("T6 裁剪含标记不超预算", count_tokens(a._truncate_tokens(_tc, 100, keep_head=True)) <= 100)
    check("T6 尾部保留不超预算", count_tokens(a._truncate_tokens(_tc, 100, keep_head=False)) <= 100)

    conn.close()
    print(f"\n===== 扩展回归结果: PASS {PASS} / FAIL {FAIL} =====")
    return 0 if FAIL == 0 else 1


# ── 视图投影回归（P0-视图渲染：嵌套解析 + 中文关系映射 + 引用节点）──
def main_views():
    global PASS, FAIL
    from view_generator import generate_views_from_sysml
    code = """
package '卫星通信' {
    part def 天线 { attribute 增益 : Real; }
    part def 转发器 { attribute 带宽 : Real; }
    part 卫星通信系统 {
        part 天线单元 : 天线;
        part 转发器单元 : 转发器;
    }
    requirement 支持高速率用户链路 satisfies 转发器单元
}
"""
    g = generate_views_from_sysml(code, view_types=['BDD', 'REQ'])
    views = g.get("views") or {}
    bdd = views.get("BDD", {})
    req = views.get("REQ", {})
    check("BDD 嵌套 part 解析出组合边",
          any(e["kind"] == "composition" for e in bdd.get("edges", [])),
          str([e["kind"] for e in bdd.get("edges", [])]))
    check("BDD 节点含父+子部件", len(bdd.get("nodes", [])) >= 4, str(len(bdd.get("nodes", []))))
    check("REQ satisfies 边生成",
          any(e["kind"] == "satisfy" for e in req.get("edges", [])),
          str([e["kind"] for e in req.get("edges", [])]))
    check("REQ 含被满足引用节点",
          len(req.get("nodes", [])) >= 2, str(len(req.get("nodes", []))))
    print(f"\n===== 视图投影回归: PASS {PASS} / FAIL {FAIL} =====")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "views":
        sys.exit(main_views())
    sys.exit(main())
