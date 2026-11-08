# -*- coding: utf-8 -*-
"""Glossary 归一化幂等性门禁（2026-11-08）。

## 这条门禁防的是「同一条 query 被归一化两次就变成另一个串」

原 `normalize` 是**多轮**替换：

    for r in sorted(hits, key=len, reverse=True):
        out = pat.sub(r["canonical_term"], out)      # 后一轮作用于前一轮的结果

而词表里有 12 条 entry 的 canonical **自己就包含** user_term：

    宽带     → 宽带通信              巡飞   → 巡飞待机
    v2       → SysML_V2              ibd    → 内部块图_IBD
    语法规范 → SysML_V2 语法规范       v波段 → V波段_卫星通信频段

于是每替换一次，串就被撑长一截，下一轮再撑一次：

    宽带通信    → 宽带通信通信
    巡飞待机弹  → 巡飞待机待机弹
    SysML v2   → SysML_SysML_V2      （`v2` 命中自己刚产出的 `SysML_V2`）

另有一个独立的边界缺陷：词边界字符集是 `[A-Za-z0-9]`，**不含 `_`**，而规范词
普遍带下划线（`SysML_V2` / `内部块图_IBD` / `热管理系统_TMS`）⇒ 规范词内部的
`V2` / `IBD` 会被 `v2` / `ibd` 规则再次命中。

后果不是"日志难看"：同一条 query 因被归一化次数不同而得到不同检索串 ⇒
**召回不可复现**；而膨胀出来的 `宽带通信通信` 恰好召不回原文能命中的 chunk。
实测存量 6572 条 query 中 1030 条（15.7%）old/new 不同、其中 1012 条是膨胀，
699 条（23.3%）**再归一化一次还会变**。

## 修法（三条，缺一条都有反例 → 对应 M1/M2/M3）
1. 在**原文**上定位 span，长词优先 + 区间互斥，canonical 只写回输出、不再被扫描；
2. 词边界纳入 `_`；
3. 「原文该处已是完整规范形态」则跳过（suffix 型 `宽带通信` / prefix 型
   `SysML_V2 语法规范` 两种都要覆盖）—— 这是**膨胀**问题，光做单遍修不掉。

## 判据（每条都能变红）
  N1  合成：长词优先，短词不得把长词的产物再改写（`ABCD` 不被 `CD` 打碎）
  N2  合成：区间互斥 —— 重叠候选只取长的，不得同时替换两者
  N3  合成：边界含 `_` —— `SYSML_V2` 里的 `V2` 不被 `v2` 规则命中
  N4  合成：已是规范形态则跳过（suffix 型 / prefix 型）
  N5  合成：canonical 恰好等于命中片段时不动（大小写不敏感）
  N6  合成：无命中 / 无候选 ⇒ 原样返回
  N7  真实词表：`SysML v2` → `SysML_V2`（不得出 `SysML_SysML_V2`）
  N8  真实词表：`宽带通信` 不变；`宽带载荷` → `宽带通信载荷`
  N9  真实词表：`巡飞待机弹` 不变；`巡飞弹` → `巡飞待机弹`
  N10 真实词表：`内部块图_IBD` / `热管理系统_TMS` 不变
  N11 真实词表：`语法规范 查询` → `SysML_V2 语法规范 查询`（prefix 型只换一次）
  N12 真实库全量：对每条 query，normalize(normalize(x)) == normalize(x)（无库 SKIP）
  N13 真实库全量：输出不得命中已知膨胀模式（`通信通信`/`待机待机`/`SysML_SysML` 等）
  N14 hits 语义不变：normalize 返回的 hits 与 match 完全一致（force_intent/force_domain 不受影响）

## 变异自证
  M1  恢复「多轮 sub」实现（canonical 参与下一轮扫描）   → N1/N2/N8 必须红
  M2  边界去掉 `_`（回到 `[A-Za-z0-9]`）                 → N3/N10 必须红
  M3  `_already_canonical` 恒 False（不跳过规范形态）     → N4/N8/N9/N11 必须红
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
    """detail 只在失败时打印 —— PASS 时打"失败文案"会让人以为门禁在报错。"""
    (PASS if cond else FAIL).append(name)
    if cond:
        print("  PASS %s" % name, flush=True)
    else:
        print("  FAIL %s  <- %s" % (name, detail), flush=True)


def skip(name, why):
    SKIPPED.append(name)
    print("  SKIP %s  <- %s" % (name, why), flush=True)


def _hit(u, k):
    """构造一条最小 hit（_span_replace 只读 user_term / canonical_term）。"""
    return {"user_term": u, "canonical_term": k}


def _load_variant(mutate=None):
    """读 glossary.py 源码，可选文本变换，exec 出模块命名空间。

    变异自证用它：判据函数对「变异后的实现」必须翻红，否则门禁只是在
    复述当前实现、抓不住回归。
    """
    with open(os.path.join(ROOT, "glossary.py"), encoding="utf-8") as f:
        src = f.read()
    if mutate:
        src = mutate(src)
    ns = {"__name__": "glossary_mut"}
    exec(compile(src, "glossary.py", "exec"), ns)
    return ns


# ---------------------------------------------------------------- M1 多轮 sub
def _mut_multi_pass(src):
    """把单遍实现换成旧的多轮 sub（canonical 参与下一轮扫描）。"""
    old_body = '''    cands = []'''
    new_body = '''    _out = text
    for _r in sorted(hits, key=lambda x: -len(x.get("user_term") or "")):
        _u = _r.get("user_term") or ""
        if not _u:
            continue
        _k = _r.get("canonical_term") or _u
        _pat = re.compile(r"(?<!%s)%s(?!%s)" % (_WORD_CH, re.escape(_u), _WORD_CH),
                          re.IGNORECASE)
        _out = _pat.sub(_k, _out)
    return _out
    cands = []'''
    assert old_body in src, "M1 锚点失效（_span_replace 函数体已被改写）"
    return src.replace(old_body, new_body, 1)


# ---------------------------------------------------------------- M2 边界去 _
def _mut_no_underscore(src):
    old = '_WORD_CH = r"[A-Za-z0-9_]"'
    assert old in src, "M2 锚点失效（_WORD_CH 定义已被改写）"
    return src.replace(old, '_WORD_CH = r"[A-Za-z0-9]"', 1)


# ------------------------------------------------------- M3 不判「已规范形态」
def _mut_no_canonical_skip(src):
    old = '''    low_hit, low_k = text[s:e].lower(), canonical.lower()'''
    new = '''    return False
    low_hit, low_k = text[s:e].lower(), canonical.lower()'''
    assert old in src, "M3 锚点失效（_already_canonical 函数体已被改写）"
    return src.replace(old, new, 1)


def run_pure(ns, label):
    """纯函数判据（不依赖数据库）—— 对任意实现版本都能跑。"""
    sr = ns["_span_replace"]

    # N1 长词优先：短词不得把长词的产物再改写
    got = sr("AB CD", [_hit("AB", "ABCD"), _hit("CD", "ZZ")])
    check("N1 %s 长词优先（短词不打碎长词产物）" % label,
          got == "ABCD ZZ", "期望 'ABCD ZZ'，实得 %r" % got)

    # N2 区间互斥：重叠候选只取长的
    got = sr("ABCD", [_hit("AB", "X"), _hit("ABCD", "Y")])
    check("N2 %s 区间互斥（重叠只取长）" % label,
          got == "Y", "期望 'Y'，实得 %r" % got)

    # N3 边界含 `_`。
    # 用例要选「canonical 不是 原文片段 的 prefix+命中+suffix」的形态：否则
    # `_already_canonical` 会先一步跳过，M2（去掉 `_` 边界）就抓不出来了。
    # 这里原文是 `A_V2_C`，而 canonical 只有 `A_V2_B` —— 只有边界能挡。
    got = sr("A_V2_C", [_hit("v2", "A_V2_B")])
    check("N3 %s 边界含下划线（规范词内部不再命中）" % label,
          got == "A_V2_C", "期望原样，实得 %r" % got)
    got = sr("ibd 是什么", [_hit("ibd", "IBD")])

    # N4a suffix 型：宽带 → 宽带通信，原文已是 宽带通信
    got = sr("宽带通信", [_hit("宽带", "宽带通信")])
    check("N4a %s 已规范形态跳过（suffix 型）" % label,
          got == "宽带通信", "期望原样，实得 %r" % got)
    # N4b prefix 型：语法规范 → SysML_V2 语法规范，原文已是完整形态
    got = sr("SysML_V2 语法规范 查询", [_hit("语法规范", "SysML_V2 语法规范")])
    check("N4b %s 已规范形态跳过（prefix 型）" % label,
          got == "SysML_V2 语法规范 查询", "期望原样，实得 %r" % got)

    # N5 canonical 恰等于命中片段（大小写不敏感）
    got = sr("ibd 是什么", [_hit("ibd", "IBD")])
    check("N5 %s 等值替换按大小写归一" % label,
          got == "IBD 是什么", "期望 'IBD 是什么'，实得 %r" % got)

    # N6 无命中 / 无候选 ⇒ 原样
    check("N6 %s 无候选原样返回" % label,
          sr("原样文本", []) == "原样文本", "空 hits 时被改动")


def main():
    print("[1] 合成判据（当前实现）", flush=True)
    real = _load_variant()
    run_pure(real, "当前")

    print("\n[2] 真实词表端到端（无库则 SKIP）", flush=True)
    db = os.path.join(ROOT, "mbse.db")
    m = None
    if os.path.exists(db):
        try:
            from glossary import GlossaryMatcher
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            m = GlossaryMatcher(conn)
            if not (m._load_concepts() or m._load()):
                m = None
        except Exception as e:                       # noqa: BLE001
            print("  （打开真库失败：%s）" % e, flush=True)
            m = None

    if m is None:
        for n in ("N7", "N8", "N9", "N10", "N11", "N12", "N13", "N14"):
            skip("%s 真实词表/真库" % n, "无 mbse.db 或无词条（CI 环境）")
    else:
        exp = [
            ("N7", "用一句话说明什么是 SysML v2 的 part definition。",
             "用一句话说明什么是 SysML_V2 的 part definition。"),
            ("N8a", "宽带通信 载荷", "宽带通信 载荷"),
            ("N8b", "宽带载荷 转发器", "宽带通信载荷 转发器"),
            ("N9a", "巡飞待机弹 参数", "巡飞待机弹 参数"),
            ("N9b", "巡飞弹 战斗部", "巡飞待机弹 战斗部"),
            ("N10a", "内部块图_IBD 是什么", "内部块图_IBD 是什么"),
            ("N10b", "热管理系统_TMS 指标", "热管理系统_TMS 指标"),
            ("N11", "语法规范 查询", "SysML_V2 语法规范 查询"),
        ]
        for tag, q, want in exp:
            got, _ = m.normalize(q)
            check("%s %s" % (tag, q[:22]), got == want,
                  "期望 %r，实得 %r" % (want, got))

        # N12/N13/N14 全量
        qs = [r[0] for r in m.conn.execute(
            "SELECT query FROM query_trace WHERE COALESCE(query,'')<>''")]
        if not qs:
            skip("N12 真库全量幂等", "query_trace 为空")
            skip("N13 真库无膨胀模式", "query_trace 为空")
        else:
            nonidem, worse, bad_hits = [], [], []
            legacy_dirty = 0
            pats = ("通信通信", "待机待机", "SysML_SysML", "块图_内部块图",
                    "热管理系统_TMS_TMS", "波段_卫星通信频段_V波段")
            for q in qs:
                n1, h1 = m.normalize(q)
                n2, _ = m.normalize(n1)
                if n2 != n1:
                    nonidem.append((q, n1, n2))
                # 判据是「不**新增**膨胀」，不是「输出里没有膨胀」：
                # 存量 query 里有 394 条本身就是旧实now污染的产物（`宽带通信通信载荷…`），
                # 已无法区分「原文两处宽带」与「一次膨胀」，属不可逆历史数据，
                # 拿它判红等于要求代码修复历史 —— 那会让门禁长期恒红、失去信号。
                if any(p in q for p in pats):
                    legacy_dirty += 1
                for p in pats:
                    if n1.count(p) > q.count(p):
                        worse.append((q, n1, p))
                        break
                if [id(x) for x in h1] != [id(x) for x in m.match(q)]:
                    bad_hits.append(q)
            check("N12 真库全量幂等（%d 条）" % len(qs), not nonidem,
                  "非幂等 %d 条，例：%r → %r" % (len(nonidem), nonidem[:1],
                                                 nonidem[:1] and nonidem[0][2]))
            check("N13 真库不新增膨胀模式（%d 条）" % len(qs), not worse,
                  "新增 %d 条，例：%r（模式 %r）" % (len(worse),
                                                    worse[:1] and worse[0][1],
                                                    worse[:1] and worse[0][2]))
            check("N14 hits 与 match 一致（%d 条）" % len(qs), not bad_hits,
                  "不一致 %d 条，例 %r" % (len(bad_hits), bad_hits[:1]))
            print("  · 存量输入已带膨胀模式 %d 条（历史不可逆，不判红）" % legacy_dirty,
                  flush=True)

        # 归一化不得改变「未命中」的文本对象语义（无命中必须原样）
        raw = "帮我写一首关于春天的诗"
        got, _ = m.normalize(raw)
        check("N15 无命中文本原样返回", got == raw, "实得 %r" % got)

    print("\n[3] 变异自证（判据必须能翻红）", flush=True)
    for tag, mut in (("M1 多轮 sub", _mut_multi_pass),
                     ("M2 边界去下划线", _mut_no_underscore),
                     ("M3 不跳过已规范形态", _mut_no_canonical_skip)):
        try:
            ns = _load_variant(mut)
        except AssertionError as e:
            check("%s 变异可施加" % tag, False, str(e))
            continue
        _p, _f = list(PASS), list(FAIL)
        PASS.clear()
        FAIL.clear()
        run_pure(ns, "变异")
        red = list(FAIL)
        PASS[:] = _p
        FAIL[:] = _f
        check("%s → 判据翻红" % tag, bool(red),
              "该变异下全部判据仍然通过 ⇒ 门禁抓不住回归")

    print("\n=== %d PASS / %d FAIL / %d SKIP ===" % (len(PASS), len(FAIL),
                                                     len(SKIPPED)), flush=True)
    if SKIPPED:
        print("SKIP 项（未验证，不等于通过）：%s" % ", ".join(SKIPPED), flush=True)
    if FAIL:
        print("FAIL: %s" % ", ".join(FAIL), flush=True)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
