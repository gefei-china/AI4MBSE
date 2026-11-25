"""design 重复 Agent 合并（2026-11-07 米爸第 14 轮第 3 问）——可复现、可回滚。

## 评估：保留谁
| 维度 | `design` (id=1035) | `工程建模方案设计Agent` (id=2) |
|---|---|---|
| name | **路由意图名**（tool_call_logs.intent=design 681 条） | 显示名当 name 用 |
| description | 157 字三段式（动作/话术/边界） | 92 字 |
| intent_keywords | 7 个专有词 | 空 |
| capabilities | 4 项 | 空 |
| 绑定 | 23（含 zhiyuan-dev-mcp + 智源MCP工具手册） | 21（是其真子集） |
| display_name | 相同 | 相同 |
| system_prompt | 相同（476 字，逐字） | 相同 |

⇒ **保留 `design`(1035)**，删除 id=2。理由：name 是路由/落库/历史日志的键，
   删它等于让 681 条历史意图变成孤儿；且保留方在描述/关键词/capabilities/绑定上全面更优。

## 引用迁移清单（改前逐列扫描全库得出，不凭印象）
| 表.列 | 命中 | 处理 |
|---|---|---|
| agents.name | 1 | 删行 |
| agents.display_name | 2 | **不动**：保留方 display_name 本就是这个值 |
| agent_tools (agent_id=2) | 21 | 全是 design 已有绑定的子集 ⇒ 直接删，不迁移 |
| agent_team_members.sub_agent_id=2 | 1 | 改成 1035；若 (main,1035) 已存在则删重复行 |
| tool_call_logs.intent / agent_name | 1 / 682 | **历史不改**（审计事实） |
| agent_memory.agent_id | 43 | 字符串值改成 'design' |
| agent_tasks.agent_id | 94 | 字符串值改成 'design' |
| hil_confirmations.agent_id | 8 | 字符串值改成 'design' |
| asset_change_logs.asset_name/new_value | 5 / 2 | **审计日志不改**（记录当时发生了什么） |
| plugins | `com.zhiyuan.legacy.agent.design` 指向 'design' | 不动 |
| core/capability_clusters.py | design 族 members 含旧名 | 移除，覆盖率门禁会校验 |
| data/bootstrap/pipeline_assets.json | agents 表 2 行 | 用 --apply 重新导出快照 |

⚠️ `services/tools/register_skills.py` / `seed_all` 不会重建 Agent（只有 skills/tools），
   但 **bootstrap_assets 会用快照 UPSERT 复活删掉的行** ⇒ 必须重新导出快照。

备份：docs/backup_20261107/mbse_before_merge_1342.db
"""
from __future__ import annotations

import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "mbse.db")
OLD_ID, OLD_NAME, KEEP_ID, KEEP_NAME = 2, "工程建模方案设计Agent", 1035, "design"


def _scan(c):
    """改前扫描：把所有引用旧名的表.列列出来（含字符串型 agent_id）。"""
    tabs = [r[0] for r in c.execute("select name from sqlite_master where type='table'")]
    out = []
    for t in tabs:
        cols = [r[1] for r in c.execute('PRAGMA table_info("%s")' % t)]
        for col in cols:
            try:
                n = c.execute('select count(*) from "%s" where "%s"=?' % (t, col),
                              (OLD_NAME,)).fetchone()[0]
                if n:
                    out.append((t, col, n))
            except Exception:
                pass
        # ⚠️ 首版只扫了 `agent_id` 整数列，**漏了 `sub_agent_id`**
        #   ⇒ 预演里看不到那 1 条团队关系，改后会变成悬空外键。
        #   凡"引用别表主键"的整数列都要显式扫，不能凭列名猜。
        for idcol in ("agent_id", "sub_agent_id", "main_agent_id", "target_id"):
            if idcol in cols:
                try:
                    n = c.execute('select count(*) from "%s" where "%s"=?' % (t, idcol),
                                  (OLD_ID,)).fetchone()[0]
                    if n:
                        out.append((t, idcol + "(int=%d)" % OLD_ID, n))
                except Exception:
                    pass
    return out


