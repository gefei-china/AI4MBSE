"""OpenAI 兼容 Provider（P1-3 插件化）：OpenAI-compatible /chat/completions 调用。

自 llm.py LLMClient 的真实调用逻辑抽取为独立 provider（P1-3 插件化）。
约定：接收 provider 配置 dict（DB llm_providers 行），提供 chat/stream/test_connection。
"""
import json
import logging
import re
import time
from typing import Optional

from ..base import BaseLLM

# ══════════════════════════════════════════════════════════════════
# 行内工具调用兜底解析（2026-10-09）
#
# 现象：部分模型不总走标准 `tool_calls` 字段，而是把工具调用
#      **以标记语言写进 content 文本**：
#        `<｜｜DSML｜｜ calls>`
#        `<｜｜DSML｜｜ invoke name="sysml_v2_validate">`
#        `  <｜｜DSML｜｜ parameter name="code" string="true">...</｜｜DSML｜｜>`
#        `<｜｜DSML｜｜ endinvoke>`
#      ⇒ 上游拿不到 tool_calls ⇒ 判定"无工具调用"⇒ 直接结束
#      ⇒ 模型想调的工具没调、代码没产出，**还会把上一轮代码贴出来**
#      （实测 N3 视图展开 state/parameter 产出雷同即此因）。
#
# 为什么修在 provider 层：这是**模型兼容性问题**，与哪个 Agent 无关
# ⇒ 任何用该模型的 Agent 都会踩；修在这里对所有调用方生效。
#
# 安全性：解析不出任何调用时**返回 None**，调用方保留原响应 ⇒ 零回归面。
# ══════════════════════════════════════════════════════════════════

#: 各种模型可能用的标记前缀（实测 deepseek 用DSML；其它模型可能用别的）
_INLINE_MARKERS = (r"DSML", r"tool_call", r"TOOL_CALL")

#: `... invoke name="xxx"` 的参数块起始
_RE_INVOKE = re.compile(
    r"invoke\s+name\s*=\s*[\"']([^\"']+)[\"']", re.I)
#: `... parameter name="xxx" [string="true"]>值<｜｜DSML｜｜ parameter>`
#: ★ 实测 deepseek-v4-flash 的真实格式（2026-10-09 从库里捞的原文）：
#:   `<｜｜DSML｜｜ parameter name="code" string="true">package ...<｜｜DSML｜｜ parameter>`
#:   ⇒ 结束标记带**词**（parameter / invoke / calls），且用**全角** `｜`
#:   ⇒ 不能用半角 `\|`；也不能只匹配 `>`（会一路吃到下一个标记）。
_RE_PARAM = re.compile(
    r"parameter\s+name\s*=\s*[\"']([^\"']+)[\"'][^>]*>(.*?)"
    r"<[｜|]{2}\s*DSML\s*[｜|]{2}\s*parameter\s*>",
    re.I | re.S)

#: 剥标记语言：`<｜｜DSML｜｜ ... >` 与 `<｜｜DSML｜｜ ... />`
_RE_STRIP_MARKUP = re.compile(
    r"<[｜|]{2}\s*DSML\s*[｜|]{2}[^>]*?>[\s\S]*?"
    r"<[｜|]{2}\s*DSML\s*[｜|]{2}\s*(?:parameter|invoke|calls"
    r"|endinvoke|endcalls|end_calls)\s*>"
    r"|<[｜|]{2}\s*DSML\s*[｜|]{2}[^>]*/>",
    re.I)


def _salvage_inline_tool_calls(data: dict):
    """把 content 里的行内工具调用还原成标准 `tool_calls`。

    返回**新的**响应 dict；**识别不到则返回 None**（调用方保留原响应）。
    ★ 只在「原本没有 tool_calls」时出手——标准路径优先，绝不覆盖。
    """
    try:
        choices = data.get("choices") or []
        if not choices:
            return None
        msg = choices[0].get("message") or {}
        if msg.get("tool_calls"):          # 标准路径已有 ⇒ 不碰
            return None
        content = msg.get("content") or ""
        if not any(m in content for m in _INLINE_MARKERS):
            return None
        names = _RE_INVOKE.findall(content)
        if not names:
            return None

        # 按 invoke 出现顺序切参数块：每个 invoke 之后的 parameter 归它所有
        calls, pending = [], None
        for seg in re.split(r"(?=invoke\s+name\s*=)", content, flags=re.I):
            m = _RE_INVOKE.search(seg)
            if not m:
                continue
            args = {}
            for pname, pval in _RE_PARAM.findall(seg):
                v = pval.strip()
                # 能当 JSON 就解，否则当字符串（工具入参几乎都是字符串）
                try:
                    args[pname] = json.loads(v)
                except (ValueError, TypeError):
                    args[pname] = v
            calls.append({"id": f"inline_{len(calls) + 1}",
                          "type": "function",
                          "function": {"name": m.group(1),
                                       "arguments": json.dumps(args, ensure_ascii=False)}})
        if not calls:
            return None

        # 把标记语言从 content 里剥掉，剩余文本仍作为正文（若有）
        cleaned = _RE_STRIP_MARKUP.sub("", content).strip()
        new = dict(data)
        new["choices"] = [dict(choices[0])]
        new["choices"][0] = dict(choices[0])
        new["choices"][0]["message"] = dict(msg)
        new["choices"][0]["message"]["content"] = cleaned
        new["choices"][0]["message"]["tool_calls"] = calls
        logging.getLogger(__name__).info(
            "inline tool_calls salvaged: %s", [c["function"]["name"] for c in calls])
        return new
    except Exception:                # noqa: BLE001 —— 兜底解析失败不得影响主链路
        return None

