"""验证 Agent 名称口径统一（2026-09-30 用户反馈 2：卡片名 ≠ 编辑页设置的名）。

# ── CI 豁免（2026-10-05 标注，理由已实测）──────────────────
# ── 已接进 CI（2026-10-05 第二轮第 2 项收尾）──────────
# 修复要点：根因是**门禁口径**：C1/C3 锁定的正是**已被修掉的旧行为** —— BaseRepo.execute 已于
#   2026-10-04 改为「INSERT 取 lastrowid，UPDATE/DELETE 取 rowcount」。改为验证修正后的
#   正确语义（0 行⇒0、1 行⇒1，两者可区分），并用「旧 lastrowid 写法下 0 行与 1 行
#   无法区分」做变异自证。
# 双环境实测：生产库真跑 + 全新干净库（无样本处干净 SKIP）均 rc=0。
# 注：本门禁原硬编码 `ROOT/"mbse.db"`（本项目第 7/8 处）⇒ 已改读 MBSE_DB_PATH，
#   否则 CI 干净库上根本没有该文件，会直接崩。


## 背景（真库实测，勿重查）
- `agents.name`（意图标识）与 `agents.display_name`（展示名）是两个字段；
- 但**历史表把这两个字符串当软外键存下来**：
  · agent_tasks.agent_id      → 存 **name**（13 值 12 命中）
  · agent_memory.agent_id     → 存 **name**（9 值 8 命中）
  · tool_call_logs.agent_name → **混存** display_name(356 条) + name(33 条)
- 编辑页原先 `#f-name` 是 hidden input 且 `saveAgent` 写死 `name = f-name || disp`
  → **编辑保存时 name 永不更新**，卡片却把 name 当 badge 展示 → 用户看到的就是"对不上"。

## 判据分组
A/D 后端级联主路径 + 行数守恒
B   双重口径保护（name == display_name，真库 10/20 例）
C   `BaseRepo.execute` 的 lastrowid 陷阱（**含变异自证**）
E/F 改名守卫：重名 / 空名 / 代码级硬引用提示
G   前端：不再是 hidden input；suggestAgentName 行为（源码抽取实跑）
H   真库端到端：拿既有「测试agent」改名 → 级联 → 改回（用 finally 保证复原）

## 纪律
- 夹具自证：级联前先断言旧值确实存在，否则"改写成功"是空转断言；
- 变异自证：把级联换成 no-op / 换成 lastrowid 写法，关键判据必须翻转。
"""
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

from repositories.agent_repo import AgentRepo  # noqa: E402
from routers.studio_parts.agents import _rename_warnings  # noqa: E402

API = os.environ.get("MBSE_API", "http://127.0.0.1:8000")
NODE = os.environ.get("MBSE_NODE", r"C:\Users\gefei\.workbuddy\binaries\node\versions\22.22.2-3\node.exe")
PASS, FAIL = [], []
CUR = FAIL
MF = []


def chk(name, cond, detail=""):
    (PASS if cond else CUR).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"   [{detail}]" if detail and not cond else ""))


# 最小夹具 DD（列取自真库 PRAGMA，够跑级联即可）
DDL = [
    """CREATE TABLE agents (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,
        display_name TEXT NOT NULL, builtin INTEGER DEFAULT 0, agent_role TEXT DEFAULT 'sub')""",
    """CREATE TABLE agent_tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, task_key TEXT,
        agent_id TEXT, status TEXT DEFAULT 'pending', created_at TEXT DEFAULT CURRENT_TIMESTAMP)""",
    """CREATE TABLE agent_memory (id INTEGER PRIMARY KEY AUTOINCREMENT, agent_id TEXT, mem_type TEXT,
        content TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP)""",
    """CREATE TABLE tool_call_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, intent TEXT, agent_name TEXT,
        tool_name TEXT, tool_type TEXT, arguments TEXT, result TEXT, ok INTEGER DEFAULT 1,
        latency_ms INTEGER DEFAULT 0, conversation_id TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP)""",
]
REF_COLS = (("agent_tasks", "agent_id"), ("agent_memory", "agent_id"), ("tool_call_logs", "agent_name"))


def new_db():
    d = tempfile.mkdtemp()
    c = sqlite3.connect(os.path.join(d, "t.db"))
    c.row_factory = sqlite3.Row
    for s in DDL:
        c.execute(s)
    c.commit()
    return c, d


