"""verify_thinking_shape — 判据：思考分流**规则化**（可维护性，米爸 2026-10-10）。

## 要解决的问题（米爸原话："agent、技能、工具都是暂时的，后续会增加新的意图、任务"）

`_intent` 是**动态**的，不是固定枚举：
  · `orchestration.py:111` 直接用 `agent_def.name` 当 intent；
  · `agents` 表实测有 **27 个** active Agent，**名字含中文**（「活动图生成」「需求视图生成」…）；
  · skills / tools 后续还会继续增。

⇒ 纯名单制（`thinking_policy.off` 枚举 intent 名）**必然随系统演进而漂移**。
   实测当前覆盖率：对 27 个 Agent 名的命中数 = **0/27**。
   ⇒ 新增一个 Agent，若没人记得把它加进名单，它的思考就完全不受控
   （实测最坏形态：思考占 completion **99.2%**、耗时 **4017 秒**）。

## 方案：三层优先级（规则只做加法，不夺走例外权）

| 层 | 依据 | 覆盖能力 | 默认 |
|---|---|---|---|
| 1 显式名单 | `thinking_policy.on/off` | 人配的例外 | 已有 |
| **2 形态规则** | **任务形态信号**（带 tools？输出上限？） | ★**全部 intent，含未来新增** | **关** |
| 3 兜底 | 都不命中 ⇒ 不发字段 | = 改动前行为 | — |

## 判据
① 形态开关已登记 schema，且**默认关闭**（新增能力不该默认改行为）
② **默认关闭时行为零漂移**（行为验，非字符串锚点）
③ ★ **开启后能覆盖「不在名单里的 intent」**——这是可维护性的核心断言
④ ★ 规则**只加不减**：名单显式 `on` 的意图，规则不得反转它
⑤ ★ **阈值不误伤重产码**：大 max_tokens 的产码任务不得被判关
⑥ 变异自证

## 用法
    ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_thinking_shape.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

PROVIDER = os.path.join(_ROOT, "llm", "providers", "openai_compat.py")

#: 用真实 Agent 名做"新增 intent"模拟 —— 它**不在**任何名单里，
#: 正是"系统演进后新增的任务"的真实形态。
AGENT_NAME_PROBE = "活动图生成"


def _payload(intent: str, *, has_tools: bool, max_tokens: int,
             shape_on: bool, policy: dict):
    """跑一次真实 provider，抓实际下发的 thinking 字段（假 httpx，不烧额度）。

    返回值**保持 dict 类型**（`{"type":"disabled"}` / `__ABSENT__` 字符串）。
    ⚠️ 踩过的坑：早先版本返回 `str(dict)`，判据里又拿它跟 dict 比 →
    行为完全正确却被判红（`'{...}' != {...}`）。**判据要比较结构，不比较字符串。**
    """
    tools = [{"type": "function", "function": {"name": "t", "parameters": {}}}] if has_tools else None
    script = f"""
import json, httpx, core.config as C
R = C.get
POL = {policy!r}
def g(gr, k, d=None):
    if gr == 'llm' and k == 'thinking_policy':
        return json.dumps(POL)
    if gr == 'llm' and k == 'thinking_shape_enabled':
        return {shape_on!r}
    if gr == 'llm' and k == 'shape_probe_max_tokens':
        return 1200
    if gr == 'llm' and k == 'shape_cheap_max_tokens':
        return 0
    return R(gr, k, d)
C.get = g
cap = []
def fp(u, json=None, **k):
    cap.append(json or {{}})
    raise RuntimeError('STOP')
httpx.post = fp
from llm.providers.openai_compat import OpenAICompatProvider as P
cfg = {{'id': 9004, 'name': 'p', 'model_name': 'm',
       'base_url': 'https://x.invalid', 'api_key': 'k',
       'max_tokens': {max_tokens}, 'context_window': 128000,
       'temperature': 0.3, 'model_params': '{{}}', 'status': 'active'}}
