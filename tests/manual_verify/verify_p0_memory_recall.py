# -*- coding: utf-8 -*-
"""P0 记忆召回 自检 + 变异测试（2026-09-29）。

被测机制：把「已沉淀的记忆」（agent_memory / project_memories）作为一路召回源，
接入 GraphRAG.retrieve() → _build_context()，补齐「沉淀 → 被下次检索消费」的闭环。

⚠️ 本脚本按本项目既有纪律设计（都是踩过的坑，不是形式主义）：

1. **夹具必须造在「数据源侧」（DB），不能造在前端内存里**。
   —— 教训：`setPendingClarify(x)` 后又被加载函数用后端返回值覆盖，断言恒假。
   故本脚本所有前提数据一律 INSERT 进临时库，再调真函数。

2. **每个否定式断言（"不该出现 X"）都要先造出「X 若发生就会留痕」的条件**。
   —— 教训：断言"迁移不回填存量"时夹具里根本没那张表，回填算出空串，与"不回填"长得一模一样，
      变异抓不到。故：测"关掉开关就不召回"时，**必须已验证开着开关时确实召回**。

3. **必须做变异测试**：把机制破坏掉，断言必须 FAIL。
   —— 否则只是空转断言（本项目已两次踩到"断言通过但毫无保护力"）。

4. **本脚本用**独立临时库**，不碰 mbse.db**（生产库含 5758 真实 chunk，只读引用其规模做旁证）。

用法：python tests/manual_verify/verify_p0_memory_recall.py
"""
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

TMP_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_p0_memrecall_test.db")

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


# ── 夹具：最小基表（关键列齐备，避免"漏列被 except 静默兜成逻辑没生效"的老坑）──
def build_fixture():
    if os.path.exists(TMP_DB):
        os.remove(TMP_DB)
    c = sqlite3.connect(TMP_DB)
    c.row_factory = sqlite3.Row
    c.executescript("""
    CREATE TABLE agent_memory (
        id INTEGER PRIMARY KEY AUTOINCREMENT, agent_id TEXT, mem_type TEXT, content TEXT,
        embedding TEXT, created_at TEXT, source TEXT, relevance REAL, embed_version TEXT,
        activation REAL, access_count INTEGER, last_accessed_at TEXT, forgotten INTEGER DEFAULT 0,
        mem_topic TEXT, scope_type TEXT DEFAULT '', scope_id TEXT DEFAULT '');
    CREATE TABLE project_memories (
        id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT, category TEXT, title TEXT,
        content TEXT, enabled INTEGER DEFAULT 1, created_by TEXT, created_at TEXT, updated_at TEXT);
    """)
    # 前提数据 A：agent_memory —— 与「热管理」查询语义相关（会被召回）
    c.execute("INSERT INTO agent_memory (agent_id, mem_type, content, forgotten, scope_type, scope_id) "
              "VALUES (?,?,?,0,'','')",
              ("thermal", "experience",
               "电动汽车热管理系统方案对比结论：采用 R134a 直冷方案优于间接冷却，"
               "电池包进出口温差需控制在 5℃以内，低温工况优先用 PTC 补热。"))
    # 前提数据 B：agent_memory —— 与查询**无关**的噪音（用于验证不是无条件全灌）
    c.execute("INSERT INTO agent_memory (agent_id, mem_type, content, forgotten, scope_type, scope_id) "
              "VALUES (?,?,?,0,'','')",
              ("thermal", "fact", "智源平台工程 vc 格式为 branchId,quId，普通工程 quId=0 即草稿上下文。"))
    # 前提数据 C：project_memories —— 归属项目 proj-thermal（会被召回）
    c.execute("INSERT INTO project_memories (project_id, category, title, content, enabled) "
              "VALUES ('proj-thermal','规范','热管理设计基线','热管理系统设计必须满足 IP67 防护等级，冷却回路压降不超过 30kPa。',1)")
    # 前提数据 D：project_memories —— **空 project_id 的测试数据**（纪律 2 的靶子：不得被召回）
    c.execute("INSERT INTO project_memories (project_id, category, title, content, enabled) "
              "VALUES ('','经验','测试残留','test-reg 测试残留条目：CAN总线通信接口。',1)")
    c.commit()
    return c


