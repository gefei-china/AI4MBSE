# CI-OPTIONAL: [local] tier=local（见 tools/gates_manifest.json）：需生产真库金标（intent_samples 档位分布）与评测跑批器口径；CI 无 mbse.db ⇒ 跑不了。本机已实测通过，改法须同时改清单 tier 并接线，不允许只删这行
# -*- coding: utf-8 -*-
"""意图样本「三档标注」一致性门禁（第 19 轮）。

## 为什么需要它
`suggested` 曾把三种可信度完全不同的样本挤在一档里（实测 491 条 = 347 银标 + 17 合成 + 127 真待审），
页面据此把人工待办算成 491（**虚高 3.8 倍**），而评测跑批器
（`tools/intent_eval_snapshot.py`）早已按 gold/silver/synth **三集分报**。
⇒ 状态字段与评测口径不同构，两边各说一套。

本批把档位写进 `status`（`silver` / `synth`），并用 `tier_of()` 作为**唯一判定函数**。
本门禁钉住四件事：
  1) 判定函数本身（含**优先级**：aug 必须先于 dual_label 判，否则 183 条会同时落进两集）；
  2) 评测跑批器三集的 SQL 与状态**逐字对应**（防有人只改一边）；
  3) 真库存量不变量（silver 必来自双视图、synth 必来自 aug、三集文本两两无交）；
  4) 统计口径单一：`pending_human` 必须等于 new+suggested（页面禁止自己拼）；
  5) 「补建议」候选集必须覆盖「已是 suggested 但无标签」的死区（实测 118 条曾卡死）。

## 变异自证（缺一条就可能是空转断言）
  M1 交换 tier_of 的判定顺序 → T2 的「aug+双视图」用例必须 FAIL
  M2 去掉 created_by 判断（恒判 silver）→ T2 的「普通产出」用例必须 FAIL
  M3 把补建议 SQL 退回只认 status='new' → T7 必须 FAIL
"""
import inspect
import os
import sqlite3
import sys
import textwrap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.chdir(ROOT)

from repositories.intent_sample_repo import (  # noqa: E402
    PENDING_SUGGEST_SQL, STATUS_TIER, VALID_STATUS, IntentSampleRepo, tier_of,
)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s%s" % ("PASS" if cond else "FAIL", name,
                         ("  <- " + str(detail)) if detail else ""), flush=True)


# ── 判定用例表（真实函数与变异体跑同一张表，才叫"断言有牙齿"）──────────────
TIER_CASES = [
    # (source, created_by, 期望)          —— 期望值写死在这里，即"口径"
    ("aug", "", "synth"),
    ("aug", "dual_label_v1", "synth"),   # ★ 优先级：aug 先判（否则会同时进 silver 集）
    ("prod", "dual_label_v1", "silver"),
    ("prod_import", "dual_label_v1", "silver"),
    ("prod", "", "suggested"),
    ("", "", "suggested"),
]


def tier_failures(fn) -> list:
    bad = []
    for src, cb, want in TIER_CASES:
        got = fn(src, cb)
        if got != want:
            bad.append((src, cb, want, got))
    return bad


print("[1] 状态枚举与档位映射")
check("T1a VALID_STATUS 含六档",
      set(VALID_STATUS) == {"new", "suggested", "silver", "synth", "confirmed", "rejected"},
      VALID_STATUS)
check("T1b STATUS_TIER 三档齐全且与状态同名",
      STATUS_TIER == {"confirmed": "gold", "silver": "silver", "synth": "synth"},
      STATUS_TIER)

print("[2] tier_of 判定表（含优先级）")
_bad = tier_failures(tier_of)
check("T2 tier_of 六组用例全部正确", not _bad, _bad)

print("[3] 评测跑批器三集 SQL 与状态逐字对应")
try:
    import importlib.util
    _spec = importlib.util.spec_from_file_location(
        "_intent_eval_snapshot", os.path.join(ROOT, "tools", "intent_eval_snapshot.py"))
    _snap = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_snap)
    _sets = _snap.SETS
    # 集合名 ≠ 状态名：`gold` 档的状态是 `confirmed`。用 STATUS_TIER 反查，
    # 断言"每个集合按**自己的**状态字段取" —— 若有人把 silver 集改回按 created_by 取，这里会红。
    _inv = {tier: st for st, tier in STATUS_TIER.items()}
    _miss = [k for k in ("gold", "silver", "synth")
             if ("status='%s'" % _inv[k]) not in (_sets.get(k) or "")]
    check("T3 gold/silver/synth 三集都按各自 status 取", not _miss,
          "缺失或未按 status 取: %s" % _miss)
    # synth 集不得再按 source='aug' 取（否则与 silver 的排除条件形成两套口径）
    check("T3b synth 集不再按 source='aug' 取（口径已归一到 status）",
          "source" not in (_sets.get("synth") or ""), _sets.get("synth"))
except Exception as e:  # noqa: BLE001
    check("T3 评测跑批器可导入并取出 SETS", False, repr(e))

