# -*- coding: utf-8 -*-
"""意图样本池 API（2026-09-26）：设置页「意图样本」面板的后端契约。

## 为什么要有它（产品口径）
意图路由的评测集原先硬编码在测试脚本里（29 例），线上真实说法进不来、标定窗口不稳。
本模块把评测集变成**可维护的资产**：线上输入自动进候选池 → 人工确认 → 直接成为评测集 →
页面一键跑分。三件事闭环：**采样（哪里来）→ 标注（谁负责）→ 评测（效果如何）**。

## 关键约束（与仓储层一致，前端也必须遵守）
1. **评测只消费 `status='confirmed'`** —— 自动采集写的是 `suggested`（系统自己的判定）。
   拿系统判定当标签去评测系统＝自我循环，会永远 100%。
2. **改标注 ≠ 改路由**：本模块只维护样本池，不写 `intent_rules` / 不改词表。
   要让系统"学会"某条规则，走既有「意图规则」配置（那是另一条链路，避免此处越权）。
3. 高频优先：候选按 `seen_count` 降序，先标真实高频说法（标注性价比最高）。

端点：
    GET    /api/intent-samples                    列表 + 统计（筛选 status/intent/source/q/分页）
    POST   /api/intent-samples                    新增（手工录入）
    PUT    /api/intent-samples/{sid}               改标注/状态/备注
    DELETE /api/intent-samples/{sid}               删除
    POST   /api/intent-samples/import              从既有数据回填（builtin|messages|query_trace）
    POST   /api/intent-samples/suggest             对未标注的跑一次识别给建议标签（小批量）
    POST   /api/intent-samples/run-eval            用 confirmed 集跑评测（准确率/macro-F1/混淆矩阵）
    GET    /api/intent-samples/export              导出评测集 JSON
"""
from fastapi import APIRouter, Depends

from core.deps import db_session, current_user
from core.audit import audit, audit_user
from repositories.intent_sample_repo import IntentSampleRepo

router = APIRouter(tags=["意图样本池"])

# ⚠️ 写端点**不加权限门**，与同页的 `PUT /api/system/config/static`（routers/config.py，无权限依赖）保持一致：
#    同一块设置页上两个按钮要求不同权限，会出现"改配置能点、改样本 403"的割裂。
#    所有写操作保留 audit 留痕；若后续要给设置页统一加门，应与 config 端点一并加（不要只加这一处）。


@router.get("/api/intent-samples")
def list_samples(status: str = "", intent: str = "", source: str = "", q: str = "",
                 limit: int = 200, offset: int = 0, conn=Depends(db_session)):
    """列表 + 统计。`stats.by_status` 供页面顶部显示「待确认 N / 已确认 M」。"""
    return IntentSampleRepo(conn).list(status=status or None, intent=intent or None,
                                       source=source or None, q=q or None,
                                       limit=max(1, min(limit, 1000)), offset=max(0, offset))


@router.post("/api/intent-samples")
def create_sample(body: dict, conn=Depends(db_session),
                  user=Depends(current_user)):
    """手工录入样本。body: {text, intent?, status?(默认 confirmed，因为是人录的), source?, note?}"""
    r = IntentSampleRepo(conn).upsert(
        body.get("text") or "", intent=body.get("intent") or "",
        status=body.get("status") or "confirmed", source="manual",
        note=body.get("note") or "", created_by=audit_user(user))
    if r.get("ok"):
        audit(audit_user(user), "intent_sample_create", body.get("text", "")[:80])
    return r


@router.put("/api/intent-samples/{sid}")
def update_sample(sid: int, body: dict, conn=Depends(db_session),
                  user=Depends(current_user)):
    """改标注/状态/备注。`status='confirmed'` 时必须有标签（否则评测集会出现"期望为空"的用例）。"""
    r = IntentSampleRepo(conn).update(sid, intent=body.get("intent"), status=body.get("status"),
                                      note=body.get("note"))
    if r.get("ok"):
        audit(audit_user(user), "intent_sample_update", f"id={sid} {body}")
    else:
        r["message"] = {"confirm_requires_intent": "设为已确认前必须选择意图（不能留空）",
                        "bad_status": "状态取值非法",
                        "not_found": "样本不存在"}.get(r.get("reason", ""), r.get("reason", ""))
    return r


@router.delete("/api/intent-samples/{sid}")
def delete_sample(sid: int, conn=Depends(db_session),
                  user=Depends(current_user)):
    r = IntentSampleRepo(conn).delete(sid)
    audit(audit_user(user), "intent_sample_delete", f"id={sid}")
    return r


