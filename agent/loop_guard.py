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
           "normalize_args", "call_key", "strict_call_key", "semantic_args",
           "DEDUP_IGNORE_KEYS",
           "budget_exhausted", "dedup_hit", "call_tool_guarded", "LoopGuard",
           "tokens_of", "tokens_total_of", "charged_tokens", "CACHE_HIT_WEIGHT",
           "is_correctable_failure", "is_validation_block_result"]

# ── 默认阈值（与编排侧口径对齐，取"能挡住失控、又不误伤正常"）──
# token：编排侧 _ORCH_TOKEN_BUDGET=200000 是**整个 run**（6 个任务）；
# 单轮 loop 只有 3 轮 × ≤3 工具，给 200000 等于不设闸，故取一个真能起作用的量级：
# 单轮 3 轮 ReAct 的正常用量在 1~3 万 token 量级，60000 是"正常×2 以上才触发"。
DEFAULT_TOKEN_BUDGET = 60000
DEFAULT_TOOL_TIMEOUT_S = 120.0
DEFAULT_DUP_THRESHOLD = 2      # 同一 (工具,参数) 出现第几次开始短路
#: ★ 2026-10-17（米爸反馈）：单次工具调用**报错自动重试**次数。
#: 重试「执行异常」（瞬时故障如 HTTP 502/连接重置）；超时按米爸第二反馈也纳入重试，
#: 但**每次重试的等待时间有上限**（见 DEFAULT_RETRY_TIMEOUT_S），防单工具拖死整轮。
DEFAULT_TOOL_RETRIES = 3
#: ★ 2026-10-17（米爸反馈「超时也需要重试，但是需要时间上限，比如30s」）：
#: 重试尝试的**单次等待上限**（秒）。首轮仍用完整 tool_timeout_s（给慢工具公平机会），
#: 重试一律收紧到这个值——120s × 3 次的全额等待会把一轮拖到 8 分钟，不可接受。
DEFAULT_RETRY_TIMEOUT_S = 30.0

#: ★ 2026-10-28：短路回喂给模型的**既有结果上限**（字符）。
#: 原为 4000，与模型可见上限 `_TOOL_MODEL_CAP`(8000) 不同源 —— 意味着
#: "第一次看到的是 6000 字、第二次短路只回喂 4000 字"，同一份结果两种口径。
#: 目标包名恰好落在 4000 之后时，短路反而把命中切掉了 ⇒ 统一到 8000。
_TOOL_REPLAY_CAP = 8000


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


#: ★ 2026-10-28（conv 9726 实证）：**语义等价键**——只影响"分页/票据"的参数。
#:
#: 为什么必须剔除：那次会话里同一棵子树被查了两遍，
#:   ① `query_data_tree(parentId=3 系统架构设计)`（无 pageSize）
#:   ② `query_data_tree(parentId=3 系统架构设计, pageSize=100)`
#: 参数串不同 ⇒ 旧 `call_key` 判定"这是两次不同的调用" ⇒ 短路没触发
#: ⇒ 模型拿到的是**第二次的空白记忆**（它没意识到自己十分钟前刚查过，
#:   而那次回执里就写着目标包）⇒ 最终答出"该包不存在"。
#:   这类"同义不同参"的重复调用是**纯烧预算**：`dup_short_circuits` 当时是 0。
#:
#: 边界（诚实说明）：剔除后若上一次**没取全**（hasMore/total 未覆盖），
#: 重跑是**有意义**的 ⇒ `probe_dup` 会放行（见 `paging_complete` 守卫）。
DEDUP_IGNORE_KEYS = frozenset({
    "pageSize", "pagesize", "pageNum", "pagenum", "page", "limit", "offset",
    "includeReadToken", "include_read_token", "readToken", "read_token", "token",
})


def semantic_args(args):
    """剔除"只影响分页/票据"的参数后的参数副本（去重键专用）。"""
    out = {}
    for k, v in (args or {}).items():
        if str(k) in DEDUP_IGNORE_KEYS:
            continue
        out[k] = v
    return out


def call_key(name: str, args) -> str:
    """一次调用的去重键：`工具名|规范化参数`（**语义键**，忽略分页/票据参数）。"""
    return "%s|%s" % (name or "", normalize_args(semantic_args(args)))


def strict_call_key(name: str, args) -> str:
    """**严格键**（含分页参数）。用于"我到底调了几次不同参数"这类统计与排障。"""
    return "%s|%s" % (name or "", normalize_args(args))


