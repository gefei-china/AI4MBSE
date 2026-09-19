# -*- coding: utf-8 -*-
"""P2/P3 回归：生成端本地校验门禁（`sysml_v2_check` + cards.py 接入 + 版本留痕）是否真的成立。

被验对象（集成指南 §2.2 三个接入点里的 ②③，即 §3 的 P1/P2/P3）：
    · `sysml_v2_check.py`          —— P1 本地校验模块（项目级合并 + 语法/语义双路计数）
    · `cards.py::_gen_sysml_views` —— P2 生成后校验 → views["check"]（5 个调用点的唯一收敛处）
    · `agent/utils.py::_archive_sysml_version` —— P3 摘要写进 element_summary["check"]

7 段断言：
    [1] 基线复现：对 sysml_models/ev_thermal_mgmt/ 6 文件跑**项目级合并口径**，
        必须落在 rc=1 / ERROR 47（语法 33 / 语义 14）/ WARN 48
        —— 与 知识文档 §8.6、方案评估 §3.1 两套独立实现的实测**逐项一致**（互证脚本可用）。
    [2] 三档判据 + 双路分流失流（单产物口径 BCDEF 六个用例）：
        证明 `pass / report / block` 真的由**语法路**决定；纯语义错**不阻断**；
        跨文件引用伪错只落语义路 → 不影响门禁（「接入点②用单产物口径」能成立的原因）。
    [3] hash 短路：同内容第二次调用零成本且结论一致。
    [4] 降级路径：校验器缺失 / 超时 / 空内容 / 文件不存在 → `unavailable`，**一律不抛异常**
        （保证「校验失败不阻断建模主链路」这句承诺是真的）。
    [5] 生成端集成：真实 `CardMixin._gen_sysml_views` 会挂 `views["check"]`，键齐全；
        并且**无代码 / impact 意图仍返回 None**（回归，未被本轮改动破坏）。
    [6] 行为等价（只增不改）：关掉校验 or 令 `_check_generated_sysml` 返回 None →
        产物剔除 `check` 键后与开启时**逐字节一致**。
        可选 `--base <git-ref>`：拿该 ref 的 cards.py 做 **AST 级**比对，
        证明新函数 = 旧函数 + **2 条语句**（多写者环境下这是最强的「只增不改」证据）。
    [7] 入库留痕：`_archive_sysml_version` 把 check 写进 element_summary
        （全程未提交事务，验完 ROLLBACK，库里零残留）。

运行：<venv>/python.exe -X utf8 tools/verify/verify_sysml_check_gate.py
      <venv>/python.exe -X utf8 tools/verify/verify_sysml_check_gate.py --base 5466ea6

⚠️ 通过数口径（报数必须带参数）：
      · **裸跑 → 81/81**：第 [6b] 段的 3 项 AST 级对拍**需要基线 ref，缺 ref 自动跳过**；
      · **带 `--base <ref>` → 84/84**：3 项补上。
    两个数都不是回归，只是段数不同。脚本末尾会自己打印当前口径。
"""
import ast
import json
import os
import sqlite3
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

MODELS = os.path.join(ROOT, "sysml_models", "ev_thermal_mgmt")
CARDS_PY = os.path.join(ROOT, "agent", "pipeline_parts", "cards.py")

_oks, _fails = [], []


def check(name, cond, detail=""):
    (_oks if cond else _fails).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + ("  | " + str(detail) if detail else ""))


def hr(t):
    print("\n" + "─" * 82)
    print("■ " + t)


# ───────────────────────── 用例语料（单产物口径） ─────────────────────────
# 依据：tmp/v2check/ 的既有探测用例 + 知识文档 §2.5 的三层能力实证样例
CASE_OK = ("package DemoOk {\n"
           "    private import ScalarValues::*;\n"
           "    part def Engine;\n"
           "    part e1 : Engine;\n"
           "}\n")

CASE_SYNTAX_ONLY = ("package Broken2 {\n"
                    "    part deff B {\n"
                    "        attribut y : ;\n"
                    "    }\n"
                    "}\n")

