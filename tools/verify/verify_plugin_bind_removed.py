#!/usr/bin/env python

# ── 已接进 CI（2026-10-05 第二轮第 2 项修复后）──────────
# 修复要点：原硬编码 mbse.db（设 MBSE_DB_PATH 也无效）+ 把「干净库无样本」判成失败；
#   现读配置库路径，无样本一律 SKIP（同 verify_skill_injection 的口径）。
# 双环境实测：生产库 + 全新干净库 均 rc=0。

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
    # ⚠️ 2026-10-05 修订：原先硬编码 `os.path.join(ROOT, "mbse.db")`
    # ⇒ 设了 MBSE_DB_PATH 指向干净库时它**仍在读生产库**（本地绿 / CI 红，同 verify_skill_injection 那个坑）。
    # 改读配置里的 DB_PATH。
    sys.path.insert(0, ROOT)
    from core.config import DB_PATH
    db = DB_PATH
    conn = sqlite3.connect(db)                  # 新连接，避免旧事务视图假 PASS
    # ⚠️ 必须设 row_factory=Row —— 生产代码的 `core/audit.rows_to_list` 用 `dict(r)` 取行，
    #    普通 tuple 行会报 "cannot convert dictionary update sequence element #0 to a sequence"。
    #    这个报错极像"代码写坏了"，实则是**夹具漏配了被测代码依赖的约定**（老坑，勿再踩）。
    conn.row_factory = sqlite3.Row

    def skip(name, why):
        """无样本 → SKIP（记一笔但不判 FAIL）。

        ⚠️ 为什么不能判 FAIL：本组断言的是"存量数据的形状"。
        干净库里**根本没有** agent_tools 行，"没有行"与"行里有 plugin 残留"是两回事；
        把前者判成失败 ⇒ CI 每次必红，而红因不是代码坏了（同 verify_skill_injection 的修法）。
        """
        print("  SKIP  " + name + "  [" + why + "]")

    # 表本身都可能不存在（全新空库 / 未 init_db）⇒ 与"表在但没数据"同样按无样本处理，
    # 不能让它崩（崩溃会把后面所有断言一起吞掉，看起来像"全红"）。
    _t = conn.execute("SELECT COUNT(*) FROM sqlite_master "
                      "WHERE type='table' AND name='agent_tools'").fetchone()[0]
    n_total = (conn.execute("SELECT COUNT(*) FROM agent_tools").fetchone()[0]
               if _t else 0)
    if n_total == 0:
        skip("agent_tools 中 plugin 绑定数 = 0", "本库无 agent_tools 数据（干净库），无法验证存量形状")
        skip("tool_type 仅剩 skill/mcp/tool", "同上")
        skip("绑定总量 = 60（删 1 条后）", "同上：60 是**生产库**的历史口径，不是不变式")
    else:
        n_plugin = conn.execute("SELECT COUNT(*) FROM agent_tools WHERE tool_type='plugin'").fetchone()[0]
        check("agent_tools 中 plugin 绑定数 = 0", n_plugin == 0, "实际 %d" % n_plugin)
        dist = dict(conn.execute("SELECT tool_type, COUNT(*) FROM agent_tools GROUP BY tool_type").fetchall())
        check("tool_type 仅剩 skill/mcp/tool", set(dist.keys()) <= {"skill", "mcp", "tool"},
              str(dist))
        # 60 是"删掉那 1 条 plugin 绑定后"的生产库历史数字，随用户增删会变
        # ⇒ 只把它当**观测值**记录，不做硬断言（否则谁来绑一条就红一次，是噪音不是门禁）
        print("  INFO  绑定总量 = %d（生产库历史口径 60，仅供参考，不做硬断言）" % n_total)

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
    _has_agents = conn.execute("SELECT COUNT(*) FROM sqlite_master "
                               "WHERE type='table' AND name='agents'").fetchone()[0]
    if not _has_agents:
        skip("load_from_db 未因本次改动抛异常", "本库连 agents 表都没有（未 init_db），非本次改动所致")
        r._db_meta = {}
    else:
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
        pool = pipe._global_skill_pool(user={"id": 1, "role_name": "admin"}) or []
        if not pool:
            # ⚠️ 全局技能池来自 skills 表；干净库里一张都没有 ⇒ 无从验证"触发"。
            # 这同样是"没样本"，不是"能力没了" ⇒ SKIP（判 FAIL 会让 CI 恒红且红因不对）。
            skip("技能能力未丢失（file-ops 仍可经全局池触发）",
                 "本库 skills 表为空，无全局技能池可触发")
        else:
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
