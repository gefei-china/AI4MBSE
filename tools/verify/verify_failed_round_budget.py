"""verify_failed_round_budget — 判据：**轮次上限只约束失败重试，不限制正常调用**。

## 语义（米爸 2026-10-09 纠正）
「工具调用上限」要管的是**失败重试**，不是**正常推进**：
- **成功的轮次不扣预算** —— 模型正常推进（查资料→产码→校验）多少轮都不该被截断
- **只有失败/无效的轮次才扣**（`ok=0` / 超时 / 结果为空 / 全被跳过）

## 为什么旧实现是缺陷
`for _round in range(max_tool_rounds)` 把成功轮与失败轮**一并计数**
⇒ 成功推进被失败轮"连坐"截断。
实测八视图 41 轮里 **29 轮（71%）是成功推进**，
v1 的 6 轮里只有 3 轮是真推进 —— 却和"查 6 次都查不到"付**同样代价**。
⇒ 这正是 v6 报 78 个语法错后进无工具收尾的直接原因。

## 判据
① `charge_failed_round` 单元：成功轮不扣、失败轮扣、到上限即停
② **成对实测**：同一组轮次，成功版比失败版**多跑很多轮**（不受限）
③ 两侧（execute.py / stream.py）**默认值一致**且都读新配置
④ 真实链路：mock 连续成功 8 轮 ⇒ **不被 6 轮截断**（旧实现会截）

## 变异自证（必跑）
把 `charge_failed_round` 改成恒不扣（= 旧语义），断言判据**变红**。

## 用法
    ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_failed_round_budget.py
"""
from __future__ import annotations

import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def unit_checks() -> list[tuple[str, bool]]:
    from agent.loop_guard import LoopGuard
    out = []
    g = LoopGuard()
    for _ in range(10):
        g.charge_failed_round(True)
    out.append((f"连续 10 个**成功轮**不扣预算（failed={g.failed_rounds}，期望 0）",
                g.failed_rounds == 0))
    out.append(("成功 10 轮后 failed_rounds_left() 仍为 True",
                g.failed_rounds_left()))

    g2 = LoopGuard(max_failed_rounds=3)
    for _ in range(3):
        g2.charge_failed_round(False)
    out.append((f"连续 3 个**失败轮**扣满（failed={g2.failed_rounds}）",
                g2.failed_rounds == 3))
    out.append((f"失败轮用尽⇒ failed_rounds_left()=False（stop={g2.stop_reason!r}）",
                not g2.failed_rounds_left()))
    out.append(("stop_reason 标记为 failed_rounds（可回显定位）",
                g2.stop_reason == "failed_rounds"))

    # ★ 成对对比：同样的"轮次数"，成功版不受限
    ok_run, bad_run = 0, 0
    gk = LoopGuard(max_failed_rounds=3)
    for _ in range(10):
        if not gk.failed_rounds_left():
            break
        gk.charge_failed_round(True)
        ok_run += 1
    gb = LoopGuard(max_failed_rounds=3)
    for _ in range(10):
        if not gb.failed_rounds_left():
            break
        gb.charge_failed_round(False)
        bad_run += 1
    out.append((f"★ 10 轮里成功版跑满 {ok_run} 轮 / 失败版只跑 {bad_run} 轮"
                f"（旧语义下两者都会被 6 截断）", ok_run == 10 and bad_run == 3))
    return out


def config_checks() -> list[tuple[str, bool]]:
    """两侧默认值一致 + 都读失败预算配置（对齐 verify_tool_round_budget.py 的纪律）。"""
    out = []
    paths = {
        "execute": os.path.join(_ROOT, "agent", "pipeline_parts", "execute.py"),
        "stream": os.path.join(_ROOT, "agent", "pipeline_parts", "stream.py"),
    }
    defaults = {}
    for tag, p in paths.items():
        with open(p, encoding="utf-8") as f:
            src = f.read()
        m = re.search(r'_cfg\.get\("agent",\s*"max_tool_rounds",\s*(\d+)\)', src)
        defaults[tag] = int(m.group(1)) if m else None
        out.append((f"{tag}: max_tool_rounds 默认值可解析（实测 {defaults[tag]}）",
                    defaults[tag] is not None))
        out.append((f"{tag}: 读 agent.max_failed_rounds 配置",
                    'max_failed_rounds' in src and
                    '_cfg.get("agent", "max_failed_rounds"' in src))
        out.append((f"{tag}: 循环里调用 failed_rounds_left()", "failed_rounds_left()" in src))
        out.append((f"{tag}: 每轮调用 charge_failed_round(", "charge_failed_round(" in src))
    out.append((f"两侧 max_tool_rounds 默认值一致（{defaults['execute']} == {defaults['stream']}）",
                defaults["execute"] == defaults["stream"]))
    # 硬上限必须仍存在（防死循环）——不能为了"不限制"把上限删掉
    out.append(("硬上限仍保留（default ≥ 10，防死循环）",
                (defaults["execute"] or 0) >= 10))
    return out


