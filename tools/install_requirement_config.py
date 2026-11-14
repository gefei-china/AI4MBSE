# CI-OPTIONAL: [local] 一次性配置安装器（本脚本自身是工具，不入 CI）
# -*- coding: utf-8 -*-
"""需求分析与冲突检测：配置资产安装器（幂等，可重跑）。

## 为什么是「安装器」而不是产品代码
米爸约束：需求规则与标准必须是**可配置知识资产**，不写死代码。
本脚本只把数据写进 DB + 启动快照，产品运行时读这些配置；脚本本身不参与判定。

## 写入内容
1. 3 个 Skill：规则集校验 / 完整性检查 / 冲突检测
2. 3 个 Agent：requirement_rule_check / requirement_completeness_check / requirement_conflict_check
3. Agent 绑定（skills + graph_retrieve）
4. 标准档案：generate_rules 新增配置键（标准集 / 类别开关 / 检索硬门槛）
5. intent_rules 触发词
6. HIL 策略：高风险需求冲突需人工确认（复用现有 decision_point）
7. project_methodology：默认项目绑定标准档案

## 同源纪律
`skills` / `agents` 表由 `data/bootstrap/pipeline_assets.json` 启动自举 UPSERT，
新增资产必须同步进快照，否则重启会被覆盖 / snapshot_drift 报缺。
`skills.content` 在 update_exclude 里（用户可编辑），但新增行仍须进快照才能建行。

用法： ./.venv/Scripts/python.exe -X utf8 tools/install_requirement_config.py [--dry-run]
"""
import argparse
import json
import os
import shutil
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
DB = os.path.join(ROOT, "mbse.db")
SNAP = os.path.join(ROOT, "data", "bootstrap", "pipeline_assets.json")
CATALOG = os.path.join(ROOT, "data", "requirement_standards", "rule_catalog.json")

STANDARD_DOCS = ["Guide to writing Requirements.pdf", "ISO 29148-2011 Requirements Engineering.pdf"]
RULE_SKILLS = ["requirement_rule_check_guide", "requirement_completeness_guide",
               "Requirement_Conflict_Analysis"]


def j(x):
    return json.dumps(x, ensure_ascii=False)


def _skill_frontmatter(s: dict) -> dict:
    """Skill frontmatter 的**唯一构造点**（DB 写入与快照导出共用，避免两处漂移）。

    ⚠️ 故意不含 `must_retrieve` / `required_docs`：
      · 二者全仓零消费点（死配置），留着只会让人以为"这里能控制检索"；
      · `required_docs` 若写死两篇，会让「拆多套标准档案」变成假切换；
      · 文档白名单唯一来源 = `requirement_standard_profile.profiles[*].docs`，
        检索意图判定唯一来源 = `services.requirement_checks.is_check_intent()`（无开关）。
    """
    return {"name": s["name"], "description": s["description"],
            "triggers": s["triggers"], "version": s["version"],
            "allowed-tools": " ".join(s["allowed_tools"]),
            "category": "AI建模", "author": "平台内置"}


def _agent_kb_scope() -> dict:
    """Agent kb_scope 的**唯一构造点**（DB 写入与快照导出共用）。

    ⚠️ Agent 级 **不写 doc 白名单** —— 白名单由「标准档案」唯一决定。
    原因（"拆多套"时暴露）：`merge_scope` 是**并集**语义（用户显式选优先、档案追加），
    若 Agent 里也写死两篇，切到 `incose_only_v1` 时最终 docs 仍是两篇
    ⇒ 检索照样命中 29148 ⇒ **换标准是假的**。同一份清单写两处必然漂移。
    `branches` 仍保留：图谱实体检索按分支口径，与文档白名单是两件事。
    """
    return {"branches": ["release"], "docs": []}


def qcol(name: str) -> str:
    """列名加双引号：`references` 等 SQLite 保留字不加引号会 syntax error。"""
    return '"%s"' % str(name).replace('"', '""')


