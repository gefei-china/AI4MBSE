# -*- coding: utf-8 -*-
"""AgentPipeline Mixin：记忆/模型上下文/用户上下文/报告素材构建。

由 tools/split_pipeline.py 从 agent/pipeline.py 机械切分。
⚠️ 该脚本**已执行完毕、不可重跑**（其头部有明示：再跑会以薄入口为输入产出错误分片并破坏本目录装配）。
`agent/pipeline.py` 现为薄 Mixin 入口，**本目录（pipeline_parts/）才是当前真源，可直接编辑**。"""
from .common import *

import re as _re

# ── 身份声明剥离（2026-10-29）───────────────────────────────────────────────
# 实测：长期记忆里存着一条「用户叫米巴，最喜欢的产品代号是「智源」」，
#   而账号登录用户是 gefei —— 两者并存时，模型**优先用记忆里的自报名**称呼用户
#   （截图实测：输出"你好，米巴！"），因为记忆块的标题是「【核心偏好（跨会话稳定，请遵守）】」。
# 判词：**称呼的权威来源是登录账号，不是记忆里的自报名**。
#   ⇒ 有登录身份时，把记忆中的「用户叫X」这类**身份声明子句剥掉**，
#     记忆里其余偏好（"最喜欢的产品代号是…"）仍照常注入，不丢信息。
# ⚠️ 只剥**子句**、不整条丢：该条记忆里还有有效偏好，整条丢会连带损失。
_IDENTITY_CLAIM_RE = _re.compile(
    r"(?:用户叫|我叫|我的名字是|我的名字|我的称呼是|我的称呼|称呼我为?|称呼我是|"
    r"大家都叫我|本人叫|可以叫我|请叫我)[^，,。；;、！!？?\n]{1,20}"
)


def _strip_identity_claim(content: str) -> str:
    """把记忆文本里的「用户叫X」身份声明子句剥掉，返回剩余内容。

    纯函数（无 IO、无外部依赖），便于单测与变异自证。
    剥完若只剩空白/标点 ⇒ 返回空串（调用方据此整条不注入）。
    """
    c = str(content or "")
    if not c:
        return ""
    out = _IDENTITY_CLAIM_RE.sub("", c)
    out = out.lstrip("，,。；;、 　")
    return out.strip()


def _has_account_identity(user) -> bool:
    """当前会话是否带登录账号身份（display_name / username）。"""
    if not user:
        return False
    try:
        return bool(str(user.get("display_name") or user.get("username") or "").strip())
    except Exception:
        return False


