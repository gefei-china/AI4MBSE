# -*- coding: utf-8 -*-
"""P2-1 门禁：misevolution 安全回归监控（红线 #5：自进化的前置条件）。

═══════════════════════════════════════════════════════════════
动机（2026-10-18《自进化 AI 平台调研评估》红线 #5 + 路线图 P2 首项）
═══════════════════════════════════════════════════════════════
P1-4 之后系统可自改配置；misevolution 研究（decrypt 2025）证实智能体会在
**纯内部循环**里安全滑坡（99.4%→54.4%）且无外部触发。本门禁守 core/misevolution
的信号口径与晋升复测链路：信号诚实（空窗口不是 0%）、基线可冻结可比较、
晋升后必复测、复测零配置写且**不自动回滚**（回滚过人）。

判据结构（承 verify_sandbox / verify_config_proposal 的五段式）：
  A 纯函数    grade 方向分级 + worse_delta + compare_signals 三态
              （clean / regression / insufficient_data；**空基线 ≠ clean**）
  B 配置      misevolution 组双登记 + 键集合一致 + 引擎零写语句（静态）+
              口径真被消费（影子配置变异自证）
  C 端到端    干净临时库（import 前切 MBSE_DB_PATH/MBSE_CONFIG_PATH）：
              合成数据已知速率 → 空窗口诚实 → 基线往返 → 回归判定 →
              ★execute_confirmation 全链路（配置真改 + 复测记录 + HIL 零新增）
              → 告警发布（百分数 / None 语义 / 空数据不告警）→ 白名单审计
              写入点行为级 → 三组变异自证
  D 真库      只读取证：五信号实况（不断言具体值——真库状态会变）
  E 接线      复测钩子两分支 + 端点（路由真注册 + 采集失败显式 ok:False
              不 500/不落半条基线）+ 告警注册与 None 跳过 + repo 只碰两表
              + 白名单写入点 + 迁移三处接线 + misev 与 run 级指标无交集

运行：python tools/verify/verify_misevolution.py
"""
import io
import os
import re
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# ── 干净库 + 隔离配置：先切后 import（core.config/DB 路径 import 期绑定）──
# 配置隔离是必须的：C 段要验 execute_confirmation 全链路（save_override 是
# 唯一配置写路径），不隔离就会写到用户真配置。
_TMP = os.path.join(ROOT, "tmp", "verify_misevolution")
os.makedirs(_TMP, exist_ok=True)
_CLEAN_DB = os.path.join(_TMP, "clean.db")
_CLEAN_CFG = os.path.join(_TMP, "config.json")
for _p in (_CLEAN_DB, _CLEAN_CFG):
    if os.path.exists(_p):
        os.remove(_p)
# 预建空配置：save_override 要有落点（C7 全链路会经它写配置）
io.open(_CLEAN_CFG, "w", encoding="utf-8").write("{}")
os.environ["MBSE_DB_PATH"] = _CLEAN_DB
os.environ["MBSE_CONFIG_PATH"] = _CLEAN_CFG
_PROD_DB = os.path.join(ROOT, "mbse.db")

_results = []


def check(name, ok, detail=""):
    _results.append((name, bool(ok), detail))
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name,
                           ("  ← " + str(detail)) if (detail and not ok) else ""))
    return bool(ok)


def skip(name, why):
    _results.append((name, None, why))
    print("  [SKIP] " + name + "  -- " + str(why))


def section(t):
    print("\n== " + t + " " + "=" * max(0, 60 - len(t)))


def _read(rel):
    return io.open(os.path.join(ROOT, rel), encoding="utf-8").read()


def _n_rows(conn, table):
    return int(conn.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0])


# ═══════════════════════════════════════════════════════════════
section("A 纯函数：分级方向 + 三态比较")
# ═══════════════════════════════════════════════════════════════
from core import misevolution as me                      # noqa: E402

# A1 grade：lower_better 方向 + unknown 诚实
check("A1a rate=None ⇒ unknown（空窗口不是 0）",
      me.grade(None, warn=0.05, alert=0.15, lower_better=True) == me.STATUS_UNKNOWN)
check("A1b lower_better：==warn ⇒ warn（闭区间）",
      me.grade(0.05, warn=0.05, alert=0.15, lower_better=True) == me.STATUS_WARN)
check("A1c lower_better：>alert ⇒ alert",
      me.grade(0.16, warn=0.05, alert=0.15, lower_better=True) == me.STATUS_ALERT)
check("A1d lower_better：<warn ⇒ ok",
      me.grade(0.04, warn=0.05, alert=0.15, lower_better=True) == me.STATUS_OK)
# A2 grade：覆盖率类反向（越低越坏）
check("A2a 覆盖率类：==warn ⇒ warn",
      me.grade(0.50, warn=0.50, alert=0.20, lower_better=False) == me.STATUS_WARN)
check("A2b 覆盖率类：<alert ⇒ alert（致盲）",
      me.grade(0.19, warn=0.50, alert=0.20, lower_better=False) == me.STATUS_ALERT)
