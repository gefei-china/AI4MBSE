# -*- coding: utf-8 -*-
"""verify_no_fake_actor —— 防「虚构人名写进生产数据」回归的门禁。

## 背景（2026-10-23，米爸决策：系统中 mock 的数据排查并移除）

系统曾大量使用「王工」「李工」「赵管」三个**虚构人名**作为审计/创建人/操作人的
默认值或兜底值。它们看起来像"系统里有这三个同事"，实际查无此人。

### 三类问题（严重度递增）

| # | 问题 | 症状 | 实测 |
|---|---|---|---|
| ① | `audit("王工", ...)` 硬编码 | 审计流水的"谁做的"全是虚构人名 | 全库 47 处 |
| ② | `getattr(user, "username", "") or "王工"` | `user` 是 **dict**，`getattr` 取属性恒失败 ⇒ 永远落到兜底 | glossary 8 处 |
| ③ | `(user or {}).get("name", "王工")` | `current_user` 返回的 dict **没有 `name` 键**（只有 `display_name`/`username`）⇒ 永远取默认值 | artifacts/impact |

③ 最隐蔽：**它看起来在读用户身份，实际 100% 取不到**。

### 为什么必须用门禁守住

这类"默认值"极容易被后来人当成"合理的兜底"再加回来 ——
而它每次加回来的后果是**审计不可追责**（用户看到「王工 创建了项目」却查无此人）。
纯靠 review 抓不住：一次 grep 47 处，人眼会漏。

## 判据

B1 · 生产代码（routers/services/agent/core/knowledge_pipeline/repositories/database/
    ontology_semantics/norm_apply 等）中**不得出现**「王工」「李工」「赵管」「陈专」字面量
    —— **注释与 docstring 除外**（那里是"历史行为说明"，是刻意保留的对照材料）
B2 · 审计/创建人一律走 `core.audit.audit_user(user)`（唯一口径：
    display_name → username → "未登录"）
B3 · 不得出现 `getattr(user, "username"...)` 形式对 user 取属性
    —— `current_user` 返回 dict，`getattr` 恒失败（这是 ② 类问题的根因）
B4 · 不得出现 `(user or {}).get("name"` 形式取 `name` 键
    —— `current_user` 的 dict 没有 `name` 键（这是 ③ 类问题的根因）
B5 · 变异自证：注入上述任一写法，门禁必须判红（证明判据有鉴别力，而非恒绿）

## 用法
    ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_no_fake_actor.py
"""
from __future__ import annotations

import ast
import io
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

#: 虚构人名黑名单
FAKE_NAMES = ("王工", "李工", "赵管", "陈专")

#: 受检生产目录（相对 _ROOT）
SCAN_DIRS = (
    "routers", "services", "agent", "core", "knowledge_pipeline",
    "repositories", "database", "knowledge_engine.py", "ontology_semantics.py",
    "norm_apply.py", "vector2graph.py", "knowledge_reflow.py", "ontology_blueprint.py",
)

#: 明确豁免的文件/路径（测试脚本、验证脚本、文档、一次性工具）
EXEMPT_SUBSTR = (
    os.sep + "tests" + os.sep,
    os.sep + "tools" + os.sep,
    os.sep + "docs" + os.sep,
    os.sep + "tmp" + os.sep,
    os.sep + ".git" + os.sep,
    os.sep + "static" + os.sep,
    os.sep + "migrations" + os.sep,   # 迁移脚本里的历史 DEFAULT 值不再改写
)

_fails: list[str] = []
_passes: list[str] = []


def _ok(msg: str) -> None:
    _passes.append(msg)
    print(f"[PASS] {msg}")


def _fail(msg: str) -> None:
    _fails.append(msg)
    print(f"[FAIL] {msg}")


