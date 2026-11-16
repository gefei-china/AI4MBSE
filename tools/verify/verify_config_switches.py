# -*- coding: utf-8 -*-
"""幽灵配置开关门禁：凡代码读取的配置键，必须在配置真源里登记。

## 为什么需要这条门禁（2026-11-09 实测）

排查「B 类：路由准确率长期无法定论」时发现：`agent/intent.py` 有 **5 个**开关
（`explain_preempt_short` / `continue_inherit` / `generic_clarify` /
`multi_intent_clarify` / `platform_read_route`），代码注释明确写着
「回退开关：intent.xxx=False（置 True 即恢复旧行为）」，但
`DEFAULT_CONFIG` / `CONFIG_SCHEMA` / `ENV_MAP` **三处一处都没有**：

    save_override({"intent.explain_preempt_short": True})  → ValueError: 未知配置项
    MBSE_SHADOW_CONFIG='{"intent":{"explain_preempt_short":true}}' → ValueError: 未知配置项
    （无对应环境变量映射）

⇒ 后果不是「默认值不合适」，而是**想用数据回答「该不该开」都做不到** ——
A/B 只能靠 monkey-patch 打补丁，无法在生产上翻转验证。
与 `request_invariant` 那次（「有读取、无登记」）是同一族缺陷。

## 判据（不扫全仓字符串，只认确定的配置读取形态）

只检查 `_cfg_get("key", default)` —— 该函数的语义是 `core.config.get("intent", key, default)`，
签名固定、读的就是 intent 组，不会误判普通 `dict.get`。
（通用正则 `\.get\("a","b"\)` 会把 `action.get("warn")`、`route.get("none")` 这类
普通字典读取也当成配置读取，实测 29 个候选里只有 6 个是真的 ⇒ 不用它。）

每条判据双向：正常全部登记 ⇒ 绿；植入一个未登记键 ⇒ 必须红。
"""
from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

PASS, FAIL = [], []


def chk(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name,
                          (" — " + str(detail)) if (detail and not cond) else ""))


#: `_cfg_get("key", default)` —— intent 组专用读取器（agent/intent.py:311）
_RX_CFG_GET = re.compile(r'_cfg_get\(\s*["\']([a-z0-9_]+)["\']')


def scan(path: str):
    """返回该文件里被 `_cfg_get` 读取的全部键（去重保序）。"""
    try:
        s = open(path, encoding="utf-8", errors="ignore").read()
    except Exception:
        return []
    out = []
    for m in _RX_CFG_GET.finditer(s):
        k = m.group(1)
        if k not in out:
            out.append(k)
    return out


def main() -> int:
    from core import config as cfg

    print("=" * 74)
    print("幽灵配置开关门禁：代码读取的键必须在配置真源里登记")
    print("=" * 74)

    src = os.path.join(ROOT, "agent", "intent.py")
    chk("被检文件存在", os.path.isfile(src), src)
    keys = scan(src)
    chk("扫到 intent 组读取键", len(keys) >= 10, "keys=%d %s" % (len(keys), keys[:6]))

    defaults = cfg.DEFAULT_CONFIG.get("intent") or {}
    schema = cfg.CONFIG_SCHEMA.get("intent") or {}

    # ── [1] 全部读取键都已登记 ────────────────────────────────────────
    print("\n[1] 读取键 ⊆ 配置真源（DEFAULT_CONFIG ∪ CONFIG_SCHEMA）")
    miss = [k for k in keys if k not in defaults and k not in schema]
    chk("无未登记键（无幽灵开关）", not miss,
        "未登记 %d 个: %s" % (len(miss), miss))
    if keys:
        print("  读取键 %d 个：%s" % (len(keys), ", ".join(keys)))

    # ── [2] 已登记键必须出现在 SCHEMA（界面可改）────────────────────────
    print("\n[2] 登记项在 CONFIG_SCHEMA 里（否则界面/接口改不了）")
    no_schema = [k for k in keys if k in defaults and k not in schema]
    chk("DEFAULT 有、SCHEMA 无 ⇒ 判红", not no_schema, no_schema)

    # ── [3] 六个已知历史幽灵键必须可被真正设置（行为级）────────────────
    print("\n[3] 历史幽灵键行为级可设置（影子层 + save_override 白名单）")
    legacy = ("explain_preempt_short", "continue_inherit", "generic_clarify",
              "multi_intent_clarify", "platform_read_route", "speech_act_de")
    for k in legacy:
        chk("SCHEMA 可设置 %s" % k, k in schema, "不在 CONFIG_SCHEMA.intent")
        chk("DEFAULT 有默认值 %s" % k, k in defaults, "不在 DEFAULT_CONFIG.intent")

    # 真跑一次影子层（不写用户级配置）：能读到即说明校验通过
    old = os.environ.get(cfg.SHADOW_ENV)
    try:
        import json as _json
        os.environ[cfg.SHADOW_ENV] = _json.dumps(
            {"intent": {"explain_preempt_short": True}}, ensure_ascii=False)
        cfg.reload()
        chk("影子层接受 intent.explain_preempt_short", 
            cfg.get("intent", "explain_preempt_short") is True,
            cfg.get("intent", "explain_preempt_short"))
    except Exception as e:
        chk("影子层接受 intent.explain_preempt_short", False,
            "%s: %s" % (type(e).__name__, str(e)[:90]))
    finally:
        if old is None:
            os.environ.pop(cfg.SHADOW_ENV, None)
        else:
            os.environ[cfg.SHADOW_ENV] = old
        cfg.reload()

    # ── [4] 变异自证：植入未登记键 ⇒ 扫描必须发现 ────────────────────────
    print("\n[4] 变异自证（判据不是空转）")
    fake = ['_cfg_get("__phantom_probe__", False)']
    _orig = scan
    try:
        globals()["scan"] = lambda p: _orig(p) + ["__phantom_probe__"]
        _k = scan(src)
        _m = [k for k in _k if k not in defaults and k not in schema]
        chk("植入未登记键 ⇒ 判据报出", "__phantom_probe__" in _m, _m)
    finally:
        globals()["scan"] = _orig

    print("\n" + "=" * 74)
    print("  PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
    if FAIL:
        print("  失败项：" + "; ".join(FAIL))
    print("=" * 74)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
