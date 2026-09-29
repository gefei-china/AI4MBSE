# -*- coding: utf-8 -*-
"""上下文防腐 / 范围自愈 / 质量门禁 的自检脚本（2026-09-19 P0 改动）。

背景（会话 351 取证：「帮我生成电动汽车热管理系统的 sysml V2 代码并进行校验」全链失败）：
  ① 建模上下文按 branch **无条件列名**注入既有实体 → 把上一任务领域素材（巡飞弹/动力分系统）
     塞进本轮，子 Agent 判定「素材与标题不匹配」**拒绝产出代码** → 下游校验无输入 → 链条断裂；
  ② design agent 的 kb_scope.docs 白名单 2 条全不存在 → 实体/分块/文档粗匹配**三路同时归零**
     且完全静默（4887 块 SysML 规范恒不可见）；
  ③ 反思闭环 reflection 判 passed=false/score=62，却不参与 orchestrated_status 聚合 →
     卡片仍显示「正常完成」，**失败被记录成成功**。

本脚本**不依赖 git ref、不依赖服务**（直接 import 模块 + 只读库），随时可跑：
    .venv/Scripts/python.exe tools/verify/verify_context_scope_guard.py
口径提示：[2] 的 count 断言依赖 config 真实值为 count；[3][7][8] 为**夹具驱动**（临时库 + 猴补
database.get_db），断言对象是「机制」而非本次部署恰好配了什么 —— 故把 settings.default_project_id
置空 / 改值**不会**影响通过数（此前的旧实现会，属误报，已于 2026-09-20 修正）。
[8] 的夹具 DDL **故意带一个「毒默认」** `DEFAULT 'poison-project'`（等价于现存库里的
`DEFAULT 'project-satnet-broadband'`）：凡是漏给 project_id 的写入路径，落库值就会是它。
"""
import ast
import json
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

_n_pass = 0
_n_fail = 0


def check(name, ok, detail=""):
    global _n_pass, _n_fail
    if ok:
        _n_pass += 1
        print(f"  PASS  {name}" + (f"  | {detail}" if detail else ""))
    else:
        _n_fail += 1
        print(f"  FAIL  {name}" + (f"  | {detail}" if detail else ""))


def hr(t):
    print()
    print("=" * 90)
    print(t)
    print("=" * 90)


