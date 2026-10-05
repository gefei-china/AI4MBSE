# -*- coding: utf-8 -*-
"""CI 等价环境复现：全新干净库 + 不依赖本地服务，按 ci.yml 顺序跑全部门禁。

## 为什么单独一个脚本

上一轮是人工按顺序跑的，不可复现；这轮把「怎么造 CI 等价环境」固化下来：
  1. 建一个 init_db 过的**全新干净库**，并**自证**三个模块的 DB_PATH 都指向它
     （MEMORY：只设环境变量对已 import 的进程无效）
  2. 全部子门禁只通过环境变量 MBSE_DB_PATH 继承该库
  3. 跑完对**生产库做指纹比对**，证明零漂移

用法：
    .venv/Scripts/python.exe tools/run_ci_equiv.py            # 全量
    .venv/Scripts/python.exe tools/run_ci_equiv.py --only 5   # 只跑前 5 个
"""
import hashlib
import io
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time

ROOT = r"C:\Users\gefei\WorkBuddy\2026-08-04-19-05-52\mbse_system"
os.chdir(ROOT)
PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
CI = os.path.join(ROOT, ".github", "workflows", "ci.yml")
PROD = os.path.join(ROOT, "mbse.db")


def fingerprint(path):
    if not os.path.exists(path):
        return {"exists": False}
    st = os.stat(path)
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read(8 << 20))
    counts = {}
    try:
        c = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True)
        for t in ("documents", "document_chunks", "conversations", "entities", "messages"):
            try:
                counts[t] = c.execute("select count(*) from %s" % t).fetchone()[0]
            except Exception as e:
                counts[t] = "ERR:%s" % e
        c.close()
    except Exception as e:
        counts["_open"] = str(e)
    return {"size": st.st_size, "mtime_ns": st.st_mtime_ns,
            "head_sha": h.hexdigest()[:16], "counts": counts}


def gate_commands():
    """从 ci.yml 抽取所有 `python tools/verify/xxx.py [args]` 执行行（注释行不算）。

    ⚠️ 2026-10-05 踩坑：**必须连参数一起抓**。
    初版只抓脚本路径，把 `verify_orchestration_e2e.py --static-only` 跑成了
    **live 模式** ⇒ 真调 LLM（3986s 超时）+ **往生产库写了 1 会话 2 消息**。
    （靠生产库指纹自证才发现，否则就是一次静默的生产库污染。）
    """
    cmds = []
    for raw in io.open(CI, encoding="utf-8").read().splitlines():
        line = raw.strip()
        if line.startswith("#"):
            continue
        m = re.search(r"python\s+(tools/verify/[A-Za-z0-9_]+\.py)(.*)$", line)
        if m:
            # 参数在空格之后，必须整段尾随（否则 --static-only 会被丢掉 ⇒ 跑成 live 模式）
            cmds.append((m.group(1) + " " + (m.group(2) or "").strip()).strip())
    # 去重保序
    seen, out = set(), []
    for c in cmds:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def main():
    only = None
    if "--only" in sys.argv:
        only = int(sys.argv[sys.argv.index("--only") + 1])

    cmds = gate_commands()
    if only:
        cmds = cmds[:only]
    print("[ci-equiv] 待跑门禁 %d 个" % len(cmds))

    tmpdir = tempfile.mkdtemp(prefix="ciequiv_")
    clean = os.path.join(tmpdir, "clean.db")
    print("[ci-equiv] 建干净库 %s" % clean)
    r = subprocess.run([PY, "tools/mkclean_db.py", clean],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", cwd=ROOT)
    if "OK" not in (r.stdout or ""):
        print(r.stdout, r.stderr[-2000:])
        print("[FATAL] 干净库建库失败")
        return 2

    fp_before = fingerprint(PROD)
    print("[ci-equiv] 生产库指纹(before): %s" % json.dumps(fp_before, ensure_ascii=False))

    env = dict(os.environ)
    env["MBSE_DB_PATH"] = clean
    env["PYTHONIOENCODING"] = "utf-8"

    results, t_all = [], time.time()
    for i, cmd in enumerate(cmds, 1):
        t0 = time.time()
        try:
            # cmd 形如 "tools/verify/xxx.py --flag"；参数必须原样带上（见 gate_commands 注释）
            argv = [PY] + cmd.split()
            p = subprocess.run(argv, capture_output=True, text=True,
                               encoding="utf-8", errors="replace",
                               timeout=600, env=env, cwd=ROOT)
            rc, out, err = p.returncode, p.stdout or "", p.stderr or ""
        except subprocess.TimeoutExpired:
            rc, out, err = None, "", "TIMEOUT"
        sec = round(time.time() - t0, 1)
        results.append({"cmd": cmd, "rc": rc, "sec": sec})
        flag = "OK " if rc == 0 else "RED"
        print("  [%3d/%3d] %s %-52s %ss" % (i, len(cmds), flag,
                                            os.path.basename(cmd), sec), flush=True)
        if rc not in (0,):
            tail = [l for l in (out + err).splitlines() if l.strip()][-8:]
            for l in tail:
                print("        | %s" % l[:170])

    fp_after = fingerprint(PROD)
    drift = json.dumps(fp_before, sort_keys=True) != json.dumps(fp_after, sort_keys=True)
    print("\n[自证] 生产库零漂移: %s" % ("否（异常！）" if drift else "是"))

    red = [r for r in results if r["rc"] not in (0,)]
    print("[ci-equiv] 用时 %ss | 绿 %d / 红 %d" %
          (round(time.time() - t_all), len(results) - len(red), len(red)))
    for r in red:
        print("   RED %s (rc=%s)" % (r["cmd"], r["rc"]))
    return 1 if red else 0


if __name__ == "__main__":
    sys.exit(main())
