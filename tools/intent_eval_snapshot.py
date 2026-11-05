# -*- coding: utf-8 -*-
"""意图评测跑批器 → 产出 checked-in 快照（P0-3，2026-10-30）。

## 为什么要"跑批器 + 快照"而不是"CI 里直接跑"
与 `verify_rag_eval_gate.py` 同因：CI 跑在干净的 checkout 上，
**`mbse.db` 不入 git** ⇒ 样本池、意图缓存、Agent 配置都不在，评测无从跑起（跑出来是 0，不是"质量差"）。
所以分工固定为：
- **本脚本（nightly / 手动）**：跑真实路由，产出 `docs/评测/nightly/latest-intent-report.json`；
- **门禁（`verify_intent_eval_ci.py`）**：只做判定，消费快照，PR 上零成本。

## 三个集合，**分开报**，永不混算
| 集合 | 来源 | 标签怎么来的 | 能回答什么 | 不能回答什么 |
|---|---|---|---|---|
| `gold` | 人工确认（`status='confirmed'`） | 人 | 绝对水平（区间宽） | 细粒度回归（n 太小） |
| `silver` | 生产语句 + 双视图一致 | 两个视图的 LLM 独立标注取一致 | 回归检测（n 大、区间窄） | 绝对水平（与路由器同源 ⇒ 系统性高估） |
| `synth` | 手写改写（`source='aug'`） | 由构造决定（写这句话就是为了表达某意图） | 边界覆盖 / 改写鲁棒性 | 生产分布（措辞比真实输入规整） |

**为什么禁止混算**：三者的难度分布显著不同（实测 synth 准确率远低于 gold）。
混成一个数会得到一个"既不代表生产、也看不出回归"的指标 —— 那种数字最容易被当成成绩单。

## 用法
    python tools/intent_eval_snapshot.py                    # 跑全部集合
    python tools/intent_eval_snapshot.py --sets gold,silver # 只跑指定集合
    python tools/intent_eval_snapshot.py --limit 20         # 每集合只跑前 N 条（快速体检）
    python tools/intent_eval_snapshot.py --no-resume         # 忽略进度文件全量重算
                                          （**改了路由器实现后必须加它**，否则续跑会复用旧预测）
"""
import argparse
import collections
import hashlib
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

OUT_DIR = os.path.join(ROOT, "docs", "评测", "nightly")
OUT = os.path.join(OUT_DIR, "latest-intent-report.json")
# 断点续跑用。为什么必须有：412 条 × LLM 兜底 ≈ 10~20 分钟，
# 而**进程被杀是常态**（会话中断/机器休眠/手动 Ctrl+C）。第一版没有它，
# 实测被中断一次就白跑 166 次 detect 调用（每条都真花额度）。
PROGRESS = os.path.join(ROOT, "tmp", "intent_eval_progress.jsonl")

SETS = {
    # 金标：只有人能给的标签
    "gold": "SELECT text, intent FROM intent_samples "
            "WHERE status='confirmed' AND intent<>'' AND COALESCE(text,'')<>'' ORDER BY id",
    # 银标：生产语句 + 双视图一致（排除 aug，避免与 synth 集重叠）
    "silver": "SELECT text, intent FROM intent_samples "
              "WHERE created_by='dual_label_v1' AND intent<>'' AND COALESCE(source,'')<>'aug' "
              "AND COALESCE(text,'')<>'' ORDER BY id",
    # 合成：手写改写，标签由构造决定（用它原始的 label，不用双视图结论 —— 见 --report）
    "synth": "SELECT text, intent FROM intent_samples "
             "WHERE COALESCE(source,'')='aug' AND intent<>'' AND COALESCE(text,'')<>'' ORDER BY id",
}


