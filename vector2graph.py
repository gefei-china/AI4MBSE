"""O-1：向量 → 图谱半自动转化工作流（客户 P13-13）。

客户语义：初期以向量库为临时存储与快速检索层，随人工确认将向量检索命中的
文档片段 / LLM 生成的候选模型元素渐进转化为正式知识库图谱元素——
"从向量到图的渐进转化，对设计师无感"，最终以图数据库为权威数据底座。

实现：
1. extract_candidates：对给定 chunk 命中（或全文片段），按本体 schema 用 LLM
   抽取候选实体/关系（Mock 模式确定性规则抽取兜底）→ v2g_candidates 表（pending）
2. confirm_candidates：人工确认 → GraphStore.bulk_create（过本体校验）→
   落库 + 建立 chunk↔entity 溯源链接（document_chunks.linked_entity_ids）
3. 增量更新：ingest 只处理新增文档（已有文档跳过），v2g 按 batch 增量
"""
import json
import logging
import re
import uuid

from ontology_semantics import GraphStore, OntologyValidator
# P0-2 表面归一实现迁入 entity_resolver（诊断 §4.3 P1-1 解循环依赖）；
# 本模块 re-export 保持既有调用方（sysml_importer / verify_kb_v2_enhance）零改动。
from entity_resolver import (  # noqa: F401  (re-export 兼容)
    _normalize_mentions, load_cross_ling_index)
# P0 方案 v2 / S2：谓词与属性 key 归一（词典体系统一，保守策略未命中原样返回）
from text_normalize import normalize_predicate, normalize_prop_key

logger = logging.getLogger(__name__)


def _survivorship_apply(conn, target_id: str, cand, operator: str) -> dict:
    """P0 方案 v2 / S1：确认挂靠时的 survivorship 择优合并。

    对齐 MDM golden record / Senzing full attribution：
    - 属性补齐：候选属性按 normalize_prop_key 归一后，目标实体缺失的 key 补入（保留方优先，
      冲突值不覆盖——图库即 golden record）
    - 别名并入：候选名 != 目标名时写入 properties.aliases（后续消歧/检索可命中）
    - 来源挂接：候选 source_doc 非空且目标为空时补记（不覆盖）
    - lineage 审计：graph_edit_logs 记录 op='survivorship_align'，含本次补齐 key 与别名
    返回 {props_merged: [key...], alias_added: bool}
    """
    row = conn.execute(
        "SELECT name, properties, source_doc FROM entities WHERE id=? AND status!='deprecated'",
        (target_id,)).fetchone()
    if not row:
        return {"props_merged": [], "alias_added": False}
    try:
        target_props = json.loads(row["properties"] or "{}") if row["properties"] else {}
    except Exception:
        target_props = {}
    try:
        cand_props = json.loads(cand["properties"] or "{}") if cand["properties"] else {}
    except Exception:
        cand_props = {}
    merged_keys = []
    for k, v in cand_props.items():
        nk = normalize_prop_key(k)
        if nk in ("aliases", "matched_word", "source_frag"):
            continue  # 内部元数据字段不参与择优
        if nk in target_props or v in (None, "", []):
            continue
        target_props[nk] = v
        merged_keys.append(nk)
    alias_added = False
    cand_name = cand["entity_name"] or ""
    aliases = list(target_props.get("aliases") or [])
    if cand_name and cand_name != row["name"] and cand_name not in aliases:
        aliases.append(cand_name)
        target_props["aliases"] = aliases
        alias_added = True
    conn.execute("UPDATE entities SET properties=? WHERE id=?",
                 (json.dumps(target_props, ensure_ascii=False), target_id))
    if not row["source_doc"] and (cand["source_doc"] or ""):
        conn.execute("UPDATE entities SET source_doc=? WHERE id=?",
                     (cand["source_doc"], target_id))
    conn.execute(
        "INSERT INTO graph_edit_logs (op, node_id, payload, operator) VALUES (?,?,?,?)",
        ("survivorship_align", target_id,
         json.dumps({"candidate": cand_name,
                     "candidate_id": cand["id"],
                     "props_merged": merged_keys, "alias_added": alias_added},
                    ensure_ascii=False), operator))
    # P2（2026-09-07）entity_aliases 写入侧：mention 原文 → canonical 别名映射
    # （此前只写 properties.aliases，实体别名表空转；写入后供检索召回 + sync-aliases 同步词典）
    try:
        _br = conn.execute("SELECT branch FROM entities WHERE id=?", (target_id,)).fetchone()
        from entity_resolver import write_entity_alias as _wea
        _wea(conn, target_id, cand_name, branch=(_br["branch"] if _br else "personal"),
             source_doc=cand["source_doc"] or "", source_type="survivorship_align",
             created_by=operator)
    except Exception as _ae:  # noqa: BLE001
        logger.warning("survivorship 写 entity_aliases 跳过（不阻断）: %s", _ae)
    return {"props_merged": merged_keys, "alias_added": alias_added}


def _new_batch_id() -> str:
    return f"v2g-{uuid.uuid4().hex[:8]}"


def _mock_extract_v2(text: str, entity_types: list, relation_types: list) -> dict:
    """Mock 抽取增强版：对齐信息抽取(NER/RE)行业标准。

    - 实体（NER）：本体类型词在文本中的**原子指称**（词本身，非上下文片段），
      同指称去重；修饰词/上下文保留在 properties.source_frag 供溯源。
    - 关系（RE）：正则匹配 `实体词 关系词 实体词` 三元组（头尾必须是已识别
      实体指称，关联对象明确），按关系词长度优先（长词如 SATISFIES 先匹配）。
    - 置信度：**实体 0.45、关系 0.5**（★ 2026-10-23 由 0.8/0.85 下调）。
      原值 0.8/0.85 看起来与真 LLM 抽取无异 ⇒ 用户分不清哪些候选是规则凑的；
      且高于系统确认阈值（ingest.confidence_threshold 0.75 附近）⇒ 有被误判为"高置信"的路径。
      规则抽取（把本体类型词当实体指称）精度本就低，**必须落在阈值之下强制人工复核**。
    - 每条候选带 `extractor: "rule"` 标记，落进 properties ⇒ 前端可显式区分来源。
    """
    nodes, edges = [], []
    seen_names = set()
    for w in entity_types:
        if not w or len(nodes) >= 20:
            continue
        for m in re.finditer(re.escape(w), text):
            name = w  # 原子实体指称 = 类型词本身
            if name in seen_names:
                break  # 每个类型词在本文档只出一个候选（去重）
            seen_names.add(name)
            nodes.append({
                "name": name,
                "entity_type": w,
                "confidence": 0.45,
                "properties": {"extractor": "rule", "matched_word": w,
                               "source_frag": text[max(0, m.start() - 20):m.end() + 20].strip()[:120]},
            })
            break
    # 关系：三元组 (实体)(0..14字)(关系词)(0..14字)(实体)
    if len(nodes) >= 2 and relation_types:
        node_names = [n["name"] for n in nodes]
        rels_sorted = sorted(relation_types, key=len, reverse=True)
        seen_edges = set()
        for rel in rels_sorted:
            if len(edges) >= 20:
                break
            alt = "|".join(map(re.escape, node_names))
            pat = rf"({alt})[^。\n]{{0,14}}?{re.escape(rel)}[^。\n]{{0,14}}?({alt})"
            for m in re.finditer(pat, text):
                s, t = m.group(1), m.group(2)
                if s == t:
                    continue
                key = (s, rel, t)
                if key in seen_edges:
                    continue
                seen_edges.add(key)
                edges.append({
                    "source_name": s,          # 头实体（关联候选实体）
                    "target_name": t,          # 尾实体（关联候选实体）
                    "relation_type": rel,
                    "confidence": 0.5,
                    "props": {"auto": True, "extractor": "rule",
                              "source_frag": m.group(0)[:120]},
                })
    return {"nodes": nodes, "edges": edges}


# AI 建模候选入库的目标分支（v2g/confirm 链路的唯一写入分支）。
# 2026-10-11 提取为常量：此前 4 处硬编码 "personal"（create_edge / create_node /
# UPDATE sysml_version_id / 版本打点），而 `_disambiguate_many` 的比对口径
# 必须与写入分支一致，否则 `dup_action='align'` 会把候选合到别的分支的实体上
# （`entities` 主键是复合键 (id, branch)，真库有 61 组同 id 跨 dev/personal/release）。
_AI_INGEST_BRANCH = "personal"

