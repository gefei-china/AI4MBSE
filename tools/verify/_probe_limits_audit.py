# -*- coding: utf-8 -*-
"""_probe_limits_audit.py —— 审查所有「为省 token 设、但可能妨碍流程」的限制。

米爸口径（2026-11-11）：
> 「能不加的限制可以先不加，**不要为了省 token 导致产品流程都无法走通**」

本脚本**只读**，把context/agent 域里所有带上限/截断/预算的旋钮列出来，逐条回答：
  ·它的上限**相对真实约束**（provider 窗口）是不是合理？
  · 它在**正常请求**（远小于窗口）下会不会误伤？
  · 关掉它会怎样（能否作为逃生口）？

输出分三类：
  ✅ 合理（有据、留有余量、默认不误伤）
  ⚠️ 可疑（上限远小于窗口的 10%，或无开关）
  ★已被本批修掉
"""
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from core.config import DB_PATH  # noqa: E402


def ro():
    c = sqlite3.connect("file:%s?mode=ro" % DB_PATH.replace("\\", "/"), uri=True)
    c.row_factory = sqlite3.Row
    return c


def main():
    import core.config as cfg
    conn = ro()

    # 真实窗口基准
    row = conn.execute(
        "SELECT MIN(context_window) a, MAX(context_window) b, COUNT(*) n "
        "FROM llm_providers WHERE status='active' AND context_window>0").fetchone()
    cw_min, cw_max = int(row["a"]), int(row["b"])
    # 真实 LLM（非 embedding）provider
    llm = conn.execute(
        "SELECT id, name, context_window FROM llm_providers "
        "WHERE status='active' AND model_type<>'embedding' AND context_window>0 "
        "ORDER BY context_window ASC LIMIT 1").fetchone()
    llm_cw = int(llm["context_window"]) if llm else cw_min

    print("=" * 84)
    print("限制审查：哪些「省 token 型上限」会妨碍流程（**只读，未改任何配置**）")
    print("=" * 84)
    print("真实 LLM provider 窗口：最小 %d / 最大 %d tok（最小者=%s）"
          % (cw_min, cw_max, llm["name"] if llm else "?"))
    print("⇒ 下面凡上限 < %d tok（最小窗口的 30%%）的，一律标⚠️" % int(llm_cw * 0.3))
    print()

    items = [
        ("context", "total_budget_tokens", "总闸上限（显式配置）",
         "0=未设 ⇒ 走自适应（本批已修）"),
        ("context", "total_budget_window_ratio", "总闸上限=窗口×ratio",
         "0.75 ⇒ 留 25% 给输出（本批新增）"),
        ("context", "budget_window_tokens", "占比制分母（provider 取不到时兜底）",
         "65536"),
        ("context", "budget_retrieval_tokens", "检索区 token 预算", ""),
        ("context", "budget_history_tokens", "历史区 token 预算", ""),
        ("context", "budget_retrieval_chars", "检索区字符预算（旧键回退）", ""),
        ("context", "budget_history_chars", "历史区字符预算（旧键回退）", ""),
        ("context", "history_msg_max_chars", "单条历史消息截断", "1500 字符"),
        ("context", "history_summary_chars", "中期摘要上限", "800 字符"),
        ("context", "artifact_digest_max_chars", "产物摘要注入上限", ""),
        ("context", "model_context_chars", "建模上下文注入上限", "800 字符"),
        ("context", "total_budget_guard", "总闸总开关", "true=开/可 false 关"),
        ("sysml", "l0_card_enabled", "L0 硬约束卡开关", "true=开（≈1979 tok 固定注入）"),
    ]

    # 只有「以 token 为单位、用于**总量控制**」的旋钮才谈得上占窗口比例。
    # 首版把 ratio(0.75) 与 bool(True) 也当数字算占比 ⇒ 产出
    # 「total_budget_window_ratio 占窗口 0.0% ⚠️」这类**误报**。
    # 探针自身失真比不探更坏（会让人去"修"本来正确的配置）。
    TOTAL_KIND = {"total_budget_tokens", "total_budget_window_ratio",
                  "budget_window_tokens", "budget_retrieval_tokens",
                  "budget_history_tokens"}
    BLOCK_KIND = {"history_msg_max_chars", "history_summary_chars",
                  "model_context_chars", "artifact_digest_max_chars"}

    print("%-34s %10s %8s %s" % ("旋钮", "当前值", "占窗口", "判定"))
    print("-" * 84)
    warn = []
    for grp, key, desc, note in items:
        try:
            v = cfg.get(grp, key, None)
        except Exception:
            v = None
        if v is None:
            print("%-34s %10s %8s —" % ("%s.%s" % (grp, key), "(未设)", "—"))
            continue
        if key not in TOTAL_KIND:
            verdict = "ℹ️ 单块/开关，设计如此"
            pct = "—"
        else:
            try:
                fv = float(v)
            except Exception:
                fv = None
            pct = ("%.1f%%" % (fv / llm_cw * 100)) if (fv and llm_cw) else "—"
            if fv == 0 and key.endswith("_ratio"):
                verdict = "ℹ️ 0=关闭占比制"
            elif fv and fv < llm_cw * 0.1:
                verdict = "⚠️ 总量上限远小于窗口"
                warn.append("%s.%s = %s（仅占最小窗口 %.1f%%）"
                            % (grp, key, v, fv / llm_cw * 100))
            else:
                verdict = "✅"
        print("%-34s %10s %8s %s" % ("%s.%s" % (grp, key), v, pct, verdict))
        if note:
            print("%-34s %s" % ("", "└ " + note))

    # Agent 侧上限
    print()
    from agent.pipeline_parts.common import _AGENT_ROLE_CAP, _ATT_INJECT_CAP, _TOOL_MODEL_CAP
    print("常量类上限（agent/pipeline_parts/common.py）：")
    for name, val, desc in (
            ("_AGENT_ROLE_CAP", _AGENT_ROLE_CAP, "Agent 角色块字符上限（本批已改段级装配）"),
            ("_ATT_INJECT_CAP", _ATT_INJECT_CAP, "上传资料注入上限"),
            ("_TOOL_MODEL_CAP", _TOOL_MODEL_CAP, "工具结果回填模型侧上限")):
        print("  %-18s = %6d  %s" % (name, val, desc))
    print("  ⚠️ 这三个都以**字符**计，与 provider 的 token 窗口无联动；")
    print("     其中 _AGENT_ROLE_CAP 已改段级装配（不再腰斩），另两个仍是硬截断。")

    print()
    print("=" * 84)
    if warn:
        print("⚠️ 可疑上限（建议放开或按窗口联动）：")
        for w in warn:
            print("   ·", w)
    else:
        print("✅ 未发现「远小于窗口」的硬上限（除已修的 total_budget_tokens）")
    print("=" * 84)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()