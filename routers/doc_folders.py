"""文档目录树 API（2026-09-21，「基于文件的管理」P0-b）。

设计依据：`docs/文档库基于文件的管理-评估与优化方案-20260921.md` §4.2（唯一实现依据）。
契约要点：
- 目录树是**逻辑组织维度**，不参与检索过滤（决策点 B4=A：检索仍按 `domain`）；
- 删除目录**绝不级联删文档**，其中的文档回到「未归类」（`folder_id=0`）；
- 文档是全局资产：本模块所有接口**不带 branch**（该维度已随 §1.1 作废）；
- 回收站**不新增端点**：`GET /api/documents?state=deprecated` 已是回收站视图，
  进站复用既有 `POST /api/documents/{id}/deprecate`（`chunk_sync=True`），出站复用 `/restore`。
  方案 §4.2 备注已列此约束：「不要新增 /deprecate 的并行端点」——同理不新增 /trash 的并行端点。
"""
from fastapi import APIRouter, Depends

from core.deps import db_session, current_user, require_any_permission
from core.audit import audit, audit_user
from repositories.doc_folder_repo import DocFolderRepo, ROOT_FOLDER_ID, normalize_folder
from repositories.meta_repo import MetaRepo

router = APIRouter(tags=["文档目录树"])

# 与 documents 上传/改元数据同一套写权限（设计师 kb_review:modify / 知识工程师 kb_ontology:edit）
DOC_WRITE_PERMS = [("kb_review", "modify"), ("kb_ontology", "edit")]


# ═══════════════ 目录树 ═══════════════
@router.get("/api/doc-folders")
def list_doc_folders(conn=Depends(db_session)):
    """目录树 + 计数。

    返回 {folders:[{id,name,parent_id,path,sort,domain,doc_count,doc_count_all}],
          uncategorized, total_docs}。
    前端据 parent_id 组树；`doc_count`（直接）/`doc_count_all`（含子目录）都给，见仓储注释。
    """
    return DocFolderRepo(conn).list_tree()


@router.post("/api/doc-folders")
def create_doc_folder(body: dict, conn=Depends(db_session),
                      user=Depends(require_any_permission(DOC_WRITE_PERMS))):
    """新建目录。body: {name, parent_id?(0=根), domain?(目录级默认域)}"""
    r = DocFolderRepo(conn).create(
        body.get("name") or "", body.get("parent_id", ROOT_FOLDER_ID),
        domain=body.get("domain") or "", created_by=audit_user(user))
    if r.get("ok"):
        audit(audit_user(user), "doc_folder_create",
              f"新建文档目录: {body.get('name')} (parent={body.get('parent_id', 0)})", conn=conn)
    return r


@router.put("/api/doc-folders/{folder_id}")
def update_doc_folder(folder_id: int, body: dict, conn=Depends(db_session),
                      user=Depends(require_any_permission(DOC_WRITE_PERMS))):
    """重命名目录（级联刷新自身与全部后代的物化路径 `path`）。body: {name}"""
    repo = DocFolderRepo(conn)
    r = repo.rename(folder_id, body.get("name") or "")
    if r.get("ok") and not r.get("unchanged"):
        audit(audit_user(user), "doc_folder_rename",
              f"重命名文档目录 #{folder_id} → {body.get('name')}（刷新 {r.get('refreshed', 0)} 个节点）",
              conn=conn)
    return r


@router.post("/api/doc-folders/{folder_id}/move")
def move_doc_folder(folder_id: int, body: dict, conn=Depends(db_session),
                    user=Depends(require_any_permission(DOC_WRITE_PERMS))):
    """移动目录到新父级 / 在同级中排序（2026-09-27 左栏交互优化）。

    body: `{parent_id?: int（0=根）, before_id?: int, after_id?: int}` —— 可单用或组合：
      · 只给 parent_id            → 换父，追加到目标同级末尾
      · 只给 before_id/after_id   → 同级排序（父不变）
      · 都给                      → 换父并落到指定位置

    ⚠️ **不拆成 /move 与 /reorder 两个端点**：数据层是同一个动作（改 parent_id + 重写同级 sort），
    拆开只会迫使前端先判断走哪条、且两条都要各写一遍 sort 语义。审计动作名按"父是否变化"二选一。
    ⚠️ 目录移动**不动物理文件**（doc_folders 是逻辑树，源副本仍平铺在 data/uploads/），
    也不触碰 chunks/向量 —— 与 delete/rename 同一约定（见 tools/verify/verify_doc_folders.py 的 B8）。
    """
    repo = DocFolderRepo(conn)
    before = repo.get(folder_id)
    r = repo.move_folder(folder_id, body.get("parent_id", ROOT_FOLDER_ID),
                         before_id=body.get("before_id"), after_id=body.get("after_id"))
    if r.get("ok") and not r.get("unchanged"):
        name = (before or {}).get("name") or f"#{folder_id}"
        same_parent = bool(before and before["parent_id"] == r.get("parent_id"))
        if same_parent:
            audit(audit_user(user), "doc_folder_reorder",
                  f"目录「{name}」同级排序 → sort={r.get('sort')}", conn=conn)
        else:
            audit(audit_user(user), "doc_folder_move",
                  f"移动目录「{name}」→ 父 #{r.get('parent_id')}"
                  f"（重写 {r.get('rewritten', 0)} 个节点路径）", conn=conn)
    return r


