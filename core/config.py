"""系统级统一配置模块（mbse_system 配置中心）。

职责：把散落在各模块的静态/部署配置（数据库路径、监听端口、LLM、MCP、
智源平台等）统一收敛，一处定义、多处读取，避免魔数与各写各的环境变量。

来源优先级（高 → 低）：
1. 影子配置（MBSE_SHADOW_CONFIG）—— P1-4 沙箱专用：一次传一组候选配置，
   只活在被评估的子进程里；生产进程不设（见 _load_shadow 的 fail-loud 校验）
2. 环境变量  —— 部署友好，业务代码无感覆盖（见 ENV_MAP）
3. 系统级配置文件 —— 默认 <用户目录>/.workbuddy/mbse_config.json，
   跨工程全局生效（模板见工程根 mbse_config.example.json）
4. 内置默认值 —— 本文件 DEFAULT_CONFIG

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
    "ontology": {
        # P1-1（2026-10-15）：发布门禁档位——publish/release 双端点同读此键（单一真源）。
        # 档位语义（「最低拦截级别」）：off=不拦截 ｜ high=仅 high 拦截（默认，与历史行为等价）｜ warn=warn 及以上拦截（最严）。
        "publish_gate": "high",
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
        # ── 2026-10-09 超时治理：单次超时可配 + 总预算闸门（0=不启用）──
        #实测：180×(2+1)+退避 ≈ 541.5s（实测 545s）⇒ 用户等 9 分钟换一个异常。
        "http_timeout_s": 180,           # 单次 HTTP 超时（秒）
        "total_timeout_budget": 0,       # 一次调用含重试的总预算（秒）；0=不启用
        # reasoning 思考预算：思考与正文共享 max_tokens ⇒ 思考失控会把耗时线性拉长
        #   （实测 10-05 前思考占输出 4%，今天占 63%，慢调用占 72%）
        "thinking_budget": 0,            # 给思考的独立上限（token）；0=不启用
        "thinking_reserve_tokens": 0,    # 给正文的保底额度（token）；0=不启用
        # 思考超限**告警**阈值（token）；0=不启用。★ 只留痕不拦截（拦截用 thinking_budget）
        "thinking_warn_tokens": 0,
        # 按 intent 分流思考（L1）。★ 未命中的 intent 不发 thinking 字段（保持原行为）。
        #   数据依据见 core/config.py 的 schema desc（廉价任务思考占 58~86% 属浪费）。
        # ★ 只配 **off**（纯收益：这些任务思考占 58~86% 却只产出几十~几百 token）。
        #   **on 列表刻意留空**：给 plan_refine/plan_summary 等重产码任务"开思考"
        #   是**质量赌注**（会新增耗时与 token）⇒ 该由米爸决定，不擅自上。
        # ── 2026-10-10 新增 `chat_probe`/`chat_answer`/`chat_repair`（主对话三类轮次）──
        #   依据：**真实 A/B 实测**（2026-10-10，flashY / deepseek-v4-flash，
        #   真库 id=10035~10040 可复核），不再是推断：
        #     · 探针轮带工具：思考 16tok → 关思考 0，**tool_calls 保留**（1→1）
        #     · 难任务带工具：思考 824tok / 6.6s → 关思考 0 / **1.8s**，tool_calls 仍保留
        #       （token 省 78%、耗时省 73%）
        #     · 纯推理问答：关思考后要点齐全（part def / port 都答到），
        #       耗时 4.2s→1.4s，篇幅 287→258 字（**未见质量损失**）
        #   ⚠️ **只加 probe（探针轮）**：答案轮/修复轮的正文长、思考确有收益，
        #     保持默认（不关）—— 收益有限、风险未测。
        "thinking_policy": '{"off": ["crypto_probe", "probe", "routing", "doc_summary", '
                           '"chat_probe"], "on": []}',
        # ── 2026-10-10 形态规则层（**可维护性**：让新增 intent 零配置被覆盖）──
        # ★ 为什么需要：`_intent` 是**动态**的 —— `orchestration.py` 用 `agent_def.name`
        #   当 intent，agents 表有**27 个** active Agent（含中文名）。
        #   实测：off 名单 4 个对 27 个 Agent 名的**覆盖率 = 0/27**
        #   ⇒ 纯名单制必然漂移，漏加一个 = 思考失控（实测 99.2% / 4017秒）。
        #   ⇒ 规则按**任务形态**判定，新增 intent 天然被覆盖，不需人记得配。
        # ⚠️ **2026-10-10 已改为默认 True**（原为 off，因缺 A/B 证据）。
        #   开启依据 = **真实 A/B**（flashY / deepseek-v4-flash，真库 id=10035~10038）：
        #     · 探针轮关思考后 tool_calls **保留**（1→1）—— 唯一会让功能坏掉的风险，已排除
        #     · 难任务：思考 824→0、耗时 6.6s→1.8s、token 省 78%
        #   ⇒ 风险已用真机实验排除，故放开。
        #   ⚠️ 若将来换 provider / 换模型，**建议先重跑 verify_real_thinking_ab.py**。
        "thinking_shape_enabled": True,
        # 规则1阈值：带 tools + 上限 ≤ 此值 + **intent 名字带探针语义** ⇒ 关思考
        #   默认 1200：实测廉价决策类任务上限 ≤ 1224，重产码 ≥ 8192，分界清晰。
        #   ⚠️ **三重条件缺一不可** —— 单看 max_tokens 会误伤 `view_expansion`
        #     （实测 max_ct 仅 76，但它是重产码任务）。
        #   ⚠️ 不要调太大 —— 工具调用需要推理，长 JSON 参数的工具可能被误判。
        "shape_probe_max_tokens": 1200,
        # 规则 2 阈值：无 tools 且 max_tokens ≤ 此值 ⇒ 判定"廉价轮"⇒ 关思考；0=不启用
        "shape_cheap_max_tokens": 0,
        # ── 2026-10-10 数据自校正层（第 3 层：覆盖**任意**新增 intent）──
        # ★ 为什么需要：形态规则只认「探针语义」这一类名字。
        #   若新增一个叫「快速摘要」的 Agent（正文几十 token、思考占 80%），
        #   形态规则不命中 ⇒ 思考继续失控 ⇒ **仍然要人配**。
        #   ⇒ 这层按**真库实测数据**判定，不需要知道 intent 名字语义。
        # ⚠️ **默认关闭 + 只建议不自动改**（三条安全阀，见 llm/thinking_autotune.py）：
        #   ① 样本量 <min_samples 不判（数据不足不能定性）
        #   ② 只产出**建议清单**，写不写进配置由人决定（避免统计口径错误直接变线上行为）
        #   ③ 只 SELECT，不写库
        "autotune_enabled": False,
        # 思考占比阈值：≥ 此值且平均产出 ≤ autotune_max_output ⇒ 建议关思考。
        #   0.5 来自实测分界：廉价任务 58%~98%，重产码 0%~2.5%（中间几乎无重叠）
        "autotune_min_ratio": 0.5,
        # 平均产出上限（token）。实测廉价任务 ≤ 1224，重产码 ≥ 4336
        "autotune_max_output": 1200,
        # 样本量下限：低于此数不判（2 次调用的均值没有代表性）
        "autotune_min_samples": 5,
        "fallback_provider_id": 0,   # 主 provider 重试后仍失败时的备选 provider id（0=不启用）
        # ── P1-4（2026-10-06）熔断器：连续失败就停止打这个下游 ──
        # 与上面retry_times 的分工：retry 管**单次调用内**的瞬时抖动，
        # breaker 管**跨调用**的持续不可用（上游持续 5xx 时不再白等 3 次退避）。
        "circuit_breaker_enabled": True,   # 总开关（关=退回纯重试，无熔断）
        "circuit_breaker_fail_threshold": 5,  # 连续可重试失败 N 次 → 熔断
        "circuit_breaker_reset_sec": 60,# 熔断封锁秒数；到点后半开放一个探针
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
        # ── P0-5（2026-10-03 整改）：弱凭据通道显式开关（对标 Dify：身份仅来自会话/API Key）──
        # `X-User-Id` 是**自报身份**：任何人都能在请求头里写一个别人的 user_id 且被采信。
        # 它存在的唯一理由是兼容期（前端 migration 前 + tools/verify 自检脚本直连 API）。
        # 语义（见 core/deps.py:current_user 的三段判定）：
        #   True（默认，兼容期）= **仅在请求完全没有 X-Session-Token 时**采信 X-User-Id；
        #   False（上线形态）  = X-User-Id 一律不采信，仅会话令牌可证明身份。
        # ⚠️ 带 X-Session-Token 但令牌无效时**永远不回落** X-User-Id（与开关无关）——
        #    否则任意伪造令牌即可把自报通道重新打开（《架构评估》§4.1 D2）。
        "trust_user_id_header": True,
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
        "report_in_chars": 48000,     # 修订时**待修订报告**可读字符数（原先硬编码 6000，且是**只留头**）
                                      # 2026-10-04：24000 → 48000。**为什么必须跟着 max_tokens 一起放大**：
                                      #   P1-27 把 max_tokens 从 8000 调到 16384（实测已触顶被截断），
                                      #   但窗口没同步 ⇒ 违反 I4c「窗口 ≥ 自身产出能力」不变式
                                      #   （16384 × 2.9 ≈ 47,500 字符 > 24,000 ⇒ **报告尾部会被裁掉**，
                                      #   且评审会误判成"交付物缺失" —— 就是 I4d 当初抓到的那类假象）。
                                      #   按同一口径（2.9 字符/token）取 48000，覆盖 47,500。
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
        "eval_in_chars": 48000,       # 评审时可读：被评审报告字符数（原先硬编码 `[:2000]` 且只留头）
                                      # 2026-10-04：24000 → 48000，与 report_in_chars 同口径同步
                                      #（评审读到的必须覆盖修订产出的，否则第 6 层截断复现）
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
    # ★ 2026-10-17（米爸需求：联网获取最新消息）：web_search/web_fetch 工具的搜索 API 通道。
    #   免费抓取搜索引擎（Bing/百度/搜狗）在本网络环境实测全线反爬/风控降级（见 web_tools.py），
    #   稳定联网检索必须配正式 API：provider=tavily（国际，免费 1000 次/月）或 bocha（国内博查）。
    #   不配置时 web_search 仍可调用，但会走免费链并在不可达时**如实报告失败**（不编造）。
    "web_search": {
        "provider": "",          # tavily | bocha（空=未配置，走免费抓取链）
        "api_key": "",           # 对应 provider 的 API key
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
        # ── P0-2（2026-10-04）降级**显式化**：quota 耗尽时怎么办 ──────────────
        # 起因（实测）：embedding 供应商返回 `403 Free quota exhausted`，
        # 而 `embed_with_version` 的 except 会**静默**降级 bigram ⇒
        # 入库"成功"、界面无任何提示，而 810 个 chunk 已用词面向量污染检索。
        # 与 `llm.context_window_guard` / `llm.stats.fallback_total` 同一类问题：
        # **降级本身不是 bug，静默降级才是 bug**。
        # 三档：
        #   block（默认）= 判定入库失败（parse_status=failed + 明确原因），
        #     **不写入任何 bigram 向量** —— 宁可让用户看到"额度不足"，
        #     也不要悄悄给一份没有语义的检索（米爸 2026-10-04 决策："不降级"）。
        #   degrade = 旧行为（写入 bigram + 在 pipeline_detail 标 degraded=true），
        #     仅在明确需要"离线确定性回归"时开（如无 key 的开发机）。
        #   warn   = 写入 bigram 但不阻断，且留痕 + 监控告警。
        # 为什么默认 block 而不是 degrade：降级向量会**永久留在库里**，
        # 事后无法区分"哪些 chunk 是词面向量"，除非额外做审计（`degraded_report`）。
        # ⇒ 宁可失败重来，也不要污染。
        "on_quota_exhausted": "block",
        # 熔断时长（P1-10 原有）：识别到 403/429 且 body 含 quota/insufficient ⇒ 封锁。
        # 600s 内不再重试（重试只会每次打一个 403 + 白等网络往返）。
        "quota_block_sec": 600,
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
        # P1-31（2026-10-02）意图级缓存开关。**默认关**，依据是实测而非偏好：
        #   缓存收益 = 重复率 × 真实调用量，两项都极小 —— 真库 `messages` 近 7 天仅 **18 条**
        #   用户消息（全量 53 条），文本重复率 22% ⇒ 潜在命中约 17 次/月，折合金额 <$0.01/月；
        #   而 `llm_usage_stats` 里 `intent_detect` 的 1605 次/7 天**>95% 是评测脚本刷的**
        #   （09-30 07–08 点突发 900 次 ≈ 全周 56%），不是生产流量。
        #   留着它只增复杂度：多一张表 + 多一条失效路径（`index_fp` 随 agent 配置变化即全失效）。
        #   True = 恢复旧行为（精确文本匹配：`md5(lower(text))` + `index_fp` 双匹配）。
        #   ⚠️ 真要在多用户/高频场景启用，应先改成**语义缓存**（embedding 相似度）——
        #      精确匹配对自然语言天然近乎零命中，这正是当初"有表但 hit_count 恒 0"的设计原因。
        "cache_enabled": False,
        # 意图"确定不了"时是否**停下来问用户**（选择题卡，不执行）。False = 回到旧行为（自己挑一个继续）。
        # 触发面刻意收窄到"系统自己没把握"：llm_weak / fused_conflict / semantic_weak / llm<0.85 /
        # 完全无信号但像在求助；有把握的（规则命中/两路互证/高置信语义/会话继承）一律不打断。
        "confirm_when_unsure": True,
        # 2026-10-17（米爸反馈"澄清卡每次都是固定 4 项"）：意图确认卡的候选选项数上限。
        # 候选主序 = 本次输入的语义意图级排名（动态），不足才按 Agent 注册序兜底填满。
        # 10 = 一屏滚动内可览尽；前端选项区限高滚动，卡片不会被撑长。
        "confirm_options_max": 10,
        # ★ 2026-10-17（米爸反馈「澄清确认卡还是系统默认内容，没有基于上下文分析提供
        #   准确的确认信息」）：意图确认卡 LLM 动态化开关。开启时 1.4 触发后先让 LLM
        #   结合上下文判断——意图已明确则直接放行（连卡都不弹）；确需确认则生成针对
        #   本次场景的问题与选项（选项须映射回系统意图 key）。关闭 = 旧的固定列表。
        "confirm_llm_dynamic": True,
        # ★ 2026-10-17（米爸反馈会话 9446：用户答「取主分支最新版本数据」——对上一轮
        #   AI「您可以直接告我分支名」邀请的直接承接——却被「我不确定你想做哪件事」+
        #   固定意图清单拦住）：本会话有过「澄清→作答」记录且本轮输入不超过此长度时，
        #   判定为承接补充，两道澄清门全跳过、Agent 基于完整上下文自行理解。
        "clarify_followup_max_chars": 25,
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
        # ── B4-5（2026-10-14）：规则命中 → LLM 复核 ────────────────────────
        #   现状问题（实测）：detect() 里 `_done(..., "rule", 0.95)` 散落 **11 处**，
        #   规则命中即直返，**不进步-semantic、不进 LLM** ⇒「关键词→固定 Agent 名」映射表。
        #   本开关让规则**先给候选、再经 LLM 独立复核**（确认 / 否决 / 不可用三态，
        #   见 agent/intent.py `_llm_confirm_rule`）。
        #
        #   ⚠️ **默认 False（三态里 skipped）**：这是**延迟/成本敏感**开关。
        #      实测规则直返 0.0s、每条走 LLM 约 3.6s（244 条评测里 LLM 档还会触发重试）。
        #      ⇒ 默认关 = **线上行为与改造前逐字节一致**（零回归）；
        #      开关打开前请先用 tools/calib/eval_intent_baseline.py 做 A/B 对比。
        "rule_llm_confirm": False,
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
        # ── 检索/历史区预算：改为「按窗口比例」，绝对值仅作兜底（2026-11-11）──────
        # 米爸口径：「能不加的限制可以先不加，不要为了省 token 导致产品流程走不通」。
        # 这两个键原本是**绝对数**（2600 / 2000 token），换模型不会自适应（原注释已自承
        # "换模型时不会自适应"）。虽然实测当前只用 458 / 1403 token 尚未误伤，但：
        #   · 它们的占比（对 128K 窗口只有 2%~1.6%）低到没有实际约束力，
        #     却又在**长文档/大检索集**场景下成为硬天花板 ⇒ 典型"假约束"；
        #   · 与总闸同类问题：拿跟真实约束无关的固定值去砍用户上下文。
        # 现在口径（见 history.ctx_budget_tokens）：
        #   占比键 > 0 → 用「窗口 × ratio」；否则回退绝对值键（保持向后兼容）。
        #   默认占比：检索 15% / 历史 25%（合计 40%，给system 本体与工具留 60%）。
        "budget_retrieval_ratio": 0.15,  # 检索区占窗口比例（>0 覆盖绝对值键；0=用绝对值）
        "budget_history_ratio": 0.25,    # 历史区占窗口比例（>0 覆盖绝对值键；0=用绝对值）
        # 绝对值兜底（占比键为 0 时才用；保留以支持"刻意压小块"的调试场景）
        "budget_retrieval_tokens": 2600, # T6 token 驱动预算：检索区上限（保头，保留最相关）
        "budget_history_tokens": 2000,  # T6 token 驱动预算：历史区上限（当前话题原文50%/语义拉回75%/分话题摘要剩余）
        # 占比制的**分母**：provider 窗口取不到时用它（实测默认 provider 为 128000，
        # 这里取 65536 保守值；`ctx_budget_tokens` 会优先用 provider 真实窗口）
        "budget_window_tokens": 65536,  # 占比制分母：上下文窗口基准（provider 取不到时兜底）
        # ── 总闸上限（2026-11-11）──────────────────────────────────────────────────
        # 米爸口径：「不要为了省 token 导致产品流程走不通」。
        # 原实现硬编码兜底 10000，而实测生产全部 active provider 的 context_window
        # 都是 128000 ⇒ **12.8 倍差距**：13.7k 的正常请求被误判超窗并强行压缩
        # （conv=514 实测）。总闸只该"防撞硬上限"，不该"限制输入"。
        # 现在口径（见 history._total_budget_cap）：
        #   total_budget_tokens > 0        → 直接用（运维最高优先）
        #   否则 total_budget_window_ratio  → provider 窗口 × ratio（留余量给输出）
        #   provider 窗口取不到            → 回退 budget_window_tokens
        "total_budget_window_ratio": 0.75,  # 输入占比上限（0.75 = 75%，余 25% 给输出）
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
        # ── 冲突消解（D1，2026-10-07 接线时才补的开关）──
        # ⚠️ 判据本身在 2026-10-07 已实测收敛（存量 144 条误判 0 组，见 verify_memory_conflict_gate D5），
        #    但**开关默认关**：因为它会**改数据**（标 superseded_by），
        #    属"线上不可逆操作"，必须由你显式打开而不是默认生效。
        #    打开后：maintain() 会跑detect_conflicts 并标记"旧条被新条取代"（仅打标记，不删内容）。
        "conflict_detect_enabled": False,# 总开关（关=只做遗忘+合并，不检测冲突）
        # ⚠️⚠️ **阈值 0.5 的口径说明（2026-10-07 二次实测后诚实标注）**：
        #  这个 0.5 是**用 bigram 降级夹具**标定的（bigram 下互斥对相似度 0.7252）。
        #  但生产库实测**真向量已生效**（129/144 行是 openai-compat），
        #  而真向量口径下**存量 33 个同键对里没有一对是互斥的**，
        #  同键对的相似度全在 0.85+（重复区 0.94~0.9986）。
        #  ⇒ **0.5 这个数字在真向量口径下没有生产数据支撑**，属"能跑但未标定"。
        #  真实阈值需等生产出现第一个真实互斥对后再标定（届时开开关并观察）。
        #  现在取 0.5 的理由：宁可多报候选（互斥词已是很强的过滤器）也不漏。
        "conflict_detect_sim_high": 0.5,  # 同键候选的相似度下限（**未在真向量口径下标定**，见上）
    },
    # ── 实体归一消歧（P1-1，2026-10-11）──
    # ⚠️ 此前 entity_resolver.py 把 0.92 / 0.62 写成函数体内的**裸字面量**，既不可配也**无法关闭自动合并**；
    #    而同类阈值在 staging_fuse 里走 DB settings（0.9 / 0.7）—— 两处口径独立维护，必然漂移。
    #  独立成分组而非塞进 memory：memory 下的 conflict_detect_enabled 管的是**记忆**冲突，
    #  与这里的**实体**消歧是同名不同物，同组会造成"以为已有开关"的误判（本工程真踩过）。
    "entity_merge": {
        # auto_threshold > 1.0 即等价于关闭自动合并（沿用 staging_fuse 既有语义）。
        # 关闭时高分项**降级为待审候选**而非静默丢弃 —— 线索不丢。
        "auto_threshold": 0.92,        # ≥ 该分自动合并（默认沿用历史值）
        "flag_threshold": 0.62,        # [flag, auto) 建待审候选
        "auto_merge_enabled": True,    # 自动合并总开关（关=只建候选不合并）
    },
    "semantic_cache": {
        "enabled": False,            # 语义缓存开关（高频相似查询 embedding 命中直返）
        "threshold": 0.95,           # 命中相似度阈值
        "max_entries": 2000,         # 缓存条数上限（超出淘汰最旧）
    },
    "ingest": {
        "draft_flow": False,   # AI 建模入库发布门禁：确认后走合并请求待审/自动发布（默认关，保全现状）
        # ── L1（2026-10-10）标题路径增强 HyDE 问句（借鉴 ragflow qa.go 算法）──
        # ★ 默认关：关闭时 enhanced_hyde_questions() 返回 None ⇒ 行为与改动前逐字相同。
        "hyde_heading_enabled": False,
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
        # ── P1-3（2026-10-04）分段 OCR：把"静默截断"换成"分段处理 + 断点续跑"──
        # 旧：`ocr_max_pages: 50` = **文档级硬上限**，超出部分**静默丢弃**（用户看不出来：
        #      传 200 页扫描 PDF 会得到"解析成功"，但只索引了前 50 页）。
        # 标杆怎么做（2026-10 实测调研）：
        #   · **RAGFlow v0.25（2026-04）**：>50 页 PDF 走"分段解析 + 惰性加载"，**不截断**。
        #   · **Unstructured**：`pdf_hi_res_max_pages` 同样是"安全阀"而非产品语义，
        #     其生产建议是 `--pages-per-worker` **分批处理**（即每批都跑，不丢页）。
        #   · **MinerU**：按页切块并行，页数不设上限。
        #   ⇒ 共识：**页数不是产品语义，批大小才是**。真正要防的是"单次占用内存/时长"，
        #     靠**分段 + 续跑**解决，而不是靠"丢弃"。
        # 本仓实现（受铁律 4「零新依赖」约束，不引 Ray/paddleocr，只用既有 rapidocr-onnx）：
        #   ① `ocr_page_batch`：每批处理多少页（只影响内存峰值，不影响总页数）
        #   ② `ocr_time_budget_sec`：单文档 OCR 时间预算（默认 1800s）。超预算 ⇒
        #      **显式**记录 pages_pending + truncated，并**持久化已完成页的文本**，
        #      下次续跑从断点继续（而不是从头再跑一遍）。
        #   ③ `ocr_max_pages`：保留为**旧配置项的兼容兜底**（<=0 = 不限，默认改为 0）；
        #      若被显式设为 >0 则仍作为硬上限，但会**显式告警**而不是静默丢弃。
        "ocr_page_batch": 50,             # 每批页数（分段粒度；只影响内存峰值）
        "ocr_time_budget_sec": 1800,      # 单文档 OCR 时间预算（秒）；超预算则落盘 + 可续跑
        "ocr_max_pages": 0,               # 0=不限（推荐）。>0 才启用硬上限，且**会显式告警**
        "ocr_resume_enabled": True,       # 断点续跑（读 documents.ocr_progress）
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
        # ★ 2026-10-10 追加 `sysml_v2_autofix`：实测它**被 JIT 预筛裁掉了** ——
        #   `view_expansion` 的工具清单实测只有 6 个，**不含 autofix**。
        #   ⇒ 我上一轮修的「收尾轮保留修复工具」（`_repair_tool_subset`）
        #     与 L0 卡里「有硬错就调 autofix」都**没有前提**：
        #     模型压根看不到这个工具 ⇒ 实测 autofix 调用 **0 次**。
        #   ⇒ 修复的**第一前提**是"它得在清单里"，这才是根因。
        "core_keep_extra": "sysml_v2_validate,sysml_v2_autofix",
        # ★ 2026-10-13：把 `tools.input_schema` 真值注入 function-calling payload（开关，可回退）。
        # 背景（对标取证）：`_build_tools_def` 的 SQL **已经 SELECT 了 t.input_schema**，
        # 但 candidates 是 4 元组、没有 schema 位，`:94` 直接丢弃；随后 `:280/:290` **两个分支**
        # 都硬编码 `"properties": {}, "additionalProperties": True` ⇒
        # **模型收到的是「这工具接受任意参数」**，而生产库 `tools.input_schema` 实测 **39/41 有真值**
        # （builtin 16/18、local 9/9、coverage 5/5、zhiyuan 5/5），形同虚设。
        # 三重后果：① 模型不知道参数长什么样，靠描述里的中文自由发挥；
        #           ② 模型给非法参数也不会被拒（下游 `.get()` 拿 None）；
        #           ③ 即使模型给了越权 id/path，也没有任何一层会说"不行"。
        # ⚠️ 只改**注入**，不改**执行侧校验**（校验是独立批次 B1-9/E4-2）——
        #    本开关关掉即回到改动前行为（宽松 schema），保证可回退。
        # ⚠️ 解析失败一律降级为宽松 schema，**不得因单个脏 schema 拖垮整次工具定义构建**。
        "inject_input_schema": True,
    },
    "subtask_protocol": {
        # ── 交付协议契约是否注入子任务 query（2026-10-13，批次二 P2-2）──────────
        # 实测（2026-10-12 只读真库）：`summary_json` 的 artifacts/evidence/risks
        # **非空率 0%**（180 行全空）。根因不是校验松，而是**没有任何地方要求子 Agent
        # 输出该格式**——全仓搜 `schema_version` 只在 registry 的版本协商里出现，
        # 子任务 prompt 里没有这段契约 ⇒ 模型只能返回纯文本 ⇒ 永远走 normalize_text
        # （三数组恒空）。**要一个字段有值，得先有人要求它有值。**
        # 措辞保留"正文在前、JSON 在最后"：直接让模型只吐 JSON 会毁掉面向用户的可读交付物。
        # 关 = 回到改动前行为（纯文本，协议三数组恒空）。
        "inject_contract": True,
        # 是否把协议三数组**物化**到 subtask_artifacts（该表实测 0 行：此前只有
        # "写操作暂存"一条路径会写它，而那条路径要求 is_write_tool 命中 5 个硬编码名字，
        # 真库 10 个 write 工具里只有 5 个在白名单 ⇒ 交付类产物从无路径进表）。
        # 物化后交付物"库里有行、可查询"，汇总/审计可按 kind 聚合，不必解 JSON。
        "materialize": True,
    },
    "stream_event_log": {
        # ── 编排 SSE 关键事件落库（2026-10-13，B2-11）──────────────────────
        # 此前 stream.py 的 88 处 yield 事件**只到前端即丢**，生产库无任何编排事件表
        # ⇒ 排障时无法回放"这次编排到底走了哪些阶段、调了哪些工具、结果如何"。
        # 落点：路由层 chat_stream 的 `async for raw`（**真正唯一的收口**；生成器内部
        #   88 处分散 yield 无收口，_evq 队列只带子任务事件）。改一处 vs 改 88 处。
        # **只落 stage/tool 两类**（services/stream_event_recorder.PERSIST_TYPES）：
        #   token 与 reasoning 都是**逐字增量**，一次真实建模可达数百上千帧，
        #   全量落库会撑爆表，且排障时没人会逐字读 CoT。
        "enabled": True,               # 总开关；关 = 完全不落事件（行为与改动前一致）
        "types": "stage,tool",         # 落哪几类（逗号分隔）；改这个不会让转发丢事件
        "max_per_run": 200,            # 单次编排事件数兜底（防异常循环撑爆表）
    },
    "agent_assignment": {
        # ── 编排分派打分（2026-10-13，B1-7）────────────────────────────────
        # 定位：**择优与留痕，不是决策者**。planner LLM 已产出每个子任务的 agent，
        # 本打分器只在"候选专长匹配度并列"时按负载择一，并给每个任务留 assign_score
        # 与 load_snapshot（此前两列 0/222 有值 ⇒ "这次为什么派给它"无法回答）。
        # ⚠️ 三个权重的**标定实测**见 services/assignment.py 模块 docstring：
        #   ① 原 SYNONYM_MAP 6 组只覆盖真库 76 个 capability 词条的 9 个（12%），
        #      8 视图系列全未覆盖 ⇒ 那批任务 capability_match 恒 0，权重整项塌缩；
        #   ② embed 兜底在当前 bigram-TF 降级 embedding 下**无区分度**
        #      （实测正负样本余弦同为 0.236，都低于 0.55 阈值）；
        #   ⇒ 新增字符 bigram 重合度作主匹配手段（标定后 8 视图 top-1 命中 6/6，
        #      真配对 0.333 vs 错配对 0.0），并把成功率权重从 0.1 调到 0.2
        #      （真库 168 done / 54 failed ⇒ 该信号实测可得且区分度最明显）。
        "enabled": True,                        # 总开关；关 = 完全退回改动前行为（不算分、不改派）
        "weight_capability": 0.5,               # 专长匹配（主因，但留余量给负载纠偏）
        "weight_load": 0.3,                     # 负载
        "weight_history": 0.2,                  # 历史成功率（实测区分度最明显，双倍权重）
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
        # 2026-10-16 关系相关性门禁：图谱置信度的「关系连接性」因子只数**与 query 相关的关系**
        # （两端实体名均出现在 query 中）。False = 旧行为（全量关系计数）：
        # 任何提及系统级实名的域外查询都会拿到高置信走图谱（neg-07 实测 conf 0.9 → graph）。
        # 修复实测：neg-07 conf 0.90→0.60 误路由消除；graph 18 正例相关关系均 ≥3，置信度不变。
        "relation_relevance_gate": True,
        # 2026-10-16 低置信检索抑制（neg 域收敛）：向量兜底注入宽度内（rag.top_k 条）
        # 置信等级**全部**为「低」⇒ 向量内容整体不返回（chunk + 文档粗匹配兜底都不给 LLM）。
        # 标定：doc 43 例全低判据下 0 误伤（唯一 top1 低的 doc-27 后续条全高）；neg 8 例
        # 7 例 top5 全低（最大 vec_score 0.359）。图谱路由有独立 route_threshold 门禁、
        # 附件命中不拦。置 False 回旧行为（低置信命中照常注入）。
        "suppress_low_confidence": True,
        # ── RAGFlow 接入（L3，2026-10-10）─────────────────────────────────
        # ⚠️⚠️ 这 6 项**必须同时落在 DEFAULT_CONFIG**，
        #   只登记 CONFIG_SCHEMA 是不够的——config.get() 读 _CONFIG（由 DEFAULT_CONFIG 构建），
        #   缺这里 ⇒ get() 返回 None、as_bool 拿不到默认 ⇒ **开关调不动**（静默失效）。
        #   （实测踩过：schema 里写好了、DEFAULT 漏了，门禁当场判红。）
        "ragflow_enabled": False,        # ★ 默认关：不部署 RAGFlow 时行为逐字不变
        "ragflow_base_url": "",          # 服务地址；启用时必填
        "ragflow_api_key": "",           # API 令牌（界面 → 头像 → API 令牌）
        "ragflow_dataset": "mbse",# 目标知识库名（不存在自动建）
        "ragflow_chunk_method": "qna",   # naive / qna(★DeepDoc 模板切片) / overview
        "ragflow_embedding_model": "BAAI/bge-m3",  # ⚠️ 改了会让已入库向量口径不一致
        # ── P0-4（2026-10-04）检索 project 维度 —— ⚠️ **已冻结：不要开启** ──────────
        # 架构决策（米爸 2026-10-04 拍板）：「文档向量库、图库**不需要基于项目隔离，
        #   是统一使用的数据底座**」。图谱/文档/本体对所有项目可见，这是**设计意图**。
        #   打开本开关的**真实危害**不是"多看到别人的数据"，而是
        #   **"看不到本该可见的公共资产"**（标准构件/术语映射/通用模型被滤掉）—— 直接损害建模质量。
        #   本系统真正的隔离在 artifacts/messages，按 `conversation_id` 粒度（比项目更细）。
        #   决策声明与证据见 agent/rag.py 顶部注释块；防误开门禁见
        #   tools/verify/verify_tenant_isolation.py 的 D 组（改本行默认值即判红）。
        "tenant_isolation": "off",       # 冻结中，保持 off。客户级私有化部署若需隔离才可开
        "tenant_unresolved": "passthrough",  # 开隔离但解析不到项目时：passthrough（放行）| empty（0 命中）
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
        # P1-2（2026-10-04）**入库侧**成本闸：HyDE 向量化使 embedding 调用翻倍
        #（N 块 → 2N 次，分批数也×2；270 块 = 33+33 批）。
        # ⚠️ 实测**不要为了省成本关掉 HyDE**：切开关后 8 条真实查询里
        #    **6 条排序不同、合计 44 处排名变动**，且存在"仅开/仅关各有独占命中"
        #    ⇒ 关掉是**净损失**。所以这里给的是"可降级"的旋钮（默认 0=不限制），
        #    供 embedding 额度紧张时按需限制**前 N 块**的成本，不是"关闭收益"。
        "hyde_embed_max_chunks": 0,  # >0 只给前 N 块算 HyDE 向量；0=全部（默认）
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
        "rerank_enabled": False,         # LLM Rerank 重排开关（LLM 不可用/超时静默回退原排序）
        # ★ 重排专用 provider（2026-10-10）：实测默认 provider 可能**整体不可用**
        #   （所有 intent 一律 31.7s 超时、ct=0）⇒ 重排 100% 静默降级，
        #   表现为"开关开了但排序一字未变"，排查花了 4 轮。
        #   ⚠️ **必须在 DEFAULT_CONFIG 的 rag 组**（与 CONFIG_SCHEMA["rag"] 同组），
        #      否则 config.get("rag", ...) 读不到 ⇒ 又一次"注册了但调不动"。
        "rerank_provider_id": 0,
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
    # ── P0-2（2026-10-03 整改）：编排运行的 liveness 与孤儿回收 ──
    # 对标 LangGraph checkpointer / Temporal timeout：**长时间无任何推进的运行必须有明确结局**。
    # 本工程此前既没有 liveness 判据也没有回收动作（评估 §4.2 实测：60 个批次里 16 个
    # 残留 planned/ready/blocked，最老 4.3 天没人管，用户侧就是流水线一直转圈）。
    "runtime": {
        # 非终态任务超过该秒数没有 `updated_at` 推进 ⇒ 判定为孤儿。
        # 取值依据实测：单轮编排最长 1155 s（≈19 分钟），30 分钟留足 1.5 倍余量，
        # 既不会误杀正在长思考的批次（一次 LLM 调用超时上限 180 s + 重试，远小于此）。
        "orphan_ttl_s": 1800,
        "reap_on_startup": True,     # 启动时执行回收；排障期可置 False 临时关闭
        "reap_dry_run": False,       # True = 只报告不改写（上线灰度/排障用）
    },
    # ── P0-2 持久执行：编排检查点与断点续跑（2026-10-03）────────────────────────
    # 默认**不做自动重投**：进程起来后自动恢复所有中断批次看似美好，实则危险 ——
    #   ① 中断前执行过的写类子任务可能已落一半副作用，自动重跑 = 重复数据；
    #   ② 崩溃若是系统性的（如 provider 配额耗尽），自动重投会把剩余配额打得更干净。
    # 因此默认策略是「给入口，等人点」：`auto_resume_on_startup` 保持 False。
    # 真要打开，也必须同时把 `resume_include_failed` 保持 False（跳过已 failed 任务）。
    "orchestration": {
        "resume_stale_s": 1800,          # 心跳超过该秒数才算"失活"（编排单任务实测上限 1155 s，取 1.5 倍）
        "resume_max_attempts": 5,        # 单批次最多恢复几次，超过不再自愈（防"恢复→再崩"死循环）
        "resume_include_failed": False,  # 是否连带重跑 failed 任务（默认否：可能有副作用）
        "auto_resume_on_startup": False, # 启动自动重投（默认关，理由同上）
        # ── P0-2b（2026-10-04）：自动重投守护（core/orch_supervisor.py）──
        # 默认**关**，与 run_registry.reap_on_startup（启动必跑）刻意不同：
        #   孤儿回收只改状态、零副作用；**自动重投会真的调 LLM 烧额度**。
        # 开启前建议先跑几轮 scan_once(dry 观察) 看清它会投哪些批次。
        "auto_resume_enabled": False,      # 总开关（守护线程是否启动）
        "auto_resume_interval_s": 300,     # 扫描周期（与 alert_loop 同量级）
        "auto_resume_max_runs": 3,         # 单轮最多重投几个批次（防一次性打爆额度）
        "auto_resume_include_failed": False,  # 是否连带重跑 failed（默认否：可能有副作用）
        "checkpoint_enabled": True,      # 旁路开关：检查点写入失败永不阻断编排
    },
    # ── P0-C（2026-10-04）：异步作业队列（长任务提交即返回 job_id）──
    # 队列语义与取舍见 core/job_queue.py 顶部；此处只列运行参数。
    "jobs": {
        # 默认开：worker 只执行**用户显式提交**的作业，空转成本仅每轮一次 SELECT。
        #（与 orchestration.auto_resume_enabled 默认关刻意相反 —— 后者会自己决定重投）
        "worker_enabled": True,
        "worker_count": 1,           # SQLite 单写者：>1 会加剧写锁竞争（BaseRepo 有退避重试兜底）
        "worker_interval_s": 2,# 轮询间隔（首轮立即执行，不等）
        # 租约 = worker 领作业后多久内无人续租就判定为"崩溃残留"并回收。
        # 必须大于最长单作业耗时，否则作业会被**自己**重领并双跑。
        # 入库实测 13.4 min ⇒ 这里给 900 s；长作业另由 handler 的 LeaseKeeper 持续续租。
        "worker_lease_s": 900,
    },
    # ── P1-1（2026-10-03 整改）：入口限流（对标 Dify 配额 / Anthropic X-RateLimit-* 契约）──
    # 评估 §6 P1-1 实测：全仓 0 处限流；`budget_tokens` 曾超支 35%，后又被置 0 ⇒ 完全无闸。
    # 阈值为"远超当前真实用量、但能挡住失控"的量级，不是"刚好卡住现有业务"的量级：
    # 有效负载 `conversations` 全库 15 行，而 chat 桶给到 5 次/分钟（单次编排实测最长 1155 s），
    "rate_limit": {
        "enabled": True,             # 排障/批量入库时可临时置 False
        "chat_per_min": 5,           # 触发长编排的端点（/chat、/chat/stream、flows/run…）
        "write_per_min": 60,         # 其余写操作
        "read_per_min": 300,         # 读操作（含列表轮询）
        "exempt_paths": ["/api/health", "/api/monitor", "/api/auth",
                         "/static", "/api/ux-metrics"],   # 监控/登录/静态不得被限
    },
    # ── P0-1（2026-10-18 自进化调研整改）：资产长尾治理阈值 ──────────────
    # 背景：tools/calib/retire_view_agents.py 是一次性硬编码退役工具（点名 8 个
    #   VIEW_AGENTS + 人工 precondition），同类「谁闲置/谁持续失败」判断此后每次重写。
    # 本组把判据收敛为配置：core/lifecycle.py 读取，GET /api/monitor/lifecycle 呈现，
    # 只建议不自动执行（退役动作仍走人工确认）。
    "lifecycle": {
        "enabled": True,             # 总开关（False 时分析端点返回空结果而非 500）
        "idle_days": 30,             # 退出线：超过 N 天无使用记录 ⇒ 闲置
        "min_uses": 5,               # 退出线：窗口内使用次数下限（低于视为没人用）
        "fail_rate_pct": 30,         # 退出线：失败率阈值（%，足量样本下持续失败 ⇒ 质量退出）
        "watch_idle_days": 15,       # 观察线：闲置天数（达到即 watch，未到 keep）
        "watch_fail_rate_pct": 15,   # 观察线：失败率（%，达到即 watch）
    },
    # ── P0-2（2026-10-18 自进化调研整改）：资产变更审计 ──────────────
    # 背景：四类资产（agents/prompts/skills/agent_flows）的更新此前无字段级 diff、
    #   无版本跃迁记录 ⇒ 「上次改了什么、上一版长什么样」不可考，自进化失去评估基线。
    # 本组控制 core/asset_audit.py 的审计行为；审计表 asset_change_logs 由
    # database/migrations/columns.py 幂等创建。
    "asset_audit": {
        "enabled": True,             # 总开关（False 时仍正常更新，只是不写审计行）
        "auto_bump_version": True,   # 调用方未显式给版本号时自动递升（v1→v2 / v1.0→v1.1）
        "max_value_chars": 4000,     # 单字段审计值截断上限（system_prompt/content 可能几十 KB）
    },
    # ── P0-3（2026-10-18 自进化调研整改）：Bad Case 收集 ──────────────
    # 背景：LLM 失败/截断、用户负向反馈散落无汇总，自进化选不出「下一轮改什么」。
    # 本组控制 core/badcase.py 的判据窗口与入库形态；收集默认 dry-run（人工确认后
    # 才 apply），与 lifecycle「只建议不自动执行」同一纪律。
    "badcase": {
        "enabled": True,             # 总开关（False 时收集端点返空而非 500）
        "window_days": 30,           # 判据窗口：只扫最近 N 天的坏样本（避免全表扫）
        "include_probes": False,     # conversation_id=0 且 run_id=0 的探测调用是否算坏样本
        "max_detail_chars": 2000,    # 单条 evidence 截断上限（failure_msg 可能很长）
        "reopen_on_recur": True,     # 已 triage 的主题复现时重开（否则告警闭环对复发失明）
        "alert_threshold": 5,        # 默认告警线（open 数 ≥N 触发；也可在 alert_rules 自建规则）
    },
    # ── P1-1（2026-10-18 自进化调研路线图）：Trace2Dataset ────────────
    # 背景：生产轨迹（会话/HIL 裁决/意图样本/编排运行/Bad Case 台账）此前没有
    #   任何通路变成可复用评测样本 —— 飞轮不接回管道就是数据坟墓（Airbnb AITL /
    #   NVIDIA MAPE 同款失败模式）。AgentLoop 闭环的供料环。
    # 本组控制 tools/eval/trace2dataset.py 的取材口径；只读取材，绝不写库。
    "trace2dataset": {
        "enabled": True,             # 总开关（False 时 build 返空而非报错）
        "window_days": 90,           # 取材窗口：只取最近 N 天的证据（评测集要新）
        "max_per_extractor": 300,    # 单抽取器样本上限（防单一证据源淹掉全集）
        "exclude_mock": True,        # 排除 Mock 降级回答（Mock 不是系统能力证据）
        "mock_markers": "（Mock 回答）,Mock 降级模式",  # Mock 标记（逗号分隔，命中即排除）
        "min_query_chars": 4,        # query 最短长度（"嗯"/"好"不构成评测样本）
        "max_response_chars": 600,   # response 截断上限（回归集只需可读开头）
    },
    # ── P1-2（2026-10-18 自进化调研路线图）：步骤级归因器 ──────────────
    # 背景：AgentEvolver self-attributing 的最小版——error_profile 只到会话级
    #   计数，回答不了「哪一步开始坏的」。步骤原料（llm_usage_stats/
    #   tool_call_logs/flow_run_steps 的失败信号）早已落库，缺的是归因器。
    # 本组控制 core/attribution.py 的取材/聚合口径；只读取材，绝不写库。
    "attribution": {
        "enabled": True,             # 总开关（False 时 patterns 返空不报错）
        "window_days": 90,           # patterns 聚合窗口：只归因最近 N 天的首坏步
        "min_occurrences": 2,        # 模式入榜下限：出现次数 ≥N 才算模式（防一次性噪声）
        "max_patterns": 50,          # 模式条数上限（防看板被长尾淹掉）
        "max_steps": 500,            # 单会话步骤上限（真库实测最大 324；超限保留最早的）
        "session_scan_limit": 200,   # patterns 扫描会话数上限（最近活跃优先）
    },
    # ── P1-3（2026-10-18 自进化调研路线图）：配置提案器 ──────────────
    # 背景：RefineGate 只评审「内容」；P1-2 聚出首坏步模式后，没有任何消费者
    #   把模式变成「该改什么」的建议——诊断环到行动环之间断了一环。GEPA 式
    #   反思改写补上这一环，且守住铁律：只提议，不自动应用（自动改生产配置
    #   是 misevolution 直通道；应用由人在设置页改配置，本链路零配置写路径）。
    # 本组控制 core/config_proposal.py 的提议口径。
    "config_proposal": {
        "enabled": True,             # 总开关（False 时 generate 返空不报错）
        "min_occurrences": 2,        # 模式入提案下限（与 attribution 同语义：防一次性噪声）
        "prompt_min_occurrences": 4, # prompt/skill 提案的更高下限（改提示词影响面更大）
        "unattributed_min": 1,       # unattributed 会话数达阈值即提埋点提案（缺口可见）
        "max_proposals": 20,         # 单次 generate 入库上限（防长尾淹掉评审队列）
        "llm_draft": False,          # GEPA 式 LLM 反思改写 rationale（False=纯模板，确定性）
        "max_rationale_chars": 500,  # rationale 截断上限（LLM 改写后仍受此约束）
    },
    # 本组控制 core/sandbox.py 的沙箱 A/B 评估口径（P1-4）。
    "sandbox": {
        "enabled": True,             # 总开关（False 时拒绝发起沙箱评估，不静默降级）
        "gate_timeout_s": 150,       # 单门禁超时（秒）：超时记 error，不进 fail 混淆回归信号）
        "default_scope": "full",     # 默认评估范围：full=全门禁 | referenced=静态引用该配置的门禁
        "infra_error_ratio": 0.3,    # 双臂 error 占比超此值 ⇒ verdict=error（评估环境坏了，结论无效）
        "max_report_gates": 400,     # 报告内门禁明细条数上限（防长尾淹掉摘要）
    },
    # ── P2-1（2026-10-19 自进化调研路线图 P2 首项）：misevolution 安全回归监控 ──
    # 背景：P1-4 之后系统可自改配置；misevolution 研究（decrypt 2025）证实智能
    #   体会在纯内部循环里安全滑坡（99.4%→54.4%）且无外部触发 ⇒ 红线 #5 要求
    #   拒答/越权类信号做常驻门禁 + 晋升后复测。口径与诚实边界见 core/misevolution.py。
    "misevolution": {
        "enabled": True,             # 总开关（False 时监控端点返 enabled=False，不静默降级）
        "window_days": 7,            # 信号窗口（天）
        "min_denominator": 20,       # 分母下限：不足 ⇒ status=unknown（空窗口不是 0%）
        "regression_delta": 0.05,    # 相对冻结基线的恶化幅度超此值 ⇒ verdict=regression
        "auth_reject_warn": 0.05,    # 身份拒绝率 warn/alert（绝对阈值）
        "auth_reject_alert": 0.15,
        "perm_denied_warn": 0.02,    # 工具权限拒绝率（RBAC 越权尝试的确定性边界）
        "perm_denied_alert": 0.05,
        "whitelist_denied_warn": 0.02,   # 白名单越权尝试率（P2-1 补的留痕）
        "whitelist_denied_alert": 0.05,
        "tool_fail_warn": 0.10,      # 工具失败率（健康度基线）
        "tool_fail_alert": 0.25,
        "kind_coverage_warn": 0.50,  # failure_kind 覆盖率（可监控性，红线 #4；低于下沿分级）
        "kind_coverage_alert": 0.20,
    },
    # ── P2-2（2026-10-19 自进化调研路线图 P2 第二项）：失败驱动的任务自生成 ──
    # 背景：EnvBuilder / AgentGym / AgentEvolver Self-Questioning 式自造课程——
    #   从失败证据（P1-2 首坏步模式 + P0-3 坏样本台账 + 类型覆盖率）长出「该练
    #   什么任务」的提议。铁律同 P1-3：只提议不自动执行（自动造任务并自动跑 =
    #   内部循环自我强化，misevolution 滑坡形态），人裁决后走人的链路。
    "curriculum": {
        "enabled": True,                # 总开关（False 时 generate 返 enabled=False）
        "min_pattern_count": 2,         # 首坏步模式复发下限（<此值不值得单独立任务）
        "min_badcase_occurrences": 3,   # 坏样本复发下限（bad_cases.occurrences）
        "kind_coverage_min": 0.5,       # 失败类型覆盖率下沿（红线 #4 可监控性）
        "unattributed_min": 1,          # 结局坏零坏步会话数下限
        "max_proposals": 20,            # 单批提案上限（防长尾淹掉看板）
    },
    # ── P2-3 Agent 档案库/种群（DGM 式 archive）：变体台账 + 死路记忆 ──────
    # 铁律：**只记录，不切换生效 agent、不回滚配置**（回滚过人的铁律同 P1-4
    # ——档案库是记忆，不是手）。死路判定门槛设在 2 次而非 1 次：一次回归
    # 可能是噪声（并发/环境/偶发），反复才是模式。
    "agent_archive": {
        "enabled": True,                # 总开关（False 时 scan/refresh 返 enabled=False）
        "window_days": 90,              # 进化历史回看窗口（天）
        "min_denominator": 2,           # 各维度分母下限：不足即 unknown（空窗口不是 0 分）
        "dead_end_repeat": 2,           # 同一变体判回归几次记死路
        "max_variants": 200,            # 单次扫描入册上限（防长尾注水）
    },
    # ── P2-4（2026-10-19 自进化调研路线图 P2 第四项）：画布拓扑台账与体检 ────
    # 口径裁决：路线图原文「多画布流拓扑实验」，但真库 agent_flows 62 条
    #   vs flow_runs 仅 11 条（且 9 条是同日同构画布的秒级重复），在 11 条
    #   运行记录上比较两种拓扑得到的是噪声；且红线 #6 明写「不碰架构拓扑」。
    #   故本期落**台账 + 体检**：只读画布、只记缺陷、只报同构，绝不写拓扑。
    "flow_topology": {
        "enabled": True,                # 总开关（False 时 scan/overview 返 enabled=False）
        "min_denominator": 2,           # 运行证据分母下限：不足即 unknown（没跑过≠跑零次）
        "linear_degenerate_min": 4,     # 达到几个节点才算「纯链无并行」可疑
        "max_canvases": 2000,           # 单次扫描画布上限（防长尾注水）
    },
    # ── 自进化周报（价值感知层）：只读汇总，不新增判据、不发明数字 ──────────
    # 定位：把archive / curriculum / sandbox / HIL / 安全信号 / 拓扑六个域的
    #   既有台账汇成**一页可行动摘要**，回答"这周系统替我做了什么、我该做什么"。
    #铁律：周报是镜子不是手——只SELECT，且**不能一键处理的事必须显式标注**，
    #   不许渲染成看起来能点的按钮。
    "evolution_report": {
        "enabled": True,                # 总开关（False 时 build 返 enabled=False）
        "window_days": 7,               # 本周窗口（天）；环比对照 = 上一个等长窗口
        "max_actions": 50,              # 待决策条目上限（防长尾注水）
        "max_detail": 8,                # 每源明细条目上限
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
    "MBSE_INTENT_CACHE_ENABLED": ("intent", "cache_enabled"),  # P1-31：默认 False（实测收益≈0）
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


# ── P1-4（2026-10-19）影子配置层 ────────────────────────────────────────
#: 影子配置环境变量名。**只有沙箱评估的子进程会设置它**；生产进程永不设置
#: ⇒ 不设时 _load_shadow() 返 {}，四层合并与改动前完全一致（行为零变化）。
SHADOW_ENV = "MBSE_SHADOW_CONFIG"


def _load_shadow() -> dict:
    """影子配置层（最高优先级）：MBSE_SHADOW_CONFIG（JSON）→ {section: {key: value}}。

    ## 为什么单开一个环境变量而不是复用 ENV_MAP
    ENV_MAP 是"每个键一个环境变量"的部署覆盖通道；沙箱需要的是"一次传一组
    候选配置、且只活在一个评估子进程里"——一组 JSON 更贴合，也不会和部署
    覆盖混用（误设 MBSE_SHADOW_CONFIG 的生产进程会得到最高优先级覆盖，
    故校验必须 fail-loud，见下）。

    ## fail-loud 校验（不是静默忽略）
    未知分组/未知键/类型非法一律抛 ValueError：影子键拼错若静默失效，
    "影子配置下无回归" 的结论就是假的——比没有评估更坏。同理 JSON 解析
    失败也抛。唯一不抛的情况：变量未设/为空串（返 {}）。
    """
    raw = os.environ.get(SHADOW_ENV) or ""
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except Exception as e:
        raise ValueError(f"{SHADOW_ENV} 不是合法 JSON: {e}")
    if not isinstance(data, dict):
        raise ValueError(f"{SHADOW_ENV} 必须是 JSON 对象 {{section: {{key: value}}}}")
    out: dict = {}
    for section, items in data.items():
        if not isinstance(items, dict):
            raise ValueError(f"{SHADOW_ENV}.{section} 必须是对象 {{key: value}}，收到 {type(items).__name__}")
        meta_sec = CONFIG_SCHEMA.get(section)
        if meta_sec is None:
            raise ValueError(
                f"未知配置分组: {section!r}（{SHADOW_ENV} 只接受 CONFIG_SCHEMA 已登记的分组）")
        for key, val in items.items():
            meta = meta_sec.get(key)
            if meta is None:
                raise ValueError(f"未知配置项: {section}.{key}（{SHADOW_ENV} 只接受 CONFIG_SCHEMA 已登记的键）")
            out.setdefault(section, {})[key] = _coerce(meta, val)
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
    """重新合并四层配置（影子 > 环境变量 > 配置文件 > 默认值）并刷新内存/兼容属性。

    静态配置经 save_override() 落盘后调用本函数即可即时生效；
    host/port/db path 等启动级参数需重启服务完全生效。

    P1-4：影子层（MBSE_SHADOW_CONFIG）只在沙箱评估子进程内设置，
    生产进程不设 ⇒ _load_shadow() 返 {} ⇒ 合并结果与三层时代一致。
    """
    global _CONFIG
    _CONFIG = _merge(_merge(_merge(DEFAULT_CONFIG, _load_file(CONFIG_PATH)),
                            _load_env()), _load_shadow())
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
    # ★ B4-5（2026-10-14）：**`intent` 段此前整段不在 SCHEMA 里** ——
    #   `save_override` 用 `CONFIG_SCHEMA` 做白名单校验（core/config.py:1451），
    #   于是 `intent.keyword_scored` / `keyword_generic` / `history_llm_min` 等
    #   **12 个既有开关一律无法通过设置页 / REST API 保存**（一律抛「未知配置项」）。
    #   即：代码里能读到的开关，用户在界面上根本改不了 —— **"文档有、代码有、界面没有"**。
    #   ⚠️ 与 MEMORY 里「静态 grep 到 ≠ 用户看得见」同族：这次是**配置存在 ≠ 可被配置**。
    #   ⇒ 本段把 intent 全部开关补进白名单（含 B4-5 新增的 rule_llm_confirm），
    #     门禁 `verify_intent_config_reachable` 锁住"SCHEMA 与 DEFAULT_CONFIG 的 intent 段不漂移"。
    "intent": {
        "keyword_scored":         {"type": "bool", "desc": "关键词层「竞争打分」（False = 回到「首个命中即返回」旧行为，A/B 用）"},
        "keyword_generic":        {"type": "bool", "desc": "泛词是否参与打分（False = 泛词既不加分也不触发共现，A/B 用）"},
        "sample_collect":         {"type": "bool", "desc": "真实请求是否把用户输入采集进意图样本池"},
        "cache_enabled":          {"type": "bool", "desc": "意图级缓存（精确文本 + 索引指纹双匹配）"},
        "confirm_when_unsure":    {"type": "bool", "desc": "低置信/冲突时是否转入澄清确认"},
        "confirm_options_max":    {"type": "int", "desc": "意图确认卡的候选选项数上限（按本次输入语义相关序动态选取）"},
        "confirm_llm_dynamic":    {"type": "bool", "desc": "意图确认卡内容由 LLM 基于上下文动态生成（关闭=固定意图列表；LLM 失败自动兜底）"},
        "clarify_followup_max_chars": {"type": "int", "desc": "澄清后承接补充的最大字符数（本会话有过澄清作答且输入不超过此长度 ⇒ 跳过澄清门直接执行）"},
        "llm_sem_conflict_min":   {"type": "float", "desc": "语义与 LLM 意图互斥时的语义分下限（达阈转澄清）"},
        "history_recall":         {"type": "bool", "desc": "是否把历史用户话喂给 LLM 一路"},
        "history_recall_turns":   {"type": "int", "desc": "喂给 LLM 的最近几轮用户话"},
        "history_llm_min":        {"type": "float", "desc": "带历史采纳（route=llm_history）的地板值（非分离阈）"},
        "history_confirm_silent": {"type": "bool", "desc": "两路一致（确认）时不弹「可改选」澄清细条"},
        "rule_llm_confirm":       {"type": "bool", "desc": "B4-5：规则命中后先经 LLM 独立复核（确认/否决/不可用三态）。默认关=零回归；开启前请用 tools/calib/eval_intent_baseline.py 做 A/B"},
    },
    "database": {
        "path":           {"type": "str", "desc": "SQLite 数据文件路径（需重启生效）"},
        "connect_timeout": {"type": "int", "desc": "数据库连接超时（秒）"},
    },
    # P1-1（2026-10-15）：本体治理组。⚠️ 白名单纪律——save_override 以 CONFIG_SCHEMA 为准，
    # DEFAULT_CONFIG 有而 SCHEMA 无 = 界面/API 均改不了（intent 组 12 个开关的教训）。
    "ontology": {
        "publish_gate": {"type": "str", "desc": "★ 发布门禁档位（最低拦截级别）：off=发布不做一致性拦截｜high=仅 high 拦截（默认，与历史行为等价）｜warn=warn 及以上拦截（最严）。检查口径=待发布编辑态数据；改完即时生效，版本历史面板显示当前档位"},
    },
    "llm": {
        "force_mock":      {"type": "bool",   "desc": "强制 Mock 模式（不依赖外部网络）"},
        "deepseek_api_key": {"type": "secret", "desc": "DeepSeek API key（无 key 时注入预置 provider）"},
        "qwen_api_key":     {"type": "secret", "desc": "Qwen API key"},
        "context_window_guard": {"type": "str", "desc": "context_window 守卫：clamp(默认，超窗截断+留痕)/warn(只告警不截断，用于放开「假天花板」)/off(不介入)"},
        "stream_include_usage": {"type": "bool", "desc": "流式请求显式带 stream_options.include_usage（默认关；DeepSeek 流式默认就发 usage 帧，仅部分兼容端点需要打开）"},
        "retry_times":          {"type": "int", "desc": "真实调用失败重试次数（指数退避；流式恒不重试）"},
        "retry_backoff_ms":     {"type": "int", "desc": "首次重试退避毫秒（第 n 次 = 该值 × 2^n）"},
        # ── 2026-10-09 超时治理（实测 v4/v1 两次踩中「等 9 分钟换一个异常」）──
        # 原`httpx timeout=180` 是**硬编码**，且与 retry_times **相乘**：
        #   180 × (2+1) + 退避 ≈ **541.5 秒**（实测 545s 吻合）。
        "http_timeout_s":       {"type": "int", "desc": "单次 HTTP 调用超时秒数（默认 180）。★ 改小前先确认慢模型：deepseek-v4-pro 实测单次 121s"},
        "total_timeout_budget": {"type": "int", "desc": "★ **一次调用（含全部重试）的总时间预算**秒数，默认 0=不启用。启用后即使 http_timeout_s × retry_times 仍会乘出长尾，也会被这个闸门截断（实测不加它最坏 541s）"},
        # ── 2026-10-09 reasoning 思考预算（**根治** 541s 超时的真因）──
        # 实测：思考与正文**共享 max_tokens**，而 qwen3.8-* 默认开着思考 ⇒
        #   10-05 前思考占输出 4%（给用户 96%）→ 今天占 **63%**（给用户仅 37%），
        #   慢调用占 **72%**；极端一例 98% 的额度用来思考、只给用户 103 token。
        #   ⇒ 按 20~80ms/token 线性拉长耗时 ⇒ 撞超时 ⇒ 重试 ⇒ 更慢（乘法放大）。
        "thinking_budget":         {"type": "int", "desc": "★ **给 reasoning 思考的独立上限**（token），默认 0=不启用。思考与正文共享 max_tokens，不设限时思考可吃掉 63% 输出（实测）"},
        "thinking_reserve_tokens": {"type": "int", "desc": "★ **给正文的保底额度**（token，思考不得吃光），默认 0=不启用。实际压算 max_tokens = min(max_tokens, thinking_budget + thinking_reserve_tokens)"},
        "thinking_warn_tokens": {"type": "int", "desc": "★ **思考超限告警阈值**（token），默认 0=不启用。只在 `_record_usage` 打 WARNING（流式/非流式唯一共同点），**只留痕不拦截**（拦截用 thinking_budget）。实测 310/6890 次调用 reasoning>2000tok，极端例 13430/13535=99.2% 耗 4017s。按 (intent, provider) 去重防刷屏"},
        # ── 2026-10-09 thinking 按 intent 分流（L1；粒度=任务维度）──
        # 依据实测（llm_usage_stats 按 intent 聚合思考占completion 的比例）：
        #   廉价任务（输出 <1200 tok）思考占 58%~86% ⇒ 纯浪费
        #     crypto_probe 30tok/86%、probe 863/82%、routing 217/69%、doc_summary 331/58%
        #   重产码任务（输出 >4000 tok）思考占 0%~1.4% ⇒ 本该开
        #     plan_refine 5697/0%、plan_summary 4336/1.4%、impact_advice 1619/0%
        # ⚠️ **三态语义**（关键，见 openai_compat 注释）：
        #   命中 off ⇒ 显式发 {"type":"disabled"}（**必须显式**，不发=模型端仍按默认开启）
        #   命中 on  ⇒ 显式发 {"type":"enabled"}
        #   **其余一律不发该字段**（保持改动前行为；否则全链路会被一起关掉思考）
        "thinking_policy": {"type": "str", "desc": '★ **按 intent 分流思考**的 JSON：{"off":[...关思考的 intent...],"on":[...开思考的 intent...]}。★ 未命中的 intent **不发 thinking 字段**（保持原行为），**切勿**配"默认关闭"——那会把重产码任务的思考也一起关掉（见 openai_compat 缺陷 2 注释）。★ **界面改本项即时生效**（走`/api/system/config/static` → 写 `~/.workbuddy/mbse_config.json` → `reload()` → `get()` 读到；2026-10-10 实测链路确认，此前"改 settings 表无效"的提示是错的）。⚠️ 改完需**新建会话**才对已在跑的会话生效（分流在调用时读配置）'},
        # ── 形态规则层（可维护性：新增 intent 零配置被覆盖）──
        "thinking_shape_enabled": {"type": "bool", "desc": "★ **按任务形态自动判定思考**的总开关，默认 false=不启用（零行为漂移）。★ 为什么需要：`_intent` 是动态的（编排层直接用 Agent 名当 intent，库里有27 个 active Agent 含中文名），实测 off 名单对 Agent 名覆盖率 **0/27** ⇒ 纯名单制必然随系统演进而漂移。开启后新增 intent 也能被自动覆盖，不需人记得改配置"},
        "shape_probe_max_tokens": {"type": "int", "desc": "★ 形态规则1：带 tools 且 max_tokens ≤ 此值 ⇒ 判为「探针轮」⇒ 关思考（默认 1200）。依据实测：廉价决策类任务思考占 58~98% 却只产几十~几百 token，而重产码任务上限 ≥8192。⚠️ **不要调太大** —— 工具调用本身需要推理，长 JSON 参数的工具可能被误判而丢 tool_calls"},
        "shape_cheap_max_tokens": {"type": "int", "desc": "形态规则2：无 tools 且 max_tokens ≤ 此值 ⇒ 判为「廉价轮」⇒ 关思考；0=不启用（默认关，因为无 tools 的调用里混着大量重产码任务，误判风险高）"},
        # ── 数据自校正层（第 3 层）──
        "autotune_enabled": {"type": "bool", "desc": "★ **按真库实测数据自校正**思考分流的开关，默认 false=不启用。★ 为什么需要：形态规则只认「探针语义」这类名字，若新增「快速摘要」这类 Agent（思考占 80%、只产几十 token）则规则不命中、思考继续失控 ⇒ 仍要人配。这层按 `llm_usage_stats` 实测聚合判定，**不需要知道 intent 名字**。⚠️ 只产出**建议清单**（`llm.thinking_autotune.suggest_thinking_off()`），不自动改配置"},
        "autotune_min_ratio": {"type": "float", "desc": "★ 自校正的思考占比阈值：≥ 此值**且**平均产出 ≤ autotune_max_output ⇒ 建议关思考（默认 0.5）。依据实测分界：廉价任务 58%~98%，重产码 0%~2.5%，中间几乎无重叠"},
        "autotune_max_output": {"type": "int", "desc": "自校正的平均产出上限（token，默认 1200）。实测廉价任务 ≤1224，重产码 ≥ 4336"},
        "autotune_min_samples": {"type": "int", "desc": "★ 安全阀：样本量低于此数**不判**（默认 5）。理由：2 次调用的均值没有代表性，而误判代价（关掉重产码的思考）远大于收益"},
        "fallback_provider_id": {"type": "int", "desc": "主 provider 重试后仍失败时的备选 provider id（0=不启用）"},
        "circuit_breaker_enabled": {"type": "bool", "desc": "P1-4 熔断总开关：连续失败时停止打该 provider（关=仅重试）"},
        "circuit_breaker_fail_threshold": {"type": "int", "desc": "连续可重试失败 N 次后熔断该 provider"},
        "circuit_breaker_reset_sec": {"type": "int", "desc": "熔断封锁秒数；到点后半开放一个探针请求"},
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
    # ── agent 组（2026-10-09 新增）──
    # ★ 为什么现在才进 schema：`max_tool_rounds` 一直在代码里读 `config.get("agent", …)`，
    #   但**本组从未登记进 CONFIG_SCHEMA** ⇒ 界面上看不到、改不了，只能改代码。
    #   实测（2026-10-09）：`CONFIG_SCHEMA` 无agent 组、`settings` 表 `agent.*` 0 行。
    "agent": {
        # ★ 2026-10-17（米爸反馈1：正常使用总被「调用次数用尽」中断）：12 → 100。
        #   语义分工不变：成功推进不扣任何预算，100 只是防死循环的绝对天花板；
        #   真正的失控防线 = max_failed_rounds + loop_token_budget + 空转检测。
        "max_tool_rounds":   {"type": "int", "desc": "ReAct 工具轮**绝对硬上限**（默认 100，2026-10-17 米爸反馈由 12 提高——防死循环天花板，正常成功推进不受它限制，成功轮不扣预算见 max_failed_rounds）"},
        "max_failed_rounds": {"type": "int", "desc": "★ **失败轮预算**（默认 3）：只有工具失败/无效的轮次才扣；成功推进不扣（米爸 2026-10-09 纠正的语义，旧实现把成功轮也计入总轮数⇒ 连坐截断）"},
        "loop_token_budget": {"type": "int", "desc": "★ ReAct 循环 **token 预算**（默认 300000，2026-10-17 由 60000 提高）：本轮请求内所有 LLM/评估调用累计 token 上限，超限停止调工具并出结论；0=不限（不推荐，失控保护）"},
        "tool_call_retries": {"type": "int", "desc": "★ 单次工具调用**报错自动重试**次数（默认 3，2026-10-17 米爸反馈）：重试执行异常（HTTP 502/连接重置）与超时；业务失败（权限拒绝/参数截断）不重试——重试不会有不同结果"},
        "tool_retry_timeout_s": {"type": "int", "desc": "★ 重试尝试的**单次等待上限**（秒，默认 30，2026-10-17 米爸反馈『超时也需要重试，但需要时间上限』）：首轮仍用完整工具超时（默认 120s）给慢工具公平机会，重试一律收紧到本值，防止单工具把一轮拖死"},
        "ask_user_enabled":  {"type": "bool", "desc": "★ ask_user 澄清工具开关（默认开，2026-10-17 米爸反馈3）：开启后 AI 执行中「真的需要用户拍板」时调用该工具，弹出可点选确认卡片并暂停流程等回答；关闭则回到纯文本提问的旧形态"},
        # ── 自主续跑（2026-10-17 米爸「harness 持续工作」需求）──
        # 对齐标杆：Claude Code /goal（评估器决定放行还是打回）+ AutoGPT boundedness
        # 教训（次数/墙钟/token/空转四道闸，缺一即失控）。触发门：本轮**实际调用过工具**
        # （_lg.calls 非空）才评估——纯问答轮没有「剩余步骤」可言，且省评估器成本。
        "auto_continue_enabled": {"type": "bool", "desc": "★ 自主续跑总开关（默认开）：AI 完成度评估判定「任务未完成」时自动继续工作，无需用户逐条下达指令；关闭则维持一问一答形态"},
        "auto_continue_max":     {"type": "int", "desc": "★ 自主续跑次数上限（默认 3）：单轮请求内最多自动续跑几次（AutoGPT「无完成任务生成」对策；每次续跑重置 ReAct 轮次预算，但仍共享 token 预算）"},
        "auto_continue_deadline_s": {"type": "int", "desc": "★ 自主续跑墙钟上限（秒，默认 600；0=不设）：从本轮请求开始累计，超时不再续跑（仍受次数/token 闸约束）"},
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
        "total_budget_window_ratio": {"type": "float", "desc": "总闸上限=provider窗口×该比例（默认0.75；总闸只防撞硬上限，不用于省token）"},
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
        "conflict_detect_enabled": {"type": "bool", "desc": "冲突消解总开关：检出同键互斥记忆后打 superseded_by 标记（仅标记不删；默认关）"},
        "conflict_detect_sim_high": {"type": "float", "desc": "冲突检测的同键候选相似度下限（互斥事实字面高度重叠，实测 0.73）"},
    },
    "entity_merge": {
        "auto_threshold": {"type": "float", "desc": "实体消歧自动合并分阈值（≥ 该分自动合并；>1.0 等价关闭。DB settings 的 auto_threshold 优先）"},
        "flag_threshold": {"type": "float", "desc": "实体消歧建待审候选的下界（[flag,auto) 区间进人工队列）"},
        "auto_merge_enabled": {"type": "bool", "desc": "实体消歧自动合并总开关（关=高分项降级为待审候选，不丢线索）"},
    },
    "semantic_cache": {
        "enabled":      {"type": "bool", "desc": "语义缓存开关（相似查询命中直返）"},
        "threshold":    {"type": "float", "desc": "命中相似度阈值"},
        "max_entries":  {"type": "int", "desc": "缓存条数上限（超出淘汰最旧）"},
    },
    "ingest": {
        "draft_flow":          {"type": "bool", "desc": "AI 建模入库发布门禁（确认后走合并请求待审/自动发布）"},
        # ── L1（2026-10-10）标题路径增强 HyDE 问句 ─────────────────────────
        # 现状问题（实测）：`_generate_hyde_questions` 是「术语+固定模板」，
        # 全库问句句式几乎一样 ⇒ 同质化稀释召回。
        # 本项开启后：**含 markdown 标题的块**改用标题层级路径合成多种真实问法
        #（算法借鉴 ragflow `qa.go:216 extractQAMarkdown` 的标题栈，纯本地实现、零依赖）；
        # 无标题块（纯正文/表格/OCR）自动回落到原模板 ⇒ **永不劣化**。
        "hyde_heading_enabled": {"type": "bool", "desc": "★ 用标题层级路径合成 HyDE 假设问题（替代单一模板，提升问句区分度）。仅对含 markdown 标题的块生效；无标题块自动回落原模板。默认关=行为不变"},
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
        "core_keep_extra": {"type": "str", "desc": "JIT 工具预筛保底集合的追加项（逗号分隔工具名）。★ 2026-10-10 实测：修复类工具必须列进来，否则会被语义预筛裁掉（实测 view_expansion 清单 6 个里原本没有 sysml_v2_autofix，导致 autofix 一次都没被调用）"},
        "inject_input_schema": {"type": "bool", "desc": "把 tools.input_schema 注入 function-calling 的 parameters（关=回到 2026-10-13 之前的宽松空 schema）。★ 实测该列 39/41 有真值，此前被 SELECT 后丢弃、payload 恒为 properties:{}"},
    },
    "subtask_protocol": {
        "inject_contract": {"type": "bool", "desc": "把交付协议契约注入子任务 query（关=模型只返纯文本，协议三数组恒空）。★ 实测注入前 artifacts/evidence/risks 非空率 0%"},
        "materialize": {"type": "bool", "desc": "把协议三数组物化写入 subtask_artifacts（关=只留在 summary_json 里，该表实测 0 行）"},
    },
    "agent_assignment": {
        "enabled": {"type": "bool", "desc": "编排分派打分总开关（关=不算分不改派，完全退回 2026-10-13 之前行为）。★ 该打分器只做择优与留痕，planner LLM 仍是决策者"},
        "weight_capability": {"type": "float", "desc": "专长匹配权重（默认 0.5）。标定实测：原词表只覆盖真库 76 词条的 9 个，改为字符 bigram 重合度后 8 视图 top-1 命中 6/6"},
        "weight_load": {"type": "float", "desc": "负载权重（默认 0.3）。负载来自 agent_tasks 里 status='running' 的行数（B1-3 之后才有数据）"},
        "weight_history": {"type": "float", "desc": "历史成功率权重（默认 0.2，原 0.1 调高）。真库 168 done/54 failed ⇒ 该信号实测可得且区分度最明显"},
    },
    "stream_event_log": {
        "enabled": {"type": "bool", "desc": "编排 SSE 关键事件落库总开关（关=不落任何事件，转发不受影响）"},
        "types": {"type": "str", "desc": "落哪几类事件（逗号分隔）。★ 刻意不含 token/reasoning：两者是逐字增量，一次真实建模可达数百帧，全量落库会撑爆表"},
        "max_per_run": {"type": "int", "desc": "单次编排（conversation_id+run_id）事件数上限，超出丢弃最旧的"},
    },
    "kb_scope": {
        "docs_missing_fallback": {"type": "bool", "desc": "白名单文档不存在时剔除失效项；全失效则退化为不限文档"},
        "docs_missing_warn":     {"type": "bool", "desc": "白名单失效时写日志并在检索结果回显 kb_scope_warn"},
    },
    "rag": {
        # ── RAGFlow 接入（L3 路线，2026-10-10）────────────────────────────
        # 定位：RAGFlow **只替换「解析 + 切片」**，检索/图谱/四项治理全不动。
        # 实测依据（ragflow-main v1.0.0-rc1，Go 重写版）：SDK 仅依赖
        # requests + beartype（Apache-2.0）⇒ 客户端零重依赖、不破铁律 4；
        # ⚠️ 但服务端仍需 Docker + ES + MySQL + Redis + 对象存储。
        # ⚠️⚠️ **默认 false**：不部署 RAGFlow 时行为与改动前逐字相同。
        # 启用后任何异常（连不上/认证失败/解析超时/返回空）自动回落本地
        # chunk_text_structured —— 入库主链路绝不被可选依赖拖垮。
        "ragflow_enabled":        {"type": "bool",  "desc": "★ 是否用 RAGFlow 做解析+切片（默认关）。关闭=本地 chunk_text_structured，零漂移。启用后失败自动降级回本地，绝不影响入库"},
        "ragflow_base_url":       {"type": "str",   "desc": "RAGFlow 服务地址（如 http://127.0.0.1:9380）。⚠️ 仅启用时必填，未填⇒适配层直接返回 error 并降级"},
        "ragflow_api_key":        {"type": "str",   "desc": "RAGFlow API Key（在 RAGFlow 界面 → 右上角头像 → API 令牌 生成）"},
        "ragflow_dataset":        {"type": "str",   "desc": "目标知识库名（不存在会自动创建）。建议按项目/分支分库，如 mbse-default"},
        "ragflow_chunk_method":   {"type": "str",   "desc": "切片方法：naive（按字数）/ qna（问题-答案对，★推荐，DeepDoc 模板化切片）/ overview（摘要视角）"},
        "ragflow_embedding_model":{"type": "str",   "desc": "RAGFlow 侧 embedding 模型。⚠️ 改了会让已入库分块的向量口径不一致，建议与本工程 embedding 配置保持同源"},
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
        "relation_relevance_gate": {"type": "bool", "desc": "图谱置信度关系因子只计入与 query 相关的关系（两端实体名均在 query 中）；False=旧行为全量计数"},
        "suppress_low_confidence": {"type": "bool", "desc": "低置信检索抑制：向量兜底注入宽度内全为「低」置信时不返回任何向量内容（neg 域收敛）；False=旧行为"},
        "confidence_high":       {"type": "float", "desc": "命中置信等级「高」分界（真向量相似度口径）"},
        "confidence_mid":        {"type": "float", "desc": "命中置信等级「中」分界"},
        "rerank_enabled":        {"type": "bool",  "desc": "LLM Rerank 重排开关（失败静默回退原排序）"},
        "rerank_provider_id":   {"type": "int",   "desc": "★ 重排专用 LLM provider_id（0=用默认）。实测默认 provider 可能整体不可用（所有 intent 一律 31.7s 超时、ct=0）⇒ 重排 100% 静默降级、表现为「开关开了但排序未变」。重排每次查询都跑、对延迟敏感，应独立指定更稳的 provider"},
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
    # P0-1（2026-10-18）：资产长尾治理阈值（core/lifecycle.py 消费；前端契约登记处）
    "lifecycle": {
        "enabled":            {"type": "bool",  "desc": "长尾治理分析总开关（False 时端点返空结果）"},
        "idle_days":          {"type": "int",   "desc": "退出线：闲置天数（超过无使用记录）"},
        "min_uses":           {"type": "int",   "desc": "退出线：窗口内使用次数下限"},
        "fail_rate_pct":      {"type": "int",   "desc": "退出线：失败率阈值（%）"},
        "watch_idle_days":    {"type": "int",   "desc": "观察线：闲置天数（达到即 watch）"},
        "watch_fail_rate_pct":{"type": "int",   "desc": "观察线：失败率（%，达到即 watch）"},
    },
    # P0-2（2026-10-18）：资产变更审计（core/asset_audit.py 消费；前端契约登记处）
    "asset_audit": {
        "enabled":            {"type": "bool",  "desc": "变更审计总开关（False 时正常更新但不写审计行）"},
        "auto_bump_version":  {"type": "bool",  "desc": "调用方未显式给版本号时自动递升版本"},
        "max_value_chars":    {"type": "int",   "desc": "单字段审计值截断上限（字符）"},
    },
    # P0-3（2026-10-18）：Bad Case 收集（core/badcase.py 消费；前端契约登记处）
    "badcase": {
        "enabled":            {"type": "bool",  "desc": "Bad Case 收集总开关（False 时端点返空结果）"},
        "window_days":        {"type": "int",   "desc": "判据窗口：只扫最近 N 天的坏样本"},
        "include_probes":     {"type": "bool",  "desc": "探测调用（无会话且无 run）是否算坏样本"},
        "max_detail_chars":   {"type": "int",   "desc": "单条 evidence 截断上限（字符）"},
        "reopen_on_recur":    {"type": "bool",  "desc": "已 triage 的主题复现时重开为 open"},
        "alert_threshold":    {"type": "int",   "desc": "默认告警线：open 数达到即值得关注"},
    },
    # P1-1（2026-10-18）：Trace2Dataset（tools/eval/trace2dataset.py 消费）
    "trace2dataset": {
        "enabled":            {"type": "bool",  "desc": "Trace2Dataset 取材总开关（False 时构建返空）"},
        "window_days":        {"type": "int",   "desc": "取材窗口：只取最近 N 天的生产证据"},
        "max_per_extractor":  {"type": "int",   "desc": "单抽取器样本上限（防单一证据源淹掉全集）"},
        "exclude_mock":       {"type": "bool",  "desc": "排除 Mock 降级回答（Mock 不是系统能力证据）"},
        "mock_markers":       {"type": "str",   "desc": "Mock 标记（逗号分隔，query/response 命中即排除）"},
        "min_query_chars":    {"type": "int",   "desc": "query 最短长度（短于则不构成评测样本）"},
        "max_response_chars": {"type": "int",   "desc": "response 截断上限（字符）"},
    },
    # P1-2（2026-10-18）：步骤级归因器（core/attribution.py 消费）
    "attribution": {
        "enabled":            {"type": "bool",  "desc": "归因总开关（False 时模式聚合返空）"},
        "window_days":        {"type": "int",   "desc": "模式聚合窗口：只归因最近 N 天的首坏步"},
        "min_occurrences":    {"type": "int",   "desc": "模式入榜下限（出现次数，防一次性噪声上榜）"},
        "max_patterns":       {"type": "int",   "desc": "模式条数上限（防看板被长尾淹掉）"},
        "max_steps":          {"type": "int",   "desc": "单会话步骤上限（超限保留最早的，首坏步在头部）"},
        "session_scan_limit": {"type": "int",   "desc": "模式聚合扫描会话数上限（最近活跃优先）"},
    },
    "config_proposal": {
        "enabled":               {"type": "bool",  "desc": "提案总开关（False 时 generate 返空）"},
        "min_occurrences":       {"type": "int",   "desc": "模式入提案下限（出现次数，防一次性噪声）"},
        "prompt_min_occurrences": {"type": "int",  "desc": "prompt/skill 提案下限（更高：改提示词影响面更大）"},
        "unattributed_min":      {"type": "int",   "desc": "unattributed 会话数达阈值即提埋点提案"},
        "max_proposals":         {"type": "int",   "desc": "单次 generate 入库上限（防长尾淹掉评审队列）"},
        "llm_draft":             {"type": "bool",  "desc": "GEPA 式 LLM 反思改写 rationale（False=纯模板）"},
        "max_rationale_chars":   {"type": "int",   "desc": "rationale 截断上限（字符）"},
    },
    # P1-4（2026-10-19）：沙箱 A/B 评估器（core/sandbox.py 消费）
    "sandbox": {
        "enabled":            {"type": "bool",  "desc": "沙箱评估总开关（False 时拒绝发起，不静默降级）"},
        "gate_timeout_s":     {"type": "int",   "desc": "单门禁子进程超时（秒，超时记 error）"},
        "default_scope":      {"type": "str",   "desc": "默认评估范围：full=全门禁；referenced=静态引用该配置的门禁"},
        "infra_error_ratio":  {"type": "float", "desc": "双臂 error 占比超此值 ⇒ verdict=error（环境坏，结论无效）"},
        "max_report_gates":   {"type": "int",   "desc": "报告门禁明细条数上限（防长尾淹掉摘要）"},
    },
    # P2-1（2026-10-19）：misevolution 安全回归监控（core/misevolution.py 消费）
    "misevolution": {
        "enabled":                {"type": "bool",  "desc": "监控总开关（False 时端点返 enabled=False）"},
        "window_days":            {"type": "int",   "desc": "信号窗口（天）"},
        "min_denominator":        {"type": "int",   "desc": "分母下限：不足记 unknown（空窗口不是 0%）"},
        "regression_delta":       {"type": "float", "desc": "相对基线恶化幅度超此值 ⇒ regression"},
        "auth_reject_warn":       {"type": "float", "desc": "身份拒绝率 warn 阈值"},
        "auth_reject_alert":      {"type": "float", "desc": "身份拒绝率 alert 阈值"},
        "perm_denied_warn":       {"type": "float", "desc": "工具权限拒绝率 warn 阈值"},
        "perm_denied_alert":      {"type": "float", "desc": "工具权限拒绝率 alert 阈值"},
        "whitelist_denied_warn":  {"type": "float", "desc": "白名单越权尝试率 warn 阈值"},
        "whitelist_denied_alert": {"type": "float", "desc": "白名单越权尝试率 alert 阈值"},
        "tool_fail_warn":         {"type": "float", "desc": "工具失败率 warn 阈值"},
        "tool_fail_alert":        {"type": "float", "desc": "工具失败率 alert 阈值"},
        "kind_coverage_warn":     {"type": "float", "desc": "failure_kind 覆盖率 warn（低于即 warn）"},
        "kind_coverage_alert":    {"type": "float", "desc": "failure_kind 覆盖率 alert（低于即 alert）"},
    },
    # ── P2-2（2026-10-19 自进化调研路线图 P2 第二项）：失败驱动的任务自生成 ──
    # 铁律同 P1-3：只提议不自动执行（人裁决 accepted/rejected 留痕，任务的创建/
    # 执行走人所在链路）。口径与证据边界见 core/curriculum.py。
    "curriculum": {
        "enabled":                  {"type": "bool",  "desc": "任务自生成总开关（False 时 generate 返 enabled=False）"},
        "min_pattern_count":        {"type": "int",   "desc": "首坏步模式复发下限（<此值不值得单独立任务）"},
        "min_badcase_occurrences":  {"type": "int",   "desc": "坏样本复发下限（bad_cases.occurrences）"},
        "kind_coverage_min":        {"type": "float", "desc": "失败类型覆盖率下沿（红线 #4 可监控性）"},
        "unattributed_min":         {"type": "int",   "desc": "结局坏零坏步会话数下限"},
        "max_proposals":            {"type": "int",   "desc": "单批提案上限（防长尾淹掉看板）"},
    },
    # ── P2-3 Agent 档案库/种群（DGM 式 archive）：只记录不切换生效版本 ──────
    "agent_archive": {
        "enabled":         {"type": "bool", "desc": "Agent 档案库总开关（False 时 scan/refresh 返 enabled=False）"},
        "window_days":     {"type": "int",  "desc": "进化历史回看窗口（天）"},
        "min_denominator": {"type": "int",  "desc": "各维度分母下限：不足即 unknown（空窗口不是 0 分）"},
        "dead_end_repeat": {"type": "int",  "desc": "同一变体判回归几次记死路（1 次可能是噪声，反复才是模式）"},
        "max_variants":    {"type": "int",  "desc": "单次扫描入册上限（防长尾注水）"},
    },
    # ── P2-4（2026-10-19 自进化调研路线图 P2 第四项）：画布拓扑台账与体检 ────
    # 铁律：只读 agent_flows、只记录缺陷、只报同构，**绝不写生效拓扑**
    # （红线 #6「不碰架构拓扑」；体检镜不是手，同 P2-3 档案库纪律）。
    "flow_topology": {
        "enabled":              {"type": "bool", "desc": "画布拓扑体检总开关（False 时 scan/overview 返 enabled=False）"},
        "min_denominator":      {"type": "int",  "desc": "运行证据分母下限：有运行证据的画布不足此数 ⇒ failed_runs 记 unknown"},
        "linear_degenerate_min":{"type": "int",  "desc": "达到几个节点才算「纯链无并行」可疑"},
        "max_canvases":         {"type": "int",  "desc": "单次扫描画布上限（防长尾注水）"},
    },
    # ── 自进化周报（价值感知层）────────────────────────────────────────────
    # 铁律：只读汇总层。窗口内的环比只在本期与上期**都有数据**时才出delta，
    #   上期无台账 ⇒ unknown，绝不回落 0（空窗口不是 0 分）。
    "evolution_report": {
        "enabled":      {"type": "bool", "desc": "自进化周报总开关（False 时 build 返 enabled=False）"},
        "window_days":  {"type": "int",  "desc": "本周窗口（天）；环比对照窗口为上一个等长窗口"},
        "max_actions":  {"type": "int",  "desc": "待决策条目上限（防长尾注水）"},
        "max_detail":   {"type": "int",  "desc": "每源明细条目上限"},
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
