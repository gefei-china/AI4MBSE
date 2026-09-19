# -*- coding: utf-8 -*-
"""SysML v2 校验工具 + 修复编排 Skill 注册（幂等，可重复执行）。

注册三件事：
  1. `tools` 表      —— `sysml_v2_validate`（本地 checker.jar 三路校验，source='local'）
  2. `agent_tools` 表 —— 绑定到建模相关 Agent（见 BINDINGS）
  3. `skills` 表     —— 「SysML v2 校验与修复」技能（**编排规则**：何时校验 / 怎么修 / 上限几轮）

────────────────────────────────────────────────────────────────────────────
为什么把规则放 DB（工具描述 + skill content），而不是写进 Python
────────────────────────────────────────────────────────────────────────────
「生成后必须自校验、有错则修、最多 3 轮」属**编排策略**，会随建模实践调整。写在 pipeline 里
意味着每次调整都要改代码 + 重启；写在 DB 里则**改文本即生效**，且能被前端「技能/工具」页面管理。
本脚本只负责"把定义灌进去"，不含任何判断逻辑。

⚠️ 本 skill **刻意不声明 `allowed_tools`**（这是有原因的，别"补全"它）：
  工程里 skill 的 allowed_tools 是**独占式白名单**——`execute.py:259-269` 命中后直接把它赋给
  `_tool_whitelist`，于是该回合**其它工具（RAG / 图谱 / 文件…）全部被禁**。
  工具能否被 LLM 看到，由 `agent_tools` 绑定 + JIT 保底决定，**不靠本 skill**。
  若确要收紧权限，应在 Agent 侧做，而不是在这里写一个会误伤其余能力的白名单。

用法：<repo>\\.venv\\Scripts\\python.exe -X utf8 register_sysml_check_tools.py
"""
import json
import sqlite3

from core.config import DB_PATH

# ─────────────────────────── ① 工具定义 ───────────────────────────
TOOL_DEFS = [
    {
        "name": "sysml_v2_validate",
        # 描述会随工具定义**每次**注入 LLM 的 tools 载荷，是最高优先级的常驻提示 →
        # 「生成后必须调用」这句话写在这里，比写在任何提示词里都可靠。
        "description": (
            "校验 SysML v2 源码（本地 checker.jar，官方语义）。返回**三路计数**"
            "（词法/语法/语义）与按位置聚合的错误清单（含出错行的源码原文与修复方向）。"
            "【必用场景】生成或修改任何 SysML v2 代码后，必须调用本工具自校验；"
            "若判据为 block（有词法/语法硬错），按诊断修复后**再次调用**本工具复核，最多 3 轮。"
            "修复时只改诊断指到的位置，不要为了通过而删改建模语义。"
            "参数 code 传**完整**的这段 V2 源码（不要传片段，否则会因引用缺失产生假错）。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "code": {"type": "string",
                         "description": "要校验的 SysML v2 源码**整段**（mode=code 时必填）"},
                "mode": {"type": "string", "enum": ["code", "project"],
                         "description": "code=单产物口径（默认，校验刚生成的这段）；"
                                        "project=项目级合并口径（校验 sysml_models/ 下已入库模型）"},
                "files": {"type": "string",
                          "description": "mode=project 时用：sysml_models/ 下的文件名，逗号分隔；"
                                         "留空=该目录下全部 .sysml"},
                "max_items": {"type": "integer",
                              "description": "返回的诊断条目上限（默认 20，按位置聚合后计数）"},
            },
            "required": [],
        },
        "side_effect": "read",
        "risk_level": "low",
        "version": "v1.0",
    },
]

# ─────────────────────────── ② Agent 绑定 ───────────────────────────
# 只绑「会产出/处理 V2 代码」的 Agent。绑定 = 该 Agent 的工具候选里出现校验工具
# （`tools.py:_build_tools_def` 的 TR-P3 分支从 agent_tools 联表注入，无需改 Python）。
BINDINGS = {
    "design": ["sysml_v2_validate"],        # 方案设计（主建模意图，_build_model_code_req 在此生效）
    "zhiyuan_mgmt": ["sysml_v2_validate"],  # 智源链路（已有远程 check；本地校验作补充，更快且离线可用）
}

