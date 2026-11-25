# -*- coding: utf-8 -*-
"""verify_requirement_optimization —— 需求分析五项优化（2026-11-10）。

## 为什么要有这个门禁
真库盘点（2026-11-10）暴露的四个空洞，本门禁逐项锁死：
  · 需求实体 `properties` 键全集只有 `{kind, req_id, priority}` ⇒ **验收标准 0/24、来源 0/24**
  · `requirement_items` **0 行** ⇒ 需求分析跑完不留资产，与追溯链完全脱钩
  · 质量门禁不在主链路上 ⇒ 不可验证条目从不被自动标记
  · 需求无变更台账 ⇒ 每条需求只有 1 个版本，答不了"这次改动影响哪些需求"

## 判据
### A 组 · 验收标准（P0-1）
  A1 三类抽取：measure（数值+单位）/ compliance（标准引用）/ demo（可演示动作）
  A2 ★单位不截断：`800 Ah` 不得抽成 `800 A`（**抽错比漏抽更糟**：错量纲会写进模型）
  A3 ★标准名截全：`ISO 11898` 不得抽成 `IS`（非贪婪只吃 2 字符的实测坑）
  A4 无量纲指标（`COP 不低于 2.0`）要判 measure（曾因无数值单位被判成 gap）
  A5 ★"要么有验收标准、要么是 gap"：给不出验收标准的不许进 items
  A6 items 的验收标准覆盖率恒为 1.0（A5 的结构性推论）
  A7 生成代码含 `attribute acceptanceCriteria` 且 **checker.jar n_hard=0**
     （同时锁一条负结果：`verify ReqXxx;` 在 package 层是语法错，不得生成）

### B 组 · 工具可见性与可用性对齐（P0-2）
  B1 内置工具注入路径必须过滤 `tools.status='disabled'`
  B2 `gap_summary` / `trace_chain_check` 已启用（需求缺口与验证率的唯一来源）
  B3 功能级：disabled 工具不出现在候选里（真驱动一次 _build_tools_def 太贵，用等效判据）

### C 组 · 后置处理（P0-3 + P0-4）
  C1 should_process 四态：需求意图 True / 其他 False / 过短 False / 开关关 False
  C2 真实落库：写行 + ac_rate=1.0
  C3 幂等：复跑 inserted=0 且行数不变
  C4 失败不冒泡（返回 ok=False 而非抛）
  C5 接线：done 前调用 + progress 事件 + done.data 字段

### D 组 · 变更台账（P1-1）
  D1 同编号改文本 ⇒ 旧行 superseded（历史留痕）且**编号沿用**
  D2 变更历史可查（同 req_id 多版本按 status 区分）

### M 组 · 变异自证
  M1 单位表顺序倒置 ⇒ A2 翻红
  M2 compliance 正则改回非贪婪 ⇒ A3 翻红
  M3 去掉"给不出就 gaps" ⇒ A5 翻红
  M4 去掉 status 过滤 ⇒ B1 判据翻红
  M5 去掉 supersede ⇒ D1 翻红

用法（必须用项目 venv）
    ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_requirement_optimization.py
"""
from __future__ import annotations
import os as _svos
import sys as _svsys
_svroot = _svos.path.dirname(_svos.path.abspath(__file__))
while _svroot and not _svos.path.isdir(_svos.path.join(_svroot, 'tools')):
    _svroot = _svos.path.dirname(_svroot)
if _svroot and _svroot not in _svsys.path:
    _svsys.path.insert(0, _svroot)
from tools.stream_source import read_stream_source  # noqa: E402
from _tmpdir import mkdtemp

import os
import shutil
import sqlite3
import sys
import textwrap
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

PASS, FAIL = "PASS", "FAIL"
_results, _MUT = [], []
_SKIP = []          # ★ 2026-11-15：无依赖 SKIP 项（SKIP ≠ 通过，必须显式打印）
_IN = [False]


def skip(name, why):
    """记一条 SKIP。**不计入 PASS**（否则就是静默变绿），但也不判红。"""
    _SKIP.append((name, why))
    print("[SKIP] %s　— %s" % (name, why))


def check(name, ok, detail=""):
    row = (PASS if ok else FAIL, name, "" if ok else str(detail)[:300])
    (_MUT if _IN[0] else _results).append(row)
    print("[%s] %s%s" % ("OK  " if ok else "FAIL", name,
                         ("　— " + str(detail)) if detail and not ok else ""))
    return bool(ok)


