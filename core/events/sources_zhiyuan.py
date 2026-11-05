# -*- coding: utf-8 -*-
"""真实事件源：智源平台模型变更（2026-11-01）—— 第三个真实源（MCP 入口）。

━━━ 为什么它走 MCP 而不是 HTTP ━━━
本平台**已经是 MCP server**（`routers/mcp_gateway.py`，协议 2025-06-18）。
外部 harness（智源 / DeepSeek Harness / Claude Code）消费本平台能力时走的是 MCP。
⇒ 事件的**入口也应该在 MCP 上**（`tools/call name=report_event`），
否则外部要同时维护两条通道（MCP 调工具 + HTTP 报事件），**很容易只做了前者**
—— 这正是"接上了但事件从没进来"的典型成因。

⚠️ 但本模块**不依赖 MCP**：`build_zhiyuan_source` 产出的 SourceSpec 同时被
`POST /api/events/zhiyuan`（HTTP 直调）与 MCP `report_event`（标准协议）复用
—— **同一份准入规则、同一份幂等口径**，不为两条通道各写一套。

━━━ 独有的一个概念：「明确请求」的判定 ━━━
智源是人建模的地方（`actor_type='human'`），所以它**必须**靠白名单 + 明确 @ 判定
（与文档入库 / 校验失败那种"系统既成事实"不同）。
⇒ 默认 `allowed` 为空 ⇒ 所有真人事件都 `filtered`。
  这是刻意的：**谁能在智源里一句话就触发本平台写操作，必须由人显式配置。**
  （对齐 dsh 的 admission table：名单外 + 未提及 ⇒ 忽略。）

━━━ Thread 口径 = 分支 ━━━
同一分支下的多次变更归为一条事件流 ⇒ 面板上能看"这个分支这周改了几次"。
"""

from __future__ import annotations

from core.events.model import Admission, OBSERVE_MODE, build_trigger
from core.events.sources import SourceSpec

__all__ = ["ZHIYUAN_SOURCE_KIND", "build_zhiyuan_source", "emit_zhiyuan_change"]

ZHIYUAN_SOURCE_KIND = "zhiyuan"


def build_zhiyuan_source(allowed_senders: frozenset = frozenset(),
                         scope_keys: frozenset = frozenset(),
                         mode: str = OBSERVE_MODE,
                         policy_version: str = "v1",
                         listen: str = "mentions") -> SourceSpec:
    """构造智源模型变更来源。

    :param allowed_senders: 白名单（智源里的用户名，小写比较）。**空 = 谁都不放行**
    :param scope_keys: 只接收这些分支（空 = 不接收任何分支）
    :param listen: "mentions"=只收明确 @（推荐）| "all"=收全部（噪音大，慎用）
    """
    adm = Admission(
        source=ZHIYUAN_SOURCE_KIND,
        allowed=allowed_senders,
        listen=listen,
        scope_keys=scope_keys,
        mode=mode,
        version=policy_version,
    )

    def _to_trigger(raw: dict):
        raw = raw or {}
        branch = str(raw.get("thread_scope") or raw.get("branch") or "").strip()
        ext = str(raw.get("external_id") or "").strip() or branch
        return build_trigger(
            source=ZHIYUAN_SOURCE_KIND,
            external_id=ext,
            thread_scope=branch or ext,
            actor=str(raw.get("actor") or "").strip(),
            actor_type=str(raw.get("actor_type") or "human"),
            addressed=(raw.get("addressed") if raw.get("addressed") in ("mention", "direct") else None),
            text=str(raw.get("text") or ""),
            payload=raw.get("payload") if isinstance(raw.get("payload"), dict) else {},
            version=str(raw.get("version") or ""),
            at=str(raw.get("at") or ""),
        )

    def _action_for(trigger):
        """建议动作：变更影响分析（**只声明不执行**）。"""
        branch = trigger.thread_key.split(":", 1)[-1] or trigger.external_id
        return ("impact_analysis", "智源模型变更待做影响分析：%s" % branch)

    return SourceSpec(
        kind=ZHIYUAN_SOURCE_KIND,
        title="智源模型变更",
        admission=adm,
        to_trigger=_to_trigger,
        action_for=_action_for,
        scope_of=lambda raw: str((raw or {}).get("thread_scope") or (raw or {}).get("branch") or ""),
    )


def emit_zhiyuan_change(conn, branch: str, actor: str, text: str = "",
                        addressed: str = "mention", payload: dict = None,
                        version: str = "", actor_type: str = "human",
                        registry=None) -> dict:
    """智源变更后发一条事件（**永不抛异常**，失败只留日志）。"""
    try:
        from core.events.sources import get_registry
        from repositories.event_repo import EventRepo
        from services.event_service import EventService

        reg = registry if registry is not None else get_registry()
        if reg.get(ZHIYUAN_SOURCE_KIND) is None:
            return {"error": "source_not_registered", "kind": ZHIYUAN_SOURCE_KIND}
        svc = EventService(EventRepo(conn), reg)
        return svc.receive(ZHIYUAN_SOURCE_KIND, {
            "external_id": str(branch),
            "thread_scope": str(branch),
            "actor": str(actor or "system"),
            "actor_type": str(actor_type),
            "addressed": addressed if addressed in ("mention", "direct") else None,
            "text": str(text or ""),
            "payload": payload or {},
            "version": str(version or ""),
        })
    except Exception as e:
        try:
            import logging
            logging.getLogger(__name__).warning(
                "智源变更事件未发出（不阻断主流程）: %s", str(e)[:160])
        except Exception:
            pass
        return {"error": str(e)[:200]}
