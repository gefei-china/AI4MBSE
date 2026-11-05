# -*- coding: utf-8 -*-
"""记忆质量评测基准（D5，2026-10-07）—— LongMemEval 式五能力 + 真实数据闭环。

## 为什么需要它（依据 docs/记忆管理优化实施方案与实施计划-20261007.md §6 D5）

本仓记忆能力已相当完整（召回质量实测合格、遗忘引擎真跑），但**改记忆逻辑后无法回答
"变好了还是变坏了"** —— 与 `tools/eval/` 五件套建好 1,286 行而 `eval_reports` 0 行是同族问题。

本文档即那条"参照系"。**评测集全部从生产库真实记忆反推**（禁止编造 query）：
每条 `q` 都是库中真实存在、且有访问证据（`access_count>0`）的记忆所回答的问题。

## 五能力拆解（照LongMemEval）

| 能力 | 本仓对应 | 评测项|
|---|---|---|
| 信息抽取 | 召回能否命中正确事实 | `T-xx` 精确命中 |
| 多轮推理 | 能否召回支撑推理的多条 | `M-xx` 多命中 |
| 时序推理 | 新旧事实的时间取舍 | ⚠️ 本仓无双时态⇒ **本批不测**（能力不存在，测了是虚设） |
| **知识更新** | 冲突事实的处理 | `C-xx` 冲突组（对应 D1） |
| **拒答** | 该不该召回时召回 | `N-xx` 噪音组（问候语/Mock 话术必须 0 命中） |

## 关键设计：为什么评测集要**包含噪音样本**

实测（2026-10-07）发现 `agent_memory` 里`id=170`（问候语，access=**360**）、
`id=276`（Mock 回答，access=**70**）**访问计数最高**。
⇒ **它们确实被召回过**（命中率高），但**内容是噪音**。

⚠️ 这意味着"访问计数高"**不能**当"这条记忆有价值"的证据——
噪音被反复召回 → 计数高 → 若拿它做白名单/清理依据会保护垃圾。
⇒ 评测集必须把这类样本**显式列为"应拒答"**（`N-xx` 组），
并用 `recall_memories` 实测它们**是否真的被挡**。

运行：
    python tools/eval/eval_memory_quality.py --db <path> --json-out <path>
    # 默认只读生产库副本；--apply 才会写 eval_reports
"""
import argparse
import json
import os
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from agent.memory_recall import recall_memories  # noqa: E402

# ══════════════════════════════════════════════════════════════════════
# 评测集：从生产库真实记忆反推（2026-10-07 实测快照，用 id锚定防漂移）
#
# `expect_ids`：期望召回的 id 集合（不是期望分数——分数会随向量引擎/bigram 变动，
#   用 id 判定才稳定）。`forbid_ids`：必须**不**出现的 id（噪音闸）。
# ⚠️ id 是快照，会随库增长而失效，但**不会错位**（id 不会被复用）——
#   记忆被删则该用例判"库中已无此条"，计入 skipped 而非 FAIL。
# ══════════════════════════════════════════════════════════════════════

# ── T组：信息抽取（单条精确命中） ──
T_CASES = [
    dict(id="T01", q="智源工程包结构怎么查询", intent="zhiyuan_mgmt", expect=[81],
         why="工程列表定vc → 查包树的方法论（access=9）"),
    dict(id="T02", q="SysML v2 需求追溯该用哪个关系", intent="knowledge_qa",
         expect=[186, 172], why="satisfy/verify 两类专用关系（access 148/115，最高分组）"),
    dict(id="T03", q="整车需求导入建模的红线是什么", intent="requirement_analysis",
         expect=[265], why="不改语义/不改编号/不做推导（access=106）"),
    dict(id="T04", q="satisfy X by Y 句式有什么语法约束", intent="requirement_analysis",
         expect=[165, 166], why="X 必须是 requirement usage（access 97/69）"),
    dict(id="T05", q="SysML v2 接口建模常见陷阱", intent="design",
         expect=[145], why="端口名不可与 port 等保留字同名（access=69）"),
    dict(id="T06", q="需求建模时绑定主体和声明被满足能同时用吗", intent="requirement_analysis",
         expect=[266], why="二者互斥，否则模型校验不通过（access=64）"),
    dict(id="T07", q="涉众追溯应该挂在哪类需求上", intent="knowledge_qa",
         expect=[268], why="挂在关注点需求与参与者信息需求上（access=62）"),
    dict(id="T08", q="通信卫星总体设计先做什么", intent="design",
         expect=[182], why="先需求到架构显式映射再画框图（access=53）"),
]

