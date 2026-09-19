"""系统级统一配置模块（mbse_system 配置中心）。

职责：把散落在各模块的静态/部署配置（数据库路径、监听端口、LLM、MCP、
智源平台等）统一收敛，一处定义、多处读取，避免魔数与各写各的环境变量。

来源优先级（高 → 低）：
1. 环境变量  —— 部署友好，业务代码无感覆盖（见 ENV_MAP）
2. 系统级配置文件 —— 默认 <用户目录>/.workbuddy/mbse_config.json，
   跨工程全局生效（模板见工程根 mbse_config.example.json）
3. 内置默认值 —— 本文件 DEFAULT_CONFIG

职责边界：
- 本模块只管「静态/部署配置」（启动时确定，不随界面变化）。
- 「动态配置」（运行时可通过界面调整，如 query_route / graph_confidence_threshold /
  default_project_id 等）走 settings 表，由 routers/meta.py 提供 REST 管理，
  读取入口见 database.meta_repo（get_settings）。

典型用法：
    from core import config
    base_url = config.get("zhiyuan", "base_url")
    host, port = config.HOST, config.PORT
"""
import json
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 系统级配置文件（跨工程全局）；可用环境变量 MBSE_CONFIG_PATH 覆盖
CONFIG_PATH = os.environ.get(
    "MBSE_CONFIG_PATH",
    os.path.join(os.path.expanduser("~"), ".workbuddy", "mbse_config.json"),
)