# ── 消歧三维度阈值（P1-1/P1-2，2026-10-11）────────────────────────────────
# 口径与 entity_resolver.detect_and_save_candidates 同源（不重造第二轮算法），
# 但**不含图邻域维度** —— 那是写后消歧路径才有的信息。
#
# ⚠️ floor 的意义（抄 Talend T-Swoosh「每个 matching function 各自过阈值」）：
#   Neo4j 官方警告「score cutoffs ≠ precision」，Senzing 亦强调要区分
#   「相似」与「同一实体」。若无 per-dimension floor，则
#   「转发器」 vs 「有效载荷转发器」这类**层级关系**会因字符高度重叠被误判为重复。
#
# 实测标定（VectorEngine bigram TF 余弦 / fuzzy_score）：
#   转发器 vs 转发器           vec=1.0000  fuzzy=1.0000   ⇒ 同一实体
#   转发器 vs 有效载荷转发器    vec=0.5774  fuzzy=0.6667   ⇒ 层级关系（部件⊂分系统）
#   转发器 vs 宽带载荷          vec=0.0000  fuzzy=0.0000   ⇒ 无关
#   宽带载荷 vs 宽带载荷分系统   vec=0.7071  fuzzy=0.7500   ⇒ 层级关系（**vec 反而更高！**）
#   转发器A vs 转发器B          vec=0.6667  fuzzy=0.6667   ⇒ 真重复（同位置字符替换）
#
# ⚠️ 由第 3、4 行可推出**关键结论**：层级关系的 vec 与真重复**同量级**（0.71 vs 0.67），
#   单靠全局阈值无法区分 ⇒ related 判定必须用**前缀+聚合词后缀**信号（见 _disambiguate_many 内）。
#   floor 的作用是**兜底**（挡住语义明显不同的假包含），不是 related 的主判据。
_FLOOR_VEC = 0.60        # 向量维度下限（兜底）
_FLOOR_FUZZY = 0.60     # 模糊维度下限（兜底）
_SUSPECT_MIN = 0.72     # 三维加权分达到此值且过 floor ⇒ dup_suspect

# ──「名称包含」的统一判定（P4，2026-10-11）────────────────────────────────
# ⚠️ 单一实现：`_disambiguate_many`（AI 建模主链路）与
#    `sysml_importer._score_pair`（归一校验批内）**共用本函数与 `_AGG_SUFFIX`**。
#    此前两边各存一份拷贝且列表不一致（那边多 需求/功能/req），属典型「口径分裂」，
#    是最易漏的缺陷形态 —— 单看任一份都正确。统一后由同一份代码产出，不可能再漂移。
_AGG_SUFFIX = ("分系统", "子系统", "系统", "设备", "组件", "部件组", "载荷",
               "平台", "总成", "需求", "功能", "req")


def _containment_status(a: str, b: str, floor_ok: bool) -> str:
    """两名为**包含关系**（其一是另一个的子串）时的状态判定。返回 related / dup_suspect。

    ⚠️ 为什么「包含」不能直接判重复（Neo4j / Senzing 官方立场）：
    名称包含通常是**层级 / 复合术语**而不是同一实体 ——
      「转发器」⊂「有效载荷转发器」      部件 ⊂ 分系统
      「热管理」⊂「EV 热管理系统」       主题 ⊂ 子系统
    Neo4j 官方明确：「母公司 vs 子公司不该因共享品牌而合并」。

    🔴 P4（2026-10-11）第 1 条判据 —— **包含但短名不是长名前缀 ⇒ related**：
    修 `text_normalize._bigrams` 之前，无空格拉丁名的 fuzzy **恒为 0**
    ⇒ floor_ok 恒 False ⇒ 这类恒落 related，问题被假阴性掩盖。
    fuzzy 修复后 floor_ok 变真，`EVThermalManagementSystem ⊃ ThermalManagement`
    会**直接升级为 dup_suspect**（假阳性：层级关系被当重复并建议合并）。
    ⇒ 收紧：只有短名是长名**前缀**时才有可能是同根（同根 + 后缀）；
      中缀 / 后缀包含一律当作复合术语 ⇒ related（不参与合并）。

    判据（顺序敏感）：
      短名不是长名前缀                    ⇒ related（复合术语 / 层级）
      前缀 + 聚合词后缀 + 不过 floor       ⇒ related（层级）
      前缀 + 聚合词后缀 + 过 floor         ⇒ dup_suspect（可能是同一实体被写全称，交人工）
      不过 floor                          ⇒ related（语义不足以判同一实体）
      其余（过 floor）                     ⇒ dup_suspect
    """
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    if not long_.startswith(short):
        return "related"          # P4：中缀/后缀包含 = 复合术语
    tail = long_[len(short):]
    has_agg = bool(tail) and any(s in tail for s in _AGG_SUFFIX)
    if has_agg and not floor_ok:
        return "related"          # 层级关系：部件 ⊂ 分系统
    if has_agg:
        # 前缀 + 聚合词后缀且三维都够：仍可能是「同一部件被写全称」
        # → 判重复但降一档信任（不自动合并，交人工）
        return "dup_suspect"
    return "dup_suspect" if floor_ok else "related"

# 进程级 VectorEngine 单例：避免每个候选重建词表（性能；`_vector` 内部有缓存）
_VE_SINGLETON = None


def _get_vector_engine():
    global _VE_SINGLETON
    if _VE_SINGLETON is None:
        try:
            from knowledge_engine import VectorEngine
            _VE_SINGLETON = VectorEngine()
        except Exception:  # noqa: BLE001  向量不可用时降级为纯 fuzzy（不阻断消歧）
            _VE_SINGLETON = False
    return _VE_SINGLETON or None


