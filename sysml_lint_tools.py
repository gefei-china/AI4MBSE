"""sysml_v2_lint — 命名规范与规约确定性检查工具。

与 autofix 的分工：
  autofix  改写**语法**（改完能过校验器）
  lint     检查**规约**（命名/必填属性/forbidden 规则是否被遵守）

两者都不做语义级改写 —— 涉及建模意图的一律只报告。

关键设计：**每条规则自带 enforce 分级**
  lint   提示（不阻断，人工判断）
  check  阻断（must-fix，违反即不合格）
  forbid 禁止（出现即错误）
不分级则无法自动执行 —— 这是本工具存在的理由。
"""
from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
MODELS_DIR = os.path.join(ROOT, "sysml_models")

ENFORCE_ORDER = {"lint": 0, "check": 1, "forbid": 2}
ENFORCE_ZH = {"lint": "提示", "check": "阻断", "forbid": "禁止"}

# ── 规约卡命名正则（与 methodology_profile_tools.DEFAULT_PROFILE.naming_conventions 同源）──
# ⚠️ 两处若各自维护必然漂移 —— 门禁 verify_sysml_lint 会断言两者一致。
NAMING_RULES = {
    "part_def": (r"^[A-Z][A-Za-z0-9_]*$", "类型定义须大驼峰（PascalCase）"),
    "requirement": (r"^[A-Z][A-Za-z0-9_]*$", "需求须大驼峰"),
    "port": (r"^[a-z][A-Za-z0-9_]*$", "端口/属性须小驼峰（camelCase）"),
    "item_def": (r"^[A-Z][A-Za-z0-9_]*$", "项定义须大驼峰"),
    "use_case": (r"^[A-Z][A-Za-z0-9_]*$", "用例须大驼峰"),
}

# ── 抽取各元类的定义名 ──
# ⚠️ 三处实测踩坑，这里是它们的合并解法：
#  ① 必须**区分 def（类型定义）与 usage（实例）**，二者命名规范相反：
#     part def Vehicle;（大驼峰）vs part batteryThermal;（小驼峰）
#     用 `part (?:def\s+)?` 统一匹配 ⇒ 44 个 part usage 被大驼峰规则判违规（全假阳性）。
#  ② 必须覆盖「无 def 形态」：真实样例写 `requirement ReqX {`（无 def），
#     只匹配 `xxx def` ⇒ 需求类命名检查静默失效。
#  ③ 不能用 `^\s*` 行首锚定：LLM 常输出单行紧凑写法 `package P { part def vehicle; }`
#     ⇒ 行锚定匹配不到。但去掉锚定又会命中注释（`// part def FakeThing`）。
#     ⇒ **正解：扫描前剥离注释**（见_strip_comments），正则不加行锚定。
_DEF_PATTERNS = [
    # (rule_key, 抽取正则, 中文说明, 是否为 def)
    ("part_def",      r"(?<![\w.])part\s+def\s+([^\s:;{]+)",         "part def（类型）", True),
    ("part_usage",    r"(?<![\w.])part\s+(?!def\b)([^\s:;{]+)",       "part usage（实例）", False),
    ("item_def",      r"(?<![\w.])item\s+def\s+([^\s:;{]+)",         "item def（类型）", True),
    ("requirement_def",   r"(?<![\w.])requirement\s+def\s+([^\s:;{]+)", "requirement def", True),
    ("requirement_usage", r"(?<![\w.])requirement\s+(?!def\b)([^\s:;{]+)", "requirement usage", False),
    ("use_case_def",  r"(?<![\w.])use\s+case\s+def\s+([^\s:;{]+)",   "use case def（类型）", True),
    ("port_def",      r"(?<![\w.])port\s+def\s+([^\s:;{]+)",         "port def（类型）", True),
    ("port_usage",    r"(?<![\w.])port\s+(?!def\b)([^\s:;{]+)",       "port usage（实例）", False),
    ("attribute_def",   r"(?<![\w.])attribute\s+def\s+([^\s:;{]+)",  "attribute def（类型）", True),
    ("attribute_usage", r"(?<![\w.])attribute\s+(?!def\b)([^\s:;{]+)", "attribute usage", False),
]