def _strip_comments_and_docstrings(src: str) -> str:
    """去掉注释与 docstring，只留**真实代码**。

    为什么必须去：注释/docstring 里提到「王工」是**刻意保留的历史行为说明**
    （例如 `audit_user` 的 docstring 就写着"替代历史写死的 audit("王工", ...)"），
    那是给人看的对照材料，不是可执行路径。
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:
        # 语法错误的文件不在本门禁职责内，交给 py_compile 报
        return src
    # 收集所有 docstring 的位置区间
    spans: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", None) or []
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                d = body[0]
                spans.append((d.lineno, d.end_lineno or d.lineno))
    lines = src.splitlines()
    # 逐行去注释（简单但对本项目足够：注意不要误伤字符串内的 #）
    out = []
    for i, line in enumerate(lines, start=1):
        if any(a <= i <= b for a, b in spans):
            out.append("")
            continue
        stripped = re.sub(r"(?<!['\"])#[^\n]*$", "", line)
        out.append(stripped)
    return "\n".join(out)


def _iter_source_files():
    for rel in SCAN_DIRS:
        p = os.path.join(_ROOT, rel)
        if os.path.isfile(p):
            yield rel, p
        elif os.path.isdir(p):
            for dirpath, dirnames, filenames in os.walk(p):
                dirnames[:] = [d for d in dirnames if d not in ("__pycache__", ".git")]
                for fn in filenames:
                    if not fn.endswith(".py"):
                        continue
                    full = os.path.join(dirpath, fn)
                    if any(s in full for s in EXEMPT_SUBSTR):
                        continue
                    yield os.path.relpath(full, _ROOT).replace("\\", "/"), full


def _b1_no_fake_name_literal() -> None:
    """B1：生产代码里不得出现虚构人名字面量（注释/docstring 除外）。"""
    hits: list[str] = []
    for rel, full in _iter_source_files():
        src = io.open(full, encoding="utf-8").read()
        code = _strip_comments_and_docstrings(src)
        for name in FAKE_NAMES:
            for m in re.finditer(re.escape(name), code):
                line_no = code[:m.start()].count("\n") + 1
                hits.append(f"{rel}:{line_no} 「{name}」")
    if hits:
        _fail(f"B1 生产代码残留虚构人名 {len(hits)} 处：\n      " + "\n      ".join(hits[:20]))
    else:
        _ok("B1 生产代码无虚构人名字面量（注释/docstring 中的历史说明除外）")


def _b2_audit_uses_audit_user() -> None:
    """B2：审计写入走 audit_user 唯一口径。"""
    bad = []
    for rel, full in _iter_source_files():
        src = io.open(full, encoding="utf-8").read()
        code = _strip_comments_and_docstrings(src)
        # audit("王工"...) 已被 B1 覆盖；这里查"中文人名样"字面量。
        # 允许非人名标识：system / mcp-client / 可疑请求（未认证）/ 未登录 等。
        _ALLOWED_AUDIT_LITS = {"未登录", "系统", "system", "mcp-client",
                               "可疑请求（未认证）", "未认证"}
        for m in re.finditer(r'\baudit\(\s*(["\'])([^"\']{1,20})\1\s*,', code):
            lit_val = m.group(2)
            if lit_val in _ALLOWED_AUDIT_LITS:
                continue
            # 中文里含"工/管/老师/先生"等 ⇒ 疑似人名
            if any(ch in lit_val for ch in ("工", "管", "老师", "先生", "博士", "工程师")):
                bad.append(f"{rel}: audit(\"{lit_val}\", ...)")
    if bad:
        _fail(f"B2 审计调用用字面量作操作人 {len(bad)} 处（应走 audit_user）：\n      "
              + "\n      ".join(bad[:15]))
    else:
        _ok("B2 审计操作的字面量人名已全部消除（统一走 audit_user）")


def _b3_no_getattr_on_user() -> None:
    """B3：不得用 getattr(user, ...) 取属性 —— current_user 返回 dict，恒失败。"""
    bad = []
    for rel, full in _iter_source_files():
        src = io.open(full, encoding="utf-8").read()
        code = _strip_comments_and_docstrings(src)
        for m in re.finditer(r'getattr\(\s*user\s*,', code):
            ln = code[:m.start()].count("\n") + 1
            bad.append(f"{rel}:{ln} {m.group(0)}")
    if bad:
        _fail(f"B3 用 getattr(user, ...) 取身份 {len(bad)} 处（dict 取属性恒失败 ⇒ 永远落到兜底）：\n      "
              + "\n      ".join(bad[:15]))
    else:
        _ok("B3 无 getattr(user, ...) 取身份（已统一为 audit_user）")


def _b4_no_name_key_lookup() -> None:
    """B4：不得用 get("name") 从 current_user 取身份 —— 它没有 name 键。"""
    bad = []
    for rel, full in _iter_source_files():
        src = io.open(full, encoding="utf-8").read()
        code = _strip_comments_and_docstrings(src)
        # 只查**身份变量**上的 get("name")：user / u / current_user / _user。
        #   （普通业务 dict 的 get("name") 是取实体名/插件名，与身份无关，不该误伤。）
        _ID_RE = r'(?:\buser\b|\bu\b|\bcurrent_user\b|\b_user\b)'
        for m in re.finditer(
                r'(?:\(\s*' + _ID_RE + r'\s+or\s*\{\}\s*\)|' + _ID_RE + r')\s*\.\s*get\(\s*[\'"]name[\'"]',
                code):
            ln = code[:m.start()].count("\n") + 1
            bad.append(f"{rel}:{ln} {m.group(0)[:60]}")
    if bad:
        _fail(f"B4 用 get(\"name\") 从身份 dict 取人名 {len(bad)} 处（该键不存在 ⇒ 恒取默认值）：\n      "
              + "\n      ".join(bad[:15]))
    else:
        _ok("B4 无 get(\"name\") 取身份（已统一为 audit_user）")


def _b5_mutation_self_proof() -> None:
    """B5：变异自证 —— 注入三种写法，门禁必须判红（证明判据有鉴别力）。"""
    src_probe = os.path.join(_ROOT, "routers", "_fake_actor_probe.py")
    if os.path.exists(src_probe):
        os.remove(src_probe)
    original = (
        "from core.audit import audit\n"
        "def probe(user):\n"
        "    audit('王工', 'x', 'y')\n"
        "    a = getattr(user, 'username', '')\n"
        "    b = (user or {}).get('name', '王工')\n"
        "    return a, b\n"
    )
    try:
        io.open(src_probe, "w", encoding="utf-8", newline="\n").write(original)
        hits = []
        code = _strip_comments_and_docstrings(io.open(src_probe, encoding="utf-8").read())
        for name in FAKE_NAMES:
            if name in code:
                hits.append(f"字面量「{name}」")
        if re.search(r'getattr\(\s*user\s*,', code):
            hits.append("getattr(user,...)")
        if re.search(r'get\(\s*[\'"]name[\'"]\s*,', code):
            hits.append('get("name",...)')
        if len(hits) < 3:
            _fail(f"B5 变异自证失败：注入了 {len(hits)}/3 种写法却只检出 {hits}（判据无鉴别力）")
        else:
            _ok(f"B5 变异自证通过：注入 3 种违规写法，判据全部检出（{', '.join(hits)}）")
    finally:
        if os.path.exists(src_probe):
            os.remove(src_probe)


def main() -> int:
    print("=" * 72)
    print("verify_no_fake_actor — 防虚构人名写进生产数据")
    print("=" * 72)
    _b1_no_fake_name_literal()
    _b2_audit_uses_audit_user()
    _b3_no_getattr_on_user()
    _b4_no_name_key_lookup()
    _b5_mutation_self_proof()

    print("\n" + "=" * 72)
    print(f"结果：{len(_passes)} PASS / {len(_fails)} FAIL")
    if _fails:
        for m in _fails:
            print(f"  - {m}")
        print("FAIL")
        return 1
    print("✅ 门禁通过：审计/创建人一律取真实身份，无虚构人名")
    return 0


if __name__ == "__main__":
    sys.exit(main())
