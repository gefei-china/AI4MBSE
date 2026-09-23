"""分支管理域：/api/branches, /api/branches/merge-requests"""
import json
import re

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from core.deps import db_session, current_user, require_permission
from core import branch_rules
from repositories.branch_repo import BranchRepo
from repositories.commit_repo import CommitRepo
from core.audit import audit

router = APIRouter(tags=["分支管理"])

# 分支类型全集：dev（主开发，系统预置唯一）/ release（发布，系统预置唯一）/ personal（个人分支）/ local（本地分支）
# 可手动创建的类型仅 personal 与 local（见 create_branch 的校验）：personal 须从 dev/release 拉基线；
# local 用于离线/实验性操作，不 fork 基线、不可作为合并源。dev/release 为系统预置分支，不可手动创建。
VALID_TYPES = {"dev", "release", "personal", "local"}
DEV_BRANCH = "dev"
RELEASE_BRANCH = "release"

# 分支保护规则（P0-2）：单一真源在 core/branch_rules.py（类型默认 → 内置名称兜底 →
# 分支级 protection_rules 覆盖）。此处不再维护硬编码集合 —— 内置 release/dev/personal 的
# 「不可删 / 不可改名」语义由 branch_rules.PROTECTED_NAMES 兜底表达，且支持分支级规则显式解锁。


def _actor(user) -> str:
    """审计操作人：优先取当前登录用户显示名；匿名请求兜底保留原默认名「王工」。"""
    return (user or {}).get("display_name") or "王工"


def _check_writable_branch(branch) -> str | None:
    """分支直写校验（P0-2 改读保护规则 writable，规则见 core/branch_rules.py）。

    branch 可传分支行 dict（能读到分支级规则，推荐）或分支名 str（按其类型默认判定）。
    """
    if branch_rules.effective_rules(branch).get("writable", True):
        return None
    nm = branch if isinstance(branch, str) else (branch.get("name") or "")
    if branch_rules.is_release_family(nm):
        return "release 分支为只读发布分支，不可直接编辑；请切换到 dev 编辑后通过合并更新"
    return f"{nm} 分支受保护（只读），不可直接编辑；请切换到可写分支编辑后通过合并更新"


def _validate_branch_name(name: str) -> str | None:
    """分支名校验：非空、长度、允许字符。"""
    if not name or not name.strip():
        return "分支名必填"
    name = name.strip()
    if len(name) > 60:
        return "分支名过长（≤60 字符）"
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9_\-/\.]+$", name):
        return "分支名只能包含字母/数字/_/-/./，且以字母或数字开头"
    return None


# ═══════════ 分支 CRUD（注意：{name:path} 路由必须在 merge-requests 之后注册）═══════════

@router.get("/api/branches")
def list_branches(conn=Depends(db_session)):
    repo = BranchRepo(conn)
    branches = repo.list_branches()
    for b in branches:
        b["entity_count"] = repo.count_entities_by_branch(b["name"])
        # P1-2 ahead/behind：dev 相对 release、个人分支相对 parent_branch（GitHub 分支列表语义）
        baseline = None
        if b["branch_type"] == "dev":
            baseline = RELEASE_BRANCH
        elif b["branch_type"] == "personal" and b.get("parent_branch"):
            baseline = b["parent_branch"]
        if baseline and repo.get_branch(baseline):
            b.update(repo.ahead_behind(b["name"], baseline))
        else:
            b["ahead"], b["behind"] = 0, 0
    return branches


