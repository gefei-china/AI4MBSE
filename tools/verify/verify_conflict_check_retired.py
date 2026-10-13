# -*- coding: utf-8 -*-
"""门禁：`conflict_check` 假实现已下线（米爸 2026-10-13 决定：只移除假实现，保留可用那套）。

**下线的是什么**：`agent/pipeline_parts/tools.py` 里的
`return {"ok":True, "result":"冲突检测完成，无新冲突"}` ——
**无条件返回"无冲突"，与传入什么参数完全无关**。真库实测 17 次调用
（`预评审Agent` 15 / `需求分析Agent` 2），`arguments` **全是 `{}`**（连 query 都没传），
却让模型得出"已检测、无冲突"的结论。属**假成功**：工具会说漂亮话，但从没做过任何检测。

**保留的是什么**（本门禁反向守着它们不被误删）：
  · `workflows/tools.py:281` —— 会**真检索图谱**、比对同名实体的实现；
  · `workflows/nodes.py:857` —— ReAct 默认工具串（走 workflows 那套）；
  · `entity_resolver.detect_conflicts()` + `knowledge_conflicts` 表 —— 真正的冲突消解（另一条路）。

**本门禁的核心价值：守住"两条独立进入路径"**
同一个工具名有**两条**能进 payload 的路径，只处理一条等于没下线（实测踩过）：
  ① DB 注册表路径 → 查 `tools.status='active'`
  ② P1-6「安装即可消费」路径 → **只查插件状态，不查 `tools.status`**
     而 `plugins` 表里有一条 `com.zhiyuan.legacy.tool.conflict-check`（published、
     `tool_entry_from_plugin()` 返 `executable: True`）⇒ 只置 `tools.status=disabled` 时，
     **该工具仍被注入**（实测确认）。

**只读**：临时库（真库备份副本），**不写生产库**；变异在内存副本做。

**运行**：`.venv/Scripts/python.exe -X utf8 tools/verify/verify_conflict_check_retired.py`
"""
import io
import os
import re
import sys
import sqlite3
import tempfile
import shutil

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TOOLS = os.path.join(ROOT, "agent", "pipeline_parts", "tools.py")
REG = os.path.join(ROOT, "agent", "registry.py")
SEEDS = os.path.join(ROOT, "database", "seeds.py")
MISC = os.path.join(ROOT, "database", "migrations", "misc.py")
SCHEMA = os.path.join(ROOT, "database", "schema.py")
MINIT = os.path.join(ROOT, "database", "migrations", "__init__.py")
WTOOLS = os.path.join(ROOT, "workflows", "tools.py")
WNODE = os.path.join(ROOT, "workflows", "nodes.py")
RESOLVER = os.path.join(ROOT, "entity_resolver.py")
PROD_DB = os.path.join(ROOT, "mbse.db")

sys.path.insert(0, ROOT)   # 本脚本以脚本方式运行，需手动把仓库根放进 path

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


def read(p):
    return io.open(p, encoding="utf-8").read()


t_src = read(TOOLS)
r_src = read(REG)
s_src = read(SEEDS)
m_src = read(MISC)
w_src = read(WTOOLS)
n_src = read(WNODE)

print("=== A. 假实现已移除 ===")
# 只看**代码**行：注释里提到 conflict_check 是**刻意保留的说明**（记录为什么删），
# 不过滤注释会让 A1/A3 因为"文档提到了它"而假红。
t_code = re.sub(r"--[^\n]*", "", t_src)
check("A1 执行分支已删（不再是 if name == 'conflict_check'）",
      'if name == "conflict_check"' not in t_code)
# ⚠️ 必须**剥掉注释**再查：我的替换注释里引用了这句文案（说明"删的是什么"），
#    不过滤会把"文档提到了它"当成"实现还在" ⇒ 假红。
_t_nocomment = re.sub(r"#.*", "", t_src)
check("A2 那句假文案已不存在（已剥注释）",
      "冲突检测完成，无新冲突" not in _t_nocomment,
      "无条件返回『无新冲突』的假实现仍在")
