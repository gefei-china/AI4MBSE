# -*- coding: utf-8 -*-
"""verify_artifact_validator —— 批次三 #17：report/requirement/doc 三类交付物结构校验。

## 为什么这类门禁必须有牙齿
校验器最危险的失败模式是**「什么都没查却报通过」**：
  · `checked` 写死 1（分不清"跑了 4 条判据"与"跑了 1 条"）；
  · 空内容被当成"没有结构问题"；
  · 未知 kind 返回 ok=True 被误读成"校验通过"；
  · `_CHECKED_COUNT` 与实际 `add(...)` 数量对不上。
⇒ 本门禁的核心是 **X 组：数字与事实一致** + **V 组：空/未知不得判绿**。

## 判据
### V 组 · 元判据（防假绿）
  V1 空内容 ⇒ `ok=False` 且理由写明"内容为空"（**绝不"没内容所以没错"**）
  V2 只有 `{title, ref}` 这类定位信息（无正文）⇒ 同样判红，**不许拿 title 当正文蒙混**
  V3 未知 kind / `sysml` ⇒ `checked=0` + `degraded=True`（**不得假装校验过**）
  V4 校验器异常 ⇒ `ok=True` 但 `checked=0` + `degraded=True`（不阻断交付）
  V5 三类**都真的能被检出问题**（每类各给一个坏样本，必须 ok=False）

### X 组 · 数字与事实一致（★最关键）
  X1 `checked` 等于该类**实际执行的判据条数**（report 4 / requirement 4 / doc 3）
  X2 坏样本命中的 issue 条数 == 该类判据条数（既不为 0，也不过量）
     —— X2 是对 X1 的**独立复核**：只报 `_CHECKED_COUNT` 是自说自话

### 接线组 · 主路径真的调用了
  W1 `stream.py` 物化环节调用了 `validate_artifact`
  W2 校验结果**随产物落库**（写进 content_json.structure_check）
  W3 汇总正文把未通过的交付物**呈现**给用户（不是静默过滤）
  W4 `kind='artifact'`（容器名）**不参与**校验 —— 协议 kind 才是判据

### 变异自证
  M1 把 `_text_of` 改成"空内容返回 title" ⇒ V1/V2 必须判红
  M2 把 `_CHECKED_COUNT["report"]` 改成 99 ⇒ X1/X2 必须判红
  M3 删掉 requirement 的「无 RFC-2119 关键词」判据 ⇒ V5(requirement) 必须判红
  M4 把未知 kind 的 `degraded` 改成 False 且 `checked=1` ⇒ V3 必须判红

用法（必须用项目 venv）
    ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_artifact_validator.py
"""
from __future__ import annotations

import ast
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

VAL = os.path.join(_ROOT, "services", "artifact_validator.py")
STREAM = os.path.join(_ROOT, "agent", "pipeline_parts", "stream.py")

PASS, FAIL = "PASS", "FAIL"
_results = []
_MUT_ROWS = []
_IN_MUT = [False]

GOOD_REPORT = (
    "# 性能分析报告\n\n## 一、现状\n"
    "系统当前 P95 响应时间为 820ms，主要瓶颈在检索层的向量重排。\n\n"
    "## 二、结论\n"
    "建议将重排候选集从 50 降到 20，预计 P95 可降至 400ms 以内。\n\n"
    "## 三、风险与待确认\n"
    "该估算假设重排模型不变；若更换模型需重新标定。\n"
)
GOOD_REQ = (
    "REQ-001 系统应当在检索重排阶段将候选集规模从 50 降至 20，"
    "使得 P95 响应时间不超过 400ms；验收方法：压测 1000 并发请求，"
    "统计 P95 时延并与基线对比。\n"
)
GOOD_DOC = (
    "# 部署手册\n\n## 一、环境准备\n"
    "需要 Python 3.13 与随包运行时 java-runtime/。\n\n"
    "## 二、启动步骤\n"
    "执行 init_db 后启动 uvicorn，监听 8012 端口。\n"
)

BAD = {
    "report": "性能不太行。",                      # 过短 / 无分节 / 无结论 / 无风险
    "requirement": "系统要快。",                    # 无编号 / 无 RFC-2119 / 无验收
    "doc": "简单说明一下怎么用。",                  # 过短 / 无标题 / 分节不足
}


def check(name, ok, detail=""):
    row = (PASS if ok else FAIL, name, "" if ok else str(detail)[:220])
    (_MUT_ROWS if _IN_MUT[0] else _results).append(row)
    print("[%s] %s%s" % ("OK  " if ok else "FAIL", name,
                         ("　— " + detail) if detail and not ok else ""))
    return bool(ok)


