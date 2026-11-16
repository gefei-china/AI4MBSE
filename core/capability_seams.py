# -*- coding: utf-8 -*-
"""能力 seam 三层分离：契约 / 执行器 / 触发策略（P1-1）。

对标 DSH ``dsh-jobs/lib/index.js:60``（抽象 seam 单独加载即抛错）与
``dsh-spill/lib/index.js:28-31``（只暴露一个方法，策略归属他包）。

三层各自独立演进：
  * ``Capability``  —— 能力契约：名字 + 自描述（不含实现）。
  * ``Executor``    —— 执行器：唯一的副作用承载者，可替换后端。
  * ``TriggerPolicy`` —— 触发策略：决定本次是否执行，不含实现。

``invoke()`` 是唯一入口；策略不通过时返回 ``{"skipped": True}``，
不会去调用执行器。这样「判定」与「执行」在结构上分离，
与 ``core/guards.py`` 的零 IO 判定共同构成 shadow 零执行的基础。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@runtime_checkable
class Capability(Protocol):
    name: str

    def describe(self) -> dict: ...


@runtime_checkable
class Executor(Protocol):
    def run(self, capability: Capability, payload: dict) -> dict: ...


@runtime_checkable
class TriggerPolicy(Protocol):
    def should_run(self, capability: Capability, payload: dict) -> bool: ...


class AbstractSeam:
    """抽象 seam：直接调用即抛错，强制由具体实现挂载。"""

    def run(self, *args, **kwargs):
        raise NotImplementedError(
            "%s 是抽象能力 seam，未绑定执行器；请通过 CapabilityRegistry.register 挂载实现"
            % type(self).__name__)


@dataclass(frozen=True)
class Binding:
    capability: Capability
    executor: Executor
    policy: TriggerPolicy


class AlwaysRun:
    def should_run(self, capability, payload) -> bool:
        return True


class DuplicateCapability(RuntimeError):
    pass


class UnknownCapability(RuntimeError):
    pass


class ContractError(RuntimeError):
    pass


class CapabilityRegistry:
    def __init__(self):
        self._bindings = {}

    def register(self, name: str, capability: Capability, executor: Executor,
                 policy: TriggerPolicy = None, *, replace: bool = False) -> Binding:
        if name in self._bindings and not replace:
            raise DuplicateCapability("能力重复注册: %s（重复注册必须显式 replace=True）" % name)
        binding = Binding(capability=capability, executor=executor, policy=policy or AlwaysRun())
        self._bindings[name] = binding
        return binding

    def resolve(self, name: str) -> Binding:
        if name not in self._bindings:
            raise UnknownCapability("未注册的能力: %s；已注册: %s"
                                    % (name, ", ".join(sorted(self._bindings)) or "(空)"))
        return self._bindings[name]

    def invoke(self, name: str, payload: dict) -> dict:
        binding = self.resolve(name)
        if not binding.policy.should_run(binding.capability, payload):
            return {"skipped": True, "capability": name}
        return binding.executor.run(binding.capability, payload)

    def names(self):
        return tuple(sorted(self._bindings))


def run_contract(registry: CapabilityRegistry, name: str, cases: list,
                 alt_registry: "CapabilityRegistry | None" = None) -> dict:
    """契约测试：同一组输入，换一个执行后端后**可见输出结构必须一致**。

    只比对键集（可见行为），不比对具体值 —— 后端实现本就可以产出不同值，
    但调用方看到的字段形状不能变。这是「换后端不改可见行为」的可测判据。
    """
    outputs = [registry.invoke(name, case) for case in cases]
    keysets = [tuple(sorted((o or {}).keys())) for o in outputs]
    if len(set(keysets)) != 1:
        raise ContractError("同能力在不同输入下输出键集不一致: %s" % (keysets,))
    result = {"cases": len(cases), "keyset": list(keysets[0]), "alt_checked": False}
    if alt_registry is not None:
        alt_keysets = [tuple(sorted((alt_registry.invoke(name, case) or {}).keys()))
                       for case in cases]
        if set(alt_keysets) != {keysets[0]}:
            raise ContractError("替换执行器后可见输出键集改变: %s -> %s"
                                % (keysets[0], alt_keysets))
        result["alt_checked"] = True
    return result


DEFAULT = CapabilityRegistry()

__all__ = ["Capability", "Executor", "TriggerPolicy", "AbstractSeam", "Binding",
           "AlwaysRun", "DuplicateCapability", "UnknownCapability", "ContractError",
           "CapabilityRegistry", "run_contract", "DEFAULT"]
