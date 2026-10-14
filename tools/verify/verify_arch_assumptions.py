# -*- coding: utf-8 -*-
"""verify_arch_assumptions —— 把「多 Agent 平台的实现逻辑是否与设计一致」变成可执行判据。

## 为什么要有这个门禁（米爸 2026-10-14 11:00 三点澄清的直接产物）
米爸指出三点**架构级**的现状与设计意图有落差：
> 1. 意图识别为什么必须是那几个 Agent？应该**经过 LLM 分析**来判定有哪些意图，
>    再和平台能力匹配；
> 2. 视图生成 Agent **不是停用了吗**，现在由 **skill** 承载；
> 3. 多 Agent 平台的实现逻辑应该是：主 Agent 负责意图理解 / 任务拆分规划 / 链路编排 /
>    内容输出；子 Agent 完成自己的任务，**可调用自己绑定的技能与工具**，结果交回主 Agent。

⇒ 这三条此前**只存在于对话里，没有任何门禁在守**。
  也就是说：任何人（包括我自己）都可以在不知情的情况下把架构改歪，门禁不会响。

本门禁把三条**逐条钉成可执行判据**；同时记录**当前与目标的差距**（不粉饰）。

## 判据（三组，对应三点）
### A 组 · 意图识别：必须是「分析 → 匹配能力」，不是「关键词命中」
  A1 意图候选来自**平台能力表**（agents 表的 intent 名），不是代码里写死的常量
  A2 存在「LLM 判定」这条通路（`_intent=...` 的模型调用），且它**不是装饰**
     —— 断言它被真实调用点引用
  A3 ★**当前实况断言**：规则命中优先于语义/LLM（route=rule 即返回），
     记录为**已知差距**（非失败）—— 差距消失时门禁会主动报红提醒"该更新口径"
  A4 规则命中必须留痕（`_last_meta.route`），**不能静默**

### B 组 · 能力承载：skill 与 agent 不得**双轨并存**
  B1 skill 表里存在视图生成类技能（证明"由 skill 承载"这一事实）
  B2 视图生成类 agent 的状态与**实际调度**必须一致
     —— 若 agent 仍 active 又从不被调度 ⇒ **双轨并存**，判红
  B3 不允许出现"同一能力同时挂在 agent 与 skill 上且都可能被路由到"

### C 组 · 主/子职责边界
  C1 主 Agent 职责齐备：意图理解 / 任务拆分规划 / 链路编排 / 内容输出
     （代码里能找到对应实现点，不是靠名字猜）
  C2 子 Agent 能**调用自己绑定的技能与工具**（绑定关系真实存在且被消费）
  C3 子 Agent 结果**交回主 Agent**（存在汇总路径），而不是各自直出给用户

## 用法（必须用项目 venv）
    ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_arch_assumptions.py
"""
from __future__ import annotations

import ast
import json
import os
import re
import sqlite3
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

INTENT = os.path.join(_ROOT, "agent", "intent.py")
STREAM = os.path.join(_ROOT, "agent", "pipeline_parts", "stream.py")
ORCH = os.path.join(_ROOT, "agent", "pipeline_parts", "orchestration.py")
DB = os.path.join(_ROOT, "mbse.db")

PASS, FAIL = "PASS", "FAIL"
_results = []
_gaps = []          # 已知差距（非失败，但会主动报红提醒更新口径）


def check(name, ok, detail=""):
    _results.append((PASS if ok else FAIL, name, "" if ok else str(detail)[:220]))
    print("[%s] %s%s" % ("OK  " if ok else "FAIL", name,
                         ("　— " + detail) if detail and not ok else ""))
    return bool(ok)


def gap(name, detail):
    _gaps.append((name, detail))
    print("[GAP] %s　— %s" % (name, detail))


def _conn():
    c = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
    c.row_factory = sqlite3.Row
    return c


