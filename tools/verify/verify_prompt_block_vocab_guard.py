# -*- coding: utf-8 -*-
"""verify_prompt_block_vocab_guard.py —— prompt 块的 block_name 必须有**运行期**词表守卫。

## 要根治的问题（2026-11-16）

`context_audit.block_name` 的消费方是 `core.event_vocabulary.validate_event` ——
读路径遇到词表外类型且无 `ignorable` 会**抛 `UnknownEventError` ⇒ 会话回放失败**。
而写路径此前**从不校验**，于是"漏登记"只在很久之后以"回放失败"暴露。

**同一形态实测发生三次**，全是"新增 prompt 块时忘了登记词表"：

| block_name | 何时进 prompt | 何时被发现 | 发现者 |
|---|---|---|---|
| `artifact_facts` | 2026-11-14 | 2026-11-15 | 门禁 |
| `output_contract` | 2026-11-14 | 2026-11-15 | 门禁 |
| `artifact_baseline` | 2026-11-13 | 2026-11-16 | 门禁 |

⇒ 根因不是"记性不好"，而是**「加块」与「登记」是两个动作、无强耦合**。
门禁能抓，但**抓在事后**。修法：在 `context_audit.record_blocks`（context_audit 的
唯一写入点）加运行期校验 —— **只上报不阻断**，漏登记那一刻就进观测台账。

## 判据

A 组（守卫存在且接线）：
  A1 `_audit_block_vocab` 存在
  A2 `record_blocks` 真的调用了它（不是写了没接线）
  A3 调用在 `_enabled()` 之后（审计关掉时不产生观测噪音）

B 组（行为）：
  B1 已登记块名 ⇒ 不误报
  B2 未登记块名 ⇒ 被抓到，且进 `observe_fail` 台账
  B3 同一名字只报一次（热路径去重）
  B4 绝不阻断：None / 空 dict / 字符串 / 数字等畸形输入都不抛
  B5 字符串输入**不得逐字符上报**（实测踩过：`for n in "notadict"` 产出 'a'/'c'…）
  B6 词表模块导入失败 ⇒ 静默返回空（不阻断主流程）
  B7 变异自证：把词表当成空集 ⇒ B1 必须翻红（证明判据非空转）

C 组（一致性）：生产库实际出现过的 block_name 全部在词表内
     —— 与 `verify_universal_agent_core` 的覆盖判据同源，这里做**独立复核**
        （两边都过才算过；只过一边说明其中一边的判据有问题）

用法： ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_prompt_block_vocab_guard.py
退出码：0 全绿（C 组无库时 SKIP）／1 有失败
"""
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
DB = os.environ.get("MBSE_GATE_DB") or os.environ.get("MBSE_DB_PATH") or os.path.join(ROOT, "mbse.db")

_PASS, _FAIL, _SKIP = [], [], []


def chk(name, ok, detail=""):
    (_PASS if ok else _FAIL).append(name)
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name,
                           ("　— " + str(detail)) if detail and not ok else ""))


def skip(name, why):
    _SKIP.append(name)
    print("  [SKIP] %s :: %s" % (name, why))


