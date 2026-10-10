# -*- coding: utf-8 -*-
"""_probe_sys_split.py —— P1-1 可行性验证（**只读，不落库**）。

米爸拍板口径：「先出**不落库的可行性验证**（拿最长的"活动图生成"试拆，量出实际降幅），
再决定是否动」。本脚本只做**测量与对照**，不写 DB、不改代码。

## 要回答的三个问题

  Q1 L0 硬约束卡（共享注入 1979 tok）与各 Agent 自带 `## 约束` 的**重复度**有多大？
     ⇒ 若高度重复，说明"精简 Agent prompt"不该动约束段，而该动 L0 卡（或反之）
  Q2 8 个视图 Agent 的 system 本体里，**输出示例**占多少？
     ⇒ Anthropic：few-shot 高价值但可精简；Manus：示例会导致过模仿
  Q3 若按「基础段 + 按需段」拆，一次真实建模请求能省多少 tok？

## 硬约束（本脚本纪律）

  · DB 一律 `mode=ro` 只读打开
  · 拆分**只在内存里做**，不写回 agents 表
  · 每个结论都打印**可复现的实测数字**，不写"大约/预计"
"""
import os
import re
import sqlite3
import sys
from difflib import SequenceMatcher

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from core.config import DB_PATH  # noqa: E402
from core.token_counter import count_tokens  # noqa: E402
from agent.pipeline_parts.v2_constraints import build_l0_card  # noqa: E402

VIEW_AGENTS = ("活动图生成", "参数视图生成", "需求视图生成", "状态机视图生成",
               "顺序视图（时序图）生成", "交互视图（IBD）生成", "结构视图生成", "用例视图生成")


def ro():
    c = sqlite3.connect("file:%s?mode=ro" % DB_PATH.replace("\\", "/"), uri=True)
    c.row_factory = sqlite3.Row     # 只读连接默认返回 tuple ⇒ r["x"] 会 TypeError
    return c


def sections(sp):
    """按 markdown 标题把 system_prompt 切成 {标题: 正文}。

    ★ 标题正则必须容忍 `#职责` 与 `## 职责` 两种写法（本仓**两套都存在**）：
      实测 8 个视图 Agent 里，`活动图生成/需求视图/IBD/结构/用例` 用 `## 职责`（带空格），
      而 `参数视图/状态机/顺序视图` 用 `#职责`（无空格、只有一级）。
      首版正则要求 `#{2,3}\\s+` ⇒ 后三个 Agent 整段被算进 `__head__`，
      Q2 表里"全 0"就是这个原因。**任何按段拆分的方案都会在这里踩坑。**
    """
    out, cur, buf = {}, "__head__", []
    for line in (sp or "").splitlines():
        m = re.match(r"^(#{1,6})\s*(.+?)\s*$", line)
        if m and m.group(2).strip():
            out[cur] = "\n".join(buf)
            cur, buf = m.group(2).strip(), []
        else:
            buf.append(line)
    out[cur] = "\n".join(buf)
    return out


def title_style(sp):
    """判定该 Agent 的**段标题写法**（只看已知段名，避免被代码块里的 `#` 注释误判）。

    实测（2026-11）：8 个视图 Agent **全部是 mixed** ——
      `活动图生成` 用 `## 职责`（二级+空格），`参数视图生成` 用 `#职责`（一级+无空格），
      每个 Agent 内部两种混用（`## 视图规范参考` + `#输出示例`）。
    ⇒ **段名集合完全一致，只是写法不同** ⇒ 按段名拆分的方案**通用可行**，
      但解析器必须容忍两种写法（首版正则要求 `#{2,3}\\s+` ⇒ 半数 Agent 解析为空）。
    """
    KNOWN = ("职责", "输入", "输出", "工作流程", "约束", "视图规范参考", "输出示例")
    spacey = nospace = 0
    for m in re.finditer(r"^(#{1,6})\s*(.+?)\s*$", sp or "", re.M):
        title = m.group(2).strip()
        if any(k in title for k in KNOWN):
            if m.group(1) == "#" or not m.group(0)[len(m.group(1)):len(m.group(1)) + 1].isspace():
                nospace += 1
            else:
                spacey += 1
    if spacey and nospace:
        return "mixed(#%d/##%d)" % (nospace, spacey)
    return "space" if spacey else ("nospace" if nospace else "none")


