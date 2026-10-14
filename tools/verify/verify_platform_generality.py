# -*- coding: utf-8 -*-
"""verify_platform_generality —— 平台能力**通用性**门禁（米爸 2026-10-14 定位新增）。

## 为什么要有这条门禁（它拦的是一类**特定**缺陷）
米爸明确：优化要针对 **AI 平台工程能力**（通用 / 稳定 / 健壮），
**不是针对特定数据或特定模型的适配**。

按这个定位回查批次三，发现 #17（交付物结构校验）**在两处接线点传错了输入**：
物化与汇总都把「协议里的 artifact 条目」（只有 `kind/title/ref`）直接送进校验器，
而校验器要的是**正文** ⇒ **每份交付物都会误报「内容为空」**。

⚠️ 这不是"校验器不работа"，而是**假红**：
   模型没按协议给结构化字段是常态，但正文**一直都在**（`sub_res` / `result`）。
   ⇒ 判据必须验「输入取自正文而非协议字段」。

## 判据（三条都是结构性的，不依赖任何具体数据）
### G 组 · 通用性（正文回落）
  G1 物化接线：传给 `validate_artifact` 的对象必须能取到**正文**
     （来自协议正文字段，**或**回落到 `sub_res`）
  G2 汇总接线：同上（回落到 `_it['result']`）
  G3 ★**反向判据**：把回落逻辑去掉 ⇒ G1/G2 必须判红
     （否则可以靠"永远传不到正文"来通过 ⇒ 判据被反向锁死）

### S 组 · 确定性（不依赖模型是否配合）
  S1 `artifact_validator` 的判据**不得调用 LLM**（纯字面/结构判定）
  S2 校验器对**任意**文本都能给出判定（不挑特定内容）——
     用 3 组彼此无关的样例（SysML 报告 / 需求条目 / 散文），至少 2 组能出结论

### R 组 · 健壮性（异常不阻断）
  R1 校验调用被 `try` 包住且**降级不抛**
  R2 校验结果缺失时，**不得**报成"结构不合规"（那是不可判定，不是缺陷）

## 用法（必须用项目 venv）
    ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_platform_generality.py
"""
from __future__ import annotations

import ast
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

STREAM = os.path.join(_ROOT, "agent", "pipeline_parts", "stream.py")
VALIDATOR = os.path.join(_ROOT, "services", "artifact_validator.py")
ORCH = os.path.join(_ROOT, "agent", "pipeline_parts", "orchestration.py")

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


def g_group():
    print("\n=== G 组 · 通用性（正文回落）===")
    src = open(STREAM, encoding="utf-8").read()

    # G1 物化接线（**向后**扫：structure_check 之前就是接线代码）
    # ⚠️ 窗口用**字符**而非行数：固定行数会随代码增删而失效（R2 已栽过一次）。
    i = src.find("structure_check")
    seg1 = src[i:i + 300] if i >= 0 else ""
    seg1b = src[max(0, i - 1800):i] if i >= 0 else ""
    check("G1 物化接线回落到子任务正文（sub_res）",
          bool(seg1b) and ("sub_res" in seg1b) and ('"content"' in seg1b),
          "物化段未找到 sub_res 回落")
    check("G1b 物化接线只在协议字段无正文时才回落（不覆盖协议正文）",
          bool(seg1b) and ("content" in seg1b and "text" in seg1b and "body" in seg1b),
          "未见多字段探测（content/text/body/markdown/value）")

    # G2 汇总接线
    j = src.find("结构体检未通过")
    seg2 = src[max(0, j - 1800):j + 200] if j >= 0 else ""
    check("G2 汇总接线回落到交付物正文（result）",
          bool(seg2) and ('_it.get("result")' in seg2 or "_it.get('result')" in seg2),
          "汇总段未找到 result 回落")

    # G3 反向判据：去掉回落 ⇒ 必须判红
    _IN_MUT[0] = True
    try:
        mutated = src.replace("_body_fallback", "__GONE__").replace(
            '_bd = str(_it.get("result") or "").strip()', '_bd = ""')
        check("M1 去掉正文回落后 G1/G2 必须判红",
              not ("_body_fallback" in mutated and 'sub_res' in mutated),
              "回落变量被改名后仍被判为有回落 ⇒ G1/G2 对本缺陷无灵敏度")
    finally:
        _IN_MUT[0] = False


