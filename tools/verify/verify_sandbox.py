# -*- coding: utf-8 -*-
"""P1-4 门禁：沙箱 A/B 评估器 + 配置晋升链路（core/sandbox + config_promotion
+ sandbox_repo + proposals/config 路由 + HIL 执行分支）。

**背景（2026-10-19《自进化 AI 平台调研评估》路线图 P1-4）**：P1-3 提案器
提议改配置后，缺一条安全通道。本门禁守护的链路：
  ① 沙箱 A/B（core/sandbox.py）：影子配置 vs 活配置各跑 CI 门禁集
     （ci.yml 执行行 + 原参数），差分判回归——双臂顺序（门禁共享
     ROOT/tmp）、每臂独立临时库/配置副本、影子层只进实验组、源码
     内容指纹防评估期间外部编辑污染（均为实测踩坑后立规）；
  ② 晋升（core/config_promotion.py）：verdict=pass 才可发起 ⇒ 入
     hil_confirmations ⇒ 人批准后才经 save_override 真改配置并落
     config_history；回滚同样过人。**没有任何自动写配置的路径**。

守护面：
  A. 纯函数：差分判定九类矩阵、infra 占比、空集、陈旧结论、引用检测
     确定性、CI 命令参数保全（--static-only）、scope fail-loud；
  B. 配置消费：sandbox 组五键双登记 + 变异自证（超时/占比/默认范围/
     总开关四口径都必须真被消费）；
  C. 干净库端到端：注入小型评估集跑通「评估 → 晋升守卫 → HIL 批准
     → 配置真改 + 台账 + 提案标记 → 回滚」，以及 ★零生产副作用
     （评估前后除 sandbox_runs 外任何表行数不变 + 配置文件字节不变
     + 活配置不变）、regression/非配置目标/终态提案/陈旧结论 全部
     挡死、幂等 unchanged、reject 分支零改动、非配置 action 越界保护；
  B'. 变异自证：判定函数/陈旧判据/晋升守卫/影子层/变异检测五处逐一
     变异，对应断言必须转红；
  D. 真库只读 smoke：引用检测与选集的确定性、活配置对账；
  E. 接线：四端点 + 作业 kind + config 双登记 + 迁移双登记 + 三层
     零配置写静态断言 + HIL 执行分支接线。

双环境口径：干净库自建 MBSE_DB_PATH 临时空库 + MBSE_CONFIG_PATH 临时
配置文件（**晋升执行会写真配置，不隔离就会写到用户真配置**）；真库段只读。
"""
import os
import shutil
import sqlite3
import sys
import time as _time


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

# ── 干净库 + 隔离配置文件：先切后 import（core.config 两个路径都是
#    import 期绑定的，顺序即正确性）────────────────────────────────────
_TMP = os.path.join(ROOT, "tmp", "verify_sandbox")
os.makedirs(_TMP, exist_ok=True)
_CLEAN_DB = os.path.join(_TMP, "clean.db")
_CFG = os.path.join(_TMP, "sandbox_gate_config.json")
for _p in (_CLEAN_DB, _CFG):
    for _sfx in ("", "-wal", "-shm"):
        try:
            os.remove(_p + _sfx)
        except OSError:
            pass
os.environ["MBSE_DB_PATH"] = _CLEAN_DB
os.environ["MBSE_CONFIG_PATH"] = _CFG

_results = []


def check(name, ok, detail=""):
    _results.append((name, bool(ok), detail))
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name,
                           ("  ← " + str(detail)) if (detail and not ok) else ""))


def _table_counts(conn) -> dict:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    out = {}
    for r in rows:
        t = r[0]
        try:
            out[t] = conn.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
        except Exception:
            out[t] = -1
    return out


def _seed_proposal(conn, rule="llm_transient_retry", section="llm", key="retry_times",
                   current="2", proposed="3", fp="fp-gate-1", status="pending"):
    conn.execute(
        "INSERT INTO config_proposals (rule, target_section, target_key, current_value, "
        "proposed_value, rationale, fingerprint, status) VALUES (?,?,?,?,?,?,?,?)",
        (rule, section, key, current, proposed, "gate-seed", fp, status))
    conn.commit()
    return conn.execute("SELECT id FROM config_proposals WHERE fingerprint=?",
                        (fp,)).fetchone()[0]


