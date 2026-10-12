"""探针：验 `_intent` 真能**落库可归因**（Mock 通道，**不烧额度**）。

## 这一维为什么必须有

`verify_chat_task_intent.py` 的 AST 判据只能证明「源码写了 `_intent=`」。
落库这一环才决定**真库能否按任务维度做思考治理** ——
如果 intent 记不进去，前面的分流表配了也白配（无法归因、无法验证效果）。

## 为什么走 Mock 通道

真实通道当前**不可用**（实测2026-10-10）：
  · `deepseek-v4-flash` → 402 Insufficient Balance
  · `qwen3.8-max`       → 403 Free quota exhausted
（与真库 `failure_kind` 分布吻合：balance 46 / quota 20）
而本探针验的是**记账链路**（intent 是否写进 `llm_usage_stats.intent`），
与真实额度无关 ⇒ 用 `MBSE_LLM_FORCE_MOCK=1` 完全够。

⚠️ 每次运行会往 `mbse.db` 写 3 行 `used_mock=1` 的记录（可归因样本）。
这是**有意留痕**（证明链路通），不是污染：它们 `used_mock=1` 可被一眼区分。

输出：末行 `E2E <逗号分隔的 intent 列表>`
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

os.environ["MBSE_LLM_FORCE_MOCK"] = "1"


def main() -> int:
    import json

    import core.config as cfgmod
    from llm import llm_client
    from database import db_conn

    real_get = cfgmod.get

    def fg(group, key, default=None):
        if group == "llm" and key == "thinking_policy":
            return json.dumps({"off": ["chat_probe"], "on": []}, ensure_ascii=False)
        return real_get(group, key, default)

    with db_conn() as c:
        before = c.execute(
            "SELECT COALESCE(MAX(id),0) FROM llm_usage_stats").fetchone()[0]

    cfgmod.get = fg
    try:
        for intent in ("chat_probe", "chat_answer", "chat_repair"):
            llm_client.chat([{"role": "user", "content": "落库可归因探针"}],
                            provider_id=104, _intent=intent)
    finally:
        cfgmod.get = real_get

    with db_conn() as c:
        rows = c.execute(
            "SELECT intent FROM llm_usage_stats WHERE id>? ORDER BY id",
            (before,)).fetchall()
    intents = [r[0] for r in rows]
    print("E2E " + ",".join(intents))
    return 0


if __name__ == "__main__":
    sys.exit(main())