@router.post("/api/branches")
def create_branch(body: dict, conn=Depends(db_session), user=Depends(current_user)):
    """创建分支：dev/release 为系统预置唯一分支，不可手动创建；
    个人分支须从 dev 或 release 拉取基线（创建时 fork 基线实体/关系）；
    本地分支（local）用于离线/实验性操作，不 fork 基线、不可作为合并源。"""
    # P0-4：建分支权限门（branch_dev:create，设计师/知识工程师矩阵已含 → 零行为回归）
    require_permission("branch_dev", "create")(user=user)
    repo = BranchRepo(conn)
    name = (body.get("name") or "").strip()
    err = _validate_branch_name(name)
    if err:
        return JSONResponse({"error": err}, 400)
    branch_type = body.get("branch_type", "personal")
    if branch_type not in ("personal", "local"):
        return JSONResponse({"error": "只能创建个人分支或本地分支（dev/release 为系统预置分支）；"
                                       "个人分支从 dev 或 release 拉取基线，本地分支用于离线/实验"}, 400)
    parent = (body.get("parent_branch") or "").strip()
    if branch_type == "personal":
        if parent not in (DEV_BRANCH, RELEASE_BRANCH):
            return JSONResponse({"error": "个人分支须从 dev 或 release 拉取基线（parent_branch）"}, 400)
    else:
        # local 分支不需要父分支（离线/实验），有 parent 也忽略（不 fork 基线）
        parent = ""
    if repo.get_branch(name):
        return JSONResponse({"error": f"分支 {name} 已存在"}, 400)
    repo.create_branch(name, branch_type, parent, body.get("description", ""))
    tag = "(local)" if branch_type == "local" else f"(personal, 基线={parent})"
    audit(_actor(user), "branch_create", f"创建分支: {name} {tag}", conn=conn)
    return {"ok": True, "name": name}


# ═══════════ 个人分支从基线同步（git merge 语义）═══════════
@router.post("/api/branches/{target:path}/sync")
def sync_branch(target: str, body: dict, conn=Depends(db_session), user=Depends(current_user)):
    """personal 分支从基线分支（dev/release）同步图谱数据（git merge 语义）。

    body: { source: 基线分支名, adopt_modified?: [entity_id, ...] 接受源版覆盖的冲突实体 }
    规则：仅 personal 分支可执行；仅能从 dev/release 基线同步；新增自动采纳、冲突默认保留 personal。
    """
    repo = BranchRepo(conn)
    source = (body.get("source") or "").strip()
    if not source:
        return JSONResponse({"error": "缺少 source（要同步的基线分支）"}, 400)
    tgt_b = repo.get_branch(target)
    src_b = repo.get_branch(source)
    if not tgt_b:
        return JSONResponse({"error": f"分支 {target} 不存在"}, 404)
    if not src_b:
        return JSONResponse({"error": f"基线分支 {source} 不存在"}, 404)
    if tgt_b["branch_type"] != "personal":
        return JSONResponse({"error": "仅个人(personal)分支可执行「从基线同步」"}, 400)
    if src_b["branch_type"] not in (DEV_BRANCH, RELEASE_BRANCH):
        return JSONResponse({"error": "仅可从 dev/release 基线分支同步"}, 400)
    if target == source:
        return JSONResponse({"error": "源分支与目标分支相同，无需同步"}, 400)
    err = _check_writable_branch(tgt_b)   # P0-2：按分支保护规则（writable）判定
    if err:
        return JSONResponse({"error": err}, 400)
    adopt = body.get("adopt_modified") or []
    if not isinstance(adopt, list):
        adopt = [adopt]
    result = repo.sync_from_branch(target, source, adopt)
    audit(_actor(user), "branch_sync", f"分支 {target} 从 {source} 同步", conn=conn)
    return result


# ═══════════ 合并请求 ═══════════

@router.get("/api/branches/merge-requests")
def list_merge_requests(branch: str = "", conn=Depends(db_session)):
    """合并请求列表。branch 非空 → 只返回与该分支相关的 MR（source_branch 或 target_branch 命中，
    2026-09-14 图谱工作区分支视角）；缺省返回全量（管理统计等旧调用方兼容）。"""
    return BranchRepo(conn).list_merge_requests(branch=branch.strip())


@router.get("/api/branches/diff")
def branch_diff(base: str = "", head: str = "", mode: str = "full", conn=Depends(db_session)):
    """分支差异（只读，无副作用）：实体 added/modified/removed + 关系 added/removed。

    base 缺省 → 自动选最新 release 分支（发布基线）；head 缺省 → dev。
    mode=merge-base（P1-2）：三点式，只返回 head 自上次 merge 以来的增量实体差异。
    """
    repo = BranchRepo(conn)
    if not base:
        rel = repo.rows("SELECT name FROM branches WHERE branch_type='release' ORDER BY id DESC LIMIT 1")
        if not rel:
            return JSONResponse({"error": "无 release 分支可作为基线，请显式指定 base"}, 400)
        base = rel[0]["name"]
    if not head:
        head = DEV_BRANCH
    if not repo.get_branch(base):
        return JSONResponse({"error": f"基线分支 {base} 不存在"}, 400)
    if not repo.get_branch(head):
        return JSONResponse({"error": f"对比分支 {head} 不存在"}, 400)
    if base == head:
        return JSONResponse({"error": "基线分支与对比分支不能相同"}, 400)
    if mode not in ("full", "merge-base"):
        return JSONResponse({"error": "mode 必须为 full 或 merge-base"}, 400)
    return {"base": base, "head": head, **repo.diff_branches(base, head, mode)}


