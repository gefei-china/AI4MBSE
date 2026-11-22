#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""门禁：执行后核验第 4 态 `unknown`（A9，2026-11-22）。

## 要挡住的是什么

改造前 post_check 把「查询异常/回读不到」与「回读到了但不一致」都记为
`verified=False`，调用方据此判失败并重试。请求已发出时写没写成客户端无法区分，
重试会造成重复实体/重复关系。现在 `post.verify_status` 分四态，unknown 必须
显式给出「不要重试，先查询确认」。

判据：
  S1  常态写入 verified，四态字段存在且布尔 verified 零回归
  S2  回读到但值不一致 ⇒ failed（确定未达成，与 unknown 必须分开）
  S3  查询抛异常（锁等待）⇒ unknown，且 verified=False
  S4  unknown 文案显式含「不要重试」
  S5  真跑 write_entity 的 unknown 故障注入：顶层透传 verify_status/retry_safe
  S6  删除核验查询异常 ⇒ unknown
  S7  边核验查询异常 ⇒ unknown
  S8  回读不到（无异常）⇒ unknown，不是 failed
  M1  把 _post_result 的 indeterminate 改回 failed ⇒ S3 必须判红（真跑变异）
  M2  抹掉 _post_error 的 unknown 分支 ⇒ S4/S5 必须判红（真跑变异）
  M3  抹掉 write_entity 顶层透传 ⇒ 源码判据必须红

