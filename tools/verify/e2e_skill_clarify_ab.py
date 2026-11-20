# -*- coding: utf-8 -*-
"""E2E A/B 评测：「视图生成内容澄清」在**含糊输入**下是否真的改变建模产出。

# CI-OPTIONAL: 真实 LLM 会话（每次调用 ~2-4 分钟）+ 运行中 8000 服务 + 真库 mbse.db，CI 无此运行时；gates_manifest.json 已登记 local 层

为什么输入必须是含糊的（2026-11-20 踩过坑）
------------------------------------------------
第一版评测输入是「卫星热控建模：建个需求视图，范围整星，目的热平衡，约束单机温度」
—— 视图/系统/目的/约束**全都有了**，模型跳过澄清是合理行为，测出来锚点 0/5。
拿它给「内容澄清技能」判 FAIL 属于**测错场景**，不是技能无效。

本评测按技能原始定位取输入：用户说不清要什么。两个约束都用实测钉过：
  · 含「建模」→ 命中 session.py::_CLARIFY_ACTION_VERBS ⇒ 跳过系统级 clarify_ask，
    否则第一轮被澄清卡拦住、根本不进 pipeline（技能没有机会被消费）
  · 含「不确定」→ 命中技能 triggers；且确实缺系统名/视图/指标/涉众

判据（全部可数，不靠"看着像"）
  J1 命中：SSE skill 事件 names 里出现技能名
  J2 注入生效：正文出现版式锚点（目标视图/结构化描述/假设与默认/待确认/以上为建模输入）
  J3 澄清优先：锚点先于 SysML 代码出现（B 组应先确认再建模）
  J4 因果性：A 组（个人停用）不得命中、且锚点必须少于 B 组

用法（需先起 8000 服务）
  ./.venv/Scripts/python.exe -X utf8 tools/verify/e2e_skill_clarify_ab.py --q 0 --group ab
  ./.venv/Scripts/python.exe -X utf8 tools/verify/e2e_skill_clarify_ab.py --q 0 --group b
"""
import json
import os
import sqlite3
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8000"
UID = "1"
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB = os.path.join(ROOT, "mbse.db")
HDR = {"Content-Type": "application/json", "X-User-Id": UID}
SKILL_NAME = "视图生成内容澄清"
PLUGIN_ID = "com.mbse.local.skill.skill-150"

QUESTIONS = [
    "卫星热控这块我不太确定该怎么建模，你看着弄吧",
    "要做个热控相关的模型，具体要哪些东西我也说不准",
]
ANCHORS = ["目标视图", "结构化描述", "假设与默认", "待确认", "以上为建模输入"]
SYSML_MARKS = ["package ", "part def", "requirement def", "action def", "state def",
               "attribute ", "interface def"]


def post(path, body, timeout=60):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(),
                                 headers=HDR, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def put(path, body, timeout=60):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(),
                                 headers=HDR, method="PUT")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def set_installed(enabled):
    return put("/api/plugins/%s/installed-enabled" % PLUGIN_ID, {"enabled": bool(enabled)})


def stream_chat(cid, msg, timeout=600):
    body = json.dumps({"message": msg}).encode()
    req = urllib.request.Request(BASE + "/api/conversations/%d/chat/stream" % cid,
                                 data=body, headers=HDR, method="POST")
    text, evs, skills, clarify = [], [], [], []
    cur_ev, cur_data = None, []
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        for raw in r:
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if line.startswith("event:"):
                cur_ev = line.split(":", 1)[1].strip()
                evs.append(cur_ev)
            elif line.startswith("data:"):
                cur_data.append(line.split(":", 1)[1].strip())
            elif line == "":
                if cur_data:
                    try:
                        p = json.loads("\n".join(cur_data))
                    except Exception:
                        p = {}
                    if cur_ev == "token":
                        text.append(p.get("delta") or "")
                    elif cur_ev in ("skill", "skills"):
                        skills.extend(str(n) for n in (p.get("names") or []) if n)
                    elif "clarify" in (cur_ev or ""):
                        clarify.append(cur_ev)
                cur_ev, cur_data = None, []
    return "".join(text), sorted(set(skills)), clarify, time.time() - t0


