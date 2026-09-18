"""基础通用报告导出工具（report_export）：内容 → 对应格式文档（md/docx/pdf）。

与 file_* 文件工具 / 「报告生成」技能同层的基础能力，双链路可用：
- Agent 会话：LLM 生成报告后调用本工具导出（write 副作用，走写意图注入 + HIL 门控）
- 工作流 tool 节点：把上游 LLM/Agent 节点生成的报告直接导出为 Word/PDF 落盘

复用 report_generator（同一数据契约 Report = {title, sections:[{heading, body, table?}], summary}）。
"""
import json
import os

from report_generator import report_generator

REPORT_TOOL_DEFS = [
    {
        "name": "report_export",
        "description": "把报告内容导出为指定格式文档（md/docx/pdf）并落盘：path 输出文件路径（必填）、title 报告标题、"
                       "content 报告全文（markdown，按 ## 分节）与 sections JSON 分节二选一、fmt 格式（md|docx|pdf，默认 md）；写操作",
        "side_effect": "write", "risk_level": "low", "version": "v1.0",
        "input_schema": {"type": "object", "properties": {
            "path": {"type": "string", "description": "输出文件绝对路径或相对路径（必填）"},
            "title": {"type": "string", "description": "报告标题"},
            "content": {"type": "string", "description": "报告全文 markdown（按 ## 分节；与 sections 二选一）"},
            "sections": {"type": "string", "description": "结构化分节 JSON：[{\"heading\",\"body\"},...]（与 content 二选一）"},
            "fmt": {"type": "string", "description": "导出格式：md|docx|pdf（默认 md）"}},
            "required": ["path"]},
    },
]
REPORT_TOOL_NAMES = {d["name"] for d in REPORT_TOOL_DEFS}


def _sections_from_markdown(markdown: str) -> list:
    """markdown 全文 → 分节（按 ## 标题切分，无标题则整篇为「正文」）。"""
    sections, cur = [], None
    for line in (markdown or "").splitlines():
        if line.startswith("## "):
            if cur:
                sections.append(cur)
            cur = {"heading": line[3:].strip(), "body": []}
        elif cur is not None:
            cur["body"].append(line)
    if cur:
        sections.append(cur)
    for s in sections:
        s["body"] = "\n".join(s["body"]).strip()
    return sections or [{"heading": "正文", "body": (markdown or "").strip()}]


def _guard_path(path: str) -> str:
    """复用 file_tools 关键文件保护：拒绝覆盖平台运行时关键文件。返回空串表示放行。"""
    try:
        from file_tools import _is_critical
        if _is_critical(path):
            return f"拒绝写入：{path} 为平台运行时关键文件"
    except Exception:
        pass
    return ""


def exec_report_tool(name: str, arguments: dict | None = None) -> dict:
    """统一入口：执行报告导出工具，返回 {"ok": bool, "result": str}。异常不抛出。"""
    args = arguments or {}
    if name != "report_export":
        return {"ok": False, "result": f"未知报告工具: {name}"}
    path = str(args.get("path") or "").strip()
    if not path:
        return {"ok": False, "result": "path（输出文件路径）必填"}
    guard = _guard_path(path)
    if guard:
        return {"ok": False, "result": guard}
    fmt = str(args.get("fmt") or "md").lower().lstrip(".")
    if fmt not in ("md", "docx", "pdf"):
        return {"ok": False, "result": f"不支持的格式: {fmt}（仅支持 md/docx/pdf）"}
    title = str(args.get("title") or "报告")
    # sections（JSON 字符串或对象）与 content（markdown 全文）二选一
    sections = args.get("sections")
    if isinstance(sections, str):
        try:
            sections = json.loads(sections)
        except Exception:
            sections = None
    if not sections or not isinstance(sections, list):
        content = str(args.get("content") or "")
        sections = _sections_from_markdown(content)
    if not sections or not any(s.get("body") for s in sections):
        return {"ok": False, "result": "报告内容为空：请提供 content（markdown 全文）或 sections（分节）"}
    report = {"title": title, "sections": sections, "summary": "", "report_type": "analysis"}
    try:
        out = report_generator.export(report, fmt, path)
        return {"ok": True, "result": f"已导出 {fmt.upper()} 报告（{len(sections)} 节）→ {out}"}
    except Exception as e:
        return {"ok": False, "result": f"报告导出失败: {str(e)[:200]}"}
