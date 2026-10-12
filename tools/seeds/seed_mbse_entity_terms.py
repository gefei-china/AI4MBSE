"""种子：MBSE 实体级中英词条（SKOS 形状），打通**跨语言消歧**。

为什么需要这个脚本而不用 `POST /api/glossary/import`（2026-10-12 实测）：
    `_commit` 把英文词条写成 `term_status='admitted'`，
    而 `load_cross_ling_index` 与 `_normalize_mentions` 的 SQL **都过滤 `term_status='approved'`**
    ⇒ **走 HTTP 导入的英文词条永远进不了消歧索引**（导入成功、效果为 0）。
    两条消歧链路口径一致是刻意保持的（见 load_cross_ling_index 的 docstring），
    不该为导入通道放宽单边口径 ⇒ 由本脚本在数据层把状态显式提升为 `approved`。

因此本脚本的定位是「**数据层**解决方案」，代码层口径不动。

用法：
    ./.venv/Scripts/python.exe -X utf8 tools/seeds/seed_mbse_entity_terms.py --dry-run
    ./.venv/Scripts/python.exe -X utf8 tools/seeds/seed_mbse_entity_terms.py --commit

保证：
  * **幂等**：同一条词条重复跑不产生新行（按 term 去重 + UPDATE 而非 INSERT）
  * **不覆盖既有定义**：只补缺失的 term；已有 term 一律不动
  * **dry-run 不写库**
"""
import argparse
import csv
import io
import os
import sqlite3
import sys

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CSV_PATH = os.path.join(BASE, "seeds", "mbse_entity_terms.csv")
DB = os.path.join(BASE, "mbse.db")

ZH = lambda s: any("一" <= c <= "鿿" for c in (s or ""))
lang_of = lambda s: "zh" if ZH(s) else "en"


def load_rows(path):
    with open(path, encoding="utf-8-sig") as f:
        return [r for r in csv.DictReader(f) if (r.get("名称") or "").strip()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true", help="真落库（默认 dry-run）")
    ap.add_argument("--db", default=DB)
    args = ap.parse_args()

    rows = load_rows(CSV_PATH)
    print("CSV 数据行 =", len(rows))
    if not rows:
        print("✗ CSV 无数据行")
        return 1

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    _cols = {r[1] for r in conn.execute("PRAGMA table_info(glossary_terms)")}
    _has_uim = "use_in_matching" in _cols
    if not _has_uim and args.commit:
        print("⚠ glossary_terms 缺 use_in_matching 列 ⇒ 跳过提权"
              "（请先跑 init_db / _migrate_columns）")
    stats = {"concept_new": 0, "concept_upd": 0, "term_new": 0,
             "term_skip": 0, "no_uim_column": False, "cross_conflict": []}
    try:
        for r in rows:
            cid = (r.get("术语ID") or "").strip()
            label = r["名称"].strip()
            en_terms = [t.strip() for t in (r.get("英文") or "").split(";") if t.strip()]
            zh_terms = [t.strip() for t in (r.get("同义词") or "").split(";") if t.strip()]
            abbr = [(r.get("缩写") or "").strip()]

            # ── 同形异义预检：同一个英文词条是否已挂在**别的**概念下 ──
            for t in en_terms + [a for a in abbr if a]:
                hit = conn.execute(
                    "SELECT concept_id FROM glossary_terms WHERE LOWER(term)=LOWER(?)",
                    (t,)).fetchall()
                owners = {h["concept_id"] for h in hit} - {cid}
                if owners:
                    stats["cross_conflict"].append((t, sorted(owners)))

            exists = conn.execute(
                "SELECT 1 FROM glossary_concepts WHERE concept_id=?", (cid,)).fetchone()
            if not args.commit:
                # dry-run：只报告
                stats["concept_new" if not exists else "concept_upd"] += 1
                for t in [label] + zh_terms + en_terms + abbr:
                    if not t:
                        continue
                    has = conn.execute(
                        "SELECT 1 FROM glossary_terms WHERE concept_id=? AND LOWER(term)=LOWER(?)",
                        (cid, t)).fetchone()
                    if not has:
                        stats["term_new"] += 1
                continue

            if exists:
                # 只补空字段，绝不覆盖人工维护的内容
                conn.execute(
                    "UPDATE glossary_concepts SET definition=COALESCE(NULLIF(?,''),definition),"
                    " domain=COALESCE(NULLIF(?,''),domain), concept_status='approved',"
                    " version=version+1, updated_at=CURRENT_TIMESTAMP WHERE concept_id=?",
                    ((r.get("定义") or "").strip(), (r.get("域") or "satellite_comms").strip(), cid))
                stats["concept_upd"] += 1
            else:
                conn.execute(
                    "INSERT INTO glossary_concepts (concept_id, pref_label, definition, domain,"
                    " concept_status, created_by, source, note) VALUES (?,?,?,?,?,?,?,?)",
                    (cid, label, (r.get("定义") or "").strip(),
                     (r.get("域") or "satellite_comms").strip(), "approved",
                     "seed", (r.get("来源") or "seed").strip(), (r.get("注") or "").strip()))
                stats["concept_new"] += 1

            # ── 词条：zh preferred / en synonym / abbr，全部 term_status='approved' ──
            add = [(label, "zh", "preferred")]
            add += [(t, "zh", "synonym") for t in zh_terms]
            add += [(t, "en", "synonym") for t in en_terms]
            add += [(t, lang_of(t), "abbr") for t in abbr if t]
            for term, lg, kind in add:
                has = conn.execute(
                    "SELECT 1 FROM glossary_terms WHERE concept_id=? AND LOWER(term)=LOWER(?)",
                    (cid, term)).fetchone()
                if has:
                    stats["term_skip"] += 1
                    continue
                conn.execute(
                    "INSERT INTO glossary_terms (concept_id, term, lang, term_kind,"
                    " term_status, source, reliability, use_in_matching)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (cid, term, lg, kind, "approved", "seed", 8, 1))
                stats["term_new"] += 1
            # ⚠️ 存量行提权（2026-10-12）：把本概念下所有 approved 词条显式置
            #    `use_in_matching=1`，使其**不再依赖 term_status 间接生效** ——
            #    这才是 `use_in_matching` 存在的意义：消歧可用性与术语生命周期解耦。
            if _has_uim:
                conn.execute(
                    "UPDATE glossary_terms SET use_in_matching=1 WHERE concept_id=?", (cid,))
            else:
                stats["no_uim_column"] = True
        if args.commit:
            conn.commit()
    finally:
        conn.close()

    print(("COMMIT" if args.commit else "DRY-RUN") + " 统计：", stats)
    if stats["cross_conflict"]:
        print("⚠ 同形异义冲突（该词条已挂在别的概念下，需人工裁决）：")
        for t, owners in stats["cross_conflict"]:
            print(f"   {t!r} → 已被 {owners} 占用")
        return 2
    print("✅ 无同形异义冲突")
    return 0


if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.exit(main())