# ── 意图粒度等价表（评测口径，2026-10-30）──────────────────────────────────
# 阶段意图（子 Agent）与其粗类在「路由判准」上等价：路由器按设计把
# view_expansion 折叠进 design、model_validation_repair 折叠进 review
# （见 agent/intent.py `_stage_note`）。把子 Agent 当成顶层意图来打银标，
# 会造出 0 召回的"假回归"——本轮实测 system_mgmt / view_expansion /
# model_validation_repair 三类的 0 召回**全是标注协议缺陷**，不是路由器能力下降。
# 评测只判"路由到正确的执行主体家族"，不判"是否点名了某个阶段"。
# （细粒度阶段精度是另一项指标，不在本门禁范围内，见 docs/建议1-6...md。）
INTENT_EQUIV = {
    "architecture_skeleton": "design",
    "view_expansion": "design",
    "model_release": "design",
    "change_safety_gate": "impact",
    "model_validation_repair": "review",
    "trace_verification": "review",
    "requirement_structuring": "requirement_analysis",
    "methodology_resolver": "requirement_analysis",
    "多方案生成": "design",
}
# 非路由意图：Agent 已禁用 / 不属于"活路由意图"，从评测集剔除（避免污染基线）。
#   system_mgmt：Agent 已 disabled，且其 silver 样本实为"工程查询(MCP 工具)"误标，
#   并非真正的系统管理（用户/审计/权限）。gold 里的 system_mgmt 是真标签、路由器
#   能正确产出，故**只从 silver 剔除**（见 run_set 的 `_drop`）。
NON_ROUTING_INTENTS = {"system_mgmt"}


def _norm_intent(it):
    """阶段意图折叠到粗类；非路由意图原样返回（由调用方按集合决定是否剔除）。"""
    it = (it or "").strip()
    return INTENT_EQUIV.get(it, it)


def _h(t):
    return hashlib.sha1((t or "").encode("utf-8")).hexdigest()[:12]


def _macro_f1(pairs):
    by = collections.defaultdict(lambda: [0, 0, 0])   # want -> [tp, fp, fn]
    labels = {w for w, _ in pairs} | {g for _, g in pairs}
    for w, g in pairs:
        if w == g:
            by[w][0] += 1
        else:
            by[g][1] += 1
            by[w][2] += 1
    f1s = []
    for lb in labels:
        tp, fp, fn = by[lb]
        if tp == 0 and fp == 0 and fn == 0:
            continue
        p = tp / (tp + fp) if (tp + fp) else 0.0
        r = tp / (tp + fn) if (tp + fn) else 0.0
        f1s.append(2 * p * r / (p + r) if (p + r) else 0.0)
    return round(sum(f1s) / len(f1s), 4) if f1s else 0.0


