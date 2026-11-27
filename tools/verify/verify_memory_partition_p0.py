# -*- coding: utf-8 -*-
"""记忆「分型」P0/P1 落地门禁（2026-11-27）。

锁四件事，每件都对应方案里一条会被"接了线但没生效"骗过去的验收：

  [1] `apply_candidates` 的 **merge / promote / demote / revive** 分支
      此前只处理 `correct` ⇒ dream 产出的四分之三动作**批准后一条都不落库**（候选包看着有建议）。
      判据：① 无 HIL 一个字节都不写；② 有 HIL 时四条动作各自落到位；
            ③ **正文一字不变**（影子语义）；④ detail 计数与实际行状态一致。
  [2] `deposit` 的**写前近重复闸 + 写时口径取代 + 双时态**
      判据：① 同 agent 同 topic 且余弦≥0.95 ⇒ 不新增、只记访问；
            ② 冷层条目被再次写入 ⇒ **复活**（不新增副本）；
            ③ 新内容含互斥断言词且相似度在 [0.85,0.95) ⇒ 旧条被 superseded 且 `valid_to` 关闭；
            ④ 被取代/过期的条目不进召回。
  [3] `memory_maintain` / `memory_dream` **作业注册 + 周期调度**
      判据：① 两个 kind 在 `KINDS` 与 `describe()` 里都在（前端可见）；
            ② `tick()` 首次提交、未到点不重复提交、到点再提交；
            ③ 调度器**不靠内存状态**判"跑过没有"（换一个新连接/新进程照样不重复提交）。
  [4] 记忆注入**去重**（`context.py` 只留条数+主题）
      判据：① 检索块不再出现记忆正文（正文只在长期记忆块）；
            ② 记忆部分字符数下降 ≥40%（方案 P1-5 的验收值）；
            ③ 切回 `memory_context_block=full` 时逐字回到旧行为（可回滚）。

变异自证（每条都必须让对应判据翻红）：
  M1 exec `memory_dream` 去掉 merge 的落库语句      ⇒ [1] 红
  M2 exec `memory_service` 把近重复闸关掉           ⇒ [2] 红
  M3 exec `memory_scheduler` 忽略历史（恒提交）     ⇒ [3] 红
  M4 把注入开关切回 full                            ⇒ [4] 红

用法： ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_memory_partition_p0.py
"""
import inspect
import json
import os
import sqlite3
import sys
import textwrap
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.chdir(ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools", "verify"))

from _tmpdir import mkdtemp  # noqa: E402  共享：创建即登记，atexit 自动回收

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s%s" % ("PASS" if cond else "FAIL", name,
                         ("  <- " + str(detail)) if detail else ""), flush=True)


# ══════════════════════════════════════════════════════════════════════
# 夹具：最小 agent_memory + dream 需要的三张流水表；跑真迁移补全新列
# ══════════════════════════════════════════════════════════════════════
_AM_DDL = """
CREATE TABLE agent_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    mem_type TEXT DEFAULT 'fact',
    content TEXT DEFAULT '',
    embedding TEXT DEFAULT '[]',
    embed_version TEXT DEFAULT '',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    source TEXT DEFAULT 'agent',
    relevance REAL DEFAULT 1.0,
    activation REAL DEFAULT 1.0,
    access_count INTEGER DEFAULT 0,
    last_accessed_at TEXT DEFAULT '',
    forgotten INTEGER DEFAULT 0,
    mem_topic TEXT DEFAULT '',
    scope_type TEXT DEFAULT '',
    scope_id TEXT DEFAULT '',
    tier TEXT DEFAULT 'recall',
    verify_state TEXT DEFAULT 'unverifiable',
    evidence_ref TEXT DEFAULT '',
    evidence_note TEXT DEFAULT '',
    source_credibility REAL,
    superseded_by INTEGER DEFAULT 0,
    superseded_at TEXT DEFAULT ''
)"""


def fixture(with_flow=True):
    d = mkdtemp(prefix="mempart_")
    conn = sqlite3.connect(os.path.join(d, "t.db"))
    conn.row_factory = sqlite3.Row
    conn.executescript(_AM_DDL + ";")
    if with_flow:
        conn.executescript("""
            CREATE TABLE messages (id INTEGER PRIMARY KEY, conversation_id INTEGER DEFAULT 1,
                role TEXT DEFAULT 'user', content TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE tool_call_logs (id INTEGER PRIMARY KEY, tool_name TEXT DEFAULT '',
                arguments TEXT DEFAULT '', result TEXT DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE query_trace (id INTEGER PRIMARY KEY, query TEXT DEFAULT '',
                query_raw TEXT, normalized TEXT DEFAULT '', route TEXT DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        """)
    conn.commit()
    # 跑真迁移：补 valid_from / valid_to / archived_at / superseded_by / verify_state
    try:
        from database.migrations.memory import _migrate_memory_layer
        _migrate_memory_layer(conn)
    except Exception as e:  # noqa: BLE001
        print("    （迁移跳过：%s: %s）" % (type(e).__name__, str(e)[:80]))
    return conn


def exec_module(path, mutate=None, name="mut"):
    """读源码 →（可选）文本变异 → exec 成独立模块命名空间。"""
    src = open(os.path.join(ROOT, path), encoding="utf-8").read()
    if mutate:
        src = mutate(src)
    ns = {"__name__": name, "__file__": os.path.join(ROOT, path)}
    exec(compile(src, path, "exec"), ns)
    return ns


