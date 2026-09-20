"""D11 A2A 协议互通 + 事件回调 闭环验证（8.2 API 网关层「事件回调」+ 8.3 A2A 标准化互通）。

T1. 节点事件发射：注册订阅 → 运行流程 → Mock HTTP Server 收到 node_done 事件（A2A 结构：protocol/kind/event.type/run_id/node_id/sender）
T2. HMAC 签名：订阅带 secret → Mock 收到 X-A2A-Signature，同密钥重算比对一致
T3. webhook 节点执行：流程含 webhook 节点（url=Mock）→ 收到 A2A message、节点 done、data 含 status_code
T4. A2A 入站消息：agent_messages 可存 sender 身份字段，pubsub 订阅可认领
T5. 回调失败不阻断：订阅不可达 URL → 运行仍 completed，订阅标记 failed
T6. run_completed 事件：运行结束后 Mock 收到 run_completed（含 status/error_count）
"""
import hashlib
import hmac
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["MBSE_LLM_FORCE_MOCK"] = "1"
_TEST_DB = os.path.join(os.path.dirname(__file__), "_d11_test.db")
if os.path.exists(_TEST_DB):
    os.remove(_TEST_DB)
os.environ["MBSE_DB_PATH"] = _TEST_DB

from database import db_conn, init_db  # noqa: E402
from repositories.studio_repo import StudioRepo  # noqa: E402
from workflows import FlowExecutor  # noqa: E402

init_db()

PASS = 0
FAIL = 0


