# -*- coding: utf-8 -*-
"""P1-3 常驻门禁：8 个视图生成 Agent 已从插件库移除，且不会被重新发布回来。

为什么需要单独一条（D1 那条门禁守不住）：
  D1 的门禁只查 `agents` 表与资产快照。实测 D1 之后 `plugins` 里 id=46~53
  仍是 `scope='public' status='published'`，`capability_registry` 里 8 条
  `status=active lifecycle=published` ⇒ **插件市场仍能搜到、仍算可用能力**。
  两条门禁守的是同一次退役的两个不同资产面，缺一个就会出现「库里删了、
  市场里还卖着」的不一致。

判据分四组：
  R 真库最终态（removed + disabled/deprecated + agents 无残留）
  C 承接能力（9 个 Skill published 且挂在 view_expansion 上）
  M 市场可见性（按 plugin_system.store.queries 的真实 where 子句复算）
  X 变异自证（判据有牙齿，全零 IO 内存库）
无真库时降级并打印 DEGRADED，不假绿。

用法：
  ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_p1_3_view_agent_plugins.py
"""
import json
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
DB = os.environ.get("MBSE_DB_PATH") or os.path.join(ROOT, "mbse.db")

PIDS = ["com.zhiyuan.legacy.agent.agent-165", "com.zhiyuan.legacy.agent.agent-166",
        "com.zhiyuan.legacy.agent.ibd", "com.zhiyuan.legacy.agent.agent-168",
        "com.zhiyuan.legacy.agent.agent-169", "com.zhiyuan.legacy.agent.agent-170",
        "com.zhiyuan.legacy.agent.agent-171", "com.zhiyuan.legacy.agent.agent-172"]
AGENT_IDS = list(range(165, 173))
SKILL_PREFIX = "sysml_view_generation_"
VIEW_AGENT = 637

PASS, FAIL, DEGRADED = [], [], []


def rec(tag, cond, detail=""):
    (PASS if cond else FAIL).append(tag)
    print("  %s %-4s %s" % ("PASS" if cond else "FAIL", tag, detail))


def ro():
    c = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
    c.row_factory = sqlite3.Row
    return c


# ── 纯判据（门禁与变异共用）──────────────────────────────────────────────
def bad_final_state(conn):
    """插件库退役是否彻底。返回违规列表。"""
    bad = []
    ph = ",".join("?" * len(PIDS))
    rows = conn.execute("SELECT plugin_id,status FROM plugins WHERE plugin_id IN (%s)"
                        % ph, PIDS).fetchall()
    if len(rows) != len(PIDS):
        bad.append("plugins 应有 %d 行，实得 %d" % (len(PIDS), len(rows)))
    w = [(r[0], r[1]) for r in rows if r[1] != "removed"]
    if w:
        bad.append("plugins 非 removed: %s" % w[:4])
    caps = ["plugin:" + p for p in PIDS]
    crows = conn.execute("SELECT capability_id,status,lifecycle FROM capability_registry "
                         "WHERE capability_id IN (%s)" % ",".join("?" * len(caps)),
                         caps).fetchall()
    if len(crows) != len(caps):
        bad.append("capability_registry 应有 %d 行，实得 %d" % (len(caps), len(crows)))
    wc = [(r[0], r[1], r[2]) for r in crows
          if r[1] != "disabled" or r[2] != "deprecated"]
    if wc:
        bad.append("capability_registry 非 disabled/deprecated: %s" % wc[:4])
    iph = ",".join("?" * len(AGENT_IDS))
    n = conn.execute("SELECT COUNT(*) FROM agents WHERE id IN (%s)" % iph,
                     AGENT_IDS).fetchone()[0]
    if n:
        bad.append("agents 表残留 %d 行（165~172）" % n)
    return bad


def bad_capability(conn):
    """承接方必须完好：9 个 Skill published 且挂在 view_expansion 上。"""
    bad = []
    n = conn.execute("SELECT COUNT(*) FROM skills WHERE name LIKE ? AND status='published'",
                     (SKILL_PREFIX + "%",)).fetchone()[0]
    if n != 9:
        bad.append("published 视图 Skill 应为 9，实得 %d" % n)
    n2 = conn.execute("SELECT COUNT(*) FROM agent_tools WHERE agent_id=? AND tool_type='skill' "
                      "AND tool_name LIKE ? AND enabled=1",
                      (VIEW_AGENT, SKILL_PREFIX + "%")).fetchone()[0]
    if n2 != 9:
        bad.append("view_expansion(%d) 挂载应为 9，实得 %d" % (VIEW_AGENT, n2))
    st = conn.execute("SELECT status FROM agents WHERE id=?", (VIEW_AGENT,)).fetchone()
    if not st or st[0] != "active":
        bad.append("view_expansion(%d) 不是 active" % VIEW_AGENT)
    return bad


def market_visible(conn, kind="agent"):
    """按 plugin_system/store/queries.py 的真实 where 子句复算市场可见性。

    市场：scope='public' AND status IN ('published','disabled')
    我的：status!='removed'
    两处口径任一放宽，退役条目就会重新出现 —— 所以两个都算。
    """
    ph = ",".join("?" * len(PIDS))
    m = conn.execute("SELECT plugin_id FROM plugins WHERE plugin_id IN (%s) "
                     "AND scope='public' AND status IN ('published','disabled')" % ph,
                     PIDS).fetchall()
    mn = conn.execute("SELECT plugin_id FROM plugins WHERE plugin_id IN (%s) "
                      "AND status!='removed'" % ph, PIDS).fetchall()
    return [r[0] for r in m], [r[0] for r in mn]


