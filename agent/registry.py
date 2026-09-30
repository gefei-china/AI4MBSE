"""Agent 注册表（意图 → Agent 定义）：DB agents 表 → 内置 DEFINITIONS 兜底。"""
import os
import re
import json
import time
import uuid
from database import get_db, db_conn
from knowledge_engine import VectorEngine, QueryRouter  # P1: 双引擎底座
from llm import llm_client
from core.config import STATIC_DIR
from file_tools import exec_file_tool, FILE_TOOL_NAMES as _FILE_TOOL_NAMES  # 基础通用文件操作工具
from report_tools import exec_report_tool, REPORT_TOOL_NAMES as _REPORT_TOOL_NAMES  # 基础通用报告导出工具
from .definition import AgentDefinition

# Task 4：旧固定编排池常量（与 AgentPipeline._ORCH_AGENTS 保持一致；字符串常量避免循环导入）
_FALLBACK_ORCH_AGENTS = "requirement_analysis,design,impact,review,report_generation,knowledge_qa"

# Task 4：能力枚举词表（供 Task 12 capabilities DB 层校验使用；本次仅定义常量）
_CAPABILITIES_ENUM = {
    "需求拆解", "需求分析", "需求条目化", "需求追踪", "追溯", "冲突检测",
    "架构设计", "方案设计", "系统建模", "工程建模", "模型校验", "模型质量",
    "影响评估", "变更影响", "预评审", "质量校验", "质量评审", "报告生成",
    "知识问答", "文件操作", "数据导入", "任务规划", "多Agent编排",
}


def _parse_json_array(v) -> list:
    """容错解析 JSON 数组字符串（capabilities 等）；解析失败返回空列表。

    兼容：AgentRepo.list_agents/get_agent 已对 capabilities/input_schema/output_schema
    做 json.loads（Task 14 后端字段解析），此处入参可能已是 list → 直接返回副本。
    """
    if isinstance(v, (list, tuple, set)):
        return [x for x in v]
    try:
        r = json.loads(v or "[]")
        return r if isinstance(r, list) else []
    except Exception:
        return []


def _parse_json_obj(v) -> dict:
    """容错解析 JSON 对象字符串（input_schema/output_schema 等）；解析失败返回空 dict。

    兼容：AgentRepo 已解析为 dict 时直接返回副本（Task 14 后端字段解析对齐）。
    """
    if isinstance(v, dict):
        return v
    try:
        r = json.loads(v or "{}")
        return r if isinstance(r, dict) else {}
    except Exception:
        return {}


# ── Task 12：能力元数据注入清洗（安全治理；纯函数，供 routers/studio.py 保存接口入库前调用）──
def sanitize_description(desc, max_len=200) -> str:
    """描述注入清洗：截断 ≤max_len、去控制字符、去脚本标记，返回清洗后文本。

    capabilities/description 会拼进 Planner prompt，脏数据可劫持规划器（提示注入）。
    清洗规则：控制字符（\\x00-\\x1f/\\x7f）移除；`<script>`/`</script>`（容忍空白与闭合
    标记变体）与 `javascript:` 协议标记移除；反引号与花括号（`{{`/`${` 模板注入）移除。
    合法文本原样保留（幂等无害），仅长度超限时截断。
    """
    if desc is None:
        return ""
    s = str(desc)
    s = re.sub(r"[\x00-\x1f\x7f]", "", s)                             # 控制字符
    s = re.sub(r"<\s*/?\s*script\s*>?", "", s, flags=re.IGNORECASE)   # <script> / </script>
    s = re.sub(r"javascript\s*:", "", s, flags=re.IGNORECASE)         # javascript: 协议注入
    s = s.replace("`", "").replace("{", "")                           # 反引号 / 花括号（{{ 与 ${ 一并去除）
    return s[:max_len]


