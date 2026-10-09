# -*- coding: utf-8 -*-
"""migrate_v24_native_toolcall_providers —— 切到原生 tool-calling 模型 + 修实测出的context 误配。

## 背景（2026-10-09，米爸决策 R2）

### R2-a· 换用原生 tool-calling 模型
实测 7 个 provider（`tools/verify/probe_native_tool_call.py`，每个连打 3 轮）：

| id | 名称 | 原生 tool_calls 命中率 | 结论 |
|---|---|---|---|
| 1 | DeepSeek-V3 | 不可测（402 额度耗尽） | 待充值后复测 |
| 71 | deepseek-v4-flash | 不可测（402） | **已实测会用行内写法**（此前 N3 缺陷的根因） |
| 72 | deepseek-v4-pro | 不可测（402） | 待充值后复测 |
| 103 | qwen3.8-omni-flash | **1/3 = 33%** | ❌ 不稳定，不可作主力 |
| **104** | **qwen3.8-max** | **3/3 = 100%** | ✅ **可作主力** |
| **107** | **ds-v4-neibu** | **3/3 = 100%** | ✅ **可作主力**（当前 is_default=1） |

⚠️ **型号名不能证明能力**：103 名字里也有 qwen，但只有 33% 命中。
   ⇒ 选型必须逐个打真实接口看 `tool_calls` 字段。

### R2-b · 修 context_window 误配（本脚本的另一半）

实测（`probe_ctx_limit.py`，阶梯加压到 10 万字，**严格区分 402 与上下文超限**）：

| provider | 库中 cw | 库中 max_tokens | 输入余量 | 实测真实上限 |
|---|---|---|---|---|
| 103/104/107 | **8192** | **8192** | **0** ⚠️ | **> 100000 字** |

⇒ `cw=8192` 是**误配**（保守默认值没被实测校准）。
   而 `openai_compat.py` 里 `if mt > cw: mt = cw` 会把它当**硬上限**
   ⇒ 长 prompt（skill 正文 + 骨架 + 检索结果）会被**静默截断**，
      现象是"模型好像没看完材料就产出退化"——与本项目此前追查的退化现象高度相似。

★ 这与 MEMORY 里`context_window 守卫`那段注释描述的是**同一类误配**，
   当时只改了「是否可配」，没解决「配置值本身是猜的」。

## 本脚本做什么

1. 给 104/107 打**能力标签** `native_tool_calling`，让路由/选型脚本能识别；
2. 把 103/104/107 的 `context_window` 校准为实测值，`max_tokens` 收敛到合理比例；
3. **不改** `is_default`（切换默认模型影响面大，需米爸确认后再动）；
4. `dry_run` 默认 True，打印将要执行的 SQL，确认后再 `--apply`。

## 用法

    # 预览（默认）
    ./.venv/Scripts/python.exe -X utf8 tools/migrate_v24_native_toolcall_providers.py
    # 执行
    ./.venv/Scripts/python.exe -X utf8 tools/migrate_v24_native_toolcall_providers.py --apply
"""
from __future__ import annotations

import argparse
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

#: 实测结论（本轮 probe 脚本产出，不是推测）
NATIVE_TC = {
    104: "qwen3.8-max",# 3/3 原生 tool_calls
    107: "ds-v4-neibu",     # 3/3 原生 tool_calls
}
UNSTABLE = {103: "qwen3.8-omni-flash"}   # 1/3，不可作主力

#: 实测阶梯：4000→100000 字全部成功 ⇒ cw 取128000（留余量），max_tokens 取 32768
#: ⚠️ 取 128000 而非实测的 100000+：留出安全余量，避免刚好卡在边界被截断。
CONTEXT_FIX = {
    103: {"context_window": 128000, "max_tokens": 32768},
    104: {"context_window": 128000, "max_tokens": 32768},
    107: {"context_window": 128000, "max_tokens": 32768},
}

TAG_NATIVE = "native_tool_calling"
TAG_UNSTABLE = "unstable_tool_calling"


