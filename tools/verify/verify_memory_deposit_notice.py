# -*- coding: utf-8 -*-
"""记忆沉淀「存了什么必须告知用户」门禁（2026-11-27）。

## 它防的是什么
用户明确要求：**保存经验/记忆时要告知用户**。此前整条链路完全静默 —— 用户既不知道
平台记住了什么，也不知道「这次为什么没记」。这条链路跨了三层，任何一层断掉表现都一样：
  · `deposit()` 的 info 出口没填 ⇒ 上层拿不到 action；
  · `_memory_notice()` 把 `skipped` 也报出来 ⇒ 每次对话都刷屏，用户学会忽略所有提示；
  · stream / orchestration 没接 ⇒ 后端算出来了但用户永远看不到（"算对了没写出去"同族）；
  · 前端没 esc ⇒ notice 含用户/模型原文摘要，直接插 innerHTML 是可注入面。

## 判据（三层都要过，且每条变异都要能翻红）
A 区（行为，夹具可写）：
  A1 created    → info 含 action/id/content/scope
  A2 dedup_hit  → 命中近重复闸，info 明说"没有新增"，且**行数不变**
  A3 revived    → 冷层命中复活，info.action=revived
  A4 skipped    → 空内容；_memory_notice 返回 None（不打扰）
  A5 blocked    → 安全拦截；notice 只说"因安全策略"，**不泄露规则名/命中片段**
  A6 不传 info  → 行为逐字不变（既有 6 处调用方零影响）
  A7 相似度取真值：reason 里的 0.xxx 必须原样出现在 notice，取不到就略去括号
B 区（接线，AST/文本 + 行为）：
  B1 stream.py 两处沉淀调用后都 yield `progress` 且 `source=memory_deposit`
  B2 orchestration.py 的 card_data 带 `memory_notice`
  B3 前端 06-cards.js 渲染 `cd.memory_notice` 且**经 esc**
  B4 子任务不发（stream 第二处有 `conversation_id` 守卫）
  B5 `_mem_info` 在两处 stream 调用都被接住（不是只接了第一处）
C 区（变异自证）：
  M1 去掉前端 esc              ⇒ B3 翻红
  M2 去掉 stream 的 source 标记 ⇒ B1 翻红
  M3 去掉 orchestration 的字段 ⇒ B2 翻红

用法： ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_memory_deposit_notice.py
"""
import ast
import io
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.chdir(ROOT)

PASS, FAIL = [], []


def chk(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name,
                           ("  — " + str(detail)) if detail and not ok else ""))


def read(path):
    return io.open(os.path.join(ROOT, path), encoding="utf-8").read()


STREAM = "agent/pipeline_parts/stream.py"
ORCH = "agent/pipeline_parts/orchestration.py"
FRONT = "static/js/mods/06-cards.js"
MEM = "agent/pipeline_parts/memory.py"


