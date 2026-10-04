# -*- coding: utf-8 -*-
"""Agent loop 护栏（2026-10-04 整改）—— 单Agent ReAct 路径与编排路径**共用同一份**。

## 为什么要有这个模块（而不是在两处 loop 里各写一段）
实测发现：编排路径 `_stream_orchestrated_flow` 有任务数上限 + token 预算
（`stream.py:60-61`），而单 Agent ReAct 路径（`execute.py` / `stream.py` 内联的那两段）
**只有 `max_tool_rounds=3` 一道闸**：
  · 无 token 预算      ⇒ 单轮可烧掉任意额度（编排侧有 20 万上限）
  · 无重复调用检测    ⇒ 同工具同参数反复调，只靠 3 轮兜
  · 工具无超时        ⇒ 单个慢工具能挂死整轮
两条路径是同一套ReAct 语义的两种实现，护栏必须同源 —— 否则今天补了A 明天忘了 B，
又会出现新的不对称。**本模块只提供纯函数 + 一个包装器，判定逻辑与执行解耦，便于测试。**

## 三道闸的语义（各自解决一个具体的失控形态）
1. **token 预算**：累计本轮 loop 已消耗的 token，超限即停止继续调工具并出结论。
   为什么按累计而非单次：失控形态是"每轮都在合理范围内、但轮次多"，单次看不出来。
2. **重复调用检测**：`(工具名, 规范化参数)` 相同且上次结果非弱 ⇒ 直接短路，
   回喂"该调用已执行过，结果如下"而不是再跑一次。
   为什么这一条不是"省时间"而是"省钱"：模型陷入循环时最典型的表现就是
   同参数反复调同一工具，每次都真跑一遍。
3. **工具超时**：单次工具执行超过阈值即返回可读的失败结果，让模型自己决定换路，
   而不是让整轮挂死。注意 Python **无法强制杀线程**，所以超时后线程仍会在后台
   跑完（无法取消）；因此这里只是"不再等它"，并如实告知模型"已超时"，
   **不谎称已取消**。

## 关键约束
- `normalize_args` 必须**规范化顺序**：JSON 串化前把 key 排序，
  否则 `{"a":1,"b":2}` 与 `{"b":2,"a":1}` 会被当成两次不同调用，检测形同虚设。
- 超时结果里的 `_t_timeout` 标记必须让上层能识别，否则模型会把"超时"当成
  工具真实返回的结论继续推理（比直接失败更糟）。
"""
import json
import threading
import time

__all__ = ["DEFAULT_TOKEN_BUDGET", "DEFAULT_TOOL_TIMEOUT_S",
           "normalize_args", "call_key", "budget_exhausted", "dedup_hit",
           "call_tool_guarded", "LoopGuard"]

# ── 默认阈值（与编排侧口径对齐，取"能挡住失控、又不误伤正常"）──
# token：编排侧 _ORCH_TOKEN_BUDGET=200000 是**整个 run**（6 个任务）；
# 单轮 loop 只有 3 轮 × ≤3 工具，给 200000 等于不设闸，故取一个真能起作用的量级：
# 单轮 3 轮 ReAct 的正常用量在 1~3 万 token 量级，60000 是"正常×2 以上才触发"。
DEFAULT_TOKEN_BUDGET = 60000
DEFAULT_TOOL_TIMEOUT_S = 120.0
DEFAULT_DUP_THRESHOLD = 2      # 同一 (工具,参数) 出现第几次开始短路


def normalize_args(args) -> str:
    """把参数规范化成**与 key 顺序无关**的稳定字符串。

    必须排序，否则 `{"a":1,"b":2}` 与 `{"b":2,"a":1}` 会被视为不同调用，
    重复检测形同虚设（这是最容易写错的一步）。
    """
    try:
        return json.dumps(args or {}, sort_keys=True, ensure_ascii=False,
                          default=str)
    except Exception:
        return str(args)


def call_key(name: str, args) -> str:
    """一次调用的去重键：`工具名|规范化参数`。"""
    return "%s|%s" % (name or "", normalize_args(args))


def budget_exhausted(tokens_used: int, budget: int | None) -> bool:
    """token 预算是否已耗尽。`budget<=0 or None` = 不限（关掉这道闸）。"""
    if not budget or int(budget) <= 0:
        return False
    return int(tokens_used or 0) >= int(budget)


