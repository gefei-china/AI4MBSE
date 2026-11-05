# -*- coding: utf-8 -*-
"""生成「智源 MCP 工具手册」技能包（2026-10-30）。

## 为什么生成而不是手写
`zhiyuan-dev-mcp` 有 **71 个工具**，每个都带真实 `inputSchema`（必填项、枚举、
`additionalProperties:false`）。手写文档必然发生两类漂移：
  ① 服务端加/改工具后，文档静默过期（没人会主动回来改 71 条）；
  ② 参数名靠记忆写，错一个字就变成"文档教模型传错参数"（比没文档更糟）。
所以真相只有一个：**`mcp_servers.tool_meta`**（MCP `list_tools` 的落库结果）。
本脚本从它生成 `references/`，并把"分类覆盖"做成硬断言 —— 将来新增工具未归类，
生成直接失败，而不是漏在文档外。

## 产出
    static/skill_packages/zhiyuan_mcp_tools/
        skill.md                    入口（手写正文 + 自动统计）
        references/工具目录.md       71 个工具完整契约（自动）
        references/参数速查.md       公共参数（workspace/requestId/readToken/分页）解析（自动）
        references/任务配方.md       端到端调用链（手写，见 RECIPES）
        scripts/dump_tools.py       重生成器（拷本脚本，供运维就地重跑）
        scripts/lookup.py           按关键词检索工具（"哪个工具能做 X"）

## 用法
    ./.venv/Scripts/python.exe -X utf8 tools/gen_zhiyuan_mcp_skill.py
    ./.venv/Scripts/python.exe -X utf8 tools/gen_zhiyuan_mcp_skill.py --check   # 只校验不写
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PKG = os.path.join(ROOT, "static", "skill_packages", "zhiyuan_mcp_tools")

#: 工具 → 分类。**必须显式列全**（生成时断言 71/71，漏一个就失败）。
#: 为什么不用"按前缀猜"：`get_model_*` 里既有纯查询（get_model_text）也有
#: 校验规则表（get_model_validation_rules），猜出来的分类会让人找错工具。
GROUPS = {
    "① 坐标与导航（先拿到 workspace）": [
        "query_projects", "query_branches", "query_model_versions", "query_data_tree",
        "search_model_elements", "get_modeling_context", "get_modeling_capabilities",
        "get_model_elements", "get_model_features", "get_model_text",
        "get_model_associations", "get_model_relationships", "get_model_relationships_batch",
        "query_reference_candidates",
    ],
    "② 导出": ["export_sysml_v2"],
    "③ 变更执行（预览 → 提交 → 查状态 → 撤销）": [
        "preview_model_changes", "apply_model_changes",
        "get_model_operation", "undo_model_operation",
    ],
    "④ 元素增删改": [
        "create_model_elements", "update_model_elements", "delete_model_elements",
        "clone_model_elements", "move_model_elements", "get_model_delete_impact",
        "update_model_properties", "update_model_features",
        "get_model_custom_properties", "update_model_custom_properties",
        "set_feature_value", "set_specializations",
        "set_model_expression", "get_model_expression",
        "set_model_attribute_declaration", "set_model_type_relations",
    ],
    "⑤ 关系（语义连线）": [
        "create_model_relationships", "update_model_relationships",
        "delete_model_relationships", "update_model_relationship_properties",
    ],
    "⑥ 视图（画布 / 图元 / 布局 / 表格）": [
        "get_model_diagram_guide", "create_model_view", "get_model_view",
        "update_model_view", "configure_model_view", "get_model_view_guidance",
        "create_model_view_element", "present_model_elements", "present_model_relationship",
        "remove_view_presentations", "update_view_presentations", "update_view_styles",
        "layout_model_view", "generate_model_view", "query_model_table",
        "set_state_diagram_labels", "set_model_expression_visibility",
        "place_model_activity_nodes",
    ],
    "⑦ 状态机 / 行为语义": [
        "get_state_machine_semantics", "configure_state_transition", "configure_state_actions",
    ],
    "⑧ 校验": ["get_model_validation_rules", "validate_model", "validate_sysml_text"],
    "⑨ 导入与命名空间": ["import_sysml_text", "manage_model_imports"],
    "⑩ 注释 / 文档": [
        "annotate_model_elements", "get_model_annotations", "update_model_annotation",
    ],
    "⑪ 自动化与变体": [
        "get_model_automation_options", "run_modeling_automation", "manage_model_variants",
    ],
}

#: 公共参数：所有工具都出现、且语义固定的那几个。单独成章，避免在 71 条里重复。
COMMON_PARAMS = {
    "workspace": (
        "**工程坐标系，几乎每个工具必填**，三个字段都 required、且必须是**数据库 id 字符串**：\n"
        "```json\n{\"projectId\": \"2113198870736076800\",\n"
        " \"branchId\": \"2113198870744465408\",\n"
        " \"versionId\": \"2115373400643141632\"}\n```\n"
        "- `versionId` 必须来自 `query_model_versions` 的返回，**不能用版本名称**（如 `V4` 是名字不是 id）。\n"
        "- **只有 RELEASED 的版本是只读快照**；要写入必须用 `status=DRAFT` 的版本。\n"
        "- 拿不到三元组就不要猜 —— 先按「任务配方 · 配方1」查一遍。"),
    "requestId": (
        "**写入类工具的幂等键**（必填）。同一个 `requestId` 重复提交 = 同一笔操作，不会双写。\n"
        "约定：用 `uuid4()` 或 `run-<批次>-<任务>-<序号>`；**不要复用上一次的 id** 去做另一笔操作。"),
    "readToken": (
        "**写快照票据**（大多可省略）。显式传入时服务端会校验「你读到的模型快照是否仍一致」，"
        "不一致返回 `STALE_READ` ⇒ 重新读取或省略该票据后重试。\n"
        "默认省略即可；只有需要「读到什么就一定按什么写」的严格场景才传。"),
    "pageNum/pageSize/offset/limit": (
        "分页参数。不同工具口径不同：\n"
        "- `query_*` 系用 `pageNum`(从 1 开始) + `pageSize`(默认 20，上限 100)\n"
        "- `get_model_*` 系用 `offset` + `limit`，**沿返回的 `nextOffset` 读到 null 才算取全**\n"
        "只取第一页就下结论是最常见的错误（长列表/多值属性尤其如此）。"),
}

RECIPES = """# 典型任务配方（调用链）

