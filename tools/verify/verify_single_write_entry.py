#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""门禁：写入口唯一性（P0-0，2026-11-21 新增）。

## 要挡住的是什么

同一件事「写一个实体」在本系统有 20 个 HTTP 端点各写一遍。每个端点自己决定
要不要校验类型、要不要过本体约束、要不要留痕、写完要不要回读核验 ——
Palantir 的答案是反过来的：**默认只允许通过 Action 编辑**，其他写入方式明确不推荐用于新用途。

实测（改造前）：
  · `POST /api/knowledge/entities` 只查"类型是否存在"，**不校验属性**
    （而同项目的 `PUT /api/knowledge/graph/nodes/{nid}` 调 `validate_node`）⇒ 两把尺子
  · 执行后核验（P0-3）无挂点 —— 只能挂在 Agent 工具链上，前端直调全部绕过

## 判据（每条都能判红，见末尾变异自证）

  G1  统一入口模块存在，且**五件事都在里面**：类型门禁 / 属性校验 / 写入 / 留痕 / 执行后核验
  G2  已迁移端点**不得**再出现重复实现（`validate_node`、`repo.create_entity`、
      `repo.update_entity`、`_log_graph_edit`）—— 出现即说明又长出第二把尺子
  G3  已迁移端点必须**真的调用** `write_entity`（防"声明迁移、实际没接"）
  G4  待迁移端点数 ≤ 预算（**只降不升**；新增写端点必须先迁移或显式抬预算）
      ⚠️ 口径边界：G4 只统计 WRITE_FILES 这 4 个文件的**端点**（2026-11-22 由 3 扩到 4：
      `graph_query.py` 原就在文件系统里、只是没被统计，扩面后待迁移同口径 +3）。全仓对
      entities/relations 的直接 SQL 写由 `verify_write_surface.py` 全量登记。
      **不要**把 G4 的「待迁移 12」当成写入口覆盖率。
  G5  `post_check` 的核验语义**非空且会失败** —— 用临时库跑两轮：
      数据一致 ⇒ verified=True；属性被篡改 ⇒ verified=False。
      这条是防"核验函数恒返回 True"这种假核验。

## 为什么 G5 要真跑

