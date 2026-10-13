# -*- coding: utf-8 -*-
"""verify_claims_and_conflicts —— 批次三 #16（claims 协议）+ #15（结论冲突检测）。

## 为什么这两个必须**同一个门禁**
`claims` 是冲突检测的**唯一输入源**（`detect_conflicts` 只看 `claims[].subject`）。
分开写就会出现本仓最典型的假绿：**判据扫到 0 个 claim 就恒真**，
而"为什么扫到 0 个"（协议没注入 / 模型没写 subject）被两处门禁各自忽略。
⇒ 这里先断言 **claims 真的能落地**（D 组），再断言**冲突真的能检出**（C 组）。

## 判据
### D 组 · claims 协议
  D1 枚举三档齐全（fact/inference/to_verify），且 `DEFAULT_CLAIM_KIND == "to_verify"`
  D2 **兜底方向**：缺 `kind` ⇒ `to_verify`（比 basis 的 `asserted` 更保守）
  D3 缺 `text` 的条目被丢弃（text 是唯一必填）；`subject` 缺失时**保留条目**
  D4 claims **非数组不判 ok=False**（可选字段，不能因新增维度把存量判成非法）
  D5 长度/条数上限生效（text≤500 / subject≤60 / ≤30 条）
  D6 `normalize_text`（纯文本兜底）产出的 claims 为**空数组**而非伪造

### C 组 · 冲突检测
  C1 数值互斥检出（同一 subject：3 个 vs 5 个）
  C2 单位归一后**不**误报（3k vs 3000 ⇒ 同一取值，不算冲突）
  C3 枚举/极性互斥检出（"支持 X" vs "不支持 X"）
  C4 无锚点（缺 subject）的 claim **不参与**判定
  C5 `conflict_stats` 能区分三种"空结果"：`no-data` / `insufficient` / `checkable`
     ★ 这是本门禁最重要的一条：**`detect_conflicts` 返回 [] 时，
       调用方必须能分清"没冲突"与"没数据可判"**。
  C6 互为子串（"接口" vs "外部接口"）**不**报冲突（属一详一略）

### 接线组 · 主路径真的调用了
  W1 `RefineGate.run` 走冲突检测（结果含 `conflicts` 键）
  W2 **三条返回路径字段同键**（disabled / 空报告 / 异常降级）——
     本仓老教训：两条路径字段不齐 ⇒ 前端与日志各自误判
  W3 子任务 prompt 契约里含 `claims` 与 `subject`（不注入 = 协议永不落地）
  W4 汇总正文按 kind 分档呈现 `to_verify`

### 变异自证
  M1 把 `detect_conflicts` 改成恒返回 [] ⇒ C1/C3 必须判红
  M2 把 `DEFAULT_CLAIM_KIND` 改成 "fact" ⇒ D1/D2 必须判红
  M3 从 prompt 契约里删掉 claims ⇒ W3 必须判红
  M4 把 `conflict_stats` 的 verdict 恒返回 "checkable" ⇒ C5 必须判红

用法（必须用项目 venv）
    ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_claims_and_conflicts.py
"""
from __future__ import annotations

import ast
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

PROTO = os.path.join(_ROOT, "services", "subtask_protocol.py")
REFINE = os.path.join(_ROOT, "workflows", "refine.py")
STREAM = os.path.join(_ROOT, "agent", "pipeline_parts", "stream.py")

PASS, FAIL = "PASS", "FAIL"
_results = []
_MUT_ROWS = []
_IN_MUT = [False]


def check(name, ok, detail=""):
    row = (PASS if ok else FAIL, name, "" if ok else str(detail)[:220])
    (_MUT_ROWS if _IN_MUT[0] else _results).append(row)
    print("[%s] %s%s" % ("OK  " if ok else "FAIL", name,
                         ("　— " + detail) if detail and not ok else ""))
    return bool(ok)


def _summ(**kw):
    base = {"task_key": "t1", "status": "full", "summary": "x",
            "artifacts": [], "evidence": [], "risks": [], "claims": [],
            "confidence": 0.5, "meta": {}}
    base.update(kw)
    return base


