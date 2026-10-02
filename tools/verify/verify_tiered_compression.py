# -*- coding: utf-8 -*-
"""P1-3 五档分层压缩验证（compression.py + history._apply_total_budget + 四段式摘要）。

验证五件事：
1. 档位映射：pressure_tier 各压力比 → 正确档位（含全部边界与真实尖峰回归样例）；
2. 单元级逐档行为：档1 只裁检索/历史；档2 裁附件；档3 摘要占位；档4 全量；本体哨兵句永不裁；
3. 端到端：HistoryMixin._apply_total_budget 真跑——常态零开销 / 档1 行为 / 重度超压大幅下降
   （⚠️ 提前收敛语义：某档裁完 ≤cap 即停，端到端**不断言更高档的动作**——那是单元级的职责）；
4. 变异自证（两针）：
   a) TIER_TRIGGERS 抬到永不触发 → tier 判定断言必须挂；
   b) ATT_HEADER 改错 → **先构造好固定 prompt 再 patch**（patch 若同时污染构造则自证失效，
      2026-10-02 实测踩过：make_prompt 与 cut 读同一常量 ⇒ patch 后"构造+定位"一起变，断言空转）；
5. 四段式摘要：prompt 契约关键词 + LLM 正常/失败两路径 + 源码静态断言。

用法：直接跑（MBSE_DB_PATH 指向临时库，零网络——LLM 用 monkeypatch 假客户端）。
"""
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="verify_tiered_comp_")
os.environ["MBSE_DB_PATH"] = os.path.join(_TMP, "verify.db")

sys.path.insert(0, r"C:/Users/gefei/WorkBuddy/2026-08-04-19-05-52/mbse_system")
os.chdir(r"C:/Users/gefei/WorkBuddy/2026-08-04-19-05-52/mbse_system")

from database import init_db
init_db()

from agent.pipeline_parts import compression as cp
from agent.pipeline_parts.history import HistoryMixin

FAILURES = []


