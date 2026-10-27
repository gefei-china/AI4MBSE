# -*- coding: utf-8 -*-
"""MCP 工具元数据（描述 + 参数契约）的存储与读取（P0-6 · 2026-10-26）。

## 问题（实测，生产库快照）

MCP 工具注入 `tools` payload 时，**每个工具的形态是**：

```json
{"type":"function","function":{
  "name":"query_projects",
  "description":"MCP 工具（zhiyuan-dev-mcp），端点 http://xuanyi-dev.huilingyun.com/mcp",
  "parameters":{"type":"object","properties":{},"additionalProperties":true}}}
```

⇒ **71 个工具的 description 去重后只有 1 种、parameters 只有 1 种**（实测）。
由此产生三个后果：

| 后果 | 说明 |
|---|---|
| **① JIT 语义预筛零区分度** | 所有 MCP 工具的文本完全相同 ⇒ 打分全是噪声 ⇒ 2026-10-16 不得不把 MCP 工具列入 `core_keep` 禁裁剪（"绑 71 个只露 6 个"的反面） |
| **② 模型不知道工具能做什么** | 只有名字 + 一句"这是 MCP 工具" ⇒ 模型只能靠名字猜用途、猜参数 ⇒ 调用失败或传错参 |
| **③ 每次固定烧 5,467 token** | 单个 224 字符 / 77 token × 71 ⇒ 占整包 payload 的 **71~80%**（实测） |

## 根因：发现流程**主动丢弃**了 description 与 inputSchema

`mcp_health.discover_server()` 第 98 行：

```python
json.dumps([t.get("name") for t in tools], ensure_ascii=False)
```

`mcp_client.list_tools()` 明明返回 `[{name, description, inputSchema}]`（见 mcp_client.py:175-180），
**但只把 name 存进了 `mcp_servers.tools`** ⇒ schema 在发现那一刻就丢了。

## 本模块的解法（两段式，对齐 Deep Agents「按需 + 无损」）

### 第 1 段：把丢掉的东西存回来（**修数据，不改行为**）

新增 `mcp_servers.tool_meta` 列（JSON），存 `[{name, description, inputSchema}]`。
`discover_server` 落库时**同时**写 `tools`（保持原字段不变，兼容既有消费点）
与 `tool_meta`（新字段）。存量 server 可通过"重新发现"补齐。

### 第 2 段：按需暴露（**减少注入量 + 提升可用性**）

有 `tool_meta` 后：
1. **description 真实** ⇒ JIT 语义预筛**恢复区分度** ⇒ 不必再"全量禁裁剪"
2. **inputSchema 真实** ⇒ 模型拿到参数契约（顺带修掉「以为任意参数」的老问题）
3. **按 server 保底 N 个 + 语义选族** ⇒ 71 个压到 N 个，**且保底工具必在**

> ⚠️ **纪律**：第 2 段的行为变更**必须**保留"显式契约"语义 ——
> 用户在「绑定 MCP 服务器」面板里绑的 server，其**关键工具**不能被裁掉。
> 本模块的保底策略是：**每个 server 至少露 `min_keep` 个**（按语义排序取前 N），
> 而不是"全露"或"全裁"这个二元选择。

## 兼容与降级

- `tool_meta` 为空（存量 / 未重新发现）⇒ **完全退回改动前行为**（全量注入 + 模板 description），
  逐字节一致 ⇒ 零风险。
- 开关 `context_audit.mcp_meta_enabled`（默认 True）/ `mcp_min_keep`（默认 8，0 = 退回全量）。
"""

from __future__ import annotations

import json


def load_tool_meta(server_row) -> dict:
    """从 `mcp_servers` 行读出 `{tool_name: {description, inputSchema}}`。

    容错：字段缺失 / 非法 JSON / 结构不符 ⇒ 返回 `{}`（调用方据此退回旧行为）。
    """
    if not server_row:
        return {}
    try:
        raw = server_row["tool_meta"] if "tool_meta" in server_row.keys() else ""
    except Exception:
        raw = ""
    if not raw:
        return {}
    try:
        arr = json.loads(raw)
    except Exception:
        return {}
    if not isinstance(arr, list):
        return {}
    out = {}
    for it in arr:
        if isinstance(it, dict):
            nm = str(it.get("name") or "").strip()
            if nm:
                out[nm] = {
                    "description": str(it.get("description") or ""),
                    "inputSchema": it.get("inputSchema") or it.get("input_schema") or {},
                }
        elif isinstance(it, str) and it.strip():
            # 兼容存量形态：纯名字数组
            out[it.strip()] = {"description": "", "inputSchema": {}}
    return out


