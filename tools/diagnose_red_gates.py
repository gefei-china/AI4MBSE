# -*- coding: utf-8 -*-
"""逐项归因第二轮的 19 个红灯门禁（2026-10-05）。

## 要解决什么

盘点表里只记了「哪个门禁红 + 多少 PASS/FAIL」，
但**没记 FAIL 的是哪条断言** ⇒ 无法判断是
「产品代码真的退化」还是「门禁口径过期」。

## 关键纪律（踩过坑）

1. **切库必须让子进程自己认干净库**。设`MBSE_DB_PATH` 环境变量后，
   还要**先自证**门禁进程实际开的库就是干净库 —— 有些门禁硬编码相对路径
   `sqlite3.connect("mbse.db")`，环境变量对它无效（这正是
   `verify_skill_injection` 踩过的坑）。
   ⇒ 本脚本在跑之前/之后各记一次**生产库指纹**（size + mtime + 关键表行数），
   跑完比对，证明"零漂移"，而不是口头保证。
2. **不预设结论**。FAIL 行原样落盘，由人判读，不在这里自动定性。
3. 超时单独归类，不混进"红"里。

## 用法
    .venv/Scripts/python.exe tools/diagnose_red_gates.py            # 全量
    .venv/Scripts/python.exe tools/diagnose_red_gates.py verify_slot_merge  # 单个
"""
import hashlib
import json
import os
import subprocess
import sys
import time

ROOT = r"C:\Users\gefei\WorkBuddy\2026-08-04-19-05-52\mbse_system"
os.chdir(ROOT)
PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe")

RED_GATES = [
    "verify_agent_name_unify", "verify_intent_history", "verify_intent_llm_fusion",
    "verify_multiturn_context", "verify_llm_context_guard_mutate",
    "verify_orch_reliability", "verify_p14b",
    "verify_slot_merge", "verify_plugin_builtin", "verify_uninstall_flow",
    "verify_multi_project_scope", "verify_multi_project_scope_mutate",
    "verify_ontology_dom_range", "verify_orch_whitelist", "verify_plugin_bind_removed",
    "verify_doc_global", "verify_commit_hash", "verify_continuation",
    "verify_partial_persist", "verify_token_budget",
]

TIMEOUT = 600
PROD_DB = os.path.join(ROOT, "mbse.db")


def prod_fingerprint():
    """生产库指纹：size + mtime_ns + sha256(前 8MB) + 关键表行数。

    sha256 全量 300MB 太慢，只取前 8MB —— 足够发现"被写过"（写会改 mtime 与头部）。
    """
    import sqlite3
    if not os.path.exists(PROD_DB):
        return {"exists": False}
    st = os.stat(PROD_DB)
    h = hashlib.sha256()
    with open(PROD_DB, "rb") as f:
        h.update(f.read(8 << 20))
    counts = {}
    try:
        c = sqlite3.connect("file:%s?mode=ro" % PROD_DB.replace("\\", "/"), uri=True)
        for t in ("documents", "document_chunks", "conversations", "entities"):
            try:
                counts[t] = c.execute("select count(*) from %s" % t).fetchone()[0]
            except Exception as e:
                counts[t] = "ERR:%s" % e
        c.close()
    except Exception as e:
        counts["_open"] = str(e)
    return {"size": st.st_size, "mtime_ns": st.st_mtime_ns,
            "head_sha": h.hexdigest()[:16], "counts": counts}


def run_one(name, db_path):
    env = dict(os.environ)
    env["MBSE_DB_PATH"] = db_path
    env["PYTHONIOENCODING"] = "utf-8"
    t0 = time.time()
    try:
        p = subprocess.run(
            [PY, os.path.join("tools", "verify", name + ".py")],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=TIMEOUT, env=env, cwd=ROOT)
        return {"rc": p.returncode, "out": p.stdout or "", "err": p.stderr or "",
                "sec": round(time.time() - t0, 1)}
    except subprocess.TimeoutExpired:
        return {"rc": None, "out": "", "err": "TIMEOUT>%ds" % TIMEOUT,
                "sec": TIMEOUT}


