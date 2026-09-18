"""AI 设计工坊（定制化中心）域：/api/studio/*（拆分为 studio_parts/ 十一个分片，此文件为聚合入口）。"""
from routers.studio_parts.prompts import *  # noqa: F401,F403  路由注册（副作用 import）
from routers.studio_parts.skills import *  # noqa: F401,F403  路由注册（副作用 import）
from routers.studio_parts.mcp import *  # noqa: F401,F403  路由注册（副作用 import）
from routers.studio_parts.tools import *  # noqa: F401,F403  路由注册（副作用 import）
from routers.studio_parts.rules import *  # noqa: F401,F403  路由注册（副作用 import）
from routers.studio_parts.flows import *  # noqa: F401,F403  路由注册（副作用 import）
from routers.studio_parts.agents_reg import *  # noqa: F401,F403  路由注册（副作用 import）
from routers.studio_parts.hooks_copy import *  # noqa: F401,F403  路由注册（副作用 import）
from routers.studio_parts.agents import *  # noqa: F401,F403  路由注册（副作用 import）
from routers.studio_parts.a2a_events import *  # noqa: F401,F403  路由注册（副作用 import）
from routers.studio_parts.market import *  # noqa: F401,F403  路由注册（副作用 import）
from routers.studio_parts.shared import router  # noqa: F401  re-export 兼容旧引用
