# -*- coding: utf-8 -*-
"""工具回执「判读句」注解层 —— 确定性补一句结论，不依赖模型自觉。

## 为什么必须存在（conv 9726 真机实证，2026-10-28）

用户要求「写入包 `SD_通信子系统时序图`」，会话以「该包不存在，需新建」收尾。
但同一轮的**工具回执里目标包出现了两次**：

| 调用 | 参数 | 回执 |
|---|---|---|
| `query_data_tree`（#5503） | parentId=通信子系统 | 7 个子包里**第 2 个就是** `SD_通信子系统时序图`(2115359040604536832) |
| `search_model_elements`（#5510） | keyword=SD_通信子系统时序图 | 命中同一 ID |
| `query_data_tree`（#5512） | parentId=2115359040604536832 | `items: []`（它是空包，**空包是正常的**） |

⇒ 模型把「**空包**」读成「**不存在**」，并据此放弃写入。

三处可确定的成因（都不是"模型笨"，而是回执本身歧义）：

1. **空 ≠ 不存在**：`items: []` 与"查不到这个节点"在回执里长得一模一样，
   回执又不回显"我查的是谁"，模型只能猜。
2. **命中不显眼**：7 条同构 JSON 里要肉眼比对中文名，模型漏了；而
   `search_model_elements` 的命中结果也没有一句"命中即存在"的判定。
3. **本层过滤 ≠ 全局不存在**：`query_data_tree(keyword=SD_)` 只在**某一层的直接子级**
   里过滤，0 命中被当成了"工程里没有"。同理 `query_projects` 的 0 命中只是
   名称包含匹配失败（实测用户写「广汽建模方法论1022」、真实名是「广汽方法论建模1022」）。
   → 模型为此多花了 1 次调用重查，并把"0 命中"写成了"找不到工程"。

## 设计约束

- **纯函数 + 零 IO**：只吃 `(工具名, 参数, 回执)` 吐回执，便于用真数据做门禁。
- **只增不改**：不修改 `result` 原文（原文是审计口径），只**追加**一段
  `\\n\\n【判读】…`，并另挂 `note` 字段供前端/评测读取。
- **宁缺毋滥**：解析不出结构就原样返回，绝不编造结论。
"""
import json

__all__ = ["annotate", "ANNOTATED_TOOLS", "build_note"]

#: 需要判读的工具（收窄到"实测踩过坑"的四个；其余工具原样透传）
ANNOTATED_TOOLS = ("query_data_tree", "search_model_elements",
                   "query_projects", "query_model_versions")

#: 清单里最多列几个名字（再多模型也不比对了，只报总数）
_MAX_NAMES = 40


def _as_dict(payload):
    """把回执解析成 dict；解析不出来 ⇒ None（调用方据此原样透传）。"""
    if isinstance(payload, dict):
        return payload
    if not isinstance(payload, str):
        return None
    s = payload.strip()
    if not s:
        return None
    try:
        d = json.loads(s)
    except Exception:
        return None
    return d if isinstance(d, dict) else None


def _name_list(items, limit=_MAX_NAMES):
    """把 items 压成一行「名字(类型)」清单（顺序即回执顺序，便于逐字核对）。"""
    out = []
    for it in (items or [])[:limit]:
        if not isinstance(it, dict):
            continue
        nm = str(it.get("name") or it.get("elementName") or "").strip()
        if not nm:
            continue
        tp = str(it.get("type") or "").strip()
        out.append("%s(%s)" % (nm, tp) if tp else nm)
    more = ""
    if items and len(items) > limit:
        more = "…等共 %d 项" % len(items)
    return "、".join(out) + more


def _ids_of(items, limit=8):
    out = []
    for it in (items or [])[:limit]:
        if not isinstance(it, dict):
            continue
        eid = str(it.get("elementId") or it.get("dataId") or "").strip()
        nm = str(it.get("name") or "").strip()
        if eid:
            out.append("%s=%s" % (nm or "?", eid))
    return "、".join(out)


def paging_complete(payload) -> bool:
    """回执是否"取全了"（**没有截断证据**就返回 True）。

    判据只用回执里**明确写出来**的信号：`hasMore=True` 或 `total > len(items)`。
    解析不出结构 ⇒ 返回 True（不把"看不懂"当成"不完整"：
    否则所有文本类工具的去重都会被永久关掉，等于把一个老功能悄悄废掉）。
    """
    d = _as_dict(payload)
    if d is None:
        return True
    if d.get("hasMore") is True:
        return False
    try:
        total = d.get("total")
        if total is not None and str(total).strip() != "":
            if int(str(total)) > len(d.get("items") or []):
                return False
    except Exception:
        pass
    return True


