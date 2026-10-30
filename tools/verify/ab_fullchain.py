# -*- coding: utf-8 -*-
"""全链路 A/B 验证：**全部开关打开** vs **全部关闭**，同一真实任务对比。

回答的问题（用户提问）：
  「全部打开后有进行全链路验证吗？效果怎么样，怎么评估的？」

设计（为什么这样评估）：
  · **同一任务、同一模型、同一工程**，只改开关 —— 否则差异无法归因
  · 任务刻意选**明确多步**（"分三步完成…"），否则模型不建待办，
    todo/harness 两个闸根本不会进入视野（那测出来的"无差异"是假的）
  · 每组跑 **2 轮**取均值 —— 单轮受 LLM 随机性影响太大，1 轮定结论不可靠

评估指标（每条都可从库里独立复核）：
  M1 端到端耗时（秒）
  M2 LLM 调用次数与 token（成本）
  M3 新增 stage 是否出现（功能是否真的生效）
  M4 待办条数与状态（计划是否外化）
  M5 拦截次数与 verify 执行次数（强制闸是否动作）
  M6 产出字数（是否因拦截而质量下降）
  M7 异常/残留（是否有副作用）

用法：
  python tools/verify/ab_fullchain.py
退出码 0 = 验证跑完（**不做 pass/fail 判定** —— A/B 是观测量，不是判据）
"""
from __future__ import annotations

import os
import sys
import json
import time
import sqlite3
import urllib.request

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

BASE = "http://127.0.0.1:8000"
ROUNDS = 2

# ★ 刻意选**明确多步**的任务：否则模型不会建待办，两个闸根本没机会动作，
#   那样测出来的"无差异"是假的无差异。
TASK = ("请分三步完成，每步都要真正执行、不要只给建议："
        "1) 查询当前有哪些工程；"
        "2) 选其中一个工程，查询它的包结构或模型树；"
        "3) 基于实际查到的内容，给出一段不超过 200 字的情况总结。")

ALL_ON = {
    "agent.todo_enabled": "true",
    "agent.harness_enabled": "true",
    "agent.obstacle_recovery_enabled": "true",
    "agent.harness_max_blocks": "8",
    "agent.harness_max_stall": "2",
    "agent.budget_degrade_ratio": "0.7",
}
ALL_OFF = {k: ("false" if v == "true" else v) for k, v in ALL_ON.items()
           if k.startswith("agent.todo_enabled") or k.endswith("_enabled")}


def req(method, path, payload=None, timeout=120):
    data = json.dumps(payload).encode() if payload is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def sse(path, payload, timeout=1200):
    rq = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json",
                 "Accept": "text/event-stream"})
    evs, buf = [], ""
    with urllib.request.urlopen(rq, timeout=timeout) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if line.startswith("data:"):
                buf += line[5:].strip()
                try:
                    evs.append(json.loads(buf))
                    buf = ""
                except Exception:
                    pass
    return evs


def set_switches(on: bool, key_map):
    upd = {k: ("true" if on else "false") for k in
           ("agent.todo_enabled", "agent.harness_enabled",
            "agent.obstacle_recovery_enabled")}
    upd.update({k: v for k, v in key_map.items() if not k.endswith("_enabled")})
    return req("PUT", "/api/system/config/static", {"updates": upd})


def run_one(tag: str) -> dict:
    """跑一轮，返回该轮的实测指标（全部从库/事件流独立取）。"""
    c = req("POST", "/api/conversations", {"title": f"AB-{tag}"})
    cid = c["id"]
    t0 = time.time()
    try:
        evs = sse(f"/api/conversations/{cid}/chat/stream", {"message": TASK})
        err = ""
    except Exception as e:
        evs, err = [], repr(e)
    dt = time.time() - t0

    stages = [str(e.get("name") or "") for e in evs
              if isinstance(e, dict) and e.get("type") == "stage"]
    text = "".join(str(e.get("delta") or "") for e in evs
                   if isinstance(e, dict) and e.get("type") == "token")

    from core.config import DB_PATH
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    n_tools = db.execute(
        "SELECT COUNT(*) FROM tool_call_logs WHERE conversation_id=?", (cid,)).fetchone()[0]
    n_fail = db.execute(
        "SELECT COUNT(*) FROM tool_call_logs WHERE conversation_id=? AND ok=0",
        (cid,)).fetchone()[0]
    todos = db.execute(
        "SELECT status, subject, verify, verify_result FROM agent_todos "
        "WHERE conversation_id=?", (cid,)).fetchall()
    llm = db.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(total_tokens),0) t FROM llm_usage_stats "
        "WHERE conversation_id=?", (cid,)).fetchone()
    db.close()

    NEW = {"待办验收闸", "验收未通过 · 继续工作", "验收闸强制放行"}
    return {
        "tag": tag, "cid": cid, "sec": round(dt, 1), "err": err,
        "events": len(evs), "chars": len(text.strip()),
        "n_tools": n_tools, "n_tool_fail": n_fail,
        "n_llm": llm["n"] if llm else 0,
        "tokens": llm["t"] if llm else 0,
        "new_stages": [s for s in stages if s in NEW],
        "stages": stages,
        "n_todo": len(todos),
        "todo_status": [t["status"] for t in todos],
        "todo_has_verify": sum(1 for t in todos if (t["verify"] or "").strip()),
        "todo_has_result": sum(1 for t in todos if (t["verify_result"] or "").strip()),
    }


