# -*- coding: utf-8 -*-
"""只读资产盘点：`skills.frontmatter` 污染 + 空作用域记忆。

**本脚本不做任何写操作**（只 SELECT）。它的产出是"要不要改、怎么改"的决策依据，
不是修改动作本身 —— 这两类数据都是**业务资产**，批量 UPDATE 前必须先看清。

## 盘点项 1：`skills.frontmatter` 被污染成整份正文

问题（实测）：
  · `frontmatter` 字段本意只存 YAML 头（约 300 字符），实测 24 个发布技能里
    **21 个** 存的是**整份正文**（最长 9,383 字符）；
  · 注入时 `skills.py:181` 是 `frontmatter[:400]` **按字符硬截** ⇒ 句子腰斩；
  · 更严重：`frontmatter` 是**旧版正文快照**，与 `content` 不同步
    ⇒ 同一段 prompt 里同时出现「轮次上限为 3」（旧）与「运行时真值，共 6 轮」（新）
    ⇒ **指令冲突**，模型无法判断以谁为准。

本盘点输出：
  · 每个技能的 frontmatter / content 长度、是否同步、截断点是否落在句中
  · 是否含"轮次/上限"这类会与运行时注入冲突的表述

## 盘点项 2：`scope_type=''` 的空作用域记忆

问题：实测 125 条 `scope_type=''` 的记忆只按 `agent_id` 过滤 ⇒ **跨项目可见**。
风险不是"看到别人数据"，而是**"该全局可见的被锁进单一项目"**（本仓
`rag.tenant_isolation` 注释里记录过同族教训）。

本盘点输出：
  · 按关键词聚类，判断这 125 条更像"全局通用经验"还是"某项目的"
  · 给出**建议动作**（保持全局 / 回填 project / 逐条人工判定），**不执行**

用法：
    ./.venv/Scripts/python.exe -X utf8 tools/inventory_assets.py
    ./.venv/Scripts/python.exe -X utf8 tools/inventory_assets.py --json > inv.json
"""
import argparse
import json
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from database import get_db                          # noqa: E402
from core.token_counter import count_tokens          # noqa: E402

#: 会与"运行时注入真值"冲突的表述（出现即说明快照是旧的/硬编码的）
_CONFLICT_PAT = [
    r"轮次上限为\s*\d+", r"轮次上限\s*\d+", r"上限为\s*\d+\s*轮",
    r"共\s*\d+\s*轮", r"第\s*\d+\s*轮就", r"预算\s*\d+",
]

#: 空作用域记忆的语义聚类（粗分，只为让 125 条可读，不做自动归类）
_CLUSTERS = [
    ("命名/格式约定", ("命名", "驼峰", "下划线", "前缀", "格式", "大小写", "规范")),
    ("建模/视图规则", ("活动图", "状态机", "视图", "元素", "关系", "端口", "包")),
    ("流程/协作偏好", ("先", "再", "确认", "流程", "步骤", "优先", "习惯")),
    ("工具/环境", ("工具", "环境", "路径", "配置", "版本", "API")),
    ("需求/追溯", ("需求", "追溯", "覆盖", "验证", "用例")),
]


def inventory_frontmatter(con) -> dict:
    rows = con.execute(
        "SELECT id, name, LENGTH(COALESCE(frontmatter,'')) fm, "
        "       LENGTH(COALESCE(content,'')) ct, frontmatter, content "
        "FROM skills WHERE enabled=1 AND status='published' ORDER BY fm DESC").fetchall()

    out = []
    for r in rows:
        fm = r["frontmatter"] or ""
        ct = r["content"] or ""
        # 是否"整份正文被塞进 frontmatter"：长度接近 content 且不是纯 YAML 头
        yaml_only = bool(re.match(r"^\s*---[\s\S]*?---\s*$", fm))
        looks_full = len(fm) > 400 and not yaml_only
        # 截断点是否落在句中（[:400] 之后既不是换行也不是句号）
        cut_at = fm[400:401] if len(fm) > 400 else ""
        mid_sentence = bool(len(fm) > 400 and cut_at not in ("", "\n", "\r", "。", "；", "！", "？"))
        # 冲突表述
        conflicts = []
        for pat in _CONFLICT_PAT:
            for m in re.finditer(pat, fm):
                conflicts.append(m.group(0))
        # 与 content 是否同步（frontmatter 应是 content 的 YAML 头 / 前缀）
        synced = bool(ct.startswith(fm[:200])) if fm and ct else False
        out.append({
            "id": r["id"], "name": r["name"],
            "frontmatter_chars": len(fm), "content_chars": len(ct),
            "frontmatter_over_400": len(fm) > 400,
            "looks_like_full_body": looks_full,
            "truncation_mid_sentence": mid_sentence,
            "in_sync_with_content": synced,
            "conflict_phrases": sorted(set(conflicts))[:6],
            "injected_chars": min(len(fm), 400),
        })
    polluted = [x for x in out if x["looks_like_full_body"]]
    conflicted = [x for x in out if x["conflict_phrases"]]
    return {
        "total_published": len(out),
        "polluted_count": len(polluted),
        "truncated_mid_sentence_count": sum(1 for x in out if x["truncation_mid_sentence"]),
        "with_conflict_phrases": len(conflicted),
        "extra_injected_tokens_est": count_tokens(
            "".join((con.execute("SELECT frontmatter FROM skills WHERE id=?", (x["id"],))
                     .fetchone()["frontmatter"] or "")[:400] for x in polluted)),
        "rows": out,
    }


