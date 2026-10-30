# -*- coding: utf-8 -*-
"""role 手册裁剪 A/B（只读评测，不改任何配置/数据）。

## 为什么要单独一个 A/B

`context_audit.role_manual_filter` 默认关闭，因为它是本轮**唯一改"模型可见内容"**的改动。
开关前必须回答两个问题，本脚本负责**能测的那部分**：

| 判据 | 本脚本能否测 | 说明 |
|---|---|---|
| ① Token 变化 | **能（精确）** | role / system 块 token，`token_counter` 与 provider 无关 |
| ② 要求保留率 | **能（结构化）** | 关键段是否保住、被裁段里有没有"跨工具流程约束" |
| ③ 任务正确率 | **不能** | 需真实 provider + 人工/自动判分；mock 下无意义 |
| ④ 端到端时延 | **不能** | 同上 |

⇒ 本脚本给的是 ①②；③④ 必须在真实环境按同一批任务另跑。
   **不要把①②的结论当成"质量已验证"。**

## 输出三块

1. **Token 对照**：每 Agent × query 的 role / system token（裁剪前 → 后）
2. **保留率审计**：
   - 非手册段（prose）保留率 —— 必须 100%
   - 本轮暴露工具的手册段保留率 —— 必须 100%
   - 被裁掉的段里，**含"跨工具流程/约束关键词"的有几段** —— 这是真实风险面
3. **风险清单**：把被裁掉的、且含流程约束词的段落列出来（人工过一眼就能判断要不要改开关）

用法：
    ./.venv/Scripts/python.exe -X utf8 tools/eval/ab_role_manual.py
    ./.venv/Scripts/python.exe -X utf8 tools/eval/ab_role_manual.py --json > ab.json
"""
import argparse
import json
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# ── 模拟 online 态（**只在临时副本上**，绝不改生产库）──────────────────
# 为什么需要：生产库 MCP server 是 `status='offline'`，
#   而 `bound_tools_for` 用 `status=="online"` 过滤 ⇒ 评测里根本拿不到绑定，
#   会误报"该 Agent 未绑定 MCP"（实为离线）。评测要测的是**恢复 online 后**的效果。
_SIM_NOTE = ""
if os.environ.get("AB_NO_SIMULATE") != "1":
    import shutil as _sh
    import sqlite3 as _sq
    import tempfile as _tf
    _PROD = os.path.join(_ROOT, "backups", "t-prod-20261026-122709.db")
    if os.path.exists(_PROD):
        _T = os.path.join(_tf.gettempdir(), "ab_role_manual.db")
        for _p in (_T, _T + "-wal", _T + "-shm"):
            if os.path.exists(_p):
                os.remove(_p)
        _sh.copy2(_PROD, _T)
        _con = _sq.connect(_T)
        # 生产快照可能还没跑过迁移 ⇒ 副本里补列（只在临时文件上，无副作用）
        _cols = [r[1] for r in _con.execute("PRAGMA table_info(mcp_servers)")]
        if "tool_meta" not in _cols:
            _con.execute("ALTER TABLE mcp_servers ADD COLUMN tool_meta TEXT DEFAULT ''")
            _con.commit()
        _srv = _con.execute("SELECT name, tools FROM mcp_servers WHERE status!='online'").fetchall()
        for _name, _tools in _srv:
            try:
                _arr = json.loads(_tools or "[]")
            except Exception:
                _arr = []
            _meta = [{"name": _n,
                      "description": f"智源平台接口：{_n}",
                      "inputSchema": {"type": "object",
                                      "properties": {"project": {"type": "string"},
                                                     "versionId": {"type": "string"}}}}
                     for _n in _arr if isinstance(_n, str)]
            _con.execute("UPDATE mcp_servers SET status='online', tool_meta=? WHERE name=?",
                         (json.dumps(_meta, ensure_ascii=False), _name))
            _SIM_NOTE += f"{_name}({len(_meta)} 工具) "
        _con.commit()
        _con.close()
        os.environ["MBSE_DB_PATH"] = _T
        os.environ.setdefault("MBSE_LLM_FORCE_MOCK", "1")

from core.token_counter import count_tokens                    # noqa: E402
from database import get_db                                    # noqa: E402
from agent.role_manual_filter import (                          # noqa: E402
    PROSE, collect_mcp_tool_names, compute_exposed, filter_role_manual,
    split_by_tool_names,
)

#: 跨工具流程/约束关键词 —— 出现在**被裁掉的**段里才值得警惕。
#: 依据：这些词编码"先后顺序 / 确认闸门 / 票据一致性"，往往跨多个工具生效，
#: 裁掉对应工具段会连带丢掉它们（例：`先 preview 再 apply`、`confirmationToken` 校验）。
_CROSS_CUTTING = (
    "先", "再", "然后", "之后", "确认", "confirmationToken", "confirmed",
    "必须", "禁止", "不得", "不要", "票据", "requestId", "scope", "versionId",
    "校验", "一致", "顺序", "上限", "预算", "轮次",
)

