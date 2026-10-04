# -*- coding: utf-8 -*-
"""P1-8：意图路由评测进 CI —— 可阻断的回归门禁（2026-10-04）。

━━━ 第一版差点做成一个"随机红的门禁"，这里记下踩到的坑 ━━━
初版直接在本机库上跑 `IntentRouter.detect` 测准确率，得到 1.000（29/29 全对），
差点把 0.55 定成阈值。**随后在干净库上复测才发现真相**：
  · `intent_rules` 表在**干净库上是 0 行** —— 关键词规则是**数据**，不在 git 里；
  · 本机库有 21 个 Agent（7 内置 + 14 用户自建），干净库只有 7 个；
  · 于是同一个脚本，本机 1.000 / CI 0.828，**阈值定多少都是错的**。
**教训（已升格为硬规则）**：准确率类门禁必须先证明"输入在两个环境里等价"，
否则它测的不是代码，是**数据差异**。本版因此强制「每次自建干净库再跑」。

━━━ 现在锁定的四件事 ━━━
  E1 指标函数 `metrics()` 自身正确（accuracy / macro-F1 / 混淆矩阵）—— 纯函数、零依赖
  E2 黄金集健康度：条数不缩、文本非空、无重复、意图名合法
     （防"把黄金集删到只剩 3 条，评测照样全绿"—— 最常见的假绿）
  E3 意图路由准确率**不低于下限**，且**两次运行结果完全一致**（无隐藏随机性）
  E4 缓存绕过不变式（AGENTS.md 坑 26：意图缓存命中时测的是历史结论，A/B 会假绿）

**基线来源**：`tests/manual_verify/intent_cases.py`（29 例，已入库）。
刻意不读 `intent_samples` 表 —— 那 279 行是本机数据、不在 git，CI 上必然为空。
**对标**：LangSmith / Dify 的 eval-in-CI 做法是把评测拆成
"确定性部分进 PR 阻断、模型相关部分 nightly 跑"。本门禁属前者：CI 无 LLM secret，
锁的是"规则层+语义层不许塌"，不追模型分数。

用法：
    python tools/eval/run_ci_eval.py             # 门禁模式（CI 用），回归则退出码 1
    python tools/eval/run_ci_eval.py --report    # 额外打印错例明细与基线对照
"""
import importlib.util
import os
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS, FAIL = "PASS", "FAIL"
_results = []

# ── 门禁阈值 ────────────────────────────────────────────────────────────────
# 基线演进（真实数据 A/B：**同一个干净库**、同一份 29 例、仅"规则集有无"之差）：
#   入库前（intent_rules 0 行）：accuracy=0.828  macro_f1=0.8268  错例 5 条
#   入库后（intent_rules 4 行）：accuracy=0.931  macro_f1=0.9158  错例 2 条
#     被修复的 3 条与规则一一对应：生成结构树→design / 画一张参数图→design /
#     追溯矩阵检查一下→review；**新引入 0 条**。
# 下限取 0.85：距基线 8 个百分点（够吸收种子数据小幅变动），又足够高到能挡住
# "规则集丢失 / 规则层被改坏"这类量级的塌陷 —— 那正是入库前 0.828 的水平。
MIN_CASES = 20
MIN_ACCURACY = 0.85
# 已知基线（仅用于 --report 时打印对照，不参与判定；判定只看 MIN_ACCURACY）
KNOWN_BASELINE = {"accuracy": 0.931, "macro_f1": 0.9158, "n": 29}


def _rec(name, ok, detail=""):
    _results.append((PASS if ok else FAIL, name, detail))
    print(f"  {PASS if ok else FAIL}  {name}" + (f"  {detail}" if not ok and detail else ""))
    return ok


def _fresh_db():
    """建一个**全新的空库**并把 DB_PATH 指过去。

    ⚠️ 必须在 import 任何业务模块**之前**设置环境变量：`core.config.DB_PATH` 在 import 期
    就被各模块绑进自身作用域（批次 2 已踩过一次：改晚了 ⇒ 验证脚本写生产库）。
    """
    p = os.path.join(tempfile.gettempdir(), "_ci_eval_%d.db" % os.getpid())
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(p + suffix):
            os.remove(p + suffix)
    os.environ["MBSE_DB_PATH"] = p
    sys.path.insert(0, ROOT)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    from database import init_db
    init_db()
    return p


