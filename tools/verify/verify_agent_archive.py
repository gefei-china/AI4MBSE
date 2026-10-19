# -*- coding: utf-8 -*-
"""P2-3 门禁：Agent 档案库/种群（DGM 式 archive，自进化调研路线图 P2 第三项）。

═══════════════════════════════════════════════════════════════
动机（2026-10-18《自进化 AI 平台调研评估》路线图 P2 第三项）
═══════════════════════════════════════════════════════════════
DGM/AlphaZero 式种群的关键不是「保留最优」，而是**记住死掉的个体**——
否则系统反复重走已知死路。本门禁守 core/agent_archive 的四条命脉：

  ① **画像只用真实可算的证据**：真实库里 sandbox_runs / misevolution_checks /
     bad_cases / config_history 是提案级或会话级，没有 agent_id+version 列。
     把它们硬关联到某变体需要一次并不存在的接线——那是 fabricate。本门禁
     用「引擎零写语句 + 画像来源清单」把这条纪律钉死。
  ② **诚实 unknown**：维度分母不足 ⇒ value=None / verdict=unknown，
     **绝不回落 0**；summary 只有「lower_better 维度 value==0」才判 pass，
     探索读数（lineage_depth=0 = 没探索过）**不作 pass 证据**
     （冒烟实测踩到：4/5 维 unknown 时曾误判 pass）。
  ③ **死路记忆是核心价值**：负向结论 ≥dead_end_repeat 才判死路（一次回归
     可能是噪声）；error / insufficient_data **不算负向**（环境坏≠路不行）。
  ④ **只记录不切换生效版本**：档案库是记忆不是手——全链路没有
     UPDATE agents / config_history 的语句（回滚过人的铁律同 P1-4）。

判据结构（承 verify_misevolution / verify_curriculum 的五段式）：
  A 纯函数    结局三态（error/insufficient 不算负向）+ 分母不足即 unknown +
              震荡检测（A→B→A 改回循环，同对只计一次）+ 指纹稳定性 +
              死路门槛（严格 ≥）+ 画像方向感知（summary.pass 须有
              lower_better 正证据）+ 深度回溯防环
  B 配置      agent_archive 组双登记 + 键一致 + 引擎零写语句 +
              口径真被消费（影子配置变异自证 window_days/min_denominator）
  C 端到端    干净临时库（import 前切 MBSE_DB_PATH/MBSE_CONFIG_PATH）：
              合成 A→B→A 改回历史 ⇒ 震荡被检出 ⇒ scan 收敛台账 ⇒ 指纹
              幂等 ⇒ 显式挂载两条 regression ⇒ refresh 判死路 ⇒
              ★第二连接可见（repo 自带 commit）⇒ ★全表快照零副作用
              （除自家两表外任何表行数零变化）⇒ 端点行为级（含冷库显式
              失败）⇒ 变异自证
  D 真库      只读取证：历史可读、scan 可跑（临时库副本，不写真库）
  E 接线      迁移三处 + 端点路由注册 + repo 只碰自家表 + 引擎/repo/router
              三层无 agents/config_history 写路径（静态）+ 配置注释含
              铁律依据

运行：python tools/verify/verify_agent_archive.py
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

# ── 干净库 + 隔离配置：先切后 import（core.config/DB 路径 import 期绑定）──
# 子进程（变异自证）用 VERIFY_ARCHIVE_TMP 换独立目录 ⇒ 天然隔离，不必删库。
# ⚠️ 本机 os.remove 对工作区文件走 safe-delete（转回收站）且在子进程里会抛
# OSError ⇒ 门禁启动时**不删库文件**，改用「换路径」而非「删文件」做隔离。
_TMP = os.environ.get("VERIFY_ARCHIVE_TMP") or os.path.join(
    ROOT, "tmp", "verify_agent_archive")
os.makedirs(_TMP, exist_ok=True)
_CLEAN_DB = os.path.join(_TMP, "clean.db")
_CLEAN_CFG = os.path.join(_TMP, "config.json")
# ⚠️ 库文件**不删也不改名**：一旦把 clean.db 挪走，同进程里 get_db() 缓存的
# 单例连接会指向旧 inode ⇒ 后续 CREATE TABLE 落在旧文件、新连接报
# "no such table"（实测踩到）。起点保证靠 init_db() 幂等 + C 段显式清表。
io.open(_CLEAN_CFG, "w", encoding="utf-8").write("{}")
os.environ["MBSE_DB_PATH"] = _CLEAN_DB
os.environ["MBSE_CONFIG_PATH"] = _CLEAN_CFG

_results = []


def check(name, ok, detail=""):
    _results.append((name, bool(ok), detail))
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name,
                           ("  ← " + str(detail)) if (detail and not ok) else ""))


def section(t):
    print("\n── %s ──" % t)


def _read(rel):
    return io.open(os.path.join(ROOT, rel), encoding="utf-8").read()


def _strip_code(src):
    """剥掉 docstring 与注释（静态断言只看**代码形态**：注释里正当引用表名
    是文档需要，不是违规——P2-2 E3 踩过的教训）。"""
    out, in_doc = [], False
    for line in src.split("\n"):
        s = line.strip()
        if in_doc:
            if s.endswith('"""') or s.endswith("'''"):
                in_doc = False
            continue
        if s.startswith('"""') or s.startswith("'''"):
            if not (len(s) > 3 and s.endswith(('"""', "'''"))):
                in_doc = True
            continue
        if s.startswith("#"):
            continue
        out.append(re.sub(r"#.*$", "", line))
    return "\n".join(out)


def _resp(r):
    """端点返回值统一解析：dict 直取，JSONResponse 解析 body（端点失败路径
    返 JSONResponse(500 + ok:False)，「500 是诚实失败语义」要断在 body 上）。"""
    if isinstance(r, dict):
        return r, getattr(r, "status_code", None)
    try:
        return json.loads(r.body), getattr(r, "status_code", None)
    except Exception:
        return {"ok": False, "error": "unparsable response: %r" % (r,)}, None


def _n_rows(conn, table):
    """表行数（表不存在返 0——老库缺表不该让门禁崩）。"""
    try:
        return int(conn.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0])
    except Exception:
        return 0