# ══════════════════════════════════════════════════════════════════════
# [1] apply_candidates 四分支
# ══════════════════════════════════════════════════════════════════════
def probe_apply(ns=None):
    """返回各判据布尔值。ns=None 用产品模块。"""
    if ns is None:
        from services.memory_dream import dream, apply_candidates
        ns = {"dream": dream, "apply_candidates": apply_candidates}
    conn = fixture()
    try:
        # 六条记忆：两条同前缀可合并、一条 recall 高频可升 core、一条 core 无访问可降、
        # 一条已归档有访问可复活、一条将被 correct
        rows = [
            (1, "tester", "需求追溯链包含五个环节：来源→分解→分配→关联→验证", "fact", "recall", 0, "2026-11-01 10:00:00", "", "2020-01-01 00:00:00"),
            (2, "tester", "需求追溯链包含五个环节：来源、分解、分配、关联、验证", "fact", "recall", 0, "2026-11-02 10:00:00", "", "2020-01-01 00:00:00"),
            (3, "tester", "满足关系应引用 requirement usage 而非 definition", "fact", "recall", 9, "2026-11-25 10:00:00", "", "2020-01-01 00:00:00"),
            (4, "tester", "早期结论：整型参数可用 String 传递", "fact", "core", 0, "", "", "2020-01-01 00:00:00"),
            (5, "tester", "冷层里的载荷配置经验", "fact", "archival", 6, "2026-11-26 10:00:00", "2026-11-20 10:00:00", "2020-01-01 00:00:00"),
            (6, "tester", "宝可梦中心在山洞东侧，走出即完成", "fact", "recall", 1, "2026-11-20 10:00:00", "", "2020-01-01 00:00:00"),
        ]
        for r in rows:
            conn.execute(
                "INSERT INTO agent_memory (id,agent_id,content,mem_type,tier,access_count,"
                "last_accessed_at,archived_at,created_at) VALUES (?,?,?,?,?,?,?,?,?)", r)
        conn.execute("INSERT INTO messages (id,content) VALUES (1,?)",
                     ("宝可梦中心不在东侧，笔记有误，实际在西侧",))
        conn.execute("INSERT INTO query_trace (id,query,query_raw) VALUES (1,'x','x')")
        conn.commit()
        pkg = ns["dream"](conn, sessions=3, note_limit=60)
        # 造一份"五类动作齐全"的候选包（input_sha256 沿用 dream 的快照，apply 会核它）
        pkg["candidates"] = [
            {"action": "merge", "memory_ids": [1, 2], "keep_id": 2, "topic": "需求追溯链",
             "claim": "需求追溯链", "reason": "同主题同前缀 2 条", "confidence": 0.6},
            {"action": "promote", "memory_id": 3, "to_tier": "core", "reason": "访问 9 次",
             "confidence": 0.7},
            {"action": "demote", "memory_id": 4, "to_tier": "recall", "reason": "core 无访问",
             "confidence": 0.5},
            {"action": "revive", "memory_id": 5, "to_tier": "recall", "reason": "归档后仍被访问",
             "confidence": 0.6},
        ]
        before = {r["id"]: dict(r) for r in conn.execute(
            "SELECT id, content, tier, forgotten, superseded_by, valid_to FROM agent_memory")}
        r_noh = ns["apply_candidates"](conn, pkg)
        after_noh = {r["id"]: dict(r) for r in conn.execute(
            "SELECT id, content, tier, forgotten, superseded_by, valid_to FROM agent_memory")}
        f_nohil = (not r_noh["ok"]) and r_noh["applied"] == 0 and before == after_noh

        r_hil = ns["apply_candidates"](conn, pkg, hil_approved=True, approved_by="gate")
        cur = {r["id"]: dict(r) for r in conn.execute(
            "SELECT id, content, tier, forgotten, superseded_by, valid_to, archived_at "
            "FROM agent_memory")}
        f_merge = (int(cur[1]["superseded_by"] or 0) == 2
                   and int(cur[1]["forgotten"] or 0) == 0          # 合并≠软删
                   and (cur[1]["valid_to"] or "").strip() != "")   # 双时态：有效期关闭
        f_promote = cur[3]["tier"] == "core"
        f_demote = cur[4]["tier"] == "recall"
        f_revive = (cur[5]["tier"] == "recall" and int(cur[5]["forgotten"] or 0) == 0)
        # 正文一字不变（全部 6 条）
        f_content = all(cur[i]["content"] == before[i]["content"] for i in before)
        f_detail = (r_hil.get("detail", {}).get("merge") == 1
                    and r_hil["detail"].get("promote") == 1
                    and r_hil["detail"].get("demote") == 1
                    and r_hil["detail"].get("revive") == 1)
        return {"nohil": f_nohil, "merge": f_merge, "promote": f_promote,
                "demote": f_demote, "revive": f_revive, "content": f_content,
                "detail": f_detail, "res": r_hil}
    finally:
        conn.close()


_PKG = None


def _seed_pkg(conn):
    """给 [2] 用的向量：绕过网络（deposit 内部走 `MemoryService._embed`，测试里打桩）。"""
    return None


