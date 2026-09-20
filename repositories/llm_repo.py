"""LLM 配置域 Repository：llm_providers 表。

对应 routers/llm.py 的全部数据访问。
"""
from repositories.base import BaseRepo


class LlmRepo(BaseRepo):
    """LLM Provider 数据访问。"""

    def list_providers(self) -> list:
        return self.rows("SELECT * FROM llm_providers ORDER BY is_default DESC, name")

    def clear_default(self, model_type: str = "chat") -> None:
        """取消某类型全部默认标记（新增/更新该类型默认 provider 前调用）。

        model_type 隔离：对话默认与向量默认互不影响，可分别设置。
        """
        self.execute("UPDATE llm_providers SET is_default=0 WHERE model_type=?", (model_type,))

    def create_provider(self, name: str, provider_type: str, base_url: str, api_key: str,
                        model_name: str, max_tokens: int, temperature: float, is_default: int,
                        model_type: str = "chat", context_window: int = 8192,
                        tags: str = "[]", priority: int = 0, budget_tokens: int = 0) -> int:
        return self.execute(
            "INSERT INTO llm_providers (name, provider_type, base_url, api_key, model_name, max_tokens, temperature, is_default, model_type, context_window, tags, priority, budget_tokens) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (name, provider_type, base_url, api_key, model_name, max_tokens, temperature, is_default, model_type, context_window, tags, priority, budget_tokens),
        )

    def update_provider(self, pid: int, name: str, provider_type: str, base_url: str,
                        model_name: str, max_tokens: int, temperature: float, is_default: int,
                        model_type: str = "chat", context_window: int = 8192,
                        tags: str = "[]", priority: int = 0, budget_tokens: int = 0) -> None:
        self.execute(
            "UPDATE llm_providers SET name=?, provider_type=?, base_url=?, model_name=?, max_tokens=?, temperature=?, is_default=?, model_type=?, context_window=?, tags=?, priority=?, budget_tokens=? WHERE id=?",
            (name, provider_type, base_url, model_name, max_tokens, temperature, is_default, model_type, context_window, tags, priority, budget_tokens, pid),
        )

    def update_provider_key(self, pid: int, api_key: str) -> None:
        self.execute("UPDATE llm_providers SET api_key=? WHERE id=?", (api_key, pid))

    def update_provider_status(self, pid: int, status: str) -> None:
        """停用/启用模型：status=active|disabled（调用侧已按 model_type 隔离校验）。"""
        self.execute("UPDATE llm_providers SET status=? WHERE id=?", (status, pid))

    def update_provider_params(self, pid: int, model_params: str) -> None:
        """保存模型参数模板（P0 平台化：{"temperature":0.3,"max_tokens":8192,"top_p":0.9}）。"""
        self.execute("UPDATE llm_providers SET model_params=? WHERE id=?", (model_params, pid))

    def delete_provider(self, pid: int) -> None:
        self.execute("DELETE FROM llm_providers WHERE id=?", (pid,))
