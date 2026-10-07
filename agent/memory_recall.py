# -*- coding: utf-8 -*-
"""P0 记忆召回（Memory-as-Recall-Source）：把「已沉淀的经验/决策」变成 RAG 的一路召回源。

背景（2026-09-29 调研结论，见 docs/LLM-Wiki与RAG对比调研-及MBSE知识库优化建议-20260929.md）：
本工程**累积层早已建成但检索侧零消费** —— `agent_memory`（125 条）与 `project_memories`（24 条）
的消费者只有 `agent/pipeline_parts/memory.py`（pipeline 一次性注入 Constitution 块），
`agent/rag.py::retrieve()` 从不读它们（全文件搜 `memory` 仅 1 处命中，且是 `graph_db.use_memory` 图谱开关）。
后果：`knowledge_reflow.py` 的承诺「结论不流失，成为下次任务的记忆上下文」**只兑现了一半** ——
摄取了、沉淀了，但没被下次检索消费。本模块补的就是这个闭环。

定位（对齐 Karpathy LLM Wiki 的「编译层」）：
- RAG 层（document_chunks 5758 块）：**精确定位**原始证据，逐句溯源。
- 记忆层（本模块）：**跨会话综合**的结论/决策/经验 —— 是"已经想明白过的事"，不必每次从原文重新推导。

⚠️ 三条纪律（都是本项目踩过的坑，不是理论洁癖）：

1. **绝不作为事实来源注入**。记忆是 LLM 提炼产物，可能含幻觉。`recall_reason` 必须显式标注
   「记忆」并附「仅供对齐、不得作为事实依据引用」的定位声明 —— 与 `_build_memory_hint`
   的既有口径一致（那里已写「不得虚构扩展或作为事实来源引用」）。

2. **`project_memories` 必须按 project_id 过滤，不得全库召回**。实测（2026-09-29）：
   24 条里 `project_id` 大量为**空串**，且含 `source='test-reg'` 的测试数据
   （「IP67防护等级要求」「CAN总线通信接口」各重复多条）。若不过滤，
   **测试数据会冒充项目知识进入检索结果**。无 project_id 上下文 → 该路**直接不召回**。

3. **按内容长度截断 + 去重**。实测 `agent_memory` 中存在 `mem_type='experience'` 的**整篇报告正文**
   （id=82「# MBSE 任务汇总最终报告（修订版）」），200+ 字起步。原样并入 RRF 会把
   长噪音顶上榜单。故：单条截断到 `rag.memory_max_chars`，且按内容前 N 字去重（同题重复条目只留最高分）。

零新增 schema：只读既有 `agent_memory` / `project_memories` 两表，复用 `MemoryService.search`
（真向量优先 + 时间衰减 + bigram 兜底，已是成熟实现）与 `MemoryService._scope_filter`（mem0 四维作用域）。

⚠️ 4. **必须过噪音闸门**（2026-09-29 实测逼出来的第 4 条纪律）。
   在**真实库**上跑 `MemoryService.search(conn,'chat','SysML v2 包结构怎么设计')`，召回 Top1 是：
     「你好，我是 MBSE 平台的通用助手。你发送的「123」我这边没有识别到具体意图…」（闲聊话术）
   实测 `agent_memory` 118 条中 106 条是 `mem_type='experience'`，而内容形态高度杂糅：
   纯问候语、平台话术、以及 `# XXX最终报告` 形态的**长文档标题**（正文被截到 400 字）。
   若不过滤，P0 就等于**往检索结果里灌噪音** —— 比不接更糟（模型会当"经验"对齐）。
   故 `_is_noise()` 为硬闸门：命中即丢弃，不参与排序（宁可少召回，不可假召回）。
"""
import logging
import re

logger = logging.getLogger(__name__)