def v_group(av):
    print("\n=== V 组 · 元判据（防假绿）===")

    # V1 空内容判红
    r = av.validate_artifact("report", {"content": "   "})
    check("V1 空内容 ⇒ ok=False 且理由写明「内容为空」",
          r["ok"] is False and any("内容为空" in i for i in r["issues"]),
          "实测 ok=%r issues=%s" % (r["ok"], r["issues"]))

    # V2 无正文（只有 title/ref）判红
    r2 = av.validate_artifact("report", {"title": "报告标题", "ref": "doc://x"})
    check("V2 只有 title/ref（无正文）⇒ 判红，不许拿 title 当正文蒙混",
          r2["ok"] is False and any("内容为空" in i for i in r2["issues"]),
          "实测 ok=%r issues=%s" % (r2["ok"], r2["issues"]))

    # V3 未知 kind / sysml 不假装校验过
    r3 = av.validate_artifact("sysml", {"content": "part def X;"})
    r3b = av.validate_artifact("不存在的kind", {"content": "x"})
    check("V3 sysml/未知 kind ⇒ checked=0 + degraded=True（不假装校验过）",
          r3["checked"] == 0 and r3["degraded"] is True
          and r3b["checked"] == 0 and r3b["degraded"] is True,
          "sysml=%s 未知=%s" % (r3, r3b))

    # V4 异常降级
    _orig = av._VALIDATORS["report"]
    try:
        av._VALIDATORS["report"] = lambda t, add: (_ for _ in ()).throw(RuntimeError("boom"))
        r4 = av.validate_artifact("report", {"content": GOOD_REPORT})
    finally:
        av._VALIDATORS["report"] = _orig
    check("V4 校验器自身异常 ⇒ ok=True 但 checked=0 + degraded（不阻断交付）",
          r4["ok"] is True and r4["checked"] == 0 and r4["degraded"] is True,
          "实测=%s" % r4)

    # V5 三类各能被检出问题；且好样本通过
    for k, good in (("report", GOOD_REPORT), ("requirement", GOOD_REQ), ("doc", GOOD_DOC)):
        rb = av.validate_artifact(k, {"content": BAD[k]})
        check("V5a %s 坏样本被判红" % k, rb["ok"] is False,
              "实测 ok=%r issues=%s" % (rb["ok"], rb["issues"]))
        rg = av.validate_artifact(k, {"content": good})
        check("V5b %s 好样本通过（无误杀）" % k, rg["ok"] is True,
              "实测 issues=%s" % rg["issues"])

    # V5c requirement 的两条核心判据各能单独触发
    no_kw = av.validate_artifact("requirement", {"content":
        "REQ-001 系统需要在检索阶段降低候选集规模使得 P95 不超过 400ms；验收：压测 1000 并发。"})
    check("V5c 缺 RFC-2119 关键词被单独指出",
          any("RFC-2119" in i for i in no_kw["issues"]),
          "实测 issues=%s" % no_kw["issues"])
    no_acc = av.validate_artifact("requirement", {"content":
        "REQ-001 系统应当将检索重排候选集从 50 降至 20。"})
    check("V5d 缺验收口径被单独指出",
          any("验收" in i for i in no_acc["issues"]),
          "实测 issues=%s" % no_acc["issues"])


def x_group(av):
    print("\n=== X 组 · 数字与事实一致（★最关键）===")
    from services import artifact_validator as m
    tree = ast.parse(open(VAL, encoding="utf-8").read())
    fns = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}

    # X1 声明的条数 == 函数里 add(...) 的次数（**AST 计数，不靠读注释**）
    for k, fn_name in (("report", "_v_report"), ("requirement", "_v_requirement"),
                       ("doc", "_v_doc")):
        fn = fns.get(fn_name)
        n_add = sum(1 for n in ast.walk(fn) if isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Name) and n.func.id == "add") if fn else -1
        declared = m._CHECKED_COUNT.get(k)
        check("X1 %s：checked 声明值(%s) == 实际 add 次数(%d)"
              % (k, declared, n_add), declared == n_add,
              "声明与事实不符 ⇒ 这个字段就是新的假绿源")

    # X2 独立复核：坏样本命中的 issue 数 == 该坏样本**应触发**的条数。
    # ⚠️ 不是"等于全部判据条数" —— 部分判据是**条件触发**
    #   （report 的"有推测表述但无风险标注"要两个条件同时成立才报），
    #   所以 EXPECT 逐类写死它**该**命中几条；超出同样是问题（多报了不该报的）。
    EXPECT = {"report": 3, "requirement": 4, "doc": 3}
    for k in ("report", "requirement", "doc"):
        rb = m.validate_artifact(k, {"content": BAD[k]})
        exp = EXPECT[k]
        check("X2 %s：坏样本命中 %d 条 issue（应命中 %d 条，上限 %d）"
              % (k, len(rb["issues"]), exp, m._CHECKED_COUNT.get(k, 0)),
              len(rb["issues"]) == exp,
              "实测 %d 条：%s" % (len(rb["issues"]), rb["issues"]))

    # X2b ★上限断言：issues 条数**不得超过**声明的判据条数
    #   （超过说明有判据被重复触发、或 declared 被调大了来掩盖漏报）
    for k in ("report", "requirement", "doc"):
        rb = m.validate_artifact(k, {"content": BAD[k]})
        check("X2b %s：issues 条数不超过声明上限" % k,
              len(rb["issues"]) <= m._CHECKED_COUNT.get(k, 0),
              "实测 %d > 上限 %d" % (len(rb["issues"]), m._CHECKED_COUNT.get(k, 0)))