> 每条都是「按顺序调用、中间结果直接喂给下一步」。跳过前置步骤是失败率最高的写法。

## 配方 1 · 拿到工程坐标（workspace 三元组）
所有工具都要它，而它必须**逐级查**：

```
query_projects(keyword="广汽方法论建模1022")
   → 取 items[0].projectId
query_branches(projectId=<上一步>)
   → 取 items[].branchId（master 是 isMaster=true 那条，别默认选它）
query_model_versions(projectId=..., branchId=..., includeDraft=true)
   → 取 items[].versionId  +  status（DRAFT 才可写，RELEASED 只读）
```

⚠️ 三个常见错误：
1. **只用 keyword 查不到时的兜底**：先不带 keyword 分页翻，或改用 `creatorName`/`ownerName`；
2. **把版本名称当 versionId**（`name="V4"` ≠ `versionId="2115373400643141632"`）；
3. **选了 RELEASED 版本去写** ⇒ 报只读。写操作前必须确认 `status=DRAFT`。

## 配方 2 · 浏览包结构（找目标包）
```
query_data_tree(version={projectId,branchId,versionId})            # 根下一层
query_data_tree(version=..., parentId=<dataId>)                    # 逐层展开
```
- 省略 `parentId` 返回根下一层；用返回的 `dataId` 继续展开。
- `keyword` / `type` **只过滤当前层**，不能跨层查找 —— 想按名字找全局元素用配方 3。
- 输出给用户时**必须带 dataId**：后续所有按包操作都靠它，名字不是唯一标识。