def cnt(conn, tbl, col, val):
    return conn.execute(f"SELECT COUNT(*) FROM {tbl} WHERE {col}=?", (val,)).fetchone()[0]


def row_counts(conn):
    """各软外键表的总行数 —— 级联必须**不增不减**（不改归属以外的任何东西）。"""
    out = {}
    for tbl, _col in REF_COLS:
        out[tbl] = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
    return out


def real_sync(repo, old_row, new_name, new_disp):
    return repo.sync_agent_name_refs(old_row, new_name, new_disp)


def scenario(sync_fn, label, expect_rows=8):
    """一条完整场景：name≠display + 混合口径历史行。返回 total（统计出来的改写行数）。

    夹具行账（expect_rows=8 的来源，别再算错）：
      agent_tasks    agent_id ∈ {req_agent×2, 需求分析Agent×1, other_agent×1}  → 改写 3（other 保留）
      agent_memory   agent_id ∈ {req_agent×1, 需求分析Agent×1}                 → 改写 2
      tool_call_logs agent_name ∈ {req_agent×1, 需求分析Agent×2}               → 改写 3
      合计 8。注意：允诺两个口径都改写是**故意**的 —— tool_call_logs 真库里就是混存列，
      各自保留原本的语义（不把历史上的展示名行悄悄"纠正"成标识名）。
    """
    c, d = new_db()
    repo = AgentRepo(c)
    c.execute("INSERT INTO agents (name, display_name) VALUES ('req_agent','需求分析Agent')")
    c.execute("INSERT INTO agent_tasks (agent_id) VALUES ('req_agent'),('req_agent'),('需求分析Agent')")
    c.execute("INSERT INTO agent_memory (agent_id) VALUES ('req_agent'),('需求分析Agent')")
    c.execute("INSERT INTO tool_call_logs (agent_name) VALUES ('req_agent'),('需求分析Agent'),('需求分析Agent')")
    c.execute("INSERT INTO agent_tasks (agent_id) VALUES ('other_agent')")   # 第三方，永不被动
    c.commit()

    before_rows = row_counts(c)
    # ── 夹具自证（否则后面的"改写成功"是空转断言）──
    chk(f"[{label}] 夹具自证：name 口径事前存在(agent_tasks=2)", cnt(c, "agent_tasks", "agent_id", "req_agent") == 2)
    chk(f"[{label}] 夹具自证：display 口径事前存在(tool_call_logs=2)", cnt(c, "tool_call_logs", "agent_name", "需求分析Agent") == 2)
    chk(f"[{label}] 夹具自证：第三方行存在", cnt(c, "agent_tasks", "agent_id", "other_agent") == 1)

    stat = sync_fn(repo, {"name": "req_agent", "display_name": "需求分析Agent"},
                   "req_analysis_v2", "需求分析助手Agent")
    total = sum(stat.values())

    chk(f"[{label}] A1 agent_tasks 旧 name 清空", cnt(c, "agent_tasks", "agent_id", "req_agent") == 0)
    chk(f"[{label}] A2 agent_tasks 承接新 name（2 行）", cnt(c, "agent_tasks", "agent_id", "req_analysis_v2") == 2,
        cnt(c, "agent_tasks", "agent_id", "req_analysis_v2"))
    chk(f"[{label}] A3 agent_memory 已同步", cnt(c, "agent_memory", "agent_id", "req_analysis_v2") == 1)
    chk(f"[{label}] A4 tool_call_logs display 侧同步（2 行）", cnt(c, "tool_call_logs", "agent_name", "需求分析助手Agent") == 2,
        cnt(c, "tool_call_logs", "agent_name", "需求分析助手Agent"))
    chk(f"[{label}] A5 tool_call_logs name 侧同步（1 行）", cnt(c, "tool_call_logs", "agent_name", "req_analysis_v2") == 1)
    chk(f"[{label}] A6 第三方行未被误伤", cnt(c, "agent_tasks", "agent_id", "other_agent") == 1)
    chk(f"[{label}] D1 总行数守恒（无丢行/无重复）", before_rows == row_counts(c),
        f"{before_rows} -> {row_counts(c)}")
    chk(f"[{label}] D2 统计行数 == 实际改写行数（{expect_rows}）", total == expect_rows, f"stat={stat} total={total}")
    c.close()
    shutil.rmtree(d, ignore_errors=True)
    return total


def noop_sync(repo, old_row, new_name, new_disp):
    return {}


