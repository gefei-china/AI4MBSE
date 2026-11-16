# -*- coding: utf-8 -*-
"""技能正文升级：`sysml_requirement_structuring_guide` v1.0 → v1.1（加"落库"这一步）。

## 为什么改
v1.0 的步骤止于"生成需求视图骨架并过校验"，**没有落库**。后果（2026-11-07 实测）：
条目只活在对话里 —— 库里没有任何需求实体，五元素追溯链
（Need→Requirement→Function→Component→Verification）的第一环是空的。
新工具 `requirement_commit` 已就位，正文必须同步，否则模型拿不到"要落库"的指令
（说明书写什么，模型才做什么；与 2026-11-07 修 `sysml_stdlib_meta` 同一口径）。

判据：正文含 `requirement_commit`、version 升到 v1.1、可重复执行（已升过则跳过）。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

NEW_STEP = """5. **落库（有后果的动作，需人工确认）**：条目要成为工程资产才谈得上追溯 ——
   调 `requirement_commit`，把上一步的 `items[]`/`gaps[]` 原样传入（也可只传 `text` 让它自己条目化）。
   幂等：**同一条需求重复提交不会新增**（按归一化文本指纹判等），可以放心重跑。
   返回里的 `REQ-*` 编号**项目内稳定不变**，后续 refine/derive/satisfy 一律引用它。
   - 会话已归入工程 ⇒ 自动落到该工程；未归入 ⇒ 落到"无工程"，此时应提示用户先归入项目。
   - 落库失败时**如实说"未能入库"**，不要声称已入库（工具会返回失败原因，照抄即可）。

## 判据"""
OLD_TAIL = "## 判据"

JUDGE_ADD = """
- 落库后 `requirement_items` 里能按工程查到这些条目；`REQ-*` 与 `req_view_code` 里的 `reqId` **必须一致**。"""


def main() -> int:
    from database import get_db
    conn = get_db()
    try:
        row = conn.execute("SELECT id, version, content FROM skills WHERE name=?",
                           ("sysml_requirement_structuring_guide",)).fetchone()
        if not row:
            print("[FAIL] 技能不存在")
            return 1
        if "requirement_commit" in (row["content"] or ""):
            print("[OK] 已含 requirement_commit，无需改动（幂等）")
            return 0
        body = row["content"]
        if OLD_TAIL not in body:
            print("[FAIL] 找不到『## 判据』锚点，正文结构已变，请手工核对")
            return 1
        body = body.replace("version: v1.0", "version: v1.1", 1)
        body = body.replace(OLD_TAIL, NEW_STEP, 1)
        # 判据段补一条"编号一致"
        idx = body.find("## 失败处理")
        if idx > 0:
            body = body[:idx] + JUDGE_ADD.strip() + "\n\n" + body[idx:]
        conn.execute("UPDATE skills SET content=?, version='v1.1', updated_at=CURRENT_TIMESTAMP "
                     "WHERE id=?", (body, row["id"]))
        conn.execute("INSERT INTO asset_change_logs (asset_type, asset_id, asset_name, field, "
                     "old_value, new_value, version_from, version_to, changed_by, source) "
                     "VALUES ('skill', ?, 'sysml_requirement_structuring_guide', 'content', ?, ?, "
                     "'v1.0','v1.1','requirement-fix','migration')",
                     (str(row["id"]), (row["content"] or "")[:4000], body[:4000]))
        conn.commit()
        print(f"[OK] 已升级 v1.0→v1.1，正文 {len(row['content'])}→{len(body)} 字")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
