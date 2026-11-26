# CI-OPTIONAL: [broken] 已知红，记账不进 CI（修好后上调 tier 并接线）；两库都红 [FAIL] [FAIL] B4 ReAct 默认串仍含它（走 workflows 那套） 
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
from _tmpdir import mkdtemp  # 共享：创建即登记，atexit 退出时自动回收临时目录
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
RESOLVER = os.path.join(ROOT, "services/entity_resolver.py")
PROD_DB = (os.environ.get("MBSE_DB_PATH") or os.path.join(ROOT, "mbse.db"))

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


def skip(name, why=""):
    """判据面不存在（不是失败）。用于「干净库上该资产本就不存在」这类情形。

    ⚠️ 什么时候该用 SKIP 而不是 FAIL：判据测的是「某个**已存在的**遗留资产被正确处理」，
       而干净库（CI 等价口径）根本不会种出那条资产 ⇒ 断言无从谈起。
       此时判红是**假红**（报的是"环境没有"，读起来像"产品坏了"）。
    ⚠️ 但当库是**生产口径**时，同一条资产缺失就是**真问题**（有人把行删了，
       违反"只下线不删行"）⇒ 那时必须判红。所以下面的守卫都要先判"是不是生产库"。
    """
    print("  [SKIP] %s  %s" % (name, why))


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
# ★ 2026-11-26 修 B4（同族第五例「被测样本已退役」）：
#   原判据 `"conflict_check" in n_src`（n_src = workflows/nodes.py）断言
#   「ReAct 默认串仍含它」——因为那串在 ReAct 节点里，用意是"保留那套还在被用"。
#   实测 `workflows/nodes.py` 的**画布节点执行器已于 2026-11-05 整体移除**
#   （文件头注释：「画布工作流已明确舍弃，18 个节点执行器全部移除」；
#    主链路 react 已于 2026-11-04 改为**委托主链路 Agent**，不再自带工具串）
#   ⇒ 被测对象没了，B4 恒红。
#   顺带查清一个**必须记录的事实**：`workflows/tools.py` 的这份实现
#   **在生产代码里已零调用方**（只有本门禁和 verify_atomic_tools / verify_skill_script_run 引它）。
#   所以 B 组的真实价值从"证明它在用"降级为"证明这份真检索实现**还在**（未来若复活画布/直调可直接用）"。
#   ⇒ B4 改为**守护那个架构决策不被悄悄翻回**：画布 react 节点不得复活。
#     若有人复活它并顺手接回 conflict_check，就会绕过 D 组的注入切断（两路径切断 = 本门禁的核心）。
check("B4 画布 ReAct 节点执行器仍处移除态（复活会绕过 D 组的注入切断）",
      "def _exec_react" not in n_src and "react" not in re.findall(r"ttype\s*==\s*['\"](\w+)['\"]", n_src),
      "画布 react 节点又出现了 ⇒ 检查它是否接回 conflict_check 等已下线工具")
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


