"""Glossary 术语归一化层（P0-1 / P1-1 / P2-1 / P2-3）。

行业对齐：
- CloudCanal「实体关联」+ Anyscale「Query Rewriting」：把用户短词/别名归一化为规范概念
- AWS Metadata Filtering + 企业分类法：domain 受控词表，检索前域过滤
- Sabitov 生产路由 playbook：置信度分级（≥0.70 采纳 / 0.40-0.69 复查 / <0.40 降级）

职责：
1. GlossaryMatcher：glossary 表加载 + 用户输入术语匹配 → 归一化 + 强制 intent/domain
2. infer_domain：上传文档按文件名/内容自动打 domain（低置信度进 review 队列）
3. trace_query：查询全链路落库（query → 归一化 → 意图 → 域 → 命中 → 输出）
"""
import json
import re
import time

# 受控词表：domain 白名单（企业分类法：6-12 顶层域）
# 2026-09-07 演示切换：+loitering_munition（巡飞弹领域，migrate_loitering.py 数据集使用）
DOMAINS = ["sysml_norm", "satellite_comms", "loitering_munition", "thermal_mgmt", "generic", "unknown"]

# 文件名 → domain 规则（正则，优先级从高到低）
_DOMAIN_FILE_RULES = [
    (re.compile(r"sysml|v2|语法|规范|kerml", re.I), "sysml_norm"),
    (re.compile(r"巡飞|察打|蜂群|战斗部|光电吊舱|自杀式无人机|loitering|munition", re.I), "loitering_munition"),
    (re.compile(r"宽带|通信|卫星|波束|转发器|载荷|V波段|kbp0|s1flow", re.I), "satellite_comms"),
    (re.compile(r"热管理|tms|温度|热控|thermal", re.I), "thermal_mgmt"),
]

# 内容关键词 → domain 规则（用于低置信度时的内容补判）
_DOMAIN_CONTENT_RULES = [
    (re.compile(r"\b(part def|package|requirement def|attribute)\b|SysML", re.I), "sysml_norm"),
    (re.compile(r"巡飞弹|巡飞待机|察打|蜂群|战斗部|光电吊舱|引信|弹射", re.I), "loitering_munition"),
    (re.compile(r"宽带|转发器|波束|载荷|V波段|TWTA|相控阵", re.I), "satellite_comms"),
    (re.compile(r"热管理|温度调节|冷却|散热|thermal", re.I), "thermal_mgmt"),
]

# 意图路由关键词（与 IntentRouter 对齐，供归一化后强制意图）
_INTENT_HINTS = {
    "sysml_norm": ["v2", "sysml", "语法", "规范", "代码", "kerml", "常见错误", "修正"],
    "loitering_munition": ["巡飞", "察打", "蜂群", "战斗部", "光电吊舱", "地面控制站", "发射"],
    "satellite_comms": ["宽带", "卫星", "通信", "转发器", "波束"],
    "thermal_mgmt": ["热管理", "热控", "温度", "tms", "冷却"],
}


def infer_domain(filename: str = "", content: str = "", return_score: bool = False):
    """上传文档自动打 domain。返回 (domain, confidence)。

    置信度规则：
    - 文件名命中 → 0.9（高置信，直接入库）
    - 文件名未中、内容命中 → 0.6（中置信，进 review 队列）
    - 均未命中 → ("unknown", 0.2)（低置信，进 review 队列）
    """
    score = 0.0
    domain = "unknown"
    for rx, d in _DOMAIN_FILE_RULES:
        if filename and rx.search(filename):
            if d != domain:
                domain, score = d, 0.9
            break
    if score < 0.9:
        for rx, d in _DOMAIN_CONTENT_RULES:
            if content and rx.search(content):
                if d != domain:
                    domain, score = d, 0.6
                break
    if return_score:
        # 未命中任何规则 → (unknown, 0.2)：低置信，进 review 队列
        return domain, max(score, 0.2)
    return domain


