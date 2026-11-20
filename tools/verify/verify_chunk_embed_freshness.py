# CI-OPTIONAL: [local] tier=local（见 tools/gates_manifest.json）：需真实 embedding 服务（内网 192.168.100.202:8004）+ 真库 mbse.db，CI runner 无该组合 ⇒ 本机跑。判据：document_chunks 向量与当前模型同源（按 chunking.py 的 doc_title+section+content 口径重建 embed_text；比率判据 >=85% 达标，容忍个别离群）。2026-11-16 新增：与 verify_memory_embed_freshness 同批防同类缺陷在文档侧重演。改法须同步改清单 tier 并接线，不允许只删这行
# -*- coding: utf-8 -*-
"""verify_chunk_embed_freshness.py —— 文档 chunk 向量必须与当前 embedding 模型同源。

## 为什么加这条（2026-11-16，与记忆向量同批）

记忆侧发现「换过 embedding 模型但旧向量没重算」使召回恒为 0（记忆评测
27.3% → 63.6%）。同一份数据底座上，`document_chunks`（主文档检索，7069 条）
必须同样受保护 —— 它坏了影响面更大（所有知识库问答）。

## ⚠️ 两个必须写清的坑（都亲自踩过，都会产出**假过期**）

### 坑 1：比对输入必须与入库**完全一致**
入库的 embedding 不是对 `content` 算的，而是对
`embed_text = f"{doc_title} {section} {content}"`（Title-Enriched v2，
见 `knowledge_pipeline/chunking.py:149/162/188`）。
用 `content` 去比，实测得到 0.45~0.48 的"过期"假象；
换成 `doc_title + content` 就升到 0.89~0.90 —— **差的那 0.1 就是 section**。
⇒ 本门禁严格按 `chunking.py` 的拼接口径重建 embed_text。

### 坑 2：不能只看单个 chunk 的孤例
初版抽样发现 #14588 = 0.48 就判"整批过期"，但同文档 #14587 = 0.937。
差别只在 `section` 字段是否为空 ⇒ 是输入口径问题，不是数据问题。
⇒ 本门禁按**月份分层 + 每份文档多抽**，并报告分布而不是单一数值。

## 阈值
`FRESH_THRESHOLD = 0.85`。依据（实测）：
  · 同模型同输入：0.90 ~ 1.0000（个别 0.89 因服务端浮点微抖动）
  · 换模型（记忆侧实测）：|cos| < 0.03
0.85 落在两簇之间且偏保守 —— **宁可漏报也不制造假过期**
（假过期会诱导人去重算 7069 条 × 数千字，代价极高且收益为零）。

用法： ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_chunk_embed_freshness.py
退出码：0 全绿（无库/无服务时 SKIP）／1 有失败
"""
import json
import math
import os
import sqlite3
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
DB = os.environ.get("MBSE_GATE_DB") or os.environ.get("MBSE_DB_PATH") or os.path.join(ROOT, "mbse.db")

FRESH_THRESHOLD = 0.85
#: 每月抽几条。比率判据需要足够样本才稳（6 条算比率太抖）⇒ 每月 5 条、总量 ~10。
#: 仍远小于全量 7069 条，评测在秒级完成。
PER_MONTH = 5
#: 只抽样**长度达标**的 chunk。
#: ⚠️ 短文本的 embedding 天然不稳定：实测测试文件 `ui_upload_check.txt`
#: （内容仅 `【短文档】ui_upload_check.txt / UPLOAD_MARKER_ok / line2`）
#: 在各种拼接口径下自洽度都是 0.68~0.81，**永远不会达到 0.85**。
#: 这不是"向量过期"，是短文本语义信息太少 ⇒ 微小输入差异即导致方向偏移。
#: ⇒ 抽样时排除过短文本，否则门禁会**永久假红**，逼人把阈值一路下调直到失效。
MIN_CONTENT_CHARS = 100

