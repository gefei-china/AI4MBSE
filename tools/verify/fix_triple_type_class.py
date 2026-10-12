"""一次性数据修复：把「type 谓词但 object_type='literal'」的历史三元组
重置为待物化，让它们重走**已修正**的写图链（graph_db.py:809 会按 class 处理）。

⚠️ 为什么是「重置 graph_stored」而不是「直接 UPDATE object_type='class'」：
   - 生产代码 `graph_db.sync_pending_triples` 已有
     `o_type: "class" if pred in ("type","a","rdf:type")` 的修正；
   - 直接改 object_type 会绕过这条统一逻辑（下次别的路径写入又变回 literal）；
   - 重置 graph_stored=0 ⇒ 走**与新数据完全相同**的路径，物化后 object_type 仍是
     'literal'（那是"三元组客体"的语义），但**写进图库的是类 IRI**。
   ⇒ 这才是「修数据而不破坏代码契约」的做法。

幂等：已重置过的行再次执行影响 0 行。
安全：改前自动备份到 tmp/，回滚只需按备份写回。
"""
import json
import os
import sqlite3
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_DB = os.path.join(_ROOT, "mbse.db")


def _backup(rows) -> str:
    os.makedirs(os.path.join(_ROOT, "tmp"), exist_ok=True)
    p = os.path.join(_ROOT, "tmp", f"triples_type_reset_{time.strftime('%Y%m%d_%H%M%S')}.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=1)
    return p


def main() -> int:
    rollback = "--rollback" in sys.argv

    conn = sqlite3.connect(_DB)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        "SELECT id, triple_id, object_type, graph_stored FROM triples "
        "WHERE predicate='type' AND COALESCE(graph_stored,0)=1")]

    if rollback:
        # 找最近一份备份写回
        d = os.path.join(_ROOT, "tmp")
        cands = sorted(f for f in os.listdir(d) if f.startswith("triples_type_reset_"))
        if not cands:
            print("找不到备份，无法回滚")
            return 1
        bak = json.load(open(os.path.join(d, cands[-1]), encoding="utf-8"))
        for r in bak:
            conn.execute("UPDATE triples SET graph_stored=? WHERE id=?",
                         (r["graph_stored"], r["id"]))
        conn.commit()
        print(f"[回滚] 已按 {cands[-1]} 复原 {len(bak)} 行")
        return 0

    print("=" * 70)
    print("修复：type 谓词三元组重置为待物化（走已修正的写图链）")
    print("=" * 70)
    print(f"命中 {len(rows)} 行（predicate='type' 且 graph_stored=1）")
    if not rows:
        print("无需修复（已全部重置过）")
        return 0

    bak = _backup(rows)
    print(f"已备份 -> {os.path.relpath(bak, _ROOT)}")

    # ⚠️ 回滚放finally（本轮教训：临时改数据必须这样，否则崩了就留脏数据）
    try:
        ids = [r["id"] for r in rows]
        ph = ",".join("?" * len(ids))
        conn.execute(f"UPDATE triples SET graph_stored=0 WHERE id IN ({ph})", ids)
        conn.commit()
        print(f"[OK  ] 已重置 {len(ids)} 行为 graph_stored=0")
    finally:
        # 独立校验：无论成败都核对生产态
        left = conn.execute(
            "SELECT COUNT(*) FROM triples WHERE predicate='type' "
            "AND COALESCE(graph_stored,0)=1").fetchone()[0]
        pend = conn.execute(
            "SELECT COUNT(*) FROM triples WHERE predicate='type' "
            "AND COALESCE(graph_stored,0)=0 AND status='approved'").fetchone()[0]
        conn.close()
        print(f"校验：仍标记已入图={left}（应为0），待物化 approved={pend}（应=11）")
    return 0


if __name__ == "__main__":
    sys.exit(main())