check("A2c 覆盖率类：>warn ⇒ ok",
      me.grade(0.80, warn=0.50, alert=0.20, lower_better=False) == me.STATUS_OK)
# A3 worse_delta 方向
check("A3a worse_delta lower_better=正向差",
      abs(me.worse_delta(0.20, 0.10, lower_better=True) - 0.10) < 1e-12)
check("A3b worse_delta 覆盖率类=反向差（下滑才是恶化）",
      abs(me.worse_delta(0.20, 0.40, lower_better=False) - 0.20) < 1e-12)


def _sig(rate, sufficient=True):
    return {"rate": rate, "sufficient": sufficient}


# A4 compare：同快照 ⇒ clean
_same = {n: _sig(0.05) for n in me.SPEC_NAMES}
_c0 = me.compare_signals(_same, _same, settings={"regression_delta": 0.05})
check("A4a 同快照比较 ⇒ clean", _c0["verdict"] == me.VERDICT_CLEAN, _c0)
check("A4b compared=5（五信号全参与）", _c0["compared"] == 5, _c0)
# A5 compare：恶化超 delta ⇒ regression（逐信号列出）
#   注意覆盖率类是「越高越好」⇒ 恶化 = 现值低于基线（方向别设反）
_base5 = {n: _sig(0.05) for n in me.SPEC_NAMES}
_base5["kind_coverage"] = _sig(0.10)           # 覆盖率基线高
_cur = dict(_base5)
_cur["tool_fail_rate"] = _sig(0.30)            # +0.25 > 0.05 ⇒ 恶化
_cur["kind_coverage"] = _sig(0.01)             # -0.09（反向）⇒ 恶化
_c1 = me.compare_signals(_cur, _base5, settings={"regression_delta": 0.05})
check("A5a 恶化超阈值 ⇒ regression", _c1["verdict"] == me.VERDICT_REGRESSION, _c1)
check("A5b regressed 精确列出两个信号",
      set(_c1["regressed"]) == {"tool_fail_rate", "kind_coverage"}, _c1["regressed"])
check("A5c 覆盖率**改善**不判 regression（方向敏感性）",
      me.compare_signals({"kind_coverage": _sig(0.30)}, _base5,
                         settings={"regression_delta": 0.05})["verdict"]
      == me.VERDICT_CLEAN)
# A6 compare：恰好等于阈值不判（严格大于）
_eq = dict(_same)
_eq["tool_fail_rate"] = _sig(0.10)             # 恰好 +0.05
_c2 = me.compare_signals(_eq, _same, settings={"regression_delta": 0.05})
check("A6 delta 恰好等于阈值 ⇒ 不判 regression（严格大于）",
      _c2["verdict"] == me.VERDICT_CLEAN, _c2)
# A7 compare：任一侧分母不足 ⇒ excluded；全 excluded ⇒ insufficient_data
_ins = {n: _sig(None, sufficient=False) for n in me.SPEC_NAMES}
_c3 = me.compare_signals(_ins, _same, settings={"regression_delta": 0.05})
check("A7a 全 excluded ⇒ insufficient_data（不假装 clean）",
      _c3["verdict"] == me.VERDICT_INSUFFICIENT and _c3["compared"] == 0, _c3)
_half = dict(_same)
_half["auth_reject_rate"] = _sig(None, sufficient=False)
_c4 = me.compare_signals(_half, _same, settings={"regression_delta": 0.05})
check("A7b 单侧不足 ⇒ 该信号 excluded、其余照判",
      _c4["signals"]["auth_reject_rate"]["excluded"] is True
      and _c4["compared"] == 4 and _c4["verdict"] == me.VERDICT_CLEAN, _c4)
# A8 compare：空基线/空当前 ⇒ insufficient_data（不是 clean）
check("A8a 空基线 dict ⇒ insufficient_data",
      me.compare_signals(_same, {}, settings={"regression_delta": 0.05})["verdict"]
      == me.VERDICT_INSUFFICIENT)
check("A8b 空当前 ⇒ insufficient_data",
      me.compare_signals({}, _same, settings={"regression_delta": 0.05})["verdict"]
      == me.VERDICT_INSUFFICIENT)
# A9 注册表完整性
check("A9a 五信号齐备且名字唯一",
      len(me.SPEC_NAMES) == 5 and len(set(me.SPEC_NAMES)) == 5, me.SPEC_NAMES)
check("A9b 信号规格完整（label/desc/双 SQL 齐备且只读）",
      all(s.label and s.desc
          and s.num_sql.strip().upper().startswith("SELECT")
          and s.den_sql.strip().upper().startswith("SELECT")
          for s in me._SPECS))

# ═══════════════════════════════════════════════════════════════
section("B 配置：双登记 + 引擎零写 + 口径被消费")
# ═══════════════════════════════════════════════════════════════
from core import config as _cfg                                    # noqa: E402

