# -*- coding: utf-8 -*-
"""P2 门禁（2026-11-11）：衰减 / 显著性软引导 / A/B 度量。

  R1 sweep 纯只读，不改任何表
  R2 归档必须显式 confirm，否则一行不动
  R3 归档后再移除，`restore()` 能还原原字段（L1 ground truth 不丢）
  R4 软引导默认开启，目标式 prompt 不含「一律 worth=false」；关开关可回滚
  R5 compare_packages 四项齐全，未实测项保持 None（不拿 0 冒充"没变化"）
  M1~M4 变异自证：去 confirm / 跳过归档直接删 / 恒用硬 prompt / None 填 0

⚠️ 这里刻意**不**验证"超龄流水被自动删除"：Anthropic 原文第一条法则是
   永远保留原始底账，那是 Dreaming 唯一的自愈抓手。本仓只做可恢复的冷归档。
"""
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("[OK  ] " if cond else "[FAIL] ") + name +
          ("  — " + str(detail) if detail and not cond else ""), flush=True)


def load(path, mutate=None, name="mut"):
    with open(os.path.join(ROOT, path), encoding="utf-8") as f:
        src = f.read()
    if mutate:
        src = mutate(src)
    ns = {"__name__": name}
    exec(compile(src, path, "exec"), ns)
    return ns


