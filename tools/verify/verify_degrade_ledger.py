# -*- coding: utf-8 -*-
"""降级台账（P0：让错误可见）契约门禁 —— 2026-11-25。

## 防的是什么
架构评估 P0 实测：1980 个 `except Exception` 里 1361 个静默吞掉（69%）。
要治理它，先得有一个**统一的、可分级、可被请求级消费**的登记口。本门禁守的就是
这个登记口的四条契约 —— 任何一条被拿走，"让错误可见"就退化成"换了个地方静默"：

  ① **绝不抛**：`note()` 在异常消息自身都坏掉（`__str__` 抛）时也不能抛。
     否则"记录失败"会变成"新的失败"，且发生在收尾路径上最难查。
  ② **分级不混**：三级各自落位；非法 level **不得**被静默当 cosmetic 过滤掉
     （写错的 level 恰恰是最该被看见的）。
  ③ **两套消费者不互相替代**：全局聚合（看板）与请求级采集器（会话流）同时拿得到；
     只实现其中一个，另一个消费方就永远看不到。
  ④ **drain 是增量的、且合并后能再交付**：同一 where 第 2 次发生时，
     前端必须能收到 `×2` 的更新 —— 否则用户看到停在 `×1`，与实际不符。

## 变异自证（M 系列：判据必须有牙齿）
  M1  把 `note()` 的全局聚合整段去掉        → G1/G3 必须红
  M2  把 collector 分支去掉                 → C1/C2 必须红
  M3  `normalize_level` 对非法值返回 cosmetic→ L2 必须红
  M4  `Collector.add` 合并时不回退游标       → C4 必须红
"""
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.chdir(ROOT)

import core.degrade as DG  # noqa: E402

PASS, FAIL, SKIPPED = [], [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s%s" % ("PASS" if cond else "FAIL", name,
                         ("  <- " + str(detail)) if detail else ""), flush=True)


class _BadStr(Exception):
    """`__str__` 会抛的异常 —— 真实世界存在（自定义异常持有坏对象时）。"""
    def __str__(self):
        raise RuntimeError("bad __str__")


# ══ ① 基础契约：聚合 + 分级 ═══════════════════════════════════════════
print("[1] 全局聚合与分级", flush=True)
DG.reset()

DG.note("t.critical", RuntimeError("boom"), level="critical", impact="结果不可信")
DG.note("t.degraded", RuntimeError("soft"), level="degraded", impact="少了语义召回")
DG.note("t.cosmetic", RuntimeError("noise"), level="cosmetic")

snap = DG.snapshot()
check("G1 三处均进入全局聚合", snap["total"] == 3,
      "total=%s" % snap["total"])
check("G2 by_level 三级各自落位",
      snap["by_level"].get("critical") == 1
      and snap["by_level"].get("degraded") == 1
      and snap["by_level"].get("cosmetic") == 1,
      repr(snap.get("by_level")))
check("G3 where 明细含 level/impact/error",
      snap["where"].get("t.critical", {}).get("level") == "critical"
      and "不可信" in (snap["where"].get("t.critical", {}).get("impact") or "")
      and "RuntimeError" in (snap["where"].get("t.critical", {}).get("last") or ""),
      repr(snap["where"].get("t.critical")))
check("G4 worst 排行 critical 在最前",
      (snap["worst"] or [{}])[0].get("where") == "t.critical",
      repr([w.get("where") for w in snap["worst"]]))

# 级别只升不降：先 critical 后 cosmetic，仍按 critical 报
DG.reset()
DG.note("t.sticky", RuntimeError("a"), level="critical")
DG.note("t.sticky", RuntimeError("b"), level="cosmetic")
check("G5 同 where 级别只升不降（不被后续 cosmetic 稀释）",
      DG.snapshot()["where"]["t.sticky"]["level"] == "critical",
      DG.snapshot()["where"]["t.sticky"]["level"])

