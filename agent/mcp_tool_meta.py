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


def _nested_address_fields(info: dict) -> set:
    """只取**嵌套寻址对象内部**的 id 字段（不含顶层 required 标量）。

    与 `_address_set` 的差别正是"服务端下发的坐标"与"客户端自造键"的差别：
    坐标总是打包在寻址对象里（`workspace` / `version`）成组出现，
    而 `requestId` 这类幂等键是顶层标量。
    """
    sch = (info or {}).get("inputSchema") or {}
    if not isinstance(sch, dict):
        return set()
    props = sch.get("properties") or {}
    out = set()
    if isinstance(props, dict):
        for _k, v in props.items():
            if not isinstance(v, dict):
                continue
            sub = v.get("properties")
            if not isinstance(sub, dict):
                continue
            for pk in sub:
                if str(pk).lower().endswith("id"):
                    out.add(str(pk))
    return out


def _lookup_prefix_ok(name: str) -> bool:
    """该工具是否是「查询类」（能产出标识符）——只有查询类才可能是坐标的生产者。

    通用规则：**按名字前缀**判定读/写语义，不针对任何具体工具名。
    """
    n = str(name or "").lower()
    return n.startswith(("query_", "list_", "search_", "get_", "find_", "browse_"))


def _address_set(info: dict) -> set:
    """一个工具「需要哪些坐标标识符」。

    取两部分的并集：
      ① 顶层 `required` 里形如 `<x>Id` 的标量字段；
      ② 嵌套对象属性（`properties` 里的子对象）里形如 `<x>Id` 的字段
         —— 平台把「工程/分支/版本」打包成一个寻址对象（`version` / `workspace`），
         这个对象**整体必填**，其内部字段就是该工具要的坐标。

    ⚠️ 只看顶层 `required` 会漏掉一半工具：`export_sysml_v2` 顶层只 required `action`，
    坐标在它的 `version` 属性里（PREVIEW 时可省，EXPORT 时必填）。
    """
    sch = (info or {}).get("inputSchema") or {}
    if not isinstance(sch, dict):
        return set()
    out = set()
    for r in (sch.get("required") or []):
        rs = str(r)
        if rs.lower().endswith("id"):
            out.add(rs)
    props = sch.get("properties") or {}
    if isinstance(props, dict):
        for _k, v in props.items():
            if not isinstance(v, dict):
                continue
            sub = v.get("properties")
            if not isinstance(sub, dict):
                continue
            for pk in sub:
                if str(pk).lower().endswith("id"):
                    out.add(str(pk))
    return out


def _stem(field: str) -> str:
    """`projectId` → `project`；`branchId` → `branch`（大小写不敏感的词干）。"""
    f = str(field or "").strip()
    return (f[:-2] if f.lower().endswith("id") and len(f) > 2 else f).lower()


