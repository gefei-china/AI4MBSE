# -*- coding: utf-8 -*-
"""system_prompt 分层守护（P1-26）—— 防止「动态块混入可缓存前缀」与「两条路径口径再漂移」。

背景（两条独立证据）：
  ① Anthropic Claude Code 的 system prompt 用显式分界线 `SYSTEM_PROMPT_DYNAMIC_BOUNDARY`
     切开静态层（可缓存）与动态层；并配 `DANGEROUS_uncachedSystemPromptSection(name, compute,
     reason必填)` 命名约定，让「破坏缓存」在 code review 里可见。本脚本是该约定的**自动化替代**。
  ② DeepSeek Context Caching 按**前缀完整匹配**：动态块一旦排在静态块之前，缓存前缀就在该处
     断掉，其后所有内容（含跨 agent 完全一致的规则块）全部无法命中。

实测教训（本脚本要防住的具体事故）：
  · P1-24 / P1-25 的「顺序优化」**只改了 execute.py**，`stream.py`（流式主路径）完全未生效 ——
    两份内联拼接 ⇒ 改一处漏一处。P1-26 已收敛为 `common.assemble_system_prompt` 单一真源。
  · 修复前 `_build_skill_prompt(intent, user_input, ...)` 排在第 3 位，**依赖 user_input ⇒
    几乎每轮都变**，其后 4 个 L1 全局静态块（本体/边界/输出/引用规则）从未命中过。

判据设计（**绑定源码事实，非手写清单**）：
  用 AST 从 `execute.py` / `stream.py` 抽取 `assemble_system_prompt({...})` 里每个 key 对应的
  value 表达式，收集其引用的变量名。据此**客观判定层归属**：
    · 可缓存区（L1 全局静态 + L2 agent 级）的块**不得引用**任何动态变量；
    · 动态区（L3/L4）的块**必须引用**至少一个动态变量。
  这样「把动态块塞回前缀」会被直接抓红，而不依赖人去维护一份分类名单（名单会漂移）。

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
    assemble_system_prompt,
)

_EXECUTE = os.path.join(_ROOT, "agent", "pipeline_parts", "execute.py")
_STREAM = os.path.join(_ROOT, "agent", "pipeline_parts", "stream.py")

#: 动态变量：一旦出现在某个块的表达式里，该块内容就会随会话/轮次变化 ⇒ 必须归动态区。
#: （`agent_def` 不在其中：它属 L2，在同一 agent 内稳定，允许出现在可缓存区。）
DYNAMIC_VARS = {
    "user_input", "intent", "slots", "context_text", "skill_block", "skill_name",
    "att_text", "user_ctx", "report_prompt", "branch", "conversation_id", "user",
}

#: 允许某条路径**不提供**的块（内容可选）。`tool_rules` 目前仅流式路径注入。
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


def extract_block_refs(src_text):
    """从源码文本抽取 `assemble_system_prompt({...})` 的 {key: 引用变量集合}；找不到返回 None。

    接受**文本**而非路径，便于对变异后的源码重跑（变异自证）。
    """
    try:
        tree = ast.parse(src_text)
    except SyntaxError:
        return None
    found = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        fname = getattr(fn, "id", None) or getattr(fn, "attr", None)
        if fname != "assemble_system_prompt" or not node.args:
            continue
        d = node.args[0]
        if not isinstance(d, ast.Dict):
            continue
        out = {}
        for k, v in zip(d.keys, d.values):
            if not isinstance(k, ast.Constant) or not isinstance(k.value, str):
                continue
            names = {n.id for n in ast.walk(v) if isinstance(n, ast.Name)}
            out[k.value] = names
        found = out
    return found


def count_calls(src_text):
    """统计源码里 `assemble_system_prompt(` 作为普通标识符调用的次数（文本级，用于契约断言）。"""
    return len(re.findall(r"(?<![\w.])assemble_system_prompt\s*\(", src_text))


#: 旧的 ad-hoc 顺序拼接形态（`+ f"{self._build_xxx()}"` 或 `+ self._build_xxx()`）。
#: 一旦复现，说明有人绕过单一真源自己排顺序 ⇒ 必然与另一条路径漂移。
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
print("[C] 两条路径的源码契约（防口径再漂移）")
print("=" * 78)

_src = {}
for name, path in (("execute.py", _EXECUTE), ("stream.py", _STREAM)):
    _src[name] = io.open(path, encoding="utf-8").read()

for name, text in _src.items():
    check("C1 %s 调用 assemble_system_prompt（唯一真源）" % name,
          count_calls(text) == 1, "calls=%d" % count_calls(text))
    check("C2 %s 无 ad-hoc 顺序拼接残留" % name,
          not _ADHOC_PAT.search(text), "matched=%r" % (_ADHOC_PAT.search(text).group(0) if _ADHOC_PAT.search(text) else None))

_refs = {}
for name, text in _src.items():
    r = extract_block_refs(text)
    check("C3 %s 的分块 dict 可解析" % name, r is not None)
    _refs[name] = r or {}

for name, r in _refs.items():
    if not r:
        continue
    unknown = set(r) - set(PROMPT_BLOCK_ORDER)
    check("C4 %s 无未登记块 key" % name, not unknown, "unknown=%s" % sorted(unknown))
    required = set(PROMPT_BLOCK_ORDER) - OPTIONAL_KEYS
    missing = required - set(r)
    check("C5 %s 必需块齐全（缺 %s 会被静默丢弃）" % (name, sorted(OPTIONAL_KEYS)),
          not missing, "missing=%s" % sorted(missing))
    # **核心判据**：可缓存区内的块不得引用任何动态变量
    bad = {k: sorted(r[k] & DYNAMIC_VARS) for k in PROMPT_CACHEABLE_KEYS
           if k in r and (r[k] & DYNAMIC_VARS)}
    check("C6 **主判据** %s 可缓存区块未引用动态变量" % name, not bad, "violations=%s" % bad)
    # 反向：动态区的块必须引用至少一个动态变量（防静态块被误放动态区，白丢缓存）
    weak = [k for k in PROMPT_DYNAMIC_KEYS if k in r and not (r[k] & DYNAMIC_VARS)]
    check("C7 %s 动态区块均确实依赖动态输入" % name, not weak, "suspect=%s" % weak)

print()
print("=" * 78)
print("[M] 变异自证（判据不是空转）")
print("=" * 78)

# M1：模拟「有人把动态块 skill_prompt 塞回可缓存区」——判据必须抓红。
_mut_cacheable = tuple(PROMPT_CACHEABLE_KEYS) + ("skill_prompt",)
_r = _refs.get("stream.py") or _refs.get("execute.py") or {}
_m1_bad = {k: sorted(_r.get(k, set()) & DYNAMIC_VARS) for k in _mut_cacheable
           if (_r.get(k, set()) & DYNAMIC_VARS)}
check("M1 把 skill_prompt 塞回可缓存区 → 被抓住（它引用 user_input）",
      bool(_m1_bad), "m1_bad=%s" % _m1_bad)

# M2：模拟「有人删掉/改名单一真源调用」——源码契约必须抓红。
_mut_src = _src.get("stream.py", "").replace("assemble_system_prompt({", "OLD_assemble({")
check("M2 改名单一真源调用 → 文本契约抓红",
      count_calls(_mut_src) == 0 and extract_block_refs(_mut_src) is None)
check("M2b 变异后 ad-hoc 判据不受影响（独立判据）",
      not _ADHOC_PAT.search(_mut_src))

# M3：模拟「有人把静态块 ontology 误放动态区」——C7 反向判据抓红。
_mut_dynamic = tuple(PROMPT_DYNAMIC_KEYS) + ("ontology",)
_m3_weak = [k for k in _mut_dynamic if k in _r and not (_r.get(k, set()) & DYNAMIC_VARS)]
check("M3 把静态块 ontology 误放动态区 → 反向判据抓红（它不引用动态变量）",
      "ontology" in _m3_weak, "weak=%s" % _m3_weak)

print()
print("=" * 78)
print("PASS %d / FAIL %d" % (len(_PASS), len(_FAIL)))
print("=" * 78)
sys.exit(1 if _FAIL else 0)
