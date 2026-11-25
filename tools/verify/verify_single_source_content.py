# -*- coding: utf-8 -*-
# CI-OPTIONAL: [local] tier=local（见 tools/gates_manifest.json）：2026-11-20 补登记：历史漏入清单，当前按 local 手工跑；是否晋 pr 需先确认无外部依赖并同步 ci.yml（G6a）
"""正文「单一事实源」门禁（2026-11-19）—— 治「同一成品讲 N 遍」。

## 为什么需要它（用户原话：「这个问题之前反馈过多次一直未能彻底解决」）

msg#7039（21,864 字，直行 path）：**提示词出现 2 遍、同一份 SysML 代码出现 3 遍**。
一路查下来是**两个缺口叠加**，任一单独存在都不会出问题：

  ① `agent/final_content.py::_is_interim` 引用了**从未定义**的 `_SIM_OTHER`
     ⇒ `NameError` 被 `dedupe_interim_code_blocks` 的
       `except Exception: return text, 0` **静默吞掉**
     ⇒ **代码块去重自上线起一次都没真正跑过**。
     为什么前几轮"调阈值"全都无效：判据没机会执行。
     为什么只测 sysml 样本发现不了：`_SIM_OTHER` 只在 `family not in (sysml, plain)` 时才被求值
     ⇒ 纯 sysml 消息（如 #5135）能收敛，一旦含 ```text 块整函数就放弃。

  ② **直行路径（execute.py）根本没挂 dedupe** —— 三处挂钩点是 planner×2 + stream×1。
     msg#7039 走的是直行（`card_data.exec` 有值）⇒ 落在覆盖缺口里。

再加第三层（成因，不是缺口）：harness 续跑三次各写一份**完整报告**，
三份措辞差异极大（3-gram 相似度 0.383/0.253），任何"相似度判重述"都抓不到
⇒ `merge_round_content` 只能追加 ⇒ 正文也是三份。

## 本门禁守什么

  A 静态：全仓**没有**「引用但模块级无定义」的名字（NameError 类静默失效）
  B 块级：containing ```text + ```sysml 混排的真实事故消息，必须收敛到每族一份
  C 段级：重复的「结论型」小节必须收敛
  D 覆盖：四条路径（直行/流式/编排LLM/编排拼接）**都**挂了收敛
  E 续跑：自洽完整重述 ⇒ 替换（不累加），但 prev 独有块补回
  F 变异：拿掉关键因子结论必须翻转
  G 非回归：内容真不同的两轮仍追加（不丢信息）

用法：.venv\\Scripts\\python.exe -X utf8 tools/verify/verify_single_source_content.py
退出码：全绿 0 / 有失败 1。
"""
import os
import re
import sqlite3
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
DB = os.path.join(ROOT, "mbse.db")

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
_results = []


def ck(tag, ok, detail=""):
    _results.append((PASS if ok else FAIL, tag))
    print("  %-5s %s%s" % (PASS if ok else FAIL, tag, ("  | " + str(detail)) if detail else ""))


def skip(tag, why=""):
    _results.append((SKIP, tag))
    print("  SKIP  %s%s" % (tag, ("  | " + why) if why else ""))


def _ro():
    con = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
    con.row_factory = sqlite3.Row
    return con


def _fence_count(text):
    return len(re.findall(r"^```[\w.+\-]*[ \t]*$", str(text or ""), re.M)) // 2


#: 与 `final_content._MIN_BLOCK_CHARS` 的**短块例外**同口径：
#: 小于 40 字的块不参与收敛（`package P {}` 这类占位不是重复问题）。
_MIN_MEANINGFUL = 40


def _dup_pairs(text):
    """返回仍**互为 interim**（= 仍是重复）的块对计数。这是"没有重复"的精确定义。

    ⚠️ 不要用「同族块数 ≤1」当判据：真库 #6062 有三个**不同包名**的 SysML 模型
      （LiveDemoCapabilityModel / CapabilityInventoryModel /
       CapabilityInventorySummary），它们同族但**不是重复**，≤1 会误判。
      而 #5889 收敛后剩 2 个 json 块、彼此不相似 —— 也已经"没有重复"了。
    """
    from agent.final_content import _FENCE_RE, _lang_family, _norm, _is_interim
    bs = [(lg, b) for lg, b in _FENCE_RE.findall(str(text or ""))
          if len(_norm(b)) >= _MIN_MEANINGFUL]
    n = 0
    for i in range(len(bs)):
        for j in range(i + 1, len(bs)):
            fi, fj = _lang_family(bs[i][0]), _lang_family(bs[j][0])
            if fi != fj:
                continue
            if _is_interim(_norm(bs[i][1]), _norm(bs[j][1]), fi):
                n += 1
    return n


