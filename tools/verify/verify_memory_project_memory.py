# -*- coding: utf-8 -*-
"""项目记忆（Project Constitution）的「定位」门禁（P1-8，2026-11-27）。

## 为什么要有这条门禁
方案 P1-8 的原话是「`project_memories` 二选一：由 knowledge_reflow 自动生成项目级决策填充，
或下线『项目记忆』页与 project_memory 提示词块，避免『有页面没数据』」。

**2026-11-27 实测后选 B（不自动生成），且理由必须写进判据，否则下一季度会被重新提出：**
  · `project_memories` 实测 **0 行**；
  · 唯一存在的"决策/规范"记忆 5 条，**全部 `scope_type=''`**（无项目归属），
    且内容是 impact 测试残留（「载荷配置调整 ×3」「巡飞弹参数调整」「变更影响分析须闭环验证」）；
  · `knowledge_reflow` 的写入路径**要求项目归属**（无归属跳过），实测 56 条 reflow 记忆
    **全部**来自 `test-reg` 测试源、scope 为空 —— 即自动通道历史上从未被真实业务触发过。
  ⇒ 任何"自动填充"要么**无内容**，要么**必须给无归属行臆造项目归属** —— 后者正是历史事故形态
    （孤儿记忆被"顺手补默认项目"后变成项目知识）。故：**保留人工台账，明令禁止自动兜底**。

## 断言
  P1 空表 ⇒ 注入块为**空串**（零 token 成本；页面空态是"没人写"，不是"坏了"）
  P2 有手工行 ⇒ 注入块非空、带项目名与"仅当属于该项目领域时适用"范围声明
  P3 跨项目隔离：别的项目的行不得出现在本项目块里
  P4 **禁止自动兜底**：`agent_memory` 里塞满无归属的 决策/规范 行 + `project_memories` 为空
     ⇒ 块必须仍为空（这是"不许把测试残留变成项目宪法"的机器可执行版）
  P5 变异自证：给 `_build_project_memory` 注入"空表时回退读 agent_memory"的分支 ⇒ P4 必红
  P6 代码层禁止：`_build_project_memory` 源码中不得出现 `agent_memory`
     （P4 是行为判据，P6 是结构判据；只留 P4 会被"只在该项目有行时才兜底"绕过）

用法： ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_memory_project_memory.py
"""
import os
import re
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.chdir(ROOT)

PASS, FAIL = [], []


def chk(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name,
                           ("  — " + str(detail)) if detail and not ok else ""))


_FIX_PID = "proj-fix"
_OTHER_PID = "proj-other"