def build_skills(catalog):
    cats = catalog["rule_categories"]
    comps = catalog["completeness_categories"]
    conflicts = catalog["conflict_types"]

    rule_lines = "\n".join(
        "- %s %s｜依据 %s｜判据：%s｜默认严重度 %s"
        % (c["id"], c["name"], " ; ".join(c["clause_ref"]), c["criterion"], c["severity"])
        for c in cats)
    comp_lines = "\n".join(
        "- %s %s｜依据 %s｜判据：%s"
        % (c["id"], c["name"], " ; ".join(c["clause_ref"]), c["criterion"])
        for c in comps)
    conflict_lines = "\n".join(
        "- %s %s：%s" % (c["id"], c["name"], c["criterion"]) for c in conflicts)

    contract = (
        "\n## 输出契约（必须严格遵守，否则结果判为未判定）\n"
        "只输出一个 JSON 对象，不要包裹 markdown 代码块：\n"
        '{"checks":[{"req_id","rule_id","category","verdict","severity",'
        '"evidence","clause_ref","suggestion"}],'
        '"covered_categories":[...],"configured_categories":[...],'
        '"standard_id":"...","verdict":"checked|insufficient"}\n'
        "- verdict 取值 pass / fail / to_verify；**没有条款出处或原文证据的一律 to_verify**，"
        "不得写成已确认事实。\n"
        "- 必须逐类过一遍规则清单，covered_categories 与 configured_categories 需可逐项核对；"
        "漏类或缺字段时把 verdict 写 insufficient，不许声称已检查。\n"
        "- 每条结论的 clause_ref 必须来自上方清单，不要编造条款号。\n")

    s1 = {
        "name": "requirement_rule_check_guide",
        "skill_type": "text2json",
        "description": "按标准规则集逐条校验条目化需求，输出带条款出处的结构化校验结果",
        "version": "v1.0",
        "status": "published",
        "triggers": ["按标准校验需求", "需求语法规则校验", "需求模板校验", "句式规范校验",
                     "需求规则集", "requirement rule check", "检查需求是否规范"],
        "allowed_tools": ["graph_retrieve"],
        "allowed_roles": [],
        "content": (
            "# 需求模板语法与句式规则校验（规则集即配置）\n\n"
            "## 前置（硬门槛）\n"
            "生成任何结论前必须先调用 `graph_retrieve` 检索当前标准档案中的条款原文"
            "（query 带章节锚点，如 `29148 5.2.5 Characteristics of individual requirements`"
            " 或 `Guide R11 Separate Clauses`）。未检索不得输出结论；检索不到就如实写"
            "「依据不足」并把 verdict 置 insufficient，绝不凭记忆编造条款。\n\n"
            "## 输入\n"
            "条目化需求清单（`req_id` / `text`），以及当前标准档案的规则类别子集。\n\n"
            "## 规则类别清单（%d 类，来自知识库真实条款；修改本段即改规则，无需改代码）\n%s\n\n"
            "## 判定流程\n"
            "1. 读取当前标准档案启用的类别子集，逐条需求逐类过一遍。\n"
            "2. 每条结论必须给出需求原文片段作为 evidence、清单里的 clause_ref 作为出处。\n"
            "3. 无法判定（信息不足 / 条款未检索到）标 to_verify，不计入 pass。\n"
            "4. 按 severity 排序输出，并给出可执行的修改建议。\n%s"
        ) % (len(cats), rule_lines, contract),
    }

    s2 = {
        "name": "requirement_completeness_guide",
        "skill_type": "text2json",
        "description": "按标准检查需求集完整性，输出按类别组织的结构化完整性结果",
        "version": "v1.0",
        "status": "published",
        "triggers": ["需求完整性检查", "检查需求是否完整", "需求完备性", "completeness check",
                     "需求缺项检查"],
        "allowed_tools": ["graph_retrieve"],
        "allowed_roles": [],
        "content": (
            "# 需求完整性检查（类别即配置）\n\n"
            "## 前置（硬门槛）\n"
            "先调用 `graph_retrieve` 检索标准条款（如 `29148 5.2.6 Characteristics of a set "
            "of requirements`）。未检索不得声称已检查；依据不足时如实标注。\n\n"
            "## 输入\n"
            "条目化需求清单 + 当前标准档案启用的完整性类别子集。\n\n"
            "## 完整性验证类别（%d 类，来自知识库真实条款）\n%s\n\n"
            "## 判定流程\n"
            "1. 逐类检查，每类输出 covered(bool) / missing[] / evidence / clause_ref。\n"
            "2. missing 必须引用具体需求编号或说明缺哪一类内容，不许写笼统的「信息不足」。\n"
            "3. 未覆盖的类别不得算作通过；类别未全部检查时 verdict 写 insufficient。\n\n"
            "## 输出契约\n"
            "只输出一个 JSON 对象：\n"
            '{"completeness":[{"category","covered","missing":[],"evidence","clause_ref"}],'
            '"covered_categories":[...],"configured_categories":[...],'
            '"standard_id":"...","verdict":"checked|insufficient"}\n'
            "- clause_ref 必须取自上方清单，不得编造条款号。\n"
        ) % (len(comps), comp_lines),
    }

    s3 = {
        "name": "Requirement_Conflict_Analysis",
        "skill_type": "prompt",
        "description": "需求条目间冲突检测：语义层判定，输出冲突条目对与证据",
        "version": "v2.0",
        "status": "published",
        "triggers": ["需求冲突分析", "需求一致性检查", "requirement conflict analysis",
                     "检测需求冲突", "需求矛盾检查"],
        "allowed_tools": ["graph_retrieve"],
        "allowed_roles": [],
        "content": (
            "# 需求冲突检测（语义层；确定性层由平台已有 detect_conflicts 承担）\n\n"
            "## 边界（务必遵守）\n"
            "平台的确定性检测只报「同一 subject 上取值/极性互斥」，同义改写、隐含矛盾、"
            "范围交叠由本技能负责。两层结果合并去重后才可下结论。\n"
            "**若确定性层返回空列表，必须同时读 conflict_stats.anchored；anchored==0 时"
            "只能报「未判定」，绝不能写「已检查无冲突」。**\n\n"
            "## 前置（硬门槛）\n"
            "先调用 `graph_retrieve` 召回同域/同主体/同指标的需求条目与标准条款，再判定。\n\n"
            "## 冲突类型枚举（%d 类，可配置）\n%s\n\n"
            "## 严重度\n"
            "high = 导致需求不可实现或相互排斥；medium = 需澄清但可并存；low = 表述或术语瑕疵。\n\n"
            "## 判定流程\n"
            "1. 按类型枚举逐类检查，只报有证据的冲突；信息不足标「待确认」不臆造。\n"
            "2. 每条冲突必须给出双方需求编号、原文证据、冲突描述与修改建议。\n"
            "3. 按严重度降序输出，并复核可追溯性。\n\n"
            "## 输出契约\n"
            "只输出一个 JSON 对象：\n"
            '{"conflicts":[{"conflict_id","type","req_ids":[],"severity",'
            '"description","evidence","clause_ref","suggestion"}],'
            '"layer":"semantic","anchored":<int>,"verdict":"checked|insufficient"}\n'
            "- 每条冲突须带 clause_ref（标准条款出处，取自规则清单）与 evidence（双方需求原文），"
            "无出处一律标 to_verify；不得编造条款号。\n"
            "- 没有证据不得写 conflicts 条目；anchored 为 0 时 verdict 必须是 insufficient。\n"
        ) % (len(conflicts), conflict_lines),
    }
    return [s1, s2, s3]


