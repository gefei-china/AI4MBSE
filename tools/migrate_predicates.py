# -*- coding: utf-8 -*-
"""P0 方案 v2 / S4：存量数据一次性清理（谓词归一 + 属性 key 归一）。

用法：
  python tools/migrate_predicates.py            # dry-run：仅预演影响行数
  python tools/migrate_predicates.py --apply    # 执行（先自动备份 relations）

清理范围（机械归一，保守策略与 normalize_* 一致）：
1. relations.relation_type：包含→CONTAINS、满足→SATISFIES、连接→CONNECTS 等（实库 24 条）
2. entities.properties key：band→频段、desc→描述 等（实库 ~75 个实体）
不触碰：实体身份合并（20 pending 消歧候选留待融合工作台人工裁决）、triples 表（当前为空）。
审计：graph_edit_logs 记 op='migrate_predicates'。
"""
import json
import sys
import os
import datetime

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from text_normalize import normalize_predicate, normalize_prop_key  # noqa: E402


def main():
    apply_mode = "--apply" in sys.argv
    from database.connection import get_db  # noqa: E402
    conn = get_db()
    stamp = datetime.date.today().strftime("%Y%m%d")

    # ── 0. 备份（--apply 才执行；表级快照，可整表回滚）──
    if apply_mode:
        n = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
            (f"relations_bak_{stamp}",)).fetchone()[0]
        if not n:
            conn.execute(f"CREATE TABLE relations_bak_{stamp} AS SELECT * FROM relations")
            conn.execute(f"CREATE TABLE entities_bak_{stamp} AS SELECT * FROM entities")
            conn.commit()
            print(f"[backup] relations_bak_{stamp} / entities_bak_{stamp} 已创建")

    # ── 1. 谓词归一 ──
    rows = conn.execute(
        "SELECT relation_type, COUNT(*) AS c FROM relations WHERE status!='deprecated' "
        "GROUP BY relation_type").fetchall()
    pred_plan = []
    for r in rows:
        nn = normalize_predicate(r["relation_type"])
        if nn != r["relation_type"]:
            pred_plan.append((r["relation_type"], nn, r["c"]))
    print("== 谓词归一 ==")
    pred_total = sum(x[2] for x in pred_plan)
    for old, new, cnt in pred_plan:
        print(f"  {old!r} x{cnt} -> {new}")
    print(f"将更新 {len(pred_plan)} 组共 {pred_total} 条边")
    if apply_mode and pred_plan:
        for old, new, _ in pred_plan:
            conn.execute("UPDATE relations SET relation_type=? WHERE relation_type=? AND status!='deprecated'",
                         (new, old))
        conn.execute(
            "INSERT INTO graph_edit_logs (op, node_id, payload, operator) VALUES (?,?,?,?)",
            ("migrate_predicates", "-",
             json.dumps({"pred_plan": [{"from": o, "to": n_, "count": c} for o, n_, c in pred_plan]},
                        ensure_ascii=False), "migration"))
        print("[applied] 谓词已归一")
    elif not apply_mode:
        print("[dry-run] 未写入。加 --apply 执行")

    # ── 2. 属性 key 归一 ──
    ents = conn.execute("SELECT id, properties FROM entities WHERE status!='deprecated'").fetchall()
    prop_touched, prop_keys = 0, {}
    updates = []
    for e in ents:
        try:
            props = json.loads(e["properties"] or "{}") if e["properties"] else {}
        except Exception:
            continue
        changed, new_props = False, {}
        for k, v in props.items():
            nk = normalize_prop_key(k)
            if nk != k and nk not in new_props:      # 折叠异写 key（先到先得，保留方优先）
                new_props[nk] = v
                prop_keys[(k, nk)] = prop_keys.get((k, nk), 0) + 1
                changed = True
            else:
                new_props[k] = v
        if changed:
            updates.append((json.dumps(new_props, ensure_ascii=False), e["id"]))
            prop_touched += 1
    print("== 属性 key 归一 ==")
    for (old, new), cnt in sorted(prop_keys.items(), key=lambda x: -x[1]):
        print(f"  {old} -> {new} ({cnt} 个实体)")
    print(f"将更新 {prop_touched} 个实体的 properties")
    if apply_mode and updates:
        conn.executemany("UPDATE entities SET properties=? WHERE id=?", updates)
        print("[applied] 属性 key 已归一")

    # ── 3. 结果校验 ──
    if apply_mode:
        left = conn.execute(
            "SELECT COUNT(*) FROM relations WHERE status!='deprecated' "
            "AND relation_type IN ('包含','满足','连接')").fetchone()[0]
        print(f"== 校验 == 旧谓词残留: {left}（预期 0）")
        conn.commit()
    conn.close()
    print(("DONE (applied)" if apply_mode else "DONE (dry-run)"))


if __name__ == "__main__":
    main()