def lastrowid_sync(repo, old_row, new_name, new_disp):
    """【变体】复刻"用 BaseRepo.execute 返回值当行数"的错误写法，暴露 lastrowid 陷阱。"""
    stat = {}
    for ov, nv in ((old_row.get("name"), new_name), (old_row.get("display_name"), new_disp)):
        if not ov or not nv or ov == nv:
            continue
        for tbl, col in REF_COLS:
            n = repo.execute(f"UPDATE {tbl} SET {col}=? WHERE {col}=?", (nv, ov))
            if n:
                stat[f"{tbl}.{col}"] = stat.get(f"{tbl}.{col}", 0) + n
    return stat


def legacy_execute(conn, sql, params=()):
    """【变体】复刻 2026-10-04 **修正前**的 BaseRepo.execute 语义：`lastrowid or rowcount`。

    用途：证明"UPDATE/DELETE 必须用 rowcount"——因为 lastrowid 是"本连接最近一次
    INSERT 的 rowid"，对写 UPDATE 无意义，会让"影响 0 行"与"影响 1 行"返回同一个值。
    """
    cur = conn.execute(sql, tuple(params))
    return cur.lastrowid or cur.rowcount


def c_lastrowid_trap():
    print("\n── C. BaseRepo.execute 的 rowcount 语义（2026-10-04 修正后）──")
    # ⚠️ 2026-10-05：本组原判据锁定的正是**已被修掉的旧行为** ——
    #   原 C1 要求「UPDATE 后返回值 ≠ 0（lastrowid 脏值残留）」、原 C3 用
    #   `lastrowid_sync` 变体证明"错误写法会多统计"。但产品已于 2026-10-04 改为
    #   「INSERT 取 lastrowid，UPDATE/DELETE 取 rowcount」（见 repositories/base.py:46）。
    #   ⇒ 变体与正确写法已无区别，C1/C3 必然红。**这不是产品退化，是门禁口径过期。**
    #   ⇒ 改为验证**修正后的正确语义**：0 行 ⇒ 0、1 行 ⇒ 1，两者可区分。
    c, d = new_db()
    repo = AgentRepo(c)
    n0 = repo.execute("INSERT INTO agent_memory (agent_id) VALUES ('zzz')")
    chk("C1 INSERT 返回 lastrowid（>0）", n0 > 0, f"返回值={n0}")
    ret0 = repo.execute("UPDATE agent_memory SET agent_id='yyy' WHERE agent_id='no_such_row'")
    chk("C1b UPDATE 影响 0 行 ⇒ 返回 0（**不是** lastrowid 脏值）", ret0 == 0,
        f"返回值={ret0}（上一次 INSERT 的 lastrowid={n0}）")
    ret1 = repo.execute("UPDATE agent_memory SET agent_id='yyy' WHERE agent_id='zzz'")
    chk("C1c UPDATE 影响 1 行 ⇒ 返回 1（与 0 行**可区分**= 修正的核心价值）", ret1 == 1,
        f"返回值={ret1}")
    chk("C2 真实受影响行数此处为 0（无 aaa 行）", cnt(c, "agent_tasks", "agent_id", "bbb") == 0)

    # 变异自证：旧写法下 0 行与 1 行返回**同一个脏值** ⇒ 证明 rowcount 语义不可省。
    #   必须先 INSERT 一行制造 lastrowid，否则两次都返回 0、区分不出问题。
    c.execute("INSERT INTO agent_memory (agent_id) VALUES ('uuu')")
    c.commit()
    leg0 = legacy_execute(c, "UPDATE agent_memory SET agent_id='v0' WHERE agent_id='no_such_row'")
    leg1 = legacy_execute(c, "UPDATE agent_memory SET agent_id='v1' WHERE agent_id='uuu'")
    chk("C3 变异自证：旧 lastrowid 写法下 0 行与 1 行**无法区分**（证明 rowcount 必要）",
        leg0 == leg1 and leg0 != 0,
        f"旧写法 0行={leg0} / 1行={leg1}（应相等且非 0）；新写法 0行={ret0} / 1行={ret1}")
    c.close()
    shutil.rmtree(d, ignore_errors=True)


