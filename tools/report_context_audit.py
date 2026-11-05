# -*- coding: utf-8 -*-
"""上下文归因报表（命令行）—— 只读，随时可跑。

## 用途

`context_audit` 的数据用这个脚本看，不必等前端。典型用法：

    # 看最近 7 天 token 花在哪个块（最常用）
    ./.venv/Scripts/python.exe -X utf8 tools/report_context_audit.py

    # 看 30 天趋势 + 各开关实际触发次数
    ./.venv/Scripts/python.exe -X utf8 tools/report_context_audit.py --days 30 --all

    # 只看某个会话的逐轮明细（含归因 vs 实际计费差额）
    ./.venv/Scripts/python.exe -X utf8 tools/report_context_audit.py --conversation 123

    # 开关前后对照：导出为 JSON 便于 diff
    ./.venv/Scripts/python.exe -X utf8 tools/report_context_audit.py --json > before.json

## 输出口径（重要）

`context_audit` 只记 **system prompt 的块**，不含 messages / tools payload。
⇒ 块合计 **小于** `llm_usage_stats.prompt_tokens`，差额是 messages + tools，不是 bug。
"""
import argparse
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from database import get_db                            # noqa: E402
from repositories.context_audit_repo import ContextAuditRepo   # noqa: E402


def _bar(v: int, vmax: int, width: int = 28) -> str:
    if not vmax:
        return ""
    n = max(0, int(round(v / vmax * width)))
    return "█" * n


def print_blocks(rep: ContextAuditRepo, days: int) -> dict:
    d = rep.blocks(days=days)
    print("=" * 88)
    print(f"① 块级 token 分布（最近 {d['window_days']} 天 · {d['total_hits']} 次装配 · "
          f"{d['total_tokens']} tok）")
    print("=" * 88)
    if not d.get("ready", False):
        print(f"  ⚠️ 不可用：{d.get('hint', '')}")
        return d
    if not d["rows"]:
        print("  （无数据：该窗口内没有真实装配落库）")
        print("  ⇒ 排查顺序：① 服务启动过吗（建表在 init_db）② context_audit.enabled 是否被关"
              " ③ 窗口内是否真的有过对话")
        return d
    vmax = d["rows"][0]["tokens"] or 1
    print(f"  {'block':<20}{'层':<7}{'总tok':>9}{'占比':>7}{'次':>6}{'单次tok':>9}  分布")
    print("  " + "-" * 84)
    for r in d["rows"]:
        print(f"  {r['block']:<20}{r['layer']:<7}{r['tokens']:>9}{r['token_share']:>6.1f}%"
              f"{r['hits']:>6}{r['tok_per_hit']:>9.1f}  {_bar(r['tokens'], vmax)}")
    print("  " + "-" * 84)
    if d["layers"]:
        st = d["layers"].get("static", 0)
        dy = d["layers"].get("dynamic", 0)
        tot = st + dy
        print(f"  静态区（可缓存）{st:>8} tok ({st/tot*100:>5.1f}%)   "
              f"动态区（每轮变）{dy:>8} tok ({dy/tot*100:>5.1f}%)")
    print(f"\n  ⚠️ {d['note']}")
    return d


def print_switches(rep: ContextAuditRepo, days: int) -> dict:
    d = rep.switches(days=days)
    print("\n" + "=" * 88)
    print(f"② 各优化项**实际运行情况**（最近 {d['window_days']} 天 · "
          f"{d['summary_rows']} 条汇总记录）")
    print("=" * 88)
    if not d.get("ready", False):
        print(f"  ⚠️ 不可用：{d.get('hint', '')}")
        return d
    if not d["metrics"]:
        print("  （无指标：该窗口内没有任何带 extra 的装配，或改动均未触发）")
        return d
    # ★ 2026-10-27：按语义分两段显示。快照型求和是错的（8 个工具 × 4 轮 ≠ 32 个工具）
    _cnt = [m for m in d["metrics"] if m.get("kind") == "count"]
    _gau = [m for m in d["metrics"] if m.get("kind") != "count"]
    if _cnt:
        print("\n  ── 计数型（值为窗口内**发生次数**，累加有意义）──")
        print(f"  {'指标':<28}{'标签':<24}{'次数':>8}{'样本轮':>8}")
        for m in _cnt:
            print(f"  {m['metric']:<28}{m['label']:<24}"
                  f"{str(m['value']) + m.get('unit', ''):>8}{m['samples']:>8}")
    if _gau:
        print("\n  ── 快照型（值为**单轮**用量，故取均值；累加会翻倍失真）──")
        print(f"  {'指标':<28}{'标签':<24}{'均值':>9}{'峰值':>8}{'最近':>8}{'样本轮':>8}")
        for m in _gau:
            u = m.get("unit", "")
            print(f"  {m['metric']:<28}{m['label']:<24}"
                  f"{str(m['value']) + u:>9}{str(m['max']) + u:>8}"
                  f"{str(m['last']) + u:>8}{m['samples']:>8}")
        _sum_hint = "、".join(f"{m['metric']}={m['sum']}" for m in _gau[:2])
        if _sum_hint:
            print(f"\n  ⚠️ 快照型的 `sum`（{_sum_hint}）仅供内部核对，"
                  f"**不要当「总量」展示** —— 那是多轮相加，不是真实存量。")
    print("\n  ⇒ 读法：计数型 0/缺失 = 该能力本窗口**没被触发**（配置没开 / 前置不满足 / 场景没走到）；"
          "\n     快照型的均值才是「每轮典型用量」，用它跟配置阈值比才有意义。")
    return d