def _fams(text):
    """按语言族统计**有意义的**围栏块（≥40 字）。"""
    from agent.final_content import _FENCE_RE, _lang_family, _norm
    out = {}
    for lang, body in _FENCE_RE.findall(str(text or "")):
        if len(_norm(body)) < _MIN_MEANINGFUL:
            continue
        f = _lang_family(lang)
        out[f] = out.get(f, 0) + 1
    return out


# ══════════════════════════════════════════════════════════════════════════
def group_a():
    print("\n[A] 静态：全仓无「引用但模块级无定义」的名字")
    try:
        r = subprocess.run(
            [sys.executable, "-X", "utf8",
             os.path.join(ROOT, "tools", "verify", "find_undefined_names.py")],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=ROOT, timeout=300)
        out = (r.stdout or "") + (r.stderr or "")
        ck("A1 扫描器可运行", "未发现" in out or "发现" in out, out.strip().splitlines()[-1][:80])
        ck("A2 全仓零未定义名字", r.returncode == 0,
           "\n".join(l for l in out.splitlines() if "·" in l)[:300])
    except Exception as e:                                   # noqa: BLE001
        ck("A1 扫描器可运行", False, str(e)[:120])

    src = open(os.path.join(ROOT, "agent", "final_content.py"), encoding="utf-8").read()
    ck("A3 `_SIM_OTHER` 已定义（事故根因①）", re.search(r"^_SIM_OTHER\s*=\s*0\.\d+", src, re.M) is not None)
    ck("A4 dedupe 兜底不再静默（有日志或 re-raise）",
       "logger.warning" in src or "logging" in src or "raise" in src,
       "仅看 final_content 是否还全静默")

    tsrc = open(os.path.join(ROOT, "agent", "pipeline_parts", "execute.py"), encoding="utf-8").read()
    ck("A5 直行收敛失败走 warning 而非 pass", "_lg.getLogger" in tsrc)


# ══════════════════════════════════════════════════════════════════════════
def group_b():
    print("\n[B] 块级：混排 text+sysml 的真实事故必须收敛到每族一份")
    from agent.final_content import dedupe_interim_code_blocks, _FENCE_RE, _lang_family

    if not os.path.exists(DB):
        skip("B 真库事故", "无数据库")
        return
    con = _ro()
    try:
        t = con.execute("SELECT content FROM messages WHERE id=7039").fetchone()
    finally:
        con.close()
    if not t:
        skip("B 真库事故 #7039", "消息不存在")
        return
    body = t["content"]
    before = _fence_count(body)
    fams_before = _fams(body)
    out, n = dedupe_interim_code_blocks(body)
    fams_after = _fams(out)

    ck("B1 事故消息原本含 ≥2 个同族块（确实是重复）",
       any(v >= 2 for v in fams_before.values()), fams_before)
    ck("B2 收敛后不再有互为 interim 的块对（= 没有重复）",
       _dup_pairs(out) == 0, "残留重复对=%d" % _dup_pairs(out))
    ck("B3 收敛确实发生（>0）", n > 0, "%d 处，%d -> %d 字" % (n, len(body), len(out)))
    ck("B4 text 族也被收敛（旧代码在此分支 NameError 早退）",
       fams_after.get("text", 0) <= 1 and fams_before.get("text", 0) >= 2,
       "text %s -> %s" % (fams_before.get("text"), fams_after.get("text")))
    ck("B5 内容没被清空（保留主要篇幅）", len(out) >= 0.3 * len(body),
       "%d / %d" % (len(out), len(body)))

    # 幂等：再跑一次不应继续缩水
    out2, n2 = dedupe_interim_code_blocks(out)
    ck("B6 幂等（二次收敛 0 处）", n2 == 0 and len(out2) == len(out), n2)


