# -*- coding: utf-8 -*-
"""注册 code_exec / doc_parse 两个内置工具并绑定到 Agent（2026-11-06，P0）

## 为什么光"接代码"不够

`agent/pipeline_parts/tools.py:426-431` 的注入逻辑是：

    for name in agent_def.tools:                     # ← 只遍历 Agent **声明**过的
        if name in builtin_desc and not any(...):
            candidates.append((name, builtin_desc[name], "builtin", ""))

也就是说内置工具必须**同时**满足两件事才会进模型清单：

  ① 在 `BUILTIN_DESC`（运行时描述表）里        —— 代码层，已加
  ② 在 `agent_def.tools`（Agent 的工具声明）里  —— 数据层，本脚本负责

缺 ② 就是"加了三处登记但模型还是看不到"。这是本仓踩过的坑
（`save_artifact` 曾漏登记 `BUILTIN_DESC`，反向也一样：登记了但没声明 ⇒ 不注入）。

## 绑定策略

| 工具 | 绑定给谁 | 理由 |
|---|---|---|
| `code_exec` | 全部 active Agent | 通用计算能力，无持久副作用（沙箱内无文件/网络） |
| `doc_parse` | 全部 active Agent | 读文档是通用需求（用户上传 PDF/Word/Excel 的任何场景） |
| `skill_script_run` | **不全量绑**（2026-11-07 P1） | 脚本有文件/网络能力 ⇒ `side_effect=write`，只靠「技能命中 + 技能白名单」注入。全量绑等于给每个 Agent 发一把"跑任意受信脚本"的钥匙，与 side_effect 分级原则冲突 |

⚠️ `code_exec` / `doc_parse` 两者 `side_effect` 都是 `read`，因此**不受写意图闸限制**
   （不会被"这轮没提写操作"挡掉）。`doc_parse` 的路径保护复用 `file_tools._is_critical`。
⚠️ `skill_script_run` 是 `write` ⇒ 会过写前确认闸（HIL 策略决定要不要人点头），
   这是有意的：它能写文件、能发 HTTP，标 read 就是骗过闸门。

用法：
    ./.venv/Scripts/python.exe -X utf8 tools/register_capability_tools.py          # 执行
    ./.venv/Scripts/python.exe -X utf8 tools/register_capability_tools.py --dry-run  # 只看不写
"""
import json
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

DB = os.path.join(ROOT, "mbse.db")

#: 要注册的工具定义（从代码里的 *_TOOL_DEFS 取，避免两处维护漂移）
def _defs():
    from code_tools import CODE_TOOL_DEFS
    from doc_tools import DOC_TOOL_DEFS
    from skill_tools import SKILL_TOOL_DEFS
    from meta_tools import META_TOOL_DEFS
    return list(CODE_TOOL_DEFS) + list(DOC_TOOL_DEFS) + list(SKILL_TOOL_DEFS) + list(META_TOOL_DEFS)


#: ★ 2026-11-07（P1）：只绑定「读类、通用」工具给全部 Agent。
#: `skill_script_run` 是 **write**（脚本有文件/网络能力），不在这里全量绑定 ——
#: 它靠技能命中 + 技能白名单注入（`tools.py` 的 `if not _wl` 分支）。
#: 全量绑一个写类工具等于给每个 Agent 发一把"能跑任意受信脚本"的钥匙，
#: 与我们刚建立的 side_effect 分级原则直接冲突。
_BIND_ALL = {"code_exec", "doc_parse",
             # ★ 2026-11-07：`capability_query` 只读，**全量绑**。
             #   真机实测教训：用户问「平台有哪些 skill」被意图路由判进 `system_mgmt`，
             #   而它只绑了 6 个 sys_query_* ⇒ 模型看不到 capability_query，
             #   转而调 sys_query_monitor/audit 并**反问用户**（clarify_ask）。
             #   ⇒ "在会话里一站式管能力"的前提是：**被路由到的那个 Agent 得能查**。
             #   只读工具没有这个顾虑，全量绑即可，不依赖路由是否命中某个特定 Agent。
             "capability_query"}
#: ★ 2026-11-07（P2）：写类元能力只给"管理入口"性质的 Agent ——
#:   主 Agent（决策入口）+ `system_mgmt`（系统管理域，能力资产天然属它）。
#:   不给子 Agent：子 Agent 的职责是执行任务，不是改平台配置。
#:   ⚠️ 注意这**不降低安全性**：`capability_apply` 无论绑给谁，都只能写确认单，
#:      真正的变更要用户点批准（模型无法自批）。
_BIND_MAIN_ONLY = {"capability_apply"}
_BIND_ALSO = {"system_mgmt": {"capability_apply"}}