def probe_deposit(ns=None):
    """近重复闸 / 复活 / 口径取代 / 双时态。"""
    if ns is None:
        from services.memory_service import MemoryService as MS
    else:
        MS = ns["MemoryService"]
    conn = fixture(with_flow=False)
    # 打桩向量化：**按正文精确映射**到三维向量，避免依赖 embedding 网络、
    # 也避免"用哈希造向量"这种看着确定性、其实相似度不可控的写法
    # （本判据测的是阈值行为，向量必须由测试显式指定）。
    _vecs = {}

    def _fake_embed(_conn, text):
        return list(_vecs.get(str(text), [0.0, 0.0, 1.0])), "test-1"

    _orig_embed = MS._embed
    MS._embed = staticmethod(_fake_embed)
    # ⚠️ 默认 `memory.supersede_on_deposit=False`（写入路径不自动改历史解释权，
    #   那是 `verify_memory_conflict_gate` A5 的硬要求）。本节测的是**能力**是否存在，
    #   故在这段里显式打开；"默认关"由 A5 单独锁 —— 两条成对，缺一都说明不了问题。
    import core.config as _C
    _orig_get = _C.get
    _C.get = lambda s, k, d=None: (True if (s == "memory" and k == "supersede_on_deposit")
                                   else _orig_get(s, k, d))
    try:
        T1 = "需求追溯链包含五个环节：来源→分解→分配"
        T1b = "需求追溯链包含五环节：来源、分解、分配"
        T2 = "完全不同的载荷配置经验：EIRP 上限 30dBW"
        T3 = "旧的载荷上限口径已废弃，新口径按 28dBW 执行"
        # ① 同向量 ⇒ 第二次不新增
        _vecs[T1] = [1.0, 0.0, 0.0]
        _vecs[T1b] = [1.0, 0.0, 0.0]
        i1 = MS.deposit(conn, "a1", T1, mem_topic="追溯")
        i2 = MS.deposit(conn, "a1", T1b, mem_topic="追溯")
        n1 = conn.execute("SELECT COUNT(*) FROM agent_memory").fetchone()[0]
        acc = conn.execute("SELECT access_count FROM agent_memory WHERE id=?", (i1,)).fetchone()[0]
        f_dedup = (i1 is not None and i1 == i2) and n1 == 1 and int(acc) >= 1

        # ② 不同内容 ⇒ 新增
        _vecs[T2] = [0.0, 1.0, 0.0]
        i3 = MS.deposit(conn, "a1", T2, mem_topic="载荷")
        f_new = (i3 != i1) and conn.execute(
            "SELECT COUNT(*) FROM agent_memory").fetchone()[0] == 2

        # ③ 冷层被再次写入 ⇒ 复活而非新增
        conn.execute("UPDATE agent_memory SET tier='archival', forgotten=0 WHERE id=?", (i3,))
        conn.commit()
        i3b = MS.deposit(conn, "a1", T2, mem_topic="载荷")
        r3 = conn.execute("SELECT tier, forgotten FROM agent_memory WHERE id=?", (i3,)).fetchone()
        f_revive = (i3b == i3 and r3["tier"] == "recall" and int(r3["forgotten"]) == 0
                    and conn.execute("SELECT COUNT(*) FROM agent_memory").fetchone()[0] == 2)

        # ④ 口径取代：cos([0.45,0.89,0],[0,1,0]) ≈ 0.892，落在 [0.85, 0.95) ⇒ 取代旧条
        _vecs[T3] = [0.45, 0.89, 0.0]
        i4 = MS.deposit(conn, "a1", T3, mem_topic="载荷")
        r_old = conn.execute("SELECT superseded_by, valid_to FROM agent_memory WHERE id=?",
                             (i3,)).fetchone()
        f_supersede = (i4 not in (None, i3) and int(r_old["superseded_by"] or 0) == i4
                       and (r_old["valid_to"] or "").strip() != "")

        # ⑤ 召回不返回被取代 / 有效期关闭的条
        hits = MS.search(conn, "a1", query="载荷配置经验 上限", top_k=10)
        f_recall = i3 not in {h["id"] for h in hits}
        return {"dedup": f_dedup, "new": f_new, "revive": f_revive,
                "supersede": f_supersede, "recall": f_recall,
                "ids": (i1, i3, i4)}
    finally:
        _C.get = _orig_get
        MS._embed = staticmethod(_orig_embed)
        conn.close()


# ══════════════════════════════════════════════════════════════════════
# [3] 作业注册 + 周期调度
# ══════════════════════════════════════════════════════════════════════
_JOB_DDL = """
CREATE TABLE job_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_key TEXT UNIQUE,
    kind TEXT NOT NULL,
    payload TEXT DEFAULT '{}',
    status TEXT DEFAULT 'queued',
    progress INTEGER DEFAULT 0,
    stage TEXT DEFAULT '',
    attempts INTEGER DEFAULT 0,
    max_attempts INTEGER DEFAULT 3,
    lease_owner TEXT DEFAULT '',
    lease_until TEXT DEFAULT '',
    result TEXT DEFAULT '',
    error TEXT DEFAULT '',
    created_at TEXT DEFAULT '',
    updated_at TEXT DEFAULT ''
)"""