def sanitize_capabilities(raw_caps, allow_extra=False) -> list:
    """能力枚举词表白名单清洗（防注入：能力标签拼入 Planner prompt）。

    - raw_caps：list / tuple / set 或 JSON 数组字符串；解析失败按 [] 容错
    - 每项 strip 后：allow_extra=False（严格）仅保留 capabilities_enum() 集合内；
      allow_extra=True（宽松）白名单项原样保留，自定义项经注入清洗（sanitize_description）
      后长度 ≤12 才放行（允许自定义短词但清洗注入标记，超长剔除）
    - 去重（保序）+ 限 10 项
    """
    if isinstance(raw_caps, str):
        caps = _parse_json_array(raw_caps)
    elif isinstance(raw_caps, (list, tuple, set)):
        caps = list(raw_caps)
    else:
        caps = []
    enum = AgentRegistry.capabilities_enum()
    out = []
    for c in caps:
        if not isinstance(c, str):
            c = str(c)
        c = c.strip()
        if not c:
            continue
        if c in enum:
            keep = c
        elif allow_extra:
            cleaned = sanitize_description(c)  # 注入标记清洗（不截断）
            keep = cleaned if (cleaned and len(cleaned) <= 12) else None
        else:
            continue
        if keep and keep not in out:
            out.append(keep)
        if len(out) >= 10:
            break
    return out