#: 评测样本（Agent → 查询）。选**会命中 MCP 技能/工具**的问法，才测得到裁剪效果。
CASES = {
    "智源对接agent": [
        "查询智源工程列表",
        "导出模型为 SysML v2",
        "帮我分析需求变更影响",
        "读取文件内容",
        "把内容写入文件",
        "预览模型变更",
    ],
    "MBSE建模总体负责人": ["查询智源工程列表", "生成活动图"],
}


def _has_cross(seg: str) -> list:
    return [k for k in _CROSS_CUTTING if k in seg]


def run_agent(con, agent_name, queries, user, min_keep, top_n):
    from agent import AgentPipeline
    p = AgentPipeline()
    c = get_db()
    try:
        p.registry.load_from_db(c, user)
    finally:
        c.close()
    bound = p.registry.get_bound_mcp(agent_name)
    all_names = collect_mcp_tool_names(bound)
    if not bound or not all_names:
        return {"agent": agent_name, "skipped": True,
                "reason": "该 Agent 未绑定 MCP（本改动对它零影响）"}

    role_off = p._build_role_block(p.registry.get(agent_name))
    rows = []
    for q in queries:
        exposed, _any = compute_exposed(bound, q, min_keep=min_keep, top_n=top_n)
        role_on, stats = filter_role_manual(role_off, all_names, exposed)

        segs = split_by_tool_names(role_off, all_names)
        prose = [(n, t) for n, t in segs if n == PROSE]
        manual = [(n, t) for n, t in segs if n != PROSE]
        kept = [(n, t) for n, t in manual if n in exposed]
        dropped = [(n, t) for n, t in manual if n not in exposed]

        # 保留率审计：裁剪后的文本里，prose 与 kept 的内容必须逐条还在
        prose_kept = sum(1 for _n, t in prose if t in role_on)
        kept_kept = sum(1 for _n, t in kept if t in role_on)
        # 风险面 A：被裁段里含跨工具流程词的比例（宽口径，仅用于圈定人工复核范围）
        risky = [(n, len(t), _has_cross(t)) for n, t in dropped if _has_cross(t)]
        # 风险面 B（**精确口径**）：被裁段**点名了本轮仍暴露的工具** ⇒ 真实的跨工具依赖损失。
        #   例：`preview_model_changes` 段写着「取得确认后 EXPORT」，
        #   若 preview 未暴露而 apply 暴露 ⇒ 模型可能直接 apply（跳过预览确认）。
        #   这比关键词共现精确得多：关键词只说明"这段讲了约束"，本判据说明"这段的约束
        #   指向的工具本轮还在用"。
        _exposed_sorted = sorted(exposed, key=len, reverse=True)
        cross_dep = []
        for _n, _t in dropped:
            _hit = [e for e in _exposed_sorted if e in _t]
            if _hit:
                cross_dep.append({"tool": _n, "chars": len(_t), "mentions": _hit})

        rows.append({
            "query": q,
            "mode": stats.get("mode"),
            "exposed_mcp": len(exposed),
            "manual_segments": len(manual),
            "kept_segments": len(kept),
            "dropped_segments": len(dropped),
            "role_tokens_before": count_tokens(role_off),
            "role_tokens_after": count_tokens(role_on),
            "role_chars_before": len(role_off),
            "role_chars_after": len(role_on),
            "prose_total": len(prose),
            "prose_kept": prose_kept,
            "kept_total": len(kept),
            "kept_kept": kept_kept,
            "dropped_with_cross_tool_words": len(risky),
            "dropped_total": len(dropped),
            "risk_samples": [{"tool": n, "chars": c_, "hits": h} for n, c_, h in risky[:5]],
            "dropped_naming_exposed": len(cross_dep),
            "cross_dep": cross_dep[:8],
        })
    return {"agent": agent_name, "skipped": False, "all_manual_segments": len(all_names),
            "role_tokens_total": count_tokens(role_off), "rows": rows}