@router.get("/api/branches/integrity-report")
def integrity_report(conn=Depends(db_session)):
    """数据体检（只读巡检）：关系引用完整性——悬空/跨分支死行扫描 + 废弃实体引用计数。"""
    return BranchRepo(conn).integrity_report()


@router.get("/api/branches/commits/verify")
def verify_commits(limit: int = 0, conn=Depends(db_session)):
    """提交内容哈希自检（P0-3，对标 G9 commit SHA）：全量重算 content_hash 与库值比对。

    只读。`limit>0` 只校验最近 N 条。`missing` = 未回填的历史行；`mismatched` = 疑似被改库篡改。
    与 `/api/branches/{name}/history` 不冲突（第 3 段字面量分别为 verify / history）。
    """
    return CommitRepo(conn).verify_commits(limit=limit)


@router.get("/api/branches/merge-requests/{mr_id}/conflicts")
def merge_conflicts(mr_id: int, conn=Depends(db_session)):
    """合并请求冲突解决状态（每个冲突字段：双方值 + 是否已解决）。"""
    repo = BranchRepo(conn)
    mr = repo.get_merge_request(mr_id)
    if not mr:
        return JSONResponse({"error": f"合并请求 #{mr_id} 不存在"}, 404)
    return {"mr_id": mr_id, "conflicts": repo.get_conflict_status(mr),
            "unresolved": len(repo.pending_conflicts(mr))}


@router.post("/api/branches/merge-requests/{mr_id}/resolve-conflict")
def resolve_conflict(mr_id: int, body: dict, conn=Depends(db_session), user=Depends(current_user)):
    """解决某个冲突字段：pick=source 以源分支为准 | target 以目标分支为准 | manual 手动值；
    delete_modify 冲突用 keep_delete（保留删除）/ keep_modify（保留修改）。"""
    # P0-4：解决冲突权限门（branch_dev:merge_request，矩阵已含 → 零行为回归）
    require_permission("branch_dev", "merge_request")(user=user)
    repo = BranchRepo(conn)
    entity_id = (body.get("entity_id") or "").strip()
    field = (body.get("field") or "").strip()
    pick = body.get("pick")
    if not entity_id or not field:
        return JSONResponse({"error": "entity_id 与 field 必填"}, 400)
    mr = repo.get_merge_request(mr_id)
    if not mr:
        return JSONResponse({"error": f"合并请求 #{mr_id} 不存在"}, 404)
    # 定位冲突类型（delete_modify 支持 keep_delete/keep_modify，property 支持 source/target/manual）
    conflict = next((c for c in repo.get_conflict_status(mr)
                     if c.get("entity_id") == entity_id and c.get("field") == field), None)
    if not conflict:
        return JSONResponse({"error": f"实体 {entity_id} 的字段 {field} 不在冲突清单中"}, 400)
    is_delete_modify = conflict.get("conflict_type") == "delete_modify"
    valid_picks = ("keep_delete", "keep_modify") if is_delete_modify else ("source", "target", "manual")
    if pick not in valid_picks:
        return JSONResponse({"error": f"该冲突类型下 pick 必须为 {'/'.join(valid_picks)}"}, 400)
    if pick == "manual":
        v = body.get("value")
        if v is None or (isinstance(v, str) and not v.strip()):
            return JSONResponse({"error": "手动合并需填写字段值"}, 400)
    result = repo.resolve_conflict(mr_id, entity_id, field, pick, str(body.get("value", "")),
                                   actor=(user or {}).get("display_name") or (user or {}).get("username") or "王工")
    if not result.get("ok"):
        return JSONResponse({"error": result["error"]}, 400)
    audit(_actor(user), "merge_conflict_resolve",
          f"合并请求#{mr_id} 冲突解决: {entity_id}.{field} → {pick}", conn=conn)
    return {"ok": True, "entity_id": entity_id, "field": field, "pick": pick}