CASE_SEMANTIC_ONLY = ("package Broken3 {\n"
                      "    part def C {\n"
                      "        attribute z : ThisTypeDoesNotExist_XYZ;\n"
                      "    }\n"
                      "}\n")

# 语法错 + 语义错并存：用于证明「门禁只看语法路，语义错条数不参与判定」
CASE_MIXED = ("package Broken {\n"
              "    part def A {\n"
              "        attribute x : Real;\n"
              "}\n")

# v1 风格 refines（知识文档点名的 AI 高频错法），也应命中语法路
CASE_V1_REFINES = ("package P {\n"
                   "    private import ScalarValues::*;\n"
                   "    requirement def R;\n"
                   "    requirement r1 : R;\n"
                   "    requirement r2 : R;\n"
                   "    r2 refines r1;\n"
                   "}\n")

# ★ 跨文件引用型伪错：语法全对，只是引用了「本段之外」的类型 ——
#   单产物口径下必然报语义错，但**绝不能**落到语法路（否则生成端会被伪错阻断）
CASE_CROSSFILE_FAKE = ("package P2 {\n"
                       "    private import ScalarValues::*;\n"
                       "    part def TMS;\n"
                       "    part t : TMS;\n"
                       "    requirement r : ReqCooling;\n"
                       "}\n")

GEN_CONTENT = ("已按需求生成冷却回路模型。\n\n```sysml\n" + CASE_OK + "```\n")

CHECK_KEYS = {"rc", "verdict", "blocked", "n_error", "n_syntax", "n_semantic",
              "n_warn", "scope", "top", "error", "at"}


def _is_injected_stmt(s):
    """P2 注入的两条语句之一？（`_chk = self._check_generated_sysml(code)` 或 `if _chk ...`）

    ⚠️ 必须**深度**匹配：注入的 `if` 落在 `_gen_sysml_views` 的**外层 `try:` 块内**，
    不是函数顶层语句 —— 只扫顶层会漏（本脚本第一版就栽在这，n_if=0）。
    """
    if isinstance(s, ast.Assign) and isinstance(s.value, ast.Call):
        return getattr(s.value.func, "attr", "") == "_check_generated_sysml"
    if isinstance(s, ast.If):
        return "_chk" in ast.dump(s.test)
    return False


def _func_norm(src, name, drop_injected=False):
    """取函数 AST（去 docstring）；`drop_injected=True` 时深度剔除 P2 注入的语句。

    `ast.dump` 默认不带行号，所以这是**结构级**等价判定（行号漂移不影响结论）。
    """
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            fn = ast.parse(ast.unparse(node)).body[0]        # 深拷贝一份，别动原树
            b = list(fn.body)
            if b and isinstance(b[0], ast.Expr) and isinstance(getattr(b[0], "value", None), ast.Constant) \
                    and isinstance(b[0].value.value, str):
                fn.body = b[1:]                              # 去 docstring（两版文案不同）
            if drop_injected:
                # 先快照父节点再做删改，避免边遍历边改列表导致漏项
                for parent in list(ast.walk(fn)):
                    for _f, val in list(ast.iter_fields(parent)):
                        if isinstance(val, list) and val and all(isinstance(x, ast.stmt) for x in val):
                            val[:] = [x for x in val if not _is_injected_stmt(x)]
            return fn
    return None