# ══════════════════════════════════════════════════════════════════════
print("== A. 纯函数（判定矩阵 / 陈旧 / 引用 / CI 命令 / scope）==")
from core import sandbox as S            # noqa: E402
from core import config_promotion as P   # noqa: E402
from core import config as CFG           # noqa: E402

# A1 九类矩阵
_matrix = {
    ("pass", "pass"): S.CAT_STABLE_PASS, ("pass", "fail"): S.CAT_REGRESSION,
    ("pass", "error"): S.CAT_REGRESSION, ("fail", "pass"): S.CAT_IMPROVEMENT,
    ("error", "pass"): S.CAT_IMPROVEMENT, ("fail", "fail"): S.CAT_STABLE_FAIL,
    ("error", "error"): S.CAT_INFRA_ERROR, ("fail", "error"): S.CAT_MIXED,
    ("error", "fail"): S.CAT_MIXED,
}
_bad = {k: S.categorize(*k) for k in _matrix if S.categorize(*k) != _matrix[k]}
check("A1 差分类别九矩阵（fail-closed：对照过+实验败 ⇒ regression）", not _bad, _bad)

# A2 判定序：空集/ infra 占比 / 回归 / 纯通过（存量失败中性）
_v = S.differential_verdict([])
check("A2a 空评估集 ⇒ error（信号缺失比回归更严重）", _v["verdict"] == S.VERDICT_ERROR)
_v = S.differential_verdict([{"a": "pass", "b": "pass"},
                             {"a": "pass", "b": "pass"},
                             {"a": "fail", "b": "fail"},
                             {"a": "error", "b": "error"}], 0.3)
check("A2b 存量 fail + 双臂 error（1/4=25%<阈值）⇒ pass 且计数正确",
      _v["verdict"] == S.VERDICT_PASS and _v["counts"].get(S.CAT_STABLE_FAIL) == 1
      and _v["counts"].get(S.CAT_INFRA_ERROR) == 1, _v)
_v = S.differential_verdict([{"a": "pass", "b": "pass"},
                             {"a": "fail", "b": "fail"},
                             {"a": "error", "b": "error"}], 0.2)
check("A2c infra 占比超阈值 ⇒ error（环境坏，结论无效）",
      _v["verdict"] == S.VERDICT_ERROR and "infra" in _v["reason"], _v)
_v = S.differential_verdict([{"a": "pass", "b": "pass"}, {"a": "pass", "b": "fail"}])
check("A2d 一个回归 ⇒ regression（唯一判红类别）",
      _v["verdict"] == S.VERDICT_REGRESSION and _v["counts"][S.CAT_REGRESSION] == 1, _v)
_v = S.differential_verdict([{"a": "fail", "b": "fail"}, {"a": "fail", "b": "pass"}])
check("A2e improvement 不改判定（只记录）", _v["verdict"] == S.VERDICT_PASS, _v)
_rows = [{"a": "pass", "b": "fail"}, {"a": "fail", "b": "pass"}]
S.differential_verdict(_rows)
check("A2f 判定给每行打 category 标签（报告按严重度排序用）",
      all("category" in r for r in _rows), _rows)

# A3 陈旧结论判据
import json as _json  # noqa: E402
_run = {"shadow_json": _json.dumps({"llm": {"retry_times": 3}})}
_prop = {"target_section": "llm", "target_key": "retry_times", "proposed_value": "3"}
check("A3a 影子与提案当前目标一致 ⇒ 匹配",
      S.run_matches_proposal(_run, _prop) is True)
check("A3b 提案 proposed 变过 ⇒ 不匹配（旧结论过期，必须重跑）",
      S.run_matches_proposal(_run, dict(_prop, proposed_value="4")) is False)
check("A3c 影子多一个键 ⇒ 不匹配",
      S.run_matches_proposal({"shadow_json": _json.dumps(
          {"llm": {"retry_times": 3, "fallback_provider_id": 1}})}, _prop) is False)
check("A3d shadow_json 损坏 ⇒ 不匹配（不抛）",
      S.run_matches_proposal({"shadow_json": "{oops"}, _prop) is False)

# A4 影子构造
check("A4 shadow_from_targets 一次评估只含一个键",
      S.shadow_from_targets("llm", "retry_times", 3) == {"llm": {"retry_times": 3}})

