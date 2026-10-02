# -*- coding: utf-8 -*-
"""system_prompt 分层守护（P1-26 建；P1-28 适配块内容单一真源）。

背景（两条独立证据）：
  ① Anthropic Claude Code 的 system prompt 用显式分界线 `SYSTEM_PROMPT_DYNAMIC_BOUNDARY`
     切开静态层（可缓存）与动态层；并配 `DANGEROUS_uncachedSystemPromptSection(name, compute,
     reason必填)` 命名约定，让「破坏缓存」在 code review 里可见。本脚本是该约定的**自动化替代**。
  ② DeepSeek Context Caching 按**前缀完整匹配**：动态块一旦排在静态块之前，缓存前缀就在该处
     断掉，其后所有内容（含跨 agent 完全一致的规则块）全部无法命中。

实测教训（本脚本要防住的具体事故）：
  · P1-24 / P1-25 的「顺序优化」**只改了 execute.py**，`stream.py`（流式主路径）完全未生效 ——
    两份内联拼接 ⇒ 改一处漏一处。P1-26 收敛**顺序**为 `common.assemble_system_prompt`。
  · P1-28（2026-10-02）进一步收敛**块内容**为 `common.build_prompt_blocks`（两条路径此前
    各写一份 20 键 dict，已漂移 `role` / `tool_rules` / `skill_block` 三处）。本脚本随之上修：
    **判据的抽取目标从「两个调用点的 dict 表达式」改为「`build_prompt_blocks` 内的字面量」**。

判据设计（**绑定源码事实，非手写清单**）：
  用 AST 抽 `build_prompt_blocks` 里每个 key 对应的 value 表达式，收集其引用（`ast.Name` +
  `ctx.<attr>`）。据此**客观判定层归属**：
    · 可缓存区（L1 全局静态 + L2 agent 级）的块**不得引用**任何动态变量；
    · 动态区（L3/L4）的块**必须引用**至少一个动态变量。
  这样「把动态块塞回前缀」会被直接抓红，而不依赖人去维护一份分类名单（名单会漂移）。

⚠️ 2026-10-02 的一个坑（本文件被重写的原因）：旧版 C4~C7 写在 `if not r: continue` 之后 ——
  当抽取失败（`r` 为 None，重构后正是如此）时这些断言**被静默跳过**，脚本只报 5 条 FAIL
  而**不报"判据没跑"**。故本次给每条断言加"必须真的执行到"的显式检查（C3b）。

运行：`./.venv/Scripts/python.exe tools/verify/verify_prompt_layering.py`
"""
import ast
import io
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from agent.pipeline_parts.common import (  # noqa: E402
    PROMPT_BLOCK_ORDER,
    PROMPT_CACHEABLE_KEYS,
    PROMPT_DYNAMIC_KEYS,
    PROMPT_LAYER_BOUNDARY,
    TOOL_RULES_TEXT,
    assemble_system_prompt,
)

_P = os.path.join(_ROOT, "agent", "pipeline_parts")
_EXECUTE = os.path.join(_P, "execute.py")
_STREAM = os.path.join(_P, "stream.py")
_COMMON = os.path.join(_P, "common.py")

#: 动态变量：一旦出现在某个块的表达式里，该块内容就会随会话/轮次变化 ⇒ 必须归动态区。
#: （`agent_def` 不在其中：它属 L2，在同一 agent 内稳定，允许出现在可缓存区。）
DYNAMIC_VARS = {
    "user_input", "intent", "slots", "context_text", "skill_block", "skill_name",
    "att_text", "user_ctx", "report_prompt", "branch", "conversation_id", "user",
}

#: 允许**不在 dict 字面量里**的块（改为条件赋值）。`tool_rules` 目前仅流式路径注入。
OPTIONAL_KEYS = {"tool_rules"}

_PASS = []
_FAIL = []


