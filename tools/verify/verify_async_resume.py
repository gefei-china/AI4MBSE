# -*- coding: utf-8 -*-
"""verify_async_resume — 门禁：异步外呼任务必须**跨轮续跑**，不能靠模型记 taskId。

## 为什么需要这道门禁（会话 9629，连续 4 轮取不到 V2 代码）

用户 4 次要求「获取工程 V2 代码」全部失败。实测真因不是取数逻辑写错，而是
**异步任务的责任没人负**：

| 轮次 | 实际发生（`tool_call_logs` 实证） |
|---|---|
| msg 4890 | 提交导出 → taskId → **只查 1 次** STATUS（progress=10）就收尾，且谎称"连续查询十余次" |
| msg 4892 | 下一轮**从零重来**：重查工程/分支/版本、重新 PREVIEW/EXPORT（taskId 从不跨轮留存）|
| 8 小时后实测 | 两个 taskId **全部 COMPLETED / 100%** —— 任务本来会成功，只是"没人回来取" |

## 本门禁锁四件事

① **提交即托管**：工具结果含 taskId+status ⇒ `detect_job` 能识别并被 `record` 落库；
② **跨轮续跑**：下一轮 `resume_jobs` 能看见它；RUNNING ⇒ 明确告知"别重新导出"；
   COMPLETED ⇒ 自动分页读取并把文档交给 `_tool_sysml_docs`（persist 即归档）；
③ **已完成但未归档的任务不能永久跳过**（9629 那个 taskId 正是此形态）；
④ **读不出完整文件 ⇒ 标记 UNREADABLE 终态**，不每轮空跑、也不许谎称已归档。

含**变异自证**：把"待处理判据"改回 `status != COMPLETED`（漏掉已完成未归档）后，
门禁必须变红。

用法：
    python tools/verify/verify_async_resume.py           # 离线（假 pipe，秒级）
    python tools/verify/verify_async_resume.py --live    # 额外打真 MCP 校验拼装字节数
"""
import json
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from agent import async_jobs as aj  # noqa: E402
from agent.utils import accumulate_paged_doc, collect_tool_sysml_doc  # noqa: E402

FAILS = []


def ok(name, cond, detail=""):
    print("  [%s] %s%s" % ("OK  " if cond else "FAIL", name, ("  ← " + detail) if detail else ""))
    if not cond:
        FAILS.append(name)


# ── 造一个"真协议形态"的分页文件（中文 → 字符数≠字节数，压住字节偏移这个坑）──
FULL = "package 'demo' {\n" + "".join(
    "  package 'p%d' { attribute 'a%d' : Real; }\n" % (i, i) for i in range(1, 40)) + "}\n"