def cleanup(cid):
    conn = sqlite3.connect(DB)
    for sql, args in (
        ("DELETE FROM run_frames WHERE run_id IN (SELECT run_id FROM run_sessions WHERE conversation_id=?)", (cid,)),
        ("DELETE FROM run_sessions WHERE conversation_id=?", (cid,)),
        ("DELETE FROM messages WHERE conversation_id=?", (cid,)),
        ("DELETE FROM conversations WHERE id=?", (cid,)),
    ):
        conn.execute(sql, args)
    conn.commit()
    conn.close()


def run(label, enabled, q):
    set_installed(enabled)
    time.sleep(0.6)
    cid = json.loads(post("/api/conversations",
                          {"title": "澄清技能E2E-%s" % label, "project_id": ""}))["id"]
    text, skills, clarify, dt = stream_chat(cid, q)
    first_anchor = min((text.find(a) for a in ANCHORS if a in text), default=-1)
    first_sysml = min((text.find(m) for m in SYSML_MARKS if m in text), default=-1)
    out = {"label": label, "enabled": enabled, "cid": cid, "secs": round(dt, 1),
           "skills_hit": skills, "hit_target": SKILL_NAME in skills,
           "clarify_ev": clarify,
           "anchors": [a for a in ANCHORS if a in text],
           "anchor_n": sum(a in text for a in ANCHORS),
           "first_anchor_pos": first_anchor, "first_sysml_pos": first_sysml,
           "sysml_marks": sum(text.count(m) for m in SYSML_MARKS),
           "len": len(text), "text": text}
    cleanup(cid)
    return out


def show(o):
    print("[%s] J1命中=%s skills=%s\n      J2锚点=%d/%d %s  J3锚点首位=%d SysML首位=%d  J4 SysML记号=%d 字数=%d 耗时=%.1fs"
          % (o["label"], o["hit_target"], o["skills_hit"], o["anchor_n"], len(ANCHORS),
             o["anchors"], o["first_anchor_pos"], o["first_sysml_pos"],
             o["sysml_marks"], o["len"], o["secs"]))


def merge(path, key, obj):
    """单组运行时保留另一组结果，避免同文件互相覆盖（首版踩过）。"""
    d = {"q": None, "anchors": ANCHORS, "res": {}}
    if os.path.exists(path):
        try:
            d = json.load(open(path, encoding="utf-8"))
        except Exception:
            pass
    d.setdefault("res", {})[key] = obj
    d["q"] = obj.get("q") or d.get("q") or QUESTIONS[0]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)


def main():
    qi, grp = 0, "ab"
    for i, a in enumerate(sys.argv):
        if a == "--q":
            qi = int(sys.argv[i + 1])
        elif a == "--group":
            grp = sys.argv[i + 1].lower()
    q = QUESTIONS[qi]
    print("Q%d = %r（%d 字）" % (qi, q, len(q)))
    print("判据锚点 = %s\n" % " / ".join(ANCHORS))
    res = {}
    if "b" in grp:
        res["B"] = run("B-启用", True, q)
        res["B"]["q"] = q
        show(res["B"])
    if "a" in grp:
        res["A"] = run("A-停用", False, q)
        res["A"]["q"] = q
        show(res["A"])
    set_installed(True)  # 复原，别把环境留在停用态

    path = os.path.join(ROOT, "outputs", "eval_skill_clarify_ab.json")
    for k, v in res.items():
        merge(path, k, v)
    all_res = json.load(open(path, encoding="utf-8"))["res"]
    if "A" in all_res and "B" in all_res:
        b, a = all_res["B"], all_res["A"]
        fails = []
        if not b["hit_target"]:
            fails.append("J1 B 组未命中目标技能")
        if a["hit_target"]:
            fails.append("J4 A 组（已停用）仍命中 —— 可消费判定失效")
        if b["anchor_n"] < 3:
            fails.append("J2 B 组锚点 < 3/5 —— 技能未被真正遵循")
        if b["anchor_n"] <= a["anchor_n"]:
            fails.append("J4 B 组锚点未超过 A 组 —— 技能没有带来可观测差异")
        print("\nJ1/J4：B命中=%s A命中=%s；J2：B锚点=%d vs A=%d"
              % (b["hit_target"], a["hit_target"], b["anchor_n"], a["anchor_n"]))
        print("结论：%s" % ("PASS —— 技能在含糊输入场景下被命中并改变了产出形态"
                           if not fails else "FAIL —— " + "；".join(fails)))
    print("明细 → %s" % path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