def check(name, cond, detail=""):
    if cond:
        _PASS.append(name)
        print("  PASS  %s" % name)
    else:
        _FAIL.append(name)
        print("  FAIL  %s%s" % (name, ("  <<< " + str(detail)) if detail else ""))


def _refs_of_value(v) -> set:
    """收集一个表达式引用的名字：`ast.Name.id` + `ctx.<attr>` 的 attr。

    后者是 P1-28 引入的形态（块构造改为 `ctx.xxx`）—— 必须一并收集，否则
    `ctx.user_input` 只会被看成 `ctx`，动态性判据全部失效。
    """
    names = {n.id for n in ast.walk(v) if isinstance(n, ast.Name)}
    attrs = {n.attr for n in ast.walk(v)
             if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "ctx"}
    return names | attrs


def extract_block_refs(src_text, func_name="build_prompt_blocks"):
    """从源码文本抽取 `func_name` 函数体内 `blocks = {...}` 字面量 → `{key: 引用集合}`。

    返回 `None` = 抽取失败（**调用方必须据此判红**，不得静默跳过后续断言）。
    """
    try:
        tree = ast.parse(src_text)
    except SyntaxError:
        return None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            for sub in ast.walk(node):
                if isinstance(sub, ast.Assign) and isinstance(sub.value, ast.Dict):
                    out = {}
                    for k, v in zip(sub.value.keys, sub.value.values):
                        if isinstance(k, ast.Constant) and isinstance(k.value, str):
                            out[k.value] = _refs_of_value(v)
                    return out or None
    return None


def count_calls(src_text, fn="assemble_system_prompt"):
    """统计源码里该函数作为普通标识符被调用的次数（文本级，用于契约断言）。"""
    return len(re.findall(r"(?<![\w.])%s\s*\(" % fn, src_text))


#: 旧的 ad-hoc 顺序拼接形态（`+ f"{self._build_xxx()}"` 或 `+ self._build_xxx()`）。
_ADHOC_PAT = re.compile(
    r"\+\s*(?:f?\"\{self\._build_|self\._build_)"
    r"(ontology_hint|boundary_hint|output_rules|citation_rules|model_code_req|"
    r"skill_prompt|prompt_template|memory_hint|model_context|project_memory)"
)

print("=" * 78)
print("[A] 分层常量自洽（纯函数级）")
print("=" * 78)

_overlap = set(PROMPT_CACHEABLE_KEYS) & set(PROMPT_DYNAMIC_KEYS)
check("A1 可缓存区与动态区 key 无重叠", not _overlap, "overlap=%s" % sorted(_overlap))
check("A2 两层并集 == PROMPT_BLOCK_ORDER",
      set(PROMPT_CACHEABLE_KEYS) | set(PROMPT_DYNAMIC_KEYS) == set(PROMPT_BLOCK_ORDER),
      "order=%s" % list(PROMPT_BLOCK_ORDER))
check("A3 拼装顺序中可缓存区在前（常量层面）",
      list(PROMPT_BLOCK_ORDER)[:len(PROMPT_CACHEABLE_KEYS)] == list(PROMPT_CACHEABLE_KEYS))
check("A4 分界线标记非空（等价 SYSTEM_PROMPT_DYNAMIC_BOUNDARY）",
      bool(PROMPT_LAYER_BOUNDARY) and isinstance(PROMPT_LAYER_BOUNDARY, str))

print()
print("=" * 78)
print("[B] 拼装行为（功能级）")
print("=" * 78)

_all = {k: "<%s>" % k for k in PROMPT_BLOCK_ORDER}
_out = assemble_system_prompt(_all)
_seq = re.findall(r"<([a-z_]+)>", _out)
_last_cache = max(_seq.index(k) for k in PROMPT_CACHEABLE_KEYS)
_first_dyn = min(_seq.index(k) for k in PROMPT_DYNAMIC_KEYS)
check("B1 **主判据**：拼装结果里静态区严格先于动态区",
      _last_cache < _first_dyn, "last_cache=%d first_dyn=%d" % (_last_cache, _first_dyn))