_log = logging.getLogger("mbse.llm")

# 上下文额度告警去重：同一 (provider_id, context_window, guard) 组合**只报一次**。
# 本模块是全平台 LLM 调用的唯一出口，若每轮都刷会把日志淹掉 → 必须去重。
_ctx_warned: set = set()


def _ctx_guard() -> str:
    """读 `llm.context_window_guard`；读取失败或非法值一律回落 `clamp`（= 改动前行为）。"""
    try:
        from core import config as _cfg
        v = str(_cfg.get("llm", "context_window_guard", "clamp") or "clamp").strip().lower()
    except Exception:
        return "clamp"
    return v if v in ("clamp", "warn", "off") else "clamp"


def _est_input_tokens(messages) -> int:
    """估算输入 token 数（仅供超窗留痕）。异常 → 0（绝不阻断主链路）。"""
    try:
        from core.token_counter import count_messages_tokens
        return int(count_messages_tokens(messages))
    except Exception:
        return 0


# ── 2026-10-10 形态规则层（thinking按「任务形态」自动判定，而非按名字枚举）─────
def _c_module():
    """取 core.config 模块（延迟导入，避免模块级循环依赖）；失败返回 None。"""
    try:
        from core import config as _c
        return _c
    except Exception:
        return None


def _looks_structured_output(messages) -> bool:
    """判断这轮调用是否**要求模型输出纯结构化结果（JSON）** ⇒ 应关思考。

    ★ **为什么必须关**（2026-10-10 实测真故障）：
      开启 `rag.rerank_enabled` 后重排"完全没生效"（顺序一字未变），
      根因是每次调用都抛 `JSONDecodeError: Expecting value: line 1 column 1`，
      被 `services/rag_rerank.py` 的`except: return hits[:top_k]` **静默吞掉**。
      真因：**reasoning 模型会在 JSON 之前输出思考**，于是 `json.loads` 失败。
      ⇒ 不是重排实现有问题，是**思考污染了结构化输出**。

    ★ **为什么按 messages 内容判断、而不是往off 名单里加 intent**：
      实测全库有 **28 个**要求结构化输出的 intent
      （planner / skill_route / intent_detect / task_decompose / plan_eval …），
      逐个加名单**必然随系统演进而漂移** ——
      这正是形态规则层要解决的老问题（`thinking_policy.off` 对 27 个 Agent 名覆盖率 0/27）。
      ⇒ 判据取自**提示词本身**，新增调用点**零配置**被覆盖。

    识别方式（保守，宁可不判也不误判）：
      只在 **system 消息**里找"必须输出结构化结果"的强指令短语，
      不看 user 消息（用户可能只是提问里带了 `{}`）。
    """
    try:
        for m in (messages or []):
            if not isinstance(m, dict):
                continue
            if m.get("role") != "system":
                continue
            c = str(m.get("content") or "")
            if not c:
                continue
            if any(k in c for k in (
                    "只输出 JSON", "只输出JSON", "仅输出 JSON",
                    "输出 JSON", "输出JSON",
                    "严格输出 JSON", "必须是 JSON", "返回 JSON")):
                return True
    except Exception:
        return False
    return False


def _looks_like_probe(intent: str) -> bool:
    """判断 intent 是否属于「探针语义」（模型自选工具的决策轮）。

    ★ **为什么必须有这个判据**（2026-10-10 实测发现）：
      形态规则 1原本只看「带 tools + max_tokens 小」，但真库显示
      **`view_expansion`（视图生成）的 max_ct 只有 76** —— 它带工具、正文短，
      却是**重产码**任务（思考确有收益）⇒ 只看 max_tokens 会把它误关。

      ⇒ 加一层「语义」判据：只有**名字本身就表明是探针/路由/探测**的 intent
      才允许被规则关。这让规则对**新增 intent 也是安全的**：
      新 Agent 起个正常业务名（"活动图生成"）⇒ 不会命中；
      真要新增探针类 intent，走名单显式配即可（不依赖规则猜）。

    匹配方式：**子串**（`probe`/`detect`/`route`/`routing`/`probe_model` 等），
    不用全等 —— 因为探针类 intent 的命名习惯是加后缀（`probe_plain`/`probe_model`）。
    """
    try:
        s = str(intent or "").lower()
    except Exception:
        return False
    if not s:
        return False
    for k in ("probe", "detect", "route", "routing", "classify", "dispatch"):
        if k in s:
            return True
    return False