def save_tool_meta(tools: list) -> str:
    """把 `list_tools()` 的原始结果序列化为 JSON（供 `mcp_servers.tool_meta` 落库）。

    只保留 `name` / `description` / `inputSchema` 三个键 —— 原始返回可能含更多字段
    （如 annotations），一律不落库（无用且占空间）。
    """
    out = []
    for t in (tools or []):
        if not isinstance(t, dict):
            continue
        nm = str(t.get("name") or "").strip()
        if not nm:
            continue
        sch = t.get("inputSchema") or t.get("input_schema") or {}
        if not isinstance(sch, dict):
            sch = {}
        out.append({
            "name": nm,
            "description": str(t.get("description") or "")[:600],
            "inputSchema": sch,
        })
    return json.dumps(out, ensure_ascii=False)


def pick_for_injection(meta: dict, user_input: str, min_keep: int = 8, top_n: int = 0):
    """从 server 的工具集里选出本轮要暴露的工具。

    返回 `(names, stats)`：
      · `names` —— 选中的工具名列表（**保持 `meta` 的原始顺序** ⇒ 顺序稳定、可复现）
      · `stats` —— 诊断指标（总数 / 选中数 / 语义命中数）

    策略（**排序取前 N**，而不是「命中即全收」）：
      1. 对每个工具算语义分（query token 与 name+description token 的交集，
         按 token 长度加权 —— 长词更具体，避免「查询」二字把全部工具都拉进来）
      2. 按 `(分数降序, 原始顺序)` 排序，取前 `N`：`N = top_n > 0 ? top_n : min_keep`
      3. 输出再按原始顺序重排（**顺序只由 `meta` 决定** ⇒ 同一 server 多次调用可复现）

    ⚠️ 为什么不用「保底集 + 命中者全收」：实测 71 个 MCP 工具里 **69 个** 都含
    「查询」二字，「命中即入选」等于没筛（实测选中 69/71，token 几乎没降）。
    排序取前 N 才真正把「最相关的 N 个」挤出来，且 N 恒定 ⇒ 预算可预测。
    """
    empty = {"total": 0, "selected": 0, "keep": 0, "semantic": 0, "semantic_names": []}
    if not meta:
        return [], dict(empty)
    names = list(meta.keys())
    total = len(names)
    if min_keep <= 0 and top_n <= 0:
        return names, {"total": total, "selected": total, "keep": 0, "semantic": 0,
                       "semantic_names": []}

    want = top_n if top_n and top_n > 0 else min_keep
    want = max(1, min(want, total))
    order = {nm: i for i, nm in enumerate(names)}

    # ── 打分 ──
    scores = {}
    if user_input:
        q_tokens = _tokens(str(user_input).lower())
        if q_tokens:
            for nm, info in meta.items():
                t_tokens = _tokens(("%s %s" % (nm, info.get("description") or "")).lower())
                if not t_tokens:
                    continue
                # 交集按 token 长度加权：命中「工程列表」远强于命中「查询」
                w = sum(len(t) for t in (q_tokens & t_tokens))
                if w:
                    scores[nm] = w
    semantic_hits = sorted(scores, key=lambda nm: (-scores[nm], order[nm]))

    # ── 排序取前 N（无分者按原始顺序垫底 ⇒ 语义全落空时等于"前 N 个保底"）──
    ranked = sorted(names, key=lambda nm: (-scores.get(nm, 0), order[nm]))
    chosen_set = set(ranked[:want])

    # ── 输出按原始顺序（可复现）──
    chosen = [nm for nm in names if nm in chosen_set]
    return chosen, {
        "total": total,
        "selected": len(chosen),
        "keep": want,
        "semantic": len([nm for nm in chosen if nm in scores]),
        "semantic_names": [nm for nm in semantic_hits if nm in chosen_set],
    }


#: 中英文常见词停用表（粗匹配用，避免"的了是"这类噪声命中）
_STOP = set("的了是在和与及或把被让对从向为以于就都也很而但并且如果因为所以这些那些我们您请帮我要想能会如何怎么什么是否可以一个这个那个".split())


def _tokens(text: str) -> set:
    """粗分词：英文按词、中文按 2-3 字滑窗（与本仓 `_flow_tokens` 同思路，避免引入新依赖）。"""
    import re
    out = set()
    for w in re.findall(r"[a-zA-Z][a-zA-Z0-9_]{1,}", text or ""):
        out.add(w)
        for part in w.split("_"):
            if len(part) >= 2:
                out.add(part)
    cjk = re.sub(r"[^一-鿿]", "", text or "")
    for i in range(len(cjk)):
        for L in (2, 3):
            if i + L <= len(cjk):
                t = cjk[i:i + L]
                if t not in _STOP:
                    out.add(t)
    return out
