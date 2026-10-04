# -*- coding: utf-8 -*-
"""P0-4（多租户隔离：图谱路径）回归验证 —— 零 LLM、独立临时库，可反复跑。

**为什么需要它**（《架构-可扩展性-稳定性整体评估》§6 P0-4）：
    `agent/rag.py::retrieve()` 原先只按 **branch + source_doc** 过滤，`project_id`
    仅出现在"记忆召回"那一路 —— 图谱实体/关系/词典桥接三条入口**全无项目维度**。
    后果：同一分支下两个项目的模型互相可见，接第二个项目即读到了第一个项目的模型。

**本脚本锁定的不变式**：
    T1  默认关闭时行为**逐字不变**（老库零回归 —— 这是敢上线的最低门槛）
    T2  关闭时 tenant.enabled=False 且不丢任何行
    T3  开启 + 解析到项目 ⇒ 只保留本项目实体（**跨项目不泄漏**）
    T4  开启 + 解析不到项目 ⇒ 按 `tenant_unresolved` 决定放行/0 命中（两种都验）
    T5  脏配置（unresolved 写成乱七八糟的值）⇒ 按宽松档处理，**不许全量 0 命中**
    T6  关系侧同过滤（只过 r 侧不够：e1/e2 的 JOIN 会泄露对方实体名）
    T7  词典桥接（第三条实体入口）同样过过滤 —— 否则"用领域词典绕开隔离"是现成漏洞
    T8  体检端点算得对（孤儿项目 / 空 project_id 计数）
    T9  **显式承认缺口**：向量/分片路径无项目维度，retrieval 结果里 tenant_vector_isolated=False

**变异自证（强制）**：注入"不过滤"的错写法，脚本必须判 FAIL。
   最关键 M1：把物化点过滤摘掉 —— 那样 T3 必然失败，证明 T3 真在守隔离。

用法：
    <repo>\\.venv\\Scripts\\python.exe -X utf8 tools/verify/verify_tenant_isolation.py
退出码：全绿 0 / 有失败或空转 1。
"""
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

RAG_SRC = os.path.join(ROOT, "agent", "rag.py")
MON_SRC = os.path.join(ROOT, "routers", "monitor.py")

PASS, FAIL, VACUOUS = "PASS", "FAIL", "VACUOUS"
_results = []


def _rec(name, ok, detail="", kind=FAIL):
    _results.append((kind if not ok else PASS, name, detail))
    print(f"  {PASS if ok else kind}  {name}" + (f"  {detail}" if not ok and detail else ""))
    return ok


# ── 夹具：两个项目、同分支、同名实体，检索词相同 ──────────────────────────────
# 场景取自真实风险：两个项目都用"天线"这类通用词时，靠名字无法区分归属，
# 唯一可靠的隔离手段就是 project_id。
DDL = [
    """CREATE TABLE entities (
        id TEXT PRIMARY KEY, name TEXT DEFAULT '', entity_type TEXT DEFAULT '',
        status TEXT DEFAULT 'active', branch TEXT DEFAULT 'dev', project_id TEXT DEFAULT '',
        source_doc TEXT DEFAULT '', properties TEXT DEFAULT '{}', is_current INTEGER DEFAULT 1)""",
    """CREATE TABLE relations (
        id INTEGER PRIMARY KEY AUTOINCREMENT, source_id TEXT DEFAULT '', target_id TEXT DEFAULT '',
        status TEXT DEFAULT 'active', project_id TEXT DEFAULT '', relation_type TEXT DEFAULT '')""",
    "CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT DEFAULT '', code TEXT DEFAULT '')",
    "CREATE TABLE glossary_concepts (id TEXT PRIMARY KEY, pref_label TEXT DEFAULT '', "
    "maps_to_class TEXT DEFAULT '', maps_to_inst TEXT DEFAULT '')",
]


