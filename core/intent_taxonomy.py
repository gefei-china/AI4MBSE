"""意图字典（intent_taxonomy）—— 把「意图」从 agents 表派生改为**独立可维护实体**。

## 为什么（2026-11-07 米爸第 17 轮）
现状：意图清单是 `agents.status='active'` 派生出来的（`IntentSampleRepo._dynamic_intents`），
中文标签硬编码在前端 `IS_INTENT_LABEL`，示例说法硬编码在
`agent/intent.py::_SEMANTIC_UTTERANCES`（只有 8 个意图）。
⇒ 用户看到的两个问题都由此而来：
  ① 审核页只能从下拉**单选**，无法新增一个"还没对应 Agent 的意图"；
  ② 无法编辑（中文名/说明/示例说法都不在任何表里）。

## 行业做法（DVC / Annotera / beefed.ai 综述的共识）
意图分类体系（taxonomy）必须是**受控词表**，每个意图要有：
  · 一个 **canonical 名**（机器用，snake_case，稳定不变）
  · **别名表**（legacy / 用户说法 → canonical，用于归一化与检索扩展）
  · **scope note**（一句话定义 + 什么不属于我）
  · **3~5 条示例说法**（正例；负例靠 cluster 边界说明）
  · **版本号 + 弃用路径**（announce → alias → migrate → remove）
「A taxonomy without a change-and-release process becomes a frozen artifact」——
所以本表必须可增删改，而不是写在代码里。

## 与既有结构的关系（不推翻，只补一层）
| 既有 | 本表如何配合 |
|---|---|
| `agents`（执行主体） | `agent_name` 指向它；**可为空**（纯分类标签，不对应任何 Agent） |
| `agents.intent_keywords` | 保留（关键词层打分用）；本表的 `examples` 喂 **LLM few-shot**（互补，不重复） |
| `_SEMANTIC_UTTERANCES`（硬编码 8 条） | 迁移进 `examples`，代码回落常量（DB 不可读时仍可用） |
| 前端 `IS_INTENT_LABEL` | 改为读本表 `label`，硬编码只作首屏兜底 |
| `intent_samples.intent` | 不必改：它存的就是 canonical 名 |

## 状态语义（弃用路径）
`active` 活跃 / `deprecated` 弃用（不再参与路由，但历史样本仍能解析、仍可被别名归一化）
/ `merged` 已并入 `merged_into`（保留用于把旧标注自动改指）
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from typing import Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DDL = """
CREATE TABLE IF NOT EXISTS intent_taxonomy (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL UNIQUE,       -- canonical 名（机器用，snake_case，稳定）
    label        TEXT NOT NULL DEFAULT '',   -- 中文显示名（人用）
    scope_note   TEXT NOT NULL DEFAULT '',   -- 一句话定义 + 什么不算我
    aliases      TEXT NOT NULL DEFAULT '[]', -- JSON: ["需求分析","需求解析"]
    examples     TEXT NOT NULL DEFAULT '[]', -- JSON: 示例说法，喂 LLM few-shot
    cluster      TEXT NOT NULL DEFAULT '',   -- 能力族（与 core.capability_clusters 对齐）
    agent_name   TEXT NOT NULL DEFAULT '',   -- 映射到的 Agent；空 = 纯分类标签
    status       TEXT NOT NULL DEFAULT 'active',   -- active/deprecated/merged
    merged_into  TEXT NOT NULL DEFAULT '',   -- status='merged' 时的目标 canonical 名
    version      TEXT NOT NULL DEFAULT 'v1.0',
    owner        TEXT NOT NULL DEFAULT '',
    created_at   TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at   TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_intent_tax_status ON intent_taxonomy(status);
"""

#: 初始种子：由 `agents` 现状 + `_SEMANTIC_UTTERANCES` 迁移而来。
#: 只填**确实确定**的内容；示例说法没有的留空（不编造），靠后续审核补。
SEED = [
    ("requirement_analysis", "需求分析", "把模糊诉求转成可验收的需求规格（ID/MoSCoW/GWT 验收标准）。不含：质量评审、批量条目化",
     ["需求分析", "需求规格", "需求解析"], ["帮我解析这份需求文档", "把需求条目提取出来", "从任务书里提取需求"],
     "requirement", "requirement_analysis"),
    ("requirement_quality", "需求质量", "对**已写好**的需求做质量评审：模糊词/不可验证/自相矛盾/缺验收判据。不含：改写需求、拆分条目",
     ["需求质量", "需求评审", "质量评审"], ["需求质量分析", "检查需求有没有模糊词", "需求可验证性检查"],
     "requirement", "requirement_quality"),
    ("requirement_structuring", "需求结构化", "把长文本/访谈记录拆成带 ID 的需求条目。不含：规格化定义、质量评审",
     ["需求结构化", "需求条目化", "需求拆解"], [],
     "requirement", "requirement_structuring"),
    ("design", "设计/建模", "系统架构方案设计、多方案权衡、骨架与八视图生成、方法论选择。不含：预评审打分、变更影响分析",
     ["方案设计", "架构设计", "系统建模", "建模"], ["帮我做总体设计", "设计系统架构方案", "生成模型代码", "出一份视图"],
     "design", "design"),
    ("impact", "影响分析", "分析一处变更会波及哪些模型元素（依赖链 BFS、传播路径、风险等级）。不含：删除安全门、模型修改",
     ["变更影响", "影响范围", "影响分析"], ["分析变更影响", "这次改动会波及哪些模块", "影响范围分析"],
     "change", "impact"),
    ("review", "评审", "对 SysML v2 模型做预评审：语法/逻辑/规范/合理性四类校验 + 可评审性评分。不含：自动修复、正式签批",
     ["预评审", "校验", "模型校验"], ["校验模型代码", "评审一下这份文档", "预评审这份设计"],
     "verify", "review"),
    ("report_generation", "报告生成", "把各专家分析结果组装成规范工程报告并导出 md/docx/pdf。不含：产出新分析结论",
     ["报告生成", "导出报告", "写报告"], ["生成一份报告", "导出报告", "写一份评审报告"],
     "report", "report_generation"),
    ("knowledge_qa", "知识问答", "解释 MBSE/SysML v2 概念、规范条款解读、问题诊断，答案标注来源。不含：平台操作指引、改模型",
     ["知识问答", "概念解释", "原理"], ["介绍一下这个方法", "什么是这个概念", "原理是什么"],
     "knowledge", "knowledge_qa"),
    ("chat", "闲聊/兜底", "未被其它意图认领的请求：寒暄、平台操作指引、跨域综合问答。不含：明确归域的任务",
     ["闲聊", "兜底"], [], "knowledge", "chat"),
    ("system_mgmt", "系统管理", "平台自身管理：用户/角色/审计/监控。不含：业务建模",
     ["系统管理", "审计日志"], ["系统里有几个用户", "查看审计日志", "用户权限怎么管理"],
     "shared", ""),
    # 建模流水线 8 阶段（agents 里真实存在、可被路由，此前前端下拉选不到）
    ("methodology_resolver", "方法论解析", "解析/选择建模方法论与规约（profile 规约卡）。", [], [], "design", "methodology_resolver"),
    ("architecture_skeleton", "架构骨架生成", "按方法论生成包结构与骨架代码。", [], [], "design", "architecture_skeleton"),
    ("view_expansion", "视图展开", "把骨架展开成八视图 SysML v2 代码。", [], [], "design", "view_expansion"),
    ("model_validation_repair", "模型校验修复", "语法诊断 → 自动修复 → 复检的循环。", [], [], "verify", "model_validation_repair"),
    ("trace_verification", "追溯核验", "需求-模型追溯矩阵、覆盖率、孤儿元素识别。", [], [], "verify", "trace_verification"),
    ("change_safety_gate", "变更安全门", "删除/批量替换前的安全边界评估。", [], [], "change", "change_safety_gate"),
    ("model_release", "模型发布", "校验通过后落库发布、生成新版本。", [], [], "platform", "model_release"),
    ("智源对接agent", "智源平台对接", "通过 MCP 读写智源平台工程/分支/模型与源码导出。", [], [], "platform", "智源对接agent"),
    ("智源数据读写agent", "智源数据读写", "智源平台数据的查询与增删改。", [], [], "platform", "智源数据读写agent"),
    ("多方案设计", "多方案设计", "多方案架构设计与对比。", [], [], "design", "多方案设计"),
    ("多方案生成", "多方案生成", "变体空间候选提议与筛选（不产出模型代码）。", [], [], "design", "多方案生成"),
    ("MBSE建模总体负责人", "MBSE 建模总体负责人", "建模流水线总控与多 Agent 编排（执行主体，非路由候选）。", [], [], "orchestration", "MBSE建模总体负责人"),
]

VALID_STATUS = ("active", "deprecated", "merged")


def ensure_table(conn) -> None:
    conn.executescript(DDL)
    conn.commit()


def seed(conn, force: bool = False) -> dict:
    """幂等播种：只插不存在的 name。返回统计。"""
    ensure_table(conn)
    added, exist = 0, 0
    for (name, label, note, aliases, examples, cluster, agent) in SEED:
        r = conn.execute("SELECT id FROM intent_taxonomy WHERE name=?", (name,)).fetchone()
        if r:
            exist += 1
            if not force:
                continue
        conn.execute(
            "INSERT OR IGNORE INTO intent_taxonomy "
            "(name,label,scope_note,aliases,examples,cluster,agent_name) VALUES (?,?,?,?,?,?,?)",
            (name, label, note, json.dumps(aliases, ensure_ascii=False),
             json.dumps(examples, ensure_ascii=False), cluster, agent))
        added += 1
    # 从 agents 补齐：库里存在但种子里没有的 Agent（用户自建）
    try:
        for r in conn.execute("SELECT name, display_name FROM agents WHERE status='active'"):
            n = r[0]
            if conn.execute("SELECT 1 FROM intent_taxonomy WHERE name=?", (n,)).fetchone():
                continue
            conn.execute(
                "INSERT INTO intent_taxonomy (name,label,scope_note,aliases,examples,cluster,agent_name) "
                "VALUES (?,?,?,?,?,?,?)",
                (n, r[1] or n, "", "[]", "[]", "", n))
            added += 1
    except Exception:
        pass
    conn.commit()
    return {"added": added, "existing": exist}


def list_all(conn, status: Optional[str] = None) -> list:
    sql = "SELECT * FROM intent_taxonomy"
    args = ()
    if status:
        sql += " WHERE status=?"
        args = (status,)
    sql += " ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'deprecated' THEN 1 ELSE 2 END, id"
    cur = conn.execute(sql, args)
    cols = [d[0] for d in (cur.description or [])]
    out = []
    for r in cur.fetchall():
        # ⚠️ 不能直接 dict(r)：调用方若没设 row_factory，r 是 tuple ⇒
        #   `dict(tuple)` 抛 "cannot convert dictionary update sequence"。
        #   本模块被门禁/路由/脚本多处调用，行工厂不能靠"大家都记得设"来保证。
        if isinstance(r, dict):
            d = dict(r)
        elif hasattr(r, "keys"):
            d = {k: r[k] for k in r.keys()}
        else:
            d = dict(zip(cols, r))
        for k in ("aliases", "examples"):
            try:
                d[k] = json.loads(d.get(k) or "[]")
            except Exception:
                d[k] = []
        out.append(d)
    return out


def upsert(conn, name: str, label: str = "", scope_note: Optional[str] = None,
           aliases: Optional[list] = None, examples: Optional[list] = None,
           cluster: Optional[str] = None, agent_name: Optional[str] = None,
           status: Optional[str] = None, merged_into: Optional[str] = None,
           owner: str = "") -> dict:
    """新增或编辑一个意图。**name 不可改**（canonical 稳定），其余都可改。"""
    name = (name or "").strip()
    if not name:
        return {"ok": False, "reason": "empty_name"}
    if status and status not in VALID_STATUS:
        return {"ok": False, "reason": "bad_status"}
    ensure_table(conn)
    cur = conn.execute("SELECT id FROM intent_taxonomy WHERE name=?", (name,)).fetchone()
    if cur:
        sets, args = ["updated_at=CURRENT_TIMESTAMP"], []
        for col, val, enc in (("label", label, False), ("scope_note", scope_note, False),
                              ("cluster", cluster, False), ("agent_name", agent_name, False),
                              ("status", status, False), ("merged_into", merged_into, False),
                              ("owner", owner or None, False)):
            if val is not None:
                sets.append(col + "=?")
                args.append(val)
        for col, val in (("aliases", aliases), ("examples", examples)):
            if val is not None:
                sets.append(col + "=?")
                args.append(json.dumps(list(val), ensure_ascii=False))
        conn.execute("UPDATE intent_taxonomy SET " + ", ".join(sets) + " WHERE id=?",
                     args + [cur[0]])
        conn.commit()
        return {"ok": True, "id": cur[0], "created": False}
    conn.execute(
        "INSERT INTO intent_taxonomy (name,label,scope_note,aliases,examples,cluster,"
        "agent_name,status,merged_into,owner) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (name, label or name, scope_note or "",
         json.dumps(list(aliases or []), ensure_ascii=False),
         json.dumps(list(examples or []), ensure_ascii=False),
         cluster or "", agent_name or "", status or "active", merged_into or "", owner))
    conn.commit()
    return {"ok": True, "id": conn.execute(
        "SELECT id FROM intent_taxonomy WHERE name=?", (name,)).fetchone()[0], "created": True}


def resolve_alias(conn, text: str) -> str:
    """把用户输入/旧标注归一化到 canonical 名（行业标准做法：synonym ring）。

    命中顺序：精确 name → 别名精确 → 别名子串（长别名优先）→ 返回原值。
    DB 不可读时返回原值（不猜）。
    """
    t = (text or "").strip()
    if not t:
        return t
    try:
        ensure_table(conn)
        r = conn.execute("SELECT name FROM intent_taxonomy WHERE name=?", (t,)).fetchone()
        if r:
            return r[0]
        # 别名精确
        for row in conn.execute("SELECT name, aliases FROM intent_taxonomy "
                                "WHERE status IN ('active','deprecated')"):
            try:
                al = json.loads(row[1] or "[]")
            except Exception:
                al = []
            if t in al:
                return row[0]
        # 别名子串（最长优先，防"需求"抢"需求质量"）
        best, best_len = "", 0
        for row in conn.execute("SELECT name, aliases FROM intent_taxonomy "
                                "WHERE status IN ('active','deprecated')"):
            try:
                al = json.loads(row[1] or "[]")
            except Exception:
                al = []
            for a in al:
                if a and a in t and len(a) > best_len:
                    best, best_len = row[0], len(a)
        return best or t
    except Exception:
        return t


def fewshot_map(conn) -> dict:
    """返回 `{canonical: [示例说法]}`，供 LLM few-shot 使用（替代硬编码表的**增量**来源）。

    与 `_SEMANTIC_UTTERANCES` 的关系：那边是代码常量（DB 不可读时兜底），
    这里是**用户可维护**的主来源；两者合并，本表优先。
    """
    out = {}
    try:
        ensure_table(conn)
        for r in conn.execute("SELECT name, examples FROM intent_taxonomy "
                              "WHERE status='active'"):
            try:
                ex = json.loads(r[1] or "[]")
            except Exception:
                ex = []
            if ex:
                out[r[0]] = ex
    except Exception:
        return {}
    return out


def suggest_new_names(conn, limit: int = 20) -> list:
    """从样本池里找「有标注值但不在字典里」的意图名 → 建议补入字典。

    这就是"无法新增意图"的自动发现机制：不用人工想还有哪些缺失。
    """
    out = []
    try:
        ensure_table(conn)
        known = {r[0] for r in conn.execute("SELECT name FROM intent_taxonomy")}
        for r in conn.execute(
                "SELECT intent, COUNT(*) n FROM intent_samples "
                "WHERE status='confirmed' AND intent<>'' GROUP BY intent "
                "ORDER BY n DESC"):
            if r[0] not in known:
                out.append({"name": r[0], "count": r[1]})
            if len(out) >= limit:
                break
    except Exception:
        pass
    return out


def main(argv: list) -> int:
    db = os.path.join(ROOT, "mbse.db")
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    if "--seed" in argv:
        print(seed(conn))
    rows = list_all(conn)
    print("意图字典共 %d 条：" % len(rows))
    for r in rows:
        ex = len(r["examples"])
        al = len(r["aliases"])
        print("  %-26s %-14s cl=%-14s agent=%-20s 别名%d 示例%d %s"
              % (r["name"], r["label"], r["cluster"] or "-",
                 r["agent_name"] or "-", al, ex, r["status"]))
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