def _decide_by_shape(*, has_tools: bool, max_tokens: int, messages,
                     config_mod, intent: str = ""):
    """按**任务形态**判定思考开关。返回 True=开/ False=关/ None=不干预。

    ★ **为什么要有这一层（可维护性，米爸 2026-10-10 提出）**
    `_intent` 是**动态**的：`orchestration.py:111` 用 `agent_def.name` 当 intent，
    而 `agents` 表有 **27 个** active Agent（名字含中文，如「活动图生成」）。
    实测：现有 `thinking_policy.off` 名单 4 个，对 27 个 Agent 名的**覆盖率 = 0/27**
    ⇒纯名单制**必然随系统演进而漂移**，漏加一个 = 该任务思考失控
      （实测极端形态：思考占 99.2%、耗时 4017 秒）。

    ⇒ 这一层让**新增 intent 天然被覆盖**，不需要任何人记得改配置。

    ## 规则（每条都对应一个实测事实，不是拍脑袋）
    1. **「探针轮」= 带 tools + 上限小 + intent 名字带探针语义** ⇒ 关思考
       实测所有带 tools 的决策类调用思考占 **58%~98%** 而正文只产几十~几百 token
       （`probe` 97.6% / `crypto_probe` 79.1% / `routing` 81.6%）。
       ★ **真实 A/B 已验证安全**（flashY / deepseek-v4-flash，2026-10-10，
         真库 id=10035~10038）：关思考后 **tool_calls 保留**，
         难任务 token 省 78%、耗时省 73%。
       ⚠️ **为什么还要intent 语义这一层**：`view_expansion`（视图生成）实测
         max_ct 只有 76 —— 它"带工具 + 上限小"却**是重产码**，
         只看 max_tokens 会误伤⇒ 追加 `_looks_like_probe()` 语义判据。
    2. **「廉价摘要轮」= 输出上限小** ⇒ 关思考
       实测重产码任务 `plan_refine` 上限 8195、`plan_summary` 8192，
       而廉价任务上限普遍 ≤ 1224。
       ⚠️ **默认 0=不启用**：无 tools 的调用里混着大量重产码任务
         （实测 `plan_refine` 平均 5697 token），误判风险高、收益未验证。

    ## 为什么默认关闭
    `thinking_shape_enabled` 默认 **off**：规则是**新增能力**，
    在没有真机 A/B 证据前不该改变任何现有行为（`probe` 那类可能丢 tool_calls 的风险，
    见 `verify_real_thinking_ab.py`）。米爸拍板后再逐级放开。
    """
    if config_mod is None:
        return None
    try:
        if not config_mod.as_bool("llm", "thinking_shape_enabled", False):
            return None
    except Exception:
        return None
    try:
        # 规则 1：探针轮（带工具 + 输出上限小）⇒ 关
        # ⚠️ **max_tokens 单独用不可靠**（2026-10-10 实测发现）：
        #   `view_expansion`（视图生成，**重产码**、思考确有收益）实测 max_ct 只有 **76**
        #   ⇒ 只要"带 tools + 上限小"就判关，会把它一起关掉。
        #   ⇒ 追加第二条件：intent 必须是**已知的探针语义**。
        #   判据来自真实 A/B（flashY，2026-10-10）：
        #     · 探针轮（带工具，模型自选工具）：关思考后 tool_calls **保留**（1→1）
        #     · 难任务带工具：思考 824→0、耗时 6.6s→1.8s，tool_calls 仍保留
        #   ⇒ 「带工具 + 探针语义」这个组合是**实测安全**的。
        probe_mt = int(config_mod.get("llm", "shape_probe_max_tokens", 1200) or 1200)
        if has_tools and int(max_tokens or 0) <= probe_mt and _looks_like_probe(intent):
            return False
        # 规则 1.5：**结构化输出轮**⇒ 关思考（2026-10-10 实测真故障）
        #   实测：`rag_rerank`（检索重排，要求模型"只输出 JSON"）
        #   调用 28 次全部 `JSONDecodeError: Expecting value: line 1 column 1`
        #   ⇒ 静默回退原排序 ⇒ **开了 rerank_enabled 也没生效**（顺序完全未变）。
        #   根因：reasoning 模型在 JSON 前输出思考 ⇒ `json.loads` 失败。
        #   ⚠️ **全库有 28 个要求结构化输出的 intent**
        #     （planner / skill_route / intent_detect / task_decompose / …）
        #     ⇒ **绝不逐个加名单**（那必然漂移，正是形态规则要解决的老问题）；
        #     改为**按 messages 内容自动识别**（见 `_looks_structured_output`）。
        if _looks_structured_output(messages):
            return False
        # 规则 2：廉价输出轮（无工具且上限极小）⇒ 关
        cheap_mt = int(config_mod.get("llm", "shape_cheap_max_tokens", 0) or 0)
        if not has_tools and cheap_mt > 0 and int(max_tokens or 0) <= cheap_mt:
            return False
    except Exception:
        return None
    return None


