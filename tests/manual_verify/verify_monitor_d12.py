"""D12 统一监控平台 闭环验证（8.2「监控与日志面板」+「监控面板和性能分析工具」+ 11.3 运维监控）。

T1. dashboard 真实聚合：手工造 flow_runs/llm_usage_stats/flow_run_steps → /api/monitor/dashboard 返回真实统计
T2. 趋势按天补齐：days=7 → trend 长度 7，无数据日期补 0，有数据日正确
T3. 日志检索：run_id / node_type / status / 关键词 q 四种过滤条件生效
T4. 告警规则 CRUD + toggle：POST/GET/PUT/DELETE/toggle 全链路
T5. 执行器阈值触发：成功流程 success_rate=100 不触发；失败流程触发 success_rate/error_count；慢流程触发 avg_latency
T6. 告警列表 + ack：/api/monitor/alerts 过滤 + ack 后状态变更
T7. webhook 通知：规则带 notify_url → Mock 收到 A2A alert_triggered 事件（含 HMAC 签名）
T8. 规则暂停后不再触发：toggle paused → 再跑失败流程 → 无新事件且无通知
"""
import hashlib
import hmac
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["MBSE_LLM_FORCE_MOCK"] = "1"
_TEST_DB = os.path.join(os.path.dirname(__file__), "_d12_test.db")
if os.path.exists(_TEST_DB):
    os.remove(_TEST_DB)
os.environ["MBSE_DB_PATH"] = _TEST_DB

from database import db_conn, init_db  # noqa: E402
from repositories.studio_repo import StudioRepo  # noqa: E402
from workflows import FlowExecutor  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
import main  # noqa: E402

init_db()
client = TestClient(main.app)

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


# ── Mock HTTP Server：记录告警 webhook 通知 ──
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

    def log_message(self, *args):
        pass


_server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
MOCK_PORT = _server.server_address[1]
MOCK_URL = f"http://127.0.0.1:{MOCK_PORT}/alert"
_t = threading.Thread(target=_server.serve_forever, daemon=True)
_t.start()


def code_node(nid, code, label=None):
    return {"id": nid, "type": "code", "label": label or f"代码{nid}",
            "config": {"language": "python", "timeout": 10, "inputs": {}, "code": code}}


def create_flow(conn, name, nodes, edges=None):
    return StudioRepo(conn).create_agent_flow(
        name, "d12", json.dumps(nodes, ensure_ascii=False),
        json.dumps(edges or [], ensure_ascii=False))


# ══════════ T1/T2/T3：dashboard 真实聚合 + 趋势 + 日志检索 ══════════
print("== T1/T2/T3: dashboard 聚合 + 趋势 + 日志检索 ==")
with db_conn() as conn:
    # 造运行数据：3 次（2 成功 1 失败），含 LLM 用量（2 真实 1 mock）
    for i, (status, ms) in enumerate([("completed", 200), ("completed", 400), ("partial", 600)]):
        conn.execute("INSERT INTO flow_runs (flow_id, flow_name, status, order_json, total_latency_ms, error_count) "
                     "VALUES (?,?,?,?,?,?)", (i + 1, f"F{i}", status, "[]", ms, 0 if status == "completed" else 1))
    for i, (mock, tok) in enumerate([(0, 1000), (0, 2000), (1, 300)]):
        conn.execute("INSERT INTO llm_usage_stats (provider_name, model_name, intent, used_mock, "
                     "prompt_tokens, completion_tokens, total_tokens, estimated_cost) "
                     "VALUES (?,?,?,?,?,?,?,?)",
                     ("mock" if mock else "provider", f"model{i}", "req", mock, tok, 0, tok, 0.0))
    conn.execute("INSERT INTO flow_run_steps (run_id, seq, node_id, node_type, node_label, status, content, data, latency_ms) "
                 "VALUES (1,1,'n1','code','节点A','done','{\"r\": 1}','{}',50)")
    conn.execute("INSERT INTO flow_run_steps (run_id, seq, node_id, node_type, node_label, status, content, data, latency_ms) "
                 "VALUES (1,2,'n2','llm','节点B','error','模拟失败','{}',120)")
    conn.commit()

dash = client.get("/api/monitor/dashboard?days=7").json()
runs = dash.get("runs", {})
chk("T1 runs.total=3", runs.get("total") == 3, f"total={runs.get('total')}")
chk("T1 runs.success/failed", runs.get("success") == 2 and runs.get("failed") == 1,
    f"success={runs.get('success')} failed={runs.get('failed')}")
chk("T1 success_rate≈66.7", abs((runs.get("success_rate") or 0) - 66.7) < 0.2,
    f"rate={runs.get('success_rate')}")
