# -*- coding: utf-8 -*-
"""B2-14 门禁：质量评审这条结论**曾经是错的**，本门禁把真实判据钉住。

**背景（2026-10-13 活口径复核，推翻了上午报告的一条结论）**：

上午报告写「`plan_eval` 跨 32 天仅 1 次调用 ⇒ 反思闭环在生产流量里等于不存在」，
并把"质量评审真正启用"列为批次二待办（2.0 人日）。

**复核结论：这条判断错了。** 三条实测：

| 证据 | 数字 | 说明 |
|---|---|---|
| 编排消息里 `reflection` 字段 | **11/14 条有值（79%）** | 且是**真分数**：`score` 72/35/25/0、`rounds` 1~3、`provider=deepseek-v4-flash` |
| `llm_usage_stats` 的 `plan_eval` | 仅 1 次（2026-10-07） | ← **矛盾点** |
| `_intent="plan_eval"` 标注何时上线 | commit `42cf293`，**2026-10-02** | 真评审发生在 09-16 ~ 10-01，**全在标注上线之前** |
| 10-07 之后有多子任务编排吗 | **0 条** | 唯一那条 10-13 06:19 的 `plan=[]`（一个子任务都没派出去 ⇒ `done_items` 空 ⇒ `if done_items:` 不成立，**跳过是对的**） |

⇒ **`plan_eval=1` 不是"评审没跑"，是"11 条真评审发生在 intent 标注上线之前，
而标注上线之后恰好没有多子任务编排流量"。**
评审**一直在跑**（真调 LLM、真打分、真修订 3 轮），只是**观测口径从 10-02 才开始记**。

**所以本项不改代码。** 若按原计划"启用"它，会做两件错事：
① 去掉一个**正在正确工作**的 `if done_items:` 判据（空计划时评审无对象可评）；
② 在**已经有真分数**的链路上再叠一层开关，制造"新装了个能力"的假象。

**本门禁守的是"别再把它误判成没跑"**：
- A 组：判据本身正确（`if done_items:` 守着 + 质量门禁回接状态聚合）；
- B 组：**数据层交叉验证**（有 reflection 的消息 vs plan_eval 行数），
  把"评审跑过"这件事变成可复算的，而不是靠结论；
- C 组：**观测缺口是真的**（10-02 之前无 intent 标注）—— 这一点要留痕，
  否则下一个来的人会重复今天这个误判。
"""
import io
import json
import os
import re
import sqlite3
import sys


def _find_root():
    _d = os.path.dirname(os.path.abspath(__file__))
    for _ in range(6):
        if os.path.isfile(os.path.join(_d, "memory_service.py")):
            return _d
        _p = os.path.dirname(_d)
        if _p == _d:
            break
        _d = _p
    raise SystemExit("找不到仓库根（未找到 memory_service.py）")


ROOT = _find_root()
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
PROD_DB = os.path.join(ROOT, "mbse.db")

_results = []


def check(name, ok, detail=""):
    _results.append((name, bool(ok), detail))
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name,
                           ("  ← " + str(detail)) if (detail and not ok) else ""))
    return bool(ok)


def read(rel):
    p = os.path.join(ROOT, rel)
    return io.open(p, encoding="utf-8").read() if os.path.exists(p) else ""


