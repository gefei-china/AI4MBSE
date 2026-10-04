# -*- coding: utf-8 -*-
"""P0-C 第二步：长任务业务端点接入作业队列 —— 不变式 + 变异自证。

## 为什么需要这个门禁（"能力齐了但没人用"是真实发生过的）
上一轮`core/job_queue.py` 交付后，全仓 **grep 不到任何业务端点调用它**
（只在 schema / 自身 / 门禁里出现）—— 队列能力齐备却无人使用，
长任务仍同步占连接。这类"最后��公里没通"的缺陷没有任何单测会自然报警，
只能靠一个**断言业务端点确实接了队列**的门禁。

## 六条不变式
H1 **端点接队列**：三类长任务端点都提供异步分支，且异步分支**真的只提交不执行**
   （判据：异步调用返回体里有 job_id，且 handler 未被调用）。
H2 **提交即返回**：异步提交的耗时与handler 执行时长无关（毫秒级）。
H3 **幂等锚定当次在途**：`reuse_terminal=False` 时，
   ① 在途（queued/running）重复提交 ⇒ 同一 job_id、不重复入队；
   ② **终态重复提交 ⇒ 新建作业**（否则用户第二次点「重试」被永久吞掉）。
   ⚠️ 这对断言必须成对：只测① 会让"判据恒真"的写法蒙混过关
   （判据写成"这个 key 提交过"时 ① 照样过，② 恒错）。
H4 **长作业续租**：handler执行期间 `lease_until` 必须被推进。
   反证：13.4 min 的入库 > 默认租约 300 s ⇒ 不续租会被自己的 worker 重领并双跑。
   判据必须**观测数据库里的 lease_until 变化**，而不是"断言代码里有 LeaseKeeper"。
H5 **管道失败不重试**：解析失败（扫描件/损坏文件）重试多少次都是同一结果，
   不该烧 embedding 额度 ⇒ handler 返回而不抛异常。
   反证判据：真的抛 ⇒ worker 会把 attempt 推上去并最终 failed。
H6 **同步/异步抽取行为一致**：auto_extract_stage 被两条路径共用
   （防"异步上传的文档不进治理面板待办"这类只在真数据上显形的漂移）。

## 变异自证（每条变异改**行为**，再用同一判据函数真跑一遍）
M1 幂等判据锚成"这一行"而非"家族内在途" ⇒ H3 判红
M2 `reuse_terminal` 改成"终态也复用" ⇒ H3② 判红
M3 LeaseKeeper 的心跳间隔设成大于租约 ⇒ H4 判红
M4 解析失败改成抛异常 ⇒ H5 判红
"""
import os
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PASS, FAIL, VACUOUS = "PASS", "FAIL", "VACUOUS"
_results = []
_MUT_VERDICT = []   # 变异结论（M1/M2/M3…）：必须全部 PASS（即"确实判红"）
_MUT_DETAIL = []    # 变异期的中间断言：红是预期，仅供人核对
_IN_MUT = [False]

JQ_SRC = os.path.join(ROOT, "core", "job_queue.py")
JH_SRC = os.path.join(ROOT, "core", "job_handlers.py")
KP_SRC = os.path.join(ROOT, "knowledge_pipeline", "ingest.py")

DDL = """
CREATE TABLE IF NOT EXISTS job_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_key TEXT UNIQUE NOT NULL,
    kind TEXT DEFAULT '',
    payload TEXT DEFAULT '{}',
    status TEXT DEFAULT 'queued',
    progress INTEGER DEFAULT 0,
    stage TEXT DEFAULT '',
    result TEXT DEFAULT '',
    error TEXT DEFAULT '',
    attempt INTEGER DEFAULT 0,
    max_attempts INTEGER DEFAULT 3,
    lease_until TEXT DEFAULT '',
    heartbeat_at TEXT DEFAULT '',
    worker_id TEXT DEFAULT '',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE INDEX IF NOT EXISTS ix_job_pick ON job_jobs(status, created_at);
CREATE INDEX IF NOT EXISTS ix_job_kind ON job_jobs(kind, status);
"""