def main(apply: bool = False) -> int:
    c = sqlite3.connect(DB)
    print("=== 改前引用扫描 ===")
    for t, col, n in _scan(c):
        print("  %-28s %-18s %d" % (t, col, n))
    print("\n保留 %s(id=%d)，删除 %s(id=%d)" % (KEEP_NAME, KEEP_ID, OLD_NAME, OLD_ID))
    if not apply:
        print("（预演，未写库）")
        c.close()
        return 0

    # ① 团队关系：旧成员行改成 1035；若已有 (main,1035) 则删重复
    rows = c.execute("SELECT id, main_agent_id FROM agent_team_members "
                     "WHERE sub_agent_id=?", (OLD_ID,)).fetchall()
    for rid, mid in rows:
        dup = c.execute("SELECT 1 FROM agent_team_members WHERE main_agent_id=? "
                        "AND sub_agent_id=? AND id<>?", (mid, KEEP_ID, rid)).fetchone()
        if dup:
            c.execute("DELETE FROM agent_team_members WHERE id=?", (rid,))
            print("  团队行 %d：目标已有相同成员 ⇒ 删重复" % rid)
        else:
            c.execute("UPDATE agent_team_members SET sub_agent_id=? WHERE id=?",
                      (KEEP_ID, rid))
            print("  团队行 %d：sub_agent_id 2 → 1035" % rid)

    # ② 字符串型 agent_id（记忆/任务/确认单）：指向旧名的统一改到保留名
    for t in ("agent_memory", "agent_tasks", "hil_confirmations"):
        try:
            n = c.execute("UPDATE %s SET agent_id=? WHERE agent_id=?" % t,
                          (KEEP_NAME, OLD_NAME)).rowcount
            print("  %-22s agent_id 迁移 %d 行" % (t, n))
        except Exception as e:
            print("  %-22s 跳过（%s）" % (t, e))

    # ③ 绑定：旧 Agent 的 21 条全是保留方子集 ⇒ 直接删，不迁移
    old = {(r[0], r[1]) for r in c.execute(
        "SELECT tool_type, tool_name FROM agent_tools WHERE agent_id=?", (OLD_ID,))}
    keep = {(r[0], r[1]) for r in c.execute(
        "SELECT tool_type, tool_name FROM agent_tools WHERE agent_id=?", (KEEP_ID,))}
    extra = old - keep
    assert not extra, "旧 Agent 有独有绑定，需先迁移：%s" % extra
    c.execute("DELETE FROM agent_tools WHERE agent_id=?", (OLD_ID,))
    print("  agent_tools：删旧行 %d 条（独有绑定 0，无需迁移）" % len(old))

    # ④ 删 Agent 行
    c.execute("DELETE FROM agents WHERE id=?", (OLD_ID,))
    c.commit()

    # ⑤ 校验
    left = c.execute("SELECT COUNT(*) FROM agents WHERE name=?", (OLD_NAME,)).fetchone()[0]
    kept = c.execute("SELECT id,name,display_name,status FROM agents WHERE id=?",
                     (KEEP_ID,)).fetchone()
    print("\n  旧名残留 %d 行；保留行 %s" % (left, tuple(kept)))
    assert left == 0 and kept and kept[1] == KEEP_NAME
    # ⚠️ 下面 4 类命中是**刻意保留**，不是残留：
    #   · agents.display_name —— 保留方的显示名本来就是这个值（用户可见名不变）
    #   · tool_call_logs / asset_change_logs —— 历史审计事实，改了等于篡改日志
    #   首版把它们一律打印成「<< 残留」，是判据自身的假警报。
    EXPECTED = {
        ("agents", "display_name"), ("tool_call_logs", "intent"),
        ("tool_call_logs", "agent_name"), ("asset_change_logs", "asset_name"),
        ("asset_change_logs", "new_value"),
    }
    print("=== 改后引用扫描 ===")
    bad = []
    for t, col, n in _scan(c):
        if (t, col) in EXPECTED:
            print("  %-28s %-18s %d  （预期保留：显示名/历史审计）" % (t, col, n))
        else:
            print("  %-28s %-18s %d  << 真残留" % (t, col, n))
            bad.append((t, col, n))
    assert not bad, "仍有未迁移引用：%s" % bad
    c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main("--apply" in sys.argv))