class GlossaryMatcher:
    """术语匹配（P0-3/P0-4 概念层版）。匹配逻辑：
    - 术语层：glossary_terms（preferred/synonym/alias）→ 概念级召回，命中带
      concept_id / definition / concept_status / replaced_by（弃用词给出替代建议）
    - 路由层：glossary kind='intent' 行原样保留（意图/域/加权，接口不变）
    - 子串匹配（不区分大小写，术语先按长度倒序避免短词先命中）
    兼容：旧调用方拿到的 hit 仍是 {user_term, canonical_term, domain, intent, boost}，
    新增 concept_* 字段；_load() 的 kind='intent' 过滤逻辑不动，避免回归。
    """

    def __init__(self, conn):
        self.conn = conn
        self._rows = None
        self._concept_rows = None

    def _load(self) -> list:
        if self._rows is None:
            try:
                # P0 方案 v2 / 词典隔离：意图路由只读 kind='intent'（兼容历史 NULL/空值），
                # 不再吃归一化映射（entity/predicate/prop_key），防止归一词污染意图路由。
                rows = self.conn.execute(
                    "SELECT user_term, canonical_term, domain, intent, boost, active "
                    "FROM glossary WHERE active=1 "
                    "AND (kind='intent' OR kind IS NULL OR kind='') "
                    "ORDER BY LENGTH(user_term) DESC"
                ).fetchall()
                self._rows = [dict(r) for r in rows]
            except Exception:
                self._rows = []
        return self._rows

    def _load_concepts(self) -> list:
        """P0-3：概念层术语加载。retired 概念不参与召回，deprecated 概念带 replaced_by。

        R1 合并（2026-09-07）：概念层已承载 intent/boost（原在旧 glossary 表），
        故一并取出供 resolve() 汇总 force_intent/boost——旧表归档后路由语义不丢。
        列缺失（老库未迁移）时自动回退为不带该列的查询，避免整个概念层失效。
        """
        if self._concept_rows is None:
            base = (
                "SELECT t.term AS user_term, c.pref_label AS canonical_term, "
                "c.domain, c.concept_status, c.replaced_by, c.concept_id, "
                "c.definition, t.term_kind, t.lang "
                "FROM glossary_terms t JOIN glossary_concepts c ON c.concept_id=t.concept_id "
                "WHERE c.concept_status!='retired' AND t.term_status!='deprecated' "
                "ORDER BY LENGTH(t.term) DESC")
            # P1 maps_to 消费（2026-09-07）：带出本体映射列，供 RAG 词典→图谱召回；
            # 老库缺列时逐级回退，保证概念层主通道不受影响。
            variants = (
                base.replace("t.term_kind, t.lang",
                             "t.term_kind, t.lang, c.intent, c.boost, c.maps_to_class, c.maps_to_inst"),
                base.replace("t.term_kind, t.lang", "t.term_kind, t.lang, c.intent, c.boost"),
                base.replace("t.term_kind, t.lang", "t.term_kind"),
                base,
            )
            for sql in variants:
                try:
                    rows = self.conn.execute(sql).fetchall()
                    self._concept_rows = [dict(r) for r in rows]
                    break
                except Exception:
                    continue
            if self._concept_rows is None:
                self._concept_rows = []
        return self._concept_rows

    def mapped_concepts(self, text: str) -> list:
        """P1：命中的概念里带本体映射（maps_to_class / maps_to_inst）的子集。

        消费方：GraphRAG._glossary_class_link——RAG 归一化命中概念后，
        按映射类召回图谱实体（词典→图谱的桥接通道）。
        """
        return [r for r in self.match_concepts(text)
                if (r.get("maps_to_class") or r.get("maps_to_inst") or "").strip()]

    def match(self, text: str) -> list:
        """返回命中列表（术语长度倒序）。

        hit 字段：intent 行 → {user_term, canonical_term, domain, intent, boost}；
        概念行 → 上述 + {concept_id, concept_status, replaced_by, definition, term_kind}。
        """
        if not text:
            return []
        low = text.lower()
        hits = []
        seen_terms = set()
        # 概念层优先（新机制），同一词不重复命中路由层
        for r in self._load_concepts():
            if r["user_term"].lower() in low:
                hits.append(r)
                seen_terms.add(r["user_term"].lower())
        for r in self._load():
            if r["user_term"].lower() in low and r["user_term"].lower() not in seen_terms:
                hits.append(r)
        return hits

    def match_concepts(self, text: str) -> list:
        """P0-3：概念级召回（只返回概念层命中，供前端术语选择器 / 检索归一用）。

        deprecated 概念命中时附 replaced_by，前端提示"该词已弃用，建议使用 XXX"。
        """
        if not text:
            return []
        low = text.lower()
        return [r for r in self._load_concepts() if r["user_term"].lower() in low]

    def normalize(self, text: str) -> tuple:
        """归一化：把命中的 user_term 替换为 canonical_term（概念层用规范词 pref_label）。
        返回 (normalized_text, hits)。无命中时原样返回。
        """
        hits = self.match(text)
        if not hits:
            return text, hits
        out = text
        # 长术语优先替换，避免子串交叉
        for r in sorted(hits, key=lambda x: len(x["user_term"]), reverse=True):
            # 大小写不敏感替换：只替换独立出现的词（前后非字母数字）
            pat = re.compile(
                r"(?<![A-Za-z0-9])" + re.escape(r["user_term"]) + r"(?![A-Za-z0-9])",
                re.IGNORECASE,
            )
            out = pat.sub(r["canonical_term"], out)
        return out, hits

    def resolve(self, text: str) -> dict:
        """一步到位：归一化 + 汇总强制意图/域。
        返回 {original, normalized, hits, force_intent, force_domain, boost,
              deprecated_hits}
        force_intent/force_domain：全部命中术语中取第一个非空（词表顺序 = 配置优先级）
        deprecated_hits：命中的已弃用概念（前端提示"建议使用 replaced_by"）
        """
        normalized, hits = self.normalize(text)
        force_intent = next((h.get("intent") for h in hits if h.get("intent")), "")
        force_domain = next((h.get("domain") for h in hits if h.get("domain")), "")
        boost = max([h.get("boost") or 1.0 for h in hits] or [1.0])
        deprecated_hits = [
            {"user_term": h.get("user_term", ""), "concept_id": h.get("concept_id", ""),
             "replaced_by": h.get("replaced_by", "")}
            for h in hits if h.get("concept_status") == "deprecated"]
        return {
            "original": text,
            "normalized": normalized,
            "hits": hits,
            "force_intent": force_intent,
            "force_domain": force_domain,
            "boost": boost,
            "deprecated_hits": deprecated_hits,
        }


