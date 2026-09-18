"""LLM 抽象接口（P1-3 插件化）。

所有 LLM Provider 实现 BaseLLM，经 ProviderRegistry 注册后即可被系统使用。
新增 LLM = 新建 provider 类 + 注册一行 + DB providers 表一行配置。
"""
from typing import Any, Optional


class BaseLLM:
    """LLM 统一抽象：chat / embed 两个核心能力。

    chat 返回 OpenAI-compatible 响应 dict：{"choices": [{"message": {...}}], "usage": {...}}
    （可附加 _meta 元信息）；stream=True 时返回生成器（逐行 SSE 数据）。
    """

    name: str = "base"

    def chat(
        self,
        messages: list,
        model: Optional[str] = None,
        temperature: float = 0.3,
        max_tokens: int = 4096,
        stream: bool = False,
        tools: Optional[list] = None,
        thinking: bool = False,
        **kwargs: Any,
    ):
        """对话生成。参数与 OpenAI Chat Completions 对齐，便于各 provider 透传。"""
        raise NotImplementedError

    def embed(self, texts: list, model: Optional[str] = None) -> list:
        """文本向量化（embedding providers 实现；对话类 provider 可抛 NotImplemented）。"""
        raise NotImplementedError

    def test_connection(self, cfg: Optional[dict] = None) -> dict:
        """连通性测试：返回 {"ok": bool, "detail": str}。"""
        return {"ok": False, "detail": "provider 未实现 test_connection"}
