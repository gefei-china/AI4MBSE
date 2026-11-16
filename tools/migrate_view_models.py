# -*- coding: utf-8 -*-
"""存量视图重投影（2026-11-09）。

背景（实测）：`artifacts.meta.sysml_views` 里存的是**视图生成时的快照**。
本轮修掉三处生成侧缺陷后，新生成的视图变好了，但存量仍是旧快照：

| 指标 | 存量实测（230 个视图产物 / 3976 个视图节点） |
|---|---|
| 节点带 `parent`（包框要靠它） | **0** |
| 含重复 id 的视图 | **55** |
| 含乱码（U+FFFD / 错码）的标签 | **65** |

本脚本只用**客观事实**重投影，不改任何名字、不改版本链：
  · 代码来源：同 `conversation_id` + `message_id` 的 `sysml_versions.code_text`
    （实测命中 222/230；视图产物自身没有 file_path / preview_content，也不带 artifact_id）
  · 视图类型：沿用该产物里**已有的键**（不新增、不删除视图）
  · 同义键（`check` / `quality_check` 等）在新结果缺失时保留旧值

运行：
  <venv>/python.exe -X utf8 tools/migrate_view_models.py            # 默认 dry-run
  <venv>/python.exe -X utf8 tools/migrate_view_models.py --apply    # 落库（先自动备份）
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.config import DB_PATH  # noqa: E402


def _code_for(conn, conv_id, msg_id):
    """取该消息（优先）或该会话的 SysML 代码。返回 (code, 来源说明)。"""
    if msg_id:
        r = conn.execute(
            "SELECT code_text FROM sysml_versions WHERE conversation_id=? AND message_id=? "
            "AND length(coalesce(code_text,''))>0 ORDER BY id DESC LIMIT 1",
            (conv_id, msg_id)).fetchone()
        if r:
            return r[0], "msg"
    r = conn.execute(
        "SELECT code_text FROM sysml_versions WHERE conversation_id=? "
        "AND length(coalesce(code_text,''))>0 ORDER BY id DESC LIMIT 1",
        (conv_id,)).fetchone()
    return (r[0], "conv") if r else (None, "")


_RE_CODE_FENCE = re.compile(r"```(?:sysml|SysML|kerML|kerml)?\s*\n([\s\S]{80,}?)```")


def _code_for_message(conn, conv_id, msg_id, content):
    """历史消息专用：**严格**取同消息代码，禁用会话级兜底。

    为什么严格：353 条带视图快照的消息里，只有 75 条能精确命中
    `sysml_versions.message_id`，92 条只能拿到「会话里最新的一版代码」，
    186 条完全无代码。若用会话最新代码重投影早期消息的视图，会把 v0.5
    的视图套到 v0.1 的消息上 —— 内容看似合理、实则张冠李戴，比乱码更危险。
    消息正文里自带的 ```sysml 代码块是同一条消息的可靠来源（实测命中 106 条），
    优先于任何跨消息推断。
    """
    r = conn.execute(
        "SELECT code_text FROM sysml_versions WHERE conversation_id=? AND message_id=? "
        "AND length(coalesce(code_text,''))>0 ORDER BY id DESC LIMIT 1",
        (conv_id, msg_id)).fetchone()
    if r and r[0]:
        return r[0], "version"
    m = _RE_CODE_FENCE.search(content or "")
    if m and ("package " in m.group(1) or "part def" in m.group(1)):
        return m.group(1), "fence"
    return None, ""


def _dedupe_snapshot(views, _dedupe_view):
    """无可靠代码时的**无损**修复：只去掉同 id 重复节点 / 重复边。

    不改名、不增删视图、不引入任何外部代码 ⇒ 零语义风险。
    直接复用生成侧 `view_generator._dedupe_view`，保证两处口径同源。
    """
    merged, changed = {}, False
    for vt, v in (views or {}).items():
        if not isinstance(v, dict):
            merged[vt] = v
            continue
        ns, es = _dedupe_view(v.get("nodes") or [], v.get("edges") or [])
        if len(ns) != len(v.get("nodes") or []) or len(es) != len(v.get("edges") or []):
            changed = True
        nv = dict(v)
        nv["nodes"], nv["edges"] = ns, es
        merged[vt] = nv
    return merged, changed


def _stats(views):
    """视图集合的可量化快照：节点数 / 带 parent / 重复 id 的视图数 / 乱码视图数。"""
    nodes = parents = nodes_after = dup_views = moji_views = 0
    for v in (views or {}).values():
        ns = (v or {}).get("nodes") or []
        ids = [n.get("id") for n in ns]
        nodes += len(ids)
        nodes_after += len(set(ids))
        parents += sum(1 for n in ns if n.get("parent"))
        if len(ids) != len(set(ids)):
            dup_views += 1
        if any("\ufffd" in str(n.get("name") or "") for n in ns):
            moji_views += 1
    return {"nodes": nodes, "uniq": nodes_after, "parents": parents,
            "dup_views": dup_views, "moji_views": moji_views}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写库（默认 dry-run）")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 条（调试用）")
    ap.add_argument("--target", choices=["artifacts", "messages", "both"], default="artifacts",
                    help="artifacts=产物 meta.sysml_views（默认）；messages=历史消息 card_data；both=两者")
    args = ap.parse_args()

    from view_generator import generate_views_from_sysml, _dedupe_view

    # ⚠️ 连接必须在两个分支之前建立：messages 分支同样要用 conn。
    # （曾把 _migrate_messages 插在创建连接之前，ast.parse 查不出，实跑才报
    #  UnboundLocalError —— 语法检查不等于可用性验证。）
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    if args.target in ("messages", "both"):
        rc = _migrate_messages(conn, generate_views_from_sysml, _dedupe_view, args)
    else:
        rc = 0
    if args.target == "messages":
        return rc
    rows = conn.execute(
        "SELECT id, conversation_id, message_id, title, meta FROM artifacts "
        "WHERE typeof(meta)='text' AND meta LIKE '%sysml_views%' ORDER BY id").fetchall()
    if args.limit:
        rows = rows[:args.limit]

    before = {"nodes": 0, "uniq": 0, "parents": 0, "dup_views": 0, "moji_views": 0}
    after = dict(before)
    plan, skipped = [], []

    # 先把产物按「代码来源」分组：同一条消息的多个视图产物共用一次解析。
    # 实测单次 generate 约 4.5s（大头是 Java 解析），逐个跑 222 条要 17 分钟；
    # 分组后一次调用生成该消息需要的全部视图类型，解析次数降到「消息数」级。
    groups = {}
    for r in rows:
        try:
            meta = json.loads(r["meta"] or "{}")
        except Exception:
            skipped.append((r["id"], "meta 不是合法 JSON"))
            continue
        sv = meta.get("sysml_views")
        if not isinstance(sv, dict) or not isinstance(sv.get("views"), dict) or not sv["views"]:
            skipped.append((r["id"], "无 sysml_views.views"))
            continue
        b = _stats(sv["views"])
        for k in before:
            before[k] += b[k]
        key = (r["conversation_id"], r["message_id"] or 0)
        groups.setdefault(key, []).append((r, meta, sv, b))

    print(f"待处理 {sum(len(v) for v in groups.values())} 条产物，归并为 {len(groups)} 组（每组一次解析）")

    for (conv_id, msg_id), items in groups.items():
        code, src = _code_for(conn, conv_id, msg_id)
        want = sorted({k for _r, _m, sv, _b in items for k in sv["views"].keys()})
        res = None
        if code:
            try:
                res = generate_views_from_sysml(code, view_types=want,
                                                intent=items[0][2].get("intent"))
            except Exception as e:
                res = None
                reason = f"重投影失败: {type(e).__name__}: {e}"
        if not res or not (res.get("views") or {}):
            for r, _m, _sv, b in items:
                skipped.append((r["id"], "会话内无可用代码" if not code else "重投影结果为空"))
                for k in after:
                    after[k] += b[k]
            continue
        for r, meta, sv, b in items:
            sub = {k: v for k, v in (res["views"] or {}).items()
                   if k in sv["views"] and (v or {}).get("nodes")}
            if not sub:
                skipped.append((r["id"], "该视图类型重投影为空（保留旧快照）"))
                for k in after:
                    after[k] += b[k]
                continue
            merged = dict(sv["views"])
            merged.update(sub)
            a = _stats(merged)
            for k in after:
                after[k] += a[k]
            new_sv = dict(sv)
            new_sv["views"] = merged
            for k, v in (res or {}).items():
                if k != "views" and k not in new_sv:
                    new_sv[k] = v
            plan.append((r["id"], src, json.dumps(meta, ensure_ascii=False),
                         json.dumps({**meta, "sysml_views": new_sv}, ensure_ascii=False)))

    def line(name, d):
        return (f"{name}: 节点 {d['nodes']}（唯一 {d['uniq']}） 带 parent {d['parents']} "
                f"重复 id 的视图 {d['dup_views']} 乱码视图 {d['moji_views']}")

    print("=" * 78)
    print(line("重投影前", before))
    print(line("重投影后", after))
    print(f"计划更新 {len(plan)} 条 / 跳过 {len(skipped)} 条 / 共扫描 {len(rows)} 条")
    if skipped:
        from collections import Counter
        print("跳过原因：" + "；".join(f"{k}×{v}" for k, v in Counter(x[1] for x in skipped).most_common(6)))
    delta_parent = after["parents"] - before["parents"]
    dup_before = before["nodes"] - before["uniq"]
    dup_after = after["nodes"] - after["uniq"]
    print(f"Δ 带 parent：+{delta_parent}    重复节点（总数−唯一）：{dup_before} → {dup_after}    "
          f"乱码视图：{before['moji_views']} → {after['moji_views']}")
    print("=" * 78)

    if not args.apply:
        print("（dry-run，未写库；加 --apply 落库）")
        return 0
    if not plan:
        print("无变更。")
        return 0

    bak = f"{DB_PATH}.bak_viewreproj_{time.strftime('%Y%m%d_%H%M%S')}"
    shutil.copy2(DB_PATH, bak)
    print(f"已备份：{bak}")
    cur = conn.cursor()
    for aid, src, old, new in plan:
        cur.execute("UPDATE artifacts SET meta=? WHERE id=?", (new, aid))
    conn.commit()
    print(f"已更新 {len(plan)} 条。")
    return 0


def _migrate_messages(conn, generate_views_from_sysml, _dedupe_view, args) -> int:
    """历史消息 `card_data.sysml_views` 重投影（严格代码来源 + 无损兜底）。

    为什么必须做：前端加载历史消息时读的是 **卡片数据里的视图快照**
    （`static/js/mods/06-cards.js` / `05-markdown.js` 走 `cd.sysml_views`），
    只改 `artifacts.meta` 用户在当前会话里看不到变化。
    实测：SQL `LIKE '%sysml_views%'` 粗筛 353 条，但其中 **275 条只是文本里
    出现了这个词**，真正带 `sysml_views.views` 字典的只有 **78 条**（75 条
    精确命中同消息代码，3 条无码）。所以必须先做结构过滤，不能拿 LIKE 数当口径。

    三档策略（按「是否会把别的版本内容套到本条消息」排序）：
      ① 同消息有可靠代码（sysml_versions 精确命中 / 消息正文代码块）→ 完整重投影
      ② 无可靠代码 → **仅无损去重**（复用生成侧 `_dedupe_view`），零语义风险
      ③ 无病灶 → 不动
    明确**禁用**会话级兜底代码：实测 92 条只能拿到「会话最新代码」，用它重投影
    会把 v0.5 的视图套到 v0.1 的消息上（内容看似合理、实则张冠李戴）。
    """
    raw = conn.execute(
        "SELECT id, conversation_id, content, card_data FROM messages "
        "WHERE card_data LIKE '%sysml_views%' ORDER BY id").fetchall()
    # 结构过滤：LIKE 会命中「文本里提到 sysml_views」的 275 条假阳性，
    # 必须确认顶层键是 dict 且 views 非空，才计入待处理集合。
    rows = []
    for r in raw:
        try:
            _d = json.loads(r["card_data"] or "{}")
        except Exception:
            rows.append(r)          # 留给下面的「非法 JSON」分支如实记录
            continue
        _sv = _d.get("sysml_views")
        if isinstance(_sv, dict) and isinstance(_sv.get("views"), dict) and _sv["views"]:
            rows.append(r)
    if args.limit:
        rows = rows[:args.limit]
    before = {"nodes": 0, "uniq": 0, "parents": 0, "dup_views": 0, "moji_views": 0}
    after = dict(before)
    plan, skipped, untouched = [], [], 0
    n_reproj = n_dedupe = 0
    for r in rows:
        try:
            cd = json.loads(r["card_data"] or "{}")
        except Exception:
            skipped.append((r["id"], "card_data 不是合法 JSON"))
            continue
        sv = cd.get("sysml_views")
        if not isinstance(sv, dict) or not isinstance(sv.get("views"), dict) or not sv["views"]:
            skipped.append((r["id"], "无 sysml_views.views"))
            continue
        b = _stats(sv["views"])
        # 只处理确有病灶的：含 BDD/PKG（parent 全空即层级缺失）、含重复 id、含乱码
        has_graph = any(k in ("BDD", "PKG") for k in sv["views"].keys())
        if not (has_graph or b["dup_views"] or b["moji_views"]):
            untouched += 1
            continue
        for k in before:
            before[k] += b[k]

        code, src = _code_for_message(conn, r["conversation_id"], r["id"], r["content"])
        merged = dict(sv["views"])
        res = None
        if code:
            want = sorted(sv["views"].keys())
            try:
                res = generate_views_from_sysml(code, view_types=want, intent=sv.get("intent"))
            except Exception as e:
                skipped.append((r["id"], f"重投影失败: {type(e).__name__}: {e}"))
                for k in after:
                    after[k] += b[k]
                continue
            for k, v in ((res or {}).get("views") or {}).items():
                if k in merged and (v or {}).get("nodes"):
                    merged[k] = v
        else:
            # ② 无可靠代码：只做同 id 去重，绝不引入外部内容
            merged, changed = _dedupe_snapshot(sv["views"], _dedupe_view)
            if not changed:
                skipped.append((r["id"], "无同消息代码且快照已无重复（保留原样）"))
                for k in after:
                    after[k] += b[k]
                continue

        if merged == sv["views"]:
            skipped.append((r["id"], "重投影结果无变化"))
            for k in after:
                after[k] += b[k]
            continue
        a = _stats(merged)
        for k in after:
            after[k] += a[k]
        new_sv = dict(sv)
        new_sv["views"] = merged
        for k, v in (res or {}).items():
            if k != "views" and k not in new_sv:
                new_sv[k] = v
        # 计数只统计**真正进入写入计划**的条目，避免「调用过」被读成「已更新」
        if code:
            n_reproj += 1
        else:
            n_dedupe += 1
        plan.append((r["id"], json.dumps({**cd, "sysml_views": new_sv}, ensure_ascii=False)))

    def line(name, d):
        return (f"{name}: 节点 {d['nodes']}（唯一 {d['uniq']}） 带 parent {d['parents']} "
                f"重复 id 的视图 {d['dup_views']} 乱码视图 {d['moji_views']}")

    print("=" * 78)
    print("[消息 card_data] " + line("处理前", before))
    print("[消息 card_data] " + line("处理后", after))
    print(f"[消息 card_data] 计划更新 {len(plan)}（完整重投影 {n_reproj} / 仅无损去重 {n_dedupe}）"
          f" / 跳过 {len(skipped)} / 无需处理 {untouched} / 共 {len(rows)}")
    if skipped:
        from collections import Counter
        print("  跳过原因：" + "；".join(f"{k}×{v}" for k, v in Counter(x[1] for x in skipped).most_common(6)))
    print(f"  带 parent：{before['parents']} → {after['parents']}   "
          f"重复节点：{before['nodes'] - before['uniq']} → {after['nodes'] - after['uniq']}   "
          f"乱码视图：{before['moji_views']} → {after['moji_views']}")
    print("=" * 78)
    if not args.apply:
        print("（dry-run，未写库；加 --apply 落库）")
        return 0
    if not plan:
        print("无变更。")
        return 0
    bak = f"{DB_PATH}.bak_msgviewreproj_{time.strftime('%Y%m%d_%H%M%S')}"
    shutil.copy2(DB_PATH, bak)
    print(f"已备份：{bak}")
    cur = conn.cursor()
    for mid, new_cd in plan:
        cur.execute("UPDATE messages SET card_data=? WHERE id=?", (new_cd, mid))
    conn.commit()
    print(f"已更新 {len(plan)} 条消息。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
