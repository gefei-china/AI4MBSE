# -*- coding: utf-8 -*-
"""写入目标解析器 —— 把「用户点名的包」解析成**确定的 id**，供三处共用。

## 为什么不写"dataId 前置校验"（2026-10-28 米爸追问后的定型）

原先想到的做法是："拿到模型给的 dataId，校验它对不对，不对就拦"。
那是**在错误的输入上做校验** —— 它默认了"目标 id 由模型提供"。

真机证据（conv 9740）：模型在澄清卡里给的 `2114902516325482496`
在本会话回执里**一直是 parentId**（名字「通信子系统」），
而用户点名的 `SD_通信子系统时序图` 是它的**子包**（id 2115359040604536832）。
若只做"校验"，最好的结果也只是**拦住**；而正确的能力是
**从用户那句话说清楚的目标，直接把 id 解析出来**，根本不让模型去猜。

⇒ 本模块只做一件事：`resolve(...) -> 唯一的 (id, name, type, parent)`。
   · 解析出唯一目标 ⇒ 卡片、提示、执行闸**都用它**（单一事实源）；
   · 解析不唯一/解析不到 ⇒ 返回 `ambiguous` / `not_found` 与候选，
     **由界面让用户选**，而不是让模型闭眼挑一个。

## 判据为什么是通用的（不含任何平台特例）

只用三样东西：
  ① 用户原话里的名字候选（引号内的、含下划线/英文前缀的标识符样 token）；
  ② **本会话已落库的工具回执**里的元素清单（`dataId|elementId`, `name`, `type`, `parentId`）；
  ③ 名字等价关系（完全相等 > 互为子串 > 去分隔符后相等）。
不假设"一定是智源"、不假设"一定是包"、不假设任何接口字段名以外的约定
（字段名取 `dataId`/`elementId`/`id` 三种常见写法，缺哪个都能跑）。
⇒ 换成任何 MCP/HTTP 建模平台，只要回执里有"标识 + 名字 + 父标识"，本模块即可用。

## 与 `import_target_guard` 的关系

那个模块是**事后拦截**（给一个 id，判它是不是父包）。
本模块是**事中解析**（给出该用哪个 id）。二者**不是替代关系**：
· 解析成功 ⇒ 用解析结果（正常路径）；
· 解析不到（回执缺失/名字对不上）⇒ 退回 `import_target_guard` 的拦截口径，
  保守处理（宁可让人确认，不可覆盖错包）。
"""

import json
import re

#: 名字候选：引号包裹的（中英文引号皆可）
_QUOTED = re.compile(r"[「『\"“'“”']([^」』\"”'\n]{2,60})[」』\"”']")
#: 标识符样 token：`SD_通信子系统时序图`、`P-001`、`IBD_Foo` 这类
_IDENT = re.compile(r"\b([A-Za-z][A-Za-z0-9]*[_\-][\u4e00-\u9fa5A-Za-z0-9_\-]{1,40})")

#: 回执里可能承载"标识"的字段名（按优先级）
_ID_KEYS = ("dataId", "elementId", "id")
#: 回执里可能承载"名字"的字段名
_NAME_KEYS = ("name", "elementName", "displayName")


def _norm(s) -> str:
    """名字归一：去空白、去分隔符、转小写 —— 只用于**等价比较**，不回填原值。"""
    return re.sub(r"[\s_\-·．.]+", "", str(s or "")).lower()


def extract_candidates(user_input: str) -> list:
    """从用户原话里取「可能是在指名某个元素」的候选（去重、保序）。

    刻意**不做语义猜测**：只取引号内 / 标识符样 token。
    宁可少取（解析不到就走保守路径），也不把"通信子系统"这种泛指当目标。
    """
    txt = str(user_input or "")
    out, seen = [], set()

    def _add(x):
        x = str(x or "").strip()
        if not x or x in seen or len(x) > 40:
            return
        seen.add(x)
        out.append(x)

    for m in _QUOTED.finditer(txt):
        _add(m.group(1))
    for m in _IDENT.finditer(txt):
        _add(m.group(1))
    return out


