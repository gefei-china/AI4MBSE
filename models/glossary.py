"""术语与域审核模型（自 routers/glossary.py 原样搬移，P1-2 Models 外置）。"""
from pydantic import BaseModel


class GlossaryIn(BaseModel):
    user_term: str
    canonical_term: str
    domain: str = "unknown"
    intent: str = ""
    boost: float = 1.5
    description: str = ""
    active: int = 1


class DomainReviewIn(BaseModel):
    action: str = "confirm"       # confirm | correct
    corrected_domain: str = ""    # action=correct 时指定
