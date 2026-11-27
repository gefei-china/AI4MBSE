# -*- coding: utf-8 -*-
"""门禁：澄清续答必须能被团队路由消费，不得判「团队暂不支持」。

背景（2026-11-27 会话 1000000254 真机事故）：
  用户回答澄清「就在 AI建模1126 工程里」后，前端发 /chat/stream，
  `user_input` 是「【澄清补充】…原请求：继续」这种元指令。
  团队路由 LLM 判成「团队暂不支持该能力」⇒ stream.py 的 reject 分支
  只发 5 帧、done{ok:false, message_id:0} 后 return ⇒ 不落库
  ⇒ 前端停在旧确认卡，用户看到「确认后没有正常展示后续流程」。

判据：
  A 两种真实续答格式（前端 resume_text 模板 / 历史卡追认模板）都必须路由成功；
  B 普通请求行为不变（噪声仍 chat、越界仍 reject）——防修 A 把闸门一起拆了；
  C 变异自证：把剥离逻辑去掉，A 必须翻红。锚点短、唯一，断言 mutated != src。

用法：python tools/verify/verify_clarify_resume_route.py
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from agent import team_router as tr

RESULTS = []
TEAM = {
    "intent": "MBSE建模总体负责人",
    "members": [
        {"name": "变更影响Agent", "intent_keywords": ["变更影响", "影响分析", "卫星通信"],
         "description": "做变更影响分析"},
        {"name": "报告生成Agent", "intent_keywords": ["报告"], "description": "写报告"},
    ],
}
RESUME_TEXT = ("【澄清补充】用户已补充以下建模信息（请据此继续，无需再确认）：\n"
               "- 就在「AI建模1126」工程里，请到该工程下找卫星通信相关包\n\n原请求：继续")
APPENDED_TEXT = ("继续\n\n———\n\n【澄清补充】问题「卫星通信建模工程在哪个工程下？」"
                 "→ 回答：就在「AI建模1126」工程里，请到该工程下找卫星通信相关包")
# 变异用例专用：不含任何成员关键词，剥离与否必须决定 direct vs reject
NEUTRAL_RESUME = ("【澄清补充】用户已补充以下建模信息（请据此继续，无需再确认）：\n"
                  "- 工程编号是 AI建模1126，请用这个坐标继续\n\n原请求：继续")


def check(group, name, cond, detail=""):
    RESULTS.append((group, name, bool(cond), detail))


def group_a():
    for label, q in (("前端 resume_text 模板", RESUME_TEXT),
                     ("历史卡追认模板", APPENDED_TEXT)):
        d = tr.decide(q, TEAM)
        check("A", "%s ⇒ 不 reject" % label,
              d.get("action") != "reject", str(d))
        check("A", "%s ⇒ 定向变更影响Agent" % label,
              d.get("action") == "direct" and d.get("target") == "变更影响Agent", str(d))


def group_b():
    d_noise = tr.decide("你好", TEAM)
    check("B", "噪声仍走 chat（不因修复全放行）", d_noise.get("action") == "chat", str(d_noise))
    d_direct = tr.decide("生成一份卫星通信的变更影响分析报告", TEAM)
    check("B", "普通请求仍定向成员", d_direct.get("action") == "direct", str(d_direct))
    d_other = tr.decide("帮我写一首关于春天的诗", TEAM)
    check("B", "越界请求仍 reject（闸门没被拆）", d_other.get("action") == "reject", str(d_other))


def group_c():
    src_path = os.path.join(_ROOT, "agent", "team_router.py")
    src = open(src_path, encoding="utf-8").read()
    anchor = '        if _real:\n            q = _real'
    if src.count(anchor) != 1:
        check("C", "变异锚点唯一", False, "count=%d" % src.count(anchor))
        return
    check("C", "变异锚点唯一", True)
    mutated = src.replace(anchor, '        if False:\n            q = _real', 1)
    check("C", "变异体真的变了（mutated != src）", mutated != src)
    if mutated == src:
        return
    tmp = os.path.join(tempfile.gettempdir(), "_tr_mut_clarify_gate.py")
    open(tmp, "w", encoding="utf-8").write(mutated)
    spec = importlib.util.spec_from_file_location("_tr_mut_clarify", tmp)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    d = mod.decide(NEUTRAL_RESUME, TEAM)
    check("C", "★ 去掉剥离后中性续答被 reject ⇒ A 有牙齿",
          d.get("action") == "reject", str(d))
    try:
        os.remove(tmp)
    except OSError:
        pass


def main():
    for fn in (group_a, group_b, group_c):
        try:
            fn()
        except Exception as e:
            check(fn.__name__, "组执行异常", False, repr(e))
    w = max(len(n) for _, n, _, _ in RESULTS)
    print("=" * (w + 16))
    print("澄清续答路由门禁（确认后必须有后续流程）")
    print("=" * (w + 16))
    for g, n, ok, detail in RESULTS:
        print("  [%s] %s%s" % ("OK" if ok else "FAIL", n,
                               ("  — " + detail) if (detail and not ok) else ""))
    bad = [x for x in RESULTS if not x[2]]
    print("-" * (w + 16))
    print("合计 %d 项，通过 %d，失败 %d" % (len(RESULTS), len(RESULTS) - len(bad), len(bad)))
    for g, n, _, d in bad:
        print("  - %s：%s" % (n, d[:200]))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
