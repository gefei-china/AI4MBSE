# -*- coding: utf-8 -*-
"""P0-2 门禁：资产变更审计（core/asset_audit.py + asset_change_logs + 四路接线）。

**背景（2026-10-18《自进化 AI 平台调研评估》整改第二项）**：四类资产
（agents/prompts/skills/agent_flows）的更新此前无字段级 diff、无版本跃迁 ——
「上次改了什么、上一版长什么样」不可考，自进化失去评估基线。本门禁守护：

  A. 纯函数：版本递升形态 + diff 口径（None/''/int-str 归一化）；
  B. 配置消费：enabled/auto_bump_version/max_value_chars 三键变异自证
     （判据走 core.config，不被模块内常量旁路）；
  C. 干净库端到端（走**真实 repo 方法**，不绕道 SQL）：
     有 diff ⇒ 应用+逐字段审计+版本跃迁；**无 diff ⇒ 什么都不做**
     （不执行 UPDATE、不碰 updated_at、不写审计行——「原样保存不是变更」）；
     调用方显式版本受尊重；未知列忽略；行不存在返 ok=False；
     读取端点 /api/studio/asset-changelog 可见刚写的审计；
  D. 真库只读 smoke：迁移幂等建表（带备份，与 verify_session_monitor 同纪律）。

双环境口径：干净库自建 MBSE_DB_PATH 临时空库（import 前切库）；真库段只读+备份。
"""
import os
import shutil
import sqlite3
import sys


def _find_root():
    _d = os.path.dirname(os.path.abspath(__file__))
    for _ in range(6):
        if os.path.isfile(os.path.join(_d, "memory_service.py")):
            return _d
        _p = os.path.dirname(_d)
        if _p == _d:
            break
        _d = _p
    raise SystemExit("找不到仓库根（未找到 memory_service.py）")


ROOT = _find_root()
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# ── 干净库：先切库后 import（core.config/DB_PATH import 期绑定，顺序即正确性）──
_TMP = os.path.join(ROOT, "tmp", "verify_asset_audit")
os.makedirs(_TMP, exist_ok=True)
_CLEAN_DB = os.path.join(_TMP, "clean.db")
if os.path.exists(_CLEAN_DB):
    os.remove(_CLEAN_DB)
os.environ["MBSE_DB_PATH"] = _CLEAN_DB

_results = []


def check(name, ok, detail=""):
    _results.append((name, bool(ok), detail))
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name,
                           ("  ← " + str(detail)) if (detail and not ok) else ""))
    return bool(ok)


def skip(name, why):
    _results.append((name, None, why))
    print("  [SKIP] " + name + "  -- " + str(why))


