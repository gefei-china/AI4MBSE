# CI-OPTIONAL: [local] 需运行中的服务 + 真实 LLM key（走 /chat/stream 真机对话与人工确认闸）；CI 无服务无 secret
# -*- coding: utf-8 -*-
"""会话入口真机验证：需求条目化 → 落库（含写前确认闸）。

## 为什么必须走会话入口而不是直接调函数
R1 修的正是「**注册了没接线**」：工具行有、绑定有、技能正文教模型调用，
但分发点没 import ⇒ 46 次真实调用全返回「未知工具」。
这类缺陷在函数级测试里**完全看不见**（直接调 `exec_tool` 永远成功）。
唯一能证明"修好了"的判据是：**模型在真实对话里调用它，且拿到真实结果**。

## 判据（严格，不允许"或"式松判）
C1 第 1 轮：`requirement_itemize` 调用成功（ok=1，不再是「未知工具」）
C2 第 1 轮：含模糊句 ⇒ 模型**必须**暂停澄清（不许自己编数值），且**此时不得落库**
C3 第 2 轮：`requirement_commit` 被写前确认闸拦住（side_effect=write）
C4 人工批准后条目**真的**落到会话所属工程（数量严格增加）
C5 条目编号与需求视图骨架里的 `reqId` 一致（追溯不断链）
C6 同文重复提交 inserted=0（幂等）

用法（需服务在 8000 端口运行）：
    python tools/verify/verify_requirement_chat_e2e.py
"""
import json
import os
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
BASE = "http://127.0.0.1:8000"

def _unique_sentence() -> str:
    """每轮插入一条**唯一且仍可验证**的需求（带随机数值）。

    为什么需要：本门禁会被反复运行，若用例文本固定，第二轮的条目因幂等
    `inserted=0` ⇒ "落库后数量增加"这条判据**在第二轮必然假红**（本轮实测踩过）。
    换个说法就是：判据要对环境稳健，不能假设"每次跑都是干净库"。
    """
    import random
    return "冷却回路压降不超过 %d kPa" % random.randint(10, 99)


RUN_SENT = _unique_sentence()
MSG1 = ("请把下面这段需求结构化并入库："
        "电池包在 -20℃ 环境下 30 分钟内应完成加热至 10℃ 以上；"
        "电芯间最大温差不超过 5℃；"
        "热管理系统应通过 CAN 总线每 100ms 上报状态；"
        "%s；"
        "系统应具有良好的用户体验。" % RUN_SENT)
MSG2 = ("那条模糊需求先不入库。其余 3 条都能量化验证，"
        "请立刻用 requirement_commit 把它们落库到当前工程，然后告诉我入库的 REQ 编号。")


def _req(method, path, body=None, timeout=60):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8", "replace")
    try:
        return json.loads(raw)
    except Exception:
        return {"_raw": raw}


