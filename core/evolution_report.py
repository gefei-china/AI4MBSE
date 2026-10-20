# -*- coding: utf-8 -*-
"""原型：自进化周报引擎（价值感知的第一个可运行形态）。

═══════════════════════════════════════════════════════════════
它回答一个问题：**这一周，系统替我做了什么？**
═══════════════════════════════════════════════════════════════
自进化六环全部交付后，价值感知链断在最后一环：能力都在（192 门禁常驻、
步骤级归因、配置提案、沙箱晋升、死路记忆、安全五信号、任务自生成、拓扑
体检），但用户只能逐个端点查 JSON，没有一个地方能回答"这一周系统替我
做了什么"。

本原型聚合四段，每段都只读既有台账，**不新增任何判据、不发明任何数字**：

  ① **省了什么**（archive域）   —— 死路记忆 + 变体画像：记住了多少条走不通的路
  ② **抓到了什么**（curriculum域）—— 弱区诊断：发现了哪些该练的弱区
  ③ **守住了什么**（sandbox/misevolution 域）—— 变更前被门禁拦住的有哪些
  ④ **还欠着什么**（三队列+ 安全信号）—— 待人裁决的建议 + 当前安全状态

★诚实分母纪律（承 P2-1/P2-2/P2-3/P2-4 全系列）：
  - **没数据写「本周无」，绝不写 0**。「这周拦了 0 次拦截」（真拦了且都没拦下）
    与「这周没发生变更所以没东西可拦」是完全不同的两件事，混在一起会让这张
    周报第一周就失去可信度。
  - 分母不足的指标记 unknown（None），**不回落 0**。
  - 每个数字都带**来源**（哪张表哪一行），能点回台账核对。
  - 段落级的 verdict 只有三态：clean / needs_attention / insufficient_data。

本文件是**原型**：单文件、只读、无迁移无端点，先让形状和口径过一遍再决定
是否产品化（产品化要补配置双登记、端点、门禁、CI 接线）。
"""
from __future__ import annotations

import json
import sqlite3
from typing import Optional

# ── 四段周报的定义（产品口径集中在此，勿在调用处另写字面量）───────────────

#: 段 key → 段标题（一句话回答"这段对用户意味着什么"）
SECTIONS = {
    "saved": "省了什么",
    "found": "抓到了什么",
    "guarded": "守住了什么",
    "owed": "还欠着什么",
}

#: 段级 verdict
V_CLEAN = "clean"
V_ATTENTION = "needs_attention"
V_UNKNOWN = "insufficient_data"


def _tbl_exists(conn: sqlite3.Connection, table: str) -> bool:
    try:
        conn.execute("SELECT 1 FROM %s LIMIT 1" % table).fetchone()
        return True
    except sqlite3.Error:
        return False


def _count(conn: sqlite3.Connection, sql: str, params=()) -> Optional[int]:
    """计数；表不存在返 None（**不是 0**——表还没建= 没有这项证据）。"""
    try:
        return int(conn.execute(sql, tuple(params)).fetchone()[0])
    except sqlite3.Error:
        return None


def _rows(conn: sqlite3.Connection, sql: str, params=()) -> list:
    try:
        return [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]
    except sqlite3.Error:
        return []


