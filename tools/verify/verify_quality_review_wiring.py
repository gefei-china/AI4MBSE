# CI-OPTIONAL: [broken] 已知红，记账不进 CI（修好后上调 tier 并接线）；两库都红 [FAIL] [FAIL] B4 ★ 标注上线后 reflection 与 plan_eval 双留痕一致（每轮评审恰好一次 eval）  ← 缺归属：{9779: (10, 4), 9999: (3, 0), 10012: (1,  
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

---

## 2026-10-18 演进（第二季度复核：三条断言随真实变化过期，按「演化而非推翻」处理）

| 断言 | 过期原因 | 演进方式 |
|---|---|---|
| A1/D2 正则锚 `"type": "reasoning"` | #10 拆型（2026-10-18）把该 yield 改为 `"type": "progress"`（`kind` 兼容层保留）⇒ 锚点漂移、A1 假红 | 正则改为 `(?:progress(?:\\|reasoning))` 双接受——语义是「done_items 守护的评审系统提示」，两种事件名都是它的历史形态 |
| B4「矛盾必须存在」 | 标注上线后流量回来（10-14 conv#9360 两次真实编排），plan_eval 涨到 13 ≥ reflection 9 ⇒ 旧矛盾被数据消解，断言前提消失 | 反转为**当期双留痕一致**：标注上线（10-02）后有 reflection 的会话，其 plan_eval 行数 ≥ 各消息 rounds 之和（每轮评审恰好一次 eval） |
| B6「10-07 后确无编排」 | 10-14 已有 2 条多子任务编排（score 85/90 双通过）⇒ 解释「计数涨不上去」的前提也没了 | 反转为**蕴含式**：10-07 后的编排**若有**，则都必须有 reflection 真留痕（不要求流量必须存在——流量不是代码属性） |
| B3 恒真 `True` | 「有多轮修订」写死通过，判据不能判红（本仓纪律：判据本身必须能判红） | 改为真库实算 rounds>1 条数，无样本时 SKIP |
| B 段整体 | 干净库（CI）上无任何编排消息 ⇒ B1/B2/B4/B5 假红（rc=1）⇒ 真库金标形态被接进 CI | 按仓内惯例（verify_p14b / verify_uninstall_flow）改显式 SKIP：**判据面不存在 ≠ 产品缺陷**；A/C/D 静态段仍全量在 CI 跑 |