_dflt = _cfg.DEFAULT_CONFIG.get("misevolution") or {}
_sch = _cfg.CONFIG_SCHEMA.get("misevolution") or {}
check("B1 misevolution 组在 DEFAULT_CONFIG", bool(_dflt))
check("B2 misevolution 组在 CONFIG_SCHEMA", bool(_sch))
check("B3 两处键集合一致", set(_dflt) == set(_sch),
      "defaults-only=%s schema-only=%s" % (set(_dflt) - set(_sch),
                                           set(_sch) - set(_dflt)))
check("B4 组内 14 键（1 开关 + 3 口径 + 10 阈值）", len(_dflt) == 14, sorted(_dflt))
# B5 引擎零写语句（静态：代码区无写 SQL 关键词；SQL 串亦只有 SELECT）
_eng_src = _read(os.path.join("core", "misevolution.py"))
_code = re.sub(r'""".*?"""', "", _eng_src, flags=re.S)      # 去 docstring 防误伤
check("B5a 引擎无写语句（INSERT/UPDATE/DELETE/CREATE/DROP/ALTER）",
      not re.search(r"\b(INSERT|UPDATE|DELETE|CREATE|DROP|ALTER|REPLACE)\b", _code))
check("B5b 引擎所有 SQL 都是 SELECT 开头",
      all(s.strip().upper().startswith("SELECT")
          for s in re.findall(r'"(SELECT[^"]*)"', _eng_src)))
# B6 口径真被消费：影子配置覆盖 window_days ⇒ get_settings 读到新值
#   （同时验证 P1-4 影子层对本组生效；生产进程不设 ⇒ 行为零变化）
_old_shadow = os.environ.get("MBSE_SHADOW_CONFIG")
try:
    os.environ["MBSE_SHADOW_CONFIG"] = '{"misevolution": {"window_days": 99}}'
    _cfg.reload()
    _s = me.get_settings()
    check("B6a 影子配置覆盖 window_days ⇒ settings 读到 99", _s["window_days"] == 99, _s)
    check("B6b 影子未覆盖的键仍取默认（分层合并，不是整体替换）",
          _s["regression_delta"] == 0.05, _s.get("regression_delta"))
    os.environ["MBSE_SHADOW_CONFIG"] = '{"misevolution": {"no_such_key": 1}}'
    _raised = False
    try:
        _cfg.reload()
    except ValueError:
        _raised = True
    check("B6c 未知键 fail-loud（typo 不静默失效）", _raised)
finally:
    if _old_shadow is None:
        os.environ.pop("MBSE_SHADOW_CONFIG", None)
    else:
        os.environ["MBSE_SHADOW_CONFIG"] = _old_shadow
    _cfg.reload()

# ═══════════════════════════════════════════════════════════════
section("C 端到端：干净库全链路")
# ═══════════════════════════════════════════════════════════════
from database.schema import init_db                                # noqa: E402
from database import db_conn                                       # noqa: E402

init_db()
_db_cm = db_conn()
_conn = _db_cm.__enter__()          # 门禁进程内长连接（__exit__ 在末尾）
_c = _conn.cursor()

# C1 迁移建两表（列齐 + 索引）
_tbls = {r[0] for r in _c.execute(
    "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
check("C1a misevolution_baselines 已建", "misevolution_baselines" in _tbls)
check("C1b misevolution_checks 已建", "misevolution_checks" in _tbls)
_bl_cols = [r[1] for r in _c.execute(
    "PRAGMA table_info(misevolution_baselines)").fetchall()]
_ck_cols = [r[1] for r in _c.execute(
    "PRAGMA table_info(misevolution_checks)").fetchall()]
check("C1c baselines 六列（epoch/signals_json/note/frozen_by）",
      _bl_cols[:2] == ["id", "epoch"] and "signals_json" in _bl_cols, _bl_cols)
check("C1d checks 八列（epoch/source/signals_json/comparison_json/verdict）",
      _ck_cols[:2] == ["id", "epoch"] and "comparison_json" in _ck_cols
      and "verdict" in _ck_cols, _ck_cols)

# C2 合成数据：100 审计事件（5 拒答）+ 100 工具调用（30 失败/15 带 kind）
#   失败数 30 ≥ min_denominator(20) ⇒ kind_coverage 可算（10 条会被正确判
#   unknown，那是分母纪律不是缺陷）
#
# ★ 2026-10-26 **时间炸弹修复**：合成时间戳原写死 "2026-10-19 10:00:00"，
#   而 `collect_global_metrics` 走 `collect_signals(conn, settings)` 的**默认窗口**
#   （window_days=7 ⇒ cutoff = now-7d）。⇒ 当"今天"越过 10-19 10:00 + 7d 之后，
#   这批 C2 数据**滑出窗口**，C8 段只剩 C5 注入的 100 条全失败 ⇒ 发布值 100.0%，
#   C8c 期望的 65% 判红（C8e 连带）。
#   实测：10-25 基线还绿（cutoff=10-18，两批都在窗口内），10-26 变红
#   （cutoff=10-19 10:4x，C2 那批 10:00 差 71 分钟出窗）——**与产品无关，纯时间到了**。
#   ⇒ 修法：时间戳**相对 now**（恒在窗口内），并在 C8 段加"窗口内行数"前置断言，
#      万一窗口口径再变，报错直接说"合成数据滑出窗口"，而不是甩一个 100.0 让人猜。
_T_C2 = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 2 * 3600))
_T_C5 = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 1 * 3600))
for i in range(100):
    _c.execute("INSERT INTO audit_logs (user_name, event_type, detail, result) "
               "VALUES ('gate', ?, 'd', 'success')",
               ("auth_identity_reject" if i < 5 else "llm_chat",))
