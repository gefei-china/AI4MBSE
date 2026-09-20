"""验证三项优化：①流程设置(名称/描述/版本/状态)保存 ②深度思考 ③多轮对话迭代"""
import json
import os
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000"
PASS = 0
FAIL = 0

def chk(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ✅ {name} {detail}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {detail}")

def api(path, body=None, method="GET"):
    req = urllib.request.Request(BASE + path, method=method)
    if body is not None:
        req.add_header("Content-Type", "application/json")
        req.data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            raw = r.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return json.loads(raw)
        except Exception:
            return {"error": raw[:200]}

print("=== ① 流程关键信息设置（名称/描述/版本/状态）===")
# 未填名称 → 400
r = api("/api/studio/agent-flows", {"nodes": [], "edges": []}, "POST")
chk("未填名称被拦截", isinstance(r.get("error"), str) and "名称" in r.get("error", ""), f"→ {r.get('error')}")
# 保存带 version/status
r = api("/api/studio/agent-flows", {
    "name": "【验证】设置面板流程", "description": "验证设置面板保存", "version": "v2", "status": "published",
    "nodes": [{"id": "n1", "type": "llm", "label": "总结", "config": {"prompt": "x"}}],
    "edges": [],
}, "POST")
fid = r.get("id")
chk("保存成功且返回 id", bool(fid), f"→ id={fid}")
r2 = api(f"/api/studio/agent-flows/{fid}")
chk("version/status 已持久化", r2.get("version") == "v2" and r2.get("status") == "published",
    f"→ version={r2.get('version')} status={r2.get('status')}")
# 编辑（update）保留 version
r3 = api("/api/studio/agent-flows", {
    "id": fid, "name": "【验证】设置面板流程-改", "description": "编辑验证",
    "version": "v3", "status": "archived",
    "nodes": [{"id": "n1", "type": "llm", "label": "总结", "config": {"prompt": "x"}}],
    "edges": [],
}, "POST")
r4 = api(f"/api/studio/agent-flows/{fid}")
chk("编辑后 version/status 更新", r4.get("version") == "v3" and r4.get("status") == "archived" and r4.get("name", "").endswith("改"),
    f"→ name={r4.get('name')} version={r4.get('version')} status={r4.get('status')}")

print("=== ② AI 生成深度思考 ===")
r = api("/api/studio/ai/generate-flow", {"prompt": "从需求文本抽取需求条目，做冲突检测，再生成影响分析报告", "deep_thinking": True}, "POST")
chk("深度思考生成成功", r.get("ok") and r.get("source") == "llm", f"→ source={r.get('source')} name={r.get('name')}")
chk("返回 reasoning 思考过程", bool(r.get("reasoning")), f"→ reasoning({len(r.get('reasoning') or '')}字): {(r.get('reasoning') or '')[:60]}")
chk("deep_thinking 标注", r.get("deep_thinking") is True, f"→ {r.get('deep_thinking')}")
n_count = len(r.get("nodes", []))
chk("生成节点数合理", n_count >= 3, f"→ {n_count} 节点")

print("=== ③ 多轮对话迭代（不覆盖前序指令）===")
# 第一轮：生成基础流程
r1 = api("/api/studio/ai/generate-flow", {"prompt": "做一个知识检索后问答的流程", "deep_thinking": False}, "POST")
base_nodes = [n.get("id") for n in (r1.get("nodes") or [])]
chk("第一轮生成成功", bool(r1.get("ok")) and len(base_nodes) >= 2, f"→ {len(base_nodes)} 节点: {base_nodes}")
# 构造对话历史（user + assistant 摘要）
conv = [
    {"role": "user", "content": "做一个知识检索后问答的流程"},
    {"role": "assistant", "content": f"📋 {r1.get('name')} · {len(r1.get('nodes') or [])} 节点"},
]
# 第二轮：追加指令（携带历史）
r2 = api("/api/studio/ai/refine-flow", {
    "prompt": "在流程最后加一个 LLM 总结节点",
    "definition": {"nodes": r1.get("nodes", []), "edges": r1.get("edges", [])},
    "conversation": conv,
    "deep_thinking": False,
}, "POST")
chk("第二轮迭代成功", bool(r2.get("ok")) and r2.get("source") == "llm", f"→ source={r2.get('source')}")
r2_ids = [n.get("id") for n in (r2.get("nodes") or [])]
# 前序节点应保留（不覆盖）
kept = [nid for nid in base_nodes if nid in r2_ids]
chk("前序节点保留（不覆盖）", len(kept) >= len(base_nodes) - 1, f"→ 保留 {kept}")
# 应有新增 llm 总结节点
has_new_llm = any(n.get("type") == "llm" for n in (r2.get("nodes") or []))
chk("新增 LLM 节点", has_new_llm, f"→ 节点类型 {[n.get('type') for n in (r2.get('nodes') or [])]}")

# 第三轮：再追加（历史 + 前一轮新历史）
conv2 = conv + [
    {"role": "assistant", "content": f"📋 {r2.get('name')} · {len(r2.get('nodes') or [])} 节点"},
]
r3 = api("/api/studio/ai/refine-flow", {
    "prompt": "把总结节点的 prompt 改为强调输出结构化要点",
    "definition": {"nodes": r2.get("nodes", []), "edges": r2.get("edges", [])},
    "conversation": conv2,
    "deep_thinking": False,
}, "POST")
chk("第三轮迭代成功（三轮上下文连续）", bool(r3.get("ok")) and r3.get("source") == "llm",
    f"→ source={r3.get('source')} nodes={len(r3.get('nodes') or [])}")
r3_ids = [n.get("id") for n in (r3.get("nodes") or [])]
kept2 = [nid for nid in r2_ids if nid in r3_ids]
chk("第二轮节点继续保留", len(kept2) >= len(r2_ids) - 1, f"→ 保留 {kept2}")

# Mock 模式深度思考（进程内强制）
os.environ["MBSE_LLM_FORCE_MOCK"] = "1"
import sys as _s
_s.path.insert(0, ".")
from ai_copilot import FlowCopilot
mock_r = FlowCopilot().generate("帮我做知识问答", deep_thinking=True)
chk("Mock 深度思考可生成", mock_r.get("ok") and mock_r.get("source") == "template" and bool(mock_r.get("reasoning")),
    f"→ source={mock_r.get('source')} reasoning={bool(mock_r.get('reasoning'))} nodes={len(mock_r.get('nodes') or [])}")
del os.environ["MBSE_LLM_FORCE_MOCK"]

# 清理验证数据
api(f"/api/studio/agent-flows/{fid}", None, "DELETE")
print()
print(f"RESULT: {PASS} PASS / {FAIL} FAIL")
sys.exit(0 if FAIL == 0 else 1)
