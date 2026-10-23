"""AI 工坊 Copilot：自然语言 → DAG 流程生成引擎（P0）。

设计原则（工程稳定性 + 数据兼容）：
- **稳定性**：模板检索复用优先 → LLM 结构化输出 → 硬校验器校验；
  LLM 失败/无 key 时回退确定性 Mock 模板（关键词匹配），保证任何环境可生成且不产生幻觉节点。
- **数据兼容**：生成结果与手动创建完全同构（nodes/edges JSON 与 agent_flows 表格式一致，
  {id,type,label,config} / {source,target}），可直接保存/执行/导入画布，全链路复用。
- **全面性**：校验器覆盖节点类型/工具/Agent/边端点/孤儿/重复 id，返回分级问题清单。
"""
import json
import re

from workflows import ToolRegistry


# ── 节点类型 schema（与画布 FLOW_NODE_TYPES / FlowExecutor.NODE_TYPES 对齐）──
NODE_SCHEMA = {
    "llm":   {"label": "LLM 节点", "fields": ["prompt", "system_prompt", "model", "provider_id", "write_keys", "subscribe"]},
    "tool":  {"label": "工具节点", "fields": ["tool", "arguments", "branch"]},
    "agent": {"label": "Agent 节点", "fields": ["agent", "query", "provider_id", "memorize", "write_keys", "subscribe"]},
    "skill": {"label": "Skill 节点", "fields": ["skill"]},
    "mcp":   {"label": "MCP 节点", "fields": ["endpoint", "tool", "arguments", "transport"]},
    "if":    {"label": "条件节点", "fields": ["expression", "max_iterations"]},
    "orchestrator": {"label": "Manager 节点", "fields": ["workers", "task", "strategy"]},
    "reflection": {"label": "反思节点", "fields": ["target", "criteria"]},
    "code":     {"label": "代码节点", "fields": ["language", "code", "inputs"]},
    "http":     {"label": "HTTP 节点", "fields": ["method", "url", "headers", "body", "query", "timeout"]},
    "iteration": {"label": "迭代节点", "fields": ["items", "subflow"]},
    "knowledge": {"label": "知识节点", "fields": ["query", "top_k", "branch"]},
    "pubsub":   {"label": "消息节点", "fields": ["op", "topic", "payload", "write_key"]},
    "debate":   {"label": "协商节点", "fields": ["mode", "proposal", "sources", "options", "threshold"]},
}

# ── 2026-10-23（米爸决策：删除 Mock 模板，失败即报错）────────────────────────
# 为什么删：本文件原先在 LLM 不可用时回退到 4 套**写死的流程模板**（需求分析流程 /
# 变更影响分析 / 模型预评审 / 知识问答），并标注"（Mock 兜底）"——但内容仍会被前端
# 当作真实 AI 产出导入流程。这与 llm 层的既有原则冲突（llm/__init__.py 三处降级
# 点一律 raise LLMUnavailableError，门禁 verify_no_mock_fallback.py 守住）：
# **用户拿到的是编造的内容**。演示模板应属于 docs / 离线演示，不属于生产链路。
# 现在：LLM 不可用 → 返回 {"ok": False, "error": ...}，前端如实展示失败原因。