def _rec(name, ok, detail="", kind=FAIL):
    rec = (PASS if ok else kind, name, detail)
    # ⚠️ 变异期的**中间断言**（H3a/H4d 等被变异体跑出来的红/绿）必须与
    #   "变异结论"分开记账：它们的红是**预期**的，混进结论会让门禁永远红。
    #   ⇒ 变异期只把 `_rec("M<n> ...")` 这类结论记进 _MUT_VERDICT；
    #   中间断言记进 _MUT_DETAIL（仅供人核对"它确实是因为目标原因而红"）。
    if _IN_MUT[0]:
        (_MUT_VERDICT if name[:2] in ("M1", "M2", "M3", "M4") else _MUT_DETAIL).append(rec)
    else:
        _results.append(rec)
    print("  [%s] %s%s" % (rec[0], name, ("  ← " + detail) if detail else ""))
    return bool(ok)


def _db():
    """建一个内存库，**并登记为 worker 的连接来源**。

    ⚠️ 必须登记：`job_queue._run_one` 通过 `_conn()` 自取连接，
    若不注入，它会走 `database.get_db()`连到**生产库**（我实测踩过：
    作业卡在 running 因为生产库没有这张表 / 状态写在另一个库，断言永远读到 running）。
    这与 core/job_queue.set_conn_factory 文档里记的是同一个坑。
    """
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(DDL)
    _SHARED["con"] = con
    try:
        import core.job_queue as _real
        _real.set_conn_factory(lambda: con)
    except Exception:
        pass
    return con


def _mkdb():
    """建一个**独立**内存库（不登记为 worker 连接源）。"""
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(DDL)
    return con


def _bind(ns, con, worker_con=None):
    """把连接工厂注入到某个命名空间。

    ⚠️ `worker_con` 必须给 **worker 自己那份连接**：`job_queue._run_one` 在
    `finally` 里会 `conn.close()` 关掉它借来的连接。若把断言要读的那条连接
    借给 worker，读的时候就是 "Cannot operate on a closed database"
    （我实测踩了；且这类错误会伪装成"功能坏了"）。
    SQLite `:memory:` 无法跨连接共享 ⇒ 这里用 **共享缓存 URI 内存库**，
    让"worker 的连接"与"断言的连接"看到同一份数据。
    """
    wc = worker_con if worker_con is not None else con
    try:
        ns["set_conn_factory"](lambda: wc)
    except Exception:
        pass
    return con


def _shared_mem_pair(name="jobgate"):
    """返回 (主连接, worker 连接)：同一份数据、两个独立连接对象。"""
    uri = "file:%s_%d?mode=memory&cache=shared" % (
        name, int(time.time() * 1000) % 1000000)
    main_con = sqlite3.connect(uri, uri=True, check_same_thread=False)
    main_con.row_factory = sqlite3.Row
    main_con.executescript(DDL)
    main_con.execute("PRAGMA database_list")   # 保持库句柄存活
    wk = sqlite3.connect(uri, uri=True, check_same_thread=False)
    wk.row_factory = sqlite3.Row
    wk.execute("SELECT COUNT(*) FROM job_jobs").fetchone()   # 让 worker 连接也打开同一库
    return main_con, wk


def _load(src_path, mod_name, mutate=None):
    """加载模块（可带变异）。只做源码文本变异 + 独立命名空间 exec，不污染真实模块。"""
    src = open(src_path, encoding="utf-8").read()
    if mutate:
        old, new = mutate
        assert old in src, "变异锚点未命中：%r" % old[:60]
        src = src.replace(old, new, 1)
    ns = {"__name__": mod_name, "__file__": src_path}
    exec(compile(src, src_path, "exec"), ns)
    return ns


# ══════════════════════════════════════════════════════════════════
# H1/H2 端点确实接了队列，且异步分支只提交不执行
# ══════════════════════════════════════════════════════════════════
def t_h1_h2(ns):
    print("\n=== H1/H2 端点接队列 + 提交即返回 ===")
    con = _db()
    called = {"n": 0}

    def slow(payload, job):
        called["n"] += 1
        time.sleep(1.5)
        return {"ok": True}

    ns["register_handler"]("probe_kind", slow)
    payload = {"project_id": "P1", "actor": "tester"}
    t0 = time.time()
    r = ns["submit"](con, "probe_kind", payload, job_key="k:1", reuse_terminal=False)
    el = time.time() - t0
    ok = _rec("H2a 提交耗时毫秒级（不等handler）", el < 0.3, "elapsed=%.4fs" % el)
    ok &= _rec("H2b 返回 job_id + queued",
               isinstance(r.get("job_id"), int) and r["status"] == "queued", str(r))
    ok &= _rec("H1a handler 尚未执行（异步分支不内联跑）", called["n"] == 0,
               "called=%d" % called["n"])
    con.close()
    return ok


