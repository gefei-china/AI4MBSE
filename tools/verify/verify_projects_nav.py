# -*- coding: utf-8 -*-
"""项目（容器）与任务归属链路自检 —— 左侧项目管理优化（2026-09-24）。

验证对象（对应 routers/projects.py + repositories/project_repo.py + routers/conversations.py）：
  1. projects 表数据来源三列可读写（source / workspace / remote）
  2. 数据来源校验：本地缺工作空间 → 400；远端缺主机名 → 400；远端 auth=key 缺身份文件 → 400
  3. 本地/远端两种来源创建 → GET 回读一致（remote 为 JSON，字段完整）
  4. 编辑名称（PUT）只改名称不动数据来源
  5. 任务归项目：POST /api/conversations 带 project_id → /task-count 计数 +1
  6. 任务操作与原列表一致：PATCH 重命名、DELETE 删除
  7. 移除项目：只移除容器不删内容 —— 会话解绑（project_id 置空）且仍存在
  8. pick-folder 端点已注册（**不实际弹框**：会阻塞等待人工选择）

用法（服务需已启动；脚本自建自清，不碰既有数据）：
  .venv/Scripts/python.exe -X utf8 tools/verify/verify_projects_nav.py
"""
import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000"
OK, FAIL = [], []


def call(method: str, path: str, body=None):
    """返回 (status_code, payload)。4xx 也返回而不抛，便于断言校验分支。"""
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


def check(name: str, cond: bool, evidence: str = ""):
    (OK if cond else FAIL).append(name)
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  ← " + evidence) if evidence else ""))
    return cond


