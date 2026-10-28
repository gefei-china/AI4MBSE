# -*- coding: utf-8 -*-
"""AgentPipeline Mixin：工具定义、执行、重试策略与调用留痕。

由 tools/split_pipeline.py 从 agent/pipeline.py 机械切分而成；⚠️ 切分脚本**已一次性执行完毕、不可重跑**—— 此后本文件按普通源码维护（方法体与其它模块一样可直接改）。"""
from .common import *

# ★ 2026-10-28：补模块级 `logger`。
#   此前本文件有 **5 处** `logger.warning/info`（可消费过滤剔除、技能白名单裁剪、
#   MCP 动词闸挡掉、可消费过滤失败、未就绪工具跳过），但模块里**从未定义 logger**
#   ⇒ 每处都抛 NameError，而它们又统统被 `except Exception: pass` 包住
#   ⇒ **一行日志都没打出来，也没有任何报错**。
#   这正是"静默兜底把错误变成不可见"的典型：留痕代码写了等于没写。
import logging as _logging

logger = _logging.getLogger(__name__)

#: ★2026-10-10：确定性**本地**修复类工具，豁免「写意图闸」（仅对产码类 Agent）。
#: 为什么需要：它们的 side_effect=write（确实改代码），而八视图输入是
#: 「生成 X 视图并校验」—— 不含修复词 ⇒ 被整类过滤 ⇒ 模型看不到它们
#: ⇒ 实测 validate 报了 block 却没人修（autofix 调用 **0 次**）。
#: ⚠️ 只豁免"改自己刚生成的代码"的工具，**不含任何入库/删改类工具**。
_REPAIR_WRITE_EXEMPT = {"sysml_v2_autofix"}

#: ★ 2026-10-17（米爸三轮反馈的收口件）：**AI 主动澄清工具**（human-in-the-loop）。
#: 背景：米爸要求「基于实际上下文、基于 AI 真的需要澄清的内容输出确认清单」，
#: 而旧机制只有两道**路由层**澄清（阶段 1.4 意图确认 / 1.5 内容级探测）——
#: AI 执行到一半发现自己缺信息（要选版本/定范围/缺参数）时，只能写一段
#: markdown「请确认：……」然后停（截图实证：用户拿到纯文本，无法点选）。
#: 本工具让"需要用户拍板"成为 Agent 循环里的**一等动作**：
#:   LLM 调 ask_user → _exec_tool_call 拦截返回 clarify_required 信号
#:   → ReAct 循环中断 → 发射 clarify_ask 事件（结构化卡片）+ 落挂起 → 等用户回答
#:   → 用户点选/跳过 → 续跑（答案随【澄清补充】进原轮）。
#: ⚠️ 注入位置在**所有过滤之后无条件 append**（见 _build_tools_def 末尾）：
#:   它是对话级能力，不属于任何 Agent 绑定 ⇒ 白名单/JIT/写意图闸都不该挡它。
_ASK_USER_TOOL = {
    "type": "function",
    "function": {
        "name": "ask_user",
        "description": (
            "向用户发起确认/澄清（人在回路）：当你**确实需要用户拍板**才能继续时调用——"
            "① 缺少关键信息（未指定工程/版本/范围/格式等，且无法从上下文合理推断）；"
            "② 有多个合理方案需要用户选择（如选哪个版本、导出多大范围）；"
            "③ 动作影响面大，执行前需要用户确认。调用后流程暂停并弹出**可点选的确认卡片**，"
            "用户回答后自动继续执行。⚠️ 不要用纯文本提问然后自行结束——那不会生成确认卡片，"
            "用户只能手打回复；也不要为确认而确认（信息足够时直接执行，不要滥用）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {"type": "string",
                             "description": "要用户确认/回答的问题：一句话说清这次要决定什么，必须结合当前上下文具体化（如『要导出哪个版本的 SysML v2 代码？』），不要写泛泛的『请问您想做什么』"},
                "options": {"type": "array", "items": {"type": "string"},
                            "description": "供用户点选的候选答案：2-6 个，基于当前上下文生成（如具体版本号/具体范围/具体工程名）；用户也可选『其他/自定义』自由输入"},
                "context": {"type": "string",
                            "description": "（可选）补充背景：为什么需要确认、各选项的含义与差异，帮助用户决策（限 500 字内）"},
                "allow_custom": {"type": "boolean",
                                 "description": "（可选，默认 true）是否允许用户选『其他/自定义』自由输入"},
            },
            "required": ["question"],
        },
    },
}


_READ_ATTACHMENT_TOOL = {
    "type": "function",
    "function": {
        "name": "read_attachment_section",
        "description": (
            "读取**本轮上传附件**的指定章节正文（按需取全文，不是一次性全塞给你）。"
            "当 prompt 里给出了「上传资料·章节提纲」或「尚未读到正文的章节」时，"
            "用本工具按章节号取回原文，然后��继续任务。"
            "⚠️ **传章号即可拿到该章的全部子节**（如 section=\"5\" 会一次返回 5.1~5.5 全部正文，"
            "结果末尾会注明）⇒ **不要逐个小节重复调用**（5、5.1、5.2… 全取一遍会撑爆上下文）。"
            "⚠️ 不要因为正文没出现在上下文里就向用户索要原文——你手上有本工具，"
            "自己取；只有取不到（章节号写错/附件无该章）才向用户说明。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "section": {"type": "string",
                            "description": "要读取的章节号，如 '5'、'5.3'、'第6章'、'7.1'。"
                                           "省略或传空则返回附件的章节清单（提纲）。"},
                "doc_index": {"type": "integer",
                              "description": "（可选）本轮第几个附件，从 1 开始；"
                                             "只有一个附件时省略即可。"},
            },
            "required": [],
        },
    },
}


def _impact_preview(name: str, arguments: dict) -> str:
    """★ 2026-10-28（写前确认 HITL）：生成**影响面摘要**（给人看的），而不是裸参数 JSON。

    ## 为什么不能只给 JSON
    "只给 JSON 让人批 = 没让人批" —— 人看不懂参数就不会认真看，
    于是审批退化成闭眼点通过（行业反模式：approval fatigue）。
    ⇒ 摘要必须回答三个问题：**做什么 / 对谁做 / 影响多大**。

    ## 口径（纯结构推导，零硬编码工具名）
    - **目标名**：取常见命名键（name / title / label / elementName / ...）
    - **容器**：取常见容器键（parentId / ownerId / containerId / viewId / projectId ...）
    - **规模**：取列表型参数的条数（elements / items / elementIds / operations ...）
    - **动作**：从工具名的动词族反推（create→新增 / update→修改 / delete→删除 ...）
    """
    args = arguments or {}
    # 目标名
    _target = ""
    # ① 语义名（最易识别）
    for _k in ("name", "title", "label", "elementName", "packageName", "viewName",
               "newName", "targetName"):
        _v = args.get(_k)
        if isinstance(_v, (str, int)) and str(_v).strip():
            _target = str(_v).strip()
            break
    # ② ★ ID 类键（2026-10-28 真机清库实测补：删除/移动类参数里**只有 ID 没有 name**
    #   ⇒ 初版摘要渲染成「删除「（未命名对象）」× N」，用户等于盲批。
    #   与 `hil_service._batch_target` 保持同一口径。)
    if not _target:
        for _k in ("dataId", "dataID", "data_id", "elementId", "elementID", "viewId",
                   "presentingId", "packageId", "targetId", "id", "uuid"):
            _v = args.get(_k)
            if isinstance(_v, (str, int)) and str(_v).strip():
                _s = str(_v).strip()
                _target = "#" + (_s[-12:] if len(_s) > 12 else _s)
                break
    # 容器
    _container = ""
    for _k in ("parentId", "parentElementId", "ownerId", "containerId", "viewId",
               "packageId", "versionId", "projectId", "branchId"):
        _v = args.get(_k)
        if isinstance(_v, (str, int)) and str(_v).strip():
            _container = "%s=%s" % (_k, str(_v).strip())
            break
    # 规模
    _count = 0
    for _k in ("elements", "items", "elementIds", "operations", "changes",
               "presentingIds", "targets", "relationships"):
        _v = args.get(_k)
        if isinstance(_v, (list, tuple)) and len(_v) > _count:
            _count = len(_v)
    # 动作
    _n = str(name or "").lower()
    _verb = "执行"
    for _pat, _w in (("delete", "删除"), ("remove", "移除"), ("import", "导入"),
                     ("apply", "应用变更"), ("undo", "撤销"), ("overwrite", "覆盖"),
                     ("create", "新增"), ("update", "修改"), ("set", "设置"),
                     ("move", "移动"), ("clone", "克隆"), ("layout", "布局"),
                     ("present", "呈现"), ("configure", "配置"), ("generate", "生成"),
                     ("run", "运行"), ("annotate", "标注")):
        if _pat in _n:
            _verb = _w
            break
    _parts = ["%s「%s」" % (_verb, _target or "（未命名对象）")]
    if _container:
        _parts.append("容器 %s" % _container)
    if _count:
        _parts.append("影响 %d 项" % _count)
    return "；".join(_parts)