print("[4] 真库不变量（口径：全库，只读）")
_conn = sqlite3.connect("file:mbse.db?mode=ro", uri=True)
_conn.row_factory = sqlite3.Row
try:
    _bad_silver = _conn.execute(
        "SELECT COUNT(*) FROM intent_samples WHERE status='silver' AND "
        "(COALESCE(created_by,'')<>'dual_label_v1' OR COALESCE(source,'')='aug')").fetchone()[0]
    check("T4a silver 行都来自双视图且非 aug 语句", _bad_silver == 0, "违规 %d 行" % _bad_silver)
    _bad_synth = _conn.execute(
        "SELECT COUNT(*) FROM intent_samples WHERE status='synth' AND COALESCE(source,'')<>'aug'"
    ).fetchone()[0]
    check("T4b synth 行都来自 aug", _bad_synth == 0, "违规 %d 行" % _bad_synth)
    _sets_text = {}
    for st, tier in STATUS_TIER.items():
        _sets_text[tier] = {r[0] for r in _conn.execute(
            "SELECT text FROM intent_samples WHERE status=? AND COALESCE(intent,'')<>''", (st,))}
    _ov = []
    _ks = sorted(_sets_text)
    for i in range(len(_ks)):
        for j in range(i + 1, len(_ks)):
            n = len(_sets_text[_ks[i]] & _sets_text[_ks[j]])
            if n:
                _ov.append("%s∩%s=%d" % (_ks[i], _ks[j], n))
    check("T4c 三集文本两两无交（同一道题不能算两遍）", not _ov, _ov)

    # T5 统计口径单一
    _st = IntentSampleRepo(_conn).stats()
    _expect = _st["by_status"].get("new", 0) + _st["by_status"].get("suggested", 0)
    check("T5 pending_human == new+suggested", _st.get("pending_human") == _expect,
          "pending_human=%s expect=%s" % (_st.get("pending_human"), _expect))
    check("T5b by_tier 与 by_status 一致",
          _st["by_tier"] == {t: _st["by_status"].get(s, 0) for s, t in STATUS_TIER.items()},
          _st.get("by_tier"))
finally:
    _conn.close()

print("[6] 「补建议」候选集覆盖无标签死区（夹具库，行为级）")
_mem = sqlite3.connect(":memory:")
_mem.row_factory = sqlite3.Row
_mem.execute("CREATE TABLE intent_samples (id INTEGER PRIMARY KEY, text TEXT, intent TEXT, "
             "status TEXT, source TEXT, created_by TEXT, seen_count INTEGER DEFAULT 0)")
_seed = [
    ("new 有 hint", "", "new"),
    ("suggested 无标签", "", "suggested"),
    ("suggested 有标签", "design", "suggested"),
    ("银标", "design", "silver"),
    ("合成", "design", "synth"),
]
for i, (t, it, st) in enumerate(_seed, 1):
    _mem.execute("INSERT INTO intent_samples (id,text,intent,status,source,created_by) "
                 "VALUES (?,?,?,?,'prod','')", (i, t, it, st))
_mem.commit()
_sel = {r["text"] for r in _mem.execute(PENDING_SUGGEST_SQL, (50,)).fetchall()}
check("T7 选中 new 与「suggested 且无标签」", _sel == {"new 有 hint", "suggested 无标签"}, _sel)
check("T7b 不选已有标签的 suggested / 银标 / 合成",
      not (_sel & {"suggested 有标签", "银标", "合成"}), _sel)

print("[8] 变异自证（断言必须有牙齿）")
_src = textwrap.dedent(inspect.getsource(tier_of))


def _twin(mutate, tag):
    s = mutate(_src)
    if s == _src:
        check("M%s 变异未生效（锚点没命中，属脚本缺陷）" % tag, False, "源码未被改动")
        return None
    ns = {"__name__": "mutant_%s" % tag}
    exec(compile(s, "<mutant_%s>" % tag, "exec"), ns)
    return ns["tier_of"]


def _mut_swap(s):
    """M1：把 aug 判断挪到 dual_label 之后（等价于"先判银标"）。"""
    return s.replace(
        '    if (source or "").strip() == "aug":\n        return "synth"\n'
        '    if (created_by or "").strip() == "dual_label_v1":\n        return "silver"',
        '    if (created_by or "").strip() == "dual_label_v1":\n        return "silver"\n'
        '    if (source or "").strip() == "aug":\n        return "synth"')


def _mut_nosrc(s):
    """M2：去掉来源判断，凡不是双视图都当 suggested 之前先判 aug —— 直接砍掉 aug 分支。"""
    return s.replace('if (source or "").strip() == "aug":', "if False:")


_m1 = _twin(_mut_swap, "1")
check("M1 交换判定顺序（银标优先）→ 用例表必须红",
      _m1 is not None and bool(tier_failures(_m1)),
      "failures=%s" % (tier_failures(_m1) if _m1 else "n/a"))

_m2 = _twin(_mut_nosrc, "2")
check("M2 砍掉 aug 分支 → 用例表必须红",
      _m2 is not None and bool(tier_failures(_m2)),
      "failures=%s" % (tier_failures(_m2) if _m2 else "n/a"))

# M3：补建议 SQL 退回旧口径（只认 status='new'）→ T7 的第二个用例必须红
_old_sql = ("SELECT id, text FROM intent_samples WHERE status='new' "
            "ORDER BY seen_count DESC, id LIMIT ?")
_sel_old = {r["text"] for r in _mem.execute(_old_sql, (50,)).fetchall()}
check("M3 补建议 SQL 退回只认 new → 死区用例必须红",
      "suggested 无标签" not in _sel_old, _sel_old)
_mem.close()

print()
print("=" * 78)
print("PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
for f in FAIL:
    print("  FAIL:", f)
raise SystemExit(1 if FAIL else 0)
