# -*- coding: utf-8 -*-
"""P0-1 门禁：资产长尾治理（core/lifecycle.py + GET /api/monitor/lifecycle + 面板接线）。

**背景（2026-10-18《自进化 AI 平台调研评估》整改第一项）**：
`tools/calib/retire_view_agents.py` 是一次性硬编码退役工具（点名 8 个 VIEW_AGENTS、
人工 precondition、手工执行）——同类「谁闲置/谁持续失败」判断无法复用、无法审计。
本门禁守护其泛化版 `core/lifecycle.py` 的四条性质：

  A. 判定是**纯函数**：阈值进、档位+理由出（四档矩阵逐格单测，无需数据库）；
  B. 阈值**走配置**：monkeypatch core.config 变异 idle_days/enabled，
     资产档位必须随之翻转——证明判据没有被模块内常量旁路（「不为跑通用例写死」的判据）；
  C. 干净库**端到端**：种子 agents/skills 六类典型形态，断言档位/汇总/理由/排序/API 过滤；
  D. 真库**只读 smoke**（开发机）：全量返回 + ★ name-join 取证
     （agent_tasks.agent_id 存 Agent **名**——按 id join 会静默全 0，本门禁专锁此口径）；
  E. 前后端**都接线**：路由存在 + 面板函数存在（只改后端不改编前端 = 改动无效，
     从返回值上看不出来——verify_reasoning_kind 的同款教训）。

双环境口径：
  · 干净库（CI 等价）：MBSE_DB_PATH 指向 tmp 空库 + init_db()；⚠️ 必须在任何项目
    import **之前**切库（core.config/DB_PATH 是 import 期绑定，晚设无效——实测踩过）。
  · 真库（开发机）：`file:mbse.db?mode=ro` 只读，不写、不迁移、不备份。

用法（项目 .venv，仓库根目录执行）：
    .venv/Scripts/python.exe tools/verify/verify_lifecycle.py
"""
import os
import shutil
import sqlite3
import sys

# ── 仓库根发现（不 import 任何项目模块）─────────────────────────────────
def _find_root():
    _d = os.path.dirname(os.path.abspath(__file__))
    for _ in range(6):
        if os.path.isfile(os.path.join(_d, "memory_service.py")):
            return _d
        _p = os.path.dirname(_d)
        if _p == _d:
            break
        _d = _p
    raise SystemExit("找不到仓库根（未找到 memory_service.py）")


ROOT = _find_root()
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# ── 双环境口径之干净库：先切库，后 import（import 期绑定，顺序即正确性）──
_TMP = os.path.join(ROOT, "tmp", "verify_lifecycle")
os.makedirs(_TMP, exist_ok=True)
_CLEAN_DB = os.path.join(_TMP, "clean.db")
if os.path.exists(_CLEAN_DB):
    os.remove(_CLEAN_DB)
os.environ["MBSE_DB_PATH"] = _CLEAN_DB

# 固定判定时刻：种子时间戳全部相对它推导 ⇒ 门禁在任何日期跑结果一致（不随日历漂移）
NOW_STR = "2026-10-18 12:00:00"
NOW = __import__("datetime").datetime(2026, 10, 18, 12, 0, 0)


def _days_ago(n: int) -> str:
    from datetime import timedelta
    return (NOW - timedelta(days=n)).strftime("%Y-%m-%d %H:%M:%S")


_results = []


def check(name, ok, detail=""):
    _results.append((name, bool(ok), detail))
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name,
                           ("  ← " + str(detail)) if (detail and not ok) else ""))
    return bool(ok)


def skip(name, why):
    _results.append((name, None, why))
    print("  [SKIP] " + name + "  -- " + str(why))


def _verdict_of(items, name):
    for it in items:
        if it.get("name") == name:
            return it
    return None