chk("T1 avg_latency=400", runs.get("avg_latency_ms") == 400, f"avg={runs.get('avg_latency_ms')}")
llm = dash.get("llm", {})
chk("T1 llm.calls=3", llm.get("calls") == 3, f"calls={llm.get('calls')}")
chk("T1 llm.mock_rate≈33.3", abs((llm.get("mock_rate") or 0) - 33.3) < 0.2, f"mock_rate={llm.get('mock_rate')}")
chk("T1 llm.total_tokens=3300", llm.get("total_tokens") == 3300, f"tokens={llm.get('total_tokens')}")
chk("T1 system 含 pid/uptime/db_size", (dash.get("system") or {}).get("pid") == os.getpid()
    and "db_size_kb" in dash.get("system", {}), f"system={dash.get('system')}")
trend = dash.get("trend", [])
chk("T2 trend 长度=days", len(trend) == 7, f"len={len(trend)}")
chk("T2 trend 有数据日 runs 聚合", sum(t.get("runs", 0) for t in trend) == 3,
    f"sum={sum(t.get('runs', 0) for t in trend)}")
chk("T2 trend 无数据日补 0", min(t.get("runs", -1) for t in trend) >= 0)
node_stats = dash.get("node_stats", [])
chk("T2 node_stats 含 code/llm 耗时", any(n.get("type") == "code" for n in node_stats)
    and any(n.get("type") == "llm" for n in node_stats), f"stats={node_stats[:4]}")
if node_stats:
    code_s = next((n for n in node_stats if n.get("type") == "code"), None)
    chk("T2 code 节点 avg_ms≈50", code_s and code_s.get("avg_ms") == 50, f"avg_ms={code_s and code_s.get('avg_ms')}")
    llm_s = next((n for n in node_stats if n.get("type") == "llm"), None)
    chk("T2 llm 节点 errors=1", llm_s and llm_s.get("errors") == 1, f"errors={llm_s and llm_s.get('errors')}")

logs = client.get("/api/monitor/logs?run_id=1").json()
chk("T3 run_id 过滤", logs.get("total") == 2, f"total={logs.get('total')}")
logs2 = client.get("/api/monitor/logs?node_type=llm").json()
chk("T3 node_type 过滤", logs2.get("total") == 1, f"total={logs2.get('total')}")
logs3 = client.get("/api/monitor/logs?status=error").json()
chk("T3 status 过滤", logs3.get("total") == 1, f"total={logs3.get('total')}")
logs4 = client.get("/api/monitor/logs?q=%E8%8A%82%E7%82%B9A").json()  # 关键词「节点A」
chk("T3 关键词过滤", logs4.get("total") == 1, f"total={logs4.get('total')}")
if logs.get("logs"):
    chk("T3 日志含 flow_name 联查", logs["logs"][0].get("flow_name") is not None,
        f"flow_name={logs['logs'][0].get('flow_name')}")

# ══════════ T4：告警规则 CRUD + toggle ══════════
print("== T4: 告警规则 CRUD + toggle ==")
r = client.post("/api/monitor/alert-rules", json={
    "name": "成功率过低", "metric": "success_rate", "operator": "<",
    "threshold": 100, "level": "warning", "status": "active"})
chk("T4 创建规则", r.status_code == 200 and r.json().get("id", 0) > 0, f"{r.text}")
rid1 = r.json()["id"]
r = client.post("/api/monitor/alert-rules", json={
    "name": "耗时过高", "metric": "avg_latency", "operator": ">",
    "threshold": 100, "level": "critical", "status": "active"})
rid2 = r.json()["id"]
r = client.post("/api/monitor/alert-rules", json={
    "name": "错误数", "metric": "error_count", "operator": ">",
    "threshold": 0, "level": "warning", "status": "active"})
rid3 = r.json()["id"]
rules = client.get("/api/monitor/alert-rules").json()
chk("T4 规则列表=3", len(rules) == 3, f"len={len(rules)}")
r = client.put(f"/api/monitor/alert-rules/{rid1}", json={
    "name": "成功率过低(改)", "metric": "success_rate", "operator": "<",
    "threshold": 100, "level": "critical", "notify_url": "", "secret": "", "status": "active"})
chk("T4 更新规则", r.status_code == 200, f"{r.text}")
got = client.get("/api/monitor/alert-rules").json()
chk("T4 更新生效", next((x for x in got if x["id"] == rid1), {}).get("name") == "成功率过低(改)",
    f"{next((x for x in got if x['id'] == rid1), {}).get('name')}")
