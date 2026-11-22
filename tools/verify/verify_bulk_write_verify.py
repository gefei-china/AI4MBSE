# -*- coding: utf-8 -*-
"""P0-0 第六批次门禁：批量写入回读核验（A9 四态）真跑 + 接线 + 变异自证。

## 为什么需要
`rollback_merge` 曾只返回 restored_* 计数、不回读：恢复漏行时路由照常回 200。
本门禁守：批量写后按期望集合回读，failed 改判失败、unknown 不可重试，
且原有回执 key 一律保留（零回归）。

判据：
  S1 restore 精确匹配 ⇒ verified
  S2 restore 缺实体 ⇒ failed
  S3 restore 多关系 ⇒ failed
  S4 merge 子集（目标多既有行）⇒ verified
  S5 merge 缺实体 ⇒ failed
  S6 回读异常 ⇒ unknown（不是 failed）
  S7 apply failed ⇒ ok=False / applied=True / error
  S8 apply unknown ⇒ retry_safe=False 且原 key 保留
  S9 真跑 BranchRepo.rollback_merge ⇒ post.verified 且 ok=True
  S10 静态接线：rollback_merge / resolve_merge 都调 verify_branch_write
  M1 抹四态（恒 verified）⇒ S2/S3 判红
  M2 抹 apply 的 failed 分支 ⇒ S7 判红
  M3 抹 indeterminate ⇒ S6 判红
"""
import json
import os
import shutil
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

_TMP = tempfile.mkdtemp(prefix="verify_bulk_write_")
os.environ["MBSE_DB_PATH"] = os.path.join(_TMP, "db.sqlite")
os.environ["MBSE_LLM_FORCE_MOCK"] = "1"

from database import init_db, get_db              # noqa: E402
init_db()

from core import bulk_write_verify as _bwv         # noqa: E402
from repositories.branch_repo import BranchRepo    # noqa: E402

_RESULTS = []


def ck(name, cond, detail=""):
    _RESULTS.append((bool(cond), name, detail))
    print("[%s] %s%s" % ("PASS" if cond else "FAIL", name,
                         ("  <- " + detail) if detail else ""))


def _checks(conn):
    out = []
    def add(name, cond, detail=""):
        out.append((bool(cond), name, detail))

    c = conn
    # 先删关系再删实体（relations 对 entities 有外键；变异重跑时不能反）
    c.execute("DELETE FROM relations WHERE branch='V_BULK'")
    c.execute("DELETE FROM entities WHERE branch='V_BULK'")
    c.execute("INSERT INTO entities (id,name,branch,status,entity_type,properties) "
              "VALUES ('BE1','E1','V_BULK','reviewed','需求','{}'), "
                     "('BE2','E2','V_BULK','reviewed','需求','{}')")
    c.execute("INSERT INTO relations (source_id,target_id,relation_type,branch,properties,status) "
              "VALUES ('BE1','BE2','R','V_BULK','{}','reviewed')")
    c.commit()

    p = _bwv.verify_branch_write(c, "V_BULK", ["BE1", "BE2"],
                                 [{"source_id": "BE1", "target_id": "BE2",
                                   "relation_type": "R"}], mode="restore")
    add("S1 restore 精确匹配", p["verify_status"] == "verified", str(p)[:150])

    p2 = _bwv.verify_branch_write(c, "V_BULK", ["BE1", "BE2", "MISSING"],
                                  [{"source_id": "BE1", "target_id": "BE2",
                                    "relation_type": "R"}], mode="restore")
    add("S2 restore 缺实体 failed", p2["verify_status"] == "failed", str(p2)[:150])

    p3 = _bwv.verify_branch_write(c, "V_BULK", ["BE1", "BE2"],
                                  [{"source_id": "BE1", "target_id": "BE2",
                                    "relation_type": "R"},
                                   {"source_id": "X", "target_id": "Y",
                                    "relation_type": "EXTRA"}], mode="restore")
    add("S3 restore 多关系 failed", p3["verify_status"] == "failed", str(p3)[:150])

    p4 = _bwv.verify_branch_write(c, "V_BULK", ["BE1"],
                                  [{"source_id": "BE1", "target_id": "BE2",
                                    "relation_type": "R"}], mode="merge")
    add("S4 merge 子集 verified", p4["verify_status"] == "verified", str(p4)[:150])

    p5 = _bwv.verify_branch_write(c, "V_BULK", ["BE1", "NOPE"], [], mode="merge")
    add("S5 merge 缺实体 failed", p5["verify_status"] == "failed", str(p5)[:150])

    class _Broken:
        def execute(self, *a, **k):
            raise RuntimeError("回读故障注入")
    p6 = _bwv.verify_branch_write(_Broken(), "V_BULK", ["BE1"], [], mode="restore")
    add("S6 回读异常 unknown", p6["verify_status"] == "unknown", str(p6)[:150])

    r7 = _bwv.apply_to_result({"ok": True, "restored_entities": 1}, p2, "回滚")
    add("S7 apply failed 改判", r7["ok"] is False and r7.get("applied") is True
        and "error" in r7 and r7["restored_entities"] == 1, str(r7)[:150])

    r8 = _bwv.apply_to_result({"ok": True, "restored_entities": 1}, p6, "回滚")
    add("S8 apply unknown 不可重试", r8["ok"] is True
        and r8.get("retry_safe") is False and r8["restored_entities"] == 1,
        str(r8)[:150])
    return out


