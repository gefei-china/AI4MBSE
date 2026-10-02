# -*- coding: utf-8 -*-
"""P1-4 自检：RAG/GraphRAG 检索参数配置化（2026-09-21）。

验证四件事：
[A] core/config.py：DEFAULT_CONFIG["rag"] 默认值 + CONFIG_SCHEMA["rag"] 键类型齐全；
[B] 源码级断言：agent/rag.py / knowledge_engine.py / 35-ctxconfig.js 的消费点真的读 config
    （带命中数断言，防"字符串恰好出现但没接线"）；
[C] 行为验证：进程内 monkeypatch core.config._CONFIG → GraphRAG 阈值 / _graph_confidence 权重
    随配置变化（不落配置文件，零副作用）；
[M] 变异自证：把配置读改回旧硬编码（模拟漏改）→ [B] 必须 FAIL。

裸跑：tools/verify/verify_rag_config.py（用 <repo>\\.venv\\Scripts\\python.exe -X utf8）
"""
import io
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PASS, FAIL = [], []


def check(tag, ok, detail=""):
    (PASS if ok else FAIL).append(tag)
    print(f"{'[PASS]' if ok else '[FAIL]'} {tag}{(' —— ' + detail) if detail and not ok else ''}")


def read(p):
    return (REPO / p).read_bytes().decode("utf-8", "replace")


# ── [A] config.py ─────────────────────────────────────────────
sys.path.insert(0, str(REPO))
from core import config as cfg  # noqa: E402

RAG_DEFAULTS = {
    "route_threshold": 0.75, "top_k": 4, "fallback_top_k": 5, "rrf_k": 60,
    "hyde_enabled": True, "hyde_weight": 0.05,
    "w_coverage": 0.50, "w_relations": 0.30, "w_typing": 0.20,
    "confidence_high": 0.70, "confidence_mid": 0.45,
    # P1-22（2026-10-02）：rerank_enabled 默认由 True 改为 **False** —— 官方评测 A/B 实测
    # 证明两级 LLM 重排「召回零提升（recall@3/@5 开与关完全相同）、ndcg 仅 +0.002，
    # 却慢 7 倍、每轮多 ~2585 tokens 且缓存命中恒 0」。旋钮保留，置 true 可重开。
    "rerank_enabled": False, "rerank_max_candidates": 8,
}
missing = [k for k, v in RAG_DEFAULTS.items() if cfg.DEFAULT_CONFIG.get("rag", {}).get(k) != v]
check("A1 DEFAULT_CONFIG.rag 13 键默认值", not missing, f"缺失/不符: {missing}")

schema_rag = cfg.CONFIG_SCHEMA.get("rag", {})
type_bad = [k for k, t in RAG_DEFAULTS.items()
            if k not in schema_rag
            or schema_rag[k].get("type") != ("bool" if isinstance(t, bool) else ("int" if isinstance(t, int) else "float"))]
check("A2 CONFIG_SCHEMA.rag 键+类型", not type_bad, f"缺失/类型错: {type_bad}")

# ── [B] 源码级消费点（锚定字符串 + 命中数） ─────────────────────
rag_py = read("agent/rag.py")
ke_py = read("knowledge_engine.py")
rr_py = read("services/rag_rerank.py")
js = read("static/js/mods/35-ctxconfig.py".replace(".py", ".js"))

B = [
    ("B1 rag.py 路由阈值读配置", rag_py.count('_cfg.get("rag", "route_threshold", 0.75)') == 1),
    ("B2 rag.py top_k 读配置", rag_py.count('int(_cfg.get("rag", "top_k", 4))') == 1),
    ("B3 rag.py 兜底 top_k 读配置", rag_py.count('int(_cfg.get("rag", "fallback_top_k", 5))') == 1),
    ("B4 rag.py 混合检索传配置 top_k", rag_py.count("top_k=_rag_top_k") == 1),
    ("B5 rag.py 降级路传配置 top_k", rag_py.count("top_k=_rag_fb_top_k") == 1),
    ("B6 rag.py 置信权重读配置", all(f'_cfg.get("rag", "w_{n}"' in rag_py for n in ("coverage", "relations", "typing"))),
    ("B7 rag.py 置信公式用权重变量", "w_cov * hit + w_rel * rels + w_typ * type_match" in rag_py),
    ("B8 ke.py 配置读取块存在", all(s in ke_py for s in (
        '_cfg.get("rag", "rrf_k", 60)', '_cfg.get("rag", "hyde_enabled", True)',
        '_cfg.get("rag", "hyde_weight", 0.05)', '_cfg.get("rag", "confidence_high", 0.70)',
        '_cfg.get("rag", "confidence_mid", 0.45)', '_cfg.get("rag", "rerank_max_candidates", 8)'))),
    ("B9 ke.py HyDE 受开关门控", "if _hyde_on and hyde_cand:" in ke_py),
    ("B10 ke.py RRF 常数传配置", "k=_rrf_k" in ke_py),
    ("B11 ke.py HyDE 权重用配置", "hyde_scores.get(cid, 0.0) * _hyde_w" in ke_py),
    ("B12 ke.py 置信等级用配置分界", '_ref >= _conf_high' in ke_py and '_ref >= _conf_mid' in ke_py),
    ("B13 ke.py 重排候选数传配置", "max_candidates=_rerank_cand" in ke_py),
    ("B14 rerank 开关仍读 rag.rerank_enabled", rr_py.count('_cfg.get("rag", "rerank_enabled", True)') == 1),
    ("B15 前端 rag 分组存在", "id: 'rag'" in js and "检索与 RAG" in js),
    ("B16 前端 13 个 rag 字段", js.count("sec:'rag'") == 13),
    ("B17 前端字段键与 CONFIG_SCHEMA 对齐",
     all(f"key:'rag.{k}'" in js for k in RAG_DEFAULTS)),
]
for tag, ok in B:
    check(tag, ok)

