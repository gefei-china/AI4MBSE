# -*- coding: utf-8 -*-
"""事件驱动 API（2026-11-01，Super-agent 落地 P0-4）。

## 端点
    GET  /api/events/sources              已注册来源列表（含 observe/execute 与策略版本）
    POST /api/events/sources              注册/更新来源配置（准入规则）
    POST /api/events/sources/{kind}/mode  切 observe ⇄ execute（**放开自动执行的唯一开关**）
    POST /api/events/{kind}               接收一个外部事件（入站事件入口）
    GET  /api/events/triggers             事件台账（含 disposition + reason）
    GET  /api/events/workitems            工作单元列表（观察记录 = "本应做什么"）
    GET  /api/events/workitems/{id}       单个工单详情（含授权快照）
    POST /api/events/workitems/{id}/advance  推进工单状态（状态机判定在 service）
    GET  /api/events/stats                事件域概览

## 为什么入站端点需要鉴权（不是"公开 webhook"）
`POST /api/events/{kind}` 会**创建工作单元**。虽然默认 observe（不执行动作），
但"匿名可投递事件"意味着任何人都能往库里灌事件、污染台账、消耗准入额度。
⇒ 写类端点走 `require_any_permission(EVENT_WRITE_PERMS)`（与作业提交同档）。

⚠️ **来源级白名单**是第一道防线，端点鉴权是第二道 —— 两道都要有：
  · 端点鉴权 = "谁能往这个系统投事件"（部署级）
  · 来源白名单 = "这个来源里谁能发起工作"（来源级）
只做前者 ⇒ 任何登录用户都能冒充名单内的人触发工作。

## 幂等契约（与 core/job_queue 同源，但判据不同）
事件端点的幂等键 = **来源 + 外部事件 ID**（由来源自己定义，见 SourceSpec.to_trigger）。
⇒ 外部系统重投同一条消息 ⇒ 同一 Trigger ⇒ 不重复建工单，返回 disposition='duplicate'。
⚠️ 与 job_queue 的差别：作业的幂等键是"当次在途操作"（终态可重跑），
   事件的幂等键是"外部事实"（**终态也不该重跑** —— 同一个分支变更被做两次是事故）。
"""
from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from core.deps import db_session, current_user, require_any_permission
from repositories.event_repo import EventRepo
from services.event_service import EventService

router = APIRouter(tags=["事件驱动"])

# 事件投递会创建工单并写台账 → 与作业提交同档（写类，非只读）
EVENT_WRITE_PERMS = [("config", "write"), ("kb_review", "modify"), ("kb_ontology", "edit")]


def _limit_default(default: int = 50, cap: int = 200) -> int:
    return max(1, min(int(default or 50), cap))


def _svc(conn) -> EventService:
    return EventService(EventRepo(conn))


@router.get("/api/events/sources")
def list_sources(conn=Depends(db_session), user=Depends(current_user)):
    """已注册来源列表（DB 为准，含 mode 与策略版本）。"""
    repo = EventRepo(conn)
    items = repo.list_sources()
    return {
        "items": items,
        "registered": _svc(conn).registry.kinds(),
        "note": "mode=observe 表示只记录不执行；切 execute 需显式调用 mode 端点",
    }


@router.post("/api/events/sources")
def upsert_source(body: dict, conn=Depends(db_session),
                  user=Depends(require_any_permission(EVENT_WRITE_PERMS))):
    """注册/更新来源配置。

    body = {kind, title?, listen?, allowed?, scope_keys?, mode?, policy_version?}

    ⚠️ `kind` 一旦有历史数据就**不可改语义**（它出现在历史 Trigger 里）。
    改策略请用同一 kind 更新配置；要换语义请新建 kind。
    """
    spec = build_source_from_body(body)
    if not spec:
        return JSONResponse({"error": "缺少必填字段 kind / allowed / scope_keys"}, 400)
    try:
        row = _svc(conn).register_source(spec)
    except Exception as e:
        return JSONResponse({"error": "注册来源失败: %s" % e}, 500)
    return {"ok": True, "source": row}