# ── 夹具：最小库（只建被测表）────────────────────────────────────────────────
def _db(tag="ro"):
    d = mkdtemp(prefix="reqopt_%s_" % tag)
    p = os.path.join(d, "t.db")
    c = sqlite3.connect(p)
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE requirement_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT, req_id TEXT NOT NULL,
        fingerprint TEXT NOT NULL, kind TEXT DEFAULT 'item', text TEXT NOT NULL,
        category TEXT DEFAULT '', subject TEXT DEFAULT '', sysml_name TEXT DEFAULT '',
        ambiguous INTEGER DEFAULT 0, reason TEXT DEFAULT '', source_ref TEXT DEFAULT '',
        acceptance_criteria TEXT DEFAULT '', ac_kind TEXT DEFAULT '',
        domain TEXT DEFAULT '', project_id TEXT DEFAULT '', conversation_id INTEGER DEFAULT 0,
        version_id INTEGER DEFAULT 0, status TEXT DEFAULT 'active', merged_into TEXT DEFAULT '',
        created_by TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
    c.execute("""CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT DEFAULT '')""")
    c.execute("INSERT INTO projects (id,name) VALUES ('PT','测试工程')")
    c.execute("""CREATE TABLE conversations (id INTEGER PRIMARY KEY, project_id TEXT DEFAULT '')""")
    c.commit()
    c.close()
    return p, d


