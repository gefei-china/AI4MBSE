# -*- coding: utf-8 -*-
"""迁移：把需求条目化链路接成可用资产（2026-11-07，幂等可重跑）。

## 为什么需要这个脚本
2026-11-07 只读实测发现三个同时成立的矛盾事实：

| 事实 | 证据 |
|---|---|
| `services/tools/requirement_itemize_tools.py` 有完整实现（切句/分类/gaps/需求视图骨架） | 模块 300+ 行，`__main__` 自带 demo |
| **没有任何分发点 import 它** | `grep -rn "requirement_itemize_tools"` 只命中迁移脚本与文档 |
| tools 表有一行、agent 635 有绑定、技能正文教模型调用 | 46 次真实调用 → **全部**返回「未知工具」 |

⇒ 典型的「注册了 ≠ 接线了」。接线（两套分发点）已在代码层完成，本脚本负责**资产层**：
① 启用 `requirement_itemize`（原文状态 `disabled`，无任何停用留痕）；
② 新增 `requirement_commit`（写操作，走写前确认闸），补 `tools` 行 + `capability_registry` 行；
③ 绑定给「需求结构化Agent」并把两个工具加入技能 `sysml_requirement_structuring_guide` 的
   `allowed_tools`（否则技能命中后候选里没有它，模型会如实回答"工具集中没有"）。

判据：脚本重复执行结果一致（不新增重复行、不改已有 id）。
"""
import json
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.environ.get("MBSE_DB_PATH") or os.path.join(ROOT, "mbse.db")

ITEMIZE = "requirement_itemize"
COMMIT = "requirement_commit"
OWNER_AGENT = "requirement_structuring"
SKILL_NAME = "sysml_requirement_structuring_guide"

COMMIT_DESC = (
    "需求条目入库：把条目化结果写入需求实体表（requirement_items），"
    "得到项目内**稳定不变**的 REQ 编号供追溯引用。"
    "参数 items:条目数组（可直接用 requirement_itemize 的输出）或 text:原文（本工具先条目化）、"
    "gaps:待澄清数组、project:工程标识（缺省取会话归属）、version_id:关联的需求视图版本、"
    "source_ref:来源追溯。幂等：同一条需求重复提交只更新元数据不新增（幂等键 = 归一化文本 sha1）。"
    "写操作，需人工确认。"
)
COMMIT_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {"type": "array", "description": "条目数组（requirement_itemize 返回的 items）"},
        "gaps": {"type": "array", "description": "待澄清数组（requirement_itemize 返回的 gaps）"},
        "text": {"type": "string", "description": "需求原文；未给 items 时由本工具先条目化"},
        "domain": {"type": "string", "description": "领域标识"},
        "source_ref": {"type": "string", "description": "来源追溯标记"},
        "project": {"type": "string", "description": "工程标识；缺省取会话所属工程"},
        "version_id": {"type": "integer", "description": "关联的需求视图版本 id"},
    },
}