@router.post("/api/intent-samples/import")
def import_samples(body: dict, conn=Depends(db_session),
                   user=Depends(current_user)):
    """从既有数据回填候选。body: {source: builtin|messages|query_trace, limit?}"""
    src = body.get("source") or "messages"
    r = IntentSampleRepo(conn).import_source(src, limit=int(body.get("limit") or 200))
    if r.get("ok"):
        audit(audit_user(user), "intent_sample_import", f"{src} +{r.get('added')}/~{r.get('updated')}")
    return r


@router.get("/api/intent-samples/overrides")
def list_overrides(limit: int = 50, conn=Depends(db_session)):
    """2026-11-07 P0-2：列出**用户澄清卡改选**产生的待审样本。

    只读。note 已解析成 `parsed={override,detected,route,route_conf}`，
    人工确认走既有 `PUT /api/intent-samples/{sid}`（status=confirmed + intent=用户改选值）。
    ⚠️ 绝不在这里自动确认 —— 与「只提议不自动应用」一致。
    """
    from services.intent_override_feedback import override_candidates
    items = override_candidates(conn, limit=max(1, min(limit, 500)))
    pending = [x for x in items if x.get("status") != "confirmed"]
    return {"ok": True, "total": len(items), "pending": len(pending),
            "items": items,
            "hint": "确认时把 intent 设为 parsed.override，status 设为 confirmed"}


@router.post("/api/intent-samples/overrides/confirm")
def confirm_overrides(body: dict, conn=Depends(db_session),
                      user=Depends(current_user)):
    """批量审核改选样本：只处理**显式给出的 id 列表**，且必须有 override 值。

    为什么不做「一键全确认」：改选值是用户当时点的按钮，仍需人工判断是否是有效样本
    （误点、玩笑、系统包装文本都可能混进来）。逐条 id 是审核行为的最小可审计单位。
    """
    ids = [int(x) for x in (body.get("ids") or []) if str(x).isdigit()]
    if not ids:
        return {"ok": False, "reason": "no_ids"}
    import json as _json
    repo = IntentSampleRepo(conn)
    done, skipped = [], []
    for sid in ids[:100]:
        row = conn.execute("SELECT id,status,source,note FROM intent_samples WHERE id=?",
                           (sid,)).fetchone()
        if not row or row["status"] == "confirmed" or row["source"] != "override":
            skipped.append(sid)
            continue
        try:
            ov = (_json.loads(row["note"] or "{}").get("override") or "").strip()
        except Exception:
            ov = ""
        if not ov:
            skipped.append(sid)
            continue
        repo.update(sid, intent=ov, status="confirmed")
        done.append({"id": sid, "intent": ov})
    audit(audit_user(user), "intent_override_confirm",
          f"批量确认改选样本 {len(done)} 条（跳过 {len(skipped)} 条）", conn=conn)
    return {"ok": True, "confirmed": done, "skipped": skipped}


@router.post("/api/intent-samples/suggest")
def suggest_samples(body: dict, conn=Depends(db_session),
                    user=Depends(current_user)):
    """对 status='new' 的样本跑一次**真实意图识别**，写入建议标签（小批量，默认 40 条）。

    刻意分批：每条都要 embedding（冷启更慢），一次跑全池会卡住请求；人工确认本来就该分批做。
    """
    return IntentSampleRepo(conn).suggest_batch(limit=int(body.get("limit") or 40))


@router.post("/api/intent-samples/run-eval")
def run_eval(body: dict, conn=Depends(db_session),
             user=Depends(current_user)):
    """用 `confirmed` 集跑评测：准确率 / macro-F1 / 混淆矩阵 / 错例。

    口径与 `tests/manual_verify/eval_intent_routing.py` **完全一致**（同一封装 `metrics()`），
    并遵守既有纪律：**绕开意图缓存**（否则测的是历史结论，A/B 会假绿 —— AGENTS.md 坑 26）。
    返回 `{ok:false, reason:'no_confirmed'}` 时不报错，由页面引导"先确认样本"。
    """
    repo = IntentSampleRepo(conn)
    cases = repo.confirmed_cases()
    if not cases:
        return {"ok": False, "reason": "no_confirmed", "n": 0,
                "message": "还没有「已确认」样本 —— 请先在下方确认若干条（或从内置 29 例导入）"}
    from agent.pipeline import AgentPipeline
    from agent.intent import IntentRouter
    pipe = AgentPipeline()
    pipe._load_db_agents()          # 真实请求路径同款：DB Agent 关键词 + 语义索引
    rt = pipe.router
    og, os_ = IntentRouter._cache_get, IntentRouter._cache_set
    IntentRouter._cache_get = lambda self, *a, **k: None
    IntentRouter._cache_set = lambda self, *a, **k: None
    y_true, y_pred, rows = [], [], []
    try:
        for text, want in cases:
            got = rt.detect(text)
            meta = rt.get_last_meta()
            y_true.append(want); y_pred.append(got)
            rows.append({"text": text, "want": want, "got": got, "ok": got == want,
                         "route": meta.get("route", ""), "confidence": meta.get("confidence")})
    finally:
        IntentRouter._cache_get, IntentRouter._cache_set = og, os_
    from repositories.intent_sample_repo import metrics
    m = metrics(y_true, y_pred)
    m.update({"ok": True, "wrong": [r for r in rows if not r["ok"]]})
    audit(audit_user(user), "intent_sample_eval", f"n={m['n']} acc={m['accuracy']}")
    return m


