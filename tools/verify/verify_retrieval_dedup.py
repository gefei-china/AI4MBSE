# -*- coding: utf-8 -*-
"""检索结果去重 / 来源名归一化 / 零召回关键词重试 门禁（2026-11-01）。

## 背景（全部来自生产实测，不是假设）
`mbse.db` 里 id=847 与 id=848 是**同一份 PDF 被导入两次**：
同名、同 `file_size=513027`、55 个 chunk **逐字完全相同（逐条比对 0 处差异）**，
但 `source_doc` 一个带 document_id 前缀、一个不带。后果：

    注入 5 条，文档分布 = {847_xxx: 2, xxx: 2, 热管理.md: 1}
    ⇒ 5 槽里 4 槽是同一份物理文件，界面看着引了 3 个文档、实际只有 2 个真实来源。

## 判据（每条都带**变异自证**：把被保护的行为改坏，门禁必须翻红）
  [D1] 跨文档正文逐字相同 ⇒ 判副本，只留一条
  [D2] 同文档内正文相同 ⇒ **保留**（文档里真有两处一样的话，是两个位置）
  [D3] 短正文（< min_chars）不参与去重（避免误合并套话短片段）
  [D4] 归一化：前缀数字 == document_id ⇒ 剥掉 `{id}_`
  [D5] 归一化零误伤：前缀数字 ≠ document_id ⇒ **不剥**（如 `2024_年度报告.pdf`）
  [D6] 归一化**不依赖本批对照物**（去重删掉副本后仍要生效 —— 踩过的坑）
  [D7] 端到端：真实重复副本场景下，返回结果里不出现同一份内容两次
  [K1] 关键词抽取只收实词且长度 ≥ 3
  [K2] 习用语被排除（`系统对` 是 jieba 把"系统"+"对"粘成的 l 类，用它召回 20 条噪声）
  [K3] 2 字泛词被排除（`要求` 参与拼接会让命中从 0 涨到 19 条噪声）
  [K4] 无高区分度词 ⇒ 不产生重试候选（域外问题不该被"重试"救活）
  [M1] 变异：把去重判据改成"同文档也删" ⇒ D2 必须翻红
  [M2] 变异：把归一化改成"无条件剥前缀" ⇒ D5 必须翻红

用法： ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_retrieval_dedup.py
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

from agent.retrieval_dedup import (dedupe_and_normalize, dedupe_exact,   # noqa: E402
                                   normalize_doc_names)
from agent.query_keywords import high_signal_terms, retry_candidates     # noqa: E402

_FAILS = []


def check(name, cond, detail=""):
    tag = "OK  " if cond else "FAIL"
    if not cond:
        _FAILS.append(name)
    print(f"[{tag}] {name}" + (f"  — {detail}" if detail else ""))


def mk(doc_id, doc, idx, text):
    return {"document_id": doc_id, "source_doc": doc,
            "chunk_index": idx, "content": text}


LONG_A = ("a) 工具运行时，对于网络传输数据，应当对网络会话加密，避免敏感信息被网络嗅探；"
          "b) 工具应当保证运维安全，禁止配置弱口令，高位权限，明文账户密码等信息；"
          "c) 工具应具备审计能力，记录所有用户与大语言模型的交互日志、数据上传/下载日志，"
          "以便进行安全回溯；d) 工具应通过白名单、指令授权等途径，确保仅能执行用户权限"
          "范围内的本地系统命令，防止出现资源误删除等非预期结果。")
LONG_B = ("c) 工具应保证软件组件安全，应当通过漏洞安全检查和开源许可安全检查，"
          "不使用存在安全风险的组件和第三方组件；d) 工具应具备完善的异常处理机制，"
          "在出现错误时应平滑降级，提示用户并提供恢复建议，不应导致整个应用不可用。")

print("=" * 70)
print("[A] 去重：跨文档副本 vs 同文档多位置")
print("=" * 70)

# 生产真实形态：847 带前缀、848 不带前缀，正文逐字相同
COPY_A = mk(847, "847_技术要求.pdf", 45, LONG_A)
COPY_B = mk(848, "技术要求.pdf", 45, LONG_A)      # 同一份文件的第二个副本
SAME_DOC = mk(847, "847_技术要求.pdf", 60, LONG_A)  # 同文档不同 chunk，正文恰好相同

kept, st = dedupe_exact([COPY_A, COPY_B, mk(1, "其他文档.md", 9, LONG_B)])
check("D1 跨文档正文相同 → 只留一条", len(kept) == 2 and st["dropped"] == 1,
      f"kept={len(kept)} dropped={st['dropped']}")

kept2, st2 = dedupe_exact([COPY_A, SAME_DOC])
check("D2 同文档内正文相同 → 保留（两个位置都是证据）",
      len(kept2) == 2 and st2["dropped"] == 0, f"kept={len(kept2)}")

SHORT_A = mk(847, "847_x.pdf", 1, "本章小结")
SHORT_B = mk(848, "x.pdf", 1, "本章小结")
kept3, st3 = dedupe_exact([SHORT_A, SHORT_B], min_chars=80)
check("D3 短正文不参与去重", len(kept3) == 2 and st3["dropped"] == 0,
      f"kept={len(kept3)}")

check("D3b 阈值可调：min_chars=2 时同样的短句会被判重",
      dedupe_exact([SHORT_A, SHORT_B], min_chars=2)[1]["dropped"] == 1)

print()
print("=" * 70)
print("[B] 来源名归一化：只剥真正是 document_id 的前缀")
print("=" * 70)

norm, stn = normalize_doc_names([mk(847, "847_技术要求.pdf", 1, LONG_A)])
check("D4 前缀 == document_id → 剥掉", norm[0]["source_doc"] == "技术要求.pdf",
      f"got={norm[0]['source_doc']!r}")

# 合法文件名自带数字前缀，且与 document_id 不一致 ⇒ 绝不能剥
norm2, _ = normalize_doc_names([mk(900, "2024_年度报告.pdf", 1, LONG_A)])
check("D5 前缀 ≠ document_id → 不剥（零误伤）",
      norm2[0]["source_doc"] == "2024_年度报告.pdf", f"got={norm2[0]['source_doc']!r}")

# 踩过的坑：去重先删掉副本后，本批里已无同名对照物，归一化仍须生效
only_one = [mk(847, "847_技术要求.pdf", 45, LONG_A)]
after_dd, _ = dedupe_and_normalize([COPY_A, COPY_B])
check("D6 去重后本批只剩带前缀的一份，归一化仍生效",
      len(after_dd) == 1 and after_dd[0]["source_doc"] == "技术要求.pdf",
      f"got={[h['source_doc'] for h in after_dd]}")

print()
print("=" * 70)
print("[C] 端到端：5 个槽不应被同一份文件占满")
print("=" * 70)

# 复刻生产实测：847/848 各贡献 2 条同内容，另有 2 个其它文档各 1 条
E2E = [
    mk(847, "847_技术要求.pdf", 45, LONG_A),
    mk(847, "847_技术要求.pdf", 47, LONG_B),
    mk(848, "技术要求.pdf", 45, LONG_A),
    mk(848, "技术要求.pdf", 47, LONG_B),
    mk(1, "热管理知识库.md", 3, "纯电汽车热管理建模场景，涵盖电池热失控与座舱空调。"),
    mk(2, "广汽方法论.docx", 5, "MagicGrid 方法论：系统域、需求域、验证域三域五关注点。"),
]
e2e, est = dedupe_and_normalize(E2E)
docs = {}
for h in e2e:
    docs[h["source_doc"]] = docs.get(h["source_doc"], 0) + 1
check("D7 去重后真实文档数 = 3（改前按名字算是 4 个、实际只 2 个来源）",
      len(docs) == 3 and est["dup_dropped"] == 2,
      f"docs={docs} dropped={est['dup_dropped']}")
check("D7b 单份文件不再出现 4 条同源内容",
      max(docs.values()) == 2, f"每文档条数={docs}")

print()
print("=" * 70)
print("[D] 关键词抽取：只收实词、长度 ≥ 3")
print("=" * 70)

check("K1 信息安全有哪些要求 → ('信息安全',)",
      high_signal_terms("信息安全有哪些要求") == ("信息安全",),
      f"got={high_signal_terms('信息安全有哪些要求')}")

# 这是本次最大的坑：jieba 把"系统对"标成 l（习用语），用它召回 20 条噪声
t2 = high_signal_terms("这个系统对可靠性有什么要求吗")
check("K2 习用语「系统对」被排除，只留「可靠性」",
      "系统对" not in t2 and "可靠性" in t2, f"got={t2}")

check("K3 2 字泛词「要求」被排除", "要求" not in high_signal_terms("安全要求怎么做"),
      f"got={high_signal_terms('安全要求怎么做')}")

check("K4 域外问题无高区分度词 ⇒ 不产生重试候选",
      retry_candidates("今天北京天气怎么样") == [],
      f"got={retry_candidates('今天北京天气怎么样')}")

check("K4b 红烧肉怎么做 → 有词但库里召不回（由检索侧保证拒答）",
      retry_candidates("红烧肉怎么做") == ["红烧肉"],
      f"got={retry_candidates('红烧肉怎么做')}")

# ★ 这是把域外拒答准确率从 1.000 拉低到 0.800 的真凶，必须守住
check("K5 纯英文词不参与重试（Python 爬虫 → 无候选，避免字面巧合）",
      high_signal_terms("如何用 Python 写一个网页爬虫") == ()
      and retry_candidates("如何用 Python 写一个网页爬虫") == [],
      f"got={high_signal_terms('如何用 Python 写一个网页爬虫')}")

check("K5b 领域英文缩写同样不单独触发（SysML 无中文词时返回空，由原查询自身召回）",
      high_signal_terms("SysML v2 词法有哪些硬约束") == (),
      f"got={high_signal_terms('SysML v2 词法有哪些硬约束')}")

print()
print("=" * 70)
print("[M] 变异自证：把被保护的行为改坏，判据必须翻红")
print("=" * 70)


def _mutant_dedupe_same_doc(hits, min_chars=80):
    """变异体：不区分文档，只要正文相同就删（应当被 D2 抓住）。"""
    seen, out = set(), []
    for h in hits:
        k = h.get("content")
        if k in seen:
            continue
        seen.add(k)
        out.append(h)
    return out, {"dropped": len(hits) - len(out)}


m_kept, _ = _mutant_dedupe_same_doc([COPY_A, SAME_DOC])
check("M1 变异「同文档也删」→ D2 的形态会退化（判据能识别）",
      len(m_kept) == 1 and len(dedupe_exact([COPY_A, SAME_DOC])[0]) == 2,
      "真实实现保留 2 条，变异体只留 1 条")


def _mutant_normalize(hits):
    """变异体：无条件剥数字前缀（应当被 D5 抓住）。"""
    import re
    out = []
    for h in hits:
        h = dict(h)
        h["source_doc"] = re.sub(r"^\d{1,8}_", "", str(h.get("source_doc") or ""))
        out.append(h)
    return out, {}


m_norm = _mutant_normalize([mk(900, "2024_年度报告.pdf", 1, LONG_A)])[0]
check("M2 变异「无条件剥前缀」→ 会把 2024_年度报告.pdf 改坏（D5 的价值）",
      m_norm[0]["source_doc"] == "年度报告.pdf"
      and normalize_doc_names([mk(900, "2024_年度报告.pdf", 1, LONG_A)])[0][0]["source_doc"]
      == "2024_年度报告.pdf",
      "真实实现保留原名，变异体改成 年度报告.pdf")

print()
print("=" * 70)
if _FAILS:
    print(f"总计：{len(_FAILS)} 项失败")
    for f in _FAILS:
        print("  ✗", f)
    sys.exit(1)
print("总计：全部通过")
print("=" * 70)