def is_validation_block_result(name: str, result) -> bool:
    """纯函数：这次结果是否为 `sysml_v2_validate` 的"硬错阻断"回执。

    供**重复调用短路分支**判定是否要改回喂"校验停滞"说明（2026-11-20）。
    短路发生在执行前，validation_stalled 的执行后检测永远看不到它 ⇒
    必须在这里独立判一次"上次结果是 block"，否则同一 block 错误会被无限回喂。
    """
    if str(name or "") != "sysml_v2_validate":
        return False
    txt = str(result if isinstance(result, str) else
              (result or {}).get("result") or "")
    return ("判据：block" in txt or "判据:block" in txt
            or "存在硬错" in txt)


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

    # ★★ 2026-11-03（真库 28 条实证）：**必须继承上下文**。
    #   工具在裸 `threading.Thread` 里执行 —— `ContextVar` 不跨线程传播 ⇒
    #   工具内部的一切 LLM 调用（典型：`graph_retrieve` → `knowledge_engine`
    #   → `services.rag_rerank.llm_rerank`）拿到的 trace 上下文是**空的**：
    #   实测那 28 条 `rag_rerank` 的 `trace_id=''`、`conversation_id=0`、`run_id=0`，
    #   而同期同编排的 `chat_probe` 归属完整。
    #   后果：① 检索重排的 token 完全不计入会话/run 成本；
    #        ② `trace_id` 为空使"这一轮调了哪些能力、花了多少钱"断链。
    #   ⚠️ 不用 `threading.Thread` 的 kwargs（没有该能力），必须在 _target 内部 run。
    import contextvars as _cv
    _tool_ctx = _cv.copy_context()

    def _target():
        try:
            box["r"] = _tool_ctx.run(exec_fn, name, args)
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


def is_correctable_failure(res) -> bool:
    """这次失败是不是**参数类、可自修正**的（⇒ 不该扣失败轮预算）。

    ★ 2026-10-28（conv 9736 实证）：那一轮模型连续 3 次把 `elementTypes`
      传成平台不认的 `SEQUENCE_VIEW`，平台回的是
      `{"code":"INVALID_ARGUMENTS","retryable":false,"recovery":{"action":"CORRECT_ARGUMENTS"}}`
      —— 这是**信息性结果**（告诉它"这个值不合法，请按清单改"），
      而失败轮预算把它当"工具坏了"，3 次就整轮判死 ⇒ 建包与写入一次都没尝试。
      ⇒ 参数类错误必须**不扣**失败轮：它恰恰是"再试一次就会对"的那类。

    判据（只认回执里明写的信号，不猜）：
      · `code in {INVALID_ARGUMENTS, ARGUMENT_*, ...}`（平台/MCP 参数错误族）
      · `error in {args_truncated}`（我方截断）
      · `recovery.action == CORRECT_ARGUMENTS`
      · 文本里出现 `INVALID_ARGUMENTS` / `未知elementTypes` / `参数非法` 等
    """
    try:
        d = res if isinstance(res, dict) else {}
        code = str(d.get("code") or "").upper()
        err = str(d.get("error") or "").upper()
        rec = d.get("recovery") or {}
        action = str((rec or {}).get("action") or "").upper()
        if code.startswith("INVALID_ARGUMENTS") or code in (
                "ARGUMENT_INVALID", "INVALID_PARAMS", "BAD_REQUEST"):
            return True
        if err in ("ARGS_TRUNCATED", "INVALID_ARGUMENTS"):
            return True
        if action == "CORRECT_ARGUMENTS":
            return True
        txt = str(d.get("result") or "")[:2000]
        for k in ("INVALID_ARGUMENTS", "未知elementTypes", "参数非法",
                  "CORRECT_ARGUMENTS", "参数不合法", "invalid arguments"):
            if k in txt:
                return True
        return False
    except Exception:
        return False


def tokens_total_of(resp) -> int:
    """取**原始** token 总数（`_meta.token_count` = prompt + completion）。

    仅供报表/排障展示"这轮吞吐了多少 token"。**不要**拿它判预算 —— 见下。
    """
    try:
        meta = (resp or {}).get("_meta") or {}
        return int(meta.get("token_count") or 0)
    except Exception:
        return 0


#: ★ 2026-10-28：**缓存命中的计费权重**（默认 0.1，对齐 DeepSeek 命中价 ≈ 1/10）。
#: 为什么预算要按"计费等效 token"而不是原始 token：
#:   conv 9730 实测 —— 270,433 prompt 中 **249,344（92%）是缓存命中**，
#:   真实新增只有 21,089 miss + 10,600 输出。而预算闸把每轮重发的 16.7k 固定前缀
#:   按全价累计 ⇒ 判"已用 86%" ⇒ 在离终点两轮处降级收尾。
#:   ⇒ 预算本意是**控成本**，那就必须按成本计价的口径记，而不是按"重复读了几遍前缀"。
#: 该权重可配（`agent.cache_hit_weight`），置 1.0 即退回"按原始 token 计"的旧口径。
CACHE_HIT_WEIGHT = 0.1


