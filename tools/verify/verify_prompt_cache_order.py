#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""P4 门禁：动态区**缓存友好排序**契约（2026-10-26）。

## 为什么需要这道门禁

`common.py:228` 自己声明了排序原则：

> 分界线以下的块 key（动态区）。元组顺序 = 实际拼装顺序（**层内按「依赖的易变度」升序**，
> 让相对稳定的块尽量靠前）。

但**实现与声明不一致**：`skill_prompt` 依赖 `user_input`（每轮可能变），却排在第 3 位，
把其后所有块（`memory` / `model_context` / `project_memory` / `slots` / `user_ctx` …）
一起拖成缓存 miss。

实测（生产库 conv=9571，8 个不同 query，逐块 md5）：
  · 静态区 8 块 **全部恒定**
  · 动态区 **只有 `skill_prompt` 会变**（4/8 唯一值，6/8 非空，均 1,016 tok）
  · 其余动态块（`memory` 80 tok / `model_context` 226 tok / `retrieval` 11 tok）**逐个恒定**
⇒ 当前唯一的缓存断点就是 `skill_prompt` 的位置。

DeepSeek / Anthropic 的 prompt cache 都是**前缀完整匹配**：断点之后全部不计命中。
把 `skill_prompt` 押后，其前面的 `memory`(80) + `model_context`(226) 等就恢复可缓存。

## 判定口径（三条，都可被变异证伪）

  [A] 静态区仍全部在动态区之前（继承 `verify_prompt_layering` 的 A3，此处复核防回归）
  [B] **易变块必须排在稳定块之后** —— 这是本门禁的核心契约
  [C] 实测：`skill_prompt` 之前的**恒定前缀 token** 必须 ≥ 阈值（默认 300）
      阈值依据：`memory`(80) + `model_context`(226) ≈ 306 tok 是从"白扔"里抢回来的
      最小可观量；低于它就说明排序又退回去了。

## 变异自证

把 `PROMPT_DYNAMIC_KEYS` 里 `skill_prompt` 挪回第 3 位，[B] 和 [C] 必须同时翻红。
若仍绿，说明本门禁在空转。
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_FAILS = []


def check(name, ok, detail=""):
    if ok:
        print(f"[OK] {name}" + (f"  — {detail}" if detail else ""))
    else:
        print(f"[FAIL] {name}" + (f"  — {detail}" if detail else ""))
        _FAILS.append(name)


from agent.pipeline_parts.common import (                       # noqa: E402
    PROMPT_BLOCK_ORDER, PROMPT_CACHEABLE_KEYS, PROMPT_DYNAMIC_KEYS,
)

#: 「每轮可能变」的块 —— 必须排在稳定块之后。
#: 逐个依据（全部来自实测，不是直觉）：
#:   skill_prompt  依赖 user_input，8 query 里 4 个唯一值（**唯一真实断点**）
#:   skill_block   依赖本轮显式 skill_name
#:   slots         本轮 LLM 产出（任务拆解）
#:   attachment    本轮附件（换附件就变）
#:   retrieval     本轮检索结果（每轮检索必然不同）
#:   report        本轮报告模板
#:   template      提示词实验室模板（intent + user_input）
_VOLATILE = ("skill_prompt", "skill_block", "slots", "attachment",
             "retrieval", "report", "template")

#: 「相对稳定」的块 —— 应在易变块之前。
#:   intent_line   依赖 intent（同会话内稳定）
#:   model_code_req 依赖 intent
#:   memory        core 层无条件常驻，实测跨 query 恒定
#:   model_context 依赖 branch + conversation（同会话稳定）
#:   project_memory 依赖 user_input + conversation
#:   user_ctx      用户上下文
_STABLE = ("intent_line", "model_code_req", "memory", "model_context",
           "project_memory", "user_ctx")


def _idx(key):
    return list(PROMPT_BLOCK_ORDER).index(key)


def _would_violate(seq):
    """返回该序列中「排在某个稳定块之前」的易变块列表（供 [B]/[D] 共用）。"""
    viol = []
    for v in _VOLATILE:
        if v not in seq:
            continue
        for st in _STABLE:
            if st not in seq:
                continue
            if list(seq).index(v) < list(seq).index(st):
                viol.append(v)
                break
    return viol


# ══════════════════════════════════════════════════════════════
# [A] 静态区仍全部在前
# ══════════════════════════════════════════════════════════════
_ord = list(PROMPT_BLOCK_ORDER)
_last_cache = max(_idx(k) for k in PROMPT_CACHEABLE_KEYS)
_first_dyn = min(_idx(k) for k in PROMPT_DYNAMIC_KEYS)
check("[A] 静态区全部在动态区之前",
      _last_cache < _first_dyn,
      f"last_cache={_last_cache} first_dyn={_first_dyn}")

# ══════════════════════════════════════════════════════════════
# [B] 易变块排在稳定块之后（核心契约）
# ══════════════════════════════════════════════════════════════
_violations = []
for _v in _VOLATILE:
    if _v not in PROMPT_DYNAMIC_KEYS:
        continue
    for _st in _STABLE:
        if _st not in PROMPT_DYNAMIC_KEYS:
            continue
        if _idx(_v) < _idx(_st):
            _violations.append(f"{_v}(#{_idx(_v)}) 排在 {_st}(#{_idx(_st)}) 之前")

check("[B] 7 个易变块均排在 6 个稳定块之后", not _violations,
      "；".join(_violations[:3]) if _violations
      else f"动态区顺序 = {list(PROMPT_DYNAMIC_KEYS)}")