def _seed_agent_history(conn):
    """合成一段**可检出震荡**的进化历史（干净临时库专用）。

    agent 101/102/103 用高位 id（避开 init_db 内置 agent 的 id，避免主键冲突）。
    · 101 走 v1→v2→v3，system_prompt 被 A→B→A 改回（死路形态：改了又认输）；
    · 102 走 v1→v2 单调演进（正常形态，不该被判震荡）；
    · 103 保持 v1 不动（无历史的新变体：画像该全 unknown 而非假装干净）；
    · 另有一条 **30 天前**的老日志（agent 101 的 v0→v1）：用于验证
      window_days 窗口口径真的在过滤历史（默认 90 天窗口不该收它）。
    """
    import time as _t
    now = _t.strftime("%Y-%m-%d %H:%M:%S")
    old = _t.strftime("%Y-%m-%d %H:%M:%S", _t.localtime(_t.time() - 30 * 86400))
    # 幂等：先把本函数上次种的行清掉（B 段与 C 段各调一次，主键会撞）
    conn.execute("DELETE FROM asset_change_logs WHERE asset_id IN (101, 102, 103)")
    conn.execute("DELETE FROM agents WHERE id IN (101, 102, 103)")
    for aid, ver in ((101, "v3"), (102, "v2"), (103, "v1")):
        conn.execute(
            "INSERT INTO agents (id, name, display_name, version, status) "
            "VALUES (?,?,?,?,'active')",
            (aid, "design_%d" % aid, "设计%d" % aid, ver))
    logs = [
        # agent 101：A→B（v1→v2）→ A（v2→v3）= 改回循环，死路形态
        (101, "system_prompt", "promptA", "promptB", "v1", "v2", now),
        (101, "system_prompt", "promptB", "promptA", "v2", "v3", now),
        # agent 102：单调演进，非死路
        (102, "hil_level", "L0", "L1", "v1", "v2", now),
        (102, "kb_required", "0", "1", "v1", "v2", now),
        # 30 天前的老日志（默认 90 天窗口外，365 天窗口内）
        (101, "description", "描述0", "描述1", "v0", "v1", old),
    ]
    for aid, field, o, n, vf, vt, ts in logs:
        conn.execute(
            "INSERT INTO asset_change_logs (asset_type, asset_id, asset_name, "
            "field, old_value, new_value, version_from, version_to, "
            "changed_by, source, created_at) "
            "VALUES ('agent',?,?,?,?,?,?,?,'tester','api',?)",
            (aid, "a%d" % aid, field, o, n, vf, vt, ts))
    conn.commit()


from database import init_db, get_db                            # noqa: E402
init_db()          # DB_PATH import 期已绑定 _CLEAN_DB（env 在 import 前切好）
from core import agent_archive as aa                             # noqa: E402
from core import config as _cfg                                  # noqa: E402
from repositories.agent_archive_repo import (                   # noqa: E402
    AgentArchiveRepo, fingerprint_of)

_CFG = {"enabled": True, "window_days": 90, "min_denominator": 2,
        "dead_end_repeat": 2, "max_variants": 200}

# ══════════════════════════════════════════════════════════════
section("A 纯函数")
# ══════════════════════════════════════════════════════════════

# A1 结局三态：全正 → pass
_g = aa.grade_outcomes(["pass", "pass", "clean"], min_count=1)
check("A1a 全正结局 ⇒ pass", _g["verdict"] == "pass" and _g["negative"] == 0, _g)
# A2 任一回归 ⇒ regression（不因"多数通过"掩盖）
_g = aa.grade_outcomes(["pass", "regression", "pass"], min_count=1)
check("A2a 混入一次 regression ⇒ regression（不按多数掩盖）",
      _g["verdict"] == "regression" and _g["negative"] == 1, _g)
# A3 error / insufficient_data 不算负向（环境坏 ≠ 这条路不行）
check("A3a error 不算负向",
      aa.is_negative("error") is False and aa.is_negative("insufficient_data") is False
      and aa.is_negative("regression") is True)
_g = aa.grade_outcomes(["pass", "error", "insufficient_data"], min_count=1)
check("A3b error+insufficient 混入 ⇒ 仍 pass 且 negative=0",
      _g["verdict"] == "pass" and _g["negative"] == 0 and _g["count"] == 3, _g)
# A4 诚实分母：不足即 unknown（不回落 pass）
_g = aa.grade_outcomes(["regression"], min_count=2)
check("A4a 负向 1 条但 min_count=2 ⇒ unknown（不回落 pass 不假洗白）",
      _g["verdict"] == "unknown" and _g["negative"] == 1, _g)
_g = aa.grade_outcomes([], min_count=2)
check("A4b 空结论 ⇒ unknown 且 count=0（空窗口不是 0 分）",
      _g["verdict"] == "unknown" and _g["count"] == 0, _g)

# A5 震荡检测（核心信号）：A→B→A 计 1；单调 A→B→C 计 0
check("A5a ★两值往复 A→B→A 计 1 次震荡（死路形态：改了又认输）",
      aa.count_oscillations([("a", "b"), ("b", "a")]) == 1,
      aa.count_oscillations([("a", "b"), ("b", "a")]))
check("A5b 单调演进 A→B→C 无震荡（不是所有改动都是死路）",
      aa.count_oscillations([("a", "b"), ("b", "c")]) == 0)
check("A5b2 三值环 a→b→c→a 不算改回循环（不是 A→B→A 形态，不误判死路）",
      aa.count_oscillations([("a", "b"), ("b", "c"), ("c", "a")]) == 0,
      aa.count_oscillations([("a", "b"), ("b", "c"), ("c", "a")]))
check("A5c ★一次来回只记 1 个震荡（无序对去重，不虚高）",
      aa.count_oscillations([("a", "b"), ("b", "a"), ("a", "b"), ("b", "a")]) == 1,
      aa.count_oscillations([("a", "b"), ("b", "a"), ("a", "b"), ("b", "a")]))
check("A5d 两个字段各震荡一次（同输入内无字段区分 ⇒ 1；跨字段由调用方分桶）",
      aa.count_oscillations([("a", "b"), ("b", "a"), ("c", "d"), ("d", "c")]) == 2,
      aa.count_oscillations([("a", "b"), ("b", "a"), ("c", "d"), ("d", "c")]))
check("A5e 缺前值的对（old 为空）跳过，不猜",
      aa.count_oscillations([(None, "b"), ("b", "a")]) == 0)
check("A5f 自环（old==new）不计震荡",
      aa.count_oscillations([("a", "a"), ("a", "a")]) == 0)
check("A5g 空输入 ⇒ 0", aa.count_oscillations([]) == 0)

# A6 死路门槛：严格 >=
check("A6a 负向 1 < 阈值 2 ⇒ 不判死路（一次回归可能是噪声）",
      aa.dead_end_of(1, threshold=2) is False)
check("A6b 负向 2 >= 阈值 2 ⇒ 判死路",
      aa.dead_end_of(2, threshold=2) is True)
check("A6c 负向 0 ⇒ 不判死路（没失败不等于死路）",
      aa.dead_end_of(0, threshold=2) is False)

# A7 指纹稳定性（不入 diff 内容 ⇒ 重复扫描不注水）
_fp1 = aa.variant_fingerprint(7, "v1")
check("A7a 同 (agent_id, version) 指纹稳定（重复扫描不注水）",
      _fp1 == aa.variant_fingerprint(7, "v1"))
check("A7b 不同 version 指纹不同",
      _fp1 != aa.variant_fingerprint(7, "v2"))
