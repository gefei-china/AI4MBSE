# -*- coding: utf-8 -*-
"""资产长尾治理分析器（P0-1，2026-10-18《自进化 AI 平台调研评估》整改第一项）。

动机：`tools/calib/retire_view_agents.py` 是**一次性硬编码退役工具**——点名 8 个
VIEW_AGENTS、人工 precondition、手工分批 disabled。同类判断（谁在闲置、谁在持续
失败、谁没人用）此后每次治理都要重写一遍，且判据散落在脚本里无法审计。

本模块把它泛化为**配置驱动的通用分析器**：

  · 输入只有两样：一张资产表（agents / skills，未来注册新 kind 即可扩展，不改引擎）
    + 使用证据（agents → agent_tasks 聚合；skills → use_stats JSON）；
  · 阈值全部走 `core.config` 的 `lifecycle` 组，模块内**不写死常量**——改阈值不改
    代码；门禁 `verify_lifecycle.py` 用「阈值变异」自证判据真被消费；
  · 只**建议**不自动执行：产出 keep/watch/review/retire_candidate 四档 + 理由 +
    建议动作，退役/停用仍由人确认（Misevolution 教训：自进化不得绕过人工关口
    自行收缩能力面）。

判定规则（优先级从强到弱，先命中先返回；uses=尝试次数，fail_rate=failures/uses）：

  retire_candidate  退出条件任一：
    (a) 闲置退出：idle_days ≥ idle_days 且 uses < min_uses   （没人用）
    (b) 质量退出：uses ≥ min_uses 且 fail_rate ≥ fail_rate_pct（足量样本持续失败）
  review            预警信号任一且未达退出：
    (a) idle_days ≥ idle_days 且 uses ≥ min_uses   （曾常用、现已凉）
    (b) fail_rate ≥ fail_rate_pct 且 uses < min_uses（失败率高但样本不足，证据弱需人看）
    (c) uses < min_uses 且 idle_days ≥ watch_idle_days（采用不足且已过观察期）
  watch             轻微信号任一：idle_days ≥ watch_idle_days 或 fail_rate ≥ watch_fail_rate_pct
  keep              其余（含新资产：last_used 缺态回退 created_at，刚建的不算闲置）

时间口径：SQLite `CURRENT_TIMESTAMP` 存的是 **UTC**，故 now 默认取 UTC  naive
datetime；传入 now 可覆盖（门禁传固定值保证确定性）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

# ── 档位与建议动作（产品契约：面板/门禁/API 三方共用，勿在调用处另写字面量）──
VERDICT_KEEP = "keep"
VERDICT_WATCH = "watch"
VERDICT_REVIEW = "review"
VERDICT_RETIRE = "retire_candidate"

VERDICTS = (VERDICT_KEEP, VERDICT_WATCH, VERDICT_REVIEW, VERDICT_RETIRE)

SUGGESTED_ACTIONS = {
    VERDICT_KEEP: "",
    VERDICT_WATCH: "持续观察，无需动作",
    VERDICT_REVIEW: "人工复核：保留、改进或纳入退役计划",
    VERDICT_RETIRE: "人工评估后停用/退役（保留审计记录）",
}

# 阈值默认值（与 core.config DEFAULT_CONFIG.lifecycle 保持一致；此处仅作
# get_thresholds 的兜底，正常路径全部走配置——门禁会变异配置证消费）
_THRESHOLD_DEFAULTS = {
    "enabled": True,
    "idle_days": 30,
    "min_uses": 5,
    "fail_rate_pct": 30,
    "watch_idle_days": 15,
    "watch_fail_rate_pct": 15,
}

_TS_FORMATS = ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S",
               "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S%z", "%Y-%m-%d")


@dataclass(frozen=True)
class AssetKindSpec:
    """一类资产的长尾判据来源。

    两种取数形态二选一（都为 None 时该 kind 无使用证据，只按存在性输出）：
      usage_sql —— 参数化 SQL，按 usage_param_col 指定的资产列聚合出
                   (uses, successes, failures, last_used_at)；
      usage_fn  —— 行解析器（如 skills.use_stats JSON），入参资产行 dict，返回同构四元组。
    新增资产种类 = 注册一个 spec，判定引擎零改动（可扩展性的落点）。

    ⚠️ usage_param_col 是**证据表的 join 列**，不一定是主键列：
       agent_tasks.agent_id 存的是 Agent **名**（schema 注释明示「存的是 Agent 名而非
       数字 id」，真库实测 'design'/62 次、'requirement_analysis'/47 次）⇒ agents 的
       使用证据必须按 name join，按 id join 会全表返 0（真库 smoke 会抓到）。
    """
    kind: str
    label: str
    table: str
    id_col: str = "id"
    name_col: str = "name"
    status_col: Optional[str] = "status"
    status_active: tuple = ("active",)
    usage_sql: str = ""
    usage_fn: Optional[Callable] = None
    usage_param_col: str = ""          # 空 ⇒ 用 id_col（证据表按主键 join 的种类）
    fallback_time_col: str = "created_at"   # last_used 缺态时的活跃时间回退列

    def param_col(self) -> str:
        return self.usage_param_col or self.id_col


def _skill_usage(row: dict):
    """skills.use_stats JSON → (uses, successes, failures, last_used_at)。

    结构约定 `{"uses":N,"success":N,"fail":N,"last_used":"..."}`（写入侧即此格式）；
    解析失败/字段缺失一律退化为 0/None——没有证据不产生信号，也不许炸。
    """
    stats = row.get("use_stats")
    if isinstance(stats, (bytes, bytearray)):
        try:
            stats = stats.decode("utf-8", "ignore")
        except Exception:
            stats = None
    if isinstance(stats, str):
        try:
            stats = json.loads(stats or "{}")
        except Exception:
            stats = {}
    if not isinstance(stats, dict):
        stats = {}

    def _i(v):
        try:
            return int(v or 0)
        except Exception:
            return 0

    return _i(stats.get("uses")), _i(stats.get("success")), _i(stats.get("fail")), stats.get("last_used")


# ── 资产种类注册表（扩展点：新 kind 在此登记，勿改判定引擎）──
KIND_REGISTRY: dict[str, AssetKindSpec] = {
    "agent": AssetKindSpec(
        kind="agent", label="智能体", table="agents",
        status_col="status", status_active=("active",),
        usage_sql=(
            "SELECT COUNT(*) AS uses,"
            " COALESCE(SUM(CASE WHEN status='done' THEN 1 ELSE 0 END), 0) AS successes,"
            " COALESCE(SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END), 0) AS failures,"
            " MAX(created_at) AS last_used_at"
            " FROM agent_tasks WHERE agent_id=?"
        ),
        # agent_tasks.agent_id 存 Agent 名（非数字 id）⇒ 按 name join（见 spec 文档）
        usage_param_col="name",
        fallback_time_col="created_at",
    ),
    "skill": AssetKindSpec(
        kind="skill", label="技能", table="skills",
        status_col="status", status_active=("published",),
        usage_fn=_skill_usage,
        fallback_time_col="created_at",
    ),
}


def register_kind(spec: AssetKindSpec) -> None:
    """注册新资产种类（幂等；重复注册同名 kind 以新 spec 覆盖）。"""
    KIND_REGISTRY[spec.kind] = spec


def get_thresholds() -> dict:
    """从 core.config 读取 lifecycle 组（缺键回退默认值）。

    门禁通过 monkeypatch `core.config.get` 变异本函数返回值，证明阈值真的走配置、
    没有被模块内常量旁路。
    """
    from core import config as _config
    out = {}
    for key, default in _THRESHOLD_DEFAULTS.items():
        if key == "enabled":
            out[key] = _config.as_bool("lifecycle", key, bool(default))
            continue
        try:
            out[key] = type(default)(_config.get("lifecycle", key, default))
        except Exception:
            out[key] = default
    return out


def _parse_ts(v):
    """容错时间解析（SQLite 'YYYY-MM-DD HH:MM:SS' / ISO8601）；失败返回 None。"""
    if not v:
        return None
    if isinstance(v, datetime):
        return v.replace(tzinfo=None) if v.tzinfo else v
    s = str(v).strip().replace("T", " ").replace("Z", "")
    for fmt in _TS_FORMATS:
        try:
            dt = datetime.strptime(s[:32], fmt)
            return dt.replace(tzinfo=None) if dt.tzinfo else dt
        except Exception:
            continue
    try:
        dt = datetime.fromisoformat(s)
        return dt.replace(tzinfo=None) if dt.tzinfo else dt
    except Exception:
        return None


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _classify(uses: int, failures: int, idle_days: Optional[float], th: dict) -> tuple[str, list[str]]:
    """四档判定（纯函数：阈值进、档位+理由出——门禁直接单测，无需数据库）。"""
    fail_rate = round(failures * 100.0 / uses, 1) if uses > 0 else 0.0
    idle = idle_days if idle_days is not None else 0.0
    idle_thr = float(th.get("idle_days", 30))
    min_uses = int(th.get("min_uses", 5))
    fail_thr = float(th.get("fail_rate_pct", 30))
    w_idle = float(th.get("watch_idle_days", 15))
    w_fail = float(th.get("watch_fail_rate_pct", 15))

    # ── retire_candidate：退出条件 ──
    if idle >= idle_thr and uses < min_uses:
        return VERDICT_RETIRE, [
            f"闲置 {idle:.0f} 天 ≥ {idle_thr:.0f} 天，且仅 {uses} 次使用（下限 {min_uses}）"]
    if uses >= min_uses and fail_rate >= fail_thr:
        return VERDICT_RETIRE, [
            f"失败率 {fail_rate}% ≥ {fail_thr}%（{failures}/{uses} 次失败，足量样本持续失败）"]

    # ── review：预警级 ──
    if idle >= idle_thr and uses >= min_uses:
        return VERDICT_REVIEW, [
            f"已闲置 {idle:.0f} 天（阈值 {idle_thr:.0f}），但历史有 {uses} 次使用"]
    if fail_rate >= fail_thr and uses < min_uses:
        return VERDICT_REVIEW, [
            f"失败率 {fail_rate}% ≥ {fail_thr}%，但样本不足（{uses} < {min_uses}），证据弱需人工判断"]
    if uses < min_uses and idle >= w_idle:
        return VERDICT_REVIEW, [
            f"采用不足：{uses} 次使用 < 下限 {min_uses}，且已闲置 {idle:.0f} 天（观察线 {w_idle:.0f} 天）"]

    # ── watch：轻微信号 ──
    reasons = []
    if idle >= w_idle:
        reasons.append(f"闲置 {idle:.0f} 天 ≥ 观察线 {w_idle:.0f} 天")
    if fail_rate >= w_fail:
        reasons.append(f"失败率 {fail_rate}% ≥ 观察线 {w_fail}%")
    if reasons:
        return VERDICT_WATCH, reasons
    return VERDICT_KEEP, []


def _table_exists(conn, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    return bool(row)


def analyze(conn, kinds=None, thresholds: Optional[dict] = None,
            now: Optional[datetime] = None) -> dict:
    """长尾分析入口。

    参数：
      kinds       —— 资产种类（KIND_REGISTRY 键）或 None=全部已注册种类；
      thresholds  —— 显式阈值（None ⇒ get_thresholds() 走 core.config）；
      now         —— 判定时刻（None ⇒ UTC now；门禁传固定值保证确定性）。

    返回：{"ok", "enabled", "thresholds", "summary", "items", "generated_at"}。
    任何单行/单表异常都只退化该资产（uses=0、无信号），不让整屏 500。
    """
    th = dict(thresholds) if thresholds else get_thresholds()
    now = now or _utcnow_naive()
    want = list(kinds) if kinds else list(KIND_REGISTRY.keys())
    want = [k for k in want if k in KIND_REGISTRY]

    result = {
        "ok": True,
        "enabled": bool(th.get("enabled", True)),
        "thresholds": {k: th.get(k) for k in _THRESHOLD_DEFAULTS},
        "summary": {"total": 0, "by_verdict": {v: 0 for v in VERDICTS},
                    "by_kind": {k: 0 for k in want}},
        "items": [],
        "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
    }
    if not result["enabled"]:
        return result

    for kind in want:
        spec = KIND_REGISTRY[kind]
        if not _table_exists(conn, spec.table):
            continue
        try:
            rows = [dict(r) for r in conn.execute(
                f"SELECT * FROM {spec.table}").fetchall()]
        except Exception:
            continue
        for row in rows:
            try:
                item = _analyze_row(conn, spec, row, th, now)
            except Exception:
                continue    # 单资产异常不拖垮整屏（与 monitor 域既有降级哲学一致）
            if item:
                result["items"].append(item)
                result["summary"]["by_verdict"][item["verdict"]] += 1
                result["summary"]["by_kind"][kind] += 1
                result["summary"]["total"] += 1
    # 排序：严重档优先；同档内失败率高者优先、闲置久者优先（面板最需要看的排最上）
    _rank = {VERDICT_RETIRE: 0, VERDICT_REVIEW: 1, VERDICT_WATCH: 2, VERDICT_KEEP: 3}
    result["items"].sort(key=lambda it: (
        _rank.get(it["verdict"], 9), -it.get("fail_rate", 0.0),
        -(it.get("idle_days") or 0.0)))
    return result


def _analyze_row(conn, spec: AssetKindSpec, row: dict, th: dict, now: datetime) -> Optional[dict]:
    uses = successes = failures = 0
    last_used_at = None
    if spec.usage_sql:
        agg = conn.execute(spec.usage_sql, (row.get(spec.param_col()),)).fetchone()
        if agg:
            uses = int(agg["uses"] or 0)
            successes = int(agg["successes"] or 0)
            failures = int(agg["failures"] or 0)
            last_used_at = agg["last_used_at"]
    elif spec.usage_fn:
        try:
            uses, successes, failures, last_used_at = spec.usage_fn(row)
        except Exception:
            uses = successes = failures = 0
            last_used_at = None
    uses, successes, failures = int(uses or 0), int(successes or 0), int(failures or 0)

    # 活跃时间：last_used 缺态回退 created_at（新资产刚建不算闲置）
    last_dt = _parse_ts(last_used_at) or _parse_ts(row.get(spec.fallback_time_col))
    idle_days = round((now - last_dt).total_seconds() / 86400.0, 1) if last_dt else None
    fail_rate = round(failures * 100.0 / uses, 1) if uses > 0 else 0.0

    verdict, reasons = _classify(uses, failures, idle_days, th)

    status = row.get(spec.status_col) if spec.status_col else None
    online = True
    if spec.status_col and status is not None:
        online = str(status) in tuple(spec.status_active)
    if "enabled" in row and row.get("enabled") is not None:
        online = online and bool(row.get("enabled"))

    return {
        "kind": spec.kind,
        "kind_label": spec.label,
        "id": row.get(spec.id_col),
        "name": row.get(spec.name_col) or "",
        "status": status,
        "online": online,
        "uses": uses,
        "successes": successes,
        "failures": failures,
        "fail_rate": fail_rate,
        "last_used_at": last_used_at,
        "idle_days": idle_days,
        "verdict": verdict,
        "reasons": reasons,
        "suggested_action": SUGGESTED_ACTIONS[verdict],
    }