_LINE_COMMENT = re.compile(r"//[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)


def _strip_comments(code: str) -> str:
    """剥离注释 —— 避免命名检查命中注释里的示例代码。

    用空白替换而非删除，**保持行号不变**，这样诊断里的行号仍指向原始文件。
    """
    def blank(m):
        return re.sub(r"[^\n]", " ", m.group(0))
    code = _BLOCK_COMMENT.sub(blank, code)
    code = _LINE_COMMENT.sub(blank, code)
    # 字符串字面量（SysML 文档注释 /* */ 与 " " 两种）
    code = re.sub(r'"[^"\n]*"', lambda m: " " * len(m.group(0)), code)
    return code

# 命名规范：类型=大驼峰，实例=小驼峰（与 SysML v2 社区惯例一致）
NAMING_RULES = {
    "part_def": (r"^[A-Z][A-Za-z0-9_]*$", "类型定义须大驼峰（PascalCase）"),
    "part_usage": (r"^[a-z][A-Za-z0-9_]*$", "实例须小驼峰（camelCase）"),
    "item_def": (r"^[A-Z][A-Za-z0-9_]*$", "项定义须大驼峰"),
    "requirement_def": (r"^[A-Z][A-Za-z0-9_]*$", "需求定义须大驼峰"),
    "requirement_usage": (r"^[A-Z][A-Za-z0-9_]*$", "需求实例沿用大驼峰（Req 前缀惯例）"),
    "use_case_def": (r"^[A-Z][A-Za-z0-9_]*$", "用例须大驼峰"),
    "port_def": (r"^[A-Z][A-Za-z0-9_]*$", "端口定义须大驼峰"),
    "port_usage": (r"^[a-z][A-Za-z0-9_]*$", "端口实例须小驼峰"),
    "attribute_def": (r"^[A-Z][A-Za-z0-9_]*$", "属性定义须大驼峰"),
    "attribute_usage": (r"^[a-z][A-Za-z0-9_]*$", "属性实例须小驼峰"),
}

# forbidden 规则（出现即 forbid）
FORBIDDEN = [
    ("extend_keyword", r"^\s*extend\s+\S+", "extend",
     "SysML v2 无 extend 关键字（用 include use case 或 :> ）"),
    ("first_keyword", r"^\s*first\b", "first",
     "SysML v2 无 first 关键字（用 if 条件）"),
    ("connector_keyword", r"\bconnector\s+\w+\s+from\b", "connector",
     "v2 用 connect A to B，不用 connector...from...to"),
    ("traces_keyword", r"\brequirement\b.*\btraces\b", "traces",
     "v2 无 traces 关键字（用 dependency from..to）"),
    ("double_and", r"&&", "&&", "逻辑与用单个 &（双 && 是 C 习惯）"),
    ("non_private_import", r"^\s*import\s+(?!private|public)", "import 无前缀",
     "顶层 import 须带可见性前缀"),
]


def _strip_quoted(line: str) -> str:
    """去掉单引号包裹（中文标识符必须加引号，命名检查只看真实名字）。"""
    m = re.match(r"^\s*(?:part|item|requirement|use\s+case|port)\s+def\s+"
                 r"'([^']+)'", line)
    return m.group(1) if m else line


def _lint_code(code: str, enforce_filter=None) -> dict:
    violations, checked = [], 0
    filt = set(enforce_filter) if enforce_filter else None

    # ★ 先剥离注释（保持行号不变），避免命名检查命中注释里的示例代码
    clean = _strip_comments(code)
    lines = clean.splitlines()

    # ① 命名规范（逐行finditer：同一行可能有多个定义）
    for key, pat, _zh, _is_def in _DEF_PATTERNS:
        spec = NAMING_RULES.get(key)
        if not spec:
            continue
        regex, _desc = spec
        for i, line in enumerate(lines, 1):
            for m in re.finditer(pat, line):
                checked += 1
                raw = m.group(1).strip().rstrip(";")
                if len(raw) >= 2 and raw[0] == raw[-1] == "'":
                    # 中文标识符带单引号是合法写法，不判命名风格
                    continue
                if not re.match(regex, raw):
                    violations.append({
                        "id": f"NAME-{key}", "enforce": "check", "line": i,
                        "kind": key, "found": raw,
                        "message": f"{key} 命名 {raw!r} 不符规范：需匹配 {regex}",
                        "rule": pat,
                    })

    # ② forbidden 规则（同样用剥离注释后的文本）
    for rid, pat, token, why in FORBIDDEN:
        for i, line in enumerate(lines, 1):
            if re.search(pat, line):
                violations.append({
                    "id": rid, "enforce": "forbid", "line": i,
                    "kind": "forbidden", "found": token,
                    "message": f"出现禁用构造 {token!r}：{why}",
                    "rule": pat,
                })

    if filt:
        violations = [v for v in violations if v["enforce"] in filt]
    violations.sort(key=lambda v: (ENFORCE_ORDER.get(v["enforce"], 0), v["line"]))
    return {"violations": violations, "checked": checked}


def _render(code, res, profile_id, n_hard):
    L = ["【SysML 规约检查（lint）】"]
    L.append(f"规约：{profile_id}")
    L.append(f"被检查的命名定义：{res['checked']} 处｜违反项：{len(res['violations'])}")
    L.append("")
    if not res["violations"]:
        L.append("✅ 未发现规约违反。（注：这只覆盖命名与禁用构造，"
                 "不覆盖类型族/语义正确性 —— 那些由 sysml_v2_validate 判定）")
        return "\n".join(L)
    cur = None
    for v in res["violations"]:
        if v["enforce"] != cur:
            cur = v["enforce"]
            L.append(f"── {ENFORCE_ZH.get(cur, cur)}级（{cur}）──")
        L.append(f"  L{v['line']:<4} [{v['id']}] {v['message']}")
    blocking = [v for v in res["violations"] if v["enforce"] in ("check", "forbid")]
    L.append("")
    if blocking:
        L.append(f"⛔ 有 {len(blocking)} 项阻断级/禁止级违反 —— **模型不合格，需先修复**。")
        L.append("   提示：本工具只报告不修复。语法类可先调 sysml_v2_autofix；"
                 "命名类需人工改（自动改名会破坏跨文件引用）。")
    else:
        L.append(f"ℹ️ 全部为提示级（lint），不阻断；可自行决定是否调整。")
    if n_hard:
        L.append(f"⚠️ 代码本身另有 {n_hard} 个硬错（词法/语法），建议先跑 sysml_v2_validate。")
    return "\n".join(L)


def _lint(args: dict) -> dict:
    code = (args.get("code") or "").strip()
    files = args.get("files") or []
    profile_id = (args.get("profile_id") or "omg_sysml_v2_default")
    enforce_filter = args.get("enforce_filter") or None

    if not code and not files:
        return {"ok": False, "result": "需提供 code 或 files。二者都没给，无法检查。"}

    if files:
        paths = []
        for f in files:
            p = os.path.normpath(os.path.join(MODELS_DIR, f))
            if not os.path.abspath(p).startswith(os.path.abspath(MODELS_DIR)):
                return {"ok": False, "result": f"路径越界：{f}"}
            if not os.path.isfile(p):
                return {"ok": False, "result": f"文件不存在：{f}"}
            paths.append(p)
        merged, names = [], []
        for p in paths:
            with open(p, encoding="utf-8") as fh:
                merged.append(fh.read())
            names.append(os.path.relpath(p, MODELS_DIR).replace("\\", "/"))
        code = "\n".join(merged)
    else:
        names = ["<inline>"]

    res = _lint_code(code, enforce_filter)

    # 顺手报硬错（若有），但不重复判定
    n_hard = 0
    try:
        import sysml_v2_check as svc
        chk = svc.check_code(code)
        n_hard = chk.get("n_hard", 0) or 0
    except Exception as exc:                # noqa: BLE001
        n_hard = None
        chk_warn = f"{type(exc).__name__}: {exc}"

    blocking = [v for v in res["violations"] if v["enforce"] in ("check", "forbid")]
    return {
        "ok": True,
        "result": _render(code, res, profile_id, n_hard),
        "checked": res["checked"],
        "violation_count": len(res["violations"]),
        "blocking_count": len(blocking),
        "violations": res["violations"],
        "verdict": "fail" if blocking else ("warn" if res["violations"] else "pass"),
        "profile_id": profile_id,
        "files": names,
    }


def exec_tool(name: str, arguments: dict | None = None) -> dict:
    args = arguments or {}
    if name == "sysml_v2_lint":
        return _lint(args)
    return {"ok": False, "result": f"未知工具: {name}"}


def _selftest():
    print("=" * 72)
    print("sysml_v2_lint 自测")
    print("=" * 72)

    # 前置结构断言：_DEF_PATTERNS 与 NAMING_RULES 的 key 必须一一对应且均为 4 元组
    assert all(len(x) == 4 for x in _DEF_PATTERNS), "_DEF_PATTERNS 必须全是 4 元组"
    orphan = [k for k, *_ in _DEF_PATTERNS if k not in NAMING_RULES]
    assert not orphan, f"这些规则没有对应命名规范（会静默跳过）：{orphan}"
    dead = [k for k in NAMING_RULES if k not in {x[0] for x in _DEF_PATTERNS}]
    assert not dead, f"这些命名规范没有抽取规则（死规则）：{dead}"
    print(f"[前置] _DEF_PATTERNS {len(_DEF_PATTERNS)} 条，与命名规范完全对齐")

    print("\n① 干净代码应 pass（注意 def=大驼峰 / usage=小驼峰，与真实样例一致）：")
    good = ("package P {\n"
            "  part def Vehicle;\n"
            "  part vehicle1 {\n"
            "    port def CoolantPort;\n"
            "    port coolantIn;\n"
            "  }\n"
            "}")
    r = _lint({"code": good})
    print(f"  verdict={r.get('verdict')} checked={r.get('checked')} "
          f"violations={r.get('violation_count')}")
    for v in r.get("violations", []):
        print(f"    L{v['line']} [{v['id']}] {v['message'][:60]}")
    assert r.get("violation_count") == 0, "干净代码不该有违反"

    print("\n①b 单行紧凑写法也必须被检查（行锚定盲区回归）：")
    r1b = _lint({"code": "package P { part def vehicle; }"})
    ids1b = [v["id"] for v in r1b.get("violations", [])]
    print(f"  verdict={r1b.get('verdict')} ids={ids1b} checked={r1b.get('checked')}")
    assert "NAME-part_def" in ids1b, "单行紧凑写法里的命名违规必须被抓到"

    print("\n①c 注释里的示例代码不得被检查（剥离注释回归）：")
    with_cmt = ("package P {\n"
                "  // 示例: part def fakeName;\n"
                "  /* part def alsoFake; */\n"
                "  part def Vehicle;\n"
                "}")
    r1c = _lint({"code": with_cmt})
    print(f"  checked={r1c.get('checked')} violations={r1c.get('violation_count')}"
          f"（应 1 / 0）")
    assert r1c.get("checked") == 1 and r1c.get("violation_count") == 0, \
        "注释里的定义名被误检了"

    print("\n② 小写 part def 应被拦（check 级）：")
    bad = "package P {\n  part def vehicle;\n}"
    r2 = _lint({"code": bad})
    print(f"  verdict={r2.get('verdict')} blocking={r2.get('blocking_count')}")
    for v in r2.get("violations", []):
        print(f"    L{v['line']} [{v['enforce']}] {v['message'][:62]}")
    assert r2.get("blocking_count") >= 1, "小写类型名必须阻断"

    print("\n③ 禁用构造应被拦（forbid 级）：")
    for code, want, why in [
        ("package P {\n  part def V;\n  extend V;\n}", "extend_keyword", "extend 关键字"),
        ("package P {\n  import ScalarValues::*;\n  part def V;\n}",
         "non_private_import", "import 缺 private 前缀"),
        ("package P {\n  part def V;\n  requirement def A traces B;\n}",
         "traces_keyword", "traces 关键字"),
        ("package P {\n  part def V { attribute a : Real; }\n}",
         None, "对照：合法代码不应误报"),
    ]:
        rr = _lint({"code": code})
        ids = [v["id"] for v in rr.get("violations", [])]
        if want is None:
            print(f"  {why:26} verdict={rr.get('verdict')} ids={ids}（应为空）")
            assert not ids, "合法代码被误报"
        else:
            print(f"  {why:26} verdict={rr.get('verdict')} ids={ids}")
            assert want in ids, f"{why} 应被识别为 forbid，实际 {ids}"

    print("\n④ 中文标识符不该误报（合法但要引号）：")
    cn = "package P {\n  part def '电池包';\n}"
    r4 = _lint({"code": cn})
    print(f"  verdict={r4.get('verdict')} violations={r4.get('violation_count')}")
    assert r4.get("violation_count") == 0, "中文名带引号是合法的，不该报命名违规"

    print("\n⑤ 真实样例（合并口径）：")
    r5 = _lint({"files": [f"ev_thermal_mgmt/{n}" for n in
                ("00_master.sysml", "01_requirements.sysml", "02_architecture.sysml")]})
    print(f"  verdict={r5.get('verdict')} checked={r5.get('checked')} "
          f"violations={r5.get('violation_count')} blocking={r5.get('blocking_count')}")
    from collections import Counter
    print("  违反类型分布:", dict(Counter(v["id"] for v in r5.get("violations", []))))

    print("\n⑥ enforce_filter 生效（只看 forbid）：")
    r6 = _lint({"code": bad, "enforce_filter": ["forbid"]})
    print(f"  仅 forbid 过滤后 violations={r6.get('violation_count')}（应为 0）")

    print("\n" + "=" * 72)
    print("  自测通过：干净→pass / 命名违规→阻断 / 禁用构造→forbid / 中文不误报 /过滤生效")
    return 0


if __name__ == "__main__":
    sys.exit(_selftest())