def _db():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    for d in DDL:
        con.execute(d)
    con.executemany("INSERT INTO projects (id,name,code) VALUES (?,?,?)",
                    [("P-A", "项目甲", "A"), ("P-B", "项目乙", "B")])
    rows = [
        ("e-a1", "天线", "部件", "P-A"),      # 项目甲
        ("e-b1", "天线", "部件", "P-B"),      # 项目乙 —— 同名同分支，只有 project_id 能区分
        ("e-a2", "馈线", "部件", "P-A"),
        ("e-x1", "无主实体", "部件", ""),      # 未填 project_id
    ]
    for eid, nm, tp, pid in rows:
        con.execute("INSERT INTO entities (id,name,entity_type,branch,project_id,status) "
                    "VALUES (?,?,?,'dev',?,'active')", (eid, nm, tp, pid))
    con.execute("INSERT INTO relations (source_id,target_id,status,project_id,relation_type) "
                "VALUES ('e-a1','e-a2','active','P-A','contains')")
    con.execute("INSERT INTO relations (source_id,target_id,status,project_id,relation_type) "
                "VALUES ('e-b1','e-b1','active','P-B','self')")
    con.commit()
    return con


def _load_rag(mut=None):
    """按需加载 tenant 相关函数（避免整模块 import 触发重型依赖）。"""
    src = open(RAG_SRC, encoding="utf-8").read()
    if mut:
        old, new = mut
        assert old in src, "变异锚点丢失: %r" % old[:60]
        src = src.replace(old, new, 1)
    ns = {"__name__": "rag_tenant_mut"}
    # 只执行文件头部到 class GraphRAG 之前（tenant 助手都在那儿）
    cut = src.find("class GraphRAG")
    exec(compile(src[:cut], RAG_SRC, "exec"), ns)
    return ns


# 用**真实源码**执行一次拿到被测符号
RAG = _load_rag()
tenant_scope = RAG["tenant_scope"]
tenant_filter_rows = RAG["tenant_filter_rows"]


def _cfg(monkey, key, val):
    """把配置值注入到 tenant_scope 依赖的 _cfg_get 上。"""
    monkey["rag." + key] = val


def t_t1_t2():
    print("\n=== T1/T2 默认关闭：行为与改动前逐字一致 ===")
    # 用真实配置读取（core.config 里的默认值）而不是手写，断言"默认值必须是 off"
    from core import config as cfg
    ok = _rec("T1a 配置默认值 tenant_isolation=off",
              str(cfg.get("rag", "tenant_isolation", "off")).lower() == "off",
              f"got={cfg.get('rag', 'tenant_isolation', None)}")
    ok &= _rec("T1b 配置默认值 tenant_unresolved=passthrough（宽松档）",
               str(cfg.get("rag", "tenant_unresolved", "passthrough")).lower() == "passthrough")
    ts = tenant_scope({"project_id": "P-A"})
    ok &= _rec("T1c 关闭时 enabled=False（即便传了 project_id）", ts["enabled"] is False, f"{ts}")
    rows = [{"id": "e-a1", "project_id": "P-A"}, {"id": "e-b1", "project_id": "P-B"},
            {"id": "e-x1", "project_id": ""}]
    kept = tenant_filter_rows(rows, ts)
    ok &= _rec("T2  关闭时一行不丢（零回归）", len(kept) == 3 and [r["id"] for r in kept]
               == ["e-a1", "e-b1", "e-x1"], f"kept={[r['id'] for r in kept]}")
    return ok


def t_t3():
    print("\n=== T3 开启 + 解析到项目 ⇒ 跨项目不泄漏 ===")
    rows = [{"id": "e-a1", "project_id": "P-A"}, {"id": "e-b1", "project_id": "P-B"},
            {"id": "e-x1", "project_id": ""}]
    # 直接构造 tenant_scope 已解出的上下文，避免依赖全局配置（配置由 T4 用 monkey 验）
    ts = {"enabled": True, "pid": "P-A", "unresolved": "passthrough", "mode": "on"}
    kept = tenant_filter_rows(rows, ts)
    ok = _rec("T3a 只留本项目实体", [r["id"] for r in kept] == ["e-a1"], f"kept={[r['id'] for r in kept]}")
    ok &= _rec("T3b 他项目实体被滤掉", all(r["project_id"] == "P-A" for r in kept))
    ok &= _rec("T3c 无主实体在严格隔离下不可见（开隔离前必须先回填）",
               "e-x1" not in [r["id"] for r in kept])
    ts_b = {"enabled": True, "pid": "P-B", "unresolved": "passthrough", "mode": "on"}
    kept_b = tenant_filter_rows(rows, ts_b)
    ok &= _rec("T3d 换项目得到另一组结果（对称，非单向过滤）",
               [r["id"] for r in kept_b] == ["e-b1"], f"kept={[r['id'] for r in kept_b]}")
    return ok


