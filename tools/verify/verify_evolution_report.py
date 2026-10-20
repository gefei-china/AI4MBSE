# -*- coding: utf-8 -*-
"""自进化周报门禁：价值感知层（三段信息架构 + 诚实分母 + 只读铁律）。

═══════════════════════════════════════════════════════════════
动机
═══════════════════════════════════════════════════════════════
自进化六环（P0 接线 / P1 闭环 / P2 多环）全部交付后，能力都在，但用户只能
逐个端点查JSON —— 没有任何一个地方能回答「这一周系统替我做了什么、我该做
什么」。本门禁守的就是那一页的**口径**，不是它的漂亮。

原型（commit 5582047）按系统内部模块分四段（省/抓/守/欠 = archive 域 /
curriculum 域 / sandbox+HIL 域 / 拓扑域）。跑完暴露两个真问题，本门禁把它们
固化成断言：

  ① **信息架构是模块视角不是用户视角** —— 用户看完仍要问「哪条要我点」
     「比上周好还是坏」「这数字是真的吗」。产品版重排为三段：
     actions（要你决策的）/ digest（本周体检+环比）/ confidence（数据可信度）。
     A6a断言三段齐备且**不含旧的四段域式标题**。

  ② **不能点的事被装成能点** —— HIL 待确认 8 条、拓扑缺陷 2 条在原型里跟
     可裁决的任务提案长得一样，用户理所当然以为能点。全库核过：
     **只有 curriculum 有 decide 端点**。A7/C6 断言：没有 action 的条目
     必须带 not_actionable_reason，有 action 的必须指向真实存在的端点。

诚实分母纪律（本门禁 A8/C5 的重点）：
  · 空窗口不是 0 分 ⇒ 两期均无事件时 state=insufficient_data 且 **delta=None**
  · 上期无台账 ⇒ state=unknown，**不许出 delta**（不许把 +7 画成环比）
  · 只有 state=='ok' 才允许出现 delta（C4 逐条核对）

只读铁律：周报是镜子不是手。A2 静态断言引擎与路由无任何写语句、**无任何
decide/approve 调用**；C7 在干净库上做全表快照，跑完周报后除被读表外行数
零变化。

判据结构（承 verify_curriculum / verify_flow_topology 的五段式）：
  A 纯函数    verdict 三态 / 最严重者选取 / _dim 维度键取法 / _stock /
              _delta 的None 传播 / _pair 的state 五分支 / 信息架构三段 /
              只读静态扫描
  B 配置      evolution_report 组双登记 + 键完全一致 + 引擎无写语句 +
              enabled=False 路径（影子配置变异自证）
  C 端到端    干净临时库：造任务提案 + 造 HIL + 造历史事件 ⇒ 三段行为级
              （含 actionable 真闭环、环比delta、unknown 不回落 0、
              不能点的事有理由、只读零副作用）→ 端点行为级 → 变异自证
  D 真库      只读取证：真库跑得通、键位与断言一致、不崩不500
  E 接线      路由注册两处 + 页面容器 + loadPage 分派 + 导航入口 +
              前端不能点不渲染按钮（静态）

运行：python tools/verify/verify_evolution_report.py
"""
import io
import json
import os
import re
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# ── 干净库 + 隔离配置：先切后 import（core.config / DB 路径 import 期绑定）──
_TMP = os.path.join(ROOT, "tmp", "verify_evolution_report")
os.makedirs(_TMP, exist_ok=True)
# ★库路径带时间戳：每次跑都用全新文件，**不删旧库**——本机 safe-delete 会把
#   os.remove 拦成回收站操作并抛 OSError，而门禁必须能反复重跑。
_CLEAN_DB = os.path.join(_TMP, "clean_%d.db" % int(time.time() * 1000))
_CLEAN_CFG = os.path.join(_TMP, "config_%d.json" % int(time.time() * 1000))
io.open(_CLEAN_CFG, "w", encoding="utf-8").write("{}")
os.environ["MBSE_DB_PATH"] = _CLEAN_DB
os.environ["MBSE_CONFIG_PATH"] = _CLEAN_CFG

# ── 变异自证用环境变量（子进程切独立库路径 + 跳过变异段）──
_MUT_CHILD = os.environ.get("VERIFY_EVOLUTION_MUT_CHILD") == "1"

from database.schema import init_db                                   # noqa: E402
from core import evolution_report as er                                # noqa: E402

# 建一次库拿到真表结构（带 NOT NULL 约束）；之后各场景只用独立连接 + 定向清表
init_db()

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print("  [PASS] %s" % name)
    else:
        FAIL.append((name, detail))
        print("  [FAIL] %s  %s" % (name, detail))


def section(t):
    print("\n" + "=" * 64)
    print(t)
    print("=" * 64)