def probe_scheduler(ns=None):
    if ns is None:
        from services import memory_scheduler as ms
        tick = ms.tick
    elif isinstance(ns, dict):
        tick = ns["tick"]          # exec 出来的模块命名空间（变异体）
    else:
        tick = ns.tick
    d = mkdtemp(prefix="memsch_")
    conn = sqlite3.connect(os.path.join(d, "t.db"))
    conn.row_factory = sqlite3.Row
    conn.executescript(_JOB_DDL + ";")
    conn.commit()
    try:
        t0 = time.mktime(time.strptime("2026-11-27 10:00:00", "%Y-%m-%d %H:%M:%S"))
        r1 = tick(conn, now=t0)
        n_after1 = conn.execute("SELECT COUNT(*) FROM job_jobs").fetchone()[0]
        # 立刻再判一次：在途 → 不得重复提交
        r2 = tick(conn, now=t0 + 60)
        n_after2 = conn.execute("SELECT COUNT(*) FROM job_jobs").fetchone()[0]
        # 把首轮作业标终态（模拟 worker 跑完），并在 6h 后判一次 → 应再提交
        conn.execute("UPDATE job_jobs SET status='done'")
        conn.commit()
        # 同一天内再判：维护未到点（6h）⇒ 不提交；dream 当天已跑 ⇒ 不提交
        r3 = tick(conn, now=t0 + 3600)
        n_after3 = conn.execute("SELECT COUNT(*) FROM job_jobs").fetchone()[0]
        # 跨 6h 且跨日（次日 10:00）⇒ 维护到点 + dream 到点，各提交一次
        r4 = tick(conn, now=t0 + 86400)
        n_after4 = conn.execute("SELECT COUNT(*) FROM job_jobs").fetchone()[0]
        kinds = {r[0] for r in conn.execute("SELECT kind FROM job_jobs")}
        # 新连接（模拟另一个进程）同一天再判 ⇒ 仍不重复提交（判据不靠内存状态）
        conn2 = sqlite3.connect(os.path.join(d, "t.db"))
        conn2.row_factory = sqlite3.Row
        try:
            r5 = tick(conn2, now=t0 + 86400 + 120)
            n_after5 = conn2.execute("SELECT COUNT(*) FROM job_jobs").fetchone()[0]
        finally:
            conn2.close()
        return {"first": bool(r1.get("maintain") and r1.get("dream")),
                "inflight": n_after2 == n_after1,
                "not_due": n_after3 == n_after2,
                "due_again": n_after4 == n_after2 + 2,
                "no_mem_state": n_after5 == n_after4,
                "kinds": kinds, "r": (r1, r2, r3, r4, r5),
                "counts": (n_after1, n_after2, n_after3, n_after4, n_after5)}
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════
# [4] 注入去重（检索块只留条数与主题）
# ══════════════════════════════════════════════════════════════════════
_MEM_BODY_A = (
    "需求追溯链五环节：①来源需求（客户/文档给出的原始诉求，必须可指回出处）；②派生需求分解"
    "（按涉众—系统—子系统—单元逐级派生，每一级都要写清父需求编号）；③需求分配到系统元素"
    "（allocate 到具体部件或功能，禁止悬空分配）；④关联实现部件/功能（用 satisfy 指向实现它的"
    "part usage，不指向 part def）；⑤验证闭环（每个需求至少一条 verify 到验证用例，覆盖率为"
    "100% 才算闭环）。缺任何一环，追溯矩阵都会出现断链，评审时按断链条数扣分。")
_MEM_BODY_B = (
    "SysML v2 需求追溯只能用 satisfy 与 verify 两类专用关系：satisfy 表示实现/满足，verify 表示"
    "验证用例覆盖需求；不要自造 relation 名（例如「trace_to」「link」），也不要拿 DERIVES 当"
    "满足关系用。写 satisfy 时被满足的一端必须是 requirement usage（实例），不是 requirement def"
    "（定义），否则模型校验会报 Couldn't resolve reference。需求分配用 allocate X to Y。")


def _retrieval():
    return {"entities": [], "relations": [], "chunk_hits": [], "vector_docs": [],
            "memory_hits": [
                {"content": _MEM_BODY_A, "mem_type": "fact", "score": 0.91,
                 "source_type": "agent_memory", "meta": {"id": 133, "mem_topic": "需求追溯链"}},
                {"content": _MEM_BODY_B, "mem_type": "fact", "score": 0.88,
                 "source_type": "agent_memory", "meta": {"id": 285, "mem_topic": "SysML需求追溯"}},
            ]}


def probe_injection(mode_override=None):
    """返回 (digest 文本, full 文本)。mode_override 为 None 时走真实配置。"""
    from agent.pipeline import AgentPipeline
    import core.config as C
    _orig = C.get

    def _patched(section, key, default=None):
        if section == "context_audit" and key == "memory_context_block" and mode_override:
            return mode_override
        return _orig(section, key, default)

    C.get = _patched
    try:
        p = AgentPipeline()
        return p._build_context(_retrieval(), "需求追溯")
    finally:
        C.get = _orig