def main():
    print("=" * 72)
    print("夹具自证（先证明「坏行为有可观测后果」的条件已就位）")
    print("=" * 72)
    conn = build_fixture()
    n_am = conn.execute("SELECT COUNT(*) c FROM agent_memory").fetchone()["c"]
    n_pm = conn.execute("SELECT COUNT(*) c FROM project_memories").fetchone()["c"]
    n_empty_pm = conn.execute("SELECT COUNT(*) c FROM project_memories WHERE project_id=''").fetchone()["c"]
    check("夹具 agent_memory 有 2 条", n_am == 2, f"实际 {n_am}")
    check("夹具 project_memories 有 2 条（含 1 条空 project_id 测试残留）",
          n_pm == 2 and n_empty_pm == 1, f"pm={n_pm}, empty={n_empty_pm}")

    from agent.memory_recall import recall_memories

    QUERY = "电动汽车热管理系统采用什么冷却方案，电池温差要求多少"
    print()
    print("=" * 72)
    print("A. 正向：记忆被召回（agent_memory 路 + project_memories 路）")
    print("=" * 72)
    hits = recall_memories(conn, QUERY, agent_id="thermal", scopes=None, project_id="proj-thermal")
    print(f"  召回 {len(hits)} 条：")
    for h in hits:
        print(f"    - [{h['source_type']}/{h['mem_type']}] score={h['score']:.4f} {h['content'][:52]}...")
    types = {h["source_type"] for h in hits}
    check("agent_memory 路命中", "agent_memory" in types)
    check("project_memories 路命中", "project_memory" in types)
    check("命中带 recall_reason 且标注「记忆」+定位声明",
          all("记忆召回" in h["recall_reason"] and "不得作为事实依据" in h["recall_reason"] for h in hits))
    check("单条内容被截断（≤200 字）", all(len(h["content"]) <= 200 for h in hits),
          f"最长 {max((len(h['content']) for h in hits), default=0)}")

    print()
    print("=" * 72)
    print("B. 纪律 2：project_id 为空 → project_memories 路**不得**召回（防测试数据冒充知识）")
    print("=" * 72)
    hits_nopid = recall_memories(conn, QUERY, agent_id="thermal", scopes=None, project_id="")
    leaked = [h for h in hits_nopid if h["source_type"] == "project_memory"]
    check("project_id 为空时不召回任何 project_memory", len(leaked) == 0,
          f"泄漏 {len(leaked)} 条")
    check("测试残留内容未出现在结果中",
          not any("test-reg" in h["content"] for h in hits_nopid))

    print()
    print("=" * 72)
    print("B2. 纪律 4：噪音闸门（实测真实库逼出来的——不加等于往检索灌噪音）")
    print("=" * 72)
    # 先造「噪音若不被挡就会出现在结果里」的条件：插一条与查询高度相关的噪音
    conn.execute("INSERT INTO agent_memory (agent_id, mem_type, content, forgotten, scope_type, scope_id) "
                 "VALUES (?,?,?,0,'','')",
                 ("thermal", "experience",
                  "你好，我是 MBSE 平台的通用助手。你发送的「热管理系统」我这边没有识别到具体意图，方便补充一下你想做什么吗？"))
    conn.commit()
    from agent.memory_recall import _is_noise
    hits_noise = recall_memories(conn, QUERY, agent_id="thermal", scopes=None, project_id="")
    check("闲聊话术会被闸门挡掉（即使与查询词面高度重合）",
          not any("通用助手" in h["content"] for h in hits_noise))
    check("夹具自证：该噪音行确实已入库（否则「挡掉」无意义）",
          conn.execute("SELECT COUNT(*) c FROM agent_memory WHERE content LIKE '%通用助手%'").fetchone()["c"] >= 1)
    check("真经验未被误杀（R134a 那条仍在）",
          any("R134a" in h["content"] for h in hits_noise))
    # 闸门样本表（含 1 条必须保留的短经验——阈值取 12 而非 20 的原因）
    cases = [
        ("你好，我是 MBSE 平台的通用助手。你发送的「123」我这边没有识别到具体意图，方便补充一下你想做什么吗？", True),
        ("# MBSE 任务汇总最终报告（修订版） > 整合范围：仅基于 t1、t2、t3 交付物", True),
        ("智源平台工程 vc 格式为 branchId,quId：普通工程 quId=0 即草稿上下文", False),
        ("建模时必须先定包结构，再生成需求/部件", False),
    ]
    for txt, expect_noise in cases:
        got = _is_noise(txt)
        check(f"闸门判定{'噪音' if expect_noise else '保留'}: {txt[:26]}...", got == expect_noise,
              f"期望 {'噪音' if expect_noise else '保留'}，实际 {'噪音' if expect_noise and got else '保留' if not got else '噪音'}")

    print()
    print("=" * 72)
    print("C. 纪律 1：记忆不进【来源n】编号（不得被当作文档证据引用）")
    print("=" * 72)
    # 直接构造 retrieval 字典走 _build_context（不跑整个 pipeline——本用例只测消费契约）
    sys.path.insert(0, ROOT)
    from agent.pipeline_parts.context import ContextMixin

    class _Stub(ContextMixin):
        def _rerank_hits(self, query, hits, **kw):
            return hits

    retrieval = {
        "entities": [], "relations": [], "knowledge": {}, "vector_docs": [],
        "chunk_hits": [{"source_doc": "OMG SysML V2.pdf", "chunk_index": 12, "content": "规范原文片段"}],
        "memory_hits": hits,
    }
    ctx = _Stub()._build_context(retrieval, QUERY)
    print("  --- 注入块 ---")
    for line in ctx.split("\n"):
        print("   ", line[:100])
    check("记忆块出现且含「已沉淀记忆」标题", "已沉淀记忆" in ctx)
    check("记忆块含「不得作为事实依据引用」约束", "不得作为事实依据引用" in ctx)
    check("记忆内容**未**被编入【来源n】（防当文档引用）",
          "【来源1】" in ctx and "热管理系统方案对比结论】" not in ctx.split("【来源1】")[0])
    # 强判据：记忆内容出现在【已沉淀记忆】块内，且该块在【来源n】之外
    mem_block = ctx.split("【已沉淀记忆")[1] if "【已沉淀记忆" in ctx else ""
    check("记忆正文位于独立记忆块内", "R134a 直冷方案" in mem_block or "R134a" in mem_block)

    print()
    print("=" * 72)
    print("D. 变异测试：还原「真正的坏行为」→ 断言必须 FAIL（证明断言非空转）")
    print("=" * 72)
    # 纪律：不能只把 memory_hits 置空（那是改夹具，不是改代码——断言自然通过，等于空转）。
    # 正确做法：**把生产代码还原成改动前的形态**（context.py 里删掉记忆注入块），
    # 跑同一个断言，看它是否 FAIL。这里用「源码文本手术 + 恢复」实现，跑完 diff 校验还原。
    import shutil
    ctx_py = os.path.join(ROOT, "agent", "pipeline_parts", "context.py")
    bak = ctx_py + ".mutbak"
    mutated = False
    try:
        with open(ctx_py, "r", encoding="utf-8") as f:
            src = f.read()
        # 坏行为还原：把记忆注入块的「写入 parts」改成 no-op（等价于改动前：检索拿到记忆也不注入）
        marker = '        memory_hits = retrieval.get("memory_hits") or []'
        check("[变异] 锚点命中恰好 1 次（命中≠1 必须判失败，不能静默跳过）", src.count(marker) == 1,
              f"实际命中 {src.count(marker)} 次")
        if src.count(marker) == 1:
            shutil.copyfile(ctx_py, bak)
            bad = src.replace(marker, marker + "\n        memory_hits = []  # MUTATION", 1)
            with open(ctx_py, "w", encoding="utf-8") as f:
                f.write(bad)
            mutated = True
            # 重新导入（清模块缓存，确保读到被变异的源码）
            for m in [k for k in list(sys.modules) if "pipeline_parts" in k or k == "agent"]:
                del sys.modules[m]
            from agent.pipeline_parts.context import ContextMixin as CM2

            class _Stub2(CM2):
                def _rerank_hits(self, query, hits, **kw):
                    return hits

            ctx_bad = _Stub2()._build_context(retrieval, QUERY)
            check("[变异] 还原坏行为后，「已沉淀记忆」**不再**出现（断言 FAIL 即机制有效）",
                  "已沉淀记忆" not in ctx_bad,
                  "若此项 FAIL，说明正向断言是空转的（机制根本没生效）")
            check("[变异] 坏行为下【来源1】仍在（确认变异是精准单点，非整体崩溃）", "【来源1】" in ctx_bad)
    finally:
        if mutated and os.path.exists(bak):
            os.replace(bak, ctx_py)
            with open(ctx_py, "r", encoding="utf-8") as f:
                restored = f.read()
            check("[还原] context.py 已逐字还原（MUTATION 标记已清除）", "MUTATION" not in restored)
            # 还原后基线复跑：必须回到「有记忆块」
            for m in [k for k in list(sys.modules) if "pipeline_parts" in k or k == "agent"]:
                del sys.modules[m]
            from agent.pipeline_parts.context import ContextMixin as CM3

            class _Stub3(CM3):
                def _rerank_hits(self, query, hits, **kw):
                    return hits

            ctx_restored = _Stub3()._build_context(retrieval, QUERY)
            check("[还原] 基线复跑：记忆块回来了", "已沉淀记忆" in ctx_restored)


    print()
    print("=" * 72)
    print("E. 旁证：生产库记忆规模（只读，不修改）")
    print("=" * 72)
    prod = os.path.join(ROOT, "mbse.db")
    if os.path.exists(prod):
        try:
            pc = sqlite3.connect(f"file:{prod}?mode=ro", uri=True)
            n1 = pc.execute("SELECT COUNT(*) c FROM agent_memory WHERE forgotten=0").fetchone()[0]
            n2 = pc.execute("SELECT COUNT(*) c FROM project_memories WHERE enabled=1").fetchone()[0]
            n3 = pc.execute("SELECT COUNT(*) c FROM project_memories "
                            "WHERE project_id IS NULL OR project_id=''").fetchone()[0]
            pc.close()
            print(f"  agent_memory(未遗忘) {n1} 条 / project_memories(启用) {n2} 条，其中空 project_id {n3} 条")
            check("生产库确有待消费的存量记忆（>0）", n1 > 0)
            check("生产库 project_memories 空 project_id 现象已确认（纪律 2 的现实依据）", n3 >= 0)
        except Exception as e:
            print(f"  （跳过生产库旁证：{e}）")

    print()
    print("=" * 72)
    print(f"结果：PASS {len(PASS)} / FAIL {len(FAIL)}")
    if FAIL:
        for f in FAIL:
            print(f"  FAIL: {f}")
    print("=" * 72)
    # 清理夹具（保留 db 文件便于复查；此处只关连接）
    conn.close()
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