# ── [C] 行为验证（进程内 patch，不落文件） ─────────────────────
try:
    # C1 GraphRAG 路由阈值随配置
    cfg._CONFIG["rag"]["route_threshold"] = 0.9
    from agent.rag import GraphRAG
    g = GraphRAG()
    check("C1 GraphRAG.threshold 随配置(0.9)", abs(g.confidence_threshold - 0.9) < 1e-9,
          f"got {g.confidence_threshold}")
    check("C2 QueryRouter.threshold 同步", abs(g.router.threshold - 0.9) < 1e-9)
    cfg._CONFIG["rag"]["route_threshold"] = 0.75

    # C3 图谱置信权重随配置（构造：5 实体全 typed + 3 关系 → hit=1, rels=1, type_match=1）
    ents = [{"entity_type": f"T{i}", "name": f"e{i}"} for i in range(5)]
    rels = [{"id": i} for i in range(3)]
    base = GraphRAG._graph_confidence(ents, rels)
    check("C3 默认权重=1.0", abs(base - 1.0) < 1e-9, f"got {base}")
    cfg._CONFIG["rag"]["w_relations"] = 1.0
    cfg._CONFIG["rag"]["w_coverage"] = 0.0
    cfg._CONFIG["rag"]["w_typing"] = 0.0
    only_rel = GraphRAG._graph_confidence(ents, rels)
    check("C4 权重改后=1.0(仅关系因子满格)", abs(only_rel - 1.0) < 1e-9, f"got {only_rel}")
    only_rel_half = GraphRAG._graph_confidence(ents, rels[:1])  # rels=1/3，w_relations 仍=1.0
    check("C5 关系因子半格=0.333", abs(only_rel_half - 1 / 3) < 1e-9, f"got {only_rel_half}")
    for k in ("w_coverage", "w_relations", "w_typing"):
        cfg._CONFIG["rag"][k] = RAG_DEFAULTS[k]
    check("C6 权重复原后回到 1.0", abs(GraphRAG._graph_confidence(ents, rels) - 1.0) < 1e-9)
except Exception as e:  # noqa: BLE001
    check("C 行为验证", False, f"异常: {e!r}")

# ── [M] 变异自证：模拟"消费点漏改回硬编码"→ [B] 必须能抓住 ──────
MUT = [
    ("M1 路由阈值漏改", "agent/rag.py", 'float(_cfg.get("rag", "route_threshold", 0.75))', "0.75"),
    ("M2 HyDE 门控漏改", "knowledge_engine.py", "if _hyde_on and hyde_cand:", "if hyde_cand:"),
    ("M3 RRF 常数漏改", "knowledge_engine.py", "k=_rrf_k,", "k=60,"),
    ("M4 置信等级漏改", "knowledge_engine.py", "_ref >= _conf_high", "_ref >= 0.70"),
]
backup = {p: (REPO / p).read_bytes() for _, p, _, _ in MUT}
try:
    cur = dict(backup)  # 累积变异：同文件多处变异逐次叠加，防"后写覆盖前写"
    for _, p, old, new in MUT:
        assert old.encode() in cur[p], f"锚点不存在，变异脚本自身错: {old}"
        cur[p] = cur[p].replace(old.encode(), new.encode(), 1)
        (REPO / p).write_bytes(cur[p])
    saved = len(PASS)
    for tag, _, _, _ in MUT:
        # 变异后重读源码重跑对应断言
        r2 = read("agent/rag.py")
        k2 = read("knowledge_engine.py")
        if "路由阈值" in tag:
            check("M1 caught", '_cfg.get("rag", "route_threshold"' not in r2)
        elif "HyDE 门控" in tag:
            check("M2 caught", "if _hyde_on and hyde_cand:" not in k2)
        elif "RRF" in tag:
            check("M3 caught", "k=_rrf_k" not in k2)
        else:
            check("M4 caught", "_ref >= _conf_high" not in k2)
finally:
    for p, b in backup.items():
        (REPO / p).write_bytes(b)
# 还原后逐字节校验
for p, b in backup.items():
    check(f"M-还原 {p}", (REPO / p).read_bytes() == b)

print(f"\n===== verify_rag_config: {len(PASS)} pass / {len(FAIL)} fail =====")
sys.exit(1 if FAIL else 0)