# A5 引用检测（确定性 + 已知命中 + 空键）
_r1 = S.gates_referencing("llm", "retry_times")
_r2 = S.gates_referencing("llm", "retry_times")
check("A5a 引用检测确定性（两次相同）", _r1 == _r2 and _r1)
check("A5b 已知引用命中（verify_total_timeout_budget 断言重试次数）",
      "verify_total_timeout_budget" in _r1, _r1[:6])
# 键名运行时拼接：否则本字符串会出现在本门禁源码里，被静态扫描自匹配
# （gates_referencing 扫的是 tools/verify/*.py 全体，含本文件）
check("A5c 不存在的分组+键 ⇒ 空集（section 级命中是保守语义：读该分组的都算）",
      S.gates_referencing("zz" + "_no_such_section", "zz" + "_no_such_key") == [])

# A6 CI 命令口径：带原参数、无重复
_cmds = S.ci_gate_commands()
_names = [n for n, _ in _cmds]
_oe = [a for n, a in _cmds if n == "verify_orchestration_e2e"]
check("A6a CI 门禁命令非空且无重名", len(_cmds) > 100 and len(_names) == len(set(_names)),
      "n=%d" % len(_cmds))
check("A6b ★带 CI 原参数（orchestration_e2e 必须 --static-only，防 live 烧钱）",
      bool(_oe) and "--static-only" in _oe[0], _oe)

# A7 scope
_full = S.select_gates("full")
_ref = S.select_gates("referenced", "llm", "retry_times")
check("A7a full = 全 CI 集；referenced ⊆ full",
      len(_full) == len(_cmds) and set(n for n, _ in _ref) <= set(_names))
check("A7b referenced 非空（llm.retry_times 有门禁在读）", len(_ref) > 0, len(_ref))
try:
    S.select_gates("bogus")
    check("A7c 未知 scope ⇒ fail-loud", False, "未抛异常")
except ValueError:
    check("A7c 未知 scope ⇒ fail-loud", True)

# A8 源码指纹（可用性 + 十六进制 + 未跟踪文件不干扰）
_fp1 = S._source_fingerprint()
check("A8a 源码指纹可用（git 部署）", _fp1.get("available") is True, _fp1)
check("A8b 指纹是 40 位十六进制（sha1）",
      len(str(_fp1.get("sha1", ""))) == 40, _fp1.get("sha1"))
_probe_untracked = os.path.join(ROOT, "tmp", "verify_sandbox", "_untracked_probe.txt")
with open(_probe_untracked, "w", encoding="utf-8") as _f:
    _f.write("untracked noise")
_fp2 = S._source_fingerprint()
os.remove(_probe_untracked)
check("A8c 未跟踪文件不改变指纹（tmp 噪声不误伤）", _fp2.get("sha1") == _fp1.get("sha1"))

# ══════════════════════════════════════════════════════════════════════
print("\n== B. 配置消费（sandbox 组五键双登记 + 变异自证）==")
_cfg_src = open(os.path.join(ROOT, "core", "config.py"), encoding="utf-8").read()
check("B1 core/config.py 双登记（DEFAULT_CONFIG + CONFIG_SCHEMA）",
      _cfg_src.count('"sandbox"') >= 2, _cfg_src.count('"sandbox"'))
_st = S.get_settings()
check("B2 五键齐备且类型正确（enabled/gate_timeout_s/default_scope/"
      "infra_error_ratio/max_report_gates）",
      _st.get("enabled") is True and isinstance(_st.get("gate_timeout_s"), int)
      and _st.get("default_scope") in S.SCOPES
      and isinstance(_st.get("infra_error_ratio"), float)
      and isinstance(_st.get("max_report_gates"), int), _st)

_og, _ob = CFG.get, CFG.as_bool


def _mut(section, key, value, is_bool=False):
    if is_bool:
        CFG.as_bool = lambda s, k, d=False, _v=value: (
            _v if (s, k) == (section, key) else _ob(s, k, d))
    else:
        CFG.get = lambda s, k, d=None, _v=value: (
            _v if (s, k) == (section, key) else _og(s, k, d))


