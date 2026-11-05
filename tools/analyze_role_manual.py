# -*- coding: utf-8 -*-
"""诊断：Agent `system_prompt`（role 块）的体积构成 —— 到底贵在哪一段。

## 为什么需要这个工具

端到端实测（2026-10-26，生产库快照 + 真实 `execute` 链路）发现：

  · **system prompt 占整包 98%**（4,401 tok vs messages 89 tok）—— 成本几乎全在 system；
  · 其中 `role` 块（= `agents.system_prompt`）在真实链路上只有 859 tok（路由到 `chat`），
    但 **`智源对接agent` 的 system_prompt 是 14,538 字符 ≈ 5,300 tok**；
  · 该 14,538 字符里 **14,002 字符（96.3%）是 58 个 MCP 工具的「使用手册」**
    （形如 `工具名\n分页查询...keyword 匹配...`）。

⇒ 问题不是"role 写得太长"，而是**工具手册与工具暴露粒度脱节**：
  上一轮已把 MCP 工具按需暴露到 8 个（`mcp_min_keep=8`），
  但 role 里仍然全文注入了 58 个工具的说明 —— 其中 50 个工具的用法
  对**本轮根本没有暴露给模型**，是纯噪音。

## 这个工具做什么（只读，不改任何数据）

按 `mcp_servers.tools` 的工具名切分 `agents.system_prompt`，逐段统计：
  · 每个工具说明段的字符数 / token 数
  · 哪些工具名在 system_prompt 里出现 / 缺失
  · **本轮实际暴露的工具集**（走真实 `_build_tools_def`）对应的说明段占比
  · 给出"若只保留本轮暴露工具的说明，可省多少"

## 用法

    ./.venv/Scripts/python.exe -X utf8 tools/analyze_role_manual.py
    ./.venv/Scripts/python.exe -X utf8 tools/analyze_role_manual.py --agent 智源对接agent \\
        --query 查询智源工程列表
    ./.venv/Scripts/python.exe -X utf8 tools/analyze_role_manual.py --all   # 全部 Agent 概览
"""
import argparse
import json
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from core.token_counter import count_tokens          # noqa: E402
from database import get_db                          # noqa: E402


def _load_agents(con):
    out = {}
    for r in con.execute("SELECT name, system_prompt FROM agents WHERE status='active'").fetchall():
        out[r["name"]] = r["system_prompt"] or ""
    return out


def _mcp_tool_names(con):
    """全部 MCP server 的工具名 → {tool_name: server_name}。"""
    owner = {}
    for r in con.execute("SELECT name, tools FROM mcp_servers").fetchall():
        try:
            for n in (json.loads(r["tools"] or "[]") or []):
                if isinstance(n, str) and n.strip():
                    owner.setdefault(n.strip(), r["name"])
        except Exception:
            continue
    return owner


def _split_manual(sp: str, tool_names):
    """按工具名切分 system_prompt，返回 [(tool_name|'__prose__', text)]。

    切分规则保守（宁可不切，也不切错）：
      工具名必须**独占行首**（可带尾部空白）才算段落起点 ——
      与人工书写的「工具名换行接说明」格式一致，避免误伤正文里的工具名提及。
    """
    if not sp:
        return []
    pat = re.compile(r"(?m)^(" + "|".join(re.escape(n) for n in
                                        sorted(tool_names, key=len, reverse=True)) + r")[ \t]*$")
    marks = [(m.start(), m.group(1)) for m in pat.finditer(sp)]
    if not marks:
        return [("__prose__", sp)]
    out = []
    if marks[0][0] > 0:
        out.append(("__prose__", sp[:marks[0][0]]))
    for i, (pos, name) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(sp)
        out.append((name, sp[pos:end]))
    return out


