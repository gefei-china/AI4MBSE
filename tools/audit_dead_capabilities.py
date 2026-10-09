"""audit_dead_capabilities — 只读扫描「不可使用/无效/过期」的能力资产。

**只读，不改任何数据。** 目的是在删除前把每一条的判定依据落到具体证据上，
避免"看着像没用的就删"。

判定分四档（每档都有硬证据，不靠名字猜）：
  D0确认无效 —— 有结构性缺陷证据（引用不存在的工具 / 指向已删模块 / 状态自相矛盾）
  D1 高度可疑 —— 零引用且不在任何代码路径里（需人工确认，可能是新建未接线）
  D2 明确不该删 —— builtin / 有代码引用 / 有绑定 / published 且在用
  D3 测试残留 —— plugin_id/name 命中测试命名特征且 status 非published

删除顺序建议：**先 D3（无害）→ 再 D0（有硬证据）→ D1 需你确认**。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "mbse.db")

# 测试残留的命名特征（来自实测样本：e2e-checker / browser-check-970 / agent-tmp-…）
TEST_PAT = re.compile(
    r"(e2e-checker|browser-check|agent-tmp|ui-probe|p0-probe|test[-_]|tmp[-_]|probe[-_]|"
    r"foo|bar|baz|demo[-_]|sample[-_])", re.I)


def _open():
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def audit_plugins(conn):
    """plugins 表里的测试残留与不可用条目。"""
    rows = conn.execute("SELECT id,plugin_id,name,type,status,scope,namespace,"
                        "author_id,current_version FROM plugins ORDER BY id").fetchall()
    out = []
    for r in rows:
        pid, name, status = r["plugin_id"] or "", r["name"] or "", r["status"]
        is_test = bool(TEST_PAT.search(pid) or TEST_PAT.search(name))
        # 有无安装副本（决定它是否可能被人用着）
        n_inst = conn.execute(
            "SELECT COUNT(*) FROM plugin_installs WHERE plugin_id=?", (pid,)).fetchone()[0]
        # 是否被别的 plugin 声明依赖
        n_dep = 0
        for o in rows:
            if o["plugin_id"] == pid or not o["plugin_id"]:
                continue
            try:
                m = json.loads(o["manifest_json"] or "{}")
            except Exception:                # noqa: BLE001
                continue
            deps = m.get("dependencies") or {}
            if isinstance(deps, dict) and deps.get(pid):
                n_dep += 1
        out.append({
            "id": r["id"], "plugin_id": pid, "name": name, "type": r["type"],
            "status": status, "scope": r["scope"], "namespace": r["namespace"],
            "installs": n_inst, "depended_by": n_dep,
            "is_test_name": is_test,
            "visible_in_center": status != "removed" and (n_inst > 0 or r["author_id"] != 0),
        })
    return out


def audit_agents(conn, code_refs):
    """agents 表：状态异常 + 零绑定 + 名称不在代码引用里。"""
    rows = conn.execute(
        "SELECT id,name,display_name,status,builtin,agent_role,task_domain,stage_order,"
        "system_prompt,description FROM agents ORDER BY id").fetchall()
    out = []
    for r in rows:
        aid, nm = r["id"], r["name"] or ""
        n_bind = conn.execute(
            "SELECT COUNT(*) FROM agent_tools WHERE agent_id=?", (aid,)).fetchone()[0]
        n_team = conn.execute(
            "SELECT COUNT(*) FROM agent_team_members WHERE sub_agent_id=? AND enabled=1",
            (aid,)).fetchone()[0]
        in_code = nm in code_refs
        issues = []
        if r["status"] not in ("active", "disabled"):
            issues.append(f"status值异常={r['status']}")
        if not (r["description"] or "").strip():
            issues.append("描述为空")
        if not (r["system_prompt"] or "").strip():
            issues.append("system_prompt 为空")
        out.append({
            "id": aid, "name": nm, "display": r["display_name"],
            "status": r["status"], "builtin": r["builtin"], "role": r["agent_role"],
            "domain": r["task_domain"], "stage": r["stage_order"],
            "binds": n_bind, "team": n_team, "in_code": in_code,
            "issues": issues,
        })
    return out


def audit_tools(conn, code_refs):
    """tools 表：零绑定 + 不在代码里 + 契约问题。"""
    rows = conn.execute("SELECT id,name,status,side_effect,version,input_schema,"
                        "description FROM tools ORDER BY id").fetchall()
    out = []
    for r in rows:
        tid, nm = r["id"], r["name"] or ""
        n_bind = conn.execute(
            "SELECT COUNT(*) FROM agent_tools WHERE tool_name=?", (nm,)).fetchone()[0]
        # 技能白名单引用
        n_skill = 0
        for s in conn.execute("SELECT allowed_tools FROM skills"):
            try:
                arr = json.loads(s[0] or "[]")
            except Exception:                # noqa: BLE001
                continue
            if nm in (arr or []):
                n_skill += 1
        schema = r["input_schema"] or ""
        issues = []
        if not schema.strip() or schema.strip() == "{}":
            issues.append("input_schema 为空（LLM 只能靠 description 猜参数名）")
        if r["status"] != "active":
            issues.append(f"status={r['status']}")
        out.append({
            "id": tid, "name": nm, "status": r["status"],
            "side_effect": r["side_effect"], "binds": n_bind, "skill_refs": n_skill,
            "in_code": nm in code_refs, "issues": issues,
        })
    return out


def audit_skills(conn, live_tools):
    """skills 表：白名单空/引用不存在/已停用。"""
    rows = conn.execute("SELECT id,name,status,enabled,allowed_tools,allowed_roles,"
                        "dependencies,category,description FROM skills ORDER BY id").fetchall()
    out = []
    for r in rows:
        sid, nm = r["id"], r["name"] or ""
        def _j(v):
            try:
                return json.loads(v or "[]")
            except Exception:                # noqa: BLE001
                return None                 # None = 解析失败
        tools = _j(r["allowed_tools"])
        roles = _j(r["allowed_roles"])
        deps = _j(r["dependencies"])
        issues = []
        if tools is None:
            issues.append("allowed_tools 格式非JSON（解析失败 ⇒ 白名单失效）")
        elif not tools:
            issues.append("allowed_tools 为空（约束空转）")
        else:
            miss = [t for t in tools if t not in live_tools]
            if miss:
                issues.append(f"引用不存在的工具: {miss}")
        if roles is not None and not roles:
            issues.append("allowed_roles 为空")
        if deps is not None and not deps:
            pass
        if r["enabled"] not in (0, 1):
            issues.append(f"enabled 值异常={r['enabled']}")
        out.append({
            "id": sid, "name": nm, "status": r["status"], "enabled": r["enabled"],
            "category": r["category"], "n_tools": len(tools or []),
            "n_roles": len(roles or []), "issues": issues,
            "live": (r["status"] == "published" and r["enabled"] == 1),
        })
    return out


def _code_refs():
    """扫描生产代码里出现的工具名/Agent 名（排除迁移与测试脚本）。"""
    refs = set()
    skip_dirs = (".venv", "tmp", "backups", "__pycache__", "node_modules", "outputs")
    skip_files = ("migrate_", "verify_", "audit_", "test_", "eval_", "probe_",
                  "sync_plugins_backfill", "fix_", "backup_verify")
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames
                        if d not in skip_dirs and not d.startswith(".")]
        for fn in filenames:
            if not fn.endswith((".py", ".js", ".html")):
                continue
            if any(fn.startswith(p) for p in skip_files):
                continue
            p = os.path.join(dirpath, fn)
            try:
                with open(p, encoding="utf-8", errors="ignore") as fh:
                    txt = fh.read()
            except Exception:                # noqa: BLE001
                continue
            for m in re.finditer(r"[a-z][a-z0-9_]{3,40}", txt):
                refs.add(m.group(0))
    return refs


def main():
    conn = _open()
    try:
        live_tools = {r[0] for r in conn.execute(
            "SELECT name FROM tools WHERE status='active'")}
        refs = _code_refs()
        print("=" * 76)
        print("能力资产审计（只读，未改任何数据）")
        print("=" * 76)
        print(f"代码引用词条：{len(refs)} 个（用于判定「是否在代码路径里」）\n")

        # ── plugins ──
        pl = audit_plugins(conn)
        d3 = [x for x in pl if x["is_test_name"] and x["status"] != "published"]
        dead_pl = [x for x in pl if x["status"] == "removed" and not x["is_test_name"]]
        print("── ① plugins（管理注册表）──")
        print(f"  总数 {len(pl)}｜测试残留(D3) {len(d3)}｜"
              f"非测试但 removed {len(dead_pl)}｜能力中心可见 {sum(1 for x in pl if x['visible_in_center'])}")
        print(f"  D3 测试残留明细：")
        for x in d3:
            print(f"    id={x['id']:<4} {x['type']:6} {x['status']:9} inst={x['installs']} "
                  f"dep={x['depended_by']}  {x['name'][:34]}")
        if dead_pl:
            print(f"  ⚠️ 非测试命名但 status=removed（需确认是否该留作历史）：")
            for x in dead_pl[:10]:
                print(f"    id={x['id']:<4} {x['type']:6} {x['name'][:40]}")

        # ── agents ──
        ag = audit_agents(conn, refs)
        print(f"\n── ② agents ──")
        from collections import Counter
        print("  状态分布:", dict(Counter(x["status"] for x in ag)))
        dis = [x for x in ag if x["status"] == "disabled"]
        prob = [x for x in ag if x["issues"]]
        zero = [x for x in ag if x["binds"] == 0 and x["team"] == 0 and not x["in_code"]
                and x["status"] == "active"]
        print(f"  已 disabled {len(dis)} 条：")
        for x in dis:
            print(f"    id={x['id']:<4} {x['name'][:30]:32} builtin={x['builtin']} bind={x['binds']} team={x['team']}")
        print(f"  有结构性问题的 {len(prob)} 条：")
        for x in prob:
            print(f"    id={x['id']:<4} {x['name'][:28]:30} {'; '.join(x['issues'])[:60]}")
        print(f"  ★零绑定+不在代码+active（D1 高度可疑）{len(zero)} 条：")
        for x in zero:
            print(f"    id={x['id']:<4} {x['name'][:30]:32} role={x['role']} domain={x['domain']}")

        # ── tools ──
        tl = audit_tools(conn, refs)
        print(f"\n── ③ tools ──")
        print("  状态分布:", dict(Counter(x["status"] for x in tl)))
        nobind = [x for x in tl if x["binds"] == 0 and x["skill_refs"] == 0]
        print(f"  零绑定且无 skill 引用 {len(nobind)} 条：")
        for x in nobind:
            mark = "" if x["in_code"] else "  ← 代码里也没有"
            print(f"    id={x['id']:<6} {x['name'][:28]:30} status={x['status']:9}{mark}")
        noschema = [x for x in tl if x["issues"] and "input_schema" in x["issues"][0]]
        print(f"  input_schema 为空 {len(noschema)} 条：")
        for x in noschema[:12]:
            print(f"    {x['name'][:34]:36} bind={x['binds']}")

        # ── skills ──
        sl = audit_skills(conn, live_tools)
        print(f"\n── ④ skills ──")
        print("  状态分布:", dict(Counter((x["status"], x["enabled"]) for x in sl)))
        bad = [x for x in sl if x["issues"]]
        print(f"  有问题的 {len(bad)} 条：")
        for x in bad:
            print(f"    id={x['id']:<4} {x['name'][:34]:36} {'; '.join(x['issues'])[:62]}")
        notlive = [x for x in sl if not x["live"]]
        print(f"  未生效（status≠published 或 enabled=0）{len(notlive)} 条：")
        for x in notlive:
            print(f"    id={x['id']:<4} {x['name'][:34]:36} status={x['status']} enabled={x['enabled']}")

        print("\n" + "=" * 76)
        print("汇总：本次仅审计，未删除任何数据。")
        print("=" * 76)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())