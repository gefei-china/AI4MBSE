"""sysml_ast_extract — SysML v2 结构化抽取工具。

回答「这个模型里有哪些东西、它们怎么连的」，供 N3 视图展开 / N5 追溯核验 /
覆盖性分析消费。

底座：`sysml_ast.parse_text_ast`（Java 实现的真 AST 解析，非正则猜测）。
若Java 侧不可用，本工具**如实降级**并说明，绝不用正则假装解析成功。
"""
from __future__ import annotations

import collections
import os
import re
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
MODELS_DIR = os.path.join(ROOT, "sysml_models")

# 项目实测：AST 实测覆盖到的 kind（2026-10-08 六文件实测）
# 声明但样例未覆盖：connection/constraint/interface/item/occurrence/state/view
#⚠️ state 未被覆盖 ⇒ 状态机视图的转换/守护关系抽不出边，属已知缺口。
KIND_ZH = {
    "package": "包", "part": "部件", "item": "项", "requirement": "需求",
    "port": "端口", "interface": "接口", "attribute": "属性", "action": "功能",
    "state": "状态", "use_case": "用例", "constraint": "约束",
    "connection": "连接", "view": "视图", "occurrence": "发生",
}


# ── 路径与输入 ───────────────────────────────────────────────
def _resolve_files(files):
    """把相对路径解析为绝对路径；越界一律拒绝（与 project_check 同口径）。"""
    out = []
    for f in files or []:
        p = os.path.normpath(os.path.join(MODELS_DIR, f))
        if not os.path.abspath(p).startswith(os.path.abspath(MODELS_DIR)):
            raise ValueError(f"路径越界（仅允许 sysml_models/ 下）：{f}")
        if not os.path.isfile(p):
            raise FileNotFoundError(f"文件不存在：{f}")
        out.append(p)
    return out