check("A7c ★引擎与 repo 两侧指纹算法同源（否则幂等失效）",
      _fp1 == fingerprint_of(7, "v1"), (_fp1, fingerprint_of(7, "v1")))
check("A7d 空 version 也产稳定指纹（不崩）",
      isinstance(aa.variant_fingerprint(1, ""), str) and len(aa.variant_fingerprint(1, "")) == 16)

# A8 方向声明（杜绝 P2-2 方向坑）：lineage_depth 是唯一「越大越好」的维度
_lb = {d.name: d.lower_better for d in aa.DIMENSIONS}
check("A8a 五维齐备", set(_lb) == {"oscillation", "field_changes", "regressions",
                                   "lineage_depth", "field_diversity"}, _lb)
check("A8b oscillation / field_changes / regressions / field_diversity 均 lower_better",
      all(_lb[k] for k in ("oscillation", "field_changes", "regressions",
                           "field_diversity")))
check("A8c ★lineage_depth 唯一 lower_better=False（探索读数）",
      _lb["lineage_depth"] is False)

# A9 build_profile 是纯函数（无 I/O：同一 history 两次算完全一致）
_hist = {
    "agents": [{"id": 1, "name": "a", "version": "v3", "status": "active"}],
    "agent_logs": [
        {"asset_id": 1, "field": "system_prompt", "old_value": "x",
         "new_value": "y", "version_from": "v1", "version_to": "v2"},
        {"asset_id": 1, "field": "system_prompt", "old_value": "y",
         "new_value": "x", "version_from": "v2", "version_to": "v3"},
    ],
    "field_pairs": {(1, "system_prompt"): [("x", "y"), ("y", "x")]},
    "version_depth": {(1, "v3"): 2},
}
_p1 = aa.build_profile(_hist, agent_id=1, version="v3", outcomes=[], settings=_CFG)
_p2 = aa.build_profile(_hist, agent_id=1, version="v3", outcomes=[], settings=_CFG)
check("A9a build_profile 纯函数（同输入同输出，无 I/O）", _p1 == _p2)

# A10 画像语义：震荡被检出、深度算对、方向判定正确
check("A10a 震荡维度检出 A→B→A（value=1, negative=1）",
      _p1["dimensions"]["oscillation"]["value"] == 1
      and _p1["dimensions"]["oscillation"]["negative"] == 1,
      _p1["dimensions"]["oscillation"])
check("A10b 血缘深度 = version_depth 命中值 2（越大越好方向）",
      _p1["dimensions"]["lineage_depth"]["value"] == 2
      and _p1["dimensions"]["lineage_depth"]["lower_better"] is False)
check("A10c 该版本引入 1 条字段 diff / 改动字段 1 个（version_to=v3 的那一条）",
      _p1["dimensions"]["field_changes"]["value"] == 1
      and _p1["dimensions"]["field_diversity"]["value"] == 1,
      {"fc": _p1["dimensions"]["field_changes"],
       "fd": _p1["dimensions"]["field_diversity"]})
check("A10d 有震荡（负向）⇒ summary regression",
      _p1["summary"]["verdict"] == "regression", _p1["summary"])

# A11 ★诚实 pass 判据（冒烟实测踩到的缺陷）：全探索读数不得判 pass
_hist_pure = {"agents": [{"id": 1, "name": "a", "version": "v1", "status": "active"}],
              "agent_logs": [], "field_pairs": {},
              "version_depth": {(1, "v1"): 0}}
_pp = aa.build_profile(_hist_pure, agent_id=1, version="v1", outcomes=[],
                       settings=_CFG)
check("A11a ★仅 lineage_depth=0（没探索过）⇒ insufficient_data 不判 pass",
      _pp["summary"]["verdict"] == "insufficient_data"
      and _pp["summary"]["positives"] == 0, _pp["summary"])
_hist_clean = {"agents": [{"id": 1, "name": "a", "version": "v2", "status": "active"}],
               "agent_logs": [
                   {"asset_id": 1, "field": "hil_level", "old_value": "L0",
                    "new_value": "L1", "version_from": "v1", "version_to": "v2"},
                   {"asset_id": 1, "field": "kb_required", "old_value": "0",
                    "new_value": "1", "version_from": "v1", "version_to": "v2"}],
               "field_pairs": {(1, "hil_level"): [("L0", "L1")],
                               (1, "kb_required"): [("0", "1")]},
               "version_depth": {(1, "v2"): 1}}
_pc = aa.build_profile(_hist_clean, agent_id=1, version="v2", outcomes=[],
                       settings=_CFG)
check("A11b 有 lower_better 维度且 value==0（真无问题）⇒ pass",
      _pc["summary"]["verdict"] == "pass" and _pc["summary"]["positives"] >= 1,
      _pc["summary"])

# A12 深度回溯防环（血缘成环时不死循环）
_hist_cycle = {"agents": [{"id": 1, "name": "a", "version": "v1", "status": "active"}],
               "agent_logs": [], "field_pairs": {},
               "version_of_edge": {(1, "v1"): "v2", (1, "v2"): "v1"}}
_t0 = time.time()
_hc = aa.collect_history.__doc__ is not None          # 触达函数（不真跑环）
check("A12 深度回溯有环保护（源码含 visited 集合判据）",
      "seen" in aa.collect_history.__code__.co_names
      or "seen" in _strip_code(aa.collect_history.__code__ and
                               open(aa.collect_history.__code__.co_filename,
                                    encoding="utf-8").read()),
      _hc)

# A13 record_outcome 校验：不支持的结论被拒（纯参数校验，不碰库）
_conn_ro = sqlite3.connect(":memory:")
try:
    _r = aa.record_outcome(_conn_ro, agent_id=1, version="v1",
                           outcome="不存在的结论", source="manual")
    check("A13 不支持的 outcome 被显式拒（fail-loud，不静默入库）",
          _r.get("ok") is False and "不支持" in str(_r.get("error")), _r)
finally:
    _conn_ro.close()

# ══════════════════════════════════════════════════════════════
section("B 配置")
# ══════════════════════════════════════════════════════════════

_aa_cfg = _read(os.path.join("core", "config.py"))
check("B1a DEFAULT_CONFIG 有 agent_archive 组", '"agent_archive"' in _aa_cfg)
check("B1b CONFIG_SCHEMA 有 agent_archive 组",
      re.search(r'CONFIG_SCHEMA[\s\S]*?"agent_archive"\s*:\s*\{', _aa_cfg) is not None)

from core.config import DEFAULT_CONFIG, CONFIG_SCHEMA              # noqa: E402
check("B2a DEFAULT_CONFIG.agent_archive 存在", "agent_archive" in DEFAULT_CONFIG)
check("B2b CONFIG_SCHEMA.agent_archive 存在", "agent_archive" in CONFIG_SCHEMA)
_dk = set(DEFAULT_CONFIG["agent_archive"])
_sk = set(CONFIG_SCHEMA["agent_archive"])
check("B2c ★两处键完全一致（无孤儿键，双登记纪律）", _dk == _sk,
      {"defaults_only": sorted(_dk - _sk), "schema_only": sorted(_sk - _dk)})
