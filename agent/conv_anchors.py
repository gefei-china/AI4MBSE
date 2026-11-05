# -*- coding: utf-8 -*-
"""会话**坐标锚点**：把对话中已确认的工程/分支/版本/元素落库，每轮注入，不依赖历史窗口。

## 为什么需要（用户实测质疑："我前面已经确定基于那个工程，是不是上下文遗漏了"）

**是的，机制上确实会丢。** 逐条核实（conv 9629）：

| 事实 | 数据 |
|---|---|
| 用户首次确定工程 | msg **4869**「基于这个工程"广汽方法论建模1022"获取工程的V2代码」 |
| 再次提问 | msg **4899**「在"通信子系统用例"包中新增一个"测试"包」（相隔 1h50m、17 条消息）|
| 历史加载机制 | `_load_history`：当前话题只取最近 **8 条**（`topic_current_max_msgs=8`）+ 总预算 3000 字符；更早的靠**语义回捞**（`topic_retrieve_threshold=0.18`）|
| 结果 | 4869 **在 8 条窗口之外**；这次没丢，是因为窗口内 4886/4888/4890/4892 四条**助手回复自己复述了工程名**——纯属运气 |

⇒ 只要助手某几轮回答里没复述工程名（短回答、只回一句"已完成"），锚点就**彻底丢失**，
模型只能重新 `query_projects`；而工程有 18 个 ⇒ 乱选 ⇒ 在错工程里搜包全空 ⇒ `PERMISSION_DENIED`。

**根因不是"预算太小"，而是"把锚点当成普通历史消息"**：工程/分支/版本是**会话级事实**
（一旦确定就该一直有效），不该和闲聊内容抢同一个滑动窗口。

## 机制

- **提取**：每次工具调用成功后，从**结果与参数**里递归找「id + name」配对
  （任何 `*Id`/`dataId`/`elementId` 键 + 同级 `name`），按 key 的语义归类到
  `project / branch / version / element` 槽位。**不写死具体字段名/平台**。
- **落库**：`conversation_anchors(conversation_id, slot, value, label)`，同槽位覆盖（后确定者优先）。
- **注入**：每轮拼 prompt 前取出来，作为「会话已确定的坐标（实测）」写进 system prompt，
  并明确「可直接复用，不要重新查询工程列表」。

## 纪律

- 只记**工具真实返回**的 id（不猜、不编）；
- 用户新指定目标时覆盖旧值（`set_anchor` 同槽位 upsert）；
- 表不存在时静默降级（不影响主流程）。
"""
import json
import re

TABLE = "conversation_anchors"

#: 槽位识别词（按 key 名归类，按优先级匹配）
_SLOT_RULES = (
    ("version", ("versionid",)),
    ("branch", ("branchid",)),
    ("project", ("projectid",)),
    ("element", ("dataid", "elementid", "ownerid", "parentid")),
)
#: 槽位显示名
_SLOT_LABEL = {"project": "工程", "branch": "分支", "version": "版本", "element": "元素"}
#: 同一轮里，这些 key 会作为"目标元素"覆盖 element 槽（比 ownerId 更具体）
_ELEMENT_KEYS = ("dataId", "elementId")