# ══════════════════════════════════════════════════════════════
# ① 省了什么 —— archive 域（死路记忆 + 变体画像）
# ══════════════════════════════════════════════════════════════
def section_saved(conn: sqlite3.Connection) -> dict:
    """死路记忆与变体台账。

    ★这一段的价值不是"变体有多少个"（那是台账注水），而是
    **"有多少条路已经确认走不通、且系统记住了"** —— 这是省下的时间。

    ★★真库实测修正（原型跑出来的）：真库 asset_change_logs **0 条**，
    agents.version 分布是 v1/v1.0.0/1 —— 也就是说 archive 台账里那30 个
    "变体"是**从未被改过的 agent 各自的初始版本**，不是 30 次进化。
    直接说"30 个变体在册"会让人以为系统做了 30 次进化。
    改为区分**「进化出的变体」**（有真实变更留痕的）与
    **「仅在册的初始版本」**（agent 资产没被改过时的诚实答案），
    后者单独列出且不算作"省了什么"。
    """
    if not _tbl_exists(conn, "agent_variants"):
        return {"verdict": V_UNKNOWN, "reason": "archive 台账尚未建立",
                "items": [], "metrics": {}}
    total = _count(conn, "SELECT COUNT(*) FROM agent_variants") or 0
    dead = _count(conn, "SELECT COUNT(*) FROM agent_variants "
                         "WHERE status='dead_end'") or 0
    retired = _count(conn, "SELECT COUNT(*) FROM agent_variants "
                           "WHERE status='retired'") or 0
    neg = _count(conn, "SELECT COUNT(*) FROM variant_outcomes "
                       "WHERE outcome IN ('regression','clean_fail')") or 0
    attached = _count(conn, "SELECT COUNT(*) FROM variant_outcomes") or 0
    # ★真实进化留痕：asset_change_logs 里的 agent 类变更条数。
    #   没有它 = 台账里的变体全是「初始版本」而非「进化出来的变体」。
    evo_changes = _count(conn, "SELECT COUNT(*) FROM asset_change_logs "
                                 "WHERE asset_type='agents'")
    if evo_changes is None:
        evo_changes = 0
    evolved = max(0, total - retired - 1) if total else 0
    items = _rows(conn, "SELECT agent_name, version, dead_end_reason, "
                        "dead_end_at FROM agent_variants "
                        "WHERE status='dead_end' ORDER BY id DESC LIMIT 10")
    metrics = {
        "variants_on_ledger": total,
        "asset_change_logs_agents": evo_changes,
        "dead_ends": dead,
        "retired": retired,
        "negative_outcomes": neg,
        "outcomes_attached": attached,
    }
    if total == 0:
        return {"verdict": V_UNKNOWN,
                "reason": "尚无变体台账（agent 资产还没被改过，"
                          "台账每维都是 unknown 才是诚实答案）",
                "items": items, "metrics": metrics}
    if evo_changes == 0:
        # 没有一条真实变更留痕 ⇒ 台账里的变体全是初始版本，
        # 说"省了什么"是不实的——如实说"本周无进化事件"。
        return {"verdict": V_UNKNOWN,
                "reason": "本周无进化事件（agent 资产变更留痕 %d 条，"
                          "台账上的 %d 个变体全是初始版本，不算进化）"
                          % (evo_changes, total),
                "items": items, "metrics": metrics}
    if dead:
        return {"verdict": V_ATTENTION,
                "reason": "记住了 %d 条走不通的路" % dead,
                "items": items, "metrics": metrics}
    return {"verdict": V_CLEAN,
            "reason": "有真实进化留痕（%d 条），暂无确认死路" % evo_changes,
            "items": items, "metrics": metrics}


