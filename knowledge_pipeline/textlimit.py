# -*- coding: utf-8 -*-
"""文本长度预算：检索命中内容的**唯一**截断实现。

为什么单独抽一个模块（2026-11-01）：
    原实现把 `content[:300]` **硬编码在 4 个地方**
    （`knowledge_engine.hybrid_search` 1 处 + `knowledge_pipeline/search.py` 3 处）。
    实测后果：chunk 实际 p50=523 字、avg=432 字，而检索只返回前 300 字
    ⇒ **71.3%（5122/7179）的命中内容被砍掉一截**，
    模型看到的是"条文只有半句"，于是答"信息不足"或自行补全。

    这是本仓反复出现的「同一语义在多处各写一份常量」老坑 ——
    所以在改的时候**不逐个调数字，而是收敛成一个实现 + 一个配置项**，
    以后只改配置就能全局生效。

配置：`rag.chunk_content_chars`
    - `0`       = 不截断（全部返回；适合小语料/长文精读）
    - `>0`      = 每条命中的最大字符数（防止 5681 字那种异常大块撑爆上下文）
    默认 1200：覆盖绝大多数 chunk（p50=523），同时挡住 p99 以上的异常长块。
"""
from __future__ import annotations

_DEFAULT = 1200
_CACHE: dict = {"v": None}


def limit() -> int:
    """当前生效的内容上限（0 = 不截断）。失败回落默认值，绝不让配置问题阻断检索。"""
    v = _CACHE.get("v")
    if v is not None:
        return v
    try:
        from core import config as _cfg
        n = int(_cfg.get("rag", "chunk_content_chars", _DEFAULT) or 0)
    except Exception:
        n = _DEFAULT
    if n < 0:
        n = 0
    _CACHE["v"] = n
    return n


def reset_cache() -> None:
    """测试/门禁用：清掉缓存以便下次读到新配置。"""
    _CACHE["v"] = None


def clip(text: str) -> str:
    """按配置截断命中内容。`limit()==0` 时原样返回（不截断）。"""
    s = text or ""
    n = limit()
    if n <= 0:
        return s
    return s[:n]
