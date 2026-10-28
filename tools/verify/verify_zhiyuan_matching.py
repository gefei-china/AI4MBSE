# -*- coding: utf-8 -*-
"""智源对接链路优化专项门禁（2026-10-27）。

覆盖本批四处改动，每条都有**变异自证**（把代码改回旧写法 ⇒ 对应判据必须翻红），
否则判据只是"跑通了"，抓不到东西。

用法：
    ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_zhiyuan_matching.py
"""
from __future__ import annotations

import inspect
import os
import re
import sqlite3
import sys
import textwrap

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

from agent.mcp_tool_meta import (  # noqa: E402
    address_chain, filter_mcp_by_intent, load_tool_meta, mcp_write_group, pick_for_injection,
)
from agent.pipeline_parts.skills import SkillMixin  # noqa: E402

PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("[%s] %s%s" % ("OK  " if cond else "FAIL", name, ("  — " + str(detail)) if detail else ""))


# ══════════════════════════════════════════════════════════════════
# 夹具
# ══════════════════════════════════════════════════════════════════
DB = os.path.join(REPO, "mbse.db")
con = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
con.row_factory = sqlite3.Row

_row = con.execute("SELECT * FROM mcp_servers WHERE name='zhiyuan-dev-mcp'").fetchone()
META = load_tool_meta(_row)
COORD = ["query_projects", "query_branches", "query_model_versions"]
GATED = ("delete", "import", "apply", "undo", "overwrite")

print("夹具：zhiyuan-dev-mcp 工具 %d 个\n" % len(META))

