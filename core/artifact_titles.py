# -*- coding: utf-8 -*-
"""产物标题的「系统性兜底名」单一真源（2026-10-01，P1-7）。

**为什么单独成模块**：「AI 生成文档」「AI 生成报告」「{kind}产物」这三个名字**不是用户起的名字**，
而是写入侧在「内容里没有 title 字段」时的**兜底默认值**。原本它们散落在 2 个文件 3 处
（`agent/utils.py:225` 报告兜底、`:277` 长文档兜底、`services/artifact_materializer.py:183` 二次物化兜底），
而**消费侧**（`agent/session_artifacts.py` 的「本会话既有产物」摘要）又需要判定「这个标题有没有信息量」——
判据一旦各写一份，写入侧改兜底名就会让消费侧**静默漏判**（本仓「同一坑多份措辞不同的副本必然漂移」的
老问题）。故收敛到 `core/`（最低层，`agent/` 与 `services/` 都可引）。

**判定语义**：标题 == 兜底名 ⇒ 该产物**从未被人命名过** ⇒ 它对「本会话既有产物」摘要**零信息量**
（甚至有害：模型可能以为存在一个叫「AI 生成文档」的东西，进而引用一个不存在的名字）。
实测（2026-10-01，真库 `artifacts` 70 行）：占位名 25 行 = **35.7%**。

**为什么不用「以『产物』结尾」这类正则**：会误伤真实标题（「设计产物」「需求产物」）。
这里的判据是**闭集精确匹配**，集合来自写入侧的实际取值：
- `FALLBACK_*`：`agent/utils.py` 两处字面量；
- `{kind}产物`：`services/artifact_materializer.py:183` 用 `subtask_artifacts.kind` 拼，
  而该 kind 受 `services/subtask_protocol.py:22 KIND_VALUES` 校验 ⇒ 值域是闭集。
  `tools/verify/verify_multiturn_context.py` 有一条**契约断言**守着「本集合覆盖 KIND_VALUES」，
  KIND_VALUES 变动会在自检里报出来，而不是静默漏判。
"""

# ── 写入侧兜底名（改这里 = 同时改写入与消费；不要再在别处写第二份字面量）──
FALLBACK_REPORT_TITLE = "AI 生成报告"      # agent/utils.py 报告产物（无 report_title/title 时）
FALLBACK_DOCUMENT_TITLE = "AI 生成文档"    # agent/utils.py 长 markdown 文档产物（无 title 时）

# 二次物化的 kind 值域（= services/subtask_protocol.KIND_VALUES 的副本；
# 副本无法避免（core 不能反向依赖 services），故由自检的契约断言守着不漂移）
SUBTASK_KINDS = ("sysml", "requirement", "report", "doc")


def materialize_fallback_title(kind):
    """`services/artifact_materializer.py` 的兜底名（`f"{kind}产物"`）。写入侧请调它，别手拼。"""
    return "%s产物" % (kind or "other")


# 全部系统性兜底名（闭集）
PLACEHOLDER_TITLES = frozenset(
    (FALLBACK_REPORT_TITLE, FALLBACK_DOCUMENT_TITLE)
    + tuple(materialize_fallback_title(k) for k in SUBTASK_KINDS))


def is_placeholder_title(title):
    """标题是否为系统性兜底名。

    精确匹配（去首尾空白），**不是**正则 —— 避免误伤「设计产物」这类真实标题。
    """
    return (title or "").strip() in PLACEHOLDER_TITLES
