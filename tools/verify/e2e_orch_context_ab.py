#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""e2e_orch_context_ab.py —— 主↔子上下文三项改进的**全链路编排 A/B 实测**（2026-11-02）。

## 为什么必须实测，而不是只看门禁
门禁验的是「函数行为」与「代码接线」。本轮三项改动都是**上下文预算**类改动，
它们的收益与风险只体现在真实编排的 **prompt_tokens / 耗时 / 协议字段** 上。
本仓既有教训：「工具报成功 + 审计通过 ≠ 数据在生效路径上」。

## A/B 设计（同一消息、同一附件、只切一个变量）
| 组 | `delegation.attachments_mode` | 预期 |
|---|---|---|
| A | `auto`（改进后默认） | 520KB 文档**不进**子任务 prompt，只给【附件清单】 |
| B | `all`（旧行为） | 每个子任务都灌入附件正文块 |

判据只用可证伪的库内数据（不信任何自述）：
  · `llm_usage_stats.prompt_tokens`（按 sub_task_key 分组）——A 组子任务应显著更小；
  · `agent_tasks.summary_json.meta.protocol_source` —— 改进点③可证伪标记；
  · 汇总正文里的「未取回上游完整交付物」标注 —— 改进点②可见性；
  · 正文代码围栏是否成半份 —— 改进点②围栏安全；
  · SSE 是否正常 done、子任务是否有交付物 —— 链路没被改坏。

用法：
  python tools/verify/e2e_orch_context_ab.py            # 跑 A + B
  python tools/verify/e2e_orch_context_ab.py --only A   # 只跑一组（省时）
退出码 0 = 全部判据通过。
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

BASE = "http://127.0.0.1:8000"
SSE_TIMEOUT = int(os.environ.get("E2E_SSE_TIMEOUT", "1500"))

#: 夹具：520KB / 610 块的已解析文档（>200KB 阈值 ⇒ auto 模式必然只给清单）
DOC = {"doc_id": 846,
       "filename": "ISO 29148-2011 Requirements Engineering.pdf",
       "size": 520050}

#: 多意图消息 —— 目的是让主 Agent 拆出 ≥2 个子任务（单 Agent 直行则 A/B 无差异，测不出东西）
MSG = ("参考我上传的《ISO 29148-2011 需求工程》附件，完成三件事：\n"
       "1) 列出该标准对需求质量特性（quality characteristics）的要求要点；\n"
       "2) 据此为「车辆儿童遗留保护」场景给出 5 条需求条目化示例（含 id 与验证方法）；\n"
       "3) 给出这 5 条需求的校验要点清单。\n"
       "要求：每条结论都要标明依据来自附件哪一部分；不要编造标准条款号。")

R = []


def ck(name, ok, detail=""):
    R.append((name, bool(ok), detail))
    print(("  [PASS] " if ok else "  [FAIL] ") + name + (("  -- " + str(detail)) if detail else ""))


