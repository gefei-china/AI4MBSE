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


# 词边界字符集（**不含 `_`**：下划线是标识符分段符，不是单词内字符）。
#
# 2026-11-08 修正：上一版把 `_` 算进词内字符（`[A-Za-z0-9_]`），本意是阻止
# `SysML_V2` 里的 `v2` 被再次替换成 `SysML_SysML_V2`。但那是**膨胀**问题，
# 已由 `_already_canonical()`（「原文该处已是完整规范形态则跳过」）单独守住 ——
# 实测把 `_` 当分隔符后，存量 6579 条 query 的归一化**非幂等仍为 0、新增膨胀仍为 0**。
# 而把 `_` 算作词内字符的代价是**丢掉合法命中**：`sysml_v2_validate`、
# `内部块图_IBD` 这类蛇形标识符整串当 token ⇒ `SysML_V2` / `sysml` / `v2` 一个都
# 命中不了 ⇒ 域/意图信号丢失（实测 360 条 query 因此从不带域约束）。
_WORD_CH = r"[A-Za-z0-9]"

# term 首/尾是否是「单词内字符」——决定该侧要不要词边界。与 _WORD_CH 同口径。
_ASCII_EDGE = re.compile(r"[A-Za-z0-9]")

# term → 编译好的正则（模块级缓存）。match() 会被每条 query 调用一次，
# 词表 134 条逐个 re.compile 会拖慢首包；缓存后每条 query 只做 134 次 search。
_PAT_CACHE = {}


def _term_pattern(term: str):
    """term 的匹配正则：**按 term 自身首尾字符决定该侧是否需要词边界**。

    ★ 2026-11-08：`match()` 此前用 `term.lower() in text.lower()` 做**子串**判断，
    而 `_span_replace()` 用边界正则 —— 两者口径不同，hits 比替换宽。宽松的那一侧
    恰好是**下游用来定 force_domain / force_intent 的**，于是英文单词内部的短缩写
    被当成术语命中，把 query 钉死在一个不相干的域上。实测存量 6579 条 query
    里被这种假命中污染的：

        PA  ⊂ part/Part         452 条 → domain=satellite_comms
        SA  ⊂ satisfy           248 条 → domain=satellite_comms
        CEP ⊂ accept             57 条 → domain=loitering_munition
        LM  ⊂ AI/LLM             50 条 → domain=loitering_munition
        tms ⊂ EVTMS               5 条 → domain=thermal_mgmt
        Antenna ⊂ AntennaInterfaceUnit  5 条 → domain=satellite_comms

    `ref part` 被判成卫星通信域（库内该域 0 chunk）→ 必然零召回，正是这批假命中
    造成的既有故障。故**一处定义、两处共用**（match / _span_replace 都走本函数），
    以后不可能再漂移。

    为什么边界要**按 term 自身首尾字符**决定，而不是统一加：
      · ASCII 边缘必须有边界：`PA` 不能命中 `part`（英文单词内部无术语）；
      · CJK 边缘不该加：中文没有空格分词，`转发器EIRP` 里 `转发器` 是独立术语，
        统一加 `(?!…)` 会让它**因为后面紧跟英文而被漏掉**（同一类错误的另一方向）。
    """
    key = term.lower()
    pat = _PAT_CACHE.get(key)
    if pat is not None:
        return pat
    pre = r"(?<!%s)" % _WORD_CH if _ASCII_EDGE.match(term[:1]) else ""
    suf = r"(?!%s)" % _WORD_CH if _ASCII_EDGE.match(term[-1:]) else ""
    pat = re.compile(pre + re.escape(term) + suf, re.IGNORECASE)
    _PAT_CACHE[key] = pat
    return pat


def _term_in(text: str, term: str) -> bool:
    """term 是否在 text 中以「合法边界」出现（match 与 normalize 的同一口径）。"""
    return bool(term) and _term_pattern(term).search(text) is not None


def _span_replace(text: str, hits: list) -> str:
    """在**原文**上单遍替换：长词优先、区间互斥、「已是规范形态」则跳过。

    三条规则各自不可省，反例见 tools/verify/verify_glossary_normalize_idempotent.py
    的 M1/M2/M3 变异（去掉任一条都会让幂等判据翻红）。
    """
    cands = []
    for r in hits:
        u = r.get("user_term") or ""
        if not u:
            continue
        k = r.get("canonical_term") or u
        for mo in _term_pattern(u).finditer(text):
            s, e = mo.start(), mo.end()
            hit = text[s:e]
            if hit == k:
                continue                      # 原文此处与规范词逐字相同
            if _already_canonical(text, s, e, k):
                continue                      # 原文该处已是完整规范形态（防膨胀）
            cands.append((s, e, k, len(u)))
    if not cands:
        return text
    # 长词优先（避免短词吃掉长词的一部分）；等长时起点靠前者优先，保证结果确定
    cands.sort(key=lambda t: (-t[3], t[0]))
    taken = []
    for s, e, k, _ in cands:
        if any(not (e <= a or s >= b) for a, b, _ in taken):
            continue                          # 与已选区间重叠 ⇒ 丢弃（长词已占位）
        taken.append((s, e, k))
    taken.sort()
    out, cur = [], 0
    for s, e, k in taken:
        out.append(text[cur:s])
        out.append(k)
        cur = e
    out.append(text[cur:])
    return "".join(out)