def runtime_check() -> list[tuple[str, bool]]:
    """真实链路：mock 连续成功 8 轮 ⇒ 旧实现会在第 6 轮截断。"""
    import json
    import llm
    from agent.pipeline import AgentPipeline
    from database import get_db

    seen = []

    def fake_chat(messages, provider_id=None, stream=False, tools=None,
                  thinking=False, **kw):
        n = len(seen)
        seen.append(n)
        msg = {"role": "assistant", "content": ""}
        if n < 8:
            # 持续要工具，且都是**成功**结果（ok: true）⇒ 每轮都算成功推进
            msg["tool_calls"] = [{
                "id": f"c{n+1}", "type": "function",
                # ⚠️ 必须用**白名单允许且真能出内容**的工具：
                #   首版用 `graph_retrieve` ⇒ 实测被最小权限拒绝
                #   （ok=0「不在本次子任务授予的工具白名单内」）⇒ 每轮都算失败
                #   ⇒ 3 轮就把失败预算烧光 ⇒ 看起来"只跑了 5 轮"，
                #   **看似实现有问题，其实是 mock 选错了工具**。
                #   `sysml_stdlib_meta` 实测在该场景可用，且参数名已修
                #   （查不同包⇒ 不同参数，避免被 probe_dup 短路）。
                "function": {"name": "sysml_stdlib_meta",
                             "arguments": json.dumps({"package": f"Pkg{n}"})}}]
            return {"choices": [{"message": msg, "finish_reason": "tool_calls"}],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 5},
                    "_meta": {"provider": "fake", "model": "fake",
                              "used_mock": False, "latency_ms": 1}}
        msg["content"] = "完成"
        return {"choices": [{"message": msg, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 5},
                "_meta": {"provider": "fake", "model": "fake",
                          "used_mock": False, "latency_ms": 1}}

    orig = llm.llm_client.chat
    llm.llm_client.chat = fake_chat
    conn = get_db()
    from datetime import datetime
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur = conn.execute(
        "INSERT INTO conversations (title,intent,status,user_id,phase,created_at,"
        "updated_at,project_id,current_intent,last_slots,pending_clarify) VALUES "
        "(:t,:i,'active',NULL,'requirement',:ts,:ts,'','view_expansion','{}','')",
        {"t": "verify_failed_budget", "i": "view_expansion", "ts": ts})
    conn.commit()
    err = ""
    try:
        pipe = AgentPipeline()
        pipe._load_db_agents()
        pipe.execute("请检索标准库并生成视图",
                     conversation_id=cur.lastrowid, branch="dev",
                     forced_intent="view_expansion", provider_id=104)
    except Exception as exc:                # noqa: BLE001
        err = f"{type(exc).__name__}: {exc}"
    finally:
        llm.llm_client.chat = orig

    tool_rounds = len(seen) - 1        # 末次是收尾，不算工具轮
    return [
        (f"pipeline 无异常（err={err[:70] or '无'}）", not err),
        (f"★ 连续成功推进跑了 {tool_rounds} 轮工具"
          f"（旧语义下会在第 6 轮截断 ⇒ 只跑 6）", tool_rounds >= 8),
    ]


def main() -> int:
    print("=" * 72)
    print("判据 · 轮次上限只约束失败重试，不限制正常调用")
    print("=" * 72)
    ok = True

    print("\n[①] 失败轮预算单元")
    for name, passed in unit_checks():
        print(f"[{'OK  ' if passed else 'FAIL'}] {name}")
        ok = ok and passed

    print("\n[②] 两侧配置与接线")
    for name, passed in config_checks():
        print(f"[{'OK  ' if passed else 'FAIL'}] {name}")
        ok = ok and passed

    print("\n[③] 真实链路：连续成功轮不该被截断")
    for name, passed in runtime_check():
        print(f"[{'OK  ' if passed else 'FAIL'}] {name}")
        ok = ok and passed

    print("\n" + "=" * 72)
    print(f"判据结论：{'全部通过' if ok else '存在未通过项'}")
    print("=" * 72)

    if ok:
        print("\n[变异自证] 把 charge_failed_round 改成恒不扣（= 旧语义），应变红")
        p = os.path.join(_ROOT, "agent", "loop_guard.py")
        with open(p, encoding="utf-8") as f:
            src = f.read()
        anchor = "        if ok:\n            return\n        self.failed_rounds += 1"
        if anchor not in src:
            print("[FAIL] 变异锚点未找到 —— 判据可能已失效，请人工检查")
            return 1
        mutated = src.replace(
            anchor,
            "        if True:\n            return   # 变异：恒不扣（= 旧语义）\n"
            "        self.failed_rounds += 1", 1)
        with open(p, "w", encoding="utf-8") as f:
            f.write(mutated)
        try:
            import importlib
            import agent.loop_guard as lg
            importlib.reload(lg)
            # ★ 必须**重跑整组单元判据**，而不是只看某个表达式。
            #   首版我只查 `failed_rounds_left()` 返回值 ⇒ 变异下它仍是 True
            #   （failed=0 当然"还有额度"）⇒ 报告「未变红」，
            #   **但判据其实是能抓住的** —— 「连续 3 个失败轮扣满」那条已判红。
            #   ⇒ 教训同前：**变异自证要问"整组判据会不会红"，不是"我抽查的那条会不会红"**。
            unit_after = unit_checks()
            n_red = sum(1 for _, p_ in unit_after if not p_)
            print(f"[变异自证] 变异后单元判据 {n_red}/{len(unit_after)} 条判红")
            for nm, p_ in unit_after:
                if not p_:
                    print(f"           ★ 变红：{nm}")
            red = n_red > 0
            print(f"[变异自证] {'已变红，真判据' if red else '**未变红 ⇒ 空转断言**'}")
            ok = ok and red
        finally:
            with open(p, "w", encoding="utf-8") as f:
                f.write(src)
            import importlib
            import agent.loop_guard as lg
            importlib.reload(lg)
        with open(p, encoding="utf-8") as f:
            assert f.read() == src, "回滚失败！"
        print("[OK  ] 回滚校验：生产代码已逐字还原")
        # 独立复核：回滚后判据必须恢复全绿
        green_after = unit_checks()
        n_green = sum(1 for _, p_ in green_after if p_)
        print(f"[OK  ] 回滚后 {n_green}/{len(green_after)} 条判据恢复通过")
        ok = ok and (n_green == len(green_after))
        print("=" * 72)
        print(f"最终结论：{'全部通过' if ok else '未通过'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())