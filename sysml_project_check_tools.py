# -*- coding: utf-8 -*-
"""`sysml_v2_project_check` —— 工程级（多文件合并）SysML v2 门禁。

为什么必须有这个工具（不是"再包一层 validate"）
-------------------------------------------------
实测数据（今天）：
  同一批 `sysml_models/ev_thermal_mgmt/*.sysml`
    单文件口径 `check_code`   → n_hard = 10
    工程级口径 `check_project` → n_hard = 2
⇒ **约 80% 的"错误"是单文件口径的跨文件伪错**（引用在另一个包里，单文件看不到）。
`sysml_v2_check.check_project` 的文档已写明「单文件口径约 2/3 是跨文件伪错，**门禁必须用本口径**」。

所以本工具的价值是：
  ① 让 N6 发布门禁能用**正确口径**判定（否则会长期假红）
  ② 按文件聚合诊断，让 LLM 知道**该改哪个文件**
  ③ 明确告知"这是工程级口径"，避免 LLM 误以为单文件报的都是真错

与 `sysml_v2_autofix` 的关系
---------------------------
本工具**只判不改**（门禁语义）。修不修、怎么修由 autofix 或 LLM 决定。
发布决策链：`project_check` 判口径 → `autofix` 修规则级 → `validate` 复核 → 人工确认。

安全约束（沿用 `_resolve_files` 的路径越界防护）
------------------------------------------------
只允许校验 `sysml_models/` 下的 `.sysml`，防 LLM 借工具读任意文件。
"""
import logging
import os

logger = logging.getLogger(__name__)

MODELS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sysml_models")
MAX_ITEMS_DEFAULT = 20
_VERDICT_HEAD = {
    "pass": "通过 —— 全包合并后无词法/语法错",
    "report": "有语义错但无硬错（不阻断）",
    "block": "存在硬错，禁止发布",
    "unavailable": "校验器不可用，本次**未真正校验**",
}


def _as_int(v, d, lo, hi):
    try:
        return max(lo, min(hi, int(v)))
    except Exception:
        return d


def _resolve_files(raw):
    """解析并**限制**路径 —— 与 sysml_check_tools._resolve_files 同口径（防路径越界）。"""
    items = raw
    if isinstance(items, str):
        items = [s.strip() for s in items.replace(";", ",").split(",") if s.strip()]
    base = os.path.abspath(MODELS_DIR) + os.sep
    if not items:
        import sysml_v2_check as svc
        dft = svc._default_paths()
        if not dft:
            return {"ok": False,
                    "result": "未提供 files，且 sysml_models/ 下没有可校验的 .sysml 文件"}
        return dft
    out = []
    for p in items:
        s = str(p).strip()
        if not s:
            continue
        ap = os.path.abspath(s if os.path.isabs(s) else os.path.join(MODELS_DIR, s))
        if not ap.startswith(base):
            return {"ok": False,
                    "result": f"路径越界：只允许校验 sysml_models/ 下的模型文件（收到 {s}）"}
        if not ap.endswith(".sysml"):
            return {"ok": False, "result": f"只支持 .sysml 文件（收到 {s}）"}
        if not os.path.isfile(ap):
            return {"ok": False, "result": f"文件不存在：{s}"}
        out.append(ap)
    if not out:
        return {"ok": False, "result": "未解析出任何可校验文件"}
    return out