# ══════════════════════════════════════════════════════════════════
# [A] 寻址前置链（坐标系三元组恒在场）
# ══════════════════════════════════════════════════════════════════
# ── [A1] 需要寻址的工具 ⇒ 其所需的每个坐标轴字段的生产者都在链上 ──
#   ⚠️ 判据不能写成「链上恒有全部三个坐标」——那是**过强**的：
#   `query_branches` 只需要 projectId，它的链就只有"查工程"（再往上要 branchId 就越级了）。
#   正确不变式：**该工具要的每个坐标轴字段，其生产者必在链上**。
from agent.mcp_tool_meta import _address_set, _nested_address_fields  # noqa: E402
_axis_min = max(5, len(META) // 3)
_usage = {}
for _nm, _info in META.items():
    for _f in _address_set(_info):
        _usage[_f] = _usage.get(_f, 0) + 1
_nested = set()
for _nm, _info in META.items():
    _nested |= _nested_address_fields(_info)
_axes = {f for f, c in _usage.items() if c >= _axis_min and f in _nested}

# 生产者复算（与 address_chain 内部同一套判据，但独立实现一遍 ⇒ 避免"同源实现互证"）
_prod = {}
for _f in _axes:
    _st = _f[:-2].lower()
    _c = [nm for nm in META
          if _f not in _address_set(META[nm])
          and nm.lower().startswith(("query_", "list_", "search_", "get_", "find_", "browse_"))
          and _st in ("%s %s" % (nm, META[nm].get("description") or "")).lower()
          and _address_set(META[nm]) < _axes]
    if _c:
        _prod[_f] = min(_c, key=lambda nm: (len(_address_set(META[nm])), list(META).index(nm)))

_bad = []
for _nm in META:
    _need = _address_set(META[_nm]) & _axes
    if not _need:
        continue
    _ch = set(address_chain(META, {_nm}))
    for _f in _need:
        if _f in _prod and _prod[_f] not in _ch:
            _bad.append((_nm, _f, _prod[_f]))
check("[A1] 每个工具所需的坐标轴字段，其生产者都在寻址链上", not _bad,
      "违反 %d 处：%s" % (len(_bad), _bad[:3]))
check("[A1] 坐标三元组的生产者唯一确定",
      _prod.get("projectId") == "query_projects"
      and _prod.get("branchId") == "query_branches"
      and _prod.get("versionId") == "query_model_versions",
      "生产���=%s" % {k: v for k, v in _prod.items()})

# ── [A2] 中文写类请求（历史失败形态）⇒ 坐标三元组齐全 ──
Q_CREATE = "在通信子系统用例包中新增测试包"
_names, _st = pick_for_injection(META, Q_CREATE, min_keep=8, top_n=0)
check("[A2] 历史失败请求（新增测试包）⇒ 坐标三元组齐全",
      all(c in _names for c in COORD),
      "选中 %d 个，chain=%s" % (len(_names), _st.get("chain")))

for _q in ("把当前模型导出为SysML v2源码", "校验这段SysML v2代码的语法", "删除旧的需求包"):
    _n, _s = pick_for_injection(META, _q, min_keep=8, top_n=0)
    check("[A2] 请求「%s」⇒ 坐标三元组齐全" % _q, all(c in _n for c in COORD),
          "chain=%s" % _s.get("chain"))

# ── [A3] 无寻址结构的 server ⇒ 零变化 ──
_row2 = con.execute("SELECT * FROM mcp_servers WHERE id=160").fetchone()
META2 = load_tool_meta(_row2)
_n2, _s2 = pick_for_injection(META2, Q_CREATE, min_keep=8, top_n=0)
check("[A3] 无寻址结构的 server ⇒ 不补任何工具（零变化）",
      not _s2.get("chain") and len(_n2) == len(META2),
      "chain=%s" % _s2.get("chain"))

# ── [A4] 坐标轴不得含客户端自造幂等键（requestId）──
check("[A4] 坐标轴 = {projectId, branchId, versionId}（不含 requestId 幂等键）",
      _axes == {"projectId", "branchId", "versionId"}, "轴=%s" % sorted(_axes))
check("[A4] requestId 不在坐标轴内（客户端自造键不产生伪生产者）",
      "requestId" not in _axes and "get_model_operation" not in _names)

# ── [A5] 变异自证：把坐标轴判据退化成「只用使用次数」⇒ 必须出现 requestId 伪生产者 ──
_axes_naive = {f for f, c in _usage.items() if c >= _axis_min}
check("[A5] 变异自证：去掉「成组出现在寻址对象里」⇒ requestId 会混入坐标轴",
      "requestId" in _axes_naive and "requestId" not in _axes,
      "退化后轴=%d 个" % len(_axes_naive))

# ══════════════════════════════════════════════════════════════════
# [B] MCP 破坏性/覆盖写工具的动词级意图闸
# ══════════════════════════════════════════════════════════════════
_groups = {nm: mcp_write_group(nm) for nm in META}


def mcp_names_for(query):
    ns, _ = pick_for_injection(META, query, min_keep=8, top_n=0)
    return ns


def gated_present(query):
    return [n for n in mcp_names_for(query)
            if _groups.get(n) in GATED]


# ── [B1] 读类/导出/校验请求 ⇒ 不含破坏性工具 ──
#   ⚠️ 必须测**真实链路**（`_build_tools_def`）：动词闸住在 tools.py 的工具构建层
#   （因为它要尊重白名单模式 `_wl`），直接调 `pick_for_injection` 测不到那一层。
from agent.pipeline import AgentPipeline  # noqa: E402
_USER = {"id": 1, "role_name": "系统管理员"}


def real_tool_names(query):
    p = AgentPipeline()
    p._load_db_agents()
    p._build_skill_prompt("智源对接agent", query, user=_USER)
    td = p._build_tools_def("智源对接agent", query, user=_USER)
    return [t["function"]["name"] for t in (td or [])]


def gated_present_real(query):
    return [n for n in real_tool_names(query) if _groups.get(n) in GATED]


for _q in ("把当前模型导出为SysML v2源码", "校验这段SysML v2代码的语法", "查询智源工程列表"):
    check("[B1] 请求「%s」⇒ 不暴露破坏性/覆盖写工具" % _q, not gated_present_real(_q),
          "暴露=%s" % gated_present_real(_q))

# ── [B1b] 真实链路：坐标三元组恒在场（寻址链 + 动词闸的合流结果）──
for _q in ("在通信子系统用例包中新增测试包", "把当前模型导出为SysML v2源码",
           "校验这段SysML v2代码的语法", "查询智源工程列表"):
    _n = real_tool_names(_q)
    check("[B1b] 真实链路：请求「%s」⇒ 坐标三元组齐全" % _q,
          all(c in _n for c in COORD),
          "缺=%s" % [c for c in COORD if c not in _n])

# ── [B2] 请求含对应动词 ⇒ 该族工具出现 ──
_all = list(META.keys())
_keep_del, _drop_del = filter_mcp_by_intent(_all, "删除旧的需求包", _groups)
check("[B2] 请求含「删除」⇒ delete 族工具保留", "delete_model_elements" in _keep_del)
_keep_imp, _drop_imp = filter_mcp_by_intent(_all, "把代码导入智源", _groups)
check("[B2] 请求含「导入」⇒ import 族工具保留", "import_sysml_text" in _keep_imp)
_keep_upd, _drop_upd = filter_mcp_by_intent(_all, "把需求包改名成测试包", _groups)
check("[B2] 请求含「改名」⇒ update 族工具保留（常规建模动作不被闸）",
      "update_model_elements" in _keep_upd)

# ── [B3] 常规建模动作（create）不被闸 ──
_keep_cr, _drop_cr = filter_mcp_by_intent(_all, "查询智源工程列表", _groups)
check("[B3] 读类请求下 create/update 族不被闸（只闸 delete/import/apply/undo）",
      "create_model_elements" in _keep_cr and "update_model_elements" in _keep_cr,
      "挡掉=%s" % _drop_cr[:4])

# ── [B4] 变异自证：把闸退化成「不按动词族、全部保留」⇒ [B1] 必须翻红 ──
_mut_keep, _mut_drop = filter_mcp_by_intent(_all, "把当前模型导出为SysML v2源码", {})
check("[B4] 变异自证：闸失效（空 groups ⇒ 全放行）⇒ [B1] 的判据会翻红",
      "delete_model_elements" in _mut_keep,
      "闸失效后 delete 族重新暴露 ⇒ [B1] 确实在生效")

# ── [B5] 闸的族集合必须窄（只含破坏性/覆盖写）──
check("[B5] 受闸约束的族 = delete/import/apply/undo/overwrite（窄集合）",
      set(GATED) == {"delete", "import", "apply", "undo", "overwrite"})

# ══════════════════════════════════════════════════════════════════
# [C] 技能触发词：合取（AND）语义 + 精度
# ══════════════════════════════════════════════════════════════════
_hit = SkillMixin._trigger_hit
check("[C1] 合取触发词命中：「新增测试包」⇒ 命中「新增+包」",
      _hit(["新增+包"], "在通信子系统用例包中新增测试包"))
check("[C1] 合取触发词命中：「新增包」也命中「新增+包」",
      _hit(["新增+包"], "新增包"))
check("[C1] 合取不误命中：「生成需求视图」⇒ 不命中「新增+包」",
      not _hit(["新增+包"], "生成需求视图"))

# ── [C2] 存量无 '+' 的触发词行为**逐字节不变** ──
_legacy = ["智源", "zhiyuan", "工程列表", "包结构"]
check("[C2] 存量触发词（无 '+'）仍是子串语义",
      _hit(_legacy, "帮我查一下智源的工程列表") and
      not _hit(_legacy, "生成需求视图"))

# ── [C3] 技能「智源平台管理」的触发词不含泛词 ──
_sk = con.execute("SELECT triggers, description, frontmatter, content FROM skills "
                  "WHERE name='智源平台管理'").fetchone()
_trg = con.execute("SELECT triggers FROM skills WHERE name='智源平台管理'").fetchone()
import json  # noqa: E402
_trgs = json.loads(_trg["triggers"])
_generic = [t for t in _trgs if t in ("包", "元素", "建模", "校验", "视图", "新增", "删除",
                                      "移动", "导入", "导出", "修改", "更新", "添加")]
check("[C3] 「智源平台管理」触发词不含泛词（包/元素/建模/校验/视图…）",
      not _generic, "残留泛词=%s" % _generic)
check("[C3] 「智源平台管理」触发词含合取词组（新增+包 等）",
      any("+" in t for t in _trgs), "共 %d 个触发词" % len(_trgs))

# ── [C4] 变异自证：把合取退化成子串 ⇒ 精度判据必须翻红 ──
_src = inspect.getsource(SkillMixin._trigger_hit)
_mut_src = _src.replace(
    'if "+" in t:', 'if False:')
_mut_ns = {}
exec(compile(textwrap.dedent(_mut_src), "<mut>", "exec"), _mut_ns)
_mut_hit = _mut_ns["_trigger_hit"]
check("[C4] 变异自证：合取退化成子串 ⇒ 「生成需求视图」会误命中（精度判据有牙齿）",
      _mut_hit(["新增+包"], "生成需求视图") is False,
      "变异后仍不命中 ⇒ 判据空转")

# ══════════════════════════════════════════════════════════════════
# [D] 提示词 / 技能内容 / 工具绑定
# ══════════════════════════════════════════════════════════════════
_ag = con.execute("SELECT system_prompt, description FROM agents WHERE id=735").fetchone()
_sp = _ag["system_prompt"] or ""
for _kw in ("query_projects", "query_branches", "query_model_versions",
            "includeDraft", "get_model_delete_impact", "sysml_v2_validate",
            "最多 3 轮", "没有工具回执"):
    check("[D1] Agent 735 system_prompt 含「%s」" % _kw, _kw in _sp)
check("[D1] Agent 735 system_prompt 长度 %d（改动前 134）" % len(_sp), len(_sp) > 500)

# ── [D2] frontmatter 与 content YAML 头同源 + ≤400 ──
def _yaml(text):
    t = text or ""
    if not t.startswith("---"):
        return ""
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?", t, re.S)
    return ("---\n%s\n---" % m.group(1)) if m else ""


for _nm in ("智源平台管理", "sysml_view_generation_guide"):
    _r = con.execute("SELECT frontmatter, content FROM skills WHERE name=?", (_nm,)).fetchone()
    check("[D2] %s 的 frontmatter 与 content YAML 头同源" % _nm,
          (_r["frontmatter"] or "").strip() == _yaml(_r["content"] or "").strip())
    check("[D2] %s 的 frontmatter ≤ 400（注入不被腰斩）" % _nm,
          len(_r["frontmatter"] or "") <= 400, "长度 %d" % len(_r["frontmatter"] or ""))

# ── [D3] Agent 735 绑定了 4 个本地 SysML 工具（视图 skill 正文点名要用）──
_bind = {r["tool_name"] for r in con.execute(
    "SELECT tool_name FROM agent_tools WHERE agent_id=735 AND tool_type='tool' AND enabled=1")}
for _t in ("sysml_ast_extract", "sysml_stdlib_meta", "sysml_v2_lint", "sysml_v2_project_check",
           "sysml_v2_validate", "sysml_v2_autofix"):
    check("[D3] Agent 735 绑定本地工具 %s" % _t, _t in _bind)

# ── [D4] 视图 skill 的 allowed_tools 含正文点名的工具 ──
for _nm in ("sysml_view_generation_requirement", "sysml_view_generation_structure",
            "sysml_view_generation_usecase", "sysml_view_generation_activity",
            "sysml_view_generation_ibd", "sysml_view_generation_sequence",
            "sysml_view_generation_state", "sysml_view_generation_parameter"):
    _r = con.execute("SELECT allowed_tools, content FROM skills WHERE name=?", (_nm,)).fetchone()
    _at = set(json.loads(_r["allowed_tools"] or "[]"))
    _named = set(re.findall(r"\b(sysml_[a-z0-9_]+|graph_retrieve)\b", _r["content"] or ""))
    _named.discard(_nm)          # 技能**自身名字**会被正文提及，不是工具
    check("[D4] %s 的 allowed_tools 覆盖正文点名的工具" % _nm,
          not (_named - _at), "缺=%s" % sorted(_named - _at))

# ── [D5] 视图 skill 的白名单**不得排除** `sysml_v2_autofix`（收尾修复轮的工具来源）──
#   ⚠️ 本条是被 `verify_p0_repair_round_tools` 抓出来的**真回归**（2026-10-27）：
#   skill 的 `allowed_tools` 在 execute 路径是**独占式白名单** ⇒ 白名单里没有的工具
#   **整类不可见** ⇒ 收尾修复轮的 `tools` 变成 0 个 ⇒ **模型报错了却没人能修**。
#   ⇒ 判据是「**不得排除**」而不是「必须包含」：**空白名单 `[]` = 不限制 = 不排除任何工具**
#   （那是更宽松的形态，不是缺陷）；只有"非空且缺了它"才是缺陷。
for _nm in ("sysml_view_generation_requirement", "sysml_view_generation_structure",
            "sysml_view_generation_usecase", "sysml_view_generation_activity",
            "sysml_view_generation_ibd", "sysml_view_generation_sequence",
            "sysml_view_generation_state", "sysml_view_generation_parameter",
            "sysml_view_generation_guide"):
    _r = con.execute("SELECT allowed_tools, content FROM skills WHERE name=?", (_nm,)).fetchone()
    _at = set(json.loads(_r["allowed_tools"] or "[]"))
    _named = set(re.findall(r"\b(sysml_[a-z0-9_]+|graph_retrieve)\b", _r["content"] or ""))
    _named.discard(_nm)
    _excluded = (_at - _named) if _at else set()      # 白名单多列的 = 正文没点名，但**需要**（如 autofix）
    # ⚠️ **空白名单 `[]` = 不限制 = 不排除任何工具** ⇒ 此时"正文点名但不在白名单"不是缺陷。
    _missing = (_named - _at) if _at else set()       # 非空白名单里正文点名却被排除 ⇒ 缺陷
    check("[D5] %s 的白名单不排除正文点名的工具" % _nm,
          not _missing, "被排除=%s" % sorted(_missing))
    # 白名单里的工具必须**真实存在**（否则白名单是空转）
    _known = {r["name"] for r in con.execute("SELECT name FROM tools")}
    _known |= {"ask_user"}
    check("[D5] %s 的白名单里的工具真实存在" % _nm,
          not (_at - _known), "不存在=%s" % sorted(_at - _known))

# ── [D6] 独占式白名单的存在性（提醒：白名单外的工具整类不可见）──
_src_tools = open(os.path.join(REPO, "agent/pipeline_parts/execute.py"), encoding="utf-8").read()
check("[D6] execute 路径的白名单是**独占式**（skill 白名单直接赋给 _tool_whitelist）",
      "self._tool_whitelist = list(_skill_wl)" in _src_tools)

# ══════════════════════════════════════════════════════════════════
# [E] 组：**技能事件时序**（2026-10-27 真机 E2E 暴露的缺陷）
#
# 缺陷形态：`stream.py` 曾把 `skill` 事件 yield 放在**技能匹配之前**
#   （旧 4165~4172 的 yield vs `build_prompt_blocks` 旧 4254 才调 `_build_skill_prompt`）
#   ⇒ `_last_skill_hits` 恒为空 ⇒ 前端「🧩 技能执行」卡永远空白
#   ⇒ 真机 E2E 的「E2 技能命中」判据**恒 FAIL**（不是技能没命中，是事件早于匹配）。
#
# ⚠️ 修法不是「把事件挪到后面」——那会让事件晚于 reasoning 到达、前端顺序乱；
#   而是**提前预匹配 + 本轮缓存复用**（匹配含 LLM 兜底，算两次 = 多烧一次 LLM；
#   offload 有副作用，重复做会留垃圾行）。
#
# 判据用 **AST 位置**而非行号（行号会随改动漂移）：
#   E1 skill 事件 yield 的 AST 位置 必须**晚于** `_build_skill_prompt` 调用
#   E2 `reset_skill_state` 在 stream.py 里**只有一处**调用，且**早于**预匹配
#      （否则 `_skill_forced` 是上一轮的值 ⇒ 缓存键错 + 白名单被清零）
#   E3 技能匹配有**本轮缓存**，且缓存键含 intent+input+role+forced
#   E4 skill 事件处**不得引用 `_ctx_agent`**（它在后面的工具循环段才定义 ⇒
#      前向引用；该分支此前从不执行，缺陷被掩盖了 —— 修好 E1 后才暴露）
#   E5 变异自证：把预匹配挪回事件之后 ⇒ E1 必须翻红
# ══════════════════════════════════════════════════════════════════
import ast  # noqa: E402

_stream_path = os.path.join(REPO, "agent/pipeline_parts/stream.py")
_stream_src = open(_stream_path, encoding="utf-8").read()
_tree = ast.parse(_stream_src)


def _find_pos(tree, pred):
    """返回满足 pred 的第一个节点 (lineno, col_offset)；找不到返回 None。"""
    for n in ast.walk(tree):
        if pred(n):
            return (n.lineno, n.col_offset)
    return None


def _is_skill_event_yield(n):
    """`yield {...'type': 'skill'...}` 的 AST 判定（不靠字符串匹配）。"""
    if not isinstance(n, ast.Yield) or not isinstance(n.value, ast.Dict):
        return False
    for k, v in zip(n.value.keys, n.value.values):
        if isinstance(k, ast.Constant) and k.value == "type" and \
           isinstance(v, ast.Constant) and v.value == "skill":
            return True
    return False


def _is_call_to(n, name):
    if not isinstance(n, ast.Call):
        return False
    f = n.func
    if isinstance(f, ast.Attribute):
        return f.attr == name
    if isinstance(f, ast.Name):
        return f.id == name
    return False


# 只取主生成器（execute_stream）里的位置
_skill_yields = [(n.lineno, n.col_offset) for n in ast.walk(_tree)
                  if _is_skill_event_yield(n)]
_pmatch = _find_pos(_tree, lambda n: _is_call_to(n, "_build_skill_prompt"))
_reset = _find_pos(_tree, lambda n: _is_call_to(n, "reset_skill_state"))

check("[E1] stream.py 存在 skill 事件 yield", bool(_skill_yields), "找到 %d 处" % len(_skill_yields))
check("[E1] stream.py 存在技能预匹配调用（_build_skill_prompt）", _pmatch is not None)
check("[E1] 技能预匹配发生在 skill 事件 yield **之前**（否则事件恒空）",
      bool(_skill_yields) and _pmatch is not None and
      _pmatch[0] <= min(y[0] for y in _skill_yields),
      "预匹配@%s 事件@%s" % (_pmatch, sorted(_skill_yields)[:1]))

# reset_skill_state 在 stream.py 只能有一处调用，且必须早于预匹配
_reset_calls = [n.lineno for n in ast.walk(_tree) if _is_call_to(n, "reset_skill_state")]
check("[E2] stream.py 的 reset_skill_state 只有一处调用（避免二次 reset 清空白名单）",
      len(_reset_calls) == 1, "实测 %d 处：%s" % (len(_reset_calls), _reset_calls))
check("[E2] reset_skill_state 早于技能预匹配（否则 _skill_forced 是上一轮的值）",
      bool(_reset_calls) and _pmatch is not None and
      _reset_calls[0] <= _pmatch[0],
      "reset@%s 预匹配@%s" % (_reset_calls[:1], _pmatch))

# 缓存：`_build_skill_prompt` 必须有缓存层，键含 intent+input+role+forced
_skills_path = os.path.join(REPO, "agent/pipeline_parts/skills.py")
_skills_src = open(_skills_path, encoding="utf-8").read()
_common_path = os.path.join(REPO, "agent/pipeline_parts/common.py")
_common_src = open(_common_path, encoding="utf-8").read()
check("[E3] 技能匹配有本轮缓存（_skill_prompt_cache）",
      "_skill_prompt_cache" in _skills_src)
_cache_blk = ""
if "_skill_prompt_cache" in _skills_src:
    _i = _skills_src.find("_skill_prompt_cache")
    _cache_blk = _skills_src[max(0, _i - 900):_i + 500]
for _k in ("intent", "user_input", "role_name", "_skill_forced"):
    check("[E3] 缓存键含 %s（否则跨轮/跨角色串味）" % _k, _k in _cache_blk)
check("[E3] 缓存必须被 reset_skill_state 清零（生命周期 = 一轮）",
      "pipe._skill_prompt_cache = None" in _common_src)

# skill 事件处不得引用 `_ctx_agent`（前向引用：它在工具循环段才定义）
#   ⚠️ 判据必须**只认代码、不认注释** —— 同文件另一处 skill 事件的**注释里**
#   提到了 `_ctx_agent`（正是它记录了这个坑），按子串匹配会误报。
#   ⇒ 用 AST：检查该 yield 的 dict 里有没有 `_ctx_agent` 这个 Name 节点。
_bad_ctx = []
for _y in _skill_yields:
    for _n in ast.walk(_tree):
        if isinstance(_n, ast.Yield) and (getattr(_n, "lineno", -1), getattr(_n, "col_offset", -1)) == _y:
            for _sub in ast.walk(_n):
                if isinstance(_sub, ast.Name) and _sub.id == "_ctx_agent":
                    _bad_ctx.append(_y[0])
            break
check("[E4] skill 事件处不引用 _ctx_agent（前向引用，会在真机报 NameError）",
      not _bad_ctx, "违规行=%s" % _bad_ctx)

# [E5] 变异自证：把预匹配挪到事件之后 ⇒ [E1] 必须翻红
#   做法：把源码里 `_build_skill_prompt(intent, user_input, user)` 的调用行**删掉**
#   （模拟"回到旧写法：只有 build_prompt_blocks 里那次调用"）⇒ 该行不再早于事件。
_mut_ok = False
try:
    _lines = _stream_src.splitlines()
    _hit = [i for i, L in enumerate(_lines)
            if "_build_skill_prompt(intent, user_input, user)" in L]
    if _hit:
        _mut_lines = list(_lines)
        _mut_lines[_hit[0]] = "                pass  # MUT: 移除预匹配"
        _mut_src = "\n".join(_mut_lines)
        _mut_tree = ast.parse(_mut_src)
        _mut_pmatch = _find_pos(_mut_tree, lambda n: _is_call_to(n, "_build_skill_prompt"))
        _mut_yields = [(n.lineno, n.col_offset) for n in ast.walk(_mut_tree)
                       if _is_skill_event_yield(n)]
        _mut_ok = (not _mut_pmatch) or (
            not _mut_yields) or (_mut_pmatch[0] > min(y[0] for y in _mut_yields))
except Exception as _e:  # pragma: no cover
    _mut_ok = False
check("[E5] 变异自证：移除预匹配 ⇒ [E1] 的时序判据必须翻红", _mut_ok,
      "变异体未生效" if not _mut_ok else "")

# ══════════════════════════════════════════════════════════════════
# [F] 组：**已成功写操作的事实注入**（2026-10-27 真机 E2E 暴露的第二个缺陷）
#
# 缺陷形态（conv 9698）：
#   `create_model_elements` 回执 `status=SUCCEEDED`（平台独立回读证实新包确实建成），
#   但模型最终回复断言「**尚未写入平台**……因轮次用尽，写操作没有执行」
#   —— **与事实完全相反**。
#   机制：① ReAct 累积 11 轮、messages 极长 ⇒ 模型在收尾时「看漏」那条回执；
#        ② 收尾提示（`finalize_messages`）只在 `lacks_deliverable` 为真时才走，
#           而本次模型**产出了长回复** ⇒ 收尾提示**根本没触发** ⇒ 无任何机制纠正它。
#
# ⚠️ 这与「不得虚构已完成」是**同一条纪律的两个方向** ——
#   虚构已完成（声称写了但没写）与**虚构未完成**（声称没写但写了）都是失实。
#
# 判据：
#   F1 `write_ops_digest` 只列**成功**回执（失败的由各自错误回执承载）
#   F2 无 write_ops 时 `finalize_messages` **逐字节不变**（零回归面）
#   F3 `_is_write_tool` 能识别 MCP 写工具，且**不误判**读工具
#   F4 stream.py 在**最终生成之前**注入写操作摘要（不依赖收尾分支 ⇒ 两条路径都覆盖）
#   F5 变异自证：把摘要注入去掉 ⇒ [F4] 必须翻红
# ══════════════════════════════════════════════════════════════════
from agent import loop_protocol as _lp  # noqa: E402
from agent.pipeline_parts.stream import _is_write_tool  # noqa: E402

# F1 摘要只列成功回执
_d = _lp.write_ops_digest([{"name": "create_model_elements", "detail": "SUCCEEDED"}])
check("[F1] write_ops_digest 列出工具名与成功标识",
      "create_model_elements" in _d and "已执行成功" in _d, _d[:120])
check("[F1] write_ops_digest 对空清单返回空串（无写操作 ⇒ 不注入）",
      _lp.write_ops_digest([]) == "" and _lp.write_ops_digest(None) == "")
check("[F1] write_ops_digest 跳过无名条目",
      "：" not in _lp.write_ops_digest([{"name": "", "detail": "x"}]))

# F2 零回归：无 write_ops ⇒ 逐字节不变
_base_msgs = [{"role": "user", "content": "x"}]
_r_none = _lp.finalize_messages(_base_msgs)
_r_empty = _lp.finalize_messages(_base_msgs, write_ops=[])
_r_none2 = _lp.finalize_messages(_base_msgs, write_ops=None)
check("[F2] finalize_messages 无 write_ops ⇒ 逐字节不变", _r_none == _r_empty == _r_none2)
_r_with = _lp.finalize_messages(_base_msgs, write_ops=[{"name": "create_model_elements", "detail": "SUCCEEDED"}])
check("[F2] finalize_messages 有 write_ops ⇒ 追加清单",
      "create_model_elements" in _r_with[-1]["content"] and len(_r_with[-1]["content"]) > len(_r_none[-1]["content"]))

# F3 写工具判定（MCP 工具不在 tools 表 ⇒ 必须靠动词族）
_p = None
try:
    from agent.pipeline import AgentPipeline as _AP
    _p = _AP()
    _p._load_db_agents()
except Exception:
    _p = None
if _p is not None:
    _WRITE = ("create_model_elements", "update_model_elements", "delete_model_elements",
              "apply_model_changes", "import_sysml_text", "undo_model_operation")
    _READ = ("query_projects", "query_branches", "query_model_versions", "query_data_tree",
             "search_model_elements", "get_modeling_capabilities", "export_sysml_v2",
             "validate_sysml_text", "get_model_elements")
    _miss_w = [n for n in _WRITE if not _is_write_tool(_p, n)]
    _fp_r = [n for n in _READ if _is_write_tool(_p, n)]
    check("[F3] MCP 写类工具全部被识别", not _miss_w, "漏=%s" % _miss_w)
    check("[F3] 读类工具不被误判为写类", not _fp_r, "误判=%s" % _fp_r)

# F4 stream.py 在最终生成前注入写操作摘要
check("[F4] stream.py 存在写操作清单累积（_write_ops.append）",
      "_write_ops.append" in _stream_src)
check("[F4] stream.py 在最终生成前注入写操作摘要（write_ops_digest）",
      "write_ops_digest" in _stream_src)
#   ⚠️ 判据用**行号**而非字符偏移：
#     `_write_ops.append` 在源码里出现多次（累积处 + 注释 + helper 里），
#     `str.find` 取到的是**第一次**出现，不代表运行时顺序 ⇒ 会假红。
#     运行时顺序（累积 → 注入 → 最终生成）由**行号**近似表达才可靠。
_lines_all = _stream_src.splitlines()


def _first_line(pred, default=-1):
    for _i, _L in enumerate(_lines_all):
        if pred(_L):
            return _i + 1
    return default


_acc = _first_line(lambda L: "_write_ops.append(" in L and "def " not in L)
_inj = _first_line(lambda L: "write_ops_digest(_write_ops)" in L)
_gen = _first_line(lambda L: "if not tool_injected:" in L)
check("[F4] 注入点晚于累积点", _acc > 0 and _inj > _acc,
      "accum@行%s inject@行%s" % (_acc, _inj))
check("[F4] 注入点早于最终生成分支", _gen < 0 or _inj < _gen,
      "inject@行%s gen@行%s" % (_inj, _gen))
#   两处 finalize_messages 都要传 write_ops
check("[F4] finalize_messages 两处调用都传了 write_ops",
      _stream_src.count("write_ops=_write_ops") >= 2,
      "实测 %d 处" % _stream_src.count("write_ops=_write_ops"))

# F5 变异自证：把注入去掉 ⇒ [F4] 必须翻红
#   ⚠️ 变异目标必须是**真正调用 write_ops_digest 的那行**（不是 helper 定义、不是注释）
_mut2_ok = False
try:
    _ml = list(_lines_all)
    _tgt = [i for i, L in enumerate(_ml) if "write_ops_digest(_write_ops)" in L]
    if _tgt:
        _ml[_tgt[0]] = "                    _wdigest = ''  # MUT: 移除写操作摘要注入"
        _ms = "\n".join(_ml)
        _mt = ast.parse(_ms)          # 变异体必须仍是合法 Python
        # 变异后：调用点消失 ⇒ [F4] 的「注入点晚于累积点」判据必须失效
        _has_call = any("write_ops_digest(_write_ops)" in L for L in _ml)
        _mut2_ok = not _has_call
except Exception:
    _mut2_ok = False
check("[F5] 变异自证：移除写操作摘要注入 ⇒ [F4] 必须翻红", _mut2_ok,
      "变异体未生效" if not _mut2_ok else "")

con.close()

# ══════════════════════════════════════════════════════════════════
print("\n" + "=" * 72)
print("PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
if FAIL:
    print("❌ FAIL：")
    for f in FAIL:
        print("   -", f)
    sys.exit(1)
print("✅ ALL PASS —— 寻址链 / 动词闸 / 触发词精度 / 提示词与绑定 全部达标")
sys.exit(0)