## 配方 3 · 按名称找元素（不去遍历树）
```
search_model_elements(workspace=三元组, keyword="通信子系统")
   → 一次跨全部层级，同时模糊匹配中文 name 与英文 shortName
   → 取 items[].elementId / dataId
```
这是"按名字找东西"的**首选**工具；`query_data_tree` 是"已知父节点、看直接子级"。

## 配方 4 · 导出现有模型源码
```
export_sysml_v2(action="PREVIEW", version=三元组, scope="PROJECT")
   → 把范围摘要展示给用户并确认
export_sysml_v2(action="EXPORT",  version=三元组, scope="PROJECT", confirmToken=<上一步票据>)
```
- `scope=DATA` / `VIEW` 时必须传**已确认的 dataId**。
- 全工程导出可能超长，按 `scope=DATA` 逐包导出更稳。

## 配方 5 · 修改模型（两条路，优先走②）
**① 强类型命令（推荐）**
```
preview_model_changes(workspace=三元组, command=[...])       # 纯只读预览
apply_model_changes(workspace=三元组, commands=[...], requestId=uuid)
get_model_operation(workspace=三元组, requestId=<上一步>)     # 查执行状态
undo_model_operation(workspace=三元组, operationId=...)       # 需要时撤销
```
- 每条命令独立事务、独立 requestId；**失败即停，不承诺跨命令原子回滚**。
- `get_model_operation` 里 `operationId` 与 `requestId` **必须且只能给一个**。
- `UNKNOWN` 状态**禁止自动重做**（可能已生效），先查证。

**② 专用工具（语义更明确，逐字段可控）**
- 改名/改说明 → `update_model_elements`
- 可见性/方向/多重性/需求文本 → `update_model_properties`
- 自定义属性 → `get_model_custom_properties` → `update_model_custom_properties`
- 表达式 → `set_model_expression` → `get_model_expression` 回读
- 建元素 → `get_modeling_capabilities(parentId=父)` 拿 `creationRef` → `create_model_elements`

## 配方 6 · 建视图并放元素
```
get_model_diagram_guide(viewKind="IBD", workspace=三元组)      # 先读该图种的用途/元素/验收规则
create_model_view(workspace=..., name="通信子系统IBD", ownerId=<元素>, viewKind="IBD", requestId=uuid)
get_model_view_guidance(viewId=..., workspace=三元组)          # 拿本视图真实可用的 DRAWING 候选
create_model_view_element(viewId=..., creationRef=..., requestId=uuid)   # 或 present_model_elements
layout_model_view(viewId=..., requestId=uuid)                  # 最后统一布局
```
- `layout_model_view` **不会补造缺失连线** —— 先把关系建好并呈现，再布局。
- 一次 `present_model_elements` 最多 50 个元素。

## 配方 7 · 校验（语法 vs 语义，两个不同工具）
```
validate_sysml_text(workspace=三元组, text=<源码>)             # 纯语法检查，不需要工程里的东西
get_model_validation_rules(workspace=三元组)                   # 先读规则 ID
validate_model(workspace=三元组, packageIds=[...], ruleIds=[...], requestId=uuid)  # 工程内语义检查
```
- **语法通过 ≠ 语义成立**：`validate_sysml_text` 只查语法；
  `validate_model` 才查工程内规则（返回违规定位，`detailsComplete=false` 表示还有更多）。

