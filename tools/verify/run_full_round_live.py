# -*- coding: utf-8 -*-
"""完整一轮真机编排验证（外部 HTTP 入口 → SSE 事件流 → 落库后验）。

补上此前唯一未做项：**plan_guard 的 live 卡片值**（此前只有打桩的 plan_block_live 7/0，
真实 LLM 规划下 `card_data.plan_guard` 是否真有值未验证过）。

用法：
  python tools/verify/run_full_round_live.py run       # 跑完整一轮（门禁基线 + 真机编排）
  python tools/verify/run_full_round_live.py inspect <conv_id> [events_json]
                                                        # 编排结束后落库后验
退出码 0 = 全绿。

判据（inspect）：
  A. 流正常收尾（done 事件 / 无 500 / 无挂死）
  B. ★ card_data.plan_guard 非 null（live 卡片值——本轮主目标）
  C. replan_decisions 有本会话新行（health 巡检留痕）
  D. 子任务：必需 done；非必需在早停触发时 cancelled（不触发则 done 也算过）
  E. card_data.exec.replan 或 plan_revised 事件留痕（触发与否都登记）
  F. 全程无未捕获异常（回复非 error 卡）
"""

import json
import os
import sys
import time
import urllib.request

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

BASE = "http://127.0.0.1:8000"
GOAL = ("对某型无人机地面控制站做需求条目化和架构骨架两步，"
        "完成后可选给一份评审建议（非必需）。")
EVENTS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "_full_round_events.json")

PASS = 0
FAIL = 0
FAILED = []


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ✓ %s" % label)
    else:
        FAIL += 1
        FAILED.append(label)
        print("  ✗ %s   %s" % (label, detail))