"有核验函数"和"核验会失败"是两件事。上一轮我就在这上面栽过：判据只数
`post_condition` 关键字出现次数，函数恒返回 True 也照样绿。
"""
import os
import re
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
GATE = os.path.join(ROOT, "services", "graph_write_gate.py")
PARTS = os.path.join(ROOT, "routers", "knowledge_parts")

#: 参与统计的写端点文件（相对仓库根；首批范围；扩范围时同步抬/降预算）
#: ⚠️ 注意路径不在同一目录：`graph_workspace.py` 在 `routers/`，另两个在 `routers/knowledge_parts/`
#:    —— 上一版就按同一目录拼路径，结果它“不存在”，G2 直接判红（门禁自己抓到的）。
WRITE_FILES = (
    "routers/knowledge_parts/entities.py",
    "routers/knowledge_parts/graph.py",
    "routers/knowledge_parts/graph_query.py",   # 2026-11-22 扩面：PUT edges/{eid} 在此文件
    "routers/graph_workspace.py",
)

#: 待迁移端点预算（**只降不升**）。
#: 2026-11-21 首批 20 个写端点 → 迁移 3 个 ⇒ 17
#: 2026-11-21 第二批 → 再迁移 4 个（graph nodes create/delete、graph edges create/delete）⇒ 13
#: 2026-11-22 第五批 → 再迁移 1 个（PUT graph/edges/{eid}，走 write_edge op=edge_update）。
#:   ⚠️ 该端点本就在 G4 统计面**外**（graph_query.py），所以 G4 分子不降——覆盖率进展由
#:      `verify_write_surface.py` 的台账体现，不要误读为"迁移没生效"。
#: 2026-11-22 同轮扩统计面 3→4 文件（纳入 graph_query.py）：4 个写端点中 1 个已迁移、
#:   3 个原本就在但未统计 ⇒ 13+3=16。**是同口径还原，不是抬预算放宽**：
#:   判断"只降不升"必须按同一文件集合比，否则扩面会造成假红、逼人误抬预算。
BUDGET_PENDING = 16

#: 已迁移端点必须出现的接线函数。
#: ⚠️ 2026-11-21：第二批迁移了 4 个**边**端点，它们走 `write_edge`（关系生命周期），
#:   不走 `write_entity`（实体生命周期）—— 这是刻意的分离：两者的表、核验语义、
#:   校验入口都不同（`entity_versions` vs `relations`；`validate_node` vs `validate_edge`）。
#:   判据按操作类型分别认，**不是放宽**：任一端点仍必须命中其中一个，且不能两个都没有。
WIRING_FUNCS = ("write_entity", "write_edge")

#: 已迁移端点不得出现的重复实现（正则）。带左括号 ⇒ 只认**真实调用**，
#: 注释里提到这些名字不算（本项目大量注释会引用被删掉的老实现作为考古记录）。
DUP_PATTERNS = (
    ("validate_node", r"validate_node\s*\("),
    ("repo.create_entity", r"repo\.create_entity\s*\("),
    ("repo.update_entity", r"repo\.update_entity\s*\("),
    ("_log_graph_edit", r"_log_graph_edit\s*\("),
    ("GraphStore(", r"GraphStore\s*\(\s*conn\s*\)\s*\.\s*(create_node|create_edge)"),
)

_results = []
PASS, FAIL = "PASS", "FAIL"


def _add(ok, name, detail=""):
    _results.append((PASS if ok else FAIL, name, "" if ok else str(detail)[:300]))


def _read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def _endpoints(text):
    """从装饰器里抽出 "METHOD /path"。"""
    out = []
    for m in re.finditer(r'@router\.(get|post|put|delete|patch)\(\s*"([^"]+)"', text):
        out.append((m.group(1).upper(), m.group(2), m.start()))
    return out


def _segment(text, start):
    """取某个装饰器之后、下一个顶层装饰器之前的函数体。"""
    nxt = len(text)
    m = re.compile(r'^@router\.', re.M).search(text, start + 1)
    if m:
        nxt = m.start()
    return text[start:nxt]


def check_g1():
    if not os.path.isfile(GATE):
        _add(False, "G1 统一入口模块存在", GATE)
        return
    src = _read(GATE)
    need = {
        "类型门禁": "def check_entity_type",
        "属性校验": "def validate_props",
        "执行后核验": "def post_check",
        "写入": "KnowledgeRepo",
        "留痕(图谱日志)": "graph_edit_logs",
        "留痕(审计)": "core.audit",
    }
    miss = [k for k, v in need.items() if v not in src]
    _add(not miss, "G1 统一入口五件事齐备", "缺: " + ", ".join(miss) if miss else "")


def _migrated():
    from core.graph_write_gate import MIGRATED_ENDPOINTS
    out = set()
    for ep in MIGRATED_ENDPOINTS:
        meth, _, path = ep.partition(" ")
        out.add((meth.upper(), path))
    return out


def check_g2_g3():
    mig = _migrated()
    found, dup_hits, wired = set(), [], []
    for rel in WRITE_FILES:
        p = os.path.join(ROOT, rel)
        if not os.path.isfile(p):
            _add(False, f"G2 端点文件存在: {rel}", p)
            continue
        text = _read(p)
        for meth, path, pos in _endpoints(text):
            if (meth, path) not in mig:
                continue
            found.add((meth, path))
            body = _segment(text, pos)
            for label, pat in DUP_PATTERNS:
                if re.search(pat, body):
                    dup_hits.append(f"{meth} {path} → {label}")
            if any(f in body for f in WIRING_FUNCS):
                wired.append(f"{meth} {path}")
    _add(not dup_hits, "G2 已迁移端点无重复实现",
         "仍在自己写: " + "; ".join(dup_hits[:6]) if dup_hits else "")
    missing = mig - found
    _add(not missing, "G2b 声明的已迁移端点真实存在",
         "声明了但找不到: " + str(sorted(missing)) if missing else "")
    _add(len(wired) == len(mig), "G3 已迁移端点都接到统一入口（%s）" % "/".join(WIRING_FUNCS),
         f"仅 {len(wired)}/{len(mig)} 接线: 缺 " + str(sorted(set(f'{a} {b}' for a, b in mig) - set(wired))))


def check_g4():
    mig = _migrated()
    total, pending = 0, []
    for rel in WRITE_FILES:
        p = os.path.join(ROOT, rel)
        if not os.path.isfile(p):
            continue
        for meth, path, _ in _endpoints(_read(p)):
            if meth == "GET":
                continue
            total += 1
            if (meth, path) not in mig:
                pending.append(f"{os.path.basename(rel)}:{meth} {path}")
    _add(len(pending) <= BUDGET_PENDING,
         f"G4 待迁移端点 ≤ 预算（{len(pending)}/{BUDGET_PENDING}；"
         "仅 %d 文件口径，全仓写入面见 verify_write_surface.py）" % len(WRITE_FILES),
         "超预算，新增写端点请先迁移或显式抬预算:\n      " + "\n      ".join(pending))


def _tmp_db(rows):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE entity_versions (id TEXT, branch TEXT, version_no INT, "
              "is_current INT, name TEXT, entity_type TEXT, properties TEXT)")
    c.execute("CREATE TABLE entities (id TEXT, branch TEXT)")
    c.execute("CREATE TABLE relations (id INTEGER, source_id TEXT, target_id TEXT, "
              "relation_type TEXT, branch TEXT)")
    for r in rows:
        c.execute("INSERT INTO entity_versions VALUES (?,?,?,?,?,?,?)", r)
        c.execute("INSERT INTO entities VALUES (?,?)", (r[0], r[1]))
    return c


def check_g5():
    try:
        from core.graph_write_gate import post_check, post_check_deleted, post_check_edge
    except Exception as e:
        _add(False, "G5 执行后核验可导入", e)
        return
    props = {"req_id": "R-1", "priority": "高"}
    good = _tmp_db([("E1", "dev", 1, 1, "需求A", "单元需求",
                     '{"req_id": "R-1", "priority": "高"}')])
    bad = _tmp_db([("E1", "dev", 1, 1, "需求A", "单元需求",
                    '{"req_id": "R-1", "priority": "低"}')])
    miss = _tmp_db([])
    r_good = post_check(good, "E1", "dev", "需求A", "单元需求", props, "update")
    r_bad = post_check(bad, "E1", "dev", "需求A", "单元需求", props, "update")
    r_miss = post_check(miss, "E1", "dev", "需求A", "单元需求", props, "update")
    ok = (r_good["verified"] is True and r_bad["verified"] is False
          and r_miss["verified"] is False)
    _add(ok, "G5 核验三态正确（一致✓ / 篡改✗ / 无行✗）",
         f"good={r_good['verified']} bad={r_bad['verified']} miss={r_miss['verified']}")

    # G5b 删除的**反向断言**：行还在 ⇒ 必须 False（防"删除失败也报成功"）
    alive = _tmp_db([("E1", "dev", 1, 1, "需求A", "单元需求", "{}")])
    gone = _tmp_db([])
    d_alive = post_check_deleted(alive, "E1", "dev")
    d_gone = post_check_deleted(gone, "E1", "dev")
    okb = d_alive["verified"] is False and d_gone["verified"] is True
    _add(okb, "G5b 删除核验反向断言（行还在✗ / 已消失✓）",
         f"alive={d_alive['verified']} gone={d_gone['verified']}")

    # G5c 边核验：创建要存在且端点一致；删除要消失
    e_db = sqlite3.connect(":memory:")
    e_db.row_factory = sqlite3.Row
    e_db.execute("CREATE TABLE relations (id INTEGER, source_id TEXT, target_id TEXT, "
                 "relation_type TEXT, branch TEXT)")
    # ★ 2026-11-21 真机抓到：提交中文别名「包含」，落库经 core.relmap.canonical
    #   归一为 CONTAINS。核验必须复用同一映射，否则「写入成功却报 failed」。
    e_db.execute("INSERT INTO relations VALUES (7,'A','B','CONTAINS','dev')")
    ec_ok = post_check_edge(e_db, 7, "dev", "A", "B", "包含", op="edge_create")
    ec_wrong = post_check_edge(e_db, 7, "dev", "A", "C", "包含", op="edge_create")
    ec_bogus = post_check_edge(e_db, 7, "dev", "A", "B", "NOT_A_REL", op="edge_create")
    ed_alive = post_check_edge(e_db, 7, "dev", op="edge_delete")
    e_db.execute("DELETE FROM relations WHERE id=7")
    ed_gone = post_check_edge(e_db, 7, "dev", op="edge_delete")
    okc = (ec_ok["verified"] and not ec_wrong["verified"]
           and not ec_bogus["verified"]
           and not ed_alive["verified"] and ed_gone["verified"])
    _add(okc, "G5c 边核验（别名归一✓/端点不符✗/无关类型✗/删除前✗/删除后✓）",
         f"alias_ok={ec_ok['verified']} wrong={ec_wrong['verified']} "
         f"bogus={ec_bogus['verified']} "
         f"del_alive={ed_alive['verified']} del_gone={ed_gone['verified']}")

    # G5d detail 只在失败时给文案。
    #    2026-11-21 真机抓到：成功时也输出 `'A' != 'A'`，ok 是对的但文案反着，
    #    会把排障方向引到"怎么不相等"。判据：通过项 detail 必须为空串。
    bad_details = [(c["check"], c["detail"]) for c in ec_ok["checks"]
                   if c["ok"] and c["detail"]]
    _add(not bad_details, "G5d 通过项不输出失败文案（防误导）",
         f"成功却带 detail: {bad_details}")


def mutations():
    """变异自证：把判据搬到一个"坏世界"里，确认它们会判红。"""
    out = []
    # M1：往已迁移端点的函数体里塞一行 validate_node( ⇒ G2 必须判红
    mig = _migrated()
    victim = None
    for rel in WRITE_FILES:
        p = os.path.join(ROOT, rel)
        if not os.path.isfile(p):
            continue
        text = _read(p)
        for meth, path, pos in _endpoints(text):
            if (meth, path) in mig:
                body = _segment(text, pos)
                if "write_entity" in body:
                    victim = (rel, meth, path, body + "    GraphStore(conn).validator.validate_node(\n")
                    break
        if victim:
            break
    if not victim:
        out.append(("M1 变异体构造（找不到已迁移端点）", False, "无可变异目标"))
    else:
        hit = bool([1 for _l, _p in DUP_PATTERNS if re.search(_p, victim[3])])
        out.append(("M1 注入 validate_node ⇒ G2 判红", hit, "" if hit else "注入后仍未命中"))
    # M2：伪造一个"恒 True"的核验函数 ⇒ G5 的判据必须不认
    fake = lambda *a, **k: {"verified": True, "checks": []}
    ok = fake(None, "E1", "dev", "需求A", "单元需求", {"a": 1}, "update")["verified"] is True
    # 真函数在"无行"时必须 False —— 这里断言两者行为不同，说明判据能区分
    miss = _tmp_db([])
    try:
        from core.graph_write_gate import post_check
        real = post_check(miss, "E1", "dev", "需求A", "单元需求", {"a": 1}, "update")["verified"]
    except Exception:
        real = None
    out.append(("M2 恒 True 的核验与真核验可区分", ok and real is False,
                f"fake={ok} real_on_missing={real}"))
    return out


def main():
    print("=" * 76)
    print("门禁：写入口唯一性（P0-0）")
    print("=" * 76)
    check_g1()
    check_g2_g3()
    check_g4()
    check_g5()

    for st, name, detail in _results:
        print("  [%s] %s%s" % (st, name, ("  <- " + detail) if detail else ""))
    n_fail = sum(1 for r in _results if r[0] == FAIL)

    print("\n--- 变异自证 ---")
    mut = mutations()
    for name, ok, detail in mut:
        print("  [%s] %s%s" % (PASS if ok else FAIL, name, ("  <- " + detail) if detail else ""))
    n_mut_fail = sum(1 for x in mut if not x[1])

    print("\n" + "=" * 76)
    if n_fail or n_mut_fail or not mut:
        print("结论：未通过（常态 %d 条失败 / 变异 %d 组未判红）" % (n_fail, n_mut_fail))
        return 1
    print("结论：全部通过（常态 %d 条 + 变异 %d 组按预期判红）" % (len(_results), len(mut)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