def _ensure_builtin_rows(conn, dry_run=False):
    """把内置工具写进 tools 表（source='builtin', builtin=1）。

    为什么要进表：`_build_tools_def` 会按需点名查 `tools` 表拿 `input_schema`
    （见 pipeline_parts/tools.py 的 `_inject_schema` 段）。不在表里 ⇒ 模型拿到的是
    空壳 schema（`"properties": {}`）⇒ 只能瞎猜参数。
    """
    added, updated = [], []
    for d in _defs():
        name = d["name"]
        row = conn.execute("SELECT id FROM tools WHERE name=?", (name,)).fetchone()
        schema = json.dumps(d.get("input_schema") or {}, ensure_ascii=False)
        if row:
            updated.append(name)
            if not dry_run:
                conn.execute(
                    "UPDATE tools SET source='builtin', builtin=1, status='active', "
                    "description=?, input_schema=?, side_effect=?, risk_level=?, version=? "
                    "WHERE id=?",
                    (d.get("description", ""), schema, d.get("side_effect", "read"),
                     d.get("risk_level", "low"), d.get("version", "v1.0"), row["id"]))
        else:
            added.append(name)
            if not dry_run:
                conn.execute(
                    "INSERT INTO tools (name, source, description, status, input_schema, "
                    "version, side_effect, risk_level, owner, kind, config, builtin) "
                    "VALUES (?, 'builtin', ?, 'active', ?, ?, ?, ?, '平台内置', '', '{}', 1)",
                    (name, d.get("description", ""), schema, d.get("version", "v1.0"),
                     d.get("side_effect", "read"), d.get("risk_level", "low")))
    return added, updated


def _bind_all_agents(conn, names, dry_run=False):
    """把工具绑定给 Agent（type='tool'）。返回 (新增数, 已存在数, Agent 数)。

    分两档（2026-11-07 P2）：
      · `_BIND_ALL`         → 全部 active Agent（读类通用能力）
      · `_BIND_MAIN_ONLY`   → 仅 `agent_role='main'`（能力资产的管理入口）
      · 其余（写类，如 skill_script_run）→ 不自动绑，靠技能命中/委派显式授权
    ⚠️ 不这样做的话：`skill_script_run` 会被发给每个子 Agent ⇒ 每个子 Agent 都有
       "跑任意受信脚本"的钥匙；而 `capability_*` 全不绑 ⇒ 用户"在会话里加能力"用不了。
    """
    _all_names = [n for n in names if n in _BIND_ALL]
    _main_names = [n for n in names if n in _BIND_MAIN_ONLY]
    agents = conn.execute(
        "SELECT id, name, agent_role FROM agents WHERE status='active'").fetchall()
    added = exist = 0
    for a in agents:
        for nm in _all_names:
            row = conn.execute(
                "SELECT 1 FROM agent_tools WHERE agent_id=? AND tool_type='tool' AND tool_name=?",
                (a["id"], nm)).fetchone()
            if row:
                exist += 1
                continue
            added += 1
            if not dry_run:
                conn.execute(
                    "INSERT INTO agent_tools (agent_id, tool_type, tool_name, enabled) "
                    "VALUES (?, 'tool', ?, 1)", (a["id"], nm))
        if str(a["agent_role"] or "") != "main":
            # 例外：`_BIND_ALSO` 里点名的 Agent（如 system_mgmt）也要绑
            _extra = _BIND_ALSO.get(str(a["name"] or ""), set())
            for nm in [n for n in _main_names if n in _extra]:
                row = conn.execute(
                    "SELECT 1 FROM agent_tools WHERE agent_id=? AND tool_type='tool' "
                    "AND tool_name=?", (a["id"], nm)).fetchone()
                if row:
                    exist += 1
                    continue
                added += 1
                if not dry_run:
                    conn.execute(
                        "INSERT INTO agent_tools (agent_id, tool_type, tool_name, enabled) "
                        "VALUES (?, 'tool', ?, 1)", (a["id"], nm))
            continue
        for nm in _main_names:
            row = conn.execute(
                "SELECT 1 FROM agent_tools WHERE agent_id=? AND tool_type='tool' AND tool_name=?",
                (a["id"], nm)).fetchone()
            if row:
                exist += 1
                continue
            added += 1
            if not dry_run:
                conn.execute(
                    "INSERT INTO agent_tools (agent_id, tool_type, tool_name, enabled) "
                    "VALUES (?, 'tool', ?, 1)", (a["id"], nm))
    return added, exist, len(agents)


def main():
    dry = "--dry-run" in sys.argv
    if not os.path.isfile(DB):
        print("数据库不存在：%s" % DB)
        return 1
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    try:
        added, updated = _ensure_builtin_rows(conn, dry_run=dry)
        names = [d["name"] for d in _defs()]
        b_add, b_exist, n_agents = _bind_all_agents(conn, names, dry_run=dry)
        if not dry:
            conn.commit()
        print("[register] 工具行：新增 %d（%s）/ 已存在更新 %d（%s）"
              % (len(added), ",".join(added) or "-", len(updated), ",".join(updated) or "-"))
        print("[register] 绑定：active Agent %d 个；新增绑定 %d 条，已存在 %d 条"
              % (n_agents, b_add, b_exist))
        print("[register] %s" % ("（dry-run，未写库）" if dry else "已提交"))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