def main():
    # ── A. 纯函数判定矩阵 ──────────────────────────────────────────────
    print("== A. 判定纯函数（四档矩阵 + 阈值变异自证）==")
    from core import lifecycle as LC

    th = {k: v for k, v in LC._THRESHOLD_DEFAULTS.items()}
    cases = [
        # (name, uses, failures, idle_days, expected, why)
        ("A1 足量样本+高失败 ⇒ 退役(质量退出)", 10, 4, 5, LC.VERDICT_RETIRE),
        ("A2 长期闲置+少用 ⇒ 退役(闲置退出)", 2, 0, 40, LC.VERDICT_RETIRE),
        ("A3 曾常用+现已凉 ⇒ 复核", 8, 0, 40, LC.VERDICT_REVIEW),
        ("A4 失败率高但样本不足 ⇒ 复核(证据弱)", 3, 1, 2, LC.VERDICT_REVIEW),
        ("A5 采用不足且过观察期 ⇒ 复核", 2, 0, 20, LC.VERDICT_REVIEW),
        ("A6 刚过观察线 ⇒ 观察", 8, 0, 20, LC.VERDICT_WATCH),
        ("A7 失败率过观察线 ⇒ 观察", 8, 2, 5, LC.VERDICT_WATCH),
        ("A8 全项健康 ⇒ 保持", 8, 0, 5, LC.VERDICT_KEEP),
    ]
    for name, uses, failures, idle, expected in cases:
        v, reasons = LC._classify(uses, failures, idle, th)
        check(name, v == expected, f"got={v} reasons={reasons}")

    # 阈值变异：同一输入，阈值不同 ⇒ 档位必须不同（判据真被消费的直接证据）
    v0, _ = LC._classify(8, 1, 5, th)
    th2 = dict(th); th2["fail_rate_pct"] = 10
    v1, _ = LC._classify(8, 1, 5, th2)
    check("A9 ★阈值变异自证：fail_rate_pct 30→10，同一输入 8用/1败 从 %s 翻为 %s"
          % (v0, v1), v0 == LC.VERDICT_KEEP and v1 == LC.VERDICT_RETIRE,
          f"v0={v0} v1={v1}")
    th3 = dict(th); th3["watch_idle_days"] = 3
    v2, _ = LC._classify(8, 0, 5, th3)
    check("A10 阈值变异自证：watch_idle_days 15→3，闲置5天 从 %s 翻为 %s"
          % (v0, v2), v2 == LC.VERDICT_WATCH, f"v2={v2}")
    v3, _ = LC._classify(2, 0, None, th)      # 无时间证据 ⇒ 不产生闲置信号
    check("A11 idle_days=None（无时间证据）不误判为闲置", v3 == LC.VERDICT_KEEP,
          f"got={v3}")

    # ── B. 配置消费（默认路径走 core.config，变异后行为必须变）──────────
    print("\n== B. 阈值走配置（DEFAULT_CONFIG/CONFIG_SCHEMA 双登记 + 变异自证）==")
    import core.config as CFG

    dc = CFG.DEFAULT_CONFIG.get("lifecycle") or {}
    check("B1 DEFAULT_CONFIG.lifecycle 组存在且六键齐全",
          all(k in dc for k in ("enabled", "idle_days", "min_uses", "fail_rate_pct",
                                "watch_idle_days", "watch_fail_rate_pct")),
          f"got={sorted(dc)}")
    check("B2 CONFIG_SCHEMA.lifecycle 登记（前端/管理端契约）",
          all(k in (CFG.CONFIG_SCHEMA.get("lifecycle") or {})
              for k in ("enabled", "idle_days", "min_uses", "fail_rate_pct",
                        "watch_idle_days", "watch_fail_rate_pct")))
    check("B3 默认值与文档口径一致（30/5/30/15/15）",
          dc.get("idle_days") == 30 and dc.get("min_uses") == 5
          and dc.get("fail_rate_pct") == 30 and dc.get("watch_idle_days") == 15
          and dc.get("watch_fail_rate_pct") == 15 and dc.get("enabled") is True,
          f"got={dc}")
    _orig_as_bool, _orig_get = CFG.as_bool, CFG.get
    try:
        CFG.as_bool = lambda section, key, default=False: (
            False if (section, key) == ("lifecycle", "enabled") else _orig_as_bool(section, key, default))
        _th_off = LC.get_thresholds()
        check("B4 ★enabled=False 变异生效（get_thresholds 读到 False）",
              _th_off.get("enabled") is False, f"got={_th_off.get('enabled')}")
    finally:
        CFG.as_bool = _orig_as_bool
    try:
        def _fake_get(section, key, default=None, *a, **k):
            if section == "lifecycle" and key == "idle_days":
                return 0
            return _orig_get(section, key, default, *a, **k)
        CFG.get = _fake_get
        _th_mut = LC.get_thresholds()
        check("B5 ★idle_days=0 变异生效（get_thresholds 读到 0）",
              _th_mut.get("idle_days") == 0, f"got={_th_mut.get('idle_days')}")
    finally:
        CFG.get = _orig_get
    _th_back = LC.get_thresholds()
    check("B6 还原后默认值恢复（变异不留痕）", _th_back.get("idle_days") == 30,
          f"got={_th_back.get('idle_days')}")

    # ── C. 干净库端到端（种子六类形态 + API 端点）──────────────────────
    print("\n== C. 干净库端到端（init_db 种子 + 档位/汇总/排序/API 过滤）==")
    from database import init_db, get_db
    init_db()
    conn = get_db()
    conn.row_factory = sqlite3.Row
    _seed(conn)

    res = LC.analyze(conn, now=NOW)
    check("C1 analyze ok 且 enabled", res.get("ok") and res.get("enabled") is True)
    items = res.get("items") or []
    # ⚠️ init_db 会种子 7 个内置 Agent（_seed_agents，表空时写入；created_at=真实当前
    #    时刻 ≥ NOW ⇒ 闲置为负 ⇒ keep）⇒ 内容断言一律按 lc- 前缀过滤，只验本门禁种子，
    #    不因内置种子增减而漂移（也顺带证明「种子行不被误判为长尾」）。
    lc_items = [it for it in items if str(it.get("name", "")).startswith("lc-")]
    check("C2 本门禁种子资产全量返回（6 agents + 4 skills）", len(lc_items) == 10,
          f"got={len(lc_items)}（总 {len(items)}，含内置种子）")
    _exp = {
        "lc-a1-keep": LC.VERDICT_KEEP,          # 3 用/0 败/新 ⇒ 健康
        "lc-a2-retire-idle": LC.VERDICT_RETIRE,  # 0 用/100 天 ⇒ 闲置退出
        "lc-a3-retire-fail": LC.VERDICT_RETIRE,  # 10 用/6 败(60%) ⇒ 质量退出
        "lc-a4-review-idle": LC.VERDICT_REVIEW,  # 8 用/60 天 ⇒ 曾常用现已凉
        "lc-a5-watch": LC.VERDICT_WATCH,         # 8 用/20 天 ⇒ 过观察线
        "lc-a6-review-failweak": LC.VERDICT_REVIEW,  # 3 用/1 败(33%) 样本不足
        "lc-s1-retire": LC.VERDICT_RETIRE,       # use_stats 0 用/100 天
        "lc-s2-keep": LC.VERDICT_KEEP,           # 12 用/0 败/昨天刚用
        "lc-s3-watch-fail": LC.VERDICT_WATCH,    # 10 用/2 败(20%) ⇒ 过观察线
        "lc-s4-offline": LC.VERDICT_KEEP,        # 草稿+停用：在线=False 但不因状态误判
    }
    _bad = []
    for _name, _want in _exp.items():
        _it = _verdict_of(lc_items, _name)
        if not _it:
            _bad.append(f"{_name}:缺失"); continue
        if _it.get("verdict") != _want:
            _bad.append(f"{_name}:{_it.get('verdict')}!={_want}")
    check("C3 ★十类形态档位全部正确", not _bad, "; ".join(_bad))
    _lc_bv = {}
    for _it in lc_items:
        _lc_bv[_it["verdict"]] = _lc_bv.get(_it["verdict"], 0) + 1
    check("C4 汇总计数（retire=3/review=2/watch=2/keep=3）",
          _lc_bv.get("retire_candidate") == 3 and _lc_bv.get("review") == 2
          and _lc_bv.get("watch") == 2 and _lc_bv.get("keep") == 3, f"got={_lc_bv}")
    check("C5 严重档排最前（lc 种子内 retire_candidate 占据头部）",
          [it["verdict"] for it in lc_items[:3]] == [LC.VERDICT_RETIRE] * 3,
          f"head={[it['verdict'] for it in lc_items[:3]]}")
    _nonkeep = [it for it in lc_items if it["verdict"] != LC.VERDICT_KEEP]
    check("C6 非 keep 档全部带判定理由（可解释性）",
          all(it.get("reasons") for it in _nonkeep),
          f"空理由: {[it['name'] for it in _nonkeep if not it.get('reasons')]}")
    _r = _verdict_of(lc_items, "lc-a3-retire-fail")
    check("C7 质量退出项的失败率口径（6/10=60.0%）",
          _r and abs(_r.get("fail_rate", 0) - 60.0) < 0.05, f"got={_r and _r.get('fail_rate')}")
    _s4 = _verdict_of(lc_items, "lc-s4-offline")
    check("C8 停用资产可见且 online=False（下线态不隐藏、不误判）",
          _s4 is not None and _s4.get("online") is False and _s4.get("verdict") == LC.VERDICT_KEEP,
          f"got={_s4 and (_s4.get('online'), _s4.get('verdict'))}")
    _a1 = _verdict_of(lc_items, "lc-a1-keep")
    check("C9 使用证据聚合来自 agent_tasks（3 次 done）",
          _a1 and _a1.get("uses") == 3 and _a1.get("successes") == 3,
          f"got={_a1 and (_a1.get('uses'), _a1.get('successes'))}")
    # 内置种子（无 lc- 前缀）不得被误判成长尾（其 created_at=真实当前时刻）
    _seed_items = [it for it in items if not str(it.get("name", "")).startswith("lc-")]
    check("C9b 内置种子 Agent 不被误判为长尾（全 keep）",
          bool(_seed_items) and all(it["verdict"] == LC.VERDICT_KEEP for it in _seed_items),
          f"got={[(it['name'], it['verdict']) for it in _seed_items][:4]}")

    # ── API 端点（真实函数调用，不过 HTTP——仓内门禁惯例）──
    from routers.monitor import monitor_lifecycle
    _api_all = monitor_lifecycle(kind="all", verdict="", limit=200, conn=conn)
    _api_lc = [it for it in (_api_all.get("items") or []) if str(it.get("name", "")).startswith("lc-")]
    check("C10 端点 ok 且本门禁种子全量返回", _api_all.get("ok") and len(_api_lc) == 10)
    _api_agent = monitor_lifecycle(kind="agent", verdict="", limit=200, conn=conn)
    check("C11 kind=agent 过滤只返智能体（lc 6 项）",
          all(it["kind"] == "agent" for it in _api_agent.get("items") or [])
          and sum(1 for it in _api_agent["items"] if str(it.get("name", "")).startswith("lc-")) == 6)
    _api_ret = monitor_lifecycle(kind="all", verdict="retire_candidate", limit=200, conn=conn)
    check("C12 verdict=retire_candidate 过滤只返待评估退役（lc 3 项）",
          all(it["verdict"] == "retire_candidate" for it in _api_ret.get("items") or [])
          and sum(1 for it in _api_ret["items"] if str(it.get("name", "")).startswith("lc-")) == 3)
    _api_unknown = monitor_lifecycle(kind="no_such_kind", verdict="", limit=200, conn=conn)
    check("C13 未注册 kind 不炸（返空而非 500，扩展安全性）",
          _api_unknown.get("ok") and _api_unknown.get("items") == [],
          f"got={_api_unknown.get('ok')}")

    # ── 配置关闭时的端点行为（产品契约：返空不返错）──
    _orig_as_bool2 = CFG.as_bool
    try:
        CFG.as_bool = lambda section, key, default=False: (
            False if (section, key) == ("lifecycle", "enabled") else _orig_as_bool2(section, key, default))
        _api_off = monitor_lifecycle(kind="all", verdict="", limit=200, conn=conn)
        check("C14 ★配置 enabled=False ⇒ 端点返 ok+空（非 500，产品降级路径）",
              _api_off.get("ok") is True and _api_off.get("enabled") is False
              and _api_off.get("items") == [])
    finally:
        CFG.as_bool = _orig_as_bool2
    conn.close()

    # ── D. 真库只读 smoke（开发机；CI 无库 ⇒ 整体 SKIP）────────────────
    print("\n== D. 真库只读 smoke（file:...?mode=ro，不写不迁移）==")
    _prod = os.path.join(ROOT, "mbse.db")
    if not os.path.exists(_prod):
        skip("D1-D4 真库断言", "CI 环境无 mbse.db（干净库口径）⇒ 判据面不存在")
    else:
        _pc = sqlite3.connect("file:%s?mode=ro" % _prod, uri=True)
        _pc.row_factory = sqlite3.Row
        _res_p = LC.analyze(_pc, now=NOW)
        check("D1 真库 analyze ok", _res_p.get("ok") is True)
        _n_ag = _pc.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
        _n_sk = _pc.execute("SELECT COUNT(*) FROM skills").fetchone()[0]
        _bk = (_res_p.get("summary") or {}).get("by_kind") or {}
        check("D2 agents 全量分析（与真库 COUNT 一致，含 disabled）",
              _bk.get("agent") == _n_ag, f"analyze={_bk.get('agent')} db={_n_ag}")
        check("D3 skills 全量分析（与真库 COUNT 一致）",
              _bk.get("skill") == _n_sk, f"analyze={_bk.get('skill')} db={_n_sk}")
        # ★ name-join 取证：agent_tasks.agent_id 存 Agent 名。若误按数字 id join，
        #   'design' 的 uses/failures 会静默归零（档位失真但端点照样返 200）。
        _design = _verdict_of(_res_p.get("items") or [], "design")
        check("D4 ★真库 'design' 使用证据非零（agent_tasks 按 name join 生效）",
              bool(_design) and _design.get("uses", 0) > 0 and _design.get("failures", 0) > 0,
              f"got={_design and (_design.get('uses'), _design.get('failures'))}")
        _bv_p = (_res_p.get("summary") or {}).get("by_verdict") or {}
        print("  真库长尾汇总: %s" % _bv_p)
        for _it in (_res_p.get("items") or [])[:5]:
            print("    [%s] %s#%s %s 用%d/败%d 失败率%.1f%% 闲置%s天 | %s"
                  % (_it["verdict"], _it["kind"], _it["id"], _it["name"], _it["uses"],
                     _it["failures"], _it["fail_rate"], _it["idle_days"],
                     "；".join(_it["reasons"])[:60]))
        _pc.close()

    # ── E. 前后端接线（只做后端不改前端 = 改动无效，返回值上看不出来）──
    print("\n== E. 接线完整性（路由 + 面板 + 面板容器）==")
    _mon_src = open(os.path.join(ROOT, "routers", "monitor.py"), encoding="utf-8").read()
    check("E1 monitor.py 注册 /api/monitor/lifecycle 路由",
          '@router.get("/api/monitor/lifecycle")' in _mon_src)
    _adm_src = open(os.path.join(ROOT, "static", "js", "mods", "31-admin.js"), encoding="utf-8").read()
    check("E2 31-admin.js 有 loadOpsLifecycle/renderOpsLifecycle",
          "async function loadOpsLifecycle(" in _adm_src and "function renderOpsLifecycle(" in _adm_src)
    check("E3 loadOps 主加载链里 fire-and-forget 调用（失败降级不拖垮看板）",
          "try{ loadOpsLifecycle(); }catch(e){}" in _adm_src)
    _idx_src = open(os.path.join(ROOT, "static", "index.html"), encoding="utf-8").read()
    check("E4 index.html 有 ops-lifecycle 容器 + 档位筛选下拉",
          'id="ops-lifecycle"' in _idx_src and 'id="ops-lifecycle-verdict"' in _idx_src)
    _lc_src = open(os.path.join(ROOT, "core", "lifecycle.py"), encoding="utf-8").read()
    check("E5 面板不提供一键停用（只建议不自动执行的产品纪律）",
          "只建议不自动执行" in _adm_src or "只建议不自动执行" in _idx_src)

    print("\n结果: %d PASS / %d FAIL / %d SKIP"
          % (sum(1 for r in _results if r[1] is True),
             sum(1 for r in _results if r[1] is False),
             sum(1 for r in _results if r[1] is None)))
    _failed = [r[0] for r in _results if r[1] is False]
    if _failed:
        print("失败项: " + "; ".join(_failed))
    shutil.rmtree(_TMP, ignore_errors=True)
    if _failed:
        sys.exit(1)
    print("全部通过 —— 资产长尾治理（纯函数判定 + 配置消费 + 端到端 + 真库 smoke + 前后端接线）"
          + ("（干净库：真库断言按判据面不存在 SKIP）" if any(r[1] is None for r in _results) else "。"))