def ensure_table(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS %s (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id INTEGER NOT NULL,
            slot TEXT NOT NULL,
            value TEXT NOT NULL DEFAULT '',
            label TEXT NOT NULL DEFAULT '',
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(conversation_id, slot)
        )""" % TABLE)


#: 同一对象内多个 id 键并存时的**具体度**（越大越具体）。
#: 踩过的坑：`query_branches` 返回的分支对象同时含 `branchId` 和 `projectId`，
#: 而 `name` 是分支名 "master" ⇒ 若两者都记，project 槽会被覆盖成 "master"（错位）。
#: ⇒ 每个对象只取**一个**最具体的槽位。
_KEY_RANK = {
    "versionid": 90,
    "dataid": 80, "elementid": 80,
    "branchid": 70,
    "projectid": 60,
    "ownerid": 40, "parentid": 30,
}


def _slot_of(key: str) -> str:
    k = (key or "").lower()
    for slot, words in _SLOT_RULES:
        if any(w in k for w in words):
            return slot
    return ""


def _rank_of(key: str) -> int:
    return _KEY_RANK.get((key or "").lower(), 0)


def _collect_pairs(node, out, depth=0):
    """递归收集 `{...Id: x, name: y}` 形式的配对（通用，不认平台字段名）。

    每个对象**只取一个**最具体的 id 键（见 `_KEY_RANK`），避免"分支对象里的 projectId
    把工程槽覆盖成分支名"这种错位。
    """
    if depth > 6 or not isinstance(node, (dict, list)):
        return
    if isinstance(node, list):
        for it in node:
            _collect_pairs(it, out, depth + 1)
        return
    name = node.get("name") or node.get("displayName") or node.get("title")
    if isinstance(name, str) and name.strip():
        best = None
        for k, v in node.items():
            if not isinstance(k, str) or not isinstance(v, str) or not v.strip():
                continue
            slot = _slot_of(k)
            if not slot:
                continue
            if best is None or _rank_of(k) > _rank_of(best[0]):
                best = (k, slot, v.strip())
        if best:
            lbl = name.strip()[:100]
            # ★ 版本对象要带上状态：DRAFT/RELEASED 决定"能不能写"。
            #   不带状态的话，锚点里只会有个 versionId，模型无从判断该不该往这里写
            #   （conv 9639 就是拿 RELEASED 去 create ⇒ PERMISSION_DENIED）。
            st = str(node.get("status") or "").strip().upper()
            if best[1] == "version" and st:
                lbl = "%s（%s）" % (lbl, st)
            out.append({"slot": best[1], "value": best[2],
                        "label": lbl[:120], "key": best[0],
                        "_status": st})
    for v in node.values():
        if isinstance(v, (dict, list)):
            _collect_pairs(v, out, depth + 1)


def extract_anchors(result_text: str, arguments: dict | None = None) -> list:
    """从一次工具调用（结果 + 参数）里提取坐标锚点。返回 [{slot,value,label,key}]。

    参数也扫：`get_modeling_capabilities(parentId=...)` 这类"用户点名的目标"，
    结果里未必回显名称，但参数本身就是确定的事实。
    """
    out = []
    t = (result_text or "").strip()
    if t.startswith("{") or t.startswith("["):
        try:
            _collect_pairs(json.loads(t), out)
        except Exception:
            pass
    # 参数里带 name 的（如 create_model_elements(name=..., ownerId=...)）
    if isinstance(arguments, dict):
        nm = arguments.get("name")
        if isinstance(nm, str) and nm.strip():
            for k, v in arguments.items():
                if isinstance(v, str) and v.strip() and _slot_of(k) and k.lower() != "name":
                    slot = _slot_of(k)
                    out.append({"slot": slot, "value": v.strip(),
                                "label": nm.strip()[:120], "key": k, "_from_args": True})
        # parentId 单独处理：调用方明确指定的父容器（即使没有 name 也应记下 id，
        # label 留空 → 注入时显示"未命名"，模型仍可直接复用该 id）
        if isinstance(arguments.get("parentId"), str) and arguments["parentId"].strip():
            out.append({"slot": "element", "value": arguments["parentId"].strip(),
                        "label": "", "key": "parentId", "_from_args": True})
    # ③ element 槽位**只认"唯一"**：
    #   `query_data_tree` 返回的是"当前层列表"（根下 4 个包），任意一个都不是"目标"；
    #   若逐条记录就会互相覆盖，最后停在无关条目上（实测会记成"3 系统架构设计"）。
    #   规则：结果里 element 只有 1 个 ⇒ 唯一，可信；多于 1 个 ⇒ 丢弃，改为信任参数
    #   （`parentId`/`ownerId` 是调用方**明确指定**的目标）。
    elems = [a for a in out if a["slot"] == "element" and not a.get("_from_args")]
    if len(elems) > 1:
        out = [a for a in out if a["slot"] != "element" or a.get("_from_args")]
    # ★ version 槽位：一次查询常同时返回 RELEASED + DRAFT（`includeDraft=true`）。
    #   **优先保留 DRAFT** —— 写操作只能落在草稿版本；若记成 RELEASED，
    #   模型会拿它去 create ⇒ PERMISSION_DENIED（conv 9639 实测）。
    vers = [a for a in out if a["slot"] == "version"]
    if len(vers) > 1:
        drafts = [a for a in vers if a.get("_status") == "DRAFT"]
        keep = drafts[-1] if drafts else vers[-1]
        out = [a for a in out if a["slot"] != "version"] + [keep]
    # 同一轮：更具体的 element 键优先
    specific = [a for a in out if a["slot"] == "element" and a["key"] in _ELEMENT_KEYS]
    if specific:
        out = [a for a in out if a["slot"] != "element"] + specific
    # 按 (slot, value) 去重（parentId 既可能来自"带 name 的参数扫描"也可能来自单独处理）
    seen, uniq = set(), []
    for a in out:
        k = (a["slot"], a["value"])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(a)
    return uniq


def set_anchor(conn, conversation_id: int, slot: str, value: str, label: str = "") -> None:
    """写入/覆盖一个槽位（同会话同槽位只留最新一条）。"""
    if not conversation_id or not slot or not value:
        return
    try:
        ensure_table(conn)
        conn.execute(
            "INSERT INTO %s (conversation_id, slot, value, label) VALUES (?,?,?,?) "
            "ON CONFLICT(conversation_id, slot) DO UPDATE SET "
            "value=excluded.value, label=excluded.label, updated_at=CURRENT_TIMESTAMP" % TABLE,
            (conversation_id, slot, value, (label or "")[:120]))
    except Exception:
        pass


def record_from_tool(conn, conversation_id: int, result_text: str,
                     arguments: dict | None = None) -> int:
    """一次工具调用后调用：提取并落库，返回写入条数。"""
    n = 0
    for a in extract_anchors(result_text, arguments):
        try:
            set_anchor(conn, conversation_id, a["slot"], a["value"], a["label"])
            n += 1
        except Exception:
            pass
    return n


def load_anchors(conn, conversation_id: int) -> list:
    """取该会话全部锚点（按 project→branch→version→element 顺序，便于阅读）。"""
    order = {"project": 0, "branch": 1, "version": 2, "element": 3}
    try:
        ensure_table(conn)
        rows = conn.execute(
            "SELECT slot, value, label, updated_at FROM %s WHERE conversation_id=?"
            % TABLE, (conversation_id,)).fetchall()
        ds = [dict(r) for r in rows]
        ds.sort(key=lambda d: order.get(d.get("slot"), 9))
        return ds
    except Exception:
        return []


def render_block(anchors: list) -> str:
    """渲染成注入 system prompt 的文字块。空则返回 ""（零回归面）。"""
    if not anchors:
        return ""
    lines = []
    for a in anchors:
        slot = a.get("slot") or ""
        lbl = _SLOT_LABEL.get(slot, slot)
        name = (a.get("label") or "").strip()
        val = (a.get("value") or "").strip()
        if slot == "version":
            _is_draft = "DRAFT" in (name or "").upper()
            _warn = ("——**草稿版本，写操作落在这里**" if _is_draft
                     else "——**已发布版本只读，写入必须另有草稿版本**")
            lines.append("- 版本：%s（versionId=%s）%s" % (name or "未命名", val, _warn))
        elif slot == "element":
            lines.append("- 目标元素：%s（dataId=%s）" % (name or "未命名", val))
        else:
            lines.append("- %s：%s（%sId=%s）" % (lbl, name or "未命名",
                                                "project" if slot == "project" else "branch", val))
    return (
        "\n\n【会话已确定的坐标（系统从本轮之前的真实工具回执提取，可直接复用）】\n"
        + "\n".join(lines)
        + "\n**这些值直接使用，不要重新 query_projects / query_branches / query_model_versions 去猜；"
          "只有用户明确要换工程/分支/版本，或当前坐标查不到目标时，才重新查询并在结果里确认。**"
    )


def clear(conn, conversation_id: int) -> None:
    try:
        ensure_table(conn)
        conn.execute("DELETE FROM %s WHERE conversation_id=?" % TABLE, (conversation_id,))
    except Exception:
        pass