@router.post("/api/branches/merge-requests")
def create_merge_request(body: dict, conn=Depends(db_session), user=Depends(current_user)):
    # P0-4：发起合并请求权限门（branch_dev:merge_request，矩阵已含 → 零行为回归）
    require_permission("branch_dev", "merge_request")(user=user)
    repo = BranchRepo(conn)
    src, tgt = (body.get("source_branch") or "").strip(), (body.get("target_branch") or "").strip()
    if not src or not tgt:
        return JSONResponse({"error": "源分支与目标分支必填"}, 400)
    if src == tgt:
        return JSONResponse({"error": "源分支与目标分支不能相同"}, 400)
    if not repo.get_branch(src):
        return JSONResponse({"error": f"源分支 {src} 不存在"}, 400)
    if not repo.get_branch(tgt):
        return JSONResponse({"error": f"目标分支 {tgt} 不存在"}, 400)
    # ── 合并流向规则（git 风格分支模型）──
    # 个人分支 → dev（合并回主开发）；dev → release（发布）；release 只读不可作为合并源
    src_b = repo.get_branch(src)
    if src_b and src_b.get("branch_type") == "local":
        return JSONResponse({"error": "本地分支不可作为合并源（离线/实验，请先在 dev 上操作）"}, 400)
    if tgt not in (DEV_BRANCH, RELEASE_BRANCH):
        return JSONResponse({"error": f"合并目标分支只能是 {DEV_BRANCH} 或 {RELEASE_BRANCH}"}, 400)
    if src == RELEASE_BRANCH:
        return JSONResponse({"error": "release 分支为只读发布分支，不可作为合并源"}, 400)
    if tgt == RELEASE_BRANCH and src != DEV_BRANCH:
        return JSONResponse({"error": "只有 dev 可以合并到 release（发布）；个人分支请先合并回 dev"}, 400)
    # 冲突检测（P0-2 完整性：属性 key 并集 + delete_modify，含 conflict_type）
    src_entities = {r["id"]: dict(r) for r in repo.get_entities_by_branch(src)
                    if r["status"] != "deprecated"}
    tgt_entities = {r["id"]: dict(r) for r in repo.get_entities_by_branch(tgt)
                    if r["status"] != "deprecated"}
    conflicts = repo._detect_conflicts(src, tgt)
    detail = json.dumps({
        "src_entities": len(src_entities),
        "tgt_entities": len(tgt_entities),
        "conflicts": conflicts,
    }, ensure_ascii=False)
    result = repo.create_merge_request(
        src, tgt, json.dumps(conflicts, ensure_ascii=False), detail,
        release_version=body.get("release_version", "") or "",
        actor=_actor(user), title=body.get("title", "") or "",
        draft=bool(body.get("draft")))
    if not result.get("ok"):
        # P0-1 发布门禁拦截时携带 blocked_pending_review（前端可提示未评审实体清单）
        return JSONResponse(result, 400)
    audit(_actor(user), "merge_request", f"合并请求: {src} → {tgt}, 冲突:{len(conflicts)}", conn=conn)
    return {"ok": True, "id": result["id"], "conflicts": len(conflicts), "detail": conflicts,
            "src_entities": len(src_entities), "tgt_entities": len(tgt_entities)}


@router.delete("/api/branches/merge-requests/{mr_id}")
def delete_merge_request(mr_id: int, conn=Depends(db_session), user=Depends(current_user)):
    result = BranchRepo(conn).delete_merge_request(mr_id)
    if not result.get("ok"):
        return JSONResponse({"error": result["error"]}, 400)
    audit(_actor(user), "merge_delete", f"删除合并请求#{mr_id}", conn=conn)
    return result


