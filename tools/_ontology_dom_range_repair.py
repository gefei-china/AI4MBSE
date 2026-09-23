# -*- coding: utf-8 -*-
"""本体关系类型 dom/range 漂移修复（方案 A「保语义」）+ 缺失快照补齐。

背景（2026-09-23 核查，见 docs/本体一致性校验优化方案与标杆对标-20260923.md）：
  换域链路不完整——13 个 relation 类型的 dom/range 由「演示初始化」用**巡飞弹/UAV 词表**写入，
  「领域切换」只换了 entity/attribute，**没碰 relation** → 9 个关系类型的 src/tgt 指向不存在的实体类型
  → ① 一致性检查 9 条 high → 本体无法发布；② 校验器对**存量边**重放 → 141/249 非法、
  13 个关系类型里 9 个在 28 个注册类型中**不存在任何合法组合**（今后写不进、存量边重写即被拒）。

修复依据：方案附录 A —— **以存量 249 条边为金标**，声明名单必须覆盖存量边实际用到的全部端点类型。
本脚本把「显式目标名单」与「存量实际端点硬下限」两条互相钉住：名单写死（可评审）+ 机器断言（防漏）。

三件事（全部幂等；默认 dry-run）：
  1) 改 ontology_types 的 relation.constraints.allowed_values.src/tgt（appendix A 名单）
  2) 写 ontology_change_logs 留痕（类型级 before/after）
  3) 补齐 **active 版本缺失的快照**（v25.0.0 声明为消费基线但 snapshot_count=0）
     —— 这是「消费侧读最新快照」得以成立的硬前提；不新增版本号（刻意：见 --help 说明）

用法：
  PY=./.venv/Scripts/python.exe
  $PY tools/_ontology_dom_range_repair.py            # dry-run：只打印，不写库
  $PY tools/_ontology_dom_range_repair.py --apply    # 执行（先自动 WAL 保真备份）
  $PY tools/_ontology_dom_range_repair.py --verify   # 只做修复后验收（不写库）
"""
import argparse
import copy
import glob
import json
import os
import sqlite3
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

DB = os.path.join(ROOT, "mbse.db")

# ── 修复目标：relation 名 → {src: [...], tgt: [...]} ──────────────────────────
# 6 个「有存量边」的关系：声明 := 存量实际端点（附录 A「存量实际在用」列 = 硬下限）
# 4 个「无存量边」的关系：按附录 A 的通用 UAV→卫星域映射**机械改写原声明**（可追溯、可回退，待业务确认）
# DERIVES 声明与存量完全一致 → 不动。
TARGETS = {
    # ---- 有存量边：以存量实际端点为名单 ----
    "CONTAINS": {
        "src": ["地面段", "天线分系统", "星座", "通信卫星系统", "通信载荷分系统"],
        "tgt": ["功率放大器", "卫星平台", "地面段", "地面站", "天线", "天线分系统", "姿轨控分系统",
                "数传分系统", "星地链路", "星座", "测控分系统", "测控站", "用户终端", "电源分系统",
                "转发器", "通信载荷分系统"],
    },
    "SATISFIES": {
        "src": ["功率放大器", "地面段", "天线", "天线分系统", "姿轨控分系统", "数传分系统", "星地链路",
                "星座", "测控分系统", "用户终端", "电源分系统", "转发器", "通信卫星系统", "部件"],
        "tgt": ["利益相关方需求", "单元需求", "子系统需求", "系统需求"],
    },
    "ALLOCATED_TO": {   # 原声明 src/tgt 与存量**方向相反**（附录 A 标「中」置信度）
        "src": ["利益相关方需求", "系统需求"],
        "tgt": ["功能", "外部系统", "通信载荷分系统"],
    },
    "COMPOSED_OF": {
        "src": ["电源分系统", "转发器"],
        "tgt": ["部件"],
    },
    "TRACE": {          # 原声明与存量完全不同（附录 A 标「中」置信度）
        "src": ["验证用例"],
        "tgt": ["功能"],
    },
    "VERIFIED_BY": {
        "src": ["利益相关方需求", "单元需求", "子系统需求", "系统需求"],
        "tgt": ["验证用例"],
    },
    # ---- 无存量边：通用映射改写（UAV → 卫星域），待业务确认 ----
    "CONNECTS": {"src": ["部件"], "tgt": ["测控分系统"]},
    #   原 src 飞控计算机→部件；原 tgt 卫星导航接收机→测控分系统
    "DEPENDS_ON": {"src": ["测控分系统"], "tgt": ["星座"]},
    #   原 src 卫星导航接收机→测控分系统；原 tgt 卫星导航星座→星座
    "FLOW_TO": {"src": ["用户终端", "通信载荷分系统"], "tgt": ["用户终端"]},
    #   原 src 光电吊舱→通信载荷分系统 / 机载数据终端→用户终端；原 tgt 地面数据终端、机载数据终端→用户终端
    "REALIZES": {
        "src": ["电源分系统", "数传分系统", "部件", "通信载荷分系统"],
        "tgt": ["系统需求", "子系统需求", "单元需求"],
    },
    #   原 src 动力分系统→电源分系统、战斗部/引信→部件、数据链分系统→数传分系统、光电吊舱→通信载荷分系统
}

