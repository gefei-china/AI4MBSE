# -*- coding: utf-8 -*-
"""verify_role_block_sections.py —— 守卫：Agent 角色块**不得被字符腰斩**。

## 为什么需要它（2026-11-11 可行性验证发现）

`_AGENT_ROLE_CAP = 1200`（**字符**，common.py:112）用于控流式路径的 Agent prompt 体量，
实现是 `prompt.py::_build_role_block` 里一句 `raw[:cap] + "…"`。

实测 8 个视图生成 Agent 的 prompt 是 **2361~5956 tok**，而 1200 字符 ≈ 1098 tok
⇒ 现状是**砍掉 74~82%**。更糟的是砍的位置：

| Agent | 完整 | 旧(腰斩) | 被整段切掉的部分 |
|---|---|---|---|
| 活动图生成 | 5956 | 1098 (**-82%**) | `## 约束` / `### 视图规范参考` / **`## 输出示例`** |
| 参数视图生成 | 4110 | 1061 (-74%) | 同上 |
| …其余 6 个 | 2450~3403 | 1019~1183 | 同上 |

而这些 Agent 的 prompt 里明写"**语法形式以【输出示例】为唯一依据**"
⇒ 砍掉示例 = 模型自由发挥 = 回到 v2_constraints 模块注释里记录的原始事故
（"一个 156 行的TMS 模型踩了 7 条语义错"）。

**为什么长期没被发现**：`verify_sysml_baseline_cases.py` 只校验 L0 硬约束卡的内容，
**从不检查 Agent prompt 是否被腰斩** ⇒ 覆盖率盲区。

## 不变式

  I1 流式路径（cap>0）对**有段结构**的 Agent：必留段（职责/输入/输出/工作流程/
     约束/输出示例）**一个都不能少**，且**原始标题行必须原样保留**
     （拼成 `职责` 而非 `## 职责` 会让结构退化成散文本 —— 首版踩过）
  I2 无段结构的短 prompt（chat/knowledge_qa 等 18 个）：行为**逐字不变**
     （否则本次改动会影响 90% 的 Agent，违反"最小影响面"）
  I3 可裁段（视图规范参考）超上限时折叠为占位，且占位里说明"可查原文"
  I4 装配后仍超 cap 时**不再腰斩**（Anthropic: minimal ≠ short）

## 变异自证

  M1 把装配改回 `raw[:cap] + "…"` ⇒ I1 必须判红
  M2 装配时丢掉原始标题行 ⇒ I1 必须判红（段名认不出）
  M3 让无段结构 Agent 也走折叠 ⇒ I2 必须判红

用法： ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_role_block_sections.py
退出码：0 = 全绿；1 = 有失败或空转。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

_results = []


def check(name, ok, detail=""):
    _results.append(ok)
    print("[%s] %s%s" % ("OK  " if ok else "FAIL", name, ("　— " + detail) if detail and not ok else ""))


def load_agents():
    """从生产库**只读**取 active Agent 的 name/system_prompt。"""
    import sqlite3
    from core.config import DB_PATH
    conn = sqlite3.connect("file:%s?mode=ro" % DB_PATH.replace("\\", "/"), uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT name, system_prompt FROM agents WHERE status='active' "
        "AND LENGTH(system_prompt)>0 ORDER BY LENGTH(system_prompt) DESC").fetchall()
    conn.close()
    return [(r["name"], r["system_prompt"] or "") for r in rows]


class _FakeDef:
    def __init__(self, sp):
        self.system_prompt = sp


# ── ★ 2026-11-26：夹具样本（把"被测对象"从已退役的长 Agent 手里接回来）──────
# 事故形态（第三次同族，前两次是 verify_load_aware_assign 的 D 组与
# verify_compression_anchor 的锚点）：本门禁的 I3/I4 与 M1~M3 **全靠**真库里存在
# "超 cap 且含 `### 视图规范参考`"的 Agent，而那批正是 2026-10-14（commit 07ffea0
# 「B4-4 下线视图 Agent 双轨」）退役的 8 个视图生成 Agent（prompt 2361~5956 tok）。
# 退役后实测：active 24 个里只有 3 个有段结构，且**没有一个超过 cap=8000**
# ⇒ `tgt` 为空（I3/I4 直接判红 + I3a/I3b 两条断言根本没执行）
# ⇒ M1/M2/M3 的变异都抓不到差异（报"守卫空转"）。
# **症状看起来像"守卫失效"，真因是"被测样本已不存在"。**
# 修法：把样本变成门禁**自带**的夹具 —— 判据测的是装配函数的行为，
#      与"库里恰好有没有长 Agent"无关；真库那份仍照跑（I1/I2 对真 Agent 有意义）。
_FIXTURE_ROLE = "\n".join([
    "# 你是 SysML v2 活动图生成 Agent。",
    "",
    "## 职责",
    "依据需求与功能模型产出活动图代码。" + "活动图的控制流必须完整表达。" * 150,
    "",
    "## 输入",
    "需求条目、功能清单、上下文工程。" + "输入不足时应主动澄清而不是猜测。" * 150,
    "",
    "## 输出",
    "纯 SysML v2 代码块，不含解释文字。" + "代码块语言标记必须是 sysml。" * 150,
    "",
    "## 工作流程",
    "1. 读取需求 → 2. 识别动作 → 3. 建立控制流 → 4. 自检。" + "每步都要留痕。" * 150,
    "",
    "## 约束",
    "语法形式以【输出示例】为唯一依据。" + "不得自创语法。" * 150,
    "",
    "### 视图规范参考",
    "视图规范的长篇参考内容（可裁段）。" + "此处是可裁剪的规范正文。" * 150,
    "",
    "## 输出示例",
    "```sysml",
    "action def Demo {",
    "    action a1;",
    "    action a2;",
    "    first a1 then a2;",
    "}",
    "```",
])

# 无段结构的超长夹具：专供 M3（"无段结构 Agent 被改写 ⇒ I2 必须判红"）。
# ⚠️ 少了它，M3 同样空转 —— 真库现在的无段结构 Agent 最长也只有 2040 tok（远小于 cap），
#    M3 的变异体没有任何样本可改写，于是"变异抓不到差异"被误读成"守卫失效"。
_FIXTURE_FLAT = "你是一个通用助手。请按用户要求完成任务并给出结论。" * 400


def _fixture_agents(cap):
    """夹具 + 真库 Agent 合并；返回 (agents, 有段夹具长度, 无段夹具长度)。

    ⚠️ 两个夹具都必须**真的超过 cap**，否则 I3/I4 与 M1~M3 又会退化成空转
    —— 这正是本次事故的形态，所以「夹具够长」本身写成断言（见 [F1]/[F3]）。
    """
    return ([("__fixture_view_role__", _FIXTURE_ROLE),
             ("__fixture_flat_role__", _FIXTURE_FLAT)],
            len(_FIXTURE_ROLE), len(_FIXTURE_FLAT))


def survey(pipe, agents, cap):
    """跑一遍全部 Agent，返回 (structured_fail, unstructured_changed, stats)。"""
    structured_fail, unstructured_changed = [], []
    n_struct = 0
    for name, sp in agents:
        a = _FakeDef(sp)
        old = (sp[:cap] + "…") if len(sp) > cap else sp
        new = pipe._build_role_block(a, cap=cap)
        titles = [t for t, _, _ in pipe._split_role_sections(sp) if t]
        if not titles:
            # I2：无段结构必须逐字不变
            if new != old:
                unstructured_changed.append(name)
            continue
        n_struct += 1
        out_titles = [t for t, _, _ in pipe._split_role_sections(new) if t]
        # ★ 判据口径：要求「**原本存在的**必留段一个都不能丢」，
        #   而非「必须凑齐 6 个段」。
        #   反例（实测）：`MBSE建模总体负责人` 只有一个 `## 输出格式` 段，
        #   它本来就没有「职责/输入/约束/输出示例」；凑齐 6 段是**判据自己写错了**
        #   （首版犯了这个错，把一个正确实现判成 FAIL —— 门禁自身失真比没门禁更坏）。
        keep_needed = [k for k in pipe._ROLE_MUST_KEEP
                       if any(k in t for t in titles)]
        keep_got = [k for k in pipe._ROLE_MUST_KEEP
                    if any(k in t for t in out_titles)]
        missing = [k for k in keep_needed if k not in keep_got]
        # I1 附带：原始标题行必须原样保留（`## 职责` 不能被拼成 `职责`）
        heads_lost = [t for t, head, _b in pipe._split_role_sections(new)
                      if t and head and head.strip() not in new]
        if missing or heads_lost:
            structured_fail.append(
                (name, missing or ["原始标题行丢失:%s" % heads_lost[:2]]))
    return structured_fail, unstructured_changed, n_struct


def main():
    print("=== Agent 角色块段级装配守卫（防字符腰斩）===")
    from agent.pipeline_parts.common import _AGENT_ROLE_CAP
    from agent import get_pipeline
    from core.token_counter import count_tokens

    pipe = get_pipeline()
    agents = load_agents()
    cap = _AGENT_ROLE_CAP
    print("  `_AGENT_ROLE_CAP` = %d 字符；active Agent = %d 个"
          % (cap, len(agents)))

    # ★ 2026-11-26：夹具并入被测集（理由见 `_FIXTURE_ROLE` 上方注释）
    _fx, _fx_len, _flat_len = _fixture_agents(cap)
    check("[F1] 有段夹具确实超过 cap（否则 I3/I4 与 M1/M2 会退化成空转——本次事故形态）",
          _fx_len > cap, "夹具 %d 字符 ≤ cap %d" % (_fx_len, cap))
    check("[F2] 夹具含可裁段『视图规范参考』与必留段『输出示例』",
          "视图规范参考" in _FIXTURE_ROLE and "输出示例" in _FIXTURE_ROLE
          and "```sysml" in _FIXTURE_ROLE)
    check("[F3] 无段夹具确实超过 cap 且**无段标题**（否则 M3 空转）",
          _flat_len > cap and not any(l.lstrip().startswith("#") for l in _FIXTURE_FLAT.splitlines()),
          "夹具 %d 字符 ≤ cap %d" % (_flat_len, cap))
    agents = _fx + agents

    fails, changed, n_struct = survey(pipe, agents, cap)
    check("[I1] 有段结构的 Agent 必留段齐全（职责/输入/输出/工作流程/约束/输出示例）",
          not fails, "缺失: %s" % fails[:3])
    check("[I2] 无段结构的 Agent 行为逐字不变（不影响 90% 的 Agent）",
          not changed, "被改动: %s" % changed[:5])
    print("  （有段结构 %d 个 / 无段结构 %d 个）" % (n_struct, len(agents) - n_struct))

    # I3：可裁段折叠为占位且留有"可查原文"提示
    # ★ 2026-11-26：改从**夹具**取样本，不再靠真库里有没有那样的 Agent
    #   （那批 Agent 已于 2026-10-14 退役 ⇒ 原写法让 I3/I4 直接判红且 I3a/I3b 没执行）。
    tgt = _FIXTURE_ROLE
    out = pipe._build_role_block(_FakeDef(tgt), cap=cap)
    folded = pipe._ROLE_TRIM_PLACEHOLDER in out
    check("[I3a] 可裁段（视图规范参考）超上限时折叠为占位", folded)
    check("[I3b] 占位文案说明「可查原文」（否则模型不知去哪找）",
          "原文" in pipe._ROLE_TRIM_PLACEHOLDER or "Agent 定义" in pipe._ROLE_TRIM_PLACEHOLDER)
    # I3c：真库里若仍有含该段的 Agent，也要覆盖（有则测真库，无则如实记录，不判红）
    _real_tgt = next((sp for _n, sp in load_agents() if "视图规范参考" in sp), "")
    if _real_tgt:
        check("[I3c] 真库同类 Agent 同样被折叠为占位",
              pipe._ROLE_TRIM_PLACEHOLDER in pipe._build_role_block(_FakeDef(_real_tgt), cap=cap))
    else:
        print("  [INFO] 真库当前无含『视图规范参考』的 active Agent"
              "（8 个视图 Agent 已于 2026-10-14 退役）⇒ I3c 用夹具覆盖，不判红")

    # I4：必留段本身超 cap 时不再腰斩
    out = pipe._build_role_block(_FakeDef(tgt), cap=10)   # 极端 cap
    kept_example = "输出示例" in out and "```sysml" in out
    check("[I4] 必留段超 cap 时**不再腰斩**（示例代码块完整保留）", kept_example,
          "输出=%d 字符" % len(out))

    # 收益量化（供报告引用，非判据）
    rows = [(_n, count_tokens(sp),
             count_tokens((sp[:cap] + "…") if len(sp) > cap else sp),
             count_tokens(pipe._build_role_block(_FakeDef(sp), cap=cap)))
            for _n, sp in agents[:8]]
    print("\n  效果对照（前 8 个 Agent，token）：")
    print("  %-24s %7s %9s %9s" % ("agent", "完整", "旧(腰斩)", "新(段级)"))
    for n, a, b, c in rows:
        print("  %-24s %7d %9d %9d" % (n[:22], a, b, c))

    # ── 变异自证 ──────────────────────────────────────────────
    print("\n=== 变异自证 ===")
    import types

    # M1：还原字符腰斩
    orig_build = pipe._build_role_block
    pipe._build_role_block = lambda agent_def, cap=0: (
        (getattr(agent_def, "system_prompt", "") or "").strip()[:cap] + "…"
        if cap and len((getattr(agent_def, "system_prompt", "") or "").strip()) > cap
        else (getattr(agent_def, "system_prompt", "") or "").strip())
    f1, _, _ = survey(pipe, agents, cap)
    pipe._build_role_block = orig_build
    check("[M1] 还原字符腰斩 ⇒ I1 必须判红", bool(f1), "守卫空转")

    # M2：装配时丢原始标题行
    def _bad_assemble(self, raw, cap):
        secs = self._split_role_sections(raw)
        if not any(t for t, _, _ in secs):
            return raw[:cap] + "…"
        out = []
        for title, _head, body in secs:
            out.append(("%s\n%s\n" % (title, self._ROLE_TRIM_PLACEHOLDER))
                       if title and any(k in title for k in self._ROLE_TRIMMED)
                       else ("%s\n%s\n" % (title, body) if title else body))
        return "\n".join(out).strip()

    orig_asm = pipe._assemble_role_sections
    pipe._assemble_role_sections = types.MethodType(_bad_assemble, pipe)
    f2, _, _ = survey(pipe, agents, cap)
    pipe._assemble_role_sections = orig_asm
    check("[M2] 装配丢失原始标题行 ⇒ I1 必须判红", bool(f2), "守卫空转")

    # M3：让无段结构 Agent 也被折叠/改写
    def _bad_build3(self, agent_def, cap=0):
        raw = (getattr(agent_def, "system_prompt", "") or "").strip()
        if not raw:
            raw = "你是网络总体MBSE设计助手。"
        if not cap or len(raw) <= cap:
            return raw
        return raw[:cap] + "…" + "（已折叠）"
    pipe._build_role_block = types.MethodType(_bad_build3, pipe)
    _f3, c3, _ = survey(pipe, agents, cap)
    pipe._build_role_block = orig_build
    check("[M3] 无段结构 Agent 被改写 ⇒ I2 必须判红", bool(c3), "守卫空转")

    n_fail = sum(1 for r in _results if not r)
    print("\n%d pass / %d fail" % (len(_results) - n_fail, n_fail))
    return 1 if n_fail else 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())