def ro_conn():
    db = os.path.join(ROOT, "mbse.db")
    c = sqlite3.connect("file:" + db.replace("\\", "/") + "?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


# ─────────────────────────────────────────────────────────────────────────────
hr("[1] KB-S 文档白名单自愈：rag.GraphRAG._resolve_scope_docs")

from agent.rag import GraphRAG


def _fx_conn_docs(n=2):
    """夹具：自建临时库 + documents 表（**真迁移生成**）+ n 行夹具文档。

    为什么自建（2026-09-20 CI 首跑教训）：本节断言的是「白名单自愈**机制**」，
    与开发库恰好有哪些文档无关。旧写法从 mbse.db 捞最近 2 篇当夹具 ——
    CI/全新库的 documents 表是**空的** → 「部分失效」用例退化成「全部失效」
    → unfiltered=True → 假失败。断言对象必须是机制，而非当前库恰好配了什么。

    为什么不手抄 DDL（2026-09-23 第二次「夹具 DDL 漂移」教训）：
      旧夹具只建 `(id, filename)` 两列，而 `_resolve_scope_docs` 自 2026-09-21（G2）起
      会按 `lifecycle_status` 过滤（排除已下线文档）。夹具缺列 → SQL 抛
      `no such column: lifecycle_status` → 被方法内 `except Exception` 静默兜成
      「原样返回」→ 4 条断言以「函数根本没生效」的假象失败，
      而唯一使用**真库连接**的那条用例反而通过（这就是定位线索）。
      现改为「最小基表 + 跑**真迁移** `_migrate_document_lifecycle`」：
      生命周期列由生产代码生成，夹具不再手抄；末尾再断言关键列确已就位 ——
      迁移若被移除/改名，夹具当场报错，而不是退化成静默假绿。
      （同类前车之鉴：`entity_versions` 手抄 DDL 漏表 → `no such table`。）
    """
    import tempfile
    from database.migrations import _migrate_document_lifecycle
    d = os.path.join(tempfile.gettempdir(), f"_kb_scope_fixture_{os.getpid()}.db")
    if os.path.exists(d):
        os.remove(d)
    c = sqlite3.connect(d)
    c.row_factory = sqlite3.Row
    # 最小基表（真库 schema.py 的子集：只留迁移与断言真正依赖的列）
    c.executescript("""
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL,
            parse_status TEXT DEFAULT 'pending',
            branch TEXT DEFAULT 'global'
        );
        CREATE TABLE document_chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id INTEGER NOT NULL,
            branch TEXT DEFAULT 'global'
        );
    """)
    _migrate_document_lifecycle(c)          # ← 生命周期列（lifecycle_status 等）由生产迁移补齐
    _cols = {r[1] for r in c.execute("PRAGMA table_info(documents)")}
    assert "lifecycle_status" in _cols, (
        f"夹具契约失守：documents 缺 lifecycle_status（迁移未生效或被改名？）实际列={sorted(_cols)}")
    for i in range(n):
        c.execute("INSERT INTO documents (filename) VALUES (?)", (f"__fixture_doc_{i}.md",))
    c.commit()
    return c, d


conn = ro_conn()
_fx, _fx_path = _fx_conn_docs(2)
try:
    have = [r["filename"] for r in _fx.execute(
        "SELECT filename FROM documents ORDER BY id LIMIT 2")]
    missing = "__definitely_not_exist__.md"

    ok1, w1 = GraphRAG._resolve_scope_docs(_fx, have)
    check("全有效白名单 → 原样返回、无告警", ok1 == have and w1 is None, f"got={ok1}, warn={w1}")

    ok2, w2 = GraphRAG._resolve_scope_docs(_fx, have + [missing])
    check("部分失效 → 剔除失效项并给出告警", ok2 == have and w2 and missing in w2["missing"],
          f"effective={ok2}, missing={w2 and w2['missing']}")
    check("部分失效 → unfiltered=False（未放宽）", w2 and w2["unfiltered"] is False,
          f"unfiltered={w2 and w2['unfiltered']}")

    ok3, w3 = GraphRAG._resolve_scope_docs(_fx, [missing])
    check("★ 全失效 → 放宽为不限文档（绝不静默 0 命中）", ok3 == [] and w3 and w3["unfiltered"] is True,
          f"effective={ok3}, unfiltered={w3 and w3['unfiltered']}")

    ok4, w4 = GraphRAG._resolve_scope_docs(_fx, [missing, missing])
    check("重复项去重（脏数据不再重复计入）", w4 and w4["requested"] == [missing],
          f"requested={w4 and w4['requested']}")

    ok5, w5 = GraphRAG._resolve_scope_docs(_fx, [])
    check("空白名单 → 不做过滤（空列表, None）", ok5 == [] and w5 is None, f"got={ok5}, warn={w5}")

    # 关掉 fallback：失效项不该被剔除（回到改动前行为，仅告警）
    from core import config as _cfg
    _orig_get = _cfg.get

    def _fake_no_fb(sec, key, default=None):
        if (sec, key) == ("kb_scope", "docs_missing_fallback"):
            return False
        return _orig_get(sec, key, default)

    _cfg.get = _fake_no_fb
    try:
        ok6, w6 = GraphRAG._resolve_scope_docs(conn, [missing])
    finally:
        _cfg.get = _orig_get
    check("docs_missing_fallback=False → 保留原白名单（可回退到改动前行为）",
          ok6 == [missing] and w6 and w6["unfiltered"] is False, f"effective={ok6}")
finally:
    conn.close()

# ─────────────────────────────────────────────────────────────────────────────
hr("[2] 建模上下文：既有实体注入形态（memory.MemoryMixin._build_model_context）")

from agent.pipeline_parts.memory import MemoryMixin
from core import config as _cfg2

m = MemoryMixin()
_O = _cfg2.get


def _with_mode(mode, fn):
    def _fake(sec, key, default=None):
        if (sec, key) == ("context", "model_context_entities"):
            return mode
        return _O(sec, key, default)
    _cfg2.get = _fake
    try:
        return fn()
    finally:
        _cfg2.get = _O


_out_count = _with_mode("count", lambda: m._build_model_context("dev", 0, "帮我生成电动汽车热管理系统代码"))
_out_names = _with_mode("names", lambda: m._build_model_context("dev", 0, "帮我生成电动汽车热管理系统代码"))
_out_none = _with_mode("none", lambda: m._build_model_context("dev", 0, "帮我生成电动汽车热管理系统代码"))

check("count（默认）只报数量、不列具体实体名", "既有建模实体：本分支共" in _out_count and "活跃实体：" not in _out_count,
      repr(_out_count[:120]))
check("names 模式仍可列出具体名字（旧行为可回退）", ("活跃实体：" in _out_names) or ("既有建模实体" not in _out_names),
      repr(_out_names[:120]))
check("none 模式不含实体项", "实体" not in _out_none, repr(_out_none[:120]))
check("三种模式都带「适用范围」声明（第二层防御）",
      all("与本" in t and "不符" in t for t in (_out_count, _out_names, _out_none) if t),
      repr(_out_count[:60]))

# 声明必须**不参与预算裁剪**：把预算压到极小，声明仍应完整
def _fake_small(sec, key, default=None):
    if (sec, key) == ("context", "model_context_chars"):
        return 40
    if (sec, key) == ("context", "model_context_entities"):
        return "names"
    return _O(sec, key, default)


_cfg2.get = _fake_small
try:
    _out_small = m._build_model_context("dev", 0, "x")
finally:
    _cfg2.get = _O
check("预算极小（40 字符）时范围声明仍完整保留（不被截掉）",
      "不符" in _out_small and "必须忽略" in _out_small, repr(_out_small[:100]))

# ─────────────────────────────────────────────────────────────────────────────
hr("[3] 项目宪法：注入带项目名 + 适用范围声明（memory.MemoryMixin._build_project_memory）")

# ⚠️ 本段**不得对真实库断言语义**（2026-09-20 实测教训）：原实现直接对 mbse.db 断言
#    「注入块存在」，而清理把 settings.default_project_id 置空后三条断言全挂 —— 那是
#    **配置变更**而非代码回归，属误报（与「通过数随配置变化」是同一类问题）。
#    现改为**夹具驱动**：临时库 + 猴补 database.get_db，断言对象是「取项目 / 注不注入」的
#    **机制**，与本次部署恰好配了什么无关。
import tempfile
import database as _dbmod

_FIX_PID = "proj-fixture"
_UNSET = object()
_SQL_LOG: list = []


def _fixture_db(setting=_UNSET, memories=(), budget=None):
    """建临时库（settings + project_memories）。

    setting=_UNSET 表示**连该 settings 行都不写**（测「未配置」）；setting="" 表示置空。
    """
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT, description TEXT)")
    c.execute("CREATE TABLE project_memories (id INTEGER PRIMARY KEY AUTOINCREMENT, "
              "project_id TEXT, category TEXT, title TEXT, content TEXT, enabled INTEGER DEFAULT 1)")
    if setting is not _UNSET:
        c.execute("INSERT INTO settings (key,value,description) "
                  "VALUES ('default_project_id',?,'')", (setting,))
    if budget is not None:
        c.execute("INSERT INTO settings (key,value,description) "
                  "VALUES ('memory.project_memory_chars',?,'')", (str(budget),))
    for cat, title, content in memories:
        c.execute("INSERT INTO project_memories (project_id,category,title,content,enabled) "
                  "VALUES (?,?,?,?,1)", (_FIX_PID, cat, title, content))
    c.commit()
    c.close()
    return path


