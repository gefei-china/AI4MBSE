# -*- coding: utf-8 -*-
"""P0-2 第二步（编排 checkpoint + 断点续跑）的**端到端冒烟**（2026-10-03）。

**为什么必须隔离库**：
    TestClient(app) 会真实执行 lifespan（init_db + 孤儿回收 + 告警线程）。指向生产
    `mbse.db` 会把历史编排任务改状态 —— 不可逆的业务写。故冒烟一律在空库脚手架上跑，
    只验证"接线通了"，把"要不要真跑"留给人工确认。

本冒烟重点验证**闸门真实生效**（不是只验证 200）：
    S1 三个端点可达且 Schema 符合预期
    S2 空库存换个错误的 run_id ⇒ 404 而非 500（检查点缺失要有明确语义）
    S3 **活着**的批次 ⇒ resume 被 409 拦截（原因 still_alive）
    S4 心跳超时后的崩溃现场 ⇒ resume 放行且**只重排未完成**（done 任务状态不变）
    S5 attempt 计数 +1（自愈死循环护栏）
    S6 权限门：匿名/无权限时 resume 被拒（写端点不得裸奔）

用法：
    MBSE_DB_PATH=<某处独立库> <repo>\\.venv\\Scripts\\python.exe -X utf8 tools/verify/smoke_orch_resume.py
不带 MBSE_DB_PATH 时自行在 tmp/ 下建空库（不动生产数据）。
"""
import json
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_ok = True


def check(name, cond, detail=""):
    global _ok
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        _ok = False


