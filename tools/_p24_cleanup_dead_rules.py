# -*- coding: utf-8 -*-
"""P2-4 遗留①：清理 4 条死规则（引用 7 个本体不存在的类型）+ 注册本域正式规则。
先备份到 tools/_swrl_dead_rules_backup.json（可恢复），删除走 API 留痕。"""
import io
import json
import sqlite3

from fastapi.testclient import TestClient
from main import app

cli = TestClient(app)
conn = sqlite3.connect("file:mbse.db?mode=ro", uri=True)
conn.row_factory = sqlite3.Row

dead = [dict(r) for r in conn.execute("SELECT * FROM swrl_rules ORDER BY id")]
io.open("tools/_swrl_dead_rules_backup.json", "w", encoding="utf-8").write(
    json.dumps(dead, ensure_ascii=False, indent=1))
print(f"备份 {len(dead)} 条死规则 → tools/_swrl_dead_rules_backup.json")
for r in dead:
    print(f"   #{r['id']} {r['name']} body={r['body'][:60]}")

# 删除（走 API）
for r in dead:
    d = cli.delete(f"/api/swrl/rules/{r['id']}")
    print(f"DELETE #{r['id']}: {d.json()}")

# 注册本域正式规则：CONTAINS 传递闭包（P2-4 首跑已验证可推出 13 条事实）
c = cli.post("/api/swrl/rules", json={
    "name": "SatContainsTransitive",
    "comment": "CONTAINS 传递闭包：A包含B、B包含C ⇒ A包含C（CONTAINS 未声明 transitive，"
               "SWRL 补齐传递推导；P2-4 首跑实测可推出 13 条二级包含事实）",
    "body": "CONTAINS(?a, ?b), CONTAINS(?b, ?c)",
    "head": "CONTAINS(?a, ?c)",
    "priority": 5,
    "is_active": True})
print("注册 SatContainsTransitive:", c.json())

lst = cli.get("/api/swrl/rules").json()
print("清理后规则表:", [(x["id"], x["name"], x["is_active"]) for x in lst["rules"]])
