# -*- coding: utf-8 -*-
"""P0-0 第九批④（2026-11-23）：全量重灌时保留手工写入的三元组。

## 冲突

`sync_to_fuseki*` 用 `CLEAR DEFAULT` + 全量 INSERT 重灌镜像。用户经
`POST /api/sparql/update` 写入的三元组也在 DEFAULT 图里 ⇒ 一次自动重灌
（或启动对齐）就把它们无声清掉：派生视图的维护吞掉了用户数据。

## 什么算"手工"

    actual - base 投影 - temporal 投影

但**不能**简单按"非 base"取差：W3C Time 时态三元组的 bnode label 是
Fuseki 内部重命名的（`_:safe_ba` → `_:b318`），按整行文本比对会把它们
全当手工，重插后旧时态图与新时态图并存（真正的数据重复）。
因此按**谓词**识别时态三元组并排除，不按 bnode label 文本。

被手工改写的 base 三元组（如同 IRI 换了 label）无法区分，重灌仍会覆盖 ——
这是"派生视图 vs 手工改派生视图"的固有矛盾，须由使用约定规避：
手工数据请用独立 IRI 命名空间，不要改写投影生成的三元组。

开关 `graph_db.fuseki_preserve_manual` 默认关，旧行为零回归。
"""
from __future__ import annotations

_TIME_NS = "<http://www.w3.org/2006/time#"
_HAS_VALID = "<http://mbse/ontology/hasValidInterval>"


def preserve_enabled() -> bool:
    try:
        from core.config import get as _cfg_get
        return bool(_cfg_get("graph_db", "fuseki_preserve_manual"))
    except Exception:
        return False


def is_temporal(line: str) -> bool:
    """按谓词识别时态投影（bnode label 会被 Fuseki 重命名，不能按文本比）。"""
    return _TIME_NS in line or _HAS_VALID in line


def normalize_lines(ttl: str) -> set:
    """与同步器同一口径：每行一条完整 N-Triples 三元组。"""
    return {ln.strip() for ln in (ttl or "").splitlines() if ln.strip().endswith(" .")}


def collect_manual(cli, base_ttl: str, timeout: float = 30.0) -> set:
    """回读 Fuseki，取出既非 base 也非 temporal 的三元组（即手工写入）。"""
    from services.fuseki_freshness import actual_triples
    actual = actual_triples(cli)
    base = normalize_lines(base_ttl)
    return {ln for ln in actual if ln not in base and not is_temporal(ln)}


def reinsert_manual(cli, manual: set, timeout: float = 30.0) -> int:
    """CLEAR 之后重新插入手工三元组；返回分块数（空集合零网络）。"""
    from services.sparql_sync import _build_insert, _chunk_lines
    calls = 0
    for chunk in _chunk_lines("\n".join(sorted(manual or set()))):
        if chunk.strip():
            cli.update(_build_insert(chunk), timeout=timeout)
            calls += 1
    return calls
