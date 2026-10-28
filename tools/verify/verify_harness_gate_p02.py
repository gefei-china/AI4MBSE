# -*- coding: utf-8 -*-
"""门禁：P0-2 harness 级强制闸（内核判定 + 接线）。

方案：docs/持续自主执行能力-最终方案设计-20261028.md §4.3.2 / §6 第 2 步。

对齐官方 Stop hook 三条不可省的设计，本门禁逐条验证：
  ① 判据是 **verify 的实际执行结果**，不是 LLM 判断 ⇒ 确定性
  ② **必须有上限**（默认 8，对齐官方）⇒ 防 FM-1.5「该停不停」12.4%
  ③ 强制放行时**必须如实告知「未完成」** ⇒ 不能伪装成完成

用法：
  python tools/verify/verify_harness_gate_p02.py
退出码 0 = 全绿；1 = 有红项。
"""
from __future__ import annotations

import os
import sys
import importlib.util

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

RESULTS = []


def check(group, name, cond, detail=""):
    RESULTS.append((group, name, bool(cond), detail))


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_ROOT, rel))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hg = _load("harness_gate", "agent/harness_gate.py")


# ══════════════ A. 内核：harness_decide ══════════════

def group_a():
    gate_pending = {"done": False, "fallback": False,
                    "remaining": ["校验包 P-001", "跑通导入"], "reason": "2/2 未完成"}
    gate_done = {"done": True, "fallback": False, "remaining": []}
    gate_none = {"done": False, "fallback": True, "remaining": []}

    # A1 开关关 ⇒ 永不拦
    d = hg.harness_decide(gate_pending, 0, 8, harness_enabled=False)
    check("A", "开关关闭 ⇒ 不拦", d["block"] is False, str(d))

    # A2 上限 0 ⇒ 不拦
    d = hg.harness_decide(gate_pending, 0, 0, harness_enabled=True)
    check("A", "上限为 0 ⇒ 不拦", d["block"] is False, str(d))

    # A3 无待办（fallback）⇒ 不拦
    d = hg.harness_decide(gate_none, 0, 8, harness_enabled=True)
    check("A", "无待办 ⇒ 不拦（逐字节兼容 D-3）", d["block"] is False, str(d))

    # A4 全部完成 ⇒ 放行
    d = hg.harness_decide(gate_done, 0, 8, harness_enabled=True)
    check("A", "全部通过验收 ⇒ 放行", d["block"] is False, str(d))

    # A5 ★ 有未完成 ⇒ 拦
    d = hg.harness_decide(gate_pending, 0, 8, harness_enabled=True)
    check("A", "★有未通过验收 ⇒ 拦", d["block"] is True, str(d))
    check("A", "拦的理由含剩余项数", "2" in d["reason"], d["reason"])

    # A6 ★ 上限：连续阻断达上限 ⇒ 强制放行
    d = hg.harness_decide(gate_pending, 7, 8, harness_enabled=True)
    check("A", "★第 8 次（blocks_used=7）⇒ 仍拦", d["block"] is True, str(d))
    d = hg.harness_decide(gate_pending, 8, 8, harness_enabled=True)
    check("A", "★第 9 次（blocks_used=8）⇒ 强制放行", d["block"] is False, str(d))
    check("A", "★强制放行标记为 force_release", d["force_release"] is True, str(d))

    # A7 超出上限也强制放行
    d = hg.harness_decide(gate_pending, 20, 8, harness_enabled=True)
    check("A", "远超上限 ⇒ 仍强制放行（不会停不下来）", d["block"] is False, str(d))

    # A8 默认上限 = 8（对齐官方）
    check("A", "★默认上限 = 8（对齐官方）", hg.HARNESS_MAX_BLOCKS == 8,
          str(hg.HARNESS_MAX_BLOCKS))

    # A9 ★ 无进展检测（真机实测驱动）：连续 N 次 remaining 无变化 ⇒ 提前放行
    d = hg.harness_decide(gate_pending, 1, 8, harness_enabled=True,
                          stall_streak=2, max_stall=2)
    check("A", "★无进展达阈值 ⇒ 提前强制放行（不等次数上限）",
          d["block"] is False and d["force_release"] is True, str(d))
    check("A", "★无进展放行标 stalled=True", d.get("stalled") is True, str(d))
    check("A", "★无进展放行理由说明「待办无任何变化」",
          "无任何变化" in d["reason"] or "卡死" in d["reason"], d["reason"])
    # 未达阈值仍拦
    d = hg.harness_decide(gate_pending, 1, 8, harness_enabled=True,
                          stall_streak=1, max_stall=2)
    check("A", "无进展 1 次（阈值 2）⇒ 仍拦", d["block"] is True, str(d))
    # 无进展检测优先于次数上限（不该等 8 次）
    d = hg.harness_decide(gate_pending, 2, 8, harness_enabled=True,
                          stall_streak=2, max_stall=2)
    check("A", "★无进展检测优先于次数上限（第 3 次就放行，不等 8 次）",
          d["block"] is False and d["force_release"] is True, str(d))


