# -*- coding: utf-8 -*-
"""`zhiyuan_sysmlv2_import` 的**目标包一致性闸**（2026-10-28）。

## 为什么必须有（conv 9740 真实澄清卡里的一个错误 id）

那一轮模型已经跑通校验（`sysml_v2_validate` 判据 pass，0 错），弹出澄清卡问：

> 是否将已通过校验的「通信子系统时序图」…**覆盖写入包「SD_通信子系统时序图」
> （dataId 2114902516325482496）**？

但 `2114902516325482496` 不是目标包 —— 从同会话三条真机回执（conv 9736/9737/9738）
一致反查得到：**它的名字是「通信子系统」，是父包**。
目标包「SD_通信子系统时序图」是 `2115359040604536832`。

该工具语义是**覆盖导入**（把自己解析出的子树整体写进目标包）
⇒ 一旦用户按卡片点了"确认写入"，会**覆盖掉「通信子系统」下已有的
IBD/功能定义/结构定义/系统需求/用例 等子树** —— 不可逆的数据破坏。

⇒ 写入前必须做一次**确定性**的一致性校验：id 对应的**真实名字**
与用户请求里点名的包名必须能对上；对不上就拒绝，并把候选报给模型改正。

## 设计

- `extract_package_names()` 纯函数：从用户原话取包名候选（引号/书名号内 + `SD_` 标识符）。
- `resolve_name_from_receipts()`：从**本会话已拿到的工具回执**里反查 dataId 的真实名字
  （零额外网络调用；查不到返回 None ⇒ 不阻断，交给 HIL 人工确认）。
- `judge()` 纯函数：三态 —— allow / block（名字冲突）/ unknown（查不到名字）。

宁缺毋滥：查不到名字**不阻断**（否则首次写入永远无法进行），但
**能查到且对不上就一定拦**（这是唯一会造成不可逆破坏的情形）。
"""
from __future__ import annotations

import json
import re

__all__ = ["extract_package_names", "resolve_name_from_receipts", "judge",
           "GUARDED_TOOL"]

#: 需要本闸守护的工具（覆盖导入语义）。其余工具不受影响。
GUARDED_TOOL = "zhiyuan_sysmlv2_import"

#: 包名候选的边界字符（引号/书名号/括号/空白/标点）
_QUOTED = re.compile(r"[「『\"“']([^」』\"”'\n]{2,60})[」』\"”']")
#: 形如 SD_通信子系统时序图 的标识符（ASCII 前缀 + 中文，常见于本仓视图包命名）
_ALNUM_CN = re.compile(r"\b([A-Za-z]{1,6}[_\-][\u4e00-\u9fa5A-Za-z0-9_\-]{1,40})")


def _has_cjk_run(x: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fa5]{2,}", x))


def extract_package_names(user_input: str) -> list:
    """从用户原话里取「可能是在指一个包」的名字候选（去重、保序）。

    ⚠️ 判据要**保守**：整句（含逗号句号的长片段）不是包名。
    第一版把「获取工程数据，分析并帮我设计…」这句也当成了候选（引号里是整句话），
    结果与真名比对时必然"都对不上"⇒ 会误判成 block。
    ⇒ 只接受：不含句读标点、且长度 ≤ 24 的片段。
    """
    txt = str(user_input or "")
    out, seen = [], set()
    _BAD = "，。、；：？！,.;:?!\n\t（）()【】[]"

    def _add(x):
        x = str(x or "").strip()
        if not x or x in seen:
            return
        if len(x) > 24:
            return
        if any(ch in x for ch in _BAD):
            return
        seen.add(x)
        out.append(x)

    for m in _QUOTED.finditer(txt):
        _add(m.group(1))
    for m in _ALNUM_CN.finditer(txt):
        _add(m.group(1))
    return out


