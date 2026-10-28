# -*- coding: utf-8 -*-
"""AgentPipeline Mixin：会话状态、意图槽位与澄清（clarify）持久化。

由 tools/split_pipeline.py 从 agent/pipeline.py 机械切分而成；⚠️ 切分脚本**已一次性执行完毕、不可重跑**—— 此后本文件按普通源码维护（方法体与其它模块一样可直接改）。"""
from .common import *

# 2026-09-17 S4：澄清探测（_clarify_detect）的确定性前置规则——命中即跳过，省一次 LLM 调用。
# 该探测此前每次请求都会调用一次 LLM（usage: intent='clarify_detect'，prompt 139~152）。
# 规则 2 的领域关键词直接复用 router._db_intents，此处只补通用动作/建模动词。
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

    # ── 2026-10-16 R1：会话级主 Agent（执行主体持久化） ─────────────────────
    #   为什么单独立方法而不塞进 DST：DST 管的是"这一轮在聊什么意图"（每轮都变），
    #   主 Agent 管的是"这个会话由谁执行"（用户选定后长期不变）—— 两者生命周期不同，
    #   混在一个 JSON 里会被 `_merge_slots` 的覆盖语义冲掉。
    def _load_main_agent(self, conversation_id) -> str:
        """读会话绑定的主 Agent 名（空串=未绑定/会话不存在）。绝不抛。"""
        if not conversation_id or int(conversation_id) <= 0:
            return ""
        try:
            from repositories.conversation_repo import ConversationRepo
            from database import get_db
            conn = get_db()
            try:
                return ConversationRepo(conn).get_main_agent(int(conversation_id))
            finally:
                conn.close()
        except Exception:
            return ""

    def _save_main_agent(self, conversation_id, main_agent: str) -> None:
        """把所选主 Agent 落到会话（幂等）。失败不影响本轮执行——静默但留日志。"""
        if not conversation_id or int(conversation_id) <= 0 or not main_agent:
            return
        try:
            from repositories.conversation_repo import ConversationRepo
            from database import get_db
            conn = get_db()
            try:
                ConversationRepo(conn).set_main_agent(int(conversation_id), str(main_agent))
                conn.commit()
            finally:
                conn.close()
        except Exception as _e:
            try:
                logger.warning("[main-agent] 持久化失败（不影响本轮）：%s", _e)
            except Exception:
                pass

    # ── P0-5（2026-09-30）：意图识别用的"最近几轮用户原话" ─────────────────────────
    #  与 `context.history_*`（给**生成模型**注入的对话上下文，默认 6 轮）刻意分开：
    #  意图识别只需要"最近 1~2 条用户话"，且**只取 user 且 msg_type=text** ——
    #  取值面越窄噪声越少（净化规则与实测见 IntentRouter._sanitize_history）。
    def _load_recent_user_msgs(self, conversation_id, n: int = 2) -> list:
        """取最近 n 条用户文本消息（**时间正序**返回）；conversation_id<=0 / n<=0 返回 []。

        注意这是**原始池**，不做净化：`role` 与 `msg_type` 只是最外层的粗筛（排除卡片/工具消息），
        「澄清续答」「编排子任务文本」等脏样本仍会取到 —— 它们在 IntentRouter._sanitize_history
        里统一剔除。所以调用方应取**多于实际需要**的条数（见 _load_history_for_intent）。
        """
        try:
            if not conversation_id or int(conversation_id) <= 0 or int(n) <= 0:
                return []
            from database import get_db
            conn = get_db()
            try:
                rows = conn.execute(
                    "SELECT content FROM messages WHERE conversation_id=? AND role=? "
                    "AND msg_type=? ORDER BY id DESC LIMIT ?",
                    (int(conversation_id), "user", "text", int(n))).fetchall()
            finally:
                conn.close()
            out = [(r["content"] or "").strip() for r in rows]
            return [x for x in reversed(out) if x]
        except Exception:
            return []

    def _load_history_for_intent(self, conversation_id) -> list:
        """P0-5：意图识别用的历史（读配置 → 取最近 N 条用户话）。开关关闭/取不到时返回 []。"""
        try:
            from core import config as _cfg
            if not bool(_cfg.get("intent", "history_recall", True)):
                return []
            n = int(_cfg.get("intent", "history_recall_turns", 2) or 0)
        except Exception:
            n = 2
        # 原始池留足余量（4 倍，下限 8）：真实会话里 user 消息夹着大量"澄清续答"
        #   （【澄清补充】卡片文本，量过会话 325：20 条里超过一半，且**最新的几条恰恰都是它**）
        #   —— 只按 n 取原文，常常一条有效历史都不剩，P0-5 反而在最需要它的长会话里失效。
        #   净化与"最终取几条"仍由 intent 侧的 _sanitize_history 负责，这里不重复判脏。
        return self._load_recent_user_msgs(conversation_id, max(int(n) * 4, 8))

    def _merge_slots(self, prev_slots: dict, new_slots: dict,
                     conversation_id=None, user_input=None) -> dict:
        """P0-2 槽位跨轮合并 + P1-4 治理（2026-10-01）。

        prev 为底，新槽位覆盖；goal/scope 新值优先；entities/constraints 走
        `merge_slot_items`（归一化去重 + 子串合并 + 上限收敛，**本轮优先**）。
        P1-4 L2：本轮若换了话题（判据见 `_is_topic_switch`，与 `_tag_topics` 同源）
        → 丢弃**历史** entities/constraints（goal/scope 仍按新值覆盖）；
        否则跨话题的旧对象会一直挂在 prompt 的「【任务拆解】」段里，单调增长且永不衰减。

        消费面（已全枚举）：slots 只进①单 Agent 直答/生成路径的 system prompt
        （stream.py / execute.py 的「【任务拆解（P1 结构化）】」段）、②前端卡片、
        ③落库 `conversations.last_slots`；**编排/planner 不消费 slots**（`_try_orchestrate`
        不收该参数）。`conversation_id`/`user_input` 缺省时退化为旧的纯合并语义。
        """
        if not isinstance(prev_slots, dict):
            prev_slots = {}
        if not isinstance(new_slots, dict):
            new_slots = {}
        try:
            from core import config as _cfg
            _norm = bool(_cfg.get("slots", "merge_normalize_dedup", True))
            _sub = bool(_cfg.get("slots", "merge_substring_dedup", True))
            _max_e = int(_cfg.get("slots", "max_entities", 10) or 0)
            _max_c = int(_cfg.get("slots", "max_constraints", 8) or 0)
            _reset = bool(_cfg.get("slots", "reset_on_topic_switch", True))
        except Exception:
            _norm, _sub, _max_e, _max_c, _reset = True, True, 10, 8, True
        _caps = {"entities": _max_e, "constraints": _max_c}
        # P1-4 L2：换话题 → 只丢**历史** entities/constraints（本轮值仍在 new_slots 里）
        if _reset and conversation_id and user_input:
            try:
                if self._is_topic_switch(conversation_id, user_input):
                    prev_slots = {k: v for k, v in prev_slots.items()
                                  if k not in ("entities", "constraints")}
            except Exception:
                pass
        self._slot_merge_stats = {}
        out = {k: v for k, v in prev_slots.items()}
        for k, v in new_slots.items():
            if k in ("entities", "constraints") and isinstance(v, list):
                if _norm or _sub:
                    merged, _st = merge_slot_items(out.get(k), v, max_items=_caps.get(k, 0),
                                                   normalize=_norm, substring=_sub)
                    out[k] = merged
                    self._slot_merge_stats[k] = _st
                else:
                    seen = list(out.get(k) or [])   # 旧行为（A/B 回滚用）
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
        """澄清候选：除当前意图外的已知意图列表（供前端「改选重发」按钮）。

        ★ 2026-10-17（米爸反馈"澄清候选每次都是固定清单"）：排序改按**本次输入的语义
        意图级排名**（`router._last_sem_ranking`，detect_semantic 旁路存下，动态），
        字典序仅兜底 —— 与意图确认卡同口径（此前是字典序前 5，换输入不换清单）。
        """
        known = [k for k in self.router._db_intents.keys() if k != "chat"]
        _ranking = [(_r.get("intent", "") if isinstance(_r, dict) else str(_r[0]))
                    for _r in (getattr(self.router, "_last_sem_ranking", None) or [])]
        _out, _seen = [], set()
        for _n in list(_ranking) + known:
            if _n and _n != current and _n != "chat" and _n not in _seen:
                _seen.add(_n)
                _out.append(_n)
        return _out[:5]

    # ── 2026-09-26 意图确认（「确定不了就别硬选」，用户明确要求）─────────────────────
    #  背景：低置信/无信号时系统会**自己挑一个**意图去执行 —— 「帮我看看这个项目的预算」落到 chat，
    #  更糟的是被 LLM 以 0.60 猜成 report_generation 真去跑报告 Agent。用户要的是"停下来问"。
    #  做法：复用**既有内容级澄清的那张选择题卡**（选项 + 其他/自定义 + 答完续跑），因为
    #  「不硬选」的完整机制后端已经有了（落挂起 → /clarify-answer → 带【澄清补充】续跑），无需另造。
    _INTENT_LABEL = {
        "requirement_analysis": "需求分析", "requirement_quality": "需求质量评审",
        "design": "设计/建模", "impact": "变更影响分析", "review": "评审/校验",
        "report_generation": "报告生成", "system_mgmt": "系统管理",
        "knowledge_qa": "知识问答", "chat": "闲聊/其他",
        # ★ 2026-10-17：候选清单扩到 10 项（confirm_options_max）后，下述细粒度 Agent
        #   会进选项 —— 不补中文名，卡片上就是裸英文 key（architecture_skeleton：…）。
        "requirement_structuring": "需求结构化", "architecture_skeleton": "架构骨架生成",
        "view_expansion": "视图展开生成", "model_validation_repair": "模型校验修复",
        "trace_verification": "追溯验证", "change_safety_gate": "变更安全门",
        "model_release": "模型发布入库", "zhiyuan_mgmt": "智源工程管理",
    }
    # 「像在求助」的语气词：完全无信号时用它区分"用户在提请求"与"纯寒暄"——后者弹卡是打扰。
    _REQUEST_HINTS = ("帮我", "请", "我想", "我要", "能不能", "能否", "如何", "怎么", "怎样",
                      "看一下", "看下", "查一下", "查下", "给我", "做个", "写个", "有没有")

    def _should_confirm_intent(self, user_input, meta) -> bool:
        """判据：**只有"系统自己没把握"才问**。有把握的一律不打断（否则每次对话都被拦一次）。

        问：`llm_weak`（LLM 给了具体意图但 <0.85）/ `fused_conflict`（关键词与语义打架）/
            `llm_conflict`（语义与 LLM 互斥，P0-1）/ `semantic_weak`（弱语义命中）/
            `llm` 且 conf<0.85 / 完全无信号但**像在求助**
        不问：`inherit`（追问/续写，问反而打扰）/ 规则命中 / `fused`（两路互证）/ 高置信语义 /
            纯寒暄 / 续答消息（含 `CLARIFY_RESUME_MARK`，防"问→答→又问"的循环）
        开关：`intent.confirm_when_unsure`（默认 True）——一键回到"不打断"的旧行为。
        """
        try:
            from core import config as _cfg
            if not bool(_cfg.get("intent", "confirm_when_unsure", True)):
                return False
        except Exception:
            pass
        if self.CLARIFY_RESUME_MARK in (user_input or ""):
            return False
        route = (meta or {}).get("route") or ""
        conf = float((meta or {}).get("confidence") or 0)
        # P0-1：`llm_conflict`（语义与 LLM 互斥）同样要问 —— 不硬选。
        if route in ("llm_weak", "fused_conflict", "semantic_weak", "llm_conflict"):
            return True
        if route == "llm" and conf < 0.85:
            return True
        if route == "chat" and conf <= 0:      # 完全无信号：只有"像在求助"时才问
            t = (user_input or "").strip()
            return len(t) >= 6 and any(k in t for k in self._REQUEST_HINTS)
        return False

    def _intent_confirm_questions(self, user_input, current_intent, sem_alt=None) -> list:
        """构造"意图确认"题：**选项 = 候选意图（中文名 + 一句职责说明）**，当前猜测置顶并如实标注。

        为什么选项文本要带"说明"：选项被选中后会原样进入续答文本（【澄清补充】…回答「…」），
        而续答要重新过一次意图识别 —— 说明里含该意图的**特异词**（如"方案设计/建模"），
        关键词层才能稳定命中；只写"设计"这种泛词反而会再次落空（泛词不单独构成信号）。

        P0-5（2026-09-30）`sem_alt`：本次**语义层的意图级 top1**（{intent, score}）。
        ★ 2026-10-17（米爸反馈"为啥每次澄清都是这个固定的问题清单…可以十多个"）：
        候选主序升级为 `router._last_sem_ranking` —— **本次输入的语义意图级全量排名**
        （detect_semantic 按意图名聚合后旁路存下，见 intent.py 同处注释）。此前消费方
        只有 top1 一个（插在"当前猜测"之后），其余候选按 `_db_intents` 字典序填满
        **前 4 项** —— 于是「导出最近审计日志」这类句子的卡片永远只显示字典前 4 个，
        真正的 system_mgmt 连选项都进不去，用户只能靠"其他/自定义"手打。
        现在：当前猜测置顶 → sem_alt 第二 → 语义排名其余（按本次输入相关度）→
        字典序兜底填满；数量取 `intent.confirm_options_max`（默认 10，设置页可配）。
        语义层没跑/没结论（排名为空）时整体退回旧行为（纯字典序），保证确定性可回归。

        ⚠️ **只影响候选呈现的顺序与数量，不改变"谁被选中"** —— 语义意见本身不构成
        路由决定（与 P0-1 的"不硬选"一致），所以这里可以安全地消费它。
        """
        names = [k for k in (getattr(self.router, "_db_intents", {}) or {}).keys() if k != "chat"]
        _alt_name = sem_alt.get("intent", "") if isinstance(sem_alt, dict) else ""
        _ranking = [(_r.get("intent", "") if isinstance(_r, dict) else str(_r[0]))
                    for _r in (getattr(self.router, "_last_sem_ranking", None) or [])]
        try:
            from core import config as _cfg
            _max_opts = int(_cfg.get("intent", "confirm_options_max", 10) or 10)
        except Exception:
            _max_opts = 10
        _max_opts = max(4, min(_max_opts, len(names) or 1))
        _ordered, _seen = [], set()

        def _push(_n):
            if _n and _n in names and _n not in _seen:
                _seen.add(_n)
                _ordered.append(_n)

        _push(current_intent)          # 当前猜测置顶（标注"我的猜测"）
        _push(_alt_name)               # 语义 top1 第二
        for _n in _ranking:
            _push(_n)                  # 语义排名其余（本次输入的相关序，动态）
        for _n in names:
            _push(_n)                  # 字典序兜底填满（排名为空时即旧行为）
        opts = []
        for n in _ordered[:_max_opts]:
            try:
                desc = (self.registry.get(n).description or "").strip()[:22]
            except Exception:
                desc = ""
            label = self._INTENT_LABEL.get(n, n)
            txt = f"{label}：{desc}" if desc else label
            if n == current_intent:
                txt += "（我的猜测）"
            opts.append(txt)
        return [{
            "id": "q_intent",
            "question": "我不确定你想让我做哪件事 —— 请选一项，或直接在下方补充你的说法（选完我再执行）",
            "options": opts,
            "allow_custom": True,
        }]

    # ── 2026-10-17 澄清承接轮 + 确认卡 LLM 动态化（米爸反馈，实证会话 9446）─────────────
    def _is_clarify_followup(self, conversation_id, user_input) -> bool:
        """本轮是否为「上一轮澄清/未完成任务的承接补充」。

        实证案例（会话 9446，米爸截图）：AI 导出失败后明确邀请「下一轮先补全
        分支→版本标识」「您若方便，也可以直接告我该工程下分支的名称」；用户答
        「取主分支最新版本数据」（9 字）——这是对邀请的直接回应，却被阶段 1.4
        「我不确定你想做哪件事」+ 固定意图清单拦住，上下文生生断裂。

        判据（全满足才是承接）：
          1) 本会话存在带 answered 记录的 clarify 卡（走过 澄清→作答，任务未闭合）
          2) 该卡之后已有 assistant 产出（续跑过），且其后**只有本轮这一条** user 消息
             （即用户紧跟作答后回应，中间没开新话题）
          3) 本轮输入 ≤ `intent.clarify_followup_max_chars`（默认 25）——短句像补充、
             不像新任务（新任务通常带明确动作/对象，字数也更多）
        承接 ⇒ 调用方（stream.py）两道澄清门（1.4 意图确认 / 1.5 内容澄清）全跳过，
        直接执行；Agent 持有完整上下文，能自行理解「用户在回答我上一轮的问题」。
        ⚠️ 任何查询异常返回 False——宁可误弹卡（旧行为），不可误放行。
        """
        try:
            from core import config as _cfg
            _max_chars = int(_cfg.get("intent", "clarify_followup_max_chars", 25) or 25)
        except Exception:
            _max_chars = 25
        _text = str(user_input or "").strip()
        if not _text or len(_text) > _max_chars:
            return False
        if not conversation_id or int(conversation_id) <= 0:
            return False
        try:
            from database import db_conn
            with db_conn() as _cc:
                _card = _cc.execute(
                    "SELECT id FROM messages WHERE conversation_id=? AND msg_type='clarify' "
                    "AND card_data LIKE '%\"answered\"%' ORDER BY id DESC LIMIT 1",
                    (int(conversation_id),)).fetchone()
                if not _card:
                    return False
                _rows = _cc.execute(
                    "SELECT role FROM messages WHERE conversation_id=? AND id>? ORDER BY id",
                    (int(conversation_id), _card["id"])).fetchall()
        except Exception:
            return False
        _roles = [r["role"] for r in _rows]
        if not _roles or "assistant" not in _roles:
            return False
        _user_idx = [i for i, r in enumerate(_roles) if r == "user"]
        # 本轮 user 消息（入口已落库）必须是卡之后唯一一条 user 且排在最后
        if len(_user_idx) != 1 or _user_idx[0] != len(_roles) - 1:
            return False
        return True

    def _llm_intent_confirm_questions(self, user_input, current_intent, conversation_id=None):
        """LLM 基于上下文生成「意图确认」题（米爸反馈：「澄清确认卡还是会有系统默认的
        内容，应该是没有基于上下文分析提供准确的确认信息」）。

        与固定列表版 `_intent_confirm_questions` 的关系：
        - 本函数是**主路径**：把「用户输入 + 最近几轮上下文 + 可选意图清单」交给 LLM，
          生成针对本次场景的确认问题与选项。LLM 看到上下文后有两种结论：
          a) `{"need": false, "intent": "..."}` —— 意图其实已明确（误触发 1.4，
             如承接补充轮）⇒ 返回 `("skip", intent)`，调用方**不弹卡直接执行**；
          b) `{"need": true, "questions": [...]}` —— 确需用户拍板 ⇒ 生成针对性
             问题与选项（每个选项须映射回清单里的意图 key，见下方 _map_option）。
        - LLM 失败/Mock/超时 ⇒ 返回 `None`，调用方兜底回固定列表版（确定性可回归）。
        - 选项为什么必须映射回意图 key：选项被选中后原样进入续答文本
          （【澄清补充】…），续答要重新过意图识别——选项含该意图的**特异词**
          （如「方案设计/建模」）关键词层才能稳定命中；纯自由文本会再次落空。

        开关：`intent.confirm_llm_dynamic`（默认 True）。
        """
        try:
            from core import config as _cfg
            if not bool(_cfg.get("intent", "confirm_llm_dynamic", True)):
                return None
        except Exception:
            pass
        # ── 可选意图清单（给 LLM 的候选池：key | 中文名 | 一句职责）──
        _lines = []
        for _k, _v in (getattr(self.router, "_db_intents", {}) or {}).items():
            if _k == "chat":
                continue
            try:
                _desc = (self.registry.get(_k).description or "").strip()[:40]
            except Exception:
                _desc = ""
            _label = self._INTENT_LABEL.get(_k, _k)
            _lines.append(f"{_k} | {_label} | {_desc}" if _desc else f"{_k} | {_label}")
        if not _lines:
            return None
        # ── 最近上下文（用户原话 + AI 回复尾部，截断防爆）──
        _ctx_lines = []
        if conversation_id and int(conversation_id) > 0:
            try:
                from database import db_conn
                with db_conn() as _cc:
                    _rows = _cc.execute(
                        "SELECT role, content FROM messages WHERE conversation_id=? "
                        "AND role IN ('user','assistant') AND msg_type='text' "
                        "ORDER BY id DESC LIMIT 4", (int(conversation_id),)).fetchall()
                for _r in reversed(_rows):
                    _who = "用户" if _r["role"] == "user" else "AI"
                    _ctx_lines.append(f"{_who}: {str(_r['content'] or '')[-300:]}")
            except Exception:
                _ctx_lines = []
        _ctx = "\n".join(_ctx_lines)
        try:
            from llm import llm_client
            resp = llm_client.chat([
                {"role": "system", "content": (
                    "你是 MBSE 智能助手的意图澄清模块。系统对用户最新这句话的路由置信度很低。"
                    "请结合对话上下文判断用户意图：\n"
                    "1) 若上下文已能明确用户要用哪项能力（含用户正在回答上一轮 AI 的提问/补充"
                    "上一轮未完成任务的信息），输出 {\"need\":false,\"intent\":\"<意图key>\"}；\n"
                    "2) 若确实需要用户拍板，输出 {\"need\":true,\"questions\":[{\"id\":\"q1\","
                    "\"question\":\"<基于上下文的针对性提问>\",\"options\":[\"<中文名：说明>\",...],"
                    "\"allow_custom\":true}]}——只出 1 题，2-5 个选项，**不同选项应指向不同意图**；\n"
                    "选项的中文名部分必须出自下方可选意图清单（这样用户选完系统才知道该跑什么）；"
                    "提问要贴合上下文，点明用户这句话与哪个未决事项相关，"
                    "禁止输出清单式罗列（如「请选一项」这种没有信息量的话）。\n"
                    "只输出 JSON，不要其他文字。\n\n可选意图清单（key | 中文名 | 职责）：\n"
                    + "\n".join(_lines))},
                {"role": "user", "content": (
                    (f"最近对话上下文：\n{_ctx}\n\n" if _ctx else "")
                    + f"用户最新输入：{str(user_input)[:300]}")},
            ], _intent="clarify_detect")
            content = (resp.get("choices") or [{}])[0].get("message", {}).get("content") or ""
            j = self._parse_json_block(content)
        except Exception:
            return None
        if not isinstance(j, dict):
            return None
        if j.get("need") is False:
            _skip_to = str(j.get("intent") or "").strip()
            # 只在 LLM 给出清单内的意图 key 时才敢放行（否则按需确认处理）
            if _skip_to in (getattr(self.router, "_db_intents", {}) or {}):
                return ("skip", _skip_to)
            return None
        _qs = j.get("questions")
        if not (isinstance(_qs, list) and _qs):
            return None
        # ── 中文名 → key 反查表（选项必须能映射回系统意图，保证续答可路由）──
        _label2key = {}
        for _k in (getattr(self.router, "_db_intents", {}) or {}).keys():
            if _k != "chat":
                _label2key[self._INTENT_LABEL.get(_k, _k)] = _k
        _out_qs = []
        for _q in _qs[:1]:
            if not isinstance(_q, dict):
                continue
            _opts, _mapped = [], []
            for _o in (_q.get("options") or [])[:6]:
                _t = str(_o or "").strip()
                if not _t:
                    continue
                _key = ""
                for _lb, _k in sorted(_label2key.items(), key=lambda x: -len(x[0])):
                    if _t.startswith(_lb):
                        _key = _k
                        break
                if _key:
                    _opts.append(_t[:60])
                    _mapped.append(_key)
            if len(_opts) < 2:
                continue     # 有效选项不足 ⇒ 整组作废，调用方兜底固定列表
            _out_qs.append({
                "id": "q_intent",
                "question": str(_q.get("question") or "")[:150],
                "options": _opts,
                "allow_custom": True,
                # 供续答路由：选项→意图 key 的映射（前端透传，回填进【澄清补充】）
                "option_intents": _mapped,
            })
        if not _out_qs:
            return None
        return ("ask", _out_qs)

    # ── 内容级澄清：信息不清晰 → 选择题确认（优先选择题，支持补充输入），回答后续答 ──
    # 混合触发：LLM 声明式 quick 判定为主 + 规则兜底（输入过短/未提及领域实体）
    CLARIFY_INTENTS = ("design", "requirement_analysis", "impact", "requirement_quality")
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
          2) action_kw:<词>：输入含明确建模/动作动词（复用 router._db_intents
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
            for _vs in (getattr(self.router, "_db_intents", {}) or {}).values():
                kws.extend(_vs or [])
        except Exception:
            kws = []
        kws.extend(_CLARIFY_ACTION_VERBS)
        for k in kws:
            if k and str(k).lower() in low:
                return "action_kw:" + str(k)
        return ""

    def _persist_hil_confirm(self, conversation_id, intent, confirm_req,
                             content="", card_data=None) -> int:
        """★ 2026-10-28（写前确认 HITL）：落一条"等待人工确认"的消息（时间线痕迹）。

        ## 为什么状态源仍然是 `hil_confirmations` 而不是 `conversations.pending_clarify`
        - 澄清只有**一个**待答状态 ⇒ 放 `conversations.pending_clarify` 够用；
        - 写前确认是**多张单**（不同工具、不同批次可同时在等）⇒ 必须有独立的表
          ⇒ `hil_confirmations` 才是状态源（`status` / `payload` / `decided_by` / `result_json`）。
        ⇒ 本方法只负责"会话时间线上有一条可见的等待卡片"，**不承担状态**。
          （刷新页面后前端可从 `GET /api/studio/hil-confirmations` 重建卡片。）

        返回消息 id（供 done 事件携带，前端据此定位消息容器）。失败返回 0。

        ★ 2026-10-28（conv 9733 实测）：**不能再只存一句占位语**。
          那次真机：模型已经跑完校验、生成了完整时序图代码，最后一步写入被判"待确认"，
          而这里把整轮内容压成 16 个字的「⏸ 写操作需要人工确认后才能执行」，
          `exec`（工具回执/思考/进度）直接为空 ⇒ 刷新后**这一轮做过什么全丢了**，
          用户只看到一句"待确认"，无从判断该不该批准。
          ⇒ 新增 `content` / `card_data` 两个可选入参：
            · `content` 为空时才回落到占位语（向后兼容既有调用）；
            · `card_data` 带上本轮 `exec`（工具/思考/进度）+ `confirm`，供历史还原。
        """
        _msg_id = 0
        try:
            from database import db_conn
            _card = {"confirm": confirm_req or {}}
            if isinstance(card_data, dict):
                _card.update(card_data)
            _content = str(content or "").strip() or "⏸ 写操作需要人工确认后才能执行"
            with db_conn() as _cc:
                cur = _cc.execute(
                    "INSERT INTO messages (conversation_id, role, content, msg_type, card_data) "
                    "VALUES (?,?,?,?,?)",
                    (conversation_id, "assistant", _content, "hil_confirm",
                     json.dumps(_card, ensure_ascii=False)))
                _msg_id = cur.lastrowid or 0
        except Exception:
            pass
        return _msg_id

    def _persist_clarify(self, conversation_id, user_input, intent, branch, attachments,
                         forced_intent, skill_name, team, questions,
                         content="", card_data=None) -> int:
        """落库澄清挂起状态（conversations.pending_clarify）+ 澄清卡消息（msg_type='clarify'）。
        返回消息 id（供 done 事件携带，前端据此定位重建后的消息容器）。失败返回 0。

        ★ 2026-10-28（conv 9735 实测）：与 `_persist_hil_confirm` **同一个坑** ——
          此前只写死「需要您确认建模信息后继续」（12 个字），
          而那一轮已经跑完定位、生成了时序图代码、`sysml_v2_validate` 返回 **0 错误**。
          结果：用户看到的是一条"请确认"，**没有代码、没有校验回执**，
          却要在 4 个写入选项里挑一个 —— 等于盲确认。
          用户原话是「写入前需要我确认」，确认的前提是**能看到要写什么**。
          ⇒ 新增 `content` / `card_data`：调用方把本轮正文与 `exec`（工具回执/
          思考/闸门）传进来，历史里就能还原"做了什么、要确认什么"。
        """
        _msg_id = 0
        try:
            _pending = {
                "questions": questions,
                "context": {"input": str(user_input)[:2000], "intent": intent, "branch": branch,
                            "attachments": attachments or [], "forced_intent": forced_intent or "",
                            "skill_name": skill_name or "", "team": team or ""},
            }
            from database import db_conn
            _card = {"questions": questions}
            if isinstance(card_data, dict):
                _card.update(card_data)
            _content = str(content or "").strip() or "需要您确认建模信息后继续"
            with db_conn() as _cc:
                _cc.execute(
                    "UPDATE conversations SET pending_clarify=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (json.dumps(_pending, ensure_ascii=False), conversation_id))
                cur = _cc.execute(
                    "INSERT INTO messages (conversation_id, role, content, msg_type, card_data) "
                    "VALUES (?,?,?,?,?)",
                    (conversation_id, "assistant", _content,
                     "clarify", json.dumps(_card, ensure_ascii=False)))
                _msg_id = cur.lastrowid or 0
        except Exception:
            pass
        return _msg_id
