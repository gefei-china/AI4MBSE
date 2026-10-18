# -*- coding: utf-8 -*-
"""资产变更审计（P0-2，2026-10-18《自进化 AI 平台调研评估》整改第二项）。

**问题**：agents / prompts / skills / agent_flows 四类资产的更新路径此前只有一条
audit_logs 文字流水（「更新Agent#3」）——**没有字段级 diff、没有版本跃迁**：
谁把 system_prompt 从 A 改成 B、上一版长什么样，一律不可考。自进化若要「评估每次
变更的效果」，首先得能回答「上次改了什么」；没有这个基线，任何「改进」都无法归因。

**本模块提供的单一通用更新路径**（四个 repo 的 update_* 全部收敛到这里）：

    audited_update(conn_or_repo, asset_type, asset_id, new_values, table=..., ...)

  读现值 → diff →（无 diff 则什么都不做）→ 递升版本 → 应用 UPDATE → 逐字段写
  asset_change_logs。全部语句走同一个连接的同一个事务：审计插入失败 ⇒ 异常上抛
  ⇒ 调用方事务回滚 ⇒ UPDATE 一起回滚（不存在「改了但没审计」的半成品）。

设计纪律：
  · **无 diff 不动手**：字段值与现值一致时连 UPDATE 都不执行（不白碰
    updated_at，也不写审计行）——「打开编辑框原样保存」不是一次变更；
  · **diff 驱动**：只把真正变化的字段写进 SET / 审计行，未提供的键原样保留
    （覆盖旧 update_* 的 COALESCE 语义：None/缺键 = 保留既有值）；
  · **版本双轨**：调用方显式传 version ⇒ 尊重调用方；未传且
    `asset_audit.auto_bump_version` ⇒ 自动递升（v1→v2、v1.0→v1.1、v1.0.0→v1.0.1）；
  · **配置可控**：`asset_audit.enabled=False` 时更新照常、只是不写审计行
    （降级通道，不是功能开关的隐式默认）；
  · **值截断**：超长字段（system_prompt/content/nodes...）按
    `asset_audit.max_value_chars` 截断入审计库，防审计表被几十 KB 的 prompt 撑爆；
  · **通用**：不认识任何具体资产类型——表名/列名/版本列全是参数，新资产种类
    接入只需在调用处传参，本文件零改动（与 core/lifecycle.py 同哲学）。
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Optional

__all__ = ["bump_version", "diff_values", "audited_update", "get_audit_settings"]

# 版本号形态：v?<int>[.<int>[.<int>...]]（v1 / v1.0 / v1.0.0 / 1.2.3 都认）
_VER_RE = re.compile(r"^(v?)(\d+(?:\.\d+)*)$")


def get_audit_settings() -> dict:
    """从 core.config 读取 asset_audit 组（缺键回退默认值；门禁变异证消费）。"""
    from core import config as _config
    return {
        "enabled": _config.as_bool("asset_audit", "enabled", True),
        "auto_bump_version": _config.as_bool("asset_audit", "auto_bump_version", True),
        "max_value_chars": int(_config.get("asset_audit", "max_value_chars", 4000) or 4000),
    }


def bump_version(v: Any) -> str:
    """递升版本号最后一段（v1→v2、v1.0→v1.1、v1.0.0→v1.0.1）。

    非标准形态（空/None/'draft'/'v1.0-beta'...）不猜语义，原样加后缀 ".1"——
    审计的价值在于「可区分相邻两次」，不在于版本号好看。
    """
    s = str(v or "").strip()
    m = _VER_RE.match(s)
    if not m:
        return (s + ".1") if s else "v1.0.1"
    prefix, nums = m.group(1), m.group(2).split(".")
    nums[-1] = str(int(nums[-1]) + 1)
    return prefix + ".".join(nums)


def _norm(v: Any) -> Optional[str]:
    """diff 比较口径：None 保持 None，其余统一转字符串（DB TEXT/int 与调用方
    传入的 json.dumps 结果/原生 int 在这一层对齐；1 与 '1' 视为相同）。"""
    return None if v is None else str(v)


def diff_values(before: dict, new_values: dict) -> list[tuple[str, Optional[str], Optional[str]]]:
    """字段级 diff：返回 [(field, old, new), ...]（归一化后比较，仅含真变化）。"""
    out = []
    for k, new in new_values.items():
        if k not in before:
            continue                      # 非本表列：由调用方负责，diff 不猜
        old = _norm(before.get(k))
        nv = _norm(new)
        if old != nv:
            out.append((k, old, nv))
    return out


def _clip(v: Optional[str], max_chars: int) -> Optional[str]:
    if v is None or len(v) <= max_chars:
        return v
    return v[:max_chars] + f"…[{len(v) - max_chars} more chars]"


def _fetch_one(obj, sql: str, params: tuple) -> Optional[dict]:
    """取单行 dict：BaseRepo 用 .one()（其 .execute 返 lastrowid/rowcount，不是
    cursor——直接 .execute().fetchone() 会 AttributeError）；裸 sqlite3.Connection
    （row_factory=Row）走 execute().fetchone()。两条路径都不抛「表不存在」之外
    的形态差异。"""
    one = getattr(obj, "one", None)
    if callable(one):
        return one(sql, params)
    row = obj.execute(sql, params).fetchone()
    return dict(row) if row else None


def audited_update(conn_or_repo, asset_type: str, asset_id: int, new_values: dict, *,
                   table: str, id_col: str = "id", version_col: str = "version",
                   name_col: str = "name", actor: str = "", source: str = "api",
                   settings: Optional[dict] = None) -> dict:
    """带审计的通用资产更新（四类资产 update_* 的唯一实现路径）。

    参数：
      conn_or_repo —— 带 .execute(sql, params) 的对象（BaseRepo 子类或
                      row_factory=sqlite3.Row 的 sqlite3.Connection）；
      asset_type   —— 资产类型名（进 asset_change_logs.asset_type）；
      asset_id     —— 资产主键；
      new_values   —— {列名: 新值}；**只写真正变化的列**，缺键/None 视语义由
                      调用方决定是否纳入（None 纳入 = 显式置空）；
      table        —— 资产表名；version_col/name_col —— 版本/名称列名。

    返回：{"ok", "changed", "applied", "logged", "diffs", "version_from",
           "version_to", "ignored"}。无 diff 时 applied=False（未执行 UPDATE）。
    行不存在时 ok=False、changed=False（调用方应先做存在性检查）。

    ⚠️ 事务：本函数不 commit/rollback——与调用方共用连接与事务（db_session 请求
       结束时统一提交）。审计 INSERT 失败会抛异常上滚，保证「改了必审计」。
    """
    st = settings or get_audit_settings()
    ex = conn_or_repo.execute
    before = _fetch_one(conn_or_repo, f"SELECT * FROM {table} WHERE {id_col}=?", (asset_id,))
    if before is None:
        return {"ok": False, "changed": False, "applied": False, "logged": 0,
                "diffs": [], "version_from": "", "version_to": "", "ignored": [],
                "error": "资产不存在"}

    # 只认真实列（防御：老库缺列/调用方多传键时不炸，后者记为 ignored）
    values = {k: v for k, v in new_values.items() if k in before}
    ignored = [k for k in new_values if k not in before]

    diffs = diff_values(before, values)
    result = {"ok": True, "changed": bool(diffs), "applied": False, "logged": 0,
              "diffs": [{"field": k, "old": o, "new": n} for k, o, n in diffs],
              "version_from": str(before.get(version_col) or "") if version_col in before else "",
              "version_to": "", "ignored": ignored}
    if not diffs:
        return result                      # 无 diff 不动手（不碰 updated_at、不写审计）

    # ── 版本：调用方显式给 ⇒ 尊重；未给且开自动递升 ⇒ 递升后一并写入 ──
    set_cols = dict(values)
    if version_col in before and version_col not in set_cols and st.get("auto_bump_version"):
        bumped = bump_version(before.get(version_col))
        set_cols[version_col] = bumped
        result["version_to"] = bumped
    elif version_col in set_cols:
        result["version_to"] = _norm(set_cols[version_col]) or ""

    # ── 应用 UPDATE（updated_at 仅在该表确有此列时维护）──
    cols = list(set_cols.keys())
    assigns = ", ".join(f"`{c}`=?" for c in cols)
    if "updated_at" in before:
        assigns += ", updated_at=CURRENT_TIMESTAMP"
    ex(f"UPDATE {table} SET {assigns} WHERE {id_col}=?", (*[set_cols[c] for c in cols], asset_id))
    result["applied"] = True

    # ── 审计：一行一个变更字段（与 audit_logs 的动作流水互补，可回放）──
    if st.get("enabled"):
        asset_name = str(set_cols.get(name_col) or before.get(name_col) or "")
        max_chars = int(st.get("max_value_chars") or 4000)
        for k, old, new in diffs:
            ex("INSERT INTO asset_change_logs (asset_type, asset_id, asset_name, field,"
               " old_value, new_value, version_from, version_to, changed_by, source)"
               " VALUES (?,?,?,?,?,?,?,?,?,?)",
               (asset_type, asset_id, asset_name, k,
                _clip(old, max_chars), _clip(new, max_chars),
                result["version_from"], result["version_to"], actor, source))
            result["logged"] += 1
    return result