class FlowCopilot:
    """AI 流程生成器：模板检索 → LLM 生成 → 校验 → 返回与手动创建同构的 DAG 定义。"""

    def __init__(self, conn=None):
        self.conn = conn
        self._tools: list = []
        self._agents: list = []
        self._skills: list = []
        if conn is not None:
            self._load_catalog(conn)

    def _load_catalog(self, conn) -> None:
        """加载可用工具/Agent/Skill 清单（注入 System Prompt 的白名单）。"""
        try:
            self._tools = [t["name"] for t in ToolRegistry(conn).list()]
        except Exception:
            self._tools = list(ToolRegistry.BUILTINS.keys())
        try:
            for r in conn.execute("SELECT name, display_name, description FROM agents WHERE status='active'").fetchall():
                self._agents.append({
                    "name": r["name"], "display_name": r["display_name"] or r["name"],
                    "description": r["description"] or "",
                })
        except Exception:
            pass
        try:
            for r in conn.execute("SELECT name FROM skills WHERE status!='draft'").fetchall():
                self._skills.append(r["name"])
        except Exception:
            pass

    # ── 主入口：生成 ──
    def generate(self, prompt: str, conn=None, deep_thinking: bool = False) -> dict:
        """自然语言 → DAG 定义（{name, description, nodes, edges}）+ 校验结果。

        deep_thinking: 深度思考（LLM reasoning）——慢但分析更深入；Mock 模式下模拟思考。
        """
        if conn is not None and self.conn is None:
            self.conn = conn
            self._load_catalog(conn)
        prompt = (prompt or "").strip()
        if not prompt:
            return {"ok": False, "error": "任务描述必填"}
        example = self._retrieve_example(prompt, conn)
        # 1) LLM 结构化生成。★ 2026-10-23：**不再回退 Mock 模板** ——
        #    LLM 不可用（无 Key / 调用失败 / 返回非 JSON）→ 直接返回明确错误，
        #    绝不产出写死的流程定义（那会被前端当真实 AI 产出导入生产）。
        generated = self._llm_generate(prompt, example, conn, deep_thinking=deep_thinking)
        if not generated:
            return {
                "ok": False,
                "error": ("AI 流程生成不可用：未配置 LLM Key 或 LLM 调用失败，本次未生成任何流程定义。"
                          "请在「设置 → 大模型」中配置可用的 LLM Provider 后重试。"),
                "name": "", "description": "", "nodes": [], "edges": [],
                "validations": [], "has_error": True,
                "source": "unavailable", "prompt": prompt,
                "reasoning": "", "deep_thinking": deep_thinking,
            }
        # 2) 规范化 + 校验
        generated["nodes"] = self._normalize_nodes(generated.get("nodes", []))
        generated["edges"] = self._normalize_edges(generated.get("edges", []))
        validations = self.validate(generated["nodes"], generated["edges"])
        generated["validations"] = validations
        generated["has_error"] = any(v["level"] == "error" for v in validations)
        generated["ok"] = True
        generated["prompt"] = prompt
        generated["source"] = "llm" if generated.get("_source") == "llm" else "template"
        generated["reasoning"] = generated.pop("_reasoning", "")[:800]
        generated["deep_thinking"] = deep_thinking
        return generated

    # ── 迭代微调（多轮对话：携带历史 conversation，LLM 在完整上下文上修改）──
    def refine(self, prompt: str, definition: dict, conn=None, deep_thinking: bool = False,
               conversation: list | None = None) -> dict:
        """在原定义基础上按追加指令修改（LLM 重生成；失败回退原定义+说明）。

        conversation: 多轮对话历史 [{role, content}]（不含本次最新指令），
        与最新指令一起传给 LLM，保证迭代上下文连续、不覆盖前序意图。
        """
        if conn is not None and self.conn is None:
            self.conn = conn
            self._load_catalog(conn)
        prompt = (prompt or "").strip()
        if not prompt:
            return {"ok": False, "error": "迭代指令必填"}
        base = json.dumps(definition, ensure_ascii=False)[:4000]
        refined = self._llm_generate(prompt, None, conn, base_definition=base,
                                     deep_thinking=deep_thinking, conversation=conversation)
        if not refined:
            # ★ 2026-10-23：**不再回退 Mock 模板** —— LLM 不可用时如实报失败，
            #   保留原定义交由用户手动调整。绝不用写死模板冒充"AI 已完成修改"。
            return {"ok": False,
                    "error": ("AI 迭代修改不可用：未配置 LLM Key 或 LLM 调用失败，本次未做任何修改。"
                              "已保留原定义，请手动调整或配置 LLM Provider 后重试。"),
                    "name": definition.get("name") or "未命名流程",
                    "description": definition.get("description") or "",
                    "nodes": definition.get("nodes", []), "edges": definition.get("edges", []),
                    "validations": [], "has_error": True, "source": "unavailable"}
        refined["nodes"] = self._normalize_nodes(refined.get("nodes", []))
        refined["edges"] = self._normalize_edges(refined.get("edges", []))
        # LLM 未返回 name/description 时继承原定义（迭代不丢关键信息）
        if not (refined.get("name") or "").strip():
            refined["name"] = definition.get("name") or "未命名流程"
        if not (refined.get("description") or "").strip():
            refined["description"] = definition.get("description") or ""
        refined["validations"] = self.validate(refined["nodes"], refined["edges"])
        refined["has_error"] = any(v["level"] == "error" for v in refined["validations"])
        refined["ok"] = True
        refined["prompt"] = prompt
        refined["source"] = "template" if refined.get("_source") == "mock" else "llm"
        refined.pop("_source", None)
        refined["reasoning"] = refined.pop("_reasoning", "")[:800]
        refined["deep_thinking"] = deep_thinking
        return refined

    # ── 硬校验器（防幻觉：类型/工具/Agent/边端点/孤儿/重复 id）──
    def validate(self, nodes: list, edges: list) -> list:
        issues = []
        ids = [n.get("id") for n in nodes]
        seen = set()
        for i, n in enumerate(nodes):
            nid = n.get("id")
            if not nid:
                issues.append({"level": "error", "node": f"#{i}", "message": "节点缺少 id"})
                continue
            if nid in seen:
                issues.append({"level": "error", "node": nid, "message": f"重复节点 id: {nid}"})
            seen.add(nid)
            ntype = n.get("type")
            if ntype not in NODE_SCHEMA:
                issues.append({"level": "error", "node": nid, "message": f"未知节点类型: {ntype}（应为 {'/'.join(NODE_SCHEMA)}）"})
                continue
            cfg = n.get("config") or {}
            if ntype == "tool":
                tool = cfg.get("tool")
                if tool and tool not in self._tools:
                    issues.append({"level": "error", "node": nid, "message": f"工具不存在: {tool}"})
                elif not tool:
                    issues.append({"level": "warn", "node": nid, "message": "工具节点未配置 tool"})
            elif ntype == "agent":
                agent = cfg.get("agent")
                if agent and agent not in [a["name"] for a in self._agents]:
                    issues.append({"level": "error", "node": nid, "message": f"Agent 不存在: {agent}"})
                elif not agent:
                    issues.append({"level": "warn", "node": nid, "message": "Agent 节点未指定 agent"})
            elif ntype == "skill":
                skill = cfg.get("skill")
                if skill and self._skills and skill not in self._skills:
                    issues.append({"level": "warn", "node": nid, "message": f"Skill 不在发布清单: {skill}"})
            elif ntype == "if":
                if not cfg.get("expression"):
                    issues.append({"level": "warn", "node": nid, "message": "条件节点未配置 expression"})
            elif ntype == "llm":
                if not cfg.get("prompt"):
                    issues.append({"level": "warn", "node": nid, "message": "LLM 节点未配置 prompt"})
            elif ntype == "code":
                if not cfg.get("code"):
                    issues.append({"level": "warn", "node": nid, "message": "代码节点未配置 code"})
            elif ntype == "http":
                if not cfg.get("url"):
                    issues.append({"level": "warn", "node": nid, "message": "HTTP 节点未配置 url"})
            elif ntype == "iteration":
                if not cfg.get("subflow"):
                    issues.append({"level": "warn", "node": nid, "message": "迭代节点未配置 subflow"})
            elif ntype == "knowledge":
                if not cfg.get("query"):
                    issues.append({"level": "warn", "node": nid, "message": "知识节点未配置 query"})
        id_set = set(ids)
        for e in edges:
            s, t = e.get("source"), e.get("target")
            if s not in id_set:
                issues.append({"level": "error", "edge": f"{s}→{t}", "message": f"连线起点不存在: {s}"})
            if t not in id_set:
                issues.append({"level": "error", "edge": f"{s}→{t}", "message": f"连线终点不存在: {t}"})
            if e.get("when") not in (None, "true", "false"):
                issues.append({"level": "warn", "edge": f"{s}→{t}", "message": "when 只能是 true/false"})
        # 孤儿节点（无入边无出边且非唯一节点）
        if len(nodes) > 1:
            for nid in id_set:
                linked = any(e.get("source") == nid or e.get("target") == nid for e in edges)
                if not linked:
                    issues.append({"level": "warn", "node": nid, "message": "孤立节点（未连线）"})
        return issues

    # ── 模板检索复用（VectorEngine bigram 相似度）──
    def _retrieve_example(self, prompt: str, conn) -> str:
        try:
            from knowledge_engine import VectorEngine
            candidates = []
            if conn is not None:
                try:
                    for r in conn.execute("SELECT name, nodes FROM flow_templates").fetchall():
                        candidates.append({"text": f"{r['name']} {r['nodes'][:300]}", "json": r["nodes"]})
                except Exception:
                    pass
            if conn is not None:
                try:
                    for r in conn.execute("SELECT name, nodes FROM agent_flows ORDER BY id DESC LIMIT 20").fetchall():
                        candidates.append({"text": f"{r['name']} {r['nodes'][:300]}", "json": r["nodes"]})
                except Exception:
                    pass
            if not candidates:
                return ""
            scored = VectorEngine().search(prompt, candidates, top_k=1, key="text")
            if scored and scored[0][0] > 0.1:
                return scored[0][1]["json"][:1200]
        except Exception:
            pass
        return ""

    # ── LLM 结构化生成（真实模式，失败重试 2 次后回退 Mock）──
    def _llm_generate(self, prompt: str, example: str, conn, base_definition: str = "",
                      deep_thinking: bool = False, conversation: list | None = None) -> dict | None:
        try:
            from llm import llm_client
            sys_p = self._build_system_prompt(example, base_definition)
            messages = [{"role": "system", "content": sys_p}]
            # 多轮对话历史（迭代微调上下文，不含本次最新指令）
            for m in (conversation or []):
                role = m.get("role") in ("user", "assistant") and m.get("role") or "user"
                content = str(m.get("content") or "")
                if content:
                    messages.append({"role": role, "content": content})
            messages.append({"role": "user", "content": prompt})
            last = None
            retries = 1 if deep_thinking else 2  # 深度思考慢，单次尝试避免重试叠加超时
            for _ in range(retries):  # 重试：LLM 偶发超时/格式不稳
                try:
                    resp = llm_client.chat(messages, thinking=deep_thinking)
                    msg = (resp.get("choices") or [{}])[0].get("message", {})
                    content = msg.get("content") or ""
                    data = self._extract_json(content)
                    if data and isinstance(data, dict):
                        data["_source"] = "llm"
                        rc = msg.get("reasoning_content") or ""
                        if rc:
                            data["_reasoning"] = rc
                        return data
                except Exception as e:
                    last = e
            return None
        except Exception:
            return None

    def _build_system_prompt(self, example: str, base_definition: str) -> str:
        p = (
            "你是 MBSE 系统工程流程编排助手。根据用户任务描述生成 DAG 编排流程定义。\n\n"
            "可用节点类型与 config 字段（严格使用）：\n"
            "- llm: {prompt, system_prompt?, provider_id?}，prompt 支持 {{节点id.字段}} 引用上游输出\n"
            "- tool: {tool, arguments}，tool 必须是下列工具清单之一\n"
            "- agent: {agent, query}，agent 必须是下列 Agent 清单之一\n"
            "- skill: {skill}\n"
            "- mcp: {endpoint, tool, arguments}\n"
            "- if: {expression}，如 {{n1.data.result}} contains 冲突\n"
            "- orchestrator（Manager 多 Agent 协作）: {workers(逗号分隔 intent 名), task, strategy(contract_net/parallel/sequential)}\n"
            "- reflection（质量反思）: {target(被评审节点id), criteria(评估标准)}，低分配合 loop 循环边实现改进闭环\n\n"
            f"可用工具：{', '.join(self._tools) or '无'}\n"
            f"可用 Agent：{', '.join(a['name'] + '(' + a['display_name'] + ')' for a in self._agents) or '无'}\n"
            f"可用 Skill：{', '.join(self._skills) or '无'}\n"
        )
        if base_definition:
            p += f"\n用户已有流程定义，请按迭代指令在保持结构有效的前提下修改（保留无关节点/连线）：\n{base_definition}\n"
        elif example:
            p += f"\n最相似范例流程（参考其节点构成/连线模式，可替换其中的工具与 Agent 以适应用户需求）：\n{example}\n"
        p += (
            "\n输出要求：\n"
            "1. 只输出一个 JSON 对象，不要任何解释文字、不要 markdown 代码块标记\n"
            "2. 格式：{\"name\":\"流程名\",\"description\":\"说明\",\"nodes\":[{\"id\":\"n1\",\"type\":\"...\",\"label\":\"显示名\",\"config\":{...}}],\"edges\":[{\"source\":\"n1\",\"target\":\"n2\"}]}\n"
            "3. 节点 id 用 n1,n2,... 递增；edge 的 source/target 必须引用已存在的节点 id\n"
            "4. 引用的工具/Agent 必须来自上方清单，禁止编造\n"
            "5. 流程 3-8 个节点为宜，必要时用 if 节点做条件分支（分支边加 \"when\":\"true\"/\"false\"）\n"
            "6. 若任务描述包含多个阶段（如 需求分析→方案设计→代码生成→规则校验→输出），"
            "必须一次性生成覆盖全部阶段的完整链路，节点可超过 8 个，严禁只生成其中某一段"
        )
        return p

    @staticmethod
    def _extract_json(text: str) -> dict | None:
        """从 LLM 输出提取 JSON（容忍 ```json 包裹 / 前后多余文本）。"""
        if not text:
            return None
        m = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", text)
        if m:
            text = m.group(1)
        else:
            m = re.search(r"(\{[\s\S]*\})", text)
            if m:
                text = m.group(1)
        try:
            return json.loads(text)
        except Exception:
            return None

    # ── 2026-10-23：_mock_generate 已删除（米爸决策：删除 Mock 模板，失败即报错）──
    # 保留一个显式的"能力边界"标记，供调用方区分"没生成"与"生成失败"：
    # 无 LLM Key / LLM 调用失败 / 返回非 JSON → 一律 **不产出任何流程定义**。
    # 绝不用写死模板冒充 AI 产出（那会让用户把虚构流程导入生产）。
    _MOCK_REMOVED = True

    # ── 规范化（id 重排 / label 兜底 / config 类型安全）──
    @staticmethod
    def _normalize_nodes(nodes: list) -> list:
        out = []
        for i, n in enumerate(nodes or []):
            cfg = n.get("config") or {}
            if isinstance(cfg, str):
                try:
                    cfg = json.loads(cfg)
                except Exception:
                    cfg = {}
            if not isinstance(cfg, dict):
                cfg = {}
            nid = n.get("id") or f"n{i + 1}"
            out.append({
                "id": nid,
                "type": n.get("type", "llm"),
                "label": n.get("label") or NODE_SCHEMA.get(n.get("type", "llm"), {}).get("label", nid),
                "config": cfg,
            })
        return out

    @staticmethod
    def _normalize_edges(edges: list) -> list:
        out = []
        for e in edges or []:
            if not e.get("source") or not e.get("target"):
                continue
            item = {"source": e["source"], "target": e["target"]}
            if e.get("when") in ("true", "false"):
                item["when"] = e["when"]
            if e.get("condition"):
                item["condition"] = e["condition"]
            out.append(item)
        return out


# 全局实例
flow_copilot = FlowCopilot()
