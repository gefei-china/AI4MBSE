"""名称归一化与模糊匹配单一实现层（entity_resolver / vector2graph / staging_fuse 共用）。

设计动机（诊断报告 §4.3 P1-1：解循环依赖）：
归一化/打分/分桶逻辑散落 entity_resolver 与 vector2graph，导致循环依赖——
  - entity_resolver.conflict_detect 反向导入 vector2graph._normalize_mentions（L457）
  - vector2graph 又三处导入 entity_resolver.normalize_name（L95/154/692）+ fuzzy_score（L239）

本模块收敛全部**纯字符串**归一函数（零 DB / 零业务模块依赖）：
  - normalize_name / normalize_full（格式层 → 别名/简全称展开层）
  - fuzzy_score / _bigrams / _token_sort_ratio / _partial_ratio（双 fuzzy 打分）
  - _blocking_key / _canopy_key（Blocking 分桶键，旧/新两代）
  - _ALIAS_TABLE / _SHORT_FULL_TABLE（别名表 / 简全称表种子）
  - strip_honorifics（去敬语，承接 vector2graph._normalize_mentions 的正则）

依赖方向（单向，无环）：entity_resolver → text_normalize，vector2graph → text_normalize。
entity_resolver 对本模块做 re-export 保持既有调用方（staging_fuse / routers / services /
verify 脚本）零改动；vector2graph 的局部导入改指本模块。

行为契约：本文件函数为回归基线（verify_fusion_fixes / verify_staging_migration /
verify_staging_fuse）所依赖，迁移时逐字节保留原语义，仅新增/调整注释。
"""
import re

# ═══════════ 1. 名称归一（格式层） + 字符串 fuzzy ═══════════


def normalize_name(name: str) -> str:
    """名称归一（现有逻辑保留）：全角→半角、去空白/括号尾注、小写。"""
    s = str(name or "").strip()
    s = "".join(chr(ord(c) - 0xFEE0) if 0xFF01 <= ord(c) <= 0xFF5E else c for c in s)
    s = s.lower()
    s = re.sub(r"[\s\-_/（）()\[\]【】]+", "", s)
    s = re.sub(r"(?:改进型|优化型|方案[一二三123]|v\d+|原型|候选)$", "", s)
    return s


def _bigrams(s):
    """中文 bigram 切分（兼容英文空格分词）。"""
    if not s:
        return set()
    # 中文按 bigram，英文按 lowercase 词
    zh = re.findall(r"[\u4e00-\u9fff]+", s)
    bigrams = set()
    for z in zh:
        for i in range(len(z) - 1):
            bigrams.add(z[i:i + 2])
    # 英文/数字按空格分词
    en = re.sub(r"[^\w\s]", " ", s).lower().split()
    bigrams.update(en)
    return bigrams


def _bigrams_ordered(s) -> list:
    """中文 bigram + 英文词，按出现顺序返回（_bigrams 的确定性变体）。

    供 Blocking 索引（_disambiguate_many 的 bigram 桶）使用：
    桶 key 需可复现、遍历需有序，set 版本（_bigrams）无法保证顺序。
    """
    if not s:
        return []
    zh = re.findall(r"[\u4e00-\u9fff]+", s)
    out = []
    for z in zh:
        for i in range(len(z) - 1):
            out.append(z[i:i + 2])
    en = re.sub(r"[^\w\s]", " ", s).lower().split()
    out.extend(en)
    return out


def _token_sort_ratio(a: str, b: str) -> float:
    """自实现 token_sort_ratio（中文友好）：两字符串 bigram 集合 Jaccard。"""
    ba = _bigrams(a)
    bb = _bigrams(b)
    if not ba or not bb:
        return 0.0
    inter = len(ba & bb)
    union = len(ba | bb)
    return inter / union if union > 0 else 0.0


def _partial_ratio(a: str, b: str) -> float:
    """自实现 partial_ratio：较短字符串的 bigram 有多少被较长覆盖。"""
    ba = _bigrams(a)
    bb = _bigrams(b)
    if not ba or not bb:
        return 0.0
    smaller, larger = (ba, bb) if len(ba) <= len(bb) else (bb, ba)
    if not smaller:
        return 0.0
    hit = len(smaller & larger)
    return hit / len(smaller)


