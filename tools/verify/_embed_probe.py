# -*- coding: utf-8 -*-
"""embedding 服务可用性探针（门禁用，2026-11-25）。

## 为什么需要

有一批门禁的**被测路径**里含「语义候选筛选 / 向量召回」步骤（如 `_build_tools_def`
按 user_input 语义挑最相关工具）。产品对 embedding 不可用**已按设计降级**（bigram），
但降级后排序质量显著下降 ⇒ 断言可能翻红。

这类红的本体是**环境**（`192.168.100.202:8004` 502），不是代码缺陷：
  · CI 没有这个服务 ⇒ 常驻 pr 层必然恒红；
  · 本机服务挂掉时也会红，而"红因"与产品改动无关 ⇒ 干扰归因。

⇒ 正确做法不是放宽判据，而是**在依赖不可用时显式 SKIP 并打印原因**
（本仓既有口径：`verify_evalset_fingerprint` 的 `_corpus_empty ⇒ skip`、
`verify_sse_heartbeat` 的 `VACUOUS` 单列）。
⚠️ SKIP **必须计入汇总输出**（不能静默变绿）—— 否则等于删掉了这道门禁。

## 用法

```python
from tools.verify._embed_probe import embedding_available
if not embedding_available():
    print("  [SKIP] 需 embedding 服务，本机不可用 ⇒ 跳过 <断言名>")
else:
    check(...)
```
"""

from __future__ import annotations


def embedding_available(timeout: float = 5.0) -> bool:
    """用**产品自己的路径**打一次最小请求；任何异常都算不可用。

    ⚠️ 判据选择的三次否决（都试过，都不可靠）：
      ① `import 成功` —— provider 是 HTTP 服务，客户端能 import 照样连不上（首版踩到）；
      ② 读 `core.config.get("embedding", "base_url")` —— 该段**没有** url 键，
         真正的 base_url 在 `llm_providers` 表里（`knowledge_pipeline/embedder.py:244`）；
      ③ 只看 `unavail_blocked()` —— 那是**进程内**熔断窗口状态，门禁是新进程，恒 False。
    ⇒ 唯一可靠：实例化 Embedder、直接调它的 `_embed_api`，用它的失败做判据。
      这也是"夹具与产品同源"的同一条纪律。
    """
    try:
        from knowledge_pipeline.embedder import Embedder
        e = Embedder()
        if not getattr(e, "_api", None):
            return False                    # 没配 API ⇒ 本来就走 bigram，不算"服务挂了"
        e._embed_api(["ping"])
        return True
    except Exception:                       # noqa: BLE001 —— 探针本身绝不抛
        return False
