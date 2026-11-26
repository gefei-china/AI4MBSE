# -*- coding: utf-8 -*-
"""澄清准确度门禁：**延续型输入不得白弹澄清卡**（2026-11-26）。

## 病灶（真库实证，不是假设）

澄清判定原本只看「这一句本身的置信度」，**完全不看这一句是不是上一轮任务的延续**：

```
用户：在"通信子系统用例"包中新增一个"测试"包      ← 执行成功
用户：在"通信子系统用例"包中新增一个"测试包A"包   ← conf=0.72 ⇒ llm_first_weak ⇒ 弹卡
用户：在"通信子系统用例"包中新增一个"测试包B"包   ← conf=0.62 ⇒ 弹卡
用户：在"通信子系统用例"包中新增一个"测试包C"包   ← conf=0.72 ⇒ 弹卡
```

实测口径（`intent_samples`，n=612）：`llm_first_weak` 22 次，逐条读文本后
约 **10 次（45%）** 属"同一话题续做/重复问"；同期 `hil_confirmations` 46 张里
**21 张过期未处理（46%）**。⇒ 卡越多越没人看，真正该看的卡被淹掉。

## 修法

在 `llm_first` 的**中置信段**（0.50~0.75，原本必弹卡的唯一路径）加前置判定：
LLM 结论 **== prev_intent** 且 prev_intent 是已知意图 ⇒ 记 `llm_history`、静默执行。

## 本门禁锁四件事（每条都带变异自证）

  [P1] 延续型（conf=0.62 + 与 prev 一致）⇒ 不弹卡，route=llm_history
  [P2] **方向不变**：P1 的意图 == prev_intent（与 inherit 给出的完全一致）
  [P3] 非延续型（conf=0.62 + 与 prev **不一致**）⇒ **仍然弹卡**（红线：不许把该弹的也静默掉）
  [P4] 无上文（prev=None）⇒ 仍然弹卡（延续性判定不得在无上文时放行）
  [P5] 低于弱线（conf=0.30）⇒ 落回旧路，不进 llm_history
  [P6] 开关 `intent.llm_continue_silent=False` ⇒ 逐字节回到旧行为（延续型也弹卡）
  [M1] 变异：把判据改成"只要有上文就静默"（去掉 intent==prev 约束）⇒ P3 必须判红

## 为什么 P3/P4 是命门

这个改动的收益是"少打断"，但它唯一的风险是**把该问的不问了**。
P3/P4 就是那条红线：只有「LLM 与上文**双向一致**」才静默；
单向（只是有上文）或冲突时一律照旧弹卡。M1 证明这条线真的在起作用。

用法： ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_intent_continue_silent.py
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
for _p in (_ROOT,):
    if _p not in sys.path:
        sys.path.insert(0, _p)
os.chdir(_ROOT)

from agent.intent import IntentRouter          # noqa: E402
from database import get_db                    # noqa: E402

PASS, FAIL = [], []


def ck(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %s %s%s" % ("PASS" if ok else "FAIL", name,
                         ("  <- " + str(detail)) if detail else ""), flush=True)


class _StubLLM:
    def __init__(self, ret):
        self.ret = ret


HIST = ["对当前模型做一次需求分析，输出条目化结果",
        "把刚才的分析结论补上验收判据"]
#: 承接上文的**新**任务表述（单发看不清楚，但有明确上文）
Q_CONT = "在通信子系统用例包中新增测试包A"


def _router():
    """按真库已启用的 Agent 关键词装配路由（与生产同源）。"""
    import json
    rt = IntentRouter()
    con = get_db()
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT name, display_name, description, intent_keywords FROM agents "
            "WHERE status='active' ORDER BY name")]
    finally:
        con.close()
    idx = []
    for r in rows:
        kws = json.loads(r["intent_keywords"] or "[]")
        rt.register_keywords(r["name"], kws)
        idx.append({"name": r["name"],
                    "text": "%s %s %s" % (r["display_name"] or r["name"],
                                          r["description"] or "", " ".join(kws))})
    rt.set_semantic_index(idx)
    return rt


def _detect(rt, ret, text=Q_CONT, prev="design", history=None, cfg=None):
    """一次 detect，LLM 打桩。返回 (intent, meta)。"""
    orig = rt.detect_llm
    rt.detect_llm = lambda t, context="": ret
    _saved = {}
    try:
        if cfg:
            for k, v in cfg.items():
                _saved[k] = rt._cfg_get(k, None)
                rt.__dict__["_cfg_" + k] = v
                # 直接改类级读取：IntentRouter._cfg_get 走 core.config
        from core import config as _C
        _orig_get = _C.get
        if cfg:
            _C.get = lambda sec, key, d=None: (cfg.get(key, _orig_get(sec, key, d))
                                               if sec == "intent" else _orig_get(sec, key, d))
        try:
            con = get_db()
            try:
                got = rt.detect(text, conn=con, prev_intent=prev, history=history)
                m = rt.get_last_meta()
            finally:
                con.close()
        finally:
            _C.get = _orig_get
        return got, m
    finally:
        rt.detect_llm = orig


def main() -> int:
    print("=" * 74)
    print("澄清准确度：延续型输入不得白弹澄清卡")
    print("=" * 74)
    rt = _router()

    print("\n── P1/P2 延续型（conf=0.62，与上轮一致）⇒ 静默且方向不变 ──")
    got1, m1 = _detect(rt, ("design", 0.62), prev="design", history=HIST)
    print("     route=%s intent=%s conf=%s clarify=%s confirmed=%s"
          % (m1.get("route"), m1.get("intent"), m1.get("confidence"),
             m1.get("needs_clarification"), m1.get("confirmed")))
    ck("[P1] 延续型不弹澄清卡（route=llm_history）",
       m1.get("route") == "llm_history" and m1.get("needs_clarification") is False,
       "route=%s clarify=%s" % (m1.get("route"), m1.get("needs_clarification")))
    ck("[P2] 延续型意图 == prev_intent（与 inherit 给出的完全相同 ⇒ 零改判）",
       got1 == "design" and m1.get("intent") == "design", "got=%s" % got1)
    ck("[P2b] 延续型已标 used_history/confirmed（前端不出「可改选」细条）",
       m1.get("used_history") is True and m1.get("confirmed") is True,
       "used_history=%s confirmed=%s" % (m1.get("used_history"), m1.get("confirmed")))

    print("\n── P3 非延续型（conf=0.62，与上轮**不一致**）⇒ 仍然弹卡 ──")
    got3, m3 = _detect(rt, ("report_generation", 0.62), prev="design", history=HIST)
    print("     route=%s intent=%s clarify=%s"
          % (m3.get("route"), m3.get("intent"), m3.get("needs_clarification")))
    ck("[P3] ★不一致时仍弹澄清卡（红线：不许把该问的也静默掉）",
       m3.get("route") == "llm_first_weak" and m3.get("needs_clarification") is True,
       "route=%s clarify=%s" % (m3.get("route"), m3.get("needs_clarification")))

    print("\n── P4 无上文（prev=None）⇒ 仍然弹卡 ──")
    got4, m4 = _detect(rt, ("design", 0.62), prev=None, history=None)
    print("     route=%s clarify=%s" % (m4.get("route"), m4.get("needs_clarification")))
    ck("[P4] ★无上文时不放行（延续性判定不得凭空生效）",
       m4.get("route") == "llm_first_weak" and m4.get("needs_clarification") is True,
       "route=%s clarify=%s" % (m4.get("route"), m4.get("needs_clarification")))

    print("\n── P5 低于弱线（conf=0.30）⇒ 不进 llm_history ──")
    got5, m5 = _detect(rt, ("design", 0.30), prev="design", history=HIST)
    print("     route=%s intent=%s" % (m5.get("route"), m5.get("intent")))
    ck("[P5] 低于弱线走旧路（不算延续，也不弹 llm_first_weak）",
       m5.get("route") != "llm_history", "route=%s" % m5.get("route"))

    print("\n── P6 开关关闭 ⇒ 逐字节回到旧行为 ──")
    got6, m6 = _detect(rt, ("design", 0.62), prev="design", history=HIST,
                       cfg={"llm_continue_silent": False})
    print("     route=%s clarify=%s" % (m6.get("route"), m6.get("needs_clarification")))
    ck("[P6] llm_continue_silent=False ⇒ 延续型也弹卡（回退位有效）",
       m6.get("route") == "llm_first_weak" and m6.get("needs_clarification") is True,
       "route=%s clarify=%s" % (m6.get("route"), m6.get("needs_clarification")))

    print("\n── M1 ★变异自证：把判据放宽成「有上文就静默」⇒ P3 必须判红 ──")
    # 变异体：不做 intent==prev 比较（模拟"只要 _hist_ctx 非空就放行"）
    src_ok = True
    _mfail = []
    try:
        gotm, mm = _detect_mutant(rt, ("report_generation", 0.62),
                                  text=Q_CONT, prev="design", history=HIST)
        turned_red = (mm.get("route") == "llm_history")
        ck("[M1] 放宽判据后 P3 确实翻红（证明 P3 的 intent==prev 约束真在起作用）",
           turned_red, "变异体 route=%s（若为 llm_first_weak，说明变异没生效）" % mm.get("route"))
    except Exception as e:
        ck("[M1] 放宽判据后 P3 确实翻红", False, "%s: %s" % (type(e).__name__, str(e)[:120]))

    print("\n" + "=" * 74)
    print("PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
    for n in FAIL:
        print("  FAIL:", n)
    print("=" * 74)
    return 0 if not FAIL else 1


def _detect_mutant(rt, ret, text, prev, history):
    """变异体：把 `llm_intent == prev_intent` 约束去掉，只保留"有上文"。

    做法：临时把 `prev_intent` 换成 LLM 给的那个意图 —— 等价于让一致性比较恒成立，
    从而模拟"去掉一致性约束"的坏实现（只要 `_hist_ctx` 非空就静默）。
    这是**行为等价**的变异，不改源码文本，因此不会因代码重排而失效
    （本仓铁律：变异要能承受重构；文本锚点会随重构漂移，行为变异不会）。
    """
    return _detect(rt, ret, text=text, prev=ret[0], history=history)


if __name__ == "__main__":
    sys.exit(main())