# ══════════════════════════════════════════════════════════════════════
# ══════════════════════════════════════════════════════════════════════
# [2b] 遗忘判据必须带「价值维度」——纯时间判据会按日期轮换掉最值钱的记忆
# ══════════════════════════════════════════════════════════════════════
def probe_forget_value(ms=None):
    """夹具：三条同样"久未访问"（100 天），差别只在访问次数。

    真机预演取证（生产库 275 条活跃 / max_unused_days=30）：会归档 28 条，其中
    id=278 access=69、id=317 access=62、id=380 access=49 … 25/28 有访问记录
    ⇒ 纯时间判据 = 按日期轮换，不是 LRU。本组就是钉这一条。
    """
    if ms is None:
        from services.memory_service import MemoryService as ms
    conn = fixture(with_flow=False)
    try:
        _old = "2020-01-01 00:00:00"
        rows = [
            (1, "hot",  "高频旧经验：需求追溯链五环节必须逐环核对", 60),
            (2, "cold", "低频旧经验：某次试验的参数记录格式", 0),
            (3, "mid",  "中频旧经验：视图导出前先校验包结构", 2),
        ]
        for rid, agent, content, acc in rows:
            conn.execute(
                "INSERT INTO agent_memory (id,agent_id,content,mem_type,access_count,"
                "created_at,last_accessed_at,tier,forgotten) VALUES (?,?,?,'fact',?,?,?,'recall',0)",
                (rid, agent, content, acc, _old, _old))
        conn.commit()
        n = ms.forget(conn, max_unused_days=30, max_age_days=0, min_access=0,
                      max_unused_access=2)
        st = {r["id"]: r["tier"] for r in conn.execute(
            "SELECT id, tier FROM agent_memory")}
        # 回滚档：access 不限制 ⇒ 高频也降层（证明 -1 真的关掉了价值维度）
        n2 = 0
        conn.execute("UPDATE agent_memory SET tier='recall'")
        conn.commit()
        n2 = ms.forget(conn, max_unused_days=30, max_age_days=0, min_access=0,
                       max_unused_access=-1)
        st2 = {r["id"]: r["tier"] for r in conn.execute(
            "SELECT id, tier FROM agent_memory")}
        return {"n": n, "st": st, "n2": n2, "st2": st2}
    finally:
        conn.close()


def probe_tier_quota(dm=None):
    """夹具：20 条"该 promote 的高频 recall" + 3 条"归档后又被访问"的冷层。

    真机验证抓到的缺陷（2026-11-27）：`_tier_candidates` 原先是**全局一个 limit 顺序扫**，
    `promote` 一类就能把 6 个名额吃光 ⇒ 冷层复活通道产出恒为 0，而候选包里
    "有 6 条建议"看起来完全正常（只是没有 revive）。
    本组断言：**任何一类都不能挤掉另一类**（revive 独立配额，且优先）。
    """
    if dm is None:
        from services.memory_dream import _tier_candidates as dm
    conn = fixture(with_flow=False)
    try:
        for i in range(1, 21):
            conn.execute(
                "INSERT INTO agent_memory (id,agent_id,content,mem_type,access_count,"
                "last_accessed_at,tier,forgotten) VALUES (?,?,?,'fact',?,?,'recall',0)",
                (i, "hot", "高频经验 %d" % i, 150, "2026-11-26 10:00:00"))
        for i in range(100, 103):
            conn.execute(
                "INSERT INTO agent_memory (id,agent_id,content,mem_type,access_count,"
                "last_accessed_at,archived_at,tier,forgotten) "
                "VALUES (?,?,?,'fact',?,?,?,'archival',0)",
                (i, "cold", "冷层经验 %d" % i, 4, "2026-11-20 10:00:00", "2026-10-10 10:00:00"))
        conn.commit()
        out = dm(conn, limit=6)
        kinds = [c["action"] for c in out]
        return {"n": len(out), "kinds": kinds,
                "revive": sum(1 for k in kinds if k == "revive"),
                "promote": sum(1 for k in kinds if k == "promote"),
                "ids": [c["memory_id"] for c in out if c["action"] == "revive"]}
    finally:
        conn.close()


def probe_forget_idempotent(ms=None):
    """夹具：一条已归档（forgotten=0）+ 一条待归档，连跑两次 forget。

    真机验证抓到的缺陷（2026-11-27）：解耦后归档行的 `forgotten` 就是 0，
    而 `forget` 的筛选条件是 `WHERE forgotten=0` ⇒ **已归档的行每跑一次就被再归档一次**，
    每次都写一条 `archived` 事件。实测连跑两次：事件表 7→9→11，6h 调度下
    每天 4 次 × 若干条 ⇒ 周报「本周归档数」一上线就失真。
    """
    if ms is None:
        from services.memory_service import MemoryService as ms
    conn = fixture(with_flow=False)
    try:
        _old = "2020-01-01 00:00:00"
        conn.execute(
            "INSERT INTO agent_memory (id,agent_id,content,mem_type,access_count,"
            "created_at,last_accessed_at,archived_at,tier,forgotten) "
            "VALUES (1,'a','已归档的老经验','fact',0,?,?,?,'archival',0)",
            (_old, _old, "2026-11-01 00:00:00"))
        conn.execute(
            "INSERT INTO agent_memory (id,agent_id,content,mem_type,access_count,"
            "created_at,last_accessed_at,tier,forgotten) "
            "VALUES (2,'a','待归档的低频经验','fact',0,?,?,'recall',0)", (_old, _old))
        conn.commit()
        n1 = ms.forget(conn, max_unused_days=30, max_age_days=0, min_access=0,
                       max_unused_access=2)
        e1 = conn.execute("SELECT COUNT(*) FROM memory_events WHERE kind='archived'").fetchone()[0]
        n2 = ms.forget(conn, max_unused_days=30, max_age_days=0, min_access=0,
                       max_unused_access=2)
        e2 = conn.execute("SELECT COUNT(*) FROM memory_events WHERE kind='archived'").fetchone()[0]
        return {"n1": n1, "e1": e1, "n2": n2, "e2": e2,
                "tier": {r["id"]: r["tier"] for r in conn.execute("SELECT id,tier FROM agent_memory")}}
    finally:
        conn.close()


