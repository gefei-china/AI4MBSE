# -*- coding: utf-8 -*-
"""R3 回归验证：主 Agent 不丢 + 无幽灵「知识库检索」环节（2026-10-16）。

背景（米爸两个反馈，均有落库证据）
----------------------------------
Q1「让 MCP 查询智源工程列表，无法正常返回」
   会话 9372 实测：第一轮 msg 4041 正确走「智源对接agent」；
   但用户点「跳过澄清」后，msg 4045 掉回「通用问答Agent」⇒ 该 Agent 没绑 MCP
   ⇒ 工程列表查询必然失败。真因：`sendResume`（11-pipeline.js）的 chat/stream
   请求体只有 {message, attachments, branch}，**没带 team/skill_name**。

Q2「为啥每次都固定有这几个环节，特别是知识库检索」
   截图中 4 个环节：意图识别 / 知识库检索未引用 / 生成与校验 / 写入会话。
   其中「知识库检索未引用」是**幽灵环节**：`should_retrieve=False` 时后端
   stream.py 的 `done` 事件仍无条件发射，前端 `onStageEvent` 据此渲染成
   "已完成·未引用"。同文件另两处（69-72 编排规划、1507-1510 编排复用）
   已按同一理由整段移除，只剩主直行路径漏改。

判据
----
S1 澄清续答请求体必须带 team（前端源码静态检查）
S2 端到端：带 team 的主 Agent 请求 ⇒ intent 是主 Agent
S3 端到端：**不带 team**（复刻事故请求形态）时不再静默掉回 chat ——
   至少要可见（warning）而不是无声换执行主体
S4 未配 kb_required 的 Agent ⇒ 流里**不出现**「知识库检索」stage 事件
S5 配了 kb_required / 有 # 标签时 ⇒ 该 stage 正常出现（不误伤）
"""
from __future__ import annotations

import json
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASE = "http://127.0.0.1:8000"
MAIN_AGENT = "智源对接agent"

_fails: list[str] = []
_passes = 0


def chk(name: str, ok: bool, detail: str = "") -> None:
    global _passes
    if ok:
        _passes += 1
        print(f"  [PASS] {name}" + (f"  — {detail}" if detail else ""))
    else:
        _fails.append(name)
        print(f"  [FAIL] {name}  — {detail}")


def section(t: str) -> None:
    print(f"\n{'=' * 70}\n{t}\n{'=' * 70}")


def sse_events(body: dict) -> list:
    """走真实 SSE 流，收集全部事件（只取 JSON data 行）。"""
    req = urllib.request.Request(
        BASE + "/api/conversations/%s/chat/stream" % body.pop("_cid"),
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "X-User-Id": "1", "X-Session-Token": "verify-r3"},
        method="POST")
    out = []
    with urllib.request.urlopen(req, timeout=240) as r:
        buf = ""
        while True:
            chunk = r.read(4096)
            if not chunk:
                break
            buf += chunk.decode("utf-8", "ignore")
            while "\n\n" in buf:
                frame, buf = buf.split("\n\n", 1)
                for ln in frame.splitlines():
                    if ln.startswith("data:"):
                        try:
                            out.append(json.loads(ln[5:].strip()))
                        except Exception:
                            pass
    return out


