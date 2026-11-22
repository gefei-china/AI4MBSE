# -*- coding: utf-8 -*-
"""P0-0 第六/七批门禁：批量写入回读核验（A9 四态）+ 回执端到端 + 快照消费顺序。

## 为什么需要
`rollback_merge` 曾只返回 restored_* 计数、不回读：恢复漏行时路由照常回 200。
第六批改回读 + failed 改判；第七批又抓到两个漏网口：
- repo 核验完成后**先清空快照**再返回 ⇒ failed/unknown 无法二次回滚（可恢复失败被做成不可逆）；
- 路由层**手工重建白名单响应**，把 `post`/`retry_safe` 裁掉 ⇒ unknown 被前端当成纯成功。

判据：
  S1~S8  （第六批）四态 + apply 改判 + 原 key 保留
  S9     真跑 rollback_merge ⇒ verified
  S9b    verified 时快照消费
  S11    回读 failed ⇒ ok=False 且快照保留（可重试）
  S12    回读 unknown ⇒ retry_safe=False 且快照保留
  S13    rollback 路由 `**result` 透传四态（静态锁）
  S14    快照消费语句在回读 SELECT **之后**（SQL 顺序动态锁）
  S15    failed 重试不重复写 rollback 发布日志（按 mr 幂等）
  M1~M3  （第六批）四态/apply/indeterminate 变异
  M4     模拟"提前消费快照" ⇒ S11 判红
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


class _Broken:
    def execute(self, *a, **k):
        raise RuntimeError("回读故障注入")


class _RecordingConn:
    """只记录 SQL 文本、其余属性全委托的真实连接代理（用于 S14 顺序判据）。"""

    def __init__(self, inner):
        self._inner = inner
        self.sql = []

    def execute(self, *a, **k):
        self.sql.append(str(a[0]) if a else "")
        return self._inner.execute(*a, **k)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _snap(c, mr_id):
    row = c.execute("SELECT prev_release_snapshot FROM merge_requests WHERE id=?",
                    (mr_id,)).fetchone()
    return (row[0] if row else "") or ""


def _fixture(c, mr_id, branch, eid):
    """一条 MR + release 分支 + 合并前快照（每个用例独立 id，避免变异重跑撞 UNIQUE）。"""
    c.execute("DELETE FROM knowledge_publish_logs WHERE merged_from=?",
              (f"回滚自合并请求#{mr_id}",))
    c.execute("DELETE FROM merge_requests WHERE id=?", (mr_id,))
    c.execute("DELETE FROM branches WHERE name=?", (branch,))
    c.execute("INSERT INTO branches (name,branch_type) VALUES (?,?)", (branch, "release"))
    snap = {"entities": [{"id": eid, "name": "回滚探针", "branch": branch,
                          "status": "reviewed", "entity_type": "需求", "properties": "{}"}],
            "relations": []}
    c.execute("INSERT INTO merge_requests (id, source_branch, target_branch, status, "
              "prev_release_snapshot) VALUES (?,?,?,?,?)",
              (mr_id, "dev", branch, "merged", json.dumps(snap, ensure_ascii=False)))
    c.commit()


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
    caught1 = caught2 = caught3 = caught4 = False
    try:
        c = get_db()
        base = _checks(c)

        # ── S9 / S9b / S14：真跑 rollback_merge 正常路径 ──
        _fixture(c, 9001, "V_RELEASE", "VR1")
        rec = _RecordingConn(c)
        rb = BranchRepo(rec).rollback_merge(9001)
        base.append((rb.get("ok") is True
                     and (rb.get("post") or {}).get("verify_status") == "verified",
                     "S9 真跑 rollback_merge 核验通过", str(rb)[:180]))
        base.append((_snap(c, 9001) == "{}", "S9b verified 后快照消费", _snap(c, 9001)[:60]))
        idx_clear = max([i for i, s in enumerate(rec.sql)
                         if "prev_release_snapshot='{}'" in s] or [-1])
        idx_read = [i for i, s in enumerate(rec.sql)
                    if s.strip().lower().startswith(
                        ("select id from entities where branch=?",
                         "select source_id, target_id, relation_type from relations where branch=?"))]
        base.append((idx_clear >= 0 and bool(idx_read) and idx_clear > max(idx_read),
                     "S14 快照消费在回读之后",
                     "clear=%s read=%s" % (idx_clear, idx_read)))

        # ── S11 / S15：failed 注入 ⇒ 快照保留 + 重试不重复留痕 ──
        orig_verify = _bwv.verify_branch_write

        def _fake_failed(conn, branch, expected_entities=None, expected_relations=None,
                         mode="restore"):
            return orig_verify(conn, branch, ["__MISSING__"], [], mode=mode)

        _fixture(c, 9002, "V_RELEASE2", "VR2")
        _bwv.verify_branch_write = _fake_failed
        try:
            rb2 = BranchRepo(c).rollback_merge(9002)
            n1 = c.execute("SELECT COUNT(*) FROM knowledge_publish_logs WHERE merged_from=?",
                           ("回滚自合并请求#9002",)).fetchone()[0]
            rb2b = BranchRepo(c).rollback_merge(9002)  # 重试
            n2 = c.execute("SELECT COUNT(*) FROM knowledge_publish_logs WHERE merged_from=?",
                           ("回滚自合并请求#9002",)).fetchone()[0]
        finally:
            _bwv.verify_branch_write = orig_verify
        base.append((rb2.get("ok") is False and rb2b.get("ok") is False
                     and "VR2" in _snap(c, 9002),
                     "S11 failed ⇒ 快照保留（可恢复）", str(rb2)[:180]))
        base.append((n1 == 1 and n2 == 1, "S15 failed 重试不重复写 rollback 日志",
                     "n1=%s n2=%s" % (n1, n2)))

        # ── S12：unknown 注入 ⇒ retry_safe=False 且快照保留 ──
        def _fake_unknown(*a, **k):
            return orig_verify(_Broken(), "X", [], [], mode="restore")

        _fixture(c, 9003, "V_RELEASE3", "VR3")
        _bwv.verify_branch_write = _fake_unknown
        try:
            rb3 = BranchRepo(c).rollback_merge(9003)
        finally:
            _bwv.verify_branch_write = orig_verify
        base.append((rb3.get("ok") is True and rb3.get("retry_safe") is False
                     and "VR3" in _snap(c, 9003),
                     "S12 unknown ⇒ 快照保留 + 不可重试", str(rb3)[:180]))

        # ── S13：路由层四态透传（静态锁，防白名单响应回归）──
        rsrc = open(os.path.join(_ROOT, "routers", "branches.py"),
                    encoding="utf-8").read()
        i = rsrc.find("def rollback_merge(mr_id")
        j = rsrc.find("\n@router", i + 1) if i >= 0 else -1
        rseg = rsrc[i:j if j > i else len(rsrc)]
        base.append(("**result" in rseg, "S13 rollback 路由四态透传", ""))

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

            # M4：模拟旧顺序（核验之前先消费快照），S11 的同款判据必须判红
            _bwv._post_result = orig_post
            _fixture(c, 9004, "V_RELEASE4", "VR4")

            def _early(*a, **k):
                c.execute("UPDATE merge_requests SET prev_release_snapshot='{}' WHERE id=9004")
                return _fake_failed(*a, **k)
            _bwv.verify_branch_write = _early
            try:
                rb4 = BranchRepo(c).rollback_merge(9004)
            finally:
                _bwv.verify_branch_write = orig_verify
            caught4 = not (rb4.get("ok") is False and "VR4" in _snap(c, 9004))
            print("  [%s] M4 提前消费快照 ⇒ S11 判红" % ("PASS" if caught4 else "FAIL"))
        finally:
            _bwv._post_result = orig_post
            _bwv.apply_to_result = orig_apply
            _bwv.verify_branch_write = orig_verify
        c.close()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)

    n_fail = sum(1 for x in _RESULTS if not x[0])
    mut_ok = caught1 and caught2 and caught3 and caught4
    print("=" * 70)
    if n_fail or not mut_ok:
        print("结论：未通过（常态 %d 失败 / 变异 %s）" % (n_fail, mut_ok))
        return 1
    print("结论：全部通过（常态 %d 条 + 变异 4 组按预期判红）" % len(_RESULTS))
    return 0


def _orig_post_real(checks, op, indeterminate=False):
    """M3 用：强制 indeterminate=False 的真实现（避免递归调用被 monkeypatch）。"""
    from core.graph_write_gate import _post_result as _real
    return _real(checks, op, indeterminate=indeterminate)


if __name__ == "__main__":
    sys.exit(main())
