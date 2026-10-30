"""门禁：知识库检索「实质相关」判定（2026-10-28）。

## 被验的真实故障（不是构造的）

用户报：「让 AI 从支持库检索 `perform action` 用法，AI 反馈未检索到，
但知识库明显有 OMG SysML v2 文档介绍具体用法」。

实测确认库中有 **45 个切片**含 `perform action`（doc 799 = OMG SysML v2 规范
7.17.6 节「Perform Action Usages」），检索链路返回 20 条正确命中，
却对外报「知识库未检索到与查询实质相关的内容（词重叠 0/0）」。

## 三层根因（本门禁逐一守护）

| # | 根因 | 判据组 |
|---|---|---|
| 1 | `embed_version='openai-compat'` 不记模型名；默认 provider 换成了 bge-m3，而库里向量是 Qwen3-Embedding-0.6B 生成的 ⇒ 维度同为 1024 但**空间不兼容** ⇒ 余弦 0.02~0.09 ⇒ 全判低置信 | D |
| 2 | provider 110 的 `model_name='bge-m3  '` 带两个尾随空格 ⇒ `/embeddings` 恒 404 ⇒ 静默降级 bigram | D |
| 3 | `tools.py` 弱相关判定三条缺陷：只抽中文 token / overlap 跨语言恒 0 / `max_score<0.5` 在 RRF 分制下恒真 | A~C |

## 运行

    python tools/verify/verify_retrieval_relevance.py

退出码 0 = 全绿；1 = 有红项。
"""

from __future__ import annotations

import os
import re
import sqlite3
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_PASS = 0
_FAIL = 0
_FAILED = []


