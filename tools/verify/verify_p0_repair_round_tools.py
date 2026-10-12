"""verify_p0_repair_round_tools — 判据：校验报block 后**收尾轮要留修复工具**。

## 缺陷（2026-10-09 实测八视图）
收尾轮（`should_finalize` 命中后）一律 `tools=None`。
实测 v6 时序图：`sysml_v2_validate` 报**78 个语法错**之后，收尾轮
`completion_tokens=28` —— 写不出 15k 字符的修复代码；
`sysml_v2_autofix` 实测**一次都没被调用过**。
⇒ 「校验报错了却没人修」。

## 判据
① `repair_tools_needed` 单元（**默认 False = 零行为漂移**）：
   ·最近 tool 结果含 `判据：block` → True
   · `pass` / `report` → False（保持改动前）
   · 无 tool 结果 / 碰到非校验类 tool → False
② `_repair_tool_subset` 只留修复类工具，且**一个都没有时返回 None**
③ **真实链路**：mock provider 报 block，确认收尾那次调用**真的带了工具**
   （基线：收尾那次 `tools=None`）
④ **反空转**：必须在 `tools_def` 为 None 时返回 None（不是 `[]`）

## 变异自证（必跑）
把 `repair_tools_needed` 改成恒返回 False（等价于改回改动前），
断言判据 **变红**；改回后再断言变绿。

## 用法
    ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_p0_repair_round_tools.py
"""
from __future__ import annotations

import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

BLOCK = ("【SysML v2 校验】判据：block —— 存在硬错，必须修复后重新校验\n"
         "计数：词法 0 / 语法 78 / 语义 0｜ERROR 共 78 条")

#: 真**有语法错**的代码片段 —— 喂给真实 `sysml_v2_validate` 会回 `判据：block`
#: （实测 rc=1 / 语法 4）。首版误喂合法代码 `package P {}` ⇒ 工具回 pass
#: ⇒ 全程 messages 里没有 block ⇒ repair 判定恒 False ⇒ 判据③测的是普通工具轮。
BAD_CODE = 'package P { part \'a\'; async message m from x to y; }' 
PASS = "【SysML v2 校验】判据：pass —— 通过 —— 无词法/语法错\n计数：词法 0 / 语法 0 / 语义 0"
REPORT = ("【SysML v2 校验】判据：report —— 有语义错但无硬错（不阻断）\n"
          "计数：词法 0 / 语法 0 / 语义 25｜ERROR 共 25 条")


def unit_checks() -> list[tuple[str, bool]]:
    from agent import loop_protocol as lp
    f = lp.repair_tools_needed
    out = []
    cases = [
        (True, [{"role": "tool", "content": BLOCK}], "最近是block ⇒ 应给修复工具"),
        (False, [{"role": "tool", "content": PASS}], "pass ⇒ 不给（保持原行为）"),
        (False, [{"role": "tool", "content": REPORT}], "report（仅语义错）⇒ 不给"),
        (False, [], "无消息 ⇒ 不给"),
        (False, [{"role": "assistant", "content": "hi"}], "无 tool 结果 ⇒ 不给"),
        # ★ 跨条不许猜：最后一条是别的工具结果 ⇒ 即使更早有过 block 也不给
        (False, [{"role": "tool", "content": BLOCK},
                 {"role": "tool", "content": "graph_retrieve 的结果"}],
         "最后一条不是校验结果 ⇒ 不给（不跨条猜）"),
        (True, [{"role": "tool", "content": BLOCK}, {"role": "assistant", "content": "x"},
                {"role": "tool", "content": "判据：block 第二段"}], "最近一条仍是 block ⇒ 给"),
    ]
    for want, msgs, name in cases:
        got = bool(f(msgs))
        out.append((f"{name}（期望 {want}）", got == want))
    return out


def subset_checks() -> list[tuple[str, bool]]:
    from agent.pipeline_parts.execute import (_repair_tool_subset,
                                              _REPAIR_TOOL_NAMES)
    def td(*names):
        return [{"type": "function",
                 "function": {"name": n, "description": "", "parameters": {}}}
                for n in names]
    out = []
    sub = _repair_tool_subset(td("graph_retrieve", "sysml_v2_validate",
                                "sysml_stdlib_meta", "sysml_v2_autofix"))
    got = [t["function"]["name"] for t in (sub or [])]
    out.append(("只留修复类工具（不含 graph_retrieve / stdlib_meta）",
                got == ["sysml_v2_validate", "sysml_v2_autofix"]))
    # ★ 反空转：没有修复工具时必须返回 None 而不是 [] ——
    #   [] 会让 chat() 走"有工具但清单空"的路径，行为与 None 不同。
    out.append(("无修复工具时返回 None（不是 []）",
                _repair_tool_subset(td("graph_retrieve")) is None))
    out.append(("tools_def=None 时返回 None",
                _repair_tool_subset(None) is None))
    out.append((f"白名单含 autofix（{_REPAIR_TOOL_NAMES}）",
                "sysml_v2_autofix" in _REPAIR_TOOL_NAMES))
    return out



