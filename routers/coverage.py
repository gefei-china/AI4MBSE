"""覆盖性分析域：/api/coverage/*（SRS-GN-CO，2026-09-20）。

前端呈现端点：把 coverage_tools.exec_tool（只读确定性计算）包装为 HTTP。
scope 默认解析在 exec_tool._resolve_scope 内完成——显式参数 > settings
默认工程/分支 > 拒绝分析（宁可不给数字，不给混算数字）。本路由不做任何
口径判断，保证前后端数字同源（对话 Agent 与页面共用同一工具层）。
"""
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from coverage_tools import exec_tool, COVERAGE_TOOL_NAMES

router = APIRouter(tags=["覆盖性分析"])


@router.get("/api/coverage/tools")
def list_tools():
    """可用覆盖性工具清单（前端 Tab 用）。"""
    return {"tools": list(COVERAGE_TOOL_NAMES)}


@router.get("/api/coverage/{tool_name}")
def run_tool(tool_name: str, branch: str = "", project_id: str = "", batch_id: str = ""):
    """执行一个覆盖性工具（只读）。返回 {ok, result|error}。

    result 为工具的结构化 JSON 字符串（前端 JSON.parse 后渲染）；
    ok=False 时 result 为拒绝/异常说明（如未设置当前工程）。
    """
    if tool_name not in COVERAGE_TOOL_NAMES:
        return JSONResponse({"ok": False, "result": f"未知工具：{tool_name}（可用：{', '.join(COVERAGE_TOOL_NAMES)}）"}, 404)
    arguments: dict = {}
    if branch.strip():
        arguments["branch"] = branch.strip()
    if project_id.strip():
        arguments["project_id"] = project_id.strip()
    if batch_id.strip():
        arguments["batch_id"] = batch_id.strip()
    return exec_tool(tool_name, arguments)