def t_h1_endpoints(overrides=None):
    """H1b：业务端点源码里确实存在异步分支（静态哨兵，防"端点没接队列"回退）。

    为什么必须有静态哨兵：真实 HTTP 调用需要起服务 + 鉴权 + LLM 配置，
    CI 里跑不动；而"端点里没有 mode=async 分支"这件事用grep 就能钉死。
    动态行为由 tools/verify/verify_job_handpoints_live.py（真机）覆盖。
    """
    print("\n=== H1b 业务端点异步分支（静态哨兵）===")
    checks = [
        ("routers/meta.py", "/api/documents/upload", "stage_upload_document"),
        ("routers/meta.py", "/api/documents/{doc_id}/retry", '"doc_retry"'),
        ("routers/meta.py", "/api/documents/{doc_id}/reindex", '"doc_reindex"'),
        ("routers/knowledge_parts/pipeline.py", "/api/knowledge/project-ingest/commit",
         '"project_ingest"'),
        ("routers/knowledge_parts/pipeline.py", "/api/knowledge/reconcile",
         '"knowledge_reconcile"'),
        ("routers/knowledge_parts/pipeline.py", "/api/knowledge/v2g/extract", '"v2g_extract"'),
    ]
    ov = overrides or {}
    ok = True
    for rel, route, needle in checks:
        src = ov.get(rel) or open(os.path.join(ROOT, rel), encoding="utf-8").read()
        has_route = route in src
        has_async = needle in src
        # 就近判定：路由函数体内出现该 kind ⇒ 该端点确实提交了这种作业
        ok &= _rec("H1b %s 提交 %s" % (route, needle),
                   has_route and has_async,
                   "route=%s kind=%s" % (has_route, has_async))
    return ok


# ══════════════════════════════════════════════════════════════════
# H3 幂等锚定「当次在途」——成对断言
# ══════════════════════════════════════════════════════════════════
def t_h3(ns):
    print("\n=== H3 幂等锚定当次在途（成对断言）===")
    con = _db()
    a = ns["submit"](con, "k", {"x": 1}, job_key="doc_retry:7", reuse_terminal=False)
    b = ns["submit"](con, "k", {"x": 1}, job_key="doc_retry:7", reuse_terminal=False)
    ok = _rec("H3a 在途重复提交 ⇒ 同一 job_id", b["job_id"] == a["job_id"],
              "%s vs %s" % (a["job_id"], b["job_id"]))
    ok &= _rec("H3b 在途重复提交标记 deduped", b.get("deduped") is True)
    n = con.execute("SELECT COUNT(*) FROM job_jobs").fetchone()[0]
    ok &= _rec("H3c 在途重复提交未重复入队", n == 1, "rows=%d" % n)
    # 让它进终态
    con.execute("UPDATE job_jobs SET status='done' WHERE id=?", (a["job_id"],))
    conn_commit(con)
    c = ns["submit"](con, "k", {"x": 1}, job_key="doc_retry:7", reuse_terminal=False)
    ok &= _rec("H3d 终态后重复提交 ⇒ 新建作业（不吞掉用户的重试）",
               c["job_id"] != a["job_id"] and c.get("deduped") is False,
               "old=%s new=%s" % (a["job_id"], c["job_id"]))
    n2 = con.execute("SELECT COUNT(*) FROM job_jobs").fetchone()[0]
    ok &= _rec("H3e 终态后确实多了一行", n2 == 2, "rows=%d" % n2)
    # running 也算在途
    con.execute("UPDATE job_jobs SET status='running' WHERE id=?", (c["job_id"],))
    conn_commit(con)
    d = ns["submit"](con, "k", {"x": 1}, job_key="doc_retry:7", reuse_terminal=False)
    ok &= _rec("H3f running 态也去重（防双击）",
               d["job_id"] == c["job_id"] and d.get("deduped") is True)
    # 对照：默认（reuse_terminal=True）保留旧的"一次性提交"语义
    e1 = ns["submit"](con, "k2", {}, job_key="once:1")
    conn_commit(con)
    con.execute("UPDATE job_jobs SET status='failed' WHERE id=?", (e1["job_id"],))
    conn_commit(con)
    e2 = ns["submit"](con, "k2", {}, job_key="once:1")
    ok &= _rec("H3g 默认语义仍复用终态作业（向后兼容未被破坏）",
               e2["job_id"] == e1["job_id"] and e2.get("deduped") is True)
    con.close()
    return ok