def _with_db(path, fn, log=False):
    """令函数内的 `from database import get_db` 指向临时库（每次调用返回新连接，可被其 close）。

    log=True 时用**探针连接**记录所有 SQL —— 用于断言「某分支下**连查询都不发生**」。
    必要性（2026-09-20 变异测试实测）：仅断言返回值会**空转** —— 「settings 行缺失 → 返回 ""」
    在「旧写法回退到硬编码项目」时同样成立（回退去的项目在夹具库里也无记忆，殊途同归），
    故必须断言**没去查任何项目的记忆**，该判据才非空转。
    """
    _orig = _dbmod.get_db
    if log:
        _SQL_LOG.clear()

    class _Spy:
        def __init__(self, c):
            self._c = c

        def execute(self, sql, *a):
            _SQL_LOG.append(sql)
            return self._c.execute(sql, *a)

        def close(self):
            self._c.close()

    def _fake():
        cc = sqlite3.connect(path)
        cc.row_factory = sqlite3.Row
        return _Spy(cc) if log else cc

    _dbmod.get_db = _fake
    try:
        return fn()
    finally:
        _dbmod.get_db = _orig


_MEMS = [("规范", "命名规范", "实体命名必须用大驼峰，禁止拼音缩写。"),
         ("决策", "评审结论", "所有交付物必须附可核引用，否则不予通过。")]