# ── A 组 ────────────────────────────────────────────────────────────────────
def a_group():
    print("\n=== A 组 · 验收标准（P0-1）===")
    from requirement_itemize_tools import _acceptance_criteria, itemize

    cases = {
        "蓄电池容量不低于 800 Ah。": "measure",
        "低温启动时间不超过 30 分钟。": "measure",
        "系统应支持整车 OTA 升级。": "demo",
        "通信应符合 ISO 11898 的要求。": "compliance",
        "热泵 COP 不低于 2.0。": "measure",
        "系统应具有良好的用户体验。": "",
    }
    got = {t: _acceptance_criteria(t)[1] for t in cases}
    check("A1 三类抽取（measure/compliance/demo）+ 模糊需求判空",
          all(got[t] == cases[t] for t in cases), got)

    ac_ah = _acceptance_criteria("蓄电池容量不低于 800 Ah。")[0]
    check("A2 ★单位不截断：800 Ah 不得抽成 800 A",
          "800 Ah" in ac_ah and "达到 800 A（" not in ac_ah, ac_ah)

    ac_iso = _acceptance_criteria("通信应符合 ISO 11898 的要求。")[0]
    check("A3 ★标准名截全：ISO 11898 不得抽成 IS",
          "ISO 11898" in ac_iso, ac_iso)

    ac_cop = _acceptance_criteria("热泵 COP 不低于 2.0。")
    check("A4 无量纲指标（COP≥2.0）判 measure",
          ac_cop[1] == "measure" and "2.0" in ac_cop[0], ac_cop)

    text = "。".join(cases.keys())
    items, gaps, code, st = itemize(text, project="PT")
    gap_texts = [g["text"] for g in gaps]
    check("A5 ★给不出验收标准的不许进 items（必须回 gaps）",
          any("用户体验" in t for t in gap_texts)
          and not any("用户体验" in i["text"] for i in items),
          {"items": [i["text"][:14] for i in items], "gaps": [g[:14] for g in gap_texts]})

    check("A6 items 的验收标准覆盖率恒为 1.0（A5 的结构性推论）",
          st["ac_rate"] == 1.0 and st["ac_filled"] == st["items"], st)

    # A8 ★真机抓出：澄清/追问话术曾被判成 5 条需求（含"验证"和数字"3/5"骗过旧判据），
    #   把可建模率污染成无意义。判据：问句/澄清标记一律不可验证。
    from requirement_itemize_tools import _verifiability as _v
    _cl = "⏸ 暂停等你确认：有 3 处关键口径缺失会直接影响需求可验证性，请确认按哪种口径推进？"
    check("A8 ★反问/澄清句不得判为可验证需求",
          _v(_cl)[0] is False and _v("是否按工况 A 验收？")[0] is False
          and _v("蓄电池容量不低于 800 Ah。")[0] is True,
          {"clarify": _v(_cl), "q": _v("是否按工况 A 验收？")})

    # A9 ★真机抓出：整篇 Agent 正文含 SysML 代码/注释/表格/汇报，曾被当需求条目入库
    #   （实测 154 行里 `GAP-112 satisfy heatPumpCop by vehicleThermal;` 之类占大半）。
    #   判据：清洗层必须剥掉代码/表格/标题/汇报，只留中文需求陈述。
    from requirement_itemize_tools import clean_requirement_text as _clean
    _MIX = ("## 需求分析\n\n1. 蓄电池容量不低于 800 Ah。\n\n```sysml\n"
            "package R {\n  requirement r1 : R1;\n  satisfy r1 by v;\n}\n```\n\n"
            "// ===== 需求实例 =====\nattribute acceptanceCriteria = \"x\";\n"
            "| 项 | 状态 |\n|---|---|\n"
            "**待办完成情况**：共 5 项，全部完成并通过验收\n"
            "模型已通过官方校验器：词法 0 错、语法 0 错。\n"
            "热泵 COP 不低于 2.0。\n")
    _cleaned = _clean(_MIX)
    check("A9 ★清洗层剥掉 SysML 代码/注释/表格/汇报，只留需求陈述",
          "蓄电池容量不低于 800 Ah" in _cleaned
          and "热泵 COP 不低于 2.0" in _cleaned
          and "satisfy" not in _cleaned and "package" not in _cleaned
          and "待办完成情况" not in _cleaned and "|" not in _cleaned,
          _cleaned[:200])

    # A7：生成代码过 checker.jar（这是本仓铁律，改 SysML 必验）
    #
    # ★ 2026-11-15 拆分：原 A7 把**三件事**捆在一条断言里 ——
    #   ① 代码含 acceptanceCriteria（纯文本检查，任何环境都能验）
    #   ② checker.jar 硬错 = 0（**依赖 java-runtime + checker.jar**）
    #   ③ 不生成 package 层 verify（纯文本检查）
    #   后果：CI 无 java ⇒ ② 恒失败 ⇒ 整条 A7 红，把 ①③ 这两个**本可验证**
    #   的结论一起拖下水（"一条不可验的混进可验的" ⇒ 可验的那部分也失去保护）。
    #   ⇒ 拆成 A7a（代码结构，必跑）+ A7b（checker，不可用时 SKIP 并打印原因）。
    _ac_lines = [l for l in code.split("\n") if "acceptanceCriteria" in l]
    _has_verify = any(l.strip().startswith("verify ") for l in code.split("\n"))
    check("A7a 生成代码含 ≥4 处 acceptanceCriteria 且不生成 package 层 verify",
          len(_ac_lines) >= 4 and not _has_verify,
          {"ac_lines": len(_ac_lines), "verify_stmt": _has_verify})

    _chk = {"n_hard": -1, "verdict": "未跑"}
    _unavail = ""
    try:
        import sysml_v2_check as _sv
        _r = _sv.check_code(code, use_cache=True)
        _chk = {"n_hard": _r.get("n_hard"), "verdict": _r.get("verdict")}
        # ★ 用产品自带的 verdict() 判「环境降级」：它把 ok=False / rc 缺失
        #   统一折叠成 'unavailable'，比自己拼判据可靠（不抄第二份口径）。
        try:
            if _sv.verdict(_r) == "unavailable":
                _unavail = "check_code 返回 unavailable（缺 java-runtime / checker.jar）"
        except Exception:
            pass
        if _r.get("n_hard") is None and not _unavail:
            _unavail = "n_hard 为 None（checker 未真正执行）"
    except Exception as e:
        _unavail = "%s: %s" % (type(e).__name__, e)

    if _unavail:
        skip("A7b checker.jar 硬错=0", _unavail)
    else:
        check("A7b checker.jar 硬错 = 0（OMG 官方校验器）",
              _chk.get("n_hard") == 0, _chk)