def analyze(con, agent_name, user_input, exposed=None):
    sp = con.execute("SELECT system_prompt FROM agents WHERE name=?", (agent_name,)).fetchone()
    sp = (sp["system_prompt"] if sp else "") or ""
    owner = _mcp_tool_names(con)
    segs = _split_manual(sp, set(owner))

    total_chars = len(sp)
    total_tok = count_tokens(sp)
    man_segs = [(n, t) for n, t in segs if n != "__prose__"]
    prose_segs = [(n, t) for n, t in segs if n == "__prose__"]
    man_chars = sum(len(t) for _, t in man_segs)
    man_tok = sum(count_tokens(t) for _, t in man_segs)

    print("=" * 84)
    print(f"Agent: {agent_name}")
    print("=" * 84)
    print(f"  system_prompt 合计       : {total_chars:>7} 字符 / {total_tok:>6} tok")
    print(f"  其中工具手册段           : {man_chars:>7} 字符 / {man_tok:>6} tok"
          f"  ({man_chars/max(1,total_chars)*100:.1f}% 字符)")
    print(f"  其中非工具叙述           : {total_chars-man_chars:>7} 字符"
          f" / {total_tok-man_tok:>6} tok")
    print(f"  识别到的工具段数         : {len(man_segs)} 段"
          f"（MCP 工具库共 {len(owner)} 个）")

    if not man_segs:
        print("\n  （未识别到工具手册段，无需处理）")
        return

    # 本轮实际暴露的工具（若给了 user_input）
    if exposed is not None:
        exp = set(exposed)
        kept = [(n, t) for n, t in man_segs if n in exp]
        dropped = [(n, t) for n, t in man_segs if n not in exp]
        k_chars = sum(len(t) for _, t in kept)
        d_chars = sum(len(t) for _, t in dropped)
        print(f"\n  本轮暴露的 MCP 工具       : {len(exp)} 个")
        print(f"  ├─ 说明段被用到           : {len(kept)} 段 / {k_chars} 字符"
              f" / {sum(count_tokens(t) for _,t in kept)} tok")
        print(f"  └─ 说明段**纯噪音**       : {len(dropped)} 段 / {d_chars} 字符"
              f" / {sum(count_tokens(t) for _,t in dropped)} tok"
              f"  ({d_chars/max(1,total_chars)*100:.1f}% 全文)")
        if dropped:
            print(f"\n  ⇒ 若只保留本轮暴露工具的说明，可省 {d_chars} 字符"
                  f" ≈ {sum(count_tokens(t) for _,t in dropped)} tok"
                  f"（占 system 的 {sum(count_tokens(t) for _,t in dropped)/max(1,total_tok)*100:.1f}%）")
        print(f"\n  未暴露但仍有说明段的工具（前 20）：")
        for n, t in sorted(dropped, key=lambda x: -len(x[1]))[:20]:
            print(f"    {len(t):>5} 字符  {n}")

    print(f"\n  最长的 10 段：")
    for n, t in sorted(man_segs, key=lambda x: -len(x[1]))[:10]:
        print(f"    {len(t):>5} 字符 / {count_tokens(t):>4} tok  {n}")

    # 缺失说明的工具（MCP 有、prompt 里没有）
    in_prompt = {n for n, _ in man_segs}
    missing = sorted(set(owner) - in_prompt)
    if missing:
        print(f"\n  MCP 有但 system_prompt 无说明的工具（{len(missing)} 个）：")
        print(f"    {missing[:12]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", default="智源对接agent")
    ap.add_argument("--query", default="查询智源工程列表",
                    help="用于计算「本轮实际暴露的工具集」；传空串则跳过该段分析")
    ap.add_argument("--all", action="store_true", help="全部 active Agent 概览")
    args = ap.parse_args()

    con = get_db()
    try:
        if args.all:
            agents = _load_agents(con)
            owner = _mcp_tool_names(con)
            print("=" * 84)
            print(f"{'Agent':<26} {'总字符':>8} {'总tok':>7} {'手册字符':>9} {'手册%':>7}")
            print("=" * 84)
            rows = []
            for name, sp in agents.items():
                segs = _split_manual(sp, set(owner))
                man = sum(len(t) for n, t in segs if n != "__prose__")
                rows.append((name, len(sp), count_tokens(sp), man,
                             man / max(1, len(sp)) * 100))
            for r in sorted(rows, key=lambda x: -x[1])[:20]:
                print(f"{r[0]:<26} {r[1]:>8} {r[2]:>7} {r[3]:>9} {r[4]:>6.1f}%")
            return 0

        exposed = None
        if args.query:
            # 走真实装配，拿到本轮实际注入的工具集
            from agent import AgentPipeline
            p = AgentPipeline()
            c = get_db()
            try:
                p.registry.load_from_db(c, {"role_name": "admin", "role": "admin"})
            finally:
                c.close()
            td = p._build_tools_def(args.agent, args.query,
                                    {"role_name": "admin", "role": "admin"})
            owner = _mcp_tool_names(con)
            exposed = [t["function"]["name"] for t in td
                       if t["function"]["name"] in owner]
            print(f"[参考] query={args.query!r} ⇒ 本轮暴露 MCP 工具 {len(exposed)} 个\n")

        analyze(con, args.agent, args.query, exposed)
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
