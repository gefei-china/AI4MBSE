"""verify_chat_task_intent — 判据：主对话调用点**必须**带任务类型标识（P0，米爸"实施"）。

## 为什么单独一个脚本（这是本轮最重要的一条）

`verify_thinking_policy.py` 判定全绿，但它验的是
**「命中分流表之后，三态下发是否正确」**——
⇒ 它**测不到"主对话有没有进分流表"**。

实测证据（2026-10-10 实施前）：主对话 10 个 `llm_client.chat()` 调用点
（`stream.py` 4 / `execute.py` 6）**全部未传 `_intent`**
⇒ `openai_compat` 里 `_who` 为空 ⇒ 分流表**管不到它**。
而真库（`mbse.db` 只读）显示这条路径恰恰是耗时第一：

| 指标 | 实测值 |
|---|---|
| 调用次数 | 1856 |
| 思考占 completion | **49.3%** |
| 累计耗时 | **495 分钟** |
| 最长单次 | **4017 秒**（id=8188，reasoning/completion=**99.2%**） |

⇒ **最该管的那条路，恰好是唯一没被接管的。**

## 本脚本验什么

① **覆盖面对不对**（★ 本脚本的核心，也是旧脚本缺的那一维）
   —— 用 **AST**（不是字符串匹配）扫 `stream.py` / `execute.py`：
   每一个 `llm_client.chat(...)` 调用都必须带 `_intent=` 关键字。
   ★ 用 AST 是因为字符串匹配会被注释里的 `# llm_client.chat(` 骗过。

② 任务类型常量已定义且被真正引用（常量定义错了也判红）

③ 三个轮次角色在两条路径上都有覆盖
   （PROBE=带工具探针 / REPAIR=收尾修复 / ANSWER=最终生成）

④ ★ **语义断言**：带修复工具的调用必须标 REPAIR，带 `tools=None` 直出必须标 ANSWER
   —— 防止"标了 intent 但标错了角色"这种形式正确、实质错误的假绿。

⑤ **真库口径复核**（只读）：确认主对话历史数据确实是 `intent=''`

⑥ **变异自证**：摘掉一处 `_intent=` 必须判红（回滚走 try/finally + 收尾独立校验）

## 用法
    ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_chat_task_intent.py
"""
from __future__ import annotations

import ast
import json
import os
import sqlite3
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

COMMON = os.path.join(_ROOT, "agent", "pipeline_parts", "common.py")
STREAM = os.path.join(_ROOT, "agent", "pipeline_parts", "stream.py")
EXECUTE = os.path.join(_ROOT, "agent", "pipeline_parts", "execute.py")
DB = os.path.join(_ROOT, "mbse.db")

ROLE_PROBE = "CHAT_TASK_PROBE"
ROLE_REPAIR = "CHAT_TASK_REPAIR"
ROLE_ANSWER = "CHAT_TASK_ANSWER"
ROLES = (ROLE_PROBE, ROLE_REPAIR, ROLE_ANSWER)


def _chat_calls(path: str) -> list[ast.Call]:
    """返回该文件里所有 `llm_client.chat(...)` 调用的 AST 节点。"""
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        # 匹配 llm_client.chat / _llm.chat 之类的属性调用
        if isinstance(fn, ast.Attribute) and fn.attr == "chat":
            base = fn.value
            name = getattr(base, "id", None) or getattr(base, "attr", None)
            if name and "llm" in str(name).lower():
                out.append(node)
    return out


def _kw(node: ast.Call, name: str):
    for k in node.keywords:
        if k.arg == name:
            return k.value
    return None


def _intent_name(node: ast.Call) -> str | None:
    """返回 _intent 的语义名：字符串常量取字面值；Name 取变量名。"""
    v = _kw(node, "_intent")
    if v is None:
        return None
    if isinstance(v, ast.Constant) and isinstance(v.value, str):
        return v.value
    if isinstance(v, ast.Name):
        return v.id
    return "<expr>"


def _tools_kind(node: ast.Call) -> str:
    """粗判本调用的 tools 形态：tools / none / none_expr。"""
    v = _kw(node, "tools")
    if v is None:
        return "absent"
    if isinstance(v, ast.Constant) and v.value is None:
        return "none"
    return "expr"


def coverage_checks() -> list[tuple[str, bool]]:
    out: list[tuple[str, bool]] = []
    for label, path in (("stream.py（主路径）", STREAM), ("execute.py（冷路径）", EXECUTE)):
        calls = _chat_calls(path)
        missing = [c.lineno for c in calls if _intent_name(c) is None]
        out.append((
            f"★ {label}：{len(calls)} 个 chat 调用**全部**带 `_intent`"
            + (f"（缺失行 {missing}）" if missing else ""),
            bool(calls) and not missing,
        ))
    return out


