# -*- coding: utf-8 -*-
"""Glossary 术语边界门禁（2026-11-08）。

## 这条门禁防的是「命中判据比替换判据宽」

`GlossaryMatcher.match()` 此前用 `user_term.lower() in text.lower()` 做**子串**
判断，而 `_span_replace()`（归一化）用**词边界正则**。两者口径不同，且宽的
那一侧恰好是**下游用来定 force_domain / force_intent 的**：

    agent/rag.py:231      resolve(query) → force_domain → hybrid_search(domain=…)
    agent/intent.py:95    bool(hits)  ⇒ 判「是不是知识问题」
    agent/intent.py:1047  force_intent ⇒ 直接 route 到 knowledge_qa(0.95)

于是英文单词内部的两字母缩写被当成术语命中，把 query 钉在一个不相干的域上。
真库实测（6579 条 query）：

    PA      ⊂ part / Part                        452 条 → satellite_comms
    SA      ⊂ satisfy                            248 条 → satellite_comms
    CEP     ⊂ accept                              57 条 → loitering_munition
    LM      ⊂ AI/LLM                              50 条 → loitering_munition
    tms     ⊂ EVTMS                                5 条 → thermal_mgmt
    Antenna ⊂ AntennaInterfaceUnit                 5 条 → satellite_comms

`satisfy` / `part` 都是 SysML 语法本身的词 ⇒ **越是在问 SysML 的问题，越会
被钉到卫星通信域**。`ref part` 正因此被判成 satellite_comms（库内 0 chunk），
2026-10-16 那次「20 条 → 0 条 → 20 条」的召回抖动就是它。

## 边界规则：按 term 自身首尾字符决定，不是统一加

    ASCII 边缘   → 必须有词边界（`PA` ⊄ `part`，`tms` ⊄ `EVTMS`）
    CJK  边缘   → **不**加边界（`转发器EIRP` 里 `转发器` 是独立术语；统一加
                  会让它因为紧跟英文而被漏掉 —— 与上面是同类错误的两个方向）

`_` 不算词内字符（标识符分段符）：`sysml_v2_validate` 里 `SysML_V2` / `sysml` /
`v2` 都要能命中，否则蛇形标识符整串当 token ⇒ 域/意图信号丢失（实测 360 条）。
防膨胀不靠这条边界，靠 `_already_canonical()`（见 verify_glossary_normalize_idempotent.py）。

## 判据
  B1~B6  词内假命中必须消失（PA/part、SA/satisfy、CEP/accept、LM/LLM、
         tms/EVTMS、Antenna/AntennaInterfaceUnit）
  B7~B8  独立出现的缩写仍要命中（PA / tms）—— 只杀假阳性、不杀真阳性
  B9     蛇形标识符段内命中（`sysml_v2_validate` ⊃ SysML_V2/sysml/v2）
  B10    CJK 边缘不设边界（`转发器EIRP` ⊃ 转发器）
  B11    `ref part` 端到端：resolve() 不再给 satellite_comms（本次故障原样复现）
  B12    结构性：match/_span_replace **共用** `_term_pattern`，两处都不得自行
         `re.compile` 或回退子串（口径一分叉就会重新漂移）
  B13    空 user_term 不得命中任何文本（`"" in text` 恒真）
  B14    真库：全部 query 的每个 hit 都通过同一 `_term_in` 判定（无库 SKIP）
  B15    真库：`ref part` / `part definition` 无 satellite_comms（无库 SKIP）

## 变异自证
  M1  把 `_term_in` 改回子串                    → B1~B6 必须红
  M2  `_term_pattern` 对 CJK 边缘也加边界        → B10 必须红
  M3  `_span_replace` 改用无边界正则             → B16 必须红（归一化会把
      `ref part` 改成 `ref 功率放大器rt`，即"子串命中的后果"落到文本上）
"""
import os
import re
import sqlite3
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.chdir(ROOT)

