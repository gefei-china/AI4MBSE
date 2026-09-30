"""只读探查：旧表 builtin 标记 vs plugins 表 author_name/plugin_id 前缀的对齐情况。"""
import sqlite3
from collections import Counter

DB = r"C:\Users\gefei\WorkBuddy\2026-08-04-19-05-52\mbse_system\mbse.db"
conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row

print("=== 旧表 builtin 分布 ===")
for t in ("skills", "tools", "agents", "mcp_servers"):
    try:
        tot = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        b = conn.execute(f"SELECT COUNT(*) FROM {t} WHERE builtin=1").fetchone()[0]
        print(f"  {t:14s} 总={tot:4d}  builtin=1 → {b}")
    except Exception as e:
        print(f"  {t:14s} ERR {e}")

print("\n=== 旧表 builtin=1 的名字（供与 plugins.plugin_id 比对）===")
for t, col in (("skills", "name"), ("tools", "name"), ("agents", "name"), ("mcp_servers", "name")):
    try:
        rows = conn.execute(f"SELECT {col} FROM {t} WHERE builtin=1").fetchall()
        ns = [r[0] for r in rows]
        print(f"  {t:14s} ({len(ns)}): {ns[:12]}")
    except Exception as e:
        print(f"  {t:14s} ERR {e}")

print("\n=== plugins.plugin_id 前缀分布（非removed）===")
rows = conn.execute(
    "SELECT plugin_id, author_name, type FROM plugins WHERE status!='removed'").fetchall()
pref = Counter()
for r in rows:
    pid = r["plugin_id"]
    # 取到第三段为止
    parts = pid.split(".")
    p = ".".join(parts[:3]) if len(parts) >= 3 else pid
    pref[p] += 1
for k, v in pref.most_common():
    print(f"  {k:34s} {v}")

print("\n=== 前缀 × author_name 交叉 ===")
cross = Counter()
for r in rows:
    parts = r["plugin_id"].split(".")
    p = ".".join(parts[:3]) if len(parts) >= 3 else r["plugin_id"]
    cross[(p, r["author_name"])] += 1
for (p, a), v in sorted(cross.items(), key=lambda x: -x[1]):
    print(f"  {p:34s} {a:8s} {v}")

print("\n=== 提议判据的自洽性检验 ===")
def is_builtin_row(r):
    """唯一判据候选：作者名 或 plugin_id 落在平台/legacy 命名空间。"""
    return (r["author_name"] == "平台内置") or r["plugin_id"].startswith("com.zhiyuan.legacy.")

for label, fn in [
    ("仅 author_name=='平台内置'", lambda r: r["author_name"] == "平台内置"),
    ("仅前缀 com.zhiyuan.legacy.", lambda r: r["plugin_id"].startswith("com.zhiyuan.legacy.")),
    ("两者取或（提议）", is_builtin_row),
    ("author_id==0（现状宽口径）", lambda r: False),
]:
    if label.startswith("author_id"):
        continue
    hits = [r for r in rows if fn(r)]
    print(f"  {label:28s} → {len(hits):3d} 条  types={dict(Counter(r['type'] for r in hits))}")
conn.close()