# ── 内置默认值（三层优先级的最低层）──
DEFAULT_CONFIG = {
    "app": {
        "title": "AI赋能MBSE系统设计工具",
        "version": "1.0.0",
        "host": "127.0.0.1",
        "port": 8000,
    },
    "database": {
        "path": os.path.join(BASE_DIR, "mbse.db"),   # SQLite 数据文件
        "connect_timeout": 30,                        # 连接超时（秒）
    },
    "llm": {
        "force_mock": False,     # 强制 Mock 模式（测试/无 key 环境）
        "deepseek_api_key": "",  # 预置 provider 注入用（无 key 时启动自动写入）
        "qwen_api_key": "",      # 同上
    },
    "mcp": {
        "timeout": 15,           # MCP JSON-RPC 调用超时（秒）
        "max_response": 4000,    # MCP 响应截断上限（字符）
    },
    "zhiyuan": {
        "base_url": "",          # MBSE 平台地址，如 http://mbse-platform:8080
        "token": "",             # 平台网关鉴权 token（Bearer）
        "headers": {},           # 附加请求头（网关要求其他 header 时使用）
        "timeout": 15,           # 接口调用超时（秒）
        "max_response": 8000,    # 响应截断上限（字符，防止打爆 LLM context）
    },
    "integration": {
        "timeout": 15,           # 通用 HTTP 工具默认调用超时（秒，工具 config 可覆盖）
        "max_response": 8000,    # 通用 HTTP 工具默认响应截断上限（字符，工具 config 可覆盖）
        "headers": {},           # 通用 HTTP 工具默认附加请求头（工具 config 可覆盖）
    },
    "delegation": {
        "max_tasks": 12,         # 单次编排最大子任务数
        "worker_timeout_s": 120, # 单个 Worker/子任务执行超时（秒，超时标记 failed）
        "total_time_budget_s": 600,  # 单次编排全局时间预算（秒，超限终止剩余任务降级汇总）
        "max_depth": 3,          # 委派递归深度上限
    },
    "embedding": {
        "enabled": True,         # 语义出口总开关；False 强制 bigram（Mock/离线确定性）
        "intent_threshold": 0.15,  # 意图路由语义兜底阈值（真向量/bigram 共用经验值）
        "tool_top_k": 6,         # 工具 JIT 预筛 top_k（对齐 Semantic Tool Selection）
        "tool_threshold": 0.12,  # 工具 JIT 预筛相似度阈值（低于则空回退全量注入）
        "memory_top_k": 5,       # 记忆检索 top_k
        "bigram_dim": 4096,      # bigram 降级向量哈希槽数（P1-1b：原 embedder 硬编码；大规模语料可调高降碰撞）
    },
    "context": {
        "history_immediate_turns": 6,   # 即时窗口轮数（原文逐字注入）
        "history_mid_turns": 20,        # 中期窗口轮数（LLM 摘要压缩）
        "history_msg_max_chars": 1500,  # 单条消息截断上限（字符）
        "history_summary_chars": 800,   # 中期摘要上限（字符）
        "topic_sim_threshold": 0.15,    # 话题切换相似度阈值（相邻用户消息 bigram 余弦，低于则开新话题）
        "topic_label_chars": 14,        # 话题标签长度（取段首用户消息前 N 字符）
        "topic_current_max_msgs": 8,    # 当前话题原文注入最大消息数
        "topic_boundary_keep": 2,       # 话题切换边界保留上一话题原文条数（保证切换语义连续）
        "topic_retrieve_topk": 6,       # 会话内语义拉回的历史片段条数
        "topic_retrieve_threshold": 0.15,  # 语义拉回最低相似度（低于不注入；对齐 intent 语义兜底经验值）
        "topic_retrieve_threshold_dense": 0.35,  # 语义拉回阈值（真 embedding 路独立量纲；bigram 路用 topic_retrieve_threshold）
        "topic_group_match_dense": 0.30,  # 当前话题组匹配阈值（真 embedding 路；bigram 路固定 0.12）
        "model_context_chars": 800,     # 建模上下文注入上限（字符，当前模型状态工作记忆）
        "rerank": True,                 # 知识库检索后 LLM 重排（开关，粗筛→细排；行业对齐 RAG 多阶段）
        "rerank_top_n": 8,              # 重排候选数
        "rerank_keep": 3,               # 重排保留数（其余保底置后）
        "budget_system_chars": 6000,    # P1a-2 上下文 Token 预算：system 区上限（字符，粗估 1中文字≈1.5 token）
        "budget_retrieval_chars": 4000, # 检索数据区上限（超限从尾部裁剪，保留最相关头部）
        "budget_history_chars": 3000,   # 历史区上限（摘要已压缩，兜底裁剪）
        "budget_system_tokens": 4000,   # T6 token 驱动预算：system 区上限（优先于字符版；按 tiktoken 中文≈1.5字/token 折算）
        "budget_retrieval_tokens": 2600, # T6 token 驱动预算：检索区上限（保头，保留最相关）
        "budget_history_tokens": 2000,  # T6 token 驱动预算：历史区上限（当前话题原文50%/语义拉回75%/分话题摘要剩余）
    },
    "reasoning": {
        "direct_merge": True,        # 推理结果直接并入图库（跳过审核队列）；false=恢复「提交审核→审核队列」门禁
    },
    "memory": {
        "forget_enabled": True,      # 遗忘引擎开关（激活度低于阈值软遗忘）
        "forget_threshold": 0.2,     # 遗忘激活度阈值
        "consolidate_threshold": 0.85,  # 合并引擎：记忆内容相似度阈值（bigram 余弦）
        "maintain_every": 50,        # 每 N 次沉淀触发一次维护（遗忘+合并）
    },
    "semantic_cache": {
        "enabled": False,            # 语义缓存开关（高频相似查询 embedding 命中直返）
        "threshold": 0.95,           # 命中相似度阈值
        "max_entries": 2000,         # 缓存条数上限（超出淘汰最旧）
    },
    "ingest": {
        "draft_flow": False,   # AI 建模入库发布门禁：确认后走合并请求待审/自动发布（默认关，保全现状）
        "review_source_types": ["ai_generated"],  # 需强制待审的来源（draft_flow 开启时生效）
    },
    "extract": {
        # 2026-09-19：原 knowledge_pipeline/extract.py 的 _extract_pdf 硬编码 pdf.pages[:50]，
        # 使页数多的规范类文档入库只覆盖约 17%（实测 SysML v2 官方 691 页 / KerML 454 页，
        # 全量抽取分别只需 23.6s / 17.6s，抽取本身不是瓶颈）。改为可配置并提高默认值。
        "pdf_max_pages": 1200,   # PDF 抽取页数上限（防超大文件拖垮入库；<=0 表示不限制）
    },
    "chunking": {
        "default_size": 600,         # 默认分块大小（字符，≈500-650 token 中文）
        "overlap": 90,               # 重叠（字符，15% of default_size；句子级重叠时取其整句）
        "min_chunk": 50,             # 最小块长度（字符，过滤语义碎片）
        "sentence_overlap": True,    # 句子级重叠：flush 时保留上一块末尾完整句（默认开）
        "title_enriched": True,      # 向量化拼接 文档标题+section+content（Title-Enriched Chunking）
        "table_max_rows": 15,        # markdown 表格整块保留的最大行数（超限按 10-15 行组块）
        "code_block_chunk": True,    # 代码块（``` 围栏）独立成块，不被按句切碎
        "size_by_type": {            # 按文件类型覆盖分块大小（字符）
            ".md": 800,
            ".txt": 600,
            ".pdf": 600,
            ".docx": 600,
            ".xlsx": 1000,           # 表格文档：大块保留行结构
            ".csv": 1000,
            ".pptx": 400,            # 幻灯片：页级内容短，小块更贴页
        },
    },
    "graph_db": {
        "enabled": False,            # 图数据库总开关（默认关，保全现状；开启后走 pyoxigraph 镜像）
        "backend": "pyoxigraph",     # 后端：pyoxigraph | fuseki | neo4j
        "path": os.path.join(BASE_DIR, "data", "graph_db"),   # TDB 持久化目录（pyoxigraph）
        "export_path": os.path.join(BASE_DIR, "data", "rdf_export"),  # N-Quads 导出目录
        "sync_batch": 500,           # 物化消费批量（graph_stored=0 → TDB）
        "use_memory": False,         # 内存模式（不落盘，测试用）
        "endpoint": "",              # Fuseki SPARQL 端点（如 http://localhost:3030/ds）
        "username": "",              # Fuseki 认证（可选）
        "password": "",              # Fuseki 认证（可选，secret）
        "neo4j_uri": "",             # Neo4j Bolt URI（如 bolt://localhost:7687）
        "neo4j_user": "",            # Neo4j 用户名
        "neo4j_password": "",        # Neo4j 密码（secret）
    },
}