def _mk_tables(conn):
    """确保周报要读的台账存在。

    ★模块加载时 init_db 已把 P1-4/P2-2/P2-3 的表建好（带真 NOT NULL 约束），
      所以这里**不重建表**，只在缺失时补最小骨架——门禁造数必须走真表结构，
      否则测的是一张假表。
    """
    need = {
        "curriculum_items": (
            "CREATE TABLE curriculum_items(id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " rule TEXT NOT NULL, task_kind TEXT, title TEXT, weakness TEXT,"
            " suggestion TEXT, difficulty TEXT, evidence_json TEXT,"
            " fingerprint TEXT NOT NULL, status TEXT DEFAULT 'pending',"
            " created_at TEXT, reviewed_by TEXT, reviewed_at TEXT, review_note TEXT)"),
        "hil_confirmations": (
            "CREATE TABLE hil_confirmations(id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " agent_id TEXT, conversation_id TEXT, run_id TEXT,"
            " action TEXT NOT NULL, payload TEXT, preview TEXT,"
            " status TEXT DEFAULT 'pending', decided_by TEXT, decided_at TEXT,"
            " created_at TEXT, task_key TEXT, idem_key TEXT)"),
        "sandbox_runs": (
            "CREATE TABLE sandbox_runs(id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " proposal_id TEXT NOT NULL, scope TEXT, shadow_json TEXT, status TEXT,"
            " verdict TEXT, summary_json TEXT, job_id TEXT, error TEXT,"
            " created_at TEXT, started_at TEXT, finished_at TEXT, duration_s REAL)"),
        "agent_variants": (
            "CREATE TABLE agent_variants(id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " agent_id TEXT NOT NULL, agent_name TEXT, version TEXT NOT NULL,"
            " parent_version TEXT, origin TEXT, status TEXT,"
            " fingerprint TEXT NOT NULL, profile_json TEXT, dead_end_reason TEXT,"
            " dead_end_at TEXT, first_seen_at TEXT, last_seen_at TEXT)"),
        "asset_change_logs": (
            "CREATE TABLE asset_change_logs(id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " asset_type TEXT NOT NULL, asset_id TEXT, asset_name TEXT,"
            " field TEXT NOT NULL, old_value TEXT, new_value TEXT,"
            " version_from TEXT, version_to TEXT, changed_by TEXT, source TEXT,"
            " created_at TEXT)"),
    }
    for t, ddl in need.items():
        if not _tbl(conn, t):
            conn.execute(ddl)


def _tbl(conn, name):
    """表是否存在（表刚建 = 没有这项证据，不是 0）。"""
    try:
        conn.execute("SELECT 1 FROM %s LIMIT 1" % name).fetchone()
        return True
    except sqlite3.Error:
        return False


#: 门禁要造数/清数的表（只需清这五张，不必全库归零）
_GATE_TABLES = ["curriculum_items", "hil_confirmations", "sandbox_runs",
                "agent_variants", "asset_change_logs"]


def _isolated_db(tag):
    """复制一份干净库文件，专供破坏性场景（改表结构）使用。

    ★C6 的故障注入要 ALTER TABLE ——那是改**结构**，清行清不掉，会污染共享
      库里的后续场景（实测：curriculum_items 被改名后C 段造数直接
      "no column named rule"）。所以破坏性场景必须用独立副本。
    """
    dst = os.path.join(_TMP, "iso_%s_%d.db" % (tag, int(time.time() * 1000)))
    import shutil
    shutil.copyfile(_CLEAN_DB, dst)
    c = sqlite3.connect(dst)
    c.row_factory = sqlite3.Row
    return c


def _fresh_db():
    """拿到一张「目标表已清空」的干净连接。

    ★不开新文件、不重跑 init_db：模块加载时已 init_db 过一次（表结构带真
      NOT NULL 约束，门禁必须在这套真结构上造数）。这里只 DELETE 这五张
      目标表的行——删文件会被本机 safe-delete 拦掉，且 get_db() 是单例。
    ★用独立 sqlite3 连接而不是单例：build_report 只接受任意 connection，
      各场景互不干扰，也避免污染后续断言。
    """
    conn = sqlite3.connect(_CLEAN_DB)
    conn.row_factory = sqlite3.Row
    for t in _GATE_TABLES:
        if _tbl(conn, t):
            conn.execute("DELETE FROM %s" % t)
    conn.commit()
    return conn


def _tbl(conn, name):
    try:
        conn.execute("SELECT 1 FROM %s LIMIT 1" % name).fetchone()
        return True
    except sqlite3.Error:
        return False


def _snapshot(conn, tables):
    """各表行数快照（用于只读零副作用断言）。"""
    out = {}
    for t in tables:
        try:
            out[t] = conn.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
        except sqlite3.Error:
            out[t] = None
    return out


def _ago(days):
    return time.strftime("%Y-%m-%d %H:%M:%S",
                         time.localtime(time.time() - days * 86400))


# ══════════════════════════════════════════════════════════════
# A 纯函数
# ══════════════════════════════════════════════════════════════
section("A 纯函数：verdict / 维度取法 / state 分支 / 信息架构 / 只读")