print("\n=== D. 迁移行为（临时库实跑）===")
# ★★ 2026-11-26 重写 D 组的**基准构造**（原实现是假绿，两条判据都失去意义）：
#
#   原实现：临时库从**真库备份** → 直接跑迁移 → 断言 tools/plugins 都是 disabled。
#   问题：真库**早已迁移过**（实测 tools=disabled / plugins=disabled）
#         ⇒ 跑迁移前就已经是 disabled ⇒ **迁移函数即使完全空转也会全绿**。
#         `_t0/_p0` 那行"迁移前"打印也失去意义（打印出来就是 disabled）。
#   这是「独立验证必要条件缺失」：判据通过不证明被测行为发生。
#   ⇒ 修法：**强制把临时库置成"改动前"状态**（tools=active / plugins=published），
#     再跑迁移。这样"跑完变 disabled"才真正证明迁移有效。
#     ⚠️ 这个前置必须写成断言（D0b），否则将来真库/夹具再变一次，整组又会静默假绿。
TMP = mkdtemp(prefix="verify_ck_retired_")
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
    # ★ 2026-11-26 新增「判据面存在」守卫（干净库口径实测暴露的假红）：
    #   `plugins` 里那条 `com.zhiyuan.legacy.tool.conflict-check` 是**生产库存量数据**
    #   （某个历史 legacy 插件），`init_db()` 在干净库上**不会种它**
    #   ⇒ CI 等价口径（MBSE_DB_PATH 指向空库）下 D2/D4/D5 必然 "got=None" / "0 < 17"。
    #   那不是产品问题，是**判据面不存在** ⇒ 应 SKIP。
    #   ⚠️ 但「生产口径下插件行不存在」就是真问题（有人删了行，违反"只下线不删行"）
    #      ⇒ 所以守卫必须**先判是不是生产库**，不能一律 SKIP。
    # ⚠️ 判「是不是生产口径」**不能**用 agents 数量：`init_db()` 在干净库上也种出
    #    25 个 agent（7 个种子 + 资产自举）⇒ `agents>=20` 在干净库同样成立
    #    （本轮第一版就是这么写的，SKIP 守卫完全没生效，干净库照旧 6 条假红）。
    #    改用**只有真实使用才会产生的证据**：tool_call_logs 行数。
    #    干净库 init_db 后该表恒为 0；生产库有真实调用留痕（本工具自己就有 18 条）。
    _n_logs_tmp = _c.execute("SELECT COUNT(*) FROM tool_call_logs").fetchone()[0]
    _n_agents_tmp = _c.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
    _is_prod_like = _n_logs_tmp > 0
    _has_plugin_row = _c.execute(
        "SELECT COUNT(*) FROM plugins WHERE name='conflict-check'").fetchone()[0] > 0
    _can_assert = _has_plugin_row or _is_prod_like

    def check_d(name, cond, detail=""):
        """D/E/F 组专用：判据面不存在时 SKIP 而不是 FAIL（见 D 组守卫注释）。"""
        if _can_assert:
            check(name, cond, detail)
        else:
            skip(name, "干净库无 legacy 插件行 ⇒ 判据面不存在（不是产品问题）")

    if not _has_plugin_row:
        if _is_prod_like:
            check("D0a 生产口径下插件行必须仍在（只下线不删行）", False,
                  "有 %d 条工具调用留痕（生产口径）但 plugins 里没有 conflict-check 行"
                  " ⇒ 行被删了" % _n_logs_tmp)
        else:
            skip("D1-D7 迁移行为",
                 "干净库无 legacy 插件行（initial_db 不种存量插件）⇒ 判据面不存在")
    # ── 构造「改动前」基准（这是本组判据成立的前提）──
    _c.execute("UPDATE tools SET status='active' WHERE name='conflict_check'")
    _c.execute("UPDATE plugins SET status='published' WHERE name='conflict-check'")
    _c.commit()
    _t0 = _c.execute("SELECT status FROM tools WHERE name='conflict_check'").fetchone()
    _p0 = _c.execute("SELECT status FROM plugins WHERE name='conflict-check'").fetchone()
    print("  迁移前（强制构造的基准）：tools=%s｜插件=%s"
          % (_t0["status"] if _t0 else "无", _p0["status"] if _p0 else "无"))
    check_d("D0b ★基准确实是『改动前』状态（否则后面的『跑完 disabled』证明不了任何事）",
          bool(_t0) and _t0["status"] == "active"
          and bool(_p0) and _p0["status"] == "published",
          "基准构造失败：tools=%s plugins=%s"
          % (_t0["status"] if _t0 else None, _p0["status"] if _p0 else None))

    _fn = _load_migrate_fn(m_src)
    _fn(_c)
    _fn(_c)   # 幂等性（第二次不得报错/不得把别的行也改掉）

    _t1 = _c.execute("SELECT status FROM tools WHERE name='conflict_check'").fetchone()
    _p1 = _c.execute("SELECT status FROM plugins WHERE name='conflict-check'").fetchone()

    check_d("D1 tools 行已 disabled", _t1 and _t1["status"] == "disabled",
          "got=%s" % (_t1["status"] if _t1 else None))
    check_d("D2 ★ 插件行也 disabled（否则仍会被注入）",
          _p1 and _p1["status"] == "disabled", "got=%s" % (_p1["status"] if _p1 else None))
    check_d("D3 只下线不删行（tools 行保留，历史流水不悬空）",
          _c.execute("SELECT COUNT(*) FROM tools WHERE name='conflict_check'").fetchone()[0] == 1)
    check_d("D4 只下线不删行（plugins 行保留）",
          _c.execute("SELECT COUNT(*) FROM plugins WHERE name='conflict-check'").fetchone()[0] == 1)
    # ★ 2026-11-26 修 D5：原判据 `== 17`（钉死数字）。实测已 18 条
    #   （之后又发生过调用 / 或历史清理过一次再增长）。
    #   本判据的**真实语义**是「历史调用留痕不许被清理」（下线 ≠ 抹掉历史证据），
    #   ⇒ 方向性判据：**只增不减**（≥ 登记值），不锁具体数字。
    _LOGS_BASE = 17
    _logs_n = _c.execute("SELECT COUNT(*) FROM tool_call_logs "
                         "WHERE tool_name='conflict_check'").fetchone()[0]
    check_d("D5 历史调用留痕未被删（≥ 登记值；下线工具不等于抹掉历史证据）",
          _logs_n >= _LOGS_BASE, "实测 %d < 登记 %d ⇒ 历史被清理过" % (_logs_n, _LOGS_BASE))
    print("    历史留痕：%d 条（登记值 %d，只增不减）" % (_logs_n, _LOGS_BASE))
    _c.commit()
    _c.close()

    from agent.pipeline_parts.tools import ToolMixin  # noqa: E402

    class _M(ToolMixin):
        def __init__(self):
            self.registry = type("R", (), {
                "get_bound_mcp": lambda s, i: [],
                "get": lambda s, i: type("A", (), {"tools": ["graph_retrieve", "entity_create"]})(),
            })()

    _names = [d["function"]["name"] for d in _M()._build_tools_def("requirement_analysis", "")]
    check_d("D6 ★ payload 里不再有 conflict_check（两条路径都断了）",
          "conflict_check" not in _names,
          "仍被注入（共 %d 个工具）" % len(_names))
    check_d("D7 其余工具未被误伤（graph_retrieve 等仍在）", "graph_retrieve" in _names)
