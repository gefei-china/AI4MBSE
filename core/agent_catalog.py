"""Agent 流水线角色目录（Agent Catalog）：把散在代码里的阶段清单收敛成**一处读取**。

## 为什么需要（问题：同一份名单写在 4 个地方）

改造前，「哪些 Agent 属于哪个阶段集合」硬编码在 4 处，**新增一个阶段要改 4 个文件**：

| 位置 | 常量 | 语义 |
|---|---|---|
| `agent/pipeline_parts/session.py:70` | `_STANDARD_INTENT_LABELS`（18 个） | 可落库为 `conversations.intent` 的标准标签 |
| `agent/pipeline_parts/stream.py:64` | `_CODE_STAGE_INTENTS`（6 个） | 产码阶段（决定流程名「生成与校验」/「生成汇总」） |
| `agent/intent.py:1350` | 粒度规则提示词里的 8 个阶段名 | 意图分类器要知道"哪些是建模流水线阶段" |
| `skills.py:377` / `registry.py:16` | `_ORCH_AGENTS`（6 个） | 无团队模式时的默认编排候选池 |

这与本仓既有纪律相反（`register_sysml_check_tools.py` 文件头）：
「策略写在 pipeline 里意味着每次调整都要改代码 + 重启；写在 DB 里则**改文本即生效**」。

## 数据来源（能推导的不加列，推导不出来的才加列）

| 集合 | 来源 | 为什么这样取 |
|---|---|---|
| 建模阶段意图 | **`agents.stage_order IS NOT NULL`** | 该列 2026-10 已由 `tools/migrate_v5_pipeline_nodes.py` 建好并填了 8 个阶段，**无需新增** |
| 标准意图标签 | `builtin=1` **或** `stage_order IS NOT NULL` | 两者都代表"这是一个意图标识"；排除主 Agent（如 `智源对接agent`）—— 那是**执行主体**不是意图 |
| 产码阶段 | **`agents.is_code_stage`**（2026-11-04 新增） | **不能**用工具绑定推导：`design` 靠正文输出 ```sysml 代码块产码、未绑 sysml 工具；而 `review` 绑了 validate 却不是产码阶段 |
| 默认编排池 | **`agents.is_orch_default`**（2026-11-04 新增） | **不能**用 `builtin=1 AND role=sub` 推导：会多出 `chat`（兜底问答，不该被派子任务）与 `requirement_quality`，改变既有编排行为 |

## 缓存（为什么不是"每次查库"，也不是"永久常量"）

`session._intent_for_storage` 每轮落库都会调用 —— 原注释明确写了"刻意不查库"（怕高频往返）。
但写死常量又导致"新增内置意图需同步常量"的漂移（`tests/test_intent_explainability.py`
有一条用例专门盯着这个漂移）。

正解是**进程内缓存 + TTL**：一次查询覆盖 N 秒内的所有调用，既没有高频往返，也不用改代码。
Agent 增删改后调 `invalidate()` 立即生效（管理端保存后可主动调；不调最迟 TTL 后生效）。

## 兜底

DB 不可读（新库未迁移 / 表缺列 / 连接失败）⇒ 回落本模块的常量。
**这是本模块唯一的硬编码**，只为"读不到也要能用"，不是策略来源。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional

#: 缓存有效期（秒）。取值权衡：太大 ⇒ 改配置后要等；太小 ⇒ 高频落库路径仍有往返。
#: 60s 与"改完刷新一下页面就看到"的体感匹配。
TTL_SECONDS = 60.0

# ── 兜底常量（**仅** DB 不可读时生效；真实来源是 agents 表）────────────────
#: 建模阶段意图兜底：与 `agents.stage_order` 的 8 个阶段**同源同序**
#: （methodology_resolver → … → change_safety_gate → model_release）。
FALLBACK_STAGE_INTENTS: List[str] = [
    "methodology_resolver",
    "requirement_structuring",
    "architecture_skeleton",
    "view_expansion",
    "model_validation_repair",
    "trace_verification",
    "change_safety_gate",
    "model_release",
]

#: 标准意图标签兜底：内置意图（`builtin=1`）+ 上面 8 个阶段意图。
#: 刻意**不含**主 Agent 名（`智源对接agent` 等）—— 那是执行主体，不是意图
#: （`conversations.intent` 曾因此被污染 40 行）。
FALLBACK_STANDARD_INTENTS = frozenset({
    "chat", "design", "impact", "review", "report_generation", "requirement_analysis",
    "requirement_quality", "knowledge_qa", "system_mgmt", "zhiyuan_mgmt",
}) | frozenset(FALLBACK_STAGE_INTENTS)

#: 产码阶段兜底：**只含真实存在的 4 个**。
#: ⚠️ 原 `stream._CODE_STAGE_INTENTS` 有 6 个，其中 `multi_design` 与
#: `sysml_view_generation` 在 `agents` 表**根本不存在**（实测 0 行，幽灵名）。
#: 兜底值**不复制幽灵名** —— 那等于把已发现的错误固化下来。
FALLBACK_CODE_STAGES = frozenset({
    "design", "architecture_skeleton", "view_expansion", "model_validation_repair",
})

#: 默认编排池兜底：与原 `_ORCH_AGENTS` 完全一致（保证改造前后行为逐字节相同）。
FALLBACK_ORCH_DEFAULT: List[str] = [
    "requirement_analysis", "design", "impact", "review",
    "report_generation", "knowledge_qa",
]

_LOCK = threading.Lock()
_CACHE: Dict[str, Any] = {"ts": 0.0, "data": None}


def invalidate() -> None:
    """清缓存。Agent 增删改后调用可立即生效（不调用则最迟 `TTL_SECONDS` 后生效）。"""
    with _LOCK:
        _CACHE["ts"] = 0.0
        _CACHE["data"] = None


def _query(conn=None) -> Optional[Dict[str, Any]]:
    """从 agents 表读四类集合。读不到返回 None（由调用方回落兜底）。

    `stage_order` 列可能存在但全为 NULL（老库未迁移）⇒ 视为不可用。
    """
    _owned = False
    try:
        if conn is None:
            from database import get_db
            conn = get_db()
            _owned = True
        cols = {r[1] for r in conn.execute("PRAGMA table_info(agents)")}
        if not cols:
            return None
        has_code = "is_code_stage" in cols
        has_orch = "is_orch_default" in cols
        has_stage = "stage_order" in cols

        sel = ["name", "status", "builtin", "agent_role"]
        sel.append("stage_order" if has_stage else "NULL AS stage_order")
        sel.append("COALESCE(is_code_stage,0) AS is_code_stage" if has_code else "0 AS is_code_stage")
        sel.append("COALESCE(is_orch_default,0) AS is_orch_default" if has_orch else "0 AS is_orch_default")
        rows = conn.execute(
            "SELECT %s FROM agents WHERE status='active'" % ", ".join(sel)).fetchall()
        if not rows:
            return None

        def _g(row, key, idx):
            try:
                return row[key]
            except Exception:
                return row[idx]

        stage_pairs: List[tuple] = []
        std: set = set()
        code: set = set()
        orch: List[str] = []
        for row in rows:
            name = str(_g(row, "name", 0) or "")
            if not name:
                continue
            builtin = int(_g(row, "builtin", 2) or 0)
            so = _g(row, "stage_order", 4)
            is_code = int(_g(row, "is_code_stage", 5) or 0)
            is_orch = int(_g(row, "is_orch_default", 6) or 0)
            if so is not None:
                stage_pairs.append((int(so), name))
                std.add(name)
            if builtin:
                std.add(name)
            if is_code:
                code.add(name)
            if is_orch:
                orch.append(name)

        stage_pairs.sort()
        stage_names = [n for _, n in stage_pairs]
        return {
            "stage_intents": stage_names,
            # 标准意图：内置意图 ∪ 阶段意图（都代表"意图标识"）
            "standard_intents": frozenset(std),
            "code_stages": frozenset(code),
            "orch_default": orch,
            # 缺列/全空 ⇒ 便于调用方与门禁区分"真没配"和"从没迁移过"
            "has_stage_order": bool(stage_names),
            "columns": {"is_code_stage": has_code, "is_orch_default": has_orch,
                        "stage_order": has_stage},
        }
    except Exception:
        return None
    finally:
        if _owned:
            try:
                conn.close()
            except Exception:
                pass


def snapshot(conn=None, force: bool = False) -> Dict[str, Any]:
    """取一份目录快照（带缓存）。返回：

        {stage_intents, standard_intents, code_stages, orch_default, source, ...}

    ⚠️ 缓存**只对 `conn is None`（默认库）生效**。显式传 conn 时每次现查、且**不写缓存** ——
       否则一个内存库/临时连接的查询结果会污染进程级缓存，让后续所有调用读到别的库的名单
       （本仓已经因为"两处口径不一致"栽过，不能让缓存再造一处）。
    """
    if conn is not None:
        got = _query(conn)
        if got and (got["stage_intents"] or got["standard_intents"] or got["orch_default"]):
            data = dict(got)
            data["source"] = "db"
            return data
        return {
            "stage_intents": list(FALLBACK_STAGE_INTENTS),
            "standard_intents": frozenset(FALLBACK_STANDARD_INTENTS),
            "code_stages": frozenset(FALLBACK_CODE_STAGES),
            "orch_default": list(FALLBACK_ORCH_DEFAULT),
            "has_stage_order": False,
            "columns": {"is_code_stage": False, "is_orch_default": False,
                        "stage_order": False},
            "source": "fallback",
        }

    now = time.time()
    if not force and _CACHE["data"] is not None and (now - _CACHE["ts"]) < TTL_SECONDS:
        return _CACHE["data"]

    with _LOCK:
        # 双检：等锁期间可能已被别的线程填好
        if not force and _CACHE["data"] is not None and (time.time() - _CACHE["ts"]) < TTL_SECONDS:
            return _CACHE["data"]
        got = _query(None)
        if got and (got["stage_intents"] or got["standard_intents"] or got["orch_default"]):
            data = dict(got)
            data["source"] = "db"
        else:
            data = {
                "stage_intents": list(FALLBACK_STAGE_INTENTS),
                "standard_intents": frozenset(FALLBACK_STANDARD_INTENTS),
                "code_stages": frozenset(FALLBACK_CODE_STAGES),
                "orch_default": list(FALLBACK_ORCH_DEFAULT),
                "has_stage_order": False,
                "columns": {"is_code_stage": False, "is_orch_default": False,
                            "stage_order": False},
                "source": "fallback",
            }
        _CACHE["data"] = data
        _CACHE["ts"] = time.time()
        return data


# ── 四个取用口（各处只调这些，不直接碰表）────────────────────────────────
def stage_intents(conn=None) -> List[str]:
    """建模流水线阶段意图（按 `agents.stage_order` 升序）。用于意图分类的粒度提示。"""
    return list(snapshot(conn).get("stage_intents") or [])


def standard_intents(conn=None) -> frozenset:
    """可落库为 `conversations.intent` 的标准标签。"""
    return frozenset(snapshot(conn).get("standard_intents") or ())


def code_stages(conn=None) -> frozenset:
    """产码阶段（决定流程名与视图联动）。"""
    return frozenset(snapshot(conn).get("code_stages") or ())


def orch_default(conn=None) -> List[str]:
    """无团队模式时的默认编排候选池。"""
    return list(snapshot(conn).get("orch_default") or [])


def orch_default_csv(conn=None) -> str:
    """同上，逗号串（供 `_ORCH_AGENTS` 这类字符串接口直接用）。"""
    return ",".join(orch_default(conn))


def source(conn=None) -> str:
    """`db` / `fallback` —— 供门禁与排障判断"到底读的是表还是兜底"。"""
    return str(snapshot(conn).get("source") or "")
