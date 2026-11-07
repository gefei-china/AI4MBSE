"""待审样本**高价值审核清单**（2026-11-07 米爸第 16 轮 P2）。

## 目的
真库 546 条样本里只有 48 条 confirmed（2.29 条/意图，标杆 ≥20）。
485 条 suggested 全量人工审不现实 ⇒ 按「标注性价比」排序，先审最值得的。

## 打分维度（都可解释，不用黑箱）
| 维度 | 权重 | 理由 |
|---|---|---|
| 频次 `seen_count`（取 log） | 3.0 | 同一说法越常出现，标它越划算（改一条影响多条真实请求） |
| **标签为空** | 2.5 | 系统自己没给出结论 ⇒ 这条样本零信息，标了立刻可用 |
| 判错代价高（写类/平台类意图） | 2.0 | 误判会触发真实工具调用或落库 |
| **系统自认不确定** | 1.5 | conf 0.4~0.7 / `inherit`（靠上文猜）/ `chat` 兜底但标了非 chat |
| 与现判**不一致**的强信号 | 1.5 | 空标签但 route 是 rule(0.95) ⇒ 有强信号却没落标签，值得核 |

## 输出
`docs/意图样本-高价值审核清单-YYYYMMDD.md`（人可直接照着点）
+ `tools/tmp/review_priority.json`（机读）
**只读生成，绝不改库**：确认动作仍需人工在设置页逐条/批量点（`PUT /api/intent-samples/{id}`）。
"""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 必须入 path：本脚本要 import repositories.intent_sample_repo（真实频次统计的唯一实现，
# 不复制一份 SQL —— 否则两处口径漂移）
sys.path.insert(0, ROOT)
DB = os.path.join(ROOT, "mbse.db")
OUT_MD = os.path.join(ROOT, "docs", "意图样本-高价值审核清单.md")
OUT_JSON = os.path.join(ROOT, "tools", "tmp", "review_priority.json")

#: 判错代价高的意图（误判会触发真实写/落库/MCP 调用）
HIGH_COST = {"智源对接agent", "智源数据读写agent", "model_release",
             "design", "impact", "review", "requirement_analysis"}
#: 系统"自认不确定"的 route
UNSURE_ROUTES = {"inherit", "llm_weak", "llm_first_weak", "chat",
                 "semantic_weak", "speech_act", "fused_conflict", "llm_conflict"}


def _score(row) -> tuple:
    """返回 (总分, 命中原因列表)。

    ★ 2026-11-07（第 17 轮修正）：频次改用 **`real`（messages 表真实出现次数）**，
      不再用 `seen_count`。原因（实测）：390/547 条（71%）的 seen_count 高于真实次数，
      最极端的「请检索标准库并生成视图」seen=61 而真实 **0**（全部来自测试会话）——
      用 seen_count 排序会把测试脚本刷出来的句子排到第 1，标注性价比全错。
    """
    real = int(row.get("real") or 0)
    seen = int(row["seen_count"] or 1)
    intent = (row["intent"] or "").strip()
    route = (row["hit_route"] or "").strip()
    conf = float(row["hit_conf"] or 0)
    s, why = 0.0, []

    # 真实频次（log 压缩）。real=0 说明从未被真实用户使用过 ⇒ 不加频次分。
    if real > 0:
        s += 3.0 * math.log(1 + real, 10) * 2
        if real >= 5:
            why.append("真实高频(x%d)" % real)
    else:
        why.append("无真实使用记录")
    if not intent:
        s += 2.5
        why.append("标签为空")
    if intent in HIGH_COST:
        s += 2.0
        why.append("判错代价高")
    if route in UNSURE_ROUTES or (0.4 <= conf < 0.7):
        s += 1.5
        why.append("系统不确定(%s/%.2f)" % (route or "-", conf))
    if route in ("rule", "rule_scored", "platform_read") and conf >= 0.9 and not intent:
        s += 1.5
        why.append("强信号但无标签")
    if route == "inherit" and intent not in ("", "chat"):
        s += 0.8
        why.append("靠上文猜出具体意图")
    return round(s, 2), why


def _suggest_action(row, why) -> str:
    intent = (row["intent"] or "").strip()
    if not intent:
        return "补标签（系统无结论）"
    if "系统不确定" in " ".join(why) or "靠上文猜" in " ".join(why):
        return "复核是否改判"
    return "确认或改判"


