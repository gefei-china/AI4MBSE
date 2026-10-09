"""migrate_v15_retire_old_view_routes — 旧视图 Agent **禁路由但保留数据** + 复合信号去「校验」。

## 背景（2026-10-08 实测，米爸拍板的两项）

上一版V14 把路由词按"用户语序"重写后，**实测 9 条真实说法只命中 5条**，且
`verify_intent_routing_baseline.py` 的基线从 1.000/0.955掉到 0.931/0.909
⇒ 已在 `migrate_v14b_rollback_route_words.py` 把**路由词整体回滚**，
skill 绑定保留（那部分是有效的：8 个节点都能读到绑定正文）。

回滚后剩 2/9，逐条归因（不是猜）：

| 用户说法 | 实际路由 | 归因（实测打分） |
|---|---|---|
| 生成活动图 | 活动图生成（旧 Agent，1.75 分） | **旧 Agent 抢路由**：它有「生成活动图」「活动图」两个词条，压过 view_expansion 的 1.0 |
| 生成状态机视图 | 状态机视图生成（旧 Agent） | 同上 |
| 删除影响分析 | impact（1.5 分） | 旧 Agent `impact` 的「影响分析」压过 change_safety_gate 的 1.0 |
| 帮我校验修复模型 | review | `_is_composite_task=True`（含「校验」）⇒ **db 层整块被跳过**，落到 review 规则 |
| 生成追溯矩阵 | review | 同上（含「校验」? 无—— 是 review 的「校验/评审」强信号前置，实测 route=rule） |
| 解析建模方法论 | knowledge_qa | `_is_explain_ask=True`（说明类优先，**设计如此**，不动） |
| 把这段需求条目化 | knowledge_qa | 回滚后原值「需求条目化条目」语序不匹配（**不修：改词即拉低基线**） |

⇒ 两项处置（米爸 2026-10-08 拍板）：
  · **旧视图 Agent 禁路由、保留数据** —— 清空 8 个旧视图 Agent 的
    `intent_keywords`（Agent 行、绑定工具、system_prompt 一律保留，
    `status` 不改 ⇒ 可随时恢复路由）。
  · **复合任务信号移除「校验」** —— `_COMPOSITE_SIGNALS` 去掉"校验"。

## 为什么不整体删掉旧 Agent

米爸上一轮的既定口径是"N3 未达标前不迁旧 Agent"。本轮 N3 全量仍不稳
（3/8，且检出产出雷同）⇒ 旧 Agent 仍要保留兜底。
**清空路由词 ≠ 停用 Agent**：它仍可被显式 `forced_intent` 调用（前端 @Agent），
只是不再"抢"自然语言路由。

##风险与验证

清空 8 个 Agent 的关键词会**让那8 条路由从有信号变成无信号**，
下沉到语义/LLM 层 ⇒ 可能改变既有行为。脚本因此：
  ① 改前跑一次基线，改后跑一次，**对比真实样本池 accuracy**（不允许掉）；
  ② 跑流水线 9 条说法的路由前后对比，**逐条打印**（可复核）。

⚠️ `verify_intent_routing_baseline.py` 自身不稳定（实测 3 次重跑 macro_f1
在 0.78~0.87 间波动，accuracy 在 0.931/0.9655 间交替——错例是design↔review
的 LLM 语义摇摆）⇒ **只把"真实样本池 accuracy"当判据**（它 3 次都稳定 0.9091），
macro_f1 仅打印不判定。

##幂等 / 回滚

- 清空是幂等的（重复跑结果一致）；
- 回滚：`mbse.db.bak-v14-*` 里有全部 8 个旧 Agent 的原 `intent_keywords`，
  用 `migrate_v14b_rollback_route_words.py` 同法恢复（该脚本读备份库）。
- 代码改动（`_COMPOSITE_SIGNALS`）回滚 = 还原 `agent/intent.py` 一行。
"""
from __future__ import annotations

import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

DB = os.path.join(ROOT, "mbse.db")

#: 8 个旧视图 Agent —— 视图能力已由 N3 `view_expansion` 承接
OLD_VIEW_AGENTS = [
    "需求视图生成", "结构视图生成", "交互视图（IBD）生成", "用例视图生成",
    "活动图生成", "状态机视图生成", "参数视图生成", "顺序视图（时序图）生成",
]

INTENT_PY = os.path.join(ROOT, "agent", "intent.py")

#: 流水线 9 条说法（改前改后都要跑，逐条可复核）
PROBES = [
    ("解析建模方法论", "methodology_resolver"),
    ("把这段需求条目化", "requirement_structuring"),
    ("生成架构骨架", "architecture_skeleton"),
    ("生成活动图", "view_expansion"),
    ("生成状态机视图", "view_expansion"),
    ("帮我校验修复模型", "model_validation_repair"),
    ("生成追溯矩阵", "trace_verification"),
    ("删除影响分析", "change_safety_gate"),
    ("模型发布入库", "model_release"),
]


def probe_routes(tag: str):
    from agent.pipeline import AgentPipeline
    from database import db_conn
    pipe = AgentPipeline()
    pipe._load_db_agents()
    out = {}
    with db_conn() as conn:
        for q, exp in PROBES:
            got = pipe.router.detect(q, conn=conn)
            if isinstance(got, tuple):
                got = got[0]
            out[q] = (got, exp)
    print(f"\n  ── 路由实测（{tag}）──")
    ok = 0
    for q, (got, exp) in out.items():
        good = got == exp
        ok += good
        print(f"    [{'OK  ' if good else 'FAIL'}] {q:16} → {got:26} (期望 {exp})")
    print(f"    命中 {ok}/{len(out)}")
    return ok, out


