# -*- coding: utf-8 -*-
"""SysML v2「单引号 ≠ 字符串」生成端规则 + 校验停滞归因门禁（2026-11-23）。

## 背景（真机会话 conv 1000000000，活动视图任务）

用户报「校验工具又短路了，任务无法快速完成」。复核到的事实链：

1. 生成端把分支比较值写成 **单引号**：`if '判定'.'结果' == '允许' then …`；
2. checker.jar 报 **10 条语义错**：`Couldn't resolve reference to Element ''允许''`
   —— 因为 SysML v2 / KerML 里**单引号是「转义标识符」**，只有双引号才是字符串字面量；
3. agent 按「语义：引用/类型族不成立」的方向改（改端口承载），**没改引号** ⇒ 条数恒为 10；
4. `loop_guard.validation_stalled()` 的签名**只含计数**（词法0/语法0/语义10/report）⇒ 3 次判停滞；
5. 原短路回喂文案断言「根因不在本次代码改动，而是环境级问题（标准库未加载）」⇒
   **错误归因被写进给模型的提示**，最终交付写「校验未能完成 / 根因在校验环境」。

## 本门禁守什么

  Q1  L0 硬约束卡里必须有「单引号是标识符、双引号才是字符串」这条（生成端止血）
  Q2  出处表登记 `S02`（本仓纪律：卡里每条都要有实测编号）
  Q3  停滞回喂文案**不得**再断言「根因在环境」，必须给可执行自查步骤
  Q4  真机 A/B：同一份 285 行活动视图，单引号 ⇒ 语义错 ≥10；改成双引号 ⇒ **0 错 / pass**
      （A 是"夹具真的能触发"，B 是"改法真的能修好"；缺任一半结论都不成立）

## 变异（必须判红）

  M1 抹掉 Q1 的卡片规则        ⇒ Q1 红
  M2 文案改回「根因在环境」     ⇒ Q3 红
  M3 夹具预先把引号改成双引号   ⇒ Q4a 红（证明 A 组不是空转）

## 降级

  checker.jar 不可用（无 java / 无 jar）⇒ Q4 打印 SKIP 并明示「不计入通过」，
  绝不静默当绿。Q1–Q3 + 变异仍必须通过。
"""
from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CARD = REPO / "agent" / "pipeline_parts" / "v2_constraints.py"
GUARD = REPO / "agent" / "loop_guard.py"
FIX = REPO / "tools" / "verify" / "fixtures" / "sysml_activity_quote_285.sysml"

_p, _f, _s = [], [], []


def ck(cond, msg, detail=""):
    (_p if cond else _f).append(msg)
    print(("  [PASS] " if cond else "  [FAIL] ") + msg + (("  «%s»" % detail) if detail and not cond else ""))


def skip(msg):
    _s.append(msg)
    print("  [SKIP] %s（**不计入通过**）" % msg)