# 噪音特征（对话话术/平台自我介绍/纯标题/空壳）——命中任一条即判噪音。
# 判据来源：2026-09-29 实测生产库 118 条 agent_memory 的形态归纳（见模块 docstring 纪律 4）。
_NOISE_PATTERNS = (
    re.compile(r"^(你好|您好|嗨|hi|hello|谢谢|感谢|收到|好的|ok|嗯)[，,。!！\s]"),
    re.compile(r"我是\s*MBSE\s*平台的?通用助手"),
    re.compile(r"没有识别到具体意图|方便补充一下你?想做什么|请补充"),
    re.compile(r"^#{1,3}\s"),              # 纯 Markdown 标题开头（长报告被截断后的残骸）
    re.compile(r"^[>【]\s*(整合范围|说明|本报告)"),
    # ── Mock / 占位回声（P1-记忆 2026-10-07 评测实测泄漏，access=70 且排名第 1，score 0.520）──
    # 形态：`（Mock 回答）已收到你的消息：xxx` / `【Mock】…`。
    # 为什么之前漏了：既有第 1 条只锚**行首**的「收到」，而本样本行首是全角「（」；
    # 长度 34 又过了 12 字下限 ⇒ 两道闸都不命中。
    # ⚠️ 只锚**括号前缀**这一形态硬特征，不加"含'消息'就判噪音"这类宽泛规则
    #（"用户消息应先校验字段"这类真经验会含"消息"⇒ 会误杀）。
    re.compile(r"^[(（【\[]\s*(Mock|MOCK|mock)\s*(回答|回复|响应)?\s*[)）\]】]"),
)
# 含以下信号才可能是有价值的「可复用结论/经验」（宽松白名单，避免误杀真经验）
_VALUE_HINTS = ("必须", "应", "采用", "结论", "规范", "约束", "格式", "步骤", "先", "不", "需", "要点", "基线")


def _is_noise(text: str) -> bool:
    """噪音判别：对话话术 / 平台自我介绍 / 长文档残骸 / 过短空壳。

    ⚠️ 刻意**保守**（只挡明确无价值的），因为误杀一条真经验的代价 > 放过一条噪音
    （本项目已因"用分不开的阈值做删除决策"吃过亏，见 pipeline_parts/memory.py 的标定注释）。
    故判据全部是**形态级硬特征**，不用相似度阈值。
    """
    s = " ".join(str(text or "").split())
    if len(s) < 12:                        # 过短：不足以承载可复用结论
        # ⚠️ 阈值取 12 而非 20：实测「建模时必须先定包结构，再生成需求/部件」（17 字）是**真经验**，
        #    20 字阈值会误杀它。误杀真经验的代价 > 放过一条短噪音（本项目"分不开的阈值不可做删除决策"
        #    的既有教训，见 pipeline_parts/memory.py 标定注释），故取更保守的下限。
        return True
    for p in _NOISE_PATTERNS:
        if p.search(s):
            return True
    # 长文档残骸：整段以标题式短语开头且无明显结论信号 → 视为"报告壳"
    if len(s) > 150 and not any(k in s for k in _VALUE_HINTS):
        return True
    return False


# 记忆召回默认参数（可被 rag.* 配置覆盖；默认值与「改动前无此路」等价语义 = 不影响存量行为，
# 因为这是**新增**召回源，不改变既有 BM25/向量/HyDE 的打分）
DEFAULT_TOP_K = 4
DEFAULT_MAX_CHARS = 200
DEFAULT_WEIGHT = 0.25
# 跨域兜底降权系数（2026-09-29）：同域召回不足时补位，但分数打折，绝不压过同域结果。
DEFAULT_CROSS_PENALTY = 0.7
# 跨域兜底触发门槛（2026-09-29）：同域最高分低于此值才跨域兜底。
# 标定依据：实测同域强命中 ≈0.42（bigram 口径）、跨域弱命中 ≈0.03-0.07；
#   0.25 落在两档之间，能把"同域已够用"与"同域确实没货"分开。
DEFAULT_CROSS_MIN_SCORE = 0.25


def _cfg_float(key: str, default: float) -> float:
    """读 rag.* 浮点配置；缺失/异常回默认（不抛，不阻断检索主链路）。"""
    try:
        from core import config as _cfg
        return float(_cfg.get("rag", key, default))
    except Exception:
        return default


def _cfg_int(key: str, default: int) -> int:
    try:
        from core import config as _cfg
        return int(_cfg.get("rag", key, default))
    except Exception:
        return default


def _cfg_bool(key: str, default: bool) -> bool:
    try:
        from core import config as _cfg
        v = _cfg.get("rag", key, default)
        if isinstance(v, bool):
            return v
        return str(v).strip().lower() in ("1", "true", "yes", "on")
    except Exception:
        return default


def _clean(text: str, limit: int) -> str:
    """压缩空白 + 按长度截断（不截半句话优先）。记忆正文常含 Markdown/换行，压平便于入 prompt。"""
    s = " ".join(str(text or "").split())
    if len(s) <= limit:
        return s
    cut = s[:limit]
    # 尽量在句末断开，避免「从句子中间断开」的半句话（与 conv_summary 同一纪律）
    for sep in ("。", "；", "，", ".", ";", ","):
        idx = cut.rfind(sep)
        if idx >= limit * 0.6:
            return cut[:idx + 1]
    return cut


