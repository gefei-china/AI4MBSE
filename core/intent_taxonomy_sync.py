"""意图字典自举（derivation）—— 把「词表填空」从人工逐条补，改成**从数据派生 + 报告缺口**。

## 为什么要有这个模块（2026-11-07 第 19 轮遗留项）
22 条意图字典里 13 条 `aliases` / `examples` 为空。手工补写中文示例属于**硬编码知识**：
  · aliases 与 `agents.intent_keywords` 是同一件事的两份副本 ⇒ 必然漂移；
  · examples 被 `agent/intent.py::fewshot_map` 直接喂 LLM few-shot ⇒ 编造示例会污染识别。
所以这里**只做派生**，两个来源都是库里已有的真实数据：

| 字段 | 派生来源 | 为什么是它 |
|---|---|---|
| `aliases` | `agents.intent_keywords`（按 `agent_name` 关联） | 那就是"用户会怎么称呼这个能力"的词表，已经在维护了，不该有第二份 |
| `examples` | `intent_samples` 中 `status='confirmed'` 的真实说法 | 人工确认过的原话，零编造；不足 `min_confirmed` 条就**不填** |

派生不出来的**如实报缺口**（`gaps` / `drift`），留给人或后续流量去补 —— **不编造**。

## 三条纪律（违反任一条都会造成静默污染）
1. **人工维护永不被覆盖**：只有「当前值为空且 `*_src` 为空」或「`*_src` 就是本来源」的行才写。
   人工在页面上显式改过一项时，`intent_taxonomy.upsert` 会把 `*_src` 清空 ⇒ 双向判据只在写入侧一处。
2. **幂等**：同一份数据连跑两次，第二次 `changes` 必为空。
3. **默认 dry-run**：`apply()` 必须由调用方显式触发，且每次写库都返回明细供审计。

## 为什么**不**反向填 `agents.intent_keywords`
反向（aliases → keywords）看似对称，但 `intent_keywords` 参与关键词层**打分与触发**
（`IntentRouter._kw_layers`），把"归一化用的别名"塞进"触发词"会改变识别结果 ——
那是行为变更，不是数据补齐。故反向只进 `gaps` 报告，由人决定。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from typing import Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# CLI 直跑（`python core/intent_taxonomy_sync.py`）时 sys.path[0] 是 core/，导入不到包
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

#: 派生来源标识（写进 `intent_taxonomy.aliases_src` / `examples_src`）。
SRC_AGENT_KW = "agents.intent_keywords"
SRC_SAMPLES = "intent_samples.confirmed"

#: 缺省派生参数。示例数 5 = 与行业 few-shot 惯例一致（3~5 条）；
#: `min_confirmed=2` 防止「只有 1 条样本」时把孤例固化成字典内容。
DEF_MAX_EXAMPLES = 5
DEF_MIN_CONFIRMED = 2
DEF_MAX_ALIASES = 12
_EX_TEXT_MIN, _EX_TEXT_MAX = 4, 200
_EX_ALIAS_MIN, _EX_ALIAS_MAX = 2, 20


def _loads(v) -> list:
    try:
        x = json.loads(v or "[]")
        return x if isinstance(x, list) else []
    except Exception:
        return []


def _clean(items, *, lo: int, hi: int, drop: set, cap: int) -> list:
    """去空白/超长/与被派生对象重名/重复，保序截断。

    `drop` 里放「与 canonical 名、label 完全相同」的项 —— 它们作为别名没有信息量
    （归一化时 `name` 已经是精确命中，轮不到别名）。
    """
    out, seen = [], set()
    for it in items or []:
        s = str(it or "").strip()
        if not s or len(s) < lo or len(s) > hi or s in drop or s in seen:
            continue
        seen.add(s)
        out.append(s)
        if len(out) >= cap:
            break
    return out


def _confirmed_by_intent(conn) -> dict:
    """`{intent: [text, ...]}` —— 只取人工确认过的（自动采集的 suggested 不能当字典来源）。"""
    out: dict = {}
    try:
        rows = conn.execute(
            "SELECT intent, text, seen_count, id FROM intent_samples "
            "WHERE status='confirmed' AND intent<>'' AND text<>'' "
            "ORDER BY intent, seen_count DESC, id ASC").fetchall()
    except Exception:
        return out
    for r in rows:
        try:
            intent, text = r[0], r[1]
        except Exception:
            continue
        out.setdefault(intent, []).append(text)
    return out


def _agent_keywords(conn) -> dict:
    """`{agent_name: [keyword, ...]}`。表不存在/列不存在时返回空（不猜）。"""
    out: dict = {}
    try:
        rows = conn.execute(
            "SELECT name, intent_keywords FROM agents WHERE status='active'").fetchall()
    except Exception:
        return out
    for r in rows:
        out[str(r[0])] = _loads(r[1])
    return out


def plan(conn, *, max_examples: int = DEF_MAX_EXAMPLES,
         min_confirmed: int = DEF_MIN_CONFIRMED,
         max_aliases: int = DEF_MAX_ALIASES) -> dict:
    """计算"该补什么"，**不写库**。返回 changes / gaps / drift / stats。"""
    from core import intent_taxonomy as itx
    itx.ensure_table(conn)

    kw = _agent_keywords(conn)
    conf = _confirmed_by_intent(conn)

    rows = conn.execute(
        "SELECT id, name, label, aliases, examples, aliases_src, examples_src, "
        "agent_name, status FROM intent_taxonomy ORDER BY id").fetchall()
    cols = ["id", "name", "label", "aliases", "examples", "aliases_src",
            "examples_src", "agent_name", "status"]
    # ⚠️ 不能假设调用方设了 row_factory（本模块被 CLI/门禁直接调用）
    recs = [dict(r) if hasattr(r, "keys") else dict(zip(cols, r)) for r in rows]

    changes, gaps = [], []
    for r in recs:
        name = r["name"]
        cur_al = _loads(r["aliases"])
        cur_ex = _loads(r["examples"])
        al_src = (r["aliases_src"] or "")
        ex_src = (r["examples_src"] or "")
        drop = {name, (r["label"] or "").strip()}

        # ── aliases ← agents.intent_keywords ──
        new_al = None
        owner_al = bool(cur_al) and not al_src          # 人工维护（含早期人工种子）
        if not owner_al and al_src in ("", SRC_AGENT_KW):
            src = kw.get((r["agent_name"] or "").strip()) or []
            if src:
                cand = _clean(src, lo=_EX_ALIAS_MIN, hi=_EX_ALIAS_MAX,
                              drop=drop, cap=max_aliases)
                if cand and cand != cur_al:
                    new_al = cand

        # ── examples ← confirmed 样本 ──
        new_ex = None
        owner_ex = bool(cur_ex) and not ex_src
        if not owner_ex and ex_src in ("", SRC_SAMPLES):
            src = conf.get(name) or []
            # 长度过滤后再判"够不够" —— 否则 2 条里 1 条是系统包装文本时仍会填
            usable = _clean(src, lo=_EX_TEXT_MIN, hi=_EX_TEXT_MAX, drop=drop,
                            cap=max_examples)
            if len(usable) >= min_confirmed and usable != cur_ex:
                new_ex = usable

        if new_al is not None or new_ex is not None:
            changes.append({
                "id": r["id"], "name": name,
                "aliases": new_al, "examples": new_ex,
                "aliases_src": SRC_AGENT_KW if new_al is not None else None,
                "examples_src": SRC_SAMPLES if new_ex is not None else None,
                "before": {"aliases": cur_al, "examples": cur_ex},
                "why": "aliases←关键词" if new_al is not None else "",
                })
            if new_ex is not None:
                changes[-1]["why"] = ",".join(filter(None, [
                    changes[-1]["why"], "examples←confirmed"]))

        # ── 缺口（应用后仍为空的那一侧）──
        final_al = new_al if new_al is not None else cur_al
        final_ex = new_ex if new_ex is not None else cur_ex
        miss = []
        if not final_al:
            if not (r["agent_name"] or "").strip():
                miss.append("aliases:无绑定 Agent")
            elif not kw.get(r["agent_name"].strip()):
                miss.append("aliases:Agent 未配 intent_keywords")
            else:
                miss.append("aliases:关键词全部被过滤（过短/重名/重复）")
        if not final_ex:
            n = len(conf.get(name) or [])
            if n == 0:
                miss.append("examples:无 confirmed 样本")
            elif n < min_confirmed:
                miss.append("examples:confirmed 仅 %d 条（<门槛 %d）" % (n, min_confirmed))
            else:
                miss.append("examples:样本全部被过滤（过短/过长/重复）")
        if miss:
            gaps.append({"id": r["id"], "name": name, "label": r["label"],
                         "agent_name": r["agent_name"], "missing": miss})

    # ── 配置漂移：两边对不上 ──
    tax_names = {(r["agent_name"] or "").strip() for r in recs if (r["agent_name"] or "").strip()}
    drift = []
    for agent in sorted(kw):
        if agent not in tax_names:
            drift.append({"kind": "agent_without_taxonomy", "agent": agent,
                          "hint": "该 Agent 可被路由，但意图字典里没有对应条 ⇒ 下拉选不到、无法维护示例"})
    try:
        disabled = {r[0] for r in conn.execute(
            "SELECT name FROM agents WHERE status<>'active'").fetchall()}
    except Exception:
        disabled = set()
    for r in recs:
        a = (r["agent_name"] or "").strip()
        if a and a in disabled:
            drift.append({"kind": "taxonomy_points_to_disabled_agent",
                          "name": r["name"], "agent": a,
                          "hint": "字典指向已停用的 Agent（不影响归一化，但示例不会随关键词更新）"})

    return {
        "changes": changes, "gaps": gaps, "drift": drift,
        "stats": {
            "taxonomy_total": len(recs),
            "changes": len(changes),
            "gap_rows": len(gaps),
            "drift_rows": len(drift),
            "alias_filled_after": sum(
                1 for r in recs
                if (r["aliases"] and _loads(r["aliases"])) or
                any(c["id"] == r["id"] and c["aliases"] for c in changes)),
            "example_filled_after": sum(
                1 for r in recs
                if _loads(r["examples"]) or
                any(c["id"] == r["id"] and c["examples"] for c in changes)),
            "min_confirmed": min_confirmed,
            "max_examples": max_examples,
        },
    }


def apply(conn, p: Optional[dict] = None, **kw) -> dict:
    """把 plan 写入库。返回 `{ok, changed, skipped}`。"""
    p = p if p is not None else plan(conn, **kw)
    changed = 0
    for c in p.get("changes") or []:
        sets, args = ["updated_at=CURRENT_TIMESTAMP"], []
        if c.get("aliases") is not None:
            sets += ["aliases=?", "aliases_src=?"]
            args += [json.dumps(c["aliases"], ensure_ascii=False), c["aliases_src"] or ""]
        if c.get("examples") is not None:
            sets += ["examples=?", "examples_src=?"]
            args += [json.dumps(c["examples"], ensure_ascii=False), c["examples_src"] or ""]
        cur = conn.execute("UPDATE intent_taxonomy SET " + ", ".join(sets) +
                           " WHERE id=?", args + [c["id"]])
        changed += int(cur.rowcount or 0)
    conn.commit()
    return {"ok": True, "changed": changed, "planned": len(p.get("changes") or []),
            "gaps": len(p.get("gaps") or []), "drift": len(p.get("drift") or [])}


def coverage(conn) -> dict:
    """覆盖度快照（供门禁与页面）：不给方案，只报现状。"""
    p = plan(conn)
    s = dict(p["stats"])
    s["alias_coverage"] = round(s["alias_filled_after"] / max(1, s["taxonomy_total"]), 4)
    s["example_coverage"] = round(s["example_filled_after"] / max(1, s["taxonomy_total"]), 4)
    return {"stats": s, "gaps": p["gaps"], "drift": p["drift"],
            "pending_changes": len(p["changes"])}


def report(conn, *, show_cap: int = 40) -> str:
    """人读报告（CLI / 日志）。"""
    p = plan(conn)
    s = p["stats"]
    lines = [
        "意图字典覆盖：%d 条；可补 %d 条；缺口 %d 条；漂移 %d 条；"
        "别名已填 %d / 示例已填 %d"
        % (s["taxonomy_total"], s["changes"], s["gap_rows"], s["drift_rows"],
           s["alias_filled_after"], s["example_filled_after"]),
    ]
    for g in (p["gaps"] or [])[:show_cap]:
        lines.append("  缺口 %-26s %s" % (g["name"], " | ".join(g["missing"])))
    for d in (p["drift"] or [])[:show_cap]:
        lines.append("  漂移 %-30s %s" % (d["kind"], d.get("agent") or d.get("name")))
    return "\n".join(lines)


def main(argv: list) -> int:
    db = os.path.join(ROOT, "mbse.db")
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        if "--apply" in argv:
            p = plan(conn)
            r = apply(conn, p)
            print("已写入：plan=%d changed=%d gaps=%d drift=%d"
                  % (r["planned"], r["changed"], r["gaps"], r["drift"]))
            for c in (p["changes"] or [])[:40]:
                print("  + %-26s %s" % (c["name"], c["why"]))
        print(report(conn))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