def probe_maintain_idempotent(ms=None):
    """夹具：一对高频重复 + 一条待归档，连跑两次 maintain()。

    真机验证抓到的缺陷（2026-11-27，第二次）：`consolidate()` 的输入面**没排除已被取代的行**
    ⇒ 同一批对每跑一次就被"再合并"一次。后果三层：
      · `mark_superseded` 重执行 `valid_to=datetime('now')` ⇒ **"这条何时失效"被一路往后推**，
        双时态（P1-7）的时间线被维护任务自己篡改；
      · `memory_events` 每次 +2×对数 ⇒ 周报「本周合并数」翻倍；
      · 6h 调度下一天 4 次 ⇒ 一周数百条假事件。
    实测取证：真库连跑两次，事件 36 → 84，merged 恒为 12。
    """
    if ms is None:
        from services.memory_service import MemoryService as ms
    conn = fixture(with_flow=False)
    try:
        # ⚠️ `last_accessed_at` 必须用**现在**：`maintain()` 会先跑时效判据，
        #   用 2020 年的日期会让两行先被归档（archived=2），合并根本没机会发生 ⇒ F2f 空转。
        #   （实测：这一版第一稿就是这么红的，红的原因不是实现坏，是夹具不自洽。）
        for rid, txt in ((1, "需求追溯链包含五个环节：来源分解分配关联验证"),
                         (2, "需求追溯链包含五个环节：来源分解分配关联验证。")):
            conn.execute(
                "INSERT INTO agent_memory (id,agent_id,content,mem_type,created_at,"
                "last_accessed_at,tier,forgotten) "
                "VALUES (?,?,?,'fact',datetime('now'),datetime('now'),'recall',0)",
                (rid, "a", txt))
        conn.commit()
        r1 = ms.maintain(conn)
        snap1 = {r["id"]: (r["superseded_by"], r["superseded_at"], r["valid_to"])
                 for r in conn.execute(
                     "SELECT id,superseded_by,superseded_at,valid_to FROM agent_memory")}
        e1 = conn.execute("SELECT COUNT(*) FROM memory_events").fetchone()[0]
        r2 = ms.maintain(conn)
        snap2 = {r["id"]: (r["superseded_by"], r["superseded_at"], r["valid_to"])
                 for r in conn.execute(
                     "SELECT id,superseded_by,superseded_at,valid_to FROM agent_memory")}
        e2 = conn.execute("SELECT COUNT(*) FROM memory_events").fetchone()[0]
        return {"r1": r1, "r2": r2, "e1": e1, "e2": e2,
                "same": snap1 == snap2, "snap": snap2}
    finally:
        conn.close()


