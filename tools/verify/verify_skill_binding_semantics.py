# -*- coding: utf-8 -*-
"""verify_skill_binding_semantics — 门禁：**绑定到底是不是白名单**必须有明确定义。

## 背景（用户提问："只绑了一个 skill，为啥会触发其他的？白名单是怎么回事"）

实测（conv 9633，`智源对接agent` 仅绑定「文件操作」）`skill_hits` =
`['com.zhiyuan.legacy.skill.file-ops', 'sysml_view_generation_activity', '文件操作']`
—— 三个技能里**只有一个是绑定的**。原因不是 bug，而是 `agent_tools` 的绑定
在本系统里语义是「优先候选」，**不是准入白名单**：

    candidates = bound + [全库 status='published' AND enabled=1]

行业里两种语义都存在（调研结论）：
  · Anthropic Agent Skills：装进环境即可用（= 本系统现状 bound_first）
  · OpenAI Agents SDK / Bedrock Agents：必须显式装配（= bound_only）

两种都合理，但**必须由产品显式选定并说清**，不能含糊。本门禁锁：
① 默认档 = bound_first（对齐 Anthropic），且文档明说"绑定≠白名单"；
② 可切到 bound_only（对齐显式装配），切换**只改配置不改代码**；
③ 技能日志记的是"注入"不是"执行"（不许拿最终答案冒充技能产出）；
④ 工具侧绑定的 MCP 工具**不得被语义裁剪**（显式授权 > JIT 预筛）。

含**变异自证**：删掉 bound_only 分支、或把 result 改回最终答案，门禁必须变红。
"""
import os
import re
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

FAILS = []


def ok(name, cond, detail=""):
    print("  [%s] %s%s" % ("OK  " if cond else "FAIL", name, ("  ← " + detail) if detail else ""))
    if not cond:
        FAILS.append(name)


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def strip_comments(src):
    """剥掉注释与 docstring，只留可执行代码 —— 否则断言会被自己的说明文字命中。"""
    out = []
    in_doc = False
    for line in src.split("\n"):
        s = line.strip()
        if s.count('"""') == 1:
            in_doc = not in_doc
            continue
        if in_doc or s.startswith("#"):
            continue
        out.append(re.sub(r"\s+#\s.*$", "", line))
    return "\n".join(out)


