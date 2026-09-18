"""LLM Provider 注册表（P1-3 插件化核心）。

ProviderRegistry：name → provider 类 的注册映射。
- 内置 provider 在 llm/providers/__init__.py 注册；
- 新增 LLM = 新建 provider 类（继承 BaseLLM）+ 注册一行 + DB providers 表配置一行。
"""
from typing import Dict, Optional, Type

from .base import BaseLLM


class ProviderRegistry:
    """按名称注册/创建 LLM Provider 类。全局单例注册表。"""

    _classes: Dict[str, Type[BaseLLM]] = {}

    @classmethod
    def register(cls, name: str, provider_cls: Type[BaseLLM]) -> None:
        cls._classes[name] = provider_cls

    @classmethod
    def create(cls, name: str, cfg: Optional[dict] = None) -> BaseLLM:
        """按名称实例化 provider。未知名称 → ValueError（调用方负责兜底）。

        cfg=None 时按无参构造（兼容 MockLLM 等无需配置的 provider）。
        """
        if name not in cls._classes:
            raise ValueError(f"未注册的 LLM provider: {name}（已注册: {sorted(cls._classes)}）")
        provider_cls = cls._classes[name]
        if cfg is None:
            return provider_cls()
        try:
            return provider_cls(cfg)
        except TypeError:
            # provider 构造不接收配置 → 无参构造（如 MockLLM）
            return provider_cls()

    @classmethod
    def has(cls, name: str) -> bool:
        return name in cls._classes

    @classmethod
    def registered(cls) -> list:
        return sorted(cls._classes)