def main():
    print("=" * 78)
    print("门禁：prompt 块 block_name 的运行期词表守卫")
    print("=" * 78)

    import agent.context_audit as CA
    from core.event_vocabulary import KNOWN_EVENT_TYPES

    print("\n--- GROUP_A ---")
    chk("A1 `_audit_block_vocab` 存在", hasattr(CA, "_audit_block_vocab"), "")
    src = open(os.path.join(ROOT, "agent", "context_audit.py"), encoding="utf-8").read()
    # A2：必须真的在 record_blocks 里被调用（写了函数但没接线 = 空转）
    i_rec = src.find("def record_blocks")
    i_call = src.find("_audit_block_vocab(blocks", i_rec) if i_rec >= 0 else -1
    chk("A2 record_blocks 真的调用了守卫（不是写了没接线）",
        i_rec >= 0 and i_call > i_rec, "def@%s call@%s" % (i_rec, i_call))
    # A3：调用必须在 _enabled() 之后（否则审计关闭时仍会产生观测噪音）
    i_enabled = src.find("if not _enabled():", i_rec) if i_rec >= 0 else -1
    chk("A3 守卫调用在 _enabled() 之后（审计关闭时不产生噪音）",
        i_enabled >= 0 and i_call > i_enabled, "enabled@%s call@%s" % (i_enabled, i_call))

    print("\n--- GROUP_B ---")
    CA._VOCAB_UNKNOWN_SEEN.clear()
    r = CA._audit_block_vocab({"memory": "x", "retrieval": "y", "role": "z",
                               "artifact_baseline": "w"})
    chk("B1 已登记块名 ⇒ 不误报", r == [], r)

    CA._VOCAB_UNKNOWN_SEEN.clear()
    probe = "_gate_probe_unknown_block"
    r = CA._audit_block_vocab({"memory": "x", probe: "y"})
    chk("B2 未登记块名 ⇒ 被抓到", r == [probe], r)
    try:
        from core.observe_fail import snapshot
        snap = snapshot() or {}
        chk("B2b 已上报到 observe_fail 台账", "prompt_block_vocab" in snap,
            list(snap)[:6])
    except Exception as e:
        chk("B2b 已上报到 observe_fail 台账", False, repr(e))

    r2 = CA._audit_block_vocab({"memory": "x", probe: "y"})
    chk("B3 同一名字只报一次（热路径去重）", r2 == [], r2)

    ok_shape = True
    detail = ""
    for bad in (None, {}, "", "notadict", 123, [], {"": ""}, {"a": None}):
        try:
            CA._audit_block_vocab(bad)
        except Exception as e:
            ok_shape, detail = False, "%r → %r" % (bad, e)
            break
    chk("B4 畸形输入均不抛异常（绝不阻断主流程）", ok_shape, detail)

    CA._VOCAB_UNKNOWN_SEEN.clear()
    r_str = CA._audit_block_vocab("notadict")
    chk("B5 字符串输入不逐字符上报（实测踩过：for n in 'notadict' 产出 'a'/'c'…）",
        r_str == [] and not CA._VOCAB_UNKNOWN_SEEN,
        "fresh=%s seen=%s" % (r_str, sorted(CA._VOCAB_UNKNOWN_SEEN)[:8]))

    # B6 导入失败兜底
    _ok6 = True
    try:
        import builtins
        _orig = builtins.__import__

        def _boom(name, *a, **kw):
            if name == "core.event_vocabulary":
                raise ImportError("simulated")
            return _orig(name, *a, **kw)

        builtins.__import__ = _boom
        try:
            _ok6 = CA._audit_block_vocab({"whatever_block": "x"}) == []
        finally:
            builtins.__import__ = _orig
    except Exception:
        _ok6 = False
    chk("B6 词表导入失败 ⇒ 静默返回空（不阻断）", _ok6, "")

    # B7 变异自证：把词表当空集 ⇒ 正常块名也会被报 ⇒ B1 判据必翻红
    CA._VOCAB_UNKNOWN_SEEN.clear()
    _orig_vocab = KNOWN_EVENT_TYPES
    try:
        import core.event_vocabulary as EV
        EV.KNOWN_EVENT_TYPES = frozenset()
        mut = CA._audit_block_vocab({"memory": "x"})
        chk("B7 变异自证：词表置空 ⇒ 正常块名也被报 ⇒ B1 必翻红",
            mut == ["memory"], mut)
    finally:
        import core.event_vocabulary as EV
        EV.KNOWN_EVENT_TYPES = _orig_vocab
        CA._VOCAB_UNKNOWN_SEEN.clear()

    print("\n--- GROUP_C ---")
    if not os.path.exists(DB):
        skip("C1 生产库 block_name 全部已登记", "干净库")
    else:
        try:
            conn = sqlite3.connect(DB)
            seen = {r[0] for r in conn.execute(
                "SELECT DISTINCT block_name FROM context_audit "
                "WHERE block_name IS NOT NULL")}
            conn.close()
            unknown = sorted(seen - set(KNOWN_EVENT_TYPES))
            chk("C1 生产库 block_name 全部已登记（独立复核）",
                not unknown, "未登记: %s" % unknown)
            print("      生产库 block_name 共 %d 种，词表 %d 项"
                  % (len(seen), len(KNOWN_EVENT_TYPES)))
        except Exception as e:
            skip("C1 生产库 block_name 全部已登记", "查询失败: %r" % (e,))

    print("\n" + "=" * 78)
    print("结果：PASS %d / FAIL %d / SKIP %d" % (len(_PASS), len(_FAIL), len(_SKIP)))
    for n in _FAIL:
        print("  - " + n)
    if _FAIL:
        print("\n修复：把缺的 block_name 加进 core/event_vocabulary.py 的 KNOWN_EVENT_TYPES")
    print("=" * 78)
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
