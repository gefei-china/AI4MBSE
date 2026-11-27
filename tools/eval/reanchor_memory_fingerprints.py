# -*- coding: utf-8 -*-
"""记忆评测集「内容指纹」快照生成器（P0-4，2026-11-27）。

## 为什么不再用 id 当锚
`eval_memory_quality.py` 的用例原先锚 `expect=[81]` 这类**行 id**。id 会因两条完全正常的
运维动作而失效：① 记忆被遗忘/归档；② 被删后重沉淀（新 id）。于是评测报 FAIL 的其实是
**库内容变了**，不是**召回退化了** —— 与"验证器也会错"同族。
2026-11-16 的重锚表只是把 id 手工换成另一批 id，**根因没动**（下一次库演进又全红）。

## 指纹是什么、不是什么
- **是**：内容归一化后的 md5 + 前 400 字原文。归一化 = 全角转半角、去空白/标点、小写。
  ⇒ 同一句话的"标点/空格差异"仍能命中；**换了说法的改写命中不了**（那归语义等价管）。
- **不是**：向量。向量随 embedding provider/版本漂移，写进快照等于把"当时那台机器的模型"
  固化进评测集 —— 这是本仓明确反对的做法（口径必须可复现，不能被一次配额降级改写）。

## 与「语义等价集合」的分工
本文件给**指纹**（逐字/近乎逐字）；评测运行期再用**参考正文的向量**与库内活跃条目算余弦
（`--sem-sim`，默认 0.90）得到**语义等价集合**。两者并集才是判定面。
⇒ 行被删、被重沉淀、被改写，只要库里还有"讲同一件事"的条目，用例就**仍然有判别力**。

## 用法
    ./.venv/Scripts/python.exe -X utf8 tools/eval/reanchor_memory_fingerprints.py          # 只打印
    ./.venv/Scripts/python.exe -X utf8 tools/eval/reanchor_memory_fingerprints.py --write   # 落盘
生成物 `tools/eval/memory_eval_fingerprints.json` **必须入库**（它是评测集的参照系）。
⚠️ 快照取的是 `agent_memory` **全表**（含 forgotten/archival）—— 那些行恰恰是"最容易被误判成
召回失败"的锚点，必须留下它们的正文。
"""
import argparse
import json
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

# ⚠️ 归一化/指纹**只从评测主干 import**，不在这里另写一份：
#   "同一判据两份实现"必然漂移（本仓同类事故已多次），而漂移后
#   现场表现是"快照里的 fp 与运行期算出的 fp 对不上 ⇒ 指纹通道静默失效"，
#   比没有指纹更糟（看着有、其实一条都不命中）。
from tools.eval.eval_memory_quality import (  # noqa: E402
    T_CASES, M_CASES, C_CASES, REANCHOR_ALIVE, fingerprint)

FP_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "memory_eval_fingerprints.json")


def build(db_path: str) -> dict:
    """从库里取每个用例锚定行的正文 → {case_id: [{id, fp, head}...]}"""
    conn = sqlite3.connect("file:%s?mode=ro" % db_path.replace("\\", "/"), uri=True)
    conn.row_factory = sqlite3.Row
    try:
        out = {}
        for cs in list(T_CASES) + list(M_CASES) + list(C_CASES):
            ids = []
            for i in (cs.get("expect") or []):
                if int(i) not in ids:
                    ids.append(int(i))
            for i in (REANCHOR_ALIVE.get(cs["id"]) or []):
                if int(i) not in ids:
                    ids.append(int(i))
            refs = []
            for i in ids:
                row = conn.execute(
                    "SELECT id, content FROM agent_memory WHERE id=?", (i,)).fetchone()
                if not row:
                    continue
                refs.append({"id": int(row["id"]),
                             "fp": fingerprint(row["content"]),
                             "head": str(row["content"] or "")[:400]})
            out[cs["id"]] = refs
        return {"generated_at": __import__("time").strftime("%Y-%m-%d %H:%M:%S"),
                "db": os.path.basename(db_path),
                "norm": "fullwidth->halfwidth, drop spaces/punct, lower",
                "cases": out}
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(ROOT, "mbse.db"))
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--out", default=FP_PATH)
    args = ap.parse_args()
    data = build(args.db)
    n_ref = sum(len(v) for v in data["cases"].values())
    n_case = len(data["cases"])
    missing = [k for k, v in data["cases"].items() if not v]
    print("用例 %d 个 / 参照条目 %d 条" % (n_case, n_ref))
    if missing:
        print("⚠ 无参照的用例（行已被删，无法取正文 → 运行时只能靠语义等价兜）：%s"
              % ", ".join(missing))
    for k, v in data["cases"].items():
        print("  %-4s %s" % (k, " | ".join("%s:%s" % (r["id"], r["head"][:24]) for r in v)))
    if args.write:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print("已写出：%s" % args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