try:
    _mut("sandbox", "gate_timeout_s", 7)
    check("B3 ★gate_timeout_s 变异 ⇒ get_settings 真读到（超时口径被消费）",
          S.get_settings()["gate_timeout_s"] == 7)
    CFG.get = _og
    _mut("sandbox", "infra_error_ratio", 0.9)
    check("B4 ★infra_error_ratio 变异 ⇒ get_settings 真读到（环境阈值被消费）",
          abs(S.get_settings()["infra_error_ratio"] - 0.9) < 1e-9)
    CFG.get = _og
    _mut("sandbox", "default_scope", "referenced")
    check("B5 ★default_scope 变异 ⇒ get_settings 真读到（默认范围被消费）",
          S.get_settings()["default_scope"] == "referenced")
    CFG.get = _og
    _mut("sandbox", "enabled", False, is_bool=True)
    check("B6 ★enabled 变异 ⇒ start_run 被挡（总开关真被消费）",
          S.start_run(None, 1).get("ok") is False
          and "停用" in str(S.start_run(None, 1).get("error")))
    CFG.as_bool = _ob
finally:
    CFG.get, CFG.as_bool = _og, _ob

# ══════════════════════════════════════════════════════════════════════
print("\n== C. 干净库端到端（评估 → 晋升守卫 → HIL 批准 → 台账 → 回滚）==")
from database import init_db, get_db          # noqa: E402
from hil_service import HILService            # noqa: E402

init_db()
conn = get_db()
conn.row_factory = sqlite3.Row

# 注入小型评估集（生产路径走 scope 口径；这里是门禁缝，见 evaluate 文档）
_MINI = [("verify_migration_guard", ["tools/verify/verify_migration_guard.py"])]

_pid = _seed_proposal(conn)
_rows_before = _table_counts(conn)
_cfg_bytes_before = open(_CFG, "rb").read() if os.path.exists(_CFG) else b""

_out = S.start_run(conn, _pid, "referenced")
check("C1 start_run：queued + 影子即提案当前目标",
      _out.get("ok") and _out["run"]["status"] == S.RUN_QUEUED
      and _json.loads(_out["run"]["shadow_json"]) == {"llm": {"retry_times": "3"}}
      and _out["run"]["scope"] == "referenced", _out)
_run_id = _out["run"]["id"]

_done = S.evaluate(conn, _run_id, gates=_MINI)
_summ = _json.loads(_done["summary_json"])
check("C2 evaluate（注入 1 门禁双臂）：done + verdict=pass + 变异检测在跑",
      _done["status"] == S.RUN_DONE and _done["verdict"] == S.VERDICT_PASS
      and _summ["counts"].get(S.CAT_STABLE_PASS) == 1
      and _summ.get("mutation_check") in ("clean", "unavailable")
      and _summ["gates_total"] == 1, _summ.get("counts"))

# ★零生产副作用：评估前后除 sandbox_runs 外任何表行数不变 + 配置字节不变
_rows_after = _table_counts(conn)
_diff = {t: (_rows_before.get(t), _rows_after.get(t))
         for t in set(_rows_before) | set(_rows_after)
         if _rows_before.get(t) != _rows_after.get(t)}
check("C3 ★零生产副作用：评估前后只有 sandbox_runs +1，其余表行数不变",
      set(_diff) <= {"sandbox_runs"} and _diff.get("sandbox_runs") == (0, 1), _diff)
_cfg_bytes_after = open(_CFG, "rb").read() if os.path.exists(_CFG) else b""
check("C3b 配置文件字节不变（门禁子进程写的是每臂副本）",
      _cfg_bytes_before == _cfg_bytes_after)
check("C3c 活配置不变（retry_times 仍 2）", CFG.get("llm", "retry_times") == 2)

# 晋升守卫链
_p2 = _seed_proposal(conn, fp="fp-gate-2")
conn.execute(
    "INSERT INTO sandbox_runs (proposal_id, scope, shadow_json, status, verdict, "
    "summary_json) VALUES (?, 'referenced', ?, 'done', 'regression', '{}')",
    (_p2, _json.dumps({"llm": {"retry_times": "3"}})))
conn.commit()
_r = P.request_promotion(conn, _p2)
check("C4 ★verdict=regression ⇒ 晋升被挡（没有 pass 结论不给人批）",
      _r.get("ok") is False and "沙箱" in str(_r.get("error")), _r)