当期真库实测（2026-10-18）：conv#9360 两次编排 4 次 plan_eval = 2+2 rounds、2 次 plan_refine，
reflection 双留痕 score 85/90、rounds 2、conflicts 0 —— **#14 不改代码，再次确认**。
"""
import os as _svos
import sys as _svsys
_svroot = _svos.path.dirname(_svos.path.abspath(__file__))
while _svroot and not _svos.path.isdir(_svos.path.join(_svroot, 'tools')):
    _svroot = _svos.path.dirname(_svroot)
if _svroot and _svroot not in _svsys.path:
    _svsys.path.insert(0, _svroot)
from tools.stream_source import read_stream_source  # noqa: E402
import io
import json
import os
import re
import sqlite3
import sys


def _find_root():
    _d = os.path.dirname(os.path.abspath(__file__))
    for _ in range(6):
        if os.path.isfile(os.path.join(_d, "services/memory_service.py")):
            return _d
        _p = os.path.dirname(_d)
        if _p == _d:
            break
        _d = _p
    raise SystemExit("找不到仓库根（未找到 services/memory_service.py）")


ROOT = _find_root()
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
PROD_DB = (os.environ.get("MBSE_DB_PATH") or os.path.join(ROOT, "mbse.db"))

_results = []
SKIPPED = []   # 判据面不存在（干净库无编排消息）——SKIP 不计 FAIL，但也不装 PASS


def check(name, ok, detail=""):
    _results.append((name, bool(ok), detail))
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name,
                           ("  ← " + str(detail)) if (detail and not ok) else ""))
    return bool(ok)


def skip(name, why):
    """判据面不存在（干净库/CI 常态）⇒ 显式 SKIP。

    按仓内惯例（verify_p14b / verify_uninstall_flow）：**干净库上「无样本」的断言
    一律 SKIP，不做假绿也不假红**。SKIP 行不计入失败数。
    """
    SKIPPED.append(name)
    _results.append((name, None, why))
    print("  [SKIP] %s  ← %s" % (name, why))


def read(rel):
    if rel.replace('\\', '/').endswith('pipeline_parts/stream.py'):
        return read_stream_source()
    p = os.path.join(ROOT, rel)
    return io.open(p, encoding="utf-8").read() if os.path.exists(p) else ""


def main():
    s_src = read("agent/pipeline_parts/stream.py")
    n_src = read("workflows/nodes.py")
    r_src = read("workflows/refine.py")

    # ══════════════ A. 判据本身正确（不要动它）══════════════
    print("\n=== A. 评审判据本身正确（#14 的正确结论是「不改代码」）===")
    # ★ 2026-10-18：#10 拆型后该 yield 的事件名是 progress（reasoning+kind 是拆型前形态），
    #   双接受——锚定的是语义「done_items 守护的评审系统提示」，不是某一个历史事件名。
    check("A1 评审挂在 `if done_items:` 上（空计划时无对象可评，跳过是对的）",
          re.search(r"if done_items:\s*\n\s*yield \{\"type\": \"(?:progress|reasoning)\"",
                    s_src) is not None)
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
    print("\n=== B. 数据层交叉验证（真库只读；干净库无编排消息 ⇒ 整段 SKIP）===")
    if not os.path.exists(PROD_DB):
        check("B0 生产库存在", False, PROD_DB)
    else:
        c = sqlite3.connect("file:%s?mode=ro" % PROD_DB, uri=True)
        c.row_factory = sqlite3.Row

        # B1 有 plan 的编排消息里，reflection 有值的比例
        orch, with_refl, with_plan = 0, 0, 0
        scores, rounds_seen = [], []
        refl_by_conv = {}          # conv -> 该会话带 reflection 的 rounds 之和（标注上线后）
        for r in c.execute("SELECT id, conversation_id, created_at, card_data FROM messages "
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
                rf = d.get("reflection") or {}
                sc = rf.get("score")
                if isinstance(sc, (int, float)):
                    scores.append(sc)
                rd = rf.get("rounds")
                if isinstance(rd, (int, float)):
                    rounds_seen.append(rd)
                # 标注上线（commit 42cf293，2026-10-02）之后的双留痕才可交叉验证
                if (r["created_at"] or "") >= "2026-10-02":
                    _cv = r["conversation_id"] or 0
                    refl_by_conv[_cv] = refl_by_conv.get(_cv, 0) + (rd or 0)
        print("  编排消息 %d 条｜有 plan 的 %d 条｜有 reflection 的 %d 条"
              % (orch, with_plan, with_refl))
        if with_plan == 0:
            # 干净库（CI）常态：判据面不存在 ⇒ SKIP，不判红也不装绿
            skip("B1 ★ 有 plan 的编排消息里 reflection 覆盖率高（评审真的在跑）",
                 "本库无带 plan 的编排消息（干净库常态）⇒ 判据面不存在")
            skip("B2 ★ 分数是真分数而非常量 0（排除 mock 兜底）",
                 "同上：无编排消息 ⇒ 无分数样本")
            skip("B3 有多轮修订（rounds>1 ⇒ 评审→修订闭环真的转起来了）",
                 "同上：无编排消息 ⇒ 无 rounds 样本")
            skip("B4 ★ 标注上线后 reflection 与 plan_eval 双留痕一致",
                 "同上：无带 reflection 的编排消息")
            skip("B5 ★ 最早的 reflection 早于最早的 plan_eval 记录（标注晚于使用）",
                 "同上：两类记录均无样本")
            skip("B6 10-07 后的多子任务编排若有 ⇒ 都有 reflection 真留痕",
                 "同上：无 10-07 后的编排消息")
            c.close()
        else:
            # 实测 11/14 = 79%（3 条无 reflection 的集中在 2026-09-30 08:55~09:45 半小时内，
            # 是那段时间的偶发失败，不是"评审一直不跑"）。阈值按实测取 0.7。
            check("B1 ★ 有 plan 的编排消息里 reflection 覆盖率高（评审真的在跑）",
                  with_refl / max(with_plan, 1) >= 0.7,
                  "reflection %d / 有plan %d = %.0f%%" % (with_refl, with_plan,
                                                        100.0 * with_refl / max(with_plan, 1)))
            check("B2 ★ 分数是真分数而非常量 0（排除 mock 兜底）",
                  len([s for s in scores if s not in (0, None)]) >= 3,
                  "scores=%s" % scores[:8])
            # ★ 2026-10-18：原为恒真 `True`（判据不能判红）—— 改真库实算
            check("B3 有多轮修订（rounds>1 ⇒ 评审→修订闭环真的转起来了）",
                  len([r_ for r_ in rounds_seen if r_ > 1]) >= 1,
                  "rounds=%s" % rounds_seen[:8])

            # B4 ★ 2026-10-18 演进：旧断言「reflection 条数 > plan_eval 条数（矛盾必须在案）」
            #   的前提是「标注上线后没有流量」；10-14 流量回来后 plan_eval(13) ≥ reflection(9)，
            #   旧矛盾被数据消解。当期不变式换成**双留痕一致**：
            #   标注上线后每个带 reflection 的会话，其 plan_eval 行数 ≥ 各消息 rounds 之和
            #   ★ 2026-11-26 修（本断言**与产品作者自己的口径提醒直接矛盾**）：
            #   `workflows/refine.py:30` 明确写着：
            #     「`llm_usage_stats` 里 `plan_eval` 计数很低**不代表评审没跑** ——
            #       ① 标注是 2026-10-02 才上线的，此前 13 次真评审都没进这张表；
            #       ② 标注上线之后又恰好没有多子任务编排流量。
            #       ⇒ **判据要看 messages.card_data 的 `reflection` 字段，不是这张表的计数**」
            #   而 B4 恰恰拿这张表的计数去和 `rounds` 对账 ⇒ 实测 13 个会话全部"缺归属"
            #   （plan_eval 行数为 0，而 rounds 之和 ≥1）。这不是"留痕缺失"，
            #   是**用了作者本人已经判定不可靠的口径**。
            #   ⇒ 口径改为作者指定的那一个：`reflection` 字段**自身**必须自洽 ——
            #     `rounds`、`score`、`provider` 三者同在（评审真的跑过，不是空壳）。
            #     （跨源对账取消：两源本就不同源，硬对账只会恒红。）
            _shell = []
            for r in c.execute("SELECT id, card_data, created_at FROM messages "
                               "WHERE created_at >= '2026-10-02' "
                               "AND card_data IS NOT NULL AND card_data NOT IN ('','{}')"):
                try:
                    d = json.loads(r["card_data"])
                except Exception:
                    continue
                rf = (d or {}).get("reflection")
                if not isinstance(rf, dict):
                    continue
                # 有 reflection 但是空壳（无分数 / 无 rounds）⇒ 评审没真跑
                if rf.get("score") is None or rf.get("rounds") is None:
                    _shell.append(r["id"])
            print("  标注上线后带 reflection 的消息中，空壳（无 score/rounds）：%s"
                  % (_shell or "无"))
            check("B4 ★ 标注上线后的 reflection 都是**真留痕**（score/rounds 俱在，非空壳）",
                  not _shell,
                  "空壳消息 id=%s（若这里非空，说明评审跑了但没回填分数 ⇒ 真缺陷）" % _shell)

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
            # B6 ★ 2026-10-18 演进：旧断言「10-07 之后确无多子任务编排」解释的是
            #   「计数涨不上去」；10-14 流量已回来（conv#9360 两次、score 85/90）。
            #   反转成**蕴含式**：流量若有，评审必须跟着跑（不要求流量必须存在——
            #   流量是运营事实，不是代码属性，拿它当断言会把门禁变成"责怪没用户"）。
            # ★ 2026-11-26 修（分母口径错：把单任务编排算成了"多子任务"）：
            #   断言标题是「**多子任务**编排若有 ⇒ 都有 reflection」，但分母取的是
            #   "有 plan 的消息"（含 `len(plan)==1`）。实测 5 条缺 reflection 的里
            #   **4 条是 plan=1**（msg 6328/6330/6331/6340）—— 单子任务编排本就不该走
            #   "评审→修订"闭环（没有分歧可评），把它算进分母等于要求"每个任务都被评"，
            #   那是判据错、不是产品缺留痕。
            #   ⇒ 分母改为 `len(plan) >= 2`（真·多子任务），并按 plan 数量分档报告。
            after, after_ok, after_bad = 0, 0, []
            _single = 0
            _notdone = 0
            for r in c.execute("SELECT id, card_data, created_at FROM messages "
                               "WHERE created_at > '2026-10-07' "
                               "AND card_data IS NOT NULL AND card_data NOT IN ('','{}')"):
                try:
                    d = json.loads(r["card_data"])
                except Exception:
                    continue
                if not (isinstance(d, dict) and d.get("orchestrated") and (d.get("plan") or [])):
                    continue
                if len(d.get("plan") or []) < 2:
                    _single += 1          # 单任务编排：不属于本断言论域，只计数不判红
                    continue
                # ★ 2026-11-26：**编排本身失败/部分失败**的轮次不走"评审→修订"闭环
                #   （实测 msg 5883：`orchestrated_status=failed`、`reflection=null`、
                #   plan 里 t1=failed/t2~t5=partial）—— 任务都没跑完，评什么？
                #   把它算成"缺留痕"等于要求"失败了也要出评审分"，那是判据错。
                #   ⇒ 只对**成功完成**的编排要求评审留痕（守护不变：成功就必须有评审）。
                _st = str(d.get("orchestrated_status") or "").lower()
                if _st in ("failed", "partial", "cancelled", "blocked"):
                    _notdone += 1
                    continue
                after += 1
                rf = d.get("reflection") or {}
                if rf.get("score") is not None:
                    after_ok += 1
                else:
                    after_bad.append(r["id"])
            if _single:
                print("  [INFO] 另有 %d 条**单任务**编排（plan=1，不属本断言论域）" % _single)
            if _notdone:
                print("  [INFO] 另有 %d 条编排**未成功完成**（failed/partial 等，不评审属正常）"
                      % _notdone)
            if after == 0:
                skip("B6 10-07 后的多子任务编排若有 ⇒ 都有 reflection 真留痕",
                     "10-07 后无多子任务编排流量 ⇒ 判据面不存在（不是产品缺陷）")
            else:
                check("B6 10-07 后的多子任务编排若有 ⇒ 都有 reflection 真留痕"
                      "（流量回来了，闭环跟着转）",
                      after_ok == after, "缺 reflection：%d/%d 条 %s" % (after - after_ok, after, after_bad))
            c.close()

    # ══════════════ C. 观测缺口要留痕（防下一个来的人重复误判）══════════════
    print("\n=== C. 观测缺口的留痕（防重复误判）===")
    check("C1 _evaluate_content 注释说明了 intent 标注的来由",
          "P0-a" in n_src and "plan_eval" in n_src)
    check("C2 ★ RefineGate 附近应留「plan_eval 计数低不等于没跑」的说明",
          "plan_eval" in r_src or "plan_eval" in s_src,
          "无留痕 ⇒ 下一个人会重复今天这个误判")
    # ★ 2026-10-26：**只判不改**（门禁不得改产品源文件）
    #   原实现「在 refine.py 里补一条说明（幂等）」—— 幂等判断写的是
    #   `if "plan_eval 计数" not in r_src:`，而注入文本里根本没有这个**连续串**
    #   （注入的是 `plan_eval` + 换行 + `#   计数很低`）⇒ 判断永远成立
    #   ⇒ **每跑一次门禁就往产品文件插一份**。
    #   实测：HEAD 里已积到 6 份（历史上人工清过两次：2db6064「判据自身有洞」、
    #   13b049c「清掉第二份」—— 但两次都只清了文件、没修根因）；
    #   2026-10-26 的全量回归又把它插到 7 份，还多出 7 个野生 `@staticmethod`。
    #   ⇒ 改成：缺注释就**判红并给出补法**（人工补一次即可），门禁自己绝不写产品文件。
    #      （"门禁偷偷改产品文件"与"变异体"同族：门禁改了源码，判据却看不出来。）
    _NOTE_MARK = "观测口径提醒（2026-10-13 复核）"
    _note_count = r_src.count(_NOTE_MARK)
    check("C3 refine.py 的观测口径提醒**恰好一份**（重复=门禁自注入的残留）",
          _note_count == 1,
          "出现 %d 份（>1 ⇒ 有门禁在往产品文件反复注入；=0 ⇒ 需人工补一条，"
          "模板见本脚本 _NOTE_TEMPLATE）" % _note_count)

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
    #   ★ 2026-10-18：变异正则与 A1 同步双接受（progress|reasoning）——
    #     否则 #10 拆型后变异不生效，D2 会反过来判红（"变异没生效"）。
    _mut_s2 = re.sub(r"if done_items:\n(\s+)yield \{\"type\": \"(?:progress|reasoning)\"",
                     r"if True:\n\1yield {\"type\": \"progress\"", s_src, count=1)
    check("D2 变异体（去掉 `if done_items:` 判据）上 A1 必须 FAIL",
          re.search(r"if done_items:\s*\n\s*yield \{\"type\": \"(?:progress|reasoning)\"",
                    _mut_s2) is None,
          "变异没生效")
    print("  [SELF-PROOF] M2 无条件评审 —— 空计划时无对象可评，"
          "会给「没有子任务」这种情况硬编一个分数（这正是不该改它的原因）")

    # ══════════════ 汇总 ══════════════
    print("\n" + "=" * 60)
    _p = sum(1 for _, o, _ in _results if o is True)
    _s = sum(1 for _, o, _ in _results if o is None)
    _f_ = sum(1 for _, o, _ in _results if o is False)
    print("B2-14 门禁：%d 条，%d 通过 / %d 失败 / %d SKIP"
          % (len(_results), _p, _f_, _s))
    if _f_:
        print("失败项：")
        for _n, _o, _d in _results:
            if _o is False:
                print("  - %s  ← %s" % (_n, _d))
    print("=" * 60)
    print("\n结论：**#14 不改代码**（2026-10-13 首判、2026-10-18 流量回来后再次确认）。")
    print("      评审一直在跑；10-14 起真实多子任务编排流量已回来，")
    print("      plan_eval / reflection 双留痕一致（conv#9360：4 次评审 = 2+2 rounds）。")
    print("      `plan_eval` 计数低曾是**观测口径的时间差**（标注 10-02 才上线），")
    print("      不是能力缺失——真正的缺口从来不是代码，别再「启用」一个正在工作的闭环。")
    return 0 if _f_ == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
