# -*- coding: utf-8 -*-
"""P1-3：把 8 个视图生成 Agent 从**插件库**中移除（软删除，照抄仓库既有 removed 范式）。

背景（2026-11-16 实测）：
  D1 只清了 `agents` 表与资产快照，**插件库没动**。实测 `plugins` id=46~53
  仍是 `type='agent' status='published'`，`capability_registry` 里 8 条
  `status=active lifecycle=published` ⇒ 插件市场仍能搜到、仍算「可用能力」。

能力承接已评估（非拍脑袋）：
  9 个 `sysml_view_generation_*` Skill 正文合计 51,785 字（原 8 个 Agent 的
  system_prompt 合计 36,158 字）；硬规则 46 条仅 1 条未覆盖；
  8 个视图的 SysML 关键字**零丢失**（3 个还有新增）；
  并额外带 autofix / validate / graph_retrieve / ast / 重试 闭环。
  唯一缺口「只输出代码，不输出解释性文字」在现行 `output_contract`
  （2026-11-15 拍板改为「结论摘要 + JSON 块」两段式）下已过时且相反，不补。

改动面（照抄既有 removed 范式 plugin#56/94/96/97/146）：
  · plugins.status          published → removed         （8 行）
  · capability_registry     active/published → disabled/deprecated（8 行）
  · plugin_versions         **不动**（既有 5 个 removed 插件的版本记录仍是 published）
  · plugin_audit_logs       保留 24 行历史 + 追加 1 行本次留痕

用法：
  ./.venv/Scripts/python.exe -X utf8 tools/_p1_3_remove_view_agent_plugins.py            # dry-run
  ./.venv/Scripts/python.exe -X utf8 tools/_p1_3_remove_view_agent_plugins.py --apply
  ./.venv/Scripts/python.exe -X utf8 tools/_p1_3_remove_view_agent_plugins.py --verify
"""
import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DB = os.environ.get("MBSE_DB_PATH") or os.path.join(ROOT, "mbse.db")
OUT = os.path.join(ROOT, "docs", "评测", "p1-3-view-agent-plugins-removal-20261116.json")
BAKDIR = os.path.join(ROOT, "backups")

# agents#165~172 在插件库里对应的 plugin_id（实测：id=46~53）
TARGETS = [
    (46, "com.zhiyuan.legacy.agent.agent-165", "需求视图生成", "requirement"),
    (47, "com.zhiyuan.legacy.agent.agent-166", "结构视图生成", "structure"),
    (48, "com.zhiyuan.legacy.agent.ibd", "交互视图（IBD）生成", "ibd"),
    (49, "com.zhiyuan.legacy.agent.agent-168", "用例视图生成", "usecase"),
    (50, "com.zhiyuan.legacy.agent.agent-169", "活动图生成", "activity"),
    (51, "com.zhiyuan.legacy.agent.agent-170", "状态机视图生成", "state"),
    (52, "com.zhiyuan.legacy.agent.agent-171", "参数视图生成", "parameter"),
    (53, "com.zhiyuan.legacy.agent.agent-172", "顺序视图（时序图）生成", "sequence"),
]
PIDS = [t[1] for t in TARGETS]
SKILL_PREFIX = "sysml_view_generation_"

PASS, FAIL = [], []


def rec(tag, cond, detail=""):
    (PASS if cond else FAIL).append(tag)
    print("  %s %-4s %s" % ("PASS" if cond else "FAIL", tag, detail))


def utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def ro():
    c = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
    c.row_factory = sqlite3.Row
    return c


def rw():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def backup():
    os.makedirs(BAKDIR, exist_ok=True)
    dst = os.path.join(BAKDIR, "mbse.db.bak-before-p1-3-plugins-%s.db"
                       % datetime.now().strftime("%Y%m%d_%H%M%S"))
    src = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
    out = sqlite3.connect(dst)
    src.backup(out)
    src.close()
    out.close()
    # 判等口径：sqlite_master 表数 + 关键表行数（不看文件字节）
    a, b = ro(), sqlite3.connect("file:%s?mode=ro" % dst.replace("\\", "/"), uri=True)
    na = a.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0]
    nb = b.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0]
    rows = {}
    for t in ("plugins", "plugin_versions", "plugin_installs", "capability_registry",
              "plugin_audit_logs", "agents", "skills"):
        rows[t] = (a.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0],
                   b.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0])
    a.close(), b.close()
    assert na == nb and all(x == y for x, y in rows.values()), \
        "备份判等失败: master %s/%s rows %s" % (na, nb, rows)
    print("  备份: %s (%d B)\n        sqlite_master %d/%d，关键表行数逐项一致 %s"
          % (os.path.basename(dst), os.path.getsize(dst), na, nb,
             {k: v[0] for k, v in rows.items()}))
    return dst


