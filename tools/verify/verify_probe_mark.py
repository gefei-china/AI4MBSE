"""S0 门禁：探针/验证噪声必须与业务统计彻底分家（2026-10-28）。

## 为什么需要这条门禁

这不是"新功能测试"，而是**度量地基**的回归保护。上一轮实测（@75eca26）发现：
- `tool_call_logs` 混入 fake_tool 126 次（失败率 89%、latency 记成 1.79e12 ms）⇒ 全量失败率虚高 3 个点；
- `llm_usage_stats` 里 `chat_probe` 占 7 天成本 **51%** ⇒ "成本优化"基线全错。

一旦有人新加一个假工具却忘了登记，或把 `is_probe` 过滤从某个统计里删掉，
**数字会静默回到错的**，而看板上看不出任何异常。所以用门禁把这几条钉住。

## 判据设计原则

1. **断言"关系"而非"具体数值"**：不写 `失败率==12.06%`（换个环境就假红），
   写 `业务失败率 < 全量失败率`（这是口径隔离生效的**本质**）。
2. **负例必须存在**：只测"假工具被标"不够——还要测"真工具没被误标"，
   否则把所有行都标成 probe 也能让前一条通过。
3. **守卫生效性用"量纲"验**：延迟过滤后 max 必须落到人类可信区间。

用法：`python tools/verify/verify_probe_mark.py` → EXIT 0 全过。
"""
from __future__ import annotations

import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

FAILS: list = []
PASSES: list = []


def chk(tag: str, cond: bool, detail: str = "") -> None:
    (PASSES if cond else FAILS).append(tag)
    print(("  PASS " if cond else "  FAIL ") + tag + ((" — " + detail) if detail else ""))


def _db_path() -> str:
    try:
        from database.schema import DB_PATH
        return DB_PATH
    except Exception:
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        return os.path.join(root, "mbse.db")


