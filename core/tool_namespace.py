# -*- coding: utf-8 -*-
"""工具命名空间 + 原子换世代（P2-1 剩余缺口）。

已生效、本轮不动的部分：``llm/__init__.py`` 里已有 ``ProviderRegistry``
（``ProviderRegistry.has/create`` 按 provider_type 创建实现），
即「换 provider 不改调用方」已经落地。缺的是 MCP 侧的统一命名空间与
``notifications/tools/list_changed`` 的原子换世代。

对标 DSH ``dsh-mcp-client/README.zh.md:74,126``：
工具以 ``mcp__<server>__<rawName>`` 注册；列表变更时串行原子换世代，
避免「半挂载」状态（旧工具还在、新工具只来了一半）。
"""
from __future__ import annotations

import re
from types import MappingProxyType

_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
PREFIX = "mcp"


class ToolNameError(RuntimeError):
    pass


class DuplicateToolError(RuntimeError):
    pass


def normalize(server: str, raw: str) -> str:
    if not server or not raw or not _NAME.match(server) or not _NAME.match(raw):
        raise ToolNameError("server / raw 必须是非空且只含 [A-Za-z0-9_.-] 的标识：%r / %r"
                            % (server, raw))
    return "%s__%s__%s" % (PREFIX, server, raw)


def parse(name: str):
    parts = (name or "").split("__")
    if len(parts) != 3 or parts[0] != PREFIX or not parts[1] or not parts[2]:
        return None
    return parts[1], parts[2]


class ToolCatalog:
    def __init__(self):
        self._by_server = {}
        self._generation = 0

    @property
    def generation(self) -> int:
        return self._generation

    def register(self, server: str, tools) -> int:
        names = [normalize(server, t) for t in tools]
        if len(names) != len(set(names)):
            raise DuplicateToolError("同一 server 下工具名重复: %s" % server)
        self._by_server = {**self._by_server, server: tuple(names)}
        self._generation += 1
        return self._generation

    def apply_list_changed(self, server: str, tools) -> int:
        """列表变更：先完整构造新工具集，再一次性替换，杜绝半挂载。"""
        return self.register(server, tools)

    def snapshot(self, server=None):
        """不可变视图：返回后即使发生换世代，旧快照内容也不变。"""
        if server is not None:
            return MappingProxyType({server: self._by_server.get(server, ())})
        return MappingProxyType(dict(self._by_server))

    def all_names(self):
        return tuple(sorted(n for names in self._by_server.values() for n in names))


__all__ = ["ToolNameError", "DuplicateToolError", "normalize", "parse", "ToolCatalog"]