def dedup_hit(seen: dict, key: str, threshold: int = DEFAULT_DUP_THRESHOLD) -> int:
    """登记一次调用并判断是否该短路。

    :returns: 命中短路时返回该键的累计出现次数（0 = 不短路，允许执行）
    ⚠️ 只有**结果非弱**的调用才计入（弱结果另有一道收敛逻辑，见调用方_weak_n），
    否则"工具本来就查不到东西"会被误判成重复调用而掩盖问题。
    """
    n = int((seen or {}).get(key, 0)) + 1
    (seen or {})[key] = n
    return n if n >= max(2, int(threshold or DEFAULT_DUP_THRESHOLD)) else 0


def call_tool_guarded(exec_fn, name: str, args: dict, *,
                      timeout_s: float | None = DEFAULT_TOOL_TIMEOUT_S) -> dict:
    """带超时地调用一次工具，**统一返回 `{ok, result}` 结构**（与工具原生返回同形）。

    为什么不在 `_exec_tool_call` 内部加超时：那个函数有 7 处提前 `return`
    （白名单拒绝/offload 重读/Hook/MCP/HTTP/…），在函数体里套超时必须改所有路径，
    风险面远大于收益。在**调用点**包一层则一处覆盖全部工具类型。

    ⚠️ 诚实说明：Python 无法强制杀死运行中的线程，`join(timeout)` 到点后线程
    **仍在后台继续**（拿不到结果、也不能撤回副作用）。因此本函数返回的是
    "不再等待 + 已超时"，**不是"已取消"** —— 必须这样告知模型，
    否则模型会把超时当成工具的真实结论继续推理。
    """
    if not timeout_s or float(timeout_s) <= 0:
        r = exec_fn(name, args)
        return r if isinstance(r, dict) else {"ok": True, "result": str(r)}

    box: dict = {}

    def _target():
        try:
            box["r"] = exec_fn(name, args)
        except BaseException as e:      # noqa: BLE001 —— 线程里必须全接，否则静默丢失
            box["e"] = e

    th = threading.Thread(target=_target, daemon=True, name="tool-%s" % (name or "")[:20])
    th.start()
    th.join(float(timeout_s))
    if th.is_alive():
        return {"ok": False, "_t_timeout": True,
                "result": "工具「%s」执行超过 %ss 仍未返回，已停止等待。"
                          "请勿重复调用同一工具同一参数（它可能已在后台继续运行），"
                          "改用其他方式获取信息，或基于已有信息直接作答。"
                          % (name, int(timeout_s)),
                "error": "tool_timeout"}
    if "e" in box:
        e = box["e"]
        return {"ok": False, "result": "工具执行异常：%s" % str(e)[:200],
                "error": type(e).__name__}
    r = box.get("r")
    return r if isinstance(r, dict) else {"ok": True, "result": str(r)}


def tokens_of(resp) -> int:
    """从一个 `llm_client.chat()` 响应里取 token 数（**唯一真源是 `_meta.token_count`**）。

    为什么不用 `usage.prompt_tokens` 直接相加：
    - Mock / 无 usage 时 `_record_usage` 会按 `len(输出)//2` **估算**并回填 `_meta.token_count`，
      直接读 usage 会得到 0 ⇒ 预算闸在降级路径上完全失效（而降级恰恰是最该省钱的场景）；
    - `_meta.token_count` 是 Task7 记账的同一口径，编排侧 `run_tokens` 也用它 ⇒ 两侧同源。
    """
    try:
        meta = (resp or {}).get("_meta") or {}
        return int(meta.get("token_count") or 0)
    except Exception:
        return 0


