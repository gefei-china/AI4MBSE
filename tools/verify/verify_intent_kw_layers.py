# -*- coding: utf-8 -*-
"""意图识别「builtin INTENTS 层降级为兜底」的自检（P1-15，移除写死意图）。

背景：P1-14 把 INTENTS 硬编码关键词合并进 DB 内置 Agent，本批补上唯一缺失的
requirement_quality Agent 后，DB 层已完整覆盖 builtin 层。于是把关键词路由的
builtin 层从「并列参与竞争」降级为「DB 空时才兜底」——路由完全跟着 Agent 配置走，
硬编码 INTENTS 不再是路由主依据。

本脚本证明：
  1) `_kw_layers` 降级语义正确（db 非空→只用 db；db 空→builtin 兜底）；
  2) requirement_quality 新 Agent 经 db 层识别生效；
  3) 数据契约：DB 层关键词 ⊇ INTENTS 词表（降级安全的前提，可失效）；
  4) 变异自证：撤销 db 优先（→builtin）或撤销兜底（→空）都被抓住。
"""
import inspect
import json
import os
import sqlite3
import sys
import textwrap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.chdir(ROOT)

from agent.intent import IntentRouter  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s%s" % ("PASS" if cond else "FAIL", name,
                         ("  <- " + str(detail)) if detail else ""), flush=True)


def _load_agents():
    c = sqlite3.connect("file:mbse.db?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    rows = c.execute(
        "SELECT name, display_name, description, intent_keywords FROM agents "
        "WHERE status='active' ORDER BY name").fetchall()
    c.close()
    return [dict(r) for r in rows]


_ROWS = _load_agents()


def make_router():
    rt = IntentRouter()
    sem_idx = []
    for r in _ROWS:
        kws = json.loads(r["intent_keywords"] or "[]")
        rt.register_keywords(r["name"], kws)
        sem_idx.append({"name": r["name"],
                        "text": f"{r['display_name'] or r['name']} {r['description'] or ''} {' '.join(kws)}"})
    rt.set_semantic_index(sem_idx)
    return rt


# ── [F] 降级语义 ───────────────────────────────────────────────
print("[F] builtin 层降级为兜底")

# F1: db 空（未 register）→ builtin 兜底，行为不塌方
_rt0 = IntentRouter()
check("F1 db 空 → _kw_layers 回退 builtin 兜底",
      [_l[0] for _l in _rt0._kw_layers()] == ["builtin"],
      "layers=%s" % [_l[0] for _l in _rt0._kw_layers()])

# F2: db 非空 → 只用 db 层，builtin 不再参与
_rt1 = make_router()
check("F2 db 非空 → _kw_layers 只用 db 层（builtin 退出竞争）",
      [_l[0] for _l in _rt1._kw_layers()] == ["db"],
      "layers=%s" % [_l[0] for _l in _rt1._kw_layers()])

# F3: requirement_quality 新 Agent（本批补）经 db 层识别。
# ⚠️ 直接测纯函数 `_db_kw_priority`（只走 db 层关键词），不走 detect —— intent_cache 是
#    持久化表，detect 会命中旧缓存（route=cache），只能证明「缓存里存了」而非「db 层识别了」。
_rt2 = make_router()
for _t in ("帮我做一次需求质量分析", "这份需求的质量评审怎么做", "需求里有哪些不可验证的表述"):
    _r = _rt2._db_kw_priority(_t.lower())
    check("F3 requirement_quality 经 db 层识别（纯函数）: %s" % _t,
          _r == "requirement_quality", "db_pick=%s" % _r)

# F4: 数据契约 —— DB 层关键词 ⊇ INTENTS 词表（除 chat）。这是降级安全的**前提**：
#     若未来有人在 INTENTS 加新词却不同步 agent，本断言会红（防「静默丢路由」）。
_db_kw = {}
for r in _ROWS:
    _db_kw[r["name"]] = set(k.lower() for k in (json.loads(r["intent_keywords"] or "[]")))
_missing = {}
for _intent, _kw in IntentRouter.INTENTS.items():
    if _intent == "chat":
        continue
    _diff = set(_kw) - _db_kw.get(_intent, set())
    if _diff:
        _missing[_intent] = sorted(_diff)
check("F4 数据契约：DB 层关键词 ⊇ INTENTS（除 chat），无缺失",
      not _missing, "缺失=%s" % _missing)


# ── [M] 变异自证（exec 孪生体）─────────────────────────────────
print("[M] _kw_layers 两个分支非空转")

_src = textwrap.dedent(inspect.getsource(IntentRouter._kw_layers)).replace("\r\n", "\n")


def _twin(mutate, label):
    mut = mutate(_src)
    check("M%s 变异锚点命中" % label, mut != _src, "未命中")
    ns = {}
    exec(compile(mut, "<kw_layers_twin>", "exec"), ns)
    return ns["_kw_layers"]


class _Fake:
    INTENTS = {"design": ["建模"], "chat": []}


# M1: 撤销 db 优先（`if self._db_intents:` → `if False:`）→ db 非空也走 builtin，
#     丢失自建 Agent 关键词（「需求视图生成」只在 db 层）→ 被抓住。
_fn1 = _twin(lambda s: s.replace("if self._db_intents:", "if False:"), "1")
_f1 = _Fake()
_f1._db_intents = {"需求视图生成": ["需求视图"]}
_layers1 = _fn1(_f1)
check("M1 撤销 db 优先 → builtin 层丢自建 Agent 关键词（被抓住）",
      [_l[0] for _l in _layers1] == ["builtin"],
      "layers=%s" % [_l[0] for _l in _layers1])

# M2: 撤销兜底（`return [("builtin", self.INTENTS)]` → `return []`）→ db 空时返回空，
#     关键词层无信号下沉（而非用 builtin 兜底），兜底失效被抓住。
_fn2 = _twin(lambda s: s.replace('return [("builtin", self.INTENTS)]', "return []"), "2")
_f2 = _Fake()
_f2._db_intents = {}
_layers2 = _fn2(_f2)
check("M2 撤销兜底 → db 空时返回空（兜底失效被抓住）",
      _layers2 == [], "layers=%s" % _layers2)


# ── 汇总 ───────────────────────────────────────────────────────
print()
print("=" * 78)
print("PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
for f in FAIL:
    print("  FAIL:", f)
raise SystemExit(1 if FAIL else 0)
