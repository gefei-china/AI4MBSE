# -*- coding: utf-8 -*-
"""P1-4 配置晋升执行器：沙箱通过 + HIL 批准 ⇒ 配置才真的改（唯一写入口）。

═══════════════════════════════════════════════════════════════
动机（2026-10-19《自进化 AI 平台调研评估》路线图 P1-4）
═══════════════════════════════════════════════════════════════
P1-3 提案器的铁律是「只提议不自动应用」——应用由人在设置页改配置。那条路
有两个缺口：① 人改之前没有任何证据表明这个配置变更**不会改坏系统**；
② 改完没有任何台账，错了也回不去。

P1-4 补上这条安全通道，分两半：
  · 沙箱 A/B（core/sandbox.py）：影子配置跑门禁套件，对照组 vs 实验组
    差分判回归——**评估期间不碰任何生产状态**（门禁子进程拿临时库 +
    临时配置副本，影子层 MBSE_SHADOW_CONFIG 只活在被评估的子进程里）；
  · 本模块：晋升。沙箱 verdict=pass ⇒ 人发起晋升 ⇒ 进 hil_confirmations
    队列 ⇒ **人批准后本模块才执行配置变更**（经 core.config.save_override
    这条既有唯一写路径）并落 config_history 台账；回滚同样过人。

═══════════════════════════════════════════════════════════════
设计纪律
═══════════════════════════════════════════════════════════════
1. **本模块是全仓唯一「经人批准后写配置」的执行点**，且写只经
   `core.config.save_override`（既有路径：校验 → 落盘 → reload）。
   没有任何自动/定时/回调路径能调到 apply_config_change——调用方只有
   HIL decide 端点（approve 分支）。门禁静态断言 + 行为级证明双重守护。
2. **fail-closed**：目标不是 CONFIG_SCHEMA 已登记的 section.key ⇒ 拒绝
   （提案规则可能提议 instrumentation/skill 这类非配置目标，那些本就
   不该走配置晋升，人工修订后 decide=applied 留痕即可）。
3. **留痕**：每次应用（晋升/回滚）写 config_history（旧值→新值、来源
   提案/历史行、谁、何时、备注）⇒ 任何变更可回滚，回滚本身也是一次
   需人批准的应用（闭环，不留自动后门）。
4. **幂等**：新值 == 当前生效值 ⇒ 不重复写、不重复记历史（返
   applied=False + reason），但 HIL 确认单仍正常终态化（人已批准）。
5. **晋升前置条件**（全部满足才入 HIL 队列）：提案是配置键目标、提案
   pending（已决不重开）、存在 verdict=pass 的沙箱结论且**与提案当前
   目标一致**（配置现场变过 ⇒ 结论过期 ⇒ 必须重跑，防拿旧结论晋升新值）。
"""
from __future__ import annotations

import json
import time
from typing import Optional

# ── 产品契约常量（API/门禁/前端三方共用，勿在调用处另写字面量）─────────────
#: HIL 确认单 action（hil_confirmations.action 列；与既有工具类 action 共存）
HIL_ACTION_APPLY = "config_apply"
HIL_ACTION_ROLLBACK = "config_rollback"
HIL_ACTIONS = (HIL_ACTION_APPLY, HIL_ACTION_ROLLBACK)

#: config_history.source 取值（台账里区分「这次变更是谁促成的」）
SRC_PROPOSAL = "proposal"      # 沙箱通过 + HIL 批准的提案晋升
SRC_ROLLBACK = "rollback"      # 回滚某条历史（source_id = 被回滚的 history id）
SRC_MANUAL = "manual"          # 预留：设置页直改若将来接入台账（当前未接）


def is_config_target(section: str, key: str) -> bool:
    """提案目标是否是 CONFIG_SCHEMA 已登记的配置键（fail-closed 判据）。

    instrumentation.* / skill.* 这类「行动建议」目标返回 False——它们不是
    配置键，没有「改配置」这个动作可执行（人工修订后 decide=applied 留痕）。
    """
    from core import config as _config
    sec = _config.CONFIG_SCHEMA.get(str(section or ""))
    return bool(sec) and str(key or "") in sec