PASS, FAIL, SKIPPED = [], [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    if cond:
        print("  PASS %s" % name, flush=True)
    else:
        print("  FAIL %s  <- %s" % (name, detail), flush=True)


def skip(name, why):
    SKIPPED.append(name)
    print("  SKIP %s  <- %s" % (name, why), flush=True)


# 最小词表：只放本条门禁需要的条目，值取自真库（domain 与 canonical 都是真的）。
ROWS = [
    {"user_term": "PA", "canonical_term": "功率放大器", "domain": "satellite_comms",
     "intent": "knowledge_qa", "boost": 1.0, "active": 1},
    {"user_term": "SA", "canonical_term": "太阳翼", "domain": "satellite_comms",
     "intent": "knowledge_qa", "boost": 1.0, "active": 1},
    {"user_term": "CEP", "canonical_term": "圆概率误差",
     "domain": "loitering_munition", "intent": "knowledge_qa", "boost": 1.0, "active": 1},
    {"user_term": "LM", "canonical_term": "巡飞弹", "domain": "loitering_munition",
     "intent": "knowledge_qa", "boost": 1.0, "active": 1},
    {"user_term": "Antenna", "canonical_term": "天线", "domain": "satellite_comms",
     "intent": "knowledge_qa", "boost": 1.0, "active": 1},
    {"user_term": "tms", "canonical_term": "热管理系统_TMS", "domain": "thermal_mgmt",
     "intent": "knowledge_qa", "boost": 1.0, "active": 1},
    {"user_term": "SysML_V2", "canonical_term": "SysML_V2", "domain": "sysml_norm",
     "intent": "knowledge_qa", "boost": 1.0, "active": 1},
    {"user_term": "sysml", "canonical_term": "SysML", "domain": "sysml_norm",
     "intent": "knowledge_qa", "boost": 1.0, "active": 1},
    {"user_term": "v2", "canonical_term": "SysML_V2", "domain": "sysml_norm",
     "intent": "knowledge_qa", "boost": 1.0, "active": 1},
    {"user_term": "转发器", "canonical_term": "转发器", "domain": "satellite_comms",
     "intent": "knowledge_qa", "boost": 1.0, "active": 1},
    {"user_term": "宽带", "canonical_term": "宽带通信", "domain": "satellite_comms",
     "intent": "knowledge_qa", "boost": 1.0, "active": 1},
]


def _load_variant(mutate=None):
    with open(os.path.join(ROOT, "glossary.py"), encoding="utf-8") as f:
        src = f.read()
    if mutate:
        src = mutate(src)
    ns = {"__name__": "glossary_mut"}
    exec(compile(src, "glossary.py", "exec"), ns)
    return ns, src


def _matcher(ns, rows=None, concepts=()):
    """构造一个不依赖数据库的 matcher（_load / _load_concepts 直接返回夹具）。"""

    class _M(ns["GlossaryMatcher"]):
        def __init__(self):
            self.conn = None
            self._rows = [dict(r) for r in (ROWS if rows is None else rows)]
            self._concept_rows = [dict(r) for r in concepts]

    return _M()


def run_cases(ns, label):
    m = _matcher(ns)
    terms = lambda q: [h["user_term"] for h in m.match(q)]          # noqa: E731
    dom = lambda q: m.resolve(q)["force_domain"]                    # noqa: E731

    for tag, q, bad, why in (
            ("B1", "ref part", "PA", "part"),
            ("B2", "需要 satisfy 追溯", "SA", "satisfy"),
            ("B3", "transition accept do send", "CEP", "accept"),
            ("B4", "生成AI/LLM推理服务", "LM", "LLM"),
            ("B5", "EVTMS 完整模型", "tms", "EVTMS"),
            ("B6", "AntennaInterfaceUnit 端口", "Antenna", "AntennaInterfaceUnit")):
        got = terms(q)
        check("%s %s 不命中 `%s`（`%s` 里的假命中）" % (tag, label, bad, why),
              bad not in got, "实得 hits=%r" % got)

    # 正例：独立出现的缩写真要命中，别把真阳性一起杀掉
    check("B7 %s 独立缩写仍命中（PA 模块 增益）" % label,
          "PA" in terms("PA 模块 增益"), "实得 hits=%r" % terms("PA 模块 增益"))
    check("B8 %s 独立缩写仍命中（tms 热管理）" % label,
          "tms" in terms("tms 热管理 指标"), "实得 hits=%r" % terms("tms 热管理 指标"))

    # 蛇形标识符：`_` 是分段符，段内术语必须命中（否则 360 条 query 丢域信号）
    got = terms("sysml_v2_validate 校验")
    check("B9 %s 蛇形标识符段内命中（sysml_v2_validate）" % label,
          "SysML_V2" in got and "sysml" in got,
          "实得 hits=%r" % got)

    # CJK 边缘不设边界：`转发器EIRP` 里 `转发器` 必须命中
    check("B10 %s CJK 边缘不设边界（转发器EIRP）" % label,
          "转发器" in terms("转发器EIRP是多少"),
          "实得 hits=%r" % terms("转发器EIRP是多少"))

    # 故障原样复现：`ref part` 曾因 PA 被判成卫星通信域（该域 0 chunk ⇒ 必零召回）
    for q in ("ref part", "part definition 和 ref 的区别"):
        d = dom(q)
        check("B11 %s `%s` 不再命中 satellite_comms" % (label, q[:26]),
              d != "satellite_comms", "force_domain=%r（hits=%r）" % (d, terms(q)))

    # B16：归一化只能替换「合法出现」的那一处，不能连带改掉词内的同形子串。
    # 用例必须**同时**含一处合法 `PA` 与一处词内 `pa`：只放 `part` 的话
    # match() 返回空 ⇒ normalize 直接短路返回，变异 M3 根本跑不到（假绿）。
    txt, _ = m.normalize("PA 模块 part 定义")
    check("B16 %s 归一化只替换合法出现（不碰 part）" % label,
          "功率放大器" in txt and "part" in txt,
          "实得 %r（应形如 '功率放大器 模块 part 定义'）" % txt)

    # B13：空词条不得命中一切（`"" in text` 恒真 ⇒ 每条 query 都带一个假 hit）
    check("B13 %s 空 user_term 不命中" % label,
          ns["_term_in"]("任意文本", "") is False,
          "实得 %r" % ns["_term_in"]("任意文本", ""))


def run_structural(src, label):
    """B12：口径只能有一处定义 —— 两处各写一份迟早漂移。"""
    m = re.search(r"def match\(self.*?\n(?=    def )", src, re.S)
    body = m.group(0) if m else ""
    sp = re.search(r"def _span_replace\(.*?\n(?=def )", src, re.S)
    spbody = sp.group(0) if sp else ""
    check("B12 %s match() 走 _term_in（不得回退子串/自写正则）" % label,
          "_term_in(" in body and "re.compile" not in body
          and ".lower() in low" not in body.replace(
              "u.lower() in low and", "").replace(
              "(r.get(\"user_term\") or \"\").lower() in low", ""),
          "match() 里仍有独立判据")
    check("B12 %s _span_replace 走 _term_pattern（不得自写正则）" % label,
          "_term_pattern(" in spbody and "re.compile" not in spbody,
          "_span_replace 里仍有独立正则")


# ---------------------------------------------------------------- 变异
def _mut_substring(src):
    """M1：把 `_term_in` 改回子串判断（本次要修的那个 bug）。"""
    old = 'return bool(term) and _term_pattern(term).search(text) is not None'
    assert old in src, "M1 锚点失效（_term_in 已被改写）"
    return src.replace(
        old, 'return bool(term) and term.lower() in text.lower()', 1)


def _mut_always_boundary(src):
    """M2：对所有 term 无条件加边界（丢掉「CJK 边缘不加」这条）。"""
    old = ('    pre = r"(?<!%s)" % _WORD_CH if _ASCII_EDGE.match(term[:1]) else ""\n'
           '    suf = r"(?!%s)" % _WORD_CH if _ASCII_EDGE.match(term[-1:]) else ""')
    assert old in src, "M2 锚点失效（_term_pattern 边界分支已被改写）"
    return src.replace(
        old, '    pre = r"(?<!%s)" % _WORD_CH\n'
             '    suf = r"(?!%s)" % _WORD_CH', 1)


def _mut_replace_without_boundary(src):
    """M3：`_span_replace` 用无边界正则（子串命中真的落到文本上）。"""
    old = "        for mo in _term_pattern(u).finditer(text):"
    assert old in src, "M3 锚点失效（_span_replace 的匹配行已被改写）"
    return src.replace(
        old, "        for mo in re.compile(re.escape(u), re.IGNORECASE).finditer(text):", 1)


def main():
    print("[1] 边界判据（当前实现）", flush=True)
    ns, src = _load_variant()
    run_cases(ns, "当前")
    run_structural(src, "当前")

    print("\n[2] 真库全量（无库则 SKIP）", flush=True)
    db = os.path.join(ROOT, "mbse.db")
    if not os.path.exists(db):
        for n in ("B14", "B15"):
            skip("%s 真库" % n, "无 mbse.db（CI 环境）")
    else:
        try:
            from glossary import GlossaryMatcher, _term_in
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            m = GlossaryMatcher(conn)
            if not (m._load_concepts() or m._load()):
                raise RuntimeError("词表为空")
            qs = [r[0] for r in conn.execute(
                "SELECT query FROM query_trace WHERE COALESCE(query,'')<>''")]
            bad = []
            for q in qs:
                for h in m.match(q):
                    if not _term_in(q, h["user_term"]):
                        bad.append((q[:40], h["user_term"]))
            check("B14 真库 %d 条 query：每个 hit 都过同一 _term_in" % len(qs),
                  not bad, "越界命中 %d 例：%r" % (len(bad), bad[:3]))
            doms = {}
            for q in ("ref part", "ref part 语法", "part definition 和 ref 的区别"):
                doms[q] = m.resolve(q)["force_domain"]
            check("B15 真库 `ref part` 系列不再给 satellite_comms",
                  all(v != "satellite_comms" for v in doms.values()),
                  "实得 %r" % doms)
        except Exception as e:                              # noqa: BLE001
            skip("B14/B15 真库", "打开失败：%s" % e)

    print("\n[3] 变异自证（判据必须能翻红）", flush=True)
    for tag, mut in (("M1 命中改回子串", _mut_substring),
                     ("M2 CJK 边缘也加边界", _mut_always_boundary),
                     ("M3 归一化用无边界正则", _mut_replace_without_boundary)):
        try:
            mns, msrc = _load_variant(mut)
        except AssertionError as e:
            check("%s 变异可施加" % tag, False, str(e))
            continue
        _p, _f = list(PASS), list(FAIL)
        PASS.clear()
        FAIL.clear()
        run_cases(mns, "变异")
        red = list(FAIL)
        PASS[:] = _p
        FAIL[:] = _f
        check("%s → 判据翻红" % tag, bool(red),
              "该变异下全部判据仍通过 ⇒ 门禁抓不住回归；红项=%r" % (red,))

    print("\n=== %d PASS / %d FAIL / %d SKIP ===" % (len(PASS), len(FAIL),
                                                     len(SKIPPED)), flush=True)
    if SKIPPED:
        print("SKIP 项（未验证，不等于通过）：%s" % ", ".join(SKIPPED), flush=True)
    if FAIL:
        print("FAIL: %s" % ", ".join(FAIL), flush=True)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