def chk(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  → {detail}")


# ── Mock HTTP Server：记录收到的请求（body + headers）供断言 ──
RECEIVED = []
RECEIVED_LOCK = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def _record(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        with RECEIVED_LOCK:
            RECEIVED.append({"path": self.path, "method": self.command,
                             "body": body, "headers": dict(self.headers)})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def do_POST(self):
        self._record()

    def do_PUT(self):
        self._record()

    def do_GET(self):
        self._record()

    def log_message(self, *args):
        pass


_server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
MOCK_PORT = _server.server_address[1]
MOCK_URL = f"http://127.0.0.1:{MOCK_PORT}/callback"
_t = threading.Thread(target=_server.serve_forever, daemon=True)
_t.start()


def code_node(nid, code, label=None):
    return {"id": nid, "type": "code", "label": label or f"代码{nid}",
            "config": {"language": "python", "timeout": 10, "inputs": {}, "code": code}}


def webhook_node(nid, url, payload=None, secret="", label=None):
    cfg = {"method": "POST", "url": url, "timeout": 10}
    if payload:
        cfg["payload"] = payload
    if secret:
        cfg["secret"] = secret
    return {"id": nid, "type": "webhook", "label": label or "回调", "config": cfg}


def create_flow(conn, name, nodes, edges=None):
    return StudioRepo(conn).create_agent_flow(
        name, "d11", json.dumps(nodes, ensure_ascii=False),
        json.dumps(edges or [], ensure_ascii=False))


def add_sub(conn, event_type, url, run_id=0, node_id="", secret=""):
    conn.execute("INSERT INTO flow_event_subscriptions (run_id, node_id, event_type, webhook_url, secret) "
                 "VALUES (?,?,?,?,?)", (run_id, node_id, event_type, url, secret))
    conn.commit()


def find_events(etype, node_id=None, run_id=None):
    """从 RECEIVED 中过滤指定类型事件（body 为 A2A event JSON）。"""
    out = []
    for r in RECEIVED:
        try:
            d = json.loads(r["body"])
        except Exception:
            continue
        if d.get("kind") != "event":
            continue
        ev = d.get("event") or {}
        if ev.get("type") != etype:
            continue
        if node_id is not None and ev.get("node_id") != node_id:
            continue
        if run_id is not None and ev.get("run_id") != run_id:
            continue
        out.append((r, d))
    return out


# ── T1+T2+T6: 节点事件发射 + 签名 + run_completed ──
print("== T1/T2/T6: 事件发射（node_done）+ HMAC 签名 + run_completed ==")
with db_conn() as conn:
    fid = create_flow(conn, "D11-事件发射", [
        code_node("n1", "_out = {'msg': '事件发射验证', 'n': 42}")])
    # 全局订阅 + 带签名订阅（node_done 全节点）
    add_sub(conn, "node_done", MOCK_URL, run_id=0)
    add_sub(conn, "node_done", MOCK_URL, run_id=0, secret="secret-key-001")
    add_sub(conn, "run_completed", MOCK_URL, run_id=0)
    before = len(RECEIVED)
    ex = FlowExecutor(conn)
    result = ex.run([code_node("n1", "_out = {'msg': '事件发射验证', 'n': 42}")], [],
                    {}, conn, persist=True, flow_id=fid, flow_name="D11-事件发射")
    rid = result["run_id"]
    after = len(RECEIVED)

    node_events = find_events("node_done", node_id="n1")
    chk("T1 node_done 事件已发射", len(node_events) >= 1, f"收到 {len(node_events)} 条")
    if node_events:
        r, d = node_events[0]
        chk("T1 A2A 结构 protocol/kind", d.get("protocol") == "a2a" and d.get("kind") == "event",
            f"{json.dumps(d, ensure_ascii=False)[:120]}")
        chk("T1 event.type==node_done", (d.get("event") or {}).get("type") == "node_done")
        chk("T1 event.run_id 关联", (d.get("event") or {}).get("run_id") == rid)
        chk("T1 event.node_id==n1", (d.get("event") or {}).get("node_id") == "n1")
        chk("T1 sender 身份卡", (d.get("sender") or {}).get("name") == "mbse-workflow")
    # 签名：找到带 X-A2A-Signature 的 node_done 请求，用同密钥重算比对
    signed = [x for x in RECEIVED[before:after] if x["headers"].get("X-A2A-Signature")]
    chk("T2 签名请求存在", len(signed) >= 1, f"signed={len(signed)}")
    if signed:
        body = signed[0]["body"]
        expect = hmac.new(b"secret-key-001", body, hashlib.sha256).hexdigest()
        got = signed[0]["headers"]["X-A2A-Signature"]
        chk("T2 HMAC-SHA256 签名校验通过", got == expect, f"expect={expect[:16]} got={got[:16]}")
    rc_events = find_events("run_completed")
    chk("T6 run_completed 事件已发射", len(rc_events) >= 1, f"收到 {len(rc_events)} 条")
    if rc_events:
        ev = rc_events[0][1].get("event") or {}
        payload = ev.get("payload") or {}
        chk("T6 run_completed 含状态", payload.get("status") == result["status"]
            and payload.get("error_count") == len(result.get("errors", [])),
            f"{json.dumps(ev, ensure_ascii=False)[:160]}")
    # 订阅状态已更新（最近回调时间 + 状态码）
    row = conn.execute("SELECT last_status_code, last_event_at FROM flow_event_subscriptions "
                       "WHERE event_type='node_done' AND secret='' ORDER BY id LIMIT 1").fetchone()
    chk("T1 订阅 last_status_code 已记录", row and row["last_status_code"] == 200,
        f"last_status_code={row and row['last_status_code']}")

# ── T3: webhook 节点（A2A 出站消息）──
print("== T3: webhook 节点执行（A2A message 出站）==")
with db_conn() as conn:
    RECEIVED.clear()
    nodes = [
        code_node("n1", "_out = {'result': '需求基线已固化'}", label="生成"),
        webhook_node("n2", MOCK_URL + "/webhook",
                     payload="{{n1.data.outputs}}", secret="wh-secret", label="A2A 回调"),
    ]
    fid = create_flow(conn, "D11-webhook节点", nodes)
    ex = FlowExecutor(conn)
    result = ex.run(nodes, [{"source": "n1", "target": "n2"}], {},
                    conn, persist=True, flow_id=fid, flow_name="D11-webhook节点")
    n2 = result["results"].get("n2", {})
    chk("T3 webhook 节点 done", n2.get("status") == "done", f"status={n2.get('status')}")
    chk("T3 回调响应透传 status_code", (n2.get("data") or {}).get("status_code") == 200,
        f"data={json.dumps(n2.get('data', {}), ensure_ascii=False)[:120]}")
    msgs = [x for x in RECEIVED if x["path"].endswith("/webhook")]
    chk("T3 Mock 收到 A2A message", len(msgs) >= 1, f"收到 {len(msgs)} 条")
    if msgs:
        d = json.loads(msgs[0]["body"])
        chk("T3 message 结构 protocol/kind", d.get("protocol") == "a2a" and d.get("kind") == "message",
            f"{json.dumps(d, ensure_ascii=False)[:140]}")
        chk("T3 message 模板引用解析", "需求基线已固化" in json.dumps(d, ensure_ascii=False),
            f"contents={json.dumps(d.get('message', {}), ensure_ascii=False)[:120]}")
        chk("T3 webhook 签名头", msgs[0]["headers"].get("X-A2A-Signature") is not None)
        chk("T3 sender 身份卡", (d.get("sender") or {}).get("name") == "A2A 回调")

# ── T4: A2A 入站消息（agent_messages sender 字段 + pubsub 认领）──
print("== T4: A2A 入站消息 → 消息池 → pubsub 订阅认领 ==")
with db_conn() as conn:
    ex = FlowExecutor(conn)
    ex.run_id = 7777
    # 模拟 a2a_inbound 写入：标准 A2A message + sender 身份卡
    a2a_body = {
        "protocol": "a2a", "version": "0.1",
        "message": {"id": "msg-ext-1", "kind": "text",
                    "contents": [{"text": "外部协作结果: 链路预算通过"}],
                    "context": {"topic": "ext_result"}},
        "sender": {"name": "外部设计Agent", "type": "external", "role": "worker"},
    }
    topic = "ext_result"
    contents = a2a_body["message"]["contents"] or []
    content = contents[0].get("text") if contents and isinstance(contents[0], dict) else None
    sender = a2a_body.get("sender") or {}
    conn.execute(
        "INSERT INTO agent_messages (run_id, seq, topic, content_json, publisher_node, claimed, "
        "sender_name, sender_type, sender_role) VALUES (?,?,?,?,?,0,?,?,?)",
        (7777, 1, topic, json.dumps({"text": content}, ensure_ascii=False), "a2a-inbound",
         sender.get("name", "external"), sender.get("type", "external"), sender.get("role", "agent")))
    conn.commit()
    row = conn.execute("SELECT * FROM agent_messages WHERE run_id=7777").fetchone()
    chk("T4 A2A 身份字段落库", row and row["sender_name"] == "外部设计Agent"
        and row["sender_type"] == "external" and row["sender_role"] == "worker",
        f"sender={row and (row['sender_name'], row['sender_type'], row['sender_role'])}")
    # pubsub 订阅认领该消息（同 run_id + topic）
    out = ex._exec_pubsub("订阅", {"op": "subscribe", "topic": "ext_result",
                                   "write_key": "ext_msg"}, {}, {}, conn, {})
    chk("T4 pubsub 认领 A2A 消息", out.get("status") == "done" and out.get("data", {}).get("claimed"),
        f"content={out.get('content', '')[:60]}")
    chk("T4 认领内容一致", "链路预算通过" in out.get("content", ""), out.get("content", "")[:60])

# ── T5: 回调失败不阻断主流程 ──
print("== T5: 回调失败不阻断主流程 ==")
with db_conn() as conn:
    RECEIVED.clear()
    add_sub(conn, "node_done", "http://127.0.0.1:1/unreachable", run_id=0)
    fid = create_flow(conn, "D11-失败不阻断", [code_node("n1", "_out = {'v': 1}")])
    ex = FlowExecutor(conn)
    result = ex.run([code_node("n1", "_out = {'v': 1}")], [], {}, conn,
                    persist=True, flow_id=fid, flow_name="D11-失败不阻断")
    chk("T5 运行仍 completed", result["status"] == "completed", f"status={result['status']}")
    bad = conn.execute("SELECT status, last_error FROM flow_event_subscriptions "
                       "WHERE webhook_url LIKE '%unreachable%'").fetchone()
    chk("T5 订阅标记 failed", bad and bad["status"] == "failed",
        f"status={bad and bad['status']}")
    chk("T5 last_error 已记录", bool(bad and bad["last_error"]), f"err={bad and bad['last_error']}")

_server.shutdown()
print(f"\n结果: {PASS} 通过, {FAIL} 失败")
sys.exit(0 if FAIL == 0 else 1)
