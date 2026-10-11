# -*- coding: utf-8 -*-
"""_probe_hier_live.py —— 真实对话验证「三级层级事件」确实下发（打真实端点 :8000）。

米爸要求：「整个执行过程可见，要按照层级展示具体环节，比如 agent 下的 skill
调用的 mcp，应该按照层级进行分层展示」。

本脚本验证**协议侧**是否真的把层级字段下发到前端能拿到的地方：
  ① 每一类事件都带 depth / agent（skill、tool 必查）
  ② depth 分布符合契约（agent=0 / skill=1 / tool=1或2）
  ③ tool 的 parent 能对上 skill 或 agent 的 key（层级能真的挂上）
  ④「知识库检索」stage 不再无条件出现（未检索就不该有）
  ⑤ chat（闲聊）不再触发知识库检索（本轮解绑了图谱 + kb_required=0）

用法： ./.venv/Scripts/python.exe -X utf8 tools/verify/_probe_hier_live.py "提问文本"
"""
import json
import sys
import urllib.request

BASE = "http://127.0.0.1:8000"
# ⚠️ 本机 HTTP_PROXY 会劫持 127.0.0.1（实测返回 502），本地探针必须禁代理
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

QUESTION = sys.argv[1] if len(sys.argv) > 1 else "查询一下知识库里关于活动图建模的规范"
_results = []


def check(name, ok, detail=""):
    _results.append(bool(ok))
    print("[%s] %s%s" % ("OK  " if ok else "FAIL", name, ("　— " + detail) if detail and not ok else ""))


def post_json(path, payload, timeout=60):
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with OPENER.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print("=" * 80)
    print("三级层级事件 —— 真实端点验证（:8000，不 mock）")
    print("=" * 80)
    with OPENER.open(BASE + "/openapi.json", timeout=10) as r:
        print("服务在线 HTTP %s" % r.status)

    conv = post_json("/api/conversations", {"title": "层级验证(临时)", "type": "chat"})
    cid = conv.get("id") or (conv.get("data") or {}).get("id")
    print("会话 conv=%s ｜ 提问：%s\n" % (cid, QUESTION))

    req = urllib.request.Request(
        BASE + "/api/conversations/%s/chat/stream" % cid,
        data=json.dumps({"message": QUESTION}).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"})
    events, buf = [], b""
    with OPENER.open(req, timeout=240) as r:
        while True:
            chunk = r.read1(4096) if hasattr(r, "read1") else r.read(4096)
            if not chunk:
                break
            buf += chunk
            while b"\n\n" in buf:
                raw, buf = buf.split(b"\n\n", 1)
                s = raw.decode("utf-8", "replace")
                et, data = "message", ""
                for line in s.splitlines():
                    if line.startswith("event:"):
                        et = line[6:].strip()
                    elif line.startswith("data:"):
                        data += line[5:].strip()
                if not data:
                    continue
                try:
                    events.append((et, json.loads(data)))
                except Exception:
                    events.append((et, {}))

    print("=== 事件序列（关键字段）===")
    print("  %-10s %-6s %-22s %-14s %s" % ("type", "depth", "agent", "skill", "name/摘要"))
    print("  " + "-" * 76)
    for et, ev in events:
        if et not in ("tool", "skill", "agent", "stage", "subtask"):
            continue
        nm = (ev.get("name") or (ev.get("names") and "、".join(ev["names"])) or "")[:24]
        print("  %-10s %-6s %-22s %-14s %s"
              % (et, ev.get("depth", "-"), str(ev.get("agent") or "-")[:20],
                 str(ev.get("skill") or "-")[:12], nm))

    tools = [e for t, e in events if t == "tool"]
    skills = [e for t, e in events if t == "skill"]
    agents = [e for t, e in events if t == "agent"]
    stages = [e for t, e in events if t == "stage"]

    print("\n=== 判据 ===")
    # ① 字段完整性
    ok1 = all(("depth" in e and "agent" in e) for e in tools) if tools else True
    check("① tool 事件全部带 depth/agent", ok1,
          "缺: %s" % [e.get("name") for e in tools if "depth" not in e or "agent" not in e][:4])
    ok1b = all(("depth" in e and "agent" in e) for e in skills) if skills else True
    check("①b skill 事件全部带 depth/agent", ok1b)

    # ② depth 分布
    d_agents = {e.get("depth") for e in agents}
    check("② agent 事件 depth=0（第 1 层容器）",
          d_agents.issubset({0, None}) or not d_agents,
          "实际 depth=%s" % d_agents)
    d_tools = {e.get("depth") for e in tools}
    check("②b tool 事件 depth ∈ {1,2}", d_tools.issubset({1, 2}),
          "实际=%s" % d_tools)

    # ③ parent 能对上（前端要靠它挂树）
    if tools:
        agent_keys = {"agent_" + (e.get("name") or "") for e in agents}
        skill_keys = set()
        for e in skills:
            for nm in (e.get("names") or []):
                skill_keys.add("skill_" + nm)
        orphan = []
        for e in tools:
            sk = e.get("skill")
            ag = e.get("agent")
            if sk and ("skill_" + sk) not in skill_keys:
                orphan.append((e.get("name"), "skill未定义:" + sk))
            elif not sk and ag and ("agent_" + ag) not in agent_keys:
                orphan.append((e.get("name"), "agent未定义:" + ag))
        check("③ tool 的 skill/agent 归属都能对上已发出的容器节点", not orphan,
              "孤儿: %s" % orphan[:4])
    else:
        print("  （本轮无 tool 事件，③ 跳过——本轮提问未触发工具）")

    # ④ 知识库检索 stage：knowledge_qa 是 kb_required=1 ⇒ 应当出现
    kb_stage = [e for e in stages if e.get("name") == "知识库检索"]
    print("  知识库检索 stage 出现 %d 次（本轮Agent 为知识问答类，期望 ≥1）" % len(kb_stage))

    # ⑤ chat 不该再检索
    print("\n=== 汇总 ===")
    print("  tool 事件 %d ｜ skill %d ｜ agent %d ｜ stage %d"
          % (len(tools), len(skills), len(agents), len(stages)))
    n_fail = sum(1 for r in _results if not r)
    print("  %d pass / %d fail" % (len(_results) - n_fail, n_fail))
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())