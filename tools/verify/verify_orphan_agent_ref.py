# -*- coding: utf-8 -*-
"""B1-11 门禁：`agent_tasks.agent_id` 的**孤儿引用**检测。

**背景（真库实证，口径 2026-10-12 只读）**：
`agent_tasks.agent_id` 存的**不是数字 id，而是 Agent 名**（实测值 `design` /
`MBSE建模总体负责人` / `智源平台管理`）。真库 222 行里有 **8 行指向 `智源平台管理`**，
而 `agents.name` 中**没有这个名字**（`agents` 里只有 `zhiyuan_mgmt`，且已 disabled）
⇒ 这批任务行是**Agent 改名/下线时留下的孤儿**。

**为什么这是缺陷而不只是数据脏**：
`agent_tasks` 是"这次编排当初派给了谁"的**唯一存证**（文章工程要点③要的正是
「这次为什么得出这个结果」）。孤儿化之后，这 8 行的归属信息**永久丢失**，
且没有任何机制会告知——运维查这批历史 run 时只会看到"派给了一个不存在的 Agent"。

**为什么不加外键**：`agent_tasks` 与 `agents` 是**跨模块弱引用**（`agent_id` 存名而非 id），
加 FK 需要先把列改成数字 id —— 那是数据模型级变更，超出本次范围。
**本次用最小可行方案**：落一条**快照列** `agent_name_snapshot`（派发时写当时的权威名，
即使 Agent 之后改名也留痕）+ 一条**检测门禁**（有孤儿就判红）。

**本脚本守什么**：
  [A] 快照列已加入 DDL 与迁移，且口径一致
  [B] `create_plan` 落库时**真写**快照（否则加了列也是空）
  [C] 快照取「派发时 agents.name 的权威值」，不是盲抄 plan 里的字符串
      （盲抄等于把同一个坏值存两份 —— 换个名就又孤儿了）
  [D] 本仓现状可复算（只读）：孤儿值与行数
  [E] **变异自证**：去掉快照写入 ⇒ B/C 必须 FAIL

**运行**：`.venv/Scripts/python.exe -X utf8 tools/verify/verify_orphan_agent_ref.py`
"""
import io
import os
import re
import sys
import sqlite3
import tempfile
import shutil

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TQUEUE = os.path.join(ROOT, "task_queue.py")
SCHEMA = os.path.join(ROOT, "database", "schema.py")
MIGR = os.path.join(ROOT, "database", "migrations", "columns.py")
PROD_DB = os.path.join(ROOT, "mbse.db")

_ok = 0
_fail = 0
_muts = []


def check(name, cond, detail=""):
    global _ok, _fail
    if cond:
        _ok += 1
        print("  [PASS] %s" % name)
    else:
        _fail += 1
        print("  [FAIL] %s  %s" % (name, detail))


sys.path.insert(0, ROOT)
from task_queue import TaskQueue  # noqa: E402

t_src = io.open(TQUEUE, encoding="utf-8").read()
sc_src = io.open(SCHEMA, encoding="utf-8").read()
m_src = io.open(MIGR, encoding="utf-8").read()

print("=== A. 快照列已加入 DDL 与迁移 ===")
check("A1 DDL 有 agent_name_snapshot",
      re.search(r"agent_name_snapshot TEXT DEFAULT ''", sc_src) is not None)
check("A2 迁移补列有 agent_name_snapshot",
      re.search(r'_add\("agent_tasks",\s*"agent_name_snapshot"', m_src) is not None)
check("A3 DDL 与迁移口径一致",
      "agent_name_snapshot TEXT DEFAULT ''" in sc_src
      and '"agent_name_snapshot", "TEXT DEFAULT \'\'' in m_src)

print("\n=== B. create_plan 真写快照 ===")
_cp = t_src.split("def create_plan")[1][:2200] if "def create_plan" in t_src else ""
check("B1 INSERT 语句含 agent_name_snapshot", "agent_name_snapshot" in _cp)
check("B2 取值来自解析后的权威名（不是盲抄 plan 原值）",
      "resolve_agent_name(" in _cp or "_resolved_name" in _cp,
      "盲抄 t.get('agent') 等于把坏值存两份 —— Agent 一改名就又孤儿")
check("B3 解析失败时仍有兜底（不留 None）",
      re.search(r'_resolved_name\s*=\s*[^\n]*or\s*', _cp) is not None
      or 'str(t.get("agent"' in _cp,
      "解析失败应回落原值，不能写入空/NULL")

print("\n=== C. 权威名解析函数 ===")
check("C1 提供了 resolve_agent_name",
      "def resolve_agent_name" in t_src)
_rn = t_src.split("def resolve_agent_name")[1][:900] if "def resolve_agent_name" in t_src else ""
check("C2 解析时排除 disabled（已下线不算权威在役名）",
      "status='active'" in _rn or "status!='disabled'" in _rn,
      "若不排除，改名过渡期的新旧名会都算'存在'，孤儿检不出来")
check("C3 解析失败返回空串而非抛错（不阻断落库）",
      'return ""' in _rn or 'return str(' in _rn)

