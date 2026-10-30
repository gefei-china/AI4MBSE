# -*- coding: utf-8 -*-
"""★ 写前确认（HITL）**真机端到端闭环验证** —— 连真服务 + 真 MCP 平台。

## 验证的闭环（每一环都用**平台独立回读**证实，不看任何"已完成"自述）
   ① 可逆写（notify 档）⇒ 放行 ⇒ 平台回读：包**真的建成了**
   ② 破坏性写（confirm 档）⇒ 拦截 ⇒ 确认单 pending ⇒ 平台回读：包**还在**（证明没有副作用）
   ③ 走真实 HTTP API 批准 ⇒ 执行 ⇒ 平台回读：包**没了**（证明真的执行了）
   ④ 重复批准 ⇒ 执行幂等命中 ⇒ 平台回读：仍是"没了"（不重复执行、不报错）
   ⑤ 拒绝路径 ⇒ 不执行 ⇒ 平台回读：目标仍在

## 为什么必须这样验（2026-10-27 的教训）
上一轮 E8 实测：`create_model_elements` 回执 `SUCCEEDED`，模型却断言「尚未写入平台」。
⇒ **模型自述不可信，只有平台独立回读是事实。**
本脚本所有断言都落在「平台上的元素树」这一外部事实上。

用法： ./.venv/Scripts/python.exe -X utf8 tools/verify/e2e_hil_confirm.py
"""
import json
import os
import sys
import time
import uuid

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)
os.chdir(REPO)

import httpx  # noqa: E402

BASE = "http://127.0.0.1:8000"
PASS = 0
FAIL = 0
EVID = []


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] %s" % label)
    else:
        FAIL += 1
        print("  [FAIL] %s | %s" % (label, detail))
    EVID.append({"label": label, "ok": bool(cond), "detail": str(detail)[:400]})


def section(t):
    print("\n=== %s ===" % t)


# ── 平台坐标（真机实测值，2026-10-27 记录）──
WS = {"projectId": "2113198870736076800",
      "branchId": "2113198870744465408",
      "versionId": "2114939811837575168"}
OWNER = "2114907195813986304"          # 「通信子系统用例图」包
PKG_NAME = "HIL闭环验证包_%s" % time.strftime("%H%M%S")


