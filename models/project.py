"""项目域模型（自 routers/projects.py 原样搬移，P1-2 Models 外置）。"""
from typing import Optional

from pydantic import BaseModel


class ProjectIn(BaseModel):
    id: str
    name: str
    code: str
    domain: str = ""
    description: str = ""
    scenario_template_id: Optional[str] = None
    ontology_profile_id: Optional[str] = None
