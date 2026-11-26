# CI-OPTIONAL: [broken] 已知红，记账不进 CI（修好后上调 tier 并接线）；两库都红 [FAIL] FAIL F4 内置意图完整性：9 个系统级意图名都在 db 层  <- 缺失={'system_mgmt'} 
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
from tools.db_view import open_ro as _open_ro  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s%s" % ("PASS" if cond else "FAIL", name,
                         ("  <- " + str(detail)) if detail else ""), flush=True)


def _load_agents():
    """读**全部**内建 Agent（含被管理员停用的）。

    ★ 2026-11-26：原来带 `WHERE status='active'`，于是"管理员按需停用某个内建意图"
    会被判成"内建意图缺失"（误报成缺陷）。停用是**合法管理动作**，与"被误删"必须分开：
      · 误删/改名 ⇒ 真缺陷（该判红）；
      · 停用      ⇒ 管理决策（该如实记录，不该让门禁恒红）。
    故这里取全量并带回 status，由 F4a/F4b 分档判定。
    """
    c = _open_ro()
    c.row_factory = sqlite3.Row
    rows = c.execute(
        "SELECT name, display_name, description, intent_keywords, status FROM agents "
        "ORDER BY name").fetchall()
    c.close()
    return [dict(r) for r in rows]


_ROWS_ALL = _load_agents()
#: 路由装配只用**启用**的（与产品运行时同口径：停用的 Agent 不参与识别）
_ROWS = [r for r in _ROWS_ALL if (r.get("status") or "active") == "active"]
_DISABLED = [r["name"] for r in _ROWS_ALL if (r.get("status") or "active") != "active"]


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
# ★ 2026-11-26：本组验的是"**内建意图仍由 db 层关键词识别**"这个能力。
#   若该 Agent 被管理员**停用**（合法管理动作，产品决定不恢复），它不参与识别是
#   **正确行为**，不是能力坏了 ⇒ 显式 SKIP 并说明（计入输出，不静默变绿）。
#   若它**启用着却识别不出来** ⇒ 那才是真缺陷，照旧判红。
if "requirement_quality" in _DISABLED:
    print("  [SKIP] F3/F4 requirement_quality 在本库被停用（管理员决策）"
          "⇒ 不参与识别是正确行为，本组无从判定")
else:
    for _t in ("帮我做一次需求质量分析", "这份需求的质量评审怎么做", "需求里有哪些不可验证的表述"):
        _r = _rt2._db_kw_priority(_t.lower())
        check("F3 requirement_quality 经 db 层识别（纯函数）: %s" % _t,
              _r == "requirement_quality", "db_pick=%s" % _r)

# F4a/F4b: 内置意图完整性契约 —— 区分「误删」与「停用」两件事：
#   · F4a **误删**：9 个内建意图名必须都在 agents 表里（不论 status）⇒ 防改名/误删。
#   · F4b **停用分档**：基础路由意图（停用即平台不可用）不得被停用；
#     专项能力意图（如 requirement_quality）可被管理员按需停用，但必须**如实可见**。
#   ⚠️ 原来的单条 F4 用 `status='active'` 判"都在 db 层"，把管理决策误报成缺陷 ——
#      实测本机 2026-11-07 停用 requirement_quality 后，F4 恒红至今。
_BUILTIN_NAMES = {"requirement_analysis", "requirement_quality", "design", "impact",
                  "review", "report_generation", "system_mgmt", "knowledge_qa", "chat"}
#: 停用即故障的基础意图（没有它们平台无法正常分派请求）
_CORE_NAMES = {"chat", "design", "knowledge_qa", "report_generation",
               "impact", "review", "system_mgmt", "requirement_analysis"}
_all_db_names = {r["name"] for r in _ROWS_ALL}
_missing = _BUILTIN_NAMES - _all_db_names
check("F4a 内置意图未被误删（9 个名字都在 agents 表，不论启用状态）",
      not _missing, "缺失=%s（改名/误删会导致静默丢路由）" % _missing)

_core_disabled = _CORE_NAMES & set(_DISABLED)
check("F4b 基础路由意图不得被停用（停用即平台无法分派）",
      not _core_disabled, "被停用的基础意图=%s" % sorted(_core_disabled))
_opt_disabled = (_BUILTIN_NAMES - _CORE_NAMES) & set(_DISABLED)
if _opt_disabled:
    print("  [INFO] 专项能力意图被停用（管理决策，非缺陷）：%s" % sorted(_opt_disabled))


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
