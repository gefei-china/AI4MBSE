"""门禁取源码的共享工具（2026-10-05 沉淀）。

背景（实测踩坑，勿再重复）：
`textwrap.dedent(inspect.getsource(fn))` 在本工程**会静默失效** ——
dedent 取的是「所有非空行的最小公共缩进」，而本工程多个被测函数体内含
**多行字符串（prompt / 提示词）里的顶格行** ⇒ 公共缩进被算成 0 ⇒ dedent 什么都不做。

后果是**极隐蔽的**：函数的每一行缩进都比预期多 4 格，于是门禁里写死的锚点
（如 `"    if _digest:\\n        plan_prompt += ("`）全部静默不命中 ⇒
变异注入 `find` 返回 -1 ⇒ 拼出畸形源码 ⇒ exec 抛 IndentationError；
更糟的是「锚点命中」断言若写成 `!= -1` 之外还会**假绿**。

本模块提供两个正确实现：
  * `src_of(fn)`      —— 按**首行缩进**剥离（不被多行字符串欺骗）
  * `find_block(src, head, tail)` —— 按**行内容**定位起止（不写死缩进）

配套：`audit_dedent_drift.py` 会全仓扫描仍在用 `textwrap.dedent(getsource(...))`
的地方并报告哪些函数当前正处在「dedent 失效」状态。
"""
import inspect
import re


def dedent_src(raw):
    """按**首行**缩进剥离，而非全局最小缩进。

    只剥离恰好等于首行缩进的前缀；多行字符串里的顶格内容行不受影响。
    """
    if not raw:
        return raw
    raw = raw.replace("\r\n", "\n")
    lines = raw.split("\n")
    head = lines[0]
    ind = len(head) - len(head.lstrip())
    if ind <= 0 or not head[:ind].strip() == "":
        return raw
    prefix = head[:ind]
    out = []
    for ln in lines:
        out.append(ln[ind:] if ln.startswith(prefix) else ln)
    return "\n".join(out)


def src_of(fn):
    """正确 dedent 的 `inspect.getsource`。门禁一律用它替代 `textwrap.dedent(...)`。"""
    return dedent_src(inspect.getsource(fn))


def find_line(src, needle, nth=1):
    """按**行内容**（lstrip 后相等/包含）定位，返回 (行下标, 该行原文)。

    写死缩进的锚点会随源码结构微调而静默失效 —— 一律走这里。
    """
    hit = 0
    for i, ln in enumerate(src.split("\n")):
        if needle in ln:
            hit += 1
            if hit == nth:
                return i, ln
    return -1, None


def find_block(src, head_needle, tail_needle, start_after=0):
    """定位 [head 行, tail 行) 区间，返回 (start_idx, end_idx)。

    两者都按**行内容**匹配；`tail_needle` 从 head 之后开始找。
    找不到返回 (-1, -1)，调用方应断言 `start >= 0 and end > start`。
    """
    lines = src.split("\n")
    s = -1
    for i in range(start_after, len(lines)):
        if head_needle in lines[i]:
            s = i
            break
    if s < 0:
        return -1, -1
    for j in range(s + 1, len(lines)):
        if tail_needle in lines[j]:
            return s, j
    return s, -1


def indent_of(line):
    return len(line) - len(line.lstrip())


def dedent_status(fn):
    """返回 (原始首行缩进, textwrap.dedent 后的首行缩进, 是否失效)。

    ⚠️ 判据是「**dedent 之后**首行缩进是否为 0」，不是「dedent 前后是否相等」。
    （第一版写成比较两者是否相等，结果对「前后都是 4」的失效情形判成了 ok。）
    """
    import textwrap
    raw = inspect.getsource(fn).replace("\r\n", "\n")
    l0 = raw.split("\n")[0]
    l1 = textwrap.dedent(raw).split("\n")[0]
    return indent_of(l0), indent_of(l1), indent_of(l1) > 0


def re_indent(src):
    """把一段源码整体左移到顶格（用于 exec 单函数体）。"""
    return dedent_src(src)
