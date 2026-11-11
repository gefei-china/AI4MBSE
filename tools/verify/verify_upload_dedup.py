# -*- coding: utf-8 -*-
"""verify_upload_dedup —— 上传去重（同名/同内容在上传时就提示，2026-11-10）。

## 为什么要有这个门禁（真实账单）
真库实测（2026-11-09）：`documents` 14 → 16，其中 id **851 / 852 同名**
`1_800_SysML-v2-AI建模知识文档.md`、**各 270 chunk**（相隔 18 分钟两次上传）。
后果：检索里同一段话出现两遍（挤掉别的来源）+ `verify_vec_cache` 与库差 540 行。
而当时库里**只有 filename + file_size 可比** ⇒ "同名误传"与"同名新版"无法区分，
"要不要拦"只能靠猜。本门禁锁的是**判定本身**，不是某次数据清理。

## 判据
### P 组 · 判定语义
  P1 字节完全相同 ⇒ kind=content + action=skip（复用，不产生重复 chunk）
  P2 同名不同内容 ⇒ kind=name + action=warn（允许同名更版，但要提示）
  P3 内容判据**优先于**文件名（改名重传也算重复）
  P4 存量行 content_sha256 为空 ⇒ 不参与内容判据（未知 ≠ 相同），只按文件名判
  P5 策略可配置：block ⇒ action=block（409 + force 才能过）；skip/warn 语义互换也生效
  P6 总开关关 ⇒ checked=False 且 action=proceed（逐字节回退到旧行为）
  P7 空文件不算"互相重复"（否则所有空文件会被判成同一份）

### R 组 · 接线（真的接在请求里，不是只造了个函数）
  R1 `routers/meta.py::upload_document` 在 ingest **之前**调用判定
  R2 skip/block 时**提前返回**（不得把重复件送进管道）
  R3 `force=1` 能越过 skip/block
  R4 同步/异步两条分支都会把 `duplicate` 带回前端（warn 也提示）
  R5 两条入库路径都写 `content_sha256`（否则异步上传的文档永不参与判重）
  R6 前端在 `fatal` 时弹确认、`skipped` 时提示复用（不是当成失败）

### D 组 · 盘点
  D1 `duplicate_groups` 能分出 same_content / same_name 两类
  D2 端点 `/api/documents/duplicates` 存在且只读（不删数据）

### M 组 · 变异自证
  M1 把内容判据去掉（先判文件名）⇒ P1 必须退化成 kind=name
  M2 把总开关判断去掉 ⇒ P6 必须翻红
  M3 把"空 sha 不算相同"去掉（直接用 sha256(b'') 比）⇒ P7 必须翻红

用法（必须用项目 venv）
    ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_upload_dedup.py
"""
from __future__ import annotations
from _tmpdir import mkdtemp

import hashlib
import inspect
import os
import sqlite3
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

PASS, FAIL = "PASS", "FAIL"
_results = []
_MUT = []
_IN_MUT = [False]


def check(name, ok, detail=""):
    row = (PASS if ok else FAIL, name, "" if ok else str(detail)[:260])
    (_MUT if _IN_MUT[0] else _results).append(row)
    print("[%s] %s%s" % ("OK  " if ok else "FAIL", name,
                         ("　— " + str(detail)) if detail and not ok else ""))
    return bool(ok)