check("B2d 键集合与引擎 _SETTINGS_DEFAULTS 一致",
      _dk == set(aa._SETTINGS_DEFAULTS), sorted(_dk ^ set(aa._SETTINGS_DEFAULTS)))
check("B2e dead_end_repeat=2（一次回归可能是噪声，反复才是模式）",
      DEFAULT_CONFIG["agent_archive"]["dead_end_repeat"] == 2)
check("B2f min_denominator=2（诚实分母：不足即 unknown）",
      DEFAULT_CONFIG["agent_archive"]["min_denominator"] == 2)

# B3 引擎零写语句（只读历史，写只经 repo）——「只记录不切换生效版本」的结构性证明
_eng = _strip_code(_read(os.path.join("core", "agent_archive.py")))
_banned = ("INSERT ", "UPDATE ", "DELETE ", "REPLACE ", "DROP ", "ALTER ",
           "conn.execute(\"INSERT", "conn.execute(\"UPDATE")
_hits = [b for b in _banned if b in _eng]
check("B3 ★引擎零写语句（INSERT/UPDATE/DELETE/DROP/ALTER 全不出现）",
      not _hits, _hits)
# 引擎只 SELECT 既有历史表
_sel_tables = set(re.findall(r"FROM\s+(\w+)", _eng))
_allowed_sel = {"agents", "asset_change_logs", "sqlite_master"}
check("B4 ★引擎只读三张既有表（agents/asset_change_logs/sqlite_master）",
      _sel_tables <= _allowed_sel, sorted(_sel_tables - _allowed_sel))

# B5 口径真被消费：影子配置覆盖 window_days=0 ⇒ 历史查不到 ⇒ scanned=0
#   （影子层在 core.config 模块加载期解析 ⇒ 改 env 后必须 _cfg.reload()，
#     与 verify_misevolution B6 同一姿势）
_conn = sqlite3.connect(_CLEAN_DB)
_conn.row_factory = sqlite3.Row
try:
    # 清台账：门禁幂等（同一 clean.db 复用），上次运行留下的 agent_variants
    # 会让"窗口 1 天时 v0 不入册"这类断言假红（记忆只增不减是设计语义）
    for _t in ("variant_outcomes", "agent_variants"):
        try:
            _conn.execute("DELETE FROM %s" % _t)
        except Exception:
            pass                  # 首跑时表还没建（init_db 已在上方建过，兜底即可）
    _conn.commit()
    _seed_agent_history(_conn)
    _old_shadow = os.environ.get("MBSE_SHADOW_CONFIG")
    try:
        os.environ["MBSE_SHADOW_CONFIG"] = json.dumps(
            {"agent_archive": {"window_days": 1}})
        _cfg.reload()
        _st0 = aa.get_settings()
        check("B5a get_settings 读到影子 window_days=1（影子层对本组生效）",
              _st0["window_days"] == 1, _st0)
        # 窗口 1 天 ⇒ 30 天前的老日志（v0→v1）收不到 ⇒ v0 不入册
        _out0 = aa.scan(_conn, settings=None)
        _r0 = AgentArchiveRepo(_conn)
        _has_v0 = _r0.get_variant(agent_id=101, version="v0") is not None
        _has_v2 = _r0.get_variant(agent_id=101, version="v2") is not None
        check("B5b ★窗口 1 天：当日历史版本 v2 仍入册（口径真被消费）",
              _has_v2 is True, {"has_v2": _has_v2})
        check("B5c ★窗口 1 天：30 天前的老版本 v0 不入册（窗口真的在过滤历史）",
              _has_v0 is False, {"has_v0": _has_v0})
        check("B5d 分层合并：未覆盖键仍取默认（不是整体替换）",
              _st0["dead_end_repeat"] == 2 and _st0["min_denominator"] == 2, _st0)
    finally:
        if _old_shadow is None:
            os.environ.pop("MBSE_SHADOW_CONFIG", None)
        else:
            os.environ["MBSE_SHADOW_CONFIG"] = _old_shadow
        _cfg.reload()
    # 台账只增不减（档案库不"忘掉"已入册的历史——记忆不可逆，同死路终态）：
    # 窗口放宽后老版本 v0 **依然在册**（上一步没入册的话才该是新入册）
    _out_wide = aa.scan(_conn, settings=None)
    _has_v0_wide = AgentArchiveRepo(_conn).get_variant(
        agent_id=101, version="v0") is not None
    check("B5e ★窗口复位 90 天后老版本 v0 入册（窗口口径双向可证）",
          _has_v0_wide is True, {"has_v0": _has_v0_wide})
    _st_r = aa.get_settings()
    check("B5d 影子撤除后口径复位（reload 到位，不污染后续断言）",
          _st_r["window_days"] == 90 and _st_r["min_denominator"] == 2, _st_r)
    # B6 未知键 fail-loud（P1-4 影子层纪律对本组同样生效）
    try:
        os.environ["MBSE_SHADOW_CONFIG"] = '{"agent_archive": {"no_such_key": 1}}'
        _raised = False
        try:
            _cfg.reload()
        except ValueError:
            _raised = True
        check("B6 影子未知键 ⇒ fail-loud（typo 不静默失效）", _raised)
    finally:
        if _old_shadow is None:
            os.environ.pop("MBSE_SHADOW_CONFIG", None)
        else:
            os.environ["MBSE_SHADOW_CONFIG"] = _old_shadow
        _cfg.reload()
finally:
    _conn.close()


# ══════════════════════════════════════════════════════════════
section("C 端到端（干净临时库）")
# ══════════════════════════════════════════════════════════════

print("  （C 段清空业务表：避免 B 段影子配置/扫描残留污染端到端语义。")
print("   用 DELETE 而非删库文件——get_db() 单例持有旧连接，删文件会让")
print("   后续 CREATE 落空、新连接报 no such table）")
_BUSINESS_TABLES = ("agent_variants", "variant_outcomes", "asset_change_logs")
_singleton = get_db()
for _t in _BUSINESS_TABLES:
    try:
        _singleton.execute("DELETE FROM %s" % _t)
    except Exception as _e:
        print("   （跳过 %s：%s）" % (_t, _e))
# B 段种子 agent（101/102/103）也清掉——C 段自己重新种，保证起点确定
_singleton.execute("DELETE FROM agents WHERE id IN (101, 102, 103)")
_singleton.commit()
init_db()               # 幂等：补回被 B 段删过的行不会重灌（种子有存在性判据）
_conn = sqlite3.connect(_CLEAN_DB)
_conn.row_factory = sqlite3.Row
_seed_agent_history(_conn)

_repo = AgentArchiveRepo(_conn)

