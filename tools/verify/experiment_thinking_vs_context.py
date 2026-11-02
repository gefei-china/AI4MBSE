"""真实调用实验：思考强度 / 上下文设置 在**任务执行**时的实际差异（2026-10-31）。

为什么必须走 OpenAICompatProvider.chat 而不是手搓 httpx：
  要验证的是**我方实现**（档位→字段映射、正文保底、覆盖分层）真的影响上游行为，
  手搓请求只能证明"上游支持某字段"，证明不了"我们的代码用对了"。

三轮实验：
  E1 思考强度（同一道题，5 档）        —— 看 reasoning_tokens / 耗时 / 答案正确性
  E2 上下文窗口（同一份长材料，3 档）  —— 看 clamp 是否截断、能否答出埋在深处的细节
  E3 单轮覆盖（同模型，配置 vs 覆盖）  —— 看"入口临时调"是否真的压过模型配置

用法：cd 仓库 && ./.venv/Scripts/python.exe -X utf8 tools/verify/experiment_thinking_vs_context.py
"""
import json
import os
import sqlite3
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from llm.providers.openai_compat import OpenAICompatProvider  # noqa: E402
from core import runtime_state as rs  # noqa: E402

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "mbse.db")


def load_provider(pid=71):
    con = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
    con.row_factory = sqlite3.Row
    r = con.execute("SELECT * FROM llm_providers WHERE id=?", (pid,)).fetchone()
    con.close()
    return dict(r)


def call(cfg, messages, mp=None, max_tokens=4096, overrides=None):
    """走我方 provider 真实调用，返回 (耗时s, 正文, reasoning_tokens, meta)。"""
    c = dict(cfg)
    c["model_params"] = json.dumps(mp or {"thinking_mode": "模型默认"}, ensure_ascii=False)
    p = OpenAICompatProvider(c)
    rs.set_llm_overrides(overrides)
    try:
        t = time.time()
        d = p.chat(messages, max_tokens=max_tokens)
        dt = time.time() - t
    finally:
        rs.set_llm_overrides(None)
    msg = (d.get("choices") or [{}])[0].get("message", {}) or {}
    meta = d.get("_meta") or {}
    u = d.get("usage") or {}
    rt = ((u.get("completion_tokens_details") or {}).get("reasoning_tokens"))
    return round(dt, 2), (msg.get("content") or ""), rt, meta


# ── E1：思考强度 ───────────────────────────────────────────────────────────
# 题目选"需要多步验算"的，否则强弱档都秒答、看不出差异（这是设计实验的常识：
#   区分度来自任务难度，不是来自参数名）。
TASK1 = ("某系统有 3 个冗余通道，单通道可用度 0.98，切换成功率 0.995。"
         "求系统（2oo3 表决）的可用度，并给出中间步骤。")

LEVELS = ["关闭", "低", "中", "高"]
REPEAT = 2


def e1(cfg):
    print("\n" + "=" * 100)
    print("E1 思考强度：同一道多步计算题，走我方 5 档映射（重复 %d 次取均值）" % REPEAT)
    print("=" * 100)
    print("%-8s %-22s %-30s %-9s %-9s %s" % ("档位", "实际下发(applied)", "reasoning tokens", "均值", "耗时均值", "正文长度"))
    rows = []
    for lv in ["模型默认"] + LEVELS:
        mp = {"thinking_mode": "模型默认"} if lv == "模型默认" else {
            "thinking_mode": "关闭" if lv == "关闭" else "开启", "thinking_level": lv}
        rts, dts, lens, applied = [], [], [], ""
        for _ in range(REPEAT):
            dt, content, rt, meta = call(cfg, [{"role": "user", "content": TASK1}], mp=mp)
            rts.append(rt or 0)
            dts.append(dt)
            lens.append(len(content or ""))
            applied = (meta.get("thinking") or {}).get("applied", "-")
        rows.append((lv, applied, rts, round(statistics.mean(rts), 1),
                     round(statistics.mean(dts), 2), round(statistics.mean(lens))))
        print("%-8s %-22s %-30s %-9s %-9s %s" % (lv, applied, str(rts),
                                                 rows[-1][3], rows[-1][4], rows[-1][5]))
    return rows


# ── E2：上下文窗口 ─────────────────────────────────────────────────────────
# 造一份"针在深处"的长材料：关键事实埋在第 N 段中间，不在首尾。
# 判据：答对 = 模型真的看进去了；答错/说不知道 = 被截断或没读到。
def build_long_doc(n_sections=60, needle_at=52):
    parts = []
    for i in range(1, n_sections + 1):
        if i == needle_at:
            parts.append(f"## 第{i}节 关键约束\n本系统的极限工作温度必须不高于 -42.5 ℃，"
                         f"且该指标为强制项（编号 SYS-THERM-0042）。")
        else:
            parts.append(f"## 第{i}节 一般说明\n本节描述第{i}个子系统的常规设计约定与接口边界，"
                         f"内容用于占位以拉长上下文，不含强制指标。")
    return "\n\n".join(parts), needle_at