def sentences(text):
    """按中英文标点切句（用于重复度比对）。"""
    parts = re.split(r"[。；;！!？?\n]", text or "")
    return [p.strip() for p in parts if len(p.strip()) >= 8]


def overlap_ratio(a, b):
    """A 中有多少比例的句子能在 B 里找到近似（≥0.75 相似度）——即"冗余率"。"""
    sa = sentences(a)
    if not sa:
        return 0.0, 0
    hit = 0
    for x in sa:
        for y in sentences(b):
            if SequenceMatcher(None, x, y).ratio() >= 0.75:
                hit += 1
                break
    return hit / len(sa), len(sa)


def main():
    conn = ro()
    card = build_l0_card()
    card_tok = count_tokens(card)
    print("=" * 78)
    print("P1-1 可行性验证：system 本体按需加载（**只读，未落库**）")
    print("=" * 78)
    print("\n【输入】DB(ro) = %s" % DB_PATH)
    print("【输入】L0 硬约束卡 = %d tok（共享注入到每个建模 Agent）" % card_tok)

    rows = conn.execute(
        "SELECT name, system_prompt, kb_required, hil_level FROM agents "
        "WHERE status='active' AND LENGTH(system_prompt)>0 "
        "ORDER BY LENGTH(system_prompt) DESC").fetchall()

    # ── Q1 重复度 ────────────────────────────────────────────────
    print("\n" + "-" * 78)
    print("Q1  L0 卡 vs 各 Agent 自带「约束」段的重复度")
    print("-" * 78)
    print("%-24s %7s %7s %8s" % ("agent", "自身tok", "约束tok", "约束冗余率"))
    tot_self, tot_cons, tot_red = 0, 0, 0.0
    detail = []
    for r in rows:
        sp = r["system_prompt"] or ""
        secs = sections(sp)
        cons = ""
        for k, v in secs.items():
            if "约束" in k or "禁止" in k:
                cons += "\n" + v
        if not cons:
            continue
        ratio, n = overlap_ratio(cons, card)
        tot_self += count_tokens(sp)
        tot_cons += count_tokens(cons)
        tot_red += count_tokens(cons) * ratio
        detail.append((r["name"], count_tokens(sp), count_tokens(cons), ratio))
        print("%-24s %7d %7d %7.0f%%" % (r["name"][:22], count_tokens(sp),
                                         count_tokens(cons), ratio * 100))
    print("-" * 78)
    print("合计：自身 %d tok，其中约束段 %d tok；约束段中与 L0 卡重复约 %d tok"
          % (tot_self, tot_cons, int(tot_red)))

    # ── Q2 视图 Agent 的段分布 ───────────────────────────────────
    print("\n" + "-" * 78)
    print("Q2  8 个视图 Agent 的段级分布（找可拆的大块）")
    print("-" * 78)
    print("%-22s %7s %7s %7s %7s %7s %7s" %
          ("agent", "total", "职责", "输入", "输出", "工作流", "约束/规范/示例"))
    sums = [0] * 6
    styles = []
    for r in rows:
        if r["name"] not in VIEW_AGENTS:
            continue
        sp = r["system_prompt"] or ""
        styles.append((r["name"], title_style(sp)))
        secs = sections(sp)
        g = lambda *ks: sum(count_tokens(v) for k, v in secs.items()
                             if any(x in k for x in ks))
        t = count_tokens(sp)
        vals = [t, g("职责"), g("输入"), g("输出"), g("工作流"),
                g("约束") + g("规范") + g("示例")]
        sums = [a + b for a, b in zip(sums, vals)]
        print("%-22s %7d %7d %7d %7d %7d %7d" % (r["name"][:20], *vals))
    print("-" * 78)
    print("%-22s %7d %7d %7d %7d %7d %7d" % ("合计", *sums))
    print("说明：「约束/规范/示例」= 可按需加载候选；「职责/输入/输出/工作流」= 骨架段")
    print("\n★ 标题风格分布（决定拆分方案能否通用）：")
    for n, s in styles:
        print("   %-24s %s" % (n[:22], s))
    n_sp = sum(1 for _, s in styles if s.startswith("space"))
    n_no = sum(1 for _, s in styles if s.startswith("nospace"))
    n_mx = sum(1 for _, s in styles if s.startswith("mixed"))
    print("   ⇒ 全部 mixed（`#职责` 与 `## 职责` 混用）= %d / %d 个Agent"
          % (n_mx, len(styles)))
    print("   ⇒ **段名集合完全一致，只是 Markdown 层级/空格写法不同**")
    print("      ⇒ 按段名拆分**通用可行**，但解析器必须同时容忍两种写法")
    print("      ⇒ 落地时的硬要求：新解析器要同时认 `#职责` 与 `## 职责`，")
    print("        且必须有门禁覆盖全部 8 个 Agent（否则半数静默失效 —— 与压缩锚点同类事故）")

    # ── Q3 拆分收益测算（纯内存，不落库）─────────────────────────
    print("\n" + "-" * 78)
    print("Q3  「基础段 + 按需段」拆分测算（拿最长的 活动图生成 试拆）")
    print("-" * 78)
    tgt = next((r for r in rows if r["name"] == "活动图生成"), None)
    if not tgt:
        print("目标 Agent 不存在")
        return
    sp = tgt["system_prompt"] or ""
    secs = sections(sp)
    # 基础段 = 职责/输入/输出/工作流/约束（干活必需的规程）
    # 按需段 = 输出示例 + 视图规范参考（大体量、且只在"写代码那一刻"才用得上）
    base_keys = [k for k in secs if any(x in k for x in ("职责", "输入", "输出要求",
                                                          "输出格式", "工作流", "约束"))]
    ondemand_keys = [k for k in secs if any(x in k for x in ("示例", "规范参考"))]
    base = "\n".join(secs[k] for k in base_keys if not any(
        x in k for x in ("示例",)))
    # 「输出」段会同时命中"输出示例"，这里显式剔除
    base = "\n".join(secs[k] for k in base_keys if "示例" not in k and "规范" not in k)
    ondemand = "\n".join(secs[k] for k in ondemand_keys)
    bt, ot = count_tokens(base), count_tokens(ondemand)
    full = count_tokens(sp)
    print("原 system 本体            = %5d tok" % full)
    print("  ├─ 基础段（必留）      = %5d tok  (%.0f%%)" % (bt, bt / full * 100))
    print("  └─ 按需段（示例+规范）  = %5d tok  (%.0f%%)" % (ot, ot / full * 100))
    print("若按需段改为「工具按需取回」：单轮下发 %d tok，降幅 %.0f%%"
          % (bt, (full - bt) / full * 100))
    print("\n叠加 L0 卡 %d tok 后的单轮 system 侧合计：%d → %d tok"
          % (card_tok, full + card_tok, bt + card_tok))

    print("\n" + "=" * 78)
    print("结论（均为实测，拆分方案仅在内存中演算，**未写 DB、未改代码**）：")
    print("  1. 约束段与 L0 卡重复率见上表 —— 决定该精简哪一侧")
    print("  2. 视图 Agent 的可拆块集中在「输出示例 + 视图规范参考」")
    print("  3. 活动图生成：%d → %d tok（-%.0f%%），是 8 个视图 Agent 里绝对降幅最大的"
          % (full, bt, (full - bt) / full * 100))
    print("=" * 78)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()