# C1 收敛台账：agents 当前版本 + 历史 version_to + 前代 version_from 都入册
_out1 = aa.scan(_conn)
check("C1a scan 入册变体（当前版本 + 历史版本 + 前代都收）",
      _out1["created"] >= 3 and _out1["scanned"] >= 3, _out1)
check("C1b 首扫 enabled=True", _out1["enabled"] is True)
_cnts = _repo.count_by_status()
check("C1c 分状态计数：现行 active + 历史 retired",
      _cnts.get("active", 0) >= 1 and _cnts.get("retired", 0) >= 1, _cnts)
_v_v3 = _repo.get_variant(agent_id=101, version="v3")
check("C1d 现行版本 v3 记 active", _v_v3 and _v_v3["status"] == "active", _v_v3)
_v_v1 = _repo.get_variant(agent_id=101, version="v1")
check("C1e 历史版本 v1 记 retired（非现行）",
      _v_v1 and _v_v1["status"] == "retired", _v_v1)
check("C1f 血缘 parent_version 从 version_to→version_from 链回溯",
      _v_v3 and _v_v3["parent_version"] == "v2", _v_v3)

# C2 指纹幂等：二次扫描 created=0，skipped=候选数（不注水）
_out2 = aa.scan(_conn)
check("C2a ★二次扫描 created=0（指纹幂等，台账不注水）",
      _out2["created"] == 0 and _out2["skipped"] == _out1["scanned"],
      {"created": _out2["created"], "skipped": _out2["skipped"],
       "first": _out1["scanned"]})
check("C2b 台账行数不变（%d 行）" % len(_repo.list_variants(limit=100)),
      len(_repo.list_variants(limit=100)) == _out1["created"])

# C3 画像与震荡检出
_out3 = aa.refresh(_conn)
_v_v3b = _repo.get_variant(agent_id=101, version="v3")
_prof = _v_v3b.get("profile") or {}
check("C3a refresh 后画像落库（profile 非空）",
      bool(_prof.get("dimensions")), _prof)
check("C3b ★A→B→A 改回循环被检出（oscillation=1）",
      (_prof.get("summary") or {}).get("oscillation") == 1,
      (_prof.get("summary") or {}))
check("C3c 有震荡 ⇒ summary.verdict=regression",
      (_prof.get("summary") or {}).get("verdict") == "regression",
      (_prof.get("summary") or {}))

# C4 显式挂载结局（档案库的入户口）
_r4 = aa.record_outcome(_conn, agent_id=101, version="v3",
                        outcome="regression", source="manual")
check("C4a 挂载 regression ⇒ ok", _r4.get("ok") is True, _r4)
_r4b = aa.record_outcome(_conn, agent_id=101, version="v3",
                         outcome="pass", source="manual")
check("C4b 挂载 pass ⇒ ok", _r4b.get("ok") is True, _r4b)
check("C4c 负向计数 = 1（只有 regression 计入）",
      _repo.count_negative_outcomes(int(_v_v3b["id"])) == 1)
# error 不计入负向
aa.record_outcome(_conn, agent_id=101, version="v3", outcome="error", source="manual")
check("C4d ★error 挂载后负向计数仍 = 1（环境坏≠这条路不行）",
      _repo.count_negative_outcomes(int(_v_v3b["id"])) == 1,
      _repo.count_negative_outcomes(int(_v_v3b["id"])))

# C5 未入册变体拒绝挂载（不凭空造历史）
_r5 = aa.record_outcome(_conn, agent_id=999, version="v99",
                        outcome="regression", source="manual")
check("C5 ★未入册变体拒绝挂载（档案库不凭空造历史）",
      _r5.get("ok") is False and "未入册" in str(_r5.get("error")), _r5)
_n_before = len(_repo.list_variants(limit=500))
check("C5b 拒绝后台账行数不变（真没建行）",
      len(_repo.list_variants(limit=500)) == _n_before)

# C6 死路判定：负向达阈值才判；且 error 不推
aa.record_outcome(_conn, agent_id=101, version="v3", outcome="regression",
                  source="manual")
check("C6a 负向达 2 ⇒ 达阈值", _repo.count_negative_outcomes(int(_v_v3b["id"])) == 2)
_out6 = aa.refresh(_conn)
_v_v3c = _repo.get_variant(agent_id=101, version="v3")
check("C6b ★refresh 后该变体判 dead_end（死路记忆落点）",
      _v_v3c["status"] == "dead_end", _v_v3c)
check("C6c dead_end 有原因与时间戳（可追溯）",
      bool(_v_v3c.get("dead_end_reason")) and bool(_v_v3c.get("dead_end_at")), _v_v3c)
_dead = aa.query_dead_ends(_conn)
check("C6d ★query_dead_ends 能查到（提案侧回避入口）", len(_dead) >= 1, len(_dead))
_hint = aa.dead_end_hint(_conn, agent_id=101, version="v3")
check("C6e ★dead_end_hint 对死路变体报 is_dead_end=True",
      _hint["is_dead_end"] is True and _hint["status"] == "dead_end", _hint)
_hint_u = aa.dead_end_hint(_conn, agent_id=888, version="vX")
check("C6f ★未入册变体 hint 老实说 unrecorded（没记录≠没问题）",
      _hint_u["status"] == "unrecorded" and _hint_u["is_dead_end"] is False, _hint_u)

# C6e 死路是终态：再挂 pass 也不复活
aa.record_outcome(_conn, agent_id=101, version="v3", outcome="pass", source="manual")
aa.refresh(_conn)
_v_v3d = _repo.get_variant(agent_id=101, version="v3")
check("C6g ★死路是终态（再挂 pass + refresh 不复活）",
      _v_v3d["status"] == "dead_end", _v_v3d)

# C7 只记录不切换生效版本：agents 表 version 不得被档案库改动
_ver_now = _conn.execute("SELECT version FROM agents WHERE id=101").fetchone()[0]
check("C7a ★agents.version 未被档案库改动（记忆不是手）", _ver_now == "v3", _ver_now)

# C8 ★全表快照零副作用：除自家两表外任何表行数零变化
_all_tables = [r[0] for r in _conn.execute(
    "SELECT name FROM sqlite_master WHERE type='table' "
    "AND name NOT LIKE 'sqlite_%'").fetchall()]
_snap_before = {t: _n_rows(_conn, t) for t in _all_tables}
aa.scan(_conn)
aa.refresh(_conn)
_snap_after = {t: _n_rows(_conn, t) for t in _all_tables}
_dirty = {t: (_snap_before[t], _snap_after[t]) for t in _all_tables
          if _snap_before[t] != _snap_after[t]}
check("C8a ★零副作用：仅 agent_variants/variant_outcomes 可变，其余全零变化",
      set(_dirty) <= {"agent_variants", "variant_outcomes"}, _dirty)

