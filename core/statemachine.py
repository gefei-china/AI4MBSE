# -*- coding: utf-8 -*-
"""#18（2026-10-18《自进化 AI 平台调研评估》）：统一状态机。

**问题**：进化决策本身需要状态机表达（候选→实验→晋升→回退），但全仓
23 处状态字面量散落在各 repo/路由的常量与 if 守卫里（MR 一套、intent_sample
一套、bad_case 一套、文档生命周期又一套……）—— 同一套「候选→确认→驳回」
语义被反复重写，且**互不知晓**：进化流水线（P1-3 配置提案 / P1-4 沙箱实验）
无从建模，因为没有任何一处能回答「这个实体现在处于进化管线的哪一环」。

**做法**：一个纯函数引擎 + 一张声明式注册表。
- 新实体 = 在注册表登记一个 Machine（状态域 + 迁移边 + 旧枚举映射），引擎零改动；
- 已接线的实体（merge_request / bad_case）的读写路径**必须过 Machine.check**，
  非法迁移在 repo 层就被拒（不靠路由层偶然守卫）；
- 未接线的实体先声明覆盖（states/edges 与真实数据一致，门禁 D 段勘察佐证），
  严格校验随各域自己的节奏接入 —— 收口优先于强改，23 处字面量从此有唯一出处。

与 lifecycle / asset_audit / badcase 同纪律：引擎不写死任何业务阈值，
迁移表是**结构声明**（不是配置），因此不走 core.config。
"""
from dataclasses import dataclass, field

__all__ = ["Edge", "Machine", "MACHINES", "get", "entities", "validate_registry",
           "evolution_pipeline"]


@dataclass(frozen=True)
class Edge:
    """一条迁移边：from 状态经 action 到 to。note_required=该动作必须带意见
    （如驳回：追溯需要，无意见的驳回是治理黑洞）。"""
    action: str
    to: str
    note_required: bool = False


def _edges(spec: dict) -> tuple:
    """紧凑写法 → 平铺边表：{"draft": [("open", "open"), ("reject", "closed", True)]}。"""
    out = []
    for frm, items in spec.items():
        for it in items:
            action, to = it[0], it[1]
            note = bool(it[2]) if len(it) > 2 else False
            out.append((frm, Edge(action=action, to=to, note_required=note)))
    return tuple(out)


@dataclass(frozen=True)
class Machine:
    """一个实体的状态机声明。

    wired=True 表示该实体的读写路径已接入本引擎（非法迁移在数据层被拒）；
    wired=False 表示仅声明覆盖（字面量已收口到本表，强校验待该域接入）。
    template=True 表示这是给流水线用的模板机（不对应具体表，不参与数据勘察）。
    """
    entity: str
    states: tuple
    initial: str
    edges: tuple                       # ((from_state, Edge), ...)
    legacy: tuple = ()                 # ((old_status, new_status), ...) 旧枚举归一化
    terminal: tuple = ()               # 终态（无出边；数据级操作如回滚不改变状态）
    desc: str = ""
    wired: bool = False
    template: bool = False

    # ── 纯函数（门禁 A 段直接矩阵验证）────────────────────────────
    def normalize(self, status) -> str:
        """旧枚举 → 新枚举（存量数据/旧调用方兼容）；未知值原样返回。"""
        s = "" if status is None else str(status)
        for old, new in self.legacy:
            if s == old:
                return new
        return s

    def targets(self, status) -> tuple:
        """某状态的全部合法目标态。"""
        return tuple(e.to for frm, e in self.edges if frm == status)

    def actions(self, status) -> list:
        """某状态可执行的动作清单（前端按钮/接口文档同源）。"""
        return [{"action": e.action, "to": e.to, "note_required": e.note_required}
                for frm, e in self.edges if frm == status]

    def check(self, frm, to) -> tuple:
        """(ok, error)：frm→to 是否合法。错误信息可直接回给调用方。"""
        f = self.normalize(frm)
        t = self.normalize(to)
        if f not in self.states:
            return False, f"当前状态非法：{frm!r}（{'/'.join(self.states)}）"
        if t not in self.states:
            return False, f"目标状态非法：{to!r}（{'/'.join(self.states)}）"
        if t in self.targets(f):
            return True, ""
        acts = "、".join(f"{e.action}→{e.to}" for e in
                         (e for frm2, e in self.edges if frm2 == f)) or "（终态，无出边）"
        return False, f"状态 {f} 不允许迁移到 {t}（可执行：{acts}）"

    def check_action(self, frm, action) -> tuple:
        """(ok, to, error)：按动作名校验（路由层 action=approve/reject 语义）。"""
        f = self.normalize(frm)
        if f not in self.states:
            return False, "", f"当前状态非法：{frm!r}（{'/'.join(self.states)}）"
        for frm2, e in self.edges:
            if frm2 == f and e.action == action:
                return True, e.to, ""
        acts = "、".join(e.action for frm2, e in self.edges if frm2 == f) or "（终态，无出边）"
        return False, "", f"状态 {f} 不允许执行 {action}（可执行：{acts}）"