def print_daily(rep: ContextAuditRepo, days: int) -> dict:
    d = rep.daily(days=days)
    print("\n" + "=" * 88)
    print(f"③ 按日趋势（最近 {d['window_days']} 天）")
    print("=" * 88)
    if not d.get("ready", False):
        print(f"  ⚠️ 不可用：{d.get('hint', '')}")
        return d
    if not d["rows"]:
        print("  （无数据）")
        return d
    print(f"  {'日期':<12}{'轮次':>6}{'块行':>7}{'总tok':>10}{'静态':>9}{'动态':>9}{'静态比':>8}")
    print("  " + "-" * 66)
    for r in d["rows"]:
        print(f"  {r['date']:<12}{r['traces']:>6}{r['block_rows']:>7}{r['tokens']:>10}"
              f"{r['static_tokens']:>9}{r['dynamic_tokens']:>9}{r['static_ratio']:>7.1f}%")
    return d


def print_conversations(rep: ContextAuditRepo, days: int, cid: int) -> dict:
    d = rep.conversations(days=days, conversation_id=cid)
    print("\n" + "=" * 88)
    if not d.get("ready", False):
        print(f"  ⚠️ 不可用：{d.get('hint', '')}")
        return d
    if cid:
        print(f"④ 会话 {cid} 的逐轮明细（归因 vs 实际计费）")
        print("=" * 88)
        if not d["traces"]:
            print("  （无数据）")
            return d
        print(f"  {'trace':<18}{'phase':<10}{'system':>9}{'计费':>9}{'差额':>9}"
              f"{'缓存命中':>10}{'调用':>6}")
        print("  " + "-" * 74)
        for t in d["traces"]:
            print(f"  {str(t['trace_id'])[:16]:<18}{t['phase']:<10}"
                  f"{t['system_tokens']:>9}"
                  f"{(t['billed_prompt_tokens'] or 0):>9}"
                  f"{(t['delta_messages_tools'] if t['delta_messages_tools'] is not None else '-'):>9}"
                  f"{(t['cache_hit'] or 0):>10}{t['llm_calls']:>6}")
        print("\n  ⇒ 「差额」= 计费 prompt_tokens − system 块合计 = messages + tools payload。")
        print("     正常应 > 0；为负说明 trace 关联错位，需排查。")
        return d
    print(f"④ 最贵会话 TOP（最近 {d['window_days']} 天）")
    print("=" * 88)
    if not d["conversations"]:
        print("  （无数据）")
        return d
    print(f"  {'conversation':<14}{'轮次':>7}{'system tok':>12}{'块行':>7}  最近")
    print("  " + "-" * 62)
    for c in d["conversations"]:
        print(f"  {c['conversation_id']:<14}{c['traces']:>7}{c['system_tokens']:>12}"
              f"{c['block_rows']:>7}  {c['last_at']}")
    return d


def main() -> int:
    ap = argparse.ArgumentParser(description="上下文归因报表（只读）")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--all", action="store_true", help="打印全部四个视图")
    ap.add_argument("--daily", action="store_true", help="打印按日趋势")
    ap.add_argument("--conversation", type=int, default=0,
                    help="指定会话 id 看逐轮明细（0=看最贵 TOP）")
    ap.add_argument("--json", action="store_true", help="输出 JSON（便于 diff 开关前后）")
    ap.add_argument("--limit", type=int, default=40)
    args = ap.parse_args()

    conn = get_db()
    try:
        rep = ContextAuditRepo(conn)
        # 就绪自检只做一次 —— 否则四个视图各打一遍同样的提示，噪音大于信息
        ok, hint = rep.ready()
        if not ok and not args.json:
            print("=" * 88)
            print("⚠️ 上下文归因不可用")
            print("=" * 88)
            print(f"  {hint}\n")
            return 2
        if args.json:
            payload = {
                "window_days": args.days,
                "blocks": rep.blocks(days=args.days, limit=args.limit),
                "switches": rep.switches(days=args.days),
                "daily": rep.daily(days=args.days),
                "conversations": rep.conversations(
                    days=args.days, conversation_id=args.conversation),
            }
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            if not payload["blocks"].get("ready", False):
                print("⚠️ " + str(payload["blocks"].get("hint") or ""), file=sys.stderr)
                return 2
            return 0

        print_blocks(rep, args.days)
        if args.all or args.daily:
            print_daily(rep, args.days)
        if args.all:
            print_switches(rep, args.days)
            print_conversations(rep, args.days, args.conversation)
        elif args.conversation:
            print_conversations(rep, args.days, args.conversation)
        else:
            print_switches(rep, args.days)
        print()
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