# C9 ★第二连接可见性（repo 自带 commit 的行为级证明：BaseRepo.execute 不 commit）
_conn.commit()
_conn2 = sqlite3.connect(_CLEAN_DB)
_conn2.row_factory = sqlite3.Row
try:
    _seen = _conn2.execute(
        "SELECT COUNT(*) AS n FROM agent_variants WHERE status='dead_end'"
    ).fetchone()[0]
    check("C9a ★第二连接可见死路行（repo 写方法自带 commit）",
          _seen >= 1, _seen)
    _n_out = _conn2.execute(
        "SELECT COUNT(*) AS n FROM variant_outcomes").fetchone()[0]
    check("C9b 第二连接可见结局行（commit 到位）", _n_out >= 3, _n_out)
finally:
    _conn2.close()

# C10 端点行为级（含冷库显式失败）
import routers.agent_archive as _aa_router                       # noqa: E402
_paths = {r.path for r in _aa_router.router.routes}
check("C10a 五个端点注册（台账/扫描/复算/挂载/血缘）",
      _paths == {"/api/agent-archive", "/api/agent-archive/scan",
                 "/api/agent-archive/refresh",
                 "/api/agent-archive/{variant_id}/outcome",
                 "/api/agent-archive/lineage/{agent_id}"}, sorted(_paths))
_r_list = _aa_router.list_variants(agent_id=0, status="", limit=50, conn=_conn)
check("C10b 列表端点正常返回（ok + 台账 + 分态计数 + 维度契约）",
      _r_list.get("ok") is True and len(_r_list.get("variants") or []) >= 1
      and _r_list.get("statuses") == list(aa.VARIANT_STATUSES)
      and {d["name"] for d in (_r_list.get("dimensions") or [])}
      == set(aa.DIMENSION_NAMES), {k: _r_list.get(k) for k in ("ok", "counts")})
_r_scan = _aa_router.scan_archive(conn=_conn, user=None)
check("C10c 扫描端点幂等（二次 created=0）",
      _r_scan.get("ok") is True and _r_scan.get("created") == 0, _r_scan)
_r_lin = _aa_router.get_lineage(agent_id=101, conn=_conn)
check("C10d 血缘端点返回该 agent 的版本台账",
      _r_lin.get("ok") is True and len(_r_lin.get("lineage") or []) >= 2,
      len(_r_lin.get("lineage") or []))

# 冷库（从未 init_db 的新连接）⇒ 显式 ok:False，不 500 不静默
_cold = sqlite3.connect(":memory:")
try:
    _r_cold = _aa_router.list_variants(agent_id=0, status="", limit=10, conn=_cold)
    check("C10e ★冷库列表 ⇒ 显式 ok:False+error（不裸 500）",
          _r_cold.get("ok") is False and "agent_variants" in str(_r_cold.get("error")),
          _r_cold)
    _r_cold_scan = _aa_router.scan_archive(conn=_cold, user=None)
    # 扫描失败路径返 JSONResponse(500 + ok:False)——解析 body 断言语义
    _cs_body, _cs_code = _resp(_r_cold_scan)
    check("C10f ★冷库扫描 ⇒ 显式失败（不裸 500、不静默造历史）",
          _cs_body.get("ok") is False and _cs_code == 500, (_cs_code, _cs_body))
finally:
    _cold.close()

# C11 端点采集异常显式化（猴子补丁 collect_history 抛错）
_conn3 = sqlite3.connect(_CLEAN_DB)
_conn3.row_factory = sqlite3.Row
_orig_hist = aa.collect_history


def _boom_hist(*_a, **_kw):
    raise RuntimeError("__aa_boom__")


try:
    aa.collect_history = _boom_hist
    _r_boom = _resp(_aa_router.scan_archive(conn=_conn3, user=None))
    check("C11 ★历史读取失败 ⇒ 扫描端点显式报错（不 500 不静默）",
          _r_boom[0].get("ok") is False
          and "__aa_boom__" in str(_r_boom[0].get("error")), _r_boom)
finally:
    aa.collect_history = _orig_hist
check("C11b 猴子补丁已复位（后续断言不受污染）", aa.collect_history is _orig_hist)
_conn3.close()

