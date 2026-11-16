# -*- coding: utf-8 -*-
"""产物命名唯一真源：给 SysML 代码产物起一个「看得出这是什么」的名字。

为什么需要它（米爸 2026-11-09 反馈原话）：
    「产物清单中代码文件名称都是代码块，都不知道具体这个代码是什么，
      需要在名称上体现出来，基于生成时的诉求与目标，自动生成文件名称，
      注意，对于多版本的情况要保持一致性。」

修复前的三条各自命名路径（同一语义各写一份，必然错位）：
    · `agent/utils.py` 第 3 节  → `代码文件 v0.1（SysML）`
    · `agent/utils.py` 兜底分支 → `代码块 1（sysml）`   ← 用户看到的就是这个
    · 工具取回路径            → `SysML 源码（工具取回 #1）`
    实际产出以「代码块 1（sysml）」为主：因为 `_vid`（版本 id）此前在无结构化视图时为空，
    版本分支根本不进，全部落到兜底分支。名字里零信息量，清单无法区分是哪个模型。

命名规则（显式、可测、不依赖 LLM）—— 按优先级取第一个命中的"主体名"：
    R1  `package <Name>`                     ← 最稳，模型/包的正式名
    R2  首个类型定义名（part def / requirement def / view def / constraint def …）
    R3  首个实例声明名（part x : X;）
    R4  会话诉求摘要（conversations.title 或首条用户消息）
    R5  兜底 `SysML 模型`

一致性（多版本同名）：
    同一个会话里，**首版定名后锁死**。后续版本只改版本后缀，主体不变——
    否则列表里会出现「LinkBudget → PowerBudget → BudgetModel」这种名字乱跳，
    反而比"代码块 1"更难追踪。锁定值写进 `artifacts.meta["name_key"]`。
"""
import re

# 单条主体名最大长度（中文按字符算；超长会挤爆产物列表的标题列）
MAX_SUBJECT = 28
# 诉求摘要最大长度
MAX_GOAL = 12

# R1：package / library package / model
#   注意：**不能排除冒号** —— `package A::Budget` 是合法的限定名，
#   排除 `:` 会让捕获止于 "A"（首版实测就是这么错的），去前缀交给 _clean_ident。
_RE_PACKAGE = re.compile(
    r"^[ \t]*(?:library\s+)?package\s+([^\s{;]+)", re.M)

# R2：类型定义名。SysML v2 的关键字都出现在 `xxx def Name` 形态里。
#      顺序 = 语义强度（构件 > 需求 > 约束 > 视图 > 行为 > 其他）
_DEF_KEYWORDS = [
    "part", "requirement", "constraint", "view", "viewpoint",
    "action", "state", "calculation", "analysis case", "verification case",
    "use case", "case", "attribute", "item", "port", "interface",
    "connection", "allocation", "flow", "enum", "metadata", "occurrence",
    "item def", "class", "datatype", "behavior", "function", "struct",
]
_RE_DEF = re.compile(
    r"^[ \t]*(?:abstract\s+)?(?:public|private|protected)?\s*(?:"
    + "|".join(re.escape(k) for k in _DEF_KEYWORDS)
    + r")\s+def\s+([^\s{;:\[<]+)", re.M)

# R3：实例声明（part x : X; / requirement r : R;）
_RE_INST = re.compile(
    r"^[ \t]*(?:part|requirement|constraint|action|state|attribute|item|port)\s+"
    r"([A-Za-z_][\w]*)\s*:", re.M)

# 语气词 / 客套话，用于从诉求里剥掉噪声
_GOAL_NOISE = re.compile(
    r"^(请|帮我|帮忙|麻烦|我想|我要|需要|能不能|可以|给我|帮忙生成|生成|输出|写出|写个|给个|来一个|来份)+")
_PUNCT = re.compile(r"[，。！？、,.!?:：；;\"'“”‘’（）()\[\]【】\s]+")


def _clean_ident(raw: str) -> str:
    """去掉限定前缀（A::B::C → C）、泛型/模板尾巴、以及外层引号。

    引号这层是实测补的：SysML v2 允许 `package '无人机飞控系统BDD'` 这种带引号标识符，
    不去掉的话产物名会变成「'无人机飞控系统BDD' · v0.6」（引号跟着进列表和文件名）。
    """
    s = str(raw or "").strip()
    if "::" in s:
        s = s.split("::")[-1]
    s = re.split(r"[<\[{]", s)[0]
    s = s.strip().strip("'\"`")
    return s.strip()


def extract_package_name(code_text: str) -> str:
    m = _RE_PACKAGE.search(code_text or "")
    return _clean_ident(m.group(1)) if m else ""


