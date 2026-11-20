# -*- coding: utf-8 -*-
"""证据链落地：把 Dreaming 候选的 `verify_state` + `evidence_ref` 写进记忆库。

## 为什么需要这个工具（2026-11-16）

`agent_memory` 有完整的证据链字段（`verify_state` / `evidence_ref` /
`evidence_note` / `source_credibility`），`core/memory_dream.py` 也有完整的三态
判定逻辑（`_verify_claim`：否定优先两遍判定 + 数值冲突分级）。

**但真机实测：295 条记忆全部 `unverifiable`、`evidence_ref` 全空。**
查下来不是能力缺失 —— `dream()` 跑 40 条就产出 47 个候选、38 个带
`evidence_ref`（如 `messages#5057`）。缺的是**没人触发 apply**
（`apply_candidates` 要求 `hil_approved=True`，而全仓没有调用入口）。
⇒ 等于「校验机制建好了但没通电」。

## 分级设计（关键：不同状态的业务影响**不等价**）

| state | 写入的列 | 对召回的影响 | 风险 |
|---|---|---|---|
| `verified` | verify_state / evidence_ref / evidence_note | **无**（不参与排除） | 低：只是"找到了流水证据"这个客观事实 |
| `needs_review` | 同上 | **无**（`EXCLUDED_FROM_RECALL` 只含 contradicted） | 低：只是排队等人工 |
| `contradicted` | 同上 | **会被召回侧排除** | **高：误判 = 丢真记忆** |

⇒ 工具按状态分级，默认**只落低风险的两种**，`contradicted` 必须显式点名：

```
--states verified,needs_review     # 默认：纯事实标注，不动召回
--states all                       # 含 contradicted（= 全批准，需人工确认）
```

## 硬纪律（沿用 memory_dream 的设计，不改）
1. **默认 dry-run**：不加 `--apply` 一个字节都不写。
2. **不改 content / tier / forgotten**：只改上面 4 列中的 3 个（影子语义，可回退）。
   由 `apply_candidates` 保证 —— 本工具不自己写 SQL。
3. **input_sha256 校验**：候选包与当前库不一致（`INPUT_STORE_CHANGED`）就拒绝落库，
   防止"看着是这批记忆，落地时库已经被改了"。
4. **留痕**：`approved_by` 写进 evidence_note + 落库清单 JSON。

## 用法
    # ① 看候选（dry-run，什么都不写）
    ./.venv/Scripts/python.exe -X utf8 tools/memory_dream_apply.py

    # ② 只落低风险状态（verified + needs_review）
    ./.venv/Scripts/python.exe -X utf8 tools/memory_dream_apply.py --apply --by miba

    # ③ 含 contradicted（会被召回排除，慎用）
    ./.venv/Scripts/python.exe -X utf8 tools/memory_dream_apply.py --apply --states all --by miba
"""
import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

#: 低风险状态：写进去不影响召回（verified 只是标注，needs_review 只排队）
LOW_RISK_STATES = ("verified", "needs_review")

#: `--states all` 的展开值（含 contradicted —— 它会被召回侧排除，需显式点名）
ALL_STATES = ("verified", "needs_review", "contradicted", "unverifiable")


def normalize_states(spec):
    """把 `--states` 字符串解析成集合；`all` ⇒ ALL_STATES。

    抽成函数是为了**可测**：门禁要能直接验证"默认分级确实把 contradicted 挡在门外"，
    而不是只在 main() 里跑一遍看输出。写在 main() 里就只能靠人眼，等于没判据。
    """
    s = str(spec or "").strip().lower()
    if s == "all":
        return set(ALL_STATES)
    return {x.strip() for x in str(spec or "").split(",") if x.strip()}


def filter_by_states(pkg, states):
    """按状态过滤候选包（只过滤 `action == 'correct'` 的项，其它动作原样保留）。

    `states` 为 None ⇒ 不过滤（= all）。
    返回 (过滤后的新包, 丢弃条数)。
    """
    all_c = pkg.get("candidates") or []
    if states is None:
        keep = all_c
    else:
        keep = [c for c in all_c
                if c.get("action") != "correct" or (c.get("state") in states)]
    out = dict(pkg)
    out["candidates"] = keep
    return out, len(all_c) - len(keep)