# ── 注册表 ──────────────────────────────────────────────────────
# 顺序即阅读顺序：先进化流水线本体，后各承载实体。
_MERGE_REQUEST = Machine(
    entity="merge_request",
    states=("draft", "open", "merged", "closed"),
    initial="draft",
    edges=_edges({
        "draft":  [("open", "open"), ("reject", "closed", True)],
        "open":   [("approve", "merged"), ("reject", "closed", True)],
        "closed": [("reopen", "open")],
    }),
    legacy=(("pending", "open"), ("approved", "merged"), ("rejected", "closed")),
    terminal=("merged",),
    desc="MR 评审生命周期（对标 GitHub PR）；merged 后状态不再迁出，"
         "数据级回滚走 BranchRepo.rollback_merge（不改状态）",
    wired=True,
)

_BAD_CASE = Machine(
    entity="bad_case",
    states=("open", "resolved", "ignored"),
    initial="open",
    edges=_edges({
        "open":     [("resolve", "resolved"), ("ignore", "ignored")],
        "resolved": [("reopen", "open")],
        "ignored":  [("reopen", "open")],
    }),
    desc="P0-3 Bad Case triage 三态；复现重开由 core/badcase 按 "
         "reopen_on_recur 策略触发（仍走本机 check）",
    wired=True,
)

_EVOLUTION_PIPELINE = Machine(
    entity="evolution_pipeline",
    states=("candidate", "experiment", "promoted", "rolled_back"),
    initial="candidate",
    edges=_edges({
        "candidate":  [("start_experiment", "experiment")],
        "experiment": [("promote", "promoted"), ("abort", "candidate")],
        "promoted":   [("rollback", "rolled_back")],
    }),
    terminal=("rolled_back",),
    desc="进化流水线模板机（候选→实验→晋升→回退）：P1-3 配置提案与 P1-4 沙箱 "
         "A/B 实例化它；abort=实验中止（#19 取消链路），晋升必须过 HIL 确认",
    template=True,
)

_INTENT_SAMPLE = Machine(
    entity="intent_sample",
    states=("new", "suggested", "confirmed", "rejected"),
    initial="new",
    edges=_edges({
        "new":       [("suggest", "suggested")],
        "suggested": [("confirm", "confirmed"), ("reject", "rejected")],
        "rejected":  [("resuggest", "suggested")],
    }),
    desc="意图样本标注流转；评测只吃 confirmed（intent_sample_repo 注释同义）。"
         "当前 update 端点为自由设置（校验成员不校验迁移），强校验待该域接入",
)

_V2G_CANDIDATE = Machine(
    entity="v2g_candidate",
    states=("", "submitted", "approved"),
    initial="",
    edges=_edges({
        "":          [("submit", "submitted")],
        "submitted": [("approve", "approved")],
    }),
    desc="V2G 候选审核闸（review_status：''=未提交 → submitted → approved）",
)

_V2G_MATCH = Machine(
    entity="v2g_match",
    states=("pending", "confirmed", "rejected", "merged"),
    initial="pending",
    edges=_edges({
        "pending":   [("confirm", "confirmed"), ("reject", "rejected")],
        "confirmed": [("merge", "merged")],
        # 质量闸整批驳回：已 auto_merge 的行回退 pending 重来（staging_fuse 批次回滚）
        "merged":    [("requeue", "pending")],
    }),
    desc="V2G 候选匹配管线（v2g_candidates.status，与 review_status 是同一行上的 "
         "两条独立生命周期：匹配先过、人工审核后过）。真库 351 行（pending 345 / "
         "confirmed 6）；merged→pending 为 staging_fuse 质量闸抽检失败的回滚边",
)