# ── M 组：多轮推理（多条同时命中才算过） ──
M_CASES = [
    dict(id="M01", q="MBSE 方法论骨架的四层结构是什么", intent="knowledge_qa",
         expect=[174, 216, 180, 173], want_min=2,
         why="需求派生→架构满足→行为→验证，多条方法论互补"),
    dict(id="M02", q="热管理系统需求分析的核心矛盾是什么", intent="design",
         expect=[150, 146], want_min=1,
         why="两类热负载控温诉求错位（access=66）"),
    dict(id="M03", q="知识库需求工程方法的派生链条是怎样的", intent="knowledge_qa",
         expect=[90, 91], want_min=1,
         why="涉众→系统→子系统→单元（access 68/51，两条同源）"),
]

# ── C 组：知识更新（冲突事实，对应 D1） ──
# ⚠️ 这些是**真实存在的重复表述**（库中 id 133/134/135 讲同一个追溯链五环节）。
# 本组测的不是"能否合并"（consolidate 已覆盖），
# 而是"**同 topic 多条是否污染排序**"——即冲突检测的输入面。
C_CASES = [
    dict(id="C01", q="需求追溯链有几个环节", intent="requirement_analysis",
         expect=[133, 134, 135], want_min=1, max_hits=1,
         why="三条同源表述；期望**只出最相关的一条**。max_hits=1 使它真正在测"
             "「同 topic 污染」——此前只有 want_min=1，实测三条全召回也判 PASS，"
             "等于 D1 上线与否它都绿、基线失效。"
             "⚠️ D1 冲突仲裁未上线时本组**必然 FAIL**，这正是它的价值："
             "作为 D1 的前后对比基线（D1 上线后应转绿）"),
]

# ── N 组：拒答（噪音必须被挡，0 命中为过） ──
#⚠️ 这三条是**实测的高访问计数噪音**（access 360/70/66）——
#   它们"常被召回"但"不该召回"。本组是噪音闸门的守卫。
N_CASES = [
    dict(id="N01", q="你好", intent="chat", forbid=[170],
         why="问候语（access=360，实测被召回过）必须被噪音闸挡掉"),
    dict(id="N02", q="再补充一下动力系统的约束条件有哪些", intent="requirement_analysis",
         forbid=[276], why="Mock 回声话术（access=70）不应作为记忆召回"),
    dict(id="N03", q="MBSE 团队任务汇总", intent="knowledge_qa", forbid=[89],
         why="整篇报告正文壳（access=66）—— 长不等于值得记"),
]


def _fid_set(hits):
    """召回结果 → id 集合。

    ⚠️ **id 在 `meta.id`，不在顶层**（2026-10-07 实测踩到）：
    `recall_memories` 的返回结构是
    `{content, score, source_type, mem_type, scope, meta:{id, created_at, cross_domain, ...}, rank, recall_reason}`
    —— 顶层**没有** `id` 字段。第一版评测器按顶层 `id` 取，
    导致 **12/12 全FAIL 而Top1 内容完全正确**：
    「召回质量差」与「评测器抓不到 id」是两件事，**别把前者当成后者**。
    （同MEMORY「失败返回看起来像真的的值」族——错的判据比真的缺陷更误导。）
    """
    out = set()
    for h in hits:
        mid = (h.get("meta") or {}).get("id")
        if mid is None:
            mid = h.get("id")            # 兼容将来结构变化
        if mid is not None:
            out.add(int(mid))
    return out