for i in range(100):
    _c.execute("INSERT INTO tool_call_logs (tool_name, ok, failure_kind, created_at) "
               "VALUES (?,?,?,?)",
               ("t%d" % i, 0 if i < 30 else 1, "timeout" if i < 15 else "",
                _T_C2))
_conn.commit()
_S = me.get_settings()
_sig = me.collect_signals(_conn, settings=_S, cutoff="2000-01-01 00:00:00")
_a, _f, _k = _sig["auth_reject_rate"], _sig["tool_fail_rate"], _sig["kind_coverage"]
check("C2a auth_reject 分子=5（拒答精确计数）", _a["numerator"] == 5, _a)
check("C2b auth_reject 速率自洽 num/den",
      abs(_a["rate"] - _a["numerator"] / _a["denominator"]) < 1e-12, _a)
check("C2c tool_fail=30/100=0.30", abs(_f["rate"] - 0.30) < 1e-9, _f)
check("C2d kind_coverage=15/30=0.50", abs(_k["rate"] - 0.50) < 1e-9, _k)

# C3 空窗口诚实：cutoff 在未来 ⇒ 全 unknown（不是 0）
_sig0 = me.collect_signals(_conn, settings=_S, cutoff="2999-01-01 00:00:00")
check("C3 空窗口五信号全 unknown（不是 0%）",
      all(_sig0[n]["status"] == me.STATUS_UNKNOWN and _sig0[n]["rate"] is None
          for n in me.SPEC_NAMES))
# C3b 分母不足：min_denominator 调到 1000 ⇒ 全 unknown
_S_big = dict(_S, min_denominator=1000)
_sig1 = me.collect_signals(_conn, settings=_S_big, cutoff="2000-01-01 00:00:00")
check("C3b 分母不足 min_denominator ⇒ unknown（样本少不装成没问题）",
      all(_sig1[n]["rate"] is None for n in me.SPEC_NAMES))

# C4 基线冻结 + 读取往返
from repositories.misevolution_repo import MisevolutionRepo          # noqa: E402
_repo = MisevolutionRepo(_conn)
_bl = _repo.freeze_baseline(epoch="gate:pre", signals=_sig,
                            note="门禁冻结", frozen_by="gate")
check("C4a freeze 返回行（含 id）", bool(_bl.get("id")), _bl)
_latest = _repo.latest_baseline()
check("C4b latest_baseline 往返：signals 解析回 dict 且五键齐",
      isinstance(_latest.get("signals"), dict)
      and all(n in _latest["signals"] for n in me.SPEC_NAMES))
check("C4c latest 取最新一条（基线是状态不是流水）",
      _latest["id"] == _bl["id"] and _latest["epoch"] == "gate:pre")
# C4d commit 纪律行为级：**第二条连接**能读到 ⇒ freeze_baseline 真提交了
#   （BaseRepo.execute 本身不提交；若 repo 不自带 commit，作业/脚本路径的
#   写入会在连接关闭时回滚——P1-4 在 sandbox_repo 实测踩过）
_c2nd = sqlite3.connect(_CLEAN_DB)
try:
    _vis = int(_c2nd.execute("SELECT COUNT(*) FROM misevolution_baselines "
                             "WHERE id=?", (_bl["id"],)).fetchone()[0])
    check("C4d 第二连接可见冻结行（repo 写方法自带 commit）", _vis == 1, _vis)
finally:
    _c2nd.close()

# C5 回归判定（端到端）：再注入 100 次全失败无 kind
for i in range(100):
    _c.execute("INSERT INTO tool_call_logs (tool_name, ok, failure_kind, created_at) "
               "VALUES (?,?,?,?)", ("r%d" % i, 0, "", _T_C5))
_conn.commit()
_sig2 = me.collect_signals(_conn, settings=_S, cutoff="2000-01-01 00:00:00")
_cmp = me.compare_signals(_sig2, _latest["signals"], settings=_S)
check("C5a 注入回归 ⇒ verdict=regression",
      _cmp["verdict"] == me.VERDICT_REGRESSION, _cmp)
check("C5b tool_fail_rate 与 kind_coverage 双回归",
      set(_cmp["regressed"]) == {"tool_fail_rate", "kind_coverage"}, _cmp["regressed"])
check("C5c 绝对阈值同步 alert（双阈值独立生效）",
      _sig2["tool_fail_rate"]["status"] == me.STATUS_ALERT
      and _sig2["kind_coverage"]["status"] == me.STATUS_ALERT)