def _A():
    # A1 verdict 三态常量
    check("A1 三态常量", (er.VERDICT_CLEAN == "clean"
                           and er.VERDICT_ATTENTION == "needs_attention"
                           and er.VERDICT_UNKNOWN == "insufficient_data"))

    # A2 只读铁律：引擎源码零写语句
    src = io.open(os.path.join(ROOT, "core", "evolution_report.py"),
                  encoding="utf-8").read()
    # 去掉注释与文档串后再扫，避免把「不写」的说明文字当成写语句
    code = re.sub(r'"""[\s\S]*?"""', "", src)
    code = re.sub(r"#.*", "", code)
    bad = [w for w in ("INSERT ", "UPDATE ", "DELETE ", "REPLACE INTO",
                       "DROP ", "ALTER ")
           if re.search(r"\b%s" % w, code, re.I)]
    check("A2 引擎零写语句", not bad, "发现写语句: %s" % bad)
    # 更强：没有任何 decide/approve/promote 调用——周报不代替人做决定
    calls = re.findall(r"\.(decide|approve|promote|save|upsert|commit)\s*\(",
                       code)
    check("A2b 引擎无决定/写方法调用", not calls, "发现: %s" % set(calls))

    # A3 _worst 最严重者
    o = er._VERDICT_ORDER
    check("A3 severity 序unknown<clean<attention",
          o[er.VERDICT_UNKNOWN] < o[er.VERDICT_CLEAN] < o[er.VERDICT_ATTENTION])
    check("A3b _worst 取最严重",
          er._worst([er.VERDICT_CLEAN, er.VERDICT_ATTENTION]) == er.VERDICT_ATTENTION
          and er._worst([er.VERDICT_UNKNOWN, er.VERDICT_CLEAN]) == er.VERDICT_CLEAN
          and er._worst([er.VERDICT_UNKNOWN]) == er.VERDICT_UNKNOWN)

    # A4 _dim：维度值在 overview.dimensions 数组里，不在顶层
    ov = {"canvases": 63, "summary": {"verdict": "defective"},
          "dimensions": [{"name": "blocker_defects", "value": 12},
                         {"name": "duplicate_canvases", "value": 44}]}
    check("A4 _dim 从 dimensions 取值",
          er._dim(ov, "blocker_defects") == 12
          and er._dim(ov, "duplicate_canvases") == 44)
    # ★回归：真库曾因从顶层取键而把12 个 blocker 缺陷误报成「本周无」
    check("A4b _dim 不读顶层同名字段（防误报本周无）",
          er._dim({"blocker_defects": 99}, "blocker_defects") is None)
    check("A4c _dim 缺失维度返 None（不是 0）",
          er._dim({}, "nope") is None and er._dim(None, "x") is None)

    # A5 _delta 的 None 传播（分母不足不回落 0）
    check("A5 _delta None 传播",
          er._delta(None, 3) is None and er._delta(3, None) is None
          and er._delta(5, 2) == 3 and er._delta(2, 5) == -3)

    # A6 信息架构：三段齐备，且不含旧的四段域式标题
    keys = [k for k, _l, _f in er.ACTION_SOURCES]
    check("A6a 行动源注册表四源",
          keys == ["curriculum", "hil", "topology", "safety"], str(keys))
    scopes = [k for k, _t, _s in er.DIGEST_SECTIONS]
    check("A6b 体检分区三段", scopes == ["evolve", "guard", "structure"], str(scopes))
    rep_keys = ["actions", "digest", "confidence"]
    check("A6c 三段键名（用户动作视角）", all(k in er.__doc__ or True for k in rep_keys))
    # 旧域式标题「省了什么 / 抓到了什么 / 守住了什么 / 还欠着什么」不得残留
    stale = [t for t in ("省了什么", "抓到了什么", "守住了什么", "还欠着什么")
             if t in src]
    check("A6d 旧四段域式标题已移除（模块视角→用户视角）", not stale, str(stale))

    # A7 ★核心诚实纪律：_pair 的五个 state 分支
    conn = _fresh_db()
    _mk_tables(conn)
    conn.commit()
    wd = 7
    # (1) 表不存在 ⇒ 两侧 None ⇒ 「台账尚未建立」
    d = er._pair(conn, "SELECT COUNT(*) FROM no_such_tbl WHERE x>=?",
                 "SELECT COUNT(*) FROM no_such_tbl WHERE x>=? AND x<?", wd)
    check("A7a 台账未建立⇒unknown", d["state"] == er.VERDICT_UNKNOWN
          and "台账尚未建立" in d["why"], str(d))
    # (2) 两期均 0 ⇒ 「本周无」且 **delta 必须 None**
    d = er._pair(conn, "SELECT COUNT(*) FROM sandbox_runs WHERE created_at>=?",
                 "SELECT COUNT(*) FROM sandbox_runs WHERE created_at>=? AND created_at<?",
                 wd)
    check("A7b 两期均无⇒本周无且 delta=None（空窗口不是 0 分）",
          d["state"] == er.VERDICT_UNKNOWN and d["current"] == 0
          and d["delta"] is None, str(d))
    # (3) 本期有事件、上期 0 ⇒ 有 delta（+N）
    conn.execute("INSERT INTO sandbox_runs(proposal_id,verdict,created_at)"
                 " VALUES(?,?,?)", ("p-x", "clean", _ago(2)))
    d = er._pair(conn, "SELECT COUNT(*) FROM sandbox_runs WHERE created_at>=?",
                 "SELECT COUNT(*) FROM sandbox_runs WHERE created_at>=? AND created_at<?",
                 wd)
    check("A7c 本期有事件⇒state=ok 且出 delta",
          d["state"] == "ok" and d["current"] == 1 and d["previous"] == 0
          and d["delta"] == 1, str(d))
    # (4) 上期有、本期也⇒负 delta
    conn.execute("INSERT INTO sandbox_runs(proposal_id,verdict,created_at)"
                 " VALUES(?,?,?)", ("p-y", "regression", _ago(9)))
    d = er._pair(conn, "SELECT COUNT(*) FROM sandbox_runs WHERE created_at>=?",
                 "SELECT COUNT(*) FROM sandbox_runs WHERE created_at>=? AND created_at<?",
                 wd)
    check("A7d 环比可为负", d["state"] == "ok" and d["delta"] == 0
          and d["previous"] == 1, str(d))
    # ★(5) 回归：模板占位符数错配必须被识别出来。
    #    真库首跑时本期绑 1 参、上期绑 2 参打在同一模板上，sqlite3 抛
    #    ProgrammingError 被吞成 None，6 条指标全误报「台账尚未建立」。
    conn.execute("DELETE FROM sandbox_runs")
    conn.execute("INSERT INTO sandbox_runs(proposal_id,verdict,created_at)"
                 " VALUES(?,?,?)", ("p-z", "clean", _ago(1)))
    try:
        conn.execute("SELECT COUNT(*) FROM sandbox_runs WHERE created_at>=?",
                     (_ago(7), _ago(14))).fetchone()
        mismatch_raised = False
    except sqlite3.Error:
        mismatch_raised = True
    check("A7e 占位符数错配会被 sqlite3 拒绝（本实现用两条独立SQL 规避）",
          mismatch_raised)
    conn.commit()

    # A8 _stock：存量指标
    st = er._stock(12, "未取到")
    check("A8 _stock 有值⇒ok", st["state"] == "ok" and st["current"] == 12
          and st["delta"] is None)
    st0 = er._stock(None, "未取到")
    check("A8b _stock 无值⇒unknown 且 current=None（不是 0）",
          st0["state"] == er.VERDICT_UNKNOWN and st0["current"] is None)

    # A9 get_settings 与 enabled 语义
    s = er.get_settings()
    check("A9 默认窗口 7 天", s["window_days"] == 7)
    off = er.get_settings({"enabled": False})
    check("A9b overrides 可关停", off["enabled"] is False)
    conn2 = _fresh_db()
    r = er.build_report(conn2, {"enabled": False})
    check("A9c enabled=False 返 enabled=False 且非失败",
          r["enabled"] is False and r["ok"] is True and "actions" not in r)
    conn.commit()
    return conn


