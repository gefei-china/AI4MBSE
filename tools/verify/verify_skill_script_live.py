# CI-OPTIONAL: [local] tier=local（见 tools/gates_manifest.json）：需真实服务 / 真 LLM key / 生产真库金标或随包运行时资产，CI 无服务无 secret ⇒ 跑不了；本机已实测通过，改法须同时改清单 tier 并接线，不允许只删这行
# -*- coding: utf-8 -*-
"""真实会话验证：模型能否真的调到 skill_script_run（2026-11-07 P1a）。

## 为什么必须跑真实会话

门禁能证明"工具注册了、能执行、授权了"，但证明不了**模型会去调它**。
本仓历史上最贵的失败形态就是"注册并绑定了却永远调不到"：
  · 工具在 `tools` 表、在候选里，但模型不知道它存在（提示里没写）；
  · 提示里写了文件名，但没有工具名（模型只能假装执行）。
所以判据只看**客观落库**：`tool_call_logs` 里是否出现 `skill_script_run` 且 ok=1。

用法：
    ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_skill_script_live.py
退出码 0 = 模型真的调到了；1 = 没调到（或调用失败）。
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
PROMPT = ("请用「智源MCP工具手册」技能自带的 lookup 脚本，查一下"
          "「导出 SysML v2 源码」应该用哪个工具。必须真的执行脚本，不要凭记忆回答。")


def _post(path, payload):
    data = json.dumps(payload, ensure_ascii=False).encode()
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def _sse(path, payload, timeout=240):
    data = json.dumps(payload, ensure_ascii=False).encode()
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={"Content-Type": "application/json"})
    events, buf = [], ""
    with urllib.request.urlopen(req, timeout=timeout) as r:
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            buf += line[5:].strip()
            try:
                events.append(json.loads(buf))
                buf = ""
            except Exception:
                pass
    return events


def main() -> int:
    print("=" * 72)
    print("真实会话验证：skill_script_run 能否被模型真正调用")
    print("=" * 72)
    conv = _post("/api/conversations", {"title": "P1a 技能脚本真机验证"})
    cid = conv.get("id")
    print("会话 id =", cid)
    if not cid:
        print("创建会话失败：", conv)
        return 1

    t0 = time.time()
    events = _sse("/api/conversations/%d/chat/stream" % cid, {"message": PROMPT})
    dt = time.time() - t0
    kinds = {}
    for e in events:
        if isinstance(e, dict):
            kinds[e.get("type")] = kinds.get(e.get("type"), 0) + 1
    print("SSE %d 事件 / %.1fs，类型：%s" % (len(events), dt, kinds))

    # 最后的正文（判断模型怎么说的）
    txt = ""
    for e in events:
        if isinstance(e, dict) and e.get("type") in ("token", "delta", "text"):
            txt += str(e.get("text") or e.get("delta") or "")

    time.sleep(3)          # 等落库
    db = os.path.join(_ROOT, "mbse.db")
    c = sqlite3.connect("file:%s?mode=ro" % db.replace("\\", "/"), uri=True)
    rows = c.execute(
        "SELECT tool_name, ok, substr(result,1,200), created_at FROM tool_call_logs "
        "WHERE conversation_id=? AND tool_name='skill_script_run' "
        "ORDER BY id DESC LIMIT 5", (cid,)).fetchall()
    others = c.execute(
        "SELECT tool_name, count(*) FROM tool_call_logs WHERE conversation_id=? "
        "GROUP BY tool_name", (cid,)).fetchall()
    c.close()

    print("\n本次会话的工具调用：", others)
    print("skill_script_run 调用数：", len(rows))
    for r in rows:
        print("  ok=%s  %s  %s" % (r[1], str(r[2])[:150].replace("\n", " "), r[3]))

    ok = any(r[1] == 1 for r in rows)
    print("\n" + "=" * 72)
    print("结论：%s" % ("✅ 模型真的调到了脚本执行工具（且成功）" if ok
                       else "❌ 模型没有成功调用 skill_script_run"))
    if not ok:
        print("模型回复片段：", txt[:500])
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