def run_set(name, sql, limit=0, resume=True):
    from agent.intent import IntentRouter
    from core.stats import wilson_ci
    from database import get_db

    c = get_db()
    rows = [(r[0], r[1]) for r in c.execute(sql).fetchall()]
    c.close()
    if limit:
        rows = rows[:limit]

    # ★ 必须清意图缓存：`intent_cache` 是**持久化 DB 表**（不是进程内存），
    #   上一次跑批/上一次线上请求留下的结论会直接短路 detect ⇒
    #   "改了代码但评测分数纹丝不动"或"改了但看不出变化"都可能源于它。
    #   清它是安全的：缓存可从原文重建（宁可多花一次调用，也不让评测读到旧结论）。
    try:
        _cc = get_db()
        try:
            _n = _cc.execute("DELETE FROM intent_cache").rowcount
            _cc.commit()
        finally:
            _cc.close()
        print("    已清意图缓存 %d 行（防旧结论短路）" % (_n or 0))
    except Exception as _e:      # noqa: BLE001
        print("    [warn] 清缓存失败（不影响评测，但可能读到旧结论）：%s" % str(_e)[:80])

    # ── 断点续跑：已完成样本直接复用结论 ──
    # 键 = (集合名, 文本摘要)。**不含 want** —— 标签若被人改过，续跑必须用**新标签**
    # 重新计分（预测不用重跑，那是模型行为，与标签无关）。这是"续跑不等于复用结论"的分界。
    done = {}
    if resume:
        try:
            with open(PROGRESS, encoding="utf-8") as f:
                for ln in f:
                    try:
                        d = json.loads(ln)
                    except Exception:
                        continue          # 半行（进程在写的时候被杀）⇒ 丢弃，下次重算
                    if d.get("set") and d.get("h"):
                        done[(d["set"], d["h"])] = d
        except FileNotFoundError:
            pass
        except Exception as _e:      # noqa: BLE001
            print("    [warn] 进度文件读取失败（当作全新跑）：%s" % str(_e)[:80])

    # ★ 必须用**生产构造**：`AgentPipeline()` + `_load_db_agents()`。
    #   为什么不能裸 `IntentRouter()`：DB Agent 关键词与**语义索引**是在真实请求路径
    #   （`stream.py: self._load_db_agents(user)`）里注入的，裸构造只有内置词表 ⇒
    #   测的是"退化的关键词层"。**实测代价**（同一 48 条 gold、同一模型）：
    #     裸 `IntentRouter()`        → 38/48 = **0.7917**
    #     `AgentPipeline`+prewarm    → 43/48 = **0.8958**（与既有 `eval_intent_routing.py` 完全一致）
    #   ⇒ 裸构造会凭空造出 10 个百分点的"质量下降"，而代码一行没改。
    from agent import AgentPipeline as _AP
    _pipe = _AP()
    _pipe._load_db_agents()
    rt = _pipe.router
    pairs, outs, t0 = [], [], time.time()
    reused = 0
    dropped = 0
    # 非路由意图只从 silver 剔除（gold 的 system_mgmt 是真标签、路由器能正确产出）。
    _drop = NON_ROUTING_INTENTS if name == "silver" else set()
    _pf = open(PROGRESS, "a", encoding="utf-8") if resume else None
    try:
        for txt, want in rows:
            h = _h(txt)
            hit = done.get((name, h))
            if hit:
                got, route, conf = hit.get("got"), hit.get("route"), hit.get("conf")
                reused += 1
            else:
                try:
                    c = get_db()
                    got = rt.detect(txt, conn=c)
                    m = rt.get_last_meta() or {}
                    c.close()
                except Exception as e:      # noqa: BLE001
                    got, m = "", {"route": "exc", "confidence": 0.0, "err": str(e)[:80]}
                route, conf = m.get("route"), round(float(m.get("confidence") or 0), 3)
                if _pf is not None:
                    _pf.write(json.dumps({"set": name, "h": h, "got": got,
                                          "route": route, "conf": conf,
                                          "want": want}, ensure_ascii=False) + "\n")
                    _pf.flush()        # 逐条 flush：被杀也只丢当条，不丢整批
            # ★ 粒度等价归一：阶段意图折叠到粗类；非路由意图按集合剔除。
            #   （见模块顶部 INTENT_EQUIV / NON_ROUTING_INTENTS 注释）
            w = _norm_intent(want)
            g = _norm_intent(got)
            if w in _drop:
                dropped += 1
                continue
            pairs.append((w, g))
            outs.append({"h": h, "want": w, "got": g,
                         "ok": bool(w == g), "route": route, "conf": conf})
            if len(outs) % 25 == 0:
                print("    … %s %d/%d（%.0fs，复用 %d）"
                      % (name, len(outs), len(rows), time.time() - t0, reused))
    finally:
        if _pf is not None:
            _pf.close()
    if reused:
        print("    复用已完成样本 %d 条（断点续跑）" % reused)
    if dropped:
        print("    剔除非路由标签 %d 条（%s，不计入评分）" % (dropped, name))

    n = len(pairs)
    k = sum(1 for w, g in pairs if w == g)
    lo, hi = wilson_ci(k, n)
    return {
        "n": n, "correct": k, "dropped": dropped, "n_raw": len(rows),
        "accuracy": round(k / n, 4) if n else 0.0,
        "ci95": [round(lo, 4), round(hi, 4)],
        "width_pts": round((hi - lo) * 100, 2) if n else 0.0,
        "macro_f1": _macro_f1(pairs),
        "confusion": {w: dict(collections.Counter(g for ww, g in pairs if ww == w))
                      for w in sorted({w for w, _ in pairs})},
        "elapsed_s": round(time.time() - t0, 1),
        # route 分布是**可诊断性**的关键：`llm/llm_fused` 占比骤降 = LLM 裁判在空转，
        # 只看 accuracy 会把"基础设施故障"误读成"模型能力下降"。
        "routes": dict(collections.Counter(str(x.get("route") or "") for x in outs)),
        "outcomes": outs,
    }