REL_EDGES_SQL = """
SELECT r.relation_type, s.entity_type AS st, t.entity_type AS tt
  FROM relations r
  JOIN entities s ON s.id=r.source_id AND s.branch=r.branch
  JOIN entities t ON t.id=r.target_id AND t.branch=r.branch
 WHERE r.status != 'deprecated'
"""


# ── 工具 ──────────────────────────────────────────────────────────────────────
def _norm(v):
    vals = v if isinstance(v, list) else ([v] if v else [])
    return [str(x).strip() for x in vals if str(x).strip() and str(x) != "?"]


def _declared(constraints_json):
    try:
        c = json.loads(constraints_json or "{}")
    except Exception:
        c = {}
    av = c.get("allowed_values") or {}
    dom = c.get("domain") if c.get("domain") is not None else av.get("src")
    rng = c.get("range") if c.get("range") is not None else av.get("tgt")
    return c, _norm(dom), _norm(rng)


def collect(conn):
    """现状快照：entity 类型集合 / relation 类型行 / 存量边端点 / 存量边总数。"""
    ent = {r["name"] for r in conn.execute(
        "SELECT name FROM ontology_types WHERE type_kind='entity'")}
    rels = [dict(r) for r in conn.execute(
        "SELECT id, name, type_kind, constraints FROM ontology_types "
        "WHERE type_kind='relation' ORDER BY name")]
    edges = [dict(r) for r in conn.execute(REL_EDGES_SQL)]
    actual = {}
    for e in edges:
        a = actual.setdefault(e["relation_type"], {"src": set(), "tgt": set()})
        a["src"].add(e["st"])
        a["tgt"].add(e["tt"])
    return ent, rels, edges, actual


def proposed_rows(conn):
    """构造「修复后」的 ontology_types 全量行（用于试算，不写库）。"""
    rows = []
    for r in conn.execute("SELECT * FROM ontology_types"):
        d = dict(r)
        if d["name"] in TARGETS and d["type_kind"] == "relation":
            c, _, _ = _declared(d["constraints"])
            av = c.get("allowed_values") if isinstance(c.get("allowed_values"), dict) else {}
            av["src"] = list(TARGETS[d["name"]]["src"])
            av["tgt"] = list(TARGETS[d["name"]]["tgt"])
            c["allowed_values"] = av
            c.pop("domain", None)      # 旧单值写法归一（唯一事实源 = allowed_values.src/tgt）
            c.pop("range", None)
            d["constraints"] = json.dumps(c, ensure_ascii=False)
        rows.append(d)
    return rows


def simulate(conn, rows):
    """用「修复后」的 rows 跑存量边全量重校验 —— 这是本域的验收金标。"""
    from ontology_semantics import OntologyValidator
    ov = OntologyValidator(conn, rows=rows)
    bad = []
    for e in conn.execute(REL_EDGES_SQL):
        errs = ov.validate_edge(e["st"], e["relation_type"], e["tt"])
        if errs:
            bad.append((e["relation_type"], e["st"], e["tt"]))
    total = len(conn.execute(REL_EDGES_SQL).fetchall())
    return total, bad


def structural_issues(rows, ent):
    """结构体检（与 _ontology_check 的 bad_dom_range 同判据）：声明名必须 ∈ 注册实体类型。"""
    out = []
    for r in rows:
        if r["type_kind"] != "relation":
            continue
        _, dom, rng = _declared(r["constraints"])
        bad = sorted({x for x in dom + rng if x not in ent})
        if bad:
            out.append((r["name"], bad))
    return out