_p_fix = _fixture_db(setting=_FIX_PID, memories=_MEMS)
_out_pm = _with_db(_p_fix, lambda: m._build_project_memory(
    user_input="帮我生成电动汽车热管理系统的 sysml V2 代码"))
check("注入块含项目标识（pid）", _FIX_PID in _out_pm and "Constitution" in _out_pm, repr(_out_pm[:80]))
check("注入块含「仅当属于该项目领域时适用」的范围声明", "仅当本次任务属于该项目领域时适用" in _out_pm,
      repr(_out_pm[:160]))
check("范围声明不参与预算裁剪（截掉了等于没有防御）",
      "仅当本次任务属于该项目领域时适用" in _with_db(
          _fixture_db(setting=_FIX_PID, memories=_MEMS, budget=40),
          lambda: m._build_project_memory(user_input="x" * 100)),
      "预算 40 字符 + 超长 query 下仍完整")
os.unlink(_p_fix)

# ★ 清理（2026-09-20）的核心不变式：默认项目「未配置 / 已置空 / 指向无记忆的项目」→ 一律**不注入**
_nolog = _with_db(_fixture_db(setting=_UNSET),
                  lambda: m._build_project_memory(user_input="x"), log=True)
check("默认项目 settings 行**缺失** → 不注入，且**连记忆查询都不发生**（无硬编码兜底）",
      _nolog == "" and not any("project_memories" in s for s in _SQL_LOG),
      f"out={_nolog!r} sql={_SQL_LOG}")
check("默认项目**置空** → 不注入（置空是「停用」的合法表达，不是错误态）",
      _with_db(_fixture_db(setting=""), lambda: m._build_project_memory(user_input="x")) == "")
check("默认项目指向的项目**无启用记忆** → 不注入",
      _with_db(_fixture_db(setting=_FIX_PID, memories=()),
               lambda: m._build_project_memory(user_input="x")) == "")
check("显式 project_id 优先于 settings 兜底（取项目顺序：显式 → settings → 空）",
      _FIX_PID in _with_db(_fixture_db(setting="someone-else", memories=_MEMS),
                           lambda: m._build_project_memory(project_id=_FIX_PID, user_input="x")))

# ─────────────────────────────────────────────────────────────────────────────
hr("[4] 质量门禁回接：services.subtask_protocol.apply_quality_gate")

from services.subtask_protocol import apply_quality_gate, summarize_status

_s, g1 = apply_quality_gate("full", {"passed": False, "score": 62, "issues": ["报告被截断", "引用不可核"]})
check("★ reflection 未通过 → full 降级为 partial（失败可见）", _s == "partial", f"status={_s}")
check("缺口说明带分数与 issues 摘要", g1 and "62" in g1[0] and "报告被截断" in g1[0], f"gaps={g1}")

_s2, g2 = apply_quality_gate("full", {"passed": True, "score": 90})
check("reflection 通过 → 状态不变、无缺口", _s2 == "full" and g2 == [], f"status={_s2}, gaps={g2}")

_s3, g3 = apply_quality_gate("failed", {"passed": False, "score": 10})
check("只降不升：failed 不被改回 partial", _s3 == "failed", f"status={_s3}")

_s4, g4 = apply_quality_gate("full", None)
check("无 reflection（未启用闭环）→ 状态不变", _s4 == "full" and g4 == [], f"status={_s4}")

check("summarize_status 既有口径未被改动（全 full → full）",
      summarize_status([{"status": "full"}, {"status": "full"}]) == "full")

# ─────────────────────────────────────────────────────────────────────────────
hr("[5] 源码级：两条编排路径都写 orchestrated_status（同一功能不留路径差异）")