def _disambiguate_many(conn, names: list, branch: str | None = None,
                       cross_ling: dict | None = None) -> dict:
    """批量候选消歧（P1-2 Blocking 化）：一次全表扫描建索引，消灭 O(N×M)。

    与旧 _disambiguate 单候选语义一致（判定顺序保留）：
    - dup_high：与已有实体名称完全相同（或 normalize_name 后相同）
    - dup_suspect：名称包含/被包含（≥2 字）或字符重叠率 ≥0.8 且长度差 ≤2
    - none：无重复

    ⚠️ P1-1/P1-2/P1-3（2026-10-11）：`dup_suspect` 判定**已从单一字符维度扩为三维度**
    （vec 0.70 + fuzzy 0.30），并加 **per-dimension floor**（各维 ≥0.60）；
    另新增 **`related` 状态**表达「相关但不同实体」（Senzing `Possibly related` 范式）。

    状态集（⚠️ 新增 related，`_NR_BLOCKING_STATUSES` 与前端 `_NR_BLOCKING` 须同步）：
    - dup_high    同名/归一全等/别名互为规范名 ⇒ 高度重复
    - dup_suspect 疑似重复（三维过 floor）
    - related     **相关但不同实体**（典型：部件 ⊂ 分系统，如「转发器」vs「有效载荷转发器」）
    - none        无关联
    related 不参与「重复合并」，供人判定层级关系（Neo4j 官方：母公司 vs 子公司不该因共享品牌而合并）。
    索引三件套（内存 dict，建索引 O(N)，查询 O(M×桶均)）：
    - exact{原名} / norm{归一名} → dup_high 零漏配（O(1) 命中）
    - prefix{normalize_full 前 4 字} → 覆盖「同前缀包含」类 dup_suspect
    - bigram{normalize_full 连续 bigram} → 覆盖「字符重叠 ≥0.8」类 dup_suspect
      （80% 字符相同 + 长度差 ≤2 的两名必然共享 ≥1 个公共 bigram，理论零漏配；
      如 高轨卫星系统 vs 低轨卫星系统 → 公共 bigram「轨卫/卫星/星系/系统」）
    桶内顺序 = DB rows 插入序，与旧全表遍历首命中语义一致。
    复杂度：O(N 建索引 + M×桶均)。返回 {raw_name(strip 后): (status, entity_id)}。

    P0-2 修复（2026-10-11）：SQL 补 branch 过滤。
    根因：`entities` 主键是**复合键 (id, branch)**，真库有 61 组同 id 跨 dev/personal/release
    三分支。此前本查询既无 branch 也无 entity_type 过滤 ⇒ 会把**别的分支的实体**拿来比对，
    而 `confirm_candidates` 入库时硬编码写 `personal` 分支（:978）⇒
    `dup_action='align'` 会把候选属性合到 dev 分支的实体上（跨分支错配）。
    对照：`entity_resolver.detect_and_save_candidates` 已于 2026-10-11 补同口径
    （`_canopy_key(..., branch=)` + `a["branch"] != b["branch"]: continue`），本函数尚未跟上。

    ⚠️ backward-compat：`branch=None`（默认）= 保持旧的全分支扫描语义，
    供既有调用方与测试脚本（`tests/manual_verify/verify_p1_blocking_smoke.py`）零改动沿用；
    传入具体 branch 时才做分支隔离。

    P5 跨语言消歧（2026-10-12）：新增**可选** `cross_ling`（由
    `entity_resolver.load_cross_ling_index(conn)` 产出，形如
    `{规范名normalize: [别名词原文, ...]}`，且**只含跨语言词条**）。
    🔴 本函数此前是**跨语言消歧的断点**：它不读 `_normalize_mentions` 的结果、不查词库，
    而图库实体全是中文、AI 候选全是英文 ⇒ 两侧 bigram 无交集 ⇒ 恒不命中
    （真库实测 60 个英文候选只产出 2 个 `dup_high`）。
    修法只在**索引侧**灌入别名词（查询侧与全部判定逻辑**一行未改**）：
      - 别名进 `exact` ⇒ 候选 `Transponder` 与别名 normalize_full 全等
        ⇒ 走既有 ②a 分支判 **dup_high**，返回的仍是真实中文实体的 id；
      - 别名进 `norm` / `prefix` / `bigram` ⇒ 复合英文名（`TransponderUnit`）能被召回；
        但相似度仍用**实际名字**计算 ⇒ 跨语言相似度≈0 ⇒ **过不了 floor ⇒ 不会误合并**，
        最坏退化为 `none`/`related`，符合「宁可漏配不误合」。
    ⚠️ 桶内存的是**别名词**（用于全等比对），不是实体真名 —— 这是上面第一条能生效的前提。
    """
    out = {str(n or "").strip(): ("none", "") for n in names if n}
    pending = [k for k in out if k]
    if not pending:
        return out
    from text_normalize import _bigrams_ordered, normalize_full, normalize_name
    from text_normalize import fuzzy_score   # P1-1：三维度之一
    _ve = _get_vector_engine()
    _vcache = {}                              # name → vector
    def _ve_cos(a: str, b: str) -> float:
        """向量余弦（带 name 级缓存）；向量引擎不可用时返回 0.0（降级为纯 fuzzy）。"""
        if not _ve:
            return 0.0
        if a not in _vcache:
            _vcache[a] = _ve._vector(a)
        if b not in _vcache:
            _vcache[b] = _ve._vector(b)
        try:
            return float(_ve._cosine(_vcache[a], _vcache[b]))
        except Exception:  # noqa: BLE001
            return 0.0
    exact, norm, prefix_idx, bigram_idx = {}, {}, {}, {}

    def _index_one(raw: str, eid):
        """把一个名字灌进四套索引（P5 抽出，供实体真名与跨语言别名共用）。"""
        other = str(raw).strip()
        if not other:
            return
        exact.setdefault(other, eid)
        no = normalize_name(other)
        if no:
            norm.setdefault(no, eid)
        nfo = normalize_full(other)
        if nfo:
            prefix_idx.setdefault(nfo[:4], []).append((other, eid, nfo))
            for bg in _bigrams_ordered(nfo):
                bigram_idx.setdefault(bg, []).append((other, eid, nfo))

    # P0-2：branch 有值时按分支隔离（entities 主键是 (id, branch)）
    if branch:
        _rows = conn.execute(
            "SELECT id, name FROM entities WHERE status!='deprecated' AND branch=?",
            (branch,)).fetchall()
    else:
        _rows = conn.execute(
            "SELECT id, name FROM entities WHERE status!='deprecated'").fetchall()
    for r in _rows:
        _index_one(r["name"], r["id"])
        # P5：跨语言别名入索引（值是别名词原文，走 normalize_full 建桶）
        if cross_ling:
            _own = normalize_name(str(r["name"]).strip())
            for _alt in cross_ling.get(_own, ()) or ():
                if normalize_name(str(_alt).strip()) != _own:
                    _index_one(_alt, r["id"])
    for name in pending:
        # ① 精确/归一全等 → dup_high（旧逻辑第一优先，零漏配）
        eid = exact.get(name)
        if eid is None:
            nn = normalize_name(name)
            if nn:
                eid = norm.get(nn)
        if eid:
            out[name] = ("dup_high", eid)
            continue
        # ② 包含/重叠 → dup_suspect：prefix ∪ bigram 桶并集（特征序 first-seen）
        nf = normalize_full(name) or ""
        probe = ([nf[:4]] if nf else []) + _bigrams_ordered(nf)
        cand_items, seen_oids = [], set()
        for k in probe:
            for other, eid, nfo_i in prefix_idx.get(k, []):
                if eid not in seen_oids:
                    seen_oids.add(eid)
                    cand_items.append((other, eid, nfo_i))
            for other, eid, nfo_i in bigram_idx.get(k, []):
                if eid not in seen_oids:
                    seen_oids.add(eid)
                    cand_items.append((other, eid, nfo_i))
        for other, eid, nfo_i in cand_items:
            # ②a 别名展开后规范名全等 → dup_high（中英术语互为对方规范名，判为同一实体）
            if nf and nfo_i and nf == nfo_i:
                out[name] = ("dup_high", eid)
                break
            # ── P1-1 / P1-2 / P1-3（2026-10-11）：三维度打分 + per-dimension floor
            #       + related 语义 ─────────────────────────────────────
            # 原实现只有「字符重叠率」单一维度，却直接判 dup_suspect（会被当重复合并）。
            # 问题（MBSE 场景实测）：`name in other` 会把**层级关系**误判为重复 ——
            #   「转发器」 vs 「有效载荷转发器」是部件 ⊂ 分系统，是 related 不是同一实体；
            #   「卫星」 vs 「主星」同理。Neo4j 官方亦警告「母公司 vs 子公司不该因共享品牌而合并」。
            # 三维度（与 entity_resolver.detect_and_save_candidates 同源实现，不重造）：
            #   vec  = VectorEngine bigram TF 余弦（0.70）
            #   fuzzy= text_normalize.fuzzy_score（0.30）
            # P1-2 per-dimension floor（抄 Talend T-Swoosh「每个 matching function 各自过阈值」）：
            #   全局分高但**某一维度不及格** ⇒ 不算匹配，防「字符高度重叠掩盖语义不同」。
            _v_sim = 0.0
            if name and other:
                try:
                    _v_sim = float(_ve_cos(name, other))
                except Exception:  # noqa: BLE001
                    _v_sim = 0.0
            _f_sim = float(fuzzy_score(name, other)) if (name and other) else 0.0
            _score = round(0.70 * _v_sim + 0.30 * _f_sim, 4)
            _floor_ok = _v_sim >= _FLOOR_VEC and _f_sim >= _FLOOR_FUZZY

            # P1-3：包含关系 → 区分「层级关系(related)」与「重复(dup)」
            #   ⚠️ **不能用全局阈值区分**（2026-10-11 实测标定）：
            #       「宽带载荷 ⊂ 宽带载荷分系统」vec=0.7071（层级）
            #       「转发器A vs 转发器B」  vec=0.6667（真重复）
            #     两者同量级，单一 floor 阈值**必然误判其一**。
            #   可靠信号是**前缀关系**：层级关系里短名通常是长名的词干/前缀
            #     （「宽带载荷」→「宽带载荷分系统」前缀命中，剩余为「分系统」这类聚合词后缀）
            #   而真重复是「同位置字符替换」（转发器**A** → 转发器**B**，前缀不成立）。
            #   Neo4j 官方同此立场：母公司 vs 子公司不该因共享品牌而合并，应建模为 related。
            if len(name) >= 2 and len(other) >= 2 and (name in other or other in name):
                # P4（2026-10-11）：包含判定统一由 `_containment_status` 产出
                # （详见该函数注释：为何中缀包含必须是 related 而非重复）
                out[name] = (_containment_status(name, other, _floor_ok), eid)
                break
            # 字符重叠率路径：加 floor 门 + 向量补强（原有口径保留为下限）
            if name and other:
                s1, s2 = set(name), set(other)
                inter = len(s1 & s2)
                m = max(len(s1), len(s2))
                if m and inter / m >= 0.8 and inter / m < 1.0 \
                        and abs(len(name) - len(other)) <= 2:
                    # P1-2：字符重叠高但向量/模糊任一不及格 ⇒ 降级为 related，不判重复
                    if not _floor_ok:
                        out[name] = ("related", eid)
                    else:
                        out[name] = ("dup_suspect", eid)
                    break
                # P1-1 补强：原实现完全没有向量维度 ⇒ 漏配。
                # 现在对未命中前两条规则的候选补一次三维判定，够 floor 才算 dup_suspect。
                if _floor_ok and _score >= _SUSPECT_MIN:
                    out[name] = ("dup_suspect", eid)
                    break
    return out


def _disambiguate(conn, name: str) -> tuple:
    """单候选消歧（兼容接口）：包装 _disambiguate_many，语义与旧实现一致。"""
    if not name:
        return "none", ""
    key = str(name).strip()
    return _disambiguate_many(conn, [key]).get(key, ("none", ""))


def _disambiguate_rel(conn, src_name: str, tgt_name: str, rel_type: str) -> tuple:
    """P0-A 关系候选消歧：与已有图谱关系（非 deprecated）比对同三元组。

    返回 (rel_matching_status, rel_match_rel_id)：
    - dup_high：已有完全相同的 (source_id, relation_type, target_id) 边（高度重复，应合并或跳过）
    - none：无重复（含源/目标实体尚未入库，无法匹配）
    """
    if not (src_name and tgt_name and rel_type):
        return "none", ""
    def _eid(name: str):
        row = conn.execute(
            "SELECT id FROM entities WHERE name=? AND status!='deprecated' LIMIT 1",
            (str(name).strip(),)).fetchone()
        return row["id"] if row else ""
    s, t = _eid(src_name), _eid(tgt_name)
    if not (s and t):
        return "none", ""
    row = conn.execute(
        "SELECT id FROM relations WHERE source_id=? AND relation_type=? AND target_id=? "
        "AND status!='deprecated' LIMIT 1",
        (s, str(rel_type).strip(), t)).fetchone()
    if row:
        return "dup_high", str(row["id"])
    return "none", ""


