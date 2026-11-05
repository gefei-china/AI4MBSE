"""多方案设计域：/api/alternatives/*（FR-MG-6，2026-11-03）。

本路由把「多方案比较 + 多维度打分」暴露为**可复算的确定性端点**：

- `GET  /api/alternatives/dimensions` —— 维度目录（前端不硬编码维度名/方向）
- `POST /api/alternatives/score`      —— 对给定候选方案打分，返回对比矩阵

为什么需要这个端点（而不是只在对话里出卡片）：
1. **可复算**：同一批候选 + 同一权重 ⇒ 同一结果。页面与对话共用同一算法层
   （`core/alternatives.py`），不会各算各的。
2. **可审计**：权重覆盖、并列判定、覆盖率门槛的口径都在响应里带出去，
   调用方能自己核对「这个推荐是怎么来的」。
3. **前端不重算**：前端只渲染 `alternatives` 矩阵；它自己再算一遍必然与后端漂移。

⚠️ 本路由**只读、无副作用**：不写库、不调 LLM、不落版本。
打分算法是纯函数（`core/alternatives.py`），输入相同即输出相同。
"""
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from core.alternatives import (
    build_comparison_table,
    load_config_from_db,
    normalize_dimensions,
    score_alternatives,
)
from core.deps import db_session, current_user
from core.audit import audit, audit_user

router = APIRouter(tags=["多方案设计"])


class AlternativeItem(BaseModel):
    """一个候选方案。

    `scores` 用**规范维度 key**（performance/cost/risk/extensibility/requirement_coverage）。
    缺失的维度 ⇒ 该方案该维度记为「未测量」（**不补零**），且覆盖率下降。
    """

    name: str = Field(..., description="方案名")
    summary: str = Field("", description="一句话概述")
    scores: dict = Field(default_factory=dict, description="各维度原始量")
    rationale: dict = Field(default_factory=dict, description="各维度取值依据")


class ScoreRequest(BaseModel):
    alternatives: list = Field(..., description="候选方案列表")
    weights: dict = Field(default_factory=dict, description="维度权重覆盖（空=默认）")
    tie_epsilon: float = Field(0.02, description="并列判定阈值（最高分与次高分差小于它即并列）")
    min_coverage: float = Field(0.6, description="推荐门槛：覆盖率低于它的方案不参与推荐")


@router.get("/api/alternatives/dimensions")
def list_dimensions():
    """维度目录：key / 中文名 / 方向 / 单位 / 当前生效权重。

    数据源 = **平台配置**（`generate_rules` 表）⇒ 运营改配置即生效，
    前端据此渲染表头与方向提示，**不在前端硬编码维度名**。
    """
    cfg = load_config_from_db()
    dims = normalize_dimensions(cfg.get("dimensions"))
    weights = cfg.get("weights") or {}
    for d in dims:
        d["weight"] = weights.get(d["key"], 0.0)
    return {
        "dimensions": dims,
        "keys": [d["key"] for d in dims],
        "source": "平台配置" if cfg.get("dimensions") else "默认维度集",
        "tie_epsilon": cfg.get("tie_epsilon"),
        "min_coverage": cfg.get("min_coverage"),
        "max_count": cfg.get("max_count"),
    }


@router.post("/api/alternatives/score")
def score(body: ScoreRequest):
    """对候选方案做多维度归一化 + 加权汇总 + 推荐判定。

    返回 `{ok, alternatives, scoring, dimensions}`：
    - `alternatives` = 矩阵（rows=维度行、columns=方案列），前端直接渲染；
    - `scoring` = 打分明细（每方案原始量/归一化值/总分/覆盖率/缺失维度/排名/是否推荐）。

    ⚠️ 口径与**平台工具** `alternatives_score` 完全同源（同一算子 + 同一份配置）——
       页面与对话不会各算各的。

    口径提醒：
    - 缺维度**不补零**（补零 = 把"没测"说成"最差"）；
    - 覆盖率低于 `min_coverage` 的方案**不参与推荐**（防"少测多赢"）；
    - 最高分与次高分差 < `tie_epsilon` ⇒ 判**并列推荐**，不擅自宣称最优。
    """
    items = [a for a in (body.alternatives or []) if isinstance(a, dict)]
    if not items:
        return JSONResponse({"ok": False, "error": "alternatives 为空，无法打分"}, 400)
    cfg = load_config_from_db()
    scored = score_alternatives(
        items,
        weights=body.weights or cfg.get("weights"),
        tie_epsilon=float(body.tie_epsilon or cfg.get("tie_epsilon") or 0.02),
        min_coverage=float(body.min_coverage or cfg.get("min_coverage") or 0.6),
        dimensions=cfg.get("dimensions"),
        aliases=cfg.get("aliases"),
    )
    return {
        "ok": True,
        "alternatives": build_comparison_table(scored),
        "scoring": {
            "version": scored.get("version"),
            "weights": scored.get("weights"),
            "items": scored.get("items"),
            "notes": scored.get("notes") or [],
        },
        "dimensions": scored.get("dimensions") or [],
    }