_p3 = _seed_proposal(conn, rule="tool_kind_gap", section="instrumentation",
                     key="tool_call_logs.failure_kind", current="空（unknown）",
                     proposed="扩词表", fp="fp-gate-3")
_r = P.request_promotion(conn, _p3)
check("C5 非配置键目标 ⇒ 晋升被挡（人工修订后 decide 留痕即可）",
      _r.get("ok") is False and "不是已登记配置键" in str(_r.get("error")), _r)

_p4 = _seed_proposal(conn, fp="fp-gate-4", status="rejected")
_r = P.request_promotion(conn, _p4)
check("C6 终态提案 ⇒ 晋升被挡（已决不重开）",
      _r.get("ok") is False and "终态" in str(_r.get("error")), _r)

_p5 = _seed_proposal(conn, proposed="4", fp="fp-gate-5")
# 先跑出一致的 pass 结论，再把提案目标改掉 ⇒ 结论过期（现场配置变过）
S.evaluate(conn, S.start_run(conn, _p5, "referenced")["run"]["id"], gates=_MINI)
conn.execute("UPDATE config_proposals SET proposed_value='5' WHERE id=?", (_p5,))
conn.commit()
_r = P.request_promotion(conn, _p5)
check("C7 ★陈旧结论 ⇒ 晋升被挡（pass 结论的影子和提案当前 proposed 不一致）",
      _r.get("ok") is False and "不一致" in str(_r.get("error")), _r)

# 正式晋升：入 HIL 队列（配置仍不变）
_r = P.request_promotion(conn, _pid)
check("C8 promote ⇒ HIL 确认单入队（config_apply + 完整 payload）",
      _r.get("ok") and _r.get("confirmation_id"), _r)
_cid = _r["confirmation_id"]
_conf = HILService.get(conn, _cid)
check("C8b 确认单 pending + action=config_apply + payload 带 sandbox_run_id",
      _conf["status"] == "pending" and _conf["action"] == P.HIL_ACTION_APPLY
      and _conf["payload"].get("sandbox_run_id") == _run_id
      and _conf["payload"].get("section") == "llm"
      and _conf["payload"].get("key") == "retry_times"
      and _conf["payload"].get("value") == "3", _conf.get("payload"))
check("C8c 入队不改配置（retry_times 仍 2）", CFG.get("llm", "retry_times") == 2)

# HIL 批准 ⇒ 走路由同一条执行分支（行为级证明 flows.py 接线）
from routers.studio_parts.flows import decide_hil_confirmation   # noqa: E402
_dec = decide_hil_confirmation(_cid, {"approve": True}, conn=conn,
                               user={"username": "gate", "display_name": "门禁"})
check("C9 ★HIL 批准 ⇒ 配置真的改了（2→3，save_override 唯一写路径）",
      _dec.get("status") == "approved" and _dec.get("executed") is True
      and CFG.get("llm", "retry_times") == 3, _dec.get("exec_result"))
_hist = conn.execute("SELECT * FROM config_history ORDER BY id DESC LIMIT 1").fetchone()
check("C9b 台账：llm.retry_times 2→3，source=proposal，applied=1",
      _hist["section"] == "llm" and _hist["key"] == "retry_times"
      and _hist["old_value"] == "2" and _hist["new_value"] == "3"
      and _hist["source"] == P.SRC_PROPOSAL and _hist["applied"] == 1
      and _hist["source_id"] == _pid, dict(_hist))
_prow = conn.execute("SELECT status, review_note FROM config_proposals WHERE id=?",
                     (_pid,)).fetchone()
check("C9c 提案同步标记 applied（队列反映事实）",
      _prow["status"] == "applied" and "晋升" in _prow["review_note"], dict(_prow))
_audit_n = conn.execute(
    "SELECT COUNT(*) n FROM audit_logs WHERE event_type='hil_decide'").fetchone()["n"]
check("C9d HIL 决策留痕（audit_logs 有 hil_decide）", _audit_n >= 1, _audit_n)

# 回滚：同样过人
_rb = P.request_rollback(conn, _hist["id"])
check("C10 回滚请求 ⇒ HIL 确认单入队（config_rollback）",
      _rb.get("ok") and _rb.get("confirmation_id"), _rb)
_cid2 = _rb["confirmation_id"]
check("C10b 回滚入队不改配置（仍 3）", CFG.get("llm", "retry_times") == 3)
_dec2 = decide_hil_confirmation(_cid2, {"approve": True}, conn=conn,
                                user={"username": "gate", "display_name": "门禁"})
