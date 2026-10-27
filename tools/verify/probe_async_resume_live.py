# -*- coding: utf-8 -*-
"""端到端探针：**用户只发一句「继续获取V2代码」，产物是否自动落地**。

模拟真实失败场景（会话 9629 连续 4 轮失败），验证新机制：
  1. 造一个会话，预置一条"已提交但未归档"的异步任务（taskId 来自**真实**导出）
  2. 发一句「继续获取V2代码」（钉住执行主体 = 智源对接agent）
  3. 断言：① 产物库里出现了文件；② 模型**没有**重新 PREVIEW/EXPORT（不再从零重来）

不写死任何期望值：产物内容与字节数一律从库里读回比对。

用法：python tools/verify/probe_async_resume_live.py
"""
import json
import os
import sqlite3
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
DB = os.path.join(ROOT, "mbse.db")
BASE = "http://127.0.0.1:8000"
TASK = os.environ.get("PROBE_TASK", "2114940498336481282")
MSG = "继续获取V2代码"


def call(path, body=None, method=None, headers=None):
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers=h,
                                 method=method or ("POST" if body is not None else "GET"))
    with urllib.request.urlopen(req, timeout=900) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    from agent import async_jobs as aj

    tok = call("/api/auth/local-login", {"display_name": "async-resume-probe"})
    hdr = {"X-Session-Token": tok["token"]}
    conv = call("/api/conversations", {"title": "async resume probe %d" % int(time.time())},
                headers=hdr)
    cid = conv.get("id") or conv.get("conversation", {}).get("id")
    print("[1] 测试会话 id=%s" % cid)

    # 预置"已提交但未归档"任务（真实 taskId，真实文件）
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    aj.ensure_table(c)
    aj.record(c, cid, "zhiyuan-dev-mcp", "export_sysml_v2",
              {"task_id": TASK, "status": "RUNNING", "progress": 10,
               "file_name": "", "meta": {}})
    c.commit()
    print("[2] 预置待续跑任务 taskId=%s" % TASK)
    before_tools = c.execute("SELECT COUNT(*) FROM tool_call_logs WHERE conversation_id=?",
                             (cid,)).fetchone()[0]
    c.close()

    stages, tools = [], []
    body = {"message": MSG, "branch": "dev", "team": "智源对接agent"}
    req = urllib.request.Request(
        "%s/api/conversations/%d/chat/stream" % (BASE, cid),
        data=json.dumps(body, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json", **hdr}, method="POST")
    with urllib.request.urlopen(req, timeout=900) as r:
        for raw in r:
            line = raw.decode("utf-8", "ignore").strip()
            if not line.startswith("data:"):
                continue
            p = line[5:].strip()
            if not p or p == "[DONE]":
                continue
            try:
                ev = json.loads(p)
            except Exception:
                continue
            if ev.get("type") == "stage":
                stages.append((ev.get("name"), ev.get("detail")))
            elif ev.get("type") == "tool" and ev.get("status") == "done":
                tools.append((ev.get("name"), ev.get("ok")))

    print("\n[3] stage 事件：")
    for s in stages:
        print("    %s | %s" % (s[0], str(s[1] or "")[:90]))
    print("\n[4] 工具调用：%s" % tools)

    c = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
    c.row_factory = sqlite3.Row
    arts = list(c.execute("SELECT id,kind,title,length(preview_content) n FROM artifacts "
                          "WHERE conversation_id=? ORDER BY id", (cid,)))
    logs = list(c.execute("SELECT id,tool_name,arguments FROM tool_call_logs "
                          "WHERE conversation_id=? ORDER BY id", (cid,)))
    jrow = c.execute("SELECT status,artifact_id FROM %s WHERE conversation_id=?" % aj.TABLE,
                     (cid,)).fetchone()
    ans = c.execute("SELECT content FROM messages WHERE conversation_id=? AND role='assistant' "
                    "ORDER BY id DESC LIMIT 1", (cid,)).fetchone()
    c.close()

    print("\n[5] 产物（本会话）：")
    if not arts:
        print("    ❌ 0 条 —— 未自动落地")
    for a in arts:
        print("    id=%s kind=%s %s (%s 字符)" % (a["id"], a["kind"], a["title"], a["n"]))
    print("\n[6] 任务表状态：%s" % (dict(jrow) if jrow else None))
    print("[7] 工具日志 %d 条：%s" % (len(logs), [x["tool_name"] for x in logs]))

    # 关键判据：**按 action 判**，不能按工具名判 —— 续跑本身就走同一个工具
    # （STATUS 查状态 + READ 分页读），那是"复用"；重新导出是 PREVIEW/EXPORT。
    acts = []
    for r in logs:
        a = r["arguments"] or "{}"
        try:
            acts.append(str(json.loads(a).get("action") or "-").upper())
        except Exception:
            acts.append("?")
    reexport = any(a in ("PREVIEW", "EXPORT") for a in acts)
    print("\n[8] 动作序列：%s" % acts)
    print("    是否重新提交导出（PREVIEW/EXPORT）：%s"
          % ("是 ❌（未复用旧任务）" if reexport else "否 ✅ 复用旧任务（只 STATUS+READ）"))

    # 二次核验：任务行必须已回填 artifact_id（否则下轮会重复读、生成重复产物）
    linked = bool(jrow and jrow["artifact_id"])
    print("    任务行 artifact_id 已回填：%s" % ("✅" if linked else "❌（下轮会重复读取）"))

    ok_art = bool(arts)
    ok_link = bool(jrow and jrow["artifact_id"])
    print("\n=== 结论 ===")
    print("  自动落地产物：%s" % ("✅" if ok_art else "❌"))
    print("  未重新导出  ：%s" % ("✅" if not reexport else "❌"))
    print("  任务已回填  ：%s" % ("✅" if ok_link else "❌"))
    if ans:
        head = (ans["content"] or "").strip().split("\n")[0][:100]
        print("  回答首行    ：%s" % head)
    print("\nCONV_ID=%s" % cid)
    sys.exit(0 if (ok_art and not reexport and ok_link) else 1)


if __name__ == "__main__":
    main()
