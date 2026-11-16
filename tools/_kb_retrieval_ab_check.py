# -*- coding: utf-8 -*-
"""检索侧 A/B 断言：软删重复文档后，检索结果不得再命中它，且腾出的槽位给了别的文档。

与 `_kb_retrieval_ab.py`（只负责跑并落 JSON）分工：
  · 那一个 = **执行器**（真实调用 GraphRAG.retrieve，两库各跑一次）
  · 本文件 = **断言器**（读两份 JSON 做对比判定，纯函数，可被门禁复用）

判据：
  A1 被下架文档不得出现在 B 组任何 query 的命中文档里（核心）
  A2 A 组确实命中了它（否则 A1 是空转 —— 没测到 ≠ 通过）
  A3 B 组每个 query 仍能命中其它文档（不是把检索弄空了）
  A4 命中槽位不减少（腾出的位置被其它文档接住，而不是白丢）
"""
import argparse
import json
import sys

PASS, FAIL, SKIP = [], [], []


def chk(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name,
                           ("　— " + str(detail)) if detail and not cond else ""))


def skip(name, why):
    SKIP.append(name)
    print("  [SKIP] %s :: %s" % (name, why))


def compare(a, b, dropped_id):
    """纯函数：返回 (a_hit, b_hit, per_query)。门禁与自检共用。"""
    a_q = {x["q"]: x for x in a.get("queries", [])}
    b_q = {x["q"]: x for x in b.get("queries", [])}
    per = []
    for q, ax in a_q.items():
        bx = b_q.get(q) or {}
        per.append({
            "q": q,
            "a_docs": ax.get("docs") or [],
            "b_docs": bx.get("docs") or [],
            "a_n": ax.get("n_chunks") or 0,
            "b_n": bx.get("n_chunks") or 0,
            "a_hit_dropped": dropped_id in (ax.get("docs") or []),
            "b_hit_dropped": dropped_id in (bx.get("docs") or []),
            "b_has_others": bool([d for d in (bx.get("docs") or []) if d != dropped_id]),
        })
    return per


def judge(a, b, dropped_id):
    """返回违规列表（空=通过）。"""
    bad = []
    per = compare(a, b, dropped_id)
    if not per:
        return ["A/B 都没有 query 结果，无法判定"]
    if not any(x["a_hit_dropped"] for x in per):
        bad.append("A 组没有任何 query 命中 doc %s ⇒ 本对比没测到该文档，"
                   "A1 会变成空转（应换 query 或该文档本就不相关）" % dropped_id)
    hit_in_b = [x["q"] for x in per if x["b_hit_dropped"]]
    if hit_in_b:
        bad.append("B 组仍有 query 命中已下架的 doc %s：%s" % (dropped_id, hit_in_b))
    empty_b = [x["q"] for x in per if not x["b_has_others"]]
    if empty_b:
        bad.append("B 组这些 query 除被删文档外没有其它命中（检索被弄空）：%s" % empty_b)
    lost = [(x["q"], x["a_n"], x["b_n"]) for x in per if x["b_n"] < x["a_n"]]
    if lost:
        bad.append("B 组命中数少于 A 组（槽位白丢，未被其它文档接住）：%s" % lost)
    return bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--a", required=True)
    ap.add_argument("--b", required=True)
    ap.add_argument("--dropped", type=int, required=True)
    args = ap.parse_args()
    a = json.load(open(args.a, encoding="utf-8"))
    b = json.load(open(args.b, encoding="utf-8"))
    print("=" * 78)
    print("检索 A/B 断言：A=%s  B=%s  被下架文档=%s"
          % (a.get("label"), b.get("label"), args.dropped))
    print("=" * 78)
    per = compare(a, b, args.dropped)
    for x in per:
        print("  Q: %s" % x["q"][:56])
        print("     A docs=%s (%d chunks)" % (x["a_docs"], x["a_n"]))
        print("     B docs=%s (%d chunks)" % (x["b_docs"], x["b_n"]))
        if x["a_hit_dropped"]:
            print("     ★ A 命中了 doc %s；B %s"
                  % (args.dropped, "仍命中（未修复）" if x["b_hit_dropped"] else "已不再命中"))
    print()
    bad = judge(a, b, args.dropped)
    chk("A1 已下架文档不再出现在任何 query 的命中里",
        not any(x["b_hit_dropped"] for x in per))
    chk("A2 A 组确实命中过它（否则 A1 空转）",
        any(x["a_hit_dropped"] for x in per))
    chk("A3 B 组每个 query 仍命中其它文档", all(x["b_has_others"] for x in per))
    chk("A4 命中槽位未减少（腾出的位置被接住）",
        all(x["b_n"] >= x["a_n"] for x in per),
        [(x["q"][:30], x["a_n"], x["b_n"]) for x in per if x["b_n"] < x["a_n"]])
    # 变异自证：把 B 的 docs 塞回被删文档 ⇒ A1 必须判红
    mb = json.loads(json.dumps(b))
    for x in mb["queries"]:
        x["docs"] = (x.get("docs") or []) + [args.dropped]
    chk("M1 变异：B 仍命中被删文档 ⇒ 判据必须判红",
        any(v.startswith("B 组仍有 query") for v in judge(a, mb, args.dropped)))
    # 变异自证：把 B 的命中数压到 0 ⇒ A4 必须判红
    mb2 = json.loads(json.dumps(b))
    for x in mb2["queries"]:
        x["n_chunks"] = 0
    chk("M2 变异：B 命中数被清零 ⇒ 判据必须判红",
        any(v.startswith("B 组命中数少于") for v in judge(a, mb2, args.dropped)))
    print()
    print("PASS %d / FAIL %d / SKIP %d" % (len(PASS), len(FAIL), len(SKIP)))
    if FAIL:
        for f in FAIL:
            print("  -", f)
        for v in bad:
            print("  !", v)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