_GENERATION = Machine(
    entity="generation_history",
    states=("pending", "confirmed", "rejected", "partial"),
    initial="pending",
    edges=_edges({
        "pending": [("confirm", "confirmed"), ("reject", "rejected"),
                    ("partial", "partial")],
    }),
    desc="生成历史确认流（pending → confirmed/rejected/partial）",
)

_HIL_CONFIRMATION = Machine(
    entity="hil_confirmation",
    states=("pending", "approved", "rejected"),
    initial="pending",
    edges=_edges({
        "pending": [("approve", "approved"), ("reject", "rejected")],
    }),
    desc="人工介入确认（HIL）：pending → approved/rejected；P1-4 门禁晋升的落点",
)

_DOCUMENT_LIFECYCLE = Machine(
    entity="document_lifecycle",
    states=("uploaded", "committed", "stored", "deprecated", "archived"),
    initial="uploaded",
    edges=_edges({
        "uploaded":  [("commit", "committed")],
        "committed": [("store", "stored")],
        "stored":    [("deprecate", "deprecated", True), ("archive", "archived")],
        "deprecated": [("restore", "committed"), ("archive", "archived")],
    }),
    desc="文档生命周期（MetaRepo.transition_lifecycle 同义声明；"
         "该函数当前为 from 校验 + to 特例的自由转换，强校验待接入）",
)

_FLOW_RUN = Machine(
    entity="flow_run",
    states=("queued", "running", "completed", "failed", "canceled"),
    initial="queued",
    edges=_edges({
        "queued":  [("start", "running"), ("cancel", "canceled")],
        "running": [("complete", "completed"), ("fail", "failed"),
                    ("cancel", "canceled")],
    }),
    desc="编排运行态（queued → running → completed/failed/canceled）",
)

MACHINES = {
    m.entity: m for m in (
        _EVOLUTION_PIPELINE, _MERGE_REQUEST, _BAD_CASE, _V2G_CANDIDATE, _V2G_MATCH,
        _GENERATION, _HIL_CONFIRMATION, _DOCUMENT_LIFECYCLE, _FLOW_RUN,
        _INTENT_SAMPLE,
    )
}


def get(entity: str):
    """按实体键取状态机；未登记返回 None（调用方应显式处理，不静默放行）。"""
    return MACHINES.get(entity)


def entities() -> list:
    """全部已登记实体键（模板机在后，供勘察/文档用）。"""
    return [m.entity for m in MACHINES.values() if not m.template] + \
           [m.entity for m in MACHINES.values() if m.template]


def evolution_pipeline() -> Machine:
    """进化流水线模板机（P1-3/P1-4 实例化入口）。"""
    return _EVOLUTION_PIPELINE


def validate_registry() -> list:
    """注册表自检（门禁 B 段消费）：返回错误列表，空=完好。

    校验：initial ∈ states；边端点 ∈ states；同 (from, action) 不重复；
    legacy 映射目标 ∈ states；terminal 无出边；entity 键唯一。
    """
    errors = []
    for m in MACHINES.values():
        if m.initial not in m.states:
            errors.append(f"{m.entity}: initial {m.initial!r} 不在 states")
        for frm, e in m.edges:
            if frm not in m.states:
                errors.append(f"{m.entity}: 边起点 {frm!r} 不在 states")
            if e.to not in m.states:
                errors.append(f"{m.entity}: 边 {frm}→{e.to} 终点不在 states")
        seen = set()
        for frm, e in m.edges:
            key = (frm, e.action)
            if key in seen:
                errors.append(f"{m.entity}: 重复动作 {frm}.{e.action}")
            seen.add(key)
        for old, new in m.legacy:
            if new not in m.states:
                errors.append(f"{m.entity}: legacy {old}→{new} 目标不在 states")
        for t in m.terminal:
            if t not in m.states:
                errors.append(f"{m.entity}: terminal {t!r} 不在 states")
            elif t in {frm for frm, _ in m.edges}:
                errors.append(f"{m.entity}: terminal {t!r} 存在出边（矛盾）")
    return errors