def _resolve_coref(conn, text: str) -> str:
    """P1-1 指代消解（抽取前预处理）：代词/回指短语 → 上下文最近实体名。

    规则兜底（确定性，可回归）：
    - 按段落扫描已出现的实体名（本体类型词 + glossary 术语），记录提及位置
    - 将回指短语（该公司/该系统/该模型/上述机构/前述协议/其…）替换为
      该短语之前最近一次出现的实体名
    - 仅用于抽取输入（消除"（他，任职，X）"类无效三元组），不改原文溯源
    """
    if not text or len(text.strip()) < 6:
        return text
    ov = OntologyValidator(conn)
    names = list(ov.entity_types())
    try:
        names += [r["canonical_term"] for r in conn.execute(
            "SELECT canonical_term FROM glossary WHERE active=1").fetchall()]
    except Exception as e:
        logger.warning("glossary 术语加载失败，指代消解降级: %s", e)
    names = sorted({n for n in names if n and len(n) >= 2}, key=len, reverse=True)
    if not names:
        return text
    # 回指短语（其 仅匹配独立出现/后接业务词，避免误伤"其它/其他"）
    coref_patterns = [r"该公司", r"该系统", r"该模型", r"该方案", r"上述机构",
                      r"前述协议", r"该设备", r"该平台", r"该卫星", r"该载荷",
                      r"其(?=[，。；、\s]|性能|功能|指标|带宽|功率|质量|寿命|工作|设计|可靠性|寿命|状态)"]
    out = []
    for para in re.split(r"(\n+)", text):
        if len(para.strip()) < 6 or not para.strip():
            out.append(para)
            continue
        mentions = []
        for nm in names:
            for m in re.finditer(re.escape(nm), para):
                mentions.append((m.start(), nm))
        mentions.sort()
        reps = []
        for pat in coref_patterns:
            for m in re.finditer(pat, para):
                prev = [x for x in mentions if x[0] < m.start()]
                if not prev or prev[-1][1] in m.group(0):
                    continue
                reps.append((m.start(), m.end(), prev[-1][1]))
        if not reps:
            out.append(para)
            continue
        reps.sort(reverse=True)  # 从右到左替换，左侧位置不漂移
        for s, e, nm in reps:
            para = para[:s] + nm + para[e:]
        out.append(para)
    return "".join(out)


def _dedup_candidates(nodes: list, edges: list, norms: dict) -> tuple:
    """P0-3 重叠/跨 chunk 去重（Hash + 语义相似度双层，v2.0 §2.2）：

    - 节点候选：Hash 键 (normalized_name, entity_type) 去重，保留最高置信度；
      同分取短名（更接近规范名）；落选者的原提及并入 properties.source_mentions 溯源
    - 关系候选：Hash 键 (src_norm, relation_type, tgt_norm) 去重；
      Hash 不同但两端名称语义相似（fuzzy_score>0.95）判重（Embedding 的确定性降级）
    """
    from text_normalize import fuzzy_score
    idx, out_n = {}, []  # key -> out_n 下标（后到更优时原位替换，保证输出与 seen 一致）
    for n in nodes:
        raw = n.get("name") or ""
        key = (norms.get(raw, raw), n.get("entity_type", ""))
        conf = float(n.get("confidence") or 0)
        if key in idx:
            i = idx[key]
            prev = out_n[i]
            prev_conf = float(prev.get("confidence") or 0)
            if conf > prev_conf or (conf == prev_conf and len(raw) < len(prev.get("name") or "")):
                winner, loser, out_n[i] = n, prev, n
            else:
                winner, loser = prev, n
            # 溯源：落选原提及并入胜者的 source_mentions
            props = dict(winner.get("properties") or {})
            mentions = list(props.get("source_mentions") or [])
            if loser.get("name") and loser["name"] != winner.get("name") and loser["name"] not in mentions:
                mentions.append(loser["name"])
            props["source_mentions"] = mentions
            winner["properties"] = props
            continue
        idx[key] = len(out_n)
        out_n.append(n)
    seen_e, out_e = [], []
    for e in edges:
        s = norms.get(e.get("source_name", ""), e.get("source_name", ""))
        t = norms.get(e.get("target_name", ""), e.get("target_name", ""))
        key = (s, e.get("relation_type", ""), t)
        if not (s and t and key[1]):
            continue
        dup = False
        for k2 in seen_e:
            if (key[1] == k2[1] and fuzzy_score(key[0], k2[0]) > 0.95
                    and fuzzy_score(key[2], k2[2]) > 0.95):
                dup = True
                break
        if dup:
            continue
        seen_e.append(key)
        out_e.append(e)
    return out_n, out_e


def _entity_candidate_source(conn) -> str:
    """实体候选来源（settings.entity_candidate_source，默认 sysml）。

    sysml = 以 AI 建模 SysML 代码提交入库的模型元素为候选词表做确定性匹配；
    llm   = LLM 按本体 schema 自由抽取（Mock 兜底）。
    """
    row = conn.execute("SELECT value FROM settings WHERE key='entity_candidate_source'").fetchone()
    return (row["value"] if row and row["value"] else "sysml") or "sysml"


def _extract_sysml_candidates(conn, texts: list, relation_types: list) -> dict:
    """候选来源=sysml：以 AI 建模 SysML 提交入库的模型元素为候选词表，在文档中做
    确定性词表匹配生成实体/关系候选（实体候选默认来源，**不降级 LLM 自由抽取**）。

    - 候选词典：entities WHERE graph_source='sysml_import' AND status!='deprecated'
      （AI 建模 SysML 代码提交入库的元素：实体名 + 类型 + 属性）
    - 实体候选：文档命中 SysML 实体名 → 候选（confidence 0.9，随候选携带
      sysml_entity_id，落库时直接消歧打标 dup_high 关联源模型实体——确认入库与
      模型元素融合而非新建，保证文件抽取与 SysML 建模数据同源不冲突）
    - 关系候选：优先复用 SysML 模型已有关系（两端实体均被文档命中）；无命中时
      同 chunk 命中 ≥2 实体 → 本体关系类型首项兜底
    - 属性：SysML 实体 properties.attributes 并入候选 properties（供审核参考）
    返回 {nodes, edges}；无 SysML 数据或全未命中 → 空结构（不调 LLM，闭环无降级）。
    """
    rows = conn.execute(
        "SELECT id, name, entity_type, properties FROM entities "
        "WHERE graph_source='sysml_import' AND status!='deprecated' AND name != ''").fetchall()
    if not rows:
        return {"nodes": [], "edges": []}
    vocab = []
    seen = set()
    for r in rows:
        nm = str(r["name"]).strip()
        if not nm or nm in seen:
            continue
        seen.add(nm)
        vocab.append({"id": r["id"], "name": nm, "entity_type": r["entity_type"],
                      "properties": json.loads(r["properties"] or "{}")})
    # 长名优先匹配，避免短名吞长名（如「卫星」吞「卫星系统」）
    vocab.sort(key=lambda v: len(v["name"]), reverse=True)

    # 1) 实体候选：全词匹配（英文/数字按词边界，中文直接包含）
    joined = "\n".join(texts)
    nodes, node_names, node_ids = [], [], []
    used = set()
    for v in vocab:
        if v["id"] in used:
            continue
        pat = r"(?<![A-Za-z0-9])" + re.escape(v["name"]) + r"(?![A-Za-z0-9])"
        if re.search(pat, joined):
            props = {"matched_sysml_entity": v["name"]}
            attrs = v["properties"].get("attributes")
            if isinstance(attrs, dict) and attrs:
                props["attributes"] = attrs
            nodes.append({
                "name": v["name"], "entity_type": v["entity_type"],
                "confidence": 0.9, "sysml_entity_id": v["id"], "properties": props,
            })
            used.add(v["id"])
            node_names.append(v["name"])
            node_ids.append(v["id"])
    if not nodes:
        return {"nodes": [], "edges": []}

    # 2) 关系候选：优先复用 SysML 模型已有关系（两端实体均被文档命中）
    edges, seen_edges = [], set()
    if len(node_ids) >= 2:
        rel_rows = conn.execute(
            "SELECT r.relation_type, e1.name AS s, e2.name AS t FROM relations r "
            "JOIN entities e1 ON e1.id=r.source_id AND e1.branch=r.branch "
            "JOIN entities e2 ON e2.id=r.target_id AND e2.branch=r.branch "
            "WHERE r.status!='deprecated' AND r.relation_type != '' "
            "  AND e1.graph_source='sysml_import' AND e2.graph_source='sysml_import'").fetchall()
        for rr in rel_rows:
            s, t, rt = rr["s"], rr["t"], rr["relation_type"]
            if s in node_names and t in node_names:
                key = (s, rt, t)
                if key not in seen_edges:
                    seen_edges.add(key)
                    edges.append({"source_name": s, "target_name": t, "relation_type": rt,
                                  "confidence": 0.85, "props": {"auto": True, "from_sysml": True}})
        # 兜底：无既有 SysML 关系时，同 chunk 命中 ≥2 实体 → 本体关系类型首项
        if not edges and relation_types:
            for t in texts:
                hit = [n for n in node_names if n in t]
                if len(hit) >= 2:
                    for i in range(len(hit) - 1):
                        if len(edges) >= 20:
                            break
                        edges.append({"source_name": hit[i], "target_name": hit[i + 1],
                                      "relation_type": relation_types[0], "confidence": 0.85,
                                      "props": {"auto": True}})
    return {"nodes": nodes, "edges": edges}