def w_group():
    print("\n=== W 组 · 主路径真的调用了 ===")
    src = open(STREAM, encoding="utf-8").read()
    check("W1 物化环节调用了 validate_artifact",
          "validate_artifact" in src and "artifact_validator" in src)
    check("W2 校验结果随产物落库（structure_check）",
          "structure_check" in src)
    check("W3 汇总正文把未通过项呈现给用户",
          "structure_check" in src and "结构体检未通过" in src)
    check("W4 容器名 kind='artifact' 被显式排除",
          'if _a.get("kind") != "artifact"' in src)
    # W5 校验不得阻断交付（异常降级包裹）
    i = src.find("structure_check")
    check("W5 校验调用被 try 包住（异常不阻断子任务）",
          i > 0 and "except Exception" in src[i:i + 400],
          "调用点后 400 字符内未见 except")


def mutation_selfproof(av):
    print("\n=== 变异自证（证明判据有牙齿）===")
    from services import artifact_validator as m

    # M1 _text_of 空内容回退到 title
    _IN_MUT[0] = True
    orig_tf = m._text_of
    try:
        m._text_of = lambda art: (art.get("title", "") if isinstance(art, dict)
                                  else str(art or ""))
        v_group(m)
    finally:
        m._text_of = orig_tf
        _IN_MUT[0] = False
    _expect_mut("M1", "_text_of 空内容回退到 title", "V1", "V2")

    # M2 _CHECKED_COUNT 改成 99
    _IN_MUT[0] = True
    orig_cnt = dict(m._CHECKED_COUNT)
    try:
        m._CHECKED_COUNT.update({k: 99 for k in orig_cnt})
        x_group(m)
    finally:
        m._CHECKED_COUNT.clear()
        m._CHECKED_COUNT.update(orig_cnt)
        _IN_MUT[0] = False
    _expect_mut("M2", "_CHECKED_COUNT 改成 99", "X1", "X2b")

    # M3 删掉 requirement 的 RFC-2119 判据
    _IN_MUT[0] = True
    orig_r = m._VALIDATORS["requirement"]
    try:
        def _no_kw(t, add):
            if len(t) < m.MIN_CONTENT_CHARS["requirement"]:
                add("内容过短")
            if not m._REQ_ID.search(t):
                add("无条目标识")
            if not __import__("re").search(r"验收|验证", t):
                add("无验收口径")
        m._VALIDATORS["requirement"] = _no_kw
        v_group(m)
    finally:
        m._VALIDATORS["requirement"] = orig_r
        _IN_MUT[0] = False
    _expect_mut("M3", "删掉 requirement 的 RFC-2119 判据", "V5c")

    # M4 未知 kind 改成"已校验"
    _IN_MUT[0] = True
    orig_va = m.validate_artifact
    try:
        def _fake(kind, artifact):
            return {"ok": True, "checked": 1, "issues": [], "degraded": False,
                    "kind": str(kind or "-")}
        m.validate_artifact = _fake
        v_group(m)
    finally:
        m.validate_artifact = orig_va
        _IN_MUT[0] = False
    _expect_mut("M4", "未知 kind 谎报「已校验」", "V3")


def _expect_mut(tag, desc, *prefixes):
    hits = [n for st, n, _ in _MUT_ROWS
            if st == FAIL and any(n.startswith(p) for p in prefixes)]
    _MUT_ROWS.clear()
    ok = bool(hits)
    print("[%s] %s %s ⇒ %s 必须判红%s"
          % ("OK  " if ok else "FAIL", tag, desc, "/".join(prefixes),
             ("（实际红：%s）" % hits[0]) if hits else "（**未判红 = 判据空转**）"))
    if not ok:
        _results.append((FAIL, "%s %s" % (tag, desc), "变异后判据未判红"))


def main() -> int:
    print("=" * 74)
    print("判据 · 批次三 #17 report/requirement/doc 三类交付物结构校验")
    from services import artifact_validator as av
    v_group(av)
    x_group(av)
    w_group()
    mutation_selfproof(av)

    npass = sum(1 for st, _, _ in _results if st == PASS)
    nfail = sum(1 for st, _, _ in _results if st == FAIL)
    print("\n" + "=" * 74)
    print("%d pass / %d fail" % (npass, nfail))
    if nfail:
        for st, n, d in _results:
            if st == FAIL:
                print("  - %s%s" % (n, ("　— " + d) if d else ""))
        return 1
    print("✅ 全部通过：三类交付物结构可判定 + checked 与事实一致 + 主路径已接线")
    return 0


if __name__ == "__main__":
    sys.exit(main())