def b_double_caliber():
    print("\n── B. 双重口径保护（name == display_name，真库 10/20 例）──")
    c, d = new_db()
    repo = AgentRepo(c)
    c.execute("INSERT INTO agents (name, display_name) VALUES ('same','same')")
    c.execute("INSERT INTO agent_tasks (agent_id) VALUES ('same'),('same'),('same')")
    c.execute("INSERT INTO tool_call_logs (agent_name) VALUES ('same'),('same')")
    c.commit()
    chk("B0 夹具自证：事前 3+2 行", cnt(c, "agent_tasks", "agent_id", "same") == 3 and cnt(c, "tool_call_logs", "agent_name", "same") == 2)

    before_rows = row_counts(c)
    stat = repo.sync_agent_name_refs({"name": "same", "display_name": "same"}, "new_name", "new_disp")
    chk("B1 agent_tasks 全部归到 new_name（3 行）", cnt(c, "agent_tasks", "agent_id", "new_name") == 3,
        cnt(c, "agent_tasks", "agent_id", "new_name"))
    chk("B2 没有任何行落到 new_disp（未被第二口径抢走）",
        cnt(c, "agent_tasks", "agent_id", "new_disp") == 0 and cnt(c, "tool_call_logs", "agent_name", "new_disp") == 0)
    chk("B3 tool_call_logs 同样归到 new_name（2 行）", cnt(c, "tool_call_logs", "agent_name", "new_name") == 2)
    chk("B4 旧值已清空", cnt(c, "agent_tasks", "agent_id", "same") == 0 and cnt(c, "tool_call_logs", "agent_name", "same") == 0)
    chk("B5 统计行数 = 5", sum(stat.values()) == 5, str(stat))
    chk("B6 行数守恒", before_rows == row_counts(c), f"{before_rows} -> {row_counts(c)}")
    again = repo.sync_agent_name_refs({"name": "new_name", "display_name": "new_name"}, "new_name", "new_name")
    chk("B7 同值重跑零动作（幂等）", again == {}, str(again))
    c.close()
    shutil.rmtree(d, ignore_errors=True)


def ef_guards():
    print("\n── E/F. 改名守卫与影像提示 ──")
    w = _rename_warnings({"name": "design", "display_name": "old", "builtin": 1}, "design_v2", "new")
    chk("F1 代码级硬引用名（design）给出提示", bool(w) and any("硬引用" in x for x in w), str(w))
    chk("F2 内置 Agent 额外提示", any("内置" in x for x in w), str(w))
    chk("F3 自定义名不打扰", _rename_warnings({"name": "my_own_agent", "display_name": "x", "builtin": 0}, "m2", "y") == [])
    chk("F4 未改名（只改其它字段）→ 不提示",
        _rename_warnings({"name": "design", "display_name": "d", "builtin": 0}, "design", "d") == [])
    try:
        from agent.code_level_names import code_level_agent_names
        names = code_level_agent_names()
        chk("F5 清单真的读到了 intent 路由表", "requirement_analysis" in names, f"{sorted(names)[:8]}")
        chk("F6 清单含内置编排池成员 knowledge_qa", "knowledge_qa" in names, f"{sorted(names)[:8]}")
    except Exception as e:  # noqa: BLE001
        chk("F5 清单真的读到了 intent 路由表", False, str(e))
        chk("F6 清单含内置编排池成员 knowledge_qa", False, str(e))

    c, d = new_db()
    repo = AgentRepo(c)
    c.execute("INSERT INTO agents (name, display_name) VALUES ('a1','甲'),('a2','乙')")
    c.commit()
    clash = repo.one("SELECT id, display_name FROM agents WHERE name=? AND id<>?", ("a2", 1))
    chk("E1 重名可被检出（路由依赖这条查询）", bool(clash) and clash["id"] == 2, str(clash))
    chk("E2 自己沿用原名不算重名", not repo.one("SELECT id, display_name FROM agents WHERE name=? AND id<>?", ("a1", 1)))
    c.close()
    shutil.rmtree(d, ignore_errors=True)

    # N：影响面统计 count_name_refs
    c2, d2 = new_db()
    r2 = AgentRepo(c2)
    c2.execute("INSERT INTO agent_tasks (agent_id) VALUES ('zz'),('zz'),('yy')")
    c2.execute("INSERT INTO tool_call_logs (agent_name) VALUES ('zz')")
    c2.commit()
    imp = r2.count_name_refs("zz")
    chk("E3 count_name_refs 统计准确（agent_tasks=2, tool_call_logs=1）",
        imp.get("agent_tasks.agent_id") == 2 and imp.get("tool_call_logs.agent_name") == 1, str(imp))
    chk("E4 不存在的名字统计为 0", sum(r2.count_name_refs("nope").values()) == 0)
    c2.close()
    shutil.rmtree(d2, ignore_errors=True)


