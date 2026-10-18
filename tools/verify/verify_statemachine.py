# -*- coding: utf-8 -*-
"""#18 门禁：统一状态机（core/statemachine.py + 已接线实体的读写路径）。

**背景（2026-10-18《自进化 AI 平台调研评估》路线图第 18 项）**：全仓状态字面量
曾散落在各 repo/路由的常量与 if 守卫里（MR 一套、bad_case 一套、V2G 一套……），
同一套「候选→确认→驳回」语义被反复重写且互不知晓 —— 进化流水线（P1-3 配置提案 /
P1-4 沙箱 A/B）无从建模。本门禁守护：

  A. 纯函数矩阵：每台机的 normalize（legacy 归一）/targets/actions/check/check_action
     —— 合法迁移放行、非法迁移必拒，且错误文案可直接回给调用方；
  B. 注册表自检：validate_registry（initial/边端点/重复动作/legacy/terminal/键唯一）
     +  wired/template 标记纪律（wired 的实体必须在读写路径过机检）；
  C. 干净库端到端：MR 全生命周期（draft→open→merged；merged 二次改写拒；reject
     无意见拒；closed→reopen→再 reject；legacy pending 行的可编辑态归一）+
     badcase triage（合法流转/原地流转拒/非法状态拒/复现重开）+ 库存脏状态不被
     静默覆盖（plan 与 apply 同源）；
  D. 真库只读勘察：8 表 9 列状态取值 ⊆ 对应机 states（MR 允许 legacy 归一）——
     注册表的声明覆盖有真实数据佐证，不是纸上机器；
  E. 接线断言：branch_repo/branches/badcase 消费注册表且**无重复 inline 状态表**
     （那正是 #18 要收口的问题）+ 运行期 import 自证。

双环境口径：干净库自建 MBSE_DB_PATH 临时空库（import 前切库）；真库段只读。
"""
import os
import json
import shutil
import sqlite3
import sys


def _find_root():
    _d = os.path.dirname(os.path.abspath(__file__))
    for _ in range(6):
        if os.path.isfile(os.path.join(_d, "memory_service.py")):
            return _d
        _p = os.path.dirname(_d)
        if _p == _d:
            break
        _d = _p
    raise SystemExit("找不到仓库根（未找到 memory_service.py）")


ROOT = _find_root()
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# ── 干净库：先切库后 import（core.config/DB_PATH import 期绑定，顺序即正确性）──
_TMP = os.path.join(ROOT, "tmp", "verify_statemachine")
os.makedirs(_TMP, exist_ok=True)
_CLEAN_DB = os.path.join(_TMP, "clean.db")
if os.path.exists(_CLEAN_DB):
    os.remove(_CLEAN_DB)
os.environ["MBSE_DB_PATH"] = _CLEAN_DB

_results = []


def check(name, ok, detail=""):
    _results.append((name, bool(ok), detail))
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name,
                           ("  ← " + str(detail)) if (detail and not ok) else ""))
    return bool(ok)


def skip(name, why):
    _results.append((name, None, why))
    print("  [SKIP] " + name + "  -- " + str(why))


def _seed_llm(conn, conv, msg="", days_ago=0):
    """按可控时间播种判据面（llm_usage_stats，bad_case 检测器的上游）。"""
    conn.execute(
        "INSERT INTO llm_usage_stats (provider_name, model_name, used_mock, call_failed, "
        "failure_kind, failure_msg, finish_reason, conversation_id, run_id, created_at) "
        "VALUES ('p-seed','m-seed',0,1,'quota',?, 'stop', ?, 0, datetime('now', ?))",
        (msg, conv, "-%d day" % days_ago))


