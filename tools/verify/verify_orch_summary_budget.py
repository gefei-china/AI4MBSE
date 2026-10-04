# -*- coding: utf-8 -*-
"""编排「汇总输入预算」不变式验证（零 LLM、零库写入，可反复跑）。

**为什么需要它**（2026-09-20，会话 368 实测）：
    汇总环节把每个子任务交付物硬编码截到 **500 字符**（`workflows/planner.py::_summarize_plan`），
    而实际交付物 1,260 / 4,000 / 4,000 字符 → 丢弃率 **60.3% / 87.5% / 87.5%**；
    质量评审据此**如实**报「t2 架构方案权衡在『给出三个方案』处中断，三个方案具体内容
    及方案选择结论均缺失」——评审没错，是管线在丢内容。叠加 `task_queue.py` 的 `result[:4000]`
    落库截断，同一个交付物在链路上被切两次。

本脚本锁定的三条不变式：
    I1 汇总单项预算**不再**是 500（且总预算按子任务数均分、有保底、有上限）
    I2 超预算裁剪必须**头尾同留**（只留头会把「结论/方案对比/风险」整段丢掉 —— 正是本次缺口形态）
    I3 落库保留上限已配置化且默认抬高（不再是 4000）

**变异自证（强制）**：`check_*` 函数同时对「旧写法」跑一遍，**必须失败**。
    按工程纪律（skill §6.2）：断言"跑通了"证明不了它有效，能抓住旧写法才算数。
    本脚本内置 4 组变异，任一变异未被抓住 → 该组断言判为 VACUOUS（空转）并 rc=1。

用法：
    <repo>\\.venv\\Scripts\\python.exe -X utf8 tools/verify/verify_orch_summary_budget.py
退出码：全绿 0 / 有失败 1。
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
_results = []


def _rec(status, tag, detail=""):
    _results.append((status, tag, detail))
    print("  %-4s  %s%s" % (status, tag, ("  | " + detail) if detail else ""))


# ── I1：汇总单项预算 ────────────────────────────────────────────────────────
def check_budget(fn):
    """对给定「预算函数」跑不变式，返回失败清单（空 = 通过）。**不打印、不记录**。"""
    fails = []
    old_hard = 500                      # 改动前的硬编码值
    # (a) 3 个子任务时，单项预算必须**显著大于**旧硬编码 500（否则等于没修）
    v3 = fn(3)
    if not isinstance(v3, int) or v3 <= old_hard:
        fails.append("n=3 预算=%r 未超过旧硬编码 %d" % (v3, old_hard))
    # (b) 子任务继续变多时**不得跌破保底**（防"每个都看不清"）。
    #     ⚠️ 断言点必须选在「总预算均分后确实低于保底」的 n 上，否则这条不变式根本没被触发 ——
    #     实测踩过：最初用 n=12（12000//12=1000 ≥ 600），"去掉保底"的变异**抓不到** = 空转。
    for n_probe in (21, 30, 40):
        v = fn(n_probe)
        if v < 600:
            fails.append("n=%d 预算=%d 低于保底 600" % (n_probe, v))
            break
    # (c) 子任务少时不得突破单项上限（防单条撑爆提示词）
    v2 = fn(2)
    if v2 > 1600:
        fails.append("n=2 预算=%d 突破单项上限 1600" % v2)
    # (d) 单调不增：子任务越多，单项预算不得反而更大（分配语义）
    seq = [fn(n) for n in (2, 3, 5, 8, 12)]
    if any(seq[i] < seq[i + 1] for i in range(len(seq) - 1)):
        fails.append("预算随 n 非单调不增：%r" % (seq,))
    # (e) 空/异常输入不得抛异常
    try:
        fn(0)
        fn(None)
    except Exception as e:
        fails.append("n=0/None 抛异常：%s" % e)
    return fails


# ── I2：头尾采样 ────────────────────────────────────────────────────────────
def check_clip(fn, cap=200):
    """对给定「裁剪函数」跑不变式，返回失败清单。**不打印、不记录**。"""
    fails = []
    head_unit, mid_unit, tail_unit = "前", "中", "后"
    text = head_unit * 400 + mid_unit * 2000 + tail_unit * 400
    out = fn(text, cap)
    if not isinstance(out, str):
        return ["返回非字符串：%r" % type(out)]
    # (a) 头部保留（清单/前言在此）
    if not out.lstrip().startswith(head_unit):
        fails.append("头部未保留（应以 %r 开头）" % head_unit)
    # (b) **尾部保留**（结论/方案对比/风险在此）—— 这是本次缺口的直接判据
    if not out.rstrip().endswith(tail_unit):
        fails.append("**尾部未保留**（应以 %r 结尾）—— 结论会被整段丢弃" % tail_unit)
    # (c) 必须显著短于原文（否则等于没裁，会撑爆提示词）
    if len(out) >= len(text) * 0.5:
        fails.append("裁剪无效：%d → %d" % (len(text), len(out)))
    # (d) 裁剪后不得超过 cap 太多（允许标记本身的开销）
    if len(out) > cap * 1.5:
        fails.append("超预算：cap=%d 实际=%d" % (cap, len(out)))
    # (e) 未超限时**原样返回**（不得无谓改动）
    short = "简短交付物"
    if fn(short, cap) != short:
        fails.append("未超限文本被改动")
    # (f) cap<=0 = 不限长，原样返回
    if fn(text, 0) != text:
        fails.append("cap<=0 未按「不限长」原样返回")
    return fails


# ── I3：落库保留上限 ────────────────────────────────────────────────────────
def check_keep(fn):
    fails = []
    v = fn()
    if not isinstance(v, int):
        return ["返回非整数：%r" % v]
    if v == 4000:
        fails.append("仍为旧硬编码 4000（未配置化）")
    if v < 12000:
        fails.append("保留上限 %d 过低（交付物仍会被静默截尾）" % v)
    return fails


def _has_code_literal(src: str, pattern: str) -> bool:
    """`pattern` 是否出现在**真实代码**里（注释 / 字符串字面量 / docstring 内的不算）。

    为什么需要这一层：本轮改动**故意在 docstring 里写明旧写法**以记录坑
    （如「原实现是 `str(content)[:6000]`，只留头」），纯文本 grep 会把这段记录当成
    「坑还在」→ 实测误报 1 条。

    ⚠️ **不能**简单粗暴地"删掉所有字符串再匹配" —— 本轮要查的 pattern 本身含字符串
    字面量（如 `it.get('result') or '')[:400]`），删了就再也匹配不上（从"误报"变成"漏报"，
    更危险）。故改为**看命中起点落在哪**：起点在 STRING/COMMENT token 区间内 → 视为
    "写在文字里"、跳过；起点在代码里 → 才算命中。用 tokenize 拿真实位置，不靠猜。
    """
    import io as _io
    import re as _re
    import tokenize as _tk
    try:
        toks = list(_tk.generate_tokens(_io.StringIO(src).readline))
    except Exception:                                   # noqa: BLE001
        return bool(_re.search(pattern, src))
    line_off = [0]
    for ln in src.splitlines(keepends=True):
        line_off.append(line_off[-1] + len(ln))

    def _abs(row, col):
        return line_off[row - 1] + col

    blocked = []
    for t in toks:
        if t.type in (_tk.COMMENT, _tk.STRING):
            blocked.append((_abs(*t.start), _abs(*t.end)))
    for m in _re.finditer(pattern, src):
        s = m.start()
        if any(bs <= s < be for bs, be in blocked):
            continue
        return True
    return False


# ── I5：汇总输入必须含「完整交付物」（2026-09-19 conv 370 实测）────────────────
_SUM_ASSIGN = re.compile(r'_mit\["result"\]\s*=\s*(.+?)(?=\n\s*_sum_items\.append\(_mit\))', re.S)


def check_summary_input(src: str) -> list:
    """在 stream.py 源码上判断：喂给汇总的 `result` 是「摘要 + 完整交付物」还是「纯摘要替换」。

    为什么是源码级：这段逻辑在 `_generate_orchestration` 生成器内部，脱离 SSE 上下文
    无法单测；而它恰恰是 conv 370「报告只能写『交付物在约束条件处截断』」的根因 ——
    Task 10 用协议摘要（~250 字符）**替换**了交付物正文（实测 12,998 字符）。
    """
    fails = []
    m = _SUM_ASSIGN.search(src)
    if not m:
        return ['找不到 `_mit["result"] = ...` 赋值（结构变了？）']
    expr = m.group(1).strip()
    # (a) 必须真的从 `_it` 取交付物原文
    if not _has_code_literal(src, r'_it\.get\("result"\)'):
        fails.append("未从 `_it` 取交付物原文（get(\"result\")）")
    # (b) 赋值不得是裸摘要变量（= 纯替换，交付物正文被丢弃）
    if re.fullmatch(r"_sec", expr):
        fails.append('`_mit["result"] = _sec` → 纯摘要替换，交付物正文被丢弃')
    # (c) 赋值表达式必须引用承载正文的变量
    if not re.search(r"\b_body\b|\b_it\b", expr):
        fails.append("赋值未引用交付物变量：expr=%r" % expr[:70])
    return fails


def _mut_summary_back(src: str) -> str:
    """变异 M9：把「摘要 + 完整交付物」退回「纯摘要替换」（conv 370 的旧写法）。"""
    return _SUM_ASSIGN.sub('_mit["result"] = _sec', src, count=1)


def main():
    repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    print("=" * 90)
    print("编排「汇总输入预算」不变式验证")
    print("  repo=%s" % repo)
    print("=" * 90)

    # ① 导入被测实现
    try:
        from workflows.planner import FlowPlannerMixin
        from task_queue import TaskQueue
    except Exception as e:
        _rec(FAIL, "导入被测模块", "%s: %s" % (type(e).__name__, e))
        _summary()
        return 1
    _rec(PASS, "导入 workflows.planner.FlowPlannerMixin / task_queue.TaskQueue")

    _budget = FlowPlannerMixin._summarize_item_budget
    _clip = FlowPlannerMixin._head_tail_clip
    _keep = TaskQueue._result_keep_chars

    # ② I1 —— 真实实现 + 变异自证
    f_real = check_budget(_budget)
    if f_real:
        _rec(FAIL, "I1 汇总单项预算（真实实现）", "; ".join(f_real))
    else:
        _rec(PASS, "I1 汇总单项预算（真实实现）",
             "n=2/3/5/8/12 → %s" % [ _budget(n) for n in (2, 3, 5, 8, 12)])
    # 变异 M1：还原旧硬编码 500
    f_m1 = check_budget(lambda n: 500)
    if f_m1:
        _rec(PASS, "I1 变异自证 M1（还原 `[:500]`）", "已被抓住：%s" % f_m1[0])
    else:
        _rec(FAIL, "I1 变异自证 M1（还原 `[:500]`）", "**未被抓住 → 断言空转（VACUOUS）**")
    # 变异 M2：分配时不设保底（子任务一多就 <600）
    f_m2 = check_budget(lambda n: min(1600, 12000 // max(int(n or 1), 1)))
    if f_m2:
        _rec(PASS, "I1 变异自证 M2（去掉保底）", "已被抓住：%s" % f_m2[0])
    else:
        _rec(FAIL, "I1 变异自证 M2（去掉保底）", "**未被抓住 → 断言空转（VACUOUS）**")

    # ③ I2 —— 真实实现 + 变异自证
    f_real2 = check_clip(_clip)
    if f_real2:
        _rec(FAIL, "I2 头尾采样（真实实现）", "; ".join(f_real2))
    else:
        _rec(PASS, "I2 头尾采样（真实实现）", "头/尾均保留、裁剪有效、未超限原样返回")
    # 变异 M3：只留头（改动前的 `_truncate_budget(keep_head=True)` 语义）
    f_m3 = check_clip(lambda t, c: (str(t or "")[:c] if c > 0 else str(t or "")))
    if f_m3:
        _rec(PASS, "I2 变异自证 M3（只留头）", "已被抓住：%s" % f_m3[0])
    else:
        _rec(FAIL, "I2 变异自证 M3（只留头）", "**未被抓住 → 断言空转（VACUOUS）**")
    # 变异 M4：只留尾
    f_m4 = check_clip(lambda t, c: (str(t or "")[-c:] if c > 0 else str(t or "")))
    if f_m4:
        _rec(PASS, "I2 变异自证 M4（只留尾）", "已被抓住：%s" % f_m4[0])
    else:
        _rec(FAIL, "I2 变异自证 M4（只留尾）", "**未被抓住 → 断言空转（VACUOUS）**")

    # ④ I3 —— 真实实现 + 变异自证
    f_real3 = check_keep(_keep)
    if f_real3:
        _rec(FAIL, "I3 落库保留上限（真实实现）", "; ".join(f_real3))
    else:
        _rec(PASS, "I3 落库保留上限（真实实现）", "keep=%d" % _keep())
    f_m5 = check_keep(lambda: 4000)
    if f_m5:
        _rec(PASS, "I3 变异自证 M5（还原 `[:4000]`）", "已被抓住：%s" % f_m5[0])
    else:
        _rec(FAIL, "I3 变异自证 M5（还原 `[:4000]`）", "**未被抓住 → 断言空转（VACUOUS）**")

    # ④b I4：反思修订环节 RefineGate 的四个上限（2026-09-20 新增）
    #     为什么单独一段：`orch_content = _ref.get("content") or orch_content` ——
    #     **用户最终看到的报告正文就是修订输出**，所以这四个上限才是"报告有多长"的真正天花板。
    _REFINE_WANT = {"item_chars": 400, "items_total_chars": 2000,
                    "report_in_chars": 6000, "max_tokens": 3000}   # 键 → **旧硬编码值**
    _REFINE_EXP = {"item_chars": 1200, "items_total_chars": 6000,
                   "report_in_chars": 24000, "max_tokens": 8000}   # 键 → 期望默认值

    def check_refine_limits(getter):
        """`getter(key) -> 实际生效值`；返回失败清单。"""
        fails = []
        for k, old in _REFINE_WANT.items():
            try:
                v = getter(k)
            except Exception as e:                              # noqa: BLE001
                fails.append("%s 取值抛异常：%s" % (k, e))
                continue
            if v is None:
                fails.append("%s 读不到（配置组缺失 → 只是调用点默认值，不是真可配）" % k)
            elif int(v) == old:
                fails.append("%s 仍为旧硬编码 %d" % (k, old))
            elif int(v) < 1:
                fails.append("%s 非正数 %r" % (k, v))
        return fails

    def check_refine_window(get_report_in, get_max_tokens):
        """I4c：待修订报告窗口必须 >= 本环节**自身产出**的能力。

        为什么单列：`report_in_chars` < 报告实际长度时，修订/评审拿到的是**头尾采样后**的报告
        —— 报告里引用「见第五章」而第五章正好落在省略区 → 评审报「被引用但未在可见内容中」。
        conv 371 实测：19011 字符报告被 12000 裁到 63%，第五/六/七章整段丢失。
        换算口径：1 output token ≈ 2.9 字符（实测 comp 6518 tokens → 19011 字符），断言取 ×2.5 留余量。
        """
        try:
            rin, mt = int(get_report_in()), int(get_max_tokens())
        except Exception as e:                                  # noqa: BLE001
            return ["取值异常：%s" % e]
        need = int(mt * 2.5)
        if rin < need:
            return ["report_in_chars=%d < max_tokens×2.5=%d（报告尾部会被裁掉）" % (rin, need)]
        return []

    # 换算系数：1 output token ≈ 2.9 字符（实测 comp 6518 tokens → 19011 字符 ≈ 2.92）
    EVAL_CHARS_PER_TOKEN = 2.9

    def _eval_probe_len() -> int:
        """I4d 探针长度：由 `refine.max_tokens × 2.9` 推导（**不得写死**，理由见下）。"""
        try:
            from core import config as _cfg
            return int(int(_cfg.get("refine", "max_tokens", 8000)) * EVAL_CHARS_PER_TOKEN)
        except Exception:
            return 22409

    def check_eval_window(clip_fn, probe_len=None):
        """I4d：**评审输入窗口**必须覆盖报告实际长度。

        为什么单列：这一层在 `workflows/nodes.py::_evaluate_content`（`[:2000]`），
        **不在 refine.py 里** —— 前四条不变式全查不到它。
        它是「报告越修越长、评审看到的比例越小」的元凶：conv 372 报告 22,409 字符
        被截到 8.9% → 评审判「t2/t3 无实质内容」，而那两节**确实存在**。

        probe_len（2026-09-20 修正，**原为写死的 22409**）：
          None → 由 `refine.max_tokens × 2.9` 推导。
          为什么必须改成推导：写死 22409 时，把 `refine.max_tokens` 抬到 12000
          （报告理论最长 ≈ 34,800 字符）却**漏抬 `eval_in_chars`** 的情形下 I4d **仍 PASS**
          （因为它只测 22,409）→ **门禁盲区：红不了，但评审会再次看不到报告尾部**，
          等于重演 conv 372。实测复现见 `tmp/llm_ctx/probe_raise.py`（场景 A）。
          ⚠️ 与 I4c 的 ×2.5 不同是有意的：I4c 判「窗口 ≥ 产出能力」取保守下界，
          本判据要覆盖**报告实际长度**，故取实测系数 2.9
          （I4c 的 2.5 比实测低约 17%，属偏松，已记为待办、本次不擅改）。
        """
        if probe_len is None:
            probe_len = _eval_probe_len()
        text = "甲" * probe_len
        try:
            out = clip_fn(text)
        except Exception as e:                                     # noqa: BLE001
            return ["调用异常：%s" % e]
        if not isinstance(out, str):
            return ["返回非字符串：%r" % type(out)]
        if len(out) < probe_len * 0.9:
            return ["评审窗口只保留 %d/%d 字符（评审看不到后半段 → 会误判『交付物缺失』）"
                    % (len(out), probe_len)]
        return []

    _rg = None
    try:
        from workflows.refine import RefineGate
        _rg = RefineGate
    except Exception as e:                                       # noqa: BLE001
        _rec(FAIL, "导入 workflows.refine.RefineGate", "%s: %s" % (type(e).__name__, e))
    if _rg is not None:
        _getters = {"item_chars": _rg._item_chars, "items_total_chars": _rg._items_total_chars,
                    "report_in_chars": _rg._report_in_chars, "max_tokens": _rg._max_tokens}
        f_r = check_refine_limits(lambda k: _getters[k]())
        if f_r:
            _rec(FAIL, "I4 修订环节四上限（真实实现）", "; ".join(f_r))
        else:
            _rec(PASS, "I4 修订环节四上限（真实实现）",
                 " / ".join("%s=%d" % (k, _getters[k]()) for k in ("item_chars", "items_total_chars",
                                                                   "report_in_chars", "max_tokens")))
        # 变异 M6：还原旧的四项硬编码
        f_m6 = check_refine_limits(lambda k: _REFINE_WANT[k])
        if f_m6:
            _rec(PASS, "I4 变异自证 M6（还原四项旧硬编码）", "已被抓住：%s" % f_m6[0])
        else:
            _rec(FAIL, "I4 变异自证 M6（还原四项旧硬编码）", "**未被抓住 → 断言空转（VACUOUS）**")
        # 变异 M7：配置读取返回 None（= 模拟「config 里根本没有 refine 组」的**不可读**态）
        # ⚠️ 上一版这里读的是**真实** config 的 refine 段 —— 该组现已存在，于是变异拿到的
        #    正是真实值、自然不失败 = **空转**（实测被自己的变异自证抓出来）。
        #    变异必须直接模拟**缺失**，不能回读真实配置。
        f_m7 = check_refine_limits(lambda k: None)
        if f_m7:
            _rec(PASS, "I4 变异自证 M7（配置不可读 → None）", "已被抓住：%s" % f_m7[0])
        else:
            _rec(FAIL, "I4 变异自证 M7（配置不可读 → None）",
                 "**未被抓住 → 断言空转（VACUOUS）**")
        # 变异 M8：期望值与实际不符时也要报（防"期望值被悄悄改成实际值"）
        f_m8 = check_refine_limits(lambda k: 999 if k != "max_tokens" else 3000)
        if f_m8:
            _rec(PASS, "I4 变异自证 M8（mock 返回 999/3000）", "已被抓住：%s" % f_m8[0])
        else:
            _rec(FAIL, "I4 变异自证 M8（mock 返回 999/3000）", "**未被抓住 → 断言空转（VACUOUS）**")
        # I4b：修订输入的裁剪必须**头尾同留**（原先 `str(content)[:6000]` 只留头 →
        #      报告结论在末尾，被整段丢掉）
        f_r2 = check_clip(_rg._clip, cap=300)
        if f_r2:
            _rec(FAIL, "I4b 修订输入头尾采样（RefineGate._clip）", "; ".join(f_r2))
        else:
            _rec(PASS, "I4b 修订输入头尾采样（RefineGate._clip）", "头/尾均保留")

        # I4c：报告窗口 >= 自身产出能力（conv 371 实测：12000 装不下 19011 字符的报告）
        f_i4c = check_refine_window(_rg._report_in_chars, _rg._max_tokens)
        if f_i4c:
            _rec(FAIL, "I4c 报告窗口 >= 产出能力（真实实现）", "; ".join(f_i4c))
        else:
            _rec(PASS, "I4c 报告窗口 >= 产出能力（真实实现）",
                 "report_in_chars=%d >= max_tokens(%d)×2.5=%d"
                 % (_rg._report_in_chars(), _rg._max_tokens(), int(_rg._max_tokens() * 2.5)))
        # 变异 M10：还原 12000（conv 371 之前的值）→ 必须被抓住
        f_m10 = check_refine_window(lambda: 12000, _rg._max_tokens)
        if f_m10:
            _rec(PASS, "I4c 变异自证 M10（还原 12000）", "已被抓住：%s" % f_m10[0])
        else:
            _rec(FAIL, "I4c 变异自证 M10（还原 12000）", "**未被抓住 → 断言空转（VACUOUS）**")

    # ④d I4d：**评审输入窗口**必须覆盖报告实际长度（2026-09-20 conv 372 实测 —— 第 6 层截断）
    #     为什么必须单列：这一层在 `workflows/nodes.py::_evaluate_content`，**不在 refine.py 里**，
    #     前四条（I1~I4c）全查不到它。它是"报告越修越长、评审看到的比例越小"的元凶：
    #     报告 22,409 字符被 `[:2000]` 截到 8.9% → 评审判「t2/t3 无实质内容」，而那两节确实存在。
    try:
        from workflows.nodes import FlowNodesMixin as _FN
        _ev_clip = _FN._clip_for_eval
    except Exception as e:                                       # noqa: BLE001
        _rec(FAIL, "导入 workflows.nodes.FlowNodesMixin._clip_for_eval",
             "%s: %s" % (type(e).__name__, e))
        _ev_clip = None
    if _ev_clip is not None:
        _probe = _eval_probe_len()
        f_i4d = check_eval_window(_ev_clip)
        if f_i4d:
            _rec(FAIL, "I4d 评审窗口覆盖报告长度（真实实现）", "; ".join(f_i4d))
        else:
            _rec(PASS, "I4d 评审窗口覆盖报告长度（真实实现）",
                 "探针 %d 字符（= refine.max_tokens(%d)×%.1f）→ 保留 %d 字符（未裁）"
                 % (_probe, int(_probe / EVAL_CHARS_PER_TOKEN), EVAL_CHARS_PER_TOKEN,
                    len(_ev_clip("甲" * _probe))))
        # 变异 M11：还原 `str(content)[:2000]` —— conv 372 之前的写法
        f_m11 = check_eval_window(lambda t: str(t)[:2000])
        if f_m11:
            _rec(PASS, "I4d 变异自证 M11（还原 [:2000]）", "已被抓住：%s" % f_m11[0])
        else:
            _rec(FAIL, "I4d 变异自证 M11（还原 [:2000]）", "**未被抓住 → 断言空转（VACUOUS）**")
        # ── 变异 M12（2026-09-20 新增）：补「探针写死」这个**门禁盲区**的自证 ──────────
        # 旧实现 `probe_len=22409` 的后果：抬 `refine.max_tokens` 却漏抬 `eval_in_chars` 时
        # I4d 恒 PASS（只测 22,409）→ 红不了，但评审实际看不到尾部（重演 conv 372）。
        # M12a（结构级）：探针默认值必须是 None（= 运行时推导），写死即失败。
        try:
            import inspect as _ins
            _d = _ins.signature(check_eval_window).parameters["probe_len"].default
            if _d is None:
                _rec(PASS, "I4d 变异自证 M12a：探针未写死（默认 None = 运行时推导）",
                     "当前探针=%d" % _probe)
            else:
                _rec(FAIL, "I4d 变异自证 M12a：探针未写死（默认 None = 运行时推导）",
                     "**默认值被写死为 %r → 抬高 max_tokens 后本判据会失效**" % (_d,))
        except Exception as _e:                                   # noqa: BLE001
            _rec(FAIL, "I4d 变异自证 M12a", "%s: %s" % (type(_e).__name__, _e))
        # M12b（行为级反例）：窗口只留 24,000 字符，去接 max_tokens=12000 时的探针 34,800
        #   → 必须判「不足」。这一条直接钉住「漏抬 eval_in_chars」这个 bug 形态。
        _m12b = check_eval_window(lambda t: str(t)[:24000],
                                  probe_len=int(12000 * EVAL_CHARS_PER_TOKEN))
        if _m12b:
            _rec(PASS, "I4d 变异自证 M12b：窗口 24000 < 探针 34800 → 已抓住", _m12b[0])
        else:
            _rec(FAIL, "I4d 变异自证 M12b：窗口 24000 < 探针 34800 → 已抓住",
                 "**未被抓住 → 断言空转（VACUOUS）**")
        # 源码级：**只查 `_evaluate_content` 函数体**（不能扫全文件！）
        #   —— 实测 nodes.py:593（pubsub 节点）也有 `str(content)[:2000]`，那是**展示用途**、
        #   与评审窗口无关；扫全文件会把它误判成"本层未修"（实测 23/24，误报 1 条）。
        try:
            import inspect
            _ev_src = inspect.getsource(_FN._evaluate_content)
            if _has_code_literal(_ev_src, r"\[:2000\]"):
                _rec(FAIL, "I4d 源码级：_evaluate_content 内已无 `[:2000]`", "旧写法仍在")
            else:
                _rec(PASS, "I4d 源码级：_evaluate_content 内已无 `[:2000]`（只查该函数体）")
        except Exception as e:                                   # noqa: BLE001
            _rec(FAIL, "I4d 源码级：_evaluate_content 检查", "%s: %s" % (type(e).__name__, e))

    # ④c I5：汇总输入必须含**完整交付物**，不得用协议摘要替换（2026-09-19 conv 370 实测）
    #     为什么单列：Task 10 引入「结构化摘要消费」时，`_sum_items` 把 `result` **替换**成摘要
    #     （~250 字符/子任务）→ `plan_summary` 实测输入仅 1,072 tokens，而 t1/t2/t3 交付物原文
    #     合计 12,998 字符 → 报告只能如实写「交付物在约束条件处截断」，质量门禁 62/100。
    #     注：前四项（I1~I4）修的是「**切**得太狠」，这一项修的是「**换**错了东西」——
    #     同为「汇总看不到交付物」，症状相似、根因不同，别混改。
    try:
        _ssrc5 = open(os.path.join(repo, "agent", "pipeline_parts", "stream.py"),
                      encoding="utf-8", errors="replace").read()
        f_i5 = check_summary_input(_ssrc5)
        if f_i5:
            _rec(FAIL, "I5 汇总输入含完整交付物（真实实现）", "; ".join(f_i5))
        else:
            _rec(PASS, "I5 汇总输入含完整交付物（真实实现）",
                 "`_mit['result'] = 摘要 + 完整交付物`（非纯摘要替换）")
        # 变异 M9：还原「纯摘要替换」—— 正是 conv 370 的病
        f_m9 = check_summary_input(_mut_summary_back(_ssrc5))
        if f_m9:
            _rec(PASS, "I5 变异自证 M9（还原纯摘要替换）", "已被抓住：%s" % f_m9[0])
        else:
            _rec(FAIL, "I5 变异自证 M9（还原纯摘要替换）", "**未被抓住 → 断言空转（VACUOUS）**")
    except Exception as e:                                       # noqa: BLE001
        _rec(FAIL, "I5 汇总输入含完整交付物", "%s: %s" % (type(e).__name__, e))

    # ⑤ 源码级：旧硬编码点必须已消失（防止"改了函数但调用点仍切一刀"）
    #    判据走 `_has_code_literal`（**只认真实代码**）——注释/docstring 里记录旧写法不算命中，
    #    否则"把坑写在文档里"反而会让自检变红（实测误报 1 条）。
    try:
        psrc = open(os.path.join(repo, "workflows", "planner.py"), encoding="utf-8", errors="replace").read()
        qsrc = open(os.path.join(repo, "task_queue.py"), encoding="utf-8", errors="replace").read()
        ssrc = open(os.path.join(repo, "agent", "pipeline_parts", "stream.py"),
                    encoding="utf-8", errors="replace").read()
        rsrc = open(os.path.join(repo, "workflows", "refine.py"),
                    encoding="utf-8", errors="replace").read()
        _checks = [
            (psrc, r"it\.get\('result'\) or ''\)\[:500\]", "planner.py 仍存在 `[:500]`"),
            (qsrc, r"result\[:4000\]", "task_queue.py 仍存在 `result[:4000]`"),
            (ssrc, r"it\.get\('result'\) or ''\)\[:400\]", "stream.py 降级路仍存在 `[:400]`"),
            (rsrc, r"it\.get\('result'\) or ''\)\[:400\]", "refine.py 仍存在交付物 `[:400]`"),
            (rsrc, r"items_txt\[:2000\]", "refine.py 仍存在 `items_txt[:2000]`"),
            (rsrc, r"str\(content\)\[:6000\]", "refine.py 仍存在 `str(content)[:6000]`（只留头）"),
            (rsrc, r"max_tokens=3000", "refine.py 仍存在 `max_tokens=3000`"),
        ]
        bad = [msg for src, pat, msg in _checks if _has_code_literal(src, pat)]
        if bad:
            _rec(FAIL, "源码级：旧硬编码截断点已清除", "; ".join(bad))
        else:
            _rec(PASS, "源码级：旧硬编码截断点已清除（只认真实代码；注释里的「旧写法记录」不算命中）",
                 "planner[:500] / task_queue[:4000] / stream[:400] / refine[:400]/[:2000]/[:6000]/mt3000")
    except Exception as e:
        _rec(FAIL, "源码级：旧硬编码截断点已清除", "%s: %s" % (type(e).__name__, e))

    # ⑥ 配置契约：新键必须存在，且**关系不变式成立**（不写死具体数值）
    #
    # 【2026-10-04 修正：写死期望值 ⇒ 门禁随配置调大而失效】
    #   这段原本把 summary_max_tokens / max_tokens / eval_max_tokens 硬编码为
    #   8000 / 8000 / 3072。但这三个键已被 **P1-27 有意调大**（16384 / 16384 / 8192），
    #   config.py 里留有明确注释："实测 max_ct=8192 已触顶被截断"。
    #   ⇒ 于是：本地与 CI **同时**报这条 FAIL（23/26），卡住了整个 CI。
    #   而且它连带把 I4c / I4d 拖红 —— 因为那两条量的是"窗口 vs 产出能力"，
    #   max_tokens 调大后产出能力变大、窗口没跟着变 ⇒ 关系真的不成立了。
    #
    # 【正确的判据】门禁要守的是**关系**，不是某次调参的数值：
    #   · 键必须存在（有默认值）；
    #   · 窗口 ≥ 该键可产出的字符数（这才是"报告尾部不会被裁掉"的真条件）。
    # 这样将来再调大 max_tokens，门禁会自动要求同步放大窗口 —— 正是它该做的。
    try:
        from core import config as _cfg
        # 键的存在性（这些是"必须有的默认值"，不含数值大小）
        want = {
            ("delegation", "summary_item_max_chars"): 1600,
            ("delegation", "summary_total_chars"): 12000,
            ("delegation", "summary_floor_chars"): 600,
            ("delegation", "subtask_result_keep_chars"): 20000,
        }
        # refine 段：除下面三个"窗口/产出"键外，其余仍按值校验
        for k, v in _REFINE_EXP.items():
            # 这两个是**窗口**，与 report_in_chars 同类；它们的大小由 I4c/I4d 的
            # 关系不变式管（窗口 ≥ 产出能力），不写死—— 2026-10-04 起它们已随
            # max_tokens 同步放大到 48000，再按 24000 校验就是"门禁比需求更滞后"。
            if k not in ("max_tokens", "report_in_chars", "eval_in_chars"):
                want[("refine", k)] = v
        want[("refine", "enabled")] = True
        want[("refine", "max_rounds")] = 2
        want[("refine", "pass_score")] = 70
        # 产出能力类键：只要求"存在且为正"，大小交给关系不变式管
        bad_cfg = []
        # 产出能力类键：只要求"存在且为正"，大小交给关系不变式管
        for sec, key in (("delegation", "summary_max_tokens"),
                         ("refine", "max_tokens"),
                         ("refine", "eval_max_tokens"),
                         # 窗口类同样只要求为正 —— 它们必须 ≥ 产出能力，
                         # 由 I4c / I4d 量关系，不在这里写死数值。
                         ("refine", "report_in_chars"),
                         ("refine", "eval_in_chars")):
            v = _cfg.get(sec, key, None)
            if not (isinstance(v, int) and v > 0):
                bad_cfg.append("%s.%s=%r（应为正整数）" % (sec, key, v))
        bad = list(bad_cfg)
        for (sec, key), exp in want.items():
            got = _cfg.get(sec, key, None)
            if got != exp:
                bad.append("%s.%s=%r（期望 %r）" % (sec, key, got, exp))
        if bad:
            _rec(FAIL, "配置契约：delegation 5 键 + refine 8 键默认值（窗口类按值/ 产出类按正整数）",
                 "; ".join(bad))
        else:
            _rec(PASS, "配置契约：delegation 5 键 + refine 8 键默认值齐备"
                        "（窗口类按值校验；产出类只要求正整数，大小由 I4c/I4d 的关系不变式管）")
    except Exception as e:
        _rec(FAIL, "配置契约：delegation 5 键 + refine 8 键默认值", "%s: %s" % (type(e).__name__, e))

    # ⑦ 向后兼容：缺键时 get 必须回落到调用方默认（旧 config 文件不炸）
    try:
        from core import config as _cfg
        v = _cfg.get("delegation", "__not_exist__", "FALLBACK")
        if v == "FALLBACK":
            _rec(PASS, "向后兼容：缺键回落调用方默认", 'get(...,"FALLBACK")→"FALLBACK"')
        else:
            _rec(FAIL, "向后兼容：缺键回落调用方默认", "得到 %r" % v)
    except Exception as e:
        _rec(FAIL, "向后兼容：缺键回落调用方默认", "%s: %s" % (type(e).__name__, e))

    return _summary()


def _summary():
    print("=" * 90)
    n_fail = sum(1 for s, _, _ in _results if s == FAIL)
    n_pass = sum(1 for s, _, _ in _results if s == PASS)
    print("断言汇总：%d/%d 通过" % (n_pass, len(_results)))
    if n_fail:
        print("失败项：")
        for s, t, d in _results:
            if s == FAIL:
                print("  - %s  | %s" % (t, d))
    print("=" * 90)
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