# ══════════════ B. 回灌内容 ══════════════

def group_b():
    fb = hg.build_block_feedback(["校验包 P-001", "跑通导入"], 1, 8)
    check("B", "回灌含剩余清单", "校验包 P-001" in fb and "跑通导入" in fb, fb[:120])
    check("B", "回灌含阻断次数", "1/8" in fb, fb[:120])
    check("B", "回灌明确要求别只给建议", "不要只给建议" in fb, fb[:160])

    # B2 失败明细进回灌
    fb2 = hg.build_block_feedback(["校验包 P-001"], 2, 8,
                                  ["校验包 P-001 —— 验收工具「sysmlv2_check」未通过：2 errors"])
    check("B", "★失败明细进回灌", "2 errors" in fb2, fb2[:200])

    # B3 ★ 强制放行必须含「未完成」
    notice = hg.build_force_release_notice(["校验包 P-001"], 8, 8)
    check("B", "★★强制放行告知含「未完成」",
          hg.HARNESS_FORCE_NOTICE_MARK in notice, notice[:160])
    check("B", "★强制放行告知列出未完成项", "校验包 P-001" in notice, notice[:160])

    # B4 强制放行告知**不含**「已完成」（不能伪装成完成）
    check("B", "★强制放行告知不含「已完成」", "已完成" not in notice, notice[:200])


# ══════════════ C. verify 规格解析 ══════════════

