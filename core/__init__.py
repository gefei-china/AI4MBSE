"""core 包：基础设施层，零业务依赖。

- config: 统一配置（端口/DB路径/静态目录）
- audit: 跨模块公共工具（审计日志 / 行转换）
- deps:  公共 FastAPI 依赖（请求级连接等）

约定：core 内的模块不允许 import routers/services/agents 等业务层。
"""