def _dotted(section: str, key: str) -> str:
    return "%s.%s" % (section, key)


def current_value(section: str, key: str):
    """当前生效值（读，不写）。"""
    from core import config as _config
    return _config.get(section, key, "")


def apply_config_change(conn, *, section: str, key: str, value,
                        source: str, source_id: int = 0,
                        applied_by: str = "", note: str = "") -> dict:
    """执行一次配置变更（**只由 HIL approve 路径调用**）。

    返回 {applied: bool, old, new, history_id, reason?}：
      · applied=False + reason="unchanged" —— 新值即当前值（幂等，不写不记）
      · applied=False + reason="invalid_target" —— 目标不是已登记配置键
    任何异常都向上抛（调用方 = HIL decide 端点，会把执行失败写进确认单
    回执与审计，不静默）。
    """
    if not is_config_target(section, key):
        return {"applied": False, "reason": "invalid_target",
                "old": "", "new": str(value)}
    from core import config as _config
    from repositories.proposal_repo import ProposalRepo
    old = _config.get(section, key, "")
    new = value
    if str(old) == str(new):
        # 幂等：不重复写盘、不重复记历史；但提案仍可被标记（见下）
        hist_id = _insert_history(conn, section, key, old, new, source,
                                  source_id, applied_by, note or "无变化（幂等）",
                                  applied=False)
        _maybe_mark_proposal(conn, source, source_id, applied_by,
                             note or "晋升时配置已是目标值（幂等）")
        return {"applied": False, "reason": "unchanged", "old": old, "new": new,
                "history_id": hist_id}
    # ── 唯一的配置写路径：save_override（校验 → 落盘 → reload）──
    _config.save_override({_dotted(section, key): new})
    hist_id = _insert_history(conn, section, key, old, new, source,
                              source_id, applied_by, note, applied=True)
    _maybe_mark_proposal(conn, source, source_id, applied_by,
                         note or "经沙箱 A/B + HIL 晋升应用")
    return {"applied": True, "old": old, "new": _config.get(section, key),
            "history_id": hist_id}


def _insert_history(conn, section: str, key: str, old, new, source: str,
                    source_id: int, applied_by: str, note: str,
                    applied: bool = True) -> int:
    """config_history 落一行（含未应用的幂等行——reason 区分，台账不撒谎）。"""
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    cur = conn.execute(
        "INSERT INTO config_history (section, key, old_value, new_value, source, "
        "source_id, applied_by, applied_at, note, applied) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (str(section), str(key), str(old), str(new), str(source),
         int(source_id or 0), str(applied_by or ""), now,
         (str(note or "") + ("" if applied else " [未应用:unchanged]")),
         1 if applied else 0))
    conn.commit()
    return int(cur.lastrowid)


def _maybe_mark_proposal(conn, source: str, source_id: int,
                         applied_by: str, note: str) -> None:
    """晋升成功后把对应提案标记 applied（队列反映事实；已决则跳过）。

    只处理 source=proposal；rollback 不回改提案状态（回滚的是配置，
    不是提案裁决——提案当年确实被人批准过，那是历史事实）。
    """
    if source != SRC_PROPOSAL or not source_id:
        return
    try:
        from core import config_proposal as _cp
        from repositories.proposal_repo import ProposalRepo
        row = ProposalRepo(conn).get_proposal(int(source_id))
        if row and _cp.can_decide(row.get("status")):
            ProposalRepo(conn).decide_proposal(
                int(source_id), _cp.PROPOSAL_APPLIED, note=note,
                decided_by=applied_by or "hil_promotion")
            conn.commit()      # ProposalRepo 不自动提交（路由靠 db_session 兜底）；
            # 本函数可能跑在作业/脚本等无 db_session 的路径上 ⇒ 自己提交，
            # 否则配置已改、台账已落，提案标记却丢了（状态不一致）。
    except Exception:
        pass        # 标记失败不影响配置已应用的事实（台账与审计已在）


