# -*- coding: utf-8 -*-
"""SysML v2 校验工具（AI 建模闭环的「暴露 → 回喂」段）。

工具清单：
  · `sysml_v2_validate` —— 校验 SysML v2 源码，返回**三路计数**（词法/语法/语义）+
    按位置聚合的错误清单（附源码原文与修复方向），供 LLM 依诊断自行修复。

────────────────────────────────────────────────────────────────────────────
四条设计约束（先读再改）
────────────────────────────────────────────────────────────────────────────
① **只判错，不修复**。本模块不生成修复方案、不改写代码。原因：自动改写、尤其"改到校验通过为止"
   的自动修复会**掩盖真实建模缺陷**（把类型族错误改成能编译的形状，模型语义就丢了）。
   修复是 LLM 的职责——它能看到诊断 + 源码 + L0 约束卡三重信息。
② **输出面向 LLM，不是面向人**。同位置的重复诊断必须聚合（实测 `satisfy R by A;` 一处吐 4 条），
   且**必须用源码原文回显**——校验器 stdout 非 UTF-8，中文字符会变成 `'??'`，
   LLM 拿到乱码根本无法定位（详见 `sysml_v2_check._parse` 的 `source` 字段）。
③ **`ok` ≠ `verdict`**（最容易搞混的一点）：
     · `ok`      = **工具执行成功了吗**（进程跑通、拿到诊断）；
     · `verdict` = **模型合法吗**（`pass`/`report`/`block`/`unavailable`）。
   发现语法错是**校验成功**，故 `ok=True`。若把发现错误当成工具失败，LLM 会去重试工具而不是修模型。
④ **一切异常降级为结构化错误，绝不抛异常**（不阻断建模主链路）。校验器不可用时必须**明确告知
   LLM"本次未真正校验"**，否则它会把这当成"通过"——这是比报错更危险的失败模式。
"""
import logging
import os

logger = logging.getLogger(__name__)

MODELS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sysml_models")
MAX_ITEMS_DEFAULT = 20      # 回喂条目上限（防打爆 prompt；超出部分只报计数）
_VERDICT_HEAD = {
    "pass": "通过 —— 无词法/语法错",
    "report": "有语义错但无硬错（不阻断）",
    "block": "存在硬错，必须修复后重新校验",
    "unavailable": "校验器不可用，本次**未真正校验**",
}


def exec_tool(name: str, arguments: dict | None = None) -> dict:
    """工具执行入口（`agent.pipeline_parts.tools._exec_tool_call` 按 `sysml_v2_` 前缀路由）。"""
    args = arguments or {}
    if name == "sysml_v2_validate":
        return _validate(args)
    return {"ok": False, "result": f"未知 SysML 校验工具: {name}"}


def _as_int(v, default, lo, hi):
    try:
        return max(lo, min(hi, int(v)))
    except Exception:
        return default


def _resolve_files(raw):
    """解析并**限制**文件路径 —— 防 LLM 借校验工具读任意文件。

    返回路径列表；出错时返回 `{"ok": False, "result": ...}`（调用方直接透传）。
    """
    items = raw
    if isinstance(items, str):
        items = [s.strip() for s in items.replace(";", ",").split(",") if s.strip()]
    if not items:
        import sysml_v2_check as svc
        dft = svc._default_paths()
        if not dft:
            return {"ok": False, "result": "未提供 files，且默认模型目录下没有可校验的 .sysml 文件"}
        return dft
    base = os.path.abspath(MODELS_DIR) + os.sep
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


