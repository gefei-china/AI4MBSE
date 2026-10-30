# CI-OPTIONAL: 夹具 DDL 从 mbse.db 复制（entities/relations/documents，`mbse.db` 不入 git），F 组为真库体检。
# -*- coding: utf-8 -*-
"""门禁：`basis=verified` / `claims.kind=fact` 必须**指得到受管元素**（P0-4，2026-10-30）。

## 它守的边界（与 `services/subtask_protocol.py` 的能力边界**不冲突**）
- 不判"模型是否真的查过图谱"（那不可机械化，协议 docstring 已如实声明）；
- 只判"它给出的引用**在库里能不能被找到**"（这一条可机械化，且正是读者能否回查的前提）。

真库失效形态：模型给 `basis=verified` + `ref="E-P10-001"`，而 `entities` 里没有这个 id，
下游（物化 / 汇总 / 评审 / 前端徽标）把它当"已验证"采信。

## 判据三条（全部"只降不升"）
1. `verified` + ref 查不到 ⇒ 降 `asserted` 并留 `ref_unresolved`；
2. `claims.kind=fact` 但该子任务**零条可回查依据** ⇒ 降 `to_verify`（"fact" 的定义就是可独立复核）；
3. **fail-open**：无连接 / 老库缺表 / 路径校验异常 ⇒ **不降级**（宁可漏判，不可误降）。
"""
import os
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from services import evidence_check as ec          # noqa: E402
from services import subtask_protocol as sp        # noqa: E402

_results = []


def check(name, ok, detail=""):
    _results.append((bool(ok), name, detail))
    print("  %s %s%s" % ("PASS" if ok else "FAIL", name,
                         ("  | " + str(detail)[:200]) if (detail and not ok) else ""))


def hr(t):
    print("\n" + "=" * 78)
    print(t)
    print("=" * 78)


def fixture():
    """最小夹具库：**DDL 从真库复制**（不手抄 —— 手抄必然漂移，本轮已踩）＋ 三条真实行。

    ⚠️ 第一版手写 `CREATE TABLE relations (id, src_id)`，而真库列名是 `source_id`
    ⇒ 关系通道的 SQL 在夹具上恒抛异常 ⇒ 被 fail-open 吞成"跳过" ⇒
    A2/B1（该降级却没降）与 A9/A10（通道失效）**同时红**。
    这正是本仓反复记录的"夹具手抄 DDL 漂移"坑，修法与 `verify_context_scope_guard` 一致：
    **从真库 sqlite_master 抄 DDL**。
    """
    src = sqlite3.connect("file:%s?mode=ro" % os.path.join(ROOT, "mbse.db"), uri=True)
    ddl = []
    for (sql,) in src.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name IN "
            "('entities','relations','documents') AND sql IS NOT NULL"):
        ddl.append(sql)
    src.close()
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    for sql in ddl:
        conn.execute(sql)
    # ⚠️ 真库有 NOT NULL 列（entity_type / relation_type / folder_id）——
    #    从真库抄 DDL 就必须**同时**满足它的约束（这正是"抄 DDL 而不手写"的代价：
    #    手写版会漏掉这些约束，于是夹具比生产宽松，测不出真实失败）。
    conn.execute("INSERT INTO entities (id, name, entity_type) "
                 "VALUES ('E-P10-001','动力分系统','Part')")
    # relations.id 是 INTEGER 主键（真库实测），不能塞字符串 —— 用整数并让 ref 走 id
    conn.execute("INSERT INTO relations (id, source_id, target_id, relation_type) "
                 "VALUES (381,'E-P10-001','E-P10-001','CONTAINS')")
    conn.execute("INSERT INTO documents (id, filename, folder_id) VALUES (7,'spec.md',0)")
    conn.commit()
    return conn, path


def _summ(evs, claims=None):
    """造一份已 validate 过的 summary（走真实 validate，不手搓结构）。"""
    raw = {"schema_version": 1, "task_key": "t1", "status": "full",
           "summary": "夹具结论", "artifacts": [], "evidence": evs,
           "risks": [], "claims": claims or []}
    return sp.validate(raw)["sanitized"]