_str_src = open(os.path.join(ROOT, "agent/pipeline_parts/stream.py"), encoding="utf-8").read()
_orc_src = open(os.path.join(ROOT, "agent/pipeline_parts/orchestration.py"), encoding="utf-8").read()
check("流式编排路径写 orchestrated_status", '"orchestrated_status": _agg_status' in _str_src)
check("非流式编排路径也写 orchestrated_status（此前缺失）",
      '"orchestrated_status": _agg_status' in _orc_src)
check("非流式路径并入质量门禁", "apply_quality_gate" in _orc_src)

# ─────────────────────────────────────────────────────────────────────────────
hr("[6] 反例守护：不得引入「语义相关性过滤」这类标定不通过的判据")

_mem_src = open(os.path.join(ROOT, "agent/pipeline_parts/memory.py"), encoding="utf-8").read()
check("memory.py 中不再存在 _filter_by_relevance 的**定义**（标定 gap<0，已放弃）",
      "def _filter_by_relevance" not in _mem_src)
check("memory.py 无对已删除符号的悬空调用（防 NameError）",
      "_filter_by_relevance(" not in _mem_src.replace("def _filter_by_relevance", ""))
check("memory.py 留有「标定不通过」的结论注释（防止后人重蹈）",
      "gap = -0.0954" in _mem_src or "不存在能分开二者的阈值" in _mem_src)

cfg_src = open(os.path.join(ROOT, "core/config.py"), encoding="utf-8").read()
check("config 中 model_context_entities 默认 count", '"model_context_entities": "count"' in cfg_src)
check("config 中无残留的 model_context_relevance_* 死配置",
      "model_context_relevance" not in cfg_src)

# ─────────────────────────────────────────────────────────────────────────────
hr("[7] 默认项目链路：无硬编码兜底 + 「未设置」空态语义一致（2026-09-20 清理）")


def _str_consts(node):
    """函数内字符串字面量，**排除 docstring**（注释/文档里提到旧硬编码不算违规）。"""
    doc = ast.get_docstring(node, clean=False)
    return [n.value for n in ast.walk(node)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value != doc]


def _find_fn(path, name):
    for n in ast.walk(ast.parse(open(path, encoding="utf-8").read())):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return n
    return None


_repo_fn = _find_fn(os.path.join(ROOT, "repositories/project_repo.py"), "get_default_project_id")
check("repository 层不再硬编码默认项目（域固化残留已清）",
      _repo_fn is not None and not any("project-satnet-broadband" in s for s in _str_consts(_repo_fn)))
_mem_fn = _find_fn(os.path.join(ROOT, "agent/pipeline_parts/memory.py"), "_build_project_memory")
check("注入层不再硬编码默认项目兜底",
      _mem_fn is not None and not any("project-satnet-broadband" in s for s in _str_consts(_mem_fn)))

_prj_fn = _find_fn(os.path.join(ROOT, "routers/projects.py"), "get_default_project")
check("GET /api/projects/default 用空态对象表达「未设置」（不再以 404 混淆「未配置/已删」）",
      _prj_fn is not None
      and any("unset" in s for s in _str_consts(_prj_fn))
      and not any(isinstance(n, ast.Name) and n.id == "JSONResponse" for n in ast.walk(_prj_fn)))

from routers.graph_workspace import _cohort_default_branch

_bc = sqlite3.connect(":memory:")
_bc.row_factory = sqlite3.Row          # 与 database.get_db() 一致：函数内用 row["col"] 取值
_bc.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
_bc.execute("CREATE TABLE branches (name TEXT)")
_bc.executemany("INSERT INTO branches (name) VALUES (?)", [("release",), ("dev",), ("personal",)])
check("落库分支：settings 行缺失 → 回落 personal", _cohort_default_branch(_bc) == "personal")
_bc.execute("INSERT OR REPLACE INTO settings (key,value) VALUES ('default_branch','dev/test')")
check("★ 落库分支指向 branches 表**不存在**的分支 → 回落 personal（实测曾被写成 dev/test）",
      _cohort_default_branch(_bc) == "personal")
_bc.execute("UPDATE settings SET value='dev' WHERE key='default_branch'")
check("落库分支指向真实存在的分支 → 原样采用", _cohort_default_branch(_bc) == "dev")
_bc.close()