# ── 环境变量 → 配置路径映射（部署覆盖通道，最高优先级）──
ENV_MAP = {
    "MBSE_CONFIG_PATH": None,              # 仅定位配置文件，不写入配置项
    "MBSE_DB_PATH": ("database", "path"),
    "MBSE_HOST": ("app", "host"),
    "MBSE_PORT": ("app", "port"),
    "MBSE_LLM_FORCE_MOCK": ("llm", "force_mock"),
    "MBSE_LLM_DEEPSEEK_API_KEY": ("llm", "deepseek_api_key"),
    "MBSE_LLM_QWEN_API_KEY": ("llm", "qwen_api_key"),
    "ZHIYUAN_BASE_URL": ("zhiyuan", "base_url"),
    "ZHIYUAN_API_TOKEN": ("zhiyuan", "token"),
    "MBSE_EMBED_BIGRAM_DIM": ("embedding", "bigram_dim"),
}

# 布尔型配置项（ENV 值为 "1"/"true"/"yes" 视为 True）
_BOOL_KEYS = {
    ("llm", "force_mock"),
}


def _merge(base: dict, override: dict) -> dict:
    """深合并：override 非空值覆盖 base（None/空串/空 dict 视为未设置）。"""
    out = dict(base)
    for k, v in (override or {}).items():
        if v is None:
            continue
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        elif isinstance(v, str) and not v.strip():
            continue
        elif isinstance(v, dict) and not v:
            continue
        else:
            out[k] = v
    return out


def _load_file(path: str) -> dict:
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception as e:
        print(f"[config] ⚠️ 配置文件解析失败（{path}）: {e}；已忽略，使用其余来源")
        return {}


def _load_env() -> dict:
    """从环境变量构造覆盖层（ENV_MAP 中有映射的才生效）。"""
    out = {}
    for env_var, path in ENV_MAP.items():
        if path is None or not os.environ.get(env_var):
            continue
        val = os.environ[env_var]
        if path in _BOOL_KEYS:
            val = str(val).lower() in ("1", "true", "yes", "on")
        out.setdefault(path[0], {})[path[1]] = val
    return out