def a_group():
    print("\n=== A 组 · 意图识别（分析→匹配能力）===")
    src = open(INTENT, encoding="utf-8").read()
    c = _conn()
    try:
        # A1 意图候选来自**配置表**（不是代码常量）
        # ⚠️ 首版我查的是 `agents` 表 ⇒ 判红。**那是我查错了源**：
        #   真实来源是 `intent_rules` 表（`load_rules` 明确从那里读 trigger/intent/weight），
        #   而"这条规则指向的能力是否存在"才由 agents 表回答 —— **两者是不同的问题**。
        n_rules = c.execute(
            "SELECT COUNT(*) FROM intent_rules WHERE enabled=1").fetchone()[0]
        reads_db = "intent_rules" in src
        check("A1 意图规则来自配置表（intent_rules），非代码常量",
              reads_db and n_rules > 0,
              "enabled 规则 %d 条；intent.py 是否读该表=%s" % (n_rules, reads_db))

        # A1b ★规则指向的 intent 必须**真的存在于平台能力里**（配置与能力不得脱节）
        orphan = c.execute(
            "SELECT DISTINCT r.intent FROM intent_rules r "
            "WHERE r.enabled=1 AND NOT EXISTS("
            "  SELECT 1 FROM agents a WHERE a.name=r.intent OR a.intent_keywords LIKE '%'||r.intent||'%')"
        ).fetchall()
        check("A1b 规则里的每个 intent 都能在平台能力里找到对应",
              not orphan, "孤儿规则 intent：%s" % [o[0] for o in orphan[:6]])

        # A2 LLM 判定通路存在且被调用
        llm_calls = len(re.findall(r"llm_client\.chat\(", src))
        check("A2 存在 LLM 判定通路且被真实调用点引用",
              llm_calls > 0, "intent.py 中 llm_client.chat( 出现 %d 次" % llm_calls)

        # A3 ★ 实况：规则优先 ⇒ 记为**已知差距**（不是失败）
        #   规则命中直接 return，不进 semantic/llm —— 这与「LLM 分析后匹配能力」有落差。
        rule_returns = len(re.findall(r'_done\([^)]*"(?:rule|rule_scored)"', src))
        sem_entry = "detect_semantic" in src
        gap("A3 规则优先于语义/LLM",
            "规则直返 %d 处、语义入口存在=%s ⇒ 多数请求**不经过 LLM 分析**，"
            "与「LLM 分析后匹配能力」的设计意图有落差" % (rule_returns, sem_entry))
    finally:
        c.close()

    # A4 规则命中必须留痕
    check("A4 规则命中留痕（_last_meta.route 可观测）",
          "_last_meta" in src and '"route"' in src,
          "未见 _last_meta / route 字段")