# ── 夹具 ────────────────────────────────────────────────────────────────────
def _db(tag="p"):
    d = mkdtemp(prefix="docdedup_%s_" % tag)
    p = os.path.join(d, "t.db")
    c = sqlite3.connect(p)
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE documents (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        filename TEXT DEFAULT '', file_type TEXT DEFAULT '',
        file_size INTEGER DEFAULT 0, parse_status TEXT DEFAULT 'completed',
        chunk_count INTEGER DEFAULT 0, uploaded_by TEXT DEFAULT '',
        branch TEXT DEFAULT 'global', lifecycle_status TEXT DEFAULT 'stored',
        content_sha256 TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
    c.execute("""CREATE TABLE doc_metadata (
        document_id INTEGER PRIMARY KEY, title TEXT DEFAULT '',
        author TEXT DEFAULT '', version TEXT DEFAULT 'v1.0',
        tags TEXT DEFAULT '[]', source TEXT DEFAULT 'upload',
        extra TEXT DEFAULT '{}')""")
    c.commit()
    return c, p, d


def _add(c, name, content, sha=None, chunks=10):
    sha = hashlib.sha256(content).hexdigest() if sha is None else sha
    cur = c.execute("INSERT INTO documents (filename, file_size, chunk_count, content_sha256) "
                    "VALUES (?,?,?,?)", (name, len(content or b""), chunks, sha))
    did = int(cur.lastrowid)
    c.execute("INSERT INTO doc_metadata (document_id, title) VALUES (?,?)",
              (did, os.path.splitext(name)[0]))
    c.commit()
    return did


def _with_policy(**kw):
    """临时改配置（内存覆盖，不落盘）。返回还原函数。"""
    from core import config as _c
    old = {}
    for k, v in kw.items():
        old[k] = _c.get("upload", k, None)
        _c.set_override_in_memory = None  # 占位：下面直接用 monkeypatch
    return old


# ── P 组 ────────────────────────────────────────────────────────────────────
def p_group():
    print("\n=== P 组 · 判定语义 ===")
    from knowledge_pipeline import dedup as dd

    c, p, d = _db("p")
    A = b"# spec\nsame bytes\n"
    _add(c, "a.md", A, chunks=12)

    r = dd.check_duplicate(c, "a.md", A)
    check("P1 字节完全相同 ⇒ content + skip",
          r["kind"] == "content" and r["action"] == "skip" and r["existing"]["doc_id"] > 0, r)

    r = dd.check_duplicate(c, "a.md", b"# spec\nNEW bytes\n")
    check("P2 同名不同内容 ⇒ name + warn",
          r["kind"] == "name" and r["action"] == "warn"
          and r["same_name_count"] == 1, r)

    r = dd.check_duplicate(c, "renamed.md", A)
    check("P3 内容判据优先于文件名（改名重传仍是 content）",
          r["kind"] == "content" and r["action"] == "skip", r)

    # P4：存量行 sha 为空 ⇒ 不参与内容判据
    c.execute("UPDATE documents SET content_sha256='' WHERE id=1")
    c.commit()
    r = dd.check_duplicate(c, "a.md", A)
    check("P4 存量 sha 为空 ⇒ 只按文件名判（未知 ≠ 相同）",
          r["kind"] == "name", r)

    # P5：策略可配置
    dd._cfg = lambda g, k, dv: {"dup_name_policy": "block"}.get(k, dv)
    r = dd.check_duplicate(c, "a.md", b"other")
    check("P5a dup_name_policy=block ⇒ action=block", r["action"] == "block", r)
    # ⚠️ P4 刚把 doc1 的 sha 清空 ⇒ 这里必须补一份**同内容**的文档，
    #    否则 P5b 断言的"内容判据"根本没有夹具（首版就这么误报过一条）。
    _add(c, "copy_of_a.md", A, chunks=12)
    dd._cfg = lambda g, k, dv: {"dup_content_policy": "warn"}.get(k, dv)
    r = dd.check_duplicate(c, "renamed2.md", A)
    check("P5b dup_content_policy=warn ⇒ action=warn（内容相同也可放行）",
          r["kind"] == "content" and r["action"] == "warn", r)
    dd._cfg = lambda g, k, dv: {"dup_check_enabled": False}.get(k, True)
    r = dd.check_duplicate(c, "a.md", A)
    check("P6 总开关关 ⇒ checked=False 且 action=proceed（逐字节回退）",
          r["checked"] is False and r["action"] == "proceed", r)

    # 还原真配置读取
    import importlib
    importlib.reload(dd)

    # P7：空文件互不判重
    r = dd.check_duplicate(c, "empty1.txt", b"")
    check("P7 空文件不算重复（否则所有空文件互判同一份）",
          r["kind"] == "" and r["action"] == "proceed", r)
    c.close()


# ── R 组 ────────────────────────────────────────────────────────────────────
def r_group():
    print("\n=== R 组 · 接线 ===")
    p = os.path.join(_ROOT, "routers", "meta.py")
    s = open(p, encoding="utf-8").read().replace("\r\n", "\n")

    i_up = s.find("def upload_document(")
    i_dup = s.find("_chk_dup(conn, file.filename, content)", i_up)
    i_ing = s.find("ingest_upload_document(conn, file.filename", i_up)
    i_stage = s.find("stage_upload_document(conn, file.filename", i_up)
    check("R1 判定在 ingest 之前调用（同步与异步两条分支都在其后）",
          0 < i_up < i_dup and i_dup < i_ing and i_dup < i_stage,
          {"upload": i_up, "dup": i_dup, "ingest": i_ing, "stage": i_stage})

    seg = s[i_dup:i_ing]
    check("R2 skip/block 时提前返回（重复件不进管道）",
          '"skipped": True' in seg and "409" in seg and "return" in seg,
          seg[:200])

    i_force = s.find('_force = str(force or "")', i_up)
    check("R3 force=1 能越过 skip/block",
          0 < i_force < i_dup and "and not _force" in s[i_dup:i_dup + 600],
          s[i_dup:i_dup + 300])

    i_async_ret = s.find('if _dup.get("kind"):', i_dup)
    i_sync_ret = s.find('if _dup.get("kind"):', i_async_ret + 10)
    check("R4 同步/异步两条分支都把 duplicate 带回前端（warn 也提示）",
          i_async_ret > 0 and i_sync_ret > i_async_ret,
          {"async": i_async_ret, "sync": i_sync_ret})

    ing = open(os.path.join(_ROOT, "knowledge_pipeline", "ingest.py"),
               encoding="utf-8").read().replace("\r\n", "\n")
    n_stamp = ing.count("stamp_content_hash")
    check("R5 两条入库路径都写 content_sha256（同步 ingest + 异步 stage）",
          n_stamp >= 2, "stamp_content_hash 出现 %d 次" % n_stamp)

    js = open(os.path.join(_ROOT, "static", "js", "mods", "20-docs.js"),
              encoding="utf-8").read().replace("\r\n", "\n")
    check("R6 前端：fatal 弹确认 + skipped 提示复用（不当成失败）",
          "r.fatal && r.duplicate" in js and "r.skipped && r.duplicate" in js
          and "confirmDialog" in js[js.find("r.fatal && r.duplicate"):][:1500],
          "前端未处理重复回包")


# ── D 组 ────────────────────────────────────────────────────────────────────
def d_group():
    print("\n=== D 组 · 盘点 ===")
    import importlib
    from knowledge_pipeline import dedup as dd
    importlib.reload(dd)

    c, p, d = _db("d")
    A = b"dup bytes"
    _add(c, "x1.md", A, chunks=5)
    _add(c, "x2.md", A, chunks=5)          # 同内容不同名
    _add(c, "y.md", b"v1", chunks=3)
    _add(c, "y.md", b"v2", chunks=4)       # 同名不同内容
    groups = dd.duplicate_groups(c)
    kinds = {g["kind"] for g in groups}
    check("D1 盘点分出 same_content / same_name 两类",
          kinds == {"same_content", "same_name"}, [(g["kind"], g["key"]) for g in groups])
    sc = [g for g in groups if g["kind"] == "same_content"]
    check("D1b same_content 组含 2 份且浪费块数 = 5",
          sc and sc[0]["count"] == 2 and sc[0]["chunks"] == 10, sc)

    # ★ 真机冒烟踩出来：端点首版把"重复组内全部块数"当成"浪费量"报出去（650 vs 实际可回收 325）。
    #   判据：必须同时给出 chunks_in_groups 与 reclaimable_chunks，且后者只对 same_content 计。
    meta0 = open(os.path.join(_ROOT, "routers", "meta.py"), encoding="utf-8").read()
    check("D2b 盘点端点区分「组内总块数」与「可回收块数」（不得混为一个数）",
          "chunks_in_groups" in meta0 and "reclaimable_chunks" in meta0
          and "wasted_chunks" not in meta0,
          "端点仍在用含义不明的单一数字")
    check("D2d 端点提供 ?verify=1（读源副本验字节），且未验证时不谎报可回收量",
          "verify_bytes=bool(verify)" in meta0 and "sha_verified" in meta0
          and "bytes_verified" in meta0,
          "端点缺少显式字节校验开关")
    # 功能级：同内容组才算可回收
    c2, p2, d2 = _db("d2")
    A2 = b"same bytes"
    _add(c2, "k1.md", A2, chunks=5)
    _add(c2, "k2.md", A2, chunks=5)
    from knowledge_pipeline.dedup import duplicate_groups as _dg
    _gs = _dg(c2)
    _sc = [g for g in _gs if g["kind"] == "same_content"]
    _reclaim = sum(max(0, int(g["chunks"] or 0) - max(int(x.get("chunk_count") or 0)
                                                     for x in g["docs"]))
                   for g in _sc)
    check("D2c same_content 组的可回收量 = 总块数 − 保留一份", _reclaim == 5, _reclaim)
    c2.close()

    meta = open(os.path.join(_ROOT, "routers", "meta.py"), encoding="utf-8").read()
    seg = meta[meta.find("def list_duplicate_documents"):]
    seg = seg[:seg.find("@router.post")]
    # ⚠️ 判据必须落在**可执行的 SQL 调用**上，不能是纯文本包含 ——
    #   本函数的 docstring 里就有「删除走 DELETE /api/documents/{id}」这句话，
    #   用 `"DELETE" in seg` 会被自己的说明文字满足（本仓 §6.9a 已踩过一次）。
    import re as _re
    _sql_writes = _re.findall(r'execute\(\s*[\'"](?:DELETE|UPDATE|INSERT|DROP)\b', seg, _re.I)
    check("D2 盘点端点只读（没有任何写 SQL / 删除调用）",
          not _sql_writes and "delete_document" not in seg,
          _sql_writes[:3])
    c.close()


# ── M 组 ────────────────────────────────────────────────────────────────────
def m_group():
    print("\n=== M 组 · 变异自证 ===")
    import textwrap
    from knowledge_pipeline import dedup as dd

    src = textwrap.dedent(inspect.getsource(dd.check_duplicate))
    c, p, d = _db("m")
    A = b"same bytes here"
    _add(c, "a.md", A)

    _IN_MUT[0] = True
    # M1：去掉内容判据（等价"只按文件名判"的旧实现）
    mut1 = src.replace("if sha:", "if False:", 1)
    ns = dict(vars(dd))
    exec(compile(mut1, "<m1>", "exec"), ns)
    r = ns["check_duplicate"](c, "a.md", A)
    check("M1 去掉内容判据 ⇒ 同内容退化成 kind=name（P1 翻红）",
          r["kind"] == "name", r)

    # M2：去掉总开关判断
    mut2 = src.replace('if not bool(_cfg("upload", "dup_check_enabled", True)):',
                       "if False:", 1)
    ns2 = dict(vars(dd))
    ns2["_cfg"] = lambda g, k, dv: False if k == "dup_check_enabled" else dv
    exec(compile(mut2, "<m2>", "exec"), ns2)
    r2 = ns2["check_duplicate"](c, "zzz.md", b"brand new")
    check("M2 去掉总开关判断 ⇒ 关闭后仍会判定（P6 翻红）",
          r2["checked"] is True, r2)

    # M3：空内容也参与内容判据（sha256(b'') 恒等 ⇒ 所有空文件互判重复）
    # ⚠️ 必须**先造一份 sha=sha256(b'') 的行**：否则变异体查不到命中，断言空转通过
    #    （这正是"变异没真正改变被观察行为"的假绿，本仓 §6.9b 的现成教训）。
    _add(c, "blank_seed.txt", b"", sha=hashlib.sha256(b"").hexdigest())
    mut3 = src.replace('if sha:', 'if True:', 1)
    ns3 = dict(vars(dd))
    ns3["content_sha256"] = lambda b: hashlib.sha256(b"").hexdigest()
    exec(compile(mut3, "<m3>", "exec"), ns3)
    r3 = ns3["check_duplicate"](c, "empty.txt", b"")
    check("M3 空内容参与判重 ⇒ 空文件被判重复（P7 翻红）",
          r3["kind"] == "content", r3)
    _IN_MUT[0] = False
    c.close()


def main():
    t0 = time.time()
    print("verify_upload_dedup —— 上传去重（同名/同内容在上传时提示）")
    p_group()
    r_group()
    d_group()
    m_group()
    n_pass = sum(1 for r in _results if r[0] == PASS)
    n_mut = sum(1 for r in _MUT if r[0] == PASS)
    n_fail = sum(1 for r in _results + _MUT if r[0] == FAIL)
    print("\n结果：%d PASS / %d FAIL　（其中变异自证 %d 条）　%.1fs"
          % (n_pass, n_fail, n_mut, time.time() - t0))
    for st, name, detail in _results + _MUT:
        if st == FAIL:
            print("  ✗ %s　%s" % (name, detail))
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
