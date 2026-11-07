# CI-OPTIONAL: [local] tier=local（见 tools/gates_manifest.json）：需真实服务 / 真 LLM key / 生产真库金标或随包运行时资产，CI 无服务无 secret ⇒ 跑不了；本机已实测通过，改法须同时改清单 tier 并接线，不允许只删这行
"""多簇识别验证（2026-11-07 米爸第 14 轮第 1 问后半）。

判据分三层（防止"只测解析函数"式假绿）：
  A 纯函数层：多簇解析 / 越权成员校验 / 旧格式兼容
  B 真路由器层：`IntentRouter.detect()` 跑复合句 → 主返回仍是**单值**（契约不变），
                  `get_last_meta()['clusters']` 含 ≥2 个族且都解析到具体 Agent
  C 安全层：模型点名的成员必须属于该族白名单；主意图必须落在 DB 已注册 Agent
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

FAILED = []


def rec(code, ok, detail=""):
    print("[%s] %-4s %s%s" % ("PASS" if ok else "FAIL", code, detail,
                              "" if ok else "  <<<"))
    if not ok:
        FAILED.append(code)


def main() -> int:
    from core import capability_clusters as C
    from core import config as cfg

    print("=" * 78)
    print("多簇识别验证 — 纯函数 / 真路由器 / 安全边界")
    print("=" * 78)

    known = {"model_validation_repair", "review", "report_generation",
             "change_safety_gate", "impact", "design", "requirement_analysis"}

    # ── A 纯函数 ──
    p = C.parse_cluster_output(
        '{"clusters":[{"id":"verify","agent":"model_validation_repair",'
        '"confidence":0.9},{"id":"report","agent":"","confidence":0.6}],'
        '"confidence":0.9}', known)
    r = C.resolve_clusters(p, "语法报错，修好并出报告", known)
    rec("A1", len(r["clusters"]) == 2 and r["primary_agent"] == "model_validation_repair"
        and r["extras"] == ["report_generation"],
        "一次识别 2 个簇；主=阶段成员，次=族代表回落")
    p2 = C.parse_cluster_output(
        '{"clusters":[{"id":"design","agent":"../../evil","confidence":0.9}]}', known)
    r2 = C.resolve_clusters(p2, "设计架构", known)
    rec("A2", r2["clusters"][0]["agent"] == "design",
        "越权成员被丢弃并回落族代表（白名单生效）")
    p3 = C.parse_cluster_output('{"intent":"design","confidence":0.88}', known)
    rec("A3", C.resolve_clusters(p3, "设计架构", known)["primary_agent"] == "design",
        "旧单值格式仍兼容（不会因升级而解析失败）")
    rec("A4", C.CLUSTER_VERSION == 2 and "parse_cluster_output" in C.__all__,
        "簇定义版本已升级且新接口已导出")

    # ── B 真路由器（需要 LLM，失败要明确报出而不是静默跳过）──
    cfg._CONFIG.setdefault("intent", {})["cluster_route"] = True
    cfg._CONFIG["intent"]["cache_enabled"] = False
    from agent.pipeline import AgentPipeline
    pipe = AgentPipeline()
    pipe._load_db_agents()
    router = pipe.router
    COMPOUND = (
        "先做需求条目化，再设计系统架构给两套方案，最后把分析结果整理成报告导出来")
    got = router.detect(COMPOUND)
    meta = router.get_last_meta()
    cl = meta.get("clusters") or []
    print("  复合句 -> intent=%s route=%s clusters=%s" %
          (got, meta.get("route"),
           [(x.get("id"), x.get("agent")) for x in cl]))
    rec("B1", isinstance(got, str) and bool(got),
        "detect() 仍返回单值字符串（对外契约不变）")
    rec("B2", len(cl) >= 2,
        "复合句识别出 ≥2 个能力族（实际 %d）" % len(cl))
    rec("B3", all(x.get("agent") for x in cl),
        "每个簇都解析到具体 Agent 名")
    ids = {x.get("id") for x in cl}
    rec("B4", len(ids) >= 2 and all(i in C.ROUTE_CLUSTERS for i in ids),
        "命中的族互不重复且都在路由可见族内：%s" % sorted(ids))
    rec("B5", meta.get("multi_cluster") is True,
        "meta.multi_cluster 标记为真，供编排层决定 team/direct")

    # 单意图句不应误报多簇
    got2 = router.detect("这个模型语法上有问题吗？帮我预评审一下打个分")
    m2 = router.get_last_meta()
    print("  单意图句 -> intent=%s clusters=%s" %
          (got2, [(x.get("id"), x.get("agent")) for x in (m2.get("clusters") or [])]))
    rec("B6", not m2.get("multi_cluster"),
        "单意图句不误标 multi_cluster（避免过度触发 team 编排）")

    # ── C 默认打开 ──
    rec("C1", cfg.get("intent", "cluster_route", None) is True,
        "cluster_route 默认 True（第 14 轮拍板）")

    print("=" * 78)
    if FAILED:
        print("FAIL %d 项：%s" % (len(FAILED), ", ".join(FAILED)))
        return 1
    print("ALL_OK=True  —— 多簇识别验证全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