def run_case(conn, case, kind):
    """跑单条，返回 (pass, detail)。"""
    hits = recall_memories(conn, case["q"], agent_id=case["intent"],
                          scopes=None, project_id="")
    got_ids = _fid_set(hits)
    top1 = f"{hits[0]['score']:.3f}" if hits else "-"
    snippet = (hits[0]["content"][:38] if hits else "(空)")
    if kind == "N":
        # 拒答组：期望 forbid 的都不出现
        leaked = got_ids & set(case["forbid"])
        return (not leaked), {
            "q": case["q"], "top1": top1, "hits": len(hits),
            "leaked": sorted(leaked), "why": case["why"], "top1_text": snippet}
    # 命中组：期望 id 至少出现 want_min 条
    want_min = case.get("want_min", 1)
    hit = got_ids & set(case["expect"])
    # ★ 2026-10-22 新增：可选的**污染上限** max_hits（同 topic 最多召回几条）。
    #   为什么需要它：C01 声明测"同 topic 多条是否污染排序"，但只有 want_min=1
    #   ⇒ 实测 matched=[133,134,135]（三条同源**全部**召回）也判 PASS。
    #   **D1 冲突仲裁上线与否，这个用例都绿** ⇒ 判据失效、基线无意义。
    #   加上 max_hits 后它才真正在测"污染"：命中≥1 且同源召回≤上限。
    max_hits = case.get("max_hits")
    ok = (len(hit) >= want_min)
    if ok and max_hits is not None:
        ok = (len(hit) <= max_hits)
    return ok, {
        "q": case["q"], "top1": top1, "hits": len(hits),
        "matched": sorted(hit), "want_min": want_min, "max_hits": max_hits,
        "want": case["expect"], "why": case["why"], "top1_text": snippet}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(ROOT, "mbse.db"))
    ap.add_argument("--json-out", default="")
    ap.add_argument("--apply", action="store_true",
                    help="写入 eval_reports（默认只读，避免污染生产库）")
    args = ap.parse_args()

    # ⚠️ `--apply` 必须用**可写**连接（2026-10-07 实测踩到）：
    # 评测主体刻意用 `mode=ro` 只读连接（防误写生产库），
    # 但落库需要写 ⇒ 两种模式分开取连接，别用同一个 ro 去INSERT。
    if args.apply:
        conn = sqlite3.connect(args.db)
        conn.row_factory = sqlite3.Row
        # SQLite WAL 下即使有别的只读连接在，写也没问题；这里只确保不是 ro
    else:
        conn = sqlite3.connect("file:%s?mode=ro" % args.db, uri=True)
        conn.row_factory = sqlite3.Row
    # 确认评测集锚定的 id 仍在库中（被删则 skipped，不判FAIL）
    alive = {r[0] for r in conn.execute("SELECT id FROM agent_memory")}

    results, n_pass, n_fail, n_skip = [], 0, 0, 0
    print("=" * 74)
    print("记忆质量评测基准（D5）  db=%s" % os.path.basename(args.db))
    print("=" * 74)

    for group, cases, kind in (("T 信息抽取", T_CASES, "T"),
                               ("M 多轮推理", M_CASES, "M"),
                               ("C 知识更新", C_CASES, "C"),
                               ("N 拒答/噪音", N_CASES, "N")):
        print("\n== %s ==" % group)
        for cs in cases:
            if kind != "N" and not (set(cs["expect"]) & alive):
                print("  SKIP %s （锚定记忆已不在库中）" % cs["id"])
                n_skip += 1
                # 也要落进 results：否则汇总处的 SKIP 明细取不到，
                # 且 JSON 报告里看不到"哪条失效了"（静默缩水）。
                results.append({"id": cs["id"], "kind": kind, "status": "skip",
                                "passed": None, "why": cs.get("why", ""),
                                "want": cs.get("expect", []),
                                "matched": sorted(set(cs["expect"]) & alive)})
                continue
            ok, d = run_case(conn, cs, kind)
            results.append(dict(case=cs["id"], kind=kind, passed=ok, **d))
            if ok:
                n_pass += 1
                print("  ok   %s  top1=%s hits=%d  %s" % (cs["id"], d["top1"], d["hits"], d["why"][:34]))
            else:
                n_fail += 1
                extra = ("泄漏 %s" % d["leaked"]) if kind == "N" else (
                    "命中 %s（要 %d）" % (d["matched"], d["want_min"]))
                print("  FAIL %s  top1=%s hits=%d  %s | top1=%s"
                      % (cs["id"], d["top1"], d["hits"], extra, d["top1_text"]))

    total = n_pass + n_fail
    rate = (n_pass / total) if total else 0.0
    print("\n" + "=" * 74)
    print("PASS %d / FAIL %d / SKIP %d  →  命中率 %.1f%%"
          % (n_pass, n_fail, n_skip, rate * 100))
    print("=" * 74)

    # ⚠️ SKIP 不得静默（2026-10-22 补）。SKIP = 评测集锚定的记忆已不在库中。
    #   它的危险在于：**命中率把 SKIP 排除在分母外**，于是"用例失效"表现为
    #   "覆盖率下降"而非"失败"——数字看着正常，实际上这条用例已经不测任何东西了。
    #   实测 T06 已 SKIP（锚定记忆被删），而汇总行此前不给出任何提示。
    if n_skip:
        print("⚠ SKIP %d 条：评测集锚定的记忆已不在库中（本组已不产生判别力）。"
              % n_skip)
        print("  SKIP 不计入命中率分母，故上面那个百分数**不代表全部用例有效**。")
        print("  处理：重锚（换 expect id）或删除该用例，别让基线静默缩水。")
        print("  SKIP 明细：%s"
              % ", ".join(r["id"] for r in results if r.get("status") == "skip")
              if any(r.get("status") == "skip" for r in results)
              else "  SKIP 明细：（见上方逐行输出）")

    # 分类小计（哪类能力拖后腿，一眼可见）
    for g, kind in (("T", "T"), ("M", "M"), ("C", "C"), ("N", "N")):
        # SKIP 不算"未通过"（它是"不测了"而非"测了没过"），
        # 但要在小计里显出来，否则组内通过率会被稀释得不明不白。
        sub = [r for r in results if r["kind"] == kind and r.get("status") != "skip"]
        if sub:
            p = sum(1 for r in sub if r["passed"])
            ns = sum(1 for r in results if r["kind"] == kind and r.get("status") == "skip")
            print("  %s 组: %d/%d%s" % (g, p, len(sub),
                                        ("（另有 %d 条 SKIP 不计）" % ns) if ns else ""))

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(dict(ts=time.strftime("%Y-%m-%d %H:%M:%S"),
                           db=os.path.basename(args.db),
                           pass_=n_pass, fail=n_fail, skip=n_skip,
                           rate=round(rate, 4), results=results),
                      f, ensure_ascii=False, indent=2)
        print("\nJSON 已写出: %s" % args.json_out)

    if args.apply:
        n = _write_report(conn, n_pass, n_fail, n_skip, rate, results)
        print("eval_reports 已落库 %d 行（id=%s）" % (n, conn.execute("SELECT last_insert_rowid()").fetchone()[0]))
    conn.close()
    return 0 if n_fail == 0 else 1