@router.post("/api/branches/merge-requests/{mr_id}/resolve")
def resolve_merge(mr_id: int, body: dict, conn=Depends(db_session), user=Depends(current_user)):
    action = body.get("action", "approve")  # approve | reject
    if action not in ("approve", "reject"):
        return JSONResponse({"error": "action 必须为 approve/reject"}, 400)
    # 合并审批硬门禁：仅 approve（通过）需要 branch_release:review_merge 权限；
    # reject 与其它状态不强制（匿名请求由依赖内部放行，向后兼容测试脚本）
    if action == "approve":
        require_permission("branch_release", "review_merge")(user=user)
    repo = BranchRepo(conn)
    mr = repo.get_merge_request(mr_id)
    if not mr:
        return JSONResponse({"error": f"合并请求 #{mr_id} 不存在"}, 404)
    # P1-1 状态机：approve 仅 open 可；reject 允许 open/draft（草稿也可直接关闭）
    if action == "approve" and mr["status"] != "open":
        return JSONResponse({"error": f"仅待评审（open）合并请求可通过（当前状态：{mr['status']}）"}, 400)
    if action == "reject" and mr["status"] not in ("draft", "open"):
        return JSONResponse({"error": f"该合并请求已处理（{mr['status']}）"}, 400)
    review_note = (body.get("review_note") or "").strip()
    if action == "reject" and len(review_note) < 5:
        return JSONResponse({"error": "驳回必须填写意见（≥5 字，用于追溯）"}, 400)
    if action == "approve":
        pending = repo.pending_conflicts(mr)
        if pending:
            fields = ", ".join(f"{p['entity_name']}.{p['field']}" for p in pending[:3])
            return JSONResponse(
                {"error": f"还有 {len(pending)} 处冲突未解决（{fields}{'…' if len(pending) > 3 else ''}），请先解决后再通过"},
                400)
    result = repo.resolve_merge(mr_id, "merged" if action == "approve" else "closed",
                                actor=_actor(user), review_note=review_note)
    if not result.get("ok"):
        if result.get("code") == "conflict_changed":
            # P0-1 冲突清单已变化：前端弹引导对话框（列出新增/消失冲突）
            return JSONResponse(result, 409)
        # P0-1 发布门禁拦截（未评审数据禁止进入 release）：留痕 + 返回拦截原因
        audit(_actor(user), "merge_gate_rejected",
              f"合并请求#{mr_id} 发布门禁拦截: {result.get('error', '')}", conn=conn)
        return JSONResponse(result, 400)
    audit(_actor(user), "merge_resolve", f"合并请求#{mr_id}: {action}", conn=conn)
    # 评审留痕：approve/reject 动作 + 意见写入时间线
    repo.add_mr_comment(mr_id, author=_actor(user), action=action,
                        comment=review_note or ("通过合并" if action == "approve" else ""))
    return {"ok": True, **result}


@router.get("/api/branches/merge-requests/{mr_id}/preview-merge")
def preview_merge(mr_id: int, conn=Depends(db_session)):
    """合并结果预览（dry-run，不落库）：基于当前源/目标分支差异计算合并后将发生的变更统计。"""
    repo = BranchRepo(conn)
    mr = repo.get_merge_request(mr_id)
    if not mr:
        return JSONResponse({"error": f"合并请求 #{mr_id} 不存在"}, 404)
    if mr["status"] == "merged":
        return JSONResponse({"error": "该合并请求已合并"}, 400)
    # base=目标分支（合并目的地），head=源分支（被带入的变更）
    diff = repo.diff_branches(mr["target_branch"], mr["source_branch"], mode="full")
    s = diff.get("summary", {})
    conflicts = repo.pending_conflicts(mr)
    verb = "复制" if mr["target_branch"] == "release" else "迁入"
    return {"ok": True, "source": mr["source_branch"], "target": mr["target_branch"],
            "verb": verb,
            "summary": {"entities_add": s.get("ent_added", 0),
                        "entities_update": s.get("ent_modified", 0),
                        "entities_remove": s.get("ent_removed", 0),
                        "relations_add": s.get("rel_added", 0),
                        "relations_update": s.get("rel_modified", 0),
                        "relations_remove": s.get("rel_removed", 0)},
            "conflicts_total": len(conflicts),
            "conflicts_unresolved": len(conflicts)}


@router.get("/api/branches/merge-requests/{mr_id}/comments")
def list_mr_comments(mr_id: int, conn=Depends(db_session)):
    """MR 评审意见时间线（approve/reject/comment/rollback 全量留痕）。"""
    if not BranchRepo(conn).get_merge_request(mr_id):
        return JSONResponse({"error": f"合并请求 #{mr_id} 不存在"}, 404)
    return BranchRepo(conn).list_mr_comments(mr_id)