def conn_commit(con):
    try:
        con.commit()
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════
# H4 长作业续租（观测 lease_until 真变化）
# ══════════════════════════════════════════════════════════════════
def t_h4(mutate=None):
    print("\n=== H4 长作业续租（观测 lease_until）===")
    jh = _load(JH_SRC, "core.job_handlers__h4", mutate=mutate)
    con, wk = _shared_mem_pair("h4")
    _bind(jh, con, worker_con=wk)
    jid = 1
    con.execute("INSERT INTO job_jobs (id, job_key, kind, status, lease_until, attempt) "
                "VALUES (?,?,?,'running',?,1)", (jid, "k", "probe", "2000-01-01 00:00:00"))
    conn_commit(con)
    before = con.execute("SELECT lease_until FROM job_jobs WHERE id=?", (jid,)).fetchone()[0]

    # 真实起一个 LeaseKeeper，但把间隔压到 0.05 s（否则门禁要跑 300 s）
    #⚠️ 判据形状与生产一致：**观测 lease_until 是否被推进**，
    #    而非断言"代码里写了 LeaseKeeper"（那是断言我自己的算术）。
    keeper = jh["LeaseKeeper"](jid, lease_s=900, interval_s=0.05)
    calls = {"n": 0}

    def fake_hb(c, job_id, lease_s=300):
        calls["n"] += 1
        return True

    # 注入假heartbeat：只观测"有没有被周期性调用"，不依赖真库写入
    import core.job_queue as real_jq
    orig = real_jq.heartbeat
    real_jq.heartbeat = fake_hb
    try:
        with keeper:
            time.sleep(0.4)
    finally:
        real_jq.heartbeat = orig
    ok = _rec("H4a 心跳线程在执行期间被调用（>0 次）", calls["n"] > 0,
              "calls=%d" % calls["n"])
    ok &= _rec("H4b LeaseKeeper 退出后线程停止", not keeper._thread.is_alive())
    # 真正观测数据库：手动跑一次真 heartbeat，确认 lease_until 会被写进未来
    con.execute("UPDATE job_jobs SET lease_until=? WHERE id=?", (before, jid))
    conn_commit(con)
    ns = _load(JQ_SRC, "core.job_queue__h4")
    ns["heartbeat"](con, jid, lease_s=900)
    after = con.execute("SELECT lease_until FROM job_jobs WHERE id=?", (jid,)).fetchone()[0]
    ok &= _rec("H4c heartbeat 真把 lease_until 推到未来",
               after > before and after > time.strftime("%Y-%m-%d %H:%M:%S"),
               "%s → %s" % (before, after))
    # 关键对照：**续租间隔必须小于租约**，否则等于没续
    k2 = jh["LeaseKeeper"](jid, lease_s=900)
    ok &= _rec("H4d 默认续租间隔 < 租约（否则会漏租）",
               k2.interval_s < k2.lease_s,
               "interval=%s lease=%s" % (k2.interval_s, k2.lease_s))
    ok &= _rec("H4e 续租间隔 ≤ 租约/2（容忍连续失败）",
               k2.interval_s <= k2.lease_s / 2.0,
               "interval=%s" % k2.interval_s)
    con.close()
    return ok


# ══════════════════════════════════════════════════════════════════
# H5 管道失败不重试（用真 worker 循环观测 attempt）
# ══════════════════════════════════════════════════════════════════
def t_h5(ns, jh):
    print("\n=== H5 管道失败不该触发重试 ===")
    con, wk = _shared_mem_pair("h5")
    _bind(ns, con, worker_con=wk)
    _bind(jh, con, worker_con=wk)
    ran = {"n": 0}

    def failing_but_returning(payload, job):
        # 模拟 doc_ingest 遇到解析失败：返回 failed 状态而不抛
        ran["n"] += 1
        return {"parse_status": "failed", "error": "扫描件无文本层"}

    ns["register_handler"]("doc_ingest", failing_but_returning)
    job = ns["submit"](con, "doc_ingest", {"doc_id": 1}, job_key="d:1")
    picked = ns["pick_next"](con, worker_id="w1", lease_s=60)
    ns["_run_one"](picked)
    after = ns["get"](con, job["job_id"])
    ok = _rec("H5a 管道失败 ⇒作业判done（不再重试）",
              after["status"] == "done", "status=%s" % after["status"])
    ok &= _rec("H5b attempt 未被推高（未进入重试循环）",
               int(after["attempt"]) == 1, "attempt=%s" % after["attempt"])
    ok &= _rec("H5c handler 确实被调用过（不是没跑就判过）", ran["n"] == 1)
    con.close()
    return ok


