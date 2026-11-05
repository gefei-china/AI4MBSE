# -*- coding: utf-8 -*-
"""事件来源注册表 —— 来源各自定义"什么算一个事件、收谁的、怎么回"（2026-11-01）。

━━━ 为什么要有这个模块 ━━━
Super-agent 原文的一个关键设计（§3.1 / dsh 文档 §"What is reusable"）：
**Source 的实现属于部署，Source 的契约属于框架**。每个来源自己知道：
  · 什么是它的一条事件（外部 ID 长什么样）
  · 哪些 scope（分支/文档/需求）要接收
  · 谁能发起工作（白名单）
  · 回复发到哪里（channel address）

本模块把"来源"抽象成一个注册表，于是**新增一个来源 = 注册一条描述**，
而不是新增一个框架分支。这让"事件驱动"可扩展，同时不产生第二套编排逻辑。

━━━ 为什么默认全部 observe ━━━
来源默认 `mode=OBSERVE_MODE`：只记录"本应做什么"，不执行。
理由（本工程既有政策："不强制、不默认提供不合理内容，来源为空时优雅降级"）：
  · 主动写操作的副作用不对称（建实体/建关系/合并分支难回滚）
  · 事件正文不可信 ⇒ 误触发会放大成脏数据
⇒ 先可观测，后可执行。放开执行是**逐来源显式**的动作。

━━━ channel 的简化 ━━━
Super-agent 的 Channel 有完整幂等/退避/重试语义。本工程**只做一件事**：
记录"该往哪回"（`reply_to` + `reply_kind`）。真正的发送留给上层
（复用本工程既有的 `routers/studio_parts/a2a_events.py` 消息池通道）。
理由：新建一套发送队列会与既有 `job_queue` 作业队列重复。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from core.events.model import (
    Admission, AdmissionVerdict, Trigger, WorkItem,
    OBSERVE_MODE, admit, build_trigger, make_workitem,
)

__all__ = ["SourceSpec", "SourceRegistry", "registry", "get_registry"]


@dataclass(frozen=True)
class SourceSpec:
    """一个事件来源的描述。

    · `kind`        —— 来源种类（稳定标识，出现在 Trigger.source 与策略键里）
    · `title`       —— 展示名
    · `admission`   —— 接收范围与授权规则
    · `to_trigger`  —— 外部原始消息 → Trigger
    · `action_for`  —— Trigger → 建议动作（**观察模式下只记录，不执行**）
    · `scope_of`    —— 外部消息 → scope（分支 id / 文档 id / 需求 id）
    · `reply_to`    —— 外部消息 → 回复地址（可选）
    """

    kind: str
    title: str
    admission: Admission
    to_trigger: Callable[[dict], Trigger]
    action_for: Callable[[Trigger], str]
    scope_of: Callable[[dict], str]
    reply_to: Optional[Callable[[dict], str]] = None
    # 可选：**建单前置校验**（返回 (ok, reason)）。用于"这类事件即使准入通过，
    # 也不该建工单"的场景 —— 例如 SysML 事件没带原始代码（无从修复）。
    # ⚠️ 与 admission 分工：admission 判"该不该做"（身份/范围），
    #    precheck 判"能不能做"（数据完备性）。两者都是**结构化判定**，都不读正文语义。
    precheck: Optional[Callable[[dict], tuple]] = None


class SourceRegistry:
    """来源注册表：注册 kind → SourceSpec，并提供"事件 → 准入 → 工单"的主流程。

    ⚠️ **本类不做 IO**：落库在 repositories/event_repo.py。
    职责边界与本工程其它 service 一致（services 只编排，不写 SQL）。
    """

    def __init__(self):
        self._specs: dict[str, SourceSpec] = {}

    # ── 注册 ──
    def register(self, spec: SourceSpec) -> None:
        """注册一个来源。同 kind 重复注册 ⇒ 直接覆盖（幂等重载）。

        为什么允许覆盖而不报错：热重载/测试里反复注册是常态，
        报错反而制造噪声。但**不允许**注册后 kind 悄悄变了语义 ——
        kind 一旦入库就是历史事实，改它必须新建 kind（见 ev_sources.kind 的说明）。
        """
        self._specs[spec.kind] = spec

    def get(self, kind: str) -> Optional[SourceSpec]:
        return self._specs.get(kind)

    def set_mode(self, kind: str, mode: str) -> bool:
        """改某个来源的**进程内**模式（observe ⇄ execute）。

        ⚠️ **为什么必须有这个方法（2026-11-01 实测修掉的真实缺陷）**：
        原实现只改 DB（`EventRepo.set_source_mode`），
        **进程内 `spec.admission.mode` 从没同步** ⇒
        切到 execute 之后，**新事件建的工单仍然拿 observe 快照** ⇒
        "放开自动执行"这个开关**实际不生效**。
        （实测症状：DB 里 mode='execute'，但新工单 mode='observe'，执行被拒。）
        ⇒ DB 与进程内必须**同时**改，两处不一致 = 开关失灵。

        :returns: True=该 kind 已注册且已切换；False=未注册
        """
        spec = self._specs.get(kind)
        if not spec:
            return False
        adm = spec.admission
        self._specs[kind] = SourceSpec(
            kind=spec.kind,
            title=spec.title,
            admission=Admission(
                source=adm.source,
                allowed=adm.allowed,
                listen=adm.listen,
                scope_keys=adm.scope_keys,
                mode=mode,
                version=adm.version,
            ),
            to_trigger=spec.to_trigger,
            action_for=spec.action_for,
            scope_of=spec.scope_of,
            reply_to=spec.reply_to,
            precheck=spec.precheck,   # ⚠️ 必须带上，否则切模式后前置校验静默失效
        )
        return True

    def kinds(self) -> list:
        return sorted(self._specs.keys())

    def specs(self) -> list:
        return [self._specs[k] for k in self.kinds()]

    # ── 主流程 ──
    def build(self, kind: str, raw: dict) -> Trigger:
        """外部消息 → Trigger（找不到来源即抛错，不静默丢弃）。"""
        spec = self._specs.get(kind)
        if not spec:
            raise KeyError(f"未注册的事件来源: {kind}")
        return spec.to_trigger(raw)

    def evaluate(self, kind: str, trigger: Trigger,
                 is_duplicate: bool = False) -> AdmissionVerdict:
        """准入判定（Fast Path，纯函数）。"""
        spec = self._specs.get(kind)
        if not spec:
            return AdmissionVerdict(False, "source_not_listed", "none", True)
        return admit(trigger, spec.admission, is_duplicate=is_duplicate)

    def to_workitem(self, kind: str, trigger: Trigger,
                    verdict: AdmissionVerdict) -> WorkItem:
        """已准入事件 → 工单（**观察模式下不执行 action，仅记录**）。"""
        spec = self._specs.get(kind)
        if not spec:
            raise KeyError(f"未注册的事件来源: {kind}")
        action = spec.action_for(trigger)
        title = ""
        if isinstance(action, tuple):
            action, title = action[0], action[1]
        return make_workitem(trigger, spec.admission, verdict, action, title)


# ── 进程内默认注册表 ──
registry = SourceRegistry()


def get_registry() -> SourceRegistry:
    """取全局注册表。**不要在模块顶层注册来源** ——
    导入顺序不确定会导致来源半注册（这是本工程 `routers/__init__.py`
    显式注册全部 router 的同源理由）。
    """
    return registry
