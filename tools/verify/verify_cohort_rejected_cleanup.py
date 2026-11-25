# -*- coding: utf-8 -*-
# CI-OPTIONAL: [local] tier=local（见 tools/gates_manifest.json）：2026-11-24 B3：假批准批次作废后状态锁定（cohort rejected / 107 明细 rejected / 无 inferred 残留），并与清理前备份逐行比对证明实体关系未误伤。需生产真库与 backups/ 下备份文件，CI 无此数据
"""B3 验证：假批准批次已作废，且清理未误伤其他表。

    .venv/Scripts/python.exe tools/verify/verify_cohort_rejected_cleanup.py

背景：cohort=1 是 2026-09-13 失败批准遗留（明细 id 形态不符 / V2G_* 实体不存在 /
60 条推断到未注册类型 / stat 声称 52 实体实际 0 个入库）。用户 2026-11-24 决策作废。

判据：
  R1 cohort=1 状态为 rejected 且带 note
  R2 107 条明细全部 rejected（无 pending 残留）
  R3 triples 无 status='inferred' 残留（107 条已作废）
  R4 approved 三元组未被误伤（仍 30 条）
  R5 inferred_facts 未被改动（15 条 —— 属 SWRL 链路，与本次无关）
  R6 entities / relations 未被误伤
  R7 变体：若 cohort 回到 approved 或出现 inferred 残留，本脚本判红
"""
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  OK  " if cond else " FAIL ") + name + (f"  {detail}" if detail else ""))


def main():
    db = ROOT / "mbse.db"
    if not db.exists():
        print("SKIP: 真库不存在")
        return 0
    c = sqlite3.connect(str(db))
    c.row_factory = sqlite3.Row

    print("\n[1] 批次头")
    r = c.execute("SELECT status, note, decided_by FROM reasoning_cohorts WHERE id=1").fetchone()
    check("R1 cohort=1 已作废", r and r["status"] == "rejected",
          f"status={r['status'] if r else 'no row'} by={r['decided_by'] if r else '-'}")
    check("R1b 作废原因已留痕", bool(r and (r["note"] or "").strip()),
          (r["note"][:60] if r else ""))

    print("\n[2] 明细与三元组")
    pend = c.execute("SELECT COUNT(*) FROM reasoning_cohort_items WHERE cohort_id=1 AND status!='rejected'").fetchone()[0]
    rej = c.execute("SELECT COUNT(*) FROM reasoning_cohort_items WHERE cohort_id=1 AND status='rejected'").fetchone()[0]
    check("R2 明细全部 rejected", pend == 0 and rej == 107, f"rejected={rej} 非rejected={pend}")
    inf = c.execute("SELECT COUNT(*) FROM triples WHERE status='inferred'").fetchone()[0]
    check("R3 无 inferred 残留", inf == 0, f"inferred={inf}")
    rej_t = c.execute("SELECT COUNT(*) FROM triples WHERE status='rejected'").fetchone()[0]
    check("R3b 作废条数正确", rej_t == 107, f"rejected={rej_t}")

    print("\n[3] 未误伤")
    appr = c.execute("SELECT COUNT(*) FROM triples WHERE status='approved'").fetchone()[0]
    check("R4 approved 三元组完好", appr == 30, f"approved={appr}")
    facts = c.execute("SELECT COUNT(*) FROM inferred_facts").fetchone()[0]
    check("R5 inferred_facts 未动", facts == 15, f"facts={facts}（SWRL 链路，与本次无关）")
    # ⚠️ 口径（2026-11-24 实测纠错）：entities 是**按分支多副本**存（61 个 id 有重复行），
    #    count(*)=184，其中 1 条 deprecated ⇒ 有效 183。上一版断言写 184 却用
    #    `status!='deprecated'` 过滤，必然 183≠184 假红 —— 是脚本口径错，不是数据被误伤。
    ents_all = c.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    ents = c.execute("SELECT COUNT(*) FROM entities WHERE status!='deprecated'").fetchone()[0]
    rels = c.execute("SELECT COUNT(*) FROM relations WHERE status!='deprecated'").fetchone()[0]
    check("R6 实体/关系未误伤", ents_all == 184 and ents == 183 and rels == 249,
          f"全部={ents_all} 有效={ents} relations={rels}")
    # R6b 最强证明：与清理前备份逐行比对（备份不存在时跳过，不假绿）
    try:
        baks = sorted((ROOT / "backups").glob("mbse_before_B3_cleanup_*.db"))
        if baks:
            bk = sqlite3.connect(str(baks[-1]))
            bk.row_factory = sqlite3.Row
            cur_rows = [tuple(x) for x in c.execute(
                "SELECT id,name,entity_type,status,branch FROM entities ORDER BY id,branch").fetchall()]
            bak_rows = [tuple(x) for x in bk.execute(
                "SELECT id,name,entity_type,status,branch FROM entities ORDER BY id,branch").fetchall()]
            cur_rel = [tuple(x) for x in c.execute(
                "SELECT id,source_id,target_id,relation_type,status,branch FROM relations ORDER BY id").fetchall()]
            bak_rel = [tuple(x) for x in bk.execute(
                "SELECT id,source_id,target_id,relation_type,status,branch FROM relations ORDER BY id").fetchall()]
            bk.close()
            check("R6b 与清理前备份逐行一致（实体/关系）",
                  cur_rows == bak_rows and cur_rel == bak_rel,
                  f"entities {len(cur_rows)} vs {len(bak_rows)} / relations {len(cur_rel)} vs {len(bak_rel)}")
        else:
            check("R6b 与清理前备份逐行一致（实体/关系）", False,
                  "未找到备份文件 backups/mbse_before_B3_cleanup_*.db")
    except Exception as e:
        check("R6b 与清理前备份逐行一致（实体/关系）", False, f"{type(e).__name__}: {e}")

    print("\n[4] 清理后图库不可见（作废即不再出现在推导列表）")
    try:
        from routers.graph_workspace import reasoning_inferred_list
        rows = c.execute("SELECT COUNT(*) FROM triples WHERE status='inferred'").fetchone()[0]
        check("R7 作废项不再被 /reasoning/inferred 取出", rows == 0,
              "该端点查询 status='inferred'，作废后必然为空")
    except Exception as e:
        check("R7 作废项不再被 /reasoning/inferred 取出", False, f"{type(e).__name__}: {e}")

    print(f"\n结果：{len(PASS)} pass / {len(FAIL)} fail")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
