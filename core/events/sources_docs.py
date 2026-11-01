# -*- coding: utf-8 -*-
"""真实事件源：文档入库（2026-11-01）—— 第一个接上真实链路的 Source。

━━━ 为什么第一个接它（选型理由）━━━
候选有四个（智源分支变更 / 文档入库 / SysML 校验失败 / 需求变更），选**文档入库**：
  ① **零外部凭据** —— 智源要 base_url+token（当前为空），文档入库不需要任何配置；
  ② **链路完整现成** —— `knowledge_pipeline.ingest_document` 已有完整的
     解析→分块→embedding→落库→commit 全流程；
  ③ **触发点语义准确** —— `conn.commit()` 之后才是"内容已进图库"，
     在此之前 chunks 还没落库（同一事务未提交）；
  ④ **量级真实** —— 一次入库 = 一个文档 = 一批 chunk + 实体，
     正是"人该被解放"的典型场景（人工要记得去触发抽取/追溯）。

━━━ 触发时机：为什么落在 commit 之后 ━━━
`ingest_document` 的第 6 步把 `lifecycle_status` 置为 `stored`，
第 7 步生成 LLM 真摘要。**事件落在 commit 之后**：
  · 此时 chunks 已落库 ⇒ "入库完成"为真；
  · 此时已放锁 ⇒ 事件处理不与入库抢写锁
    （与该函数"先 commit 再调 LLM"同源纪律：避免把长事务拖长）。
⚠️ **绝不在 commit 之前触发**：那时回滚会让"事件已发生但内容没进库"。

━━━ Thread 口径：文档自身的 id ━━━
同一份文档的多次重解析/重新抽取 ⇒ 同一 Thread ⇒ 能看到该文档的完整事件史。
（`documents.lifecycle_status` 的真实取值只有 `stored` 一类，
本模块不依赖它做判定 —— **不碰生命周期状态机**。）

━━━ action 是什么 ━━━
本 Source **不执行**任何抽取/建实体动作，只声明"建议做什么"：
`extract_entities`（抽取实体）+ `review_trace`（待审追溯）。
理由与 observe 模式一致：抽取会写图谱 ⇒ 副作用不可逆 ⇒ 人确认后再做。
"""

from __future__ import annotations

from core.events.model import Admission, OBSERVE_MODE, build_trigger
from core.events.sources import SourceSpec

__all__ = ["build_docs_source", "DOCS_SOURCE_KIND", "emit_document_stored"]

DOCS_SOURCE_KIND = "documents"


def build_docs_source(allowed_senders: frozenset = frozenset(),
                      scope_keys: frozenset = frozenset(),
                      mode: str = OBSERVE_MODE,
                      policy_version: str = "v1") -> SourceSpec:
    """构造文档入库来源。

    ⚠️ 默认 `allowed_senders` 为空 ⇒ **默认全部事件只观察不建工单**
    （准入表：名单外 + 未明确@ ⇒ `not_listed` filtered）。
    这是刻意的：文档入库是**系统内部事件**（不是人发的请求），
    它天然属于"该做但不紧急"的一类 ⇒ 默认记录、等人决定要不要批量处理。

    真正的"系统内部事件"如何准入？见 `_to_trigger` 里 `actor_type='system'` 的处理：
    系统事件走 `listen='all'` 的分支，不依赖白名单。
    """
    adm = Admission(
        source=DOCS_SOURCE_KIND,
        allowed=allowed_senders,
        # 系统内部事件：收全部（不靠 @ 触发）
        listen="all",
        scope_keys=scope_keys,
        mode=mode,
        version=policy_version,
    )

    def _to_trigger(raw: dict):
        raw = raw or {}
        doc_id = str(raw.get("external_id") or "").strip()
        filename = str(raw.get("filename") or "")
        actor = str(raw.get("actor") or "").strip() or "system"
        return build_trigger(
            source=DOCS_SOURCE_KIND,
            external_id=doc_id,
            thread_scope=doc_id,          # 同一文档 = 同一 Thread
            actor=actor,
            actor_type=str(raw.get("actor_type") or "system"),
            addressed=None,               # 系统事件不需要"明确@"
            text=filename,                # 只留文件名（人类可读），不塞正文
            payload={
                "filename": filename,
                "chunk_count": raw.get("chunk_count"),
                "entity_count": raw.get("entity_count"),
                "doc_type": raw.get("file_type") or "",
            },
            version=str(raw.get("version") or ""),
            at=str(raw.get("at") or ""),
        )

    def _action_for(trigger):
        """建议动作：抽取实体 + 待审追溯（**只声明，不执行**）。"""
        fn = (trigger.payload or {}).get("filename") or trigger.text or trigger.external_id
        return ("extract_entities", f"文档入库待抽取实体: {fn}")

    return SourceSpec(
        kind=DOCS_SOURCE_KIND,
        title="文档入库",
        admission=adm,
        to_trigger=_to_trigger,
        action_for=_action_for,
        scope_of=lambda raw: str((raw or {}).get("external_id") or ""),
    )


def emit_document_stored(conn, doc_id, filename: str = "",
                         chunk_count: int = 0, entity_count: int = 0,
                         file_type: str = "", actor: str = "system",
                         actor_type: str = "system", at: str = "",
                         registry=None) -> dict:
    """文档入库完成后发一条事件（**幂等** ⇒ 同一 doc_id 重复调用只产生一条）。

    ⚠️ **本函数永不抛异常、永不阻断入库**：
    事件是"锦上添花"的旁路，入库成功才是主流程。
    ⇒ 任何失败都只记日志。
    （与本工程"降级必须留痕"纪律同源：失败要留痕，不是静默吞。）

    :param registry: 可选，指定来源注册表。**默认用全局注册表**；
        传参是为了可测（单测用独立注册表，避免污染全局）。
    :returns: 事件处置结果 dict；失败时 {"error": ...}
    """
    try:
        from core.events.sources import get_registry
        from repositories.event_repo import EventRepo
        from services.event_service import EventService

        reg = registry if registry is not None else get_registry()
        if reg.get(DOCS_SOURCE_KIND) is None:
            # 未注册 ⇒ 不静默跳过，返回可查的原因
            return {"error": "source_not_registered", "kind": DOCS_SOURCE_KIND}

        svc = EventService(EventRepo(conn), reg)
        res = svc.receive(DOCS_SOURCE_KIND, {
            "external_id": str(doc_id),
            "filename": filename,
            "chunk_count": chunk_count,
            "entity_count": entity_count,
            "file_type": file_type,
            "actor": actor,
            "actor_type": actor_type,
            "at": at,
        })
        return res
    except Exception as e:
        try:
            import logging
            logging.getLogger(__name__).warning(
                "文档入库事件发送失败（不阻断入库）: %s", str(e)[:160])
        except Exception:
            pass
        return {"error": str(e)[:200]}