def _note_tree(args, d):
    """query_data_tree：空包 ≠ 不存在；非空则把子元素清单提到一句里。"""
    items = d.get("items") or []
    parent = str((args or {}).get("parentId") or "").strip()
    ver = (args or {}).get("version") or {}
    vid = str((ver or {}).get("versionId") or "").strip()
    kw = str((args or {}).get("keyword") or "").strip()
    where = ("节点 %s" % parent) if parent else "分支根"
    scope = ("，版本 %s" % vid) if vid else ""
    if items:
        head = "【判读】%s 下查到 %d 个子元素%s%s：%s。" % (
            where, len(items), "（关键词「%s」过滤）" % kw if kw else "", scope,
            _name_list(items))
        tail = ("**这些元素确实存在**（回执即证据）。若要找的目标名出现在上面，"
                "直接用它的 id（%s）作写入目标，不要再判「不存在」。"
                % _ids_of(items))
        if not paging_complete(d):
            tail += "⚠️ 本次未取全（hasMore/total 未覆盖），必要时加大 pageSize 重查。"
        return head + tail
    # 空结果：区分"本层过滤 0 命中"与"空包"
    if kw:
        return ("【判读】按关键词「%s」在 %s 的**直接子级**中 0 命中%s。"
                "⚠️ 这是**本层过滤未命中**，不等于该元素不存在——它可能在更深层或别的包。"
                "要判存在性请用 `search_model_elements`（全工程版本检索），"
                "或去掉 keyword 重新展开本层。" % (kw, where, scope))
    return ("【判读】%s 查询成功、但**无子元素**%s ⇒ 这是一个**空包/空节点**。"
            "⚠️ **空 ≠ 不存在**：该包本身已存在（可以直接作为写入目标），"
            "只是里面还没有内容。不要把它读成「这个包不存在」。"
            % (where, scope))


def _note_search(args, d):
    """search_model_elements：命中即存在；0 命中不等于不存在。"""
    items = d.get("items") or []
    kw = str((args or {}).get("keyword") or "").strip()
    if items:
        return ("【判读】关键词「%s」命中 %d 条：%s。"
                "**命中即证明这些元素在目标版本内存在**（id：%s）；"
                "PACKAGE 的 ownerId 就是它的父节点，可直接作为写入/展开目标。"
                % (kw, len(items), _name_list(items), _ids_of(items)))
    return ("【判读】关键词「%s」0 命中。本工具做的是**名称包含匹配**，"
            "⇒ 0 命中不足以断定「不存在」（可能是用词不同、被截断、或不在本版本）。"
            "下一步：换更短的关键词（取 2~4 字核心词）再试一次，"
            "或用 `query_data_tree` 沿父包逐层展开确认，再下结论。" % kw)


def _note_projects(args, d):
    """query_projects：0 命中是"名字没对上"，不是"没有工程"。"""
    items = d.get("items") or []
    kw = str((args or {}).get("keyword") or "").strip()
    if items:
        return "【判读】命中 %d 个工程：%s。" % (len(items), _name_list(items))
    return ("【判读】关键词「%s」0 命中。工程名是**包含匹配**，0 命中通常说明"
            "名字没对上（实测：用户写「广汽建模方法论1022」，实际工程名是"
            "「广汽方法论建模1022」）。下一步：**换更短的关键词**（取 2~4 字）再查一次，"
            "不要直接回复「找不到工程」。" % kw)


def _note_versions(args, d):
    """query_model_versions：把"草稿/已发布"讲清，避免写错版本。"""
    items = d.get("items") or []
    if not items:
        return ("【判读】该分支下未返回任何版本。"
                "写入前必须先拿到**草稿态**版本号（includeDraft=true），否则写不进去。")
    brief = []
    for it in items[:_MAX_NAMES]:
        if not isinstance(it, dict):
            continue
        brief.append("%s(%s,%s)" % (str(it.get("name") or it.get("versionNumber") or "?"),
                                    str(it.get("status") or "?"),
                                    str(it.get("versionId") or "")))
    return ("【判读】共 %d 个版本：%s。写操作只能用**草稿/可写**版本；"
            "RELEASED 是已发布快照，不要往里写。" % (len(items), "、".join(brief)))


_NOTES = {
    "query_data_tree": _note_tree,
    "search_model_elements": _note_search,
    "query_projects": _note_projects,
    "query_model_versions": _note_versions,
}


def build_note(name, args, payload):
    """生成判读句；不需要/解析不出 ⇒ 空串。"""
    fn = _NOTES.get(str(name or ""))
    if fn is None:
        return ""
    d = _as_dict(payload)
    if d is None:
        return ""
    try:
        return str(fn(args or {}, d) or "")
    except Exception:
        return ""          # 判读失败绝不阻断工具返回（旁路原则）


def annotate(name, args, res):
    """给一次工具回执补上判读句（**原地不改结构，只追加**）。

    :param res: `_exec_tool_call` 的标准返回字典 `{ok, result, ...}`
    :returns: 同一个字典（便于链式调用）

    行为：
      · `ok=False` / 非目标工具 / 解析失败 ⇒ **原样返回**（不编造结论）；
      · 正常 ⇒ `result` 末尾追加一段 `\\n\\n【判读】…`，并挂 `note` 字段。
    为什么不覆盖 result 原文：原文是审计与回归的唯一口径，
    改写它会让"日志里看到的"与"模型看到的"分叉（本仓已有同类教训）。
    """
    if not isinstance(res, dict) or not res.get("ok"):
        return res
    note = build_note(name, args, res.get("result"))
    if not note:
        return res
    res["note"] = note
    base = res.get("result")
    if isinstance(base, str) and base:
        if note in base:
            return res
        res["result"] = base + "\n\n" + note
    return res
