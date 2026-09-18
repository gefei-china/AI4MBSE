"""D3 工作流节点补齐闭环验证：code / http / iteration / knowledge 四类新节点。

场景（单一 DAG 全链路）：
n1 knowledge（检索）→ n2 code（变换，引用 n1 输出）→ n3 http（调用 Mock 服务，引用 n2 输出）
→ n4 code（产出列表）→ n6 iteration（items 引用 n4 输出，subflow 为 n5 code）
另测：code 节点语法错误 → status=error；copilot NODE_SCHEMA/validate 对新类型闭环。
"""
import os
import sys
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["MBSE_LLM_FORCE_MOCK"] = "1"
_TEST_DB = os.path.join(os.path.dirname(__file__), "_d3_test.db")
if os.path.exists(_TEST_DB):
    os.remove(_TEST_DB)
os.environ["MBSE_DB_PATH"] = _TEST_DB

from database import init_db, get_db
init_db()

MOCK_PORT = 19021
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


class MockHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        from urllib.parse import urlparse, parse_qs
        if self.path.startswith("/d3"):
            qs = parse_qs(urlparse(self.path).query)
            echo = qs.get("q", [""])[0]
            data = json.dumps({"code": 0, "data": {"message": "ok", "echo": echo}}).encode()
            self.send_response(200)
        else:
            data = json.dumps({"code": 404, "message": "not found"}).encode()
            self.send_response(404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


server = HTTPServer(("127.0.0.1", MOCK_PORT), MockHandler)
threading.Thread(target=server.serve_forever, daemon=True).start()

from workflows import FlowExecutor
from ai_copilot import FlowCopilot, NODE_SCHEMA

NODE_TYPES = FlowExecutor.NODE_TYPES

PORT = MOCK_PORT
DAG_NODES = [
    {"id": "n1", "type": "knowledge", "label": "知识检索",
     "config": {"query": "系统架构", "top_k": 3, "branch": "dev/main"}},
    {"id": "n2", "type": "code", "label": "变换",
     "config": {"inputs": {"q": "{{n1.data.query}}", "k": "{{n1.data.top_k}}"},
                "code": "_out['q_upper'] = str(_in['q']).upper()\n_out['k_x2'] = int(_in['k']) * 2"}},
    {"id": "n3", "type": "http", "label": "HTTP 调用",
     "config": {"method": "GET", "url": "http://127.0.0.1:%d/d3?q={{n2.data.outputs.q_upper}}" % PORT}},
    {"id": "n4", "type": "code", "label": "产出列表",
     "config": {"code": "_out['names'] = ['甲', '乙', '丙']"}},
    {"id": "n5", "type": "code", "label": "子流程(逐项)",
     "config": {"inputs": {"item": "{{blackboard.__item}}"},
                "code": "_out['length'] = len(str(_in['item']))"}},
    {"id": "n6", "type": "iteration", "label": "迭代",
     "config": {"items": "{{n4.data.outputs.names}}", "subflow": "n5"}},
]
DAG_EDGES = [
    {"source": "n1", "target": "n2"},
    {"source": "n2", "target": "n3"},
    {"source": "n3", "target": "n4"},
    {"source": "n4", "target": "n6"},
]

print("== T1: 节点类型注册 ==")
chk("NODE_TYPES 含 4 类新节点", all(t in NODE_TYPES for t in ("code", "http", "iteration", "knowledge")),
    str(NODE_TYPES))
chk("Copilot NODE_SCHEMA 含 4 类新节点", all(t in NODE_SCHEMA for t in ("code", "http", "iteration", "knowledge")),
    str(list(NODE_SCHEMA.keys())))

print("== T2: Copilot 校验新类型 DAG（无 error 级问题）==")
cop = FlowCopilot(get_db())
issues = cop.validate(DAG_NODES, DAG_EDGES)
errs = [i for i in issues if i.get("level") == "error"]
chk("validate 无 error 级问题", not errs, str(errs))

print("== T3: 全 DAG 执行（含 4 类新节点）==")
ex = FlowExecutor(get_db())
summary = ex.run(DAG_NODES, DAG_EDGES, {"input": "D3 验证"}, conn=get_db(), persist=False)
chk("运行状态 completed", summary.get("status") == "completed", str(summary.get("errors"))[:200])
res = summary.get("results", {})
chk("n1 knowledge 执行成功", res.get("n1", {}).get("status") == "done",
    str(res.get("n1", {}).get("data"))[:200])
d1 = res.get("n1", {}).get("data", {})
chk("n1 检索 query/top_k 透传", d1.get("query") == "系统架构" and d1.get("top_k") == 3, str(d1)[:200])
chk("n1 检索计数为 int（0/真实值）", isinstance(d1.get("graph_count"), int) and isinstance(d1.get("vector_count"), int), str(d1)[:200])

d2 = res.get("n2", {}).get("data", {})
chk("n2 code 变换输出 q_upper", d2.get("outputs", {}).get("q_upper") == "系统架构",
    str(d2.get("outputs"))[:200])
chk("n2 code 变换输出 k_x2=6", d2.get("outputs", {}).get("k_x2") == 6, str(d2.get("outputs"))[:200])

d3 = res.get("n3", {}).get("data", {})
chk("n3 http 调用成功", res.get("n3", {}).get("status") == "done", str(d3)[:200])
chk("n3 http 响应含 ok（data_path 提取）", "ok" in str(d3.get("result", "")), str(d3)[:200])
chk("n3 http 回显含大写 query（模板渲染联动）", "系统架构" in str(d3.get("result", "")), str(d3)[:200])

d6 = res.get("n6", {}).get("data", {})
chk("n6 iteration 迭代 3 轮", d6.get("count") == 3, str(d6)[:200])
rounds = d6.get("rounds", [])
chk("n6 各轮 item 正确", [r.get("item") for r in rounds] == ["甲", "乙", "丙"], str([r.get("item") for r in rounds]))
chk("n6 每轮子流程节点执行成功",
    all(r.get("nodes", {}).get("n5", {}).get("status") == "done" for r in rounds), str(rounds)[:200])
lens = [r.get("nodes", {}).get("n5", {}).get("content", "") for r in rounds]
chk("n6 子流程按 __item 计算长度", lens[0].count("1") >= 1 and lens[1].count("1") >= 1 and lens[2].count("1") >= 1,
    str(lens)[:200])

print("== T4: code 节点错误路径 ==")
bad = ex._exec_code("坏代码", {"code": "raise Exception('boom')"}, {}, {}, {})
chk("code 语法/运行错误 → status=error", bad.get("status") == "error" and "代码执行失败" in bad.get("content", ""),
    str(bad)[:200])
bad2 = ex._exec_code("空代码", {"code": ""}, {}, {}, {})
chk("code 空代码 → status=error", bad2.get("status") == "error", str(bad2)[:200])

print("== T5: knowledge 节点缺 query 错误路径 ==")
kbad = ex._exec_knowledge("缺参数", {"top_k": 3}, {}, {}, {})
chk("knowledge 缺 query → status=error", kbad.get("status") == "error", str(kbad)[:200])

server.shutdown()
print(f"\nD3 验证结果: {PASS} PASS / {FAIL} FAIL")
sys.exit(1 if FAIL else 0)
