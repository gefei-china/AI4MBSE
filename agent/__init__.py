"""Agent orchestration engine - intent recognition, GraphRAG, pipeline execution.

解耦拆分（2026-08）：原单体 3482 行 → 包结构，对外导入契约不变（agent/AgentPipeline/AgentRegistry/IntentRouter/GraphRAG/ConflictDetector/AgentDefinition）：
- definition.py AgentDefinition（Agent 定义）
- registry.py   AgentRegistry（意图 → Agent 定义注册表）
- intent.py     IntentRouter（三层混合意图识别）
- rag.py        GraphRAG + ConflictDetector（图谱检索 / 冲突检测）
- pipeline.py   AgentPipeline（编排管道）
"""
from .definition import AgentDefinition
from .registry import AgentRegistry
from .intent import IntentRouter
from .rag import GraphRAG, ConflictDetector
from .pipeline import AgentPipeline

# 兼容原单体模块级名称（外部脚本/测试可能按 agent.xxx 访问）
from database import get_db, db_conn  # noqa: E402
from knowledge_engine import VectorEngine, QueryRouter  # noqa: E402
from llm import llm_client  # noqa: E402
from core.config import STATIC_DIR  # noqa: E402
from file_tools import exec_file_tool, FILE_TOOL_NAMES as _FILE_TOOL_NAMES  # noqa: E402
from report_tools import exec_report_tool, REPORT_TOOL_NAMES as _REPORT_TOOL_NAMES  # noqa: E402

# Global instance
# ── 2026-10-09（P1-31，多用户并发）────────────────────────────────────────────
# ★ `agent` 这个模块级单例**保留**（大量既有脚本/测试按 `from agent import agent` 用），
#   但**生产对话入口不再使用它** —— 改用 `get_pipeline()`。
#
# 为什么必须改（实测，非推断）：
#   Pipeline 把请求级中间产物挂在 `self` 上（19 个 `self._xxx` 字段）。
#   `AgentPipeline.__init__` 实测仅 **0.001 ms**（只建 5 个轻量对象），
#   而 `_load_db_agents()` ≈25 ms/请求（每次请求本来就要跑，没有缓存白拿）
#   ⇒ 「每请求新建」的**额外成本 ≈25 ms**，相对一次 LLM 调用（3~60 s）可忽略。
#
#   而不新建的代价（`tmp/probe_leak/probe_concurrency.py` 实测，8 线程并发）：
#       模块级单例共享：标记被覆盖 **7/8**，ctx 归属错误 **7/8**
#       每请求新建    ：**0/8**
#   ⇒ 支持多用户并发 ⇒ **必须**每请求新建。
#   `reset_request_state()` 只能解决**顺序执行**，解决不了并发：
#   A 写进去的字段会被 B 的入口清零抹掉，A 收尾取到 None ⇒ 本次产出丢失。
agent = AgentPipeline()


#: Pipeline 构造耗时上限（毫秒）—— 超过则打警告，用于发现"构造意外变重"。
_PIPELINE_CTOR_WARN_MS = 50.0


def get_pipeline():
    """**每请求获取一个独立的 Pipeline 实例**（生产对话入口必须用这个）。

    ── 为什么 ──
    支持多用户并发 ⇒ 请求级状态必须是**请求私有**。
    共享单例时 `_sysml_last_pass_code` / `_mem_ctx` / `_tool_*` 等字段会互相覆盖，
    实测 8 线程并发下丢失率 **7/8**（见 `tools/verify/verify_pipeline_concurrency.py`）。
    入口清零（`reset_request_state`）**不能替代**本函数：
    清零只防"上一轮残留"，防不住"**别的请求正在处理中**"。

    ── 为什么不直接用 `AgentPipeline()` ──
    两者等价；本函数只是把"必须新建"这件事**显式化**，
    并留一个构造耗时观测点（构造意外变重时要能发现，而不是静默变慢）。

    ── 兼容性 ──
    · 既有 `from agent import agent`（脚本/测试）**照旧可用**，行为不变；
    · 生产路由（`routers/conversations.py`）已改用本函数。
    """
    import time as _t
    t0 = _t.perf_counter()
    p = AgentPipeline()
    dt_ms = (_t.perf_counter() - t0) * 1000
    if dt_ms > _PIPELINE_CTOR_WARN_MS:
        try:
            import logging as _lg
            _lg.getLogger("mbse.agent").warning(
                "Pipeline 构造耗时 %.1f ms（阈值 %.0f ms）—— 若持续偏高，"
                "说明构造路径变重了（每次请求都会付这个成本）",
                dt_ms, _PIPELINE_CTOR_WARN_MS)
        except Exception:
            pass
    return p