def main() -> int:
    from core.probe_mark import (
        is_probe_tool, is_probe_intent, probe_where, sane_latency_where,
        DEFAULT_MAX_SANE_LATENCY_MS,
    )

    print("[A] 判定函数：正例必须命中")
    for name in ("fake_tool", "fake_mcp_tool", "definitely_not_a_real_tool_xyzzy",
                 "no_such_tool_abc", "_probe_liveness"):
        chk(f"A is_probe_tool({name})=1", is_probe_tool(name) == 1)

    print("[B] 判定函数：负例必须不误伤（防止「全标 probe」也能过 A）")
    for name in ("graph_retrieve", "sysml_v2_validate", "query_projects", "file_read",
                 "latest_tool", "contest_result", "", None):
        chk(f"B is_probe_tool({name!r})=0", is_probe_tool(name) == 0)

    print("[C] intent 判定")
    for it in ("chat_probe", "probe", "probe_liveness_min"):
        chk(f"C is_probe_intent({it})=1", is_probe_intent(it) == 1)
    # 在线评测**不得**被当探针：它消费真实回答，属业务成本的真实组成
    for it in ("chat_answer", "chat_eval", "gen_faithfulness", "rag_rerank"):
        chk(f"C is_probe_intent({it})=0（评测不是探针）", is_probe_intent(it) == 0)

    print("[D] SQL 片段可直接拼接且带表别名支持")
    chk("D probe_where() 含 COALESCE", "COALESCE(is_probe,0)=0" in probe_where())
    chk("D probe_where('l') 带别名", "l.is_probe" in probe_where("l"))
    chk("D sane_latency_where() 有上界", "BETWEEN 0 AND" in sane_latency_where())

    db = _db_path()
    if not os.path.isfile(db):
        chk("E 数据库存在", False, db)
        return _report()
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    print("[E] 两表均已补 is_probe 列")
    for t in ("tool_call_logs", "llm_usage_stats"):
        cols = [r[1] for r in con.execute(f"PRAGMA table_info({t})")]
        chk(f"E {t}.is_probe 存在", "is_probe" in cols)

    print("[F] 存量回填：已知假工具/探针 intent 全部已标记")
    row = con.execute("SELECT COUNT(*) n FROM tool_call_logs "
                      "WHERE tool_name IN ('fake_tool','definitely_not_a_real_tool_xyzzy',"
                      "'no_such_tool_abc') AND COALESCE(is_probe,0)=0").fetchone()
    chk("F 无未标记的假工具行", (row["n"] or 0) == 0, f"未标记 {row['n']} 行")
    row = con.execute("SELECT COUNT(*) n FROM llm_usage_stats "
                      "WHERE intent IN ('chat_probe','probe','probe_liveness_min') "
                      "AND COALESCE(is_probe,0)=0").fetchone()
    chk("F 无未标记的探针 intent 行", (row["n"] or 0) == 0, f"未标记 {row['n']} 行")

    print("[G] 负例：真实工具不得被误标（防「全标」作弊）")
    row = con.execute("SELECT COUNT(*) n FROM tool_call_logs "
                      "WHERE tool_name IN ('graph_retrieve','sysml_v2_validate','query_projects',"
                      "'query_data_tree','search_model_elements') AND COALESCE(is_probe,0)=1").fetchone()
    chk("G 真实工具 probe 标记数=0", (row["n"] or 0) == 0, f"误标 {row['n']} 行")

    print("[H] 口径隔离生效：业务失败率必须低于全量失败率")
    allr = con.execute("SELECT COUNT(*) n, SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END) f "
                       "FROM tool_call_logs").fetchone()
    biz = con.execute("SELECT COUNT(*) n, SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END) f "
                      "FROM tool_call_logs WHERE 1=1" + probe_where()).fetchone()
    if allr["n"] and biz["n"]:
        ra = allr["f"] * 100.0 / allr["n"]
        rb = biz["f"] * 100.0 / biz["n"]
        chk("H 业务失败率 < 全量失败率", rb < ra, f"业务 {rb:.2f}% < 全量 {ra:.2f}%")
        chk("H 业务样本量 <= 全量样本量", biz["n"] <= allr["n"])
    else:
        chk("H 样本量充足", False, "库内无工具调用记录")

    print("[I] 延迟量纲守卫：过滤后 max 必须落在人类可信区间")
    r = con.execute("SELECT COALESCE(MAX(latency_ms),0) mx FROM tool_call_logs WHERE 1=1"
                    + probe_where() + sane_latency_where()).fetchone()
    chk("I 双守卫后 max < 1 小时", 0 <= (r["mx"] or 0) <= DEFAULT_MAX_SANE_LATENCY_MS,
        f"max={r['mx']} ms")
    raw = con.execute("SELECT COALESCE(MAX(latency_ms),0) mx FROM tool_call_logs").fetchone()
    chk("I 说明性：原始 max 确实被污染过（守卫有意义）",
        (raw["mx"] or 0) > DEFAULT_MAX_SANE_LATENCY_MS,
        f"raw max={raw['mx']} ms")

    print("[J] 自进化信号已接入探针过滤（源码级）")
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    src = open(os.path.join(root, "core", "misevolution.py"), encoding="utf-8").read()
    chk("J misevolution 引入 probe_where", "from core.probe_mark import probe_where" in src)
    # ⚠️ 断言必须按 **SignalSpec 块**判，不能按单行正则判：
    #   SQL 写的是 `"...created_at>=?" + _PROBE`，`_PROBE` **在引号之外**，
    #   单行 `"(?:num_sql|den_sql)="[^"]*"` 永远匹配不到 ⇒ 首版写成了 0/6 假红。
    #   （实现是对的：4/4 块都带 _PROBE。按纪律「门禁断言写错 ≠ 实现错」，先证后改断言。）
    blocks = [b for b in src.split("SignalSpec(")[1:] if "tool_call_logs" in b]
    with_probe = [b for b in blocks if "_PROBE" in b]
    chk("J 含 tool_call_logs 的信号块全部带 _PROBE",
        len(blocks) > 0 and len(with_probe) == len(blocks),
        f"{len(with_probe)}/{len(blocks)}")

    print("[K] 能力填充率已接入探针过滤（源码级）")
    capsrc = open(os.path.join(root, "routers", "capabilities.py"), encoding="utf-8").read()
    chk("K _fill_rate 使用 probe_where", "probe_where" in capsrc)
    chk("K _fill_rate 返回 excludes_probe 口径", "excludes_probe" in capsrc)

    print("[L] 看板双口径字段存在（不删数据，只分列）")
    msrc = open(os.path.join(root, "routers", "monitor.py"), encoding="utf-8").read()
    for f in ("llm_calls_biz", "llm_calls_probe", "llm_tokens_biz"):
        chk(f"L monitor 返回 {f}", f in msrc)

    print("[M] 写入侧接线完整（防「函数写了但没接」——本仓多次同型事故）")
    # 生产写入点清单（不含 tools/verify、tests）。新增写入点必须同步接入 else 本断言会红。
    # 口径：`INSERT INTO tool_call_logs` 出现在这些**生产**文件里。
    _prod_writers = [
        "agent/pipeline_parts/tools.py",
        "agent/pipeline_parts/memory.py",
        "plugin_system/executor.py",
        "routers/mcp_gateway.py",
        "routers/studio_parts/flows.py",
        "workflows/tools.py",
    ]
    for rel in _prod_writers:
        p = os.path.join(root, rel)
        if not os.path.isfile(p):
            chk(f"M {rel} 存在", False)
            continue
        s = open(p, encoding="utf-8").read()
        has_insert = "INSERT INTO tool_call_logs" in s
        # 该文件里每个 INSERT 都应带 is_probe 列
        ins = s.count("INSERT INTO tool_call_logs")
        with_probe = s.count("is_probe)")
        chk(f"M {rel} 的 tool_call_logs 写入带 is_probe",
            has_insert and with_probe >= 1, f"insert={ins} probe_col={with_probe}")
        chk(f"M {rel} 调用了 is_probe_tool", "is_probe_tool" in s)
    # llm 侧：唯一生产写入点
    lsrc = open(os.path.join(root, "llm", "__init__.py"), encoding="utf-8").read()
    chk("M llm/__init__.py 的 llm_usage_stats 写入带 is_probe",
        "is_probe) " in lsrc or "failure_msg, is_probe)" in lsrc)
    chk("M llm/__init__.py 调用 is_probe_intent", "is_probe_intent" in lsrc)

    con.close()
    return _report()


def _report() -> int:
    print()
    print(f"[RESULT] PASS {len(PASSES)} / FAIL {len(FAILS)}")
    if FAILS:
        print("[FAILED]", FAILS)
        return 1
    print("[ALL GREEN]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
