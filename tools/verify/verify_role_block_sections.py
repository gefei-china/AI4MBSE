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

    fails, changed, n_struct = survey(pipe, agents, cap)
    check("[I1] 有段结构的 Agent 必留段齐全（职责/输入/输出/工作流程/约束/输出示例）",
          not fails, "缺失: %s" % fails[:3])
    check("[I2] 无段结构的 Agent 行为逐字不变（不影响 90% 的 Agent）",
          not changed, "被改动: %s" % changed[:5])
    print("  （有段结构 %d 个 / 无段结构 %d 个）" % (n_struct, len(agents) - n_struct))

    # I3：可裁段折叠为占位且留有"可查原文"提示
    tgt = next((sp for _n, sp in agents if "视图规范参考" in sp), "")
    if tgt:
        out = pipe._build_role_block(_FakeDef(tgt), cap=cap)
        folded = pipe._ROLE_TRIM_PLACEHOLDER in out
        check("[I3a] 可裁段（视图规范参考）超上限时折叠为占位", folded)
        check("[I3b] 占位文案说明「可查原文」（否则模型不知去哪找）",
              "原文" in pipe._ROLE_TRIM_PLACEHOLDER or "Agent 定义" in pipe._ROLE_TRIM_PLACEHOLDER)
    else:
        check("[I3] 存在含『视图规范参考』段的 Agent（前置条件）", False)

    # I4：必留段本身超 cap 时不再腰斩
    if tgt:
        out = pipe._build_role_block(_FakeDef(tgt), cap=10)   # 极端 cap
        kept_example = "输出示例" in out and "```sysml" in out
        check("[I4] 必留段超 cap 时**不再腰斩**（示例代码块完整保留）", kept_example,
              "输出=%d 字符" % len(out))
    else:
        check("[I4] 必留段超 cap 时不再腰斩（前置条件）", False)

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