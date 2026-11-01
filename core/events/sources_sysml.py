# -*- coding: utf-8 -*-
"""真实事件源：SysML v2 校验失败（2026-11-01）—— 第二个真实源。

━━━ 为什么接它 ━━━
① **零外部依赖**：校验器 `checker.jar` 已随包（`java-runtime` 挂载）；
② **链路已成熟**：`sysml_v2_validate`（只判错）+ `sysml_v2_autofix`（规则级修复，
   自带"改写后重跑校验、硬错增加则回滚"的自校验）都已存在；
③ **这正是 Super-agent「主动」最有价值的场景**：建模时校验失败，
   **不需要人再发一句"帮我修一下"** —— 失败本身就是事件。

━━━ 三条边界：为什么闭环只能做到"语法等价修复"为止 ━━━
`sysml_check_tools.py` 的 ① 号约束（原样继承，不改）：
  「自动改写、尤其『改到校验通过为止』会**掩盖真实建模缺陷**
   （把类型族错误改成能编译的形状，模型语义就丢了）」
⇒ `autofix` 只做**语义等价**的语法改写（connector→connect、关键字等价、可见性前缀）；
   涉及建模意图的（类型族、satisfy 引用谁、subject 与 satisfy 二选一、first...if 缺条件）
   **只报告不改写**（`UNFIXABLE` 表）。
⇒ 本模块**不越过**这条线：它只负责"发事件"，修不修、修到哪，由 `autofix` 的规则表裁决。

━━━ 为什么默认关闭（config 门闸）━━━
校验在**每次生成**都会跑（AI 建模主链路，高频）。若无条件发事件：
  · 一次会话几十次校验失败 ⇒ 台账被灌满 ⇒ 与"少占用人的注意力"直接冲突；
  · 且大多数失败是"LLM 下一轮自己就修好了"，不需要人/工单介入。
⇒ 默认 `events.sysml_source_enabled=False`；开启后仍靠 **Trigger ID 由代码哈希派生**
   天然去重（同一段失败代码只产生一条事件，不会每轮重报）。

━━━ 为什么 payload 存 code 而不存路径 ━━━
修复需要**原始代码**。存路径会在"文件已被改写/删除"时拿不到原文 ⇒ 修复无从下手。
⚠️ 但代码可能很长 ⇒ 存之前做长度闸（超限只存前缀 + 标记 truncated，
此时**不建可执行工单**——半截代码修出来的结果必然错）。
"""

from __future__ import annotations

import hashlib

from core.events.model import Admission, OBSERVE_MODE, build_trigger
from core.events.sources import SourceSpec

__all__ = [
    "SYML_SOURCE_KIND", "build_sysml_source", "emit_sysml_check_failed",
    "MAX_CODE_CHARS",
]

SYML_SOURCE_KIND = "sysml_check"

# 代码长度闸：超限则不建可执行工单（半截代码修复结果必然错）
MAX_CODE_CHARS = 200_000