RAW = FULL.encode("utf-8")
SIZE = len(RAW)
_N = len(FULL)
_CH = [0, _N // 3, (2 * _N) // 3, _N]
TEXT = [FULL[_CH[i]:_CH[i + 1]] for i in range(3)]
OFF = [0]
for _t in TEXT:
    OFF.append(OFF[-1] + len(_t.encode("utf-8")))


def read_page(i):
    seg = TEXT[i]
    return json.dumps({
        "taskId": "T-DEMO", "fileName": "demo.sysml", "sizeBytes": str(SIZE),
        "offset": OFF[i], "bytesRead": len(seg.encode("utf-8")),
        "nextOffset": None if i == len(TEXT) - 1 else OFF[i + 1],
        "eof": i == len(TEXT) - 1, "completeFile": False, "content": seg,
    }, ensure_ascii=False)


class FakePipe:
    """假 pipe：`_exec_tool_call` 返回预置结果（离线、可断言），不联网。"""

    def __init__(self, script):
        self.script = script      # {(name, action): [结果...]} 按序弹出
        self.calls = []
        self._tool_sysml_docs = []
        self._tool_sysml_docs_seen = set()

    def _exec_tool_call(self, name, args):
        act = (args or {}).get("action") or "?"
        self.calls.append((name, act, (args or {}).get("offset")))
        key = (name, act)
        q = self.script.get(key) or []
        res = q.pop(0) if q else '{"status":"UNKNOWN"}'
        return {"ok": True, "result": res}


def prepare_db(tmpconv, task_id, status, progress=0):
    """在**真库临时副本**上验证，避免污染生产。"""
    src = os.path.join(ROOT, "mbse.db")
    tmp = os.path.join(ROOT, "tmp", "_async_resume_test.db")
    os.makedirs(os.path.dirname(tmp), exist_ok=True)
    for suf in ("", "-wal", "-shm"):
        p = tmp + suf
        if os.path.exists(p):
            os.remove(p)
    s = sqlite3.connect(src)
    try:
        s.execute("PRAGMA wal_checkpoint(PASSIVE)")
    except Exception:
        pass
    s.close()
    import shutil
    shutil.copyfile(src, tmp)
    for suf in ("-wal", "-shm"):
        if os.path.exists(src + suf):
            shutil.copyfile(src + suf, tmp + suf)
    c = sqlite3.connect(tmp)
    c.row_factory = sqlite3.Row
    aj.ensure_table(c)
    c.execute("DELETE FROM %s WHERE conversation_id=?" % aj.TABLE, (tmpconv,))
    aj.record(c, tmpconv, "", "export_sysml_v2",
              {"task_id": task_id, "status": status, "progress": progress,
               "file_name": "demo.sysml", "meta": {}})
    c.commit()
    return tmp, c


def main():
    print("=== ① 提交即托管：能识别真协议结果 ===")
    real_submit = json.dumps({"status": "SUBMITTED", "message": "请使用 action=STATUS 与 taskId 查询结果",
                              "taskId": "2114956963811217410"})
    j = aj.detect_job(real_submit)
    ok("SUBMITTED+taskId 被识别", bool(j) and j["task_id"] == "2114956963811217410",
       str(j))
    real_running = json.dumps({"taskId": "X1", "status": "RUNNING", "progress": 10,
                               "fileName": "a.sysml"})
    j2 = aj.detect_job(real_running)
    ok("RUNNING 被识别且带进度", bool(j2) and j2["progress"] == 10, str(j2))
    ok("普通工具结果不被误判", aj.detect_job('{"items":[],"total":"0"}') is None)
    ok("非 JSON 不被误判", aj.detect_job("package Foo {}") is None)

    print("\n=== ② 跨轮续跑：RUNNING ⇒ 明确说'别重新导出' ===")
    tmp, c = prepare_db(90001, "T-RUN", "RUNNING", 10)
    pipe = FakePipe({("export_sysml_v2", "STATUS"):
                     [json.dumps({"taskId": "T-RUN", "status": "RUNNING", "progress": 10})]})
    r = aj.resume_jobs(pipe, 90001, conn=c)
    ok("未完成 ⇒ 不产出文档", not r["docs"])
    ok("归入 running", r["running"] == ["T-RUN"], str(r["running"]))
    note = " ".join(r["notes"])
    ok("明确要求本轮不要重新导出", ("不要重新" in note) or ("别重新" in note), note[:90])
    ok("注入了进度事实", "10%" in note, note[:90])
    c.close()

    print("\n=== ③ 跨轮续跑：COMPLETED ⇒ 自动分页读取并交给归档 ===")
    tmp, c = prepare_db(90002, "T-OK", "COMPLETED", 100)
    pipe = FakePipe({
        ("export_sysml_v2", "STATUS"):
            [json.dumps({"taskId": "T-OK", "status": "COMPLETED", "progress": 100,
                         "fileName": "demo.sysml", "sizeBytes": str(SIZE)})],
        ("export_sysml_v2", "READ"): [read_page(0), read_page(1), read_page(2)],
    })
    r = aj.resume_jobs(pipe, 90002, conn=c)
    ok("产出文档", len(r["docs"]) == 1, str(len(r["docs"])))
    if r["docs"]:
        d = r["docs"][0]
        ok("拼出全文（字节数==sizeBytes）", len(d["text"].encode("utf-8")) == SIZE,
           "%d vs %d" % (len(d["text"].encode("utf-8")), SIZE))
        ok("内容与原文一致", d["text"] == FULL)
    ok("文档已进 pipe._tool_sysml_docs（persist 即归档）",
       len(pipe._tool_sysml_docs) == 1)
    ok("读了 3 段（按 nextOffset 推进）",
       len([x for x in pipe.calls if x[1] == "READ"]) == 3,
       str([x for x in pipe.calls if x[1] == "READ"]))
    ok("告知模型'已完成、别再导出'", "已完成" in " ".join(r["notes"]))
    row = c.execute("SELECT status FROM %s WHERE conversation_id=?" % aj.TABLE,
                    (90002,)).fetchone()
    ok("状态落库为 COMPLETED", (row["status"] if row else "") == "COMPLETED", str(row and row["status"]))
    c.close()

    print("\n=== ④ 已完成但未归档的任务不能被永久跳过（9629 正是此形态）===")
    tmp, c = prepare_db(90003, "T-DONE-NOART", "COMPLETED", 100)
    js = aj.open_jobs(c, 90003)
    ok("COMPLETED 但 artifact_id=0 ⇒ 仍属待处理",
       any(x["task_id"] == "T-DONE-NOART" for x in js), str([x["task_id"] for x in js]))
    c.close()

    print("\n=== ⑤ 读不出完整文件 ⇒ 标 UNREADABLE 终态（不空跑、不许谎称已归档）===")
    tmp, c = prepare_db(90004, "T-GAP", "COMPLETED", 100)
    pipe = FakePipe({
        ("export_sysml_v2", "STATUS"):
            [json.dumps({"taskId": "T-GAP", "status": "COMPLETED", "progress": 100,
                         "fileName": "demo.sysml", "sizeBytes": str(SIZE)})],
        ("export_sysml_v2", "READ"): [read_page(0), read_page(2)],   # 故意缺中间段
    })
    r = aj.resume_jobs(pipe, 90004, conn=c)
    ok("缺口不产出文档", not r["docs"])
    ok("告知'读取失败、不要声称已归档'", "不要声称已归档" in " ".join(r["notes"]))
    row = c.execute("SELECT status FROM %s WHERE conversation_id=?" % aj.TABLE,
                    (90004,)).fetchone()
    ok("标为 UNREADABLE", (row["status"] if row else "") == aj.UNREADABLE,
       str(row and row["status"]))
    js2 = aj.open_jobs(c, 90004)
    ok("UNREADABLE 不再进待处理（不每轮空跑）", not js2, str([x["task_id"] for x in js2]))
    c.close()

    print("\n=== ⑥ 过期/不可查询任务：必须判终态，不能永久'仍在运行' ===")
    tmp, c = prepare_db(90006, "T-EXPIRED", "RUNNING", 10)
    pipe = FakePipe({("export_sysml_v2", "STATUS"): [json.dumps({
        "code": "PERMISSION_DENIED", "message": "您没有数据访问权限",
        "retryable": False})]})   # 服务端真实过期形态：无 status 字段
    r = aj.resume_jobs(pipe, 90006, conn=c)
    ok("不产出文档", not r["docs"])
    ok("不归入 running（否则永远等不到）", r["running"] == [], str(r["running"]))
    ok("归入 failed", r["failed"] == ["T-EXPIRED"], str(r["failed"]))
    ok("明确告知可重新提交", "重新提交" in " ".join(r["notes"]),
       " ".join(r["notes"])[:90])
    row = c.execute("SELECT status FROM %s WHERE conversation_id=?" % aj.TABLE,
                    (90006,)).fetchone()
    ok("标为 EXPIRED", (row["status"] if row else "") == aj.EXPIRED, str(row and row["status"]))
    ok("EXPIRED 不再进待处理", not aj.open_jobs(c, 90006))
    c.close()

    print("\n=== ⑦ 僵尸任务：轮询达上限即判过期 ===")
    tmp, c = prepare_db(90007, "T-ZOMBIE", "RUNNING", 0)
    c.execute("UPDATE %s SET poll_count=? WHERE conversation_id=?" % aj.TABLE,
              (aj.MAX_POLLS, 90007))
    c.commit()
    pipe = FakePipe({("export_sysml_v2", "STATUS"): [json.dumps({
        "taskId": "T-ZOMBIE", "status": "RUNNING", "progress": 100})]})
    r = aj.resume_jobs(pipe, 90007, conn=c)
    ok("轮询超限 ⇒ 判过期而非继续等", r["failed"] == ["T-ZOMBIE"], str(r["failed"]))
    c.close()

    print("\n=== ⑧ 变异自证：把判据改回 status!=COMPLETED ⇒ 第④项必须变红 ===")
    src = open(os.path.join(ROOT, "agent", "async_jobs.py"), encoding="utf-8").read()
    mutated = src.replace("WHERE conversation_id=? AND artifact_id=0 ",
                          "WHERE conversation_id=? AND status != 'COMPLETED' AND artifact_id=0 ")
    ok("变异点确实命中", mutated != src)
    ns = {}
    exec(compile(mutated, "<mut_async>", "exec"), ns)
    tmp, c = prepare_db(90005, "T-MUT", "COMPLETED", 100)
    # 变异版 open_jobs（重建同名全局 TABLE 等已在 ns 内）
    mjs = ns["open_jobs"](c, 90005)
    ok("变异版漏掉'已完成未归档'任务（证明原判据确有必要）",
       not any(x["task_id"] == "T-MUT" for x in mjs), str([x["task_id"] for x in mjs]))
    c.close()

    if "--live" in sys.argv:
        print("\n=== ⑦ 真实链路（--live）：真 MCP 拉 conv 9629 的已完成任务 ===")
        from mcp_client import MCPClient
        c0 = sqlite3.connect("file:%s?mode=ro" % os.path.join(ROOT, "mbse.db"), uri=True)
        c0.row_factory = sqlite3.Row
        row = c0.execute("SELECT endpoint,transport,headers FROM mcp_servers "
                         "WHERE name='zhiyuan-dev-mcp'").fetchone()
        c0.close()
        cli = MCPClient(row["endpoint"], row["transport"] or "sse",
                        headers=json.loads(row["headers"] or "{}"))

        class RealPipe:
            def __init__(self):
                self._tool_sysml_docs = []
                self._tool_sysml_docs_seen = set()

            def _exec_tool_call(self, name, args):
                return cli.call_tool(name, args)

        tmp, c_live = prepare_db(9629, "2114956963811217410", "RUNNING", 10)
        c_live.commit()
        rp = RealPipe()
        r2 = aj.resume_jobs(rp, 9629, conn=c_live)
        ok("真链路产出文档", len(r2["docs"]) == 1, str(len(r2["docs"])))
        if r2["docs"]:
            d = r2["docs"][0]
            ok("真链路字节数 == 服务端声明 80543（如已变更以实际为准）",
               len(d["text"].encode("utf-8")) > 50000,
               "%d 字节" % len(d["text"].encode("utf-8")))

    print("\n" + "=" * 70)
    if FAILS:
        print("❌ FAIL %d 项：%s" % (len(FAILS), "; ".join(FAILS)))
        sys.exit(1)
    print("✅ 通过 —— 提交即托管 / 跨轮续跑 / 已完成未归档不漏 / 不可读标终态（含变异自证）")


if __name__ == "__main__":
    main()