# 非法 level 按 degraded，且**不**被当 cosmetic 过滤
DG.reset()
DG.note("t.badlevel", RuntimeError("x"), level="危重")
snap = DG.snapshot()
check("L1 非法 level 归入 degraded",
      snap["by_level"].get("degraded") == 1,
      repr(snap["by_level"]))
check("L2 非法 level **未**被静默当 cosmetic（写错也要被看见）",
      snap["by_level"].get("cosmetic") == 0,
      repr(snap["by_level"]))

# ══ ② 绝不抛 ═══════════════════════════════════════════════════════════
print("[2] 绝不抛（含 __str__ 崩坏的异常）", flush=True)
DG.reset()
_raised = None
try:
    DG.note("t.badstr", _BadStr(), level="critical", impact="消息提取失败也要计数")
    DG.note("t.none", None, level="degraded", impact="无异常对象的降级（配置缺失走兜底）")
except BaseException as e:      # noqa: BLE001 —— 连 BaseException 都不许漏
    _raised = e
check("S1 note() 对 __str__ 抛异常的 exc 不抛", _raised is None, repr(_raised))
snap = DG.snapshot()
check("S2 坏 __str__ 仍然计了数（不抛 ≠ 不留痕）",
      snap["where"].get("t.badstr", {}).get("count") == 1,
      repr(snap["where"].get("t.badstr")))
check("S3 exc=None 的降级也能登记（非异常降级同样可见）",
      snap["where"].get("t.none", {}).get("count") == 1,
      repr(snap["where"].get("t.none")))

# ══ ③ 请求级采集器 ═════════════════════════════════════════════════════
print("[3] 请求级采集器 + drain 增量", flush=True)
DG.reset()
col = DG.collect()
DG.note("c.one", RuntimeError("1"), level="degraded", impact="影响一")
DG.note("c.two", RuntimeError("2"), level="critical", impact="影响二")

first = DG.drain(col)
check("C1 采集器拿到两条（保序）",
      [e["where"] for e in first] == ["c.one", "c.two"],
      repr([e["where"] for e in first]))
check("C2 drain 是增量的（第二次为空）", DG.drain(col) == [],
      repr(DG.drain(col)))

# 合并后必须能再交付（游标回退）。
# ⚠️ 语义是「从游标整段重发」而不是「只重发被改的那一条」—— 列表有序、游标只能整段走。
#   前端按 where 作 id upsert，重复交付是无害的；**不重新交付才是有害的**（停在 ×1）。
DG.note("c.one", RuntimeError("1 again"), level="degraded")
second = DG.drain(col)
_c1 = [e for e in second if e["where"] == "c.one"]
check("C3 同 where 再次发生 → 合并计数为 2 且**重新交付**",
      len(_c1) == 1 and _c1[0]["count"] == 2,
      repr(second))

# 汇总
DG.note("c.three", RuntimeError("3"), level="cosmetic")
_s = col.summary()
# 计次口径：c.one×2 + c.two×1 + c.three×1 = 4（summary 按**发生次数**汇总，不是条目数）
check("C4 summary 按级别汇总（含 critical 标记）",
      _s["total"] == 4 and _s["critical"] == 1 and _s["by_level"].get("cosmetic") == 1,
      repr(_s))

# 两套消费者同时可见（不是二选一）
check("C5 同一事件既进采集器也进全局聚合（两套消费者不互斥）",
      col.summary()["total"] >= 1 and DG.snapshot()["total"] >= 3,
      "col=%s global=%s" % (col.summary()["total"], DG.snapshot()["total"]))
DG.reset_current()

# 无采集器时 note 不炸、且不进任何采集器
DG.note("c.orphan", RuntimeError("no collector"), level="degraded")
check("C6 无采集器时 note 照常工作（只进全局）",
      DG.snapshot()["where"].get("c.orphan", {}).get("count") == 1,
      repr(DG.snapshot()["where"].get("c.orphan")))

