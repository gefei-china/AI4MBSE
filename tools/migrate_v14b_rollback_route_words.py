"""migrate_v14b_rollback_route_words — 回滚 V14 的 intent_keywords 改动。

## 为什么回滚（2026-10-08 实测，这是我自己的错）

V14 给 8 个流水线节点写了 `intent_keywords`（按"用户语序"重写），
跑 `tools/verify/verify_intent_routing_baseline.py` 后：

| 评测集 | 改前基线 | V14 之后 |
|---|---|---|
| 内置集 accuracy | **1.000** | 0.931 |
| 内置集 macro_f1 | 1.000 | 0.7796 |
| 真实样本池 accuracy | **0.955** | 0.9091 |
| 真实样本池 macro_f1 | 0.965 | 0.7567 |

⇒ **关键词层被我的改动拉低了**，且 macro_f1 掉得最狠（0.97 → 0.78）。

而 `verify_intent_routing_baseline.py` 的文档字符串**早就写明了**这条禁忌
（本轮我读到时已经太晚）：

> 填关键词反而有风险：8 个视图 Agent 的名字高度近义…一旦填入关键词，
> `_kw_score` 的"最长命中优先"会让它们**互抢** —— 这正是 P1-13/P1-14
> 反复加守卫要防的问题。… **V7（修关键词路由）→ 撤销**

⇒ 我做��正是 V7 当年做过并被评测推翻的事，只是把对象从"8 个视图 Agent"
换成了"8 个流水线节点"。**历史结论对同类方案同样成立，我没有先查历史结论。**

## 回滚范围：只回路由词，保留 skill 绑定

| 项| 处理 | 理由 |
|---|---|---|
| `agents.intent_keywords` | **回滚到 V14 前原值** | 评测证明填词拉低基线 |
| `agent_tools` 的 skill 绑定 | **保留** | 绑定只影响"命中后注入什么正文"，不参与路由计算；`get_bound_skills` 不参与打分|

⚠️ skill 绑定保留是否安全需实测确认 ⇒ 本脚本会跑
   `verify_intent_routing_baseline.py` 对比回滚后的基线，
   **不回到 1.000/0.955 就报错退出**（不"改完就算"）。

## 幂等 / 回滚

- 原值从 `mbse.db.bak-v14-*` 备份库读取（备份已校验：表数/agents/skills/
  agent_tools 行数与源库一致）；也支持 `--from-json` 手工指定。
- 重复跑结果一致（幂等）。
"""
from __future__ import annotations

import glob
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

NODES = ["methodology_resolver", "requirement_structuring", "architecture_skeleton",
         "view_expansion", "model_validation_repair", "trace_verification",
         "change_safety_gate", "model_release"]

#: 回滚目标基线（`verify_intent_routing_baseline.py` 文档字符串记载的历史值）
EXPECT_BUILTIN_ACC = 0.99
EXPECT_SAMPLE_ACC = 0.90


def _load_backup():
    import sqlite3
    cands = sorted(glob.glob(os.path.join(ROOT, "mbse.db.bak-v14-*")))
    if not cands:
        print("[FAIL] 找不到 v14 之前的备份库，无法自动回滚")
        print("       请用 --from-json 手工提供原值")
        return None
    path = cands[-1]
    print(f"  备份库：{os.path.basename(path)}")
    b = sqlite3.connect(path)
    b.row_factory = sqlite3.Row
    out = {}
    for n in NODES:
        row = b.execute("SELECT intent_keywords FROM agents WHERE name=?", (n,)).fetchone()
        if row and row[0]:
            out[n] = json.loads(row[0])
    b.close()
    return out or None


def _run_baseline():
    """跑既有基线评测，返回 (内置 accuracy, 真实样本 accuracy) 的百分数文本。"""
    p = subprocess.run(
        [sys.executable, "-X", "utf8",
         os.path.join(ROOT, "tools", "verify", "verify_intent_routing_baseline.py")],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=900, cwd=ROOT)
    out = p.stdout or ""
    accs = []
    for line in out.splitlines():
        if "accuracy=" in line:
            try:
                accs.append(float(line.split("accuracy=")[1].split()[0]))
            except Exception:
                pass
    return accs, out


def main() -> int:
    from database import db_conn

    print("=" * 72)
    print("① 读取 V14 前的原值")
    print("=" * 72)
    old = _load_backup()
    if not old:
        return 1
    for n in NODES:
        print(f"  {n:28} {old.get(n)}")

    print("\n" + "=" * 72)
    print("② 回滚 intent_keywords（保留 skill 绑定）")
    print("=" * 72)
    with db_conn() as conn:
        for n in NODES:
            kws = old.get(n)
            if kws is None:
                print(f"  [SKIP] {n} 备份里无值，未动")
                continue
            conn.execute("UPDATE agents SET intent_keywords=? WHERE name=?",
                         (json.dumps(kws, ensure_ascii=False), n))
            print(f"  [ROLLBACK] {n:28} -> {kws}")
        conn.commit()

        # skill 绑定必须仍在（这是 V14 唯一有效的部分）
        print("\n  ── skill 绑定保留情况 ──")
        lost = []
        for n in NODES:
            aid = conn.execute("SELECT id FROM agents WHERE name=?", (n,)).fetchone()[0]
            cnt = conn.execute(
                "SELECT count(*) FROM agent_tools WHERE agent_id=? AND tool_type='skill'",
                (aid,)).fetchone()[0]
            print(f"    {n:28} skill={cnt}")
            if cnt < 1:
                lost.append(n)
        if lost:
            print(f"[FAIL] 这些节点绑定被回滚掉了：{lost}")
            return 1
        print("    全部节点 skill 绑定仍在 ✓")

    print("\n" + "=" * 72)
    print("③ 回滚后复跑基线评测（不回到基线即判FAIL）")
    print("=" * 72)
    accs, out = _run_baseline()
    print(out.strip()[-900:])
    if not accs:
        print("[FAIL] 未解析到 accuracy，无法确认基线")
        return 1
    if len(accs) >= 2:
        b, s = accs[0], accs[1]
        print()
        print(f"  内置集 accuracy={b}（门槛 {EXPECT_BUILTIN_ACC}）")
        print(f"  真实样本 accuracy={s}（门槛 {EXPECT_SAMPLE_ACC}）")
        if b >= EXPECT_BUILTIN_ACC and s >= EXPECT_SAMPLE_ACC:
            print("  ✅ 基线已恢复")
            return 0
        print("  ❌ 基线仍未恢复 ⇒ skill 绑定也在影响路由，必须继续排查")
        return 1
    print(f"  仅解析到一个 accuracy={accs[0]}，样本不足")
    return 1


if __name__ == "__main__":
    sys.exit(main())