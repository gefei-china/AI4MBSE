# CI-OPTIONAL: [local] tier=local（见 tools/gates_manifest.json）：需真实服务 / 真 LLM key / 生产真库金标或随包运行时资产，CI 无服务无 secret ⇒ 跑不了；本机已实测通过，改法须同时改清单 tier 并接线，不允许只删这行
# -*- coding: utf-8 -*-
"""能力定义自检：Agent / Skill / Tool 的字段是否"写得准"（2026-11-07）。

## 为什么需要"自检器"而不是"填写规范"

规范写在文档里没人执行 ⇒ 凭感觉填 ⇒ 出问题时无法归因。
本模块把规范落成**判据**，每条都能在真实数据上翻红，并给出**改法**。

## 设计与平台真实消费点对齐（不是通用最佳实践）

| 字段 | 真实消费点 | 写错的后果（实测过） |
|---|---|---|
| `agents.description` | 语义索引文本 `display_name + description + keywords`
  （`agent/pipeline.py:88`，**不含 system_prompt**） | 描述太短 ⇒ 语义层无信号 ⇒ 落到 chat/knowledge_qa |
| `agents.system_prompt` | `role` 块（`prompt.py:287`，上限 8000 字符） | 提到的工具没绑 ⇒ 诱导幻觉调用 |
| `agents.intent_keywords` | 关键词层（`intent.py:342`，硬编码词表已删，**这是唯一词表源**） | 泛词抢路由（`_GENERIC_KW` 打分压到 0，但仍会成候选） |
| `skills.triggers` | 子串/合取匹配（`skills.py:15`，先于语义层） | 写"包""元素"⇒ 一次输入命中 3 个技能 |
| `skills.description` | 语义索引 + **未命中时只显示这一行**（`skills.py:262`） | 空描述 ⇒ 模型只知道有个技能，不知何时用 |
| `skills.allowed_tools` | 命中后**补入候选**（授权语义，非上限） | 写了不存在的工具 ⇒ 授权了却永远调不到 |
| `tools.description` | ① JIT 语义打分（决定注入哪几个）② HIL 确认卡文案 | 71 个工具描述全同 ⇒ 语义零区分度，只能全量注入 |
| `tools.input_schema.required` | 缺失参数**不得入确认队列**（`tool_readiness`） | 无 required ⇒ 畸形调用进队列，批准后必然失败 |

## 判据分级

- **ERR**：一定导致"配了不生效"，必须改
- **WARN**：会降低准确率/可维护性，建议改
- **INFO**：现状观察，不判错

用法：
    ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_agent_spec.py            # 全量体检
    ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_agent_spec.py --kind agent
    ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_agent_spec.py --one review
    ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_agent_spec.py --gate       # 只对内置资产当门禁
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, _ROOT)

#: 描述里应出现的"能力三要素"信号（任一即算有）
_CAP_VERBS = ("执行", "完成", "生成", "解析", "校验", "检索", "查询", "创建", "修改",
              "删除", "导出", "导入", "分析", "评审", "解释", "回答", "计算", "转换",
              "汇总", "编排", "调度", "写入", "读取", "提取", "对齐", "比对", "推荐")
_OBJ_HINTS = ("模型", "需求", "视图", "代码", "文档", "工程", "报告", "知识", "工具",
              "能力", "数据", "接口", "方案", "矩阵", "追溯", "指标", "场景", "Agent",
              "技能", "用户", "权限", "审计", "会话", "版本", "包", "元素", "关系")
_SCENE_HINTS = ("当", "用于", "适用", "面向", "针对", "在…时", "时使用", "场景",
                "用户", "说", "问", "请求", "任务", "前置", "触发")
_BOUNDARY_HINTS = ("不", "排除", "除…外", "边界", "不负责", "不替代", "超出", "仅",
                   "不要", "禁止", "不执行", "不涉及")

#: 复用平台的泛词表（单一真源：`agent/intent.py::_GENERIC_KW`）
def _generic_kw() -> set:
    try:
        from agent.intent import IntentRouter
        return set(IntentRouter._GENERIC_KW)
    except Exception:
        return set()


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", str(s or ""))


class Finding:
    __slots__ = ("level", "code", "subject", "detail", "fix")

    def __init__(self, level, code, subject, detail, fix=""):
        self.level, self.code, self.subject = level, code, subject
        self.detail, self.fix = detail, fix

    def __str__(self):
        tail = ("  → 改法：" + self.fix) if self.fix else ""
        return "  [%s] %-4s %s%s" % (self.level, self.code, self.detail, tail)


# ══════════════════════════════════════════════════════════════════════
# Agent
# ══════════════════════════════════════════════════════════════════════

def audit_agent(row: dict, binds: list, all_binds: dict) -> list:
    """`binds` = 该 Agent 绑的 [(tool_type, tool_name)]；
    `all_binds` = {"tools": 全部工具名, "mcp_tools": {server_name: set(工具名)}}。

    ⚠️ **必须展开 MCP server 绑定**（2026-11-07 自检器第一版踩的假红）：
       智源对接agent 绑的是 `tool_type='mcp'` 的整个 server（71 个工具），
       只认 `tool_type='tool'` 就会把 `query_projects` 等 9 个工具判成"未绑定"
       ⇒ 报告 9 条 ERR，全是假的。判据必须匹配**实际授权形态**，
       否则自检器自己就成了噪音源（与"门禁假绿"同族，只是方向相反：假红）。
    """
    out = []
    name = row.get("name") or ""
    desc = str(row.get("description") or "").strip()
    prompt = str(row.get("system_prompt") or "")
    kws = []
    try:
        kws = json.loads(row.get("intent_keywords") or "[]")
    except Exception:
        pass

    # 已授权工具 = 逐个绑的 tool + 其绑定 MCP server 下的全部工具
    bound_tools = {n for t, n in binds if t == "tool"}
    for t, n in binds:
        if t == "mcp":
            bound_tools |= set((all_binds.get("mcp_tools") or {}).get(n) or set())

    # ── A1 描述长度：它是语义索引里唯一的实质内容 ──
    if len(desc) < 40:
        out.append(Finding(
            "ERR", "A1", name,
            "描述仅 %d 字符（语义路由文本 = display_name+description+keywords，"
            "prompt 不参与路由）" % len(desc),
            "写到 80~200 字符：做什么 + 对什么对象 + 什么话术下用它 + 不接什么活"))
    elif len(desc) < 80:
        out.append(Finding("WARN", "A1", name,
                           "描述 %d 字符偏短，语义层区分度不足" % len(desc),
                           "补全触发场景与对象，目标 80~200 字符"))

    # ── A2 能力三要素 ──
    if desc:
        has_v = any(v in desc for v in _CAP_VERBS)
        has_o = any(o in desc for o in _OBJ_HINTS)
        miss = [n for n, ok in (("动作", has_v), ("对象", has_o)) if not ok]
        if miss:
            out.append(Finding("WARN", "A2", name,
                               "描述缺「%s」要素：%s" % ("/".join(miss), desc[:60]),
                               "写成「<动作> <对象>，用于<场景>」三段式"))

    # ── A3 边界声明（防抢占：路由是互斥选择，没说清边界就会被别的意图抢）──
    if desc and not any(b in desc for b in _BOUNDARY_HINTS):
        out.append(Finding("WARN", "A3", name,
                           "描述里没有边界（不做什么/不接什么活）",
                           "加半句「不负责 X（交给 Y）」——路由是互斥选择"))

    # ── A4 关键词含泛词 ──
    gen = _generic_kw()
    hit = [k for k in kws if k in gen]
    if hit:
        out.append(Finding("WARN", "A4", name,
                           "关键词含泛词 %s（关键词层打分会被压到 0，但仍会成竞争候选）"
                           % "、".join(hit),
                           "删掉，换成该 Agent 独有的说法（如「预评审」「需求质量」）"))
    if not kws:
        out.append(Finding("INFO", "A4b", name, "没有意图关键词（只能靠语义层或显式指派）",
                           "至少给 3~8 个该域独有说法"))

    # ── A5 prompt 提到的工具 ⊆ 实际授权（幻觉源）──
    # 只认"像工具名"的 token：小写+下划线，长度≥6，且不是常见词
    mentioned = set(re.findall(r"\b([a-z][a-z0-9]*(?:_[a-z0-9]+){1,4})\b", prompt))
    known_tool_names = {n for n in all_binds.get("tools", set())}
    active_names = {n for n in all_binds.get("active_tools", known_tool_names)}
    # ★ 2026-11-07 判据修正（自检器第一版误伤）：**只统计 status='active' 的工具**。
    #   原因：非 active 的工具根本不会被注入 ⇒ 模型不可能调到它 ⇒ 也报不出"没有执行器"。
    #   把"停用工具"算进 ghost，会把「如实说明某工具当前停用」这句**正确表述**判成缺陷
    #   —— 实测智源对接agent 的 prompt 写「`sysml_stdlib_meta` 当前停用，需要时如实说明」，
    #   被报 ERR。这是判据把「告知不可用」误读成「承诺可用」。
    ghost = sorted(t for t in mentioned
                   if t in active_names and t not in bound_tools)
    # 停用工具若被提到，要求 prompt 里同时出现"停用/不可用/禁用"的说明，
    # 否则模型会以为它可用（这才是真正的幻觉源，且与绑不绑无关）。
    _dead = sorted(t for t in mentioned
                   if t in known_tool_names and t not in active_names and t not in bound_tools)
    _dead_silent = []
    for _t in _dead:
        _i = prompt.find(_t)
        _win = prompt[max(0, _i - 120):_i + 160] if _i >= 0 else ""
        if not any(k in _win for k in ("停用", "禁用", "不可用", "未启用", "已下线")):
            _dead_silent.append(_t)
    if _dead_silent:
        out.append(Finding(
            "WARN", "A5b", name,
            "prompt 提到已停用工具但没说明它停用：%s" % "、".join(_dead_silent[:4]),
            "补一句「当前停用」或删掉——否则模型会去调一个永不出现的工具"))
    if ghost:
        out.append(Finding(
            "ERR", "A5", name,
            "prompt 提到 %d 个**存在但未绑定**的工具：%s" % (len(ghost), "、".join(ghost[:5])),
            "要么绑上，要么从 prompt 删掉——模型会照着 prompt 去调，然后报「没有执行器」"))

    # ── A6 绑了但 prompt 全没提（盲绑：模型不知何时该用）──
    # ⚠️ 绑了 MCP server 时 bound_tools 会有几十项 ⇒ 这里只对**逐绑的 tool** 判，
    #    否则每个 MCP Agent 都刷一条 INFO，纯噪音。
    if bound_tools:
        _explicit = {n for t, n in binds if t == "tool"}
        silent = sorted(t for t in _explicit
                        if t not in prompt and len(t) >= 6)
        # 只对"非通用读工具"提示，避免噪音
        _skip = {"capability_query", "doc_parse", "code_exec", "sys_query_users",
                 "sys_query_roles", "sys_query_perms", "sys_query_audit",
                 "sys_query_monitor", "sys_query_convs"}
        silent = [t for t in silent if t not in _skip]
        if silent:
            out.append(Finding(
                "INFO", "A6", name,
                "绑了 %d 个工具但 prompt 未提及（模型只能靠工具自带的 description 判断）"
                % len(silent),
                "关键工具在 prompt 里写清「何时用 / 什么时候不要用」"))

    # ── A7 prompt 长度 ──
    from agent.pipeline_parts.common import _AGENT_ROLE_CAP
    if len(prompt) > _AGENT_ROLE_CAP:
        out.append(Finding(
            "WARN", "A7", name,
            "prompt %d 字符 > 上限 %d（超限走段级折叠，可裁段会被折掉）"
            % (len(prompt), _AGENT_ROLE_CAP),
            "把工具手册这类可裁内容挪到技能正文，prompt 只留职责/约束/输出"))
    if not prompt.strip():
        out.append(Finding("ERR", "A7b", name, "prompt 为空（会退化成通用助手）",
                           "至少写身份 + 职责 + 输出要求 + 边界"))
    return out


# ══════════════════════════════════════════════════════════════════════
# Skill
# ══════════════════════════════════════════════════════════════════════

def audit_skill(row: dict, trig_index: dict, tool_names: set) -> list:
    """`trig_index` = trigger → [其它技能名]（用于冲突检测）。"""
    out = []
    name = row.get("name") or ""
    desc = str(row.get("description") or "").strip()
    content = str(row.get("content") or "")
    try:
        trigs = [str(x) for x in json.loads(row.get("triggers") or "[]")]
    except Exception:
        trigs = []
    try:
        atools = [str(x) for x in json.loads(row.get("allowed_tools") or "[]")]
    except Exception:
        atools = []

    # ── S1 触发词含泛词 ──
    gen = _generic_kw()
    hit = [t for t in trigs if t in gen]
    if hit:
        out.append(Finding(
            "ERR", "S1", name,
            "触发词含泛词 %s（子串匹配 ⇒ 会把无关请求拉进来；实测一次输入命中 3 个技能）"
            % "、".join(hit),
            "改成合取写法（如「新增+包」）或换成该技能独有的说法"))

    # ── S2 触发词与其它技能冲突 ──
    conflicts = {}
    for t in trigs:
        others = [x for x in (trig_index.get(t) or []) if x != name]
        if others:
            conflicts[t] = others
    if conflicts:
        _txt = "；".join("%s↔%s" % (k, "、".join(v[:3])) for k, v in list(conflicts.items())[:3])
        out.append(Finding(
            "WARN", "S2", name,
            "触发词与其它技能重复（会同时命中）：%s" % _txt,
            "把其中一个改成合取写法，或合并成一个技能"))

    # ── S3 描述：未命中时它是唯一被模型看到的一行 ──
    if not desc:
        out.append(Finding("ERR", "S3", name, "描述为空", "写一句「什么情况下用我」"))
    elif len(desc) < 20:
        out.append(Finding("WARN", "S3", name, "描述仅 %d 字符" % len(desc),
                           "补到 30~120 字符"))

    # ── S4 声明的工具必须真实存在 ──
    ghost = [t for t in atools if t not in tool_names]
    if ghost:
        out.append(Finding(
            "ERR", "S4", name,
            "allowed_tools 里有 %d 个不存在的工具：%s" % (len(ghost), "、".join(ghost[:5])),
            "删掉或先注册——命中时会把它们补进候选，绑了不存在的名字等于白授权"))

    # ── S5 正文要能约束行为 ──
    if content and len(content) < 200:
        out.append(Finding("WARN", "S5", name,
                           "正文仅 %d 字符（太薄，命中后给不了可执行约束）" % len(content),
                           "补：工具映射（哪步用哪个工具）+ 输出铁律 + 反例"))
    _has_rule = any(k in content for k in ("铁律", "必须", "禁止", "不得", "不要"))
    if content and not _has_rule:
        out.append(Finding(
            "WARN", "S5b", name, "正文没有强制性约束（必须/禁止/不得）",
            "加「输出铁律」段——这是防空回复、防编造的唯一抓手"))

    # ── S6 声明了脚本但没在正文里说明怎么执行 ──
    try:
        scripts = json.loads(row.get("scripts") or "[]")
    except Exception:
        scripts = []
    if scripts and "skill_script_run" not in content:
        out.append(Finding(
            "WARN", "S6", name,
            "声明了脚本 %s 但正文没提 `skill_script_run`" % scripts[:2],
            "写清「用 skill_script_run 执行，argv 怎么传」——否则模型只会假装执行"))
    return out


# ══════════════════════════════════════════════════════════════════════
# Tool
# ══════════════════════════════════════════════════════════════════════

def audit_tool(row: dict, desc_index: dict) -> list:
    out = []
    name = row.get("name") or ""
    desc = str(row.get("description") or "").strip()
    se = str(row.get("side_effect") or "")
    try:
        schema = json.loads(row.get("input_schema") or "{}")
    except Exception:
        schema = {}
    props = (schema or {}).get("properties") or {}
    req = (schema or {}).get("required") or []

    # ── T1 描述为空 ──
    if not desc:
        out.append(Finding("ERR", "T1", name, "描述为空（模型靠它判断何时调用；JIT 也无法打分）",
                           "写「做什么 + 什么时候用 + 关键参数含义」"))

    # ── T2 描述与其它工具完全相同（JIT 零区分度）──
    same = [x for x in (desc_index.get(_norm(desc)) or []) if x != name]
    if desc and same:
        out.append(Finding(
            "ERR", "T2", name,
            "描述与 %d 个工具完全相同：%s" % (len(same), "、".join(same[:4])),
            "描述要能区分用途——全同会让 JIT 语义预筛失去区分度（实测 71 个 MCP 工具全同）"))

    # ── T3 多参工具缺 required ──
    if len(props) >= 2 and not req and row.get("source") != "mcp":
        out.append(Finding(
            "WARN", "T3", name,
            "有 %d 个参数但没有 required（缺失参数无法在入队前被拦）" % len(props),
            "把真正必填的列进 required——否则畸形调用会进确认队列，批准后必然失败"))

    # ── T4 副作用与名字/语义不符 ──
    # ⚠️ 2026-11-07 判据修正（第二处误伤）：**不能只看名字有没有 delete**。
    #   `get_model_delete_impact` / `preview_model_changes` 这类名字里带写动词、
    #   但语义是**只读查询**（查删除影响面、预览变更），标 read 是**正确**的。
    #   `verify_hil_write_gate.py` 的 A 组也把这两条固定为 read 期望 —— 判据必须与它一致，
    #   否则两条门禁互相打架，且会诱导人去把一个只读工具错误标成 write（真会造成多余确认）。
    _READ_ONLY_PREFIX = ("get_", "preview_", "check_", "query_", "list_", "search_",
                         "validate_", "count_", "diff_")
    _is_ro_name = name.lower().startswith(_READ_ONLY_PREFIX)
    if se == "read" and not _is_ro_name and re.search(
            r"(create|update|delete|remove|import|apply|write|save|set|move)",
            name, re.I):
        out.append(Finding(
            "ERR", "T4", name,
            "名字像写操作但 side_effect=read（会绕过写前确认闸）",
            "改成 write/destructive，或用 MCP 工具的 side-effect 接口人工定档"))
    if se == "destructive" and row.get("risk_level") != "high":
        out.append(Finding("WARN", "T4b", name,
                           "destructive 但 risk_level=%s" % row.get("risk_level"),
                           "改成 high（HIL 档位按 risk 取）"))
    return out


# ══════════════════════════════════════════════════════════════════════
# 驱动
# ══════════════════════════════════════════════════════════════════════

def main() -> int:
    args = sys.argv[1:]
    kind = ""
    one = ""
    gate = "--gate" in args
    for i, a in enumerate(args):
        if a == "--kind" and i + 1 < len(args):
            kind = args[i + 1]
        if a == "--one" and i + 1 < len(args):
            one = args[i + 1]

    db = os.path.join(_ROOT, "mbse.db")
    conn = sqlite3.connect("file:%s?mode=ro" % db.replace("\\", "/"), uri=True)
    conn.row_factory = sqlite3.Row
    try:
        all_f = []
        n_subj = 0

        if not kind or kind == "agent":
            rows = conn.execute(
                "SELECT * FROM agents WHERE status='active'" +
                (" AND (name=? OR display_name=?)" if one else "") +
                " ORDER BY agent_role='main' DESC, name",
                ((one, one) if one else ())).fetchall()
            tool_names = {r[0] for r in conn.execute(
                "SELECT DISTINCT name FROM tools").fetchall()}
            active_tools = {r[0] for r in conn.execute(
                "SELECT DISTINCT name FROM tools WHERE status='active'").fetchall()}
            # MCP server → 它的工具集合（展开绑定用，见 audit_agent 的假红说明）
            mcp_tools = {}
            for sr in conn.execute(
                    "SELECT id, name FROM mcp_servers").fetchall():
                mcp_tools[sr[1]] = {x[0] for x in conn.execute(
                    "SELECT name FROM tools WHERE mcp_server_id=?", (sr[0],)).fetchall()}
            for r in rows:
                binds = [(x[0], x[1]) for x in conn.execute(
                    "SELECT tool_type, tool_name FROM agent_tools WHERE agent_id=?",
                    (r["id"],)).fetchall()]
                all_f += audit_agent(dict(r), binds,
                                     {"tools": tool_names, "active_tools": active_tools,
                                      "mcp_tools": mcp_tools})
                n_subj += 1

        if not kind or kind == "skill":
            srows = conn.execute(
                "SELECT * FROM skills WHERE status='published'" +
                (" AND name=?" if one else "") + " ORDER BY name",
                ((one,) if one else ())).fetchall()
            trig_index = {}
            for s in srows:
                try:
                    for t in json.loads(s["triggers"] or "[]"):
                        trig_index.setdefault(str(t), []).append(s["name"])
                except Exception:
                    pass
            tool_names = {r[0] for r in conn.execute(
                "SELECT DISTINCT name FROM tools").fetchall()}
            for s in srows:
                all_f += audit_skill(dict(s), trig_index, tool_names)
                n_subj += 1

        if not kind or kind == "tool":
            trows = conn.execute(
                "SELECT * FROM tools WHERE status='active'" +
                (" AND name=?" if one else "") + " ORDER BY name",
                ((one,) if one else ())).fetchall()
            desc_index = {}
            for t in trows:
                desc_index.setdefault(_norm(t["description"]), []).append(t["name"])
            for t in trows:
                all_f += audit_tool(dict(t), desc_index)
                n_subj += 1
    finally:
        conn.close()

    errs = [f for f in all_f if f.level == "ERR"]
    warns = [f for f in all_f if f.level == "WARN"]
    infos = [f for f in all_f if f.level == "INFO"]

    print("=" * 78)
    print("能力定义自检（%d 个对象）— ERR %d / WARN %d / INFO %d"
          % (n_subj, len(errs), len(warns), len(infos)))
    print("=" * 78)

    by_subj = {}
    for f in all_f:
        by_subj.setdefault(f.subject, []).append(f)
    for subj, fs in sorted(by_subj.items(),
                           key=lambda kv: (-sum(1 for x in kv[1] if x.level == "ERR"),
                                           -sum(1 for x in kv[1] if x.level == "WARN"))):
        if not any(x.level in ("ERR", "WARN") for x in fs) and not one:
            continue          # 全 INFO 的不占版面（除非点名查它）
        print("\n■ %s" % subj)
        for f in fs:
            print(str(f))

    if gate:
        # 门禁用：只对**内置**资产判 ERR（自定义资产允许在建设中）
        print("\n" + "-" * 78)
        print("gate 模式：内置资产的 ERR 计为失败")
        return 0 if not errs else 1
    return 0 if not errs else 1


if __name__ == "__main__":
    sys.exit(main())