# ─────────────────────────────────────────────────────────────────────────────
hr("[8] 「不强制 / 不默认提供」：归属项目显式给值，不依赖列默认值（2026-09-20）")

# 夹具要点：表 DDL 里**故意写一个「毒默认」** `DEFAULT 'poison-project'` —— 它等价于现存库里
# 那句 `DEFAULT 'project-satnet-broadband'`。若某条写入路径**省略了** project_id 列，落库值就会
# 是 `poison-project`，断言立刻抓住；只有**显式传值**才可能落成 '' 或配置值。
# 为什么不扫源码看「INSERT 列清单里有没有 project_id」：那是文本级判据（换个写法就失效）；
# 这里断言的是**落库结果**，属行为级，不会空转（§6.2）。
_POISON = "poison-project"

# P0-1 影子历史表：**从迁移模块导入同一份 DDL**，不抄写 —— 2026-09-23 实测本夹具因手抄 DDL
# 漏了该表，导致 `create_entity` 抛 `no such table: entity_versions`（夹具第二次因抄写漂移）。
from database.migrations.ontology import ENTITY_VERSIONS_DDL as _EV_DDL

_WRITE_DDL = [
    "CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT, description TEXT)",
    "CREATE TABLE conversations (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, intent TEXT, "
    "user_id INTEGER, project_id TEXT DEFAULT 'poison-project')",
    "CREATE TABLE entities (id TEXT, name TEXT, entity_type TEXT, properties TEXT, status TEXT, "
    "branch TEXT, source_type TEXT, source_doc TEXT, created_by TEXT, reviewed_by TEXT, "
    "reviewed_at TEXT, knowledge_category TEXT, project_id TEXT DEFAULT 'poison-project', "
    "PRIMARY KEY (id, branch))",
    "CREATE TABLE relations (id INTEGER PRIMARY KEY AUTOINCREMENT, source_id TEXT, target_id TEXT, "
    "relation_type TEXT, properties TEXT, status TEXT, branch TEXT, source_doc TEXT, created_by TEXT, "
    "project_id TEXT DEFAULT 'poison-project')",
    _EV_DDL,
]

# 夹具**契约**：被测写入路径依赖的表必须齐（齐不了就显式报错，而不是等到某条 SQL 抛
# OperationalError 让人误以为"产品坏了"）。新增依赖表时同步登记这里（§6.1 夹具纪律）。
_EXPECT_TABLES = {"settings", "conversations", "entities", "relations", "entity_versions"}


def _write_fixture(default_project=_UNSET):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    c = sqlite3.connect(path)
    for ddl in _WRITE_DDL:
        c.execute(ddl)
    _have = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert _EXPECT_TABLES <= _have, (
        "夹具缺表 %s —— 写入路径会 OperationalError（夹具必须补齐被测代码依赖的约定）"
        % sorted(_EXPECT_TABLES - _have))
    if default_project is not _UNSET:
        c.execute("INSERT INTO settings (key,value,description) "
                  "VALUES ('default_project_id',?,'')", (default_project,))
    c.commit()
    c.close()
    return path


def _drive_writes(path) -> dict:
    """在夹具库上跑**真实**写入路径（仓储/路由函数，不是复刻 SQL），返回各处落库归属。"""
    from repositories.conversation_repo import ConversationRepo
    from repositories.knowledge_repo import KnowledgeRepo
    from routers.graph_workspace import _merge_cohort_items

    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    out = {}
    try:
        cid = ConversationRepo(c).create_conversation("t", "chat", 1)
        out["会话"] = c.execute(
            "SELECT project_id FROM conversations WHERE id=?", (cid,)).fetchone()["project_id"]

        KnowledgeRepo(c).create_entity("E-API", "N1", "部件", "{}", "dev", status="reviewed")
        out["实体(知识库API)"] = c.execute(
            "SELECT project_id FROM entities WHERE id='E-API' AND branch='dev'").fetchone()["project_id"]

        KnowledgeRepo(c).create_relation("E-API", "E-API", "CONTAINS", "{}", "dev")
        out["关系(知识库API)"] = c.execute(
            "SELECT project_id FROM relations WHERE source_id='E-API'").fetchone()["project_id"]

        _merge_cohort_items(c, "dev", "tester", [
            {"id": 1, "s": "GW-A", "p": "包含", "o": "GW-B",
             "item_kind": "relation", "inferred_json": "{}"}])
        out["实体(图谱工作台)"] = [r["project_id"] for r in c.execute(
            "SELECT project_id FROM entities WHERE id IN ('GW-A','GW-B') AND branch='dev'")]
        out["关系(图谱工作台)"] = c.execute(
            "SELECT project_id FROM relations WHERE source_id='GW-A'").fetchone()["project_id"]
    finally:
        c.close()
    return out