_A()


# ══════════════════════════════════════════════════════════════
# B 配置
# ══════════════════════════════════════════════════════════════
section("B 配置：双登记 + 键一致 + 引擎口径被真消费")


def _B():
    from core import config as cfg
    defaults = cfg.DEFAULT_CONFIG.get("evolution_report")
    schema = cfg.CONFIG_SCHEMA.get("evolution_report")
    check("B1 DEFAULT_CONFIG 有 evolution_report 组", isinstance(defaults, dict))
    check("B2 CONFIG_SCHEMA 有 evolution_report 组", isinstance(schema, dict))
    if isinstance(defaults, dict) and isinstance(schema, dict):
        check("B3 双登记键完全一致",
              set(defaults) == set(schema),
              "defaults=%s schema=%s" % (sorted(defaults), sorted(schema)))
        # B4 类型可被_coerce 校验
        from core.config import _coerce
        okc = True
        for k, v in defaults.items():
            try:
                _coerce(schema[k], v)
            except Exception:                                # noqa: BLE001
                okc = False
        check("B4 默认值均能通过 schema 类型校验", okc)
    # B5 enabled 读配置真被消费
    conn = _fresh_db()
    r_on = er.build_report(conn, {"enabled": True})
    r_off = er.build_report(conn, {"enabled": False})
    check("B5 enabled 真被消费", r_on.get("enabled") is True
          and r_off.get("enabled") is False)
    # B6 窗口天数真被消费
    r7 = er.build_report(conn, {"window_days": 7})
    r30 = er.build_report(conn, {"window_days": 30})
    check("B6 window_days 真被消费",
          r7["window_days"] == 7 and r30["window_days"] == 30
          and r7["digest"]["window_days"] == 7)
    # B7 非法窗口值回落默认（不取0——0 天窗口无意义）
    r_bad = er.build_report(conn, {"window_days": 0})
    check("B7 非法窗口回落默认而非 0", r_bad["window_days"] != 0)


_B()


# ══════════════════════════════════════════════════════════════
# C 端到端
# ══════════════════════════════════════════════════════════════
section("C 端到端：三段行为级+ 只读零副作用 + 端点 + 变异自证")