def _max_rounds() -> int:
    """从 execute.py 里取真实的 max_tool_rounds 默认值。

    ⚠️ 2026-10-09 踩过：判据里硬编码 `> 6` / `n <= 8`，
      而生产侧默认值已改成 12（成功轮不扣预算，硬上限放宽）
      ⇒ mock 的 8 次调用**跑不满循环** ⇒ 收尾轮从不执行
      ⇒ 判据③ 误判"收尾带查资料类"。
    ⇒ 判据**必须自己推导上限**，不能抄一个会漂的数字。
    """
    import re as _re
    src = open(os.path.join(_ROOT, "agent", "pipeline_parts", "execute.py"),
               encoding="utf-8").read()
    m = _re.search(r'_cfg\.get\("agent",\s*"max_tool_rounds",\s*(\d+)\)', src)
    return int(m.group(1)) if m else 12


def runtime_check() -> list[tuple[str, bool]]:
    """真实链路：mock provider 让validate 报 block，看收尾那次调用有没有带工具。"""
    import llm
    from agent.pipeline import AgentPipeline
    from database import get_db

    seen: list = []
    MR = _max_rounds()
    # 必须**超过硬上限**才会逼出收尾轮
    _PROBE_ROUNDS = MR + 2

    def fake_chat(messages, provider_id=None, stream=False, tools=None,
                  thinking=False, **kw):
        n = len(seen)
        seen.append({"tools": [t["function"]["name"] for t in (tools or [])],
                     "has_block": any("判据：block" in str(m.get("content") or "")
                                      for m in messages if isinstance(m, dict))})
        msg = {"role": "assistant", "content": ""}
        # ★ 关键（实测踩过两次）：
        #   ① 必须喂**真的有语法错**的代码 —— 合法代码（如 `package P {}`）
        #      会让真实 validate 工具回「判据：pass」⇒ messages 里永远没有 block
        #      ⇒ repair 判定恒 False。
        #   ② 必须**连续给 tool_calls 直到超过 max_tool_rounds(6)**，
        #      否则循环在第 2 轮 `break`，`should_finalize` 里的收尾轮
        #      **根本不执行** ⇒ 测到的最后一次调用是「循环内轮次」（带全量工具），
        #      判据就会去断言"收尾带查资料类"—— **测错了对象**。
        #   ⇒ 这里对 n < 8 一律返回 tool_calls，把 6 轮走满再逼出收尾轮。
        if n < _PROBE_ROUNDS:
            msg["tool_calls"] = [{
                "id": f"c{n + 1}", "type": "function",
                # ★ 参数必须**逐轮不同**（`{n}` 插值）：
                #   首版每轮传同一份 `BAD_CODE` ⇒ `LoopGuard.probe_dup` 第 4 次
                #   就短路（回 ok=True 但非真实执行）⇒ 实测循环在第 6 次调用就结束，
                #   **收尾轮被当成"循环内轮次"** ⇒ 判据③ 误判。
                #   这个坑与 `verify_failed_round_budget` 里那个同源：
                #   **mock 必须保证每轮都是"未见过"的调用**。
                "function": {"name": "sysml_v2_validate",
                             "arguments": json.dumps(
                                 {"code": BAD_CODE + f"\n// round {n}"})}}]
            return {"choices": [{"message": msg, "finish_reason": "tool_calls"}],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 5},
                    "_meta": {"provider": "fake", "model": "fake",
                              "used_mock": False, "latency_ms": 1}}
        msg["content"] = "已修复。"
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
        {"t": "verify_repair_round", "i": "view_expansion", "ts": ts})
    conn.commit()
    try:
        pipe = AgentPipeline()
        pipe._load_db_agents()
        pipe.execute("请生成 sequence 视图并调用 sysml_v2_validate 校验",
                     conversation_id=cur.lastrowid, branch="dev",
                     forced_intent="view_expansion", provider_id=104)
    except Exception as exc:                # noqa: BLE001
        print(f"    （pipeline 异常：{type(exc).__name__}: {str(exc)[:60]}）")
    finally:
        llm.llm_client.chat = orig

    out = []
    # ⚠️ 判据③ 必须区分「循环内轮次」与「收尾轮」：
    #   硬上限 = MR ⇒ 循环内最多 MR 轮（每次都带**全量**工具，属正常），
    #   收尾轮是**第 7 次**调用（`should_finalize` 命中后额外那一次）。
    #   首版我看 `seen[-1]` 就断言"收尾带查资料类"—— 实测那是**循环内轮次**，
    #   本来就该有全量工具 ⇒ **判据自己测错了对象**（空跑/假失败）。
    #   正解：拿**最后一次**调用与**循环内轮次**对比 ——
    #   收尾轮的工具数应当**更少**（只剩修复类）。
    out.append((f"假 provider 被调用 {len(seen)} 次（硬上限={MR}，"
                f"期望 > {MR}，多出来的那次才是收尾轮）", len(seen) > MR))
    tail = seen[-1] if seen else {"tools": []}
    loop_rounds = [x for x in seen[:-1]] or [{"tools": []}]
    loop_max = max(len(x["tools"]) for x in loop_rounds)
    out.append((f"★ 收尾轮存在且工具**少于**循环内轮次"
                f"（收尾 {len(tail['tools'])} 个 vs 循环内最多 {loop_max} 个；"
                f"改动前收尾恒为 0 个）",
                0 < len(tail["tools"]) < loop_max))
    out.append(("★ 收尾轮工具里**不含**查资料类"
                f"（实测 {tail['tools']}）",
                not ({"graph_retrieve", "sysml_stdlib_meta",
                      "sysml_ast_extract"} & set(tail["tools"]))))
    out.append(("收尾轮含 autofix（能被真正用来修）",
                "sysml_v2_autofix" in tail["tools"]))
    return out


