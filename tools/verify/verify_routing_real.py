# CI-OPTIONAL: [local] tier=local（见 tools/gates_manifest.json）：需真实服务 / 真 LLM key / 生产真库金标或随包运行时资产，CI 无服务无 secret ⇒ 跑不了；本机已实测通过，改法须同时改清单 tier 并接线，不允许只删这行
# -*- coding: utf-8 -*-
"""会话入口验证：改完 description/keywords 后，**路由是否落到期望 Agent**（2026-11-07）。

## 为什么不能只看 Agent 数量/描述长度

改的是"给路由看的字段"。真正的判据只有一条：**这句话发给系统，它选了谁**。
本脚本走真实 HTTP 入口（`/api/conversations/{id}/chat/stream`），
从 SSE 的 `agent` 事件与 `tool_call_logs.agent_name` 取实际执行主体，不看模型自述。

## 用例设计原则

每条用例都配「必须落到 X」与「绝不能落到 Y」：
- 只要求 X ⇒ 可能因为"全部落 chat"而假过（chat 是兜底，什么都能落）
- 加上 Y ⇒ 能识别"被邻近 Agent 抢走"这种真缺陷

## 期望值来源

`agents.description` 的**触发话术**（本轮刚写的三段式第二段）。
即：描述里承诺"用户说 X 时用我"，那 X 就必须真的路由到它 —— 否则说明描述与实现不符。

用法：
    ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_routing_real.py
    ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_routing_real.py --case 3   # 单跑
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
import urllib.request

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

BASE = "http://127.0.0.1:8000"
RES = []

#: (用例号, 用户原话, 期望 Agent, 绝不应落到)
CASES = [
    (1, "帮我设计一下这个无人机地面控制站的系统架构，给两套方案对比",
     "design", ("chat", "knowledge_qa", "review")),
    (2, "这个模型语法上有问题吗？帮我预评审一下，打个分",
     "review", ("chat", "design", "knowledge_qa")),
    (3, "改了发动机的接口定义，会影响哪些部件？给我影响范围",
     "impact", ("chat", "design", "review")),
    (4, "把刚才的分析整理成一份报告导出来",
     "report_generation", ("chat", "knowledge_qa")),
    (5, "我有个需求描述得很模糊：系统要快速响应。这条需求怎么定？验收标准怎么写？",
     "requirement_analysis", ("chat", "design", "report_generation")),
    (6, "SysML v2 里 part def 和 part 有什么区别？",
     "knowledge_qa", ("chat", "design", "review")),
    (7, "这个平台怎么新建一个工程？在哪个菜单",
     "chat", ("design", "review", "impact")),
]


def _db():
    return sqlite3.connect("file:%s?mode=ro"
                           % os.path.join(_ROOT, "mbse.db").replace("\\", "/"), uri=True)


def _post(path, payload):
    req = urllib.request.Request(BASE + path,
                                 data=json.dumps(payload, ensure_ascii=False).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode() or "{}")


def _sse(path, payload, timeout=300):
    """读 SSE，**收到 done 就 break**。

    ⚠️ 第一版没有 break ⇒ 服务端在 done 之后不关连接（keep-alive），
       `for raw in r` 一直阻塞到 timeout ⇒ 单个用例跑满 300 秒，
       看起来像"验证卡死"，实际是判据代码自己不肯收尾。
    """
    req = urllib.request.Request(BASE + path,
                                 data=json.dumps(payload, ensure_ascii=False).encode(),
                                 headers={"Content-Type": "application/json"})
    evs, buf = [], ""
    with urllib.request.urlopen(req, timeout=timeout) as r:
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            buf += line[5:].strip()
            try:
                ev = json.loads(buf)
                buf = ""
            except Exception:
                continue
            evs.append(ev)
            if isinstance(ev, dict) and ev.get("type") == "done":
                break
    return evs


def _rec(name, ok, detail=""):
    RES.append(bool(ok))
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name,
                           ("  | " + str(detail)[:200]) if detail else ""))


def run_case(no, text, expect, forbid, timeout=200):
    cid = _post("/api/conversations", {"title": "路由验证 %d" % no}).get("id")
    t0 = time.time()
    evs = _sse("/api/conversations/%d/chat/stream" % cid, {"message": text},
               timeout=timeout)
    dt = time.time() - t0
    # 从 SSE 取「意图识别」stage 与 agent 事件（首个 agent 事件 = 执行主体）
    intent, agent, route, conf = "", "", "", None
    for e in evs:
        if not isinstance(e, dict):
            continue
        if e.get("type") == "stage" and e.get("name") == "意图识别" \
                and e.get("status") == "done":
            intent = e.get("intent") or intent
            route = e.get("route") or route
            conf = e.get("confidence", conf)
            agent = e.get("agent") or agent
        if e.get("type") == "agent" and e.get("name") and not agent:
            agent = e.get("name")
    # 交叉校验：落库的 agent_name（不看 SSE，避免前端改文案影响判据）
    c = _db()
    try:
        db_agents = [r[0] for r in c.execute(
            "SELECT DISTINCT agent_name FROM tool_call_logs WHERE conversation_id=?",
            (cid,)).fetchall() if r[0]]
    finally:
        c.close()

    ok = (intent == expect)
    bad = intent in forbid
    line = "意图=%s 路由=%s 置信=%s 执行主体=%s %.0fs" % (intent, route, conf, agent, dt)
    _rec("#%d「%s…」→ 期望 %s" % (no, text[:18], expect), ok and not bad, line)
    if db_agents:
        print("       实际调过工具的 Agent：%s" % db_agents[:3])
    return ok and not bad


def main() -> int:
    only = None
    if "--case" in sys.argv:
        i = sys.argv.index("--case")
        if i + 1 < len(sys.argv):
            only = int(sys.argv[i + 1])
    print("=" * 78)
    print("会话入口路由验证（真实 HTTP + SSE，判据 = 实际选中的意图/Agent）")
    print("=" * 78)
    for no, text, expect, forbid in CASES:
        if only and no != only:
            continue
        try:
            run_case(no, text, expect, forbid)
        except Exception as e:
            _rec("#%d 执行异常" % no, False, "%s: %s" % (type(e).__name__, str(e)[:150]))
    ok = all(RES)
    print("\n" + "=" * 78 + "  ALL_OK=%s  (%d passed / %d failed)"
          % (ok, sum(1 for x in RES if x), sum(1 for x in RES if not x)))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