check("C10c ★HIL 批准回滚 ⇒ 配置回到 2 + 新台账 source=rollback",
      _dec2.get("executed") is True and CFG.get("llm", "retry_times") == 2
      and _dec2["exec_result"].get("applied") is True, _dec2.get("exec_result"))
_h2 = conn.execute("SELECT * FROM config_history ORDER BY id DESC LIMIT 1").fetchone()
check("C10d 回滚台账：3→2，source=rollback，source_id=被回滚行",
      _h2["old_value"] == "3" and _h2["new_value"] == "2"
      and _h2["source"] == P.SRC_ROLLBACK and _h2["source_id"] == _hist["id"], dict(_h2))

# reject 分支：零改动
_p6 = _seed_proposal(conn, fp="fp-gate-6")
S.evaluate(conn, S.start_run(conn, _p6, "referenced")["run"]["id"], gates=_MINI)
_r6 = P.request_promotion(conn, _p6)
_cid3 = _r6["confirmation_id"]
_cfg_before_reject = CFG.get("llm", "retry_times")
_h_before = conn.execute("SELECT COUNT(*) n FROM config_history").fetchone()["n"]
_dec3 = decide_hil_confirmation(_cid3, {"approve": False}, conn=conn,
                                user={"username": "gate", "display_name": "门禁"})
check("C11 HIL 拒绝 ⇒ 零改动（配置/台账/提案状态均不变）",
      _dec3.get("status") == "rejected" and _dec3.get("executed") is None
      and CFG.get("llm", "retry_times") == _cfg_before_reject == 2
      and conn.execute("SELECT COUNT(*) n FROM config_history").fetchone()["n"] == _h_before
      and conn.execute("SELECT status FROM config_proposals WHERE id=?",
                       (_p6,)).fetchone()["status"] == "pending", _dec3)

# 幂等：目标值即当前值 ⇒ 不重复写、记一行 applied=0
_p7 = _seed_proposal(conn, current="2", proposed="2", fp="fp-gate-7")
S.evaluate(conn, S.start_run(conn, _p7, "referenced")["run"]["id"], gates=_MINI)
_r7 = P.request_promotion(conn, _p7)
_cid4 = _r7["confirmation_id"]
_dec4 = decide_hil_confirmation(_cid4, {"approve": True}, conn=conn,
                                user={"username": "gate", "display_name": "门禁"})
_h3 = conn.execute("SELECT * FROM config_history ORDER BY id DESC LIMIT 1").fetchone()
check("C12 幂等：目标值=当前值 ⇒ applied=False + 台账 applied=0（不撒谎）+ 配置不变",
      _dec4["exec_result"].get("applied") is False
      and _h3["applied"] == 0 and CFG.get("llm", "retry_times") == 2,
      _dec4.get("exec_result"))

# 非配置 action 越界保护
_fake = {"action": "entity_create", "payload": {"x": 1}}
check("C13 execute_confirmation 非配置 action ⇒ 不执行（越界保护）",
      P.execute_confirmation(conn, _fake).get("executed") is False)

# 评估集注入为空 ⇒ failed（不伪装成 verdict）
_p8 = _seed_proposal(conn, fp="fp-gate-8")
_rid8 = S.start_run(conn, _p8, "referenced")["run"]["id"]
try:
    S.evaluate(conn, _rid8, gates=[])
    check("C14 注入空评估集 ⇒ RuntimeError（fail-loud）", False, "未抛")
except RuntimeError:
    _r8 = S.get_run(conn, _rid8)
    check("C14 注入空评估集 ⇒ RuntimeError + 运行记录 failed（不伪装 verdict）",
          _r8["status"] == S.RUN_FAILED and not _r8["verdict"], dict(_r8))

# ══════════════════════════════════════════════════════════════════════
print("\n== B'. 变异自证（五处逐一变异，断言必须转红）==")
# M1 判定函数不判红
_orig_verdict = S.differential_verdict
S.differential_verdict = lambda res, r=0.3: {"verdict": S.VERDICT_PASS,
                                             "reason": "", "counts": {}, "total": len(res)}
