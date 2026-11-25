# -*- coding: utf-8 -*-
"""静默兜底静态扫描器（P0 治理台账）—— 2026-11-25。

## 回答什么问题
架构评估的 P0 结论是「1980 个 `except Exception` 里 1361 个静默吞掉（69%）」。
那个数字是一次性统计；本脚本把它变成**可重复、可比对**的台账，用于：
  ① 定期看「治理有没有进展」（silent_total 是否在降）；
  ② 门禁卡住「新增静默」——不要求一次改完，但**不许变多**。

## 「静默」的判定（这是本脚本的全部价值所在，必须写死口径）
一个 `except` 处理器算静默，当且仅当**它内部没有任何"可见动作"**：
  · 没有 `raise`（含 `raise ... from`）
  · 没有日志调用：`log/logger/print/warning/error/exception/critical` 等
  · 没有降级/观测台账调用：`note(` / `degrade` / `observe_fail`
  · 没有把异常写进返回值以外的外部结构（如 `append` 到错误列表）
判据用 AST 而非正则：正则会把注释里的 `logger` 当成"可见"，
而真实事故恰恰是「注释写着"已记录"、代码里什么都没做」。

⚠️ **本脚本只做分类，不做价值判断。** `silent` 不等于"错"——
有些兜底（可选增强、缩略图缓存）静默是正确的。所以输出里给的是
**`suspect_main`（疑似主流程）启发式标记**，见下；真正的判定必须由人复核。
把启发式当结论是这类工具最常见的误用。

## `suspect_main` 启发式（明确是启发式，不是断言）
函数名或所在文件命中主流程词表（query/search/retrieve/gen/write/save/load/
import/export/validate/check/apply/sync/commit）即标记。依据：本仓已归档的
真实事故全部落在这些动作上（rag 检索、graph 写入、ingest 门禁、metrics 读取）。
**未命中不代表安全**，只代表"没有历史证据表明它出过事"。

## 用法
    python tools/scan_silent_except.py                # 人类可读汇总
    python tools/scan_silent_except.py --json out.json  # 明细（供看板/门禁比对）
    python tools/scan_silent_except.py --top 40        # 只打印前 N 个疑似主流程点
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

#: 不参与扫描的目录（虚拟环境、缓存、门禁脚本自身 —— 门禁里有大量**故意的**
#: 静默 except 用于变异自证，计入会污染基线）
SKIP_DIRS = {".venv", "__pycache__", ".git", "node_modules", "tmp", "backups",
             "sysml.library", "java-runtime", "fuseki", "sparql", "data",
             "outputs", "perf-report", "screenshots", "tmp_verify", "tmp_viewbench"}

#: **产品代码**目录（默认只扫这些）。
#: 为什么默认不收 `tools/`：那里有 130+ 个一次性脚本与门禁，门禁里大量 `except: pass`
#: 是**故意**的（变异自证要复刻旧实现）。把它们计入会让基线随门禁数量漂移，
#: 而基线一旦漂移就失去"不许变多"的意义。
#: 口径上这与架构评估报告一致（报告统计的是 agent/core/services/routers/repositories）。
PRODUCT_DIRS = {
    "agent", "core", "services", "routers", "repositories",
    "llm", "workflows", "knowledge_pipeline", "database", "plugin_system",
    # ⚠️ `adapters/` 是 2026-11-25 P1 分层归位新增的对外适配层（graph_db / vector_index /
    #    web_tools 等 10 个模块从根目录搬进来）。**漏登记它 = 这些模块的静默兜底从账本里
    #    静默消失**：实测基线 2564、搬迁后 2508，差的 56 条正好等于 adapters 单独扫出的
    #    silent 数（except=76 / strict=10）——「静默变少」是统计口径缩小，不是真的改少了。
    #    新增层/新包时**必须同步这里**，否则基线比对会持续给出虚假的「在变好」。
    "adapters",
}

#: 「可见动作」的名字片段（小写匹配）。命中任一即认为该 except **不是**静默。
VISIBLE_TOKENS = (
    "raise", "log", "logger", "logging", "print", "warning", "warn", "error",
    "exception", "critical", "traceback", "note", "degrade", "observe_fail",
    "append", "add(", "record", "report", "emit", "notify", "alert", "audit",
    "incr", "counter", "metric", "stat",
)

#: 疑似主流程的词表（启发式，见模块 docstring）
MAIN_FLOW_TOKENS = (
    "query", "search", "retrieve", "gen", "write", "save", "load", "import",
    "export", "validate", "check", "apply", "sync", "commit", "insert", "update",
    "delete", "create", "build", "render", "parse", "resolve", "judge", "score",
)


def _iter_py(root: str, product_only: bool = True):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        rel_dir = os.path.relpath(dirpath, root).replace("\\", "/")
        # 只在根目录时按 PRODUCT_DIRS 收窄（子目录不再判断，否则会漏掉包内子目录）
        if product_only and rel_dir == ".":
            dirnames[:] = [d for d in dirnames if d in PRODUCT_DIRS]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root).replace("\\", "/")
            # 根目录下只收直接 .py（产品代码的顶层模块，如 adapters/graph_db.py / core/domain/ingest_gate.py）；
            # tools/ tests/ 等目录在 product_only 时已被上面的 dirnames 过滤掉
            if product_only and rel_dir == "." and rel != fn:
                continue
            # 门禁脚本自身排除（内含故意静默，用于变异自证）
            if rel.startswith("tools/verify/") or rel.startswith("tests/"):
                continue
            yield full, rel


def _call_names(node: ast.AST) -> list[str]:
    """收集节点内所有调用表达式的「被调名字」小写形态（含属性链尾名）。"""
    out = []
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            name = ""
            if isinstance(f, ast.Name):
                name = f.id
            elif isinstance(f, ast.Attribute):
                name = f.attr
            elif isinstance(f, ast.Call):
                # 链式调用 f()(...) —— 取内层名
                inner = f.func
                name = getattr(inner, "id", "") or getattr(inner, "attr", "")
            if name:
                out.append(name.lower())
    return out


def _is_silent(handler: ast.ExceptHandler) -> tuple[bool, str]:
    """返回 (是否静默, 形态描述)。

    形态描述用于人工快速判断（pass / return / assign / continue …），
    不参与判定逻辑。
    """
    body = handler.body or []
    # ① 有 raise ⇒ 可见
    for n in ast.walk(handler):
        if isinstance(n, ast.Raise):
            return False, "raise"
    # ② 有可见名字的调用 ⇒ 可见
    for name in _call_names(handler):
        for tok in VISIBLE_TOKENS:
            # `add(` 这类带括号的 token 用前缀判断不合适，单独处理
            if tok.endswith("("):
                continue
            if tok in name:
                return False, "logged:" + name
    # ③ 兜底：body 只有 pass / return / continue / break / 纯赋值 / 纯表达式常量
    if not body:
        return True, "empty"
    kinds = set()
    for stmt in body:
        if isinstance(stmt, ast.Pass):
            kinds.add("pass")
        elif isinstance(stmt, ast.Return):
            kinds.add("return")
        elif isinstance(stmt, ast.Continue):
            kinds.add("continue")
        elif isinstance(stmt, ast.Break):
            kinds.add("break")
        elif isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            kinds.add("assign")
        elif isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
            kinds.add("docstring")
        else:
            kinds.add("other:" + type(stmt).__name__)
    if all(k in ("pass", "return", "continue", "break", "assign", "docstring")
           for k in kinds):
        return True, "+".join(sorted(kinds))
    return False, "other"


def _is_broad_except(handler: ast.ExceptHandler) -> bool:
    """是否 `except Exception` / 裸 `except:` / 元组里含 Exception。

    为什么单独统计这一维：架构评估报告的「1980 / 1361 / 69%」只数
    `except Exception` 这一类（那是"抓了大类但什么都不做"的典型形态）；
    本扫描器默认统计**所有** `ExceptHandler`（含 `except ValueError:` 等具体类型）。
    ⇒ 两者的分母天生不同。给出这一维，才能让两个数字**可解释地**对上，
    而不是靠"大概差不多"糊过去（本仓纪律：同一个量多处各写一份必然对不上，
    正确做法是让差异可解释，而不是把某个数改到看起来一致）。
    """
    t = handler.type
    if t is None:
        return True
    if isinstance(t, ast.Name) and t.id == "Exception":
        return True
    if isinstance(t, ast.Tuple):
        for el in t.elts:
            if isinstance(el, ast.Name) and el.id == "Exception":
                return True
    return False


def _is_strict_silent(handler: ast.ExceptHandler) -> bool:
    """严格口径：body **只有** `pass`（或空 / 纯表达式常量）。

    ⚠️ 为什么必须同时给两个口径（这是本仓已归档的纪律：同一个量多处各写一份，
    最后必然对不上）：
      · 架构评估报告里的「1361 / 69%」用的是**严格口径**（只算 `: pass`）——
        那种 `except: return 0` / `except: x = {}` 虽然也是兜底，但**至少表达了
        "在这一步放弃并给一个默认值"**的意图，报告把它们算作"有动作"。
      · 本工具默认的宽口径把 `return/assign/continue` 也算静默 —— 因为
        `except: return 0` 在指标层面与 `pass` **同样不可见**（0 与"没算出来"
        长得一样），治理上必须一起看。
    ⇒ 两个数不是矛盾，是同一条光谱的两段。**比对报告用 strict_*，
      治理推进看 silent_*（宽）**。任何对外引用都必须注明用的是哪个口径。
    """
    body = handler.body or []
    for n in ast.walk(handler):
        if isinstance(n, ast.Raise):
            return False
    for name in _call_names(handler):
        for tok in VISIBLE_TOKENS:
            if tok.endswith("("):
                continue
            if tok in name:
                return False
    if not body:
        return True
    return all(isinstance(s, (ast.Pass,))
               or (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))
               for s in body)


def _func_of(tree: ast.AST) -> dict[int, str]:
    """行号 → 所属函数名（取最内层）。用于文档里的「哪个函数在静默」。"""
    m: dict[int, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            start = getattr(node, "lineno", 0)
            end = getattr(node, "end_lineno", start) or start
            for ln in range(start, end + 1):
                m[ln] = node.name
    return m


def _suspect_main(rel: str, func: str) -> bool:
    hay = (rel + " " + (func or "")).lower()
    return any(t in hay for t in MAIN_FLOW_TOKENS)


def scan(root: str | None = None, product_only: bool = True) -> dict:
    """扫描并返回台账。

    :param root: 覆盖根目录（仅供门禁在临时目录做 E2E 自证）。
    :param product_only: True=只扫产品代码目录（默认，快且口径稳定）；
                         False=全仓（含 tools/，用于一次性摸底，**不要进基线**）。
    """
    _root = root or ROOT
    if root:                    # 临时目录（E2E）没有 PRODUCT_DIRS 结构，一律全扫
        product_only = False
    files = 0
    total = 0
    strict_total = 0
    broad_total = 0          # 仅 `except Exception` / 裸 except 的条数（对齐报告分母）
    broad_silent = 0         # 其中静默（宽口径）
    broad_strict = 0         # 其中静默（严格口径）
    items: list[dict] = []
    by_dir: dict[str, dict] = {}
    parse_fail: list[dict] = []

    for full, rel in _iter_py(_root, product_only=product_only):
        files += 1
        try:
            src = open(full, "r", encoding="utf-8", errors="replace").read()
            tree = ast.parse(src, filename=rel)
        except (SyntaxError, OSError, ValueError) as e:
            parse_fail.append({"file": rel, "error": "%s: %s" % (type(e).__name__, str(e)[:120])})
            continue
        fmap = _func_of(tree)
        top = rel.split("/")[0] if "/" in rel else "(root)"
        st = by_dir.setdefault(top, {"except": 0, "silent": 0, "strict": 0})
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            total += 1
            st["except"] += 1
            _broad = _is_broad_except(node)
            if _broad:
                broad_total += 1
            _strict = _is_strict_silent(node)
            if _strict:
                strict_total += 1
                st["strict"] += 1
            silent, kind = _is_silent(node)
            if not silent:
                continue
            st["silent"] += 1
            if _broad:
                broad_silent += 1
                if _strict:
                    broad_strict += 1
            func = fmap.get(node.lineno, "")
            items.append({
                "file": rel, "line": node.lineno, "func": func, "kind": kind,
                "strict": _strict, "broad": _broad,
                "suspect_main": _suspect_main(rel, func),
            })

    silent_total = len(items)
    return {
        "generated_at": __import__("time").strftime("%Y-%m-%d %H:%M:%S"),
        "scanned_files": files,
        "except_total": total,
        # 宽口径：含 `return 默认值 / assign / continue` 兜底（治理视角，见 _is_strict_silent）
        "silent_total": silent_total,
        "silent_pct": round(100.0 * silent_total / total, 1) if total else 0.0,
        # 严格口径：只算 `: pass`（与架构评估报告的 1361 同口径，用于对外引用）
        "strict_silent_total": strict_total,
        "strict_silent_pct": round(100.0 * strict_total / total, 2) if total else 0.0,
        "suspect_main_total": sum(1 for x in items if x["suspect_main"]),
        # 仅 `except Exception` / 裸 except（对齐架构评估报告分母的那一维，见 _is_broad_except）
        "broad_except_total": broad_total,
        "broad_silent_total": broad_silent,
        "broad_silent_pct": round(100.0 * broad_silent / broad_total, 1) if broad_total else 0.0,
        "broad_strict_silent": broad_strict,
        "by_dir": dict(sorted(by_dir.items(), key=lambda kv: -kv[1]["silent"])),
        "parse_fail": parse_fail,
        "items": items,
    }


def _print_human(res: dict, top: int) -> None:
    print("扫描文件：%d ｜ except 总数：%d" % (res["scanned_files"], res["except_total"]))
    print("  · 宽口径静默（含 return 默认值/assign/continue）：%d（%.1f%%）  ← 治理推进看这个"
          % (res["silent_total"], res["silent_pct"]))
    print("  · 严格静默（仅 `: pass`）：%d（%.2f%%）  ← 对外引用/对齐评估报告看这个"
          % (res["strict_silent_total"], res["strict_silent_pct"]))
    print("  · 仅 `except Exception` 一档：%d 条 except / 宽静默 %d（%.1f%%）/ 严格 %d"
          % (res["broad_except_total"], res["broad_silent_total"],
             res["broad_silent_pct"], res["broad_strict_silent"]))
    print("    ↑ 这一维才与架构评估报告的「1980 / 1361 / 69%」同分母。"
          "⚠️ 但两者**仍不可直接比对**：报告未公布其扫描范围与判据实现，"
          "本工具在本机实测的 `except Exception` 基数为 %d 条（多于报告的 1980），"
          "差异已定位到范围（是否含 llm/workflows/knowledge_pipeline 等）与"
          "判据（是否要求 body 仅 pass）。**后续一律以本工具输出为唯一基线。**"
          % res["broad_except_total"])
    print("  · 其中疑似主流程（启发式）：%d" % res["suspect_main_total"])
    print()
    print("%-18s %8s %8s %8s" % ("目录", "except", "宽静默", "严格"))
    for k, v in res["by_dir"].items():
        print("%-18s %8d %8d %8d" % (k, v["except"], v["silent"], v.get("strict", 0)))
    if res["parse_fail"]:
        print("\n⚠ 解析失败 %d 个文件（未计入统计，需人工看）：" % len(res["parse_fail"]))
        for x in res["parse_fail"][:10]:
            print("   %s：%s" % (x["file"], x["error"]))
    sus = [x for x in res["items"] if x["suspect_main"]]
    if sus and top:
        print("\n疑似主流程的静默点（前 %d，启发式标记，需人工复核）：" % min(top, len(sus)))
        for x in sus[:top]:
            print("   %-42s:%-6d %-26s %-14s%s"
                  % (x["file"], x["line"], (x["func"] or "-")[:26], x["kind"],
                     "  [strict]" if x.get("strict") else ""))


def main() -> int:
    ap = argparse.ArgumentParser(description="静默兜底静态扫描（P0 治理台账）")
    ap.add_argument("--json", default="", help="把完整结果写到该路径")
    ap.add_argument("--top", type=int, default=25, help="打印前 N 个疑似主流程点（0=不打印）")
    ap.add_argument("--all", action="store_true",
                    help="全仓扫描（含 tools/ 的一次性脚本，仅用于一次性摸底，别拿它建基线）")
    a = ap.parse_args()
    res = scan(product_only=not a.all)
    _print_human(res, a.top)
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=1)
        print("\n完整结果已写入：%s（%d 条）" % (a.json, len(res["items"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