# ── 晋升请求（入 HIL 队列）───────────────────────────────────────────────
def request_promotion(conn, proposal_id: int, *, user: str = "") -> dict:
    """为提案发起晋升：校验前置条件 → 入 hil_confirmations 待批准。

    前置（缺一不可，全部 fail-closed）：
      ① 提案存在且 pending（已决不重开）；
      ② 目标是已登记配置键（非配置目标走人工修订 + decide 留痕）；
      ③ 存在 verdict=pass 的沙箱结论，且其影子配置与提案**当前**
         target/proposed 一致（现场配置变过 ⇒ 旧结论过期 ⇒ 重跑）。
    返回 {ok, confirmation_id, proposal, sandbox_run} 或 {ok: False, error}。
    **本函数不写任何配置**——只写 HIL 队列（既有确认单机制）。
    """
    from core import config_proposal as _cp
    from core import sandbox as _sandbox
    from hil_service import HILService
    from repositories.proposal_repo import ProposalRepo
    row = ProposalRepo(conn).get_proposal(int(proposal_id))
    if not row:
        return {"ok": False, "error": "提案不存在"}
    if not _cp.can_decide(row.get("status")):
        return {"ok": False, "error": "提案已决（%s），终态不重开" % row.get("status")}
    section, key = row.get("target_section", ""), row.get("target_key", "")
    if not is_config_target(section, key):
        return {"ok": False, "error":
                "目标 %s.%s 不是已登记配置键（非配置目标无需晋升：人工修订后 "
                "decide=applied 留痕即可）" % (section, key)}
    run = _sandbox.latest_passing_run(conn, int(proposal_id))
    if not run:
        return {"ok": False, "error":
                "没有 verdict=pass 的沙箱结论——请先跑沙箱 A/B 评估"}
    if not _sandbox.run_matches_proposal(run, row):
        return {"ok": False, "error":
                "沙箱结论与提案当前目标不一致（现场配置已变或结论过期）——请重跑沙箱评估"}
    payload = {"section": section, "key": key,
               "value": row.get("proposed_value", ""),
               "proposal_id": int(proposal_id),
               "sandbox_run_id": int(run.get("id") or 0)}
    preview = ("配置晋升：%s.%s  %s → %s\n提案 #%s（规则 %s）\n沙箱结论 #%s：%s"
               % (section, key, row.get("current_value", ""),
                  row.get("proposed_value", ""), proposal_id,
                  row.get("rule", ""), run.get("id"), run.get("verdict", "")))
    conf = HILService.queue_confirmation(
        conn, agent_id="config_proposal", action=HIL_ACTION_APPLY,
        payload=payload, preview=preview, conversation_id=0, run_id=0,
        task_key="proposal:%d" % int(proposal_id))
    if not conf:
        return {"ok": False, "error": "入 HIL 队列失败"}
    return {"ok": True, "confirmation_id": int(conf.get("id") or 0),
            "proposal": row, "sandbox_run": run}