@router.post("/api/events/sources/{kind}/mode")
def set_source_mode(kind: str, body: dict, conn=Depends(db_session),
                    user=Depends(require_any_permission(EVENT_WRITE_PERMS))):
    """切换来源模式：observe ⇄ execute。

    ⚠️ **这是放开自动执行的唯一开关**，因此：
      · 要求写权限；
      · 必须写审计（复用 `audit()`）—— 谁在什么时候放开了哪个来源，要能查；
      · 切回 observe **不撤回已建工单**（对齐 dsh「Policy 变更不撤销 Mandate，
        只把变更投递给在途工单」—— 撤销会让半成品留在库里）。
    """
    from core.audit import audit, audit_user
    mode = str((body or {}).get("mode") or "").strip()
    if mode not in ("observe", "execute"):
        return JSONResponse({"error": "mode 必须是 observe 或 execute"}, 400)
    repo = EventRepo(conn)
    if not repo.get_source(kind):
        return JSONResponse({"error": "来源不存在: %s" % kind}, 404)
    n = repo.set_source_mode(kind, mode)
    if n:
        try:
            audit(audit_user(user), "event_source_mode",
                  f"事件来源 {kind} 模式切为 {mode}", conn=conn)
        except Exception:
            pass   # 审计失败不得阻断切换（但模式已改，需人工核对）
    return {"ok": True, "kind": kind, "mode": mode,
            "note": "已建工单的模式是快照，不随本次切换改变；仅新工单用新模式"}


@router.post("/api/events/{kind}")
def receive_event(kind: str, body: dict, conn=Depends(db_session),
                  user=Depends(require_any_permission(EVENT_WRITE_PERMS))):
    """接收一个外部事件 ⇒ 返回处置结论。

    body = {external_id, thread_scope, actor, actor_type?, addressed?, text?, payload?, version?, at?}

    返回 = {disposition, reason, strength, trigger_id, workitem_id?, mode, accepted}
    ⚠️ disposition 取值：admitted / filtered / duplicate / rejected
       —— **永远有结论**，不会出现"提交了但什么都没发生且查不到"。
    """
    svc = _svc(conn)
    try:
        res = svc.receive(kind, body or {})
    except KeyError as e:
        return JSONResponse({"error": "未注册的事件来源: %s" % kind,
                             "registered": svc.registry.kinds()}, 400)
    except Exception as e:
        return JSONResponse({"error": "事件处理失败: %s" % e}, 500)
    if res.get("disposition") == "rejected":
        return JSONResponse(res, 400)
    return res


@router.get("/api/events/triggers")
def list_triggers(conn=Depends(db_session), user=Depends(current_user),
                  source: str = "", thread_key: str = "",
                  limit: int = Query(50, ge=1, le=200)):
    """事件台账（含 disposition + reason —— 被过滤的事件也在，能回答"为什么没动"）。"""
    items = EventRepo(conn).list_triggers(source=source, thread_key=thread_key,
                                          limit=_limit_default(limit, 200))
    return {"items": items}


@router.get("/api/events/workitems")
def list_workitems(conn=Depends(db_session), user=Depends(current_user),
                   source: str = "", state: str = "", mode: str = "",
                   limit: int = Query(50, ge=1, le=200)):
    """工作单元列表。

    ⚠️ `mode=observe` 的条目 = **"本应做什么"的观察记录**（P0-3 的核心产物）。
    观察期内这些条目的 action 不会被执行。
    """
    items = EventRepo(conn).list_workitems(source=source, state=state, mode=mode,
                                           limit=_limit_default(limit, 200))
    return {"items": items,
            "note": "observe 工单只记录不执行；action 字段是建议动作"}