def summarize(pkg):
    cs = pkg.get("candidates") or []
    by_state, by_action = {}, {}
    for c in cs:
        by_action[c.get("action")] = by_action.get(c.get("action"), 0) + 1
        if c.get("action") == "correct":
            st = c.get("state") or "?"
            by_state[st] = by_state.get(st, 0) + 1
    return {
        "total": len(cs),
        "by_action": by_action,
        "correct_by_state": by_state,
        "notes_scanned": (pkg.get("stats") or {}).get("notes_scanned"),
        "input_sha256": (pkg.get("input_sha256") or "")[:16],
        "fingerprint": (pkg.get("fingerprint") or "")[:16],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(ROOT, "mbse.db"))
    ap.add_argument("--apply", action="store_true", help="真正写库（默认 dry-run）")
    ap.add_argument("--states", default=",".join(LOW_RISK_STATES),
                    help="要落库的状态（逗号分隔）：verified,needs_review / all。"
                         "默认只落低风险两种（contradicted 会被召回排除）")
    ap.add_argument("--note-limit", type=int, default=80,
                    help="扫描多少条记忆（默认 80；-1 = 全量）")
    ap.add_argument("--by", default="", help="批准人（落库留痕，--apply 时必填）")
    ap.add_argument("--report", default="")
    args = ap.parse_args()

    from core.memory_dream import dream, apply_candidates

    import sqlite3
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    if args.note_limit and args.note_limit > 0:
        limit = args.note_limit
    else:
        limit = conn.execute("select count(*) from agent_memory "
                             "where coalesce(forgotten,0)=0").fetchone()[0] or 1

    # ── ① 生成候选包（只读）──
    pkg = dream(conn, note_limit=limit)
    s = summarize(pkg)
    print("=" * 74)
    print("证据链落地 · %s" % ("DRY-RUN（不写库）" if not args.apply else "APPLY"))
    print("=" * 74)
    print("扫描记忆: %s 条 | 候选: %s | 输入sha: %s"
          % (s["notes_scanned"], s["total"], s["input_sha256"]))
    print("按动作: %s" % json.dumps(s["by_action"], ensure_ascii=False))
    print("correct 按状态: %s" % json.dumps(s["correct_by_state"], ensure_ascii=False))

    # ── ② 过滤：只保留指定状态（逻辑在 filter_by_states，已被门禁直接测试）──
    try:
        wanted = normalize_states(args.states)
    except Exception:
        wanted = None
    bad = wanted - set(ALL_STATES) if wanted else set()
    if bad:
        print("\n✗ 未知状态: %s（可选 %s / all）" % (sorted(bad), "/".join(ALL_STATES)))
        return 2
    if str(args.states).strip().lower() == "all":
        wanted = None
    filtered, dropped = filter_by_states(pkg, wanted)
    print("\n状态过滤: %s ⇒ 保留 %d 条（丢弃 %d 条）"
          % ("all" if wanted is None else ",".join(sorted(wanted)),
             len(filtered.get("candidates") or []), dropped))

    # 风险提示：contradicted 会被召回侧排除
    _kc = filtered.get("candidates") or []
    n_contra = sum(1 for c in _kc
                   if c.get("action") == "correct" and c.get("state") == "contradicted")
    if n_contra:
        print("  ⚠️ 其中 %d 条 contradicted —— 落库后**会被召回侧排除**，"
              "误判等于丢掉真记忆" % n_contra)

    # ── ③ 预览样本 ──
    sample = [c for c in _kc if c.get("action") == "correct"][:8]
    if sample:
        print("\n样本（前 %d 条）:" % len(sample))
        for c in sample:
            print("  mem=%-5s %-13s ref=%-16s overlap=%s"
                  % (c.get("memory_id"), c.get("state"),
                     c.get("evidence_ref"), c.get("overlap")))
            print("      %s" % str(c.get("reason") or "")[:88])

    if not args.apply:
        print("\n（dry-run：未写库。加 --apply --by <批准人> 才落库）")
        _maybe_report(args, filtered, s, applied=None)
        conn.close()
        return 0

    # ── ④ 真正落库 ──
    if not str(args.by).strip():
        print("\n✗ --apply 必须带 --by <批准人>（留痕纪律）")
        conn.close()
        return 2

    res = apply_candidates(conn, filtered, hil_approved=True,
                           approved_by=str(args.by).strip())
    print("\n落库结果: ok=%s applied=%s reason=%s"
          % (res.get("ok"), res.get("applied"), res.get("reason")))
    if not res.get("ok"):
        print("  未写任何行。常见原因：INPUT_STORE_CHANGED（候选包与库不一致，重跑即可）")
        conn.close()
        return 1

    # 事后核对：verify_state 分布 + evidence_ref 填充率
    dist = {}
    for r in conn.execute("select coalesce(verify_state,'(null)') s, count(*) n "
                          "from agent_memory group by 1 order by n desc"):
        dist[r["s"]] = r["n"]
    n_ev = conn.execute("select count(*) from agent_memory "
                        "where evidence_ref is not null and evidence_ref<>''").fetchone()[0]
    n_all = conn.execute("select count(*) from agent_memory").fetchone()[0]
    print("\n事后核对:")
    print("  verify_state 分布: %s" % json.dumps(dist, ensure_ascii=False))
    print("  evidence_ref 填充: %d / %d (%.1f%%)"
          % (n_ev, n_all, 100.0 * n_ev / max(1, n_all)))

    _maybe_report(args, filtered, s, applied=res)
    conn.close()
    return 0


def _maybe_report(args, pkg, summary, applied):
    """落库/预览清单写文件，便于审计与回退。"""
    path = args.report
    if not path:
        tag = "applied" if applied else "preview"
        path = os.path.join(ROOT, "tmp",
                            "_memory_evidence_%s_%s.json"
                            % (tag, time.strftime("%Y%m%d_%H%M%S")))
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        payload = {
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "db": args.db,
            "dry_run": applied is None,
            "states": args.states,
            "approved_by": args.by,
            "summary": summary,
            "apply_result": applied,
            "candidates": [
                {k: c.get(k) for k in ("action", "memory_id", "state",
                                       "evidence_ref", "overlap", "reason")}
                for c in (pkg.get("candidates") or [])],
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        print("\n清单已写: %s" % path)
        if applied:
            print("回退方式: 把这些 memory_id 的 verify_state 置回 'unverifiable'、"
                  "evidence_ref 置空（本清单含完整 id 列表）")
    except Exception as e:
        print("\n（清单写入失败，不影响落库结果）: %r" % (e,))


if __name__ == "__main__":
    sys.exit(main())