def _warn_context_once(cfg, cw, mt, est_in, guard, why) -> None:
    """上下文额度留痕（同 provider + cw + guard 只报一次；自身异常静默，不影响调用）。

    这是「假天花板」的**唯一可见出口**：DB `llm_providers.context_window` 可能只是保守
    配置而非模型真实上限（实测 id=1 配 cw=8192，上游在 in=6575 + out=5841 = 12416 时仍 200）。
    没有这条日志时，「输出被悄悄截短」与「模型本来就写不长」在现象上无法区分。
    """
    try:
        key = (cfg.get("id"), cw, guard)
        if key in _ctx_warned:
            return
        _ctx_warned.add(key)
        from core.token_counter import input_budget
        _log.warning(
            "[context_window] %s｜provider=%s(id=%s) model=%s｜context_window=%s "
            "max_tokens生效=%s 估算输入=%s 输入余量=%s guard=%s"
            "｜若该 context_window 是保守配置，置 llm.context_window_guard=warn/off 即可放开（warn 只告警不截断）",
            why, cfg.get("name"), cfg.get("id"), cfg.get("model_name"),
            cw, mt, est_in, input_budget(cw, mt, 512), guard,
        )
    except Exception:
        pass


class OpenAICompatProvider(BaseLLM):
    """OpenAI-compatible Chat Completions provider。

    配置（cfg）：DB llm_providers 行 —— base_url / api_key / model_name / model_type /
    max_tokens / context_window / temperature / name。
    """

    name = "openai_compat"

    def __init__(self, cfg: Optional[dict] = None):
        self.cfg = cfg or {}
        self.stats = {"real": 0, "mock": 0, "last_provider": "", "last_used_mock": True}

    # ── 对外接口（BaseLLM 契约）──
    # 2026-09-17 S4：三个采样参数默认值改为 None，让「调用方显式传参 > DB 配置 > 内置默认」成立。
    # 旧的 `max_tokens=4096 / temperature=0.3` 默认值 + `cfg.get(...)` 优先的写法，会导致
    # 调用方显式传的 max_tokens 被 DB 配置（provider 71/72 = 16384）**顶掉**——即"传了也不生效"
    # （这正是历史上有 3 次 completion 精确等于 16,384 的原因）。
    # 语义：显式传参才覆盖 DB；不传则完全沿用原行为（仍取 DB 配置，没有 DB 配置才用内置默认）。
    def chat(self, messages, model=None, temperature=None, max_tokens=None, stream=False,
             tools=None, thinking=False, **kwargs):
        """OpenAI-compatible 调用。返回 OpenAI 响应 dict（附 _meta）；stream=True 返回 SSE 生成器。"""
        t0 = time.time()
        cfg = self.cfg
        provider_name = cfg.get("name", "未配置")
        # 优先级：调用方显式 model > DB model_name > "-"
        model_name = model or cfg.get("model_name") or "-"
        cw = cfg.get("context_window") or 8192
        # 优先级：调用方显式 max_tokens > DB max_tokens > 4096
        mt = max_tokens if max_tokens is not None else cfg.get("max_tokens", 4096)
        # ── context_window 守卫（2026-09-20：由「静默截断」改为「可配置 + 留痕」）────────
        # 原实现只有一句**静默截断**（`mt` 一旦超过 `cw` 就把它赋值成 `cw`）：把 DB 的
        # context_window 当硬上限，但该值可能是**保守配置而非模型真实上限**
        # （实测 id=1 配 cw=8192，上游在 in=6575 + out=5841 = 12416 时照常 200 返回）
        # → 后果是**静默压低输出上限，且现象上与「模型本来就写不长」无法区分**。
        # 三档（`llm.context_window_guard`；默认 clamp **与改动前逐字节等价**）：
        #   clamp = 超窗截到 cw（原行为）+ 首次触发 WARNING 留痕
        #   warn  = 不截断，只 WARNING（把「假天花板」暴露出来，交上游判定是否接受）
        #   off   = 完全不介入（静默，等价于删掉这一段）
        # 顺带**接线**：`core/token_counter.input_budget`（ctx - mt - safety）此前全仓无生产
        # 调用点（死代码），这里用它的余量判据识别「本次请求必然超窗」这一原先完全不可见的情形。
        _guard = _ctx_guard()
        _est_in = _est_input_tokens(messages)
        if mt > cw:
            if _guard == "clamp":
                mt = cw
            if _guard != "off":      # off = 完全不介入（含不告警），否则「静默」语义不成立
                _warn_context_once(cfg, cw, mt, _est_in, _guard, "max_tokens 超过 context_window")
        elif _guard != "off" and _est_in > 0 and _est_in + mt > cw:
            _warn_context_once(cfg, cw, mt, _est_in, _guard, "估算输入 + max_tokens 超过 context_window")
        # ★★ 2026-10-09 **reasoning 思考预算**（根治 541s 超时的真因，不是加限制）
        #
        # 实测根因（详见 docs/工具调用轮次耗尽 报告 §11）：
        #   思考与正文**共享 max_tokens**，而思考模型（qwen3.8-*）默认开着思考：
        #     · 10-05 之前（非 reasoning 模型）：思考占输出 **4%**  → 给用户 96%
        #     · 今天（reasoning 模型）：思考占输出 **63%** → 给用户只剩 **37%**
        #     · 今天慢调用（>120s）：思考占 **72%**
        #   极端一例：143 秒那次 `completion=6425` / `reasoning=6322`
        #   ⇒ **98% 的额度用来思考，只给用户 103 个 token**。
        #   ⇒ 按 ~20~80 ms/token 线性拉长耗时 ⇒ 撞超时 ⇒ 重试 ⇒ 更慢（乘法）。
        #
        # 两个旋钮（默认 0 = 不启用 ⇒ 零行为漂移）：
        #   llm.thinking_budget         给思考的**独立上限**（token）
        #   llm.thinking_reserve_tokens **给正文的保底**（思考不得吃光这部分）
        #   实际压算：max_tokens = min(max_tokens, thinking_budget + reserve)
        #   —— 用「思考上限 + 正文保底」反推正文能拿多少，
        #      比单纯砍 max_tokens 精确（不会无谓截短非思考模型）。
        _think_cap = 0
        _reserve = 0
        try:
            from core import config as _c
            _think_cap = int(_c.get("llm", "thinking_budget", 0) or 0)
            _reserve = int(_c.get("llm", "thinking_reserve_tokens", 0) or 0)
        except Exception:
            pass
        if _think_cap > 0:
            _allowed = _think_cap + _reserve
            if mt > _allowed:
                mt = _allowed
        payload = {
            "model": model_name,
            "messages": messages,
            "temperature": temperature if temperature is not None else cfg.get("temperature", 0.3),
            "max_tokens": mt,
            "stream": stream,
        }
        if tools:
            payload["tools"] = tools
        # 模型参数模板（DB llm_providers.model_params）：top_p / top_k / thinking_mode
        mp = {}
        try:
            mp = json.loads(cfg.get("model_params") or "{}")
        except Exception:
            mp = {}
        # ★ P1-b（2026-10-10）**Agent 级 model_params 覆盖 provider 级**
        #   为什么：`agents.model_params` 列一直有、前端一直在存，但**全仓无消费点**
        #   ⇒ 在 Agent 表单里配思考档位/temperature **配了不生效**（死配置）。
        #   语义：同名键以 **Agent 级为准**，其余沿用 provider 级（浅合并，Agent 优先）。
        #   ⚠️ 调用方显式传参仍高于两者（语义分层：调用点 > Agent > provider > 模型默认）。
        try:
            _amp = kwargs.get("_model_params") or kwargs.get("model_params") or {}
            if isinstance(_amp, dict) and _amp:
                _merged = dict(mp)
                _merged.update(_amp)
                mp = _merged
        except Exception:
            pass
        if mp.get("top_p") is not None and mp["top_p"] != "":
            try:
                payload["top_p"] = float(mp["top_p"])
            except (TypeError, ValueError):
                pass
        if mp.get("top_k") is not None and mp["top_k"] != "":
            try:
                payload["top_k"] = int(mp["top_k"])
            except (TypeError, ValueError):
                pass
        # ── 思考模式（2026-10-09 重构：**三态**）────────────────────────
        #★★ 原实现有两个缺陷，米爸的"实施"要求先修它们：
        #
        # 缺陷 1（★致命）：`if thinking:` 只在**开启**时发字段，
        #   关闭时**什么都不发** ⇒ 模型端按**默认**（= 开启）思考
        #   ⇒ **我方的「关闭思考」从来没生效过**（实测思考占比 63% 就是证据）。
        #   标杆实测要求**显式**下发：Qwen `enable_thinking:false` /
        #   DeepSeek `thinking:{type:disabled}` / Claude `thinking:{type:disabled}`。
        #缺陷 2（★陷阱）：若把"关闭"改成**总是**发disabled，
        #   则全链路（`chat()` 默认 `thinking=False`，实测**无一处**传 True）
        #   **全部被关掉思考** ⇒ 重产码任务（视图生成，正文 1.5 万token）
        #   也会一起受害 —— **修缺陷 1 时若不做三态，会造出更大的事故**。
        #
        # ⇒ 三态（按 llm.thinking_policy，**per-intent**，实测数据驱动）：
        #   on  列表 → 显式 enabled
        #   off 列表 → 显式 disabled
        #   其余     → **不发该字段**（= 保持改动前行为，零漂移）
        _tmode = mp.get("thinking_mode")
        # ★ P1-b（2026-10-10）记录 **thinking_mode 是「显式指定」还是「默认值」**。
        #   为什么必须区分：`thinking_mode=关闭` 与 `thinking=False`(默认值) 在
        #   `_decision` 里都表现为 False，但**语义完全相反**：
        #     · 默认 False  ⇒「没表态」⇒ **不发字段**（保持改动前行为，防缺陷 2）
        #     · 显式「关闭」⇒「表态了」⇒ **必须发 disabled**（否则配了不生效）
        #   实测踩过：Agent 级 thinking_mode=关闭 传进来后`thinking=False`，
        #   但下发条件只看 `_off` 名单 ⇒ **Agent 级的"关闭"根本没生效**（死配置）。
        _tmode_explicit = _tmode in ("开启", "关闭")
        if _tmode == "开启":
            thinking = True
        elif _tmode == "关闭":
            thinking = False

        # per-intent 分流（2026-10-09；粒度选**任务维度**，标杆三家全是 per-request）
        #─��� 2026-10-10 新增第0 层「形态规则」——
        # ★ **为什么必须有这一层（可维护性）**：`_intent` 是**动态**的 ——
        #   `orchestration.py` 用 `agent_def.name` 当 intent，而 agents 表有 **27 个**
        #   active Agent（名字含中文，如「活动图生成」），**外加 skills/tools 未来新增**。
        #   实测：现有off 名单 4 个，对27 个 Agent 名的**覆盖率 = 0/27**。
        #   ⇒ 纯名单制**必然漂移**：漏加一个 = 该任务思考失控（实测可达 99.2% / 4017 秒）。
        #   ⇒ 规则层按**任务形态**判定，新增 intent 天然被覆盖，**不需要人记得配**。
        #
        # 规则只做"加法"：命中规则且未被名单显式覆盖时才生效，绝不夺走例外权。
        _decision = None            # None=不干预/  True=开/  False=关
        _who = kwargs.get("_intent") or kwargs.get("intent") or ""
        try:
            from core import config as _c
            _pol = json.loads(_c.get("llm", "thinking_policy", "{}") or "{}")
        except Exception:
            _pol = {}
        _off = set(_pol.get("off") or [])
        _on = set(_pol.get("on") or [])
        # 第 0 层：形态规则（默认关闭，逐级放开；见 `_decide_by_shape`）
        _cfg_mod = _c_module()
        _by_shape = _decide_by_shape(
            has_tools=bool(tools),
            max_tokens=mt,
            messages=messages,
            config_mod=_cfg_mod,
            intent=_who,
        )
        if _who:
            if _who in _off:
                _decision = False
            elif _who in _on:
                _decision = True
        # ★ 名单**未命中**时才看形态规则（名单优先级最高）
        if _decision is None and _who and _by_shape is not None:
            _decision = _by_shape
        if _decision is None:
            # 没命中分流表：沿用 provider 级 thinking_mode 或调用方的 thinking
            _decision = bool(thinking)
        # ★ 决定依据只记到**本地日志**（`payload` 会原封不动作为请求体发给上游，
        #   塞任何自定义键都会被当成非法参数 → 400；这个坑很隐蔽，因为本地测试不经过真实网关）。
        #   日志的价值：出现"某个 intent 的思考异常"时，能直接看出是名单配的、还是形态规则判的。
        _reason = ("policy_off" if (_who and _who in _off) else
                   "policy_on" if (_who and _who in _on) else
                   ("shape:" + ("off" if _by_shape is False else "on" if _by_shape else "none"))
                   if (_who and _by_shape is not None) else
                   "provider_default")
        if _decision or (_tmode_explicit and _tmode == "开启"):
            # ★ 同理：显式「开启」也必须真发出去（否则 Agent 配的"开启"也是死配置）
            payload["thinking"] = {"type": "enabled"}
        elif (_off and _who in _off) or _by_shape is False or _tmode_explicit:
            # ★ 只在「**显式命中 off 列表**」/「**形态规则判关**」/「**thinking_mode 显式表态**」
            #   三者之一成立时才发 disabled —— 其余「关闭」一律不发字段（保持改动前行为）。
            #   这是避免"全链路被一起关掉"的关键（见上方缺陷 2）。
            payload["thinking"] = {"type": "disabled"}
            if _who:
                _log.info("[thinking] 关思考｜intent=%s 依据=%s has_tools=%s max_tokens=%s",
                          _who, _reason, bool(tools), mt)
        headers = {
            "Authorization": f"Bearer {cfg.get('api_key', '')}",
            "Content-Type": "application/json",
        }
        url = str(cfg.get("base_url", "")).rstrip("/") + "/chat/completions"
        if stream:
            return self._stream_api(url, payload, headers, provider_name, model_name, t0)
        import httpx
        # ★ 2026-10-09：单次超时从**硬编码 180** 改为**可配置**（默认仍 180，零行为漂移）。
        #   为什么要改（实测 v4 与 v1 连续两次命中）：
        #     原 `timeout=180` 与 `llm.retry_times=2` 相乘 ⇒
        #     **单次调用最坏 180×3 + 退避 ≈ 541.5 秒**（实测 545s 吻合）。
        #     用户等 9 分钟换来**一个异常**（conv 9244 落库 0 行、什么都没拿到）。
        #   ⚠️ 改小前先确认：慢模型（deepseek-v4-pro 实测单次 121s）别调太紧。
        try:
            from core import config as _c
            _to = float(_c.get("llm", "http_timeout_s", 180))
        except Exception:
            _to = 180.0
        # ★★ 2026-10-30（P0 缺口②·deadline 传播）：把本次调用的超时**压到剩余墙钟以内**。
        #   为什么必须在**这一层**做（实测得出，不是理论）：
        #     墙钟只在主循环轮次边界检查是不够的 —— 单次 LLM 调用能一口气阻塞几十秒。
        #     实测 conv 9771（墙钟 8s）：卡在 planner 的 LLM 调用 **56,978 ms**，
        #     而 planner 跑在编排主循环**之前** ⇒ 循环里的检查一次都没跑到，
        #     用户等了 61s 才拿到"无终态帧"。
        #   语义：未登记 deadline（脚本/测试/非会话调用）⇒ 原样返回 `_to`，
        #        **零行为变更**；登记了 ⇒ 取 min(原 timeout, 剩余)。
        #   `cap_timeout` 返回 0 = 预算已耗尽 ⇒ 直接放弃本次调用（下方 raise），
        #   让上层走"基于已有信息收尾"而不是继续烧一次完整调用。
        try:
            from core import runtime_state as _rs
            _capped = _rs.cap_timeout(_to)
            if _capped <= 0:
                raise ValueError("整轮墙钟预算已耗尽，放弃本次 LLM 调用（rl=deadline）")
            # 只在**真的压过**时留痕（正常路径零噪声）：
            #   排查"墙钟为什么没生效"时，唯一能回答的是"cap 当时返回了 8 还是 180"。
            #   实测背景：conv 9774 的 task_decompose 单次跑 57s，需确认是 cap 没生效
            #   还是 httpx 没按 timeout 中断。
            if _capped < _to:
                try:
                    print("[llm_deadline] timeout %.1fs → %.1fs（剩余墙钟 %.1fs）"
                          % (_to, _capped, _rs.thread_deadline_left() or 0), flush=True)
                except Exception:
                    pass
            _to = _capped
        except ValueError:
            raise
        except Exception:
            pass      # 运行时状态模块不可用 ⇒ 保持原 timeout（绝不因为观测设施挂掉而阻断调用）
        resp = httpx.post(url, json=payload, headers=headers, timeout=_to)
        data = resp.json()
        if resp.status_code != 200 or not data.get("choices"):
            raise ValueError(f"LLM API {resp.status_code}: {str(data)[:200]}")
        # ★ 2026-10-09：部分模型（实测 deepseek-v4-flash）**不总是**把工具调用
        #   放进标准 `tool_calls` 字段，而是把`<｜｜DSML｜｜ calls>` 这类标记
        #   **直接写进 content 文本**。
        #   ⇒ 上游 `msg.get("tool_calls")` 拿不到 ⇒ 判定为"无工具调用"⇒ 直接结束，
        #   模型想调的工具没调、代码没产出，**还会把上一轮的代码贴出来**。
        #   ⚠️ 这是 **provider 层通用缺口**（任何用该模型的 Agent 都会踩），
        #   不是某个 Agent 的问题 ⇒ 修在这里，对所有 OpenAI 兼容模型生效。
        #   解析不出内容时**逐字返回原响应**（零回归面）。
        _salvaged = _salvage_inline_tool_calls(data)
        if _salvaged:
            data = _salvaged
        self.stats["real"] += 1
        self.stats["last_provider"] = provider_name
        self.stats["last_used_mock"] = False
        data["_meta"] = {
            "provider": provider_name, "model": model_name,
            "used_mock": False, "latency_ms": int((time.time() - t0) * 1000),
            # 上下文额度留痕（2026-09-20）：让「天花板到底是多少、本次有没有被截」可从响应侧审计，
            # 不必再去猜 DB 配置。纯新增键，不改任何既有键的语义。
            "context_window": cw, "max_tokens_effective": mt,
            "est_input_tokens": _est_in, "context_guard": _guard,
        }
        return data

    def _stream_api(self, url, payload, headers, provider_name="", model_name="", t0=None):
        """SSE 流式调用：逐行透传 `data: ...` 帧。

        P0-a（2026-10-02）两处改动，都是"把静默失败暴露出来"：
        ① **非 200 立刻抛错**。此前不分状态码直接 `iter_lines()` —— 上游返回 400
           （余额不足 / 参数错 / 鉴权失败）时错误正文不带 `data:` 前缀 ⇒ **零帧输出且不抛异常**
           ⇒ 生成器"正常结束" ⇒ 上层落一条**成功**记录、用户看到**空回复**。
           这正是「表面成功、实际为空」的静默形态（P1-26 的 402 就走过这条路）。
        ② `stream_options.include_usage` 改为**可配置**（`llm.stream_include_usage`，默认关）。
           实测 DeepSeek 流式**默认就发 usage 帧**，故默认无需该字段；部分 OpenAI 兼容端点
           （如百炼）不带则只能拿字符估算值 —— 需要精确 token 时打开此开关即可。
        """
        import httpx
        try:
            from core import config as _cfg
            _include_usage = bool(_cfg.as_bool("llm", "stream_include_usage", False))
        except Exception:
            _include_usage = False
        if _include_usage:
            payload = dict(payload)
            payload["stream_options"] = {"include_usage": True}
        # 2026-10-30：流式同样受剩余墙钟约束（与上方非流式同源，避免两套口径）。
        #   ⚠️ 流式一次连接可能持续很久（边生成边收），把 timeout 压到"剩余预算"
        #   相当于给整个流一个硬上限 —— 这正是我们要的：到点必须收尾。
        _sto = 120
        try:
            from core import runtime_state as _rs2
            _c2 = _rs2.cap_timeout(_sto)
            if _c2 <= 0:
                raise ValueError("整轮墙钟预算已耗尽，放弃本次流式 LLM 调用（rl=deadline）")
            _sto = _c2
        except ValueError:
            raise
        except Exception:
            pass
        try:
            with httpx.stream("POST", url, json=payload, headers=headers, timeout=_sto) as resp:
                if resp.status_code != 200:
                    _body = ""
                    try:
                        _body = resp.read().decode("utf-8", "ignore")[:300]
                    except Exception:
                        pass
                    raise ValueError(f"LLM 流式 API {resp.status_code}: {_body}")
                for line in resp.iter_lines():
                    if line.startswith("data: "):
                        yield line + "\n\n"
        except Exception as e:
            yield f"data: {{\"choices\":[{{\"delta\":{{\"content\":\"\"}}}}]}}\n\n"
            raise e from None
        finally:
            self.stats["real"] += 1
            self.stats["last_provider"] = provider_name
            self.stats["last_used_mock"] = False
            self.stats["last_latency_ms"] = int((time.time() - (t0 or time.time())) * 1000)

    # 1x1 像素 PNG（base64 内联，零外部依赖）：multimodal 探测的最小图像载荷
    _PX_PNG_B64 = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
        "AAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
    )

    def test_connection(self, cfg: Optional[dict] = None) -> dict:
        """连通性测试（2026-10-16 起 = **真实可用验证**，废除"只列模型"的假绿探测）。

        背景（2026-10-16 实测）：阿里云 MaaS embedding 配额 403 时，GET /models 仍返回 200
        ⇒ chat 类型原实现（先 /models 后兜底 chat）会给出"连接成功"的假绿；用户在
        列表页看到成功、编辑页（表单同样走本函数）看到 403，两边都没说谎，是口径不一。
        现统一为真实最小调用：
        - embedding:  POST /embeddings（原逻辑保持，本来就是真调用）
        - chat:       POST /chat/completions，"ping"，max_tokens=5 —— 证明配额/模型真的可用
        - multimodal: POST /chat/completions，文本 + 1x1 PNG data URI —— 证明图像输入真被接受
        失败时返回上游原始错误（含 403 配额信息），不再有任何"列到模型就算通"的路径。
        """
        c = cfg or self.cfg
        if not c.get("api_key"):
            return {"ok": False, "error": "API key not configured", "mock": True}
        if "{" in str(c.get("base_url", "")) or "}" in str(c.get("base_url", "")):
            return {"ok": False, "error": "Base URL 含未替换的占位符（如 {WorkspaceId}），请替换为实际值后再测试"}
        mtype = str(c.get("model_type") or "chat").strip().lower()
        try:
            import httpx
            headers = {"Authorization": f"Bearer {c['api_key']}"}
            if mtype == "embedding":
                url = str(c["base_url"]).rstrip("/") + "/embeddings"
                resp = httpx.post(url, json={"model": c["model_name"], "input": ["ping"]},
                                  headers=headers, timeout=15)
                if resp.status_code == 200:
                    return {"ok": True, "status": 200, "method": "embeddings"}
                return {"ok": False, "status": resp.status_code, "method": "embeddings",
                        "error": f"HTTP {resp.status_code}: {self._resp_error(resp)}"}
            url = str(c["base_url"]).rstrip("/") + "/chat/completions"
            if mtype == "multimodal":
                payload = {
                    "model": c["model_name"],
                    "messages": [{"role": "user", "content": [
                        {"type": "text", "text": "回复：OK"},
                        {"type": "image_url", "image_url": {
                            "url": "data:image/png;base64," + self._PX_PNG_B64}},
                    ]}],
                    "max_tokens": 8,
                }
                method = "multimodal"
            else:
                payload = {
                    "model": c["model_name"],
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 5,
                }
                method = "chat"
            resp = httpx.post(url, json=payload, headers=headers, timeout=30)
            if resp.status_code == 200:
                return {"ok": True, "status": 200, "method": method}
            return {"ok": False, "status": resp.status_code, "method": method,
                    "error": f"HTTP {resp.status_code}: {self._resp_error(resp)}"}
        except UnicodeEncodeError:
            return {"ok": False,
                    "error": "配置含非 ASCII 字符（中文等）：请检查 Base URL / API Key / 模型名是否含中文，API Key 应为服务商签发的真实密钥（sk- 开头）"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    @staticmethod
    def _resp_error(resp) -> str:
        """从 OpenAI 兼容错误响应中提取可读错误信息（百炼返回 {"error":{"message":...}}）。"""
        try:
            j = resp.json()
        except Exception:
            return (resp.text or "")[:200]
        if isinstance(j, dict):
            err = j.get("error")
            if isinstance(err, dict):
                return err.get("message") or json.dumps(err, ensure_ascii=False)[:200]
            if isinstance(err, str):
                return err[:200]
            return (j.get("message") or str(j)[:200])
        return str(j)[:200]
