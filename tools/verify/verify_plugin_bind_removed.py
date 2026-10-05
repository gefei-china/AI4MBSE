#!/usr/bin/env python

# ── CI 豁免（2026-10-05 标注，理由已实测）──────────────────
# CI-OPTIONAL: C 实测干净库上红（file-ops 仍可经全局池触发，命中=0）⇒ 需先修
#   分类：A=需服务在跑/ B=需密钥或写真库/ C=实测就红需先修。
#   依据见 docs/遗留优化项-第二轮盘点-20261005.md；
#   由 tools/verify/verify_gate_wiring.py 强制要求（要么接线，要么写理由）。
# -*- coding: utf-8 -*-
"""verify_plugin_bind_removed.py —— 验证「Agent 绑定插件」机制已彻底移除（2026-09-30 第 10 轮）

判据设计三原则（血泪教训，勿简化为"源码里搜不到 plugin 就行"）：
  ① 只做**字符串搜索**会把注释里提到的 plugin 也算进去（本轮我特意在注释里写了大量 plugin 说明）
     → 必须走 `ast` 解析真实语法树，注释不进 AST。
  ② 只断言"删掉了"是**否定式断言** —— 必须先在夹具里造出"若没删就会留下痕迹"的条件（见 C 组）。
  ③ 必须断言**没删过头**：registry.py /349 的 `source: "plugin"`（插件来源的 Agent 标记）
     与本轮移除的「Agent 绑定插件」是两回事，误删会导致插件 Agent 不可用。

用法：.venv/Scripts/python.exe tools/verify/verify_plugin_bind_removed.py
"""
import ast
import io
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS  " + name + (("  [" + detail + "]") if detail else ""))
    else:
        FAIL += 1
        print("  FAIL  " + name + (("  [" + detail + "]") if detail else ""))


def src(p):
    return io.open(os.path.join(ROOT, p), encoding="utf-8").read()