def main() -> int:
    print("=" * 72)
    print("P0 判据 · 校验报 block 后收尾轮保留修复工具")
    print("=" * 72)
    ok = True
    print("\n[①] repair_tools_needed 单元（默认 False = 零行为漂移）")
    for name, passed in unit_checks():
        print(f"    [{'OK  ' if passed else 'FAIL'}] {name}")
        ok = ok and passed

    print("\n[②] _repair_tool_subset 白名单与反空转")
    for name, passed in subset_checks():
        print(f"    [{'OK  ' if passed else 'FAIL'}] {name}")
        ok = ok and passed

    print("\n[③] 真实链路：收尾那次调用是否真带工具")
    for name, passed in runtime_check():
        print(f"    [{'OK  ' if passed else 'FAIL'}] {name}")
        ok = ok and passed

    print("\n" + "=" * 72)
    print(f"判据结论：{'全部通过' if ok else '存在未通过项'}")
    print("=" * 72)
    if ok:
        print("\n[变异自证] 把 repair_tools_needed 改成恒 False（= 改动前），应变红")
        p = os.path.join(_ROOT, "agent", "loop_protocol.py")
        with open(p, encoding="utf-8") as f:
            src = f.read()
        anchor = 'if not messages:\n        return False\n    # 从后往前找最近一条 tool 结果'
        if anchor not in src:
            print("[FAIL] 变异锚点未找到 —— 判据可能已失效，请人工检查")
            return 1
        mutated = src.replace(
            anchor,
            'if True:\n        return False   # 变异：恒不给工具（= 改动前行为）\n'
            '    if not messages:\n        return False\n    # 从后往前找最近一条 tool 结果',
            1)
        with open(p, "w", encoding="utf-8") as f:
            f.write(mutated)
        try:
            import importlib
            import agent.loop_protocol as lpm
            importlib.reload(lpm)
            red = bool(lpm.repair_tools_needed([{"role": "tool", "content": BLOCK}]))
            print(f"[变异自证] repair_tools_needed(block) → {red}"
                  f"（期望 False ⇒ {'已变红，真判据' if not red else '仍是 True ⇒ 空转断言'}）")
            ok = ok and (not red)
        finally:
            with open(p, "w", encoding="utf-8") as f:
                f.write(src)
            import importlib
            import agent.loop_protocol as lpm
            importlib.reload(lpm)
        with open(p, encoding="utf-8") as f:
            assert f.read() == src, "回滚失败！"
        print("[OK  ] 回滚校验：生产代码已逐字还原")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())