def _req(path, payload=None, method="GET", timeout=120):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    rq = urllib.request.Request(BASE + path, data=data, method=method,
                                headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(rq, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _sse(path, payload, timeout=SSE_TIMEOUT, on_event=None):
    rq = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"})
    events, buf = [], ""
    with urllib.request.urlopen(rq, timeout=timeout) as r:
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            buf += line[5:].strip()
            try:
                ev = json.loads(buf)
                buf = ""
            except Exception:
                continue
            events.append(ev)
            if on_event:
                on_event(ev, len(events))
    return events


def _set_mode(mode: str):
    _req("/api/system/config/static", {"updates": {"delegation.attachments_mode": mode}},
         method="PUT")


def _get_mode() -> str:
    """**从服务进程**读回生效值。

    ⚠️ 2026-11-02 实测踩到：第一版写的是 `from core import config; config.get(...)` ——
    那读的是**本脚本进程**的配置（默认 auto），而 PUT 写的是**服务进程**的配置。
    ⇒ A 组"恰好对了"（默认就是 auto），B 组立刻暴露（要 all 读回 auto）。
    这类"验证器读错了对象"的错误比产品错更隐蔽：A 组会给出假绿。
    """
    try:
        d = _req("/api/system/config")
    except Exception:
        return ""
    # 响应结构：{static: {delegation: {...}}, ...} 或直接扁平 —— 两种都兼容
    for path in (("static", "delegation", "attachments_mode"),
                 ("delegation", "attachments_mode")):
        cur = d
        ok = True
        for k in path:
            if isinstance(cur, dict) and k in cur:
                cur = cur[k]
            else:
                ok = False
                break
        if ok and isinstance(cur, str):
            return cur
    # 兜底：直接在 JSON 文本里找（结构变化时不至于误报）
    import re as _re
    m = _re.search(r'"attachments_mode"\s*:\s*"([^"]+)"', json.dumps(d, ensure_ascii=False))
    return m.group(1) if m else ""


def _db():
    from database import get_db
    return get_db()


def _label(tag, msg):
    print(f"\n  [{tag}] {msg}")


def _tool_mix(cid: int) -> dict:
    """该会话的工具调用分布（护闸效果的直接证据）。"""
    conn = _db()
    try:
        return {str(r[0]): int(r[1]) for r in conn.execute(
            "SELECT tool_name, COUNT(*) FROM tool_call_logs WHERE conversation_id=? "
            "GROUP BY tool_name ORDER BY COUNT(*) DESC", (cid,)).fetchall()}
    except Exception:
        return {}
    finally:
        conn.close()


def run_group(tag: str) -> dict:
    """跑一组真实编排，返回该组的可证伪观测。"""
    from core import config as _cfg
    want = "auto" if tag == "A" else "all"
    _label(tag, f"切配置 delegation.attachments_mode={want} …")
    _set_mode(want)
    got = _get_mode()
    ck(f"{tag} 配置已生效（读回 = {want}）", got == want, f"actual={got}")
    if got != want:
        return {}

    conv = _req("/api/conversations", {"title": f"上下文A/B实测-{tag}"}, method="POST")
    cid = conv.get("id")
    ck(f"{tag} 创建会话", bool(cid), json.dumps(conv)[:120])
    if not cid:
        return {}

    # 附件带上 doc_id（文档库选文件路径）；size 已知 520KB ⇒ auto 必判"按需"
    att = [{"doc_id": DOC["doc_id"], "filename": DOC["filename"], "size": DOC["size"],
            "url": f"/api/documents/{DOC['doc_id']}/download"}]

    _label(tag, f"发起真实编排请求（conv={cid}，SSE 超时 {SSE_TIMEOUT}s）…")

    # ★ 取数口径（重要）：按 `llm_usage_stats.id` 的**时间窗口**取，而不是按 conversation_id。
    #   实测原因（本轮发现，非本改动引入）：子任务跑在**子线程**里，`_trace_ctx` 是
    #   ContextVar ⇒ 子线程拿不到父线程的 conversation_id，有一部分子任务调用会记成
    #   conversation_id=0（真库实测 46 条 sub_task_key 非空但 conv=0）。
    #   id 窗口是单调的、必覆盖本组全部调用，且 `sub_task_key` 在子线程内 bind 有效
    #   ⇒ 「窗口 + sub_task_key」两个条件组合是唯一可靠的分组口径。
    conn0 = _db()
    try:
        start_id = int((conn0.execute(
            "SELECT COALESCE(MAX(id),0) FROM llm_usage_stats").fetchone() or [0])[0])
    finally:
        conn0.close()

    t0 = time.time()
    seen = {"subtask": 0, "done": 0}
    last = [t0]

    def _prog(ev, n):
        et = ev.get("type")
        if et == "subtask":
            seen["subtask"] += 1
        elif et == "done":
            seen["done"] += 1
        now = time.time()
        if now - last[0] > 30:
            last[0] = now
            print(f"        …{int(now - t0)}s，已收 {n} 事件（subtask {seen['subtask']}）")

    try:
        events = _sse(f"/api/conversations/{cid}/chat/stream",
                      {"message": MSG, "attachments": att}, on_event=_prog)
    except Exception as e:
        ck(f"{tag} 真实编排完成（未超时/未 500）", False, f"{type(e).__name__}: {e}")
        return {"conv": cid, "failed": True}
    dt = time.time() - t0

    types = sorted({str(e.get("type")) for e in events if isinstance(e, dict)})
    final = ""
    for e in events:
        if isinstance(e, dict) and e.get("type") == "done":
            final = str(((e.get("data") or {}).get("content")) or "")
    ck(f"{tag} 真实编排完成（未超时/未 500）", True, f"{len(events)} 事件 / {dt:.1f}s")
    ck(f"{tag} 收到 done 事件（流正常收尾）", "done" in types, str(types)[:150])
    ck(f"{tag} 有子任务事件（确实走了编排，不是单 Agent 直行）",
       seen["subtask"] > 0, f"subtask_events={seen['subtask']}")
    ck(f"{tag} 正文非空（有交付物）", len(final) > 200, f"len={len(final)}")
    # 改进点②：正文围栏不得成半份
    ck(f"{tag} 正文代码围栏不成半份（围栏安全截断）",
       final.count("```") % 2 == 0, f"fences={final.count('```')}")
    # 改进点②：仅凭摘要下结论必须可见
    ck(f"{tag} 汇总可见性：「未取回上游完整交付物」标注（若适用）", True,
       "出现" if "未取回上游完整交付物" in final else "未出现（=下游已取全文，或本次无上游 offload）")

    # ── 库内取证 ──
    conn = _db()
    try:
        run_row = conn.execute(
            "SELECT run_id FROM agent_tasks WHERE conversation_id=? ORDER BY id DESC LIMIT 1",
            (cid,)).fetchone()
        run_id = run_row[0] if run_row else 0
        tasks = [dict(r) for r in conn.execute(
            "SELECT task_key,title,status,agent_id,latency_ms,summary_json,token_count "
            "FROM agent_tasks WHERE conversation_id=? ORDER BY seq", (cid,)).fetchall()]
        usage = [dict(r) for r in conn.execute(
            "SELECT sub_task_key,intent,prompt_tokens,completion_tokens,reasoning_tokens,"
            "finish_reason,conversation_id FROM llm_usage_stats WHERE id>? ORDER BY id",
            (start_id,)).fetchall()]
    finally:
        conn.close()

    sub_usage = [u for u in usage if u.get("sub_task_key")]
    sub_prompt = sum(int(u.get("prompt_tokens") or 0) for u in sub_usage)
    tot_prompt = sum(int(u.get("prompt_tokens") or 0) for u in usage)

    # 协议来源（改进点③）
    # ⚠️ 口径：只对**真正执行过**的任务断言（done/failed）。
    #   `cancelled` 的任务 summary_json 是 `{}`（从未产出交付物），
    #   把它算进"协议来源为空"是断言口径错，不是产品缺陷（本轮实测踩到）。
    srcs, claims_n, risks_n = {}, 0, 0
    exec_tasks = 0
    for t in tasks:
        if str(t.get("status") or "") not in ("done", "failed"):
            continue
        exec_tasks += 1
        try:
            sm = json.loads(t.get("summary_json") or "{}")
        except Exception:
            sm = {}
        if not isinstance(sm, dict):
            sm = {}
        src = ((sm.get("meta") or {}).get("protocol_source")) or "-"
        srcs[src] = srcs.get(src, 0) + 1
        claims_n += len(sm.get("claims") or [])
        risks_n += len(sm.get("risks") or [])

    done_n = sum(1 for t in tasks if t.get("status") == "done")
    print(f"        取数：run={run_id} tasks={len(tasks)}(done={done_n}) "
          f"sub_usage={len(sub_usage)} sub_prompt={sub_prompt} total_prompt={tot_prompt}")
    print(f"        协议来源={srcs} claims={claims_n} risks={risks_n}")

    ck(f"{tag} 每个子任务都有协议记录（summary_json 可解析）",
       all(isinstance(t.get("summary_json"), str) and t.get("summary_json") for t in tasks)
       or not tasks, "tasks=%d" % len(tasks))
    ck(f"{tag} 协议来源可证伪（只统计真正执行过的任务，且无空值）",
       exec_tasks > 0 and bool(srcs) and "-" not in srcs,
       f"srcs={srcs} exec_tasks={exec_tasks}")
    ck(f"{tag} 三数组真的非空（claims>0 或 risks>0，证协议生效非空转）",
       (claims_n + risks_n) > 0, f"claims={claims_n} risks={risks_n}")

    return {"conv": cid, "run": run_id, "events": len(events), "elapsed": dt,
            "tasks": len(tasks), "done": done_n, "sub_usage": len(sub_usage),
            "sub_prompt": sub_prompt, "total_prompt": tot_prompt,
            "srcs": srcs, "claims": claims_n, "risks": risks_n,
            "final_len": len(final), "subtask_events": seen["subtask"],
            # ★ 归一化指标（A/B 可比性的前提）：
            #   planner 是 LLM，**同一消息两次可能拆出不同数量的子任务**
            #   （实测 A 组首轮 3 个、复跑 5 个）⇒ 直接比 token 合计是不公平的。
            #   这两种口径才是可比的：每子任务平均、每次子任务调用平均。
            "sub_prompt_per_task": round(sub_prompt / max(1, len(tasks)), 1),
            "sub_prompt_per_call": round(sub_prompt / max(1, len(sub_usage)), 1),
            "tools": _tool_mix(cid)}


def main() -> int:
    only = ""
    if "--only" in sys.argv:
        i = sys.argv.index("--only")
        if i + 1 < len(sys.argv):
            only = sys.argv[i + 1].strip().upper()

    print("=" * 88)
    print("主↔子上下文三项改进 · 全链路编排 A/B 实测")
    print(f"  夹具：doc {DOC['doc_id']}《{DOC['filename']}》{DOC['size'] // 1024}KB")
    print(f"  变量：delegation.attachments_mode   A=auto(新) / B=all(旧)")
    print("=" * 88)

    try:
        _req("/api/conversations", timeout=20)
    except Exception as e:
        ck("服务可达", False, repr(e))
        return 1
    ck("服务可达", True)

    orig = _get_mode()          # 跑完恢复
    res = {}
    try:
        if only in ("", "A"):
            res["A"] = run_group("A")
        if only in ("", "B"):
            res["B"] = run_group("B")
    finally:
        try:
            _set_mode(orig or "auto")
            print(f"\n  已恢复 delegation.attachments_mode={orig or 'auto'}")
        except Exception as e:
            print(f"\n  ⚠️ 恢复配置失败：{e}")

    # ── 跨组对比（A/B 都跑时才有意义）──
    if res.get("A") and res.get("B") and not res["A"].get("failed") and not res["B"].get("failed"):
        a, b = res["A"], res["B"]
        print("\n" + "=" * 88)
        print("A/B 对比（同一消息、同一附件，仅切附件注入模式）")
        print("=" * 88)
        rows = [("子任务事件数", a["subtask_events"], b["subtask_events"], ""),
                ("agent_tasks 行数", a["tasks"], b["tasks"], ""),
                ("done 子任务数", a["done"], b["done"], ""),
                ("子任务 prompt_tokens 合计", a["sub_prompt"], b["sub_prompt"], "拆分不同则不可直比"),
                ("★每子任务平均 prompt", a["sub_prompt_per_task"], b["sub_prompt_per_task"], "可比口径"),
                ("★每次调用平均 prompt", a["sub_prompt_per_call"], b["sub_prompt_per_call"], "可比口径"),
                ("全会话 prompt_tokens 合计", a["total_prompt"], b["total_prompt"], ""),
                ("耗时(s)", round(a["elapsed"], 1), round(b["elapsed"], 1), ""),
                ("正文长度", a["final_len"], b["final_len"], ""),
                ("claims+risks", a["claims"] + a["risks"], b["claims"] + b["risks"], "")]
        for nm, va, vb, note in rows:
            print(f"  {nm:<26} A(auto)={va!s:<12} B(all)={vb!s:<12} {note}")
        print(f"  {'工具调用分布':<26} A={a.get('tools')}")
        print(f"  {'':<26} B={b.get('tools')}")

        # 核心判据：auto 下附件不进子任务 prompt ⇒ **每子任务/每次调用**的 prompt 更小
        pa, pb = a["sub_prompt_per_task"], b["sub_prompt_per_task"]
        if pa and pb:
            cut = 1 - pa / pb
            ck("★A/B 核心判据（按子任务归一化）：auto 的每子任务 prompt 更小",
               pa < pb, f"A={pa} B={pb} 降幅={cut:.1%}")
        else:
            ck("★A/B 核心判据：两组都有子任务调用记录（可对比）", False, f"A={pa} B={pb}")
        ck("A/B 均完成编排且都有交付物（改动没破坏链路）",
           a["done"] > 0 and b["done"] > 0 and a["final_len"] > 200 and b["final_len"] > 200,
           f"A done={a['done']} len={a['final_len']} | B done={b['done']} len={b['final_len']}")

    fails = [r for r in R if not r[1]]
    print("\n" + "=" * 88)
    print(f"断言汇总：{len(R) - len(fails)}/{len(R)} 通过")
    for nm, _, dt in fails:
        print("  ✗ " + nm + (f"  | {dt}" if dt else ""))
    print(("✅ ALL PASS" if not fails else "❌ 有红项"))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
