# -*- coding: utf-8 -*-
"""修复：被 Studio 保存动作清空的 `agents.intent_keywords` + `design` 意图名丢失。

## 事故（本机真库，2026-10-30）
`repositories/agent_repo.update_agent` 原写法
    json.dumps(data.get("intent_keywords", []) or [])
把「调用方没传这个字段」和「调用方要清空」折叠成同一件事 ⇒
**任何不感知该字段的保存动作**（改显示名 / 改提示词 / 设团队成员）都会把关键词写成 `[]`。
后果链条（用户感受是"老是走不通 / MCP 工具不全"）：
    关键词清空 → 意图路由的关键词层对该 Agent 失效 → 请求落到 chat
    → chat 没有绑定该 MCP/技能工具 → 模型只能用别的手段瞎试 / 直接说"工具不全"。

同一天另有一步：Agent#2 的**标识名** `design` 被改成 `工程建模方案设计Agent`。
本工程里 **意图名 == Agent 标识名**（`agent/intent.py` 的 `_db_intents` 按 name 建），
所以改名等于**删掉意图 `design`** ⇒ 路由器仍会判出 design，但找不到 agent。

## 数据从哪来（不猜）
`asset_change_logs` 有字段级 diff：`field='intent_keywords'` 的 `old_value` 就是清空前的内容。
本脚本只按这份审计回滚，逐条打印 before/after，**不凭空造词**。

## 纪律
- 默认预演；`--apply` 才写。
- 只修「当前为空 且 审计里有非空原值」的 Agent（已恢复/本来就有词的一律不动）。
- 幂等：跑第二次应报 0 条待修。
- 每次写入都补一条 audit_logs（可追溯是谁修的）。

## 用法
    ./.venv/Scripts/python.exe -X utf8 tools/_repair_agent_keywords.py
    ./.venv/Scripts/python.exe -X utf8 tools/_repair_agent_keywords.py --apply
"""
import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

#: 本工程里「意图名」是路由与编排的硬契约（`_db_intents` 按 Agent.name 建）。
#: 这些名字不允许被"改名"抹掉 —— 展示名请改 display_name。
CORE_INTENTS = ("design", "review", "impact", "chat", "knowledge_qa",
                "report_generation", "requirement_analysis", "requirement_quality")


def _kw_list(raw):
    try:
        v = json.loads(raw or "[]")
        return [str(x) for x in (v or []) if str(x).strip()]
    except Exception:
        return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="写入（默认只预演）")
    a = ap.parse_args()

    from database import db_conn
    n_kw = n_name = 0
    with db_conn() as conn:
        print("=" * 78)
        print("[1] 恢复被清空的 intent_keywords（来源：asset_change_logs.old_value）")
        print("=" * 78)
        # 每个 agent 取**最近一次**把它清空的记录（old_value 非空、new_value 为空）
        rows = list(conn.execute(
            "SELECT l.asset_id, l.old_value, l.created_at, ag.name, ag.display_name, "
            "       COALESCE(ag.intent_keywords,'[]') AS cur "
            "FROM asset_change_logs l JOIN agents ag ON ag.id=l.asset_id "
            "WHERE l.field='intent_keywords' AND l.asset_type LIKE '%agent%' "
            "ORDER BY l.id DESC"))
        seen = set()
        for r in rows:
            aid = int(r["asset_id"])
            if aid in seen:
                continue                      # 只看最近一条即可
            seen.add(aid)
            old = _kw_list(r["old_value"])
            cur = _kw_list(r["cur"])
            if not old:
                continue                      # 审计里原值也空 ⇒ 无从恢复，且不该瞎补
            if cur:
                continue                      # 现在已有词 ⇒ 不动（可能人工补过了）
            print("  agent#%-5s %-26s 当前 [] → 恢复 %d 个词" % (aid, r["name"], len(old)))
            print("       %s" % "、".join(old[:10]) + ("…" if len(old) > 10 else ""))
            n_kw += 1
            if a.apply:
                conn.execute("UPDATE agents SET intent_keywords=?, updated_at=CURRENT_TIMESTAMP "
                             "WHERE id=?", (json.dumps(old, ensure_ascii=False), aid))
                conn.execute(
                    "INSERT INTO audit_logs (user_name,event_type,detail,result,created_at) "
                    "VALUES (?,?,?,?,CURRENT_TIMESTAMP)",
                    ("_repair_agent_keywords", "agent_update",
                     "修复：恢复 Agent#%s 的 %d 个意图关键词（该字段曾被保存动作清空）"
                     % (aid, len(old)), "success"))
        print("\n  待修 %d 个 Agent" % n_kw)

        print("\n" + "=" * 78)
        print("[2] 恢复核心意图的标识名（意图名 == Agent.name 是硬契约）")
        print("=" * 78)
        # 哪些核心意图名当前没有同名 Agent
        have = {r[0] for r in conn.execute(
            "SELECT name FROM agents WHERE status='active'")}
        missing = [c for c in CORE_INTENTS if c not in have]
        print("  缺失的核心意图名：%s" % (missing or "无"))
        # 找"像是被改名掉的那个"：审计里有 rename 且 old 是缺失的核心名
        for r in conn.execute(
                "SELECT l.asset_id, l.old_value, l.new_value, l.created_at "
                "FROM asset_change_logs l WHERE l.field='name' ORDER BY l.id DESC"):
            old = str(r["old_value"] or "").strip()
            if old not in missing:
                continue
            aid = int(r["asset_id"])
            cur = conn.execute("SELECT name, display_name FROM agents WHERE id=?",
                               (aid,)).fetchone()
            if not cur or cur["name"] == old:
                continue
            print("  agent#%-5s 标识名 %s → %s（展示名保留 %s）"
                  % (aid, cur["name"], old, cur["display_name"]))
            n_name += 1
            if a.apply:
                conn.execute("UPDATE agents SET name=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                             (old, aid))
                conn.execute(
                    "INSERT INTO audit_logs (user_name,event_type,detail,result,created_at) "
                    "VALUES (?,?,?,?,CURRENT_TIMESTAMP)",
                    ("_repair_agent_keywords", "agent_rename",
                     "修复：Agent#%s 标识名 %s 恢复为 %s（意图名与 Agent.name 是硬契约，"
                     "展示名请用 display_name）" % (aid, cur["name"], old), "success"))

        print("\n" + "=" * 78)
        print("[3] 核心意图目录现状")
        print("=" * 78)
        for c in CORE_INTENTS:
            row = conn.execute("SELECT id,name,display_name,intent_keywords FROM agents "
                               "WHERE name=? AND status='active'", (c,)).fetchone()
            if row:
                k = _kw_list(row["intent_keywords"])
                print("  ✓ %-22s agent#%-5s 关键词 %d 个" % (c, row["id"], len(k)))
            else:
                print("  ✗ %-22s **缺失**（路由会判出该意图却找不到 Agent）" % c)

        if a.apply:
            conn.commit()
    print("\n%s：关键词恢复 %d 个，标识名恢复 %d 个"
          % ("已写入" if a.apply else "[预演] 未写库", n_kw, n_name))
    if not a.apply:
        print("加 --apply 写入。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