def fuzzy_score(a: str, b: str) -> float:
    """双 fuzzy 取最大值（覆盖词序颠倒 + 缩写/短串匹配），归一 0~1。"""
    na = normalize_name(a)
    nb = normalize_name(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    return max(_token_sort_ratio(na, nb), _partial_ratio(na, nb))


# ═══════════ 2. Blocking 分块（旧代：E-1/E-2 桶） ═══════════


def _blocking_key(entity_type: str, name: str) -> str:
    """分块 key：类型 + 名称归一首字符（中文取 Big5 前 2 字）。"""
    nt = normalize_name(name)
    prefix = nt[:3] if len(nt) >= 2 else nt
    return f"#{entity_type}#{prefix}"


# ═══════════ 3. 别名表 / 简全称表 / 全量归一（工序①） ═══════════

# ── 别名表：alias（简称/同义词）→ 规范名。简全称表：缩写 → 全称。
# 可维护：未来可落库本体术语表（glossary）动态加载；先内置种子（迁移自 entity_resolver §8.1）。
#
# 中英术语对照：英文/缩写/中文简称 → 中文规范名。normalize_full 对整串精确匹配别名
# （小写、保留空格），命中后替换为规范名，从而中英文同名归一为同一规范名，
# 供消歧（vector2graph._disambiguate_many 的 normalize_full 全等判定）判为同一实体。
# 规范名取值对齐 ontology/seeds 已入库实体（天线/功率放大器/电源分系统/热控分系统/
# 姿轨控分系统/信关站…），避免与不存在的实体强行合并。
_ALIAS_TABLE = {
    # ── 卫星平台/系统（既有） ──
    "卫星": "卫星系统",
    "satellite": "卫星系统",
    "测控网": "测控系统",
    # ── 载荷 ──
    "载荷": "有效载荷",
    "payload": "有效载荷",
    "通信载荷": "通信载荷",
    "communication payload": "通信载荷",
    "comm payload": "通信载荷",
    # ── 转发器 ──
    "转发器": "转发器",
    "transponder": "转发器",
    # ── 地面/信关站 ──
    "地面站": "地面站",
    "ground station": "地面站",
    "ground segment": "地面站",
    "信关站": "信关站",
    "gateway": "信关站",
    "网关": "信关站",
    # ── 天线 ──
    "天线": "天线",
    "antenna": "天线",
    "相控阵天线": "相控阵天线",
    "phased array antenna": "相控阵天线",
    "array antenna": "相控阵天线",
    # ── 功放 / TWTA ──
    "功放": "功率放大器",
    "放大器": "功率放大器",
    "amplifier": "功率放大器",
    "power amplifier": "功率放大器",
    "twta": "功率放大器",
    "行波管放大器": "功率放大器",
    "travelling wave tube amplifier": "功率放大器",
    # ── 电源分系统 ──
    "电源": "电源分系统",
    "power": "电源分系统",
    "电源分系统": "电源分系统",
    "power subsystem": "电源分系统",
    "power supply": "电源分系统",
    # ── 热控分系统 ──
    "热控": "热控分系统",
    "thermal control": "热控分系统",
    "thermal subsystem": "热控分系统",
    "热控分系统": "热控分系统",
    # ── 姿轨控分系统 ──
    "姿态": "姿态",
    "attitude": "姿态",
    "轨控": "轨控",
    "orbit control": "轨控",
    "姿轨控": "姿轨控分系统",
    "aocs": "姿轨控分系统",
    "attitude and orbit control": "姿轨控分系统",
    "attitude control": "姿轨控分系统",
    "orbit control system": "姿轨控分系统",
    # ── 有效载荷反向别名（既有兼容） ──
    "有效载荷": "有效载荷",
    "satellite payload": "有效载荷",
    "payload subsystem": "有效载荷",
}
_SHORT_FULL_TABLE = {
    "KB": "知识库",
    "kg": "千克",
    "kw": "千瓦",
    "hz": "赫兹",
    "REQ": "需求",
    "req": "需求",
    "SR": "系统需求",
    "FR": "功能需求",
    "NFR": "非功能需求",
}


def normalize_full(name: str) -> str:
    """工序① 全量归一化：大小写/全半角/空格/标点统一 + 别名表 + 简全称表 + 格式规范化。

    与旧 normalize_name 的区别：多一步「空格保留」→ 别名/简全称展开后再去分隔符，
    并对别名表/简全称表做匹配（规范名映射）。
    """
    s = str(name or "").strip()
    s = "".join(chr(ord(c) - 0xFEE0) if 0xFF01 <= ord(c) <= 0xFF5E else c for c in s)
    s = s.lower()
    # 空格统一：连续空白 → 单空格（暂不删除，供英文词匹配）
    s = re.sub(r"\s+", " ", s).strip()
    # 标点统一：中文标点 → 英文标点
    s = s.replace("，", ",").replace("。", ".").replace("；", ";").replace("：", ":")
    s = s.replace("（", "(").replace("）", ")").replace("【", "[").replace("】", "]")
    # 别名表 / 简全称表：精确匹配别名 → 规范名（先小写表 key 比对）
    alias_lookup = {k.lower(): v for k, v in _ALIAS_TABLE.items()}
    if s in alias_lookup:
        s = alias_lookup[s].lower()
    short_lookup = {k.lower(): v for k, v in _SHORT_FULL_TABLE.items()}
    if s in short_lookup:
        s = short_lookup[s].lower()
    # 格式规范化：去括号尾注/序号后缀/版本号
    s = re.sub(r"\([^)]*\)|\[[^\]]*\]", "", s)
    s = re.sub(r"[\s\-_/]+", "", s)
    s = re.sub(r"(?:改进型|优化型|方案[一二三123]|v\d+|原型|候选)$", "", s)
    return s


# ═══════════ 4. Canopy 粗筛键（新代：工序① 规则预处理桶） ═══════════


def _canopy_key(entity_type: str, name: str) -> str:
    """Canopy 分桶 key：类型 + 归一化名称的前 4 个字符（含别名展开后）。

    与旧 _blocking_key 的区别：基于 normalize_full（别名/简全称已展开），
    桶粒度稍大（前 4 字符）减少漏配，桶内两两仍可接受。
    """
    nt = normalize_full(name)
    prefix = nt[:4] if len(nt) >= 3 else nt
    return f"#{entity_type}#{prefix}"


# ═══════════ 5. 去敬语（P0-2 表面归一） ═══════════


def strip_honorifics(name: str) -> str:
    """去除中文敬语/职衔后缀（先生/女士/尊敬的X/高级工程师/工程师）。

    承接 vector2graph._normalize_mentions 的去敬语正则（P0-2 表面归一），
    通常作用于 normalize_name 之后的字符串；无敬语时原样返回。
    """
    s = str(name or "")
    return re.sub(r"(先生|女士|尊敬的?|高级工程师|工程师)$", "", s).strip()


# ═══════════ 6. 谓词归一 + 属性 key 归一（P0 方案 v2 / S2：词典体系统一） ═══════════
# 背景（实库证据）：relations.relation_type 中英并存 CONTAINS(28)/包含(11)、SATISFIES(19)/满足(11)、
# CONNECTS(10)/连接(2)；entities.properties key 异写 band/频段、throughput/数据速率、
# desc/描述、thermal_control_scheme/热控方案。未折叠导致按谓词过滤漏数据、属性融合永不命中。
#
# 设计约定：
# - 静态种子表内置（对齐 SysML V2 标准谓词），可运维扩展走 glossary 表（kind='predicate'/'prop_key'）
#   由调用方（vector2graph 等）动态合并后传入 normalize_predicate 的 extra_lookup 参数；
#   本层保持零 DB 依赖。
# - 保守策略：未命中原样返回，防误折叠。

_PREDICATE_TABLE = {
    # 规范谓词（SysML V2 对齐）→ 简写 key 全部小写匹配
    "contains": "CONTAINS", "包含": "CONTAINS",
    "satisfies": "SATISFIES", "satisfy": "SATISFIES", "满足": "SATISFIES",
    "derives": "DERIVES", "derive": "DERIVES", "派生": "DERIVES",
    "flow_to": "FLOW_TO", "flows_to": "FLOW_TO", "flow": "FLOW_TO", "流向": "FLOW_TO",
    "connects": "CONNECTS", "connect": "CONNECTS", "连接": "CONNECTS",
    "verified_by": "VERIFIED_BY", "verify": "VERIFIED_BY", "verifies": "VERIFIED_BY", "验证": "VERIFIED_BY",
    "trace": "TRACE", "traces": "TRACE", "追溯": "TRACE",
    "depends_on": "DEPENDS_ON", "dependson": "DEPENDS_ON", "depends": "DEPENDS_ON", "依赖": "DEPENDS_ON",
    "allocated_to": "ALLOCATED_TO", "allocate": "ALLOCATED_TO", "allocates": "ALLOCATED_TO", "分配": "ALLOCATED_TO",
    "realizes": "REALIZES", "realize": "REALIZES", "实现": "REALIZES",
    "generalization": "GENERALIZATION", "generalizes": "GENERALIZATION", "泛化": "GENERALIZATION",
    "associated_with": "ASSOCIATED_WITH", "associate": "ASSOCIATED_WITH", "关联": "ASSOCIATED_WITH",
    "conflicts": "CONFLICTS", "conflict": "CONFLICTS", "冲突": "CONFLICTS",
    # 大写规范名自身也归一（幂等：normalize_predicate("CONTAINS")=="CONTAINS"）
}

_PROP_KEY_TABLE = {
    "频段": "频段", "band": "频段",
    "数据速率": "数据速率", "throughput": "数据速率", "datarate": "数据速率",
    "描述": "描述", "desc": "描述", "description": "描述",
    "热控方案": "热控方案", "thermal_control_scheme": "热控方案",
    "工作频率": "工作频率", "operating_frequency": "工作频率",
}


def _fold_ws_punct(s: str) -> str:
    """谓词/属性归一的通用字符串预处理：全角→半角、小写、去空白/分隔符。"""
    t = str(s or "").strip()
    t = "".join(chr(ord(c) - 0xFEE0) if 0xFF01 <= ord(c) <= 0xFF5E else c for c in t)
    t = t.lower()
    t = re.sub(r"[\s\-_/（）()\[\]【】]+", "", t)
    return t


def normalize_predicate(rel_type: str, extra_lookup: dict | None = None) -> str:
    """关系谓词归一：中英文/异写 → 规范谓词（SysML V2 对齐）。

    P0 方案 v2 / S2。保守策略：
    - 命中静态 _PREDICATE_TABLE 或 extra_lookup（调用方从 glossary kind='predicate' 动态加载）→ 返回规范谓词
    - 未命中 → 原样返回（不强行折叠，防误判）
    幂等：规范名再次传入返回自身（extra/静态表以大写规范名为 value，key 折叠小写比对）。
    """
    raw = str(rel_type or "").strip()
    if not raw:
        return ""
    folded = _fold_ws_punct(raw)
    lookup = dict(_PREDICATE_TABLE)
    if extra_lookup:
        for k, v in extra_lookup.items():
            lookup[_fold_ws_punct(k)] = v
    if folded in lookup:
        return lookup[folded]
    # 规范名直通（幂等）：原值本身即某表的 value
    upper = folded.upper()
    if upper in set(lookup.values()):
        return upper
    return raw


def normalize_prop_key(key: str, extra_lookup: dict | None = None) -> str:
    """属性 key 归一：异写/中英文 → 规范 key（保留方多数写法）。

    P0 方案 v2 / S2。保守策略同 normalize_predicate：未命中原样返回。
    """
    raw = str(key or "").strip()
    if not raw:
        return ""
    folded = _fold_ws_punct(raw)
    lookup = dict(_PROP_KEY_TABLE)
    if extra_lookup:
        for k, v in extra_lookup.items():
            lookup[_fold_ws_punct(k)] = v
    if folded in lookup:
        return lookup[folded]
    return raw
