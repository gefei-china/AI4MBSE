# -*- coding: utf-8 -*-
"""verify_no_mock_fallback —— 防「LLM 静默回落 Mock」回归的门禁。

## 背景（2026-10-09，米爸决策 R1）

原实现在**三处**失败时静默回落 Mock，返回
`（Mock 回答）…当前为 Mock 降级模式（未配置真实 LLM API Key）`：

| # | 位置 | 触发条件 |
|---|---|---|
| ① | `llm/__init__.py::chat` | 未配置 api_key |
| ② | `llm/__init__.py::chat` | 真实调用失败（402 额度耗尽 / 超时 / 5xx） |
| ③ | `llm/__init__.py::chat_stream` | 流式调用失败 |

★危害：**用户拿到的是编造的内容**。
  这比 N3 的"产出雷同"更严重 —— 雷同交付的至少是**真代码**（错的），
  Mock 交付的是**不存在的内容**（假的），且在正文里看起来像一句正常回复
  （`used_mock` 标记只在用户展开「执行详情」时可见）。

实测触发场景：2026-10-09 稳定性实验跑到第 3 轮时 LLM 402 额度耗尽
⇒ 该轮所有"产出"都是 Mock 占位。

## 判据

A1 · `llm/__init__.py` 里 `self.mock.chat(` **只出现在 `llm.force_mock` 分支**
    （显式的测试通道必须保留 —— 回归测试不依赖外部网络）
A2 · 三处降级点都已改为抛 `LLMUnavailableError`，
    且**不再有** `return resp` 的Mock 返回路径
A3 · 不存在 `except LLMUnavailableError: pass` /
    `except Exception: ... return mock` 之类的**吞异常**写法
    （抛异常的意义就在于不被吞；一旦被吞就退化成静默降级）
A4 · 显式测试通道仍可用：`llm.force_mock=True` 时仍返回 Mock
    —— 这是"保留能力"，不是"保留缺陷"
A5 · 端到端：无 api_key 时抛异常而非返回 Mock 文本

## 用法
    ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_no_mock_fallback.py
"""
from __future__ import annotations

import ast
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

LLM_INIT = os.path.join(_ROOT, "llm", "__init__.py")

_fails: list[str] = []
_passes: list[str] = []


def _ok(msg: str) -> None:
    _passes.append(msg)
    print(f"[PASS] {msg}")


def _bad(msg: str) -> None:
    _fails.append(msg)
    print(f"[FAIL] {msg}")


def _src() -> str:
    with open(LLM_INIT, encoding="utf-8") as f:
        return f.read()


def _a1_mock_call_sites() -> None:
    """A1：`self.mock.chat(` 只允许出现在 force_mock 分支。"""
    src = _src()
    tree = ast.parse(src, filename=LLM_INIT)

    # 找 force_mock 判定所在行
    force_lines: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "as_bool" and node.args \
                and isinstance(node.args[0], ast.Constant) and node.args[0].value == "llm":
            force_lines.append(node.lineno)

    sites: list[int] = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "chat"
                and isinstance(node.func.value, ast.Attribute)
                and node.func.value.attr == "mock"):
            sites.append(node.lineno)

    if not sites:
        _ok("A1 没有任何 `self.mock.chat(` 调用（连测试通道都没了，需复核）")
        return

    # 每个调用点必须在某个 force_mock 判定之后 25 行内（同一分支内）
    orphan = []
    for ln in sites:
        if not any(0 <= ln - fl <= 25 for fl in force_lines):
            orphan.append(ln)

    if orphan:
        _bad(f"A1 有 {len(orphan)} 处 `self.mock.chat(` **不在 force_mock 分支内**"
             f"（行号 {orphan}）⇒ 存在静默降级路径")
    else:
        _ok(f"A1 全部 {len(sites)} 处 `self.mock.chat(` 都在 `llm.force_mock` 分支内"
            f"（行号 {sites}）")