def main():
    try:
        c = get_db()
        base = _checks(c)

        # S9 真跑 rollback_merge（夹具形态复用 verify_doc_folders B7）
        c.execute("DELETE FROM merge_requests WHERE id=9001")
        c.execute("DELETE FROM branches WHERE name='V_RELEASE'")
        c.execute("INSERT INTO branches (name,branch_type) VALUES ('V_RELEASE','release')")
        snap = {"entities": [{"id": "VR1", "name": "回滚探针", "branch": "V_RELEASE",
                              "status": "reviewed", "entity_type": "需求", "properties": "{}"}],
                "relations": []}
        c.execute("INSERT INTO merge_requests (id, source_branch, target_branch, status, "
                  "prev_release_snapshot) VALUES (9001,'dev','V_RELEASE','merged',?)",
                  (json.dumps(snap, ensure_ascii=False),))
        c.commit()
        rb = BranchRepo(c).rollback_merge(9001)
        s9 = (rb.get("ok") is True and (rb.get("post") or {}).get("verify_status") == "verified")
        base.append((s9, "S9 真跑 rollback_merge 核验通过", str(rb)[:180]))

        src = open(os.path.join(_ROOT, "repositories", "branch_repo.py"),
                   encoding="utf-8").read()
        # 用函数切片避免全局命中（两处函数都必须接线）
        i_rb = src.find("def rollback_merge(")
        i_rm = src.find("def resolve_merge(")
        # 两函数在源码里的先后不假设（上次假设反了导致 S10a 假红）：
        # 按位置排序后逐个切到下一个函数起点，最后一段切到文件末尾。
        spans = sorted([(i, "rb") for i in (i_rb,) if i >= 0]
                       + [(i, "rm") for i in (i_rm,) if i >= 0])
        seg = {}
        for idx, (pos, tag) in enumerate(spans):
            end = spans[idx + 1][0] if idx + 1 < len(spans) else len(src)
            seg[tag] = src[pos:end]
        seg_rb, seg_rm = seg.get("rb", ""), seg.get("rm", "")
        base.append(("verify_branch_write" in seg_rb, "S10a rollback_merge 接线", ""))
        base.append(("verify_branch_write" in seg_rm, "S10b resolve_merge 接线", ""))
        for ok, name, detail in base:
            ck(name, ok, detail)

        # ---- 变异自证（真跑判据，复用同一批判据函数）----
        print("\n--- 变异自证 ---")
        orig_post = _bwv._post_result
        orig_apply = _bwv.apply_to_result
        orig_broken_ok = None
        try:
            _bwv._post_result = lambda checks, op, indeterminate=False: {
                "verified": True, "verify_status": "verified", "op": op,
                "checks": list(checks)}
            m1 = _checks(c)
            n1 = [x for x in m1 if "S2" in x[1] or "S3" in x[1]]
            caught1 = len(n1) == 2 and all(not x[0] for x in n1)
            print("  [%s] M1 抹四态 ⇒ S2/S3 判红" % ("PASS" if caught1 else "FAIL"))

            _bwv._post_result = orig_post
            def bad_apply(result, post, prefix="批量写入"):
                result["post"] = post
                return result
            _bwv.apply_to_result = bad_apply
            m2 = _checks(c)
            s7 = next(x for x in m2 if "S7" in x[1])
            caught2 = not s7[0]
            print("  [%s] M2 抹 failed 改判 ⇒ S7 判红" % ("PASS" if caught2 else "FAIL"))

            _bwv.apply_to_result = orig_apply
            _bwv._post_result = lambda checks, op, indeterminate=False: _orig_post_real(
                checks, op, False)
            m3 = _checks(c)
            s6 = next(x for x in m3 if "S6" in x[1])
            caught3 = not s6[0]
            print("  [%s] M3 抹 indeterminate ⇒ S6 判红" % ("PASS" if caught3 else "FAIL"))
        finally:
            _bwv._post_result = orig_post
            _bwv.apply_to_result = orig_apply
        c.close()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)

    n_fail = sum(1 for x in _RESULTS if not x[0])
    mut_ok = caught1 and caught2 and caught3
    print("=" * 70)
    if n_fail or not mut_ok:
        print("结论：未通过（常态 %d 失败 / 变异 %s）" % (n_fail, mut_ok))
        return 1
    print("结论：全部通过（常态 %d 条 + 变异 3 组按预期判红）" % len(_RESULTS))
    return 0


def _orig_post_real(checks, op, indeterminate=False):
    """M3 用：强制 indeterminate=False 的真实现（避免递归调用被 monkeypatch）。"""
    from core.graph_write_gate import _post_result as _real
    return _real(checks, op, indeterminate=indeterminate)


if __name__ == "__main__":
    sys.exit(main())