def charged_tokens(resp, cache_hit_weight: float | None = None) -> int:
    """一次 LLM 调用**应计入预算**的 token 数（计费等效口径）。

    组成：`cache_miss + completion + cache_hit × 权重`（未标注部分按全价兜底，绝不丢）。
    无缓存字段（Mock / provider 不返回）⇒ 退回原始 `token_count`，零行为漂移。
    """
    try:
        meta = (resp or {}).get("_meta") or {}
        total = int(meta.get("token_count") or 0)
        hit = int(meta.get("cache_hit_tokens") or 0)
        miss = int(meta.get("cache_miss_tokens") or 0)
        if not (hit or miss):
            return total
        ct = int(meta.get("completion_tokens") or 0)
        w = CACHE_HIT_WEIGHT if cache_hit_weight is None else float(cache_hit_weight)
        rest = max(total - hit - miss - ct, 0)     # 未标注部分按全价
        return int(miss + rest + ct + hit * max(w, 0.0))
    except Exception:
        return 0


def tokens_of(resp) -> int:
    """**预算记账唯一入口**：返回应计入预算的 token（= `charged_tokens`）。

    ★ 2026-10-28 语义变更（保留函数名，避免 7 处调用点各改一遍 → 必然漏一处）：
      原实现直接返 `_meta.token_count`（含 92% 的缓存命中），导致"重复读前缀"
      被按全价累计、任务在预算看似耗尽时仍有大量剩余额度。
      现在改为**计费等效 token**；要原始吞吐请看 `tokens_total_of`。
    为什么沿用同一个函数名而不是新增：`tokens_of` 在全仓**只**服务于
    `LoopGuard.add_tokens`（预算闸），语义唯一，改这里即全路径生效。
    """
    return charged_tokens(resp)



#: ★ 2026-11-20（conv 999999952 实证）：**同工具不同参数的试探式检索**。
#:   已有 `probe_dup` 只挡"语义等价"的重复；t4 用 A8E/A8/整车/热管理/混动…
#:   连续调了 17 次 `query_projects`（外加 11 次 query_data_tree），每次参数都不同
#:   ⇒ 一次都没被挡。对"定位类"只读工具设**同轮次数上限**，到点回喂明确结论，
#:   逼模型基于已有回执作答或换工具，而不是继续猜关键词。
#:   为什么会扭伤：这类工具只读、无副作用，模型习惯"再换个词试试"，而每试一次
#:   都是一次完整 LLM 轮次（真机 t4 单独占 4 分钟）。
PROBE_TOOL_LIMITS = {
    "query_projects": 5, "query_data_tree": 8,
    "query_branches": 4, "query_model_versions": 4,
    "search_model_elements": 6,
}