def main():
    print("=" * 78)
    print("P1-3 门禁：视图 Agent 已从插件库移除")
    print("=" * 78)

    has_db = os.path.isfile(DB) and os.path.getsize(DB) > 0
    if not has_db:
        DEGRADED.append("无 mbse.db：R/C/M 组跳过，仅跑 X 组变异自证")
        print("  SKIP R/C/M（干净 checkout 无真库）")
    else:
        stat0 = (os.path.getsize(DB), os.path.getmtime(DB))
        c = ro()
        try:
            b = bad_final_state(c)
            rec("R1", not b, "插件库退役彻底: %s"
                % (b or "8 条 removed + 8 条 disabled/deprecated + agents 0 残留"))
            b2 = bad_capability(c)
            rec("C1", not b2, "承接能力完好: %s"
                % (b2 or "9 个 Skill published 且挂在 view_expansion(637)"))
            hist = c.execute("SELECT COUNT(*) FROM plugin_audit_logs WHERE plugin_id IN (%s)"
                             % ",".join("?" * len(PIDS)), PIDS).fetchone()[0]
            rec("C2", hist >= 24, "历史审计保留（%d 行 ≥ 24）" % hist)
            ver = c.execute("SELECT COUNT(*) FROM plugin_versions WHERE plugin_id IN (%s)"
                            % ",".join("?" * len(PIDS)), PIDS).fetchone()[0]
            rec("C3", ver == 8, "plugin_versions 保留 8 行（软删除不改版本记录）")
            vis, vis_mine = market_visible(c)
            rec("M1", not vis, "插件市场查不到这 8 条（scope/status 口径实测）%s"
                % (vis or ""))
            rec("M2", not vis_mine, "「我的能力」也查不到（status!='removed'）%s"
                % (vis_mine or ""))
            # 反向对照：市场里仍应有其它 agent，证明 M1 不是「因为市场空了」而空转
            others = c.execute("SELECT COUNT(*) FROM plugins WHERE type='agent' "
                               "AND scope='public' AND status IN ('published','disabled')"
                               ).fetchone()[0]
            rec("M3", others >= 1, "负对照：市场仍有其它 agent 插件 %d 条（M1 非空转）"
                % others)
        finally:
            c.close()
        stat1 = (os.path.getsize(DB), os.path.getmtime(DB))
        rec("Z1", stat0 == stat1, "真库全程 size/mtime 未变（只读，%d B）" % stat1[0])

    # ── X 变异自证（内存库，零 IO）───────────────────────────────────────
    mem = sqlite3.connect(":memory:")
    mem.row_factory = sqlite3.Row
    mem.executescript("""
        CREATE TABLE plugins (plugin_id TEXT, status TEXT, scope TEXT, type TEXT);
        CREATE TABLE capability_registry (capability_id TEXT, status TEXT, lifecycle TEXT);
        CREATE TABLE agents (id INTEGER, status TEXT);
        CREATE TABLE skills (name TEXT, status TEXT);
        CREATE TABLE agent_tools (agent_id INTEGER, tool_type TEXT, tool_name TEXT, enabled INTEGER);
    """)
    for p in PIDS:
        mem.execute("INSERT INTO plugins VALUES (?,?,?,?)", (p, "published", "public", "agent"))
        mem.execute("INSERT INTO capability_registry VALUES (?,?,?)",
                    ("plugin:" + p, "active", "published"))
    for i in range(9):
        nm = SKILL_PREFIX + "x%d" % i
        mem.execute("INSERT INTO skills VALUES (?,?)", (nm, "published"))
        mem.execute("INSERT INTO agent_tools VALUES (?,?,?,?)", (VIEW_AGENT, "skill", nm, 1))
    mem.execute("INSERT INTO agents VALUES (?,?)", (VIEW_AGENT, "active"))
    mem.commit()

    rec("X1", bool(bad_final_state(mem)), "变异① 未清理的状态必须判红")
    rec("X2", not bad_capability(mem), "对照② 承接完好时 C 组必须判绿（防恒红）")
    v1, v2 = market_visible(mem)
    rec("X3", len(v1) == 8 and len(v2) == 8,
        "变异③ published+public 时市场可见 8 条（证明 M1 有牙齿）")
    mem.execute("UPDATE plugins SET status='removed'")
    mem.execute("UPDATE capability_registry SET status='disabled', lifecycle='deprecated'")
    mem.commit()
    rec("X4", not bad_final_state(mem), "变异④ 正确清理后同一判据转绿")
    rec("X5", not market_visible(mem)[0] and not market_visible(mem)[1],
        "变异⑤ 清理后市场与我的能力双双不可见")
    mem.execute("UPDATE plugins SET status='disabled' WHERE plugin_id=?",
                ("com.zhiyuan.legacy.agent.ibd",))
    rec("X6", bool(market_visible(mem)[0]),
        "变异⑥ 若误把状态改成 disabled，市场会重新可见（判据必须抓住）")
    mem.execute("UPDATE plugins SET status='removed'")
    mem.execute("UPDATE skills SET status='draft' WHERE name=?",
                (SKILL_PREFIX + "x0",))
    rec("X7", bool(bad_capability(mem)), "变异⑦ 承接 Skill 落回 draft 必须判红")
    mem.close()

    print("-" * 78)
    print("PASS %d / FAIL %d | DEGRADED %d" % (len(PASS), len(FAIL), len(DEGRADED)))
    for d in DEGRADED:
        print("  DEGRADED:", d)
    if FAIL:
        print("  FAIL:", ", ".join(FAIL))
    print("=" * 78)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