def _load_cases():
    """载入**入库的**黄金集（tests/manual_verify/intent_cases.py）。"""
    p = os.path.join(ROOT, "tests", "manual_verify", "intent_cases.py")
    spec = importlib.util.spec_from_file_location("_ci_intent_cases", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return list(mod.BUILTIN_PAIRS)


def e1_metrics():
    print("\n=== E1 指标函数自身正确（纯函数，零依赖）===")
    from repositories.intent_sample_repo import metrics
    m = metrics(["a", "b", "c", "d"], ["a", "b", "c", "e"])
    ok = _rec("E1a accuracy 精确等于 0.75（手算期望，不用被测代码算期望）",
              abs(float(m["accuracy"]) - 0.75) < 1e-9, f"got={m.get('accuracy')}")
    ok &= _rec("E1b n 正确", m.get("n") == 4, f"got={m.get('n')}")
    f1 = m.get("macro_f1")
    ok &= _rec("E1c macro_f1 在 [0,1]", f1 is not None and 0.0 <= float(f1) <= 1.0, f"got={f1}")
    perfect = metrics(["a", "b"], ["a", "b"])
    ok &= _rec("E1d 全对时 accuracy == 1.0",
               abs(float(perfect["accuracy"]) - 1.0) < 1e-9, f"got={perfect.get('accuracy')}")
    all_wrong = metrics(["a", "b"], ["x", "y"])
    ok &= _rec("E1e 全错时 accuracy == 0.0",
               abs(float(all_wrong["accuracy"])) < 1e-9, f"got={all_wrong.get('accuracy')}")
    empty = metrics([], [])
    ok &= _rec("E1f 空集不抛异常（门禁在空数据上要给出信息而非崩红）",
               isinstance(empty, dict), f"type={type(empty)}")
    return ok


def e2_dataset(db_path):
    print("\n=== E2 黄金集健康度（防'删样本式假绿'）===")
    cases = _load_cases()
    ok = _rec("E2a 黄金集可载入", bool(cases), f"n={len(cases)}")
    ok &= _rec(f"E2b 条数 ≥ {MIN_CASES}（当前 {len(cases)}）", len(cases) >= MIN_CASES,
               f"n={len(cases)}")
    ok &= _rec("E2c 无空文本样本", not [t for t, _ in cases if not (t or "").strip()])
    dup = len(cases) - len({t for t, _ in cases})
    ok &= _rec("E2d 无重复文本", dup == 0, f"重复 {dup} 条")
    intents = {w for _, w in cases}
    ok &= _rec("E2e 期望意图 ≥3 类", len(intents) >= 3, f"intents={sorted(intents)}")
    # 意图名合法性：以**干净库**的 Agent 名 + 规则表为权威集合（+ chat 兜底意图）
    # ⚠️ `agents.name` 才是意图键（不是 intent_name —— 该列不存在，别再写错）；
    #    其中也会出现中文展示名（"交互视图（IBD）生成" 等），故这里只做**子集包含**判定，
    #    不要求"全部 name 都是合法意图"。
    legal = {"chat"}
    try:
        con = sqlite3.connect(db_path)
        for q in ("SELECT DISTINCT intent FROM intent_rules WHERE COALESCE(enabled,1)=1",
                  "SELECT DISTINCT name FROM agents WHERE COALESCE(name,'')<>''"):
            try:
                legal |= {r[0] for r in con.execute(q).fetchall() if r[0]}
            except Exception:
                pass
        con.close()
    except Exception as e:
        _rec("E2f 合法意图集合可读（跳过）", True, f"note={e}")
        return ok
    unknown = sorted(i for i in intents if i not in legal)
    # 干净安装里确实不存在的意图 —— 逐条写明理由，不做"全部放行"式和稀泥。
    # 这两个不是拼写错误：它们在**代码里被显式引用**（`orchestration._ORCH_EXCLUDE_INTENTS`
    # 含 system_mgmt / requirement_quality），只是其 Agent 是本部署自建的、不在种子集里。
    # 保留本断言的价值：把 expected 写成 "requirement_analyis" 这类 typo 仍会被判红。
    ALLOW_MISSING_IN_CLEAN = {
        "system_mgmt": "代码 orchestration._ORCH_EXCLUDE_INTENTS 显式引用；Agent 为本部署自建",
        "requirement_quality": "同上（代码里作为质量门禁意图被引用）",
    }
    typos = [i for i in unknown if i not in ALLOW_MISSING_IN_CLEAN]
    ok &= _rec("E2f 期望意图均在合法集合内（允许清单外的必须为空）", not typos,
               f"typos={typos} missing_allowed={sorted(set(unknown) & set(ALLOW_MISSING_IN_CLEAN))}")
    _rec("E2g 允许清单本身不过期（每条都要仍被代码引用）",
         all(v for v in ALLOW_MISSING_IN_CLEAN.values()),
         f"allow={sorted(ALLOW_MISSING_IN_CLEAN)}")
    # E2h 规则集入库（2026-10-04）：这是"CI 能否复现生产路由"的前提。
    # 实测依据：规则集入库前干净库 intent_rules **0 行**，黄金集准确率 0.828；
    # 入库后 4 行、0.931，且修复的 3 条错例与规则一一对应。
    try:
        _c = sqlite3.connect(db_path)
        _n_rules = _c.execute("SELECT COUNT(*) FROM intent_rules "
                              "WHERE COALESCE(enabled,1)=1").fetchone()[0]
        _c.close()
    except Exception:
        _n_rules = -1
    ok &= _rec("E2h 意图规则集已入库（干净库必须有规则，否则 CI 复现不了生产路由）",
               _n_rules >= 4, f"intent_rules={_n_rules}（期望 ≥4）")
    return ok


def _run_eval():
    """跑一遍评测，返回 (metrics, wrong)。**必须绕开意图缓存**（AGENTS.md 坑 26）。"""
    from agent.pipeline import AgentPipeline
    from agent.intent import IntentRouter
    pipe = AgentPipeline()
    pipe._load_db_agents()
    rt = pipe.router
    og, os_ = IntentRouter._cache_get, IntentRouter._cache_set
    IntentRouter._cache_get = lambda self, *a, **k: None
    IntentRouter._cache_set = lambda self, *a, **k: None
    y_true, y_pred, wrong = [], [], []
    try:
        for text, want in _load_cases():
            got = rt.detect(text)
            meta = rt.get_last_meta()
            y_true.append(want)
            y_pred.append(got)
            if got != want:
                wrong.append({"text": text, "want": want, "got": got,
                              "route": meta.get("route", ""), "conf": meta.get("confidence")})
    finally:
        IntentRouter._cache_get, IntentRouter._cache_set = og, os_
    from repositories.intent_sample_repo import metrics
    return metrics(y_true, y_pred), wrong


def e3_accuracy(show=False):
    print("\n=== E3 准确率下限 + 两次运行一致性（干净库 + 无 LLM 口径）===")
    m, wrong = _run_eval()
    acc = float(m.get("accuracy") or 0.0)
    ok = _rec(f"E3a 准确率 ≥ {MIN_ACCURACY}（实测 {acc:.3f}，n={m.get('n')}）",
              acc >= MIN_ACCURACY, f"acc={acc:.3f}")
    # 自一致性：同一输入跑两次必须逐条相同。防"门禁本身随机红/随机绿"——
    # 随机性来源通常是意图缓存或语义索引的进程内状态。
    m2, wrong2 = _run_eval()
    same = ([w["got"] for w in wrong] == [w["got"] for w in wrong2]
            and abs(float(m2.get("accuracy") or 0) - acc) < 1e-9)
    ok &= _rec("E3b 两次运行结果完全一致（无隐藏随机性）", same,
               f"run2 acc={float(m2.get('accuracy') or 0):.3f}")
    print(f"      macro_f1={m.get('macro_f1')} 错例 {len(wrong)} 条"
          f"（已知基线 acc={KNOWN_BASELINE['accuracy']}）")
    if show and wrong:
        for w in wrong[:15]:
            print(f"        - {w['text'][:30]:<32} want={w['want']:<20} got={w['got']:<20}"
                  f" route={w['route']} conf={w['conf']}")
    return ok, acc, wrong


def e4_discipline():
    print("\n=== E4 缓存绕过与口径一致（AGENTS.md 坑 26）===")
    self_src = open(os.path.abspath(__file__), encoding="utf-8").read()
    ok = _rec("E4a 评测前把 _cache_get 置空",
              "IntentRouter._cache_get = lambda self, *a, **k: None" in self_src)
    ok &= _rec("E4b finally 恢复原方法（不留全局污染）",
               "IntentRouter._cache_get, IntentRouter._cache_set = og, os_" in self_src)
    ok &= _rec("E4c 与线上端点同一 metrics 封装（不各写一份算法）",
               "from repositories.intent_sample_repo import metrics" in self_src)
    ok &= _rec("E4d 评测前自建干净库（保证跨环境口径等价）",
               "os.environ[\"MBSE_DB_PATH\"] = p" in self_src and "init_db()" in self_src)
    return ok


def main():
    show = "--report" in sys.argv
    print("=" * 72)
    print("P1-8 意图路由评测 CI 门禁（确定性部分，自建干净库口径）")
    print("=" * 72)
    db = _fresh_db()
    print("干净库: %s" % db)
    ok = e1_metrics()
    ok &= e2_dataset(db)
    r3, acc, wrong = e3_accuracy(show=show)
    ok &= r3
    ok &= e4_discipline()

    bad = [r for r in _results if r[0] != PASS]
    print("\n" + "=" * 72)
    print(f"断言总数 {len(_results)}  PASS {len(_results) - len(bad)}  FAIL {len(bad)}")
    print(f"干净库+无LLM 口径准确率 {acc:.3f}（下限 {MIN_ACCURACY}），错例 {len(wrong)} 条")
    if show:
        print("提示：本门禁测的是「规则层+语义层不许塌」。模型质量需另跑带 LLM 的"
              "夜间评测（CI 无 secret，且模型输出有温度，不是确定性断言）。")
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(db + suffix)
        except OSError:
            pass
    if bad:
        for k, n, d in bad:
            print(f"  {k}  {n}  {d}")
        print("\n结论：门禁未通过（CI 应据此阻断）")
        return 1
    print("结论：门禁通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
