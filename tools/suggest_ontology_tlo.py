# -*- coding: utf-8 -*-
"""为实体类型**建议并应用** BFO 上层本体锚点（D1=A 配套，2026-11-15）。

## 为什么需要它

`missing_tlo_anchor` 建好之后真库覆盖率是 **5/28** —— 能力在、但几乎没生效，
与"没做"的实际效果差别不大。而锚点不能靠逐个叶子乱设：
**锚点沿 parent 链继承**，所以只需给分类树的**上层节点**设，子孙自动继承。

本脚本干三件事：
  1. 按分类树算出「最小集」= 所有**有效锚点为空的子树的根**（含孤立根）；
  2. 给每个节点算建议锚点 + **依据**（可审计，不是黑箱）；
  3. `--apply` 时按**拓扑序（父先于子）** 走**正规 API** 写入。

## 三条纪律

① **建议表是人工审核过的，不是自动推断**。
   `core.ontology_tlo.suggest_anchor` 的关键词规则只作为"初筛"，
   最终锚点以本文件的 `REVIEW` 表为准（每条带一句范畴判据）。
   为什么：锚点是结构判定，一旦错了会污染整条继承链
   （下游 disjointWith、SHACL 形状全跟着错），必须人能说清理由。

② **走 API 而不是直写 DB**。
   后端 `_resolve_tlo_anchor` 有「未识别拒绝」「仅 entity」「祖先冲突防护」三条校验；
   直写 DB 等于把这套校验绕过 —— 那是把一个判据写两份，必然漂移。

③ **PUT 必须带全量 body**。
   `UPDATE ontology_types SET ...` 是整行替换；只传 tlo_anchor 会把 constraints
   等字段**清空**。本脚本先读整行再改目标字段。

## 前置：悬空 disjoint_with

`利益相关方需求` 的 `constraints.disjoint_with = ['发射装置']`，而「发射装置」已不在
本体里 ⇒ 任何 PUT 都被后端 400 拒绝（`Disjoint With 指向不存在的实体类型`）
⇒ 该类型的编辑入口是**彻底堵死**的。本脚本 `--apply` 时会把这类**悬空项**一并剔除
（只剔除指向不存在类型的项，不动任何现存互斥关系），并在输出里逐条列明。

## 用法

    python tools/suggest_ontology_tlo.py              # 只出建议清单（dry-run）
    python tools/suggest_ontology_tlo.py --apply      # 走 API 写入
    python tools/suggest_ontology_tlo.py --apply --dry-run-api   # 看会发什么，不真发
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

DB = os.path.join(_ROOT, "mbse.db")
API = "http://127.0.0.1:8000"

# ══════════════════════════════════════════════════════════════════════════
# 人工审核表：类型名 → (锚点, 范畴判据)
#
# 判据原则（BFO 轻量子集）：
#   · Object        = 在时间中保持同一性、有边界、可被同一性追踪的东西
#   · Process       = 在时间中展开、有起点终点的事
#   · Quality       = 依附于载体、可测量的特征（本工程的「属性类型」）
#   · InformationContentEntity = 关于某事物的信息载体（需求/规范/用例/模型）
#
# ⚠️ 未列出的类型**不会**被猜 —— 脚本会列进「需人工判定」清单。
# ══════════════════════════════════════════════════════════════════════════
REVIEW = {
    # ── 信息内容实体：需求的本质是"陈述"（ISO/IEC/IEEE 29148 把 requirement
    #    定义为 statement），它不是物理对象，也不在时间中"发生" ──
    "需求":             ("InformationContentEntity", "需求是陈述/信息载体，不是物理对象"),
    "利益相关方需求":      ("InformationContentEntity", "IR 同上：关于期望的信息载体"),
    "系统需求":           ("InformationContentEntity", "SR 同上；**修正此前测试误写的 Object**"),
    "子系统需求":          ("InformationContentEntity", "继承自系统需求；显式声明以免依赖继承"),
    "单元需求":           ("InformationContentEntity", "同上"),
    "验证用例":           ("InformationContentEntity", "测试用例是规格性信息实体（非执行过程本身）"),
    # ── 过程：功能是"能力如何实现"的动态描述，在时间里展开 ──
    "功能":             ("Process", "功能是过程：有输入输出、在时间中展开（已设，保持一致）"),
    # ── 对象：系统与其组成部分、物理设备 ──
    "外部系统":           ("Object", "有边界、可独立存在的系统（本工程视为黑盒对象）"),
    "通信卫星系统":         ("Object", "系统的复合体，仍是可标识、可追踪的对象"),
    "星座":             ("Object", "卫星的集合，作为整体被标识与追踪"),
    "星地链路":           ("Object", "工程上按对象处理：它承载带宽/误码率/传输速率等数据属性"),
    "地面段":            ("Object", "系统的组成部分，物理上可界定"),
    "卫星平台":           ("Object", "继承通信卫星系统 → Object（无需单独设，脚本会跳过）"),
    "系统元素":           ("Object", "部件等的父类（已设）"),
    "部件":             ("Object", "已设"),
    # ── 角色/承载者 ──
    "利益相关方":          ("Object", "人/组织作为独立对象；其「角色」由关系表达（Role 类暂不强制）"),
}


def _load():
    con = sqlite3.connect(DB, timeout=30)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(
        "SELECT id,name,type_kind,parent_id,tlo_anchor,constraints,properties,"
        "description,status,iri,profile_source,profile_ref,replaced_by,deprecated_note "
        "FROM ontology_types")]
    con.close()
    return rows


def _tree(rows):
    ents = [r for r in rows if r["type_kind"] == "entity"]
    by_id = {str(r["id"]): r for r in ents}
    kids = {}
    for r in ents:
        kids.setdefault(str(r["parent_id"]) if r["parent_id"] else "ROOT", []).append(r)
    return ents, by_id, kids


def _subtree_size(nid, kids):
    n = 0
    for c in kids.get(nid, []):
        n += 1 + _subtree_size(str(c["id"]), kids)
    return n


def _dangling(cons, ent_names):
    """返回该类型的悬空 disjoint_with 项（指向不存在的实体类型）。"""
    try:
        c = json.loads(cons or "{}")
    except Exception:
        return []
    if not isinstance(c, dict):
        return []
    return [x for x in (c.get("disjoint_with") or []) if x and x not in ent_names]


def _api_put(tid, body, dry=False):
    if dry:
        return 0, json.dumps(body, ensure_ascii=False)[:200]
    req = urllib.request.Request(
        f"{API}/api/knowledge/ontology/types/{tid}", method="PUT",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode("utf-8")[:300]
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")[:300]
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"


def _full_body(r, ent_names, *, tlo_anchor=None, drop_dangling=False):
    """构造 PUT 全量 body（**必须全量**，否则整行替换会清空未传字段）。"""
    try:
        cons = json.loads(r["constraints"] or "{}")
    except Exception:
        cons = {}
    if not isinstance(cons, dict):
        cons = {}
    if drop_dangling:
        d = _dangling(r["constraints"], ent_names)
        if d:
            cons["disjoint_with"] = [x for x in (cons.get("disjoint_with") or [])
                                     if x not in d]
            if not cons["disjoint_with"]:
                cons.pop("disjoint_with", None)
    try:
        props = json.loads(r["properties"] or "{}")
    except Exception:
        props = {}
    body = {
        "name": r["name"], "type_kind": r["type_kind"],
        "properties": props, "constraints": cons,
        "description": r["description"] or "", "status": r["status"],
        "parent_id": r["parent_id"], "iri": r["iri"] or "",
        "profile_source": r["profile_source"] or "",
        "profile_ref": r["profile_ref"] or "",
        "replaced_by": r["replaced_by"] or "",
        "deprecated_note": r["deprecated_note"] or "",
    }
    if tlo_anchor is not None:
        body["tlo_anchor"] = tlo_anchor
    return body


def run(apply=False, dry_api=False):
    from core import ontology_tlo as T

    rows = _load()
    ents, by_id, kids = _tree(rows)
    ent_names = {r["name"] for r in ents}

    print("=" * 80)
    print("BFO 上层本体锚点 —— 建议清单与应用（D1=A 配套）")
    print("模式：%s" % ("APPLY（走 API 写入）" if (apply and not dry_api)
                    else ("APPLY 预演（只打印请求体）" if apply else "DRY-RUN（只出清单）")))
    print("=" * 80)

    # ── 1. 前置体检：悬空 disjoint_with（会堵死该类型的 PUT）──
    dangling_map = {}
    for r in ents:
        d = _dangling(r["constraints"], ent_names)
        if d:
            dangling_map[r["name"]] = d
    print("\n[前置] 悬空 disjoint_with（会让该类型任何 PUT 被 400 拒绝）：")
    if dangling_map:
        for k, v in dangling_map.items():
            print("   %-16s → %s  （apply 时将剔除悬空项，不动现存互斥关系）" % (k, v))
    else:
        print("   无")

    # ── 2. 算最小集：有效锚点为空的子树的根 ──
    print("\n[清单] 需要设锚点的**上层节点**（子孙自动继承，故不逐个叶子设）：")
    plan = []
    need_review = []

    def walk(nid, inherited_anchor):
        for r in kids.get(nid, []):
            own = T.normalize(r["tlo_anchor"] or "")
            eff = own or inherited_anchor
            if eff:
                walk(str(r["id"]), eff)      # 已有效 ⇒ 子孙跟着继承，不再深入
                continue
            # 有效锚点为空 ⇒ 这棵子树的根，需要处理
            size = _subtree_size(str(r["id"]), kids)
            rec = REVIEW.get(r["name"])
            if rec:
                anchor, why = rec
                plan.append({"row": r, "anchor": anchor, "why": why, "subtree": size})
                print("   %-16s → %-26s 影响(%d 子孙)  依据：%s"
                      % (r["name"], anchor, size, why))
            else:
                sug = T.suggest_anchor(r["name"], "entity", r["description"] or "")
                need_review.append({"row": r, "sug": sug, "subtree": size})
                print("   %-16s → 【需人工判定】影响(%d 子孙)  初筛=%r (%s)"
                      % (r["name"], size, sug.get("anchor") or "无", sug.get("basis")))
            # 无论是否已定，都继续往下找（同一子树里可能还有独立的分支根）
            walk(str(r["id"]), anchor if rec else inherited_anchor)

    walk("ROOT", "")

    if need_review:
        print("\n[需人工判定] %d 个（未在 REVIEW 表内，脚本**不猜**）：" % len(need_review))
        for x in need_review:
            print("   - %s" % x["row"]["name"])

    # ── 3. 修正「已设但与审核表不一致」的锚点（如测试误写的）──
    fixes = []
    for r in ents:
        own = T.normalize(r["tlo_anchor"] or "")
        rec = REVIEW.get(r["name"])
        if own and rec and own != rec[0]:
            fixes.append({"row": r, "old": own, "anchor": rec[0], "why": rec[1]})
    print("\n[修正] 已设但与范畴判据不一致的锚点：")
    if fixes:
        for f in fixes:
            print("   %-16s %s → %s   依据：%s" % (f["row"]["name"], f["old"], f["anchor"], f["why"]))
    else:
        print("   无")

    total_entity = len(ents)
    will_anchor = len(plan)
    print("\n[规模] 实体 %d 个；本轮需写入 %d 条（+ 修正 %d 条）"
          % (total_entity, will_anchor, len(fixes)))

    if not apply:
        print("\n结果：DRY-RUN（未写库）。要写入请加 --apply")
        return 0

    # ── 4. 应用：按拓扑序（父先于子）──
    print("\n" + "-" * 80)
    print("应用（父先于子，全部走 API 的全量 body）：")

    def depth(r):
        d, cur, g = 0, r["parent_id"], 0
        while cur and g < 30:
            d += 1
            p = by_id.get(str(cur))
            if not p:
                break
            cur = p["parent_id"]
            g += 1
        return d

    # 修正项优先（它们通常是父子链上的关键节点），再按深度升序
    ordered = sorted(fixes, key=lambda x: depth(x["row"])) + \
              sorted(plan, key=lambda x: depth(x["row"]))
    ok = fail = 0
    for item in ordered:
        r = item["row"]
        need_drop = r["name"] in dangling_map
        body = _full_body(r, ent_names, tlo_anchor=item["anchor"], drop_dangling=need_drop)
        code, resp = _api_put(r["id"], body, dry=dry_api)
        tag = "OK  " if code == 200 else "FAIL"
        if code == 200:
            ok += 1
        else:
            fail += 1
        extra = ""
        if need_drop:
            extra = " (同时剔除悬空 disjoint: %s)" % dangling_map[r["name"]]
        print("   [%s] %-16s → %-26s http=%s%s"
              % (tag, r["name"], item["anchor"], code, extra))
        if code != 200:
            print("          %s" % resp[:260])

    print("\n结果：写入成功 %d 条，失败 %d 条" % (ok, fail))
    if fail:
        print("⚠️ 有失败项 —— 逐条看上面 http 码与后端报错，不要当成已生效")
    return 0 if fail == 0 else 1


def main():
    ap = argparse.ArgumentParser(description="BFO 上层本体锚点建议与应用")
    ap.add_argument("--apply", action="store_true", help="走 API 写入")
    ap.add_argument("--dry-run-api", action="store_true", help="配合 --apply：只打印不真发")
    a = ap.parse_args()
    return run(apply=a.apply, dry_api=a.dry_run_api)


if __name__ == "__main__":
    sys.exit(main())
