# -*- coding: utf-8 -*-
"""多工程场景自检：P0-1 工具侧工程反查 / P0-2 会话级归属（2026-09-28）。

背景（只读调研结论：**单工程串行闭环、多工程并发不闭环**）：
  ① 工具侧工程无法反查本地项目 —— `projects.tool_binding` **只写不查**，从建模工具
     （智源 / MagicDraw）打开 AI 平台时前端也不读 URL 参数 → 上下文接不住；
  ② 「当前工程」= `settings.default_project_id` **全局单行**，不分用户/标签页/会话
     → 多标签并发时后切换者覆盖前者，前者新建的会话**静默错归属**；
  ③ 写回链路 `sysml_versions → conversations → projects` 靠 join 推导，
     会话无归属时 join 不到 → 工具闸**静默跳过**（与「未绑定报 TOOL_NOT_READY」自相矛盾）。

本脚本**夹具驱动**（临时库 + 猴补 `core.deps.get_db`），断言对象是**机制**而非真库恰好配了什么：
    .venv/Scripts/python.exe -X utf8 tools/verify/verify_multi_project_scope.py

⭐ 用户口径（2026-09-28）：**允许不关联工程的会话存在**（知识检索/问答等不读写工程）；
   **一旦基于工程的会话，则需要收敛进项目中**。故「未归属」是合法状态，
   而「写回建模工具」这类工程操作在无归属时必须**明确报错**，不能静默放行。

变异自证见 `verify_multi_project_scope_mutate.py`（M1~M3 必须全部被抓住）。
"""

# ── CI 豁免（2026-10-05 标注，理由已实测）──────────────────
# ── 已接进 CI（2026-10-05 第二轮第 2 项收尾）──────────
# 修复要点：根因是**门禁口径**：原判据要求「顶部新建任务带本页当前工程」，与当前产品口径相反。
#   三处独立证据：① 后端 2026-09-20 起 project_id 刻意不回落（注释明说"直接落空串 =
#   无工程会话"）；② 前端 54e8493「新建任务归属口径」把 newTask() 改成可选参数、仅
#   项目下发起才带 pid，顶部三处入口都调无参版；③ 存在「本任务未关联工程 · 归入」补偿入口。
#   同时补一处真产品缺陷：无 pid 时原本只提示"草稿模式"，未告知不会归属任何工程。
# 双环境实测：生产库真跑 + 全新干净库（无样本处干净 SKIP）均 rc=0。
# 注：本门禁原硬编码 `ROOT/"mbse.db"`（本项目第 7/8 处）⇒ 已改读 MBSE_DB_PATH，
#   否则 CI 干净库上根本没有该文件，会直接崩。

import json
import os
import re
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

_n_pass = 0
_n_fail = 0
_FAILS = []


def check(name, ok, detail=""):
    global _n_pass, _n_fail
    if ok:
        _n_pass += 1
        print("  PASS  " + name + ("  | " + detail if detail else ""))
    else:
        _n_fail += 1
        _FAILS.append(name)
        print("  FAIL  " + name + ("  | " + detail if detail else ""))


def hr(t):
    print()
    print("=" * 90)
    print(t)
    print("=" * 90)


# ── 夹具 ─────────────────────────────────────────────────────────────────────
_UNSET = object()
_FX_DDL = [
    # ⚠️ projects 必须带 created_at：resolve_project_by_tool 的查询按它排序（夹具缺列 →
    #    反查整体失败，而症状与「没绑定」一样 —— 手抄 DDL 的漂移就是这么骗人的）。
    "CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT, code TEXT, "
    "tool_binding TEXT DEFAULT '', created_at TEXT)",
    "CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)",
    "CREATE TABLE conversations (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, intent TEXT, "
    "user_id INTEGER, project_id TEXT DEFAULT '', created_at TEXT, updated_at TEXT)",
    # P1-1（2026-09-28）：闸门改读 `sysml_versions.project_id`（定格优先），夹具必须带这列。
    # 教训复现：手抄 DDL 与真实 schema 漂移 → 闸门 SQL 抛 `no such column: v.project_id`，
    # **崩在夹具初始化**而不是被测断言（看起来像被测逻辑坏了，极难定位）。
    # 故此处同时补列 + 在夹具建好后加「契约断言」（见下）。
    "CREATE TABLE sysml_versions (id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id INTEGER, "
    "project_id TEXT DEFAULT '', status TEXT DEFAULT '')",
    "CREATE TABLE audit_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, user_name TEXT, event_type TEXT, "
    "detail TEXT, result TEXT, branch TEXT)",
]