def summarize(rows, label):
    n = len(rows)
    if not n:
        return
    avg = lambda k: round(sum(r[k] for r in rows) / n, 1)
    print(f"\n  【{label}】{n} 轮均值")
    print(f"    端到端耗时     {avg('sec')} 秒   （各轮：{[r['sec'] for r in rows]}）")
    print(f"    LLM 调用次数   {avg('n_llm')}        token {avg('tokens')}")
    print(f"    工具调用       {avg('n_tools')}  其中失败 {avg('n_tool_fail')}")
    print(f"    产出字数       {avg('chars')}")
    print(f"    待办条数       {avg('n_todo')}   状态 {[r['todo_status'] for r in rows]}")
    print(f"    带验收方式     {avg('todo_has_verify')}   带验收回执 {avg('todo_has_result')}")
    hit = [r["new_stages"] for r in rows]
    print(f"    新增 stage     出现 {sum(1 for h in hit if h)}/{n} 轮：{hit}")


def main():
    print("=" * 74)
    print("全链路 A/B：全部开关打开 vs 全部关闭（同一真实多步任务）")
    print("=" * 74)
    print(f"任务：{TASK[:60]}…")
    print(f"每组 {ROUNDS} 轮")

    try:
        req("GET", "/api/system/config/schema", timeout=20)
    except Exception as e:
        print(f"✗ 服务不可达：{e!r}")
        return 1

    on_rows, off_rows = [], []
    try:
        # ── A 组：全关（基线先跑，避免开关残留污染）──
        set_switches(False, ALL_OFF)
        for i in range(ROUNDS):
            print(f"\n  → A组(全关) 第{i+1}轮…", flush=True)
            r = run_one(f"A{i+1}")
            off_rows.append(r)
            print(f"     {r['sec']}s / {r['chars']}字 / 待办{r['n_todo']} "
                  f"/ 新stage{r['new_stages']}")

        # ── B 组：全开 ──
        set_switches(True, ALL_ON)
        for i in range(ROUNDS):
            print(f"\n  → B组(全开) 第{i+1}轮…", flush=True)
            r = run_one(f"B{i+1}")
            on_rows.append(r)
            print(f"     {r['sec']}s / {r['chars']}字 / 待办{r['n_todo']} "
                  f"/ 新stage{r['new_stages']}")
    finally:
        set_switches(False, ALL_OFF)
        print("\n  （已恢复全部开关为关闭）")

    summarize(off_rows, "A组 · 全部关闭（基线）")
    summarize(on_rows, "B组 · 全部打开")

    # ── 差异 ──
    def avg(rs, k):
        return round(sum(r[k] for r in rs) / len(rs), 1) if rs else 0
    print("\n" + "=" * 74)
    print("差异（B全开 − A全关）")
    print("=" * 74)
    print(f"  耗时      {avg(on_rows,'sec') - avg(off_rows,'sec'):+.1f} 秒")
    print(f"  LLM 调用  {avg(on_rows,'n_llm') - avg(off_rows,'n_llm'):+.1f} 次")
    print(f"  token     {avg(on_rows,'tokens') - avg(off_rows,'tokens'):+.0f}")
    print(f"  产出字数  {avg(on_rows,'chars') - avg(off_rows,'chars'):+.0f}")
    print(f"  待办条数  {avg(on_rows,'n_todo') - avg(off_rows,'n_todo'):+.1f}")
    print(f"  新增stage 出现轮数 {sum(1 for r in on_rows if r['new_stages'])}"
          f" vs {sum(1 for r in off_rows if r['new_stages'])}")

    # 异常检查
    errs = [r for r in on_rows + off_rows if r["err"]]
    print(f"\n  异常：{len(errs)} 个" + (f" → {errs[0]['err'][:120]}" if errs else "（无）"))

    print("\n" + "-" * 74)
    print("说明：本脚本是**观测量**，不做 pass/fail 判定 ——")
    print("      A/B 差异需要结合任务性质解读（见交付说明），不能只看数字大小。")
    print("-" * 74)
    return 0


if __name__ == "__main__":
    sys.exit(main())