check("A3 builtin_desc 里的登记已删",
      re.search(r'^\s*"conflict_check"\s*:', t_code, re.M) is None,
      "仍会作为内置工具注入 payload")
check("A4 registry 里 requirement_analysis 已不再绑它",
      re.search(r'tools=\[[^\]]*"conflict_check"', r_src) is None)
check("A5 seeds 的 Agent 绑定已不再含它",
      re.search(r'"requirement_analysis":\s*\[[^\]]*"conflict_check"', s_src) is None)
check("A6 seeds 的 builtin_tools 已不再 seed 它",
      re.search(r'\(\s*"conflict_check"\s*,', s_src) is None,
      "仍会 seed ⇒ 新库初始化时复活")

print("\n=== B. 保留的那套不能被误删（反向断言）===")
check("B1 workflows/tools.py 的实现仍在", 'if name == "conflict_check"' in w_src)
check("B2 workflows 的实现是真检索（用了 rag.retrieve）",
      re.search(r'if name == "conflict_check":(.{0,500}?rag\.retrieve)', w_src, re.S) is not None,
      "看起来也被删了 —— 那套是真会检索图谱的可用实现")
check("B3 workflows 声明表仍在", re.search(r'"conflict_check":\s*\{', w_src) is not None)
check("B4 ReAct 默认串仍含它（走 workflows 那套）", "conflict_check" in n_src)
check("B5 entity_resolver.detect_conflicts 未被动过",
      os.path.exists(RESOLVER) and "def conflict_detect" in read(RESOLVER),
      "entity_resolver 的冲突消解函数名对不上（真名是 conflict_detect）")

print("\n=== C. 退役迁移覆盖两条路径 ===")
check("C1 迁移函数存在", "def _retire_fake_conflict_check" in m_src)
check("C2 处理 tools 行", "UPDATE tools SET status='disabled' WHERE name='conflict_check'" in m_src)
check("C3 ★ 处理 plugins 行（P1-6 路径不查 tools.status，少这条等于没下线）",
      "UPDATE plugins SET status='disabled'" in m_src
      and "com.zhiyuan.legacy.tool.conflict-check" in m_src)
check("C4 迁移已接入 init_db 调用链", "_retire_fake_conflict_check(conn)" in read(SCHEMA))
check("C5 迁移已在 __init__ 导出（否则 NameError）", "_retire_fake_conflict_check" in read(MINIT))


def _load_migrate_fn(src_text):
    """从 misc.py 源码里取出迁移函数（避免 import 触发整条 database 导入链）。"""
    _body = src_text.split("def _retire_fake_conflict_check", 1)[1]
    _body = "def _retire_fake_conflict_check" + _body.split("\ndef ", 1)[0]
    _ns = {"__name__": "_m"}
    exec(compile(_body, "<mig>", "exec"), _ns)
    return _ns["_retire_fake_conflict_check"]