def _count_injected(src, name):
    fn = _func_norm(src, name)
    if fn is None:
        return -1
    n = 0
    for parent in ast.walk(fn):
        for _f, val in ast.iter_fields(parent):
            if isinstance(val, list):
                n += sum(1 for x in val if isinstance(x, ast.stmt) and _is_injected_stmt(x))
    return n


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="", help="P2 改动前的 git ref（做 AST 级「只增不改」比对）")
    args = ap.parse_args()

    import sysml_v2_check as svc
    from agent.pipeline_parts.cards import CardMixin

    svc.clear_cache()

    # ── [0] 环境 ──
    hr("环境")
    check("checker 三件套齐备（java / jar / sysml.library）", svc.available(),
          f"java={os.path.isfile(svc.JAVA)} jar={os.path.isfile(svc.JAR)} lib={os.path.isdir(svc.LIB)}")
    check("sysml_models/ev_thermal_mgmt 存在", os.path.isdir(MODELS), MODELS)

    # ── [1] 基线复现（项目级合并口径） ──
    hr("[1] 基线复现：sysml_models/ev_thermal_mgmt/『项目级合并口径』")
    files = [os.path.join(MODELS, n) for n in sorted(os.listdir(MODELS)) if n.endswith(".sysml")]
    base = svc.check_project(files)
    print("       " + svc.line_text(base))
    check("文件数 = 6", len(files) == 6, len(files))
    check("rc == 1（存在 ERROR）", base["rc"] == 1, base["rc"])
    check("ERROR == 47", base["n_error"] == 47, base["n_error"])
    check("n_syntax == 33", base["n_syntax"] == 33, base["n_syntax"])
    check("n_semantic == 14", base["n_semantic"] == 14, base["n_semantic"])
    check("WARN == 48", base["n_warn"] == 48, base["n_warn"])
    check("双路计数自洽：syntax + semantic == error", base["n_syntax"] + base["n_semantic"] == base["n_error"])
    check("判据 == block（n_syntax > 0）", base["verdict"] == "block", base["verdict"])
    check("诊断行号能反查回源文件（工具硬编码 stdin 的补丁生效）",
          all(d["file"].endswith(".sysml") for d in base["errors"]), base["errors"][0]["file"] if base["errors"] else "")
    check("v1 风格 refines 归类到语法路",
          any(d["is_syntax"] and "refines" in d["msg"] for d in base["errors"]))

    # ── [2] 三档判据 + 双路分流失流 ──
    hr("[2] 三档判据 / 双路分流失流（单产物口径，6 用例）")
    cases = [
        ("A_极简合法", CASE_OK, "pass", 0, 0, 0),
        ("B_纯语法错", CASE_SYNTAX_ONLY, "block", 3, 0, None),
        ("C_纯语义错", CASE_SEMANTIC_ONLY, "report", 0, 2, None),
        ("D_语法+语义并存", CASE_MIXED, "block", 1, 2, None),
        ("E_v1风格refines", CASE_V1_REFINES, "block", 2, 0, None),
        ("F_跨文件引用伪错", CASE_CROSSFILE_FAKE, "report", 0, 2, None),
    ]
    got = {}
    for name, code, exp_v, exp_syn, exp_sem, exp_rc in cases:
        r = svc.check_code(code)
        got[name] = r
        print(f"       {name:18s} rc={r['rc']} ERROR={r['n_error']:2d} 语法={r['n_syntax']:2d} "
              f"语义={r['n_semantic']:2d} → {r['verdict']}")
        check(f"{name} 判据 == {exp_v}", r["verdict"] == exp_v, r["verdict"])
        if exp_v != "unavailable":
            check(f"{name} n_syntax == {exp_syn}", r["n_syntax"] == exp_syn, r["n_syntax"])
            check(f"{name} n_semantic == {exp_sem}", r["n_semantic"] == exp_sem, r["n_semantic"])
            check(f"{name} 双路计数自洽", r["n_syntax"] + r["n_semantic"] == r["n_error"])
        if exp_rc is not None:
            check(f"{name} rc == {exp_rc}", r["rc"] == exp_rc, r["rc"])

    check("★ 纯 rc=0 → pass", got["A_极简合法"]["verdict"] == "pass")
    check("★ 纯语义错 → report（**不阻断**，属建模决策）", got["C_纯语义错"]["verdict"] == "report")
    check("★ 语法错在场时语义错条数不影响门禁（D: 语义 2 条仍 block）",
          got["D_语法+语义并存"]["verdict"] == "block" and got["D_语法+语义并存"]["n_semantic"] == 2)
    check("★ 跨文件引用伪错**只落语义路**、不阻断（单产物口径可用的关键前提）",
          got["F_跨文件引用伪错"]["n_syntax"] == 0 and got["F_跨文件引用伪错"]["verdict"] == "report")
    check("★ v1 风格 refines 被拦在语法路（L0 卡点名的错法，门禁能抓到）",
          got["E_v1风格refines"]["verdict"] == "block")

    # ── [3] hash 短路 ──
    hr("[3] hash 短路（防反复重跑：同内容第二次必须零成本）")
    svc.clear_cache()
    t0 = time.time()
    first = svc.check_code(CASE_SEMANTIC_ONLY, use_cache=True)
    dt1 = time.time() - t0
    t0 = time.time()
    second = svc.check_code(CASE_SEMANTIC_ONLY, use_cache=True)
    dt2 = time.time() - t0
    check("首次是真跑（>= 2 s，非缓存）", dt1 >= 2.0, f"{dt1:.2f}s")
    check("第二次命中缓存 cached=True", second["cached"] is True, second["cached"])
    check("缓存结论与首次一致",
          (second["rc"], second["n_error"], second["n_syntax"], second["n_semantic"]) ==
          (first["rc"], first["n_error"], first["n_syntax"], first["n_semantic"]))
    check("短路真的省时（第二次 < 0.05 s）", dt2 < 0.05, f"{dt2*1000:.1f}ms")
    other = svc.check_code(CASE_V1_REFINES, use_cache=True)
    check("内容不同不误命缓存（新内容 cached=False 且结论不同）",
          other["cached"] is False and other["n_syntax"] == 2 and first["n_syntax"] == 0,
          f"cached={other['cached']} syn={other['n_syntax']}")

    # ── [4] 降级路径（不阻断） ──
    hr("[4] 降级路径：环境/异常一律 unavailable，且**不抛异常**")
    _orig_avail = svc.available
    try:
        svc.available = lambda: False
        svc.clear_cache()
        r = svc.check_code(CASE_OK)
        check("校验器缺失 → verdict=unavailable", r["verdict"] == "unavailable", r["verdict"])
        check("校验器缺失 → ok=False 且带原因", r["ok"] is False and "不可用" in r["error"], r["error"])
        check("校验器缺失 → 不抛异常（主链路安全）", True)
    finally:
        svc.available = _orig_avail
        svc.clear_cache()

    r = svc.check_code(CASE_OK, timeout=0.05, use_cache=False)
    check("超时 → verdict=unavailable", r["verdict"] == "unavailable", r.get("error", "")[:40])
    r = svc.check_code("   \n  ")
    check("空内容 → ok=False（空文件不算有效产出）", r["ok"] is False, r["error"])
    r = svc.check_project([os.path.join(MODELS, "no_such_file.sysml")])
    check("文件不存在 → ok=False，不抛异常", r["ok"] is False, r["error"])
    r = svc.check_project([])
    check("空文件列表 → ok=False，不抛异常", r["ok"] is False, r["error"])
    check("verdict(None) 不抛异常", svc.verdict(None) == "unavailable")
    check("summarize(None) 不抛异常", svc.summarize(None) == {})
    svc.clear_cache()

    # ── [5] 生成端集成（P2） ──
    hr("[5] 生成端集成：CardMixin._gen_sysml_views → views[\"check\"]")
    m = CardMixin()
    views = m._gen_sysml_views(GEN_CONTENT, "design", "生成 BDD 视图")
    check("含 sysml 代码 → 产出 views（非 None）", isinstance(views, dict))
    check("views[\"views\"] 非空（投影未被破坏）",
          bool((views or {}).get("views")), list(((views or {}).get("views") or {}).keys()))
    chk = (views or {}).get("check")
    check("views[\"check\"] 已挂载", isinstance(chk, dict), type(chk).__name__)
    if isinstance(chk, dict):
        check("check 字段齐全", CHECK_KEYS.issubset(set(chk)), sorted(set(chk)))
        check("check.scope == 'code'（单产物口径）", chk.get("scope") == "code", chk.get("scope"))
        check("check.verdict == pass（合法模型）", chk.get("verdict") == "pass", chk.get("verdict"))
        check("check.blocked is False", chk.get("blocked") is False)
        check("check 与 views[\"quality_check\"] 同级并存（未互相覆盖）",
              "quality_check" in views, sorted(k for k in views if k not in ("views",)))
    check("无 sysml 代码 → 仍返回 None（回归）", m._gen_sysml_views("这是一段普通回答。", "design", "") is None)
    check("impact 意图 → 仍返回 None（回归）",
          m._gen_sysml_views(GEN_CONTENT, "impact", "影响分析") is None)
    check("空内容 → 仍返回 None（回归）", m._gen_sysml_views("", "design", "") is None)

    # ── [6] 行为等价（只增不改） ──
    hr("[6] 行为等价：只增不改（新键仅 'check'）")
    # ⚠️ 取 __dict__ 里的 staticmethod 描述符本身来还原——直接取 `CardMixin.f` 拿到的是裸函数，
    #    再赋回类上会变成普通方法（self 会被当第一个参数传入）→ 报「takes 1 positional argument but 2 were given」。
    _orig_check = CardMixin.__dict__["_check_generated_sysml"]
    try:
        CardMixin._check_generated_sysml = staticmethod(lambda code: None)   # 模拟改动前：不挂 check
        off = m._gen_sysml_views(GEN_CONTENT, "design", "生成 BDD 视图")
    finally:
        CardMixin._check_generated_sysml = _orig_check
    on = m._gen_sysml_views(GEN_CONTENT, "design", "生成 BDD 视图")
    if isinstance(on, dict) and isinstance(off, dict):
        check("键集合差 == {'check'}（只多了这一个键）",
              set(on) - set(off) == {"check"}, sorted(set(on) - set(off)))
        check("键集合未被删减", not (set(off) - set(on)), sorted(set(off) - set(on)))
        same = True
        for k in off:
            a = json.dumps(off.get(k), sort_keys=True, ensure_ascii=False)
            b = json.dumps(on.get(k), sort_keys=True, ensure_ascii=False)
            if a != b:
                same = False
                print(f"         ✗ 键 {k} 不一致")
        check("其余每个键逐字节一致（改动纯增量）", same)
    else:
        check("行为等价对拍可取到两组产物", False, f"off={type(off).__name__} on={type(on).__name__}")

    # 配置开关：关掉校验必须回到「改动前」的产物（这是故障回退开关，必须真的有效）
    from core import config as _cfg
    _orig_get = _cfg.get

    def _fake_get(sec, key, default=None):
        if (sec, key) == ("sysml", "check_enabled"):
            return False
        return _orig_get(sec, key, default)

    _cfg.get = _fake_get
    try:
        _off2 = m._gen_sysml_views(GEN_CONTENT, "design", "生成 BDD 视图")
    finally:
        _cfg.get = _orig_get
    check("配置 check_enabled=False → 与改动前产物逐字节一致（回退开关真的有效）",
          json.dumps(_off2, sort_keys=True, ensure_ascii=False) == json.dumps(off, sort_keys=True, ensure_ascii=False))

    # 源码级「注入面 = 2 条语句」：不依赖 git，随时可验
    cur_src = open(CARDS_PY, encoding="utf-8").read()
    n_inj = _count_injected(cur_src, "_gen_sysml_views")
    check("源码级注入面 = 正好 2 条语句（1 条赋值 + 1 条 if，深度匹配）", n_inj == 2, f"n_injected={n_inj}")

    if args.base:
        hr(f"[6b] AST 级比对：{args.base} 的 cards.py vs 当前（证明『新 = 旧 + 2 条语句』）")
        try:
            old_src = subprocess.run(["git", "-C", ROOT, "show", f"{args.base}:agent/pipeline_parts/cards.py"],
                                     capture_output=True, timeout=60).stdout.decode("utf-8", "replace")
            old_fn = _func_norm(old_src, "_gen_sysml_views")
            check("取到基线的 _gen_sysml_views", old_fn is not None, args.base)
            if old_fn is not None:
                check("基线版注入面 = 0（确认该 ref 是 P2 改动前）", _count_injected(old_src, "_gen_sysml_views") == 0)
                new_kept = _func_norm(cur_src, "_gen_sysml_views", drop_injected=True)
                check("★ 剔除这 2 条语句后，新函数 AST == 旧函数 AST（只增不改，硬证据）",
                      ast.dump(new_kept) == ast.dump(old_fn))
        except Exception as exc:                                  # noqa: BLE001
            check("AST 比对可执行", False, f"{type(exc).__name__}: {exc}")

    # ── [7] 入库留痕（P3） ──
    hr("[7] 入库留痕：_archive_sysml_version → element_summary[\"check\"]（事务内验完 ROLLBACK）")
    try:
        from core import config as _cfg2
        db = _cfg2.get("database", "path") or os.path.join(ROOT, "mbse.db")
    except Exception:
        db = os.path.join(ROOT, "mbse.db")
    from agent.utils import _archive_sysml_version
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None
    conn.execute("BEGIN")
    TMP_CONV, TMP_MSG = -990017, -990018
    try:
        before = conn.execute("SELECT COUNT(*) AS n FROM sysml_versions").fetchone()["n"]
        v1 = _archive_sysml_version(conn, TMP_CONV, TMP_MSG, on, "自检", "package X;")
        row = conn.execute("SELECT element_summary, version_label FROM sysml_versions WHERE id=?",
                           (v1,)).fetchone() if v1 else None
        check("建档成功（返回版本 id）", bool(v1), v1)
        es = json.loads(row["element_summary"]) if row else {}
        check("element_summary 含 check 字段", isinstance(es.get("check"), dict), sorted(es.keys()))
        if isinstance(es.get("check"), dict):
            c = es["check"]
            check("check 字段齐全（前端零改动可读）", CHECK_KEYS.issubset(set(c)), sorted(set(c)))
            check("check.verdict 与生成端一致（pass）", c.get("verdict") == "pass", c.get("verdict"))
            check("check.at 有时间戳（可追溯『当时合不合法』）", bool(c.get("at")), c.get("at"))
            check("原始字段（entities/relations/views）未被挤掉",
                  all(k in es for k in ("entities", "relations", "views")), sorted(es.keys()))
        # 第二版：挂一个必然 block 的 check，验证 blocked 语义能落到库里
        blk = svc.summarize(svc.check_code(CASE_SYNTAX_ONLY))
        sv2 = dict(on)
        sv2["check"] = blk
        v2 = _archive_sysml_version(conn, TMP_CONV, TMP_MSG + 1, sv2, "自检", "")
        row2 = conn.execute("SELECT element_summary FROM sysml_versions WHERE id=?", (v2,)).fetchone()
        c2 = (json.loads(row2["element_summary"]) or {}).get("check") or {}
        check("语法错版本 → element_summary.check.blocked is True",
              c2.get("blocked") is True and c2.get("n_syntax", 0) > 0, c2.get("n_syntax"))
        check("版本链 parent_id 串起来（v2.parent = v1）",
              conn.execute("SELECT parent_id FROM sysml_versions WHERE id=?", (v2,)).fetchone()["parent_id"] == v1)
    finally:
        conn.execute("ROLLBACK")
        after = conn.execute("SELECT COUNT(*) AS n FROM sysml_versions").fetchone()["n"]
        check("ROLLBACK 后零残留（测试未污染版本链）", after == before, f"{before} → {after}")
        conn.close()

    # ── 汇总 ──
    total = len(_oks) + len(_fails)
    print("\n" + "=" * 90)
    print(f"断言汇总：{len(_oks)}/{total} 通过")
    if not args.base:
        print(f"口径提示：当前**未带 --base**，[6b] 的 3 项 AST 级「只增不改」对拍已跳过"
              f"（总数 {total}，全绿时报 81/81 属**正常**，不是回归）。")
        print("          要拿满 84 项，请：--base <P2 改动前的 git ref>（例如 5466ea6）。")
    else:
        print(f"口径提示：带 --base {args.base}，[6b] 3 项已执行（总数 {total}）。")
    if _fails:
        print("失败项：")
        for f in _fails:
            print("  ✗ " + f)
    print("=" * 90)
    return 1 if _fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