def main() -> int:
    print("=" * 78)
    print("SysML v2 单引号/字符串 规则与停滞归因门禁")
    print("=" * 78)

    card = CARD.read_text(encoding="utf-8") if CARD.exists() else ""
    guard = GUARD.read_text(encoding="utf-8") if GUARD.exists() else ""
    ck(bool(card), "F0 读到 L0 约束卡", "len=%d" % len(card))
    ck(bool(guard), "F0b 读到 loop_guard", "len=%d" % len(guard))
    if _f:
        print("\n结论：FAIL（源码未取到）")
        return 1

    # ── Q1 卡片规则 ──
    has_rule = ("单引号" in card and "双引号" in card
                and "标识符" in card and '== "允许"' in card)
    ck(has_rule, "Q1 L0 卡含「单引号是标识符、双引号才是字符串」规则（含正反例）")

    # ── Q2 出处登记 ──
    ck(re.search(r"\|\s*\*{0,2}`?S02`?\*{0,2}\s*\|", card) is not None,
       "Q2 出处表登记 S02（本仓纪律：每条规则要有实测编号）")

    # ── Q3 停滞文案中性化 ──
    bad1 = "根因不在本次代码改动" in guard
    bad2 = "而是环境级问题（如校验器标准库未加载）" in guard.replace(" ", "")
    ck(not bad1, "Q3a 回喂文案不再断言「根因不在本次代码改动」")
    ck(not bad2, "Q3b 回喂文案不再断言「而是环境级问题（标准库未加载）」")
    ck("不等于根因在环境" in guard and "自查" in guard,
       "Q3c 文案改为中性自查（明说条数不变 ≠ 环境问题 + 给步骤）")

    # ── Q4 真机 A/B ──
    if not FIX.exists():
        ck(False, "Q4 夹具存在", str(FIX))
    else:
        code = FIX.read_text(encoding="utf-8")
        n_sq = len(re.findall(r"==\s*'([^']+)'", code))
        ck(n_sq == 10, "Q4a 夹具含 10 处单引号比较值（真实会话原样）", "n=%d" % n_sq)
        try:
            # ⚠️ 门禁在 tools/verify 下运行，sys.path[0] 不是仓库根 ⇒ 必须先补，否则 import 必失败、
            #    被下面的 except 吞成"不可用"⇒ 真机 A/B 恒 SKIP（首版就这么假跳过了一次）
            if str(REPO) not in sys.path:
                sys.path.insert(0, str(REPO))
            import sysml_v2_check as V
            # 只用 available()（它检查内置 JAVA/JAR/LIB 三个绝对路径）；
            # 不要再加 shutil.which("java") —— 本机 java 不在 PATH，会把"可用"误判成"不可用"
            avail = bool(V.available())
        except Exception as ex:
            print("  [warn] import sysml_v2_check 失败：%s: %s" % (type(ex).__name__, str(ex)[:120]))
            avail, V = False, None
        if not avail:
            skip("Q4b/Q4c checker.jar 或 java 不可用，跳过真机 A/B")
        else:
            ra = V.check_code(code, use_cache=False)
            ea = [e for e in (ra.get("errors") or []) if e.get("path") == "semantic"]
            unres = [e for e in ea if "Couldn't resolve reference" in (e.get("msg") or "")]
            ck(len(ea) >= 10 and len(unres) >= 10,
               "Q4b 单引号 ⇒ ≥10 条语义错且为 Couldn't resolve reference",
               "semantic=%d unres=%d" % (len(ea), len(unres)))

            code2 = re.sub(r"==\s*'([^']+)'", lambda m: '== "%s"' % m.group(1), code)
            rb = V.check_code(code2, use_cache=False)
            eb = rb.get("errors") or []
            ck(len(eb) == 0 and V.verdict(rb) == "pass",
               "Q4c 同一份代码改成双引号 ⇒ 0 错 / 判据 pass",
               "errs=%d verdict=%s" % (len(eb), V.verdict(rb)))

            # M3 夹具如果本来就写成双引号，A 组会假绿 ⇒ 必须能判红
            ck(len(ea) > 0, "M3 若夹具已是双引号，Q4b 必红（A 组非空转）",
               "semantic=%d" % len(ea))

    # ── 静态变异（不需要真机） ──
    print("\n-- 变异（必须判红） --")
    # M1 把卡片里的正例（== "允许"）改掉 ⇒ Q1 的正反例判据必须失效
    #   （首版变异只删了标题行，正例行还在 ⇒ 变异恒不翻转 = 空转，已改）
    m1 = card.replace('== "允许"', '== "X"')
    _m1_hit = ("单引号" in m1 and "双引号" in m1
               and "标识符" in m1 and '== "允许"' in m1)
    print("  [%s] M1 抹掉卡片正例 ⇒ Q1 判红" % ("PASS" if not _m1_hit else "FAIL"))
    if _m1_hit:
        _f.append("变异 M1 未按预期判红（卡片正例仍可命中）")

    m2 = guard.replace("不等于根因在环境",
                       "说明根因不在本次代码改动，而是环境级问题（如校验器标准库未加载）")
    print("  [%s] M2 文案改回「根因在环境」⇒ Q3 判红"
          % ("PASS" if ("根因不在本次代码改动" in m2 and "不等于根因在环境" not in m2) else "FAIL"))
    if not ("根因不在本次代码改动" in m2 and "不等于根因在环境" not in m2):
        _f.append("变异 M2 未按预期判红")

    print("\n" + "=" * 78)
    tail = ("跳过 %d 项（不计入通过）：%s" % (len(_s), "；".join(_s))) if _s else "无跳过"
    print("结论：%s（PASS %d / FAIL %d / %s）"
          % ("OK" if not _f else "FAIL", len(_p), len(_f), tail))
    if _f:
        for m in _f:
            print("  ✗ " + m)
    print("=" * 78)
    return 0 if not _f else 1


if __name__ == "__main__":
    sys.exit(main())
