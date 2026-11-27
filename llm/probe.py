# -*- coding: utf-8 -*-
"""模型「功能自检」—— 回答「这个模型到底能不能用」，而不是「TCP 通不通」。

## 为什么需要（2026-11-27 需求原话）

> 模型管理中「测试」功能是否完备，点击测试只确认连接成功，功能是否正常其实无法知道，
> 需要进行真实的使用验证。

改动前 `test_connection` 只发 `"ping" / max_tokens=5`，判据是 `HTTP 200`。它证明的是
"上游愿意回一个包"，**证明不了本平台真的能用它干活**。四种"200 但没用"的真实形态：

| 形态 | 旧判据结果 | 实际后果 |
|---|---|---|
| 200 但 `content` 为空（纯思考模型 / 被截断） | ✅ 通过 | 每轮回答空白，用户以为系统坏了 |
| 200 但 **不支持流式** | ✅ 通过 | 对话页一直转圈（`stream=true` 直接 400/断开） |
| 200 但 **不支持 tool_calls** | ✅ 通过 | Agent 工具链全程空转，只能干聊 |
| 200 但上游**静默换成别的模型** | ✅ 通过 | 配额/能力与配置不符，测的模型不是用的模型 |

⇒ 本模块逐项真跑：**基础对话 / 流式 / 工具调用 / JSON 输出 / 视觉输入 / 实际模型名**，
每项给独立判据 + 实际回显片段，`ok` 只由**必需项**决定。可选能力不支持记 `warn`
（不判失败，但明确写出"该能力不可用，Agent 相关功能会退化"）。

向量模型（`model_type='embedding'`）同理逐项真跑：**直连向量化 → 语义区分度 →
与库内索引维度一致 → 平台真实的 Embedder 通路**。只判"有没有向量"等于对话侧只看
HTTP 200：常量向量、维度与存量不一致（检索侧静默 0 命中）、熔断期静默降级 bigram
这三种"有向量但检索废掉"的形态都会判绿。见本文件 `_check_embed_*`。

## 与业务统计的关系

探针本身的 token 是**真实成本**，但不应污染"每轮业务成本"口径。
所以记账走 `llm_usage_stats`，`intent="chat_probe"` —— `core.probe_mark.is_probe_intent`
会把它标成 `is_probe=1`，统计侧默认排除（`probe_where()`），要查也在。
"""
from __future__ import annotations

import json
import math
import time
from typing import Any, Optional

# 1x1 透明 PNG：视觉通道探针用最小合法图像，避免真的花掉图片 token。
PX_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=="
)

_LEVEL_ORDER = {"pass": 0, "warn": 1, "fail": 2}


def _mk(key: str, name: str, required: bool = True) -> dict:
    return {"key": key, "name": name, "required": required,
            "level": "fail", "ok": False, "detail": "", "latency_ms": 0, "sample": ""}


def _done(c: dict, level: str, detail: str, sample: str = "", latency_ms: int = 0, **extra) -> dict:
    c["level"] = level
    c["ok"] = level == "pass"
    c["detail"] = detail
    c["sample"] = sample
    c["latency_ms"] = latency_ms
    if extra:
        c.update(extra)
    return c


def _err_text(resp) -> str:
    """从 OpenAI 兼容错误体里取可读消息（百炼/深寻/OpenAI 都是 {"error":{"message"}}）。"""
    try:
        j = resp.json()
    except Exception:
        return (resp.text or "")[:200]
    if isinstance(j, dict):
        e = j.get("error")
        if isinstance(e, dict):
            return str(e.get("message") or json.dumps(e, ensure_ascii=False))[:300]
        if isinstance(e, str):
            return e[:300]
        return json.dumps(j, ensure_ascii=False)[:300]
    return str(j)[:300]