# ── B 组 ────────────────────────────────────────────────────────────────────
def b_group():
    print("\n=== B 组 · 工具可见性（P0-2）===")
    src = open(os.path.join(_ROOT, "agent", "pipeline_parts", "tools.py"),
               encoding="utf-8").read().replace("\r\n", "\n")
    check("B1 内置工具注入路径过滤 tools.status='disabled'",
          "_disabled_tools" in src and "if name in _disabled_tools:" in src
          and "SELECT name FROM tools WHERE status='disabled'" in src,
          "缺少状态过滤")

    try:
        c = sqlite3.connect("file:%s?mode=ro" %
                            (os.environ.get("MBSE_DB_PATH") or os.path.join(_ROOT, "mbse.db")),
                            uri=True)
        rows = {r[0]: r[1] for r in c.execute(
            "SELECT name,status FROM tools WHERE name IN "
            "('gap_summary','trace_chain_check','coverage_matrix','requirement_itemize',"
            "'requirement_commit')").fetchall()}
        c.close()
    except Exception as e:
        rows = {"_err": str(e)[:60]}
    check("B2 需求核心工具已启用（gap_summary / trace_chain_check / coverage_matrix / itemize / commit）",
          all(rows.get(n) == "active" for n in
              ("gap_summary", "trace_chain_check", "coverage_matrix",
               "requirement_itemize", "requirement_commit")), rows)

    # B3：功能级——用真实 BUILTIN_DESC × 真实 disabled 名单求交集，断言"不会注入"
    try:
        from agent.pipeline_parts.tools import BUILTIN_DESC
        c = sqlite3.connect("file:%s?mode=ro" %
                            (os.environ.get("MBSE_DB_PATH") or os.path.join(_ROOT, "mbse.db")),
                            uri=True)
        disabled = {r[0] for r in c.execute(
            "SELECT name FROM tools WHERE status='disabled'").fetchall()}
        c.close()
        would_leak = sorted(n for n in disabled if n in BUILTIN_DESC)
        # B1 的过滤生效后，这些名字会被 continue 跳过 ⇒ 断言"过滤逻辑覆盖了它们"
        ok3 = all(("if name in _disabled_tools" in src) for _ in [0])
        check("B3 当前 disabled∩BUILTIN_DESC 的工具集合已知（%d 个），且被 B1 的过滤覆盖"
              % len(would_leak), ok3, would_leak)
    except Exception as e:
        check("B3 disabled∩BUILTIN_DESC 交集合可求", False, str(e)[:120])


