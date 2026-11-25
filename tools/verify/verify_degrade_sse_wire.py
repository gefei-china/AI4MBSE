# -*- coding: utf-8 -*-
"""降级事件「真的接到了会话流上」门禁 —— 2026-11-25（P0）。

## 它防的是什么
`core/degrade.py` 有了台账、`routers/conversations.py` 有了 drain→yield ——
但这两件事之间是**弱连接**：删掉一行 `yield`、把 drain 挪到 done 之后、
或者把 collector 建在 `iter_with_heartbeat_async` **之后**（那样 worker 的
`copy_context()` 就拷不到它），台账依然"有数"，而用户**一条降级也看不到**。
这类"接了一半"的改动在本仓反复出现（"记录了但没人看得到"）。

因此本门禁钉住四件事，缺一条就红：
  W1  collector 必须在构造 `iter_with_heartbeat_async` **之前**创建
      （跨线程可见性的唯一前提 —— worker 用 copy_context 继承父上下文）；
  W2  转发循环里必须有 `drain` → `yield`，且 **drain 出现在处理 done/error 之前**
      （done 一转发前端就收口，之后发的降级帧会被丢弃）；
  W3  finally 里必须 `reset_current()`
      （不清会泄漏到复用同一 context 的下一轮 ⇒ 上一轮的降级卡出现在下一轮，
        张冠李戴比看不见更糟）；
  W4  `degrade` 不得被算作 `_sent_any`（它是旁路通知，不是"已给用户输出正文"，
      否则失败归因会把"一个字没出"误判成"说到一半断"）。

## 真跑一次转发（不是只做文本匹配）
W5~W7 用**真实的** `core.degrade` 采集器 + 一个模拟的 async 事件源，
按端点里的同一顺序（先 drain 再转发）复刻转发循环，断言：
  · 产出里确实出现 `event: degrade`；
  · 载荷字段齐全（where/level/impact/count），前端 procAddDegrade 依赖它们；
  · 同一 where 二次发生时，产出里的 count 变成 2（前端据此显示 ×2）。

## 变异自证
  M1  把 drain 挪到"处理完 raw 之后" → W2 顺序判据变红
  M2  删掉 finally 里的 reset        → W3 变红
  M3  collector 在包装器之后创建      → W1 变红
"""
import ast
import asyncio
import json
import os
import re
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.chdir(ROOT)

import core.degrade as DG  # noqa: E402
from core.sse import to_sse_frame  # noqa: E402

PASS, FAIL, SKIPPED = [], [], []
CONV = os.path.join(ROOT, "routers", "conversations.py")


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s%s" % ("PASS" if cond else "FAIL", name,
                         ("  <- " + str(detail)) if detail else ""), flush=True)


def _read(path):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


SRC = _read(CONV)

# ══ W1~W4：接线位置（静态，但判据是**相对位置**而不是"出现过"）═══════════
print("[1] 会话流接线位置", flush=True)
_i_collect = SRC.find("_dg_col = _deg.collect()")
_i_wrap = SRC.find("_frames = iter_with_heartbeat_async(")
check("W1 collector 在 iter_with_heartbeat_async **之前**创建（跨线程可见的前提）",
      _i_collect > 0 and _i_wrap > 0 and _i_collect < _i_wrap,
      "collect@%s wrap@%s" % (_i_collect, _i_wrap))

_i_drain = SRC.find("for _dg in _dg_col.drain():")
_i_done = SRC.find('startswith(("event: done", "event: error"))')
check("W2a 转发循环里有 drain → yield 降级帧",
      _i_drain > 0 and "_to_frame({\"type\": \"degrade\"" in SRC,
      "drain@%s" % _i_drain)
check("W2b drain 出现在「终态哨兵」之前（done 一转发前端就收口，之后发的会丢）",
      _i_drain > 0 and _i_done > 0 and _i_drain < _i_done,
      "drain@%s done_sentinel@%s" % (_i_drain, _i_done))

_i_reset = SRC.find("_deg_r.reset_current()")
check("W3 finally 里有 reset_current()（否则泄漏到复用 context 的下一轮）",
      _i_reset > 0, "reset@%s" % _i_reset)
# 必须在 finally 段内（即位于第一个 finally 之后、且不在 try 体内提前调用）
_i_fin = SRC.find("        finally:\n            # ── P0 降级可见")
check("W3b reset_current 位于生成器 finally 段首（收尾必经）",
      _i_fin > 0 and _i_reset > _i_fin,
      "finally@%s reset@%s" % (_i_fin, _i_reset))

_w4_ok = ('"connected", "degrade"' in SRC or "'connected', 'degrade'" in SRC)
check("W4 `degrade` 被排除在 _sent_any 之外（旁路通知不算「已输出正文」）",
      _w4_ok, "found=%s" % _w4_ok)

# ══ W5~W7：真跑一次转发（复刻端点里的同一顺序）════════════════════════
print("[2] 真跑转发循环（模拟事件源）", flush=True)