def _fx_db(projects=(), default_project=_UNSET, convs=()):
    """临时夹具库。projects: [(id, name, tool_binding_json)]；convs: [(id, project_id)]。"""
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    for d in _FX_DDL:
        c.execute(d)
    # 夹具契约断言：被测代码依赖的列必须在夹具里存在。
    # 手抄 DDL 一旦与真实 schema 漂移，症状是「崩在初始化」而非「断言失败」——
    # 看着像被测逻辑坏了，实际是夹具缺列（2026-09-28 P1 实测复现）。
    _need = {"sysml_versions": "project_id", "conversations": "project_id", "projects": "tool_binding"}
    for _t, _col in _need.items():
        _cols = [r[1] for r in c.execute("PRAGMA table_info(%s)" % _t).fetchall()]
        assert _col in _cols, "夹具缺列：%s.%s（DDL 与生产 schema 漂移）→ 现有列 %s" % (_t, _col, _cols)
    for pid, name, tb in projects:
        c.execute("INSERT INTO projects (id,name,code,tool_binding) VALUES (?,?,?,?)",
                  (pid, name, "C-" + pid, tb))
    if default_project is not _UNSET:
        c.execute("INSERT INTO settings (key,value) VALUES ('default_project_id',?)",
                  (default_project,))
    for cid, cpid in convs:
        c.execute("INSERT INTO conversations (id,title,intent,user_id,project_id) "
                  "VALUES (?,?,?,?,?)", (cid, "t", "chat", 1, cpid))
        c.execute("INSERT INTO sysml_versions (id,conversation_id) VALUES (?,?)", (cid, cid))
    c.commit()
    c.close()
    return path


class _Probe:
    """SQL 探针连接：记录所有 SQL，供「连查询都不该发生」这类非空转断言使用。"""

    def __init__(self, c):
        self._c = c
        self.sql = []

    def execute(self, q, *a):
        self.sql.append(str(q))
        return self._c.execute(q, *a)

    def __getattr__(self, n):
        return getattr(self._c, n)


def _conn(path, probe=False):
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    c.isolation_level = None
    return _Probe(c) if probe else c


# ─────────────────────────────────────────────────────────────────────────────
hr("[1] P0-1 工具侧工程反查：parse_tool_binding / resolve_project_by_tool")

from repositories.project_repo import parse_tool_binding, resolve_project_by_tool

check("parse_tool_binding：空串 / None → {}（未绑定，不抛异常）",
      parse_tool_binding("") == {} and parse_tool_binding(None) == {})
check("parse_tool_binding：非法 JSON / 非 dict → {}（脏数据不炸链路）",
      parse_tool_binding("{oops") == {} and parse_tool_binding("[1,2]") == {})
check("parse_tool_binding：正常 JSON 原样解析",
      parse_tool_binding(json.dumps({"tool": "zhiyuan", "ref": "1,0"}))["ref"] == "1,0")

_P = [("p-a", "工程A", json.dumps({"tool": "zhiyuan", "ref": "1,0"})),
      ("p-b", "工程B", json.dumps({"tool": "zhiyuan", "ref": "2,0"})),
      ("p-c", "工程C", json.dumps({"tool": "magicdraw", "ref": "TWC-9"})),
      ("p-d", "工程D", "")]
_db = _fx_db(_P)
_c = _conn(_db)

hit, cands = resolve_project_by_tool(_c, "zhiyuan", "2,0")
check("★ tool+ref 精确匹配 → 命中对应项目", hit == "p-b", f"got={hit}")

hit2, _ = resolve_project_by_tool(_c, "zhiyuan", " 2,0 ")
check("ref 两端空白不影响命中（strip 归一）", hit2 == "p-b", f"got={hit2}")

hit3, _ = resolve_project_by_tool(_c, "ZHIYUAN", "1,0")
check("tool 大小写不敏感（MODELING_TOOLS 以小写为口径）", hit3 == "p-a", f"got={hit3}")

hit4, cands4 = resolve_project_by_tool(_c, "zhiyuan", "9,9")
check("★ 同工具但 ref 不同 → 不命中（相似 ≠ 同一个工程，绝不模糊匹配）", hit4 == "",
      f"got={hit4}")
