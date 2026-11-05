# -*- coding: utf-8 -*-
"""上下文归因只读诊断 API（P0-8 · 2026-10-26）。

## 为什么需要

`context_audit` 自 2026-10-26 起在真实链路落库，但**没有消费方** ⇒
数据在库里、开关取舍靠猜。本路由把它变成可查的报表（**全部只读**）。

## 端点

| 方法 | 路径 | 回答什么问题 |
|---|---|---|
| GET | `/api/diagnostics/context-audit/blocks` | 每轮 token 花在哪个块（找头部消耗块） |
| GET | `/api/diagnostics/context-audit/daily` | 按日趋势 + 各改动指标累计（开关前后对照） |
| GET | `/api/diagnostics/context-audit/conversations` | 最贵会话 TOP N；传 `conversation_id` 则给逐 trace 明细 |
| GET | `/api/diagnostics/context-audit/switches` | 各开关**实际触发次数**（"改了到底生效没"） |
| GET | `/api/diagnostics/context-audit/overview` | 上述四项的合并摘要（一次拿全） |

## 权限口径（与 monitor / ux_metrics 同纪律）

只读端点**不加权限门**：本仓既有运维类只读端点（`/api/monitor/dashboard`、
`/api/ux-metrics/summary`）均为开放读。原因：加了门会需要前端配套跳转，
而这类数据是"内部成本诊断"，不含用户隐私内容（块名 + token 数 + 会话 id）。

⚠️ 但**不做任何写操作** —— 本路由全部方法都只 `SELECT`。

## 与真实计费的边界（务必不要误读）

`context_audit` 只记 **system prompt 的块**，不含 messages 与 tools payload。
⇒ 块合计 **必然小于** `llm_usage_stats.prompt_tokens`，差额 = messages + tools。
`conversations?conversation_id=` 端点会显式给出该差额，便于核对。
"""
from fastapi import APIRouter, Depends

from core.deps import db_session
from repositories.context_audit_repo import ContextAuditRepo

router = APIRouter(tags=["诊断 · 上下文归因"])


@router.get("/api/diagnostics/context-audit/blocks")
def audit_blocks(days: int = 7, limit: int = 40, conn=Depends(db_session)):
    """按块聚合：token / 字符 / 出现次数 / 占比 / 单次平均。"""
    return ContextAuditRepo(conn).blocks(days=days, limit=limit)


@router.get("/api/diagnostics/context-audit/daily")
def audit_daily(days: int = 14, conn=Depends(db_session)):
    """按日趋势：system 块总量、静态/动态比、各项改动指标（开关前后对照用）。"""
    return ContextAuditRepo(conn).daily(days=days)


@router.get("/api/diagnostics/context-audit/conversations")
def audit_conversations(days: int = 7, limit: int = 20, conversation_id: int = 0,
                        conn=Depends(db_session)):
    """会话维度：`conversation_id=0` 给最贵 TOP N；`>0` 给该会话逐 trace 明细。"""
    return ContextAuditRepo(conn).conversations(
        days=days, limit=limit, conversation_id=conversation_id)


@router.get("/api/diagnostics/context-audit/switches")
def audit_switches(days: int = 7, conn=Depends(db_session)):
    """各开关的实际触发次数 —— 回答"改了但到底生效了吗"。"""
    return ContextAuditRepo(conn).switches(days=days)


@router.get("/api/diagnostics/context-audit/overview")
def audit_overview(days: int = 7, conn=Depends(db_session)):
    """一次拿全：块分布 + 开关触发 + 最贵会话 + 按日趋势 + 有数据的天数。

    ★ 2026-10-27：补返 `daily`（此前只返回了 `days_with_data` 这个计数，
      而 `daily` 本身算完就被丢掉 ⇒ 前端趋势图拿不到数据，只能再多打一次接口）。
      观测面板是单页一次性渲染，往返次数直接影响打开速度。
    """
    repo = ContextAuditRepo(conn)
    blocks = repo.blocks(days=days)
    switches = repo.switches(days=days)
    convs = repo.conversations(days=days, limit=5)
    daily = repo.daily(days=days)
    return {
        "window_days": days,
        "has_data": bool(blocks.get("total_tokens")),
        "blocks": blocks,
        "switches": switches,
        "daily": daily,
        "top_conversations": convs.get("conversations") or [],
        "days_with_data": len(daily.get("rows") or []),
        "hint": ("若 has_data=false：说明该窗口内没有真实装配落库（请求量低或"
                 "context_audit.enabled 被关）。"),
    }
