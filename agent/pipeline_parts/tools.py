# -*- coding: utf-8 -*-
"""AgentPipeline Mixin：工具定义、执行、重试策略与调用留痕。

由 tools/split_pipeline.py 从 agent/pipeline.py 机械切分，勿手工编辑方法体。"""
from .common import *


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
        builtin_desc = {
            "graph_retrieve": "知识图谱检索（GraphRAG：实体链接 + 子图遍历 + 向量降级），参数 query:str",
            "conflict_check": "冲突检测（新元素与既有元素属性冲突）",
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
        }
        candidates = []  # (name, desc, type, server_name_or_None)
        # MCP 绑定工具
        for m in bound:
            for t in (m.get("tools") or []):
                candidates.append((t, f"MCP 工具（{m.get('name','')}），端点 {m.get('endpoint','')}",
                                   "mcp", m.get("name", "")))
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
                    "SELECT t.name, t.description, t.input_schema, t.side_effect FROM tools t "
                    "JOIN agent_tools at ON at.tool_name=t.name JOIN agents a ON at.agent_id=a.id "
                    "WHERE a.name=? AND at.tool_type='tool' AND at.enabled=1 AND t.status='active' "
                    "AND t.source!='builtin'", (intent,)).fetchall():
                    desc = row["description"] or f"自定义工具 {row['name']}"
                    if row["side_effect"] == "write":
                        desc += "（写操作，需人工确认）"
                    elif row["side_effect"] == "destructive":
                        desc += "（高风险操作，必须人工确认）"
                    if not any(c[0] == row["name"] for c in candidates):
                        candidates.append((row["name"], desc, "db_tool", ""))
            finally:
                conn.close()
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
                candidates = [c for c in candidates if _keep_t("tools", _name2id.get(c[0], -1))]
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
        # P0-3：委派最小权限——本次调用指定的工具白名单（Worker ⊆ Supervisor），
        # 白名单模式下不做「读类核心工具保底」，避免突破最小权限
        _wl = getattr(self, "_tool_whitelist", None)
        if _wl:
            _wlset = set(_wl)
            candidates = [c for c in candidates if c[0] in _wlset]
        # P0-按需工具：写类工具默认不注入（side_effect=write/destructive）——
        # 仅当输入含明确写意图关键词（创建/入库/保存/修改/删除…）时注入，
        # 防「无关写工具被注入 / LLM 顺手调用」；白名单（委派最小权限）模式下跳过
        if not _wl:
            _write_intent_kw = ("创建", "新增", "添加", "入库", "写入", "保存", "修改", "更新", "删除",
                                "录入", "登记", "生成实体", "建立实体", "落地", "持久化", "导入")
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
                candidates = [c for c in candidates
                              if _se_map.get(c[0], "read") not in ("write", "destructive")
                              or (_sk_wl and c[0] in _sk_wl)]
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
        if len(candidates) >= jit_threshold and user_input:
            try:
                from semantic import SemanticSearch
                from core import config as _cfg
                idx = [{"name": c[0], "text": f"{c[0]} {c[1]}"} for c in candidates]
                # P1a-1：对齐 Semantic Tool Selection——top_k + threshold + 空回退
                # 2026-09-19：阈值按**本次实际走的路**选（两路余弦量纲不同，一套阈值必有一路失准）：
                #   dense（真 embedding）→ tool_threshold_dense（0.53，分位等价标定）
                #   bigram（降级）      → tool_threshold（0.12，原经验值）
                # 弱相关 → 不预筛，维持全量注入（保底可靠）
                _ss = SemanticSearch()
                _th_b = float(_cfg.get("embedding", "tool_threshold", 0.12) or 0.12)
                _th_d = float(_cfg.get("embedding", "tool_threshold_dense", 0.53) or 0.53)
                scored = _ss.rank(user_input, idx,
                                  top_k=int(_cfg.get("embedding", "tool_top_k", jit_top_n)),
                                  threshold=0, key="text")
                _th = _th_d if getattr(_ss, "last_backend", "bigram") == "dense" else _th_b
                chosen = {s[1]["name"] for s in scored if s[0] >= _th}
                if not chosen:
                    # 空回退：全弱相关时维持全量（不瘦身，保底可靠）——
                    # core_keep 保底只能在「语义确有命中」时叠加，否则 chosen 永非空、空回退失效
                    candidates = [c for c in candidates]
                else:
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
        for name, desc, ctype, server in candidates:
            if ctype == "mcp":
                tools.append({
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": desc,
                        "parameters": {"type": "object", "properties": {},
                                       "additionalProperties": True},
                    },
                })
            else:
                tools.append({
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": desc,
                        "parameters": {"type": "object", "properties": {},
                                       "additionalProperties": True},
                    },
                })
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

    def _exec_tool_call(self, name: str, arguments: dict) -> dict:
        """缺口B：执行单个工具调用（内置 handler / MCP tools/call），返回标准化结果。

        优化1：每次调用落库 tool_call_logs（可观测），异常同样记录。
        """
        import time as _time
        intent_ctx = getattr(self, "_tool_intent_ctx", None) or {}
        agent_ctx = getattr(self, "_tool_agent_ctx", None) or {}
        conv_ctx = getattr(self, "_tool_conv_ctx", 0)
        t0 = _time.time()
        tool_type = "builtin"
        try:
            # P0-3：委派最小权限防御——白名单外的工具直接拒绝（即使 tool def 未注入，模型也可能臆造）
            _wl0 = getattr(self, "_tool_whitelist", None)
            if _wl0 and name not in _wl0:
                result = {"ok": False,
                          "result": f"工具「{name}」不在本次子任务授予的工具白名单内（最小权限，Worker ⊆ Supervisor），已拒绝执行"}
                self._log_tool_call(name, tool_type, arguments, result, intent_ctx, agent_ctx, conv_ctx, t0)
                return result
            # 确定性 Hooks：PreToolUse 强制校验（对齐 Claude Code，不依赖 LLM 概率；故障静默放行）
            # 优先级：白名单拒绝（最小权限）→ 本钩子（block/require_confirm/warn）→ HIL L2 → destructive → 执行
            self._hook_warn = ""
            # V2.6：进程内工具（避免 HTTP 自环死锁）——mbse_pull_ingest 直通入库
            # （拉取动作即用户对话内的显式拍板，不进 HIL 确认队列；结果含完整入库统计）
            if name == "mbse_pull_ingest":
                try:
                    from database import get_db as _gdb
                    from services.knowledge_service import KnowledgeService as _KS
                    _args = arguments or {}
                    _c = _gdb()
                    try:
                        _r = _KS(_c).zhiyuan_pull_ingest(
                            actor=str((agent_ctx or {}).get("agent") or "ai"),
                            vc=str(_args.get("vc") or ""),
                            package_data_id=_args.get("package_data_id"),
                            target_branch=str(_args.get("target_branch") or "personal"))
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
            # M5：HIL 强制——L2 Agent 的写工具操作进入人工确认队列（不直接执行）
            if getattr(self, "_hil_level", "L0") == "L2":
                from hil_service import HILService
                if HILService.is_write_tool(name):
                    from database import db_conn
                    with db_conn() as conn:
                        conf = HILService.queue_confirmation(
                            conn, agent_ctx.get("agent", ""), name, arguments or {},
                            preview=f"Agent 请求执行写操作 {name}，参数：{str(arguments or {})[:200]}",
                            conversation_id=conv_ctx or 0)
                        if conf:
                            result = {"ok": True,
                                      "result": f"写操作「{name}」已进入人工确认队列（确认单 #{conf['id']}），确认后生效"}
                            self._log_tool_call(name, tool_type, arguments, result, intent_ctx, agent_ctx, conv_ctx, t0)
                            return result
            # TR-P2b：副作用门控——destructive 工具必须人工确认（行业标准：模型只推荐不授权）
            if self._tool_side_effect(name) == "destructive":
                from hil_service import HILService
                from database import db_conn
                with db_conn() as conn:
                    conf = HILService.queue_confirmation(
                        conn, agent_ctx.get("agent", ""), name, arguments or {},
                        preview=f"高风险工具「{name}」需人工确认后执行，参数：{str(arguments or {})[:200]}",
                        conversation_id=conv_ctx or 0)
                if conf:
                    result = {"ok": True,
                              "result": f"高风险工具「{name}」已进入人工确认队列（确认单 #{conf['id']}），确认后执行"}
                else:
                    result = {"ok": False, "requires_confirm": True,
                              "result": f"「{name}」为 destructive 高风险工具，已拒绝直接执行，请通过人工确认流程操作"}
                self._log_tool_call(name, tool_type, arguments, result, intent_ctx, agent_ctx, conv_ctx, t0)
                return result
            # 人在回路（2026-09-11）：外部集成写工具（tools.side_effect='write'，如智源覆盖导入）
            # 一律不直接执行——进入人工确认队列，批准后由确认处理端自动执行并回写结果
            if self._tool_side_effect(name) == "write" and not getattr(self, "_orch_subtask", False):
                from hil_service import HILService
                from database import db_conn
                with db_conn() as conn:
                    conf = HILService.queue_confirmation(
                        conn, agent_ctx.get("agent", ""), name, arguments or {},
                        preview=f"写操作「{name}」需人工确认（模型只推荐不授权），参数：{str(arguments or {})[:200]}",
                        conversation_id=conv_ctx or 0)
                if conf:
                    result = {"ok": True,
                              "result": f"⏸ 写操作「{name}」已进入人工确认队列（确认单 #{conf['id']}）："
                                        f"执行前需要你在「工作流 → 执行监控 → HIL 人工确认队列」中批准，批准后自动执行并回写结果。"}
                    self._log_tool_call(name, tool_type, arguments, result, intent_ctx, agent_ctx, conv_ctx, t0)
                    return result
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
                client = MCPClient(row["endpoint"], row["transport"] or "sse")
                # D2：MCP 工具按注册表 retry_policy 做指数退避重试（仅瞬态失败）
                result, retried, _did = retry_exec(
                    lambda: client.call_tool(name, arguments or {}),
                    self._retry_policy_for(name))
                if retried:
                    result = {**result, "retried": retried}
                self._log_tool_call(name, tool_type, arguments, result, intent_ctx, agent_ctx, conv_ctx, t0)
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
                hits = r.get("chunk_hits") or r.get("entities") or []
                # Q2-修复：实质相关判断——查询词中文 token 与命中内容的重叠率 < 20% 视为弱相关，
                # 引导以用户上下文/上传资料为准，避免 LLM 拿历史无关实体硬凑分析
                q_tokens = set(re.findall(r"[\u4e00-\u9fa5]{2,4}", q or ""))
                hit_text = " ".join([(h.get("content") or "")[:500] for h in (r.get("chunk_hits") or [])])
                hit_tokens = set(re.findall(r"[\u4e00-\u9fa5]{2,4}", hit_text))
                overlap = q_tokens & hit_tokens
                ratio = len(overlap) / max(len(q_tokens), 1)
                max_score = max([h.get("score") or 0 for h in (r.get("chunk_hits") or [])], default=0)
                weak = (not hits) or (not q_tokens) or (ratio < 0.2 and max_score < 0.5)
                if weak:
                    detail = f"知识库未检索到与查询实质相关的内容（词重叠 {len(overlap)}/{len(q_tokens)}），请基于用户上下文与上传资料回答"
                    result = {"ok": True, "result": detail, "weak": True}  # P0-按需工具：弱相关标记 → 上层收敛工具循环
                else:
                    detail = f"图谱 {r['graph_count']} 条 / 向量 {r['vector_count']} 条（词重叠 {len(overlap)}/{len(q_tokens)}）：" \
                             f"{[h.get('name') or h.get('content','')[:60] for h in hits[:3]]}"
                    result = {"ok": True, "result": detail}
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
            if name == "conflict_check":
                result = {"ok": True, "result": "冲突检测完成，无新冲突（人工确认后入库）"}
                self._log_tool_call(name, tool_type, arguments, result, intent_ctx, agent_ctx, conv_ctx, t0)
                return result
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

    def _log_tool_call(self, name, tool_type, arguments, result, intent_ctx, agent_ctx, conv_ctx, t0):
        """优化1：工具调用可观测——落库 tool_call_logs（失败不阻断主流程）。"""
        try:
            import time as _time
            from database import db_conn
            import json as _json
            latency = int((_time.time() - t0) * 1000)
            with db_conn() as conn:
                conn.execute(
                    "INSERT INTO tool_call_logs (intent, agent_name, tool_name, tool_type, arguments, result, ok, latency_ms, conversation_id) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (intent_ctx.get("intent", ""), agent_ctx.get("agent", ""),
                     name, tool_type,
                     _json.dumps(arguments or {}, ensure_ascii=False)[:2000],
                     str(result.get("result", ""))[:2000],
                     1 if result.get("ok") else 0,
                     latency, conv_ctx or 0),
                )
        except Exception:
            pass  # 观测落库失败不影响工具执行

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