def group_c():
    s = hg.parse_verify_spec("人工确认")
    check("C", "『人工确认』⇒ manual", s["kind"] == "manual", str(s))

    s = hg.parse_verify_spec("sysmlv2_check")
    check("C", "纯工具名 ⇒ tool", s["kind"] == "tool" and s["tool"] == "sysmlv2_check", str(s))

    s = hg.parse_verify_spec('sysmlv2_check {"pkg": "P-001"}')
    check("C", "工具名+JSON 参数 ⇒ 参数解析正确",
          s["kind"] == "tool" and s["args"].get("pkg") == "P-001", str(s))

    s = hg.parse_verify_spec("some_tool 裸参数")
    check("C", "非 JSON 参数 ⇒ 按 value 传",
          s["kind"] == "tool" and s["args"].get("value") == "裸参数", str(s))

    s = hg.parse_verify_spec("")
    check("C", "空 verify ⇒ manual（不抛异常）", s["kind"] == "manual", str(s))

    # ★★ 真机实测缺陷回归（2026-10-28 全链路 A/B 抓出）：
    #    verify 字段由 LLM 自由书写，它写自然语言。
    #    旧实现"按空白切分第一段当工具名" ⇒ 把中文句子当工具名去调用，
    #    实际产生了这些假工具调用（tool_call_logs 实录）：
    #      「定位分支与版本（含草稿）后调用」 / 「人工确认：总结仅引用…」
    #    ⇒ 这些用例必须全部降级为 manual，**绝不拿自然语言去调工具**。
    NATURAL = [
        "定位分支与版本（含草稿）后调用 query_data_tree",
        "调用 query_projects 返回至少一个工程",
        "人工确认：总结仅引用本次实际查到的工程与包树内容，字数≤200",
        "第 5 章也要看",
        "跑一下校验",
    ]
    bad = []
    for v in NATURAL:
        sp_ = hg.parse_verify_spec(v)
        if sp_["kind"] != "manual":
            bad.append((v[:24], sp_["tool"]))
    check("C", "★★自然语言 verify 一律降级 manual（不当工具名调用）",
          not bad, f"被误当工具名：{bad}")

    # 反向：真工具名仍必须是 tool（防矫枉过正把真工具也降级）
    REAL_TOOLS = ["query_data_tree", "sysmlv2_check", "gen_pkg",
                  "mcp:zhiyuan:query_projects",
                  'query_data_tree {"branch": "dev"}']
    bad2 = []
    for v in REAL_TOOLS:
        sp_ = hg.parse_verify_spec(v)
        if sp_["kind"] != "tool":
            bad2.append(v)
    check("C", "★真工具名仍识别为 tool（未矫枉过正）", not bad2, str(bad2))


# ══════════════ D. 接线（两处同构 + 位置正确）══════════════

