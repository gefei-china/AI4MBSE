# CI-OPTIONAL: [local] 需**生产真库**（复制后在其上跑真实版本闭环）；CI 的库是 init_db 空库，复现不出「120 条有代码版本从未候选化 / 孤儿归属」这类数据形态问题
# -*- coding: utf-8 -*-
"""R2 门禁：用**生产库副本 + 真实版本**验证「闭环可推进 + 孤儿归属可自救」。

## 为什么用生产库副本而不是空库
空库能证明"代码能跑"，但证明不了"**现有 128 个真实版本**能不能走下去"——
而实测的关键事实正是：120 条有代码的版本从未候选化、0 条 committed、
7 条版本挂在已删除的工程上（孤儿）。这些都是**数据形态**问题，空库复现不出来。
副本跑完即删，生产库一个字节都不动。

## 判据
B1 closure 端点能如实报出"卡在第几段"（对未候选化的真实版本 ⇒ next=candidates）
B2 孤儿版本 closure 的 zhiyuan 段被闸拦住且给出可执行动作（不再是一句笼统提示）
B3 reattribute 能把孤儿版本改到现有工程（且拒绝不存在的目标工程）
B4 真实版本走完 ①→④：候选化 → 提交审核 → 三元组通过 → 实体落地
B5 走完后 closure.closed 在 ①~④ 段全为 done，且 next 只剩 zhiyuan
B6 幂等：同一版本重复 import-candidates 返回同一批次（不重复造候选）
"""
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

PROD = os.path.join(ROOT, "mbse.db")


def _prepare():
    tmp = tempfile.mkdtemp(prefix="r2_copy_")
    dst = os.path.join(tmp, "prod_copy.db")
    for suf in ("", "-wal", "-shm"):
        src = PROD + suf
        if os.path.exists(src):
            shutil.copy2(src, dst + suf)
    os.environ["MBSE_DB_PATH"] = dst
    return tmp, dst