def _content_of(payload: dict) -> str:
    """取回答正文：兼容 content / reasoning_content（纯思考模型的正文可能在后者）。"""
    try:
        msg = (payload.get("choices") or [{}])[0].get("message") or {}
    except Exception:
        return ""
    c = msg.get("content")
    if isinstance(c, list):      # 少数实现返回 [{"type":"text","text":...}]
        c = " ".join(b.get("text", "") for b in c if isinstance(b, dict))
    return str(c or msg.get("reasoning_content") or "")


def _tool_calls_of(payload: dict) -> list:
    try:
        msg = (payload.get("choices") or [{}])[0].get("message") or {}
    except Exception:
        return []
    return msg.get("tool_calls") or []


def _record(provider: dict, intent: str, pt: int, ct: int, latency_ms: int,
            ok: bool, err: str = "") -> None:
    """探针用量记账（is_probe 由 intent 判定，统计侧默认排除，但可查）。

    记账失败绝不阻断自检：自检的产出是"能不能用"的判断，不是账单。
    `_no_record=True`（门禁用）时完全不写库 —— 门禁连的是生产库，不该留下任何行。
    """
    if (provider or {}).get("_no_record"):
        return
    try:
        from database import db_conn
        from core.probe_mark import is_probe_intent
        with db_conn() as conn:
            conn.execute(
                "INSERT INTO llm_usage_stats (provider_id, provider_name, model_name, intent, used_mock, "
                "prompt_tokens, completion_tokens, total_tokens, finish_reason, latency_ms, "
                "call_failed, failure_kind, failure_msg, is_probe) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (provider.get("id") or 0, str(provider.get("name") or "")[:80],
                 str(provider.get("model_name") or "")[:80], intent, 0,
                 int(pt), int(ct), int(pt) + int(ct), "probe", int(latency_ms),
                 0 if ok else 1, "" if ok else "probe_failed",
                 (err or "")[:200], is_probe_intent(intent)))
    except Exception:
        pass


# ── 各项探针 ────────────────────────────────────────────────────────────────

def _check_chat(client, cfg, headers, model: str) -> dict:
    c = _mk("chat", "基础对话（能否产出非空正文）")
    t0 = time.time()
    try:
        r = client.post(cfg["_chat_url"], headers=headers, json={
            "model": model,
            "messages": [{"role": "user", "content": "用一句话说明什么是需求工程。"}],
            "max_tokens": 64, "temperature": 0,
        }, timeout=45)
    except Exception as e:
        return _done(c, "fail", f"请求异常：{type(e).__name__}: {e}", latency_ms=int((time.time() - t0) * 1000))
    lat = int((time.time() - t0) * 1000)
    if r.status_code != 200:
        return _done(c, "fail", f"HTTP {r.status_code}: {_err_text(r)}", latency_ms=lat)
    try:
        j = r.json()
    except Exception:
        return _done(c, "fail", "响应不是 JSON（可能被网关/代理改写）", (r.text or "")[:120], lat)
    txt = _content_of(j)
    if not txt.strip():
        return _done(c, "fail",
                     "HTTP 200 但**正文为空**：这类模型当前不可用于对话（思考模型的正文可能在 "
                     "reasoning_content，本函数已兼容；仍为空说明上游没产出）", "", lat)
    u = j.get("usage") or {}
    _record(cfg.get("_provider") or {}, "chat_probe", u.get("prompt_tokens") or 0,
            u.get("completion_tokens") or 0, lat, True)
    return _done(c, "pass", f"正文 {len(txt)} 字，返回模型 {j.get('model') or model}",
                 txt[:80].replace("\n", " "), lat, returned_model=str(j.get("model") or ""))


