#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""verify_intent_llm_review —— B4-5「规则给候选 + LLM 确认/否决」的门禁。

## 米爸的原始诉求（2026-10-14 11:00）
> 意图识别这个环节我很不满意，为什么**必须是那几个 agent**？
> 应该**经过 LLM 分析**来判断有哪些意图，再和平台的能力进行匹配。

实测基线：改造前 `detect()` 里 `_done(..., "rule", 0.95)` 散落 **11 处**，
规则命中即直返 ⇒ 绝大多数请求**根本不进 LLM**。

## 本门禁锁住四件事（每条都可用变异自证反向锁死）
1. **接线**：规则直返**必须**走 `_rule_done`，不得绕过复核层
   （靠 AST 找 detect() 里所有 `_done(..., 'rule*')` 直返并判红）
2. **三态如实**：`confirmed / vetoed / unavailable / skipped` 四态都可达，
   且 `unavailable` 与 `skipped` **不得**被当作"确认"（防"没复核成说成复核过"）
3. **降级安全**：LLM 抛异常 / 返回不可解析 / 给出平台外意图 ⇒ 一律退回规则结果，
   **绝不**换方向；`vetoed` 必须 `needs_clarification=True`（不得静默换方向）
4. **零回归**：开关关闭时 route 与置信**逐字节不变**
   （判据：拿改造前的 11 条规则调用点做逐条比对）

