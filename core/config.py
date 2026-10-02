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
        # ── P0-a（2026-10-02）：流式 usage 采集 ────────────────────────────────────────
        # 流式（前端主路径 `/chat/stream`）此前**完全不落 token 统计**（恒 pt=0/ct=0），
        # 导致成本、截断观测、TokenCounter 三项在主路径上全失真。现在 `_stream_wrapped`
        # 会**逐帧解析**（`llm.parse_stream_frame`）拿 usage 与 finish_reason。
        # 本开关只管"要不要主动请求 usage 帧"：
        #   false（默认）= 不主动加 `stream_options.include_usage`。**实测 DeepSeek 流式
        #                  默认就发 usage 帧**，故默认即可拿到真实值，且对上游零侵入；
        #   true          = 显式请求该字段（部分 OpenAI 兼容端点如百炼不带此帧时开启，
        #                  代价是上游若不认该字段会返回 400 → 该次流式失败）。
        "stream_include_usage": False,
        # ── P0-a 顺带接线：重试与回退（`LLMClient._chat_with_retry` / `_chat_resilient`）──
        # ⚠️ 这三项此前**只在代码里读、config 里没有定义** ⇒ `_cfg.get(..., default)`
        # 恒回落调用点默认值，属"看着可配、其实写死"（与 2026-09-20 补 `refine` 组同族）。
        # 补注册后才能在配置面板 / 系统配置文件里真正调（当前默认值 = 原调用点默认值，零行为漂移）。
        "retry_times": 2,            # 真实调用失败后的重试次数（指数退避；流式恒不重试）
        "retry_backoff_ms": 500,     # 首次重试退避毫秒（第 n 次 = backoff * 2^n）
        "fallback_provider_id": 0,   # 主 provider 重试后仍失败时的备选 provider id（0=不启用）
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
    "auth": {
        # FR-UR-1：IAM 统一身份认证（OAuth2.0 授权码，客户《接口对接.pdf》）
        "mode": "local",         # local=体验模式（免校验，无真实数据） | sso=IAM 统一身份认证
        "sso_base": "https://sso-test.chinasatnet.com.cn",
        "client_id": "",         # IDAAS 平台应用注册后生成（为空时 sso 不可用，回退 local）
        "client_secret": "",
        "redirect_uri": "http://127.0.0.1:8000/static/login.html",  # 部署时改为实际地址
        "default_role_id": 80,   # IAM 新用户默认角色（设计师；IAM 用户信息无角色字段→本地映射）
        "session_ttl_hours": 12,
        "enforce_login": False,  # 现阶段 False：不强制登录（API 兼容 X-User-Id）；上线置 True
    },
    "integration": {
        "timeout": 15,           # 通用 HTTP 工具默认调用超时（秒，工具 config 可覆盖）
        "max_response": 8000,    # 通用 HTTP 工具默认响应截断上限（字符，工具 config 可覆盖）
        "headers": {},           # 通用 HTTP 工具默认附加请求头（工具 config 可覆盖）
    },
    "delegation": {
        "max_tasks": 12,         # 单次编排最大子任务数
        "worker_timeout_s": 120, # 单个 Worker/子任务执行超时（秒，超时标记 failed）
        # ── P0-7（2026-09-30）流式编排子任务超时：由「固定 wall-clock」改为「停滞 + 硬上限」──
        # 实测（会话 514 实跑）：t1 到 182s 仍在正常吐 token，却在 241s 被固定 120s×2 次重试判
        # failed，**已生成的内容被丢弃** → 用户看到「（计划已执行，但无成功交付物）」。判据错在
        # "按经过时间"而非"按是否还在产出"。注意：旧的 `worker_timeout_s` 只作用于**非流式
        # planner 的并行分支**（planner.py）；流式路径此前是 skills.py 里的硬编码类属性，现场调不动。
        "subtask_idle_timeout_s": 240,   # P1-27：150→240（功能优先，防长推理期被误判停滞）
        "subtask_timeout_s": 600,        # P1-27：300→600（建模子任务实测 2~3 分钟，300s 余量太薄）
        # P0-7：agent_tasks 每会话保留的编排批次数（run_id 唯一化后防无界增长；只清终态旧批次）
        "keep_runs_per_conversation": 50,
        # ── ⚠️ 2026-10-02（P0 配置审计）实测：本项是**死配置**，全仓零消费点 ──────────
        # `grep -rn total_time_budget_s --include=*.py .` 只命中「本文件定义 + CONFIG_SCHEMA」
        # 两处，**没有任何编排代码读它** ⇒ 原注释「超限终止剩余任务降级汇总」是**未兑现的承诺**
        # （配置谎言：用户以为有全局兜底，实际没有）。
        # 处置（刻意**不接线、不删除**）：
        #   · 不接线 —— 实测真实编排耗时可达 849s（> 此处 600s），一旦接线会**中断正常长任务**，
        #     属于"为了让配置看起来生效而破坏功能"，与"先保证功能跑通"相悖；
        #   · 不删除 —— 键与 schema 保留，现场若确有需求可接线；删除反而会让已有配置文件里的
        #     该键变成"静默失效的未知项"。
        # 现状的等价兜底是逐子任务的 `subtask_timeout_s`（600s/个）+ `subtask_idle_timeout_s`
        # （240s 无产出），它们**都在真实生效**（P0-7 实测）。
        # 要真正启用全局预算：在编排子任务调度处加 deadline 检查，并同时把默认值改到 > 3600。
        "total_time_budget_s": 600,  # ⚠️ 当前**未接线**（死配置），见上方说明；改它不会影响行为
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
        "summary_max_tokens": 16384,     # P1-27：8000→16384（实测 max_ct=8192 已触顶被截断）。
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
        "max_tokens": 16384,           # P1-27：8000→16384（实测 plan_refine max_ct=8195 已触顶）。
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
        # 2026-09-30 会话 514 实测（第 8 层截断，方向与前几层**相反** —— 这次截断的是**输出**）：
        #   评审调用原先硬编码 `max_tokens=1024`，而当前 provider 是**思考模型**
        #   （`deepseek-v4-flash`，回包含 `reasoning_content`）→ 推理与正文**共享**这个上限。
        #   大输入下实测 `reasoning_tokens` 吃到 789/1024 → 正文 JSON 被 `finish_reason="length"`
        #   从中间截断 → 正则 `\{[\s\S]*\}` 找不到**闭合** `}` → 走硬编码兜底判 0 分「解析失败」，
        #   **把"模型其实给了分"报成"解析失败"**，并把 orchestrated_status 误降级为 partial。
        #   实测标定：reasoning 峰值 941 + 正文 JSON ≈ 350 ≈ 1300 → 取 3072 留 >2x 余量。
        "eval_max_tokens": 8192,      # P1-27：3072→8192（reasoning 峰值随输入增大；大输入下 3072 被吃光→JSON 截断）
    },
    # P1-27（2026-10-02）：会话摘要（hover 卡）。原 max_tokens **硬编码在 conv_summary.py 里为 300**，
    #   而思考模型 reasoning 与正文共享上限 ⇒ 实测 266 次调用 max_ct=301（**恒触顶**），
    #   摘要几乎必然被截断。此处配置化并放宽。
    "conv_summary": {
        "max_tokens": 4096,       # 摘要**输出**上限（token）
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
        # 2026-09-25：语义层"接住弱信号句"标定结果（脚本 tests/manual_verify/calibrate_intent_semantic.py，
        #   真实链路剖面 29 例 + 1008 组网格）。修前 4 句需 LLM 兜底；修后 2 句（且剩下的是
        #   真该走 chat 的「你好/谢谢」），准确率 27/29 → 29/29。
        # ⚠️ 关键认知：修前是「**低绝对门槛 0.49 + 严比值守卫 1.5x**」——而 dense 余弦量纲压缩
        #   （实测正确句的 top1/top2 常在 0.87/0.80 ≈ 1.08），1.5x 在 dense 下**几乎不可达**，
        #   于是弱信号句既不达 0.69 的 sem_low 墙、又过不了比值守卫 → 全部掉 LLM。
        #   标定结论是**反过来分配**：抬绝对门槛（0.49 → 0.64）、放比值守卫（1.5/1.15 → 1.05/1.05）。
        #   另有同等重要的一半：给语义索引补粗粒度示例 utterance（见 agent/intent.py
        #   `_SEMANTIC_UTTERANCES`）—— 否则 top1 会是「结构视图生成」这类子 Agent 名，
        #   答词表与意图名不一致，**阈值怎么调都错**。
        "intent_threshold_dense": 0.64,    # 0.49 → 0.64（标定：抬门槛，且仍能接住全部 5 句目标弱信号）
        "intent_sem_low_dense": 0.64,      # 0.69 → 0.64（原"低置信一律不硬检索"墙，真正卡住弱信号的就是它）
        "intent_sem_mid_dense": 0.64,      # 0.69 → 0.64（与 low 对齐：弱档判定改由 lead_w 守卫承担）
        "intent_sem_high_dense": 0.76,     # 不变（lead_w==lead_s 后该分界已不影响判定，保留以供再标定）
        # 2026-09-25：语义"领先倍率"提为配置项（原硬编码在 intent.detect_semantic 里）。
        # 原因：dense 余弦**量纲压缩**（实测同批 top1/top2 常是 0.66/0.63 这种"黏在一起"的分布），
        #   1.5x/1.15x 这类比值门槛在 dense 下远比在 bigram 下严苛 —— 不把它一起标定，
        #   光调阈值仍会把句子挡在门外（标定见 tests/manual_verify/calibrate_intent_semantic.py）。
        "intent_lead_weak": 1.05,          # 1.15 → 1.05（弱档；再紧就接不住「知识库…」1.14倍那句）
        "intent_lead_strong": 1.05,        # 1.50 → 1.05（强档；1.5x 在 dense 下不可达，是修前的第二道墙）
    },
    # ── 意图路由开关（2026-09-26 补齐）──
    # ⚠️ 此前**根本没有这个组**：`IntentRouter._cfg_get("keyword_generic", True)` 读的是
    #    `_cfg.get("intent", key, default)` → 组不存在 → 永远回落 default，
    #    于是 `eval_intent_routing.py --scored 0 / --generic 0` 这两个 A/B 开关**形同虚设**
    #    （脚本 patch 了 DEFAULT_CONFIG 也读不到，两次运行必然同结论 —— 与"没清缓存"同类的假绿）。
    #    补上组后：无文件/环境覆盖时 `_CONFIG["intent"]` 与 DEFAULT_CONFIG 同引用，patch 即生效。
    "intent": {
        "keyword_scored": True,    # 关键词层"竞争打分"（False = 回到"首个命中即 return"旧行为，A/B 用）
        "keyword_generic": True,   # 泛词是否参与打分（False = 泛词既不加分也不触发共现，A/B 用）
        "sample_collect": True,    # 真实请求是否把用户输入采集进意图样本池（设置页「意图样本」的数据来源）
        # 意图"确定不了"时是否**停下来问用户**（选择题卡，不执行）。False = 回到旧行为（自己挑一个继续）。
        # 触发面刻意收窄到"系统自己没把握"：llm_weak / fused_conflict / semantic_weak / llm<0.85 /
        # 完全无信号但像在求助；有把握的（规则命中/两路互证/高置信语义/会话继承）一律不打断。
        "confirm_when_unsure": True,
        # P0-1（2026-09-30）语义 ↔ LLM 互证：语义意图级 top1 与 LLM 结论互斥、且语义分数达到
        #   此阈值（默认取 dense `th`）→ 转澄清（route='llm_conflict'），不硬选。
        "llm_sem_conflict_min": 0.49,
        # P0-5（2026-09-30，v2）历史联合召回：低置信分支（关键词/语义均无结论）时，把**最近
        #   几轮用户原话**净化后带进 LLM 兜底。**只喂 LLM** —— 实测：拼进规则层会被历史里的
        #   "输出影响报告"以 0.95 劫持（6/6 追问全变 report_generation）；拼进语义层 5/5 返回空。
        #   ⚠️ v1 的收益叙事已被实测**收缩**：追问句带上文确实能让 LLM 判出 impact（同话题 9/9，
        #      分 0.62~0.95），但**追问句本来就能被 `inherit` 答对**（prev=impact 时意图 6/6 是
        #      impact，其中 2/6 走的正是 inherit）→ 这道门在正确场景下**不改路由方向**，
        #      只提供置信度与可观测性（详见 intent.py 的 P0-5 v2 注释）。
        #   False = 完全不取历史（回到纯单句识别；可回滚）。
        "history_recall": True,
        "history_recall_turns": 2,   # 取最近几条用户话（越大噪声越大，1~3 为宜）
        # P0-5 v2 采纳门（route=llm_history）的**地板值**：低于它不采纳。
        # ⚠️ 它**不是分离阈**，别再当分离阈调 —— 实测两侧分布重叠，不存在安全阈值：
        #   probe_gate_calib2（直连 detect_llm、未截断）同话题侧 9/9 判出 impact、分 [0.62, 0.95]；
        #   噪声侧 7/9 给出**非 chat**结论、分 [0.55, 0.90] → 分离带倒挂（0.90 > 0.62）。
        #   真正的守卫是"结论必须与 prev_intent 一致"（不可能改变路由方向，见 intent.py）。
        # 取 0.55 的理由：不低于 `inherit` 自身的置信度（否则"确认"反而会把置信度拉低）。
        #   （v1 曾取 0.80/0.75 —— 那是**截断分布**下标的，已作废，勿回退。）
        "history_llm_min": 0.55,
        # P0-5 v2：两路一致（上文 LLM 结论 == prev_intent）时，**不再弹那条"可改选"澄清细条**。
        #   这是本批唯一的行为变化，且它不改路由方向（意图恒等于 inherit 会给的那个）、
        #   只免去一次无谓的追问确认。置 False 即回到"与旧行为零差异"（仅多置信度与 meta 标记）。
        "history_confirm_silent": True,
    },
    # P1-4（2026-10-01）会话槽位（DST slots）治理：合并口径 + 入参净化。
    #   三层根因与实测见 agent/pipeline_parts/common.py 顶部注释。
    "slots": {
        "sanitize_clarify_input": True,  # L3：task_decompose 前抽取「原请求：」，剔澄清卡元对话壳
        "merge_normalize_dedup": True,   # L1：判重前归一化（全角→半角、去空白标点）——A/B 用
        "merge_substring_dedup": True,   # L1：子串包含合并（信息更全者留）——A/B 用
        "max_entities": 10,              # L2：entities 上限（本轮优先，超限丢最旧历史）
        "max_constraints": 8,            # L2：constraints 上限（同上）
        "reset_on_topic_switch": True,   # L2：换话题时丢弃历史 entities/constraints（复用 topic 判据）
        # L2 话题判据（**已标定**，见 history._is_topic_switch docstring 与
        #   tmp/mt_ctx/calib2_variants.py）：当前话题向量取「段内最近 N 条 **user** 消息」
        #   累加（词袋 ~310 稳定），阈值 0.08 落在实测分离带 (0.0350, 0.1342) 内（8/8 + 8/8）。
        #   ⚠️ 与 context.topic_sim_threshold(0.15) **不通用** —— 那个对应「全段累加」的大词袋。
        "topic_switch_threshold": 0.08,  # L2：换话题判定阈值（仅相似度 + 承接词两条件）
        "topic_ref_msgs": 10,            # L2：当前话题向量的 user 消息条数上限（稳定词袋规模）
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
        # P1-8（2026-10-01）历史（输入/输出）相关性召回收口：
        #   ① 原文段由「吃满为止」改**均分份额** —— 旧实现被最新一条长回复独占预算，
        #      实测真库 conv=514 里最新用户输入被裁到 16 tok（残句）、更早 3 条输入整条消失；
        #   ② 拉回分角色配额 —— 短文本余弦系统性偏高，混合 top-k 会被 user 侧通吃，
        #      助手此前产出的结论/版本一条都进不来（实测 conv=1 的 top-6 全是 user）；
        #   ③ 丢弃与当前输入逐字重复的拉回块（高相似度 ≠ 有信息量）。
        "history_raw_min_share_tokens": 96,   # 原文段每条的最小 token 份额（均分的下限）
        "topic_retrieve_per_role_cap": 3,     # 拉回分角色配额（user/assistant 各 ≤N，0=不限制）
        "topic_retrieve_drop_echo": True,     # 拉回块与当前输入逐字重复时丢弃
        # P1-4b（2026-09-21）标定修订：原 0.35 为经验值。标定脚本 tools/_topic_threshold_calibrate.py
        # （分位等价映射，真 embedding dim=1024，153 条真实 query）实测：**0.35 的判定通过率高达 99.3%**
        # （≈形同虚设），等价 dense 阈值为 0.6022（下界，bigram 路话题域加权未复现）。
        # 与行业经验表「有一定关联」档 0.5–0.7 一致 → 按「宁宽松勿严格」取下沿 0.50。
        # ⚠️ 标定语料候选集偏小（会话历史），方向可信、数值待真实多话题语料重标。
        "topic_retrieve_threshold_dense": 0.50,
        "topic_group_match_dense": 0.50,  # 同上标定：原 0.30 通过率 **100%**（完全失效），等价 dense 0.6023
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
        # P1-22（2026-10-02）默认**关闭**：实测证明本路（2 级上下文重排）无收益，且与 1 级
        # `rag.rerank_enabled` 串联会对同一批 hits 重排两次。依据见 rag.rerank_enabled 处注释。
        # 需要时置 true 即可开启（旋钮保留）。
        "rerank": False,                # 知识库检索后 LLM 重排（开关，粗筛→细排；行业对齐 RAG 多阶段）
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
        # ── 占比制预算（2026-09-21 P1-4b 新增；依据调研 §4.3）─────────────────────
        # 上述两个 token 预算是**绝对数**，换模型（ctx 65536 → 更大窗口）时不会自适应。
        # 行业做法是按上下文窗口占比表达（Codex `model_auto_compact_token_limit` ≈ 窗口 50%；
        # Claude Code 自动压缩 ≈ 92%、社区推荐 80%）。此处新增占比键：
        #   占比 > 0 → 用「window × ratio」，绝对值键被忽略；ratio = 0（默认）→ 回落绝对值键。
        # 与既有「token 项填 0 回退字符版」的兼容模式同构，**默认 0 即零行为漂移**。
        "budget_window_tokens": 65536,  # 占比制分母：上下文窗口基准（当前 provider id=1 实测 65536）
        "budget_retrieval_ratio": 0.0,  # 检索区占窗口比例（0=关闭，用 budget_retrieval_tokens）
        "budget_history_ratio": 0.0,    # 历史区占窗口比例（0=关闭，用 budget_history_tokens）
        # ── 会话产物摘要（P0-6，2026-09-30）────────────────────────────────────────
        # 修的是**多轮断链**：第 1 轮建好的模型（sysml_versions 版本链 / artifacts 产物）
        # 此前在后续轮次**没有任何注入通道** —— 实测会话 514 第 2 轮把"已经建好的需求模型"
        # 当成还不存在的东西从零设计（正文写着"原始需求数据仅有编号与正文是不够的"）。
        # 摘要由 agent/session_artifacts.py 只读产出，注入 4 处：流式 planner / 非流式 planner /
        # 子任务上下文快照 / 会话历史块。
        # ⚠️ **无产物会话 → 空串 → 各处完全不加段落**（提示词逐字节不变）—— 这是零回归的保证，
        #    也是验收断言 D1 的对象。三键都可回退（enabled=false 即回到改动前行为）。
        "artifact_digest_enabled": True,   # 会话产物摘要开关（false = 回退到改动前行为）
        "artifact_digest_max_chars": 900,  # 摘要字符上限（<=0 不限）
        "artifact_digest_versions": 3,     # 摘要中列出最近 N 个 SysML 模型版本
        # P1-7（2026-10-01）：标题恰为系统性兜底名（AI 生成文档/报告、{kind}产物）的产物行整行不列。
        # 判据单一真源 = core/artifact_titles.py；实测占位名占真库 artifacts 的 35.7%，
        # 且会把小摘要会话的引导语占比推到 81.8%。false = 回到改动前（占位名照列）。
        "artifact_digest_skip_placeholder_only": True,
    },
    "reasoning": {
        "direct_merge": True,        # 推理结果直接并入图库（跳过审核队列）；false=恢复「提交审核→审核队列」门禁
    },
    "memory": {
        "forget_enabled": True,      # 遗忘引擎开关
        "forget_threshold": 0.2,     # 遗忘激活度阈值（仅 forget_by_activation=true 时生效，见下）
        "consolidate_threshold": 0.85,  # 合并引擎：记忆内容相似度阈值（bigram 余弦）
        "maintain_every": 50,        # 每 N 次沉淀触发一次维护（遗忘+合并）
        # 多维作用域软重排（对齐 mem0）：「仅打破近邻，不做硬分桶」——按检索槽位序号取加成
        "scope_boost": "1.35,1.18,1.06,1.0",  # 槽位0..3 作用域优先级加成（project/user/agent/global）
        "backend": "sqlite",         # 记忆后端（sqlite=内置；预留外部实现挂载点，见 memory_backend.py）

        # ── 2026-09-29 遗忘判据重写（原激活度公式结构性永不触发，见 memory_service.forget docstring）──
        # 判据1（主）：距「最后一次被访问」超过 max_unused_days 天 → 遗忘。
        #   这是"过期经验"的真正语义：**没人再用它**。置 0 关闭该判据。
        "max_unused_days": 0,
        # 判据2：从未被访问过（access_count <= forget_min_access）且创建已超过 max_age_days 天 → 遗忘。
        #   专治"沉淀即死"的噪音：沉淀时一次性写入、此后无人检索命中的条目。
        "max_age_days": 90,
        "forget_min_access": 0,
        # 判据3（遗留通道）：按 activation 衰减阈值遗忘。默认 **关闭** ——
        #   deposit 固定写 activation=1.0、record_access 只增不减、衰减下限 0.4 > 阈值 0.2，
        #   该通道在本工程实测恒定不触发（对 118 条跑出 0 遗忘）。保留为可运维开关，勿轻易打开。
        "forget_by_activation": False,
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
        # ── 2026-09-21：OCR 兜底（扫描版 PDF / 图片）──
        # 背景：原实现对图片返回 46 字符占位串（非空 → 进向量库污染检索），
        # 扫描版 PDF 抽 0 字直接判 failed 且不给原因。接入 rapidocr-onnxruntime
        # （离线 ONNX，模型内置包内、零网络下载）后按下面的判据逐页补 OCR。
        # 实测：OCR 1.4~4.7 s/页 vs 文本层 0.01~0.06 s/页 —— 慢 50~200 倍，
        # 故**只对文本层稀疏的页**付这个成本，不可无差别全量 OCR（691 页规范全跑要 30~50 分钟）。
        "ocr_enabled": True,              # False=完全回到改动前行为（A/B 对比与故障回退用）
        "ocr_min_chars_per_page": 100,    # 页文本层字数低于此值视为"稀疏页"，该页补 OCR
        "ocr_max_pages": 50,              # 单次入库允许 OCR 的页数上限（>0；防超大扫描件拖垮入库）
        "ocr_render_scale": 2.0,          # PDF 渲染倍率（2.0 ≈ 1224x1584 px，实测识别率与耗时的平衡点）
        # ── 结果质量门禁（2026-09-21 补）────────────────────────────────────────
        # 背景：OCR 对低质截图照样能"吐出"几百字，只是**全是错字**。原实现只看
        # "有没有字"，于是乱码以 completed 入库 → 进向量库 → 被检索召回污染 RAG。
        # 阈值基线（tmp/ocr_probe/bench_score_baseline.py 实测）：
        #   正例（清晰中文图/扫描PDF/混合PDF页）均分 0.870~0.936、低置信行 **0%**
        #   反例（低质截图 doc 811）          均分 0.595~0.675、低置信行 51.9%~95.7%
        # → 取 0.70 / 50%：正例留 0.17 余量，反例两维同时被挡（冗余安全）。
        "ocr_min_avg_score": 0.70,        # 识别行平均置信度下限，低于此值判"不可用"
        "ocr_dense_min_lines": 30.0,      # 密集文本低质放行：行数下限（≥此行数且均分达 ocr_dense_min_avg 即放行）
        "ocr_dense_min_avg": 0.65,        # 密集文本低质放行：均分下限（低于常规 0.70；真乱码 0.595 类仍挡）
        "ocr_max_low_score_ratio": 50.0,  # 低置信行（<0.7）占比上限（%），超过判"不可用"
        "ocr_grayscale": True,            # 识别前转灰度：实测均分 +0.08 且更快（放大反而更差，别做）
    },
    "vision": {
        # 2026-09-21：会话图片附件的**视觉通道**（AI 建模传架构图/连线图的主路径）。
        # 背景：实测 `agent/pipeline_parts/history.py:44-77` 的附件后缀白名单**不含任何图片格式**，
        # 图片 → `text=""` → 落进 `att_skipped` → `att_blocks=[]` → prompt 里连文件名都没有
        # （`routers/conversations.py:107` 注释自认"图片…保持会话内联展示"）→ **模型从未见过图**。
        # 本段把图片按 OpenAI 多模态 content 块（image_url + base64 data URL）注入 user 消息。
        # 安全默认（铁律）：`enabled=False` 时**与改动前行为逐字节等价**；
        # 且即便 enabled=True，仍要求所选 provider 标了视觉能力
        # （`llm.provider_supports_vision`：model_type=='vision' 或 tags 含 'vision'），
        # 不具备则**明确留痕降级**（写入 attachments_info.vision），绝不静默丢弃。
        "enabled": False,          # False=不注入图片（与改动前行为等价；也是故障回退开关）
        "max_images": 2,           # 单次请求最多注入图片数（视觉 token 贵，多图会挤占上下文）
        "max_side": 1280,          # 长边像素上限（超出等比缩小后再编码；该值直接决定视觉 token 量）
        "max_bytes": 4194304,      # 单图重编码后字节上限（4MB；超过则该图跳过并留痕）
        "jpeg_quality": 85,        # 无 alpha 通道时的重编码质量（有 alpha 一律 PNG，保透明）
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
        #         knowledge_engine.hybrid_search（RRF 常数/HyDE/置信等级/重排候选数/召回宽度）、
        #         services/rag_rerank.py（LLM 重排开关）、agent/pipeline_parts/context.py（注入条数）。
        # 检索链路：图谱优先 → 置信不足走向量混合检索（BM25+向量 RRF 融合 + HyDE 补召）→ LLM 重排 → 注入。
        # P1-4b（2026-09-21）：按 docs/AI上下文配置项-依据与行业对标-20260921.md §4.1 拆分层旋钮、修倒挂。
        # 2026-09-21 标定（tools/eval/calibrate_routing.py，graph 18 正例 / neg 8 负例）：
        # 原值 0.75 对「恰好命中 2 个实体」的正例**结构上不可达**——2 实体满分恰为
        # w_cov*min(2/5,1) + w_rel*1.0 + w_typ*1.0 = 0.5*0.4 + 0.3 + 0.2 = **0.700** < 0.75，
        # 于是这类查询永远走 mixed（白付向量检索+LLM 重排 ≈1.6s；图短路路径仅 ~2ms，差 800×）。
        # 实测：阈值 ≤0.70 时正例短路 18/18、负例误短路 1/8（FPR 0.125，与 0.75 时相同）；
        # 当前 0.75 时正例仅短路 12/18。取「仍覆盖全部正例的最高阈值」0.70（阈值扫描平台上沿）。
        # ⚠️ 负例误短路那 1 例不随阈值变化（conf=0.90），属实体链接精度问题，非阈值问题。
        "route_threshold": 0.70,     # 检索路由阈值：图谱置信度 ≥ 阈值 → 纯图路由（跳过向量检索）
        # ── 检索漏斗分层（2026-09-21 P1-4b 新增）───────────────────────────────
        # 背景：此前只有 top_k 一个旋钮，它同时兼任「每路召回宽度」「RRF 融合池大小」
        #       两职（内部按 top_k*2 召回），而 rag.rerank_max_candidates=8 又大于 top_k=4
        #       → 送进 LLM 重排的候选被 top_k 卡死在 4 条，重排无选择空间（结构倒挂）。
        #       行业标准是「广召回 20-50（每路）→ 融合 20-50 → 重排 20-50 → 窄注入 3-8」，
        #       故拆成四层独立旋钮，约束：recall_k ≥ top_k ≥ rerank_max_candidates ≥ inject_k。
        "recall_k": 20,              # 每路（BM25 / 向量）召回宽度；0 = 回落旧行为 top_k*2
        "top_k": 20,                 # RRF 融合后保留的候选池（= 送 LLM 重排的池子）
        "inject_k": 3,               # 最终注入 prompt 的片段条数（消费侧 chunk_hits[:inject_k]）
        "fallback_top_k": 5,         # 混合检索异常时 search_chunks 兜底条数
        "rrf_k": 60,                 # RRF 融合常数（Σ1/(k+rank)；越大排名差异对得分影响越平缓，行业常用 60）
        "hyde_enabled": True,        # Reverse HyDE 兜底开关（chunk 假设问题匹配；用户措辞≠文档措辞时补召回）
        "hyde_weight": 0.05,         # HyDE 补充分权重（叠加进融合分，0=等效关闭）
        "w_coverage": 0.50,          # 图谱置信权重：命中实体覆盖度因子（min(命中数/5,1)）
        "w_relations": 0.30,         # 图谱置信权重：关系连接性因子（min(关系数/3,1)）
        "w_typing": 0.20,            # 图谱置信权重：类型标注完整度因子
        "confidence_high": 0.70,     # 命中置信等级「高」分界（真向量相似度口径）
        "confidence_mid": 0.45,      # 命中置信等级「中」分界
        # P1-22（2026-10-02）默认**关闭**——官方检索评测 `tools/eval/run_eval.py` 全量 doc 域
        # （20 条）A/B 实测：开重排 recall@3/@5 = 0.4、关重排同样 0.4（**召回零提升**）；
        # ndcg@10 仅 0.227 → 0.225（**+0.002，可忽略**）；但评测耗时 196.2s → 28.1s（**慢 7 倍**），
        # 且线上每轮多 2 次 LLM 往返（rag_rerank ~1775 + rerank ~810 tokens，**缓存命中恒 0**）。
        # 结论：当前语料/配置下 LLM 重排是纯成本。旋钮保留，换语料或换 provider 后可置 true 重评。
        "rerank_enabled": False,     # LLM Rerank 重排开关（LLM 不可用/超时静默回退原排序）
        "rerank_max_candidates": 10, # 送 LLM 重排的候选上限（应 ≤ top_k，否则上限形同虚设）；0 = 全部候选
        # ── P0 记忆召回（2026-09-29）───────────────────────────────────────────
        # 背景：累积层（agent_memory/project_memories）早就建好，但检索侧零消费 ——
        #       knowledge_reflow.py 沉淀的结论从未被下次检索读回，闭环缺一半。
        #       本条即补齐：把「已沉淀的经验/决策」作为**一路独立召回源**并入检索结果。
        # 定位：RAG 层管精确定位原文证据；记忆层管跨会话综合结论（对齐 Karpathy LLM Wiki 编译层）。
        # 纪律：记忆是 LLM 提炼产物，可能含幻觉 → 命中一律带「仅供对齐、不得作为事实依据引用」标注；
        #       project_memories 必须按 project_id 过滤（实测该表 project_id 大量为空且混有
        #       source='test-reg' 测试数据，全库召回会让测试数据冒充项目知识）。
        "memory_enabled": True,      # 记忆召回总开关（关 = 逐字回到「无此路」的旧行为）
        "memory_weight": 0.25,       # 记忆一路的融合权重（乘进该路得分；<1 = 让位给原文检索）
        "memory_top_k": 4,           # 记忆召回条数上限（agent_memory / project_memories 各自）
        "memory_max_chars": 200,     # 单条记忆入 prompt 的截断长度（实测存在整篇报告形态的 experience，须截断）
        # 2026-09-29：跨域兜底降权（同域有效记忆不足 top_k/2 时，全库召回补位并打此折扣）。
        # 起因：记忆按 agent_id 精确过滤，库内 chat=1/design=24/requirement_analysis=13… 分布极不均，
        # 落在稀疏域时召回恒为 0 —— 而通用方法论就躺在别的域里（"未命中"被错当"没有"）。
        # 降权（<1）保证「跨域只补位、不压主」；设 1.0 = 关闭降权但不关闭兜底，设 0 = 同关。
        "memory_cross_domain_penalty": 0.7,
        # 跨域兜底**触发门槛**：同域最高分 < 此值才跨域兜底（防"同域已够用还灌跨域噪音"）。
        # 实测标定：同域强命中≈0.42、跨域弱命中≈0.03–0.07（bigram 口径）→ 取 0.25 分档。
        "memory_cross_min_score": 0.25,
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
        "stream_include_usage": {"type": "bool", "desc": "流式请求显式带 stream_options.include_usage（默认关；DeepSeek 流式默认就发 usage 帧，仅部分兼容端点需要打开）"},
        "retry_times":          {"type": "int", "desc": "真实调用失败重试次数（指数退避；流式恒不重试）"},
        "retry_backoff_ms":     {"type": "int", "desc": "首次重试退避毫秒（第 n 次 = 该值 × 2^n）"},
        "fallback_provider_id": {"type": "int", "desc": "主 provider 重试后仍失败时的备选 provider id（0=不启用）"},
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
        "subtask_idle_timeout_s": {"type": "int", "desc": "流式编排：子任务无产出判超时（秒，默认 150）"},
        "subtask_timeout_s":      {"type": "int", "desc": "流式编排：子任务 wall-clock 硬上限（秒，默认 300）"},
        "keep_runs_per_conversation": {"type": "int", "desc": "agent_tasks 每会话保留批次数（默认 50）"},
        "total_time_budget_s": {"type": "int", "desc": "⚠️ 未接线（死配置，2026-10-02 实测全仓零消费点）：当前改此值不影响任何行为；真实兜底是 subtask_timeout_s / subtask_idle_timeout_s"},
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
        "eval_max_tokens":    {"type": "int",  "desc": "评审输出上限（token，默认 3072；原先硬编码 1024，思考模型的 reasoning 会把额度吃光导致 JSON 被截断）"},
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
        "intent_lead_weak":       {"type": "float", "desc": "语义弱档采纳的 top1/top2 领先倍率（默认 1.15；dense 余弦量纲压缩，需与阈值联合标定）"},
        "intent_lead_strong":     {"type": "float", "desc": "语义强档采纳的 top1/top2 领先倍率（默认 1.50；同上）"},
    },
    "slots": {
        "sanitize_clarify_input": {"type": "bool", "desc": "task_decompose 前抽取「原请求：」剔除澄清卡元对话壳（P1-4 L3）"},
        "merge_normalize_dedup":  {"type": "bool", "desc": "槽位判重前归一化（全角→半角、去空白标点；P1-4 L1）"},
        "merge_substring_dedup":  {"type": "bool", "desc": "槽位子串包含合并，保留信息更全者（P1-4 L1）"},
        "max_entities":           {"type": "int",  "desc": "槽位 entities 上限（本轮优先，超限丢最旧历史；P1-4 L2）"},
        "max_constraints":        {"type": "int",  "desc": "槽位 constraints 上限（同上；P1-4 L2）"},
        "reset_on_topic_switch":  {"type": "bool", "desc": "换话题时丢弃历史 entities/constraints（复用 topic 判据；P1-4 L2）"},
        "topic_switch_threshold": {"type": "float", "desc": "换话题判定阈值（仅相似度+承接词；已标定 0.08，与 context.topic_sim_threshold 不通用）"},
        "topic_ref_msgs":         {"type": "int", "desc": "当前话题向量的 user 消息条数上限（默认 10；稳定词袋规模）"},
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
        "history_raw_min_share_tokens": {"type": "int", "desc": "当前话题原文段每条最小 token 份额（均分下限，防被单条长输出吃满）"},
        "topic_retrieve_per_role_cap": {"type": "int", "desc": "语义拉回分角色配额（user/assistant 各 ≤N；0=不限制）"},
        "topic_retrieve_drop_echo": {"type": "bool", "desc": "拉回块与当前输入逐字重复时丢弃（零信息量）"},
        "topic_retrieve_threshold_dense": {"type": "float", "desc": "语义拉回阈值（真 embedding 路；已按分位等价映射标定修订为 0.5）"},
        "topic_group_match_dense": {"type": "float", "desc": "当前话题组匹配阈值（真 embedding 路；已标定修订为 0.5）"},
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
        "budget_window_tokens":    {"type": "int", "desc": "占比制分母：上下文窗口基准 token（换模型时同步）"},
        "budget_retrieval_ratio":  {"type": "float", "desc": "检索区占窗口比例（>0 覆盖绝对值键；0=关闭占比制）"},
        "budget_history_ratio":    {"type": "float", "desc": "历史区占窗口比例（>0 覆盖绝对值键；0=关闭占比制）"},
        "artifact_digest_enabled": {"type": "bool", "desc": "会话产物摘要注入开关（多轮：让后续轮次知道本会话已产出什么）"},
        "artifact_digest_max_chars": {"type": "int", "desc": "会话产物摘要字符上限（<=0 不限）"},
        "artifact_digest_versions": {"type": "int", "desc": "摘要中列出的最近 SysML 模型版本数"},
        "artifact_digest_skip_placeholder_only": {"type": "bool", "desc": "摘要过滤掉标题为系统兜底名的产物行（P1-7）"},
    },
    "reasoning": {
        "direct_merge":            {"type": "bool", "desc": "推理结果直接并入图库（跳过审核队列）；false=恢复提交审核门禁"},
    },
    "memory": {
        "forget_enabled":       {"type": "bool", "desc": "遗忘引擎开关"},
        "forget_threshold":     {"type": "float", "desc": "遗忘激活度阈值（仅 forget_by_activation=true 时生效）"},
        "consolidate_threshold": {"type": "float", "desc": "合并引擎相似度阈值（bigram 余弦）"},
        "maintain_every":       {"type": "int", "desc": "每 N 次沉淀触发一次维护"},
        "scope_boost":          {"type": "str", "desc": "作用域优先级加成（逗号分隔，槽位0..3：project/user/agent/global）"},
        "backend":              {"type": "str", "desc": "记忆后端（sqlite=内置；预留外部实现）"},
        "max_unused_days":      {"type": "int", "desc": "距今未访问超过 N 天则遗忘（0=关闭；主判据，治“没人再用”）"},
        "max_age_days":         {"type": "int", "desc": "零访问条目的存活天数上限（配合 forget_min_access 治“沉淀即死”噪音）"},
        "forget_min_access":    {"type": "int", "desc": "低于此访问次数视为“从未被用”（默认 0=只清零访问）"},
        "forget_by_activation": {"type": "bool", "desc": "启用按 activation 衰减阈值遗忘（遗留通道，本工程实测恒定不触发，默认关）"},
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
    # 2026-09-21 补注册：DEFAULT_CONFIG 里早有 extract/vision 段，但此前未进 CONFIG_SCHEMA，
    # 而 `save_override` 对未注册键直接 raise「未知配置项」→ **这两组开关在界面上根本改不了**，
    # 只能在代码/配置文件里改。补注册后 OCR 与视觉通道的开关才真正可运维（含故障回退）。
    "extract": {
        "pdf_max_pages":          {"type": "int",   "desc": "PDF 抽取页数上限（<=0 不限制，默认 1200）"},
        "ocr_enabled":            {"type": "bool",  "desc": "扫描件/图片 OCR 兜底总开关（关=回到改动前行为）"},
        "ocr_min_chars_per_page": {"type": "int",   "desc": "页文本层字数低于此值视为稀疏页并补 OCR（默认 100）"},
        "ocr_max_pages":          {"type": "int",   "desc": "单次入库允许 OCR 的页数上限（默认 50）"},
        "ocr_render_scale":       {"type": "float", "desc": "PDF 渲染倍率（默认 2.0 ≈ 1224x1584 px）"},
        "ocr_min_avg_score":      {"type": "float", "desc": "OCR 结果平均置信度下限（默认 0.70；低于此值判质量不合格，不入库）"},
        "ocr_dense_min_lines":    {"type": "float", "desc": "OCR 密集文本放行：行数下限（默认 30；行数多=真实内容结构，达线即低质放行并打标）"},
        "ocr_dense_min_avg":      {"type": "float", "desc": "OCR 密集文本放行：均分下限（默认 0.65，低于常规 0.70；放行打 degraded 标，质量分落 quality_score）"},
        "ocr_max_low_score_ratio": {"type": "float", "desc": "OCR 低置信行（<0.7）占比上限 %（默认 50；超过判质量不合格，不入库）"},
        "ocr_grayscale":          {"type": "bool",  "desc": "OCR 前转灰度（默认开；实测均分 +0.08 且更快）"},
    },
    "vision": {
        "enabled":      {"type": "bool", "desc": "会话图片附件视觉通道总开关（需所选模型标 vision 能力；默认关=回到改动前行为）"},
        "max_images":   {"type": "int",  "desc": "单次请求最多注入图片数（默认 2）"},
        "max_side":     {"type": "int",  "desc": "图片长边像素上限，超出等比缩小（默认 1280，直接决定视觉 token 量）"},
        "max_bytes":    {"type": "int",  "desc": "单图重编码后字节上限（默认 4MB，超过则该图跳过并留痕）"},
        "jpeg_quality": {"type": "int",  "desc": "无 alpha 时重编码 JPEG 质量（默认 85）"},
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
        "route_threshold":       {"type": "float", "desc": "检索路由阈值：图谱置信度≥阈值走纯图路由（跳过向量检索）。0.70=已标定（2 实体命中满分恰 0.700，故 0.75 时该类正例永不短路）"},
        "recall_k":              {"type": "int",   "desc": "每路（BM25/向量）召回宽度（0=回落 top_k*2）"},
        "top_k":                 {"type": "int",   "desc": "RRF 融合后保留的候选池（送 LLM 重排）"},
        "inject_k":              {"type": "int",   "desc": "最终注入 prompt 的片段条数"},
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
        "rerank_max_candidates": {"type": "int",   "desc": "送 LLM 重排的候选上限（应 ≤ top_k，0=全部候选）"},
        "memory_enabled":        {"type": "bool",  "desc": "P0 记忆召回开关：把已沉淀的 agent_memory/project_memories 作为一路召回源"},
        "memory_weight":         {"type": "float", "desc": "记忆一路的融合权重（乘进该路得分；<1=让位给原文检索）"},
        "memory_top_k":          {"type": "int",   "desc": "记忆召回条数上限（两路各自）"},
        "memory_max_chars":      {"type": "int",   "desc": "单条记忆入 prompt 的截断长度"},
        "memory_cross_domain_penalty": {"type": "float", "desc": "跨域兜底记忆的降权系数（同域不足时补位；1.0=不降权，0=关兜底）"},
        "memory_cross_min_score": {"type": "float", "desc": "跨域兜底触发门槛：同域最高分低于此值才跨域（防稀释榜单）"},
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