def build_agents():
    kb = _agent_kb_scope()
    common = ("输出必须是结构化 JSON（见 output_schema）。每条结论须带标准条款出处 clause_ref "
              "与需求原文证据 evidence；无出处或依据不足一律标 to_verify，不得写成已确认事实。")
    return [
        {
            "name": "requirement_rule_check", "display_name": "需求规则校验Agent",
            "description": "按标准规则集（≥10 类）逐条校验需求文本，输出结构化校验结果",
            "system_prompt": (
                "你是需求规范校验专家。依据当前标准档案的规则集，对每条需求逐类判定"
                "模板、语法、句式、标识、量化、可验证、可追溯等类别。\n" + common +
                "\n规则类别来自配置资产，不在本提示词里硬编码；换标准档案即换判据。"),
            "intent_keywords": ["按标准校验需求", "需求语法规则校验", "需求模板校验",
                                "检查需求是否规范", "需求规则校验"],
            "kb_scope": kb, "capabilities": ["需求校验", "规则检查"],
            # ★ 2026-11-14 真机修正：初版 `checks` 只声明 `{"type":"array"}`（无 items），
            #   模型于是自创字段名（把编号写进 category、把判据写进 evidence）⇒
            #   前端 issue 标题退化成 "[-] 需复核"，用户看不出是哪条规则在报错。
            #   items 必须显式声明字段，模型才有确定的写法。
            "output_schema": {"type": "object", "required": ["checks", "verdict"],
                              "properties": {
                                  "checks": {"type": "array", "items": {
                                      "type": "object",
                                      "required": ["rule_id", "verdict"],
                                      "properties": {
                                          "rule_id": {"type": "string",
                                                      "description": "规则编号，取自档案类别清单，如 RC-01"},
                                          "category": {"type": "string"},
                                          "verdict": {"type": "string",
                                                      "enum": ["pass", "fail", "to_verify"]},
                                          "severity": {"type": "string",
                                                       "enum": ["high", "medium", "low"]},
                                          "req_id": {"type": "string"},
                                          "evidence": {"type": "string",
                                                       "description": "需求原文片段"},
                                          "clause_ref": {"type": "array",
                                                         "items": {"type": "string"}},
                                          "suggestion": {"type": "string"}}}},
                                  "covered_categories": {"type": "array",
                                                         "items": {"type": "string"}},
                                  "verdict": {"type": "string"}}},
            "skills": ["requirement_rule_check_guide"],
        },
        {
            "name": "requirement_completeness_check", "display_name": "需求完整性检查Agent",
            "description": "按标准检查需求集完整性（≥5 类），输出按类别组织的结构化结果",
            "system_prompt": (
                "你是需求完整性检查专家。依据当前标准档案的完整性验证类别，逐类检查"
                "需求集是否覆盖齐全、有无 TBD/TBS/TBR、属性与追溯链是否齐备。\n" + common +
                "\n完整性类别来自配置资产，换标准档案即换检查清单。"),
            "intent_keywords": ["需求完整性检查", "检查需求是否完整", "需求缺项检查",
                                "需求完备性检查"],
            "kb_scope": kb, "capabilities": ["完整性检查"],
            # items 显式声明（同上：只给 array 会让模型自创字段名）
            "output_schema": {"type": "object", "required": ["completeness", "verdict"],
                              "properties": {
                                  "completeness": {"type": "array", "items": {
                                      "type": "object",
                                      "required": ["category", "covered"],
                                      "properties": {
                                          "category": {"type": "string",
                                                       "description": "完整性类别编号，如 CC-01"},
                                          "covered": {"type": "boolean"},
                                          "missing": {"type": "array",
                                                      "items": {"type": "string"}},
                                          "evidence": {"type": "string"},
                                          "clause_ref": {"type": "array",
                                                         "items": {"type": "string"}}}}},
                                  "covered_categories": {"type": "array",
                                                         "items": {"type": "string"}},
                                  "verdict": {"type": "string"}}},
            "skills": ["requirement_completeness_guide"],
        },
        {
            "name": "requirement_conflict_check", "display_name": "需求冲突检测Agent",
            "description": "检测需求条目间冲突，确定性筛查 + 语义判定双层合并输出",
            "system_prompt": (
                "你是需求冲突检测专家。第一层用平台既有的确定性检测（claims 锚点上的"
                "取值/极性互斥）兜住最刺眼的矛盾，第二层按配置的冲突类型枚举做语义判定。\n" +
                common + "\n冲突类型枚举来自配置资产；空结果不等于无冲突：必须同时读"
                "conflict_stats.anchored，为 0 时只报「未判定」。"),
            "intent_keywords": ["需求冲突分析", "检测需求冲突", "需求矛盾检查",
                                "需求一致性检查"],
            "kb_scope": kb, "capabilities": ["冲突检测", "一致性检查"],
            # items 显式声明；`anchored` 必须回传（0 时只许报「未判定」）
            "output_schema": {"type": "object", "required": ["conflicts", "verdict"],
                              "properties": {
                                  "conflicts": {"type": "array", "items": {
                                      "type": "object",
                                      "required": ["type", "req_ids"],
                                      "properties": {
                                          "type": {"type": "string",
                                                   "description": "冲突类型编号，如 CT-01"},
                                          "layer": {"type": "string",
                                                    "enum": ["deterministic", "semantic"]},
                                          "req_ids": {"type": "array",
                                                      "items": {"type": "string"}},
                                          "description": {"type": "string"},
                                          "severity": {"type": "string",
                                                       "enum": ["high", "medium", "low"]},
                                          "evidence": {"type": "string"},
                                          "suggestion": {"type": "string"}}}},
                                  "items": {"type": "array",
                                            "description": "被检测的需求条目（确定性层据此锚定取值互斥）",
                                            "items": {
                                                "type": "object",
                                                "required": ["req_id", "text"],
                                                "properties": {
                                                    "req_id": {"type": "string"},
                                                    "text": {"type": "string"},
                                                    "subject": {"type": "string",
                                                                "description": "被约束的对象/指标名，≤12 字，如「自检时间」"},
                                                    "ambiguous": {"type": "boolean"}}}},
                                  "layer": {"type": "string"},
                                  "anchored": {"type": "integer",
                                               "description": "上一层实际可比的 claims 锚点数"},
                                  "verdict": {"type": "string"}}},
            "skills": ["Requirement_Conflict_Analysis"],
        },
    ]