## 配方 8 · 把源码写进智源
```
# 先确保 workspace 指向 DRAFT 版本！
query_model_versions(..., includeDraft=true)   → 确认 status=DRAFT
import_sysml_text(workspace=三元组,
                  mode="ADD_PACKAGE",          # 新增包；覆盖已有包用 OVERWRITE_PACKAGE
                  packageName="通信子系统IBD",
                  targetPackageId=<父包 dataId>,
                  text=<完整 SysML v2 源码>,
                  requestId=uuid)
query_data_tree(version=三元组, parentId=<父包>)   # 回读确认新包真的出现了
```
- `OVERWRITE_PACKAGE`：保留目标包本身与 dataId，**删除其子树后重写** —— 破坏性，先 `preview`/告知用户。
- 回执有告警 = 部分语义未应用，必须如实汇报，别当成"成功"。

## 配方 9 · 删除（高风险，先查影响）
```
get_model_delete_impact(workspace=三元组, dataId=<目标>)
   → 展示级联影响、取得用户确认、拿 DELETE_ELEMENTS 用途票据
delete_model_elements(workspace=三元组, dataId=..., requestId=uuid, approvalId=<票据>)
```
级联数量在执行前可能未知 —— 如实说明"可能连带删除 N 个"，不要假装确定。

