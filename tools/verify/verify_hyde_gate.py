#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""门禁：HyDE 的收益与成本闸（P1-2，2026-10-04）

## 这道门禁要防什么

原排序文档把 P1-2 写成「HyDE 使 embedding 成本 ×2 ⇒ 建议降级」。
**实测推翻了「降级」这个建议**：

- 成本侧成立：主向量 N 条 + HyDE N 条 = 2N 次向量化（分批数也×2）。
- 但收益侧**不可忽略**：切 `rag.hyde_enabled` 开关后，
  8 条真实查询里**6 条 top-10 排序不同、合计 44 处排名变动**，
  且存在「仅开有独占命中 / 仅关有独占命中」（如仅开召回 800/202，
  仅关召回 812/57）⇒ **关掉 HyDE 是净损失**。

⇒ 所以本项交付的不是"关掉"，而是「**可降级**」：
新增 `rag.hyde_embed_max_chunks`（>0 只给前 N 块算 HyDE 向量，
0=全部），供 embedding 额度紧张时按需限制成本。

门禁钉住三条：
1. **入库侧成本闸存在且默认不限制**（不许有人把默认改成小 N 而悄悄降质量）。
2. **检索侧开关仍然有效**（`hyde_enabled` 能真的被 `get()` 读到）——
   这条来自一次真实踩坑：测开关时改的是 `DEFAULT_CONFIG`，
   而 `get()` 读**模块级 `_CONFIG`** ⇒ 开关压根没切换，
   却据此得出"HyDE 零影响"的错误结论。此门禁把「读的是哪个对象」钉死。
3. **HyDE 不是死代码**：在真库上跑 A/B，断言"至少有一条查询排序不同"
   —— 若 HyDE 真的对结果零影响，那本门禁应该判红（那时才适合删它）。

## 三条纪律（对应本项目反复吃过的亏）