# ── 判据单一真源（门禁与变异共用）────────────────────────────────────────
def check_state(conn, expect_removed=True):
    """返回违规字符串列表（空 = 通过）。"""
    bad = []
    ph = ",".join("?" * len(PIDS))
    rows = conn.execute("SELECT plugin_id,status FROM plugins WHERE plugin_id IN (%s)"
                        % ph, PIDS).fetchall()
    if len(rows) != len(PIDS):
        bad.append("plugins 命中 %d 行，应为 %d（行被物理删除了？）" % (len(rows), len(PIDS)))
    want = "removed" if expect_removed else "published"
    wrong = [(r[0], r[1]) for r in rows if r[1] != want]
    if wrong:
        bad.append("plugins.status 非 %s: %s" % (want, wrong[:4]))
    caps = ["plugin:" + p for p in PIDS]
    crows = conn.execute("SELECT capability_id,status,lifecycle FROM capability_registry "
                         "WHERE capability_id IN (%s)" % ",".join("?" * len(caps)),
                         caps).fetchall()
    if len(crows) != len(caps):
        bad.append("capability_registry 命中 %d 行，应为 %d" % (len(crows), len(caps)))
    if expect_removed:
        badc = [(r[0], r[1], r[2]) for r in crows
                if r[1] != "disabled" or r[2] != "deprecated"]
    else:
        badc = [(r[0], r[1], r[2]) for r in crows
                if r[1] != "active" or r[2] != "published"]
    if badc:
        bad.append("capability_registry 状态不符: %s" % badc[:4])
    return bad


def check_capability_kept(conn):
    """承接方 9 个 Skill 必须仍 published 且仍挂在 view_expansion 上。"""
    bad = []
    n = conn.execute("SELECT COUNT(*) FROM skills WHERE name LIKE ? AND status='published'",
                     (SKILL_PREFIX + "%",)).fetchone()[0]
    if n != 9:
        bad.append("承接 Skill 应为 9 个 published，实得 %d" % n)
    n2 = conn.execute("SELECT COUNT(*) FROM agent_tools WHERE agent_id=637 "
                      "AND tool_type='skill' AND tool_name LIKE ? AND enabled=1",
                      (SKILL_PREFIX + "%",)).fetchone()[0]
    if n2 != 9:
        bad.append("view_expansion(637) 挂载的视图 Skill 应为 9 个，实得 %d" % n2)
    return bad


