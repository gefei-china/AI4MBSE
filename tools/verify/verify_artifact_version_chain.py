# CI-OPTIONAL: [local] tier=local（见 tools/gates_manifest.json）：需真库（读 artifacts/sysml_versions 既有数据做命名与版本判据），CI 干净库上无这些数据；本机已实测通过。改法须同步改清单 tier 并接线，不允许只删这行
# -*- coding: utf-8 -*-
"""产物**版本链按产物主体分组** + **命名按主体锁定** 的判据（2026-11-13）。

## 锁死的是什么缺陷（真机 conv 990727 实证）
原实现两处都**按会话**而不是**按产物**：

| 位置 | 原实现 | 后果 |
|---|---|---|
| `_archive_sysml_version` 版本计数 | `COUNT(*) WHERE conversation_id=?` | v0.1=需求视图、v0.2=用例视图，**两个不同产物串成一条修订链**，`parent_id` 还互指 |
| `_resolve_code_artifact_title` 命名锁定 | `ORDER BY id LIMIT 1` 取会话第一条 | 用例视图被命名为「串联混动汽车运行需求 · 需求视图… · v0.2」——**名字与内容不符** |

修法统一到「产物主体 subject」：`derive_subject`（package 名 → 主定义名 → 实例名）。
同 subject ⇒ 版本 +1、parent 串联、复用锁定名；不同 subject ⇒ 各自从 v0.1 起、独立命名。

## 判据（全部可复算，落在函数返回值上）
  G1 两个不同 package 的代码 ⇒ 各自 v0.1（不是 v0.1 / v0.2）
  G2 同一 package 的第二次 ⇒ v0.2 且 parent 指向该 subject 的上一版
  G3 parent 只在同 subject 内串联（不同 subject 的 parent 互不指向）
  G4 旧版收敛（superseded）**只影响同 subject**，不把别的产物 current 作废
  G5 命名：不同 subject ⇒ 不同 title；同 subject 第二次 ⇒ 复用首版 name_base
  G6 判定非空转：ssubject 必须真的被写入（DB 回读）

用法： ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_artifact_version_chain.py
落库到专用 conversation_id（负数/高位），退出前清理，不污染真实会话。
"""
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)
os.chdir(REPO)

PASS, FAIL = 0, 0
ROWS = []


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] %s" % label)
    else:
        FAIL += 1
        print("  [FAIL] %s | %s" % (label, str(detail)[:260]))
    ROWS.append({"label": label, "ok": bool(cond), "detail": str(detail)[:300]})


CODE_A = """package '串联混动汽车运行需求' {
    doc /* 运行需求视图 */
    requirement Req01 { doc /* 整车应能行驶 */ }
}
"""
CODE_A2 = """package '串联混动汽车运行需求' {
    doc /* 运行需求视图 v2 */
    requirement Req01 { doc /* 整车应能行驶（修订） */ }
    requirement Req02 { doc /* 应支持制动能量回收 */ }
}
"""
CODE_B = """package '串联混动汽车用例' {
    doc /* 顶层用例视图 */
    use case Drive { doc /* 行驶 */ }
}
"""


