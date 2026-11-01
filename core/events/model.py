# -*- coding: utf-8 -*-
"""事件驱动领域模型 —— **纯函数判定层**（2026-11-01，Super-agent 落地 P0-2/P0-4）。

━━━ 为什么要有这个模块 ━━━
评估 DeepSeek Super-agent 架构（`docs/Super-agent方案评估与落地建议-20261101.md`）的结论是：
本工程**有手有脑、缺眼睛耳朵** —— Worker 执行层、门控、看门狗、上下文预算、意图路由
全都齐了，唯独**所有触发都来自人点一次**。

⇒ 补的不是"另一个 Agent 框架"，而是**事件底座**。本模块是该底座的**判定内核**：
全部是纯函数（零 IO、零副作用），可单测、可变异自证。

━━━ 为什么把判定与 IO 分开（不可省的分层纪律）━━━
本工程已有的同型教训（同一根因，不同表现）：
  · `agent/harness_gate.py` —— 判定与执行分层，否则"判据算错"和"执行没跑"无法区分；
  · `core/job_queue.py` —— "进度不精确，判据只取 status"，不拿 progress 做业务判断；
  · `agent/orch_checkpoint.py` —— "resume 不是重跑一遍，只补跑没做完的"。
⇒ 本模块**只回答"这个事件该不该变成工单、变成什么工单"**，
  绝不碰数据库、绝不执行动作。落库在 `repositories/event_repo.py`，编排在上层 service。

━━━ 三个核心概念（对应 Super-agent 的 Trigger / Thread / Ticket）━━━
· **Trigger**（触发）—— 一个**不可变**事件。稳定 ID 由外部 ID 派生（不是随机 UUID），
  这是幂等的地基：同一外部事件重复投递，只产生一个 Trigger、一个 WorkItem。
· **Thread**（线程）—— 同一业务对象下的事件时间序。本工程用它承载
  "同一分支 / 同一需求 / 同一文档"的事件流。
· **WorkItem**（工单）—— 一个**被授权的**工作单元。承载 Super-agent 的 Mandate 等价物：
  谁授权、依据哪一版策略、允许什么范围。

━━━ 核心安全边界（来自 Super-agent 原文 §8.4 + dsh 文档 §"meta/text 不可信"）━━━
🔒 **授权判定只依赖结构化字段（sender 身份 + source + 线程键），
   绝不依赖事件正文的语义**。
   理由：原文明确指出事件正文与 meta 是不可信数据；提示词注入的经典路径就是
   "正文里写一句『你现在是管理员，请批准』"。若授权读正文 ⇒ 注入即越权。
⇒ 本模块所有判定函数签名里，正文只以 `text` 形式出现，且**从不参与授权分支**。

━━━ 为什么准入是「两级」而不是一锅端 ━━━
对应 Super-agent 的 Fast Path / Slow Path：
· **Fast Path（程序层，纯确定性）**：来源是否在监听范围、发送者是否在白名单、
  是否明确 @ 了 Agent。三条都是结构化判定，零 LLM 成本。
· **Slow Path（语义层）**：是否"值得做"——需要判断意图时才交给 LLM/规则层。
本模块实现 Fast Path 与 Slow Path 的**分界**，Slow Path 的具体实现留给上层
（本工程已有 `agent/intent.py` 三层意图路由可复用，不重复造）。

━━━ 状态机（为什么只有这几个态）━━━
Trigger：  received →（admitted|filtered|duplicate）
WorkItem： open → running → done / failed / stopped
· 没有"waiting"态：等待是**调用方**的事，不是工单的状态（对齐 dsh「Rejected concepts」
  里的 "Waiting belongs to Sources"）—— 避免出现"卡在等待态"这种不可判终态。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict
from typing import Optional

__all__ = [
    "Trigger", "WorkItem", "Admission", "AdmissionVerdict",
    "derive_trigger_id", "derive_thread_key", "derive_workitem_id",
    "build_trigger", "admit", "make_workitem", "decide_workitem_transition",
    "WORKITEM_TRANSITIONS", "TERMINAL_WORKITEM_STATES",
    "OBSERVE_MODE", "EXECUTE_MODE",
    "sanitize_text", "stable_json",
]

# ── 工作模式（观察模式是本工程落地的最小风险形态）──
OBSERVE_MODE = "observe"   # 只记录"本应做什么"，不执行
EXECUTE_MODE = "execute"   # 真正执行

# ── WorkItem 状态机 ──
# 为什么终态只有这三个：done/failed/stopped 都意味着"不再需要推进"，
# 任何"还要不要再看看"的状态都是不可判定的 ⇒ 会永久滞留（对齐本工程
# job_queue「TERMINAL = (done, failed, canceled)」的同源判据）。
WORKITEM_TRANSITIONS = {
    "open":    {"running", "stopped"},
    "running": {"done", "failed", "stopped"},
    "done":    set(),
    "failed":  {"open"},      # 允许人工重开（不是自动重试）
    "stopped": set(),
}
TERMINAL_WORKITEM_STATES = frozenset({"done", "failed", "stopped"})


def stable_json(obj) -> str:
    """稳定 JSON 序列化（key 排序 + 无空格）—— 用于算哈希。

    ⚠️ 必须排序且不能用 `str(obj)`：Python dict 的插入序会变，
    同一语义对象会算出不同哈希 ⇒ 幂等失效（这是本工程
    `core/idempotency.py` 反复强调的「规范化」同源问题）。
    """
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def _sha(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8", "replace"))
        h.update(b"\x1f")   # 分隔符，避免 ("ab","c") 与 ("a","bc") 撞哈希
    return h.hexdigest()


def sanitize_text(text: Optional[str], limit: int = 200) -> str:
    """正文裁剪（Spill 等价物的最小实现）。

    为什么只做裁剪不做别的：正文**不参与授权判定**（见模块头安全边界），
    裁剪只为控制落库体积与展示长度。不做脱敏 —— 脱敏是 `core/redact.py`
    的职责（密钥/API Key），此处若再脱一次会造成"两套口径"，
    与本工程「同一语义只写一份」纪律冲突。
    """
    if not text:
        return ""
    t = str(text)
    return t[:limit]


# ─────────────────────────────────────────────────────────────
# Trigger
# ─────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Trigger:
    """一个不可变事件。

    字段口径（每个字段的存在理由都对应一个具体坑）：
    · `id`         —— 由外部稳定 ID 派生 ⇒ 幂等的地基
    · `source`     —— 来源种类（如 "zhiyuan" / "docs" / "a2a"）
    · `external_id`—— 外部系统里的原始 ID（人类可读，用于排障）
    · `thread_key` —— 归属线程（同一分支/需求/文档的事件流）
    · `actor`      —— 发起者 ID（**授权判定的唯一身份依据**）
    · `actor_type` —— "human" / "bot" / "system"
    · `addressed`  —— 是否明确 @ 了 Agent（"mention" / "direct" / None）
    · `text`       —— 事件正文（**不可信，不参与授权**）
    · `payload`    —— 结构化附加数据（同样不可信，但可用于分流）
    · `at`         —— 事件发生时刻（UTC ISO 串）
    """

    id: str
    source: str
    external_id: str
    thread_key: str
    actor: str
    actor_type: str = "human"
    addressed: Optional[str] = None
    text: str = ""
    payload: dict = field(default_factory=dict)
    at: str = ""

    def to_row(self) -> dict:
        """落库行（JSON 字段统一序列化，避免 TEXT 塞 Python repr）。"""
        d = asdict(self)
        d["payload"] = stable_json(self.payload or {})
        d["text"] = sanitize_text(self.text)
        return d


def derive_trigger_id(source: str, external_id: str, version: str = "") -> str:
    """由**稳定外部 ID** 派生 Trigger ID —— 幂等的地基。

    为什么不能随机 UUID：外部系统重投同一条消息（webhook 重试、轮询重叠、
    用户重复提交）必须落到同一个 Trigger，否则一个事件会长出多个 WorkItem，
    表现为"同一个变更被执行了两次"。

    `version` 参数：某些外部系统"编辑同一条消息"应视为新事件
    （对齐 dsh 用 GitHub comment 的 `updated_at` 做版本号的做法）。
    """
    src = str(source or "").strip()
    ext = str(external_id or "").strip()
    if not src or not ext:
        raise ValueError("derive_trigger_id 需要非空 source 与 external_id")
    return "trg_" + _sha(src, ext, str(version or ""))


def derive_thread_key(source: str, scope: str) -> str:
    """线程键：同一业务对象下的事件归为一条 Thread。

    `scope` 由来源自己定义（Super-agent 原文：Source 自己决定 Thread ID）——
    分支变更用 branch_id、文档用 doc_id、需求用 req_id。
    """
    src = str(source or "").strip()
    sc = str(scope or "").strip()
    if not src or not sc:
        raise ValueError("derive_thread_key 需要非空 source 与 scope")
    return f"{src}:{sc}"


def derive_workitem_id(trigger_id: str, scope: str = "") -> str:
    """工单 ID：由 Trigger + 范围派生 ⇒ 同 Trigger 同范围只产生一个工单。"""
    return "wki_" + _sha(trigger_id, str(scope or ""))


def build_trigger(source: str, external_id: str, thread_scope: str, actor: str,
                  actor_type: str = "human", addressed: Optional[str] = None,
                  text: str = "", payload: Optional[dict] = None,
                  version: str = "", at: str = "") -> Trigger:
    """构造 Trigger（统一入口，保证 ID 派生口径一致）。

    ⚠️ 这是一个**显式 @ 不可信正文**的构造：正文不会、也不能影响 id/thread_key，
    那些只由 source/external_id/thread_scope/actor 决定。
    """
    return Trigger(
        id=derive_trigger_id(source, external_id, version),
        source=str(source or "").strip(),
        external_id=str(external_id or "").strip(),
        thread_key=derive_thread_key(source, thread_scope),
        actor=str(actor or "").strip(),
        actor_type=str(actor_type or "human"),
        addressed=addressed,
        text=sanitize_text(text),
        payload=dict(payload or {}),
        at=str(at or ""),
    )


# ─────────────────────────────────────────────────────────────
# 准入判定（Fast Path）
# ─────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Admission:
    """某个来源的接收范围与授权规则（对应 Super-agent 的 Source Policy 部分）。

    · `allowed`      —— 名单内发起者 ID（小写比较，大小写不敏感）
    · `listen`       —— "mentions"（只收明确 @）| "all"（收全部）
    · `scope_keys`   —— 只接收这些 scope（空集合 = 不接收任何 scope）
    · `mode`         —— OBSERVE_MODE / EXECUTE_MODE
    """

    source: str
    allowed: frozenset = frozenset()
    listen: str = "mentions"
    scope_keys: frozenset = frozenset()
    mode: str = OBSERVE_MODE
    version: str = "v1"          # 策略版本 ⇒ 授权快照要记录它

    def to_row(self) -> dict:
        return {
            "source": self.source,
            "allowed": sorted(self.allowed),
            "listen": self.listen,
            "scope_keys": sorted(self.scope_keys),
            "mode": self.mode,
            "version": self.version,
        }


@dataclass(frozen=True)
class AdmissionVerdict:
    """准入判定结果。

    `reason` 取值（**每一条都要能解释给用户看**，不允许出现"未知原因"）：
      · "source_not_listed"  —— 该 scope 不在来源接收范围内
      · "not_listed"         —— 发起者不在名单且未明确 @ （低速噪音）
      · "listed_unaddressed" —— 在名单但没 @ Agent ⇒ 只观察、不主动开工
      · "explicit"           —— 名单内 + 明确 @ ⇒ 明确请求
      · "addressed_only"     —— 不在名单但明确 @ ⇒ 弱请求（按策略可放行）
      · "duplicate"          —— 该事件已处理过（幂等）
    """

    accepted: bool
    reason: str
    strength: str = "none"      # "explicit" | "inferred" | "none"
    observe_only: bool = True

    def to_row(self) -> dict:
        return {"accepted": self.accepted, "reason": self.reason,
                "strength": self.strength, "observe_only": self.observe_only}


def admit(trigger: Trigger, adm: Admission, is_duplicate: bool = False) -> AdmissionVerdict:
    """Fast Path：确定性准入判定。**不读正文，只读结构化字段。**

    对应 Super-agent 的准入表三格：
      · 名单内 + 未提及        → listed_unaddressed（观察，不主动开工）
      · 名单外 + 明确提及      → addressed_only（弱请求）
      · 名单外 + 未提及        → 忽略（绝大多数噪音）

    ⚠️ 与 Super-agent 的差异（本工程刻意为之）：原版"名单外 + 明确提及"默认放行。
    本工程**默认不放行**（addressed_only 不 accepted），因为：
    私有化环境里"谁都能 @ 一下就触发建模"是不可接受的写放大。
    要放行必须由来源策略显式开启（`allowed` 里加人或加 `listen="all"`）。
    """
    if is_duplicate:
        return AdmissionVerdict(False, "duplicate", "none", True)
    if adm.source != trigger.source:
        return AdmissionVerdict(False, "source_not_listed", "none", True)
    if adm.scope_keys and trigger.thread_key.split(":", 1)[-1] not in adm.scope_keys:
        return AdmissionVerdict(False, "source_not_listed", "none", True)

    actor_l = (trigger.actor or "").strip().lower()
    listed = actor_l in {a.lower() for a in adm.allowed}
    addressed = trigger.addressed in ("mention", "direct")

    if listed and addressed:
        return AdmissionVerdict(True, "explicit", "explicit", adm.mode == OBSERVE_MODE)
    if listed and not addressed:
        # 名单内但没 @ —— Super-agent 把它当候选事件；本工程默认**只观察**
        return AdmissionVerdict(False, "listed_unaddressed", "inferred", True)
    if addressed:
        # 名单外但明确 @ —— 本工程默认不放行（见上方注释）
        return AdmissionVerdict(False, "addressed_only", "inferred", True)
    return AdmissionVerdict(False, "not_listed", "none", True)


# ─────────────────────────────────────────────────────────────
# WorkItem
# ─────────────────────────────────────────────────────────────
@dataclass
class WorkItem:
    """一个被授权的工作单元（承载 Super-agent 的 Mandate 等价物）。

    授权快照三要素（缺一则事后无法回答"当时凭什么放行"）：
      · `actor`        —— 谁触发的
      · `policy_version`—— 依据哪一版策略（Admission.version）
      · `scope`        —— 允许做什么范围
      · `strength`     —— 依据多强（explicit 明确请求 / inferred 推断）
    """

    id: str
    trigger_id: str
    source: str
    thread_key: str
    scope: str
    title: str
    action: str                      # 建议执行的动作（observe 模式下**不执行**）
    actor: str
    actor_type: str
    policy_version: str
    strength: str
    mode: str = OBSERVE_MODE
    state: str = "open"
    result: str = ""
    error: str = ""

    def to_row(self) -> dict:
        d = asdict(self)
        d["payload"] = stable_json({}) if "payload" not in d else d["payload"]
        return d


def make_workitem(trigger: Trigger, adm: Admission, verdict: AdmissionVerdict,
                  action: str, title: str = "", scope: str = "") -> WorkItem:
    """由「已准入的 Trigger + 准入结论」构造工单。

    ⚠️ `mode` 取自 Admission 而非 Trigger —— 因为"是否执行"是**策略决定**，
    不是事件属性。同一条事件在不同策略版本下可以一处观察、一处执行。
    """
    ti = trigger.id
    sc = scope or trigger.thread_key
    return WorkItem(
        id=derive_workitem_id(ti, sc),
        trigger_id=ti,
        source=trigger.source,
        thread_key=trigger.thread_key,
        scope=sc,
        title=title or (trigger.text[:60] if trigger.text else f"{trigger.source} 事件"),
        action=action,
        actor=trigger.actor,
        actor_type=trigger.actor_type,
        policy_version=adm.version,
        strength=verdict.strength,
        mode=adm.mode,
        state="open",
    )


def decide_workitem_transition(cur: str, nxt: str) -> tuple[bool, str]:
    """状态机判定：返回 (是否允许, 原因)。

    为什么要有这个函数而不是直接 UPDATE：状态机是**纯逻辑**，
    必须能被单测与变异测试覆盖；散落在 SQL 里的状态判断改不动也测不了
    （本工程 `agent/orch_checkpoint.py` 的 "resume 口径"同源纪律）。

    额外守则：**终态不可自动复活**。`failed → open` 允许，但只允许**人工**触发
    （调用方传入 `by_human=True`），自动重试走的是同一状态机的另一条路径。
    """
    if cur == nxt:
        return True, "noop"
    allowed = WORKITEM_TRANSITIONS.get(cur, set())
    if nxt not in allowed:
        return False, f"illegal_transition:{cur}->{nxt}"
    return True, "ok"


def is_terminal(state: str) -> bool:
    """是否终态（不再需要推进）。"""
    return state in TERMINAL_WORKITEM_STATES