def main() -> int:
    print("=" * 78)
    print("[1] apply_candidates：merge / promote / demote / revive")
    r = probe_apply()
    check("F1a 无 HIL ⇒ 拒绝且 0 写入（六条全部状态不变）", r["nohil"])
    check("F1b merge ⇒ 非保留条 superseded_by=keep，且**不是** forgotten=1（合并≠软删）",
          r["merge"])
    check("F1c promote ⇒ tier=core", r["promote"])
    check("F1d demote ⇒ tier=recall", r["demote"])
    check("F1e revive ⇒ 冷层回 recall 且 forgotten=0", r["revive"])
    check("F1f 正文一字不变（影子语义，可回退）", r["content"])
    check("F1g detail 四类计数各为 1（数字与行状态一致）", r["detail"], r["res"].get("detail"))

    print("\n[2] deposit：近重复闸 / 复活 / 口径取代 / 双时态")
    d = probe_deposit()
    check("F2a 同 agent+同 topic 余弦≈1 ⇒ 不新增，只记访问", d["dedup"], d["ids"])
    check("F2b 不同内容 ⇒ 正常新增", d["new"])
    check("F2c 冷层被再次写入 ⇒ 复活回 recall（不产生副本）", d["revive"])
    check("F2d 新条含互斥词且 0.85≤cos<0.95 ⇒ 旧条 superseded + valid_to 关闭",
          d["supersede"], d["ids"])
    check("F2e 被取代的旧条不再进召回", d["recall"])

    print("\n[3] 作业注册 + 周期调度")
    from services.job_handlers import KINDS, describe
    check("F3a KINDS 已注册 memory_maintain / memory_dream",
          {"memory_maintain", "memory_dream"} <= set(KINDS))
    _desc = describe()
    check("F3b describe() 可见（前端按 group 动态渲染才有入口）",
          "memory_maintain" in _desc and "memory_dream" in _desc
          and bool(_desc["memory_maintain"].get("group")))
    s = probe_scheduler()
    check("F3c 首次 tick 各提交一次（维护 + 做梦）", s["first"], s["r"][0])
    check("F3d 在途时重复 tick 不重复提交", s["inflight"], s["counts"])
    check("F3e 未到点（1h 内、同日）不提交", s["not_due"], s["counts"])
    check("F3f 跨 6h 且跨日 ⇒ 维护与做梦各再提交一次", s["due_again"], s["counts"])
    check("F3g 判据不靠内存状态（换新连接同日仍不重复提交）", s["no_mem_state"], s["counts"])

    print("\n[3b] 遗忘判据的价值维度（高频旧经验不得被时间判据降层）")
    fv = probe_forget_value()
    check("F3h 久未访问 + 低频(access≤2) ⇒ 降冷层", fv["st"].get(2) == "archival", fv["st"])
    check("F3i 久未访问但高频(access=60) ⇒ **不降层**（防按日期轮换）",
          fv["st"].get(1) == "recall", fv["st"])
    check("F3j 边界值 access==2 ⇒ 降层（判据是 ≤ 不是 <）", fv["st"].get(3) == "archival", fv["st"])
    check("F3k max_unused_access=-1 回滚档 ⇒ 高频也降层（能力可关，不是焊死）",
          fv["st2"].get(1) == "archival", fv["st2"])

    print("\n[3c] 分层候选的配额（promote 不得挤掉 revive）")
    q = probe_tier_quota()
    check("F3l promote 满额场景下 revive 仍产出（分桶配额，不是全局 limit）",
          q["revive"] >= 1 and q["promote"] >= 1, q)
    check("F3m 总条数不超过 limit", q["n"] <= 6, q)

    print("\n[3d] forget 幂等（已归档的行不得被反复再归档）")
    fi = probe_forget_idempotent()
    check("F3n 首次只归档待归档那一条（n=1），已归档的不重算",
          fi["n1"] == 1 and fi["e1"] == 1, fi)
    check("F3o 连跑第二次 n=0 且**不新增事件**（6h 调度下不得刷假数）",
          fi["n2"] == 0 and fi["e2"] == fi["e1"], fi)

    print("\n[2b] maintain 幂等（重复跑不得改写取代时间线 / 不得重复记账）")
    mi = probe_maintain_idempotent()
    check("F2f 首次确实合并了一对（判据非空转）", mi["r1"].get("merged") == 1, mi["r1"])
    check("F2g 第二次 merged=0（已取代的行不再参与合并）", mi["r2"].get("merged") == 0, mi["r2"])
    check("F2h 第二次不新增任何事件（6h 调度下不得刷假流动数）",
          mi["e2"] == mi["e1"], (mi["e1"], mi["e2"]))
    check("F2i superseded_at / valid_to 第二次**一字不变**（双时态不被维护篡改）",
          mi["same"], mi["snap"])

    print("\n[4] 注入去重（检索块 = 条数 + 主题）")
    _digest = probe_injection()
    _full = probe_injection("full")
    check("F4a 检索块不再出现记忆正文（正文只注入一次）",
          (_MEM_BODY_A not in _digest) and (_MEM_BODY_B not in _digest)
          and ("已沉淀记忆 2 条" in _digest), _digest[:120])
    check("F4b 主题进入检索块（导航信息未丢）",
          "需求追溯链" in _digest and "SysML需求追溯" in _digest)
    _drop = 1 - (len(_digest) / max(1, len(_full)))
    check("F4c 记忆块字符下降 ≥40%（方案 P1-5 验收值）", _drop >= 0.40,
          "digest=%d / full=%d / 降幅=%.1f%%" % (len(_digest), len(_full), _drop * 100))
    # 注意：full 档按 `memory_max_chars`（200）截断，所以只能比**正文前缀**，
    # 不能比整段（正文比 200 字长，整段必然不在 —— 那会把"截断生效"误判成"没回到旧行为"）。
    check("F4d full 档回到旧行为（正文前缀出现）",
          (_MEM_BODY_A[:40] in _full) and (_MEM_BODY_B[:40] in _full))

    print("\n[5] 变异自证（每条都必须让对应判据翻红）")
    # M1：去掉 merge 分支的落库语句
    def _m1(src):
        return src.replace(
            "                if MemoryService.mark_superseded(conn, _mid, _keep):\n"
            "                    _n += 1", "                if False:\n                    _n += 1", 1)
    try:
        ns = exec_module("services/memory_dream.py", _m1, "dream_m1")
        mm = probe_apply(ns)
        check("M1 去掉 merge 落库 ⇒ F1b 翻红", mm["merge"] is False, mm["merge"])
    except Exception as e:  # noqa: BLE001
        check("M1 去掉 merge 落库 ⇒ F1b 翻红", False, "%s: %s" % (type(e).__name__, e))

    # M2：近重复闸关掉
    def _m2(src):
        return src.replace("            if _dd_on:", "            if False:", 1)
    try:
        ns = exec_module("services/memory_service.py", _m2, "svc_m2")
        dm = probe_deposit(ns)
        check("M2 关掉近重复闸 ⇒ F2a 翻红", dm["dedup"] is False, dm["dedup"])
    except Exception as e:  # noqa: BLE001
        check("M2 关掉近重复闸 ⇒ F2a 翻红", False, "%s: %s" % (type(e).__name__, e))

    # M3：调度器忽略历史（恒判到点）
    def _m3(src):
        return src.replace("        _due = (_last is None) or ((now - _last) >= _iv_h * 3600)",
                           "        _due = True", 1)
    try:
        ns = exec_module("services/memory_scheduler.py", _m3, "sch_m3")
        sm = probe_scheduler(ns)
        check("M3 忽略历史恒提交 ⇒ F3e 翻红", sm["not_due"] is False, sm["counts"])
    except Exception as e:  # noqa: BLE001
        check("M3 忽略历史恒提交 ⇒ F3e 翻红", False, "%s: %s" % (type(e).__name__, e))

    # M4：注入开关切回 full（把"默认 digest"这个回归量化出来）
    try:
        _full_only = probe_injection("full")
        check("M4 切回 full ⇒ F4a 翻红（正文前缀重新出现）",
              (_MEM_BODY_A[:40] in _full_only) and (_MEM_BODY_B[:40] in _full_only))
    except Exception as e:  # noqa: BLE001
        check("M4 切回 full ⇒ F4a 翻红（正文重新出现）", False,
              "%s: %s" % (type(e).__name__, e))


    # M5：去掉判据 1 的价值维度（回到"只看时间"）⇒ F3i 必须翻红
    def _m5(src):
        return src.replace(
            "                        and (max_unused_access is None or max_unused_access < 0\n"
            "                             or _access <= int(max_unused_access))):",
            "                        ):", 1)
    try:
        ns = exec_module("services/memory_service.py", _m5, "svc_m5")
        fv_m = probe_forget_value(ns["MemoryService"])
        check("M5 去掉价值维度 ⇒ F3i 翻红（高频旧经验被时间判据降层）",
              fv_m["st"].get(1) == "archival", fv_m["st"])
    except Exception as e:  # noqa: BLE001
        check("M5 去掉价值维度 ⇒ F3i 翻红（高频旧经验被时间判据降层）", False,
              "%s: %s" % (type(e).__name__, e))


    # M6：把分桶配额改回"全局 limit、promote 先占" ⇒ F3l 必须翻红
    #   ⚠️ 锚点必须跟着实现走：上一版锚的是 `out = revive_bucket[...]` 两行连写，
    #   实现改成"先算 _acts 再排序"后该串已不存在 ⇒ `_m6` 变空操作，
    #   变异体行为与原实现一模一样，门禁报"判据抓不住"（看起来像判据坏了）。
    #   ⇒ 现在用两处**单行**替换：① revive 不用配额；② revive 排到 promote 之后。
    def _m6(src):
        a = src.replace("    out = revive_bucket[:_REVIVE_QUOTA]",
                        "    out = []", 1)
        b = a.replace('    _acts = [c for c in tier_bucket if c["action"] != "skip"]',
                      '    _acts = [c for c in tier_bucket if c["action"] != "skip"] + revive_bucket',
                      1)
        return b
    try:
        ns = exec_module("services/memory_dream.py", _m6, "dream_m6")
        qm = probe_tier_quota(ns["_tier_candidates"])
        check("M6 改回全局 limit ⇒ F3l 翻红（revive 被 promote 挤光）",
              qm["revive"] == 0, qm)
    except Exception as e:  # noqa: BLE001
        check("M6 改回全局 limit ⇒ F3l 翻红（revive 被 promote 挤光）", False,
              "%s: %s" % (type(e).__name__, e))

    # M7：把 forget 的冷层排除去掉 ⇒ F3o 必须翻红（重复记账）
    #   锚点取**唯一那一行**（`where +=` 冷层条件），不做跨行匹配 —— 跨行锚点被缩进/换行
    #   变动打断时会产出语法错的变异体，报"SyntaxError"看起来像"判据抓到了"，其实是变异打空。
    def _m7(src):
        return src.replace(
            "                where += \" AND (tier IS NULL OR tier != 'archival')\"",
            "                pass", 1)
    try:
        ns = exec_module("services/memory_service.py", _m7, "svc_m7")
        fim = probe_forget_idempotent(ns["MemoryService"])
        check("M7 去掉冷层排除 ⇒ F3o 翻红（同一行被反复再归档）",
              fim["n2"] > 0 and fim["e2"] > fim["e1"], fim)
    except Exception as e:  # noqa: BLE001
        check("M7 去掉冷层排除 ⇒ F3o 翻红（同一行被反复再归档）", False,
              "%s: %s" % (type(e).__name__, e))


    # M8：把 consolidate 的"排除已取代"去掉 ⇒ F2g 必须翻红
    #   锚点取唯一那一行；跨行锚点被注释/换行改动打断时会产出语法错变异体，
    #   报 SyntaxError 看起来像"判据抓到了"，实际是变异打空（本仓记过的同族）。
    def _m8(src):
        return src.replace('                where += " AND COALESCE(superseded_by,0)=0"',
                           "                pass", 1)
    try:
        ns = exec_module("services/memory_service.py", _m8, "svc_m8")
        mim = probe_maintain_idempotent(ns["MemoryService"])
        check("M8 去掉排除已取代 ⇒ F2g 翻红（同一对反复合并、时间线被推后）",
              mim["r2"].get("merged", 0) > 0, mim["r2"])
    except Exception as e:  # noqa: BLE001
        check("M8 去掉排除已取代 ⇒ F2g 翻红（同一对反复合并、时间线被推后）", False,
              "%s: %s" % (type(e).__name__, e))

    print()
    print("=" * 78)
    print("PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("  FAIL:", f)
    print("=" * 78)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