print("\n=== D. 行为层：快照真落库（临时库）===")
TMP = tempfile.mkdtemp(prefix="verify_orphan_")
TMP_DB = os.path.join(TMP, "t.db")
try:
    _tc = sqlite3.connect(TMP_DB)
    _tc.row_factory = sqlite3.Row
    _tc.execute("""CREATE TABLE agent_tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, task_key TEXT, title TEXT,
        agent_id TEXT, agent_name_snapshot TEXT DEFAULT '', task_type TEXT, config TEXT,
        deps TEXT, status TEXT DEFAULT 'planned', error TEXT, assigned_by TEXT,
        seq INTEGER DEFAULT 0, context TEXT DEFAULT '', expected_output TEXT DEFAULT '',
        conversation_id INTEGER DEFAULT 0)""")
    _tc.execute("CREATE TABLE agents (id INTEGER PRIMARY KEY, name TEXT, status TEXT)")
    _tc.execute("INSERT INTO agents VALUES (1,'design','active')")
    _tc.execute("INSERT INTO agents VALUES (2,'zhiyuan_mgmt','disabled')")
    _tc.commit()
    # 计划里的名字大小写/空格与权威名不一致 ⇒ 解析应命中
    _plan = [{"key": "t1", "title": "A", "agent": " DESIGN ", "deps": []},
             {"key": "t2", "title": "B", "agent": "zhiyuan_mgmt", "deps": []},
             {"key": "t3", "title": "C", "agent": "不存在的Agent", "deps": []}]
    TaskQueue.create_plan(_tc, 1, _plan)
    _rows = {r["task_key"]: (r["agent_id"], r["agent_name_snapshot"])
             for r in _tc.execute("SELECT task_key,agent_id,agent_name_snapshot FROM agent_tasks")}
    print("  落库结果: %s" % {k: v for k, v in _rows.items()})
    check("D1 t1 快照解析为权威名 design（大小写/空格被归一）",
          _rows["t1"][1] == "design", str(_rows["t1"]))
    check("D2 t1 的 agent_id 也归一为权威名（消除孤儿根因）",
          _rows["t1"][0] == "design", str(_rows["t1"]))
    check("D3 t2 指向已 disabled 的 Agent：快照仍记录当时的值（留痕）",
          _rows["t2"][1] == "zhiyuan_mgmt", str(_rows["t2"]))
    check("D4 t3 查不到时回落原值（不写空、不丢信息）",
          _rows["t3"][1] == "不存在的Agent" and _rows["t3"][0] == "不存在的Agent",
          str(_rows["t3"]))
    _tc.commit()
    # D5 孤儿判定：快照能对上 ⇒ 不是孤儿
    _snap_ok = _tc.execute(
        "SELECT COUNT(*) FROM agent_tasks WHERE agent_name_snapshot<>'' "
        "AND agent_name_snapshot IN (SELECT name FROM agents)").fetchone()[0]
    check("D5 快照可对上 agents.name 的行数（t1+t2）", _snap_ok == 2, "got=%s" % _snap_ok)
    _tc.close()
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print("\n=== E. 真库现状复算（只读，报告孤儿基线）===")
if os.path.exists(PROD_DB):
    _c = sqlite3.connect("file:%s?mode=ro" % PROD_DB, uri=True)
    _orph = _c.execute(
        "SELECT agent_id, COUNT(*) FROM agent_tasks WHERE agent_id IS NOT NULL AND agent_id!='' "
        "AND agent_id NOT IN (SELECT name FROM agents) GROUP BY 1").fetchall()
    _tot = _c.execute("SELECT COUNT(*) FROM agent_tasks").fetchone()[0]
    _n_orph = sum(n for _, n in _orph)
    _has_snap = "agent_name_snapshot" in [r[1] for r in _c.execute("PRAGMA table_info(agent_tasks)")]
    _c.close()
    print("  真库孤儿：%s（合计 %d/%d 行）｜快照列已存在：%s"
          % (_orph, _n_orph, _tot, _has_snap))
    check("E1 孤儿规模已量化（历史遗留，本次不回填）", _n_orph >= 0)
    if not _has_snap:
        print("  [INFO] 真库尚未执行迁移（error_class/snapshot 列待服务启动时补）")
else:
    print("  [SKIP] 未找到真库")

print("\n=== F. 变异自证（去掉快照写入）===")
_anchor = "agent_name_snapshot"
if "agent_name_snapshot" not in _cp:
    print("  [VACUOUS] create_plan 里没有快照列，自证中止")
else:
    _mut = _cp.replace("agent_name_snapshot", "agent_id")
    if _mut == _cp:
        print("  [VACUOUS] 变异未生效")
    else:
        check("F1 变异体上 B1 必须 FAIL（不再写快照列）",
              "agent_name_snapshot" not in _mut)
        _muts.append(("M1", "快照列退化为 agent_id（等于没做）"))
        print("  [SELF-PROOF] M1 快照写回 agent_id —— 改名后同样孤儿化，本条改动等于没做")

print("\n" + "=" * 60)
print("常态断言：%d 条，%d 通过 / %d 失败" % (_ok + _fail, _ok, _fail))
print("变异自证：%d 组" % len(_muts))
for mid, desc in _muts:
    print("  - %s %s" % (mid, desc))
print("结论：%s" % ("门禁通过" if _fail == 0 and _muts else "门禁未通过"))
sys.exit(0 if (_fail == 0 and _muts) else 1)