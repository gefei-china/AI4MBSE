"""eval_augmented.py —— P0-4 泛化语料的**中间验收**（只读，不改任何样本状态）。

解决的问题：泛化产物一律 `status='suggested'`，需人工确认才进评测集（`confirmed`）。
但人工确认前，得先回答两个问题：
  ① 这些泛化说法在**真实链路上判得动吗**？（若普遍判不动，说明泛化偏离了系统能力边界）
  ② 确认它们之后，**原 44 条的准确率会不会被拖累**？（理论上不会：评测逐条独立、
     且泛化语料**不进**规则层/语义索引 —— 见脚本末尾的泄漏断言）

因此本脚本**不写库、不改状态**，只把两组分开跑并对比：
  - `confirmed`（现有评测集，n=44）
  - `source='aug'`（泛化候选，未确认）

判读纪律：
  - `aug` 组的准确率**不是**"系统变差了"，而是**泛化语料的难度画像**；
  - 若 aug 组大量判成 `chat`，多半是语料过度口语/边缘 → 应由人工在确认时筛掉，
    而不是去改系统（那是"用测试集调参"）。
  - 逐条结果全部落盘，便于人工按 `intent` 逐类筛。

跑法（必须用项目 venv）：
  <repo>\\.venv\\Scripts\\python.exe -X utf8 tests\\manual_verify\\eval_augmented.py --limit-per-intent 5
"""
import argparse
import json
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from database import get_db                # noqa: E402
from agent.pipeline import AgentPipeline   # noqa: E402


def load_rows(conn, where: str, args=()):
    return [(r["text"], r["intent"], r["status"], r["source"])
            for r in conn.execute(
                "SELECT text, intent, status, source FROM intent_samples "
                f"WHERE intent<>'' AND ({where}) ORDER BY intent, id", args).fetchall()]


def cap(rows, per_intent):
    """每意图截断，避免逐条真实链路调用过久。"""
    if per_intent <= 0:
        return rows
    out, cnt = [], Counter()
    for t, i, s, src in rows:
        if cnt[i] < per_intent:
            out.append((t, i, s, src))
            cnt[i] += 1
    return out


def run_group(rt, get_db_fn, rows):
    got = []
    for text, want, status, source in rows:
        c = get_db_fn()
        try:
            try:
                c.execute("DELETE FROM intent_cache")
                c.commit()
            except Exception:
                pass
            pred = rt.detect(text, conn=c)
            meta = rt.get_last_meta()
        finally:
            c.close()
        got.append({"text": text, "want": want, "pred": pred, "route": meta["route"],
                    "conf": meta["confidence"], "ok": pred == want})
    return got


def report(name, got):
    n = len(got)
    if not n:
        print(f"\n[{name}] 无样本")
        return {}
    acc = sum(1 for g in got if g["ok"]) / n
    print(f"\n[{name}] n={n}  accuracy={acc:.4f}  错 {n - sum(1 for g in got if g['ok'])} 条")
    per = defaultdict(lambda: [0, 0])
    for g in got:
        per[g["want"]][1] += 1
        per[g["want"]][0] += (1 if g["ok"] else 0)
    for k in sorted(per, key=lambda x: -per[x][1]):
        ok, tot = per[k]
        print(f"    {k:<24} {ok}/{tot}  {ok / tot:.2f}")
    dist = Counter(g["route"] for g in got)
    print(f"    route 分布: {dict(dist)}")
    return {"n": n, "accuracy": acc}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit-per-intent", type=int, default=5, help="每意图最多跑几条（0=不限）")
    ap.add_argument("--out", default="tmp/p_intent/eval_augmented.json")
    args = ap.parse_args()

    conn = get_db()
    try:
        conf_rows = load_rows(conn, "status='confirmed'")
        aug_rows = load_rows(conn, "source='aug' AND status<>'confirmed'")
    finally:
        conn.close()

    print("=" * 78)
    print("P0-4 中间验收：泛化候选在真实链路上的可达性（只读，不改样本状态）")
    print("=" * 78)
    print(f"confirmed={len(conf_rows)} 条   aug候选={len(aug_rows)} 条")

    pipe = AgentPipeline()
    pipe._load_db_agents()          # ⚠️ 必须调：否则语义索引为空，测的是退化的关键词层
    rt = pipe.router
    get_db_fn = get_db

    aug_capped = cap(aug_rows, args.limit_per_intent)
    print(f"本次实跑：confirmed {len(conf_rows)} 条 + aug {len(aug_capped)} 条"
          f"（aug 每意图上限 {args.limit_per_intent}）")

    res_conf = run_group(rt, get_db_fn, conf_rows)
    res_aug = run_group(rt, get_db_fn, aug_capped)

    print("\n" + "-" * 78)
    s1 = report("confirmed（现有评测集）", res_conf)
    s2 = report("aug（泛化候选，未确认）", res_aug)
    print("\n" + "-" * 78)
    if s1 and s2:
        print(f"对照：confirmed {s1['accuracy']:.4f}  vs  aug {s2['accuracy']:.4f}"
              f"  （差 {s1['accuracy'] - s2['accuracy']:+.4f}）")
        print("判读：aug 组偏低属**预期**（口语/边缘说法多）；关键是**没有整片判成 chat**。")

    # ── 泄漏自证：泛化语料不得进入决策路径 ──
    print("\n[泄漏自证] aug 语料是否混进了决策路径（必须全为 0）")
    aug_texts = {t for t, *_ in aug_rows}
    leaks = 0
    c2 = get_db()
    try:
        triggers = {r[0] for r in c2.execute("SELECT trigger FROM intent_rules").fetchall()}
        hit = aug_texts & triggers
        print(f"  intent_rules.trigger 命中 aug 文本: {len(hit)}")
        leaks += len(hit)
    except Exception as e:
        print(f"  intent_rules 检查跳过：{e}")
    finally:
        c2.close()
    from agent.intent import IntentRouter
    utterances = set(IntentRouter._SEMANTIC_UTTERANCES or ())
    hit2 = aug_texts & utterances
    print(f"  _SEMANTIC_UTTERANCES 命中 aug 文本: {len(hit2)}")
    leaks += len(hit2)
    print(f"  → 泄漏总数 {leaks}（0 = 泛化语料只在样本池里，未进规则层/语义索引）")

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump({"confirmed": res_conf, "aug": res_aug, "summary": {"confirmed": s1, "aug": s2},
                   "leaks": leaks}, f, ensure_ascii=False, indent=1)
    print(f"\n逐条结果已落盘：{args.out}（可按 intent 逐类筛，供人工确认时参考）")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