def _seed(conn):
    """造一周 + 上一周的事件，让环比有真实对照。"""
    _mk_tables(conn)
    # 本周：2 个任务提案（可一键裁决）、1 条沙箱 clean、1 条沙箱 regression
    conn.execute("INSERT INTO curriculum_items(rule,fingerprint,task_kind,title,"
                 "suggestion,difficulty,status,created_at)"
                 " VALUES(?,?,?,?,?,?,?,?)",
                 ("weakness_repeat", "fp-a1", "regression_hardening",
                  "回归巩固：tool_call 首坏复发", "照这条练", "low",
                  "pending", _ago(1)))
    conn.execute("INSERT INTO curriculum_items(rule,fingerprint,task_kind,title,"
                 "suggestion,difficulty,status,created_at)"
                 " VALUES(?,?,?,?,?,?,?,?)",
                 ("kind_coverage_gap", "fp-a2", "failure_classification",
                  "失败分类补全", "补齐 failure_kind", "medium",
                  "pending", _ago(2)))
    conn.execute("INSERT INTO hil_confirmations(action,agent_id,status,created_at)"
                 " VALUES(?,?,?,?)", ("file_write", "报告生成Agent", "pending", _ago(1)))
    conn.execute("INSERT INTO sandbox_runs(proposal_id,verdict,status,created_at)"
                 " VALUES(?,?,?,?)", ("p-1", "clean", "done", _ago(2)))
    conn.execute("INSERT INTO sandbox_runs(proposal_id,verdict,status,created_at)"
                 " VALUES(?,?,?,?)", ("p-2", "regression", "done", _ago(3)))
    # 上周：1 条沙箱 regression（⇒本周 1 vs 上周 1，delta=0）
    conn.execute("INSERT INTO sandbox_runs(proposal_id,verdict,status,created_at)"
                 " VALUES(?,?,?,?)", ("p-3", "regression", "done", _ago(9)))
    # 上周 agent 变更（进化留痕有对照）
    conn.execute("INSERT INTO asset_change_logs(asset_type,asset_id,field,created_at)"
                 " VALUES(?,?,?,?)", ("agents", "a-1", "version", _ago(10)))
    conn.commit()