# ── 生效配置（模块加载时计算，可由 reload() 刷新）──
_CONFIG: dict = {}


def _sync_compat() -> None:
    """刷新模块级兼容属性（reload 后同步，旧 import 绑定需重新导入或使用 get()）。"""
    globals()["DB_PATH"] = get("database", "path")
    globals()["HOST"] = get("app", "host")
    globals()["PORT"] = int(get("app", "port", 8000))
    globals()["APP_TITLE"] = get("app", "title")
    globals()["APP_VERSION"] = get("app", "version")
    globals()["ZHIYUAN_BASE_URL"] = get("zhiyuan", "base_url", "").rstrip("/")
    globals()["ZHIYUAN_API_TOKEN"] = get("zhiyuan", "token", "")


def reload() -> dict:
    """重新合并三层配置（环境变量 > 配置文件 > 默认值）并刷新内存/兼容属性。

    静态配置经 save_override() 落盘后调用本函数即可即时生效；
    host/port/db path 等启动级参数需重启服务完全生效。
    """
    global _CONFIG
    _CONFIG = _merge(_merge(DEFAULT_CONFIG, _load_file(CONFIG_PATH)), _load_env())
    _sync_compat()
    return _CONFIG


# ── 配置项元数据（REST 校验与 schema 接口共用的事实源）──
CONFIG_SCHEMA = {
    "app": {
        "title":       {"type": "str",  "desc": "系统标题"},
        "version":     {"type": "str",  "desc": "版本号"},
        "host":        {"type": "str",  "desc": "监听地址（需重启生效）"},
        "port":        {"type": "int",  "desc": "监听端口（需重启生效）"},
    },
    "database": {
        "path":           {"type": "str", "desc": "SQLite 数据文件路径（需重启生效）"},
        "connect_timeout": {"type": "int", "desc": "数据库连接超时（秒）"},
    },
    "llm": {
        "force_mock":      {"type": "bool",   "desc": "强制 Mock 模式（不依赖外部网络）"},
        "deepseek_api_key": {"type": "secret", "desc": "DeepSeek API key（无 key 时注入预置 provider）"},
        "qwen_api_key":     {"type": "secret", "desc": "Qwen API key"},
    },
    "mcp": {
        "timeout":      {"type": "int", "desc": "MCP JSON-RPC 调用超时（秒）"},
        "max_response": {"type": "int", "desc": "MCP 响应截断上限（字符）"},
    },
    "zhiyuan": {
        "base_url":     {"type": "url",   "desc": "智源平台地址（MBSE 平台网关）"},
        "token":        {"type": "secret", "desc": "智源平台鉴权 token（Bearer）"},
        "headers":      {"type": "dict",  "desc": "附加请求头（网关要求其他 header 时使用）"},
        "timeout":      {"type": "int",   "desc": "智源接口调用超时（秒）"},
        "max_response": {"type": "int",   "desc": "智源响应截断上限（字符）"},
    },
    "integration": {
        "timeout":      {"type": "int",   "desc": "通用 HTTP 工具默认调用超时（秒）"},
        "max_response": {"type": "int",   "desc": "通用 HTTP 工具默认响应截断上限（字符）"},
        "headers":      {"type": "dict",  "desc": "通用 HTTP 工具默认附加请求头"},
    },
    "delegation": {
        "max_tasks":           {"type": "int", "desc": "单次编排最大子任务数"},
        "worker_timeout_s":    {"type": "int", "desc": "单 Worker/子任务执行超时（秒）"},
        "total_time_budget_s": {"type": "int", "desc": "单次编排全局时间预算（秒）"},
        "max_depth":           {"type": "int", "desc": "委派递归深度上限"},
    },
    "embedding": {
        "enabled":          {"type": "bool", "desc": "语义出口总开关（False 强制 bigram）"},
        "intent_threshold": {"type": "float", "desc": "意图路由语义兜底阈值"},
        "tool_top_k":       {"type": "int", "desc": "工具 JIT 预筛 top_k"},
        "tool_threshold":   {"type": "float", "desc": "工具 JIT 预筛相似度阈值（低于则空回退全量）"},
        "memory_top_k":     {"type": "int", "desc": "记忆检索 top_k"},
        "bigram_dim":       {"type": "int", "desc": "bigram 降级向量哈希槽数（默认 4096，ENV: MBSE_EMBED_BIGRAM_DIM）"},
    },
    "context": {
        "history_immediate_turns": {"type": "int", "desc": "即时窗口轮数（原文逐字）"},
        "history_mid_turns":       {"type": "int", "desc": "中期窗口轮数（LLM 摘要压缩）"},
        "history_msg_max_chars":   {"type": "int", "desc": "单条消息截断上限（字符）"},
        "history_summary_chars":   {"type": "int", "desc": "中期摘要上限（字符）"},
        "topic_sim_threshold":     {"type": "float", "desc": "话题切换相似度阈值（相邻用户消息 bigram 余弦）"},
        "topic_label_chars":       {"type": "int", "desc": "话题标签长度（段首消息前 N 字符）"},
        "topic_current_max_msgs":  {"type": "int", "desc": "当前话题原文注入最大消息数"},
        "topic_boundary_keep":     {"type": "int", "desc": "话题切换边界保留上一话题原文条数"},
        "topic_retrieve_topk":     {"type": "int", "desc": "会话内语义拉回片段条数"},
        "topic_retrieve_threshold": {"type": "float", "desc": "语义拉回最低相似度"},
        "topic_retrieve_threshold_dense": {"type": "float", "desc": "语义拉回阈值（真 embedding 路，量纲与 bigram 不同）"},
        "topic_group_match_dense": {"type": "float", "desc": "当前话题组匹配阈值（真 embedding 路）"},
        "model_context_chars":     {"type": "int", "desc": "建模上下文注入上限（字符，当前模型状态工作记忆）"},
        "rerank":                  {"type": "bool", "desc": "知识库检索后 LLM 重排开关"},
        "rerank_top_n":            {"type": "int", "desc": "重排候选数"},
        "rerank_keep":             {"type": "int", "desc": "重排保留数（其余保底置后）"},
        "budget_system_chars":     {"type": "int", "desc": "system 区 token 预算（字符粗估）"},
        "budget_retrieval_chars":  {"type": "int", "desc": "检索数据区预算（超限尾部裁剪）"},
        "budget_history_chars":    {"type": "int", "desc": "历史区预算（摘要后兜底裁剪）"},
        "budget_system_tokens":    {"type": "int", "desc": "T6 system 区 token 预算（优先于字符版，0=回退字符版）"},
        "budget_retrieval_tokens": {"type": "int", "desc": "T6 检索区 token 预算（保头保留最相关，0=回退字符版）"},
        "budget_history_tokens":   {"type": "int", "desc": "T6 历史区 token 预算（话题原文/语义拉回/摘要分区复用，0=回退字符版）"},
    },
    "reasoning": {
        "direct_merge":            {"type": "bool", "desc": "推理结果直接并入图库（跳过审核队列）；false=恢复提交审核门禁"},
    },
    "memory": {
        "forget_enabled":       {"type": "bool", "desc": "遗忘引擎开关（激活度低于阈值软遗忘）"},
        "forget_threshold":     {"type": "float", "desc": "遗忘激活度阈值"},
        "consolidate_threshold": {"type": "float", "desc": "合并引擎相似度阈值（bigram 余弦）"},
        "maintain_every":       {"type": "int", "desc": "每 N 次沉淀触发一次维护"},
    },
    "semantic_cache": {
        "enabled":      {"type": "bool", "desc": "语义缓存开关（相似查询命中直返）"},
        "threshold":    {"type": "float", "desc": "命中相似度阈值"},
        "max_entries":  {"type": "int", "desc": "缓存条数上限（超出淘汰最旧）"},
    },
    "ingest": {
        "draft_flow":          {"type": "bool", "desc": "AI 建模入库发布门禁（确认后走合并请求待审/自动发布）"},
        "review_source_types": {"type": "str", "desc": "需强制待审的来源列表（逗号分隔，如 ai_generated）"},
    },
    "chunking": {
        "default_size":      {"type": "int",   "desc": "默认分块大小（字符，中文 ≈500-650 token）"},
        "overlap":           {"type": "int",   "desc": "重叠字符（句子级重叠时取其整句）"},
        "min_chunk":         {"type": "int",   "desc": "最小块长度（字符，过滤语义碎片）"},
        "sentence_overlap":  {"type": "bool",  "desc": "句子级重叠：flush 保留上一块末尾完整句"},
        "title_enriched":    {"type": "bool",  "desc": "向量化拼接 文档标题+section+content"},
        "table_max_rows":    {"type": "int",   "desc": "表格整块保留最大行数（超限组块）"},
        "code_block_chunk":  {"type": "bool",  "desc": "代码块独立成块（不被按句切碎）"},
        "size_by_type":      {"type": "dict",  "desc": "按文件类型覆盖分块大小（字符）"},
    },
    "graph_db": {
        "enabled":           {"type": "bool",  "desc": "图数据库总开关（默认关，保全现状）"},
        "backend":           {"type": "str",   "desc": "后端：pyoxigraph | fuseki | neo4j"},
        "path":              {"type": "str",   "desc": "TDB 持久化目录（pyoxigraph Store path）"},
        "export_path":       {"type": "str",   "desc": "N-Quads 导出目录（Jena/Fuseki 互导）"},
        "sync_batch":        {"type": "int",   "desc": "物化消费批量（graph_stored=0 → TDB）"},
        "use_memory":        {"type": "bool",  "desc": "内存模式（不落盘，测试用）"},
        "endpoint":          {"type": "str",   "desc": "Fuseki SPARQL 端点（如 http://localhost:3030/ds）"},
        "username":          {"type": "str",   "desc": "Fuseki 认证用户名（可选）"},
        "password":          {"type": "secret","desc": "Fuseki 认证密码（可选）"},
        "neo4j_uri":         {"type": "str",   "desc": "Neo4j Bolt URI（如 bolt://localhost:7687）"},
        "neo4j_user":        {"type": "str",   "desc": "Neo4j 用户名"},
        "neo4j_password":    {"type": "secret","desc": "Neo4j 密码"},
    },
}