def s_group():
    print("\n=== S 组 · 确定性（不依赖模型）===")
    from services import artifact_validator as av
    src = open(VALIDATOR, encoding="utf-8").read()

    # S1 判据不得调 LLM（查**真实调用**，不查注释里的 "LLM" 字样）
    tree = ast.parse(src)
    llm_calls = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            f = n.func
            name = getattr(f, "attr", None) or getattr(f, "id", None) or ""
            if "chat" in str(name).lower() or "llm" in str(name).lower():
                llm_calls.append(str(name))
    imported_llm = bool(re.search(
        r"^\s*(?:from|import)\s+(?:llm|openai|anthropic)\b", src, re.M))
    check("S1 校验器不调用 LLM（纯字面/结构判定）",
          not llm_calls and not imported_llm,
          "调用=%s import=%s" % (llm_calls, imported_llm))

    # S2 对任意文本都能给结论（3 组无关样例，至少 2 组出结论）
    samples = {
        "sysml_报告": "# 建模报告\n\n## 结论\n模型共 12 个部件，校验通过。",
        "需求条目": "REQ-007 系统应当在下发指令前校验操作者权限；验收：越权请求全部拒绝。",
        "散文片段": "今天天气不错，我们去公园走走吧，顺便买点水果。",
    }
    got = {}
    for k, txt in samples.items():
        r = av.validate_artifact("report", {"content": txt})
        got[k] = int(r.get("checked") or 0) > 0
    check("S2 对任意文本都能给判定（至少 2/3 组出结论）",
          sum(got.values()) >= 2, "实测 %s" % got)

    # S3 空输入必须判红（不许"没内容所以没错"）
    r = av.validate_artifact("report", {})
    check("S3 空输入判红（不可判定的最坏情形也不放过）",
          r.get("ok") is False, "实测 ok=%r" % r.get("ok"))


def r_group():
    print("\n=== R 组 · 健壮性（异常不阻断）===")
    src = open(STREAM, encoding="utf-8").read()
    i = src.find("structure_check")
    seg = src[max(0, i - 300):i + 600] if i >= 0 else ""
    check("R1 校验调用被 try 包住且降级不抛",
          "except Exception" in seg and "pass" in seg,
          "物化段未见 try/except 降级")

    # R2 「内容为空」不得报成结构不合规
    # ⚠️ 扫描窗口要够宽：判据在「结构体检未通过」**之后**约 700 字符处
    #   （中间夹着回落逻辑）。首版用「往前 1200 行」⇒ 根本没扫到 ⇒ **判红的是判据自己**。
    #   ⇒ 改用**字符距离**判定，且窗口必须大于实测距离。
    j = src.find("结构体检未通过")
    seg2 = src[j:j + 1200] if j >= 0 else ""
    check("R2 不可判定（无正文）不报成「结构不合规」",
          "内容为空" in seg2,
          "汇总段未区分「无正文」与「结构不合规」⇒ 会产生假红")


    # R3 ★**三条编排路径都要有能力口径**（防"同名能力多路径"再现）
    #    复用路径 `_stream_orchestrated` **不执行子任务** ⇒ 结构化能力不生效；
    #    若只有主路径标了口径，复用路径仍是**用户看不出的盲区**。
    paths = (
        ("stream.py 主路径", src, '"mode": "subtasks_executed"'),
        ("stream.py 复用路径", src, '"mode": "template_reuse"'),
        ("orchestration.py 非流式", open(ORCH, encoding="utf-8").read(),
         '"mode": "subtasks_executed"'),
    )
    for label, text, mode in paths:
        check("R3 %s 有 capability_scope 标记" % label,
              "capability_scope" in text and mode in text,
              "未找到 capability_scope 或 mode=%s" % mode)
    # R3b 复用路径必须显式声明"未执行子任务"
    check("R3b 复用路径显式声明未执行子任务（用户能看出区别）",
          '"subtasks_executed": False' in src and '"mode": "template_reuse"' in src,
          "复用路径未声明 subtasks_executed=False")


def main() -> int:
    print("=" * 72)
    print("判据 · 平台通用性（米爸 2026-10-14 定位：通用 / 稳定 / 健壮）")
    g_group()
    s_group()
    r_group()
    npass = sum(1 for st, _, _ in _results if st == PASS)
    nfail = sum(1 for st, _, _ in _results if st == FAIL)
    print("\n" + "=" * 72)
    print("%d pass / %d fail" % (npass, nfail))
    if nfail:
        for st, n, d in _results:
            if st == FAIL:
                print("  - %s%s" % (n, ("　— " + d) if d else ""))
        return 1
    print("✅ 平台能力通用性成立：判据不依赖特定数据/模型，异常不阻断")
    return 0


if __name__ == "__main__":
    sys.exit(main())