# ─────────────────────────── ③ 技能（编排规则）───────────────────────────
SKILL_NAME = "SysML v2 校验与修复"
SKILL_MD = """---
name: SysML v2 校验与修复
description: 生成/修改 SysML v2 代码后自校验 → 按诊断修复 → 再校验（最多 3 轮），不伪造通过
category: AI建模
version: v1.0
---
# SysML v2 校验与修复

你负责让产出的 SysML v2 代码**在交付前就暴露错误**，而不是把错误留给人工发现。

## 何时走本流程

任何「产出或修改 SysML v2 代码」的回合，**都必须**走完下面的闭环 —— 包括：
新建模型、按需求改模型、按评审意见修模型、多方案生成。

## 闭环（最多 3 轮）

1. **生成**：产出 V2 代码。
2. **校验**：调用 `sysml_v2_validate`，`code` 传**完整**的这段代码。
   - 不要只传改动的片段：片段里引用了上一轮的类型，会被报成「解析不到引用」的假错。
3. **读诊断，按「词法 → 语法 → 语义」顺序处理**：
   - **词法/语法错（硬错）必须先清零**。硬错在场时，语义错大多是它的**级联产物**
     （语法错会破坏包结构，导致后续引用全部解析失败）—— 此时**逐条修语义错是白费功夫**，
     修好硬错后它们常会自己消失。
   - 语义错多为**建模决策**（例如「`satisfy` 必须引用 usage 而非 def」），按诊断提示判断，
     改不动或不确属错时可以保留并说明。
4. **修复**：只改诊断指到的位置。
   - **禁止**为了让代码"能编译过"而牺牲建模语义：不要把 `part def` 降级成 `attribute def`、
     不要删掉 `satisfy`/`refine` 关系、不要用注释掉代码的方式绕过错误。
5. **再校验**：修复后**再次调用** `sysml_v2_validate` 复核（同内容命中缓存，几乎零成本）。
6. **最多 3 轮**。3 轮后仍未通过 → **如实报告**：给出判据、剩余错误与位置，
   说明"已尝试 N 轮未通过"。**不要**声称已通过或已修复。

## 硬性纪律

- **不调用不得声称**：本回合没有调用过 `sysml_v2_validate`，就**不得**说模型"合法/校验通过"。
- **不伪造通过**：只有 `verdict=pass`，或 `verdict=report` 且你已确认剩余语义错可接受，
  才可以说"校验通过"。`report` 必须**说明**还剩余多少条语义错。
- **不盲改**：诊断没提到的位置不要动 —— 会引入新错误。
- **不掩盖**：`verdict=unavailable` 表示**校验器不可用、本次未真正校验**，
  必须如实说明"本次未能校验"，不要说成通过。

## 判据速查

| verdict | 含义 | 你该做什么 |
|---|---|---|
| `pass` | 无词法/语法错 | 可以交付 |
| `report` | 只有语义错（不阻断） | 判断是否处理；保留则须说明 |
| `block` | 存在词法/语法硬错 | **必须先修硬错**，再复核 |
| `unavailable` | 校验器不可用 | 如实说明未能校验，不声称通过 |

## 口径提醒

- `code` 传的是**单产物口径**：只校验这一段，跨文件引用会被报成语义错（假错）——
  这类假错**不会**触发 `block`（`block` 只由词法/语法错触发），不必为它改代码。
- 校验**已入库的模型文件**（`sysml_models/` 下的），用 `mode=project` + `files`，
  那是项目级合并口径，结论才代表整个工程。
"""

SKILL_TRIGGERS = [
    "校验", "检查模型", "模型检查", "校验模型", "语法错误", "语法错", "模型报错", "编译错误",
    "词法", "语义错", "修复模型", "模型修复", "sysml 校验", "sysml v2", "建模", "生成模型",
    "模型代码", "视图生成", "多方案",
]


