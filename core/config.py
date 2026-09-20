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
        # ── context_window 守卫（2026-09-20 新增；只读一次/实例，改后重启生效）───────────
        # 背景：`llm/providers/openai_compat.py` 原有一句**静默** `if mt > cw: mt = cw`，
        # 把 DB `llm_providers.context_window` 当硬上限。但实测该值可能只是**保守配置而非
        # 模型真实上限**（id=1 配 cw=8192，上游在 in=6575 + out=5841 = 12416 时仍 200 返回）
        # → 于是它**静默压低输出上限**，且现象上与「模型本来就写不长」无法区分。
        # 三档：
        #   clamp（默认）= 超窗截到 cw（= 改动前逐字节行为）+ 首次触发时 WARNING 留痕
        #   warn         = 不截断，只 WARNING（把"假天花板"暴露出来，由上游判定是否接受）
        #   off          = 完全不介入（静默）
        # 现场用法：想在不换模型的前提下放开输出上限 → 置 warn（或 off），并把
        # `refine.max_tokens` / `delegation.summary_max_tokens` 一并上调（否则那两个才是瓶颈）。
        "context_window_guard": "clamp",
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
        # ── 汇总环节输入预算（2026-09-20 新增；此前是 planner.py:54 硬编码 `[:500]`）──────
        # 背景（会话 368 实测）：汇总 LLM 每个子任务只看得到**前 500 字符**，而交付物实际
        # 1,260 / 4,000 / 4,000 字符 → 丢弃率 60.3% / 87.5% / 87.5%。质量评审据此如实判
        # 「t2 架构方案权衡在『给出三个方案』处中断」——**评审没错，是管线在丢内容**。
        # 修法三件：① 可配置（不再硬编码）；② 总预算按子任务数**公平分配**（防"子任务多则每个都看不清"）；
        # ③ 头尾采样（head 60% + tail 40%）——交付物的「清单/前言」在头、「结论/风险/方案对比」在尾，
        # 只留头会把结论整段丢掉（这正是本次门禁缺口的形态）。
        "summary_item_max_chars": 1600,  # 汇总输入：单个子任务交付物上限（字符）
        "summary_total_chars": 12000,    # 汇总输入：本轮所有子任务合计预算（字符，按数量均分并受 item 上限约束）
        "summary_floor_chars": 600,      # 汇总输入：均分后每项的保底（防止子任务多时被切到不可读）
        "summary_max_tokens": 8000,      # 汇总**输出**上限（token）。
                                         # 2026-09-20：由 3000 提到 8000 —— 会话 369 实测门禁报
                                         # 「1.3 节内容在末尾被截断」，即**输出被切**（输入侧已修好）。
                                         # 8000 是按**改动当时默认 provider 的天花板**取的：id=1 DeepSeek-V3
                                         # 的 max_tokens / context_window 当时均为 **8192**，且所有编排 Agent
                                         # 的 model_provider_id 都是 None → 全走它。
                                         # ⚠️ 2026-09-20 同日更新：id=1 的 context_window 已由 8192 **解锁为
                                         # 65536**（真实值）→「再高会被 context_window 钳制」这条**已不成立**；
                                         # 但 id=1 的 DB `max_tokens` 仍为 8192、本项仍为 8000，
                                         # 故**当前实际输出上限 = 8000（未变，本次未上调）**。
                                         # 要真正把报告写长：本项与 id=1 的 DB max_tokens 需一起抬（见配置面板）。
                                         # ⚠️ 成本口径：2026-09-17 原取 3000 是为省成本（实测 plan_summary
                                         # 平均 completion 7,470，3000 省 60%+）。本次抬高**等于放弃这笔节省**，
                                         # 因为"用户可见报告被砍半"的代价更大（脚本原本也自述"宁可少省一点"）。
                                         # 要换回来只需改这里（或走配置面板），无需动代码。
        "subtask_result_keep_chars": 20000,  # 子任务结果落库保留上限（字符；原先 task_queue 硬编码 4000）
    },
    # ── 反思闭环 RefineGate（汇总 → 评审 → 修订 → 复评）────────────────────────────
    # ⚠️ 2026-09-20 新建该组：**此前 config 里根本没有 `refine` 组** ——
    #   `workflows/refine.py` 的 `_cfg.get("refine", ...)` 一直只是返回**调用点默认值**，
    #   即 max_rounds/pass_score/enabled 与下面四个上限全是"看着可配、其实写死"。
    #   之所以必须补齐，是因为它是**用户最终看到的报告正文**（`orch_content = _ref.get("content")`）
    #   —— 会话 369 门禁报「t2/t3 各节内容未展开」，根因就在这里的四个硬上限。
    "refine": {
        "enabled": True,              # 反思闭环总开关
        "max_rounds": 2,              # 最多修订轮数（首评 + 最多 N 次修订）
        "pass_score": 70,             # 通过分（>= 即不再修订）
        "item_chars": 1200,           # 修订时**每个**子任务交付物可引用字符数（原先硬编码 400）
        "items_total_chars": 6000,    # 修订时交付物合计可引用字符数（原先硬编码 2000）
        # 2026-09-20 conv 371 实测：12000 会**裁掉报告尾部**（报告 19011 字符 → 头尾采样后
        #   只剩 12021 = 63%，第五/六/七章全被省略）→ 评审看不到结论，报
        #   「第五章、4.2 节被引用但未在可见内容中出现」（悬空引用）。
        # 该值的下界必须 ≥ **本环节自身产出**的能力，否则"输入窗口装不下自己的输出"：
        #   max_tokens=8000 实测可产出 ~23,000 字符（comp 6518 tokens → 19011 字符 ≈ 2.9 字符/token）
        #   → 取 24000 覆盖之。不变式见 verify_orch_summary_budget 的 I4c。
        "report_in_chars": 24000,     # 修订时**待修订报告**可读字符数（原先硬编码 6000，且是**只留头**）
        "max_tokens": 8000,           # 修订**输出**上限（token，原先硬编码 3000）
                                      # ⚠️ 修订输出会**整体替换**汇总报告 → 它才是报告长度的真正天花板。
                                      # 取值口径同 delegation.summary_max_tokens（改动当时 provider 天花板 8192）。
                                      # ⚠️ 2026-09-20 同日：id=1 的 context_window 已解锁为 65536，
                                      # 但本项与 DB max_tokens 仍为 8000/8192 → **实际输出上限仍是 8000**。
        # 2026-09-20 conv 372 实测：评审函数 `workflows/nodes.py::_evaluate_content` 原先硬编码
        #   `str(content)[:2000]` —— 报告长到 22,409 字符后，评审只看得到「一、需求分析」为止，
        #   于是判「t2/t3 无实质内容」，而那两节**确实存在**。
        #   这是**第 6 层截断**，也最隐蔽：报告被修得越长，评审看到的**比例**越小，
        #   gap 描述随之漂移（「1.2 节末尾」→「2.2 节之后」→「只到 2.1」），
        #   极易误判成"报告被截断"而去调**输出**上限（方向完全错）。
        "eval_in_chars": 24000,       # 评审时可读：被评审报告字符数（原先硬编码 `[:2000]` 且只留头）
    },
    "embedding": {
        "enabled": True,         # 语义出口总开关；False 强制 bigram（Mock/离线确定性）
        "intent_threshold": 0.15,  # 意图路由语义兜底阈值（**bigram 路**；dense 路见 intent_threshold_dense）
        "tool_top_k": 6,         # 工具 JIT 预筛 top_k（对齐 Semantic Tool Selection）
        "tool_threshold": 0.12,  # 工具 JIT 预筛相似度阈值（**bigram 路**；dense 路见 tool_threshold_dense）
        "memory_top_k": 5,       # 记忆检索 top_k
        "bigram_dim": 4096,      # bigram 降级向量哈希槽数（P1-1b：原 embedder 硬编码；大规模语料可调高降碰撞）
        # 2026-09-19：服务端单批条数上限（实测阿里云百炼 text-embedding-v3 = 10，>10 报
        # 400 InvalidParameter）。Embedder._embed_api 内部按此切分，兜住「batch_size=0
        # 一次全发」的调用方（semantic.py 的 items+query、intent 语义路由等）。
        "api_batch_max": 10,     # /embeddings 单批条数上限（<=0 表示不切分）
        # ── 真 embedding（dense）路独立阈值 —— 2026-09-19 标定 ─────────────────
        # 背景：修掉「单批上限 → 静默降级 bigram」后，**dense 路首次真正生效**；而上面几个阈值
        #   都是当年按 bigram 量纲标的。两路余弦量纲不同（实测同一批 top1：dense 0.51 vs bigram 0.10）。
        # 标定法：真实 query 集（query_trace ∪ messages.user，去重后 n=83）× 各场景**真实候选集**，
        #   取 top1 分做**分位等价映射** —— 使判定通过率与旧 bigram 阈值一致（**行为等价迁移**，
        #   不是最优 F1 点；要谈最优需人工标注集，本轮没有）。
        # 取证：calibrate_dense_thresholds.py → _calibrate_dense.txt / .json
        # 调用方据此选阈值：`semantic.SemanticSearch.last_backend` / `semantic.last_backend()`。
        "tool_threshold_dense": 0.53,      # 等价旧 0.12（实测正例率 47.0% → dense 同分位 0.5274）
        "intent_threshold_dense": 0.49,    # 等价旧 0.15（正例率 57.8% → 0.4926）
        # intent 分档（旧 0.40 / 0.55 / 0.70）：
        # ⚠️ 后两档在 bigram 下**从未生效**（bigram top1 max = 0.4910 < 0.55）——dense 生效后
        #   它们会首次触发，这是本轮**明确的行为变更点**，故按 dense 高分位取保守值，
        #   使其"极少触发而非永不触发"（原设计的分档从此才真正可用）。
        "intent_sem_low_dense": 0.69,      # 等价旧 0.40（正例率 4.8% → 0.6911）
        "intent_sem_mid_dense": 0.69,      # 旧 0.55 等价映射无解 → 取 dense p95（0.6907）；与 low 重合见报告
        "intent_sem_high_dense": 0.76,     # 旧 0.70 等价映射无解 → 取 dense p99（0.7604）
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
        # 2026-09-19 标定：`_match_flows` 语义补召门（词法零命中时才生效）——
        #   bigram 路沿用 0.5；dense 路用此值。实测（agent_flows 18 条候选 × 83 query）：
        #   bigram top1 p90=0.4267 / max=0.6092；0.5 的等价 dense 分位 = 0.7855（正例率 8.4%）。
        #   标定法同 embedding 组（分位等价映射）；脚本：calibrate_dense_thresholds.py
        "semantic_fallback_gate_dense": 0.79,
        "model_context_chars": 800,     # 建模上下文注入上限（字符，当前模型状态工作记忆）
        # P0（2026-09-19）：建模上下文的**既有实体**注入形态（实现在 memory.py::_build_model_context）。
        # 实况：实体原先按 branch 无条件「列名字」注入 → 把上一任务的领域素材带进本轮（会话 351 实测：
        # 电动汽车 TMS 任务被注入「巡飞弹/动力分系统」，子 Agent 因此拒绝产出代码 → 全链断裂）。
        #   count（默认）= 只报数量、不列明细（结构性隔离：告知有历史资产，但不给可挪用的素材）
        #   names        = 列具体名字（改动前行为）
        #   none         = 该项完全不注入
        # 注：曾实现「按语义相关性过滤」，标定实测 dense/bigram 两路分布重叠、**无可用阈值** → 已放弃
        # （标定脚本 tmp/kcx/calib.py；结论与理由见 memory.py 该处上方注释）。
        "model_context_entities": "count",
        "rerank": True,                 # 知识库检索后 LLM 重排（开关，粗筛→细排；行业对齐 RAG 多阶段）
        "rerank_top_n": 8,              # 重排候选数
        "rerank_keep": 3,               # 重排保留数（其余保底置后）
        # ⚠️ 2026-09-19 移除 budget_system_chars / budget_system_tokens（死配置）：
        #    实测 system 区**非检索部分**（角色/技能/模板/规则/L0 卡/记忆）= **5,991 token**，
        #    占 system_prompt 的 92.9%（同期检索段仅 458 token）；而这两个上限为
        #    4,000 token / 6,000 字符 —— **低于不可裁部分的实际值**，即使把检索段砍到 0
        #    也满足不了，属"声明了但物理上无法生效"的旋钮。且唯一可裁的检索段已有独立
        #    预算 budget_retrieval_tokens（实测只用 458 / 上限 2,600），再叠一个 system
        #    总上限只会重复挤压同一段。留着只会误导调参的人。
        #    实测依据：docs/SysML-v2-生成端硬约束与向量化链路修复-实测报告-20260919.md §5-④
        "budget_retrieval_chars": 4000, # 检索数据区上限（超限从尾部裁剪，保留最相关头部）
        "budget_history_chars": 3000,   # 历史区上限（摘要已压缩，兜底裁剪）
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
    "sysml": {
        # 2026-09-19（P0）：生成端 L0 硬约束卡，实现在 agent/pipeline_parts/v2_constraints.py。
        # 背景：_build_model_code_req 此前只有约 200 字输出格式要求、一条语法规则都没有，
        # LLM 每轮重复犯同类错（真机实测一个 156 行 TMS 模型 7 条语义错）。开启后每轮建模
        # 都把实测语法硬约束拼进 system prompt，从源头压掉高频语法/语义错。
        "l0_card_enabled": True,   # False=完全回到改动前行为（A/B 对比与故障回退用）
        "l0_card_extra": "",       # 现场追加约束文本（留空则只用内置卡；不写代码即可补规则）
        # 2026-09-19（P2）：生成后**本地校验**（checker.jar），实现在 sysml_v2_check.py。
        # 背景：L0 卡管「预防」（从源头少犯错），本开关管「暴露」——生成完立刻校验，
        # 把「语法错 / 语义错」两路计数挂到 views["check"]，并随版本链留痕
        # （element_summary.check），让错误在**入库前**可见（集成指南 §2.2 接入点②/③）。
        # 判据只认**硬错**（词法 + 语法 = n_hard）：n_hard>0 → block（待人工）；n_hard==0 → report（不阻断）。
        # 铁律：必须分三路计数——硬错会遮蔽语义错，ERROR 总数会反向上升（见 sysml_v2_check 纪律 ①）。
        "check_enabled": True,     # False=完全不调校验器（回到改动前行为；也是故障回退开关）
        "check_timeout": 90,       # 单次校验上限（秒）。实测 4~6 s；同内容命中 hash 短路则零成本
        # 2026-09-19（三路化）：诊断分「词法 / 语法 / 语义」三路计数（实现在 sysml_v2_check.classify）。
        # 分类表属**可演进的领域知识**：新增一类诊断文案不该要求改 Python。故此处可覆盖/追加：
        #   · lexical_signs / syntax_signs      —— 整体**覆盖**内置默认正则（留空=用内置）
        #   · lexical_signs_extra / syntax_signs_extra —— **追加**片段（现场补规则，不改代码）
        # 正则片段与内置默认做 `|` 合并；判序为「词法优先」（实测 ②③ 文案前缀相同，只能靠引号内容区分，
        # 详见 sysml_v2_check 模块头的三条实测依据）。
        "lexical_signs": "",             # 覆盖内置词法特征（留空=用内置）
        "syntax_signs": "",              # 覆盖内置语法特征（留空=用内置）
        "lexical_signs_extra": "",       # 追加词法特征片段
        "syntax_signs_extra": "",        # 追加语法特征片段
        # 2026-09-20：编排交付物取哪份子任务代码。多子任务各自产码时**不能全拼**——拼装体
        # 无法作为「一份模型」校验/投影（实测 run 367：3 份片段拼出 59 条错、82 节点混合树）。
        #   longest（默认）= 只取代码最长的一份（通常即主设计交付物），来源写进附录标题；
        #   all            = 保留旧行为（全部拼接，仅用于对比排查）。
        "deliver_pick": "longest",
    },
    "tool_jit": {
        # JIT 工具预筛（`agent/pipeline_parts/tools.py::_build_tools_def`）的**保底集合追加项**。
        # 背景：候选工具（≥2 个）会按用户输入语义预筛裁剪，而内置保底只有 3 个读类核心工具
        # （graph_retrieve / validate / impact_analyze）。任何「必须常驻」的新工具都得在这里登记，
        # 否则它的注入会退化成「看语义预筛的心情」——对自校验闭环而言，概率性注入等于没有。
        # 逗号分隔工具名；留空=无追加（回到改动前行为）。
        "core_keep_extra": "sysml_v2_validate",
    },
    "kb_scope": {
        # KB-S 消费范围（Agent 的 kb_scope.docs）**失效自愈**开关。
        # 实现在 agent/rag.py::_resolve_scope_docs。
        # 背景（2026-09-19 取证）：docs 白名单是**硬锁**——同时过滤三路：
        #   实体(source_doc) / 分块(doc_names) / 文档粗匹配(filename IN)。
        # 白名单里的文档名一旦不存在（重命名 / 删库 / 手配错字 / 迁移漏改），
        # 三路会**同时**归零且完全静默 —— 表现为「Agent 明明配了知识库依赖，检索却恒为空」。
        # 实况：design agent 白名单 2 条全不存在（还是重复项）→ 4887 块 SysML 规范恒不可见。
        "docs_missing_fallback": True,   # 白名单文档不存在时剔除失效项；**全部**失效 → 退化为不限文档
        "docs_missing_warn": True,       # 发生上述情形时写日志，并在检索结果回显 kb_scope_warn
    },
    "rag": {
        # P1-4（2026-09-21）：检索路由与混合检索参数配置化（此前散落硬编码，仅 rerank_enabled 已有配置）。
        # 消费点：agent/rag.py（GraphRAG 路由阈值/检索条数/图谱置信权重）、
        #         knowledge_engine.hybrid_search（RRF 常数/HyDE/置信等级/重排候选数）、
        #         services/rag_rerank.py（LLM 重排开关）。
        # 检索链路：图谱优先 → 置信不足走向量混合检索（BM25+向量 RRF 融合 + HyDE 补召）→ LLM 重排。
        "route_threshold": 0.75,     # 检索路由阈值：图谱置信度 ≥ 阈值 → 纯图路由（跳过向量检索）
        "top_k": 4,                  # 混合检索返回条数（与消费侧 chunk_hits[:3] 对齐）
        "fallback_top_k": 5,         # 混合检索异常时 search_chunks 兜底条数
        "rrf_k": 60,                 # RRF 融合常数（Σ1/(k+rank)；越大排名差异对得分影响越平缓，行业常用 60）
        "hyde_enabled": True,        # Reverse HyDE 兜底开关（chunk 假设问题匹配；用户措辞≠文档措辞时补召回）
        "hyde_weight": 0.05,         # HyDE 补充分权重（叠加进融合分，0=等效关闭）
        "w_coverage": 0.50,          # 图谱置信权重：命中实体覆盖度因子（min(命中数/5,1)）
        "w_relations": 0.30,         # 图谱置信权重：关系连接性因子（min(关系数/3,1)）
        "w_typing": 0.20,            # 图谱置信权重：类型标注完整度因子
        "confidence_high": 0.70,     # 命中置信等级「高」分界（真向量相似度口径）
        "confidence_mid": 0.45,      # 命中置信等级「中」分界
        "rerank_enabled": True,      # LLM Rerank 重排开关（LLM 不可用/超时静默回退原排序）
        "rerank_max_candidates": 8,  # 送 LLM 重排的候选上限（其余保底置后）
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
        "context_window_guard": {"type": "str", "desc": "context_window 守卫：clamp(默认，超窗截断+留痕)/warn(只告警不截断，用于放开「假天花板」)/off(不介入)"},
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
        "summary_item_max_chars":     {"type": "int", "desc": "汇总输入：单个子任务交付物上限（字符，默认 1600）"},
        "summary_total_chars":        {"type": "int", "desc": "汇总输入：本轮合计预算（字符，按子任务数均分，默认 12000）"},
        "summary_floor_chars":        {"type": "int", "desc": "汇总输入：均分后每项保底（字符，默认 600）"},
        "summary_max_tokens":         {"type": "int", "desc": "汇总输出上限（token，默认 8000；门禁报「结论前中断」时再调。注：id=1 的 context_window 已解锁为 65536，但 DB max_tokens 仍 8192）"},
        "subtask_result_keep_chars":  {"type": "int", "desc": "子任务结果落库保留上限（字符，默认 20000）"},
    },
    "refine": {
        "enabled":            {"type": "bool", "desc": "反思闭环（汇总→评审→修订→复评）总开关"},
        "max_rounds":         {"type": "int",  "desc": "最多修订轮数（默认 2）"},
        "pass_score":         {"type": "int",  "desc": "评审通过分（默认 70）"},
        "item_chars":         {"type": "int",  "desc": "修订时可引用：单个交付物字符数（默认 1200）"},
        "items_total_chars":  {"type": "int",  "desc": "修订时可引用：交付物合计字符数（默认 6000）"},
        "report_in_chars":    {"type": "int",  "desc": "修订时可读：待修订报告字符数（默认 24000，头尾采样；须 >= max_tokens 可产出的字符数）"},
        "max_tokens":         {"type": "int",  "desc": "修订输出上限（token，默认 8000；修订输出会整体替换报告 → 报告长度真正天花板）"},
        "eval_in_chars":      {"type": "int",  "desc": "评审时可读报告字符数（默认 24000，头尾采样；原先硬编码 [:2000] 只留头）"},
    },
    "embedding": {
        "enabled":          {"type": "bool", "desc": "语义出口总开关（False 强制 bigram）"},
        "intent_threshold": {"type": "float", "desc": "意图路由语义兜底阈值（bigram 路）"},
        "tool_top_k":       {"type": "int", "desc": "工具 JIT 预筛 top_k"},
        "tool_threshold":   {"type": "float", "desc": "工具 JIT 预筛相似度阈值（bigram 路；低于则空回退全量）"},
        "memory_top_k":     {"type": "int", "desc": "记忆检索 top_k"},
        "bigram_dim":       {"type": "int", "desc": "bigram 降级向量哈希槽数（默认 4096，ENV: MBSE_EMBED_BIGRAM_DIM）"},
        "api_batch_max":    {"type": "int", "desc": "/embeddings 单批条数上限（默认 10；<=0 不切分）"},
        "tool_threshold_dense":   {"type": "float", "desc": "工具 JIT 预筛阈值（dense 路；分位等价映射，实测 0.53）"},
        "intent_threshold_dense": {"type": "float", "desc": "意图路由兜底阈值（dense 路；分位等价映射，实测 0.49）"},
        "intent_sem_low_dense":   {"type": "float", "desc": "意图弱置信下限（dense 路；等价旧 0.40，实测 0.69）"},
        "intent_sem_mid_dense":   {"type": "float", "desc": "意图中等置信（dense 路；旧 0.55 在 bigram 下不可达，取 p95=0.69）"},
        "intent_sem_high_dense":  {"type": "float", "desc": "意图高置信门（dense 路；旧 0.70 在 bigram 下不可达，取 p99=0.76）"},
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
        "semantic_fallback_gate_dense": {"type": "float", "desc": "工作流语义补召门（dense 路；bigram 路固定 0.5，实测等价 0.79）"},
        "model_context_chars":     {"type": "int", "desc": "建模上下文注入上限（字符，当前模型状态工作记忆）"},
        "model_context_entities":  {"type": "str", "desc": "建模上下文既有实体注入形态：count(只报数量,默认)/names(列名字)/none"},
        "rerank":                  {"type": "bool", "desc": "知识库检索后 LLM 重排开关"},
        "rerank_top_n":            {"type": "int", "desc": "重排候选数"},
        "rerank_keep":             {"type": "int", "desc": "重排保留数（其余保底置后）"},
        # 注：原 budget_system_chars / budget_system_tokens 已于 2026-09-19 移除（死配置，见上）
        "budget_retrieval_chars":  {"type": "int", "desc": "检索数据区预算（超限尾部裁剪）"},
        "budget_history_chars":    {"type": "int", "desc": "历史区预算（摘要后兜底裁剪）"},
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
    "sysml": {
        "l0_card_enabled": {"type": "bool", "desc": "生成端 SysML v2 L0 硬约束卡注入开关（关=回到改动前行为）"},
        "l0_card_extra":   {"type": "str",  "desc": "追加到 L0 卡尾部的现场约束文本（留空用内置卡）"},
        "check_enabled":   {"type": "bool", "desc": "生成后本地校验（checker.jar）总开关（关=回到改动前行为）"},
        "check_timeout":   {"type": "int",  "desc": "单次校验上限（秒，默认 90；实测 4~6 s）"},
        "lexical_signs":       {"type": "str", "desc": "覆盖内置词法特征正则（留空=用内置）"},
        "syntax_signs":        {"type": "str", "desc": "覆盖内置语法特征正则（留空=用内置）"},
        "lexical_signs_extra": {"type": "str", "desc": "追加词法特征正则片段（现场补规则，不改代码）"},
        "syntax_signs_extra":  {"type": "str", "desc": "追加语法特征正则片段"},
        "deliver_pick":        {"type": "str", "desc": "编排交付物取码策略：longest=只取最长的一份（默认，保证可校验单模型）/ all=全部拼接（旧行为）"},
    },
    "tool_jit": {
        "core_keep_extra": {"type": "str", "desc": "JIT 工具预筛保底集合的追加项（逗号分隔工具名）"},
    },
    "kb_scope": {
        "docs_missing_fallback": {"type": "bool", "desc": "白名单文档不存在时剔除失效项；全失效则退化为不限文档"},
        "docs_missing_warn":     {"type": "bool", "desc": "白名单失效时写日志并在检索结果回显 kb_scope_warn"},
    },
    "rag": {
        "route_threshold":       {"type": "float", "desc": "检索路由阈值：图谱置信度≥阈值走纯图路由（跳过向量检索）"},
        "top_k":                 {"type": "int",   "desc": "混合检索返回条数（与消费侧 chunk_hits[:3] 对齐）"},
        "fallback_top_k":        {"type": "int",   "desc": "混合检索异常时 search_chunks 兜底条数"},
        "rrf_k":                 {"type": "int",   "desc": "RRF 融合常数（Σ1/(k+rank)，越大排名差异越平缓；行业常用 60）"},
        "hyde_enabled":          {"type": "bool",  "desc": "Reverse HyDE 兜底开关（chunk 假设问题匹配补召回）"},
        "hyde_weight":           {"type": "float", "desc": "HyDE 补充分权重（叠加进融合分，0=等效关闭）"},
        "w_coverage":            {"type": "float", "desc": "图谱置信权重：命中实体覆盖度因子"},
        "w_relations":           {"type": "float", "desc": "图谱置信权重：关系连接性因子"},
        "w_typing":              {"type": "float", "desc": "图谱置信权重：类型标注完整度因子"},
        "confidence_high":       {"type": "float", "desc": "命中置信等级「高」分界（真向量相似度口径）"},
        "confidence_mid":        {"type": "float", "desc": "命中置信等级「中」分界"},
        "rerank_enabled":        {"type": "bool",  "desc": "LLM Rerank 重排开关（失败静默回退原排序）"},
        "rerank_max_candidates": {"type": "int",   "desc": "送 LLM 重排的候选上限（其余保底置后）"},
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