def extract_candidates(conn, query: str, chunk_ids: list | None = None,
                       top_k: int = 5, batch_id: str | None = None,
                       doc_id: int | None = None,
                       candidate_source: str | None = None) -> dict:
    """O-1 Step1：基于查询命中 chunks 抽取候选实体/关系。

    - candidate_source（None=读 settings.entity_candidate_source，默认 sysml）：
      sysml = 以 AI 建模 SysML 入库元素为词表做确定性匹配（不降级 LLM）；
      llm   = LLM 按本体 schema 自由抽取（Mock 确定性兜底）
    - doc_id：限定在指定文档的 chunks 内抽取（上传后自动抽取 → 溯源指向本文档）
    返回 {batch_id, candidates, node_count, edge_count, rejected:[...]}
    """
    # 1) 取命中 chunks（未指定则用混合检索；doc_id 指定时限定该文档）
    if not chunk_ids:
        if doc_id:
            # 上传后抽取：只用该文档自己的 chunks（按 query 向量相似度排序）
            rows = conn.execute(
                "SELECT id, content, source_doc FROM document_chunks WHERE document_id=? AND content != ''",
                (doc_id,)).fetchall()
            from knowledge_engine import VectorEngine
            ve = VectorEngine()
            qv = ve._vector(query)
            scored = [(ve._cosine(qv, ve._vector(r["content"])), dict(r)) for r in rows]
            scored.sort(key=lambda x: x[0], reverse=True)
            chunk_ids = [s[1]["id"] for s in scored[:top_k] if s[0] > 0]
            if not chunk_ids:  # 向量全 0 时兜底取前几块
                chunk_ids = [r["id"] for r in rows[:top_k]]
        else:
            try:
                from knowledge_engine import hybrid_search
                res = hybrid_search(conn, query, top_k=top_k)
                # hybrid_search 返回不带 chunk_id，按 document_id+content 回查 chunk id
                chunk_ids = []
                for h in res["hits"]:
                    row = conn.execute(
                        "SELECT id FROM document_chunks WHERE document_id=? AND content=? LIMIT 1",
                        (h.get("document_id"), h.get("content", ""))).fetchone()
                    if row:
                        chunk_ids.append(row["id"])
            except Exception as e:
                logger.warning("hybrid_search 取 chunk 失败，走全文兜底: %s", e)
                chunk_ids = []
    if not chunk_ids:
        # 直接按 query 相似度取 chunks
        rows = conn.execute("SELECT id, content, source_doc FROM document_chunks WHERE content != '' LIMIT 200").fetchall()
        from knowledge_engine import VectorEngine
        ve = VectorEngine()
        qv = ve._vector(query)
        scored = [(ve._cosine(qv, ve._vector(r["content"])), dict(r)) for r in rows]
        scored.sort(key=lambda x: x[0], reverse=True)
        chunk_ids = [s[1]["id"] for s in scored[:top_k] if s[0] > 0]

    texts, docs = [], {}
    for cid in chunk_ids[:top_k]:
        row = conn.execute("SELECT * FROM document_chunks WHERE id=?", (cid,)).fetchone()
        if row:
            texts.append(row["content"])
            docs[cid] = {"content": row["content"], "source_doc": row["source_doc"]}

    if not texts:
        return {"batch_id": batch_id or _new_batch_id(), "candidates": [], "node_count": 0,
                "edge_count": 0, "rejected": []}

    # P1-1 指代消解：抽取输入预处理（代词/回指 → 最近实体名），仅影响抽取，不改原文溯源
    texts = [_resolve_coref(conn, t) for t in texts]

    # 2) 本体 schema
    ov = OntologyValidator(conn)
    entity_types = ov.entity_types()
    relation_types = ov.relation_types()
    schema = ov.schema_text()

    # 3) 候选来源分派：sysml（默认）→ AI 建模 SysML 入库元素词表确定性匹配，
    #    不降级 LLM 自由抽取（无命中即空候选，保证与建模数据同源不冲突）；
    #    llm → LLM 按本体 schema 抽取（llm.force_mock 或失败 → Mock 确定性兜底）
    src = candidate_source or _entity_candidate_source(conn)
    if src == "sysml":
        extracted = _extract_sysml_candidates(conn, texts, relation_types)
    else:
        try:
            from core import config as _cfg
            from llm import llm_client
            extracted = None
            if not _cfg.as_bool("llm", "force_mock", False):
                prompt = (
                    f"{schema}\n"
                    "从以下文档片段中抽取知识图谱候选实体与关系，仅输出 JSON："
                    '{"nodes":[{"name","entity_type","properties","confidence"}],"edges":[{"source_name","target_name","relation_type","confidence"}]}\n'
                    "confidence 为 0-1 的抽取置信度（高置信 0.9+，一般 0.7-0.85，低置信 <0.7）。\n"
                    f"片段：\n" + "\n---\n".join(t[:500] for t in texts)
                )
                resp = llm_client.chat([
                    {"role": "system", "content": "你是知识抽取引擎，严格按本体类型抽取，输出合法 JSON。"},
                    {"role": "user", "content": prompt},
                ])
                content = resp["choices"][0]["message"]["content"] or ""
                m = re.search(r"\{[\s\S]*\}", content)
                if m:
                    extracted = json.loads(m.group(0))
        except Exception:
            extracted = None
        if not extracted:
            # ★ 2026-10-23：规则降级抽取 —— 显式标注 extractor="rule" 并降置信度（见 _mock_extract_v2）。
            #   降级本身保留（无 LLM 时仍能给出可复核的候选），但**不得冒充 LLM 抽取结果**：
            #   ① 每条候选 properties 带 extractor="rule"（前端可区分来源）；
            #   ② 置信度 0.45/0.5，落在确认阈值之下 ⇒ 强制人工复核。
            extracted = _mock_extract_v2("\n".join(texts), entity_types, relation_types)
            for _n in (extracted.get("nodes") or []):
                (_n.setdefault("properties", {}) or {}).setdefault("extractor", "rule")
            for _e in (extracted.get("edges") or []):
                (_e.setdefault("props", {}) or {}).setdefault("extractor", "rule")

    nodes = extracted.get("nodes", [])[:20]
    edges = extracted.get("edges", [])[:20]

    # P0-2 表面归一 + P0-3 重叠/跨 chunk 去重（抽取后立即执行，v2.0 融合第一步/§2.2）
    # P1 类型限定归一：传入节点实体类型，glossary 绑定 entity_type 的词条仅对匹配类型折叠
    _type_map = {n.get("name"): (n.get("entity_type") or "") for n in nodes if n.get("name")}
    norms = _normalize_mentions(conn, [n.get("name") for n in nodes if n.get("name")]
                                + [e.get("source_name", "") for e in edges]
                                + [e.get("target_name", "") for e in edges],
                                types=_type_map)
    nodes, edges = _dedup_candidates(nodes, edges, norms)

    # 4) 候选落库 + 本体预校验（提前标 rejected）+ 消歧打标
    batch = batch_id or _new_batch_id()
    candidates = []
    rejected = []
    # P1-2 批量消歧：pending 且非 sysml 直连的名称一次建索引，循环内零全表扫
    disamb = _disambiguate_many(conn, [
        n["name"] for n in nodes if n.get("name") and not n.get("sysml_entity_id")])
    for n in nodes:
        if not n.get("name"):
            continue
        cid = uuid.uuid4().hex[:8]
        props = n.get("properties") or {}
        conf = float(n.get("confidence") or 0.5)
        errs = ov.validate_node(n.get("entity_type", ""), props)
        status = "pending"
        if errs:
            status = "rejected"
            rejected.append({"name": n.get("name"), "errors": errs})
        m_status, m_eid = "none", ""
        if status == "pending":
            if n.get("sysml_entity_id"):
                # 候选来源=sysml：直接关联源模型实体（确认入库与模型元素融合而非新建）
                m_status, m_eid = "dup_high", n["sysml_entity_id"]
            else:
                m_status, m_eid = disamb.get(str(n["name"]).strip(), ("none", ""))
        conn.execute(
            "INSERT INTO v2g_candidates (batch_id, chunk_id, source_doc, entity_name, entity_type, properties, status, errors, confidence, matching_status, match_entity_id, normalized_name) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (batch, chunk_ids[0] if chunk_ids else 0, docs.get(chunk_ids[0], {}).get("source_doc", "") if chunk_ids else "",
             n["name"][:80], n.get("entity_type", ""),
             json.dumps(props, ensure_ascii=False), status, "; ".join(errs),
             conf, m_status, m_eid, norms.get(n["name"], n["name"])))
        candidates.append({"cid": cid, "name": n["name"], "entity_type": n.get("entity_type", ""),
                           "chunk_id": chunk_ids[0] if chunk_ids else 0, "status": status,
                           "confidence": conf, "matching_status": m_status, "match_entity_id": m_eid,
                           "normalized_name": norms.get(n["name"], n["name"])})
    for e in edges:
        # 关系候选：rel_source/rel_target 关联两端实体（对齐三元组标准），
        # entity_name 仅作可读展示，不再承担解析职责
        e_conf = float(e.get("confidence") or 0.6)
        # P0-A 关系消歧打标：与已有图谱同三元组比对（入库前提示重复）
        rm_status, rm_rel_id = _disambiguate_rel(conn, e.get("source_name", ""),
                                                 e.get("target_name", ""), e.get("relation_type", ""))
        conn.execute(
            "INSERT INTO v2g_candidates (batch_id, chunk_id, source_doc, entity_name, entity_type, properties, rel_type, rel_source, rel_target, status, confidence, rel_matching_status, rel_match_rel_id) "
            "VALUES (?,?,?,?,?,?,?,?,?, 'pending', ?, ?, ?)",
            (batch, chunk_ids[0] if chunk_ids else 0, docs.get(chunk_ids[0], {}).get("source_doc", "") if chunk_ids else "",
             f"{e.get('source_name')} --{e.get('relation_type')}-- {e.get('target_name')}"[:80], "关系候选",
             json.dumps(e.get("props") or {}, ensure_ascii=False),
             e.get("relation_type", ""), e.get("source_name", ""), e.get("target_name", ""),
             e_conf, rm_status, rm_rel_id))
        candidates.append({"cid": uuid.uuid4().hex[:8], "name": "关系候选",
                           "entity_type": "关系候选", "chunk_id": chunk_ids[0] if chunk_ids else 0, "status": "pending",
                           "rel_type": e.get("relation_type", ""),
                           "rel_source": e.get("source_name", ""), "rel_target": e.get("target_name", ""),
                           "confidence": e_conf, "matching_status": "none", "match_entity_id": "",
                           "rel_matching_status": rm_status, "rel_match_rel_id": rm_rel_id})
    conn.commit()
    return {"batch_id": batch, "candidates": candidates,
            "node_count": len(nodes), "edge_count": len(edges), "rejected": rejected}