try:
    P(dict(cfg)).chat([{{'role': 'user', 'content': 'x'}}],
                      tools={tools!r}, _intent={intent!r})
except Exception:
    pass
print('SHAPE', json.dumps(cap[-1].get('thinking','__ABSENT__'))
      if cap else '__NO_CALL__')
"""
    r = subprocess.run([sys.executable, "-X", "utf8", "-c", script],
                       capture_output=True, text=True, timeout=120, cwd=_ROOT)
    for ln in (r.stdout or "").splitlines():
        if ln.startswith("SHAPE "):
            raw = ln.split(" ", 1)[1]
            if raw in ("__ABSENT__", "__NO_CALL__") or raw.startswith("__ERR__"):
                return raw
            try:
                return json.loads(raw)
            except Exception:
                return raw
    return f"__ERR__rc={r.returncode}"


def main() -> int:
    print("=" * 74)
    print("判据 · 思考分流规则化（可维护性：覆盖未来新增的 intent/Agent）")
    print("=" * 74)
    ok = True

    from core import config
    from core.config import CONFIG_SCHEMA
    llm_schema = CONFIG_SCHEMA.get("llm", {})

    # ① 配置
    print("\n① 配置与 schema")
    for name, passed in [
        ("`llm.thinking_shape_enabled` 已登记 CONFIG_SCHEMA",
         "thinking_shape_enabled" in llm_schema),
        ("`llm.shape_probe_max_tokens` 已登记 CONFIG_SCHEMA",
         "shape_probe_max_tokens" in llm_schema),
        # ★ 2026-10-10：规则已由**默认 off 改为默认 on** —— 依据是真实 A/B
        #   （flashY，id=10035~10038：关思考后 tool_calls 保留 1→1）。
        #   ⇒ 判据从「断言默认值」改成「断言**开启状态下的行为正确**」
        #     （第 ② 步已按两种状态分别验证）。
        #   ⚠️ 若将来换 provider/模型，判据不会自动提醒重跑 A/B ——
        #     这一点由文档注释与 `verify_real_thinking_ab.py` 的存在来承担。
        ("形态规则当前为**启用**（A/B 证据支持；换模型建议重跑 A/B）",
         config.as_bool("llm", "thinking_shape_enabled", False)),
    ]:
        print(f"[{'OK  ' if passed else 'FAIL'}] {name}")
        ok = ok and passed

    # ② 默认关闭 ⇒ 零漂移（用一个**探针语义**的 intent，它才会被规则命中）
    print("\n② 默认关闭时行为零漂移（用探针语义 intent，它本该被规则命中）")
    got = _payload("probe_liveness", has_tools=True, max_tokens=800,
                   shape_on=False, policy={"off": ["routing"], "on": []})
    for name, passed in [
        (f"★ 规则关闭 + 探针 intent + 带工具 + 小上限 ⇒ 不发 thinking（实测 {got}）",
         got == "__ABSENT__"),
    ]:
        print(f"[{'OK  ' if passed else 'FAIL'}] {name}")
        ok = ok and passed

    # ③ ★ 核心：规则开启后能覆盖「不在名单里的 intent」
    print("\n③ 规则开启 ⇒ 覆盖名单外的 intent（可维护性的核心断言）")
    got2 = _payload("probe_liveness", has_tools=True, max_tokens=800,
                    shape_on=True, policy={"off": ["routing"], "on": []})
    for name, passed in [
        (f"★ 探针语义 intent「probe_liveness」**不在名单**也能被规则覆盖"
         f"（实测 {got2}）", got2 == {"type": "disabled"}),
    ]:
        print(f"[{'OK  ' if passed else 'FAIL'}] {name}")
        ok = ok and passed

    # ③b ★ 反向：规则**不得**误伤"带工具 + 上限小"的重产码任务
    #   实测依据：`view_expansion`（视图生成）max_ct 只有 76，
    #   它"带 tools + 上限小"却**是重产码** ⇒ 只看 max_tokens 就会误关。
    print("\n③b ★ 反向：规则不得误伤「带工具+上限小」的重产码任务")
    got2b = _payload(AGENT_NAME_PROBE, has_tools=True, max_tokens=76,
                     shape_on=True, policy={"off": [], "on": []})
    for name, passed in [
        (f"★ Agent 名「{AGENT_NAME_PROBE}」（max_tokens=76）⇒ 不发thinking"
         f"（实测 {got2b}）", got2b == "__ABSENT__"),
    ]:
        print(f"[{'OK  ' if passed else 'FAIL'}] {name}")
        ok = ok and passed

    # ④ 规则只加不减：名单显式 on 的意图，规则不得反转
    print("\n④ 名单优先级高于规则（规则只做加法）")
    got3 = _payload("routing", has_tools=True, max_tokens=800,
                    shape_on=True, policy={"off": [], "on": ["routing"]})
    for name, passed in [
        (f"名单 on 的意图不会被规则反转（实测 {got3}）",
         got3 == {"type": "enabled"}),
    ]:
        print(f"[{'OK  ' if passed else 'FAIL'}] {name}")
        ok = ok and passed

    # ⑤ 阈值不误伤重产码
    print("\n⑤ 大输出上限的产码任务不得被误判关")
    got4 = _payload("probe_liveness", has_tools=True, max_tokens=8192,
                    shape_on=True, policy={"off": [], "on": []})
    for name, passed in [
        (f"★ max_tokens=8192（重产码量级）⇒ 不发 thinking（实测 {got4}）",
         got4 == "__ABSENT__"),
    ]:
        print(f"[{'OK  ' if passed else 'FAIL'}] {name}")
        ok = ok and passed

    print("\n" + "=" * 74)
    print(f"判据结论：{'全部通过' if ok else '存在未通过项'}")
    print("=" * 74)
    if not ok:
        return 1

    # ── 变异自证 ──────────────────────────────────────────────────────
    # ⚠️ **踩过的坑（变异打错位置 ⇒ 判据空转）**：第一版变异把
    #   `as_bool("llm","thinking_shape_enabled", False)` 的默认值改成 True，
    #   结果判据**仍绿**。原因：本脚本的假 `C.get` **拦截了上游取值**，
    #   `as_bool` 拿到的永远是探针给的 False ⇒ 改默认值根本影响不到行为。
    #   ⇒ 教训：**变异必须打在"判据观测得到"的那一层**，
    #     否则会得出"判据没用"的错误结论（这里恰好相反：判据有用，变异打偏了）。
    # 正确位置：让 `as_bool` **恒为真**（绕过配置），这才模拟"规则被意外打开"。
    print("\n[变异自证] 让规则绕过配置恒为启用，应判红（默认关闭这条就守不住了）")
    with open(PROVIDER, encoding="utf-8") as f:
        src = f.read()
    anchor = 'if not config_mod.as_bool("llm", "thinking_shape_enabled", False):'
    assert anchor in src, "锚点未找到"
    mutated = src.replace(anchor, "if False:", 1)
    try:
        with open(PROVIDER, "w", encoding="utf-8") as f:
            f.write(mutated)
        red_got = _payload("probe_liveness", has_tools=True, max_tokens=800,
                           shape_on=False, policy={"off": ["routing"], "on": []})
        red = red_got != "__ABSENT__"
        print(f"[变异自证] 规则关闭时实测 {red_got} ⇒ "
              f"{'已变红（判据有效）' if red else '**仍是绿 ⇒ 判据空转**'}")
        ok = ok and red
    finally:
        with open(PROVIDER, "w", encoding="utf-8") as f:
            f.write(src)

    with open(PROVIDER, encoding="utf-8") as f:
        assert f.read() == src, "回滚失败！provider 未逐字还原"
    print("[OK  ] 回滚校验：provider 已逐字还原")

    print("=" * 74)
    print(f"最终结论：{'全部通过' if ok else '未通过'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
