# -*- coding: utf-8 -*-
"""T0 触发器探针：找一条能真实触发内容级澄清（阶段1.5）的测试输入。

判定链（与 stream.py 一致）：
  1) intent = forced_intent or detected；须 ∈ CLARIFY_INTENTS
  2) _should_confirm_intent（阶段1.4，优先于1.5）
  3) _clarify_detect → _clarify_skip_reason（quick_pick/action_kw/long_input 全不命中）
     → LLM quick 判定 need=true 或规则兜底（<18字）
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from agent import AgentPipeline

CANDS = [
    "帮我弄一下这个",
    "请帮我弄一下",
    "我想整个东西",
    "帮我整一个东西",
    "有个事儿要办",
]

p = AgentPipeline()
p._load_db_agents("1")

print(f"{'输入':<14} {'detected':<22} {'1.4确认':<8} {'skip_reason':<18} {'1.5澄清'}")
print("-" * 90)
for c in CANDS:
    try:
        det = p.router.detect(c)
    except Exception as e:
        print(f"{c:<14} DETECT-ERR {e}")
        continue
    intent = det
    try:
        meta = p.router.get_last_meta()
    except Exception:
        meta = {}
    iq = False
    try:
        iq = p._should_confirm_intent(c, meta)
    except Exception as e:
        iq = f"ERR{e}"
    qs = None
    skip = ""
    if intent in p.CLARIFY_INTENTS and not iq:
        try:
            qs = p._clarify_detect(c, intent, None, forced_intent="", skill_name="")
            skip = getattr(p, "_last_clarify_skip", "")
        except Exception as e:
            qs = f"ERR:{e}"
    nq = len(qs) if isinstance(qs, list) else qs
    print(f"{c:<14} {intent:<22} {str(iq):<8} {skip or '-':<18} {nq}  "
          f"[route={meta.get('route')} conf={meta.get('confidence')}]")