## 配方 10 · 改自定义属性
```
get_model_custom_properties(elementId=..., workspace=三元组, includeOptions=true)   # 拿 propertyId/propertyProjectId 与类型
update_model_custom_properties(elementId=..., workspace=三元组, changes=[...], requestId=uuid)
```
`changes` 里 `SET` 必须带强类型 `value`；要清空用 `UNSET` 且**不带 value**。
"""


def _load_meta():
    from database import db_conn
    with db_conn() as conn:
        row = conn.execute(
            "SELECT name, tool_meta FROM mcp_servers WHERE name='zhiyuan-dev-mcp' "
            "ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        raise SystemExit("未找到 mcp_servers.name='zhiyuan-dev-mcp'（先到「能力中心·工具与MCP」发现一次）")
    meta = json.loads(row["tool_meta"] or "[]")
    out = {}
    for x in meta:
        if isinstance(x, dict) and x.get("name"):
            out[x["name"]] = x
    return row["name"], out


def _fmt_props(sch: dict, common: set) -> str:
    """把 inputSchema 渲染成参数表。`common` 里的参数只在速查章讲一次，这里标注指引。"""
    props = sch.get("properties") or {}
    req = list(sch.get("required") or [])
    if not props:
        return "（无参数）"
    lines = ["| 参数 | 必填 | 类型 | 说明 |", "|---|---|---|---|"]
    for nm in sorted(props, key=lambda k: (k not in req, k)):
        p = props[nm] or {}
        t = p.get("type") or "?"
        if p.get("enum"):
            t += "：" + " / ".join(str(e) for e in p["enum"])
        if t == "object" and (p.get("properties") or {}):
            sub = ", ".join(sorted(p["properties"].keys()))
            t = "object{%s}" % sub
        if t == "array" and isinstance(p.get("items"), dict):
            it = p["items"].get("type") or "?"
            t = "array<%s>" % it
        d = (p.get("description") or "").replace("\n", " ").replace("|", "／").strip()
        if nm in common:
            d = (d + " " if d else "") + "（公共参数，见「参数速查」）"
        lines.append("| `%s` | %s | %s | %s |" % (
            nm, "✓" if nm in req else "", t, d[:220] or "—"))
    extra = sch.get("additionalProperties")
    if extra is False:
        lines.append("")
        lines.append("> `additionalProperties: false` ⇒ **传表外参数会被直接拒绝**"
                     "（历史事故：给 `query_data_tree` 传 `value` 被拒）。")
    return "\n".join(lines)


def build(meta: dict, srv: str):
    # ── 覆盖断言：71 个必须全部归类、且分类里不能有元数据里没有的名字 ──
    classified = [t for g in GROUPS.values() for t in g]
    dup = {t for t in classified if classified.count(t) > 1}
    missing = sorted(set(meta) - set(classified))
    ghost = sorted(set(classified) - set(meta))
    problems = []
    if dup:
        problems.append("重复归类：%s" % sorted(dup))
    if missing:
        problems.append("未归类（元数据里有、GROUPS 里没有）：%s" % missing)
    if ghost:
        problems.append("幽灵项（GROUPS 里有、元数据里没有）：%s" % ghost)
    if problems:
        raise SystemExit("分类覆盖断言失败 —— 文档会静默漏工具，先修 GROUPS：\n  " + "\n  ".join(problems))

    # ── 工具目录 ──
    all_props = set()
    for x in meta.values():
        all_props |= set((x.get("inputSchema") or {}).get("properties") or {})
    common = {k for k in ("workspace", "requestId", "readToken") if k in all_props}

    ws_n = sum(1 for x in meta.values()
               if "workspace" in ((x.get("inputSchema") or {}).get("properties") or {}))

    parts = ["# 智源 MCP 工具目录（%d 个）" % len(meta), "",
             "> 本文件由 `tools/gen_zhiyuan_mcp_skill.py` 从 `mcp_servers.tool_meta` "
             "**自动生成**，请勿手改。", ""]
    for gname, tools in GROUPS.items():
        parts.append("## " + gname)
        parts.append("")
        for t in tools:
            x = meta[t]
            sch = x.get("inputSchema") or {}
            desc = (x.get("description") or "").replace("\n", " ").strip()
            parts.append("### `%s`" % t)
            parts.append("")
            # 首句当"一句话作用"，其余保留（服务端描述里后半句常是关键约束）
            parts.append(desc or "（服务端未提供描述）")
            parts.append("")
            req = sch.get("required") or []
            parts.append("**必填**：%s" % ("、".join("`%s`" % r for r in req) if req else "无"))
            parts.append("")
            parts.append(_fmt_props(sch, common))
            parts.append("")
        parts.append("")
    catalog = "\n".join(parts)

    # ── 参数速查 ──
    sp = ["# 公共参数速查", "",
          "这两个/三个参数在几十个工具里重复出现，语义固定，单独讲一次。", ""]
    for k, v in COMMON_PARAMS.items():
        sp.append("## `%s`" % k)
        sp.append("")
        sp.append(v)
        sp.append("")
    # 统计哪些工具真的用到（可核对）
    sp.append("## 覆盖统计")
    sp.append("")
    sp.append("| 公共参数 | 出现在多少个工具 |")
    sp.append("|---|---|")
    for k in ("workspace", "requestId", "readToken"):
        n = sum(1 for x in meta.values()
                if k in ((x.get("inputSchema") or {}).get("properties") or {}))
        sp.append("| `%s` | %d / %d |" % (k, n, len(meta)))
    sp.append("")
    sp.append("## 其余高频参数（同义不同名，容易写错）")
    sp.append("")
    sp.append("| 含义 | 出现形式 | 备注 |")
    sp.append("|---|---|---|")
    sp.append("| 元素标识 | `dataId` / `elementId` / `sourceId` / `targetId` / `ownerId` | "
              "`dataId` 是树节点（可含包/视图），`elementId` 偏语义元素；不确定时用 "
              "`search_model_elements` 返回的两个都给 |")
    sp.append("| 分页 | `pageNum`+`pageSize` / `offset`+`limit` | `query_*` 用前者，`get_model_*` 用后者 |")
    sp.append("| 幂等 | `requestId` | 写入类必填；不同操作**不要复用** |")
    sp.append("| 票据 | `readToken` / `approvalId` / `confirmToken` | 分别对应 读快照 / 删除用途 / 导出确认 |")
    sp.append("")
    params = "\n".join(sp)

    # ── skill.md ──
    n_req = sum(1 for x in meta.values() if (x.get("inputSchema") or {}).get("required"))
    skill = f"""---