def install_db(catalog, dry=False):
    skills = build_skills(catalog)
    agents = build_agents()
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    stats = {"skills": 0, "agents": 0, "bindings": 0, "rules": 0,
             "intents": 0, "hil": 0, "profile": 0}
    try:
        if dry:
            print("[dry-run] 将写入 %d Skill / %d Agent" % (len(skills), len(agents)))
            return stats, skills, agents
        # ── Skills ──
        for s in skills:
            existing = con.execute("SELECT id FROM skills WHERE name=?", (s["name"],)).fetchone()
            # ★ 2026-11-14：frontmatter 里**不写** must_retrieve / required_docs。
            #   两者全仓零消费点（纯尸体配置），而且都会让「拆多套」变假：
            #   required_docs 写死两篇 ⇒ 切到 incose_only_v1 仍会带上 29148。
            #   文档白名单的唯一来源 = 标准档案 profiles[*].docs。
            fm = j(_skill_frontmatter(s))
            vals = dict(
                name=s["name"], skill_type=s["skill_type"], description=s["description"],
                version=s["version"], status=s["status"], frontmatter=fm,
                content=s["content"], triggers=j(s["triggers"]), category="AI建模",
                allowed_tools=j(s["allowed_tools"]), allowed_roles=j(s["allowed_roles"]),
                enabled=1, scope="global", source="manual", builtin=0,
                dependencies="[]", references="[]", examples="[]", scripts="[]",
                use_stats="{}", feedback_notes="[]", pinned=0,
                updated_at="CURRENT_TIMESTAMP")
            if existing:
                sets = ",".join("%s=%s" % (qcol(k), "CURRENT_TIMESTAMP" if v == "CURRENT_TIMESTAMP"
                                            else "?") for k, v in vals.items() if k != "name")
                params = [v for k, v in vals.items() if k != "name" and v != "CURRENT_TIMESTAMP"]
                con.execute("UPDATE skills SET %s WHERE name=?" % sets, params + [s["name"]])
            else:
                ks = list(vals.keys())
                ph = ",".join("CURRENT_TIMESTAMP" if vals[k] == "CURRENT_TIMESTAMP" else "?"
                              for k in ks)
                con.execute("INSERT INTO skills (%s) VALUES (%s)" %
                            (",".join(qcol(k) for k in ks), ph),
                            [v for k, v in vals.items() if v != "CURRENT_TIMESTAMP"])
            stats["skills"] += 1

        # ── Agents ──
        for a in agents:
            existing = con.execute("SELECT id FROM agents WHERE name=?", (a["name"],)).fetchone()
            vals = dict(
                name=a["name"], display_name=a["display_name"], description=a["description"],
                system_prompt=a["system_prompt"], hil_level="L1", kb_required=1,
                intent_keywords=j(a["intent_keywords"]), status="active", version="v1",
                kb_scope=j(a["kb_scope"]), capabilities=j(a["capabilities"]),
                input_schema=j({"type": "object", "required": ["items"],
                                "properties": {"items": {"type": "array"}}}),
                output_schema=j(a["output_schema"]), max_concurrency=2,
                model_params="{}", protocol_range=">=1,<3", builtin=0,
                agent_role="sub", task_domain="modeling", intent_keywords_active=0,
                is_code_stage=0, is_orch_default=0)
            if existing:
                sets = ",".join("%s=?" % qcol(k) for k in vals if k != "name")
                con.execute("UPDATE agents SET %s WHERE name=?" % sets,
                            [v for k, v in vals.items() if k != "name"] + [a["name"]])
                aid = existing["id"]
            else:
                ks = list(vals.keys())
                cur = con.execute("INSERT INTO agents (%s) VALUES (%s)" %
                                  (",".join(qcol(k) for k in ks),
                                   ",".join("?" for _ in ks)), list(vals.values()))
                aid = cur.lastrowid
            stats["agents"] += 1
            # 绑定（skills + graph_retrieve）
            binds = [("skill", sk) for sk in a["skills"]] + [("tool", "graph_retrieve")]
            for tt, tn in binds:
                if not con.execute("SELECT 1 FROM agent_tools WHERE agent_id=? AND tool_type=? "
                                   "AND tool_name=?", (aid, tt, tn)).fetchone():
                    con.execute("INSERT INTO agent_tools (agent_id,tool_type,tool_name,enabled) "
                                "VALUES (?,?,?,1)", (aid, tt, tn))
                    stats["bindings"] += 1

        # ── 标准档案（generate_rules，纯配置）──
        cfg = {
            # ★ 2026-11-14：**多套标准档案**（米爸要求「拆多套」）。
            #   结构：顶层放公共字段（catalog_path/skills/agents），`profiles` 放各套差异，
            #   `active_profile` 指向当前生效那套。切换 = 改这一个字符串（见
            #   `services.requirement_checks.switch_profile` / `tools/switch_requirement_profile.py`）。
            #   为什么要多套而不是一个"docs 列表"：不同标准的**判据集**并不相同
            #   （29148 讲 SRS 特性、INCOSE Guide 讲 R01-R42 写作规则），
            #   档案必须能同时表达"用哪些文档"和"用哪些 Skill"，否则换标准只换了检索范围、
            #   判据仍是旧那套 ⇒ 假切换。
            "requirement_standard_profile": j({
                "active_profile": "iso29148_incose_v1",
                "catalog_path": "data/requirement_standards/rule_catalog.json",
                "skills": ["requirement_rule_check_guide", "requirement_completeness_guide",
                           "Requirement_Conflict_Analysis"],
                "agents": [a["name"] for a in agents],
                "profiles": {
                    "iso29148_incose_v1": {
                        "standard_ids": ["ISO/IEC/IEEE 29148:2011", "INCOSE Guide v4"],
                        "docs": STANDARD_DOCS,
                        "description": "默认档案：29148-2011 的 SRS 特性 + INCOSE Guide 的写作规则"
                                       "（两套互为补充，覆盖面最广）",
                    },
                    "iso29148_only_v1": {
                        "standard_ids": ["ISO/IEC/IEEE 29148:2011"],
                        "docs": [STANDARD_DOCS[1]],
                        "description": "仅 ISO 29148-2011：只按 SRS 特性判，不引入 INCOSE 写作规则",
                    },
                    "incose_only_v1": {
                        "standard_ids": ["INCOSE Guide v4"],
                        "docs": [STANDARD_DOCS[0]],
                        "description": "仅 INCOSE Guide v4：只按 R01-R42 写作规则判，适合先做文本规范收敛",
                    },
                },
                "description": "多套标准档案；换标准 = 改 active_profile 一个字符串",
            }),
            "requirement_rule_categories": j([c["id"] for c in catalog["rule_categories"]]),
            "requirement_completeness_categories": j(
                [c["id"] for c in catalog["completeness_categories"]]),
            "requirement_conflict_types": j([c["id"] for c in catalog["conflict_types"]]),
                                }
        # ★ 2026-11-14：重装**不得静默重置用户已切换的档案**。
        #   安装器每次都写整份 JSON；若原样覆盖 `active_profile`，
        #   客户按需切到 incose_only_v1 之后，谁再跑一次安装器就被打回默认 ——
        #   而"安装输出看起来完全正常"，没人会想到配置被悄悄重置
        #   （最隐蔽的那类回归：命令成功、行为变了、无任何痕迹）。
        #   ⇒ 读旧值：旧 active_profile 若在新 profiles 里仍存在，就沿用它。
        _old_active = ""
        try:
            _row = con.execute("SELECT rule_value FROM generate_rules WHERE rule_key=?",
                               ("requirement_standard_profile",)).fetchone()
            if _row and _row[0]:
                _old = json.loads(_row[0]) if isinstance(_row[0], str) else dict(_row[0])
                if isinstance(_old, dict):
                    _old_active = str(_old.get("active_profile") or "")
        except Exception:
            _old_active = ""
        _cfg_obj = json.loads(cfg["requirement_standard_profile"])
        if _old_active and _old_active in (_cfg_obj.get("profiles") or {}):
            _cfg_obj["active_profile"] = _old_active
            cfg["requirement_standard_profile"] = j(_cfg_obj)
            print("沿用已有生效档案：%s（安装器不重置用户选择）" % _old_active)
        for k, v in cfg.items():
            if con.execute("SELECT 1 FROM generate_rules WHERE rule_key=?", (k,)).fetchone():
                con.execute("UPDATE generate_rules SET rule_value=? WHERE rule_key=?", (v, k))
            else:
                con.execute("INSERT INTO generate_rules (rule_key,rule_value,description) "
                            "VALUES (?,?,?)", (k, v, "需求配置化校验（2026-11-14）"))
            stats["rules"] += 1

        # ── 意图触发词 ──
        intent_map = [
            ("按标准校验需求", "requirement_rule_check"),
            ("需求完整性检查", "requirement_completeness_check"),
            ("需求冲突分析", "requirement_conflict_check"),
        ]
        for trig, intent_name in intent_map:
            if not con.execute("SELECT 1 FROM intent_rules WHERE trigger=?", (trig,)).fetchone():
                con.execute("INSERT INTO intent_rules (trigger,intent,weight,enabled,created_by) "
                            "VALUES (?,?,?,1,?)", (trig, intent_name, 80, "system"))
                stats["intents"] += 1

        # ── HIL 策略：高风险需求冲突需人工确认（复用现有 decision_point）──
        if not con.execute("SELECT 1 FROM hil_policies WHERE match_key=?",
                           ("requirement_conflict",)).fetchone():
            con.execute(
                "INSERT INTO hil_policies (scope,match_key,decision_point,level,conditions_json,"
                "reason,timeout_min,fallback_level,enabled,priority,created_by) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                ("global", "requirement_conflict", "model_conflict", "confirm", "{}",
                 "需求条目间检出 high 严重度冲突时挂人工确认，不自动消解",
                 0, "confirm", 1, 55, "system"))
            stats["hil"] += 1

        # ── 项目绑定标准档案 ──
        proj = con.execute("SELECT id FROM projects ORDER BY id LIMIT 1").fetchone()
        if proj and not con.execute("SELECT 1 FROM project_methodology WHERE project_id=?",
                                    (proj["id"],)).fetchone():
            con.execute("INSERT INTO project_methodology (project_id,profile_id,is_active) "
                        "VALUES (?,?,1)", (proj["id"], "iso29148_incose_v1"))
            stats["profile"] += 1

        con.commit()
    finally:
        con.close()
    return stats, skills, agents