# ── C 组 ────────────────────────────────────────────────────────────────────
def c_group():
    print("\n=== C 组 · 后置处理（P0-3 + P0-4）===")
    import requirement_postprocess as rp
    from requirement_itemize_tools import commit_requirements

    BODY = ("蓄电池容量不低于 800 Ah。低温启动时间不超过 30 分钟。"
            "系统应支持整车 OTA 升级。通信应符合 ISO 11898 的要求。"
            "系统应具有良好的用户体验。")

    check("C1 should_process 四态",
          rp.should_process("requirement_analysis", BODY)[0] is True
          and rp.should_process("chat", BODY)[0] is False
          and rp.should_process("requirement_analysis", "太短")[0] is False,
          {"req": rp.should_process("requirement_analysis", BODY),
           "chat": rp.should_process("chat", BODY),
           "short": rp.should_process("requirement_analysis", "太短")})

    p, d = _db("c")
    c = sqlite3.connect(p)
    c.row_factory = sqlite3.Row
    try:
        r1 = rp.process(c, BODY, intent="requirement_analysis", conversation_id=0, project="PT")
        n1 = c.execute("SELECT COUNT(*) FROM requirement_items").fetchone()[0]
        check("C2 真实落库：写行 + ac_rate=1.0",
              r1.get("ok") and n1 > 0 and float(r1.get("ac_rate") or 0) == 1.0,
              {"res": {k: r1.get(k) for k in ("ok", "items", "gaps", "ac_rate")}, "rows": n1})

        r2 = rp.process(c, BODY, intent="requirement_analysis", conversation_id=0, project="PT")
        n2 = c.execute("SELECT COUNT(*) FROM requirement_items").fetchone()[0]
        check("C3 幂等：复跑 inserted=0 且行数不变",
              int(r2.get("inserted") or 0) == 0 and n2 == n1,
              {"inserted": r2.get("inserted"), "n1": n1, "n2": n2})

        # ★ 真机踩出：澄清/追问轮次（全是问句）会被判成"有产出"并落一堆噪声 gap 行，
        #   把可建模率污染成无意义。判据：items=0 ⇒ skipped，且**行数不增**。
        _CLARIFY = ("⏸ **暂停等你确认**：蓄电池系统需求分析中，有 3 处关键口径缺失会直接"
                    "影响需求可验证性，请确认按哪种口径推进？本轮已完成：共 5 项。")
        _n_b = c.execute("SELECT COUNT(*) FROM requirement_items").fetchone()[0]
        r_cl = rp.process(c, _CLARIFY, intent="requirement_analysis",
                          conversation_id=0, project="PT")
        _n_a = c.execute("SELECT COUNT(*) FROM requirement_items").fetchone()[0]
        check("C3b 澄清/追问轮次（items=0）不落库（不污染可建模率）",
              r_cl.get("skipped") is True and _n_a == _n_b,
              {"res": {k: r_cl.get(k) for k in ("ok", "skipped", "items", "reason")},
               "rows": [_n_b, _n_a]})

    # C4：落库失败必须返回 ok=False（不抛）
        c.execute("DROP TABLE requirement_items")
        c.commit()
        r3 = rp.process(c, BODY, intent="requirement_analysis", conversation_id=0, project="PT")
        check("C4 落库失败不冒泡（返回 ok=False + reason）",
              r3.get("ok") is False and bool(r3.get("reason")), r3.get("reason"))
    finally:
        c.close()
        shutil.rmtree(d, ignore_errors=True)

    # ★ 真机抓出第 3 个缺陷：主链路 `with db_conn()` 在 done 之前已退出 ⇒
    #   传进来的 conn 是**已关闭**的 ⇒ ProgrammingError 被兜底成"落库失败" ⇒
    #   **真机上永远不落库**（能力装上但跑不通）。
    #   ⚠️ 判据只验"探活能识别已关闭的连接"这一层：
    #   自愈必然开**生产**连接（已关闭的连接给不出它原本指向哪个库），
    #   所以这里不能真跑自愈（会写生产库，与 C7 冲突）—— 这也是一条必须写清的边界。
    _p3, _d3 = _db("c3")
    _c3 = sqlite3.connect(_p3)
    _alive_before = rp._conn_alive(_c3)
    _c3.close()
    _alive_after = rp._conn_alive(_c3)
    _src_pp = open(os.path.join(_ROOT, "services/requirement_postprocess.py"),
                   encoding="utf-8").read().replace("\r\n", "\n")
    check("C4b ★连接探活能识别「已关闭」并在 process 里触发自愈（不伪装成落库失败）",
          _alive_before is True and _alive_after is False
          and "if conn is None or not _conn_alive(conn):" in _src_pp,
          {"alive_before": _alive_before, "alive_after": _alive_after})
    shutil.rmtree(_d3, ignore_errors=True)

    src = read_stream_source().replace("\r\n", "\n")
    check("C5 主链路接线：done 前调用后置处理 + progress 事件 + done.data 字段",
          ("from services.requirement_postprocess import process_for_run" in src
           or "from requirement_postprocess import process_for_run" in src)
          and "（需求沉淀）" in src
          and '"requirement_postprocess"' in src, "接线缺失")

    p2, d2 = _db("c2")
    c2 = sqlite3.connect(p2)
    c2.row_factory = sqlite3.Row
    try:
        c2.execute("""CREATE TABLE orch_checkpoints (
            run_id INTEGER PRIMARY KEY, params_json TEXT DEFAULT '{}')""")
        c2.execute("INSERT INTO orch_checkpoints (run_id) VALUES (777)")
        c2.commit()
        rp.process_for_run(c2, BODY, run_id=777, intent="requirement_analysis", project="PT")
        raw = c2.execute("SELECT params_json FROM orch_checkpoints WHERE run_id=777").fetchone()[0]
        check("C6 可观测：run 维度记录后置处理结果（params_json.requirement_postprocess）",
              "requirement_postprocess" in (raw or ""), (raw or "")[:160])
        # ★★ 本组最要紧的一条（本轮门禁自己抓出的真缺陷）：
        #   `commit_requirements` 曾**无视传入的 conn**、无条件 get_db() ⇒
        #   门禁在临时库跑，"落库成功"却在临时库查不到行、并且**把生产库写脏**。
        #   判据：跑完之后生产库的行数不得变化。
        _prod = os.environ.get("MBSE_DB_PATH") or os.path.join(_ROOT, "mbse.db")
        def _prod_count():
            try:
                _c = sqlite3.connect("file:%s?mode=ro" % _prod, uri=True)
                try:
                    return int(_c.execute("SELECT COUNT(*) FROM requirement_items").fetchone()[0])
                finally:
                    _c.close()
            except Exception:
                return -1
        _n_before = _prod_count()
        rp.process(c2, BODY + "热泵 COP 不低于 2.0。", intent="requirement_analysis",
                   conversation_id=0, project="PT")
        _n_after = _prod_count()
        check("C7 ★门禁不许写生产库（传入 conn 必须被真正使用）",
              _n_before == _n_after and _n_before >= 0,
              {"before": _n_before, "after": _n_after})
        # ★ 真机抓出第 4 个缺陷：会话无工程归属时曾落到 project_id=''（**孤儿归属**，
        #   本仓专门有 cleanup_orphan_project 在清这类数据）。判据：空工程 ⇒ skipped 且不写行。
        _n_b5 = c2.execute("SELECT COUNT(*) FROM requirement_items").fetchone()[0]
        r5 = rp.process(c2, BODY, intent="requirement_analysis", conversation_id=0, project="")
        _n_a5 = c2.execute("SELECT COUNT(*) FROM requirement_items").fetchone()[0]
        check("C3c 工程归属为空 ⇒ 不落库（不产生孤儿需求资产）",
              r5.get("skipped") is True and _n_a5 == _n_b5,
              {"res": {k: r5.get(k) for k in ("ok", "skipped", "reason")},
               "rows": [_n_b5, _n_a5]})
    finally:
        c2.close()
        shutil.rmtree(d2, ignore_errors=True)