def e2(cfg):
    print("\n" + "=" * 100)
    print("E2 上下文窗口：同一份长材料，比较 8K / 32K / 128K 三档能否读到埋在深处的强制指标")
    print("=" * 100)
    doc, needle = build_long_doc()
    est_tokens = len(doc) // 2       # 中文约 2 字符/token 的粗估，仅用于说明规模
    print("材料规模：%d 字符 ≈ %d tokens（关键事实埋在第 %d/%d 节）" % (len(doc), est_tokens, needle, 60))
    q = (doc + "\n\n---\n问题：上面材料里提到的强制温度指标是多少？指标编号是什么？"
               "如果你没有看到该指标，请直接回答“未看到”，不要猜测。")
    print("%-10s %-10s %-12s %-10s %-10s %s" % ("ctx档位", "max_tok", "估算输入tok", "耗时s", "guard", "是否答对"))
    rows = []
    for cw in (8192, 32768, 131072):
        ov = {"context_window": cw}
        dt, content, rt, meta = call(cfg, [{"role": "user", "content": q}],
                                     mp={"thinking_mode": "关闭"}, max_tokens=1024, overrides=ov)
        ok = ("-42.5" in (content or "")) and ("SYS-THERM-0042" in (content or ""))
        rows.append((cw, meta.get("max_tokens_effective"), meta.get("est_input_tokens"),
                     dt, meta.get("context_guard"), ok, (content or "")[:60].replace("\n", " ")))
        print("%-10s %-10s %-12s %-10s %-10s %s" % (cw, meta.get("max_tokens_effective"),
                                                    meta.get("est_input_tokens"), dt,
                                                    meta.get("context_guard"),
                                                    "✅ 是" if ok else "❌ 否"))
        print("           回答首句：%s" % rows[-1][6])
    return rows


# ── E3：单轮覆盖 vs 模型配置 ───────────────────────────────────────────────
def e3(cfg):
    print("\n" + "=" * 100)
    print("E3 单轮覆盖：模型配置=关闭思考，本轮临时改「高」——看覆盖是否压过配置")
    print("=" * 100)
    base_mp = {"thinking_mode": "关闭", "thinking_level": "关闭"}
    dt1, c1, rt1, m1 = call(cfg, [{"role": "user", "content": TASK1}], mp=base_mp)
    print("① 用模型配置（关闭）      : reasoning=%-5s 耗时=%-6s applied=%s"
          % (rt1, dt1, (m1.get("thinking") or {}).get("applied")))
    ov = {"thinking_level": "高"}
    dt2, c2, rt2, m2 = call(cfg, [{"role": "user", "content": TASK1}],
                            mp=base_mp, overrides=ov)
    print("② 本轮覆盖=高（配置仍关闭）: reasoning=%-5s 耗时=%-6s applied=%s"
          % (rt2, dt2, (m2.get("thinking") or {}).get("applied")))
    print("   → 结论：%s" % ("✅ 覆盖生效（思考被打开）" if (rt2 or 0) > 0 and not (rt1 or 0)
                              else "❌ 覆盖未生效（仍是关闭）"))
    # 上下文覆盖是否压过 DB 的 128000
    dt3, c3, rt3, m3 = call(cfg, [{"role": "user", "content": "只回复：可以"}],
                            mp={"thinking_mode": "关闭"}, max_tokens=512,
                            overrides={"context_window": 4096})
    print("③ DB context_window=128000，本轮覆盖=4096 : meta.context_window=%s（应为 4096）"
          % m3.get("context_window"))
    print("   → 结论：%s" % ("✅ 覆盖生效" if m3.get("context_window") == 4096 else "❌ 覆盖未生效"))
    return (rt1, rt2, m3.get("context_window"))


def main():
    cfg = load_provider(71)
    print("实验对象：provider#%s %s / model=%s / base=%s"
          % (cfg["id"], cfg["name"], cfg["model_name"], cfg["base_url"]))
    r1 = e1(cfg)
    r2 = e2(cfg)
    r3 = e3(cfg)

    print("\n" + "=" * 100)
    print("结论摘要")
    print("=" * 100)
    print("E1 思考强度：按 reasoning tokens 均值排序 ——")
    for lv, applied, rts, mean_rt, mean_dt, mean_len in r1:
        print("   %-8s applied=%-20s reasoning均值=%-7s 耗时均值=%ss" % (lv, applied, mean_rt, mean_dt))
    print("E2 上下文窗口：%s" % ("；".join(
        "ctx=%s→%s" % (r[0], "答对" if r[5] else "未答对") for r in r2)))
    print("E3 单轮覆盖：配置关闭 reasoning=%s，覆盖为高 reasoning=%s，ctx覆盖回读=%s" % r3)
    return 0


if __name__ == "__main__":
    sys.exit(main())