@router.post("/api/branches/merge-requests/{mr_id}/comments")
def add_mr_comment(mr_id: int, body: dict, conn=Depends(db_session), user=Depends(current_user)):
    """给合并请求追加评审意见（不改状态，纯评论）。"""
    text = (body.get("comment") or "").strip()
    if not text:
        return JSONResponse({"error": "comment 不能为空"}, 400)
    repo = BranchRepo(conn)
    if not repo.get_merge_request(mr_id):
        return JSONResponse({"error": f"合并请求 #{mr_id} 不存在"}, 404)
    row = repo.add_mr_comment(mr_id, author=_actor(user), action="comment", comment=text)
    audit(_actor(user), "mr_comment", f"合并请求#{mr_id} 追加评审意见", conn=conn)
    return {"ok": True, "comment": row}


@router.post("/api/branches/merge-requests/{mr_id}/open")
def open_merge_request(mr_id: int, conn=Depends(db_session), user=Depends(current_user)):
    """P1-1：草稿转正式评审（draft → open）。"""
    result = BranchRepo(conn).open_merge_request(mr_id, actor=_actor(user))
    if not result.get("ok"):
        return JSONResponse({"error": result["error"]}, 400)
    audit(_actor(user), "merge_open", f"合并请求#{mr_id} 草稿转正式评审", conn=conn)
    BranchRepo(conn).add_mr_comment(mr_id, author=_actor(user), action="open", comment="草稿转正式评审")
    return {"ok": True, **result}


@router.post("/api/branches/merge-requests/{mr_id}/reopen")
def reopen_merge_request(mr_id: int, conn=Depends(db_session), user=Depends(current_user)):
    """P1-1：重新打开已关闭的合并请求（closed → open）。"""
    result = BranchRepo(conn).reopen_merge_request(mr_id, actor=_actor(user))
    if not result.get("ok"):
        return JSONResponse({"error": result["error"]}, 400)
    audit(_actor(user), "merge_reopen", f"重新打开合并请求#{mr_id}", conn=conn)
    BranchRepo(conn).add_mr_comment(mr_id, author=_actor(user), action="reopen", comment="重新打开合并请求")
    return {"ok": True, **result}


@router.post("/api/branches/merge-requests/{mr_id}/rollback")
def rollback_merge(mr_id: int, conn=Depends(db_session), user=Depends(current_user)):
    """FR-KG-16：回滚已合并的发布合并请求——release 分支还原到合并前快照。

    权限对齐 resolve_merge：需 branch_release:review_merge（合并审批同权）。
    """
    require_permission("branch_release", "review_merge")(user=user)
    result = BranchRepo(conn).rollback_merge(mr_id)
    if not result.get("ok"):
        return JSONResponse({"error": result["error"]}, 400)
    audit(_actor(user), "merge_rollback",
          f"回滚合并请求#{mr_id}: 还原 release {result.get('restored_entities', 0)} 实体 / "
          f"{result.get('restored_relations', 0)} 关系", conn=conn)
    BranchRepo(conn).add_mr_comment(
        mr_id, author=_actor(user), action="rollback",
        comment=f"回滚发布：还原 {result.get('restored_entities', 0)} 实体 / "
                f"{result.get('restored_relations', 0)} 关系")
    return {"ok": True, "restored_entities": result["restored_entities"],
            "restored_relations": result["restored_relations"]}


@router.get("/api/branches/{name}/history")
def branch_history(name: str, conn=Depends(db_session)):
    """分支版本管理：分支提交时间线（id DESC，含 kind/message/changes_count）。"""
    repo = BranchRepo(conn)
    if not repo.get_branch(name):
        return JSONResponse({"error": f"分支 {name} 不存在"}, 404)
    from repositories.commit_repo import CommitRepo
    return CommitRepo(conn).get_branch_history(name)

# ═══════════ 分支保护规则（P0-2；必须早于下方 {name:path} 通配路由注册，否则被其吞掉）═══════════