def address_chain(meta: dict, chosen, max_rounds: int = 2) -> list:
    """**寻址前置链闭包**：把「能产出坐标的查询工具」补进来（通用规则，不含任何硬编码工具名）。

    ## 为什么需要（2026-10-27 实测）

    平台有一组**坐标三元组**：`projectId` / `branchId` / `versionId`。
    实测 71 个 MCP 工具里 **65 个**都要这组坐标（以寻址对象 `workspace` / `version` 传入）。
    而产出它们的是三个「查询类」工具：查工程 → 查分支 → 查版本（**逐级依赖**：
    查分支要知道工程，查版本要知道分支）。

    **裁剪会把这条链剪断**：对中文写类请求（如「在通信子系统用例包中新增测试包」），
    工具名是英文、分词打分对中文几乎无区分度 ⇒ 实测这三个坐标工具**一个都没被选中**，
    而 skill 正文第一句恰恰是「核心坐标系（一切请求都从这三项开始）」——
    **提示词让模型先调的工具不在清单里** ⇒ 模型只能瞎猜坐标（必然 INVALID_ARGUMENTS /
    PERMISSION_DENIED）或反复盲试。

    ## 规则（结构推导，与平台无关）

    1. **坐标轴**：被 ≥ `axis_min` 个工具需要的 `<x>Id` 字段（共享寻址轴，不是 `requestId`
       这类客户端自造幂等键——后者没有查询类生产者，会自然落选）。
    2. **生产者**：对轴上的字段 F，工具 P 是其生产者，当且仅当
       ① `F` 不在 P 自己的坐标集里；② P 的名字/描述含 F 的**词干**（`versionId`→`version`）；
       ③ P 是**查询类**（名字前缀 `query_/list_/search_/get_/find_/browse_`）；
       ④ 在满足以上条件的 P 中取**坐标集最小**者（最上游的那一级）。
    3. **闭包**：对每个被选中的工具，把它要的坐标轴字段的生产者补进来，
       并对生产者递归（查版本要工程与分支 ⇒ 再补查工程、查分支）。

    实测（zhiyuan-dev-mcp，71 工具）：坐标轴 = `projectId`(68) / `branchId`(67) / `versionId`(66)；
    生产者唯一确定 = 查工程(坐标集 ∅) / 查分支({projectId}) / 查版本({projectId,branchId})；
    ⇒ 任何需要寻址的请求都会**恒定补齐这 3 个**（体量 +3 个工具，约 +1k token）。

    ⚠️ 不设「工具名白名单」：换一个平台（同样有工程/分支/版本坐标系）同样成立；
    没有坐标结构的 server（如内置 3 工具 MCP）**一条都不会补** ⇒ 逐字节不变。
    """
    if not meta or not chosen:
        return []
    names = list(meta.keys())
    order = {nm: i for i, nm in enumerate(names)}
    chosen_set = set(p for p in chosen if p in meta)
    if not chosen_set:
        return []

    # ── ① 坐标轴：被 ≥ axis_min 个工具需要、且**成组出现在寻址对象里**的 id 字段 ──
    #    为什么要「成组出现在寻址对象里」这条：它把**客户端自造键**排除掉。
    #    实测 `requestId`（幂等键）被 42 个工具需要（也达到轴阈值），
    #    但它**从不**出现在寻址对象里（寻址对象恒为 {projectId,branchId,versionId}）
    #    ⇒ 不是服务端下发的坐标 ⇒ 不算轴 ⇒ 不会给"生产 requestId"这种伪需求补工具。
    axis_min = max(5, len(names) // 3)
    usage = {}
    nested = set()
    for nm in names:
        info = meta.get(nm) or {}
        for f in _address_set(info):
            usage[f] = usage.get(f, 0) + 1
        for f in _nested_address_fields(info):
            nested.add(f)
    axes = {f for f, c in usage.items() if c >= axis_min and f in nested}
    if not axes:
        return []

    # ── ② 生产者：坐标集是「坐标轴全集」的**真子集**的查询类工具 ──
    #    为什么要「真子集」这条：它把**客户端自造键**（如 `requestId` 幂等键）排除掉。
    #    实测 `requestId` 也被 42 个工具需要（达到轴阈值），但它的"生产者"候选
    #    （如 get_model_operation）的坐标集 **等于** 轴全集 ⇒ 不满足真子集 ⇒ 落选。
    #    而真正的三级解析器坐标集分别是 ∅ / {projectId} / {projectId,branchId} ⇒ 真子集成立。
    producers = {}
    for f in axes:
        st = _stem(f)
        if not st:
            continue
        cands = []
        for nm in names:
            info = meta.get(nm) or {}
            p_aset = _address_set(info)
            if not p_aset < axes:          # 真子集：排除轴全集本身与含非轴字段者
                continue
            if f in p_aset:
                continue
            if not _lookup_prefix_ok(nm):
                continue
            blob = ("%s %s" % (nm, info.get("description") or "")).lower()
            if st not in blob:
                continue
            cands.append(nm)
        if not cands:
            continue
        producers[f] = min(cands, key=lambda nm: (len(_address_set(meta.get(nm) or {})), order[nm]))

    if not producers:
        return []

    # ── ③ 闭包：逐级向上游补齐 ──
    added = []
    frontier = set(chosen_set)
    for _ in range(max(1, max_rounds)):
        grew = False
        need = set()
        for nm in frontier:
            for f in _address_set(meta.get(nm) or {}):
                if f in axes:
                    need.add(f)
        for f in need:
            p = producers.get(f)
            if p and p not in chosen_set:
                chosen_set.add(p)
                added.append(p)
                grew = True
        if not grew:
            break
        frontier = set(added)
    return [nm for nm in names if nm in set(added)]


def name_hits_group(name: str, group: str) -> bool:
    """工具名是否属于某个动词族 —— **按词元边界匹配**，不是子串匹配。

    ★ 2026-10-28（实测误判）：旧写法是 `grp in nm.lower()`，于是
      「pre**view**_model_changes」（预览命令批）被当成 **view 族**（出图）成员
      ⇒ 「设计时序图」这一轮把它当成"用户要出图"的证据拉进候选，
        白占 8,369 字符（占整轮工具载荷 20%），而它属**与主用协议互斥**的
        命令批协议 —— 模型看到两套写入方式只会更犹豫。
      同族误判还有：pre**sent**_ation vs sent? 等潜在情形。
    ⇒ 改为"按 `_` 切分后的词元等价匹配"：`create_model_view` → {create, model, view}
      命中 view ✅；`preview_model_changes` → {preview, model, changes} 不含 view ✅。
    兼容：允许词元以该组名开头（`imports`/`validators` 这类复数/派生写法）。
    """
    if not name or not group:
        return False
    g = group.lower()
    for tok in str(name).lower().split("_"):
        if tok == g or (tok.startswith(g) and len(tok) <= len(g) + 3):
            return True
    return False


#: 单个 MCP 工具的参数契约超过此字符数即视为「重协议工具」：
#: **只在它属本轮命中动词族/语义 top-N/寻址链时才注入**，否则由闭包补入的一律回退。
#: 标定依据（2026-10-28 真机，71 工具目录 + 「设计时序图并写入」这一轮）：
#:   36 个工具载荷 41,265 字符（≈13.7k tokens/轮），其中
#:   `preview_model_changes` 单占 8,369 字符（20%）且与主用协议互斥。
#: 4000 的取法：把 8k 级"命令批/批量变更"类协议挡在外面，而正路写入工具
#: （`import_sysml_text` ≈1.1k、`create_model_view` ≈1.1k、`validate_sysml_text` ≈0.3k）
#: 全部远低于此线 ⇒ 不会误杀。
MCP_HEAVY_SCHEMA_CHARS = 4000


#: ★★ 2026-10-28（conv 9740 实证）**动词同义词的单一事实源**。
#:
#: 为什么必须收敛成一份：本模块原本有**两份**同语义词表 ——
#:   · `_INTENT_VERBS`（闭包用，**加**工具：命中就把该族工具拉进候选）
#:   · `_MCP_WRITE_VERB_TABLE`（闸门用，**减**工具：没命中就把该族工具剔出候选）
#: 两份各写一份 ⇒ 同一句话在两处结论相反，而中间没有任何东西会报错。
#: 真机后果：用户说「…**写入**对应的包…」
#:   · 闭包侧当时也只收「导入/写回/回写/import/upload/push」⇒ 一个都不命中；
#:   · 闸门侧同样不含「写入」⇒ 即便闭包把它拉进来，闸门也会再剔掉。
#:   ⇒ `import_sysml_text`（MCP 里唯一真正能落库的工具）**在两侧同时隐形**，
#:     模型手里只剩 `create_model_elements`（逐元素建），于是连猜 3 次类型名
#:     ⇒ INVALID_ARGUMENTS×3 ⇒ 当失败轮打满 ⇒ 整轮判死。
#: 这类"同一语义多处各写一份必然错位"的教训本仓已记过多次，这次直接把
#: 词表**收成一份**，两份表都从它派生 ⇒ 结构上不可能再彼此矛盾。
#:
#: 维护约定：新增一种说法只改这里；`_INTENT_VERBS` / `_MCP_WRITE_VERB_TABLE`
#: 只声明"取哪些族"，**不得再内联词条**（门禁会检查两者皆为本表的引用）。
_VERB_SYNONYMS = {
    "create": ("新增", "新建", "创建", "添加", "加一个", "建一个", "增加", "补充",
               "生成", "产出", "create", "add", "new", "insert", "make"),
    "update": ("修改", "更新", "改名", "重命名", "调整", "改成", "改为", "换成",
               "改一下", "优化", "update", "modify", "rename", "set", "patch", "edit"),
    "delete": ("删除", "移除", "删掉", "删了", "清理", "清除", "删除掉",
               "delete", "remove", "drop", "purge"),
    "move":   ("移动", "挪", "移到", "move", "relocate"),
    # 「落库」这一族：中英文说法一次收全（漏一个词就等于该族工具永不出现）
    "import": ("导入", "写入", "写入到", "写进", "写到", "落库", "入库", "存到", "存入",
               "落地", "回写", "写回", "灌入", "持久化", "覆盖导入", "导入源码", "导入模型",
               "import", "upload", "push", "save", "persist"),
    "overwrite": ("覆盖", "覆写", "替换", "overwrite", "replace"),
    "export": ("导出", "下载", "拉取", "export", "download", "pull"),
    "apply":  ("应用", "下发", "批量变更", "应用变更", "变更集",
               "apply", "apply_changes", "change set"),
    "undo":   ("撤销", "回退", "还原", "回滚", "undo", "revert", "rollback"),
    "clone":  ("克隆", "复制", "副本", "拷贝", "clone", "copy", "duplicate"),
    "annotate": ("标注", "注释", "批注", "说明文字", "annotate", "annotation"),
    "layout": ("布局", "排版", "排布", "摆放位置", "整理位置", "layout"),
    "present": ("呈现", "展示", "显示", "出示", "present"),
    "place":  ("放置", "摆放", "布置", "place", "position"),
    "configure": ("配置", "设定", "设定为", "configure"),
    "manage": ("管理", "变体", "变型", "导入管理", "manage"),
    # 「校验」族：写入前必经一步。缺席时模型会拿本地校验器当平台校验，结论不可迁移。
    "validate": ("校验", "验证", "检查", "语法检查", "查错", "纠错",
                 "validate", "verify", "lint", "check"),
    # 「出图」族：⚠️ 刻意不收单字「图」（会误伤"地图/意图"），且 view 族工具共 15 个。
    "view": ("视图", "时序图", "顺序图", "序列图", "状态机", "状态图", "活动图", "用例图",
             "需求图", "参数图", "块定义图", "内部块图", "部署图", "分配图", "控制流图",
             "功能分解图", "画图", "出图", "绘图", "创建视图", "新建视图"),
    # ── 以下 4 族只给**执行侧**（HITL 写判定）用，**不进** `_INTENT_VERBS` ──
    #   `mcp_write_group(name)` 的语义是「这个名字是不是写工具」，
    #   把 validate/view 塞进来会让 `get_model_view` / `validate_sysml_text`
    #   被当成写操作 ⇒ 读操作也弹人工确认（stream.py:134 正是这种用法）。
    "set":      ("设置", "设定", "赋值", "置为", "清空", "替换", "set_", "assign", "unset"),
    "remove":   ("移除", "去除", "摘除", "remove", "detach", "unlink"),
    "generate": ("生成", "产出", "generate", "produce", "render"),
    "run":      ("运行", "执行", "触发", "run", "execute", "trigger", "automation"),
}


#: MCP 工具的**动词族 → 触发词**（写类工具的"该不该出现在本轮"的判据）。
#:
#: 为什么要有：MCP 工具**不在 `tools` 表**里 ⇒ 后端那道"写意图闸"（按 `side_effect`
#: 过滤）**根本管不到它们** ⇒ 实测（2026-10-27）：
#:   · 「把当前模型导出为 SysML v2 源码」→ 工具清单里出现了 `delete_model_elements`
#:   · 「校验这段 SysML v2 代码的语法」  → 出现了 `apply_model_changes` / `import_sysml_text`
#:   · 「在通信子系统用例包中新增测试包」→ 出现了 `apply_model_changes`
#: 读类任务被塞进破坏性/覆盖写工具 ⇒ 模型"顺手调用"就是事故。
#:
#: 规则：**按工具名里的动词族判定**（不是按工具名硬编码）——
#:   用户输入里没出现该动词族的任何触发词 ⇒ 该工具本轮不注入。
#:   触发词表**尽量宽**（宁多不少）⇒ 召回不降；精度提升来自"动词族对不上就不给"。
#:   例：`delete_model_elements` 需要"删除/移除/删掉/清理"，"新增测试包"不含 ⇒ 正确挡掉。
#:
#: ★ 2026-10-28：词条**全部来自 `_VERB_SYNONYMS`**（不再内联第二份）。
#:
#: ⚠️ 键的**顺序必须固定**（下表的字面顺序 = 历史顺序）：
#:   `mcp_write_group` 按插入顺序取**首个**命中，调序会让同一工具换族
#:   （如 `remove_view_presentations` 历史上命中 "move"，调序后会变成 "remove"）
#:   ⇒ 注入侧与 HITL 判定同时漂移。所以这里不写成 `= _VERB_SYNONYMS`，
#:   而是**只挑写族、按历史顺序**取引用。
#: ⚠️ 也**不能**把 `validate` / `view` 纳入：本表兼任「这个名字是不是写工具」的判据
#:   （`stream.py` 的写前确认用它），纳入后 `get_model_view` / `validate_sysml_text`
#:   会被当成写操作 ⇒ 读操作也弹人工确认。
_MCP_WRITE_GROUP_ORDER = (
    "delete", "import", "apply", "undo", "overwrite",
    "create", "update", "move", "clone", "annotate", "layout",
    "present", "place", "configure", "manage",
    "set", "remove", "generate", "run",
)
_MCP_WRITE_VERB_TABLE = {g: _VERB_SYNONYMS[g] for g in _MCP_WRITE_GROUP_ORDER}


#: **受动词闸约束的族**（破坏性 / 覆盖写）。
#:
#: 为什么只管这几族：实测「查工程列表」这类读请求下，MCP 的语义选集会挑中一批**写类**工具
#: （英文名按字典序排前 ⇒ create/configure/clone 等），若把**所有**写族都按动词闸掉，
#: 会出现「读请求里一个 MCP 工具都不剩」⇒ 反而把能力掐死。
#: ⇒ 只闸**破坏性/覆盖写**这几族：它们误暴露的代价最高（模型"顺手调用"就是事故），
#:   而 create/update 这类**常规建模动作**保留（漏了会让建模流程做不下去）。
_MCP_GATED_GROUPS = ("delete", "import", "apply", "undo", "overwrite")


#: ★ 2026-10-28（写前确认 HITL）执行侧**专用**的族表与判据。
#:
#: ## 为什么不能直接复用 `mcp_write_group`
#: `mcp_write_group` 是**注入侧**判据（问「这一轮该不该把这个工具递到模型眼前」），
#: 按子串匹配、且**故意不挡** create/update（挡了建模做不下去）。
#: 执行侧问的是「**这个操作要不要人点头**」⇒ 口径必须不同、且**更严**。
#: ⇒ 两者**刻意分开**，改一个不影响另一个。
#:
#: ## 实测（2026-10-28，真实 71 工具目录）暴露的两个真实问题
#: ① **漏判（更危险）**：6 个 `set_*` 工具 —— `set_model_expression` /
#:    `set_specializations` / `set_model_type_relations` / `set_state_diagram_labels` /
#:    `set_model_attribute_declaration` / `set_model_expression_visibility` ——
#:    平台描述明确写「设置或清空」「替换」「**保存真实 OCL AST**」⇒ **全是写操作**，
#:    但名字里没有 create/update 等动词 ⇒ 旧动词族判成 read ⇒ **闸门直接放行**。
#: ② **误判**：`get_model_delete_impact` 名字含 "delete" ⇒ 旧动词族判 destructive，
#:    而它其实是**只读**的删除影响分析（平台描述：「删除前校验…取得 DELETE_ELEMENTS 用途票据」）。
#:
#: ## 判据（三条，纯结构、零描述解析）
#: ① **读前缀优先** —— 命中 `get_/query_/list_/search_/…` 即判 read。
#:    这条同时修掉 ② 的误判（`get_model_delete_impact`）与
#:    `preview_model_changes`（描述里出现「删除」但它是纯只读预览 —— **描述关键词法不可靠**，
#:    真目录实测：其描述含「删除」「写入」「执行」三个词却是只读 ⇒ 故描述一律不参与判定）。
#: ② **长动词优先** —— "remove" 含 "move" 子串 ⇒ 必须 remove 先匹配。
#: ③ **补齐动词族** —— set / remove / generate / run（真目录实测得出）。
#:
#: ⚠️ 本表**只影响执行侧的写前确认闸**（写前确认 §4.2），
#:    注入侧的 `_MCP_GATED_GROUPS` 与 `filter_mcp_by_intent` **逐字节不变** ⇒ 零回归。

#: 读动词前缀（结构判据，命中即判 read）。
_MCP_READ_PREFIXES = (
    "get_", "query_", "list_", "search_", "find_", "export_", "preview_",
    "read_", "fetch_", "check_", "describe_", "count_",
)

#: 执行侧：**不可逆 / 覆盖写**的动词族（需最高档确认）。
MCP_DESTRUCTIVE_GROUPS = ("delete", "remove", "import", "apply", "undo", "overwrite")

#: 执行侧：**可逆写**的动词族（需普通确认）。
MCP_WRITE_GROUPS = (
    "create", "update", "move", "clone", "annotate", "layout", "present",
    "place", "configure", "manage", "set", "generate", "run",
)


def mcp_write_group(name: str) -> str:
    """判断一个 MCP 工具属于哪个写动词族；读类工具返回空串。

    **按工具名里的动词词匹配**，不硬编码任何具体工具名。
    读类动词（query/get/list/search/find/export/validate/preview…）不在表里 ⇒ 返回 ""。
    """
    n = str(name or "").lower()
    for grp in _MCP_WRITE_VERB_TABLE:
        if grp in n:
            return grp
    return ""


def mcp_effect_level(name: str) -> str:
    """★ 执行侧写类判定：返回 `"read"` / `"write"` / `"destructive"`。

    与注入侧 `mcp_write_group` 的区别（**刻意不同口径**）：
      · 注入侧 = 恰当性（该不该递到模型眼前）⇒ 窄口径、只闸 5 族；
      · 执行侧 = 授权（要不要人点头）⇒ **宽口径**、覆盖全部写族。

    判据（纯结构，不解析描述 —— 真实目录实测：描述关键词法误判率高）：
      ① 读前缀优先 ⇒ read；
      ② 否则按动词族匹配（**长词优先**："remove" 含 "move" 子串）；
      ③ 匹配不到 ⇒ read。

    真实目录实测结果（71 个工具）：read 30 / write 34 / destructive 7。
    """
    n = str(name or "").lower()
    if not n:
        return "read"
    if n.startswith(_MCP_READ_PREFIXES):
        return "read"
    # ⚠️ **破坏族必须整族先判**，不能按"最长动词优先"统一排 ——
    #   实测反例：`remove_view_presentations` 里 "present"(7) 是 "presentations" 的子串，
    #   若统一按长度排，present(7) 会压过 remove(6) ⇒ **删除被判成"可逆写"**。
    #   ⇒ 破坏族整族先匹配（族内再按长度降序），其次才是写族。
    for grp in sorted(MCP_DESTRUCTIVE_GROUPS, key=len, reverse=True):
        if grp in n:
            return "destructive"
    for grp in sorted(MCP_WRITE_GROUPS, key=len, reverse=True):
        if grp in n:
            return "write"
    return "read"


def filter_mcp_by_intent(names, user_input: str, groups: dict) -> tuple:
    """按**动词族**过滤 MCP 的**破坏性/覆盖写**工具。

    `groups` = {工具名: 动词族}（由 `mcp_write_group` 批量算出）。
    返回 `(保留列表, 被挡列表)`。

    ⚠️ 读类工具**永不被挡**；`create/update/move/clone/...` 等常规建模动作**也不挡**
    （见 `_MCP_GATED_GROUPS` 的理由）。
    ⚠️ 挡的是"**用户压根没提这个动作**"的工具，不是"用户禁止这个动作"。
    """
    if not groups:
        return list(names), []
    q = str(user_input or "").lower()
    if not q:
        return list(names), []
    kept, dropped = [], []
    for nm in names:
        grp = groups.get(nm)
        if not grp or grp not in _MCP_GATED_GROUPS:
            kept.append(nm)
            continue
        words = _MCP_WRITE_VERB_TABLE.get(grp) or ()
        if any(w.lower() in q for w in words):
            kept.append(nm)
        else:
            dropped.append(nm)
    return kept, dropped


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
             "expanded": [], "chain": []}
    if not meta:
        return [], dict(empty)
    names = list(meta.keys())
    total = len(names)
    if min_keep <= 0 and top_n <= 0:
        return names, {"total": total, "selected": total, "keep": 0, "semantic": 0,
                       "semantic_names": [], "expanded": [], "chain": []}

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

    # ── ★ 坐标系前置链闭包（寻址三元组）──
    #   见 `address_chain` docstring。**不占 N 的名额**（与 expand_with_deps 同级）。
    chain = []
    try:
        chain = address_chain(meta, chosen_set)
        chosen_set |= set(chain)
    except Exception:
        chain = []

    # ── 输出按原始顺序（可复现）──
    chosen = [nm for nm in names if nm in chosen_set]

    # ── ★ 2026-10-28：「闭包补进来的重契约工具」按体积回退 ──
    #   为什么需要：闭包（依赖/动词族/寻址链）是**启发式**，它会把"描述里被提到"
    #   但本轮根本用不上的重型协议工具一并拉进来。实测「设计时序图并写入」这一轮：
    #     36 个工具载荷 41,265 字符（≈13.7k tokens/轮），而
    #     `preview_model_changes` **一个就占 8,369 字符（20%）**——
    #     它是"强类型命令批预览"协议，与本任务用的文本包协议（import_sysml_text）
    #     互斥；模型看到两套写入协议只会更犹豫（真机里它正是先选了 create 系工具）。
    #   规则（**只看体积，不点名任何工具**）：
    #     · 语义 top-N 命中者（`ranked[:want]`）与动词族/寻址链命中的**保留** ——
    #       那是"用户真的提了这个动作"，体积再大也得给；
    #     · 仅由**描述互引**补进来的（`expanded` 里非动词族部分的近似），
    #       若其契约超过 `MCP_HEAVY_SCHEMA_CHARS`，则**不补**（省下的预算给正路工具）。
    #   代价与取舍：省 token 优先；若将来某个重工具真成为正路，用户话里会带它的动词族，
    #   届时由上面第一条保住（不会误杀）。
    heavy_skipped = []
    try:
        # 本轮**真正命中动词族**的组（用户话里出现了该族任一触发词）
        _q_low = str(user_input or "").lower()
        _trig = set()
        for _g, _ws in _INTENT_VERBS.items():
            if any(str(w).lower() in _q_low for w in _ws):
                _trig.add(_g)
        # 保底集：语义 top-N ∪ 寻址链 ∪ 动词族命中者（三者都是"用户真的要这个动作"）
        _keep_seed = set(ranked[:want]) | set(chain) | {
            nm for nm in names if any(name_hits_group(nm, g) for g in _trig)}
        _kept = []
        for nm in chosen:
            if nm in _keep_seed:
                _kept.append(nm)
                continue
            _sch = (meta.get(nm) or {}).get("inputSchema") or {}
            try:
                _ln = len(json.dumps(slim_schema(_sch), ensure_ascii=False))
            except Exception:
                _ln = 0
            if _ln > MCP_HEAVY_SCHEMA_CHARS:
                heavy_skipped.append(nm)
                continue
            _kept.append(nm)
        chosen = _kept
    except Exception:
        pass

    return chosen, {
        "total": total,
        "selected": len(chosen),
        "keep": want,
        "semantic": len([nm for nm in chosen if nm in scores]),
        "semantic_names": [nm for nm in semantic_hits if nm in chosen_set],
        "expanded": [nm for nm in names if nm in set(expanded)],
        "chain": [nm for nm in names if nm in set(chain)],
        "heavy_skipped": heavy_skipped,
    }


