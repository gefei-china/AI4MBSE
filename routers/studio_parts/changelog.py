# -*- coding: utf-8 -*-
"""AI 设计工坊路由分片：资产变更审计读取（P0-2，asset_change_logs）。

写侧在 repositories 的 update_*（core/asset_audit.audited_update 落地）；
本文件只读——把「上次改了什么、上一版长什么样」暴露给前端与自进化评估。
"""
from routers.studio_parts.shared import *


@router.get("/api/studio/asset-changelog")
def asset_changelog(asset_type: str = "", asset_id: int = 0, field: str = "",
                    limit: int = 100, conn=Depends(db_session)):
    """资产变更历史（字段级 diff）。asset_type+asset_id 定位单资产；都不传=最近全局。

    老库（迁移未跑）无 asset_change_logs 表 ⇒ 返空列表而非 500（与 monitor 域
    _table_cols 防御同哲学）。
    """
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='asset_change_logs'").fetchone()
    if not exists:
        return {"ok": True, "items": [], "total": 0, "note": "审计表未建立（迁移未执行）"}
    conds, args = [], []
    if asset_type:
        conds.append("asset_type=?")
        args.append(asset_type)
    if asset_id:
        conds.append("asset_id=?")
        args.append(int(asset_id))
    if field:
        conds.append("field=?")
        args.append(field)
    where = (" WHERE " + " AND ".join(conds)) if conds else ""
    total = conn.execute(f"SELECT COUNT(*) FROM asset_change_logs{where}", args).fetchone()[0]
    rows = [dict(r) for r in conn.execute(
        f"SELECT * FROM asset_change_logs{where} ORDER BY id DESC LIMIT ?",
        (*args, int(limit))).fetchall()]
    return {"ok": True, "items": rows, "total": total}