def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        # ── ① 工具（幂等：先 INSERT OR IGNORE，再 UPDATE 刷新描述/schema）──
        ins = upd = 0
        for t in TOOL_DEFS:
            cur = conn.execute(
                "INSERT OR IGNORE INTO tools "
                "(name, source, description, status, input_schema, version, side_effect, risk_level, "
                " kind, scope, builtin) "
                "VALUES (?,?,?,?,?,?,?,?,'internal','public',0)",
                (t["name"], "local", t["description"], "active",
                 json.dumps(t["input_schema"], ensure_ascii=False),
                 t["version"], t["side_effect"], t["risk_level"]))
            ins += cur.rowcount
            # 描述/入参可能随版本更新 → 显式刷新（INSERT OR IGNORE 不会更新已存在行）
            conn.execute(
                "UPDATE tools SET description=?, input_schema=?, status='active', "
                "side_effect=?, risk_level=?, version=? WHERE name=?",
                (t["description"], json.dumps(t["input_schema"], ensure_ascii=False),
                 t["side_effect"], t["risk_level"], t["version"], t["name"]))
            upd += 1
        conn.commit()
        print(f"[register] tools：新增 {ins} 条，刷新 {upd} 条")

        # ── ② Agent 绑定（幂等）──
        bound, skipped = 0, []
        for agent_name, tool_names in BINDINGS.items():
            row = conn.execute("SELECT id FROM agents WHERE name=?", (agent_name,)).fetchone()
            if not row:
                skipped.append(agent_name)
                continue
            for tn in tool_names:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO agent_tools (agent_id, tool_type, tool_name, enabled) "
                    "VALUES (?, 'tool', ?, 1)", (row["id"], tn))
                bound += cur.rowcount
                # 已存在但被停用的 → 重新启用（确保本次注册意图落地）
                conn.execute(
                    "UPDATE agent_tools SET enabled=1 WHERE agent_id=? AND tool_type='tool' AND tool_name=?",
                    (row["id"], tn))
        conn.commit()
        print(f"[register] agent_tools：新增绑定 {bound} 条"
              + (f"；⚠️ 跳过不存在的 Agent：{skipped}" if skipped else ""))

        # ── ③ 技能（幂等 upsert）──
        trg = json.dumps(SKILL_TRIGGERS, ensure_ascii=False)
        row = conn.execute("SELECT id FROM skills WHERE name=?", (SKILL_NAME,)).fetchone()
        if row:
            conn.execute(
                "UPDATE skills SET description=?, skill_type='package', triggers=?, category=?, "
                "content=?, frontmatter=?, status='published', version='v1.0', source='manual', "
                "enabled=1, scope='public' WHERE name=?",
                ("AI 建模自校验闭环：生成/修改 V2 代码后调用 sysml_v2_validate 取三路诊断，"
                 "按位置修复并再次校验（最多 3 轮）；不伪造通过。",
                 trg, "AI建模", SKILL_MD, SKILL_MD, SKILL_NAME))
            print(f"[register] skills：已更新「{SKILL_NAME}」(id={row['id']})")
        else:
            conn.execute(
                "INSERT INTO skills (name, description, skill_type, triggers, category, content, "
                "frontmatter, status, version, source, enabled, scope, builtin) "
                "VALUES (?,?,'package',?,?,?,?,'published','v1.0','manual',1,'public',0)",
                (SKILL_NAME,
                 "AI 建模自校验闭环：生成/修改 V2 代码后调用 sysml_v2_validate 取三路诊断，"
                 "按位置修复并再次校验（最多 3 轮）；不伪造通过。",
                 trg, "AI建模", SKILL_MD, SKILL_MD))
            print(f"[register] skills：已创建「{SKILL_NAME}」")
        conn.commit()
    finally:
        conn.close()
    print("[register] 完成")


if __name__ == "__main__":
    main()