def collect_elements(conn, conversation_id: int, limit: int = 300) -> list:
    """扫本会话已落库的工具回执，抽出元素清单 `[{id, name, type, parent}]`（只读、零网络）。

    为什么从回执里取而不是再查一次平台：
      · 回执是**模型当时确实看到过的**东西 ⇒ 用它解析，等于复现模型的视野，
        解析结果与模型的行为可对齐、可解释；
      · 零网络 ⇒ 澄清卡渲染前也能同步调用（渲染路径里不能有慢 IO）。
    """
    out = []
    try:
        rows = conn.execute(
            "SELECT result FROM tool_call_logs WHERE conversation_id=? "
            "ORDER BY id DESC LIMIT ?",
            (int(conversation_id or 0), int(limit))).fetchall()
    except Exception:
        return out
    for (res,) in rows:
        try:
            d = json.loads(res)
        except Exception:
            continue
        _walk_collect(d, out, 0)
    # 去重（同一 id 只留第一次出现的名字；回执里同 id 可能重复多次）
    dedup, seen = [], {}
    for e in out:
        if e["id"] in seen:
            continue
        seen[e["id"]] = True
        dedup.append(e)
    return dedup


def _walk_collect(node, out, depth):
    """递归收集带标识的项（限深，避免异常结构把栈打爆）。"""
    if depth > 8:
        return
    if isinstance(node, list):
        for x in node:
            _walk_collect(x, out, depth + 1)
        return
    if not isinstance(node, dict):
        return
    eid = ""
    for k in _ID_KEYS:
        v = node.get(k)
        if v not in (None, ""):
            eid = str(v)
            break
    if eid:
        nm = ""
        for k in _NAME_KEYS:
            if node.get(k):
                nm = str(node[k])
                break
        if nm:
            out.append({
                "id": eid,
                "name": nm,
                "type": str(node.get("type") or node.get("elementType") or ""),
                "parent": str(node.get("parentId") or node.get("ownerId") or ""),
            })
    for v in node.values():
        if isinstance(v, (list, dict)):
            _walk_collect(v, out, depth + 1)


def _match(cand: str, name: str) -> int:
    """候选与名字的匹配强度：3 完全相等 / 2 去分隔符相等 / 1 互为子串 / 0 不匹配。"""
    a, b = str(cand or ""), str(name or "")
    if not a or not b:
        return 0
    if a == b:
        return 3
    if _norm(a) == _norm(b):
        return 2
    if a in b or b in a:
        return 1
    return 0


