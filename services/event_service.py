# -*- coding: utf-8 -*-
"""事件驱动 service —— receive → 准入 → 工单（2026-11-01，Super-agent 落地 P0-2/P0-3）。

本模块是**编排层**：把「外部事件」变成「可追踪、可授权、可执行（或仅观察）的工作单元」。

━━━ 主流程（三步，每步都留痕）━━━
    receive(kind, raw)
      ① 落 Trigger（幂等：同 external_id 重复投递 ⇒ 同一条）
      ② Fast Path 准入判定（纯函数，**不读正文**）
      ③ 准入通过 ⇒ 建 WorkItem（带授权快照）；不通过 ⇒ 记 disposition + reason
    ⇒ 无论哪条分支，**事件都会留在 ev_triggers 里并带处置结论**。

⚠️ **为什么"被过滤"也必须落库（不可省）━━━
本工程最贵的一类 bug 是"静默丢弃不留痕 ⇒ 看起来像没发起"
（用户记忆里「上传附件静默丢弃」那次定位：后端 `continue` 掉一行，
前端两条入队路径形态不同 ⇒ 用户看到的就是"流程没发起"）。
⇒ 本模块**不允许**出现"事件被丢弃但库里查不到"的路径。
被过滤的事件也要有 disposition='filtered' + reason='...'。

━━━ 观察模式（P0-3，本落地的最小风险形态）━━━
`mode='observe'` 时：
  · 工单**照建**（记录"本应做什么"）
  · **不执行** action（不调任何写工具、不改图谱）
  · 人确认后才可切到 execute（`EventRepo.set_source_mode`）

为什么先观察：MBSE 建模的写副作用（建实体/建关系/合并分支）**回滚成本远高于读**，
而事件正文不可信 ⇒ 误触发的代价不对称。先可观测，后可执行。

⚠️ **observe 与 execute 的判定必须唯一真源**：
`mode` 只存在 `ev_sources.mode` 一处，工单上的 `mode` 是**落库时的快照**。
若两处都能改，就会出现"来源是 observe、工单却是 execute"⇒ 观察期写库判据失效。
⇒ 放行执行的唯一入口是 `set_source_mode`（它改来源，工单快照随之在新工单体现）。

━━━ 幂等与并发 ━━━
· 同 external_id 重复投递 ⇒ 同 Trigger ID ⇒ `INSERT OR IGNORE` 命中 ⇒ 不重复建工单
· 并发投递同事件 ⇒ 主键唯一约束 ⇒ 只有一个赢家
· 判定"已存在"用**主键唯一性**，不用"先查再插"（TOCTOU）
"""

from __future__ import annotations

from typing import Optional

from core.events.model import (
    Admission, AdmissionVerdict, Trigger, WorkItem,
    OBSERVE_MODE, EXECUTE_MODE,
    decide_workitem_transition, is_terminal,
    decide_handoff, build_handoff_context,
)
from core.events.sources import SourceRegistry, get_registry
from repositories.event_repo import EventRepo

__all__ = ["EventService", "receive_event", "register_source", "list_workitems"]