def group_d():
    sp = os.path.join(_ROOT, "agent", "pipeline_parts", "stream.py")
    ex = os.path.join(_ROOT, "agent", "pipeline_parts", "execute.py")
    ap = os.path.join(_ROOT, "agent", "pipeline_parts", "autopilot.py")
    s = open(sp, encoding="utf-8").read()
    e = open(ex, encoding="utf-8").read()
    a = open(ap, encoding="utf-8").read()

    # D1 两处都调 _harness_gate
    check("D", "stream.py 调 _harness_gate", s.count("self._harness_gate(") >= 1)
    check("D", "execute.py 调 _harness_gate", e.count("self._harness_gate(") >= 1)

    # D2 ★ 位置（AST 精确，不用 find —— find 会命中更早的同名注释）：
    #    闸调用必须在**同一个 while 节点内**、且在该 while 的 break **之前**。
    #    ⚠️ 第一版判据写成「break 之后」是错的 —— break 之后已跳出 while，
    #       `continue` 会直接语法报错（py_compile 已实测到）。
    import ast
    for name, path in (("stream.py", sp), ("execute.py", ex)):
        tree = ast.parse(open(path, encoding="utf-8").read())
        found = False
        detail = ""
        for node in ast.walk(tree):
            if not isinstance(node, ast.While):
                continue
            # 该 while 体内的 break（不含内层 while 的）
            breaks = []
            gates = []
            def scan(n):
                for ch in ast.iter_child_nodes(n):
                    if isinstance(ch, ast.While):
                        continue          # 内层循环的不算
                    if isinstance(ch, ast.Break):
                        breaks.append(ch.lineno)
                    if isinstance(ch, ast.Call):
                        fn = ch.func
                        if isinstance(fn, ast.Attribute) and \
                                fn.attr == "_harness_gate":
                            gates.append(ch.lineno)
                    scan(ch)
            scan(node)
            if not gates:
                continue
            g = min(gates)
            if not breaks:
                found = False
                detail = f"while@{node.lineno} 无 break"
                break
            b = max(x for x in breaks if x > g) if any(x > g for x in breaks) else None
            if b is not None:
                found = True
                detail = f"while@{node.lineno} gate@{g} < break@{b}"
                break
            else:
                found = False
                detail = f"while@{node.lineno} gate@{g} 之后无 break（位置错误）"
                break
        check("D", f"{name} ★闸在同 while 内的 break 之前（能拦住不让结束）",
              found, detail)

    # D2b 闸必须在**它自己那条路径**的落库之前
    #    ⚠️ stream.py 有两处「写入会话」（3084 收口轮 / 3309 主轮）——
    #       取闸**之后最近**的那处，否则会拿另一条路径的落库来比 ⇒ 假红。
    for name, path in (("stream.py", sp), ("execute.py", ex)):
        src2 = open(path, encoding="utf-8").read()
        tree = ast.parse(src2)
        gate_line = None
        for node in ast.walk(tree):
            for n in ast.walk(node):
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                        and n.func.attr == "_harness_gate":
                    gate_line = n.lineno
                    break
            if gate_line:
                break
        persist_line = None
        for i, l in enumerate(src2.split("\n")):
            if i + 1 <= (gate_line or 0):
                continue
            if '"写入会话"' in l or "落 user 消息" in l:
                persist_line = i + 1
                break
        ok = (gate_line and persist_line and gate_line < persist_line)
        check("D", f"{name} 闸在落库之前", ok,
              f"gate@{gate_line} persist@{persist_line}")

    # D3 计数在循环外（跨轮累积）——
    #    ⚠️ 判据只认**初始化形式** `_harness_blocks = 0`：
    #       循环体内还有 `_harness_blocks = int(...)`（更新计数，不是重置），
    #       AST 若按"任何赋值"判定会把更新误认成重置 ⇒ 假红。
    import ast
    for name, path in (("stream.py", sp), ("execute.py", ex)):
        tree = ast.parse(open(path, encoding="utf-8").read())
        reset_in_loop = False
        init_outside = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.While):
                continue
            # 循环体内是否有 `_harness_blocks = 0`（真重置）
            for n in ast.walk(node):
                if isinstance(n, ast.Assign) and \
                        any(isinstance(t, ast.Name) and t.id == "_harness_blocks"
                            and isinstance(n.value, ast.Constant) and n.value.value == 0
                            for t in n.targets):
                    reset_in_loop = True
        # 循环外是否有初始化
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for n in ast.walk(node):
                    if isinstance(n, ast.Assign) and \
                            any(isinstance(t, ast.Name) and t.id == "_harness_blocks"
                                and isinstance(n.value, ast.Constant)
                                and n.value.value == 0 for t in n.targets):
                        init_outside = True
        check("D", f"{name} 计数初始化在循环外（跨轮累积，不每轮清零）",
              init_outside and not reset_in_loop,
              f"init_outside={init_outside} reset_in_loop={reset_in_loop}")

    # D4 语义唯一一份（IO 在 autopilot，两处不各写一份）
    check("D", "★IO 语义只在 autopilot._harness_gate",
          a.count("def _harness_gate") == 1)
    for name, src in (("stream.py", s), ("execute.py", e)):
        check("D", f"{name} 未复制 _harness_gate 实现",
              "def _harness_gate" not in src)

    # D5 强制放行必须把告知并入 llm_content（不能丢）
    for name, src in (("stream.py", s), ("execute.py", e)):
        check("D", f"{name} 强制放行把告知并入产出",
              "_hgate.get(\"notice\")" in src)

    # D6 拦住时必须 continue（回外层循环继续 ReAct）
    for name, src in (("stream.py", s), ("execute.py", e)):
        seg = src[src.find("if _hgate.get(\"block\"):"):]
        seg = seg[:seg.find("if _hgate.get(\"force_release\"):") + 2000] \
            if "if _hgate.get(\"force_release\"):" in seg else seg[:2000]
        check("D", f"{name} 拦住时 continue 回外层循环", "continue" in seg)

    # D7 闸总开关默认关
    check("D", "★harness_enabled 默认关",
          '_cfg.as_bool("agent", "harness_enabled", False)' in a)
    check("D", "harness_max_blocks 默认 8",
          '_cfg.get("agent", "harness_max_blocks", 8)' in a)

    # D8 ★★ 不变式：notice 非空 ⟺ force_release=True
    #    真机 E2E 抓到过这个 bug：notice 里写了「达到上限 8」，
    #    但返回的 force_release 被写死成 False ⇒ 上层拿不到"这是强制放行"的信号
    #    ⇒ 可能被当成普通完成。
    #    ⇒ 固化为判据：任何 `"force_release": False` 与 notice 赋值在同一返回语句里即判红。
    import re as _re
    bad = []
    for m in _re.finditer(r'return\s*\{(.*?)\}\s*$', a, _re.S | _re.M):
        blk = m.group(1)
        if '"force_release": False' in blk and "build_force_release_notice" in blk:
            bad.append(blk[:80])
    # 更严格：检查所有含 notice 变量的返回块，force_release 不得写死 False
    for m in _re.finditer(
            r'return\s*\{"block":\s*False.*?\}', a, _re.S):
        blk = m.group(0)
        if "notice" in blk and '"force_release": False' in blk:
            bad.append(blk[:100])
    check("D", "★★不变式：放行时 force_release 不得写死 False（notice 与之联动）",
          not bad, f"命中 {len(bad)} 处：{bad[:2]}")
    check("D", "force_release 从 decision 透传（bool(...get(...))）",
          "bool(decision.get(\"force_release\"))" in a
          and "bool(decision2.get(\"force_release\"))" in a)


