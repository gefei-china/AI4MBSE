# -*- coding: utf-8 -*-
"""E2E 门禁：只读建模请求不得越界去查平台坐标/写入（PLATFORM_SCOPE_NO_DRIFT_V1）。

真机对照（conv 999999952 vs 修后同形状输入）：
  修前 1208s / 5 子任务 / 28 次平台工具调用（17 query_projects + 11 query_data_tree）
  修后  477s / 2 子任务 /  0 次平台工具调用
根因是 core.write_scope 把「建立需求集+生成视图代码」判成 yes（弱动词+建模对象），
规划器据此派出平台定位与写入子任务。本门禁把该回归钉死。

⚠️ 约 8 分钟（真实 LLM 两子任务），属 local 层。

输入取原会话的**同形状但更短**版本（截取需求表前两条），观测：
  J1 计划里是否出现平台定位/写入类子任务（不应出现）
  J2 是否出现 query_projects / query_data_tree 调用（不应出现）
  J3 done 是否带 usage、消息是否带 summary（统计可用）
  J4 若触发"等用户"，是否真的发出 clarify_ask 卡（而不是只在正文里说）
"""
# CI-OPTIONAL: 真实 LLM 会话（~8 分钟）+ 运行中 8000 服务 + 真库；CI 无此运行时，已登记 local 层

import json
import os
import sqlite3
import sys
import time
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")
BASE = "http://127.0.0.1:8000"
HDR = {"Content-Type": "application/json", "X-User-Id": "1"}
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "mbse.db")

Q = ("为 A8E 串并联混动整车建立顶层运行需求集 '热管理相关需求集'（类型 RequirementCheck，"
     "stakeholder 为 用户、驾驶员、乘客）。需求：\n"
     "| HM-01 | 提供舒适温度 | 功能 | 整车应接收主驾/副驾/后排分区温度设定并独立闭环控制，"
     "偏差 ≤ 1.5 ℃ |\n"
     "| HM-06 | 动力电池热管理 | 性能 | 电池温度超限时启动液冷，最高单体温度 ≤ 45 ℃ |\n"
     "生成符合 SysML v2 规范的需求视图代码。")

PLATFORM_TOOLS = ("query_projects", "query_data_tree", "query_branches",
                  "query_model_versions", "import_sysml_text")
PLAN_MARKS = ("智源平台坐标", "写入", "导入", "父容器")


def post(path, body, timeout=60):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(),
                                 headers=HDR, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def main():
    cid = json.loads(post("/api/conversations", {"title": "验收-平台越界-20261120",
                                                 "project_id": ""}))["id"]
    print("门禁：平台越界不漂移（PLATFORM_SCOPE_NO_DRIFT_V1）")
    print("cid =", cid, "| 输入 %d 字" % len(Q))
    body = json.dumps({"message": Q}).encode()
    req = urllib.request.Request(BASE + "/api/conversations/%d/chat/stream" % cid,
                                 data=body, headers=HDR, method="POST")
    ev = None
    data, tools, plans, clarifies, done, texts = [], [], [], [], None, []
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=900) as r:
        for raw in r:
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if line.startswith("event:"):
                ev = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data.append(line.split(":", 1)[1].strip())
            elif line == "":
                if data:
                    try:
                        p = json.loads("\n".join(data))
                    except Exception:
                        p = {}
                    if ev == "tool" and p.get("status") == "run":
                        tools.append(str(p.get("name") or ""))
                    elif ev == "subtask":
                        plans.append(str(p.get("title") or ""))
                    elif ev == "clarify_ask":
                        clarifies.append(p)
                    elif ev == "token":
                        texts.append(str(p.get("delta") or ""))
                    elif ev == "done":
                        done = (p.get("data") or p)
                ev, data = None, []
    secs = time.time() - t0
    text = "".join(texts)
    print("耗时 %.1fs  正文 %d 字  工具 %d 次" % (secs, len(text), len(tools)))

    hits = [t for t in tools if any(k in t for k in PLATFORM_TOOLS)]
    print("\nJ1 计划子任务：")
    for p in dict.fromkeys(plans):
        print("   -", p[:88])
    bad_plan = [p for p in plans if any(k in p for k in PLAN_MARKS)]
    print("J1 平台定位/写入类子任务：", bad_plan[:3] or "无")
    from collections import Counter
    print("\nJ2 平台类工具调用：", Counter(t for t in hits) or "无")
    print("J2 全部工具：", Counter(tools).most_common(8) or "无")

    ok = True
    if bad_plan:
        print("FAIL J1 仍派发了平台定位/写入子任务")
        ok = False
    if hits:
        print("FAIL J2 仍调用了平台工具")
        ok = False
    if done:
        u = done.get("usage") or {}
        print("\nJ3 done.usage =", json.dumps(u, ensure_ascii=False),
              "msg_type =", done.get("msg_type"))
        early = bool(done.get("clarify_asked")) or done.get("msg_type") in ("clarify", "hil_confirm")
        if not early and not u.get("elapsed_ms"):
            print("FAIL J3 done 缺 elapsed_ms")
            ok = False
    print("J4 clarify_ask 卡片数 =", len(clarifies),
          "（正文提到确认：%s）" % ("是" if "确认" in text else "否"))
    if "请你确认" in text and not clarifies:
        print("WARN J4 正文要求确认但没发卡（本轮可能未走到 ask_user）")

    c = sqlite3.connect(DB)
    row = c.execute("SELECT card_data FROM messages WHERE conversation_id=? AND role='assistant' "
                    "ORDER BY id DESC LIMIT 1", (cid,)).fetchone()
    if row:
        cd = json.loads(row[0] or "{}")
        st = (cd.get("summary") or {}).get("stats") or {}
        print("\nJ3 落库 usage =", json.dumps(cd.get("usage"), ensure_ascii=False))
        print("J3 落库 summary.stats =", json.dumps(st, ensure_ascii=False))
        print("J3 折叠产物数 =", len(((cd.get("exec") or {}).get("process_artifacts") or [])))
        if not cd.get("usage") or not st:
            print("FAIL J3 落库缺统计")
            ok = False
    c.execute("DELETE FROM run_frames WHERE run_id IN (SELECT run_id FROM run_sessions "
              "WHERE conversation_id=?)", (cid,))
    c.execute("DELETE FROM run_sessions WHERE conversation_id=?", (cid,))
    c.execute("DELETE FROM messages WHERE conversation_id=?", (cid,))
    c.execute("DELETE FROM conversations WHERE id=?", (cid,))
    c.commit()
    c.close()
    print("\n结论：", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
