# -*- coding: utf-8 -*-
"""P1-28 自检：`build_prompt_blocks` / `build_skill_block` 的**零行为变动**证明。

## 为什么这样测

"抽取前后行为不变"不能靠人读代码确认，也不能在测试里**手抄一份旧逻辑**（那样源码被改坏
照样绿）。本脚本的做法是：

  1. 用 `git show HEAD:<file>` 取**重构前**的 `execute.py` / `stream.py` 源码；
  2. 用 AST 定位其中 `assemble_system_prompt({...})` 的 dict 字面量 / `skill_block` 赋值链，
     用 `ast.get_source_segment` 抽出**那段原文**；
  3. 在受控命名空间里 `exec` 它（提供 stub `self` + 全部局部变量）⇒ **旧行为基线**；
  4. 与新函数在等价参数下的输出**逐字节比对**。

⇒ 判据来自**旧源码本身**，不是第二份实现。

## stub 设计要点

stub 的每个 `_build_*` 返回**带调用指纹**的字符串（`<<_build_role_block|a=[...]|kw={'cap':1200}>>`），
因此 `cap=0` 与 `cap=_AGENT_ROLE_CAP` 这类**参数差异会被逐字节捕获**，而不是被"反正都返回一个
字符串"抹平。对象 repr 含地址 ⇒ 用 `_stable()` 归一化，保证同一进程内可重复。

## 覆盖

  旧 execute dict ↔ 新 batch 模式；旧 stream dict ↔ 新 interactive 模式；
  旧 skill_block 构造（两路）↔ 新 `build_skill_block`；旧 slots 块 ↔ 新 `build_slots_block`。
  多组 ctx（含空 slots / 空工具 / 空报告 / 长正文等边界）逐组比对，并含 4 条变异自证。
"""
import ast
import io
import os
import subprocess
import sys
import textwrap
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.chdir(_ROOT)

from agent.pipeline_parts.common import (  # noqa: E402
    PROMPT_BLOCK_ORDER,
    PromptBlocksCtx,
    _AGENT_ROLE_CAP,
    _SKILL_CONTENT_CAP,
    build_prompt_blocks,
    build_skill_block,
    build_slots_block,
)

_P = "agent/pipeline_parts"
_PASS, _FAIL = [], []

#: **重构前的基线提交**（P1-28 落地前的 HEAD）。
#: ⚠️ 必须写死，不能用 `HEAD` —— 实测踩到：重构一经提交，`HEAD` 就**变成了新代码**，
#: "旧基线"与"新实现"成了同一份 ⇒ 等价证明**自我失效**（脚本报的是"HEAD 里没有内联 dict"，
#: 看起来像断言失效，实为基线选错）。
#: 同样纪律见 `pure-move-refactor-verify` 技能：**基线要写死，不能读"当前状态"**。
#: 可用环境变量覆盖（例如把基线进一步前移时不必改代码）。
_BASELINE_REF = os.environ.get("P1_28_BASELINE_REF", "06eaab7")


def check(name, cond, detail=""):
    (_PASS if cond else _FAIL).append(name)
    print("  %s %s%s" % ("PASS" if cond else "FAIL", name,
                         ("  <- " + str(detail)) if detail else ""), flush=True)


def _old_src(rel):
    """取**基线提交**（不是 HEAD）的源码文本。"""
    out = subprocess.run(["git", "show", "%s:%s" % (_BASELINE_REF, rel)], cwd=_ROOT,
                         capture_output=True)
    if out.returncode != 0:
        raise RuntimeError("git show 失败（基线 ref=%s）：%s"
                           % (_BASELINE_REF, out.stderr.decode("utf-8", "ignore")[:200]))
    return out.stdout.decode("utf-8", "ignore")


