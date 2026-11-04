# -*- coding: utf-8 -*-
"""门禁：**环境可重建** —— 资产快照 ↔ 库 的漂移检测（2026-11-04 新增）。

── 为什么要有这个门禁（米爸「后续手动新增的 agent 还会出问题吗」）──────────────

本轮实测（同一份代码）：

| 项 | `init_db()` 建的空库 | 真库 | 差 |
|---|---|---|---|
| agents | 7 | 30 | 23 |
| tools | 14 | 45 | 31 |
| skills | 0 | 42 | 42 |
| agents 表列数 | 25 | 28 | 3（task_domain/stage_order/intent_keywords_active） |

⇒ **环境不可重建**。后果不是"少几个 agent"这么轻，而是：
  · CI 上那些 agent 根本不存在 ⇒ 依赖它们的门禁**静默跳过**（校验 0 行也算通过）；
  · 缺 `stage_order` 列让"建模阶段意图"恒为空集，缺 `task_domain` 让多任务域路由整条失效，
    而相关代码大多是 `try/except` 兜底 ⇒ **空库上什么都不报错**；
  · 于是"CI 全绿"与"这台机器上的产品对不对"**没有关系**。

── 本门禁守什么 ─────────────────────────────────────────────────────────
  A1 快照存在且结构完整（三张表 + 两张关联表键齐）
  A2 库与快照**无漂移**：库里有、快照没有 = 有人新增了资产但没导出
     （这正是"后续手动新增 agent"会踩的坑 —— 现在它会红，而不是悄悄落后）
  A3 同名字段一致：改了 agent 的 prompt/schema/绑定但没导出，也红
  A4 `init_db()` 能给出**与快照一致**的资产量（真跑建库到临时库，不污染任何现有库）
  A5 agents 表包含流水线所需的三个关键列（防止 schema 漂移复发）

修复动作统一是：`python tools/export_asset_snapshot.py --apply` 然后提交快照。
"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from database.bootstrap_assets import (       # noqa: E402
    SPEC, snapshot_path, load_snapshot, snapshot_drift)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print("  PASS %s" % name)
    else:
        FAIL.append((name, detail))
        print("  FAIL %s  <- %s" % (name, detail))


print("=" * 68)
print("A 门禁：环境可重建（资产快照 ↔ 库）")
print("=" * 68)

# ── A1：快照结构 ────────────────────────────────────────────────────────
_sp = snapshot_path()
check("A1 快照文件存在", os.path.isfile(_sp), "缺 %s" % _sp)
_snap = load_snapshot()
if not _snap:
    print("\n无法继续：快照缺失或不可解析。请先运行 "
          "python tools/export_asset_snapshot.py --apply")
    print("PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
    raise SystemExit(1)

_missing_tables = [t for t in SPEC if not (_snap.get("tables") or {}).get(t)]
check("A1b 快照含全部配置表（%s）" % "/".join(SPEC), not _missing_tables,
      "缺表 %s" % _missing_tables)

for t, meta in SPEC.items():
    rows = (_snap.get("tables") or {}).get(t) or []
    bad = [r for r in rows if not str(r.get(meta["key"]) or "").strip()]
    check("A1c %s 每行都有业务键 `%s`" % (t, meta["key"]), not bad,
          "%d 行缺键" % len(bad))

_rel_missing = [r for r in ("agent_tools", "agent_team_members")
                if r not in (_snap.get("relations") or {})]
check("A1d 快照含关联表（agent_tools/agent_team_members）", not _rel_missing,
      "缺 %s" % _rel_missing)

# ── A2/A3：库 ↔ 快照漂移 ────────────────────────────────────────────────
# ⚠️ 只对**可用的库**做漂移检查；CI 的空库还没自举时，这里用 A4 的临时库代替。
_conn = None
try:
    from database import get_db
    _conn = get_db()
except Exception as e:
    print("  [INFO] 无法连接当前库（%s），跳过 A2/A3，由 A4 覆盖" % type(e).__name__)

if _conn is not None:
    _d = snapshot_drift(_conn)
    check("A2 库里没有「新增但未导出」的资产", not _d["missing_in_snapshot"],
          "这些在库里、快照里没有（请 export 后提交）: %s" % _d["missing_in_snapshot"][:8])
    check("A3 声明性字段与快照一致（改了没导出会红）", not _d["field_diff"],
          "不一致: %s" % _d["field_diff"][:8])
    # ── A2b：快照有、库无 ──────────────────────────────────────────────
    # ⚠️ 这一条是**变异自证抓出来的判据缺口**（本轮实测）：
    #   A4 的临时库是**从快照自举出来的** ⇒ 快照里塞个幽灵 agent，临时库里也有它
    #   ⇒ `库 >= 快照` 恒成立 ⇒ 光靠 A4 抓不到"快照本身被污染/被手改"。
    #   判据要区分两种情形（否则老库首次自举前会被误判）：
    #     · 库几乎为空（agents 数 < 快照的 50%）⇒ 尚未自举，只 INFO；
    #     · 库已有大部分资产 ⇒ 已经同步过，此时快照多出来的就是**脏数据或手改** ⇒ FAIL。
    _snap_agents = len((_snap.get("tables") or {}).get("agents") or [])
    try:
        _live_agents = _conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
    except Exception:
        _live_agents = 0
    _bootstrapped = _snap_agents and _live_agents >= max(1, int(_snap_agents * 0.5))
    if _d["missing_in_db"]:
        if _bootstrapped:
            check("A2b 快照里没有「库中不存在」的幽灵资产", False,
                  "快照有、库无（已同步过的库不该出现，可能是手改快照/库被删）: %s"
                  % _d["missing_in_db"][:8])
        else:
            check("A2b 快照里没有「库中不存在」的幽灵资产", True,
                  "库尚未自举（%d/%d），只提示" % (_live_agents, _snap_agents))
            print("  [INFO] 快照有、当前库无 %d 个（下次 init_db 会自举补齐）: %s"
                  % (len(_d["missing_in_db"]), _d["missing_in_db"][:6]))
    else:
        check("A2b 快照里没有「库中不存在」的幽灵资产", True, "")

# ── A4：init_db() 真跑一次，产物必须与快照一致 ──────────────────────────
# 这是本门禁的**核心断言**：证明"一条命令能把环境重建到与快照一致"。
# 隔离方式：MBSE_DB_PATH 指向临时目录 ⇒ 绝不碰任何现有库。
_py = sys.executable
_tmpdir = tempfile.mkdtemp(prefix="mbse_boot_")
_tmpdb = os.path.join(_tmpdir, "rebuild.db")
_env = dict(os.environ)
_env["MBSE_DB_PATH"] = _tmpdb
_env["PYTHONIOENCODING"] = "utf-8"
_env["PYTHONUTF8"] = "1"
_boot_ok, _boot_out = False, ""
try:
    r = subprocess.run([_py, "-X", "utf8", "-c",
                        "from database import init_db; init_db()"],
                       cwd=_ROOT, capture_output=True, env=_env, timeout=300)
    _boot_ok = (r.returncode == 0)
    _boot_out = (r.stdout or b"").decode("utf-8", "replace")[-600:] + \
                (r.stderr or b"").decode("utf-8", "replace")[-400:]
except Exception as e:
    _boot_out = "%s: %s" % (type(e).__name__, e)

check("A4 `init_db()` 能在空库上跑通（一条命令重建环境）", _boot_ok,
      "rc!=0 或异常；末尾输出=%r" % _boot_out[-300:])

if _boot_ok and os.path.isfile(_tmpdb):
    _c2 = sqlite3.connect(_tmpdb)
    try:
        for t in SPEC:
            _want = len((_snap.get("tables") or {}).get(t) or [])
            _got = _c2.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
            # 库里的行可以**多于**快照（种子自带的内置项），但不得少于快照
            check("A4b 重建后 %s 覆盖快照（库 %d >= 快照 %d）" % (t, _got, _want),
                  _got >= _want, "库 %d < 快照 %d ⇒ 自举没生效" % (_got, _want))
        # A5：关键列必须存在（schema 漂移防护）
        _cols = {r[1] for r in _c2.execute("PRAGMA table_info(agents)")}
        _need = {"task_domain", "stage_order", "intent_keywords_active"}
        check("A5 重建的 agents 表含流水线三列（task_domain/stage_order/intent_keywords_active）",
              _need <= _cols, "缺 %s" % sorted(_need - _cols))
    finally:
        _c2.close()
    # 关联表也要覆盖
    _c3 = sqlite3.connect(_tmpdb)
    try:
        for rel in ("agent_tools", "agent_team_members"):
            _want = len((_snap.get("relations") or {}).get(rel) or [])
            _got = _c3.execute("SELECT COUNT(*) FROM %s" % rel).fetchone()[0]
            check("A4c 重建后 %s 覆盖快照（%d >= %d）" % (rel, _got, _want), _got >= _want,
                  "库 %d < 快照 %d" % (_got, _want))
    finally:
        _c3.close()

# 清理临时库（本门禁不留垃圾）
try:
    if os.path.isfile(_tmpdb):
        os.remove(_tmpdb)
    for suf in ("-wal", "-shm"):
        if os.path.isfile(_tmpdb + suf):
            os.remove(_tmpdb + suf)
    os.rmdir(_tmpdir)
except Exception:
    pass

# ── A6：自举**不得**覆盖"同名多行"（2026-11-04 真实事故的防复发断言）──────
# 事故经过：`_bootstrap_tables` 第一版用 `UPDATE skills ... WHERE name=?`，
#   而 skills 表**无 name 唯一约束**（真库有 `行业调研` ×5、`智源平台管理` ×3）⇒
#   该 UPDATE 命中全部同名行，把快照那一条的内容写进所有同名行 ⇒
#   5 条不同 skill 变成 5 条副本（实测：唯一内容数 3 → 1）。
#   代码已修为"同名多行一律跳过"(ambiguous)，本断言用**构造库**证明它真的跳过。
try:
    from database.bootstrap_assets import bootstrap_assets as _boot2
    _d2 = tempfile.mkdtemp(prefix="mbse_amb_")
    _amb = os.path.join(_d2, "amb.db")
    _ac = sqlite3.connect(_amb)
    _ac.execute("CREATE TABLE agents (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, "
                "display_name TEXT, system_prompt TEXT, capabilities TEXT, hil_level TEXT, "
                "agent_role TEXT, task_domain TEXT, stage_order INTEGER, "
                "input_schema TEXT, output_schema TEXT, builtin INTEGER DEFAULT 0)")
    # skills 表刻意**不加 name 唯一约束**，复刻真库形态
    _ac.execute("CREATE TABLE skills (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, "
                "skill_type TEXT, description TEXT, content TEXT, source TEXT, "
                "trigger TEXT, status TEXT, enabled INTEGER DEFAULT 1)")
    _ac.execute("CREATE TABLE tools (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, "
                "description TEXT, status TEXT)")
    _ac.execute("CREATE TABLE agent_tools (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "agent_id INTEGER, tool_type TEXT, tool_name TEXT, enabled INTEGER DEFAULT 1)")
    _ac.execute("CREATE TABLE agent_team_members (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "main_agent_id INTEGER, sub_agent_id INTEGER, enabled INTEGER DEFAULT 1)")
    # 同名两行，内容不同（这就是被事故毁掉的形态）
    _ac.execute("INSERT INTO skills (name, content, source, status) VALUES "
                "('__AMB__','内容甲','manual','published')")
    _ac.execute("INSERT INTO skills (name, content, source, status) VALUES "
                "('__AMB__','内容乙','ai_improve','published')")
    _ac.commit()
    _ac.close()

    # 造一份只含"__AMB__ 内容甲"的快照
    _snap2 = os.path.join(_d2, "snap.json")
    with open(_snap2, "w", encoding="utf-8") as _f:
        json.dump({"schema_version": 1,
                   "tables": {"skills": [{"name": "__AMB__", "skill_type": "prompt",
                                          "description": "", "content": "内容甲",
                                          "source": "manual", "trigger": "",
                                          "enabled": 1}],
                              "agents": [], "tools": []},
                   "relations": {"agent_tools": [], "agent_team_members": []}}, _f,
                  ensure_ascii=False)
    _ac = sqlite3.connect(_amb)
    _r2 = _boot2(_ac, path=_snap2)
    _contents = sorted(r[0] for r in _ac.execute(
        "SELECT content FROM skills WHERE name='__AMB__'"))
    _ac.close()
    check("A6 自举遇到「同名多行」必须跳过（不得把多行覆盖成同一份）",
          _r2.get("ambiguous", 0) >= 1 and _contents == ["内容乙", "内容甲"],
          "ambiguous=%s 内容=%s（若只剩一份 = 复发了 2026-11-04 的覆盖事故）"
          % (_r2.get("ambiguous"), _contents))
    try:
        os.remove(_amb)
        os.remove(_snap2)
        os.rmdir(_d2)
    except Exception:
        pass
except Exception as _e_a6:
    check("A6 自举遇到「同名多行」必须跳过（不得把多行覆盖成同一份）", False,
          "%s: %s" % (type(_e_a6).__name__, str(_e_a6)[:120]))

if _conn is not None:
    try:
        _conn.close()
    except Exception:
        pass

print()
print("=" * 68)
print("PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
for _n, _d in FAIL:
    print("  FAIL:", _n, "<-", _d)
if FAIL:
    print("\n修复动作：python tools/export_asset_snapshot.py --apply  （然后提交快照）")
print("=" * 68)
raise SystemExit(1 if FAIL else 0)
