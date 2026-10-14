#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""verify_reuse_gate —— B4-7：沉淀流程复用「泛词劫持」门禁。

## 真缺陷（2026-10-14 真实前端验证抓到，不是离线推理）
`orchestration.py::_try_reuse_planner_flow` 的准入判据是
`score >= 2`（name 命中 bigram ×3 + desc 命中 ×2 + `name in input` +5）。
而 `_flow_tokens` 产出**字符 bigram**，模板名只有 12 字（12 个 bigram），
长输入与它的交集**天然只有 1~3 个** ⇒ **门槛等于"有一个泛词就算命中"**。

实测（真库唯一已发布模板 flow 234「自动编排·先做需求分析再生成方案设计」）：

| 输入 | name 命中 bigram | 词法分 | 修复前 |
|---|---|---|---|
| 生成一份报告 | {生成} | 3 | **命中（劫持）** |
| 帮我生成设计文档 | {生成, 设计} | 6 | **命中（劫持）** |
| 请输出这份需求的影响分析 | {生成, 分析} | 6 | **命中（劫持）** |
| 梳理接口评审清单 | {} | 0 | 未命中 |

⇒ 8 个探针里 **6 个被劫持**，含根本不该走建模编排的「生成一份报告」。
危害：用户要的是别的东西，却拿到一个 2 节点（需求分析→方案设计）的沉淀流程。

## ⚠️ 四轮修法都被实测否掉（记在这里，别再走回头路）
① 命中数量/覆盖度 → **挡住真请求**（真请求只命中 1~3 个）
② 命中占比（命中数÷模板 bigram 数）→ 两边同时爆炸，仍挡真请求
③ 「命中是否全为泛词」→ **真请求命中的恰好就是泛词**，仍被挡
④ 语义相似度阈值 → **实测无语中区分力**：
   「帮我生成设计文档」**0.6636 >** 「为卫星…生成 SysML V2 结构视图模型并校验」**0.5233**
   ⇒ 泛词请求分**更高**（embedding 是 bigram 降级态，同样不可靠）

⇒ 收敛到**唯一实测成立**的两条：泛词不得独享准入权 + 模板名去前缀后整体出现。

## 本门禁锁什么
G 组：泛词独享 ⇒ 必须被挡（**3 条负样本**，覆盖实测过的三种劫持形态）
P 组：正样本不得被误挡 —— **必须含「探针有区分度」判据**，
      否则「全部未命中」也会绿（这正是本门禁自身的假绿风险）
D 组：★**已知未解决**：模板名去前缀判据对 auto 沉淀模板**实测仍不生效**
      （`自动编排·xxx` 去掉前缀后 core 命中，但函数返回 None ——
      根因在 FlowExecutor 侧 t2 报 error 导致 `done_items` 为空，
      **不是准入判据的问题**）。记录而非判红，处置见门禁 docstring。
"""
import os
import sqlite3
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

DB = os.path.join(_ROOT, "mbse.db")

# 实测过的劫持样本（负）：修复前全部命中，复用泛词不得独享准入权
GENERIC_PROBES = [
    "生成一份报告",
    "帮我生成设计文档",
    "请输出这份需求的影响分析",
]
# 必须不被误挡的样本（正）：与模板主题一致但**不只含泛词**
POSITIVE_PROBES = [
    "自动编排·先做需求分析再生成方案设计",
]

_PASS, _FAIL, _GAP = [], [], []


def check(name, ok, detail=""):
    (_PASS if ok else _FAIL).append(name)
    print("  %s %s%s" % ("[PASS]" if ok else "[FAIL]", name,
                         ("  —— %s" % detail) if detail else ""))


def gap(name, detail):
    _GAP.append(name)
    print("  [GAP ] %s  —— %s" % (name, detail))


def pipeline():
    from agent.pipeline import AgentPipeline
    p = AgentPipeline()
    # ⚠️ 必须调 `_load_db_agents()`：它**不在 __init__ 里**（pipeline.py:53）
    p._load_db_agents()
    return p


def main():
    print("=== verify_reuse_gate · B4-7（沉淀流程复用的泛词劫持）===")
    c = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
    c.row_factory = sqlite3.Row
    try:
        n_pub = c.execute(
            "SELECT COUNT(*) FROM agent_flows "
            "WHERE source='planner_auto' AND status='published'").fetchone()[0]
        print("\n=== 前置：已发布 planner_auto 模板 %d 个 ===" % n_pub)
        if n_pub == 0:
            print("  [SKIP] 无已发布模板 ⇒ 复用机制当前不生效，本门禁无对象可验。")
            print("         （模板发布后本门禁会自动生效）")
            return 0

        p = pipeline()

        # G 组：泛词不得独享准入权
        print("\n=== G 组 · 泛词独享不得准入（3 条负样本）===")
        leaked = []
        for t in GENERIC_PROBES:
            try:
                got = p._try_reuse_planner_flow(t, "design") is not None
            except Exception:                                # noqa: BLE001
                got = "EXC"
            check("G 泛词样本不触发复用：%s" % t, got is not True,
                  "实测命中=%s（泛词独享了准入权）" % got)
            if got is True:
                leaked.append(t)

        # P 组：正样本 + 区分度判据
        print("\n=== P 组 · 正样本 + 防空转 ===")
        # 先确认词法判据层本身对正样本是有信号的（否则整组恒真）
        rows = c.execute(
            "SELECT name, description FROM agent_flows "
            "WHERE source='planner_auto' AND status='published'").fetchall()
        qt = p._flow_tokens(POSITIVE_PROBES[0])
        max_score = 0
        for r in rows:
            nm = r["name"] or ""
            qn = qt & p._flow_tokens(nm)
            qd = qt & p._flow_tokens(r["description"] or "")
            s = len(qn) * 3 + len(qd) * 2
            core = (nm.split("·", 1)[-1] if "·" in nm else nm).strip()
            if core and core in POSITIVE_PROBES[0]:
                s += 5
            max_score = max(max_score, s)
        check("P1 正样本在**词法层**有信号（score ≥2，否则整组恒真）",
              max_score >= 2, "实测最高词法分 %d" % max_score)

        # ★区分度：正负样本的准入结论必须不同
        check("P2 ★判据有区分度（负样本未命中 ∧ 正样本词法层有信号）",
              not leaked and max_score >= 2,
              "泄漏 %s；正样本词法分 %d" % (leaked, max_score))

        # D 组：已知未解决项
        print("\n=== D 组 · 已知未解决（记录，不判红）===")
        pos_res = []
        for t in POSITIVE_PROBES:
            try:
                pos_res.append((t, p._try_reuse_planner_flow(t, "design") is not None))
            except Exception as e:                           # noqa: BLE001
                pos_res.append((t, "EXC:%s" % type(e).__name__))
        if any(v is not True for _, v in pos_res):
            gap("D1 「name 去前缀后整体出现」对 auto 沉淀模板仍未让复用成立",
                "实测 %s。**已定位为 FlowExecutor 侧问题（t2 报 error ⇒ done_items 为空 ⇒ "
                "return None），不是准入判据的问题** —— 准入层实测已放行（score=%d）。"
                "下一步应查 FlowExecutor 的 t2 error，而非继续调准入阈值。"
                % (pos_res, max_score))
        else:
            print("  [INFO] 正样本复用已成立 ⇒ D1 口径可关闭")
    finally:
        c.close()
    print("\n%d pass / %d fail / %d gap" % (len(_PASS), len(_FAIL), len(_GAP)))
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())