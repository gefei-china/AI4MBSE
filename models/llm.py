"""LLM 配置域模型（自 routers/llm.py 原样搬移，P1-2 Models 外置）。"""
from typing import Optional

from pydantic import BaseModel


class ProviderIn(BaseModel):
    name: str
    provider_type: Optional[str] = "openai"
    base_url: str
    api_key: Optional[str] = ""
    model_name: str
    model_type: Optional[str] = "chat"   # chat 对话模型 | embedding 向量模型
    max_tokens: Optional[int] = 8192
    context_window: Optional[int] = 8192  # 上下文窗口（tokens），对话请求 max_tokens 不超过该值
    temperature: Optional[float] = 0.3
    is_default: Optional[int] = 0
    # D10 智能路由：能力标签 / 优先级 / Token 预算
    tags: Optional[list] = []             # 能力标签，如 ["code","chinese","fast"]
    priority: Optional[int] = 0           # 路由优先级（越大越优先）
    budget_tokens: Optional[int] = 0      # Token 预算（0=不限）


class ProviderKeyIn(BaseModel):
    api_key: str