def _commit():
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"],
                                       cwd=ROOT, text=True).strip()
    except Exception:
        return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sets", default="gold,silver,synth")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--no-write", action="store_true")
    ap.add_argument("--allow-llm-down", action="store_true",
                    help="LLM 不可用时仍出报告（报告标 llm_ok=false，门禁会拒收）")
    ap.add_argument("--no-resume", action="store_true",
                    help="忽略进度文件，全部重算（改了路由器实现后必须用它）")
    a = ap.parse_args()

    names = [x.strip() for x in a.sets.split(",") if x.strip()]
    report = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
              "commit": _commit(), "db": "mbse.db", "sets": {}}
    # ⚠️ `llm.default_chat_provider` **不是配置键**（实测 `save_override` 直接抛"未知配置项"），
    #   上一版读它恒得空串 ⇒ 报告里无法判断"这份成绩是哪家模型给的"。
    #   真源是 DB：`llm_providers.is_default`。
    env = {"llm_ok": False, "provider_id": 0, "provider": "", "error": ""}
    try:
        from database import get_db as _gdb
        _c = _gdb()
        _r = _c.execute("SELECT id, name FROM llm_providers WHERE model_type='chat' "
                        "AND is_default=1 ORDER BY priority DESC LIMIT 1").fetchone()
        _c.close()
        if _r:
            env["provider_id"], env["provider"] = int(_r[0]), str(_r[1])
    except Exception as _e:      # noqa: BLE001
        env["error"] = "读默认 provider 失败：%s" % str(_e)[:80]

    # ★ LLM 预检：**LLM 不可用时拒绝出报告**。
    #   理由（本轮实测踩到）：qwen3.8-max 在跑批中途额度耗尽返回 403，
    #   路由器的 LLM 裁判静默退回启发式 ⇒ 报告变成"退化层成绩"，
    #   而**报告本身看不出任何异常**（route 里仍有 llm/llm_fused，看起来像正常跑完）。
    #   这种数字一旦进了基线，后面所有棘轮判定都失去意义。
    if not a.allow_llm_down:
        try:
            from llm import llm_client as _lc
            _lc.chat([{"role": "user", "content": "只回一个字：好"}],
                     _intent="intent_eval_preflight", max_tokens=16)
            env["llm_ok"] = True
        except Exception as _e:      # noqa: BLE001
            env["error"] = "LLM 预检失败：%s" % str(_e)[:160]
            print("[ABORT] " + env["error"])
            print("        评分会退化为启发式层，与历史基线不可比。")
            print("        修好后重跑；若**确实**要测降级层，加 --allow-llm-down（报告会标 llm_ok=false）。")
            return 2
    else:
        env["llm_ok"] = False
        env["error"] = "--allow-llm-down：显式要求测降级层"
    report["env"] = env
    report["default_chat_provider"] = env["provider"]

    for nm in names:
        if nm not in SETS:
            print("跳过未知集合：%s" % nm)
            continue
        print("\n=== 集合 %s ===" % nm)
        report["sets"][nm] = run_set(nm, SETS[nm], a.limit, resume=not a.no_resume)
        s = report["sets"][nm]
        print("  n=%d acc=%.4f CI95=[%.1f%%, %.1f%%] 宽 %.1f 点 macro-F1=%.4f  %.1fs"
              % (s["n"], s["accuracy"], s["ci95"][0] * 100, s["ci95"][1] * 100,
                 s["width_pts"], s["macro_f1"], s["elapsed_s"]))

    report["corpus_n"] = sum(v["n"] for v in report["sets"].values())
    report["corpus_n_raw"] = sum((v.get("n_raw") if v.get("n_raw") is not None else v["n"])
                                 for v in report["sets"].values())
    print("\n语料合计（评分 %d / 采集 %d）条（%s）" % (
        report["corpus_n"], report["corpus_n_raw"], ", ".join(
            "%s=%d(raw %d)" % (k, v["n"], v.get("n_raw", v["n"]))
            for k, v in report["sets"].items())))

    if a.no_write:
        print("[--no-write] 未落盘")
        return 0
    os.makedirs(OUT_DIR, exist_ok=True)
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    os.replace(tmp, OUT)
    print("已写 %s" % os.path.relpath(OUT, ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
