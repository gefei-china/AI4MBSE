# -*- coding: utf-8 -*-
"""团队定义驱动编排 · 端到端真机验证（2026-10-07）。

验的是**真实 HTTP + 真实 SSE + 真实 LLM**下的行为，不是单测里的 mock：
  case1「你好」                → 不得触发编排（改前 team 模式无条件编排）
  case2「生成需求视图」        → 单成员直行，1个 agent 事件、无 subtask 编排事件
  case3「帮我写一首诗」        → 明确提示不支持 + 带能力清单与出路
  case4「…生成结构视图…然后校验」→ 走编排（出现 subtask/agent 多阶段事件）

用法（需服务已在 127.0.0.1:8000 跑着）：
    ./.venv/Scripts/python.exe -X utf8 tools/verify/e2e_team_routing.py
    ./.venv/Scripts/python.exe -X utf8 tools/verify/e2e_team_routing.py --cases 1,2
"""
import argparse
import json
import sys
import time

import httpx

API = "http://127.0.0.1:8000"
TEAM = "MBSE建模总体负责人"

CASES = {
    1: ("你好", "chat"),
    2: ("生成需求视图", "direct"),
    3: ("帮我写一首诗", "reject"),
    4: ("帮我进行热管理系统的工程建模，生成结构视图并生成代码，然后进行校验", "orchestrate"),
}


def run_case(cid: int, query: str, want: str, timeout: int = 420):
    """跑一轮 SSE，统计事件构成。返回 (summary dict, 首个文本片段)。"""
    ev = {"token": 0, "subtask": 0, "agent": [], "stage": [], "error": 0, "done": None}
    text = []
    t0 = time.time()
    with httpx.stream("POST", API + "/api/conversations/%s/chat/stream" % cid,
                      json={"message": query, "branch": "personal", "team": TEAM},
                      timeout=httpx.Timeout(timeout, read=timeout)) as r:
        if r.status_code >= 400:
            return {"http": r.status_code}, ""
        for line in r.iter_lines():
            if line.startswith("event: "):
                cur = line[7:]
            elif line.startswith("data: ") and cur:
                try:
                    d = json.loads(line[6:])
                except Exception:
                    continue
                if cur == "token":
                    ev["token"] += 1
                    text.append(d.get("delta") or "")
                elif cur == "subtask":
                    ev["subtask"] += 1
                elif cur == "agent":
                    if d.get("status") == "run":
                        ev["agent"].append(d.get("name") or d.get("display_name") or "")
                elif cur == "stage":
                    ev["stage"].append(d.get("name"))
                elif cur == "error":
                    ev["error"] += 1
                elif cur == "done":
                    ev["done"] = d
    ev["elapsed"] = round(time.time() - t0, 1)
    ev["text"] = "".join(text)
    return ev, "".join(text)[:200]


def main() -> int:
    global API
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", default="", help="只跑指定 case，逗号分隔")
    ap.add_argument("--api", default=API)
    args = ap.parse_args()
    API = args.api

    todo = [int(x) for x in args.cases.split(",") if x.strip()] or sorted(CASES)
    fails, passes = [], []

    def chk(cond, msg):
        (passes if cond else fails).append(msg)
        print(("  [PASS] " if cond else "  [FAIL] ") + msg)

    for n in todo:
        query, want = CASES[n]
        print("\n=== case%d（期望 %s）：%s ===" % (n, want, query[:40]))
        cid = httpx.post(API + "/api/conversations",
                         json={"title": "团队路由E2E-case%d" % n}, timeout=20).json().get("id")
        if not cid:
            chk(False, "case%d 建会话失败" % n)
            continue
        ev, head = run_case(cid, query, want)
        print("  事件: token=%s subtask=%s agent=%s stage=%s error=%s 耗时=%ss"
              % (ev.get("token"), ev.get("subtask"), ev.get("agent"),
                 ev.get("stage"), ev.get("error"), ev.get("elapsed")))
        if head:
            print("  文本: %s" % head.replace("\n", " ")[:120])
        chk(ev.get("http", 200) < 400, "case%d HTTP 正常（%s）" % (n, ev.get("http", 200)))
        chk(ev.get("error", 0) == 0, "case%d 无 error 事件" % n)

        if want == "chat":
            chk(ev["subtask"] == 0, "case%d ★未触发编排（subtask=%d）" % (n, ev["subtask"]))
        elif want == "direct":
            chk(ev["subtask"] == 0, "case%d ★单成员直行不编排（subtask=%d）" % (n, ev["subtask"]))
            chk("需求视图生成" in " ".join(ev["agent"]) or ev["token"] > 0,
                "case%d 有产出（agent=%s / token=%s）" % (n, ev["agent"], ev["token"]))
        elif want == "reject":
            blob = (head or "") + json.dumps(ev.get("done") or {}, ensure_ascii=False)
            chk("不支持" in blob or "暂不支持" in blob,
                "case%d ★回复含「不支持」提示" % n)
            chk("本团队可处理" in blob, "case%d ★带团队能力清单（不是死胡同）" % n)
        elif want == "orchestrate":
            chk(ev["subtask"] > 0 or len(set(ev["agent"])) > 1,
                "case%d ★走编排（subtask=%s agent=%s）" % (n, ev["subtask"], ev["agent"]))

    print("\n" + "=" * 68)
    print("PASS %d / FAIL %d" % (len(passes), len(fails)))
    if fails:
        print("HAS FAILURE")
        for f in fails:
            print("  x " + f)
        return 1
    print("ALL GREEN")
    return 0


if __name__ == "__main__":
    sys.exit(main())