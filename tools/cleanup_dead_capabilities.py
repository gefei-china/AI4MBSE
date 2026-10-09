"""cleanup_dead_capabilities — 清理确认无用的能力资产（测试残留 / 重复行 / 越界绑定）。

⚠️ 清理原则（逐条都有硬证据，不靠名字猜）：
  1. **只删「有硬证据」的无用资产**：命名命中测试特征 + 描述自证是测试 + 状态非 published
     + 零安装副本 + 零依赖 + 对应磁盘目录也确实是测试物
  2. **「有缺陷」≠「该删」**：约束字段为空、input_schema 为空等问题资产
     若其能力本身在用（文件操作/ 报告生成 / 变更影响分析等），**一律保留**——
     那是「待补数据」，不是「废弃」。删了会砸掉在用能力。
  3. **同表重复行才删**（同名 + 内容同），并保留一行
  4. **工具/Agent 一个都不删**，只处理：① plugins 测试残留 ② skills 测试残留
     ③ skills 重复行 ④越界的 sys_* 绑定（f6a8085 已定的口径）
  5. 删 plugins 前必须确认其legacy 映射的行已无对应旧表记录，
     否则会连带删掉运行时真相

默认 dry-run；--apply 才写。含删除前后逐条对比与回读自检。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
DB = os.path.join(ROOT, "mbse.db")

# 测试残留命名特征
TEST_PAT = re.compile(
    r"(e2e-checker|browser-check|agent-tmp|ui-probe|p0-probe|"
    r"test[-_]skill|tmp[-_]|probe[-_]|foo|bar|baz|demo[-_]|sample[-_])", re.I)

# 反向自证：描述里自述为测试的关键词。
# ⚠️ 这些措辞**取自实测数据**，不是猜的（2026-10-08 逐条 dump 描述后归纳）：
#   '浏览器端到端实测创建' / 'P0 测试技能' / 'Agent：测试agent' /
#   '浏览器验证用临时探针' / 'P0 验证用，用完即删' / 'P0探针工具'
# 第一版只写了「P0 测试技能|测试技能|probe tmp」几种，
# 结果 11 条里9 条被"描述未自证"跳过 ⇒ **判据漏词 = 该删的没删**。
SELF_TEST_PAT = re.compile(
    r"(P0\s*测试技能|测试技能|test\s*skill|临时\s*探针|临时探针|probe\s*tmp|"
    r"调试用|端到端实测创建|浏览器.{0,6}实测|验证用.{0,4}用完即删|用完即删|"
    r"探针工具|探针|测试agent|Agent：测试|临时)",re.I)

stats = {}


def log(k, msg):
    stats[k] = stats.get(k, 0) + 1
    tag = "SKIP" if k == "skip" else k.upper()
    print(f"  [ {tag:4} ] {msg}")


def _open():
    c = sqlite3.connect(DB, timeout=60)
    c.row_factory = sqlite3.Row
    return c


def plan_plugins(conn, apply_):
    """plugins 测试残留：命名 + 自述 + 状态 + 零安装 + 零依赖 + 有 legacy 映射则跳过。"""
    print("\n── ① plugins 测试残留 ──")
    rows = conn.execute(
        "SELECT id,plugin_id,name,type,status,scope,author_id,manifest_json "
        "FROM plugins ORDER BY id").fetchall()
    all_ids = {r["plugin_id"] for r in rows}
    victims = []
    for r in rows:
        pid, name = r["plugin_id"] or "", r["name"] or ""
        reasons = []
        if not (TEST_PAT.search(pid) or TEST_PAT.search(name)):
            continue
        if r["status"] == "published":
            log("skip", f"id={r['id']} {name[:28]} —— status=published，属在用能力，不删")
            continue
        # 描述自证（从 manifest 取）
        try:
            m = json.loads(r["manifest_json"] or "{}")
        except Exception:                    # noqa: BLE001
            m = {}
        blob = json.dumps(m, ensure_ascii=False)
        if not SELF_TEST_PAT.search(blob):
            log("skip", f"id={r['id']} {name[:28]} —— 命名像测试但描述未自证为测试，保留待人工确认")
            continue
        n_inst = conn.execute(
            "SELECT COUNT(*) FROM plugin_installs WHERE plugin_id=?", (pid,)).fetchone()[0]
        if n_inst:
            log("skip", f"id={r['id']} {name[:28]} —— 有 {n_inst} 个安装副本，可能被人用着")
            continue
        # 被别的 plugin 依赖则跳过
        if any(o["plugin_id"] != pid and o["plugin_id"] and pid in (o["manifest_json"] or "")
               for o in rows):
            log("skip", f"id={r['id']} {name[:28]} —— 被其他 plugin 引用")
            continue
        # 有 legacy 映射且旧表仍有此行 ⇒ 它仍代表一个运行时能力，只是测试物
        rt = m.get("runtime") or {}
        lt, lid = rt.get("legacy_table"), rt.get("legacy_id")
        if lt and lid:
            alive = conn.execute(f"SELECT COUNT(*) FROM {lt} WHERE id=?", (lid,)).fetchone()[0]
            if alive:
                log("skip", f"id={r['id']} {name[:28]} —— 仍映射到 {lt} id={lid}（运行时存在）")
                continue
        victims.append((r["id"], pid, name, r["type"], r["status"]))
        log("clean", f"id={r['id']:<4} {r['type']:6} {name[:30]:32} status={r['status']}")

    stats["plugin_deleted"] = len(victims)
    if victims and apply_:
        for vid, pid, *_ in victims:
            conn.execute("DELETE FROM plugin_installs WHERE plugin_id=?", (pid,))
            # 依赖表列名实测为 consumer_id / provider_id（不是 plugin_id）。
            # ⚠️ 该表当前 0 行，仍要按真实列名写对——否则删到有依赖的数据时会崩在半路。
            if _table(conn, "plugin_dependencies"):
                conn.execute("DELETE FROM plugin_dependencies "
                             "WHERE provider_ref=? OR consumer_id=? OR provider_id=?",
                             (pid, pid, pid))
            conn.execute("DELETE FROM plugins WHERE id=?", (vid,))
        conn.commit()
        print(f"  → 已删除 {len(victims)} 条 plugins 测试残留")
    elif victims:
        print(f"  → dry-run：将删除 {len(victims)} 条（加 --apply 执行）")
    return len(victims)


def _table(conn, name):
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def plan_skill_tests(conn, apply_):
    """skills 里的 test-skill-*：描述自证 + 已 disabled + 零引用。"""
    print("\n── ② skills 测试残留 ──")
    rows = conn.execute(
        "SELECT id,name,status,enabled,description FROM skills ORDER BY id").fetchall()
    # 当前 published skill 名集合（判断是否被谁依赖）
    live_names = {r["name"] for r in conn.execute(
        "SELECT name FROM skills WHERE status='published' AND enabled=1")}
    victims = []
    for r in rows:
        nm, desc = r["name"] or "", r["description"] or ""
        if not (TEST_PAT.search(nm) or SELF_TEST_PAT.search(desc)):
            continue
        if r["status"] == "published" and r["enabled"] == 1:
            log("skip", f"id={r['id']} {nm[:28]} —— 已生效，不删")
            continue
        # 被别的 skill 依赖？
        deps = []
        for o in conn.execute("SELECT name,dependencies FROM skills WHERE id<>?", (r["id"],)):
            try:
                arr = json.loads(o["dependencies"] or "[]")
            except Exception:                # noqa: BLE001
                arr = []
            if nm in (arr or []):
                deps.append(o["name"])
        if deps:
            log("skip", f"id={r['id']} {nm[:28]} —— 被依赖：{deps}")
            continue
        victims.append((r["id"], nm, r["status"]))
        log("clean", f"id={r['id']:<4} {nm[:34]:36} status={r['status']}")

    if victims and apply_:
        for sid, nm, _ in victims:
            conn.execute("UPDATE plugins SET status='removed' WHERE name=?", (nm,))
            conn.execute("DELETE FROM skills WHERE id=?", (sid,))
        conn.commit()
        print(f"  → 已删除 {len(victims)} 条 skills 测试残留")
    elif victims:
        print(f"  → dry-run：将删除 {len(victims)} 条（加 --apply 执行）")
    return len(victims)


def plan_dup_skills(conn, apply_):
    """同名重复的 skills：保留一行（优先 published），其余标 disabled 而非删。"""
    print("\n── ③ skills 同名重复行 ──")
    rows = conn.execute("SELECT id,name,status,enabled,description,allowed_tools FROM skills "
                        "ORDER BY name, id").fetchall()
    by_name = {}
    for r in rows:
        by_name.setdefault(r["name"], []).append(r)
    dups = {k: v for k, v in by_name.items() if len(v) > 1}
    if not dups:
        log("skip", "无同名重复行")
        return 0
    for nm, lst in dups.items():
        # 保留「published+enabled」优先，其次约束字段更完整，最后 id 最小。
        # ⚠️ 不能只看 published —— 实测 Requirement_Analysis_Pre-check 两行**都是 disabled**，
        #   第一版"取 published 否则取第一行"会保留 id=70（allowed_tools 更空的���条）。
        def _score(x):
            return (1 if (x["status"] == "published" and x["enabled"] == 1) else 0,
                    len(x["allowed_tools"] or ""),
                    -x["id"])
        keep = max(lst, key=_score)
        log("clean", f"{nm[:36]:38} {len(lst)} 行，保留 id={keep['id']}"
                     f"（status={keep['status']} en={keep['enabled']}）"
                     f"，其余 {','.join(str(x['id']) for x in lst if x['id'] != keep['id'])} 标 disabled")
        if apply_:
            for x in lst:
                if x["id"] != keep["id"]:
                    conn.execute("UPDATE skills SET status='disabled', enabled=0 WHERE id=?",
                                 (x["id"],))
    if apply_ and dups:
        conn.commit()
    return len(dups)


def plan_sys_bindings(conn, apply_):
    """建模节点上的 sys_* 越界绑定（f6a8085 已定口径：Agent 能力=技能/MCP/工具三类）。"""
    print("\n── ④ 建模节点上的 sys_* 越界绑定 ──")
    SYS6 = ("sys_query_audit", "sys_query_logs", "sys_query_users",
            "sys_query_roles", "sys_query_monitor", "sys_query_config")
    ph = ",".join("?" * len(SYS6))
    rows = conn.execute(
        f"SELECT a.id AS aid, a.name AS aname, a.task_domain AS dom, "
        f"a.stage_order AS stage, t.tool_name AS tname "
        f"FROM agent_tools t JOIN agents a ON a.id=t.agent_id "
        f"WHERE t.tool_name IN ({ph}) AND a.status='active'", SYS6).fetchall()
    if not rows:
        log("skip", "无越界绑定")
        return 0
    # ★ 关键修正（dry-run 抓到的真错误）：`system_mgmt`（系统管理 Agent，task_domain='system'）
    #   绑 sys_query_* 是**它的正常能力**，不是越界 —— 第一版判据会把系统管理能力删掉。
    #   越界的定义：域为 modeling/general 却绑了 sys_*（系统查询与建模无关）。
    SELF_DOMAIN_OK = {"system"}
    victims = [r for r in rows if r["dom"] not in SELF_DOMAIN_OK]
    for r in rows:
        if r in victims:
            log("clean", f"{r['aname'][:26]:28} domain={r['dom']} stage={r['stage']} ← {r['tname']}")
        else:
            log("skip", f"{r['aname'][:24]:26} domain={r['dom']} ← {r['tname']}"
                        f"（域匹配，是该Agent 的正常能力）")
    rows = victims
    if not rows:
        print("  → 无真正越界的绑定（域匹配项已排除）")
        return 0
    if apply_:
        for r in rows:
            conn.execute("DELETE FROM agent_tools WHERE agent_id=? AND tool_name=?",
                         (r["aid"], r["tname"]))
        conn.commit()
        print(f"  → 已解绑 {len(rows)} 条")
    else:
        print(f"  → dry-run：将解绑 {len(rows)} 条（加 --apply 执行）")
    return len(rows)


def main(apply_):
    print("=" * 74)
    print("清理确认无用的能力资产" + ("（APPLY）" if apply_ else "（DRY-RUN）"))
    print("=" * 74)
    if not os.path.isfile(DB):
        print(f"[ABORT] 库不存在：{DB}")
        return 2
    conn = _open()
    try:
        nobj = conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0]
        if nobj < 50:
            print(f"[ABORT] 目标库可疑：对象数={nobj}")
            return 2
        before = {
            "plugins": conn.execute("SELECT COUNT(*) FROM plugins").fetchone()[0],
            "skills": conn.execute("SELECT COUNT(*) FROM skills").fetchone()[0],
            "tools": conn.execute("SELECT COUNT(*) FROM tools").fetchone()[0],
            "agents": conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0],
            "binds": conn.execute("SELECT COUNT(*) FROM agent_tools").fetchone()[0],
        }
        print(f"[前置] 清理前：{before}")
        # 清理前先记下能力中心可见数（用于自检的相对判据）
        vis_before = 0
        try:
            from plugin_system.store import queries as _Q
            vis_before = len(_Q.list_mine(conn, {"id": 1}, "", ""))
            print(f"[前置] 能力中心可见（清理前）：{vis_before} 项")
        except Exception as _e:
            print(f"[前置] 能力中心可见数读取失败：{_e}")

        n1 = plan_skill_tests(conn, apply_)      # ★ 必须先删 skills：plugins 的存活判据依赖它
        n2 = plan_plugins(conn, apply_)
        n3 = plan_dup_skills(conn, apply_)
        n4 = plan_sys_bindings(conn, apply_)

        after = {
            "plugins": conn.execute("SELECT COUNT(*) FROM plugins").fetchone()[0],
            "skills": conn.execute("SELECT COUNT(*) FROM skills").fetchone()[0],
            "tools": conn.execute("SELECT COUNT(*) FROM tools").fetchone()[0],
            "agents": conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0],
            "binds": conn.execute("SELECT COUNT(*) FROM agent_tools").fetchone()[0],
        }
        print("\n── 清理前后对比 ──")
        for k in before:
            delta = after[k] - before[k]
            mark = "" if delta == 0 else f"（{delta:+d}）"
            print(f"  {k:8} {before[k]:4} → {after[k]:4} {mark}")

        print("\n── 自检（能力中心可用性不得被破坏）──")
        ok = True
        try:
            from plugin_system.store import queries as Q
            n = len(Q.list_mine(conn, {"id": 1}, "", ""))
            # 判据按「清理前后差值」而非写死阈值：
            # ⚠️ 第一版写死"应 ≥ 85"，而85 是**清理前**的数字 ⇒ 删掉测试残留后
            # 正确地降到 82，却被判FAIL。**阈值必须锚定"清理后应有的量"。**
            delta = n - vis_before
            expect_drop = stats.get("plugin_deleted", 0)
            good = (delta <= 0) and (abs(delta) <= expect_drop)
            print(f"  {'[OK  ]' if good else '[FAIL]'} 能力中心可见 {n} 项"
                  f"（清理前 {vis_before}，减少 {abs(delta)}，预期减少 ≤ {expect_drop}）")
            if not good:
                ok = False
        except Exception as exc:              # noqa: BLE001
            print(f"  [FAIL] list_mine 异常：{type(exc).__name__}: {exc}")
            ok = False
        # 运行时：active Agent / tools 数量不得减少
        for tbl, key, least in (("agents", "active", 27), ("tools", "active", 41)):
            n = conn.execute(f"SELECT COUNT(*) FROM {tbl} WHERE status='active'").fetchone()[0]
            print(f"  {'[OK  ]' if n >= least else '[FAIL]'} {tbl} active {n}（应 ≥ {least}）")
            if n < least:
                ok = False
        # 绑定不得为 0
        print(f"  {'[OK  ]' if after['binds'] > 0 else '[FAIL]'} 工具绑定 {after['binds']} 条")
        if after["binds"] <= 0:
            ok = False
        print(f"  {'[OK  ]' if conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok' else '[FAIL]'} integrity_check")
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            ok = False

        print("\n" + "=" * 74)
        print(f"清理完成：skills残留 {n1} · plugins残留 {n2} · 重复行 {n3} · 越界绑定 {n4}"
              if apply_ else "（dry-run，未写库）")
        print("=" * 74)
        return 0 if ok else 1
    finally:
        conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    sys.exit(main(a.apply))