class MemoryMixin:
    """记忆/模型上下文/用户上下文/报告素材构建。"""

    def _build_memory_hint(self, user_input: str, intent: str, user=None, scopes=None) -> str:
        """检索长期记忆（agent_memory，作用域复合过滤 + 语义相关 + 时间衰减）→ 组装注入块。

        作用域（对齐 mem0）：scopes=None 且存在会话上下文（self._mem_ctx）→ 按
        project/user/agent/global 四槽检索；无会话上下文（如定向单测）→ scopes=[] 回落旧行为（仅 agent_id）。
        P1-18 分层：core 层（用户偏好等）**无条件常驻注入**（不依赖 query 相关性），
        recall 层按 query 语义召回（排除 core 已注入 + archival 归档层）。
        仅返回命中内容；异常/无命中返回空串（不阻断主流程）。记忆只作参考对齐，
        Prompt 硬约束「不得虚构扩展」，防止把记忆当事实检索结果引用。

        ★ P0-4（2026-10-26）**噪音闸门 + 单一真源**（实测修正）：
          本路径此前**自己查一次库**（`MemoryService.search`），与 RAG 侧
          `rag.py → recall_memories()` 是**两条独立召回路径**，产生两个真实问题：

          ① **注入噪音**（实测，生产库 8 个 query）：
             本路径**没有** `_is_noise` 噪音闸门，而 RAG 侧有。
             实测「生成建模视图」：本路径注入 id=170（395 字平台寒暄话术，
             经 `_is_noise` 判定为 **True**），而 RAG 侧正确排除了它。
             ⇒ 用户看到的是一段"你好，我是 MBSE 平台的通用助手…"占了 160 字 prompt。

          ② **同一张表查两遍**（两次独立向量/BM25 计算 + 两套不同 query 构造）。

          实测**两条路径的召回 id 重叠率 = 0%**（query 构造不同 + RAG 侧有噪音闸门
          与跨域兜底所致）⇒ 原报告"同一条记忆注入两次"的判断在当前生产数据上**不成立**，
          真正的缺陷是 ①（注入噪音）与 ②（重复查询开销）。

          改法（**保持 core 常驻语义不变**）：
          · core 层仍由本路径独立查（它的语义是"无条件常驻"，RAG 侧不提供这一层），
            但**加噪音闸门**（core 内容同样可能是寒暄）；
          · recall 层改为**消费 RAG 侧已召回的 `memory_hits`**（不重复查库）——
            该列表带 score / source_type / recall_reason，信息更完整；
          · 两条路径**跨路去重**（按 id），避免同一 id 出现在两个块里；
          · 截断口径统一为 `context_audit.memory_max_chars`（默认 200，与 RAG 侧一致）。
            ⚠️ 改动前本路径截 160 字、RAG 侧截 200 字 —— 同一记忆两种长度并存，
            短的被长的完全覆盖 ⇒ 160 字截断是纯冗余。

          开关：`context_audit.memory_single_source`（默认 True）；置 False 退回旧行为。
        """
        try:
            from memory_backend import get_memory_backend
            from database import get_db
            conn = get_db()
            try:
                agent_id = intent or "chat"
                query = f"{user_input} {intent}"
                # P0#1 显式读触发：寒暄/噪音类 query 跳过整块记忆注入（省检索开销 + 避免噪音入 prompt）
                try:
                    from agent.memory_recall import should_recall_memory as _should_recall
                    if not _should_recall(intent or "chat", query):
                        return ""
                except Exception:
                    pass
                if scopes is None:
                    scopes = self._memory_scopes(intent, user) if getattr(self, "_mem_ctx", None) else []
                _be = get_memory_backend()
                _single = True
                try:
                    from core import config as _cfg
                    _single = bool(_cfg.get("context_audit", "memory_single_source", True))
                except Exception:
                    _single = True
                _max_chars = 200
                try:
                    from core import config as _cfg2
                    _max_chars = int(_cfg2.get("context_audit", "memory_max_chars", 200) or 200)
                except Exception:
                    _max_chars = 200
                # P1-18：core 常驻（无条件）+ recall 按需（query 相关，排除 core 已注入）
                core_rows = _be.search_core(conn, agent_id, top_k=2, scopes=scopes)
                # ★ 噪音闸门：core 层同样可能是寒暄/平台话术（实测 id=170 即属此类）
                try:
                    from agent.memory_recall import _is_noise as _noise_gate
                except Exception:
                    def _noise_gate(_t):
                        return False
                core_rows = [r for r in (core_rows or [])
                             if not _noise_gate(str(r.get("content") or ""))]
                recall_rows = []
                if _single:
                    # ── 单一真源：消费 RAG 侧已召回的 memory_hits（不重复查库）──
                    try:
                        _hits = getattr(self, "_rag_memory_hits", None) or []
                        _core_ids = {r.get("id") for r in core_rows if r.get("id")}
                        for r in _hits:
                            _mid = (r.get("meta") or {}).get("id")
                            if _mid is not None and _mid in _core_ids:
                                continue      # 跨路去重：core 已注入的不再重复
                            recall_rows.append({
                                "id": _mid,
                                "mem_type": r.get("mem_type") or "fact",
                                "content": r.get("content") or "",
                                "score": r.get("score") or 0.0,
                            })
                    except Exception:
                        recall_rows = []
                else:
                    # 旧行为（回滚开关）：自己查一次 recall 层
                    recall_rows = _be.search(conn, agent_id, query, top_k=4, scopes=scopes)
                    _core_ids = {r.get("id") for r in core_rows if r.get("id")}
                    recall_rows = [r for r in recall_rows if r.get("id") not in _core_ids]
                if not core_rows and not recall_rows:
                    return ""
                # ── 身份声明剥离（2026-10-29）：有登录账号身份时，称呼以账号为准 ──
                #   记忆里的「用户叫X」不得作为称呼来源（否则模型会优先用它，
                #   实测输出"你好，米巴！"而账号是 gefei）。
                #   开关：`context_audit.memory_identity_strip`（默认 True；置 False 退回旧行为）。
                _strip_identity = False
                try:
                    from core import config as _cfg3
                    _strip_identity = bool(_cfg3.get("context_audit",
                                                     "memory_identity_strip", True))
                except Exception:
                    _strip_identity = True
                if _strip_identity and _has_account_identity(user):
                    def _strip_rows(rows):
                        out = []
                        for r in (rows or []):
                            _c = str(r.get("content") or "")
                            _new = _strip_identity_claim(_c)
                            if not _new:
                                continue          # 剥完为空 ⇒ 该条只剩身份声明 ⇒ 不注入
                            if _new != _c:
                                r = dict(r)
                                r["content"] = _new
                            out.append(r)
                        return out
                    core_rows = _strip_rows(core_rows)
                    recall_rows = _strip_rows(recall_rows)
                    if not core_rows and not recall_rows:
                        return ""
                lines = []
                if core_rows:
                    lines.append("【核心偏好（跨会话稳定，请遵守）】")
                    for r in core_rows:
                        lines.append(f"- [{r.get('mem_type', 'fact')}] "
                                     f"{str(r.get('content') or '')[:_max_chars]}")
                if recall_rows:
                    lines.append("【长期记忆（跨会话经验，仅供对齐，不得虚构扩展或作为事实来源引用）】")
                    for r in recall_rows:
                        lines.append(f"- [{r.get('mem_type', 'fact')} | 相关度 {r.get('score', 0):.2f}] "
                                     f"{str(r.get('content') or '')[:_max_chars]}")
                return "\n".join(lines) + "\n"
            finally:
                conn.close()
        except Exception:
            return ""

    def _memory_recall_scope(self, intent: str, user=None) -> dict:
        """P0（2026-09-29）记忆召回的 kb_scope 补充字段（供 `GraphRAG.retrieve` 消费）。

        检索侧**不自己解析项目/意图**（那样会在 rag.py 里重复一遍 `_resolve_mem_project_id`
        的取值链，两处必然漂移）；改由**持有会话上下文的 pipeline 侧**算好、经 kb_scope 传入。

        返回 {"intent":..., "project_id":..., "memory_scopes":[...]}：
        - `memory_scopes` 仅在**存在会话上下文**（self._mem_ctx）时给出 —— 与 `_build_memory_hint`
          的既有判据一致（`scopes = self._memory_scopes(...) if getattr(self, "_mem_ctx", None) else []`）。
          无会话（定向单测/夹具）→ 不含该键，检索侧回落「仅 agent_id」的旧行为，**不引入隐式串扰**。
        - `project_id` 可为空串（未配置默认项目）→ 检索侧据此跳过 project_memories 路（防测试数据混入）。
        异常返回 {}（检索侧按缺省处理，不阻断）。
        """
        try:
            out = {"intent": intent or "chat"}
            ctx = getattr(self, "_mem_ctx", None)
            if ctx:
                out["project_id"] = self._resolve_mem_project_id(int(ctx.get("conversation_id") or 0))
                out["memory_scopes"] = self._memory_scopes(intent, user or ctx.get("user"))
            return out
        except Exception:
            return {}

    # ── ⚠️ 这里**曾**实现「按语义相关性过滤上下文条目」，**经标定后主动放弃**（2026-09-19）。
    #   标定脚本 tmp/kcx/calib.py（21 条库内真实素材 × 1 条真实 query，两路同测）：
    #     dense  路：正例最低 0.4099（KerML 语义约束） vs 负例最高 0.5054（V2G_d45be3e1 纯代号）→ gap = -0.0954
    #     bigram 路：正例最低 0.0284                    vs 负例最高 0.0962（数据链分系统）       → gap = -0.0678
    #   两路分布**都重叠** —— 负例「纯代号」在 dense 路得分甚至高于**所有**正例，
    #   即**不存在能分开二者的阈值**。用分不开的判据做删除决策 = 不可预测的误杀
    #  （正例 KerML 会被 0.55 的门滤掉，而负例代号反被放行）。故改为**结构性隔离**：
    #   不给「可被误用的具体素材」（见 `_build_model_context` 的摘要模式），并显式标注适用范围
    #  （见其与 `_build_project_memory` 的范围声明）。留此注以免后人重蹈。
    # ──

    # ── P0：建模上下文注入（工作记忆：当前模型状态，MBSE 特有）──
    def _build_model_context(self, branch="", conversation_id=0, user_input=""):
        """当前建模状态工作记忆（只读参考）：既有实体规模/视图/最近变更/会话产物。

        数据全部来自现有表（entities/graph_views/impact_analyses/artifacts），零新增表；
        异常/无数据返回空串不阻断。预算 ≤ model_context_chars（默认 800）。

        P0（2026-09-19）**结构性隔离**：实体原先**列出具体名字**，实测会把上一个任务的领域素材
        当成本轮素材 —— 会话 351（「生成电动汽车热管理系统的 SysML v2 代码」）的子 Agent 在交付物里
        写道「工作记忆里的活跃实体是动力分系统、巡飞弹等，与电动汽车热管理无关，不能挪用」，
        并**因此拒绝产出代码**（下游校验无输入 → 全链条断裂）。这不是模型不听话，是上下文塞了错素材。

        现默认 `context.model_context_entities="count"`：**只报数量、不列明细** —— 既告知
        「本分支已有历史资产」，又不存在可被挪用的素材；置 "names" 回到旧行为，"none" 完全不注入。
        另在块首给出「适用范围」声明做第二层防御（阈值类判据已实测不可靠，见上方注释）。
        """
        from core import config as _cfg
        from database import get_db
        max_chars = int(_cfg.get("context", "model_context_chars", 800))
        ent_mode = str(_cfg.get("context", "model_context_entities", "count") or "count").lower()
        try:
            conn = get_db()
            lines = []
            try:
                # 既有实体（当前分支，非 raw/废弃）
                ent_total = conn.execute(
                    "SELECT COUNT(*) c FROM entities WHERE branch=? AND status NOT IN ('raw_chunk','deprecated')",
                    (branch,)).fetchone()["c"] or 0
                if ent_total and ent_mode == "names":
                    cand = conn.execute(
                        "SELECT name FROM entities WHERE branch=? AND status NOT IN ('raw_chunk','deprecated') "
                        "ORDER BY created_at DESC, id DESC LIMIT 8", (branch,)).fetchall()
                    names = [str(r["name"]) for r in cand if str(r["name"] or "").strip()]
                    if names:
                        lines.append(f"活跃实体：{'、'.join(n[:14] for n in names)}"
                                     f"{' 等' if ent_total > len(names) else ''}（共 {ent_total} 个）")
                elif ent_total and ent_mode != "none":
                    lines.append(f"既有建模实体：本分支共 {ent_total} 个（本项只报规模、不列明细；"
                                 f"本轮可用素材请以「检索到的互联数据」为准）")
                # 视图
                views = conn.execute(
                    "SELECT name FROM graph_views WHERE branch=? ORDER BY id DESC LIMIT 5", (branch,)).fetchall()
                if views:
                    lines.append(f"视图：{'、'.join(str(r['name'])[:12] for r in views)}（{len(views)} 个）")
                # 最近变更影响分析（快照可追溯）
                impacts = conn.execute(
                    "SELECT change_source, title, created_at FROM impact_analyses ORDER BY id DESC LIMIT 3").fetchall()
                if impacts:
                    _il = []
                    for r in impacts:
                        src = str(r["change_source"] or r["title"] or "")[:16]
                        if src:
                            _il.append(f"{src}（{str(r['created_at'] or '')[:16]}）")
                    if _il:
                        lines.append("最近变更：\n" + "\n".join("  · " + i for i in _il))
                # 会话产物
                if conversation_id:
                    arts = conn.execute(
                        "SELECT kind, title FROM artifacts WHERE conversation_id=? ORDER BY id DESC LIMIT 3",
                        (conversation_id,)).fetchall()
                    if arts:
                        lines.append("当前会话产物：" + "、".join(
                            f"{r['kind']}《{str(r['title'] or '')[:12]}》" for r in arts))
            finally:
                conn.close()
            if not lines:
                return ""
            # 适用范围声明置顶且**不参与预算截断**（原实现 `block[:max_chars]` 连标题都可能被截掉，
            # 声明被截掉就等于没有第二层防御）。正文按剩余预算截断。
            _head = ("【当前建模上下文（工作记忆，只读参考）】\n"
                     "（以下为本分支**既有**建模资产，仅供了解现状；与本次诉求领域不符的内容必须忽略，"
                     "不得作为本次建模的素材、需求来源或事实依据引用）\n"
                     f"分支：{branch or '-'}\n")
            _room = max(0, max_chars - len(_head))
            return _head + ("\n".join(lines) + "\n")[:_room]
        except Exception:
            return ""

    # ── 多维作用域辅助（记忆读写的取值真源，单点化避免各处重复 SQL）──
    def _resolve_mem_project_id(self, conversation_id=0, project_id: str = "") -> str:
        """记忆注入取项目真源：**显式 project_id → 会话的 project_id → settings.default_project_id → 空**。

        实例缓存按 conversation_id 校验（同一会话复用，跨会话不串）；异常/表缺失 → 空串（不阻断）。
        """
        try:
            if project_id:
                return str(project_id)
            cid = int(conversation_id or 0)
            # 仅在「有会话上下文」时缓存：cid=0（无会话，含夹具/定向单测）**不缓存** ——
            # 否则同一实例上跨上下文复用会把上次的 project_id 泄漏进本次（2026-09-26 实测：
            # 夹具库缺 settings 行时仍误查 project_memories，被 verify[3] 抓到）。
            _cache = getattr(self, "_mem_project_id_cache", None)
            if cid > 0 and _cache is not None and _cache[0] == cid:
                return _cache[1]
            pid = ""
            from database import get_db
            if cid > 0:
                try:   # 会话查询独立兜底：夹具/老库无 conversations 表时不得抛出
                    conn = get_db()
                    try:
                        row = conn.execute("SELECT project_id FROM conversations WHERE id=?", (cid,)).fetchone()
                        if row:
                            pid = str(row["project_id"] or "")
                    finally:
                        conn.close()
                except Exception:
                    pid = ""
            if not pid:
                conn = get_db()
                try:
                    row = conn.execute(
                        "SELECT value FROM settings WHERE key='default_project_id'").fetchone()
                    pid = (row["value"] if row else "") or ""
                finally:
                    conn.close()
            if cid > 0:
                self._mem_project_id_cache = (cid, pid)
            return pid
        except Exception:
            return ""

    @staticmethod
    def _memory_user_key(user) -> str:
        """用户作用域标识：username 优先，回落 display_name（无身份 → 空）。"""
        try:
            if not user:
                return ""
            return str(user.get("username") or user.get("display_name") or "").strip()[:80]
        except Exception:
            return ""

    def _memory_scopes(self, intent: str, user=None) -> list:
        """有序记忆作用域槽位（优先级高→低）：project → user → agent → global。

        空项剔除（无项目 → 无 project 槽；无身份 → 无 user 槽）；global 恒在末位。
        """
        try:
            ctx = getattr(self, "_mem_ctx", None) or {}
            scopes = []
            pid = self._resolve_mem_project_id(int(ctx.get("conversation_id") or 0))
            if pid:
                scopes.append(("project", pid))
            uname = self._memory_user_key(user or ctx.get("user"))
            if uname:
                scopes.append(("user", uname))
            if intent:
                scopes.append(("agent", intent))
            scopes.append(("global", ""))
            return scopes
        except Exception:
            return []

    def _build_project_memory(self, project_id: str = "", user_input: str = "", conversation_id: int = 0) -> str:
        """项目级持久记忆（Project Constitution）：规范/基线/决策/经验注入 system prompt 防漂移。

        对齐 Codex durable project memory / Claude Code CLAUDE.md——每会话注入项目宪法，
        约束模型遵守既定规范与设计基线。
        **取项目顺序（2026-09-26 作用域化）：显式 project_id → 会话所属 project_id → settings.default_project_id
        → 空则完全不注入。** 会话取值让「项目宪法」真正按**会话所属工程**注入（此前一律回退默认项目，属接线缺口）；
        代码里**不再有任何硬编码兜底**；把默认项目键置空 = 停用注入。
        预算 project_memory_chars（默认 600）截尾保头（规范/基线优先）；无数据/异常返回空串不阻断。

        P0（2026-09-19）：**注入时带项目名 + 显式适用范围声明**，把「是否适用」的判断权交给模型。
        取证：默认项目（卫星通信，实测 32 条已启用记忆）里含「载荷配置调整 / 通信载荷」等无关条目，
        被无条件当作「AI 必须遵守」注入到「电动汽车热管理系统」任务。
        机制选择：**这里刻意不做语义相关性过滤** —— 标定实测 dense/bigram 两路分布重叠、
        无可用阈值（见本文件 `_build_model_context` 上方注释与 tmp/kcx/calib.py）。
        模型本就有识别能力（会话 351 的 t1/t3 主动写「素材与标题不匹配、套用就是虚构」并拒绝产出），
        它缺的只是**被明确授权忽略**这一句。
        """
        try:
            from database import get_db
            from core import config as _cfg0
            conn = get_db()
            try:
                # 动态开关/预算（settings 表，管理界面可调；缺省 开启 / 600 字符）
                _cfg_rows = {r["key"]: r["value"] for r in conn.execute(
                    "SELECT key, value FROM settings WHERE key IN ('memory.project_memory_enabled','memory.project_memory_chars')"
                ).fetchall()}
                if str(_cfg_rows.get("memory.project_memory_enabled", "1")).lower() not in ("1", "true", "yes", "on"):
                    return ""
                max_chars = int(_cfg_rows.get("memory.project_memory_chars", "600") or 600)
                # 取项目真源：显式 → 会话 → settings（单点于 _resolve_mem_project_id，带实例缓存）
                # 2026-09-20 教训保留：行缺失/置空一律取空 → 交给下方 `if not pid: return ""`，
                # 绝不再回退硬编码默认项目（否则「置空以停用注入」会被悄悄推翻）。
                pid = self._resolve_mem_project_id(conversation_id, project_id)
                if not pid:
                    return ""
                rows = conn.execute(
                    "SELECT category, title, content FROM project_memories "
                    "WHERE project_id=? AND enabled=1 ORDER BY id", (pid,)).fetchall()
            finally:
                conn.close()
            if not rows:
                return ""
            items = []
            for r in rows:
                content = (r["content"] or "").strip()
                if not content:
                    continue
                items.append(f"· [{r['category']}] {r['title']}：{content}")
            if not items:
                return ""
            head = (f"【项目规范基线（Project Constitution · 项目「{pid}」）\n"
                    "（以下为该项目的既定规范/基线，**仅当本次任务属于该项目领域时适用**；"
                    "若与本次诉求领域不符，必须整体忽略，不得作为本次任务的约束或素材来源）】")
            body = "\n".join(items) + "\n"
            _room = max(0, max_chars - len(head) - 1)   # 范围声明不参与裁剪（截掉了等于没有防御）
            return head + "\n" + body[:_room]
        except Exception:
            return ""

    def _deposit_session_memory(self, user_input: str, content: str, intent: str) -> None:
        """会话产出记忆沉淀：LLM 提炼 or 规则降级；每会话限 2 次防噪音。

        仅主会话（非 dry_run）调用；dry_run 子任务路径由 nodes._exec_agent 自行沉淀。
        作用域：默认落 **project**（会话所属工程），无工程上下文回落 **agent**；
        偏好类记忆（mem_type=preference）由 maybe_deposit 改落 **user** 槽（跨 Agent 复用）。
        签名保持 3 参（既有调用点与自检脚本零改动）。
        """
        try:
            if not content or not content.strip():
                return
            # P0#1 显式写触发：被显式排除的 intent 不沉淀（避免污染记忆库；内容级判定仍归 maybe_deposit 三重门）
            try:
                from agent.memory_recall import should_deposit_memory as _should_deposit
                if not _should_deposit(intent or "chat", content):
                    return
            except Exception:
                pass
            if getattr(self, "_mem_deposit_count", 0) >= 2:
                return
            from memory_backend import get_memory_backend
            from database import get_db
            ctx = getattr(self, "_mem_ctx", None) or {}
            pid = self._resolve_mem_project_id(int(ctx.get("conversation_id") or 0)) if ctx else ""
            if pid:
                scope_type, scope_id = "project", pid
            else:
                scope_type, scope_id = "agent", (intent or "chat")
            uname = self._memory_user_key(ctx.get("user"))
            user_scope = ("user", uname) if uname else None
            conn = get_db()
            try:
                mem_id = get_memory_backend().maybe_deposit(
                    conn, intent or "chat", content, user_input,
                    scope_type=scope_type, scope_id=scope_id, user_scope=user_scope)
                if mem_id:
                    self._mem_deposit_count = getattr(self, "_mem_deposit_count", 0) + 1
            finally:
                conn.close()
        except Exception:
            pass

    def _record_skill_feedback(self, conversation_id=0, intent="", output_content="", error=None) -> None:
        """P0-5 技能使用反馈采集：本次命中技能按输出成功/失败更新 use_stats，失败滚动备注。

        对齐 Hermes「技能在使用中自我改进」：反馈数据供 SkillImprover 周期修订触发。
        规则判定（不依赖 LLM），异常静默。

        ★ 2026-10-27（会话 9633/9629 实测）：**这里记的是「命中/注入」，不是「执行」**。
          旧实现把 `result` 填成**本轮最终答案** ⇒ 实测 conv 9633 里
          file-ops / sysml_view_generation_activity / 文件操作 三行 `result` 完全相同、
          都等于最终答案（1923 字符）⇒ 看板读成"调用了 3 个技能、各产出一份结果"，
          技能效果归因彻底失真（用户因此误以为"触发了别的 agent"）。
          技能没有独立返回值：它是被注入的指导正文，真正干活的是主循环的工具调用。
          ⇒ `result` 改填**事实描述**（命中并注入、未独立执行），`ok` 仍表示"命中成功"，
          需要判断真实效果的按 `tool_call_logs` 里 `call_kind='tool'` 的行看。
        """
        hits = self._last_skill_hits or []
        if not hits:
            return
        ok = bool((output_content or "").strip()) and not error
        note = None
        if not ok:
            note = (str(error or "")[:150] if error
                    else ("无输出" if not (output_content or "").strip() else "执行失败"))
        try:
            from database import db_conn
            with db_conn() as conn:
                # 可观测：技能调用日志（call_kind='skill'，与工具日志统一落库，失败不阻断）
                _agent = getattr(self, "_tool_agent_ctx", None) or {}
                # 事实描述：技能是"注入"不是"执行"，不拿本轮最终答案冒充技能产出。
                _summary = ("（技能注入：本轮命中并注入正文指导，未独立执行；"
                            "实际动作见同轮 call_kind=tool 的行）"
                            if ok else (note or "注入失败"))
                # P1-2：与工具日志统一带 failure_kind（此前本 INSERT 无该列 ⇒ 技能
                # 失败步在步骤级归因里只能落 unknown）。分类器复用 B1-6 的
                # services.failure_class（不另造第二份词表）；只在失败时写。
                _skind = ""
                if not ok:
                    try:
                        from services.failure_class import classify as _sfc
                        _skind = _sfc(note or _summary or "skill failed", ok=False)
                    except Exception:
                        _skind = ""
                for name in hits[:10]:
                    try:
                        # P0-a：capability_id=skill:{id}（skills.name 无 UNIQUE 约束，
                        # 重名取最小 id，与 core/capability_registry 投影口径一致）。
                        _sid_row = conn.execute(
                            "SELECT id FROM skills WHERE name=? ORDER BY id LIMIT 1",
                            (name,)).fetchone()
                        # ★ P0-3（2026-10-27）：技能调用同样要带 trace_id ——
                        #   实测（会话 9623）本写入点 3 行 trace_id 全空，而同一轮的工具行有值
                        #   ⇒ 同一轮的能力调用**一半能 JOIN、一半不能**，追溯链从中间断开。
                        #   取值与工具侧**同一实现**（实例属性 → ContextVar），不另造 id。
                        _tid = str(getattr(self, "_tool_trace_id", "") or "")
                        if not _tid:
                            try:
                                from core.audit import current_trace_id as _ctid_m
                                _tid = _ctid_m()
                            except Exception:
                                _tid = ""
                        # ★ 请求 3（2026-10-27）：技能调用同样要带 run_id，与工具调用同源归批。
                        #   此前本 INSERT 列清单**无 run_id 列**（落库取列默认值 0），且 `run_id` 形参
                        #   被错位绑到 conversation_id —— 故技能调用 run_id 恒 0，主路径归批仍漏掉最大的
                        #   一块（技能触发）。现：conversation_id 取形参真值；run_id 取本实例
                        #   `self._tool_run_ctx`（顶层轮次就地分配、嵌套沿用父编排 run，与 _log_tool_call 同源）。
                        _rid = int(getattr(self, "_tool_run_ctx", 0) or 0)
                        # S0（2026-10-28）探针标记：技能行同样要标（验证脚本会调技能）。
                        try:
                            from core.probe_mark import is_probe_tool as _ipt
                            _is_probe_skill = _ipt(name)
                        except Exception:
                            _is_probe_skill = 0
                        conn.execute(
                            # 2026-10-27（截断清单 P0-B3）：技能行也写 result_len，
                            #   与工具行口径一致（否则 0 既表示"未知"又表示"空"，无法区分）。
                            "INSERT INTO tool_call_logs (intent, agent_name, tool_name, tool_type, call_kind, "
                            "arguments, result, ok, latency_ms, conversation_id, run_id, failure_kind, capability_id, trace_id, result_len, is_probe) "
                            "VALUES (?,?,?,?,?,?,?,?,0,?,?,?,?,?,?,?)",
                            (intent or "", _agent.get("agent", ""), name, "skill", "skill",
                             "{}", _summary, 1 if ok else 0, int(conversation_id or 0), _rid,
                             _skind, ("skill:%s" % _sid_row[0]) if _sid_row else "", _tid,
                             len(_summary or ""),
                             # S0（2026-10-28）探针标记（core/probe_mark）
                             _is_probe_skill))
                    except Exception:
                        pass
                # ★ 2026-10-29（P0-2）：**本循环的 run_id 必须在这里独立计算**。
                #
                #   此前第 528 行直接引用裸 `run_id`，而本函数签名只有
                #   `conversation_id`（`run_id` 形参早在 2026-10-27 的改造里被改名/移除）
                #   ⇒ 失败分支必然抛 `NameError`，被函数末尾的 `except: pass` 吞掉。
                #   实测（构造一次失败调用）：uses / fail / feedback_notes **三个计数全不变**，
                #   而成功分支正常 +1 ⇒ 技能的失败统计与失败备注**长期只写成功那一半**。
                #   后果直指自进化闭环：SkillImprover 读的正是 feedback_notes，
                #   它拿不到"哪里不聪明"，只能凭"无输出"三字盲改（上一轮产出的坏草稿即源于此）。
                #
                #   取值口径与工具侧一致：实例属性 `_tool_run_ctx`（顶层轮次就地分配、
                #   嵌套沿用父编排 run），不另造 id。
                _rid_note = int(getattr(self, "_tool_run_ctx", 0) or 0)
                for name in hits[:10]:
                    # ★ 每条技能**独立** try：原实现整块 try 包住循环，
                    #   一条失败（如改名后 name 查不到）会让**后续技能全部跳过**，
                    #   且失败本身不可见。现在单条失败只影响单条，并打印前 120 字原因。
                    try:
                        row = conn.execute(
                            "SELECT use_stats, feedback_notes FROM skills WHERE name=? AND status='published'",
                            (name,)).fetchone()
                        if not row:
                            continue
                        try:
                            st = json.loads(row["use_stats"] or "{}")
                        except Exception:
                            st = {}
                        uses = int(st.get("uses", 0)) + 1
                        succ = int(st.get("success", 0)) + (1 if ok else 0)
                        st.update({"uses": uses, "success": succ, "fail": uses - succ,
                                   "last_used": time.strftime("%Y-%m-%d %H:%M:%S")})
                        if ok:
                            conn.execute("UPDATE skills SET use_stats=?, updated_at=CURRENT_TIMESTAMP WHERE name=?",
                                         (json.dumps(st, ensure_ascii=False), name))
                        else:
                            try:
                                notes = json.loads(row["feedback_notes"] or "[]")
                            except Exception:
                                notes = []
                            notes.append({"ts": st["last_used"], "run_id": _rid_note,
                                          "intent": intent or "", "note": note})
                            conn.execute(
                                "UPDATE skills SET use_stats=?, feedback_notes=?, updated_at=CURRENT_TIMESTAMP WHERE name=?",
                                (json.dumps(st, ensure_ascii=False), json.dumps(notes[-5:], ensure_ascii=False), name))
                    except Exception as _one:
                        # 单条失败不静默：技能统计是自进化的输入，悄悄丢等于让它瞎猜。
                        print("[skill-feedback] 技能「%s」统计写入失败（已跳过，不影响其余）: %s: %s"
                              % (name, type(_one).__name__, str(_one)[:120]), flush=True)
                        continue
        except Exception:
            pass

    def _build_report_material(self, report_type: str, retrieval: dict) -> str:
        """SP-R：类型化报告素材——变更影响（BFS 影响图）/ 预评审（校验清单）结构化数据块。

        返回 markdown 文本，追加到报告上下文；异常/无数据返回空串（不阻断生成）。
        """
        try:
            if report_type == "impact":
                card = self._card_impact(retrieval)
                src = card.get("change_source")
                src_txt = src.get("name") if isinstance(src, dict) else "-"
                lines = [f"变更源：{src_txt}",
                         f"直接影响 {card.get('direct_count', 0)} 条 / 间接 {card.get('indirect_count', 0)} 条"
                         f"（深度 {card.get('depth', 1)} 层 · {card.get('direction', 'both')}）："]
                # 影响程度分级
                _lv = card.get("impact_levels") or {}
                lines.append(f"影响程度：高 {_lv.get('high', 0)} / 中 {_lv.get('mid', 0)} / 低 {_lv.get('low', 0)}")
                # 深度统计（影响深度维度）
                _ds = card.get("depth_stats") or {}
                if _ds:
                    _dparts = [f"第{k}层(直接{d['direct']}/间接{d['indirect']})"
                               for k, d in sorted(_ds.items(), key=lambda kv: int(kv[0]))]
                    lines.append("分深度传播：" + "，".join(_dparts))
                # 广度统计（影响广度维度：类型分布 + 跨领域）
                _ts = card.get("type_stats") or {}
                if _ts:
                    lines.append("受影响元素类型：" + "，".join(f"{t}×{c}" for t, c in _ts.items()))
                _doms = card.get("domain_stats") or {}
                if _doms:
                    lines.append("跨领域影响：" + "，".join(f"{d}×{o['count']}" for d, o in _doms.items()))
                _ra = card.get("risk_analysis") or {}
                for r in (_ra.get("risks") or [])[:5]:
                    lines.append(f"- [{r.get('level', '')}] {r.get('title', '')}：{r.get('desc', '')} → {r.get('advice', '')}")
                for n in (card.get("impact_nodes") or [])[:40]:
                    lines.append(f"- [{n.get('impact', '')}] {n.get('name', '')}（{n.get('type', '')}）深度 {n.get('depth', 0)}"
                                 f" 影响度 {n.get('score', 0)}（{n.get('level', '')}）")
                if card.get("warning"):
                    lines.append(f"⚠ 提示：{card['warning']}")
                return "【变更影响分析素材】\n" + "\n".join(lines)
            if report_type == "review":
                card = self._card_review(retrieval)
                lines = [f"评审评分 {card.get('score', 0)}/100 · 结论：{card.get('conclusion', '-')}",
                         f"元素 {card.get('total_elements', 0)} · 已评审 {card.get('reviewed', 0)} · 候选 {card.get('candidates', 0)} · 冲突 {card.get('syntax_errors', 0)}"]
                for it in (card.get("issues") or [])[:30]:
                    lines.append(f"- [{it.get('level', '')}] {it.get('type', '')}：{it.get('desc', '')} → 修复：{it.get('fix', '')}")
                return "【预评审素材】\n" + "\n".join(lines)
        except Exception:
            return ""
        return ""

    def _build_user_context(self, user) -> str:
        """P2 用户上下文注入（文章：用户/业务/企业知识三类上下文）：
        身份/角色进 prompt，让同一技能因人而异；无身份时返回空串。

        ★ 2026-10-29：**称呼的权威来源是登录账号**。
          实测：长期记忆里存着「用户叫米巴…」，账号是 gefei，两者并存时模型
          优先用记忆里的自报名（输出"你好，米巴！"）⇒ 与用户诉求冲突。
          ⇒ 这里显式声明优先级：称呼以本块的「当前用户」为准，
            长期记忆里的自报名**不得**作为称呼依据（记忆注入侧已同步剥离该子句）。
        """
        if not user:
            return ""
        _disp = str(user.get("display_name") or user.get("username") or "")
        parts = [f"当前用户：{_disp}"]
        if _disp:
            parts.append(f"称呼用户时一律使用「{_disp}」；长期记忆中出现的其他自报名（如“用户叫X”）"
                         f"不得作为称呼依据")
        if user.get("role_name"):
            parts.append(f"角色：{user['role_name']}")
        if user.get("department"):
            parts.append(f"部门：{user['department']}")
        if user.get("workspace"):
            parts.append(f"工作空间：{user['workspace']}")
        return "【用户上下文】\n" + "\n".join(parts) + "\n"