# C6 晋升复测钩子：无基线 ⇒ no_baseline 记录
_c.execute("DELETE FROM misevolution_baselines")
_conn.commit()
from core import config_promotion as _cp                             # noqa: E402
_res = {}
_cp._recheck_after_change(_conn, conf={"id": 42}, kind="config_apply", result=_res)
check("C6a 无基线复测 ⇒ verdict=no_baseline（留痕不等基线）",
      _res.get("recheck", {}).get("verdict") == "no_baseline", _res)
_ck = _repo.get_check(_res["recheck"]["check_id"])
check("C6b check 行 source=promotion、epoch 带确认单号",
      _ck["source"] == me.SOURCE_PROMOTION
      and _ck["epoch"] == "promotion:conf#42:config_apply", _ck)

# C7 ★execute_confirmation 全链路（隔离配置下）：配置真改 + 复测记录 +
#    HIL 零新增（不自动回滚）+ 零越界写
from core import config as _cfg2                                     # noqa: E402
_before_hist = _n_rows(_conn, "config_history")
_before_hil = _n_rows(_conn, "hil_confirmations")
_before_checks = _n_rows(_conn, "misevolution_checks")
_conf_row = {"id": 77, "action": _cp.HIL_ACTION_APPLY, "decided_by": "gate",
             "payload": {"section": "sandbox", "key": "gate_timeout_s",
                         "value": "151", "proposal_id": 0}}
_out = _cp.execute_confirmation(_conn, _conf_row)
check("C7a execute_confirmation 执行成功（executed=True）",
      _out.get("executed") is True and _out.get("kind") == _cp.HIL_ACTION_APPLY, _out)
check("C7b 配置真被改（唯一写路径 save_override 落 151）",
      str(_cfg2.get("sandbox", "gate_timeout_s", 0)) == "151",
      _cfg2.get("sandbox", "gate_timeout_s", 0))
check("C7c config_history 新增一行", _n_rows(_conn, "config_history") == _before_hist + 1)
check("C7d 复测记录新增一行（source=promotion）",
      _n_rows(_conn, "misevolution_checks") == _before_checks + 1)
check("C7e ★HIL 确认单零新增（regression 也不自动回滚——回滚过人）",
      _n_rows(_conn, "hil_confirmations") == _before_hil)
_ck2 = _repo.list_checks(limit=1)[0]
check("C7f 复测 verdict ∈ 四态（clean/regression/insufficient/no_baseline）",
      _ck2["verdict"] in (me.VERDICT_CLEAN, me.VERDICT_REGRESSION,
                          me.VERDICT_INSUFFICIENT, "no_baseline"), _ck2["verdict"])

# C8 告警发布：百分数 + None 语义 + 空数据不告警
from core.alert_evaluator import (GLOBAL_METRICS, collect_global_metrics,  # noqa: E402
                                  evaluate_once)
check("C8a 五 misev_ 指标已注册 GLOBAL_METRICS",
      all(("misev_" + n) in GLOBAL_METRICS for n in me.SPEC_NAMES))
_m = collect_global_metrics(_conn)
check("C8b 发布值为百分数（0-100 量纲）",
      all((_m["misev_" + n] is None) or (0 <= _m["misev_" + n] <= 100)
          for n in me.SPEC_NAMES),
      {k: _m["misev_" + k] for k in me.SPEC_NAMES})
# C8b2 ★ 2026-10-26：**发布口径与同库数据自洽**（不再拿魔法常数当期望值）
#   期望值从**同库现算**：失败数/总数×100。窗口口径（window_days）一旦变化，
#   期望值自动跟着变 ⇒ 不会因为"改了窗口配置"而假红。
#   ⚠️ 附带守卫：分母必须=200（门禁自己注入的两批共 200 行）——
#   分母偏小 ⇒ 说明合成数据**滑出窗口**了（2026-10-26 那次时间炸弹的现场），
#   此时报"分母=N"比甩一个 100.0 可读得多。
_cut = me._cutoff(int(_S["window_days"]))
_win = _c.execute("SELECT COUNT(*), SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END) "
                   "FROM tool_call_logs WHERE created_at>=?", (_cut,)).fetchone()
_win_tot, _win_fail = int(_win[0] or 0), int(_win[1] or 0)
check("C8b2 合成数据**全部落在信号窗口内**（分母=200；否则是时间炸弹，不是数据问题）",
      _win_tot == 200, "窗口内 %d 行（失败 %d）；cutoff=%s" % (_win_tot, _win_fail, _cut))
_expect_fail = (round(100.0 * _win_fail / _win_tot, 2) if _win_tot else None)
check("C8c tool_fail 百分数自洽（发布值 = 同库失败数/总数×100）",
      _expect_fail is not None and abs(_m["misev_tool_fail_rate"] - _expect_fail) < 0.01,
      "发布=%r 期望=%r（窗口内 %d/%d）"
      % (_m["misev_tool_fail_rate"], _expect_fail, _win_fail, _win_tot))
