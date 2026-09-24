# -*- coding: utf-8 -*-
"""AgentPipeline Mixin：会话状态、意图槽位与澄清（clarify）持久化。

由 tools/split_pipeline.py 从 agent/pipeline.py 机械切分而成；⚠️ 切分脚本**已一次性执行完毕、不可重跑**—— 此后本文件按普通源码维护（方法体与其它模块一样可直接改）。"""
from .common import *

# 2026-09-17 S4：澄清探测（_clarify_detect）的确定性前置规则——命中即跳过，省一次 LLM 调用。
# 该探测此前每次请求都会调用一次 LLM（usage: intent='clarify_detect'，prompt 139~152）。
# 规则 2 的领域关键词直接复用 router.INTENTS / router._db_intents，此处只补通用动作/建模动词。
_CLARIFY_ACTION_VERBS = (
    "生成", "建模", "创建", "新增", "添加", "修改", "更新", "删除", "移除", "导入", "导出",
    "分析", "绘制", "写出", "输出", "设计", "编写", "评审", "校验", "检查", "统计", "列出",
    "保存", "入库", "提交", "合并", "对比", "梳理", "拆解", "推导", "补全", "重构",
)
# 规则 3 阈值：长输入信息量通常已足够，反问反而打断
_CLARIFY_SKIP_MIN_CHARS = 80