def _coerce(meta: dict, raw) -> object:
    """按 schema 类型校验并转换（非法值抛 ValueError，供 REST 层转 400）。"""
    t = meta["type"]
    if t == "int":
        try:
            return int(str(raw).strip())
        except (TypeError, ValueError):
            raise ValueError(f"必须是整数，收到: {raw!r}")
    if t == "float":
        try:
            return float(str(raw).strip())
        except (TypeError, ValueError):
            raise ValueError(f"必须是数字，收到: {raw!r}")
    if t == "bool":
        if isinstance(raw, bool):
            return raw
        if str(raw).strip().lower() in ("1", "true", "yes", "on"):
            return True
        if str(raw).strip().lower() in ("0", "false", "no", "off", ""):
            return False
        raise ValueError(f"必须是布尔值(true/false/1/0)，收到: {raw!r}")
    if t == "dict":
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:
                raise ValueError("必须是 JSON 对象字符串或对象")
        if not isinstance(raw, dict):
            raise ValueError("必须是 JSON 对象")
        return raw
    if t == "url":
        s = str(raw).strip()
        if s and not s.startswith(("http://", "https://")):
            raise ValueError(f"必须是 http(s):// 开头的 URL，收到: {s}")
        return s
    return str(raw).strip()  # str / secret（允许空串用于清除）


