# -*- coding: utf-8 -*-
"""智源对接链路优化：Agent 提示词 + 技能描述 + 工具绑定（幂等，可重复执行）。

⚠️ **一次性脚本已执行完毕、不可重跑**：它**直接写库**（不是切分生成器），
但仍按幂等设计（每次执行前自备份、可重复运行得到同样结果）。

## 本批做了什么（2026-10-27，基于真机实测）

| # | 问题（实测证据） | 处置 |
|---|---|---|
| 1 | Agent「智源对接agent」system_prompt 仅 134 字符，且写"具体能力你通过mcp来探索" —— 把能力发现推给运行时 | 重写为「坐标先行 + 分工 + 7 条硬纪律 + 输出风格」，把**不可协商的纪律**固化进提示词（静态块，还利于 prompt cache） |
| 2 | 技能「智源平台管理」DB triggers 含「包/元素/建模/校验/视图/新增/删除/移动/导入/导出」等泛词 ⇒ 实测「生成需求视图」也命中它，**一次输入命中 3 个技能** | triggers 改为**精确词 + 合取词组**（`新增+包`），并与技能正文 frontmatter **对齐为同一份** |
| 3 | 技能正文里写的坐标系三件套，**工具清单里可能没有**（实测「新增测试包」时 query_projects/query_branches/query_model_versions 全被裁掉） | 已在 `agent/mcp_tool_meta.py` 增加**寻址前置链闭包**（代码侧），本脚本负责提示词与技能侧对齐 |
| 4 | 8 个视图 skill 正文点名 `sysml_v2_lint` / `sysml_ast_extract` / `sysml_stdlib_meta`，但 Agent 735 没绑这些工具 ⇒ **模型被要求调用它看不见的工具** | 给 735 补绑 4 个本地 SysML 工具；给 8 个视图 skill 的 `allowed_tools` 补上正文点名的工具 |
| 5 | Agent 735 的 description 只有一句，语义路由信号弱 | 补齐坐标系与能力关键词 |

## 运行

```bash
./.venv/Scripts/python.exe -X utf8 tools/optimize_zhiyuan_agent.py          # 先干跑（打印将做什么）
./.venv/Scripts/python.exe -X utf8 tools/optimize_zhiyuan_agent.py --apply  # 落库（自动备份）
```

幂等：重复执行 `--apply` 结果一致；每次 `--apply` 生成一份 `backups/mbse.db.bak-before-zyx-<ts>`。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

DB = os.path.join(REPO, "mbse.db")
AGENT_ID = 735          # 「智源对接agent」
SKILL_NAME = "智源平台管理"

# ─────────────────────────────────────────────────────────────────────
# 1) Agent 735 的 system_prompt（重写）
# ─────────────────────────────────────────────────────────────────────
NEW_SYSTEM_PROMPT = """你是「智源对接 agent」——直连智源 MBSE 平台（SE-MBSE）的建模执行者。
平台的一切操作都通过 zhiyuan-dev-mcp 提供的 MCP 工具完成；本回合可用的工具以系统给出的清单为准，
技能正文（尤其「智源平台管理」）是操作协议的唯一权威。

## 坐标先行：三个坐标是所有操作的前提
`query_projects` → projectId（工程）；`query_branches` → branchId（分支）；
`query_model_versions`（带 includeDraft=true）→ versionId（版本）。
任何写操作、源码导出、包树浏览，都必须先拿到完整三元组（工程/分支/版本）。
若本回合清单里没有这三个工具，说明它没被暴露出来 —— 不要臆造 projectId/branchId/versionId，
先用能拿到的查询类工具逐级定位；确实拿不到就如实说明并请用户确认工程与分支。

## 分工（平台侧与本地侧各管一段，不要互相顶替）
- 平台侧（智源 MCP）：查工程/分支/版本/包树/元素、建/改/删元素、导出源码、覆盖导入、平台侧语法检测
- 本地侧：产出 SysML v2 代码用 `sysml_v2_validate` 自校验（三路诊断：词法/语法/语义）；
  视图展开用 `sysml_ast_extract` 抽结构、`sysml_stdlib_meta` 查标准库、`sysml_v2_lint` 查命名
- 两者都可用时：**先本地校验通过，再导入平台**；平台侧负责搬运与落库，不替代本地校验