print("\n=== D. 行为层：临时库实跑 ===")
TMP = tempfile.mkdtemp(prefix="verify_ck_retired_")
TMP_DB = os.path.join(TMP, "t.db")
try:
    _src = sqlite3.connect("file:%s?mode=ro" % PROD_DB, uri=True)
    _dst = sqlite3.connect(TMP_DB)
    _src.backup(_dst)
    _dst.close()
    _src.close()

    _c = sqlite3.connect(TMP_DB)
    _c.row_factory = sqlite3.Row
    os.environ["MBSE_DB_PATH"] = TMP_DB
    _t0 = _c.execute("SELECT status FROM tools WHERE name='conflict_check'").fetchone()
    _p0 = _c.execute("SELECT status FROM plugins WHERE name='conflict-check'").fetchone()
    print("  迁移前：tools=%s｜插件=%s" % (_t0["status"] if _t0 else "无",
                                          _p0["status"] if _p0 else "无"))
    _fn = _load_migrate_fn(m_src)
    _fn(_c)
    _fn(_c)   # 幂等性

    _t1 = _c.execute("SELECT status FROM tools WHERE name='conflict_check'").fetchone()
    _p1 = _c.execute("SELECT status FROM plugins WHERE name='conflict-check'").fetchone()
    check("D1 tools 行已 disabled", _t1 and _t1["status"] == "disabled",
          "got=%s" % (_t1["status"] if _t1 else None))
    check("D2 ★ 插件行也 disabled（否则仍会被注入）",
          _p1 and _p1["status"] == "disabled", "got=%s" % (_p1["status"] if _p1 else None))
    check("D3 只下线不删行（tools 行保留，历史流水不悬空）",
          _c.execute("SELECT COUNT(*) FROM tools WHERE name='conflict_check'").fetchone()[0] == 1)
    check("D4 只下线不删行（plugins 行保留）",
          _c.execute("SELECT COUNT(*) FROM plugins WHERE name='conflict-check'").fetchone()[0] == 1)
    check("D5 17 条历史调用留痕未被删",
          _c.execute("SELECT COUNT(*) FROM tool_call_logs "
                     "WHERE tool_name='conflict_check'").fetchone()[0] == 17)
    _c.close()

    from agent.pipeline_parts.tools import ToolMixin  # noqa: E402

    class _M(ToolMixin):
        def __init__(self):
            self.registry = type("R", (), {
                "get_bound_mcp": lambda s, i: [],
                "get": lambda s, i: type("A", (), {"tools": ["graph_retrieve", "entity_create"]})(),
            })()

    _names = [d["function"]["name"] for d in _M()._build_tools_def("requirement_analysis", "")]
    check("D6 ★ payload 里不再有 conflict_check（两条路径都断了）",
          "conflict_check" not in _names,
          "仍被注入（共 %d 个工具）" % len(_names))
    check("D7 其余工具未被误伤（graph_retrieve 等仍在）", "graph_retrieve" in _names)
finally:
    shutil.rmtree(TMP, ignore_errors=True)
    os.environ.pop("MBSE_DB_PATH", None)

print("\n=== E. 真库基线（只读）===")
if os.path.exists(PROD_DB):
    _pc = sqlite3.connect("file:%s?mode=ro" % PROD_DB, uri=True)
    _pc.row_factory = sqlite3.Row
    _t = _pc.execute("SELECT status FROM tools WHERE name='conflict_check'").fetchone()
    _p = _pc.execute("SELECT status FROM plugins WHERE name='conflict-check'").fetchone()
    print("  真库：tools=%s｜插件=%s（迁移在服务重启时执行）"
          % (_t["status"] if _t else "无", _p["status"] if _p else "无"))
    check("E1 记录改动前基线为 active/published（本次修复的由来）",
          bool(_t) and _t["status"] == "active" and bool(_p) and _p["status"] == "published",
          "真库状态已变 ⇒ 基线描述需更新")
    _pc.close()