class AgentRegistry:
    """Agent 注册表（P0-4 → P0 平台化）：意图 → Agent 定义。

    数据源优先级：DB agents 表（status='active'，可 CRUD 配置）→ 内置 DEFINITIONS 兜底。
    - 会话主入口运行时：execute/execute_stream 内通过 load_from_db() 加载当前库配置，
      实现「新建/编辑 Agent = 改数据不改代码」。
    - 兼容性：DB seed 与内置 DEFINITIONS 完全等价，未配置 DB 时行为不变。
    """

    DEFINITIONS = {
        "requirement_analysis": AgentDefinition(
            name="需求分析Agent",
            description="解析输入文本，提取条目化需求并标注来源追溯",
            tools=["graph_retrieve", "entity_create", "conflict_check"],
            hil_level="L2", kb_required=True,
            system_prompt=(
                "你是网络总体MBSE设计助手，担任需求分析专家。\n"
                "职责：从用户输入/任务书/上传资料中提取条目化需求，区分功能、性能、接口、约束类需求，"
                "为每条需求标注来源追溯，输出结构化需求清单（内部编号仅用于追溯，正文一律用自然语言陈述）。\n"
                "行为准则：不臆造需求、不遗漏明确约束；信息歧义时按领域常识作合理假设并显式标注；"
                "优先依据用户上传资料与检索到的互联数据；需求条目需可验证、无重复、无冲突。\n"
                "视图输出：若任务要求生成需求图/模块定义图（BDD）等视图，请在回答末尾输出 SysML v2 代码块"
                "（```sysml 包裹：requirement 元素块 + 模块 block 定义与关系），供系统自动投影视图。\n"
                "建模独立性：SysML v2 代码的元素（包/需求/部件/端口/关系）按用户需求与领域最佳实践完整生成；"
                "知识库检索实体仅作参考对齐，不构成元素数量上限——检索为空或命中少时不得缩减模型，按工程完整性补齐。")),
        "requirement_quality": AgentDefinition(
            name="需求质量评审Agent",
            description="扫描需求文本，标记模糊词/缺验收标准/不可验证项，输出质量改进建议",
            tools=["graph_retrieve"],
            hil_level="L0", kb_required=False,
            system_prompt=(
                "你是网络总体MBSE设计助手，担任需求质量评审专家（对齐 INCOSE 需求质量准则）。\n"
                "职责：对需求文本做质量分析——模糊不可验证词、缺少量化验收标准、需求自相矛盾、"
                "需求粒度不均、可追溯性缺失；输出问题清单（问题/严重级别/原文证据/可执行修改建议）"
                "与总体质量评分。\n"
                "行为准则：只指出可验证的问题、不空泛批评；每条问题给出具体改法（如把「快速」改为「≤500ms」）；"
                "区分规范违反与设计建议。")),
        "design": AgentDefinition(
            name="方案设计Agent",
            description="基于需求与互联数据生成多方案架构设计",
            tools=["graph_retrieve", "entity_create"],
            hil_level="L1", kb_required=True,
            system_prompt=(
                "你是网络总体MBSE设计助手，担任系统架构设计专家。\n"
                "职责：基于需求清单与知识库互联数据生成多方案系统架构设计，从性能、成本、风险、可扩展性"
                "等维度权衡比较，给出推荐方案与理由。\n"
                "行为准则：每个方案说明适用范围与代价；方案要素明确映射到对应需求；"
                "输出应包含系统组成、接口关系、关键设计权衡；不堆砌空泛架构名词，每个设计决策给出依据。\n"
                "视图输出（按需）：当用户要求生成建模代码/视图时，在回答末尾用 ```sysml 代码块输出对应的 "
                "SysML v2 建模代码（```sysml 包裹：part def/requirement def 类型定义 + part/requirement 使用实例 + "
                "satisfy（需求被部件满足）/connector（部件连接）关系），供系统自动投影视图并入库版本管理；"
                "纯问答/分析场景不必输出代码，按用户实际诉求决定。\n"
                "建模独立性：SysML v2 代码的元素（包/部件/端口/连接）按用户需求与领域最佳实践完整生成；"
                "知识库检索实体仅作参考对齐，不构成元素数量上限——检索为空或命中少时不得缩减模型，按工程完整性补齐。")),
        "impact": AgentDefinition(
            name="变更影响Agent",
            description="分析变更源对模型的影响范围（BFS 遍历产出影响图）",
            tools=["graph_retrieve", "impact_analyze"],
            hil_level="L1",
            system_prompt=(
                "你是网络总体MBSE设计助手，担任变更影响分析专家。\n"
                "职责：分析变更源对现有模型的影响范围，沿关系链识别直接与间接影响，评估风险等级，"
                "输出影响清单（受影响元素、影响类型、连锁影响、建议动作）。\n"
                "行为准则：按图遍历逐级展开，不遗漏下游影响；明确区分直接影响与传递影响；"
                "每个影响项给出风险等级与处置建议。")),
        "review": AgentDefinition(
            name="预评审Agent",
            description="对当前模型做规范性/一致性/合理性校验",
            tools=["graph_retrieve", "validate"],
            hil_level="L1",
            system_prompt=(
                "你是网络总体MBSE设计助手，担任预评审专家。\n"
                "职责：对模型、需求、方案做规范性、一致性、合理性校验，对照本体类型与关系约束，"
                "输出问题清单（问题描述、严重级别、修复建议）。\n"
                "行为准则：只指出可验证的问题、不空泛批评；问题按严重级别排序；"
                "每条问题给出可执行的修复建议；区分规范违反与设计建议。")),
        "report_generation": AgentDefinition(
            name="报告生成Agent",
            description="基于模型与检索结果生成结构化分析报告",
            tools=["graph_retrieve"],
            hil_level="L0", kb_required=True,
            system_prompt=(
                "你是网络总体MBSE设计助手，担任技术报告撰写专家。\n"
                "职责：基于模型数据与检索结果生成结构化专业报告，分节组织"
                "（概述→现状→分析→结论→建议），结论基于事实与数据，给出可执行建议。\n"
                "行为准则：语言专业精炼；引用数据标注来源（🧬知识库/📎上传资料）；"
                "结论先行、论据支撑；报告自成体系，可直接用于评审。")),
        "knowledge_qa": AgentDefinition(
            name="知识问答Agent",
            description="基于知识库图谱与向量双引擎回答领域问题",
            tools=["graph_retrieve"],
            hil_level="L0", kb_required=True,
            system_prompt=(
                "你是网络总体MBSE设计助手，担任知识库问答专家。\n"
                "职责：基于知识库图谱与向量双引擎的检索结果回答领域问题，回答中引用来源"
                "（🧬知识库/📎上传资料），明确区分事实与推断。\n"
                "行为准则：只回答检索结果与上传资料支持的内容；资料未覆盖时明确说明，"
                "再给出基于专业知识的建议；不编造知识库不存在的条目。")),
        "chat": AgentDefinition(
            name="通用问答Agent",
            description="纯问答直出，不打断用户（HIL L0）",
            tools=[], hil_level="L0",
            system_prompt=(
                "你是网络总体MBSE设计助手，担任通用对话助理。\n"
                "职责：简洁、准确地回答用户问题；闲聊场景保持友好自然。\n"
                "行为准则：回答控制在必要篇幅内；不确定时明说；不主动执行建模动作；"
                "不编造信息。")),
    }

    def __init__(self):
        self._db_defs: dict = {}          # name(intent) → AgentDefinition（DB 加载）
        self._db_meta: dict = {}          # name → 扩展元数据（display_name/icon/intent_keywords）

    def load_from_db(self, conn, user=None) -> None:
        """从 DB agents 表加载可配置 Agent（status='active'），DB 覆盖同名内置。

        P1: 同时缓存绑定工具元数据（skills 触发词/指令正文、MCP 端点/工具列表），
        供 execute 按 Skill triggers 渐进注入指令。

        P1-6「安装即可消费」（2026-09-16）：有插件映射但未安装/已停用/已下架的 Agent
        不进入意图路由池。无映射的原生 Agent 不受影响（旧体系自带能力不归能力中心管）。

        P1-8 用户隔离（2026-09-17）：原用 any_user=True 全局放宽，导致「有作者的个人能力」
        对所有人可用 —— 他人私有的 Agent 会进入你的路由池。现改为按传入 user 判定：
          · 平台内置（author_id=0）/ 本人自建 / 本人已安装 → 进入
          · 他人私有的能力 → 不进入
        user=None 时退化为"仅系统级安装"，等价于仅内置能力 —— 安全默认。
        """
        from repositories.agent_repo import AgentRepo  # 延迟导入避免循环
        repo = AgentRepo(conn)
        self._db_defs = {}
        self._db_meta = {}
        try:
            from plugin_system import store as _pstore_a
            _keep_agent = _pstore_a.consumable_filter(conn, user)
        except Exception:
            _keep_agent = None
        for a in repo.list_agents():
            if a.get("status") != "active":
                continue
            if _keep_agent is not None and not _keep_agent("agents", a.get("id")):
                continue
            name = a["name"]
            bound = repo.bound_tools_for(a["id"])
            # 2026-09-30（用户第 10 轮指令）：**移除 Agent 绑定插件机制**。
            # 此处原有 P0-5 的 plugin→skill/mcp 展开逻辑（把 agent_tools.tool_type='plugin'
            # 按 plugin_id 展开为可触发的技能/MCP），已整体删除。原因（实测证据，勿凭印象恢复）：
            #   · skill 型插件：本来就有 `_global_skill_pool`（agent/pipeline_parts/skills.py:11-50）
            #     把所有**已安装**的 skill/bundle 型插件无条件纳入候选池 → 绑定完全是冗余的。
            #     实证：剔除 plugin 展开件后重跑，技能「文件操作」仍被正常触发（走全局池）。
            #   · mcp 型插件：绑定曾是它进入 Agent 的通道，但用户判定该概念三重含义
            #     （插件/skill/mcp）混淆，要求彻底移除，MCP 统一走 `绑定 MCP 服务器` 面板。
            # 概念收敛后：Agent 可绑能力 = 技能 / MCP / 工具 三类，与面板一一对应。
            # ⚠️ 注意：本文件 :337/:349 的 `source: "plugin"` 是**插件来源的 Agent** 标记
            #    （P1-6「安装即可消费」），与本处被移除的「Agent 绑定插件」是两回事，勿一并删除。
            _sc = a.get("kb_scope") or {}
            # SP-O：DB system_prompt 自定义优先；为空时继承内置同名 Agent 的角色化提示词
            _builtin = AgentRegistry.DEFINITIONS.get(name)
            _sp = (a.get("system_prompt") or "").strip() or (_builtin.system_prompt if _builtin else "")
            self._db_defs[name] = AgentDefinition(
                name=a.get("display_name") or name,
                description=a.get("description", ""),
                tools=[t["tool_name"] for t in a.get("tools", []) if t.get("enabled", 1)],
                hil_level=a.get("hil_level") or "L0",
                kb_required=bool(a.get("kb_required")),
                kb_scope=_sc if isinstance(_sc, dict) else json.loads(_sc or "{}"),
                system_prompt=_sp,
                model_hint="",
                agent_role=a.get("agent_role") or "sub",
                intent_name=name,
            )
            self._db_meta[name] = {
                "display_name": a.get("display_name") or name,
                "icon": a.get("icon", "🤖"),
                "intent_keywords": a.get("intent_keywords", []),
                "system_prompt": a.get("system_prompt", ""),
                "model_provider_id": a.get("model_provider_id"),  # 优化2：Agent 指定 LLM provider（无则全局默认）
                "bound_tools": bound,     # P1: 绑定工具元数据（skill/mcp/tool 全量）
                # Task 4：能力元数据（供 discover 动态筛选 / 版本协议校验 / Task 5/6 编排池与智能分派）
                "capabilities": _parse_json_array(a.get("capabilities")),   # 专长标签 JSON 数组
                "input_schema": _parse_json_obj(a.get("input_schema")),     # 输入 JSON Schema
                "output_schema": _parse_json_obj(a.get("output_schema")),   # 输出 JSON Schema
                "max_concurrency": int(a.get("max_concurrency") or 2),      # 最大并发任务数（默认 2）
                "version": a.get("version") or "",                          # 语义版本
                "protocol_range": a.get("protocol_range") or "",            # A2A 协议版本范围
            }
        # ── P1-6「安装即可消费」：纯插件 Agent（无旧表承载）同样可被消费 ──
        # 前提：manifest 提供 system_prompt（否则 entry 返回 None，被跳过）。
        # 意图关键词取自 manifest.intent_keywords；缺省则该 Agent 只能被显式指定，
        # 不参与语义路由 —— 不臆造关键词，那只会污染意图分类。
        try:
            from plugin_system import store as _pstore_ap
            for _pid in _pstore_ap.plugin_ids_of_types(conn, "agent", user):
                _ae = _pstore_ap.agent_entry_from_plugin(conn, _pid)
                if not _ae or _ae["name"] in self._db_defs:
                    continue      # 旧表已承载同名 Agent → 不覆盖（旧表配置更完整）
                _an = _ae["name"]
                self._db_defs[_an] = AgentDefinition(
                    name=_ae.get("display_name") or _an,
                    description=_ae.get("description", ""),
                    tools=list(_ae.get("tools") or []),
                    hil_level="L0",
                    kb_required=False,
                    kb_scope={},
                    system_prompt=_ae.get("system_prompt") or "",
                    model_hint="",
                    agent_role=_ae.get("agent_role") or "sub",
                    intent_name=_an,
                )
                self._db_meta[_an] = {
                    "display_name": _ae.get("display_name") or _an,
                    "icon": "🤖",
                    "intent_keywords": _ae.get("intent_keywords") or [],
                    "system_prompt": _ae.get("system_prompt") or "",
                    "model_provider_id": None,
                    "bound_tools": [],
                    "capabilities": [],
                    "input_schema": {}, "output_schema": {},
                    "max_concurrency": 2, "version": "", "protocol_range": "",
                    "source": "plugin",
                    "plugin_id": _pid,
                }
        except Exception:
            pass

    def has_intent(self, name: str) -> bool:
        """该意图名当前是否可用（DB/插件已加载，或内置兜底存在）。"""
        return name in self._db_defs or name in self.DEFINITIONS

    def plugin_agents(self) -> dict:
        """来自插件、且无旧表承载的 Agent 元数据（供路由关键词注册）。"""
        return {k: v for k, v in self._db_meta.items() if v.get("source") == "plugin"}

    def get_provider_id(self, intent: str):
        """优化2：返回 Agent 指定 model_provider_id（None=用全局默认 provider）。"""
        return self._db_meta.get(intent, {}).get("model_provider_id")

    def get_bound_skills(self, intent: str) -> list:
        """P1: Agent 绑定的 skill 列表（含 triggers/content），供触发路由。"""
        return [t for t in self._db_meta.get(intent, {}).get("bound_tools", [])
                if t.get("type") == "skill" and t.get("content")]

    def get_bound_mcp(self, intent: str) -> list:
        """P1: Agent 绑定的 MCP 工具（含端点/工具列表），供渐进式发现注入。"""
        return [t for t in self._db_meta.get(intent, {}).get("bound_tools", [])
                if t.get("type") == "mcp"]

    def get(self, intent: str) -> AgentDefinition:
        return self._db_defs.get(intent) or self.DEFINITIONS.get(intent, self.DEFINITIONS["chat"])

    def get_meta(self, intent: str) -> dict:
        """返回 DB 加载 Agent 的扩展元数据（含 Task 4 能力元数据字段）；未加载返回 {}。"""
        return self._db_meta.get(intent, {})

    def discover(self, conn, domain: str = "", limit: int = 50) -> list:
        """Task 4：能力元数据动态筛选（供编排池动态化 / 智能分派消费）。

        遍历 _db_defs（DB 已加载的 enabled Agent，即 load_from_db 后）：
        - domain 非空时：capabilities 与 domain 有交集（互相包含）或 intent 名含 domain（宽松匹配）
        - 为每个候选附加运行时统计：
          current_load：agent_tasks 中该 agent 处于 running/ready 的任务数（按 agent_id=intent 名匹配）
          success_rate：done/(done+failed)，无数据返回 None
          max_concurrency / version：来自能力元数据
        - 统计查询前不假设表/列存在（try/except 容错，失败返回 0/None 不抛错）
        - 返回 [{name(显示名), intent(key), capabilities, current_load, max_concurrency,
                success_rate, version, protocol_range, description}]，按 DB 注册序稳定排序，limit 截断
        """
        domain = (domain or "").strip()
        out = []
        for intent, defn in self._db_defs.items():
            meta = self._db_meta.get(intent, {})
            caps = [str(c) for c in (meta.get("capabilities") or [])]
            if domain:
                if not (any(domain in c or c in domain for c in caps) or domain in intent):
                    continue
            # 运行时统计（容错：agent_tasks 表/列不存在或查询失败 → 0/None，不抛错）
            try:
                load = conn.execute(
                    "SELECT COUNT(*) FROM agent_tasks WHERE agent_id=? AND status IN ('running','ready')",
                    (intent,)).fetchone()[0]
            except Exception:
                load = 0
            try:
                done = conn.execute(
                    "SELECT COUNT(*) FROM agent_tasks WHERE agent_id=? AND status='done'",
                    (intent,)).fetchone()[0]
                failed = conn.execute(
                    "SELECT COUNT(*) FROM agent_tasks WHERE agent_id=? AND status='failed'",
                    (intent,)).fetchone()[0]
                success_rate = (done / (done + failed)) if (done + failed) > 0 else None
            except Exception:
                success_rate = None
            out.append({
                "name": meta.get("display_name") or defn.name,
                "intent": intent,
                "capabilities": meta.get("capabilities") or [],
                "current_load": load,
                "max_concurrency": meta.get("max_concurrency", 2),
                "success_rate": success_rate,
                "version": meta.get("version", ""),
                "protocol_range": meta.get("protocol_range", ""),
                "description": defn.description,
            })
            if len(out) >= limit:
                break
        return out

    def protocol_supported(self, range_or_intent: str, schema_version: float = 1) -> bool:
        """Task 4：校验 Agent 声明 protocol_range（如 '>=1,<3'）是否覆盖 schema_version。

        - 入参为已加载 Agent 的意图名时读取其 _db_meta.protocol_range；否则视为原始区间串
        - 解析简单版本区间（逗号分隔的 >=/<=/>/</= 子句）；解析失败返回 True 宽松放行
        """
        if not range_or_intent:
            return True
        raw = range_or_intent
        if range_or_intent in self._db_meta:
            raw = (self._db_meta.get(range_or_intent) or {}).get("protocol_range") or ""
        if not raw:
            return True
        return self._version_range_covers(raw, schema_version)

    @staticmethod
    def _version_cmp(a, b) -> int:
        """版本号比较：'1'/'1.0.0' 视为等价，按段数值比较；任一侧解析失败返回 0（宽松放行）。"""
        def _nums(v):
            nums = []
            for p in str(v).strip().lstrip("vV").split("."):
                try:
                    nums.append(int(p))
                except Exception:
                    break
            return tuple(nums)
        na, nb = _nums(a), _nums(b)
        if not na or not nb:
            return 0
        n = max(len(na), len(nb))
        na += (0,) * (n - len(na))
        nb += (0,) * (n - len(nb))
        return (na > nb) - (na < nb)

    @classmethod
    def _version_range_covers(cls, range_str, version) -> bool:
        """解析 '>=1,<3' 式简单版本区间并判断 version 是否命中；无任何可解析子句 → 宽松 True。"""
        clauses = 0
        for clause in str(range_str or "").split(","):
            clause = clause.strip()
            if not clause:
                continue
            m = re.match(r"(>=|<=|>|<|=)?\s*v?([\d.]+)", clause, re.I)
            if not m:
                continue
            op, bound = (m.group(1) or "="), m.group(2)
            cmp_ = cls._version_cmp(version, bound)
            clauses += 1
            if (op == ">=" and cmp_ < 0) or (op == ">" and cmp_ <= 0) \
                    or (op == "<=" and cmp_ > 0) or (op == "<" and cmp_ >= 0) \
                    or (op == "=" and cmp_ != 0):
                return False
        return True  # 无子句 / 全部解析失败 → 宽松放行

    def fallback_pool(self, domain: str = "") -> list:
        """Task 4：旧固定编排池意图名列表（兼容降级链；与 pipeline._ORCH_AGENTS 一致）。

        用字符串常量而非 import pipeline，避免循环导入。domain 非空时按 capabilities/意图名
        宽松过滤（无命中返回空列表，由上层继续降级到单 Agent）。
        """
        pool = _FALLBACK_ORCH_AGENTS.split(",")
        domain = (domain or "").strip()
        if not domain:
            return list(pool)
        out = []
        for intent in pool:
            meta = self._db_meta.get(intent, {})
            caps = [str(c) for c in (meta.get("capabilities") or [])]
            if any(domain in c or c in domain for c in caps) or domain in intent:
                out.append(intent)
        return out

    @staticmethod
    def capabilities_enum() -> set:
        """Task 4：内置能力枚举词表（供 Task 12 capabilities DB 层校验；本次仅定义常量）。"""
        return set(_CAPABILITIES_ENUM)

    def list(self) -> list:
        """合并 DB + 内置（DB 优先覆盖同名，最终以内置兜底保证全量）。"""
        merged = {}
        for k, v in self.DEFINITIONS.items():
            merged[k] = {"intent": k, **v.to_dict()}
        for name, defn in self._db_defs.items():
            meta = self._db_meta.get(name, {})
            merged[name] = {
                "intent": name, **defn.to_dict(),
                "display_name": meta.get("display_name", defn.name),
                "icon": meta.get("icon", "🤖"),
                "intent_keywords": meta.get("intent_keywords", []),
                "db_configured": True,
            }
        return list(merged.values())