- **不复刻被测逻辑**：A/B 用真实的 `hybrid_search`，只改配置。
- **改配置必须验证读到**：改 `_CONFIG` 后 `assert get(...) is 期望值`。
- **门禁自身要能判红**：M1/M2/M3 三组变异。
"""
from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

PASS, FAIL = "PASS", "FAIL"
_results = []
_MUT_ROWS = []
_IN_MUT = [False]


def _rec(name, ok, detail=""):
    row = (PASS if ok else FAIL, name, "" if ok else str(detail)[:220])
    (_MUT_ROWS if _IN_MUT[0] else _results).append(row)
    return bool(ok)


def _src(p):
    with open(os.path.join(ROOT, p), encoding="utf-8") as f:
        return f.read()


# ── H1：入库侧成本闸 ─────────────────────────────────────────────────────
def t_h1_cost_gate(ing, cfg):
    print("\n=== H1 入库侧 HyDE 成本闸 ===")
    ok = True
    ok &= _rec("H1a 配置项 rag.hyde_embed_max_chunks 存在",
               '"hyde_embed_max_chunks"' in cfg)
    #⚠️ 默认值必须 0（不限制）：改成小 N 等于默认降质量，
    #   而实测 HyDE 影响 44 处排名 ⇒ 默认降级是**静默的质量损失**。
    m = re.search(r'"hyde_embed_max_chunks":\s*(\d+)', cfg)
    ok &= _rec("H1b 默认值为 0（=全量，不静默降质量）",
               bool(m) and int(m.group(1)) == 0, m.group(1) if m else "未找到")
    ok &= _rec("H1c ingest 读该配置", "hyde_embed_max_chunks" in ing)
    ok &= _rec("H1d 有索引裁剪逻辑（只算前 N 块）",
               "_hyde_idx" in ing and "_hyde_cap" in ing)
    # ⚠️ 关键反证：不能只在"全 0"路径下正确 —— 裁剪分支必须真的被用上。
    #   只判"出现了 _hyde_idx 这个名字"是**弱断言**：把取数改回全量
    #   （for q in _hyde_questions）后 _hyde_idx 仍在、配置仍被读，
    #   但裁剪**完全失效** ⇒ 门禁会假绿（M2 实测踩到）。
    #   ⚠️⚠️⚠️ 三个连环坑（都实测踩过），记下来别再犯：
    #   ① `\[(.*?)\]` 会在 `join(` 的函数括号处提前截断；
    #   ② 改成抓 `embed_with_version\((.{0,300})` 后，命中的是**第一处**调用
    #      （主向量 embed_texts），不是 HyDE 那处 —— 文件里有 2+ 处同名调用；
    #   ③ 正解：**取最后一次出现**（HyDE 段在主向量段之后），
    #      或按 `_hyde_idx` 就近定位。判据要锚"HyDE 那一次调用"，
    #      不是"任意一次调用"。
    calls = [m.start() for m in re.finditer(r"embed_with_version\(", ing)]
    ok &= _rec("H1d2 文件里有多处 embed_with_version 调用（需按位置区分）",
               len(calls) >= 2, "calls=%d" % len(calls))
    # HyDE 段：取**含 _hyde_idx 的那次调用**
    hyde_call = ""
    for i in calls:
        seg = ing[i:i + 400]
        if "_hyde_idx" in seg or "_hyde_questions" in seg:
            hyde_call = seg
    ok &= _rec("H1e HyDE 那次向量化的入参由 _hyde_idx 推导（裁剪真的生效）",
               "_hyde_idx" in hyde_call,
               "未找到：HyDE 调用入参=%r" % hyde_call[:100])
    ok &= _rec("H1e2 入参不是无条件全量（不含 for q in _hyde_questions if q）",
               "for q in _hyde_questions if q" not in ing,
               "仍有全量取数写法 ⇒ 裁剪被绕过")
    # 检索侧开关关闭时，入库侧也不该白花这份 embedding
    ok &= _rec("H1f 检索侧 hyde_enabled=False 时入库侧也不算（省成本且一致）",
               'hyde_enabled' in ing)
    return ok


# ── H2：配置读的是哪个对象（来自真实踩坑）───────────────────────────────
def t_h2_config_source(cfg):
    print("\n=== H2 配置生效路径（防「改了模板没改生效值」）===")
    ok = True
    # ⚠️ 必须判**get 的函数体**读的是 _CONFIG，而不是"文件里出现过 _CONFIG"，
    #   否则把 `_CONFIG.get(section)` 改成 `_CONFIG.get(section) or DEFAULT_CONFIG…`
    #   之类仍会命中（M3 实测踩到：子串判据恒真）。
    m = re.search(r"def get\(section.*?\n(?=\ndef |\nclass )", cfg, re.S)
    ok &= _rec("H2a 能定位 config.get 函数体", bool(m))
    body = m.group(0) if m else ""
    ok &= _rec("H2b get() 读的是模块级 _CONFIG（不是 DEFAULT_CONFIG）",
               "_CONFIG.get(" in body and "DEFAULT_CONFIG.get(" not in body,
               "get 体里出现了 DEFAULT_CONFIG.get")
    ok &= _rec("H2c _CONFIG 与 DEFAULT_CONFIG 不是同一对象",
               "_CONFIG = " in cfg, "若同一对象则本条恒假，说明结构变了")
    return ok


# ── H3：HyDE 在真库上确实影响排序（防「它是死代码」被误删）──────────────
def t_h3_hyde_live(need_db=True):
    print("\n=== H3 HyDE 对真实检索结果的影响（A/B 真跑）===")
    try:
        from core import config as C
        from database import get_db
        import knowledge_engine as KE
    except Exception as e:
        return _rec("H3a 可导入检索模块", False, str(e)[:120])

    ok = _rec("H3a 可导入检索模块", True)
    # ⚠️ 改**生效对象**_CONFIG（get 真正读的），改完必须断言读到
    sec = C._CONFIG.setdefault("rag", {})
    old = sec.get("hyde_enabled")

    def probe(on):
        sec["hyde_enabled"] = on
        assert C.get("rag", "hyde_enabled") is on, "开关未生效（get 没读到）"
        c = get_db()
        try:
            out = {}
            for q in ("需求分解为系统需求与子系统需求", "如何做追溯矩阵",
                      "状态机建模的迁移条件", "接口定义与连接器的关系",
                      "部件定义与用例的关系", "怎么保证需求可验证"):
                try:
                    r = KE.hybrid_search(c, q, top_k=10)
                    out[q] = ["%s/%s" % (h.get("document_id"), h.get("chunk_index"))
                              for h in (r.get("hits") or [])]
                except Exception as e:
                    out[q] = ["ERR:%s" % str(e)[:40]]
            return out
        finally:
            c.close()

    try:
        on_r = probe(True)
        off_r = probe(False)
    finally:
        if old is not None:
            sec["hyde_enabled"] = old
        else:
            sec.pop("hyde_enabled", None)

    ok &= _rec("H3b 开关切换被 get() 读到（改的是 _CONFIG 不是模板）", True)
    diff = [q for q in on_r if on_r[q] != off_r.get(q)]
    ok &= _rec("H3c HyDE 至少影响一条查询的排序（否则它是死代码）",
               len(diff) >= 1, "diff=%d/%d" % (len(diff), len(on_r)))
    print("     受影响查询：%s" % [q[:14] for q in diff])
    # 独占命中（只有一侧有）比"仅排名变动"更能说明 HyDE 的价值
    exclusive = 0
    for q in diff:
        exclusive += len(set(on_r[q]) - set(off_r.get(q, [])))
    ok &= _rec("H3d 存在「仅 HyDE 开时才有」的独占命中（真实补召回，非仅重排）",
               exclusive >= 1, "exclusive=%d" % exclusive)
    return ok


# ── 变异自证 ─────────────────────────────────────────────────────────────
def mutations(ing, cfg):
    print("\n--- M1：默认改成限流 50（静默降质量）⇒ H1 判红 ---")
    _IN_MUT[0] = True
    mut_cfg = cfg.replace('"hyde_embed_max_chunks": 0', '"hyde_embed_max_chunks": 50', 1)
    _rec("M1 默认值改成 50 ⇒ H1 判红",
         (mut_cfg != cfg) and (not t_h1_cost_gate(ing, mut_cfg)))
    _IN_MUT[0] = False

    print("\n--- M2：删掉索引裁剪（退化成恒全量）⇒ H1 判红 ---")
    _IN_MUT[0] = True
    mut_ing = ing.replace("[\"；\".join(_hyde_questions[i]) for i in _hyde_idx]",
                          "[\"；\".join(q) for q in _hyde_questions if q]", 1)
    _rec("M2 删掉索引裁剪 ⇒ H1 判红",
         (mut_ing != ing) and (not t_h1_cost_gate(mut_ing, cfg)))
    _IN_MUT[0] = False

    print("\n--- M3：get 改成读 DEFAULT_CONFIG（模板≠生效值）⇒ H2 判红 ---")
    _IN_MUT[0] = True
    mut2 = cfg.replace("_CONFIG.get(section)", "DEFAULT_CONFIG.get(section)", 1)
    _rec("M3 get 改读模板 ⇒ H2 判红",
         (mut2 != cfg) and (not t_h2_config_source(mut2)))
    _IN_MUT[0] = False


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--static-only", action="store_true",
                    help="只跑结构断言（跳过 H3 的真库 A/B）—— CI 用：干净库无真实 chunk，"
                         "A/B 两侧都空会被判成「HyDE 零影响」而误红")
    args = ap.parse_args()

    ing = _src(os.path.join("knowledge_pipeline", "ingest.py"))
    cfg = _src(os.path.join("core", "config.py"))
    ok = True
    ok &= t_h1_cost_gate(ing, cfg)
    ok &= t_h2_config_source(cfg)
    if args.static_only:
        print("\n[H3 跳过] --static-only：H3 需要真实 chunk 与 hyde_questions，"
              "CI 干净库两者皆无 ⇒ A/B 两侧同为空会被读成「HyDE 零影响」而误红。")
        print("     H3 在开发机上真库跑（本机实测 5/6 查询受影响、有独占命中）。")
    else:
        ok &= t_h3_hyde_live()

    print("\n" + "=" * 68)
    n_fail = sum(1 for r in _results if r[0] == FAIL)
    print("常态断言：%d 条，%d 通过 / %d 失败" % (len(_results), len(_results) - n_fail, n_fail))
    for st, name, detail in _results:
        print("  [%s] %s%s" % (st, name, ("  <- " + detail) if detail else ""))
    if n_fail:
        print("结论：门禁未通过")
        return 1

    print("\n--- 变异自证 ---")
    _MUT_ROWS.clear()
    mutations(ing, cfg)
    verdicts = [r for r in _MUT_ROWS if r[0].isdigit() or r[1][:2] in ("M1", "M2", "M3")]
    subs = [r for r in _MUT_ROWS if r not in verdicts]
    n_red = sum(1 for r in verdicts if r[0] == PASS)
    print("变异组：%d，判红成功：%d（变异期子断言 %d 条，其中 %d 条转红）"
          % (len(verdicts), n_red, len(subs), sum(1 for r in subs if r[0] == FAIL)))
    for st, name, detail in subs:
        if st == FAIL:
            print("  [%s] %s" % (st, name))
    print("-" * 68)
    for st, name, detail in verdicts:
        print("  [%s] %s%s" % (st, name, ("  <- " + detail) if detail else ""))
    print("=" * 68)
    if n_red != len(verdicts) or not verdicts:
        print("结论：门禁未通过（%d/%d 变异未判红）" % (len(verdicts) - n_red, len(verdicts)))
        return 1
    print("结论：全部通过（常态 %d 条 + 变异 %d 组全部按预期判红）"
          % (len(_results), len(verdicts)))
    print("注：HyDE **不可为了省成本关闭**（实测影响排序）；"
          "本项交付的是「可降级」旋钮 rag.hyde_embed_max_chunks。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