r = client.post(f"/api/monitor/alert-rules/{rid1}/toggle")
chk("T4 toggle→paused", r.json().get("status") == "paused", f"{r.text}")
r = client.post(f"/api/monitor/alert-rules/{rid1}/toggle")
chk("T4 toggle→active", r.json().get("status") == "active", f"{r.text}")
r = client.delete(f"/api/monitor/alert-rules/{rid3}")
chk("T4 删除规则", r.status_code == 200)
chk("T4 删除后列表=2", len(client.get("/api/monitor/alert-rules").json()) == 2)
# 重建 error_count 规则（T5 需用它验证）
r = client.post("/api/monitor/alert-rules", json={
    "name": "错误数", "metric": "error_count", "operator": ">",
    "threshold": 0, "level": "warning", "status": "active"})
rid3 = r.json()["id"]
# 删除测试用临时规则（不干扰 T5 的三条核心规则）
r = client.post("/api/monitor/alert-rules", json={
    "name": "临时删除", "metric": "success_rate", "operator": "<", "threshold": 100})
rid_tmp = r.json()["id"]
r = client.delete(f"/api/monitor/alert-rules/{rid_tmp}")
chk("T4 临时规则删除", r.status_code == 200 and len(client.get("/api/monitor/alert-rules").json()) == 3)
r = client.post("/api/monitor/alert-rules", json={
    "name": "非法指标", "metric": "unknown_metric", "operator": ">", "threshold": 1})
chk("T4 非法指标被拒", r.status_code == 400, f"{r.status_code}")

# ══════════ T5：执行器阈值触发 ══════════
print("== T5: 执行器运行后告警评估 ==")
with db_conn() as conn:
    # 成功流程（无 sleep）：success_rate=100 → 不触发 success_rate<100
    fid = create_flow(conn, "D12-成功", [code_node("n1", "_out = {'ok': 1}")])
    ex = FlowExecutor(conn)
    r1 = ex.run([code_node("n1", "_out = {'ok': 1}")], [], {}, conn,
                persist=True, flow_id=fid, flow_name="D12-成功")
    chk("T5 成功流程 completed", r1["status"] == "completed", f"status={r1['status']}")
    chk("T5 成功不触发 success_rate", conn.execute(
        "SELECT COUNT(*) c FROM alert_events WHERE rule_id=? AND run_id=?", (rid1, r1["run_id"])).fetchone()["c"] == 0)
    # 慢成功流程：avg_latency > 100 命中（math 大循环制造耗时，time 不在沙箱白名单）
    slow_code = "import math\ns=0\nfor i in range(3000000):\n    s+=math.sqrt(i)\n_out={'ok': s}"
    fid2 = create_flow(conn, "D12-慢", [code_node("n1", slow_code)])
    r2 = ex.run([code_node("n1", slow_code)], [], {}, conn,
                persist=True, flow_id=fid2, flow_name="D12-慢")
    chk("T5 慢流程完成", r2["status"] == "completed" and r2["total_latency_ms"] > 100,
        f"status={r2['status']} ms={r2.get('total_latency_ms')}")
    chk("T5 慢流程触发 avg_latency", conn.execute(
        "SELECT COUNT(*) c FROM alert_events WHERE rule_id=? AND run_id=?", (rid2, r2["run_id"])).fetchone()["c"] == 1)
    # 失败流程：success_rate=0 + error_count=1 双命中（不依赖 avg_latency）
    fid3 = create_flow(conn, "D12-失败", [code_node("n1", "raise Exception('boom')")])
    r3 = ex.run([code_node("n1", "raise Exception('boom')")], [], {}, conn,
                persist=True, flow_id=fid3, flow_name="D12-失败")
    chk("T5 失败流程 partial", r3["status"] == "partial", f"status={r3['status']}")
    evs = conn.execute("SELECT * FROM alert_events WHERE run_id=?", (r3["run_id"],)).fetchall()
    chk("T5 失败触发事件", len(evs) >= 2, f"events={len(evs)}")
    ev_metrics = {e["metric"] for e in evs}
    chk("T5 success_rate 命中", "success_rate" in ev_metrics, f"metrics={ev_metrics}")
    chk("T5 error_count 命中", "error_count" in ev_metrics, f"metrics={ev_metrics}")
    lvl = {e["level"] for e in evs}
    chk("T5 等级继承规则", "warning" in lvl, f"levels={lvl}")
    last_fired = conn.execute("SELECT last_fired_at FROM alert_rules WHERE id=?", (rid1,)).fetchone()
    chk("T5 last_fired_at 已更新", bool(last_fired and last_fired["last_fired_at"]),
        f"last_fired={last_fired and last_fired['last_fired_at']}")