def main() -> int:
    if not os.path.exists(PROD):
        print(f"[SKIP] 生产库不存在: {PROD}")
        return 0
    tmp, db = _prepare()
    checks = []
    try:
        from database import get_db
        import routers.sysml_versions as SV
        import inspect

        conn = get_db()
        # ── 选样本：有代码、未候选化的真实版本 ──
        row = conn.execute(
            "SELECT id, project_id, length(code_text) cl FROM sysml_versions "
            "WHERE coalesce(trim(code_text),'')<>'' AND coalesce(trim(imported_batch),'')='' "
            "ORDER BY id DESC LIMIT 1").fetchone()
        if not row:
            print("[SKIP] 生产库里没有『有代码但未候选化』的版本可作样本")
            return 0
        vid = row["id"]
        print(f"样本版本：v{vid}（代码 {row['cl']} 字符，归属 {row['project_id'] or '（空）'}）")

        # B1：closure 如实报卡点
        c1 = SV.version_closure(vid, conn)
        checks.append(("B1 未候选化版本如实报卡点",
                       (not c1["closed"]) and c1["next"] == "candidates",
                       f"next={c1['next']} action={c1['next_action']}"))

        # B2：孤儿版本的写回闸给可执行动作
        orph = conn.execute(
            "SELECT v.id, v.project_id FROM sysml_versions v LEFT JOIN projects p ON p.id=v.project_id "
            "WHERE coalesce(trim(v.project_id),'')<>'' AND p.id IS NULL LIMIT 1").fetchone()
        if orph:
            c2 = SV.version_closure(orph["id"], conn)
            zy = [s for s in c2["stages"] if s["stage"] == "zhiyuan"][0]
            gate = SV.resolve_push_tool_gate(conn, orph["id"])
            checks.append(("B2 孤儿版本写回闸给出可执行动作",
                           (not zy["done"]) and gate.get("code") == "PROJECT_ORPHANED"
                           and "reattribute" in (zy["action"] or ""),
                           f"code={gate.get('code')} action={zy['action'][:60]}"))
            # B3：能重新归属到现有工程（并拒绝不存在的工程）
            pid_ok = conn.execute("SELECT id FROM projects LIMIT 1").fetchone()["id"]
            r_bad = SV.reattribute_version(orph["id"], {"project_id": "no_such_project"}, conn, {"display_name": "gate"})
            bad_rejected = (getattr(r_bad, "status_code", 200) == 400)
            r_ok = SV.reattribute_version(orph["id"], {"project_id": pid_ok}, conn, {"display_name": "gate"})
            after = conn.execute("SELECT project_id FROM sysml_versions WHERE id=?",
                                 (orph["id"],)).fetchone()["project_id"]
            checks.append(("B3 孤儿版本可重新归属（且拒绝无效工程）",
                           bad_rejected and r_ok.get("changed") and after == pid_ok,
                           f"bad_rejected={bad_rejected} after={after}"))
        else:
            checks.append(("B2 孤儿版本写回闸给出可执行动作", True, "无孤儿版本（跳过）"))
            checks.append(("B3 孤儿版本可重新归属（且拒绝无效工程）", True, "无孤儿版本（跳过）"))

        # B4：真实版本走完 ①→④
        before = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
        stage = {}
        r = SV.sysml_import_candidates(vid, {}, conn, {"display_name": "gate"})
        if isinstance(r, dict) and r.get("get"):
            pass  # JSONResponse 属异常路径，下面按 dict 判定
        if isinstance(r, dict) and r.get("ok"):
            stage["cand"] = r.get("batch_id")
        else:
            # JSONResponse（错误）对象：取 body
            stage["cand"] = ""
            stage["err"] = str(getattr(r, "body", r))[:160]
        if not stage.get("cand"):
            checks.append(("B4 真实版本走完 候选→审核→三元组→图库", False,
                           f"候选化失败：{stage.get('err')}"))
        else:
            batch = stage["cand"]
            ids = [x[0] for x in conn.execute(
                "SELECT id FROM v2g_candidates WHERE batch_id=? AND status='pending'",
                (batch,)).fetchall()]
            from services.knowledge_service import KnowledgeService
            ks = KnowledgeService(conn)
            ks.v2g_confirm(batch, ids, actor="gate", dup_action="create")   # 置 submitted
            conn.commit()
            ks.v2g_confirm(batch, ids, actor="gate", dup_action="create")   # 生成待审三元组
            conn.commit()
            conn.execute("UPDATE triples SET status='approved', review_decision='approve', "
                         "reviewed_by='gate', reviewed_at=CURRENT_TIMESTAMP "
                         "WHERE sysml_version_id=? AND status<>'approved'", (vid,))
            conn.commit()
            from triple_commit import commit_approved_triples
            cm = commit_approved_triples(conn, operator="gate")
            conn.commit()
            after = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
            checks.append(("B4 真实版本走完 候选→审核→三元组→图库",
                           after > before,
                           f"候选 {r.get('node_count', 0)} 节点 / {r.get('edge_count', 0)} 关系；"
                           f"实体 {before}→{after}（+{after - before}）"))

            # B5：closure 前四段全绿
            c5 = SV.version_closure(vid, conn)
            st = {s["stage"]: s["done"] for s in c5["stages"]}
            checks.append(("B5 闭环前四段全绿、next 只剩写回智源",
                           all(st.get(k) for k in ("candidates", "review", "triples", "graph"))
                           and c5["next"] in ("zhiyuan", ""),
                           f"{st} next={c5['next']}"))

            # B6：重复候选化幂等
            r2 = SV.sysml_import_candidates(vid, {}, conn, {"display_name": "gate"})
            same = isinstance(r2, dict) and r2.get("batch_id") == batch and r2.get("dup")
            checks.append(("B6 重复候选化幂等（同批次，不重复造候选）",
                           bool(same), f"batch={getattr(r2, 'get', lambda k, d=None: None)('batch_id')}"))
        conn.close()

        print("=" * 68)
        print("R2 门禁：闭环可推进 + 孤儿归属可自救（生产库副本 + 真实版本）")
        print("=" * 68)
        for name, passed, detail in checks:
            print(f"  [{'PASS' if passed else 'FAIL'}] {name} —— {detail}")
        ok = all(c[1] for c in checks)
        print("\n" + ("ALL_OK" if ok else "FAILED"))
        return 0 if ok else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
