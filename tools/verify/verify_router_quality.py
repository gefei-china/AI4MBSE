# CI-OPTIONAL: [local] tier=local（见 tools/gates_manifest.json）：需真实服务 / 真 LLM key / 生产真库金标或随包运行时资产，CI 无服务无 secret ⇒ 跑不了；本机已实测通过，改法须同时改清单 tier 并接线，不允许只删这行
"""代码级 Router 质量门禁（2026-11-07 米爸第 14 轮第 4 问）。

## 定位
标杆（Gartner 2025 / Vertext Labs / Eucalipse）一致要求：
「**每天看置信分数分布**是路由退化的领先指标」「observability is non-negotiable」。
本门禁把下列可量化指标固化成可重复运行的判据，而不是一次性测完就丢：

  A 校准       —— 读 verify_router_calibration.py 的实测结果，报 ECE / 高置信档准确率
  B 数据健康   —— confirmed 样本覆盖率、每意图样本数、suggested 积压（80% 质量靠数据）
  C 结构       —— 簇覆盖、默认开关、多簇接线、Agent 去重终态、技能去重终态
  D 防倒退     —— 已验证净负的优化不得被偷偷打开（负例段）

## 为什么校准用 WARN 不用 FAIL
ECE=0.182 / 高置信档 85.7% 是**当前既成事实**，不是回归。
把它做成 FAIL 会让门禁从上线第一天就红 ⇒ 失去信号价值（既有纪律：
"需求被否时判据要反转，不是删掉"）。故：
  · 校准值只做观测 + 越界 WARN；
  · 但**结果文件缺失 / 样本数不足 / 结论被篡改**是 FAIL（防门禁空转）。

用法：python -X utf8 tools/verify/verify_router_quality.py [--gate]
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

CALIB = os.path.join(ROOT, "tools", "tmp", "router_calibration.json")
SHADOW = os.path.join(ROOT, "tools", "tmp", "calibration_shadow_result.json")
NEGAB = os.path.join(ROOT, "tools", "tmp", "negative_examples_ab.json")
TIERS = os.path.join(ROOT, "tools", "tmp", "threshold_tiers_ab.json")
DB = os.path.join(ROOT, "mbse.db")

FAILED, WARNED = [], []


def rec(code, ok, detail="", warn_only=False):
    if ok:
        print("[PASS] %-4s %s" % (code, detail))
    elif warn_only:
        print("[WARN] %-4s %s" % (code, detail))
        WARNED.append(code)
    else:
        print("[FAIL] %-4s %s  <<<" % (code, detail))
        FAILED.append(code)


def main() -> int:
    from core import config as cfg
    import core.capability_clusters as C
    print("=" * 80)
    print("代码级 Router 质量门禁 — 校准 / 数据健康 / 结构 / 防倒退")
    print("=" * 80)

    # ── A 校准（观测 + 防空转）──
    if not os.path.exists(CALIB):
        rec("A1", False, "缺少校准实测结果，请先跑 verify_router_calibration.py")
    else:
        d = json.load(open(CALIB, encoding="utf-8"))
        rows = d.get("rows") or []
        rec("A1", len(rows) >= 15, "校准样本 %d 条（≥15 才有效）" % len(rows))
        ece = float(d.get("ece") or 0)
        hi = [x for x in rows if float(x.get("conf") or 0) >= 0.90]
        acc_hi = (sum(x["hit"] for x in hi) / len(hi)) if hi else 0.0
        rec("A2", ece <= 0.10,
            "ECE=%.3f（>0.10 表示阈值不可信；当前为既成事实，非回归）" % ece,
            warn_only=True)
        rec("A3", acc_hi >= 0.97,
            "高置信档(≥0.90) 准确率 %.1f%%（行业要求 >97%%）" % (acc_hi * 100),
            warn_only=True)
        # 结果文件必须真含错例，否则可能是"只测容易样本"的假绿
        rec("A4", any(not x["hit"] for x in rows),
            "校准集含真实错例（防止只挑容易样本刷高准确率）")

    # ── B 数据健康 ──
    conn = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
    total = conn.execute("SELECT COUNT(*) FROM intent_samples").fetchone()[0]
    conf = conn.execute("SELECT COUNT(*) FROM intent_samples WHERE status='confirmed'").fetchone()[0]
    sugg = conn.execute("SELECT COUNT(*) FROM intent_samples WHERE status='suggested'").fetchone()[0]
    n_agent = conn.execute("SELECT COUNT(*) FROM agents WHERE status='active'").fetchone()[0]
    rec("B1", total > 0, "意图样本池 %d 条（confirmed %d / suggested %d）"
        % (total, conf, sugg))
    per_intent = conf / max(n_agent, 1)
    # ⚠️ 首版把判据写成 `conf >= n_agent*2`，48>=42 就直接 PASS，
    #    把「2.3 条/意图、远低于标杆 ≥20」伪装成达标。判据必须与**结论语义**一致：
    #    达标才 PASS，不达标 WARN（既成事实，不做 FAIL）。
    rec("B2", per_intent >= 20,
        "confirmed 覆盖率 %.1f 条/意图（标杆 ≥20；当前 %d 条 / %d 个 Agent）"
        % (per_intent, conf, n_agent), warn_only=True)
    rec("B3", sugg == 0,
        "suggested 积压 %d 条 ⇒ 审核闭环存在缺口（运营项，非回归）" % sugg,
        warn_only=True)
    rec("B4", conf <= total and sugg <= total, "样本状态计数自洽")

    # ── C 结构 ──
    rec("C1", cfg.get("intent", "cluster_route", None) is True,
        "cluster_route 默认 True（第 14 轮拍板）")
    names = [r[0] for r in conn.execute(
        "SELECT name FROM agents WHERE status='active'")]
    cov = C.coverage(names)
    rec("C2", not cov["unmapped"] and not cov["stale_mapping"],
        "%d 个 active Agent 全归族，0 陈旧映射" % len(names))
    rec("C3", "工程建模方案设计Agent" not in names,
        "重复 Agent 已合并，只保留 design")
    sk = conn.execute("SELECT COUNT(*) FROM skills WHERE name='行业调研'").fetchone()[0]
    pre = conn.execute("SELECT COUNT(*) FROM skills "
                       "WHERE name='Requirement_Analysis_Pre-check'").fetchone()[0]
    rec("C4", sk == 1 and pre == 1,
        "技能去重终态：行业调研 %d 份 / Pre-check %d 份" % (sk, pre))
    src = open(os.path.join(ROOT, "agent", "intent.py"), encoding="utf-8").read()
    rec("C5", "score_clusters" in src and "_last_cluster_hits" in src,
        "确定性多簇信号已接线（不依赖 LLM 兜底）")
    rec("C6", "multi_cluster" in src and "clusters_for_llm_detailed" in src,
        "多簇输出 + 族内成员白名单已接线")
    rec("C7", "calibrated_confidence" in src and "core.route_calibration" in src,
        "shadow 校准写 meta 已接线（不改判定）")
    conn.close()

    # ── E 第 15 轮新能力（真实数据验证结果）──
    if not os.path.exists(SHADOW):
        rec("E1", False, "缺真实数据校准结果，请先跑 verify_calibration_shadow.py")
    else:
        sh = json.load(open(SHADOW, encoding="utf-8"))
        rec("E1", sh.get("n", 0) >= 40,
            "真实 confirmed 样本 %d 条跑过校准回放" % sh.get("n", 0))
        rec("E2", sh.get("ece_calibrated", 1) <= sh.get("ece_raw", 0),
            "真实数据上 ECE 不升：%.4f → %.4f" %
            (sh.get("ece_raw", 0), sh.get("ece_calibrated", 0)))
        rec("E3", sh.get("accuracy", 0) > 0,
            "真实数据准确率 %.1f%%（shadow 不改判定）" %
            (sh.get("accuracy", 0) * 100))
        # 校准表必须来自真库回放，不是空 dict
        rec("E4", len(sh.get("reliability") or {}) >= 5,
            "按 route 真实命中率表 %d 项（非拍脑袋常量）" %
            len(sh.get("reliability") or {}))
    # 样本回流闭环接线
    fb = os.path.join(ROOT, "services", "intent_override_feedback.py")
    rec("E5", os.path.exists(fb) and "record_override" in open(fb, encoding="utf-8").read(),
        "改选回流模块存在")
    conv = open(os.path.join(ROOT, "routers", "conversations.py"),
                encoding="utf-8").read()
    rec("E6", conv.count("ui_intent_override") >= 2,
        "流式 + 非流式两个入口都透传改选标记")
    rec("E7", cfg.get("intent", "threshold_tiers", None) is False,
        "P1 分档阈值默认关（真库 A/B 未证明净正前不打开）")
    if os.path.exists(TIERS):
        ab = json.load(open(TIERS, encoding="utf-8"))
        rec("E8", ab.get("decision") in ("keep_off", "consider_on"),
            "分档 A/B 已跑，决策=%s" % ab.get("decision"))

    # ── D 防倒退：净负优化不得上线 ──
    if os.path.exists(NEGAB):
        ab = json.load(open(NEGAB, encoding="utf-8"))
        base, neg = ab.get("base", {}), ab.get("neg", {})
        rec("D1", neg.get("acc", 1) <= base.get("acc", 0),
            "负例 A/B 实测为净负（%.1f%% vs %.1f%%），不接入生产提示词"
            % (neg.get("acc", 0) * 100, base.get("acc", 0) * 100))
        src_all = open(os.path.join(ROOT, "agent", "intent.py"), encoding="utf-8").read()
        rec("D2", "易混淆反例" not in src_all and "NEGATIVE_EXAMPLES" not in src_all,
            "生产提示词未注入负例段（防有人偷偷打开净负优化）")
    else:
        rec("D1", False, "缺少负例 A/B 结果，无法判防倒退")

    print("=" * 80)
    if FAILED:
        print("FAIL %d 项：%s" % (len(FAILED), ", ".join(FAILED)))
        return 1
    print("ALL_OK=True  （WARN %d 项为运营改进项，非回归）" % len(WARNED))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
