# -*- coding: utf-8 -*-
"""P0-0（2026-11-22 第六批次）**批量写入回读核验**。

## 为什么需要单独一层
`graph_write_gate` 守的是单条实体/关系写入（写后回读 + 四态）；台账里
`bulk` 类 61 处是**事务性批量写**（分支合并、快照回滚、实体去重、SysML 导入），
逐条改成 `write_entity` 会破坏事务边界与快照语义（rollback 是"整层删除再恢复"，
不是 N 次单条写）。但批量写同样会"返回 ok 而效果没落库"：

> `rollback_merge` 原实现删除 release 整层后按快照恢复，只返回 `restored_*
> **计数**、不回读。恢复漏行时计数仍为正，路由照常记审计并回 200。

本模块补的就是这一层：**批量操作完成后按期望集合回读**，复用 A9 的
`_post_result()` 四态构造（单一真源），不另写状态口径。

## 两种模式
- `restore`（回滚）：期望**精确集合**——release 的实体 id 集与关系三元组集
  必须与快照完全相等（多一行、少一行都算 failed）。
- `merge`（合并）：期望**子集**——源分支的实体 id / 关系三元组必须出现在
  目标分支；目标分支允许多既有行（git merge 语义）。

## 旁路原则
回读查询抛异常 ⇒ `indeterminate=True` ⇒ `unknown`（"没验成"不是"验出不对"），
且调用方会给 `retry_safe=False`；与 A9 同型，绝不把观测故障说成数据错误。
"""
from __future__ import annotations

from core.graph_write_gate import _post_result  # A9 四态构造单一真源


def _entity_ids(conn, branch: str) -> set:
    return {str(r[0]) for r in conn.execute(
        "SELECT id FROM entities WHERE branch=?", (branch,))}


def _relation_sigs(conn, branch: str) -> set:
    """关系按 (source_id, target_id, relation_type) 三元组比对。

    为什么不按 id：release 合并走 INSERT（不带 id）会生成新自增 id，
    与源分支 id 不同；按 id 比对会把正常合并判成 failed。
    """
    return {(str(r[0]), str(r[1]), str(r[2])) for r in conn.execute(
        "SELECT source_id, target_id, relation_type FROM relations WHERE branch=?",
        (branch,))}


def _sig_of(rows) -> set:
    out = set()
    for r in (rows or []):
        if isinstance(r, dict):
            s, t, ty = r.get("source_id") or r.get("source"), \
                       r.get("target_id") or r.get("target"), \
                       r.get("relation_type") or r.get("type")
        else:
            try:
                s, t, ty = r[0], r[1], r[2]
            except Exception:
                continue
        if s is not None and t is not None and ty is not None:
            out.add((str(s), str(t), str(ty)))
    return out


def verify_branch_write(conn, branch: str, expected_entities=None,
                        expected_relations=None, mode: str = "restore") -> dict:
    """批量写入后回读核验。返回 A9 同型 `{verified, verify_status, op, checks}`。

    :param expected_entities: 期望实体 id 列表/集合（restore=精确，merge=子集）
    :param expected_relations: 期望关系行（dict 或三元组序列）
    :param mode: 'restore' 精确集合 / 'merge' 子集
    """
    op = "branch_" + ("restore" if mode == "restore" else "merge")
    exp_e = {str(x) for x in (expected_entities or [])}
    exp_r = _sig_of(expected_relations)
    checks = []
    indeterminate = False
    try:
        act_e = _entity_ids(conn, branch)
        act_r = _relation_sigs(conn, branch)
    except Exception as ex:
        indeterminate = True
        checks.append({"check": "readback", "ok": False,
                       "detail": "回读失败：%s" % str(ex)[:120]})
        act_e, act_r = set(), set()

    if not indeterminate:
        if mode == "restore":
            miss_e, extra_e = exp_e - act_e, act_e - exp_e
            miss_r, extra_r = exp_r - act_r, act_r - exp_r
            checks.append({
                "check": "entities_exact", "ok": not miss_e and not extra_e,
                "detail": ("缺失 %s；多出 %s" % (
                    sorted(miss_e)[:6], sorted(extra_e)[:6])) if (miss_e or extra_e) else ""})
            checks.append({
                "check": "relations_exact", "ok": not miss_r and not extra_r,
                "detail": ("缺失 %s；多出 %s" % (
                    sorted(miss_r)[:6], sorted(extra_r)[:6])) if (miss_r or extra_r) else ""})
        else:
            miss_e, miss_r = exp_e - act_e, exp_r - act_r
            checks.append({
                "check": "entities_present", "ok": not miss_e,
                "detail": ("目标分支缺实体 %s" % sorted(miss_e)[:6]) if miss_e else ""})
            checks.append({
                "check": "relations_present", "ok": not miss_r,
                "detail": ("目标分支缺关系 %s" % sorted(miss_r)[:6]) if miss_r else ""})
    return _post_result(checks, op, indeterminate=indeterminate)


def apply_to_result(result: dict, post: dict, prefix: str = "批量写入") -> dict:
    """把核验状态贴到批量操作回执上；failed 改判失败，unknown 标不可重试。

    调用方原有的 key（如 restored_entities/moved）一律保留，只增不改 ⇒ 零回归。
    """
    if not isinstance(result, dict):
        return result
    result["post"] = post
    status = post.get("verify_status")
    if status == "failed":
        bad = "; ".join("%s=%s" % (c.get("check"), c.get("detail"))
                        for c in post.get("checks", []) if not c.get("ok"))[:300]
        result["ok"] = False
        result["applied"] = True
        result["error"] = "%s已执行，但回读核验未通过：%s" % (prefix, bad)
    elif status == "unknown":
        result["retry_safe"] = False
    return result