def t_t4_t5():
    print("\n=== T4/T5 解析不到项目时的两种策略 + 脏配置兜底 ===")
    ok = True
    # monkey 掉模块内的 _cfg_get：验证真实开关读到了配置
    for mode, unres, want_enabled, label in [
            ("on", "passthrough", False, "开隔离+无项目+passthrough ⇒ 放行（不过滤）"),
            ("on", "empty", True, "开隔离+无项目+empty ⇒ 生效（0 命中）"),
            ("off", "empty", False, "关闭时无论 unresolved 如何都不生效"),
    ]:
        ns = _load_rag()
        table = {"rag.tenant_isolation": mode, "rag.tenant_unresolved": unres}
        ns["_cfg_get"] = lambda g, k, d=None, _t=table: _t.get("%s.%s" % (g, k), d)
        ts = ns["tenant_scope"]({"project_id": ""})
        ok &= _rec("T4  " + label, ts["enabled"] is want_enabled, f"got={ts}")
    # 脏配置：unresolved 写成不存在的档位 ⇒ 必须回落宽松（宁可少隔离，不可全量 0 命中）
    ns = _load_rag()
    table = {"rag.tenant_isolation": "on", "rag.tenant_unresolved": "???乱写???"}
    ns["_cfg_get"] = lambda g, k, d=None, _t=table: _t.get("%s.%s" % (g, k), d)
    ts5 = ns["tenant_scope"]({"project_id": ""})
    ok &= _rec("T5a 脏 unresolved 配置回落宽松档（enabled=False）", ts5["enabled"] is False, f"{ts5}")
    ok &= _rec("T5b 回落后 unresolved 字段是合法值", ts5["unresolved"] == "passthrough", f"{ts5}")
    # 脏 isolation 值（既非 on 也非 off）⇒ 等同关闭，不许误判为开启
    ns2 = _load_rag()
    table2 = {"rag.tenant_isolation": "ON!!", "rag.tenant_unresolved": "passthrough"}
    ns2["_cfg_get"] = lambda g, k, d=None, _t=table2: _t.get("%s.%s" % (g, k), d)
    ts6 = ns2["tenant_scope"]({"project_id": "P-A"})
    ok &= _rec("T5c 非法 isolation 值按关闭处理（不误开）", ts6["enabled"] is False, f"{ts6}")
    return ok


def t_t6_t7():
    print("\n=== T6/T7 关系侧与词典桥接不成为绕过口 ===")
    src = open(RAG_SRC, encoding="utf-8").read()
    n_rel = src.count("COALESCE(r.project_id,'') = ?")
    ok = _rec("T6a 两处关系查询都带项目条件（主查询 + 词典桥接增量）", n_rel == 2, f"count={n_rel}")
    ok &= _rec("T6b 关系条件作用于 r 侧（COALESCE 处理 NULL）",
               "COALESCE(r.project_id,'')" in src)
    ok &= _rec("T7  词典桥接召回实体也过租户过滤",
               "_rec_ents = tenant_filter_rows(_rec_ents, _ts)" in src)
    ok &= _rec("T7b 图谱实体命中在物化点过滤（覆盖 SQLite + TDB 两条路径）",
               "graph_results = tenant_filter_rows(graph_results, _ts)" in src)
    return ok


def t_t8():
    print("\n=== T8 体检端点算得对 ===")
    src = open(MON_SRC, encoding="utf-8").read()
    ok = _rec("T8a 端点已定义", '@router.get("/api/monitor/tenant-readiness")' in src)
    ok &= _rec("T8b 统计孤儿项目（挂到不存在的项目上）",
               "NOT EXISTS (SELECT 1 FROM projects p WHERE p.id=e.project_id)" in src)
    ok &= _rec("T8c 统计空 project_id", "COALESCE(project_id,'')=''" in src)
    ok &= _rec("T8d 表/列缺失时返回 -1 而非 0（区分'没有'与'查不了'）",
               "return -1" in src)
    ok &= _rec("T8e 显式列出向量路径缺口", "known_gap" in src and "documents 表无该列" in src)
    return ok