# ── 主抽取 ───────────────────────────────────────────────────
def _extract(args: dict) -> dict:
    code = (args.get("code") or "").strip()
    file_list = args.get("files") or []
    view_type = args.get("view_type") or ""
    need_edges = bool(args.get("include_edges", True))
    max_items = int(args.get("max_items") or 60)

    if not code and not file_list:
        return {"ok": False,
                "result": "需提供 code（SysML 文本）或 files（相对 sysml_models/ 的路径列表）。"
                          "二者都没给，无法抽取。"}

    # 单文件口径 vs 合并口径：跨文件引用只有合并才看得见
    if file_list:
        try:
            paths = _resolve_files(file_list)
        except (ValueError, FileNotFoundError) as exc:
            return {"ok": False, "result": f"路径错误：{exc}"}
        sources = [(os.path.relpath(p, MODELS_DIR).replace("\\", "/"), open(p, encoding="utf-8").read())
                   for p in paths]
        merged = "\n".join(t for _, t in sources)
        code = merged
    else:
        sources = [("<inline>", code)]

    # 真 AST 解析（失败就明说，不退化）
    try:
        import sysml_ast as A
    except Exception as exc:                # noqa: BLE001
        return {"ok": False,
                "result": f"AST 模块不可用：{type(exc).__name__}: {exc}。"
                          f"⚠️ 这不代表模型里没有元素 —— 不得臆造节点。"}
    if not A.available():
        return {"ok": False,
                "result": "AST 解析器不可用（Java 侧未就绪）。"
                          "⚠️ 无法给出真实结构 —— 请如实说明『未能解析』，不要用正则猜测代替。"}
    try:
        parsed = A.parse_text_ast(code)
    except Exception as exc:                # noqa: BLE001
        return {"ok": False, "result": f"AST 解析失败：{type(exc).__name__}: {exc}"}

    nodes = parsed.get("nodes") or []
    edges = parsed.get("edges") or []

    # 按 kind 过滤（view_type → kind 映射）
    VIEW_KIND = {
        "requirement": ["requirement"], "structure": ["part", "package"],
        "usecase": ["use_case"], "activity": ["action"],
        "ibd": ["port", "interface", "connection"], "part": ["part"],
    }
    if view_type:
        want = set()
        for k in view_type.split(","):
            want.update(VIEW_KIND.get(k.strip(), [k.strip()]))
        if want:
            nodes = [n for n in nodes if n.get("properties", {}).get("kind") in want]

    kind_stat = collections.Counter(n.get("properties", {}).get("kind") for n in nodes)
    rel_stat = collections.Counter(e.get("relation_type") for e in edges)

    L = []
    L.append("【SysML 结构化抽取结果】")
    L.append(f"来源：{' + '.join(n for n, _ in sources)}")
    L.append(f"解析口径：{'合并（多文件，可见跨文件引用）' if len(sources) > 1 else '单文件'}")
    if view_type:
        L.append(f"视图过滤：{view_type}")
    L.append("")
    L.append("── ① 元素统计 ──")
    L.append(f"节点 {len(nodes)} 个｜关系 {len(edges)} 条")
    if kind_stat:
        L.append("  " + "｜".join(
            f"{KIND_ZH.get(k, k)} {v}" for k, v in kind_stat.most_common()))
    if rel_stat and need_edges:
        L.append("  关系：" + "｜".join(f"{k} {v}" for k, v in rel_stat.most_common()))

    # 按文件归属：合并口径下行号来自合并文本，必须映射回原文件
    L.append("")
    L.append("── ② 元素清单 ──")
    shown = nodes[:max_items]
    for n in shown:
        k = n.get("properties", {}).get("kind") or "?"
        nm = n.get("name") or "?"
        L.append(f"  [{KIND_ZH.get(k, k)}] {nm}")
    if len(nodes) > len(shown):
        L.append(f"  … 另有 {len(nodes) - len(shown)} 个（max_items={max_items} 截断）")

    if need_edges and edges:
        L.append("")
        L.append("── ③ 关系清单 ──")
        eshown = edges[:max_items]
        for e in eshown:
            L.append(f"  {e.get('source_name')} --{e.get('relation_type')}--> "
                     f"{e.get('target_name')}")
        if len(edges) > len(eshown):
            L.append(f"  … 另有 {len(edges) - len(eshown)} 条关系（截断）")

    # 已知缺口如实告知—— 不让调用方误以为"抽全了"
    covered = set(k for k in kind_stat if k)
    missing = [KIND_ZH[k] for k in ("state", "connection", "constraint", "view")
               if k in KIND_ZH and k not in covered]
    L.append("")
    L.append("── ④ 覆盖度声明（防误判为『已抽全』）──")
    if missing:
        L.append(f"  ⚠️ 本次未覆盖的元类：{'、'.join(missing)}")
        L.append("     原因：AST 导出器的 NODE_KINDS 未映射这些元类（非本次抽取失败）。")
        L.append("     影响：状态机视图的转换/守护关系抽不出边，勿据此断言『模型里没有转换』。")
    else:
        L.append("  本次覆盖全部已声明元类。")

    return {
        "ok": True,
        "result": "\n".join(L),
        "node_count": len(nodes),
        "edge_count": len(edges),
        "kind_stat": dict(kind_stat),
        "rel_stat": dict(rel_stat),
        "view_type": view_type,
        "uncovered_kinds": missing,
        "sources": [n for n, _ in sources],
    }


def exec_tool(name: str, arguments: dict | None = None) -> dict:
    args = arguments or {}
    if name == "sysml_ast_extract":
        return _extract(args)
    return {"ok": False, "result": f"未知工具: {name}"}


def _selftest():
    print("=" * 72)
    print("sysml_ast_extract 自测")
    print("=" * 72)
    print("\n① 代码抽取（视图过滤）：")
    r = _extract({"code": "package P { part def V; part v { action a; } }",
                  "view_type": "activity"})
    print(f"  ok={r.get('ok')} nodes={r.get('node_count')} kinds={r.get('kind_stat')}")

    print("\n② 真实样例（合并口径 + 追溯关系）：")
    r2 = _extract({"files": ["ev_thermal_mgmt/01_requirements.sysml",
                             "ev_thermal_mgmt/02_architecture.sysml"]})
    print(f"  ok={r2.get('ok')} nodes={r2.get('node_count')} edges={r2.get('edge_count')}")
    print(f"  关系分布={r2.get('rel_stat')}")
    print(f"  未覆盖={r2.get('uncovered_kinds')}")

    print("\n③ 拒绝入参：")
    for bad in ({}, {"files": ["../etc/passwd"]}, {"files": ["nope.sysml"]}):
        rb = _extract(bad)
        print(f"  {str(bad)[:34]:36} ok={rb.get('ok')} | {str(rb.get('result'))[:40]}")

    print("\n④ 状态覆盖缺口必须被如实声明：")
    r4 = _extract({"files": ["ev_thermal_mgmt/03_use_cases.sysml"]})
    print(f"  未覆盖声明={r4.get('uncovered_kinds')}")
    return 0


if __name__ == "__main__":
    sys.exit(_selftest())