def _sqlite_backup(tag):
    os.makedirs(os.path.join(ROOT, "backups"), exist_ok=True)
    dst_path = os.path.join(ROOT, "backups", "mbse.db.bak-%s-%s" % (
        tag, datetime.now().strftime("%Y%m%d_%H%M%S")))
    src = sqlite3.connect(DB)
    dst = sqlite3.connect(dst_path)
    try:
        with dst:
            src.backup(dst)          # WAL 保真：绝不能 shutil.copy2（本库 journal_mode=wal）
    finally:
        src.close()
        dst.close()
    # 备份自证：表数 + 关键表行数
    a, b = sqlite3.connect(DB), sqlite3.connect(dst_path)
    try:
        ta = a.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
        tb = b.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
        ka = a.execute("SELECT COUNT(*) FROM ontology_types").fetchone()[0]
        kb = b.execute("SELECT COUNT(*) FROM ontology_types").fetchone()[0]
        assert ta == tb and ka == kb, "备份自证失败：表数 %s/%s，ontology_types %s/%s" % (ta, tb, ka, kb)
    finally:
        a.close()
        b.close()
    return dst_path, "tables=%d ontology_types=%d" % (ta, ka)


# ── 主流程 ────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="本体关系 dom/range 漂移修复（方案 A）")
    ap.add_argument("--apply", action="store_true", help="写库（默认 dry-run）")
    ap.add_argument("--verify", action="store_true", help="只做修复后验收")
    ap.add_argument("--no-snapshot", action="store_true", help="跳过 active 版本快照补齐")
    ap.add_argument("--fix-log-before", nargs="?", const="AUTO", default=None,
                    help="一次性补救：从 apply 前备份库回填被污染成 after 的 before 留痕（默认取最近备份）")
    args = ap.parse_args()

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    if args.fix_log_before is not None:
        bak = _latest_backup() if args.fix_log_before == "AUTO" else args.fix_log_before
        print("=" * 78)
        print("留痕 before 回填（一次性）　源备份=%s" % (os.path.relpath(bak, ROOT) if bak else "(未找到)"))
        print("=" * 78)
        return _fix_log_before(conn, bak)

    ent, rels, edges, actual = collect(conn)
    print("=" * 78)
    print("本体关系 dom/range 漂移修复　库=%s" % DB)
    print("注册实体类型 %d ｜ relation 类型 %d ｜ 存量边 %d" % (len(ent), len(rels), len(edges)))
    print("=" * 78)

    # ── 修复前基线 ──
    _, bad_before = simulate(conn, [dict(r) for r in conn.execute("SELECT * FROM ontology_types")])
    struct_before = structural_issues([dict(r) for r in conn.execute("SELECT * FROM ontology_types")], ent)
    print("\n[修复前] 悬空 dom/range 的关系类型 %d 个 ｜ 存量边非法 %d/%d"
          % (len(struct_before), len(bad_before), len(edges)))
    for n, b in struct_before:
        print("   - %-14s 悬空: %s" % (n, "、".join(b)))

    # ── 目标名单自检（两道） ──
    print("\n[目标名单自检]")
    fail = 0
    for name, t in sorted(TARGETS.items()):
        names = t["src"] + t["tgt"]
        unknown = [x for x in names if x not in ent]
        a = actual.get(name, {"src": set(), "tgt": set()})
        miss_s = sorted(a["src"] - set(t["src"]))
        miss_t = sorted(a["tgt"] - set(t["tgt"]))
        ok = not unknown and not miss_s and not miss_t
        fail += 0 if ok else 1
        print("   [%s] %-13s src=%-2d tgt=%-2d 存量端点 src=%-2d tgt=%-2d %s"
              % ("OK " if ok else "BAD", name, len(t["src"]), len(t["tgt"]),
                 len(a["src"]), len(a["tgt"]),
                 ("未注册名=%s " % unknown if unknown else "")
                 + ("漏存量src=%s " % miss_s if miss_s else "")
                 + ("漏存量tgt=%s " % miss_t if miss_t else "")))
    if fail:
        print("\n❌ 目标名单自检未通过（%d 个），中止。" % fail)
        return 2

    # ── 试算（修复后） ──
    rows_after = proposed_rows(conn)
    struct_after = structural_issues(rows_after, ent)
    total_a, bad_after = simulate(conn, rows_after)
    print("\n[试算·修复后] 悬空 dom/range %d 个 ｜ 存量边非法 %d/%d"
          % (len(struct_after), len(bad_after), total_a))
    if struct_after or bad_after:
        print("❌ 试算未达金标（悬空 0 + 存量非法 0），中止。")
        for x in bad_after[:10]:
            print("   ", x)
        return 2
    print("   ✅ 金标达成：悬空 0 ｜ 存量边全部可校验通过")

    # 变更清单
    changed = []
    for r in rels:
        c, ds, dt = _declared(r["constraints"])
        t = TARGETS.get(r["name"])
        if not t:
            continue
        if ds != t["src"] or dt != t["tgt"]:
            changed.append((r["name"], ds, dt, t["src"], t["tgt"]))

    print("\n[将变更 %d 个关系类型]" % len(changed))
    for n, ds, dt, ns, nt in changed:
        print("   %-14s src %d→%d 项   tgt %d→%d 项" % (n, len(ds), len(ns), len(dt), len(nt)))

    # ── active 版本快照现状 ──
    v = conn.execute("SELECT id, version_label, status, active, snapshot_count, compatible "
                     "FROM ontology_versions WHERE active=1 ORDER BY id DESC LIMIT 1").fetchone()
    snap_n = conn.execute("SELECT COUNT(*) FROM ontology_version_snapshots WHERE version_id=?",
                          (v["id"],)).fetchone()[0] if v else 0
    if v:
        print("\n[消费基线] active %s (id=%s) status=%s snapshot_count(声明)=%s 实际快照行=%d"
              % (v["version_label"], v["id"], v["status"], v["snapshot_count"], snap_n))
        if snap_n == 0:
            print("   ⚠️ active 版本无快照 → `_active_ont_rows` 目前回退编辑态（=「读最新快照」尚不成立）")

    if args.verify:
        return _verify(conn, ent)
    if not args.apply:
        print("\n(dry-run：未写库。确认无误后加 --apply 执行)")
        return 0

    # ── 执行 ──
    bak, proof = _sqlite_backup("before-ontology-domfix")
    print("\n[备份] %s  (%s)" % (os.path.relpath(bak, ROOT), proof))

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    operator = "本体dom/range修复(方案A)"
    for n, ds, dt, ns, nt in changed:
        row = conn.execute("SELECT id, constraints FROM ontology_types WHERE name=? AND type_kind='relation'",
                           (n,)).fetchone()
        c, _, _ = _declared(row["constraints"])
        # ⚠️ 必须**深拷贝**（2026-09-23 实测踩坑）：`dict(c)` 是浅拷贝，下面 `av` 与
        # `before["allowed_values"]` 指向**同一个 dict**，原地改 src/tgt 会把 before 一起改掉
        # → 留痕的 before == after（等于没记录修复前状态，"可回退"落空）。
        # 症状：10 条留痕全部 before==after（历史留痕都不同，故一眼可辨）；真 before 只能从
        # apply 前的备份库取回 —— 见 `--fix-log-before`。
        before = copy.deepcopy(c)
        av = c.get("allowed_values") if isinstance(c.get("allowed_values"), dict) else {}
        av["src"], av["tgt"] = list(ns), list(nt)
        c["allowed_values"] = av
        c.pop("domain", None)
        c.pop("range", None)
        conn.execute("UPDATE ontology_types SET constraints=?, updated_at=?, updated_by=? WHERE id=?",
                     (json.dumps(c, ensure_ascii=False), now, operator, row["id"]))
        conn.execute(
            "INSERT INTO ontology_change_logs (type_id, action, before, after, operator, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (row["id"], "update",
             json.dumps({"allowed_values": before.get("allowed_values")}, ensure_ascii=False),
             json.dumps({"allowed_values": c.get("allowed_values")}, ensure_ascii=False),
             operator, now))
    print("[写入] 已更新 %d 个关系类型 + 留痕 %d 条" % (len(changed), len(changed)))

    # ── 补齐 active 版本快照 ──
    if v and snap_n == 0 and not args.no_snapshot:
        src = conn.execute("SELECT id, name, type_kind, parent_id, properties, constraints, "
                           "description, icon, color, iri FROM ontology_types").fetchall()
        for r in src:
            conn.execute(
                "INSERT INTO ontology_version_snapshots (version_id, type_id, name, type_kind, parent_id, "
                "properties, constraints, description, icon, color, iri) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (v["id"], r["id"], r["name"], r["type_kind"], r["parent_id"],
                 r["properties"] or "{}", r["constraints"] or "{}",
                 r["description"] or "", r["icon"] or "", r["color"] or "#185FA5", r["iri"] or ""))
        conn.execute("UPDATE ontology_versions SET snapshot_count=?, snapshot_created_at=? WHERE id=?",
                     (len(src), now, v["id"]))
        print("[快照] 已补齐 %s 的不可变快照 %d 行（原 snapshot_count=0）" % (v["version_label"], len(src)))

    conn.commit()
    print("\n[提交] 事务已提交")
    return _verify(conn, ent)