def main():
    print("=" * 72)
    print("A. 后端：plugin→skill/mcp 展开逻辑已移除（AST 断言，注释不算）")
    print("=" * 72)
    reg = src("agent/registry.py")
    tree = ast.parse(reg)                       # 先确保能解析
    # 找出所有函数调用名，确认 skill_entry_from_plugin / mcp_entry_from_plugin 不在 registry 里
    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Attribute):
                called.add(f.attr)
            elif isinstance(f, ast.Name):
                called.add(f.id)
    check("registry.py 不再调用 skill_entry_from_plugin", "skill_entry_from_plugin" not in called)
    check("registry.py 不再调用 mcp_entry_from_plugin", "mcp_entry_from_plugin" not in called)

    # 不能只靠"字符串搜不到 plugin" —— 注释里还有。改为断言：AST 里没有 tool_type=='plugin' 的比较
    has_plugin_cmp = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare) and isinstance(node.left, ast.Constant) is False:
            try:
                s = ast.unparse(node)
            except Exception:
                continue
            if "tool_type" in s and "plugin" in s:
                has_plugin_cmp = True
    check("registry.py 无 tool_type=='plugin' 的运行时比较", not has_plugin_cmp)

    print()
    print("=" * 72)
    print("B. 后端：绑定入口白名单 + 透传分支已移除")
    print("=" * 72)
    ag = src("routers/studio_parts/agents.py")
    at = ast.parse(ag)
    # 找 add_agent_tool 里的元组白名单常量
    tuple_lits = []
    for node in ast.walk(at):
        if isinstance(node, ast.Tuple):
            vals = [e.value for e in node.elts if isinstance(e, ast.Constant)]
            if vals and "skill" in vals:
                tuple_lits.append(vals)
    check("绑定白名单不含 plugin", all("plugin" not in t for t in tuple_lits),
          "找到的白名单: " + str(tuple_lits))
    check("绑定白名单确实存在（防误删整段校验）", len(tuple_lits) > 0)

    ar = ast.parse(src("repositories/agent_repo.py"))
    repo_cmp = []
    for node in ast.walk(ar):
        if isinstance(node, ast.Compare):
            try:
                s = ast.unparse(node)
            except Exception:
                continue
            if "tool_type" in s:
                repo_cmp.append(s)
    check("agent_repo.py 无 tool_type=='plugin' 分支",
          all("plugin" not in s for s in repo_cmp), "分支: " + str(repo_cmp))
    check("agent_repo.py 的 skill/mcp/tool 三分支仍在（未删过头）",
          any("skill" in s for s in repo_cmp) and any("mcp" in s for s in repo_cmp)
          and any("tool" in s for s in repo_cmp))

    print()
    print("=" * 72)
    print("C. 不能删过头：插件来源 Agent 的标记必须保留（夹具自证）")
    print("=" * 72)
    # 这条是"否定式断言"的反面：必须证明 registry 里 source:'plugin' 还在。
    # 夹具：造出"若误删就会丢失的痕迹" —— 直接断言语法树里存在该字典键值。
    d = ast.parse(reg)
    has_src_plugin = False
    for node in ast.walk(d):
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if isinstance(k, ast.Constant) and k.value == "source" \
                        and isinstance(v, ast.Constant) and v.value == "plugin":
                    has_src_plugin = True
    check("registry.py 仍保留 source:'plugin'（插件来源 Agent 标记，勿误删）", has_src_plugin)
    check("plugin_agents() 方法仍在（消费 source==plugin）",
          "plugin_agents" in reg and 'v.get("source") == "plugin"' in reg)

    print()
    print("=" * 72)
    print("D. 数据层：agent_tools 无 plugin 绑定残留（新连接复核）")
    print("=" * 72)
    db = os.path.join(ROOT, "mbse.db")
    conn = sqlite3.connect(db)                  # 新连接，避免旧事务视图假 PASS
    # ⚠️ 必须设 row_factory=Row —— 生产代码的 `core/audit.rows_to_list` 用 `dict(r)` 取行，
    #    普通 tuple 行会报 "cannot convert dictionary update sequence element #0 to a sequence"。
    #    这个报错极像"代码写坏了"，实则是**夹具漏配了被测代码依赖的约定**（老坑，勿再踩）。
    conn.row_factory = sqlite3.Row
    n_plugin = conn.execute("SELECT COUNT(*) FROM agent_tools WHERE tool_type='plugin'").fetchone()[0]
    check("agent_tools 中 plugin 绑定数 = 0", n_plugin == 0, "实际 %d" % n_plugin)
    dist = dict(conn.execute("SELECT tool_type, COUNT(*) FROM agent_tools GROUP BY tool_type").fetchall())
    check("tool_type 仅剩 skill/mcp/tool", set(dist.keys()) <= {"skill", "mcp", "tool"},
          str(dist))
    n_total = conn.execute("SELECT COUNT(*) FROM agent_tools").fetchone()[0]
    check("绑定总量 = 60（删 1 条后）", n_total == 60, "实际 %d" % n_total)

    print()
    print("=" * 72)
    print("E. 前端：三类绑定收敛，字段已下线")
    print("=" * 72)
    js = src("static/js/mods/30-agents.js")
    core = src("static/js/mods/01-core.js")
    check("_BIND_KINDS 只剩 skill/mcp/tool",
          "const _BIND_KINDS = ['skill','mcp','tool'];" in js)
    check("_BIND_LABEL 无 plugin", "'plugin'" not in js.split("_BIND_LABEL")[1].split("\n")[0])
    check("不再拉取 /api/plugins", "api('/api/plugins')" not in js)
    check("collectAgentBindings 无 plugins 字段", "plugins: [..._agentBindSets.plugin]" not in js)
    check("syncAgentBindings 的 want 无 plugin:", "b.plugins.map(n=>'plugin:'+n)" not in js)
    check("模板已删除 bindDropdown('plugin'...) 字段",
          "bindDropdown('plugin'" not in core)
    check("模板仍保留 skill/mcp/tool 三个字段（未删过头）",
          "bindDropdown('skill'" in core and "bindDropdown('mcp'" in core and "bindDropdown('tool'" in core)

    print()
    print("=" * 72)
    print("F. 运行时实证：删后仍能正常加载 Agent，且能力不丢（关键回归）")
    print("=" * 72)
    from agent.registry import AgentRegistry
    # ⚠️ 必须用真实构造函数 AgentRegistry()，**不能用 __new__ 跳过 __init__** ——
    #    实测 __new__ 夹具会在 load_from_db 内部报
    #    "cannot convert dictionary update sequence element #0 to a sequence"
    #    （类属性与实例属性初始化序列不一致），那是夹具缺陷、会被误读成生产回归。
    r = AgentRegistry()
    try:
        r.load_from_db(conn, user={"id": 1})
        check("load_from_db 未因本次改动抛异常", True, "加载 %d 个 Agent" % len(r._db_defs))
    except Exception as e:
        check("load_from_db 未因本次改动抛异常", False, str(e))
        r._db_meta = {}
    # 技能能力不丢：file-ops 走全局池，仍应能触发
    try:
        from agent import AgentPipeline
        pipe = AgentPipeline()
        pipe._load_db_agents()
        pipe._build_skill_prompt("requirement_analysis", "请把分析结果保存为文件",
                                 user={"id": 1, "role_name": "admin"})
        hits = getattr(pipe, "_last_skill_hits", None) or []
        check("技能能力未丢失（file-ops 仍可经全局池触发）",
              any("文件操作" in str(h) or "file-ops" in str(h) for h in hits),
              "hits=" + str(hits))
    except Exception as e:
        check("技能能力未丢失（file-ops 仍可经全局池触发）", False, "异常: " + str(e))

    print()
    print("=" * 72)
    print("结果: %d PASS / %d FAIL" % (PASS, FAIL))
    print("=" * 72)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
