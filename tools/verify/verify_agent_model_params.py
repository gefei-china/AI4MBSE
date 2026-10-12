"""verify_agent_model_params — 判据：Agent 级 `model_params` 不是死配置（P1-b，2026-10-10）。

## 问题

`agents.model_params` 这一列**一直存在**（`database/schema.py:830` 注释写着
`{"temperature":0.2}`），前端 Agent 表单也在存，**但全仓没有任何消费点**
⇒ **在 Agent 页面配参数，配了不生效**。

对比：`model_provider_id` 同样在 `agents` 表，但**有**消费链
（`registry.get_provider_id()` → `stream.py:1341` / `execute.py:155`）
⇒ 一个能用、一个不能用，差别只在"有没有人读它"。

## 本次接线
| 位置 | 内容 |
|---|---|
| `agent/registry.py:load_from_db` | 把 `model_params` 装进 `_db_meta`（此前漏了） |
| `agent/registry.py:get_model_params()` | 新增消费方法（返回**副本**，防污染缓存） |
| `llm/providers/openai_compat.py` | provider 级与 Agent 级**浅合并，Agent 优先** |
| `stream.py` / `execute.py` | 主对话 10 个调用点传 `_model_params=_agent_mp` |

语义分层：**调用点显式传参 > Agent 级 > provider 级 > 模型默认**。

## 判据
① `model_params` 进了 `_db_meta`（装载）
② `get_model_params()` 能读出，且**返回副本**（改返回值不污染缓存）
③ ★ **端到端**：Agent 级 `thinking_mode=关闭` ⇒ 真的发出 `disabled`
   （**这是核心**：证明不再是死配置）
④ ★优先级：Agent 级覆盖 provider 级
⑤ 未配 Agent 参数时**零漂移**（不改变现有行为）
⑥ 变异自证

## 用法
    ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_agent_model_params.py
"""
from __future__ import annotations

import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

REGISTRY = os.path.join(_ROOT, "agent", "registry.py")
PROVIDER = os.path.join(_ROOT, "llm", "providers", "openai_compat.py")


def _payload(*, agent_mp, provider_mp: str, intent: str) -> str:
    """真跑 provider，抓下发 payload 的 thinking 字段（假 httpx，不烧额度）。

    ⚠️ **踩过的坑**：`agent_mp` 一开始收 **JSON 字符串**，脚本里用 `{x!r}` 注入
       ⇒ 变成「字符串字面量」而不是 dict ⇒ `mp.get("thinking_mode")` 拿到 str
       ⇒ `.get` 不了 ⇒ 静默不生效，判据却报"实测 __ABSENT__"
       （看起来像"代码没接线"，实际是**传参类型错了**）。
       ⇒ 因此 `agent_mp` 现在直接收 **dict 或 None**（None = 完全不传）。
    """
    script = f"""
import json, httpx
from llm.providers.openai_compat import OpenAICompatProvider as P
cap = []
def fp(u, json=None, **k):
    cap.append(json or {{}})
    raise RuntimeError('STOP')
httpx.post = fp
cfg = {{'id': 9005, 'name': 'p', 'model_name': 'm',
       'base_url': 'https://x.invalid', 'api_key': 'k',
       'max_tokens': 4096, 'context_window': 128000, 'temperature': 0.3,
       'model_params': {provider_mp!r}, 'status': 'active'}}
try:
    P(dict(cfg)).chat([{{'role': 'user', 'content': 'x'}}],
                      _intent={intent!r}, _model_params={agent_mp!r})
except Exception:
    pass
print('AMP', (json.dumps(cap[-1]['thinking'])
              if cap and 'thinking' in cap[-1] else '__ABSENT__')
      if cap else '__NO_CALL__')
"""
    r = subprocess.run([sys.executable, "-X", "utf8", "-c", script],
                       capture_output=True, text=True, timeout=120, cwd=_ROOT)
    for ln in (r.stdout or "").splitlines():
        if ln.startswith("AMP "):
            return ln.split(" ", 1)[1]
    return f"__ERR__rc={r.returncode}"