def main():
    # ── A. 纯函数矩阵 ───────────────────────────────────────────────
    print("== A. 纯函数矩阵（normalize/targets/check/check_action 逐台机）==")
    from core import statemachine as SM

    check("A1 注册表 10 台（9 实体 + 1 模板机）", len(SM.MACHINES) == 10,
          f"got={sorted(SM.MACHINES)}")
    check("A2 entities() 模板机排在末尾（阅读顺序：实体在前）",
          SM.entities()[-1] == "evolution_pipeline"
          and "evolution_pipeline" not in SM.entities()[:-1],
          f"got={SM.entities()}")

    mr = SM.get("merge_request")
    check("A3 MR legacy 归一：pending→open / approved→merged / rejected→closed",
          mr.normalize("pending") == "open" and mr.normalize("approved") == "merged"
          and mr.normalize("rejected") == "closed" and mr.normalize(None) == ""
          and mr.normalize("merged") == "merged",
          f"got={[mr.normalize(x) for x in ('pending','approved','rejected',None,'merged')]}")
    check("A4 MR targets：draft→{open,closed} / open→{merged,closed} / closed→{open} / merged→∅",
          mr.targets("draft") == ("open", "closed") and mr.targets("open") == ("merged", "closed")
          and mr.targets("closed") == ("open",) and mr.targets("merged") == (),
          f"got={ {s: mr.targets(s) for s in mr.states} }")
    ok, to, err = mr.check_action("open", "approve")
    check("A5 check_action：open.approve→merged", ok and to == "merged" and not err, f"got={(ok,to,err)}")
    ok2, _, err2 = mr.check_action("merged", "approve")
    check("A6 check_action：merged.approve 拒（终态无出边，文案含可执行动作提示）",
          not ok2 and "终态" in err2, f"got={(ok2,err2)}")
    ok3, _, err3 = mr.check_action("closed", "reject")
    check("A7 check_action：closed.reject 拒（reopen 才是 closed 的唯一动作）",
          not ok3 and "reopen" in err3, f"got={(ok3,err3)}")
    ok4, err4 = mr.check("draft", "merged")
    check("A8 check：draft→merged 拒（跳过 open 直接合并不允许）",
          not ok4 and "不允许迁移" in err4, f"got={(ok4,err4)}")
    ok5, err5 = mr.check("weird", "open")
    check("A9 check：未知当前状态拒（frm ∉ states，含合法状态域清单）",
          not ok5 and "当前状态非法" in err5 and "draft" in err5, f"got={(ok5,err5)}")
    check("A10 actions()：draft 两动作、reject 标注 note_required（无意见驳回是治理黑洞）",
          [a["action"] for a in mr.actions("draft")] == ["open", "reject"]
          and mr.actions("draft")[1]["note_required"] is True
          and mr.actions("open")[0]["note_required"] is False,
          f"got={mr.actions('draft')}")

    bc = SM.get("bad_case")
    check("A11 bad_case 三态 + reopen 边（resolved/ignored 均可回 open）",
          bc.states == ("open", "resolved", "ignored")
          and bc.targets("resolved") == ("open",) and bc.targets("ignored") == ("open",)
          and bc.targets("open") == ("resolved", "ignored"),
          f"got={ {s: bc.targets(s) for s in bc.states} }")
    ok6, err6 = bc.check("resolved", "resolved")
    check("A12 bad_case 原地流转拒（resolved→resolved 无自环边）",
          not ok6 and "不允许迁移" in err6, f"got={(ok6,err6)}")

    ep = SM.evolution_pipeline()
    check("A13 进化流水线模板机：candidate→experiment→promoted→rolled_back",
          ep.template is True and ep.check("candidate", "experiment")[0]
          and ep.check("experiment", "promoted")[0] and ep.check("promoted", "rolled_back")[0],
          f"got={ep.states}")
    check("A14 模板机 abort 边（#19 取消链路：experiment→candidate）",
          ep.check_action("experiment", "abort")[1] == "candidate"
          and not ep.check("candidate", "promoted")[0],
          f"got={ep.check_action('experiment','abort')}")

    v2g_m = SM.get("v2g_match")
    check("A15 v2g_match 四态 + 质量闸回滚边（merged→pending）",
          v2g_m.states == ("pending", "confirmed", "rejected", "merged")
          and v2g_m.check_action("merged", "requeue")[1] == "pending"
          and v2g_m.check("confirmed", "merged")[0]
          and not v2g_m.check("rejected", "merged")[0],
          f"got={ {s: v2g_m.targets(s) for s in v2g_m.states} }")

    # ── B. 注册表自检与标记纪律 ─────────────────────────────────────
    print("\n== B. 注册表自检（validate_registry + wired/template 标记）==")
    errs = SM.validate_registry()
    check("B1 validate_registry 零错误（initial/边端点/重复动作/legacy/terminal）",
          errs == [], f"got={errs}")
    wired = sorted(e for e, m in SM.MACHINES.items() if m.wired)
    check("B2 wired 恰好两台（merge_request/bad_case —— 读写路径已过机检）",
          wired == ["bad_case", "merge_request"], f"got={wired}")
    tmpl = [e for e, m in SM.MACHINES.items() if m.template]
    check("B3 template 恰好一台（evolution_pipeline，不对应具体表）",
          tmpl == ["evolution_pipeline"], f"got={tmpl}")
    check("B4 未接线实体全部 wired=False（先声明覆盖，强校验随各域节奏）",
          all(not m.wired for e, m in SM.MACHINES.items()
              if e not in ("merge_request", "bad_case")))
    check("B5 每台机 desc 非空（声明即文档，后人读表可知语义）",
          all(m.desc.strip() for m in SM.MACHINES.values()))

    # ── C. 干净库端到端 ─────────────────────────────────────────────
    print("\n== C. 干净库端到端（MR 全生命周期 + badcase triage + 脏状态冻结）==")
    from database import init_db, get_db
    init_db()
    conn = get_db()
    conn.row_factory = sqlite3.Row
    from repositories.branch_repo import BranchRepo
    repo = BranchRepo(conn)

    # init_db 已预置 dev/release 系统分支；这里只补一个个人分支作合并源
    conn.execute("INSERT INTO branches (name, branch_type, parent_branch) "
                 "VALUES ('feat-sm','personal','dev')")
    conn.commit()

    # C1 MR 主链：draft → open → merged
    mr1 = repo.create_merge_request('feat-sm', 'dev', '[]', draft=True, actor='门禁')
    mid1 = mr1["id"]
    st = conn.execute("SELECT status FROM merge_requests WHERE id=?", (mid1,)).fetchone()["status"]
    check("C1a create draft=True ⇒ status=draft", mr1.get("ok") and st == "draft", f"got={st}")
    r = repo.open_merge_request(mid1, actor='门禁')
    st = conn.execute("SELECT status FROM merge_requests WHERE id=?", (mid1,)).fetchone()["status"]
    check("C1b open_merge_request：draft→open 过机检", r.get("ok") and st == "open", f"got={r}")
    r = repo.resolve_merge(mid1, 'merged', actor='门禁')
    st = conn.execute("SELECT status FROM merge_requests WHERE id=?", (mid1,)).fetchone()["status"]
    check("C1c resolve_merge：open→merged 过机检", r.get("ok") and st == "merged", f"got={r}")
    # C1d ★repo 层自卫：merged 上任何二次 resolve 都必须拒（此前只靠路由层偶然守卫）
    r_again = repo.resolve_merge(mid1, 'merged', actor='门禁')
    check("C1d ★merged 上二次 resolve 拒（repo 层机检自卫，错误含状态机文案）",
          r_again.get("ok") is False and "不允许迁移" in (r_again.get("error") or ""),
          f"got={r_again}")
    r_open = repo.open_merge_request(mid1)
    check("C1e merged 上 open/reopen 拒（终态无出边）",
          r_open.get("ok") is False and "终态" in (r_open.get("error") or ""), f"got={r_open}")
    r_open = repo.open_merge_request(mid1)
    check("C1e merged 上 open/reopen 拒（终态无出边）",
          r_open.get("ok") is False and "终态" in (r_open.get("error") or ""), f"got={r_open}")

    # C1f-g 第二枚 MR：先验「驳回无意见拒」，再走 reject → closed → reopen → 再 reject
    mr2 = repo.create_merge_request('feat-sm', 'dev', '[]', actor='门禁')
    mid2 = mr2["id"]
    st2 = conn.execute("SELECT status FROM merge_requests WHERE id=?", (mid2,)).fetchone()["status"]
    check("C1f create 默认 open", st2 == "open", f"got={st2}")
    r_nn = repo.resolve_merge(mid2, 'closed')
    check("C1g 驳回无意见拒（≥5 字门在机检之后仍生效）",
          r_nn.get("ok") is False and "意见" in (r_nn.get("error") or ""), f"got={r_nn}")
    r = repo.resolve_merge(mid2, 'closed', actor='门禁', review_note='门禁验证：驳回必须带意见')
    check("C1h reject 带意见 ⇒ closed", r.get("ok"), f"got={r}")
    r = repo.reopen_merge_request(mid2, actor='门禁')
    st2 = conn.execute("SELECT status FROM merge_requests WHERE id=?", (mid2,)).fetchone()["status"]
    check("C1i reopen：closed→open 过机检", r.get("ok") and st2 == "open", f"got={r}")
    r = repo.resolve_merge(mid2, 'closed', actor='门禁', review_note='门禁验证：再次驳回')
    check("C1j reopen 后可再次 reject（闭环可重复，非一次性边）",
          r.get("ok"), f"got={r}")

    # C1k-l 可编辑态归一：legacy pending（=旧 open）应视为可编辑；merged 不可
    # （冲突清单校验在路由层；repo 层对合法态幂等写 resolution —— 这里验的正是
    #  「可编辑态门是否放行 legacy 行」：没有归一化 wiring 时 'pending' 会被误拒）
    conn.execute("INSERT INTO merge_requests (source_branch, target_branch, status) "
                 "VALUES ('feat-sm','dev','pending')")
    mid3 = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    r = repo.resolve_conflict(mid3, 'no-such', 'f', 'source')
    res3 = json.loads(conn.execute(
        "SELECT resolutions FROM merge_requests WHERE id=?", (mid3,)).fetchone()["resolutions"] or "{}")
    check("C1k legacy pending 行过可编辑态门（归一化为 open ⇒ 落 resolution）",
          r.get("ok") is True and "no-such" in res3 and "f" in res3["no-such"], f"got={r}")
    r = repo.resolve_conflict(mid1, 'no-such', 'f', 'source')
    check("C1l merged 行拒绝解决冲突（可编辑态门命中）",
          r.get("ok") is False and "已处理" in (r.get("error") or ""), f"got={r}")

    # C2 badcase triage 全矩阵
    _seed_llm(conn, 9101, msg="状态机门禁种子", days_ago=1)
    conn.commit()
    from core import badcase as BC
    rec = BC.collect_bad_cases(conn, apply=True)
    row = conn.execute("SELECT * FROM bad_cases WHERE subject='conv:9101'").fetchone()
    check("C2a collect apply ⇒ 台账 1 行 open（triage 的被操作对象）",
          row is not None and row["status"] == "open", f"apply={rec.get('apply_result')}")
    cid = row["id"]
    t = BC.triage_bad_case(conn, cid, "resolved", actor="门禁")
    check("C2b triage open→resolved 过机检", t.get("ok") is True, f"got={t}")
    t = BC.triage_bad_case(conn, cid, "resolved", actor="门禁")
    check("C2c ★triage 原地流转拒（resolved→resolved 无自环边）",
          t.get("ok") is False and "不允许迁移" in (t.get("error") or ""), f"got={t}")
    t = BC.triage_bad_case(conn, cid, "open", actor="门禁")
    check("C2d triage resolved→open（reopen 边）过机检", t.get("ok") is True, f"got={t}")
    t = BC.triage_bad_case(conn, cid, "ignored", actor="门禁")
    check("C2e triage open→ignored 过机检", t.get("ok") is True, f"got={t}")
    t = BC.triage_bad_case(conn, cid, "resolved", actor="门禁")
    check("C2f ★triage ignored→resolved 拒（跨终态流转必须经 reopen）",
          t.get("ok") is False and "不允许迁移" in (t.get("error") or ""), f"got={t}")
    t = BC.triage_bad_case(conn, cid, "done", actor="门禁")
    check("C2g 非法状态拒（成员校验先于一切）",
          t.get("ok") is False and "非法状态" in (t.get("error") or ""), f"got={t}")

    # C3 ★库存脏状态：plan 与 apply 同源，不静默覆盖、不连坐
    conn.execute("UPDATE bad_cases SET status='done' WHERE id=?", (cid,))
    conn.commit()
    _seed_llm(conn, 9101, msg="脏状态后的复现", days_ago=0)
    conn.commit()
    rec2 = BC.collect_bad_cases(conn, apply=True)
    row2 = conn.execute("SELECT * FROM bad_cases WHERE id=?", (cid,)).fetchone()
    check("C3a ★脏 status='done'：plan 判 recur（与 apply 同源，不判 reopen）",
          rec2["plan"]["recur"] == 1 and rec2["plan"]["reopen"] == 0, f"plan={rec2['plan']}")
    check("C3b ★脏状态不被静默覆盖：status 仍 'done'，occurrences 正常累加",
          row2["status"] == "done" and row2["occurrences"] == row["occurrences"] + 1,
          f"status={row2['status']} occ={row2['occurrences']} (was {row['occurrences']})")
    t = BC.triage_bad_case(conn, cid, "resolved", actor="门禁")
    check("C3c ★脏状态行被机检冻结（triage 同样拒，直至人工修库）",
          t.get("ok") is False and "当前状态非法" in (t.get("error") or ""), f"got={t}")
    conn.close()

    # ── D. 真库只读勘察（注册表声明覆盖的真实数据佐证）──────────────
    print("\n== D. 真库只读勘察（8 表 9 列状态取值 ⊆ 对应机 states）==")
    _prod = os.path.join(ROOT, "mbse.db")
    if not os.path.exists(_prod):
        skip("D1 真库勘察", "mbse.db 不存在（CI 环境）")
    else:
        pc = sqlite3.connect("file:%s?mode=ro" % _prod, uri=True)
        pc.row_factory = sqlite3.Row
        # 机器 → (表, 状态列)；v2g 同行两条生命周期都勘察
        recon = [
            ("merge_request", "merge_requests", "status"),
            ("bad_case", "bad_cases", "status"),
            ("v2g_candidate", "v2g_candidates", "review_status"),
            ("v2g_match", "v2g_candidates", "status"),
            ("generation_history", "generation_history", "status"),
            ("hil_confirmation", "hil_confirmations", "status"),
            ("document_lifecycle", "documents", "lifecycle_status"),
            ("flow_run", "flow_runs", "status"),
            ("intent_sample", "intent_samples", "status"),
        ]
        covered, total_rows = 0, 0
        for ent, table, col in recon:
            m = SM.get(ent)
            try:
                dist = {r[0]: r[1] for r in pc.execute(
                    "SELECT %s, COUNT(*) FROM %s GROUP BY %s" % (col, table, col))}
            except sqlite3.OperationalError as e:
                skip("D %s.%s 勘察" % (table, col), "表/列不存在：%s" % e)
                continue
            total_rows += sum(dist.values())
            bad = {v: n for v, n in dist.items() if m.normalize(v) not in m.states}
            covered += 1
            check("D %s.%s ∈ %s 状态域%s"
                  % (table, col, ent,
                     "（legacy 归一放行）" if m.legacy else ""),
                  not bad, "越界值 %s；实测分布 %s" % (bad, dist))
            print("    %s.%s 分布 %s" % (table, col, dist))
        check("D 全覆盖：9 列全部勘察（真库行数 %d）" % total_rows, covered == 9,
              f"covered={covered}")
        pc.close()

    # ── E. 接线完整性（消费注册表 + 无重复 inline 状态表）─────────────
    print("\n== E. 接线完整性（branch_repo/branches/badcase + 运行期 import）==")
    _br = open(os.path.join(ROOT, "repositories", "branch_repo.py"), encoding="utf-8").read()
    check("E1 branch_repo 从注册表取机（_sm_get）且 MR 别名字面量有唯一出处注释",
          'from core.statemachine import get as _sm_get' in _br
          and '_MR = _sm_get("merge_request")' in _br)
    check("E2 branch_repo 三处迁移守卫走 _MR.check（open/reopen/resolve）",
          _br.count("_MR.check(") == 3, f"count={_br.count('_MR.check(')}")
    check("E3 branch_repo 无 inline 状态表残留（!= \"open\" / not in (\"draft\", \"open\") 绝迹）",
          '!= "open"' not in _br and 'not in ("draft", "open")' not in _br)
    check("E4 resolve_conflict 可编辑态走归一化 + 机状态别名（兼容存量而非硬编码）",
          "_norm_mr_status(mr.get(\"status\")) not in (MR_DRAFT, MR_OPEN)" in _br)

    _bp = open(os.path.join(ROOT, "routers", "branches.py"), encoding="utf-8").read()
    check("E5 branches resolve 端点守卫走 _MR.check_action（行为等价、文案同源）",
          "_MR.check_action(mr[\"status\"], action)" in _bp)
    check("E6 branches 无 inline 状态表残留（旧两处 if 守卫已删）",
          '!= "open"' not in _bp and 'not in ("draft", "open")' not in _bp)

    _bcp = open(os.path.join(ROOT, "core", "badcase.py"), encoding="utf-8").read()
    check("E7 badcase triage 走 _BC.check（成员校验 + 迁移边校验）",
          "_BC.check(row[\"status\"] or \"open\", status)" in _bcp
          and "status not in _BC.states" in _bcp)
    check("E8 badcase 无 inline 三态元组残留（旧 (\"open\",\"resolved\",\"ignored\") 绝迹）",
          '("open", "resolved", "ignored")' not in _bcp)
    check("E9 apply 重开判定与 plan 分类同源（normalize+targets 各出现 ≥2 次）",
          _bcp.count("_BC.normalize(") >= 2 and _bcp.count("_BC.targets(") >= 2,
          f"normalize={_bcp.count('_BC.normalize(')} targets={_bcp.count('_BC.targets(')}")

    _mon = open(os.path.join(ROOT, "routers", "monitor.py"), encoding="utf-8").read()
    check("E10 monitor triage 端点仍委托 core.badcase（机检在核心层，路由零重复）",
          "badcase.triage_bad_case" in _mon)

    # 运行期 import 自证：接线没有 import 期炸弹（core.config 已绑干净库）
    import importlib
    import routers.branches as _rb
    importlib.reload(_rb)
    check("E11 运行期：routers.branches._MR 即注册表 merge_request 机",
          _rb._MR is SM.get("merge_request") and _rb._MR.entity == "merge_request")
    import core.badcase as _bcmod
    check("E12 运行期：core.badcase._BC 即注册表 bad_case 机",
          _bcmod._BC is SM.get("bad_case") and _bcmod._BC.entity == "bad_case")

    print("\n结果: %d PASS / %d FAIL / %d SKIP"
          % (sum(1 for r in _results if r[1] is True),
             sum(1 for r in _results if r[1] is False),
             sum(1 for r in _results if r[1] is None)))
    _failed = [r[0] for r in _results if r[1] is False]
    if _failed:
        print("失败项: " + "; ".join(_failed))
    shutil.rmtree(_TMP, ignore_errors=True)
    if _failed:
        sys.exit(1)
    print("全部通过 —— 统一状态机（纯函数矩阵 + 注册表自检 + MR/badcase 全链路过机检 + "
          "真库 9 列状态域勘察 + 接线无 inline 残留）。")


if __name__ == "__main__":
    main()
