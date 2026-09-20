# -*- coding: utf-8 -*-
"""AgentPipeline Mixin：任务分解、本体提示与提示词模板。

由 tools/split_pipeline.py 从 agent/pipeline.py 机械切分，勿手工编辑方法体。

注：切分脚本自身已声明「一次性、不可重跑」（见其文件头），故此处可安全叠加人工增强。
P0（2026-09-19）：_build_model_code_req 追加 L0 硬约束卡，见 v2_constraints。
"""
from .common import *
from .v2_constraints import build_l0_card


class PromptMixin:
    """任务分解、本体提示与提示词模板。"""

    def task_decompose(self, user_input: str, intent: str) -> dict:
        """P1 意图结构化拆解（文章：意图识别不是分类任务，是拆解任务）。

        仅对建模类意图执行（需求/设计/影响/评审/报告），输出 slots
        {goal, entities[], constraints[], scope{}}；LLM 不可用/Mock/解析失败 → {} 降级（确定性）。
        """
        if intent not in ("requirement_analysis", "design", "impact", "review", "report_generation"):
            return {}
        try:
            from llm import llm_client
            resp = llm_client.chat([
                {"role": "system", "content": (
                    "你是 MBSE 任务拆解器。把用户任务拆成结构化 JSON："
                    '{"goal":"任务目标(20字内)","entities":["涉及实体/对象"],"constraints":["约束条件"],"scope":{"time":"时间范围","region":"区域范围","metric":"关注指标"}}。'
                    '只输出 JSON 对象，无法拆解时输出 {"goal":"","entities":[],"constraints":[],"scope":{}}，不要其他文字。')},
                {"role": "user", "content": str(user_input)[:500]}],
                _intent="task_decompose")
            content = (resp.get("choices") or [{}])[0].get("message", {}).get("content") or ""
            slots = self._parse_json_block(content)
            if not isinstance(slots, dict):
                return {}
            slots.setdefault("goal", "")
            slots.setdefault("entities", [])
            slots.setdefault("constraints", [])
            slots.setdefault("scope", {})
            return slots
        except Exception:
            return {}

    @staticmethod
    def _parse_json_block(text: str):
        """容错解析 LLM 输出的 JSON 块：剥代码围栏 → 首尾大括号/中括号整体截取 → json.loads。

        用首尾配对而非非贪婪正则——避免嵌套对象（如 scope{...}）被提前截断。
        解析失败返回 None。
        """
        if not text:
            return None
        t = re.sub(r"```(?:json)?|```", "", text).strip()
        for open_ch, close_ch in (("{", "}"), ("[", "]")):
            s, e = t.find(open_ch), t.rfind(close_ch)
            if s != -1 and e > s:
                try:
                    return json.loads(t[s:e + 1])
                except Exception:
                    continue
        return None

    def _build_ontology_hint(self) -> str:
        """KB-P4：本体语义层——从本体加载类型/关系约束文本注入 system prompt，
        约束 LLM 按本体 schema 抽取实体/关系（对齐米爸要求：本体用于 AI Agent 语义层）。"""
        try:
            from database import get_db
            conn = get_db()
            try:
                from ontology_semantics import OntologyValidator
                return OntologyValidator(conn).schema_text()
            finally:
                conn.close()
        except Exception:
            return ""

    def _build_boundary_hint(self) -> str:
        """P0-③（2026-09-11）：本体边界说明——注入 AI system_prompt 约束 LLM 不超纲回答。

        依据：本体技术栈报告（文章三）§3.5——本体的 3 大局限（常识边界/完全未知/自我意识）。
        静态函数（ontology_semantics.boundary_text）不需要 DB 连接；Agent 端拼装 system
        prompt 时在 schema 之后追加，确保 AI 看到"本体给不了什么"的边界。
        """
        try:
            from ontology_semantics import boundary_text
            return boundary_text()
        except Exception:
            return ""

    @staticmethod
    def _is_v2_code_agent(agent_def) -> bool:
        """该 Agent 是否「产出/处理 SysML v2 代码」。

        2026-09-20 新增。**单一事实来源 = `agent_tools` 是否绑定 `sysml_v2_validate`**：
        该绑定由 register_sysml_check_tools.py 的「显式名单 + 规则发现」在**服务启动时**幂等写入，
        规则是「active 且 system_prompt 含 SysML 且含『代码』」⇒ 谁产码，谁就同时拿到
        「本地校验工具」与「L0 硬约束卡」，两者不会漂移。
        为什么不用 prompt 直判：chat / knowledge_qa 的 prompt 里也出现「SysML」「代码」
        （它们是**路由/问答**措辞：「SysML v2建模与代码生成 → MBSE模型设计专家」），
        直判会把 1473 token 的 L0 卡撒给通用闲聊（实测副作用，已回退）。
        新建的建模 Agent 在下次服务启动时自动被规则发现并绑定，故无需改任何代码。
        查不到（未绑定 / DB 不可用）→ False，回到改动前行为。
        """
        name = ""
        if isinstance(agent_def, dict):
            name = str(agent_def.get("name") or "")
        else:
            name = str(getattr(agent_def, "name", "") or "")
        if not name:
            return False
        try:
            from database import get_db
            conn = get_db()
            try:
                row = conn.execute(
                    "SELECT 1 FROM agent_tools at JOIN agents a ON a.id=at.agent_id "
                    "WHERE a.name=? AND at.tool_name='sysml_v2_validate' AND at.enabled=1",
                    (name,)).fetchone()
                return bool(row)
            finally:
                conn.close()
        except Exception:
            return False

    def _build_model_code_req(self, intent: str, agent_def=None) -> str:
        """建模类意图：要求 LLM 在正文末尾输出完整 SysML v2 (KerML) 模型代码块，
        供自动投影 BDD/IBD/REQ 等视图与前端「代码/视图」切换查看（问题3修复）。

        P0（2026-09-19）：追加 **L0 硬约束卡**（v2_constraints.L0_CARD）——
        此前这里只有输出格式要求、一条语法规则都没有，LLM 每轮重复犯同样的语法/语义错
        （真机实测一个 156 行 TMS 模型 7 条语义错）。本方法是**流式与非流式两条路径的
        唯一共用注入点**（stream.py / execute.py 同调），改一处即两条路径同时生效。
        开关：core/config.py → sysml.l0_card_enabled / sysml.l0_card_extra。
        """
        if not intent and not self._is_v2_code_agent(agent_def):
            return ""
        if intent in ("design", "requirement_analysis", "impact", "review"):
            pass
        elif "建模" in str(intent) or "sysml" in str(intent).lower():
            pass
        elif self._is_v2_code_agent(agent_def):
            # 2026-09-20：编排会把「生成 V2 模型代码」派给**视图生成类 Agent**（实测 run 367
            # 派给「结构视图生成」），而这些名字里没有 design/sysml/建模 字样 → 此前整段 L0 卡
            # 漏注入，模型直接带着 14 条语法错交付。改为按 Agent **能力**判定 —— 判据是
            # `_is_v2_code_agent`（**查 `agent_tools` 是否绑定 `sysml_v2_validate`**），
            # 与 register_sysml_check_tools 的绑定规则同源；**不是**用 prompt 文本直判
            # （prompt 直判会把 L0 卡撒给 chat/knowledge_qa，实测副作用已回退）。
            pass
        else:
            return ""
        return ("\n【建模输出要求】当前为系统建模任务，请在正文末尾以天然语言围栏代码块输出一份完整、"
                "可直接导入建模工具的 SysML v2 (KerML) 模型代码，代码块统一使用 ```sysml 语言标签包裹；"
                "内容覆盖：包(package)、块定义(part def/part usage)、接口(interface def/usage)、"
                "需求(requirement def)+满足关系(satisfies)、以及必要的结构或行为语义；"
                "代码用于自动投影 BDD/IBD/REQ 等视图与前端「代码/视图」切换查看，请确保代码语义完整、可解析。\n"
                + build_l0_card())
    def _build_prompt_template(self, intent: str, user_input: str, user=None) -> str:
        """提示词实验室接入：按意图匹配 published 提示词模板（scenario/name 含意图关键词），
        变量插值（{{ontology_profile}}/{{user_input}}/{{intent}}）后注入 system prompt。
        无匹配返回空串——提示词实验室管理的模板自此真实作用于 AI 建模会话。

        P1-6「安装即可消费」（2026-09-16）：过一遍可消费判定——有插件映射但未安装/
        已停用/已下架的模板不参与匹配；无映射的旧体系原生模板不受影响。
        """
        try:
            from database import get_db
            conn = get_db()
            try:
                rows = conn.execute(
                    "SELECT id, name, scenario, content FROM prompts WHERE status='published'"
                ).fetchall()
                rows = list(rows)
                try:
                    from plugin_system import store as _pstore
                    _keep = _pstore.consumable_filter(conn, user)
                    rows = [r for r in rows if _keep("prompts", r["id"])]
                    # P1-6：插件侧提示词（无旧表承载）——须在 manifest.content 自带正文
                    for _pid in _pstore.plugin_ids_of_types(conn, "prompt", user):
                        _pe = _pstore.prompt_entry_from_plugin(conn, _pid)
                        if _pe:
                            rows.append({"id": 0, "name": _pe["name"],
                                         "scenario": _pe["scenario"], "content": _pe["content"]})
                except Exception:
                    pass
            finally:
                conn.close()
        except Exception:
            return ""
        if not rows:
            return ""
        intent_kws = IntentRouter.INTENTS.get(intent, []) or [intent]
        best = None
        for r in rows:
            hay = f"{r['name'] or ''} {r['scenario'] or ''}"
            if any(k and str(k).lower() in hay.lower() for k in intent_kws):
                best = r
                break
        if not best or not best["content"]:
            return ""
        content = best["content"]
        content = content.replace("{{ontology_profile}}", self._build_ontology_hint())
        content = content.replace("{{user_input}}", str(user_input)[:2000])
        content = content.replace("{{intent}}", intent)
        # 2026-09-17 S4：补齐模板声明过但此前未替换的占位符——种子模板
        # （database/seeds.py:170）声明的 variables 是 ["ontology_profile","linked_data_scope","output_schema"]，
        # 而旧实现只替换了 ontology_profile，导致 {{linked_data_scope}} / {{output_schema}} 原样进入
        # system prompt（模型读到裸占位符：既浪费 token 又语义残缺）。
        # 取值口径：这两项在 system prompt 里分别由「检索到的互联数据」段与「输出规范」段承载，
        # 故此处替换为指向该段的等价描述，而不是留空（留空会破坏模板句子结构）。
        content = content.replace("{{linked_data_scope}}", "下方「检索到的互联数据」段所列来源")
        content = content.replace("{{output_schema}}", "自然语言正文（不展示需求/实体编号）")
        # 兜底清扫：模板作者写错/新增了未识别的 {{变量}} 时，不允许裸占位符泄漏进 prompt
        content = re.sub(r"\{\{[A-Za-z0-9_]+\}\}", "", content)
        return f"\n【提示词模板 · {best['name']}（来自提示词实验室）】\n{content}\n"

    # ══ 2026-09-17 S4：流式(stream) 与非流式(execute) 两条路径**共用**的装配片段 ══
    # 背景：这两个路径各自内联了一份约 2,100 字的 system prompt（约 95% 相同），
    #   任何一处改文案都要记得改另一处，已出现过口径漂移。此处收敛为单一实现，两侧只负责拼接顺序。
    def _build_role_block(self, agent_def, cap: int = 0) -> str:
        """角色化首段（Agent 专属角色块）。

        cap>0 → 按字符上限截断：流式路径用（Agent 定制必须生效，但不能让 7,981 字的
                视图生成 Agent prompt 整块进入每一次请求）；
        cap=0 → 不截断：非流式路径沿用原行为（该路径本就是"某个命中 Agent 在执行"）。
        """
        raw = (getattr(agent_def, "system_prompt", "") or "").strip()
        if not raw:
            raw = "你是网络总体MBSE设计助手。"
        if cap and len(raw) > cap:
            raw = raw[:cap] + "…"
        return raw

    def _build_output_rules(self) -> str:
        """输出规范段（两条路径共用同一份文案）。"""
        return ("输出规范：正文一律用自然语言描述，不要展示任何需求/实体编号（如 REQ-xxx / BR-x / A-xxx 等全局编号）；"
                "涉及具体条目时用「第 1 条、第 2 条」或直接陈述内容；"
                "若调用了工具，工具返回内容仅作参考素材，最终回答必须重新组织为完整、连贯的分析文本，"
                "严禁直接复述工具返回的 JSON 或执行状态。\n")

    def _build_citation_rules(self) -> str:
        """引用规范段（两条路径共用同一份文案）。"""
        return ("引用规范：作答时若引用了下方「检索到的互联数据」中的【来源n】内容，请在对应论述末尾标注来源序号 [n]"
                "（可一次标注多个，如 [1,2]）；只能标注实际存在且确被引用的来源序号，未引用的来源一律不得标注。\n")

    def _build_attachment_block(self, att_text: str) -> str:
        """用户上传资料块（优先依据 + 注入上限已由 _ATT_INJECT_CAP 在调用侧封顶）。"""
        if not att_text:
            return ""
        return ("【用户上传资料（优先依据）】\n请优先依据以上用户上传资料回答；资料未覆盖的部分再引用下方检索到的互联数据，"
                "并在引用处标注来源（📎上传资料/🧬知识库）。\n" + str(att_text) + "\n\n")