# C8d 空数据不告警：五条 misev 规则 + 指标全 None ⇒ 全跳过不 fire
#   （monkeypatch collect_global_metrics 返全 None，模拟「没数据」；
#     不另建空库是因为规则/事件表结构由 init_db 管，补丁更贴近口径本身）
_c.execute("DELETE FROM alert_rules")
_c.execute("DELETE FROM alert_events")
for _mn, _op, _th in (("misev_auth_reject_rate", ">", 10),
                      ("misev_perm_denied_rate", ">", 10),
                      ("misev_whitelist_denied_rate", ">", 10),
                      ("misev_tool_fail_rate", ">", 50),
                      ("misev_kind_coverage", "<", 1)):
    _c.execute("INSERT INTO alert_rules (name, metric, operator, threshold, level, status) "
               "VALUES (?,?,?,?,'warning','active')", ("gate_" + _mn, _mn, _op, _th))
_conn.commit()
_orig_cgm = collect_global_metrics
try:
    import core.alert_evaluator as _ale
    _ale.collect_global_metrics = lambda conn: {
        k: None for k in GLOBAL_METRICS}
    _ev0 = evaluate_once(conn=_conn, notify=False)
finally:
    _ale.collect_global_metrics = _orig_cgm
check("C8d 空数据（全 None）evaluate_once：fired=0、skipped_unknown=5（没数据不告警）",
      _ev0["fired"] == 0 and _ev0.get("skipped_unknown", -1) == 5, _ev0)
# C8e 有数据 + 越阈 ⇒ 真告警（证明 None 跳过没有把真信号也吞了）：
#   干净库实况 tool_fail=130/200=65% > 50 ⇒ 恰好一条 fire（其余四条阈值不越界）
_ev1 = evaluate_once(conn=_conn, notify=False)
_fired_rows = _n_rows(_conn, "alert_events")
check("C8e tool_fail 65% > 50 阈值 ⇒ fired=1 且落 alert_events",
      _ev1["fired"] == 1 and _fired_rows == 1, _ev1)

# C9 ★白名单审计写入点行为级：裸对象直调 _exec_tool_call ⇒ 审计行落库
from agent.pipeline_parts.tools import ToolMixin                     # noqa: E402
_obj = ToolMixin.__new__(ToolMixin)
_obj._tool_whitelist = ["allowed_tool"]
_obj._tool_user = None
_res9 = _obj._exec_tool_call("evil_tool", {"x": 1})
_audit_n = int(_c.execute("SELECT COUNT(*) FROM audit_logs "
                          "WHERE event_type='tool_whitelist_denied'").fetchone()[0])
check("C9a 白名单外工具被拒（ok=False + 最小权限文案）",
      _res9.get("ok") is False and "白名单" in str(_res9.get("result")), _res9)
check("C9b ★拒绝留痕：tool_whitelist_denied 审计行落库", _audit_n >= 1, _audit_n)
check("C9c tool_call_logs 亦记录该拒绝（双轨：日志 + 审计）",
      int(_c.execute("SELECT COUNT(*) FROM tool_call_logs "
                     "WHERE tool_name='evil_tool' AND ok=0").fetchone()[0]) >= 1)

# ═══════════════════════════════════════════════════════════════
section("C' 变异自证（猴子补丁，finally 恢复）")
# ═══════════════════════════════════════════════════════════════
_orig_worse, _orig_grade, _orig_collect = me.worse_delta, me.grade, me.collect_signals
try:
    # V1 方向取反 ⇒ 回归抓不到（证明方向判据真在干活）
    me.worse_delta = lambda rate, baseline_rate, *, lower_better: (
        -(float(rate) - float(baseline_rate)))
    _v1 = me.compare_signals(_cur, _same, settings={"regression_delta": 0.05})
    check("V1 worse_delta 方向取反 ⇒ regression 抓不到（变异被感知）",
          _v1["verdict"] != me.VERDICT_REGRESSION, _v1["verdict"])
finally:
    me.worse_delta = _orig_worse
check("V1r 恢复后同数据 ⇒ regression（判据复位）",
      me.compare_signals(_cur, _same,
                         settings={"regression_delta": 0.05})["verdict"]
      == me.VERDICT_REGRESSION)
try:
    # V2 unknown 分支被换成 ok ⇒ 空窗口伪装成没问题
    me.grade = lambda rate, *, warn, alert, lower_better: (
        me.STATUS_OK if rate is None else _orig_grade(
            rate, warn=warn, alert=alert, lower_better=lower_better))
    _v2 = me.collect_signals(_conn, settings=_S, cutoff="2999-01-01 00:00:00")
    check("V2 unknown→ok ⇒ 空窗口全绿（诚实分支被感知）",
          all(_v2[n]["status"] == me.STATUS_OK for n in me.SPEC_NAMES))
finally:
    me.grade = _orig_grade
check("V2r 恢复后空窗口 ⇒ 全 unknown",
      all(me.collect_signals(_conn, settings=_S,
                             cutoff="2999-01-01 00:00:00")[n]["status"]
          == me.STATUS_UNKNOWN for n in me.SPEC_NAMES))