# ═══════════════════════ 选型决策（FR-MG-6 收口，2026-11-04）═════════════════════
#
# 对比卡此前只到「推荐 / 并列 / 数据不足」三态 —— **人的选型决定没有落点**：
# 选了哪个方案、谁选的、当时的得分矩阵，事后查不到。本组端点把决策变成
# 可审计记录（append-only；重选 = 追加新行，卡片读最新）。
#
# ⚠️ 与"入库为 SysML 版本"的分工：这里只记录**决策**，不产生模型版本；
#    将来"选定 → 生成版本"应挂 HIL L2 确认后另走 sysml_versions。


class SelectRequest(BaseModel):
    conversation_id: int = Field(..., description="会话 id")
    selected_name: str = Field(..., description="选定的候选方案名（须在该卡方案列中）")
    reason: str = Field("", description="选型理由（可空）")
    message_id: int = Field(0, description="对比卡所在消息 id；0=自动定位该会话最新一张含对比矩阵的助手消息")


@router.post("/api/alternatives/select")
def select_alternative(body: SelectRequest, conn=Depends(db_session), user=Depends(current_user)):
    """记录选型决策：写 `alternative_selections` + 回写卡片 `alternatives.selected`。

    校验：方案名必须在目标卡的 `alternatives.columns` 里（**不许选一个矩阵里没有的方案**）。
    幂等：同一条消息重复选 = 追加新审计行（历史可查"改选过"），卡片取最新。
    """
    import json as _json
    from datetime import datetime as _dt

    # ① 定位目标消息（显式 message_id 或自动找最新一张含对比矩阵的助手消息）
    if body.message_id:
        row = conn.execute(
            "SELECT id, card_data FROM messages WHERE id=? AND conversation_id=? AND role='assistant'",
            (body.message_id, body.conversation_id)).fetchone()
    else:
        rows = conn.execute(
            "SELECT id, card_data FROM messages WHERE conversation_id=? AND role='assistant' "
            "AND card_data LIKE '%\"alternatives\"%' ORDER BY id DESC LIMIT 5",
            (body.conversation_id,)).fetchall()
        row = None
        for r in rows:
            try:
                _cd = _json.loads(r["card_data"] or "{}")
            except Exception:
                continue
            if (_cd.get("alternatives") or {}).get("columns"):
                row = r
                break
    if not row:
        return JSONResponse({"ok": False,
                             "error": "未找到含对比矩阵的助手消息（conversation_id 或 message_id 不对）"}, 404)

    msg_id = row["id"]
    try:
        cd = _json.loads(row["card_data"] or "{}")
    except Exception:
        cd = {}
    alts = cd.get("alternatives") or {}
    names = [str(c.get("name") or "") for c in (alts.get("columns") or [])]
    if body.selected_name not in names:
        return JSONResponse({"ok": False,
                             "error": "方案「%s」不在该对比卡的候选里（可选：%s）"
                                      % (body.selected_name, "、".join(names))}, 400)

    # ② 决策快照（审计：当时看到的推荐/得分，防止事后矩阵变化导致不可对账）
    snapshot = {
        "recommended": alts.get("recommended"),
        "tie": bool(alts.get("tie")),
        "columns": [{"name": c.get("name"), "total": c.get("total"),
                     "rank": c.get("rank"), "recommended": bool(c.get("recommended")),
                     "coverage": c.get("coverage")} for c in (alts.get("columns") or [])],
    }
    by = audit_user(user) if isinstance(user, dict) else "未登录"
    now = _dt.now().strftime("%Y-%m-%d %H:%M:%S")
    selection = {"name": body.selected_name, "by": by, "at": now,
                 "reason": (body.reason or "")[:300]}

    # ③ 回写卡片（历史回放直接渲染"已选定"）+ 审计行（append-only）
    cd.setdefault("alternatives", {})["selected"] = selection
    conn.execute("UPDATE messages SET card_data=? WHERE id=?",
                 (_json.dumps(cd, ensure_ascii=False), msg_id))
    conn.execute(
        "INSERT INTO alternative_selections (conversation_id, message_id, selected_name, "
        "reason, decided_by, alternatives_snapshot) VALUES (?,?,?,?,?,?)",
        (body.conversation_id, msg_id, body.selected_name, (body.reason or "")[:300],
         by, _json.dumps(snapshot, ensure_ascii=False)))
    conn.commit()
    audit(by, "alternative_select",
          "会话#%s · 消息#%s · 选定方案「%s」" % (body.conversation_id, msg_id, body.selected_name),
          conn=conn)
    return {"ok": True, "message_id": msg_id, "selection": selection}


@router.get("/api/alternatives/selections")
def list_selections(conversation_id: int, conn=Depends(db_session)):
    """某会话的选型决策史（append-only，含改选记录）。"""
    rows = conn.execute(
        "SELECT id, message_id, selected_name, reason, decided_by, "
        "alternatives_snapshot, created_at FROM alternative_selections "
        "WHERE conversation_id=? ORDER BY id DESC LIMIT 50",
        (conversation_id,)).fetchall()
    return {"ok": True, "items": [dict(r) for r in rows]}