def _flat(d) -> list:
    """把各写入点的落库归属摊平成一维（图谱工作台那次写入会产生多行）。"""
    vals = []
    for v in d.values():
        vals.extend(v if isinstance(v, list) else [v])
    return vals


_p_none = _write_fixture(_UNSET)
_w_none = _drive_writes(_p_none)
check("★ 未配置默认项目 → 各写入路径落库归属全部为「未归属」（空串），不落列默认值",
      all(v == "" for v in _flat(_w_none)), f"got={_w_none}")
check(f"无一条路径落成毒默认（漏给 project_id 会立刻出现 {_POISON}）",
      _POISON not in str(_w_none), f"got={_w_none}")
os.unlink(_p_none)

_p_cfg = _write_fixture("proj-x")
_w_cfg = _drive_writes(_p_cfg)
# ⚠️ 2026-09-28（多工程 P0-2）**会话已从本断言中摘出**：会话归属改由发起方**显式**决定，
# 不再回落 settings.default_project_id —— 它是全局单行、不分标签页，多标签并发时后切换者
# 覆盖前者，会让先开的标签页新建会话**静默错归属**。无工程会话（知识检索/问答）是合法状态。
# 会话的新不变式单列在下一条；其余写入路径（实体/关系/图谱工作台）仍由配置驱动，判据不变。
_w_cfg_noconv = {k: v for k, v in _w_cfg.items() if k != "会话"}
check("★ 配置了默认项目 → 图谱类写入路径全部归到该项目（证明是配置在驱动归属）",
      all(v == "proj-x" for v in _flat(_w_cfg_noconv)), f"got={_w_cfg_noconv}")
check("★ 会话归属**不**跟随默认项目（P0-2：显式优先；未显式给 = 无工程会话）",
      _w_cfg["会话"] == "", f"got={_w_cfg['会话']!r}（默认=proj-x；若跟随即为串归属）")
os.unlink(_p_cfg)

_rp = sqlite3.connect(":memory:")
_rp.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
from repositories.project_repo import resolve_project_id as _rpi

check("resolve_project_id：settings 行缺失 → 空串（不猜、不兜底）", _rpi(_rp) == "")
_rp.execute("INSERT INTO settings (key,value) VALUES ('default_project_id','')")
check("resolve_project_id：置空 → 空串", _rpi(_rp) == "")
_rp.execute("UPDATE settings SET value='proj-y' WHERE key='default_project_id'")
check("resolve_project_id：配置值原样返回", _rpi(_rp) == "proj-y")
_rp.close()

_hard = []
for _rel in ("database/schema.py", "database/migrations/columns.py",
             "database/migrations/rebuild.py", "database/migrations/plugins.py"):
    if "DEFAULT 'project-satnet-broadband'" in open(
            os.path.join(ROOT, _rel), encoding="utf-8").read():
        _hard.append(_rel)
check("★ DDL 层不再残留 project_id 的硬编码默认值（4 个文件）", not _hard, f"残留={_hard}")
check("seeds 不再把 default_project_id 预设成某个具体项目",
      "('default_project_id', 'project-satnet-broadband'" not in open(
          os.path.join(ROOT, "database/seeds.py"), encoding="utf-8").read())

# ─────────────────────────────────────────────────────────────────────────────
hr("[9] 「图谱只是来源之一」：来源为空 / 无匹配时不硬塞内容（2026-09-20）")