def _post(path, payload, timeout=60):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# ─────────────────────────────────────────────────────────────
def cmd_run():
    print("=" * 74)
    print("完整一轮真机编排（门禁基线 + 真实 SSE 会话）")
    print("=" * 74)

    # 0. 服务可达 + 配置快照
    try:
        with urllib.request.urlopen(BASE + "/api/health/live", timeout=10) as r:
            live = json.loads(r.read().decode("utf-8")).get("status") == "alive"
    except Exception as e:
        print("服务不可达：%r" % e)
        return 1
    check("0.1 服务可达（/api/health/live=alive）", live)

    cfg = json.loads(urllib.request.urlopen(
        BASE + "/api/system/config", timeout=10).read().decode("utf-8"))
    st = cfg.get("static") or {}
    rp = st.get("replanning") or {}
    check("0.2 replanning 总闸开启（enabled=true）", rp.get("enabled") is True,
          json.dumps(rp, ensure_ascii=False)[:150])
    check("0.3 巡检开启（health_probe=true）", rp.get("health_probe") is True)
    check("0.4 早停开启（early_stop=true）", rp.get("early_stop") is True)
    check("0.5 重画显式关闭（redraw_on_stall=false，总闸连带陷阱已规避）",
          rp.get("redraw_on_stall") is False)

    from database import get_db
    conn = get_db()
    gr = conn.execute(
        "SELECT rule_value FROM generate_rules WHERE rule_key='pipeline_guard_mode'"
    ).fetchone()
    guard_mode = gr["rule_value"] if gr else "(缺省=warn)"
    check("0.6 计划语义校验 mode=%s（warn=只告警可见，不阻断）" % guard_mode,
          guard_mode in ("warn", "block"), guard_mode)
    n_stage = conn.execute(
        "SELECT COUNT(*) c FROM agents WHERE stage_order IS NOT NULL").fetchone()["c"]
    check("0.7 阶段序清单就位（agents.stage_order 共 %d 阶）" % n_stage, n_stage >= 8)
    conn.close()

    # 1. 真机编排会话
    r = _post("/api/conversations", {"title": "完整一轮真机验证-20261104"})
    conv_id = r.get("id")
    check("1.1 创建会话（conv=%s）" % conv_id, bool(conv_id))
    if not conv_id:
        return 1

    print("  → 发起真实编排请求（预计 >480s，请耐心等待）…")
    t0 = time.time()
    events, err = [], None
    try:
        req = urllib.request.Request(
            BASE + "/api/conversations/%s/chat/stream" % conv_id,
            data=json.dumps({"message": GOAL}).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Accept": "text/event-stream"}, method="POST")
        buf = ""
        with urllib.request.urlopen(req, timeout=1500) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if line.startswith("data:"):
                    buf += line[5:].strip()
                    try:
                        events.append(json.loads(buf))
                        buf = ""
                    except Exception:
                        pass
    except Exception as e:
        err = repr(e)
    dt = time.time() - t0

    types = sorted({str(e.get("type")) for e in events if isinstance(e, dict)})
    check("1.2 流未挂死/未 500（%d 事件 / %.0fs）" % (len(events), dt), err is None, err)
    check("1.3 收到 done 事件（正常收尾）", "done" in types, str(types))

    with open(EVENTS_PATH, "w", encoding="utf-8") as f:
        json.dump({"conv_id": conv_id, "goal": GOAL, "elapsed_sec": round(dt, 1),
                   "event_types": types,
                   "events": events if len(json.dumps(events)) < 6_000_000
                             else [e for e in events if e.get("type") != "token"]},
                  f, ensure_ascii=False)
    print("  事件已存 %s（类型：%s）" % (EVENTS_PATH, types))
    print("  → 会话 conv=%s，请继续跑 inspect 做落库后验" % conv_id)
    return 0 if FAIL == 0 else 1


# ─────────────────────────────────────────────────────────────
def cmd_inspect(conv_id, events_path=None):
    conv_id = int(conv_id)
    print("=" * 74)
    print("完整一轮：落库后验（conv=%d）" % conv_id)
    print("=" * 74)

    events = []
    if events_path and os.path.exists(events_path):
        with open(events_path, encoding="utf-8") as f:
            events = json.load(f).get("events") or []
    elif os.path.exists(EVENTS_PATH):
        with open(EVENTS_PATH, encoding="utf-8") as f:
            _d = json.load(f)
            if int(_d.get("conv_id") or 0) == conv_id:
                events = _d.get("events") or []

    from database import get_db
    conn = get_db()

    # A. 会话与回复
    conv = conn.execute("SELECT id, status FROM conversations WHERE id=?",
                        (conv_id,)).fetchone()
    check("A.1 会话存在", bool(conv))
    msgs = conn.execute(
        "SELECT id, role, msg_type, card_data, content FROM messages "
        "WHERE conversation_id=? ORDER BY id", (conv_id,)).fetchall()
    check("A.2 有消息落库（%d 条）" % len(msgs), len(msgs) >= 2)
    assistant = [m for m in msgs if m["role"] == "assistant"]
    check("A.3 有助手回复（%d 条）" % len(assistant), len(assistant) >= 1)

    # B. ★ plan_guard live 卡片值（本轮主目标）
    pg_found = None
    for m in assistant:
        try:
            cd = json.loads(m["card_data"] or "{}")
        except Exception:
            continue
        if cd.get("plan_guard"):
            pg_found = cd["plan_guard"]
            break
    check("B.1 ★ card_data.plan_guard 非 null（live 卡片值真实存在）",
          pg_found is not None,
          "assistant card_data 键：%s" % [sorted(json.loads(m['card_data'] or '{}').keys())
                                          for m in assistant][:3])
    if pg_found:
        print("    plan_guard 内容：%s" % json.dumps(pg_found, ensure_ascii=False)[:400])

    # C. replan 巡检留痕
    rd = conn.execute(
        "SELECT round, level, action, source, reason FROM replan_decisions "
        "WHERE conversation_id=? ORDER BY id", (conv_id,)).fetchall()
    check("C.1 replan_decisions 有本会话新行（%d 条，改造前为 0）" % len(rd),
          len(rd) >= 1)
    for x in rd[:6]:
        print("    [%s/%s] %s — %s" % (x["level"], x["action"], x["source"],
                                       (x["reason"] or "")[:80]))

    # D. 子任务结局
    runs = conn.execute(
        "SELECT DISTINCT run_id FROM agent_tasks WHERE conversation_id=?",
        (conv_id,)).fetchall()
    tasks = []
    for r_ in runs:
        rows = conn.execute(
            "SELECT task_key, title, status, config, seq FROM agent_tasks "
            "WHERE run_id=? ORDER BY seq", (r_["run_id"],)).fetchall()
        for t in rows:
            d = dict(t)
            try:
                d["required"] = bool((json.loads(t["config"] or "{}") or {})
                                     .get("required"))
            except Exception:
                d["required"] = None
            tasks.append(d)
    n_req = sum(1 for t in tasks if t["required"])
    n_done = sum(1 for t in tasks if t["status"] in ("done", "completed"))
    n_cancel = sum(1 for t in tasks if t["status"] in ("cancelled", "canceled"))
    n_fail = sum(1 for t in tasks if t["status"] in ("failed", "error"))
    check("D.1 有编排子任务落库（%d 个，必需 %d，runs=%s）"
          % (len(tasks), n_req, [r_["run_id"] for r_ in runs]), len(tasks) >= 2)
    check("D.2 无 failed 任务（done=%d cancelled=%d failed=%d）"
          % (n_done, n_cancel, n_fail),
          n_fail == 0 and n_done >= 1,
          json.dumps([{k: t[k] for k in ('task_key', 'status', 'required')}
                      for t in tasks], ensure_ascii=False)[:300])
    n_req_done = sum(1 for t in tasks
                     if t["required"] and t["status"] in ("done", "completed"))
    check("D.3 必需任务全部跑完（%d/%d）" % (n_req_done, n_req),
          n_req == 0 or n_req_done == n_req)
    if n_cancel:
        print("    ★ 早停触发：%d 个非必需任务被取消（这就是编排级 ReAct 的价值）" % n_cancel)
    else:
        print("    （本轮早停未触发——判定为计划健康/全部必需跑完，属正常路径，已留痕）")

    # E. plan_revised / replan 卡片事件留痕
    pr_events = [e for e in events
                 if isinstance(e, dict) and "plan_revised" in str(e.get("type") or "")
                 + str(e.get("name") or "") + json.dumps(e.get("data") or {})[:200]]
    exec_replan = False
    for m in assistant:
        try:
            cd = json.loads(m["card_data"] or "{}")
        except Exception:
            continue
        ex = cd.get("exec") or {}
        if ex.get("replan") or cd.get("replan"):
            exec_replan = True
            break
    check("E.1 重规划留痕可查（replan_decisions=%d / plan_revised 事件=%d / 卡片=%s）"
          % (len(rd), len(pr_events), exec_replan),
          len(rd) >= 1 or exec_replan or len(pr_events) >= 1)

    # F. 无未捕获异常
    last = assistant[-1] if assistant else None
    is_err_card = bool(last) and (
        (last["msg_type"] or "") == "error"
        or '"blocked_by"' in (last["card_data"] or "")
        and json.loads(last["card_data"] or "{}").get("blocked_by") not in (None, "plan_guard"))
    check("F.1 回复非 error 卡（全程无未捕获异常）", not is_err_card,
          "msg_type=%s" % (last["msg_type"] if last else None))

    conn.close()
    print()
    print("=" * 74)
    print("汇总：PASS = %d / FAIL = %d" % (PASS, FAIL))
    if FAILED:
        print("失败项：")
        for f_ in FAILED:
            print("  - %s" % f_)
    print("口径：真实 LLM + 真实 SSE 通道（用户实际路径）；只读库验证，不清理"
          "（会话 %d 留档供前端查看）。" % conv_id)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "run":
        sys.exit(cmd_run())
    elif cmd == "inspect":
        sys.exit(cmd_inspect(sys.argv[2],
                             sys.argv[3] if len(sys.argv) > 3 else None))
    else:
        print("用法：run | inspect <conv_id>")
        sys.exit(2)