def check(group: str, name: str, ok: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if ok:
        _PASS += 1
        print(f"  [PASS] {group} {name}")
    else:
        _FAIL += 1
        _FAILED.append(f"{group} {name} :: {detail}")
        print(f"  [FAIL] {group} {name} :: {detail}")


# ══════════════════════════════════════════════════════════════════════
# A. 纯函数：token 抽取必须中英文都取
# ══════════════════════════════════════════════════════════════════════
def group_a():
    from agent.retrieval_relevance import extract_tokens

    # A1 ★ 英文技术术语必须抽得出 token（旧实现返回空集 ⇒ 必判弱）
    toks = extract_tokens("perform action usage")
    check("A1", "★★英文术语抽出 token（旧实现为空集）",
          {"perform", "action", "usage"} <= toks, sorted(toks))

    # A2 中文仍能抽（不能为了修英文把中文丢了）
    toks_zh = extract_tokens("工况口径是什么")
    check("A2", "中文仍抽出 2-4 字 token", len(toks_zh) >= 1, sorted(toks_zh))

    # A3 中英混排都抽
    toks_mix = extract_tokens("perform action 泳道用法")
    check("A3", "中英混排同时抽出两侧",
          "perform" in toks_mix and any(re.match(r"[\u4e00-\u9fa5]", t) for t in toks_mix),
          sorted(toks_mix))

    # A4 ★ 跨语言重叠必须可比（中文问、英文答是常态场景）
    q = extract_tokens("perform action 泳道用法")
    h = extract_tokens("A perform action usage is declared as an action usage")
    check("A4", "★跨语言（英文档）仍能算重叠", len(q & h) >= 2, sorted(q & h))

    # A5 英文停用词不计入（否则 the/and 会把重叠率稀释成假阳性）
    t = extract_tokens("the and of the action")
    check("A5", "英文停用词不计入重叠", "the" not in t and "action" in t, sorted(t))

    # A6 空输入不抛异常
    check("A6", "空输入返回空集（不抛异常）", extract_tokens("") == set())


# ══════════════════════════════════════════════════════════════════════
# B. 纯函数：assess_relevance 判据
# ══════════════════════════════════════════════════════════════════════
def group_b():
    from agent.retrieval_relevance import assess_relevance as A

    # 真实命中：OMG 规范原文 + 实测分数
    HIT = [{"content": "A perform action usage is declared as an action usage (see7.17.2)",
            "vec_score": 0.597, "bm25_score": 12.7, "confidence_level": "中"}]

    # B1 ★★ 用户原始场景必须判"相关"
    r = A("perform action 泳道用法", HIT)
    check("B1", "★★用户原始查询不再判弱（词重叠 0/0 那一次）", r["weak"] is False, r["reason"])

    # B2 纯英文查询（旧实现 `not q_tokens` 恒真）
    r = A("perform action usage", HIT)
    check("B2", "★纯英文查询不再判弱（旧实现必判弱）", r["weak"] is False, r["reason"])

    # B3 相关判定的证据可回显（不能只说"相关"）
    check("B3", "判定带 anchor 与 evidence（可核对）",
          r["anchor"] in ("confidence", "vector", "bm25", "overlap") and r["evidence"].get("hits") == 1,
          f"anchor={r['anchor']} ev={r['evidence']}")

    # B4 ★ 无关结果必须仍判弱（防"为了修假阴性造成假阳性"）
    UNREL = [{"content": "thermal management of battery pack", "vec_score": 0.12,
              "bm25_score": 3.0, "confidence_level": "低"}]
    r = A("perform action usage", UNREL)
    check("B4", "★无关结果仍判弱（无假阳性）", r["weak"] is True, r["reason"])

    # B5 空命中判弱
    check("B5", "空命中判弱", A("x", [])["weak"] is True)

    # B6 高置信优先
    r = A("完全无关的查询 xyz", [{"content": "zzz", "vec_score": 0.0,
                                  "bm25_score": 0.0, "confidence_level": "高"}])
    check("B6", "高置信命中单独即可判相关", r["weak"] is False and r["anchor"] == "confidence")

    # B7 强 BM25 单独可判相关（与 rag._low_conf_suppressed 同口径）
    r = A("xyz", [{"content": "zzz", "vec_score": 0.0,
                   "bm25_score": 55.0, "confidence_level": "低"}])
    check("B7", "强 BM25(≥40) 单独可判相关", r["weak"] is False and r["anchor"] == "bm25")

    # B8 阈值必须可配置（不写死）
    r = A("perform action", HIT, conf_mid=0.99)
    check("B8", "conf_mid 可配置（0.99 时该证据失效）", r["anchor"] != "vector", r["anchor"])


# ══════════════════════════════════════════════════════════════════════
# C. ★★ 变异自证：把判据改回旧实现 ⇒ 对应判据必须翻红
# ══════════════════════════════════════════════════════════════════════
def group_c():
    from agent import retrieval_relevance as RR

    HIT = [{"content": "A perform action usage is declared as an action usage",
            "vec_score": 0.597, "bm25_score": 12.7, "confidence_level": "中", "score": 0.064}]

    # C1 ★★ 复原旧判据（只抽中文 + RRF 分阈值 0.5）⇒ B1 必须翻红
    def old_style(query, hits):
        q_tokens = set(re.findall(r"[\u4e00-\u9fa5]{2,4}", query or ""))
        hit_text = " ".join([(h.get("content") or "")[:500] for h in hits])
        hit_tokens = set(re.findall(r"[\u4e00-\u9fa5]{2,4}", hit_text))
        overlap = q_tokens & hit_tokens
        ratio = len(overlap) / max(len(q_tokens), 1)
        max_score = max([h.get("score") or 0 for h in hits], default=0)
        return {"weak": (not hits) or (not q_tokens) or (ratio < 0.2 and max_score < 0.5)}

    judge = lambda f: f("perform action 泳道用法", HIT)["weak"] is False
    check("C1", "★★变异自证：复原旧判据 ⇒ B1/B2 判据翻红",
          judge(RR.assess_relevance) and not judge(old_style),
          f"新={judge(RR.assess_relevance)} 旧={judge(old_style)}")

    # C2 ★ 反向自证：真实现对"无关结果"必须判弱（防判据恒绿）
    UNREL = [{"content": "unrelated thermal text", "vec_score": 0.05,
              "bm25_score": 1.0, "confidence_level": "低"}]
    check("C2", "★反向自证：真实现对无关结果判弱（防恒绿）",
          RR.assess_relevance("perform action", UNREL)["weak"] is True)

    # C3 ★ RRF 分不能当相似度阈值 —— 用真实量级验证该结论
    #   1/(60+1)*2 = 0.0328 ⇒ 任何 <0.5 的阈值都恒真
    rrf_max = 2.0 / (60 + 1)
    check("C3", "★RRF 分理论上限 < 0.5（旧阈值恒真的数学依据）",
          rrf_max < 0.5, f"2/(60+1)={rrf_max:.4f}")

    # C4 新判据不读 score 字段（不把排名分当相似度）
    src = open(os.path.join(_ROOT, "agent", "retrieval_relevance.py"), encoding="utf-8").read()
    body = "\n".join(l for l in src.split("\n") if not l.strip().startswith("#"))
    n_score = len(re.findall(r'\.get\(\s*["\']score["\']', body))
    check("C4", "★判定不读 RRF 分 score（避免再次恒真）", n_score == 0, f"出现 {n_score} 次")


# ══════════════════════════════════════════════════════════════════════
# D. ★★ 数据链路：embedding provider 与库内向量必须同源
# ══════════════════════════════════════════════════════════════════════
def group_d():
    from core.config import DB_PATH

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # D1 默认 embedding provider 的 model_name 不得带首尾空白
    rows = conn.execute(
        "SELECT id, name, model_name, base_url FROM llm_providers "
        "WHERE model_type='embedding' AND status='active'").fetchall()
    check("D1", "存在 embedding provider", len(rows) >= 1, f"{len(rows)} 个")
    blanks = [f"[{r['id']}]{r['model_name']!r}" for r in rows
              if r["model_name"] and r["model_name"] != r["model_name"].strip()]
    check("D1b", "★embedding model_name 无首尾空白（否则 /embeddings 恒 404）",
          not blanks, str(blanks))

    default = [r for r in rows if True]
    dflt = conn.execute(
        "SELECT id, name, model_name FROM llm_providers "
        "WHERE model_type='embedding' AND status='active' AND is_default=1 "
        "ORDER BY id LIMIT 1").fetchone()
    check("D2", "存在默认 embedding provider", dflt is not None,
          str([dict(r) for r in default[:2]]))

    # D3 ★★ 核验：默认 provider 生成的查询向量，与库中已知相关切片的余弦
    #    必须"明显相关"（>= conf_mid）。这是"换模型静默失效"的唯一可靠判据。
    try:
        from knowledge_pipeline import Embedder
        from agent.retrieval_relevance import CONF_MID_DEFAULT
        e = Embedder(conn)
        qv, ver = e.embed_with_version(["perform action usage"])
        if not qv or ver == "bigram-tf":
            check("D3", "★★默认 provider 可用且返回真向量", False,
                  f"version={ver!r}（fallback 到 bigram ⇒ 说明 API 不通）")
        else:
            tgt = conn.execute(
                "SELECT id, embedding FROM document_chunks "
                "WHERE lower(content) LIKE '%perform action usage%' "
                "AND embed_version=? AND embedding != '[]' LIMIT 3", (ver,)).fetchall()
            check("D3", "★库中存在同版本的相关切片（版本口径一致）", len(tgt) > 0,
                  f"version={ver!r} 命中 {len(tgt)} 条")
            if tgt:
                import json
                import math

                def _cos(a, b):
                    na = math.sqrt(sum(x * x for x in a)) or 1.0
                    nb = math.sqrt(sum(x * x for x in b)) or 1.0
                    return sum(x * y for x, y in zip(a, b)) / (na * nb)

                sims = []
                for t in tgt:
                    tv = json.loads(t["embedding"])
                    if len(tv) == len(qv[0]):
                        sims.append(_cos(qv[0], tv))
                best = max(sims) if sims else 0.0
                check("D3b", "★★默认 provider 与库内向量同源（余弦 ≥ conf_mid）",
                      best >= CONF_MID_DEFAULT,
                      f"最高余弦 {best:.4f} < {CONF_MID_DEFAULT}"
                      f" ⇒ 当前 provider 与入库模型不一致，检索会静默失效")
                check("D3c", "向量维度匹配", bool(sims), f"query dim={len(qv[0])}")
    except Exception as exc:
        check("D3", "★★embedding 链路可跑通", False, repr(exc))

    # D4 ★ 防复发：库内 embed_version 不记录模型名，换模型会静默不兼容
    #    ⇒ 至少要求"默认 provider 与库内多数向量的来源模型一致"这件事**可被查出**。
    #    这里落一条结构性判据：默认 provider 必须与入库模型同名（当前由 D3b 的
    #    余弦实测承担），并登记该设计缺陷到文档。
    docp = os.path.join(_ROOT, "docs", "持续自主执行能力-最终方案设计-20261028.md")
    # 该缺陷登记在检索问题专档（下一行），此处只断言存在登记文件
    reg = os.path.join(_ROOT, "docs", "知识库检索失效-根因与修复-20261028.md")
    check("D4", "★换模型静默失效的风险已书面登记", os.path.exists(reg), reg)

    conn.close()


# ══════════════════════════════════════════════════════════════════════
# E. ★★ 端到端：真实工具调用必须返回可用条文
# ══════════════════════════════════════════════════════════════════════
def group_e():
    try:
        from agent.pipeline_parts.tools import ToolMixin
        from agent.rag import GraphRAG

        obj = ToolMixin()
        obj.rag = GraphRAG()
        r = obj._exec_tool_call("graph_retrieve", {"query": "perform action 泳道用法"})
    except Exception as exc:
        check("E", "真实工具调用 graph_retrieve 可执行", False, repr(exc))
        return

    check("E1", "真实工具调用成功", r.get("ok") is True, str(r)[:120])
    check("E2", "★★不再标记 weak（用户原始场景）", not r.get("weak"),
          f"weak={r.get('weak')} result={str(r.get('result'))[:80]}")
    rel = r.get("relevance") or {}
    check("E3", "判定锚点是可核对的内容证据",
          rel.get("anchor") in ("confidence", "vector", "bm25", "overlap"), str(rel)[:120])

    txt = str(r.get("result") or "")
    # E4 ★★ 必须真的给出条文内容，而不是 3 条 60 字符摘要
    check("E4", "★★返回内容含规范原文（perform action usage）",
          "perform action usage" in txt.lower(), txt[:150])
    check("E5", "★返回内容标注来源文档与章节",
          "OMG" in txt and "7.17" in txt, txt[:200])
    check("E6", "★★返回内容长度足够回答「用法」（> 800 字，旧实现只有 180）",
          len(txt) > 800, f"实际 {len(txt)} 字")
    check("E7", "返回条数可核对", int(r.get("hit_count") or 0) > 0, str(r.get("hit_count")))


def main() -> int:
    print("=" * 74)
    print("门禁：知识库检索「实质相关」判定 + embedding 同源（2026-10-28）")
    print("=" * 74)
    for name, fn in (("A. token 抽取（中英文）", group_a),
                     ("B. assess_relevance 判据", group_b),
                     ("C. 变异自证 / 反向自证", group_c),
                     ("D. embedding provider 与库内向量同源", group_d),
                     ("E. 真实工具调用端到端", group_e)):
        print(f"\n── {name} ──")
        try:
            fn()
        except Exception as exc:
            check(name[0], f"{name} 未预期异常", False, repr(exc))

    total = _PASS + _FAIL
    print("\n" + "=" * 74)
    if _FAIL:
        print(f"结果：{_PASS}/{total} 通过，{_FAIL} 项失败")
        for f in _FAILED:
            print(f"  ✗ {f}")
        print("=" * 74)
        return 1
    print(f"结果：{_PASS}/{total} 全部通过")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    sys.exit(main())