finally:
    shutil.rmtree(TMP, ignore_errors=True)
    os.environ.pop("MBSE_DB_PATH", None)

print("\n=== E. 真库现状（只读）===")
# ★ 2026-11-26 修 E1 的**方向**（与 verify_fact_inference 的 F1 同族）：
#   原判据断言「真库基线为 active/published」，理由是"记录改动前基线（本次修复的由来）"。
#   它守护的是**改动前的历史时刻** ⇒ 迁移一旦在服务重启时执行，真库变成 disabled
#   ⇒ E1 恒红。而 disabled/disabled 恰恰是**目标状态**，是期望结果。
#   「变好」与「口径漂移」在原判据下信号相同（双边/反向判据的通病）。
#   ⇒ 翻转为正向：真库必须**已经是** disabled/disabled（迁移在生产库上生效了）。
#     这才是本门禁真正要守的：不只是代码里有迁移，而是它**在生产数据上跑过**。
if os.path.exists(PROD_DB):
    _pc = sqlite3.connect("file:%s?mode=ro" % PROD_DB, uri=True)
    _pc.row_factory = sqlite3.Row
    _t = _pc.execute("SELECT status FROM tools WHERE name='conflict_check'").fetchone()
    _p = _pc.execute("SELECT status FROM plugins WHERE name='conflict-check'").fetchone()
    print("  真库：tools=%s｜插件=%s"
          % (_t["status"] if _t else "无", _p["status"] if _p else "无"))
    check_d("E1 真库迁移已生效（tools=disabled 且 plugins=disabled）",
          bool(_t) and _t["status"] == "disabled"
          and bool(_p) and _p["status"] == "disabled",
          "真库仍可注入：tools=%s plugins=%s ⇒ 服务未重启或迁移未接到 init_db"
          % (_t["status"] if _t else None, _p["status"] if _p else None))
    # E2 反证：迁移**没有**把别的工具/插件顺手关掉（限定 WHERE，不误伤）
    _others = _pc.execute(
        "SELECT COUNT(*) FROM tools WHERE name<>'conflict_check' AND status='disabled'"
    ).fetchone()[0]
    _total = _pc.execute("SELECT COUNT(*) FROM tools").fetchone()[0]
    print("    其余 disabled 工具：%d / %d（非本次迁移造成，仅记录）" % (_others, _total))
    _pc.close()
else:
    print("  [SKIP] 未找到真库")