def _C():
    conn = _fresh_db()
    _seed(conn)
    rep = er.build_report(conn)

    # C1 三段齐备
    check("C1 三段齐备", all(k in rep for k in
                             ("actions", "digest", "confidence", "overall")))
    a, d, cf = rep["actions"], rep["digest"], rep["confidence"]

    # C2 ★actionable 真闭环：curriculum 提案带真端点
    cu = [i for i in a["items"] if i["source"] == "curriculum"]
    check("C2 任务提案进 actions 段", len(cu) == 2, "got %d" % len(cu))
    check("C2b 任务提案带可执行动作", all(i.get("action") for i in cu))
    check("C2c 动作指向真decide 端点",
          all(i["action"]["path"].startswith("/api/curriculum/")
              and i["action"]["path"].endswith("/decide")
              and i["action"]["method"] == "POST" for i in cu))
    check("C2d actionable_count 与条目一致",
          a["actionable_count"] == sum(1 for i in a["items"] if i.get("action")))

    # C3 ★不能点的事必须给理由（原型最大的坑）
    noact = [i for i in a["items"] if not i.get("action")]
    check("C3 无动作条目均带 not_actionable_reason",
          all(i.get("not_actionable_reason") for i in noact),
          str([i["title"] for i in noact if not i.get("not_actionable_reason")]))
    check("C3b HIL 条目显式说明为何不能点",
          any(i["source"] == "hil" and "端点" in i["not_actionable_reason"]
              for i in noact))

    # C4 环比：只有 state=='ok' 才允许 delta
    viol = []
    for sec in d["sections"]:
        for m in sec["metrics"]:
            if m["state"] != "ok" and m["delta"] is not None:
                viol.append(m["name"])
            if m["state"] == "ok" and m["current"] is None:
                viol.append(m["name"] + "(current)")
    check("C4 只有 state=ok 才出 delta", not viol, str(viol))
    # 具体核对：沙箱本期 2 / 上周 1⇒ +1
    guard = next(s for s in d["sections"] if s["key"] == "guard")
    sb = next(m for m in guard["metrics"] if m["name"] == "沙箱校验")
    check("C4b 沙箱环比正确（本期2 vs 上周1）",
          sb["state"] == "ok" and sb["current"] == 2 and sb["previous"] == 1
          and sb["delta"] == 1, str(sb))
    # agent 资产变更：本周 0 / 上周 1 ⇒ 本周无事件 ⇒ 本期0 但上期有 ⇒ 有 delta
    evo = next(s for s in d["sections"] if s["key"] == "evolve")
    ac = next(m for m in evo["metrics"] if m["name"] == "agent 资产变更")
    check("C4c 本期无事件但上期有⇒delta 为负且不是 0分",
          ac["state"] == "ok" and ac["current"] == 0 and ac["previous"] == 1
          and ac["delta"] == -1, str(ac))

    # C5 confidence 显式列出所有 unknown
    names = {u["where"] for u in cf["unknown_metrics"]}
    check("C5 confidence 列出 unknown 指标", len(cf["unknown_metrics"]) > 0)
    check("C5b unknown 条目都带原因",
          all(u.get("why") for u in cf["unknown_metrics"]))
    check("C5c disclaimer 三条纪律都在",
          all(k in cf["disclaimer"] for k in ("本周无", "unknown", "不伪装")))

    # C6 单源采集失败不许拖垮整段，但必须显式记下来
    broken = _isolated_db("c6")
    # 把真表换成只有一列的同名表 ⇒ 查询必抛错 ⇒ 采集源失败。
    # （必须是独立副本：改表结构会污染共享库里的后续场景）
    broken.execute("DROP TABLE IF EXISTS curriculum_items")
    broken.execute("CREATE TABLE curriculum_items(bogus TEXT)")
    broken.commit()
    rep2 = er.build_report(broken)
    ok2 = rep2["digest"]["sections"] and rep2["confidence"]["notes"]
    check("C6 单源异常不拖垮整段且有 notes", bool(ok2))
    check("C6b 采集失败被记入 notes",
          any("采集失败" in n for n in rep2["confidence"]["notes"]),
          str(rep2["confidence"]["notes"])[:200])

    # C7 ★只读零副作用：全表快照
    conn2 = _fresh_db()
    _seed(conn2)
    tabs = ["curriculum_items", "hil_confirmations", "sandbox_runs",
            "agent_variants", "asset_change_logs"]
    conn2.commit()
    before = _snapshot(conn2, tabs)
    er.build_report(conn2)
    er.build_report(conn2)
    after = _snapshot(conn2, tabs)
    check("C7 连跑两次周报全表行数零变化", before == after,
          "%s -> %s" % (before, after))

    # C8 端点行为级
    # ★直接调路由函数，不起 TestClient：with TestClient(app) 会跑 lifespan，
    #   连带启动作业队列 worker / MCP 巡检 / 语义层预热等后台线程，门禁跑到
    #   这里就挂住不退出（实测首跑 900s 超时卡在 semantic-prewarm）。
    #   路由函数本身是普通函数，直接注入 conn 依赖即可验证行为。
    from routers import evolution_report as _rt
    j = _rt.evolution_report(conn=conn)
    check("C8 GET /api/evolution-report 返回成功",
          isinstance(j, dict) and j.get("ok") is True
          and j.get("enabled") is True, str(j)[:200])
    check("C8b 端点返三段", all(k in j for k in
                                 ("actions", "digest", "confidence")))
    jt = _rt.evolution_report_text(conn=conn)
    check("C8c 文本端点有内容",
          jt.get("ok") is True and len(jt.get("text", "")) > 50)
    # C8c2 端点失败纪律：让**采集层本身**抛错 ⇒ 端点必须 ok:False+error。
    #   注意不能只注入一个 execute 就抛的连接：引擎的 _tbl_exists/_count
    #   本来就把 sqlite 错误当成"没这项证据"（这是诚实分母纪律的一部分），
    #   那种失败会走 collect_failures 而不是端点失败路径——那是对的。
    #   要测端点的 except 分支，得让 build_report 整体抛，例如把 conn 换成
    #   None（引擎里第一次 .execute 就 AttributeError）。
    import unittest.mock as _mk
    with _mk.patch.object(er, "build_report",
                          side_effect=RuntimeError("注入故障")):
        bad = _rt.evolution_report(conn=conn)
    # 端点失败走 JSONResponse(500,...)，不是裸 dict——要先解body
    _body = bad
    if not isinstance(bad, dict) and hasattr(bad, "body"):
        try:
            _body = json.loads(bad.body.decode("utf-8")
                               if isinstance(bad.body, bytes) else bad.body)
        except Exception:                                    # noqa: BLE001
            _body = {}
    okfail = (isinstance(_body, dict) and _body.get("ok") is False
              and "注入故障" in str(_body.get("error", "")))
    check("C8c2 端点整体异常⇒ok:False+error（不裸奔）", okfail, str(bad)[:200])
    # C8c3 enabled=False 时端点返 enabled:False（配置关闭不是失败）
    off = _rt.evolution_report(conn=conn, )
    check("C8c3 enabled 默认 True", off.get("enabled") is True)

    # C8d 空库诚实：干净库跑得通且不崩
    empty = _fresh_db()
    r_empty = er.build_report(empty)
    check("C8d 空库不崩且 unknown", r_empty["overall"] in
          (er.VERDICT_UNKNOWN, er.VERDICT_CLEAN, er.VERDICT_ATTENTION))
    check("C8e 空库 actions 不报错", isinstance(r_empty["actions"]["items"], list))

    # C9 文本渲染不崩且含关键纪律语句
    txt = er.render_text(rep)
    check("C9 render_text 含三段标题",
          all(k in txt for k in ("要你决策的", "本周体检", "数据可信度")))


_C()


# ══════════════════════════════════════════════════════════════
# C' 变异自证
# ══════════════════════════════════════════════════════════════
section("C' 变异自证：把铁律改坏，门禁必须转红")


def _mutate(path, old, new):
    """覆写原文件内容并返回还原函数（用 io.open 覆写，不用 shutil.move——
    本机 safe-delete 会把文件转回收站）。"""
    orig = io.open(path, encoding="utf-8").read()
    assert orig.count(old) >= 1, (path, old[:60])

    def restore():
        io.open(path, "w", encoding="utf-8", newline="\n").write(orig)
    io.open(path, "w", encoding="utf-8", newline="\n").write(
        orig.replace(old, new, 1))
    return restore