class ToolMixin:
    """工具定义、执行、重试策略与调用留痕。"""

    def _build_tools_def(self, intent: str, user_input: str = "", user=None) -> list:
        """缺口B + TR-P3：从 Agent 绑定构建 OpenAI-compatible 工具定义（function calling）。

        - 内置工具 → 宽松 schema（name/description）；MCP 工具 → 宽松参数 schema
        - TR-P3b 角色过滤：DB 工具 allowed_roles 非空且当前用户角色不匹配 → 不注入
        - TR-P3a JIT 工具选择：候选 >8 时按 user_input 语义预筛 top 6（复用 VectorEngine 双条件防误筛），
          保底保留读类核心工具（graph_retrieve/validate/impact_analyze）；小规模全量注入（简单可靠）
        """
        # 角色过滤辅助：DB 中该工具的允许角色
        def _roles_of(name: str) -> list:
            try:
                from database import get_db
                conn = get_db()
                try:
                    row = conn.execute("SELECT allowed_roles FROM tools WHERE name=?", (name,)).fetchone()
                    if not row:
                        return []
                    return json.loads(row["allowed_roles"] or "[]")
                finally:
                    conn.close()
            except Exception:
                return []

        def _allowed(name: str) -> bool:
            roles = _roles_of(name)
            if not roles:
                return True
            role = (user or {}).get("role_name") or (user or {}).get("role") or ""
            return role in roles

        tools = []
        bound = self.registry.get_bound_mcp(intent)
        # ★ 2026-10-13：工具参数契约注入开关（config: tool_jit.inject_input_schema，默认开）。
        #   背景见 core/config.py 同名键的注释——简言之：SQL 早就 SELECT 了 t.input_schema，
        #   但 candidates 是 4 元组装不下它，payload 恒为 `"properties": {}`（模型以为任意参数）。
        #   本次**只改注入、不改执行侧校验**；关掉即回到改动前行为。
        try:
            from core.config import get as _cfg_get_ii
            _inject_schema = bool(_cfg_get_ii("tool_jit", "inject_input_schema", True))
        except Exception:
            _inject_schema = True
        #: 工具名 → 已解析的 JSON Schema 片段（仅在 _inject_schema 为真时填充）。
        #: ⚠️ 刻意**不塞进 candidates 元组**：那是个 4 元组，全文件有 15 处按 c[0]/c[1] 位置下标访问
        #:   （过滤、角色校验、JIT 预筛、拼装…），扩成 5 元组会让每一处都变成潜在 IndexError。
        #:   旁挂字典是零侵入方案，且与"过滤逻辑与 schema 无关"这个事实相符。
        _schema_by_name = {}
        builtin_desc = {
            "graph_retrieve": "知识图谱检索（GraphRAG：实体链接 + 子图遍历 + 向量降级），参数 query:str",
            # ⚠️ 2026-10-13 已移除 `conflict_check`（假实现，无条件返回"无新冲突"）。
            #    连带移除本登记：留着会让它继续进 payload，却已无执行器 ⇒ 模型拿到一个必败工具。
            "impact_analyze": "变更影响分析（BFS 遍历产出影响图）",
            "validate": "模型预评审校验（规范性/一致性/合理性）",
            "entity_create": "创建知识实体（人工确认后入库）",
            # 基础通用文件操作工具（系统文件读写/增删，定义见 file_tools.py）
            "file_list": "列出目录下的文件与子目录（参数 path:str，recursive:bool 可选）",
            "file_read": "读取文本文件内容（UTF-8，参数 path:str，max_chars:int 可选）",
            "file_write": "创建或覆盖写入文本文件（UTF-8 自动建父目录，参数 path:str, content:str；写操作）",
            "file_append": "追加文本到文件末尾（不存在则创建，参数 path:str, content:str；写操作）",
            "file_mkdir": "创建目录（参数 path:str；写操作）",
            "file_delete": "删除文件或空目录（参数 path:str，recursive:bool 可选；destructive 高风险需人工确认）",
            # 基础通用报告导出工具（内容→md/docx/pdf，定义见 report_tools.py）
            "report_export": "把报告内容导出为指定格式文档并落盘（参数 path:str, title:str, content:str 或 sections:str, fmt:md|docx|pdf；写操作）",
            # ★ 2026-10-17（米爸能力配置）：save_artifact——把内容显式登记进**产物库**
            #   （file_write 只写磁盘进不了产物库；参数 title:str, content:str,
            #   kind:code|document|sysml|report 可选, lang:str 可选；写操作）
            "save_artifact": "把代码/文档全文保存为会话产物（入产物库，用户可在产物面板查看下载；参数 title:str, content:str, kind:str 可选, lang:str 可选；写操作）",
            # ★ 2026-10-17（米爸需求）：联网搜索/网页抓取（定义见 web_tools.py）
            "web_search": "联网搜索互联网最新消息（参数 query:str, max_results:int 可选；时效性问题——最新/今天/新闻/发布——必用本工具，不要凭训练记忆回答时效问题）",
            "web_fetch": "抓取指定网页的正文文本（参数 url:str, max_chars:int 可选；配合 web_search 结果链接深读）",
        }
        candidates = []  # (name, desc, type, server_name_or_None)

        def _absorb_schema(name: str, raw) -> None:
            """把 tools.input_schema 归一化后存进 _schema_by_name（★ 2026-10-13）。

            归一化规则（逐条都是为了让**脏数据不得污染 payload**）：
              · 非 str / 空串 → 不记（下游回落宽松 schema）
              · json 解析失败 → 不记（**不抛异常**：单个脏 schema 不得拖垮整次构建）
              · 解析结果非 dict → 不记
              · 缺 `type` → 补 `"object"`（协议要求；存量数据有只写 properties 的）
              · `properties` 非 dict → 置 `{}`（防止 LLM 侧拿到非法 schema 直接 400）
              · 显式带 `additionalProperties=False` 的**保留**（那是作者收紧的意图，不该被我们抹掉）
            """
            if not _inject_schema or not name or not raw or not isinstance(raw, str):
                return
            try:
                sch = json.loads(raw)
            except Exception:
                return
            if not isinstance(sch, dict):
                return
            if not isinstance(sch.get("properties"), dict):
                sch["properties"] = {}
            if not sch.get("type"):
                sch["type"] = "object"
            _schema_by_name[name] = sch

        # MCP 绑定工具
        # ★ 2026-10-16：同时记下「本 Agent 显式绑定的 MCP 工具名」——它们是**显式契约**，
        #   JIT 语义预筛不得裁剪（与技能白名单 `_sk_wl` 同性质，见下方 core_keep 拼接处）。
        #   事故依据（米爸报「让 MCP 查工程列表却没调用」· 实测复现）：
        #     智源对接agent 绑了 71 个 MCP 工具，问「查询工程列表」时 JIT 按语义裁到 top 6，
        #     实际注入 `['sysml_v2_validate','graph_retrieve','impact_analyze',
        #     'sys_query_roles','sys_query_users','validate']` —— `query_projects` **不在内**。
        #   ⇒ 模型如实回答"我可用的工具只有 5 项、不含工程列表查询"（那不是幻觉，是事实）。
        #   根因：JIT 的本意是防「无关工具被顺手调用」，但**绑定动作本身就是用户的显式授权**，
        #   把绑定工具和偶然候选一起语义裁剪 ⇒ 绑了 71 个只露 6 个，绑定形同虚设。
        _bound_mcp_names = set()
        # ★ P0-6（2026-10-26）：MCP 工具**真实元数据** + **按需暴露**。
        #   背景（实测，生产库快照）：
        #     · MCP 工具的 description 此前是**同一个模板串**、parameters 是宽松空壳
        #       ⇒ 71 个工具 description 去重后仅 1 种 ⇒ **JIT 语义打分零区分度**
        #       ⇒ 只能"全量注入"（5,467 token/轮，占整包 71~80%）；
        #     · 模型看到 71 个工具，但**没有用途说明、没有参数契约** ⇒ 只能靠名字猜。
        #   根因：`mcp_health.discover_server` 落库时只存了 name，
        #   把 `list_tools()` 返回的 description / inputSchema **主动丢弃**了。
        #   本次：① 真实 description + 真实 inputSchema（有 tool_meta 时）；
        #        ② 每个 server **保底 min_keep 个**（保"显式契约"语义）+ 语义命中者额外入选。
        #   兼容：tool_meta 为空（存量/未重新发现）⇒ **逐字节退回改动前行为**。
        _mcp_meta_enabled = True
        _mcp_min_keep = 8
        _mcp_top_n = 0
        try:
            from core import config as _cfg_mcp
            _mcp_meta_enabled = bool(_cfg_mcp.get("context_audit", "mcp_meta_enabled", True))
            _mcp_min_keep = int(_cfg_mcp.get("context_audit", "mcp_min_keep", 8) or 0)
            _mcp_top_n = int(_cfg_mcp.get("context_audit", "mcp_top_n", 0) or 0)
        except Exception:
            pass
        _mcp_stats = {"servers": 0, "total": 0, "selected": 0, "meta_servers": 0}
        _mcp_semantic_names = set()   # ★ 语义命中的 MCP 工具（供 core_keep 保底）
        _mcp_write_groups = {}        # ★ MCP 写类工具 → 动词族（供"动词级意图闸"使用）
        for m in bound:
            _srv_name = m.get("name", "")
            _tools = list(m.get("tools") or [])
            # ── 读该 server 的工具元数据（description + inputSchema）──
            _meta = {}
            if _mcp_meta_enabled:
                try:
                    from agent.mcp_tool_meta import load_tool_meta
                    _meta = load_tool_meta({"tool_meta": m.get("tool_meta") or ""})
                except Exception:
                    _meta = {}
            if _meta:
                _mcp_stats["meta_servers"] += 1
                # 按需暴露：保底 min_keep + 语义命中补充
                try:
                    from agent.mcp_tool_meta import pick_for_injection
                    _picked, _pst = pick_for_injection(
                        _meta, user_input or "", min_keep=_mcp_min_keep, top_n=_mcp_top_n)
                except Exception:
                    _picked, _pst = list(_meta.keys()), {"selected": len(_meta), "keep": 0,
                                                        "semantic": 0, "semantic_names": []}
                _tools = _picked or _tools
                _mcp_semantic_names |= set(_pst.get("semantic_names") or [])
                _mcp_stats["servers"] += 1
                _mcp_stats["total"] += _pst.get("total", len(_meta))
                _mcp_stats["selected"] += _pst.get("selected", len(_picked))
            else:
                # 存量 server（无 tool_meta）⇒ 全量（改动前行为，逐字节一致）
                _mcp_stats["servers"] += 1
                _mcp_stats["total"] += len(_tools)
                _mcp_stats["selected"] += len(_tools)
            for t in _tools:
                _bound_mcp_names.add(t)
                # ★ 写类动词归类（MCP 工具不在 tools 表 ⇒ 后面的 side_effect 闸管不到它们）
                try:
                    from agent.mcp_tool_meta import mcp_write_group as _mwg
                    _g = _mwg(t)
                    if _g:
                        _mcp_write_groups[t] = _g
                except Exception:
                    pass
                # description：有真实元数据就用真实的（模型才知道这工具干什么）；
                # 无元数据 ⇒ 保持改动前的模板串（逐字节一致）
                _info = _meta.get(t) if _meta else None
                if _info and _info.get("description"):
                    _desc = _info["description"]
                else:
                    _desc = f"MCP 工具（{_srv_name}），端点 {m.get('endpoint','')}"
                candidates.append((t, _desc, "mcp", _srv_name))
                # 真实参数契约（有则用；无则保持宽松空壳）
                if _info:
                    _sch = _info.get("inputSchema")
                    if isinstance(_sch, dict) and _sch:
                        try:
                            if not isinstance(_sch.get("properties"), dict):
                                _sch["properties"] = {}
                            if not _sch.get("type"):
                                _sch["type"] = "object"
                            # ★ 2026-10-27（conv 9637 实测）：**参数契约必须瘦身**再注入。
                            #   21 个 MCP 工具的完整 schema 共 47KB（≈16k tokens/轮），
                            #   13 轮就烧穿 300k token 预算 ⇒ 模型在第 12 轮被判"预算耗尽"、
                            #   离建包只差最后一步。压掉参数级长描述（保留 type/required/enum
                            #   这些结构性信息），体量降一半而可用性不降。
                            try:
                                from agent.mcp_tool_meta import slim_schema as _slim
                                _sch = _slim(_sch)
                            except Exception:
                                pass
                            _schema_by_name[t] = _sch
                        except Exception:
                            pass
        # 内置工具（仅 Agent 声明过的）
        agent_def = self.registry.get(intent)
        for name in agent_def.tools:
            if name in builtin_desc and not any(c[0] == name for c in candidates):
                candidates.append((name, builtin_desc[name], "builtin", ""))
        # TR-P3：DB 注册表自定义工具（Agent 通过绑定面板绑定的 type=tool 本地工具——
        # 修复既有缺口：绑定后运行时本应可消费，此前 _build_tools_def 遗漏了这类绑定）
        try:
            from database import get_db
            conn = get_db()
            try:
                for row in conn.execute(
                    "SELECT t.name, t.description, t.input_schema, t.side_effect, "
                    "       t.kind, t.config FROM tools t "
                    "JOIN agent_tools at ON at.tool_name=t.name JOIN agents a ON at.agent_id=a.id "
                    "WHERE a.name=? AND at.tool_type='tool' AND at.enabled=1 AND t.status='active' "
                    "AND t.source!='builtin'", (intent,)).fetchall():
                    desc = row["description"] or f"自定义工具 {row['name']}"
                    if row["side_effect"] == "write":
                        desc += "（写操作，需人工确认）"
                    elif row["side_effect"] == "destructive":
                        desc += "（高风险操作，必须人工确认）"
                    # ★★ 2026-10-28（conv 9736~9740 实证）：**配置未就绪的工具不注入**。
                    #   真机：`zhiyuan_sysmlv2_gen/check/import` 的 url 是
                    #   `{cfg:zhiyuan.base_url}/...`，而运行配置里 base_url 与 token
                    #   都是空串 ⇒ 这 5 个工具历史调用 **16 次、成功 0 次**
                    #   （timed out / WinError 10054），却因列在
                    #   `tool_jit.core_keep_extra` 里被**常驻注入**，每轮白占名额。
                    #   更糟的是模型会优先用它们（"看起来是同平台的官方工具"），
                    #   而不是真正能落库的 MCP `import_sysml_text`；
                    #   失败又长成"网络超时"，把排障方向引到"平台连不上"，
                    #   而真相是"配置是空串"。⇒ 未就绪即不注入，并留痕。
                    try:
                        from agent.tool_readiness import readiness as _ready
                        _ok, _why = _ready(row["config"])
                    except Exception:
                        _ok, _why = True, ""
                    if not _ok:
                        try:
                            self._dropped_unready = list(
                                getattr(self, "_dropped_unready", []) or []) + [
                                "%s（%s）" % (row["name"], _why)]
                        except Exception:
                            pass
                        try:
                            logger.warning("[tool_gate] 跳过未就绪工具 %s：%s",
                                           row["name"], _why)
                        except Exception:
                            pass
                        continue
                    if not any(c[0] == row["name"] for c in candidates):
                        candidates.append((row["name"], desc, "db_tool", ""))
                    _absorb_schema(row["name"], row["input_schema"])
            finally:
                conn.close()
        except Exception:
            pass
        # ★ 2026-10-13：内置工具的参数契约。
        #   为什么必须单独查：上面那条 SQL 带 `t.source!='builtin'`，**刻意排除内置工具**
        #   （内置的候选来源是 `builtin_desc` 字典，不是 DB 行）。但实测内置工具同样有真值 schema
        #   （source='builtin' 的 18 个里 **16 个有**，包括 entity_create/file_write 这类写操作）。
        #   ⇒ 不补这一段，接通的就只有"自定义/插件工具"，而**风险最高的写操作工具仍拿不到契约**。
        #   按需点名查询（只查本次要注入的内置名），不打全表。
        if _inject_schema and builtin_desc:
            try:
                from database import get_db as _bdb
                _bc = _bdb()
                try:
                    _bnames = list(builtin_desc.keys())
                    for _r in _bc.execute(
                            "SELECT name, input_schema FROM tools "
                            "WHERE name IN (%s) AND status='active'" % ",".join("?" * len(_bnames)),
                            tuple(_bnames)).fetchall():
                        _absorb_schema(_r["name"], _r["input_schema"])
                finally:
                    _bc.close()
            except Exception:
                pass
        # P1-6「安装即可消费」（2026-09-16）：已安装且启用的 tool 型插件自动进入候选——
        # 与 Agent 绑定无关：从市场装的 / 自建的 tool 只要启用即可被消费。
        # 只接纳「具备执行通道」的（有 legacy 映射落到 tools 表执行器，或 manifest 声明 HTTP 端点）；
        # 纯元数据工具无执行器，注入只会诱导 LLM 调用必败工具（同 graph_db_* 未启用时的处理）。
        try:
            from database import get_db as _pdb
            from plugin_system import store as _pstore_t
            _tc = _pdb()
            try:
                for _pid in _pstore_t.plugin_ids_of_types(_tc, "tool", user):
                    _te = _pstore_t.tool_entry_from_plugin(_tc, _pid)
                    if not _te or not _te.get("executable"):
                        continue
                    _tdesc = _te.get("description") or f"插件工具 {_te['name']}"
                    if _te.get("side_effect") == "write":
                        _tdesc += "（写操作，需人工确认）"
                    elif _te.get("side_effect") == "destructive":
                        _tdesc += "（高风险操作，必须人工确认）"
                    for _tn in (_te.get("names") or [_te["name"]]):
                        if not any(c[0] == _tn for c in candidates):
                            candidates.append((_tn, _tdesc, "plugin_tool", ""))
            finally:
                _tc.close()
        except Exception:
            pass
        # TR-P3b：角色过滤
        candidates = [c for c in candidates if _allowed(c[0])]
        # P1-6：统一可消费过滤——有插件映射但该插件未安装/已停用/已下架的从候选剔除
        # （一处覆盖 MCP 绑定 / 内置 / DB 注册表 / 插件四条来源，使"停用"对运行时真实生效）
        try:
            from database import get_db as _pdb2
            from plugin_system import store as _pstore_t2
            _fc = _pdb2()
            try:
                _keep_t = _pstore_t2.consumable_filter(_fc, user)
                _name2id = {r["name"]: r["id"]
                            for r in _fc.execute("SELECT id, name FROM tools").fetchall()}
                _before = [c[0] for c in candidates]
                candidates = [c for c in candidates if _keep_t("tools", _name2id.get(c[0], -1))]
                # ★ 2026-10-28（conv 9737 实证）：**剔除必须可见**。
                #   真机：我把 zhiyuan_sysmlv2_import/gen/check 绑给本 Agent，
                #   但 `_build_tools_def` 实调仍只有 12 个工具、三者全部 MISSING ——
                #   绑定动作"看起来成功"，运行时被本闸静默剔除，模型只能走次优路径。
                #   根因：这三个插件 author_name='平台内置' 且**从未有过安装记录**，
                #   被判成"被管理员全局卸载"（见 visibility.py 的 2026-09-29 收窄），
                #   而"从未装过"与"被卸载"在该判据下不可区分（数据侧已用
                #   tools/migrate_seed_builtin_installs.py 补齐系统级可用标记）。
                #   ⇒ 这里把剔除清单留痕，杜绝同类问题再次静默。
                _gone = [n for n in _before if n not in {c[0] for c in candidates}]
                if _gone:
                    self._dropped_by_visibility = list(_gone)
                    try:
                        logger.warning(
                            "[tools] 以下**已绑定**工具被「可消费过滤」剔除（插件未安装/被停用）：%s"
                            "；若是平台内置能力，多半是缺 user_id=0 的 plugin_installs 行"
                            "（见 tools/migrate_seed_builtin_installs.py）", _gone)
                    except Exception:
                        pass
            finally:
                _fc.close()
        except Exception:
            pass
        # V2.6：graph_db_* 工具仅在图数据库启用（graph_db.enabled=true）时注入——
        # 未启用时这些工具必败（graph_db_tools 顶层拦截返回错误），注入只会诱导 LLM 调用必败工具
        try:
            from core import config as _gcfg
            _gdb_on = str(_gcfg.get("graph_db", "enabled", False)).strip().lower() in ("1", "true", "yes", "on")
        except Exception:
            _gdb_on = False
        if not _gdb_on:
            candidates = [c for c in candidates if not str(c[0]).startswith("graph_db_")]
        # 基础通用文件工具按「技能白名单」补入候选：命中的 Skill 声明了 file_* → 工具契约即注入，
        # 否则 Agent 未显式声明 file_* 时 LLM 永远拿不到文件操作能力（skill 触发即视为授权）
        _sk_wl = getattr(self, "_skill_allowed_tools", None)
        if _sk_wl:
            from file_tools import FILE_TOOL_DEFS
            for _d in FILE_TOOL_DEFS:
                if _d["name"] in _sk_wl and not any(c[0] == _d["name"] for c in candidates):
                    candidates.append((_d["name"], _d["description"], "builtin", ""))
            # ★ 2026-10-17（米爸能力配置）：save_artifact 同通道补入——技能 48「文件操作」
            #   声明了它 ⇒ 显式归档产物能力随技能命中注入。
            #   ⚠️ 实测冒烟（会话 9543）：只扩技能 allowed_tools 不够——本段只遍历
            #   FILE_TOOL_DEFS，save_artifact 不在其中 ⇒ 技能命中了候选里也没有，
            #   AI 如实回答"工具集中没有 save_artifact"。"在白名单"≠"在候选"。
            if "save_artifact" in _sk_wl and not any(c[0] == "save_artifact" for c in candidates):
                candidates.append(("save_artifact", builtin_desc.get("save_artifact", ""),
                                   "builtin", ""))
        # P0-3：委派最小权限——本次调用指定的工具白名单（Worker ⊆ Supervisor），
        # 白名单模式下不做「读类核心工具保底」，避免突破最小权限
        _wl = getattr(self, "_tool_whitelist", None)
        if _wl:
            _wlset = set(_wl)
            # ★ P0-9（2026-10-26）：**MCP 显式绑定工具豁免技能白名单**。
            #
            # 问题（实测，生产库快照 + 真实 _build_tools_def）：
            #   `智源对接agent` 同时绑定了 MCP server（71 工具）与技能「智源平台管理」
            #   （allowed_tools 6 项）。技能一命中就被写成 `_tool_whitelist`
            #   （execute.py:460-466）⇒ 白名单**把绑定的 MCP 工具全部裁掉**：
            #     问「查询智源工程列表」：无白名单 15 工具/8 MCP → 有白名单 **2 工具/0 MCP**
            #     问「读取文件内容」    ：无白名单 21 工具/8 MCP → 有白名单 **8 工具/0 MCP**
            #   更糟的是：该技能白名单里 5 个 `zhiyuan_*` 工具虽在 tools 表且 active，
            #   却**没有以 type='tool' 绑定到本 agent** ⇒ 进不了候选集
            #   ⇒ 白名单「授了什么也拿不到、却把已有的全撤销」。
            #   ⚠️ 这正是 2026-10-16「绑了 71 个只露 6 个、工程列表查不到」事故的
            #     **真正根因之一**（当时归因为 JIT），至今仍在。
            #
            # 判断（为什么 MCP 该豁免）：
            #   「绑定 MCP」与「绑定技能」是**同一管理员、同一面板**的两次显式授权。
            #   技能的 `allowed_tools` 语义是「本技能干活只需要这几个工具」，
            #   不是「撤销本 agent 已获授权的其它工具」。用它反向撤销 MCP 授权属于越权，
            #   且失败是**静默**的（工具凭空消失，无日志、无提示）。
            #
            # 开关：`context_audit.mcp_exempt_skill_whitelist`（默认 True）。
            #   置 False ⇒ 逐字节退回改动前（白名单连 MCP 一起裁）。
            # ⚠️ 只豁免 **MCP**；内置工具 / DB 工具仍受白名单约束（最小权限不放松）。
            _mcp_exempt = True
            try:
                from core import config as _cfg_wl
                _mcp_exempt = bool(_cfg_wl.get("context_audit",
                                               "mcp_exempt_skill_whitelist", True))
            except Exception:
                _mcp_exempt = True
            if _mcp_exempt:
                _mcp_cand_names = {c[0] for c in candidates if c[2] == "mcp"}
                _rescue = [c for c in candidates if c[2] == "mcp" and c[0] not in _wlset]
                candidates = [c for c in candidates if c[0] in _wlset] + _rescue
                if _rescue:
                    try:
                        from agent.context_audit import record_blocks
                        record_blocks({}, phase="tools", extra={
                            "whitelist_exempted_mcp": len(_rescue),
                            "whitelist_size": len(_wlset),
                        })
                    except Exception:
                        pass
                    try:
                        logger.warning(
                            "技能白名单（%d 项）会裁掉 %d 个已显式绑定的 MCP 工具（如 %s），"
                            "已按 `mcp_exempt_skill_whitelist=True` 豁免。"
                            "若要恢复严格最小权限，请把该开关置 False。",
                            len(_wlset), len(_rescue),
                            ", ".join(sorted(c[0] for c in _rescue)[:3]))
                    except Exception:
                        pass
            else:
                candidates = [c for c in candidates if c[0] in _wlset]
        # P0-按需工具：写类工具默认不注入（side_effect=write/destructive）——
        # 仅当输入含明确写意图关键词（创建/入库/保存/修改/删除…）时注入，
        # 防「无关写工具被注入 / LLM 顺手调用」；白名单（委派最小权限）模式下跳过
        if not _wl:
            # ⚠️ 「修复 / 修 / 校验后自修」是**写意图**：autofix 类工具干的是改写代码，
            #    而典型触发语恰恰是「校验一下这段代码」「修复语法错误」——都不含原表里的词
            #    （实测：漏了「修复」会导致 autofix 工具被本闸过滤掉，LLM 根本看不到它）。
            #    故补入 repair 类词条。
            _write_intent_kw = ("创建", "新增", "添加", "入库", "写入", "保存", "修改", "更新", "删除",
                                "录入", "登记", "生成实体", "建立实体", "落地", "持久化", "导入",
                                "修复", "修一下", "改写", "纠正", "自修", "修好",
                                # ★ 2026-10-17（米爸反馈4）：导出/取回类任务同样需要落盘通道
                                #   ——实测（会话 9446）「导出全量模型」不含原表任何关键词，
                                #   file_write 被写意图闸滤掉 ⇒ AI 只能读取不能保存。
                                "导出", "留存", "取回", "落盘", "下载", "存档", "备份",
                                # ★ 2026-10-17（米爸能力配置）：「把代码存入产物文档」话术
                                #   不含"保存/写入"——实测（9446 msg 4600）会被本闸滤掉
                                "存入", "产物文档")
            if not any(k in (user_input or "") for k in _write_intent_kw):
                try:
                    from database import get_db as _gdb
                    _c2 = _gdb()
                    try:
                        _se_map = {r["name"]: r["side_effect"] for r in _c2.execute(
                            "SELECT name, side_effect FROM tools").fetchall()}
                    finally:
                        _c2.close()
                except Exception:
                    _se_map = {}
                # 技能白名单声明的写/破坏工具视为显式授权契约，不受写意图关键词约束
                #
                # ★★ 2026-10-10 补一个豁免（实测 autofix 从未被注入的真正根因）：
                #   `sysml_v2_autofix` 的 side_effect=write（它确实改写代码），
                #   而写意图闸要求输入含「创建/保存/修复…」等词。
                #   八视图的输入是「**请生成 sequence 时序图并校验**」——**不含修复词**
                #   ⇒ autofix 被**整类过滤** ⇒ 模型压根看不到这个工具。
                #   实测证据：本轮重跑 autofix 调用 **0 次**，而 validate 报了 5 次 block。
                #   ⇒ 修复的**第一前提**是"它得在清单里"，闸把它挡在门外。
                #   豁免范围**仅限确定性修复工具**（不改库、不入库，只改本次产出的代码），
                #   且仅当 Agent 名字里表明它在**产码**（视图/图生成）时。
                #   ⚠️ 不扩大到其它 write 工具（防"顺手改库"）。
                _repair_exempt = _REPAIR_WRITE_EXEMPT & {c[0] for c in candidates}
                if _repair_exempt and any(k in (intent or "") for k in
                                          ("view", "视图", "图生成", "design")):
                    candidates = [c for c in candidates
                                  if _se_map.get(c[0], "read") not in ("write", "destructive")
                                  or (_sk_wl and c[0] in _sk_wl)
                                  or c[0] in _repair_exempt]
                else:
                    candidates = [c for c in candidates
                                  if _se_map.get(c[0], "read") not in ("write", "destructive")
                                  or (_sk_wl and c[0] in _sk_wl)]
        # ── ★ MCP 写类工具的「动词级」意图闸（2026-10-27）──
        #   上面那道闸按 `tools.side_effect` 过滤，**MCP 工具不在 tools 表 ⇒ 管不到**。
        #   实测后果：「导出 SysML v2 源码」的清单里出现 `delete_model_elements`，
        #   「校验语法」的清单里出现 `apply_model_changes` / `import_sysml_text`
        #   —— 读类任务被塞进破坏性工具，模型"顺手调用"就是事故。
        #   这里按**工具名里的动词族**判定：用户输入没提这个动作 ⇒ 本轮不注入。
        #   ⚠️ 与技能白名单 `_sk_wl` 的关系：白名单是**权限**（能不能用），
        #      本闸是**恰当性**（这一轮该不该递到模型眼前）——两者不是一回事。
        #      白名单模式（`_wl` 非空，即 execute 冷路径把技能白名单独占成 `_tool_whitelist`）
        #      下**整闸跳过** —— 与上面那道 side_effect 闸保持同一口径，
        #      且避免「白名单声明了 delete ⇒ 恒常暴露」的副作用。
        if _mcp_write_groups and not _wl:
            try:
                from agent.mcp_tool_meta import filter_mcp_by_intent as _fmbi
                _keep_names, _drop_names = _fmbi(
                    [c[0] for c in candidates if c[0] in _mcp_write_groups],
                    user_input or "", _mcp_write_groups)
                if _drop_names:
                    _drop_set = set(_drop_names)
                    candidates = [c for c in candidates if c[0] not in _drop_set]
                    try:
                        logger.info("[tool_gate] MCP 写类工具按动词闸挡掉 %d 个: %s",
                                    len(_drop_names), ", ".join(sorted(_drop_names)[:6]))
                    except Exception:
                        pass
            except Exception:
                pass
        # TR-P3a：JIT 语义预筛——候选 ≥2（多工具可挑）即按输入语义裁剪，防「无关工具也被注入/LLM 顺手调用」
        # 保底：读类核心工具（graph_retrieve/validate/impact_analyze）不因预筛丢失；全弱相关时空回退维持全量
        jit_threshold = 2
        jit_top_n = 6
        # 读类核心工具保底 = 内置 3 个 + config `tool_jit.core_keep_extra` 追加项。
        # 为什么配置化：新增一个「必须常驻」的工具（如建模自校验 sysml_v2_validate）时，
        # 只登记配置即可，不必改这里；否则它的注入会退化成「看语义预筛的心情」。
        _extra_keep = ""
        try:
            from core import config as _kcfg
            _extra_keep = str(_kcfg.get("tool_jit", "core_keep_extra", "") or "")
        except Exception:
            pass
        core_keep = {"graph_retrieve", "validate", "impact_analyze"} | {
            s.strip() for s in _extra_keep.split(",") if s.strip()}
        if getattr(self, "_tool_whitelist", None):
            core_keep = set()  # 白名单模式下不做保底注入
        # 技能白名单声明的工具是显式契约，JIT 语义预筛不得裁剪（与 core_keep 同等保底）
        if _sk_wl:
            core_keep = core_keep | set(_sk_wl)
        # ★ 2026-10-16：Agent 显式绑定的 MCP 工具同样是显式契约 —— 用户在「绑定 MCP 服务器」
        #   面板里逐个绑上去的，JIT 无权以"语义不相关"为由把它们裁掉。
        #   （不设这一步 ⇒ 绑 71 个只露 6 个，工程列表查询类工具必被裁，见上方事故依据。）
        # ★ P0-6（2026-10-26）**保底粒度细化**：
        #   改动前：绑定的**全部** MCP 工具进 core_keep ⇒ 恒不裁剪（71 个全量注入，
        #     实测 5,467 token/轮、占整包 71~80%）。根因是 MCP 工具的 description
        #     全是同一句模板 ⇒ JIT 打分零区分度 ⇒ 只能"要么全给要么全裁"。
        #   现在：**筛选前移** —— `pick_for_injection` 已按「min_keep 保底 + 语义命中补充」
        #     把 71 个压到 N 个，故 `_bound_mcp_names`（= 已选出的集合）就是**新的显式契约粒度**，
        #     整集进 core_keep ⇒ JIT **不得二次裁剪**。
        #   ⚠️ 为什么不是"只保底语义命中者"：那会让 JIT 再把 min_keep 保底项裁掉
        #     ⇒ `mcp_min_keep` 的"至少露 N 个"保证失效（弱相关场景可能只剩 1~2 个），
        #     且同一 server 两次请求暴露的工具集不稳定。筛选只做一次，位置前移到 pick 阶段。
        #   无 tool_meta（存量 server）⇒ `_bound_mcp_names` 即全量 ⇒ **逐字节退回改动前行为**。
        if _bound_mcp_names:
            core_keep = core_keep | _bound_mcp_names
        if len(candidates) >= jit_threshold and user_input:
            try:
                from semantic import SemanticSearch
                from core import config as _cfg
                idx = [{"name": c[0], "text": f"{c[0]} {c[1]}"} for c in candidates]
                # P1a-1：对齐 Semantic Tool Selection——top_k + threshold + 空回退
                # 2026-09-19：阈值按**本次实际走的路**选（两路余弦量纲不同，一套阈值必有一路失准）：
                #   dense（真 embedding）→ tool_threshold_dense（0.53，分位等价标定）
                #   bigram（降级）      → tool_threshold（0.12，原经验值）
                # ★ 2026-10-26（P0-5）：**空回退语义已变更** —— 原"弱相关 → 维持全量注入"
                #   改为"弱相关 → 取语义 top-N"（`context_audit.jit_fallback_top_n`，0 退回全量）。
                #   理由：阈值是**质量线**不是**开关**；弱相关时排序仍有信息量，
                #   全量注入实测把 5 个工具的任务膨胀到 24 个（7,487 字符 / 4,162 token）。
                _ss = SemanticSearch()
                _th_b = float(_cfg.get("embedding", "tool_threshold", 0.12) or 0.12)
                _th_d = float(_cfg.get("embedding", "tool_threshold_dense", 0.53) or 0.53)
                # ★ P0-5（2026-10-26）：空回退改 top-N。**rank 的 top_k 必须 ≥ 回退 N**，
                #   否则 `scored` 会被提前截断（semantic.rank 返回 `scored[:top_k]`），
                #   取 top-N 只能拿到 ≤top_k 条 ⇒ 回退 N 形同虚设。
                _fb_n = 0
                try:
                    from core import config as _cfg_fb
                    _fb_n = int(_cfg_fb.get("context_audit", "jit_fallback_top_n", 8) or 0)
                except Exception:
                    _fb_n = 8
                _rank_k = max(jit_top_n, _fb_n) if _fb_n > 0 else jit_top_n
                scored = _ss.rank(user_input, idx,
                                  top_k=int(_cfg.get("embedding", "tool_top_k", _rank_k)) or _rank_k,
                                  threshold=0, key="text")
                _th = _th_d if getattr(_ss, "last_backend", "bigram") == "dense" else _th_b
                chosen = {s[1]["name"] for s in scored if s[0] >= _th}
                # ★ P0-5（2026-10-26）：空回退改为「取语义 top-N」而非「全量注入」。
                #   改动前：全部候选都低于阈值 ⇒ chosen 为空 ⇒ **全量注入**
                #   （实测「帮我把活动图生成出来」⇒ 24 个工具 / 7,487 字符 / 4,162 token，
                #    而该任务实际只需 sysml 视图类工具；同族意图载荷波动 5→24 个）。
                #   根因：把「阈值」当成了「开关」—— 阈值是**质量线**，不是**开关**。
                #   弱相关时排序**仍有信息量**（最不相关的排在后面），故 top-N 是无损的收紧。
                #   ⚠️ core_keep 保底**不受影响**：无论走哪条分支都要并入（否则
                #   graph_retrieve/validate/impact_analyze 会在弱相关场景丢失）。
                #   开关：`context_audit.jit_fallback_top_n`（0 = 退回改动前的全量行为）。
                if not chosen:
                    if _fb_n > 0:
                        # 排序降序 ⇒ 取前 N 个（scored 已按 score 降序）
                        # ⚠️ `scored` 可能**为空**：bigram 降级 + 全部零相关时，
                        #   `semantic.rank(threshold=0)` 会把 0 分条目全过滤掉 ⇒ 返回 []。
                        #   此时不能退化为全量（那正是本次要修的膨胀源），
                        #   改用**候选集原始顺序**取前 N 个（顺序本身携带一定优先级：
                        #   内置工具 → 技能白名单 → 显式绑定，详见本函数上游构建顺序）。
                        if scored:
                            _top = {s[1]["name"] for s in scored[:_fb_n]}
                        else:
                            _top = {c[0] for c in candidates[:_fb_n]}
                        chosen |= _top
                        try:
                            from agent.context_audit import record_blocks
                            record_blocks({}, phase="tools", extra={
                                "jit_fallback_full": 0,
                                "jit_fallback_top_n": len(_top),
                                "jit_fallback_no_scores": 0 if scored else 1,
                                "tool_candidates": len(candidates),
                            })
                        except Exception:
                            pass
                    else:
                        # 退回改动前行为（全量注入）。
                        # ⚠️ 必须把 chosen 置为**全部候选名** —— 否则后面的
                        #    `candidates = [c for c in candidates if c[0] in chosen]`
                        #    会把候选**全部清空**（chosen 为空 ⇒ 过滤结果为空），
                        #    这是"全量"与"清空"的区别，务必显式写全。
                        chosen = {c[0] for c in candidates}
                        try:
                            from agent.context_audit import record_blocks
                            record_blocks({}, phase="tools", extra={
                                "jit_fallback_full": 1,
                                "tool_candidates": len(candidates),
                            })
                        except Exception:
                            pass
                # core_keep 保底：两条分支都要并入（读类核心工具 + 显式绑定的 MCP）
                for c in candidates:
                    if c[0] in core_keep:
                        chosen.add(c[0])
                candidates = [c for c in candidates if c[0] in chosen]
            except Exception:
                pass  # 预筛失败则维持全量
        # 2026-09-17 S4：工具名合法性护栏 —— OpenAI 兼容协议要求 function.name 匹配 ^[a-zA-Z0-9_-]+$。
        # 实测事故：tools 表存在中文名工具（id=1594「知识库查询」）→ 整个 tools 载荷被 provider 拒绝：
        #   "400 Invalid 'tools[1].function.name': string does not match pattern" → 异常被 llm 层静默吞掉后
        #   回落 Mock（用户看到的是 Mock 文本且毫不知情），并连带让同批次其它合法工具一起失效。
        # 处理：剔除非法名并留痕（单个脏数据不得拖垮整次工具调用）。此处只拦名字，不改工具来源与过滤逻辑。
        _TOOL_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
        _bad_names = [str(c[0]) for c in candidates if not _TOOL_NAME_RE.match(str(c[0] or ""))]
        if _bad_names:
            candidates = [c for c in candidates if _TOOL_NAME_RE.match(str(c[0] or ""))]
            try:
                logger.warning(
                    "已剔除工具名不合法的工具（协议要求 ^[a-zA-Z0-9_-]+$，请到「能力中心 · 工具与MCP」改为英文名）：%s",
                    _bad_names)
            except Exception:
                pass
        # ★★ 2026-10-28：**就绪度单点闸**（放在所有过滤之后、装配之前）。
        #   为什么必须放这里而不是只放在"DB 工具分支"：
        #   `tool_jit.core_keep_extra` 会在 JIT 之后把名字**再塞回** chosen
        #   ⇒ 分支处的跳过会被复活（实测：分支处已打印"跳过未就绪工具
        #   zhiyuan_sysmlv2_check/gen/import"，最终清单里三个仍在）。
        #   ⇒ 只在"最终候选集"上判一次，才不依赖任何后续链路是否又加回来。
        #   判据与理由见 `agent/tool_readiness`（必需配置为空 ⇒ 必然失败 ⇒ 不注入）。
        try:
            from agent.tool_readiness import missing_required as _miss_req
            from database import get_db as _rdb
            _rc = _rdb()
            try:
                _cfgmap = {r["name"]: r["config"] for r in _rc.execute(
                    "SELECT name, config FROM tools WHERE kind='http'").fetchall()}
            finally:
                _rc.close()
            _unready = []
            _kept = []
            for _c in candidates:
                _cfgj = _cfgmap.get(_c[0])
                if _cfgj:
                    _miss = _miss_req(_cfgj)
                    if _miss:
                        _unready.append("%s（缺 %s）" % (_c[0], "/".join(_miss)))
                        continue
                _kept.append(_c)
            if _unready:
                candidates = _kept
                self._dropped_unready = _unready
                try:
                    logger.warning("[tool_gate] 未就绪工具不注入（必需配置为空 ⇒ 必然失败）: %s",
                                   ", ".join(_unready))
                except Exception:
                    pass
        except Exception:
            pass
        # ★ 2026-10-13：按工具名注入真实参数契约（此前**两个分支都硬编码 properties:{}**，
        #   等于把 DB 里 39/41 条已存在的 input_schema 全部丢弃 ⇒ 模型以为"任意参数"）。
        #   _LOOSE 是回落形态，与改动前**逐字节一致**，保证关开关/无 schema 时行为不变。
        _LOOSE = {"type": "object", "properties": {}, "additionalProperties": True}
        for name, desc, ctype, server in candidates:
            _params = _schema_by_name.get(name) or _LOOSE
            tools.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": desc,
                    "parameters": _params,
                },
            })
        # ★ 2026-10-17（米爸反馈「AI 执行中需要确认时请输出确认卡片」）：ask_user
        #   **无条件**注入（在所有过滤之后）。它是对话级能力而非 Agent 绑定工具，
        #   白名单（委派最小权限）/ JIT 预筛 / 写意图闸都不该把它挡掉——
        #   挡掉它 = AI 只能回到"写一段请确认的纯文本然后停"的旧形态（截图实证）。
        #   关闭开关：config `agent.ask_user_enabled`（默认开）。
        try:
            from core.config import get as _cfg_ask
            _ask_on = bool(_cfg_ask("agent", "ask_user_enabled", True))
        except Exception:
            _ask_on = True
        if _ask_on and not any(t.get("function", {}).get("name") == "ask_user" for t in tools):
            tools.append(_ASK_USER_TOOL)
        # ★ P0-3（2026-10-28）计划外化：`todo_write` —— 与 ask_user 同理**无条件**注入。
        #   理由同 ask_user：它是**对话级能力**（管理本会话待办清单），不属于任何
        #   Agent 绑定 ⇒ 白名单（委派最小权限）/ JIT 预筛 / 写意图闸都不该挡它。
        #   ⚠️ 挡掉它的后果不是"少个功能"，而是**"完成"的定义权退回模型手里**
        #     （实测：评估器把「建议下一步执行校验」判成 done=true，msg 3985）。
        #   开关：config `agent.todo_enabled`（**默认关** ⇒ 不改变现有行为）。
        #   默认关的代价是"不开则该能力不生效"；收益是"不冒未验证的风险"。
        try:
            from core.config import get as _cfg_todo
            _todo_on = bool(_cfg_todo("agent", "todo_enabled", False))
        except Exception:
            _todo_on = False
        if _todo_on and not any(t.get("function", {}).get("name") == "todo_write" for t in tools):
            try:
                from agent.todo_store import todo_tool_def as _todo_def
                tools.append(_todo_def())
            except Exception:
                pass
        # ★ 2026-11-13（#55）：`read_attachment_section` 与 ask_user 同理**无条件**注入 ——
        #   它是「把本轮已上传的附件读全」的对话级能力。若被白名单/写意图闸挡掉，
        #   模型就只能看着提纲干瞪眼、回头找用户要原文（实测原话：「请把第 5、6、7 章的
        #   正文贴进来」）。只读、无副作用，不该受任何闸约束。
        #   ⚠️ 仅当本轮**确有附件**时注入：没有附件还给模型这个工具 = 诱导它反复空调。
        if getattr(self, "_att_sections", None):
            if not any(t.get("function", {}).get("name") == "read_attachment_section"
                       for t in tools):
                tools.append(_READ_ATTACHMENT_TOOL)
        # ★ 2026-10-20（共享产物工作区·方案1）：编排子任务消费上游交付物引用
        #   （query 里的「offload #N」块）⇒ 必须能看到 tool_result_fetch。
        #   子任务实例带 _orch_result_offloads（worker 在派发前挂上）即注入；
        #   非编排场景不受影响。注入 = tools_def + 白名单双放行（ensure_fetch_tool 幂等）。
        if getattr(self, "_orch_result_offloads", None):
            from agent.pipeline_parts import tool_offload as _toff2
            _toff2.ensure_fetch_tool(tools, getattr(self, "_tool_whitelist", None))
        # ★ P0-6（2026-10-26）：MCP 暴露情况归因 ⇒ "暴露了多少 / 裁掉了多少"可观测。
        #   这三个数字是判断 `mcp_min_keep` 该设多少的唯一依据（靠猜会重演
        #   "JIT 裁太狠 ⇒ 工程列表查不到"的事故）。
        #   ⚠️ `mcp_injected` 必须按**工具名集合**判定，不能按 description 前缀 ——
        #   有 tool_meta 时 description 是真实描述（不以"MCP "开头）。
        try:
            from agent.context_audit import record_blocks
            if _mcp_stats.get("servers"):
                _mcp_names = {c[0] for c in candidates if c[2] == "mcp"}
                record_blocks({}, phase="tools", extra={
                    "mcp_servers": _mcp_stats["servers"],
                    "mcp_meta_servers": _mcp_stats["meta_servers"],
                    "mcp_total": _mcp_stats["total"],
                    "mcp_selected": _mcp_stats["selected"],
                    "mcp_semantic": len(_mcp_semantic_names),
                    "mcp_injected": len([t for t in tools
                                        if t.get("function", {}).get("name") in _mcp_names]),
                    "tool_payload_total": len(tools),
                })
        except Exception:
            pass
        return tools

    def _tool_side_effect(self, name: str) -> str:
        """TR-P2b：查询工具副作用分类（read/write/destructive），查不到默认 read。"""
        try:
            from database import get_db
            conn = get_db()
            try:
                row = conn.execute(
                    "SELECT side_effect FROM tools WHERE name=? AND status='active'", (name,)
                ).fetchone()
                return row["side_effect"] if row else "read"
            finally:
                conn.close()
        except Exception:
            return "read"

    # ══════════════════════════════════════════════════════════════════════
    # ★ 2026-10-28（写前确认 HITL）统一人工确认闸
    # ══════════════════════════════════════════════════════════════════════
    def _hil_gate(self, name: str, arguments: dict, agent_ctx: dict,
                  conv_ctx: int, tool_type: str = "") -> dict | None:
        """写前确认闸。返回 **None ⇒ 放行**；返回 dict ⇒ **拦截**（该 dict 即工具结果）。

        ## 位置（全链最靠后的确定性边界）
        `白名单(852) → RBAC(877) → mbse_pull_ingest(915) → Hook(985) → 本闸 → 执行(1114+)`
        - **RBAC 之后**：无权限的人不该让工具走到"要不要人工确认"这一步
          （`tools.py:870-876` 的既定不变量：把"他本来没资格"伪装成"他够格但需要审批"更糟）。
        - **Hook 之后**：Hook 是管理员配的**确定性细粒度**规则（`tool_hooks`，可带参数条件），
          比策略层的粗分档更具体 ⇒ 它先判；本闸只兜"Hook 没覆盖到"的。
        - **执行之前**：这是唯一能保证"**没有副作用发生**"的位置。

        ## 判据：双源写类判定 + 策略表取档位（策略即数据）
        - 写类：`hil_service.classify_action`（`tools.side_effect` ∪ MCP 动词族）
          ⇒ 修掉"**MCP 工具不在 tools 表 ⇒ side_effect 恒判 read ⇒ 三道闸全失效**"这个实测断链。
        - 档位：`services/hil_policy.evaluate`（**灰度 = 改表不改代码**）

        ## 行业依据（详见 services/hil_policy.py 模块头的「行业依据」段）
        AWS AGENTREL02-BP05「按**影响 × 可逆性**分档」；
        Suntec「不要给 agent 整体分等级，要给**每个动作**分等级」；
        agentpatternscatalog 反模式「**approval fatigue** — 闸门太多会让人闭眼点通过」
        ⇒ ⇒ ① 按动作分档 ② 策略外置为数据 ③ **必须批次合并**（否则逐条确认 ⇒ 退化）。

        ## 返回的拦截信号形状（**刻意对齐 clarify_required**）
        `{"ok": True, "confirm_required": True, "confirm": {...}}`
        ⇒ ReAct 循环（stream.py / execute.py）与 `ask_user` 澄清**同构处理**
        （检出即 break，由外层发事件 + 落挂起）。
        """
        # ── 批准旁路：确认单被批准后，执行它时不能再进本闸（否则无限循环）──
        _auth = getattr(self, "_hil_authorized_conf_id", None)
        if _auth:
            return None
        # ★ 2026-10-28（conv 9733 实测）：**确定性本地修复工具豁免写前确认**。
        #   真机现场：validate 报 12 条 ERROR（block）⇒ 模型调 `sysml_v2_autofix` 修 ⇒
        #   被判"写操作" ⇒ 弹确认单 ⇒ ReAct 中断 ⇒ 这一轮以
        #   「⏸ 写操作需要人工确认后才能执行」收尾 —— **修复闭环被一次人工点击卡死**，
        #   而该工具 `sysml_autofix_tools.py` 里 `INSERT/UPDATE/commit` 出现次数为 **0**，
        #   只对传入的 code 字符串做规则级改写（无任何持久化/外部副作用）。
        #   ⇒ 与「入库/删改平台数据」的写操作不是一类，不该共用同一道确认闸。
        #   ⚠️ 豁免范围严格等于 `_REPAIR_WRITE_EXEMPT`（当前仅 sysml_v2_autofix），
        #      绝不扩大：真正的平台写入（create/import/delete）仍必须人工确认。
        if str(name) in _REPAIR_WRITE_EXEMPT:
            return None
        try:
            from hil_service import classify_action as _ca
            _cls = _ca(name, self._tool_side_effect(name))
        except Exception:
            return None
        if _cls.get("kind") == "read":
            return None

        # ── 取档位（策略即数据；策略不可读 ⇒ fail-closed 按 confirm）──
        _level, _reason, _pol = "confirm", "", {}
        try:
            from services.hil_policy import load_policies as _lp, evaluate as _ev
            from database import get_db as _gdb
            _c = _gdb()
            try:
                _pols = _lp(_c)
            finally:
                _c.close()
            _d = _ev(_pols, decision_point="tool_write",
                     match_key=_cls.get("match_key") or "read",
                     context={"side_effect": _cls.get("kind"),
                              "tool": name,
                              "tool_family": _cls.get("match_key") or "",
                              "tool_type": tool_type or _cls.get("source")})
            _level = _d.get("level") or "confirm"
            _reason = _d.get("reason") or ""
            _pol = _d
        except Exception as _pe:
            # fail-closed：策略层异常 ⇒ 按 confirm（**绝不静默放行写操作**）
            _level = "confirm"
            _reason = "策略层不可读（fail-closed）：%s" % (str(_pe)[:120])

        # ── auto：放行 ──
        if _level == "auto":
            return None
        # ── agent 级 hil_level 作为**下限**（只收紧、不放宽）──
        #   为什么只当"地板"：行业依据（Suntec 治理框架）明确说
        #   「**不要给 agent 整体分等级，要给每个动作分等级**」——
        #   agent 级若能独立决定，会退化成"整个 agent 一刀切"。
        #   而 `hil_policy.escalate` 的不变式正是「策略层只能收紧、不能放宽」。
        #   ⚠️ 目标档位**只用 confirm / notify**，不用 approve ——
        #   approve 与 confirm 不是严格程度关系（approve 要更高权限角色批，
        #   confirm 是用户本人点一下），混进同一条 rank 会产生"approve 被 confirm 覆盖"的歧义。
        #   破坏族要不要 approve，由**策略表**按 match_key 决定（动作级分档）。
        try:
            _agent_lvl = str(getattr(self, "_hil_level", "") or "")
            if _agent_lvl in ("L1", "L2"):
                from services.hil_policy import escalate as _esc
                _floor = "notify" if _agent_lvl == "L1" else "confirm"
                _level = _esc(_level, _floor)
        except Exception:
            pass
        # ── notify：放行 + 留痕（**灰度第一档的采集点**：不拦但记全量数据）──
        if _level == "notify":
            try:
                from core.audit import audit as _au, audit_user as _auu
                _au(_auu(getattr(self, "_tool_user", None)), "hil_notify",
                    f"tool={name} match_key={_cls.get('match_key')} kind={_cls.get('kind')} "
                    f"convo={conv_ctx}", "notify",
                    branch=str((agent_ctx or {}).get("branch") or ""))
            except Exception:
                pass
            return None

        # ── confirm / approve：入队并挂起 ──
        _agent = str((agent_ctx or {}).get("agent") or "")
        _run_id = int((agent_ctx or {}).get("run_id") or 0)
        # ★ 2026-10-28（真机 E2E 暴露）：`tool_type` **必须在闸内自己判**。
        #   外面传进来的 `tool_type` 在闸这个位置还是初值 `"builtin"`
        #   （真正的类型分派在后面的 MCP / HTTP / local 分支里才发生）
        #   ⇒ 直接用它会把 MCP 写操作记成 builtin ⇒ 批准后走错执行器、审计也看不出类型。
        #   判据：`classify_action` 的 `source` 已经告诉我们它是从哪个源判出来的。
        _ttype = tool_type or ""
        if _cls.get("source") == "mcp_verb" or _ttype in ("", "builtin"):
            _src = _cls.get("source") or ""
            if _src == "mcp_verb":
                _ttype = "mcp"
            elif _src == "tools.side_effect":
                # 本地工具：按 tools 表 kind 细分（http 集成 vs 内置）
                try:
                    from database import get_db as _gdb2
                    _c2 = _gdb2()
                    try:
                        _kr = _c2.execute(
                            "SELECT kind FROM tools WHERE name=? AND status='active'",
                            (name,)).fetchone()
                        _k = str((_kr["kind"] if _kr else "") or "")
                        _ttype = "http" if _k == "http" else (_k or "builtin")
                    finally:
                        _c2.close()
                except Exception:
                    _ttype = "local"
            elif not _ttype:
                _ttype = "local"
        # 批次合并键：同一轮 + 同族 + 同会话 ⇒ 合并成一张卡（approval fatigue 防线）
        _trace = str(getattr(self, "_tool_trace_id", "") or "") or "no-trace"
        _batch = ""
        try:
            import hashlib as _h
            _batch = _h.sha1(("|".join([str(conv_ctx or 0), _agent,
                                        str(_cls.get("match_key")), _trace,
                                        str(_run_id)])).encode("utf-8")).hexdigest()[:20]
        except Exception:
            _batch = ""
        _prev = _impact_preview(name, arguments or {})
        _conf = None
        try:
            from hil_service import HILService as _HS
            from database import db_conn as _dbc
            with _dbc() as _c:
                _conf = _HS.queue_confirmation(
                    _c, _agent, name, arguments or {},
                    preview=_prev,
                    conversation_id=conv_ctx or 0,
                    run_id=_run_id,
                    tool_type=_ttype,
                    level=_level,
                    batch_id=_batch,
                    timeout_min=int(_pol.get("timeout_min") or 0))
        except Exception as _qe:
            # 入队失败 ⇒ **fail-closed**：宁可不做，也不能在没确认的情况下写下去
            return {"ok": False, "confirm_required": False,
                    "result": f"写操作「{name}」无法进入人工确认流程（{str(_qe)[:120]}），已拒绝执行"}

        if not _conf:
            return {"ok": False, "confirm_required": False,
                    "result": f"写操作「{name}」需人工确认，但确认单创建失败，已拒绝执行"}

        _cid = int(_conf.get("id") or 0)
        _bn = int(_conf.get("batch_count") or 1)
        _merged = bool(_conf.get("batch_merged"))
        return {
            "ok": True,
            "confirm_required": True,
            "confirm": {
                "id": _cid,
                "tool": name,
                "tool_type": _ttype,
                "level": _level,
                "match_key": _cls.get("match_key"),
                "kind": _cls.get("kind"),
                "reason": _reason,
                "preview": _prev,
                "arguments": arguments or {},
                "batch_count": _bn,
                "batch_merged": _merged,
                "conversation_id": conv_ctx or 0,
                "timeout_at": _conf.get("timeout_at") or "",
            },
            "result": ("⏸ 写操作「%s」已进入人工确认队列（确认单 #%d%s）：%s"
                       % (name, _cid,
                          ("，同批共 %d 项" % _bn) if _bn > 1 else "",
                          "等待人工批准后才会真正执行；**在此之前不会产生任何副作用**。"
                          "不要重复发起该操作。")),
        }

    def _exec_tool_call(self, name: str, arguments: dict) -> dict:
        """工具调用**唯一出口**：跑一遍真实执行，再补一句确定性判读。

        ★ 2026-10-28（conv 9726 实证）：为什么判读要挂在**这里**而不是各工具里
          ── 执行体有 10+ 个 return（白名单/权限/确认/超时/异常/MCP/内置…），
             逐个补注解必然漏；而 stream.py 与 execute.py **两条 ReAct 路径**
             最终都只经这一个方法 ⇒ 挂在出口即两路同源（本仓"同一语义只写一份"纪律）。
          ── 注解**不改 result 原文**，只在末尾追加判读句（原文仍是审计口径）。
          ── 判读失败绝不阻断执行（旁路原则，见 agent/tool_result_notes.py）。
        """
        res = self._exec_tool_call_impl(name, arguments)
        try:
            from agent import tool_result_notes as _notes
            return _notes.annotate(name, arguments, res)
        except Exception:
            return res

    #: 写入类工具里承载"目标元素标识"的参数名（不同平台叫法不同，全部覆盖）。
    #: MCP `import_sysml_text` → `targetPackageId`；自建 REST `sysmlv2/import` →
    #: `targetPackageDataId`；命令批/视图类 → `ownerId`。新增平台只加一行，不改逻辑。
    _TARGET_ID_KEYS = ("targetPackageId", "targetPackageDataId",
                       "targetPackageID", "packageId", "ownerId")

    def _import_target_gate(self, name, arguments, intent_ctx, conv_ctx):
        """**统一写入目标网关**（MCP 与 HTTP 两条通道共用，执行前唯一一处）。

        返回 `None`（放行）或一个 `{ok:False, code:...}` 的拦截结果。

        ## 为什么它是"网关"而不是"校验补丁"（2026-10-28 米爸追问后的定型）
        旧实现是"拿到模型给的 id，判它是不是父包"。那默认了**目标 id 由模型提供**，
        于是无论判得多准，最好的结果也只是"拦住"，差的形态是"拦住却不给正确 id"
        ⇒ 模型下一轮还是得猜。

        现在改为：**先解析、再判定**——
          ① `write_target_resolver.resolve()` 从"用户点名的名字 + 本会话回执"
             解出**唯一确定的目标 id**（不是校验，是求解）；
          ② 模型给对了 ⇒ 放行（正常路径）；
          ③ 模型给错了 ⇒ **拒绝并直接把正确 id 写进回执**，让它在同一轮内自我修正
             （回执里带上"父包会冲掉子树"的后果说明，避免它换个写法继续错）；
          ④ 解析不出唯一目标 ⇒ 不猜；退回"父包检测"这一保守口径，
             判不出就放行交给人工确认（绝不因为"解析失败"把正常写入堵死）。

        ## 覆盖范围为什么扩大到所有 import 类
        真机（conv 9740）暴露的正是这个缺口：闸只挂在 `zhiyuan_sysmlv2_import`
        上，而**真正能落库**的是 MCP 的 `import_sysml_text`（走 MCP 分支，
        在本闸原来的位置之前就 return 了）⇒ 等于没拦。
        ⇒ 判据改为"名字里是否含 import"（词元匹配），两条通道一次覆盖。
        """
        try:
            nm = str(name or "")
            try:
                from agent.mcp_tool_meta import name_hits_group as _nhg
                _is_import = _nhg(nm, "import") or "import" in nm.lower()
            except Exception:
                _is_import = "import" in nm.lower()
            if not _is_import:
                return None
            args = arguments or {}
            did = ""
            for k in self._TARGET_ID_KEYS:
                if args.get(k) not in (None, ""):
                    did = str(args.get(k))
                    break
            if not did:
                # 没给目标 id：ADD_PACKAGE 可能按包名新建，不拦（交给平台与人工确认）
                return None
            uinput = str((intent_ctx or {}).get("user_input") or "")
            from database import get_db as _gdb3
            from agent import write_target_resolver as _wtr
            _c3 = _gdb3()
            try:
                _rv = _wtr.resolve(_c3, int(conv_ctx or 0), uinput)
                _verdict, _why = _wtr.check(did, _rv)
                # 解析不出唯一目标时，退回旧的"父包检测"（保守兜底，不改变既有语义）
                if _verdict == "unknown":
                    try:
                        from agent import import_target_guard as _itg
                        _par, _child = _itg.is_parent_of_target(
                            _c3, int(conv_ctx or 0), did, uinput)
                    except Exception:
                        _par, _child = False, ""
                    if _par:
                        return {
                            "ok": False,
                            "code": "WRONG_TARGET_PACKAGE",
                            "result": (
                                "⛔ 已拒绝执行：`%s` 是**父包**，不是你要写的那一个 ——"
                                "它的子级里就含「%s」。本工具是**覆盖导入**，"
                                "写到父包会冲掉它下面既有的全部子树。"
                                "请改用**目标包自身**的 id（取回执里的 elementId，"
                                "不要用 parentId），确认后再导入。" % (did, _child)),
                        }
                    return None
            finally:
                _c3.close()
            if _verdict == "ok":
                return None
            # verdict == wrong：拒绝 + 把正确 id 一并回喂（自我修正，不靠下一轮猜）
            return {
                "ok": False,
                "code": "WRONG_TARGET_PACKAGE",
                "result": (
                    "⛔ 已拒绝执行：写入目标不对。\n"
                    "· 本次调用给的目标：%s\n"
                    "· 用户点名的元素：%s%s ⇒ 正确 id = **%s**%s\n"
                    "· 原因：%s\n"
                    "⇒ 请用正确的 id 重新调用（本工具是覆盖语义，写到父包/别的元素会破坏其既有内容）。"
                    "若你判断用户其实要写的是另一个元素，请先用 ask_user 让用户确认。"
                    % (did,
                       _rv.get("name") or "?",
                       ("（%s）" % _rv["type"]) if _rv.get("type") else "",
                       _rv.get("target_id") or "?",
                       ("，父级：%s" % _rv["parent_name"]) if _rv.get("parent_name") else "",
                       _why)),
            }
        except Exception:
            return None  # 网关自身异常绝不阻断执行（旁路原则）

    def _exec_tool_call_impl(self, name: str, arguments: dict) -> dict:
        """缺口B：执行单个工具调用（内置 handler / MCP tools/call），返回标准化结果。

        优化1：每次调用落库 tool_call_logs（可观测），异常同样记录。
        """
        import time as _time
        intent_ctx = getattr(self, "_tool_intent_ctx", None) or {}
        agent_ctx = getattr(self, "_tool_agent_ctx", None) or {}
        conv_ctx = getattr(self, "_tool_conv_ctx", 0)
        t0 = _time.time()
        # ── P0-b（2026-10-25）M3：tool span 开点 ───────────────────────
        # 为什么开在这里而不是 with 包方法体：_exec_tool_call 有 10+ 个出口
        # （ask_user/权限闸/异常各自 return），逐个补 close 必然漏；而**所有**
        # 出口都汇入 _log_tool_call（8 个调用点）⇒ 开在此、闭在 _log_tool_call，
        # 一条不错。t0 传入让 span 窗口覆盖真实执行时长（backdate 校正时钟）。
        # 无 tracer（编排子任务/开关关）⇒ open 返回 0，闭点 no-op，零开销。
        try:
            from services.orch_span import current_tracer as _ct
            _tracer = _ct()
            if _tracer is not None:
                _tracer.open_tool(name, t0=t0)
        except Exception:
            pass   # 观测装配失败绝不阻断工具执行
        tool_type = "builtin"
        try:
            # ── ★ 2026-10-17（米爸反馈3）：ask_user 拦截（人在回路澄清）─────────
            #   放在**白名单检查之前**：ask_user 是对话级能力，不属于任何 Agent
            #   绑定/子任务授权 ⇒ 不受委派最小权限约束（Worker 也准问用户）。
            #   不真正"执行"任何副作用，只把 LLM 的提问结构化成 clarify_required
            #   信号上交：ReAct 循环（execute.py / stream.py 两处同构）见到它即
            #   中断本轮，由外层发射 clarify_ask 事件 + 落挂起等用户回答。
            # ★ 2026-11-13（米爸实测截图）：ask_user 同款对话级能力的第二例——
            #   `read_attachment_section`：让模型**自己**取回附件缺失章节的正文。
            #   背景：注入侧只喂正文前 4 块 + 章节提纲，模型知道「第 5/6/7 章存在」却
            #   **没有任何工具能取到它** ⇒ 只能反过来向用户索要正文（实测原话：
            #   「请把第 5、6、7 章的正文贴进来」）。提纲若不给通道就是空头承诺。
            #   本工具把「本轮已解析的附件全文」按章节号切片返回 —— 只读、限本轮、
            #   不越权（读不到就说读不到，不会静默返回空串）。
            if name == "read_attachment_section":
                _a = arguments or {}
                _sec = str(_a.get("section") or "").strip()
                _di = _a.get("doc_index")
                try:
                    _di = int(_di) if _di not in (None, "") else 1
                except Exception:
                    _di = 1
                result = self._read_attachment_section(_sec, _di)
                self._log_tool_call(name, tool_type, arguments, result, intent_ctx, agent_ctx, conv_ctx, t0)
                return result
            # ★ 2026-11-13（#56 端到端实证）：`tool_result_fetch` 同样移到**权限闸之前**。
            #   背景：本轮取第 5 章正文 6024 字符 > `_TOOL_MODEL_CAP`(3000) ⇒ 被 offload
            #   成引用块 ⇒ 模型只拿到一个 id，它去调 tool_result_fetch 想取全文，
            #   却因用户角色没有 `agent_tool:read` 权限被**RBAC 闸拦下**
            #   （实测原话：「权限不足：当前用户没有 agent_tool:read 权限」）
            #   ⇒ 模型最终看到"空" ⇒ 得出"第 5/6/7 章正文读不到"的错误结论。
            #   判据：tool_result_fetch 读的是**本次对话里刚由本框架产生的工具结果**
            #   （offload 落库在 tool_result_offloads 表、已随会话隔离），
            #   它是**框架内建的重读通道**，不是外部数据源 ⇒ 不该受用户角色权限约束。
            #   ⚠️ 仍然只读、仍限本会话（fetch_offload 按 conversation_id 校验）。
            if name == "tool_result_fetch":
                _oid = int((arguments or {}).get("id") or 0)
                from agent.pipeline_parts import tool_offload as _to
                _off = _to.fetch_offload(_oid)
                result = ({"ok": True, "result": _off[:20000]}
                          if _off else
                          {"ok": False, "result": f"offload #{_oid} 不存在（可能 id 无效——以工具结果提示中的编号为准）"})
                self._log_tool_call(name, tool_type, arguments, result, intent_ctx, agent_ctx, conv_ctx, t0)
                return result
            if name == "todo_write":
                # ★ P0-3（2026-10-28）计划外化：管理本会话待办清单。
                #   与 ask_user 同属**对话级能力** ⇒ 放在白名单/RBAC 之前
                #   （它不碰任何外部数据源，只写本会话自己的 agent_todos 表）。
                #   状态迁移一律走 agent_todo.transition() ⇒ 非法迁移如实报错不静默。
                _a = arguments or {}
                _conv = int(getattr(self, "_tool_conv_ctx", 0) or 0)
                try:
                    from agent.todo_store import handle_todo_write as _tw
                    result = _tw(_a, _conv)
                except Exception as e:
                    result = {"ok": False, "result": "todo_write 执行异常：%r" % (e,)}
                self._log_tool_call(name, tool_type, arguments, result, intent_ctx, agent_ctx, conv_ctx, t0)
                return result
            if name == "ask_user":
                _a = arguments or {}
                _q = str(_a.get("question") or "").strip()
                if not _q:
                    result = {"ok": False,
                              "result": "ask_user 缺少 question（要问的问题）——请把要用户确认"
                                        "的内容写进 question 后重试"}
                else:
                    _opts = [str(o).strip() for o in (_a.get("options") or [])
                             if str(o).strip()][:6]
                    result = {"ok": True, "clarify_required": True,
                              "clarify": {
                                  "id": "q_ask_user",
                                  "question": _q[:500],
                                  "options": _opts,
                                  "context": str(_a.get("context") or "").strip()[:500],
                                  "allow_custom": bool(_a.get("allow_custom", True)),
                              },
                              "result": "已向用户发起确认（%s，%d 个选项）——流程暂停，"
                                        "等待用户回答后继续" % (_q[:60], len(_opts))}
                self._log_tool_call(name, tool_type, arguments, result,
                                    intent_ctx, agent_ctx, conv_ctx, t0)
                return result
            # P0-3：委派最小权限防御——白名单外的工具直接拒绝（即使 tool def 未注入，模型也可能臆造）
            _wl0 = getattr(self, "_tool_whitelist", None)
            if _wl0 and name not in _wl0:
                # 拒绝也要留痕（与下方权限闸同哲学）：白名单拒绝此前只进 tool_call_logs，
                #   而结果文案不含权限词表关键词 ⇒ failure_kind 分不出这是「越权尝试」
                #   还是普通失败；misevolution 监控（P2-1 红线 #5）因此数不到这一类
                #   「被拒绝的访问」⇒ 补审计事件。旁路写（try 包住）：审计失败不得
                #   阻断工具拒绝本身（同 _spoof_guard 先例）。
                try:
                    from core.audit import audit as _audit_wl, audit_user as _au
                    _audit_wl(_au(getattr(self, "_tool_user", None)), "tool_whitelist_denied",
                              f"tool={name} convo={conv_ctx}", "blocked",
                              branch=str((agent_ctx or {}).get("branch") or ""))
                except Exception:
                    pass
                result = {"ok": False,
                          "result": f"工具「{name}」不在本次子任务授予的工具白名单内（最小权限，Worker ⊆ Supervisor），已拒绝执行"}
                self._log_tool_call(name, tool_type, arguments, result, intent_ctx, agent_ctx, conv_ctx, t0)
                return result
            # ── P0-6（2026-10-06）：权限闸（RBAC）——全链最靠前的确定性边界 ──────────
            # 为什么插在这里：白名单管「这次子任务准不准用」，RBAC 管「**这个人**准不准用」。
            # 顺序刻意在 Hooks / HIL / destructive 三道闸**之前** ——
            #   无权限的人不该让工具走到「要不要人工确认」这一步（那等于把
            #   「他本来没资格」伪装成「他够格但需要审批」）。
            # 与既有 7 道闸的关系：本项是**权限维度**，其余是**风险维度**（白名单/HIL/destructive），
            # 正交、互补，不是重复实现。
            if not getattr(self, "_tool_perm_gate_disabled", False):
                try:
                    from core.deps import check_tool_perm as _check_perm
                    _pu = getattr(self, "_tool_user", None)
                    _se = self._tool_side_effect(name)
                    _ok, _why = _check_perm(_pu, name, _se)
                    if not _ok:
                        # 拒绝也要留痕：这是「被拒绝的访问」——文章明确说
                        # 「被拒绝的那些请求，往往才是真正值得关注的信号」。
                        try:
                            from core.audit import audit as _audit_perm, audit_user as _au
                            _audit_perm(_au(_pu), "tool_perm_denied",
                                        f"tool={name} side_effect={_se} convo={conv_ctx}",
                                        "blocked", branch=str((agent_ctx or {}).get("branch") or ""))
                        except Exception:
                            pass
                        result = {"ok": False, "permission_denied": True, "result": _why}
                        self._log_tool_call(name, tool_type, arguments, result,
                                            intent_ctx, agent_ctx, conv_ctx, t0)
                        return result
                except Exception as _pe:
                    # 权限模块异常**不得静默放行**，也不得打断编排：
                    # 按"最严"处理（拒绝），并把异常留在结果里可观测。
                    result = {"ok": False, "permission_error": True,
                              "result": f"权限校验异常，工具「{name}」已按最小权限拒绝执行：{str(_pe)[:150]}"}
                    self._log_tool_call(name, tool_type, arguments, result,
                                        intent_ctx, agent_ctx, conv_ctx, t0)
                    return result
            # P1-4（2026-10-02）：工具结果 offload 重读——读类、无副作用、进程内直通。
            # 放行保障：offload 发生时 ensure_fetch_tool 已把本工具追加进 _tool_whitelist
            # 与本轮 tools_def；白名单未启用时自然放行。
            # ★ 2026-11-13（#56）：执行分支已上移到**权限闸之前**（见上方注释）——
            #   留在原位会被 RBAC 拦下，模型取不到 offload 全文 ⇒ 误判"章节读不到"。
            #   确定性 Hooks：PreToolUse 强制校验（对齐 Claude Code，不依赖 LLM 概率；故障静默放行）
            # 优先级：白名单拒绝（最小权限）→ 本钩子（block/require_confirm/warn）→ HIL L2 → destructive → 执行
            self._hook_warn = ""
            # V2.6：进程内工具（避免 HTTP 自环死锁）——mbse_pull_ingest 直通入库
            # （拉取动作即用户对话内的显式拍板，不进 HIL 确认队列；结果含完整入库统计）
            if name == "mbse_pull_ingest":
                # P1-3（2026-09-24）：本分支**先于**下面的自动化钩子执行，导致任何针对
                # 该工具的 hook（block / require_confirm）都永不生效 —— 钩子对它形同虚设。
                # 而它是 side_effect='write' 的**外部平台拉取入库**工具，恰恰最该被约束。
                # 现改为：先求值一次钩子，block 即拒、require_confirm 即入队；
                # 未命中 / warn 才继续走进程内直通（仍不走 HTTP，保留避免自环死锁的原意）。
                try:
                    from services.tool_hook_service import ToolHookService as _THS2
                    _hk2 = _THS2().run(name, arguments or {})
                except Exception:
                    import traceback as _tb2
                    _tb2.print_exc()          # 留痕：钩子求值失败不得静默放行
                    _hk2 = {"hit": False}
                if _hk2.get("hit") and _hk2.get("action") in ("block", "require_confirm"):
                    if _hk2.get("action") == "block":
                        result = {"ok": False,
                                  "result": f"被自动化钩子拦截：{_hk2.get('message') or name}"
                                            f"（钩子：{_hk2.get('name') or '-'}）"}
                    else:
                        from hil_service import HILService as _HIL2
                        from database import db_conn as _dbc2
                        _conf2 = None
                        with _dbc2() as _c2:
                            _conf2 = _HIL2.queue_confirmation(
                                _c2, (agent_ctx or {}).get("agent", ""), name, arguments or {},
                                preview=f"自动化钩子要求人工确认：{_hk2.get('message') or name}，"
                                        f"参数：{str(arguments or {})[:200]}",
                                conversation_id=conv_ctx or 0)
                        result = ({"ok": True,
                                   "result": f"「{name}」已进入人工确认队列（钩子要求，确认单 "
                                             f"#{_conf2['id']}），确认后生效"}
                                  if _conf2 else
                                  {"ok": False, "result": f"「{name}」需人工确认（钩子要求）"})
                    self._log_tool_call(name, tool_type, arguments, result,
                                        intent_ctx, agent_ctx, conv_ctx, t0)
                    return result
                try:
                    from database import get_db as _gdb
                    from services.knowledge_service import KnowledgeService as _KS
                    _args = arguments or {}
                    _c = _gdb()
                    try:
                        # P1-2（2026-09-28 补闸门洞）：**必须把会话上下文传下去**。
                        # 此前不传 → zhiyuan_pull_ingest 的 conversation_id=0 → 走「无工程上下文」
                        # 兜底分支，用平台 default_vc 静默拉**默认工程**的数据 —— 明明当前会话
                        # 就在 conv_ctx 里。同一会话在 A 标签页归属工程 A、B 标签页把平台默认
                        # 切成 B 时，A 里的 AI 拉取会拉到 B 的工程数据（P0-2 同类问题的另一处）。
                        _r = _KS(_c).zhiyuan_pull_ingest(
                            actor=str((agent_ctx or {}).get("agent") or "ai"),
                            vc=str(_args.get("vc") or ""),
                            package_data_id=_args.get("package_data_id"),
                            target_branch=str(_args.get("target_branch") or "personal"),
                            conversation_id=int(conv_ctx or 0))
                    finally:
                        _c.close()
                    if _r.get("error"):
                        result = {"ok": False, "result": f"拉取入库失败: {_r['error']}"}
                    else:
                        _st = _r.get("stats") or {}
                        result = {"ok": True, "result": json.dumps({
                            "batch_id": _r.get("batch_id"), "status": _r.get("status"),
                            "model_name": _r.get("model_name"), "target_branch": _r.get("target_branch"),
                            "stats": _st,
                            "note": ("幂等：该数据此前已入库，本次无新增实体（重复拉取去重）"
                                     if not _st.get("entities_written") and _st.get("triples_staged")
                                     else "已写入个人分支图库")}, ensure_ascii=False)}
                except Exception as _ie:
                    result = {"ok": False, "result": f"拉取入库异常: {str(_ie)[:200]}"}
                self._log_tool_call(name, tool_type, arguments, result, intent_ctx, agent_ctx, conv_ctx, t0)
                return result
            try:
                from services.tool_hook_service import ToolHookService
                _hk = ToolHookService().run(name, arguments or {})
            except Exception:
                _hk = {"hit": False}
            if _hk.get("hit"):
                _act = _hk.get("action", "warn")
                if _act == "block":
                    result = {"ok": False,
                              "result": f"被自动化钩子拦截：{_hk.get('message') or name}（钩子：{_hk.get('name') or '-'}）"}
                    self._log_tool_call(name, tool_type, arguments, result, intent_ctx, agent_ctx, conv_ctx, t0)
                    return result
                if _act == "require_confirm":
                    from hil_service import HILService
                    from database import db_conn
                    _conf = None
                    with db_conn() as conn:
                        _conf = HILService.queue_confirmation(
                            conn, agent_ctx.get("agent", ""), name, arguments or {},
                            preview=f"自动化钩子要求人工确认：{_hk.get('message') or name}，参数：{str(arguments or {})[:200]}",
                            conversation_id=conv_ctx or 0)
                    if _conf:
                        result = {"ok": True,
                                  "result": f"「{name}」已进入人工确认队列（钩子要求，确认单 #{_conf['id']}），确认后生效"}
                    else:
                        result = {"ok": False,
                                  "result": f"「{name}」需人工确认（钩子要求），请通过人工确认流程操作"}
                    self._log_tool_call(name, tool_type, arguments, result, intent_ctx, agent_ctx, conv_ctx, t0)
                    return result
                # warn：放行执行，提示随执行结果可观测（self._hook_warn 供调用方附加）
                self._hook_warn = _hk.get("message") or f"工具「{name}」触发钩子提示（{_hk.get('name') or '-'}）"
            # T8：编排子任务写操作受控——不即时执行、不即时入队，暂存到 subtask_artifacts，
            # 由编排汇总段批量挂人工确认队列（确认归属该编排 run/task_key）
            if getattr(self, "_orch_subtask", False):
                from hil_service import HILService
                if HILService.is_write_tool(name):
                    from database import get_db
                    from services.artifact_materializer import store_write_request
                    _w_run_id = int(getattr(self, "_tool_run_ctx", 0) or 0)
                    _w_task_key = str(getattr(self, "_tool_task_key", "t") or "t")
                    try:
                        _wconn = get_db()
                        try:
                            store_write_request(_wconn, _w_run_id, _w_task_key, name, arguments or {})
                        finally:
                            _wconn.close()
                        result = {"ok": True, "result": f"写操作「{name}」已暂存，汇总后统一人工确认"}
                    except Exception as _we:
                        result = {"ok": False,
                                  "result": f"写操作「{name}」暂存失败（已阻止直接执行）: {str(_we)[:100]}"}
                    self._log_tool_call(name, tool_type, arguments, result, intent_ctx, agent_ctx, conv_ctx, t0)
                    return result
            # ══════════════════════════════════════════════════════════════
            # ★ 2026-10-28（写前确认 HITL）**统一人工确认闸** —— 替换此前分散的三道闸
            #   （M5 L2 分级闸 / TR-P2b destructive 闸 / 2026-09-11 write 闸）
            #
            # ## 为什么合并成一道
            # ① 旧三道闸**判定依据互不相同**（`hil_level` / `side_effect` / `side_effect`）
            #    ⇒ 同一类操作在不同闸得到不同结论，且**彼此不覆盖**；
            # ② 实测三处断链（全部真机验证）：
            #    · `self._hil_level` 在 stream 路径**从不赋值** ⇒ L2 闸永不触发；
            #    · `_tool_side_effect` 查不到返回 "read"，而 **MCP 工具不在 tools 表**
            #      ⇒ destructive/write 闸对 `create_model_elements` 等**全部失效**；
            #    · `HILService.WRITE_TOOLS` 是硬编码 5 个名字，其中 **4 个在 tools 表里
            #      根本不存在**（真库实证）⇒ 永远匹配不到。
            # ⇒ 现在统一为：**双源写类判定**（tools.side_effect ∪ MCP 动词族）
            #    + **策略表取档位**（`services/hil_policy.evaluate`，策略即数据，
            #    灰度 = 改表不改代码）。
            #
            # ## 判据与档位的分工（口径刻意不同，不可照抄）
            # · **注入侧**（该不该递到模型眼前）＝ 恰当性 ⇒ 窄口径，只闸破坏族；
            # · **执行侧**（要不要人点头）       ＝ 授权   ⇒ 宽口径，覆盖全部写族。
            #
            # ## 位置
            # RBAC 之后（`tools.py:870-876` 既定不变量：无权限的人不该走到"要不要审批"）
            # → Hook 之后（Hook 是管理员配的**细粒度确定性**规则，比策略粗分档更具体）
            # → 执行之前（唯一能保证"没有副作用发生"的位置）。
            _hil_res = self._hil_gate(name, arguments or {}, agent_ctx, conv_ctx, tool_type)
            if _hil_res is not None:
                result = _hil_res
                self._log_tool_call(name, tool_type, arguments, result,
                                    intent_ctx, agent_ctx, conv_ctx, t0)
                return result

            # ★★ 2026-10-28：**统一写入目标网关** —— 必须位于 MCP/HTTP 两条分派**之前**。
            #   位置理由（真机 conv 9740 的教训）：真正能落库的是 MCP `import_sysml_text`，
            #   它走下面 MCP 分支、在那里 return；旧闸只挂在 HTTP 分支
            #   （`zhiyuan_sysmlv2_import`）上 ⇒ 真写入路径**完全绕过**了检查。
            #   语义见 `_import_target_gate`（先解析目标、再判定，给错时回喂正确 id）。
            _itg_res = self._import_target_gate(name, arguments or {},
                                                intent_ctx, conv_ctx)
            if _itg_res is not None:
                self._log_tool_call(name, tool_type, arguments, _itg_res,
                                    intent_ctx, agent_ctx, conv_ctx, t0)
                return _itg_res
            # MCP 工具：从 DB 找所属服务器端点（真实 JSON-RPC 调用）
            try:
                from database import get_db
                conn = get_db()
                try:
                    row = conn.execute(
                        "SELECT * FROM mcp_servers WHERE status='online' AND tools LIKE ?",
                        (f"%{name}%",),
                    ).fetchone()
                finally:
                    conn.close()
            except Exception:
                row = None
            if row:
                from mcp_client import MCPClient
                from tool_resilience import retry_exec
                tool_type = "mcp"
                # ★ B5-2：带上该 MCP 配置的认证头（此前硬编码 ⇒ Bearer 类必 401）
                # ⚠️ 本模块的 json **不是全局导入**（第 899 行是函数内 `import json as _json`），
                #   这里直接用 `json.` 会在运行时 NameError ⇒ 必须用局部别名导入。
                _mh = {}
                try:
                    import json as _json_mcp
                    _mh = _json_mcp.loads(row["headers"] or "{}") or {}
                except Exception:
                    _mh = {}
                client = MCPClient(row["endpoint"], row["transport"] or "sse",
                                   headers=_mh if isinstance(_mh, dict) else {})
                # D2：MCP 工具按注册表 retry_policy 做指数退避重试（仅瞬态失败）
                result, retried, _did = retry_exec(
                    lambda: client.call_tool(name, arguments or {}),
                    self._retry_policy_for(name))
                if retried:
                    result = {**result, "retried": retried}
                # P0-a：source_ref=mcp_servers.name ⇒ capability_id=mcp:{server}:{tool}
                self._log_tool_call(name, tool_type, arguments, result, intent_ctx, agent_ctx, conv_ctx, t0,
                                    source_ref=row["name"])
                return result
            # 通用 HTTP 集成工具（tools.kind='http'，配置驱动执行器——适配建模软件等外部系统，零代码接入）
            try:
                from database import get_db
                conn = get_db()
                try:
                    _zrow = conn.execute(
                        "SELECT 1 FROM tools WHERE name=? AND kind='http' AND status='active'", (name,)
                    ).fetchone()
                finally:
                    conn.close()
            except Exception:
                _zrow = None
            if _zrow:
                from http_tool_executor import exec_http_tool
                from tool_resilience import retry_exec
                tool_type = "http"
                # ★ 2026-10-28（conv 9740 实证）：**覆盖导入的目标一致性闸**。
                #   ⚠️ 已上移为**统一网关**（`_import_target_gate`），位于 MCP/HTTP
                #   两条分派之前 —— 因为真正能落库的是 MCP 的 `import_sysml_text`，
                #   而它走的是 MCP 分支（在本段之前就 return 了），只在这里拦等于漏拦。
                # D2：HTTP 集成工具重试（仅瞬态失败）
                result, retried, _did = retry_exec(
                    lambda: exec_http_tool(name, arguments or {}),
                    self._retry_policy_for(name))
                if retried:
                    result = {**result, "retried": retried}
                self._log_tool_call(name, tool_type, arguments, result, intent_ctx, agent_ctx, conv_ctx, t0)
                return result
            # 智源工具（tools.source='zhiyuan'）：路由到 zhiyuan_client.exec_tool
            # 即使平台 base_url/token 未配置，client 也会返回结构化错误（LLM 可恢复）
            try:
                from database import get_db
                conn = get_db()
                try:
                    _zhiyuan = conn.execute(
                        "SELECT 1 FROM tools WHERE name=? AND source='zhiyuan' AND status='active'", (name,)
                    ).fetchone() is not None
                finally:
                    conn.close()
            except Exception:
                _zhiyuan = False
            if _zhiyuan:
                from zhiyuan_client import exec_tool as _exec_zhiyuan
                tool_type = "zhiyuan"
                result = _exec_zhiyuan(name, arguments or {})
                self._log_tool_call(name, tool_type, arguments, result, intent_ctx, agent_ctx, conv_ctx, t0)
                return result
            # 基础通用文件操作工具（file_*：系统文件读写/增删，双链路共用 file_tools.exec_file_tool）
            if name in _FILE_TOOL_NAMES:
                result = exec_file_tool(name, arguments or {})
                self._log_tool_call(name, tool_type, arguments, result, intent_ctx, agent_ctx, conv_ctx, t0)
                return result
            # ★ 2026-10-17（米爸需求）：web_search/web_fetch——联网获取最新消息。
            #   read-only 免确认；时效性问题（最新/今天/新闻）AI 应主动调用。
            if name in _WEB_TOOL_NAMES:
                import web_tools as _wt
                result = _wt.exec_tool(name, arguments or {})
                self._log_tool_call(name, tool_type, arguments, result, intent_ctx, agent_ctx, conv_ctx, t0)
                return result
            # ★ 2026-10-17（米爸能力配置）：save_artifact——AI 显式归档产物到产物库。
            #   file_write 只写沙箱磁盘、进不了产物库（9446 实证 AI 自述"没有写入能力"
            #   的真正缺口）；harness 兜底只认完整文件形态，分段展示/片段不命中。
            #   ⇒ 本工具是模型主动通道：技能 48「文件操作」声明、agent 绑定技能后经
            #   白名单注入，写意图闸按技能契约放行。
            if name == "save_artifact":
                import artifact_tools as _at2
                result = _at2.exec_tool(name, arguments or {}, conversation_id=conv_ctx)
                self._log_tool_call(name, tool_type, arguments, result, intent_ctx, agent_ctx, conv_ctx, t0)
                return result
            # 基础通用报告导出工具（report_export：内容→md/docx/pdf，双链路共用 report_tools.exec_report_tool）
            if name in _REPORT_TOOL_NAMES:
                result = exec_report_tool(name, arguments or {})
                self._log_tool_call(name, tool_type, arguments, result, intent_ctx, agent_ctx, conv_ctx, t0)
                return result
            # 系统管理域只读查询工具（2026-08-31 P0-4：对话式系统管理——用户/角色/权限/审计/监控/会话，只读安全）
            if name.startswith("sys_query_"):
                from sysadmin_tools import exec_tool as _exec_sysadmin
                result = _exec_sysadmin(name, arguments or {})
                self._log_tool_call(name, tool_type, arguments, result, intent_ctx, agent_ctx, conv_ctx, t0)
                return result
            # 图数据库只读查询工具（2026-09-01 P2：图查询消费侧——SPARQL 只读，图谱优先问答）
            if name.startswith("graph_db_"):
                from graph_db_tools import exec_tool as _exec_graph_db
                result = _exec_graph_db(name, arguments or {})
                self._log_tool_call(name, tool_type, arguments, result, intent_ctx, agent_ctx, conv_ctx, t0)
                return result
            # 覆盖性分析工具（2026-09-20 SRS-GN-CO：5 个只读确定性工具——矩阵/追溯链/场景/缺项/建模过程覆盖，
            # 数字可复现可审计，解读与补全建议由 Agent 层完成。注意按名集合路由：
            # 五个工具名只有 coverage_matrix 带 coverage_ 前缀，前缀匹配会漏掉其余四个）
            if name in ("coverage_matrix", "trace_chain_check", "scene_coverage", "gap_summary", "modeling_coverage"):
                from coverage_tools import exec_tool as _exec_coverage
                result = _exec_coverage(name, arguments or {})
                self._log_tool_call(name, tool_type, arguments, result, intent_ctx, agent_ctx, conv_ctx, t0)
                return result
            # SysML v2 结构化抽取 / 方法论规约卡（2026-10-08新增）。
            # 这两个工具名**不带 sysml_v2_ 前缀**（历史前缀表已固定，改前缀会波及
            # 已有绑定），故需独立分流分支。放在 sysml_v2_ 分支之前以免被误吞。
            if name == "sysml_ast_extract":
                try:
                    import sysml_ast_tools as _at
                    result = _at.exec_tool(name, arguments or {})
                except Exception as exc:    # noqa: BLE001
                    result = {"ok": False,
                              "result": f"结构抽取模块不可用：{type(exc).__name__}: {exc}。"
                                        f"⚠️ 这不代表模型里没有元素 —— 请如实说明"
                                        f"『未能解析』，不要用正则猜测代替。"}
                self._log_tool_call(name, tool_type, arguments, result,
                                    intent_ctx, agent_ctx, conv_ctx, t0)
                return result
            if name == "methodology_profile_load":
                try:
                    import methodology_profile_tools as _mp
                    result = _mp.exec_tool(name, arguments or {})
                except Exception as exc:    # noqa: BLE001
                    result = {"ok": False,
                              "result": f"方法论模块不可用：{type(exc).__name__}: {exc}。"
                                        f"⚠️ 无法加载规约卡 —— 不要凭记忆假设方法论要求。"}
                self._log_tool_call(name, tool_type, arguments, result,
                                    intent_ctx, agent_ctx, conv_ctx, t0)
                return result
            # ⚠️ 以下两个工具名**不带 sysml_v2_ 前缀**（注册名如此），
            #    故走不到上面的前缀分支 —— 必须各自分流，否则会「入库并绑定了却
            #    永远路由不到」（静默失效，2026-10-08 实测发现）。
            if name == "sysml_import_graph":
                try:
                    import sysml_import_graph_tools as _ig
                    result = _ig.exec_tool(name, arguments or {})
                except Exception as exc:    # noqa: BLE001
                    result = {"ok": False,
                              "result": f"落库模块不可用：{type(exc).__name__}: {exc}。"
                                        f"⚠️ 模型**未落库** —— 请如实说明『未能落库』。"}
                self._log_tool_call(name, tool_type, arguments, result,
                                    intent_ctx, agent_ctx, conv_ctx, t0)
                return result
            if name == "sysml_stdlib_meta":
                try:
                    # ⚠️ 2026-10-21：别名 `_st` 与上方 stats 字典局部变量重名，
                    #   踩 e2e G0 文件级危险名集合（跨/同函数重名均报）⇒ 改唯一名。
                    import sysml_stdlib_tools as _sysml_meta
                    result = _sysml_meta.exec_tool(name, arguments or {})
                except Exception as exc:    # noqa: BLE001
                    result = {"ok": False,
                              "result": f"标准库模块不可用：{type(exc).__name__}: {exc}。"
                                        f"⚠️ 这不代表标准库为空 —— 请如实说明"
                                        f"『未能查询标准库』，不要臆造元类。"}
                self._log_tool_call(name, tool_type, arguments, result,
                                    intent_ctx, agent_ctx, conv_ctx, t0)
                return result
            # SysML v2 校验工具（2026-09-19：AI 建模闭环的「暴露 + 回喂」段）。
            # 生成端把校验当**工具**调用 → 拿到三路诊断（词法/语法/语义）→ 自行修复 → 再校验，
            # 轮次上限复用本模块已有的 ReAct `max_tool_rounds`（= 3），故**零新循环**。
            # 进程内直通 checker.jar，不走 HTTP（避免自环死锁，同 mbse_pull_ingest 的理由）。
            if name.startswith("sysml_v2_"):
                from sysml_check_tools import exec_tool as _exec_sysml_check
                result = _exec_sysml_check(name, arguments or {})
                # 2026-09-20：缓存「最近一次校验过的代码」。LLM 学会「先校验再交付」后，代码会
                # 出现在**工具参数**里而回答正文只剩结论（复盘缺陷④）——交付通道（视图投影 /
                # 版本留痕 / 编排补回）只认正文，于是模型越规范越交付不出来。
                # 这里挂到管线实例上，由 cards._ensure_sysml_from_tools / _gen_sysml_views 兜底取回。
                # 优先取 verdict=pass 的那份（= 修好之后的最终模型）。
                _code = (arguments or {}).get("code")
                if isinstance(_code, str) and _code.strip():
                    self._sysml_last_checked_code = _code
                    if result.get("verdict") == "pass":
                        self._sysml_last_pass_code = _code
                self._log_tool_call(name, tool_type, arguments, result, intent_ctx, agent_ctx, conv_ctx, t0)
                return result
            # 内置工具 handler
            if name == "graph_retrieve":
                q = (arguments or {}).get("query", "")
                r = self.rag.retrieve(q)
                _chunk_hits = r.get("chunk_hits") or []
                hits = _chunk_hits or r.get("entities") or []
                # ★ 2026-10-28（用户报「知识库明明有 OMG 规范讲 perform action，
                #   检索却说未检索到」）：判定逻辑整体移到纯函数模块。
                #
                #   旧实现三条独立缺陷（任一条都足以让检索"看起来失效"）：
                #   ① `q_tokens` 只抽中文 ⇒ 英文术语查询 `not q_tokens` 恒真 ⇒ 必判弱
                #   ② 命中的是英文规范 ⇒ overlap 恒 0 ⇒ 中文查英文文档必判弱
                #   ③ `max_score < 0.5`：score 是 RRF 融合分 1/(60+rank)，
                #      理论最大 ≈2/61=0.033 ⇒ 该条件**恒真**，等于没写
                #   实测：vec_score 0.57~0.72（置信度中/高）的正确命中被判成
                #   「未检索到实质相关内容（词重叠 0/0）」= 用户看到的原话。
                from agent.retrieval_relevance import assess_relevance as _assess
                try:
                    from core import config as _cfg_rr
                    _conf_mid = float(_cfg_rr.get("rag", "confidence_mid", 0.45))
                except Exception:
                    _conf_mid = 0.45
                _rel = _assess(q, _chunk_hits, conf_mid=_conf_mid)
                _ev = _rel["evidence"]
                if _rel["weak"]:
                    detail = ("知识库未检索到与查询实质相关的内容"
                              f"（{_rel['reason']}），请基于用户上下文与上传资料回答")
                    result = {"ok": True, "result": detail, "weak": True,
                              "relevance": _rel}  # P0-按需工具：弱相关标记 → 上层收敛工具循环
                else:
                    # ★ 同时修「检索到了但答不出」：旧实现只给 hits[:3] 各 60 字符
                    #   （共 180 字），LLM 拿不到条文全文 ⇒ 只能答"信息不足"。
                    #   现改为给 top 4 条的**完整切片内容**（每条约 300 字）并标注来源，
                    #   让模型能真正引用规范原文。
                    _top = [h for h in _chunk_hits[:4] if isinstance(h, dict)]
                    _lines = [
                        f"知识库检索到 {len(_chunk_hits)} 条相关片段"
                        f"（最高向量相似度 {_ev['max_vec']:.3f}，"
                        f"最高 BM25 {_ev['max_bm25']:.1f}）。"
                        f"以下为原文摘录，回答时请引用具体条文："
                    ]
                    for _i, _h in enumerate(_top, 1):
                        _src = _h.get("source_doc") or "（未知来源）"
                        _sec = str(_h.get("section") or "").strip()
                        _sec = f" · {_sec}" if _sec else ""
                        _conf = _h.get("confidence_level") or "-"
                        _lines.append(
                            f"\n【来源 {_i}】{_src}{_sec}"
                            f"（相似度 {float(_h.get('vec_score') or 0):.3f}，置信度 {_conf}）\n"
                            f"{str(_h.get('content') or '').strip()}"
                        )
                    result = {"ok": True, "result": "\n".join(_lines),
                              "relevance": _rel, "hit_count": len(_chunk_hits)}
                self._log_tool_call(name, tool_type, arguments, result, intent_ctx, agent_ctx, conv_ctx, t0)
                return result
            if name == "impact_analyze":
                q = (arguments or {}).get("query", "")
                card = self._card_impact(self.rag.retrieve(q), None, q)
                if card.get("ok") is False:
                    result = {"ok": True, "result":
                              f"影响分析未完成：{card.get('reason', '')}（引导：{card.get('actions', [])}）", "card": card}
                else:
                    result = {"ok": True, "result":
                              f"直接影响 {card.get('direct_count', 0)} 条 / 间接 {card.get('indirect_count', 0)} 条"
                              f"（高 {card.get('impact_levels', {}).get('high', 0)} / 中 "
                              f"{card.get('impact_levels', {}).get('mid', 0)} / 低 {card.get('impact_levels', {}).get('low', 0)}）",
                              "card": card}
                self._log_tool_call(name, tool_type, arguments, result, intent_ctx, agent_ctx, conv_ctx, t0)
                return result
            if name == "validate":
                card = self._card_review(self.rag.retrieve((arguments or {}).get("query", "")))
                result = {"ok": True, "result": f"评分 {card.get('score',0)}，问题 {len(card.get('issues',[]))} 项"}
                self._log_tool_call(name, tool_type, arguments, result, intent_ctx, agent_ctx, conv_ctx, t0)
                return result
            # ★ 2026-10-13：**移除了 `conflict_check` 的假实现**。
            #   此前此处是 `return {"ok":True, "result":"冲突检测完成，无新冲突"}` ——
            #   **无条件返回"无冲突"，与传入什么参数完全无关**，属假成功：
            #   模型（`预评审Agent` 15 次 / `需求分析Agent` 2 次）据此得出"已检测、无冲突"的结论，
            #   而实际从未做过任何检测（17 次调用的 `arguments` **全是 `{}`**，连 query 都没传）。
            #   即"冲突检测"在本路径上等于一个**会说漂亮话的空气工具**。
            #   ⚠️ 为什么连带把 builtin_desc / registry / seeds 里的登记也一起去掉：
            #     只删本分支会让工具仍被注入 payload、却落到下方兜底 ⇒ 返回
            #     「已注册但未配置执行器」——**把"假成功"换成"报错"，那是回归不是修复**。
            #     两者必须同时动，让"模型看不到这个工具"成为一致的事实。
            #   ⚠️ 真冲突检测走**另一条路**：`entity_resolver.detect_conflicts()`
            #     （写入 knowledge_conflicts + 人工裁决），以及 `workflows/tools.py` 里那套
            #     （会真检索图谱、比对同名实体），二者均**未改动**。
            # TR-P3：DB 注册表自定义工具——已注册但未配置执行器 → typed error（LLM 可恢复，非 500）
            _registered = False
            try:
                from database import get_db
                conn = get_db()
                try:
                    _registered = conn.execute(
                        "SELECT 1 FROM tools WHERE name=? AND status='active'", (name,)).fetchone() is not None
                finally:
                    conn.close()
            except Exception:
                pass
            if _registered:
                result = {"ok": False,
                          "result": f"工具「{name}」已注册但未配置执行器（仅内置工具与在线 MCP 工具可执行），"
                                    f"请基于已有能力完成，或联系管理员为它配置执行器"}
            else:
                result = {"ok": False, "result": f"未知工具: {name}"}
            self._log_tool_call(name, tool_type, arguments, result, intent_ctx, agent_ctx, conv_ctx, t0)
            return result
        except Exception as e:
            result = {"ok": False, "result": f"工具执行失败: {str(e)[:200]}"}
            self._log_tool_call(name, tool_type, arguments, result, intent_ctx, agent_ctx, conv_ctx, t0)
            return result

    def _retry_policy_for(self, name: str) -> dict:
        """D2：读取工具重试策略（tools.retry_policy），实例级缓存。"""
        if name in self._retry_policy_cache:
            return self._retry_policy_cache[name]
        from tool_resilience import parse_policy
        policy = dict(parse_policy("{}"))
        try:
            from database import get_db
            conn = get_db()
            try:
                row = conn.execute(
                    "SELECT retry_policy FROM tools WHERE name=? AND status='active'", (name,)).fetchone()
                if row:
                    policy = parse_policy(row["retry_policy"] or "{}")
            finally:
                conn.close()
        except Exception:
            pass
        self._retry_policy_cache[name] = policy
        return policy

    def _log_tool_call(self, name, tool_type, arguments, result, intent_ctx, agent_ctx, conv_ctx, t0,
                       source_ref=""):
        """优化1：工具调用可观测——落库 tool_call_logs（失败不阻断主流程）。

        P0-2（2026-10-06）两处补强：
        - **补 user_name**：此前本表无操作者字段 ⇒ 只能回答「哪个会话调的」，
          回答不了「**是谁**调的」（文章讲的「业务方追着问到底是谁改的」那个必答项）。
        - **参数脱敏**：`arguments` 此前**原样入库**，而 http 集成工具的入参可能含
          鉴权头、MCP 配置含 token ⇒ 审计日志会变成新的泄露源（core/redact.py 文件头）。
          脱敏在**序列化之后**做（对最终落库文本生效），键名保留、只遮值。

        P0-a（2026-10-24）：补 capability_id（capability_registry 统一注册处关联列）。
        口径同 core/capability_registry：mcp → mcp:{server}:{tool}（server 名由 MCP 分支
        经 source_ref 传入，该分支已反查出 mcp_servers 行）；skill → skill:{id}（反查，
        skills.name 无 UNIQUE 不能用 name 当键）；其余分支 → tool:{name}（这些分支的
        前提就是 tools 表有该行）。反查失败留空——不造假映射。
        """
        try:
            import time as _time
            from database import db_conn
            import json as _json
            latency = int((_time.time() - t0) * 1000)
            # P0-3：脱敏。失败不阻断（旁路原则），但要留痕让人知道没脱敏成功。
            # ★ 2026-10-27（截断清单 P0-B3）：结果落库上限 2000 → 8000，且**截断可观测**。
            #   原 2000 是**静默**截断：我据此统计"工具结果都在 2000 以内"，把
            #   `mcp.max_response=4000` 造成的丢失误判成"没有大结果"，直接诊断跑偏一次。
            #   现口径：`result` 仍存文本（保持既有解析口径不变），另落 `result_len`
            #   （原始长度）与 `result_truncated`（是否被裁）两列 ⇒ 任何"看到的是不是全部"
            #   都能用 SQL 直接回答，不靠猜。
            _LOG_RES_CAP = 8000
            _raw_res = str(result.get("result", ""))
            _res_len = len(_raw_res)
            _res_trunc = 1 if _res_len > _LOG_RES_CAP else 0
            try:
                from core.redact import redact_text as _redact
                _args_txt = _redact(_json.dumps(arguments or {}, ensure_ascii=False)[:2000])
                _res_txt = _redact(_raw_res[:_LOG_RES_CAP])
            except Exception:
                _args_txt = _json.dumps(arguments or {}, ensure_ascii=False)[:2000]
                _res_txt = "[redact-failed] " + _raw_res[:_LOG_RES_CAP]
            # P0-2：操作者。口径同 audit_logs.user_name（display_name，回落 username）。
            _uname = ""
            try:
                from core.audit import audit_user as _au
                _uname = _au(getattr(self, "_tool_user", None)) or ""
            except Exception:
                pass
            with db_conn() as conn:
                # B2-12（2026-10-13）补编排归因三列。**关键点：run_id/task_key 早就挂在
                # self 上**（stream.py:497-498 设 `_tool_run_ctx`/`_tool_task_key`，
                # 原先只有"写操作暂存"那条路径消费，见 tools.py:546-547），
                # 本表 8 个调用点却一个都没用 ⇒ 工具调用日志**无法归到某次编排**。
                # 不改签名：8 处调用点各自加参数风险远大于收益，直接从 self 读。
                _run_id = int(getattr(self, "_tool_run_ctx", 0) or 0)
                _task_key = str(getattr(self, "_tool_task_key", "") or "")
                # ★ P0-3（2026-10-27）：本轮 trace_id（与 llm_usage_stats.trace_id **同源**）。
                #   此前本表只有 run_id/task_key，而主对话路径**没有编排批次**
                #   ⇒ 83.2% 的行 run_id=0，且与 LLM 使用量统计是两套 id、无法 JOIN
                #   ⇒ 回答不了「这一轮对话调了哪些能力、花了多少钱」。
                #   两级取值：实例属性（stream.py 入口落的）→ ContextVar（线程/子任务兜底）。
                _trace_id = str(getattr(self, "_tool_trace_id", "") or "")
                if not _trace_id:
                    try:
                        from core.audit import current_trace_id as _ctid
                        _trace_id = _ctid()
                    except Exception:
                        _trace_id = ""
                # failure_kind：复用 B1-6 的分类器（不另造第二份词表——两份必然漂移）。
                # 只在失败时写，成功留空（避免"成功"也带一个无意义的分类）。
                # ★ 2026-10-18 修（P1-2 步骤级归因器真库取证时发现）：原写法
                #   `_fc(classify(text, ok=False))` 把分类器**嵌套调用两次**——
                #   内层返回 kind 串（如 'timeout'），外层 `classify(kind)` 的 ok
                #   默认 True 直接返 '' ⇒ 失败行的 failure_kind **恒为空**（真库
                #   273 条 ok=0 全空，归因器的 kind 维度对工具步整体失效）。
                #   修为单次调用并显式传 ok=False。
                _fkind = ""
                if not result.get("ok"):
                    try:
                        # ⚠️ 2026-10-21：别名 `_fc` 与 _build_tools_def 的 DB 连接
                        #   局部变量重名，踩 e2e G0 文件级危险名集合 ⇒ 改唯一名。
                        from services.failure_class import classify as _fc_classify
                        _fkind = _fc_classify(
                            _res_txt if _res_txt and not _res_txt.startswith("[redact-failed]")
                            else str(result.get("error") or _res_txt or ""),
                            ok=False)
                    except Exception:
                        _fkind = ""
                # P0-a：capability_id（统一注册处关联）。skill 反查一次（name 不唯一）；
                # 反查不到留空（不造假映射）。
                _cid = ""
                try:
                    if tool_type == "mcp" and source_ref:
                        _cid = "mcp:%s:%s" % (source_ref, name)
                    elif tool_type == "skill":
                        _sid = conn.execute(
                            "SELECT id FROM skills WHERE name=? ORDER BY id LIMIT 1",
                            (name,)).fetchone()
                        _cid = ("skill:%s" % _sid[0]) if _sid else ""
                    else:
                        _cid = "tool:%s" % name
                except Exception:
                    _cid = ""
                conn.execute(
                    "INSERT INTO tool_call_logs (intent, agent_name, tool_name, tool_type, arguments, result, ok, latency_ms, conversation_id, user_name, run_id, task_key, failure_kind, capability_id, trace_id, result_len, result_truncated) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (intent_ctx.get("intent", ""), agent_ctx.get("agent", ""),
                     name, tool_type, _args_txt, _res_txt,
                     1 if result.get("ok") else 0,
                     latency, conv_ctx or 0, _uname, _run_id, _task_key, _fkind, _cid, _trace_id,
                     _res_len, _res_trunc),
                )
        except Exception:
            pass  # 观测落库失败不影响工具执行
        # P0-b：闭 tool span（sid=0 ⇒ LIFO 弹线程栈顶，即本次工具）。
        # 放在**方法级**而非 DB try 内：落库失败/异常出口也必须闭 span，否则
        # 线程栈泄漏 ⇒ 后续工具的 parent 挂到已闭节点上、end 时悬挂。
        # latency 用真实执行时长（t0 起算），与 tool_call_logs.latency_ms 同源。
        try:
            import time as _t2
            from services.orch_span import current_tracer as _ct
            _tracer = _ct()
            if _tracer is not None:
                _tracer.close_tool(0, ok=bool(result.get("ok")),
                                   latency_ms=int((_t2.time() - t0) * 1000))
        except Exception:
            pass   # 观测收口失败绝不阻断工具执行
        # ★ P0-5（2026-10-28）障碍恢复：给**失败结果**追加「这属于什么障碍 / 该怎么恢复」。
        #
        # 为什么加在这里（而不是各失败出口）：
        #   `_exec_tool_call` 有 10+ 个失败出口（白名单/RBAC/HIL/超时/异常…），
        #   逐个补必然漏；而**所有出口都汇入本方法** ⇒ 改这一处，一条不错。
        #   （本仓已用同一理由把 tool span 的开闭点放在这里。）
        #
        # 为什么追加到 result["result"]（而不是只写日志）：
        #   模型看到的是 result["result"]。失败原因若只说「失败」，
        #   模型只能选择"问用户"；给出障碍类型 + 恢复动作，它才能自己换路径。
        #   实测依据（真实样本 #3173）：模型的 next_step 是「刷新凭据后重新调用」——
        #   说明它**知道**该自恢复，只是系统没给它这个信号，它只能绕回问人。
        #
        # ⚠️ 不动 result["ok"]：失败仍是失败，只是**附带恢复建议**。
        # ⚠️ 权限类失败（permission_denied/permission_error）不追加 —— 那确实该找人。
        try:
            _ob_on = False
            try:
                from core.config import as_bool as _as_bool
                _ob_on = bool(_as_bool("agent", "obstacle_recovery_enabled", False))
            except Exception:
                _ob_on = False
            if (_ob_on and isinstance(result, dict) and not result.get("ok")
                    and not result.get("permission_denied")
                    and not result.get("permission_error")):
                from agent import agent_todo as _at
                _etxt = str(result.get("result") or result.get("error") or "")
                _cls = _at.classify_obstacle(_etxt)
                _ACTION_TXT = {
                    "retry": "重试（可能是瞬时故障）",
                    "refresh_retry": "刷新凭据后重试一次",
                    "repath": "换路径：先用 list/search 定位到正确对象再重试",
                    "refetch_replay": "重新获取最新坐标/版本后重放",
                    "downgrade": "换等价工具或缩小范围后重试",
                    "ask_user": "向用户澄清",
                }
                _hint = ("\n\n【障碍恢复建议】本失败被判定为「%s」类：%s。"
                         % (_cls["kind"], _ACTION_TXT.get(_cls["action"], _cls["action"])))
                if not _cls["ask_user_allowed"]:
                    _hint += ("\n该类问题**用现有工具可以自行解决** —— "
                              "请按上面的恢复动作再试一次，**不要**直接问用户。")
                else:
                    _hint += "\n该类信息只有用户能提供，可以调用 ask_user 澄清。"
                result["result"] = _etxt + _hint
                result["obstacle"] = _cls
        except Exception:
            pass   # 障碍提示失败绝不阻断工具结果返回

    def _resolve_req_scope(self, scope_ids, scope_id, scope, agent_def):
        """解析建模范围（文件管理=全局数据，不分分支）。
        支持多个已保存范围合并：scope_ids(list) + scope_id(单) + scope(内联)。
        返回 (req_scope, scope_att, effective_kb_scope)。G1 硬锁文档；G2/G3 片段证据注入。"""
        req_scope = None; scope_att = ''
        effective_kb_scope = dict(agent_def.kb_scope or {})
        ids = []
        if scope_ids:
            ids += [int(i) for i in scope_ids if str(i).isdigit()]
        if scope_id and int(scope_id) not in ids:
            ids.append(int(scope_id))
        if ids or scope:
            try:
                from database import db_conn as _sdb
                from services.scope_service import resolve_scope
                with _sdb() as _sc:
                    docset = set(); frags = []; ok_parts = False
                    for sid in ids:
                        rr = resolve_scope(_sc, scope_id=sid, scope=None)
                        if rr and rr.get('ok'):
                            ok_parts = True
                            docset.update(rr.get('docs') or [])
                            if rr.get('fragments'): frags.append(rr['fragments'])
                    if scope:
                        rr = resolve_scope(_sc, scope_id=None, scope=scope)
                        if rr and rr.get('ok'):
                            ok_parts = True
                            docset.update(rr.get('docs') or [])
                            if rr.get('fragments'): frags.append(rr['fragments'])
                if ok_parts:
                    req_scope = {'ok': True, 'mode': 'fragment_group' if frags else 'doc',
                                 'docs': sorted(docset), 'fragments': '\n\n'.join(frags), 'chunk_ids': []}
                    if docset:
                        effective_kb_scope['docs'] = sorted(docset)
                        effective_kb_scope['mode'] = req_scope['mode']
                    if req_scope.get('fragments'):
                        scope_att = req_scope['fragments']
            except Exception:
                req_scope = None
        return req_scope, scope_att, effective_kb_scope