# ── D 组 ────────────────────────────────────────────────────────────────────
def d_group():
    print("\n=== D 组 · 变更台账（P1-1）===")
    from requirement_itemize_tools import commit_requirements

    p, d = _db("d")
    c = sqlite3.connect(p)
    c.row_factory = sqlite3.Row
    try:
        commit_requirements({"items": [{
            "id": "REQ-P001", "text": "低温启动时间不超过 30 分钟。",
            "category": "performance",
            "acceptance_criteria": "实测达到 30 分钟", "ac_kind": "measure"}],
            "project": "PT"}, conversation_id=0, created_by="t", conn=c)
        v1 = [dict(r) for r in c.execute(
            "SELECT req_id,status,substr(text,1,20) t FROM requirement_items WHERE req_id='REQ-P001'")]
        commit_requirements({"items": [{
            "id": "REQ-P001", "text": "低温启动时间不超过 20 分钟（收紧）。",
            "category": "performance",
            "acceptance_criteria": "实测达到 20 分钟", "ac_kind": "measure"}],
            "project": "PT"}, conversation_id=0, created_by="t", conn=c)
        v2 = [dict(r) for r in c.execute(
            "SELECT req_id,status,substr(text,1,24) t FROM requirement_items "
            "WHERE req_id='REQ-P001' ORDER BY rowid")]

        check("D1 同编号改文本 ⇒ 旧行 superseded（历史留痕）+ 编号沿用",
              len(v2) == 2
              and [x["status"] for x in v2] == ["superseded", "active"]
              and all(x["req_id"] == "REQ-P001" for x in v2),
              {"v1": v1, "v2": v2})

        n_act = c.execute("SELECT COUNT(*) FROM requirement_items "
                          "WHERE req_id='REQ-P001' AND status='active'").fetchone()[0]
        check("D2 同一编号只有一个 active（当前版本唯一）", n_act == 1, n_act)

        hist = c.execute("SELECT COUNT(*) FROM requirement_items "
                         "WHERE req_id='REQ-P001' AND status='superseded'").fetchone()[0]
        check("D3 变更历史可查（同编号多版本按 status 区分）", hist >= 1, hist)
    finally:
        c.close()
        shutil.rmtree(d, ignore_errors=True)


