"""探针/验证噪声识别与统计口径隔离（S0 · 2026-10-28）。

## 为什么单独成模块

`tool_call_logs` 与 `llm_usage_stats` 是**决策依据表**：失败率、成本、延迟曲线
都从这两张表聚合而来。但它们是**同一个库**里既收生产调用、也收验证脚本与健康探针
的调用——实测（2026-10-28，@75eca26）：

| 污染源 | 规模 | 造成的失真 |
|---|---|---|
| `fake_tool` | 126 次 / 7 天，失败 112（89%） | 工具失败率被抬高 |
| `fake_tool` 的 latency | 记为 **1,793,164,758,196 ms**（≈56 年） | 任何 `AVG(latency_ms)` 报废 |
| `definitely_not_a_real_tool_xyzzy` / `no_such_tool_abc` | 14 + 若干 | 同上 |
| `chat_probe` | 1,186 次，占 7 天 LLM 成本 **51%** | 「成本优化」的基线全错 |
| `conv=1` 的 error span | 60 次，平均 **1.0 ms** | 会话错误率被算成 31%（真实≈7.5%） |

⇒ 不隔离这三类，**任何"优化前后"的对比都是自欺**。这也是本轮把它排在 S0 的原因。

## 设计原则

1. **标记而非删除**。探针本身有观测价值（`chat_probe` 是活体探针，健康度靠它），
   所以要能查、只是**默认不进业务统计**。
2. **判定集中在写入侧一处**。判断逻辑放本模块，写入点只调 `is_probe_*()`；
   避免 6 个写入点各写一套正则（本仓已有"同一常量抄多份必然漂移"的教训）。
3. **统计侧只提供片段，不改写既有查询语义**。`probe_where()` 返回一段可拼接的
   SQL，调用方自己决定拼在哪；不做全局拦截——隐式改写 SQL 是更大的风险。
4. **存量回填必须是确定性的**。只按**已证实的**假工具名/探针 intent 回填；
   拿不准的一律留 0，**不编造**（与本仓 `retry_count` / `capability_id` 的存量纪律一致）。

## 用法

```python
from core.probe_mark import probe_where, is_probe_tool, is_probe_intent

# 写入侧
conn.execute("INSERT INTO tool_call_logs (..., is_probe) VALUES (..., ?)",
             (..., is_probe_tool(tool_name)))

# 统计侧（默认排除探针）
sql = ("SELECT COUNT(*) FROM tool_call_logs WHERE ok=0"
       + probe_where())          # → " AND COALESCE(is_probe,0)=0"
```
"""

# ── 假工具名：**确定**为验证脚本所造，绝不可能是生产工具 ────────────────────
# 判据：这些名字来源于本仓 tools/verify/*.py 与测试夹具，出现即等于"有个脚本在跑"。
# 新增假工具时**必须**在此登记，否则统计会继续被污染（这也是 verify 脚本的断言对象）。
PROBE_TOOL_NAMES = frozenset({
    "fake_tool",
    "fake_mcp_tool",
    "fake_agent_tool",
    "definitely_not_a_real_tool_xyzzy",
    "no_such_tool_abc",
    "nonexistent_tool",
})

# 前缀约定：新写的探针工具建议直接加前缀，命中即标记，无需再登记名字。
PROBE_TOOL_PREFIXES = ("_probe_", "fake_", "test_tool_")

# ── 探针 intent：只有健康探活/自检用，不承载用户请求 ────────────────────────
# 注意 **不要**把 `chat_eval` / `gen_faithfulness` / `eval_*` 放进来：
#   那些是在线评测**消费**真实回答的环节，属于业务成本的真实组成，
#   把它们排除会让"每轮真实成本"偏低——那是另一种失真。
PROBE_INTENTS = frozenset({
    "chat_probe",          # 活体探针（实测占 7 天成本 51%）
    "probe",
    "probe_liveness_min",
})


def is_probe_tool(tool_name) -> int:
    """工具名是否属于探针/验证噪声。返回 0/1（直接可写 `is_probe` 列）。"""
    n = str(tool_name or "").strip()
    if not n:
        return 0
    if n in PROBE_TOOL_NAMES:
        return 1
    low = n.lower()
    return 1 if any(low.startswith(p) for p in PROBE_TOOL_PREFIXES) else 0


def is_probe_intent(intent) -> int:
    """intent 是否属于探针/自检。返回 0/1。

    兜底规则：以 `probe` 开头或以 `_probe` 结尾的也标记（防止新探针忘了登记）。
    刻意**不**用宽泛的 "test" 匹配——`latest`/`contest` 之类会被误伤。
    """
    s = str(intent or "").strip().lower()
    if not s:
        return 0
    if s in PROBE_INTENTS:
        return 1
    return 1 if (s.startswith("probe") or s.endswith("_probe")) else 0


def probe_where(alias: str = "") -> str:
    """返回可直接拼进 WHERE 的过滤片段（默认**排除**探针）。

    `alias` 传表别名时自动加前缀，如 `probe_where("l")` → `" AND COALESCE(l.is_probe,0)=0"`。

    ⚠️ 用 `COALESCE(is_probe,0)=0` 而不是 `is_probe=0`：老库经 `_add` 补列后默认 0，
    但若某行由更早的、未带该字段的降级写入路径产生（`llm/__init__.py` 的
    `no such column` 兜底分支就是这种），该列可能是 NULL ⇒ `is_probe=0` 会漏掉这些行。
    """
    p = f"{alias}." if alias else ""
    return f" AND COALESCE({p}is_probe,0)=0"


def probe_where_inclusive(alias: str = "") -> str:
    """只算探针（用于"探针自身的健康"这类查询）。"""
    p = f"{alias}." if alias else ""
    return f" AND COALESCE({p}is_probe,0)=1"


# ── 延迟合理性守卫 ────────────────────────────────────────────────────────────
# 为什么单列一条（与 is_probe 分开）：`tool_call_logs.latency_ms` 还有一类
# **不是探针、但计时错了**的行 —— 实测库内 2 行 `file_read`（2026-10-07，
# `user_name='测试'`、`args={"path":"/x.md"}` 指向不存在的文件）记了
# **1,791,347,294,749 ms ≈ 56.8 年**，直接把 `MAX/AVG(latency_ms)` 打成无意义。
#   根因与 orch_span 的 `backdate` 注释所述同型：墙钟与单调钟混用。
#   ⚠️ 但这两行的 `user_name` 恰好是"测试"——**不能**据此判探针：
#      真实用户完全可能叫这个名字，按用户名过滤会误伤生产数据。
#   ⇒ 改用**量纲守卫**：超过 1 小时的单次工具调用在工程上不可能，一律视为计时异常。
DEFAULT_MAX_SANE_LATENCY_MS = 3_600_000   # 1 小时


def sane_latency_where(alias: str = "", max_ms: int = DEFAULT_MAX_SANE_LATENCY_MS) -> str:
    """返回"延迟量纲可信"的过滤片段（排除计时溢出的脏行）。

    与 `probe_where()` **正交**：一条记录可以既是生产调用、又计时出错。
    做延迟类统计（p95/AVG/MAX）时应与本函数一起用；做失败率统计时不需要它
    （失败与否不受计时影响）。
    """
    p = f"{alias}." if alias else ""
    return f" AND COALESCE({p}latency_ms,0) BETWEEN 0 AND {int(max_ms)}"