# 用户口径（2026-09-20）：「图谱分支只是 AI 建模的一个数据来源，未匹配到对应的数据，无需强制/默认
# 提供不合理的内容，是一个不断构建完善的过程」。本段把这句话落成**不变式**：
# 来源为空 ⇒ 不注入、不编造、消费侧优雅降级；而不是拿「默认内容」把空位填满。
_EMPTY_DDL = [
    "CREATE TABLE entities (id TEXT, name TEXT, entity_type TEXT, status TEXT, branch TEXT, created_at TEXT)",
    "CREATE TABLE graph_views (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, branch TEXT)",
    "CREATE TABLE impact_analyses (id INTEGER PRIMARY KEY AUTOINCREMENT, change_source TEXT, "
    "title TEXT, created_at TEXT)",
    "CREATE TABLE ontology_versions (id INTEGER PRIMARY KEY AUTOINCREMENT, version_label TEXT, "
    "active INTEGER DEFAULT 0)",
    "CREATE TABLE ontology_version_snapshots (version_id INTEGER, type_id INTEGER, name TEXT, "
    "type_kind TEXT, parent_id INTEGER, properties TEXT, constraints TEXT, description TEXT, "
    "icon TEXT, color TEXT, iri TEXT)",
    "CREATE TABLE ontology_types (id INTEGER PRIMARY KEY, name TEXT, type_kind TEXT, parent_id INTEGER, "
    "properties TEXT, constraints TEXT, description TEXT, icon TEXT, color TEXT)",
]


def _empty_db(with_types=False, active_version=False):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    for d in _EMPTY_DDL:
        c.execute(d)
    if with_types:
        c.execute("INSERT INTO ontology_types (id,name,type_kind,parent_id,properties,constraints,"
                  "description,icon,color) VALUES (1,'部件','entity',NULL,'[]','[]','','','')")
    if active_version:
        c.execute("INSERT INTO ontology_versions (id,version_label,active) VALUES (9,'v-test',1)")
    c.commit()
    c.close()
    return path


_p_e = _empty_db()
_out_e = _with_db(_p_e, lambda: m._build_model_context("release", 0, "生成电动汽车热管理系统代码"))
check("★ 分支无图谱资产 → 建模上下文不注入（返回空串，不硬塞占位内容）",
      _out_e == "", repr(_out_e[:110]))
os.unlink(_p_e)

_p_ont = _empty_db(with_types=True)
_c_ont = sqlite3.connect(_p_ont)
_c_ont.row_factory = sqlite3.Row
from routers.knowledge_parts.shared import _active_ont_rows

check("无 active 本体版本 → 消费侧回退当前类型表（不返回空、不报错）",
      len(_active_ont_rows(_c_ont)) == 1)
_c_ont.close()
os.unlink(_p_ont)

_p_ont2 = _empty_db(with_types=True, active_version=True)
_c_ont2 = sqlite3.connect(_p_ont2)
_c_ont2.row_factory = sqlite3.Row
check("active 版本**无快照**（存量 released / 测试发布）→ 同样回退，不消费空数据",
      len(_active_ont_rows(_c_ont2)) == 1)
_c_ont2.close()
os.unlink(_p_ont2)

_p_rag = _empty_db()
_c_rag = sqlite3.connect(_p_rag)
_c_rag.row_factory = sqlite3.Row
_Og = _cfg2.get
_cfg2.get = lambda sec, key, default=None, _o=_Og: (
    False if (sec, key) == ("graph_db", "enabled") else _o(sec, key, default))
try:
    _hits = GraphRAG._entity_link(_c_rag, "巡飞弹 动力分系统", ["release"])
finally:
    _cfg2.get = _Og
check("★ 分支图谱无数据 → 实体链接 0 命中（不编造、不用默认内容兜底）", _hits == [], f"hits={_hits}")
_c_rag.close()
os.unlink(_p_rag)

# ─────────────────────────────────────────────────────────────────────────────
print()
print("=" * 90)
print(f"断言汇总：{_n_pass}/{_n_pass + _n_fail} 通过")
if _n_fail:
    print(f"❌ 失败 {_n_fail} 项")
print("口径提示：本脚本不依赖 git ref 与服务；[2] 的 count 断言依赖 config 真实值为 count；")
print("          [3][7][8] 夹具驱动（临时库），不受 settings.default_project_id 当前取值影响；")
print("          [8] 夹具带毒默认 DEFAULT 'poison-project'，专抓「漏给 project_id」的写入路径；")
print("          [9] 夹具为空库，锁定「来源为空 → 不注入/不编造/优雅降级」不变式。")
print("=" * 90)
sys.exit(1 if _n_fail else 0)
