# -*- coding: utf-8 -*-
"""P1（多工程）自检：冗余列落库 + 写入定格 + 归属闸门口径（2026-09-28）。

覆盖用户拍板的两条：
  · P1-1 冗余列落库：sysml_versions / artifacts 写入时**定格**所属工程；
  · P0-3 存量不动：历史行**不回填**（宁可留空走会话兜底，也不按当前归属倒推历史）。

夹具纪律（踩过的坑，别再犯）：
  · 列一律由**生产迁移** `_migrate_columns` 生成，不手抄 DDL —— 手抄会与真实 schema 漂移，
    而缺列在生产代码里往往表现为"静默没生效"，断言会以假象失败，极难定位；
  · 每条断言都要能被**变异脚本**打红（见 verify_p1_project_attribution_mutate.py）。

命令：./.venv/Scripts/python.exe -X utf8 tools/verify/verify_p1_project_attribution.py
"""
import os
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

PASS = FAIL = 0


def check(name, cond, got=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS  %s" % name)
    else:
        FAIL += 1
        print("  FAIL  %s  | got=%s" % (name, got))


def title(t):
    print("\n" + "=" * 90 + "\n%s\n" % t + "=" * 90)


def _cols(conn, table):
    return [r[1] for r in conn.execute("PRAGMA table_info(%s)" % table).fetchall()]


# ────────────────────────────────────────────────────────────────────
# [1] 迁移：列由生产迁移生成 + 存量不回填
# ────────────────────────────────────────────────────────────────────
title("[1] 迁移：列由生产迁移生成，存量**不回填**")

_db1 = os.path.join(tempfile.gettempdir(), "verify_p1_migr.db")
if os.path.exists(_db1):
    os.remove(_db1)
_c1 = sqlite3.connect(_db1)
# 老库形态：两表都没有 project_id
_c1.execute("CREATE TABLE sysml_versions (id INTEGER PRIMARY KEY, conversation_id INTEGER, project_id_placeholder TEXT)")
_c1.execute("CREATE TABLE artifacts (id INTEGER PRIMARY KEY, conversation_id INTEGER)")
# ⚠️ 夹具必须让「回填」有东西可填：历史行的会话 11 **确实**归属 p-hist。
# 否则"不回填"与"回填但算出空串"长得一模一样 → 断言空转（M6 变异实测抓不到，已修）。
_c1.execute("CREATE TABLE conversations (id INTEGER PRIMARY KEY, project_id TEXT DEFAULT '')")
_c1.execute("INSERT INTO conversations (id, project_id) VALUES (11, 'p-hist')")
_c1.execute("INSERT INTO sysml_versions (id, conversation_id) VALUES (1, 11)")
_c1.execute("INSERT INTO artifacts (id, conversation_id) VALUES (1, 11)")
_c1.commit()
check("夹具自证：历史行的会话确实有归属（否则「不回填」断言会空转）",
      _c1.execute("SELECT project_id FROM conversations WHERE id=11").fetchone()[0] == "p-hist",
      _c1.execute("SELECT project_id FROM conversations WHERE id=11").fetchone()[0])

from database.migrations.columns import _migrate_columns  # noqa: E402
_migrate_columns(_c1)                                     # 生产迁移：列由它生成

check("迁移后 sysml_versions 有 project_id", "project_id" in _cols(_c1, "sysml_versions"),
      _cols(_c1, "sysml_versions"))
check("迁移后 artifacts 有 project_id", "project_id" in _cols(_c1, "artifacts"),
      _cols(_c1, "artifacts"))
check("★ 存量行**未回填**（历史行归属留空，走会话兜底）",
      (_c1.execute("SELECT TRIM(COALESCE(project_id,'')) FROM sysml_versions WHERE id=1").fetchone()[0] == ""
       and _c1.execute("SELECT TRIM(COALESCE(project_id,'')) FROM artifacts WHERE id=1").fetchone()[0] == ""),
      (_c1.execute("SELECT project_id FROM sysml_versions WHERE id=1").fetchone()[0],
       _c1.execute("SELECT project_id FROM artifacts WHERE id=1").fetchone()[0]))
check("迁移幂等（再跑一次不报错）", (_migrate_columns(_c1) or True) and True)
_c1.close()
os.remove(_db1)

# ────────────────────────────────────────────────────────────────────
# [2] conversation_project_id：归属取数口（会话维度，非全局配置）
# ────────────────────────────────────────────────────────────────────
title("[2] conversation_project_id：会话归属取数（空=合法，不回落全局默认）")

from repositories.project_repo import conversation_project_id, resolve_project_id  # noqa: E402

_db2 = os.path.join(tempfile.gettempdir(), "verify_p1_cpid.db")
if os.path.exists(_db2):
    os.remove(_db2)
_c2 = sqlite3.connect(_db2)
_c2.row_factory = sqlite3.Row
_c2.execute("CREATE TABLE conversations (id INTEGER PRIMARY KEY, project_id TEXT DEFAULT '')")
_c2.execute("CREATE TABLE settings (key TEXT, value TEXT)")
_c2.execute("INSERT INTO conversations (id, project_id) VALUES (1,'p-a'), (2,''), (3,' p-b ')")
_c2.execute("INSERT INTO settings VALUES ('default_project_id','p-global')")
_c2.commit()

check("会话已归属 → 返回该工程", conversation_project_id(_c2, 1) == "p-a", conversation_project_id(_c2, 1))
check("★ 会话未归属 → 空串（**不回落** settings.default_project_id）",
      conversation_project_id(_c2, 2) == "", conversation_project_id(_c2, 2))
check("归属值带空格 → trim 后返回", conversation_project_id(_c2, 3) == "p-b", conversation_project_id(_c2, 3))
check("会话不存在 → 空串", conversation_project_id(_c2, 99) == "", conversation_project_id(_c2, 99))
check("★ 与 resolve_project_id 口径不同（前者会话维度、后者配置维度）",
      resolve_project_id(_c2) == "p-global" and conversation_project_id(_c2, 2) == "",
      (resolve_project_id(_c2), conversation_project_id(_c2, 2)))

# ────────────────────────────────────────────────────────────────────
# [3] 产物写入定格（create_artifact 三态）
# ────────────────────────────────────────────────────────────────────
title("[3] artifacts 写入：归属在**写入时定格**（显式 > 会话；无归属=合法空）")

from repositories.artifact_repo import ArtifactRepo  # noqa: E402

_c2.execute("""CREATE TABLE artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id INTEGER, message_id INTEGER DEFAULT 0,
    project_id TEXT DEFAULT '', kind TEXT, title TEXT DEFAULT '', filename TEXT DEFAULT '',
    file_path TEXT DEFAULT '', file_url TEXT DEFAULT '', mime TEXT DEFAULT '', size INTEGER DEFAULT 0,
    preview_type TEXT DEFAULT '', preview_content TEXT DEFAULT '', meta TEXT DEFAULT '{}',
    source TEXT DEFAULT '', created_by TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
_c2.commit()
_ar = ArtifactRepo(_c2)

_a1 = _ar.create_artifact(1, 0, "report", "报告A", "markdown", "x", {})
check("会话归属 p-a → 产物定格 p-a",
      _c2.execute("SELECT project_id FROM artifacts WHERE id=?", (_a1,)).fetchone()[0] == "p-a",
      _c2.execute("SELECT project_id FROM artifacts WHERE id=?", (_a1,)).fetchone()[0])
_a2 = _ar.create_artifact(2, 0, "report", "报告B", "markdown", "x", {})
check("★ 会话无归属 → 产物落空串（**不回落**平台默认 p-global）",
      _c2.execute("SELECT project_id FROM artifacts WHERE id=?", (_a2,)).fetchone()[0] == "",
      _c2.execute("SELECT project_id FROM artifacts WHERE id=?", (_a2,)).fetchone()[0])
_a3 = _ar.create_artifact(2, 0, "report", "报告C", "markdown", "x", {}, project_id="p-explicit")
check("显式传入 → 以显式为准（覆盖会话的空归属）",
      _c2.execute("SELECT project_id FROM artifacts WHERE id=?", (_a3,)).fetchone()[0] == "p-explicit",
      _c2.execute("SELECT project_id FROM artifacts WHERE id=?", (_a3,)).fetchone()[0])
_a4 = _ar.create_artifact(1, 0, "report", "报告D", "markdown", "x", {}, project_id="p-explicit")
check("显式传入优先于会话归属（会话=p-a，显式=p-explicit）",
      _c2.execute("SELECT project_id FROM artifacts WHERE id=?", (_a4,)).fetchone()[0] == "p-explicit",
      _c2.execute("SELECT project_id FROM artifacts WHERE id=?", (_a4,)).fetchone()[0])

# ────────────────────────────────────────────────────────────────────
# [4] 闸门：版本定格优先于会话当前归属（旧行走会话兜底）
# ────────────────────────────────────────────────────────────────────
title("[4] 写回闸门：版本**定格**优先，空则回退会话（存量不动仍可用）")

import json as _json  # noqa: E402
from routers.sysml_versions import resolve_push_tool_gate  # noqa: E402

_c2.execute("CREATE TABLE projects (id TEXT PRIMARY KEY, tool_binding TEXT DEFAULT '')")
_c2.execute("""CREATE TABLE sysml_versions (
    id INTEGER PRIMARY KEY, conversation_id INTEGER DEFAULT 0, project_id TEXT DEFAULT '')""")
_c2.execute("INSERT INTO projects VALUES ('p-old','%s')" % _json.dumps({"tool": "zhiyuan", "ref": "R1"}))
_c2.execute("INSERT INTO projects VALUES ('p-new','%s')" % _json.dumps({"tool": "magicdraw", "ref": "R2"}))
_c2.execute("INSERT INTO sysml_versions (id, conversation_id, project_id) VALUES (1,1,'p-old'), (2,1,'')")
# 会话 1 当前归属设为 p-new（与版本 1 的定格 p-old 不同 —— 这样才能看出"定格优先"）
_c2.execute("UPDATE conversations SET project_id='p-new' WHERE id=1")
_c2.commit()

_g1 = resolve_push_tool_gate(_c2, 1)
check("★ 版本定格 p-old、会话当前归属 p-new → 按**定格**判定（zhiyuan 放行）",
      _g1["ok"] is True and _g1["tool"] == "zhiyuan", _g1)
_g2 = resolve_push_tool_gate(_c2, 2)
check("★ 旧行定格为空 → 回退会话当前归属 p-new（magicdraw → TOOL_NOT_READY）",
      _g2["ok"] is False and _g2["code"] == "TOOL_NOT_READY" and _g2["tool"] == "magicdraw", _g2)

# 定格语义的关键对比：会话改归属后，定格版本**不漂移**、未定格版本**跟着走**
_c2.execute("UPDATE conversations SET project_id='p-old' WHERE id=1")
_c2.commit()
_g1b = resolve_push_tool_gate(_c2, 1)
check("★ 会话改归属后，定格版本判定**不漂移**（仍按 p-old 放行）",
      _g1b["ok"] is True and _g1b["tool"] == "zhiyuan", _g1b)
check("★ 未定格版本**跟着**会话走（旧行语义不变：p-new→magicdraw 变成 p-old→zhiyuan）",
      resolve_push_tool_gate(_c2, 2)["tool"] == "zhiyuan",
      resolve_push_tool_gate(_c2, 2))

# ────────────────────────────────────────────────────────────────────
# [5] 闸门三态回归（P0-2 的既有语义不能被 P1 改坏）
# ────────────────────────────────────────────────────────────────────
title("[5] 闸门三态回归：未归属 / 孤儿归属 / 未绑定工具")

_c2.execute("INSERT INTO conversations (id, project_id) VALUES (9,''), (10,'ghost-project')")
_c2.execute("INSERT INTO sysml_versions (id, conversation_id, project_id) VALUES (9,9,''), (10,10,'')")
_c2.commit()
_g3 = resolve_push_tool_gate(_c2, 9)
check("会话未关联工程 → PROJECT_NOT_BOUND（写回是工程操作，不静默放行）",
      _g3["ok"] is False and _g3["code"] == "PROJECT_NOT_BOUND", _g3)
_g4 = resolve_push_tool_gate(_c2, 10)
check("★ 孤儿归属（工程已不存在）→ PROJECT_NOT_BOUND（不能等价于已绑定）",
      _g4["ok"] is False and _g4["code"] == "PROJECT_NOT_BOUND", _g4)
check("版本不存在 → VERSION_NOT_FOUND",
      resolve_push_tool_gate(_c2, 999)["code"] == "VERSION_NOT_FOUND",
      resolve_push_tool_gate(_c2, 999))

_c2.close()
os.remove(_db2)

# ────────────────────────────────────────────────────────────────────
# [6] 源码级接线：写入点带 project_id / 拉取补传会话 / 报告归属顺序
# ────────────────────────────────────────────────────────────────────
title("[6] 源码级接线（写入点 / 拉取补传 / 报告归属顺序）")


def _src(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


_s_norm = _src("norm_apply.py")
_s_utils = _src("agent/utils.py")
_s_mat = _src("services/artifact_materializer.py")
_s_repo = _src("repositories/artifact_repo.py")
_s_tools = _src("agent/pipeline_parts/tools.py")
_s_rep = _src("routers/reports.py")
_s_schema = _src("database/schema.py")
_s_migr = _src("database/migrations/columns.py")

check("P1-1 norm_apply 版本 INSERT 落 project_id",
      "INSERT INTO sysml_versions" in _s_norm and "project_id" in _s_norm
      and "_conv_project_id(conn, conv_id)" in _s_norm)
check("P1-1 agent/utils 版本 INSERT 落 project_id",
      "INSERT INTO sysml_versions" in _s_utils and "_conversation_project_id(conn, conversation_id)" in _s_utils)
check("P1-1 artifact_materializer 产物 INSERT 落 project_id",
      "INSERT INTO artifacts" in _s_mat and "conversation_project_id(conn" in _s_mat)
check("P1-1 create_artifact 支持显式 project_id 且缺省取会话归属",
      "project_id: str | None = None" in _s_repo and "conversation_project_id(" in _s_repo)
def _ddl_block(src, table):
    """截取某表的 CREATE TABLE 块（不手抄 DDL，直接从生产 schema 取，避免断言与源码漂移）。"""
    i = src.index("CREATE TABLE IF NOT EXISTS " + table)
    # 建表块以 `)"""` 收尾（不能找第一个 `)` —— 注释里就有括号，如 "关联 artifacts(kind=sysml)"）
    return src[i:src.index(')"""', i)]


check("P1-1 schema sysml_versions DDL 含 project_id（新库直接有列）",
      "project_id" in _ddl_block(_s_schema, "sysml_versions"),
      _ddl_block(_s_schema, "sysml_versions")[:200])
check("P1-1 schema artifacts DDL 含 project_id（新库直接有列）",
      "project_id" in _ddl_block(_s_schema, "artifacts"),
      _ddl_block(_s_schema, "artifacts")[:200])
check("P1-1 迁移补列登记两表",
      '_add("sysml_versions", "project_id"' in _s_migr and '_add("artifacts", "project_id"' in _s_migr)
check("P0-3 迁移注释写明「存量不回填」", "存量不回填" in _s_migr)

check("★ P1-2 拉取工具补传 conversation_id（此前漏传 → 静默用平台 default_vc）",
      "zhiyuan_pull_ingest(" in _s_tools and "conversation_id=int(conv_ctx or 0)" in _s_tools)
check("★ P1-2 报告归档归属顺序 = 显式 → 会话 → 平台默认",
      ('_pid = (body.project_id or "").strip()' in _s_rep)
      and ("conversation_project_id(conn, int(body.conversation_id or 0))" in _s_rep)
      and ("_pid = resolve_project_id(conn)" in _s_rep)
      and _s_rep.index("conversation_project_id(conn") < _s_rep.index("_pid = resolve_project_id(conn)"))
check("P1-2 报告归属留痕来源（audit 写 来源：explicit/conversation/default）",
      "来源：" in _s_rep and "_psrc" in _s_rep)
check("★ 图库写入兜底**未**被改动（既有设计：resolve_project_id 是图写入唯一来源）",
      "project_id = resolve_project_id(self.conn)" in _src("repositories/knowledge_repo.py"))

print("\n" + "=" * 90)
print("结果：%d pass / %d fail" % (PASS, FAIL))
print("=" * 90)
sys.exit(1 if FAIL else 0)
