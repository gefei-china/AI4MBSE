# -*- coding: utf-8 -*-
"""estimated_cost 缓存折扣计价自检（P1-21）。

背景：此前 `estimated_cost` 把 `prompt_tokens` 全按 input 混合价估算，
**缓存折扣被抹平**——命中越多，成本数字虚高越严重。P1-21 改为区分两种单价：
  - provider 返回 hit/miss → `miss(及余量) * input价 + hit * cache_hit价 + ct * output价`
  - 未返回（hit=miss=0，含 Mock / 无 usage）→ 退回原式（**零行为漂移**）

本脚本证明：
  1) 有缓存字段时按折扣计价，且严格低于混合价（折扣真的生效）；
  2) 无缓存字段时与原式逐位一致（老路径零漂移）；
  3) 异常余量（hit+miss > pt）兜底不为负、不丢 token；
  4) 变异自证：把 cache_hit 单价改回 input（取消折扣）→ F1 目标断言被抓住。
"""
import os
import sqlite3
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from llm import llm_client, LLMClient  # noqa: E402

PASS, FAIL = [], []
MARK = "__cost_price_test__"
PROVIDER = {"id": 71, "name": "price-test", "model_name": "deepseek-v4-flash"}


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s%s" % ("PASS" if cond else "FAIL", name,
                         ("  <- " + str(detail)) if detail else ""), flush=True)


def _cost_of(pt=0, ct=0, hit=0, miss=0):
    """跑一次 _record_usage，读回 estimated_cost（随后清理，零残留）。"""
    usage = {"prompt_tokens": pt, "completion_tokens": ct}
    if hit or miss:
        usage["prompt_cache_hit_tokens"] = hit
        usage["prompt_cache_miss_tokens"] = miss
    resp = {"usage": usage, "choices": [{"message": {"content": "x"}}]}
    llm_client._record_usage(PROVIDER, resp, False, MARK, 0)
    c = sqlite3.connect("mbse.db")
    c.row_factory = sqlite3.Row
    r = c.execute("SELECT estimated_cost, prompt_tokens, prompt_cache_hit_tokens, "
                  "prompt_cache_miss_tokens FROM llm_usage_stats WHERE intent=? "
                  "ORDER BY id DESC LIMIT 1", (MARK,)).fetchone()
    c.execute("DELETE FROM llm_usage_stats WHERE intent=?", (MARK,))
    c.commit()
    c.close()
    return float(r["estimated_cost"] or 0)


PI = LLMClient.PRICE_PER_1M["input"]
PO = LLMClient.PRICE_PER_1M["output"]
PC = LLMClient.PRICE_PER_1M["cache_hit"]

# ── F1：有缓存字段 → 按折扣计价，且低于混合价 ──────────────────────────
_hit, _miss, _ct = 800, 200, 200
_c_disc = _cost_of(pt=_hit + _miss, ct=_ct, hit=_hit, miss=_miss)
_c_mix = (_hit + _miss) * PI + _ct * PO          # 混合价（改动前口径）
_c_expect = (_miss * PI + _hit * PC + _ct * PO) / 1_000_000
check("F1 有缓存字段 → 按折扣计价（= miss*input + hit*cache_hit + ct*output）",
      abs(_c_disc - _c_expect) < 1e-9, "got=%.9f expect=%.9f" % (_c_disc, _c_expect))
check("F2 折扣后成本严格低于混合价（缓存收益真实反映，非虚高）",
      _c_disc < _c_mix / 1_000_000,
      "折扣=%.9f 混合=%.9f 省 %.1f%%" % (_c_disc, _c_mix / 1_000_000,
                                        (1 - _c_disc / (_c_mix / 1_000_000)) * 100))

# ── F3：无缓存字段 → 原式，零漂移 ──────────────────────────────────────
_c_plain = _cost_of(pt=1000, ct=200)
_c_legacy = (1000 * PI + 200 * PO) / 1_000_000
check("F3 无缓存字段（hit=miss=0）→ 退回原式，零漂移",
      abs(_c_plain - _c_legacy) < 1e-9, "got=%.9f expect=%.9f" % (_c_plain, _c_legacy))

# ── F4：异常余量（hit+miss 超过 pt）兜底：不为负、按 input 价补齐 ────────
_c_odd = _cost_of(pt=1000, ct=0, hit=900, miss=300)   # 900+300=1200 > pt=1000
check("F4 异常余量（hit+miss > pt）→ 成本不为负且不为 0（按 input 价兜底，不丢 token）",
      _c_odd > 0, "got=%.9f" % _c_odd)

# ── M1 变异：取消折扣（cache_hit 单价改回 input）→ F1 应被打破 ──────────
_orig_pc = LLMClient.PRICE_PER_1M["cache_hit"]
LLMClient.PRICE_PER_1M["cache_hit"] = PI
try:
    _c_mut = _cost_of(pt=_hit + _miss, ct=_ct, hit=_hit, miss=_miss)
finally:
    LLMClient.PRICE_PER_1M["cache_hit"] = _orig_pc
check("M1 取消折扣（cache_hit 改回 input 价）→ 成本回到混合价，F2 判据被打破",
      abs(_c_mut - _c_mix / 1_000_000) < 1e-9, "mut=%.9f mix=%.9f" % (_c_mut, _c_mix / 1_000_000))

print()
print("PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
for f in FAIL:
    print("  FAIL:", f)
raise SystemExit(1 if FAIL else 0)
