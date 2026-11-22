# -*- coding: utf-8 -*-
"""P0-0（2026-11-22 第八批）**Fuseki 派生镜像新鲜度判据**。

## 为什么需要
Fuseki 不是数据真源，而是 SQLite 的**投影镜像**，写入不会自动投影。
而两条 SPARQL CQ 只查"非空"，**无论镜像是否落后都 pass** ⇒ 验证器在
用陈旧数据时把"查不到"报成模型问题，评审方无从察觉。

> 与 A9 的 `unknown ≠ failed` 同型：**没验成 / 验的不是最新版本**，
> 都不能当作结论。本模块让"镜像新旧"成为可观测、可自动修复的独立维度。

## 自己的坑（真机实测，勿删）
首版按 TTL 行数算期望值得到 1262、真机 Fuseki 506 ⇒ 看似"落后 60%"；
实为**判据自己错**：RDF 三元组是集合，`entities` 同一 id 跨分支重复行
会生成同一条三元组，Fuseki 只存一份。按**去重**集合算是 506，与 Fuseki
完全一致 ⇒ 真机其实是 fresh。若照首版上线会得到永远 stale 的假判据，
并在每次重启时触发无意义全量重灌。**验证器也会错，先自证判据再下结论。**

## 判据口径（为什么不是"数量精确相等"）
`sync_to_fuseki_with_time()` 会额外写 W3C Time Ontology 三元组，
而 base TTL 只含 ontology + entities + relations。若按精确相等比对，
正常的有时间投影会被误判 stale。因此采用两条下界判据：

1. `actual >= expected_base`（base 三元组一条都不能少；允许 temporal 额外行）；
2. 抽样实体 `ASK { <iri> rdfs:label ?o }` 必须全部命中（数量相同但内容错位也能抓到）。

`expected_base` **复用 `services.sparql_sync.build_*_ttl` 同一 builder**，
不与同步器各写一份计数公式（同一语义两处常量迟早漂移）。

## 四态
- `fresh`  —— 下界满足且抽样命中；
- `stale`  —— 查得到但镜像落后/缺行（可对齐，应重灌）；
- `unknown` —— 查询异常/读不到（**不自动重灌**：没验成先别动，避免盲目 CLEAR DEFAULT）；
- `skipped` —— 开关关闭 / 无 Fuseki（预留，不在本模块产生）。
"""
from __future__ import annotations


def expected_base_count(conn, project_id=None) -> int:
    """SQLite 侧应投影的 base 三元组数（与同步器共用 builder，单一真源）。

    ⚠️ 必须按**集合**去重：RDF 图存三元组集合，`entities` 同一 id 跨分支会重复
    生成同一条三元组，Fuseki 只存一份。首版按行数算得 1262、真机却只有 506，
    差点把 fresh 误判成 stale 并触发无意义全量重灌（真机实测教训）。
    """
    from services.sparql_sync import build_ontology_ttl, build_entities_ttl
    ttl = build_ontology_ttl(conn) + "\n" + build_entities_ttl(conn, project_id)
    return len({ln.strip() for ln in ttl.splitlines() if ln.strip().endswith(" .")})


def sample_entity_ids(conn, limit: int = 8) -> list:
    """抽样实体：按 id 排序取头尾各半，避免只取头部恰好都在镜像里的侥幸。

    DISTINCT 必需：同一 id 跨分支重复会让"头 4 + 尾 4"塌成 4 个甚至 1 个样本。
    """
    ids = [str(r[0]) for r in conn.execute(
        "SELECT DISTINCT id FROM entities WHERE status<>'deprecated' AND is_current=1 "
        "ORDER BY id")]
    if len(ids) <= limit:
        return ids
    half = max(1, limit // 2)
    out, seen = [], set()
    for eid in ids[:half] + ids[-half:]:
        if eid not in seen:
            seen.add(eid)
            out.append(eid)
    return out[:limit]


def _ask_label_query(eid: str) -> str:
    from services.sparql_sync import _ttl_uri, RDFS_LABEL
    return "ASK { %s %s ?o }" % (_ttl_uri(eid), RDFS_LABEL)


def _default_client():
    from services.sparql_sync import FusekiClient
    from core.fuseki_runtime import resolve_endpoint
    ep = resolve_endpoint().rstrip("/")
    base, _, ds = ep.rpartition("/")
    return FusekiClient(base_url=base or ep, dataset=ds or "mbse")


def is_stale(expected: int, actual: int, missing: list) -> bool:
    """陈旧判据（两条下界任一不满足）。单独成函数供门禁做真变异。"""
    return bool(actual < expected or missing)


def error_status(ex: Exception) -> str:
    """回读异常归 `unknown`（"没验成"），绝不降级成 `stale` 去触发自动重灌。"""
    return "unknown"


def check_freshness(conn, cli=None, project_id=None, sample_limit: int = 8) -> dict:
    """回读 Fuseki 并与 SQLite 投影比对。返回四态字典。"""
    out = {"status": "unknown", "expected_base": 0, "actual": 0,
           "sample_size": 0, "missing": [], "reason": ""}
    try:
        expected = expected_base_count(conn, project_id)
        samples = sample_entity_ids(conn, sample_limit)
        client = cli or _default_client()
        res = client.query("SELECT (COUNT(*) AS ?n) WHERE { ?s ?p ?o }")
        actual = int(res["results"]["bindings"][0]["n"]["value"])
        missing = []
        for eid in samples:
            r = client.query(_ask_label_query(eid))
            if not bool(r.get("boolean")):
                missing.append(eid)
    except Exception as ex:
        out["status"] = error_status(ex)
        out["reason"] = "新鲜度回读失败：%s: %s" % (type(ex).__name__, str(ex)[:160])
        return out

    out.update(expected_base=expected, actual=actual,
               sample_size=len(samples), missing=missing)
    if is_stale(expected, actual, missing):
        out["status"] = "stale"
        out["reason"] = ("Fuseki 三元组 %d < SQLite 应投影 %d（镜像落后）" % (actual, expected)
                         if actual < expected else
                         "抽样实体在 Fuseki 缺失：%s" % ", ".join(missing[:6]))
    else:
        out["status"] = "fresh"
        out["reason"] = "base 三元组下界满足，抽样 %d 个实体均命中" % len(samples)
    return out
