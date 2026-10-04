# -*- coding: utf-8 -*-
"""P0-5/P1-1/P1-2/P0-2 四项整改的**端到端冒烟**（2026-10-03）。

**为什么要隔离库**：
    TestClient(app) 会**真实执行 lifespan**（init_db + 孤儿回收 + 告警线程 …），
    直接指向生产 `mbse.db` 会把 16 个批次的历史孤儿任务改成 failed。
    那是不可逆的业务写 —— 按工程纪律（改动留痕/先 Isolation 后落地），冒烟必须在
    **副本或空库**上跑：验证"接线通了"，把"要不要真收"留给人工确认。

用法：
    MBSE_DB_PATH=<某处独立库> <repo>\\.venv\\Scripts\\python.exe -X utf8 tools/verify/smoke_optimization_batch1.py
若不带 MBSE_DB_PATH，本脚本会自行在 tmp/ 下建一个空库（脚手架验证，不动生产数据）。
"""
import os
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
        db = os.path.join(ROOT, "tmp", "smoke_batch1.db")
        os.makedirs(os.path.dirname(db), exist_ok=True)
        for suffix in ("", "-wal", "-shm"):
            p = db + suffix
            if os.path.exists(p):
                os.remove(p)
        os.environ["MBSE_DB_PATH"] = db
    print("库: %s%s" % (db, "（脚手架空库）" if "smoke_batch1" in db else "（外部指定）"))

    from fastapi.testclient import TestClient
    from main import app

    print("\n=== 1. 应用启动（含 lifespan：init_db + 孤儿回收 + 限流中间件装配）===")
    try:
        with TestClient(app) as cli:
            check("应用启动成功", True)
            print("\n=== 2. P0-2 孤儿观测端点 ===")
            r = cli.get("/api/monitor/orphan-runs")
            check("GET /api/monitor/orphan-runs → 200", r.status_code == 200, f"got={r.status_code}")
            body = r.json()
            check("返回 ok + summary 三字段",
                  body.get("ok") is True and "orphan_tasks" in body and "ttl_s" in body,
                  f"body keys={list(body)[:8]}")
            print(f"      orphan_tasks={body.get('orphan_tasks')} orphan_runs={body.get('orphan_runs')} "
                  f"oldest_age_s={body.get('oldest_age_s')} ttl_s={body.get('ttl_s')}")

            print("\n=== 3. P0-2 回收 dry-run（只报告不改写）===")
            r2 = cli.post("/api/monitor/orphan-runs/reap?dry_run=true")
            check("POST reap?dry_run=true → 200", r2.status_code == 200, f"got={r2.status_code}")
            b2 = r2.json()
            check("dry_run 标记为真且 reaped=0",
                  b2.get("dry_run") is True and b2.get("reaped") == 0, f"got={b2}")

            print("\n=== 4. P1-1 限流契约头（普通读请求）===")
            r3 = cli.get("/api/knowledge/entities?limit=1")
            check("GET /api/entities → 200", r3.status_code == 200, f"got={r3.status_code}")
            hdr = r3.headers
            check("回带 X-RateLimit-Limit", "x-ratelimit-limit" in hdr, f"hdr={dict(list(hdr.items())[:8])}")
            check("回带 X-RateLimit-Bucket=read", hdr.get("x-ratelimit-bucket") == "read",
                  f"got={hdr.get('x-ratelimit-bucket')}")
            print(f"      limit={hdr.get('x-ratelimit-limit')} remaining={hdr.get('x-ratelimit-remaining')}")

            print("\n=== 5. P1-1 chat 桶真的会挡（连打 8 次 /chat 非流式）===")
            codes = []
            for _ in range(8):
                rr = cli.post("/api/conversations/1/chat", json={"message": "smoke"})
                codes.append(rr.status_code)
            n429 = codes.count(429)
            check("8 次内出现 429（chat 桶阈值生效）", n429 >= 1, f"codes={codes}")
            print(f"      codes={codes}")

            print("\n=== 6. P0-5 鉴权：未认证不得被静默放行（非强制态下仍可用）===")
            r5 = cli.get("/api/auth/config")
            check("GET /api/auth/config → 200", r5.status_code == 200, f"got={r5.status_code}")
            check("配置回显 trust_user_id_header 字段存在",
                  "enforce_login" in r5.json(), f"got={r5.json()}")
            r6 = cli.get("/api/auth/me")
            check("GET /api/auth/me → 200 且未认证",
                  r6.status_code == 200 and r6.json().get("authenticated") is False,
                  f"got={r6.status_code} {r6.json()}")
    except Exception as e:  # noqa: BLE001
        import traceback
        check("应用启动/冒烟无异常", False, f"{type(e).__name__}: {e}\n{traceback.format_exc()[-800:]}")

    print("\n" + "=" * 70)
    print("冒烟结果：%s" % ("全部通过" if _ok else "存在失败（见上）"))
    print("=" * 70)
    return 0 if _ok else 1


if __name__ == "__main__":
    sys.exit(main())