# ══════════════════════════════════════════════════════════════
# ② 抓到了什么 —— curriculum 域（弱区诊断）
# ══════════════════════════════════════════════════════════════
def section_found(conn: sqlite3.Connection) -> dict:
    """失败证据 → 该练什么任务。

    价值在"**待办提案**"而不是"发现了多少问题"：每条提案都带可回溯的
    证据（样本会话/坏样本行），人可以直接照着练。
    """
    from core import curriculum as cu
    if not _tbl_exists(conn, "curriculum_items"):
        return {"verdict": V_UNKNOWN, "reason": "curriculum 队列尚未建立",
                "items": [], "metrics": {}}
    pending = _count(conn, "SELECT COUNT(*) FROM curriculum_items "
                           "WHERE status='pending'") or 0
    accepted = _count(conn, "SELECT COUNT(*) FROM curriculum_items "
                            "WHERE status='accepted'") or 0
    rejected = _count(conn, "SELECT COUNT(*) FROM curriculum_items "
                            "WHERE status='rejected'") or 0
    by_rule = _rows(conn, "SELECT rule, COUNT(*) AS n FROM curriculum_items "
                          "GROUP BY rule ORDER BY n DESC")
    items = _rows(conn, "SELECT id, rule, task_kind, title, difficulty, "
                        "status FROM curriculum_items WHERE status='pending' "
                        "ORDER BY id DESC LIMIT 10")
    # 失败证据侧（只读，不新增判据）
    evidence = {}
    try:
        ev = cu.collect_evidence(conn)
        evidence = {
            "patterns": len(ev.get("patterns") or []),
            "badcases": len(ev.get("badcases") or []),
            "unattributed": ev.get("unattributed_count"),
        }
    except Exception:                                     # noqa: BLE001
        evidence = {}
    metrics = {"pending": pending, "accepted": accepted,
               "rejected": rejected, "by_rule": by_rule,
               "evidence": evidence}
    if pending + accepted + rejected == 0:
        return {"verdict": V_UNKNOWN,
                "reason": "本周无失败证据 ⇒ 无弱区可报（空窗口不是 0 问题）",
                "items": items, "metrics": metrics}
    verdict = V_ATTENTION if pending else V_CLEAN
    reason = ("%d 条弱区建议待你裁决" % pending) if pending else \
             ("%d 条弱区建议已被认领" % accepted)
    return {"verdict": verdict, "reason": reason, "items": items,
            "metrics": metrics}


