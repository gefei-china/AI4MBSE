# -*- coding: utf-8 -*-
"""verify_compression_anchor.py —— 守卫：五档压缩必须能裁到**主路径**产出的块。

## 为什么需要它（2026-10-11 实测发现的真缺陷，P0）

`_apply_total_budget`（总闸）在**真实会话 conv=514** 上实测：

    下发前 messages = **13,692 tok**，总闸上限 10,000 ⇒ **总闸确实触发了**
    但逐档执行后 system 区 = **11,692 tok，一档都没裁掉**（档1~档4 delta 全 0）

根因（不是参数传错，是**锚点与组装结构脱钩**）：
  · 压缩侧认得的块名只有 4 个（`compression.py` 的 `_NEXT_BLOCK_MARKERS` +
    `collapse_summary_block` 的正则）：`【更早对话摘要】`/`【历史对话概览】`/`【用户上传资料…】`
  · 而这两个历史块名**只在旧三层窗口回退路径**产出（`history.py:760/764`，
    即"话题打标失败 ⇒ `_load_history_legacy`"时才走）
  · **主路径**（`_tag_topics` 打标成功，生产里绝大多数）产出的是另外三种块：
        【话题：<label> 摘要】        ← history.py:467
        【相关历史片段（第 N 轮…】    ← history.py:448
        【本会话既有产物】            ← session_artifacts
    压缩器对它们**一个都不认**。

⇒ 后果：话题打标成功的会话，总闸形同虚设，只在日志留下一行
  "[context_total] 总闸触发…裁掉 0 tok"，**静默失效**。
  这直接违反本仓既定纪律："每次降档打日志留痕——总闸若静默，等于没做"。

## 判据（全部走**真实代码路径**，不用替身预算器）

  [1] 组装侧真实产出的块名清单（从 history.py/session_artifacts.py 提取）
      必须全部出现在压缩侧的可识别锚点集合里
  [2] 用**真实** `_apply_context_budget` + 真实块喂 `apply_tier`：
      逐档 delta 必须出现负值（真裁到了），否则总闸空转
  [3] 变异自证：把压缩侧锚点替换成非主路径块名 ⇒ [1][2] 必须翻红

为什么不用"替身预算器"（首版就是这么写的，判据立不住）：
  替身会绕过锚点定位，自己去裁所有块 ⇒ 于是 [2] 恒绿、[3] 恒红（空转）。
  锚点是否被认得，只能用**真实** `_apply_context_budget` 来测——
  它内部只裁 `【更早对话摘要】/【历史对话概览】`，正是被检验的对象。

用法： ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_compression_anchor.py
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from core.token_counter import count_tokens  # noqa: E402
from agent.pipeline_parts import compression as cp  # noqa: E402

COMP = os.path.join(ROOT, "agent", "pipeline_parts", "compression.py")
HIST = os.path.join(ROOT, "agent", "pipeline_parts", "history.py")

_results = []


def check(name, ok, detail=""):
    _results.append(ok)
    print("[%s] %s%s" % ("OK  " if ok else "FAIL", name, ("　— " + detail) if detail and not ok else ""))


def fblock(name, n=40):
    return "\n%s\n%s\n" % (name, ("内容片段。") * n)


def build_system(include_legacy=True):
    """按**主路径真实形态**拼 system 区（块名与 history.py 逐字一致）。

    ⚠️ include_legacy=False 是本守卫的**关键对照**：
      生产里话题打标成功 ⇒ 主路径块**独占**历史区，旧块名一个都不出现。
      若样本里混进 `【更早对话摘要】`，压缩器裁它就能让 [2] 变绿 ⇒ 掩盖真实缺陷。
      首版正是这么写的（样本混了旧块名）⇒ [2] 假绿、[1] 真红，两条判据自相矛盾。
    """
    s = ("你是资深需求分析专家，职责是将模糊用户诉求转化为结构化需求规格。\n"
         + fblock("【话题：预评审校验 摘要】", 30)
         + fblock("【相关历史片段（第 5 轮 · 助手，相关度 0.476）】", 30)
         + fblock("【本会话既有产物】", 30)
         + fblock("【用户上传资料（优先依据）】", 30))
    if include_legacy:
        s += fblock("【更早对话摘要】", 30)
    return s


def run_tiers(budget_fn, truncate_fn, include_legacy=False):
    sysm = build_system(include_legacy)
    before = count_tokens(sysm)
    prev = sysm
    deltas = []
    for tier in range(1, len(cp.TIER_SCALES) + 1):
        cur = cp.apply_tier(prev, "", tier, budget_fn=budget_fn, truncate_fn=truncate_fn)
        deltas.append(count_tokens(cur) - count_tokens(prev))
        prev = cur
    return before, deltas, count_tokens(prev)


def anchors_of(src):
    """压缩侧"能认出来的块名"全集（从源码文本提取，不手抄）。

    ⚠️ 必须同时覆盖两种写法：
      ① `_NEXT_BLOCK_MARKERS = ("...", ...)` 里的裸字符串
      ② `collapse_summary_block` 正则里的 \\u3010…\\u3011 转义写法
      首版只抓 ①，导致压缩器实际认得的 4 个锚点只认出 1 个 ⇒ 判据本身失真。
    """
    found = set()
    for m in re.finditer(r'["\'](【[^"\'\n]{2,40})["\']', src):
        found.add(m.group(1))
    for m in re.finditer(r"\\u3010([^\\\n]{2,40})\\u3011", src):     # 正则里的转义块名
        found.add("【%s】" % m.group(1))
    return found


def main_path_block_names():
    """组装侧**真实**产出的块名（从 history.py / session_artifacts.py 提取）。

    只取**字面量前缀**：形如 `【话题：{g['topic']} 摘要】` 要归一为 `【话题：`
    （后半段是变量，压缩侧只需认得前缀即可定位块）。
    """
    names = set()
    for fp in (HIST, os.path.join(ROOT, "agent", "session_artifacts.py")):
        s = open(fp, encoding="utf-8").read()
        for m in re.finditer(r'f?"(【[^"{\n]{2,30})', s):
            names.add(m.group(1))
    # 归一：去掉 f-string 变量段，只留可定位的字面量块头
    norm = set()
    for n in names:
        n = n.rstrip("\\n")
        if n.startswith("【") and "{" not in n:
            norm.add(n)
    norm.add("【话题：")     # 形如 【话题：{...} 摘要】 的字面量前缀
    return norm


def main():
    print("=== 五档压缩锚点覆盖守卫（P0：总闸静默失效）===")
    from agent import get_pipeline
    pipe = get_pipeline()
    real_budget, real_trunc = pipe._apply_context_budget, pipe._truncate_tokens

    comp_src = open(COMP, encoding="utf-8").read()
    anchors = anchors_of(comp_src)
    print("  压缩侧可识别锚点：%s" % "、".join(sorted(anchors)))

    # [1] 主路径块名覆盖
    produced = main_path_block_names()
    covered, uncovered = [], []
    for b in produced:
        # 形如「【话题：」的前缀式块名：压缩侧只要认得它的**固定部分**即可
        ok = any(a in b or b.startswith(a) for a in anchors)
        (covered if ok else uncovered).append(b)
    check("[1] 压缩锚点覆盖组装侧真实产出的全部块名", not uncovered,
          "未覆盖 %d 个：%s" % (len(uncovered), sorted(uncovered)[:6]))

    # [2] 真实路径逐档必须真裁 —— 样本**只含主路径块**（生产真实形态）
    before, deltas, after = run_tiers(real_budget, real_trunc, include_legacy=False)
    cuts = [d for d in deltas if d < 0]
    check("[2] 纯主路径块 + 真实预算器 ⇒ 逐档真的裁掉 token（总闸不空转）",
          bool(cuts),
          "before=%d 各档 delta=%s 末值=%d（无一档裁到 ⇒ 与 conv=514 实测一致：总闸静默失效）"
          % (before, deltas, after))

    # [3] 变异自证：锚点改成非主路径块名 ⇒ [1][2] 必须翻红
    mutated = comp_src
    for b in sorted(produced):
        fixed = b[:6] if len(b) > 6 else b
        mutated = mutated.replace(fixed, "__MUT__" + fixed.replace("【", ""))
    ns = {}
    try:
        exec(compile(mutated, COMP + "<mut>", "exec"), ns)
        mut_anchors = anchors_of(mutated)
        mut_uncovered = [b for b in produced
                         if not any(a in b or b.startswith(a) for a in mut_anchors)]
        global cp
        _real_cp = cp
        cp = ns
        _b2, d2, _a2 = run_tiers(real_budget, real_trunc)
        cp = _real_cp
        red = bool(mut_uncovered) and not [d for d in d2 if d < 0]
    except Exception as e:
        red = True
        mut_uncovered = ["变异体异常:%s" % type(e).__name__]
        d2 = []
    check("[3] 变异自证：锚点替换为非主路径块名 ⇒ 判据必须翻红", red,
          "守卫空转：变异后未覆盖=%s delta=%s" % (mut_uncovered[:3], d2))

    n_fail = sum(1 for r in _results if not r)
    print("\n%d pass / %d fail" % (len(_results) - n_fail, n_fail))
    return 1 if n_fail else 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())