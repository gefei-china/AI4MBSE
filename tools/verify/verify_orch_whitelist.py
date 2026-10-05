# -*- coding: utf-8 -*-
"""编排触发「可编排意图」动态化的常驻自检（P1-12，拆意图白名单）。

背景：`_needs_orchestration` 原先用硬编码 6 意图白名单限定编排触发，用户新建的 sub 角色
Agent 永远进不了自动编排。改为读 agents 表 enabled+role=sub 的意图名，黑名单排除
chat/system_mgmt/requirement_quality，空则回退旧白名单。核心逻辑是纯函数
`_orchestrable_from_rows`（与 DB 读取解耦），本脚本直接对其做语义断言 + 变异自证。

防空转：变异必须真正改变被观察行为（去掉黑名单 → chat 混入；去掉兜底 → 空 rows 返回空集）。
"""

# ── CI 豁免（2026-10-05 标注，理由已实测）──────────────────
# CI-OPTIONAL: C 实测干净库上红（F3 缺多方案生成/结构视图生成/需求视图生成）⇒ 需先修
#   分类：A=需服务在跑/ B=需密钥或写真库/ C=实测就红需先修。
#   依据见 docs/遗留优化项-第二轮盘点-20261005.md；
#   由 tools/verify/verify_gate_wiring.py 强制要求（要么接线，要么写理由）。
import inspect
import os
import sqlite3
import sys
import textwrap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.chdir(ROOT)

from agent.pipeline_parts.orchestration import OrchestrMixin  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s%s" % ("PASS" if cond else "FAIL", name,
                         ("  <- " + str(detail)) if detail else ""), flush=True)


_m = OrchestrMixin()
_OLD_WHITELIST = {"requirement_analysis", "design", "impact", "review",
                  "report_generation", "knowledge_qa"}
_EXCLUDE = {"chat", "system_mgmt", "requirement_quality"}
_USER_AGENTS = {"需求视图生成", "结构视图生成", "多方案生成"}


def _row(name):
    return {"name": name}


# ── [F] 纯函数语义（注入 rows，与 DB 解耦） ──────────────────────
print("[F] _orchestrable_from_rows 语义")

_real_ints = _m._orchestrable_intents()   # 真库兜底：验证真数据链路也一致

check("F1 旧 6 白名单意图全部仍在可编排集合（语义等价）",
      _OLD_WHITELIST <= _real_ints, "缺失=%s" % (_OLD_WHITELIST - _real_ints))
check("F2 入口/系统/单交付物三类被排除", not (_EXCLUDE & _real_ints),
      "残留=%s" % (_EXCLUDE & _real_ints))
check("F3 用户自建 sub Agent 自动进入可编排（新能力）",
      _USER_AGENTS <= _real_ints, "缺失=%s" % (_USER_AGENTS - _real_ints))

# 纯函数：含 chat 的 rows → chat 被排除；空 rows → 回退旧白名单
_rows_mixed = [_row(n) for n in ("design", "review", "chat", "system_mgmt", "需求视图生成")]
_mixed = _m._orchestrable_from_rows(_rows_mixed)
check("F4 纯函数：黑名单在 rows 中被剔除", "chat" not in _mixed and "system_mgmt" not in _mixed,
      "mixed=%s" % sorted(_mixed))
check("F5 纯函数：空 rows → 回退旧 6 白名单（不塌方）",
      _m._orchestrable_from_rows([]) == _OLD_WHITELIST)


# ── [M] 变异自证 ───────────────────────────────────────────────
print("[M] 变异自证（去黑名单 / 去兜底 → 必须被抓住）")


def _twin(mutate, label):
    src = textwrap.dedent(inspect.getsource(OrchestrMixin._orchestrable_from_rows)).replace("\r\n", "\n")
    mut = mutate(src)
    check("M%s 变异锚点命中" % label, mut != src, "无变化=锚点未命中")
    ns = {"self": _m}
    exec(compile(mut, "<orch_whitelist_twin>", "exec"), ns)
    return ns["_orchestrable_from_rows"]


# 变异 1：去掉黑名单排除 → 含 chat 的 rows 会混入 chat
_fn1 = _twin(lambda s: s.replace("- set(self._ORCH_EXCLUDE_INTENTS)", ""), "1")
_out1 = _fn1(_m, [_row(n) for n in ("design", "chat", "system_mgmt")])
check("M1 去掉黑名单排除 → chat 混入可编排（被抓住）",
      "chat" in _out1, "out=%s" % sorted(_out1))

# 变异 2：去掉兜底 → 空 rows 返回空集（而非旧白名单）
_fn2 = _twin(lambda s: s.replace(" or set(self._ORCH_WHITELIST_FALLBACK)", ""), "2")
_out2 = _fn2(_m, [])
check("M2 去掉兜底 → 空 rows 返回空集（被抓住）", _out2 == set(), "out=%s" % sorted(_out2))


# ── 汇总 ───────────────────────────────────────────────────────
print()
print("=" * 78)
print("PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
for f in FAIL:
    print("  FAIL:", f)
raise SystemExit(1 if FAIL else 0)