try:
    # V3 信号计算抛错 ⇒ 复测钩子不得搞坏晋升（异常只进 recheck_error）
    def _boom(*a, **k):
        raise RuntimeError("simulated signal failure")
    me.collect_signals = _boom
    _res3 = {}
    _cp._recheck_after_change(_conn, conf={"id": 99}, kind="config_apply",
                              result=_res3)
    check("V3 信号计算抛错 ⇒ 钩子不抛、recheck_error 留痕（晋升不被搞坏）",
          "recheck" not in _res3 and bool(_res3.get("recheck_error")), _res3)
finally:
    me.collect_signals = _orig_collect
check("V3r 恢复后复测钩子正常记录",
      _cp._recheck_after_change(_conn, conf={"id": 100}, kind="config_apply",
                                result={}) is None)

# ═══════════════════════════════════════════════════════════════
section("D 真库：只读取证（数字会变，只验结构与自洽）")
# ═══════════════════════════════════════════════════════════════
if not os.path.exists(_PROD_DB):
    skip("D1 真库 collect_signals", "mbse.db 不存在（%s）" % _PROD_DB)
else:
    _pc = sqlite3.connect("file:%s?mode=ro" % _PROD_DB.replace("\\", "/"), uri=True)
    try:
        _ps = me.collect_signals(_pc, settings=_S)
        check("D1a 真库五信号可取（结构完整）",
              all(n in _ps for n in me.SPEC_NAMES)
              and all(k in _ps[n] for n in me.SPEC_NAMES
                      for k in ("numerator", "denominator", "rate", "status")))
        check("D1b 窗口 meta 完整（window_days/cutoff/min_denominator）",
              all(k in _ps["meta"] for k in ("window_days", "cutoff",
                                             "min_denominator")))
        check("D1c status ∈ 四态（ok/warn/alert/unknown）",
              all(_ps[n]["status"] in (me.STATUS_OK, me.STATUS_WARN,
                                       me.STATUS_ALERT, me.STATUS_UNKNOWN)
                  for n in me.SPEC_NAMES))
        print("     真库实况（%s 窗口，cutoff=%s）："
              % (_ps["meta"]["window_days"], _ps["meta"]["cutoff"]))
        for n in me.SPEC_NAMES:
            s = _ps[n]
            print("       %-22s %-7s %d/%d" % (
                n, s["status"], s["numerator"], s["denominator"]))
        # D2 速率自洽（真库口径）
        check("D2 可算信号的速率 = num/den（无口径漂移）",
              all(_ps[n]["rate"] is None
                  or abs(_ps[n]["rate"] - _ps[n]["numerator"]
                         / _ps[n]["denominator"]) < 1e-9
                  for n in me.SPEC_NAMES))
        # D3 迁移幂等：真库（只读连接不能跑迁移，此处验证干净库二次 init_db）
        init_db()
        _pc2 = sqlite3.connect(_CLEAN_DB)
        try:
            _n = int(_pc2.execute("SELECT COUNT(*) FROM misevolution_checks").fetchone()[0])
            check("D3 二次 init_db 不炸且数据不丢（幂等）", _n >= 1, _n)
        finally:
            _pc2.close()
    finally:
        _pc.close()

# ═══════════════════════════════════════════════════════════════
section("E 接线：钩子/端点/告警/repo/写入点/迁移")
# ═══════════════════════════════════════════════════════════════
_promo_src = _read(os.path.join("core", "config_promotion.py"))
check("E1a 复测钩子接入 apply 分支",
      _promo_src.count("_recheck_after_change(conn, conf=conf, kind=HIL_ACTION_APPLY") == 1)
check("E1b 复测钩子接入 rollback 分支（回滚后同样复测）",
      _promo_src.count("_recheck_after_change(conn, conf=conf, kind=HIL_ACTION_ROLLBACK") == 1)
check("E1c 钩子定义存在且旁路 try（监控失败不搞坏晋升）",
      "def _recheck_after_change" in _promo_src
      and "recheck_error" in _promo_src)
_mon_src = _read(os.path.join("routers", "monitor.py"))
# E2 行为级：路由真注册（抓装饰器被摘/路径被改），非源码字符串匹配
from routers import monitor as _mon_mod                       # noqa: E402
_mon_paths = {}
for _r in _mon_mod.router.routes:
    _mon_paths.setdefault(getattr(_r, "path", ""), set()).update(
        getattr(_r, "methods", None) or set())
check("E2a GET /api/monitor/misevolution 路由已注册（行为级）",
      "GET" in _mon_paths.get("/api/monitor/misevolution", set()),
      sorted(_mon_paths.get("/api/monitor/misevolution", set())))
check("E2b POST /api/monitor/misevolution/baseline 路由已注册（行为级）",
      "POST" in _mon_paths.get("/api/monitor/misevolution/baseline", set()),
      sorted(_mon_paths.get("/api/monitor/misevolution/baseline", set())))
check("E2c 端点 enabled=False 不静默降级（返 enabled: False）",
      '"enabled": False' in _mon_src)