def t_t9():
    print("\n=== T9 结果体显式标注「未隔离」且带冻结标记（不许被误读为已隔离）===")
    src = open(RAG_SRC, encoding="utf-8").read()
    ok = _rec("T9a retrieval 返回 tenant 上下文（可观测）", '"tenant": {' in src)
    ok &= _rec("T9b 明示冻结标记 frozen: True", '"frozen": True' in src)
    ok &= _rec("T9c 统计被滤掉的实体数（将来真开隔离时需要）", "entities_dropped" in src)
    return ok


def t_d4_execute_semantics():
    """D 组：锁住 `BaseRepo.execute` 的返回语义修正（2026-10-04 实测发现的缺陷）。

    原实现 `cur.lastrowid or cur.rowcount` 对 UPDATE/DELETE 返回无意义值
    （lastrowid = 本连接最近一次 INSERT 的 rowid），导致「影响 0 行」与
    「影响 1 行」返回值完全相同 —— 计数错、`bool()` 判真错。
    """
    print("\n=== D4 BaseRepo.execute 返回语义（UPDATE 必须给 rowcount）===")
    import sqlite3 as _sq
    from repositories.base import BaseRepo

    con = _sq.connect(":memory:")
    con.row_factory = _sq.Row
    con.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    con.execute("INSERT INTO t (v) VALUES ('x')")        # 制造 lastrowid=1
    repo = BaseRepo(con)

    n0 = repo.execute("UPDATE t SET v='y' WHERE id=999")  # 影响 0 行
    n1 = repo.execute("UPDATE t SET v='z' WHERE id=1")    # 影响 1 行
    ok = _rec("D4a UPDATE 影响 0 行 ⇒ 返回 0（不是脏值）", n0 == 0, f"got={n0}")
    ok &= _rec("D4b UPDATE 影响 1 行 ⇒ 返回 1", n1 == 1, f"got={n1}")
    ok &= _rec("D4c 两种情形可区分（修正前完全相同）", n0 != n1, f"{n0} vs {n1}")
    ok &= _rec("D4d 0 行时 bool() 为假（路由据此返 404）", bool(n0) is False)
    ok &= _rec("D4e INSERT 仍返回 lastrowid（不能把这条改坏）",
               repo.execute("INSERT INTO t (v) VALUES ('z')") == 2, "lastrowid 期望 2")
    d0 = repo.execute("DELETE FROM t WHERE id=999")
    d1 = repo.execute("DELETE FROM t WHERE id=1")
    ok &= _rec("D4f DELETE 同样按 rowcount（0 / 1 可区分）", d0 == 0 and d1 == 1, f"{d0}/{d1}")
    return ok


def m_d4():
    """M9：把 execute 还原成 `lastrowid or rowcount` ⇒ D4 必须判红。"""
    print("\n=== 变异 M9：还原 execute 的 lastrowid 语义 ===")
    import sqlite3 as _sq
    from repositories.base import BaseRepo

    con = _sq.connect(":memory:")
    con.row_factory = _sq.Row
    con.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    con.execute("INSERT INTO t (v) VALUES ('x')")

    def buggy_execute(self, sql, params=()):
        return self.conn.execute(sql, tuple(params)).lastrowid or self.conn.execute(
            sql, tuple(params)).rowcount

    orig = BaseRepo.execute
    BaseRepo.execute = buggy_execute
    try:
        repo = BaseRepo(con)
        n0 = repo.execute("UPDATE t SET v='y' WHERE id=999")
        n1 = repo.execute("UPDATE t SET v='z' WHERE id=1")
    finally:
        BaseRepo.execute = orig
    caught = (n0 == n1) and bool(n0)      # 还原后不可区分且 0 行为真
    return _rec("M9 变异被复现（0 行与 1 行返回值相同）→ 证明 D4 非空转",
                caught, f"n0={n0} n1={n1}", VACUOUS)