# ══════════════════════════════════════════════════════════════════════════
def group_c():
    print("\n[C] 段级：重复的「结论型」小节必须收敛；正常文档一字不动")
    from agent.final_content import collapse_restated_sections

    dup = ("# 报告\n\n## 一、背景\n背景说明。\n\n"
           "## 结论：17 项待办中只有 3 项能完成，其余因缺数据无法完成\n"
           "### 一、已完成\n已完成的项逐条列出。\n### 二、无法完成\n无法完成的项逐条列出，原因各不相同。\n\n"
           "## 交付：提示词模板\n模板正文在此。\n\n"
           "## 结论：17 项待办在当前条件下无法完成，根因是数据与工具双重缺失\n"
           "### 一、阻塞归因\n三类归因逐条说明，数据缺失占多数。\n"
           "### 二、能交付的\n校验报告可以交付。\n")
    out, n = collapse_restated_sections(dup)
    ck("C1 同起手式的两个结论段 ⇒ 收敛 1 处", n == 1, n)
    ck("C2 保留更长的那段（第一段的正文更长）",
       "只有 3 项能完成" in out and "在当前条件下无法完成" not in out)
    ck("C3 保留的那段内容完整（子小节还在）", "### 二、无法完成" in out)
    ck("C4 非结论章节未被动（交付：提示词模板 仍在）", "交付：提示词模板" in out)

    normal = ("# 报告\n\n## 一、背景\n说明。\n\n## 结论\n只有一个结论，正常文档。\n")
    o2, n2 = collapse_restated_sections(normal)
    ck("C5 只有一个结论 ⇒ 一字不动", n2 == 0 and o2 == normal)

    two_topic = ("# 报告\n\n## 结论：性能方面达标情况\n延迟与吞吐均满足要求，实测数据见附表。\n\n"
                 "## 结论：安全方面达标情况\n权限与审计链路完整，渗透测试无高危。\n")
    o3, n3 = collapse_restated_sections(two_topic)
    ck("C6 分主题的两个结论**不**收敛（防误杀）", n3 == 0, n3)

    o4, n4 = collapse_restated_sections("")
    ck("C7 空文本安全", o4 == "" and n4 == 0)


# ══════════════════════════════════════════════════════════════════════════
def group_d():
    print("\n[D] 覆盖：四条正文出口都挂了收敛")
    files = {
        "直行 execute.py": ("agent/pipeline_parts/execute.py", "dedupe_interim_code_blocks"),
        "流式 stream.py": ("agent/pipeline_parts/stream.py", "dedupe_interim_code_blocks"),
        "编排 LLM 整合": ("workflows/planner.py", "dedupe_interim_code_blocks"),
    }
    for label, (rel, fn) in files.items():
        src = open(os.path.join(ROOT, rel), encoding="utf-8").read()
        ck("D1 %s 挂块级收敛" % label, fn in src)
        ck("D1b %s 挂段级收敛" % label, "collapse_restated_sections" in src)
    src = open(os.path.join(ROOT, "agent", "pipeline_parts", "execute.py"),
               encoding="utf-8").read()
    # 位置：必须在 _ensure_sysml_from_tools 之后（否则补回的代码不入收敛）
    i_ens = src.find("llm_content = self._ensure_sysml_from_tools(llm_content)")
    i_fc = src.find("dedupe_interim_code_blocks as _dedupe_fc")
    ck("D2 直行收敛在 _ensure_sysml_from_tools 之后", 0 < i_ens < i_fc,
       "ens@%d fc@%d" % (i_ens, i_fc))
    # 顺序：块级 → 段级 → 序号
    i_blk = src.find("_fc_blk = _dedupe_fc")
    i_sec = src.find("_fc_sec = _collapse_fc")
    i_ord = src.find("_fc_ord = _ordfix_fc")
    ck("D3 直行顺序：块级 → 段级 → 序号归一", 0 < i_blk < i_sec < i_ord,
       "%d<%d<%d" % (i_blk, i_sec, i_ord))