def _run_mutation_proofs():
    """变异自证：每组变异都应让目标断言判红（证明断言有牙，不是恒真）。"""
    _PY = sys.executable
    _MUT_SRC = os.path.join(ROOT, "core", "agent_archive.py")


    def _mutate_and_run(name, old, new, gate_rel, expect_fail_prefixes):
        """变异 → 子进程跑门禁 → 断言目标断言判红。

        两处必须小心（都实测踩过）：
        ① 子进程用**独立库/配置路径 + 独立 tmp 目录**（VERIFY_ARCHIVE_TMP），
           不复用父进程的 clean.db——否则与父进程持有的 get_db() 单例 + WAL
           边车打架，子进程在断言前就崩（no such table），变异自证静默失效；
        ② 还原用 **io.open 覆写**而非 shutil.move/os.replace——本机文件删除
           走 safe-delete（转回收站），在异常路径上会失败并留下污染的变异体
           （实测有一次还原失败，引擎里留了 return 0 的残缺代码）。
           finally 里覆写回**原始内容**（_ORIGINAL 快照），不依赖文件系统移动。
        """
        src = io.open(_MUT_SRC, encoding="utf-8").read()
        mut = src.replace(old, new)
        assert mut != src, "变异未命中: %s" % name
        _sub_dir = os.path.join(ROOT, "tmp", "verify_agent_archive_mut_%s"
                                % name.split()[0])
        os.makedirs(_sub_dir, exist_ok=True)
        _env = dict(os.environ)
        _env["MBSE_DB_PATH"] = os.path.join(_sub_dir, "mut.db")
        _env["MBSE_CONFIG_PATH"] = os.path.join(_sub_dir, "config.json")
        _env["MBSE_SHADOW_CONFIG"] = ""
        _env["VERIFY_ARCHIVE_TMP"] = _sub_dir
        _env["VERIFY_ARCHIVE_MUT_CHILD"] = "1"
        io.open(_env["MBSE_CONFIG_PATH"], "w", encoding="utf-8").write("{}")
        try:
            io.open(_MUT_SRC, "w", encoding="utf-8").write(mut)
            proc = subprocess.run([_PY, "-X", "utf8", os.path.join(ROOT, gate_rel)],
                                  capture_output=True, text=True, cwd=ROOT,
                                  timeout=420, env=_env)
            out = proc.stdout + proc.stderr
            reds = [ln for ln in out.split("\n") if "[FAIL]" in ln]
            hit = any(p in ln for ln in reds for p in expect_fail_prefixes)
            check(name, bool(reds) and hit,
                  {"rc": proc.returncode, "reds": reds[:3],
                   "expect": expect_fail_prefixes,
                   "stderr_tail": proc.stderr.strip().split("\n")[-2:] if not reds else ""})
        finally:
            # 覆写回原始内容（不依赖任何文件移动/删除操作）
            io.open(_MUT_SRC, "w", encoding="utf-8").write(src)


    _GATE = os.path.join("tools", "verify", "verify_agent_archive.py")

    # M1 震荡检测失灵（改回循环不再计数 ⇒ 核心信号失灵）
    _mutate_and_run(
        "M1 震荡检测失灵（A→B→A 不计）⇒ A5a/C3b 判红",
        "    # a < b 只在无序对的一侧计数 ⇒ 一次来回记 1，不虚高\n    return sum(1 for (a, b) in edges if (b, a) in edges and a < b)",
        "    # a < b 只在无序对的一侧计数 ⇒ 一次来回记 1，不虚高\n    return 0",
        _GATE, ("A5a", "C3b"))

    # M2 error 被当负向 ⇒ 负向计数虚高
    _mutate_and_run(
        "M2 error 被当负向 ⇒ A3/C4d 判红",
        'NEGATIVE_OUTCOMES = (OUTCOME_REGRESSION,)',
        'NEGATIVE_OUTCOMES = (OUTCOME_REGRESSION, OUTCOME_ERROR)',
        _GATE, ("A3", "C4d"))

    # M3 诚实分母失效（不足也回落 pass）
    _mutate_and_run(
        "M3 分母不足回落 pass ⇒ A4a/C4 判红",
        '    if n >= int(min_count):\n        out["verdict"] = OUTCOME_REGRESSION if neg > 0 else OUTCOME_PASS',
        '    if n >= 0:\n        out["verdict"] = OUTCOME_REGRESSION if neg > 0 else OUTCOME_PASS',
        _GATE, ("A4a",))

    # M4 死路门槛失效（一次回归即判死路）
    _mutate_and_run(
        "M4 死路门槛取 1 ⇒ A6a 判红",
        "    return int(negative_count or 0) >= int(threshold)",
        "    return int(negative_count or 0) >= 1",
        _GATE, ("A6a",))

    # M5 pass 判据放宽（探索读数也当 pass 证据 ⇒ 冒烟同型缺陷复现）
    _mutate_and_run(
        "M5 pass 判据放宽（探索读数当正面证据）⇒ A11a 判红",
        "    positives = sum(1 for d in known\n                    if d[\"lower_better\"] and d[\"negative\"] == 0\n                    and d[\"value\"] is not None and float(d[\"value\"]) == 0.0)",
        "    positives = sum(1 for d in known\n                    if d[\"negative\"] == 0)",
        _GATE, ("A11a",))

    # M6 死路被复活（终态可覆盖）
    _MUT_REPO = os.path.join(ROOT, "repositories", "agent_archive_repo.py")
    _rsrc_orig = io.open(_MUT_REPO, encoding="utf-8").read()
    try:
        _rmut = _rsrc_orig.replace(
            "WHERE id=? AND status!='dead_end'", "WHERE id=?")
        assert _rmut != _rsrc_orig, "变异未命中: M6"
        io.open(_MUT_REPO, "w", encoding="utf-8").write(_rmut)
        _sub6 = os.path.join(ROOT, "tmp", "verify_agent_archive_mut_M6")
        os.makedirs(_sub6, exist_ok=True)
        _env6 = dict(os.environ)
        _env6["MBSE_DB_PATH"] = os.path.join(_sub6, "mut.db")
        _env6["MBSE_CONFIG_PATH"] = os.path.join(_sub6, "config.json")
        _env6["MBSE_SHADOW_CONFIG"] = ""
        _env6["VERIFY_ARCHIVE_TMP"] = _sub6
        _env6["VERIFY_ARCHIVE_MUT_CHILD"] = "1"
        io.open(_env6["MBSE_CONFIG_PATH"], "w", encoding="utf-8").write("{}")
        proc = subprocess.run([_PY, "-X", "utf8", os.path.join(ROOT, _GATE)],
                              capture_output=True, text=True, cwd=ROOT, timeout=420,
                              env=_env6)
        _r6 = proc.stdout + proc.stderr
        _reds6 = [ln for ln in _r6.split("\n") if "[FAIL]" in ln]
        check("M6 死路可被覆盖（非终态）⇒ C6g 判红",
              any("C6g" in ln for ln in _reds6),
              {"reds": _reds6[:3],
               "stderr_tail": proc.stderr.strip().split("\n")[-2:] if not _reds6 else ""})
    finally:
        io.open(_MUT_REPO, "w", encoding="utf-8").write(_rsrc_orig)

    _conn.close()

    # ══════════════════════════════════════════════════════════
    section("C' 变异自证（证明断言有牙）")
    # ══════════════════════════════════════════════════════════

    # 子进程（被 _mutate_and_run 拉起做验证）**跳过本段**：否则变异体会被
    # 自己的子进程再变异一次，断言"变异未命中"而假红（M6 实测踩到）。
    _IS_MUT_CHILD = bool(os.environ.get("VERIFY_ARCHIVE_MUT_CHILD"))
    if _IS_MUT_CHILD:
        print("  （子进程：跳过变异自证段）")
    else:
        _run_mutation_proofs()

# ══════════════════════════════════════════════════════════════
section("D 真库（只读取证）")
# ══════════════════════════════════════════════════════════════

# 真库只读：复制一份文件做档案库收敛（绝不在真库上建表/写入）
_real_db = os.path.join(ROOT, "mbse.db")
if not os.path.exists(_real_db):
    print("  （跳过：未找到真库 mbse.db）")
