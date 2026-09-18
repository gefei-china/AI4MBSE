"""用户与角色域模型（自 routers/users.py 原样搬移，P1-2 Models 外置）。"""
from typing import Optional

from pydantic import BaseModel


class RoleIn(BaseModel):
    name: str
    description: Optional[str] = ""
    permissions: Optional[dict] = {}


class UserIn(BaseModel):
    username: str
    display_name: str
    department: Optional[str] = ""
    role_id: Optional[int] = None
    workspace: Optional[str] = ""
    status: Optional[str] = "active"


class UserStatusIn(BaseModel):
    status: str  # active | disabled


class DepartmentIn(BaseModel):
    name: str
    description: Optional[str] = ""
    sort_order: Optional[int] = 0
    status: Optional[str] = "active"