# 明确点出 skill_prompt 的最小位置要求（它是实测唯一断点，最容易被误挪回前面）
if "skill_prompt" in PROMPT_DYNAMIC_KEYS and "model_context" in PROMPT_DYNAMIC_KEYS:
    check("[B2] skill_prompt 在 model_context 之后（实测唯一缓存断点必须押后）",
          _idx("skill_prompt") > _idx("model_context"),
          f"skill_prompt=#{_idx('skill_prompt')} model_context=#{_idx('model_context')}")

# ══════════════════════════════════════════════════════════════
# [C] 实测：skill_prompt 之前的恒定前缀 token
# ══════════════════════════════════════════════════════════════
try:
    import hashlib
    from collections import defaultdict

    os.environ.setdefault("MBSE_LLM_FORCE_MOCK", "1")
    from database import get_db                                  # noqa: E402
    from agent import AgentPipeline                              # noqa: E402
    from agent.pipeline_parts.common import (                    # noqa: E402
        build_prompt_blocks, PromptBlocksCtx,
    )
    from core.token_counter import count_tokens                  # noqa: E402

    _USER = {"id": 1, "username": "m", "display_name": "M",
             "role_name": "admin", "role": "admin"}
    _QS = ["生成活动图", "再生成一个状态机视图", "校验一下这段代码", "什么是SysML v2",
           "需求追溯矩阵怎么做", "帮我分析需求变更影响", "生成参数图", "这个模型有什么问题"]

    _pipe = AgentPipeline()
    _c = get_db()
    try:
        _pipe.registry.load_from_db(_c, _USER)
    finally:
        _c.close()

    _hash = defaultdict(list)
    _tok = defaultdict(list)
    for _q in _QS:
        _ctx = PromptBlocksCtx(
            agent_def=_pipe.registry.get("chat"), intent="chat", hil_level=0,
            user_input=_q, user=_USER, branch=None, conversation_id=9571, slots=None,
            user_ctx="", att_text="", context_text="", report_prompt="",
            role_cap=0, include_tool_rules=False)
        _b = build_prompt_blocks(_pipe, _ctx)
        for _k in PROMPT_BLOCK_ORDER:
            _val = _b.get(_k) or ""
            _hash[_k].append(hashlib.md5(_val.encode()).hexdigest())
            _tok[_k].append(count_tokens(_val))

    # 只统计**动态区内** skill_prompt 之前的恒定块 —— 静态区本来就恒定（A 判据已保证
    # 它们在最前），把它算进来会让本判据在任何排序下都通过，等于空转。
    # 真正体现"押后收益"的是：动态区里排在 skill_prompt 之前、且实测恒定的块
    # （memory / model_context …）—— 它们此前被 skill_prompt 挡在断点之后，白扔。
    _sp = _idx("skill_prompt")
    _dyn_start = _idx(PROMPT_DYNAMIC_KEYS[0])
    _prefix_stable_tok = 0
    _prefix_facts = []
    for _k in _ord[_dyn_start + 1:_sp]:
        _uniq = len(set(_hash[_k]))
        _avg = sum(_tok[_k]) // max(len(_tok[_k]), 1)
        if _uniq == 1 and _avg > 0:          # 恒定且有内容 ⇒ 真正可复用
            _prefix_stable_tok += _avg
            _prefix_facts.append(f"{_k}={_avg}")
    _total = sum(sum(v) // max(len(v), 1) for v in _tok.values())

    # 阈值 200 的依据：`model_context` 实测 226 tok，单它一项就够；`memory`(80) 在
    # 无 core 层记忆的场景可能为 0。取 200 既能抓到"skill_prompt 排第一"（实得 0），
    # 又不会因 memory 偶发为空而误报。若日后稳定块变小导致误报，**应调整本阈值或
    # 检查块是否真的变空，绝不允许把 skill_prompt 挪回前面来凑数**。
    check("[C] 动态区内 skill_prompt 之前的恒定块 ≥ 200 tok",
          _prefix_stable_tok >= 200,
          f"实得 {_prefix_stable_tok} tok（{','.join(_prefix_facts) or '无'}）"
          f" / system 合计 {_total} tok")

    # 记录：skill_prompt 实测确实是易变块（哪怕将来它变稳定，这条也只是提示不判失败）
    _sp_uniq = len(set(_hash["skill_prompt"]))
    print(f"   实测 skill_prompt 唯一值 {_sp_uniq}/{len(_QS)}"
          f"（>{1} 说明它确实是断点，押后有效）")
except Exception as _e:
    check("[C] 实测恒定前缀（需真实库）", False, f"{type(_e).__name__}: {_e}")

# ══════════════════════════════════════════════════════════════
# [D] 变异自证：[B] 的判据必须能抓到违规
#     ⚠️ 必须用**构造的合规基准**做变异，不能拿"当前实际顺序"当基准 ——
#        当前若已违规，_before 非空会让判据以"本身就坏"为由失败，
#        那样证明不了判据有效，只证明了现状有问题（两者要分开）。
# ══════════════════════════════════════════════════════════════
_good = [k for k in PROMPT_DYNAMIC_KEYS if k in _STABLE] + \
        [k for k in PROMPT_DYNAMIC_KEYS if k in _VOLATILE]
_bad = list(_good)
# 把 skill_prompt 挪到最前（模拟"又退回第 3 位"的旧形态）
_bad.remove("skill_prompt")
_bad.insert(0, "skill_prompt")

_v_good = _would_violate(_good)
_v_bad = _would_violate(_bad)
check("[D] 变异自证：合规基准通过、将其变异为违规必须被抓",
      (not _v_good) and bool(_v_bad),
      f"合规基准违规={_v_good or '无'}；变异后违规={_v_bad}")

print("\n" + "=" * 68)
if _FAILS:
    print(f"❌ FAIL {len(_FAILS)} 项：{_FAILS}")
    sys.exit(1)
print("✅ P4 缓存友好排序契约全部通过")
sys.exit(0)
