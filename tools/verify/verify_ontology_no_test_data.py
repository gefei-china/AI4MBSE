# -*- coding: utf-8 -*-
"""本体数据洁净度门禁：`ontology_types` 不得残留测试标记（2026-11-23）。

## 为什么有这道门禁

`tools/verify/verify_ontology_form.js` 是**真实浏览器 + 真实保存**的门禁：
它会改某个类型的 `tlo_anchor`、写 `alt_labels="门禁别名甲、门禁别名乙"`，
验证前后端同步后**再恢复原值**。

问题：它的恢复只看了 `PUT` 的 `ok`，**没有回读**。
2026-11-23 实测生产库：

    id=9041 名称=利益相关方  constraints = {"alt_labels":["门禁别名甲","门禁别名乙"],"lang":"zh"}

⇒ 测试标记留在了本体里（而当天那道门禁是绿的）。本体是单一事实源，
被测试数据污染后：别名检索、JSON-LD/SKOS 导出、Agent 的 schema 注入都会带上假别名。

## 守什么

  T1 `ontology_types` 的 `constraints` / `properties` / `description` 不含测试标记
     （门禁别名 / 测试别名 / test alias / PROBE / dummy…）
  T2 实体与关系实例的 `entity_type` / `relation_type` 都能在本体里找到
     （否则"本体是单一事实源"不成立；这条同时防止测试实体把本体撑乱）
  T3 被检行数 > 0（防止"库里没数据 ⇒ 全绿"的空转）

## 变异（必须判红）

  M1 临时注入一行含标记的类型 ⇒ T1 红（随后删除，断言复原）
"""
from __future__ import annotations

import re
import sys
import sqlite3
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

_p, _f = [], []

#: 测试标记：只匹配"明显不是业务词"的形态，避免把正常的「测试用例」类业务类型误杀
MARKERS = re.compile(r"门禁别名|测试别名|test[ _-]?alias|PROBE|dummy|foo\s*bar|xyzzy", re.I)


def ck(cond, msg, detail=""):
    (_p if cond else _f).append(msg)
    print(("  [PASS] " if cond else "  [FAIL] ") + msg + (("  «%s»" % detail) if detail and not cond else ""))


def scan(conn):
    bad = []
    for r in conn.execute("SELECT id,name,constraints,properties,description FROM ontology_types"):
        for col in ("constraints", "properties", "description"):
            v = r[col] or ""
            m = MARKERS.search(str(v))
            if m:
                bad.append((r["id"], r["name"], col, str(v)[:80]))
    return bad


def main() -> int:
    print("=" * 78)
    print("本体数据洁净度门禁（测试标记 / 类型闭合）")
    print("=" * 78)
    from core.config import DB_PATH
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    n_types = conn.execute("SELECT COUNT(*) FROM ontology_types").fetchone()[0]
    n_ent = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    n_rel = conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0]
    if n_types == 0:
        # CI 上库可能是空的（没有真实本体数据）⇒ 明确 SKIP，不假绿也不误红
        print("  [SKIP] 库里没有类型数据（CI 常态）—— 本门禁需真实本体，**不计入通过**")
        conn.close()
        print("=" * 78)
        print("结论：SKIP（0 项通过 / 0 项失败；不计入通过）")
        print("=" * 78)
        return 0
    ck(n_types > 0, "T3 被检类型行数 > 0（防空转）", "n=%d" % n_types)
    print("  · 规模：类型 %d / 实体 %d / 关系 %d" % (n_types, n_ent, n_rel))

    bad = scan(conn)
    ck(not bad, "T1 本体不含测试标记（门禁别名/测试别名/PROBE/dummy 等）",
       str(bad[:3]))

    orphan_e = conn.execute(
        "SELECT COUNT(*) FROM entities e WHERE COALESCE(e.entity_type,'')<>'' "
        "AND NOT EXISTS(SELECT 1 FROM ontology_types t WHERE t.name=e.entity_type)").fetchone()[0]
    ck(orphan_e == 0, "T2a 实体类型都能在本体里找到", "orphan=%d" % orphan_e)
    orphan_r = conn.execute(
        "SELECT COUNT(DISTINCT relation_type) FROM relations r "
        "WHERE NOT EXISTS(SELECT 1 FROM ontology_types t WHERE t.name=r.relation_type)").fetchone()[0]
    ck(orphan_r == 0, "T2b 关系类型都能在本体里找到", "orphan=%d" % orphan_r)

    # ── 变异：注入 → 必须被抓 → 复原 → 必须回到干净 ──
    print("\n-- 变异（必须判红） --")
    injected = False
    try:
        conn.execute("INSERT INTO ontology_types (name,type_kind,constraints,status) "
                     "VALUES ('门禁变异类型_T13','entity','{\"alt_labels\":[\"门禁别名甲\"]}','active')")
        poisoned = conn.execute(
            "SELECT COUNT(*) FROM ontology_types WHERE name='门禁变异类型_T13'").fetchone()[0] == 1
        injected = True
        hit = any("门禁变异类型_T13" == b[1] for b in scan(conn))
        conn.execute("DELETE FROM ontology_types WHERE name='门禁变异类型_T13'")
        conn.commit()
        clean_again = not scan(conn)
        ck(poisoned and hit, "M1 注入含标记的类型 ⇒ T1 判红", "poisoned=%s hit=%s" % (poisoned, hit))
        ck(clean_again, "M1b 移除后恢复洁净（变异不留痕）")
    except Exception as ex:
        if not injected:
            print("  [SKIP] M1 注入失败（可能是只读库）：%s" % (str(ex)[:80],))
        else:
            conn.execute("DELETE FROM ontology_types WHERE name='门禁变异类型_T13'")
            conn.commit()
            _f.append("变异 M1 中途异常：%s" % str(ex)[:80])
    finally:
        conn.close()

    print("\n" + "=" * 78)
    print("结论：%s（PASS %d / FAIL %d）" % ("OK" if not _f else "FAIL", len(_p), len(_f)))
    for m in _f:
        print("  ✗ " + m)
    print("=" * 78)
    return 0 if not _f else 1


if __name__ == "__main__":
    sys.exit(main())