def recall_memories(conn, query: str, agent_id: str = "chat", scopes: list | None = None,
                    project_id: str = "", top_k: int | None = None) -> list:
    """记忆召回：agent_memory（语义相关）+ project_memories（项目宪法）→ 统一结构列表。

    参数：
      query      —— 用户查询（用于语义相关度打分；空则退化为按时间序，见 MemoryService.search）
      agent_id   —— 记忆归属 Agent（intent），与 MemoryService 口径一致
      scopes     —— 有序作用域槽位 [(type, id), ...]（mem0 四维）；None → 仅按 agent_id（旧行为）
      project_id —— 项目 id。**非空才召回 project_memories**（纪律 2：防测试数据冒充项目知识）
      top_k      —— 两路各自上限；None → rag.memory_top_k（默认 4）

    返回 [{content, score, source_type, mem_type/category, scope, meta, recall_reason}, ...]
      按 score 降序。异常一律返回 []（**绝不抛** —— 检索主链路不能被记忆侧拖死）。
    """
    if top_k is None:
        top_k = _cfg_int("memory_top_k", DEFAULT_TOP_K) or DEFAULT_TOP_K
    max_chars = _cfg_int("memory_max_chars", DEFAULT_MAX_CHARS) or DEFAULT_MAX_CHARS

    out = []
    dropped = 0
    # ── 路 1：agent_memory（跨会话经验/事实/偏好，语义相关 + 作用域过滤）──
    # ⚠️ 2026-09-29 修复「同域饥饿」（实测暴露）：
    #   原实现只调 `MemoryService.search(conn, agent_id, ...)`，而 `agent_id` 是**精确过滤**。
    #   库内记忆按域分布严重不均：chat=1 / design=24 / requirement_analysis=13 / knowledge_qa=11 /
    #   impact=10 / zhiyuan_mgmt=9 / team_leader=2（清理后实测）。于是当 intent 落在**稀疏域**
    #   （如 chat 只有 1 条、且那条恰好是寒暄噪音）时，**召回恒为 0 条** ——
    #   而库里躺着 69 条其它域的真经验（"查询智源工程包结构树时，先…再…"这类通用方法论）。
    #   这不是"设计上的隔离"，而是**把「未命中」错当「没有」**：用户问的是通用工程问题，
    #   答案就在 design/requirement_analysis 域里，却因为当前 intent 标签不同而拿不到。
    #   修法：**同域优先 + 跨域兜底**（两级召回，跨域降权）：
    #     ① 先按 agent_id（+scopes）取同域，正常打分；
    #     ② 若同域有效条目 < top_k → 再全库召回一次（排除已取的），
    #        跨域条目 score 乘 `rag.memory_cross_domain_penalty`（默认 0.7）**降权**，
    #        使其只在本域无货时补位，绝不压过同域结果。
    #   作用域隔离仍由 `scopes` 承载（project/user 槽），**不放松**；这里放松的只是 agent_id 维度。
    _cross_penalty = _cfg_float("memory_cross_domain_penalty", DEFAULT_CROSS_PENALTY)
    try:
        from memory_service import MemoryService

        def _collect(rows, cross: bool, seen_ids: set):
            nonlocal dropped          # ⚠️ 必须声明：`dropped += 1` 否则会让它成为 _collect 的局部变量，
            got = []                  #    报 "cannot access local variable 'dropped'"，整路静默失败。
            for r in rows or []:
                _rid = r.get("id")
                if _rid in seen_ids:
                    continue
                raw = str(r.get("content") or "")
                if _is_noise(raw):      # 纪律 4：噪音硬闸门（宁可少召回，不可假召回）
                    dropped += 1
                    continue
                text = _clean(raw, max_chars)
                if not text:
                    continue
                _sc = float(r.get("score") or 0.0)
                seen_ids.add(_rid)
                got.append({
                    "content": text,
                    "score": _sc * (_cross_penalty if cross else 1.0),
                    "source_type": "agent_memory" + ("_cross" if cross else ""),
                    "mem_type": r.get("mem_type") or "fact",
                    "scope": f"{r.get('scope_type') or 'agent'}:{r.get('scope_id') or agent_id}",
                    "meta": {"created_at": r.get("created_at") or "", "id": _rid,
                             "cross_domain": bool(cross),
                             "own_agent": r.get("agent_id") or "",
                             "raw_score": round(_sc, 4)},
                })
            return got

        _seen = set()
        _same = MemoryService.search(conn, agent_id or "chat", query or "", top_k=top_k * 3,
                                     scopes=scopes)
        _hits = _collect(_same, False, _seen)
        # 跨域兜底（2026-09-29 第二轮修正）：判据从「条数不足」改为「**同域没有够强的命中**」。
        #   原因：只按条数判断会导致"同域已命中 0.42 的强相关经验，却仍拉一堆 0.03 的跨域项"，
        #   把榜单稀释成噪音。记忆侧的价值在**精度**，不在召回条数 ——
        #   同域有一条 0.25 以上的命中，说明本域经验够用，不必跨域。
        # ⚠️ `_hits` 是 dict 列表（不是 (dict, score) 元组）—— 曾因元组解包写错而整路抛错，
        #    被最外层 except 静默吞成"召回 0 条"，看起来像"没有记忆"（实测踩到）。
        _strong = max((float(h.get("score") or 0.0) for h in _hits), default=0.0)
        _cross_floor = _cfg_float("memory_cross_min_score", DEFAULT_CROSS_MIN_SCORE)
        if _strong < _cross_floor and not scopes:
            try:
                _all = MemoryService.search(conn, "", query or "", top_k=(top_k * 3),
                                            any_scope=True)
                _hits += _collect(_all, True, _seen)
            except Exception as e:
                logger.warning("记忆跨域兜底失败（不阻断）: %s", e)
        out.extend(_hits)
    except Exception as e:
        logger.warning("记忆召回 agent_memory 路失败（不阻断）: %s", e)

    # ── 路 2：project_memories（项目规范/决策，按项目过滤 + 关键词相关度）──
    # 纪律 2：project_id 为空 → 直接跳过，不做全库召回（防测试数据混入）
    if project_id:
        try:
            prow = conn.execute(
                "SELECT id, category, title, content FROM project_memories "
                "WHERE project_id=? AND enabled=1 ORDER BY id", (project_id,)).fetchall()
            if prow:
                # 相关性：复用 VectorEngine bigram 余弦（零依赖、确定性），与 agent_memory 路同口径可比
                from knowledge_engine import VectorEngine
                ve = VectorEngine()
                qv = ve._vector(query or "")
                scored = []
                for r in prow:
                    d = dict(r)
                    raw = str(d.get("content") or "")
                    if _is_noise(raw):      # 纪律 4 同口径
                        dropped += 1
                        continue
                    body = _clean(raw, max_chars)
                    if not body:
                        continue
                    title = str(d.get("title") or "").strip()
                    # 标题是记忆的「索引键」，计入打分但按短文本权重处理（标题命中的语义信号很强）
                    s = ve._cosine(qv, ve._vector(f"{title} {body}")) if qv else 0.0
                    scored.append((s, d, body, title))
                scored.sort(key=lambda x: x[0], reverse=True)
                for s, d, body, title in scored[:top_k]:
                    # 零相关度不召回（避免「本项目所有记忆」无条件灌入，与 _build_model_context 的教训一致）
                    if s <= 0:
                        continue
                    out.append({
                        "content": f"{title}：{body}" if title and not body.startswith(title) else body,
                        "score": float(s),
                        "source_type": "project_memory",
                        "mem_type": d.get("category") or "规范",
                        "scope": f"project:{project_id}",
                        "meta": {"created_at": d.get("created_at") or "", "id": d.get("id")},
                    })
        except Exception as e:
            logger.warning("记忆召回 project_memories 路失败（不阻断）: %s", e)

    # ── 跨路去重（纪律 3）：同题/同内容只留最高分（agent_memory 与 project_memories 会重复沉淀同一结论）──
    dedup = {}
    for it in out:
        key = it["content"][:60]        # 前 60 字作指纹：足以区分不同条目，又不至于因尾部差异漏去重
        cur = dedup.get(key)
        if cur is None or it["score"] > cur["score"]:
            dedup[key] = it
    merged = sorted(dedup.values(), key=lambda x: x["score"], reverse=True)[:top_k]

    # 定位声明（纪律 1）：与 _build_memory_hint 同一口径
    for i, it in enumerate(merged):
        it["rank"] = i + 1
        it["recall_reason"] = (
            f"记忆召回[{it['source_type']}#{i + 1}] score={round(it['score'], 3)}"
            f"（已沉淀的{it['mem_type']}，仅供对齐，不得作为事实依据引用）")
    if dropped:
        logger.info("记忆召回：噪音闸门丢弃 %d 条（纪律 4）", dropped)
    return merged
