"""只读探查：确认 plugins 表「内置」的权威判据（不改任何数据）。

背景：queries.plugin_dto 用 author_id==0 判 is_builtin，覆盖 74/77；
      studio_parts/shared._plugin_to_market_dto 用 author_name=='平台内置'，只覆盖 55。
两处口径不一致，需要第三方判据（manifest / namespace / source_ref）来定谁对。
"""
import json
import sqlite3
import sys
from collections import Counter

DB = r"C:\Users\gefei\WorkBuddy\2026-08-04-19-05-52\mbse_system\mbse.db"
conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row

rows = conn.execute(
    "SELECT plugin_id,name,type,scope,status,author_id,author_name,"
    "source_ref,namespace,manifest_json,created_at FROM plugins"
).fetchall()
rows = [dict(r) for r in rows]
live = [r for r in rows if r["status"] != "removed"]
print(f"总行数={len(rows)}  非removed={len(live)}")

def cut(key, rs):
    c = Counter()
    for r in rs:
        c[str(r.get(key))] += 1
    return dict(c.most_common())

print("\n=== 非removed 分布 ===")
for k in ("author_id", "author_name", "namespace", "source_ref", "scope", "status", "type"):
    print(f"{k:12s} {cut(k, live)}")

# 交叉：author_id==0 但 author_name != '平台内置' 的到底是些什么
print("\n=== A) author_id==0 且 author_name!='平台内置'（宽口径多标的条目）===")
odd = [r for r in live if (r["author_id"] or 0) == 0 and r["author_name"] != "平台内置"]
print(f"count={len(odd)}")
for r in odd:
    print(f"  [{r['type']:6s}] {r['plugin_id']:46s} author_name={r['author_name']!r} "
          f"ns={r['namespace']!r} src={r['source_ref']!r} scope={r['scope']}"[:180])

print("\n=== B) author_name=='平台内置' 的条目（窄口径标的）===")
narrow = [r for r in live if r["author_name"] == "平台内置"]
print(f"count={len(narrow)}")
for r in narrow[:8]:
    print(f"  [{r['type']:6s}] {r['plugin_id']:46s} aid={r['author_id']} src={r['source_ref']!r}"[:160])

# manifest 里是否藏着来源线索
print("\n=== C) manifest 顶层键的出现频次（找来源字段）===")
kc = Counter()
for r in live:
    try:
        m = json.loads(r["manifest_json"] or "{}")
    except Exception:
        continue
    for k in m:
        kc[k] += 1
print(dict(kc.most_common(30)))

print("\n=== D) manifest 中内置相关字段样本 ===")
for r in live[:3]:
    try:
        m = json.loads(r["manifest_json"] or "{}")
    except Exception:
        m = {}
    print(f"  {r['plugin_id']}: builtin={m.get('builtin')!r} source={m.get('source')!r} "
          f"origin={m.get('origin')!r} author={m.get('author')!r}")

print("\n=== E) 三个判据的数量对比（非removed=%d）===" % len(live))
p_wide = [r for r in live if (r["author_id"] or 0) == 0]
p_narrow = [r for r in live if r["author_name"] == "平台内置"]
print(f"  宽(author_id==0)              : {len(p_wide)}")
print(f"  窄(author_name=='平台内置')    : {len(p_narrow)}")
print(f"  差集(宽-窄)                   : {len(set(r['plugin_id'] for r in p_wide) - set(r['plugin_id'] for r in p_narrow))}")

print("\n=== F) 差集条目按 type 统计 ===")
diff = [r for r in p_wide if r["author_name"] != "平台内置"]
print(dict(Counter(r["type"] for r in diff)))
conn.close()