# ══════════════════════════════════════════════════════════════════════
# A 区：行为（夹具，可写）
# ══════════════════════════════════════════════════════════════════════
def probe_deposit():
    sys.path.insert(0, os.path.join(ROOT, "tools", "verify"))
    import verify_memory_partition_p0 as P
    from services.memory_service import MemoryService as MS
    import core.security_scan as SS

    conn = P.fixture(with_flow=False)
    VEC, VER = [0.1] * 8, "notice-probe-v1"
    MS._embed = staticmethod(lambda _c, _t: (VEC, VER))
    out = {}
    try:
        n0 = conn.execute("SELECT COUNT(*) FROM agent_memory").fetchone()[0]
        txt = "这是一条足够长的测试经验，用于验证记忆告知出口"

        # A1 created
        i1 = {}
        id1 = MS.deposit(conn, "ag1", txt, "experience", mem_topic="tt", info=i1)
        out["created"] = {"info": dict(i1), "id": id1,
                          "n": conn.execute("SELECT COUNT(*) FROM agent_memory").fetchone()[0] - n0}

        # A2 dedup_hit（同 agent/topic/向量，命中写前近重复闸）
        i2 = {}
        id2 = MS.deposit(conn, "ag1", txt + "（另一种说法）", "experience",
                         mem_topic="tt", info=i2)
        out["dedup"] = {"info": dict(i2), "id": id2,
                        "n": conn.execute("SELECT COUNT(*) FROM agent_memory").fetchone()[0] - n0}

        # A3 revived：把该条降冷层，再用同向量写入
        conn.execute("UPDATE agent_memory SET tier='archival' WHERE id=?", (id1,))
        conn.commit()
        i3 = {}
        id3 = MS.deposit(conn, "ag1", txt, "experience", mem_topic="tt", info=i3)
        out["revived"] = {"info": dict(i3), "id": id3,
                          "tier": conn.execute("SELECT tier FROM agent_memory WHERE id=?",
                                               (id1,)).fetchone()[0]}

        # A4 skipped：空内容
        i4 = {}
        MS.deposit(conn, "ag1", "", "fact", info=i4)
        out["skipped"] = dict(i4)

        # A5 blocked：安全扫描命中（伪造 scanner，不依赖真实规则/配置）
        class _FakeScanner:
            @staticmethod
            def enabled():
                return True

            @staticmethod
            def scan(_content):
                return {"safe": False, "matched": [
                    {"type": "SECRET_TYPE", "rule": "RULE_SHOULD_NOT_LEAK",
                     "snippet": "SENSITIVE_SNIPPET_HERE"}]}

        _orig = SS.MemoryScanner
        SS.MemoryScanner = _FakeScanner
        try:
            i5 = {}
            MS.deposit(conn, "ag1", "无论内容是什么都会被拦", "fact", info=i5)
            out["blocked"] = dict(i5)
        finally:
            SS.MemoryScanner = _orig

        # A6 不传 info：行为逐字不变（能新增、能返回 id）
        i6 = MS.deposit(conn, "noinfo", txt, "fact", mem_topic="tt")
        out["noinfo"] = i6
    finally:
        conn.close()
    return out


def probe_notice(res):
    """取 `_memory_notice`（纯函数）。

    ⚠️ 不能用 exec 独立命名空间：memory.py 顶部是 `from .common import *`，
    没有包上下文时抛 ImportError（实测踩到）。走真实 import 才能拿到类。
    """
    from agent.pipeline_parts.memory import MemoryMixin
    fn = MemoryMixin._memory_notice
    return {
        "created": fn(res["created"]["info"], scope_type="project"),
        "dedup": fn(res["dedup"]["info"], scope_type="project"),
        "revived": fn(res["revived"]["info"], scope_type="project"),
        "skipped": fn(res["skipped"], scope_type="project"),
        "blocked": fn(res["blocked"], scope_type="project"),
    }


# ══════════════════════════════════════════════════════════════════════
# B 区：接线（文本）—— 变异用替换文本，不 exec（避免语法错变异体假翻红）
# ══════════════════════════════════════════════════════════════════════
def wiring(stream_src, orch_src, front_src):
    # B5：两处调用都必须接住返回值
    caught = stream_src.count("_mem_info = self._deposit_session_memory(")
    # B1：两处 yield 都要有 source 标记
    src_n = stream_src.count('"source": "memory_deposit"')
    prog_n = stream_src.count('"type": "progress", "kind": "progress"')
    # B4：至少一处守卫（子任务 conversation_id=0 不发）
    guard = 'int(conversation_id or 0)' in stream_src
    # B2 / B3
    orch_field = '"memory_notice": _mem_notice' in orch_src
    front_use = "cd.memory_notice" in front_src
    front_esc = "esc(String(cd.memory_notice))" in front_src
    front_slot = "${memNotice}" in front_src
    return {"caught": caught, "src_n": src_n, "prog_n": prog_n,
            "guard": guard, "orch_field": orch_field,
            "front_use": front_use, "front_esc": front_esc, "front_slot": front_slot}