check("未命中时给出同工具候选（供前端提示「未绑定/绑定了别的工程」）",
      {x["id"] for x in cands4} == {"p-a", "p-b"}, f"got={cands4}")

hit5, cands5 = resolve_project_by_tool(_c, "magicdraw", "TWC-9")
check("magicdraw 通道同样可反查", hit5 == "p-c", f"got={hit5}")

hit6, cands6 = resolve_project_by_tool(_c, "catia", "x")
check("未绑定该工具 → 不命中且候选为空", hit6 == "" and cands6 == [], f"got={hit6},{cands6}")

check("缺 tool 或 ref → 不命中（返回 ('',[])，不猜不兜底）",
      resolve_project_by_tool(_c, "", "1,0") == ("", [])
      and resolve_project_by_tool(_c, "zhiyuan", "") == ("", []))
check("未绑定工具的项目（tool_binding 空）不参与任何匹配",
      "p-d" not in str(resolve_project_by_tool(_c, "zhiyuan", "1,0")[1]))
_c.close()
os.unlink(_db)

# ─────────────────────────────────────────────────────────────────────────────
hr("[2] P0-1 端点 /api/projects/resolve：路由顺序 + 夹具驱动行为")

_src = open(os.path.join(ROOT, "routers", "projects.py"), encoding="utf-8").read()
_i_res = _src.find('@router.get("/api/projects/resolve")')
_i_pid = _src.find('@router.get("/api/projects/{project_id}")')
check("★ resolve 注册在 /api/projects/{project_id} **之前**（否则会被 path 参数吃掉 → 404）",
      0 <= _i_res < _i_pid, f"resolve@{_i_res} vs {{project_id}}@{_i_pid}")

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import core.deps as _deps
    from routers.projects import router as _proj_router

    _db2 = _fx_db(_P)
    _real = _deps.get_db

    def _fake_get_db():
        return _conn(_db2)

    _deps.get_db = _fake_get_db
    try:
        _app = FastAPI()
        _app.include_router(_proj_router)
        _cl = TestClient(_app)
        r1 = _cl.get("/api/projects/resolve?tool=zhiyuan&ref=1,0")
        check("★ 端点按 tool+ref 反查命中（不是被 {project_id} 吃掉的 404）",
              r1.status_code == 200 and r1.json().get("matched") is True
              and r1.json().get("project_id") == "p-a", f"got={r1.status_code} {r1.text[:120]}")
        r2 = _cl.get("/api/projects/resolve?tool=zhiyuan&ref=9,9")
        check("未命中恒 200（未匹配是合法状态）+ reason=ref_mismatch + 候选非空",
              r2.status_code == 200 and r2.json().get("matched") is False
              and r2.json().get("reason") == "ref_mismatch"
              and len(r2.json().get("candidates") or []) == 2, f"got={r2.text[:160]}")
        r3 = _cl.get("/api/projects/resolve?project_id=p-c")
        check("已知 project_id 直通命中（reason=project_id）",
              r3.json().get("matched") is True and r3.json().get("reason") == "project_id")
        r4 = _cl.get("/api/projects/resolve?project_id=nope")
        check("project_id 不存在 → matched=false 且 reason=project_not_found",
              r4.status_code == 200 and r4.json().get("reason") == "project_not_found")
        r5 = _cl.get("/api/projects/resolve?tool=catia&ref=1")
        check("★ 不支持的工具 → 400（与 build_tool_binding 口径一致）",
              r5.status_code == 400, f"got={r5.status_code}")
        r6 = _cl.get("/api/projects/resolve")
        check("缺参数 → 200 + reason=missing_param（不猜、不落到默认项目）",
              r6.json().get("reason") == "missing_param")
    finally:
        _deps.get_db = _real
    os.unlink(_db2)
except ImportError as e:
    check("端点级断言需要 fastapi TestClient", False, f"ImportError: {e}")

# ─────────────────────────────────────────────────────────────────────────────
hr("[3] P0-2 会话归属显式化：不再回落全局默认（多标签并发不串归属）")

from repositories.conversation_repo import ConversationRepo

_db3 = _fx_db(_P, default_project="p-b")     # 平台默认工程 = p-b
_c3 = _conn(_db3)

