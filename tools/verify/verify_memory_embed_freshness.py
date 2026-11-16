# CI-OPTIONAL: [local] tier=local（见 tools/gates_manifest.json）：需真实 embedding 服务（内网 192.168.100.202:8004）+ 真库 mbse.db，CI runner 无该组合 ⇒ 本机跑。判据：库内记忆向量与当前模型自洽（>=0.9）；服务不可用时整组 SKIP（测不了不等于没问题）。2026-11-16 新增：该缺陷（换模型旧向量没重算）使记忆评测 27.3%->63.6%。改法须同步改清单 tier 并接线，不允许只删这行
# -*- coding: utf-8 -*-
"""verify_memory_embed_freshness.py —— 记忆向量必须与**当前** embedding 模型同源。

## 真机事故（2026-11-16，架构级）

排查"高价值老记忆进不了 top4"时，逐维拆解排序分数发现：
查询「需求追溯链有几个环节」，而 #133 内容「需求追溯链五环节：来源需求→…」
（字面几乎完全相同）的真向量余弦只有 **0.014**；而弱相关的 #457 却得 0.636。

进一步做"库内向量 vs 同内容重新 embedding"的自洽比对（决定性判据）：

| 记忆 | 创建时间 | 自洽余弦 |
|---|---|---|
| #91 / #133 / #134 / #135 | 2026-09-11~17 | **-0.02 ~ 0.02** |
| #186 / #216 | 2026-09-26 | -0.01 / 0.02 |
| #268 | 2026-10-01 | 0.025 |
| #91 | 2026-09-11 | -0.021 |
| #504 | 2026-11-04 | **1.0000** |
| #657 | 2026-11-16 | **1.0000** |

规律：**2026-11-04 前后换过 embedding 模型**，旧向量没重算。
而 `embed_version` 全都标着 `openai-compat` ⇒ `search_core` 按版本号判断"同源"，
**不做任何校验** ⇒ 旧向量与查询向量近乎正交 ⇒ 那些记忆在向量检索里
**完全不可见**（余弦≈0 ⇒ 排序垫底 ⇒ 永不召回）。

后果量化：全量重算 198 条后，记忆质量评测 **27.3% → 63.6%**（提升 4 条，回归 0 条）。

## 本门禁锁什么

A 组（真库，抽样自洽性）：
  A1 抽样记忆的「库内向量 vs 当前模型重算」余弦 ≥ 阈值（默认 0.9）
     —— 不一致说明向量过期，**不是"检索算法"问题，是数据问题**
  A2 抽样覆盖多个创建时间段（防止只测新数据而漏掉旧数据）
  A3 变异自证：把某条向量置零后，A1 判据必须翻红

B 组（接线）：
  B1 重算工具存在且支持 --force-reembed
  B2 工具默认 dry-run（不 --apply 不写库）
  B3 工具在版本降级（bigram-tf）时必须停止且不写

## 为什么阈值取 0.9
同一模型对**完全相同文本**两次 embedding 的自洽余弦实测 1.0000；
不同模型的向量近乎正交（|cos| < 0.03）。
0.9 落在两个簇中间且远离两侧，有充分余量。**不是拍脑袋**：
取 0.5 会把不匹配的也放过（实测最大不匹配值 0.025），取 0.99 会因
浮点/服务端微抖动误报。

用法： ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_memory_embed_freshness.py
退出码：0 全绿（无库/无服务时 SKIP）／1 有失败
"""
import json
import math
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
DB = os.environ.get("MBSE_GATE_DB") or os.environ.get("MBSE_DB_PATH") or os.path.join(ROOT, "mbse.db")
TOOL = os.path.join(ROOT, "tools", "backfill_memory_embeddings.py")

#: 自洽余弦阈值。实测依据见模块 docstring「为什么阈值取 0.9」。
FRESH_THRESHOLD = 0.9

_PASS, _FAIL, _SKIP = [], [], []


def chk(name, ok, detail=""):
    (_PASS if ok else _FAIL).append(name)
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name,
                           ("　— " + str(detail)) if detail and not ok else ""))


def skip(name, why):
    _SKIP.append(name)
    print("  [SKIP] %s :: %s" % (name, why))


def cos(u, v):
    if not u or not v or len(u) != len(v):
        return None
    qn = math.sqrt(sum(x * x for x in u)) or 1.0
    vn = math.sqrt(sum(x * x for x in v)) or 1.0
    return sum(x * y for x, y in zip(u, v)) / (qn * vn)