check("B2 所有块都被拼出（无丢失）",
      set(_seq) == set(PROMPT_BLOCK_ORDER), "missing=%s" % sorted(set(PROMPT_BLOCK_ORDER) - set(_seq)))
check("B3 空块/None 跳过（不留多余空行）",
      assemble_system_prompt({"role": "R\n", "ontology": "", "tools": None, "retrieval": "X"}) == "R\nX")
check("B4 未登记的 key 被忽略（防新增块漏分层）",
      "NO_SUCH_BLOCK" not in assemble_system_prompt({"role": "R\n", "zzz": "NO_SUCH_BLOCK"}))

print()
print("=" * 78)
print("[C] 源码契约（防口径再漂移）")
print("=" * 78)

_src = {}
for name, path in (("execute.py", _EXECUTE), ("stream.py", _STREAM), ("common.py", _COMMON)):
    _src[name] = io.open(path, encoding="utf-8").read()

# ── C1/C2：两个调用点仍走唯一真源、且不得自己排顺序 ──
for name in ("execute.py", "stream.py"):
    text = _src[name]
    check("C1 %s 调用 assemble_system_prompt（唯一真源）" % name,
          count_calls(text) == 1, "calls=%d" % count_calls(text))
    check("C2 %s 无 ad-hoc 顺序拼接残留" % name,
          not _ADHOC_PAT.search(text), "matched=%r" % (_ADHOC_PAT.search(text).group(0)
                                                        if _ADHOC_PAT.search(text) else None))

# ── C3：块内容只剩一处（common.build_prompt_blocks 的 dict 字面量）──
_r = extract_block_refs(_src["common.py"])
check("C3 common.build_prompt_blocks 的 blocks 字面量可解析", _r is not None)
check("C3b 抽取成功（**防后续断言被静默跳过**：抽取失败时必须判红）", bool(_r))

if _r:
    unknown = set(_r) - set(PROMPT_BLOCK_ORDER)
    check("C4 无未登记块 key", not unknown, "unknown=%s" % sorted(unknown))
    required = set(PROMPT_BLOCK_ORDER) - OPTIONAL_KEYS
    missing = required - set(_r)
    check("C5 必需块齐全（缺 %s 会被静默丢弃）" % sorted(OPTIONAL_KEYS),
          not missing, "missing=%s" % sorted(missing))
    # **核心判据**：可缓存区内的块不得引用任何动态变量
    bad = {k: sorted(_r[k] & DYNAMIC_VARS) for k in PROMPT_CACHEABLE_KEYS
           if k in _r and (_r[k] & DYNAMIC_VARS)}
    check("C6 **主判据** 可缓存区块未引用动态变量", not bad, "violations=%s" % bad)
    # 反向：动态区的块必须引用至少一个动态变量（防静态块被误放动态区，白丢缓存）
    weak = [k for k in PROMPT_DYNAMIC_KEYS if k in _r and not (_r[k] & DYNAMIC_VARS)]
    check("C7 动态区块均确实依赖动态输入", not weak, "suspect=%s" % weak)

# ── C8：P1-28 新增 —— 两条路径的差异必须**显式**存在于调用点（防"顺手抹平"）──
_c8 = {
    "execute.py": ("role_cap=0", "include_tool_rules=False"),
    "stream.py": ("role_cap=_AGENT_ROLE_CAP", "include_tool_rules=True"),
}
for name, (a, b) in _c8.items():
    text = re.sub(r"\s+", "", _src[name])          # 忽略空白差异（参数可换行书写）
    ok = (a.replace(" ", "") in text) and (b.replace(" ", "") in text)
    check("C8 %s 的路径差异已显式参数化（%s / %s）" % (name, a, b), ok)