#: 意图动词族：请求里出现任意一个中文/英文词 ⇒ 把工具名含对应英文动词的工具纳入。
#: 为什么需要：粗分词打分对**短中文请求**（"新增一个测试包"）几乎无区分度，
#: 而工具名是英文（create_model_elements）⇒ 中文与英文名不相交 ⇒ 打分全 0
#: ⇒ 只能按"原始顺序前 N 个"保底，与任务毫无关系（实测就是这种情形）。
#: 这里做的是**动词族映射**，不是把某个具体工具写死：任何名字含这些动词的工具都受益。
#:
#: ★★ 2026-10-28（conv 9740 实证）：本表原本**自带一份词条**，与闸门用的
#: `_MCP_WRITE_VERB_TABLE` 各写一份 ⇒ 同一句话在两侧结论相反且无人报错。
#: 真机：用户原话「…并完成校验后，**写入**对应的包…」
#:   · 本表（闭包，加工具）只收「导入/写回/回写/import/upload/push」⇒ 不命中；
#:   · 闸门（减工具）同样不含「写入」⇒ 即便拉进来也会被剔掉。
#:   ⇒ `import_sysml_text`（MCP 里唯一能真正落库的工具）在两侧同时隐形，
#:     模型手里只剩 `create_model_elements` ⇒ 连猜 3 次类型名 ⇒ INVALID_ARGUMENTS×3
#:     ⇒ 当失败轮打满 ⇒ 整轮判死。
#: ⇒ 现在词条**全部引用 `_VERB_SYNONYMS`**，本表只声明"取哪几族"。
#:   顺带补齐 `validate` / `view` 两族：校验是写入前必经一步（缺席时模型会拿本地
#:   校验器当平台校验，结论不可迁移），出图族缺席时模型会拿 create 工具去"建视图"
#:   （工具选错，参数必然错 —— 正是 SEQUENCE_VIEW 那 3 次失败）。
_INTENT_VERBS = {g: _VERB_SYNONYMS[g] for g in (
    "create", "update", "delete", "move", "import", "export", "validate", "view")}


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
                    if nm not in picked and name_hits_group(nm, grp):
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