def _write_report(conn, n_pass, n_fail, n_skip, rate, results):
    """写入 `eval_reports`（数据闭环的落点）。

    ⚠️⚠️ **2026-10-07 实测踩到两个坑，都写在这里**：

    坑1：**我以为 `eval_reports` 是 0 行，实际有 4 行**（`tools/eval/run_eval` 的 RAG 评测在用）
      ⇒ **"表 0 行 ⇒ 能力没跑过"这个推断又一次不成立**。判"有没有跑过"必须看
      **谁在写**（`created_by`）而不是只看行数。

    坑2：表结构与我假设完全不同—— **无 `eval_name` / `score` 列**，
      实际是 `doc_id`(NOT NULL) + P/R/F1 指标 + `model_version`。
      第一版按 `eval_name/score` 拼 SQL ⇒ `no such column: eval_name`。
      而 `doc_id` NOT NULL 无默认值 ⇒ 漏给值直接 `IntegrityError`。

    适配口径（复用既有 4 行的写法，不发明新格式）：
      doc_id=0（与既有 RAG 评测一致 —— 它们也用 0，表示"非文档级评测"）
      doc_name = "记忆质量评测 ..."，entity_f1 存命中率作单一汇总指标
      detail   = 完整 JSON（含每条用例明细，够复现）
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(eval_reports)")}
    if not cols:
        raise RuntimeError("eval_reports 表不存在——不能声称已落库（宁可不写，不可写错地方）")
    detail = json.dumps(dict(schema="memory-quality/2026-10-07",
                             pass_=n_pass, fail=n_fail, skip=n_skip,
                             rate=round(rate, 4), results=results), ensure_ascii=False)
    cand = {
        "doc_id": 0,                                  # NOT NULL；与既有 RAG 评测口径一致
        "doc_name": "记忆质量评测 5能力 %d项" % len(results),
        # ⚠️ 2026-10-22（P0-5）：原先把同一个 rate 写进 **6 个** P/R/F1 字段。
        #   后果不是"冗余"，而是**语义覆写** —— 与 RAG 检索评测（entity_recall/
        #   relation_recall 有真实检索语义）共用同两列后，entity_f1=1.0 既可能是
        #   "检索全对"，也可能是"记忆 15/15"，**在库里不可区分**。
        #   现在：entity_f1 存命中率（该表仅有的汇总位，保持向后兼容），
        #   relation_* **留空**（记忆评测没有"关系召回"这个概念，
        #   填 0 会被误读成"关系召回 0 分"）。真正的区分靠 eval_kind。
        "entity_precision": round(rate, 4),
        "entity_recall": round(rate, 4),
        "entity_f1": round(rate, 4),
        "relation_precision": None,      # 记忆评测无关系召回语义 ⇒ 留空而非填 rate
        "relation_recall": None,
        "relation_f1": None,
        "detail": detail,
        "created_by": "tools/eval/eval_memory_quality",
        "model_version": "memory-recall",
        "is_golden": 0,
        "golden_set_id": 0,
        "eval_kind": "memory_quality",   # 2026-10-22：类型列，不再靠 created_by 反推
        # doc 域检索指标对本评测无意义 ⇒ 显式留空（不是 0）
        "doc_recall_at_5": None, "doc_mrr_at_10": None, "doc_ndcg_at_10": None,
        "doc_pool_recall": None, "doc_rerank_drop_rate": None,
        "doc_recall_miss_rate": None,
    }
    use = {k: v for k, v in cand.items() if k in cols}
    if "doc_id" in cols and use.get("doc_id") is None:
        raise RuntimeError("doc_id 为 NOT NULL，必须显式给值")
    conn.execute("INSERT INTO eval_reports (%s) VALUES (%s)"
                 % (",".join(use), ",".join("?" * len(use))), list(use.values()))
    # O3：指标快照表（entity_f1=命中率；快照行带三色，不再依赖固定列语义）
    try:
        from repositories.eval_report_repo import write_metric_rows
        write_metric_rows(conn, conn.execute("SELECT last_insert_rowid()").fetchone()[0],
                          "memory_quality", {"entity_f1": cand.get("entity_f1")})
    except Exception:
        pass  # 快照表是扩展通道，不连坐主落库
    conn.commit()
    return 1


if __name__ == "__main__":
    sys.exit(main())