# ── C9：文案唯一真源 —— tool_rules 正文只在 common 定义一次 ──
_hits = [n for n in _src if TOOL_RULES_TEXT.strip()[:24] in _src[n]]
check("C9 工具约束文案只在 common.py 定义（不散落各路径）",
      _hits == ["common.py"], "found_in=%s" % _hits)

# ── C10：块内容一处生成 —— 两个调用点不得再**直传内联 dict** ──
# ⚠️ 判据刻意收窄到「assemble_system_prompt({ ... })」这一形态：初版搜裸 `"role":` 之类的
#    块 key，结果命中的是 `messages=[{"role": "system"...}]` 与返回值 dict 里的同名键 ⇒
#    9 条全假阳。**判据要绑"结构形态"，不要绑"文本里出现过这个词"**。
_leaks = [n for n in ("execute.py", "stream.py")
          if re.search(r"assemble_system_prompt\s*\(\s*\{", _src[n])]
check("C10 两个调用点不再直传内联 dict（块内容已单点化）", not _leaks, "leaks=%s" % _leaks)

print()
print("=" * 78)
print("[M] 变异自证（判据不是空转）")
print("=" * 78)

# M1：模拟「有人把动态块 skill_prompt 塞回可缓存区」——判据必须抓红。
_mut_cacheable = tuple(PROMPT_CACHEABLE_KEYS) + ("skill_prompt",)
_rr = _r or {}
_m1_bad = {k: sorted(_rr.get(k, set()) & DYNAMIC_VARS) for k in _mut_cacheable
           if (_rr.get(k, set()) & DYNAMIC_VARS)}
check("M1 把 skill_prompt 塞回可缓存区 → 被抓住（它引用 ctx.user_input）",
      bool(_m1_bad), "m1_bad=%s" % _m1_bad)

# M2：模拟「有人改掉唯一真源调用」——源码契约必须抓红（锚点随重构更新）。
_mut_src = _src["stream.py"].replace("assemble_system_prompt(build_prompt_blocks(",
                                     "OLD_assemble(build_prompt_blocks(")
check("M2 改名单一真源调用 → 文本契约抓红（锚点命中）",
      _mut_src != _src["stream.py"] and count_calls(_mut_src) == 0)
check("M2b 变异后 ad-hoc 判据不受影响（独立判据）", not _ADHOC_PAT.search(_mut_src))

# M3：模拟「有人把静态块 ontology 误放动态区」——C7 反向判据抓红。
_mut_dynamic = tuple(PROMPT_DYNAMIC_KEYS) + ("ontology",)
_m3_weak = [k for k in _mut_dynamic if k in _rr and not (_rr.get(k, set()) & DYNAMIC_VARS)]
check("M3 把静态块 ontology 误放动态区 → 反向判据抓红（它不引用动态变量）",
      "ontology" in _m3_weak, "weak=%s" % _m3_weak)

# M4：模拟「有人把块内容又抄回调用点」——C10 必须抓红（判据与 C10 同一形态）。
_mut_leak = _src["stream.py"] + '\n_X = assemble_system_prompt({"role": "x"})\n'
check("M4 块内容被抄回调用点（直传内联 dict）→ C10 抓红",
      bool(re.search(r"assemble_system_prompt\s*\(\s*\{", _mut_leak))
      and not re.search(r"assemble_system_prompt\s*\(\s*\{", _src["stream.py"]))

# M5：模拟「抽取目标函数被改名」——C3b 必须判红（防静默跳过）。
_mut_common = _src["common.py"].replace("def build_prompt_blocks(", "def build_prompt_blocks_RENAMED(")
check("M5 抽取目标被改名 → C3b 抓到（不得静默跳过后续断言）",
      extract_block_refs(_mut_common) is None)

print()
print("=" * 78)
print("PASS %d / FAIL %d" % (len(_PASS), len(_FAIL)))
print("=" * 78)
sys.exit(1 if _FAIL else 0)