@router.get("/api/events/workitems/{wid}")
def get_workitem(wid: str, conn=Depends(db_session), user=Depends(current_user)):
    """单个工单详情（含授权快照：actor / policy_version / strength）。"""
    wi = EventRepo(conn).get_workitem(wid)
    if not wi:
        return JSONResponse({"error": "工单不存在", "id": wid}, 404)
    trg = EventRepo(conn).get_trigger(wi.get("trigger_id") or "")
    return {"workitem": wi, "trigger": trg,
            "mandate": {
                "actor": wi.get("actor"),
                "actor_type": wi.get("actor_type"),
                "policy_version": wi.get("policy_version"),
                "strength": wi.get("strength"),
                "scope": wi.get("scope"),
            }}


@router.post("/api/events/workitems/{wid}/advance")
def advance_workitem(wid: str, body: dict, conn=Depends(db_session),
                     user=Depends(require_any_permission(EVENT_WRITE_PERMS))):
    """推进工单状态。

    body = {state, result?, error?}    state ∈ open/running/done/failed/stopped

    ⚠️ 状态机判定在 service 层（`decide_workitem_transition`），
       非法迁移返回 409 而不是静默改库。
    ⚠️ 本端点**只改状态，不执行 action** —— 执行由人确认后显式发起。
    """
    state = str((body or {}).get("state") or "").strip()
    if not state:
        return JSONResponse({"error": "缺少 state"}, 400)
    res = _svc(conn).advance(wid, state,
                             result=str((body or {}).get("result") or ""),
                             error=str((body or {}).get("error") or ""))
    if not res.get("ok"):
        return JSONResponse(res, 409)
    return res


@router.get("/api/events/stats")
def events_stats(conn=Depends(db_session), user=Depends(current_user)):
    """事件域概览。"""
    return {"stats": EventRepo(conn).stats()}


# ── 来源构造（从请求体）──
def build_source_from_body(body: dict):
    """把请求体变成 SourceSpec。

    ⚠️ **这里不接"工具/动作执行器"** —— 来源只声明"收到事件后建议做什么动作"，
    真正的执行由上层在**人确认后**发起。
    这样做的理由：若来源自带执行器，"observe 模式不执行"就无从保证
    （执行器已经挂在来源上了，很难证明它没被调用）。
    """
    from core.events.model import Admission, OBSERVE_MODE, build_trigger
    from core.events.sources import SourceSpec

    body = body or {}
    kind = str(body.get("kind") or "").strip()
    if not kind:
        return None
    listen = str(body.get("listen") or "mentions").strip()
    if listen not in ("mentions", "all"):
        listen = "mentions"
    mode = str(body.get("mode") or OBSERVE_MODE).strip()
    if mode not in ("observe", "execute"):
        mode = OBSERVE_MODE

    adm = Admission(
        source=kind,
        allowed=frozenset(str(x).strip().lower() for x in (body.get("allowed") or []) if str(x).strip()),
        listen=listen,
        scope_keys=frozenset(str(x).strip() for x in (body.get("scope_keys") or []) if str(x).strip()),
        mode=mode,
        version=str(body.get("policy_version") or "v1"),
    )

    def _to_trigger(raw: dict):
        raw = raw or {}
        ext = str(raw.get("external_id") or "").strip()
        scope = str(raw.get("thread_scope") or "").strip() or ext
        actor = str(raw.get("actor") or "").strip()
        addressed = raw.get("addressed")
        addressed = str(addressed) if addressed in ("mention", "direct") else None
        return build_trigger(
            source=kind, external_id=ext, thread_scope=scope,
            actor=actor, actor_type=str(raw.get("actor_type") or "human"),
            addressed=addressed, text=str(raw.get("text") or ""),
            payload=raw.get("payload") or {},
            version=str(raw.get("version") or ""),
            at=str(raw.get("at") or ""),
        )

    def _action_for(trigger):
        """默认动作：**只描述，不执行**。来源可自定义（注册 SourceSpec 时覆盖）。"""
        return (f"evaluate:{trigger.source}", (trigger.text or "")[:60])

    return SourceSpec(
        kind=kind,
        title=str(body.get("title") or kind),
        admission=adm,
        to_trigger=_to_trigger,
        action_for=_action_for,
        scope_of=lambda raw: str((raw or {}).get("thread_scope") or ""),
    )
