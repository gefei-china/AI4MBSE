"""元数据域模型（自 routers/meta.py 原样搬移，P1-2 Models 外置）。"""
from typing import Optional

from pydantic import BaseModel


class DocMetaIn(BaseModel):
    title: Optional[str] = ""
    author: Optional[str] = ""
    version: Optional[str] = "v1.0"
    tags: Optional[list] = []
    extra: Optional[dict] = {}