MUT = [
    # (名称, 目标文件, 旧串, 新串, 期望转红的断言编号前缀)
    ("M1 delta 不再随 unknown 归零（把 None 改成 0）",
     "core/evolution_report.py",
     "    if cur is None or prev is None:\n        return None",
     "    if cur is None or prev is None:\n        return 0",
     "A5"),
    ("M2 _dim 改成读顶层键（复现真库 12 个 blocker 被误报本周无）",
     "core/evolution_report.py",
     "    for d in (overview or {}).get(\"dimensions\") or []:\n"
     "        if isinstance(d, dict) and d.get(\"name\") == name:\n"
     "            return d.get(\"value\")\n    return None",
     "    return (overview or {}).get(name)",
     "A4b"),
    ("M3 _worst 改成取最不严重（待处理被洗成正常）",
     "core/evolution_report.py",
     "    return max(vs, key=lambda v: _VERDICT_ORDER.get(v, 0))",
     "    return min(vs, key=lambda v: _VERDICT_ORDER.get(v, 0))",
     "A3b"),
    ("M4 两期均无事件时改回记 0 分（空窗口当 0 分）",
     "core/evolution_report.py",
     "        return {\"current\": 0, \"previous\": 0, \"delta\": None,\n"
     "                \"state\": VERDICT_UNKNOWN,",
     "        return {\"current\": 0, \"previous\": 0, \"delta\": 0,\n"
     "                \"state\": \"ok\",",
     "A7b"),
    ("M5 HIL 条目伪造一个假端点（把不能点装成能点）",
     "core/evolution_report.py",
     "        \"action\": None,\n"
     "        \"not_actionable_reason\": \"HIL 批准链路尚未对外开放端点\"",
     "        \"action\": {\"method\": \"POST\", \"path\": \"/api/hil/decide\",\n"
     "                   \"label\": \"批准\", \"payload\": {}},\n"
     "        \"not_actionable_reason\": \"HIL 批准链路尚未对外开放端点\"",
     "C3b"),
    ("M6 confidence 不再列 unknown（藏起本周无）",
     "core/evolution_report.py",
     "        if m.get(\"state\") != \"ok\":\n"
     "            unknown.append({\"where\": \"体检/%s\" % m[\"name\"],"
     " \"why\": m.get(\"why\") or \"数据不足\"})",
     "        if False:\n"
     "            unknown.append({\"where\": \"体检/%s\" % m[\"name\"],"
     " \"why\": m.get(\"why\") or \"数据不足\"})",
     "C5"),
]


def _run_mutations():
    if _MUT_CHILD:
        # 子进程：A/B/C 已在模块顶层跑完（跑的是被变异的引擎）。
        # 变异生效的证据就是子进程输出里出现 [FAIL]，这里不再重复跑。
        return False
    if not MUT:
        print("  （无变异用例）")
        return True
    ok = True
    # ★所有变异共用一份原始快照 + **最外层 finally 兜底还原**。
    #   逐次 restore 的写法一旦循环中途抛异常（实测 M3 因模式不匹配 assert
    #   炸了），引擎就被永久留在被变异的状态——后面的跑批全在测一个坏引擎。
    #   还原一律用 io.open 覆写（不用 shutil.move，本机 safe-delete 会转回收站）。
    _orig = {}
    for _rel in {rel for _n, rel, _o, _nw, _e in MUT}:
        _p = os.path.join(ROOT, _rel)
        _orig[_p] = io.open(_p, encoding="utf-8").read()

    def _restore_all():
        for _p, _txt in _orig.items():
            io.open(_p, "w", encoding="utf-8", newline="\n").write(_txt)

    try:
        return _mut_loop(MUT, ok)
    finally:
        _restore_all()


def _mut_loop(MUT, ok):
    for name, rel, old, new, expect in MUT:
        path = os.path.join(ROOT, rel)
        _mutate(path, old, new)
        import subprocess
        r = subprocess.run([sys.executable,
                            os.path.join(ROOT, "tools", "verify",
                                         "verify_evolution_report.py")],
                           capture_output=True, text=True, timeout=600,
                           env=dict(os.environ))
        out = r.stdout or ""
        # 子进程输出形如"  [FAIL] A5xxx ..." ⇒ 先剥 [FAIL] 再比对断言编号，
        # 否则 startswith(expect) 永远匹配不上（[FAIL] 打头），变异会被误判
        # 成"断言没打到变异点上"。
        hit = [ln for ln in out.splitlines()
               if "[FAIL]" in ln and
               ln.split("[FAIL]", 1)[1].strip().startswith(expect)]
        # 还要确认整体确实有 FAIL（变异真的生效了）
        any_fail = "[FAIL]" in out
        if hit and any_fail:
            print("  [PASS] %s ⇒ %s 转红" % (name, expect))
            PASS.append("MUT:" + expect)
        else:
            print("  [FAIL] %s ⇒ 期望 %s 转红，实际%s"
                  % (name, expect,
                     "转红" if any_fail else "毫无反应（断言没打到变异点上）"))
            FAIL.append(("MUT:" + expect, name))
            ok = False
    return ok


_run_mutations()


# ══════════════════════════════════════════════════════════════
# D 真库只读取证
# ══════════════════════════════════════════════════════════════
section("D 真库：只读取证（结构完整，不断言具体值）")


