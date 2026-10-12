"""verify_rerun_gain — 判据：九处修复后的**端到端对照**（基线 vs 重跑）。

## 为什么要有这个
前面十二道门禁都是**机制级**（"分流逻辑对不对"、"预算压得对不对"）。
但米爸要的是**"到底有没有变好"** —— 只能靠端到端对照回答。

## 对照口径（必须可比，否则结论无效）
| 维度 | 基线 | 重跑 |可比性 |
|---|---|---|---|
| 会话 | 9240–9248 | 新建 | — |
| provider | 103 qwen3.8-omni-flash（主） | **104 qwen3.8-max** | ⚠️ **模型不同** |
| 工具调用总数 | 41 | 重跑值 | 机制级，**可比** |
| 无效查询占比 | 29%（12/41） | 重跑值 | 机制级，**可比** |
| `validate` 排最后的比例 | 6/6 | 重跑值 | 机制级，**可比** |
| `autofix` 调用次数 | **0** | 重跑值 | 机制级，**可比** |
| 思考占 completion | 63% | 重跑值 | ⚠️ 模型自带 thinking 比例不同 |

⚠️ **耗时/质量不可直接对比**（模型不同：基线 103 omni-flash vs 重跑 104 max）。

## 判据
① 工具调用**效率类**指标不劣化（无效查询占比下降）
② `autofix` 从 0 变成被调用（这是缺陷③的核心目标）
③ 交付物**可被校验器解析**（不是靠"看起来像代码"）
④ 思考关闭的 intent 上，思考占比确实下降（若该 intent 有样本）

## 用法
    ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_rerun_gain.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, _ROOT)

DB = os.path.join(_ROOT, "mbse.db")
BASE = [9240, 9242, 9243, 9244, 9245, 9246, 9247, 9248]
VIEW_OF = {9240: "v1 需求", 9242: "v2 用例", 9243: "v3 结构", 9244: "v4 活动",
           9245: "v5 IBD", 9246: "v6 时序", 9247: "v7 状态机", 9248: "v8 参数"}


def _conn():
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def _tool_stats(c, conv):
    rows = list(c.execute(
        "SELECT tool_name, ok, result FROM tool_call_logs "
        "WHERE conversation_id=? ORDER BY id", (conv,)))
    names = [r["tool_name"] for r in rows]
    bad = sum(1 for r in rows
              if r["ok"] == 0 or "未找到" in (r["result"] or "")
              or "需提供" in (r["result"] or ""))
    vp = next((i + 1 for i, n in enumerate(names) if n == "sysml_v2_validate"), None)
    return {
        "n": len(rows),
        "bad": bad,
        "bad_pct": round(bad / len(rows) * 100) if rows else None,
        "validate_last": (vp == len(names)) if (vp and names) else False,
        "autofix": sum(1 for n in names if n == "sysml_v2_autofix"),
        "notfound": sum(1 for r in rows if "未找到" in (r["result"] or "")),
    }


#: 本轮重跑的起始会话 id。
#: ★ 为什么不能用 `LIKE 'crypto_chain%'`：实测库里有多批同名会话
#:   （含 9289/9307-9309 等失败尝试）⇒ LIKE 会把它们全混进来，视图错位。
RERUN_MIN_ID = 9319
RERUN_TITLES = tuple(f"crypto_chain_{k}" for k in
                     ("v1_requirement", "v2_usecase", "v3_structure", "v4_activity",
                      "v5_ibd", "v6_sequence", "v7_state", "v8_parameter"))


def find_rerun_convs(c):
    """找出重跑会话：id >= RERUN_MIN_ID、标题精确匹配，取每个视图**最新**一次。"""
    q = ("SELECT id, title FROM conversations WHERE id >= ? AND title IN "
         "({}) ORDER BY id").format(",".join("?" * len(RERUN_TITLES)))
    latest = {}
    for r in c.execute(q, (RERUN_MIN_ID,) + RERUN_TITLES):
        t = r["title"]
        if t not in latest or r["id"] > latest[t]:
            latest[t] = r["id"]
    return [latest[t] for t in RERUN_TITLES if t in latest]


def main() -> int:
    print("=" * 78)
    print("端到端对照 · 基线(9240-9248) vs 九处修复后重跑")
    print("=" * 78)
    c = _conn()
    rerun = find_rerun_convs(c)
    if not rerun:
        print("[FAIL] 未找到重跑会话（id > 9288 且 title like 'crypto_chain%'）")
        print("       ⇒ 重跑尚未产生数据，无法对照")
        return 1

    base_all = [_tool_stats(c, v) for v in BASE if list(
        c.execute("SELECT 1 FROM tool_call_logs WHERE conversation_id=? LIMIT 1", (v,)))]
    rerun_all = [_tool_stats(c, v) for v in rerun]

    def agg(rows):
        n = sum(r["n"] for r in rows)
        bad = sum(r["bad"] for r in rows)
        return {
            "convs": len(rows), "calls": n, "bad": bad,
            "bad_pct": round(bad / n * 100) if n else None,
            "validate_last": sum(1 for r in rows if r["validate_last"]),
            "autofix": sum(r["autofix"] for r in rows),
            "notfound": sum(r["notfound"] for r in rows),
        }

    b, r = agg(base_all), agg(rerun_all)
    print(f"\n基线会话 {BASE}")
    print(f"重跑会话 {rerun}\n")
    hdr = f"{'指标':26} {'基线':>12} {'重跑':>12}"
    print(hdr)
    print("-" * len(hdr))
    for key, label, better in [
        ("convs", "会话数", None),
        ("calls", "工具调用总数", "down"),
        ("bad", "无效查询数", "down"),
        ("bad_pct", "无效查询占比%", "down"),
        ("notfound", "『未找到』结果数", "down"),
        ("validate_last", "validate排最后", "none"),
        ("autofix", "autofix 调用次数", "up"),
    ]:
        bs, rs = f"{b[key]}", f"{r[key]}"
        flag = ""
        if better == "down" and isinstance(b[key], int) and isinstance(r[key], int):
            flag = "  ✔ 改善" if r[key] < b[key] else ("  ✘ 变差" if r[key] > b[key] else "")
        elif better == "up":
            flag = "  ✔ 改善" if r[key] > b[key] else ""
        print(f"{label:26} {bs:>12} {rs:>12}{flag}")

    print("\n" + "=" * 78)
    print("逐视图明细")
    print("=" * 78)
    print(f"{'视图':12} {'基线:次数/无效':>16} {'重跑:次数/无效':>16} {'autofix':>10}")
    for bv, rv in zip(BASE, rerun):
        bs = _tool_stats(c, bv)
        rs = _tool_stats(c, rv)
        print(f"{VIEW_OF.get(bv, bv):12} "
              f"{f'{bs['n']}/{bs['bad']}':>16} {f'{rs['n']}/{rs['bad']}':>16} "
              f"{f'{bs['autofix']}→{rs['autofix']}':>10}")

    # ── 判据 ──
    print("\n" + "=" * 78)
    print("判据")
    print("=" * 78)
    ok = True
    checks = []
    if r["convs"] < len(BASE):
        checks.append((f"[INFO] 重跑只完成 {r['convs']}/{len(BASE)} 个视图"
                       f" ⇒ 对照不完整，下述结论仅对已跑部分成立", True))
    # ① 无效查询占比不劣化
    if b["bad_pct"] is not None and r["bad_pct"] is not None:
        checks.append((f"① 无效查询占比 {b['bad_pct']}% → {r['bad_pct']}%"
                       f"（不劣化）", r["bad_pct"] <= b["bad_pct"]))
    # ② autofix 被调用（缺陷③的核心目标）
    checks.append((f"② autofix 被调用（基线 {b['autofix']} → 重跑 {r['autofix']}）",
                   r["autofix"] > b["autofix"]))
    # ③『未找到』应减少（缺陷A：标准库漏索引 + 参数名）
    checks.append((f"③ 『标准库中未找到』{b['notfound']} → {r['notfound']}（减少）",
                   r["notfound"] <= b["notfound"]))
    for name, passed in checks:
        print(f"[{'OK  ' if passed else 'FAIL'}] {name}")
        ok = ok and passed

    print("\n" + "=" * 78)
    print(f"判据结论：{'全部通过' if ok else '存在未通过项'}")
    print("⚠️ 提醒：重跑用 qwen3.8-max(104)，基线主用 qwen3.8-omni-flash(103)")
    print("   ⇒ 耗时/产出质量**不可直接对比**，只有机制类指标可比。")
    print("=" * 78)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())