def _check_stream(client, cfg, headers, model: str) -> dict:
    c = _mk("stream", "流式输出（对话页依赖）")
    t0 = time.time()
    got, done_mark, first_ms = 0, False, 0
    try:
        with client.stream("POST", cfg["_chat_url"], headers=headers, json={
            "model": model, "messages": [{"role": "user", "content": "数到三"}],
            "max_tokens": 32, "temperature": 0, "stream": True,
        }, timeout=60) as r:
            if r.status_code != 200:
                body = r.read()[:300].decode("utf-8", "replace")
                return _done(c, "fail", f"HTTP {r.status_code}: {body[:200]}",
                             latency_ms=int((time.time() - t0) * 1000))
            for line in r.iter_lines():
                if not line:
                    continue
                s = line.decode("utf-8", "replace") if isinstance(line, bytes) else str(line)
                if s.startswith("data:"):
                    payload = s[5:].strip()
                    if payload == "[DONE]":
                        done_mark = True
                        break
                    try:
                        j = json.loads(payload)
                    except Exception:
                        continue
                    delta = ((j.get("choices") or [{}])[0] or {}).get("delta") or {}
                    if (delta.get("content") or delta.get("reasoning_content")):
                        if got == 1:
                            first_ms = int((time.time() - t0) * 1000)
                        got += 1
    except Exception as e:
        return _done(c, "fail", f"流式请求异常：{type(e).__name__}: {e}",
                     latency_ms=int((time.time() - t0) * 1000))
    lat = int((time.time() - t0) * 1000)
    if got <= 0:
        return _done(c, "fail", "没有收到任何内容增量（SSE 帧为空）——不支持流式或协议不符", "", lat)
    if not done_mark:
        # 有增量但没 [DONE]：多数客户端能靠 EOF 收口，判 warn 不判死
        return _done(c, "warn", f"收到 {got} 个增量但未见 [DONE] 结束标记（EOF 收口可容忍）",
                     f"首字 {first_ms}ms", lat)
    return _done(c, "pass", f"收到 {got} 个增量 + [DONE]", f"首字 {first_ms}ms", lat)


def _check_tools(client, cfg, headers, model: str, required: bool) -> dict:
    c = _mk("tools", "工具调用 / 函数调用" + ("" if required else "（该模型未标 tools 标签，仅告知）"),
            required=required)
    t0 = time.time()
    tools = [{
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "查询指定城市的天气",
            "parameters": {"type": "object",
                           "properties": {"city": {"type": "string", "description": "城市名"}},
                           "required": ["city"]},
        },
    }]
    try:
        r = client.post(cfg["_chat_url"], headers=headers, json={
            "model": model, "max_tokens": 128, "temperature": 0,
            "messages": [{"role": "user", "content": "北京天气如何？请调用工具查询。"}],
            "tools": tools, "tool_choice": "auto",
        }, timeout=45)
    except Exception as e:
        return _done(c, "fail" if required else "warn",
                     f"请求异常：{type(e).__name__}: {e}", latency_ms=int((time.time() - t0) * 1000))
    lat = int((time.time() - t0) * 1000)
    if r.status_code != 200:
        lvl = "fail" if required else "warn"
        return _done(c, lvl, f"HTTP {r.status_code}: {_err_text(r)} —— 该模型不接受 tools 参数，"
                             "Agent 工具链会全程空转", latency_ms=lat)
    try:
        j = r.json()
    except Exception:
        return _done(c, "fail", "响应不是 JSON", (r.text or "")[:120], lat)
    calls = _tool_calls_of(j)
    if not calls:
        lvl = "fail" if required else "warn"
        return _done(c, lvl, "模型回了正文但没有 tool_calls ⇒ 不支持可靠的工具调用，"
                             "Agent 无法执行动作（只能纯对话）", _content_of(j)[:80], lat)
    fn = (calls[0] or {}).get("function") or {}
    args_raw = fn.get("arguments")
    try:
        args = json.loads(args_raw) if isinstance(args_raw, str) else (args_raw or {})
        if not isinstance(args, dict):
            raise ValueError("arguments 不是对象")
    except Exception as e:
        return _done(c, "fail", f"tool_calls 存在但 arguments 不可解析（{e}）——"
                                "工具入参会报错", str(args_raw)[:80], lat)
    _record(cfg.get("_provider") or {}, "chat_probe", 0, 0, lat, True)
    return _done(c, "pass", f"命中 {fn.get('name')}，入参 {json.dumps(args, ensure_ascii=False)[:60]}",
                 "", lat)


