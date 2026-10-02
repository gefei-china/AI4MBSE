# -*- coding: utf-8 -*-
"""P1-3 分层压缩（五档总闸）—— 2026-10-02 米爸拍板实施（评估报告 §2.1 P1 最大缺口）。

演进关系：P1-2（09-24）的总闸只有两档（scale 0.6/0.35），且**可裁块只有检索区与两个历史块**
——实测 24~27k 尖峰压不动：system 本体+模板+规则占 prompt 大头，两档用尽仍超
（总闸日志会显示"到达档位 2"但 after 仍 >cap，输出侧 guard 兜底成常态）。

本模块把总闸从"两档重裁"升级为"五档逐层解锁更多可裁块"（压力 = 估算输入 tokens / total_cap）：

  档0 ≤100%    零开销（原样返回，常态路径）
  档1 >100%    scale 0.6 重裁检索/历史（= 旧档 1，行为不变）
  档2 >130%    scale 0.35 + 附件块裁到 ~1200 tok（= 旧档 2 + 附件纳入）
  档3 >160%    scale 0.2 + 【更早对话摘要】整块置一行占位（细节都在 DB，可追问恢复）
  档4 >200%    全量：scale 0.1，检索/历史/附件/摘要全部压到最小，只保 system 本体 + 当前输入

纪律：
· 不引入第二套裁剪原语——scale 重裁复用 `_apply_context_budget`，token 截断复用
  `HistoryMixin._truncate_tokens`，本模块只做「档位决策 + 块定位」，逻辑可独立单测；
· system 本体（角色/建模规则/工具约束）任何档都不裁——压它直接改变产出质量（P1-2 §3 既定）；
· 提前收敛：某档裁完已 ≤cap 即停，不做无用功；
· 调用方（history._apply_total_budget）保留既有开关与故障保护，本模块纯函数无副作用。
"""

#: 档 1..4 对应的检索/历史 scale（档 1/2 与旧两档数值一致 ⇒ 旧触发面行为零漂移）
TIER_SCALES = (0.6, 0.35, 0.2, 0.1)

#: 档 i 的触发线：pressure = before/total_cap **严格大于** TIER_TRIGGERS[i-1] 才进档 i
TIER_TRIGGERS = (1.0, 1.3, 1.6, 2.0)

#: 附件块头（prompt.py::_build_attachment_block 唯一真源，勿在此复制第二份文案）
ATT_HEADER = "【用户上传资料（优先依据）】"

#: 附件块的**下一个**块头候选（取最早出现者作为块尾；均无前导空行歧义）
_NEXT_BLOCK_MARKERS = (
    "检索到的互联数据：",       # stream.py 拼装 + execute assemble 的检索块头
    "【需求质量分析结果",        # 报告场景 context_text 尾巴（stream.py:1341）
    "【更早对话摘要】",
    "【历史对话概览】",
)

#: 档 3 摘要占位（历史细节全部在 conversation_summaries / messages 表，追问即可恢复）
_SUMMARY_PLACEHOLDER = "（已达深度压缩：更早轮次摘要已省略，需要时直接追问即可恢复）"


def pressure_tier(before: int, total_cap: int) -> int:
    """按压力比返回档位 0..4（0=预算内零动作）。

    边界语义：**严格大于**才进档（r == 1.0 属预算内；r == 1.3 属档 1，不到档 2）。
    """
    if not total_cap or total_cap <= 0 or not before or before <= 0:
        return 0
    r = before / total_cap
    tier = 0
    for i, trig in enumerate(TIER_TRIGGERS, start=1):
        if r > trig:
            tier = i
        else:
            break
    return tier


def _block_span(text: str, header: str):
    """定位「header 开头、到下一个已知块头/文末」的块 span（start, end）。

    附件/摘要等块以【块头】开头；块尾取 `_NEXT_BLOCK_MARKERS` 中最早出现者。
    找不到 header 返回 None。
    """
    start = text.find(header)
    if start < 0:
        return None
    body_start = start + len(header)
    end = len(text)
    for m in _NEXT_BLOCK_MARKERS:
        p = text.find(m, body_start)
        if 0 <= p < end:
            end = p
    return start, end


def cut_attachment_block(system_prompt: str, truncate_fn, keep_tokens: int = 1200) -> str:
    """档 2+：把附件块正文按 token 截断（保留头部——资料相关性在头部，对齐既有裁剪语义）。

    truncate_fn: HistoryMixin._truncate_tokens（token 驱动 + 迭代逼近）。
    块不存在 / 正文已在预算内 → 原样返回（幂等）。
    """
    try:
        span = _block_span(system_prompt or "", ATT_HEADER)
        if not span:
            return system_prompt
        start, end = span
        header_end = start + len(ATT_HEADER)
        body = system_prompt[header_end:end]
        if not body.strip():
            return system_prompt
        kept = truncate_fn(body, keep_tokens, keep_head=True)
        return system_prompt[:header_end] + kept + system_prompt[end:]
    except Exception:
        return system_prompt  # 裁剪失败不阻断主链路（与总闸同款故障保护）


def collapse_summary_block(system_prompt: str) -> str:
    """档 3+：【更早对话摘要】整块置一行占位（保留【历史对话概览】——它本来就是概览）。

    占位自身 ~40 tok，相比原块（上限 8000 字符正则域）净收益显著。
    """
    try:
        import re as _re
        pat = _re.compile(r"【更早对话摘要】\n[\s\S]*?(?=\n【历史对话概览】|$)")
        new, n = pat.subn("【更早对话摘要】\n" + _SUMMARY_PLACEHOLDER, system_prompt, count=1)
        return new if n else system_prompt
    except Exception:
        return system_prompt


def apply_tier(system_prompt: str, context_text: str, tier: int,
               budget_fn, truncate_fn) -> str:
    """执行档 `tier`（1..4）的**单档**动作，返回裁剪后的 system_prompt。

    budget_fn: 绑定的 HistoryMixin._apply_context_budget，签名兼容
    (system_prompt, retrieval_text, history_len, scale=...)——本函数补传 history_len=0
    （当前实现未消费该参数；缺失会让 TypeError 被 except 吞掉 ⇒ scale 重裁**静默失效**，
    2026-10-02 实测踩过：档1"裁掉 0 tok"即此根因，勿删）。
    逐档循环由调用方负责（每档后重算 tokens、够即停）。
    """
    if tier < 1 or tier > len(TIER_SCALES):
        return system_prompt
    try:
        system_prompt = budget_fn(system_prompt, context_text or "", 0,
                                  scale=TIER_SCALES[tier - 1])
    except Exception:
        pass  # scale 重裁失败不阻断，后续档继续尝试别的块
    if tier >= 2:
        system_prompt = cut_attachment_block(system_prompt, truncate_fn)
    if tier >= 3:
        system_prompt = collapse_summary_block(system_prompt)
    return system_prompt