# ══════════════════════════════════════════════════════════════════════
def d_group():
    print("\n=== D 组 · claims 协议归一化 ===")
    from services import subtask_protocol as sp

    check("D1 枚举三档齐全且默认档为 to_verify",
          tuple(sp.CLAIM_KINDS) == ("fact", "inference", "to_verify")
          and sp.DEFAULT_CLAIM_KIND == "to_verify",
          "CLAIM_KINDS=%s DEFAULT=%s" % (getattr(sp, "CLAIM_KINDS", None),
                                         getattr(sp, "DEFAULT_CLAIM_KIND", None)))

    # D2 兜底方向
    r = sp.validate({"task_key": "t1", "status": "full", "summary": "s",
                     "claims": [{"text": "某结论"}]})
    c0 = (r["sanitized"].get("claims") or [{}])[0]
    check("D2 缺 kind ⇒ 落到 to_verify（最保守）",
          c0.get("kind") == "to_verify", "实测=%r" % c0.get("kind"))

    # D2b 非法 kind 同样落到 to_verify
    r2 = sp.validate({"task_key": "t1", "status": "full", "summary": "s",
                      "claims": [{"text": "x", "kind": "瞎写的"}]})
    c2 = (r2["sanitized"].get("claims") or [{}])[0]
    check("D2b 非法 kind ⇒ 同样落到 to_verify 且给出 warning",
          c2.get("kind") == "to_verify"
          and any("kind" in w for w in r2.get("warnings") or []),
          "kind=%r warnings=%s" % (c2.get("kind"), r2.get("warnings")))

    # D3 缺 text 丢弃；缺 subject 保留
    r3 = sp.validate({"task_key": "t1", "status": "full", "summary": "s",
                      "claims": [{"kind": "fact"}, {"text": "有结论但无锚点"},
                                 {"text": "  "}]})
    cl3 = r3["sanitized"].get("claims") or []
    check("D3 缺 text 丢弃 / 缺 subject 保留",
          len(cl3) == 1 and cl3[0].get("text") == "有结论但无锚点",
          "实测 %d 条：%s" % (len(cl3), cl3))

    # D4 非数组不判非法
    r4 = sp.validate({"task_key": "t1", "status": "full", "summary": "s", "claims": "不是数组"})
    check("D4 claims 非数组不判 ok=False（可选字段不废存量）",
          r4["ok"] is True and (r4["sanitized"].get("claims") == []),
          "ok=%r claims=%r" % (r4["ok"], r4["sanitized"].get("claims")))

    # D5 上限
    r5 = sp.validate({"task_key": "t1", "status": "full", "summary": "s",
                      "claims": [{"text": "长" * 900, "subject": "S" * 120}]
                      + [{"text": "第%d条" % i, "subject": "s"} for i in range(40)]})
    cl5 = r5["sanitized"].get("claims") or []
    check("D5 长度与条数上限生效",
          len(cl5) == sp.MAX_CLAIMS
          and len(cl5[0]["text"]) == sp.MAX_SUMMARY
          and len(cl5[0]["subject"]) == sp.MAX_TITLE,
          "条数=%d text=%d subject=%d" % (len(cl5), len(cl5[0]["text"]),
                                          len(cl5[0]["subject"])))

    # D6 normalize_text 不伪造 claims
    nt = sp.normalize_text("一段正常的纯文本交付物")
    check("D6 纯文本兜底产出的 claims 为空数组（不伪造）",
          nt.get("claims") == [], "实测=%r" % nt.get("claims"))


