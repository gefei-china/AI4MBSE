# CI-OPTIONAL: [local] tier=local（见 tools/gates_manifest.json）：需真实服务 / 真 LLM key / 生产真库金标或随包运行时资产，CI 无服务无 secret ⇒ 跑不了；本机已实测通过，改法须同时改清单 tier 并接线，不允许只删这行
"""能力簇聚合门禁（2026-11-07）。

判据分组：
  A 簇定义    —— 互斥 / 代表存在 / 路由族可见性 / 族内分工可判定
  B 覆盖      —— 真库 22 个 active Agent 全归族、无陈旧映射、无错配
  C 零回归    —— cluster_route 默认 False；关闭时 detect_llm 提示词与改造前一致
  D 资产聚合  —— 技能/工具/插件按能力派生归簇，无孤儿；精简清单是只读的
  E 实证      —— A/B 结果文件存在且族化不降准确率、token 下降（防"方案空转"）

用法：python -X utf8 tools/verify/verify_capability_clusters.py [--gate]
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB = os.path.join(ROOT, "mbse.db")
AB = os.path.join(ROOT, "tools", "tmp", "intent_ab_result.json")

FAILED = []


def rec(code, ok, detail=""):
    tag = "PASS" if ok else "FAIL"
    print("[%s] %-4s %s%s" % (tag, code, detail, "" if ok else "  <<<"))
    if not ok:
        FAILED.append(code)


def main() -> int:
    from core import capability_clusters as C
    from core import config as cfg

    print("=" * 78)
    print("能力簇聚合门禁 — 8 族定义 / 22 Agent 覆盖 / 零回归 / 资产聚合 / A/B 实证")
    print("=" * 78)

    # ── A 簇定义 ──
    all_members = []
    for cid, c in C.CLUSTERS.items():
        all_members += c["members"]
    rec("A1", len(all_members) == len(set(all_members)),
        "成员互斥，共 %d 个 Agent（重复 %d）" % (len(all_members),
                                            len(all_members) - len(set(all_members))))
    rec("A2", all(C.CLUSTERS[k]["representative"] in C.CLUSTERS[k]["members"]
                  for k in C.ROUTE_CLUSTERS),
        "7 个路由族的代表都在成员表内")
    rec("A3", len(C.ROUTE_CLUSTERS) == 7
        and "orchestration" not in C.ROUTE_CLUSTERS
        and "shared" not in C.ROUTE_CLUSTERS,
        "路由可见族 = 7；主 Agent 与横切基础族不进候选池")
    cases = [
        ("requirement", "这条需求有哪些模糊词", "requirement_quality"),
        ("requirement", "把访谈记录条目化", "requirement_structuring"),
        ("requirement", "帮我定清楚验收标准", "requirement_analysis"),
        ("verify", "语法错误帮我修复", "model_validation_repair"),
        ("verify", "生成追溯矩阵找孤儿元素", "trace_verification"),
        ("knowledge", "这个平台怎么新建工程", "chat"),
        ("change", "批量替换前做安全门评估", "change_safety_gate"),
    ]
    bad = [(cid, t, C.intra_pick(cid, t), exp) for cid, t, exp in cases
           if C.intra_pick(cid, t) != exp]
    rec("A4", not bad, "族内分工 7 例确定性判据全对" if not bad else str(bad))
    rec("A5", C.resolve_cluster("requirement", "条目化一下") == "requirement_structuring"
        and C.resolve_cluster("design", "") == "design",
        "LLM 输出族 id → 具体 Agent 名（对外契约不变）")

    # ── B 真库覆盖 ──
    conn = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
    names = [r[0] for r in conn.execute(
        "SELECT name FROM agents WHERE status='active'")]
    cov = C.coverage(names)
    rec("B1", not cov["unmapped"], "22 个 active Agent 全部归族（未归族 %s）"
        % cov["unmapped"])
    rec("B2", not cov["stale_mapping"], "无指向已停用 Agent 的陈旧映射")
    rec("B3", not cov["member_mismatch"], "映射与成员表一致")

    # 视图生成：8 个已停用 Agent 由 9 个 published 技能承接（米爸第 1 问的事实核对）
    disabled_views = [r[0] for r in conn.execute(
        "SELECT name FROM agents WHERE id BETWEEN 165 AND 172 AND status='disabled'")]
    view_skills = [r[0] for r in conn.execute(
        "SELECT name FROM skills WHERE name LIKE 'sysml_view_generation_%' "
        "AND status='published'")]
    rec("B4", len(disabled_views) == 8 and len(view_skills) == 9,
        "8 个视图生成 Agent 已停用，9 个视图技能 published 承接（已停用 %d / 技能 %d）"
        % (len(disabled_views), len(view_skills)))
    pipeline = [r[0] for r in conn.execute(
        "SELECT name FROM agents WHERE id BETWEEN 634 AND 641 AND status='active'")]
    rec("B5", len(pipeline) == 8,
        "8 个建模流水线阶段 Agent 仍 active 且在被真实使用（不得误当作已卸载）")

    # ── C 零回归 ──
    # ★ 2026-11-07 第 14 轮：米爸拍板「默认打开」。判据跟着**反转**——
    #   旧判据 `is False` 会在新需求下持续假红。v2 修完阶段意图退化后才敢默认开。
    rec("C1", cfg.get("intent", "cluster_route", "MISSING") is True,
        "cluster_route 默认 True（一键回退位置 false 仍在 config schema）")
    src = open(os.path.join(ROOT, "agent", "intent.py"), encoding="utf-8").read()
    rec("C2", 'self._cfg_get("cluster_route", False)' in src
        and "if _cluster and _resolve_c and intent in _rc" in src,
        "LLM 兜底层开关与族 id→Agent 映射接线存在")
    rec("C3", "_fs_note + _gran_note" in src,
        "簇模式不注入按 Agent 名写的 few-shot / 阶段规则（否则架空族分类）")

    # ── D 资产聚合 ──
    skill_rows = [dict(name=r[0], category=r[1]) for r in conn.execute(
        "SELECT name, category FROM skills WHERE status='published'")]
    tool_rows = [dict(name=r[0], source=r[1]) for r in conn.execute(
        "SELECT name, source FROM tools WHERE status='active'")]
    plugin_rows = [dict(name=r[0], category=r[1]) for r in conn.execute(
        "SELECT name, category FROM plugins WHERE status='published'")]
    rows = skill_rows + tool_rows + plugin_rows
    groups = C.asset_clusters(rows)
    rec("D1", sum(len(v) for v in groups.values()) == len(rows),
        "%d 项资产 100%% 归簇（技能 %d / 工具 %d / 插件 %d）"
        % (len(rows), len(skill_rows), len(tool_rows), len(plugin_rows)))
    # 来源优先判据：MCP 工具按**来源**归平台族，不能靠名字（实测 70/71 名字无平台字样）
    _mcp_n = len([r for r in tool_rows if r["source"] == "mcp"])
    rec("D2", len(groups["design"]) > 0 and len(groups["shared"]) > 0,
        "设计族 %d 项、横切基础族 %d 项（簇非空）"
        % (len(groups["design"]), len(groups["shared"])))
    rec("D2b", _mcp_n > 0 and all(
            C.cluster_of_asset(r["name"], r["source"]) == "platform"
            for r in tool_rows if r["source"] == "mcp"),
        "%d 个 MCP 工具全部按来源归平台族（按名字会漏 98%%）" % _mcp_n)
    rep = C.redundancy_report(conn)
    # ★ 2026-11-07 第 14 轮：重复项已被清理（行业调研 5→1、Pre-check 4→1）。
    #   旧判据「必须能报出同名重复」在去重后必然假红 ⇒ 判据反转为
    #   「精简后同名重复应为 0」，同时保留一段"人为造重复能报出"的变异自检
    #   （否则把报告函数删了也会绿，那是空转）。
    dup_names = {x["name"] for x in rep["duplicates"]}
    rec("D3", "行业调研" not in dup_names and "Requirement_Analysis_Pre-check" not in dup_names,
        "精简后同名重复资产已清零（行业调研 5→1 / Pre-check 4→1）")
    try:
        import tempfile as _tf
        _tmp = _tf.NamedTemporaryFile(suffix=".db", delete=False)
        _tmp.close()
        _mc = sqlite3.connect(_tmp.name)
        _mc.execute("CREATE TABLE skills (name TEXT, status TEXT)")
        _mc.execute("INSERT INTO skills VALUES ('x','published')")
        _mc.execute("INSERT INTO skills VALUES ('x','published')")
        _mc.execute("CREATE TABLE plugins (name TEXT, type TEXT, status TEXT)")
        _mc.commit()
        _probe = C.redundancy_report(_mc)
        _mc.close()
        os.unlink(_tmp.name)
        rec("D3b", any(d["copies"] == 2 for d in _probe["duplicates"]),
            "变异自检：人为造 2 份同名时报告能抓到（防判据空转）")
    except Exception as e:
        rec("D3b", False, "变异自检异常：%s" % e)
    conn.close()

    # ── E A/B 实证（防方案空转）──
    if not os.path.exists(AB):
        rec("E1", False, "缺少 A/B 结果：%s（先跑 verify_intent_placement_ab.py）" % AB)
    else:
        d = json.load(open(AB, encoding="utf-8"))
        g = d.get("groups", {})
        a = g.get("dedicated/flat22", {})
        b = g.get("dedicated/cluster8", {})
        m = g.get("in_main/flat22", {})
        rec("E1", bool(a and b and m) and all(x.get("n") == d.get("gold_n")
                                              for x in (a, b, m)),
            "A/B 三组样本数一致（%s 条）" % d.get("gold_n"))
        rec("E2", b.get("acc", 0) >= a.get("acc", 0),
            "族化后准确率不降：%.1f%% → %.1f%%"
            % (a.get("acc", 0) * 100, b.get("acc", 0) * 100))
        rec("E3", b.get("avg_total_tok", 1e9) < a.get("avg_total_tok", 0),
            "族化后 token 下降：%d → %d" % (a.get("avg_total_tok", 0),
                                          b.get("avg_total_tok", 0)))
        rec("E4", m.get("avg_ms", 0) > a.get("avg_ms", 0) * 3,
            "主 Agent 内嵌决策明显更慢：%dms vs 独立分类器 %dms"
            % (m.get("avg_ms", 0), a.get("avg_ms", 0)))

    print("=" * 78)
    if FAILED:
        print("FAIL %d 项：%s" % (len(FAILED), ", ".join(FAILED)))
        return 1
    print("ALL_OK=True  —— 能力簇聚合门禁全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