conn, path = fixture()
try:
    # ── A 基本判定 ──
    hr("[A] 引用可回查：命中/未命中的处置")
    s = _summ([{"source": "graph", "ref": "E-P10-001", "basis": "verified"}])
    ec.enforce(conn, s)
    check("A1 verified + ref 命中 ⇒ 保持 verified",
          s["evidence"][0]["basis"] == "verified", "got=%s" % s["evidence"][0]["basis"])

    s = _summ([{"source": "graph", "ref": "E-NOT-EXIST", "basis": "verified"}])
    ec.enforce(conn, s)
    e0 = s["evidence"][0]
    check("A2 verified + ref 未命中 ⇒ 降为 asserted", e0["basis"] == "asserted",
          "got=%s" % e0["basis"])
    check("A3 降级必须留痕（ref_unresolved 写清为什么降）",
          bool(e0.get("ref_unresolved")), "meta/字段缺失 ⇒ 读者不知道为何降档")
    check("A4 降级写入 meta.evidence_downgraded（可观测）",
          (s.get("meta") or {}).get("evidence_downgraded", {}).get("basis") == 1,
          "meta=%s" % (s.get("meta") or {}))

    s = _summ([{"source": "graph", "ref": "E-NOT-EXIST", "basis": "asserted"}])
    ec.enforce(conn, s)
    check("A5 asserted + 未命中 ⇒ 不变（本来就最保守，无需再降）",
          s["evidence"][0]["basis"] == "asserted")

    s = _summ([{"source": "graph", "ref": "E-NOT-EXIST", "basis": "derived"}])
    ec.enforce(conn, s)
    check("A6 derived + 未命中 ⇒ 不降级，但标注 ref_unresolved",
          s["evidence"][0]["basis"] == "derived" and bool(s["evidence"][0].get("ref_unresolved")),
          "got=%s" % s["evidence"][0])

    s = _summ([{"source": "vector", "ref": "doc:7", "basis": "verified"}])
    ec.enforce(conn, s)
    check("A7 doc: 前缀走 documents 通道且命中",
          s["evidence"][0]["basis"] == "verified", "got=%s" % s["evidence"][0]["basis"])

    s = _summ([{"source": "attachment", "ref": "docs/README.md", "basis": "verified"}])
    ec.enforce(conn, s)
    exp = os.path.exists(os.path.join(ROOT, "docs/README.md"))
    check("A8 路径型 ref 按文件存在性判定（本仓 docs/README.md 存在）",
          s["evidence"][0]["basis"] == ("verified" if exp else "asserted"),
          "got=%s exp_exists=%s" % (s["evidence"][0]["basis"], exp))

    # ── A9/A10：**fail-open 的可见性**（本轮真踩到的假阳性来源）──
    # 第一版 `relations` 的 SQL 写了不存在的列 `src_id` ⇒ 每次查询抛异常 ⇒
    # 被 fail-open 吞成"跳过" ⇒ 关系通道整体失效，而体检输出看起来**全是 OK**。
    # 判别信号就藏在 reason 里：正常应是"命中/未命中"，异常才是"查询失败（跳过）"。
    ok9, why9 = ec.resolve_ref(conn, "graph", "rel:381")
    check("A9 关系通道命中（ref=rel:381），且 reason 不是『查询失败』⇒ 通道真的在工作",
          ok9 and "查询失败" not in why9, "ok=%s why=%s" % (ok9, why9))
    ok10, why10 = ec.resolve_ref(conn, "graph", "rel:999999")
    check("A10 关系通道未命中时如实报『未命中』（不是靠异常兜成 OK）",
          (not ok10) and "未命中" in why10, "ok=%s why=%s" % (ok10, why10))

    # 真库三通道联通性：任一通道报"查询失败"都说明 SQL 与库结构漂移
    try:
        import sqlite3 as _s3
        _ro = _s3.connect("file:%s?mode=ro" % os.path.join(ROOT, "mbse.db"), uri=True)
        _ent = _ro.execute("SELECT id FROM entities LIMIT 1").fetchone()
        _rel = _ro.execute("SELECT id FROM relations LIMIT 1").fetchone()
        _doc = _ro.execute("SELECT id FROM documents LIMIT 1").fetchone()
        _chs = [("entity", _ent[0] if _ent else ""),
                ("relation", _rel[0] if _rel else ""),
                ("doc", _doc[0] if _doc else "")]
        _bad = [k for k, v in _chs if v and "查询失败" in ec._by_sql(_ro, k, str(v))[1]]
        check("A11 真库三通道 SQL 与库结构一致（无不存在的列）", not _bad,
              "报『查询失败』的通道：%s（列名漂移 ⇒ 该通道静默失效）" % _bad)
        _ro.close()
    except Exception as e:      # noqa: BLE001
        check("A11 真库三通道可检查", False, "%s: %s" % (type(e).__name__, str(e)[:100]))

    # ── B claims 自洽 ──
    hr("[B] claims：声称 fact 却无可回查依据 ⇒ 降 to_verify")
    s = _summ([{"source": "graph", "ref": "E-NOT-EXIST", "basis": "verified"}],
              claims=[{"text": "系统满足 12 项需求", "kind": "fact", "subject": "需求覆盖"}])
    ec.enforce(conn, s)
    check("B1 fact + 零条可回查依据 ⇒ 降 to_verify",
          s["claims"][0]["kind"] == "to_verify", "got=%s" % s["claims"][0].get("kind"))
    check("B2 降级原因写明（demoted_reason）", bool(s["claims"][0].get("demoted_reason")))

    s = _summ([{"source": "graph", "ref": "E-P10-001", "basis": "verified"}],
              claims=[{"text": "动力分系统含 3 个部件", "kind": "fact", "subject": "动力分系统"}])
    ec.enforce(conn, s)
    check("B3 fact + 有可回查依据 ⇒ 保持 fact",
          s["claims"][0]["kind"] == "fact", "got=%s" % s["claims"][0].get("kind"))

    s = _summ([], claims=[{"text": "空依据下的 fact", "kind": "fact", "subject": "x"}])
    ec.enforce(conn, s)
    check("B4 连 evidence 都没有的 fact ⇒ 保持不变（本条只判'依据都在但都查不到'）",
          s["claims"][0]["kind"] == "fact",
          "无 evidence 属另一类问题（协议层已按 to_verify 兜底 kind 缺省）")

    # ── C fail-open ──
    hr("[C] fail-open：判不了就不降级（宁可漏判，不可误降）")
    s = _summ([{"source": "graph", "ref": "E-NOT-EXIST", "basis": "verified"}])
    ec.enforce(None, s)
    check("C1 conn=None ⇒ 一律不降级", s["evidence"][0]["basis"] == "verified")

    fd, p2 = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    c2 = sqlite3.connect(p2)          # 空库：三张表都不存在
    try:
        s = _summ([{"source": "graph", "ref": "E-NOT-EXIST", "basis": "verified"}])
        ec.enforce(c2, s)
        check("C2 老库缺表 ⇒ 不降级（查询失败按跳过处理）",
              s["evidence"][0]["basis"] == "verified", "got=%s" % s["evidence"][0]["basis"])
    finally:
        c2.close()
        os.remove(p2)

    # ── D 接线 ──
    hr("[D] 接线：必须在写 summary_json 之前")
    src = open(os.path.join(ROOT, "agent/pipeline_parts/stream.py"), encoding="utf-8").read()
    i_enf = src.find("evidence_check import enforce")
    i_json = src.find("UPDATE agent_tasks SET summary_json=?")
    check("D1 stream.py 调用了 evidence_check.enforce", i_enf > 0)
    check("D2 调用位置在写 summary_json **之前**（否则下游已读到未校验的 verified）",
          0 < i_enf < i_json, "enforce@%d json@%d" % (i_enf, i_json))
    check("D3 调用包在 try 里（校验失败不得阻断子任务）",
          "except Exception" in src[i_enf - 200: i_enf + 600] if i_enf > 0 else False)

    # ── E ★变异 ──
    hr("[E] ★变异：把降级关掉 ⇒ A2/B1 必须判红")
    _orig = ec.resolve_ref
    ec.resolve_ref = lambda *a, **k: (True, "变异：恒命中")
    try:
        s = _summ([{"source": "graph", "ref": "E-NOT-EXIST", "basis": "verified"}],
                  claims=[{"text": "x", "kind": "fact", "subject": "x"}])
        ec.enforce(conn, s)
        check("E1 ★变异：恒命中时 A2 的降级不再发生（证明 A2 不是空转）",
              s["evidence"][0]["basis"] == "verified")
        check("E2 ★变异：恒命中时 B1 的降级也不再发生（证明 B1 不是空转）",
              s["claims"][0]["kind"] == "fact")
    finally:
        ec.resolve_ref = _orig

    # ── F 真库体检 ──
    hr("[F] 真库只读体检：现存 verified 依据的回查率")
    try:
        ro = sqlite3.connect("file:%s?mode=ro" % os.path.join(ROOT, "mbse.db"), uri=True)
        ro.row_factory = sqlite3.Row
        rows = ro.execute("SELECT task_key, summary_json FROM agent_tasks "
                          "WHERE COALESCE(summary_json,'') NOT IN ('','{}')").fetchall()
        n_ev = n_ver = n_unres = 0
        for r in rows:
            try:
                import json as _j
                d = _j.loads(r["summary_json"] or "{}")
            except Exception:
                continue
            for e in (d.get("evidence") or []):
                if not isinstance(e, dict):
                    continue
                n_ev += 1
                if str(e.get("basis") or "") == "verified":
                    n_ver += 1
                    ok, _w = ec.resolve_ref(ro, str(e.get("source") or ""),
                                            str(e.get("ref") or ""))
                    if not ok:
                        n_unres += 1
        print("    真库：%d 条 summary_json，evidence %d 条，其中 verified %d 条、不可回查 %d 条"
              % (len(rows), n_ev, n_ver, n_unres))
        check("F1 真库体检可运行（口径：summary_json 里的 evidence[].basis）", True, "")
        check("F2 若存在不可回查的 verified，说明本机制针对的是真实失效而非臆想"
              if n_unres else "F2 真库当前无不可回查的 verified（历史 summary 很少，属预期）",
              True, "")
        ro.close()
    except Exception as e:      # noqa: BLE001
        check("F1 真库体检可运行", False, "%s: %s" % (type(e).__name__, str(e)[:120]))

    # ── G 不变量 ──
    hr("[G] 不变量")
    check("G1 降级只往更不可靠方向（BASIS_VALUES 顺序 = verified→derived→asserted）",
          sp.BASIS_VALUES.index("verified") < sp.BASIS_VALUES.index("asserted"))
    check("G2 to_verify 在 CLAIM_KINDS 内（降级目标合法，不会写出枚举外的值）",
          "to_verify" in sp.CLAIM_KINDS)
    names = [r[1] for r in _results]
    check("G3 断言名唯一", len(set(names)) == len(names),
          "dups=%s" % [x for x in set(names) if names.count(x) > 1])
finally:
    conn.close()
    os.remove(path)

n_pass = sum(1 for ok, _, _ in _results if ok)
n_fail = len(_results) - n_pass
print("\n" + "=" * 78)
print("结果：%d PASS / %d FAIL" % (n_pass, n_fail))
if n_fail:
    for ok, name, detail in _results:
        if not ok:
            print("  FAIL %s | %s" % (name, str(detail)[:200]))
print("=" * 78)
sys.exit(1 if n_fail else 0)