# ══════════════════════════════════════════════════════════════════════
def c_group():
    print("\n=== C 组 · 冲突检测 ===")
    from services import subtask_protocol as sp

    # C1 数值互斥
    s = [_summ(task_key="t2", claims=[{"text": "转发器有 3 个接口",
                                       "subject": "转发器接口数", "kind": "fact"}]),
         _summ(task_key="t3", claims=[{"text": "转发器有 5 个接口",
                                       "subject": "转发器接口数", "kind": "fact"}])]
    cf = sp.detect_conflicts(s)
    check("C1 同 subject 数值互斥被检出", len(cf) == 1 and cf[0]["kind"] == "value",
          "实测 %d 条：%s" % (len(cf), cf))
    check("C1b 冲突条目带双方归属（可归因到具体子 Agent）",
          bool(cf) and {x["task_key"] for x in cf[0]["claims"]} == {"t2", "t3"},
          "实测=%s" % ([x.get("task_key") for x in cf[0]["claims"]] if cf else None))

    # C2 单位归一 ⇒ 不得误报
    s2 = [_summ(task_key="t2", claims=[{"text": "上下文窗口 128k", "subject": "窗口"}]),
          _summ(task_key="t3", claims=[{"text": "上下文窗口 128000", "subject": "窗口"}])]
    cf2 = sp.detect_conflicts(s2)
    check("C2 单位归一后不误报（128k 与 128000 同一取值）", len(cf2) == 0,
          "实测误报 %d 条：%s" % (len(cf2), cf2))

    # C3 极性互斥
    s3 = [_summ(task_key="t1", claims=[{"text": "上游接口支持批量导入",
                                        "subject": "批量导入能力", "kind": "fact"}]),
          _summ(task_key="t4", claims=[{"text": "上游接口不支持批量导入",
                                        "subject": "批量导入能力", "kind": "fact"}])]
    cf3 = sp.detect_conflicts(s3)
    check("C3 极性相反被检出", len(cf3) >= 1 and cf3[0]["kind"] == "polarity",
          "实测 %d 条：%s" % (len(cf3), cf3))

    # C4 无锚点不参与
    s4 = [_summ(task_key="t1", claims=[{"text": "有 3 个", "kind": "fact"}]),
          _summ(task_key="t2", claims=[{"text": "有 5 个", "kind": "fact"}])]
    check("C4 缺 subject 的 claim 不参与判定", len(sp.detect_conflicts(s4)) == 0,
          "实测=%s" % sp.detect_conflicts(s4))

    # C5 三态口径（★核心）
    st_no = sp.conflict_stats([_summ(claims=[{"text": "无锚点结论"}])])
    st_ins = sp.conflict_stats([_summ(task_key="t1",
                                       claims=[{"text": "a", "subject": "x"}])])
    st_ok = sp.conflict_stats([_summ(task_key="t1", claims=[{"text": "a", "subject": "x"}]),
                               _summ(task_key="t2", claims=[{"text": "b", "subject": "y"}])])
    check("C5 conflict_stats 三态可区分",
          st_no["verdict"] == "no-data" and st_ins["verdict"] == "insufficient"
          and st_ok["verdict"] == "checkable",
          "实测 no=%s ins=%s ok=%s" % (st_no["verdict"], st_ins["verdict"],
                                       st_ok["verdict"]))
    check("C5b '检出 0 冲突' 与 '没数据可判' 不被混同",
          sp.conflict_stats(s)["claims_anchored"] == 2
          and sp.conflict_stats(s4)["verdict"] == "no-data",
          "有锚点时 anchored=%r；无锚点时 verdict=%r"
          % (sp.conflict_stats(s)["claims_anchored"], sp.conflict_stats(s4)["verdict"]))

    # C6 互为子串不报（一详一略）
    s6 = [_summ(task_key="t1", claims=[{"text": "提供接口", "subject": "能力"}]),
          _summ(task_key="t2", claims=[{"text": "提供外部接口", "subject": "能力"}])]
    check("C6 核心词高度重合（一详一略）不报冲突", len(sp.detect_conflicts(s6)) == 0,
          "实测=%s（核心词=%s/%s 重叠=%.2f）"
          % (sp.detect_conflicts(s6), sp._tail_core("提供接口"),
             sp._tail_core("提供外部接口"),
             sp._char_overlap(sp._tail_core("提供接口"), sp._tail_core("提供外部接口"))))

    # C8 ★反向判据：核心词**不**重合 ⇒ 必须报。
    #   ⚠️ 没有这一条，C6 可以靠"把阈值调到 0，永远豁免"来通过 ⇒ 判据被反向锁死。
    s8 = [_summ(task_key="t1", claims=[{"text": "提供批量导入接口", "subject": "能力"}]),
          _summ(task_key="t2", claims=[{"text": "提供温度告警接口", "subject": "能力"}])]
    check("C8 核心词不重合必须报冲突（防 C6 被阈值调成永真）",
          len(sp.detect_conflicts(s8)) == 1,
          "实测 %d 条：%s" % (len(sp.detect_conflicts(s8)), sp.detect_conflicts(s8)))

    # C7 真无冲突
    s7 = [_summ(task_key="t1", claims=[{"text": "3 个接口", "subject": "接口数"}]),
          _summ(task_key="t2", claims=[{"text": "已完成建模", "subject": "进度"}])]
    check("C7 真无冲突时不误报", len(sp.detect_conflicts(s7)) == 0,
          "实测=%s" % sp.detect_conflicts(s7))


