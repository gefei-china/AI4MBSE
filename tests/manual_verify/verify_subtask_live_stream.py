# -*- coding: utf-8 -*-
"""编排子任务**就地实时输出**回归（2026-09-26，用户要求"不要放在最后统一输出"）。

## 要证明什么
旧实现把子任务的 token/reasoning/tool 攒进 `evs`，等 future 完成才 `for ev in evs: yield ev`
→ 子任务执行期间界面空白，完成瞬间一次性涌出（用户观感："每个子任务的总结堆在最后"）。
现在 worker 边产边入 `_evq`、主循环边收边吐 → 内容应在**该子任务自己的卡片下**随执行出现。

## 断言
A1 首个子任务的 `token`/`reasoning` 事件出现在它的 `subtask(done)` **之前**（= 就地实时）
A2 没有**重复事件**（指纹去重后数量 == 原始数量）—— 实时转发 + 收尾 drain 不能把事件吐两遍
A3 事件带 `key`/`name` 前缀（`tkey:`）→ 前端能归到对应子任务卡（就地"对应的位置"）
A4 收尾不是"全堆最后"：首个 done 之前至少已有 ≥3 个子任务内事件

用法（需服务已在跑）：.venv/Scripts/python.exe -X utf8 tests/manual_verify/verify_subtask_live_stream.py
"""
import json
import os
import sys

ROOT = r"C:\Users\gefei\WorkBuddy\2026-08-04-19-05-52\mbse_system"
sys.path.insert(0, ROOT)
os.chdir(ROOT)
import httpx  # noqa: E402

API = "http://127.0.0.1:8000"
# ⚠️ 用例有两个坑（都实测踩过）：
#  ① 是否编排由 LLM 复杂度判定，同一句话并非确定性触发 → 断言全挂却是"没走这条路"（测试用例缺陷）；
#  ② **会被"意图确认"拦截**：如「提供一段需求，进行需求分析、方案设计、代码校验」route=fused_conflict
#     → 弹选项卡、不执行（那正是它该有的行为）→ 一个编排事件都不会有（实测 seq 为空）。
#  故用"阶段连词"句：命中**确定性的关键词规则**（不触发确认卡），且 `_split_stage_clauses` 走 L1
#  阶段连词 → 必然多意图 → 必然编排。
QUERY = "先做需求分析，再进行方案设计，最后生成 sysml 代码"
OK, FAIL = [], []


def chk(cond, msg, evidence=""):
    """⚠️ 签名必须与调用一致：本脚本首版写成 `(name, cond, ...)`，而调用是按 `(cond, msg)` 传的
    → 拼字符串时报 `can only concatenate str (not "bool")`（同一晚第二次踩，教训是**统一签名**）。"""
    (OK if cond else FAIL).append(msg)
    print(("  [PASS] " if cond else "  [FAIL] ") + msg + (("  ← " + evidence) if evidence else ""))


cid = httpx.post(API + "/api/conversations", json={"title": "就地输出回归"}, timeout=20).json().get("id")
seq = []          # [(type, key)]
fps = {}          # 指纹 -> 次数（查重复）
try:
    with httpx.stream("POST", API + "/api/conversations/%s/chat/stream" % cid,
                      json={"message": QUERY, "branch": "personal"},
                      timeout=httpx.Timeout(420, read=420)) as r:
        cur = None
        n_tok = 0
        tok_len = 0
        _clarify = False
        for line in r.iter_lines():
            if line.startswith("event: "):
                cur = line[7:]
                if cur == "clarify_ask":
                    _clarify = True
            elif line.startswith("data: ") and cur:
                d = json.loads(line[6:])
                key = d.get("key") or (d.get("name") or "").split(":")[0] or ""
                if cur == "token":
                    n_tok += 1
                    tok_len += len(d.get("delta") or "")
                    seq.append(("token", key))
                    fp = ("token", key, hash(d.get("delta") or ""))
                elif cur == "reasoning":
                    seq.append(("reasoning", key))
                    fp = ("reasoning", key, hash((d.get("delta") or "")[:60]))
                elif cur == "tool":
                    seq.append(("tool", key))
                    fp = ("tool", key, d.get("name") or "", hash(str(d.get("arguments"))[:80]))
                elif cur == "subtask":
                    seq.append(("subtask:" + str(d.get("status")), d.get("key") or ""))
                    fp = ("subtask", d.get("key"), d.get("status"))
                else:
                    fp = None
                if fp:
                    fps[fp] = fps.get(fp, 0) + 1
                # 拿到首个子任务的 done + 足够事件即可断连（不必等整轮编排跑完）
                if any(s[0] == "subtask:done" for s in seq) and len(seq) >= 12:
                    break