def _latest_backup():
    """最近的 apply 前备份（命名约定来自 _sqlite_backup）。

    ⚠️ 必须排掉 SQLite 边车文件 `-wal` / `-shm`：本库 `journal_mode=wal`，
    备份目录里同名 `...-wal` 会排在同名库文件**之后**，直接取 sorted()[-1] 会拿到边车
    （打开它没有 ontology_types 表 → `no such table`）。2026-09-23 实测踩过。
    """
    hits = [p for p in glob.glob(os.path.join(ROOT, "backups", "mbse.db.bak-before-ontology-domfix-*"))
            if not p.endswith(("-wal", "-shm"))]
    return sorted(hits)[-1] if hits else ""


def _fix_log_before(conn, backup_path):
    """把「留痕的 before」回填为**真实修复前状态**（从 apply 前备份库读）。

    为什么必须回填（2026-09-23 实测）：本脚本首版用 `dict(c)` 浅拷贝做 before，
    随后原地改 `allowed_values.src/tgt` **把 before 一起改掉了** → 10 条留痕 before==after，
    「可追溯、可回退」名存实亡。真 before 已不在主库里，唯一权威来源 = apply 前的
    WAL 保真备份（这正是当初坚持用 sqlite3 `backup()` 而非文件拷贝的价值）。

    幂等且**保守**：只重写「before == after（即被污染形态）」的行；对 before≠after 的历史留痕
    一律不动（避免误伤真实记录）。
    """
    if not backup_path or not os.path.exists(backup_path):
        print("❌ 找不到备份库：%r" % backup_path)
        return 2
    b = sqlite3.connect("file:" + backup_path.replace("\\", "/") + "?mode=ro", uri=True)
    b.row_factory = sqlite3.Row
    fixed, skipped = 0, []
    try:
        for name in sorted(TARGETS):
            row = conn.execute("SELECT id FROM ontology_types WHERE name=? AND type_kind='relation'",
                               (name,)).fetchone()
            brow = b.execute("SELECT constraints FROM ontology_types WHERE name=? AND type_kind='relation'",
                             (name,)).fetchone()
            if not row or not brow:
                skipped.append((name, "缺主库行或备份行"))
                continue
            try:
                bc = json.loads(brow["constraints"] or "{}")
            except Exception:
                bc = {}
            true_before = json.dumps({"allowed_values": bc.get("allowed_values")}, ensure_ascii=False)
            log = conn.execute(
                "SELECT id, before, after FROM ontology_change_logs WHERE type_id=? AND action='update' "
                "ORDER BY id DESC LIMIT 1", (row["id"],)).fetchone()
            if not log:
                skipped.append((name, "无 update 留痕"))
                continue
            if log["before"] == true_before:
                print("   [skip] %-14s before 已正确" % name)
                continue
            if log["before"] != log["after"]:
                skipped.append((name, "before≠after（非污染形态），不覆盖"))
                continue
            conn.execute("UPDATE ontology_change_logs SET before=? WHERE id=?", (true_before, log["id"]))
            fixed += 1
            print("   [fix ] %-14s 回填 before（原与 after 相同 = 被浅拷贝污染）" % name)
    finally:
        b.close()
    conn.commit()
    print("\n[回填] 已修正 %d 条留痕；跳过 %d 条 %s" % (fixed, len(skipped), skipped if skipped else ""))
    return 0