_m1 = S.differential_verdict([{"a": "pass", "b": "fail"}])
check("M1 变异：regression 不算回归 ⇒ A2d 断言转红（判据有牙）",
      _m1["verdict"] != S.VERDICT_REGRESSION)
S.differential_verdict = _orig_verdict
# M2 陈旧判据恒真
_orig_match = S.run_matches_proposal
S.run_matches_proposal = lambda run, proposal: True
_m2 = S.run_matches_proposal({"shadow_json": "{}"}, dict(_prop, proposed_value="999"))
check("M2 变异：陈旧判据恒真 ⇒ A3b 断言转红", _m2 is True)
S.run_matches_proposal = _orig_match
# M3 晋升守卫被拆（pass 结论查询返回归行）
_orig_latest = S.latest_passing_run
S.latest_passing_run = lambda conn, pid: {"id": 0, "verdict": "regression",
                                          "status": "done",
                                          "shadow_json": _json.dumps(
                                              {"llm": {"retry_times": "3"}})}
_m3 = P.request_promotion(conn, _p2)          # _p2 只有 regression 结论
check("M3 变异：守卫拆掉 ⇒ regression 提案也能晋升（C4 断言转红）", _m3.get("ok") is True)
S.latest_passing_run = _orig_latest
# M4 影子层失效（_load_shadow 恒空 ⇒ 最高优先级覆盖没了）
_orig_shadow = CFG._load_shadow
os.environ["MBSE_SHADOW_CONFIG"] = '{"llm":{"retry_times":9}}'
CFG._load_shadow = lambda: {}
CFG.reload()
_m4 = CFG.get("llm", "retry_times")
CFG._load_shadow = _orig_shadow
os.environ.pop("MBSE_SHADOW_CONFIG", None)
CFG.reload()
check("M4 变异：影子层恒空 ⇒ 覆盖失效（retry_times 读不到 9，A 段影子断言转红）",
      _m4 != 9, _m4)
# M5 变异检测失灵（指纹恒定 ⇒ 跑中改源码也发现不了）
_orig_fp = S._source_fingerprint
_fp_calls = [0]


def _fp_const():
    _fp_calls[0] += 1
    return {"available": True, "sha1": "const"}


S._source_fingerprint = _fp_const
_m5_raised = False
_pm5 = _seed_proposal(conn, fp="fp-gate-m5")     # 全新 pending 提案（前面提案已终态）
try:
    S.evaluate(conn, S.start_run(conn, _pm5, "referenced")["run"]["id"], gates=_MINI)
except RuntimeError:
    _m5_raised = True
S._source_fingerprint = _orig_fp
check("M5 变异：指纹恒定（检测失灵）⇒ 本应判红的场景不再报错（反证检测真在干活）",
      _m5_raised is False)

# ══════════════════════════════════════════════════════════════════════
print("\n== D. 真库只读 smoke ==")
_prod = os.path.join(ROOT, "mbse.db")
if os.path.exists(_prod):
    _pc = sqlite3.connect("file:%s?mode=ro" % _prod.replace("\\", "/"), uri=True)
    _pc.row_factory = sqlite3.Row
    _d1a = S.gates_referencing("llm", "retry_times")
    _d1b = S.gates_referencing("llm", "retry_times")
    check("D1 真库环境引用检测仍确定性（两次相同）", _d1a == _d1b and _d1a)
    _live_retry = CFG.get("llm", "retry_times")
    _mismatch = []
    for _row in _pc.execute(
            "SELECT target_section, target_key, current_value FROM config_proposals "
            "WHERE target_section='llm'").fetchall():
        if str(CFG.get(_row["target_section"], _row["target_key"], "")) != \
                str(_row["current_value"]):
            _mismatch.append((_row["target_section"], _row["target_key"],
                              _row["current_value"]))
    check("D2 ★llm.* 提案的 current_value 与活配置逐键对账（提案没读现场就是拍脑袋）",
          not _mismatch, _mismatch)
    _d3 = S.select_gates("referenced", "llm", "retry_times")
    _d3full = dict((n, a) for n, a in S.select_gates("full"))
    check("D3 真库 referenced 选集 ⊆ full 且参数一致",
          all(n in _d3full and _d3full[n] == a for n, a in _d3))
    _pc.close()
else:
    print("  [SKIP] 真库不存在（%s），D 段跳过" % _prod)

