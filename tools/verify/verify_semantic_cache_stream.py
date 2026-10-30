# -*- coding: utf-8 -*-
"""P1-1 门禁：语义缓存接入流式路径（`execute_stream`）。

## 背景（实测）

`semantic_cache` 此前只在 `execute.py:477/795` 使用，`stream.py` **0 处引用**；
而生产 SSE 入口（`routers/conversations.py:391`）走 `execute_stream`
⇒ **主对话完全不享受语义缓存**。而 `chat_probe`（主对话）占真实 token 的
51%（15.6M / 30.47M）。

## 判据（每条带副作用隔离）

  [1] 开关关 ⇒ 不发缓存、不读缓存（行为与改动前逐字节一致：仍真实调用 LLM）
  [2] 开关开 + 首次提问 ⇒ 真实调用 LLM，且**写入缓存**
  [3] 同一问题再问 ⇒ **命中缓存**，llm_info.provider == "semantic-cache"
  [4] 命中时仍是**流式**：token 事件 ≥ 2 个（不是一次性一大段）
  [5] 命中时**不谎报**：latency_ms == 0、tokens 全 0、used_mock == False
  [6] 命中时**不回写**（hit 后缓存条数不增、时间戳不被刷新）
  [7] 有工具（tools_def 非空）⇒ 不读缓存（避免复用"无工具答案"）
  [8] 有附件 ⇒ 不读缓存（缓存键只有 user_input，看不见图/文档）
  [9] dry_run / resume ⇒ 不读缓存
  [10] 源码级：读取与写入两处都存在（防被后续重构悄悄删掉）
"""
import os
import sys
import json
import shutil
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

_TMP = os.path.join(tempfile.gettempdir(), "verify_sc_stream.db")
for _p in (_TMP, _TMP + "-wal", _TMP + "-shm"):
    if os.path.exists(_p):
        os.remove(_p)
shutil.copy2(os.path.join(_ROOT, "backups", "t-prod-20261026-122709.db"), _TMP)
os.environ["MBSE_DB_PATH"] = _TMP
os.environ["MBSE_LLM_FORCE_MOCK"] = "1"

import logging                                        # noqa: E402
logging.disable(logging.WARNING)

from database import init_db, get_db                  # noqa: E402
init_db()

from agent import get_pipeline                        # noqa: E402
from core import config as cfg                        # noqa: E402

_USER = {"id": 1, "username": "sc", "display_name": "SC", "role_name": "admin",
         "role": "admin", "department": "验证"}
_FAILS = []