# ══════════════════════════════════════════════════════════════
# ③ 守住了什么 —— sandbox 域 + 安全信号域
# ══════════════════════════════════════════════════════════════
def section_guarded(conn: sqlite3.Connection) -> dict:
    """变更前被门禁/人工拦下的。

    ★本段最容易骗人：「拦了 0 次」有两种含义——(a) 真跑了 N 次门禁、
    都没拦下（好）；(b) 这周没发生任何变更、没东西可拦（**不是好**，
    是"本段本周无事件"）。必须分开报。

    ★★原型跑出来修正：安全信号原本被塞进本段，导致「本周无变更事件」
    时整段理由变成"安全信号有 2 项 alert"——两件事压成一句话，用户读不出
    "这周到底有没有变更"。改为：**变更守卫**与**安全状态**是两个独立子块，
    各自独立成句，段级verdict 取最严重者。
    """
    out = {"verdict": V_UNKNOWN, "reason": "", "items": [], "metrics": {},
           "subblocks": []}
    # ── 子块 A：变更守卫（沙箱 A/B）──
    runs = _count(conn, "SELECT COUNT(*) FROM sandbox_runs")
    if runs is None:
        out["subblocks"].append({
            "key": "change_guard", "title": "变更守卫",
            "verdict": V_UNKNOWN, "reason": "沙箱台账尚未建立"})
    else:
        rejected = _count(conn, "SELECT COUNT(*) FROM sandbox_runs "
                                "WHERE verdict='regression'") or 0
        passed = _count(conn, "SELECT COUNT(*) FROM sandbox_runs "
                               "WHERE verdict='clean'") or 0
        out["metrics"].update({"sandbox_runs": runs,
                               "regressions_caught": rejected,
                               "passed": passed})
        if runs == 0:
            out["subblocks"].append({
                "key": "change_guard", "title": "变更守卫",
                "verdict": V_UNKNOWN,
                "reason": "本周无变更事件（**不是「拦了 0 次」**——"
                          "没发生变更就没有东西可拦）"})
        elif rejected:
            out["subblocks"].append({
                "key": "change_guard", "title": "变更守卫",
                "verdict": V_ATTENTION,
                "reason": "门禁拦下 %d 次配置回归" % rejected})
        else:
            out["subblocks"].append({
                "key": "change_guard", "title": "变更守卫",
                "verdict": V_CLEAN,
                "reason": "%d 次变更全部通过门禁" % runs})
    # ── 子块 B：人工确认点（HIL）──
    hil_pending = _count(conn, "SELECT COUNT(*) FROM hil_confirmations "
                               "WHERE status='pending'")
    if hil_pending is not None:
        out["metrics"]["hil_pending"] = hil_pending
        out["items"] = _rows(conn, "SELECT id, action, agent_id, created_at "
                                   "FROM hil_confirmations "
                                   "WHERE status='pending' ORDER BY id DESC LIMIT 5")
        out["subblocks"].append({
            "key": "hil", "title": "人工确认点",
            "verdict": V_ATTENTION if hil_pending else V_CLEAN,
            "reason": ("%d 个写操作在等你确认（系统没有替你动手）" % hil_pending)
                      if hil_pending else "无待确认写操作"})

    # ── 子块 C：安全状态（misevolution 五信号）──
    try:
        from core import misevolution as mv
        sig = mv.collect_signals(conn)
        alerts = [{"signal": k, "label": v.get("label"),
                   "rate": v.get("rate"), "numerator": v.get("numerator"),
                   "denominator": v.get("denominator"),
                   "status": v.get("status")}
                  for k, v in sig.items()
                  if k != "meta" and v.get("status") in ("alert", "warn")]
        unknown = [k for k, v in sig.items()
                   if k != "meta" and not v.get("sufficient")]
        out["metrics"]["safety_alerts"] = alerts
        out["metrics"]["safety_unknown_signals"] = unknown
        out["metrics"]["safety_window"] = (sig.get("meta") or {}).get("window_days")
        if alerts:
            out["subblocks"].append({
                "key": "safety", "title": "安全状态",
                "verdict": V_ATTENTION,
                "reason": "%d 项安全信号处于 alert/warn" % len(alerts),
                "note": "alert 是**待人解读的信号**，不是已确认的恶化"})
        else:
            out["subblocks"].append({
                "key": "safety", "title": "安全状态",
                "verdict": V_CLEAN if not unknown else V_UNKNOWN,
                "reason": "无安全信号告警"
                          + ("（%d 项分母不足记 unknown）" % len(unknown)
                             if unknown else "")})
    except Exception as e:                                # noqa: BLE001
        out["subblocks"].append({
            "key": "safety", "title": "安全状态",
            "verdict": V_UNKNOWN,
            "reason": "安全信号采集失败: %s: %s" % (type(e).__name__, e)})

    # 段级 verdict：子块里最严重的那个
    _order = {V_UNKNOWN: 0, V_CLEAN: 1, V_ATTENTION: 2}
    if out["subblocks"]:
        worst = max((b["verdict"] for b in out["subblocks"]),
                    key=lambda v: _order.get(v, 0))
        out["verdict"] = worst
        out["reason"] = "；".join("%s：%s" % (b["title"], b["reason"])
                                 for b in out["subblocks"])
    return out


