"""知识库域模型（自 routers/knowledge.py 原样搬移，P1-2 Models 外置）。"""
from typing import Optional

from pydantic import BaseModel


class EntityIn(BaseModel):
    id: str
    name: str
    entity_type: str
    properties: Optional[dict] = {}
    branch: Optional[str] = "dev"
    knowledge_category: Optional[str] = ""   # P0-3: 知识类别子类名（空=未分类）


class BatchReviewIn(BaseModel):
    entity_ids: list = []
    action: str = "confirm"  # confirm | reject


class GraphNodeIn(BaseModel):
    id: Optional[str] = None            # 缺省自动生成 N-xxx
    name: str
    entity_type: str
    properties: Optional[dict] = {}
    x: Optional[float] = 0
    y: Optional[float] = 0
    branch: Optional[str] = "dev"
    knowledge_category: Optional[str] = ""   # P0-3: 知识类别子类名（空=未分类）


class GraphEdgeIn(BaseModel):
    source_id: str
    target_id: str
    relation_type: str
    props: Optional[dict] = {}
    branch: Optional[str] = "dev"


class OntologyTypeIn(BaseModel):
    name: str
    type_kind: str                      # entity | relation | attribute
    properties: Optional[dict] = {}    # {key:{type,required,note}} 或 {key:说明}（兼容）
    constraints: Optional[dict] = {}    # {required,unique,allowed_values,cardinality,attributes}
    description: Optional[str] = ""
    icon: Optional[str] = ""
    color: Optional[str] = "#185FA5"
    parent_id: Optional[int] = None     # P0-1：父类型（subClassOf 层级，entity/attribute）
    iri: Optional[str] = ""             # P0-1：实体 IRI（空=系统按策略自动生成）
    # P1-8（2026-09-07）：外部标准映射列激活（此前 0 填充死列）
    profile_source: Optional[str] = ""  # 来源 Profile 名，如 SysML V2
    profile_ref: Optional[str] = ""     # 引用元素，如 Part::PartDefinition / ISO 15288 条款
    # P1-8：生命周期（status 白名单 draft/review/released/deprecated；空=不变更）
    status: Optional[str] = None
    replaced_by: Optional[str] = ""     # 弃用后的替代类型名
    deprecated_note: Optional[str] = "" # 弃用原因/说明


class V2GExtractIn(BaseModel):
    query: str
    chunk_ids: Optional[list] = []
    top_k: Optional[int] = 5
    doc_id: Optional[int] = None  # S1：限定在指定文档内抽取（上传后溯源指向本文档）


class V2GConfirmIn(BaseModel):
    batch_id: Optional[str] = None  # S7：可空——selected_ids 优先跨批次确认
    selected_ids: Optional[list] = []
    dup_action: Optional[str] = "create"  # P0-C 消歧前移：skip（跳过重复）/ align（对齐合并已有实体）/ create（强制新建，默认）
    triple_only: Optional[bool] = None  # 三元组唯一图写入源：True=仅生成待审三元组（不直写图库）；None=读 settings


class V2GRejectIn(BaseModel):
    candidate_ids: list = []
    reason: Optional[str] = ""  # 驳回原因（治理中心单条/批量驳回留痕）


class V2GUpdateIn(BaseModel):
    name: str
    entity_type: str = ""
    properties: str = "{}"  # 候选属性 JSON 字符串


class SysMLIn(BaseModel):
    source: str = "text"                # text | json | upload
    content: Optional[object] = ""      # SysML 文本 / 模型 JSON
    model_name: Optional[str] = ""


class MergeIn(BaseModel):
    keep_id: str
    dup_id: str


class RetrieveIn(BaseModel):
    query: str
    branch: Optional[str] = "dev"
    hybrid: Optional[bool] = False   # KB-P1: 混合检索（BM25+向量）
    top_k: Optional[int] = 5
