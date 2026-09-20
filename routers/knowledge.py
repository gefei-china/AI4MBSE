"""知识库域：/api/knowledge/*（拆分为 knowledge_parts/ 八个分片，此文件为聚合入口）。"""
from routers.knowledge_parts.entities import *  # noqa: F401,F403  路由注册（副作用 import）
from routers.knowledge_parts.graph import *  # noqa: F401,F403  路由注册（副作用 import）
from routers.knowledge_parts.glossary import *  # noqa: F401,F403  路由注册（副作用 import）
from routers.knowledge_parts.graph_query import *  # noqa: F401,F403  路由注册（副作用 import）
from routers.knowledge_parts.stats import *  # noqa: F401,F403  路由注册（副作用 import）
from routers.knowledge_parts.ontology import *  # noqa: F401,F403  路由注册（副作用 import）
from routers.knowledge_parts.ontology_version import *  # noqa: F401,F403  路由注册（副作用 import）
from routers.knowledge_parts.pipeline import *  # noqa: F401,F403  路由注册（副作用 import）
from routers.knowledge_parts.shared import router  # noqa: F401  re-export 兼容旧引用