## 硬纪律
1. 写操作（新增/修改/删除/覆盖导入/应用变更）前，先复述「父容器 + 变更内容 + 目标版本」并取得用户明确确认
2. 删除类先 `get_model_delete_impact` 看影响面，再请求确认
3. 只有草稿（DRAFT）版本可写；写已发布版本会返回 PERMISSION_DENIED —— 遇到就如实说明"需要草稿版本"，不要反复重试
4. 工具报错时按返回的 `fields[].expected` 与 `recovery.instructions` 改参数，不要同参数重试、不要臆造参数名
5. 没有工具回执就不许说"已完成/已写入/已导出"；写完必须回读校验（`query_data_tree` / `search_model_elements` 确认已挂上）
6. 本地代码校验 `verdict=block` 时按诊断定位修复并重验，最多 3 轮；不得声称未实际发生的通过
7. 遇到确实无法确认的内容，用澄清卡向我确认，不要自己假设

## 输出风格
结论先行；包树用缩进列表并附真实 dataId；源码用 ```sysml 代码块；
给出真实的工程/分支/版本/元素标识符，不确定就标注"待确认"。"""

NEW_AGENT_DESC = ("直连智源 MBSE 平台（SE-MBSE）的建模执行者：工程/分支/版本坐标查询、包结构树浏览、"
                  "元素新增修改删除、SysML v2 源码导出与覆盖导入、平台侧语法检测；"
                  "并可在本地产出 SysML v2 代码、用 sysml_v2_validate 自校验后再写回智源。"
                  "涉及智源工程、包结构、包树、元素、源码导入导出、语法检测的任务由本 Agent 承接。")

# ─────────────────────────────────────────────────────────────────────
# 2) 技能「智源平台管理」：triggers / description / content
# ─────────────────────────────────────────────────────────────────────
# triggers：**精确词 + 合取词组**。
#   合取写法 `A+B` 由 skills.py::_trigger_hit 支持（向后兼容：无 `+` 的旧词行为不变）。
#   实测依据：原 triggers 含「包/元素/建模/校验/视图」等泛词 ⇒ 「生成需求视图」也命中本技能。
NEW_TRIGGERS = [
    # —— 平台标识（最特异，放前面）——
    "智源", "zhiyuan", "SE-MBSE",
    # —— 读：坐标与结构 ——
    "工程列表", "包结构", "包树", "工程树", "数据树", "模型版本",
    # —— 读：源码 ——
    "导出+源码", "导入+源码", "拉取+源码", "写回+智源", "导入+智源",
    # —— 写：包 ——
    "新增+包", "创建+包", "新建+包", "删除+包", "移除+包", "修改+包", "更新+包", "改名+包", "移动+包",
    # —— 写：元素 ——
    "新增+元素", "创建+元素", "删除+元素", "移除+元素", "修改+元素", "更新+元素", "改名+元素", "移动+元素",
    # —— 其它 ——
    "语法检测", "token",
]

NEW_SKILL_DESC = ("智源MBSE平台（SE-MBSE）直连管理：工程/分支/版本坐标、包结构树、元素增删改、"
                  "SysML v2 源码导出与覆盖导入、语法检测")

NEW_SKILL_CONTENT = """---
name: 智源平台管理
description: 智源MBSE平台（SE-MBSE）直连管理：工程/分支/版本坐标、包结构树、元素增删改、SysML v2 源码导出与覆盖导入、语法检测
triggers: [智源, zhiyuan, SE-MBSE, 工程列表, 包结构, 包树, 工程树, 数据树, 模型版本, 导出+源码, 导入+源码, 拉取+源码, 写回+智源, 导入+智源, 新增+包, 创建+包, 新建+包, 删除+包, 移除+包, 修改+包, 更新+包, 改名+包, 移动+包, 新增+元素, 创建+元素, 删除+元素, 移除+元素, 修改+元素, 更新+元素, 改名+元素, 移动+元素, 语法检测, token]
category: 平台集成
author: 平台内置
version: v1.1
---
# 智源平台管理技能

你通过 **zhiyuan-dev-mcp** 直连智源 MBSE 平台（SE-MBSE）。工具名以服务端实际注册为准（下表是**真实工具名**）。

## 零、坐标系铁律：一切请求都从这三项开始

| 步骤 | 工具 | 关键参数 | 拿到什么 |
|---|---|---|---|
| 找工程 | `query_projects` | `keyword` | `projectId` |
| 找分支 | `query_branches` | `projectId` | `branchId` |
| 找版本 | `query_model_versions` | `projectId`, `branchId`, **`includeDraft: true`** | `versionId` + `status`(RELEASED/DRAFT) |

**三者逐级依赖**：不知道工程就查不到分支，不知道分支就查不到版本。
后续所有工具的 `workspace` / `version` 对象里都要填这三个值。

> ⚠️ **`query_model_versions` 默认只返回「已发布启用」版本，草稿版本必须显式 `includeDraft: true` 才会出现。**
> 这是"查不到版本 / 写不进去"的头号原因：看不到草稿版本 → 拿已发布版本去写 → `PERMISSION_DENIED`。

> ⚠️ **若本回合工具清单里没有这三个查询工具**：不要臆造 projectId/branchId/versionId。
> 先用清单里已有的查询类工具（`get_modeling_context` / `search_model_elements` / `query_data_tree`）
> 逐级定位；仍拿不到就**如实告诉用户"当前工具清单无法定位工程坐标，请指定工程与分支"**，不要盲试。

## 分组字段：`version` vs `workspace`（**同一台 server 内两种写法并存**）
两种都传同一个对象 `{projectId, branchId, versionId}`，但**字段名不同，不能互换**：

| 工具 | 分组字段名 |
|---|---|
| `query_data_tree` | **`version`** |
| `search_model_elements` | **`workspace`** |
| `get_modeling_capabilities` | **`workspace`** |
| `get_model_elements` | **`version`** |
| `create_model_elements` / `create_model_relationships` / `create_model_view` | **`workspace`** |
| `export_sysml_v2` | `version`（内含 `projectId/branchId/versionId`） |

传错字段名会得到 `INVALID_ARGUMENTS`，且返回的 `fields[].expected` 会列出该工具**完整合法字段集** ——照它改，不要同参数重试。

## 一、常见任务的最短路径

| 用户诉求 | 最短工具路径 |
|---|---|
| 「有哪些工程 / 某个工程有哪些分支」 | `query_projects` → `query_branches` |
| 「看这个工程的包结构 / 某个包里有什么」 | `query_model_versions(includeDraft=true)` → `query_data_tree`（逐层用返回的 dataId 展开） |
| 「按名字找元素」（跨全部层级，首选） | `search_model_elements(keyword=..., workspace=草稿, includeReadToken=true)` → `items[].elementId` |
| 「新建一个包 / 新建一个元素」 | `search_model_elements` 定位父 → `get_modeling_capabilities` 取 creationRef → `create_model_elements` → 回读校验 |
| 「改这个元素的属性 / 删掉这个元素」 | `search_model_elements` 定位 → `update_model_elements` / 先 `get_model_delete_impact` 再 `delete_model_elements` |
| 「把当前模型导出成 SysML v2 源码」 | `export_sysml_v2` 四段协议（见下） |
| 「把这段代码写回智源」 | 本地 `sysml_v2_validate` 通过 → 复述目标包与影响面 → 用户确认 → `import_sysml_text` → 回读校验 |
| 「这段代码在智源侧语法对不对」 | `validate_sysml_text` |

## 二、写操作三段链（新增包 / 新增元素）——顺序不能跳
实测可复现（在「通信子系统用例」下建「测试」包成功）：

1. **定位父元素**（二选一，优先 1）
   - `search_model_elements(keyword="包名", workspace=草稿, includeReadToken=true)` → `items[].elementId`（**首选**：一次跨全部层级，无需逐层展开）
   - `query_data_tree(version=草稿, parentId=上层, pageSize=100)` → 逐层展开太慢，仅用于浏览
2. **取创建授权**
   - `get_modeling_capabilities(parentId=父 elementId, workspace=草稿)`
   - 结果里 `creatable[]` 是一组可创建类型，逐个按 `type` 找：
     - `creationRef`（如 `BASIC:PACKAGE`）← **就在 `creatable[]` 元素里，不在结果顶层**
     - `ownerId` 用结果顶层的 **`creationOwnerId`**
   - 若 `nextOffset` 非空，说明还有下一页可创建类型，**沿 nextOffset 读完**才完整
3. **执行创建**
   - `create_model_elements(creationRef, name, ownerId=creationOwnerId, requestId=新uuid, workspace=草稿)`
   - `requestId` 每次新生成（幂等键：重复提交同一 requestId 只会生效一次）
4. **回读校验**（必做）
   - `query_data_tree(version=草稿, parentId=父, pageSize=100)` 确认新元素已挂上
   - 不能只看"提交成功"就收工

## 三、写操作的版本铁律
- **只有草稿（DRAFT）版本可写**。在 RELEASED 版本上写一律返回
  `PERMISSION_DENIED`（`recovery.instructions` 会提示"改用有权限且可编辑的目标"）。
- 没有草稿版本时，**如实告知用户"当前只有已发布版本，需先在平台创建草稿版本"**，
  不要反复用已发布版本重试，也不要声称"已写入"。
- 写入后再次 `query_model_versions(includeDraft=true)` 复核该草稿版本仍在（未被发布覆盖）。

## 四、源码导出（只读，四段协议）
`export_sysml_v2` 是**异步分页协议**：`PREVIEW` → `EXPORT`（带 `confirmationToken` + `confirmed=true`）→
`STATUS`（轮询直到 COMPLETED）→ `READ`（按 `nextOffset` 分段读，`eof=true` 为止）。

- **PREVIEW** 先把范围摘要展示给用户并取得确认，才允许 EXPORT；
- 大文件必须分段读并拼接，**单段不是全文**；拼齐后才算取到；
- 结果里 `sizeBytes` 是权威字节数，可用它校验拼接是否完整。

## 五、报错自愈（照结构化提示改，不要同参数重试）
| 返回 | 含义 | 应对 |
|---|---|---|
| `INVALID_ARGUMENTS` + `fields[].expected` | 字段名/类型不对 | 按 `expected` 列出的字段重写参数（常见：`version`↔`workspace`、对象 vs 字符串） |
| `PERMISSION_DENIED` + `recovery.nextTools` | 无权限/版本只读 | 读 `recovery.instructions`；通常是「没用草稿版本」或「Key 缺 scope」 |
| 列表/版本查询返回空 | 可能是只看已发布 | 加 `includeDraft: true` 或先确认父节点是否正确 |
| `scanLimited: true` | 搜索被扫描上限截断 | 用 `afterId=nextAfterId` 继续翻页 |

## 六、安全与流程规范
1. 查询类直接执行；**任何写操作前必须复述「父容器 + 新增/修改内容 + 目标草稿版本」并获用户确认**
2. 删除类先 `get_model_delete_impact` 看影响面，再经用户确认
3. 写操作完成后必须回读校验，回复中给出**真实 dataId / 名称 / 所在版本**
4. 不得虚构"已完成/已写入/已归档"：没有工具回执就不能声称完成
5. 本地产出的 SysML v2 代码**先过 `sysml_v2_validate`**（词法/语法/语义三路诊断）再导入平台；
   `verdict=block` 时按诊断修复并重验，最多 3 轮，**不得伪造通过**
6. token 过期（401）：平台系统配置页更新，或 `PUT /api/system/config/static`
   body `{"updates": {"zhiyuan.token": "<新token>"}}`
"""

# ─────────────────────────────────────────────────────────────────────
# 3) 工具绑定：Agent 735 补 4 个本地 SysML 工具
# ─────────────────────────────────────────────────────────────────────
NEW_TOOL_BINDS = ["sysml_ast_extract", "sysml_stdlib_meta", "sysml_v2_lint", "sysml_v2_project_check"]

# 8 个视图 skill 的 allowed_tools 补齐正文点名的工具
VIEW_SKILLS = [
    "sysml_view_generation_requirement", "sysml_view_generation_structure",
    "sysml_view_generation_usecase", "sysml_view_generation_activity",
    "sysml_view_generation_ibd", "sysml_view_generation_sequence",
    "sysml_view_generation_state", "sysml_view_generation_parameter",
]
VIEW_SKILL_TOOLS = ["graph_retrieve", "sysml_ast_extract", "sysml_stdlib_meta",
                    "sysml_v2_validate", "sysml_v2_lint", "sysml_v2_autofix"]
# ⚠️ **必须含 `sysml_v2_autofix`**（2026-10-27 实测踩坑）：
#   skill 的 `allowed_tools` 在 **execute 路径**是**独占式白名单**
#   （`execute.py::_skill_allowed_tools` 直接赋给 `_tool_whitelist`）⇒ 白名单里没有的工具
#   **整类不可见**。
#   本批原先把 8 个视图 skill 的 `allowed_tools` 统一成 5 项（**漏了 autofix**），
#   结果 `verify_p0_repair_round_tools` 的「收尾轮含 autofix」判据翻红
#   —— 收尾修复轮的 `tools` 变成 0 个 ⇒ **模型报错了却没人能修**。
#   ⇒ 凡技能正文点名要用的工具，白名单必须**逐个对齐**（正文点名 `sysml_v2_autofix`
#   的 `sysml_view_generation_guide` 就是漏网的证据）。

#: 需要**重建 frontmatter 列**的技能（frontmatter 与 content 的 YAML 头必须同源）。
#: 除本批改过的「智源平台管理」外，还含 `sysml_view_generation_guide`
#: —— 它的 frontmatter 描述是旧版措辞（实测「八视图按 需求→…」vs content「八视图：…」），
#: 属**改动前就存在**的失同步，一并收口。
FM_SYNC_SKILLS = [SKILL_NAME] + VIEW_SKILLS + ["sysml_view_generation_guide"]


def _extract_yaml(text: str) -> str:
    """从 content 抽 YAML 头（含 `---` 分隔符，与 `skills.frontmatter` 列的存量格式一致）。"""
    t = text or ""
    if not t.startswith("---"):
        return ""
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?", t, re.S)
    return ("---\n%s\n---" % m.group(1)) if m else ""


def backup_db() -> str:
    ts = time.strftime("%Y%m%d_%H%M%S")
    dst = os.path.join(REPO, "backups", "mbse.db.bak-before-zyx-%s" % ts)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    src = sqlite3.connect(DB)
    try:
        bk = sqlite3.connect(dst)
        try:
            src.backup(bk)
        finally:
            bk.close()
    finally:
        src.close()
    return dst


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正落库（默认干跑）")
    args = ap.parse_args()

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    plan = []

    # ── 1) Agent 735 ──
    a = cur.execute("SELECT id, name, display_name, system_prompt, description FROM agents WHERE id=?",
                     (AGENT_ID,)).fetchone()
    if not a:
        print("!! 找不到 agent id=%d" % AGENT_ID)
        return 2
    print("== Agent %s（%s）==" % (a["id"], a["display_name"]))
    if a["system_prompt"] != NEW_SYSTEM_PROMPT:
        plan.append(("agent.system_prompt", len(a["system_prompt"] or ""), len(NEW_SYSTEM_PROMPT)))
    if (a["description"] or "") != NEW_AGENT_DESC:
        plan.append(("agent.description", len(a["description"] or ""), len(NEW_AGENT_DESC)))

    # ── 2) 技能 ──
    s = cur.execute("SELECT id, description, triggers, content, allowed_tools FROM skills WHERE name=?",
                    (SKILL_NAME,)).fetchone()
    if not s:
        print("!! 找不到技能 %s" % SKILL_NAME)
        return 2
    print("== Skill %s（id=%s）==" % (SKILL_NAME, s["id"]))
    new_trig = json.dumps(NEW_TRIGGERS, ensure_ascii=False)
    if (s["triggers"] or "") != new_trig:
        plan.append(("skill.triggers", len(s["triggers"] or ""), len(new_trig)))
    if (s["description"] or "") != NEW_SKILL_DESC:
        plan.append(("skill.description", len(s["description"] or ""), len(NEW_SKILL_DESC)))
    if (s["content"] or "") != NEW_SKILL_CONTENT:
        plan.append(("skill.content", len(s["content"] or ""), len(NEW_SKILL_CONTENT)))

    # 智源平台管理 allowed_tools：补本地校验工具
    at = set(json.loads(s["allowed_tools"] or "[]"))
    if "sysml_v2_validate" not in at:
        at.add("sysml_v2_validate")
        plan.append(("skill.allowed_tools(+sysml_v2_validate)", len(s["allowed_tools"] or ""),
                     len(json.dumps(sorted(at), ensure_ascii=False))))

    # ── 3) 视图 skill 的 allowed_tools 补齐 ──
    for nm in VIEW_SKILLS:
        row = cur.execute("SELECT id, allowed_tools FROM skills WHERE name=?", (nm,)).fetchone()
        if not row:
            continue
        cur_at = set(json.loads(row["allowed_tools"] or "[]"))
        if cur_at == set(VIEW_SKILL_TOOLS):
            continue
        plan.append(("skill.allowed_tools[%s]" % nm, len(row["allowed_tools"] or ""),
                     len(json.dumps(VIEW_SKILL_TOOLS, ensure_ascii=False))))

    # ── 3) frontmatter 列与 content 的 YAML 头**同源**（注入侧只取 frontmatter[:400]，
    #       不同源 ⇒ 模型看到的是旧版开头，与运行时注入自相矛盾）──
    #       ⚠️ 改动 content 的 YAML 头**必须**同步重建 frontmatter 列，否则门禁 [4] 翻红。
    for nm in FM_SYNC_SKILLS:
        row = cur.execute("SELECT frontmatter, content FROM skills WHERE name=?", (nm,)).fetchone()
        if not row:
            continue
        want = _extract_yaml(row["content"] or "")
        if want and (row["frontmatter"] or "").strip() != want.strip():
            plan.append(("skill.frontmatter[%s]" % nm, len(row["frontmatter"] or ""), len(want)))

    # ── 4) Agent 735 的工具绑定 ──
    have = {r["tool_name"] for r in cur.execute(
        "SELECT tool_name FROM agent_tools WHERE agent_id=? AND tool_type='tool' AND enabled=1",
        (AGENT_ID,)).fetchall()}
    for t in NEW_TOOL_BINDS:
        if t not in have:
            plan.append(("agent_tools.bind[%s]" % t, 0, 1))

    print("\n== 变更计划（%d 项）==" % len(plan))
    for tag, old, new in plan:
        print("   - %-46s %s -> %s" % (tag, old, new))
    if not plan:
        print("   （无变更，幂等）")

    if not args.apply:
        print("\n干跑结束。加 --apply 落库。")
        conn.close()
        return 0

    bak = backup_db()
    print("\n已备份 -> %s" % bak)

    # ── 落库 ──
    cur.execute("UPDATE agents SET system_prompt=?, description=? WHERE id=?",
                (NEW_SYSTEM_PROMPT, NEW_AGENT_DESC, AGENT_ID))
    cur.execute("UPDATE skills SET description=?, triggers=?, content=?, allowed_tools=? WHERE name=?",
                (NEW_SKILL_DESC, new_trig, NEW_SKILL_CONTENT,
                 json.dumps(sorted(at), ensure_ascii=False), SKILL_NAME))
    for nm in VIEW_SKILLS:
        cur.execute("UPDATE skills SET allowed_tools=? WHERE name=?",
                    (json.dumps(VIEW_SKILL_TOOLS, ensure_ascii=False), nm))
    # frontmatter 列 ← content 的 YAML 头（两者必须同源）
    for nm in FM_SYNC_SKILLS:
        row = cur.execute("SELECT content FROM skills WHERE name=?", (nm,)).fetchone()
        if not row:
            continue
        want = _extract_yaml(row["content"] or "")
        if want:
            cur.execute("UPDATE skills SET frontmatter=? WHERE name=?", (want, nm))
    for t in NEW_TOOL_BINDS:
        row = cur.execute(
            "SELECT id FROM agent_tools WHERE agent_id=? AND tool_type='tool' AND tool_name=?",
            (AGENT_ID, t)).fetchone()
        if row:
            cur.execute("UPDATE agent_tools SET enabled=1 WHERE id=?", (row["id"],))
        else:
            cur.execute(
                "INSERT INTO agent_tools (agent_id, tool_type, tool_name, enabled, created_at) "
                "VALUES (?,?,?,?,datetime('now'))", (AGENT_ID, "tool", t, 1))
    conn.commit()

    # ── 自证：回读 + 断言 ──
    a2 = cur.execute("SELECT system_prompt, description FROM agents WHERE id=?", (AGENT_ID,)).fetchone()
    s2 = cur.execute("SELECT description, triggers, content, allowed_tools FROM skills WHERE name=?",
                     (SKILL_NAME,)).fetchone()
    assert a2["system_prompt"] == NEW_SYSTEM_PROMPT, "system_prompt 回读不一致"
    assert a2["description"] == NEW_AGENT_DESC, "description 回读不一致"
    assert s2["content"] == NEW_SKILL_CONTENT, "skill content 回读不一致"
    assert json.loads(s2["triggers"]) == NEW_TRIGGERS, "triggers 回读不一致"
    binds = {r["tool_name"] for r in cur.execute(
        "SELECT tool_name FROM agent_tools WHERE agent_id=? AND tool_type='tool' AND enabled=1",
        (AGENT_ID,)).fetchall()}
    missing = [t for t in NEW_TOOL_BINDS if t not in binds]
    assert not missing, "工具绑定缺失: %s" % missing
    print("\n自证通过：回读逐字节一致，4 个工具绑定在位。")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