# ══════════════════════════════════════════════════════════════════════
def w_group():
    print("\n=== W 组 · 主路径真的调用了 ===")
    from workflows.refine import RefineGate

    # ⚠️ **必须打桩 `_evaluate_content`**：它内部会 `llm_client.chat(...)` 真调 LLM。
    #   离线门禁真调 LLM 会（a）超时（b）烧钱（c）在 CI 无 secret 时直接挂 ——
    #   本门禁要验的是「冲突检测有没有接进 RefineGate」，**不是**验评审质量。
    #   打桩后仍能抓到：conflicts 键是否存在、检出数是否正确。
    #   （这是首版门禁的真实缺陷：直接 run() ⇒ 跑 5 分钟不返回。）
    import workflows.nodes as _nodes
    _orig_eval = _nodes.FlowNodesMixin._evaluate_content
    _seen = {}

    def _fake_eval(content, criteria="", **kw):
        _seen["criteria"] = criteria
        return {"score": 90, "passed": True, "issues": [], "advice": "",
                "_meta": {"provider": "stub", "model": "stub", "used_mock": True}}

    # W1 冲突结果随 run 返回
    items = [
        {"task_key": "t1", "title": "A", "agent_id": "a", "result": "正文",
         "summary": _summ(task_key="t1", claims=[{"text": "有 3 个接口",
                                                  "subject": "接口数"}])},
        {"task_key": "t2", "title": "B", "agent_id": "b", "result": "正文",
         "summary": _summ(task_key="t2", claims=[{"text": "有 9 个接口",
                                                  "subject": "接口数"}])},
    ]
    try:
        _nodes.FlowNodesMixin._evaluate_content = staticmethod(_fake_eval)
        r = RefineGate.run("报告正文", "目标", items, [{"key": "t1"}, {"key": "t2"}])
    except Exception as e:                                   # noqa: BLE001
        r = {}
        check("W1 RefineGate.run 返回 conflicts 键且检出 1 条", False,
              "调用异常：%r" % e)
    finally:
        _nodes.FlowNodesMixin._evaluate_content = _orig_eval
    cf = r.get("conflicts") if isinstance(r, dict) else None
    check("W1 RefineGate.run 返回 conflicts 键且检出 1 条",
          isinstance(cf, dict) and int(cf.get("count") or 0) == 1,
          "实测=%s" % cf)
    check("W1b 冲突项写进 issues（确定性检出不该依赖 LLM 记得写）",
          any("接口数" in str(i) for i in ((r or {}).get("issues") or [])),
          "issues=%s" % ((r or {}).get("issues")))

    # W1c 无冲突时**不得**污染评审标准（提示词逐字节不变 ⇒ 零回归）
    items_ok = [
        {"task_key": "t1", "title": "A", "agent_id": "a", "result": "正文",
         "summary": _summ(task_key="t1", claims=[{"text": "3 个接口", "subject": "接口数"}])},
        {"task_key": "t2", "title": "B", "agent_id": "b", "result": "正文",
         "summary": _summ(task_key="t2", claims=[{"text": "已完成", "subject": "进度"}])},
    ]
    try:
        _nodes.FlowNodesMixin._evaluate_content = staticmethod(_fake_eval)
        r2 = RefineGate.run("报告正文", "目标", items_ok, None)
    finally:
        _nodes.FlowNodesMixin._evaluate_content = _orig_eval
    check("W1c 无冲突时评审标准与默认完全一致（零回归）",
          _seen.get("criteria") == RefineGate.__dict__.get("_DEFAULT", None)
          or _seen.get("criteria", "").startswith("1) 完整性"),
          "实测 criteria 前 40 字=%r" % _seen.get("criteria", "")[:40])

    # W2 三条返回路径字段同键
    src = open(REFINE, encoding="utf-8").read()
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "run"), None)
    rets = [n for n in ast.walk(fn) if isinstance(n, ast.Return)] if fn else []
    with_cf = 0
    for rt in rets:
        if rt.value is None:
            continue
        if any(isinstance(n, ast.Constant) and n.value == "conflicts"
               for n in ast.walk(rt.value)):
            with_cf += 1
    check("W2 三条返回路径都带 conflicts 键（字段同键）",
          len(rets) >= 4 and with_cf == len(rets),
          "return 共 %d 处，带 conflicts 的 %d 处" % (len(rets), with_cf))

    # W3 prompt 契约里必须含 claims + subject（不注入 = 协议永不落地）
    ssrc = open(STREAM, encoding="utf-8").read()
    i = ssrc.find("【交付格式】")
    check("W3a 子任务 prompt 含【交付格式】契约段", i >= 0)
    seg = ssrc[i:i + 2200] if i >= 0 else ""
    check("W3b 契约里要求输出 claims 数组", '"claims"' in seg)
    check("W3c 契约里要求 subject 必填（冲突检测的锚点）",
          "subject" in seg and "必填" in seg)

    # W4 汇总正文按 kind 分档呈现 to_verify
    check("W4 汇总正文按 kind 分档呈现（含待验证字样）",
          "to_verify" in ssrc and "待验证" in ssrc)