def main():
    ap = argparse.ArgumentParser(description="role 手册裁剪 A/B（只读）")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--agent", default="", help="只测某个 Agent")
    args = ap.parse_args()

    con = get_db()
    user = {"id": 1, "username": "ab", "display_name": "AB",
            "role_name": "admin", "role": "admin"}

    # 取当前配置（不修改，只读）
    from core import config as cfg
    min_keep = int(cfg.get("context_audit", "mcp_min_keep", 8) or 0)
    top_n = int(cfg.get("context_audit", "mcp_top_n", 0) or 0)

    cases = {k: v for k, v in CASES.items() if not args.agent or k == args.agent}
    results = []
    try:
        for ag, qs in cases.items():
            results.append(run_agent(con, ag, qs, user, min_keep, top_n))
    finally:
        con.close()

    if args.json:
        print(json.dumps({"min_keep": min_keep, "top_n": top_n, "results": results},
                         ensure_ascii=False, indent=2))
        return 0

    print("=" * 96)
    print(f"role 手册裁剪 A/B（只读 · mcp_min_keep={min_keep} · mcp_top_n={top_n}）")
    if _SIM_NOTE:
        print(f"DB：临时副本（生产库仅作来源）。已模拟 online：{_SIM_NOTE.strip()}"
              f" —— 生产当前为 offline，本表反映的是\"恢复后\"的效果。")
    print("=" * 96)
    print("⚠️ 本表只覆盖 ①token ②要求保留率。**任务正确率在 mock 下无法测**，")
    print("   真实验证需另跑：同一批任务 × 开关两态 → 正确率 / 时延 / 人工抽检。")
    print()

    for r in results:
        if r.get("skipped"):
            print(f"— {r['agent']}：跳过（{r['reason']}）\n")
            continue
        print(f"■ {r['agent']}（role 全文 {r['role_tokens_total']} tok）")
        print(f"  {'query':<20}{'mode':<16}{'暴露':>5}{'保/裁':>9}"
              f"{'role tok 前→后':>20}{'降幅':>8}{'prose':>8}{'保留段':>8}{'含流程词':>9}")
        print("  " + "-" * 92)
        for x in r["rows"]:
            drop = (1 - x["role_tokens_after"] / max(1, x["role_tokens_before"])) * 100
            print(f"  {x['query'][:18]:<20}{str(x['mode']):<16}{x['exposed_mcp']:>5}"
                  f"{str(x['kept_segments']) + '/' + str(x['dropped_segments']):>9}"
                  f"{str(x['role_tokens_before']) + ' → ' + str(x['role_tokens_after']):>20}"
                  f"{drop:>7.1f}%"
                  f"{str(x['prose_kept']) + '/' + str(x['prose_total']):>8}"
                  f"{str(x['kept_kept']) + '/' + str(x['kept_total']):>8}"
                  f"{str(x['dropped_with_cross_tool_words']) + '/' + str(x['dropped_total']):>9}")
        # 保留率硬判据
        bad_prose = [x["query"] for x in r["rows"] if x["prose_kept"] != x["prose_total"]]
        bad_kept = [x["query"] for x in r["rows"] if x["kept_kept"] != x["kept_total"]]
        print(f"\n  prose 段保留率：{'✅ 100%' if not bad_prose else '❌ 丢失 ' + str(bad_prose)}")
        print(f"  暴露工具段保留率：{'✅ 100%' if not bad_kept else '❌ 丢失 ' + str(bad_kept)}")
        # 风险面
        tot_drop = sum(x["dropped_total"] for x in r["rows"])
        tot_risk = sum(x["dropped_with_cross_tool_words"] for x in r["rows"])
        print(f"  被裁段中含「流程/约束词」：{tot_risk}/{tot_drop} 段"
              f"（{tot_risk / max(1, tot_drop) * 100:.0f}%，宽口径·仅圈定复核范围）")
        tot_cd = sum(x["dropped_naming_exposed"] for x in r["rows"])
        print(f"  **被裁段点名了仍暴露的工具**：{tot_cd}/{tot_drop} 段"
              f"（精确口径·真实依赖损失）"
              f"{'  ✅ 无' if tot_cd == 0 else '  ⚠️ 需人工判断'}")
        seen = set()
        print("  精确风险样本（被裁掉、但正文点名了本轮仍暴露的工具）：")
        any_cd = False
        for x in r["rows"]:
            for s_ in x["cross_dep"]:
                if s_["tool"] in seen:
                    continue
                seen.add(s_["tool"])
                any_cd = True
                print(f"    · {s_['tool']:<32} {s_['chars']:>5} 字符"
                      f"  点名 {s_['mentions']}")
        if not any_cd:
            print("    （无）—— 被裁掉的段均未提及本轮暴露的工具，跨工具依赖无损失。")
        print()

    print("=" * 96)
    print("判读指引：")
    print("  · prose / 暴露工具段保留率 **必须 100%** —— 不 100% 说明裁剪有损，禁止开启开关。")
    print("  · 「含流程词」比例只表示**需要人工过一眼**的范围，不表示一定有害：")
    print("      - 若这些词描述的是**该工具自身**的用法（如 export 的 confirmationToken），")
    print("        而该工具本轮未被暴露 ⇒ 裁掉无害。")
    print("      - 若描述的是**跨工具顺序**（如「先 preview 再 apply」），而两个工具都在暴露集")
    print("        却只剩一个的说明 ⇒ 有信息损失，应把它补进 prose 段或调大 mcp_min_keep。")
    print("  · token 降幅大 ≠ 可以开。③任务正确率必须另行验证（本脚本不覆盖）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