def main():
    sk_src = read("agent/pipeline_parts/skills.py")
    sk_code = strip_comments(sk_src)
    cfg_src = read("core/config.py")
    mem_code = strip_comments(read("agent/pipeline_parts/memory.py"))
    tool_code = strip_comments(read("agent/pipeline_parts/tools.py"))

    print("=== ① 绑定语义必须是显式两档（不是含糊的'优先候选'）===")
    ok("存在 bound_only 分支", '"bound_only"' in sk_code or "'bound_only'" in sk_code)
    ok("存在 bound_first 默认", "bound_first" in sk_code)
    ok("候选集按 scope 分叉（bound_only 只取 bound）",
       re.search(r"bound_only[\s\S]{0,200}?candidates\s*=\s*list\(bound\)", sk_code) is not None
       or re.search(r"candidates\s*=\s*list\(bound\)\s*\n\s*else:", sk_code) is not None)
    ok("默认档取 bound_first（未配置时）",
       re.search(r'get\(\s*["\']agent["\']\s*,\s*["\']skill_pool_scope["\']\s*,\s*["\']bound_first["\']',
                 sk_code) is not None)
    ok("config 有 skill_pool_scope 的 schema（可被 UI 覆盖）",
       "skill_pool_scope" in cfg_src)
    ok("schema 说明里点明两档对齐的产品",
       ("Anthropic" in cfg_src and "Agents SDK" in cfg_src))
    ok("源码明确写清'绑定≠白名单'",
       ("不是准入" in sk_src) or ("绑定只是" in sk_src) or ("绑定=优先候选" in sk_src))

    print("\n=== ② 技能日志记'注入'不记'执行'（不许拿最终答案冒充产出）===")
    ok("已去除旧写法（result=最终答案）",
       not re.search(r"_summary\s*=\s*str\(output_content or \"\"\)\[:2000\]", mem_code),
       "旧表达式仍在" if re.search(r"_summary\s*=\s*str\(output_content", mem_code) else "")
    ok("新写法明确标注'注入/未独立执行'",
       "技能注入" in mem_code and ("未独立执行" in mem_code))
    ok("docstring 解释了为什么不能填最终答案", "归因彻底失真" in read("agent/pipeline_parts/memory.py"))

    print("\n=== ③ 工具侧：绑定的 MCP 工具不得被 JIT 语义裁剪 ===")
    ok("存在绑定 MCP 豁免集合", "_bound_mcp_names" in tool_code)
    ok("豁免集合确实并入保留集（core_keep）",
       re.search(r"core_keep[\s\S]{0,400}_bound_mcp_names", tool_code) is not None
       or re.search(r"_bound_mcp_names[\s\S]{0,400}core_keep", tool_code) is not None)

    print("\n=== ④ 功能性双档验证（不是只看源码有没有那两个字）===")
    from agent.pipeline_parts.skills import SkillMixin
    from core import config as _cfg

    BOUND = [{"name": "只绑这个", "content": "BODY-BOUND", "triggers": ["读文件"],
              "desc": "d", "frontmatter": "", "allowed_tools": []}]
    POOL = BOUND + [{"name": "全池技能A", "content": "BODY-POOL", "triggers": ["读文件"],
                     "desc": "d", "frontmatter": "", "allowed_tools": []},
                    {"name": "全池技能B", "content": "BODY-POOL2", "triggers": ["读文件"],
                     "desc": "d", "frontmatter": "", "allowed_tools": []}]

    class _Reg:
        def get_bound_skills(self, intent):
            return list(BOUND)

    class _Stub(SkillMixin):
        def __init__(self):
            self.registry = _Reg()
            self._last_skill_hits = []
            self._skill_allowed_tools = None
            self._skill_forced = False
            self._tool_conv_ctx = 0

        def _global_skill_pool(self, user=None):
            return list(POOL)

        def _semantic_skill_match(self, ui, rest, threshold=0.15):
            return [s["name"] for s in rest]

        def _llm_pick_skills(self, ui, cands):
            return [s["name"] for s in cands]

        def _semantic_candidates(self, ui, rest, k=5):
            return [s["name"] for s in rest]

    _orig = _cfg.get

    def _run(scope):
        def _fake(section, key=None, default=None):
            if section == "agent" and key == "skill_pool_scope":
                return scope
            return _orig(section, key, default)
        _cfg.get = _fake
        try:
            st = _Stub()
            st._build_skill_prompt("智源对接agent", "帮我读文件")
            return list(st._last_skill_hits)
        finally:
            _cfg.get = _orig

    hits_first = _run("bound_first")
    hits_only = _run("bound_only")
    print("   bound_first 命中：%s" % hits_first)
    print("   bound_only  命中：%s" % hits_only)
    ok("bound_first ⇒ 未绑定技能也会被命中（= 现状，对齐 Anthropic）",
       any(h != "只绑这个" for h in hits_first), str(hits_first))
    ok("bound_only ⇒ 只命中绑定的（= 显式装配，对齐 Agents SDK）",
       hits_only == ["只绑这个"], str(hits_only))

    print("\n=== ⑤ 变异自证：把 bound_only 分支废掉，功能性断言必须变红 ===")
    _src_p = os.path.join(ROOT, "agent", "pipeline_parts", "skills.py")
    _s = open(_src_p, encoding="utf-8").read()
    _mut = _s.replace('if _scope == "bound_only":\n            candidates = list(bound)',
                      'if False:\n            candidates = list(bound)')
    ok("变异点命中", _mut != _s)
    _ns = {}
    import importlib
    # 相对导入（from . import ...）要求文件在包内 ⇒ 临时落到同包目录，用完即删。
    _tmp_py = os.path.join(os.path.dirname(_src_p), "_mut_skills_tmp.py")
    try:
        with open(_tmp_py, "w", encoding="utf-8", newline="\n") as f:
            f.write(_mut)
        _mod = importlib.import_module("agent.pipeline_parts._mut_skills_tmp")
    finally:
        try:
            if os.path.exists(_tmp_py):
                os.remove(_tmp_py)
        except Exception:
            pass
    _MutStub = type("MutStub", (_mod.SkillMixin,), dict(_Stub.__dict__))
    def _run_mut(scope):
        def _fake(section, key=None, default=None):
            if section == "agent" and key == "skill_pool_scope":
                return scope
            return _orig(section, key, default)
        _cfg.get = _fake
        try:
            st = _MutStub()
            st._build_skill_prompt("智源对接agent", "帮我读文件")
            return list(st._last_skill_hits)
        finally:
            _cfg.get = _orig
    mut_hits = _run_mut("bound_only")
    ok("变异版 bound_only 不再受限（说明该分支是真起作用，不是摆设）",
       mut_hits != ["只绑这个"], str(mut_hits))

    mut2 = mem_code.replace("（技能注入：", "（旧：")
    ok("变异③（把 result 改回非事实描述）能被识别", "技能注入" not in mut2)

    print("\n=== ⑤ 真实数据盘点（只报告，不判失败）===")
    try:
        c = sqlite3.connect("file:%s?mode=ro" % os.path.join(ROOT, "mbse.db"), uri=True)
        c.row_factory = sqlite3.Row
        r = c.execute("SELECT COUNT(*) n FROM skills WHERE status='published' AND enabled=1").fetchone()
        print("   全池（published+enabled）技能数 = %s" % r["n"])
        r2 = c.execute("SELECT COUNT(*) n FROM agent_tools WHERE tool_type='skill'").fetchone()
        print("   绑定到各 Agent 的技能条目数   = %s" % r2["n"])
        row = c.execute("SELECT id, card_data FROM messages WHERE conversation_id=9633 "
                        "AND role='assistant' ORDER BY id DESC LIMIT 1").fetchone()
        if row and row["card_data"]:
            import json
            cd = json.loads(row["card_data"])
            hits = cd.get("skill_hits") or []
            bound = {x["tool_name"] for x in c.execute(
                "SELECT tool_name FROM agent_tools WHERE agent_id=735 AND tool_type='skill'")}
            unbound = [h for h in hits if h not in bound]
            print("   conv 9633 命中 %d 个，其中未绑定 %d 个：%s" % (len(hits), len(unbound), unbound))
            print("   ⇒ 绑定≠白名单：未绑定技能同样会被注入（bound_first 档的预期行为）")
        c.close()
    except Exception as e:
        print("   （盘点跳过：%s）" % e)

    print("\n" + "=" * 70)
    if FAILS:
        print("❌ FAIL %d 项：%s" % (len(FAILS), "; ".join(FAILS)))
        sys.exit(1)
    print("✅ 通过 —— 绑定语义显式两档 / 技能日志记注入 / 绑定工具免裁剪（含变异自证）")


if __name__ == "__main__":
    main()