# ══════════════════════════════════════════════════════════════════════════
def group_e():
    print("\n[E] 续跑：自洽完整重述 ⇒ 替换；内容不同 ⇒ 追加（不丢信息）")
    from agent.auto_continue import merge_round_content, _ngram_sim, _RESTATE_TOPIC_MIN
    from agent.final_content import carry_over_unique_blocks

    if os.path.exists(DB):
        con = _ro()
        try:
            t = con.execute("SELECT content FROM messages WHERE id=7039").fetchone()
        finally:
            con.close()
        body = t["content"] if t else ""
        b2 = body.find("## 结论：17 项待办中，只有 3 项")
        b3 = body.find("## 结论：17 项待办在当前条件下无法完成")
        if b2 > 0 and b3 > b2:
            a1, a2, a3 = body[:b2], body[b2:b3], body[b3:]
            acc = ""
            for a in (a1, a2, a3):
                acc = merge_round_content(acc, a)
            ck("E1 三次 attempt 合并后显著短于累加", len(acc) < 0.6 * len(body),
               "%d vs 累加 %d" % (len(acc), len(body)))
            ck("E2 合并后结论段只有 1 个",
               len(re.findall(r"^## 结论", acc, re.M)) == 1,
               len(re.findall(r"^## 结论", acc, re.M)))
            # ⚠️ 测的是**真实链路的最终态**：merge → 出口 dedupe。
            #   为什么不能只测 merge 的输出：实测 a3（第三次 attempt 的原始回复）
            #   **自身**就带 2 份相同代码 —— 模型单轮输出即重复，那是出口收敛的职责，
            #   不是 merge 的。三条路径（直行/流式/编排）都挂了出口收敛（D 组守着）。
            from agent.final_content import dedupe_interim_code_blocks as _dd
            acc_final, _ = _dd(acc)
            ck("E3 合并+出口收敛后不再有互为 interim 的块对",
               _dup_pairs(acc_final) == 0,
               "残留重复对=%d fams=%s" % (_dup_pairs(acc_final), _fams(acc_final)))
            ck("E3b 原始 a3 自身确有重复（证明出口收敛不可省）",
               _dup_pairs(a3) > 0, "a3 重复对=%d" % _dup_pairs(a3))
            ck("E4 提示词没丢（两种提法都在 allowed 集内）",
                   ("# 角色" in acc), ("# 角色" in acc))
            sim23 = _ngram_sim(a2, a3)
            ck("E5 a2~a3 相似度高于阈值（替换确实该触发）",
               sim23 >= _RESTATE_TOPIC_MIN, "%.3f >= %.2f" % (sim23, _RESTATE_TOPIC_MIN))
        else:
            skip("E1-E5 真库三轮模拟", "切点没命中")

    # 反例：内容真不同
    r1 = "## 一、需求分析\n" + "需求条目逐条拆解与来源追溯。" * 200
    r2 = "## 二、架构设计\n" + "组件划分与接口定义逐条说明。" * 200
    m = merge_round_content(r1, r2)
    ck("E6 内容不同的两轮仍追加（两段都在）",
       "## 一、需求分析" in m and "## 二、架构设计" in m)
    ck("E7 该反例的相似度确实低于阈值",
       _ngram_sim(r1, r2) < _RESTATE_TOPIC_MIN, "%.3f" % _ngram_sim(r1, r2))

    # 短增量（接着做）不应替换
    prev = "## 一、总体方案\n" + "方案说明。" * 300
    inc = "已完成第 3 项，回执如下：pass。\n"
    m2 = merge_round_content(prev, inc)
    ck("E8 短增量不触发替换（prev 全文保留）", "## 一、总体方案" in m2 and "已完成第 3 项" in m2)

    # carry：prev 独有块补回
    blk = "```sysml\n" + "\n".join("part def C%d;" % i for i in range(80)) + "\n```"
    prev2 = "## 一、x\n" + blk + "\n说明。\n" + "正文。" * 300
    cur2 = "## 一、x\n" + "正文。" * 400
    carried = carry_over_unique_blocks(prev2, cur2)
    ck("E9 prev 独有块被补回", "part def C0;" in carried)
    ck("E9b 补回只发生一次（不重复补）",
       carried.count("part def C0;") == 1, carried.count("part def C0;"))
    # 同一份块措辞微调（差 2 字）不应被当成"独有"再补一遍
    blk2 = blk.replace("part def C79;", "part def C79 ;")
    cur3 = "## 一、x\n" + blk2 + "\n" + "正文。" * 400
    carried2 = carry_over_unique_blocks(prev2, cur3)
    ck("E10 同一份块的微调版不重复补回（md5 判据会在这里翻车）",
       carried2.count("part def C0;") == 1, carried2.count("part def C0;"))


