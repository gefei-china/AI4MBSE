# -*- coding: utf-8 -*-
"""历史版本回填：重算每条版本链的 diff（双向）与累积契约 coverage。

背景：2026-11-09 之前 `diff` 只算 added_nodes、`element_summary` 无 contract/coverage 字段，
所以历史会话（含米爸正在复现的 990541）即使今天修好了代码，回看旧版本仍然看不到"丢了什么"。

做了什么（纯计算，不改 code_text）：
    · element_summary.nodes / edges 去重（旧数据跨视图重复 append，重复率实测 67%）
    · element_summary.contract  = 该链历史节点并集
    · element_summary.coverage  = {contract_size, covered, lost_n, lost}
    · diff                      = {added_nodes, removed_nodes, added_edges, removed_edges, kept_nodes}
                                  （按**父版本**逐对比，与线上新逻辑同口径）

边界：只改 element_summary / diff 两列；不改 status / adopted / code_text / 版本号。

用法：
    python tools/migrate_version_coverage.py            # dry-run
    python tools/migrate_version_coverage.py --apply    # 真写（自动备份）
"""
import argparse
import json
import os
import shutil
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import core.config as cfg  # noqa: E402


def _uniq(seq):
    seen, out = set(), []
    for x in seq:
        # 与线上 `_uniq_keep` 同口径：过滤乱码节点（U+FFFD），否则告警里会输出一串方块
        if not x or "\ufffd" in x:
            continue
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


# 契约滚动窗口（与 agent/utils.py::_archive_sysml_version 的 _CONTRACT_WINDOW 保持一致）
CONTRACT_WINDOW = 5


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    db = cfg.DB_PATH
    if args.apply:
        bak = os.path.join(os.path.dirname(db),
                           f"backups/mbse.db.bak-version-cov-{time.strftime('%Y%m%d-%H%M%S')}")
        os.makedirs(os.path.dirname(bak), exist_ok=True)
        shutil.copy2(db, bak)
        print(f"[备份] {bak}")

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = list(conn.execute(
        "SELECT id, conversation_id, version_label, parent_id, element_summary, diff "
        "FROM sysml_versions ORDER BY conversation_id, id"))

    # 按会话分组、按 id 升序重算（版本链是线性的：parent 指向上一版）
    by_conv = {}
    for r in rows:
        by_conv.setdefault(r["conversation_id"], []).append(r)

    updates = []
    for conv, chain in by_conv.items():
        prev_nodes, prev_edges = [], []
        history = []            # 各版去重后的 nodes（时间正序），用于滚动契约窗口
        for r in chain:
            try:
                es = json.loads(r["element_summary"] or "{}")
            except Exception:
                es = {}
            if not isinstance(es, dict):
                es = {}
            nodes = _uniq([str(x) for x in (es.get("nodes") or []) if x])
            edges = _uniq([str(x) for x in (es.get("edges") or []) if x])

            es["nodes"] = nodes[:200]
            es["edges"] = edges[:200]
            # entities/relations 是"出现次数"口径（虚高），修正为唯一数
            es["entities"] = len(nodes)
            es["relations"] = len(edges)

            ns, ps = set(nodes), set(prev_nodes)
            es_set, pe_set = set(edges), set(prev_edges)
            diff = {
                "added_nodes": [x for x in nodes if x not in ps][:200],
                "removed_nodes": [x for x in prev_nodes if x not in ns][:200],
                "added_edges": [x for x in edges if x not in pe_set][:200],
                "removed_edges": [x for x in prev_edges if x not in es_set][:200],
                "kept_nodes": len(ns & ps),
            }

            # 滚动窗口契约（与线上同口径）：最近 N 版并集 + 当前版
            window = history[-CONTRACT_WINDOW:] if len(history) > CONTRACT_WINDOW else history
            contract = _uniq([x for lst in window for x in lst] + nodes)
            lost = [x for x in contract if x not in ns]
            es["contract"] = contract[:200]
            es["coverage"] = {
                "contract_size": len(contract),
                "covered": len(ns & set(contract)),
                "lost_n": len(lost),
                "lost": lost[:200],
                "window": CONTRACT_WINDOW,
            }
            updates.append((r["id"], json.dumps(diff, ensure_ascii=False),
                            json.dumps(es, ensure_ascii=False)))
            prev_nodes, prev_edges = nodes, edges
            history.append(nodes)

    print(f"\n[计划] 重算 {len(updates)} 个版本（{len(by_conv)} 条版本链）")
    # 打印受影响最明显的链：末版 coverage.lost_n > 0
    worst = []
    for conv, chain in by_conv.items():
        last = chain[-1]
        u = [x for x in updates if x[0] == last["id"]]
        if u:
            es = json.loads(u[0][2])
            cov = es.get("coverage") or {}
            if int(cov.get("lost_n") or 0) > 0:
                worst.append((int(cov["lost_n"]), conv, last["version_label"],
                              cov.get("contract_size"), (cov.get("lost") or [])[:5]))
    worst.sort(reverse=True)
    print(f"[覆盖回归] 末版仍缺元素的链 {len(worst)} / {len(by_conv)}")
    for n, conv, lab, csize, sample in worst[:10]:
        print(f"   conv={conv} {lab}: 契约 {csize} 个，缺 {n} → {sample}")

    if not args.apply:
        print("\n[dry-run] 未写库。确认无误后加 --apply。")
        conn.close()
        return 0

    try:
        conn.execute("BEGIN")
        for vid, dj, ej in updates:
            conn.execute("UPDATE sysml_versions SET diff=?, element_summary=? WHERE id=?",
                         (dj, ej, vid))
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"\n[失败] 已回滚：{e}")
        conn.close()
        return 1
    conn.close()
    print(f"\n[完成] 已重算 {len(updates)} 个版本")
    return 0


if __name__ == "__main__":
    sys.exit(main())
