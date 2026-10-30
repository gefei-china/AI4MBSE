# -*- coding: utf-8 -*-
"""verify_skill_tool_names — 门禁：**技能正文里点名的工具，必须真实存在于 MCP 工具清单**。

## 为什么需要这道门禁（会话 9629 实测，「工程能力太差」的真正来源之一）

用户报「创建包的工具也有，工程树查询工具也有，为啥还是失败」。查下来有一层原因很直白：
**`智源平台管理` 技能正文里写的工具名，在真实 MCP 里一个都不存在。**

    正文写                       真实工具
    zhiyuan_project_list    →    query_projects
    zhiyuan_project_tree    →    query_data_tree
    zhiyuan_sysmlv2_gen     →    export_sysml_v2
    zhiyuan_sysmlv2_check   →    validate_sysml_text
    zhiyuan_sysmlv2_import  →    import_sysml_text
    mbse_pull_ingest        →    （本仓工具，非该 MCP）

模型按"权威 playbook"去调 `zhiyuan_project_tree` ⇒ 工具不存在 ⇒ 只能自己猜参数
⇒ 连续 `INVALID_ARGUMENTS`。**这不是模型笨，是文档在骗它。**

同类陷阱还有字段命名不一致（`query_data_tree` 用 `version`，其余用 `workspace`），
本门禁也一并校验"正文若提到 version/workspace，必须把两种写法都讲清"。

## 锁三件事
① 正文里反引号包裹的 `snake_case` 标识符，若形似工具名则必须存在于真实 MCP 工具集
   （白名单是"参数名/字段名/键名"，不是工具）；
② 正文不得残留已知的**历史错误工具名前缀**（`zhiyuan_project_*` / `zhiyuan_sysmlv2_*`）；
③ 正文必须交代 `version` 与 `workspace` 两种分组字段并存（否则模型必踩）。
"""
import json
import os
import re
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from agent.mcp_tool_meta import load_tool_meta  # noqa: E402

SKILL = "智源平台管理"
MCP = "zhiyuan-dev-mcp"

#: 非工具名的标识符（参数/字段/键/概念）—— 出现在正文里不算错
NOT_TOOLS = {
    "project_id", "branch_id", "version_id", "element_id", "owner_id", "data_id",
    "include_draft", "next_offset", "size_bytes", "creation_ref", "creation_owner_id",
    "request_id", "page_size", "page_num", "parent_id", "read_token", "include_read_token",
    "next_after_id", "scan_limited", "confirmation_token", "after_id", "max_bytes",
    "view_id", "short_name", "semantic_status", "notification_status", "operation_id",
    "affected_ids", "element_type", "mime_type", "mime_type", "result_type",
}

FAILS = []


def ok(name, cond, detail=""):
    print("  [%s] %s%s" % ("OK  " if cond else "FAIL", name, ("  ← " + detail) if detail else ""))
    if not cond:
        FAILS.append(name)


def main():
    c = sqlite3.connect("file:%s?mode=ro" % os.path.join(ROOT, "mbse.db"), uri=True)
    c.row_factory = sqlite3.Row
    srow = c.execute("SELECT id, version, content FROM skills WHERE name=?", (SKILL,)).fetchone()
    mrow = c.execute("SELECT tool_meta FROM mcp_servers WHERE name=?", (MCP,)).fetchone()
    c.close()
    if not srow:
        print("!! 找不到技能 %s" % SKILL)
        sys.exit(1)
    if not mrow or not mrow["tool_meta"]:
        print("!! 找不到 %s 的 tool_meta（先跑一次重新发现）" % MCP)
        sys.exit(1)
    tools = set(load_tool_meta({"tool_meta": mrow["tool_meta"]}).keys())
    txt = srow["content"] or ""
    print("技能 %s（%s，%d 字符）｜真实工具 %d 个" % (SKILL, srow["version"], len(txt), len(tools)))

    print("\n=== ① 正文点名的工具必须真实存在 ===")
    # 反引号内的 snake_case 标识符
    cands = set(re.findall(r"`([a-z][a-z0-9]*(?:_[a-z0-9]+)+)`", txt))
    # 形似工具名：出现在"工具清单"语境（以常见动词开头 或 命中已知工具名）
    suspect = [x for x in cands
               if x not in tools and x not in NOT_TOOLS
               and re.match(r"^(query|get|set|create|update|delete|remove|move|apply|preview|"
                            r"undo|search|present|layout|generate|annotate|configure|manage|"
                            r"validate|import|export|place|clone|run|zhiyuan|mbse)_", x)]
    ok("没有'形似工具名却不存在'的引用", not suspect,
       "可疑=%s" % sorted(suspect) if suspect else
       "正文引用真实工具 %d 个" % len([x for x in cands if x in tools]))

    print("\n=== ② 不得残留历史错误工具名前缀 ===")
    for bad in ("zhiyuan_project_list", "zhiyuan_project_tree",
                "zhiyuan_sysmlv2_gen", "zhiyuan_sysmlv2_check", "zhiyuan_sysmlv2_import"):
        ok("已清除 %s" % bad, bad not in txt)

    print("\n=== ③ 必须交代 version / workspace 两种分组字段并存 ===")
    ok("正文提到 version 字段", "`version`" in txt or "version=" in txt)
    ok("正文提到 workspace 字段", "`workspace`" in txt or "workspace=" in txt)
    ok("明确指出二者不能互换（同一 server 内并存）",
       ("不能互换" in txt) or ("两种写法并存" in txt) or ("字段名不同" in txt))

    print("\n=== ④ 必须交代「默认只返已发布 / 草稿要 includeDraft」 ===")
    ok("提到 includeDraft", "includeDraft" in txt)
    ok("说明默认只返已发布版本", ("默认只返回" in txt) or ("默认只返" in txt))

    print("\n=== ⑤ 必须交代「已发布版本只读」 ===")
    ok("说明 RELEASED 不可写 / 只能草稿写",
       ("只有草稿" in txt) or ("RELEASED 版本上写" in txt) or ("只有草稿（DRAFT）版本可写" in txt))

    print("\n=== ⑥ 变异自证：塞回一个历史错误名 ⇒ 第②项必须变红 ===")
    mutated = txt.replace("`query_data_tree`", "`zhiyuan_project_tree`", 1)
    ok("变异点命中（正文确实含 query_data_tree）", mutated != txt)
    ok("变异版会被 ② 抓到", "zhiyuan_project_tree" in mutated)

    print("\n" + "=" * 70)
    if FAILS:
        print("❌ FAIL %d 项：%s" % (len(FAILS), "; ".join(FAILS)))
        sys.exit(1)
    print("✅ 通过 —— 技能点名的工具真实存在 / 无历史错误名 / 字段与草稿通道讲清（含变异自证）")


if __name__ == "__main__":
    main()