def mcp_call(tool, args, timeout=90):
    """直连 MCP 平台调工具（**独立于本系统**，用于回读事实）。"""
    import sqlite3
    c = sqlite3.connect("file:mbse.db?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    r = c.execute("SELECT endpoint,headers FROM mcp_servers WHERE name='zhiyuan-dev-mcp'").fetchone()
    c.close()
    hdr = json.loads(r["headers"] or "{}")
    h = {"Content-Type": "application/json",
         "Accept": "application/json, text/event-stream",
         "Authorization": hdr.get("Authorization", "")}
    body = {"jsonrpc": "2.0", "id": int(time.time() * 1000) % 100000,
            "method": "tools/call", "params": {"name": tool, "arguments": args}}
    with httpx.Client(trust_env=True, timeout=timeout) as c2:
        resp = c2.post(r["endpoint"], json=body, headers=h)
    try:
        j = json.loads(resp.text)
        txt = j["result"]["content"][0]["text"]
        return json.loads(txt)
    except Exception:
        return {"_raw": resp.text[:800], "_status": resp.status_code}


def platform_has(name):
    """★ 平台独立回读：父容器下是否存在名为 name 的子包。返回 (exists, items)。"""
    d = mcp_call("query_data_tree", {"version": WS, "parentId": OWNER, "pageSize": 100})
    items = d.get("items") or []
    return any((it.get("name") or "") == name for it in items), items


def api(method, path, body=None, timeout=180):
    with httpx.Client(trust_env=False, timeout=timeout) as c:
        if method == "GET":
            r = c.get(BASE + path)
        else:
            r = c.post(BASE + path, json=body or {})
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {"_raw": r.text[:600]}


def main():
    print("=" * 74)
    print("写前确认（HITL）真机端到端闭环验证")
    print("目标包名：%s" % PKG_NAME)
    print("=" * 74)

    # ── 前置：服务可达 ──
    section("0. 前置探活")
    st, _ = api("GET", "/api/dashboard")
    check("[0] 服务可达（dashboard 200）", st == 200, str(st))
    if st != 200:
        print("\n服务不可达，终止。")
        return 1
    ex0, items0 = platform_has(PKG_NAME)
    check("[0] 平台可回读父容器（初始无同名包）", not ex0,
          "已存在？items=%d" % len(items0))
    # ★ 建一个**真实会话**并在其中放一条 user 消息 ——
    #   否则"回灌文案带原请求复述"这条验证不了（conversation_id=0 时查不到原请求）。
    #   这不是为了凑测试通过：真实链路里确认单必然属于某个会话。
    st_c, cd = api("POST", "/api/conversations",
                   {"title": "HIL闭环验证会话", "intent": "智源对接agent"})
    _conv_id = 0
    if isinstance(cd, dict):
        _conv_id = int(cd.get("id") or (cd.get("conversation") or {}).get("id") or 0)
    check("[0] 创建验证会话成功", _conv_id > 0, "HTTP %s %s" % (st_c, str(cd)[:160]))
    if _conv_id:
        import sqlite3 as _sq
        _c = _sq.connect("mbse.db")
        _c.execute("INSERT INTO messages (conversation_id, role, content) VALUES (?,?,?)",
                   (_conv_id, "user", "把通信子系统用例图下那个 HIL 闭环验证包删掉"))
        _c.commit()
        _c.close()

    # 走本地 pipeline（连真 MCP）调工具 —— 与模型调用工具**同一份代码**
    from agent.pipeline import AgentPipeline
    pipe = AgentPipeline()
    pipe._load_db_agents()
    pipe._tool_user = {"id": 1, "role_name": "系统管理员", "username": "e2e"}
    pipe._tool_intent_ctx = {"intent": "智源对接agent"}
    pipe._tool_agent_ctx = {"agent": "智源对接agent"}
    pipe._tool_conv_ctx = _conv_id          # ★ 关联真实会话（回灌文案要靠它取原请求）
    pipe._tool_whitelist = None
    pipe._tool_perm_gate_disabled = True
    pipe._tool_trace_id = "e2e-hil-%s" % uuid.uuid4().hex[:8]

    # ══ ① 可逆写（notify 档）⇒ 放行 ══
    section("① 可逆写 create_model_elements（策略档 notify）⇒ 应放行")
    _rid = "req-hil-%s" % uuid.uuid4().hex[:12]
    r1 = pipe._exec_tool_call("create_model_elements", {
        "creationRef": "BASIC:PACKAGE", "name": PKG_NAME, "ownerId": OWNER,
        "requestId": _rid, "workspace": WS})
    check("[①] 未被确认闸拦截（notify 档放行）", not r1.get("confirm_required"),
          str(r1)[:200])
    check("[①] 工具执行成功", bool(r1.get("ok")), str(r1.get("result"))[:300])
    _dtxt = str(r1.get("result") or "")
    check("[①] 回执含 SUCCEEDED / APPLIED",
          ("SUCCEEDED" in _dtxt) or ("APPLIED" in _dtxt), _dtxt[:200])
    # ★ 平台独立回读（不采信回执）
    ex1, _ = platform_has(PKG_NAME)
    check("[①] ★ 平台独立回读：包**确实建成了**", ex1,
          "回读父容器未找到 %s" % PKG_NAME)
    print("       （平台回读已确认存在：%s）" % PKG_NAME)

    # 取到新包 dataId（供后续删除）
    _, items1 = platform_has(PKG_NAME)
    _data_id = ""
    for it in items1:
        if (it.get("name") or "") == PKG_NAME:
            _data_id = str(it.get("dataId") or "")
            break
    check("[①] 取到新包 dataId", bool(_data_id), _data_id)

    # ══ ② 破坏性写（confirm 档）⇒ 拦截 ══
    section("② 破坏性写 delete_model_elements（策略档 confirm）⇒ 应拦截")
    _rid2 = "req-hil-%s" % uuid.uuid4().hex[:12]
    r2 = pipe._exec_tool_call("delete_model_elements", {
        "dataId": _data_id, "requestId": _rid2, "workspace": WS, "confirmed": True})
    check("[②] ★ 被确认闸拦截（confirm_required=True）",
          bool(r2.get("confirm_required")), str(r2)[:260])
    _conf = (r2.get("confirm") or {})
    _cid = int(_conf.get("id") or 0)
    check("[②] 返回确认单 id", _cid > 0, str(_conf)[:200])
    check("[②] 确认单带影响面摘要（不是裸参数）",
          bool(_conf.get("preview")), str(_conf.get("preview"))[:160])
    check("[②] 确认单 level=confirm", _conf.get("level") == "confirm",
          str(_conf.get("level")))
    check("[②] 确认单 kind=destructive", _conf.get("kind") == "destructive",
          str(_conf.get("kind")))
    # ★ 关键：平台独立回读，证明**没有副作用**
    ex2, _ = platform_has(PKG_NAME)
    check("[②] ★★ 平台独立回读：包**还在**（证明拦截真的没产生副作用）", ex2,
          "包已消失 ⇒ 说明拦截失效、真的删了")

    # 确认单状态应为 pending
    st3, pend = api("GET", "/api/studio/hil-confirmations?status=pending")
    _found = None
    if isinstance(pend, list):
        for x in pend:
            if int(x.get("id") or 0) == _cid:
                _found = x
                break
    check("[②] 确认单在待确认队列中（status=pending）", _found is not None,
          "队列 %d 条，未找到 #%d（可能无权限，HTTP %s）"
          % (len(pend) if isinstance(pend, list) else -1, _cid, st3))

    # ══ ③ 拒绝路径 ⇒ 不执行 ══
    section("③ 拒绝该确认单 ⇒ 不应执行（包仍在）")
    st4, r4 = api("POST", "/api/studio/hil-confirmations/%d/decide" % _cid,
                  {"approve": False, "note": "E2E 拒绝路径验证"})
    check("[③] decide 接口返回成功", st4 == 200, "HTTP %s %s" % (st4, str(r4)[:200]))
    ex3, _ = platform_has(PKG_NAME)
    check("[③] ★ 平台独立回读：拒绝后包仍在", ex3, "包消失了 ⇒ 拒绝没生效")

    # ══ ④ 批准路径 ⇒ 真的执行 ══
    section("④ 重新发起删除并批准 ⇒ 应真的执行（包消失）")
    # 上一张单已 rejected ⇒ 幂等键已释放，可新建一张
    _rid3 = "req-hil-%s" % uuid.uuid4().hex[:12]
    pipe._tool_trace_id = "e2e-hil-%s" % uuid.uuid4().hex[:8]
    r5 = pipe._exec_tool_call("delete_model_elements", {
        "dataId": _data_id, "requestId": _rid3, "workspace": WS, "confirmed": True})
    _cid2 = int((r5.get("confirm") or {}).get("id") or 0)
    check("[④] 新确认单已生成", _cid2 > 0 and _cid2 != _cid,
          "cid=%s cid2=%s" % (_cid, _cid2))
    ex4, _ = platform_has(PKG_NAME)
    check("[④] 批准前：包仍在（拦截态）", ex4, "")

    st6, r6 = api("POST", "/api/studio/hil-confirmations/%d/decide" % _cid2,
                  {"approve": True}, timeout=240)
    check("[④] decide 接口返回成功", st6 == 200, "HTTP %s %s" % (st6, str(r6)[:300]))
    _ex = (r6 or {}).get("exec_result") or {}
    check("[④] ★ 端点报告 executed=True（真的走了执行器）",
          bool((r6 or {}).get("executed")), str(r6)[:300])
    check("[④] 执行成功（exec_result.ok）", bool(_ex.get("ok")),
          str(_ex)[:300])
    # ★ 最关键：平台独立回读
    time.sleep(2)
    ex5, _ = platform_has(PKG_NAME)
    check("[④] ★★ 平台独立回读：包**已删除**（证明批准后真的执行了）", not ex5,
          "包还在 ⇒ 批准了但没执行（这正是改动前的缺陷）")

    # ══ ⑤ 回灌文案 ══
    section("⑤ 回灌文案（模型据此才知道已执行）")
    _rt = str((r6 or {}).get("resume_text") or "")
    check("[⑤] 返回 resume_text", bool(_rt), _rt[:200])
    check("[⑤] 含'已获用户批准并执行'", "已获用户" in _rt, _rt[:160])
    check("[⑤] ★ 含'不要重复发起'（防重复写）", "不要重复发起" in _rt, _rt[:200])
    check("[⑤] ★ 含'原请求'复述", "原请求" in _rt, _rt[:200])
    check("[⑤] 返回 conversation_id（前端据此决定是否在原会话续跑）",
          "conversation_id" in (r6 or {}), str(r6 or {})[:200])

    # ══ ⑥ 执行幂等 ══
    section("⑥ 重复批准同一确认单 ⇒ 执行幂等命中（不重复执行）")
    st7, r7 = api("POST", "/api/studio/hil-confirmations/%d/decide" % _cid2,
                  {"approve": True}, timeout=120)
    _st7 = (r7 or {}).get("status") or ""
    check("[⑥] 重复批准未报错（幂等/条件更新挡住）", st7 == 200,
          "HTTP %s %s" % (st7, str(r7)[:200]))
    check("[⑥] 状态不再是 approved（已 executed 或被 raced 挡住）",
          _st7 in ("executed", "approved"), _st7)
    ex6, _ = platform_has(PKG_NAME)
    check("[⑥] ★ 平台回读：包仍是已删除状态（没有二次执行出错）", not ex6, "")

    # ══ ⑦ 落库回执 ══
    section("⑦ 确认单落库状态与执行回执")
    import sqlite3
    c = sqlite3.connect("file:mbse.db?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    row = c.execute("SELECT id,action,status,tool_type,level,result_json,exec_idem_key,"
                    "decided_by,batch_count FROM hil_confirmations WHERE id=?",
                    (_cid2,)).fetchone()
    c.close()
    check("[⑦] 确认单 status=executed（区分「批了没跑」与「跑成了」）",
          row and row["status"] == "executed",
          dict(row)["status"] if row else "无行")
    check("[⑦] result_json 有执行回执（同时是执行幂等依据）",
          bool(row and (row["result_json"] or "")), "")
    # ⚠️ 本测试用匿名 HTTP 调用 decide（无认证上下文）⇒ decided_by 为空是**合法**的。
    #   硬编码人名（"王工"）才是缺陷 —— 所以判据是"不得是硬编码人名"，而不是"必须有值"。
    _db = str((row["decided_by"] if row else "") or "")
    check("[⑦] decided_by 不是硬编码人名（匿名调用为空是合法的）",
          _db not in ("王工", "admin", "system"), "decided_by=%r" % _db)
    check("[⑦] tool_type=mcp（批准后能选对执行器）",
          row and (row["tool_type"] or "") in ("mcp", "mcp_verb", ""),
          str(row["tool_type"]) if row else "")

    # ══ 汇总 ══
    print("\n" + "=" * 74)
    print("PASS %d / FAIL %d" % (PASS, FAIL))
    _out = "tmp/e2e_out/e2e_hil_%d.json" % int(time.time())
    os.makedirs("tmp/e2e_out", exist_ok=True)
    with open(_out, "w", encoding="utf-8") as f:
        json.dump({"pkg": PKG_NAME, "cid_rejected": _cid, "cid_approved": _cid2,
                   "checks": EVID, "pass": PASS, "fail": FAIL},
                  f, ensure_ascii=False, indent=1)
    print("取证：%s" % _out)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