_cid_none = ConversationRepo(_c3).create_conversation("未指定归属", "chat", 1)
_v_none = _c3.execute("SELECT project_id FROM conversations WHERE id=?", (_cid_none,)).fetchone()[0]
check("★ 未显式给 project_id → 落库为空串（**不回落** settings.default_project_id）",
      _v_none == "", f"got={_v_none!r}（平台默认=p-b，若回落即为串归属）")

_cid_a = ConversationRepo(_c3).create_conversation("页A任务", "chat", 1, project_id="p-a")
_v_a = _c3.execute("SELECT project_id FROM conversations WHERE id=?", (_cid_a,)).fetchone()[0]
check("显式给 project_id → 归属该项目（与平台默认无关）", _v_a == "p-a", f"got={_v_a}")

# 模拟「另一标签页把平台默认切成 p-c」后，本页（页A）再建任务
_c3.execute("UPDATE settings SET value='p-c' WHERE key='default_project_id'")
_cid_a2 = ConversationRepo(_c3).create_conversation("页A第二个任务", "chat", 1, project_id="p-a")
_v_a2 = _c3.execute("SELECT project_id FROM conversations WHERE id=?", (_cid_a2,)).fetchone()[0]
check("★ 平台默认被别的标签页改掉后，本页新建任务仍归本页工程（并发不串）",
      _v_a2 == "p-a", f"got={_v_a2}")
_c3.close()

# 探针级判据（非空转）：create_conversation 连 settings 都不该查
_db3b = _fx_db(_P, default_project="p-b")
_c3b = _conn(_db3b, probe=True)
ConversationRepo(_c3b).create_conversation("探针", "chat", 1)
_sqls = " || ".join(_c3b.sql)
check("★ 探针：create_conversation 全程未查询 settings（归属不由全局配置决定）",
      "settings" not in _sqls.lower(), f"sql={_sqls[:200]}")
_c3b.close()
os.unlink(_db3b)

# 收敛入口：无工程会话 → 归入项目
_db4 = _fx_db(_P, default_project="p-b", convs=[(11, "")])
_c4 = _conn(_db4)
check("收敛：set_conversation_project 把无归属会话归入 p-a",
      ConversationRepo(_c4).set_conversation_project(11, "p-a") is True
      and _c4.execute("SELECT project_id FROM conversations WHERE id=11").fetchone()[0] == "p-a")
check("收敛：传空串 = 解除归属（回到未分组任务）",
      ConversationRepo(_c4).set_conversation_project(11, "")
      and _c4.execute("SELECT project_id FROM conversations WHERE id=11").fetchone()[0] == "")
check("收敛：会话不存在 → False（路由层转 404）",
      ConversationRepo(_c4).set_conversation_project(999, "p-a") is False)
_c4.close()
os.unlink(_db4)
os.unlink(_db3)

# ─────────────────────────────────────────────────────────────────────────────
hr("[4] 工程操作闸：无归属会话写回建模工具必须**明确报错**（不静默跳过）")

from routers.sysml_versions import resolve_push_tool_gate

_db5 = _fx_db(_P, convs=[(21, ""),                       # 无归属
                         (22, "p-c"),                    # magicdraw
                         (23, "p-a"),                    # zhiyuan
                         (24, "p-d"),                    # 有归属但未绑定工具
                         (25, "ghost-project")])         # 孤儿归属（项目已删）
_c5 = _conn(_db5)

g0 = resolve_push_tool_gate(_c5, 999)
check("版本不存在 → VERSION_NOT_FOUND", g0["code"] == "VERSION_NOT_FOUND" and not g0["ok"])

g1 = resolve_push_tool_gate(_c5, 21)
check("★ 会话未关联工程 → PROJECT_NOT_BOUND（此前 join 不到 → 静默放行）",
      (not g1["ok"]) and g1["code"] == "PROJECT_NOT_BOUND", f"got={g1}")

g2 = resolve_push_tool_gate(_c5, 22)
check("归属工程绑定 magicdraw → TOOL_NOT_READY（原语义保留）",
      (not g2["ok"]) and g2["code"] == "TOOL_NOT_READY" and g2["tool"] == "magicdraw", f"got={g2}")

g3 = resolve_push_tool_gate(_c5, 23)
check("归属工程绑定 zhiyuan → 放行", g3["ok"] is True, f"got={g3}")