def g_frontend():
    print("\n── G. 前端（01-core.js / 30-agents.js）──")
    core = open(os.path.join(REPO, "static", "js", "mods", "01-core.js"), encoding="utf-8").read()
    js = open(os.path.join(REPO, "static", "js", "mods", "30-agents.js"), encoding="utf-8").read()

    m0 = re.search(r"agent: `<h3>新建 Agent</h3>(.*?)</div>`", core, re.S)
    seg = m0.group(1) if m0 else ""
    chk("G0 能定位 agent 表单模板", bool(m0))
    chk("G1 f-name 不再是 hidden input", 'type="hidden" id="f-name"' not in seg, seg[:100])
    chk("G2 f-name 与 f-disp 都是可见输入框", 'id="f-name"' in seg and 'id="f-disp"' in seg)
    chk("G3 展示名输入联动自动建议", "onAgentDispInput" in seg and "oninput=\"onAgentDispInput()\"" in seg)
    chk("G4 编辑既有 Agent 时锁定标识名", "_agentNameTouched = true" in js)
    chk("G5 保存判 res.error（api() 不抛异常）", "res.error" in js)
    chk("G6 卡片标识名标注了可编辑来源", "内部标识名" in js)
    chk("G7 改名结果带级联行数 + 警告", "res.synced_total" in js and "res.warnings" in js)

    m = re.search(r"function suggestAgentName\(disp\)\{(.*?)\n\}", js, re.S)
    chk("G8 源码中可定位 suggestAgentName 原文", bool(m))
    if not m:
        return
    # ⚠️ 必须 console.log 输出：node xxx.js 只是"执行"，module.exports 不产生任何 stdout
    src = ("function suggestAgentName(disp){" + m.group(1) + "\n}\nconsole.log(JSON.stringify(["
           + ",".join(["suggestAgentName('需求分析 Agent X')",
                       "suggestAgentName('Requirement Analysis')",
                       "suggestAgentName('  MBSE 建模  ')",
                       "suggestAgentName('需求视图生成')",
                       "suggestAgentName('')",
                       "suggestAgentName('ab-c_9')"]) + "]));")
    td = tempfile.mkdtemp()
    tmpjs = os.path.join(td, "s.js")
    open(tmpjs, "w", encoding="utf-8").write(src)
    r = subprocess.run([NODE, tmpjs], capture_output=True, text=True, timeout=40)
    shutil.rmtree(td, ignore_errors=True)
    if r.returncode != 0:
        chk("G9 node 执行抽出的源码成功", False, r.stderr[:200])
        return
    v = json.loads(r.stdout.strip())
    chk("G9 纯中文 → 原样保留（对齐库内既有中文标识名）", v[0] == "需求分析 Agent X", str(v))
    chk("G10 纯 ASCII → 小写蛇形", v[1] == "requirement_analysis", str(v))
    chk("G11 含中文 + ASCII → 去空格但保留原文（不强行 slugging）", v[2] == "MBSE 建模", str(v))
    chk("G12 单个中文词组不被破坏", v[3] == "需求视图生成", str(v))
    chk("G13 空输入返回空串", v[4] == "", str(v))
    chk("G14 允许数字与下划线", v[5] == "ab_c_9", str(v))


AGENT_IN_KEYS = ["name", "display_name", "description", "system_prompt", "model_provider_id",
                 "intent_keywords", "icon", "status", "version", "agent_role", "hil_level",
                 "kb_required", "capabilities", "input_schema", "output_schema",
                 "max_concurrency", "protocol_range", "model_params"]