print("\n=== F. 变异自证（只下线 tools、漏掉 plugins）===")
# ⚠️ 两条历史教训（都还适用）：
#   ① 变异体只能做**语法合法**的最小编辑：整段替换 SQL 字符串会让后续括号变裸标识符
#      ⇒ SyntaxError，门禁只报"变异体执行失败"，抓不到语义。
#   ② 变异体必须**内存现生成**：写到 tmp/ 会因 gitignore 在 CI 上不存在
#      ⇒ 走 [VACUOUS] 静默失去自证（判据空转）。
# ★ 2026-11-26 第三条（本轮新踩）：**变异体的期望值不能依赖真库当前状态**。
#   原 F1 期待"插件仍 published"，而真库早已 disabled ⇒ 变异体即使正确生效，
#   插件也只是"保持 disabled" ≠ published ⇒ F1 恒红（看起来像"自证无效"，真因是基准过期）。
#   ⇒ 与 D 组同一套做法：变异体在临时库上**先强制置 published**，再跑变异函数。
#     这样"变异后仍是 published / 正常后变 disabled"的对比才成立。
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
    check_d("F0 变异锚点存在于迁移源码（自证前提）", False, "锚点缺失 ⇒ 自证中止")
else:
    try:
        _fnbody = ("def _retire_fake_conflict_check"
                   + _mut_src.split("def _retire_fake_conflict_check", 1)[1].split("\ndef ", 1)[0])
        compile(_fnbody, "<mut>", "exec")
    except SyntaxError as _se:
        print("  [VACUOUS] 变异体语法错误：%s" % _se)
        check_d("F0 变异体语法合法（自证前提）", False, str(_se))
        _fnbody = None
    if _fnbody:
        T2 = mkdtemp(prefix="verify_ck_mut_")
        D2 = os.path.join(T2, "m.db")
        try:
            _s = sqlite3.connect("file:%s?mode=ro" % PROD_DB, uri=True)
            _d = sqlite3.connect(D2)
            _s.backup(_d)
            _d.close()
            _s.close()
            _mc = sqlite3.connect(D2)
            _mc.row_factory = sqlite3.Row
            # ★ 构造与 D 组同一套「改动前」基准（不依赖真库现状）
            _mc.execute("UPDATE tools SET status='active' WHERE name='conflict_check'")
            _mc.execute("UPDATE plugins SET status='published' WHERE name='conflict-check'")
            _mc.commit()
            _ns2 = {"__name__": "_m2"}
            exec(compile(_fnbody, "<mut>", "exec"), _ns2)
            _ns2["_retire_fake_conflict_check"](_mc)
            _ps = _mc.execute("SELECT status FROM plugins WHERE name='conflict-check'").fetchone()
            _ts = _mc.execute("SELECT status FROM tools WHERE name='conflict_check'").fetchone()
            print("  变异体（漏 plugins）：tools=%s｜插件=%s"
                  % (_ts["status"] if _ts else "无", _ps["status"] if _ps else "无"))
            check_d("F1 变异体（漏掉 plugins）上 D2 必须 FAIL（插件仍 published）",
                  bool(_ps) and _ps["status"] == "published",
                  "变异体里插件也变 disabled ⇒ 变异没打中或基准没构造好，自证无效")
            check_d("F2 对照：变异体里 tools 行确实已下线（证明只差 plugins 那一步）",
                  bool(_ts) and _ts["status"] == "disabled",
                  "got=%s" % (_ts["status"] if _ts else None))
            _mc.close()
            _muts.append(("M1", "退役迁移漏掉 plugins 侧（P1-6 路径仍会注入）"))
            print("  [SELF-PROOF] M1 只下线 tools 不下线插件 —— 该工具会经「安装即可消费」"
                  "重新进入 payload，证明白名单路径必须一起处理")
        except Exception as e:
            check_d("F1 变异体执行成功", False, str(e))
        finally:
            shutil.rmtree(T2, ignore_errors=True)

print("\n" + "=" * 60)
print("常态断言：%d 条，%d 通过 / %d 失败" % (_ok + _fail, _ok, _fail))
print("变异自证：%d 组" % len(_muts))
for mid, desc in _muts:
    print("  - %s %s" % (mid, desc))
_done = (_fail == 0 and (_muts or not _can_assert))
print("结论：%s%s" % (
    "门禁通过" if _done else "门禁未通过",
    "" if _muts else "（本环境判据面不存在，变异自证未执行）"))
sys.exit(0 if _done else 1)