def build_sysml_source(allowed_senders: frozenset = frozenset(),
                       scope_keys: frozenset = frozenset(),
                       mode: str = OBSERVE_MODE,
                       policy_version: str = "v1") -> SourceSpec:
    """构造 SysML 校验失败来源。

    Thread 口径 = **模型范围**（`model` 字段）：同一模型的多次校验失败归为一条事件流。
    这样面板上能看到"这个模型一直在失败" vs "偶发一次"。
    """
    adm = Admission(
        source=SYML_SOURCE_KIND,
        allowed=allowed_senders,
        listen="all",          # 系统事件，不靠 @ 触发
        scope_keys=scope_keys,
        mode=mode,
        version=policy_version,
    )

    def _to_trigger(raw: dict):
        raw = raw or {}
        code = str(raw.get("code") or "")
        # external_id 由**代码哈希**派生 ⇒ 同一段失败代码只产生一条事件（天然去重）
        digest = hashlib.sha256(code.encode("utf-8", "replace")).hexdigest()[:16]
        model = str(raw.get("model") or raw.get("scope") or "default").strip() or "default"
        n_hard = int(raw.get("n_hard") or 0)
        truncated = len(code) > MAX_CODE_CHARS
        return build_trigger(
            source=SYML_SOURCE_KIND,
            external_id="%s:%s" % (model, digest),
            thread_scope=model,          # 同一模型 = 同一 Thread
            actor=str(raw.get("actor") or "") or "system",
            actor_type=str(raw.get("actor_type") or "system"),
            addressed=None,
            # 正文只放人类可读摘要（**不参与授权**，见 model.py 安全边界）
            text="SysML v2 校验失败：硬错 %d 条（模型 %s）" % (n_hard, model),
            payload={
                "model": model,
                "code": code[:MAX_CODE_CHARS],
                "code_truncated": truncated,
                "n_hard": n_hard,
                "n_error": int(raw.get("n_error") or 0),
                "n_lexical": int(raw.get("n_lexical") or 0),
                "n_syntax": int(raw.get("n_syntax") or 0),
                "n_semantic": int(raw.get("n_semantic") or 0),
                "verdict": str(raw.get("verdict") or ""),
                "summary": str(raw.get("summary") or "")[:600],
            },
            version="",                    # 代码哈希即版本（改代码 = 新事件）
            at=str(raw.get("at") or ""),
        )

    def _action_for(trigger):
        """建议动作：**规则级自动修复 → 重校验**（只碰语法等价错误）。"""
        n_hard = int((trigger.payload or {}).get("n_hard") or 0)
        model = (trigger.payload or {}).get("model") or trigger.external_id
        return ("autofix_sysml", "SysML 校验失败待修复：硬错 %d 条（%s）" % (n_hard, model))

    def _precheck(raw: dict):
        """建单前置校验：**没原始代码 / 代码超长 ⇒ 不建工单**。

        为什么必须拦（而不是"建了再说"）：
          · 无代码 ⇒ 修复无从下手 ⇒ 建出来的工单**必然失败**，只会噪音化面板；
          · 超长已截断 ⇒ 在残缺代码上修复产出的结果**必然错**，比不修更危险。
        ⇒ 这两种情况都 `filtered` 且**带 reason**（可查"为什么这条没建单"）。
        """
        code = str((raw or {}).get("code") or "")
        if not code.strip():
            return False, "empty_code"
        if len(code) > MAX_CODE_CHARS:
            return False, "code_too_long:%d>%d" % (len(code), MAX_CODE_CHARS)
        return True, ""

    return SourceSpec(
        kind=SYML_SOURCE_KIND,
        title="SysML v2 校验失败",
        admission=adm,
        to_trigger=_to_trigger,
        action_for=_action_for,
        scope_of=lambda raw: str((raw or {}).get("model") or ""),
        precheck=_precheck,
    )


def emit_sysml_check_failed(conn, code: str, result: dict,
                            model: str = "default",
                            registry=None) -> dict:
    """校验失败（verdict=block）后发一条事件。

    ⚠️ **本函数永不抛异常、永不阻断校验**：校验是 AI 建模主链路，
    事件是旁路。任何失败只记日志。
    ⚠️ 调用方负责**门闸**（`events.sysml_source_enabled`）—— 本函数不自判配置，
    这样单测可以直接调它，不必伪造配置。
    """
    try:
        if not code or not str(code).strip():
            return {"error": "empty_code"}
        if str((result or {}).get("verdict") or "") != "block":
            # 只有 block（硬错）才值得发事件；pass/report 不打扰
            return {"error": "not_block", "verdict": (result or {}).get("verdict")}

        from core.events.sources import get_registry
        from repositories.event_repo import EventRepo
        from services.event_service import EventService

        reg = registry if registry is not None else get_registry()
        if reg.get(SYML_SOURCE_KIND) is None:
            return {"error": "source_not_registered", "kind": SYML_SOURCE_KIND}

        svc = EventService(EventRepo(conn), reg)
        return svc.receive(SYML_SOURCE_KIND, {
            "code": code,
            "model": model,
            "n_hard": (result or {}).get("n_hard", 0),
            "n_error": (result or {}).get("n_error", 0),
            "n_lexical": (result or {}).get("n_lexical", 0),
            "n_syntax": (result or {}).get("n_syntax", 0),
            "n_semantic": (result or {}).get("n_semantic", 0),
            "verdict": (result or {}).get("verdict", ""),
            "summary": (result or {}).get("summary", ""),
            "actor": "system", "actor_type": "system",
        })
    except Exception as e:
        try:
            import logging
            logging.getLogger(__name__).warning(
                "SysML 校验失败事件未发出（不阻断校验）: %s", str(e)[:160])
        except Exception:
            pass
        return {"error": str(e)[:200]}