name: 智源MCP工具手册
description: zhiyuan-dev-mcp 的 {len(meta)} 个 MCP 工具目录：有哪些工具、必填什么、怎么调用（含按任务索引与检索脚本）
triggers: [智源, zhiyuan, zhiyuan-dev-mcp, 工程坐标, projectId, 包树, 数据树, 模型版本, 创建包, 建视图, 导入源码, 导出源码, 语义校验]
category: 平台集成
author: 平台内置
version: v1.0
---
# 智源 MCP 工具手册

**职责边界（先看这一句）**：
- 本手册 = **查表**：有哪些工具、各自干什么、怎么传参。
- 技能「智源平台管理」= **流程**：端到端该按什么顺序做、每步的坑、写操作三段链。
- 两者同时命中时：**照「智源平台管理」的顺序**走，用**本手册**查每个工具的准确参数。

当前 server 共 **{len(meta)} 个工具**（{n_req} 个有必填参数，{ws_n} 个需要 `workspace`），
名字以 `query_*` / `get_model_*` / `create_model_*` / `import_sysml_text` / `export_sysml_v2` 开头。

## 三条必须记住的参数事实

1. **`workspace` 是 `{{projectId, branchId, versionId}}`，三个字段都是 id 字符串、都必填。**
   `versionId` **不能**用版本名称代替（`V4` 是 name，不是 id）。
2. **同一台 server 里 `version` 与 `workspace` 两种字段名并存**（查包树用 `version`，
   元素/视图操作用 `workspace`），值都是同一个三元组对象。传错名字会被
   `additionalProperties:false` 直接拒。
3. **`additionalProperties: false`** ⇒ schema 表外的参数会被直接拒绝。
   （历史事故：给 `query_data_tree` 传 `value`，回执 `INVALID_ARGUMENTS`。）

写入类另有两个固定参数：`requestId`（幂等键，必填、不同操作不要复用）、
`readToken`（写快照票据，**默认省略**；报 `STALE_READ` 就重新读或省略它）。

## 按任务找工具（只看名字，参数查 references）

| 我要做的事 | 工具（按调用顺序） |
|---|---|
| 找工程 / 分支 / 版本 | `query_projects` → `query_branches` → `query_model_versions` |
| 看包结构树 | `query_data_tree`（省略 parentId 看根，用 dataId 逐层展开）|
| 按名字找元素（不用遍历）| `search_model_elements`（一次跨全部层级）|
| 读元素详情 / 长文本 / 关联 | `get_model_elements` / `get_model_text` / `get_model_associations` |
| 导出 SysML v2 源码 | `export_sysml_v2`（PREVIEW → 确认 → EXPORT）|
| 改模型（通用）| `preview_model_changes` → `apply_model_changes` → `get_model_operation` |
| 改名 / 说明 / 属性 | `update_model_elements` / `update_model_properties` |
| 新建包 / 元素 | `get_modeling_capabilities`（拿 creationRef）→ `create_model_elements` |
| 移动 / 复制 / 删除 | `move_model_elements` / `clone_model_elements` / `get_model_delete_impact`→`delete_model_elements` |
| 建关系（语义连线）| `create_model_relationships`（先 `get_model_relationships` 确认合法端点类型）|
| 建视图 / 放图元 | `get_model_diagram_guide` → `create_model_view` → `get_model_view_guidance` → `create_model_view_element` / `present_model_elements` → `layout_model_view` |
| 改样式 / 坐标 | `update_view_styles` / `update_view_presentations` |
| 状态机 trigger/guard/effect | `get_state_machine_semantics` → `configure_state_transition` / `configure_state_actions` |
| 语法检查 | `validate_sysml_text` |
| 工程内语义校验 | `get_model_validation_rules` → `validate_model` |
| 写入源码到平台 | `import_sysml_text`（**先确认版本是 DRAFT**）|
| 加评论 / 文档 | `annotate_model_elements` / `update_model_annotation` |
| 平台自动化 / 变体 | `get_model_automation_options` → `run_modeling_automation`；`manage_model_variants` |

## 怎么查具体参数（渐进披露，不要一次读完）