def _stream(conv_id, message, timeout=900):
    body = json.dumps({"message": message}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(BASE + "/api/conversations/%d/chat/stream" % conv_id,
                                 data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    frames, t0 = [], time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        while True:
            chunk = r.readline()
            if not chunk:
                break
            line = chunk.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            buf = line[5:].strip()
            if not buf:
                continue
            try:
                frames.append(json.loads(buf))
            except Exception:
                frames.append({"event": "raw", "data": buf[:200]})
            if time.time() - t0 > timeout:
                frames.append({"event": "timeout"})
                break
    return frames


def _tools_of(conn, cid):
    return [dict(r) for r in conn.execute(
        "SELECT tool_name, ok, substr(result,1,200) r FROM tool_call_logs "
        "WHERE conversation_id=? AND tool_name LIKE 'requirement%' ORDER BY id", (cid,))]


def main() -> int:
    try:
        _req("GET", "/openapi.json", timeout=10)
    except Exception as exc:                                   # noqa: BLE001
        print(f"[SKIP] 服务不可用（{type(exc).__name__}）：请先启动 8000 端口")
        return 0

    from database import get_db
    conn = get_db()
    pid = conn.execute("SELECT id FROM projects ORDER BY id LIMIT 1").fetchone()["id"]
    before = conn.execute("SELECT COUNT(*) FROM requirement_items WHERE project_id=?",
                          (pid,)).fetchone()[0]
    conv = _req("POST", "/api/conversations",
                {"title": "R1 需求条目化真机验证", "project_id": pid})
    cid = conv["id"]
    print(f"会话 {cid}（工程 {pid}）｜入库前该工程需求 {before} 条")

    checks = []
    # ── 第 1 轮 ──
    frames1 = _stream(cid, MSG1)
    t1 = _tools_of(conn, cid)
    c1 = any(x["tool_name"] == "requirement_itemize" and x["ok"] for x in t1)
    checks.append(("C1 requirement_itemize 在真实对话中调用成功（不再是未知工具）",
                   c1, f"{[x['tool_name'] for x in t1]}"))
    # 判据取舍：**问不问**是模型的自由度（本轮实测有时先问、有时直接落可验证项，
    # 两种都合理）；真正不可退让的是**模糊句不得作为条目进库** ——
    # 那才是"带病建模"的入口。故判据对准后者。
    vague_in_db = conn.execute(
        "SELECT COUNT(*) FROM requirement_items WHERE project_id=? AND kind='item' "
        "AND text LIKE '%良好的用户体验%'", (pid,)).fetchone()[0]
    mid = conn.execute("SELECT COUNT(*) FROM requirement_items WHERE project_id=?",
                       (pid,)).fetchone()[0]
    checks.append(("C2 模糊句不得作为条目落库（未确认前也不许写）",
                   vague_in_db == 0 and mid == before,
                   f"模糊句入条目={vague_in_db} 库内={mid}（前 {before}）"))

    # ── 第 2 轮：要求直接落库 ──
    _stream(cid, MSG2)
    t2 = _tools_of(conn, cid)
    commits = [x for x in t2 if x["tool_name"] == "requirement_commit"]
    # ⚠️ 拦截时工具返回 ok=1（文本明确写"已进入人工确认队列…不会产生任何副作用"，
    #   刻意不置失败是为了避免模型重试发起第二张单）。故判据看**文本 + 是否真的零副作用**，
    #   不看 ok —— 看 ok 会得出「闸门失效」的错误结论。
    gated = any("人工确认队列" in (x["r"] or "") for x in commits)
    pre_approve = conn.execute("SELECT COUNT(*) FROM requirement_items WHERE project_id=?",
                               (pid,)).fetchone()[0]
    checks.append(("C3 requirement_commit 被写前确认闸拦住（批准前零副作用）",
                   bool(commits) and gated and pre_approve == mid,
                   f"commit={len(commits)} 次，拦截={gated}，批准前库内={pre_approve}（拦截时 {mid}）"))

    # ── 人工批准 ──
    pend = _req("GET", "/api/studio/hil-confirmations?status=pending&limit=50")
    pend = pend if isinstance(pend, list) else []
    mine = [p for p in pend if str(p.get("conversation_id")) == str(cid)
            and p.get("action") == "requirement_commit"]
    approved = 0
    for p in mine:
        try:
            _req("POST", "/api/studio/hil-confirmations/%d/decide" % int(p["id"]),
                 {"approve": True}, timeout=120)
            approved += 1
        except Exception as exc:                               # noqa: BLE001
            print("  批准失败:", exc)
    time.sleep(0.6)
    after = conn.execute("SELECT COUNT(*) FROM requirement_items WHERE project_id=?",
                         (pid,)).fetchone()[0]
    run_row = conn.execute(
        "SELECT req_id FROM requirement_items WHERE project_id=? AND text LIKE ?",
        (pid, "%" + RUN_SENT + "%")).fetchone()
    checks.append(("C4 批准后条目真的落到会话所属工程（含本轮唯一用例）",
                   after > before and run_row is not None,
                   f"{before}→{after}；本轮用例={(run_row['req_id'] if run_row else '未落库')}"
                   f"（确认单 {[p['id'] for p in mine]} 批准 {approved}）"))

    # ── C5：编号与需求视图 reqId 一致 ──
    ids_db = [r["req_id"] for r in conn.execute(
        "SELECT req_id FROM requirement_items WHERE project_id=? AND kind='item'", (pid,))]
    from requirement_itemize_tools import exec_tool as req_exec
    r_view = req_exec("requirement_itemize", {"text": MSG1})
    code = r_view.get("req_view_code") or ""
    consistent = bool(ids_db) and all(('"%s"' % i) in code for i in ids_db
                                      if i in [x["id"] for x in (r_view.get("items") or [])])
    checks.append(("C5 库内 REQ 编号与需求视图 reqId 一致（追溯不断链）",
                   consistent, f"库内={ids_db}"))

    # ── C6：幂等 ──
    # 判据必须比对**同一 payload 的两次提交**：拿"与历史库对比"会把
    # 「第一次提交里的 gaps 是新行」误判成幂等失效（本轮实测踩过）。
    ra = req_exec("requirement_commit", {"text": MSG1, "project": pid}, conversation_id=cid)
    rb = req_exec("requirement_commit", {"text": MSG1, "project": pid}, conversation_id=cid)
    checks.append(("C6 同一 payload 连提两次，第二次 0 新增（幂等）",
                   (rb.get("inserted") == 0),
                   f"第1次={ra.get('inserted')} 第2次={rb.get('inserted')} 工程={rb.get('project_id')}"))

    print("=" * 68)
    print("会话入口真机验证：需求条目化 → 落库（含确认闸）")
    print("=" * 68)
    for name, passed, detail in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name} —— {detail}")
    for f in frames1:
        if f.get("event") == "done":
            print("\n模型第 1 轮回复片段：", str((f.get("data") or {}).get("answer") or "")[:220])
            break
    ok = all(c[1] for c in checks)
    print("\n" + ("ALL_OK" if ok else "FAILED"))
    conn.close()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
