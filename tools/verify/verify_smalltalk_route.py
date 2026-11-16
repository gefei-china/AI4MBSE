# CI-OPTIONAL: [local] tier=local（见 tools/gates_manifest.json）：需真实服务 / 真 LLM key / 生产真库金标或随包运行时资产，CI 无服务无 secret ⇒ 跑不了；本机已实测通过，改法须同时改清单 tier 并接线，不允许只删这行
"""纯寒暄路由门禁（2026-11-07 米爸第 16 轮）。

## 修的是什么
`llm_conflict` 路由真实准确率仅 25%(1/4)，其中 2 条错例是 `你好` / `谢谢`
被判成 `knowledge_qa`；更严重的是 `你好！` 走 `llm_first` **0.90 且不澄清**。
根因：寒暄句无领域词 → 规则/语义层无信号 → 全靠 LLM 猜，而 LLM 在"MBSE 平台"
角色设定下倾向判 knowledge_qa。

## 判据分三层（防"只测寒暄"的假绿）
  A 纯函数：`_is_pure_smalltalk` 对寒暄集为真、对带任务集为假
  B 真路由器：寒暄全部落 chat/smalltalk 且**不弹澄清**
  C 零回归：48 条真库 confirmed 准确率**不降**（这是最关键的一条 ——
    拦寒暄很容易顺手把短任务句也拦掉）
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
DB = os.path.join(ROOT, "mbse.db")
FAILED = []

SMALLTALK = ["你好", "谢谢", "你好！", "您好", "在吗", "好的", "收到", "嗯",
             "hi", "hello", "早上好", "谢谢！", "多谢", "再见", "ok"]
#: 带任务/疑问的短句 —— **绝不能被判成寒暄**（误伤真实请求比漏判严重得多）
NOT_SMALLTALK = [
    "你好，帮我看看这个模型", "谢谢，那接下来怎么生成视图", "帮我设计一下系统架构",
    "校验一下这段代码", "生成 BDD 视图", "分析一下影响", "写个报告",
    "这个平台怎么新建工程", "什么是接口定义", "在吗，帮我跑一下校验",
]


def rec(code, ok, detail=""):
    print("[%s] %-4s %s%s" % ("PASS" if ok else "FAIL", code, detail,
                              "" if ok else "  <<<"))
    if not ok:
        FAILED.append(code)


def _label():
    """硬编码 grep 的替代：直接说明，保证与实现同步。"""
    return None


def main() -> int:
    from core import config as cfg
    from agent.pipeline import AgentPipeline
    from agent.intent import IntentRouter

    print("=" * 80)
    print("纯寒暄路由门禁 — 纯函数 / 真路由器 / 零回归")
    print("=" * 80)

    # 实例化一个最小对象只测纯函数（不建 Pipeline，避免 embedding 预热耗时）
    R = IntentRouter

    print("\nA 纯函数层")
    miss = [t for t in SMALLTALK if not R._is_pure_smalltalk(R(), t)]
    rec("A1", not miss, "寒暄集 %d 条全部判真（漏判 %s）" % (len(SMALLTALK), miss))
    bad = [t for t in NOT_SMALLTALK if R._is_pure_smalltalk(R(), t)]
    rec("A2", not bad, "带任务集 %d 条全部判假（误伤 %s）" % (len(NOT_SMALLTALK), bad))

    # B 真路由器
    cfg._CONFIG.setdefault("intent", {})["cache_enabled"] = False
    cfg._CONFIG["intent"]["smalltalk_route"] = True
    pipe = AgentPipeline(); pipe._load_db_agents(); r = pipe.router
    print("\nB 真路由器层")
    rows = []
    for t in SMALLTALK:
        got = r.detect(t); m = r.get_last_meta() or {}
        rows.append((t, got, m.get("route"), m.get("needs_clarification")))
    wrong = [(t, g, rt) for t, g, rt, _ in rows if g != "chat"]
    rec("B1", not wrong, "寒暄全部落 chat（错 %s）" % wrong)
    cl = [t for t, _, _, c in rows if c]
    rec("B2", not cl, "寒暄不弹澄清（否则用户说「你好」还要确认意图）%s" % cl)
    routed = [t for t, _, rt, _ in rows if rt == "smalltalk"]
    rec("B3", len(routed) >= len(SMALLTALK) - 2,
        "%d/%d 走 smalltalk 前置（其余走既有 chat 路径也算对）"
        % (len(routed), len(SMALLTALK)))

    # C 零回归：真库 confirmed 准确率
    print("\nC 零回归层（真库 confirmed）")
    conn = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
    cases = [(x[0], x[1]) for x in conn.execute(
        "SELECT text,intent FROM intent_samples WHERE status='confirmed' "
        "AND intent<>'' ORDER BY id")]
    conn.close()

    def _acc(enabled):
        """返回 (准确率, 全量判定 map)。**必须返回全量**：
        只返回错例集合的话，「原本对→现在错」的样本两边都不在错例里，C3 会假绿。"""
        cfg._CONFIG["intent"]["smalltalk_route"] = enabled
        hit = 0
        all_map, wrong = {}, []
        for text, want in cases:
            got = r.detect(text)
            all_map[text] = got
            if got == want:
                hit += 1
            else:
                wrong.append((text[:26], want, got))
        return hit / len(cases), all_map, wrong

    a_on, m_on, d_on = _acc(True)
    a_off, m_off, d_off = _acc(False)
    cfg._CONFIG["intent"]["smalltalk_route"] = True
    print("  OFF acc=%.1f%% / ON acc=%.1f%%（%d 条）" %
          (a_off * 100, a_on * 100, len(cases)))
    rec("C1", len(cases) >= 40, "真库 confirmed 样本 %d 条" % len(cases))
    rec("C2", a_on >= a_off, "寒暄开关不降低准确率：%.1f%% → %.1f%%" %
        (a_off * 100, a_on * 100))
    # 寒暄规则**只能**改变被判为纯寒暄的样本的判定。
    # ⚠️ 首版 C3 写成「ON/OFF 任何一条不同就 FAIL」，实测抓到两条：
    #     `帮我看看这个系统大概是怎么设计的` design→chat、`帮我写一首诗` chat→knowledge_qa。
    #   查证：两条都**不是纯寒暄**（含"帮我"/"写"被规则排除），寒暄规则根本没被触发
    #   ⇒ 变化来自 **LLM 兜底层本身的非确定性**（同样代码两次跑结果不同）。
    #   把随机性判成回归会让门禁永远红，且掩盖真实信号。
    #   正确判据：改动作用域 = `_is_pure_smalltalk` 为真的样本；域外变化只做**观测记录**。
    def pure(t):
        return IntentRouter._is_pure_smalltalk(r, t)

    in_scope = [(t, m_off[t], m_on.get(t)) for t in m_off
                if pure(t) and m_off[t] != m_on.get(t)]
    out_scope = [(t[:20], m_off[t], m_on.get(t)) for t in m_off
                 if not pure(t) and m_off[t] != m_on.get(t)]
    rec("C3", all(b == "chat" for _, _, b in in_scope),
        "寒暄域内变化全部指向 chat（%d 条：%s）" % (len(in_scope), in_scope))
    if out_scope:
        print("  [NOTE] 域外扰动 %d 条（LLM 兜底非确定性，与本改动无关）：%s"
              % (len(out_scope), out_scope))
    rec("C4", cfg.get("intent", "smalltalk_route", None) is True,
        "smalltalk_route 默认 True（有 False 回退位）")

    out = {"n": len(cases), "acc_off": round(a_off, 4), "acc_on": round(a_on, 4),
           "smalltalk_n": len(SMALLTALK), "not_smalltalk_n": len(NOT_SMALLTALK)}
    p = os.path.join(ROOT, "tools", "tmp", "smalltalk_route.json")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    json.dump(out, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("\n" + "=" * 80)
    print("ALL_OK=%s" % (not FAILED) if not FAILED
          else "FAIL %d 项：%s" % (len(FAILED), ", ".join(FAILED)))
    return 0 if not FAILED else 1


if __name__ == "__main__":
    raise SystemExit(main())