def batch_stats(conn) -> list:
    """抽取批次聚合（抽取治理中心批次卡数据源）。

    每批返回：batch_id、来源文档（去重）、实体/关系候选数、
    pending/confirmed/rejected 计数、最早/最近时间、最近来源文档名。
    """
    rows = conn.execute(
        "SELECT batch_id, COUNT(*) AS total,"
        "  SUM(CASE WHEN entity_type='关系候选' THEN 1 ELSE 0 END) AS rel_n,"
        "  SUM(CASE WHEN entity_type!='关系候选' THEN 1 ELSE 0 END) AS node_n,"
        "  SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending_n,"
        "  SUM(CASE WHEN status='confirmed' THEN 1 ELSE 0 END) AS confirmed_n,"
        "  SUM(CASE WHEN status='rejected' THEN 1 ELSE 0 END) AS rejected_n,"
        "  MAX(created_at) AS last_at, MIN(created_at) AS first_at"
        " FROM v2g_candidates GROUP BY batch_id ORDER BY MAX(created_at) DESC, batch_id DESC LIMIT 200"
    ).fetchall()
    out = []
    for r in rows:
        docs = conn.execute(
            "SELECT DISTINCT source_doc FROM v2g_candidates WHERE batch_id=? AND source_doc!='' LIMIT 3",
            (r["batch_id"],)).fetchall()
        out.append({
            "batch_id": r["batch_id"],
            "total": r["total"], "rel_n": r["rel_n"] or 0, "node_n": r["node_n"] or 0,
            "pending_n": r["pending_n"] or 0, "confirmed_n": r["confirmed_n"] or 0,
            "rejected_n": r["rejected_n"] or 0,
            "first_at": r["first_at"], "last_at": r["last_at"],
            "source_docs": [d["source_doc"] for d in docs],
        })
    return out


def update_candidate(conn, cid: int, name: str, entity_type: str, properties: str) -> dict:
    """编辑单条候选（治理中心：改名称/类型/属性后重新校验，仍为 pending）。"""
    cur = conn.execute(
        "SELECT * FROM v2g_candidates WHERE id=?", (cid,)).fetchone()
    if not cur:
        return {"ok": False, "error": "候选不存在"}
    if cur["status"] != "pending":
        return {"ok": False, "error": "仅待审候选可编辑"}
    props = properties or "{}"
    ov = OntologyValidator(conn)
    errs = ov.validate_node(entity_type, json.loads(props) if props.startswith("{") else {})
    m_status, m_eid = _disambiguate(conn, name)
    norm = _normalize_mentions(conn, [name]).get(name, name)
    conn.execute(
        "UPDATE v2g_candidates SET entity_name=?, entity_type=?, properties=?, errors=?,"
        " matching_status=?, match_entity_id=?, normalized_name=? WHERE id=?",
        (name[:80], entity_type, props, "; ".join(errs), m_status, m_eid, norm, cid))
    conn.commit()
    return {"ok": True, "errors": errs, "matching_status": m_status, "match_entity_id": m_eid,
            "normalized_name": norm}


def list_candidates(conn, batch_id: str | None = None, limit: int = 100,
                    offset: int = 0) -> tuple:
    """待审候选列表（分页）。返回 (items, total)。"""
    where = "WHERE 1=1"
    params = []
    if batch_id:
        where += " AND batch_id=?"
        params.append(batch_id)
    total = conn.execute(f"SELECT COUNT(*) FROM v2g_candidates {where}", params).fetchone()[0]
    q = f"SELECT * FROM v2g_candidates {where} ORDER BY id DESC LIMIT ? OFFSET ?"
    items = [dict(r) for r in conn.execute(q, params + [limit, offset]).fetchall()]
    return items, total


def clear_batch(conn, batch_id: str) -> dict:
    """清理批次（抽取治理中心容量管理）：仅允许无待审候选的批次。

    只删除 v2g_candidates 候选记录（已确认入库的实体/关系与 chunk 溯源不受影响），
    审计留痕。返回 {deleted, skipped}
    """
    pending = conn.execute(
        "SELECT COUNT(*) FROM v2g_candidates WHERE batch_id=? AND status='pending'",
        (batch_id,)).fetchone()[0]
    if pending > 0:
        return {"deleted": 0, "skipped": pending}
    cur = conn.execute(
        "DELETE FROM v2g_candidates WHERE batch_id=?", (batch_id,))
    conn.commit()
    return {"deleted": cur.rowcount, "skipped": 0}


def _triple_only_default(conn) -> bool:
    """读 settings['triple_only_writer']：1 = 三元组唯一图写入源（confirm 只生成待审三元组）；0 = 兼容直写。"""
    try:
        row = conn.execute("SELECT value FROM settings WHERE key='triple_only_writer'").fetchone()
        if row and row["value"]:
            return str(row["value"]).strip() not in ("0", "", "false", "no")
    except Exception:
        pass
    # 默认开启：三元组唯一图写入源（确认入库只生成待审三元组，经审核通过后落图）
    return True