def check(name, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    print(f"[{tag}] {name}" + (f" —— {detail}" if detail else ""))
    if not cond:
        FAILURES.append(name)


def expect_fail(name, fn):
    """变异自证脚手架：断言应当在被注入 bug 时挂掉。"""
    try:
        fn()
        print(f"[FAIL] 变异自证：{name} —— 注入 bug 后断言仍通过 ⇒ 断言空转")
        FAILURES.append(f"mutation:{name}")
    except AssertionError:
        print(f"[PASS] 变异自证：{name} —— 注入 bug 后断言如预期 FAIL")


# ── 1. 档位映射 ─────────────────────────────────────────────────────────
CAP = 10000
for before, want in [(9900, 0), (10000, 0), (10100, 1), (13000, 1), (13100, 2),
                     (16000, 2), (16100, 3), (20000, 3), (20100, 4), (30000, 4),
                     (24744, 4)]:
    check(f"档位映射 before={before}→档{want}", cp.pressure_tier(before, CAP) == want)
check("档位映射 total_cap=0 → 档0（除零保护）", cp.pressure_tier(5000, 0) == 0)
check("档位映射 真实尖峰 24744→档4", cp.pressure_tier(24744, 10000) == 4)

# ── 2. 单元级逐档行为 ──────────────────────────────────────────────────
BODY_SENTINEL = "本体哨兵句：你是系统工程专家，必须遵守建模规则。"


def make_prompt(att_chars=0, sum_chars=0, ov_chars=0, retr_chars=0):
    p = BODY_SENTINEL + "\n"
    if att_chars:
        p += cp.ATT_HEADER + "\n" + ("附件正文A" * (att_chars // 4)) + "\n\n"
    if sum_chars:
        p += "【更早对话摘要】\n" + ("摘要内容S" * (sum_chars // 4)) + "\n"
    if ov_chars:
        p += "【历史对话概览】\n" + ("概览内容O" * (ov_chars // 4)) + "\n"
    if retr_chars:
        p += "检索到的互联数据：\n" + ("检索数据R" * (retr_chars // 4))
    return p


class _Stub:
    """只绑定被测两方法——_apply_context_budget 内部仅引用 self._truncate_tokens（已核实）。

    ⚠️ 必须 staticmethod 包裹：`_truncate_tokens = HistoryMixin._truncate_tokens` 会把
    staticmethod **解包成普通函数**再赋值 ⇒ 实例访问绑定 self ⇒ cut 内部 TypeError 被吞、
    裁剪静默失效（2026-10-02 实测踩过，报错形态：got multiple values for 'keep_head'）。
    生产无此问题（Mixin 继承保持 staticmethod 描述符），但脚手架必须写对。
    """
    _truncate_tokens = staticmethod(HistoryMixin._truncate_tokens)
    _apply_context_budget = HistoryMixin._apply_context_budget


stub = _Stub()

RETR = "检索到的互联数据：\n" + ("检索数据R" * 1000)
p_t2 = make_prompt(att_chars=24000, sum_chars=3000, ov_chars=400, retr_chars=4000)
n_t2 = len(p_t2)

# 档1：只 scale 重裁，附件/摘要不动
out1 = cp.apply_tier(p_t2, RETR, 1, budget_fn=stub._apply_context_budget, truncate_fn=stub._truncate_tokens)
check("档1：附件长串未被裁", ("附件正文A" * 1000) in out1)
check("档1：本体哨兵句保留", BODY_SENTINEL in out1)

# 档2：附件被裁（1200 tok ≈ 千字符级，3000-char 连串必消失）
out2 = cp.apply_tier(p_t2, RETR, 2, budget_fn=stub._apply_context_budget, truncate_fn=stub._truncate_tokens)
check("档2：附件 3000-char 连串消失", ("附件正文A" * 1000) not in out2)
check("档2：本体哨兵句保留", BODY_SENTINEL in out2)

# 档3：摘要块变占位
out3 = cp.apply_tier(p_t2, RETR, 3, budget_fn=stub._apply_context_budget, truncate_fn=stub._truncate_tokens)
check("档3：摘要被占位替换", "（已达深度压缩：更早轮次摘要已省略" in out3
      and ("摘要内容S" * 100) not in out3)
check("档3：本体哨兵句保留", BODY_SENTINEL in out3)

# 档4：全量最紧
out4 = cp.apply_tier(p_t2, RETR, 4, budget_fn=stub._apply_context_budget, truncate_fn=stub._truncate_tokens)
check("档4：长度 ≤ 档2 结果（逐档单调）", len(out4) <= len(out2), f"{len(out2)}→{len(out4)}")
check("档4：本体哨兵句保留", BODY_SENTINEL in out4)

# ── 3. 端到端（HistoryMixin._apply_total_budget 真跑）───────────────────
import core.config as _cfgmod
_orig_get = _cfgmod.get


def _with_cap(cap_val):
    _cfgmod.get = lambda d, k, dft=None, **kw: (
        cap_val if (d, k) == ("context", "total_budget_tokens") else _orig_get(d, k, dft, **kw))


class _E2EStub:
    _truncate_tokens = staticmethod(HistoryMixin._truncate_tokens)
    _apply_context_budget = HistoryMixin._apply_context_budget
    _apply_total_budget = HistoryMixin._apply_total_budget


stub2 = _E2EStub()

# A：中等超压（~1.1x）→ 档1：总账下降、附件/摘要不动
from core.token_counter import count_tokens
prompt_mid = make_prompt(att_chars=8000, retr_chars=4000)
cap_mid = max(int(count_tokens(prompt_mid) / 1.12), 2000)
_with_cap(cap_mid)
msgs = [{"role": "system", "content": prompt_mid}, {"role": "user", "content": "问题"}]
stub2._apply_total_budget(msgs, RETR)
_with_cap(None)  # 占位，下一行恢复
_cfgmod.get = _orig_get
check("端到端档1：附件长串未裁", ("附件正文A" * 100) in msgs[0]["content"])
check("端到端档1：本体保留", BODY_SENTINEL in msgs[0]["content"])

# B：重度超压（~2.5x）→ 高档触发、总账大幅下降、本体保留
#    （不断言"摘要占位/附件消失"——若档2 已收敛 ≤cap，档3/4 按设计不执行）
prompt_heavy = make_prompt(att_chars=24000, sum_chars=6000, ov_chars=800, retr_chars=8000)
before_heavy = count_tokens(prompt_heavy) + 2
cap_low = max(int(before_heavy / 2.5), 2000)
_with_cap(cap_low)
msgs2 = [{"role": "system", "content": prompt_heavy}, {"role": "user", "content": "问题"}]
stub2._apply_total_budget(msgs2, "检索到的互联数据：\n" + ("检索数据R" * 2000))
_cfgmod.get = _orig_get
n_out = count_tokens(msgs2[0]["content"])
check("端到端重度：总 tokens 大幅下降", n_out < before_heavy * 0.5, f"{before_heavy}→{n_out}")
check("端到端重度：本体哨兵句保留", BODY_SENTINEL in msgs2[0]["content"])

# C：预算内 → 零动作（bytes 级原样）
_with_cap(before_heavy * 10)
msgs3 = [{"role": "system", "content": prompt_mid}, {"role": "user", "content": "问题"}]
stub2._apply_total_budget(msgs3, RETR)
_cfgmod.get = _orig_get
check("常态零开销：预算内原样返回", msgs3[0]["content"] == prompt_mid)

# ── 4. 变异自证（两针，测完恢复）───────────────────────────────────────
def _assert_tier4():
    assert cp.pressure_tier(24744, 10000) == 4, "24744/10000 必须判档4"


# 针2 的关键：**先构造好固定 prompt（patch 前）**，patch 后只破坏"定位"
P_ATT_FIXED = make_prompt(att_chars=24000, retr_chars=2000)


def _assert_att_cut():
    out = cp.cut_attachment_block(P_ATT_FIXED, _Stub._truncate_tokens)
    assert ("附件正文A" * 1000) not in out, "附件 3000-char 连串必须在裁剪后消失"


_orig_triggers = cp.TIER_TRIGGERS
try:
    cp.TIER_TRIGGERS = (9.9,) * 4  # 注入 bug：永不触发
    expect_fail("针1 TIER_TRIGGERS 抬高 → tier 判定断言必须挂", _assert_tier4)
finally:
    cp.TIER_TRIGGERS = _orig_triggers
check("恢复后 tier 判定回绿", cp.pressure_tier(24744, 10000) == 4)

_orig_att = cp.ATT_HEADER
try:
    cp.ATT_HEADER = "【不存在的块头】"  # 注入 bug：块定位失效（P_ATT_FIXED 已在 patch 前构造）
    expect_fail("针2 ATT_HEADER 改错 → 附件裁剪断言必须挂", _assert_att_cut)
finally:
    cp.ATT_HEADER = _orig_att
check("恢复后附件裁剪回绿",
      ("附件正文A" * 1000) not in cp.cut_attachment_block(P_ATT_FIXED, _Stub._truncate_tokens))

# ── 5. 四段式摘要 ──────────────────────────────────────────────────────
import llm as _llm


class _FakeLLM:
    def __init__(self, fail=False):
        self.fail = fail
        self.last_prompt = ""

    def chat(self, messages, **kw):
        if self.fail:
            raise RuntimeError("mock down")
        self.last_prompt = messages[0]["content"]
        return {"choices": [{"message": {"content": "目标：优化总闸\n状态：五档已实施\n约束：不动本体\n下一步：跑回归"}}]}


class _SumStub:
    _llm_summarize = HistoryMixin._llm_summarize


stub_sum = _SumStub()
fake = _FakeLLM()
_orig_client = _llm.llm_client
_llm.llm_client = fake
try:
    s = stub_sum._llm_summarize("用户: 做总闸\n助手: 好的", "conv")
finally:
    _llm.llm_client = _orig_client
check("四段式：LLM 路径输出含契约四段", all(k in s for k in ("目标：", "状态：", "约束：", "下一步：")), s[:60])
check("四段式：prompt 已升级为契约文案", "四段式摘要" in fake.last_prompt and "下一步" in fake.last_prompt)

fake2 = _FakeLLM(fail=True)
_llm.llm_client = fake2
try:
    s2 = stub_sum._llm_summarize("用户: 做总闸\n助手: 好的", "conv")
finally:
    _llm.llm_client = _orig_client
check("四段式：LLM 失败降级为规则拼接", "做总闸" in s2 or s2 == "", s2[:40])

import inspect
src = inspect.getsource(HistoryMixin._llm_summarize)
check("四段式：源码含契约关键词", all(k in src for k in ("目标：", "状态：", "约束：", "下一步：")))

print()
if FAILURES:
    print(f"❌ {len(FAILURES)} 项失败：{FAILURES}")
    sys.exit(1)
print("✅ ALL PASS —— 五档压缩/端到端/变异自证/四段式摘要全部通过")