def _check_json(client, cfg, headers, model: str) -> dict:
    c = _mk("json", "结构化 JSON 输出", required=False)
    t0 = time.time()
    try:
        r = client.post(cfg["_chat_url"], headers=headers, json={
            "model": model, "max_tokens": 80, "temperature": 0,
            "messages": [{"role": "user",
                          "content": '只输出 JSON，不要解释：{"intent": "design", "ok": true}'}],
            "response_format": {"type": "json_object"},
        }, timeout=45)
    except Exception as e:
        return _done(c, "warn", f"请求异常：{type(e).__name__}: {e}", latency_ms=int((time.time() - t0) * 1000))
    lat = int((time.time() - t0) * 1000)
    if r.status_code != 200:
        return _done(c, "warn", f"HTTP {r.status_code}: {_err_text(r)} —— 不支持 response_format，"
                                "结构化输出需靠提示词约束（可接受）", latency_ms=lat)
    txt = _content_of(r.json())
    try:
        obj = json.loads(txt.strip().strip("`").replace("json\n", "", 1))
        if not isinstance(obj, (dict, list)):
            raise ValueError("不是对象/数组")
    except Exception:
        return _done(c, "warn", "返回内容不是可解析 JSON（提示词约束下仍可用，但稳定性弱）",
                     txt[:80], lat)
    return _done(c, "pass", "response_format=json_object 生效且可解析", txt[:80], lat)


def _check_vision(client, cfg, headers, model: str) -> dict:
    c = _mk("vision", "视觉输入（多模态）", required=False)
    t0 = time.time()
    try:
        r = client.post(cfg["_chat_url"], headers=headers, json={
            "model": model, "max_tokens": 24, "temperature": 0,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "这张图是什么颜色？一个词回答。"},
                {"type": "image_url",
                 "image_url": {"url": "data:image/png;base64," + PX_PNG_B64}},
            ]}],
        }, timeout=45)
    except Exception as e:
        return _done(c, "warn", f"请求异常：{type(e).__name__}: {e}", latency_ms=int((time.time() - t0) * 1000))
    lat = int((time.time() - t0) * 1000)
    if r.status_code != 200:
        return _done(c, "fail", f"HTTP {r.status_code}: {_err_text(r)} —— 标记为 multimodal 但不接受"
                                "图像输入，上传图片会被丢弃", latency_ms=lat)
    txt = _content_of(r.json())
    if not txt.strip():
        return _done(c, "fail", "接受了图片但正文为空 —— 视觉通道不可用", "", lat)
    return _done(c, "pass", "图像输入被接受并给出回答", txt[:60], lat)


def _check_model_name(payload_ok: bool, returned: str, configured: str) -> dict:
    c = _mk("model_name", "实际返回模型名", required=False)
    if not payload_ok:
        return _done(c, "warn", "基础对话未成功，无法核对模型名")
    r, cf = str(returned or "").strip(), str(configured or "").strip()
    if not r:
        return _done(c, "warn", "上游未返回 model 字段，无法核对")
    if r == cf:
        return _done(c, "pass", f"上游返回 {r}，与配置一致")
    return _done(c, "warn", f"配置 {cf}，上游实际返回 {r}（别名或路由到别的模型，能力/计费可能与预期不符）")


# ── 对外入口 ────────────────────────────────────────────────────────────────

