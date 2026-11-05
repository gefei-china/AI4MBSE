"""编排主循环边界上的重规划决策器（P4 的统一闸门 + 节奏 + 决策顺序）。

## 为什么有这个模块

重规划的**判定纯函数**早就共享了（`services/replan_health.py` 的
`health_probe` / `early_stop_plan` / `probe_due`），但**闸门读取与节奏状态**
一直内联在流式路径（`agent/pipeline_parts/stream.py`）里，非流式路径
（`workflows/planner.py::_planner_core`）**一行都没有**。

实测后果（2026-11-04）：同一句话走流式会「观察→重新评估→调整计划」，
走非流式不会；而两条路径读的是**同一批配置**，只是有一边没读。
更根本的风险是「新增一个闸门键时必有一边漏读」——本仓已因
「`AgentRegistry.resolve` 与 `resolve_agent_name` 口径分裂」付过代价。

故把 **闸门（21 个键）+ 节奏水位 + 决策顺序**收敛到本模块，两条路径共用；
**动作应用**仍各自实现（流式要发 SSE 事件、非流式要写回返回值），
那部分差异是**合理的**（呈现方式不同），不是漂移。

## 边界纪律

1. **保守兜底**：配置读不到 ⇒ 全关（绝不「因读不到配置就开巡检/重画」）。
2. **纯观测旁路**：本模块不抛异常给调用方；调用方仍需自己 try/except（双保险）。
3. **决策可留痕**：`write_decision` 回调把每条决策（**含 none**）落
   `replan_decisions` —— 「为什么不动」与「为什么动」同等重要。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable, Dict, Iterable, List, Optional

LOOP_VERSION = "2026-11-04"

#: 决策异常必须**留痕**：静默 `return []` 会让「决策器坏了」看起来像「计划一直健康」。
#: （本模块首版就踩过：`_mk` 重复传参 ⇒ 所有决策静默为空，自检才抓出来。）
_LOG = logging.getLogger("mbse.replan")

#: 闸门默认值（**唯一出处**）。新增闸门只改这里 + `gates()` 的读取分支。
GATE_DEFAULTS: Dict[str, Any] = {
    # ── 总闸 ──
    "enabled": False,
    # ── P1：失败补跑 ──
    "retry_once": True,
    # ── P2：差量重画 ──
    "redraw_on_stall": True,
    "redraw_light": True,
    "heavy_redraw": False,
    "skip_leaf": False,
    "no_progress_stop": True,
    "max_redraw_rounds": 1,
    "redraw_expand_ratio": 1.5,
    # ── P3：用户指令 + 确认卡 ──
    "user_directive": False,
    "user_confirm_redraw": False,
    "confirm_ttl_sec": 60,
    "directive_max_touch": 3,
    "llm_redraw": False,
    # ── P4：健康巡检 + 提前收敛 ──
    "health_probe": False,
    "health_probe_llm": False,
    "early_stop": False,
    "probe_every_tasks": 3,
    "probe_every_sec": 60,
    "probe_time_budget_sec": 1800,
    "llm_actions": "replace",
}

_BOOL_KEYS = frozenset({
    "enabled", "retry_once", "redraw_on_stall", "redraw_light", "heavy_redraw",
    "skip_leaf", "no_progress_stop", "user_directive", "user_confirm_redraw",
    "llm_redraw", "health_probe", "health_probe_llm", "early_stop",
})
_INT_KEYS = frozenset({
    "max_redraw_rounds", "confirm_ttl_sec", "directive_max_touch",
    "probe_every_tasks", "probe_every_sec", "probe_time_budget_sec",
})
_FLOAT_KEYS = frozenset({"redraw_expand_ratio"})


def _as_bool(v: Any) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on", "y")
    return bool(v)


def _as_int(v: Any, default: int) -> int:
    """显式判空 —— `int(x or 3)` 会把 **0 静默变成 3**（本仓 max_steps=0 踩过同类坑）。

    0 是合法值（=「每个任务完成都判一次」/「不限预算」），必须保留原意。
    """
    if v is None or v == "":
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _as_float(v: Any, default: float) -> float:
    if v is None or v == "":
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def gates(cfg=None) -> Dict[str, Any]:
    """读出全部 replanning 闸门（**唯一闸门出处**）。

    `cfg` 可传 `core.config`（模块或等价对象，需有 `get(section, key, default)`）；
    不传则延迟导入。读取失败一律回落 `GATE_DEFAULTS`（**保守值**，全关）。
    """
    if cfg is None:
        try:
            from core import config as cfg  # noqa: PLC0415（延迟导入，避免模块加载期连配置）
        except Exception:
            return dict(GATE_DEFAULTS) | {"source": "fallback"}
    out: Dict[str, Any] = {}
    try:
        for k, dv in GATE_DEFAULTS.items():
            raw = cfg.get("replanning", k, dv)
            if k in _BOOL_KEYS:
                out[k] = _as_bool(raw)
            elif k in _INT_KEYS:
                out[k] = _as_int(raw, int(dv))
            elif k in _FLOAT_KEYS:
                out[k] = _as_float(raw, float(dv))
            else:
                out[k] = str(raw if raw not in (None, "") else dv)
    except Exception:
        return dict(GATE_DEFAULTS) | {"source": "fallback"}
    out["source"] = "config"
    return out


def llm_actions(g: Dict[str, Any]) -> tuple:
    """`llm_actions` 逗号串 → 元组（默认 `("replace",)`）。"""
    raw = str((g or {}).get("llm_actions") or "replace")
    acts = tuple(x.strip().lower() for x in raw.split(",") if x.strip())
    return acts or ("replace",)


class ReplanLoop:
    """主循环边界上的重规划决策器（持有巡检节奏的进程内水位）。

    生命周期 = **单次编排**（与流式路径的 `_hp_*` 同口径）：resume 起新 run 时重建。

    用法：

        loop = ReplanLoop(conn=conn, conversation_id=cid, run_id=rid, goal=goal,
                          write_decision=lambda source, dec: ..., llm_call=my_llm)
        for dec in loop.decide(done_items, tasks):
            if dec["action"] == "early_stop":
                ...取消 dec["cancel_keys"]...
            elif dec["action"] == "stop":
                ...block 剩余任务...
            elif dec["action"] == "refine":
                ...把 dec["refine_note"] 写进剩余任务 context...
    """

    def __init__(self, *, conn=None, conversation_id: int = 0, run_id: int = 0,
                 goal: str = "", provider_id=None, gate_cfg: Optional[Dict[str, Any]] = None,
                 write_decision: Optional[Callable[[str, Dict[str, Any]], None]] = None,
                 llm_call: Optional[Callable[[str], str]] = None,
                 clock: Callable[[], float] = time.time):
        self.conn = conn
        self.conversation_id = int(conversation_id or 0)
        self.run_id = int(run_id or 0)
        self.goal = goal or ""
        self.provider_id = provider_id
        self.g = dict(gate_cfg) if gate_cfg else gates()
        self._write = write_decision
        self._llm_call = llm_call
        self._clock = clock
        self._t0 = self._clock()
        self._hp_done_mark = 0
        self._hp_last_ts = self._t0
        self._hp_calls = 0

    # ── 就绪判定 ──────────────────────────────────────────────────────────
    def available(self) -> bool:
        """是否需要在本边界做任何事（总闸 + 至少一个分闸）。

        ⚠️ 这是对**上一版建议错误的修正**：`health_probe=True` 而 `enabled=False`
        时巡检**根本不跑**（闸门里总闸在前）。所以「只想观测」也必须开总闸，
        并**显式关掉 `redraw_on_stall`**（它默认开，会连带开重画）。
        """
        g = self.g
        if not g.get("enabled"):
            return False
        return bool(g.get("health_probe") or g.get("early_stop"))

    @property
    def state(self) -> Dict[str, Any]:
        """节奏水位快照（供审计/自检观察，不参与判定）。"""
        return {"done_mark": self._hp_done_mark, "last_ts": self._hp_last_ts,
                "llm_calls": self._hp_calls, "t0": self._t0}

    # ── 决策（边界调用一次）──────────────────────────────────────────────
    def decide(self, done_items: Optional[Iterable[Any]] = None,
               tasks: Optional[Iterable[Any]] = None,
               *, latencies_ms: Optional[List[int]] = None,
               window: Optional[int] = None) -> List[Dict[str, Any]]:
        """在迭代边界产出**决策列表**（顺序：提前收敛 → 巡检）。

        返回的每条决策都是同一个形状（便于两条路径一致地应用）：

            {"source": "early_stop"|"health", "action": "early_stop"|"stop"|"refine"|"none",
             "level": "T0"|..., "reason": str, "cancel_keys": [...],
             "refine_note": str, "metrics": {...}, "decision": <原始判定>}

        **不抛异常**：内部出错 ⇒ 返回空列表（观测旁路的铁律：只丢记录不阻断编排）。
        """
        if not self.available():
            return []
        try:
            from services import replan_health as _rh
        except Exception as _e:
            _LOG.warning("重规划决策异常（本边界跳过，编排不受影响）：%s", str(_e)[:200])
            return []
        try:
            items = list(done_items or [])
            rows = list(tasks or [])
            out: List[Dict[str, Any]] = []
            g = self.g

            # ── A. 提前收敛（先于巡检：它可能直接终结循环）──
            if g.get("early_stop"):
                es = _rh.early_stop_plan(rows, enabled=True)
                if str(es.get("action")) == "early_stop":
                    dec = self._mk("early_stop", es,
                                   cancel_keys=[str(k) for k in (es.get("cancel_keys") or [])])
                    self._record("early_stop", es)
                    out.append(dec)
                    return out      # 收敛即终结本边界，不再巡检

            # ── B. 健康巡检（节奏闸）──
            if not g.get("health_probe"):
                return out
            now = self._clock()
            every_tasks = int(g.get("probe_every_tasks") or 1)
            every_sec = int(g.get("probe_every_sec") or 60)
            if not _rh.probe_due(len(items) - self._hp_done_mark, now - self._hp_last_ts,
                                 every_tasks=every_tasks, every_sec=every_sec):
                return out
            self._hp_done_mark = len(items)
            self._hp_last_ts = now

            remain = sum(1 for t in rows
                         if str(t.get("status") or "") in ("planned", "ready"))
            budget = int(g.get("probe_time_budget_sec") or 0)
            left = (budget - (now - self._t0)) if budget > 0 else 0
            verdict = self._llm_verdict(items, rows) if g.get("health_probe_llm") else None

            # ⚠️ **窗口与节奏必须解耦**：`probe_every_tasks` 是「多久看一眼」，
            #   M1 的 window 是「看最近几个结果」。早先把两者绑成同一个值，
            #   一旦把节奏调成 1（为让早停能触发），window 就变成 1 ——
            #   而 M1 的判据要求样本 ≥3（少样本的占比是噪声）⇒ **refine 永不触发**。
            #   实测：上一轮把 probe_every_tasks 设成 1 后，这条能力静默失效。
            #   故 window 至少 M1_MIN_WINDOW。
            w = window if window is not None else max(int(every_tasks or 0), _rh.M1_MIN_WINDOW)
            hp = _rh.health_probe(
                items, rows, enabled=True, window=w,
                latencies_ms=list((latencies_ms or [])[-max(3, w):]),
                remaining_tasks=remain, budget_left_sec=left, llm_verdict=verdict,
            )
            dec = self._mk("health", hp)
            self._record("health", hp)
            if str(hp.get("action") or "none") in ("stop", "refine"):
                out.append(dec)
            else:
                # action=none 也要回一条：调用方需要知道「巡检过了、结论是不动」
                out.append(dec)
            return out
        except Exception as _e:
            _LOG.warning("重规划决策异常（本边界跳过，编排不受影响）：%s", str(_e)[:200])
            return []

    # ── LLM 巡检（可选，默认关）────────────────────────────────────────────
    def _llm_verdict(self, items: List[Any], rows: List[Any]):
        """轻量 LLM 巡检结论；不可调用/不可解析 ⇒ None（**不猜**）。"""
        if not callable(self._llm_call):
            return None
        try:
            from services import replan_health as _rh
            tpl, _ = _rh.llm_health_prompt(
                self.goal, rows,
                done_summaries=[str((it.get("summary") or {}).get("summary") or "")[:120]
                                for it in items[-5:]])
            self._hp_calls += 1
            raw = self._llm_call(tpl)
            verdict, perr = _rh.parse_llm_health(raw)
            return None if perr else verdict
        except Exception:
            return None

    # ── 决策整形与留痕 ────────────────────────────────────────────────────
    def _mk(self, source: str, raw: Dict[str, Any], *, cancel_keys=None) -> Dict[str, Any]:
        note = ""
        if str(raw.get("action")) == "refine":
            # 精修说明由调用方写入剩余任务 context（不改拓扑）
            note = str(raw.get("note") or raw.get("refine_note")
                       or raw.get("reason") or "")[:400]
        return {
            "source": source,
            "action": str(raw.get("action") or "none"),
            "level": str(raw.get("level") or ""),
            "reason": str(raw.get("reason") or ""),
            "cancel_keys": list(cancel_keys or []),
            "refine_note": note,
            "metrics": raw.get("metrics") or {},
            "signals": raw.get("signals") or [],
            "decision": raw,
        }

    def _record(self, source: str, raw: Dict[str, Any]) -> None:
        """落 `replan_decisions`（含 none —— 「为什么不动」同等重要）。"""
        if not callable(self._write):
            return
        try:
            self._write(source, raw)
        except Exception:
            pass


# ── 复用的落库写入器（两条路径同款，供调用方注入为 write_decision）───────────
def make_decision_writer(conn, *, conversation_id: int = 0, run_id: int = 0,
                         gates_snapshot: Optional[Dict[str, Any]] = None):
    """构造 `write_decision(source, decision)` 闭包（写 `replan_decisions`）。

    与流式路径 `_write_decision` **同表同列同归一化口径**；写失败只丢记录。
    """
    def _w(source, decision, **_kw):
        try:
            sigs = decision.get("signals")
            if isinstance(sigs, list) and sigs and isinstance(sigs[0], dict):
                sig_txt = ["%s:%s%s" % (s.get("id"), s.get("key"),
                                        (":" + str(s.get("detail") or "")[:40])
                                        if s.get("detail") else "")
                           for s in sigs]
            elif isinstance(sigs, list):
                sig_txt = [str(s)[:60] for s in sigs]
            else:
                sig_txt = [str(x)[:60] for x in (decision.get("basis") or [])]
            conn.execute(
                "INSERT INTO replan_decisions (conversation_id, run_id, "
                "source, round, signals, level, action, reason, gates, "
                "metrics, tokens, latency_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (int(conversation_id or 0), int(run_id or 0),
                 str(source or "")[:40], 0,
                 json.dumps(sig_txt, ensure_ascii=False),
                 str(decision.get("level") or "")[:20],
                 str(decision.get("action") or "")[:20],
                 str(decision.get("reason") or "")[:500],
                 json.dumps(gates_snapshot or {}, ensure_ascii=False),
                 json.dumps(decision.get("metrics") or {}, ensure_ascii=False)[:2000],
                 0, 0))
            conn.commit()
        except Exception:
            pass
    return _w