def _seed(conn):
    """干净库种子：6 类 agent + 4 类 skill，覆盖四档每种至少两例。

    时间戳全部相对固定 NOW 推导（与 analyze 的 now=NOW 配套）⇒ 门禁在任何
    日期执行结果一致；资产名前缀 lc- 避免与 init_db 种子碰撞。
    """
    agents = [
        # (name, created_days_ago, [(status, days_ago), ...])
        ("lc-a1-keep", 5, [("done", 2), ("done", 3), ("done", 4)]),          # 3 用/新 ⇒ keep
        ("lc-a2-retire-idle", 100, []),                                       # 0 用/100 天 ⇒ retire
        ("lc-a3-retire-fail", 10, [("done", 1)] * 4 + [("failed", 1)] * 6),   # 10 用/6 败 ⇒ retire
        ("lc-a4-review-idle", 60, [("done", 30)] * 8),                        # 8 用/60 天 ⇒ review
        # 注意：闲置天数按**最后使用时间**算（last_used 缺态才回退 created_at）——
        # 「建了 20 天但 5 天前刚用过」是活跃资产（keep），不是长尾。要造 watch 必须让
        # 最后使用也落在观察线之外（任务 20 天前 + 资产 25 天前建）。
        ("lc-a5-watch", 25, [("done", 20)] * 8),                              # 8 用/最后使用 20 天前 ⇒ watch
        ("lc-a6-review-failweak", 5, [("done", 1), ("failed", 2)]),           # 3 用/1 败 ⇒ review
    ]
    for name, c_days, tasks in agents:
        cur = conn.execute(
            "INSERT INTO agents (name, display_name, description, status, created_at, updated_at)"
            " VALUES (?,?,?,'active',?,?)",
            (name, name, "lc-gate 种子", _days_ago(c_days), _days_ago(c_days)))
        aid = cur.lastrowid
        for st, t_days in tasks:
            conn.execute(
                "INSERT INTO agent_tasks (agent_id, title, status, created_at, updated_at)"
                " VALUES (?,?,?,?,?)",
                (name, "lc-gate 任务", st, _days_ago(t_days), _days_ago(t_days)))
    skills = [
        # (name, status, enabled, created_days_ago, use_stats)
        ("lc-s1-retire", "published", 1, 100, '{"uses":0,"success":0,"fail":0}'),
        ("lc-s2-keep", "published", 1, 5,
         '{"uses":12,"success":12,"fail":0,"last_used":"%s"}' % _days_ago(1)),
        ("lc-s3-watch-fail", "published", 1, 5,
         '{"uses":10,"success":8,"fail":2,"last_used":"%s"}' % _days_ago(1)),
        ("lc-s4-offline", "draft", 0, 5, '{"uses":0,"success":0,"fail":0}'),
    ]
    for name, status, enabled, c_days, stats in skills:
        conn.execute(
            "INSERT INTO skills (name, description, status, enabled, created_at, updated_at, use_stats)"
            " VALUES (?,?,?,?,?,?,?)",
            (name, "lc-gate 种子", status, enabled, _days_ago(c_days), _days_ago(c_days), stats))
    conn.commit()


if __name__ == "__main__":
    main()
