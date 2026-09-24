# -*- coding: utf-8 -*-
"""AgentPipeline Mixin：SysML 视图抽取与富卡片（变更影响/评审）生成。

由 tools/split_pipeline.py 从 agent/pipeline.py 机械切分而成；⚠️ 切分脚本**已一次性执行完毕、不可重跑**—— 此后本文件按普通源码维护（方法体与其它模块一样可直接改）。"""
from .common import *


class CardMixin:
    """SysML 视图抽取与富卡片（变更影响/评审）生成。"""

    # ── P0：消息结论速览生成（AI 输出可折叠+集中阅读）──
    @staticmethod
    def build_message_summary(card_data: dict | None, content: str = "") -> dict:
        """规则法生成「结论速览」摘要（资料库与AI建模优化 §2.2.2 数据契约）。

        字段：
          headline: 一句话结论（≤30字）
          bullets: 要点 3-5 条
          artifacts: 关联产物（kind/label/ref）
          stats: 关键指标（tokens/elapsed_ms/tool_calls/subtasks）
          source: 'rule' | 'llm'（本期仅 'rule'，P1 可选 LLM 升级）
        """
        cd = card_data or {}
        bullets: list[str] = []
        artifacts: list[dict] = []
        stats: dict = {}

        # 0) 规范化 sysml_views：dict{parsed,views,intent,quality_check} / list[dict] / list[str]
        sysml_views_raw = cd.get("sysml_views")
        sysml_views: list = []
        sysml_kinds: list[str] = []
        if isinstance(sysml_views_raw, dict):
            views = sysml_views_raw.get("views") or {}
            if isinstance(views, dict):
                sysml_views = list(views.values())
                sysml_kinds = list(views.keys())
            elif isinstance(views, list):
                sysml_views = views
                sysml_kinds = [v.get("kind", "") for v in views if isinstance(v, dict)]
        elif isinstance(sysml_views_raw, list):
            sysml_views = sysml_views_raw
            sysml_kinds = [(v.get("kind") if isinstance(v, dict) else str(v)) for v in sysml_views_raw]

        # 1) headline：取第一个已完成子任务的 title
        plan = cd.get("plan") or []
        done_items = [p for p in plan if p.get("status") == "done"]
        first_done = done_items[0].get("title") if done_items else None
        if not first_done:
            if cd.get("degraded"):
                first_done = "部分子任务已完成"
            elif cd.get("orchestrated_status") == "failed":
                first_done = "执行失败，请查看详情"
            elif cd.get("orchestrated_status") == "partial":
                first_done = "部分完成，建议补充"
        if not first_done:
            exec_meta = cd.get("exec") or {}
            sub_titles = [s.get("title") for s in (exec_meta.get("subtasks") or []) if s.get("status") == "done"]
            first_done = sub_titles[0] if sub_titles else None
        if not first_done and cd.get("intent"):
            first_done = f"{cd.get('intent')} 任务已执行"
        if not first_done:
            first_done = (content[:24] + "…") if content else "任务已执行"

        # 2) bullets：从 plan/tools/sysml_views 聚合
        if done_items:
            for it in done_items[:4]:
                title = it.get("title") or it.get("key") or ""
                if title:
                    bullets.append(f"已完成：{title[:36]}")
        exec_meta = cd.get("exec") or {}
        for s in (exec_meta.get("subtasks") or []):
            if s.get("status") == "done" and len(bullets) < 5:
                title = s.get("title") or s.get("key") or ""
                if title and not any(title in b for b in bullets):
                    bullets.append(f"已完成：{title[:36]}")
        tools = exec_meta.get("tools") or cd.get("tools") or []
        if tools and len(bullets) < 5:
            ok = sum(1 for t in tools if t.get("ok") is True)
            fail = sum(1 for t in tools if t.get("ok") is False)
            if ok + fail > 0:
                bullets.append(f"工具调用 {ok} 成功 / {fail} 失败")
        if sysml_kinds and len(bullets) < 5:
            kinds_clean = [k for k in sysml_kinds if k and k != "None"][:4]
            if kinds_clean:
                bullets.append(f"生成视图：{', '.join(kinds_clean)}")
        # 方案/对比（design intent）
        if cd.get("intent") == "design" and len(bullets) < 5:
            bullets.append("生成多方案对比，等待确认")
        if cd.get("orchestrated_status") == "partial" and len(bullets) < 5:
            bullets.append("⚠️ 部分完成，建议补充缺失子任务")
        elif cd.get("degraded") and len(bullets) < 5:
            bullets.append("⚠️ 编排降级，建议手动调整")
        if not bullets:
            bullets.append(f"任务已处理（来源：{cd.get('intent') or 'chat'}）")

        # 3) artifacts：SysML 版本 + 工作流
        if sysml_views:
            first_v = sysml_views[0]
            ref = first_v.get("id", "") if isinstance(first_v, dict) else str(first_v)
            artifacts.append({
                "kind": "sysml", "label": f"SysML 视图 {len(sysml_views)} 个",
                "ref": f"views:{ref}"})
        if cd.get("saved_flow_id"):
            artifacts.append({"kind": "flow", "label": cd.get("saved_flow_name") or "自动编排流程",
                              "ref": f"flows:{cd['saved_flow_id']}"})

        # 4) stats
        rb = cd.get("run_budget") or {}
        if rb.get("used_tokens"):
            stats["tokens"] = rb["used_tokens"]
        if exec_meta.get("elapsed_ms"):
            stats["elapsed_ms"] = exec_meta["elapsed_ms"]
        stats["tool_calls"] = len(tools)
        stats["subtasks"] = f"{len(done_items)}/{len(plan) or len(exec_meta.get('subtasks') or [])}"

        from datetime import datetime as _dt
        return {
            "headline": first_done[:30] if first_done else "任务已执行",
            "bullets": bullets[:5],
            "artifacts": artifacts,
            "stats": stats,
            "source": "rule",
            "generated_at": _dt.now().isoformat(timespec="seconds"),
        }

    @staticmethod
    def _extract_sysml_code(text: str):
        """从 LLM 输出中提取 SysML v2 代码文本。

        2026-09-17 修复：原实现单遍正则把语言标签设为可选，导致前文 mermaid 等
        其他语言围栏的「闭合 ```」被误判为「开启围栏」，真正的 ```sysml 块开头
        被吞掉（实测：LLM 先给 mermaid BDD 再给 sysml 代码时，提取结果是标题
        + 尾注而非代码，视图投影 0 元素）。现改为两遍策略：
          1) 只匹配带 sysml/kerml 语言标签的围栏（精确，不受其他围栏干扰）；
          2) 无带标签围栏时才兜底匹配无标签围栏，且逐块校验内容含 V2 语态
             （part/requirement … def|usage、satisfies），不再整段放行。
        """
        if not text:
            return None
        feature_re = r"\b(?:part|requirement|action|attribute|interface|package|state|constraint|port)\s+(?:def|usage)\b"
        # 1) 带语言标签的围栏优先
        blocks = re.findall(r"```(?:sysml|kerml|SysML|SysMLv2|sysmlv2|kerml2|sysml_v2|sysmlv1)\s*\n(.*?)```", text, re.S)
        # 2) 无带标签围栏时，无标签围栏逐块按 V2 特征校验后兜底
        if not blocks:
            has_feature = bool(re.search(feature_re, text)) or " satisfies " in text
            if has_feature:
                blocks = [b for b in re.findall(r"```\s*\n(.*?)```", text, re.S)
                          if re.search(feature_re, b) or " satisfies " in b]
        if not blocks:
            return None
        cleaned = [b.strip() for b in blocks if b.strip()]
        return "\n\n".join(cleaned) if cleaned else None

    @classmethod
    def _ensure_sysml_blocks(cls, orch_content: str, source_items: list) -> str:
        """自动编排 LLM 摘要可能丢弃子任务交付的 SysML v2 代码块 → 汇总内容无可提取
        代码而子任务结果含代码时，从子任务补回（保证视图投影/版本建档/入库可用）。
        仅在汇总内容已无可提取 SysML 代码时补回，避免重复/污染。
        classmethod（原 staticmethod 裸引用 AgentPipeline 触发 NameError，致团队编排流中断：
        done 事件不发出 → 前端最终结果不渲染 + 主 Agent 卡停留「执行中」，2026-09-11 修复）"""
        if not source_items:
            return orch_content or ""
        if cls._extract_sysml_code(orch_content or ""):
            return orch_content or ""
        cands = []
        for it in source_items:
            res = (it.get("result") or "").strip()
            code = cls._extract_sysml_code(res) if res else None
            if code:
                cands.append((it.get("task_key") or "", it.get("agent_id") or "", code))
        if not cands:
            return orch_content or ""
        # 2026-09-20（实测 run 367）：多子任务各自产码时，把**所有**片段拼成一份交付物会得到一个
        # 「拼装体」——t2 的 335 行模型 + t3/t4 各自的片段拼在一起，单产物校验报 59 条错
        # （14 条硬错来自 t2，其余来自拼接错位），视图树也成了三份模型的混合（82 节点）。
        # 交付物必须对应**一份可校验的模型** → 默认只取代码最长的一份（通常即主设计交付物），
        # 并把来源子任务写进附录标题；`sysml.deliver_pick='all'` 可退回旧的全拼接行为。
        _pick = "longest"
        try:
            from core import config as _cfg
            _pick = str(_cfg.get("sysml", "deliver_pick", "longest") or "longest").strip().lower()
        except Exception:
            _pick = "longest"
        picked = cands if _pick == "all" else [max(cands, key=lambda x: len(x[2]))]
        _src = "、".join(f"{k}（{a}）" for k, a, _ in picked if k)
        _title = "（附：设计模型 SysML v2 代码" + (f"｜来源子任务 {_src}" if _src else "") + "）"
        return ((orch_content or "") + "\n\n" + _title + "\n\n"
                + "\n\n".join(f"```sysml\n{c}\n```" for _, _, c in picked))

    def _extract_view_types(self, user_input: str) -> list | None:
        """从用户输入抽取视图类型（BDD/IBD/PKG/PAR/REQ/UC/ACT/SEQ/STM/TRACE/视图）。

        仅在用户明确表达「要视图/图」诉求时提取（如「生成 BDD 视图」「只看需求图」），
        避免「需求」「参数」「活动」「状态」等普通词被误判为用户指定视图——
        视图类型应基于用户输入的真实意图动态确定。
        """
        if not user_input:
            return None
        # 视图诉求信号：输入须明确出现「视图/图/diagram/view」等要图字样才认定指定视图
        if not re.search(r"视图|图|diagram|view", user_input, re.I):
            return None
        # 关键词映射：英文缩写 → 视图类型；中文名 → 视图类型
        alias = {
            "BDD": "BDD", "块定义图": "BDD", "块定义": "BDD", "结构图": "BDD",
            "IBD": "IBD", "内部块图": "IBD", "内部块": "IBD",
            "PKG": "PKG", "包图": "PKG", "包": "PKG",
            "PAR": "PAR", "参数图": "PAR", "参数": "PAR",
            "REQ": "REQ", "需求图": "REQ", "需求": "REQ",
            "UC": "UC", "用例图": "UC", "用例": "UC",
            "ACT": "ACT", "活动图": "ACT", "活动": "ACT",
            "SEQ": "SEQ", "顺序图": "SEQ", "时序": "SEQ",
            "STM": "STM", "状态机": "STM", "状态图": "STM",
            "TRACE": "TRACE", "追溯图": "TRACE", "追溯": "TRACE",
            "视图": None,  # 通用词
        }
        hits = []
        text_lower = user_input.lower()
        for key, view_type in alias.items():
            if view_type is None:
                continue
            if key.lower() in text_lower:
                if view_type not in hits:
                    hits.append(view_type)
        return hits if hits else None

    def _infer_view_types(self, user_input: str) -> list | None:
        """基于用户输入内容识别建模视角 → 视图类型子集（无命中返回 None，不生成视图）。

        设计任务不再固定投影 BDD/IBD/PKG/PAR 四视图，而是按用户输入实际诉求
        （结构/需求/行为/接口/参数/用例/时序/状态/追溯）动态确定要生成的视图，
        避免无明确诉求时输出视图噪音。
        """
        if not user_input:
            return None
        # 建模视角关键词 → 对应视图类型（按 MBSE 视图语义分组，命中即加入）
        views_of_kind = [
            (["结构", "组成", "架构", "部件", "模块", "子系统", "拓扑", "块定义"], ["BDD", "PKG", "IBD"]),
            (["接口", "连接", "端口", "链路", "内部块", "交互关系"], ["IBD"]),
            (["需求", "指标", "任务书", "要求", "req"], ["REQ", "TRACE"]),
            (["行为", "流程", "活动", "过程", "动作", "act"], ["ACT", "SEQ"]),
            (["状态", "模式", "stm", "state"], ["STM"]),
            (["参数", "约束", "方程", "性能计算", "par"], ["PAR"]),
            (["用例", "场景", "用户", "uc"], ["UC"]),
            (["时序", "顺序", "交互", "消息", "seq"], ["SEQ"]),
            (["追溯", "trace", "满足", "验证"], ["TRACE"]),
            (["数据流", "数据", "dfd", "data flow"], ["ACT", "SEQ"]),
        ]
        text_lower = user_input.lower()
        hits: list[str] = []
        for kws, views in views_of_kind:
            if any(kw.lower() in text_lower for kw in kws):
                for v in views:
                    if v not in hits:
                        hits.append(v)
        return hits if hits else None

    @staticmethod
    def _code_has_structure(code: str) -> bool:
        """代码事实判据：这段 SysML v2 里是否真的定义了结构元素。

        为什么按「代码事实」而不按关键词：`_infer_view_types` 是纯关键词匹配，实测
        「先分析电动汽车热管理系统需求，再生成 SysML V2 模型代码并进行校验」只命中
        「需求」→ 只投影 REQ/TRACE，而交付代码里 20 个 `part def` / 27 个 `connect`
        一个结构视图都没出（2026-09-19 conv 369 实测）。
        关键词漏判不该让结构视图一起消失 —— 投影应服从模型里实际存在的东西。
        """
        if not code:
            return False
        return bool(re.search(
            r"\b(?:part|interface|port|item|occurrence|attribute)\s+def\b"
            r"|\bconnect\b|\ballocate\b|\bbind\b", str(code), re.I))

    @staticmethod
    def _check_generated_sysml(code: str) -> dict | None:
        """P2（集成指南 §2.2 接入点②）：生成后立刻用本地 `checker.jar` 校验 → 留痕摘要。

        为什么在这里：`_gen_sysml_views` 是 **5 个调用点的唯一收敛处**
        （`stream.py:598/686/1170`、`execute.py:363`、`orchestration.py:239`）——改一处全覆盖，
        避免「两函数同名并存被静默覆盖」那类事故重演。

        口径：**单产物**（只校验刚生成的这段代码，快且轻）→ 定位是**生成质量反馈**，
        不是工程门禁（工程门禁用项目级合并口径，见 `sysml_v2_check.check_project`）。
        单产物口径的跨文件伪错几乎全落在**语义路**，而门禁只认硬错（词法/语法）→ 伪错不影响判定。

        判据（2026-09-19 三路化：门禁只认**硬错** = 词法 + 语法，理由见 `sysml_v2_check` 纪律 ①③）：
          `pass` 放行 / `report` 只有语义错（**不阻断**，属建模决策）/ `block` 词法或语法硬错（待人工）。

        ⚠️ 本方法**只判错不修复**（集成指南 §5 边界：不自动改写代码）。
        ⚠️ 任何异常/校验器缺失/超时都降级为 `None` —— **绝不阻断建模主链路**。
        """
        try:
            from core import config as _cfg
            if not _cfg.get("sysml", "check_enabled", True):
                return None
            import sysml_v2_check as _svc
            r = _svc.check_code(
                code, timeout=int(_cfg.get("sysml", "check_timeout", 90) or 90))
            try:
                print(_svc.line_text(r), flush=True)   # 生成端留一行日志，便于线上排查
            except Exception:
                pass
            return _svc.summarize(r)
        except Exception:
            return None

    def _ensure_sysml_from_tools(self, content):
        """单 Agent 路径兜底：正文无 V2 代码、但本轮调过 sysml_v2_validate 时，把那段代码补回正文。

        2026-09-20：`sysml_v2_validate` 让 LLM 学会「先校验再交付」，代码因此从**回答正文**
        迁移到**工具参数**，而交付通道只认正文 → 模型越规范越交付不出来（复盘缺陷④）。
        补救：工具层已缓存最近一次校验过的代码（`tools.py` 的 sysml_v2_ 分派），此处补成代码块。
        幂等：正文已含可提取代码时原样返回。
        """
        try:
            if not content:
                return content
            if self._extract_sysml_code(content):
                return content
            code = (getattr(self, "_sysml_last_pass_code", None)
                    or getattr(self, "_sysml_last_checked_code", None))
            if not code or not str(code).strip():
                return content
            return (content + "\n\n（附：本轮已校验的 SysML v2 模型代码）\n\n```sysml\n"
                    + str(code).strip() + "\n```")
        except Exception:
            return content

    def _gen_sysml_views(self, llm_content, intent=None, user_input: str = ""):
        """LLM 输出含 SysML v2 代码 → 解析并投影与用户诉求匹配的视图 ViewModel；无代码/解析失败返回 None。

        视图类型确定优先级：
          1) 用户输入显式指定视图（如「生成 BDD 视图」）→ 用用户指定的；
          2) 设计(design)任务 → 基于用户输入内容识别建模视角动态确定视图子集
             （不再固定 BDD/IBD/PKG/PAR 四视图）；识别不到具体视角 → 不生成视图；
          3) 其他意图（impact/review/report/requirement_analysis 等）→ 按意图映射输出
             对应视图；意图不在映射内/映射为空（chat/knowledge_qa）→ 不生成。
        P1b-1：生成后自动附加「模型质量三件套」校验（约束/追溯/一致性，对齐 VP 活模型）。
        P2：生成后追加**本地 checker.jar 校验**（语法/语义双路计数 → views["check"] → 版本留痕），
            让错误「在入库前暴露」；只挂诊断、不阻断、不自动修复。
        """
        if not llm_content:
            return None
        # 2026-09-16：impact 意图不投影 SysML 视图——影响分析是分析能力不是建模能力，
        # LLM 输出中的代码/图块曾被投影成「代码/视图」标签页 + 归一校验，与 AI 建模能力混淆（用户确认移除）。
        if intent == "impact":
            return None
        code = self._extract_sysml_code(llm_content)
        if not code:
            # 2026-09-20：正文里没有 V2 代码时，取工具层缓存（LLM 把代码只放进
            # sysml_v2_validate 的 code 参数的情形，见 tools.py 的缓存注释）。
            code = (getattr(self, "_sysml_last_pass_code", None)
                    or getattr(self, "_sysml_last_checked_code", None))
        if not code:
            return None
        # ★ P2：生成后校验（挂诊断；失败/不可用一律降级，不影响下面的视图投影）
        _chk = self._check_generated_sysml(code)
        try:
            from view_generator import generate_views_from_sysml, INTENT_VIEWS
            # 1) 用户显式指定视图类型 → 优先于一切
            user_views = self._extract_view_types(user_input) if user_input else None
            if user_views:
                view_types = user_views
            # 2) 设计任务：按用户输入内容识别建模视角，动态确定视图子集
            elif intent == "design":
                view_types = self._infer_view_types(user_input)
                # 识别不到具体建模视角 → 不生成视图（避免 generate_views_from_sysml 内部
                # 对 None 兜底回退到 intent 固定映射，产生无诉求的视图噪音）
                if not view_types:
                    view_types = ["BDD", "IBD"]  # 问题3:识别不到视角时兜底默认结构视图,保证「代码/视图」切换可生成
                # 2026-09-19 conv 369：**部分命中**同样要看代码 —— 只命中「需求」时不该让结构视图消失。
                # 判据取「模型里确有结构元素」（代码事实），而非再加关键词：关键词必然有漏判，
                # 而 `part def`/`connect` 是模型事实。仅在**缺结构类视图且有结构元素**时补，不做无条件兜底。
                elif (not ({"BDD", "PKG", "IBD"} & set(view_types))
                      and self._code_has_structure(code)):
                    view_types = list(view_types) + ["BDD", "IBD"]
            # 3) 其他意图：按意图映射（chat/knowledge_qa 等映射为空 → 不生成视图）
            elif intent is not None:
                mapped = INTENT_VIEWS.get(intent)
                if not mapped:
                    return None
                view_types = mapped
            else:
                view_types = None
            views = generate_views_from_sysml(code, view_types=view_types, intent=intent)
            # P1b-1：模型质量三件套（约束/追溯/一致性）
            try:
                from model_quality import attach_quality_to_views
                views = attach_quality_to_views(views, code)
            except Exception:
                pass
            # P2：校验摘要挂到 views 的兄弟键（与既有 quality_check 同级；前端只迭代 views.views，零影响）
            if _chk and isinstance(views, dict):
                views["check"] = _chk
            return views
        except Exception:
            return None


    @staticmethod
    def _extract_stream_deltas(chunk):
        """解析 OpenAI 兼容 SSE 块，提取 delta.reasoning_content（思考）与 delta.content（正文）。

        chunk 形如 'data: {...}\\n\\n'；返回 [{"reasoning": str, "content": str}, ...]。
        """
        text = chunk if isinstance(chunk, str) else str(chunk)
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                payload = line[5:].strip()
                if payload == "[DONE]":
                    continue
                try:
                    obj = json.loads(payload)
                    delta = obj.get("choices", [{}])[0].get("delta", {}) or {}
                    reasoning = delta.get("reasoning_content") or ""
                    content = delta.get("content") or ""
                    if reasoning or content:
                        yield {"reasoning": reasoning, "content": content}
                except Exception:
                    continue

    def _build_rich_card(self, intent, retrieval, branch, user_input="", provider_id=None):
        """Build structured card data based on intent type."""
        if intent == "requirement_analysis":
            return self._card_candidates(retrieval, branch)
        elif intent == "impact":
            return self._card_impact(retrieval, branch, user_input, provider_id)
        elif intent == "review":
            return self._card_review(retrieval, branch)
        return {}

    def _card_candidates(self, retrieval, branch):
        """FR-MG-2/FR-HIL-1: Candidate requirement entries for human confirmation."""
        conn = get_db()
        candidates = conn.execute(
            "SELECT id, name, entity_type, properties, confidence FROM entities "
            "WHERE entity_type='需求' AND status='candidate' AND branch=? LIMIT 10", (branch,)
        ).fetchall()
        conflicts = []
        rels = conn.execute(
            "SELECT r.*, e1.name as src_name, e2.name as tgt_name FROM relations r "
            "JOIN entities e1 ON r.source_id=e1.id JOIN entities e2 ON r.target_id=e2.id "
            "WHERE r.relation_type='CONFLICTS' AND r.status='candidate'"
        ).fetchall()
        conn.close()
        items = []
        for c in candidates:
            props = json.loads(c["properties"]) if c["properties"] else {}
            items.append({
                "id": c["id"], "text": props.get("text", c["name"]),
                "source": props.get("source", "-"), "score": props.get("score", 0),
            })
        for r in rels:
            conflicts.append({"new": r["src_name"], "existing": r["tgt_name"]})
        return {"candidates": items, "conflicts": conflicts, "total_candidates": len(items)}

    def _llm_extract_change(self, user_input: str, provider_id=None) -> dict:
        """D1 结构化变更抽取（2026-09-16）：从变更描述中提取 {name, attribute, old_value, new_value, reason}。

        - name：变更对象（模型元素/参数/指标名称），供变更源匹配；
        - attribute/old_value/new_value：填充报告 1.2 变更内容表（缺失项由前端标"待验证"）；
        - reason：变更理由（技术必要性/业务驱动）。
        Mock 或解析失败 → 返回 {}（调用方回退规则子串匹配，报告按"不臆造"标注待验证）。
        """
        if not user_input or len(str(user_input).strip()) < 2:
            return {}
        try:
            from llm import llm_client
            resp = llm_client.chat(
                [{"role": "system", "content": (
                    "你是 MBSE 变更影响分析助手。从用户的变更描述中提取结构化变更信息，只输出一个 JSON 对象："
                    '{"name": "变更对象元素名称（如 功耗预算/V波段链路）", '
                    '"attribute": "被变更的属性/参数名（如 发射功率），无则 null", '
                    '"old_value": "当前值/基线值，无则 null", '
                    '"new_value": "目标值，无则 null", '
                    '"reason": "变更理由（技术必要性/业务驱动），无则 null"}。'
                    "不得虚构数值；描述中没有的信息输出 null。不要输出任何其他文字。")},
                 {"role": "user", "content": str(user_input)[:400]}],
                provider_id=provider_id,
                _intent="impact_change_extract",
            )
            msg = (resp.get("choices") or [{}])[0].get("message", {})
            content = (msg.get("content") or "").strip()
            import re as _re
            m = _re.search(r"\{[^{}]*\}", content, _re.S)
            if not m:
                return {}
            obj = json.loads(m.group(0))
            out = {}
            for k in ("name", "attribute", "old_value", "new_value", "reason"):
                v = str(obj.get(k) or "").strip()
                if v and v.lower() not in ("null", "none", "unknown", "待验证"):
                    out[k] = v
            return out
        except Exception:
            return {}
        return None

    def _llm_impact_advice(self, change_source: dict, affected: list,
                           edges: list, provider_id=None) -> dict | None:
        """LLM 生成变更影响分析的「影响程度解读 + 风险建议」（FR-CIA-2 影响程度分析）。

        输入为结构化影响集（变更源 + 受影响节点 + 影响边），输出风险条目
        [{level, title, desc, advice}]；解析失败/无 LLM 时返回 None，由调用方规则兜底。
        """
        try:
            from llm import llm_client
            top = sorted(affected, key=lambda n: -(n.get("score") or 0))[:8]
            ctx = {
                "change_source": change_source.get("name", ""),
                "direct": sum(1 for n in affected if n.get("impact") == "direct"),
                "indirect": sum(1 for n in affected if n.get("impact") == "indirect"),
                "high": sum(1 for n in affected if n.get("level") == "high"),
                "mid": sum(1 for n in affected if n.get("level") == "mid"),
                "low": sum(1 for n in affected if n.get("level") == "low"),
                "top_nodes": [{"name": n.get("name"), "type": n.get("type"),
                               "depth": n.get("depth"), "score": n.get("score")} for n in top],
                "relation_types": sorted({e.get("type") for e in edges}),
            }
            resp = llm_client.chat(
                [{"role": "system", "content": (
                    "你是 MBSE 变更影响分析专家。基于给定的变更源与影响集，输出 2-5 条风险与处置建议。"
                    "只输出一个 JSON 数组，格式：[{\"level\":\"high|mid|low\",\"title\":\"风险标题\","
                    "\"desc\":\"影响说明（含影响深度与广度）\",\"advice\":\"处置建议\"}]。"
                    "不要输出任何其他文字。")},
                 {"role": "user", "content": json.dumps(ctx, ensure_ascii=False)[:2500]}],
                provider_id=provider_id, _intent="impact_advice",
            )
            msg = (resp.get("choices") or [{}])[0].get("message", {})
            content = (msg.get("content") or "").strip()
            import re as _re
            m = _re.search(r"\[[\s\S]*\]", content)
            if m:
                items = json.loads(m.group(0))
                if isinstance(items, list):
                    return [{"level": i.get("level", "mid"), "title": str(i.get("title", ""))[:60],
                             "desc": str(i.get("desc", ""))[:200], "advice": str(i.get("advice", ""))[:200]}
                            for i in items[:5] if i.get("title")]
        except Exception:
            return None
        return None

    def _card_impact(self, retrieval, branch=None, user_input="", provider_id=None):
        """FR-CIA-1~4: 参数化变更影响分析（会话入口路径）。

        从 user_input 解析影响深度/方向（「N 层」「全部」「上游/下游」）；
        变更源优先级：输入元素名匹配 → 检索实体 → 兜底 REQ-BC-002；
        支持直接/间接区分 + 影响度分级（高/中/低，关系权重 × 层衰减）+ 无源/歧义引导结构；
        结果附深度/广度结构化统计（depth_stats/type_stats/rel_type_stats/domain_stats）与风险建议。
        """
        import re as _re
        conn = get_db()
        # 消费侧：影响分析只基于已发布(release)分支图谱（不混入开发中 dev 数据）
        kb_branches = GraphRAG._release_branches(conn)
        in_sql = ",".join("?" * len(kb_branches))
        entities = [dict(e) for e in conn.execute(
            f"SELECT id, name, entity_type, status FROM entities WHERE status!='deprecated' AND branch IN ({in_sql})",
            kb_branches
        ).fetchall()]
        relations = [dict(r) for r in conn.execute(
            f"SELECT r.*, e1.name as src_name, e2.name as tgt_name FROM relations r "
            f"JOIN entities e1 ON r.source_id=e1.id JOIN entities e2 ON r.target_id=e2.id "
            f"WHERE r.status!='deprecated' AND r.branch IN ({in_sql})",
            kb_branches
        ).fetchall()]
        conn.close()

        # ── 参数解析（自然语言 → 深度/方向）──
        depth = 3
        m = _re.search(r"(\d+)\s*(层|hop|hops|级)", user_input or "", _re.I)
        if m:
            depth = max(1, min(int(m.group(1)), 10))
        elif any(k in (user_input or "") for k in ("全部", "不限", "所有层")):
            depth = 0  # 0 = 不限深度
        direction = "both"
        if _re.search(r"上游|up(stream)?", user_input or "", _re.I):
            direction = "up"
        elif _re.search(r"下游|down(stream)?", user_input or "", _re.I):
            direction = "down"

        # ── 变更源解析：LLM 抽取 → 图谱匹配 → 输入子串 → 检索实体 → 兜底 ──
        change_source, err = None, None

        def _ambig(cands):
            return {"ok": False, "code": "SOURCE_AMBIGUOUS",
                    "reason": f"匹配到 {len(cands)} 个候选变更源元素",
                    "impact": "需先确认分析对象后才能传播影响",
                    "actions": ["select_candidate"],
                    "candidates": [{"id": e["id"], "name": e["name"], "type": e["entity_type"]}
                                   for e in cands[:10]]}

        # 1) LLM 结构化变更抽取（name 供变更源匹配；attribute/old/new/reason 供报告 1.2/1.3，2026-09-16 D1）
        _chg_ext = self._llm_extract_change(user_input, provider_id) or {}
        _ext = (_chg_ext.get("name") or "").strip()
        if _ext:
            _eq = _ext.lower()
            _exact = [e for e in entities if (e.get("name") or "").lower() == _eq]
            if len(_exact) == 1:
                change_source = _exact[0]
            elif len(_exact) > 1:
                err = _ambig(_exact)
            else:
                _m = [e for e in entities
                      if (e.get("name") or "").lower()
                      and (_eq in e["name"].lower() or e["name"].lower() in _eq)]
                if len(_m) == 1:
                    change_source = _m[0]
                elif len(_m) > 1:
                    err = _ambig(_m)
        # 2) 回退：原始输入子串匹配（现状规则，LLM 抽取失败/无 key 时兜底）
        if not change_source and not err:
            q = (user_input or "").lower()
            if q:
                matched = [e for e in entities
                           if (e.get("name") or "").lower() and e["name"].lower() in q]
                if len(matched) == 1:
                    change_source = matched[0]
                elif len(matched) > 1:
                    err = _ambig(matched)
        # 3) 检索实体
        if not change_source and not err:
            for e in retrieval.get("entities", []):
                if e.get("status", "") != "deprecated":
                    change_source = e
                    break
        # 4) 兜底
        if not change_source and not err:
            for e in entities:
                if "REQ-BC-002" in e["id"] or "BC-002" in e["id"]:
                    change_source = e
                    break
        if not change_source and not err:
            err = {"ok": False, "code": "NO_CHANGE_SOURCE",
                   "reason": "未识别到变更源：描述过于宽泛或图谱中无匹配元素",
                   "impact": "无法确定分析起点，无法传播影响",
                   "actions": ["select_source", "retry"]}
        if err:
            return {**err, "params": {"depth": depth, "direction": direction, "relation_types": []}}

        # ── 变更类型解析（2026-09-15 报告 2.0：关键词 → LLM 兜底）──
        change_type, change_desc = "attribute", (user_input or "").strip()
        if _re.search(r"改名|重命名|更名|rename", user_input or "", _re.I):
            change_type = "rename"
        elif _re.search(r"删除|移除|去掉|delete", user_input or "", _re.I):
            change_type = "delete"
        elif _re.search(r"接口|端口|连接关系|interface", user_input or "", _re.I):
            change_type = "interface"
        elif _re.search(r"参数|取值|阈值|预算|指标值|value", user_input or "", _re.I):
            change_type = "value"

        # ── 调用共享影响引擎（报告 2.0：变更类型感知 + CPM-lite 路径枚举 + 组合风险）──
        # P1 2026-09-01：graph_db.enabled 时优先 SPARQL 真图遍历（分层 BFS，分支命名图），
        # 空网络/异常回退 SQLite 全量（分析结果字段与 analyze_graph 兼容）。
        try:
            from core import config
            if config.as_bool("graph_db", "enabled"):
                from graph_db import get_writer, fetch_impact_network
                _w = get_writer(True, config.get("graph_db", "backend", "pyoxigraph"),
                                config.get("graph_db", "path", ""),
                                config.get("graph_db", "use_memory", False))
                try:
                    _fc = get_db()
                    try:
                        _net = fetch_impact_network(_w, _fc, change_source, depth,
                                                    direction, branches=kb_branches)
                    finally:
                        _fc.close()
                    if _net and _net.get("nodes"):
                        entities, relations = _net["nodes"], _net["edges"]
                finally:
                    _w.close()
        except Exception:
            pass  # 图库路径失败 → 回退 SQLite 全量

        # rename 专用路径：名字是标识的显示层，结构依赖零传播 → 引用扫描（不跑 BFS）
        if change_type == "rename":
            from services.impact_engine import scan_name_references, decide_recommendation
            _rc = get_db()
            try:
                refs = scan_name_references(_rc, change_source.get("name", ""))
            finally:
                _rc.close()
            card = {"ok": True, "change": {"type": "rename", "desc": change_desc, "type_label": "改名"},
                    "change_source": change_source, "references": refs,
                    "params": {"depth": depth, "direction": direction, "change_type": "rename",
                               "change_desc": change_desc, "relation_types": [], "reference_sources": [],
                               "use_vector": False}}
            card["decisions"] = decide_recommendation(card)
            return card

        from services.impact_engine import analyze_graph_v2, decide_recommendation
        card = analyze_graph_v2(entities, relations, change_source, depth, direction,
                                change_type=change_type, change_desc=change_desc)
        # 混合溯源（FR-CIA-1，2026-09-15 落地）：受影响 Top 元素 → 向量库文档证据。
        # 图谱是主证据源；知识库不完整时用向量库检索补充佐证（evidence + use_vector 真实置位）。
        try:
            from services.impact_engine import attach_evidence
            _ev_conn = get_db()
            try:
                attach_evidence(_ev_conn, card)
            finally:
                _ev_conn.close()
        except Exception:
            pass
        # D1 结构化变更字段（2026-09-16）：填充报告 1.2 变更内容表 / 1.3 变更理由（缺失项前端标待验证）
        try:
            _chg = card.get("change") or {}
            _chg["attribute"] = _chg_ext.get("attribute") or ""
            _chg["old_value"] = _chg_ext.get("old_value") or ""
            _chg["new_value"] = _chg_ext.get("new_value") or ""
            _chg["reason"] = _chg_ext.get("reason") or ""
            card["change"] = _chg
        except Exception:
            pass
        # S2/C3 基线元数据（2026-09-16）：分支 + 已发布版本 + 悬空实例一致性扫描 → 报告第 2 章
        try:
            from services.impact_engine import baseline_meta
            _bm = get_db()
            try:
                card["baseline"] = baseline_meta(_bm, kb_branches)
            finally:
                _bm.close()
        except Exception:
            pass
        # 重测清单 + 工作量估算表（报告 2.0 P3，Wiegers 面向三）
        try:
            from services.impact_engine import build_retest_plan
            card = build_retest_plan(entities, relations, card)
        except Exception:
            pass
        # 决策建议（可解释规则引擎，FR-CIA 报告 2.0 执行摘要）
        try:
            card["decisions"] = decide_recommendation(card)
        except Exception:
            pass
        # LLM 风险建议（解析失败时引擎内的规则风险兜底已存在）
        affected = [n for n in card.get("impact_nodes", []) if n["impact"] != "source"]
        _advice = self._llm_impact_advice(change_source, affected,
                                          card.get("impact_edges", []), provider_id)
        if _advice:
            card["risk_analysis"]["risks"] = card["risk_analysis"]["risks"] + _advice[:4]
        return card

    def _card_review(self, retrieval, branch=None):
        """FR-VR-1/FR-VC-1~5: Review results structured data."""
        conn = get_db()
        total = conn.execute("SELECT COUNT(*) FROM entities WHERE status!='deprecated'").fetchone()[0]
        reviewed = conn.execute("SELECT COUNT(*) FROM entities WHERE status='reviewed'").fetchone()[0]
        candidates = conn.execute("SELECT COUNT(*) FROM entities WHERE status='candidate'").fetchone()[0]
        conflicts = conn.execute("SELECT COUNT(*) FROM relations WHERE relation_type='CONFLICTS'").fetchone()[0]
        conn.close()
        score = min(60 + reviewed * 5 - conflicts * 10, 100)
        issues = []
        if conflicts > 0:
            issues.append({"level": "error", "type": "逻辑一致性", "desc": f"检测到 {conflicts} 项指标冲突", "fix": "发起变更影响分析后归并"})
        if candidates > 0:
            issues.append({"level": "warning", "type": "规范符合性", "desc": f"{candidates} 个候选元素待审核", "fix": "提交知识工程师审核"})
        issues.append({"level": "warning", "type": "规范符合性", "desc": "部分模块未应用构造型", "fix": "一键应用 Profile"})
        issues.append({"level": "info", "type": "合理性", "desc": "吞吐指标高于近3年同类型号均值", "fix": "复核指标论证材料"})
        return {
            "score": score, "total_elements": total, "reviewed": reviewed, "candidates": candidates,
            "syntax_errors": conflicts, "logic_issues": conflicts + 2,
            "spec_deviations": candidates, "info_hints": 1,
            "issues": issues, "conclusion": "有条件通过" if score >= 70 else "不通过",
        }

    # ── 附件内容解析：上传资料 → 文本（参与检索与 LLM 上下文）──
    # 支持的文本类：txt/md/csv/json/xml/log/sysml；pdf（pdfplumber）；docx；xlsx（openpyxl）
    # 图片/zip/ppt 等无法直接解析 → 跳过并在返回中标注（前端展示"未解析"）
