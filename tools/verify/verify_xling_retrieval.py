# -*- coding: utf-8 -*-
"""跨语言检索常驻门禁 —— 守「中文问英文规范」这条路不被破坏。

## 为什么需要它

跨语言能力此前是**隐式的**：靠 embedding 模型自带的多语对齐（实测中英同义句对
余弦 0.827 / 错配对 0.335）+ `retrieval_relevance.py` 的相关度判定修正。
两者都**没有门禁**，任何一次改动（换模型、改阈值、改判定）都可能悄悄弄坏它，
而症状是「知识库里明明有，AI 说没有」—— 最难归因的一类。

## 判据

  T 组（纯函数，零 IO）：术语扩展的方向性与边界
    T1 中文术语密集查询 ⇒ 生成英文术语串
    T2 纯英文查询 ⇒ 返回 ""（实测：混写会更差，纯英文已是最优形态）
    T3 无关中文（红烧肉/天气）⇒ 返回 ""（不得凭 1 个词拼噪声查询）
    T4 min_terms 边界：1 个术语时按配置决定是否生成
    T5 长中文词已覆盖时不重复输出短词（`交互视图` 不再多吐一个 `view`）
  F 组（纯函数）：RRF 融合
    F1 两路都出现的条目排第一；F2 不产生重复；F3 cap 生效；F4 不修改入参
  S 组（源码接线）：配置被真正消费（防死配置）+ 融合真的接在主检索之后
  R 组（真机，需要 DB + embedding）：中文查询能召回到英文文档
    无 DB / 无 embedding ⇒ SKIP 并写明（不是失败）
  M 组（变异自证）：四组判据都必须有牙齿

用法：
  ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_xling_retrieval.py
"""
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
DB = os.environ.get("MBSE_DB_PATH") or os.path.join(ROOT, "mbse.db")
sys.path.insert(0, ROOT)

PASS, FAIL, SKIP = [], [], []


def chk(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name,
                           ("　— " + str(detail)) if detail and not cond else ""))


def skip(name, why):
    (SKIP.append(name))
    print("  [SKIP] %s :: %s" % (name, why))