def is_parent_of_target(connections, conversation_id: int, data_id, user_input: str):
    """`data_id` 是否是「用户点名那个包」的**父包**（本会话回执即可判，零网络）。

    ★ 这是最贴本次事故的那个信号：conv 9740 的澄清卡要把内容**覆盖导入**到
      `2114902516325482496`，而该 id 在同会话回执里**一直以 parentId 出现**，
      其子项里就含用户点名的「SD_通信子系统时序图」⇒ 它是父包，不是目标包。
      覆盖导入会冲掉父包下的既有子树（IBD/功能定义/结构定义/需求/用例）。

    :returns: `(True, 子包名)` / `(False, "")`
    """
    try:
        did = str(data_id or "").strip()
        if not did:
            return False, ""
        txt = str(user_input or "")
        if not txt.strip():
            # 闸内**自取**用户原话（不依赖调用方是否有 user_input 上下文）
            try:
                r = connections.execute(
                    "SELECT content FROM messages WHERE conversation_id=? AND role='user' "
                    "ORDER BY id DESC LIMIT 1", (int(conversation_id or 0),)).fetchone()
                txt = str((r[0] if r else "") or "")
            except Exception:
                txt = ""
        cands = extract_package_names(txt)
        if not cands:
            return False, ""
        rows = connections.execute(
            "SELECT result FROM tool_call_logs "
            "WHERE conversation_id=? AND tool_name='query_data_tree' "
            "AND arguments LIKE ? ORDER BY id DESC LIMIT 200",
            (int(conversation_id or 0), "%" + did + "%")).fetchall()
        for (res,) in rows:
            try:
                d = json.loads(res)
            except Exception:
                continue
            for it in (d.get("items") or []):
                if not isinstance(it, dict):
                    continue
                nm = str(it.get("name") or "")
                if not nm:
                    continue
                for c in cands:
                    if nm == c or c in nm or nm in c:
                        return True, nm
        return False, ""
    except Exception:
        return False, ""


def resolve_name_from_receipts(connections, conversation_id: int, data_id, limit: int = 400):
    """从**本会话已落库的工具回执**里反查 `data_id` 的真实名字。

    :param connections: 一个 `sqlite3.Connection`（调用方给，避免本模块自己建连）
    :returns: 名字字符串；查不到 ⇒ None

    做法：扫 `tool_call_logs.result` 里含该 id 的 JSON 回执，取 `name`。
    只读、零网络；找不到就是找不到（不猜）。
    """
    try:
        did = str(data_id or "").strip()
        if not did:
            return None
        rows = connections.execute(
            "SELECT tool_name, result FROM tool_call_logs "
            "WHERE conversation_id=? AND result LIKE ? ORDER BY id DESC LIMIT ?",
            (int(conversation_id or 0), "%" + did + "%", int(limit))).fetchall()
        for _tn, res in rows:
            try:
                d = json.loads(res)
            except Exception:
                continue
            found = _walk_find(d, did)
            if found:
                return found
        return None
    except Exception:
        return None


def _walk_find(node, did, depth: int = 0):
    """在任意嵌套 JSON 里找 `dataId/elementId == did` 的项的 `name`。"""
    if depth > 8:
        return None
    if isinstance(node, list):
        for x in node:
            r = _walk_find(x, did, depth + 1)
            if r:
                return r
        return None
    if isinstance(node, dict):
        for k in ("dataId", "elementId", "id"):
            if str(node.get(k) or "") == did:
                nm = node.get("name") or node.get("elementName")
                if nm:
                    return str(nm)
        for v in node.values():
            if isinstance(v, (list, dict)):
                r = _walk_find(v, did, depth + 1)
                if r:
                    return r
    return None


def judge(user_input: str, data_id, known_name):
    """三态判定：``("allow"|"block"|"unknown", reason)``。

    · `unknown`：id 或名字缺失、或回执里查不到该 id ⇒ **放行**（交给 HIL 人工确认）
    · `block`  ：查到了名字、但用户原话里**没有任何候选**能与它对上 ⇒ 拒绝
    · `allow`  ：名字与某个候选互为子串（含完全相等）
    """
    did = str(data_id or "").strip()
    nm = str(known_name or "").strip()
    if not did or not nm:
        return "unknown", "缺少目标包 id 或其真实名字（回执里未查到）⇒ 不阻断，交由人工确认"
    cands = extract_package_names(user_input)
    if not cands:
        return "unknown", "用户原话里没有可识别的包名候选 ⇒ 不阻断，交由人工确认"
    for c in cands:
        if c == nm or c in nm or nm in c:
            return "allow", "目标包名与用户点名一致：%s ↔ %s" % (nm, c)
    return "block", ("目标包 id %s 的真实名字是「%s」，与用户点名的任何一个都对不上（%s）"
                     "——覆盖导入会破坏该包既有内容，已拒绝执行"
                     % (did, nm, "、".join(cands[:5])))
