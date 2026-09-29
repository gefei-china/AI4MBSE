"""报告域模型（导出 + 报告中心存储管理）。

报告中心：AI 建模产出的结构化报告（变更影响/预评审/模型分析）统一落库归档、
检索与二次导出。单一数据契约与 report_generator 保持一致：
Report = {title, sections:[{heading, body, table?}], summary, report_type}
"""
from typing import Optional

from pydantic import BaseModel


class ReportExportIn(BaseModel):
    title: str = "报告"
    sections: list = []          # [{heading, body, table?}]
    summary: str = ""
    report_type: str = "analysis"
    meta: dict = {}              # 文档控制信息 {doc_no, version, date, ...}（封面/元信息条用）
    fmt: str = "md"              # md | docx | pdf
    filename: str = ""           # 可选，覆盖下载文件名


class ReportSaveIn(BaseModel):
    title: str = "报告"
    sections: list = []
    summary: str = ""
    report_type: str = "analysis"          # analysis | impact | review | other
    source: str = "conversation"           # conversation | flow | skill | upload | manual
    conversation_id: int = 0
    branch: str = ""
    # P1-2（2026-09-28）：未指定 → 由路由层按「会话归属 → 平台默认项目」解析
    # （此前只有平台默认一档，会话已归属工程时仍会落到平台默认下）
    project_id: str = ""
    status: str = "draft"                  # draft | final
    created_by: Optional[str] = ""