def main():
    import tempfile
    targets = sys.argv[1:] or RED_GATES

    tmpdir = tempfile.mkdtemp(prefix="redgate_")
    clean_db = os.path.join(tmpdir, "clean.db")
    # 建干净库
    # 建干净库。
    # 依赖链（实测）：core/config.py 模块级 `DB_PATH = get("database","path")`
    #   → database/connection.py `from core.config import DB_PATH`（import 期绑进本模块作用域）
    #   → database/schema.py 同样`from core.config import DB_PATH`
    # ⇒ 只设环境变量在"已经 import 过 core 的进程"里是废品（MEMORY 教训）；
    #   这里在**全新子进程**里设环境变量（config 支持 MBSE_DB_PATH override），
    #   **并且**事后自证三个模块的DB_PATH 实际值 —— 不靠"应该生效"的口头保证。
    env = dict(os.environ)
    env["MBSE_DB_PATH"] = clean_db
    env["PYTHONIOENCODING"] = "utf-8"
    print("[setup] 建干净库: %s" % clean_db)
    setup_code = (
        "import core.config as cfg, database.connection as c, database.schema as s\n"
        "print('cfg  =', cfg.DB_PATH)\n"
        "print('conn =', c.DB_PATH)\n"
        "print('schema=', s.DB_PATH)\n"
        "assert cfg.DB_PATH == c.DB_PATH == s.DB_PATH == r'%s', 'DB_PATH 未切干净'\n"
        "s.init_db()\n"
        "import os; print('init_ok size=', os.path.getsize(cfg.DB_PATH))\n" % clean_db
    )
    p = subprocess.run([PY, "-c", setup_code],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", env=env, cwd=ROOT)
    print(p.stdout.strip() or p.stderr.strip()[-2000:])
    if "init_ok" not in (p.stdout or ""):
        print("[FATAL] 干净库建库失败或DB_PATH 未切干净，中止")
        return 2

    fp_before = prod_fingerprint()
    print("[setup] 生产库指纹(before): %s" % json.dumps(fp_before, ensure_ascii=False))

    results = []
    for i, name in enumerate(targets, 1):
        print("\n[%d/%d] %s" % (i, len(targets), name), flush=True)
        r = run_one(name, clean_db)
        lines = [l for l in (r["out"] + r["err"]).splitlines() if l.strip()]
        fail = [l for l in lines if ("FAIL" in l or "✗" in l or "❌" in l
                                     or l.strip().startswith("[X]") or " ERROR" in l)]
        summ = [l for l in lines if any(k in l for k in ("PASS", "FAIL", "SKIP", "总计", "合计", "通过"))]
        r["fail_lines"] = fail[:40]
        r["summary_lines"] = summ[-6:]
        r["name"] = name
        r["tail"] = lines[-12:]
        results.append(r)
        print("   rc=%s sec=%s fail行=%d" % (r["rc"], r["sec"], len(fail)))
        for l in (r["summary_lines"] or lines[-3:]):
            print("   | %s" % l[:200])
        for l in r["fail_lines"][:6]:
            print("   X %s" % l[:200])

    fp_after = prod_fingerprint()
    print("\n[setup] 生产库指纹(after) : %s" % json.dumps(fp_after, ensure_ascii=False))
    drift = json.dumps(fp_before, sort_keys=True) != json.dumps(fp_after, sort_keys=True)
    print("[自证] 生产库是否被改动: %s" % ("是（异常！）" if drift else "否（零漂移）"))

    outp = os.path.join(tmpdir, "report.json")
    with open(outp, "w", encoding="utf-8") as f:
        json.dump({"results": results, "prod_drift": drift,
                   "clean_db": clean_db}, f, ensure_ascii=False, indent=2)
    print("\n[report] %s" % outp)

    red = [r["name"] for r in results if r["rc"] not in (0,)]
    print("\n[汇总] 绿 %d / 红 %d" % (len(results) - len(red), len(red)))
    for n in red:
        print("   RED %s" % n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