@router.delete("/api/doc-folders/{folder_id}")
def delete_doc_folder(folder_id: int, conn=Depends(db_session),
                      user=Depends(require_any_permission(DOC_WRITE_PERMS))):
    """删除目录**及其子树**；其中的文档**回到「未归类」**（绝不级联删文档）。

    返回 {ok, deleted, deleted_folders, moved_to_uncategorized} —— 前端用真实数字做确认文案。
    """
    r = DocFolderRepo(conn).delete(folder_id)
    if r.get("ok"):
        audit(audit_user(user), "doc_folder_delete",
              f"删除文档目录「{r.get('deleted')}」（{r.get('deleted_folders', 0)} 个节点，"
              f"{r.get('moved_to_uncategorized', 0)} 份文档回未归类）", conn=conn)
    return r


# ═══════════════ 文档 ↔ 目录 ═══════════════
@router.post("/api/documents/{doc_id}/move")
def move_document(doc_id: int, body: dict, conn=Depends(db_session),
                  user=Depends(require_any_permission(DOC_WRITE_PERMS))):
    """把文档移动到目标目录（body: {folder_id}，0/缺省 = 未归类）。纯 DB 事务，不触碰向量。"""
    r = DocFolderRepo(conn).move_document(doc_id, normalize_folder(body.get("folder_id")))
    if r.get("ok"):
        audit(audit_user(user), "doc_move",
              f"文档 #{doc_id} 移动到目录 #{r.get('folder_id')}", conn=conn)
    return r


@router.post("/api/documents/batch")
def batch_documents(body: dict, conn=Depends(db_session),
                    user=Depends(require_any_permission(DOC_WRITE_PERMS))):
    """批量动作分发：`{action: move|trash|restore|commit, ids:[…], folder_id?}`。

    ⚠️ 这是**分发器**，不是并行实现：move 落 `DocFolderRepo.move_documents`，
    trash/restore/commit 落既有 `MetaRepo.batch_transition`（与 `/batch-deprecate`
    `/batch-commit` 同一套状态机与校验）。之所以不把三个动作各开一个端点，
    是因为前端"多选 → 选动作"只有一个入口，端点按动作再分一次只会让契约漂移。
    """
    action = (body.get("action") or "").strip()
    ids = [int(i) for i in (body.get("ids") or [])]
    if not ids:
        return {"ok": 0, "error": "未选中任何文档"}
    repo = DocFolderRepo(conn)
    if action == "move":
        r = repo.move_documents(ids, body.get("folder_id", ROOT_FOLDER_ID))
        audit(audit_user(user), "doc_batch_move",
              f"批量移动 {len(ids)} 份文档 → 目录 #{normalize_folder(body.get('folder_id'))}"
              f"（成功 {r.get('ok', 0)}）", conn=conn)
        return r
    meta = MetaRepo(conn)
    if action == "trash":
        # 与 /batch-deprecate 同语义：进回收站 = 下线（复用 lifecycle_status 单轴，决策点 B1=A）
        r = meta.batch_transition(ids, "deprecated", audit_user(user),
                                  reason=body.get("reason") or "批量移入回收站",
                                  allow_from=None)
    elif action == "restore":
        r = meta.batch_transition(ids, "committed", audit_user(user),
                                  reason=body.get("reason") or "批量移出回收站",
                                  allow_from=["deprecated"])
    elif action == "commit":
        # 与 /batch-commit 同语义：只允许 stored → committed
        r = meta.batch_transition(ids, "committed", audit_user(user),
                                  reason=body.get("reason") or "批量正式入库",
                                  allow_from=["stored"])
    else:
        return {"ok": 0, "error": f"未知动作：{action or '(空)'}（可用 move|trash|restore|commit）"}
    audit(audit_user(user), f"doc_batch_{action}",
          f"批量{action} {len(ids)} 份文档（成功 {r.get('ok', 0)} / 跳过 {r.get('skipped', 0)}）",
          conn=conn)
    return r