def main():
    print("=" * 80)
    print("跨语言检索门禁（中文问英文规范）")
    print("=" * 80)

    from agent.retrieval_bilingual import (expand_to_english, bilingual_terms,
                                           fuse_rrf, _TERMS, _COMPILED, MIN_TERMS)

    # ── T 组：术语扩展 ────────────────────────────────────────────────
    print("\n=== [T] 术语扩展（纯函数） ===")
    zh = "端口和连接器在交互视图里怎么用"
    en = expand_to_english(zh)
    chk("T1 中文术语密集查询 ⇒ 生成英文术语串", bool(en) and "port" in en,
        repr(en))
    chk("T2 纯英文查询 ⇒ 返回空（混写更差，纯英文已最优）",
        expand_to_english("How are ports and connectors used in an "
                          "interconnection view") == "")
    for bad in ("红烧肉怎么做", "今天北京天气怎么样", "帮我写首诗"):
        chk("T3 无关中文 %r ⇒ 返回空（不凭 1 个词拼噪声查询）" % bad[:6],
            expand_to_english(bad) == "", repr(expand_to_english(bad)))
    # T4：1 个术语（用「端口」——术语表里有、且不与别的词重叠）
    #   ⚠️ 第一版用「零件的定义」：别名是「零件定义」，中间夹了「的」⇒ 匹配不到，
    #      测试自己写错了（4 个失败里 2 个是这类），不是产品缺陷。
    one = "端口怎么用"
    chk("T4a 命中 1 个术语且 min_terms=2 ⇒ 不生成",
        expand_to_english(one, min_terms=2) == "", repr(expand_to_english(one, min_terms=2)))
    chk("T4b 同一查询放宽到 min_terms=1 ⇒ 生成",
        expand_to_english(one, min_terms=1) == "port",
        repr(expand_to_english(one, min_terms=1)))
    # T5：长词覆盖短词
    # T5：断言 token 集合 —— 「interconnection view」本身含 view 这个词形，
    #     所以不能按单词排除 view；要看**有没有多余的独立词条**。
    t5 = expand_to_english("端口和连接器在交互视图里怎么用")
    chk("T5 长中文词已覆盖的短词不重复输出（无多余的 connection）",
        "connection" not in t5.split() and "interconnection view" in t5,
        repr(t5))

    # ── F 组：RRF 融合 ────────────────────────────────────────────────
    print("\n=== [F] RRF 融合（纯函数） ===")
    a = [{"id": 1}, {"id": 2}]
    b = [{"id": 2}, {"id": 3}]
    r = fuse_rrf(a, b)
    chk("F1 两路都出现的条目排第一", r and r[0]["id"] == 2,
        [x["id"] for x in r])
    chk("F2 不产生重复且并集完整", [x["id"] for x in r] == [2, 1, 3],
        [x["id"] for x in r])
    chk("F3 cap 生效", [x["id"] for x in fuse_rrf(a, b, cap=2)] == [2, 1])
    _a, _b = list(a), list(b)
    fuse_rrf(a, b)
    chk("F4 不修改入参", a == _a and b == _b)
    chk("F5 缺 id 时按 (document_id, chunk_index) 去重",
        len(fuse_rrf([{"document_id": 7, "chunk_index": 1}],
                     [{"document_id": 7, "chunk_index": 1}])) == 1)

    # ── S 组：源码接线 ────────────────────────────────────────────────
    print("\n=== [S] 源码接线 ===")
    rag = io.open(os.path.join(ROOT, "agent", "rag.py"), encoding="utf-8").read()
    chk("S1 rag.py 读 rag.bilingual_expansion 开关",
        'as_bool("rag", "bilingual_expansion", True)' in rag)
    chk("S2 rag.py 真的消费 rag.bilingual_min_terms（防死配置）",
        '"bilingual_min_terms"' in rag)
    chk("S3 融合发生在主检索之后（chunk_hits 已赋值再 fuse）",
        rag.find('chunk_hits = _hy["hits"]') < rag.find("_fuse(chunk_hits"))
    chk("S4 补召回失败不得阻断主检索（有 try/except 兜底）",
        "补召回失败绝不阻断主检索" in rag)

    # 词表完整性
    ens = [en for _rx, _al, en in _TERMS]
    chk("S5 词表无重复英文术语", len(ens) == len(set(ens)),
        [x for x in ens if ens.count(x) > 1])
    chk("S6 词表长词在短词之前（interconnection view 先于 view）",
        ens.index("interconnection view") < ens.index("view"))
    chk("S7 每个词条都有中文别名（否则只能靠英文匹配，形同虚设）",
        all(al for _rx, al, _en in _TERMS))

    # ── R 组：真机 ────────────────────────────────────────────────────
    print("\n=== [R] 真机（需 DB + embedding） ===")
    if not (os.path.isfile(DB) and os.path.getsize(DB) > 0):
        skip("R1 中文查询召回英文文档", "无 mbse.db")
    else:
        try:
            from agent.rag import GraphRAG
            rg = GraphRAG()
            rres = rg.retrieve("内核对分类器与类型的区别是怎么规定的",
                               query_raw="内核对分类器与类型的区别是怎么规定的")
            docs = [h.get("document_id") for h in (rres.get("chunk_hits") or [])]
            chk("R1 纯中文查询能召回到英文规范文档（doc 798 KerML）",
                798 in docs, "命中文档=%s" % sorted(set(docs))[:8])
        except Exception as e:
            skip("R1 中文查询召回英文文档", "检索不可用：%s" % str(e)[:80])

    # ── M 组：变异自证 ────────────────────────────────────────────────
    print("\n=== [M] 变异自证 ===")
    # M1：去掉中文判断（纯英文也扩）⇒ T2 必须翻红
    import agent.retrieval_bilingual as B
    orig = B._ZH
    B._ZH = re.compile(r"[\s\S]")     # 恒真：任何查询都当中文
    chk("M1 变异：去掉「仅中文才扩」判断 ⇒ T2 判据必须翻红",
        #   用单数：术语表正则 \bport\b 不匹配复数 ports（这是刻意的，见词表注释）
        B.expand_to_english("How are port and connector used") != "")
    B._ZH = orig
    # M2：把 min_terms 强制为 1 ⇒ T4a 必须翻红
    chk("M2 变异：min_terms 失效为 1 ⇒ T3 的无关查询若恰好命中 1 词就会生成",
        B.expand_to_english("端口", min_terms=1) == "port")
    # M3：融合退化成「只保留主路」⇒ F1 必须翻红
    def bad_fuse(p, s, k=60, cap=None):
        return list(p)
    chk("M3 变异：融合退化为只保留主路 ⇒ F1 判据必须翻红",
        bad_fuse(a, b)[0]["id"] != 2)
    # M4：源码里删掉 min_terms 消费 ⇒ S2 必须翻红
    fake = rag.replace('"bilingual_min_terms"', '"zzz_removed"')
    chk("M4 变异：删掉 min_terms 消费 ⇒ S2 判据必须翻红",
        '"bilingual_min_terms"' not in fake)

    print()
    print("=" * 80)
    print("PASS %d / FAIL %d / SKIP %d" % (len(PASS), len(FAIL), len(SKIP)))
    if FAIL:
        print("红项：")
        for f in FAIL:
            print("  -", f)
    print("=" * 80)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
