# -*- coding: utf-8 -*-
"""P0-C 真机验证：起真服务 + 真实文档，走**异步入库**全链路并取证。

与 `verify_job_handlers.py`（离线不变式 + 变异）的分工：
  离线门禁管"逻辑对不对、有没有接队列"；本脚本管"**真跑一遍**能不能成"。
  离线门禁抓不到只在真实数据/真实进程上显形的问题（连接切换、worker 线程、
  embedding 真实耗时、SQLite 写锁竞争）。

## 验证项（全部用真实数据，不打桩）
L1 服务启动无异常，且 `/api/jobs/kinds` 列出 6 种业务作业
L2 上传真实文档（data/uploads 里的既有样本）用 `mode=async`
   ⇒ **提交耗时 < 3 s**（同步路径实测 13.4 min量级，这是本脚本的核心对比）
L3 documents 行被立刻建好（parse_status='queued'），源文件副本已落盘
L4 worker 在后台把作业跑完（status=done），且**chunks 真写进 document_chunks**
L5 幂等：同一doc_id 连续提交两次 ⇒ 第二次 deduped，不产生第二个作业
L6 终态后再提交 ⇒ 新建作业（用户的"重试"不被吞掉）
L7 `/api/jobs/{id}` 返回 cancelable/terminal 等派生字段正确

⚠️ 本脚本会**真写数据库**（新增 documents/job_jobs 行与 chunks）。
   用法：`python tools/verify/verify_job_handpoints_live.py`
   它只新增行，不删既有数据；跑完会打印新增行数便于人工核对。
"""
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PORT = int(os.environ.get("MBSE_LIVE_PORT", "8931"))
BASE = "http://127.0.0.1:%d" % PORT
TIMEOUT = 30
# 必须用项目 venv 的解释器起服务：系统python 没装 uvicorn/fastapi，
# 而本仓铁律是零新依赖 ⇒ 不允许 pip install，只能用 .venv。
VENV_PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
if not os.path.exists(VENV_PY):
    VENV_PY = sys.executable

_results = []


def rec(name, ok, detail=""):
    _results.append((bool(ok), name, detail))
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name,
                           ("  ← " + str(detail)) if detail else ""))
    return bool(ok)


def _req(path, data=None, method=None, headers=None, timeout=TIMEOUT):
    url = BASE + path
    body = None
    hdrs = {"X-User-Id": "verify-bot"}     # 兼容期身份头（core.deps 默认信任）
    if headers:
        hdrs.update(headers)
    if data is not None:
        if isinstance(data, (dict, list)):
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        else:
            body = data
    req = urllib.request.Request(url, data=body, headers=hdrs,
                                 method=method or ("POST" if body is not None else "GET"))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            return r.status, (json.loads(raw) if raw.strip().startswith(("{", "[")) else raw)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw


def _multipart(fields, filename, content):
    """手写 multipart（零依赖，本仓铁律 4：不引入 requests 等新依赖）。"""
    bnd = "----mbsejobgate%s" % int(time.time() * 1000)
    buf = io.BytesIO()
    for k, v in fields.items():
        buf.write(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                   % (bnd, k, v)).encode("utf-8"))
    buf.write(("--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"%s\"\r\n"
               "Content-Type: application/octet-stream\r\n\r\n" % (bnd, filename)).encode("utf-8"))
    buf.write(content)
    buf.write(("\r\n--%s--\r\n" % bnd).encode("utf-8"))
    return buf.getvalue(), "multipart/form-data; boundary=%s" % bnd


def _pick_sample():
    """从既有data/uploads 里挑一个**真实**样本（不造数据）。"""
    up = os.path.join(ROOT, "data", "uploads")
    if not os.path.isdir(up):
        return None, None, b""
    prefer = [f for f in os.listdir(up) if f.lower().endswith((".md", ".txt"))]
    pick = sorted(prefer)[0] if prefer else None
    if not pick:
        return None, None, b""
    with open(os.path.join(up, pick), "rb") as f:
        return pick, os.path.getsize(os.path.join(up, pick)), f.read()


