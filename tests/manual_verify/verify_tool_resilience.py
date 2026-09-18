"""D2 工具容错闭环验证：重试（指数退避）+ 降级链。

场景：
1. flaky 工具：首次 500 → 重试后 200 → 断言 ok=True 且 retried=1
2. dead 工具：恒 500 + fallback_to=graph_retrieve → 断言降级成功且 degraded_from 标记
3. 非瞬态错误（业务失败）不重试：bad 工具返回业务错误 → 断言未重试（attempts=0）
"""
import os
import sys
import json
import time
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["MBSE_LLM_FORCE_MOCK"] = "1"
_TEST_DB = os.path.join(os.path.dirname(__file__), "_d2_test.db")
if os.path.exists(_TEST_DB):
    os.remove(_TEST_DB)
os.environ["MBSE_DB_PATH"] = _TEST_DB

from database import init_db, get_db
init_db()

MOCK_PORT = 19020
PASS = 0
FAIL = 0

_call_count = {"flaky": 0, "dead": 0, "bad": 0}
_bad_calls = 0


class MockHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        path = self.path
        if "/flaky" in path:
            _call_count["flaky"] += 1
            if _call_count["flaky"] == 1:
                data = json.dumps({"error": "boom"}).encode()
                self.send_response(500)
            else:
                data = json.dumps({"data": {"ok": True, "value": "recovered"}}).encode()
                self.send_response(200)
        elif "/dead" in path:
            _call_count["dead"] += 1
            data = json.dumps({"error": "boom"}).encode()
            self.send_response(503)
        else:  # /bad 业务失败（HTTP 200 但业务 code 非 0）
            global _bad_calls
            _bad_calls += 1
            data = json.dumps({"code": 4001, "message": "参数校验失败"}).encode()
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def start_mock():
    srv = HTTPServer(("127.0.0.1", MOCK_PORT), MockHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


def register_http_tool(name, path, retry_policy, fallback_to=""):
    conn = get_db()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO tools (name, source, kind, description, status, input_schema, version, side_effect, risk_level, config, retry_policy, fallback_to) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (name, "local", "http", f"mock {name}", "active", "{}", "v1.0", "read", "low",
             json.dumps({"method": "GET", "url": f"http://127.0.0.1:{MOCK_PORT}{path}", "data_path": "data"}, ensure_ascii=False),
             json.dumps(retry_policy, ensure_ascii=False), fallback_to),
        )
        conn.commit()
    finally:
        conn.close()


def main():
    start_mock()
    time.sleep(0.2)

    # 1. 瞬态失败重试：首次 500 → 重试 200
    register_http_tool("tool_flaky", "/flaky",
                       {"max_retries": 2, "backoff_base_ms": 50, "backoff_multiplier": 1})
    from workflows import ToolExecutor
    ex = ToolExecutor()
    r = ex.execute("tool_flaky", {})
    check("重试后成功", r.get("ok") is True, json.dumps(r, ensure_ascii=False))
    check("重试计数 retried=1", r.get("retried") == 1, str(r))
    check("重试调用次数=2", _call_count["flaky"] == 2, str(_call_count["flaky"]))
    check("结果透传", "recovered" in r.get("result", ""), r.get("result", ""))

    # 2. 降级链：恒 503 + fallback_to=graph_retrieve
    register_http_tool("tool_dead", "/dead",
                       {"max_retries": 1, "backoff_base_ms": 50, "backoff_multiplier": 1},
                       fallback_to="graph_retrieve")
    r2 = ex.execute("tool_dead", {"query": "宽带通信载荷"})
    check("降级后成功", r2.get("ok") is True, json.dumps(r2, ensure_ascii=False)[:300])
    check("降级标记 degraded_from", r2.get("degraded_from") == "tool_dead", str(r2.get("degraded_from")))
    check("降级目标标记", r2.get("fallback_to") == "graph_retrieve", str(r2.get("fallback_to")))
    check("降级结果内容", "图谱" in r2.get("result", "") or "向量" in r2.get("result", ""), r2.get("result", "")[:100])

    # 3. 业务失败不重试（HTTP 200 + 业务 code）
    register_http_tool("tool_bad", "/bad",
                       {"max_retries": 3, "backoff_base_ms": 50, "backoff_multiplier": 1})
    r3 = ex.execute("tool_bad", {})
    check("业务失败不重试", r3.get("ok") is False and r3.get("retried") is None,
          json.dumps(r3, ensure_ascii=False)[:200])
    check("业务失败调用次数=1", _bad_calls == 1, str(_bad_calls))

    print(f"\n===== D2 RESULT: PASS={PASS} FAIL={FAIL} =====")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
