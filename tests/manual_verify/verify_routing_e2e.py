"""端到端验证：上传文档优先 + GraphRAG→RAG 混合路由。

场景：上传与知识库无关的文档（足球规则）→ 提问应命中上传文档（attachment_used=true，
context 含「上传资料·命中」）；无附件提问知识库话题 → 图谱优先；无关话题 → 向量兜底。
"""
import json
import os
import urllib.request
import uuid

BASE = "http://127.0.0.1:8000"


def api(path, method="GET", body=None, timeout=90):
    req = urllib.request.Request(BASE + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"} if body is not None else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8", "ignore")
        return json.loads(raw) if raw else {}


def stream_chat(cid, payload):
    req = urllib.request.Request(f"{BASE}/api/conversations/{cid}/chat/stream",
                                 data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    done = None
    with urllib.request.urlopen(req, timeout=120) as r:
        for line in r:
            line = line.decode("utf-8", "ignore").strip()
            if line.startswith("data:") and '"type": "done"' in line:
                done = json.loads(line[5:])
    return done["data"] if done else {}


def upload_doc(content, fname):
    tmp = f"static/uploads/reg_{uuid.uuid4().hex[:8]}.txt"
    open(tmp, "w", encoding="utf-8").write(content)
    boundary = "----wb" + uuid.uuid4().hex
    body = b""
    body += (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{fname}"\r\n'
             "Content-Type: text/plain\r\n\r\n").encode()
    body += open(tmp, "rb").read() + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(BASE + "/api/upload", data=body, method="POST",
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=15) as r:
        up = json.loads(r.read().decode())
    try:
        os.remove(tmp)
    except Exception:
        pass
    return up


def main():
    cid = api("/api/conversations", "POST", {"title": "路由验证"})["id"]

    # ① 上传无关话题文档（足球规则）→ 提问 → 应命中上传文档
    up = upload_doc("足球比赛规则：每队上场11人，比赛时间90分钟。越位规则：进攻方球员在对方半场接球时，若防守方最后第二名球员更靠近对方球门线则判越位。", "football.txt")
    d1 = stream_chat(cid, {"message": "足球比赛有哪些规则？",
                           "attachments": [{"url": up["url"], "filename": "football.txt", "size": 50, "is_image": False}]})
    r1 = d1.get("retrieval", {})
    print("① 上传文档优先:", "attachment_used=", d1.get("attachments_info", {}).get("parsed"),
          "| route=", r1.get("route"))
    print("   检索结果含附件命中:", r1.get("attachment_used"))
    assert r1.get("attachment_used") is True, "上传文档未成为首要依据"
    print("   ✓ 上传文档首要依据（不再输出纯知识库内容）")

    # ② 无附件：知识库话题（@知识库 主动引用触发检索）→ 图谱优先
    d2 = stream_chat(cid, {"message": "宽带通信卫星的载荷组成 @知识库"})
    r2 = d2.get("retrieval", {})
    print("② 知识库话题路由:", r2.get("route"), "| graph=", r2.get("graph_count"), "| vector=", r2.get("vector_count"))
    assert r2.get("graph_count", 0) > 0, "图谱应命中（token 补充）"
    print("   ✓ GraphRAG 优先命中")

    # ③ 无附件：无关话题（@知识库 主动引用）→ 图谱未命中 → 向量兜底
    d3 = stream_chat(cid, {"message": "量子计算中的退相干问题 @知识库"})
    r3 = d3.get("retrieval", {})
    print("③ 无关话题路由:", r3.get("route"), "| graph=", r3.get("graph_count"), "| vector=", r3.get("vector_count"))
    assert r3.get("graph_count", 0) == 0 and r3.get("route") in ("vector", "mixed"), "应走向量兜底"
    print("   ✓ RAG 向量兜底")

    # ④ 按需检索：纯问答无引用 → 不检索（route=none）
    d4 = stream_chat(cid, {"message": "你好呀，随便聊聊"})
    r4 = d4.get("retrieval", {})
    print("④ 纯问答按需:", r4.get("route"), "| graph=", r4.get("graph_count"))
    assert r4.get("route") == "none", "纯问答不应自动检索"
    print("   ✓ 未引用知识库不检索")

    api(f"/api/conversations/{cid}", "DELETE")
    print("端到端验证通过")


if __name__ == "__main__":
    main()