def trace_query(conn, query: str, trace: dict) -> int:
    """端到端查询 Trace 落库（P2-2）。返回 trace_id。"""
    try:
        cur = conn.execute(
            "INSERT INTO query_trace (query, normalized, intent, route, domain, hit_docs, "
            "hit_count, top_score, latency_ms, detail) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                query[:500],
                (trace.get("normalized") or query)[:500],
                (trace.get("intent") or "")[:100],
                (trace.get("route") or "")[:50],
                (trace.get("domain") or "")[:50],
                json.dumps(trace.get("hit_docs", []), ensure_ascii=False)[:2000],
                trace.get("hit_count") or 0,
                trace.get("top_score") or 0,
                trace.get("latency_ms") or 0,
                json.dumps(trace.get("detail", {}), ensure_ascii=False)[:4000],
            ),
        )
        conn.commit()
        return cur.lastrowid
    except Exception:
        return 0


def log_domain_review(conn, document_id: int, filename: str, domain: str,
                      confidence: float, reason: str) -> int:
    """P2-3：低置信度 domain 分类进人工确认队列。返回 queue_id（0=无需确认/已存在）。

    幂等：同 document_id 存在 pending 记录时跳过（不重复入队）。
    """
    if confidence >= 0.9:
        return 0
    try:
        dup = conn.execute(
            "SELECT id FROM domain_review_queue WHERE document_id=? AND status='pending' LIMIT 1",
            (document_id,),
        ).fetchone()
        if dup:
            return dup["id"]
        cur = conn.execute(
            "INSERT INTO domain_review_queue (document_id, filename, suggested_domain, "
            "confidence, reason, status) VALUES (?,?,?,?,?, 'pending')",
            (document_id, filename[:200], domain, confidence, reason[:500]),
        )
        conn.commit()
        return cur.lastrowid
    except Exception:
        return 0


def format_recall_reason(hit: dict, matched: list) -> str:
    """P0-4：召回原因回显——说明这条命中为什么出现（可解释路由）。"""
    reasons = []
    for m in matched:
        reasons.append(f"术语「{m['user_term']}」→「{m['canonical_term']}」")
    if hit.get("glossary_boost"):
        reasons.append(f"术语加权 ×{hit['glossary_boost']}")
    if hit.get("domain_matched"):
        reasons.append(f"域匹配({hit['domain_matched']})")
    if reasons:
        return "; ".join(reasons)
    return ""
