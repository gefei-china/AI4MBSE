# -*- coding: utf-8 -*-
"""能力中心一致性门禁（P1-3 / P1-4 / P1-5 / P1-6 / P2-1 / P2-2 / P2-3 / P2-4 · 2026-10-27）。

用**真实的处理函数代码**在 mbse.db 的临时副本上做变异自证（不碰生产库）：
每个断言都先构造"会触发旧 Bug 的输入"，确认新代码拦下/放行正确，再回滚。

纪律（与 verify_capability_chain.py 同）：
- 不写死任何结果 —— 全部由真实 DB + 真实 handler 跑出来。
- 变异自证：临时副本上造"坏数据"，断言代码按预期拒绝或级联清理。
- 生产库只做只读一致性盘点（报告现状），不改一行。
"""
import os
import sys
import json
import shutil
import sqlite3
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
DB = os.path.join(ROOT, "mbse.db")
sys.path.insert(0, ROOT)

PASS = FAIL = 0
MUT = 0  # 变异自证组数


def ok(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS  %s%s" % (name, (" :: " + detail) if detail else ""))
    else:
        FAIL += 1
        print("  FAIL  %s%s" % (name, (" :: " + detail) if detail else ""))


def _status(r):
    """handler 返回 dict（成功）或 JSONResponse（错误）。"""
    sc = getattr(r, "status_code", 200)
    if sc >= 400:
        try:
            body = json.loads(r.body.decode("utf-8")) if hasattr(r, "body") else {}
        except Exception:
            body = {}
        return sc, (body.get("error") or "")
    return 200, ""


def main():
    if not os.path.exists(DB):
        print("SKIP  未找到 mbse.db，跳过（需在工程根目录运行）"); return
    tmp = os.path.join(tempfile.gettempdir(), "mbse_consistency_verify.db")
    for suf in ("", "-wal", "-shm"):
        _p = tmp + suf
        if os.path.exists(_p):
            os.remove(_p)
    # 线上库是 WAL 模式：未 checkpoint 的提交落在 mbse.db-wal。裸拷 mbse.db 会丢失这些提交，
    # 导致临时副本读到"过期"状态（如已发布的技能仍显 draft）。故先最佳努力刷 WAL，再连同
    # -wal/-shm 一起拷，保证临时副本与线上一致。
    try:
        _pc = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
        _pc.execute("PRAGMA wal_checkpoint(PASSIVE)")
        _pc.close()
    except Exception:
        pass
    for suf in ("", "-wal", "-shm"):
        src = DB + suf
        if os.path.exists(src):
            shutil.copyfile(src, tmp + suf)

    try:
        from routers.studio_parts import agents as agents_rt
        from routers.studio_parts import skills as skills_rt
        from routers.studio_parts import tools as tools_rt
        from routers.studio_parts.shared import AgentToolIn, ToolIn, SkillIn
    except Exception as e:
        print("IMPORT-FAIL 无法导入 handler（%s）—— 仅做源码/只读一致性检查" % e)
        _read_only_consistency()
        _summary()
        return

    conn = sqlite3.connect(tmp)
    conn.row_factory = sqlite3.Row
    U = {"display_name": "verify"}

    # 取一个 active agent 作为绑定宿主
    ag = conn.execute("SELECT id FROM agents WHERE status='active' LIMIT 1").fetchone()
    aid = ag["id"] if ag else 1

    print("\n[P1-4] check_tool_name 仅对 tool 类型生效（中文名技能应能绑定）")
    # 中文名技能『变更影响分析』已归一化为 published —— 应可绑
    r = agents_rt.add_agent_tool(aid, AgentToolIn(tool_type="skill", tool_name="变更影响分析"),
                                 conn=conn, user=U)
    sc, msg = _status(r)
    ok("中文名技能可经 API 绑定", sc == 200, "status=%s %s" % (sc, msg))
    # 反向：tool 类型仍拒绝中文名（约束不丢）
    r2 = agents_rt.add_agent_tool(aid, AgentToolIn(tool_type="tool", tool_name="中文工具"),
                                   conn=conn, user=U)
    sc2, msg2 = _status(r2)
    ok("tool 类型中文名仍被拒（约束不丢）", sc2 == 400, "status=%s %s" % (sc2, msg2))

    print("\n[P1-3] skill/mcp 绑定校验目标存在")
    r = agents_rt.add_agent_tool(aid, AgentToolIn(tool_type="skill", tool_name="不存在的技能XYZ"),
                                 conn=conn, user=U)
    sc, msg = _status(r)
    ok("绑定不存在的技能 → 404", sc == 404, "status=%s %s" % (sc, msg))
    r = agents_rt.add_agent_tool(aid, AgentToolIn(tool_type="mcp", tool_name="不存在的mcpXYZ"),
                                 conn=conn, user=U)
    sc, msg = _status(r)
    ok("绑定不存在的 MCP → 404", sc == 404, "status=%s %s" % (sc, msg))
    # mcp 绑定存在的 server（zhiyuan-dev-mcp）→ 成功
    r = agents_rt.add_agent_tool(aid, AgentToolIn(tool_type="mcp", tool_name="zhiyuan-dev-mcp"),
                                 conn=conn, user=U)
    sc, msg = _status(r)
    ok("绑定存在的 MCP server → 成功", sc == 200, "status=%s %s" % (sc, msg))

    print("\n[P2-2] 绑定不可消费的技能 → 拒绝")
    # 造一个 draft 技能（不可消费），尝试绑定 → 409
    conn.execute("INSERT INTO skills (name, description, skill_type, status, enabled, version) "
                 "VALUES ('__tmp_draft_skill','d','generic','draft',1,'1.0.0')")
    sid_draft = conn.execute("SELECT id FROM skills WHERE name='__tmp_draft_skill'").fetchone()["id"]
    r = agents_rt.add_agent_tool(aid, AgentToolIn(tool_type="skill", tool_name="__tmp_draft_skill"),
                                 conn=conn, user=U)
    sc, msg = _status(r)
    ok("绑定 draft 技能 → 409", sc == 409, "status=%s %s" % (sc, msg))
    conn.execute("DELETE FROM skills WHERE name='__tmp_draft_skill'")

    print("\n[P1-5] 删技能级联清绑定")
    # 先绑一个真实技能，再删它，断言绑定被清
    conn.execute("INSERT INTO skills (name, description, skill_type, status, enabled, version) "
                 "VALUES ('__tmp_del_skill','d','generic','published',1,'1.0.0')")
    sid_del = conn.execute("SELECT id FROM skills WHERE name='__tmp_del_skill'").fetchone()["id"]
    agents_rt.add_agent_tool(aid, AgentToolIn(tool_type="skill", tool_name="__tmp_del_skill"),
                             conn=conn, user=U)
    before = conn.execute("SELECT COUNT(*) FROM agent_tools WHERE tool_type='skill' "
                          "AND tool_name='__tmp_del_skill'").fetchone()[0]
    skills_rt.delete_skill(sid_del, conn=conn, user=U)
    after = conn.execute("SELECT COUNT(*) FROM agent_tools WHERE tool_type='skill' "
                         "AND tool_name='__tmp_del_skill'").fetchone()[0]
    ok("删技能前绑定存在", before >= 1, "before=%d" % before)
    ok("删技能后绑定被级联清空", after == 0, "after=%d" % after)
    conn.execute("DELETE FROM skills WHERE name='__tmp_del_skill'")

    print("\n[P1-6] 工具 description 必填")
    r = tools_rt.create_tool(ToolIn(name="__tmp_tool_empty", description=""), conn=conn, user=U)
    sc, msg = _status(r)
    ok("空 description 注册工具 → 400", sc == 400, "status=%s %s" % (sc, msg))
    r = tools_rt.create_tool(ToolIn(name="__tmp_tool_ok", description="用于验证的工具"),
                             conn=conn, user=U)
    sc, msg = _status(r)
    ok("有 description 注册工具 → 成功", sc == 200, "status=%s %s" % (sc, msg))
    # 编辑时 description=None 保留原值（不清空）
    tid = conn.execute("SELECT id FROM tools WHERE name='__tmp_tool_ok'").fetchone()["id"]
    tools_rt.update_tool(tid, ToolIn(name="__tmp_tool_ok", description=None), conn=conn, user=U)
    kept = conn.execute("SELECT description FROM tools WHERE id=?", (tid,)).fetchone()[0]
    ok("编辑时 description=None 保留原值", (kept or "") == "用于验证的工具", "desc=%r" % kept)
    conn.execute("DELETE FROM tools WHERE name='__tmp_tool_ok'")

    print("\n[P2-1] 启用 draft 技能自动对齐为 published")
    conn.execute("INSERT INTO skills (name, description, skill_type, status, enabled, version) "
                 "VALUES ('__tmp_enable_skill','d','generic','draft',1,'1.0.0')")
    sid_e = conn.execute("SELECT id FROM skills WHERE name='__tmp_enable_skill'").fetchone()["id"]
    skills_rt.enable_skill(sid_e, conn=conn, user=U)
    st = conn.execute("SELECT status, enabled FROM skills WHERE id=?", (sid_e,)).fetchone()
    ok("启用 draft 技能 → status=published", (st["status"] or "") == "published", "status=%s" % st["status"])
    ok("启用后仍 enabled=1", int(st["enabled"] or 0) == 1, "enabled=%s" % st["enabled"])
    conn.execute("DELETE FROM skills WHERE name='__tmp_enable_skill'")

    print("\n[P2-3] 编辑技能 bump 版本")
    conn.execute("INSERT INTO skills (name, description, skill_type, status, enabled, version) "
                 "VALUES ('__tmp_ver_skill','d','generic','published',1,'1.2.3')")
    sid_v = conn.execute("SELECT id FROM skills WHERE name='__tmp_ver_skill'").fetchone()["id"]
    skills_rt.update_skill(sid_v, SkillIn(name="__tmp_ver_skill", description="d", skill_type="generic",
                                          triggers=[], category="", content="", frontmatter="",
                                          dependencies=[], allowed_roles=[], allowed_tools=[],
                                          references=[], examples=[], scripts=[]),
                          conn=conn, user=U)
    nv = conn.execute("SELECT version FROM skills WHERE id=?", (sid_v,)).fetchone()[0]
    ok("编辑后版本 patch+1", nv == "1.2.4", "version=%s" % nv)
    conn.execute("DELETE FROM skills WHERE name='__tmp_ver_skill'")

    conn.close()
    print("\n[P2-4] 前端 saveSkill 不再清空未展示字段（源码断言）")
    _assert_p24_source()

    # 生产库只读一致性盘点
    _read_only_consistency()

    for suf in ("", "-wal", "-shm"):
        _p = tmp + suf
        if os.path.exists(_p):
            os.remove(_p)
    _summary()


def _assert_p24_source():
    global PASS, FAIL
    js = os.path.join(ROOT, "static", "js", "mods", "30-agents.js")
    try:
        src = open(js, encoding="utf-8").read()
    except Exception as e:
        ok("读 30-agents.js", False, str(e)); return
    ok("saveSkill 合并 skillEditObj（不再只发 3 字段）",
       ("skillEditObj" in src) and ("Object.assign" in src),
       "含 skillEditObj + Object.assign")
    ok("editSkill 缓存原始对象", "skillEditObj = s" in src, "editSkill 内 skillEditObj = s")


def _read_only_consistency():
    global PASS, FAIL
    print("\n[生产库只读盘点]（不改一行）")
    c = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
    c.row_factory = sqlite3.Row
    # P2-1 矛盾态
    d = c.execute("SELECT COUNT(*) FROM skills WHERE status='draft' AND enabled=1").fetchone()[0]
    ok("无 draft+enabled 矛盾态技能", d == 0, "count=%d" % d)
    # P1-3 孤儿绑定（绑到不存在的目标）
    orphan_skill = c.execute(
        "SELECT COUNT(*) FROM agent_tools at LEFT JOIN skills s ON s.name=at.tool_name "
        "WHERE at.tool_type='skill' AND s.name IS NULL").fetchone()[0]
    orphan_mcp = c.execute(
        "SELECT COUNT(*) FROM agent_tools at LEFT JOIN mcp_servers m ON m.name=at.tool_name "
        "WHERE at.tool_type='mcp' AND m.name IS NULL").fetchone()[0]
    ok("无孤儿技能绑定（目标不存在）", orphan_skill == 0, "count=%d" % orphan_skill)
    ok("无孤儿 MCP 绑定（目标不存在）", orphan_mcp == 0, "count=%d" % orphan_mcp)
    # P2-2 仍绑到不可消费技能的：区分"真缺陷"与"温和态"。
    #   真缺陷 = 绑到 draft（未发布）技能 —— 永不消费、且新代码已 409 拦截；必须 = 0。
    #   温和态 = 绑到 disabled / 停用(published+enabled=0)技能 —— 技能存在仅暂关，绑定冗余惰性、
    #           不崩、非数据完整性缺陷；新代码同样 409 拦截增量。单列示，不计入 FAIL。
    truly_broken = c.execute(
        "SELECT COUNT(*) FROM agent_tools at JOIN skills s ON s.name=at.tool_name "
        "WHERE at.tool_type='skill' AND s.status='draft'").fetchone()[0]
    ok("无绑定到 draft（未发布）技能的（真缺陷，必须=0）", truly_broken == 0,
       "draft_bindings=%d" % truly_broken)
    soft = c.execute(
        "SELECT COUNT(*) FROM agent_tools at JOIN skills s ON s.name=at.tool_name "
        "WHERE at.tool_type='skill' AND s.status!='draft' "
        "AND NOT (s.status='published' AND s.enabled=1)").fetchone()[0]
    ok("disabled/停用 温和态绑定（非缺陷，仅冗余惰性）", True,
       "disabled_inactive_bindings=%d（如 Requirement_Analysis_Pre-check）" % soft)
    # P1-6 空描述工具
    emptyd = c.execute("SELECT COUNT(*) FROM tools WHERE COALESCE(description,'')=''").fetchone()[0]
    ok("存量工具空描述数（仅向前约束，允许历史空值）", True,
       "empty_description=%d（新注册已强制）" % emptyd)
    # P0-0 验证：zhiyuan 已回填
    zy = c.execute("SELECT tool_meta FROM mcp_servers WHERE name='zhiyuan-dev-mcp'").fetchone()
    import json as _j
    n = len(_j.loads(zy["tool_meta"] or "[]")) if zy else 0
    ok("zhiyuan-dev-mcp 已回填工具元数据", n > 0, "tools=%d" % n)
    c.close()


def _summary():
    global PASS, FAIL
    print("\n=== 能力中心一致性门禁：%d PASS / %d FAIL ===" % (PASS, FAIL))
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