def fixture():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE tool_result_offloads (
            id INTEGER PRIMARY KEY, conversation_id INTEGER, tool_name TEXT,
            content TEXT, created_at TEXT);
        CREATE TABLE orch_stream_events (
            id INTEGER PRIMARY KEY, conversation_id INTEGER, run_id TEXT,
            task_key TEXT, event_type TEXT, payload_json TEXT, created_at TEXT);
    """)
    conn.execute("INSERT INTO tool_result_offloads VALUES (1,1,'t','完整工具结果 A','2020-01-01 00:00:00')")
    conn.execute("INSERT INTO orch_stream_events VALUES (2,1,'r1','k1','ev','{\"x\":1}','2020-01-02 00:00:00')")
    conn.execute("INSERT INTO tool_result_offloads VALUES (3,1,'t','近期结果 B',datetime('now'))")
    conn.commit()
    return conn


def counts(conn):
    return (conn.execute("SELECT COUNT(*) FROM tool_result_offloads").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM orch_stream_events").fetchone()[0])


RET = load("core/memory_retention.py", name="memory_retention_mut")
print("\n[1] 只读盘点 / HIL / 可恢复归档", flush=True)
conn = fixture()
try:
    before = counts(conn)
    plan = RET["sweep"](conn, older_than_days=90)
    r1 = counts(conn) == before and plan["dry_run"] is True
    check("R1 sweep 纯只读且标为 dry_run", r1,
          "before=%r after=%r" % (before, counts(conn)))
    check("R1b 盘点只认超龄行（2 条，近期行不动）",
          plan["total"] == 2 and plan["tables"]["tool_result_offloads"]["sample_ids"] == [1],
          "实得 %r" % {k: v["sample_ids"] for k, v in plan["tables"].items()})
    r2 = RET["archive_apply"](conn, plan)
    check("R2 无 confirm ⇒ 拒绝且一行不动",
          r2["ok"] is False and r2["removed"] == 0 and counts(conn) == before,
          "实得 %r" % r2)
    r3 = RET["archive_apply"](conn, plan, confirm=True)
    archived = RET["restore"](conn, "tool_result_offloads", 1)
    check("R3 归档后移除，且 restore 能还原原字段",
          r3["ok"] and r3["archived"] == 2 and r3["removed"] == 2
          and archived.get("content") == "完整工具结果 A"
          and counts(conn) == (before[0] - 1, before[1] - 1),
          "实得 %r / restore=%r" % (r3, archived))
finally:
    conn.close()

print("\n[1b] 既有 TTL 已改为「先归档再删」（整合验证）", flush=True)
# 背景（2026-11-11 真机核查抓到的判断错误）：`main.py` 每次启动都调
# `tool_offload.cleanup_expired(days=30)`，它一直是**裸 DELETE** ——
# 也就是说"给 L1 加 TTL 会丢自愈抓手"这个担心，现实里 TTL 早就在跑、
# 证据早就在丢。故本项断言的是**既有删除路径已带上归档**，而不是新增调度。
_conn2 = fixture()
try:
    from core.memory_retention import archive_then_delete_expired as _arch
    _calls = []

    class _Spy:
        """把 conn 包一层，记录是否调过归档（判"先归档"还是"裸删"）。"""
        def __init__(self, c):
            self._c = c

        def execute(self, sql, *a, **k):
            _calls.append(sql)
            return self._c.execute(sql, *a, **k)

        def commit(self):
            return self._c.commit()

        def rollback(self):
            return self._c.rollback()

        def __getattr__(self, n):
            return getattr(self._c, n)

    r_arch = _arch(_Spy(_conn2), table="tool_result_offloads", days=30)
    check("R6 归档路径在删除前写入 transcript_archive",
          any("INSERT OR IGNORE INTO transcript_archive" in s for s in _calls)
          and any("DELETE FROM tool_result_offloads" in s for s in _calls)
          and r_arch.get("removed") == 1,
          "实得 removed=%s / traced=%r" % (r_arch.get("removed"),
                                           [s.split()[0] for s in _calls][:6]))
    check("R6b 归档后该行可 restore 还原",
          RET["restore"](_conn2, "tool_result_offloads", 1).get("content") == "完整工具结果 A")
finally:
    _conn2.close()

print("\n[2] 显著性软引导（对强模型给目标、不给死规则）", flush=True)
from core import config as _cfg
from memory_service import MemoryService
check("R4a 配置键在 config 声明且默认开启",
      "salience_soft_guidance" in _cfg.DEFAULT_CONFIG["memory"]
      and _cfg.DEFAULT_CONFIG["memory"]["salience_soft_guidance"] is True)
soft = MemoryService._deposit_prompt()
check("R4b 默认 prompt 是目标式，不含硬清单",
      "未来会话" in soft and "一律 worth=false" not in soft,
      "实得 %r" % soft[:100])
_cfg._CONFIG.setdefault("memory", {})["salience_soft_guidance"] = False
hard = MemoryService._deposit_prompt()
_cfg._CONFIG["memory"]["salience_soft_guidance"] = True
check("R4c 关开关可回滚到旧硬清单", "一律 worth=false" in hard)
check("R4d 规则降级 `_extract_worthy` 的硬门保持不变（安全底线不交给模型）",
      MemoryService._extract_worthy("你好，我是 MBSE 平台的通用助手", 10)[0] is False)

print("\n[3] A/B 度量（不臆造未测指标）", flush=True)
DREAM = load("core/memory_dream.py", name="memory_dream_mut")
p1 = {"candidates": [], "index": {"text": "x"}}
p2 = {"candidates": [{"state": "contradicted"}], "index": {"text": "xy"}}
d = DREAM["compare_packages"](p1, p2)
check("R5 四项指标齐全且未实测项为 None",
      all(k in d for k in ("memory_hit_rate", "fact_conflict_rate",
                           "tokens_per_question", "injected_chars"))
      and d["memory_hit_rate"] is None and d["tokens_per_question"] is None,
      "实得 %r" % d)

print("\n[4] 变异自证", flush=True)


def _m1(src):
    a = "    if not confirm:"
    assert a in src, "M1 锚点失效"
    return src.replace(a, "    if False:", 1)


def _m2(src):
    a = '                conn.execute(\n                    "INSERT OR IGNORE INTO %s (kind, ref_id, payload) VALUES (?,?,?)" %\n                    _ARCHIVE,\n                    (table, rid, json.dumps(payload, ensure_ascii=False, default=str)))'
    assert a in src, "M2 锚点失效"
    return src.replace(a, '                pass  # 变异：跳过归档', 1)


def _m3(src):
    a = "        return (MemoryService._DEPOSIT_SOFT_PROMPT if soft"
    assert a in src, "M3 锚点失效"
    return src.replace(a, "        return (MemoryService._DEPOSIT_HARD_PROMPT if False", 1)


def _m4(src):
    a = "                  else None)"
    assert a in src, "M4 锚点失效"
    return src.replace(a, "                  else 0)", 1)


for label, path, mut, run in (
    ("M1 去掉 confirm ⇒ R2 翻红", "core/memory_retention.py", _m1,
     lambda ns: (lambda c: (lambda r: (not r["ok"]) and r["removed"] == 0)(
         ns["archive_apply"](c, ns["sweep"](c, older_than_days=90))))(fixture())),
    ("M2 跳过归档直接删 ⇒ R3 翻红", "core/memory_retention.py", _m2,
     lambda ns: (lambda c: (lambda r: r["ok"] and
         ns["restore"](c, "tool_result_offloads", 1).get("content") == "完整工具结果 A")(
         ns["archive_apply"](c, ns["sweep"](c, older_than_days=90), confirm=True)))(fixture())),
    ("M3 恒用硬 prompt ⇒ R4 翻红", "services/memory_service.py", _m3,
     lambda ns: "一律 worth=false" not in ns["MemoryService"]._deposit_prompt()),
    ("M4 未实测项填 0 ⇒ R5 翻红", "core/memory_dream.py", _m4,
     lambda ns: ns["compare_packages"](p1, p2)["memory_hit_rate"] is None),
):
    try:
        ns = load(path, mut, name="mut_" + os.path.basename(path))
        still_green = run(ns)
        check(label, still_green is False,
              "变异后判据仍绿 ⇒ 抓不住这个回归")
    except AssertionError as e:
        check(label, False, str(e))
    except Exception as e:  # noqa: BLE001
        check(label, False, "%s: %s" % (type(e).__name__, e))

print("\n=== %d PASS / %d FAIL ===" % (len(PASS), len(FAIL)), flush=True)
for f in FAIL:
    print("  x %s" % f, flush=True)
sys.exit(1 if FAIL else 0)