def probe(cfg: dict, deep: bool = True) -> dict:
    """对单个 provider 做功能自检。

    `cfg` 必须是 provider 字典（含 id/name/base_url/api_key/model_name/model_type/tags）。
    `deep=False` 只跑基础对话（省 token；用于列表页的快速探活）。
    返回 {ok, level, checks[], latency_ms, model, base_url, error?}
    """
    cfg = dict(cfg or {})
    if not cfg.get("api_key"):
        return {"ok": False, "level": "fail", "checks": [],
                "error": "API Key 未配置：不发起任何请求（也不返回 Mock 内容）"}

    base = str(cfg.get("base_url") or "").strip().rstrip("/")
    model = str(cfg.get("model_name") or "").strip()
    mtype = str(cfg.get("model_type") or "chat").strip().lower()
    if not base or not model:
        return {"ok": False, "level": "fail", "checks": [], "error": "Base URL / 模型名未配置"}
    if "{" in base or "}" in base:
        return {"ok": False, "level": "fail", "checks": [],
                "error": "Base URL 含未替换的占位符（如 {WorkspaceId}），请替换后再测"}
    cfg["_chat_url"] = base + "/chat/completions"
    cfg["_provider"] = cfg

    try:
        import httpx
    except Exception as e:
        return {"ok": False, "level": "fail", "checks": [], "error": f"httpx 不可用：{e}"}
    try:
        with httpx.Client(timeout=60) as client:
            return _run(client, cfg, model, mtype, deep)
    except UnicodeEncodeError:
        return {"ok": False, "level": "fail", "checks": [],
                "error": "配置含非 ASCII 字符（中文等）：请检查 Base URL / API Key / 模型名"}


def _run(client, cfg: dict, model: str, mtype: str, deep: bool) -> dict:
    headers = {"Authorization": f"Bearer {cfg['api_key']}"}
    t0 = time.time()

    if mtype == "embedding":
        checks = _embedding_checks(client, cfg, model, deep)
    else:
        checks = [_check_chat(client, cfg, headers, model)]
        if deep:
            checks.append(_check_stream(client, cfg, headers, model))
            tags = cfg.get("tags")
            if isinstance(tags, str):
                try:
                    tags = json.loads(tags)
                except Exception:
                    tags = []
            tags = [str(t).lower() for t in (tags or [])]
            checks.append(_check_tools(client, cfg, headers, model,
                                       required=("tools" in tags or "function_call" in tags)))
            checks.append(_check_json(client, cfg, headers, model))
            if mtype == "multimodal":
                checks.append(_check_vision(client, cfg, headers, model))
            # 核对实际模型名（用基础对话返回的 model 字段 —— 不从文案里反解析）
            checks.append(_check_model_name(checks[0]["level"] == "pass",
                                            checks[0].get("returned_model") or "", model))

    fails = [c for c in checks if c["level"] == "fail"]
    warns = [c for c in checks if c["level"] == "warn"]
    ok = not fails
    level = "fail" if fails else ("warn" if warns else "pass")
    return {
        "ok": ok, "level": level, "checks": checks,
        "latency_ms": int((time.time() - t0) * 1000),
        "model": model, "base_url": cfg.get("base_url"), "model_type": mtype,
        "summary": _summary(checks, ok),
    }


def _summary(checks: list, ok: bool) -> str:
    if not checks:
        return "未执行任何检查"
    p = sum(1 for c in checks if c["level"] == "pass")
    w = sum(1 for c in checks if c["level"] == "warn")
    f = sum(1 for c in checks if c["level"] == "fail")
    head = "可用" if ok else "不可用"
    tail = "；".join(c["name"] for c in checks if c["level"] == "fail") if f else ""
    return f"{head}（{p} 项通过 / {w} 项告知 / {f} 项失败）" + (f"：{tail}" if tail else "")


# ── 向量模型：判据不是「有没有向量」，而是「能不能检索」─────────────────────
# 2026-11-27 补：此前 embedding 只判「非空 + 维度一致」，等价于对话侧只看 HTTP 200。
# 实测三种「有向量但检索废掉」的形态会被判绿：
#   ① 常量向量（每句一样）⇒ 余弦恒为 1，排序无意义（返回 200、条数、维度全对）；
#   ② 维度与已入库向量不同 ⇒ `knowledge_pipeline/search.py` 里 `len(vec) != len(q)` 直接
#      continue ⇒ 向量通道**静默 0 命中**，不报错、不掉日志，用户只看到「知识库明明有」；
#   ③ 上游通但平台通路挂了（模型名带空格 / 熔断期 / 静默降级 bigram TF）⇒ 测的是 API，
#      不是本平台 —— 这类才是「点了测试说可用，用起来检索不到」的主因。
# 所以：E1 直连向量化 → E2 语义区分度 → E3 与库内索引维度一致 → E4 走平台 Embedder 真通路。
_EMB_PROBES = [
    ("q", "需求工程里的黑盒活动图怎么画"),
    ("rel", "黑盒活动图描述系统与外部参与者的交互，不展示内部实现细节"),
    ("neg1", "红烧肉要先焯水，再用小火慢炖一小时"),
    ("neg2", "足球比赛下半场补时三分钟"),
]
_EMB_MARGIN = 0.10          # 相关句余弦须比无关句高出这么多，否则判「无区分度」
_EMB_REL_FLOOR = 0.30       # 相关句绝对相似度下限（低于此只告知，不判死）