# E2d~f 采集失败路径：冒烟（TestClient 无 lifespan 的冷库）实测会 500——
# 监控端点必须显式失败（ok:False + error），不 500、不静默、不落半条基线
_orig_collect = me.collect_signals
def _boom_collect(*_a, **_kw):
    raise RuntimeError("__mev_boom__")
me.collect_signals = _boom_collect
try:
    try:
        _r_get = _mon_mod.monitor_misevolution(conn=_conn)
    except Exception as _e:            # 回归形态：异常裸奔 ⇒ 记为失败而非崩掉门禁
        _r_get = {"__raised__": repr(_e)}
    check("E2d 信号采集失败 ⇒ GET 显式 ok:False+error（不 500 不静默）",
          isinstance(_r_get, dict) and _r_get.get("ok") is False
          and "error" in _r_get and "__mev_boom__" in str(_r_get.get("error")),
          _r_get)
    try:
        _r_post = _mon_mod.freeze_misevolution_baseline(
            epoch="e2d-boom", note="", conn=_conn, user=None)
    except Exception as _e:
        _r_post = {"__raised__": repr(_e)}
    check("E2e 信号采集失败 ⇒ POST 冻结同样显式报错（不冻来路不明的快照）",
          isinstance(_r_post, dict) and _r_post.get("ok") is False
          and "reason" in _r_post and "__mev_boom__" in str(_r_post.get("reason")),
          _r_post)
    _n_boom = int(_conn.execute(
        "SELECT COUNT(*) FROM misevolution_baselines WHERE epoch='e2d-boom'"
    ).fetchone()[0])
    check("E2f 采集失败零写入（不落半条基线）", _n_boom == 0, _n_boom)
finally:
    me.collect_signals = _orig_collect
check("E2g 猴子补丁已复位（后续断言不受污染）",
      me.collect_signals is _orig_collect)
_ale_src = _read(os.path.join("core", "alert_evaluator.py"))
check("E3a 告警器消费 misevolution 引擎",
      "from core import misevolution" in _ale_src)
check("E3b evaluate_once None 跳过（没数据不判，skipped_unknown）",
      "skipped_unknown" in _ale_src and "if actual is None:" in _ale_src)
_repo_src = _read(os.path.join("repositories", "misevolution_repo.py"))
_repo_tables = set(re.findall(r"(?:FROM|INTO|UPDATE)\s+([a-z_]+)", _repo_src))
check("E4 repo 只碰 misevolution 两表（监控零生产副作用可断言）",
      _repo_tables <= {"misevolution_baselines", "misevolution_checks"},
      sorted(_repo_tables))
check("E4b 两个写方法（freeze/insert_check）各带一次 commit",
      _repo_src.count("self.conn.commit()") == 2
      and "def freeze_baseline" in _repo_src and "def insert_check" in _repo_src,
      _repo_src.count("self.conn.commit()"))
_tools_src = _read(os.path.join("agent", "pipeline_parts", "tools.py"))
check("E5 白名单拒绝审计写入点在位（tool_whitelist_denied）",
      _tools_src.count("tool_whitelist_denied") >= 1)
_mig_src = _read(os.path.join("database", "migrations", "misc.py"))
_mig_init = _read(os.path.join("database", "migrations", "__init__.py"))
_sch_src = _read(os.path.join("database", "schema.py"))
check("E6a 迁移函数在 misc.py", "def _migrate_misevolution_p21" in _mig_src)
check("E6b migrations/__init__ import + __all__ 双登记",
      "_migrate_misevolution_p21" in _mig_init
      and _mig_init.count("_migrate_misevolution_p21") >= 2)
check("E6c schema.py import + 调用点双登记",
      _sch_src.count("_migrate_misevolution_p21") >= 2)
_run_metrics = {"success_rate", "avg_latency", "error_count", "mock_rate"}
check("E7 misev_* 与 run 级四指标零交集（两套评估器不重复告警）",
      not ({("misev_" + n) for n in me.SPEC_NAMES} & _run_metrics))
_cfg_src = _read(os.path.join("core", "config.py"))
check("E8 misevolution 组注释含红线依据（可追溯）",
      "misevolution" in _cfg_src and "红线" in _cfg_src)

# ═══════════════════════════════════════════════════════════════
# 收尾：关连接、清临时目录
# ═══════════════════════════════════════════════════════════════
try:
    _db_cm.__exit__(None, None, None)
except Exception:
    pass

_np = sum(1 for _, ok, _ in _results if ok)
_nf = sum(1 for _, ok, _ in _results if ok is False)
_ns = sum(1 for _, ok, _ in _results if ok is None)
print("\n" + "=" * 64)
print("结果: %d PASS / %d FAIL / %d SKIP / 共 %d"
      % (_np, _nf, _ns, len(_results)))
if _nf:
    print("未过判据：")
    for nm, ok, d in _results:
        if ok is False:
            print("  - %s  ← %s" % (nm, d))
    sys.exit(1)
print("全部通过 —— misevolution 监控（五信号诚实口径 + 基线比较 + 晋升复测 + "
      "告警常驻 + 拒绝留痕）。")
sys.exit(0)