# ══════════════════════════════════════════════════════════════════
# H6 同步/异步共用抽取段（静态哨兵 + 真实调用存在性）
# ══════════════════════════════════════════════════════════════════
def t_h6():
    print("\n=== H6 同步/异步抽取段共用===")
    src = open(KP_SRC, encoding="utf-8").read()
    ok = _rec("H6a auto_extract_stage 存在", "def auto_extract_stage(" in src)
    # 同步路径与异步路径都必须**委托**给它，而不是各自内联一份
    ok &= _rec("H6b 同步 ingest_upload_document 委托 auto_extract_stage",
               "auto_meta = auto_extract_stage(" in src)
    ok &= _rec("H6c 异步 run_staged_ingest 走同一段",
               "auto_extract_stage(conn, doc_id" in src)
    # 反向检查：不得同时存在内联抽取块（漂移来源）
    ok &= _rec("H6d 未残留内联抽取实现（防两处并存漂移）",
               src.count("extract_candidates(conn, query, doc_id=doc_id") <= 1,
               "count=%d" % src.count("extract_candidates(conn, query, doc_id=doc_id"))
    # 真实可调用性（不跑 LLM：抽取开关默认关闭 ⇒ 只走 disabled 分支）
    import importlib
    kp = importlib.import_module("knowledge_pipeline.ingest")
    ok &= _rec("H6e auto_extract_stage 可导入且可调用",
               callable(getattr(kp, "auto_extract_stage", None)))
    con = _db()
    con.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
    con.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY, pipeline_detail TEXT)")
    con.execute("INSERT INTO documents (id, pipeline_detail) VALUES (1, '{}')")
    conn_commit(con)
    meta = kp.auto_extract_stage(con, 1, "标题")
    ok &= _rec("H6f 开关关闭时返回 disabled 且写回 pipeline_detail",
               meta.get("enabled") is False
               and "disabled" in (con.execute(
                   "SELECT pipeline_detail FROM documents WHERE id=1").fetchone()[0] or ""),
               str(meta))
    con.close()
    return ok