def resolve(conn, conversation_id: int, user_input: str = "") -> dict:
    """解析「用户这笔写入该落到哪个元素」。

    :returns: 字典（**永远返回**，不抛异常）：
      · `status` —— ``resolved`` / ``ambiguous`` / ``not_found``
      · `target_id` / `name` / `type` / `parent_name` —— status=resolved 时有效
      · `candidates` —— ambiguous 时的候选（含 id/name/type）
      · `reason` —— 人话解释（可直接显示在卡片上，避免"静默失败"）
    """
    empty = {"status": "not_found", "target_id": "", "name": "", "type": "",
             "parent_name": "", "candidates": [], "reason": ""}
    try:
        txt = str(user_input or "")
        if not txt.strip():
            # 自取最后一条用户消息（闸门/卡片未必都持有原文）
            try:
                r = conn.execute(
                    "SELECT content FROM messages WHERE conversation_id=? AND role='user' "
                    "ORDER BY id DESC LIMIT 1", (int(conversation_id or 0),)).fetchone()
                txt = str((r[0] if r else "") or "")
            except Exception:
                txt = ""
        cands = extract_candidates(txt)
        if not cands:
            d = dict(empty)
            d["reason"] = "用户原话里没有可识别的元素名（引号内名称/标识符样 token）"
            return d
        elems = collect_elements(conn, conversation_id)
        if not elems:
            d = dict(empty)
            d["reason"] = "本会话还没有可用的元素回执 ⇒ 无法解析目标"
            return d

        # 逐候选打分；取最高分那档的全部命中
        best_score, best = 0, []
        for c in cands:
            hits = [(s, e) for e in elems for s in [_match(c, e["name"])] if s > 0]
            if not hits:
                continue
            top = max(s for s, _ in hits)
            if top > best_score:
                best_score, best = top, [e for s, e in hits if s == top]
            elif top == best_score:
                best.extend([e for s, e in hits if s == top])
        # 去重
        uniq, seen = [], set()
        for e in best:
            if e["id"] not in seen:
                seen.add(e["id"])
                uniq.append(e)

        if not uniq:
            d = dict(empty)
            d["reason"] = ("候选 %s 在本会话回执里对不上任何元素 ⇒ 不猜，"
                           "请先查询确认后再写入" % "、".join(cands[:3]))
            return d

        # ★★ 2026-10-28（conv 9740 实证）：**只有"名字相近"不算解析成功**。
        #   真机：用户点名 `SD_通信子系统时序图`，而该会话回执里另有一个
        #   `通信子系统时序图`（是模型自己新建的、名字少个前缀）。
        #   若把 best_score==1（互为子串）判成 resolved，解析器会**自信地给出错 id**
        #   —— 那比"解析不出"更危险：卡片会显示一个看起来很像的 id，用户照点就写错包。
        #   ⇒ 阈值定在 2（去掉分隔符后相等），子串级线索只作为**候选**呈现给人选。
        if best_score < 2:
            d = dict(empty)
            d["status"] = "ambiguous"
            d["candidates"] = uniq[:8]
            d["reason"] = ("回执里只有**名字相近**的元素（%s），没有与「%s」完全一致的 ——"
                           "相近不等于同一个，需要你确认是哪一个"
                           % ("、".join("%s[%s]" % (e["name"], e["type"] or "?")
                                       for e in uniq[:4]), "、".join(cands[:2])))
            return d

        # 多个命中时优先"容器类"（PACKAGE 等）—— 写入落点是容器，不是叶子特性
        if len(uniq) > 1:
            pkg = [e for e in uniq if "PACKAGE" in (e["type"] or "").upper()]
            if len(pkg) == 1:
                uniq = pkg
        if len(uniq) > 1:
            d = dict(empty)
            d["status"] = "ambiguous"
            d["candidates"] = uniq[:8]
            d["reason"] = ("候选名在回执里命中 %d 个元素（%s）⇒ 需要指定是哪一个"
                           % (len(uniq), "、".join(
                               "%s[%s]" % (e["name"], e["type"] or "?") for e in uniq[:4])))
            return d

        e = uniq[0]
        parent_name = ""
        if e["parent"]:
            for x in elems:
                if x["id"] == e["parent"]:
                    parent_name = x["name"]
                    break
        return {
            "status": "resolved",
            "target_id": e["id"],
            "name": e["name"],
            "type": e["type"],
            "parent_name": parent_name,
            "candidates": [],
            "reason": ("已解析：%s%s ⇒ id=%s"
                       % (e["name"], ("（%s）" % e["type"]) if e["type"] else "",
                          e["id"])),
        }
    except Exception as ex:
        d = dict(empty)
        d["reason"] = "解析异常：%s" % ex
        return d


def check(data_id, resolved: dict) -> tuple:
    """执行前判定：模型给的 `data_id` 与本解析结果是否一致。

    :returns: ``(verdict, reason)``；verdict ∈ ``ok`` / ``wrong`` / ``unknown``
      · `ok`      —— 与解析结果一致（或解析不出、无从判定 ⇒ 交给 HIL）
      · `wrong`   —— 解析出了确定目标，但模型给的是**别的 id** ⇒ 必须拦
      · `unknown` —— 解析不出唯一目标 ⇒ 不阻断，人工确认
    判据刻意**只认 id 相等**：名字相近不是放行理由（同名不同父很常见）。
    """
    did = str(data_id or "").strip()
    if not did:
        return "unknown", "本次调用未给出目标 id"
    st = str((resolved or {}).get("status") or "")
    if st != "resolved":
        return "unknown", "未能从上下文唯一解析目标（%s）" % (resolved or {}).get("reason", "")
    want = str(resolved.get("target_id") or "")
    if did == want:
        return "ok", "目标一致：%s（%s）" % (resolved.get("name"), did)
    return "wrong", (
        "写入目标与用户点名的不是同一个元素：调用给的是 %s，"
        "而「%s」的 id 是 %s（父：%s）⇒ 若目标是父包，覆盖写入会连带冲掉它下面的子树"
        % (did, resolved.get("name"), want, resolved.get("parent_name") or "未知"))