def confirm_candidates(conn, batch_id: str | None = None, selected_ids: list | None = None,
                       operator: str = "知识工程师", dup_action: str = "create",
                       triple_only: bool | None = None) -> dict:
    """O-1 Step2：人工确认候选 → 图谱入库（本体校验）+ chunk↔entity 溯源链接。

    修复：selected_ids 优先按 ids 直接确认（支持跨批次勾选）；
    batch_id 仅作未指定 ids 时按批次确认的过滤条件。
    P0-C 消歧前移：节点候选若与已有实体重复（matching_status != none），
    由确认弹窗选择 dup_action 后再入库：
    - skip  ：跳过重复候选（标记 rejected 留痕，不建实体）
    - align ：对齐合并到已有实体（不建新节点，chunk 溯源指向 match_entity_id）
    - create：强制新建（默认，向后兼容）
    返回 {confirmed, rejected, created_nodes, linked_chunks, aligned, skipped}
    """
    if selected_ids:
        ph = ",".join("?" * len(selected_ids))
        cands = conn.execute(
            f"SELECT * FROM v2g_candidates WHERE id IN ({ph}) AND status='pending'",
            selected_ids).fetchall()
    elif batch_id:
        cands = conn.execute(
            "SELECT * FROM v2g_candidates WHERE batch_id=? AND status='pending'",
            (batch_id,)).fetchall()
    else:
        cands = []
    # ── 全链路优化：V2 生成候选默认候选待审闸门 ──
    # AI 建模（sysml_version_id>0）候选须先经 submit-review 提交审核：
    # 未提交（review_status != 'submitted'）则本次仅标记提交、不写入主图库。
    try:
        _v2_cands = [c for c in cands if (c["sysml_version_id"] or 0) > 0]
        _unsubmitted = [c for c in _v2_cands if (c["review_status"] or "") != "submitted"]
        if _unsubmitted:
            for c in _unsubmitted:
                conn.execute(
                    "UPDATE v2g_candidates SET review_status='submitted', "
                    "review_submitted_at=CURRENT_TIMESTAMP WHERE id=? AND status='pending'",
                    (c["id"],))
            conn.commit()
            logger.info("confirm_candidates: %d 条 V2 候选默认进入待审（未直接入库）",
                        len(_unsubmitted))
            return {"ok": True, "confirming": 0, "pending_review": len(_unsubmitted),
                    "confirmed": 0, "created_nodes": [], "created_edges": [],
                    "linked_chunks": set(), "aligned": [], "skipped": [],
                    "rejected": [], "message":
                        "V2 生成候选已进入待审核队列；请在标注审核/triple 审核完成后再确认入库"}
    except Exception as e:  # noqa: BLE001
        logger.warning("V2 待审门禁跳过（向后兼容）: %s", e)
    # ── 三元组唯一图写入源（入库链重构）：仅生成待审三元组，不直写 entities/relations ──
    linked_chunks = set()  # P0 修复（2026-09-09）：triple_only 分支先初始化，否则 UnboundLocalError 静默退回直写
    if triple_only is None:
        triple_only = _triple_only_default(conn)
    if triple_only:
        try:
            from triple_commit import stage_candidates_as_triples
            _staged = stage_candidates_as_triples(conn, cands, batch_id, operator,
                                                   linked_chunks=linked_chunks)
            # 2026-09-22 修复：stage 后同步候选状态（含 _add_triple 幂等跳过——数据已等效在库）。
            # 原缺陷：triple_only 分支不更新 v2g_candidates.status → 批次 pending_n 永不减、
            # 重复确认重复计数、audit 误报"入库 N"。与旧直写分支的 UPDATE status='confirmed' 对齐。
            # 注意：cands 是 sqlite3.Row 列表，无 .get 方法（曾因 c.get("id") 抛 AttributeError
            # 导致 stage 整体异常退回旧直写链），改用 Row.keys() 判存在。
            _id_keys = cands[0].keys() if cands else []
            _ids = [c["id"] for c in cands if "id" in _id_keys and c["id"] is not None]
            if _ids:
                conn.executemany("UPDATE v2g_candidates SET status='confirmed' WHERE id=?",
                                 [(i,) for i in _ids])
            conn.commit()
            return {"ok": True, "triple_only": True, "staged": _staged,
                    "confirmed": _staged.get("triples", 0), "created_nodes": [],
                    "created_edges": [], "linked_chunks": list(linked_chunks),
                    "aligned": [], "skipped": [], "rejected": [],
                    "message":
                        "候选已生成待审三元组（唯一图写入源），请在「⑤ 三元组统一审核」通过后写入图谱"}
        except ImportError:
            logger.warning("triple_commit 缺失，退回旧直写流程")
        except Exception as _te:  # noqa: BLE001
            logger.warning("triple_only 暂存异常，退回旧直写流程: %s", _te)
    # ── Staging 写前融合闸（诊断 §4.2）：批内 Blocking → 四路相似 → 三阈值分流 ──
    # 在候选真正入库前完成自动合并/人工队列标注；失败仅告警不阻断（向后兼容）。
    try:
        from staging_fuse import fuse_batch, quality_gate
        fuse_batch(conn, batch_id=batch_id, selected_ids=selected_ids, operator=operator)
        qg = quality_gate(conn, batch_id)
        if not qg["pass"]:
            return {"ok": False, "confirmed": 0, "created_nodes": [],
                    "created_edges": [], "linked_chunks": set(), "aligned": [],
                    "skipped": [], "rejected": [], "quality_gate": qg}
    except ImportError:
        pass  # 老环境无融合闸模块 → 直通（向后兼容）
    except Exception as e:  # noqa: BLE001
        logger.warning("staging_fuse 执行异常，跳过融合闸（向后兼容）: %s", e)
    gs = GraphStore(conn)
    created_nodes, created_edges, rejected = [], [], []
    aligned, skipped = [], []
    linked_chunks = set()
    # AI 建模候选标记（任一候选关联 sysml_versions → 来源标注 + 提交消息前缀）
    ai_flag = 0
    if selected_ids:
        ph = ",".join("?" * len(selected_ids))
        ai_flag = conn.execute(
            f"SELECT COUNT(*) FROM v2g_candidates WHERE id IN ({ph}) AND sysml_version_id>0",
            selected_ids).fetchone()[0]
    elif batch_id:
        ai_flag = conn.execute(
            "SELECT COUNT(*) FROM v2g_candidates WHERE batch_id=? AND sysml_version_id>0",
            (batch_id,)).fetchone()[0]
    # 已确认节点名 → id 映射（关系候选入库时找源/目标节点）；
    # P0-2 表面归一兜底：raw 名查不到时按 normalized 名匹配（"马云先生" → "马云"）
    name_to_id = {}
    norm_to_id = {}
    from text_normalize import normalize_name as _norm
    for n in conn.execute(
            "SELECT id, name FROM entities WHERE status!='deprecated'").fetchall():
        name_to_id[n["name"]] = n["id"]
        norm_to_id.setdefault(_norm(n["name"]), n["id"])

    def _lookup(name: str):
        if not name:
            return ""
        if name in name_to_id:
            return name_to_id[name]
        return norm_to_id.get(_norm(name), "")
    for c in cands:
        if c["entity_type"] == "关系候选":
            # P0-A 关系消歧前移：与已有边重复（rel_matching_status=dup_high）按 dup_action 处理
            #（skip 跳过留痕 / align 对齐复用已有边 / create 默认强制新建）
            rms = c["rel_matching_status"] or "none"
            rm_rel_id = c["rel_match_rel_id"] or ""
            if rms != "none" and rm_rel_id:
                if dup_action == "skip":
                    conn.execute(
                        "UPDATE v2g_candidates SET status='rejected', reject_reason=? WHERE id=?",
                        (f"关系消歧跳过：与已有关系 #{rm_rel_id} 重复", c["id"]))
                    skipped.append({"name": c["entity_name"], "relation_id": rm_rel_id})
                    continue
                if dup_action == "align":
                    # 对齐：不建新边（同三元组复用已有边），候选 confirmed 留痕
                    conn.execute("UPDATE v2g_candidates SET status='confirmed' WHERE id=?", (c["id"],))
                    aligned.append({"name": c["entity_name"], "relation_id": rm_rel_id})
                    continue
            # S7修复+增强：关系候选确认入库——优先用 rel_source/rel_target 定位两端实体
            #（对齐三元组标准；旧数据兼容 "--" 拼接解析），两端须已在图谱中
            src_name = c["rel_source"] or ""
            tgt_name = c["rel_target"] or ""
            rel_type = c["rel_type"] or ""
            if not (src_name and tgt_name and rel_type):
                parts = str(c["entity_name"] or "").split("--")
                if len(parts) >= 3:
                    src_name, rel_type, tgt_name = parts[0].strip(), parts[1].strip(), "--".join(parts[2:]).strip()
            # P0 方案 v2 / S2：谓词归一（中英文/异写 → 规范谓词；未命中原样，保守策略）
            rel_type = normalize_predicate(rel_type)
            src_id = _lookup(src_name)
            tgt_id = _lookup(tgt_name)
            if src_id and tgt_id:
                ok, res = gs.create_edge(src_id, tgt_id, rel_type, {"auto": True}, _AI_INGEST_BRANCH,
                                         source_doc=c["source_doc"] or "", created_by=operator)
                if ok:
                    # P0-A：关系候选确认 → relations(candidate) 待审（与实体同级治理，
                    # create_relation 默认 reviewed，此处显式置回 candidate 进关系审核队列）
                    conn.execute("UPDATE relations SET status='candidate' WHERE id=?", (res,))
                    conn.execute("UPDATE v2g_candidates SET status='confirmed' WHERE id=?", (c["id"],))
                    created_edges.append({"id": res, "source": src_name,
                                          "relation": rel_type, "target": tgt_name})
                else:
                    conn.execute("UPDATE v2g_candidates SET status='rejected', errors=? WHERE id=?",
                                 ("; ".join(res), c["id"]))
                    rejected.append({"name": c["entity_name"], "errors": res})
            else:
                conn.execute("UPDATE v2g_candidates SET status='rejected', errors=? WHERE id=?",
                             (f"源/目标节点未入库（需先确认节点候选）：{src_name} -> {tgt_name}", c["id"]))
                rejected.append({"name": c["entity_name"],
                                 "errors": f"源/目标节点未入库（需先确认节点候选）：{src_name} -> {tgt_name}"})
            continue
        # P0-C：节点候选消歧前移——与已有实体重复（dup_high/dup_suspect）按 dup_action 处理
        ms = c["matching_status"] or "none"
        if ms != "none" and c["match_entity_id"]:
            if dup_action == "skip":
                conn.execute(
                    "UPDATE v2g_candidates SET status='rejected', reject_reason=? WHERE id=?",
                    (f"消歧跳过：与已有实体 {c['match_entity_id']} 重复", c["id"]))
                skipped.append({"name": c["entity_name"], "entity_id": c["match_entity_id"]})
                continue
            if dup_action == "align":
                # 对齐合并：不建新节点，chunk 溯源指向已有实体（消歧前置到确认环节）
                # P0 方案 v2 / S1：survivorship 择优——属性补齐 + 别名并入 + 来源挂接（lineage 审计）
                _sv = _survivorship_apply(conn, c["match_entity_id"], c, operator)
                if c["chunk_id"]:
                    row = conn.execute("SELECT linked_entity_ids FROM document_chunks WHERE id=?",
                                       (c["chunk_id"],)).fetchone()
                    linked = json.loads(row["linked_entity_ids"] or "[]") if row else []
                    if c["match_entity_id"] not in linked:
                        linked.append(c["match_entity_id"])
                        conn.execute("UPDATE document_chunks SET linked_entity_ids=? WHERE id=?",
                                     (json.dumps(linked, ensure_ascii=False), c["chunk_id"]))
                    linked_chunks.add(c["chunk_id"])
                conn.execute("UPDATE v2g_candidates SET status='confirmed' WHERE id=?", (c["id"],))
                aligned.append({"name": c["entity_name"], "entity_id": c["match_entity_id"],
                                "survivorship": _sv})
                # 对齐实体加入端点定位表：同批次关系候选按名称定位两端时可命中已对齐实体
                name_to_id[c["entity_name"]] = c["match_entity_id"]
                norm_to_id.setdefault(_norm(c["entity_name"]), c["match_entity_id"])
                continue
        # P0-3 同批同名拦截：图库消歧未命中（matching_status=none）但批内先建了同名实体
        # （parse_json 无去重 / 视图合并遗漏等）→ 按 dup_action 处理，防止同批重复建实体
        _existed_id = name_to_id.get(c["entity_name"]) or norm_to_id.get(_norm(c["entity_name"]), "")
        if _existed_id:
            if dup_action == "skip":
                conn.execute(
                    "UPDATE v2g_candidates SET status='rejected', reject_reason=? WHERE id=?",
                    (f"批内同名跳过：已有实体 {_existed_id}", c["id"]))
                skipped.append({"name": c["entity_name"], "entity_id": _existed_id})
                continue
            if dup_action == "align":
                # P0 方案 v2 / S1：批内同名对齐同样走 survivorship（属性补齐+别名并入）
                _sv = _survivorship_apply(conn, _existed_id, c, operator)
                if c["chunk_id"]:
                    row = conn.execute("SELECT linked_entity_ids FROM document_chunks WHERE id=?",
                                       (c["chunk_id"],)).fetchone()
                    linked = json.loads(row["linked_entity_ids"] or "[]") if row else []
                    if _existed_id not in linked:
                        linked.append(_existed_id)
                        conn.execute("UPDATE document_chunks SET linked_entity_ids=? WHERE id=?",
                                     (json.dumps(linked, ensure_ascii=False), c["chunk_id"]))
                    linked_chunks.add(c["chunk_id"])
                conn.execute("UPDATE v2g_candidates SET status='confirmed' WHERE id=?", (c["id"],))
                aligned.append({"name": c["entity_name"], "entity_id": _existed_id,
                                "survivorship": _sv})
                name_to_id[c["entity_name"]] = _existed_id
                norm_to_id.setdefault(_norm(c["entity_name"]), _existed_id)
                continue
            # dup_action=create → 继续新建（保持原语义，允许同名不同实例）
        props = json.loads(c["properties"] or "{}")
        # P0 方案 v2 / S2：新建实体属性 key 归一（band→频段 等；未命中原样）
        props = {normalize_prop_key(k): v for k, v in props.items()}
        nid = f"V2G-{uuid.uuid4().hex[:8]}"
        # AI 建模候选 → source_type=ai_generated（区分文档抽取 vector）
        # R1 统一闸门：SYSM- 批次（ingest/stage 暂存）无版本关联，按批号前缀判定为 AI 建模来源
        is_ai = (c["sysml_version_id"] or 0) > 0 or str(c["batch_id"] or "").startswith("SYSM-")
        ok, res = gs.create_node(nid, c["entity_name"], c["entity_type"], props,
                                 _AI_INGEST_BRANCH, x=0, y=0,
                                 source_doc=c["source_doc"] or "",
                                 source_type="ai_generated" if is_ai else "vector",
                                 created_by=operator)
        if ok:
            if is_ai:
                conn.execute("UPDATE entities SET sysml_version_id=? WHERE id=? AND branch=?",
                             (c["sysml_version_id"], nid, _AI_INGEST_BRANCH))
            conn.execute("UPDATE v2g_candidates SET status='confirmed' WHERE id=?", (c["id"],))
            created_nodes.append({"id": nid, "name": c["entity_name"], "entity_type": c["entity_type"]})
            name_to_id[c["entity_name"]] = nid
            norm_to_id.setdefault(_norm(c["entity_name"]), nid)
            # chunk↔entity 溯源链接
            if c["chunk_id"]:
                row = conn.execute("SELECT linked_entity_ids FROM document_chunks WHERE id=?",
                                   (c["chunk_id"],)).fetchone()
                linked = json.loads(row["linked_entity_ids"] or "[]") if row else []
                if nid not in linked:
                    linked.append(nid)
                    conn.execute("UPDATE document_chunks SET linked_entity_ids=? WHERE id=?",
                                 (json.dumps(linked, ensure_ascii=False), c["chunk_id"]))
                linked_chunks.add(c["chunk_id"])
        else:
            conn.execute("UPDATE v2g_candidates SET status='rejected', errors=? WHERE id=?",
                         ("; ".join(res), c["id"]))
            rejected.append({"name": c["entity_name"], "errors": res})
    # ── 全链路优化·三元组物化（S-P-O 原子存储，幂等）──
    # 已确认入库的实体/关系同步写入 triples 原子表供统一审核/图谱消费。
    try:
        from triple_store import write_entity_triples as _w_ent, write_relation_triple as _w_rel
        for n in created_nodes:
            _ent = conn.execute(
                "SELECT entity_type, properties, sysml_version_id FROM entities WHERE id=?",
                (n["id"],)).fetchone()
            if not _ent:
                continue
            prop = {}
            try:
                prop = json.loads(_ent["properties"] or "{}") if _ent["properties"] else {}
            except Exception:
                prop = {}
            _w_ent(conn, n["id"], n["name"], prop,
                   source_doc=next((c["source_doc"] for c in cands if c["entity_name"] == n["name"]), ""),
                   source_type="ai_generated", sysml_version_id=_ent["sysml_version_id"] or 0,
                   status="pending", created_by=operator, entity_type=_ent["entity_type"] or "")
        for e in created_edges:
            _w_rel(conn, e["id"], e["source"], e["relation"], e["id"], e["target"],
                   source_type="ai_generated" if ai_flag else "vector",
                   sysml_version_id=0,
                   status="pending", created_by=operator)
        conn.commit()
    except Exception as te:  # noqa: BLE001
        logger.warning("三元组物化异常（不阻断入库）: %s", te)
    # ── Q1：全自动写后消歧（best effort，异常不阻断入库）──
    # 每次确认入库后自动产出去重候选，供融合/三元组统一审核，用户无需手动触发引擎。
    try:
        from entity_resolver import detect_and_save_candidates
        r_detect = detect_and_save_candidates(conn)
        logger.info("confirm_candidates: 写后消歧自动运行 candidates=%s auto_merged=%s",
                    r_detect.get("candidates"), r_detect.get("auto_merged"))
    except ImportError:
        pass
    except Exception as _de:  # noqa: BLE001
        logger.warning("confirm_candidates 写后消歧跳过（向后兼容）: %s", _de)
    # ── 分支版本管理：确认入库提交打点（kind=import，branch=personal）──
    # 失败仅打印、不阻断业务；跟随本函数结尾 conn.commit() 同一事务提交
    try:
        from repositories.commit_repo import CommitRepo
        ent_ids = [n["id"] for n in created_nodes] + [a["entity_id"] for a in aligned if a.get("entity_id")]
        rel_ids = [e["id"] for e in created_edges] + [int(a["relation_id"]) for a in aligned if a.get("relation_id")]
        changes, snap = {}, {}
        if ent_ids:
            changes["entities"] = ent_ids
            ph = ",".join("?" * len(ent_ids))
            snap["entities"] = [dict(r) for r in conn.execute(
                f"SELECT id, name, entity_type, status FROM entities WHERE id IN ({ph}) "
                "GROUP BY id", ent_ids).fetchall()]
        if rel_ids:
            changes["relations"] = rel_ids
            ph = ",".join("?" * len(rel_ids))
            snap["relations"] = [dict(r) for r in conn.execute(
                f"SELECT id, source_id, target_id, relation_type FROM relations WHERE id IN ({ph})",
                rel_ids).fetchall()]
        CommitRepo(conn).create_commit(
            _AI_INGEST_BRANCH, "import",
            (f"AI 建模入库 {len(ent_ids)} 实体/{len(rel_ids)} 关系" if ai_flag
             else f"抽取确认入库 {len(ent_ids)} 实体/{len(rel_ids)} 关系"),
            changes, snap, actor=operator)
        # AI 建模入库 → 版本状态回填（committed + 批次留痕）
        if ai_flag:
            sv_row = conn.execute(
                "SELECT DISTINCT sysml_version_id FROM v2g_candidates "
                "WHERE " + ("id IN (%s)" % ",".join("?" * len(selected_ids)) if selected_ids else "batch_id=? ")
                + "AND sysml_version_id>0",
                (selected_ids if selected_ids else (batch_id or "",))).fetchone()
            if sv_row:
                conn.execute("UPDATE sysml_versions SET status='committed', imported_batch=? WHERE id=?",
                             (batch_id or "", sv_row["sysml_version_id"]))
    except Exception as e:
        print(f"[commit_repo] import 提交打点失败（不阻断业务）: {e}")
    conn.commit()
    return {"confirmed": len(created_nodes) + len(created_edges) + len(aligned),
            "nodes": created_nodes, "edges": created_edges, "rejected": rejected,
            "created_nodes": created_nodes, "linked_chunks": list(linked_chunks),
            "aligned": aligned, "skipped": skipped}


def incremental_ingest(conn, filename: str, file_type: str, content: bytes,
                       metadata: dict | None = None) -> dict:
    """O-1 增量更新：若同名文档已入库（completed），跳过全量重建只补元数据。

    返回 {skipped, doc_id, reason}
    """
    exist = conn.execute(
        "SELECT id FROM documents WHERE filename=? AND parse_status='completed'",
        (filename,)).fetchone()
    if exist:
        # 已存在 → 仅更新元数据（增量语义）
        if metadata:
            from repositories.meta_repo import MetaRepo
            import time as _t
            MetaRepo(conn).update_doc_metadata(
                exist["id"], metadata.get("title", ""), metadata.get("author", ""),
                metadata.get("version", "v1.0"),
                json.dumps(metadata.get("tags", []), ensure_ascii=False), "{}")
        return {"skipped": True, "doc_id": exist["id"], "reason": "文档已入库，跳过全量重建"}
    from knowledge_pipeline import ingest_document
    return ingest_document(conn, filename, file_type, content, metadata=metadata)