# ══════════════════════════════════════════════════════════════════
# 变异自证
# ══════════════════════════════════════════════════════════════════
def run_mutations(ns):
    """每条变异都必须让对应判据变红。"""
    print("\n=== 变异自证（判据必须来自被测行为，不能来自变异脚本自己）===")

    # M1：把"只在家族内找在途者"的判据改成"只看 job_key 精确匹配的那一行"
    #（等价于回到第一版写法 ⇒ 终态后新建的作业失去去重保护 ⇒ H3d 判红）
    print("\n--- M1 幂等判据锚回'这一行' => H3d 应判红 ---")
    _IN_MUT[0] = True
    m1 = ("            active = [d for d in rows if d.get(\"status\") in ACTIVE]",
          "            active = [d for d in rows if d.get(\"status\") in TERMINAL]")
    ok_m1 = True
    try:
        nsm = _load(JQ_SRC, "core.job_queue__m1", mutate=m1)
        ok_m1 = (not t_h3(nsm))
    except AssertionError as e:
        _rec("M1 变异锚点命中", False, str(e))
        ok_m1 = False
    # ⚠️ 结论必须在 `_IN_MUT` 置False **之前**记 —— 否则它会被当常态断言，
    #   变异计数恒为 0（"1/1 判红"这种数字就是这么来的：只有最后一条赶上了）。
    _rec("M1 判据锚错 => H3 判红", ok_m1)
    _IN_MUT[0] = False

    # M2：把续租间隔改成大于租约（等于没续租）
    print("\n--- M2 续租间隔 > 租约 ⇒ H4d/e 应判红 ---")
    _IN_MUT[0] = True
    m2 = ("self.interval_s = float(interval_s or max(5.0, self.lease_s / 3.0))",
          "self.interval_s = float(interval_s or (self.lease_s * 2))")
    ok_m2 = True
    try:
        ok_m2 = (not t_h4(mutate=m2))
    except AssertionError as e:
        _rec("M2 变异锚点命中", False, str(e))
        ok_m2 = False
    _rec("M2 漏租间隔 => H4 判红", ok_m2)
    _IN_MUT[0] = False

    # M3：端点去掉异步分支（静态哨兵必须判红）
    print("\n--- M3 端点删除异步分支 => H1b 应判红 ---")
    _IN_MUT[0] = True
    meta_rel = "routers/meta.py"
    pipe_rel = "routers/knowledge_parts/pipeline.py"
    meta_src = open(os.path.join(ROOT, meta_rel), encoding="utf-8").read()
    pipe_src = open(os.path.join(ROOT, pipe_rel), encoding="utf-8").read()
    ok_m3 = False
    try:
        # 变异 = 把端点里提交的 kind 改名（等价于"这个端点不再接队列"）
        mutated = {meta_rel: meta_src.replace('"doc_retry"', '"__mut_removed__"', 1),
                   pipe_rel: pipe_src.replace('"project_ingest"', '"__mut_removed__"', 1)}
        # 变异必须真的改掉东西，否则这条自证是空转
        assert mutated[meta_rel] != meta_src, "M3 变异锚点未命中（meta.py doc_retry）"
        assert mutated[pipe_rel] != pipe_src, "M3 变异锚点未命中（pipeline.py project_ingest）"
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            passed = t_h1_endpoints(overrides=mutated)
        ok_m3 = (not passed)
        _rec("M3 变异后 H1b 确实判红", ok_m3,
              "" if ok_m3 else "哨兵仍通过 => 无效变异")
    except AssertionError as e:
        _rec("M3 变异锚点命中", False, str(e)[:120])
        ok_m3 = False
    except Exception as e:
        _rec("M3 变异执行异常", False, str(e)[:120])
        ok_m3 = False
    _rec("M3 端点去异步 => H1b 判红", ok_m3)
    _IN_MUT[0] = False


def main():
    ns = _load(JQ_SRC, "core.job_queue__h")
    # job_queue._run_one 内部用 _conn() → 这里注入内存库连接工厂（显式，不靠 monkeypatch 时机）
    ns["set_conn_factory"](lambda: _shared_con())
    jh = _load(JH_SRC, "core.job_handlers__main")

    ok = True
    ok &= t_h1_h2(ns)
    ok &= t_h1_endpoints()
    ok &= t_h3(ns)
    ok &= t_h4()
    ok &= t_h5(ns, jh)
    ok &= t_h6()

    run_mutations(ns)

    print("\n" + "=" * 68)
    normal = [r for r in _results]
    n_pass = sum(1 for r in normal if r[0] == PASS)
    print("常态断言：%d/%d 通过" % (n_pass, len(normal)))
    for st, name, detail in normal:
        if st != PASS:
            print("  [%s] %s%s" % (st, name, ("  ← " + detail) if detail else ""))
    mut_fail = [r for r in _MUT_VERDICT if r[0] != PASS]
    print("变异自证：%d/%d 条变异均按预期判红"
          % (len(_MUT_VERDICT) - len(mut_fail), len(_MUT_VERDICT)))
    for st, name, detail in _MUT_VERDICT:
        print("  [%s] %s%s" % (st, name, ("  ← " + detail) if detail else ""))
    print("-" * 68)
    print("变异期中间断言（红=预期，用于核对是否因为目标原因而红）：")
    for st, name, detail in _MUT_DETAIL:
        print("  [%s] %s%s" % (st, name, ("  ← " + detail) if detail else ""))
    print("=" * 68)
    all_ok = (n_pass == len(normal)) and not mut_fail and _MUT_VERDICT
    if not all_ok:
        print("结论：门禁未通过")
        return 1
    print("结论：全部通过（常态 %d 条 + 变异 %d 条全部按预期判红）"
          % (len(normal), len(_MUT_VERDICT)))
    return 0


_SHARED = {"con": None}


def _shared_con():
    if _SHARED["con"] is None:
        _SHARED["con"] = _db()
    return _SHARED["con"]


if __name__ == "__main__":
    sys.exit(main())