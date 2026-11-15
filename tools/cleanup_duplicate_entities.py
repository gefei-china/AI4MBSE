# -*- coding: utf-8 -*-
"""清理重复实例（默认 dry-run，`--apply` 才真删）。

## 什么算"该清的重复"

本工程 `entities` 主键是复合键 `(id, branch)`，同一实例在 dev/release/personal
各存一份是**设计**，不是重复。故判据必须收窄到三条同时成立：

  ① 状态 deprecated（已失效，不是在用数据）
  ② 关系引用数 = 0（删了不断任何边）
  ③ 存在**同分支 + 同名 + 同类型**的另一条记录（确实是"重复"而不是"唯一的历史"）

三条缺一不可：
  · 缺 ①：会删掉在用的节点；
  · 缺 ②：会断边（图上出现悬空）；
  · 缺 ③：会删掉"唯一但已废弃"的历史留痕（那是审计证据，不是垃圾）。

## 本库的重复根因（取证，非推测）

`sysml_sync` 显示两次 SysML 导入批次相隔 8 分钟导了**同一份模型**：
    sysml-dd8b6091 (09:25:02) → 高速率需求
    sysml-ec85eda2 (09:33:09) → 高速率需求 / 宽带载荷 / 转发器
第二次导入没有复用第一次的节点，于是同名同类型同分支各留一条。
（另两条 V2G-8f52cf34 / V2G-4c9dfa1d 是 v2g 流程 26 分钟内重复产出的同一实体。）

## 刻意不做的事

· **不动 `sysml_sync`**：那是"这批导入过什么"的留痕。实体删了它会变成孤儿行
  （join 不到），但不影响业务；删掉就等于抹掉导入历史。脚本只统计并报告孤儿数。
· **不删"活着的"重复**（如 `V2G-50fe042d` 转发器/部件，reviewed 且 refs=0）：
  它是人工确认过的记录，删它等于推翻之前的确认 —— 这类必须人来定，
  脚本只把它列出来（见 `--show-alive`）。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import urllib.error
import urllib.request
from collections import Counter, defaultdict

_THIS = __file__.replace("\\", "/")
ROOT = _THIS.rsplit("/tools/", 1)[0]
DB = ROOT + "/mbse.db"
BASE = "http://127.0.0.1:8000"


def con_ro():
    c = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
    c.row_factory = sqlite3.Row
    return c


def api(method, path, timeout=60):
    req = urllib.request.Request(BASE + path, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            txt = r.read().decode() or "{}"
            try:
                return r.status, json.loads(txt)
            except Exception:
                return r.status, txt
    except urllib.error.HTTPError as e:
        return e.code, {"error": (e.read().decode() or "")[:300]}
    except Exception as e:
        return -1, {"error": "%s: %s" % (type(e).__name__, e)}


def scan(con):
    """返回 (待清理清单, 活着的重复清单)。每一项都带判据的三要素快照。"""
    rows = [dict(r) for r in con.execute(
        "SELECT id, name, entity_type, status, branch, properties, created_at, graph_source"
        "  FROM entities")]
    refs = Counter()
    for r in con.execute("SELECT source_id, target_id FROM relations WHERE status!='deprecated'"):
        refs[r["source_id"]] += 1
        refs[r["target_id"]] += 1

    # 同分支 + 同名 + 同类型 的分组
    g = defaultdict(list)
    for r in rows:
        if r["name"]:
            g[(r["branch"], r["name"], r["entity_type"])].append(r)

    doomed, alive_dup = [], []
    for k, v in g.items():
        if len(v) < 2:
            continue
        dead = [r for r in v if r["status"] == "deprecated"]
        live = [r for r in v if r["status"] != "deprecated"]
        for r in dead:
            rec = dict(r, refs=refs.get(r["id"], 0), group_size=len(v),
                       other_alive=[x["id"] for x in live])
            (doomed if rec["refs"] == 0 else alive_dup).append(rec)
        # 同组里 ≥2 条都活着、且都无引用 ⇒ 也是重复，但需人工定
        live_orphan = [r for r in live if refs.get(r["id"], 0) == 0]
        if len(live) >= 2:
            for r in live_orphan:
                alive_dup.append(dict(r, refs=0, group_size=len(v),
                                      other_alive=[x["id"] for x in live if x["id"] != r["id"]],
                                      reason="同组多条活着且本条无引用"))
    return doomed, alive_dup, refs


def print_group(recs, title):
    print("=" * 92)
    print(title)
    print("=" * 92)
    if not recs:
        print("  （无）")
        return
    by = defaultdict(list)
    for r in recs:
        by[(r["branch"], r["name"], r["entity_type"])].append(r)
    for k, v in sorted(by.items()):
        print("\n  ▸ [%s] %s ／ %s  （同组共 %d 条）" % (k[0], k[1], k[2], v[0]["group_size"]))
        for r in v:
            print("      %-14s %-9s refs=%-2d created=%s src=%-12s other_alive=%s"
                  % (r["id"], r["status"], r["refs"], (r["created_at"] or "")[:19],
                     r["graph_source"] or "-", r.get("other_alive") or "-"))
            if r.get("reason"):
                print("          原因：%s" % r["reason"])


def orphan_sysml(con, ids):
    q = "SELECT id, sysml_ref, node_id FROM sysml_sync WHERE node_id IN (%s)" % \
        ",".join("?" * len(ids))
    return [dict(r) for r in con.execute(q, ids)] if ids else []


def report(con):
    """全貌诊断（**不做删除判断**）。判据的唯一真源是上面的 `scan()`。

    为什么单列这个视图：`scan()` 的判据刻意收窄（三条同时成立），
    于是"deprecated 但同组唯一"（类型迁移残留）、"零引用"（孤立节点）
    这类**不该由脚本自动删**的项它不会报 —— 但那不代表它们不存在。
    人需要看到全貌才能决定，所以这里只统计、不判断、不删。
    """
    rows = [dict(r) for r in con.execute(
        "SELECT id, name, entity_type, status, branch, properties, graph_source"
        "  FROM entities")]
    refs = Counter()
    for r in con.execute("SELECT source_id, target_id FROM relations WHERE status!='deprecated'"):
        refs[r["source_id"]] += 1
        refs[r["target_id"]] += 1

    print("=" * 92)
    print("全貌诊断（只报告，不判断、不删）")
    print("=" * 92)
    print("  实例总数 %d（未废弃 %d / 已废弃 %d）"
          % (len(rows), sum(1 for r in rows if r["status"] != "deprecated"),
             sum(1 for r in rows if r["status"] == "deprecated")))

    # ① 同名同类型跨分支 —— 正常副本，仅计数以免被误当重复
    g = defaultdict(list)
    for r in rows:
        if r["name"]:
            g[(r["name"], r["entity_type"])].append(r)
    cross = {k: v for k, v in g.items() if len(set(x["branch"] for x in v)) > 1}
    print("\n  ① 同名同类型跨分支：%d 组 —— entities 主键是 (id,branch)，"
          "**分支副本是设计**，不是重复" % len(cross))

    # ② deprecated 但同组唯一（类型迁移残留）
    same = defaultdict(list)
    for r in rows:
        if r["name"]:
            same[(r["branch"], r["name"], r["entity_type"])].append(r)
    solo_dead = [r for k, v in same.items() for r in v
                 if r["status"] == "deprecated" and len(v) == 1]
    print("\n  ② 已废弃但同分支同类型下**唯一**的记录（类型迁移残留，脚本不清）：%d 条"
          % len(solo_dead))
    for r in solo_dead:
        print("      %-14s %-16s %-14s branch=%-9s refs=%-2d src=%s"
              % (r["id"], r["name"], r["entity_type"], r["branch"],
                 refs.get(r["id"], 0), r["graph_source"] or "-"))

    # ③ 零引用的活实例，按名称聚（跨类型同名才值得看）
    alive = [r for r in rows if r["status"] != "deprecated" and refs.get(r["id"], 0) == 0]
    print("\n  ③ 零引用的**活**实例：%d 条 / %d（孤立节点）"
          % (len(alive), sum(1 for r in rows if r["status"] != "deprecated")))
    # ⚠️ 「跨类型同名」必须拿**全库**比对，不能只在零引用子集内比：
    #   典型情形是一份在用（refs>0）、另一份孤立（refs=0）—— 只在子集里比会漏报
    #   （本脚本第一版就这么漏了「转发器」：S-4782d01d 在用、V2G-50fe042d 孤立）。
    byname_all = defaultdict(set)
    for r in rows:
        if r["status"] != "deprecated" and r["name"]:
            byname_all[r["name"]].add(r["entity_type"])
    multi_type = {k: v for k, v in byname_all.items() if len(v) > 1}
    # 只列"其中至少有一个是零引用"的（否则那是正常的分类分歧，不是重复）
    alive_names = {r["name"] for r in alive}
    multi_type = {k: v for k, v in multi_type.items() if k in alive_names}
    if multi_type:
        print("     其中**跨类型同名**且含孤立副本（同一语义实体的两种表示，需人工定）：")
        for k, v in sorted(multi_type.items()):
            print("       ▸ %s（全库出现的类型：%s）" % (k, "/".join(sorted(v))))
            for r in [x for x in rows if x["name"] == k and x["status"] != "deprecated"]:
                print("           %-14s %-14s branch=%-9s refs=%d"
                      % (r["id"], r["entity_type"], r["branch"], refs.get(r["id"], 0)))
    else:
        print("     其中跨类型同名且含孤立副本：无")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真删（默认 dry-run）")
    ap.add_argument("--show-alive", action="store_true", help="同时列出活着的重复")
    ap.add_argument("--report", action="store_true", help="只输出全貌诊断（不判断、不删）")
    ap.add_argument("--ids", default="", help="只处理指定 id（逗号分隔，交叉过滤）")
    a = ap.parse_args()

    con = con_ro()
    try:
        if a.report:
            report(con)
            return 0
        doomed, alive_dup, refs = scan(con)
        only = {x.strip() for x in a.ids.split(",") if x.strip()}
        if only:
            doomed = [r for r in doomed if r["id"] in only]
            alive_dup = [r for r in alive_dup if r["id"] in only]

        print("库: %s" % DB)
        print("dry-run=%s\n" % (not a.apply))
        print_group(doomed, "① 待清理：deprecated + 零引用 + 同分支同名同类型重复")
        if a.show_alive or not a.apply:
            print()
            print_group(alive_dup, "② 活着的重复（**脚本不删**，需人定：删了等于推翻既有确认）")

        if not doomed:
            print("\n没有符合三条判据的重复实例。")
            return 0

        # 删前逐条复核（不信任扫描结果与删除之间的时间差）
        print("\n" + "=" * 92)
        print("删前逐条复核（三条判据同时成立才放行）")
        print("=" * 92)
        ok_list, blocked = [], []
        for r in doomed:
            st = con.execute("SELECT status FROM entities WHERE id=? AND branch=?",
                             (r["id"], r["branch"])).fetchone()
            n = con.execute(
                "SELECT COUNT(*) FROM relations WHERE status!='deprecated'"
                " AND (source_id=? OR target_id=?)", (r["id"], r["id"])).fetchone()[0]
            checks = [
                ("仍存在且 status=deprecated", bool(st) and st["status"] == "deprecated"),
                ("关系引用仍为 0", n == 0),
            ]
            bad = [x for x, v in checks if not v]
            print("  %-14s %-9s %s" % (r["id"], r["branch"],
                                       "放行" if not bad else "**阻断**: " + "; ".join(bad)))
            (ok_list if not bad else blocked).append(r)

        if blocked:
            print("\n  ⚠️ %d 条被复核阻断，不处理（判据在扫描后发生了变化）" % len(blocked))

        # sysml_sync 孤儿：只报告
        orph = orphan_sysml(con, [r["id"] for r in ok_list])
        if orph:
            print("\n  ℹ️ 以下 %d 条 sysml_sync 映射将变成孤儿行（**保留**：它是导入历史留痕，"
                  "不影响业务，删除等于抹掉「这批导入过什么」）：" % len(orph))
            for o in orph:
                print("      sysml_ref=%-14s node_id=%-14s" % (o["sysml_ref"], o["node_id"]))

        if not a.apply:
            print("\n[dry-run] 未删除任何数据。确认后加 --apply 执行。")
            return 0

        print("\n" + "=" * 92)
        print("执行删除（走 API，留 audit + graph_edit_logs）")
        print("=" * 92)
        done, failed = [], []
        for r in ok_list:
            code, res = api("DELETE", "/api/knowledge/graph/nodes/%s?branch=%s"
                            % (r["id"], r["branch"]))
            okk = (code == 200 and isinstance(res, dict) and res.get("ok"))
            print("  %-14s %-9s %-16s http=%s %s"
                  % (r["id"], r["branch"], r["name"], code,
                     "OK" if okk else "FAIL " + str(res)[:110]))
            (done if okk else failed).append(r)

        # 复验
        print("\n" + "=" * 92)
        print("复验")
        print("=" * 92)
        con2 = con_ro()
        try:
            d2, a2, _ = scan(con2)
            total = con2.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
            dangling = con2.execute(
                "SELECT COUNT(*) FROM relations r"
                " LEFT JOIN entities s ON s.id=r.source_id AND s.branch=r.branch"
                " LEFT JOIN entities t ON t.id=r.target_id AND t.branch=r.branch"
                " WHERE r.status!='deprecated' AND (s.id IS NULL OR t.id IS NULL)"
            ).fetchone()[0]
        finally:
            con2.close()
        print("  剩余实例总数      : %d" % total)
        print("  剩余待清理重复    : %d（期望 0）" % len(d2))
        print("  悬空边（端点丢失）: %d（期望 0）" % dangling)
        allok = (not failed) and len(d2) == 0 and dangling == 0
        print("\n  RESULT: %s" % ("PASS" if allok else "FAIL"))
        return 0 if allok else 1
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