def _already_canonical(text: str, s: int, e: int, canonical: str) -> bool:
    """原文 [s,e) 处是否已构成完整的规范形态 —— 用于阻止「替换膨胀」。

    判据取**对称**形式：canonical 若由 `prefix + 命中片段 + suffix` 组成，且
    prefix 紧邻命中片段之前、suffix 紧邻其后都已经在原文里，则该处本就规范。

    为什么对称而不是只判 `canonical.startswith(命中片段)`（前向）：
      `宽带` → `宽带通信`（suffix 型，判后向 `text[e:e+len(suf)]`）
      `语法规范` → `SysML_V2 语法规范`（prefix 型，判前向 `text[s-len(pre):s]`）
    两种都必须覆盖，否则 `语法规范 查询` 第一次换成 `SysML_V2 语法规范 查询`
    后，第二次会把其中的 `语法规范` 再换一次，成为
    `SysML_V2 SysML_V2 语法规范`（幂等判据直接翻红）。
    """
    low_hit, low_k = text[s:e].lower(), canonical.lower()
    if not low_k or low_hit not in low_k:
        return False
    idx = low_k.index(low_hit)
    pre, suf = canonical[:idx], canonical[idx + (e - s):]
    if not pre and not suf:
        # canonical 与命中片段等值、只差大小写（`ibd` → `IBD`）⇒ **要替换**，
        # 否则大小写归一这条既有行为被静默取消。真正完全相等的已在调用方 continue。
        return False
    if pre:
        if s - len(pre) < 0 or text[s - len(pre):s].lower() != pre.lower():
            return False
    if suf:
        if text[e:e + len(suf)].lower() != suf.lower():
            return False
    return True


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

        ★ 2026-11-08：命中判据由「子串」改为 `_term_in()`（与 `_span_replace` 同一套
        边界规则）。子串口径会让 `PA` 命中 `part`、`SA` 命中 `satisfy`，而 hits 正是
        `resolve()` 汇总 force_domain / force_intent 的来源 ⇒ 假域约束 → 零召回。
        详见 `_term_pattern()` 的说明。
        """
        if not text:
            return []
        low = text.lower()
        hits = []
        seen_terms = set()
        # 概念层优先（新机制），同一词不重复命中路由层
        for r in self._load_concepts():
            u = r.get("user_term") or ""
            if u and u.lower() in low and _term_in(text, u):
                hits.append(r)
                seen_terms.add(u.lower())
        for r in self._load():
            u = r.get("user_term") or ""
            if u and u.lower() in low and u.lower() not in seen_terms \
                    and _term_in(text, u):
                hits.append(r)
        return hits

    def match_concepts(self, text: str) -> list:
        """P0-3：概念级召回（只返回概念层命中，供前端术语选择器 / 检索归一用）。

        deprecated 概念命中时附 replaced_by，前端提示"该词已弃用，建议使用 XXX"。
        边界口径与 `match()` 一致（同一 `_term_in`）。
        """
        if not text:
            return []
        low = text.lower()
        return [r for r in self._load_concepts()
                if (r.get("user_term") or "").lower() in low
                and _term_in(text, r["user_term"])]

    def normalize(self, text: str) -> tuple:
        """归一化：把命中的 user_term 替换为 canonical_term（概念层用规范词 pref_label）。
        返回 (normalized_text, hits)。无命中时原样返回。

        ★ 2026-11-08 重写（原实现是「多轮 re.sub」，非幂等，会膨胀）：
        原写法 `for r in sorted(hits, ...): out = pat.sub(...)` 后一轮作用于
        **前一轮的结果** ⇒ 替换产物被再次扫描。而词表里有 12 条 entry 的
        canonical 本身就包含自己的 user_term：

            宽带   → 宽带通信          巡飞 → 巡飞待机
            v2     → SysML_V2          ibd  → 内部块图_IBD
            语法规范 → SysML_V2 语法规范   v波段 → V波段_卫星通信频段  …

        于是每次替换都把串撑长一截，且下一轮再撑一次：
            `宽带通信`   → `宽带通信通信`
            `巡飞待机弹` → `巡飞待机待机弹`
            `SysML v2`  → `SysML_SysML_V2`（`v2` 命中自己刚产出的 `SysML_V2`）
        实测存量 3000 条 query 里 **699 条（23.3%）再归一化一次还会变**。

        非幂等的代价不是"日志难看"：同一条 query 因被归一化的次数不同而得到
        不同检索串 ⇒ 召回不可复现；而膨胀出来的串（`宽带通信通信`）恰好
        召不回原文能命中的 chunk。故改为**在原文上做单遍替换**：
        canonical 只写回输出，绝不再参与扫描。

        三条规则合起来才幂等（缺一条都有反例，见门禁 M1/M2/M3）：
        1. span 单遍 + 区间互斥 + 长词优先 —— 替换结果不被二次扫描；
        2. 边界字符集含 `_` —— 否则 `SysML_V2` 里的 `V2`、`内部块图_IBD` 里的
           `IBD` 会被 `v2` / `ibd` 规则重新命中（`_` 不在 `[A-Za-z0-9]` 里）；
        3. 「已是规范形态则跳过」—— 否则 `宽带`→`宽带通信` 会把已规范的
           `宽带通信` 再撑成 `宽带通信通信`（这一条不是扫描问题，是膨胀问题，
           光做单遍修不掉）。
        """
        hits = self.match(text)
        if not hits:
            return text, hits
        return _span_replace(text, hits), hits

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
    """端到端查询 Trace 落库（P2-2）。返回 trace_id；**写入失败返回 0 并留痕**。

    2026-11-08 修（此前是 `except Exception: return 0`，静默）：
    静默的代价不是"少一条日志"，而是**用这张表算出的所有指标分母都偏小且无法
    察觉** —— 例如"零命中率 7.24%" 的分母只是成功写入的行，真实值只会更高，
    但看板上它长得像"已统计全量"。故改为：不冒泡（观测失败不得阻断检索），
    但必须进 `core.observe_fail` 计数 + 限速日志，供门禁与健康检查读取。

    另一个隐性问题一并修掉：`commit()` 失败会**留下未提交事务**，此后同一连接
    上的每次写入都会继续失败 —— 于是失败被一次次静默吞掉，形成"连环静默"，
    表现为 trace 从某一刻起整体消失。故失败时尽力 rollback。

    ★ 2026-11-08：`query` 与 `query_raw` 分工（二者的区别见迁移里的列注释）。
    签名里的 `query` 是**实际送检索的串**，不是用户原话 —— 它上游已被拼上
    kb_hint/槽位/附件片段，此处再被 Glossary 归一化。原话由 `trace["query_raw"]`
    单独传入；**没传时回退成本参 `query`**（而不是写 NULL）：
    写 NULL 会让"调用方没传"与"2026-11-08 之前的历史行"变成同一个取值，
    而这两件事的处置完全不同（前者是漏接线，后者是不可恢复的历史）。
    """
    _raw = trace.get("query_raw")
    # 三种"没有原话"的输入统一回退成本参 `query`：键缺失 / None / 空白串。
    # 为什么不写 ''/NULL：'' 与 NULL 都要留给"没有原话"这一个语义，
    # 而"调用方漏传"是**另一个**语义（接线 bug，应当由回退掩盖后在门禁里查出来），
    # 两者混成一个取值后，线上就再也分不清"这批样本太老"和"这段代码没接上"。
    if not isinstance(_raw, str) or not _raw.strip():
        _raw = query
    try:
        cur = conn.execute(
            "INSERT INTO query_trace (query, normalized, query_raw, intent, route, domain, hit_docs, "
            "hit_count, top_score, score_kind, top_graph, top_chunk, top_vec, "
            "latency_ms, detail) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                query[:500],
                (trace.get("normalized") or query)[:500],
                str(_raw or "")[:500],
                (trace.get("intent") or "")[:100],
                (trace.get("route") or "")[:50],
                (trace.get("domain") or "")[:50],
                json.dumps(trace.get("hit_docs", []), ensure_ascii=False)[:2000],
                trace.get("hit_count") or 0,
                trace.get("top_score") or 0,
                # ★ 2026-11-08：分数的**来源**与其三路原值（详见 agent/rag.py）。
                #   `top_score` 保留原义以兼容既有消费方；`score_kind` 回答
                #   "这个数是哪种分"，`top_*` 三列回答"另外两路各是多少"。
                #   三列都记而不择一 —— "择一"本身就是信息丢失，也正是旧实现
                #   把图置信度写成 0 的间接原因。
                (trace.get("score_kind") or "")[:20],
                trace.get("top_graph"),
                trace.get("top_chunk"),
                trace.get("top_vec"),
                trace.get("latency_ms") or 0,
                json.dumps(trace.get("detail", {}), ensure_ascii=False)[:4000],
            ),
        )
        conn.commit()
        return cur.lastrowid
    except Exception as e:                                 # noqa: BLE001
        # 尽力回滚：未提交事务会污染该连接的后续所有写入（见 docstring）
        try:
            conn.rollback()
        except Exception:                                  # noqa: BLE001
            pass
        try:
            from core.observe_fail import note as _note
            _note("trace_query", e, qlen=len(query or ""),
                  route=(trace.get("route") or "")[:20])
        except Exception:                                  # noqa: BLE001
            pass
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
