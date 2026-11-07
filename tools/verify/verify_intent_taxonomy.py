# CI-OPTIONAL: [local] tier=local（见 tools/gates_manifest.json）：需真实服务 / 真 LLM key / 生产真库金标或随包运行时资产，CI 无服务无 secret ⇒ 跑不了；本机已实测通过，改法须同时改清单 tier 并接线，不允许只删这行
"""意图字典（intent_taxonomy）门禁 + 样本频次口径门禁（2026-11-07 米爸第 17 轮）。

## 覆盖两件事
### A 意图字典：从"只能单选"到"可新增/编辑/停用"
用户反馈「审核页只能单选意图，无法新增和编辑」。根因：意图此前**从 agents 表派生**，
中文名硬编码在前端、示例说法硬编码在 intent.py ⇒ 没有可维护实体。
现在 `intent_taxonomy` 是独立实体（canonical + 中文名 + scope note + 别名 +
示例说法 + 版本 + 弃用路径）。

### B 样本频次口径：`seen_count` 曾被回填刷高
实测 390/547 条（71%）的 `seen_count` 高于真实出现次数，
最极端的「请检索标准库并生成视图」seen=61 而 `messages` 真实 **0**（仅测试会话各 1 次）。
⇒ 用 seen_count 排序会把测试脚本刷出来的句子排到第 1。已修：回填 `bump_seen=False`，
排序改用 `real`（messages 表 role=user，排除测试会话）。

## 判据分组
  A 字典结构      —— 表存在、种子完整、canonical 唯一、状态合法
  B 字典能力      —— 别名归一化 / 示例说法并入 few-shot / 缺项自动发现
  C 频次口径      —— 虚高可检出、回填不再累加、排序用真实值
  D 契约          —— API 存在、前端有新增入口、canonical 不可改
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
DB = os.path.join(ROOT, "mbse.db")
FAILED = []


def rec(code, ok, detail=""):
    print("[%s] %-4s %s%s" % ("PASS" if ok else "FAIL", code, detail,
                              "" if ok else "  <<<"))
    if not ok:
        FAILED.append(code)


def main() -> int:
    from core import intent_taxonomy as itx
    from repositories.intent_sample_repo import IntentSampleRepo

    conn = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
    conn.row_factory = sqlite3.Row
    conn2 = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
    conn2.row_factory = sqlite3.Row

    print("=" * 80)
    print("意图字典门禁 — 结构 / 能力 / 频次口径 / 契约")
    print("=" * 80)

    # ── A 字典结构 ──
    tabs = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    rec("A1", "intent_taxonomy" in tabs, "intent_taxonomy 表存在")
    rows = itx.list_all(conn2)
    rec("A2", len(rows) >= 20, "字典 %d 条（≥20，覆盖 8 阶段 + 主流程意图）" % len(rows))
    names = [r["name"] for r in rows]
    rec("A3", len(names) == len(set(names)), "canonical 名唯一")
    bad = [r["name"] for r in rows if r["status"] not in itx.VALID_STATUS]
    rec("A4", not bad, "状态值合法（活跃/弃用/并入）%s" % bad)
    # 每个 Agent 型意图必须有中文名（否则前端只能显示英文）
    no_label = [r["name"] for r in rows if r["agent_name"] and not (r["label"] or "").strip()]
    rec("A5", not no_label, "映射了 Agent 的意图都有中文名 %s" % no_label)

    # ── B 字典能力 ──
    cases = [("需求分析", "requirement_analysis"), ("需求规格", "requirement_analysis"),
             ("变更影响", "impact"), ("预评审", "review"), ("校验", "review"),
             ("报告生成", "report_generation"), ("架构设计", "design")]
    bad2 = [(t, itx.resolve_alias(conn2, t), e) for t, e in cases
            if itx.resolve_alias(conn2, t) != e]
    rec("B1", not bad2, "别名归一化 %d 例全对 %s" % (len(cases), bad2))
    rec("B2", itx.resolve_alias(conn2, "不存在的意图XX") == "不存在的意图XX",
        "未命中别名时原样返回（不猜）")
    fs = itx.fewshot_map(conn2)
    rec("B3", len(fs) >= 6 and sum(len(v) for v in fs.values()) >= 20,
        "字典示例并入 few-shot：%d 个意图 / %d 条示例"
        % (len(fs), sum(len(v) for v in fs.values())))
    miss = itx.suggest_new_names(conn2)
    rec("B4", isinstance(miss, list), "缺项自动发现可用（当前 %d 个待补）" % len(miss))

    # ── C 频次口径 ──
    repo = IntentSampleRepo(conn)
    audit = repo.usage_audit()
    rec("C1", audit["samples"] > 0,
        "样本 %d 条，虚高 %d 条（%.0f%%）" %
        (audit["samples"], audit["inflated"], audit["inflated_ratio"] * 100))
    # 回填不再累加：代码里必须传 bump_seen=False
    src = open(os.path.join(ROOT, "repositories", "intent_sample_repo.py"),
               encoding="utf-8").read()
    rec("C2", "bump_seen=False" in src and "def upsert(" in src
        and "bump_seen: bool = True" in src,
        "upsert 支持 bump_seen，回填路径传 False（不再刷高频次）")
    real = repo.real_usage_counts()
    rec("C3", isinstance(real, dict),
        "真实频次统计可用（%d 条有真实使用记录）" % len(real))
    # 「请检索标准库并生成视图」这条实证：seen 远大于 real 时能被识别为虚高
    _row = conn.execute("SELECT text, seen_count FROM intent_samples "
                        "WHERE text LIKE '%检索标准库%'").fetchone()
    if _row:
        rec("C4", int(_row["seen_count"]) > int(real.get(_row["text"], 0)),
            "实证：该条 seen=%d / real=%d（虚高可被检出）"
            % (int(_row["seen_count"]), int(real.get(_row["text"], 0))))
    else:
        rec("C4", audit["inflated"] > 0, "存在虚高条目（原实证样本已被清理亦可）")

    # ── D 契约 ──
    rsrc = open(os.path.join(ROOT, "routers", "intent_samples.py"), encoding="utf-8").read()
    rec("D1", "/api/intent-taxonomy" in rsrc and "def create_taxonomy" in rsrc
        and "def update_taxonomy" in rsrc,
        "字典 CRUD API 存在（GET/POST/PUT）")
    js = open(os.path.join(ROOT, "static", "js", "mods", "42-intent-samples.js"),
              encoding="utf-8").read()
    rec("D2", "isNewIntent" in js and "isSaveTax" in js and "isLoadTaxonomy" in js,
        "前端有新增/编辑/停用入口")
    html = open(os.path.join(ROOT, "static", "index.html"), encoding="utf-8").read()
    rec("D3", "is-tax-form" in html and "意图字典（可新增" in html,
        "页面有意图字典维护区")
    rec("D4", "el.disabled = !!_isTaxEditName" in js or "disabled = !!_isTaxEditName" in js,
        "canonical 名编辑时锁定（新增后不可改）")
    # 示例说法真被并入 few-shot（不只是字典里存着）
    isrc = open(os.path.join(ROOT, "agent", "intent.py"), encoding="utf-8").read()
    rec("D5", "intent_taxonomy" in isrc and "fewshot_map" in isrc,
        "intent.py 真的把字典示例并入 few-shot（不是只存不用）")

    conn.close(); conn2.close()
    print("=" * 80)
    print("ALL_OK=True" if not FAILED else "FAIL %d 项：%s" % (len(FAILED), ", ".join(FAILED)))
    return 0 if not FAILED else 1


if __name__ == "__main__":
    raise SystemExit(main())
