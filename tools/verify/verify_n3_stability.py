"""verify_n3_stability — N3 视图展开**稳定性**实测（复现率，不是单次抽样）。
# CI-OPTIONAL: 真调 LLM 跑 8 视图 ×N 轮稳定性统计（约 40 分钟 + 费用），CI 无 LLM secret；本地跑完即出复现率。

## 为什么必须测复现率

此前两轮实测给出过**互相矛盾**的结论：
  · 一次跑8 视图：检出 `[['ibd','sequence'], ['state','parameter']]` 产出雷同；
  · 另一次跑 2 视图：同样检出 `[['sequence','parameter']]`；
  · 而单独再跑 sequence 时，产出**完全正确**（有 `message` + `event occurrence`，无 ibd 特征）。

⇒ 「模型产出与指令不符」是**间歇性**的，不是稳定缺陷。
单次跑通不能证明稳定，单次失败也不能证明缺陷
⇒ 必须每视图重复 N 轮，统计**正确率**与**雷同率**，并把两种失效分开记。

## 判据

① 前提：LLM 真实可用（响应不得含 Mock），否则直接退出非 0
② 每个视图跑 `--repeat` 轮（默认 3），逐轮：
   · **要素命中**：内容层判据不判 FAIL（该视图的必备要素在产出里）
   · **视图纯度**：产出**不含其它视图的特征关键字**（如 sequence 产出里出现 `connect`）
   · **雷同**：与本轮其它视图的产出 md5 相同
③ 汇总：每视图的正确率、以及失效类型分布

## 用法

    ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_n3_stability.py
    ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_n3_stability.py --repeat 5
    ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_n3_stability.py --views activity,state
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "tools", "verify"))

from view_content_rules import check_view  # noqa: E402

VIEWS = ["requirement", "structure", "usecase", "activity",
         "ibd", "sequence", "state", "parameter"]

REQ = ("为纯电动汽车热管理系统（EV TMS）建立 SysML v2 模型："
       "系统 EVThermalManagementSystem 下含 BatteryThermalManagement（电池热管理）、"
       "CabinThermalManagement（座舱热管理）、ThermalLoop（热回路）、"
       "HeatPumpAssembly（热泵组件）四个子系统；"
       "含 ReqEndurance（续航≥500km）、ReqColdStart（低温启动）两条需求。")

CODE_RE = re.compile(r"```(?:sysml|sysmlv2)?\s*\n(.*?)```", re.S | re.I)

#: **视图纯度**：某视图产出里若出现这些特征关键字，说明混入了别的视图。
#:
#: ⚠️ 2026-10-08 实测修正：首版把 `action` 当"活动视图专属特征"，
#:   结果 state 视图连续 2 轮被判FAIL —— 但 `action '执行系统自检';`
#:   是**状态机 do 动作的合法声明**（实测 state 产出 12 处 transition、
#:   10 处 action 声明，全部合法且 checker 判 pass）。
#:   ⇒ **判定"混入其它视图"必须用该视图的独占构造，不能用共现关键字。**
#:
#: 独占构造（各视图专有、不会出现在别处）：
#:   · use case def      → 用例视图专有
#:   · connect a to b    → IBD 专有
#:   · state/transition  → 状态机专有（但 activity 也可能写 state，故对 activity 不判）
#:   · message/occurrence→ 时序专有（occurrence 也用于 state 的 do，故只对非时序视图判）
FOREIGN_MARKERS = {
    "requirement": [r"\buse\s+case\b", r"\bconnect\b", r"\btransition\b"],
    "structure": [r"\buse\s+case\b", r"\btransition\b", r"\bmessage\b"],
    "usecase": [r"\bconnect\b", r"\btransition\b", r"\bmessage\b"],
    # activity 不判 state / action：state 的 do 动作声明本身就含 action
    "activity": [r"\buse\s+case\b", r"\bconnect\b", r"\bmessage\b"],
    "ibd": [r"\buse\s+case\b", r"\btransition\b", r"\bmessage\b"],
    "sequence": [r"\buse\s+case\b", r"\bconnect\b", r"\btransition\b"],
    # state 不判 action（do 动作声明合法）、不判 message
    "state": [r"\buse\s+case\b", r"\bconnect\b"],
    "parameter": [r"\buse\s+case\b", r"\bconnect\b", r"\btransition\b"],
}


def new_conv(conn, title, intent):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur = conn.execute(
        "INSERT INTO conversations (title, intent, status, user_id, phase, "
        "created_at, updated_at, project_id, current_intent, last_slots, pending_clarify) "
        "VALUES (?,?,'active',NULL,'requirement',?,?,'',?,'{}','')",
        (title, intent, ts, ts, intent))
    conn.commit()
    return int(cur.lastrowid)


def last_code(conn, conv):
    rows = conn.execute(
        "SELECT content FROM messages WHERE conversation_id=? AND role='assistant' "
        "ORDER BY id", (conv,)).fetchall()
    blocks = []
    for r in rows:
        blocks.extend(m.group(1) for m in CODE_RE.finditer(r[0] or ""))
    blocks = [b for b in blocks if b.strip() and len(b.strip()) > 30]
    return blocks[-1] if blocks else ""


def assert_llm_real(tag=""):
    """LLM 必须真实可用。**每次调用前都要探一次**，不能只在开头探。

    ★ 2026-10-08 实测踩坑：开头探针通过，但跑到一半**余额耗尽**（402
    Insufficient Balance）⇒ 后续每次都「静默回落 Mock」，产出 274 字符的
    占位文本（`（Mock 回答）已收到你的消息…`）。
    只探一次的话，那一轮数据会全部作废却仍被计入统计
    （实测 24 次统计里有一整轮 8 次全是 Mock 产物，伪装成"8 视图产出雷同"）。

    ⇒ 每轮开工前探一次，探不过就**立即中止整轮**并如实说明，
      绝不把 Mock 产出当真实数据统计。
    """
    from llm import llm_client
    try:
        r = llm_client.chat(
            [{"role": "user", "content": "只回复两个字：可用"}], _intent="probe")
    except Exception as exc:                # noqa: BLE001
        print(f"[FAIL]{tag} LLM 调用异常：{type(exc).__name__}: {exc}")
        return False
    txt = (r.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    if "Mock" in txt:
        print(f"[FAIL]{tag} LLM 已回落 Mock（响应={txt[:60]!r}）")
        return False
    print(f"[OK  ]{tag} LLM 真实可用（探针={txt[:20]!r}）")
    return True


#: Mock 产物的特征串（llm_client 降级时的固定话术前缀）
MOCK_MARK = "（Mock 回答）"


def looks_mock(text: str) -> bool:
    return MOCK_MARK in (text or "")


def get_skeleton(conn, pipe):
    """复用库中已通过校验的骨架（避免每次重跑 N2）。"""
    from sysml_v2_check import check_code
    for row in conn.execute(
            "SELECT id FROM conversations WHERE title LIKE 'verify_n3_e2e_skeleton%' "
            "ORDER BY id DESC LIMIT 5").fetchall():
        code = last_code(conn, row["id"])
        if code and (check_code(code).get("n_hard") or 0) == 0:
            return code
    conv = new_conv(conn, "verify_n3_e2e_skeleton", "architecture_skeleton")
    pipe.execute(REQ, conversation_id=conv, branch="dev",
                 forced_intent="architecture_skeleton")
    return last_code(conn, conv)


def purity(view, code):
    hits = [p for p in FOREIGN_MARKERS.get(view, []) if re.search(p, code)]
    return (not hits), hits


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--views", default="")
    args = ap.parse_args()
    views = ([x.strip() for x in args.views.split(",") if x.strip()]
             or list(VIEWS))

    print("=" * 72)
    print(f"N3 稳定性实测 · 每视图重复 {args.repeat} 轮（共 {len(views)*args.repeat} 次真实调用）")
    print("=" * 72)
    if not assert_llm_real(tag="[init] "):
        return 2

    from agent.pipeline import AgentPipeline
    from database import get_db
    from sysml_v2_check import check_code

    pipe = AgentPipeline()
    pipe._load_db_agents()
    conn = get_db()

    skel = get_skeleton(conn, pipe)
    if not skel:
        print("[FAIL] 无可用骨架")
        return 1
    print(f"[OK  ] 骨架 {len(skel)} 字符")

    # 统计量
    stat = {v: {"ok": 0, "no_code": 0, "impure": 0, "fail": 0, "mock": 0,
                "detail": []} for v in views}
    dup_total = 0
    aborted = 0

    for rnd in range(1, args.repeat + 1):
        print("\n" + "-" * 72)
        print(f"第 {rnd}/{args.repeat} 轮")
        print("-" * 72)
        # ★ 每轮开工前探一次 LLM：余额可能在跑到一半时耗尽（402），
        #   那时后续全落Mock，产出会被误当成真实数据统计。
        if not assert_llm_real(tag=f"[r{rnd}] "):
            print(f"  ⛔ 第 {rnd} 轮因 LLM 不可用而中止（不计入统计）")
            aborted = rnd - 1
            break
        md5s = {}
        for v in views:
            req = (f"请基于下面的架构骨架，生成 **{v}** 视图。\n"
                   f"只引用骨架中已存在的元素，不要凭空引入部件或端口。\n"
                   f"生成后必须调用 sysml_v2_validate 校验。\n\n"
                   f"骨架：\n```sysml\n{skel}\n```")
            conv = new_conv(conn, f"verify_n3_stab_{v}_r{rnd}", "view_expansion")
            try:
                pipe.execute(req, conversation_id=conv, branch="dev",
                             forced_intent="view_expansion")
            except Exception as exc:                          # noqa: BLE001
                print(f"  {v:12} 调用异常 {type(exc).__name__}: {exc}")
                stat[v]["no_code"] += 1
                continue
            code = last_code(conn, conv)
            # ★ 产出若是 Mock 占位文本，**不计入统计**（宁可少数据，不要假数据）
            raw = conn.execute(
                "SELECT content FROM messages WHERE conversation_id=? AND role='assistant' "
                "ORDER BY id DESC LIMIT 1", (conv,)).fetchone()
            if raw and looks_mock(raw["content"] or ""):
                print(f"  {v:12} ⚠️产出是 Mock 占位（LLM 已降级），不计入统计")
                stat[v]["mock"] += 1
                continue
            if not code:
                print(f"  {v:12} 未产出代码块")
                stat[v]["no_code"] += 1
                continue
            md5s.setdefault(hashlib.md5(code.encode()).hexdigest(), []).append(v)

            con = check_view(v, code)
            pure, hits = purity(v, code)
            syn = check_code(code)
            hard = syn.get("n_hard") or 0
            good = (con["level"] != "FAIL") and pure and hard == 0

            tags = []
            if hard:
                tags.append(f"硬错{hard}")
            if con["level"] == "FAIL":
                tags.append("缺必备要素")
            if not pure:
                tags.append(f"混入{hits}")
            print(f"  [{'OK  ' if good else 'FAIL'}] {v:12} {len(code):5}字符 "
                  f"语法={syn.get('verdict')} 内容={con['level']} "
                  + (" ".join(tags) if tags else ""))
            if good:
                stat[v]["ok"] += 1
            else:
                if not pure:
                    stat[v]["impure"] += 1
                if con["level"] == "FAIL" or hard:
                    stat[v]["fail"] += 1
                stat[v]["detail"].append(
                    f"r{rnd}: 硬错{hard} 内容{con['level']} 混入{hits or '无'}")
        dups = {k: vs for k, vs in md5s.items() if len(vs) > 1}
        if dups:
            dup_total += 1
            print(f"  ⚠️ 本轮产出雷同：{list(dups.values())}")

    print("\n" + "=" * 72)
    print("汇总（正确率 = 完全合格 / **该视图的实际有效次数**；Mock 与无产出不计入分母）")
    print("=" * 72)
    eff_rounds = args.repeat - aborted
    print(f"  有效轮数：{eff_rounds}/{args.repeat}"
          + (f"（第 {aborted+1} 轮起因 LLM 不可用中止）" if aborted else ""))
    print(f"  {'视图':12} {'有效':>4} {'正确':>4} {'缺要素':>6} {'混入':>4} "
          f"{'无产出':>6} {'Mock':>5} 正确率")
    tot_ok = tot_eff = 0
    for v in views:
        st = stat[v]
        # ⚠️ 2026-10-09 实测修正（判据自身算错，出现过"12/8 = 150%"这种不可能的数）：
        #   原式 `eff = eff_rounds - st["mock"]` 用「轮数」当分母，但上方的 ok/fail/detail
        #   是**逐次调用逐次计数**的（某视图第 1 轮 Mock、第 2 轮合格时，ok 会 +1）。
        #   两个口径不一致 ⇒ 分母偏小 ⇒ 正确率可以 >100%。
        #   正解：分母 = 该视图**实际被判定过的次数**（合格 + 不合格），
        #   即 ok + fail + impure + no_code——与计数口径严格同源。
        eff = st["ok"] + st["fail"] + st["impure"] + st["no_code"]
        tot_ok += st["ok"]
        tot_eff += eff
        rate = (st["ok"] / eff * 100) if eff else 0.0
        print(f"  {v:12} {eff:>4} {st['ok']:>4} {st['fail']:>6} {st['impure']:>4} "
              f"{st['no_code']:>6} {st['mock']:>5}  {rate:5.0f}%")
        for d in st["detail"]:
            print(f"      · {d}")
    n = tot_eff
    print()
    if n == 0:
        print("  ⚠️ 有效样本为 0（LLM 全程不可用）⇒ 本次无结论，不得据此判断能力")
        return 1
    print(f"  总体：{tot_ok}/{n} = {tot_ok/n*100:.0f}% 完全合格")
    print(f"  出现产出雷同的轮次：{dup_total}/{eff_rounds}"
          f"（⚠️ 分母是有效**轮数**，与上面的次数口径不同："
          f"某轮 8 个视图各算1 次）")
    # ⚠️ 样本量下限：样本太少时"雷同 0/N"没有意义——
    #   单轮本来就必绿（首个请求的状态本就是干净的），
    #   状态泄漏这类缺陷**只有连跑才暴露**。
    if eff_rounds < 2:
        print(f"  ⚠️ 只完成 {eff_rounds} 轮 ⇒ **不足以判定稳定性**。"
              f"跨请求状态泄漏等缺陷只在连跑时暴露，单轮结果必然偏乐观。")
    if n < args.repeat * 0.5:
        print(f"  ⚠️ 有效样本 {n} 次不足计划的一半（{args.repeat} 视图 × "
              f"{args.repeat} 轮）⇒ 复现率不可信，**不得据此判断能力稳定性**。")
    if eff_rounds < args.repeat:
        print(f"  ⚠️ 只完成 {eff_rounds}/{args.repeat} 轮 ⇒ 样本不足，结论仅供参考")
    if eff_rounds < args.repeat:
        return 1
    return 0 if tot_ok == n else 1


if __name__ == "__main__":
    sys.exit(main())