def main():
    print("=== 启动真服务（端口 %d）===" % PORT)
    import subprocess
    env = dict(os.environ)
    env["MBSE_PORT"] = str(PORT)
    proc = subprocess.Popen(
        [VENV_PY, "-m", "uvicorn", "main:app", "--host", "127.0.0.1",
         "--port", str(PORT), "--log-level", "warning"],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    ok = False
    try:
        for _ in range(90):
            if proc.poll() is not None:
                out = proc.stdout.read().decode("utf-8", "replace")
                print(out[-3000:])
                rec("L0 服务启动", False, "进程提前退出")
                return 1
            try:
                # 用本脚本自己的端点做就绪探针（本仓没有 /api/health）
                st, _b = _req("/api/jobs/stats", timeout=3)
                if st == 200:
                    ok = True
                    break
            except Exception:
                pass
            time.sleep(1)
        if not rec("L0 服务启动成功", ok):
            return 1

        print("\n=== L1 作业类型清单 ===")
        st, kinds = _req("/api/jobs/kinds")
        names = sorted((kinds or {}).get("items", {})) if isinstance(kinds, dict) else []
        rec("L1 /api/jobs/kinds 返回 6 种业务作业", st == 200 and len(names) == 6,
            "%s %s" % (st, names))
        rec("L1b 含 doc_ingest / project_ingest",
            "doc_ingest" in names and "project_ingest" in names)

        print("\n=== L2/L3 真实文档异步入库 ===")
        fname, fsize, content = _pick_sample()
        if not rec("L2 找到真实样本文件（data/uploads）", bool(fname), fname):
            return 1
        print("  样本：%s（%s 字节）" % (fname, fsize))
        body, ctype = _multipart(
            {"title": "", "author": "verify", "version": "v1", "tags": "gate",
             "folder_id": "0", "mode": "async"}, fname, content)
        t0 = time.time()
        st, r = _req("/api/documents/upload", data=body, headers={"Content-Type": ctype})
        el = time.time() - t0
        if not rec("L2 上传(mode=async) HTTP 200", st == 200, "%s %s" % (st, str(r)[:200])):
            return 1
        jid = int((r or {}).get("job_id") or 0)
        doc_id = int((r or {}).get("doc_id") or 0)
        rec("L2a 返回 job_id + doc_id", jid > 0 and doc_id > 0, "job=%s doc=%s" % (jid, doc_id))
        rec("L2b 提交耗时 < 3s（同步路径是分钟~13.4min量级）", el < 3.0, "elapsed=%.3fs" % el)
        rec("L2c parse_status=queued（已登记未解析）",
            (r or {}).get("parse_status") == "queued", (r or {}).get("parse_status"))
        rec("L2d 未同步跑管道（chunk_count 为 0/空）",
            not (r or {}).get("chunk_count"), "chunk_count=%s" % (r or {}).get("chunk_count"))

        st, dj = _req("/api/documents/%d/lifecycle" % doc_id)
        rec("L3 文档行已可查（documents 落库）", st == 200, str(st))

        print("\n=== L4 worker 后台跑完（真embedding）===")
        t0 = time.time()
        last = {}
        done = False
        for _ in range(240):        # 最多等 4 分钟
            st, j = _req("/api/jobs/%d" % jid)
            last = j or {}
            if last.get("status") in ("done", "failed", "canceled"):
                done = last.get("status") == "done"
                break
            time.sleep(1)
        el = time.time() - t0
        rec("L4a worker 把作业跑到 done", done,
            "status=%s error=%s" % (last.get("status"), str(last.get("error"))[:120]))
        print("  后台耗时 %.1fs，progress=%s stage=%s"
              % (el, last.get("progress"), last.get("stage")))
        res = last.get("result") or {}
        rec("L4b 结果里有 parse_status/chunk_count",
            "parse_status" in res and "chunk_count" in res,
            "parse_status=%s chunk_count=%s" % (res.get("parse_status"), res.get("chunk_count")))
        rec("L4c 解析完成（parse_status=completed）",
            res.get("parse_status") == "completed", res.get("parse_status"))
        chunks = int(res.get("chunk_count") or 0)
        rec("L4d 真的写进了 chunks（>0）", chunks > 0, "chunk_count=%d" % chunks)
        rec("L4e 进度已到 100", int(last.get("progress") or 0) == 100, last.get("progress"))

        print("\n=== L5/L6 幂等语义（真库，两种时机分别验）===")
        # ⑤在途去重：连打两次，**中间不能等它跑完** —— worker间隔 2 s，
        #    两次请求在毫秒内到达必为在途。若这里就waith后测，测的其实是
        #    "终态后新建"（L6），双击防线根本没被覆盖（我第一版就这么写错了）。
        st, r2 = _req("/api/documents/%d/retry?mode=async" % doc_id, method="POST")
        jid2 = int((r2 or {}).get("job_id") or 0)
        st, r2b = _req("/api/documents/%d/retry?mode=async" % doc_id, method="POST")
        jid2b = int((r2b or {}).get("job_id") or 0)
        rec("L5a 在途重复提交 ⇒ 同一 job_id（双击被挡住）",
            jid2 > 0 and jid2b == jid2, "first=%s second=%s" % (jid2, jid2b))
        rec("L5b 标记 deduped", (r2b or {}).get("deduped") is True,
            "deduped=%s" % (r2b or {}).get("deduped"))
        # ⑥ 终态后新建：等它跑完再提交，必须是**新**作业（用户的重试不被吞）
        for _ in range(240):
            st, j2 = _req("/api/jobs/%d" % jid2)
            if (j2 or {}).get("status") in ("done", "failed", "canceled"):
                break
            time.sleep(1)
        st, r3 = _req("/api/documents/%d/retry?mode=async" % doc_id, method="POST")
        jid3 = int((r3 or {}).get("job_id") or 0)
        rec("L6 终态后再提交 ⇒ 新建作业（重试不被吞）",
            jid3 > 0 and jid3 != jid2, "prev=%s new=%s" % (jid2, jid3))
        # 把这两个作业收尾，避免留垃圾
        for j in (jid2, jid3):
            _req("/api/jobs/%d/cancel" % j, data={})

        print("\n=== L7 查询端点派生字段 ===")
        st, j = _req("/api/jobs/%d" % jid)
        rec("L7 返回 terminal/cancelable/lease_expired",
            st == 200 and "terminal" in (j or {}) and "cancelable" in (j or {})
            and "lease_expired" in (j or {}), str(st))
        rec("L7b result 已反序列化为对象", isinstance((j or {}).get("result"), dict))
        st, stats = _req("/api/jobs/stats")
        rec("L7c /api/jobs/stats 含 config 与 stats",
            st == 200 and "stats" in (stats or {}) and "config" in (stats or {}))
        st, lst = _req("/api/jobs?limit=5")
        rec("L7d 列表端点可用", st == 200 and isinstance((lst or {}).get("items"), list))
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    print("\n" + "=" * 68)
    n_pass = sum(1 for r in _results if r[0])
    print("真机验证：%d/%d 通过" % (n_pass, len(_results)))
    for ok_, name, detail in _results:
        if not ok_:
            print("  [FAIL] %s%s" % (name, ("  ← " + str(detail)) if detail else ""))
    print("新增行：documents +1（doc_id=%s），job_jobs 若干（本次验证作业）" % doc_id)
    print("=" * 68)
    return 0 if n_pass == len(_results) else 1


if __name__ == "__main__":
    sys.exit(main())