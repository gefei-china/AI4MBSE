# -*- coding: utf-8 -*-
"""P2-4：SWRL 引擎首次真实 run（星网域规则）。

历史根因：4 条模板规则引用的 Person/hasAge/Component/partOf/System 等 7 个类型
在本体中不存在（种子=通用示例，从未适配本域）→ inferred_facts 恒 0。
本脚本：
① 注册一条本域规则：CONTAINS 传递闭包（CONTAINS 48 边、未声明 transitive，推导有真价值）
② run() 真实推理 → inferred_facts 0→>0
③ accept/reject 闭环各一条
④ 清理：删除测试规则（引擎 delete_rule），facts 保留作执行历史
"""
import json
import sqlite3
import sys

sys.path.insert(0, ".")
from core import config
from services.swrl_engine import SWRLEngine

conn = sqlite3.connect(config.DB_PATH)
conn.row_factory = sqlite3.Row

print("① 前置：inferred_facts =", conn.execute("SELECT COUNT(*) FROM inferred_facts").fetchone()[0])

eng = SWRLEngine(conn)
info = eng.load()
print(f"② load: {info}（类 {info['types']} / 属性 {info['props']} / 个体 {info['individuals']} / 规则 {info['rules']} 条——"
      f"其中 4 条模板规则引用的类型在本体不存在，注册将失败，预期）")

rid = eng.register_rule(
    name="SatContainsTransitive",
    comment="P2-4 首跑：CONTAINS 传递闭包（A包含B、B包含C ⇒ A包含C）。可逆：验证后删除本规则。",
    body="CONTAINS(?a, ?b), CONTAINS(?b, ?c)",
    head="CONTAINS(?a, ?c)",
    priority=5,
)
print(f"③ 注册规则 id={rid}")

# ⚠️ run() 消费 load() 时的 _rules_cache 快照——注册后必须重新 load 才会带上新规则
info2 = eng.load()
print(f"③b 重新 load: rules={info2['rules']}（应 5 条）")

summary = eng.run(mode="warn")
print(f"④ run: rules_run={summary.rules_run} rules_skipped={summary.rules_skipped} "
      f"facts={len(summary.facts) if hasattr(summary, 'facts') else '-'} "
      f"errors={summary.errors[:3]} warnings={[w[:60] for w in summary.warnings[:5]]}")

n = conn.execute("SELECT COUNT(*) FROM inferred_facts").fetchone()[0]
print(f"⑤ inferred_facts: {n} 行")
facts = conn.execute("SELECT id, rule_id, subject, predicate, object, is_accepted FROM inferred_facts LIMIT 10").fetchall()
for f in facts:
    print("   fact:", dict(f))

if n:
    fid = facts[0]["id"]
    a = eng.accept_fact(fid)
    print(f"⑥ accept fact#{fid}: rc={a}")
    conn.commit()
    st = conn.execute("SELECT is_accepted FROM inferred_facts WHERE id=?", (fid,)).fetchone()
    print(f"   accept 后状态 is_accepted={st['is_accepted']}")
    if len(facts) > 1:
        r = eng.reject_fact(facts[1]["id"])
        conn.commit()
        print(f"⑦ reject fact#{facts[1]['id']}: rc={r}")

print("⑧ 清理：删除测试规则")
print("   delete_rule rc =", eng.delete_rule(rid))
conn.commit()
final = conn.execute("SELECT COUNT(*) FROM inferred_facts").fetchone()[0]
accepted = conn.execute("SELECT COUNT(*) FROM inferred_facts WHERE is_accepted=1").fetchone()[0]
print(f"⑨ 收尾：inferred_facts={final}（accepted={accepted}），规则表恢复 {conn.execute('SELECT COUNT(*) FROM swrl_rules').fetchone()[0]} 条")