g4 = resolve_push_tool_gate(_c5, 24)
check("归属工程未绑定工具 → 放行（既有行为：走 vc 手填，不阻断）", g4["ok"] is True, f"got={g4}")

g5 = resolve_push_tool_gate(_c5, 25)
check("★ 孤儿归属（指向已删项目）→ PROJECT_NOT_BOUND（不能等价于已绑定）",
      (not g5["ok"]) and g5["code"] == "PROJECT_NOT_BOUND", f"got={g5}")
_c5.close()
os.unlink(_db5)

# ─────────────────────────────────────────────────────────────────────────────
hr("[5] 前端接线（源码级：URL 入口 / 页面级当前工程 / 收敛入口）")

_js41 = open(os.path.join(ROOT, "static", "js", "mods", "41-projects.js"), encoding="utf-8").read()
_js03 = open(os.path.join(ROOT, "static", "js", "mods", "03-chat.js"), encoding="utf-8").read()
_js13 = open(os.path.join(ROOT, "static", "js", "mods", "13-reports.js"), encoding="utf-8").read()

check("P0-1 前端读 URL 参数（location.search）→ 反查工程",
      "location.search" in _js41 and "/api/projects/resolve" in _js41)
check("P0-1 首屏先反查再渲染项目组（工具带进来的上下文不被平台默认覆盖）",
      "initGnavProjects" in _js41 and re.search(r"async function initGnavProjects\(\)\s*\{\s*await applyProjectFromUrl",
                                                _js41) is not None)
check("P0-2 页面级当前工程：切换工程时先落本页状态",
      "function setCurProjectId" in _js41
      and re.search(r"await api\('/api/projects/default'.*?\n\s*setCurProjectId\(pid\)", _js41, re.S) is not None)
check("★ P0-2 平台默认值只在**本页未初始化**时用作初值（其它标签页切换不影响本页）",
      _js41.count("window._curProjectId === undefined") >= 1
      and "window._curProjectId === undefined" in _js13)
# ⚠️ 2026-10-05 重新定性：原判据要求「顶部新建任务带本页当前工程」，与**当前产品口径相反**。
#   证据链（三处独立一致，不是漏改）：
#   ① 后端 2026-09-20 起 project_id **刻意不回落**（repositories/conversation_repo.py:33-37
#      注释明说"直接落空串 = 无工程会话"，不再用 settings.default_project_id）；
#   ② 前端 2026-09-29 提交 54e8493「新建任务**归属口径**」把 `newTask()` 改成
#      `newTask(projectId, projectName)`，**只有从项目下发起才带 pid**；
#      顶部/空态/命令栏三处入口（index.html:42、03-chat.js:91、37-kbar.js:39）都调无参版；
#   ③ 产品另有「本任务未关联工程 · 归入」补偿入口，允许先无工程、后续挂上。
#   ⇒ 判据改为验证**当前口径的一致性**（而不是已被推翻的旧口径），
#     并补一条「无工程时 UI 必须明确告知」——原实现在这点上确实有缺口（只说"草稿模式"，
#     用户会误以为会归入某工程），已修。
check("P0-2 顶部新建任务不强制归属（当前口径：仅从项目下发起才带工程）",
      "_pendingProjectId" in _js03
      and "newTask(pid," in _js41
      and "curProjectId()" not in _js03,
      "_pendingProjectId=%s, 41-projects 带参=%s, 03-chat 是否回落=%s"
      % ("_pendingProjectId" in _js03, "newTask(pid," in _js41, "curProjectId()" in _js03))
check("P0-2b 无工程时 UI **明确告知**（不能让用户误以为会归入某工程）",
      "不归属任何工程" in _js03 and "未关联工程" in _js03)
check("收敛入口：顶栏给「本任务未关联工程 · 归入」+ 调 /api/conversations/{id}/project",
      "convAssignCurrent" in _js13 and "assignConvToProject" in _js41
      and "/project`" in _js41)
check("无工程会话是合法状态（文案写明知识检索/问答无需工程）",
      ("知识检索" in _js41 or "知识检索" in _js13))

# ─────────────────────────────────────────────────────────────────────────────
print()
print("=" * 90)
print(f"结果：{_n_pass} pass / {_n_fail} fail")
if _FAILS:
    print("失败项：" + "; ".join(_FAILS))
print("=" * 90)
sys.exit(1 if _n_fail else 0)