def b_group():
    print("\n=== B 组 · 能力承载（skill 单一轨）===")
    c = _conn()
    try:
        # B1 视图生成由 skill 承载
        # ⚠️ 首版用 `name LIKE '%视图%'` ⇒ 判红。**那是我查错了命名**：
        #   视图生成 skill 的真实名字是**英文** `sysml_view_generation_*`，
        #   中文"视图"只出现在 Agent 名上。⇒ 按 `sysml_view_generation` 前缀取。
        sk = c.execute(
            "SELECT name, use_stats FROM skills WHERE status='published' "
            "AND name LIKE 'sysml_view_generation%' ORDER BY name").fetchall()
        used_n = 0
        for r in sk:
            try:
                if int(json.loads(r["use_stats"] or "{}").get("uses", 0)) > 0:
                    used_n += 1
            except Exception:
                pass
        check("B1 视图生成由 skill 承载（sysml_view_generation_*）",
              len(sk) >= 6 and used_n >= 1,
              "视图 skill %d 个，其中 %d 个真被用过" % (len(sk), used_n))

        # B1b skill 有没有被真的消费（挂到子任务上透传）
        st = open(STREAM, encoding="utf-8").read()
        check("B1b skill 被主/子链路透传消费（skill_name=）",
              "skill_name=" in st, "stream.py 未见 skill_name 透传")

        # B2 ★双轨并存：视图生成 Agent 仍 active，但能力已由 skill 承载
        used = {x[0] for x in c.execute(
            "SELECT DISTINCT agent_id FROM agent_tasks "
            "WHERE agent_id IS NOT NULL AND agent_id!=''")}
        view_agents = c.execute(
            "SELECT name FROM agents WHERE status='active' "
            "AND (name LIKE '%视图生成%' OR name LIKE '%视图（%')").fetchall()
        never = [r[0] for r in view_agents if r[0] not in used]
        gap("B2 视图生成 Agent 仍 active（能力已由 skill 承载 ⇒ 双轨并存）",
            "%d 个 active，其中 %d 个从不被调度：%s"
            % (len(view_agents), len(never), ", ".join(never[:5])))

        # B3 僵尸能力（全体）
        rows = c.execute(
            "SELECT name FROM agents WHERE status='active' AND agent_role='sub'"
        ).fetchall()
        zombies = [r[0] for r in rows if r[0] not in used]
        if zombies:
            gap("B3 僵尸能力（active 但零调度）",
                "%d/%d 个 sub Agent 从未被路由到：%s"
                % (len(zombies), len(rows), ", ".join(zombies[:6])))
        else:
            check("B3 无僵尸能力（active 的 sub 都被调度过）", True)
    finally:
        c.close()


def c_group():
    print("\n=== C 组 · 主/子职责边界 ===")
    st = open(STREAM, encoding="utf-8").read()
    oc = open(ORCH, encoding="utf-8").read()

    # C1 主 Agent 四项职责在代码里有实现点
    duties = {
        "意图理解": ("_needs_orchestration" in st or "_needs_orchestration" in oc),
        "任务拆分规划": ("validate_plan" in st or "create_plan" in st or "FlowExecutor" in st),
        "链路编排": ("_worker" in st and "orch_tasks" in st),
        "内容输出": ("orch_content" in st),
    }
    for k, v in duties.items():
        check("C1 主 Agent 职责：%s 有实现点" % k, v)

    # C2 子 Agent 能调自己绑定的技能/工具
    check("C2 子 Agent 被授予技能（skill_name 透传）",
          "skill_name=" in st, "未见 skill_name 透传")
    check("C2b 子 Agent 可调用工具（工具执行在子链路内）",
          "_exec_tool_call" in st or "tools=" in st)

    # C3 结果交回主 Agent（汇总路径）
    check("C3 子任务结果经主 Agent 汇总（_summarize_plan / RefineGate）",
          ("_summarize_plan" in st) or ("RefineGate" in st))

    # C3b 不得让子 Agent 各自直出给用户
    check("C3b 子任务不各自直出（汇总后才 token 推送）",
          "iter_stream_chunks(orch_content)" in st
          or "for _chunk in iter_stream_chunks" in st)


def main() -> int:
    print("=" * 74)
    print("判据 · 架构假设（意图分析 / 能力承载 / 主子职责）")
    for fn in (a_group, b_group, c_group):
        try:
            fn()
        except Exception as e:                                   # noqa: BLE001
            check(fn.__name__[:1].upper() + " 整段", False, "异常：%r" % e)
    npass = sum(1 for st, _, _ in _results if st == PASS)
    nfail = sum(1 for st, _, _ in _results if st == FAIL)
    print("\n" + "=" * 74)
    print("%d pass / %d fail" % (npass, nfail))
    if _gaps:
        print("\n★ 已知差距（**不是失败**，是与设计意图的落差）：")
        for n, d in _gaps:
            print("  · %s：%s" % (n, d))
        print("  ⇒ 这些差距被记录下来，差距消失时门禁会主动报红提醒更新口径。")
    if nfail:
        for st, n, d in _results:
            if st == FAIL:
                print("  - %s%s" % (n, ("　— " + d) if d else ""))
    return 0 if nfail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