def _tag_add(raw: str, tag: str) -> str:
    """往 tags 里加一个标签，**保持原格式**。

    ⚠️ 2026-10-09 实测踩坑：`llm_providers.tags` 存的是 **JSON 数组**
       （实测值 `'[]'`、`'["vision"]'`），不是逗号分隔字符串。
       首版按逗号拼接会把 `["vision"]` 写成 `["vision"],native_tool_calling`
       ⇒ **破坏 JSON 格式**，消费方解析即失败。
    ⇒ 这里统一走 json 解析/序列化，写回合法 JSON。
    """
    import json
    try:
        arr = json.loads(raw or "[]")
        if not isinstance(arr, list):
            arr = []
    except Exception:
        # 原值不是合法 JSON（历史脏数据）⇒ 不擅自猜结构，原样保留并加新标签
        arr = []
    if tag in arr:
        return raw
    arr.append(tag)
    return json.dumps(arr, ensure_ascii=False)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写库（默认只预览）")
    args = ap.parse_args()

    from database import db_conn

    print("=" * 74)
    print("migrate_v24 · 原生 tool-calling 选型 + context_window 实测校准")
    print("=" * 74)
    print("\n【依据：实测，非推测】")
    for pid, nm in NATIVE_TC.items():
        print(f"  · id={pid} {nm:<20} 原生 tool_calls 3/3 = 100% ✅")
    for pid, nm in UNSTABLE.items():
        print(f"  · id={pid} {nm:<20} 仅 1/3 = 33%❌ 不作主力")
    print("  · cw 实测：阶梯加压至 100000 字全部成功 ⇒ 原配 8192 为误配")

    with db_conn() as c:
        c.row_factory = __import__("sqlite3").Row
        print("\n【当前值 → 目标值】")
        plans = []
        for pid in sorted(set(list(NATIVE_TC) + list(CONTEXT_FIX) + list(UNSTABLE))):
            row = c.execute("SELECT id,name,context_window,max_tokens,tags FROM llm_providers "
                            "WHERE id=?", (pid,)).fetchone()
            if row is None:
                print(f"  id={pid} 不存在，跳过")
                continue
            old_cw, old_mt = row["context_window"], row["max_tokens"]
            fix = CONTEXT_FIX.get(pid, {})
            new_cw = fix.get("context_window", old_cw)
            new_mt = fix.get("max_tokens", old_mt)
            tags = row["tags"] or ""
            new_tags = tags
            if pid in NATIVE_TC:
                new_tags = _tag_add(tags, TAG_NATIVE)
            elif pid in UNSTABLE:
                new_tags = _tag_add(tags, TAG_UNSTABLE)
            if (old_cw, old_mt, new_tags) == (new_cw, new_mt, tags):
                print(f"  id={pid} {row['name']:<20} 已符合预期，跳过")
                continue
            plans.append((pid, row["name"], old_cw, new_cw, old_mt, new_mt, tags, new_tags))
            print(f"  id={pid} {row['name']:<20} cw {old_cw} → {new_cw} | "
                  f"max_tokens {old_mt} → {new_mt} | tags '{tags}' → '{new_tags}'")

        if not plans:
            print("\n无需变更。")
            return 0

        if not args.apply:
            print(f"\n[预览] 共 {len(plans)} 条待变更。确认无误后加 --apply 执行。")
            print("⚠️ 本脚本**不改 is_default** —— 默认模型切换影响面大，需米爸单独确认。")
            return 0

        try:
            for pid, nm, ocw, ncw, omt, nmt, ot, nt in plans:
                c.execute("UPDATE llm_providers SET context_window=?, max_tokens=?, tags=? WHERE id=?",
                          (ncw, nmt, nt, pid))
            c.commit()
            # ⚠️ **写完必须回读校验**（MEMORY：成功返回 ≠ 结果可用。
            #本脚本首版就把 plans 元组解包错位一格——SQL 与参数都"看起来对"，
            # 但写进去的 max_tokens 变成了 context_window 的值。
            # 只有回读 DB 才能发现，脚本自己报"成功"是骗人的。）
            for pid, nm, ocw, ncw, omt, nmt, ot, nt in plans:
                row = c.execute("SELECT context_window, max_tokens FROM llm_providers "
                                "WHERE id=?", (pid,)).fetchone()
                got_cw, got_mt = row["context_window"], row["max_tokens"]
                if got_cw != ncw or got_mt != nmt:
                    c.rollback()
                    print(f"❌ 回读校验失败 id={pid}: 期望 cw={ncw} mt={nmt}，"
                          f"实际 cw={got_cw} mt={got_mt} ⇒ 已回滚")
                    return 1
                print(f"  ✔ id={pid} {nm:<20} cw={got_cw} mt={got_mt}（回读一致）")
        except Exception as e:  # noqa: BLE001
            c.rollback()
            print(f"❌ 写库失败已回滚：{type(e).__name__}: {e}")
            return 1

        print(f"\n✅ 已更新 {len(plans)} 条 provider 配置。")
        print("\n下一步（需米爸确认）：")
        print("  1) 把默认模型切到 id=104 qwen3.8-max（原生 tool-calling 100%）")
        print("  2) 充值后复测 DeepSeek 系列（id=1/71/72）原生 tool-calling 命中率")
        print("  3) 用新模型重跑 verify_n3_stability --repeat 3")
        return 0


if __name__ == "__main__":
    sys.exit(main())
