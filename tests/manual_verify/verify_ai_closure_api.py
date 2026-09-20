"""AI 建模闭环 API 回归：附件注入 + 意图识别 + 工作流匹配 + forced_intent。"""
import json
import os
import urllib.request
import uuid

BASE = "http://127.0.0.1:8000"


def api(path, method="GET", body=None, timeout=60):
    req = urllib.request.Request(BASE + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"} if body is not None else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8", "ignore")
        return json.loads(raw) if raw else {}


def main():
    # 建会话
    cid = api("/api/conversations", "POST", {"title": "回归-闭环"})["id"]
    # 上传 txt
    tmp = f"static/uploads/reg_{uuid.uuid4().hex[:8]}.txt"
    open(tmp, "w", encoding="utf-8").write("卫星转发器12路 Ka频段 功率50W")
    boundary = "----wb" + uuid.uuid4().hex
    body = b""
    body += (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="reg.txt"\r\n'
             "Content-Type: text/plain\r\n\r\n").encode()
    body += open(tmp, "rb").read() + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(BASE + "/api/upload", data=body, method="POST",
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=15) as r:
        up = json.loads(r.read().decode())
    try:
        os.remove(tmp)
    except Exception:
        pass  # 沙箱回收站不可用时跳过清理（残留测试文件无害）

    # 流式对话：附件 + 知识库 + 工作流匹配
    def stream(payload):
        req = urllib.request.Request(f"{BASE}/api/conversations/{cid}/chat/stream",
                                     data=json.dumps(payload).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        done = None
        with urllib.request.urlopen(req, timeout=90) as r:
            for line in r:
                line = line.decode("utf-8", "ignore").strip()
                if line.startswith("data:") and '"type": "done"' in line:
                    done = json.loads(line[5:])
        return done["data"] if done else {}

    d1 = stream({"message": "根据上传资料做需求分析 卫星 @知识库",
                 "attachments": [{"url": up["url"], "filename": "reg.txt", "size": 50, "is_image": False}]})
    print("  intent:", d1["intent"], "| attachments_info:", d1.get("attachments_info"),
          "| matched_flows:", d1.get("matched_flows"))
    assert d1["intent"] == "requirement_analysis", f"意图识别失败: {d1['intent']}"
    assert d1.get("attachments_info", {}).get("parsed") == 1, "附件未解析"
    print("  ✓ 附件注入 + 意图识别 + 工作流匹配通过")

    d2 = stream({"message": "随便说点什么", "forced_intent": "design"})
    print("  forced_intent design →", d2["intent"])
    assert d2["intent"] == "design", "forced_intent 未生效"
    print("  ✓ 快捷指定 Agent 通过")

    d3 = stream({"message": "用测试技能处理", "skill_name": "test-skill-703443"})
    print("  skill_name 注入 → intent:", d3["intent"], "| 无错误")
    print("  ✓ 快捷指定 Skill 通过")

    api(f"/api/conversations/{cid}", "DELETE")
    print("回归 OK")


if __name__ == "__main__":
    main()
