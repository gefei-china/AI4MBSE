"""LLM Providers 注册（P1-3 插件化）。

内置 provider 在此注册；新增 LLM = 新建 provider 类（继承 BaseLLM）+ 本文件注册一行
+ DB llm_providers 表新增一行（provider_type 对应注册名）。
"""
from ..registry import ProviderRegistry
from .mock import MockLLM
from .openai_compat import OpenAICompatProvider

ProviderRegistry.register("mock", MockLLM)
ProviderRegistry.register("openai_compat", OpenAICompatProvider)
ProviderRegistry.register("openai", OpenAICompatProvider)  # 兼容：provider_type=openai 同走兼容实现

__all__ = ["MockLLM", "OpenAICompatProvider"]