def main() -> int:
    conn = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        "SELECT id,text,intent,status,source,hit_intent,hit_route,hit_conf,"
        "seen_count,note FROM intent_samples WHERE status='suggested'")]
    # ★ 真实使用频次（messages 表，排除测试会话）—— 排序主依据，见 _score 注释
    from repositories.intent_sample_repo import IntentSampleRepo
    _repo = IntentSampleRepo(conn)
    real_counts = _repo.real_usage_counts()
    audit = _repo.usage_audit()
    for r in rows:
        r["real"] = int(real_counts.get(r["text"], 0))
    total_all = conn.execute("SELECT COUNT(*) FROM intent_samples").fetchone()[0]
    conf_n = conn.execute("SELECT COUNT(*) FROM intent_samples "
                          "WHERE status='confirmed'").fetchone()[0]
    n_agent = conn.execute("SELECT COUNT(*) FROM agents WHERE status='active'").fetchone()[0]
    conn.close()

    for r in rows:
        r["score"], r["why"] = _score(r)
        r["action"] = _suggest_action(r, r["why"])
    rows.sort(key=lambda x: -x["score"])
    top = rows[:60]

    # 预估收益：若确认前 N 条，每意图 confirmed 数如何变化
    def per_intent(n):
        return round((conf_n + n) / max(n_agent, 1), 2)
    marks = [20, 40, 60]

    os.makedirs(os.path.dirname(OUT_JSON), exist_ok=True)
    json.dump({"generated_at": datetime.now().isoformat(timespec="seconds"),
               "total_samples": total_all, "confirmed": conf_n,
               "suggested": len(rows), "agents": n_agent,
               "confirmed_per_intent": round(conf_n / max(n_agent, 1), 2),
               "top": top}, open(OUT_JSON, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)

    L = []
    L.append("# 意图样本 · 高价值审核清单")
    L.append("")
    L.append("生成时间：%s　　数据源：`mbse.db` / `intent_samples`（只读生成）" %
             datetime.now().strftime("%Y-%m-%d %H:%M"))
    L.append("")
    L.append("## 现状")
    L.append("")
    L.append("| 项 | 数值 | 标杆 | 差距 |")
    L.append("|---|---|---|---|")
    L.append("| 样本总数 | %d | — | — |" % total_all)
    L.append("| 已确认 confirmed | %d | — | — |" % conf_n)
    L.append("| 待审 suggested | %d | — | — |" % len(rows))
    L.append("| 每意图 confirmed | %.2f | ≥20 | 差 %.1f 倍 |" %
             (conf_n / max(n_agent, 1), 20 / max(conf_n / max(n_agent, 1), 0.01)))
    L.append("")
    L.append("**预估收益**（按本清单顺序确认）：")
    L.append("")
    L.append("| 确认条数 | 每意图 confirmed | 说明 |")
    L.append("|---|---|---|")
    for m in marks:
        L.append("| 前 %d 条 | %.2f | %s |" %
                 (m, per_intent(m),
                  "已达 +%.0f%%" % ((per_intent(m) / max(conf_n / max(n_agent, 1), 0.01) - 1) * 100)))
    L.append("| 全部 %d 条 | %.2f | 达标需持续多轮 |" % (len(rows), per_intent(len(rows))))
    L.append("")
    L.append("## 为什么这样排序（可解释打分）")
    L.append("")
    L.append("| 维度 | 权重 | 理由 |")
    L.append("|---|---|---|")
    L.append("| **真实频次**（`messages` 表 user 消息，排除测试会话） | 3.0 | 越常被真实使用，标它越划算 |")
    L.append("| 标签为空 | 2.5 | 系统无结论，标了立刻可用 |")
    L.append("| 判错代价高（写类/平台类） | 2.0 | 误判会触发真实工具调用或落库 |")
    L.append("| 系统自认不确定 | 1.5 | conf 0.4~0.7 / inherit / chat 兜底 |")
    L.append("| 强信号但无标签 | 1.5 | route=rule 0.95 却没落标签，值得核 |")
    L.append("")
    L.append("> **⚠️ 数据可信度告警（2026-11-07 修正）**")
    L.append("> 本轮发现 `seen_count` **不等于真实使用次数**：`import_source` 回填时曾无脑 +1，")
    L.append("> 实测 **%d / %d 条（%.0f%%）** 的 seen_count 高于真实出现次数"
             "（seen 总和 %d vs 真实 %d）。"
             % (audit["inflated"], audit["samples"], audit["inflated_ratio"] * 100,
                audit["seen_sum"], audit["real_sum"]))
    L.append("> 最极端的一条「请检索标准库并生成视图」seen=61 而**真实 0**"
             "（仅 2 个测试会话各 1 次）。")
    L.append("> 故本清单排序**已改用真实频次**；`seen_count` 仅作参考列保留。"
             "代码侧已修：回填导入不再累加 `seen_count`（`bump_seen=False`）。")
    L.append("")
    L.append("## 待审清单（前 %d 条，按优先级）" % len(top))
    L.append("")
    L.append("| # | 分数 | 原话 | 真实次数 | 系统判定 | 路由/置信 | 动作 | 原因 |")
    L.append("|---|---|---|---|---|---|---|---|")
    for i, r in enumerate(top, 1):
        txt = re.sub(r"\s+", " ", (r["text"] or ""))[:44]
        sug = (r["intent"] or "（空）")
        L.append("| %d | %.1f | %s | %d | %s | %s / %s | %s | %s |" % (
            i, r["score"], txt.replace("|", "\\|"), r.get("real", 0), sug,
            r["hit_route"] or "-", ("%.2f" % (r["hit_conf"] or 0)),
            r["action"], "、".join(r["why"]) or "-"))
    L.append("")
    L.append("## 怎么用（不自动确认）")
    L.append("")
    L.append("1. 设置页「意图样本」按 `status=suggested` 筛选，逐条核对上表；")
    L.append("2. 确认：`PUT /api/intent-samples/{id}` body `{\"status\":\"confirmed\"}`"
             "（`intent` 已有值时不必传）；改判则同时传 `{\"intent\":\"目标意图\"}`；")
    L.append("3. 改选样本（`source=override`）用 `GET /api/intent-samples/overrides`"
             " 查看、`POST .../overrides/confirm` 批量确认；")
    L.append("4. **本脚本只读**：不写库、不改标签。确认动作必须由人触发。")
    L.append("")
    L.append("> 为什么不自动确认：拿系统判定当人工标签去评测系统＝自我循环，会永远 100%。"
             "这条纪律写在 `repositories/intent_sample_repo.py` 头部。")

    open(OUT_MD, "w", encoding="utf-8").write("\n".join(L))

    print("=" * 78)
    print("高价值审核清单已生成")
    print("  %s" % OUT_MD)
    print("  %s" % OUT_JSON)
    print("  待审 %d 条，取前 %d 条；confirmed %.2f 条/意图 → 确认 60 条后 %.2f"
          % (len(rows), len(top), conf_n / max(n_agent, 1), per_intent(60)))
    print("=" * 78)
    print("ALL_OK=True")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
