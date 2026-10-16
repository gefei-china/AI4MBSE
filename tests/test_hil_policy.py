"""人工介入策略裁决单测（P0-8，2026-10-16）。

运行：python tests/test_hil_policy.py
（沿用 tests/ 既有约定：check() 计数 + main() 返回码，不用 pytest）

## 验收口径

本测试守护的不是"功能能用"，而是**四条不可退让的不变式**：

1. **fail-closed**：策略不可读（空表 / None / DB 异常）⇒ 按 confirm 裁决，
   绝不静默放行。
2. **只增不减**：escalate() 不能把更严的门槛降下来。
3. **同 priority 取更严者**：两条同优先级策略并存时不得"挑宽松的那条用"。
4. **同源**：services/hil_policy.DECISION_POINTS 与 database/seeds._HIL_DECISION_POINTS
   必须完全一致 —— 两处漂移的症状是"种子写不进但不报错"，属静默失败。

## 一条刻意的**不**测

⚠️ 不测"evaluate 会不会被接进 tools.py" —— P0 明确不接链路。
   接线是后续阶段的事，且会改同步/异步语义。在这里写"已接入"的断言会假绿。
"""
import sys
import os
import pathlib

# 本文件是**脚本风格**（PASS/FAIL 计数器 + `if __name__`），与 test_glossary_routing.py 同类。
# 而 pytest 的 conftest.py 只对"被 pytest 收集的模块"注入 ROOT 到 sys.path；
# 脚本风格被 collect_ignore 排除 ⇒ 直接 `python tests/test_hil_policy.py` 跑时
# ROOT 不在 sys.path ⇒ `import services` 会 ModuleNotFoundError。
# ⇒ 脚本自己补 ROOT（与 conftest.py 的做法一致，不引入新机制）。
_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
os.chdir(_ROOT)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def main():
    from services.hil_policy import (
        LEVELS, LEVEL_RANK, DECISION_POINTS, load_policies, evaluate,
        escalate, default_decision,
    )

    # ── 不变式 4：与 seeds 的决策点枚举同源 ──
    print("\n=== 同源性（决策点枚举）===")
    from database.seeds import _HIL_DECISION_POINTS, _HIL_LEVEL_RANK
    check("DECISION_POINTS 与 seeds 一致",
          tuple(DECISION_POINTS) == tuple(_HIL_DECISION_POINTS),
          f"policy={DECISION_POINTS} seeds={_HIL_DECISION_POINTS}")
    check("LEVEL_RANK 与 seeds 一致",
          dict(LEVEL_RANK) == dict(_HIL_LEVEL_RANK),
          f"{LEVEL_RANK} vs {_HIL_LEVEL_RANK}")
    check("四级严格程度递增",
          [LEVEL_RANK[x] for x in LEVELS] == sorted(LEVEL_RANK[x] for x in LEVELS),
          str(LEVEL_RANK))

    # ── 不变式 1：fail-closed ──
    print("\n=== 不变式1：fail-closed ===")
    d = evaluate(None, "tool_write", "write")
    check("策略为 None ⇒ confirm", d["level"] == "confirm", d["level"])
    check("兜底裁决带可审计原因", "fail-closed" in d["reason"], d["reason"])
    check("兜底裁决 matched_by=fallback", d["matched_by"] == "fallback", d["matched_by"])
    d2 = evaluate([], "retrieval_empty", "retrieval_empty")
    check("空策略表 ⇒ confirm", d2["level"] == "confirm", d2["level"])
    check("default_decision 也是 confirm",
          default_decision()["level"] == "confirm")

    # 有策略表但未命中 ⇒ auto（此处 fail-open 是**有意**的，见模块 docstring）
    d3 = evaluate([{"id": 1, "match_key": "read", "level": "auto",
                    "priority": 90, "conditions": {}}],
                  "tool_write", "file_delete")
    check("有表未命中 ⇒ auto（no_policy 可区分）",
          d3["level"] == "auto" and d3["matched_by"] == "no_policy",
          f"{d3['level']}/{d3['matched_by']}")

    # ── 匹配与排序 ──
    print("\n=== 匹配与排序 ===")
    pols = [
        {"id": 10, "match_key": "any", "level": "auto", "priority": 10, "conditions": {}},
        {"id": 11, "match_key": "file_delete", "level": "confirm", "priority": 20,
         "conditions": {}},
        {"id": 12, "match_key": "file_delete", "level": "notify", "priority": 20,
         "conditions": {}},   # 与 11 同 priority、较宽松 —— 必须被忽略
    ]
    r = evaluate(pols, "tool_write", "file_delete")
    check("同 priority 取更严者（confirm 而非 notify）",
          r["level"] == "confirm", r["level"])

    r2 = evaluate(pols, "tool_write", "file_delete")
    check("命中非 any 的精确策略优先于 any",
          r2["policy_id"] == 11, str(r2["policy_id"]))

    # priority 只在**同特异性层内**比较，不能跨层压过特异性
    # （这是 2026-10-16 修正的核心：初版把 any 底策略与精确策略放同一池只比 priority，
    #   导致 any(prio=5,auto) 压过 file_delete(prio=20,confirm) ⇒ destructive 被放行）
    pols2 = [
        {"id": 20, "match_key": "any", "level": "confirm", "priority": 5, "conditions": {}},
        {"id": 21, "match_key": "x", "level": "auto", "priority": 90, "conditions": {}},
    ]
    r3 = evaluate(pols2, "tool_write", "x")
    check("精确命中优先于 any（哪怕 any 的 priority 更小）",
          r3["level"] == "auto" and r3["policy_id"] == 21,
          f"{r3['level']}/{r3['policy_id']}")

    # 同层内 priority 仍然有效（都是精确匹配）
    same_layer = [
        {"id": 22, "match_key": "y", "level": "auto", "priority": 5, "conditions": {}},
        {"id": 23, "match_key": "y", "level": "confirm", "priority": 90, "conditions": {}},
    ]
    r3b = evaluate(same_layer, "tool_write", "y")
    check("同层（均精确）内 priority 小者胜出",
          r3b["level"] == "auto" and r3b["policy_id"] == 22,
          f"{r3b['level']}/{r3b['policy_id']}")

    # 无精确命中时才回落到 any 层
    r3c = evaluate(pols2, "tool_write", "不存在的工具")
    check("无精确命中才回落到 any 层",
          r3c["level"] == "confirm" and r3c["policy_id"] == 20,
          f"{r3c['level']}/{r3c['policy_id']}")

    # ── 条件判据 ──
    print("\n=== 条件判据 ===")
    cond_pol = [{"id": 30, "match_key": "kb_search", "level": "approve",
                 "priority": 50, "conditions": {"confidence": {"op": "lt", "value": 0.6}}}]
    check("置信度 0.31 低于 0.6 ⇒ 命中 approve",
          evaluate(cond_pol, "retrieval_empty", "kb_search",
                   {"confidence": 0.31})["level"] == "approve")
    check("置信度 0.85 高于 0.6 ⇒ 不命中（走 no_policy→auto）",
          evaluate(cond_pol, "retrieval_empty", "kb_search",
                   {"confidence": 0.85})["matched_by"] == "no_policy")
    check("缺字段 ⇒ 不命中（不当 0 处理）",
          evaluate(cond_pol, "retrieval_empty", "kb_search", {})["matched_by"] == "no_policy")

    # 简写形式 + 多条件 AND
    multi = [{"id": 31, "match_key": "kb", "level": "approve", "priority": 50,
              "conditions": {"side_effect": "write", "count": {"gte": 5}}}]
    check("简写形式 eq 生效",
          evaluate(multi, "tool_write", "kb",
                   {"side_effect": "write", "count": 9})["level"] == "approve")
    check("多条件 AND：不满足其一即不命中",
          evaluate(multi, "tool_write", "kb",
                   {"side_effect": "read", "count": 9})["matched_by"] == "no_policy")
    check("未知操作符 ⇒ 不命中（不猜）",
          evaluate([{"id": 32, "match_key": "kb", "level": "approve", "priority": 1,
                     "conditions": {"x": {"op": "regex", "value": ".*"}}}],
                   "tool_write", "kb", {"x": "abc"})["matched_by"] == "no_policy")

    # ── 不变式 2：只增不减 ──
    print("\n=== 不变式2：只增不减 ===")
    check("confirm 不会被降级为 auto",
          escalate("confirm", "auto") == "confirm")
    check("auto 会被升到 confirm",
          escalate("auto", "confirm") == "confirm")
    check("notify→approve 可以收紧",
          escalate("notify", "approve") == "approve")
    check("approve→notify 不会放宽",
          escalate("approve", "notify") == "approve")
    check("显式 allow_relax 才可放宽",
          escalate("confirm", "auto", allow_relax=True) == "auto")
    check("非法 level 按 confirm 处理（fail-closed）",
          escalate("bogus", "auto") == "confirm")

    # ── 真实表读取（若已迁移）──
    print("\n=== 真实库：策略表读取 ===")
    try:
        from database import get_db
        conn = get_db()
        n = conn.execute("SELECT COUNT(*) FROM hil_policies").fetchone()[0]
        check("hil_policies 表存在且有种子数据", n > 0, f"rows={n}")
        live = load_policies(conn)
        check("load_policies 返回策略", len(live) > 0, f"len={len(live)}")
        check("conditions 已解析为 dict",
              all(isinstance(p.get("conditions"), dict) for p in live))

        # 种子策略须体现现状语义（零行为变化）
        destructive = [p for p in live if p["match_key"] == "destructive"]
        check("种子：destructive ⇒ confirm",
              destructive and destructive[0]["level"] == "confirm",
              str(destructive[:1]))
        empty_pol = [p for p in live if p["match_key"] == "retrieval_empty"]
        check("种子：retrieval_empty ⇒ auto（域外闲聊不打扰）",
              empty_pol and empty_pol[0]["level"] == "auto",
              str(empty_pol[:1]))

        # 裁决器能吃真实表
        rr = evaluate(live, "retrieval_domain_conflict", "retrieval_domain_conflict")
        check("真实表裁决：域冲突 ⇒ notify（种子语义）",
              rr["level"] == "notify", rr["level"])

        # scope 过滤
        scoped = load_policies(conn, scope="不存在的Agent")
        check("scope 过滤后仍含 global 策略", len(scoped) > 0, f"len={len(scoped)}")

        # 缺表时 load 不抛（交给 evaluate fail-closed）
        check("load_policies(None) ⇒ []", load_policies(None) == [])
        conn.close()
    except Exception as e:
        # 未迁移的库不应让本测试红掉——但必须打印，让人知道没测到
        print(f"  SKIP  真实库校验跳过：{type(e).__name__}: {str(e)[:120]}")

    print(f"\n===== P0-8 策略裁决结果: PASS {PASS} / FAIL {FAIL} =====")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