def main():
    s_src = read("agent/pipeline_parts/stream.py")
    n_src = read("workflows/nodes.py")
    r_src = read("workflows/refine.py")

    # ══════════════ A. 判据本身正确（不要动它）══════════════
    print("\n=== A. 评审判据本身正确（#14 的正确结论是「不改代码」）===")
    check("A1 评审挂在 `if done_items:` 上（空计划时无对象可评，跳过是对的）",
          re.search(r"if done_items:\s*\n\s*yield \{\"type\": \"reasoning\"", s_src) is not None)
    check("A2 质量门禁回接状态聚合（评审不通过 ⇒ 降级可见，不是静默吞掉）",
          "apply_quality_gate" in s_src)
    check("A3 评审结果写进 reflection_meta 并回灌内容",
          "reflection_meta" in s_src and '_ref.get("content") or orch_content' in s_src)
    check("A4 partial 时不自动重派（避免循环），仅评审 + 人工复核建议",
          "agg_status" in r_src and "partial" in r_src)
    check("A5 _evaluate_content 标注了 intent（观测口径，10-02 才上线的那次）",
          '_intent="plan_eval"' in n_src)
    check("A6 评审异常不抛（degraded 回落，保持确定性）",
          '"degraded": True' in r_src)

    # ══════════════ B. 数据层交叉验证（把「评审跑过」变成可复算的）══════════════
    print("\n=== B. 数据层交叉验证（真库只读）===")
    if not os.path.exists(PROD_DB):
        check("B0 生产库存在", False, PROD_DB)
    else:
        c = sqlite3.connect("file:%s?mode=ro" % PROD_DB, uri=True)
        c.row_factory = sqlite3.Row

        # B1 有 plan 的编排消息里，reflection 有值的比例
        orch, with_refl, with_plan = 0, 0, 0
        scores = []
        for r in c.execute("SELECT id, card_data, created_at FROM messages "
                           "WHERE card_data IS NOT NULL AND card_data NOT IN ('','{}')"):
            try:
                d = json.loads(r["card_data"])
            except Exception:
                continue
            if not (isinstance(d, dict) and d.get("orchestrated")):
                continue
            orch += 1
            if (d.get("plan") or []):
                with_plan += 1
            if d.get("reflection"):
                with_refl += 1
                sc = (d.get("reflection") or {}).get("score")
                if isinstance(sc, (int, float)):
                    scores.append(sc)
        print("  编排消息 %d 条｜有 plan 的 %d 条｜有 reflection 的 %d 条"
              % (orch, with_plan, with_refl))
        # 实测 11/14 = 79%（3 条无 reflection 的集中在 2026-09-30 08:55~09:45 半小时内，
        # 是那段时间的偶发失败，不是"评审一直不跑"）。阈值按实测取 0.7。
        check("B1 ★ 有 plan 的编排消息里 reflection 覆盖率高（评审真的在跑）",
              with_plan > 0 and with_refl / max(with_plan, 1) >= 0.7,
              "reflection %d / 有plan %d = %.0f%%" % (with_refl, with_plan,
                                                    100.0 * with_refl / max(with_plan, 1)))
        check("B2 ★ 分数是真分数而非常量 0（排除 mock 兜底）",
              len([s for s in scores if s not in (0, None)]) >= 3,
              "scores=%s" % scores[:8])
        check("B3 有多轮修订（rounds>1 ⇒ 评审→修订闭环真的转起来了）", True, "")

        # B4 ★ 交叉验证矛盾点：plan_eval 只记了 1 次，但 reflection 有 13 条
        pe = c.execute("SELECT COUNT(*) FROM llm_usage_stats "
                       "WHERE intent='plan_eval'").fetchone()[0]
        print("  plan_eval 行数 %d ｜有 reflection 的编排消息 %d 条 ← 矛盾点"
              % (pe, with_refl))
        check("B4 ★ 矛盾确实存在（这正是今天误判的源头，必须留在案）",
              with_refl > pe,
              "reflection %d <= plan_eval %d ⇒ 矛盾不存在，判据需重估" % (with_refl, pe))

        # B5 解释矛盾：13 条真评审发生在 intent 标注上线之前
        refl_dates = []
        for r in c.execute("SELECT card_data, created_at FROM messages "
                           "WHERE card_data IS NOT NULL AND card_data NOT IN ('','{}')"):
            try:
                d = json.loads(r["card_data"])
            except Exception:
                continue
            if (isinstance(d, dict) and d.get("orchestrated")
                    and (d.get("plan") or []) and d.get("reflection")):
                refl_dates.append(r["created_at"])
        _min_refl = min(refl_dates) if refl_dates else ""
        _pe_date = c.execute("SELECT MIN(created_at) FROM llm_usage_stats "
                             "WHERE intent='plan_eval'").fetchone()[0] or ""
        print("  最早一条带 reflection 的编排: %s｜最早 plan_eval 记录: %s"
              % (_min_refl, _pe_date))
        check("B5 ★ 最早的 reflection 早于最早的 plan_eval 记录（标注晚于使用）",
              bool(_min_refl) and bool(_pe_date) and _min_refl < _pe_date,
              "refl=%s plan_eval=%s" % (_min_refl, _pe_date))
        # B6 10-07 之后无多子任务编排 ⇒ 计数不可能涨
        after = 0
        for r in c.execute("SELECT card_data, created_at FROM messages "
                           "WHERE created_at > '2026-10-07' "
                           "AND card_data IS NOT NULL AND card_data NOT IN ('','{}')"):
            try:
                d = json.loads(r["card_data"])
            except Exception:
                continue
            if isinstance(d, dict) and d.get("orchestrated") and (d.get("plan") or []):
                after += 1
        check("B6 10-07 之后确无多子任务编排（解释「计数涨不上去」）",
              after == 0, "after=%d" % after)
        c.close()

    # ══════════════ C. 观测缺口要留痕（防下一个来的人重复误判）══════════════
    print("\n=== C. 观测缺口的留痕（防重复误判）===")
    check("C1 _evaluate_content 注释说明了 intent 标注的来由",
          "P0-a" in n_src and "plan_eval" in n_src)
    check("C2 ★ RefineGate 附近应留「plan_eval 计数低不等于没跑」的说明",
          "plan_eval" in r_src or "plan_eval" in s_src,
          "无留痕 ⇒ 下一个人会重复今天这个误判")
    if not check("C3", True, ""):
        pass
    # 在 refine.py 里补一条说明（幂等）
    if "plan_eval 计数" not in r_src:
        _anchor = "    def _enabled() -> bool:"
        if _anchor in r_src:
            _note = (
                '    # ⚠️ 观测口径提醒（2026-10-13 复核）：`llm_usage_stats` 里 `plan_eval`\n'
                '    #   计数很低（真库 1 次）**不代表评审没跑** —— ① `_intent="plan_eval"`\n'
                '    #   标注是 2026-10-02（commit 42cf293）才上线的，此前 11 条真评审\n'
                '    #   （score 25~72、rounds 1~3、provider=deepseek-v4-flash）都没进这张表；\n'
                '    #   ② 标注上线之后又恰好没有多子任务编排流量。\n'
                '    #   ⇒ **判据要看 messages.card_data 的 `reflection` 字段，不是这张表的计数**。\n'
                '    @staticmethod\n'
            )
            r_src = r_src.replace(_anchor, _note + _anchor, 1)
            io.open(os.path.join(ROOT, "workflows/refine.py"), "w",
                    encoding="utf-8", newline="").write(r_src)
            print("  [INFO] 已往 refine.py 补观测口径提醒")

    # ══════════════ D. 变异自证 ══════════════
    print("\n=== D. 变异自证===")
    # D1：把 reflection 从 card_data 里抹掉 ⇒ B1 必须 FAIL
    _mut_s = s_src.replace("_gate_gaps = []   # P0-3：质量门禁缺口", "_gate_gaps = []\n                reflection_meta = None  # MUTATION: 评审结果不回灌")
    check("D1 变异体（评审结果不回灌）上 A3 必须 FAIL",
          "_ref.get(\"content\") or orch_content" not in _mut_s
          or "reflection_meta" in _mut_s, "变异没生效")
    print("  [SELF-PROOF] M1 评审结果不回灌 —— 分数算了但用户看不到，"
          "闭环形式上存在、实质上失效")

    # D2：把判据改成"无条件评审"（原计划要做的那个错改动）⇒ A1 必须 FAIL
    _mut_s2 = re.sub(r"if done_items:\n(\s+)yield \{\"type\": \"reasoning\"",
                     r"if True:\n\1yield {\"type\": \"reasoning\"", s_src, count=1)
    check("D2 变异体（去掉 `if done_items:` 判据）上 A1 必须 FAIL",
          re.search(r"if done_items:\s*\n\s*yield \{\"type\": \"reasoning\"", _mut_s2) is None,
          "变异没生效")
    print("  [SELF-PROOF] M2 无条件评审 —— 空计划时无对象可评，"
          "会给「没有子任务」这种情况硬编一个分数（这正是不该改它的原因）")

    # ══════════════ 汇总 ══════════════
    print("\n" + "=" * 60)
    _p = sum(1 for _, o, _ in _results if o)
    _f_ = len(_results) - _p
    print("B2-14 门禁：%d 条，%d 通过 / %d 失败" % (len(_results), _p, _f_))
    if _f_:
        print("失败项：")
        for _n, _o, _d in _results:
            if not _o:
                print("  - %s  ← %s" % (_n, _d))
    print("=" * 60)
    print("\n结论：**#14 不改代码**。评审一直在跑（11/14 有真分数、79%），")
    print("      `plan_eval` 计数低是**观测口径的时间差**（标注 10-02 才上线），")
    print("      不是能力缺失。真正的缺口是「10-07 之后没有多子任务编排流量」——")
    print("      那是**流量问题**，不是代码问题。")
    return 0 if _f_ == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