# ══════════════ E. 变异自证 ══════════════

def group_e():
    """接线判据必须有鉴别力。"""
    gate_pending = {"done": False, "fallback": False,
                    "remaining": ["x"], "reason": "未完成"}

    # E1 变异：去掉上限（永不强制放行）⇒ 上限判据必须翻红
    def c_max(decide) -> bool:
        d = decide(gate_pending, 8, 8, harness_enabled=True)
        return (not d["block"]) and d["force_release"]

    def bad_no_limit(gate, blocks_used, max_blocks=8, *, harness_enabled=True):
        return {"block": True, "reason": "变异", "force_release": False,
                "remaining": [], "blocks_used": blocks_used, "max_blocks": max_blocks}

    check("E", "★变异自证：去掉上限 ⇒ 上限判据翻红",
          c_max(hg.harness_decide) and not c_max(bad_no_limit),
          f"真实现={c_max(hg.harness_decide)} 变异={c_max(bad_no_limit)}")

    # E2 变异：永不拦（恒放行）⇒ 核心拦截判据必须翻红
    def c_block(decide) -> bool:
        d = decide(gate_pending, 0, 8, harness_enabled=True)
        return d["block"]

    def bad_never_block(gate, blocks_used, max_blocks=8, *, harness_enabled=True):
        return {"block": False, "reason": "变异", "force_release": False,
                "remaining": [], "blocks_used": blocks_used, "max_blocks": max_blocks}

    check("E", "★变异自证：永不拦 ⇒ 拦截判据翻红",
          c_block(hg.harness_decide) and not c_block(bad_never_block),
          f"真实现={c_block(hg.harness_decide)} 变异={c_block(bad_never_block)}")

    # E3 变异：强制放行时不告知 ⇒ 「如实告知」判据必须翻红
    def c_notice(buil) -> bool:
        n = buil(["x"], 8, 8)
        return hg.HARNESS_FORCE_NOTICE_MARK in n and "已完成" not in n

    def bad_notice(remaining, blocks_used, max_blocks):
        return "已全部完成，任务结束。"

    check("E", "★变异自证：放行不告知 ⇒ 如实告知判据翻红",
          c_notice(hg.build_force_release_notice) and
          not c_notice(bad_notice),
          f"真实现={c_notice(hg.build_force_release_notice)} 变异={c_notice(bad_notice)}")

    # E4 反向自证：真实现本身必须通过
    check("E", "反向自证：真实现拒绝无回执置完成（防判据恒红）",
          c_block(hg.harness_decide) and c_max(hg.harness_decide)
          and c_notice(hg.build_force_release_notice))

    # E5 ★ 变异自证：去掉无进展检测（只靠次数上限）⇒ A9 判据翻红
    #    这正是真机暴露的问题：只靠 8 次上限会连续拦截 8 次且待办毫无变化，耗时 ≈2.3 分钟。
    gate_pending2 = {"done": False, "fallback": False,
                     "remaining": ["x"], "reason": "未完成"}

    def c_stall(decide) -> bool:
        d = decide(gate_pending2, 2, 8, harness_enabled=True,
                   stall_streak=2, max_stall=2)
        return (d["block"] is False) and bool(d.get("force_release"))

    def bad_no_stall(gate, blocks_used, max_blocks=8, *, harness_enabled=True,
                     stall_streak=0, max_stall=2):
        # 变异：完全忽略 stall_streak（只按次数上限判）
        if not harness_enabled or max_blocks == 0:
            return {"block": False, "reason": "x", "force_release": False,
                    "remaining": [], "blocks_used": blocks_used,
                    "max_blocks": max_blocks, "stalled": False}
        if gate.get("fallback") or gate.get("done"):
            return {"block": False, "reason": "x", "force_release": False,
                    "remaining": [], "blocks_used": blocks_used,
                    "max_blocks": max_blocks, "stalled": False}
        if blocks_used >= max_blocks:
            return {"block": False, "reason": "上限", "force_release": True,
                    "remaining": [], "blocks_used": blocks_used,
                    "max_blocks": max_blocks, "stalled": False}
        return {"block": True, "reason": "拦", "force_release": False,
                "remaining": [], "blocks_used": blocks_used,
                "max_blocks": max_blocks, "stalled": False}

    check("E", "★变异自证：去掉无进展检测 ⇒ A9 判据翻红",
          c_stall(hg.harness_decide) and not c_stall(bad_no_stall),
          f"真实现={c_stall(hg.harness_decide)} 变异={c_stall(bad_no_stall)}")

    # E6 ★★ 变异自证：去掉工具名形态校验（复原旧 bug）⇒ C 组新判据必须翻红
    def c_natural(parse) -> bool:
        return parse("定位分支与版本（含草稿）后调用 query_data_tree")["kind"] == "manual"

    def bad_parse(verify):
        raw = str(verify or "").strip()
        if raw in ("人工确认", "manual", "人工"):
            return {"kind": "manual", "tool": "", "args": {}, "raw": raw, "parse_error": ""}
        parts = raw.split(None, 1)
        # 变异：不校验工具名形态（旧行为）
        return {"kind": "tool", "tool": parts[0].strip(),
                "args": {}, "raw": raw, "parse_error": ""}

    check("E", "★★变异自证：去掉工具名形态校验 ⇒ 自然语言判据翻红",
          c_natural(hg.parse_verify_spec) and not c_natural(bad_parse),
          f"真实现={c_natural(hg.parse_verify_spec)} 变异={c_natural(bad_parse)}")


def main():
    for fn in (group_a, group_b, group_c, group_d, group_e):
        fn()
    print("=" * 74)
    print("门禁：P0-2 harness 级强制闸")
    print("=" * 74)
    groups = {}
    for g, n, ok, d in RESULTS:
        groups.setdefault(g, []).append((n, ok, d))
    total = failed = 0
    for g in sorted(groups):
        print(f"\n【{g} 组】")
        for n, ok, d in groups[g]:
            total += 1
            if not ok:
                failed += 1
            line = f"  [{'PASS' if ok else 'FAIL'}] {n}"
            if d and not ok:
                line += f"   ← {d}"
            print(line)
    print("\n" + "-" * 74)
    print(f"合计 {total} 项，通过 {total - failed}，失败 {failed}")
    print("-" * 74)
    if failed:
        print("❌ 门禁未通过")
        return 1
    print("✅ 全绿")
    return 0


if __name__ == "__main__":
    sys.exit(main())