def main():
    if not os.path.exists(DB):
        print(f"[FAIL] 数据库不存在: {DB}")
        return 1
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    log = []

    # ① 启用 requirement_itemize（保持 side_effect=read：它是纯函数，不落库）
    row = conn.execute("SELECT id, status, side_effect FROM tools WHERE name=?", (ITEMIZE,)).fetchone()
    if not row:
        print(f"[FAIL] tools 表缺少 {ITEMIZE} 行 —— 请先跑 init_db")
        return 1
    if row["status"] != "active" or row["side_effect"] != "read":
        conn.execute("UPDATE tools SET status='active', side_effect='read', "
                     "side_effect_source='manual' WHERE id=?", (row["id"],))
        log.append(f"[1] {ITEMIZE}: status {row['status']}→active, side_effect {row['side_effect']}→read")
    else:
        log.append(f"[1] {ITEMIZE}: 已是 active/read，无需改动")

    # ② 新增/校正 requirement_commit
    c_row = conn.execute("SELECT id, status FROM tools WHERE name=?", (COMMIT,)).fetchone()
    if not c_row:
        cur = conn.execute(
            "INSERT INTO tools (name, source, description, status, input_schema, version, "
            "side_effect, risk_level, side_effect_source, scope) "
            "VALUES (?, 'local', ?, 'active', ?, 'v1.0', 'write', 'medium', 'manual', 'public')",
            (COMMIT, COMMIT_DESC, json.dumps(COMMIT_SCHEMA, ensure_ascii=False)))
        log.append(f"[2] {COMMIT}: 新增 tools 行 id={cur.lastrowid}（write/medium）")
    else:
        conn.execute("UPDATE tools SET status='active', side_effect='write', "
                     "side_effect_source='manual', description=? WHERE id=?",
                     (COMMIT_DESC, c_row["id"]))
        log.append(f"[2] {COMMIT}: 已存在 id={c_row['id']}，校正为 active/write")

    # ③ capability_registry（能力中心可见性）
    #    实际 schema 与预想不同：主键是 `capability_id`（形如 `tool:<name>`），
    #    没有 description 列（描述从 tools 表关联取），风险档存在 `risk_level` 列。
    for nm, disp, se in ((ITEMIZE, "需求条目化 / 需求视图骨架", "read"),
                         (COMMIT, "需求条目入库", "write")):
        cid = f"tool:{nm}"
        ex = conn.execute("SELECT capability_id FROM capability_registry WHERE capability_id=?",
                          (cid,)).fetchone()
        if ex:
            conn.execute("UPDATE capability_registry SET display_name=?, status='active', "
                         "risk_level=?, updated_at=CURRENT_TIMESTAMP WHERE capability_id=?",
                         (disp, se, cid))
            log.append(f"[3] capability_registry {nm}: 已存在，校正 active/{se}")
        else:
            conn.execute(
                "INSERT INTO capability_registry (capability_id, kind, display_name, source_table, "
                "source_id, status, lifecycle, version, publisher, visibility, risk_level, builtin) "
                "VALUES (?, 'tool', ?, 'tools', ?, 'active', 'published', 'v1.0', 'system', "
                "'public', ?, 0)", (cid, disp, nm, se))
            log.append(f"[3] capability_registry {nm}: 新增 active/{se}")

    # ④ 绑定给需求结构化 Agent
    ag = conn.execute("SELECT id, name FROM agents WHERE name=?", (OWNER_AGENT,)).fetchone()
    if not ag:
        log.append(f"[4] 跳过绑定：找不到 Agent {OWNER_AGENT}")
    else:
        for nm in (ITEMIZE, COMMIT):
            ex = conn.execute("SELECT id, enabled FROM agent_tools WHERE agent_id=? AND "
                              "tool_type='tool' AND tool_name=?", (ag["id"], nm)).fetchone()
            if ex:
                if not ex["enabled"]:
                    conn.execute("UPDATE agent_tools SET enabled=1 WHERE id=?", (ex["id"],))
                    log.append(f"[4] 绑定 {OWNER_AGENT}←{nm}: 重新启用")
                else:
                    log.append(f"[4] 绑定 {OWNER_AGENT}←{nm}: 已存在")
            else:
                conn.execute("INSERT INTO agent_tools (agent_id, tool_type, tool_name, enabled) "
                             "VALUES (?, 'tool', ?, 1)", (ag["id"], nm))
                log.append(f"[4] 绑定 {OWNER_AGENT}←{nm}: 新增")

    # ⑤ 技能的 allowed_tools —— 技能命中后候选集来自它，漏加等于模型看不见
    sk = conn.execute("SELECT id, allowed_tools FROM skills WHERE name=?", (SKILL_NAME,)).fetchone()
    if sk:
        try:
            allowed = json.loads(sk["allowed_tools"] or "[]")
        except Exception:
            allowed = []
        if not isinstance(allowed, list):
            allowed = []
        add = [n for n in (ITEMIZE, COMMIT) if n not in allowed]
        if add:
            allowed.extend(add)
            conn.execute("UPDATE skills SET allowed_tools=? WHERE id=?",
                         (json.dumps(allowed, ensure_ascii=False), sk["id"]))
            log.append(f"[5] 技能 {SKILL_NAME} allowed_tools += {add}")
        else:
            log.append(f"[5] 技能 {SKILL_NAME} allowed_tools 已含两个工具")
    else:
        log.append(f"[5] 跳过：找不到技能 {SKILL_NAME}")

    conn.commit()
    n = conn.execute("SELECT COUNT(*) FROM tools WHERE name IN (?,?)",
                     (ITEMIZE, COMMIT)).fetchone()[0]
    conn.close()
    for line in log:
        print(line)
    print(f"[OK] 迁移完成：两个需求工具就位（tools 命中 {n} 行）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