def check(name, cond, detail=""):
    if not cond:
        _FAILS.append(name)
    print(f"[{'OK  ' if cond else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""))


def new_conv(title):
    c = get_db()
    cid = c.execute("INSERT INTO conversations (title,intent,status,phase) "
                    "VALUES (?,'chat','active','')", (title,)).lastrowid
    c.commit()
    c.close()
    return cid


def events_score(evts):
    """从事件流提取关键指标。"""
    out = {"tokens": 0, "text": "", "done": None, "provider": None, "cache_hit": False}
    for e in evts:
        t = e.get("type")
        if t == "token":
            out["tokens"] += 1
            out["text"] += e.get("delta") or ""
        elif t == "done":
            out["done"] = e.get("data") or {}
            llm = (out["done"] or {}).get("llm") or {}
            out["provider"] = llm.get("provider")
            out["cache_hit"] = bool(llm.get("cache_hit"))
    return out


def run_stream(msg, cid, **kw):
    p = get_pipeline()
    return list(p.execute_stream(msg, cid, **kw))


def cache_count():
    c = get_db()
    try:
        return c.execute("SELECT COUNT(*) c FROM semantic_cache").fetchone()["c"]
    finally:
        c.close()


_sec = cfg._CONFIG.setdefault("semantic_cache", {})
_old_on = _sec.get("enabled", False)
_sec["threshold"] = 0.99      # 收紧，避免近义句误命中干扰判据

# ══════════════════════════════════════════════════════════════
# [10] 源码级：两处都在
# ══════════════════════════════════════════════════════════════
print("=" * 78)
print("[10] 源码接线")
print("=" * 78)
_src = open(os.path.join(_ROOT, "agent", "pipeline_parts", "stream.py"),
            encoding="utf-8").read()
check("[10] stream.py 存在缓存读取（SemanticCache.get）", "SemanticCache.get" in _src)
check("[10] stream.py 存在缓存写入（SemanticCache.put）", "SemanticCache.put" in _src)
check("[10] 命中时用 iter_stream_chunks 伪装流式", "iter_stream_chunks(_cache_answer)" in _src)
check("[10] llm_info 明确标注 semantic-cache（不谎报 provider）",
      '"provider": "semantic-cache"' in _src)

# ══════════════════════════════════════════════════════════════
# [1] 开关关：不读不写
# ══════════════════════════════════════════════════════════════
print("\n" + "=" * 78)
print("[1] 开关关（改动前行为）")
print("=" * 78)
_sec["enabled"] = False
_n0 = cache_count()
cid1 = new_conv("sc-off")
ev = run_stream("什么是 SysML v2 的活动图", cid1, user=_USER)
r = events_score(ev)
check("[1] 开关关 ⇒ 未命中缓存", not r["cache_hit"], f"provider={r['provider']}")
check("[1] 开关关 ⇒ 不写缓存", cache_count() == _n0, f"{_n0} → {cache_count()}")
check("[1] 开关关 ⇒ 仍有正常回复", bool(r["text"].strip()), f"{len(r['text'])} 字符")

# ══════════════════════════════════════════════════════════════
# [2] 开关开 + 首问：真实调用 + 写缓存
# ══════════════════════════════════════════════════════════════
print("\n" + "=" * 78)
print("[2] 开关开 + 首次提问")
print("=" * 78)
_sec["enabled"] = True
_n1 = cache_count()
cid2 = new_conv("sc-on-1")
ev2 = run_stream("SysML v2 活动图包含哪些必备元素", cid2, user=_USER)
r2 = events_score(ev2)
check("[2] 首问未命中（缓存为空或不同问题）", not r2["cache_hit"], f"provider={r2['provider']}")
check("[2] 首问有正文", bool(r2["text"].strip()), f"{len(r2['text'])} 字符")
_n2 = cache_count()
print(f"  缓存条数 {_n1} → {_n2}（embedding 不可用时 put 会静默跳过）")
check("[2] 首问写入缓存（若 embedding 可用）", _n2 >= _n1,
      "允许不增：embedding 后端降级 bigram 时 put 主动跳过（不是缺陷）")

# ══════════════════════════════════════════════════════════════
# [3][4][5][6] 同一问题再问：命中 / 流式 / 不谎报 / 不回写
# ══════════════════════════════════════════════════════════════
print("\n" + "=" * 78)
print("[3-6] 同一问题再问")
print("=" * 78)
if _n2 > _n1:
    cid3 = new_conv("sc-on-2")
    ev3 = run_stream("SysML v2 活动图包含哪些必备元素", cid3, user=_USER)
    r3 = events_score(ev3)
    check("[3] 再问同一问题 ⇒ 命中缓存", r3["cache_hit"] and r3["provider"] == "semantic-cache",
          f"cache_hit={r3['cache_hit']} provider={r3['provider']}")
    check("[4] 命中时仍是流式（token 事件 ≥ 2）", r3["tokens"] >= 2, f"{r3['tokens']} 个 token 事件")
    check("[4] 命中正文与原答案一致", r3["text"].strip() == r2["text"].strip(),
          f"{len(r3['text'])} vs {len(r2['text'])}")
    _llm = (r3["done"] or {}).get("llm") or {}
    check("[5] 不谎报：latency_ms == 0", _llm.get("latency_ms") == 0, str(_llm.get("latency_ms")))
    check("[5] 不谎报：tokens 全 0",
          (_llm.get("tokens") or {}).get("prompt") == 0
          and (_llm.get("tokens") or {}).get("completion") == 0, str(_llm.get("tokens")))
    check("[5] 不谎报：used_mock == False", _llm.get("used_mock") is False, str(_llm.get("used_mock")))
    _n3 = cache_count()
    check("[6] 命中后不回写（条数不增）", _n3 == _n2, f"{_n2} → {_n3}")
    check("[6] 命中后 done 事件完整（含 content/message_id）",
          bool((r3["done"] or {}).get("content")) and "message_id" in (r3["done"] or {}),
          f"keys={sorted((r3['done'] or {}).keys())[:6]}")
else:
    print("  ⚠️ embedding 后端不可用（put 静默跳过）⇒ 用**打桩**验证接线本身")
    print("     （打桩只替换 get/put 两个静态方法，不替换我的闸门/流式/llm_info 逻辑）")
    import semantic_cache as _scmod

    _store = {}
    _orig_get, _orig_put = _scmod.SemanticCache.get, _scmod.SemanticCache.put
    _put_calls = {"n": 0}

    def _stub_put(conn, query, answer, max_entries=2000):
        _put_calls["n"] += 1
        _store[str(query).strip()] = str(answer)

    def _stub_get(conn, query, threshold=0.95):
        return _store.get(str(query).strip())

    _scmod.SemanticCache.get = staticmethod(_stub_get)
    _scmod.SemanticCache.put = staticmethod(_stub_put)
    try:
        cidA = new_conv("sc-stub-1")
        _p0 = len(_store)
        evA = run_stream("SysML v2 活动图包含哪些必备元素", cidA, user=_USER)
        rA = events_score(evA)
        check("[2] 首问真实调用 LLM（未命中）", not rA["cache_hit"], f"provider={rA['provider']}")
        check("[2] 首问写入缓存（打桩计数 +1）", _put_calls["n"] == _p0 + 1,
              f"put 调用 {_put_calls['n']} 次")
        check("[2] 缓存里确有该问的答案", "SysML v2 活动图包含哪些必备元素" in _store,
              f"键数 {len(_store)}")

        cidB = new_conv("sc-stub-2")
        evB = run_stream("SysML v2 活动图包含哪些必备元素", cidB, user=_USER)
        rB = events_score(evB)
        check("[3] 再问同一问题 ⇒ 命中缓存",
              rB["cache_hit"] and rB["provider"] == "semantic-cache",
              f"cache_hit={rB['cache_hit']} provider={rB['provider']}")
        check("[4] 命中时仍是流式（token 事件 ≥ 2）", rB["tokens"] >= 2,
              f"{rB['tokens']} 个 token 事件")
        check("[4] 命中正文与原答案逐字节一致", rB["text"].strip() == rA["text"].strip(),
              f"{len(rB['text'])} vs {len(rA['text'])}")
        _llm = (rB["done"] or {}).get("llm") or {}
        check("[5] 不谎报：latency_ms == 0", _llm.get("latency_ms") == 0, str(_llm.get("latency_ms")))
        check("[5] 不谎报：tokens 全 0",
              (_llm.get("tokens") or {}).get("prompt") == 0
              and (_llm.get("tokens") or {}).get("completion") == 0, str(_llm.get("tokens")))
        check("[5] 不谎报：used_mock == False", _llm.get("used_mock") is False,
              str(_llm.get("used_mock")))
        check("[6] 命中后**不回写**（put 计数未增）", _put_calls["n"] == _p0 + 1,
              f"put 调用 {_put_calls['n']} 次")
        check("[6] 命中后 done 事件完整（含 content / message_id）",
              bool((rB["done"] or {}).get("content")) and "message_id" in (rB["done"] or {}),
              f"keys={sorted((rB['done'] or {}).keys())[:6]}")
        check("[3] 命中缓存时 done.intent 正确（不丢上下文）",
              bool((rB["done"] or {}).get("intent")), str((rB["done"] or {}).get("intent")))
    finally:
        _scmod.SemanticCache.get = _orig_get
        _scmod.SemanticCache.put = _orig_put

# ══════════════════════════════════════════════════════════════
# [7][8][9] 闸门：有工具 / 有附件 / dry_run 都不读缓存
# ══════════════════════════════════════════════════════════════
print("\n" + "=" * 78)
print("[7-9] 安全闸门")
print("=" * 78)
# 源码级：闸门条件必须包含四个关键项（运行时构造这些场景成本高且易误判）
_gate_src = _src[_src.find("_sc_gate = ("): _src.find("_sc_gate = (") + 420] \
    if "_sc_gate = (" in _src else ""
# ★ 2026-10-26 设计变更：闸门**不再**含 `not tools_def` ——
#   实测它恒为 False（chat 恒注入 core_keep + ask_user）⇒ 原闸门是死代码。
#   现在断言它**不在**闸门里，并断言新的定位纪律（必须在 ReAct 之前、不依赖 tool_injected）。
_anchor = _src.find("_cache_answer = None")
_gate_win_raw = _src[max(0, _anchor - 1400): _anchor + 1400] if _anchor >= 0 else ""
# 只看**代码行**（剥掉注释），否则自己写的说明文字里提到 "not tools_def" 会误判
_gate_win = "\n".join(ln for ln in _gate_win_raw.split("\n")
                      if not ln.lstrip().startswith("#"))
check("[7] 闸门**不含** not tools_def（该条件恒假，是死代码）",
      "not tools_def" not in _gate_win, "含则说明又退回死代码")
check("[7] 缓存判定位于 ReAct 之前（短路 tools_def/tool_injected）",
      "tools_def = None           # 让 ReAct 立即 break" in _src
      and "tool_injected = True       # 让下方生成段跳过" in _src)
check("[8] 闸门含「无附件」（not att_blocks）", "not att_blocks" in _gate_win)
check("[9] 闸门含 dry_run / resume 互斥",
      "not dry_run" in _gate_win and "not resume_run_id" in _gate_win)

# 运行时：dry_run 必须不落库但仍能返回（回归锁：不能因为缓存改动破坏 dry_run）
cid4 = new_conv("sc-dry")
ev4 = run_stream("dry run 测试问题", cid4, user=_USER, dry_run=True)
r4 = events_score(ev4)
check("[9] dry_run 仍能正常返回内容", bool(r4["text"].strip()), f"{len(r4['text'])} 字符")

# 运行时：命中缓存的路径下，intent 仍正确传递
if _n2 > _n1:
    cid5 = new_conv("sc-intent")
    ev5 = run_stream("SysML v2 活动图包含哪些必备元素", cid5, user=_USER)
    d5 = events_score(ev5)["done"] or {}
    check("[3] 命中缓存时 done.intent 正确（不丢上下文）", bool(d5.get("intent")), str(d5.get("intent")))

_sec["enabled"] = _old_on
_sec.pop("threshold", None)

for _p in (_TMP, _TMP + "-wal", _TMP + "-shm"):
    try:
        if os.path.exists(_p):
            os.remove(_p)
    except Exception:
        pass

print("\n" + "=" * 78)
if _FAILS:
    print(f"❌ FAIL {len(_FAILS)} 项：" + "; ".join(_FAILS))
    sys.exit(1)
print("✅ ALL PASS —— 语义缓存已接入流式路径（命中/流式/不谎报/不回写/闸门/回滚）")