class LoopGuard:
    """一次 loop 的护栏状态（纯状态机，便于在测试里直接构造各种进度）。"""

    def __init__(self, *, token_budget: int | None = DEFAULT_TOKEN_BUDGET,
                 tool_timeout_s: float | None = DEFAULT_TOOL_TIMEOUT_S,
                 dup_threshold: int = DEFAULT_DUP_THRESHOLD,
                 max_failed_rounds: int = 3,
                 tool_retries: int = DEFAULT_TOOL_RETRIES,
                 retry_timeout_s: float | None = DEFAULT_RETRY_TIMEOUT_S):
        self.token_budget = token_budget
        self.tool_timeout_s = tool_timeout_s
        self.dup_threshold = dup_threshold
        # ★ 2026-10-17（米爸反馈）：单次调用报错重试次数（0=不重试）
        self.tool_retries = max(0, int(tool_retries or 0))
        # ★ 2026-10-17（米爸反馈2）：重试尝试的单次等待上限（0/None=沿用 tool_timeout_s）
        self.retry_timeout_s = (float(retry_timeout_s) or None) if retry_timeout_s else None
        self.tool_retry_count = 0        # 实际发生的重试总次数（可观测）
        self.tokens_used = 0
        #: ★ 2026-10-28：**原始吞吐**（含缓存命中，仅供展示/排障，不参与预算判定）。
        self.tokens_total_raw = 0
        self.calls: list = []              # [(name, args, ok, elapsed_ms)]
        self._seen: dict = {}              # call_key -> 次数（**仅非弱结果计入**）
        self._results: dict = {}# call_key -> 最近一次结果文本（短路时回喂）
        #: ★ 2026-10-28：call_key -> 上次回执是否取全。没取全时**不允许短路**
        #:   （用不完整的结果冒充答案，比多跑一次更糟）。
        self._complete: dict = {}
        self.dup_short_circuits = 0
        self.probe_cap_hits = 0          # 试探式检索撞上限次数（2026-11-20）
        self.validation_sigs: dict = {}  # 校验回执签名 -> 次数（停滞短路）
        #: ★ 2026-11-24：pass 回执计数（"已达标还在重复确认" ⇒ 同样短路，但文案不同）
        self.validation_pass_sigs: dict = {}
        #: 最近一次停滞短路的类别（'block' | 'pass'），供调用方选正确文案
        self._last_stall_kind = ""
        self.validation_stalls = 0
        self.timeouts = 0
        self.stop_reason = ""
        # ── 失败轮预算（2026-10-09）：**只扣失败轮**，成功推进不扣。
        # 上界取DEFAULT_DUP_THRESHOLD 同一量级（3）：连续 3 轮失败就该收手，
        # 而成功轮再多也不限制（米爸 2026-10-09 纠正的语义）。
        self.max_failed_rounds = max_failed_rounds
        self.failed_rounds = 0

    # ── 重复检测 ──
    def probe_dup(self, name: str, args) -> int:
        """执行前问一次：这个 (工具,参数) 是不是已经在跑过？0 = 允许执行。

        ★ 2026-10-28：两道收紧（均为 conv 9726 真机数据驱动）
          ① 键用**语义键**（忽略 pageSize/pageNum/includeReadToken 等）——
             只差翻页参数的重复查询，实质是同一个问题；
          ② 只有**上次取全了**才短路 —— 上次 `hasMore=True` 说明还有数据，
             此时重跑是正当的（不许用半截结果冒充答案）。
        """
        k = call_key(name, args)
        if not self._complete.get(k, True):
            return 0
        return dedup_hit(self._seen, k, self.dup_threshold)

    def probe_cap(self, name: str) -> int:
        """定位类只读工具的本轮调用次数是否已到上限（0 = 放行）。"""
        limit = PROBE_TOOL_LIMITS.get(str(name or ""))
        if not limit:
            return 0
        n = sum(1 for nm, _ar, _ok, _ms in self.calls if nm == name)
        return n if n >= limit else 0

    def probe_cap_payload(self, name: str, n: int) -> dict:
        """撞上限时的**可行动回执**：带上已有结果摘要，而不是只说"不许查了"。"""
        limit = PROBE_TOOL_LIMITS.get(str(name or ""), n)
        prior = []
        for nm, ar, ok, _ms in reversed(self.calls):
            if nm != name or not ok:
                continue
            txt = str(self._results.get(call_key(nm, ar), "") or "").strip()
            if txt:
                prior.append(txt[:220])
            if len(prior) >= 3:
                break
        hint = ("【检索次数上限】本轮已调用 `%s` %d 次（上限 %d），系统不再执行新的关键词试探。"
                "请**基于下面已有的回执**给出结论；若都未命中，就如实写「未找到」并说明已试过的关键词，"
                "不要继续换词重试。\n\n已取得的回执（最近 3 条）：\n%s"
                % (name, n, limit, "\n---\n".join(prior) if prior else "（无有效回执）"))
        return {"ok": True, "result": hint, "_probe_cap": True, "cap": limit}

    def validation_stalled(self, name: str, result) -> bool:
        """校验结果**连续 3 次完全同签名** ⇒ 再跑也不会变（2026-11-20）。

        真机 t3 连跑 8 次 `sysml_v2_validate`，语义错始终 120 条、判据 report ——
        那次根因确实是校验器标准库未加载（假错），重试不解决问题，只烧 3 分钟。

        ⚠️ 2026-11-23 反例（conv 1000000000）：签名只含**计数**，不含错误内容与位置；
        于是「同一批真错因修法没触及而条数不降」与「环境假错」在签名上无法区分。
        当天 10 条语义错经 checker.jar 受控 A/B 证实为**真错**
        （`== '允许'` 单引号当字符串 ⇒ 改双引号后 0 错 pass），却被本判定归因为环境。
        ⇒ 短路本身保留（省轮次），但**回喂文案已改成中性自查**（见 `validation_stall_payload`），
        不再断言「根因在环境」。要真正区分二者，签名需加入首条错误 `msg`（未做：会放宽短路、
        与本轮「要更快」的目标相反，留作可选改造）。
        """
        if str(name or "") != "sysml_v2_validate":
            return False
        import re as _re
        txt = str(result if isinstance(result, str) else
                  (result or {}).get("result") or "")
        # ★★ 2026-11-24（run 69 实证，用户报"多次校验没变化、一直不结束"）：
        #   **只有硬错（block）才算停滞；pass / report（n_hard=0）一律视为健康并复位计数。**
        #
        #   为什么必须这样改 —— 踩到的是一处**死锁**：
        #     · 待办验收闸的判据是「verdict=pass 或 n_hard=0」；
        #     · 而本闸此前把**pass 回执**（两次 pass 的签名完全相同）也算成"连续 3 次同签名"
        #       ⇒ 用 `validation_stall_payload()` 把 pass 文本覆盖成"校验停滞、条数没变"；
        #     · 验收闸读最后一次校验回执 ⇒ 看不到 pass ⇒ 待办「校验至无硬错」永远打不上勾；
        #     · 完成度评估判"未完成" ⇒ 自动续跑 ⇒ 再校验 ⇒ 又被覆盖成停滞…（run 69 转了 5 次）
        #   ⇒ 表面症状是"反复校验没变化"，真因是**守卫自己把成功改写成失败**。
        #   语义澄清：停滞闸要防的是"改不动同一批硬错还反复重跑"，
        #   pass 不是"没变化"，是"已经达标"—— 达标就该闭嘴。
        #
        # ★★ 2026-11-24（run 76 实证，用户报"已经通过了为什么还调用 5 次"）：
        #   **`report` 绝不能与 `pass` 混为一谈** —— 上一版把「有语义错但无硬错」也归进
        #   pass 分支，而 run 76 那次 report 实际是 **643 条语义错**
        #   （`attribute '座舱热容' : '热容'` 之类类型引用全都不存在）。
        #   ⇒ 系统据此宣称"上一版代码 判据：pass、不要再校验"，把后续 5 次校验全部硬拦。
        #   后果比不拦更糟：模型**看得见 643 个错**却被告知"已通过、不许复验"，
        #   于是既修不了也验不了 ⇒ 用户看到"通过了却还在反复调用"。
        #   现在只有**真正的 pass**（词法/语法/语义全 0 或判据明写 pass）才算达标；
        #   report 走下方签名停滞判定（同批语义错改不动时仍会短路，但**不谎称已通过**）。
        _is_true_pass = ("判据：pass" in txt or "判据:pass" in txt or "判据：通过" in txt)
        if _is_true_pass:
            self.validation_sigs.clear()
            # ★ 2026-11-24（run 71 实证）：**重复的 pass 也要短路**。
            #   那轮 8 次校验全部 pass，每次白花 4.5~6s 工具时间 + 一轮 10s 的 LLM 往返
            #   （合计约 2 分钟），只因模型"每改一版就再校验一次"却不确认已经通过。
            #   与 block 停滞的区别：block 是"改不动"，pass 重复是"已经达标还在确认"，
            #   处置相同（不再执行、回喂明确结论），但**文案必须不同**（见下方 payload），
            #   否则会把"已通过"说成"停滞/有错"，误导模型去改本来正确的代码。
            _pk = "pass"
            _pn = int(self.validation_pass_sigs.get(_pk, 0)) + 1
            self.validation_pass_sigs[_pk] = _pn
            if _pn >= 2:
                self._last_stall_kind = "pass"
                return True
            return False
        # report（有语义错但无硬错）**不重置** pass 计数吗？
        #   要重置：语义错被改掉之后判据会从 report 变 pass，此时若还记着"上轮 pass"
        #   就会立刻触发 pass 硬拦。反过来 report 也不累加 pass 计数。
        #   （pass 计数只在真 pass 时递增，report 既不增也不清 —— 保留历史以便
        #     "pass → 改坏 → report" 后再次修好时不必重新数两次。）
        sigs = tuple(sorted(_re.findall(
            r"(?:词法|语法|语义|ERROR|判据)\s*[:：]?\s*([A-Za-z0-9]+)", txt)))
        if not sigs:
            return False
        key = "|".join(sigs)
        n = int(self.validation_sigs.get(key, 0)) + 1
        self.validation_sigs[key] = n
        if n >= 3:
            self._last_stall_kind = "block"
            return True
        return False

    def pass_confirmed_n(self) -> int:
        """本轮 run 内已出现多少次 pass 校验（0 = 还没通过）。

        ★ 2026-11-24（run 74 实证）：仅"回一句不要再校验"是**无效**的 ——
          模型收到提示后仍会改写代码再校验一次（实测第 3、4 次校验的代码
          前 2000 字完全相同，说明它并没有真改出需要复验的新东西）。
          ⇒ 调用方需要这个计数来做**硬拦截**（不再执行工具、并按次数收敛）。
        """
        return int((self.validation_pass_sigs or {}).get("pass", 0) or 0)

    @staticmethod
    def validation_pass_blocked_payload() -> dict:
        """★★ 2026-11-24：pass 已确认后，对**再次校验**的硬拦截回执。

        与 `validation_pass_repeat_payload` 的区别（两者都必须存在）：
          · repeat_payload —— 校验**执行完**才发现的重复（已花掉 4.5~6s 工具时间）；
          · blocked_payload —— 校验**执行前**就拦下（这才是"不再重复校验"的字面承诺）。
        run 74 实测：仅发 repeat_payload 时模型又调了 1 次并真跑了 7.7 秒。
        """
        return {"ok": True, "_validation_stall": True, "_validation_pass": True,
                "_validation_blocked": True,
                "result": ("【本轮校验已通过，本次校验未执行】上一版代码 `判据：pass`，"
                           "系统据此**不再执行**新的 sysml_v2_validate（本轮已生效）。\n"
                           "为什么拦：一轮内重复校验同一份产物不会产生新信息，只会拖长交付时间。\n"
                           "现在只有两条路，选一条：\n"
                           "  ① 代码没实质改动 ⇒ **直接交付**（把已通过的代码作为正文），"
                           "并用上一版 pass 作为待办「校验至无硬错」的验收回执置 completed；\n"
                           "  ② 确实换了产物（不同视图/不同包）⇒ 先说明「换了什么」，"
                           "系统才允许新一轮校验；不要靠反复微调同一份代码来碰运气。")}

    @staticmethod
    def validation_stall_blocked_payload() -> dict:
        """★★ 2026-11-24：停滞已确认后，对**再次校验**的硬拦截回执。

        run 77 实证：停滞回执只替换了文本、工具照样真跑 —— 模型连发 5 次，
        每次白花 5s。这里改成**执行前就拦**（`_validation_blocked` 让上层知道
        "本次未执行"，不要把它当真实校验结果读）。

        与 `validation_pass_blocked_payload` 的差别只在**出口建议**：
        停滞意味着"有未消除的错但改不动了"，所以出口是「如实标注未消除项」，
        不是"直接交付已通过的代码"。
        """
        return {"ok": True, "_validation_stall": True, "_validation_blocked": True,
                "result": ("【校验已判定停滞，本次校验未执行】同一批错误连续多次校验的"
                           "计数签名完全相同（条数不降）⇒ 系统不再执行新的 sysml_v2_validate。\n"
                           "现在请：\n"
                           "  ① **不要再调用校验**（调用会被直接拦下，不产生任何新信息）；\n"
                           "  ② 若能看见具体错因（如某类类型引用不存在），**只做一次针对性修改**，"
                           "改完在正文里说明改了哪一处，不要再靠反复校验试探；\n"
                           "  ③ 收尾时**如实列出仍未消除的语义错条目数与类型**，"
                           "不要声称「已全部修复/已通过校验」。")}

    @staticmethod
    def validation_pass_repeat_payload() -> dict:
        """★ 2026-11-24：**校验已通过**时的短路回执（与「停滞」严格区分）。

        为什么必须另写一份而不复用 `validation_stall_payload`：
          后者开头就写"连续多次校验得到的计数签名完全相同"+"逐条读诊断、看同一类写法"，
          而此刻代码**是通过的**。拿它回喂等于告诉模型"你还有错要改" ⇒
          它会去改本来正确的代码，或再校验一次确认（正是要终止的那个循环）。
        """
        return {"ok": True, "_validation_stall": True, "_validation_pass": True,
                "result": ("【校验已通过，无需重复校验】本次校验 `判据：pass`（无词法/语法错），"
                           "且上一版同样通过 —— **不要再调用 sysml_v2_validate**。\n"
                           "当前代码已是合格产物：\n"
                           "  ① 直接把已通过的代码作为交付正文（不要再改写后重校验）；\n"
                           "  ② 继续做剩下的步骤（写产物/回读确认/更新待办状态）；\n"
                           "  ③ 若待办里有「校验至无硬错」，用本次 pass 作为验收回执直接置 completed。")}


    @staticmethod
    def validation_stall_payload() -> dict:
        return {"ok": True, "_validation_stall": True,
                "result": ("【校验停滞，已短路】连续多次校验得到的**计数签名完全相同**"
                           "（词法/语法/语义条数与判据都不变）。\n"
                           "⚠️ 这只说明「条数没变」，**不等于根因在环境** —— 2026-11-23 实测反例："
                           "`== '允许'` 这类**单引号当字符串**的写法会让同一批 10 条语义错"
                           "在任何结构改动下都不下降，改法一错就永远撞同一签名。\n"
                           "请按顺序自查，不要用同样参数重跑：\n"
                           "  ① 逐条读诊断的「源码 + 诊断」，看是否**同一类写法**成批出现；\n"
                           "  ② 若是同一类，优先改**写法本身**（如单引号标识符 → 双引号字符串、"
                           "引用 usage 而非 def），而不是改周边结构或端口；\n"
                           "  ③ 改完再校验一次（签名一变即恢复执行）；仍不变则如实写"
                           "「这 X 条语义错未能消除」并列出已试过的假设，不要归因为环境。")}

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

    def replay_payload(self, name: str, args, n: int) -> dict:
        """短路时回喂给模型的**完整结果体**（既有结果 + 一句"这是重放"的说明）。

        ★ 2026-10-28（conv 9726）：此前 stream.py 的短路只回喂结果原文、
          不带任何说明 ⇒ 模型把重放当成了"一次新的查询"，于是**再查一遍**，
          而且始终没意识到"目标包早在第 7 轮的回执里就出现了"。
          说明句必须写死三件事：① 这是重放不是新结果；② 直接引用既有结论；
          ③ **不要再把"没查到"当结论**（空/0 命中不等于不存在）。
        """
        prev = self.last_result_for(name, args)
        hint = ("\n\n【重复调用已短路】你刚刚（本轮第 %d 次）发出了与之前**等价**的调用"
                "（忽略 pageSize/pageNum/includeReadToken 等翻页与票据参数），"
                "系统**没有重新执行**，直接回放上次的结果（见上）。"
                "请直接引用这份结果作答；**不要**据此再判「该元素不存在」——"
                "若上面出现过目标名，它就是存在的；若上面是空包，空 ≠ 不存在。"
                % int(n or 2))
        return {"ok": True, "result": (prev or "") + hint, "_dup": int(n or 2)}

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
    def add_tokens(self, n: int, *, raw: int | None = None) -> None:
        """累计预算消耗（`n` = 计费等效 token，见 `tokens_of`）。

        `raw` 可选传入**原始吞吐**（含缓存命中），只用于展示与"两条口径对拍"。
        """
        try:
            self.tokens_used += int(n or 0)
        except Exception:
            pass
        if raw is not None:
            try:
                self.tokens_total_raw += int(raw or 0)
            except Exception:
                pass

    def add_llm(self, resp) -> None:
        """记录一次 LLM 调用：预算口径 + 原始吞吐**一次算清**。

        ★ 单一入口，替代 7 处 `add_tokens(tokens_of(resp))` 的手工拼装：
          两笔账（计费等效 / 原始吞吐）若各写一份，迟早只有一笔被更新
          （本仓"同一语义多处各写一份必然错位"的既有教训）。
        """
        w = CACHE_HIT_WEIGHT
        try:
            from core import config as _cfg
            _w = _cfg.get("agent", "cache_hit_weight", None)
            if _w is not None and str(_w).strip() != "":
                w = float(_w)
        except Exception:
            pass
        self.add_tokens(charged_tokens(resp, w), raw=tokens_total_of(resp))

    def budget_left(self) -> bool:
        """预算是否仍可用。`stop_reason` 会被置为 `token_budget`（供回显与测试）。"""
        if budget_exhausted(self.tokens_used, self.token_budget):
            if not self.stop_reason:
                self.stop_reason = "token_budget"
            return False
        return True

    def budget_ratio(self) -> float:
        """已用预算占比（0.0~1.0+）。无预算（None/0）⇒ 返回 0.0。

        ★ P1-1（2026-10-28）：给"预算降级收尾"提供触发依据。
        为什么要这个数：实测 89 次评估里 **11 次（12%）是 token 耗尽硬停** ——
        用户感知是"突然断了"。有了占比就能在 70% 处**主动**降级收尾
        （写待续清单 + 告知还差什么），把"断"变成"分段推进"。
        """
        try:
            b = int(self.token_budget or 0)
            if b <= 0:
                return 0.0
            return float(self.tokens_used or 0) / float(b)
        except Exception:
            return 0.0

    # ── 失败轮预算（2026-10-09）────────────────────────────
    #★ 语义（米爸 2026-10-09 纠正）：
    #   「工具调用上限」要管的是**失败重试**，不是**正常推进**。
    #   ⇒ 成功的轮次**不扣预算**；只有失败/无效的轮次才扣。
    #
    # 为什么必须分：
    #   实测八视图 41 轮里 **29 轮（71%）是成功推进**，无效仅 12 轮（29%）。
    #   旧实现 `for _round in range(max_tool_rounds)` 把两者**一并计数**
    #   ⇒ v1 的 6 轮里只有 3 轮是真推进，却和"查了6 次都查不到"的场景
    #   **付同样的代价** —— 成功轮被失败轮"连坐"截断。
    #   这正是 v6 报 78 个语法错后进无工具收尾的直接原因（详见 docs 报告 §2缺陷 C）。
    def charge_failed_round(self, ok: bool) -> None:
        """登记一轮的成败：**只有失败轮**才扣失败预算。"""
        if ok:
            return
        self.failed_rounds += 1

    def failed_rounds_left(self) -> bool:
        """失败轮是否还有额度。`stop_reason` = `failed_rounds`。"""
        if self.max_failed_rounds and self.failed_rounds >= self.max_failed_rounds:
            if not self.stop_reason:
                self.stop_reason = "failed_rounds"
            return False
        return True

    # ── 兼容垫片（2026-10-30）──────────────────────────────────────────────
    # 为什么类上要留一个同名静态方法：`is_correctable_failure` 是**模块级纯函数**，
    # 但 2026-10-28 引入时两个调用点写成了 `_guard.is_correctable_failure(result)`
    # （execute.py / stream.py）⇒ 只在"有工具调用失败"时抛
    #   AttributeError: 'LoopGuard' object has no attribute 'is_correctable_failure'
    # 整轮被掀翻、用户侧看到"本轮未产出正文内容 / 任务终止"。
    # 讽刺之处：它恰好是**为那个分支**写的救兵，救兵自己先炸。
    # 两处调用点已改为直调模块函数；这里再补一个静态同名方法作为**兜底**，
    # 让任何遗漏/第三方写法退化为"正常返回布尔值"，而不是把整轮打成异常。
    # ⚠️ 它是 staticmethod（无 self）—— 判据只有一份，禁止在此复制逻辑。
    is_correctable_failure = staticmethod(is_correctable_failure)

    # ── 超时 ──
    def call(self, exec_fn, name: str, args, *, weak: bool = False):
        """带超时执行一次工具，并**统一记账**（耗时/结果文本/去重计数/超时数）。

        记账必须在这里而不能靠调用方再调`mark_done` —— 否则漏一处
        `mark_done` 就会让重复检测静默失效（而这类漏调用极难在测试里发现）。

        ★ 2026-10-17（米爸反馈）：**报错自动重试**（含超时）。
        重试判据：
          · ``error`` 键存在的失败 —— 即工具执行抛异常（瞬时故障：HTTP 502 / 连接重置 /
            服务重启窗口），**或** ``tool_timeout``（米爸第二反馈：超时也重试）。
            ⚠️ 超时重试的诚实代价：Python 杀不掉线程，前一次调用的副作用可能仍在后台
            执行 ⇒ 重试 = 同一副作用可能跑两遍。写类工具的安全网是各自的人机确认闸
            （L2 HIL），不在本层重复设防。
          · **不重试业务失败**：权限拒绝（``permission_denied``）、参数截断等
            没有 ``error`` 键 —— 重试不会有不同结果，只会白烧预算。
        等待时间上限（米爸：『需要时间上限，比如30s』）：首轮用完整 ``tool_timeout_s``
        给慢工具公平机会；**重试尝试一律收紧到 ``retry_timeout_s``**（默认 30s）——
        否则 120s × 3 次重试会把一轮拖到 8 分钟。
        重试对模型透明：它只看到最终一次的结果（重试细节在 stats 里可观测）。
        """
        t0 = time.time()
        _attempt = 0
        while True:
            _wait = self.tool_timeout_s if _attempt == 0 else (
                self.retry_timeout_s or self.tool_timeout_s)
            r = call_tool_guarded(exec_fn, name, args, timeout_s=_wait)
            _err = str(r.get("error") or "")
            if (not r.get("ok")) and _err and _attempt < self.tool_retries:
                _attempt += 1
                self.tool_retry_count += 1
                continue
            break
        if r.get("_t_timeout"):
            self.timeouts += 1
        _ok = bool(r.get("ok"))
        self.calls.append((name, args, _ok, int((time.time() - t0) * 1000)))
        if _ok:
            # 记结果文本：短路时要把既有结果回喂给模型（否则它无从作答）
            k = call_key(name, args)
            self._results[k] = str(r.get("result") or "")[:_TOOL_REPLAY_CAP]
            # ★ 2026-10-28：记"是否取全"——决定下次同键查询还能不能短路
            try:
                from agent.tool_result_notes import paging_complete as _pc
                self._complete[k] = bool(_pc(r.get("result")))
            except Exception:
                self._complete[k] = True
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
                # ★ 2026-10-28：同时给出**原始吞吐**（用户看"花了多少"）与
                #   `tokens_used`（预算口径 = 计费等效）。两者差一个数量级时，
                #   说明绝大部分是缓存命中的固定前缀 —— 这正是"预算误判"的证据。
                "tokens_total_raw": self.tokens_total_raw,
                "dup_short_circuits": self.dup_short_circuits, "timeouts": self.timeouts,
                "tool_retries": self.tool_retries, "tool_retry_count": self.tool_retry_count,
                "stop_reason": self.stop_reason}