def main():
    # ── A. 纯函数 ────────────────────────────────────────────────────
    print("== A. 纯函数（版本递升 + diff 口径）==")
    from core import asset_audit as AA

    _bumps = [("v1", "v2"), ("v1.0", "v1.1"), ("v1.0.0", "v1.0.1"), ("1.2.3", "1.2.4"),
              ("draft", "draft.1"), ("", "v1.0.1"), (None, "v1.0.1"), ("v1.0-beta", "v1.0-beta.1")]
    _bad = [f"{i}:{AA.bump_version(i)}!={o}" for i, o in _bumps if AA.bump_version(i) != o]
    check("A1 版本递升八形态（末段+1；非标准形态加 .1 不猜语义）", not _bad, "; ".join(_bad))
    _d = AA.diff_values({"a": "1", "b": None, "c": 2}, {"a": "1", "b": "", "c": 2, "zz": "x"})
    check("A2 diff：相等不变 / None→'' 算变 / int2=='2' 不变 / 未知列忽略",
          [x[0] for x in _d] == ["b"], f"got={_d}")
    check("A3 diff 空输入返空", AA.diff_values({}, {}) == [])

    # ── B. 配置消费（三键变异自证）───────────────────────────────────
    print("\n== B. 阈值走配置（DEFAULT_CONFIG/CONFIG_SCHEMA 双登记 + 变异自证）==")
    import core.config as CFG

    dc = CFG.DEFAULT_CONFIG.get("asset_audit") or {}
    check("B1 DEFAULT_CONFIG.asset_audit 三键齐全",
          all(k in dc for k in ("enabled", "auto_bump_version", "max_value_chars")),
          f"got={sorted(dc)}")
    check("B2 CONFIG_SCHEMA.asset_audit 登记（前端契约）",
          all(k in (CFG.CONFIG_SCHEMA.get("asset_audit") or {})
              for k in ("enabled", "auto_bump_version", "max_value_chars")))
    _st = AA.get_audit_settings()
    check("B3 get_audit_settings 默认值（True/True/4000）",
          _st == {"enabled": True, "auto_bump_version": True, "max_value_chars": 4000},
          f"got={_st}")

    # ── C. 干净库端到端（真实 repo 方法）─────────────────────────────
    print("\n== C. 干净库端到端（真实 repo：diff/版本/审计/读取）==")
    from database import init_db, get_db
    init_db()
    conn = get_db()
    conn.row_factory = sqlite3.Row

    from repositories.agent_repo import AgentRepo
    from repositories.studio_repo import StudioRepo

    arepo, srepo = AgentRepo(conn), StudioRepo(conn)
    aid = arepo.create_agent({"name": "aa-1", "display_name": "审计甲", "description": "初始"})
    row0 = arepo.one("SELECT * FROM agents WHERE id=?", (aid,))
    ver0 = row0["version"]
    n_logs0 = conn.execute("SELECT COUNT(*) FROM asset_change_logs").fetchone()[0]

    # C1 有 diff ⇒ 应用 + 审计 + 版本跃迁
    res = arepo.update_agent(aid, {"name": "aa-1", "display_name": "审计甲改", "description": "初始"},
                             actor="验证员", source="api")
    check("C1 有 diff ⇒ applied/changed/logged", res.get("applied") and res.get("changed")
          and res.get("logged") >= 1, f"got={res}")
    row1 = arepo.one("SELECT * FROM agents WHERE id=?", (aid,))
    check("C2 版本自动递升（%s→%s）" % (ver0, row1["version"]),
          row1["version"] != ver0 and res.get("version_from") == ver0
          and res.get("version_to") == row1["version"], f"got={row1['version']}")
    logs = conn.execute("SELECT * FROM asset_change_logs WHERE asset_type='agent' AND asset_id=?"
                        " ORDER BY id", (aid,)).fetchall()
    check("C3 审计行只记变化字段（display_name 一行）",
          len(logs) == 1 and logs[0]["field"] == "display_name"
          and logs[0]["old_value"] == "审计甲" and logs[0]["new_value"] == "审计甲改",
          f"got={[(l['field'], l['old_value'], l['new_value']) for l in logs]}")
    check("C4 审计行带版本跃迁与操作人",
          logs[0]["version_from"] == ver0 and logs[0]["version_to"] == row1["version"]
          and logs[0]["changed_by"] == "验证员" and logs[0]["source"] == "api"
          and logs[0]["asset_name"] == "aa-1",
          f"got=({logs[0]['version_from']},{logs[0]['version_to']},{logs[0]['changed_by']})")

    # C5 ★无 diff ⇒ 什么都不做（不执行 UPDATE、不碰 updated_at、不写审计）
    ua_before = row1["updated_at"]
    res_same = arepo.update_agent(aid, {"name": "aa-1", "display_name": "审计甲改", "description": "初始"})
    row2 = arepo.one("SELECT * FROM agents WHERE id=?", (aid,))
    n_logs2 = conn.execute("SELECT COUNT(*) FROM asset_change_logs").fetchone()[0]
    check("C5 ★无 diff 不动手（applied=False/logged=0）",
          res_same.get("applied") is False and res_same.get("changed") is False
          and res_same.get("logged") == 0, f"got={res_same}")
    check("C6 ★无 diff 不碰 updated_at（原样保存不是变更）",
          row2["updated_at"] == ua_before and n_logs2 == len(logs) + 0,
          f"updated_at {ua_before}→{row2['updated_at']}")

    # C7 调用方显式版本 ⇒ 尊重，不自动递升
    res_v = arepo.update_agent(aid, {"name": "aa-1", "display_name": "审计甲改2", "version": "v9.9"})
    row3 = arepo.one("SELECT * FROM agents WHERE id=?", (aid,))
    check("C7 调用方显式版本受尊重（v9.9，未递升）",
          row3["version"] == "v9.9" and res_v.get("version_to") == "v9.9",
          f"got={row3['version']}")

    # C8 能力字段 None ⇒ 保留既有（diff 驱动 = 不进 SET）
    arepo.execute("UPDATE agents SET capabilities='[\"x\"]' WHERE id=?", (aid,))
    conn.commit()
    res_c = arepo.update_agent(aid, {"name": "aa-1", "display_name": "审计甲改3", "capabilities": None})
    row4 = arepo.one("SELECT capabilities FROM agents WHERE id=?", (aid,))
    check("C8 capabilities=None 保留既有值（旧 COALESCE 语义等价）",
          row4["capabilities"] == '["x"]' and all(
              d["field"] != "capabilities" for d in res_c.get("diffs", [])),
          f"got={row4['capabilities']}")

    # C9 长值截断（默认 max_value_chars=4000）
    long_prompt = "P" * 5000
    res_l = arepo.update_agent(aid, {"name": "aa-1", "display_name": "审计甲改3", "system_prompt": long_prompt})
    log_l = conn.execute("SELECT * FROM asset_change_logs WHERE asset_type='agent' AND asset_id=?"
                         " AND field='system_prompt' ORDER BY id DESC LIMIT 1", (aid,)).fetchone()
    # 截断形态：4000 字符 + 「…[+N chars]」标记 ⇒ 总长 4000+15~4020 之间
    check("C9 超长字段截断入审计（4000+标记，不超 4020）",
          bool(log_l) and 4000 < len(log_l["new_value"]) <= 4020
          and log_l["new_value"].startswith("P" * 4000) and log_l["new_value"].endswith("chars]"),
          f"len={len(log_l['new_value']) if log_l else None}")

    # C10 skills：无 version 入参 ⇒ 自动递升
    sid = srepo.create_skill("aa-skill", "技能甲", "text2json", "[]", "cat", "内容v1", "", "")
    row_s0 = srepo.get_skill(sid)
    res_s = srepo.update_skill(sid, "aa-skill", "技能甲", "text2json", "[]", "cat", "内容v2", "",
                               actor="验证员")
    row_s1 = srepo.get_skill(sid)
    check("C10 skill 更新 ⇒ 内容变更入审计 + 版本递升（%s→%s）"
          % (row_s0["version"], row_s1["version"]),
          res_s.get("logged") >= 1 and row_s1["version"] != row_s0["version"]
          and any(d["field"] == "content" for d in res_s.get("diffs", [])),
          f"got={res_s.get('diffs')}")
    log_s = conn.execute("SELECT * FROM asset_change_logs WHERE asset_type='skill' AND asset_id=?"
                         " AND field='content' ORDER BY id DESC LIMIT 1", (sid,)).fetchone()
    check("C11 skill 审计行 old/new 正确", bool(log_s) and log_s["old_value"] == "内容v1"
          and log_s["new_value"] == "内容v2", f"got={log_s and (log_s['old_value'], log_s['new_value'])}")

    # C12 prompts：调用方版本 + 多字段 diff
    pid = srepo.create_prompt("aa-prompt", "场景", "提示v1", "[]", "v1.0")
    res_p = srepo.update_prompt(pid, "aa-prompt", "场景", "提示v2", "[]", "v2.0")
    check("C12 prompt 更新 ⇒ 2 个变更字段入审计、版本尊重调用方",
          res_p.get("logged") == 2 and res_p.get("version_to") == "v2.0",
          f"got={res_p}")

    # C13 agent_flows：nodes 变更可审计（该表无 updated_at 列，走自适应路径）
    fid = srepo.create_agent_flow("aa-flow", "流程甲", '[{"id":"n1"}]', "[]")
    res_f = srepo.update_agent_flow(fid, "aa-flow", "流程甲", '[{"id":"n1"},{"id":"n2"}]', "[]",
                                    actor="验证员")
    check("C13 agent_flow 更新 ⇒ nodes 变更入审计（无 updated_at 列表不炸）",
          res_f.get("applied") and any(d["field"] == "nodes" for d in res_f.get("diffs", [])),
          f"got={res_f}")

    # C14 未知列忽略。⚠️ 顺带锁定一条既有语义：update_agent 是**全量替换**口径
    #   （缺键文本字段写 ''，与旧 SQL data.get(k,"") 一致——路由侧总带全量表单，
    #   故一直如此）；未知键在 repo 固定键集处就被过滤，不到通用层。
    res_x = arepo.update_agent(aid, {"name": "aa-1", "display_name": "审计甲改3", "no_such_col": 1})
    check("C14 repo 层未知入参键静默忽略（不炸、不入审计、不进 SET）",
          not any(d["field"] == "no_such_col" for d in res_x.get("diffs", []))
          and res_x.get("ok") is True,
          f"got={ {k: v for k, v in res_x.items() if k != 'diffs'} }")
    res_x2 = AA.audited_update(arepo, "agent", aid,
                               {"display_name": "审计甲改3", "no_such_col": "x"}, table="agents")
    check("C14b 通用层未知列记 ignored（不进 SET、不炸）",
          res_x2.get("ignored") == ["no_such_col"] and res_x2.get("applied") is False,
          f"got={res_x2.get('ignored')}")
    res_missing = arepo.update_agent(999999, {"name": "x", "display_name": "x"})
    check("C15 不存在资产 ⇒ ok=False（调用方先查存在性）", res_missing.get("ok") is False)

    # C16 读取端点（真实函数调用）
    from routers.studio_parts.changelog import asset_changelog
    cl = asset_changelog(asset_type="agent", asset_id=aid, field="", limit=100, conn=conn)
    check("C16 读取端点返回该资产全部审计行（≥4 条）",
          cl.get("ok") and len(cl.get("items") or []) >= 4
          and all(it["asset_id"] == aid for it in cl["items"]),
          f"total={cl.get('total')}")
    cl_all = asset_changelog(asset_type="", asset_id=0, field="", limit=100, conn=conn)
    check("C17 全局读取（四类资产审计行都在）",
          cl_all.get("ok") and {it["asset_type"] for it in cl_all.get("items") or []}
          >= {"agent", "skill", "prompt", "agent_flow"},
          f"types={sorted({it['asset_type'] for it in cl_all.get('items') or []})}")

    # C18 ★配置变异：enabled=False ⇒ 照常更新但不写审计
    _orig_as_bool = CFG.as_bool
    try:
        CFG.as_bool = lambda section, key, default=False: (
            False if (section, key) == ("asset_audit", "enabled") else _orig_as_bool(section, key, default))
        n_before = conn.execute("SELECT COUNT(*) FROM asset_change_logs").fetchone()[0]
        res_off = arepo.update_agent(aid, {"name": "aa-1", "display_name": "审计甲改4"})
        n_after = conn.execute("SELECT COUNT(*) FROM asset_change_logs").fetchone()[0]
        check("C18 ★enabled=False ⇒ 更新照常、logged=0（降级通道生效）",
              res_off.get("applied") and res_off.get("logged") == 0 and n_after == n_before,
              f"applied={res_off.get('applied')} logged={res_off.get('logged')}")
    finally:
        CFG.as_bool = _orig_as_bool

    # C19 ★配置变异：auto_bump_version=False ⇒ 版本不动
    _orig_as_bool2 = CFG.as_bool
    try:
        CFG.as_bool = lambda section, key, default=False: (
            False if (section, key) == ("asset_audit", "auto_bump_version") else _orig_as_bool2(section, key, default))
        ver_b = arepo.one("SELECT version FROM agents WHERE id=?", (aid,))["version"]
        res_nb = arepo.update_agent(aid, {"name": "aa-1", "display_name": "审计甲改5"})
        ver_a = arepo.one("SELECT version FROM agents WHERE id=?", (aid,))["version"]
        check("C19 ★auto_bump_version=False ⇒ 版本保持（策略可关）",
              ver_a == ver_b and res_nb.get("logged") >= 1, f"{ver_b}→{ver_a}")
    finally:
        CFG.as_bool = _orig_as_bool2
    conn.close()

    # ── D. 真库只读 smoke（迁移幂等建表；CI 无库 ⇒ SKIP）─────────────
    print("\n== D. 真库迁移 smoke（备份 + 幂等建表 + 索引）==")
    _prod = os.path.join(ROOT, "mbse.db")
    if not os.path.exists(_prod):
        skip("D1-D3 真库断言", "CI 环境无 mbse.db（干净库口径）⇒ 判据面不存在")
    else:
        import time
        from database.migrations import _migrate_columns
        bak = os.path.join(ROOT, "backups", "mbse.db.bak-before-asset-audit-%s"
                           % time.strftime("%Y%m%d"))
        os.makedirs(os.path.dirname(bak), exist_ok=True)
        src = sqlite3.connect(_prod)
        dst = sqlite3.connect(bak)
        src.backup(dst)
        dst.close()
        src.close()
        print(f"  备份: {bak}")
        pc = sqlite3.connect(_prod)
        pc.row_factory = sqlite3.Row
        _migrate_columns(pc)
        _migrate_columns(pc)     # 幂等：第二次不报错
        tbl = pc.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='asset_change_logs'").fetchone()
        check("D1 asset_change_logs 已建（幂等）", bool(tbl))
        cols = {r[1] for r in pc.execute("PRAGMA table_info(asset_change_logs)").fetchall()}
        check("D2 十列齐全（type/id/name/field/old/new/version_from/to/by/source/created）",
              {"asset_type", "asset_id", "asset_name", "field", "old_value", "new_value",
               "version_from", "version_to", "changed_by", "source", "created_at"} <= cols,
              f"got={sorted(cols)}")
        idx = pc.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name='ix_acl_asset'").fetchone()
        check("D3 索引 ix_acl_asset 已建", bool(idx))
        n = pc.execute("SELECT COUNT(*) FROM asset_change_logs").fetchone()[0]
        print(f"  真库现有审计行 {n} 条（迁移后由服务重启后的真实更新持续产生）")
        pc.close()

    # ── E. 接线完整性 ────────────────────────────────────────────────
    print("\n== E. 接线完整性（四路更新 + 读取端点）==")
    _ag = open(os.path.join(ROOT, "repositories", "agent_repo.py"), encoding="utf-8").read()
    check("E1 agent_repo.update_agent 走 audited_update",
          "asset_audit.audited_update(self, \"agent\"" in _ag)
    _sr = open(os.path.join(ROOT, "repositories", "studio_repo.py"), encoding="utf-8").read()
    check("E2 studio_repo 三方法走 audited_update",
          _sr.count("asset_audit.audited_update(") == 3,
          f"count={_sr.count('asset_audit.audited_update(')}")
    for f, key in (("agents.py", 'actor=audit_user(user), source="api"'),
                   ("prompts.py", 'actor=audit_user(user), source="api"'),
                   ("skills.py", 'actor=audit_user(user), source="api"'),
                   ("flows.py", "actor=audit_user(user)")):
        src = open(os.path.join(ROOT, "routers", "studio_parts", f), encoding="utf-8").read()
        check(f"E3 {f} 传 changed_by（操作人入审计）", key in src)
    check("E4 studio.py 注册 changelog 分片",
          "studio_parts.changelog" in open(os.path.join(ROOT, "routers", "studio.py"),
                                           encoding="utf-8").read())
    # 前端接线：只改后端不改编前端 = 改动无效（verify_reasoning_kind 教训）
    _ih = open(os.path.join(ROOT, "static", "index.html"), encoding="utf-8").read()
    check("E5 index.html 有 st-changelog 面板与 tab 入口（审计数据对人可见）",
          'id="st-changelog"' in _ih and _ih.count('data-pane="st-changelog"') >= 7,
          f"pane 副本数={_ih.count('data-pane=\"st-changelog\"')}")
    _st = open(os.path.join(ROOT, "static", "js", "mods", "28-studio.js"), encoding="utf-8").read()
    check("E6 28-studio.js loadStChangelog 消费 /api/studio/asset-changelog",
          "async function loadStChangelog" in _st and "/api/studio/asset-changelog" in _st)
    _sh = open(os.path.join(ROOT, "static", "js", "mods", "02-shell.js"), encoding="utf-8").read()
    check("E7 02-shell.js ST_TAB_TITLES 登记 st-changelog（面包屑标题）",
          "'st-changelog':'变更记录'" in _sh)
    _im = open(os.path.join(ROOT, "static", "js", "mods", "09-impact.js"), encoding="utf-8").read()
    check("E8 09-impact.js switchStLibTab 挂 loadStChangelog 加载钩子",
          "pane==='st-changelog') loadStChangelog()" in _im)

    print("\n结果: %d PASS / %d FAIL / %d SKIP"
          % (sum(1 for r in _results if r[1] is True),
             sum(1 for r in _results if r[1] is False),
             sum(1 for r in _results if r[1] is None)))
    _failed = [r[0] for r in _results if r[1] is False]
    if _failed:
        print("失败项: " + "; ".join(_failed))
    shutil.rmtree(_TMP, ignore_errors=True)
    if _failed:
        sys.exit(1)
    print("全部通过 —— 资产变更审计（diff 驱动 + 版本跃迁 + 逐字段审计 + 配置变异 + 真库迁移）"
          + ("（干净库：真库断言按判据面不存在 SKIP）" if any(r[1] is None for r in _results) else "。"))


if __name__ == "__main__":
    main()