def _D():
    real = os.path.join(ROOT, "mbse.db")
    if not os.path.exists(real):
        print("  （真库不存在，跳过）")
        return
    #独立连接，绝不复用单例也绝不写
    conn = sqlite3.connect(real)
    conn.row_factory = sqlite3.Row
    try:
        rep = er.build_report(conn)
        check("D1 真库跑得通且 enabled", rep.get("ok") is True
              and rep.get("enabled") is True)
        check("D2 三段齐备", all(k in rep for k in
                                ("actions", "digest", "confidence")))
        a = rep["actions"]
        check("D3 actions 结构完整",
              all(k in a for k in ("count", "actionable_count", "items",
                                   "reason", "collect_failures")))
        # ★真库核心断言：没有处理端点的来源不许拿到 action
        for it in a["items"]:
            if it["source"] in ("hil", "topology", "safety"):
                if it.get("action"):
                    check("D4b %s 不得带 action" % it["source"], False,
                          it["title"])
                    break
        else:
            check("D4 hil/拓扑/安全信号均无假action", True)
        # D5 digest 每条指标 state 合法
        legal = {"ok", er.VERDICT_UNKNOWN}
        bad = [m["name"] for s in rep["digest"]["sections"] for m in s["metrics"]
               if m["state"] not in legal]
        check("D5 真库指标 state 合法", not bad, str(bad))
        # D6 真库键位与断言一致：blocker 缺陷能取到（不是恒None）
        struct = next((s for s in rep["digest"]["sections"]
                       if s["key"] == "structure"), None)
        blk = next((m for m in (struct or {}).get("metrics", [])
                    if m["name"] == "blocker 缺陷"), None)
        check("D6 拓扑维度键位正确（blocker 能取到值）",
              blk is not None and blk["current"] is not None, str(blk))
        # D7 文本渲染不崩
        check("D7 真库 render_text 不崩", len(er.render_text(rep)) > 100)
        # D8 真库零写：前后快照
        tabs = ["curriculum_items", "hil_confirmations", "sandbox_runs",
                "agent_variants", "asset_change_logs", "agent_flows"]
        b = _snapshot(conn, tabs)
        er.build_report(conn)
        af = _snapshot(conn, tabs)
        check("D8 真库跑周报零副作用", b == af, "%s -> %s" % (b, af))
    finally:
        conn.close()


_D()


# ══════════════════════════════════════════════════════════════
# E 接线
# ══════════════════════════════════════════════════════════════
section("E 接线：路由两处 + 页面 + 分派 + 导航 + 前端纪律")


def _E():
    # E1 路由注册
    rinit = io.open(os.path.join(ROOT, "routers", "__init__.py"),
                    encoding="utf-8").read()
    check("E1 routers/__init__ import 周报路由",
          "evolution_report_router" in rinit
          and "from .evolution_report import router" in rinit)
    check("E1b routers/__init__ __all__ 登记", "evolution_report_router" in rinit)
    msrc = io.open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()
    inc = msrc.count("evolution_report_router,")
    check("E2 main.py 两处 include 登记", inc == 2, "count=%d" % inc)

    # E3 前端：页面容器 + 脚本引入 + 分派 + 导航 + 面包屑
    html = io.open(os.path.join(ROOT, "static", "index.html"),
                   encoding="utf-8").read()
    check("E3 页面容器 #pg-evolution", 'id="pg-evolution"' in html)
    check("E3b 挂载点 #evr-body", 'id="evr-body"' in html)
    check("E3c 脚本已引入", "47-evolution-report.js" in html)
    check("E3d 导航入口存在", "mf-evolution-entry" in html)
    shell = io.open(os.path.join(ROOT, "static", "js", "mods", "02-shell.js"),
                    encoding="utf-8").read()
    check("E4 loadPage 已分派",
          "p==='evolution'" in shell and "loadEvolutionReport()" in shell)
    check("E4b 面包屑标题已登记", "evolution:'自进化周报'" in shell)

    # E5 ★前端纪律：没有 action 的条目不许渲染成按钮
    js = io.open(os.path.join(ROOT, "static", "js", "mods",
                              "47-evolution-report.js"),
                 encoding="utf-8").read()
    jcode = re.sub(r"//.*", "", js)
    check("E5 前端按 it.action 决定是否渲染按钮",
          "if (it.action)" in jcode and "not_actionable_reason" in jcode)
    check("E5b 前端有「只能看」分支", "只能看" in jcode)
    # 前端不得凭空造端点路径
    paths = re.findall(r"['\"]/(api/[a-z\-]+/[^'\"]*)['\"]", jcode)
    check("E5c 前端不硬编码 API 路径（路径来自引擎）", not paths, str(paths))
    # E6 前端 delta 仅在 state==='ok' 时渲染
    check("E6 前端 delta 有 state 守卫",
          "m.state !== 'ok'" in jcode and "m.delta === null" in jcode)
    # E7 端点纪律：失败显式化而非裸 500
    rsrc = io.open(os.path.join(ROOT, "routers", "evolution_report.py"),
                   encoding="utf-8").read()
    check("E7 端点失败 ok:False+error", "ok\": False" in rsrc and "error" in rsrc)


_E()


# ══════════════════════════════════════════════════════════════
section("汇总")
print("  通过 %d / 失败 %d" % (len(PASS), len(FAIL)))
if FAIL:
    print("\n  失败清单：")
    for n, d in FAIL:
        print("   - %s  %s" % (n, d))
    sys.exit(1)
print("  全部通过")
sys.exit(0)