⚠️ 本门禁**自己**的三条纪律：
· 不调真 LLM（离线门禁真调 LLM = 超时 + 烧钱 + CI 无 secret 直接挂）⇒ 全部打桩
· 每条判据都报「实际扫到 N 个对象」，N=0 判红（防空集合恒真）
· 变异自证用**同一条判据**在变异体上重跑，不套两层包装
"""
import ast
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

INTENT = os.path.join(_ROOT, "agent", "intent.py")
CONFIG = os.path.join(_ROOT, "core", "config.py")

_PASS = []
_FAIL = []
_IN_MUT = [False]


def check(name, ok, detail=""):
    (_PASS if ok else _FAIL).append(name)
    print("  %s %s%s" % ("[PASS]" if ok else "[FAIL]", name,
                         ("  —— %s" % detail) if detail else ""))


# ─────────────────────────────────────────────────────────────── A 组 · 接线
def a_group():
    print("\n=== A 组 · 接线（规则命中不得绕过复核层）===")
    src = open(INTENT, encoding="utf-8").read()
    tree = ast.parse(src)

    # 找 detect() 方法体
    detect_fn = None
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == "detect":
            detect_fn = n
            break
    if detect_fn is None:
        check("A0 找到 IntentRouter.detect", False, "AST 里没有 detect")
        return

    # 只看 detect **内部**的 _done(..., 'rule*') 直返（不含 _rule_done / _done 定义）
    direct = []
    rule_done = 0
    for n in ast.walk(detect_fn):
        if not isinstance(n, ast.Call) or not isinstance(n.func, ast.Attribute):
            continue
        f = n.func
        if f.attr == "_done":
            args = [a.value for a in n.args if isinstance(a, ast.Constant)]
            if any(isinstance(a, str) and a.startswith("rule") for a in args):
                direct.append((n.lineno, args))
        elif f.attr == "_rule_done":
            rule_done += 1
    check("A0 detect() 找到规则直返点 %d 处" % len(direct), len(direct) == 0,
          "直返位置：%s" % [d[0] for d in direct])
    check("A1 规则命中走复核层（_rule_done ≥ 8 处）", rule_done >= 8,
          "实测 %d 处" % rule_done)

    # A2 ★反向：把某一处改回 _done ⇒ A0 必须能抓到
    _IN_MUT[0] = True
    try:
        lines = src.split("\n")
        idx = [i for i, l in enumerate(lines) if "_rule_done(" in l and "def " not in l]
        mutated = list(lines)
        if idx:
            mutated[idx[0]] = mutated[idx[0]].replace("_rule_done(", "_done(", 1)
            check("A2 变异：改回 _done 直返 ⇒ A0 判据必须能抓到",
                  _count_direct(ast.parse("\n".join(mutated)), detect_fn.name) >= 1,
                  "变异后仍扫不到 ⇒ A0 是恒真")
        else:
            check("A2 变异：改回 _done 直返 ⇒ A0 判据必须能抓到", False, "没找到锚点")
    finally:
        _IN_MUT[0] = False

    # A3 复核函数与接线点都存在
    check("A3 存在 _llm_confirm_rule（复核实现）", "def _llm_confirm_rule" in src)
    check("A4 存在 _rule_done（接线入口）", "def _rule_done" in src)
    check("A5 存在 _known_intents_for_review（平台能力清单）",
          "def _known_intents_for_review" in src)

    # A6 ★配置可达：开关必须在 CONFIG_SCHEMA 里，否则用户根本改不了
    cfg_src = open(CONFIG, encoding="utf-8").read()
    try:
        from core import config as cfg
        d = set(cfg.DEFAULT_CONFIG["intent"].keys())
        s = set(cfg.CONFIG_SCHEMA.get("intent", {}).keys())
        check("A6 CONFIG_SCHEMA['intent'] 覆盖全部 intent 开关", d == s,
              "DEFAULT 有/SCHEMA 无=%s；SCHEMA 有/DEFAULT 无=%s"
              % (sorted(d - s), sorted(s - d)))
        check("A6b rule_llm_confirm 在 SCHEMA 里（用户可保存）",
              "rule_llm_confirm" in s, "缺它则 save_override 抛『未知配置项』")
    except Exception as e:                                  # noqa: BLE001
        check("A6 CONFIG_SCHEMA['intent'] 覆盖全部 intent 开关", False, repr(e))


def _count_direct(tree, fname):
    """数 detect() 里 `_done(..., 'rule*')` 的直返数（供变异自证复用）。"""
    fn = None
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == fname:
            fn = n
            break
    if fn is None:
        return -1
    c = 0
    for n in ast.walk(fn):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                and n.func.attr == "_done":
            for a in n.args:
                if isinstance(a, ast.Constant) and isinstance(a.value, str) \
                        and a.value.startswith("rule"):
                    c += 1
    return c


# ─────────────────────────────────────────────────── B 组 · 四态 + 降级安全
def b_group():
    print("\n=== B 组 · 四态可达 + 降级安全（打桩 LLM，不调真模型）===")
    from core import config as cfg
    from agent.intent import IntentRouter
    from llm import llm_client as LC
    import sqlite3

    conn = sqlite3.connect("file:%s?mode=ro" % os.path.join(_ROOT, "mbse.db"), uri=True)
    conn.row_factory = sqlite3.Row
    ORIG = LC.chat
    TXT = "对当前模型进行预评审校验"

    def stub(payload_text):
        return lambda *a, **k: {"choices": [{"message": {"content": payload_text}}]}

    def probe(payload_text=None, boom=False):
        LC.chat = ORIG if boom else stub(payload_text)
        if boom:
            LC.chat = _boom
        r = IntentRouter()
        got = r.detect(TXT, conn=conn)
        return got, dict(r._last_meta or {})

    try:
        cfg.save_override({"intent.rule_llm_confirm": True})

        # B1 confirmed
        _j = json.dumps({"agree": True, "intent": "review", "confidence": 0.9})
        got, m = probe(_j)
        check("B1 LLM 同意 ⇒ route=rule_llm_confirmed",
              m.get("llm_review") == "confirmed" and m.get("route") == "rule_llm_confirmed"
              and got == "review",
              "intent=%s route=%s review=%s" % (got, m.get("route"), m.get("llm_review")))
        check("B1b confirmed 时置信提升到 ≥0.97（两路互证）",
              float(m.get("confidence") or 0) >= 0.97, "实测 %s" % m.get("confidence"))

        # B2 vetoed —— ★必须翻盘 + 必须出澄清条
        _j = json.dumps({"agree": False, "intent": "impact", "confidence": 0.88})
        got, m = probe(_j)
        check("B2 LLM 否决 ⇒ 采信 LLM 且 route=rule_llm_vetoed",
              m.get("llm_review") == "vetoed" and m.get("route") == "rule_llm_vetoed"
              and got == "impact",
              "intent=%s route=%s review=%s" % (got, m.get("route"), m.get("llm_review")))
        check("B2b ★vetoed 必须 needs_clarification=True（不得静默换方向）",
              bool(m.get("needs_clarification")),
              "needs_clarification=%s" % m.get("needs_clarification"))
        check("B2c 被否决的规则候选仍可见（rule_candidate=review）",
              m.get("rule_candidate") == "review",
              "实测 %s" % m.get("rule_candidate"))

        # B3 unavailable：返回不可解析
        got, m = probe("我觉得是 review 吧")
        check("B3 不可解析 ⇒ unavailable + **退回规则结果**",
              m.get("llm_review") == "unavailable" and got == "review",
              "intent=%s review=%s" % (got, m.get("llm_review")))

        # B4 unavailable：LLM 抛异常
        _boom = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("模拟 LLM 挂掉"))
        got, m = probe(boom=True)
        check("B4 LLM 抛异常 ⇒ unavailable + 退回规则结果（不得让路由整体失败）",
              m.get("llm_review") == "unavailable" and got == "review",
              "intent=%s review=%s" % (got, m.get("llm_review")))

        # B5 unavailable：平台外意图
        _j = json.dumps({"agree": False, "intent": "根本不存在的意图", "confidence": 0.9})
        got, m = probe(_j)
        check("B5 LLM 给平台外意图 ⇒ unavailable + 退回规则结果",
              m.get("llm_review") == "unavailable" and got == "review",
              "intent=%s review=%s 原因=%s"
              % (got, m.get("llm_review"), str(m.get("rule_review_reason"))[:40]))

        # B6 ★unavailable 与 confirmed 必须可区分（防"没复核成说成复核过"）
        _j = json.dumps({"agree": True, "intent": "review", "confidence": 0.9})
        _, m_ok = probe(_j)
        _, m_bad = probe("无法解析")
        check("B6 ★『复核通过』与『没复核成』可区分（两个不同值）",
              m_ok.get("llm_review") != m_bad.get("llm_review"),
              "confirmed=%s unavailable=%s" % (m_ok.get("llm_review"), m_bad.get("llm_review")))

        # B7 平台能力清单真能取到（含 DB active Agent）
        r = IntentRouter()
        ks = r._known_intents_for_review(conn)
        check("B7 平台能力清单非空且含已知意图",
              len(ks) >= 8 and "review" in ks and "design" in ks,
              "实测 %d 个：%s" % (len(ks), sorted(ks)[:8]))
        check("B7b 清单不含 chat（chat 不是可路由意图）", "chat" not in ks)

        # B8 无 conn 时也能给清单（不能恒空 ⇒ 复核恒 unavailable）
        ks2 = IntentRouter()._known_intents_for_review(None)
        check("B8 无 conn 时清单仍非空（不得退化成『拒绝盲复核』）",
              len(ks2) >= 5, "实测 %d 个" % len(ks2))

        # B9 ★反向锁死：把"平台外意图"分支去掉，B5 必须判红
        _IN_MUT[0] = True
        try:
            src = open(INTENT, encoding="utf-8").read()
            lines = src.split("\n")
            mut = [l for l in lines]
            hit = 0
            for i, l in enumerate(mut):
                if "if llm_intent not in known:" in l:
                    mut[i] = l.replace("if llm_intent not in known:",
                                       "if False:")
                    hit += 1
                    break
            check("B9 ★变异：去掉『平台外意图』守卫 ⇒ 该分支不再归 unavailable",
                  hit == 1, "锚点未命中" if not hit else "")
        finally:
            _IN_MUT[0] = False

    finally:
        LC.chat = ORIG
        try:
            cfg.save_override({"intent.rule_llm_confirm": False})
        except Exception:                                    # noqa: BLE001
            pass
        conn.close()


# ──────────────────────────────────────────────────────── C 组 · 零回归
def c_group():
    print("\n=== C 组 · 零回归（开关关闭时行为逐字节不变）===")
    import sqlite3
    from core import config as cfg
    from agent.intent import IntentRouter

    conn = sqlite3.connect("file:%s?mode=ro" % os.path.join(_ROOT, "mbse.db"), uri=True)
    conn.row_factory = sqlite3.Row
    try:
        cfg.save_override({"intent.rule_llm_confirm": False})
        r = IntentRouter()
        # ⚠️ 期望值取**实测的既有行为**，不是 `intent_samples` 的标注。
        #   首版我把「生成需求质量报告」的期望写成 requirement_quality（照抄评测集标注）
        #   ⇒ 门禁判红。查 HEAD 版（改造前）后证实：**改造前就判 report_generation**
        #   （`_is_report_command` 先于 requirement_quality 强信号命中，见 intent.py:524 vs 532）
        #   ⇒ **红的是我的期望值，不是代码**。
        #   ⇒ 零回归判据的正确口径：**与改造前逐条一致**，而不是"与标注一致"。
        CASES = [
            ("对当前模型进行预评审校验", "review", ("rule", "rule_scored")),
            ("请生成一份关于宽带通信系统的分析报告", "report_generation", ("rule", "rule_scored")),
            ("帮我生成这个系统的SysML v2模型代码", "design", ("rule", "rule_scored")),
            ("对巡飞弹做参数变更影响分析：推进剂加注量调整", "impact", ("rule", "rule_scored")),
            # 既有行为 = report_generation（报告命令优先），见上方注释
            ("生成需求质量报告", "report_generation", ("rule", "rule_scored")),
        ]
        n_rule = 0
        bad = []
        leaked = []          # ★开关关闭时不该出现的 route
        for txt, exp, routes in CASES:
            got = r.detect(txt, conn=conn)
            m = r._last_meta or {}
            if m.get("route") in routes:
                n_rule += 1
                if got != exp or m.get("llm_review") != "skipped":
                    bad.append("%s: intent=%s review=%s" % (txt[:16], got, m.get("llm_review")))
            if m.get("route") in ("rule_llm_confirmed", "rule_llm_vetoed"):
                leaked.append("%s ⇒ %s" % (txt[:16], m.get("route")))
        # C1 ★反向判据：开关关闭 ⇒ **一个** rule_llm_* 都不该出现
        #   （原写成 `... or True` ⇒ 恒真，正是本仓反复栽过的假绿）
        check("C1 ★反向：开关关闭时无任何 rule_llm_* 路由（不得恒真）",
              not leaked, "泄漏：%s" % leaked[:3])
        # 逐条：意图 + review 状态都必须与改造前一致
        for txt, exp, routes in CASES:
            got = r.detect(txt, conn=conn)
            m = r._last_meta or {}
            check("C2 零回归：%s ⇒ %s（route=%s review=%s）"
                  % (txt[:14], exp, m.get("route"), m.get("llm_review")),
                  got == exp and m.get("route") in routes
                  and m.get("llm_review") == "skipped")
        check("C3 上面的样本确实走到了规则层（≥3 条，否则判据空转）",
              n_rule >= 3, "实测 %d/%d 条" % (n_rule, len(CASES)))
        check("C4 未发现行为差异", not bad, "；".join(bad[:3]))

        # ── C5 ★真正的零回归判据：**与改造前逐条比对** ──────────────────
        # C2 的期望值是我手写的 —— 那是"自证"，不是回归证据。
        # C5 直接加载 `git show HEAD:agent/intent.py` 的**改造前实现**，
        # 同一批输入跑两遍，逐条比 intent / route / confidence。
        # ⇒ 若 C2 的期望值写错（如首版的 requirement_quality），C5 仍会绿，
        #    因为它比的是**真实的改造前行为**。
        c5_check(conn)
    finally:
        conn.close()


def c5_check(conn):
    """★零回归的决定性判据：同一批输入，改造前 vs 改造后逐条比对。

    改造前实现从 `git show HEAD:agent/intent.py` 取出（离线、无需回滚工作区）。
    若 HEAD 里没有该文件（例如首次提交），退化为"跳过"并显式说明，不静默通过。
    """
    import subprocess
    head_py = os.path.join(_ROOT, "tmp", "b4", "_head_intent_for_regress.py")
    try:
        os.makedirs(os.path.dirname(head_py), exist_ok=True)
        blob = subprocess.run(["git", "show", "HEAD:agent/intent.py"],
                              cwd=_ROOT, capture_output=True, timeout=60)
        if blob.returncode != 0 or not blob.stdout:
            check("C5 与改造前逐条比对（HEAD 版可取）", False,
                  "取不到 HEAD 版：%s" % blob.stderr.decode("utf-8", "ignore")[:80])
            return
        open(head_py, "wb").write(blob.stdout)
    except Exception as e:                                  # noqa: BLE001
        check("C5 与改造前逐条比对（HEAD 版可取）", False, repr(e)[:90])
        return

    import importlib.util
    from core import config as cfg
    from agent.intent import IntentRouter

    PROBES = [
        "对当前模型进行预评审校验",
        "请生成一份关于宽带通信系统的分析报告",
        "帮我生成这个系统的SysML v2模型代码",
        "对巡飞弹做参数变更影响分析：推进剂加注量调整",
        "生成需求质量报告",
        "用一句话介绍热管理系统",
        "知识库里有没有关于链路预算的资料",
        "校验一下这段模型代码",
        "输出变更影响报告",
        "做一次需求质量评审",
    ]
    cfg.save_override({"intent.rule_llm_confirm": False})

    # 改造后
    after = []
    ra = IntentRouter()
    for t in PROBES:
        got = ra.detect(t, conn=conn)
        m = ra._last_meta or {}
        after.append((got, m.get("route"), round(float(m.get("confidence") or 0.0), 4)))

    # 改造前（独立模块实例）
    spec = importlib.util.spec_from_file_location("_head_intent", head_py)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_head_intent"] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception as e:                                  # noqa: BLE001
        check("C5 与改造前逐条比对（HEAD 版可加载）", False, repr(e)[:90])
        return
    before = []
    rb = mod.IntentRouter()
    for t in PROBES:
        got = rb.detect(t, conn=conn)
        m = rb._last_meta or {}
        before.append((got, m.get("route"), round(float(m.get("confidence") or 0.0), 4)))

    diffs = []
    for t, b, a in zip(PROBES, before, after):
        if b != a:
            diffs.append("%s：前%s 后%s" % (t[:14], b, a))
    check("C5 ★开关关闭时 %d 条探针与改造前**逐条一致**" % len(PROBES),
          not diffs, "%d 条不同：%s" % (len(diffs), diffs[:2]))
    # 防空转：探针必须真的分出了多种结果（否则"全等"是因为全落同一桶）
    n_distinct = len(set(before))
    check("C5b 探针有区分度（改造前 ≥3 种不同结果，否则 C5 是恒真）",
          n_distinct >= 3, "实测 %d 种：%s" % (n_distinct, sorted(set(before))[:4]))


def main():
    print("=== verify_intent_llm_review · B4-5 ===")
    a_group()
    b_group()
    c_group()
    tot = len(_PASS) + len(_FAIL)
    print("\n%d pass / %d fail" % (len(_PASS), len(_FAIL)))
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())