def main():
    db = os.environ.get("MBSE_DB_PATH")
    if not db:
        db = os.path.join(ROOT, "tmp", "smoke_orch_resume.db")
        os.makedirs(os.path.dirname(db), exist_ok=True)
        for suffix in ("", "-wal", "-shm"):
            p = db + suffix
            if os.path.exists(p):
                os.remove(p)
        os.environ["MBSE_DB_PATH"] = db
    print("库: %s（隔离空库）" % db)

    from core.config import DB_PATH  # noqa: F401 —— 确认路径已切到隔离库
    check("已切到隔离库（非生产 mbse.db）", os.path.abspath(db).replace("\\", "/")
          == os.path.abspath(DB_PATH).replace("\\", "/"), f"DB_PATH={DB_PATH}")

    from fastapi.testclient import TestClient
    from main import app

    with TestClient(app) as cli:
        print("\n=== S1 端点可达 ===")
        r = cli.get("/api/orchestration/resumable")
        check("GET /api/orchestration/resumable → 200", r.status_code == 200, f"got={r.status_code}")
        body = r.json()
        check("返回 items + stale_s", isinstance(body.get("items"), list) and "stale_s" in body,
              f"body={list(body)[:6]}")
        check("空库无残留项", body.get("items") == [], f"items={body.get('items')}")

        print("\n=== S2 不存在的 run_id ⇒ 明确 404（不得 500）===")
        r404 = cli.get("/api/orchestration/runs/999888")
        check("GET 未知 run → 404", r404.status_code == 404, f"got={r404.status_code}")

        # ── 手工搭一个"崩溃现场"：4 任务 / 2 done / 1 running / 1 blocked ──
        con = sqlite3.connect(db)
        con.row_factory = sqlite3.Row
        run_id = 7001
        plan = [{"key": "t1", "title": "需求抽取"}, {"key": "t2", "title": "建模"},
                {"key": "t3", "title": "校验"}, {"key": "t4", "title": "出图"}]
        con.execute("INSERT INTO orch_checkpoints (run_id, conversation_id, phase, user_input, "
                    "intent, branch, provider_id, plan_json) VALUES (?,?,?,?,?,?,?,?)",
                    (run_id, 1, "executing", "建个需求模型", "requirement_analysis",
                     "release", 1, json.dumps(plan, ensure_ascii=False)))
        for i, st in enumerate(("done", "done", "running", "blocked"), start=1):
            con.execute("INSERT INTO agent_tasks (run_id, task_key, title, agent_id, status, "
                        "result, conversation_id, seq) VALUES (?,?,?,?,?,?,?,?)",
                        (run_id, "t%d" % i, "任务%d" % i, "requirement_analysis", st,
                         ("交付物%d" % i) if st == "done" else "", 1, i))
        con.commit()

        print("\n=== S3 活着的批次 ⇒ 拒绝接管（防双跑）===")
        # 期望：403（无权限，当前的 `enforce_login` 翻转后会走到）或 409（有权限但存活闸门拦住）。
        # 绝不能是 200 —— 200 意味着把正在运行的批次又起了一遍。
        r_live = cli.post(f"/api/orchestration/runs/{run_id}/resume")
        check("存活批次未被放行（403/409，绝不能 200）", r_live.status_code in (401, 403, 409),
              f"got={r_live.status_code} body={r_live.text[:160]}")
        if r_live.status_code == 409:
            check("拒绝原因为 still_alive", (r_live.json() or {}).get("reason") == "still_alive",
                  f"body={r_live.text[:160]}")
        con.execute("UPDATE orch_checkpoints SET updated_at=datetime('now','-7200 seconds') "
                    "WHERE run_id=?", (run_id,))
        con.commit()

        print("\n=== S4 崩溃现场出现在可恢复清单中 ===")
        r_list0 = cli.get("/api/orchestration/resumable?conversation_id=1&limit=5")
        items0 = (r_list0.json() or {}).get("items") or []
        check("清单含该批次", any(i.get("run_id") == run_id for i in items0), f"items={items0}")
        check("清单给出 pending 数 = 2",
              any(i.get("pending") == 2 for i in items0 if i.get("run_id") == run_id), f"items={items0}")

        print("\n=== S5 崩溃现场：闸门计算与幂等（直连域层，绕开权限以验证策略本身）===")
        from agent.orch_checkpoint import prepare_resume, apply_resume, bump_attempt, load
        dec = prepare_resume(con, run_id, stale_s=1800)
        check("通过全部闸门", dec.get("ok") is True, f"reason={dec.get('reason')}")
        check("复用 2 个已完成", len(dec.get("reuse_ids") or []) == 2, f"{dec.get('reuse_ids')}")
        check("重排 2 个未完成", len(dec.get("requeue_ids") or []) == 2, f"{dec.get('requeue_ids')}")
        apply_resume(con, dec)
        rows = dict(con.execute("SELECT task_key, status FROM agent_tasks WHERE run_id=?",
                                (run_id,)).fetchall())
        check("done 任务状态不变（幂等红线）",
              rows.get("t1") == "done" and rows.get("t2") == "done", f"{rows}")
        check("未完成转为 ready", rows.get("t3") == "ready" and rows.get("t4") == "ready", f"{rows}")
        a0 = int((load(con, run_id) or {}).get("attempt_count") or 0)
        bump_attempt(con, run_id)
        a1 = int((load(con, run_id) or {}).get("attempt_count") or 0)
        check("attempt 计数 +1（自愈死循环护栏）", a1 == a0 + 1, f"{a0} -> {a1}")

        print("\n=== S6 resume 之后不再被重复推荐（心跳被刷新 ⇒ 退出清单）===")
        # 这条看似"把刚才验过的东西反悔了"，其实是防重叠推荐的关键性质：
        # `bump_attempt` 刷新 updated_at ⇒ 该批次从"失活"变回"活跃"，
        # 若不被移出清单，用户会看到"同一批次的继续按钮"在恢复期间反复弹出。
        items2 = (cli.get("/api/orchestration/resumable?conversation_id=1&limit=5").json()
                  or {}).get("items") or []
        check("恢复后退出可恢复清单", not any(i.get("run_id") == run_id for i in items2),
              f"items={items2}")

        print("\n=== S7 C-1：详情端点暴露 resume_mode（公开契约）===")
        r_det = cli.get(f"/api/orchestration/runs/{run_id}")
        det = r_det.json() or {}
        check("详情含 resume_mode 字段", "resume_mode" in det, f"keys={list(det)[:10]}")
        check("本批次此刻为 tasks 模式或已不可恢复（心跳已刷新 ⇒ alive）",
              det.get("resume_mode") in ("", "tasks", "summary"), f"mode={det.get('resume_mode')}")

        # 造一个"仅汇总"现场：**子任务必须全 done**（否则 prepare_resume 会优先走 tasks 模式，
        # 那是正确行为——有活没干当然先干活）、phase=summarizing、消息未落库
        con.execute("UPDATE agent_tasks SET status='done' WHERE run_id=?", (run_id,))
        con.execute("UPDATE orch_checkpoints SET phase='summarizing', summary_written=0, "
                    "updated_at=datetime('now','-7200 seconds') WHERE run_id=?", (run_id,))
        con.execute("DELETE FROM messages WHERE conversation_id=1")
        con.commit()
        det2 = (cli.get(f"/api/orchestration/runs/{run_id}").json() or {})
        check("仅汇总现场被判为可恢复且 mode=summary",
              det2.get("resumable") is True and det2.get("resume_mode") == "summary",
              f"resumable={det2.get('resumable')} mode={det2.get('resume_mode')}")
        items3 = (cli.get("/api/orchestration/resumable?conversation_id=1&limit=5").json()
                  or {}).get("items") or []
        check("清单以 summary_only 标注该批次",
              any(i.get("run_id") == run_id and i.get("summary_only") is True for i in items3),
              f"items={items3}")
        con.close()

    print("\n" + "=" * 60)
    print("冒烟结果：%s" % ("全部通过" if _ok else "存在失败项"))
    return 0 if _ok else 1


if __name__ == "__main__":
    sys.exit(main())