def main():
    from database import get_db
    from agent.utils import _archive_sysml_version, _resolve_code_artifact_title

    CID = 999999001          # 专用验证会话 id（不撞真实会话）
    print("=" * 74)
    print("产物版本链分组 + 命名锁定 判据（conv=%d）" % CID)
    print("=" * 74)

    c = get_db()
    try:
        # 清场（幂等可重跑）
        c.execute("DELETE FROM sysml_versions WHERE conversation_id=?", (CID,))
        c.execute("DELETE FROM artifacts WHERE conversation_id=?", (CID,))
        # ⚠️ artifacts.conversation_id 有外键 → 必须先存在会话行，否则 create_artifact
        #    报 `FOREIGN KEY constraint failed`（第一版就是这么挂的）。
        c.execute("DELETE FROM conversations WHERE id=?", (CID,))
        c.execute("INSERT INTO conversations (id, title, created_at, updated_at) "
                  "VALUES (?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
                  (CID, "[门禁] 产物版本链验证"))
        c.commit()

        # ── ① 产物 A 第一版 ──
        print("\n=== ① 产物 A 首版（package '串联混动汽车运行需求'）===")
        vid1 = _archive_sysml_version(c, CID, 9001, {}, "tester", CODE_A)
        c.commit()
        r1 = c.execute("SELECT version_label, subject, parent_id, status FROM sysml_versions "
                       "WHERE id=?", (vid1,)).fetchone()
        check("① A 首版 = v0.1", r1 and r1["version_label"] == "v0.1", dict(r1) if r1 else None)
        check("② subject 已写入（非空，DB 回读）", r1 and (r1["subject"] or "").strip() != "",
              r1["subject"] if r1 else None)
        check("③ 首版无 parent", r1 and not r1["parent_id"], r1["parent_id"] if r1 else None)

        # ── ② 产物 B 首版（不同 package）──
        print("\n=== ② 产物 B 首版（package '串联混动汽车用例'，与 A 不同产物）===")
        vid2 = _archive_sysml_version(c, CID, 9002, {}, "tester", CODE_B)
        c.commit()
        r2 = c.execute("SELECT version_label, subject, parent_id, status FROM sysml_versions "
                       "WHERE id=?", (vid2,)).fetchone()
        check("④ ★ B 首版也必须是 v0.1（不能是 v0.2 —— 这是原缺陷的核心）",
              r2 and r2["version_label"] == "v0.1", dict(r2) if r2 else None)
        check("⑤ ★ B 的 parent 必须为空（不能指向 A —— 不同产物不串联）",
              r2 and not r2["parent_id"], r2["parent_id"] if r2 else None)
        check("⑥ A、B 的 subject 不同", r1 and r2 and r1["subject"] != r2["subject"],
              "%r vs %r" % (r1["subject"] if r1 else None, r2["subject"] if r2 else None))
        # 旧版收敛只影响同 subject：A 应仍是 current（B 的写入不能作废 A）
        r1b = c.execute("SELECT status FROM sysml_versions WHERE id=?", (vid1,)).fetchone()
        check("⑦ ★ 写 B 不能把 A 的 current 作废（旧版收敛限同 subject）",
              r1b and r1b["status"] == "current", r1b["status"] if r1b else None)

        # ── ③ 产物 A 第二版 ──
        print("\n=== ③ 产物 A 第二版（同 package，应递增）===")
        vid3 = _archive_sysml_version(c, CID, 9003, {}, "tester", CODE_A2)
        c.commit()
        r3 = c.execute("SELECT version_label, subject, parent_id, status FROM sysml_versions "
                       "WHERE id=?", (vid3,)).fetchone()
        check("⑧ A 第二版 = v0.2", r3 and r3["version_label"] == "v0.2", dict(r3) if r3 else None)
        check("⑨ A 第二版 parent 指向 A 首版", r3 and r3["parent_id"] == vid1,
              "%s vs %s" % (r3["parent_id"] if r3 else None, vid1))
        r1c = c.execute("SELECT status FROM sysml_versions WHERE id=?", (vid1,)).fetchone()
        check("⑩ A 首版被收敛为 superseded", r1c and r1c["status"] == "superseded",
              r1c["status"] if r1c else None)
        r2b = c.execute("SELECT status FROM sysml_versions WHERE id=?", (vid2,)).fetchone()
        check("⑪ B 仍是 current（未被 A 的第二版波及）",
              r2b and r2b["status"] == "current", r2b["status"] if r2b else None)

        # ── ④ 命名：不同 subject 不共享名字 ──
        print("\n=== ④ 命名锁定按 subject 匹配 ===")
        from repositories.artifact_repo import ArtifactRepo
        repo = ArtifactRepo(c)
        tA, fA, mA = _resolve_code_artifact_title(c, CID, CODE_A, "v0.1", "sysml")
        repo.create_artifact(CID, 9001, "code", tA, "code", CODE_A,
                             {"lang": "sysml", "version_label": "v0.1", **mA},
                             filename=fA, source="conversation", created_by="tester")
        c.commit()
        tA2, fA2, mA2 = _resolve_code_artifact_title(c, CID, CODE_A2, "v0.2", "sysml")
        tB, fB, mB = _resolve_code_artifact_title(c, CID, CODE_B, "v0.1", "sysml")
        print("     A v0.1 title = %s" % tA)
        print("     A v0.2 title = %s" % tA2)
        print("     B v0.1 title = %s" % tB)
        check("⑫ 同 subject 第二版复用首版 name_base",
              mA2.get("name_base") == mA.get("name_base"),
              "%r vs %r" % (mA2.get("name_base"), mA.get("name_base")))
        check("⑬ ★ 不同 subject 不复用名字（B 的名字不得是 A 的）",
              mB.get("name_base") != mA.get("name_base"),
              "B=%r A=%r" % (mB.get("name_base"), mA.get("name_base")))
        check("⑭ B 的 name_key 是自己的 package 名",
              "用例" in str(mB.get("name_key") or ""), mB.get("name_key"))

        # ── ⑤ 反证：判据非空转 ──
        print("\n=== ⑤ 判据非空转 ===")
        n_subj = c.execute("SELECT COUNT(DISTINCT subject) AS n FROM sysml_versions "
                           "WHERE conversation_id=?", (CID,)).fetchone()["n"]
        check("⑮ DB 里确有 2 个不同 subject（否则上面几条是空转）",
              int(n_subj or 0) == 2, n_subj)
    finally:
        # 清理（只删本验证会话）
        try:
            c.execute("DELETE FROM sysml_versions WHERE conversation_id=?", (CID,))
            c.execute("DELETE FROM artifacts WHERE conversation_id=?", (CID,))
            c.execute("DELETE FROM conversations WHERE id=?", (CID,))
            c.commit()
            print("\n（已清理验证会话数据 conv=%d）" % CID)
        except Exception as e:
            print("\n⚠ 清理失败：%s" % e)
        c.close()

    print("\n" + "=" * 74)
    print("PASS %d / FAIL %d" % (PASS, FAIL))
    os.makedirs(os.path.join(REPO, "outputs"), exist_ok=True)
    with open(os.path.join(REPO, "outputs", "_verify_artifact_version_chain.json"),
              "w", encoding="utf-8") as f:
        json.dump({"pass": PASS, "fail": FAIL, "rows": ROWS}, f, ensure_ascii=False, indent=1)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