# ══════════════════════════════════════════════════════════════════════
def mutation_selfproof():
    print("\n=== 变异自证（证明判据有牙齿）===")
    import importlib
    from services import subtask_protocol as sp

    # M1 detect_conflicts 恒返回 []
    _IN_MUT[0] = True
    orig_det = sp.detect_conflicts
    try:
        sp.detect_conflicts = lambda summaries, max_conflicts=10: []
        c_group()
    finally:
        sp.detect_conflicts = orig_det
        _IN_MUT[0] = False
    _mut_expect("M1", "detect_conflicts 恒返回 []", "C1", "C3")

    # M2 DEFAULT_CLAIM_KIND 改成 fact（兜底方向反了）
    _IN_MUT[0] = True
    ok2 = orig_k = sp.DEFAULT_CLAIM_KIND
    try:
        sp.DEFAULT_CLAIM_KIND = "fact"
        # validate 内部引用的是模块级常量，直接改即可生效
        d_group()
    finally:
        sp.DEFAULT_CLAIM_KIND = ok2
        _IN_MUT[0] = False
    _mut_expect("M2", "DEFAULT_CLAIM_KIND 反向为 fact", "D1", "D2")

    # M4 conflict_stats 的 verdict 恒 checkable
    _IN_MUT[0] = True
    orig_st = sp.conflict_stats
    try:
        sp.conflict_stats = lambda summaries: {
            "claims_total": 0, "claims_anchored": 99, "subjects": 99,
            "subtasks_with_claims": 99, "verdict": "checkable"}
        c_group()
    finally:
        sp.conflict_stats = orig_st
        _IN_MUT[0] = False
    _mut_expect("M4", "conflict_stats 恒返回 checkable", "C5")

    # M3 从契约里删掉 claims —— ⚠️ 必须**复用 W3b 的判据本身**去判变异源码，
    #   否则就成了"另写一条断言再声称它证明了 W3b 有牙齿"（自证不成立）。
    _IN_MUT[0] = True
    try:
        src = open(STREAM, encoding="utf-8").read()
        i = src.find("【交付格式】")
        mutated = src[:i] + src[i:].replace('"claims":[', '"Xclaims":[', 1) \
            if i >= 0 else src
        _mi = mutated.find("【交付格式】")
        _seg = mutated[_mi:_mi + 2200] if _mi >= 0 else ""
        # ★ 与 w_group() 中 W3b 完全同一条判据
        _red = not ('"claims"' in _seg)
        check("M3 变异：契约里删掉 claims 后，W3b 判据必须判红", _red,
              "变异后 '%s' 仍出现在契约段 ⇒ W3b 抓不到（判据空转）" % '"claims"')
        check("M3b 未变异的原契约确实含 claims（否则 M3 无意义）",
              '"claims"' in src[src.find("【交付格式】"):][:2200])
    finally:
        _IN_MUT[0] = False
    # ⚠️ 这里**不**走 `_mut_expect`：M3 的判据本身就是"变异后应判红"，
    #   它判红 = 变异自证成功，是 PASS；再套一层"必须出现 FAIL"会自相矛盾。
    #   （第一版就是这么写的 ⇒ 恒判「判据空转」，是判据自身的缺陷。）


def _mut_expect(tag, desc, *must_fail_prefixes):
    """变异体下，对应判据**必须**出现 FAIL；否则判据空转。"""
    hits = [n for st, n, _ in _MUT_ROWS
            if st == FAIL and any(n.startswith(p) for p in must_fail_prefixes)]
    _MUT_ROWS.clear()
    ok = bool(hits)
    print("[%s] %s %s ⇒ %s 必须判红%s"
          % ("OK  " if ok else "FAIL", tag, desc, "/".join(must_fail_prefixes),
             ("（实际红：%s）" % hits[0]) if hits else "（**未判红 = 判据空转**）"))
    if not ok:
        _results.append((FAIL, "%s %s" % (tag, desc), "变异后判据未判红"))


# ══════════════════════════════════════════════════════════════════════
def main() -> int:
    print("=" * 74)
    print("判据 · 批次三 #16 claims 协议 + #15 结论冲突检测")
    d_group()
    c_group()
    w_group()
    mutation_selfproof()

    npass = sum(1 for st, _, _ in _results if st == PASS)
    nfail = sum(1 for st, _, _ in _results if st == FAIL)
    print("\n" + "=" * 74)
    print("%d pass / %d fail" % (npass, nfail))
    if nfail:
        for st, n, d in _results:
            if st == FAIL:
                print("  - %s%s" % (n, ("　— " + d) if d else ""))
        return 1
    print("✅ 全部通过：claims 协议可落地 + 冲突检出有牙齿 + 主路径已接线")
    return 0


if __name__ == "__main__":
    sys.exit(main())
