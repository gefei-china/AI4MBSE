# -*- coding: utf-8 -*-
"""V8 · 补齐链路缺口①（节点契约）与 ②（已有工具绑定）。

背景：v3.1.0 链路闭环核查（`tools/verify/verify_pipeline_closure.py`）实测出12 类缺口，
其中两类**不依赖任何新工具**、本脚本即可补齐：

① **8 个节点的 `input_schema`/`output_schema` 全为空**
   —— 注册表（`data/modeling_nodes.json`）里**已定义** schema，但V5 入库时没写进库。
   这是「文档有、库里无」的又一例。**后果**：节点间只能靠自由文本传产物，
   N1 的需求条目无法保证 N2 收到、N3 的视图无法保证 N4 校验的是同一份 ⇒ 链路不闭环。

② **6 个节点的工具绑定不全** —— 其中依赖**已存在**工具的那些（graph_retrieve /
   coverage_matrix / graph_db_query / zhiyuan_* / mbse_pull_ingest）本脚本直接补；
   依赖**未实现**工具的（sysml_v2_project_check / stdlib_meta / ast_extract /
   requirement_itemize / methodology_profile_load）**跳过并报告**，不硬绑。

⚠️ 绑定原则：**只绑已存在且 active 的工具**。绑不存在的工具 = 该节点拿到一个
   永远调不通的引用，属"看起来绑好了实际不可用"的静默失效。

幂等 + dry-run 默认。
"""
import argparse
import json
import os
import sqlite3
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REG = os.path.join(_ROOT, "data", "modeling_nodes.json")
DB = os.path.join(_ROOT, "mbse.db")

# 依赖未实现工具的绑定（这些工具尚不存在，跳过）
PENDING_TOOLS = {
    "sysml_v2_project_check", "sysml_stdlib_meta", "sysml_ast_extract",
    "requirement_itemize", "methodology_profile_load", "sysml_v2_lint",
    "sysml_import_graph",
}

stats = {"schema": 0, "bind": 0, "skipped": 0, "same": 0}


def log(k, name, detail=""):
    stats[k] += 1
    tag = {"schema": "SCHEMA", "bind": "BIND", "skipped": "SKIP", "same": "SAME "}[k]
    print(f"  [{tag}] {name}{('  · ' + detail) if detail else ''}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    apply_ = args.apply

    print("=" * 74)
    print(f"V8 补齐节点契约与已有工具绑定  mode={'APPLY' if apply_ else 'DRY-RUN'}")
    print("=" * 74)

    with open(REG, encoding="utf-8") as f:
        plan = json.load(f)
    plan_nodes = {n["agent"]["name"]: n for n in plan["nodes"]}

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    try:
        active_tools = {r["name"] for r in conn.execute(
            "SELECT name FROM tools WHERE status='active'")}
        agents = {r["name"]: r for r in conn.execute(
            "SELECT id, name, input_schema, output_schema FROM agents "
            "WHERE status='active'")}

        # ── ① 节点契约 ──
        print("\n── ① 回填 input_schema / output_schema（注册表是权威来源）──")
        for nm, n in sorted(plan_nodes.items(),
                             key=lambda x: x[1]["stage_order"]):
            a = agents.get(nm)
            if not a:
                log("skipped", nm, "不在 active Agent 里")
                continue
            want_in = n["agent"].get("input_schema") or {}
            want_out = n["agent"].get("output_schema") or {}
            if not want_in or not want_out:
                log("skipped", nm, "注册表里就没定义 schema")
                continue
            cur_in = json.loads(a["input_schema"] or "{}")
            cur_out = json.loads(a["output_schema"] or "{}")
            if cur_in == want_in and cur_out == want_out:
                log("same", nm)
                continue
            if not apply_:
                log("schema", nm, "dry-run")
                continue
            conn.execute(
                "UPDATE agents SET input_schema=?, output_schema=? WHERE id=?",
                (json.dumps(want_in, ensure_ascii=False),
                 json.dumps(want_out, ensure_ascii=False),
                 a["id"]))
            log("schema", nm, f"in={len(want_in.get('properties', {}))} "
                              f"out={len(want_out.get('properties', {}))} 属性")

        # ── ② 已有工具的绑定 ──
        print("\n── ② 补齐已存在工具的绑定（不绑未实现的）──")
        for nm, n in sorted(plan_nodes.items(),
                             key=lambda x: x[1]["stage_order"]):
            a = agents.get(nm)
            if not a:
                continue
            want = n["agent"].get("bind_tools") or []
            have = {r["tool_name"] for r in conn.execute(
                "SELECT tool_name FROM agent_tools WHERE agent_id=? AND tool_type='tool'",
                (a["id"],))}
            for t in want:
                if t in have:
                    log("same", f"{nm} ← {t}")
                    continue
                if t in PENDING_TOOLS:
                    log("skipped", f"{nm} ← {t}", "工具未实现，暂不绑")
                    continue
                if t not in active_tools:
                    log("skipped", f"{nm} ← {t}", "工具不存在或非 active")
                    continue
                if not apply_:
                    log("bind", f"{nm} ← {t}", "dry-run")
                    continue
                conn.execute(
                    "INSERT INTO agent_tools (agent_id, tool_type, tool_name, enabled) "
                    "VALUES (?,'tool',?,1)", (a["id"], t))
                log("bind", f"{nm} ← {t}")
        if apply_:
            conn.commit()
        else:
            conn.rollback()

        # ── 回读自检 ──
        print("\n" + "=" * 74)
        print("回读自检")
        ok = True
        if apply_:
            # 契约复核：逐个查（不能用 agents 缓存，commit 后需重读）
            n_empty = 0
            for nm in plan_nodes:
                r = conn.execute(
                    "SELECT input_schema, output_schema FROM agents "
                    "WHERE name=? AND status='active'", (nm,)).fetchone()
                if not r:
                    continue
                i = json.loads(r["input_schema"] or "{}")
                o = json.loads(r["output_schema"] or "{}")
                if not i or not o:
                    n_empty += 1
                    print(f"  [FAIL] {nm} 契约仍为空")
            print(f"  契约仍为空的节点：{n_empty}（期望 0）")
            if n_empty:
                ok = False
            # 绑定复核：只要求「已存在工具」的绑定齐备
            for nm, n in sorted(plan_nodes.items(),
                                 key=lambda x: x[1]["stage_order"]):
                a = agents.get(nm)
                if not a:
                    continue
                have = {r["tool_name"] for r in conn.execute(
                    "SELECT tool_name FROM agent_tools WHERE agent_id=? AND tool_type='tool'",
                    (a["id"],))}
                want_exist = [t for t in (n["agent"].get("bind_tools") or [])
                              if t in active_tools]
                lack = [t for t in want_exist if t not in have]
                if lack:
                    ok = False
                    print(f"  [FAIL] {nm} 仍缺绑定 {lack}")
            print("  已存在工具的绑定：全部齐备" if ok else "  绑定仍有缺口")
        else:
            print("  [DRY-RUN] 跳过回读")
        print("=" * 74)
        print("✅ 通过" if ok else "❌ 有FAIL 项")
        print(f"\n统计：{stats}")
        if not apply_:
            print("（预览模式，加 --apply 生效）")
        return 0 if ok else 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
