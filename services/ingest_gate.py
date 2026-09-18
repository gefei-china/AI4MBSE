"""入库发布门禁（P0）：AI 建模确认后走「合并请求待审 / 自动发布」。

复用既有分支合并设施（BranchRepo.create_merge_request / resolve_merge），
在 confirm 之后对批次生成合并请求：
- draft_flow 配置关闭 → 不干预（保全现状，数据停靠在个人分支）
- draft_flow 开启 → 先建合并请求；来源命中 review_source_types（默认 ai_generated）则
  留待人工审批（待审），否则自动 approve 发布到 dev。

设计定位：增量、门控，不动 confirm 既有写入逻辑；发布粒度与既有「personal→dev」
分支合并模型一致。审批策略表（approval_policy）与置信度分级属后续（P2）演进。
"""
import logging

from core import config
from repositories.branch_repo import BranchRepo

logger = logging.getLogger(__name__)

INGEST_DRAFT_BRANCH = "personal"   # confirm 写入所在分支（KG 个人工作分支）
TARGET_BRANCH = "dev"              # 权威目标分支


def evaluate(source_type: str = "", confidence: float = 0.0) -> dict:
    """按来源/置信度评估是否需强制待审。返回 {require_review}。"""
    raw = config.get("ingest", "review_source_types", ["ai_generated"])
    if isinstance(raw, str):
        reviewed = [s.strip() for s in raw.split(",") if s.strip()]
    else:
        reviewed = list(raw or [])
    return {"require_review": (source_type or "").casefold() in {s.casefold() for s in reviewed},
            "review_source_types": reviewed}


def submit_ingest_batch(conn, batch_id: str = "", source_type: str = "ai_generated",
                        operator: str = "知识工程师", detail: str = "") -> dict:
    """确认后对批次生成发布合并请求（门控）。返回 gate 结果。"""
    if not config.as_bool("ingest", "draft_flow", False):
        return {"enabled": False}
    br = BranchRepo(conn)
    ev = evaluate(source_type)
    r = br.create_merge_request(INGEST_DRAFT_BRANCH, TARGET_BRANCH, conflicts="",
                                detail=detail or f"入库批次 {batch_id or '(未命名)'} 发布待审",
                                release_version="")
    mr_id = r.get("id")
    if not r.get("ok"):
        # 已存在未处理 personal→dev：说明上次数据仍待处理，直接标记 waiting
        pending = conn.execute(
            "SELECT id FROM merge_requests WHERE source_branch=? AND target_branch=? AND status IN ('draft','open')",
            (INGEST_DRAFT_BRANCH, TARGET_BRANCH)).fetchone()
        return {"enabled": True, "mr_id": pending["id"] if pending else None,
                "status": "waiting", "require_review": True, "detail": r.get("error", "")}
    if ev["require_review"]:
        return {"enabled": True, "mr_id": mr_id, "status": "pending",
                "require_review": True, "detail": r.get("ok", "")}
    res = br.resolve_merge(mr_id, "merged")
    return {"enabled": True, "mr_id": mr_id,
            "status": "merged" if res.get("ok") else "open",
            "require_review": False, "resolve": res}


def status() -> dict:
    """门禁状态（供前端展示）。"""
    return {"enabled": config.as_bool("ingest", "draft_flow", False),
            "review_source_types": config.get("ingest", "review_source_types", ["ai_generated"]),
            "source_branch": INGEST_DRAFT_BRANCH,
            "target_branch": TARGET_BRANCH}