## 隔离
MBSE_DB_PATH 指向临时库，在 import core.config 之前设置；不碰生产库。
"""
from __future__ import annotations

import inspect
import os
import shutil
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

_RESULTS = []


def _add(ok, name, detail=""):
    _RESULTS.append((bool(ok), name, "" if ok else str(detail)[:320]))


class _FailingConn:
    """只对指定 SQL 抛"数据库锁"，其余原样转发——故障注入的唯一定点。"""

    def __init__(self, conn, needle):
        object.__setattr__(self, "_conn", conn)
        object.__setattr__(self, "_needle", needle)

    def execute(self, sql, *args, **kwargs):
        if self._needle in str(sql):
            raise sqlite3.OperationalError("database is locked (injected by A9 gate)")
        return self._conn.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_conn"), name)


def _checks(gw, c, tag):
    """常态判据；M1/M2 变异时复用同一批判据函数，确保"判红"是真的。

    `tag` 区分常态/变异三轮，避免同一临时库里重复 id 触发 UNIQUE。
    """
    out = []
    eid = "%s-E1" % tag

    # S1 常态：真跑统一写入口
    ok = gw.write_entity(c, op="create", entity_id=eid, name="A9 实体",
                         entity_type="需求", properties={}, branch="dev", actor="verify")
    post = ok.get("post") or {}
    out.append((ok.get("ok") is True
                and post.get("verify_status") == "verified"
                and post.get("verified") is True
                and isinstance(post.get("checks"), list),
                "S1 常态写入 verified + 布尔零回归", ok.get("error") or post))

    # S2 failed：回读到了但名字不一致 —— 与 unknown 必须分开
    bad = gw.post_check(c, eid, "dev", "错的名字", "需求", {}, "update")
    out.append((bad.get("verify_status") == "failed"
                and bad.get("verified") is False
                and any(x["check"] == "name" and not x["ok"] for x in bad["checks"]),
                "S2 断言不一致 ⇒ failed", bad))

    # S3/S4 查询异常（锁等待）：unknown + 显式不要重试
    locked = _FailingConn(c, "FROM entity_versions")
    unk = gw.post_check(locked, eid, "dev", "A9 实体", "", {}, "update")
    out.append((unk.get("verify_status") == "unknown"
                and unk.get("verified") is False,
                "S3 查询异常 ⇒ unknown", unk))
    err = gw._post_error("写入已提交，但效果核验未通过", unk)
    out.append(("不要重试" in err and "先查询确认" in err,
                "S4 unknown 文案显式不重试", err))
    failed_err = gw._post_error("写入已提交，但效果核验未通过", bad)
    out.append(("不要重试" not in failed_err,
                "S4b failed 文案不冒充 unknown（两态可区分）", failed_err))

    # S5 真跑 write_entity：故障注入 post_check，验证顶层透传
    orig_post_check = gw.post_check
    try:
        gw.post_check = lambda *a, **kw: dict(unk)
        injected = gw.write_entity(c, op="update", entity_id=eid, name="A9 实体2",
                                   entity_type="需求", properties={}, branch="dev", actor="verify")
    finally:
        gw.post_check = orig_post_check
    out.append((injected.get("ok") is False
                and injected.get("stage") == "post_condition"
                and injected.get("verify_status") == "unknown"
                and injected.get("retry_safe") is False
                and "不要重试" in str(injected.get("error")),
                "S5 write_entity unknown 故障注入透传", injected.get("error") or injected))

    # S6/S7 另外两类核验函数的查询异常分支
    del_conn = _FailingConn(c, "FROM entities WHERE id")
    d = gw.post_check_deleted(del_conn, eid, "dev")
    out.append((d.get("verify_status") == "unknown" and d.get("verified") is False,
                "S6 删除核验查询异常 ⇒ unknown", d))

    econn = _FailingConn(c, "FROM relations WHERE id")
    e = gw.post_check_edge(econn, 1, "dev", op="edge_update")
    out.append((e.get("verify_status") == "unknown" and e.get("verified") is False,
                "S7 边核验查询异常 ⇒ unknown", e))

    # S8 回读不到（没抛异常）：写入后查不到，客户端仍无法判定
    missing = gw.post_check(c, "%s-NOT-EXIST" % tag, "dev", "x", "", {}, "update")
    out.append((missing.get("verify_status") == "unknown"
                and missing.get("verified") is False,
                "S8 回读不到 ⇒ unknown（不是 failed）", missing))
    return out


def main():
    tmp = tempfile.mkdtemp(prefix="mbse_a9_")
    dbp = os.path.join(tmp, "a9.db")
    os.environ["MBSE_DB_PATH"] = dbp
    try:
        import core.config as cfg
        if os.path.abspath(cfg.DB_PATH) != os.path.abspath(dbp):
            raise SystemExit("中止：DB_PATH 未指向临时库：%s" % cfg.DB_PATH)
        from database.schema import init_db
        init_db()

        import core.graph_write_gate as gw
        c = sqlite3.connect(dbp)
        c.row_factory = sqlite3.Row
        try:
            base = _checks(gw, c, "BASE")
            for ok, name, detail in base:
                _add(ok, name, detail)

            # 静态零回归：四态常量 + 路由仍原样透传 post
            src = open(os.path.join(ROOT, "routers", "knowledge_parts", "entities.py"),
                       encoding="utf-8").read()
            _add(all(s in gw.VERIFY_STATUSES for s in
                     ("verified", "failed", "unknown", "skipped")),
                 "S9 四态常量齐全")
            _add('"post": res.get("post")' in src,
                 "S10 路由 post 原样透传（前端可拿 verify_status）")

            # ---- 变异自证（真跑判据，不是字符串对拍）----
            print("\n--- 变异自证 ---")
            orig_post_result = gw._post_result
            orig_post_error = gw._post_error

            # M1 把 unknown 改回 failed（即改造前行为）⇒ S3/S8 必须判红
            def bad_post_result(checks, op, *, indeterminate=False):
                if indeterminate or not checks:
                    status = "failed"  # 变异：抹掉 unknown
                elif all(x["ok"] for x in checks):
                    status = "verified"
                else:
                    status = "failed"
                return {"verified": status == "verified", "verify_status": status,
                        "op": op, "checks": list(checks)}

            gw._post_result = bad_post_result
            try:
                m1 = _checks(gw, c, "M1")
            finally:
                gw._post_result = orig_post_result
            s3 = next(x for x in m1 if x[1].startswith("S3"))
            s8 = next(x for x in m1 if x[1].startswith("S8"))
            caught_m1 = (not s3[0]) and (not s8[0])
            print("  [%s] M1 unknown 改回 failed ⇒ S3/S8 判红" % ("PASS" if caught_m1 else "FAIL"))

            # M2 抹掉 unknown 专用文案 ⇒ S4/S5 必须判红
            def bad_post_error(prefix, post):
                bad = "; ".join(f"{x['check']}={x['detail']}" for x in post.get("checks", [])
                                if not x["ok"])[:300]
                return f"{prefix}：{bad}" if bad else prefix

            gw._post_error = bad_post_error
            try:
                m2 = _checks(gw, c, "M2")
            finally:
                gw._post_error = orig_post_error
            # 名称前缀是 "S4 unknown..."，排除 S4b 后取 S4 本身
            s4 = next(x for x in m2 if x[1].startswith("S4") and not x[1].startswith("S4b"))
            s5 = next(x for x in m2 if x[1].startswith("S5"))
            caught_m2 = (not s4[0]) and (not s5[0])
            print("  [%s] M2 抹掉不重试文案 ⇒ S4/S5 判红" % ("PASS" if caught_m2 else "FAIL"))

            # M3 抹掉 write_entity 顶层透传字段 ⇒ 源码判据红
            wsrc = inspect.getsource(gw.write_entity)
            mut3 = wsrc.replace('"verify_status": post["verify_status"], "retry_safe": False,', "")
            caught_m3 = ('"verify_status": post["verify_status"]' in wsrc
                         and '"verify_status": post["verify_status"]' not in mut3)
            print("  [%s] M3 抹掉顶层透传 ⇒ S5 依赖消失" % ("PASS" if caught_m3 else "FAIL"))
        finally:
            c.close()

        n_fail = sum(1 for x in _RESULTS if not x[0])
        print()
        for ok, name, detail in _RESULTS:
            print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name,
                                   ("  <- " + detail) if detail else ""))
        print("=" * 76)
        mut_ok = caught_m1 and caught_m2 and caught_m3
        if n_fail or not mut_ok:
            print("结论：未通过（常态 %d 失败 / 变异 %s）" % (n_fail, mut_ok))
            return 1
        print("结论：全部通过（常态 %d 条 + 变异 3 组按预期判红）" % len(_RESULTS))
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
