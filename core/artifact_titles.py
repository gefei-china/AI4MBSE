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
import re

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


# ══════════════════════════════════════════════════════════════════════════════
# 从内容取名（P1-9，2026-10-01）：治「写入侧仍在产生占位名」
#
# 兜底名的**根因**不是"忘了写 title"，而是写作侧（LLM 结构化输出）本来就没给 title 字段。
# 实测真库（2026-10-01，artifacts 85 行）：占位名 26 行 = 30.6%，分布极不均 ——
#   document **23/23 全是占位名**（整个 document 类的标题无一例外），report 3/6，
#   sysml 0/38、code 0/18（这两类的标题是代码里拼的，本来就有信息量）。
# 治法是**从内容里取**：那 26 条的内容**100% 都能取到东西**（首个 md 标题行 14 / 首个非空行 12 /
# 取不到 0），例如 `# 巡飞弹参数变更影响分析报告（推进剂加注量 + 巡飞速度 · 3 层深度）`。
#
# 为什么不调 LLM 起名：① 这是**写入热路径**（每条产物都要过），LLM 调用会拖慢归档；
# ② 内容首标题本来就是"作者自己写的名字"，比模型再编一个更忠实；③ 纯函数才能离线断言与变异自证。
# ══════════════════════════════════════════════════════════════════════════════

#: 派生标题的长度上限。实测真库**真实**标题 p50=13 / max=16（全是「代码文件 v0.1（SysML）」这类），
#: 而派生来的可能是整句话 ⇒ 必须截断。40 是「够装下一句短标题、又不会把正文整段塞进标题列」的折中；
#: 前端列表本来就有 CSS ellipsis 兜底，这里主要是防止 DB 里存进超长串。
DEFAULT_TITLE_MAX_LEN = 40

#: markdown 标题行：`# `~`#### `（最多 3 格缩进，尾部 `#` 可选）
_HEADING_RE = re.compile(r"^[ \t]{0,3}#{1,4}[ \t]+(.+?)[ \t]*#*[ \t]*$", re.M)

#: 标题**左端**剥除集：markdown 记号（`#`/`>`/`*`/`-`/`+`/`_`/`~`）与装饰符号。
_LEAD_TRIM = " \t\r\n#>*-+_~·—－"

#: 标题**右端**剥除集。⚠️ 与左端**故意不对称** —— 这里**不含任何右括号**（`）`/`】`/`」`/`》`/`"`）。
#: 首版把两端共用一份集合，结果正常标题「巡飞弹…报告（推进剂加注量 + 巡飞速度 · 3 层深度）」
#: 的右括号被 `strip()` 无条件削掉 ⇒ 变成一个**括号不成对**的标题。括号是标题的**结构**，
#: 不是边缘噪声；开头残留括号罕见（`（Mock 回答）已收到…` 那种由左端规则负责）。
_TAIL_TRIM = " \t\r\n#*_~·—－。，、；：！？.,;:!?"


def _clean_title(text, max_len=DEFAULT_TITLE_MAX_LEN):
    """折叠空白 → 去行内记号 → 分左右剥除 → 按 max_len 截断（超出加 `…`）。"""
    s = re.sub(r"\s+", " ", (text or "")).strip()
    # 行内记号：`**粗体**` / `` `代码` `` 的**闭合**记号落在串中间，两端剥除规则管不到
    # （实测 `**编制说明（按评审意见修订）**：本报告仅整合 t1–t5` 会留下一个裸 `**`）。
    # 标题里出现裸 `**`/反引号必然是标记残留、不可能是内容，故直接去掉（**不碰单个 `*` 与 `_`**，
    # 后者可能是通配符或标识符的一部分）。
    s = s.replace("**", "").replace("`", "")
    s = s.lstrip(_LEAD_TRIM).rstrip(_TAIL_TRIM)
    if len(s) > max_len:
        s = s[:max_len].rstrip(_TAIL_TRIM) + "…"
    return s


def derive_title_from_content(content, max_len=DEFAULT_TITLE_MAX_LEN) -> str:
    """从产物内容里取一个**有信息量**的标题（替代固定兜底名）。

    顺序（信息密度由高到低）：
      ① 首个 markdown 标题行（`#`~`####`）—— 作者显式写的标题，最可信；
      ② 首个非空行（`_clean_title` 会剥掉 `#`/`>`/`*` 等记号）；
      ③ 都取不到 → 返回 `""`，**由调用方退到兜底名**（不在这里硬造一个假名字）。

    **若结果本身仍是兜底名形态，返回 `""`** —— 否则 `is_placeholder_title` 那一侧的过滤会失效，
    等于把 bug 从写入侧搬到读取侧（改了名字但依然零信息量）。

    纯函数：无 IO、无 LLM、可离线断言与变异自证。
    """
    if not content:
        return ""
    text = content if isinstance(content, str) else str(content)
    m = _HEADING_RE.search(text)
    cand = m.group(1) if m else ""
    if not cand:
        for ln in text.replace("\r", "").split("\n"):
            if ln.strip():
                cand = ln
                break
    t = _clean_title(cand, max_len)
    if not t or is_placeholder_title(t):
        return ""
    return t


#: 产物 content（dict 形态）里可能承载正文的键，按优先级排列。
#: 值域取自 `services/subtask_protocol` 的产物 schema 与仓内既有字段名
#: （`document`/`doc`/`report` 三类在 `services/artifact_materializer` 里都走 markdown 预览）。
_TEXT_KEYS = ("markdown", "body", "content", "text", "summary", "code")


def extract_text_from_content(content) -> str:
    """从产物 content 里取出可读正文，供取名用（取不到返回 `""`）。

    存在的理由：`services/artifact_materializer` 的 content 是 `json.loads(content_json)` 的结果，
    **可能是 dict**；直接丢给 `derive_title_from_content` 会对字典做 `str()`，
    得到 `"{'markdown': '...'}"` 这种 JSON 残片当标题（比兜底名更糟）。
    """
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        for k in _TEXT_KEYS:
            v = content.get(k)
            if isinstance(v, str) and v.strip():
                return v
    return ""