def _force_merge(base: dict, override: dict) -> dict:
    """强制覆盖合并（允许空串/空 dict 清空既有值，与 _merge 语义相反）。"""
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _force_merge(out[k], v)
        else:
            out[k] = v
    return out


def _write_file(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def save_override(updates: dict) -> dict:
    """把静态配置项写入系统级配置文件（~/.workbuddy/mbse_config.json）并即时 reload。

    updates: {"zhiyuan.base_url": "http://...", "mcp.timeout": "20", ...}（点路径）
    - 校验未知键与类型；允许空值清除（token/base_url 置空）
    - 写回时保留文件内既有未知键，不覆盖
    """
    converted: dict = {}
    for dotted, raw in (updates or {}).items():
        if not isinstance(dotted, str) or "." not in dotted:
            raise ValueError(f"配置键必须是 分组.键 格式（如 zhiyuan.base_url），收到: {dotted!r}")
        section, key = dotted.split(".", 1)
        meta = CONFIG_SCHEMA.get(section, {}).get(key)
        if meta is None:
            raise ValueError(f"未知配置项: {dotted}")
        converted.setdefault(section, {})[key] = _coerce(meta, raw)
    merged = _force_merge(_load_file(CONFIG_PATH), converted)
    _write_file(CONFIG_PATH, merged)
    reload()
    return {"path": CONFIG_PATH, "updated": list((updates or {}).keys())}


def schema() -> dict:
    """配置项元数据（含当前值脱敏、来源层、默认值），供 REST schema 接口与前端渲染。"""
    out = {}
    for section, items in CONFIG_SCHEMA.items():
        out[section] = {}
        for key, meta in items.items():
            current = get(section, key)
            shown = current
            if meta["type"] in ("secret",) and current:
                shown = str(current)[:3] + "****"
            out[section][key] = {
                **meta,
                "current": shown,
                "default": DEFAULT_CONFIG.get(section, {}).get(key),
            }
    return out


# ── 动态配置（settings 表，运行时可通过界面调整）──
def runtime_settings(conn=None) -> dict:
    """读取 settings 表全部动态配置 {key: {value, description}}。"""
    try:
        own = conn is None
        if own:
            from database import get_db
            conn = get_db()
        try:
            rows = conn.execute("SELECT key, value, description FROM settings").fetchall()
            return {r["key"]: {"value": r["value"], "description": r["description"] or ""} for r in rows}
        finally:
            if own:
                conn.close()
    except Exception:
        return {}


def update_runtime_settings(updates: dict, conn=None) -> dict:
    """批量 upsert settings 动态配置（key → value）。"""
    updates = updates or {}
    own = conn is None
    if own:
        from database import get_db
        conn = get_db()
    try:
        for k, v in updates.items():
            conn.execute(
                "INSERT INTO settings (key, value, description) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP",
                (str(k), str(v), ""),
            )
        conn.commit()
        return {"updated": list(updates.keys())}
    finally:
        if own:
            conn.close()


def get(section: str, key: str | None = None, default=None):
    """按 分组[.键] 读取配置；key 省略时返回整个分组 dict。"""
    sec = _CONFIG.get(section)
    if not isinstance(sec, dict):
        return default
    if key is None:
        return sec
    return sec.get(key, default)


def as_bool(section: str, key: str, default: bool = False) -> bool:
    v = get(section, key, default)
    if isinstance(v, bool):
        return v
    return str(v).lower() in ("1", "true", "yes", "on")


def dump(mask_secrets: bool = True) -> dict:
    """调试用：输出当前生效配置；mask_secrets=True 时脱敏 token/api_key。"""
    def _mask(d: dict) -> dict:
        out = {}
        for k, v in d.items():
            if isinstance(v, dict):
                out[k] = _mask(v)
            elif k in ("token", "api_key", "deepseek_api_key", "qwen_api_key") and v:
                out[k] = (str(v)[:3] + "****")
            else:
                out[k] = v
        return out
    return _mask(_CONFIG)


# ── 兼容旧属性（既有模块无需大改即可迁移）──
DB_PATH = get("database", "path")
STATIC_DIR = os.path.join(BASE_DIR, "static")
HOST = get("app", "host")
PORT = int(get("app", "port", 8000))
APP_TITLE = get("app", "title")
APP_VERSION = get("app", "version")

# ── 智源平台快捷读取（zhiyuan_client 使用）──
ZHIYUAN_BASE_URL = get("zhiyuan", "base_url", "").rstrip("/")
ZHIYUAN_API_TOKEN = get("zhiyuan", "token", "")

# ── 模块加载时计算生效配置（reload() 依赖 get/as_bool/dump，必须放在文件末尾）──
reload()