#: **比率判据**（不用"每条都必须达标"）。
#: 标定依据（随机 40 条实测，seed=42）：
#:   min=0.746  p5=0.869  p25=0.956  中位=0.972  均值=0.956  max=1.000
#: ⇒ 分布是**双峰**的：绝大多数 0.95+（健康），约 5% 落在 0.75~0.87（零散离群）。
#:
#: 为什么用比率而不是单点：
#:   · 单点判据对**个别脏数据**过敏（如某 chunk 入库后被增量改过、embedding 没重算）
#:     ⇒ 门禁永久红 ⇒ 逼人把阈值一路下调 ⇒ 判据失效（本仓反复踩过的路）
#:   · 而"换过 embedding 模型"是**系统性**的：实测记忆侧整批 |cos| < 0.03，
#:     文档侧若真换模型，达标率会从 95% 掉到接近 0
#: ⇒ 判"达标率 ≥ MIN_PASS_RATIO"，既能抓住系统性劣化，又不追责个别离群。
MIN_PASS_RATIO = 0.85

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


def rebuild_embed_text(row):
    """按 `knowledge_pipeline/chunking.py` 的口径重建 embed_text。

        embed_text = f"{doc_title} {section} {content}".strip()

    ⚠️ **三个字段缺一不可**：漏掉 section 会得到 0.45~0.48 的假过期（实测）。
    `doc_title` 取 `source_doc` 去扩展名（入库时 doc_title 来自文件名/识别标题）。
    """
    doc = str(row["source_doc"] or "")
    for ext in (".pdf", ".docx", ".doc", ".pptx", ".xlsx", ".md", ".txt"):
        if doc.lower().endswith(ext):
            doc = doc[: -len(ext)]
            break
    sec = str(row["section"] or "")
    con = str(row["content"] or "")
    txt = ("%s %s %s" % (doc, sec, con)).strip()
    return txt or con