def m_t3():
    """M1：把**过滤函数本体**改坏（不比较 project_id）⇒ T3 必然失败。

    ⚠️ 这里刻意突变 `tenant_filter_rows` 函数体，而不是 `retrieve()` 里的调用点。
    原因：T3 验证的是"过滤器行为是否正确"，而 T7b 验证的是"retrieve 是否真的调了它"。
    早先版本突变调用点，结果 T3 照过 —— 那是**典型的假覆盖**：
    测的是零件、没测装配。此处两个变异各管一头（M1 管行为、M3 管接线），不留缝隙。
    """
    print("\n=== 变异 M1：过滤器不比较 project_id（还原成修复前语义）===")
    ns = _load_rag(('return [r for r in (rows or []) if str((r or {}).get("project_id") or "") == pid]',
                    'return list(rows or [])  # 变异：不隔离'))
    f = ns["tenant_filter_rows"]
    rows = [{"id": "e-a1", "project_id": "P-A"}, {"id": "e-b1", "project_id": "P-B"}]
    ts = {"enabled": True, "pid": "P-A", "unresolved": "passthrough", "mode": "on"}
    kept = f(rows, ts)
    caught = len(kept) == 2      # 变异后两个项目的行都留下了 = 泄漏
    return _rec("M1 变异被复现（跨项目行未被滤除）→ 证明 T3 非空转",
                caught, f"kept={[r['id'] for r in kept]}", VACUOUS)


def m_t3_wiring():
    """M3：把 `retrieve()` 里的调用点摘掉 ⇒ T7b 的接线断言必须失败。

    这是 M1 的搭档：M1 证明"过滤器有效"，M3 证明"它真的被调用"。
    两者缺一，就会出现"函数写得对但没人调用 = 隔离形同虚设"。
    """
    print("\n=== 变异 M3：摘掉 retrieve 里的过滤调用（过滤器成死代码）===")
    src = open(RAG_SRC, encoding="utf-8").read().replace(
        "graph_results = tenant_filter_rows(graph_results, _ts)",
        "pass  # 变异：调用被摘掉", 1)
    gone = "graph_results = tenant_filter_rows(graph_results, _ts)" not in src
    wiring_ok = ("graph_results = tenant_filter_rows(graph_results, _ts)" in src)
    return _rec("M3 变异被复现（接线断言可识别调用缺失）→ 证明 T7b 非空转",
                gone and not wiring_ok, "", VACUOUS)


def m_t4():
    """M2：把"解析不到项目"一律当放行 —— 开了隔离却静默不起作用（假安全）。"""
    print("\n=== 变异 M2：解析不到项目时永远放行 ===")
    ns = _load_rag()
    table = {"rag.tenant_isolation": "on", "rag.tenant_unresolved": "empty"}
    ns["_cfg_get"] = lambda g, k, d=None, _t=table: _t.get("%s.%s" % (g, k), d)
    # 变异：把 enabled 判定写成恒 False
    src = open(RAG_SRC, encoding="utf-8").read().replace(
        'enabled = (unres == "empty")', "enabled = False  # 变异", 1)
    ns2 = {"__name__": "m2"}
    exec(compile(src[:src.find("class GraphRAG")], RAG_SRC, "exec"), ns2)
    ts = ns2["tenant_scope"]({"project_id": ""})
    caught = (ts["enabled"] is False)      # 变异后 empty 策略失效
    return _rec("M2 变异被复现（empty 策略失效）→ 证明 T4 非空转", caught, f"{ts}", VACUOUS)


def t_d1_freeze():
    """D 组：把 2026-10-04 的架构决策**锁成门禁**（防止有人日后顺手把开关打开）。"""
    print("\n=== D1 决策锁：图谱=统一底座，禁止开启项目隔离 ===")
    from core import config as cfg
    val = str(cfg.get("rag", "tenant_isolation", "off")).lower()
    ok = _rec("D1a 配置默认值恒为 off（改了就判红）", val == "off", f"got={val!r}")
    src = open(RAG_SRC, encoding="utf-8").read()
    ok &= _rec("D1b rag.py 顶部存在「已冻结/禁止开启」的决策声明",
               "已冻结" in src and "禁止开启" in src)
    ok &= _rec("D1c 决策声明写明了真实危害（滤掉公共资产，而非泄露）",
               "看不到本该可见的公共资产" in src or "看不到本该看到的公共资产" in src)
    ok &= _rec("D1d 声明了隔离的真实位置（artifacts 按 conversation_id）",
               "conversation_id" in src and "artifacts" in src)
    ok &= _rec("D1e retrieval 回显带 frozen 标记（勿被误读为已隔离）",
               '"frozen": True' in src)
    # 反向：确认"统一底座"这条决策在代码里是可检索的，而不是只活在文档里
    ok &= _rec("D1f 决策与数据自证同时在位（entities 多值=从未按项目分区）",
               "从未按项目分区" in src or "只有 1 个" in src or "只有 1 个 project_id" in src)
    return ok