def snapshot(conn):
    """与本次无关的表的行数快照，用于证明没误伤。"""
    out = {}
    for t in ("agents", "skills", "tools", "agent_tools", "conversations",
              "agent_tasks", "plugin_installs", "plugin_versions", "plugin_audit_logs",
              "plugin_dependencies", "plugin_grants", "plugin_reviews",
              "plugin_credentials", "plugin_call_logs", "capability_registry"):
        out[t] = conn.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--verify", action="store_true")
    args = ap.parse_args()

    print("=" * 78)
    mode = "VERIFY" if args.verify else ("APPLY" if args.apply else "DRY-RUN")
    print("P1-3 视图 Agent 插件库移除 [%s]  db=%s" % (mode, os.path.basename(DB)))
    print("=" * 78)

    if args.verify:
        c = ro()
        bad = check_state(c, True)
        rec("V1", not bad, "插件库最终态: %s" % (bad or "8 条 removed + 8 条 disabled/deprecated"))
        bad2 = check_capability_kept(c)
        rec("V2", not bad2, "承接能力未受影响: %s" % (bad2 or "9 个 Skill 仍 published 且挂在 view_expansion"))
        hist = c.execute("SELECT COUNT(*) FROM plugin_audit_logs WHERE plugin_id IN (%s)"
                         % ",".join("?" * len(PIDS)), PIDS).fetchone()[0]
        rec("V3", hist >= 24, "历史审计保留（%d 行 ≥ 24）" % hist)
        c.close()
        print("-" * 78)
        print("PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
        if FAIL:
            print("FAIL:", ", ".join(FAIL))
        return 0 if not FAIL else 1

    c = ro()
    # ── S 前置 ────────────────────────────────────────────────────────
    print("[S] 前置校验")
    rows = c.execute("SELECT id,plugin_id,name,type,status FROM plugins WHERE id IN (%s)"
                     % ",".join("?" * len(PIDS)),
                     [t[0] for t in TARGETS]).fetchall()
    rec("S1", len(rows) == 8, "插件库命中 8 条（实得 %d）" % len(rows))
    rec("S2", all(r["type"] == "agent" for r in rows),
        "8 条 type 全为 agent: %s" % sorted({r["type"] for r in rows}))
    cur_st = sorted({r["status"] for r in rows})
    rec("S3", cur_st == ["published"] if not args.verify else True,
        "当前 status = %s" % cur_st)
    rec("S4", all(r["plugin_id"] == p for r, p in zip(sorted(rows, key=lambda x: x["id"]), PIDS)),
        "plugin_id 与清单逐一对应")
    # 删除安全性：未被安装、无依赖、无调用
    n_inst = c.execute("SELECT COUNT(*) FROM plugin_installs WHERE plugin_id IN (%s)"
                       % ",".join("?" * len(PIDS)), PIDS).fetchone()[0]
    rec("S5", n_inst == 0, "从未被安装（plugin_installs=%d）" % n_inst)
    n_dep = c.execute("SELECT COUNT(*) FROM plugin_dependencies WHERE provider_id IN (%s) "
                      "OR consumer_id IN (%s)"
                      % (",".join("?" * len(PIDS)), ",".join("?" * len(PIDS))),
                      PIDS + PIDS).fetchone()[0]
    rec("S6", n_dep == 0, "无依赖关系（plugin_dependencies=%d）" % n_dep)
    n_call = c.execute("SELECT COUNT(*) FROM plugin_call_logs WHERE plugin_id IN (%s)"
                       % ",".join("?" * len(PIDS)), PIDS).fetchone()[0]
    rec("S7", n_call == 0, "无调用记录（plugin_call_logs=%d）" % n_call)
    bad2 = check_capability_kept(c)
    rec("S8", not bad2, "承接方 9 个 Skill 完好: %s" % (bad2 or "published 且挂在 view_expansion"))
    # agents 表已无这些行（D1 已删）
    n_ag = c.execute("SELECT COUNT(*) FROM agents WHERE id BETWEEN 165 AND 172").fetchone()[0]
    rec("S9", n_ag == 0, "agents 表已无 165~172（D1 已删，实得 %d）" % n_ag)
    before = snapshot(c)
    c.close()

    # ── M 变异自证（内存库，零 IO）───────────────────────────────────
    print()
    print("[M] 变异自证")
    mem = sqlite3.connect(":memory:")
    mem.execute("CREATE TABLE plugins (plugin_id TEXT, status TEXT)")
    mem.execute("CREATE TABLE capability_registry (capability_id TEXT, status TEXT, lifecycle TEXT)")
    for p in PIDS:
        mem.execute("INSERT INTO plugins VALUES (?,?)", (p, "published"))
        mem.execute("INSERT INTO capability_registry VALUES (?,?,?)",
                    ("plugin:" + p, "active", "published"))
    rec("M1", bool(check_state(mem, True)),
        "变异① 清理前调 check_state(expect_removed=True) 必须判红（%d 项）"
        % len(check_state(mem, True) or []))
    mem.execute("UPDATE plugins SET status='removed'")
    mem.execute("UPDATE capability_registry SET status='disabled', lifecycle='deprecated'")
    rec("M2", not check_state(mem, True),
        "变异② 正确清理后同一判据必须转绿")
    mem.execute("UPDATE plugins SET status='published' WHERE plugin_id=?",
                ("com.zhiyuan.legacy.agent.ibd",))
    rec("M3", bool(check_state(mem, True)),
        "变异③ 只漏掉 1 条也必须判红（防「部分成功当全成功」）")
    mem.execute("UPDATE plugins SET status='removed'")
    mem.execute("UPDATE capability_registry SET lifecycle='published' "
                "WHERE capability_id='plugin:com.zhiyuan.legacy.agent.agent-165'")
    rec("M4", bool(check_state(mem, True)),
        "变异④ 只改 status 不改 lifecycle 也必须判红")
    mem.close()

    if not args.apply:
        print()
        print("-" * 78)
        print("DRY-RUN PASS %d / FAIL %d（零写入）" % (len(PASS), len(FAIL)))
        if FAIL:
            print("FAIL:", ", ".join(FAIL))
        print("确认无误后加 --apply 执行")
        return 0 if not FAIL else 1

    if FAIL:
        print("\n!! 前置/变异有 FAIL，拒绝写库")
        return 1

    # ── 写库 ──────────────────────────────────────────────────────────
    print()
    print("[W] 备份 + 写库")
    bak = backup()
    c = rw()
    try:
        ph = ",".join("?" * len(PIDS))
        n1 = c.execute("UPDATE plugins SET status='removed', updated_at=CURRENT_TIMESTAMP "
                       "WHERE plugin_id IN (%s)" % ph, PIDS).rowcount
        n2 = c.execute("UPDATE capability_registry SET status='disabled', "
                       "lifecycle='deprecated', updated_at=CURRENT_TIMESTAMP "
                       "WHERE capability_id IN (%s)"
                       % ",".join("?" * len(PIDS)), ["plugin:" + p for p in PIDS]).rowcount
        rec("W1", n1 == 8, "plugins 更新 %d 行（应 8）" % n1)
        rec("W2", n2 == 8, "capability_registry 更新 %d 行（应 8）" % n2)
        # 留痕：照抄既有 audit_logs 字段格式（user_id/user_name/plugin_id/action/detail_json/ip）
        for pid, aname, skey in [(t[1], t[2], t[3]) for t in TARGETS]:
            c.execute("INSERT INTO plugin_audit_logs (user_id,user_name,plugin_id,action,"
                      "detail_json,ip,created_at) VALUES (?,?,?,?,?,?,CURRENT_TIMESTAMP)",
                      (0, "p1-3-migration", pid, "remove",
                       json.dumps({"removed_agent": aname,
                                   "replacement_skill": SKILL_PREFIX + skey,
                                   "reason": "视图生成能力已由 Skill 承接",
                                   "at": utc()}, ensure_ascii=False), "127.0.0.1"))
        c.commit()
        n3 = c.execute("SELECT COUNT(*) FROM plugin_audit_logs WHERE plugin_id IN (%s) "
                       "AND action='remove'" % ph, PIDS).fetchone()[0]
        rec("W3", n3 == 8, "追加 8 行 remove 审计（实得 %d）" % n3)
        # 写后校验
        bad = check_state(c, True)
        rec("W4", not bad, "写后最终态: %s" % (bad or "全部 removed / disabled+deprecated"))
        bad_cap = check_capability_kept(c)
        rec("W5", not bad_cap, "承接能力未受影响: %s" % (bad_cap or "9 个 Skill 完好"))
        after = snapshot(c)
        diff = {k: (before[k], after[k]) for k in before if before[k] != after[k]}
        expect_only = {"plugins": None, "capability_registry": None, "plugin_audit_logs": None}
        ok = all(k in ("capability_registry", "plugin_audit_logs") for k in diff)
        rec("W6", ok, "未误伤其它表：变化仅 %s" % (diff or "无"))
        # plugin_versions 必须不动（照抄既有范式）
        rec("W7", before["plugin_versions"] == after["plugin_versions"],
            "plugin_versions 未改（%d 行，与既有 removed 范式一致）"
            % after["plugin_versions"])
        rec("W8", before["agents"] == after["agents"] and before["skills"] == after["skills"],
            "agents/skills 行数未变（%d / %d）" % (after["agents"], after["skills"]))
    finally:
        c.close()

    # ── 留痕 JSON ────────────────────────────────────────────────────
    payload = {
        "schema": "p1-3/view-agent-plugins-removal@1",
        "applied_at_utc": utc(),
        "db": os.path.basename(DB),
        "backup": os.path.relpath(bak, ROOT).replace("\\", "/"),
        "method": "soft_delete（照抄既有 removed 范式 plugin#56/94/96/97/146）",
        "targets": [{"plugins_id": t[0], "plugin_id": t[1], "agents_name": t[2],
                     "skill": SKILL_PREFIX + t[3]} for t in TARGETS],
        "changes": {
            "plugins.status": "published -> removed (8 行)",
            "capability_registry": "active/published -> disabled/deprecated (8 行)",
            "plugin_versions": "不动（既有 removed 插件的版本记录仍为 published）",
            "plugin_audit_logs": "保留 24 行历史 + 追加 8 行 remove",
        },
        "capability_assessment": {
            "original_prompt_chars": 36158,
            "skill_content_chars": 51785,
            "hard_rules_total": 46,
            "hard_rules_uncovered": 1,
            "uncovered": ["需求视图：只输出代码,不输出解释性文字"],
            "uncovered_verdict": (
                "不补：现行 output_contract（2026-11-15 拍板改为「结论摘要 + JSON 块」"
                "两段式）要求写摘要，与原规则相反；view_expansion(637) 有 output_schema "
                "会注入契约，输出格式由契约层管，不应写进 Skill"),
            "sysml_keyword_loss": "8 个视图全部 0 丢失（refine/extend/parameter 为新增）",
            "extra_closure": "autofix / validate / graph_retrieve / ast / 重试",
        },
        "rollback_sql": [
            "UPDATE plugins SET status='published' WHERE plugin_id IN (%s);"
            % ",".join("'%s'" % p for p in PIDS),
            "UPDATE capability_registry SET status='active', lifecycle='published' "
            "WHERE capability_id IN (%s);"
            % ",".join("'plugin:%s'" % p for p in PIDS),
            "DELETE FROM plugin_audit_logs WHERE action='remove' AND user_name='p1-3-migration';",
        ],
        "caveat": "软删除，行未物理删除；plugin_versions 与历史审计完整保留，可单步回滚",
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8", newline="\n") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
        f.write("\n")
    print("\n  留痕: %s (%d B)" % (os.path.relpath(OUT, ROOT), os.path.getsize(OUT)))

    print()
    print("=" * 78)
    print("APPLY PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
    if FAIL:
        print("FAIL:", ", ".join(FAIL))
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