# ══ ④ 容量上限：不无界增长 ═════════════════════════════════════════════
print("[4] 容量与健壮性", flush=True)
DG.reset()
col = DG.collect()
# 用同一个 where 反复触发：既测上限、也测"日志限速不刷屏"（不同 where 才会各打一条）
for i in range(DG._COLLECTOR_CAP + 15):
    DG.note("cap.same", RuntimeError("x"))
_evs = col.all()
check("K1 采集器有上限（不随失败次数无界增长）",
      len(_evs) <= DG._COLLECTOR_CAP,
      "len=%d cap=%d" % (len(_evs), DG._COLLECTOR_CAP))
check("K2 同一 where 重复失败被合并成一条（不是 55 条噪音卡）",
      len(_evs) == 1 and _evs[0]["count"] == DG._COLLECTOR_CAP + 15,
      "len=%d count=%s" % (len(_evs), (_evs[0]["count"] if _evs else None)))
DG.reset_current()

# ══ ⑤ 变异自证 ═════════════════════════════════════════════════════════
print("[5] 变异自证（判据有牙齿）", flush=True)

# M1：去掉全局聚合分支
_orig = DG.note
_src = []


def _note_no_global(where, exc=None, level="degraded", impact="", **ctx):
    """只写采集器、不写全局聚合。"""
    col = DG._CURRENT.get()
    if col is not None:
        col.add(DG.DegradeEvent(where=where, level=DG.normalize_level(level),
                                error=str(exc)[:80] if exc else "", impact=impact))
    return None


DG.reset()
try:
    DG.note = _note_no_global
    DG.note("m1.x", RuntimeError("x"), level="critical")
    _m1_red = DG.snapshot()["total"] == 0
    check("M1 去掉全局聚合 → G1/G3 判据变红（有牙齿）", _m1_red,
          "" if _m1_red else "变异体仍能写入聚合（判据与实现不一致）")
finally:
    DG.note = _orig

# M3：非法 level 归 cosmetic
_orig_norm = DG.normalize_level
try:
    DG.normalize_level = lambda lv: (str(lv).lower()
                                     if str(lv).lower() in DG.LEVEL_RANK else "cosmetic")
    DG.reset()
    DG.note("m3.x", RuntimeError("x"), level="危重")
    _m3_red = DG.snapshot()["by_level"].get("cosmetic") == 1
    check("M3 非法 level 归 cosmetic → L2 判据变红（能抓住静默过滤）", _m3_red,
          "" if _m3_red else "变异体没被抓住（L2 断言口径不一致）")
finally:
    DG.normalize_level = _orig_norm

# M4：合并时不回退游标
_orig_add = DG.Collector.add


def _add_no_rewind(self, ev):
    with self._lock:
        i = self._index.get(ev.where)
        if i is not None:
            old = self._events[i]
            old.count += ev.count
            return
        self._index[ev.where] = len(self._events)
        self._events.append(ev)


DG.reset()
try:
    DG.Collector.add = _add_no_rewind
    col = DG.collect()
    DG.note("m4.x", RuntimeError("1"))
    DG.drain(col)
    DG.note("m4.x", RuntimeError("2"))
    _m4_red = DG.drain(col) == []
    check("M4 合并不回退游标 → C3 判据变红（能抓住「停在×1」）", _m4_red,
          "" if _m4_red else "变异体仍交付了更新（C3 口径与实现不符）")
finally:
    DG.Collector.add = _orig_add
    DG.reset_current()

DG.reset()

print("\n=== %d PASS / %d FAIL / %d SKIP ===" % (len(PASS), len(FAIL), len(SKIPPED)), flush=True)
if SKIPPED:
    print("SKIP 项（未验证，不等于通过）：%s" % ", ".join(SKIPPED), flush=True)
if FAIL:
    print("FAIL: %s" % ", ".join(FAIL), flush=True)
sys.exit(1 if FAIL else 0)
