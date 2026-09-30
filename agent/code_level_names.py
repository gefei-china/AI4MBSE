"""代码级硬编码 Agent 名字清单（改名「影像提示」用）。

## 为什么要这份清单

`agents.name` 名义上只是"意图标识"，但它被**多处 Python 代码按字符串硬引用**。
这些引用**不经过数据库**，所以改 Agent 名字时**无法**像 `agent_tasks.agent_id` 那样
用一条 UPDATE 级联同步 —— 改完数据库，代码里的清单还是旧名字，语义就断了。

因此改名时不能"悄悄改完"，必须**事先提示**用户：「这个名字被代码引用，改了会影响什么」。

## 清单来源（都做惰性导入 + 异常兜底，任何一个不可读都不阻断改名）

| 来源 | 影响面 |
|---|---|
| `agent.intent.IntentRouter.INTENTS` | 意图路由表，key 就是 Agent 名；改名后该意图再也匹配不到这个 Agent |
| `agent.registry._FALLBACK_ORCH_AGENTS` | 内置编排回退池；编排降级时会按名字找不到 Agent |
| `agent.registry` 内置 `AGENTS` 字典 | 内置 Agent 定义表的 key |
| `_ORCH_AGENTS`（pipeline 类属性） | 与上面两个保持一致，由 registry 常量代表，不重复导入以免循环 |

⚠️ 注意：**这里不加载 AgentPipeline**（会牵出整条 LLM 依赖链），其 `_ORCH_AGENTS`
已由 registry 的同值常量代表（两个文件里都明确写了"保持一致"）。
"""
from typing import Set


def code_level_agent_names() -> Set[str]:
    """返回被 Python 代码硬引用的一组 Agent 名字（可能为空集 —— 任何一处读取失败都跳过而非报错）。"""
    names: Set[str] = set()

    # ① 意图路由表（最没有的一份：路由失效 = 用户问了也没人答）
    try:
        from agent.intent import IntentRouter
        names |= {k for k in (getattr(IntentRouter, "INTENTS", {}) or {}) if isinstance(k, str)}
    except Exception:      # noqa: BLE001  清单只用于提示，读不到就少提示，绝不阻断改名
        pass

    # ② 内置编排回退池（字符串常量）
    try:
        from agent.registry import _FALLBACK_ORCH_AGENTS
        names |= {x.strip() for x in (_FALLBACK_ORCH_AGENTS or "").split(",") if x.strip()}
    except Exception:      # noqa: BLE001
        pass

    # ③ 内置 Agent 定义字典
    try:
        import agent.registry as _reg
        for attr in ("AGENTS", "_BUILTIN_AGENTS", "BUILTIN_AGENTS"):
            d = getattr(_reg, attr, None)
            if isinstance(d, dict):
                names |= {k for k in d if isinstance(k, str)}
    except Exception:      # noqa: BLE001
        pass

    return names