async def _sim_forward():
    """按端点里的顺序复刻：drain 降级 → 再转发 raw。"""
    out = []
    col = DG.collect()
    # 事件源"跑起来"，中途产生两次同 where 的降级
    DG.note("sim.rag", RuntimeError("embed 超时"), level="degraded",
            impact="语义召回缺失，本轮只用关键词")
    frames = ["event: token\ndata: {\"delta\":\"A\"}\n\n",
              "event: done\ndata: {\"data\":{\"ok\":1}}\n\n"]
    for raw in frames:
        # 转发前先交付（与端点同一顺序）
        for _dg in col.drain():
            out.append(to_sse_frame({"type": "degrade", **_dg}))
        out.append(raw)
        if raw.startswith("event: done"):
            # done 之后又发生一次降级（模拟收尾路径）—— 它应该**赶不上**本流，
            # 但必须仍进全局聚合（W7 断言这一点，证明"发不出去 ≠ 没记录"）
            DG.note("sim.rag", RuntimeError("embed 超时 again"), level="degraded",
                    impact="语义召回缺失，本轮只用关键词")
    DG.reset_current()
    return out


_out = asyncio.run(_sim_forward())
_deg_frames = [f for f in _out if f.startswith("event: degrade")]
check("W5 转发产出里确实出现 `event: degrade` 帧", len(_deg_frames) == 1,
      "frames=%d" % len(_deg_frames))
_payload = {}
if _deg_frames:
    _m = re.search(r"data: (\{.*\})\s*$", _deg_frames[0])
    try:
        _payload = json.loads(_m.group(1)) if _m else {}
    except Exception:
        _payload = {}
check("W6 载荷字段齐全（前端 procAddDegrade 依赖 where/level/impact/count）",
      _payload.get("where") == "sim.rag" and _payload.get("level") == "degraded"
      and "语义召回" in (_payload.get("impact") or "")
      and int(_payload.get("count") or 0) == 1,
      repr(_payload))
check("W7 done 之后发生的降级仍进全局聚合（发不出去 ≠ 没记录）",
      _payload.get("where") == "sim.rag" and DG.snapshot()["total"] >= 2,
      "global_total=%s" % DG.snapshot().get("total"))

# 合并后再交付：同一 where 第二次发生，count 应变 2
print("[3] 合并计数可达前端", flush=True)


async def _sim_dup():
    out = []
    col = DG.collect()
    DG.note("sim.dup", RuntimeError("1"), level="degraded")
    for _dg in col.drain():
        out.append(json.loads(_dg and json.dumps(_dg)))
    DG.note("sim.dup", RuntimeError("2"), level="degraded")
    for _dg in col.drain():
        out.append(json.loads(json.dumps(_dg)))
    DG.reset_current()
    return out


_dup = asyncio.run(_sim_dup())
check("W8 同 where 二次发生时 count 变 2（前端才可能显示 ×2）",
      len(_dup) == 2 and _dup[0].get("count") == 1 and _dup[1].get("count") == 2,
      repr([d.get("count") for d in _dup]))

# ══ 变异自证 ═════════════════════════════════════════════════════════
print("[4] 变异自证（判据有牙齿）", flush=True)


def _w2_order_ok(src: str) -> bool:
    d = src.find("for _dg in _dg_col.drain():")
    s = src.find('startswith(("event: done", "event: error"))')
    return d > 0 and s > 0 and d < s


# M1：把 drain 块挪到终态哨兵之后
_m1 = SRC.replace(
    """                if _dg_col is not None:
                    try:
                        from core.sse import to_sse_frame as _to_frame
                        for _dg in _dg_col.drain():
                            yield _to_frame({"type": "degrade", **_dg})
                    except Exception:
                        pass
""", "")
_m1 = _m1.replace(
    'if isinstance(raw, str) and raw.startswith(("event: done", "event: error")):',
    'if isinstance(raw, str) and raw.startswith(("event: done", "event: error")):\n'
    '                _unused = [x for x in _dg_col.drain()]')
check("M1 把 drain 挪到终态哨兵之后 → W2b 判据变红", _w2_order_ok(SRC) and not _w2_order_ok(_m1),
      "orig=%s mutant=%s" % (_w2_order_ok(SRC), _w2_order_ok(_m1)))

# M2：删掉 reset
_m2 = SRC.replace("_deg_r.reset_current()", "pass")
check("M2 删掉 finally 里的 reset → W3 判据变红",
      ("_deg_r.reset_current()" in SRC) and ("_deg_r.reset_current()" not in _m2),
      "mutant_has_reset=%s" % ("_deg_r.reset_current()" in _m2))

# M3：collector 挪到包装器之后
_m3 = SRC.replace(
    """            _dg_col = None
            try:
                from core import degrade as _deg
                _dg_col = _deg.collect()
            except Exception:
                _dg_col = None""", "")
_m3 = _m3.replace("            async for raw in _frames:",
                  "            _dg_col = None\n            async for raw in _frames:")
_w1_ok = lambda s: (s.find("_dg_col = _deg.collect()") > 0
                    and s.find("_dg_col = _deg.collect()") < s.find("_frames = iter_with_heartbeat_async("))
check("M3 collector 挪到包装器之后 → W1 判据变红",
      _w1_ok(SRC) and not _w1_ok(_m3),
      "orig=%s mutant=%s" % (_w1_ok(SRC), _w1_ok(_m3)))

print("\n=== %d PASS / %d FAIL / %d SKIP ===" % (len(PASS), len(FAIL), len(SKIPPED)), flush=True)
if SKIPPED:
    print("SKIP 项（未验证，不等于通过）：%s" % ", ".join(SKIPPED), flush=True)
if FAIL:
    print("FAIL: %s" % ", ".join(FAIL), flush=True)
sys.exit(1 if FAIL else 0)