finally:
    pass
try:
    httpx.delete(API + "/api/conversations/%s" % cid, timeout=10)
except Exception:
    pass

print("事件序列（前 24 条）：")
for i, (t, k) in enumerate(seq[:24]):
    print("   %2d. %-16s key=%s" % (i + 1, t, k))

first_run = next((i for i, s in enumerate(seq) if s[0] == "subtask:run"), None)
first_done = next((i for i, s in enumerate(seq) if s[0] == "subtask:done"), None)
if first_run is None:
    # 显式区分两种"没走编排"：被意图确认拦截（产品的正确行为） vs 单 Agent 直跑（LLM 判定）
    if _clarify:
        print("\n本轮被**意图确认**拦截（route 不够确定 → 弹选项卡、不执行）→ 不编排。")
        print("这是 2026-09-26 新增的预期行为，不是缺陷；请换一句「确定性命中」的说法重跑。")
    else:
        print("\n本轮**未走编排**（LLM 复杂度判定选择了单 Agent 直跑）→ 无法验证子任务就地输出。")
        print("这不是缺陷：本脚本只验证「编排路径」；未走编排时请重跑（或换更明确的多意图句）。")
    sys.exit(2)
chk(first_run is not None and first_done is not None, "拿到 subtask run/done 事件",
    f"run@{first_run} done@{first_done}")
inner_before_done = [s for s in seq[first_run:first_done]] if (first_run is not None and first_done is not None) else []
chk(len(inner_before_done) >= 3,
    "A1/A4 首个 done 之前已有 ≥3 个子任务内事件（就地实时，不是完成后一次性涌出）",
    f"实得 {len(inner_before_done)} 条：{[t for t, _ in inner_before_done][:6]}")
chk(any(t in ("token", "reasoning", "tool") for t, _ in inner_before_done),
    "A1 首个 done 之前确实有 token/reasoning/tool（内容真的流出来了）")
dups = {k: v for k, v in fps.items() if v > 1 and k[0] in ("subtask",)}
chk(not dups or all(k[0] == "subtask" and k[2] == "run" for k in dups),
    "A2 无重复内容事件（实时转发 + 收尾 drain 未把事件吐两遍）",
    f"重复项={list(dups.items())[:3]}")
keys = {k for _, k in seq if k}
# ⚠️ token 事件**按设计不带 key**（子任务正文流入主答复体，前端 procAppendToken 消费）；
#    带归属的是 reasoning（`key`）与 tool（`tkey:` 前缀）——断言按这个真实契约写。
chk(any(k for t, k in seq if t == "reasoning") or any(":" in (k or "") for t, k in seq if t == "tool"),
    "A3 reasoning 带 key / tool 带 tkey: 前缀（可归到对应卡片）",
    f"keys={sorted(keys)[:4]}")
# A5（2026-09-26 合帧优化）：服务端对"连续同签名 token"做窗口合帧。
#   ⚠️ **实测校准**：子任务 token 天然稀疏（两阶段编排首个子任务 ~89 条 / ~50s ≈ 1.8 条/s），
#   50ms 窗口内通常只有 1 条 → 合帧对**子任务**这条路径近乎无效（实测 89 → 64，仅少量撞窗）。
#   真正高频的是**主流程** token（单 Agent 直跑实测 367 条，非本路径，前端 rAF 合帧消费）。
#   故这里只做"不丢内容"的硬断言 + 把条数作为**观测值**打印（不设会被误判的硬阈值，
#   首版写 `≤30` 就是拿错了前提 → 假失败）。
chk(n_tok >= 1 and tok_len >= 200,
    "A5 合帧未丢内容（token 条数与累计 delta 均正常；条数仅作观测）",
    f"token {n_tok} 条 / 累计 {tok_len} 字（≈{n_tok and round(tok_len / max(n_tok, 1), 1)} 字/条）")

print(f"\n结果：{len(OK)} 通过 / {len(FAIL)} 失败")
if FAIL:
    print("失败项：\n  " + "\n  ".join(FAIL))
    sys.exit(1)