class SessionMixin:
    """会话状态、意图槽位与澄清（clarify）持久化。"""

    def _load_conversation_dst(self, conversation_id) -> dict:
        """P0-2 轻量 DST：读取会话级意图状态 {intent, slots}（conversation_id<=0 返回空）。"""
        if not conversation_id or int(conversation_id) <= 0:
            return {"intent": "", "slots": {}}
        try:
            from database import get_db
            conn = get_db()
            try:
                row = conn.execute(
                    "SELECT current_intent, last_slots FROM conversations WHERE id=?",
                    (int(conversation_id),)).fetchone()
                if not row:
                    return {"intent": "", "slots": {}}
                slots = {}
                try:
                    slots = json.loads(row["last_slots"] or "{}")
                except Exception:
                    slots = {}
                return {"intent": row["current_intent"] or "", "slots": slots if isinstance(slots, dict) else {}}
            finally:
                conn.close()
        except Exception:
            return {"intent": "", "slots": {}}

    def _save_conversation_dst(self, conversation_id, intent, slots=None) -> None:
        """P0-2 轻量 DST：回写会话级意图状态（当前意图 + 槽位 JSON；conversation_id<=0 跳过）。"""
        if not conversation_id or int(conversation_id) <= 0:
            return
        try:
            from database import get_db
            conn = get_db()
            try:
                conn.execute(
                    "UPDATE conversations SET current_intent=?, last_slots=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (intent, json.dumps(slots or {}, ensure_ascii=False), int(conversation_id)))
                conn.commit()
            finally:
                conn.close()
        except Exception:
            pass

    def _merge_slots(self, prev_slots: dict, new_slots: dict) -> dict:
        """P0-2 槽位跨轮合并：prev 为底，新槽位覆盖；goal/scope 新值优先，entities/constraints 并集。"""
        if not isinstance(prev_slots, dict):
            prev_slots = {}
        if not isinstance(new_slots, dict):
            new_slots = {}
        out = {k: v for k, v in prev_slots.items()}
        for k, v in new_slots.items():
            if k in ("entities", "constraints") and isinstance(v, list):
                seen = list(out.get(k) or [])
                for item in v:
                    if item not in seen:
                        seen.append(item)
                out[k] = seen
            elif k in ("scope",) and isinstance(v, dict):
                sc = dict(out.get(k) or {})
                sc.update(v)
                out[k] = sc
            elif v:
                out[k] = v
            else:
                out.setdefault(k, v)
        return out

    def _clarify_candidates(self, current: str) -> list:
        """澄清候选：除当前意图外的已知意图列表（供前端「改选重发」按钮）。"""
        known = [k for k in self.router.INTENTS.keys() if k != "chat"]
        for name in self.router._db_intents.keys():
            if name not in known:
                known.append(name)
        return [k for k in known if k != current][:5]

    # ── 内容级澄清：信息不清晰 → 选择题确认（优先选择题，支持补充输入），回答后续答 ──
    # 混合触发：LLM 声明式 quick 判定为主 + 规则兜底（输入过短/未提及领域实体）
    CLARIFY_INTENTS = ("design", "requirement_analysis", "impact", "requirement_quality", "analysis")
    CLARIFY_RESUME_MARK = "【澄清补充】"

    def _clarify_detect(self, user_input, intent, provider_id=None, forced_intent=None, skill_name=None) -> list | None:
        """检测是否需要内容级澄清。返回 questions 列表或 None。

        questions: [{id, question, options[], allow_custom}]
        - 续答消息（含 CLARIFY_RESUME_MARK）跳过（防澄清循环）
        - 仅建模类意图参与；纯问答（chat/knowledge_qa）不打断
        - LLM quick 判定（非流式小调用，输出 JSON）失败/Mock 兜底 → 规则兜底
        - 规则兜底：输入过短（<18 字）或 <45 字且未提及领域实体 → 通用澄清题
        """
        # 2026-09-17 S4：本次澄清探测的跳过原因（空串 = 未命中前置规则，仍会走下方 LLM 探测）
        self._last_clarify_skip = ""
        if intent not in self.CLARIFY_INTENTS:
            return None
        if self.CLARIFY_RESUME_MARK in (user_input or ""):
            return None
        # 2026-09-17 S4：确定性前置规则——任一命中即视为信息已足够，直接跳过澄清探测（零 LLM 调用）
        # 命中原因写入 self._last_clarify_skip 供观测；命不中（空串）时下方 LLM 探测行为完全不变。
        self._last_clarify_skip = self._clarify_skip_reason(user_input, forced_intent, skill_name)
        if self._last_clarify_skip:
            return None
        questions = None
        try:
            from llm import llm_client
            resp = llm_client.chat([
                {"role": "system", "content": (
                    "你是 MBSE 建模澄清助手。判断用户建模/分析请求的信息是否足以支撑执行。"
                    "若缺少关键信息（建模对象、范围、指标、约束、验收标准等）需要向用户确认，输出 JSON："
                    '{"need":true,"questions":[{"id":"q1","question":"请用一句话提问","options":["选项A","选项B","选项C"],"allow_custom":true}]}'
                    "每题最多 4 个选项（allow_custom=true 表示允许用户输入补充信息）；"
                    "信息充足输出 {\"need\":false}。只输出 JSON，不要其他文字。")},
                {"role": "user", "content": str(user_input)[:400]}],
                _intent="clarify_detect")
            content = (resp.get("choices") or [{}])[0].get("message", {}).get("content") or ""
            j = self._parse_json_block(content)
            if isinstance(j, dict) and j.get("need") and isinstance(j.get("questions"), list) and j["questions"]:
                questions = [{
                    "id": str(q.get("id") or f"q{i + 1}"),
                    "question": str(q.get("question") or "")[:120],
                    "options": [str(o)[:40] for o in (q.get("options") or [])[:4] if str(o).strip()],
                    "allow_custom": bool(q.get("allow_custom", True)),
                } for i, q in enumerate(j["questions"][:3]) if str(q.get("question") or "").strip()]
        except Exception:
            questions = None
        # 规则兜底：LLM 判定失败/未触发，但输入明显不足 → 通用澄清题（不降级猜测执行）
        if not questions:
            text = (user_input or "").strip()
            _has_entity = any(e in text for e in (
                "载荷", "卫星", "天线", "转发器", "需求", "系统", "方案", "接口", "链路",
                "功放", "热控", "电源", "建模", "sysml", "用例", "架构", "配置", "参数"))
            if len(text) < 18 or (len(text) < 45 and not _has_entity):
                questions = [{
                    "id": "q_scope",
                    "question": "您的请求缺少关键信息，请确认本次建模/分析的对象与范围（避免我按猜测执行）",
                    "options": ["按我的描述直接执行（上下文已足够）", "先做需求分析", "聚焦某一子系统（请补充）", "生成 SysML 模型代码"],
                    "allow_custom": True,
                }]
        return questions

    def _clarify_skip_reason(self, user_input, forced_intent=None, skill_name=None) -> str:
        """2026-09-17 S4：返回跳过澄清探测的原因（非空 = 跳过，确定性、零 LLM 调用）。

        规则（任一命中即跳过，视为信息已足够）：
          1) quick_pick：快捷指定 Agent / 技能（forced_intent / skill_name）——用户已表达意图
          2) action_kw:<词>：输入含明确建模/动作动词（复用 router.INTENTS 与 router._db_intents
             的领域关键词，再叠加 _CLARIFY_ACTION_VERBS 通用动作动词）
          3) long_input：输入长度 >= _CLARIFY_SKIP_MIN_CHARS（长输入信息量足够，不必反问）
        命不中 → 返回 ""，调用方继续走原有 LLM 澄清探测（行为不变）。
        """
        if forced_intent or skill_name:
            return "quick_pick"
        text = str(user_input or "").strip()
        if len(text) >= _CLARIFY_SKIP_MIN_CHARS:
            return "long_input"
        low = text.lower()
        try:
            kws = []
            for _vs in (self.router.INTENTS or {}).values():
                kws.extend(_vs or [])
            for _vs in (getattr(self.router, "_db_intents", {}) or {}).values():
                kws.extend(_vs or [])
        except Exception:
            kws = []
        kws.extend(_CLARIFY_ACTION_VERBS)
        for k in kws:
            if k and str(k).lower() in low:
                return "action_kw:" + str(k)
        return ""

    def _persist_clarify(self, conversation_id, user_input, intent, branch, attachments,
                         forced_intent, skill_name, team, questions) -> int:
        """落库澄清挂起状态（conversations.pending_clarify）+ 澄清卡消息（msg_type='clarify'）。
        返回澄清消息 id（供 done 事件携带，前端据此定位重建后的消息容器）。失败返回 0。"""
        _msg_id = 0
        try:
            _pending = {
                "questions": questions,
                "context": {"input": str(user_input)[:2000], "intent": intent, "branch": branch,
                            "attachments": attachments or [], "forced_intent": forced_intent or "",
                            "skill_name": skill_name or "", "team": team or ""},
            }
            from database import db_conn
            with db_conn() as _cc:
                _cc.execute(
                    "UPDATE conversations SET pending_clarify=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (json.dumps(_pending, ensure_ascii=False), conversation_id))
                cur = _cc.execute(
                    "INSERT INTO messages (conversation_id, role, content, msg_type, card_data) "
                    "VALUES (?,?,?,?,?)",
                    (conversation_id, "assistant", "需要您确认建模信息后继续",
                     "clarify", json.dumps({"questions": questions}, ensure_ascii=False)))
                _msg_id = cur.lastrowid or 0
        except Exception:
            pass
        return _msg_id