def main():
    print("=== A 区：deposit 行为出口（夹具）===")
    r = probe_deposit()
    c = r["created"]
    chk("A1 created：info 含 action/id/content/scope 且确实新增一行",
        c["info"].get("action") == "created" and c["info"].get("id") == c["id"]
        and bool(c["info"].get("content")) and c["n"] == 1
        and isinstance(c["info"].get("scope"), (tuple, list)), c)

    d = r["dedup"]
    chk("A2 dedup_hit：info 明说未重复保存，且**没有新增行**",
        d["info"].get("action") == "dedup_hit" and d["n"] == 1
        and "相似度" in str(d["info"].get("reason") or ""), d)

    v = r["revived"]
    chk("A3 revived：冷层命中复活，info.action=revived 且 tier 回 recall",
        v["info"].get("action") == "revived" and v.get("tier") == "recall", v)

    chk("A4 skipped：空内容标记 skipped", r["skipped"].get("action") == "skipped", r["skipped"])

    b = r["blocked"]
    chk("A5 blocked：安全拦截只报审计引用，不泄露规则名/命中片段",
        b.get("action") == "blocked" and b.get("reason") == "security_scan"
        and b.get("detail_ref") == "audit:memory_blocked"
        and all(x not in str(b) for x in ("RULE_SHOULD_NOT_LEAK", "SENSITIVE_SNIPPET_HERE",
                                          "SECRET_TYPE")), b)

    chk("A6 不传 info：既有行为逐字不变（仍返回 id）", bool(r["noinfo"]), r["noinfo"])

    print("\n=== A 区：_memory_notice 文案 ===")
    n = probe_notice(r)
    chk("A7 created 文案含「已记入」+ 作用域 + 摘要",
        bool(n["created"]) and "已记入" in n["created"]["notice"]
        and "本项目" in n["created"]["notice"] and "experience" == n["created"]["mem_type"], n["created"])
    import re as _re
    _sm = _re.search(r"\b([01]\.\d+)\b", str(d["info"].get("reason") or ""))
    chk("A8 dedup_hit 明说「未重复保存」且相似度取 reason 真值",
        bool(n["dedup"]) and "未重复保存" in n["dedup"]["notice"]
        and _sm is not None
        and _sm.group(1) in n["dedup"]["notice"], n["dedup"])
    chk("A9 revived 明说「冷层取回」", bool(n["revived"]) and "冷层取回" in n["revived"]["notice"], n["revived"])
    chk("A10 skipped 不打扰用户（返回 None）", n["skipped"] is None, n["skipped"])
    chk("A11 blocked 只说安全策略，不泄露规则/片段",
        bool(n["blocked"]) and "安全策略" in n["blocked"]["notice"]
        and all(x not in n["blocked"]["notice"] for x in
                ("RULE_SHOULD_NOT_LEAK", "SENSITIVE_SNIPPET_HERE", "SECRET_TYPE")), n["blocked"])

    print("\n=== B 区：三处接线 ===")
    s, o, f = read(STREAM), read(ORCH), read(FRONT)
    w = wiring(s, o, f)
    chk("B1 stream 两处沉淀调用都接住返回值", w["caught"] == 2, w)
    chk("B2 stream 两处 yield 都带 progress + source=memory_deposit",
        w["prog_n"] >= 2 and w["src_n"] == 2, w)
    chk("B3 子任务不发（conversation_id 守卫）", w["guard"], w)
    chk("B4 非流式 card_data 带 memory_notice", w["orch_field"], w)
    chk("B5 前端渲染该字段且必须 esc（防注入）", w["front_use"] and w["front_esc"] and w["front_slot"], w)

    print("\n=== C 区：变异自证（判据必须有牙）===")
    # M1：去掉前端 esc
    m1 = f.replace("esc(String(cd.memory_notice))", "cd.memory_notice", 1)
    chk("M1 去掉 esc ⇒ B5 翻红", m1 != f and not wiring(s, o, m1)["front_esc"])
    # M2：去掉 stream 的 source 标记
    m2 = s.replace('"source": "memory_deposit",', "", 2)
    chk("M2 去掉 source 标记 ⇒ B2 翻红", m2 != s and wiring(m2, o, f)["src_n"] == 0)
    # M3：去掉 orchestration 字段
    m3 = o.replace('"memory_notice": _mem_notice,', "", 1)
    chk("M3 去掉 card_data 字段 ⇒ B4 翻红", m3 != o and not wiring(s, m3, f)["orch_field"])

    print()
    print("=" * 72)
    print("PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
    if FAIL:
        print("失败项：")
        for x in FAIL:
            print("  -", x)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