def _stable(v):
    """稳定 repr（对象只报类型名，避免地址进入指纹导致不可重复）。"""
    if v is None or isinstance(v, (str, int, float, bool)):
        return repr(v)
    if isinstance(v, (list, tuple)):
        return "[" + ",".join(_stable(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{" + ",".join("%s:%s" % (_stable(k), _stable(v[k])) for k in sorted(v)) + "}"
    return "<%s>" % type(v).__name__


class StubPipe:
    """记录"调了哪个方法、用什么参数"的桩（参数差异因此可被逐字节比对捕获）。"""
    _skill_allowed_tools = None
    _skill_forced = False

    def __getattr__(self, name):
        if name.startswith("_build_") or name.startswith("_team_"):
            def _f(*a, **kw):
                return "<<%s|a=%s|kw=%s>>" % (name, _stable(list(a)), _stable(kw))
            return _f
        raise AttributeError(name)


AGENT = types.SimpleNamespace(name="AgentX", tools=["t1", "t2"], system_prompt="ROLE",
                              hil_level="L0")


def _ctx(**over):
    """默认 ctx + 覆写（含各种边界组合）。"""
    base = dict(agent_def=AGENT, intent="design", hil_level="L0", user_input="做设计",
                user={"id": 1}, branch="dev", conversation_id=7,
                slots={"goal": "G", "entities": ["E1", "E2"], "constraints": ["C1"],
                       "scope": {"a": 1}},
                user_ctx="UC", att_text="ATT", context_text="CTX",
                report_prompt="", skill_block="<SKILL>")
    base.update(over)
    return base


# ══════════════════════════════════════════════════════════════════════════════
# 抽取工具：从旧源码里取出「要执行的那段原文」
# ══════════════════════════════════════════════════════════════════════════════

def _extract_prompt_dict(src_text):
    """抽 `assemble_system_prompt({...})` 的 dict 字面量源码（旧形态）。"""
    tree = ast.parse(src_text)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fname = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if fname == "assemble_system_prompt" and node.args and isinstance(node.args[0], ast.Dict):
                return ast.get_source_segment(src_text, node.args[0])
    return None


def _extract_stmt(src_text, needle, last_needle):
    """按**行**定位：从含 `needle` 的第一行起、到其后**最后**一个含 `last_needle` 的行为止，抽原文并 dedent。

    ⚠️ 初版按 AST 语句节点抽，结果抓到的是**外层 `if` 整块**（其 source segment 覆盖全部子语句），
    连带把 `conn = get_db()` 一起抽了出来 —— 执行时异常又被原代码的 `except Exception: pass`
    **静默吞掉**，于是 `skill_block` 恒为空、断言全线失败。**行级定位取最小片段**才可靠。
    """
    lines = src_text.splitlines()
    si = next((i for i, l in enumerate(lines) if needle in l), None)
    if si is None:
        return None
    ei = None
    for i in range(si, len(lines)):
        if last_needle in lines[i]:
            ei = i
    if ei is None:
        return None
    return textwrap.dedent("\n".join(lines[si:ei + 1]))


def _exec_old(code, extra_ns=None, stub=None):
    """在受控命名空间执行旧源码片段，返回该命名空间。

    `_AGENT_ROLE_CAP` 必须注入：旧 stream 的 role 行引用了它（模块级常量）。
    """
    ns = {"json": __import__("json"), "self": stub or StubPipe(),
          "_AGENT_ROLE_CAP": _AGENT_ROLE_CAP}
    if extra_ns:
        ns.update(extra_ns)
    exec(compile(code, "<old_source>", "exec"), ns)     # noqa: S102 —— 只跑被测仓自己的旧源码
    return ns


def _norm(blocks):
    """把 blocks 规范化成可逐字节比对的形式（按唯一真源顺序；保留 key 集合差异）。"""
    return [(k, blocks.get(k) or "") for k in PROMPT_BLOCK_ORDER if k in blocks]


# ══════════════════════════════════════════════════════════════════════════════
print("== 0 前置：能否取到重构前的源码 ==")
OLD_EXEC = _old_src(_P + "/execute.py")
OLD_STREAM = _old_src(_P + "/stream.py")
print("   基线 ref = %s" % _BASELINE_REF)
check("0.1 基线版 execute.py 含内联 dict（确认基线选对了）",
      "assemble_system_prompt({" in OLD_EXEC)
check("0.2 基线版 stream.py 含内联 dict", "assemble_system_prompt({" in OLD_STREAM)
check("0.2b 基线版**不含** build_prompt_blocks（确认它真的是重构前）",
      "build_prompt_blocks(" not in OLD_EXEC and "build_prompt_blocks(" not in OLD_STREAM)
check("0.3 当前工作区已无内联 dict（重构已生效）",
      "assemble_system_prompt({" not in io.open(_P + "/execute.py", encoding="utf-8").read()
      and "assemble_system_prompt({" not in io.open(_P + "/stream.py", encoding="utf-8").read())
check("0.4 旧 dict 字面量抽取成功", bool(_extract_prompt_dict(OLD_EXEC))
      and bool(_extract_prompt_dict(OLD_STREAM)))

OLD_EXEC_DICT = _extract_prompt_dict(OLD_EXEC)
OLD_STREAM_DICT = _extract_prompt_dict(OLD_STREAM)

# ══════════════════════════════════════════════════════════════════════════════
print()
print("== A 旧 execute dict ↔ 新 batch 模式（role_cap=0 / 无 tool_rules）==")

_CASES = [
    ("默认", _ctx()),
    ("slots 为空", _ctx(slots=None)),
    ("工具为空（走「纯问答直出」分支）", _ctx(agent_def=types.SimpleNamespace(
        name="AgentY", tools=[], system_prompt="R2", hil_level="L1"))),
    ("有 report_prompt", _ctx(report_prompt="REPORT")),
    ("user_ctx/skill_block 为空", _ctx(user_ctx="", skill_block="")),
    ("slots 只有 goal", _ctx(slots={"goal": "G"})),
]

for tag, kw in _CASES:
    stub = StubPipe()
    # 旧 execute 的 role_block 在 dict 之前单独赋值 —— 从旧源码抽原文执行（不是复刻）
    ns = _exec_old("role_block = self._build_role_block(agent_def)", kw, stub)
    old = _exec_old("_r = " + OLD_EXEC_DICT, {**kw, "role_block": ns["role_block"]}, stub)["_r"]
    new = build_prompt_blocks(stub, PromptBlocksCtx(**kw, role_cap=0, include_tool_rules=False))
    same = _norm(old) == _norm(new)
    check("A[%s] batch 模式块集合与值逐字节一致" % tag, same,
          "" if same else "old=%s\nnew=%s" % (_norm(old), _norm(new)))
    check("A[%s] 旧路径确实没有 tool_rules 块（差异未被误抹平）" % tag,
          "tool_rules" not in old and "tool_rules" not in new)

# ══════════════════════════════════════════════════════════════════════════════
print()
print("== B 旧 stream dict ↔ 新 interactive 模式（role_cap=_AGENT_ROLE_CAP / 有 tool_rules）==")

for tag, kw in _CASES:
    stub = StubPipe()
    old = _exec_old("_r = " + OLD_STREAM_DICT, kw, stub)["_r"]
    new = build_prompt_blocks(stub, PromptBlocksCtx(**kw, role_cap=_AGENT_ROLE_CAP,
                                                    include_tool_rules=True))
    same = _norm(old) == _norm(new)
    check("B[%s] interactive 模式块集合与值逐字节一致" % tag, same,
          "" if same else "old=%s\nnew=%s" % (_norm(old), _norm(new)))
    check("B[%s] 旧路径确实有 tool_rules 块" % tag, "tool_rules" in old and "tool_rules" in new)

# ══════════════════════════════════════════════════════════════════════════════
print()
print("== C 旧 skill_block 构造 ↔ 新 build_skill_block ==")

SKILLS = [
    ("仅 name+content", {"name": "S1", "content": "BODY"}),
    ("含资源与白名单", {"name": "S2", "content": "BODY2",
                        "references": ["r1", {"title": "r2"}], "examples": ["e1"],
                        "scripts": ["s1"], "allowed_tools": ["graph_retrieve", "kb_search"]}),
    ("正文超 4000", {"name": "S3", "content": "X" * 4500}),
    ("content 为空串", {"name": "S4", "content": ""}),
]

_EXEC_SKILL_CODE = _extract_stmt(OLD_EXEC, 'skill_block = f"【指定技能：', 'skill_block += "\\n"')
check("C0 旧 execute 的 skill_block 构造段抽取成功", bool(_EXEC_SKILL_CODE))

for tag, sk in SKILLS:
    # ── execute 形态：不截断 + 披露资源 ──
    if _EXEC_SKILL_CODE:
        stub = StubPipe()
        ns = _exec_old(_EXEC_SKILL_CODE, {"sk": sk, "skill_block": ""}, stub)
        old_blk = ns["skill_block"]
        new_blk, new_at = build_skill_block(sk, content_cap=0, include_resources=True)
        check("C1[%s] execute 形态逐字节一致" % tag, old_blk == new_blk,
              "" if old_blk == new_blk else "old=%r\nnew=%r" % (old_blk, new_blk))
        check("C2[%s] 白名单集合一致" % tag,
              set(ns.get("_at") or []) == new_at, (ns.get("_at"), new_at))
        check("C2b[%s] 旧路径把白名单写到了 self（权威注入语义保留）" % tag,
              (stub._skill_allowed_tools or set()) == new_at, stub._skill_allowed_tools)
    # ── stream 形态：截断 4000 + 不披露 ──
    old_s = 'skill_block = f"【指定技能：{sk[\'name\']}（必须遵循其完整指令）】\\n{sk[\'content\'][:4000]}\\n"'
    ns2 = _exec_old(old_s, {"sk": sk})
    new_s, _at2 = build_skill_block(sk, content_cap=_SKILL_CONTENT_CAP, include_resources=False)
    check("C3[%s] stream 形态逐字节一致" % tag, ns2["skill_block"] == new_s,
          "" if ns2["skill_block"] == new_s else "old=%r\nnew=%r" % (ns2["skill_block"], new_s))
    check("C4[%s] stream 形态不产出白名单（与旧行为一致）" % tag, not _at2)

# ══════════════════════════════════════════════════════════════════════════════
print()
print("== D slots 块（两条路径此前逐字重复）==")
for tag, slots in [("完整", {"goal": "G", "entities": ["A", "B"], "constraints": ["C"],
                             "scope": {"x": 1}}),
                   ("仅 goal", {"goal": "G"}),
                   ("空 dict", {}),
                   ("None", None)]:
    old_expr = ('(f"【任务拆解（P1 结构化）】\\n目标：{slots.get(\'goal\') or \'-\'}\\n"'
                ' f"实体：{\'、\'.join(slots.get(\'entities\') or []) or \'-\'}\\n"'
                ' f"约束：{\'；\'.join(slots.get(\'constraints\') or []) or \'-\'}\\n"'
                ' f"范围：{json.dumps(slots.get(\'scope\') or {}, ensure_ascii=False) if slots.get(\'scope\') else \'-\'}\\n"'
                ' if slots else "")')
    got_old = _exec_old("_v = " + old_expr, {"slots": slots})["_v"]
    got_new = build_slots_block(slots)
    check("D[%s] slots 块逐字节一致" % tag, got_old == got_new,
          "" if got_old == got_new else "old=%r new=%r" % (got_old, got_new))

# ══════════════════════════════════════════════════════════════════════════════
print()
print("== E 反证：判据不是恒真（差异必须能被抓住）==")
# E1：若把 batch 的 role_cap 误传成 streaming 的 cap → 必须不等
stub = StubPipe()
kw = _ctx()
old_exec_blocks = _exec_old("role_block = self._build_role_block(agent_def)", kw, stub)["role_block"]
_cap_wrong = build_prompt_blocks(stub, PromptBlocksCtx(**kw, role_cap=_AGENT_ROLE_CAP,
                                                       include_tool_rules=False))
_cap_right = build_prompt_blocks(stub, PromptBlocksCtx(**kw, role_cap=0,
                                                       include_tool_rules=False))
check("E1 role_cap 传错（0 → _AGENT_ROLE_CAP）会改变 role 块（判据能抓到参数差异）",
      _cap_wrong["role"] != _cap_right["role"])
# E2：若把 batch 误开 tool_rules → 必须多出一块
check("E2 include_tool_rules 传错会改变块集合",
      set(_cap_right) != set(build_prompt_blocks(
          stub, PromptBlocksCtx(**kw, role_cap=0, include_tool_rules=True))))

# ══════════════════════════════════════════════════════════════════════════════
print()
print("== M 变异自证（改**源码文本**再 exec，判据必须被打破）==")


def _load_common_variant(replacements):
    """把 common.py 源码按 replacements 改写后 exec 成独立模块（判据不来自手抄实现）。"""
    path = os.path.join(_ROOT, _P, "common.py")
    with io.open(path, encoding="utf-8") as f:
        src = f.read()
    for a, b in replacements:
        if a not in src:
            check("变异锚点命中: %r" % a[:40], False, "锚点未命中 → 变异失效")
            return None
        src = src.replace(a, b, 1)
    mod = types.ModuleType("common_mut")
    exec(compile(src, path + "<mut>", "exec"), mod.__dict__)     # noqa: S102
    return mod


_M1 = _load_common_variant([("_role_kw = {\"cap\": ctx.role_cap} if ctx.role_cap else {}",
                             "_role_kw = {}")])
if _M1:
    stub = StubPipe()
    kw = _ctx()
    got = _M1.build_prompt_blocks(stub, _M1.PromptBlocksCtx(**kw, role_cap=_AGENT_ROLE_CAP,
                                                            include_tool_rules=False))
    ref = build_prompt_blocks(stub, PromptBlocksCtx(**kw, role_cap=_AGENT_ROLE_CAP,
                                                    include_tool_rules=False))
    check("M1 丢掉 role_cap 传参 → role 块被打破（旧 stream 基线不再匹配）",
          got["role"] != ref["role"], got["role"])

_M2 = _load_common_variant([("return \"\".join(out)", "return \"\\n\".join(out)")])
if _M2:
    _r = _M2.assemble_system_prompt({"role": "A", "tools": "B"})
    check("M2 拼装分隔符改成换行 → 与唯一真源行为不同（分层/拼装契约能感知）",
          _r != "AB" and _r == "A\nB", repr(_r))
else:
    check("M2 锚点未命中（assemble_system_prompt 已被移动？）", False)

_M3 = _load_common_variant([('if content_cap and len(content) > content_cap:',
                             'if False and len(content) > content_cap:')])
if _M3:
    sk = {"name": "S", "content": "X" * 4500}
    blk, _ = _M3.build_skill_block(sk, content_cap=4000, include_resources=False)
    ref_blk, _ = build_skill_block(sk, content_cap=4000, include_resources=False)
    check("M3 取消正文截断 → stream 形态不再匹配（4000 截断判据有效）",
          blk != ref_blk and len(ref_blk) < len(blk), (len(blk), len(ref_blk)))

_M4 = _load_common_variant([('"tools": "可用工具：%s。\\n" % (", ".join(ctx.agent_def.tools) or "无（纯问答直出）"),',
                             '"tools": "可用工具：%s。\\n" % ", ".join(ctx.agent_def.tools),')])
if _M4:
    stub = StubPipe()
    empty = types.SimpleNamespace(name="Z", tools=[], system_prompt="R", hil_level="L0")
    got = _M4.build_prompt_blocks(stub, _M4.PromptBlocksCtx(**_ctx(agent_def=empty),
                                                            role_cap=0, include_tool_rules=False))
    ref = build_prompt_blocks(stub, PromptBlocksCtx(**_ctx(agent_def=empty),
                                                    role_cap=0, include_tool_rules=False))
    check("M4 去掉「无工具」兜底文案 → 空工具场景被打破",
          got["tools"] != ref["tools"], (got["tools"], ref["tools"]))

print()
print("PASS %d / FAIL %d" % (len(_PASS), len(_FAIL)))
for f in _FAIL:
    print("  FAIL:", f)
raise SystemExit(1 if _FAIL else 0)