print("\n=== F. 变异自证（只下线 tools、漏掉 plugins）===")
# ⚠️ 变异体**预先生成为一个文件**（tmp/p_bench/_mut_misc.py.fn.py），不在这里做字符串替换。
#    原因（第一、二版都踩了）：把 SQL 字符串整段替换会让后续括号内容变成裸标识符 ⇒ SyntaxError，
#    门禁只会报"变异体执行失败"，抓不到真正的语义。
#    现在改成：在源码副本上做**一处最小、语法合法的编辑**（把插件下线的 UPDATE 换成 SELECT 1），
#    差异恰好就是"tools 侧下了、plugins 侧没下" —— 正是我们要断言的那条。
# ⚠️ 变异体在**门禁内部现生成**（内存），不依赖外部文件。
#    第一版把它写到 tmp/ 下 —— 而 tmp/ 是 gitignore 的 ⇒ 换台机器/CI 上文件不存在，
#    门禁会走 [VACUOUS] 分支**静默失去自证能力**（老问题：判据空转）。
#    编辑方式也换了：**不在本文件里 str.replace 整段 SQL**（会让后续括号变成裸标识符
#    ⇒ SyntaxError，门禁只报"执行失败"、抓不到语义），而是在**读进来的源码文本**上
#    做一处最小、语法合法的替换（把插件下线的 UPDATE 换成 SELECT 1）。
_OLD_PLUGIN_SQL = (
    '        cur2 = conn.execute(\n'
    '            "UPDATE plugins SET status=\'disabled\' "'
)
_NEW_PLUGIN_SQL = (
    '        cur2 = conn.execute(\n'
    '            "SELECT 1"  # MUTATION: 不下线 plugins'
)
_mut_src = (m_src.replace(_OLD_PLUGIN_SQL, _NEW_PLUGIN_SQL, 1)
            if _OLD_PLUGIN_SQL in m_src else None)
if not _mut_src:
    print("  [VACUOUS] 未找到插件下线的 SQL 块（源码结构变了，需更新本变异体）")
else:
    try:
        _fnbody = ("def _retire_fake_conflict_check"
                   + _mut_src.split("def _retire_fake_conflict_check", 1)[1].split("\ndef ", 1)[0])
        compile(_fnbody, "<mut>", "exec")
    except SyntaxError as _se:
        print("  [VACUOUS] 变异体语法错误：%s" % _se)
        _fnbody = None
if _mut_src and _fnbody:
    T2 = tempfile.mkdtemp(prefix="verify_ck_mut_")
    D2 = os.path.join(T2, "m.db")
    try:
        _s = sqlite3.connect("file:%s?mode=ro" % PROD_DB, uri=True)
        _d = sqlite3.connect(D2)
        _s.backup(_d)
        _d.close()
        _s.close()
        _mc = sqlite3.connect(D2)
        _mc.row_factory = sqlite3.Row
        _ns2 = {"__name__": "_m2"}
        exec(compile(_fnbody, "<mut>", "exec"), _ns2)
        _ns2["_retire_fake_conflict_check"](_mc)
        _ps = _mc.execute("SELECT status FROM plugins WHERE name='conflict-check'").fetchone()
        _ts = _mc.execute("SELECT status FROM tools WHERE name='conflict_check'").fetchone()
        check("F1 变异体（漏掉 plugins）上 D2 必须 FAIL（插件仍 published）",
              bool(_ps) and _ps["status"] == "published",
              "变异体里插件也变 disabled ⇒ D2 抓不住变异，自证无效")
        check("F2 对照：变异体里 tools 行确实已下线（证明只差 plugins 那一步）",
              bool(_ts) and _ts["status"] == "disabled",
              "got=%s" % (_ts["status"] if _ts else None))
        _mc.close()
        _muts.append(("M1", "退役迁移漏掉 plugins 侧（P1-6 路径仍会注入）"))
        print("  [SELF-PROOF] M1 只下线 tools 不下线插件 —— 该工具会经「安装即可消费」"
              "重新进入 payload，证明白名单路径必须一起处理")
    except Exception as e:
        check("F1 变异体执行成功", False, str(e))
    finally:
        shutil.rmtree(T2, ignore_errors=True)

print("\n" + "=" * 60)
print("常态断言：%d 条，%d 通过 / %d 失败" % (_ok + _fail, _ok, _fail))
print("变异自证：%d 组" % len(_muts))
for mid, desc in _muts:
    print("  - %s %s" % (mid, desc))
print("结论：%s" % ("门禁通过" if _fail == 0 and _muts else "门禁未通过"))
sys.exit(0 if (_fail == 0 and _muts) else 1)