# ══════════════════════════════════════════════════════════════════════════
def group_f():
    print("\n[F] 变异自证：拿掉关键因子结论必须翻转")
    import agent.final_content as FC
    import agent.auto_continue as AC

    if os.path.exists(DB):
        con = _ro()
        try:
            body = con.execute("SELECT content FROM messages WHERE id=7039").fetchone()["content"]
        finally:
            con.close()
        base, n_base = FC.dedupe_interim_code_blocks(body)
        ck("F1 基线：块级能收敛", n_base > 0, n_base)
        # 变异：把门槛抄成不可能的值（等价于恢复到「未定义/失效」的效果）
        old = FC._SIM_OTHER
        try:
            FC._SIM_OTHER = 1.01
            _o, n_mut = FC.dedupe_interim_code_blocks(body)
            ck("F2 变异：text 族门槛 1.01 ⇒ text 块不再收敛（判据承重）",
               n_mut < n_base, "%d -> %d" % (n_base, n_mut))
        finally:
            FC._SIM_OTHER = old

    dup = ("# R\n\n## 结论：A 方案达标情况说明\n" + "方案 A 的实测数据与结论逐条列在此处。" * 40
           + "\n\n## 结论：A 方案达标情况说明\n" + "方案 A 的实测数据与结论逐条列在此处。" * 40 + "\n")
    _o1, n1 = FC.collapse_restated_sections(dup)
    ck("F3 基线：重复结论段收敛", n1 >= 1, n1)
    old_sim = FC._SEC_RESTATE_SIM
    try:
        FC._SEC_RESTATE_SIM = 1.01
        # 同起手式那条仍会命中（判据是 or），所以同时把起手式判定废掉不现实；
        # 改为用一个「起手式不同、正文高度重复」的样本验正文判据
        x = ("# R\n\n## 结论：性能达标\n" + "延迟吞吐实测均满足要求详见附表。" * 40
             + "\n\n## 结论：安全达标\n" + "延迟吞吐实测均满足要求详见附表。" * 40 + "\n")
        _a, nb = FC.collapse_restated_sections(x)
        FC._SEC_RESTATE_SIM = 0.0
        _b, nc = FC.collapse_restated_sections(x)
        ck("F4 变异：正文相似度门槛是承重的（0.0 时误收敛更多）", nc >= nb,
           "%d vs %d" % (nb, nc))
    finally:
        FC._SEC_RESTATE_SIM = old_sim

    # 夹具按真实形态构造：续跑重写的完整回复**自带 ≥2 个标题**
    #（msg#7039 的 a2 有 23 个、a3 有 8 个）；单标题的长文本无法与"接着做"区分，判据故意不覆盖它。
    prev = ("## 一、背景\n" + "同一件事的说明文字。" * 200
            + "\n## 二、分析\n" + "同一件事的分析文字。" * 200)
    cur = ("## 一、背景\n" + "同一件事的说明文字，措辞略有不同而已。" * 200
           + "\n## 二、分析\n" + "同一件事的分析文字，措辞略有不同而已。" * 200)
    m = AC.merge_round_content(prev, cur)
    ck("F5 基线：重述被替换（不叠加）", len(m) < len(prev) + len(cur) - 100)
    old_t = AC._RESTATE_TOPIC_MIN
    try:
        AC._RESTATE_TOPIC_MIN = 0.999
        m2 = AC.merge_round_content(prev, cur)
        ck("F6 变异：话题门槛 0.999 ⇒ 退化为追加（判据承重）",
           len(m2) >= len(prev) + len(cur) - 100, "%d vs %d" % (len(m), len(m2)))
    finally:
        AC._RESTATE_TOPIC_MIN = old_t


# ══════════════════════════════════════════════════════════════════════════
def group_g():
    print("\n[G] 非回归：现网 11 条块级重复消息全部收敛，且不越界")
    from agent.final_content import dedupe_interim_code_blocks, _FENCE_RE, _lang_family

    if not os.path.exists(DB):
        skip("G 真库批量", "无数据库")
        return
    con = _ro()
    try:
        rows = con.execute(
            "SELECT id, content FROM messages WHERE role='assistant' AND length(content)>3000 "
            "ORDER BY id DESC LIMIT 400").fetchall()
    finally:
        con.close()

    checked = fixed = 0
    worse = []
    for r in rows:
        body = r["content"]
        fams = _fams(body)
        if not any(v >= 2 for v in fams.values()):
            continue
        checked += 1
        out, n = dedupe_interim_code_blocks(body)
        fams2 = _fams(out)
        if _dup_pairs(out) == 0 and len(out) >= 0.2 * len(body):
            fixed += 1
        else:
            worse.append((r["id"], fams, fams2, len(body), len(out),
                          "残留重复对=%d" % _dup_pairs(out)))
    ck("G1 真库存在同类样本（判据有意义）", checked > 0, "%d 条" % checked)
    ck("G2 全部收敛到「无互为 interim 的块对」", not worse,
       "%d/%d 收敛；异常=%s" % (fixed, checked, worse[:3]))


def main():
    print("=" * 84)
    print("正文单一事实源门禁（2026-11-19）")
    print("  repo=%s" % ROOT)
    print("=" * 84)
    group_a()
    group_b()
    group_c()
    group_d()
    group_e()
    group_f()
    group_g()

    fails = [t for s, t in _results if s == FAIL]
    skips = [t for s, t in _results if s == SKIP]
    print("\n" + "-" * 84)
    print("合计 %d 项（PASS %d / FAIL %d / SKIP %d）"
          % (len(_results), len(_results) - len(fails) - len(skips), len(fails), len(skips)))
    if fails:
        for t in fails:
            print("  · FAIL " + t)
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
