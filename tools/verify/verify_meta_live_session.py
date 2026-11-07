# CI-OPTIONAL: [local] tier=local（见 tools/gates_manifest.json）：需真实服务 / 真 LLM key / 生产真库金标或随包运行时资产，CI 无服务无 secret ⇒ 跑不了；本机已实测通过，改法须同时改清单 tier 并接线，不允许只删这行
# -*- coding: utf-8 -*-
"""真机会话验证：用户能否**只在会话里**完成「提需求 → 审核 → 生效 → 使用」。

2026-11-07（P2）。判据只看客观落库与真实调用，不看模型自述。

## 为什么分阶段（`--phase`）

一次跑完要 3 轮真实 LLM 调用（每轮 40~80 秒），合计常超 300 秒
⇒ 第一版写成单脚本时**整体超时被杀**，看起来像"验证失败"，其实只是慢。
⇒ 拆成可按阶段执行；`all` 仍然串跑，但每轮各自打印，慢也不会误判成红。

| 阶段 | 做什么 | 判据 |
|---|---|---|
| `query` | 问「平台有哪些技能」 | `tool_call_logs` 出现 `capability_query` |
| `propose` | 让它注册一个能力 | `hil_confirmations` 出现 `cap_capability_create`，且 SSE 有 `hil_confirm` 卡 |
| `approve` | 批准上一步那张单 | `executed=true` 且 `ok=true` |
| `use` | 点名让新能力算一次 | `tool_call_logs` 出现**新能力名**且 ok=1 |

## 真机实测记录（2026-11-07，会话 990510 / 990512 / 990514）

- `query`  ✅ 模型调 `capability_query` 返回 41 个技能
- `propose` ✅ 确认单 #66 action=`cap_capability_create`，预览是人话
- `approve` ✅ 注册成功，落盘 `data/workspace/capabilities/live_bmi_calc/main.py`
- `use`    ✅ 模型调 `live_bmi_calc` 并返回 `{"bmi": 22.86}`

⚠️ `use` 阶段当轮踩到的真问题：能力只绑给「知识问答Agent」，而会话路由到
「通用问答Agent」⇒ 模型如实说"我调不了"。已修成**点名即注入**
（`tools.py` 候选构建处，判据 = 名字作为独立 token 出现在原话里）；
门禁 `verify_capability_selfextend.py` 的 E2-E4 锁住它。

用法：
    ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_meta_live_session.py query
    ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_meta_live_session.py propose
    ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_meta_live_session.py approve <conf_id>
    ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_meta_live_session.py use <conv_id> <能力名>
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


def _db():
    return sqlite3.connect("file:%s?mode=ro"
                           % os.path.join(_ROOT, "mbse.db").replace("\\", "/"), uri=True)


def _post(path, payload):
    req = urllib.request.Request(BASE + path,
                                 data=json.dumps(payload, ensure_ascii=False).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read().decode() or "{}")


def _sse(path, payload, timeout=300):
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
                evs.append(json.loads(buf))
                buf = ""
            except Exception:
                pass
    return evs


def _logs(cid):
    c = _db()
    try:
        return c.execute(
            "SELECT tool_name, ok, substr(result,1,300) FROM tool_call_logs "
            "WHERE conversation_id=? ORDER BY id", (cid,)).fetchall()
    finally:
        c.close()


def _new_conv(title):
    return _post("/api/conversations", {"title": title}).get("id")


def phase_query() -> int:
    cid = _new_conv("P2 查询阶段")
    print("会话", cid)
    ev = _sse("/api/conversations/%d/chat/stream" % cid, {
        "message": "用 capability_query 查一下平台现在有哪些 skill（技能），只列名字和状态，不要编造。"})
    print("SSE 事件类型：", {e.get("type") for e in ev if isinstance(e, dict)})
    time.sleep(2)
    names = [r[0] for r in _logs(cid)]
    ok = "capability_query" in names
    print("[%s] 模型主动调了 capability_query | %s" % ("PASS" if ok else "FAIL", names))
    return 0 if ok else 1


def phase_propose() -> int:
    cid = _new_conv("P2 提案阶段")
    print("会话", cid)
    msg = ("请用 capability_apply 给我注册一个新能力：名字 live_demo_cap，"
           "描述「示例：把两个数相加，参数 a 与 b」。"
           "脚本：def run(**kw):\n    return {'sum': float(kw.get('a') or 0) + float(kw.get('b') or 0)}\n"
           "input_schema 要有 a、b 两个 number 参数。kind=capability action=create。"
           "必须真的调工具，不要假装完成。")
    ev = _sse("/api/conversations/%d/chat/stream" % cid, {"message": msg})
    kinds = {e.get("type") for e in ev if isinstance(e, dict)}
    time.sleep(2)
    c = _db()
    c.row_factory = sqlite3.Row
    try:
        confs = c.execute(
            "SELECT id, action, status, substr(preview,1,200) AS pv FROM hil_confirmations "
            "WHERE conversation_id=? AND action LIKE 'cap_%' ORDER BY id DESC LIMIT 3",
            (cid,)).fetchall()
    finally:
        c.close()
    ok1 = bool(confs) and confs[0]["action"] == "cap_capability_create"
    ok2 = "hil_confirm" in kinds
    print("[%s] 确认单 action=cap_capability_create | %s"
          % ("PASS" if ok1 else "FAIL", [dict(x) for x in confs]))
    print("[%s] SSE 里有 hil_confirm 卡（会话内可点） | %s"
          % ("PASS" if ok2 else "FAIL", kinds))
    if confs:
        print("下一步：approve %s" % confs[0]["id"])
    return 0 if (ok1 and ok2) else 1


def phase_approve(cid_conf: str) -> int:
    d = _post("/api/studio/hil-confirmations/%s/decide" % cid_conf, {"approve": True})
    er = d.get("exec_result") or {}
    ok = bool(d.get("executed")) and bool(er.get("ok"))
    print("[%s] 批准后执行成功 | %s" % ("PASS" if ok else "FAIL", str(er.get("result"))[:300]))
    return 0 if ok else 1


def phase_use(conv_id: str, cap_name: str) -> int:
    ev = _sse("/api/conversations/%s/chat/stream" % conv_id, {
        "message": "用 %s 工具算一下：a=37，b=158（或按它的参数填）。必须真的调工具，不要心算。"
                   % cap_name})
    print("SSE 事件类型：", {e.get("type") for e in ev if isinstance(e, dict)})
    time.sleep(2)
    hit = [r for r in _logs(int(conv_id)) if r[0] == cap_name]
    ok = bool(hit) and hit[-1][1] == 1
    print("[%s] 模型调到了新注册的能力且返回正常 | %s"
          % ("PASS" if ok else "FAIL", [tuple(x) for x in hit][-3:]))
    return 0 if ok else 1


def main() -> int:
    args = sys.argv[1:]
    phase = args[0] if args else "query"
    if phase == "query":
        return phase_query()
    if phase == "propose":
        return phase_propose()
    if phase == "approve":
        if len(args) < 2:
            print("用法：approve <confirmation_id>")
            return 2
        return phase_approve(args[1])
    if phase == "use":
        if len(args) < 3:
            print("用法：use <conversation_id> <capability_name>")
            return 2
        return phase_use(args[1], args[2])
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