# ══════════ T6：告警列表 + ack ══════════
print("== T6: 告警列表 + 确认 ==")
alerts = client.get("/api/monitor/alerts?status=open").json()
chk("T6 open 列表非空", len(alerts.get("alerts", [])) >= 2, f"n={len(alerts.get('alerts', []))}")
chk("T6 total_open 计数", alerts.get("total_open") >= 2, f"total_open={alerts.get('total_open')}")
first = alerts["alerts"][0]
aid = first["id"]
chk("T6 告警含 run 关联", first.get("run_id", 0) > 0 and first.get("flow_name"), f"{first.get('run_id')} {first.get('flow_name')}")
r = client.post(f"/api/monitor/alerts/{aid}/ack")
chk("T6 ack 接口", r.status_code == 200 and r.json().get("ok"), f"{r.text}")
after = client.get("/api/monitor/alerts?status=open").json()
chk("T6 ack 后 open 减一", after.get("total_open") == alerts.get("total_open") - 1,
    f"{after.get('total_open')} vs {alerts.get('total_open')}")
level_alerts = client.get("/api/monitor/alerts?level=critical").json()
chk("T6 level 过滤", all(a.get("level") == "critical" for a in level_alerts.get("alerts", [])),
    f"n={len(level_alerts.get('alerts', []))}")

# ══════════ T7：webhook 通知（A2A alert_triggered + HMAC 签名）══════════
print("== T7: 告警 webhook 通知 ==")
r = client.post("/api/monitor/alert-rules", json={
    "name": "通知规则", "metric": "error_count", "operator": ">", "threshold": 0,
    "level": "critical", "notify_url": MOCK_URL, "secret": "alert-secret-1", "status": "active"})
rid4 = r.json()["id"]
RECEIVED.clear()
with db_conn() as conn:
    ex = FlowExecutor(conn)
    fid4 = create_flow(conn, "D12-通知", [code_node("n1", "raise Exception('boom2')")])
    ex.run([code_node("n1", "raise Exception('boom2')")], [], {}, conn,
           persist=True, flow_id=fid4, flow_name="D12-通知")
    time.sleep(0.3)
alerts_ev = [x for x in RECEIVED if json.loads(x["body"]).get("kind") == "event"]
chk("T7 Mock 收到 alert 事件", len(alerts_ev) >= 1, f"收到 {len(alerts_ev)}")
if alerts_ev:
    d = json.loads(alerts_ev[0]["body"])
    ev = d.get("event") or {}
    chk("T7 A2A 结构", d.get("protocol") == "a2a" and d.get("kind") == "event"
        and ev.get("type") == "alert_triggered", f"{json.dumps(d, ensure_ascii=False)[:160]}")
    payload = ev.get("payload") or {}
    chk("T7 payload 含指标/阈值", payload.get("metric") == "error_count"
        and payload.get("operator") == ">" and payload.get("threshold") == 0,
        f"{json.dumps(payload, ensure_ascii=False)[:120]}")
    chk("T7 sender 身份卡", (d.get("sender") or {}).get("name") == "mbse-monitor",
        f"sender={(d.get('sender') or {}).get('name')}")
    sig = alerts_ev[0]["headers"].get("X-A2A-Signature", "")
    expect = hmac.new(b"alert-secret-1", alerts_ev[0]["body"], hashlib.sha256).hexdigest()
    chk("T7 HMAC 签名校验", sig == expect, f"expect={expect[:16]} got={sig[:16]}")

# ══════════ T8：规则暂停后不再触发 ══════════
print("== T8: 暂停规则不触发 ==")
r = client.post(f"/api/monitor/alert-rules/{rid4}/toggle")
chk("T8 暂停通知规则", r.json().get("status") == "paused", f"{r.text}")
RECEIVED.clear()
with db_conn() as conn:
    before_n = conn.execute(
        "SELECT COUNT(*) c FROM alert_events WHERE rule_id=?", (rid4,)).fetchone()["c"]
    ex = FlowExecutor(conn)
    fid5 = create_flow(conn, "D12-暂停", [code_node("n1", "raise Exception('boom3')")])
    ex.run([code_node("n1", "raise Exception('boom3')")], [], {}, conn,
           persist=True, flow_id=fid5, flow_name="D12-暂停")
    time.sleep(0.3)
    after_n = conn.execute(
        "SELECT COUNT(*) c FROM alert_events WHERE rule_id=?", (rid4,)).fetchone()["c"]
    chk("T8 paused 规则不写事件", after_n == before_n, f"before={before_n} after={after_n}")
chk("T8 paused 规则不通知", len(RECEIVED) == 0, f"RECEIVED={len(RECEIVED)}")
r = client.post(f"/api/monitor/alert-rules/{rid4}/toggle")
chk("T8 恢复 active", r.json().get("status") == "active", f"{r.text}")

_server.shutdown()
print(f"\n结果: {PASS} 通过, {FAIL} 失败")
sys.exit(0 if FAIL == 0 else 1)