def extract_primary_def(code_text: str) -> str:
    """取语义最强的类型定义名：按关键字优先级，同级取最先出现。"""
    text = code_text or ""
    best, best_rank = "", 10 ** 6
    for rank, kw in enumerate(_DEF_KEYWORDS):
        pat = re.compile(
            r"^[ \t]*(?:abstract\s+)?(?:public|private|protected)?\s*"
            + re.escape(kw) + r"\s+def\s+([^\s{;:\[<]+)", re.M)
        m = pat.search(text)
        if m and rank < best_rank:
            best, best_rank = _clean_ident(m.group(1)), rank
    return best


def extract_instance_name(code_text: str) -> str:
    m = _RE_INST.search(code_text or "")
    return _clean_ident(m.group(1)) if m else ""


def summarize_goal(raw: str, limit: int = MAX_GOAL) -> str:
    """把用户诉求压成短摘要（纯规则，不用 LLM，避免又引入一次不确定性）。

    实测样例（conv 990541 的首条诉求）：
      「相对于V1的参数图，在sysml V2 规范中对应什么视图，如何表示，请给个示例代码」
      → 去掉标点/客套 →「相对于V1的参数图 在sysml V2 规范中对应什么视图 如何表示」
      → 截 12 字 →「相对于V1的参数图 在…」
    诉求摘要只是**辅助**，主体名（package/定义名）才是主标识，所以宁可短。
    """
    s = str(raw or "").strip().split("\n")[0]
    s = _PUNCT.sub(" ", s).strip()
    # 噪声词全局剔除（不只是开头）：「请给个」「帮我」「我想」出现在句中同样无信息
    s = re.sub(r"(请|帮我|帮忙|麻烦|我想|我要|需要|能不能|可以|给我|生成|输出|写出|写个|给个|来一个|来份)+", "", s)
    s = re.sub(r"\s{2,}", " ", s).strip()
    if not s:
        return ""
    if len(s) > limit:
        s = s[:limit].rstrip() + "…"
    return s


def derive_subject(code_text: str, goal_text: str = "") -> str:
    """按 R1→R5 选出"主体名"。纯函数，无 IO，可直接单测。"""
    pkg = extract_package_name(code_text)
    if pkg:
        return pkg[:MAX_SUBJECT]
    d = extract_primary_def(code_text)
    if d:
        return d[:MAX_SUBJECT]
    inst = extract_instance_name(code_text)
    if inst:
        return inst[:MAX_SUBJECT]
    g = summarize_goal(goal_text)
    if g:
        return g[:MAX_SUBJECT]
    return "SysML 模型"


def compose_title(subject: str, goal: str = "", version_label: str = "",
                  lang: str = "sysml") -> str:
    """主体 + 诉求 + 版本 → 产物标题。

    设计取舍：
      · 主体放最前 —— 用户在清单里扫的是"这是什么"，必须第一个可见；
      · 诉求只在**与主体不同**时附加，且去掉主体已含的词，防止
        「LinkBudget · LinkBudget 参数图示例」这种重复；
      · 版本后缀放最后且统一用 ` · vX.Y`，多版本排序时前缀完全一致（满足"保持一致"）。
    """
    subj = (subject or "").strip() or "SysML 模型"
    g = summarize_goal(goal)
    tail = ""
    # 诉求已被主体覆盖（主体是诉求的子串，或诉求是主体的子串）就不重复
    if g and g not in subj and subj not in g:
        # 去掉诉求里与主体重复的片段
        g2 = g.replace(subj, "").strip(" ·-—")
        if g2:
            tail = f" · {g2}"
    base = f"{subj}{tail}"
    if version_label:
        base = f"{base} · {version_label}"
    return base


def is_placeholder_title(title: str) -> bool:
    """判断是否为"零信息量"的历史命名（`代码块 1（sysml）` / `代码文件 v0.1（SysML）`）。

    用于历史回填：只有占位名才重命名，用户/模型自取的名字一律不动。
    """
    t = str(title or "").strip()
    if not t:
        return True
    return bool(re.match(r"^代码块\s*\d+\s*[（(]", t)
                or re.match(r"^代码文件\s*v?\d", t)
                or re.match(r"^SysML 源码（工具取回", t))


_FN_UNSAFE = re.compile(r'[\\/:*?"<>|\s]+')


def filename_for(subject: str, version_label: str = "", lang: str = "sysml") -> str:
    """产物清单里的"文件名"。`filename` 列此前全空（150 条 code 产物无一填写）。

    中文保留（现代系统与浏览器下载均支持），只替换文件系统非法字符；
    版本进文件名而不是靠列表序号区分，下载多个版本时不会互相覆盖。
    """
    ext = "kerml" if str(lang or "").lower().startswith("kerml") else "sysml"
    base = _FN_UNSAFE.sub("_", (subject or "model").strip()) or "model"
    if version_label:
        base = f"{base}_{version_label}"
    return f"{base}.{ext}"

