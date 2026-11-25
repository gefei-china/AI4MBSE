"""技能去重精简（2026-11-07 米爸第 14 轮第 2 问）——可复现、可回滚。

## 评估结论（真库逐份比对，不凭印象）
「行业调研」5 份：
  · id 49/123/128 = published，正文**逐字相同**（md5 e7bd7d0b95，1040 字，v1.0）
  · id 131 = draft_revision v2（1578 字）、id 134 = draft_revision v3（1722 字）
  · **最优 = v3(134)**：在 v1.0 基础上补了「执行流程（强制，防止无输出）」6 条铁律
    （触发即响应 / 输入缺失并行处理 / 路径缺失 / 工具失败降级 / 分段交付 / 完成回执），
    还有默认落盘路径与「未成功落盘不得声称已生成文件」——正是本仓反复踩过的
    「静默无输出」「带兜底把错误变成不可见」的对策。
  ⇒ 保留 id=49 这一行（最早 published、builtin=1、有 package_path），
    **把 v3 正文发布过去**（status=published / version=v1.1 / triggers 取 v3 的 10 个词），
    删除 123/128/131/134。

「Requirement_Analysis_Pre-check」4 份：全部 disabled，正文 154~266 字、
  ai_deposit/ai_improve 碎片。最优 = id=132（v3，266 字，含「不得无输出」）。
  ⇒ 保留 132（状态仍 disabled，不擅自发布），删除 70/85/135。
  ⚠️ 绑定按**技能名**存，同名仍剩 1 份 ⇒ 绑定保持有效，一条都不清
     （这正是 2026-11-06 修过的「按 name 清绑定误删正式版」陷阱）。

## 两处「重启复活」防线（上一轮踩过，必须同步）
1. `services/tools/register_skills.py` 内置包「行业调研」的 skill_md 是 1040 字 v1.0；
   启动时会更新内置行。虽有 `_keep_content` 保护正文，但**版本/触发词/描述**仍可能被写回。
   ⇒ 同步把内置包正文换成 v3，并把 triggers 改为 v3 的 10 个词。
2. `data/bootstrap/pipeline_assets.json` 快照里有 1 行「行业调研」（v1.0 1040 字）。
   空库自举会用它建行 ⇒ 同步更新快照（用 --apply 重新导出即可覆盖）。

备份：docs/backup_20261107/mbse_before_merge_1342.db（改前完整库）
"""
from __future__ import annotations

import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "mbse.db")

INDUSTRY_KEEP = 49
INDUSTRY_BEST = 134
INDUSTRY_DROP = (123, 128, 131, 134)
PRECHECK_KEEP = 132
PRECHECK_DROP = (70, 85, 135)


def main(apply: bool = False) -> int:
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    best = c.execute("SELECT content, triggers, version FROM skills WHERE id=?",
                     (INDUSTRY_BEST,)).fetchone()
    assert best, "缺 v3 正文"
    trig = best["triggers"]
    print("v3 正文 %d 字 / triggers=%s" % (len(best["content"]), trig[:90]))
    print("保留行业调研 id=%d，删除 %s" % (INDUSTRY_KEEP, INDUSTRY_DROP))
    print("保留 Pre-check id=%d，删除 %s" % (PRECHECK_KEEP, PRECHECK_DROP))
    if not apply:
        print("（预演，未写库）")
        c.close()
        return 0

    # ① 把最优正文发布到保留行
    c.execute("UPDATE skills SET content=?, triggers=?, version='v1.1', "
              "status='published', updated_at=CURRENT_TIMESTAMP WHERE id=?",
              (best["content"], trig, INDUSTRY_KEEP))
    # ② 删重复行；删前逐组确认「删完同名仍有 1 份」⇒ 绑定不清
    for group, drops, keep in (("行业调研", INDUSTRY_DROP, INDUSTRY_KEEP),
                               ("Requirement_Analysis_Pre-check", PRECHECK_DROP, PRECHECK_KEEP)):
        remain_before = c.execute("SELECT COUNT(*) FROM skills WHERE name=?", (group,)).fetchone()[0]
        for sid in drops:
            c.execute("DELETE FROM skills WHERE id=?", (sid,))
        remain = c.execute("SELECT COUNT(*) FROM skills WHERE name=?", (group,)).fetchone()[0]
        assert remain == 1, "%s 删完应剩 1 份，实际 %d" % (group, remain)
        binds = c.execute("SELECT COUNT(*) FROM agent_tools WHERE tool_type='skill' "
                          "AND tool_name=?", (group,)).fetchone()[0]
        print("  %-32s %d→%d 行，绑定保留 %d 条" % (group, remain_before, remain, binds))
    c.commit()

    # ③ 校验
    r = c.execute("SELECT id,status,version,length(content) FROM skills "
                  "WHERE name='行业调研'").fetchone()
    print("  行业调研终态:", tuple(r))
    assert r["id"] == INDUSTRY_KEEP and r["status"] == "published" and r[3] >= 1700
    c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main("--apply" in sys.argv))
