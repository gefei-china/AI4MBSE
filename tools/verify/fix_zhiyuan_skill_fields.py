# -*- coding: utf-8 -*-
"""修正「智源平台管理」的 **DB 字段**（triggers 列 / allowed_tools 列）——2026-10-27。

## 为什么（会话 9639 实测，最后一个卡点）

正文已经改写对了，但**运行时读的是 DB 列，不是正文 frontmatter**：

1. `triggers` 列还是旧的 `['智源','zhiyuan','工程列表','包结构','包树',...]`
   ⇒ 用户说「在"通信子系统用例"包中**新增一个**"测试包C"包」时**一个都不命中**
   ⇒ 技能没注入 ⇒ 模型不知道"写操作要用草稿版本" ⇒ 拿已发布版本去写
   ⇒ `PERMISSION_DENIED`，只能瞎猜"Perhaps branchId 0"。
2. `allowed_tools` 列全是**不存在的工具名**（`zhiyuan_sysmlv2_gen` / `zhiyuan_project_tree` …）
   ⇒ 6 个声明里真实存在的 **0 个**。

## 改法

- `triggers` 用**短动词**（子串匹配，长词如"新增包"永远匹配不到"新增一个"）；
- `allowed_tools` 换成**真实工具名**（本文件从 tool_meta 现查校验，不手写猜测）。

用法：python tools/verify/fix_zhiyuan_skill_fields.py [--dry-run]
"""
import json
import sqlite3
import sys
import urllib.request

DB = "mbse.db"
BASE = "http://127.0.0.1:8000"
SID = 95

#: 短动词 + 领域词（**子串匹配**，宁短勿长："新增包"匹配不到"新增一个包"）
TRIGGERS = [
    "智源", "zhiyuan", "工程列表", "包结构", "包树", "包",
    "新增", "创建", "新建", "添加", "删除", "移除", "修改", "更新", "改名", "移动",
    "元素", "子包", "建模", "写回", "导入", "导出", "拉取", "源码",
    "视图", "语法检测", "校验", "token",
]

#: 真实工具名（下面会用 tool_meta 校验；不存在的会被剔除并告警）
ALLOWED = [
    "query_projects", "query_branches", "query_model_versions", "query_data_tree",
    "search_model_elements", "get_modeling_capabilities", "get_model_elements",
    "create_model_elements", "update_model_elements", "delete_model_elements",
    "get_model_delete_impact", "create_model_relationships", "get_model_relationships",
    "export_sysml_v2", "validate_sysml_text", "import_sysml_text",
    "get_modeling_context", "move_model_elements",
]


def real_tools():
    c = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
    c.row_factory = sqlite3.Row
    row = c.execute("SELECT tool_meta FROM mcp_servers WHERE name='zhiyuan-dev-mcp'").fetchone()
    c.close()
    sys.path.insert(0, ".")
    from agent.mcp_tool_meta import load_tool_meta
    return set(load_tool_meta({"tool_meta": row["tool_meta"] or ""}).keys()) if row else set()


def login():
    body = json.dumps({"display_name": "skill-fields-fix"}, ensure_ascii=False).encode()
    req = urllib.request.Request(BASE + "/api/auth/local-login", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))["token"]


def main():
    dry = "--dry-run" in sys.argv
    tools = real_tools()
    allowed = [t for t in ALLOWED if t in tools]
    dropped = [t for t in ALLOWED if t not in tools]
    print("真实工具 %d 个" % len(tools))
    print("allowed_tools 保留 %d 个；剔除不存在 %d 个：%s"
          % (len(allowed), len(dropped), dropped or "无"))

    c = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
    c.row_factory = sqlite3.Row
    r = c.execute('SELECT name,description,skill_type,category,content,frontmatter,'
                  'dependencies,"references",examples,scripts FROM skills WHERE id=?',
                  (SID,)).fetchone()
    c.close()
    body = {
        "name": r["name"], "description": r["description"] or "",
        "skill_type": r["skill_type"] or "package",
        "triggers": TRIGGERS, "category": r["category"] or "",
        "content": r["content"] or "", "frontmatter": r["frontmatter"] or "",
        "dependencies": json.loads(r["dependencies"] or "[]"),
        "allowed_roles": [], "allowed_tools": allowed,
        "references": json.loads(r["references"] or "[]"),
        "examples": json.loads(r["examples"] or "[]"),
        "scripts": json.loads(r["scripts"] or "[]"),
    }
    print("\n新 triggers（%d 个）：%s" % (len(TRIGGERS), TRIGGERS))
    # 自检：用户原始请求能命中吗
    q = "在“通信子系统用例”包中新增一个“测试包C”包"
    hits = [t for t in TRIGGERS if t in q]
    print("对原始请求 %r 的命中：%s" % (q, hits))
    if not hits:
        print("!! 仍不命中，中止")
        sys.exit(1)

    if dry:
        print("干跑：不提交")
        return
    tok = login()
    req = urllib.request.Request(
        BASE + "/api/studio/skills/%d" % SID,
        data=json.dumps(body, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json", "X-Session-Token": tok}, method="PUT")
    with urllib.request.urlopen(req, timeout=60) as resp:
        print("\nPUT:", json.loads(resp.read().decode("utf-8")))
    c = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
    c.row_factory = sqlite3.Row
    a = c.execute("SELECT version,triggers,allowed_tools FROM skills WHERE id=?", (SID,)).fetchone()
    c.close()
    print("after -> version=%s" % a["version"])
    print("  triggers     =", json.loads(a["triggers"]))
    print("  allowed_tools=", json.loads(a["allowed_tools"]))


if __name__ == "__main__":
    main()