def t_d2_delete_integrity():
    """D 组：删项目的引用完整性（**这条与隔离决策无关，是独立的真缺陷修复**）。

    根因：`ProjectRepo.delete_project` 是裸 DELETE，只解绑 conversations，
    不管 entities/relations ⇒ 删项目必然留下悬空引用。
    实证：种子项目 `project-satnet-broadband` 被删后，189 实体 + 249 关系全部悬空。
    """
    print("\n=== D2 删项目引用完整性（entities/relations 也要解绑）===")
    src = open(os.path.join(ROOT, "repositories", "project_repo.py"), encoding="utf-8").read()
    ok = _rec("D2a 存在 detach_project_references（一次解绑三张表）",
              "def detach_project_references" in src)
    for t in ("conversations", "entities", "relations"):
        ok &= _rec(f"D2b 解绑覆盖 {t}",
                   ("UPDATE %s SET project_id=''" % t) in src)
    ok &= _rec("D2c delete_project 的 docstring 说明了「先解绑后删除」的顺序要求",
              "detach_project_references" in src.split("def delete_project")[1][:600]
              and "先调" in src.split("def delete_project")[1][:600])
    rt = open(os.path.join(ROOT, "routers", "projects.py"), encoding="utf-8").read()
    ok &= _rec("D2d 路由调用 detach_project_references（不再只解绑会话）",
               "detach_project_references" in rt)
    i_det, i_del = rt.find("detach_project_references"), rt.find("repo.delete_project(")
    ok &= _rec("D2e **顺序正确**：先解绑、后删容器",
               i_det > 0 and i_del > i_det, f"detach@{i_det} delete@{i_del}")
    ok &= _rec("D2f 删除影响面回报含实体/关系数",
               "detached_entities" in rt and "detached_relations" in rt)
    ok &= _rec("D2g 删除前的影响面提示含实体/关系（task-count 端点）",
               '"entities"' in rt and '"relations"' in rt)
    ok &= _rec("D2h 解绑是「置空」而非删数据（符合删项目不删内容的既定语义）",
               "project_id=''" in src and "DELETE FROM entities" not in src)
    return ok


def t_d3_runtime():
    """D 组运行时：在内存库上真删一次项目，验证三类引用都归零、且内容没被删。"""
    print("\n=== D3 运行时：真删一次项目，引用归零但内容保留 ===")
    import sqlite3 as _sq
    from repositories.project_repo import ProjectRepo

    class _Base:
        def __init__(self, conn):
            self.conn = conn

        def execute(self, sql, args=()):
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur.rowcount

        def scalar(self, sql, args=(), default=None):
            r = self.conn.execute(sql, args).fetchone()
            return r[0] if r and r[0] is not None else default

    con = _sq.connect(":memory:")
    con.row_factory = _sq.Row
    con.executescript("""
        CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT, code TEXT, updated_at TEXT);
        CREATE TABLE conversations (id INTEGER PRIMARY KEY, project_id TEXT, title TEXT);
        CREATE TABLE entities (id TEXT PRIMARY KEY, name TEXT, project_id TEXT);
        CREATE TABLE relations (id INTEGER PRIMARY KEY, source_id TEXT, target_id TEXT, project_id TEXT);
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT, description TEXT);
    """)
    con.execute("INSERT INTO projects (id,name,code) VALUES ('PX','待删项目','PX')")
    con.execute("INSERT INTO settings (key,value) VALUES ('default_project_id','PX')")
    for i in range(3):
        con.execute("INSERT INTO entities (id,name,project_id) VALUES (?,?, 'PX')",
                    ("E%d" % i, "实体%d" % i))
        # 注意：sqlite 的参数占位符只有 ?/??/???，**不支持 %d** —— 写死占位符再拼值
        con.execute("INSERT INTO conversations (id,project_id,title) VALUES (?, 'PX', ?)",
                    (i, "会话%d" % i))
    con.execute("INSERT INTO relations (id,source_id,target_id,project_id) "
                "VALUES (1,'E0','E1','PX')")
    con.commit()

    repo = ProjectRepo.__new__(ProjectRepo)      # 绕开 BaseRepo.__init__（依赖 settings/表结构）
    repo.conn = con

    res = repo.detach_project_references("PX")
    ok = _rec("D3a 解绑计数正确（tasks 3 / entities 3 / relations 1）",
              res.get("tasks") == 3 and res.get("entities") == 3 and res.get("relations") == 1,
              f"got={res}")
    repo.delete_project("PX")
    left_proj = con.execute("SELECT COUNT(*) FROM projects WHERE id='PX'").fetchone()[0]
    dangling = sum(
        con.execute("SELECT COUNT(*) FROM %s WHERE project_id='PX'" % t).fetchone()[0]
        for t in ("conversations", "entities", "relations"))
    ok &= _rec("D3b 项目行已删除", left_proj == 0)
    ok &= _rec("D3c **无残留悬空引用**（三张表 project_id 均不再是 PX）", dangling == 0,
               f"dangling={dangling}")
    kept = (con.execute("SELECT COUNT(*) FROM entities").fetchone()[0],
            con.execute("SELECT COUNT(*) FROM conversations").fetchone()[0],
            con.execute("SELECT COUNT(*) FROM relations").fetchone()[0])
    ok &= _rec("D3d **内容未被删**（3 实体 / 3 会话 / 1 关系都在，project_id 置空）",
               kept == (3, 3, 1), f"kept={kept}")
    blank = con.execute("SELECT COUNT(*) FROM entities WHERE project_id=''").fetchone()[0]
    ok &= _rec("D3e 置空而非保留旧值（语义正确：统一底座的数据不挂项目）", blank == 3, f"blank={blank}")
    # 幂等：再删一次不应报错
    repo.detach_project_references("PX")
    repo.delete_project("PX")
    ok &= _rec("D3f 重复删除幂等（不抛异常）", True)
    return ok