def constant_checks() -> list[tuple[str, bool]]:
    with open(COMMON, encoding="utf-8") as f:
        common_src = f.read()
    const_defs = [r for r in ROLES if f"{r} = " in common_src]
    out = [("三个任务类型常量已在 common.py 定义", len(const_defs) == len(ROLES))]
    for label, path in (("stream.py", STREAM), ("execute.py", EXECUTE)):
        with open(path, encoding="utf-8") as f:
            src = f.read()
        used = [r for r in ROLES if r in src]
        out.append((f"{label} 引用了 {len(used)}/3 个常量", len(used) >= 1))
    return out


def role_semantic_checks() -> list[tuple[str, bool]]:
    """形式正确 ≠ 语义正确：带修复工具的必须标 REPAIR，无工具直出必须标 ANSWER。"""
    out: list[tuple[str, bool]] = []
    for label, path in (("stream.py", STREAM), ("execute.py", EXECUTE)):
        for node in _chat_calls(path):
            intent = _intent_name(node)
            tools = _tools_kind(node)
            lineno = node.lineno
            if tools == "none" and intent != ROLE_ANSWER:
                out.append((f"✗ {os.path.basename(path)}:{lineno} 无工具直出却标了 {intent}"
                            f"（应为 {ROLE_ANSWER}）", False))
            if intent == ROLE_REPAIR and tools != "expr":
                out.append((f"✗ {os.path.basename(path)}:{lineno} 标了 REPAIR 却没有修复工具"
                            f"（tools={tools}）", False))
    out.append(("★ 语义：tools=None⇒ANSWER、REPAIR⇒带工具，无违例", True))
    return out


def db_checks() -> list[tuple[str, bool]]:
    """真库只读复核：确认「主对话无 intent」这个前提是真的（不是推断）。"""
    out: list[tuple[str, bool]] = []
    if not os.path.exists(DB):
        return [("⚠️真库不存在，跳过口径复核（本判据非阻断）", True)]
    try:
        conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=10)
        row = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(COALESCE(reasoning_tokens,0)),0), "
            "COALESCE(SUM(completion_tokens),0), COALESCE(MAX(latency_ms),0) "
            "FROM llm_usage_stats WHERE intent='' AND COALESCE(call_failed,0)=0"
        ).fetchone()
        conn.close()
        n, rt, ct, ml = int(row[0]), int(row[1]), int(row[2]), int(row[3])
        ratio = (rt / ct * 100.0) if ct else 0.0
        print(f"    [口径] 实施前主对话(intent='')：n={n} 思考占比={ratio:.1f}% "
              f"最长={ml / 1000:.0f}s")
        out += [
            (f"实施前主对话确有大量调用（n={n} > 100）", n > 100),
            (f"实施前主对话思考占比高（{ratio:.1f}% > 30%）", ratio > 30.0),
        ]
    except Exception as e:  # noqa: BLE001
        out.append((f"⚠️ 真库口径复核跳过（{type(e).__name__}），非阻断", True))
    return out


def all_checks() -> list[tuple[str, bool]]:
    return (coverage_checks() + constant_checks()
            + role_semantic_checks() + db_checks())