def main() -> int:
    section("S1 · 澄清续答请求体必须带 team（静态）")
    pl = (ROOT / "static/js/mods/11-pipeline.js").read_text(encoding="utf-8", errors="ignore")
    lines = pl.splitlines()
    # 定位 sendResume 函数体（从定义行到下一个顶层 `function` / 文件尾），
    # 再在其中找 chat/stream 的请求体。⚠️ 不要用「定长窗口」截取 ——
    # 上一版用 2200 字符窗口，恰好把 body 里的 team/skill_name 切在窗外，报了个假红。
    start = next(i for i, ln in enumerate(lines) if "async function sendResume(" in ln)
    end = next((i for i in range(start + 1, len(lines))
                if re.match(r"^(async )?function \w+", lines[i])), len(lines))
    fn = "\n".join(lines[start:end])
    _bi = fn.find("/chat/stream")
    _body = fn[_bi: _bi + 700] if _bi >= 0 else ""
    chk("sendResume 请求体带 team + skill_name",
        "team:" in _body and "skill_name:" in _body,
        "缺 team ⇒ 澄清续答后执行主体静默掉回通用 Agent（会话 9372 实证）")

    section("S4 · 未触发检索时不得出现「知识库检索」环节（静态）")
    st = (ROOT / "agent/pipeline_parts/stream.py").read_text(encoding="utf-8", errors="ignore")
    # 取「知识库检索 done」那一行，往前找最近的 `if should_retrieve:`，
    # 且两者之间不得夹着另一个 `yield`（否则说明守卫管不到这个 yield）。
    lines = st.splitlines()
    done_idx = next((i for i, ln in enumerate(lines)
                     if '"name": "知识库检索", "status": "done"' in ln), None)
    guarded = False
    if done_idx is not None:
        for j in range(done_idx - 1, -1, -1):
            ln = lines[j]
            if "yield" in ln:
                break                       # 前面隔了别的 yield ⇒ 没被这个 if 管住
            if re.match(r"\s*if should_retrieve:\s*$", ln):
                guarded = True
                break
    chk("知识库检索 done 事件受 should_retrieve 守卫（紧邻）",
        guarded, "无守卫 ⇒ 前端每轮渲染幽灵环节「知识库检索未引用」")
    chk("知识库检索 done 事件全文只有一处",
        sum(1 for ln in lines if '"name": "知识库检索", "status": "done"' in ln) == 1,
        "多处各写一份 ⇒ 必漂移（本类 bug 的成因）")

    section("S2/S3/S4' · 端到端（真实 HTTP SSE）")
    def post(path, body):
        req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json",
                                              "X-User-Id": "1", "X-Session-Token": "verify-r3"},
                                     method="POST")
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode())

    try:
        cv = post("/api/conversations", {"title": "verify-r3-main-agent"})
    except Exception as e:
        chk("端到端（服务未启动？）", False, f"{type(e).__name__}: {e}")
        _finish(); return 1
    cid = cv.get("id")

    # S2：带 team ⇒ 主 Agent 执行
    evs = sse_events({"_cid": cid, "message": "你好，请介绍下你的能力", "team": MAIN_AGENT})
    stages = [e for e in evs if e.get("type") == "stage"]
    names = [s.get("name") for s in stages]
    # ⚠️ 必须取 **done** 事件：run 事件不带 intent/agent，取第一个会得到 None（假红）。
    itent = next((s.get("intent") for s in stages
                  if s.get("name") == "意图识别" and s.get("status") == "done"), None)
    agnt = next((s.get("agent") for s in stages
                 if s.get("name") == "意图识别" and s.get("status") == "done"), None)
    chk("S2 带 team ⇒ 执行主体是所选主 Agent", itent == MAIN_AGENT,
        f"intent={itent}")
    chk("S2 agent 显示名同步为主 Agent", agnt == MAIN_AGENT, f"agent={agnt}")
    chk("S2 阶段含意图识别/生成与校验/写入会话",
        {"意图识别", "生成与校验", "写入会话"} <= set(names), f"stages={names}")

    # S4'：该 Agent 未配 kb_required、无 # 标签 ⇒ 不得出现知识库检索环节
    chk("S4' 未触发检索时**不出现**「知识库检索」环节",
        "知识库检索" not in names,
        f"stages={names}（出现即幽灵环节未除净）")

    # S3：不带 team（复刻事故请求形态）⇒ 记录实际掉向何方
    evs2 = sse_events({"_cid": cid, "message": "帮我查询工程列表"})
    st2 = [e for e in evs2 if e.get("type") == "stage"]
    it2 = next((s.get("intent") for s in st2
                if s.get("name") == "意图识别" and s.get("status") == "done"), None)
    print(f"         · 不带 team 时 intent={it2}")
    chk("S3 不带 team 时 intent 掉回自动识别（不再是主 Agent）",
        it2 != MAIN_AGENT, f"intent={it2}")

    _finish()
    return 0 if not _fails else 1


def _finish():
    section("结果")
    print(f"  通过 {_passes} 项，失败 {len(_fails)} 项")
    for f in _fails:
        print(f"    · {f}")
    if not _fails:
        print("  全部通过 ✓")


if __name__ == "__main__":
    raise SystemExit(main())