def request_rollback(conn, history_id: int, *, user: str = "", note: str = "") -> dict:
    """回滚某条 config_history：把该行的 new_value 改回 old_value。

    同样入 HIL 队列（回滚也是配置变更，不开自动后门）。
    被回滚行 applied=False（幂等未应用行）也允许——旧值仍是要回到的状态。
    """
    from hil_service import HILService
    row = None
    try:
        row = conn.execute("SELECT * FROM config_history WHERE id=?",
                           (int(history_id),)).fetchone()
    except Exception:
        row = None
    if not row:
        return {"ok": False, "error": "历史行不存在"}
    d = dict(row)
    section, key = d.get("section", ""), d.get("key", "")
    if not is_config_target(section, key):
        return {"ok": False, "error": "历史行目标 %s.%s 不是已登记配置键" % (section, key)}
    payload = {"history_id": int(history_id), "section": section, "key": key,
               "value": d.get("old_value", ""),
               "current_new": d.get("new_value", "")}
    preview = ("配置回滚：%s.%s  %s → %s（回滚历史 #%s，原变更人 %s @ %s）"
               % (section, key, d.get("new_value", ""), d.get("old_value", ""),
                  history_id, d.get("applied_by", ""), d.get("applied_at", "")))
    conf = HILService.queue_confirmation(
        conn, agent_id="config_proposal", action=HIL_ACTION_ROLLBACK,
        payload=payload, preview=preview, conversation_id=0, run_id=0,
        task_key="history:%d" % int(history_id))
    if not conf:
        return {"ok": False, "error": "入 HIL 队列失败"}
    return {"ok": True, "confirmation_id": int(conf.get("id") or 0),
            "history": d}


def execute_confirmation(conn, conf: dict) -> dict:
    """HIL 批准后执行确认单（**本模块对外的唯一执行入口**）。

    只认 config_apply / config_rollback 两种 action；其余 action 返回
    {executed: False, reason}（由既有端点分支处理，不越界）。
    payload 解析失败/目标非法 ⇒ 抛异常（端点记失败回执，不静默放行）。
    """
    action = str(conf.get("action") or "")
    if action not in HIL_ACTIONS:
        return {"executed": False, "reason": "非配置类确认单（%s）" % action}
    # payload 可能是 dict（HILService.get 已解析）或 JSON 字符串（裸行）——都认
    raw_payload = conf.get("payload")
    if isinstance(raw_payload, dict):
        payload = raw_payload
    else:
        try:
            payload = json.loads(raw_payload or "{}")
        except Exception:
            payload = {}
    if not isinstance(payload, dict):
        payload = {}
    decided_by = str(conf.get("decided_by") or "hil")
    if action == HIL_ACTION_APPLY:
        section = str(payload.get("section") or "")
        key = str(payload.get("key") or "")
        if not is_config_target(section, key):
            raise ValueError("确认单目标不是已登记配置键: %s.%s" % (section, key))
        out = apply_config_change(
            conn, section=section, key=key, value=payload.get("value", ""),
            source=SRC_PROPOSAL, source_id=int(payload.get("proposal_id") or 0),
            applied_by=decided_by,
            note="HIL 晋升批准（确认单 #%s）" % (conf.get("id") or 0))
        return {"executed": True, "kind": HIL_ACTION_APPLY, **out}
    # HIL_ACTION_ROLLBACK
    section = str(payload.get("section") or "")
    key = str(payload.get("key") or "")
    if not is_config_target(section, key):
        raise ValueError("确认单目标不是已登记配置键: %s.%s" % (section, key))
    out = apply_config_change(
        conn, section=section, key=key, value=payload.get("value", ""),
        source=SRC_ROLLBACK, source_id=int(payload.get("history_id") or 0),
        applied_by=decided_by,
        note="HIL 回滚批准（确认单 #%s，回滚历史 #%s）"
             % (conf.get("id") or 0, payload.get("history_id") or 0))
    return {"executed": True, "kind": HIL_ACTION_ROLLBACK, **out}


# ── 台账读取（回滚面板的数据源）────────────────────────────────────────
def list_history(conn, limit: int = 100, section: str = "") -> list:
    """config_history 台账（按时间倒序；section 可过滤）。"""
    lim = max(1, min(int(limit or 100), 500))
    if section:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM config_history WHERE section=? ORDER BY id DESC LIMIT ?",
            (str(section), lim)).fetchall()]
    return [dict(r) for r in conn.execute(
        "SELECT * FROM config_history ORDER BY id DESC LIMIT ?", (lim,)).fetchall()]