@router.get("/api/intent-samples/export")
def export_samples(conn=Depends(db_session)):
    """导出评测集 JSON（可直接喂给标定脚本 / 进版本库做回归基线）。"""
    return IntentSampleRepo(conn).export()

# ─────────────────────────────────────────────────────────────────────────────
# 意图字典（intent_taxonomy）—— 2026-11-07（米爸第 17 轮）
#
# 为什么单独一组端点：用户反馈「审核页只能单选意图，无法新增和编辑」。
# 根因是意图清单此前**从 agents 表派生**、中文标签与示例说法**硬编码在代码里**
# ⇒ 没有可维护的实体。现在引入独立字典表（canonical + 别名 + scope note +
# 示例说法 + 版本 + 弃用路径），本组端点就是它的维护入口。
#
# 纪律：
#   · `name` 是 canonical，**新增后不可改**（历史样本、路由都用它作键）；
#   · 编辑只改 label/scope_note/aliases/examples/cluster/agent_name/status；
#   · 停用走 `status='deprecated'`（不删行）——历史标注仍能解析；
#   · 全部写操作留 audit；不自动改 `intent_samples`（改标注是另一件事）。
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/api/intent-taxonomy")
def list_taxonomy(status: str = "", conn=Depends(db_session)):
    """列出意图字典。页面用它渲染下拉、中文名、scope note 与示例说法。"""
    from core import intent_taxonomy as itx
    items = itx.list_all(conn, status=status or None)
    return {"ok": True, "total": len(items), "items": items,
            # 自动发现：库里已被标注、但字典里还没有的意图名（"该补哪些"不用人工想）
            "missing": itx.suggest_new_names(conn)}


@router.post("/api/intent-taxonomy")
def create_taxonomy(body: dict, conn=Depends(db_session),
                    user=Depends(current_user)):
    """新增一个意图。允许 `agent_name` 为空（纯分类标签，不对应任何 Agent）。"""
    from core import intent_taxonomy as itx
    r = itx.upsert(
        conn, name=body.get("name") or "",
        label=body.get("label") or "",
        scope_note=body.get("scope_note"),
        aliases=body.get("aliases"), examples=body.get("examples"),
        cluster=body.get("cluster"), agent_name=body.get("agent_name"),
        status=body.get("status") or "active",
        owner=audit_user(user))
    if r.get("ok"):
        audit(audit_user(user), "intent_taxonomy_create",
              f"新增意图 {body.get('name')}", conn=conn)
    return r


@router.put("/api/intent-taxonomy/{name}")
def update_taxonomy(name: str, body: dict, conn=Depends(db_session),
                    user=Depends(current_user)):
    """编辑一个意图（name 不可改）。字段为 None 表示"不改这一项"。"""
    from core import intent_taxonomy as itx
    r = itx.upsert(
        conn, name=name,
        label=body.get("label"),
        scope_note=body.get("scope_note"),
        aliases=body.get("aliases"), examples=body.get("examples"),
        cluster=body.get("cluster"), agent_name=body.get("agent_name"),
        status=body.get("status"), merged_into=body.get("merged_into"),
        owner=audit_user(user))
    if r.get("ok"):
        audit(audit_user(user), "intent_taxonomy_update", f"编辑意图 {name}", conn=conn)
    return r


@router.post("/api/intent-taxonomy/resolve")
def resolve_taxonomy(body: dict, conn=Depends(db_session)):
    """把一句/一个旧标签归一化到 canonical 名（别名表）。供前端校验与批量迁移用。"""
    from core import intent_taxonomy as itx
    return {"ok": True, "input": body.get("text") or "",
            "resolved": itx.resolve_alias(conn, body.get("text") or "")}