def main():
    print("=" * 78)
    print("门禁：文档 chunk 向量与当前 embedding 模型同源")
    print("=" * 78)

    if not os.path.exists(DB):
        skip("A1 分层抽样自洽度", "干净库")
        skip("A2 抽样覆盖多个月份", "干净库")
        skip("A3 变异自证", "干净库")
        return _summary()

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    cols = {r[1] for r in conn.execute("PRAGMA table_info(document_chunks)")}
    if "embedding" not in cols:
        conn.close()
        skip("A1 分层抽样自洽度", "document_chunks 无 embedding 列")
        return _summary()

    rows = conn.execute(
        "select id, content, section, source_doc, embedding, created_at "
        "from document_chunks where embedding is not null and embedding<>'' "
        "and embedding<>'[]' order by created_at").fetchall()
    n_all = len(rows)
    # 排除过短文本（短文本向量天然不稳定，测它只会制造假红 —— 见 MIN_CONTENT_CHARS）
    rows = [r for r in rows if len(str(r["content"] or "")) >= MIN_CONTENT_CHARS]
    n_short = n_all - len(rows)
    if n_short:
        print("      已排除 %d 条短文本（< %d 字符，向量天然不稳定，不参与判定）"
              % (n_short, MIN_CONTENT_CHARS))
    if not rows:
        conn.close()
        skip("A1 分层抽样自洽度", "库内无长度达标的带向量 chunk")
        return _summary()

    buckets = defaultdict(list)
    for r in rows:
        buckets[str(r["created_at"])[:7]].append(r)
    picks = []
    for m in sorted(buckets):
        items = buckets[m]
        # 均匀取样 PER_MONTH 条（首/尾必含，中间均分）——
        # 初版写死 {0, len//2, len-1} 三个点，改 PER_MONTH 也不生效 ⇒ 样本恒 3 条/月，
        # 比率判据在 6 条样本上抖动极大（1 条不达标就 83.3%）。
        n = len(items)
        if n <= PER_MONTH:
            idxs = list(range(n))
        else:
            idxs = sorted({round(i * (n - 1) / (PER_MONTH - 1)) for i in range(PER_MONTH)})
        picks.extend(items[i] for i in idxs)

    try:
        from knowledge_pipeline import Embedder
        emb = Embedder(conn)
        texts = [rebuild_embed_text(p) for p in picks]
        vecs, ver = emb.embed_with_version(texts, batch_size=0)
    except Exception as e:
        conn.close()
        skip("A1 分层抽样自洽度", "embedding 调用异常：%r" % (e,))
        return _summary()

    if ver != "openai-compat" or not vecs:
        conn.close()
        for n in ("A1", "A2", "A3"):
            skip(n, "embedding 服务不可用（降级 %s）—— 测不了不等于没问题" % ver)
        return _summary()

    bad, scores = [], []
    for p, fresh in zip(picks, vecs):
        try:
            stored = json.loads(p["embedding"]) if isinstance(p["embedding"], str) else []
        except Exception:
            stored = []
        s = cos(stored, fresh)
        scores.append(s)
        if s is None or s < FRESH_THRESHOLD:
            bad.append((p["id"], str(p["created_at"])[:10],
                        None if s is None else round(s, 4)))
    valid = [s for s in scores if s is not None]
    avg = sum(valid) / len(valid) if valid else 0
    ratio = (len(valid) - len(bad)) / len(valid) if valid else 0
    print("      抽样 %d 条（%d 个月份）｜均值 %.4f｜最小 %.4f｜达标率 %.1f%%"
          % (len(picks), len(buckets), avg,
             min(valid) if valid else 0, 100 * ratio))
    if bad:
        print("      [info] 未达标 %d 条（个别离群，非系统性）：%s"
              % (len(bad), bad[:5]))
    chk("A1 抽样 chunk 的向量与当前模型同源（达标率 ≥ %.0f%%，单条阈值 %.2f）"
        % (100 * MIN_PASS_RATIO, FRESH_THRESHOLD),
        ratio >= MIN_PASS_RATIO,
        "达标率 %.1f%% < %.0f%%，未达标 %s"
        % (100 * ratio, 100 * MIN_PASS_RATIO, bad[:6]))

    chk("A2 抽样覆盖多个月份（防只测最新数据）", len(buckets) >= 1 and len(picks) >= 3,
        "月份=%d 抽样=%d" % (len(buckets), len(picks)))

    if picks and vecs:
        # A3 变异自证①：单条向量置零 ⇒ 余弦=0 ⇒ 必然低于单条阈值
        zero = [0.0] * len(vecs[0])
        s0 = cos(zero, vecs[0])
        chk("A3 变异自证：单条向量置零 ⇒ 自洽余弦=0 ⇒ 必判不达标",
            s0 is not None and s0 < FRESH_THRESHOLD, "cos=%s" % s0)

        # A3b 变异自证②：**全部**置零（模拟"换过模型"）⇒ 达标率 0% ⇒ 比率判据必翻红。
        #   这条才是比率判据的牙齿：证明它抓的是**系统性劣化**，
        #   而不是靠"某一条恰好不过"来报警。
        zero_scores = [cos(zero, v) for v in vecs]
        z_valid = [s for s in zero_scores if s is not None]
        z_ratio = (sum(1 for s in z_valid if s >= FRESH_THRESHOLD) / len(z_valid)
                   if z_valid else 1.0)
        chk("A3b 变异自证：整批置零（模拟换模型）⇒ 达标率 0% ⇒ 比率判据必翻红",
            z_ratio < MIN_PASS_RATIO, "ratio=%.2f" % z_ratio)

    conn.close()
    return _summary()


def _summary():
    print("\n" + "=" * 78)
    print("结果：PASS %d / FAIL %d / SKIP %d" % (len(_PASS), len(_FAIL), len(_SKIP)))
    for n in _FAIL:
        print("  - " + n)
    if _FAIL:
        print("\n修复：tools/reembed_chunks.py（全量重嵌）/ tools/reembed_degraded.py（只补降级）")
    print("=" * 78)
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