def _render(r, scope_note, max_items):
    """把校验结果渲染成**给 LLM 读**的文本（工具契约的 `result` 字段）。"""
    import sysml_v2_check as svc

    if not r or not r.get("ok"):
        err = (r or {}).get("error", "")
        return (f"SysML v2 校验未执行：{err}\n"
                f"⚠️ 这不代表模型合法 —— 请在回答中如实说明『本次未能校验』，不要声称已通过。")

    v = r.get("verdict")
    head = _VERDICT_HEAD.get(v, v)
    lines = [
        f"【SysML v2 校验】判据：{v} —— {head}",
        f"口径：{scope_note}｜用时 {r.get('elapsed', 0)}s",
        f"计数：词法 {r.get('n_lexical', 0)} / 语法 {r.get('n_syntax', 0)} / "
        f"语义 {r.get('n_semantic', 0)}｜ERROR 共 {r.get('n_error', 0)} 条，WARN {r.get('n_warn', 0)} 条",
    ]
    items = svc.diagnostics(r, max_items=max_items)
    if items:
        lines.append("")
        lines.append("按位置聚合（同位置的重复诊断已合并）：")
        _tag = {"lexical": "词法", "syntax": "语法", "semantic": "语义"}
        for i, d in enumerate(items, 1):
            _cols = d.get("cols") or [d.get("col")]
            _loc = f"第 {d['line']} 行 第 {_cols[0]} 列"
            if len(_cols) > 1:
                _loc += f" 起（本行共 {len(_cols)} 处同类）"
            lines.append(f"{i}. [{_tag.get(d['path'], d['path'])}] {d['file']} {_loc}")
            if d.get("source"):
                lines.append(f"   源码：{d['source']}")
            for m in d.get("messages") or []:
                lines.append(f"   诊断：{m}")
            if d.get("hint"):
                lines.append(f"   方向：{d['hint']}")
        # ⚠️ 不要用 `n_error - len(items)` 算"未列出条数"：合并后**条目数 ≠ 覆盖的诊断数**
        #    （一条目可覆盖同行多处、同位置多条），那样算出来的数字是错的（实测踩过：9 条全列了却报"另有 7 条"）。
        #    只有 `max_items` 截断时才可能真有未列出的。
        if len(items) >= max_items:
            lines.append(f"……诊断较多，仅列出前 {max_items} 条（已按位置与同类合并；"
                         f"原始共 {r.get('n_error', 0)} 条，可提高 max_items 取全）。")
    lines.append("")
    if v == "block":
        lines.append("请按上述位置与方向修复代码，修复后**再次调用本工具**校验。"
                     "不要在未复核的情况下声称已修复。")
    elif v == "report":
        lines.append("无词法/语法硬错；语义错属建模决策（如「引用 usage 而非 def」），请判断是否需要处理，"
                     "处理后再调用本工具复核。")
    elif v == "pass":
        lines.append("模型通过词法与语法校验。")
    else:
        lines.append("⚠️ 校验器不可用：本次未产生任何有效校验结论，不要据此声称模型已通过。")
    return "\n".join(lines)


def _validate(args):
    """`sysml_v2_validate`：校验 SysML v2 源码（单产物 `code` / 项目级合并 `files`）。"""
    try:
        import sysml_v2_check as svc
    except Exception as exc:                                   # noqa: BLE001
        return {"ok": False, "result": f"SysML 校验模块不可用：{type(exc).__name__}: {exc}"}

    mode = str(args.get("mode") or "code").strip().lower()
    max_items = _as_int(args.get("max_items"), MAX_ITEMS_DEFAULT, 1, 200)

    if mode in ("project", "files"):
        files = _resolve_files(args.get("files"))
        if isinstance(files, dict):
            return files
        r = svc.check_project(files)
        scope_note = f"项目级合并口径（{len(files)} 个文件按序合并后校验）"
    else:
        code = args.get("code")
        if not code or not str(code).strip():
            return {"ok": False,
                    "result": "参数 code 为空：请把要校验的 SysML v2 源码整段作为 code 传入"
                              "（不要传文件路径；校验已入库文件请用 mode=project + files）。"}
        r = svc.check_code(str(code))
        scope_note = "单产物口径（只校验本次传入的这段代码）"

    out = {
        "ok": bool(r.get("ok")),
        "result": _render(r, scope_note, max_items),
        "verdict": r.get("verdict"),
        "blocked": bool(r.get("verdict") == "block"),
        "n_error": r.get("n_error", 0),
        "n_lexical": r.get("n_lexical", 0),
        "n_syntax": r.get("n_syntax", 0),
        "n_semantic": r.get("n_semantic", 0),
        "n_hard": r.get("n_hard", 0),
        "n_warn": r.get("n_warn", 0),
        "scope": r.get("source", ""),
        "error": r.get("error", ""),
        "summary": svc.summarize(r),          # 与版本留痕同结构，便于比对
    }
    try:
        print(svc.line_text(r), flush=True)
    except Exception:
        pass
    return out