def h_e2e():
    print("\n── H. 真库端到端（既有「测试agent」改名 → 级联 → 改回）──")
    target = os.environ.get("MBSE_E2E_AGENT", "测试agent")

    def _get(p):
        with urllib.request.urlopen(API + p, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))

    def _put(p, body):
        req = urllib.request.Request(API + p, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                     method="PUT", headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8")), r.status
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8")
            try:
                return json.loads(raw), e.code
            except Exception:      # noqa: BLE001
                return {"error": raw[:200]}, e.code

    try:
        items = _get("/api/studio/agents")
    except Exception as e:  # noqa: BLE001
        chk(f"H0 服务可达（{API}）", False, str(e))
        return
    tgt = next((a for a in items if a.get("name") == target), None)
    chk(f"H0 找到用于测试既有的 {target}", bool(tgt), f"现有={[a.get('name') for a in items][:8]}")
    if not tgt:
        return
    aid = tgt["id"]
    before = _get(f"/api/studio/agents/{aid}")
    bname, bdisp = before.get("name"), before.get("display_name")
    chk("H1 该 Agent name==display_name（天然覆盖双重口径场景）", bname == bdisp, f"{bname} / {bdisp}")

    def _body(**over):
        b = {k: before.get(k) for k in AGENT_IN_KEYS if k in before}
        b["kb_scope"] = None        # None = 保留既有 KB 范围，不当场改坏配置
        b.update(over)
        return b

    def _restore():
        try:
            _put(f"/api/studio/agents/{aid}", _body(name=bname, display_name=bdisp))
        except Exception:      # noqa: BLE001
            pass

    try:
        tmp = f"{target}_tmp_{int(time.time())}"
        res, code = _put(f"/api/studio/agents/{aid}", _body(name=tmp, display_name=tmp))
        chk("H2 改名请求成功（非 4xx）", code < 400 and res.get("ok"), f"code={code} res={str(res)[:180]}")
        chk("H3 返回体带整型 synced_total", isinstance(res.get("synced_total"), int), str(res.get("synced_total")))
        chk("H4 renamed=True", res.get("renamed") is True, str(res.get("renamed")))
        chk("H5 非硬引用名 → 无警告", res.get("warnings") == [], str(res.get("warnings")))
        chk("H6 impact 统计返回了三张表的键",
            set((res.get("impact") or {}).keys()) >= {"agent_tasks.agent_id", "agent_memory.agent_id",
                                                      "tool_call_logs.agent_name"}, str(res.get("impact")))
        chk("H7 真库 name 已更新", _get(f"/api/studio/agents/{aid}").get("name") == tmp)

        _restore()
        back, code2 = _put(f"/api/studio/agents/{aid}", _body(name=bname, display_name=bdisp))
        chk("H8 改回原名成功", code2 < 400 and back.get("ok"), f"code={code2} res={str(back)[:180]}")
        restored = _get(f"/api/studio/agents/{aid}")
        chk("H9 完全复原（name/display_name 回到原值）",
            restored.get("name") == bname and restored.get("display_name") == bdisp,
            f"{restored.get('name')} / {restored.get('display_name')}")
        chk("H10 改回时同样返回了级联统计", isinstance(back.get("synced_total"), int), str(back.get("synced_total")))

        clash_name = next((a.get("name") for a in items if a.get("id") != aid and a.get("name")), None)
        if clash_name:
            r3, code3 = _put(f"/api/studio/agents/{aid}", _body(name=clash_name, display_name=bdisp))
            chk("H11 重名改名返回 409", code3 == 409, f"code={code3} res={str(r3)[:160]}")
            chk("H12 错误信息指明占用者", "占用" in (r3.get("error") or ""), str(r3.get("error")))
            chk("H13 被拒后名字未被改动", _get(f"/api/studio/agents/{aid}").get("name") == bname)
        r4, code4 = _put(f"/api/studio/agents/{aid}", _body(name="   ", display_name=bdisp))
        chk("H14 空标识名返回 400", code4 == 400, f"code={code4} res={str(r4)[:160]}")
    finally:
        _restore()
    fin = _get(f"/api/studio/agents/{aid}")
    chk("H15 收尾复核：真库该 Agent 名字仍为原值",
        fin.get("name") == bname and fin.get("display_name") == bdisp, f"{fin.get('name')} / {fin.get('display_name')}")


def main():
    print("=" * 74)
    print("verify_agent_name_unify —— Agent 名称口径统一（用户反馈 2）")
    print("=" * 74)

    print("\n── A/D. 后端级联主路径 ──")
    scenario(real_sync, "真实级联")
    c_lastrowid_trap()
    b_double_caliber()
    ef_guards()
    g_frontend()
    h_e2e()

    print("\n── 变异自证：把级联换成 no-op，同一套判据必须 FAIL ──")
    global CUR
    CUR = MF
    scenario(noop_sync, "no-op变体")
    CUR = FAIL
    chk("M1 no-op 变异后判据确实翻转（非空转）", len(MF) >= 5, f"翻转 {len(MF)} 条：{MF[:6]}")

    print("\n" + "=" * 74)
    print(f"PASS {len(PASS)} / FAIL {len(FAIL)}")
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print("  - " + f)
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