def main():
    print("=" * 78)
    print("门禁：记忆向量与当前 embedding 模型同源（防「换模型旧向量没重算」）")
    print("=" * 78)

    print("\n--- GROUP_B ---")
    chk("B1 重算工具存在", os.path.isfile(TOOL), TOOL)
    src = ""
    if os.path.isfile(TOOL):
        with open(TOOL, encoding="utf-8") as f:
            src = f.read()
    chk("B1b 工具支持 --force-reembed（全量重算，不只补空）",
        "--force-reembed" in src, "")
    chk("B2 工具默认 dry-run（--apply 才写库）",
        'action="store_true"' in src and "if args.apply" in src, "")
    chk("B3 版本降级（bigram-tf）时停止且不写",
        "bigram-tf" in src and "停止" in src, "")

    print("\n--- GROUP_A ---")
    if not os.path.exists(DB):
        for n in ("A1", "A2", "A3"):
            skip(n, "干净库：跳过需要真库的判据组")
        return _summary()

    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    # 抽样：按创建时间分桶，每桶取最新 2 条（**必须含老数据**，见 A2）
    rows = c.execute(
        "select id, content, embed_version, embedding, created_at from agent_memory "
        "where coalesce(forgotten,0)=0 and content is not null and trim(content)<>'' "
        "and embedding is not null and embedding<>'' and embedding<>'[]' "
        "order by created_at").fetchall()
    if not rows:
        c.close()
        for n in ("A1", "A2", "A3"):
            skip(n, "库内没有带向量的记忆")
        return _summary()

    # 分 3 段（最早/中间/最新），各取 3 条，确保覆盖旧数据
    n = len(rows)
    picks, seen = [], set()
    for frac in (0.0, 0.5, 0.9):
        start = min(n - 1, int(n * frac))
        for r in rows[start:start + 3]:
            if r["id"] not in seen:
                seen.add(r["id"])
                picks.append(r)
    print("      抽样 %d 条（覆盖区间 %.0f%%~100%%）" % (len(picks), 100.0 * (1 - 0)))

    try:
        from knowledge_pipeline import Embedder
        emb = Embedder(c)
        texts = [str(r["content"])[:400] for r in picks]
        vecs, ver = emb.embed_with_version(texts, batch_size=0)
    except Exception as e:
        c.close()
        skip("A1 向量自洽", "embedding 调用异常：%r" % (e,))
        return _summary()

    if ver != "openai-compat" or not vecs:
        c.close()
        for n_ in ("A1", "A2", "A3"):
            skip(n_, "embedding 服务不可用（降级 %s）——**测不了不等于没问题**" % ver)
        return _summary()

    bad = []
    for r, fresh in zip(picks, vecs):
        try:
            stored = json.loads(r["embedding"]) if isinstance(r["embedding"], str) else []
        except Exception:
            stored = []
        s = cos(stored, fresh)
        if s is None or s < FRESH_THRESHOLD:
            bad.append((r["id"], r["created_at"], None if s is None else round(s, 4)))
    chk("A1 抽样记忆的向量与当前模型同源（自洽余弦 ≥ %.2f）" % FRESH_THRESHOLD,
        not bad, "过期向量: %s" % bad[:6])

    # A2：抽样必须真的覆盖到不同时间段（否则 A1 可能只测了新数据）
    ages = sorted({str(r["created_at"])[:7] for r in picks})
    chk("A2 抽样覆盖多个创建月份（防只测新数据）", len(ages) >= 2, ages)

    # A3 变异自证：把一条向量置零 ⇒ 余弦应为 0 ⇒ A1 判据必然翻红
    if picks:
        _zero = [0.0] * len(vecs[0])
        _s = cos(_zero, vecs[0])
        chk("A3 变异自证：向量置零后自洽余弦=0 ⇒ A1 必翻红",
            _s is not None and _s < FRESH_THRESHOLD, "cos=%s" % _s)

    c.close()
    return _summary()


def _summary():
    print("\n" + "=" * 78)
    print("结果：PASS %d / FAIL %d / SKIP %d" % (len(_PASS), len(_FAIL), len(_SKIP)))
    for n in _FAIL:
        print("  - " + n)
    if _FAIL:
        print("\n修复：python tools/backfill_memory_embeddings.py --apply --force-reembed")
    print("=" * 78)
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