def _vec_cos(a, b) -> float:
    """余弦相似度（全零向量返回 0，不产生除零）。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    d = sum(float(x) * float(y) for x, y in zip(a, b))
    na = math.sqrt(sum(float(x) * float(x) for x in a))
    nb = math.sqrt(sum(float(x) * float(x) for x in b))
    if not na or not nb:
        return 0.0
    return d / (na * nb)


def _check_embed_api(client, cfg: dict, model: str):
    """E1 直连向量化：条数对应、非空、维度一致、非全零。返回 (check, vectors)。"""
    c = _mk("embedding", "向量化（条数/维度/非零向量）")
    url = str(cfg["base_url"]).rstrip("/") + "/embeddings"
    texts = [t for _, t in _EMB_PROBES]
    t0 = time.time()
    try:
        r = client.post(url, headers={"Authorization": f"Bearer {cfg['api_key']}"},
                        json={"model": model, "input": texts}, timeout=45)
    except Exception as e:
        return _done(c, "fail", f"请求异常：{type(e).__name__}: {e}",
                     latency_ms=int((time.time() - t0) * 1000)), []
    lat = int((time.time() - t0) * 1000)
    if r.status_code != 200:
        return _done(c, "fail", f"HTTP {r.status_code}: {_err_text(r)}", latency_ms=lat), []
    try:
        j = r.json()
        vecs = [d.get("embedding") for d in (j.get("data") or [])]
    except Exception:
        j, vecs = {}, []
    if not vecs or not all(isinstance(v, list) and v for v in vecs):
        return _done(c, "fail", "HTTP 200 但没有向量（data/embedding 为空）——不能用于检索",
                     latency_ms=lat), []
    dims = {len(v) for v in vecs}
    if len(dims) > 1:
        return _done(c, "fail", f"同一批输入的向量维度不一致 {sorted(dims)}", latency_ms=lat), []
    if len(vecs) < len(texts):
        return _done(c, "fail",
                     f"发 {len(texts)} 条只回 {len(vecs)} 条向量：入库/检索的条与条会错位",
                     latency_ms=lat), []
    if all(max(abs(float(x)) for x in v) < 1e-9 for v in vecs):
        return _done(c, "fail", "返回全零向量：余弦恒为 0，检索无意义", latency_ms=lat), []
    u = j.get("usage") or {}
    _record(cfg, "chat_probe", u.get("prompt_tokens") or 0, 0, lat, True)
    dim = sorted(dims)[0]
    _done(c, "pass", f"维度 {dim}，{len(texts)} 条输入全部返回", latency_ms=lat)
    return c, vecs


def _check_embed_semantics(vecs: list) -> dict:
    """E2 语义区分度：相关句必须比无关句更近，且不能是常量向量。"""
    c = _mk("embed_semantics", "语义区分度（相关句须比无关句更近）")
    if len(vecs) < 4:
        return _done(c, "warn", "向量化未通过，未做区分度判定")
    q, rel, n1, n2 = vecs[0], vecs[1], vecs[2], vecs[3]
    s_rel = _vec_cos(q, rel)
    s_neg = max(_vec_cos(q, n1), _vec_cos(q, n2))
    s_cross = _vec_cos(rel, n1)
    nums = (f"相关 {s_rel:.3f} / 无关 {s_neg:.3f}（差 {s_rel - s_neg:+.3f}）"
            f"，相关-无关句 {s_cross:.3f}")
    # 先判"两类文本根本不可分"（共线），再判"能分但分反了"——两者文案不同，用户该做的事也不同
    if s_cross > 0.99:
        return _done(c, "fail", f"向量几乎共线（{nums}）——疑似常量向量，任何排序都无意义，不能用于检索")
    if s_rel - s_neg < _EMB_MARGIN:
        return _done(c, "fail",
                     f"无区分度：相关句不比无关句更近（要求差 ≥ {_EMB_MARGIN}；{nums}）——不能用于检索")
    if s_rel < _EMB_REL_FLOOR:
        return _done(c, "warn",
                     f"能区分但相关句绝对相似度偏低（< {_EMB_REL_FLOOR}；{nums}），"
                     f"排序可能仍可用，建议用真实语料复核")
    return _done(c, "pass", f"相关句显著更近，向量具备语义检索能力（{nums}）")


def _index_vector_dim():
    """库内已入库向量的维度（取众数，避免个别脏行影响判定）。返回 (dim, 条数)。"""
    try:
        from database import db_conn
        with db_conn() as conn:
            row = conn.execute(
                "SELECT json_array_length(embedding) AS d, count(*) AS n FROM document_chunks "
                "WHERE embedding IS NOT NULL AND json_valid(embedding) "
                "GROUP BY d ORDER BY n DESC LIMIT 1").fetchone()
        if row and row[0]:
            return int(row[0]), int(row[1])
    except Exception:
        pass
    try:                                    # 无 JSON1 时的兜底：抽样算长度
        from database import db_conn
        with db_conn() as conn:
            cnt: dict = {}
            for (v,) in conn.execute(
                    "SELECT embedding FROM document_chunks WHERE embedding IS NOT NULL LIMIT 500"):
                try:
                    d = len(json.loads(v))
                except Exception:
                    continue
                cnt[d] = cnt.get(d, 0) + 1
        if cnt:
            d = max(cnt, key=cnt.get)
            return d, cnt[d]
    except Exception:
        pass
    return 0, 0


def _check_embed_index(dim: int, is_effective: bool) -> dict:
    """E3 与库内索引维度一致：维度不符时检索侧静默跳过，向量通道 0 命中。"""
    c = _mk("embed_index", "与已入库向量维度一致")
    if not dim:
        return _done(c, "warn", "向量化未通过，未核对索引维度")
    idx_dim, n = _index_vector_dim()
    if not idx_dim:
        return _done(c, "warn", "库内暂无可核对的已入库向量（新库/空知识库），无法判断换模型的影响")
    if idx_dim == dim:
        return _done(c, "pass", f"与库内 {n} 条向量一致（{dim} 维）")
    msg = (f"库内 {n} 条向量为 {idx_dim} 维，本模型 {dim} 维 —— 检索侧 "
           f"`len(vec)!=len(q)` 会直接跳过，向量通道会**静默 0 命中**（不报错、不掉日志）")
    if is_effective:
        return _done(c, "fail", msg + "；该模型正是平台当前默认向量模型 ⇒ 现在就已经检索不到")
    return _done(c, "warn", msg + "；改为默认前必须全量重建索引（知识库重索引）")


def _effective_embed_provider():
    """平台真正会用的向量 provider（与 `Embedder._probe_api` 的选择口径一致）。"""
    try:
        from database import db_conn
        with db_conn() as conn:
            row = conn.execute(
                "SELECT id, base_url, model_name FROM llm_providers "
                "WHERE status='active' AND api_key != '' AND model_type='embedding' "
                "ORDER BY is_default DESC, id LIMIT 1").fetchone()
        return dict(row) if row else None
    except Exception:
        return None


def _check_embed_platform(cfg: dict, dim: int, is_effective: bool) -> dict:
    """E4 平台真实通路：经 Embedder 取向量，确认不是 bigram 降级、不是熔断期。"""
    c = _mk("embed_platform", "平台真实通路（Embedder → 真向量，非 bigram 降级）")
    if not dim:
        return _done(c, "warn", "向量化未通过，未验证平台通路")
    if not is_effective:
        return _done(c, "warn", "非平台当前默认向量模型，未验证平台通路（设为默认后再测才真跑）")
    try:
        from database import db_conn
        from knowledge_pipeline.embedder import Embedder, EmbedQuotaExceeded
    except Exception as e:                       # noqa: BLE001
        return _done(c, "warn", f"未加载到 Embedder：{type(e).__name__}: {e}")

    # 探针不得改变运行时状态：熔断窗口与探活缓存先快照、跑完还原
    # （否则「点一次测试」就会让服务进入 10 分钟 bigram 降级态，这是测试本身制造的故障）。
    snap = (Embedder._PROBE_CACHE, Embedder._PROBE_STAMP,
            Embedder._QUOTA_BLOCK_UNTIL, Embedder._UNAVAIL_BLOCK_UNTIL, Embedder.LAST_QUOTA_ERROR)
    t0 = time.time()
    try:
        Embedder._PROBE_CACHE = None
        Embedder._PROBE_STAMP = 0.0
        with db_conn() as conn:
            # 两条文本：单条会命中 _QUERY_CACHE，测到的可能不是本次网络结果
            vecs, ver = Embedder(conn).embed_with_version(
                [_EMB_PROBES[0][1], _EMB_PROBES[1][1]])
    except EmbedQuotaExceeded as e:
        return _done(c, "fail", f"平台通路被阻断：{str(e)[:220]}",
                     latency_ms=int((time.time() - t0) * 1000))
    except Exception as e:                       # noqa: BLE001
        return _done(c, "fail", f"平台通路异常：{type(e).__name__}: {str(e)[:200]}",
                     latency_ms=int((time.time() - t0) * 1000))
    finally:
        (Embedder._PROBE_CACHE, Embedder._PROBE_STAMP,
         Embedder._QUOTA_BLOCK_UNTIL, Embedder._UNAVAIL_BLOCK_UNTIL,
         Embedder.LAST_QUOTA_ERROR) = snap
    lat = int((time.time() - t0) * 1000)
    if ver != "openai-compat":
        return _done(c, "fail",
                     f"平台实际返回 {ver}（词面 bigram 降级向量）——检索会退化成字面匹配", latency_ms=lat)
    got = len(vecs[0]) if vecs else 0
    if got != dim:
        return _done(c, "fail",
                     f"平台通路维度 {got} ≠ 直连测试 {dim}（模型名/路由不一致，检查尾随空格与默认模型）",
                     latency_ms=lat)
    if all(abs(float(x)) < 1e-9 for x in vecs[0]):
        return _done(c, "fail", "平台通路返回全零向量", latency_ms=lat)
    return _done(c, "pass", f"经 Embedder 取到 {dim} 维真向量（{ver}）", latency_ms=lat)


def _embedding_checks(client, cfg: dict, model: str, deep: bool) -> list:
    c, vecs = _check_embed_api(client, cfg, model)
    checks = [c]
    if not deep:
        return checks
    dim = len(vecs[0]) if vecs else 0
    eff = _effective_embed_provider()
    if eff and cfg.get("id"):
        is_eff = str(cfg.get("id")) == str(eff.get("id"))
    elif eff:                                     # 表单测试（未保存）：按 URL + 模型名对齐
        is_eff = (str(cfg.get("base_url") or "").rstrip("/")
                  == str(eff.get("base_url") or "").rstrip("/")
                  and str(cfg.get("model_name") or "").strip()
                  == str(eff.get("model_name") or "").strip())
    else:
        is_eff = False
    checks.append(_check_embed_semantics(vecs))
    checks.append(_check_embed_index(dim, is_eff))
    checks.append(_check_embed_platform(cfg, dim, is_eff))
    return checks
