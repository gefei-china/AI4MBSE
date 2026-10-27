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
      3. **补闭包**（见 `expand_with_deps`）：被选中工具在其 description 里显式引用的
         同伴工具、以及按请求意图动词族匹配到的写工具，一并纳入（不占 N 的名额）
      4. 输出再按原始顺序重排（**顺序只由 `meta` 决定** ⇒ 同一 server 多次调用可复现）

    ⚠️ 为什么不用「保底集 + 命中者全收」：实测 71 个 MCP 工具里 **69 个** 都含
    「查询」二字，「命中即入选」等于没筛（实测选中 69/71，token 几乎没降）。
    排序取前 N 才真正把「最相关的 N 个」挤出来，且 N 恒定 ⇒ 预算可预测。

    ★ 2026-10-27（会话 9629「在通信子系统用例包中新增测试包」失败）：
      **只"排序取前 N"是不够的**。实测该请求暴露的 8 个里
      `create_model_elements` / `get_modeling_capabilities` / `query_data_tree` /
      `search_model_elements` **一个都没有**，模型手里只有
      `move_model_elements` / `layout_model_view` / `get_model_annotations`——
      跟"建包"毫无关系的工具 ⇒ 它不是不会做，是**没工具可做**。
      根因：粗分词打分对"新增一个包"这种短中文请求天然失效（工具名是英文），
      且工具之间存在**前置依赖**（`create_model_elements` 描述里明写
      "先 get_modeling_capabilities 读取父元素，用返回的 creationRef 创建"），
      逐工具独立裁剪必然把依赖链剪断。
      ⇒ 加闭包：引用必带、意图动词命中必带。
    """
    empty = {"total": 0, "selected": 0, "keep": 0, "semantic": 0, "semantic_names": [],
             "expanded": []}
    if not meta:
        return [], dict(empty)
    names = list(meta.keys())
    total = len(names)
    if min_keep <= 0 and top_n <= 0:
        return names, {"total": total, "selected": total, "keep": 0, "semantic": 0,
                       "semantic_names": [], "expanded": []}

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

    # ── ★ 闭包：依赖 + 意图动词族（不占 N 名额）──
    expanded = []
    try:
        expanded = expand_with_deps(meta, chosen_set, str(user_input or ""))
        chosen_set |= set(expanded)
    except Exception:
        expanded = []

    # ── 输出按原始顺序（可复现）──
    chosen = [nm for nm in names if nm in chosen_set]
    return chosen, {
        "total": total,
        "selected": len(chosen),
        "keep": want,
        "semantic": len([nm for nm in chosen if nm in scores]),
        "semantic_names": [nm for nm in semantic_hits if nm in chosen_set],
        "expanded": [nm for nm in names if nm in set(expanded)],
    }


#: 意图动词族：请求里出现任意一个中文/英文词 ⇒ 把工具名含对应英文动词的工具纳入。
#: 为什么需要：粗分词打分对**短中文请求**（"新增一个测试包"）几乎无区分度，
#: 而工具名是英文（create_model_elements）⇒ 中文与英文名不相交 ⇒ 打分全 0
#: ⇒ 只能按"原始顺序前 N 个"保底，与任务毫无关系（实测就是这种情形）。
#: 这里做的是**动词族映射**，不是把某个具体工具写死：任何名字含这些动词的工具都受益。
_INTENT_VERBS = {
    "create": ("新增", "新建", "创建", "创建", "添加", "加一个", "建一个", "create", "add", "new", "insert"),
    "update": ("修改", "更新", "改名", "重命名", "调整", "update", "modify", "rename", "set", "patch"),
    "delete": ("删除", "移除", "删掉", "清理", "delete", "remove", "drop"),
    "move": ("移动", "挪", "move"),
    "import": ("导入", "写回", "回写", "import", "upload", "push"),
    "export": ("导出", "下载", "拉取", "export", "download", "pull"),
}


#: 结构定位词：请求涉及「包/父子/层级/树」⇒ 需要导航类工具去定位父容器。
#: 为什么单独一类：写工具（create_model_elements）只被要求在**已知父容器标识**上创建，
#: 它自己不做定位；定位靠 query_data_tree / search_model_elements 这类工具。
#: 实测 `create_model_elements` 的描述**不提** `query_data_tree` ⇒ 光靠依赖闭包补不到它，
#: 于是"有建包工具却找不到父包"（正是 conv 9629 的失败形态）。
_STRUCT_WORDS = ("包", "父子", "父节点", "子节点", "子级", "层级", "树", "目录", "根节点",
                 "结构", "tree", "hierarchy", "parent", "child", "navigat", "browse")

#: 导航类工具的识别词（名字或描述里出现，即认为是"定位/浏览"能力）
_NAV_HINTS = ("tree", "hierarchy", "navigat", "browse", "数据树", "层级", "当前层", "展开")


def expand_with_deps(meta: dict, chosen, user_input: str = "", max_rounds: int = 1,
                     cap_extra: int = 0) -> list:
    """把「被选中工具所依赖的同伴」补进来（**依赖闭包**），返回新增的工具名列表。

    三类补充（都是**通用规则**，不针对任何具体工具/平台）：
      ① **意图动词族**：请求里出现"新增/创建/修改/删除/导出…"时，把工具名含对应
         英文动词的工具一并纳入 —— 短中文请求靠分词打分是选不中英文命名的写工具的。
      ② **结构定位**：请求涉及"包/层级/树/父节点"时，纳入导航类工具 ——
         写工具只负责"在已知父容器上创建"，**定位父容器的那一环不在它的描述里**。
      ③ **显式引用（1 跳，只从①②新增的工具往外走）**：工具的 description 里出现了
         同 server 其它工具的名字 ⇒ 那是它的前置/后继步骤（实测 41/71 个都这样写，
         例如 `create_model_elements`：『先 get_modeling_capabilities 读取父元素，
         使用返回的 creationRef 创建普通包』）。只暴露后者会把链剪断。

    ⚠️ 为什么闭包**只从①②新增的工具**往外走、而不是从全部已选工具：
      实测按"全部已选"做 1 跳会从 8 个涨到 **32 个**（schema 59KB ≈ 20k tokens/轮），
      13 轮就烧穿 300k token 预算 ⇒ 模型在第 12 轮被"预算耗尽"收口，
      离建包只差最后一步（conv 9637 实测，累计 311,426 tokens）。
      **能力给多了 ≠ 能完成任务**：必须把注入体量压到"够用"。
      只从意图动词/结构定位新增的工具往外走，新增通常只有 3~5 个。

    ⚠️ 遍历一律按 `meta` 原始顺序（不用 set 遍历——无序会让输出不可复现）。
    """
    names = list(meta.keys())
    picked = set(p for p in chosen if p in meta)
    q = str(user_input or "").lower()

    # ① 意图动词族（先把写工具拉进来）
    verb_add = []
    if q:
        for grp, words in _INTENT_VERBS.items():
            if any(w.lower() in q for w in words):
                for nm in names:
                    if nm not in picked and grp in nm.lower():
                        picked.add(nm)
                        verb_add.append(nm)

    # ② 结构定位（把导航工具拉进来）
    nav_add = []
    if q and any(w.lower() in q for w in _STRUCT_WORDS):
        for nm in names:
            if nm in picked:
                continue
            blob = ("%s %s" % (nm, (meta.get(nm) or {}).get("description") or "")).lower()
            if any(h.lower() in blob for h in _NAV_HINTS):
                picked.add(nm)
                nav_add.append(nm)

    # ③ 引用闭包：**只从①②新增的工具往外走**（控体量）
    seeds = [nm for nm in names if nm in set(verb_add) | set(nav_add)]
    ref_add = []
    for _ in range(max(1, max_rounds)):
        grew = False
        for nm in seeds:
            d = (meta.get(nm) or {}).get("description") or ""
            if not d:
                continue
            for other in names:
                if other in picked or len(other) < 6:
                    continue
                if other in d:
                    picked.add(other)
                    ref_add.append(other)
                    grew = True
        if not grew:
            break
        # 下一轮只从"本轮新加的"继续（保持窄扩散）
        seeds = [nm for nm in names if nm in set(ref_add)]

    # 合并（按优先级），并按 names 原始顺序归一输出
    prio = []
    for grp in (verb_add, nav_add, ref_add):
        for nm in grp:
            if nm not in prio:
                prio.append(nm)
    if cap_extra and cap_extra > 0 and len(prio) > cap_extra:
        keep = set(prio[:cap_extra])
    else:
        keep = set(prio)
    return [nm for nm in names if nm in keep]





#: 中英文常见词停用表（粗匹配用，避免"的了是"这类噪声命中）
_STOP = set("的了是在和与及或把被让对从向为以于就都也很而但并且如果因为所以这些那些我们您请帮我要想能会如何怎么什么是否可以一个这个那个".split())


def _schema_sane(sch) -> bool:
    """瘦身后的结构自检：`properties` 的每个值必须是对象（属性定义），不能是字符串。

    ★ 为什么必须有这道自检（本轮实测踩到）：先前把超深的 `properties` 容器替换成
      `{"type": "object"}`，结果产出 `"properties": {"type": "object"}`
      —— 等于声明了一个名叫 `type`、定义为字符串 `"object"` 的属性 ⇒ **非法 JSON Schema**。
      后果不是"少个字段"，而是整轮 LLM 调用 400 直接失败、对话当场终止：
         `Invalid schema for function 'apply_model_changes': "object" is not of types "boolean","object"`
      ⇒ 瘦身失败时必须**回退原 schema**，宁可多花 token 也不能让工具集整体不可用。
    """
    def _walk(node):
        if isinstance(node, dict):
            props = node.get("properties")
            if isinstance(props, dict):
                for v in props.values():
                    if not isinstance(v, dict):
                        return False
                    if not _walk(v):
                        return False
            ap = node.get("additionalProperties")
            if ap is not None and not isinstance(ap, (bool, dict)):
                return False
            for k, v in node.items():
                if k in ("properties", "additionalProperties"):
                    continue
                if isinstance(v, (dict, list)) and not _walk(v):
                    return False
            return True
        if isinstance(node, list):
            return all(_walk(x) for x in node)
        return True

    try:
        return _walk(sch)
    except Exception:
        return False


def slim_schema(sch: dict, depth: int = 2, desc_cap: int = 80) -> dict:
    """把 MCP 工具的参数契约**瘦身**后再给模型（保留结构，压缩描述）。

    为什么需要（conv 9637 实测）：21 个 MCP 工具的完整 inputSchema 共 47KB（≈16k tokens/轮），
    13 轮就直接烧穿 300k token 预算 ⇒ 模型在第 12 轮被判"预算耗尽"收口，
    离建包只差最后一步。**给多了工具反而做不成事**。

    瘦身规则（只压"解释性文本"，不动"结构性信息"）：
      · 参数级 `description` 截断到 `desc_cap`（模型靠名/类型/枚举就能填对，
        真不会填时还有工具返回的 `fields[].expected` 兜底）；
      · 递归深度超过 `depth` 的子树**清空**（`properties`→`{}`、`items`→`{}`、
        `additionalProperties`→`true`），绝不塞非法占位；
      · 保留 `type` / `required` / `enum` / `properties` 键名。

    ⚠️ 不瘦身**顶层工具 description** —— 那是模型"选哪个工具"的唯一依据（见 pick_for_injection）。
    ⚠️ 瘦身后若结构自检不过 ⇒ **原样返回原 schema**（宁多花 token，不可让工具集整体 400）。
    """
    if not isinstance(sch, dict):
        return sch
    src = json.loads(json.dumps(sch))   # 深拷贝，避免原地改污染 tool_meta 缓存

    def _walk(node, d):
        if not isinstance(node, dict):
            return node
        out = {}
        for k, v in node.items():
            if k == "description" and isinstance(v, str):
                out[k] = v[:desc_cap]
                continue
            if k == "properties" and isinstance(v, dict):
                out[k] = {} if d >= depth else {pk: _walk(pv, d + 1) for pk, pv in v.items()}
                continue
            if k == "items" and isinstance(v, dict):
                out[k] = {} if d >= depth else _walk(v, d + 1)
                continue
            if k == "additionalProperties":
                if isinstance(v, dict):
                    out[k] = True if d >= depth else _walk(v, d + 1)
                else:
                    out[k] = v          # 布尔原样（false/true 都是合法的）
                continue
            out[k] = _walk(v, d) if isinstance(v, dict) else v
        return out

    slimmed = _walk(src, 0)
    if not _schema_sane(slimmed):
        return sch
    return slimmed




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
