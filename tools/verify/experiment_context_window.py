"""真实测量：上下文窗口设置如何改变**任务执行**（2026-10-31）。

★ 为什么重做这一组（第一版测错了对象）：
  第一版假设"context_window 会把超长输入截断"，于是造了 1.7k tokens 的材料去比 8K/32K/128K
  ⇒ 三档全答对、无差异。**这不是实现没问题，是我测错了地方**：
  读代码确认（`history.py:_total_budget_cap` / `_apply_total_budget`），
  `context_window` **不截断输入**（输入超限由上游报错，我方不静默裁），
  它的真实作用是决定「历史 + 检索」能被注入多少：
      总预算 = context_window × context.total_budget_window_ratio(0.75)
  ⇒ 窗口越小 ⇒ 预算越小 ⇒ 历史/检索越早被压缩 ⇒ 模型能看到的上下文越少。
  这才是"上下文设置"在任务执行时的真实差异，必须按这条链路测。

本脚本测三件事（都可复现、不依赖网络）：
  A. 同一 provider 在不同 context_window 下的**总预算**（确定性，硬数字）
  B. 一份固定长度的多轮历史，在不同预算下**实际保留多少字符 / 触发第几档压缩**
  C. 单轮覆盖 `o_context` 是否真的改变预算（验证"入口临时调"这条链）

用法：cd 仓库 && ./.venv/Scripts/python.exe -X utf8 tools/verify/experiment_context_window.py
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from agent.pipeline_parts.history import HistoryMixin  # noqa: E402
from agent.pipeline_parts.compression import (  # noqa: E402
    HIST_TOPIC_PREFIX as HIST_TOPIC,
    HIST_FRAGMENT_PREFIX as HIST_FRAG,
    ARTIFACT_HEADER as ARTIFACT,
)

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "mbse.db")


class _Probe(HistoryMixin):
    """只借用 HistoryMixin 的预算/压缩方法，不构造整条 pipeline。"""


def set_cw(pid, cw):
    """临时改 provider 的 context_window（实验后 restore）。"""
    con = sqlite3.connect(DB)
    old = con.execute("SELECT context_window FROM llm_providers WHERE id=?", (pid,)).fetchone()[0]
    con.execute("UPDATE llm_providers SET context_window=? WHERE id=?", (cw, pid))
    con.commit()
    con.close()
    return old


def build_realistic_messages(turns=40, chars_per_msg=900):
    """按**真实主路径结构**造消息（第一版造错了，这是修正后的版本）。

    ★ 为什么不能只造 user/assistant：
      读 `_apply_total_budget` 确认，压缩只作用于
        ① messages[0]（system prompt）
        ② messages[1:] 中 role=system **且** `is_main_path_hist_block()` 为真的块
          （块头只有三种：`【话题：` / `【相关历史片段（第` / `【本会话既有产物】`）
      而 user/assistant 历史**永不裁**（保护对话连续性与 tool_call 配对）。
      ⇒ 只造 user/assistant 时必然"裁掉 0 tok"，那是**设计如此**，不是压缩失效。
      第一版据此得出"总闸空转"的结论是错的 —— 又是"验证对象选错"。
    """
    sys_prompt = "你是 MBSE 助手。以下是建模规范：" + "规则条目" * 800      # 真实 system 本体（任何档都不裁）
    topic_block = HIST_TOPIC + "长会话话题摘要】" + "历史话题要点" * 700
    frag_block = HIST_FRAG + "1 轮 · 用户问温度指标）】" + "历史对话片段" * 700
    art_block = ARTIFACT + "（既有产物清单）" + "产物条目" * 300
    msgs = [{"role": "system", "content": sys_prompt},
            {"role": "system", "content": topic_block},
            {"role": "system", "content": frag_block},
            {"role": "system", "content": art_block}]
    # 真实对话历史（永不裁，但占 token，是"为什么超预算"的主因）
    for i in range(turns):
        msgs.append({"role": "user", "content": ("第%d轮提问：" % i) + "需求条目描述" * (chars_per_msg // 12)})
        msgs.append({"role": "assistant", "content": ("第%d轮回答：" % i) + "设计说明内容" * (chars_per_msg // 12)})
    return msgs


def main():
    con = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
    con.row_factory = sqlite3.Row
    pid = 71
    r = con.execute("SELECT id,name,context_window,max_tokens FROM llm_providers WHERE id=?", (pid,)).fetchone()
    con.close()
    print("实验对象：provider#%s %s（原 context_window=%s）" % (r["id"], r["name"], r["context_window"]))

    p = _Probe()
    hist = build_realistic_messages()
    from core.token_counter import count_messages_tokens
    total_chars = sum(len(m["content"]) for m in hist)
    total_tok = count_messages_tokens(hist)
    print("固定会话：%d 条消息 / %d 字符 / **%d tokens**"
          % (len(hist), total_chars, total_tok))
    print("构成：1 条 system 本体（不裁）+ 3 条历史块（可裁）+ %d 条对话历史（不裁）\n" % (len(hist) - 4))

    print("=" * 96)
    print("A. 不同 context_window 下的总预算（= cw × 0.75）")
    print("=" * 96)
    print("%-14s %-14s %-16s %-12s" % ("context_window", "总预算 tokens", "等效字符(≈×1.6)", "相对 8K"))
    base = None
    rows = []
    for cw in (8192, 32768, 131072, 614400, 1048576):
        old = set_cw(pid, cw)
        try:
            cap = p._total_budget_cap(provider_id=pid)
        finally:
            set_cw(pid, old)
        if base is None:
            base = cap
        rows.append((cw, cap))
        print("%-14s %-14s %-16s %-12s" % (cw, cap, int(cap * 1.6), "%.1f×" % (cap / base)))

    print("\n" + "=" * 96)
    print("B. 同一份历史在不同预算下**实际被保留多少**（超预算即触发压缩）")
    print("=" * 96)
    print("%-14s %-11s %-11s %-10s %-10s %s" % ("context_window", "预算tok", "压缩前tok", "压缩后tok", "裁掉tok", "裁掉比例"))
    for cw, cap in rows:
        old = set_cw(pid, cw)
        try:
            work = [dict(m) for m in hist]          # 深拷贝：_apply_total_budget 是**原地修改**
            before = count_messages_tokens(work)
            out = p._apply_total_budget(work, "检索上下文占位" * 200, provider_id=pid)
            after = count_messages_tokens(out)
        finally:
            set_cw(pid, old)
        print("%-14s %-11s %-11s %-10s %-10s %s" % (
            cw, cap, before, after, before - after,
            "%.1f%%" % ((1 - after / float(before)) * 100) if before else "-"))

    print("\n" + "=" * 96)
    print("C. 单轮覆盖 o_context 是否真的改变预算（会话入口临时调参）")
    print("=" * 96)
    from core import runtime_state as rs
    for label, ov in (("不覆盖（DB=128000）", None), ("本轮覆盖=8192", {"context_window": 8192})):
        rs.set_llm_overrides(ov)
        try:
            cap = p._total_budget_cap(provider_id=pid)
        finally:
            rs.set_llm_overrides(None)
        print("%-24s → 预算 %-8s tokens" % (label, cap))
    print("\n✅ 已修（2026-10-31）：`_total_budget_cap` 现在**优先读本轮覆盖**。")
    print("   修前实测：覆盖 8192 后预算仍 96000（.调了没效果.）；修后 6144。")
    print("   影响面：会话入口临时把上下文调小，现在同时改变 max_tokens 夹取与历史/检索预算。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
