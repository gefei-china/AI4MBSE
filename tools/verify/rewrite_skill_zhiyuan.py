"""重写「智源平台管理」技能正文：工具名对齐真实 MCP + 写操作三段链（2026-10-27）。

走真实 HTTP PUT /api/studio/skills/95（update_skill handler），尊重 P2-3 版本 bump、P2-4 全字段回传。

## 为什么重写（会话 9629 实测）
正文里写的工具名（`zhiyuan_project_list` / `zhiyuan_project_tree` / `zhiyuan_sysmlv2_gen` /
`zhiyuan_sysmlv2_check` / `zhiyuan_sysmlv2_import`）**在真实 MCP 里一个都不存在**
（实际是 `query_projects` / `query_data_tree` / `export_sysml_v2` / `create_model_elements` …）。
模型照技能做 → 找不到工具 → 只能自己猜参数 → 连续 INVALID_ARGUMENTS。

并且实测跑通了「在通信子系统用例包下新增测试包」的完整链路（dataId
2114968212157431808，shortName bao150，落在草稿版本、已发布版本不变），
把它按**可执行顺序 + 真实字段名**写进正文。

用法：python tools/verify/rewrite_skill_zhiyuan.py [--dry-run]
"""
import json
import sqlite3
import sys
import urllib.request

DB = "mbse.db"
BASE = "http://127.0.0.1:8000"
SID = 95

CONTENT = """---
name: 智源平台管理
description: 智源MBSE平台直连管理：工程/分支/版本查询、包树浏览、元素检索、新增/修改/删除元素、视图创建、SysML v2 源码生成与导入、token 更新指引（zhiyuan-dev-mcp 工具使用规范）
triggers: [智源, zhiyuan, 工程列表, 包结构, 包树, 新增包, 创建包, 新增元素, 删除元素, 修改元素, 拉取源码, 导出源码, 导入智源, 写回智源, 语法检测, 视图, token]
category: 平台集成
author: 平台内置
version: v1.0
---
# 智源平台管理技能

你通过 **zhiyuan-dev-mcp** 直连智源 MBSE 平台。工具名以服务端实际注册为准（下表是**真实工具名**）。

## 核心坐标系（一切请求都从这三项开始）
| 步骤 | 工具 | 关键参数 | 拿到什么 |
|---|---|---|---|
| 找工程 | `query_projects` | `keyword` | `projectId` |
| 找分支 | `query_branches` | `projectId` | `branchId` |
| 找版本 | `query_model_versions` | `projectId`, `branchId`, **`includeDraft: true`** | `versionId` + `status`(RELEASED/DRAFT) |

> ⚠️ **`query_model_versions` 默认只返回「已发布启用」版本，草稿版本必须显式 `includeDraft: true` 才会出现。**
> 这是"查不到版本 / 写不进去"的头号原因：看不到草稿版本 → 拿已发布版本去写 → `PERMISSION_DENIED`。

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

## 写操作三段链（新增包 / 新增元素）——顺序不能跳
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

## 写操作的版本铁律
- **只有草稿（DRAFT）版本可写**。在 RELEASED 版本上写一律返回
  `PERMISSION_DENIED`（`recovery.instructions` 会提示"改用有权限且可编辑的目标"）。
- 没有草稿版本时，**如实告知用户"当前只有已发布版本，需先在平台创建草稿版本"**，
  不要反复用已发布版本重试，也不要声称"已写入"。
- 写入后再次 `query_model_versions(includeDraft=true)` 复核该草稿版本仍在（未被发布覆盖）。

## 源码导出（只读）
- `export_sysml_v2` 是**异步分页协议**：`PREVIEW` → `EXPORT`（带 confirmationToken）→ `STATUS`（轮询直到 COMPLETED）→ `READ`（按 `nextOffset` 分段读，`eof=true` 为止）。
- 大文件必须分段读并拼接，**单段不是全文**；拼齐后才算取到。
- 结果里 `sizeBytes` 是权威字节数，可用它校验拼接是否完整。

## 报错自愈（照结构化提示改，不要同参数重试）
| 返回 | 含义 | 应对 |
|---|---|---|
| `INVALID_ARGUMENTS` + `fields[].expected` | 字段名/类型不对 | 按 `expected` 列出的字段重写参数（常见：`version`↔`workspace`、对象 vs 字符串） |
| `PERMISSION_DENIED` + `recovery.nextTools` | 无权限/版本只读 | 读 `recovery.instructions`；通常是「没用草稿版本」或「Key 缺 scope」 |
| 列表/版本查询返回空 | 可能是只看已发布 | 加 `includeDraft: true` 或先确认父节点是否正确 |
| `scanLimited: true` | 搜索被扫描上限截断 | 用 `afterId=nextAfterId` 继续翻页 |

## 安全与流程规范
1. 查询类直接执行；**任何写操作前必须复述「父容器 + 新增/修改内容 + 目标草稿版本」并获用户确认**
2. 删除类先 `get_model_delete_impact` 看影响面，再经用户确认
3. 写操作完成后必须回读校验，回复中给出**真实 dataId / 名称 / 所在版本**
4. 不得虚构"已完成/已写入/已归档"：没有工具回执就不能声称完成
5. token 过期（401）：平台系统配置页更新，或 `PUT /api/system/config/static`
   body `{"updates": {"zhiyuan.token": "<新token>"}}`
"""


def db_row():
    c = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
    c.row_factory = sqlite3.Row
    r = c.execute(
        'SELECT name, description, skill_type, triggers, category, frontmatter, '
        'dependencies, allowed_tools, "references", examples, scripts FROM skills WHERE id=?',
        (SID,)).fetchone()
    c.close()
    return r


def login():
    body = json.dumps({"display_name": "skill-rewrite"}, ensure_ascii=False).encode()
    req = urllib.request.Request(BASE + "/api/auth/local-login", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))["token"]


def main():
    dry = "--dry-run" in sys.argv
    r = db_row()
    body = {
        "name": r["name"], "description": r["description"] or "",
        "skill_type": r["skill_type"] or "package",
        "triggers": json.loads(r["triggers"] or "[]"),
        "category": r["category"] or "", "content": CONTENT,
        "frontmatter": r["frontmatter"] or "",
        "dependencies": json.loads(r["dependencies"] or "[]"),
        "allowed_roles": [],
        "allowed_tools": json.loads(r["allowed_tools"] or "[]"),
        "references": json.loads(r["references"] or "[]"),
        "examples": json.loads(r["examples"] or "[]"),
        "scripts": json.loads(r["scripts"] or "[]"),
    }
    print("新正文长度 = %d（旧 1942）" % len(CONTENT))
    if dry:
        print("干跑：不提交")
        return
    tok = login()
    req = urllib.request.Request(
        BASE + "/api/studio/skills/%d" % SID,
        data=json.dumps(body, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json", "X-Session-Token": tok}, method="PUT")
    with urllib.request.urlopen(req, timeout=60) as resp:
        print("PUT:", json.loads(resp.read().decode("utf-8")))
    c = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
    c.row_factory = sqlite3.Row
    a = c.execute("SELECT version, length(content) n FROM skills WHERE id=?", (SID,)).fetchone()
    c.close()
    print("after -> version=%s content_len=%s" % (a["version"], a["n"]))


if __name__ == "__main__":
    main()