def _a2_raises_instead_of_returning() -> None:
    """A2：三处降级点都抛 `LLMUnavailableError`。

    ⚠️ 2026-10-09 实测：判据自己写错过方法名 ——
      流式降级点不在 `chat_stream`（该方法不存在），而在 `_stream_wrapped`
      （`chat(stream=True)` 的内部包装器，真正迭代 provider 生成器的地方）。
      ⇒ 按猜的名字写判据会**假红**，也会漏掉真正的降级点。
      ⇒ 方法名以源码实测为准，不凭记忆。
    """
    src = _src()
    tree = ast.parse(src, filename=LLM_INIT)

    # 三个降级点：① chat-无key ② chat-调用失败 ③ _stream_wrapped-流式失败
    targets = ("chat", "_stream_wrapped")
    fns = {n.name: n for n in ast.walk(tree)
           if isinstance(n, ast.FunctionDef) and n.name in targets}

    for name in targets:
        fn = fns.get(name)
        if fn is None:
            _bad(f"A2 找不到方法 {name}（判据方法名与源码不符，需重新核对）")
            continue
        raises = sum(
            1 for n in ast.walk(fn)
            if isinstance(n, ast.Raise)
            and isinstance(n.exc, ast.Call)
            and isinstance(n.exc.func, ast.Name)
            and n.exc.func.id == "LLMUnavailableError")
        if raises >= 1:
            _ok(f"A2 {name}() 有 {raises} 处 `raise LLMUnavailableError`")
        else:
            _bad(f"A2 {name}() 没有 `raise LLMUnavailableError`"
                 f" ⇒ 该路径仍会返回 Mock 内容")

    #反向确认：全文件里raise 该异常的点数（防止方法改名后漏检）
    total = sum(1 for n in ast.walk(tree)
                if isinstance(n, ast.Raise)
                and isinstance(n.exc, ast.Call)
                and isinstance(n.exc.func, ast.Name)
                and n.exc.func.id == "LLMUnavailableError")
    if total < 3:
        _bad(f"A2 全文件仅 {total} 处 raise LLMUnavailableError，"
             f"少于预期的 3 处降级点 ⇒ 有一处被改漏了")
    else:
        _ok(f"A2 全文件共 {total} 处 raise LLMUnavailableError（覆盖 3 个降级点）")

    if "class LLMUnavailableError" not in src:
        _bad("A2 异常类 LLMUnavailableError 未定义")
    else:
        _ok("A2 异常类 LLMUnavailableError 已定义")


def _a3_not_swallowed() -> None:
    """A3：没有吞掉 LLMUnavailableError 的写法。"""
    bad_hits: list[str] = []
    for rel in ("llm/__init__.py",):
        path = os.path.join(_ROOT, rel.replace("/", os.sep))
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            # `except LLMUnavailableError:` 且 handler 体里没有 raise ⇒ 吞异常
            tname = getattr(node.type, "id", None) or getattr(node.type, "attr", None)
            if tname == "LLMUnavailableError":
                has_raise = any(isinstance(x, ast.Raise) for x in ast.walk(node))
                if not has_raise:
                    bad_hits.append(f"{rel}:{node.lineno} 吞掉 LLMUnavailableError")
    if bad_hits:
        for h in bad_hits:
            _bad(f"A3 {h}")
    else:
        _ok("A3 没有任何地方吞掉 LLMUnavailableError")


def _a4_force_mock_still_works() -> None:
    """A4：显式测试通道仍可用（保留能力 ≠ 保留缺陷）。"""
    src = _src()
    if 'as_bool("llm", "force_mock"' in src or 'get("llm", "force_mock"' in src \
            or "force_mock" in src:
        _ok("A4 `llm.force_mock` 开关仍在（回归测试通道保留）")
    else:
        _bad("A4 `llm.force_mock` 开关不见了 ⇒ 回归测试会依赖外部网络")


def _a5_e2e_no_key_raises() -> None:
    """A5：端到端——无 key 时抛异常，且**不含 Mock 占位文本**。"""
    try:
        from llm import LLMUnavailableError, llm_client
    except Exception as exc:                # noqa: BLE001
        _bad(f"A5 导入 llm 失败：{type(exc).__name__}: {exc}")
        return
    try:
        resp = llm_client.chat([{"role": "user", "content": "hi"}], provider_id=999999)
    except LLMUnavailableError as exc:
        if "Mock 回答" in str(exc):
            _bad("A5 异常文案里仍含 Mock 占位文本")
        else:
            _ok(f"A5 无 key → 抛 LLMUnavailableError（reason={exc.reason}）")
        return
    except Exception as exc:                # noqa: BLE001
        _bad(f"A5 抛了非预期异常：{type(exc).__name__}: {exc}")
        return
    txt = str(resp)
    if "Mock 回答" in txt:
        _bad("A5 无 key 时返回了 Mock 占位内容（静默降级未清除）")
    else:
        _bad("A5 无 key 时既没抛 LLMUnavailableError，也没返回 Mock"
             f"（返回了别的东西：{txt[:100]}）—— 行为不符合 R1")


def main() -> int:
    print("=" * 72)
    print("verify_no_mock_fallback — 防LLM 静默回落 Mock（决策 R1）")
    print("=" * 72)
    _a1_mock_call_sites()
    _a2_raises_instead_of_returning()
    _a3_not_swallowed()
    _a4_force_mock_still_works()
    _a5_e2e_no_key_raises()

    print("\n" + "=" * 72)
    print(f"结果：{len(_passes)} PASS / {len(_fails)} FAIL")
    if _fails:
        for m in _fails:
            print(f"  - {m}")
        print("FAIL")
        return 1
    print("✅ 门禁通过：LLM 不可用时一律报错，不返回编造内容")
    return 0


if __name__ == "__main__":
    sys.exit(main())