def _verify(conn, ent):
    """修复后验收（与 verify_ontology_dom_range.py 同判据，脚本内置一份便于 --verify）。"""
    from ontology_semantics import OntologyValidator
    rows = [dict(r) for r in conn.execute("SELECT * FROM ontology_types")]
    st = structural_issues(rows, ent)
    total, bad = simulate(conn, rows)
    ov = OntologyValidator(conn)
    rows_used = ov._load_types()
    print("\n" + "=" * 78)
    print("[验收] 悬空 dom/range %d ｜ 存量边非法 %d/%d" % (len(st), len(bad), total))
    # 消费口径：OntologyValidator 默认应读快照（若存在）
    v = conn.execute("SELECT id, version_label, snapshot_count FROM ontology_versions "
                     "WHERE active=1 ORDER BY id DESC LIMIT 1").fetchone()
    snap_n = conn.execute("SELECT COUNT(*) FROM ontology_version_snapshots WHERE version_id=?",
                          (v["id"],)).fetchone()[0] if v else 0
    print("[验收] active %s 快照行 %d ｜ OntologyValidator 生效类型数 %d（应 = 快照行数）"
          % (v["version_label"] if v else "-", snap_n, len(rows_used)))
    ok = (not st) and (not bad)
    print("=" * 78)
    print("结果：%s" % ("✅ 金标达成（悬空 0 + 存量边 0 非法）" if ok else "❌ 未达金标"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
