"""migrate_v11_prompts_into_skills — 把 8 个旧视图 Agent 的规格搬进对应 skill 正文。

背景（2026-10-08 实测）
----------------------
上一轮结论：8 个旧视图 Agent 的 `system_prompt` 是**2979~7981 字符**的详细规格
（合计 36158 字符），而对应 skill 正文只有 ~1100 字符，且 N3 prompt 只有 200 字符
⇒ 新方案"能跑通"≠"能替代"，这是**不能迁 Agent 的核心理由**。

本轮米爸决策：按建议步骤执行 —— 先把规格搬进 skill，让 skill 承接全部规格。
规格结构（实测8 个一致）：职责 / 输入 / 输出 / 工作流程 / 约束 / 视图规范 / 输出示例
⇒ **整篇搬迁，不做拆解加工**（拆解会丢信息，且旧规格本身是可用资产）。

注入瓶颈已解决
--------------
`skills.py` 此前是`body[:600]` 硬截断（注释声称"按需加载"但从无实现）。
本轮前一步已实现真按需加载（`skill_body_on_demand.py`，复用 tool_result_offloads
通路），长正文给引用块 + `skill_body_fetch` 可取回 ⇒ **搬 7981 字符是安全的**。

同步处理
--------
搬入后 skill 正文会含旧规格里的旧措辞，需按米爸本轮修正统一口径：
「活动图**必须**包含异常分支」⇒ **按需可选**（业务无异常路径时不硬造）。

默认 dry-run；--apply 才写。含逐条回读自检。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "mbse.db")

#旧视图 Agent → 对应 skill（一对一）
PAIRS = [
    ("需求视图生成", "sysml_view_generation_requirement"),
    ("结构视图生成", "sysml_view_generation_structure"),
    ("用例视图生成", "sysml_view_generation_usecase"),
    ("活动图生成", "sysml_view_generation_activity"),
    ("交互视图（IBD）生成", "sysml_view_generation_ibd"),
    ("顺序视图（时序图）生成", "sysml_view_generation_sequence"),
    ("状态机视图生成", "sysml_view_generation_state"),
    ("参数视图生成", "sysml_view_generation_parameter"),
]

# 米爸本轮口径修正：旧规格里的「异常分支必含」统一降为「按需可选」
NORMALIZE = [
    ("**异常分支（Exception Branch）必含**", "异常分支按需"),
    ("异常分支（Exception Branch 必含）", "异常分支（按需）"),
    ("必须包含异常分支", "异常分支按业务需要可选（无异常路径时不硬造）"),
    ("必须包含异常分支（Exception Branch）", "异常分支按业务需要可选"),
    ("必须画出异常分支", "异常分支按业务需要画"),
    ("不能只画正常路径", "有异常路径时不要只画正常路径"),
    ("必须含异常分支", "异常分支按需"),
    ("必含异常分支", "异常分支按需"),
    ("异常分支是必含项", "异常分支按需"),
]


def normalize(text: str) -> tuple:
    """按口径修正替换旧措辞，返回 (新文本, 命中次数, 明细)。"""
    hits = []
    for old, new in NORMALIZE:
        if old in text:
            n = text.count(old)
            text = text.replace(old, new)
            hits.append((old, n))
    return text, sum(h[1] for h in hits), hits


def build_content(skill_name, desc, trig, cat, old_body, spec):
    """组装新正文：保留 V10 的执行骨架（步骤/判据/失败处理）+ 追加旧规格全文。"""
    # 从现有 content 里切出 V10 骨架部分（到「## 完整视图规范」之前）
    skeleton = old_body
    marker = "\n## 完整视图规范（自旧视图 Agent 迁入，勿删）"
    if marker in skeleton:
        skeleton = skeleton.split(marker)[0].rstrip()
    parts = [
        skeleton,
        "",
        "## 完整视图规范（自旧视图 Agent 迁入，勿删）",
        "",
        f"> 以下为原「{skill_name}」Agent 的完整建模规格（{len(spec)} 字符），",
        "> 包含职责、输入输出、工作流程、约束、视图规范与输出示例。",
        "> **与上方执行骨架配合使用**：骨架给步骤与判据，本节给该视图的完整语法与规范细则。",
        "",
        spec,
    ]
    return "\n".join(parts)


def main(apply_):
    print("=" * 76)
    print("搬迁旧视图 Agent 规格 → 对应 skill 正文" + ("（APPLY）" if apply_ else "（DRY-RUN）"))
    print("=" * 76)
    if not os.path.isfile(DB):
        print(f"[ABORT] 库不存在：{DB}")
        return 2
    conn = sqlite3.connect(DB, timeout=60)
    conn.row_factory = sqlite3.Row
    try:
        nobj = conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0]
        if nobj < 50:
            print(f"[ABORT] 目标库可疑：对象数={nobj}")
            return 2

        plan = []
        print("\n── 搬迁计划 ──")
        for agent_name, skill_name in PAIRS:
            ra = conn.execute("SELECT system_prompt FROM agents WHERE name=?",
                              (agent_name,)).fetchone()
            rs = conn.execute(
                "SELECT id,content,frontmatter,description,triggers,category "
                "FROM skills WHERE name=?", (skill_name,)).fetchone()
            if not ra:
                print(f"  [SKIP] Agent {agent_name} 不存在")
                continue
            if not rs:
                print(f"  [SKIP] skill {skill_name} 不存在")
                continue
            spec, nh, hits = normalize(ra[0] or "")
            new_content = build_content(skill_name, rs["description"],
                                        json.loads(rs["triggers"] or "[]"),
                                        rs["category"], rs["content"] or "", spec)
            plan.append((rs["id"], agent_name, skill_name, new_content, nh, hits,
                         len(rs["content"] or ""), len(spec)))
            print(f"  {agent_name:22} → {skill_name:38} "
                  f"{len(rs['content'] or ''):5} + {len(spec):5} = {len(new_content):5} 字符"
                  + (f"（口径修正 {nh} 处）" if nh else ""))

        if not plan:
            print("\n无可搬迁内容")
            return 0
        if not apply_:
            print("\n（dry-run，未写库）加 --apply 执行")
            return 0

        # 写库：content + frontmatter 同步（既有做法是两者同文本）
        for (sid, an, sn, content, nh, hits, old_len, spec_len) in plan:
            conn.execute(
                "UPDATE skills SET content=?, frontmatter=?, updated_at=datetime('now','localtime') "
                "WHERE id=?", (content, content, sid))
        conn.commit()
        print(f"\n→ 已更新 {len(plan)} 个 skill 正文")

        # 回读自检
        print("\n── 自检 ──")
        ok = True
        for (sid, an, sn, content, nh, hits, old_len, spec_len) in plan:
            row = conn.execute(
                "SELECT content, frontmatter FROM skills WHERE id=?", (sid,)).fetchone()
            cur = row["content"] or ""
            prob = []
            if len(cur) < old_len + spec_len - 200:
                prob.append(f"正文未达预期长度（{len(cur)} vs 期望≈{old_len + spec_len}）")
            if row["frontmatter"] != cur:
                prob.append("frontmatter 与 content 不一致")
            if "旧视图 Agent 迁入" not in cur:
                prob.append("缺少迁入标记")
            # 口径修正必须生效
            for bad in ("必须包含异常分支", "必含异常分支", "异常分支（Exception Branch 必含）"):
                if bad in cur:
                    prob.append(f"仍含旧口径「{bad}」")
            print(f"  {'[OK  ]' if not prob else '[FAIL]'} {sn[:40]:42} "
                  f"{len(cur):5} 字符（原 {old_len} + 规格 {spec_len}）")
            for x in prob:
                ok = False
                print(f"         {x}")

        # 抽查：迁入后正文能否被按需加载取回
        print("\n── 按需加载连通性（迁入后正文 >600，必须能取回）──")
        try:
            from agent.pipeline_parts import skill_body_on_demand as m
            for (sid, an, sn, content, *_rest) in plan[:3]:
                _t, off, oid = m.offload_body(sn, content, conversation_id=0)
                back = m.fetch_body(oid) if off else ""
                good = off and len(back) == len(content)
                print(f"  {'[OK  ]' if good else '[FAIL]'} {sn[:36]:38} "
                      f"offload={off} oid={oid} 取回 {len(back)}/{len(content)} 字符")
                if not good:
                    ok = False
        except Exception as exc:                # noqa: BLE001
            print(f"  [WARN] 按需加载自检跳过：{type(exc).__name__}: {exc}")

        print("\n" + "=" * 76)
        print("✅ 规格搬迁完成" if ok else "⚠️ 有未通过项")
        print("=" * 76)
        return 0 if ok else 1
    finally:
        conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    sys.exit(main(a.apply))