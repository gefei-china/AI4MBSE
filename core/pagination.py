"""列表分页硬上限（P2-1，2026-10-04）

## 为什么不是"到处加分页"

实测（本机真实库）给出两个前提，缺一不可地改变了这���项的做法：

| 表 | 行数 |
|---|---|
| `document_chunks` | 7269 |
| `llm_usage_stats` | 5048 |
| `entities` / `relations` / `triples` | 189 / 249 / 137 |
| `documents` | 15 |

实测各端点响应体：`/api/documents` **16 KB**、`/api/knowledge/entities?limit=2000`
**126 KB**，其余均 < 2 KB。

⇒ **在当前规模下，"大列表一次性拉全"根本不是瓶颈**。
按"收益÷ 成本"排，它排不进前列（原评估把它估成 3~5 天的重构，实测不成立）。

**但有一个真风险不是规模，而是"没有天花板"**：
`/api/documents?limit=99999` 当前返回 200。
limit 无上限意味着：**数据量涨 100 倍时，一次请求会静默变成 100 MB 响应**，
而这类退化在测试环境（数据少）永远看不出来，直到生产爆掉。

⇒ 本模块提供 `clamp_limit()`：一个**共用的硬上限**。
行为取"夹紧"而非"报错"：
- 报错（HTTP 422）会让前端既有调用突然失败 ⇒ 破坏性更大；
- 夹紧则老客户端继续工作，只是拿到上限那么多条 ⇒ 平滑降级。
`clamp_limit` 同时挡住 `limit=0`（会被拼成 `LIMIT 0` 返回空）、
负数、以及非数字（`?limit=abc` 现在会 500）。

## 用法

```python
from core.pagination import clamp_limit

@router.get("/api/knowledge/entities")
def list_entities(limit: int = 50, conn=Depends(db_session)):
    rows = conn.execute("... LIMIT ?", (clamp_limit(limit, default=50, maximum=200),))
```
"""
from __future__ import annotations

DEFAULT_MAX = 200


def clamp_limit(limit, default: int = 50, maximum: int = DEFAULT_MAX) -> int:
    """把 limit 夹紧到 `[1, maximum]`；不可用时回落 `default`。

    :param limit: 调用方传入的值（可能为 None / 0 / 负数 / 非数字字符串）
    :param default: `limit` 缺失或不可解析时用
    :param maximum: 硬上限
    :returns: `1 <= result <= maximum` 的整数

    ⚠️ 为什么夹紧而不是抛 422：抛错会让**既有前端调用突然失败**（破坏性变更）；
    夹紧是平滑降级 —— 老客户端继续可用，只是最多拿`maximum` 条。
    ⚠️ 为什么不用 `Query(le=)`：那要求每个端点都改成 Query 风格，
    而本仓 44 处 `limit: int = N` 里有些在**函数体内**用 `min()` 夹紧过
    （如 `graph_workspace.py:39`）。统一走`clamp_limit` 更好读、更好测。
    """
    try:
        n = int(limit)
    except (TypeError, ValueError):
        # None / "" / "abc" / 列表 → 回落默认值
        n = int(default)
    if n <= 0:
        # 0 / 负数：语义上"不限"最常见，但 SQL LIMIT 0 会返回空、
        # LIMIT -1 在 SQLite 里等于不限 ⇒ 两种解释都有坑，统一按默认值处理。
        n = int(default)
    try:
        cap = int(maximum)
    except (TypeError, ValueError):
        cap = DEFAULT_MAX
    if cap < 1:
        cap = DEFAULT_MAX
    return max(1, min(n, cap))