- **只想知道某个工具怎么调** → `scripts/lookup.py <说法>`，例如
  `lookup.py 导入源码`。命中确定映射会直接给出契约；没命中会标注
  `【模糊】…请核对`（**模糊结果不要当确定答案用**）。
- **要完整参数表** → `references/工具目录.md`（{len(meta)} 个工具的完整 schema，
  按 11 个功能组编排）。
- **公共参数怎么填** → `references/参数速查.md`（workspace / requestId / readToken / 分页）。
- **端到端怎么串** → `references/任务配方.md`（10 条配方，含每步的坑）。
- **元数据过期**（服务端加了工具） → 跑 `scripts/dump_tools.py` 重新生成。

## 四种最常见的失败

| 回执 | 真实原因 | 怎么改 |
|---|---|---|
| `INVALID_ARGUMENTS` + `fields[]` | 参数名/取值不在 schema 内 | 读 `fields[].expected` 按清单改，别猜 |
| 只读 / 无法写入 | `versionId` 指向 `RELEASED` | `includeDraft=true` 找 `DRAFT` 版本 |
| `STALE_READ` | 显式传了过期的 `readToken` | 重新读一次，或**省略** readToken |
| 查不到元素但用户说存在 | 用了 `query_data_tree(keyword=)`（只过滤当前层）| 改用 `search_model_elements` |
"""
    return skill, catalog, params


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只校验分类覆盖与 schema，不写文件")
    a = ap.parse_args()

    srv, meta = _load_meta()
    skill, catalog, params = build(meta, srv)
    print("已读取 %s：%d 个工具，分类 %d 组（覆盖断言通过）"
          % (srv, len(meta), len(GROUPS)))

    n_req = sum(1 for x in meta.values() if (x.get("inputSchema") or {}).get("required"))
    print("  有必填参数的工具：%d / %d" % (n_req, len(meta)))
    ws = sum(1 for x in meta.values()
             if "workspace" in ((x.get("inputSchema") or {}).get("properties") or {}))
    print("  需要 workspace 的：%d / %d" % (ws, len(meta)))

    if a.check:
        print("[--check] 未写文件")
        return 0

    os.makedirs(os.path.join(PKG, "references"), exist_ok=True)
    os.makedirs(os.path.join(PKG, "scripts"), exist_ok=True)
    files = {
        "skill.md": skill,
        os.path.join("references", "工具目录.md"): catalog,
        os.path.join("references", "参数速查.md"): params,
        os.path.join("references", "任务配方.md"): RECIPES,
    }
    for rel, content in files.items():
        p = os.path.join(PKG, rel)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
        print("  写入 %-34s %6d 字节" % (rel, len(content.encode("utf-8"))))

    # 这两个脚本随包发出（运维在包里就能重跑，不必找仓库根）
    me = os.path.abspath(__file__)
    shutil.copyfile(me, os.path.join(PKG, "scripts", "dump_tools.py"))
    print("  写入 %-34s （本生成器副本）" % "scripts/dump_tools.py")
    # 检索器：源头放 tools/（入 git），包内是产物。
    # 为什么不能只放在包里：`static/skill_packages/` 在 .gitignore（既定约定，
    # git 里 0 个文件）⇒ 手写在包里的脚本会随机器丢失，而"包可从代码重建"是本仓
    # 对这类运行时资产的隐含要求。源头入 git + 产物不入 = 可重建。
    lk = os.path.join(ROOT, "tools", "zhiyuan_mcp_lookup.py")
    if os.path.exists(lk):
        shutil.copyfile(lk, os.path.join(PKG, "scripts", "lookup.py"))
        print("  写入 %-34s （源自 tools/zhiyuan_mcp_lookup.py）" % "scripts/lookup.py")
    else:
        print("  [warn] 缺 tools/zhiyuan_mcp_lookup.py ⇒ 包内 lookup.py 未更新")
    print("\n技能包目录：%s" % os.path.relpath(PKG, ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