@router.put("/api/branches/{name:path}/protection")
def update_branch_protection(name: str, body: dict, conn=Depends(db_session),
                             user=Depends(current_user)):
    """设置分支保护规则（P0-2）。

    body: {"rules": {"writable"?: bool, "deletable"?: bool, "renamable"?: bool,
                     "required_reviews"?: int, "allow_direct_push"?: bool}}
    - 仅接受规则 schema 白名单内的键与类型；未传的键保持原值（与既有分支级规则合并）。
    - 权限：admin:ops_manage（保护规则属安全配置，与建/删分支的日常权限分离）。
    - 审计：写 branch_protection_update 事件（含变更前后）。
    - 返回合并后的**生效规则**（含类型默认与内置兜底）。
    """
    require_permission("admin", "ops_manage")(user=user)
    repo = BranchRepo(conn)
    b = repo.get_branch(name)
    if not b:
        return JSONResponse({"error": f"分支 {name} 不存在"}, 404)
    raw = body.get("rules")
    if not isinstance(raw, dict):
        return JSONResponse({"error": "缺少 rules 对象"}, 400)
    patch = {}
    for k in branch_rules.RULE_KEYS:
        if k not in raw:
            continue
        v = raw[k]
        if k == "required_reviews":
            if isinstance(v, bool) or not isinstance(v, int) or v < 0:
                return JSONResponse({"error": "required_reviews 必须为非负整数"}, 400)
        elif not isinstance(v, bool):
            return JSONResponse({"error": f"{k} 必须为布尔值"}, 400)
        patch[k] = v
    if not patch:
        return JSONResponse(
            {"error": "rules 中无可识别规则键（允许：%s）" % "、".join(branch_rules.RULE_KEYS)}, 400)
    before = branch_rules.rules_for(conn, name)
    cur = branch_rules.parse_rules(b.get("protection_rules"))
    cur.update(patch)
    repo.set_protection_rules(name, json.dumps(cur, ensure_ascii=False))
    audit(_actor(user), "branch_protection_update",
          f"更新分支保护规则: {name} ← {json.dumps(patch, ensure_ascii=False)}",
          conn=conn, branch=name)
    after = branch_rules.rules_for(conn, name)
    return {"ok": True, "name": name, "rules": after, "raw": cur,
            "changed": {k: [before.get(k), after.get(k)] for k in patch}}


# ═══════════ 分支 编辑/删除（{name:path} 支持含 / 的分支名，必须最后注册）═══════════

@router.put("/api/branches/{name:path}")
def update_branch(name: str, body: dict, conn=Depends(db_session), user=Depends(current_user)):
    repo = BranchRepo(conn)
    b = repo.get_branch(name)
    if not b:
        return JSONResponse({"error": f"分支 {name} 不存在"}, 404)
    # P0-2：改名受保护规则 renamable 约束（内置分支兜底为 False，既有行为不变）
    err_ren = branch_rules.check_renamable(conn, name)
    if err_ren:
        return JSONResponse({"error": err_ren}, 400)
    new_name = (body.get("name") or name).strip()
    err = _validate_branch_name(new_name)
    if err:
        return JSONResponse({"error": err}, 400)
    if new_name != name and repo.get_branch(new_name):
        return JSONResponse({"error": f"分支 {new_name} 已存在"}, 400)
    # 分支类型不可修改（个人分支类型固定），避免类型漂移破坏合并流向
    branch_type = b.get("branch_type", "personal")
    parent = body.get("parent_branch", b.get("parent_branch", ""))
    if parent and parent != new_name and not repo.get_branch(parent):
        return JSONResponse({"error": f"父分支 {parent} 不存在"}, 400)
    status = body.get("status", b.get("status", "active"))
    if status not in ("active", "archived"):
        return JSONResponse({"error": "状态必须为 active/archived"}, 400)
    repo.update_branch(name, new_name, branch_type, parent, body.get("description", ""), status)
    audit(_actor(user), "branch_update",
          f"编辑分支: {name} → {new_name} ({branch_type}, {status})", conn=conn)
    return {"ok": True, "name": new_name}


@router.delete("/api/branches/{name:path}")
def delete_branch(name: str, conn=Depends(db_session), user=Depends(current_user)):
    # P0-4：删分支权限门（branch_dev:delete）。delete 为新增 op，已同步为设计师/知识工程师
    # 的角色矩阵补键（保持"原本无门"时的可达性不回归）；release/dev/personal 仍由下方 PROTECTED 兜底拒绝。
    require_permission("branch_dev", "delete")(user=user)
    repo = BranchRepo(conn)
    # P0-2：删除受保护规则 deletable 约束（内置分支兜底为 False，既有行为不变）
    err_del = branch_rules.check_deletable(conn, name)
    if err_del:
        return JSONResponse({"error": err_del}, 400)
    result = repo.delete_branch(name)
    if not result.get("ok"):
        return JSONResponse({"error": result["error"]}, 400)
    audit(_actor(user), "branch_delete", f"删除分支: {name}", conn=conn)
    return result
