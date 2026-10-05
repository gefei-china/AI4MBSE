# -*- coding: utf-8 -*-
"""AI 对话「确认/澄清 + 中断固化」链路自检（2026-09-25）。

验证对象：
  routers/conversations.py
    · GET  /api/conversations/{id}/messages      → 返回体带 pending_clarify（前端据此决定历史澄清卡是否可作答）
    · POST /api/conversations/{id}/clarify-answer → 支持 {free_text} 自由文本作答（主输入框路径），并清空挂起
    · POST /api/conversations/{id}/messages/partial → 固化「已生成但未完成」的内容（原只在浏览器 DOM，刷新即丢）

用法（服务需已启动；脚本自建自清，不碰既有会话）：
  .venv/Scripts/python.exe -X utf8 tools/verify/verify_chat_clarify_partial.py
"""

# ── CI 豁免（2026-10-05 标注，理由已实测）──────────────────
# CI-OPTIONAL: A 需服务在跑（BASE=http://127.0.0.1:8000，实测停服务后 rc=1）
#   分类：A=需服务在跑/ B=需密钥或写真库/ C=实测就红需先修。
#   依据见 docs/遗留优化项-第二轮盘点-20261005.md；
#   由 tools/verify/verify_gate_wiring.py 强制要求（要么接线，要么写理由）。
import json
import sqlite3
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000"
DB = r"C:\Users\gefei\WorkBuddy\2026-08-04-19-05-52\mbse_system\mbse.db"
OK, FAIL = [], []


def call(method, path, body=None):
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read().decode("utf-8", "replace")
            return r.status, (json.loads(raw) if raw.strip() else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw[:300]}


def check(name, cond, evidence=""):
    (OK if cond else FAIL).append(name)
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  ← " + evidence) if evidence else ""))
    return cond


def set_pending(conv_id, payload):
    """直接写库制造「待澄清」状态（模拟 agent 侧澄清后的落库结果）。"""
    c = sqlite3.connect(DB)
    try:
        c.execute("UPDATE conversations SET pending_clarify=? WHERE id=?",
                  (json.dumps(payload, ensure_ascii=False), conv_id))
        c.commit()
    finally:
        c.close()


def main():
    print("── 0. 前置：建一个临时会话 ──")
    st, conv = call("POST", "/api/conversations", {"title": "verify-clarify-partial"})
    cid = (conv or {}).get("id")
    if not check("创建临时会话 200", st == 200 and bool(cid), f"status={st} id={cid}"):
        return 1
    try:
        print("── 1. 消息接口带 pending_clarify（无挂起 → null）──")
        st, r = call("GET", f"/api/conversations/{cid}/messages?limit=5")
        check("返回体含 pending_clarify 字段且为 null",
              st == 200 and "pending_clarify" in (r or {}) and r["pending_clarify"] is None,
              f"status={st} keys={sorted((r or {}).keys())}")

        print("── 2. 制造待澄清 → 接口应回传问题（前端据此渲染可作答卡）──")
        qs = {"questions": [{"id": "q1", "question": "目标星网频段？",
                             "options": ["Ka", "Ku"], "allow_custom": True}],
              "context": {"input": "设计星网链路预算"}}
        set_pending(cid, qs)
        st, r = call("GET", f"/api/conversations/{cid}/messages?limit=5")
        pend = (r or {}).get("pending_clarify") or {}
        check("pending_clarify 回传 1 个问题",
              st == 200 and len(pend.get("questions") or []) == 1 and pend.get("context", {}).get("input"),
              f"questions={len(pend.get('questions') or [])}")

        print("── 3. 自由文本作答（主输入框路径）→ 构造续答文本并清空挂起 ──")
        st, r = call("POST", f"/api/conversations/{cid}/clarify-answer",
                     {"free_text": "用 Ka 频段，载波 2GHz"})
        rt = (r or {}).get("resume_text") or ""
        check("free_text 作答 200", st == 200, f"status={st} resp={r}")
        check("续答文本含【澄清补充】标记与原请求",
              "【澄清补充】" in rt and "设计星网链路预算" in rt and "Ka 频段" in rt,
              f"resume_len={len(rt)}")
        st, r = call("GET", f"/api/conversations/{cid}/messages?limit=5")
        check("作答后挂起已清空", ((r or {}).get("pending_clarify") is None), f"pending={(r or {}).get('pending_clarify')}")
        st, r2 = call("POST", f"/api/conversations/{cid}/clarify-answer", {"free_text": "再答一次"})
        check("重复作答被拒（400，防重复提交）", st == 400 and "无待澄清" in str((r2 or {}).get("error", "")),
              f"status={st} err={(r2 or {}).get('error')}")

        print("── 4. 选项式作答（回归：原有 answers 路径不受影响）──")
        set_pending(cid, {"questions": [{"id": "q9", "question": "是否含测控？", "options": ["含", "不含"]}],
                          "context": {"input": "原请求X"}})
        st, r = call("POST", f"/api/conversations/{cid}/clarify-answer",
                     {"answers": [{"question_id": "q9", "value": "含"}]})
        check("answers 作答 200 且续答含问题与答案",
              st == 200 and "是否含测控？" in ((r or {}).get("resume_text") or "")
              and "含" in ((r or {}).get("resume_text") or ""), f"status={st}")
        st, r = call("POST", f"/api/conversations/{cid}/clarify-answer", {"answers": []})
        check("空作答被拒（400）", st == 400, f"status={st}")

        print("── 5. 中断固化：空内容拒绝 / 正常内容落库 / 幂等由前端保证 ──")
        st, r = call("POST", f"/api/conversations/{cid}/messages/partial", {"content": "   "})
        check("空内容 → 400（不写空消息）", st == 400, f"status={st} err={(r or {}).get('error')}")
        st, r = call("GET", f"/api/conversations/{cid}/messages?limit=50")
        before_n = len((r or {}).get("messages") or [])
        st, r = call("POST", f"/api/conversations/{cid}/messages/partial",
                     {"content": "这是被中断时已生成的内容……", "stopped": True})
        mid = (r or {}).get("id")
        check("固化成功返回 message_id", st == 200 and bool(mid), f"status={st} id={mid}")
        st, r = call("GET", f"/api/conversations/{cid}/messages?limit=50")
        msgs = (r or {}).get("messages") or []
        last = msgs[-1] if msgs else {}
        check("固化内容已落库且带停止标记",
              len(msgs) == before_n + 1 and last.get("role") == "assistant"
              and "这是被中断时已生成的内容" in (last.get("content") or "")
              and "已停止生成" in (last.get("content") or ""),
              f"n={before_n}→{len(msgs)} role={last.get('role')}")
        st, r = call("POST", f"/api/conversations/{cid}/messages/partial", {"content": "x"})
        check("不存在的会话 → 404", call("POST", "/api/conversations/99999999/messages/partial",
                                        {"content": "abc"})[0] == 404, "非目标会话")
        print("── 6. 数据库直查：临时会话的挂起状态干净 ──")
        c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        try:
            row = c.execute("SELECT pending_clarify FROM conversations WHERE id=?", (cid,)).fetchone()
            check("DB 中 pending_clarify 为空串", (row[0] or "") == "", f"pending={row[0]!r}")
        finally:
            c.close()
    finally:
        st, _ = call("DELETE", f"/api/conversations/{cid}")
        print(f"  cleanup 会话#{cid} → {st}")

    print(f"\n结果：{len(OK)} 通过 / {len(FAIL)} 失败")
    if FAIL:
        print("失败项：" + "；".join(FAIL))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())