def main() -> int:
    print("── 0. 前置：服务可达 + 数据来源列可读 ──")
    st, projs = call("GET", "/api/projects")
    if not check("GET /api/projects 200 且为数组", st == 200 and isinstance(projs, list), f"status={st}"):
        return 1
    has_cols = all(isinstance(p, dict) and "source" in p and "workspace" in p and "remote" in p
                   for p in projs) if projs else True
    check("projects 行含 source/workspace/remote 三列（迁移生效）", has_cols,
          f"共 {len(projs)} 个项目")

    created = []
    try:
        print("── 1. 创建校验：负例（非法输入必须 400，不得静默兜底）──")
        st, r = call("POST", "/api/projects", {"name": "verify-本地缺工作空间", "source": "local"})
        check("本地来源缺工作空间 → 400", st == 400 and "工作空间" in str(r.get("error", "")),
              f"status={st} err={r.get('error')}")
        st, r = call("POST", "/api/projects", {"name": "verify-远端缺主机", "source": "remote"})
        check("远端来源缺主机名 → 400", st == 400 and "主机名" in str(r.get("error", "")),
              f"status={st} err={r.get('error')}")
        st, r = call("POST", "/api/projects", {
            "name": "verify-远端key缺身份文件", "source": "remote",
            "remote_host": "host.example.com", "remote_auth_mode": "key"})
        check("远端 key 认证缺身份文件 → 400", st == 400 and "身份文件" in str(r.get("error", "")),
              f"status={st} err={r.get('error')}")
        st, r = call("POST", "/api/projects", {
            "name": "verify-非法端口", "source": "remote",
            "remote_host": "host.example.com", "remote_port": "abc"})
        check("远端端口非数字 → 400", st == 400 and "端口" in str(r.get("error", "")),
              f"status={st} err={r.get('error')}")

        print("── 2. 正例：本地来源创建 + 回读一致 ──")
        st, r = call("POST", "/api/projects", {
            "name": "verify-本地项目", "source": "local", "workspace": r"D:\verify\ws"})
        pid_local = (r or {}).get("id")
        ok = check("本地来源创建 200", st == 200 and bool(pid_local), f"status={st} id={pid_local}")
        if ok:
            created.append(pid_local)
            st, p = call("GET", f"/api/projects/{pid_local}")
            check("回读 source=local 且 workspace 一致",
                  p.get("source") == "local" and p.get("workspace") == r"D:\verify\ws",
                  f"source={p.get('source')} workspace={p.get('workspace')}")

        print("── 3. 正例：远端来源创建（无身份验证）+ 回读 JSON ──")
        st, r = call("POST", "/api/projects", {
            "name": "verify-远端项目", "source": "remote",
            "remote_display_name": "建模工作站", "remote_host": "user@10.0.0.9",
            "remote_port": "2222", "remote_auth_mode": "none"})
        pid_remote = (r or {}).get("id")
        ok = check("远端来源创建 200", st == 200 and bool(pid_remote), f"status={st} id={pid_remote}")
        if ok:
            created.append(pid_remote)
            st, p = call("GET", f"/api/projects/{pid_remote}")
            try:
                rm = json.loads(p.get("remote") or "{}")
            except Exception:
                rm = {}
            check("回读 source=remote 且 SSH 字段完整",
                  p.get("source") == "remote" and rm.get("display_name") == "建模工作站"
                  and rm.get("host") == "user@10.0.0.9" and rm.get("port") == "2222"
                  and rm.get("auth_mode") == "none" and rm.get("identity_file") == "",
                  f"remote={rm}")
            check("远端项目不写本地工作空间（workspace 为空）", not (p.get("workspace") or ""),
                  f"workspace={p.get('workspace')!r}")

        print("── 4. 编辑：只改名称不动数据来源 ──")
        st, r = call("PUT", f"/api/projects/{pid_local}", {"name": "verify-本地项目-改名"})
        st2, p = call("GET", f"/api/projects/{pid_local}")
        check("改名成功且 workspace 未被清空",
              st == 200 and p.get("name") == "verify-本地项目-改名"
              and p.get("workspace") == r"D:\verify\ws",
              f"name={p.get('name')} workspace={p.get('workspace')}")
        st, r = call("PUT", f"/api/projects/{pid_local}",
                     {"name": "verify-本地项目-改名", "source": "remote",
                      "remote_host": "switch.example.com", "remote_auth_mode": "none"})
        st2, p = call("GET", f"/api/projects/{pid_local}")
        check("数据来源可切换（本地 → 远端）且 workspace 被清空",
              st == 200 and p.get("source") == "remote" and not (p.get("workspace") or ""),
              f"source={p.get('source')} workspace={p.get('workspace')!r}")

        print("── 5. 任务归属：项目内新建任务 ──")
        st, before = call("GET", f"/api/projects/{pid_remote}/task-count")
        st, conv = call("POST", "/api/conversations",
                        {"title": "verify-项目内任务", "project_id": pid_remote})
        cid = (conv or {}).get("id")
        ok = check("带 project_id 创建会话 200", st == 200 and bool(cid), f"status={st} id={cid}")
        st, after = call("GET", f"/api/projects/{pid_remote}/task-count")
        check("项目任务数 +1", after.get("tasks") == before.get("tasks", 0) + 1,
              f"{before.get('tasks')} → {after.get('tasks')}")
        convs = call("GET", "/api/conversations")[1] or []
        row = next((c for c in convs if c.get("id") == cid), None)
        check("会话列表回读 project_id 归属正确",
              bool(row) and str(row.get("project_id")) == str(pid_remote),
              f"project_id={row and row.get('project_id')}")

        print("── 6. 任务操作：与原列表一致（重命名 / 删除）──")
        st, r = call("PATCH", f"/api/conversations/{cid}", {"title": "verify-任务改名"})
        convs = call("GET", "/api/conversations")[1] or []
        row = next((c for c in convs if c.get("id") == cid), None)
        check("任务重命名生效", st == 200 and row and row.get("title") == "verify-任务改名",
              f"title={row and row.get('title')}")

        print("── 7. 移除项目：只移除容器，不删任务 ──")
        st, r = call("DELETE", f"/api/projects/{pid_remote}")
        check("移除项目 200 且回报解绑任务数", st == 200 and (r or {}).get("detached_tasks") == 1,
              f"status={st} resp={r}")
        st, p = call("GET", f"/api/projects/{pid_remote}")
        check("项目已不存在（404）", st == 404, f"status={st}")
        convs = call("GET", "/api/conversations")[1] or []
        row = next((c for c in convs if c.get("id") == cid), None)
        check("任务仍存在且已解绑（project_id 置空）",
              bool(row) and not (row.get("project_id") or ""),
              f"exists={bool(row)} project_id={row and row.get('project_id')!r}")
        created.remove(pid_remote)
        st, r = call("DELETE", f"/api/conversations/{cid}")
        check("清理测试任务", st == 200, f"status={st}")

        print("── 8. pick-folder 端点已注册（不实际弹框：会阻塞等待人工选择）──")
        st, r = call("GET", "/openapi.json")
        paths = (r or {}).get("paths", {})
        check("POST /api/projects/pick-folder 在路由表中",
              "/api/projects/pick-folder" in paths and "post" in paths.get("/api/projects/pick-folder", {}),
              f"registered={'/api/projects/pick-folder' in paths}")
    finally:
        print("── 清理：删除本脚本创建的项目 ──")
        for pid in created:
            st, _ = call("DELETE", f"/api/projects/{pid}")
            print(f"  cleanup {pid} → {st}")

    print(f"\n结果：{len(OK)} 通过 / {len(FAIL)} 失败")
    if FAIL:
        print("失败项：" + "；".join(FAIL))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())