def e2e_checks() -> list[tuple[str, bool]]:
    """端到端：走真实 LLMClient（**Mock 通道，不烧额度**）验 intent 真能落库可归因。

    ★ 为什么要这一维：AST 只证明「源码写了 `_intent=`」，
      真正的三道断链是 —— ① LLMClient 是否透传 kwargs
      ② provider 是否取到**同一个键**（同族键 `_task_desc` 取错就全盘失效）
      ③ 记账落库是否带上了 intent。
      前两道由 `_probe_chat_task_payload.py` 抓 payload 验；
      本函数验第三道（**落库可归因**）—— 它决定真库能否按任务维度做思考治理。
    """
    out: list[tuple[str, bool]] = []
    probe = os.path.join(_HERE, "_probe_chat_task_payload.py")
    if not os.path.exists(probe):
        return [("⚠️ 探针缺失，跳过端到端验（非阻断）", True)]
    import subprocess
    r = subprocess.run([sys.executable, "-X", "utf8", probe],
                       capture_output=True, text=True, timeout=180)
    line = [ln for ln in (r.stdout or "").splitlines() if "PROBE_RESULT" in ln]
    if not line:
        return [(f"探针可解析（rc={r.returncode}）", False)]
    d = json.loads(line[-1].split("PROBE_RESULT", 1)[1])
    out += [
        (f"① 真实链路：chat_probe 命中 off ⇒ 显式 disabled（实测 {d.get('chat_probe')}）",
         d.get("chat_probe") == {"type": "disabled"}),
        (f"① 真实链路：chat_answer 未命中 ⇒ 不发字段（实测 {d.get('chat_answer')}）",
         d.get("chat_answer") == "__ABSENT__"),
        (f"① 真实链路：chat_repair 未命中 ⇒ 不发字段（实测 {d.get('chat_repair')}）",
         d.get("chat_repair") == "__ABSENT__"),
        (f"① 真实链路：无 intent ⇒ 不发字段（实测 {d.get('__NONE__')}）",
         d.get("__NONE__") == "__ABSENT__"),
    ]

    # 落库可归因（Mock 通道：只验记账链路，不花额度）
    persist_probe = os.path.join(_HERE, "_probe_intent_persisted.py")
    if not os.path.exists(persist_probe):
        return out + [("⚠️落库探针缺失，非阻断", True)]
    r2 = subprocess.run([sys.executable, "-X", "utf8", persist_probe],
                        capture_output=True, text=True, timeout=180, cwd=_ROOT)
    got = [ln for ln in (r2.stdout or "").splitlines() if ln.startswith("E2E ")]
    if not got:
        return out + [(f"落库可归因探针可解析（rc={r2.returncode}; "
                       f"stderr={(r2.stderr or '')[-160:]!r}）", False)]
    intents = set(got[-1].split(" ", 1)[1].split(","))
    out += [
        (f"② 三种任务类型都落库可归因（实测 {sorted(intents)}）",
         {"chat_probe", "chat_answer", "chat_repair"} <= intents),
    ]
    return out


def main() -> int:
    print("=" * 74)
    print("判据 · 主对话任务类型标识（P0：覆盖面 —— 旧脚本缺的那一维）")
    print("=" * 74)
    ok = True
    for title, checks in [
        ("① 覆盖面对不对（AST 扫全部 chat 调用）", coverage_checks()),
        ("② 常量定义与引用", constant_checks()),
        ("③ 轮次角色语义", role_semantic_checks()),
        ("④ 真库口径复核（只读）", db_checks()),
        ("⑤ 端到端：真实 LLMClient（Mock 通道，不烧额度）", e2e_checks()),
    ]:
        print(f"\n{title}")
        for name, passed in checks:
            print(f"[{'OK  ' if passed else 'FAIL'}] {name}")
            ok = ok and passed

    print("\n" + "=" * 74)
    print(f"判据结论：{'全部通过' if ok else '存在未通过项'}")
    print("=" * 74)
    if not ok:
        return 1

    # ── 变异自证：摘掉 stream.py 里一处 _intent，必须判红 ────────────────
    print("\n[变异自证] 摘掉 stream.py 的一处 `_intent=CHAT_TASK_PROBE`，应判红")
    with open(STREAM, encoding="utf-8") as f:
        src = f.read()
    # ⚠️ 锚点用「子串」而非整行：2026-10-10 P1-b 给调用点追加了 `_model_params`，
    #   整行匹配会随接线参数变化而失效（判据自身变红，真代码没问题）。
    #   判据要守住的是「**这个调用点带 _intent**」，不是「这行长什么样」。
    anchor = "_intent=CHAT_TASK_PROBE, _model_params=_agent_mp"
    assert anchor in src, "锚点未找到（stream.py 的 PROBE 调用形态变了？）"
    mutated = src.replace(anchor, "_model_params=_agent_mp", 1)

    # ★ 回滚必须放 finally（此前教训：脚本崩在回滚之前 ⇒ 生产数据/代码被改坏）
    try:
        with open(STREAM, "w", encoding="utf-8") as f:
            f.write(mutated)
        red = not all(p for _, p in coverage_checks())
        print(f"[变异自证] {'已变红（覆盖面判据是有效的）' if red else '**仍是绿 ⇒ 覆盖面判据是空转断言**'}")
        ok = ok and red
    finally:
        with open(STREAM, "w", encoding="utf-8") as f:
            f.write(src)

    # 收尾独立校验：不能省（它能抓住"回滚代码本身写错"）
    with open(STREAM, encoding="utf-8") as f:
        assert f.read() == src, "回滚失败！stream.py 未逐字还原"
    print("[OK  ] 回滚校验：stream.py 已逐字还原")

    print("=" * 74)
    print(f"最终结论：{'全部通过' if ok else '未通过'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