def sync_snapshot(skills, agents, dry=False):
    """把新增资产同步进启动快照（幂等，按 name 替换）。"""
    if dry:
        print("[dry-run] 将同步快照 skills=%d agents=%d" % (len(skills), len(agents)))
        return
    shutil.copy(SNAP, SNAP + ".bak_reqcfg_20261114")
    d = json.load(open(SNAP, encoding="utf-8"))
    cat = json.load(open(CATALOG, encoding="utf-8"))
    all_skills = {**{s["name"]: s for s in build_skills(cat)},
                  **{s["name"]: s for s in []}}

    def snap_skill(s):
        return {
            "name": s["name"], "skill_type": s["skill_type"], "description": s["description"],
            "prompt_id": None, "tool_name": "", "version": s["version"],
            "status": s["status"],
            "frontmatter": j(_skill_frontmatter(s)),
            "content": s["content"], "triggers": j(s["triggers"]), "category": "AI建模",
            "package_path": "", "source": "manual", "dependencies": "[]",
            "allowed_roles": "[]", "allowed_tools": j(s["allowed_tools"]),
            "references": "[]", "examples": "[]", "scripts": "[]",
            "enabled": 1, "builtin": 0, "scope": "global", "source_ref": "",
            "plugin_id": "", "share_status": "", "origin": "",
        }

    for s in skills:
        d["tables"]["skills"] = [x for x in d["tables"]["skills"] if x.get("name") != s["name"]]
        d["tables"]["skills"].append(snap_skill(s))

    kb = _agent_kb_scope()
    for a in agents:
        row = {
            "name": a["name"], "display_name": a["display_name"], "description": a["description"],
            "system_prompt": a["system_prompt"], "model_provider_id": None,
            "model_params": "{}", "hil_level": "L1", "kb_required": 1,
            "intent_keywords": j(a["intent_keywords"]), "icon": "🤖", "version": "v1",
            "kb_scope": j(kb), "capabilities": j(a["capabilities"]),
            "input_schema": j({"type": "object", "required": ["items"],
                               "properties": {"items": {"type": "array"}}}),
            "output_schema": j(a["output_schema"]), "max_concurrency": 2,
            "protocol_range": ">=1,<3", "builtin": 0, "agent_role": "sub",
            "task_domain": "modeling", "intent_keywords_active": 0, "stage_order": None,
            "is_code_stage": 0, "is_orch_default": 0, "orch_order": None,
        }
        d["tables"]["agents"] = [x for x in d["tables"]["agents"] if x.get("name") != a["name"]]
        d["tables"]["agents"].append(row)
        # 关系快照
        for sk in a["skills"]:
            rel = {"agent": a["name"], "tool_type": "skill", "tool_name": sk, "enabled": 1}
            if rel not in d["relations"]["agent_tools"]:
                d["relations"]["agent_tools"].append(rel)
        rel = {"agent": a["name"], "tool_type": "tool", "tool_name": "graph_retrieve",
               "enabled": 1}
        if rel not in d["relations"]["agent_tools"]:
            d["relations"]["agent_tools"].append(rel)

    with open(SNAP, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print("快照已同步（备份 .bak_reqcfg_20261114）")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    cat = json.load(open(CATALOG, encoding="utf-8"))
    print("规则清单：%d 类规则 / %d 类完整性 / %d 类冲突" %
          (len(cat["rule_categories"]), len(cat["completeness_categories"]),
           len(cat["conflict_types"])))
    stats, skills, agents = install_db(cat, dry=args.dry_run)
    print("DB 写入：", stats)
    if not args.dry_run:
        sync_snapshot(skills, agents)
    print("完成")


if __name__ == "__main__":
    sys.exit(main())