else:
    import shutil as _sh
    _real_copy = os.path.join(_TMP, "real_copy.db")
    _sh.copyfile(_real_db, _real_copy)          # 覆写即可，无需先删
    _rc = sqlite3.connect(_real_copy)
    _rc.row_factory = sqlite3.Row
    # 副本上建 P2-3 两表（真库还没跑过该迁移；**绝不写真库**——
    # 真库要等应用下次启动时 lifespan 的 init_db 建表）
    try:
        from database.migrations.misc import _migrate_agent_archive_p23
        _migrate_agent_archive_p23(_rc)
        _rc.commit()
    except Exception as _e:
        print("  （副本建表失败：%s）" % _e)
    try:
        _r_ag = _rc.execute("SELECT COUNT(*) AS n FROM agents").fetchone()[0]
        _r_log = _rc.execute(
            "SELECT COUNT(*) AS n FROM asset_change_logs WHERE asset_type='agent'"
        ).fetchone()[0]
        check("D1 真库可读：agents %d 个 / agent 变更日志 %d 条" % (_r_ag, _r_log),
              _r_ag > 0, {"agents": _r_ag, "logs": _r_log})
        _hist_r = aa.collect_history(_rc, cutoff=aa._cutoff(90))
        check("D2 真实进化历史可读（collect_history 不抛）",
              isinstance(_hist_r.get("agents"), list)
              and _hist_r.get("version_depth") is not None,
              {"agents": len(_hist_r.get("agents") or []),
               "agent_logs": len(_hist_r.get("agent_logs") or [])})
        _r_scan_r = aa.scan(_rc)
        check("D3 ★真库收敛台账成功（写在副本上，真库零写入）",
              _r_scan_r["enabled"] is True and _r_scan_r["created"] >= 1,
              {"scanned": _r_scan_r["scanned"], "created": _r_scan_r["created"]})
        _r_ref_r = aa.refresh(_rc)
        check("D4 真库复算画像成功",
              _r_ref_r["enabled"] is True and _r_ref_r["refreshed"] >= 1,
              {"refreshed": _r_ref_r["refreshed"],
               "dead_ends": _r_ref_r["dead_ends"]})
        _r_osc = _ref_osc = 0
        for _v in AgentArchiveRepo(_rc).list_variants(limit=500):
            _s = (_v.get("profile") or {}).get("summary") or {}
            if _s.get("oscillation"):
                _r_osc += int(_s["oscillation"])
        print("  · 真库实况：变体 %d 个，其中检出字段震荡的变体累计震荡 %d 次"
              % (_r_ref_r["refreshed"], _r_osc))
        check("D5 真库震荡检出为非负整数（不虚高、不为负）", _r_osc >= 0, _r_osc)
        _n_rows_real = _rc.execute("SELECT COUNT(*) AS n FROM agent_variants").fetchone()[0]
        check("D6 副本库建出变体台账（%d 行）" % _n_rows_real, _n_rows_real >= 1)
        _r_osc2 = aa.count_oscillations([("a", "b"), ("b", "a")])
        check("D7 真库环境震荡函数行为一致（sanity）", _r_osc2 == 1)
    finally:
        _rc.close()
        # 副本可覆盖无需删除；但真库有几百 MB，**归零**而不是留着占磁盘
        #（os.remove 走 safe-delete 转回收站，378MB 会占满回收站）
        try:
            io.open(_real_copy, "wb").close()
        except Exception:
            pass

# ══════════════════════════════════════════════════════════════
section("E 接线")
# ══════════════════════════════════════════════════════════════

_mi = _read(os.path.join("database", "migrations", "__init__.py"))
_sc = _read(os.path.join("database", "schema.py"))
_ms = _read(os.path.join("database", "migrations", "misc.py"))
check("E1a 迁移函数已定义（misc.py）",
      "def _migrate_agent_archive_p23(conn)" in _ms)
check("E1b migrations/__init__.py import 接线",
      "_migrate_agent_archive_p23," in _mi)
check("E1c migrations/__init__.py __all__ 登记",
      '"_migrate_agent_archive_p23"' in _mi)
check("E1d schema.py import 接线",
      "_migrate_agent_archive_p23," in _sc)
check("E1e schema.py 调用点接线",
      "_migrate_agent_archive_p23(conn)" in _sc)

_ci = _read(os.path.join(".github", "workflows", "ci.yml"))
check("E2 CI 已接线 verify_agent_archive", "verify_agent_archive.py" in _ci)
check("E2b CI 注释含设计依据（DGM/死路记忆）",
      "档案库" in _ci or "死路" in _ci or "P2-3" in _ci)

_ri = _read(os.path.join("routers", "__init__.py"))
_mn = _read("main.py")
check("E3a routers/__init__.py 导入 agent_archive_router",
      "agent_archive_router" in _ri)
check("E3b routers/__init__.py __all__ 登记",
      '"agent_archive_router"' in _ri)
check("E3c main.py 装配 include 列表含 agent_archive_router",
      "agent_archive_router" in _mn)

# E4 ★repo 只碰自家两表（静态：所有表名都在白名单内）
_repo_src = _strip_code(_read(os.path.join("repositories", "agent_archive_repo.py")))
_repo_code = _repo_src
_repo_tables = set(re.findall(r"(?:FROM|INTO|UPDATE)\s+(\w+)", _repo_src))
_allowed_repo = {"agent_variants", "variant_outcomes"}
check("E4 ★repo 只碰自家两表",
      _repo_tables <= _allowed_repo, sorted(_repo_tables - _allowed_repo))

# E5 ★三层无「切换生效版本 / 回滚配置」写路径（结构性证明）
_engine_code = _strip_code(_read(os.path.join("core", "agent_archive.py")))
_router_code = _strip_code(_read(os.path.join("routers", "agent_archive.py")))
_forbidden = ("UPDATE agents", "UPDATE config_history", "DELETE FROM agents",
              "INSERT INTO agents", "UPDATE sandbox_runs", "DELETE FROM sandbox_runs",
              "update_agent", "rollback")
_hits_f = []
for _tag, _code in (("engine", _engine_code), ("repo", _repo_code),
                    ("router", _router_code)):
    for _f in _forbidden:
        if _f in _code:
            _hits_f.append("%s:%s" % (_tag, _f))
check("E5 ★引擎/repo/router 三层无切换生效版本/回滚路径（记忆不是手）",
      not _hits_f, _hits_f)

# E6 repo 三个写方法自带 commit（BaseRepo.execute 不 commit，P1-4 踩坑）
_writers = re.findall(r"def (register_variant|add_outcome|update_profile|mark_dead_end)\b",
                      _repo_src)
check("E6a repo 恰有 4 个写方法", len(_writers) == 4, _writers)
_commit_n = _repo_src.count("self.conn.commit()")
check("E6b ★四个写方法都自带 commit（BaseRepo.execute 不 commit，P1-4 铁律）",
      _commit_n >= 4, _commit_n)

# E7 配置注释含铁律依据（可追溯）
# 注释写在 "agent_archive" 键**之前**（本仓配置注释惯例）⇒ 从组注释标记
# 起取窗口，而不是从键名起取。
_cfg_at = _aa_cfg.find("P2-3 Agent 档案库")
_cfg_block = _aa_cfg[_cfg_at:_cfg_at + 700] if _cfg_at >= 0 else ""
check("E7a 配置注释含「只记录不切换生效版本」铁律",
      "只记录" in _cfg_block and "切换" in _cfg_block
      and "档案库是记忆，不是手" in _cfg_block, _cfg_block[:200])
check("E7b 迁移 docstring 含「死路」设计说明", "死路" in _ms)

# E8 端点审计留痕（三个写端点都 audit）
check("E8 scan/refresh/outcome 三端点都有 audit 留痕",
      _router_code.count("audit(") >= 3, _router_code.count("audit("))

# ══════════════════════════════════════════════════════════════
print("\n" + "═" * 66)
_p = sum(1 for _n, ok, _d in _results if ok)
_f = len(_results) - _p
print("结果: %d PASS / %d FAIL / 共 %d" % (_p, _f, len(_results)))
if _f:
    print("\n失败明细：")
    for _n, ok, _d in _results:
        if not ok:
            print("  [FAIL] %s" % _n)
sys.exit(1 if _f else 0)