def main() -> int:
    print("=" * 74)
    print("判据 · Agent 级 model_params 不是死配置（P1-b）")
    print("=" * 74)
    ok = True

    # ① 装载
    print("\n① registry 装载与消费方法")
    with open(REGISTRY, encoding="utf-8") as f:
        reg_src = f.read()
    for name, passed in [
        ("`model_params` 已装进 `_db_meta`", '"model_params": _parse_json_obj(' in reg_src),
        ("新增 `get_model_params()` 消费方法", "def get_model_params(" in reg_src),
    ]:
        print(f"[{'OK  ' if passed else 'FAIL'}] {name}")
        ok = ok and passed

    # ② 返回副本（防污染长生命周期缓存）
    r2 = subprocess.run(
        [sys.executable, "-X", "utf8", "-c",
         "import agent.registry as R;"
         "reg=R.AgentRegistry();"
         "reg._db_meta={'x':{'model_params':{'thinking_mode':'关闭'}}};"
         "a=reg.get_model_params('x');"
         "a['thinking_mode']='篡改';"      # 就地改返回值
         "b=reg.get_model_params('x');"
         "print('COPY_OK' if b.get('thinking_mode')=='关闭' else 'COPY_LEAK')"],
        capture_output=True, text=True, timeout=120, cwd=_ROOT)
    leaked = "COPY_LEAK" in (r2.stdout or "")
    print(f"[{'OK  ' if not leaked else 'FAIL'}] `get_model_params()` 返回副本"
          f"（改动不污染 registry 缓存）")
    ok = ok and not leaked

    # ③ ★ 端到端：Agent 级真的生效
    print("\n③ ★ 端到端：Agent 级 thinking_mode 真的生效（不再是死配置）")
    got = _payload(agent_mp={"thinking_mode": "关闭"},
                   provider_mp="{}", intent="view_expansion")
    for name, passed in [
        (f"Agent 级 thinking_mode=关闭 ⇒ 实发 disabled（实测 {got}）",
         got == '{"type": "disabled"}'),
    ]:
        print(f"[{'OK  ' if passed else 'FAIL'}] {name}")
        ok = ok and passed

    # ④ Agent 级覆盖 provider 级
    print("\n④ 优先级：Agent 级覆盖 provider 级")
    got2 = _payload(agent_mp={"thinking_mode": "关闭"},
                    provider_mp='{"thinking_mode": "开启"}', intent="view_expansion")
    for name, passed in [
        (f"provider=开启 + Agent=关闭 ⇒ 听 Agent 的（实测 {got2}）",
         got2 == '{"type": "disabled"}'),
    ]:
        print(f"[{'OK  ' if passed else 'FAIL'}] {name}")
        ok = ok and passed

    # ⑤ 未配 Agent 参数 ⇒ 零漂移
    print("\n⑤ 未配 Agent 参数时零漂移")
    # ★ provider_mp 故意配 thinking_mode=开启：验证"Agent 没配"时**沿用 provider 级**，
    #   而不是因为 Agent 传了空 dict 就把 provider 的设置吃掉。
    got3 = _payload(agent_mp={}, provider_mp='{"thinking_mode": "开启"}',
                    intent="view_expansion")
    got4 = _payload(agent_mp=None, provider_mp="{}", intent="view_expansion")
    for name, passed in [
        (f"Agent 未配⇒ 沿用 provider 级的「开启」（实测 {got3}）",
         got3 == '{"type": "enabled"}'),
        (f"完全不传 Agent 参数且 provider 也未配 ⇒ 不发thinking（实测 {got4}）",
         got4 == '__ABSENT__'),
    ]:
        print(f"[{'OK  ' if passed else 'FAIL'}] {name}")
        ok = ok and passed

    print("\n" + "=" * 74)
    print(f"判据结论：{'全部通过' if ok else '存在未通过项'}")
    print("=" * 74)
    if not ok:
        return 1

    # ── 变异自证：摘掉 provider 侧的 Agent 合并逻辑，必须判红 ────────────
    print("\n[变异自证] 摘掉 provider 侧的 Agent 级合并，应判红（退回死配置）")
    with open(PROVIDER, encoding="utf-8") as f:
        src = f.read()
    anchor = '_amp = kwargs.get("_model_params") or kwargs.get("model_params") or {}'
    assert anchor in src, "锚点未找到"
    try:
        with open(PROVIDER, "w", encoding="utf-8") as f:
            f.write(src.replace(anchor, '_amp = {}  # 变异：不读 Agent 级', 1))
        red_got = _payload(agent_mp={"thinking_mode": "关闭"},
                           provider_mp="{}", intent="view_expansion")
        red = red_got != '{"type": "disabled"}'
        print(f"[变异自证] 实测 {red_got} ⇒ "
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