def inventory_memory_scope(con) -> dict:
    cols = [r[1] for r in con.execute("PRAGMA table_info(agent_memory)")]
    rows = con.execute(
        "SELECT id, agent_id, mem_type, scope_type, scope_id, tier, "
        "       COALESCE(content,'') content, created_at FROM agent_memory "
        "WHERE COALESCE(scope_type,'')='' ORDER BY id").fetchall()

    def cls(txt):
        for name, kws in _CLUSTERS:
            if any(k in txt for k in kws):
                return name
        return "未归类"

    buckets = {}
    for r in rows:
        k = cls(r["content"])
        buckets.setdefault(k, []).append({
            "id": r["id"], "agent_id": r["agent_id"], "mem_type": r["mem_type"],
            "tier": r["tier"], "chars": len(r["content"]),
            "head": r["content"][:80],
        })
    # 总量与分布
    all_rows = con.execute(
        "SELECT COALESCE(scope_type,'') st, COUNT(*) c FROM agent_memory GROUP BY 1").fetchall()
    return {
        "columns": cols,
        "scope_distribution": {r["st"] or "(empty)": r["c"] for r in all_rows},
        "empty_scope_count": len(rows),
        "clusters": {k: {"count": len(v), "samples": v[:5]} for k, v in
                     sorted(buckets.items(), key=lambda x: -len(x[1]))},
    }


def main():
    ap = argparse.ArgumentParser(description="只读资产盘点（不改任何数据）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    con = get_db()
    try:
        fm = inventory_frontmatter(con)
        mem = inventory_memory_scope(con)
    finally:
        con.close()

    if args.json:
        print(json.dumps({"skills_frontmatter": fm, "memory_scope": mem},
                         ensure_ascii=False, indent=2))
        return 0

    print("=" * 96)
    print("盘点 1：skills.frontmatter 污染（只读，未做任何修改）")
    print("=" * 96)
    print(f"  发布技能 {fm['total_published']} 个")
    print(f"  frontmatter 存了整份正文：**{fm['polluted_count']} 个**")
    print(f"  注入时 [:400] 截断点落在句中：**{fm['truncated_mid_sentence_count']} 个**"
          f"（句子腰斩）")
    print(f"  含「轮次/上限」等会与运行时真值冲突的表述：**{fm['with_conflict_phrases']} 个**")
    print(f"  污染段注入合计约 {fm['extra_injected_tokens_est']} tok（400 字符/个的上限内）")
    print()
    print(f"  {'skill':<44}{'frontmatter':>12}{'content':>9}{'句中截断':>9}{'冲突词':>7}")
    print("  " + "-" * 88)
    for x in fm["rows"][:24]:
        if not x["frontmatter_over_400"] and not x["conflict_phrases"]:
            continue
        print(f"  {x['name'][:42]:<44}{x['frontmatter_chars']:>12}{x['content_chars']:>9}"
              f"{'是' if x['truncation_mid_sentence'] else '':>9}"
              f"{('是' if x['conflict_phrases'] else ''):>7}")
    print()
    print("  ⇒ 建议动作（**需人工确认，本脚本不执行**）：")
    print("     ① 若该字段本意只存 YAML 头 ⇒ 用 `content` 开头的 `---...---` 段重建，")
    print("        **不可**简单截 400（截断点会落在句中）；")
    print("     ② 若业务确实把它当\"正文快照\"用 ⇒ 应在 content 更新时同步刷新（当前没有），")
    print("        否则模型看到的永远是旧约束（本仓多批 skill 已改成\"以运行时为准\"，")
    print("        但 frontmatter 仍是旧文案 ⇒ 指令冲突）。")

    print()
    print("=" * 96)
    print("盘点 2：空作用域记忆（只读，未做任何修改）")
    print("=" * 96)
    print(f"  scope_type 分布：{json.dumps(mem['scope_distribution'], ensure_ascii=False)}")
    print(f"  空作用域（跨项目可见）共 {mem['empty_scope_count']} 条")
    print()
    print(f"  {'语义聚类':<18}{'条数':>6}  样本")
    print("  " + "-" * 88)
    for k, v in mem["clusters"].items():
        print(f"  {k:<18}{v['count']:>6}  {v['samples'][0]['head'][:52] if v['samples'] else ''}")
    print()
    print("  ⇒ 判断要点（本脚本不替你决定）：")
    print("     · 若某聚类里**多条都在讲同一项目的具体字段/视图** ⇒ 应回填 project 作用域；")
    print("     · 若讲的是**跨项目通用约定**（命名规范、SysML 语法纪律）⇒ 保持空作用域反而正确，")
    print("       强行回填会把\"该全局可见的\"锁进单一项目（本仓 tenant_isolation 注释记录过同族教训）。")
    print("     · ⚠️ 不要批量 UPDATE：先按上面的聚类抽样人工判定，再按类分批处理。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
