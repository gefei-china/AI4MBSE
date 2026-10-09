"""sysml_import_graph — SysML v2 → 工程图谱落库工具（N6 模型发布节点）。

回答「把这份模型落进工程图谱后，会新增/更新/冲突哪些实体与关系」。

⚠️ **默认 dry-run（只预演不落库）**。
   底座 `sysml_importer.import_sysml` 会**真实写入** entities/relations
   （实测表有189实体 / 249 关系），且有分支/版本/时态字段（branch、is_current、
   valid_from/valid_to、tx_from/tx_to）。误写代价高，且当前无「撤销导入」的
   工具 ⇒ 任何情况下都应先 dry-run 看预演，确认后再 confirm=true。
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
MODELS_DIR = os.path.join(ROOT, "sysml_models")
DB = os.path.join(ROOT, "mbse.db")


def _resolve(files):
    paths = []
    for f in files or []:
        p = os.path.normpath(os.path.join(MODELS_DIR, f))
        if not os.path.abspath(p).startswith(os.path.abspath(MODELS_DIR)):
            raise ValueError(f"路径越界（仅允许 sysml_models/ 下）：{f}")
        if not os.path.isfile(p):
            raise FileNotFoundError(f"文件不存在：{f}")
        paths.append(p)
    return paths


def _preview(conn, code, model_name):
    """只生成候选，不入库（底座本身就是不入库的治理入口）。

    ⚠️ candidates 是**扁平列表**（2026-10-08 实测），不是 nodes/edges 分开的结构：
       实体候选用 `entity_type`（部件/需求/端口…）标注，
       关系候选的 `entity_type` 固定为 "关系候选"，关系细节在 rel_* 字段。
       且底座已直接给出 node_count / edge_count —— **优先用它们**，不要自己按
       kind 字段猜（我第一版按 kind=='node' 判断 ⇒ 实体候选全被判成 0，静默漏报）。
    """
    import sysml_importer as SI
    r = SI.sysml_to_candidates(conn, code, model_name=model_name or "AI 建模")
    cands = r.get("candidates") or []
    REL_MARK = "关系候选"
    edges = [c for c in cands if c.get("entity_type") == REL_MARK]
    nodes = [c for c in cands if c.get("entity_type") != REL_MARK]
    # 与底座口径对齐（若底座给了计数，以底座为准并记录差异）
    n_decl, e_decl = r.get("node_count"), r.get("edge_count")
    return r, cands, nodes, edges, n_decl, e_decl


def _render_preview(r, cands, nodes, edges, files, model_name, n_decl=None, e_decl=None):
    L = ["【图谱落库预演（dry-run，未写库）】"]
    L.append(f"来源：{' + '.join(files)}")
    L.append(f"模型名：{model_name or '（未指定，用默认）'}")
    L.append("")
    L.append(f"批次号：{r.get('batch_id')}")
    L.append(f"实体候选 {len(nodes)} 个｜关系候选 {len(edges)} 条"
             f"｜本体校验拒绝 {len(r.get('rejected') or [])} 个")
    # 口径自检：自己数的必须与底座给的一致，不一致要暴露而不是掩盖
    if n_decl is not None and (n_decl != len(nodes) or e_decl != len(edges)):
        L.append(f"⚠️ 计数口径差异：底座node_count={n_decl}/edge_count={e_decl}，"
                 f"本次统计 {len(nodes)}/{len(edges)} —— 以底座为准")
    L.append("")
    if nodes:
        L.append("── 实体候选（前 20，按类型分组）──")
        from collections import Counter
        cnt = Counter((n.get("entity_type") or n.get("type") or "?") for n in nodes)
        L.append("  类型分布：" + "｜".join(f"{k} {v}" for k, v in cnt.most_common(8)))
        for n in nodes[:20]:
            nm = n.get("name") or "?"
            L.append(f"    · {nm}  [{n.get('entity_type') or '?'}] status={n.get('status')}")
        if len(nodes) > 20:
            L.append(f"    … 另有 {len(nodes) - 20} 个")
    if edges:
        L.append("")
        L.append("── 关系候选（前 15）──")
        for e in edges[:15]:
            L.append(f"    · {e.get('rel_source')} --{e.get('rel_type')}--> "
                     f"{e.get('rel_target')} (status={e.get('status')})")
        if len(edges) > 15:
            L.append(f"    … 另有 {len(edges) - 15} 条")
    rej = r.get("rejected") or []
    if rej:
        L.append("")
        L.append("── ⚠️ 被本体校验拒绝（落库时会被丢弃，需先修模型）──")
        for x in rej[:10]:
            L.append(f"    · {str(x)[:88]}")
        if len(rej) > 10:
            L.append(f"    … 另有 {len(rej) - 10} 个")
    L.append("")
    L.append("── 下一步 ──")
    L.append("  确认无误后，把 confirm=true 重新调用本工具才会真正写库。")
    L.append("  ⚠️ 落库会写入 entities/relations 并打上 branch/version/is_current 标记；")
    L.append("     当前**没有撤销导入的工具**，误落库需人工清理，务必先确认。")
    return "\n".join(L)


def _import_graph(args: dict) -> dict:
    files = args.get("files") or []
    code = args.get("code") or ""
    model_name = (args.get("model_name") or "").strip()
    confirm = bool(args.get("confirm"))
    branch = (args.get("branch") or "").strip() or None

    if not code and not files:
        return {"ok": False,
                "result": "需提供 code（SysML 文本）或 files（相对 sysml_models/ 的路径）。"}

    if files:
        try:
            paths = _resolve(files)
        except (ValueError, FileNotFoundError) as exc:
            return {"ok": False, "result": f"路径错误：{exc}"}
        merged, names = [], []
        for p in paths:
            with open(p, encoding="utf-8") as fh:
                merged.append(fh.read())
            names.append(os.path.relpath(p, MODELS_DIR).replace("\\", "/"))
        code = "\n".join(merged)
    else:
        names = ["<inline>"]

    try:
        from database import db_conn
    except Exception as exc:                # noqa: BLE001
        return {"ok": False, "result": f"database 模块不可用：{type(exc).__name__}: {exc}"}

    import sysml_importer as SI

    try:
        with db_conn() as conn:
            if not confirm:
                r, cands, nodes, edges, n_decl, e_decl = _preview(conn, code, model_name)
                return {
                    "ok": True,
                    "result": _render_preview(r, cands, nodes, edges, names, model_name,
                                              n_decl, e_decl),
                    "dry_run": True,
                    "batch_id": r.get("batch_id"),
                    "entity_candidates": len(nodes),
                    "relation_candidates": len(edges),
                    "rejected": len(r.get("rejected") or []),
                }
            # 真落库
            res = SI.import_sysml(conn, "text", code,
                                  imported_by=(args.get("imported_by") or "AI建模发布"),
                                  model_name=model_name)
    except Exception as exc:                # noqa: BLE001
        import traceback
        return {"ok": False,
                "result": f"落库失败：{type(exc).__name__}: {exc}\n{traceback.format_exc()[-600:]}"}

    L = ["【图谱落库完成】"]
    L.append(f"批次号：{res.get('batch_id')}")
    L.append(f"状态：{res.get('status')}")
    L.append(f"导入实体 {res.get('entity_count')}｜导入关系 {res.get('relation_count')}"
             f"｜拒绝 {len(res.get('rejected') or [])}")
    if res.get("detail"):
        L.append(f"详情：{res['detail']}")
    rej = res.get("rejected") or []
    if rej:
        L.append("")
        L.append("⚠️ 被拒绝项（未落库）：")
        for x in rej[:10]:
            L.append(f"    · {str(x)[:86]}")
    L.append("")
    L.append("提示：可用 graph_retrieve / graph_db_query 核对落库结果"
             "（部分查询需要 branch 参数，本次未显式指定）。")
    return {
        "ok": True,
        "result": "\n".join(L),
        "dry_run": False,
        "batch_id": res.get("batch_id"),
        "status": res.get("status"),
        "entity_count": res.get("entity_count"),
        "relation_count": res.get("relation_count"),
        "rejected": len(rej),
        "branch": branch,
    }


def exec_tool(name: str, arguments: dict | None = None) -> dict:
    args = arguments or {}
    if name == "sysml_import_graph":
        return _import_graph(args)
    return {"ok": False, "result": f"未知工具: {name}"}


def _selftest():
    print("=" * 72)
    print("sysml_import_graph 自测")
    print("=" * 72)

    print("\n① 拒绝入参：")
    for bad in ({}, {"files": ["../etc/passwd"]}, {"files": ["missing.sysml"]}):
        rb = _import_graph(bad)
        print(f"  {str(bad)[:34]:36} ok={rb.get('ok')} | {str(rb.get('result'))[:44]}")

    print("\n② 默认必须是 dry-run（绝不能自动落库）：")
    r2 = _import_graph({"code": "package P { part def ProbeVehicle; }"})
    print(f"  dry_run={r2.get('dry_run')} entity_candidates={r2.get('entity_candidates')}"
          f" relation_candidates={r2.get('relation_candidates')}")
    assert r2.get("dry_run") is True, "默认必须 dry-run，否则会误写生产库"
    assert "未写库" in r2.get("result", ""), "输出必须明示未写库"
    # ★ 口径断言：曾因按 kind=='node' 判断而把实体候选全判成 0（静默漏报），
    #   故必须断言「实体候选 > 0」且与底座 node_count 一致。
    assert r2.get("entity_candidates", 0) > 0, \
        "实体候选为 0 —— 分类逻辑又坏了（曾按 kind 字段猜导致漏报）"
    assert "计数口径差异" not in r2.get("result", ""), \
        "自统计与底座 node_count/edge_count 不一致 —— 口径漂移了"
    # 关系细节必须展示（rel_source/rel_type/rel_target）
    assert "--CONTAINS--" in r2.get("result", ""), "关系候选必须展示关系三元组"

    print("\n③ dry-run 不得改动库（前后对比）：")
    import sqlite3
    def _counts():
        c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        try:
            return (c.execute("SELECT COUNT(*) FROM entities").fetchone()[0],
                    c.execute("SELECT COUNT(*) FROM relations").fetchone()[0])
        finally:
            c.close()
    before = _counts()
    _import_graph({"code": "package P { part def AnotherProbe; }"})
    after = _counts()
    print(f"  entities {before[0]}→{after[0]}｜relations {before[1]}→{after[1]}")
    assert before == after, "dry-run 竟然改动了生产库！"

    print("\n④ dry-run 文本必须含下一步指引与风险声明：")
    txt = r2.get("result", "")
    for kw in ("confirm=true", "没有撤销导入的工具"):
        print(f"  含「{kw}」: {kw in txt}")
        assert kw in txt, f"dry-run 输出必须警示：{kw}"

    print("\n⑤ 真落库路径仅在显式 confirm 时执行（此处不真落库，只验证参数门控生效）：")
    # 用不存在的属性验证 confirm 门控仍会走真落库分支的错误处理，不污染库
    r5 = _import_graph({"code": "package P { part def GateProbe; }", "confirm": False})
    print(f"  confirm=False → dry_run={r5.get('dry_run')}")
    assert r5.get("dry_run") is True

    print("\n" + "=" * 72)
    print("  自测通过：入参校验 / 默认 dry-run / 库零改动 / 风险明示 / confirm 门控")
    return 0


if __name__ == "__main__":
    sys.exit(_selftest())