def baseline_sample_acc():
    """只取真实样本池 accuracy（3 次重跑稳定的那一项）。"""
    import subprocess
    p = subprocess.run(
        [sys.executable, "-X", "utf8",
         os.path.join(ROOT, "tools", "verify", "verify_intent_routing_baseline.py")],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=900, cwd=ROOT)
    accs = []
    for line in (p.stdout or "").splitlines():
        if "accuracy=" in line:
            try:
                accs.append(float(line.split("accuracy=")[1].split()[0]))
            except Exception:
                pass
    return (accs[1] if len(accs) >= 2 else None), accs


def patch_composite_signals() -> bool:
    """从 `_COMPOSITE_SIGNALS` 移除「校验」。返回是否发生了改动。"""
    src = open(INTENT_PY, encoding="utf-8").read()
    m = re.search(r'_COMPOSITE_SIGNALS = \(([^)]*)\)', src)
    if not m:
        print("[FAIL] 未找到 _COMPOSITE_SIGNALS 定义")
        return False
    items = [x.strip().strip('"\'') for x in m.group(1).split(",") if x.strip()]
    if "校验" not in items:
        print("  _COMPOSITE_SIGNALS 已不含「校验」，无需改动")
        return False
    new_items = [x for x in items if x != "校验"]
    new_frag = "_COMPOSITE_SIGNALS = (" + ", ".join(f'"{x}"' for x in new_items) + ")"
    src2 = src[:m.start()] + new_frag + src[m.end():]
    open(INTENT_PY, "w", encoding="utf-8").write(src2)
    print(f"  已改：{m.group(0)}")
    print(f"    → {new_frag}")
    print(f"    （原含「校验」{len(items)} 项 → 现 {len(new_items)} 项；回滚=还原这一行）")
    return True


def main() -> int:
    from database import db_conn

    print("=" * 72)
    print("① 改前基线 + 路由实测")
    print("=" * 72)
    acc_before, all_before = baseline_sample_acc()
    print(f"  真实样本池 accuracy = {acc_before}（判据用；macro_f1 不判：实测 3 次波动 0.78~0.87）")
    ok_before, _ = probe_routes("改前")

    print("\n" + "=" * 72)
    print("② 清空 8 个旧视图 Agent 的 intent_keywords（禁路由、保留数据）")
    print("=" * 72)
    with db_conn() as conn:
        for n in OLD_VIEW_AGENTS:
            row = conn.execute(
                "SELECT id, intent_keywords, status FROM agents WHERE name=?", (n,)).fetchone()
            if not row:
                print(f"  [SKIP] {n} 不存在")
                continue
            print(f"  {n:22} status={row['status']:8} {row['intent_keywords']}  → []")
            # ★ 只清 intent_keywords；id / status / system_prompt / agent_tools 一律不动
            conn.execute("UPDATE agents SET intent_keywords='[]' WHERE id=?", (row["id"],))
        conn.commit()
        # 确认数据未被动
        print("\n  ── 数据保留校验 ──")
        for n in OLD_VIEW_AGENTS:
            r = conn.execute(
                "SELECT status, system_prompt, (SELECT count(*) FROM agent_tools "
                "WHERE agent_id=agents.id) tools FROM agents WHERE name=?", (n,)).fetchone()
            print(f"    {n:22} status={r['status']:8} prompt={len(r['system_prompt'] or ''):5} "
                  f"绑定工具={r['tools']}")

    print("\n" + "=" * 72)
    print("③ 复合任务信号移除「校验」")
    print("=" * 72)
    patch_composite_signals()

    print("\n" + "=" * 72)
    print("④ 改后路由 + 基线对比")
    print("=" * 72)
    ok_after, _ = probe_routes("改后")
    acc_after, _ = baseline_sample_acc()
    print(f"\n  真实样本池 accuracy：{acc_before} → {acc_after}")
    print(f"  流水线 9 条命中：{ok_before}/9 → {ok_after}/9")

    print("\n" + "=" * 72)
    print("结论")
    print("=" * 72)
    bad = []
    if acc_before is not None and acc_after is not None and acc_after < acc_before - 0.001:
        bad.append(f"真实样本池 accuracy 掉了：{acc_before} → {acc_after}")
    if ok_after <= ok_before:
        bad.append(f"路由命中未改善：{ok_before}/9 → {ok_after}/9")
    if bad:
        for b in bad:
            print("  [FAIL]", b)
        print("  ⇒ 建议回滚（备份库里 8 个旧 Agent 的原关键词可恢复；"
              "代码回滚=还原 intent.py 的 _COMPOSITE_SIGNALS 一行）")
        return 1
    print(f"  ✅ 基线未下降，路由命中 {ok_before}/9 → {ok_after}/9")
    print("  ⚠️ 旧视图 Agent 仍在库、status 仍 active，只是**不再抢自然语言路由**；")
    print("     需要时仍可被显式 forced_intent / 前端 @Agent 调用（可随时恢复路由）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())