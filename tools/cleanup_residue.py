# -*- coding: utf-8 -*-
"""清理运行残留与悬空引用（默认 dry-run，`--apply` 才写库）。

## 清理什么、为什么、为什么不动别的

| 类别 | 判定 | 动作 | 为什么这么处理 |
|---|---|---|---|
| 探针插件 | `plugins.name` 含 probe/探针 且仍然 `published` | → `removed` | 探针是门禁造的临时插件；留在 published 会让插件市场出现冒牌条目。**不物理删行**：`plugin_versions` 有审计价值 |
| 悬空工程指针 | `conversations/sysml_versions.project_id` 指向**不存在的工程** | → `''`（空=无工程会话，合法态） | 悬空指针会让写回闸报 `PROJECT_ORPHANED` 且**用户无法自救**；空值的语义是"没归入工程"，提示可执行。**保留原值到 audit_logs**，不静默丢信息 |
| 测试会话 | 标题匹配 `verify_*` / `E2E-*` | **只报告，不删** | 删会话级联 messages/artifacts/sysml_versions/tool_call_logs，不可逆；且它们是历史验证证据。要删必须人显式加 `--include-conversations` |
| 探针工具行 | `tools.status='disabled'` 且无执行器 | **只报告，不改** | 停用即"模型看不到"，已是安全态；删行会让历史 `tool_call_logs` 失去关联 |

判据：脚本自己校验"清理后没有新增悬空引用、没有把 active 资产降级"。
"""
import argparse
import json
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:      # 脚本从 tools/ 下运行 ⇒ 仓库根必须可 import（core/database）
    sys.path.insert(0, ROOT)
DB = os.environ.get("MBSE_DB_PATH") or os.path.join(ROOT, "mbse.db")

PROBE_LIKE = ("%probe%", "%探针%")


def _audit(user: str, event: str, detail: str, conn) -> None:
    """走官方审计（audit_logs 有 prev_hash/hash 哈希链，**不能手工 INSERT**）。

    ⚠️ 手工写会得到 hash 为空的行，后续链校验会把它们判成篡改。
    """
    from core.audit import audit as _a
    _a(user, event, detail, conn=conn)


def _q(c, sql, args=()):
    return [dict(r) for r in c.execute(sql, args)]


def survey(c) -> dict:
    out = {}
    out["probe_plugins"] = _q(
        c, "SELECT id, name, status FROM plugins WHERE (name LIKE ? OR name LIKE ?) "
           "AND status <> 'removed'", PROBE_LIKE)
    out["orphan_convs"] = _q(
        c, "SELECT cv.id, cv.title, cv.project_id FROM conversations cv "
           "LEFT JOIN projects p ON p.id=cv.project_id "
           "WHERE coalesce(trim(cv.project_id),'')<>'' AND p.id IS NULL")
    out["orphan_versions"] = _q(
        c, "SELECT v.id, v.project_id FROM sysml_versions v "
           "LEFT JOIN projects p ON p.id=v.project_id "
           "WHERE coalesce(trim(v.project_id),'')<>'' AND p.id IS NULL")
    out["test_convs"] = _q(
        c, "SELECT id, title FROM conversations WHERE title LIKE 'verify_%' "
           "OR title LIKE 'E2E-%' ORDER BY id DESC")
    out["disabled_tools"] = _q(
        c, "SELECT name FROM tools WHERE status='disabled' ORDER BY name")
    return out


def apply(c, data: dict, include_conversations: bool) -> list:
    log = []
    for p in data["probe_plugins"]:
        c.execute("UPDATE plugins SET status='removed' WHERE id=?", (p["id"],))
        log.append(f"插件 {p['name']}(id={p['id']}) {p['status']}→removed")
    for v in data["orphan_versions"]:
        _audit("cleanup", "cleanup_orphan_project",
               json.dumps({"table": "sysml_versions", "id": v["id"],
                           "stale_project_id": v["project_id"]}, ensure_ascii=False), c)
        c.execute("UPDATE sysml_versions SET project_id='' WHERE id=?", (v["id"],))
        log.append(f"版本 v{v['id']} 悬空归属 {v['project_id']} → 空")
    for cv in data["orphan_convs"]:
        _audit("cleanup", "cleanup_orphan_project",
               json.dumps({"table": "conversations", "id": cv["id"],
                           "stale_project_id": cv["project_id"],
                           "title": cv["title"]}, ensure_ascii=False), c)
        c.execute("UPDATE conversations SET project_id='' WHERE id=?", (cv["id"],))
        log.append(f"会话 {cv['id']}「{cv['title'][:18]}」悬空归属 → 空")
    if include_conversations:
        for cv in data["test_convs"]:
            c.execute("DELETE FROM messages WHERE conversation_id=?", (cv["id"],))
            c.execute("DELETE FROM conversations WHERE id=?", (cv["id"],))
            log.append(f"删除测试会话 {cv['id']}「{cv['title'][:18]}」及其消息")
    return log


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写库（默认只报告）")
    ap.add_argument("--include-conversations", action="store_true",
                    help="同时删除测试会话（**不可逆**，默认不删）")
    args = ap.parse_args()
    if not os.path.exists(DB):
        print(f"[FAIL] 数据库不存在: {DB}")
        return 1
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    data = survey(c)
    print("=" * 68)
    print("运行残留盘点（%s）" % os.path.basename(DB))
    print("=" * 68)
    print(f"  探针插件仍 published      ：{len(data['probe_plugins'])} {[p['name'] for p in data['probe_plugins']]}")
    print(f"  版本悬空工程指针          ：{len(data['orphan_versions'])}")
    print(f"  会话悬空工程指针          ：{len(data['orphan_convs'])}")
    print(f"  测试会话（仅报告）        ：{len(data['test_convs'])}")
    print(f"  已停用工具（仅报告）      ：{len(data['disabled_tools'])}")

    before = {
        "agents_active": c.execute("SELECT COUNT(*) FROM agents WHERE status='active'").fetchone()[0],
        "tools_active": c.execute("SELECT COUNT(*) FROM tools WHERE status='active'").fetchone()[0],
        "skills_pub": c.execute("SELECT COUNT(*) FROM skills WHERE status='published'").fetchone()[0],
    }
    if not args.apply:
        print("\n[dry-run] 未写库。加 --apply 执行（悬空指针会被置空并写 audit_logs）。")
        c.close()
        return 0

    log = apply(c, data, args.include_conversations)
    c.commit()
    after = {
        "agents_active": c.execute("SELECT COUNT(*) FROM agents WHERE status='active'").fetchone()[0],
        "tools_active": c.execute("SELECT COUNT(*) FROM tools WHERE status='active'").fetchone()[0],
        "skills_pub": c.execute("SELECT COUNT(*) FROM skills WHERE status='published'").fetchone()[0],
    }
    # 判据：不得降级在役资产
    same = all(before[k] == after[k] for k in before)
    dangling = survey(c)
    no_dangling = not dangling["orphan_versions"] and not dangling["orphan_convs"]
    print("\n执行结果：")
    for line in log:
        print("  ·", line)
    print(f"\n  [{'PASS' if same else 'FAIL'}] 在役资产未被降级 {before} == {after}")
    print(f"  [{'PASS' if no_dangling else 'FAIL'}] 清理后无悬空工程指针")
    c.close()
    return 0 if (same and no_dangling) else 1


if __name__ == "__main__":
    sys.exit(main())