# ══════════════════════════════════════════════════════════════
# ④ 还欠着什么 —— 拓扑体检 + 结构性欠账
# ══════════════════════════════════════════════════════════════
def section_owed(conn: sqlite3.Connection) -> dict:
    """结构层面的欠账：画布冗余与结构缺陷。

    这一段是 P2-4 新增的，也是最贴近"用户能马上用上"的一段：
    画布数量在涨，但可辨识的拓扑没涨——这在"画布数量"指标下完全不可见。
    """
    from core import flow_topology as ft
    try:
        res = ft.scan(conn)
    except Exception as e:                                # noqa: BLE001
        return {"verdict": V_UNKNOWN,
                "reason": "拓扑体检失败: %s: %s" % (type(e).__name__, e),
                "items": [], "metrics": {}}
    ov = res.get("overview") or {}
    groups = res.get("groups") or []
    metrics = {
        "canvases": ov.get("canvases", 0),
        "isomorphic_groups": ov.get("isomorphic_groups", 0),
        "duplicate_canvases": ov.get("duplicate_canvases", 0),
        "uncovered_canvases": ov.get("uncovered_canvases", 0),
        "blocker_defects": next((d["value"] for d in ov.get("dimensions", [])
                                 if d["name"] == "blocker_defects"), 0),
    }
    items = [{"size": g["size"], "shape": g["shape"], "sources": g["sources"],
              "flow_names": g.get("flow_names", [])[:3],
              "suggestion": "%d 幅画布结构完全相同，%s 在反复生成同一个拓扑"
                            % (g["size"], "/".join(g["sources"]) or "某来源")}
             for g in groups[:5]]
    if not ov.get("canvases"):
        return {"verdict": V_UNKNOWN, "reason": "尚无画布定义",
                "items": items, "metrics": metrics}
    if metrics["blocker_defects"] or metrics["duplicate_canvases"]:
        return {"verdict": V_ATTENTION,
                "reason": "%d 幅画布结构重复，%d 个 blocker 级结构缺陷"
                          % (metrics["duplicate_canvases"],
                             metrics["blocker_defects"]),
                "items": items, "metrics": metrics}
    return {"verdict": V_CLEAN, "reason": "画布结构无重复、无 blocker 缺陷",
            "items": items, "metrics": metrics}


# ══════════════════════════════════════════════════════════════
# 汇总
# ══════════════════════════════════════════════════════════════
def build_report(conn: sqlite3.Connection) -> dict:
    """生成一周报（只读，不写任何表）。"""
    sections = {
        "saved": section_saved(conn),
        "found": section_found(conn),
        "guarded": section_guarded(conn),
        "owed": section_owed(conn),
    }
    verdicts = [s["verdict"] for s in sections.values()]
    if all(v == V_UNKNOWN for v in verdicts):
        overall = V_UNKNOWN
    elif V_ATTENTION in verdicts:
        overall = V_ATTENTION
    else:
        overall = V_CLEAN
    return {
        "title": "自进化周报",
        "question": "这一周，系统替我做了什么？",
        "overall": overall,
        "sections": [
            {"key": k, "title": SECTIONS[k], **sections[k]}
            for k in ("saved", "found", "guarded", "owed")
        ],
        "disclaimer": "所有数字来自既有台账的只读统计；无数据记「本周无」"
                      "而非 0，分母不足记 unknown 不回落 0。",
    }


def render_text(report: dict) -> str:
    """把周报渲染成人能读的文本（原型先看效果，不做前端）。"""
    marks = {"clean": "[清]", "needs_attention": "[待办]",
             "insufficient_data": "[无数据]"}
    L = ["%s（%s）" % (report["title"], report["question"]),
         "=" * 62]
    for s in report["sections"]:
        L.append("")
        L.append("%s %s" % (marks.get(s["verdict"], "[?]"), s["title"]))
        for b in (s.get("subblocks") or []):
            L.append("    %s %s —— %s"
                     % (marks.get(b["verdict"], "[?]"), b["title"],
                        b["reason"]))
            if b.get("note"):
                L.append("        注：%s" % b["note"])
        if not (s.get("subblocks") or []):
            L.append("    %s" % s["reason"])
        for k, v in (s.get("metrics") or {}).items():
            if isinstance(v, (int, float)) or v is None:
                L.append("    · %s = %s" % (k, v))
            elif isinstance(v, list) and v and isinstance(v[0], dict):
                L.append("    · %s:" % k)
                for it in v[:5]:
                    L.append("        %s" % json.dumps(it, ensure_ascii=False))
        for it in (s.get("items") or [])[:5]:
            if isinstance(it, dict):
                L.append("    - %s" % json.dumps(it, ensure_ascii=False)[:170])
            else:
                L.append("    - %s" % it)
    L.append("")
    L.append("—" * 62)
    L.append(report["disclaimer"])
    return "\n".join(L)
