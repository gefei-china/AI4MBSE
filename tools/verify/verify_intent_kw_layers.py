# -*- coding: utf-8 -*-
"""意图识别「关键词路由只用 DB 层（INTENTS 常量已彻底移除）」的自检（P1-16）。

背景：P1-15 把关键词路由的 builtin 层降级为兜底；P1-16 彻底删除 INTENTS 常量，
关键词路由的**唯一**来源是 agents 表 intent_keywords（db 层），路由完全跟着 Agent 配置走。

本脚本证明：
  1) `_kw_layers` 语义正确（db 非空→只用 db；db 空→返回空层，下沉语义/LLM）；
  2) requirement_quality 新 Agent 经 db 层识别生效；
  3) 内置意图完整性：9 个系统级意图名都在 db 层（防误删内置 agent 静默丢路由）；
  4) 变异自证：撤销 db 优先（→空）或恢复写死 builtin 兜底 都被抓住。
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

# F1: db 空（未 register）→ 返回空层（无关键词信号，下沉语义/LLM），不再有 builtin 兜底
_rt0 = IntentRouter()
check("F1 db 空 → _kw_layers 返回空层（无关键词信号，下沉语义/LLM）",
      _rt0._kw_layers() == [],
      "layers=%s" % _rt0._kw_layers())

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

# F4: 内置意图完整性契约 —— 9 个系统级内置意图名必须都在 db 层注册（agents 表 builtin=1）。
#     若未来有人误删内置 agent 或改名，本断言会红（防「静默丢路由」）。
_BUILTIN_NAMES = {"requirement_analysis", "requirement_quality", "design", "impact",
                  "review", "report_generation", "system_mgmt", "knowledge_qa", "chat"}
_db_names = {r["name"] for r in _ROWS}
_missing = _BUILTIN_NAMES - _db_names
check("F4 内置意图完整性：9 个系统级意图名都在 db 层", not _missing, "缺失=%s" % _missing)


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
    pass


# M1: 撤销 db 优先（`if self._db_intents:` → `if False:`）→ db 非空也走 return []，
#     丢失自建 Agent 关键词（「需求视图生成」只在 db 层）→ 被抓住。
_fn1 = _twin(lambda s: s.replace("if self._db_intents:", "if False:"), "1")
_f1 = _Fake()
_f1._db_intents = {"需求视图生成": ["需求视图"]}
_layers1 = _fn1(_f1)
check("M1 撤销 db 优先 → db 层丢自建 Agent 关键词（被抓住）",
      _layers1 == [], "layers=%s" % _layers1)

# M2: 恢复写死 builtin 兜底（`return []` → `return [("builtin", ...)]`）→ db 空时返回写死层，
#     证明「返回空层」是有效设计而非空转（若被偷偷改回硬编码兜底，本断言会红）。
_fn2 = _twin(lambda s: s.replace("return []", 'return [("builtin", {"design": ["建模"]})]'), "2")
_f2 = _Fake()
_f2._db_intents = {}
_layers2 = _fn2(_f2)
check("M2 恢复写死 builtin 兜底 → db 空时返回写死层（被抓住）",
      [_l[0] for _l in _layers2] == ["builtin"], "layers=%s" % _layers2)


# ── 汇总 ───────────────────────────────────────────────────────
print()
print("=" * 78)
print("PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
for f in FAIL:
    print("  FAIL:", f)
raise SystemExit(1 if FAIL else 0)
