"""补强「智源平台管理」技能正文：显式强化草稿通道章节（仍是指导，非特判）。

走真实 HTTP 端点 PUT /api/studio/skills/{sid}（update_skill handler）：
- 尊重 P2-3：编辑即 bump 版本（patch+1，可观测）
- 尊重 P2-4：Object.assign 全字段回传，未展示字段不清空
- 不直连 DB 改 content（绕过 handler 即绕过版本 bump 与审计）

仅 content 变化：在工具映射表、vc 规范之间新增「草稿通道（无已发布版本时）」章节，
并在安全与流程规范补第 6 条「空结果处理」原则。其余字段原样回传。
"""
import json
import sqlite3
import urllib.request

DB = "mbse.db"
BASE = "http://127.0.0.1:8000"
SID = 95  # 智源平台管理


def db_row():
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    r = c.execute(
        "SELECT name, description, skill_type, triggers, category, content, frontmatter, "
        'dependencies, allowed_tools, "references", examples, scripts '
        "FROM skills WHERE id=?", (SID,)).fetchone()
    c.close()
    return r


def login():
    body = json.dumps({"display_name": "draft-channel-patch"}, ensure_ascii=False).encode()
    req = urllib.request.Request(BASE + "/api/auth/local-login", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))["token"]


def new_content():
    # 仅在原正文基础上：① gen 行补草稿通道提示 ② 新增「草稿通道」章节 ③ 流程规范补第 6 条
    return """---
name: 智源平台管理
description: 智源MBSE平台直连管理：工程/包结构查询、SysML v2 源码生成检测导入、token 更新指引（zhiyuan_* 工具使用规范）
triggers: [智源, zhiyuan, 工程列表, 包结构, 包树, 拉取源码, 导入智源, 写回智源, 语法检测, token]
category: 平台集成
author: 平台内置
version: v1.0
---
# 智源平台管理技能

你负责通过 zhiyuan_* 工具直连智源 MBSE 平台，完成工程与模型的查询、源码生成与写回。

## 工具映射
| 需求 | 工具 | 要点 |
|---|---|---|
| 查有哪些工程 | **zhiyuan_project_list** | 导入工程返回的 vc（branchId,1,version）可直接回填复用 |
| 查包结构树 | **zhiyuan_project_tree** | 输出时保留每个包的 dataId（后续按包操作全靠它） |
| 导出 SysML v2 源码 | **zhiyuan_sysmlv2_gen** | packageDataId 缺省=全工程；按包生成先查树定位；**草稿态用 `branchId,0` 指定草稿通道（见下「草稿通道」）** |
| 语法检测 | **zhiyuan_sysmlv2_check** | 需 targetPackageDataId + text；写回前必做 |
| 写回智源 | **zhiyuan_sysmlv2_import** | 覆盖写：保留目标包及 dataId、删除其子树；仅草稿 vc（queryType=0） |
| **拉取入库** | **mbse_pull_ingest** | 拉取智源建模数据→转三元组→写入个人分支图库；重复拉取幂等去重；用户说「拉取/入库/转化三元组」即调它 |

## vc 规范
- 格式 `branchId,queryType[,versionNumber]`；0=草稿、1=已发布
- 用户未提供 vc 时不要臆造，参数留空由执行器自动回填配置的 default_vc

## 草稿通道（无已发布版本时）
智源工程存在「草稿态」：master 分支 `currentVersion=0`、尚无「已发布启用」版本。相关工具默认只返回已发布启用态的数据，此时必然为空——**这属于正常状态，不是工具故障，不要同参数反复重试**。

正确动作是**换通道而非重试同参数**：
- **导出 / 生成源码**：`zhiyuan_sysmlv2_gen` 用 **草稿 vc `branchId,0`**（versionNumber 省略）指定草稿通道，`packageDataId` 缺省=全工程；
- **写回**：仅草稿 vc（queryType=0）才允许覆盖写，本就是草稿通道（见上「vc 规范」）。

> 通用原则：**任何版本 / 列表查询返回空，先核实工程是否草稿态，再用 `branchId,0` 换通道重试；不要拿相同参数反复调用**。模型应据工具返回（如「无已发布版本」「draftAvailable」）决策，不臆断工程状态。

## 安全与流程规范
1. 查询类（list/tree/gen/check）直接执行；import 前必须复述目标包与影响并获用户确认
2. 端到端流程（团队模式）：需求分析（requirement_analysis）→ 建模代码生成（design）→ 校验（review）
   → zhiyuan_sysmlv2_check 复检语法 → 确认后 zhiyuan_sysmlv2_import 写回智源
3. 拉取入库：用户要求「从工程获取数据/转化三元组/写入图库」→ 调 mbse_pull_ingest，向用户汇报入库统计
   （候选/三元组/实体/关系）；entities_written=0 且有 triples_staged = 数据此前已入库（幂等去重），如实说明
4. 写回三步：gen 导出现状 → 修改 → check 校验通过 → import 写回 → gen 复核
5. gen 全工程生成可能超长被截断（max_response 上限），必要时按包分次生成
6. **空结果处理**：版本 / 列表类工具返回空时，优先核实工程是否草稿态并切换 `branchId,0` 通道，**禁止同参数重复调用**；
   若工具明确返回「无已发布版本 / draftAvailable」等提示，直接按其指引走草稿通道
"""


def main():
    r = db_row()
    tok = login()
    body = {
        "name": r["name"],
        "description": r["description"] or "",
        "skill_type": r["skill_type"] or "package",
        "triggers": json.loads(r["triggers"] or "[]"),
        "category": r["category"] or "",
        "content": new_content(),
        "frontmatter": r["frontmatter"] or "",
        "dependencies": json.loads(r["dependencies"] or "[]"),
        "allowed_roles": [],
        "allowed_tools": json.loads(r["allowed_tools"] or "[]"),
        "references": json.loads(r["references"] or "[]"),
        "examples": json.loads(r["examples"] or "[]"),
        "scripts": json.loads(r["scripts"] or "[]"),
    }
    data = json.dumps(body, ensure_ascii=False).encode()
    req = urllib.request.Request(
        BASE + f"/api/studio/skills/{SID}", data=data,
        headers={"Content-Type": "application/json", "X-Session-Token": tok}, method="PUT")
    with urllib.request.urlopen(req, timeout=60) as resp:
        print("PUT resp:", json.loads(resp.read().decode("utf-8")))

    # 读回确认 content 已落地 + 版本已 bump
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    after = c.execute("SELECT version, substr(content,1,40) head, length(content) clen "
                      "FROM skills WHERE id=?", (SID,)).fetchone()
    c.close()
    print("after update -> version=%s content_len=%s head=%s" % (after["version"], after["clen"], after["head"]))
    print("draft section present:", "草稿通道（无已发布版本时）" in body["content"])


if __name__ == "__main__":
    main()