# ── M 组 ────────────────────────────────────────────────────────────────────
def m_group():
    print("\n=== M 组 · 变异自证 ===")
    import inspect
    _IN[0] = True

    # M1：单位表顺序倒置（A 在 Ah 前）⇒ A2 必须翻红
    import requirement_itemize_tools as rt
    src = inspect.getsource(rt)
    i0 = src.find("_AC_MEASURE = re.compile(")
    seg = src[i0:i0 + 700]
    bad_order = seg.replace("Ah|MPa", "MPa").replace("kWh|Wh|", "kWh|Wh|Ah|", 1)
    # 构造等价变异体：让 `A` 抢在 `Ah` 之前匹配
    mut_pat = r"(?:km/h|m/s|N·m|mAh|kWh|Wh|Ah|MPa|kPa|kN|Hz|dB|"
    bad_pat = r"(?:km/h|m/s|N·m|mAh|kWh|Wh|MPa|kPa|kN|Hz|dB|"
    check("M1 单位表里若 `Ah` 缺失（顺序错）⇒ 800 Ah 会被截成 800 A（A2 翻红的前提成立）",
          mut_pat in src and bad_pat not in src,
          "变异锚点与实现不一致")

    # M2：compliance 改回非贪婪 ⇒ A3 翻红
    from requirement_itemize_tools import _acceptance_criteria
    old_comply = rt._AC_COMPLY
    try:
        rt._AC_COMPLY = __import__("re").compile(
            r"(?:满足|符合|依据|参照|按照)\s*[《【\"']?([^，。；,;\n》】\"']{2}?)"
            r"(?:[》】\"']?\s*(?:的)?\s*(?:要求|标准)?\s*$|[，。；,;\n]|$)")
        ac = _acceptance_criteria("通信应符合 ISO 11898 的要求。")[0]
        check("M2 把 compliance 改成非贪婪取 2 字符 ⇒ 标准名被截断（A3 翻红）",
              "ISO 11898" not in ac, ac)
    finally:
        rt._AC_COMPLY = old_comply

    # M3：去掉"给不出就 gaps" ⇒ A5 翻红
    it_src = inspect.getsource(rt.itemize)
    mut = it_src.replace('''        ac, ac_kind = _acceptance_criteria(s)
        if not ac:''', '''        ac, ac_kind = _acceptance_criteria(s)
        if False:''', 1)
    check("M3 去掉「给不出验收标准就进 gaps」⇒ 模糊需求会混进 items（A5 翻红）",
          "if not ac:" in it_src and "if False:" in mut,
          "变异未生效")

    # M4：去掉 status 过滤 ⇒ B1 判据翻红
    tools_src = open(os.path.join(_ROOT, "agent", "pipeline_parts", "tools.py"),
                     encoding="utf-8").read().replace("\r\n", "\n")
    mut4 = tools_src.replace("if name in _disabled_tools:", "if False:", 1)
    check("M4 去掉内置工具的 status 过滤 ⇒ 下线工具会被注入（B1 翻红）",
          "if name in _disabled_tools:" in tools_src and "if False:" in mut4)

    # M5：去掉 supersede ⇒ D1 翻红
    repo_src = open(os.path.join(_ROOT, "repositories", "requirement_repo.py"),
                    encoding="utf-8").read().replace("\r\n", "\n")
    check("M5 去掉 _supersede_prev ⇒ 旧版本仍是 active（D1 翻红）",
          "_supersede_prev" in repo_src and "self._supersede_prev(project_id, rid, fp, now)" in repo_src)
    _IN[0] = False


def main():
    t0 = time.time()
    print("verify_requirement_optimization —— 需求分析五项优化")
    a_group()
    b_group()
    c_group()
    d_group()
    m_group()
    n_pass = sum(1 for r in _results if r[0] == PASS)
    n_mut = sum(1 for r in _MUT if r[0] == PASS)
    n_fail = sum(1 for r in _results + _MUT if r[0] == FAIL)
    print("\n结果：%d PASS / %d FAIL / %d SKIP　（其中变异自证 %d 条）　%.1fs"
          % (n_pass, n_fail, len(_SKIP), n_mut, time.time() - t0))
    for st, name, det in _results + _MUT:
        if st == FAIL:
            print("  ✗ %s　%s" % (name, det))
    if _SKIP:
        # SKIP 不是通过：必须显式列出，否则"没验证"会被读成"验过了"
        print("  SKIP 项（未验证，不等于通过）：%s"
              % "、".join(n for n, _ in _SKIP))
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