class EventService:
    """事件域编排。**依赖注入式**（repo + registry），便于单测与变异自证。"""

    def __init__(self, repo: EventRepo, registry: Optional[SourceRegistry] = None):
        self.repo = repo
        self.registry = registry or get_registry()

    # ── 来源注册 ──
    def register_source(self, spec) -> dict:
        """注册来源（写库 + 进程内注册表）。

        ⚠️ **顺序有意义**：先落库再进注册表。
        反过来的话，若落库失败，进程内已有一个"库里没有"的来源
        ⇒ 事件能被接收但查不到配置 ⇒ 排障时两边对不上。
        """
        self.repo.upsert_source({
            "kind": spec.kind,
            "title": spec.title,
            "listen": spec.admission.listen,
            "allowed": sorted(spec.admission.allowed),
            "scope_keys": sorted(spec.admission.scope_keys),
            "mode": spec.admission.mode,
            "policy_version": spec.admission.version,
            "enabled": True,
        })
        self.registry.register(spec)
        return spec.admission.to_row()

    # ── 主流程 ──
    def receive(self, kind: str, raw: dict) -> dict:
        """接收一个外部事件 ⇒ 返回处置结论。

        返回结构固定为：
            {disposition, reason, strength, trigger_id, workitem_id?, mode, accepted}
        ⚠️ **返回结构里没有"什么都没发生"这种形态** —— 调用方永远能知道结果。
        """
        spec = self.registry.get(kind)
        if not spec:
            # 来源未注册 ⇒ 不能静默丢。落一条"未注册来源"的记录留痕。
            return {
                "disposition": "rejected", "reason": "source_not_registered",
                "strength": "none", "trigger_id": "", "workitem_id": "",
                "mode": OBSERVE_MODE, "accepted": False,
            }

        trigger = spec.to_trigger(raw)
        adm = spec.admission

        # ① 落 Trigger（幂等）
        existed = self.repo.trigger_exists(trigger.id)
        row = trigger.to_row()
        row["disposition"] = "received"
        row["reason"] = ""
        inserted = self.repo.insert_trigger(row)

        if existed or not inserted:
            # 幂等命中：同一外部事件重复投递 ⇒ 不重复建工单
            #
            # ⚠️ **不覆盖首次的 disposition/reason**（这是本轮实测抓到的真实缺陷）：
            # 原实现在此处 `set_trigger_disposition(tid, "duplicate", ...)`，
            # 结果一个"当初被 admitted、后来被重投"的事件，台账里只剩 `duplicate`
            # ⇒ **事后查不到它当初的处置结论**，也无法区分
            #    "本来就该 duplicate（从未 admitted）" 与 "admitted 后被重投"。
            # ⇒ 幂等命中只改一个**独立字段**（hit_count），不动首次结论。
            #    首次结论是事实（不可变），重投次数是观测（可累加）。
            self.repo.bump_trigger_hit(trigger.id)
            return {
                "disposition": "duplicate", "reason": "already_received",
                "strength": "none", "trigger_id": trigger.id, "workitem_id": "",
                "mode": adm.mode, "accepted": False,
                "first_disposition": (self.repo.get_trigger(trigger.id) or {}).get("disposition", ""),
            }

        # ② Fast Path 准入（纯函数）
        verdict = self.registry.evaluate(kind, trigger, is_duplicate=False)

        # ②b 建单前置校验（数据完备性）—— 准入通过但数据不全时**不建工单**
        #     为什么要有这一层：准入判"该不该做"（身份/范围），
        #     但"该做"不等于"能做"（如 SysML 事件没带原始代码 ⇒ 无从修复）。
        #     若不拦，会建出一堆"必然失败"的工单，把面板噪音化。
        if verdict.accepted and spec.precheck:
            try:
                _ok, _why = spec.precheck(raw)
            except Exception as _e:
                _ok, _why = False, "precheck_error:%s" % str(_e)[:80]
            if not _ok:
                self.repo.set_trigger_disposition(trigger.id, "filtered", _why)
                return {
                    "disposition": "filtered", "reason": _why,
                    "strength": verdict.strength, "trigger_id": trigger.id,
                    "workitem_id": "", "mode": adm.mode, "accepted": False,
                    "note": "准入通过但数据不完备，未建工单",
                }

        if not verdict.accepted:
            self.repo.set_trigger_disposition(trigger.id, "filtered", verdict.reason)
            return {
                "disposition": "filtered", "reason": verdict.reason,
                "strength": verdict.strength, "trigger_id": trigger.id,
                "workitem_id": "", "mode": adm.mode, "accepted": False,
            }

        # ③ 建工单（带授权快照）
        wi = self.registry.to_workitem(kind, trigger, verdict)
        created = self.repo.insert_workitem(wi.to_row())
        self.repo.set_trigger_disposition(trigger.id, "admitted", verdict.reason)

        return {
            "disposition": "admitted", "reason": verdict.reason,
            "strength": verdict.strength, "trigger_id": trigger.id,
            "workitem_id": wi.id if created else "",
            "mode": wi.mode, "accepted": True,
            "observe_only": wi.mode == OBSERVE_MODE,
            "title": wi.title, "action": wi.action,
        }

    # ── 来源模式切换（放开自动执行的唯一入口）──
    def set_mode(self, kind: str, mode: str) -> dict:
        """切来源模式（observe ⇄ execute）：**DB 与进程内 registry 同时改**。

        ⚠️ **两处必须同时改**（2026-11-01 实测修掉的真实缺陷）：
        原先只改 DB（`repo.set_source_mode`），进程内 `spec.admission.mode` 没同步
        ⇒ 切到 execute 后**新事件建的工单仍拿 observe 快照**
        ⇒ **"放开自动执行"这个开关实际不生效**（症状：DB 说 execute，
        但新工单 mode=observe，执行被拒）。
        ⇒ DB 与进程内不一致 = 开关失灵 ⇒ 必须同一个入口一次改两处。

        ⚠️ **两个方向都要能切**（第二次实测修的）：
        · 来源在 DB 里（生产路径：`register_source` 写过）⇒ 改 DB + registry
        · 来源**只在 registry 里**（单测/临时注册，没写 DB）⇒ 改 registry，
          DB 侧跳过但**不算失败**
        ⇒ 原实现"DB 没有就直接 return source_not_found" ⇒ registry 也没改
          ⇒ 开关仍然失灵（症状同前，但这次是"来源未落库"这条路）。

        ⚠️ **已建工单的 mode 是快照，不随之改变**（刻意）：
        观察期建的工单即使后来切了 execute，也**仍不可执行**
        ⇒ 观察期不会被"事后放开"绕过（见 executor 的 mode 判据）。
        """
        if mode not in (OBSERVE_MODE, EXECUTE_MODE):
            return {"ok": False, "reason": "invalid_mode", "mode": mode}
        db_changed = self.repo.set_source_mode(kind, mode) > 0
        reg_changed = self.registry.set_mode(kind, mode)
        if not db_changed and not reg_changed:
            return {"ok": False, "reason": "source_not_found", "kind": kind}
        return {"ok": True, "kind": kind, "mode": mode,
                "db_changed": db_changed, "registry_synced": reg_changed,
                "note": "已建工单的模式是快照，不随本次切换改变；仅新工单用新模式"}

    # ── 工单状态流转 ──
    def advance(self, workitem_id: str, nxt_state: str, result: str = "",
                error: str = "") -> dict:
        """推进工单状态（状态机判定在此，不在 SQL 里）。

        ⚠️ **终态不可自动复活**：`failed → open` 合法，但只允许人工触发
        （自动重试是另一条路径，且要受 `agent/loop_guard.py` 的 token 预算约束）。
        """
        wi = self.repo.get_workitem(workitem_id)
        if not wi:
            return {"ok": False, "reason": "workitem_not_found"}
        ok, why = decide_workitem_transition(wi.get("state", "open"), nxt_state)
        if not ok:
            return {"ok": False, "reason": why, "state": wi.get("state")}
        self.repo.set_workitem_state(workitem_id, nxt_state, result, error)
        return {"ok": True, "reason": why, "state": nxt_state}

    def claim_for_execute(self, workitem_id: str) -> dict:
        """取一个 observe 工单准备执行 —— **本函数只改状态，不执行动作**。

        ⚠️ 这里刻意**不调用任何写工具**：执行动作由调用方（人确认后）
        显式发起。理由：观察期内的写库判据必须是"实际写库条数 = 0"，
        若 service 层自己就动手，这条判据永远无法成立。
        """
        wi = self.repo.get_workitem(workitem_id)
        if not wi:
            return {"ok": False, "reason": "workitem_not_found"}
        if wi.get("mode") != EXECUTE_MODE:
            return {"ok": False, "reason": "source_not_in_execute_mode",
                    "mode": wi.get("mode")}
        ok, why = decide_workitem_transition(wi.get("state", "open"), "running")
        if not ok:
            return {"ok": False, "reason": why, "state": wi.get("state")}
        self.repo.set_workitem_state(workitem_id, "running")
        return {"ok": True, "reason": why, "state": "running",
                "action": wi.get("action", "")}

    # ── 查询 ──
    def list_workitems(self, source: str = "", state: str = "",
                       mode: str = "", limit: int = 50) -> list:
        return self.repo.list_workitems(source=source, state=state, mode=mode, limit=limit)

    def list_triggers(self, source: str = "", thread_key: str = "",
                      limit: int = 50) -> list:
        return self.repo.list_triggers(source=source, thread_key=thread_key, limit=limit)

    def stats(self) -> dict:
        return self.repo.stats()

    # ── 跨会话接力（Super-agent 的"连续"特征）──
    def handoff(self, workitem_id: str, to_conversation_id: int) -> dict:
        """把工单挂到另一个会话，并返回接力上下文包。

        ⚠️ **本方法不做任何业务执行** —— 只做"挂载 + 给上下文"。
        理由与 `claim_for_execute` 同源：执行必须由人确认后显式发起，
        否则"观察期零写库"这条判据无法成立。

        ⚠️ **挂载是"并行认领"不是"转移"**：原会话仍可继续推进。
        两个会话同时跑同一工单 ⇒ 重复写图谱 ⇒ 副作用不可逆。
        ⇒ 真正的执行裁决权仍在状态机 + 人工确认。
        """
        wi = self.repo.get_workitem(workitem_id)
        if not wi:
            return {"ok": False, "reason": "workitem_not_found"}
        cur = int(wi.get("conversation_id") or 0)
        ok, why = decide_handoff(wi, to_conversation_id, cur)
        if not ok:
            return {"ok": False, "reason": why, "state": wi.get("state"),
                    "conversation_id": cur}
        trg = self.repo.get_trigger(wi.get("trigger_id") or "") or {}
        ctx = build_handoff_context(wi, trg,
                                     self.repo.list_workitems_by_thread(
                                         wi.get("thread_key") or "", 5))
        n = self.repo.bind_workitem_conversation(workitem_id, to_conversation_id)
        if not n:
            return {"ok": False, "reason": "bind_failed"}
        return {"ok": True, "reason": why, "workitem_id": workitem_id,
                "conversation_id": int(to_conversation_id),
                "previous_conversation_id": cur,
                "context": ctx}

    def list_for_conversation(self, conversation_id: int, limit: int = 50) -> list:
        """某会话关联的工单（会话页"待办/相关事件"面板用）。"""
        return self.repo.list_workitems_by_conversation(conversation_id, limit)


# ── 便捷函数（供 router 层用，避免每次都 new）──
def receive_event(repo: EventRepo, kind: str, raw: dict,
                  registry: Optional[SourceRegistry] = None) -> dict:
    return EventService(repo, registry).receive(kind, raw)


def register_source(repo: EventRepo, spec) -> dict:
    return EventService(repo).register_source(spec)


def list_workitems(repo: EventRepo, **kw) -> list:
    return EventService(repo).list_workitems(**kw)
