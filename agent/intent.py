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

    # P0-3（2026-09-24）：规则表变更的**类级版本号**。
    # 背景：`AgentPipeline.__init__` 里是 `self.router = IntentRouter()`（每实例新建），
    # 而规则失效若写成「置某个模块级 router 的 _rules = None」，作用的是另一个实例 ——
    # 对请求路径完全无效（实测：加规则后指纹仍是旧的）。改用类变量后与实例数无关。
    _RULES_EPOCH = 0

    INTENTS = {
        "requirement_analysis": ["需求", "解析", "条目", "需求分析", "requirement"],
        "requirement_quality": ["需求质量", "质量评审", "质量分析", "模糊词", "不可验证"],
        "design": ["方案", "设计", "架构", "方案设计", "design", "建模", "sysmlv2代码", "代码生成", "模型生成"],
        "impact": ["变更影响", "影响分析", "影响范围", "波及", "改动", "impact", "change", "变更影响"],
        "review": ["校验", "评审", "检查", "预评审", "review", "validate"],
        "report_generation": ["报告", "文档", "汇报", "导出", "生成报告", "report"],
        "system_mgmt": ["用户", "角色", "权限", "审计", "监控", "账号", "会话统计", "系统数据", "运行统计", "有几个用户", "哪些用户", "谁的权限", "谁的角色"],
        "knowledge_qa": ["知识库", "资料", "文档里", "查一下", "knowledge", "检索",
                         # 2026-09-25：补说明类信号（此前完全无覆盖 → 一律被 design 的泛词"建模"劫持）
                         "介绍", "说明", "什么是", "是什么", "区别", "原理", "概述", "方法论"],
        "chat": [],
    }

    # ── 2026-09-25 说明类意图判定（修「MBSE建模方法论介绍」被误判为 design）──
    #  实测根因：① design 内置词含泛词「建模」，而匹配是"首个命中即 return 0.95"
    #            （`any(k in t for k in keywords)`，无特异性加权、无竞争比较）；
    #            ② knowledge_qa 完全没有"介绍/说明/是什么/区别/包含哪些"这类信号；
    #            ③ INTENTS 字典顺序上 design 远早于 knowledge_qa → 泛词先劫持。
    #  修法（最小且不改词表语义）：在**最前置**（缓存之前）加一段"说明类优先"——
    #    同时满足「含说明信号」且「不含动作信号」→ 判 knowledge_qa。
    #  为什么必须在缓存之前：这条输入早已被误判写入意图缓存，放在规则后会被旧缓存挡住，
    #    新逻辑永不生效（本仓"缓存毒化"是有前例的坑，见下方 requirement_quality / impact 两道防线）。
    #  已知边界（保守取向）：形如"介绍一下代码生成"这类**说明+动作词混用**句仍走 design——
    #    宁可让少见的混用句走原路径，也不放宽条件去抢正例（正例误伤=白跑一次建模，代价更大）。
    _EXPLAIN_SIGNALS = (
        "介绍", "说明", "是什么", "什么是", "啥是", "区别", "差异", "原理", "概述",
        "包含哪些", "有哪些", "如何理解", "怎么理解", "怎么用", "如何使用", "用途",
        "作用", "方法论", "规范", "流程", "最佳实践", "入门", "科普", "讲讲",
        "解释", "含义", "定义", "概念", "怎么做", "如何做",
    )
    _ACTION_SIGNALS = (
        "生成", "给出", "输出", "编写", "创建", "设计", "绘制", "画", "做一份", "帮我做",
        "请对", "导出", "落库", "导入", "校验", "检查", "评审", "分析一下", "出图",
        "建模方案", "重构", "补全", "提取", "抽取", "改造",
        # 2026-09-25（评测集查出）：这两个是"质量问题"的**专指词**，出现即"要做质量分析"而非问概念。
        # 不加它们时「这份需求的模糊词有哪些」会被 explain（"有哪些"）抢到 knowledge_qa，
        # 而正确意图是 requirement_quality（评测集错例 #1，见 eval_intent_routing.py）。
        "模糊词", "不可验证",
    )

    def _is_explain_ask(self, t: str) -> bool:
        """说明/介绍类提问判定：含说明信号且**不含**动作信号（对齐"先答疑、别乱动手"）。"""
        return (any(k in t for k in self._EXPLAIN_SIGNALS)
                and not any(k in t for k in self._ACTION_SIGNALS))

    # ── 2026-09-25 关键词层竞争打分（替代"首个命中即 return 0.95"）──
    #  对标依据（调研）：vLLM Semantic Router 的 **Signal–Decision** 架构把 Keyword 定位为
    #  "只产信号、不单独定路由"，最终由 Decision 聚合；NLPCraft 明确"the most specific intent
    #  match wins"（特异性优先）；Kore.ai 要求显式定义多命中的竞争与打分规则（first-match 或
    #  evaluate-all-and-score）。本仓原实现是"首个命中即 return"且无特异性概念 —— 泛词
    #  「建模/需求/报告」因此可劫持整条路由（2026-09-25 用户报障的直接根因）。
    #  规则：score(intent) = Σ 命中词权重；权重按**特异性**定（长词高、泛词极低）；
    #        **只命中泛词的意图得分=0（视为无信号）** —— 泛词不得单独决定路由（下沉到语义/LLM 层）。
    _GENERIC_KW = frozenset({
        "建模", "模型", "需求", "报告", "设计", "方案", "架构", "文档", "校验", "评审",
        "检查", "分析", "生成", "知识库", "资料", "数据", "系统", "代码", "视图", "change", "design",
        # 2026-09-25（calibrate_intent_semantic.py 查出）：「总体」是 DB 里 **team_leader** 的关键词，
        # 而它在本域是典型的**泛词**（总体设计/总体方案/总体架构都用它）——
        # 于是「对这个系统做总体设计」被 team_leader 以 0.5 分抢走（design 只命中泛词"设计"=0 分），
        # 语义层随后给出正确答案 design(0.8673) 也只能变成 fused_conflict、路由仍是 team_leader。
        # 归入泛词后 → 该句在关键词层"无信号"→ 下沉语义层 → 正确判为 design（实测 28/29 → 29/29）。
        "总体",
    })

    # P1-13：db 自定义 Agent 强特异关键词优先于 builtin 强信号的**最低分门槛**。
    # ≥1.0 = 至少一个 ≥4 字非泛词命中（"需求视图"这类）；单泛词（如"视图"）不构成信号不误抢。
    # 抽成类常量供负对照/评测 monkeypatch（改成 0.0 即"去掉守卫"，弱信号也会抢）。
    _DB_KW_PRIORITY_MIN_SCORE = 1.0

    def _kw_score(self, text_low: str, keywords) -> tuple:
        """单个意图的关键词得分 → (score, 命中词)。

        权重设计（对标"泛词降权**或要求共现**"，见 _GENERIC_KW 注释）：
          · 特异词：按长度计权（≥4 字满分 1.0），如"方案设计"/"变更影响"/"追溯矩阵"；
          · 泛词：每个 0.25（降权），且**单个泛词不构成信号**（score=0）——防止"建模"这类
            本体泛词单独劫持路由；
          · **泛词共现（≥2 个）算信号**（0.5/词）：如"请设计…架构方案"命中 方案/设计/架构 三词，
            若坚持只认特异词，这类常见正例会被判无信号 → 白跑一次 LLM（实测：route=llm，多付一次调用）。
        """
        hits = [k for k in keywords if k and k in text_low]
        if not hits:
            return 0.0, []
        spec = [k for k in hits if k not in self._GENERIC_KW]
        gene = [k for k in hits if k in self._GENERIC_KW]
        # 2026-09-25：泛词参与与否做成开关（intent.keyword_generic，默认 True）——
        # 目的是能用**评测集直接回答**"泛词词条是否可移除"（`eval_intent_routing.py --generic 0`），
        # 而不是靠感觉删词表。关掉后泛词既不加分也不触发共现规则。
        if not gene:
            return sum(min(1.0, len(k) / 4.0) for k in spec), hits
        if not (self._cfg_get("keyword_generic", True)):
            return (sum(min(1.0, len(k) / 4.0) for k in spec) if spec else 0.0), hits
        if not spec:
            return (0.5 * len(gene) if len(gene) >= 2 else 0.0), hits
        score = sum(min(1.0, len(k) / 4.0) for k in spec) + 0.25 * len(gene)
        return score, hits

    @staticmethod
    def _cfg_get(key: str, default):
        """读 intent 组配置；异常/缺失回落 default（配置层不可用时不影响路由）。"""
        try:
            from core import config as _c
            return _c.get("intent", key, default)
        except Exception:
            return default

    def _scored_keyword_pick(self, t: str, layers: list) -> tuple:
        """多意图竞争打分：返回 (intent, score, hits, layer_name) 或 (None, 0, [], '')。

        layers 顺序即优先级（DB 自定义 Agent 关键词 → 内置词表），但**同层内按分数竞争**，
        并且**跨层只在"同分"时才让前层胜出**（保留"可配置 Agent 优先"的既有语义）。
        """
        best = (None, 0.0, [], "")
        for layer_name, mapping in layers:
            for intent, kws in mapping.items():
                if intent == "chat":
                    continue
                sc, hits = self._kw_score(t, kws)
                if sc <= 0:
                    continue
                # 严格大于才替换 → 同分时保持先出现的层/意图（确定性，不吃字典顺序随机性）
                if sc > best[1]:
                    best = (intent, sc, hits, layer_name)
        return best

    def _db_kw_priority(self, t: str):
        """P1-13：DB 自定义 Agent 的**强特异关键词**优先命中 → 意图名，否则 None。

        只认 ≥ `_DB_KW_PRIORITY_MIN_SCORE`（默认 1.0 = 至少一个 ≥4 字非泛词）——
        单泛词（如"视图"）不构成信号，不误抢。纯函数（依赖 _db_intents 与 _scored_keyword_pick），
        供 detect 前置调用 + 负对照直接测（不经意图缓存）。
        """
        _pick = self._scored_keyword_pick(t, [("db", self._db_intents)])
        if _pick[0] and _pick[1] >= self._DB_KW_PRIORITY_MIN_SCORE:
            return _pick[0]
        return None

    @staticmethod
    def _is_report_command(t: str) -> bool:
        """SP-R 报告生成命令（「生成/撰写/输出…报告」）判定 —— 单一真源，db 守卫与 SP-R 路由共用。"""
        return ("报告" in t or "report" in t) and any(
            v in t for v in ("生成", "撰写", "输出", "编写", "起草", "写份", "写一"))

    def __init__(self):
        self._db_intents: dict = {}  # P0 平台化：DB 自定义 Agent 关键词（优先匹配）
        self._semantic_index: list = []  # 缺口-1：Agent 语义索引 [{name, text}]，供语义兜底路由
        # P2 逻辑规则配置化：intent_rules 规则缓存 [(trigger, intent, weight)]（None=未加载，首次 detect 自动加载）
        self._rules = None
        self._rules_epoch = -1  # P0-3：-1 ≠ 任何 epoch → 首次 detect 必加载
        # P0-1 置信度三级决策：最近一次 detect 的路由元数据（route/confidence/是否需澄清）
        self._last_meta = {"intent": "", "route": "", "confidence": 0.0,
                           "needs_clarification": False, "sem_alt": None, "used_history": False,
                           "confirmed": False}
        self._last_sem_score = 0.0  # 最近一次语义匹配 top_score（供弱置信澄清判定）
        # P0-1（2026-09-30）：语义层的"弃权意见"——**意图级** top1（达阈但未过采纳门槛）。
        # 供 LLM 兜底段做互证（融合矩阵缺的那一格），详见 detect_semantic / detect 内注释。
        self._last_sem_alt = None

    def register_keywords(self, intent: str, keywords: list) -> None:
        """注册 DB Agent 的意图关键词（新建 Agent 即可路由，无需改代码）。"""
        if keywords:
            self._db_intents[intent] = [k.lower() for k in keywords if k]

    @classmethod
    def invalidate_rules(cls) -> None:
        """规则表变更后让**所有** IntentRouter 实例下次 detect 重载（类级版本号，与实例数无关）。

        替换原先「置某个模块级 router 实例的 _rules = None」的写法：那个实例与
        `AgentPipeline.self.router` 不是同一个对象，失效形同虚设（2026-09-24 实测）。
        """
        cls._RULES_EPOCH += 1

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
        self._rules_epoch = IntentRouter._RULES_EPOCH
        return self._rules

    # ── 2026-09-25 语义层「接住弱信号句」的底座：给每个**粗粒度意图**补示例 utterance ──
    #  实测根因（tests/manual_verify/calibrate_intent_semantic.py 采样）：
    #   语义索引里装的全是 **Agent 名 + 描述**（20 条），其中大半是「结构视图生成」
    #   「参数视图生成」这类**细粒度子 Agent**；于是语义层的 top1 经常是**子 Agent 名**，
    #   而不是路由器要的意图名 —— 例如「帮我生成这个系统的SysML v2模型代码」top1=
    #   「结构视图生成」0.7255、「输出一份 BDD 视图」top1=「交互视图（IBD）生成」0.6235，
    #   而这两句期望都是 design。**答词表不一致**比阈值更致命：阈值再松也只是把 design
    #   的句子路由到某个子 Agent 上（错得更隐蔽）。
    #  修法：为每个意图补 4 条**粗粒度**示例 utterance，**逐条**入索引（name=意图名）——
    #   逐条而非拼成一段，是为了让"句对句"相似度接近 1.0（拼一段会被平均掉）。
    #  ⚠️ 刻意**不含子 Agent 专名**（如"生成结构视图"）：那类细分说法应继续由子 Agent 命中，
    #   粗粒度条目只负责接住"总体设计/生成模型代码"这种泛化说法，避免把细分路由抢走。
    #  ⚠️ 与关键词层同一纪律：**不得靠单个泛词取胜** —— utterance 是**完整说法**（"帮我做总体设计"），
    #   故它只对措辞相近的句子加分，不会像"设计"这类泛词那样到处劫持。
    _SEMANTIC_UTTERANCES = {
        "requirement_analysis": ("帮我解析这份需求文档", "把需求条目提取出来", "从任务书里提取需求", "解析需求文档"),
        "design": ("帮我做总体设计", "设计系统架构方案", "生成模型代码", "出一份视图"),
        "impact": ("分析变更影响", "这次改动会波及哪些模块", "影响范围分析", "变更影响评估"),
        "review": ("校验模型代码", "评审一下这份文档", "检查追溯矩阵", "预评审这份设计"),
        "requirement_quality": ("需求质量分析", "检查需求有没有模糊词", "需求可验证性检查", "需求质量评审"),
        "report_generation": ("生成一份报告", "导出报告", "写一份评审报告", "输出分析报告"),
        "knowledge_qa": ("介绍一下这个方法", "什么是这个概念", "知识库里有没有相关资料", "原理是什么"),
        "system_mgmt": ("系统里有几个用户", "查看审计日志", "用户权限怎么管理", "账号角色管理"),
    }

    def set_semantic_index(self, index: list) -> None:
        """P0 语义兜底：注入 Agent 语义索引 [{name, text}]（text=display_name+description+keywords）。

        2026-09-25：额外追加 `_SEMANTIC_UTTERANCES` 的示例 utterance（逐条一个条目，name=意图名）。
        必要性见 `_SEMANTIC_UTTERANCES` 注释：索引里只有 Agent 名/描述时，语义层的**答词表**
        覆盖不到路由器要的粗粒度意图名，弱信号句"接不住"（不是阈值问题）。
        """
        self._semantic_index = list(index or [])
        for _intent, _utts in self._SEMANTIC_UTTERANCES.items():
            for _u in _utts:
                self._semantic_index.append({"name": _intent, "text": _u, "kind": "intent_utterance"})
        # 2026-09-26：**后台预热**候选集向量（详见 semantic.prewarm 注释）。
        #  索引已经定稿（含示例 utterance）才预热，且必须传**同序文本**（cache key = md5(join(texts))）。
        #  放在这里是因为它是索引变更的**唯一入口**（每个请求都会走），带指纹守卫不会重复起线程。
        try:
            from semantic import SemanticSearch as _SS
            _SS().prewarm(self._semantic_index)
        except Exception:
            pass

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
                # 2026-09-25：指纹由「只放 name」改为「name+text 的摘要」——
                # 语义索引**文本**变了（补示例 utterance / Agent 描述被编辑），缓存必须失效，
                # 否则又是"旧缓存挡住新逻辑"（本仓已两次踩到：explain 前置、规则 epoch）。
                parts.append("idx:" + hashlib.md5(
                    "|".join(f"{i.get('name', '')}~{i.get('text', '')}" for i in self._semantic_index
                             ).encode("utf-8", "ignore")).hexdigest())
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
                h = hashlib.md5(text.lower().encode("utf-8", "ignore")).hexdigest()
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
        """意图级缓存写入（仅缓存高置信结果，弱置信/继承结果不缓存以免放大路由错误）。

        P0-3 修复（2026-09-30）—— key 口径必须与 `_cache_get` 一致：
        `detect()` 用 `text.lower()` 查缓存，而本函数此前对**原文**求 hash
        → 写 key = md5(原文) / 读 key = md5(lower)，**含任意 ASCII 大写的输入永不命中**
        （本域输入高频含 SysML/MBSE/BDD/IBD）。实测真库 `intent_cache`：6 行写入 /
        `hit_count` 总和 0，其中 2 行（`SysML v2模型代码`、`BDD 视图`）是结构性死行。
        修法：两侧**各自**在函数内部 lower —— 口径自足，不依赖调用方约定；
        `query` 列仍存**原文**，保留可观测性（它是展示字段，不参与匹配）。
        """
        if not text or not fp or confidence < 0.7:
            return
        try:
            import hashlib
            from database import get_db
            conn = get_db()
            try:
                h = hashlib.md5(text.lower().encode("utf-8", "ignore")).hexdigest()
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

    # ── P0-5（2026-09-30）历史联合召回：净化规则与依据 ──────────────────────────
    # 三条**实测**得出的边界（不是设计推断，探针见 tmp/p_intent/probe_history*.py）：
    #   1. **绝不能进规则层**：把历史拼进 detect 的 `text` 后，6/6 条追问句被规则层的 SP-R
    #      报告路由以 **0.95 高置信**抓走（历史里含"输出影响报告"）→ 这正是"只喂低置信段"
    #      这条限制的**真正理由**：不是省成本，是历史里任何一个强特异词都能劫持整条路由。
    #   2. **也不必进语义层**：拼接文本送 detect_semantic **5/5 返回空**（意图级顶分仅 0.44，
    #      远低于 dense 采纳阈 0.69）→ 语义一路保持原文即可，带历史收益为零、只增干扰。
    #      （原方案写的是"拼进语义与 LLM 两路"，此处按实测收窄为只喂 LLM。）
    #   3. **必须净化**：噪声上文（多话题混杂）能把 LLM 拉到 report_generation@0.85
    #      —— 那是会被**直接采纳**的分数 → 除净化外，采纳侧另设"LLM 自报置信门槛"把关（见 LLM 段；
    #      原方案写的"语义背书门"已在定稿时被实测推翻并替换，勿再按旧注释理解）。
    _HISTORY_BLOCK_MARKS = ("【澄清补充】", "[任务上下文快照]", "[澄清", "[编排")
    _HISTORY_ITEM_MAX = 200    # 单条上限（字符）：超长多为粘贴正文，作"上文"只会淹没当前句
    _HISTORY_TOTAL_MAX = 300   # 合计上限（字符）：提示词成本与噪声的联合上限
    _HISTORY_MAX_ITEMS = 3     # 条数上限（调用方一般只给 1~2 条，这里做防御）

    def _sanitize_history(self, history, current_text=None) -> str:
        """P0-5：把历史消息净化成可喂给 LLM 的"上文"（返回拼接串；空串 = 不可用）。

        净化规则（每条都对应一类真实脏样本）：
        - 剔空 / 非字符串；
        - 剔含 `_HISTORY_BLOCK_MARKS` 的：`【澄清补充】`是澄清卡续答文本（实测 117~246 字，
          句首写着"...建模信息（请据此继续）"，会让识别以为用户又提了一次建模）；
          `[任务上下文快照]`是编排子任务内部构造文本（同族污染本仓踩过一次，见 stream.py）；
        - 剔超长（> `_HISTORY_ITEM_MAX`）：多为粘贴的正文，作"上文"只会淹没当前句；
        - 剔与当前句重复的（当前句可能已落库）；
        - 去重（实测真实会话里同一句话连发 3 次）；
        - 只取**最近** `_HISTORY_MAX_ITEMS` 条（列表按时间正序给出 → 取尾部）。
        """
        if not history:
            return ""
        _cur = (current_text or "").strip()
        _out = []
        _seen = set()
        for item in history:
            _s = item.strip() if isinstance(item, str) else ""
            if not _s or len(_s) > self._HISTORY_ITEM_MAX:
                continue
            if any(mk in _s for mk in self._HISTORY_BLOCK_MARKS):
                continue
            if _cur and _s == _cur:
                continue
            if _s in _seen:
                continue
            _seen.add(_s)
            _out.append(_s)
        if not _out:
            return ""
        return " ".join(_out[-self._HISTORY_MAX_ITEMS:])[:self._HISTORY_TOTAL_MAX]

    def detect(self, text, conn=None, prev_intent=None, history=None):
        """BR-2 增强：Glossary 术语归一化优先（P0-1），置信度分级（P1-1），
        意图级缓存（P0-3），会话级意图继承（P0-2），历史联合召回（P0-5）。

        流程：意图缓存命中（L1 直返）→ 建模强信号 → Glossary 强制 → 规则关键词 → 语义（置信度分级）
        → LLM 兜底（含 confidence，**并与语义弃权意见互证**：见 LLM 段 P0-1 注释；
          走到这段时还会带上**历史用户话**：见 LLM 段 P0-5 注释）
        → 会话继承（无强信号时沿用上轮意图）→ chat。
        每次调用填充 self._last_meta = {intent, route, confidence, needs_clarification,
        sem_alt, used_history}。

        P0-5（2026-09-30）：`history` 为**最近 N 条历史用户话**（纯文本列表，时间正序）。
        ⚠️ **只喂 LLM 一路** —— 规则层与语义层一律用原文，理由与实测见 `_sanitize_history`。
        """
        t = text.lower()
        # P0-3（2026-09-24）：规则刷新必须在**指纹计算之前** —— 指纹含 _rules 内容，
        # 若拿过期的 _rules 算 fp，就会命中「规则变更前」写入的缓存条目，缓存把新规则挡在门外
        # （实测：加规则后 sysmlv2 仍返回 route='cache' 的旧 knowledge_qa）。
        if self._rules is None or self._rules_epoch != IntentRouter._RULES_EPOCH:
            self.load_rules(conn=conn)
        fp = self._index_fingerprint()
        # 2026-09-25 说明类优先（**必须在缓存之前**）：修「MBSE建模方法论介绍」→ design 这类误路由。
        # 位置理由：该输入早已被误判写入意图缓存，若放在规则层之后会被旧缓存挡住、新逻辑永不生效。
        # 强度：conf 0.8 且 route='explain' → _done 里不触发澄清（needs_clarify 仅针对 weak/inherit/llm<0.85）。
        if self._is_explain_ask(t):
            return self._done("knowledge_qa", "explain", 0.80, text, fp)
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
        # P0-5（2026-09-30）：语义旁路同样必须**每次调用重置**。它是"本次语义意见"的观测值，
        #   而**走规则层直接 return 的路径压根不会进 detect_semantic**（原来的重置只写在那个
        #   函数里）→ 不在此处重置，规则命中的调用就会读到**上一轮**的残留值。P0-1 引入
        #   `_last_sem_alt` 时漏了这一处；P0-5 要把它喂给互证门与澄清卡，残留值会被当成
        #   "本次意见"误用（可行性探针里已踩到：规则命中的那些行，alt 显示的全是上一轮的）。
        self._last_sem_score = 0.0
        self._last_sem_alt = None
        # P1-13（2026-10-02）：DB 自定义 Agent 的**强特异关键词**优先于 builtin 建模强信号。
        # 实测：「生成需求视图/结构视图/参数视图」全被下方 444 行泛词正则「生成+视图」劫持到
        # design —— 用户自建 Agent（需求视图生成等）即使配了关键词也永远轮不到（db 关键词匹配
        # 在强信号正则**之后**）。这里把「db 层强特异命中」提到最前：用户显式配的关键词赢过
        # 内置泛词规则，语义与「DB 自定义 Agent 优先」一致。判定抽成纯函数 `_db_kw_priority`
        # 便于负对照（阈值守卫不是空转）。
        # ⚠️ 唯一例外：SP-R 报告生成命令（「输出…报告」）更高优先 —— 否则「输出影响报告」
        # 会被 db 层 impact 的「变更影响」抢走（实测引入错例：report_generation 期望 → impact）。
        if not self._is_report_command(t):
            _db_pri = self._db_kw_priority(t)
            if _db_pri:
                return self._done(_db_pri, "rule_scored", 0.95, text, fp)
        # 2026-09-25（评测集查出，错例 #2）：**review 强信号前置到 P0-2 建模强信号之前**。
        # 根因：P0-2 的第二条正则 `(sysml|…).{0,24}(生成|代码|建模)` 没有"动作词"约束，
        # 于是「帮我校验一下这段sysml代码」里的 "sysml…代码" 就被判成 design（期望 review）。
        # "校验/预评审"是动作明确的高特异意图，且本条要求**无 design 生成类动作词**，
        # 故不抢"生成sysml代码并校验"这类真建模正例。
        # ⚠️ 必须同时排除"质量类"信号：否则「做一次需求质量评审」会被本条抢到 review
        # （我第一版就踩了这个坑——评测集当场抓出，期望 requirement_quality）。
        if any(k in t for k in ("校验", "预评审", "评审")) and not any(
                k in t for k in ("生成", "给出", "输出", "编写", "创建", "设计", "绘制",
                                 "需求质量", "质量评审", "质量分析", "模糊词", "不可验证")):
            return self._done("review", "rule", 0.95, text, fp)
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
        if self._is_report_command(t):
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
        # P0-3：epoch 不符即重载 —— 规则表改了**不用重启**（原判据只看 `is None`，
        # 而失效机制又作用不到本实例，导致规则改动必须重启才生效）。
        if self._rules is None or self._rules_epoch != IntentRouter._RULES_EPOCH:
            self.load_rules(conn=conn)
        for trigger, intent, weight in self._rules:
            if trigger and trigger.lower() in t:
                return self._done(intent, "rule", 0.95, text, fp)
        # 2026-09-25 关键词层：DB 自定义 Agent 关键词 + 内置词表，**竞争打分**（不再"首个命中即 return"）。
        # 开关：intent.keyword_scored（默认 True）——置 False 可回到旧行为，用于 A/B 与回滚
        # （评测脚本 tests/manual_verify/eval_intent_routing.py --scored 0 即用此开关出基线）。
        _scored = True
        try:
            from core import config as _c2
            _scored = bool(_c2.get("intent", "keyword_scored", True))
        except Exception:
            pass
        if _scored:
            pick, sc, hits, layer = self._scored_keyword_pick(
                t, [("db", self._db_intents), ("builtin", self.INTENTS)])
            if pick:
                # 强特异词（≥2.0，如"方案设计/变更影响/追溯矩阵"）→ 直接采信，**不调语义**（省一次 embedding）。
                if sc >= 2.0:
                    return self._done(pick, "rule_scored", 0.95, text, fp)
                # 2026-09-25 语义融合（对标 HybridRouter 的 dense+sparse 融合 / vLLM 多信号组合决策）：
                # 弱关键词信号（1.0~2.0）时再取一路语义信号：
                #   · 与关键词**一致** → 两路互证，置信升到 0.92（route='fused'）
                #   · **冲突**（语义给出别的意图）→ 按标杆"低置信不硬选"：仍返回关键词结果执行，
                #     但标记 needs_clarification（前端出"可改选"澄清条），不做沉默的硬路由
                #   · 语义无结论 → 维持单路结果（0.90）
                # 只在弱信号时才调语义 → 保持"成本递增"（强规则零额外成本）。
                sem_pick = self.detect_semantic(text)          # 副作用：设置 _last_sem_score
                sem_score = getattr(self, "_last_sem_score", 0.0) or 0.0
                if sem_pick and sem_pick == pick:
                    return self._done(pick, "fused", 0.92, text, fp)
                if sem_pick and sem_pick != pick:
                    return self._done(pick, "fused_conflict", 0.60, text, fp)
                return self._done(pick, "rule_scored", 0.90, text, fp)
            # 关键词层**无信号**（未命中，或只命中泛词）→ 下沉：语义 → LLM → 继承 → chat
            # （对标：低于阈值不硬路由，交给更强的下一层；泛词不得单独决定路由）
        else:
            # ── 旧行为（A/B 基线）：DB 关键词优先、首个命中即 0.95 ──
            for intent, keywords in self._db_intents.items():
                if any(k in t for k in keywords):
                    return self._done(intent, "rule", 0.95, text, fp)
            for intent, keywords in self.INTENTS.items():
                if intent == "chat":
                    continue
                if any(k in t for k in keywords):
                    return self._done(intent, "rule", 0.95, text, fp)
        # ── P0-5（2026-09-30，v2）：历史联合召回 —— 走到这里就是"低置信分支"（关键词层无信号）──
        #   **仍成立**的部分：追问句「那它的风险呢」「再详细一点」「展开说说」单发时 LLM 只给
        #   chat@0.20~0.35（等于判不出）；带上同话题上文后能判出 impact（实测 9/9，分 0.62~0.95）。
        #   且收益**主要来自 prompt 的约束表述**（"上文只用来补全省略与指代"），不是"多喂了文字"：
        #   不加约束时噪声上文能把它拉到 report_generation@0.85（会被直接采纳 → 该写法必须弃用）。
        #   ⚠️ **已被实测收缩**的部分（见下方"v2 重定稿"注释）：追问句**本来就能被 `inherit` 答对**
        #      （prev=impact 时意图 6/6 都是 impact）→ 带历史**不改路由方向**，只用于"确认"。
        #   `_hist_ctx` 只传给 detect_llm；**语义一路坚持用原文**（实测拼接对语义零收益，见上）。
        #   ⚠️ v2 收敛：**没有 prev_intent 就不带上下文** —— prev_intent 缺失时无从"确认"，
        #      而带着上下文让 LLM 自由判定正是被实测否决的那条路（噪声上文能推到 0.90 的非 chat
        #      结论）。这同时保证"无 prev_intent 时提示词与旧版**逐字节一致**"（零回归）。
        _hist_ctx = self._sanitize_history(history, text) if prev_intent else ""
        # 缺口-1：关键词未命中 → 语义匹配兜底（VectorEngine bigram 余弦，零外部依赖）
        sem = self.detect_semantic(text)
        if sem:
            route = "semantic" if self._last_sem_score >= 0.70 else "semantic_weak"
            # P0-2：弱语义（<0.70）≠ 强信号——有会话意图时优先会话继承（追问/续写如"再详细一点"不误判）
            if route == "semantic_weak" and prev_intent and prev_intent in self.INTENTS:
                return self._done(prev_intent, "inherit", 0.55, text, fp)
            return self._done(sem, route, self._last_sem_score, text, fp)
        # M2：规则/语义均未命中 → LLM 意图识别兜底（配置真实 LLM 时生效，Mock/无 key 自动跳过）
        llm_intent, llm_conf = self.detect_llm(text, context=_hist_ctx)
        if llm_intent:
            # V2.6：requirement_quality 仅词法明确触发——LLM 猜测一律降级为 chat
            # （泛词如"质量怎么样"曾被 LLM 归到需求质量意图，误触发质量评审卡）
            if llm_intent == "requirement_quality" and not any(
                    k in t for k in ("需求质量", "质量评审", "需求质量评审", "质量分析", "模糊词", "不可验证", "验收标准")):
                llm_intent, llm_conf = "chat", min(llm_conf, 0.5)
            # P0-2：弱 LLM 猜测（<0.85）≠ 强信号——有会话意图时优先会话继承（追问/续写不被 LLM 弱猜测截胡）
            if llm_conf < 0.85:
                # ── P0-5 v2（2026-09-30 **二次标定后重定稿**）：带历史 → **只做"确认继承"** ──
                #  判据 = ①有上文 ②非 chat ③**结论与 prev_intent 一致** ④conf ≥ 地板值 0.55。
                #
                #  ⚠️ v1 判据（只要求 conf ≥ 门槛）已被实测**否决**，三轮证据：
                #  (1) v1 的标定是**截断分布**：`probe_gate_calib` 只在 route=='llm_history' 时记分，
                #      而"采纳"本身就是"分 ≥ 门槛"的后果 → 它报出的"最低分 0.80"其实就是门槛值，
                #      **最小值==门槛就是截断的签名**。噪声侧同病：未采纳时显示值被
                #      `min(conf,0.5)` 钳位，真实的 0.55~0.90 全被压成 0.50、看不见。
                #  (2) 换成**直连 detect_llm 取未截断分**（probe_gate_calib2）后：
                #      同话题侧 9/9 判出 impact，分 [0.62 … 0.95]（下限 0.62）；
                #      噪声侧  9 次里 **7 次给出非 chat 结论**，分 [0.55 … 0.90]（上限 0.90）。
                #      → 分离带**倒挂**（0.90 > 0.62），**不存在安全阈值**：任何阈值都会误采
                #        5~7/9 的噪声结论。原注释"分离是靠 prompt 约束挣来的、不是阈值切出来的"
                #        这句判断随之作废。
                #  (3) 更要紧的是**收益本来就是 0**（probe_gate_v3，带 prev_intent 的实测）：
                #      同话题追问 + prev=impact 时意图 6/6 都是 impact —— 其中 2/6 走 inherit、
                #      4/6 走本门。"用上文改写方向"的能力在**正确场景下贡献为零**（inherit 已答对），
                #      却要在**错误场景下**（历史与当前问题无关）承担改错方向的风险 → 收益 0、风险 >0。
                #
                #  故 v2 = 只保留**不可能改变方向**的那部分：采纳的意图恒等于 prev_intent，
                #  即"若不采纳，`inherit` 分支给出的也是同一个意图" → 本门**不可能改写路由方向**，
                #  只能"确认/加固"既有继承。收益落在 ①置信度 0.55 → 实测 0.62~0.95
                #  ②可观测（meta.used_history）③两路一致时免去那条"可改选"细条
                #  （③由 `history_confirm_silent` 控制，默认开；置 False 即回到"零差异"）。
                #  ⚠️ **"话题转向"能力明确不做**：它正是 v1 的核心卖点、也是唯一可能改方向的部分；
                #    要复活它必须先找到**独立于 LLM 自报分的佐证**（如显式的相关性判别，或 P0-2 的
                #    域边界描述），不能靠调阈值。
                #  地板值 0.55 的取法：不低于 `inherit` 自身的置信度 —— 否则"确认"反而会把置信度
                #  从 0.55 拉低（实测同话题侧下限 0.62，余量 0.07）。它**不是**分离阈。
                if (_hist_ctx and llm_intent != "chat"
                        and llm_intent == prev_intent
                        and llm_conf >= float(self._cfg_get("history_llm_min", 0.55))):
                    return self._done(llm_intent, "llm_history", llm_conf, text, fp,
                                      cacheable=False, used_history=True, confirmed=True)
                if prev_intent and prev_intent in self.INTENTS:
                    return self._done(prev_intent, "inherit", 0.55, text, fp)
                # 2026-09-26（**扩集后评测抓出并修**）：无会话上下文可继承时，低置信猜测也**不得硬选**。
                #  实测两条生产真实说法：「帮我看看这个项目的预算」「帮我测算一下这个项目的成本」
                #  被 LLM 以 **0.60** 猜成 report_generation（大概由"预算/成本"联想到"报告"）→
                #  问预算的用户收到一张**报告澄清卡**，且真的会去跑报告 Agent。
                #  这与仓库既有哲学冲突（fused_conflict 走澄清、requirement_quality 的 LLM 猜测直接降级），
                #  故统一为「弱猜测 = 承认无法归类」：回落 chat（兜底桶），置信取 <0.7
                #  （`_cache_set` 只缓存 ≥0.7 → 弱结论不入意图缓存，不会被后续请求复用）。
                #  代价（明说）：确实属于某意图、但 LLM 只给到 0.6~0.8 的句子，会落到 chat 而非澄清；
                #  若日后出现这类真实损失，就把 route='llm_weak' 也接进澄清条（本处只管"不硬选"）。
                return self._done("chat", "llm_weak", min(llm_conf, 0.5), text, fp)
            # P0-1（2026-09-30）：**语义 ↔ LLM 互证** —— 补上融合矩阵缺的那一格（断链 1）。
            #   机制根因（实测）：走到本段的语义结果**必然已被丢弃** —— 上方
            #   `sem = self.detect_semantic(text); if sem: return ...` 已把"语义有结论"的分支
            #   全部拦走，故 LLM 段从来看不到语义层的意见；语义"弃权"（达阈但未过采纳门槛）
            #   更从未被传递 → 相反证据被静默丢弃、且不触发澄清。
            #   实测唯一错例「帮我看看这个系统的接口设计是否合理」：语义 design(≈0.55) >
            #   review(0.48)，LLM 判 review(0.85) → 采纳 review 且 needs_clarification=False。
            #   判据（保守）：语义**意图级** top1 与 LLM 结论互斥、且语义分数达最低识别阈
            #   （`llm_sem_conflict_min`，默认 0.49）→ 不硬选，转澄清（route='llm_conflict'）。
            #   ⚠️ **刻意不做"采信语义"的翻盘**：本例语义 0.55 推翻 LLM 0.85 属弱证据推翻强结论，
            #      且意图级 lead 仅在 1.145~1.158 间随 embedding 抖动、恰好跨过任何"贴边门槛"——
            #      靠调门槛让它变绿等于用测试集调参。是否允许翻盘待 P1-3 标定 α/δ 后再定。
            _alt = getattr(self, "_last_sem_alt", None)
            # ── P0-5 v2：高置信分支**不做**历史采纳（撤回 v1 在这里加的那道门）──
            #   两条理由（都来自实测/可证）：
            #   ① 改不了方向：llm_conf ≥ 0.85 时 `route='llm'` 本来就会采纳同一个意图，
            #      而 v2 的门又以"与 prev_intent 一致"为前提 → 在这里是**恒等变换**；
            #   ② 平白多一条细条：`llm_history` 会带 needs_clarify（除非 confirmed），
            #      而原本的 `llm`（conf≥0.85）是 needs_clarify=False —— 在这里加门是**纯倒退**。
            #   但**缓存防线必须留下**：本段的 llm_intent 是用上文算出来的，按原文写缓存会让
            #   下一轮"没有上文的同一句话"直接命中它（本仓"缓存毒化"有前例）→ 用了上文则不落缓存。
            #   ⚠️ 记一段走不通的路以免重蹈：v1 曾用"语义意图级 top1 必须与带历史结论一致"当背书，
            #      被实测推翻 —— 追问句原文的语义 top1 恒为 `knowledge_qa@0.37` 这类**噪声**
            #      （三句追问全中），必然与 LLM 的正确结论不一致 → 门在**最需要它的场景下必然失效**。
            #      根因：追问句本身没有语义信号，拿它的语义意见当背书 = 拿噪声当判据。
            _cache_ok = not _hist_ctx
            if (_alt and _alt["intent"] and _alt["intent"] != llm_intent
                    and _alt["score"] >= float(self._cfg_get("llm_sem_conflict_min", 0.49))):
                return self._done(llm_intent, "llm_conflict", min(llm_conf, 0.6), text, fp,
                                  cacheable=_cache_ok, used_history=bool(_hist_ctx))
            if _hist_ctx:
                # 有上文：结论**可能被上下文带偏**，且无法与"真·话题转向"区分
                #   （实测噪声上文能给出非 chat 结论、自报分高达 0.90/0.95，见 P0-5 v2 注释）。
                #   处理：**保留 LLM 的结论**（真转向不能丢），但把"这是不是猜的"暴露出来 ——
                #     与 prev_intent 一致 → `confirmed`，不打扰用户（与旧行为可见差异为零）；
                #     不一致 → 不 confirmed → 出"可改选"细条，用户可一眼否掉。
                #   ⚠️ 撤回 v1 在这里直接 `route='llm'` 的写法：conf≥0.85 的 `llm` 是
                #     `needs_clarify=False` 的**静默**路径 → 带偏时静默走错，比 v1 更糟。
                #   不落缓存：结论来自上文，按原文存会让下轮无上文的同一句话命中它。
                return self._done(llm_intent, "llm_history", llm_conf, text, fp,
                                  cacheable=False, used_history=True,
                                  confirmed=(llm_intent == prev_intent))
            return self._done(llm_intent, "llm", llm_conf, text, fp)
        # P0-2：会话级意图保持——无任何信号命中时继承上轮意图（追问/续写不被误判 chat）
        if prev_intent and prev_intent in self.INTENTS:
            return self._done(prev_intent, "inherit", 0.55, text, fp)
        return self._done("chat", "chat", 0.0, text, fp)

    def _done(self, intent, route, confidence, text, fp, cacheable=True, used_history=False,
              confirmed=False):
        """统一出口：填充 _last_meta + 高置信写意图缓存。

        澄清判定：中置信（弱语义/继承/fused_conflict/llm_conflict/**llm_history**）或
        LLM 给分<0.85 → 出"可改选"细条。

        P0-5（2026-09-30）新增两个参数：
        - `cacheable`：**带历史得出的结论不得按原文写缓存** —— 意图缓存 key 只用原文，
          若把"带上文才成立"的结论按原文存进去，下一轮**没有**上文的同一句话会直接命中它
          （本仓"缓存毒化"是有前例的坑）。
        - `used_history`：本次结论是否用到了历史上文（写进 meta，供前端/日志观测）。
        - `confirmed`（P0-5 v2）：本次是"上文 LLM 结论与 prev_intent 一致"的**确认**，不是猜测
          → 默认不再弹"可改选"细条（`intent.history_confirm_silent` = False 可关掉该行为）。
          它**不会**改变路由方向（意图恒等于 prev_intent = inherit 会给的那个）。
        另：`sem_alt`（本次语义意图级 top1）一并写进 meta —— 澄清卡用它把候选排得更准
        （P0-5 合批项：此前语义意见只在"互斥判定"用得上，候选排序完全没消费它）。
        """
        # P0-1：`llm_conflict`（语义与 LLM 互斥）同样转澄清 —— 不硬选、不静默。
        # P0-5：`llm_history`（用上文采纳的结论）同样出"可改选"细条 —— 结论依赖上文，
        #   用户应能一眼否掉；但它**不进** `_should_confirm_intent`（那是"拦下来问"的面，
        #   而这里已经执行了；两路互证 + LLM>=0.85 的结论比 inherit(0.55) 强得多，不该被打断）。
        # P0-5 v2：`confirmed`（两路一致的确认）不再需要"可改选"细条 —— 免去追问轮的无谓确认。
        _silent_ok = bool(confirmed) and bool(self._cfg_get("history_confirm_silent", True))
        needs_clarify = (not _silent_ok) and (
            route in ("semantic_weak", "inherit", "fused_conflict", "llm_conflict",
                      "llm_history") or (route == "llm" and confidence < 0.85))
        _alt = getattr(self, "_last_sem_alt", None)
        self._last_meta = {"intent": intent, "route": route, "confidence": confidence,
                           "needs_clarification": needs_clarify,
                           "sem_alt": ({"intent": _alt["intent"], "score": _alt["score"]}
                                       if _alt else None),
                           "used_history": bool(used_history),
                           "confirmed": bool(confirmed)}
        if cacheable:
            self._cache_set(text, fp, intent, route, confidence)
        return intent

    def detect_llm(self, text: str, context: str = "") -> tuple:
        """M2：LLM 意图识别兜底——规则未命中时交给 LLM 结构化判断。

        - 返回 (intent, confidence)；仅返回已知意图（内置 DEFINITIONS 或 DB 已注册 Agent），
          未知/解析失败返回 ("", 0.0)（兜底 chat）。
        - Mock / 无 key 环境：LLM 返回普通文本无法解析 JSON → 自动返回 ("", 0.0)，保持确定性可回归。
        - P0-1 置信度决策：要求 LLM 输出 confidence（0-1），<0.85 视为低置信 → 上层触发澄清提示。
        - P0-5（2026-09-30）：`context` = 最近几轮**用户原话**（已净化）。非空时才把上文写进提示词，
          并显式声明"**请判断当前问题的意图（上文只用来补全省略与指代）**"。这句话是收益的
          主要来源（实测 4 种表述对照见 tmp/p_intent/probe_prompt_variants.py）：不写约束时
          LLM 会照搬上文意图 —— 噪声上文直接给 report_generation@0.85（会被采纳）；
          写成过强的"勿把上文本身当作当前请求"又会整体变保守（同话题从 0.95 掉到 0.65~0.85）。
          `context=""` 时提示词与旧版**逐字节一致**（零回归）。
        """
        try:
            from llm import llm_client
            known = set(self._db_intents.keys()) | set(self.INTENTS.keys()) - {"chat"}
            known_list = "、".join(sorted(known))
            _user = str(text)[:500]
            if context:
                _user = ("用户当前问题：" + str(text)[:300] + chr(10)
                         + "用户在本会话中的上文（用于理解指代与省略成分）："
                         + str(context)[:300] + chr(10)
                         + "请判断**当前问题**的意图（上文只用来补全省略与指代）。")
            resp = llm_client.chat(
                [{"role": "system", "content": (
                    f"你是 MBSE 助手意图分类器。根据用户输入判断其意图，只输出一个 JSON 对象，"
                    f'格式：{{"intent": "意图名", "confidence": 0到1之间的小数}}，不要任何其他文字。'
                    f"可选意图：{known_list}。无法判断或置信度低于 0.5 时 intent 用 chat、confidence 给低值。")},
                 {"role": "user", "content": _user}],
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

        2026-09-25 弱信号标定（脚本 calibrate_intent_semantic.py）：dense 路调整为
        `th=low=mid=0.64 / high=0.76`，领先倍率 `lead_w=lead_s=1.05`（原 1.5/1.15 硬编码）。
        认知：dense 余弦**量纲压缩**，"低门槛 + 严比值"的组合在 dense 下等于**双重否决**
        （top1 既不达 sem_low、又过不了 1.5x）→ 弱信号句只能掉 LLM；标定后改成
        "高门槛 + 松比值"。更强的守卫来自索引侧：粗粒度意图示例 utterance（见
        `_SEMANTIC_UTTERANCES`）让 top1 与意图名对齐，噪声句（如「你好」）的 top1/top2
        差值极小自然过不了 lead_w。
        """
        self._last_sem_score = 0.0
        self._last_sem_alt = None
        if not self._semantic_index:
            return ""
        from semantic import SemanticSearch
        from core import config as _cfg
        _ss = SemanticSearch()
        # P0-1（2026-09-30）：top_k 2→8 —— **只为多取几条做意图级聚合**（见下）；
        #   本函数既有的 `scored[0]` / `scored[1]` 用法一个不改（rank 排序后切片，前 2 条恒等）。
        scored = _ss.rank(text, self._semantic_index, top_k=8, threshold=0, key="text")
        if not scored:
            return ""
        top_score, top_item = scored[0]
        self._last_sem_score = top_score
        name = top_item.get("name", "")
        if name == "chat":
            return ""
        # P0-1：**意图级聚合** —— 索引里同一意图有多条 utterance（`_SEMANTIC_UTTERANCES`），
        #   原始 top2 常是**同一意图的另一条**，令"领先第二名"判据失真（实测错例
        #   「帮我看看这个系统的接口设计是否合理」：design 0.5541 / design 0.4993，比值 0.90
        #   → 被判"未领先"）。故按意图名取 max 后再算意图级 top1/top2 与领先倍率，
        #   存入 `_last_sem_alt` 供 LLM 段互证。
        #   ⚠️ 只记"意图名"（`in self.INTENTS`）—— 细粒度子 Agent 名不参与互证（留 P0-2）。
        #   ⚠️ **不参与本函数任何 return 判定**（零行为变更），纯旁路观测。
        _agg = {}
        for _sc, _it in scored:
            _nm = _it.get("name", "")
            if _nm in self.INTENTS and _nm != "chat" and _sc > _agg.get(_nm, 0.0):
                _agg[_nm] = _sc
        if _agg:
            _ranked = sorted(_agg.items(), key=lambda x: -x[1])
            _a1 = _ranked[0]
            _a2 = _ranked[1] if len(_ranked) > 1 else ("", 0.0)
            self._last_sem_alt = {
                "intent": _a1[0], "score": _a1[1], "runner": _a2[0], "runner_score": _a2[1],
                "lead": (_a1[1] / _a2[1]) if _a2[1] > 0 else 99.0,
            }
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
            # 2026-09-25：领先倍率也按 dense 路单独取配置（原硬编码 1.15/1.5）——
            # dense 余弦量纲压缩，比值门槛在 dense 下远比 bigram 严苛，必须与阈值联合标定，
            # 否则"阈值调松了但仍被倍率挡回"。标定见 calibrate_intent_semantic.py。
            lead_w = float(_cfg.get("embedding", "intent_lead_weak", 1.15) or 1.15)
            lead_s = float(_cfg.get("embedding", "intent_lead_strong", 1.50) or 1.50)
        else:
            th, sem_low, sem_mid, sem_high = threshold, 0.40, 0.55, 0.70
            lead_w, lead_s = 1.15, 1.50   # bigram 路沿用历史值（未随 dense 标定改动）
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
            if len(scored) >= 2 and top_score >= sem_mid and top_score >= scored[1][0] * lead_w:
                return name  # 中等置信 + 显著领先 → 描述匹配明确，采纳（P2-B；倍率见 lead_w）
            return ""
        if top_score < sem_low:
            return ""  # 低置信：不硬检索，交给上层 LLM/chat 兜底
        # 显著领先判定（仅当存在第二名时才要求）
        if len(scored) >= 2 and top_score < scored[1][0] * lead_s:
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
        ("design", ("方案设计", "方案", "架构", "设计", "建模", "模型", "sysml", "代码生成",
                   # 2026-09-25 补（verify_multi_intent_split.py T1 实测）：「输出 BDD 视图」
                   # 此前在子句映射里**完全映射不上** → sequence 里塞的是中文原句而非意图名，
                   # planner 的阶段序约束因此半失效（['requirement_analysis','输出 BDD 视图',…]）。
                   "视图", "bdd", "ibd", "代码", "design")),
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

    # ── 2026-09-25 多意图"两级切分"（第 2 级：并列清单）────────────────────────────
    #  缺口（实测）：「提供一段需求，进行需求分析、方案设计、代码校验」这类**顿号并列清单**
    #  是用户最自然的写法，但 `_split_stage_clauses` 只认**阶段连词**（先…再…/最后）→
    #  `detect_multi` 返回 None → 只落单意图（3 个阶段的活只跑 1 个），多意图能力形同虚设。
    #  为什么做成"两级"而不是把并列塞进同一正则：阶段连词表达**顺序**（先/再/最后），
    #  并列分隔符表达**清单**（、以及同时），语义不同；混在一起既拆不准顺序，
    #  也会误伤"先A、B，再C"这类混合句。**L1 一行不改**（零行为变更），L2 只做兜底。
    _PARA_SEP = re.compile(r"[、,，;；]")
    _PARA_CONN = re.compile(r"(?:以及|同时|然后|接着|并)")
    # L2 专用的**严格**关键词集：与 L1 的 `_CLAUSE_INTENTS` 不同，这里**剔除泛词**
    # （需求/影响/设计/方案/模型/检查…）。理由与关键词层同一条纪律：泛词不得单独判定，
    # 否则「提供一段需求」这种**背景句**会被判成 requirement_analysis 阶段（实测踩到）。
    _CLAUSE_SPECIFIC = (
        # 2026-09-26（多意图评测集驱动）：① 补 requirement_quality（此前**缺失** → 「做需求质量评审」
        #   被"评审"抢成 review/requirement_analysis）；② report_generation **提到 review 之前**，
        #   对齐主判定的"报告优先"（含报告生成词 → report_generation），否则「再出一份评审报告」
        #   会因为"评审"先命中而判成 review —— 同一句话在单意图/多意图两条路径结论不同，最难排查。
        ("requirement_quality", ("需求质量", "质量评审", "模糊词", "不可验证")),
        ("requirement_analysis", ("需求分析", "需求条目", "需求提取", "解析需求", "requirement")),
        ("impact", ("影响分析", "变更影响", "impact")),
        ("report_generation", ("报告", "汇报", "导出", "report")),
        ("review", ("评审", "校验", "预评审", "review", "validate")),
        ("design", ("方案设计", "架构", "建模", "sysml", "代码生成", "视图", "bdd", "ibd", "代码", "design")),
        ("knowledge_qa", ("知识库", "检索", "knowledge")),
    )

    def _clause_to_intent(self, clause: str) -> str:
        """子句 → 意图名；无法映射返回原文本（供上层展示/拼接）。"""
        c = clause.strip()
        low = c.lower()
        for intent, keywords in self._CLAUSE_INTENTS:
            if any(k in low for k in keywords):
                return intent
        return c

    def _clause_to_intent_strict(self, clause: str) -> str:
        """L2 用严格映射：只认特异词（泛词不算），无法映射返回原文本。"""
        c = clause.strip()
        low = c.lower()
        for intent, keywords in self._CLAUSE_SPECIFIC:
            if any(k in low for k in keywords):
                return intent
        return c

    def _split_stage_clauses(self, text: str) -> list:
        """L1：多阶段指令（阶段连词）→ 原始子句列表（未映射）；无阶段连词返回空列表。"""
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

    def _split_parallel_clauses(self, text: str) -> list:
        """L2：并列清单 → `[{intent, text}]`；能映射出的**不同意图 < 2** 时返回空列表。

        切分：先按 `、，,；;` 断句；每段再**试着**按并列连词（以及/同时/然后/接着/并）细切，
        **只有当细切出的片段能映射出 ≥2 个不同意图时才采用细切结果**，否则保留整段 ——
        这样既不会把「合并需求」这类词内"并"拆坏（细切后只有 1 个意图 → 回退整段），
        也不会漏掉「变更影响分析以及生成报告」这种"整段可映射、但内含两个阶段"的说法
        （整段会映射成 impact，把"生成报告"整段吞掉 —— 实测踩到）。
        过滤：映射不上意图的片段（如"提供一段需求"这类背景句）直接丢弃；同一意图只保留首个。
        """
        if not text or not isinstance(text, str):
            return []
        frags = []
        for seg in self._PARA_SEP.split(text.strip()):
            seg = seg.strip()
            if not seg:
                continue
            subs = [s.strip() for s in self._PARA_CONN.split(seg) if s.strip()]
            # "映射成功"的判据 = 返回值不再是原文本本身（`_clause_to_intent_strict` 未命中时原样返回）
            mapped = {self._clause_to_intent_strict(s) for s in subs}
            mapped = {m for m in mapped if m in self.INTENTS}
            if len(subs) > 1 and len(mapped) >= 2:
                frags.extend(subs)
            else:
                frags.append(seg)
        tasks = [{"intent": self._clause_to_intent_strict(f), "text": f} for f in frags]
        return self._finalize_tasks(tasks)   # 2026-09-26：收口逻辑收敛到 _finalize_tasks（L1/L2 共用）

    def _finalize_tasks(self, tasks: list) -> list:
        """多意图收口（L1/L2 **共用**）：过滤"没映射上意图"的片段、同一意图只留首段、不足 2 个不同意图不算多意图。

        2026-09-26（多意图评测集驱动）：此前只有 L2 做了这套收口，L1 直接用宽松映射 + 不去重 + 不判数量，
        实测三类错（都进了 `eval_multi_intent.py` 的错例清单）：
          ① 「先生成结构视图，再生成参数视图」→ `['design','design']`（同一意图被当成两个阶段）
          ② 「先看看再想想」→ `['看看','想想']`（映射失败的原句片段被当成意图名）
          ③ 宽松映射按元组顺序首个命中，「做需求质量评审」落到 requirement_analysis（应为 requirement_quality）
        判据同 L2：映射函数的**未命中约定是"原样返回文本"**，故 `it == text` 即未映射。
        """
        out, seen = [], set()
        for t in tasks or []:
            it, txt = t.get("intent"), t.get("text")
            if not it or it == txt or it not in self.INTENTS or it in seen:
                continue
            seen.add(it)
            out.append(t)
        return out if len(out) >= 2 else []

    def split_multi_tasks(self, text: str) -> list:
        """多意图两级切分入口 → `[{intent, text}]`（text = 该阶段的原始子句，供 stage_hint 用）。

        L1 阶段连词（先…再…/最后）优先；L1 无命中才走 L2 并列清单兜底。
        返回空列表 = 单意图（由 detect() 处理）。

        2026-09-26：L1 与 L2 **统一收口**（`_finalize_tasks`）并统一用**严格映射**
        （`_clause_to_intent_strict`：泛词不算），避免"两级语义不一致"——
        同一句话因为走了 L1 还是 L2 而得出不同的意图，是最难排查的一类不一致。
        """
        clauses = self._split_stage_clauses(text)
        if clauses:
            return self._finalize_tasks(
                [{"intent": self._clause_to_intent_strict(c), "text": c} for c in clauses])
        return self._split_parallel_clauses(text)

    def split_multi_intent(self, text: str) -> list:
        """Task 11-① 多意图分解：子意图名列表（保留旧签名；内部走两级切分）。"""
        return [t["intent"] for t in self.split_multi_tasks(text)]

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
        """Task 11-③ 组合入口：多阶段输入 →
        {"intent":"multi","sequence":[子意图名],"raw_subtasks":[原始子句],"tasks":[{intent,text}]}。

        两级切分（`split_multi_tasks`）：阶段连词优先、并列清单兜底；无命中返回 None
        （上层走既有 detect 单意图路径）。纯规则，不改 detect() 主流程。

        2026-09-25：新增 `tasks`（阶段+原句）。`raw_subtasks` 仍保持**字符串列表**
        （SSE 契约与前端 `11-pipeline.js` 的展示都按字符串消费，改结构会直接渲染成
        [object Object]）；但编排需要的"每阶段在说什么"必须带上，故新增字段而非改旧字段
        —— stream.py 用它拼 `stage_hint`（planner 阶段序约束）。
        """
        tasks = self.split_multi_tasks(text)
        if not tasks:
            return None
        return {"intent": "multi",
                "sequence": [t["intent"] for t in tasks],
                "raw_subtasks": [t["text"] for t in tasks],
                "tasks": tasks}

