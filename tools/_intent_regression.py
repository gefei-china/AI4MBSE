# -*- coding: utf-8 -*-
"""最终回归：意图收紧（含缓存毒化防线）"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agent.intent import IntentRouter

router = IntentRouter()
cases = [
    ("这个功能的质量怎么样", "chat"),            # 泛词 → 不应触发质量评审
    ("帮我做需求质量评审", "requirement_quality"),  # 明确要求 → 触发
    ("对这段需求做质量分析", "requirement_quality"),
    ("这句话有模糊词吗", "requirement_quality"),
]
all_ok = True
for text, expect in cases:
    got = router.detect(text)
    ok = got == expect
    all_ok = all_ok and ok
    meta = getattr(router, "_last_meta", {})
    print(f"{'✓' if ok else '✗'} 「{text[:20]}」→ {got}（期望 {expect}，route={meta.get('route')}）")
print("全部通过" if all_ok else "存在失败")
