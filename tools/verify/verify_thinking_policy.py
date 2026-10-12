"""verify_thinking_policy — 判据：thinking 按 intent **三态**分流（L1，米爸"实施"）。

## 要修的两个缺陷（实施前先发现）
★ **缺陷 1（致命）**：原实现 `if thinking:` 只在开启时发字段，
   关闭时**什么都不发** ⇒ 模型端按**默认（开启）**思考
   ⇒ **我方的「关闭思考」从来没生效过**（实测思考占completion 63% 即为证据）。

★ **缺陷 2（陷阱）**：若把"关闭"改成**总是**发 disabled，
   则全链路（`chat()` 默认 `thinking=False`，实测**无一处**传 True）
   **全部被关掉思考** ⇒ 重产码任务（视图生成，正文 1.5 万 token）也会一起受害。
   **修缺陷 1 时若不做三态，会造出比原来更大的事故。**

## 三态语义
| 情况 | 下发 |
|---|---|
| 命中 `off` 列表 | 显式 `{"type":"disabled"}` |
| 命中 `on` 列表 | 显式 `{"type":"enabled"}` |
| **其余 / 无 intent** |★ **完全不发该字段**（保持改动前行为） |

## 粒度：**任务维度（per-intent）**
依据：① 标杆（Qwen / DeepSeek / Claude）**全是 per-request**，无一家会话级；
② 一次会话跨多个 intent（八视图链式生成跨 8 个 agent）⇒ 会话级无法表达分步控制；
③ 实测分界恰好落在 intent 上（廉价任务思考占 58~86%，重产码任务 0~1.4%）。

## 判据
① `thinking_policy` 已登记 `CONFIG_SCHEMA`，默认 `{}`（不干预）
② 三态下发（探针实测，**用假 httpx 不烧额度**）
③ ★ **防事故**：未命中 / 无 intent ⇒ 不发字段（不能被一起关掉）
④ 变异自证

## 用法
    ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_thinking_policy.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)          # ★ 漏这行 ⇒ ModuleNotFoundError: core

PROVIDER = os.path.join(_ROOT, "llm", "providers", "openai_compat.py")
PROBE = os.path.join(_HERE, "_probe_thinking_policy.py")


def config_checks() -> list[tuple[str, bool]]:
    from core import config
    from core.config import CONFIG_SCHEMA
    out = [
        ("`llm.thinking_policy` 已登记 CONFIG_SCHEMA",
         "thinking_policy" in CONFIG_SCHEMA.get("llm", {})),
        # 默认已含 off 列表（纯收益项）；**on 列表必须为空** ——
        # 给重产码任务"开思考"是质量赌注（未做 A/B 实测），不该默认上。
        ("默认配置可解析为 JSON", isinstance(
            json.loads(config.get("llm", "thinking_policy", "{}")), dict)),
        ("★ 默认 **on 列表为空**（不擅自给重产码任务加思考）",
         not (json.loads(config.get("llm", "thinking_policy", "{}")).get("on") or [])),
        # ★ 2026-10-10：off 列表新增 `chat_probe`（主对话探针轮）——
        #   依据**真实 A/B**（flashY，真库 id=10035~10038）：
        #   关思考后 tool_calls 保留（1→1），难任务 token 省 78%、耗时省 73%。
        ("★ off 列表含实测确认的 4 个廉价任务 + `chat_probe`（有A/B 证据）",
         set(json.loads(config.get("llm", "thinking_policy", "{}")).get("off") or [])
         == {"crypto_probe", "probe", "routing", "doc_summary", "chat_probe"}),
        #★ 反向护栏：**答案轮/修复轮不得进 off** ——
        #   它们正文长、思考确有收益，关掉是质量赌注（未做 A/B）。
        ("★ 答案轮/修复轮**不在** off 列表（未验证不关）",
         not ({"chat_answer", "chat_repair"}
              & set(json.loads(config.get("llm", "thinking_policy", "{}")).get("off") or []))),
    ]
    return out


def probe_checks() -> list[tuple[str, bool]]:
    out = [("探针文件存在", os.path.exists(PROBE))]
    if not os.path.exists(PROBE):
        return out
    r = subprocess.run([sys.executable, "-X", "utf8", PROBE],
                       capture_output=True, text=True, timeout=120)
    line = [ln for ln in (r.stdout or "").splitlines() if "PROBE_RESULT" in ln]
    if not line:
        return out + [(f"探针可解析（rc={r.returncode}; "
                       f"stderr={(r.stderr or '')[-200:]!r}）", False)]
    d = json.loads(line[-1].split("PROBE_RESULT", 1)[1])
    out += [
        (f"① 命中 off ⇒ 显式 disabled（实测 {d['off']}）",
         d["off"] == {"type": "disabled"}),
        (f"   第二个 off 同样生效（实测 {d['off2']}）",
         d["off2"] == {"type": "disabled"}),
        (f"② 命中 on ⇒ 显式 enabled（实测 {d['on']}，即便调用方 thinking=False）",
         d["on"] == {"type": "enabled"}),
        # ★ 这两条是**防事故**判据：不能"总是发 disabled"
        (f"③ 未命中 intent ⇒ **不发字段**（实测 {d['miss']}）",
         d["miss"] == "__ABSENT__"),
        (f"③ 无 intent ⇒ **不发字段**（实测 {d['no_intent']}）",
         d["no_intent"] == "__ABSENT__"),
    ]
    return out


def source_checks() -> list[tuple[str, bool]]:
    with open(PROVIDER, encoding="utf-8") as f:
        src = f.read()
    return [
        ("代码里存在 `disabled` 下发（真关闭）",
         '"type": "disabled"' in src),
        ("读 `llm.thinking_policy` 配置",
         'get("llm", "thinking_policy"' in src),
    ]


def disabled_guard_check() -> list[tuple[str, bool]]:
    """★ 「disabled 下发受条件约束」用**行为**验，不用字符串锚点验。

    为什么改行为验：本轮已因接线参数变化让字符串锚点**误报两次**
    （`elif _off and _who in _off:` → 形态规则 → 又加 `_tmode_explicit`）。
    每次改代码形态都要改判据，判据就会逐步变成"跟着实现走"的同义反复——
    它再也抓不住"实现被改坏"。

    真正的底线只有一条：**不能「总是发 disabled」**（缺陷 2）。
    验法：把 `thinking_shape_enabled` 打开 + 给一个**不在任何名单**、
    但**带工具且 max_tokens 很大**的 intent ⇒ 规则不该命中、
    名单也没命中 ⇒ 必须**不发字段**。若实现退化成"总是发"，这里立刻变红。
    """
    import os as _os
    import subprocess as _sp
    import sys as _sys

    script = (
        "import json,httpx,core.config as C\n"
        "R=C.get\n"
        "def g(gr,k,d=None):\n"
        "    if gr=='llm' and k=='thinking_policy': return json.dumps({'off':['routing'],'on':[]})\n"
        "    if gr=='llm' and k=='thinking_shape_enabled': return True\n"
        "    if gr=='llm' and k=='shape_probe_max_tokens': return 1200\n"
        "    return R(gr,k,d)\n"
        "C.get=g\n"
        "cap=[]\n"
        "def fp(u,json=None,**k):\n"
        "    cap.append(json or {})\n"
        "    raise RuntimeError('STOP')\n"
        "httpx.post=fp\n"
        "from llm.providers.openai_compat import OpenAICompatProvider as P\n"
        "cfg={'id':9006,'name':'p','model_name':'m','base_url':'https://x.invalid',\n"
        "     'api_key':'k','max_tokens':8192,'context_window':128000,'temperature':0.3,\n"
        "     'model_params':'{}','status':'active'}\n"
        "try:\n"
        "    P(dict(cfg)).chat([{'role':'user','content':'x'}],\n"
        "        tools=[{'a':1}],_intent='plan_refine')\n"
        "except Exception:\n"
        "    pass\n"
        "print('GUARD', json.dumps(cap[-1]['thinking'])\n"
        "      if cap and 'thinking' in cap[-1] else '__ABSENT__')\n"
    )
    r = _sp.run([_sys.executable, "-X", "utf8", "-c", script],
                capture_output=True, text=True, timeout=120, cwd=_ROOT)
    line = [ln for ln in (r.stdout or "").splitlines() if ln.startswith("GUARD")]
    if not line:
        return [(f"防事故探针可解析（rc={r.returncode}）", False)]
    got = line[-1].split(" ", 1)[1]
    return [
        ("★ 防事故：规则开+ 名单未命中 + 大 max_tokens ⇒ **不发** thinking"
         f"（实测 {got}）—— 证明不是「总是发disabled」", got == "__ABSENT__"),
    ]


def shape_default_off_check() -> list[tuple[str, bool]]:
    """★ 「形态规则默认关闭」用**行为**验，不用字符串锚点验。

    为什么要行为验：这个开关是**新增能力**，默认必须**零行为漂移**。
    锚点验（找某个字符串）会因写法微调而误报/漏报—— 本轮就误报过一次。
    行为验（真发一次请求，看payload 有没有多出 thinking 字段）**不会**被写法漂移影响。

    判据：把 `thinking_shape_enabled` 设为 False 时，
    一个**不在 off 名单里**的 intent（如 `plan_refine`）+ 带 tools + 小 max_tokens
    ⇒ **绝不能**发thinking 字段（否则就是"规则悄悄改了现有行为"）。
    """
    import json as _json
    import os as _os
    import subprocess as _sp
    import sys as _sys

    script = (
        "import json,httpx,core.config as C\n"
        "R=C.get\n"
        "def g(gr,k,d=None):\n"
        "    if gr=='llm' and k=='thinking_policy':\n"
        "        return json.dumps({'off':['routing'],'on':[]})\n"
        "    if gr=='llm' and k=='thinking_shape_enabled':\n"
        "        return False\n"
        "    return R(gr,k,d)\n"
        "C.get=g\n"
        "cap=[]\n"
        "def fp(u,json=None,**k):\n"
        "    cap.append(json or {})\n"
        "    raise RuntimeError('STOP')\n"
        "httpx.post=fp\n"
        "from llm.providers.openai_compat import OpenAICompatProvider as P\n"
        "cfg={'id':9003,'name':'p','model_name':'m','base_url':'https://x.invalid',\n"
        "     'api_key':'k','max_tokens':800,'context_window':128000,'temperature':0.3,\n"
        "     'model_params':'{}','status':'active'}\n"
        "try:\n"
        "    P(dict(cfg)).chat([{'role':'user','content':'x'}],tools=[{'a':1}],\n"
        "                       _intent='plan_refine')\n"
        "except Exception:\n"
        "    pass\n"
        "print('SHAPE_OFF', cap[-1].get('thinking','__ABSENT__') if cap else '__NO_CALL__')\n"
    )
    r = _sp.run([_sys.executable, "-X", "utf8", "-c", script],
                capture_output=True, text=True, timeout=120, cwd=_ROOT)
    line = [ln for ln in (r.stdout or "").splitlines() if ln.startswith("SHAPE_OFF")]
    if not line:
        return [(f"形态规则默认关闭（探针不可解析 rc={r.returncode}）", False)]
    got = line[-1].split(" ", 1)[1]
    return [
        ("★ 形态规则**关闭**时：未命中 off 的 intent 不受影响、不发 thinking"
         f"（实测 {got}）", got == "__ABSENT__"),
    ]


def main() -> int:
    print("=" * 74)
    print("判据 · thinking 按 intent 三态分流（L1）")
    print("=" * 74)
    ok = True
    for title, checks in [("① 配置与 schema", config_checks()),
                          ("② provider 实现", source_checks()),
                          ("②b 形态规则默认关闭（行为验）", shape_default_off_check()),
                          ("②c 防事故：非无条件发disabled（行为验）", disabled_guard_check()),
                          ("③ 三态下发（探针实测）", probe_checks())]:
        print(f"\n{title}")
        for name, passed in checks:
            print(f"[{'OK  ' if passed else 'FAIL'}] {name}")
            ok = ok and passed

    print("\n" + "=" * 74)
    print(f"判据结论：{'全部通过' if ok else '存在未通过项'}")
    print("=" * 74)
    if ok:
        print("\n[变异自证①] 去掉 disabled 下发（回到『关闭不发字段』），应判红")
        with open(PROVIDER, encoding="utf-8") as f:
            src = f.read()
        anchor = 'payload["thinking"] = {"type": "disabled"}'
        assert anchor in src, "锚点未找到"
        mutated = src.replace(anchor, 'pass  # 变异：不发 disabled', 1)
        with open(PROVIDER, "w", encoding="utf-8") as f:
            f.write(mutated)
        try:
            import glob
            import importlib
            for pyc in glob.glob(os.path.join(_ROOT, "llm", "providers",
                                               "__pycache__", "*.pyc")):
                try:
                    os.remove(pyc)
                except OSError:
                    pass
            importlib.invalidate_caches()
            red1 = not all(p for _, p in probe_checks())
            print(f"[变异自证①] {'已变红（真判据）' if red1 else '**仍是绿 ⇒ 空转断言**'}")
            ok = ok and red1
        finally:
            with open(PROVIDER, "w", encoding="utf-8") as f:
                f.write(src)
            import glob
            for pyc in glob.glob(os.path.join(_ROOT, "llm", "providers",
                                               "__pycache__", "*.pyc")):
                try:
                    os.remove(pyc)
                except OSError:
                    pass
        with open(PROVIDER, encoding="utf-8") as f:
            assert f.read() == src, "回滚失败！"
        print("[OK  ] 回滚校验：生产代码已逐字还原")

        print("\n[变异自证②] 把「受限下发」改成「总是发 disabled」，应判红")
        guard = 'elif (_off and _who in _off) or _by_shape is False or _tmode_explicit:'
        assert guard in src, "守卫锚点未找到"
        mutated2 = src.replace(guard, "elif True:", 1)
        with open(PROVIDER, "w", encoding="utf-8") as f:
            f.write(mutated2)
        try:
            import glob
            import importlib
            for pyc in glob.glob(os.path.join(_ROOT, "llm", "providers",
                                               "__pycache__", "*.pyc")):
                try:
                    os.remove(pyc)
                except OSError:
                    pass
            importlib.invalidate_caches()
            # ★ 用**行为判据**验，不用 probe_checks ——
            #   「总是发 disabled」这个变异，`probe_checks` 抓不到
            #   （它只验 off/on 命中与未命中三种情况，未命中在正常实现下本来就不发）。
            #   真正会因这个变异变红的是 `disabled_guard_check`
            #   （它专门用「名单未命中 + 规则未命中 + 大 max_tokens」去抓"总是发"）。
            red2 = not all(p for _, p in disabled_guard_check())
            print(f"[变异自证②] {'已变红（防事故判据有效）' if red2 else '**仍是绿 ⇒ 防事故判据无效**'}")
            ok = ok and red2
        finally:
            with open(PROVIDER, "w", encoding="utf-8") as f:
                f.write(src)
            import glob
            for pyc in glob.glob(os.path.join(_ROOT, "llm", "providers",
                                               "__pycache__", "*.pyc")):
                try:
                    os.remove(pyc)
                except OSError:
                    pass
        with open(PROVIDER, encoding="utf-8") as f:
            assert f.read() == src, "回滚失败！"
        print("[OK  ] 回滚校验：生产代码已逐字还原")
        print("=" * 74)
        print(f"最终结论：{'全部通过' if ok else '未通过'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())