def _render(r, files, max_items):
    import sysml_v2_check as svc
    if not r or not r.get("ok"):
        err = (r or {}).get("error", "")
        return (f"工程级校验未执行：{err}\n"
                f"⚠️ 这不代表模型合法 —— 请如实说明『本次未能校验』，不要声称已通过。")
    v = r.get("verdict")
    lines = [
        f"【SysML v2 工程级门禁】判据：{v} —— {_VERDICT_HEAD.get(v, v)}",
        f"口径：**工程级合并**（{len(files)} 个文件按序合并后一次校验）",
        f"⚠️ 这是发布门禁的正确口径 —— 单文件口径会把跨文件引用报成伪错。",
        f"计数：词法 {r.get('n_lexical', 0)} / 语法 {r.get('n_syntax', 0)} / "
        f"语义 {r.get('n_semantic', 0)}｜硬错合计 {r.get('n_hard', 0)}｜ERROR 共 {r.get('n_error', 0)} 条",
        f"用时 {r.get('elapsed', 0)}s",
    ]
    # 按文件聚合诊断（工程级口径的核心价值：告诉 LLM 该改哪个文件）
    errs = r.get("errors") or []
    by_file = {}
    for e in errs:
        by_file.setdefault(e.get("file") or "<generated>", []).append(e)
    if by_file:
        lines.append("")
        lines.append("按文件聚合（合并后行号已映射回原文件）：")
        shown = 0
        for fn in sorted(by_file):
            items = by_file[fn]
            tag = {1: "词法", 2: "语法", 3: "语义"}
            lines.append(f"  ▸ {fn} —— {len(items)} 条诊断")
            for e in items:
                if shown >= max_items:
                    break
                shown += 1
                src = str(e.get("source") or "").strip()
                lines.append(f"     L{e.get('line')} [{tag.get(e.get('path'), e.get('path'))}] "
                             f"{str(e.get('msg'))[:90]}")
                if src:
                    lines.append(f"源码：{src[:90]}")
            if shown >= max_items:
                lines.append(f"     ……诊断较多，仅列前 {max_items} 条"
                             f"（原始 {r.get('n_error', 0)} 条，可提高 max_items 取全）")
                break
    lines.append("")
    if v == "block":
        lines.append("**禁止发布**。建议顺序：① 调 sysml_v2_autofix 修规则级语法错误"
                     " → ② 调 sysml_v2_autofix 或人工处理需建模判断的项"
                     " → ③ 再次调用本工具复核。")
    elif v == "report":
        lines.append("无词法/语法硬错，可发布但须在发布说明中列出语义错。"
                     "语义错属建模决策，本工具不代为判断。")
    elif v == "pass":
        lines.append("工程级门禁通过，可以进入发布确认环节（HIL L2 仍需人工确认）。")
    else:
        lines.append("⚠️ 校验器不可用：本次未产生任何有效结论，**不得据此声称模型已通过**。")
    return "\n".join(lines)


def _project_check(args):
    try:
        import sysml_v2_check as svc
    except Exception as exc:                                          # noqa: BLE001
        return {"ok": False,
                "result": f"模块不可用：{type(exc).__name__}: {exc}。"
                          f"⚠️ 这不代表模型合法 —— 请如实说明『本次未能校验』。"}
    max_items = _as_int(args.get("max_items"), MAX_ITEMS_DEFAULT, 1, 200)
    files = _resolve_files(args.get("files"))
    if isinstance(files, dict):
        return files
    r = svc.check_project(files)
    try:
        print(svc.line_text(r), flush=True)
    except Exception:
        pass
    return {
        "ok": bool(r.get("ok")),
        "result": _render(r, files, max_items),
        "verdict": r.get("verdict"),
        "blocked": bool(r.get("verdict") == "block"),
        "n_error": r.get("n_error", 0),
        "n_lexical": r.get("n_lexical", 0),
        "n_syntax": r.get("n_syntax", 0),
        "n_semantic": r.get("n_semantic", 0),
        "n_hard": r.get("n_hard", 0),
        "scope": "project",
        "files": [os.path.basename(f) for f in files],
        "error": r.get("error", ""),
        "summary": svc.summarize(r),
    }


def exec_tool(name: str, arguments: dict | None = None) -> dict:
    """工具执行入口。`sysml_v2_project_check` 也被 `sysml_check_tools.exec_tool` 代理。"""
    args = arguments or {}
    if name == "sysml_v2_project_check":
        return _project_check(args)
    if name in ("sysml_v2_validate", "sysml_v2_autofix"):
        import sysml_check_tools as ct
        return ct.exec_tool(name, args)
    return {"ok": False, "result": f"未知 SysML 工具: {name}"}


if __name__ == "__main__":
    import glob
    # ⚠️ 相对路径以 MODELS_DIR 为基座，**子目录必须带上前缀**
    #（实测踩过：只传 "00_master.sysml" 会报"文件不存在"，因它在 ev_thermal_mgmt/ 下）
    _files = [
        os.path.relpath(p, MODELS_DIR).replace(os.sep, "/")
        for p in sorted(glob.glob(os.path.join(MODELS_DIR, "**", "*.sysml"),
                                 recursive=True))
    ]
    r = _project_check({"files": _files}) if _files else {
        "ok": False, "result": "sysml_models/ 下没有 .sysml 文件"}
    print(r["result"])
    print("\n--- 结构化字段 ---")
    for k in ("ok", "verdict", "n_hard", "n_error", "n_semantic", "files"):
        print(f"  {k} = {r.get(k)}")