def fixture(pm_rows=(), am_rows=()):
    """临时库：settings + project_memories + agent_memory（含决策/规范行的最小列集）。"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT, description TEXT)")
    c.execute("CREATE TABLE project_memories (id INTEGER PRIMARY KEY AUTOINCREMENT, "
              "project_id TEXT, category TEXT, title TEXT, content TEXT, enabled INTEGER DEFAULT 1)")
    c.execute("CREATE TABLE agent_memory (id INTEGER PRIMARY KEY AUTOINCREMENT, "
              "agent_id TEXT, mem_type TEXT, content TEXT, mem_topic TEXT, "
              "scope_type TEXT DEFAULT '', scope_id TEXT DEFAULT '', forgotten INTEGER DEFAULT 0, "
              "tier TEXT DEFAULT 'recall', access_count INTEGER DEFAULT 0, "
              "superseded_by INTEGER DEFAULT 0, valid_to TEXT DEFAULT NULL, "
              "verify_state TEXT DEFAULT 'unverifiable', embedding TEXT DEFAULT '[]', "
              "embed_version TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP)")
    for pid, cat, title, content in pm_rows:
        c.execute("INSERT INTO project_memories (project_id,category,title,content,enabled) "
                  "VALUES (?,?,?,?,1)", (pid, cat, title, content))
    for agent, topic, content, acc in am_rows:
        c.execute("INSERT INTO agent_memory (agent_id,mem_type,content,mem_topic,access_count) "
                  "VALUES (?,'experience',?,?,?)", (agent, content, topic, acc))
    c.commit()
    c.close()
    return path


def with_db(path, fn):
    """令函数内的 `from database import get_db` 指向临时库。"""
    import database as _dbmod
    _orig = _dbmod.get_db

    def _get_db(*a, **kw):
        c = sqlite3.connect(path)
        c.row_factory = sqlite3.Row
        return c

    _dbmod.get_db = _get_db
    try:
        return fn()
    finally:
        _dbmod.get_db = _orig


def _clean_src_for_ast(src: str) -> str:
    """把 `inspect.getsource` 拿到的**类内缩进片段**还原成可 `ast.parse` 的模块级源码。

    ⚠️ 实测：`getsource` 对方法返回的是**每一行都带 4 空格缩进**（含 `def` 那行），
    所以必须整体 `textwrap.dedent`，不能"首行保留"——后者会报 `unexpected indent`。
    """
    import textwrap
    return textwrap.dedent(src.expandtabs())


def build(pm_rows=(), am_rows=(), project_id=_FIX_PID, exec_src=None):
    """建夹具 → 调 `_build_project_memory`。exec_src 非空时用**变异后的源码**跑。"""
    from agent.pipeline_parts.memory import MemoryMixin
    path = fixture(pm_rows, am_rows)
    try:
        if exec_src is None:
            obj = MemoryMixin()
            return with_db(path, lambda: obj._build_project_memory(project_id=project_id,
                                                                  user_input="生成热管理模型的 sysml v2 代码"))
        ns = {"__name__": "mem_mut"}
        exec(compile(exec_src, "agent/pipeline_parts/memory.py", "exec"), ns)
        obj = ns["MemoryMixin"]()
        return with_db(path, lambda: obj._build_project_memory(project_id=project_id,
                                                              user_input="生成热管理模型的 sysml v2 代码"))
    finally:
        try:
            os.remove(path)
        except Exception:
            pass


_AM_RESIDUE = [
    ("impact", "决策", "载荷配置调整：变更操作 modify E1 的内容（来源 impact-9）", 93),
    ("impact", "决策", "巡飞弹参数调整：对巡飞弹执行 modify 类参数调整变更", 30),
    ("reflow", "规范", "变更影响分析须闭环验证：任何 modify 变更在实施前必须完成影响分析", 0),
]


def main() -> int:
    print("=" * 78)
    print("门禁：项目记忆（Project Constitution）· 人工台账定位（禁止自动兜底）")
    print("=" * 78)

    # ── P1 空表 ⇒ 空串 ──
    out = build(pm_rows=(), am_rows=())
    chk("P1 project_memories 为空 ⇒ 注入块为空串（零 token 成本）", out == "", repr(out[:80]))

    # ── P2 有手工行 ⇒ 注入 + 范围声明 ──
    out = build(pm_rows=[(_FIX_PID, "规范", "需求编号规则", "客户未给编号时用 TBD 占位")],
                am_rows=[])
    chk("P2 有手工行 ⇒ 注入块非空且带项目名与范围声明",
        bool(out) and _FIX_PID in out and "仅当本次任务属于该项目领域时适用" in out,
        repr(out[:160]))

    # ── P3 跨项目隔离 ──
    out = build(pm_rows=[(_OTHER_PID, "规范", "别的项目的规范", "不该出现在本项目块里")],
                am_rows=[])
    chk("P3 别的项目的行不出现（跨项目隔离）", out == "", repr(out[:120]))

    # ── P4 禁止自动兜底（行为判据）──
    out = build(pm_rows=[], am_rows=_AM_RESIDUE)
    chk("P4 空表 + agent_memory 里有决策/规范行 ⇒ **仍为空**（不许把残留变成项目宪法）",
        out == "", repr(out[:200]))

    # ── P6 结构判据：**代码**不得引用 agent_memory（docstring 除外）──
    # ⚠️ 第一版用 `"agent_memory" not in src` 直接比原文 —— 结果被本函数自己新增的
    #   docstring（里面写的就是"源码中不出现 agent_memory"）判红。文本判据会被**注释/文档**
    #   误伤，必须走 AST：去掉 docstring 后，检查 body 里的字符串常量与标识符。
    import ast
    import inspect
    from agent.pipeline_parts.memory import MemoryMixin
    src = inspect.getsource(MemoryMixin._build_project_memory)
    _tree = ast.parse(_clean_src_for_ast(src))
    _fn = next((n for n in ast.walk(_tree) if isinstance(n, ast.FunctionDef)), None)
    _body = list(_fn.body) if _fn else []
    if _body and isinstance(_body[0], ast.Expr) and isinstance(
            getattr(_body[0], "value", None), ast.Constant):
        _body = _body[1:]          # 剥掉 docstring
    _hits = []
    for _n in _body:
        for _x in ast.walk(_n):
            if isinstance(_x, ast.Constant) and isinstance(_x.value, str) \
                    and "agent_memory" in _x.value:
                _hits.append(_x.value[:60])
            elif isinstance(_x, ast.Name) and _x.id == "agent_memory":
                _hits.append("Name:agent_memory")
            elif isinstance(_x, ast.Attribute) and _x.attr == "agent_memory":
                _hits.append("Attr:agent_memory")
    chk("P6 `_build_project_memory` **代码体**不引用 agent_memory（docstring 除外）",
        not _hits, _hits[:3])

    # ── P5 变异自证：注入"空表回退读 agent_memory"⇒ P4 必须红 ──
    # 锚点取 return "" 前的那句 `if not rows:`（唯一），改成给它一个自动兜底分支。
    _full = inspect.getsource(MemoryMixin)
    _old = """            if not rows:
                return \"\""""
    _new = """            if not rows:
                # 变异体：空表时"顺手"用记忆库兜底（正是本门禁要禁止的形态）
                _cdb = get_db()
                try:
                    # ⚠️ 列名必须用 agent_memory 的真列（mem_topic），不是 project_memories 的
                    #   category —— 这里写错会 `no such column` 被外层 except 吞成"返回空串"，
                    #   于是变异体看起来"没生效"，门禁红得像判据坏了（实测踩过一次）。
                    _ac = _cdb.execute(
                        "SELECT mem_topic AS category, mem_topic AS title, content "
                        "FROM agent_memory WHERE forgotten=0 "
                        "AND mem_topic IN ('决策','规范') LIMIT 5").fetchall()
                finally:
                    _cdb.close()
                if _ac:
                    rows = _ac
                else:
                    return \"\""""
    if _old not in _full:
        chk("P5 变异锚点命中", False, "锚点未命中 ⇒ 判据需同步（源码形态变了）")
    else:
        # ⚠️ 变异作用于**整个 Mixin 源码**（`_build_project_memory` 需要 self 上的其他方法可得），
        #   再 exec 出变异体——这是本仓"变异自证"的标准姿势。
        _mut = _full.replace(_old, _new, 1)
        try:
            out_mut = build(pm_rows=[], am_rows=_AM_RESIDUE, exec_src=_mut)
            chk("P5 变异体确实改变了行为（兜底生效）", out_mut != "", repr(out_mut[:80]))
            chk("P5 变异自证：注入自动兜底 ⇒ P4 判据翻红（实测 out_mut 非空）",
                out_mut != "", "P4 断言的是 `out == ''`；变异后非空 ⇒ P4 必红")
        except Exception as e:  # noqa: BLE001
            chk("P5 变异自证", False, "%s: %s" % (type(e).__name__, str(e)[:160]))

    print()
    print("=" * 78)
    print("PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("  FAIL:", f)
    print("=" * 78)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