class LoopGuard:
    """一次 loop 的护栏状态（纯状态机，便于在测试里直接构造各种进度）。"""

    def __init__(self, *, token_budget: int | None = DEFAULT_TOKEN_BUDGET,
                 tool_timeout_s: float | None = DEFAULT_TOOL_TIMEOUT_S,
                 dup_threshold: int = DEFAULT_DUP_THRESHOLD):
        self.token_budget = token_budget
        self.tool_timeout_s = tool_timeout_s
        self.dup_threshold = dup_threshold
        self.tokens_used = 0
        self.calls: list = []              # [(name, args, ok, elapsed_ms)]
        self._seen: dict = {}              # call_key -> 次数（**仅非弱结果计入**）
        self._results: dict = {}# call_key -> 最近一次结果文本（短路时回喂）
        self.dup_short_circuits = 0
        self.timeouts = 0
        self.stop_reason = ""

    # ── 重复检测 ──
    def probe_dup(self, name: str, args) -> int:
        """执行前问一次：这个 (工具,参数) 是不是已经在跑过？0 = 允许执行。"""
        return dedup_hit(self._seen, call_key(name, args), self.dup_threshold)

    def mark_done(self, name: str, args, ok: bool, weak: bool = False,
                  elapsed_ms: int = 0) -> None:
        self.calls.append((name, args, ok, elapsed_ms))
        if not weak and ok:
            k = call_key(name, args)
            self._seen[k] = int(self._seen.get(k, 0)) + 1

    def last_result_for(self, name: str, args, default: str = "") -> str:
        """取该(工具,参数) 最近一次成功结果的文本（供短路回喂给模型）。

        为什么必须回喂结果而不是只说"已调用过"：模型下一轮还要基于工具输出推理，
        只告知"调过了"等于让它无从作答，会诱发第三次调用。
        """
        k = call_key(name, args)
        for nm, ar, ok, _ in reversed(self.calls):
            if call_key(nm, ar) == k and ok:
                return self._results.get(k, default) or default
        return default

    def undo_dup_count(self, name: str, args) -> None:
        """撤回一次去重计数（用于"执行后才发现结果是弱结果"的场景）。

        为什么需要：弱结果只有在**拿到返回值之后**才能判定，而 `call()` 已经
        先记了数。若不撤回，"工具本来就查不到东西"会在第 2~3 次被误判成
        重复调用 —— 于是模型收到的是"已执行过，别再调"，
        而真实情况是"这个工具查不到"，**掩盖了真正的信息**。
        """
        k = call_key(name, args)
        if int(self._seen.get(k, 0)) > 0:
            self._seen[k] = int(self._seen[k]) - 1

    # ── 预算 ──
    def add_tokens(self, n: int) -> None:
        try:
            self.tokens_used += int(n or 0)
        except Exception:
            pass

    def budget_left(self) -> bool:
        """预算是否仍可用。`stop_reason` 会被置为 `token_budget`（供回显与测试）。"""
        if budget_exhausted(self.tokens_used, self.token_budget):
            if not self.stop_reason:
                self.stop_reason = "token_budget"
            return False
        return True

    # ── 超时 ──
    def call(self, exec_fn, name: str, args, *, weak: bool = False):
        """带超时执行一次工具，并**统一记账**（耗时/结果文本/去重计数/超时数）。

        记账必须在这里而不能靠调用方再调`mark_done` —— 否则漏一处
        `mark_done` 就会让重复检测静默失效（而这类漏调用极难在测试里发现）。
        """
        t0 = time.time()
        r = call_tool_guarded(exec_fn, name, args, timeout_s=self.tool_timeout_s)
        if r.get("_t_timeout"):
            self.timeouts += 1
        _ok = bool(r.get("ok"))
        self.calls.append((name, args, _ok, int((time.time() - t0) * 1000)))
        if _ok:
            # 记结果文本：短路时要把既有结果回喂给模型（否则它无从作答）
            k = call_key(name, args)
            self._results[k] = str(r.get("result") or "")[:4000]
            # **只有非弱结果才计入去重计数**：弱结果另有一道收敛逻辑，
            # 若这里也计入，"工具本来就查不到东西"会被误判成重复调用而掩盖问题。
            if not weak:
                self._seen[k] = int(self._seen.get(k, 0)) + 1
        return r

    def stats(self) -> dict:
        """护栏统计（可挂到结果/卡片，让"为什么提前停了"可见）。"""
        return {"calls": len(self.calls),
                "distinct": len({call_key(n, a) for n, a, _, _ in self.calls}),
                "tokens_used": self.tokens_used, "token_budget": self.token_budget,
                "dup_short_circuits": self.dup_short_circuits, "timeouts": self.timeouts,
                "stop_reason": self.stop_reason}