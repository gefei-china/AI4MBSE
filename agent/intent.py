"""IntentRouter：三层混合意图识别（规则快筛→语义→LLM 精排）+ 置信度三级决策 + 会话 DST + 意图缓存。"""
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


class IntentRouter:
    """BR-2: Intent recognition via LLM or rule-based fallback.

    P0-3 扩展为 6 类意图（会话主入口路由）：
    requirement_analysis / design / impact / review / report_generation / knowledge_qa
    """

    INTENTS = {
        "requirement_analysis": ["需求", "解析", "条目", "需求分析", "requirement"],
        "requirement_quality": ["需求质量", "质量评审", "质量分析", "模糊词", "不可验证"],
        "design": ["方案", "设计", "架构", "方案设计", "design", "建模", "sysmlv2代码", "代码生成", "模型生成"],
        "impact": ["变更影响", "影响分析", "影响范围", "波及", "改动", "impact", "change", "变更影响"],
        "review": ["校验", "评审", "检查", "预评审", "review", "validate"],
        "report_generation": ["报告", "文档", "汇报", "导出", "生成报告", "report"],
        "system_mgmt": ["用户", "角色", "权限", "审计", "监控", "账号", "会话统计", "系统数据", "运行统计", "有几个用户", "哪些用户", "谁的权限", "谁的角色"],
        "knowledge_qa": ["知识库", "资料", "文档里", "查一下", "knowledge", "检索"],
        "chat": [],
    }

    def __init__(self):
        self._db_intents: dict = {}  # P0 平台化：DB 自定义 Agent 关键词（优先匹配）
        self._semantic_index: list = []  # 缺口-1：Agent 语义索引 [{name, text}]，供语义兜底路由
        # P2 逻辑规则配置化：intent_rules 规则缓存 [(trigger, intent, weight)]（None=未加载，首次 detect 自动加载）
        self._rules = None
        # P0-1 置信度三级决策：最近一次 detect 的路由元数据（route/confidence/是否需澄清）
        self._last_meta = {"intent": "", "route": "", "confidence": 0.0, "needs_clarification": False}
        self._last_sem_score = 0.0  # 最近一次语义匹配 top_score（供弱置信澄清判定）

    def register_keywords(self, intent: str, keywords: list) -> None:
        """注册 DB Agent 的意图关键词（新建 Agent 即可路由，无需改代码）。"""
        if keywords:
            self._db_intents[intent] = [k.lower() for k in keywords if k]

    def load_rules(self, conn=None) -> list:
        """P2 逻辑规则配置化：从 intent_rules 表加载启用规则 → self._rules = [(trigger, intent, weight)]。

        - 只取 enabled=1 行，按 weight 降序（detect 命中首个 = 权重最高）
        - conn 传入则复用调用方连接（路由层同一请求事务）；缺省自建连接用完即关
        - 表不存在/查询失败（老库未迁移）→ 静默降级为空规则表，不影响既有关键词逻辑
        """
        try:
            if conn is not None:
                rows = conn.execute(
                    "SELECT trigger, intent, weight FROM intent_rules WHERE enabled=1 ORDER BY weight DESC").fetchall()
            else:
                from database import get_db
                c = get_db()
                try:
                    rows = c.execute(
                        "SELECT trigger, intent, weight FROM intent_rules WHERE enabled=1 ORDER BY weight DESC").fetchall()
                finally:
                    c.close()
            self._rules = [(str(r["trigger"]), str(r["intent"]), float(r["weight"] or 1.0)) for r in rows]
        except Exception:
            self._rules = []
        return self._rules

    def set_semantic_index(self, index: list) -> None:
        """P0 语义兜底：注入 Agent 语义索引 [{name, text}]（text=display_name+description+keywords）。"""
        self._semantic_index = index or []

    def get_last_meta(self) -> dict:
        """返回最近一次 detect 的路由元数据 {intent, route, confidence, needs_clarification}。"""
        return dict(self._last_meta)

    def _index_fingerprint(self) -> str:
        """路由索引指纹：INTENTS 关键词 + DB Agent 关键词 + 语义索引名——任一变化缓存自动失效。"""
        try:
            import hashlib
            parts = []
            for k in sorted(self.INTENTS):
                parts.append(k + ":" + ",".join(sorted(self.INTENTS[k])))
            for k in sorted(self._db_intents):
                parts.append(k + ":" + ",".join(sorted(self._db_intents[k])))
            if self._semantic_index:
                parts.append("idx:" + "|".join(str(i.get("name", "")) for i in self._semantic_index))
            if self._rules:
                parts.append("rules:" + "|".join(f"{t}->{i}:{w}" for t, i, w in self._rules))
            return hashlib.md5("&".join(parts).encode("utf-8", "ignore")).hexdigest()
        except Exception:
            return ""

    def _cache_get(self, text: str, fp: str):
        """意图级缓存查询（P0-3）：指纹匹配 + query_hash 命中 → (intent, route, confidence)；未命中 None。"""
        if not text or not fp:
            return None
        try:
            import hashlib
            from database import get_db
            conn = get_db()
            try:
                h = hashlib.md5(text.encode("utf-8", "ignore")).hexdigest()
                row = conn.execute(
                    "SELECT intent, route, confidence FROM intent_cache WHERE query_hash=? AND index_fp=?",
                    (h, fp)).fetchone()
                if not row:
                    return None
                conn.execute("UPDATE intent_cache SET hit_count=hit_count+1, updated_at=CURRENT_TIMESTAMP WHERE query_hash=?",
                             (h,))
                conn.commit()
                return (row["intent"], row["route"], row["confidence"])
            finally:
                conn.close()
        except Exception:
            return None

    def _cache_set(self, text: str, fp: str, intent: str, route: str, confidence: float) -> None:
        """意图级缓存写入（仅缓存高置信结果，弱置信/继承结果不缓存以免放大路由错误）。"""
        if not text or not fp or confidence < 0.7:
            return
        try:
            import hashlib
            from database import get_db
            conn = get_db()
            try:
                h = hashlib.md5(text.encode("utf-8", "ignore")).hexdigest()
                conn.execute(
                    "INSERT INTO intent_cache (query, query_hash, intent, route, confidence, index_fp) "
                    "VALUES (?,?,?,?,?,?) ON CONFLICT(query_hash) DO UPDATE SET "
                    "intent=excluded.intent, route=excluded.route, confidence=excluded.confidence, "
                    "index_fp=excluded.index_fp, updated_at=CURRENT_TIMESTAMP",
                    (text, h, intent, route, confidence, fp))
                conn.commit()
            finally:
                conn.close()
        except Exception:
            pass

    def detect(self, text, conn=None, prev_intent=None):
        """BR-2 增强：Glossary 术语归一化优先（P0-1），置信度分级（P1-1），
        意图级缓存（P0-3），会话级意图继承（P0-2）。

        流程：意图缓存命中（L1 直返）→ 建模强信号 → Glossary 强制 → 规则关键词 → 语义（置信度分级）
        → LLM 兜底（含 confidence）→ 会话继承（无强信号时沿用上轮意图）→ chat。
        每次调用填充 self._last_meta = {intent, route, confidence, needs_clarification}。
        """
        t = text.lower()
        fp = self._index_fingerprint()
        # P0-3：意图级缓存（L1 命中直接返回，省重复规则/语义/LLM 全链路）
        cached = self._cache_get(t, fp) if fp else None
        if cached:
            intent, route, conf = cached
            # V2.6 防线：requirement_quality 缓存命中也须过强信号词校验
            # （历史误判曾随缓存毒化——泛词"质量怎么样"不应触发质量评审）
            if intent == "requirement_quality" and not any(
                    k in t for k in ("需求质量", "质量评审", "需求质量评审", "质量分析", "模糊词", "不可验证", "验收标准")):
                intent, route, conf = "chat", "chat", 0.0
            # 2026-09-16 防线（impact 版）：缓存命中但原文含 CIA 强信号词 → 覆盖为 impact
            # （「…变更影响分析…」曾被报告词/知识问答抢路由后随缓存毒化重复误判；
            #   含「报告」的不抢——SP-R 报告路由仍优先，由 ReportGenerator 识别影响报告类型）
            if intent != "impact" and "报告" not in t and any(
                    k in t for k in ("变更影响", "影响分析", "影响范围", "波及")):
                intent, route, conf = "impact", "cache_guard", 0.95
            # route 标记 cache（命中层），原始路由存 source_route 供可观测
            self._last_meta = {"intent": intent, "route": "cache", "confidence": conf,
                               "needs_clarification": False, "source_route": route}
            return intent
        self._last_glossary = None  # 每次调用重置，防跨调用残留
        # P0-2 建模强信号优先：生成/输出 + SysML/建模/视图/代码 → design（先于 Glossary 强制路由，
        # 避免「SysML v2 建模」被术语归一化强制到 knowledge_qa）
        if re.search(r"(生成|给出|输出|编写|创建).{0,24}(sysml|kerml|建模|模型|bdd|ibd|视图|代码)", t) or \
           re.search(r"(sysml|kerml|建模|bdd|ibd|视图|块定义|内部块).{0,24}(生成|代码|建模)", t):
            return self._done("design", "rule", 0.95, text, fp)
        # P0-1：Glossary 术语归一化——命中即强制路由（如「v2代码规范」→ knowledge_qa + sysml_norm 域）
        if conn is not None:
            try:
                from glossary import GlossaryMatcher
                res = GlossaryMatcher(conn).resolve(text)
                # 无论是否强制 intent，命中术语都记录（供调用方检索时 domain 过滤）
                if res.get("hits"):
                    self._last_glossary = res
                # 2026-09-16：CIA 强信号不被术语强制路由压制——词典术语（如「巡飞弹」）带
                # intent=knowledge_qa 会把「对巡飞弹做变更影响分析」整体抢到知识问答，
                # 用户明确表达的影响分析意图丢失。术语归一化仍记录（检索 domain 过滤用），仅意图让路。
                if res["force_intent"] and not any(
                        k in t for k in ("变更影响", "影响分析", "影响范围", "波及")):
                    return self._done(res["force_intent"], "rule", 0.95, text, fp)
            except Exception:
                pass
        # SP-R：报告生成命令优先路由——「生成/撰写/输出…报告」一律走 report_generation
        # （内部再由 ReportGenerator 按关键词识别 分析/变更影响/预评审 类型模板）
        if ("报告" in t or "report" in t) and any(v in t for v in ("生成", "撰写", "输出", "编写", "起草", "写份", "写一")):
            return self._done("report_generation", "rule", 0.95, text, fp)
        # CIA：变更影响分析强信号优先——「变更影响/影响分析/影响范围」明确触发 impact
        # （放报告路由之后：含报告生成词的走 report_generation，其余含影响信号的一律 impact，
        #   避免「影响」这类泛词被 requirement_analysis/chat 误路由）
        if any(k in t for k in ("变更影响", "影响分析", "影响范围", "波及", "impact_analyze")):
            return self._done("impact", "rule", 0.95, text, fp)
        # P0 需求质量：V2.6 收紧为明确要求——仅高特异词触发（单"质量/验收标准"等泛词不再命中）
        if any(k in t for k in ("需求质量", "质量评审", "需求质量评审", "质量分析", "模糊词", "不可验证")):
            return self._done("requirement_quality", "rule", 0.95, text, fp)
        # P2 逻辑规则配置化：intent_rules 规则层优先（trigger 为输入子串（忽略大小写），按 weight 降序取首个命中）
        if self._rules is None:
            self.load_rules(conn=conn)
        for trigger, intent, weight in self._rules:
            if trigger and trigger.lower() in t:
                return self._done(intent, "rule", 0.95, text, fp)
        # DB 自定义 Agent 关键词优先（P0 平台化：先匹配可配置 Agent）
        for intent, keywords in self._db_intents.items():
            if any(k in t for k in keywords):
                return self._done(intent, "rule", 0.95, text, fp)
        # 内置关键词兜底
        for intent, keywords in self.INTENTS.items():
            if intent == "chat":
                continue
            if any(k in t for k in keywords):
                return self._done(intent, "rule", 0.95, text, fp)
        # 缺口-1：关键词未命中 → 语义匹配兜底（VectorEngine bigram 余弦，零外部依赖）
        sem = self.detect_semantic(text)
        if sem:
            route = "semantic" if self._last_sem_score >= 0.70 else "semantic_weak"
            # P0-2：弱语义（<0.70）≠ 强信号——有会话意图时优先会话继承（追问/续写如"再详细一点"不误判）
            if route == "semantic_weak" and prev_intent and prev_intent in self.INTENTS:
                return self._done(prev_intent, "inherit", 0.55, text, fp)
            return self._done(sem, route, self._last_sem_score, text, fp)
        # M2：规则/语义均未命中 → LLM 意图识别兜底（配置真实 LLM 时生效，Mock/无 key 自动跳过）
        llm_intent, llm_conf = self.detect_llm(text)
        if llm_intent:
            # V2.6：requirement_quality 仅词法明确触发——LLM 猜测一律降级为 chat
            # （泛词如"质量怎么样"曾被 LLM 归到需求质量意图，误触发质量评审卡）
            if llm_intent == "requirement_quality" and not any(
                    k in t for k in ("需求质量", "质量评审", "需求质量评审", "质量分析", "模糊词", "不可验证", "验收标准")):
                llm_intent, llm_conf = "chat", min(llm_conf, 0.5)
            # P0-2：弱 LLM 猜测（<0.85）≠ 强信号——有会话意图时优先会话继承（追问/续写不被 LLM 弱猜测截胡）
            if llm_conf < 0.85 and prev_intent and prev_intent in self.INTENTS:
                return self._done(prev_intent, "inherit", 0.55, text, fp)
            return self._done(llm_intent, "llm", llm_conf, text, fp)
        # P0-2：会话级意图保持——无任何信号命中时继承上轮意图（追问/续写不被误判 chat）
        if prev_intent and prev_intent in self.INTENTS:
            return self._done(prev_intent, "inherit", 0.55, text, fp)
        return self._done("chat", "chat", 0.0, text, fp)

    def _done(self, intent, route, confidence, text, fp):
        """统一出口：填充 _last_meta + 高置信写意图缓存。澄清判定：中置信（弱语义/继承/LLM<0.85）→ 提示。"""
        needs_clarify = route in ("semantic_weak", "inherit") or (route == "llm" and confidence < 0.85)
        self._last_meta = {"intent": intent, "route": route, "confidence": confidence,
                           "needs_clarification": needs_clarify}
        self._cache_set(text, fp, intent, route, confidence)
        return intent

    def detect_llm(self, text: str) -> tuple:
        """M2：LLM 意图识别兜底——规则未命中时交给 LLM 结构化判断。

        - 返回 (intent, confidence)；仅返回已知意图（内置 DEFINITIONS 或 DB 已注册 Agent），
          未知/解析失败返回 ("", 0.0)（兜底 chat）。
        - Mock / 无 key 环境：LLM 返回普通文本无法解析 JSON → 自动返回 ("", 0.0)，保持确定性可回归。
        - P0-1 置信度决策：要求 LLM 输出 confidence（0-1），<0.85 视为低置信 → 上层触发澄清提示。
        """
        try:
            from llm import llm_client
            known = set(self._db_intents.keys()) | set(self.INTENTS.keys()) - {"chat"}
            known_list = "、".join(sorted(known))
            resp = llm_client.chat(
                [{"role": "system", "content": (
                    f"你是 MBSE 助手意图分类器。根据用户输入判断其意图，只输出一个 JSON 对象，"
                    f'格式：{{"intent": "意图名", "confidence": 0到1之间的小数}}，不要任何其他文字。'
                    f"可选意图：{known_list}。无法判断或置信度低于 0.5 时 intent 用 chat、confidence 给低值。")},
                 {"role": "user", "content": str(text)[:500]}],
                _intent="intent_detect",
            )
            msg = (resp.get("choices") or [{}])[0].get("message", {})
            content = msg.get("content") or ""
            m = re.search(r'\{\s*"intent"\s*:\s*"([\w_]+)"\s*,\s*"confidence"\s*:\s*([0-9.]+)', content)
            if not m:
                m = re.search(r'\{\s*"intent"\s*:\s*"([\w_]+)"', content)  # 兼容旧格式（无 confidence）
                if not m:
                    return ("", 0.0)
                intent, conf = m.group(1), 0.9
            else:
                intent, conf = m.group(1), float(m.group(2))
            if intent in known:
                return (intent, conf)
            return ("", 0.0)
        except Exception:
            return ("", 0.0)

    def detect_semantic(self, text: str, threshold: float = 0.15) -> str:
        """语义兜底路由：对 Agent 语义索引做相似度（真 embedding 优先，bigram 降级），返回最匹配 Agent。

        P1-1 置信度分级（对齐 Sabitov 生产路由 playbook）：
        - top_score >= 0.70：高置信，直接采纳
        - 0.40 <= top_score < 0.70：弱置信——命中 glossary 时仍采纳（术语归一化兜底），否则降级 chat
        - top_score < 0.40：低置信，降级 chat（不硬检索）
        保持既有双条件：第一名 >= threshold 且显著领先第二名（>= 1.5 倍）。
        top_score 写入 self._last_sem_score（供上层 route 分级与澄清判定）。

        2026-09-19 双阈值：以上数值是 **bigram 口径**。走真 embedding（dense）时改用
        `embedding.intent_*_dense`（分位等价映射标定，默认 0.49/0.69/0.69/0.76），
        因为两路余弦量纲不同、同一阈值必有一路失准。
        """
        self._last_sem_score = 0.0
        if not self._semantic_index:
            return ""
        from semantic import SemanticSearch
        from core import config as _cfg
        _ss = SemanticSearch()
        scored = _ss.rank(text, self._semantic_index, top_k=2, threshold=0, key="text")
        if not scored:
            return ""
        top_score, top_item = scored[0]
        self._last_sem_score = top_score
        name = top_item.get("name", "")
        if name == "chat":
            return ""
        # 2026-09-19：分档阈值按**本次实际走的路**选 —— 两路余弦量纲不同
        # （实测同一批 top1：dense 0.51 / bigram 0.10），一套阈值套两路必有一路失准。
        # dense 值由 calibrate_dense_thresholds.py 做**分位等价映射**标定
        # （保持原判定通过率 → 行为等价迁移，不是"更松/更紧"）。
        # ⚠️ 旧 0.55 / 0.70 两档在 bigram 下**从未生效**（bigram top1 max=0.4910）——
        #    dense 生效后它们首次可用，属行为变更点，故取 dense 高分位保守值（见 core/config.py）。
        if getattr(_ss, "last_backend", "bigram") == "dense":
            th = float(_cfg.get("embedding", "intent_threshold_dense", 0.49) or 0.49)
            sem_low = float(_cfg.get("embedding", "intent_sem_low_dense", 0.69) or 0.69)
            sem_mid = float(_cfg.get("embedding", "intent_sem_mid_dense", 0.69) or 0.69)
            sem_high = float(_cfg.get("embedding", "intent_sem_high_dense", 0.76) or 0.76)
        else:
            th, sem_low, sem_mid, sem_high = threshold, 0.40, 0.55, 0.70
        if top_score < th:
            return ""
        # P1-1 置信度分级：弱置信（0.40-0.70）——
        # 采纳条件（任一）：
        #   a) glossary 术语归一化命中（强信号）；
        #   b) 第一名中等置信（>=0.55）且显著领先第二名（>=1.15x）——描述匹配意图明确
        #      （P2-B 修复：「测算成本」→ 成本分析Agent 0.58 vs design 0.47，领先 1.22x
        #      被弱置信拦截导致 Gap1 失败；1.15x 倍率经实测三输入验证无误伤）。
        if top_score < sem_high and top_score >= sem_low:
            try:
                if getattr(self, "_last_glossary", None) and self._last_glossary.get("hits"):
                    return name  # 术语归一化命中 → 信任路由
            except Exception:
                pass
            if len(scored) >= 2 and top_score >= sem_mid and top_score >= scored[1][0] * 1.15:
                return name  # 中等置信 + 显著领先 → 描述匹配明确，采纳（P2-B）
            return ""
        if top_score < sem_low:
            return ""  # 低置信：不硬检索，交给上层 LLM/chat 兜底
        # 显著领先判定（仅当存在第二名时才要求）
        if len(scored) >= 2 and top_score < scored[1][0] * 1.5:
            return ""
        return name

    @staticmethod
    def extract_kb_tags(text):
        """提取 #知识库 引用标签（#xxx，允许文件名常见字符 .-_），供会话主入口展示知识库消费范围。

        符号约定（V3）：@ = 选择智能体（前端弹窗 + forced_intent，不进文本）；# = 引用知识库文件。
        """
        import re
        return re.findall(r"#([一-龥A-Za-z0-9_.\-]+)", text)

    # ── Task 11 多意图分解 + suggested_slots（纯规则实现，不调 LLM，不影响 detect 主流程）──
    # 子句 → 意图名映射表（有序：高特异优先，避免"需求分析"被 design 的"方案"等先命中）
    _CLAUSE_INTENTS = (
        ("requirement_analysis", ("需求分析", "需求条目", "需求", "requirement")),
        ("impact", ("影响分析", "变更影响", "影响", "impact")),
        ("review", ("评审", "校验", "预评审", "review", "validate", "检查")),
        ("report_generation", ("报告", "汇报", "文档", "导出", "report")),
        ("design", ("方案设计", "方案", "架构", "设计", "建模", "模型", "sysml", "代码生成", "design")),
        ("knowledge_qa", ("知识库", "检索", "查询", "资料", "knowledge")),
    )
    # 阶段拆解正则：模式 A（先…再/然后/接着…最后…）｜模式 B（X并Y，最后/然后/接着Z——无"先"的并列+收尾）
    # $ 锚定结尾：约束非贪婪子句吃满剩余文本，避免"方案设计"被拆成单字"方/出"
    _STAGE_RE_A = re.compile(
        r"先(做|进行|完成|出|写)?(.{1,20}?)(，|,|、|;|；)?"
        r"(再|然后|接着)(做|进行|完成|出|写)?(.{1,20}?)"
        r"(，|,|、|;|；)?(最后(做|进行|完成|出|写)?(.{1,20}?))?$")
    _STAGE_RE_B = re.compile(
        r"(.{1,20}?)(并|和|且|以及|与)(.{1,20}?)"
        r"(，|,|、|;|；)?(最后|然后|接着)(做|进行|完成|出|写)?(.{1,20}?)$")

    def _clause_to_intent(self, clause: str) -> str:
        """子句 → 意图名；无法映射返回原文本（供上层展示/拼接）。"""
        c = clause.strip()
        low = c.lower()
        for intent, keywords in self._CLAUSE_INTENTS:
            if any(k in low for k in keywords):
                return intent
        return c

    def _split_stage_clauses(self, text: str) -> list:
        """多阶段指令 → 原始子句列表（未映射）；无阶段连词返回空列表（单意图由 detect 处理）。"""
        if not text or not isinstance(text, str):
            return []
        t = text.strip()
        # 模式 A：先做X，(再|然后|接着)Y，(最后Z)?
        # 分组：g1动作词 g2第一子句 g3可选逗号 g4连词 g5动作词 g6第二子句 g7可选逗号 g8=g9动作词+g10最后子句
        m = self._STAGE_RE_A.search(t)
        if m:
            clauses = [m.group(2)]
            if m.group(6):
                clauses.append(m.group(6))
            if m.group(10):
                clauses.append(m.group(10))
            return clauses
        # 模式 B：X(并|和|且|以及|与)Y，(最后|然后|接着)Z（如"需求分析并建模，最后出报告"）
        m = self._STAGE_RE_B.search(t)
        if m:
            clauses = [m.group(1), m.group(3)]
            if m.group(7):
                clauses.append(m.group(7))
            return clauses
        return []

    def split_multi_intent(self, text: str) -> list:
        """Task 11-① 多意图分解：一条输入含多个阶段任务 → 子意图名列表（供动态编排衔接）。

        按阶段连词（先…再…/先…然后…/接着/最后 做…）把多阶段指令拆为子指令：
        - 命中拆解 → 每段子句映射意图名（需求分析→requirement_analysis、方案/设计/架构/建模→design、
          影响→impact、评审→review、报告→report_generation）；无法映射的子句返回原文本
        - 无阶段连词 → 返回空列表（单意图场景由既有 detect 处理）
        纯规则实现（正则），不调 LLM；不改 detect 主流程。
        """
        clauses = self._split_stage_clauses(text)
        if not clauses:
            return []
        return [self._clause_to_intent(c) for c in clauses]

    def suggest_slots(self, intent: str, text: str) -> list:
        """Task 11-② suggested_slots：按意图返回缺失槽位补全提示 [{key, label, hint}]。

        简单关键词启发判定（不含特定线索即建议，不调 LLM）：
        - impact：无变更对象/元素名线索 → 建议 change_source（变更源）
        - design：无约束条件线索 → 建议 constraints（约束条件）
        - requirement_analysis：无来源/输入线索 → 建议 source（需求来源）
        - 其余意图返回 []
        """
        if not intent or intent not in self.INTENTS:
            return []
        t = (text or "").strip()
        if intent == "impact":
            # 有变更动作/对象线索（调整/修改/改为/调到…）→ 视为已指明变更对象，不再建议
            if re.search(r"(调整|修改|改为|调到|调至|设为|改到|变更到|从.{1,12}到|降低|提高|增加|减少|→|->)", t):
                return []
            return [{"key": "change_source", "label": "变更源",
                     "hint": "指定要分析的变更元素/参数，如「功耗预算调到18W」"}]
        if intent == "design":
            if re.search(r"(约束|性能|成本|风险|指标|要求|限制|功耗|重量|体积|预算|质量|可靠性|接口|环境|兼容|时序|容量|带宽)", t):
                return []
            return [{"key": "constraints", "label": "约束条件",
                     "hint": "补充性能/成本/风险等约束"}]
        if intent == "requirement_analysis":
            if re.search(r"(任务书|资料|文档|知识库|上传|文件|依据|说明书|规范|输入|来源|附件|手册)", t):
                return []
            return [{"key": "source", "label": "需求来源",
                     "hint": "补充需求来源（任务书/上传资料/知识库）"}]
        return []

    def detect_multi(self, text) -> dict:
        """Task 11-③ 组合入口：多阶段输入 → {"intent":"multi","sequence":[子意图名],"raw_subtasks":[原始子句]}。

        split_multi_intent 非空时返回 multi 结构（供 pipeline 编排路径显式调用，前端展示阶段序列）；
        否则返回 None（上层走既有 detect 单意图路径）。纯规则，不改 detect() 主流程。
        """
        clauses = self._split_stage_clauses(text)
        if not clauses:
            return None
        return {"intent": "multi",
                "sequence": [self._clause_to_intent(c) for c in clauses],
                "raw_subtasks": clauses}

