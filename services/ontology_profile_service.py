"""本体画像派生（ontology_profiles）—— 把「当前类型集」投影成一份可复用的方法论画像。

## 为什么需要（第 19 轮遗留项）
`ontology_profiles` 实测 **0 行**，而 `methodology_profile_tools._load_ontology_layer()`
的 B 层（"这个工程能用什么"）正是读它 —— 于是 M0 方法论规约卡永远只有 A 层（文档）与
兜底常量，B 层恒空。同时 `ontology_types` 里有 **52 条**真实类型（28 实体 / 13 关系 / 11 属性），
数据一直在，只是**没有投影成画像**。

## 为什么是"派生"而不是"补种子"
`database/seeds.py` 那两条 profile 是给星网模板写的示范（`profile-satnet-kb` 等），
在"项目非空就不播种"的守卫下永远跑不到。**手工再写一份画像 = 硬编码知识**：
类型改了画像不跟着变，两份数据必然漂移。
所以这里只做投影：`ontology_types` 是唯一事实源，画像每次可重算、可对账。

## 三条纪律
1. **不覆盖人工维护的画像**：只更新 `id` 以 `profile-derived-` 开头的行。
   同名但非派生行 ⇒ 直接报错退出（宁可让人改名，也不静默覆盖）。
2. **幂等**：画像内容不变时返回 `changed=False` 且不写库（描述里也不带时间戳，
   否则每次跑都"有变化"，幂等断言就成了摆设）。
3. **默认 dry-run**：`apply()` 必须显式触发；`link_project` 更要显式（它会改 projects 表）。

## 取值口径（写进 `coverage` 报告，避免"数字说不清"）
- 只取 `status NOT IN ('deprecated','obsolete')` 的类型（当前真库 52 条全为 `released`）；
- `entities[].props` ← `attribute` 类型 `constraints.domain_classes` 里包含该实体的属性名；
- `relations[].src/tgt` ← `constraints.allowed_values.src/tgt`；两者皆空 ⇒ `open=True`
  （表示"该关系未声明端点约束"，不是"约束为空集"——这两件事语义完全不同）。
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from typing import Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

#: 派生行 id 前缀 —— 判断"这行归我管"的唯一判据（改名不管用，也防人手误改 id 后被我覆盖）。
ID_PREFIX = "profile-derived-"

#: 默认画像名。刻意与 seeds 里的示范名（`星网知识库本体`）区分开。
DEF_NAME = "当前本体快照"

_SKIP_STATUS = ("deprecated", "obsolete")


def _j(v, default):
    try:
        x = json.loads(v or "")
        return x if isinstance(x, type(default)) else default
    except Exception:
        return default


def build(conn) -> dict:
    """从 `ontology_types` 投影出画像载荷（纯读）。返回 `{entities, relations, attributes, stats}`。

    ⚠️ 两遍扫描是必须的：`SELECT ... ORDER BY name` 下，属性名（如 `EIRP`）排在实体名
    （如 `转发器`）之前很常见。若边扫边往 dict 里塞，`domain_classes` 指向一个"还没读到的实体"
    就会被静默丢掉 —— 本函数第一版正是如此（`if dc in ent` 过滤），实测会漏掉大部分 props。
    """
    ph = ",".join("?" * len(_SKIP_STATUS))
    rows = conn.execute(
        "SELECT name, type_kind, constraints FROM ontology_types "
        "WHERE COALESCE(status,'') NOT IN (%s) ORDER BY name" % ph, _SKIP_STATUS).fetchall()

    ent_names, att, dom_pairs, rel = [], [], [], []
    for r in rows:
        try:
            name, kind, cons = r[0], r[1], _j(r[2], {})
        except Exception:
            continue
        if not name:
            continue
        if kind == "entity":
            ent_names.append(name)
        elif kind == "attribute":
            att.append(name)
            for dc in (cons.get("domain_classes") or []):
                dom_pairs.append((dc, name))
        elif kind == "relation":
            av = cons.get("allowed_values") or {}
            src = list(av.get("src") or [])
            tgt = list(av.get("tgt") or [])
            rel.append({"type": name, "src": src, "tgt": tgt, "open": not (src or tgt)})

    ents = {n: [] for n in sorted(set(ent_names))}
    dangling = set()
    for dc, a in dom_pairs:
        # 指向未注册实体的属性域 ⇒ 记进 dangling（这类悬空名正是历史上 dom/range 事故的形态），
        # 但**不凭空造实体**：画像只描述已注册的类型。
        if dc in ents:
            ents[dc].append(a)
        else:
            dangling.add(dc)

    entities = [{"type": k, "props": sorted(set(v))} for k, v in sorted(ents.items())]
    relations = sorted(rel, key=lambda x: x["type"])
    return {"entities": entities, "relations": relations,
            "attributes": sorted(set(att)),
            "stats": {"entities": len(entities), "relations": len(relations),
                      "attributes": len(set(att)), "dangling_domains": len(dangling)}}


def _stable_id(name: str) -> str:
    return ID_PREFIX + hashlib.sha1(name.encode("utf-8")).hexdigest()[:12]


def plan(conn, *, name: str = DEF_NAME, profile_type: str = "domain",
         description: str = "", scenario_template_id: Optional[str] = None) -> dict:
    """算出"要不要写/写什么"，**不写库**。"""
    payload = build(conn)
    if not payload["stats"]["entities"] and not payload["stats"]["relations"]:
        return {"ok": False, "reason": "empty_ontology",
                "message": "ontology_types 里没有可用类型，派生出的画像为空 —— 不写空画像（会让人误以为已建好）",
                "payload": payload}
    desc = description or (
        "派生自 ontology_types（排除 deprecated）：实体 %d / 关系 %d / 属性 %d；"
        "属性域悬空 %d 个。重算即可同步；勿手工编辑本行（id 前缀 %s 即代表机器维护）"
        % (payload["stats"]["entities"], payload["stats"]["relations"],
           payload["stats"]["attributes"], payload["stats"]["dangling_domains"], ID_PREFIX))
    want = {
        "id": _stable_id(name), "name": name, "profile_type": profile_type,
        "scenario_template_id": scenario_template_id or None,
        "entities": json.dumps(payload["entities"], ensure_ascii=False, sort_keys=True),
        "relations": json.dumps(payload["relations"], ensure_ascii=False, sort_keys=True),
        "attributes": json.dumps(payload["attributes"], ensure_ascii=False, sort_keys=True),
        "description": desc, "status": "active",
    }
    # 显式列名 + 按下标取，不依赖 row_factory（本模块被 CLI / 门禁在裸连接上调用）
    _COLS = ("id", "name", "profile_type", "scenario_template_id", "entities",
             "relations", "attributes", "description", "status")
    row = conn.execute(
        "SELECT %s FROM ontology_profiles WHERE name=?" % ",".join(_COLS), (name,)).fetchone()
    if row is None:
        return {"ok": True, "action": "create", "row": want, "payload": payload}
    cur = dict(zip(_COLS, row))
    if not str(cur.get("id") or "").startswith(ID_PREFIX):
        return {"ok": False, "reason": "name_taken_by_human_profile",
                "message": "同名画像 %r 不是派生行（id=%s），拒绝覆盖。请换名或先人工处理。"
                           % (name, cur.get("id")),
                "payload": payload}
    diff = [k for k in ("entities", "relations", "attributes", "description",
                        "profile_type", "scenario_template_id", "status")
            if (cur.get(k) or None) != (want[k] or None)]
    if not diff:
        return {"ok": True, "action": "noop", "row": want, "diff": [], "payload": payload}
    return {"ok": True, "action": "update", "row": want, "diff": diff, "payload": payload}


def apply(conn, p: Optional[dict] = None, **kw) -> dict:
    """落库。`create` 用 INSERT、`update` 只改差异涉及到的列（避免把人工补的 description 冲掉）。"""
    p = p if p is not None else plan(conn, **kw)
    if not p.get("ok") or p.get("action") in (None, "noop"):
        return {"ok": bool(p.get("ok")), "action": p.get("action") or "error",
                "reason": p.get("reason"), "message": p.get("message"), "profile_id": None}
    r = p["row"]
    if p["action"] == "create":
        conn.execute(
            "INSERT INTO ontology_profiles (id,name,profile_type,scenario_template_id,"
            "entities,relations,attributes,description,status) VALUES (?,?,?,?,?,?,?,?,?)",
            (r["id"], r["name"], r["profile_type"], r["scenario_template_id"],
             r["entities"], r["relations"], r["attributes"], r["description"], r["status"]))
    else:
        cols = p.get("diff") or ["entities", "relations", "attributes", "description"]
        conn.execute("UPDATE ontology_profiles SET " + ", ".join("%s=?" % c for c in cols)
                     + " WHERE id=?", [r[c] for c in cols] + [r["id"]])
    conn.commit()
    return {"ok": True, "action": p["action"], "profile_id": r["id"], "diff": p.get("diff") or [],
            "stats": p["payload"]["stats"]}


def plan_link(conn, profile_id: str, project_id: Optional[str] = None) -> dict:
    """把画像挂到项目（仅当项目当前 `ontology_profile_id` 为空时）。默认不动。"""
    if project_id:
        rows = conn.execute("SELECT id,name,ontology_profile_id FROM projects WHERE id=?",
                            (project_id,)).fetchall()
    else:
        rows = conn.execute("SELECT id,name,ontology_profile_id FROM projects").fetchall()
    out = []
    for r in rows:
        cur = r[2] if hasattr(r, "__getitem__") else None
        out.append({"project": r[1], "id": r[0], "current": cur,
                    "will_set": (not cur), "profile_id": profile_id})
    return {"ok": True, "items": out,
            "pending": sum(1 for x in out if x["will_set"])}


def apply_link(conn, pl: dict) -> dict:
    n = 0
    for it in pl.get("items") or []:
        if it.get("will_set"):
            n += conn.execute(
                "UPDATE projects SET ontology_profile_id=?, updated_at=CURRENT_TIMESTAMP "
                "WHERE id=? AND COALESCE(ontology_profile_id,'')=''",
                (it["profile_id"], it["id"])).rowcount
    conn.commit()
    return {"ok": True, "linked": n}


def main(argv: list) -> int:
    import sqlite3
    conn = sqlite3.connect(os.path.join(ROOT, "mbse.db"))
    conn.row_factory = sqlite3.Row
    try:
        p = plan(conn)
        if not p.get("ok"):
            print("无法派生：%s" % p.get("message") or p.get("reason"))
            return 2
        print("画像计划：action=%s 实体 %d / 关系 %d / 属性 %d"
              % (p["action"], p["payload"]["stats"]["entities"],
                 p["payload"]["stats"]["relations"], p["payload"]["stats"]["attributes"]))
        if p.get("diff"):
            print("  将变更列：%s" % ", ".join(p["diff"]))
        if "--apply" in argv:
            r = apply(conn, p)
            print("已写入：%s profile_id=%s" % (r["action"], r["profile_id"]))
        else:
            print("[预演] 未写库。加 --apply 执行。")
        if "--link-project" in argv:
            pl = plan_link(conn, p["row"]["id"])
            print("项目挂接：待挂 %d / 共 %d" % (pl["pending"], len(pl["items"])))
            for it in pl["items"]:
                print("  %-16s current=%s → %s" % (it["project"], it["current"] or "(空)",
                                                    it["profile_id"] if it["will_set"] else "(不改)"))
            if "--apply" in argv:
                print("已挂接 %d 个项目" % apply_link(conn, pl)["linked"])
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
