# -*- coding: utf-8 -*-
"""一次性修复：把**已提交但从未落地**的异步导出发任务补齐成产物。

## 背景（真实事故）

会话 9629 用户 4 次要求「获取工程 V2 代码」，助手 4 次失败，且两次谎称
"已存入产物库"（`artifacts WHERE conversation_id=9629` 实测 **0 条**）。
真因：**异步任务没人收尾** —— 提交导出后只查 1 次状态就放弃，taskId 不跨轮留存；
而服务端那两次导出**其实早已 COMPLETED**（8 小时后实测 100%）。
本轮新增 `agent/async_jobs.py`（提交即托管 + 跨轮续跑），本脚本负责把
**历史遗留**的任务按同一套机制补做一次：查状态 → 分页读取 → 归档成产物。

## 纪律

- 复用**生产代码**（`resume_jobs` / `_archive_artifacts`），不另写一套逻辑；
- 文档来源是**真实 MCP 调用**（真 STATUS/READ），不是构造；
- 幂等：`_archive_artifacts` 自带 `(conversation, message, kind, title)` 去重；
- `--dry-run` 先看清单。

用法：
    python tools/verify/repair_pending_jobs.py --dry-run
    python tools/verify/repair_pending_jobs.py
"""
import json
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
DB = os.path.join(ROOT, "mbse.db")

from agent import async_jobs as aj  # noqa: E402


class RealPipe:
    """最小 pipe：把 `_exec_tool_call` 接到**真实 MCP**（复用生产 dispatch 语义）。"""

    def __init__(self, cli):
        self.cli = cli
        self._tool_sysml_docs = []
        self._tool_sysml_docs_seen = set()

    def _exec_tool_call(self, name, args):
        return self.cli.call_tool(name, args)


def main():
    dry = "--dry-run" in sys.argv
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    aj.ensure_table(conn)

    # 哪些会话还有未收尾任务
    convs = [r["conversation_id"] for r in conn.execute(
        "SELECT DISTINCT conversation_id FROM %s WHERE artifact_id=0" % aj.TABLE)]
    print("[范围] 有未收尾任务的会话：%s" % convs)

    from mcp_client import MCPClient
    row = conn.execute("SELECT endpoint,transport,headers FROM mcp_servers "
                       "WHERE name='zhiyuan-dev-mcp'").fetchone()
    if not row:
        print("!! 找不到 zhiyuan-dev-mcp，退出")
        return
    cli = MCPClient(row["endpoint"], row["transport"] or "sse",
                    headers=json.loads(row["headers"] or "{}"))

    from agent.utils import _archive_artifacts

    for conv in convs:
        jobs = aj.open_jobs(conn, conv)
        if not jobs:
            continue
        print("\n=== conv %s：待处理 %d 个任务 ===" % (conv, len(jobs)))
        for j in jobs:
            print("   task=%s status=%s progress=%s file=%s"
                  % (j["task_id"], j["status"], j["progress"], j["file_name"] or "-"))
        if dry:
            print("   [干跑] 跳过实际拉取")
            continue

        pipe = RealPipe(cli)
        r = aj.resume_jobs(pipe, conv, conn=conn)
        for n in r["notes"]:
            print("   · %s" % n)
        if not r["docs"]:
            print("   （本轮无可归档文档）")
            conn.commit()
            continue

        # 挂到该会话最后一条 assistant 消息上（那正是"本该交付"的那一轮）
        m = conn.execute("SELECT id, card_data, content, "
                         "COALESCE((SELECT intent FROM conversations WHERE id=?),'') AS it "
                         "FROM messages WHERE conversation_id=? AND role='assistant' "
                         "ORDER BY id DESC LIMIT 1", (conv, conv)).fetchone()
        if not m:
            print("   !! 该会话没有 assistant 消息，跳过归档")
            continue
        _archive_artifacts(conn, conv, m["id"], m["card_data"] or "{}",
                           m["content"] or "", m["it"] or "",
                           tool_docs=r["docs"])
        # 回填 artifact_id：**按文件名逐个对应**（此前按会话一把刷会把两个任务
        # 都指向同一条产物，虽然都是终态不影响续跑，但账目不清）。
        for j in jobs:
            _fn = (j.get("file_name") or "").strip()
            if not _fn:
                continue
            ar = conn.execute(
                "SELECT id FROM artifacts WHERE conversation_id=? AND title=? "
                "ORDER BY id DESC LIMIT 1", (conv, _fn)).fetchone()
            if ar:
                conn.execute("UPDATE %s SET artifact_id=? WHERE conversation_id=? AND task_id=?"
                             % aj.TABLE, (ar["id"], conv, j["task_id"]))
        conn.commit()
        got = conn.execute("SELECT id,kind,title,length(preview_content) n FROM artifacts "
                           "WHERE conversation_id=? ORDER BY id", (conv,)).fetchall()
        print("   ✅ 该会话产物现有 %d 条：" % len(got))
        for g in got:
            print("      id=%s kind=%s %s (%s 字符)" % (g["id"], g["kind"], g["title"], g["n"]))

    print("\n[表状态]")
    for r in conn.execute("SELECT conversation_id, task_id, status, artifact_id FROM %s "
                          "ORDER BY id" % aj.TABLE):
        print("   conv=%s task=%s status=%s artifact_id=%s"
              % (r["conversation_id"], r["task_id"], r["status"], r["artifact_id"]))
    conn.close()


if __name__ == "__main__":
    main()