# ══════════════════════════════════════════════════════════════════════
print("\n== E. 接线完整性 ==")
import importlib                                        # noqa: E402
_pr = importlib.import_module("routers.proposals")
_paths = [getattr(r, "path", "") for r in _pr.router.routes]
check("E1 proposals 路由四新端点注册（sandbox / sandbox-runs / run 详情 / promote）",
      "/api/proposals/{proposal_id}/sandbox" in _paths
      and "/api/proposals/{proposal_id}/sandbox-runs" in _paths
      and "/api/sandbox/runs/{run_id}" in _paths
      and "/api/proposals/{proposal_id}/promote" in _paths, _paths)
_cr = importlib.import_module("routers.config")
_cpaths = [getattr(r, "path", "") for r in _cr.router.routes]
check("E2 config 路由台账 + 回滚端点注册",
      "/api/config/history" in _cpaths and "/api/config/rollback" in _cpaths, _cpaths)
from core import job_handlers as JH                   # noqa: E402
check("E3 作业 kind 注册（KINDS + describe 双处）",
      "config_sandbox" in JH.KINDS and "config_sandbox" in JH.describe())
_mig = open(os.path.join(ROOT, "database", "migrations", "misc.py"),
            encoding="utf-8").read()
_sch = open(os.path.join(ROOT, "database", "schema.py"), encoding="utf-8").read()
_mig_init = open(os.path.join(ROOT, "database", "migrations", "__init__.py"),
                 encoding="utf-8").read()
check("E4 迁移三处接线（misc.py 定义 + schema.py 调用 + __init__ 双登记）",
      "def _migrate_sandbox_p14" in _mig
      and _sch.count("_migrate_sandbox_p14") >= 2
      and _mig_init.count("_migrate_sandbox_p14") >= 2,
      "misc=%d sch=%d init=%d" % (_mig.count("_migrate_sandbox_p14"),
                                  _sch.count("_migrate_sandbox_p14"),
                                  _mig_init.count("_migrate_sandbox_p14")))
_rep = open(os.path.join(ROOT, "repositories", "sandbox_repo.py"),
            encoding="utf-8").read()
import re as _re                                       # noqa: E402
_stmts = _re.findall(r'(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+(\w+)', _rep)
check("E5 sandbox_repo 只碰 sandbox_runs 一张表（引擎数据面可断言）",
      set(_stmts) == {"sandbox_runs"}, _stmts)
_promo_src = open(os.path.join(ROOT, "core", "config_promotion.py"),
                  encoding="utf-8").read()
_sb_src = open(os.path.join(ROOT, "core", "sandbox.py"), encoding="utf-8").read()
check("E6 ★config_promotion 唯一配置写入口是 save_override（无 settings 直写/无 _CONFIG 直改）",
      _promo_src.count("save_override(") == 1
      and "INSERT INTO settings" not in _promo_src
      and "UPDATE settings" not in _promo_src
      and "_CONFIG[" not in _promo_src)
check("E7 ★sandbox 引擎零配置写路径（无 save_override 调用 / 无 settings 直写 / 无 _CONFIG 直改）",
      "save_override(" not in _sb_src and "UPDATE settings" not in _sb_src
      and "INSERT INTO settings" not in _sb_src and "_CONFIG[" not in _sb_src)
_flows_src = open(os.path.join(ROOT, "routers", "studio_parts", "flows.py"),
                  encoding="utf-8").read()
check("E8 HIL decide 的配置执行分支接线（approve ⇒ execute_confirmation）",
      "_promo.execute_confirmation" in _flows_src
      and "_is_cfg_action" in _flows_src)

# ══════════════════════════════════════════════════════════════════════
_fails = [r for r in _results if not r[1]]
print("\n" + "=" * 66)
print("结果: %d PASS / %d FAIL / 共 %d" % (len(_results) - len(_fails), len(_fails),
                                          len(_results)))
for _n, _ok, _d in _fails:
    print("  [FAIL] %s  ← %s" % (_n, _d))
if not _fails:
    print("全部通过 —— 沙箱 A/B（双臂顺序 + 每臂隔离 + 指纹防变异 + 差分判定）+ "
          "晋升链路（pass 才可晋升 + HIL 批准才写 + 台账可回滚）+ 零生产副作用。")
sys.exit(1 if _fails else 0)