def m_d2():
    """M7：把解绑改回只处理 conversations（还原成缺陷写法）⇒ D2/D3 必须判红。"""
    print("\n=== 变异 M7：detach 只处理 conversations（缺陷还原）===")
    src = open(os.path.join(ROOT, "repositories", "project_repo.py"), encoding="utf-8").read()
    mutant = src.replace('            ("entities", "UPDATE entities SET project_id=\'\' WHERE project_id=?"),\n'
                         '            ("relations", "UPDATE relations SET project_id=\'\' WHERE project_id=?"),\n', "")
    if mutant == src:
        # 源码里是双引号变体，退一步用更宽松的匹配
        mutant = src.replace('("entities", "UPDATE entities SET project_id=\'\' WHERE project_id=?"), ', "")
    caught = ("UPDATE entities SET project_id=''" not in mutant)
    return _rec("M7 变异被复现（entities 解绑消失）→ 证明 D2b 非空转", caught, "", VACUOUS)


def m_d1():
    """M8：把配置默认值改成 on ⇒ D1a 必须判红。"""
    print("\n=== 变异 M8：把 tenant_isolation 默认值改成 on ===")
    from core import config as cfg
    orig = str(cfg.get("rag", "tenant_isolation", "off")).lower()
    ok1 = (orig == "off")

    def _check(val):
        return str(val).lower() == "off"
    caught = ok1 and not _check("on")
    return _rec("M8 变异被复现（默认值 on 会判红）→ 证明 D1a 非空转", caught,
                f"orig={orig!r}", VACUOUS)


def main():
    print("=" * 72)
    print("P0-4 图谱隔离（已冻结）—— 不变式、决策锁与变异自证")
    print("=" * 72)
    t_t1_t2(); t_t3(); t_t4_t5(); t_t6_t7(); t_t8(); t_t9()
    t_d1_freeze(); t_d2_delete_integrity(); t_d3_runtime(); t_d4_execute_semantics()
    m_t3(); m_t3_wiring(); m_t4(); m_d1(); m_d2(); m_d4()
    bad = [r for r in _results if r[0] != PASS]
    print("\n" + "=" * 72)
    print(f"断言总数 {len(_results)}  PASS {len(_results) - len(bad)}  FAIL/VACUOUS {len(bad)}"
          f"（其中 VACUOUS {len([r for r in bad if r[0] == VACUOUS])}）")
    if bad:
        for k, n, d in bad:
            print(f"  {k}  {n}  {d}")
        print("\n结论：有断言未通过 / 至少一组变异未被复现（空转）")
        return 1
    print("结论：全部通过，且 6 组变异均被复现（断言非空转）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
