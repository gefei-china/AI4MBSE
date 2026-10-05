"""种子数据：_seed/_seed_projects/_seed_agents/_seed_builtin_tools/_backfill_domains/_seed_glossary。"""
import sqlite3
import os
import json
from contextlib import contextmanager
from datetime import datetime

from core.config import DB_PATH

def _seed(conn):
    c = conn.cursor()
    # 检查是否已有数据
    if c.execute("SELECT COUNT(*) FROM roles").fetchone()[0] > 0:
        return

    # 预置角色
    preset_perms = json.dumps({
        "ai_chat": ["view", "generate", "confirm", "reject"],
        "ai_studio": ["view", "edit_prompt", "edit_skill", "config_mcp", "publish"],
        "kb_browse": ["view", "edit_request"],
        "kb_ontology": ["view"],
        "kb_review": ["view", "confirm", "modify", "merge"],
        "branch_dev": ["create", "switch", "merge_request", "delete"],
        "branch_release": ["view"],
        "report": ["view", "export"],
        "admin": []
    })
    knowledge_perms = json.dumps({
        "ai_chat": ["view", "generate", "confirm", "reject"],
        "ai_studio": ["view", "edit_prompt", "edit_skill", "config_mcp", "publish"],
        "kb_browse": ["view", "edit_request"],
        "kb_ontology": ["view", "edit", "profile_io"],
        "kb_review": ["view", "confirm", "modify", "merge"],
        "branch_dev": ["create", "switch", "merge_request", "delete"],
        "branch_release": ["view", "review_merge", "rollback"],
        "report": ["view", "export"],
        "admin": []
    })
    admin_perms = json.dumps({
        "ai_chat": [], "ai_studio": [], "kb_browse": [], "kb_ontology": [],
        "kb_review": [], "branch_dev": [], "branch_release": [], "report": [],
        "admin": ["user_manage", "role_manage", "audit_view", "integration_config", "ops_manage"]
    })

    for name, rtype, perms in [
        ("设计师", "preset", preset_perms),
        ("知识工程师", "preset", knowledge_perms),
        ("系统管理员", "preset", admin_perms),
    ]:
        c.execute("INSERT OR IGNORE INTO roles (name, type, permissions) VALUES (?,?,?)", (name, rtype, perms))

    # 预置用户
    users = [
        ("wang", "王工", "网络系统总体设计室", 1, "ws-wang", "ldap"),
        ("li", "李工", "数字化部", 2, "ws-li", "ldap"),
        ("admin", "赵管", "数字化部", 3, "ws-admin", "oauth2"),
    ]
    for u in users:
        c.execute("INSERT OR IGNORE INTO users (username, display_name, department, role_id, workspace, source) VALUES (?,?,?,?,?,?)", u)

    # 预置 LLM Provider
    c.execute("""INSERT OR IGNORE INTO llm_providers (name, provider_type, base_url, model_name, is_default)
                 VALUES ('DeepSeek-V3', 'deepseek', 'https://api.deepseek.com/v1', 'deepseek-chat', 1)""")
    c.execute("""INSERT OR IGNORE INTO llm_providers (name, provider_type, base_url, model_name, is_default)
                 VALUES ('Qwen2.5-72B', 'qwen', 'https://dashscope.aliyuncs.com/api/v1', 'qwen2.5-72b-instruct', 0)""")

    # 预置生成规则
    rules = [
        ("default_views", "需求图,用例图,BDD,IBD,参数图", "默认视图集"),
        ("naming_convention", "REQ-{domain}-{seq}", "元素命名规范"),
        ("layout_rule", "hierarchical_orthogonal", "布局规则"),
        ("quality_threshold", "75", "规范性评分自动确认阈值"),
        ("max_alternatives", "3", "多方案产出上限"),
        ("confidence_threshold", "0.75", "图谱检索置信度降级阈值"),
    ]
    for k, v, d in rules:
        c.execute("INSERT OR IGNORE INTO generate_rules (rule_key, rule_value, description) VALUES (?,?,?)", (k, v, d))

    # 预置分支（默认三分支：release / dev / personal）
    c.execute("INSERT OR IGNORE INTO branches (name, branch_type, description) VALUES ('release', 'release', '正式发布版本')")
    c.execute("INSERT OR IGNORE INTO branches (name, branch_type, parent_branch, description) VALUES ('dev', 'dev', 'release', '主开发分支')")
    c.execute("INSERT OR IGNORE INTO branches (name, branch_type, parent_branch, description) VALUES ('personal', 'personal', 'dev', '个人工作分支')")

    # 预置 MCP 服务器
    c.execute("""INSERT OR IGNORE INTO mcp_servers (name, endpoint, tools, status, latency_ms)
                 VALUES ('MCP-建模服务', 'http://mbse-mcp:4001', '["create_element","layout","validate","read_model","update_element","delete_element","list_elements","apply_profile","export_model"]', 'online', 120)""")
    c.execute("""INSERT OR IGNORE INTO mcp_servers (name, endpoint, tools, status, latency_ms)
                 VALUES ('MCP-文档解析', 'http://doc-mcp:4002', '["parse","ocr","chunk","extract"]', 'online', 210)""")

    # 预置本体类型（O-3：补 MBSE 通用类型 需求/部件/功能 支撑 SysML 导入）
    #
    # ⚠️ 2026-10-05 修复（真缺陷，实测取证）：下面 `包含` 的 allowed_values 引用了 21 个
    # 系统元素族类型，而这里原先只种了 7 个 ⇒ 全新安装的本体自带 1 条 **high** 问题
    # `bad_dom_range`（定义域/值域指向不存在的实体类型，18 个名字），
    # 于是**新库的本体永远过不了发布门禁（high>0 拒绝发布）**，
    # 且 validate 因无快照回落到 current ⇒ 恒报 high=1。
    # 种子数据必须自洽：用到的类型就得种上（本体自己的规则就是这么判的）。
    for name, kind in [("载荷", "entity"), ("转发器", "entity"), ("天线", "entity"), ("TWTA", "entity"),
                        ("需求", "entity"), ("部件", "entity"), ("功能", "entity"),
                        # ── 以下为 `包含` 约束引用到的系统元素族（缺则本体 high>0，无法发布）
                        ("系统元素", "entity"), ("卫星系统", "entity"), ("卫星平台", "entity"),
                        ("有效载荷", "entity"), ("通信载荷", "entity"), ("地面段", "entity"),
                        ("用户段", "entity"), ("电源分系统", "entity"), ("姿轨控分系统", "entity"),
                        ("测控分系统", "entity"), ("热控分系统", "entity"), ("相控阵天线", "entity"),
                        ("功率放大器", "entity"), ("变频器", "entity"), ("滤波器", "entity"),
                        ("信关站", "entity"), ("测控站", "entity"), ("用户终端", "entity"),
                        ("包含", "relation"), ("满足", "relation"), ("追溯", "relation"), ("派生", "relation"),
                        ("SATISFIES", "relation"), ("连接", "relation"), ("执行", "relation"),
                        ("频段", "attribute"), ("带宽", "attribute"), ("EIRP", "attribute")]:
        c.execute("INSERT OR IGNORE INTO ontology_types (name, type_kind) VALUES (?,?)", (name, kind))

    # P2-C：关系方向约束（SysML satisfy 语义：任意系统元素 → 需求）
    # 注意：INSERT OR IGNORE 不会覆盖既有记录，故对已有库需 UPDATE（见下方兜底）；
    # 此处约束仅对新库生效，旧库通过 _fix_ontology_constraints 修正（database/__init__.py）。
    import json as _json
    _sys_elements = ["部件", "载荷", "转发器", "天线", "TWTA", "功能"]
    c.execute(
        "UPDATE ontology_types SET constraints=? WHERE name='满足' AND type_kind='relation' AND "
        "(constraints IS NULL OR constraints='{}' OR constraints LIKE '%\"src\": \"转发器\"%')",
        (_json.dumps({"required": [], "unique": [], "desc": "satisfy 满足：任意系统元素满足需求",
                      "allowed_values": {"src": _sys_elements, "tgt": ["需求"]}}, ensure_ascii=False),))
    c.execute(
        "UPDATE ontology_types SET constraints=? WHERE name='SATISFIES' AND type_kind='relation' AND "
        "(constraints IS NULL OR constraints='{}' OR constraints LIKE '%\"src\": \"TWTA\"%')",
        (_json.dumps({"required": [], "unique": [], "desc": "satisfy 满足（英文别名）",
                      "allowed_values": {"src": _sys_elements, "tgt": ["需求"]}}, ensure_ascii=False),))
    # P2-A：connect 连接——任意系统元素可互连（SysML connect 语义）
    c.execute(
        "UPDATE ontology_types SET constraints=? WHERE name='连接' AND type_kind='relation' AND "
        "(constraints IS NULL OR constraints='{}' OR constraints LIKE '%\"src\": \"载荷\"%')",
        (_json.dumps({"required": [], "unique": [], "desc": "connect 连接：任意系统元素互连",
                      "allowed_values": {"src": _sys_elements, "tgt": _sys_elements}}, ensure_ascii=False),))
    # P2-C2：包含（composition）——组合/包含语义，与英文 CONTAINS 对齐（系统元素包含子元素）。
    # 历史窄约束 src='卫星'/tgt='载荷' 会拒绝 AI 生成模型的「载荷系统 包含 转发器」类组合，
    # 统一放宽到系统元素族（覆盖 build_satcom 的 CONTAINS 约束集合）。
    _sys_src = ["系统元素", "卫星系统", "卫星平台", "有效载荷", "通信载荷", "载荷", "地面段", "用户段", "部件"]
    _sys_tgt = ["系统元素", "卫星平台", "电源分系统", "姿轨控分系统", "测控分系统", "热控分系统",
                "有效载荷", "通信载荷", "载荷", "转发器", "天线", "相控阵天线", "功率放大器",
                "变频器", "滤波器", "TWTA", "信关站", "测控站", "用户终端", "部件", "需求"]
    c.execute(
        "UPDATE ontology_types SET constraints=? WHERE name='包含' AND type_kind='relation' AND "
        "(constraints IS NULL OR constraints='{}' OR constraints LIKE '%\"src\": \"卫星\"%')",
        (_json.dumps({"required": [], "unique": [], "desc": "包含（composition，组合/包含语义，与 CONTAINS 对齐）",
                      "allowed_values": {"src": _sys_src, "tgt": _sys_tgt}}, ensure_ascii=False),))

    # 预置知识实体（示例数据）
    entities = [
        ("ENT-001", "宽带通信载荷", "载荷", '{"band":"V","throughput":"2Gbps"}', "reviewed", "release"),
        ("ENT-002", "转发器", "转发器", '{"band":"V","channels":48}', "reviewed", "release"),
        ("ENT-003", "相控阵天线", "天线", '{"gain":"38dBi","beams":64}', "candidate", "dev"),
        ("ENT-004", "TWTA", "TWTA", '{"eirp":"62dBW","power":"200W"}', "reviewed", "release"),
        ("REQ-BC-001", "REQ-BC-001 吞吐能力", "需求", '{"text":"系统应在V波段提供不小于2Gbps的用户链路吞吐能力","source":"任务书§3.2","score":92}', "candidate", "dev"),
        ("REQ-BC-002", "REQ-BC-002 并发接入", "需求", '{"text":"系统应支持不少于100万用户的并发接入","source":"任务书§3.4","score":88}', "candidate", "dev"),
        ("REQ-A-117", "REQ-A-117 并发接入", "需求", '{"text":"单星并发接入≥50万","source":"历史基线"}', "reviewed", "release"),
        # 以下行用于满足 relations 复合外键 (id, branch) 引用（跨分支候选关系）
        ("ENT-001", "宽带通信载荷", "载荷", '{}', "candidate", "dev"),
        ("ENT-004", "TWTA", "TWTA", '{}', "candidate", "dev"),
        ("REQ-A-117", "REQ-A-117 并发接入", "需求", '{}', "candidate", "dev"),
    ]
    for e in entities:
        c.execute("INSERT OR IGNORE INTO entities (id, name, entity_type, properties, status, branch) VALUES (?,?,?,?,?,?)", e)

    # 预置关系
    relations = [
        ("ENT-001", "ENT-002", "CONTAINS", "reviewed", "release"),
        ("ENT-001", "ENT-003", "CONTAINS", "candidate", "dev"),
        ("ENT-002", "ENT-004", "CONTAINS", "reviewed", "release"),
        ("ENT-001", "ENT-004", "CONTAINS", "reviewed", "release"),
        ("ENT-004", "REQ-BC-001", "SATISFIES", "candidate", "dev"),
        ("ENT-003", "REQ-BC-002", "SATISFIES", "candidate", "dev"),
        ("ENT-002", "REQ-A-117", "SATISFIES", "reviewed", "release"),
        ("REQ-BC-002", "REQ-A-117", "CONFLICTS", "candidate", "dev"),
    ]
    for s, t, r, st, br in relations:
        c.execute("INSERT OR IGNORE INTO relations (source_id, target_id, relation_type, status, branch) VALUES (?,?,?,?,?)", (s, t, r, st, br))

    # 预置对话
    c.execute("""INSERT OR IGNORE INTO conversations (id, title, intent, user_id, phase)
                 VALUES (1, '宽带通信需求分析', 'requirement_analysis', 1, 'change')""")

    # 预置提示词
    c.execute("""INSERT OR IGNORE INTO prompts (name, scenario, content, variables, version, status, created_by)
                 VALUES ('需求分析-宽带通信', 'BR-3 需求分析',
                 '你是网络总体MBSE需求分析助手。领域本体：{{ontology_profile}}\\n约束：1)输出条目化需求；2)标注来源追溯；3)引用互联数据：{{linked_data_scope}}；4)不确定输出[TBD]',
                 '["ontology_profile","linked_data_scope","output_schema"]', 'v5', 'published', '王工')""")

    # 预置生成历史
    c.execute("""INSERT OR IGNORE INTO generation_history (id, conversation_id, gen_type, title, element_count, version, status, confirmed_by)
                 VALUES (1, 1, 'requirement', '宽带通信需求分析', 28, 'gen-v12', 'confirmed', '王工')""")

    # 预置审计日志
    logs = [
        ("王工", "llm_chat", "需求分析会话 #2055 · 生成28条候选", "success"),
        ("王工", "upload", "任务书_v2.3.docx (2.1MB)", "success"),
        ("李工", "branch_merge", "dev/窄带→release", "success"),
        ("陈专", "permission_denied", "访问分支合并评审(无权限)", "blocked"),
    ]
    for l in logs:
        c.execute("INSERT OR IGNORE INTO audit_logs (user_name, event_type, detail, result) VALUES (?,?,?,?)", l)

    # 预置设置
    settings = [
        ("system_name", "AI赋能MBSE系统设计工具", "系统名称"),
        ("default_branch", "dev", "默认工作分支"),
        ("query_route", "graph_first", "查询路由策略"),
        ("graph_confidence_threshold", "0.75", "图谱置信度降级阈值"),
        ("max_conversation_context", "20", "对话上下文最大轮数"),
    ]
    for k, v, d in settings:
        c.execute("INSERT OR IGNORE INTO settings (key, value, description) VALUES (?,?,?)", (k, v, d))

    conn.commit()


def _seed_projects(conn):
    """项目 / 场景模板 / 本体 Profile 种子（P0-1 平台化底座）。

    独立于 _seed 的全库种子：projects 表为空才执行，因此老库升级也能获得
    默认项目上下文，且不触碰用户已建数据。默认项目「星网宽带通信」与既有
    种子实体（ENT-001…）绑定，保证升级后前端知识图谱数据不丢。
    新领域接入 = 新建模板 + 新建项目，不再改代码。
    """
    c = conn.cursor()
    if c.execute("SELECT COUNT(*) FROM projects").fetchone()[0] > 0:
        return

    # ── 场景模板（3 个星网场景，示范模板化）──
    c.execute("""INSERT INTO scenario_templates (id, name, code, domain, description, entities_schema, relations_schema, views_schema, version, status)
                 VALUES ('template-satnet-broadband', '星网宽带通信', 'satnet-broadband', '卫星通信',
                 '宽带载荷 + 转发器 + 相控阵天线 + TWTA 系统方案设计',
                 '["载荷","转发器","天线","TWTA","需求"]',
                 '["CONTAINS","SATISFIES","TRACES","CONFLICTS"]',
                 '["需求图","用例图","BDD","IBD","参数图"]', 'v1', 'active')""")
    c.execute("""INSERT INTO scenario_templates (id, name, code, domain, description, entities_schema, relations_schema, views_schema, version, status)
                 VALUES ('template-satnet-narrowband', '星网窄带通信', 'satnet-narrowband', '卫星通信',
                 '窄带物联网回传 + 低速率终端接入',
                 '["终端","网关","信关站","窄带载荷","需求"]',
                 '["CONTAINS","SATISFIES","TRACES"]',
                 '["需求图","用例图","BDD","IBD"]', 'v1', 'active')""")
    c.execute("""INSERT INTO scenario_templates (id, name, code, domain, description, entities_schema, relations_schema, views_schema, version, status)
                 VALUES ('template-satnet-navigation', '星网导航增强', 'satnet-navigation', '卫星导航',
                 '导航增强信号 + 完好性监测',
                 '["导航载荷","原子钟","上变频器","监测站","需求"]',
                 '["CONTAINS","SATISFIES","TRACES"]',
                 '["需求图","用例图","BDD","IBD"]', 'v1', 'active')""")

    # ── 本体 Profile（同一模板可有多套本体裁剪视图）──
    c.execute("""INSERT INTO ontology_profiles (id, name, profile_type, scenario_template_id, entities, relations, attributes, description, status)
                 VALUES ('profile-satnet-kb', '星网知识库本体', 'domain', 'template-satnet-broadband',
                 '[{"type":"载荷","props":["band","throughput"]},{"type":"转发器","props":["channels"]},{"type":"天线","props":["gain","beams"]},{"type":"TWTA","props":["eirp","power"]},{"type":"需求","props":["text","source"]}]',
                 '[{"type":"CONTAINS","src":"部件","tgt":"部件"},{"type":"SATISFIES","src":"部件","tgt":"需求"}]',
                 '["频段","带宽","EIRP","信道数"]',
                 '面向需求分析与设计评审的知识库本体', 'active')""")
    c.execute("""INSERT INTO ontology_profiles (id, name, profile_type, scenario_template_id, entities, relations, attributes, description, status)
                 VALUES ('profile-satnet-sysml', 'SysML V2 映射本体', 'discipline', 'template-satnet-broadband',
                 '[{"type":"载荷","props":[]},{"type":"转发器","props":[]},{"type":"天线","props":[]}]',
                 '[{"type":"CONTAINS","src":"部件","tgt":"部件"},{"type":"SATISFIES","src":"部件","tgt":"需求"}]',
                 '[]',
                 '面向 SysML V2 模型导出的映射本体', 'active')""")

    # ── 项目（默认项目绑定现有种子实体，示范跨项目隔离）──
    c.execute("""INSERT INTO projects (id, name, code, domain, description, scenario_template_id, ontology_profile_id, status)
                 VALUES ('project-satnet-broadband', '星网宽带通信系统', 'satnet-broadband', '卫星通信',
                 '星网宽带通信载荷总体设计（默认项目，兼容既有数据）',
                 'template-satnet-broadband', 'profile-satnet-kb', 'active')""")
    c.execute("""INSERT INTO projects (id, name, code, domain, description, scenario_template_id, ontology_profile_id, status)
                 VALUES ('project-satnet-narrowband', '星网窄带通信系统', 'satnet-narrowband', '卫星通信',
                 '星网窄带物联网回传系统（模板示范，空项目）',
                 'template-satnet-narrowband', NULL, 'active')""")
    c.execute("""INSERT INTO projects (id, name, code, domain, description, scenario_template_id, ontology_profile_id, status)
                 VALUES ('project-satnet-navigation', '星网导航增强系统', 'satnet-navigation', '卫星导航',
                 '星网导航增强系统（模板示范，空项目）',
                 'template-satnet-navigation', NULL, 'active')""")

    # 默认项目持久化（前端 / 后端统一读取）
    c.execute("""INSERT OR REPLACE INTO settings (key, value, description)
                 VALUES ('default_project_id', '', '默认项目（P0-1 项目上下文隔离；2026-09-20 起默认不预设）')""")

    conn.commit()


def _seed_agents(conn):
    """Agent 注册表种子（P0 平台化）：agents 表空时写入存量 5+2 Agent 与工具绑定。

    幂等：仅 agents 表无数据时执行；存量 Agent 绑定与旧 AgentRegistry.DEFINITIONS 完全等价，
    保证会话主入口路由行为零变化（requirement_analysis=L2 / design·impact·review=L1 / chat=L0）。
    """
    c = conn.cursor()
    if c.execute("SELECT COUNT(*) FROM agents").fetchone()[0] > 0:
        return

    default_agents = [
        # (name, display_name, description, system_prompt, hil, kb, keywords, icon)
        ("requirement_analysis", "需求分析Agent",
         "解析输入文本，提取条目化需求并标注来源追溯",
         "", "L2", 1,
         '["需求", "解析", "条目", "需求分析", "requirement"]', "📋"),
        ("design", "方案设计Agent",
         "基于需求与互联数据生成多方案架构设计",
         "", "L1", 1,
         '["方案", "设计", "架构", "方案设计", "design"]', "🎨"),
        ("impact", "变更影响Agent",
         "分析变更源对模型的影响范围（BFS 遍历产出影响图）",
         "", "L1", 0,
         '["变更", "影响", "impact", "change", "变更影响"]', "🔀"),
        ("review", "预评审Agent",
         "对当前模型做规范性/一致性/合理性校验",
         "", "L1", 0,
         '["校验", "评审", "检查", "预评审", "review", "validate"]', "✅"),
        ("report_generation", "报告生成Agent",
         "基于模型与检索结果生成结构化分析报告",
         "", "L0", 1,
         '["报告", "文档", "汇报", "导出", "生成报告", "report"]', "📄"),
        ("knowledge_qa", "知识问答Agent",
         "基于知识库图谱与向量双引擎回答领域问题",
         "", "L0", 1,
         '["知识库", "资料", "文档里", "查一下", "@", "knowledge", "检索"]', "🧠"),
        ("chat", "通用问答Agent",
         "纯问答直出，不打断用户（HIL L0）",
         "", "L0", 0,
         '[]', "💬"),
    ]
    for name, display, desc, sp, hil, kb, kw, icon in default_agents:
        c.execute(
            "INSERT INTO agents (name, display_name, description, system_prompt, hil_level, kb_required, intent_keywords, icon, builtin) "
            "VALUES (?,?,?,?,?,?,?,?,1)",
            (name, display, desc, sp, hil, kb, kw, icon),
        )

    # Agent 工具绑定（等价旧 DEFINITIONS.tools）
    bindings = {
        "requirement_analysis": [("tool", "graph_retrieve"), ("tool", "entity_create"), ("tool", "conflict_check")],
        "design": [("tool", "graph_retrieve"), ("tool", "entity_create")],
        "impact": [("tool", "graph_retrieve"), ("tool", "impact_analyze")],
        "review": [("tool", "graph_retrieve"), ("tool", "validate")],
    }
    for name, tools in bindings.items():
        agent_id = c.execute("SELECT id FROM agents WHERE name=?", (name,)).fetchone()["id"]
        for ttype, tname in tools:
            c.execute(
                "INSERT OR IGNORE INTO agent_tools (agent_id, tool_type, tool_name) VALUES (?,?,?)",
                (agent_id, ttype, tname),
            )

    conn.commit()
    print(f"[init_db] 种子: agents {len(default_agents)} 个 + 工具绑定 {sum(len(v) for v in bindings.values())} 条")


def _seed_builtin_tools(conn) -> None:
    """TR-P2a：内置工具迁库（无条件幂等执行，行业标准：工具注册表唯一事实源）。

    内置工具不再是代码写死的隐藏集合，而是 tools 表中的 builtin 行——
    维护面板可查看/编辑元数据/停用，删除被禁止。
    """
    builtin_tools = [
        # name, description, source, side_effect, risk_level, version, input_schema
        ("graph_retrieve", "知识图谱检索（GraphRAG：实体链接 + 子图遍历 + 向量降级）", "builtin",
         "read", "low", "v1.0",
         '{"type":"object","properties":{"query":{"type":"string","description":"检索查询词"}},"required":["query"]}'),
        ("conflict_check", "冲突检测（新元素与既有元素属性冲突）", "builtin",
         "read", "low", "v1.0",
         '{"type":"object","properties":{"name":{"type":"string"},"attributes":{"type":"object"}},"required":["name"]}'),
        ("impact_analyze", "变更影响分析（BFS 三层遍历，产出影响图）", "builtin",
         "read", "medium", "v1.0",
         '{"type":"object","properties":{"source":{"type":"string","description":"变更源实体"}},"required":["source"]}'),
        ("validate", "模型预评审校验（规范性/一致性/合理性）", "builtin",
         "read", "low", "v1.0",
         '{"type":"object","properties":{"target":{"type":"string","description":"校验对象"}},"required":["target"]}'),
        ("entity_create", "创建知识实体（人工确认后入库）", "builtin",
         "write", "medium", "v1.0",
         '{"type":"object","properties":{"name":{"type":"string"},"etype":{"type":"string"},"attrs":{"type":"object"}},"required":["name"]}'),
    ]
    for tname, tdesc, tsrc, tside, trisk, tver, tschema in builtin_tools:
        conn.execute(
            "INSERT OR IGNORE INTO tools (name, source, description, status, input_schema, version, side_effect, risk_level, builtin) "
            "VALUES (?,?,?,?,?,?,?,?,1)",
            (tname, tsrc, tdesc, "active", tschema, tver, tside, trisk),
        )
    # 基础通用文件操作工具（file_*：系统文件读写/增删，定义见 file_tools.py，与 agent/workflow 双链路共用）
    try:
        from file_tools import FILE_TOOL_DEFS
        for d in FILE_TOOL_DEFS:
            conn.execute(
                "INSERT OR IGNORE INTO tools (name, source, description, status, input_schema, version, side_effect, risk_level, builtin) "
                "VALUES (?,?,?,?,?,?,?,?,1)",
                (d["name"], "builtin", d["description"], "active",
                 json.dumps(d["input_schema"], ensure_ascii=False),
                 d["version"], d["side_effect"], d["risk_level"]),
            )
        print(f"[init_db] 内置文件工具迁库: {len(FILE_TOOL_DEFS)} 个")
    except Exception as e:
        print(f"[init_db] ⚠️ 文件工具注册失败（忽略）: {e}")
    # 基础通用报告导出工具（report_export：内容→md/docx/pdf，定义见 report_tools.py，双链路共用）
    try:
        from report_tools import REPORT_TOOL_DEFS
        for d in REPORT_TOOL_DEFS:
            conn.execute(
                "INSERT OR IGNORE INTO tools (name, source, description, status, input_schema, version, side_effect, risk_level, builtin) "
                "VALUES (?,?,?,?,?,?,?,?,1)",
                (d["name"], "builtin", d["description"], "active",
                 json.dumps(d["input_schema"], ensure_ascii=False),
                 d["version"], d["side_effect"], d["risk_level"]),
            )
        print(f"[init_db] 内置报告工具迁库: {len(REPORT_TOOL_DEFS)} 个")
    except Exception as e:
        print(f"[init_db] ⚠️ 报告工具注册失败（忽略）: {e}")
    conn.commit()
    print(f"[init_db] 内置工具迁库: {len(builtin_tools)} 个")


# ═══════════════ P0-1/P2-2/P2-3：Glossary / Trace / Review 队列 ═══════════════
def _backfill_domains(conn):
    """P0-2：老库文档 domain 回填——按文件名规则自动打标（与 glossary.infer_domain 同规则）。
    仅回填从未打标（domain_confidence=0）的文档；高置信直接写库，低置信进 review 队列。
    幂等：domain_confidence>0 视为已处理，不会重复回填/重复进队列。
    """
    from glossary import infer_domain, log_domain_review
    rows = conn.execute(
        "SELECT id, filename FROM documents "
        "WHERE (domain='unknown' OR domain='') AND (domain_confidence=0 OR domain_confidence IS NULL)"
    ).fetchall()
    updated = 0
    for r in rows:
        domain, score = infer_domain(filename=r["filename"], return_score=True)
        if score < 0.9:
            log_domain_review(conn, r["id"], r["filename"], domain, score,
                              "回填自动分类置信度不足")
        conn.execute("UPDATE documents SET domain=?, domain_confidence=? WHERE id=?",
                     (domain, score, r["id"]))
        conn.execute("UPDATE document_chunks SET domain=? WHERE document_id=?",
                     (domain, r["id"]))
        updated += 1
    if updated:
        conn.commit()
        print(f"[init_db] 文档 domain 回填: {updated} 份")


def _seed_glossary(conn):
    """Glossary 种子（空表时插入预置术语，验证机制通用性）。幂等。"""
    n = conn.execute("SELECT COUNT(*) AS n FROM glossary").fetchone()["n"]
    if n:
        return
    seeds = [
        # (user_term, canonical_term, domain, intent, boost, description)
        ("v2", "SysML_V2", "sysml_norm", "knowledge_qa", 1.8, "SysML V2 建模语言（区别于 V波段通信频段）"),
        ("sysml", "SysML", "sysml_norm", "knowledge_qa", 1.5, "系统建模语言"),
        ("代码规范", "SysML_V2 语法规范", "sysml_norm", "knowledge_qa", 1.5, "代码规范 = SysML 语法规范"),
        ("语法规范", "SysML_V2 语法规范", "sysml_norm", "knowledge_qa", 1.5, ""),
        ("ibd", "内部块图_IBD", "sysml_norm", "knowledge_qa", 1.5, "内部块图（Internal Block Diagram）"),
        ("bdd", "模块定义图_BDD", "sysml_norm", "knowledge_qa", 1.5, "模块定义图（Block Definition Diagram）"),
        ("tms", "热管理系统_TMS", "thermal_mgmt", "knowledge_qa", 1.5, "热管理系统（Thermal Management System）"),
        ("v波段", "V波段_卫星通信频段", "satellite_comms", "", 1.0, "通信频段（与 SysML V2 无关，保底映射）"),
        ("宽带", "宽带通信", "satellite_comms", "", 1.0, "宽带通信领域"),
        ("转发器", "转发器", "satellite_comms", "", 1.0, "卫星转发器"),
    ]
    for user_term, canonical, domain, intent, boost, desc in seeds:
        conn.execute(
            "INSERT OR IGNORE INTO glossary (user_term, canonical_term, domain, intent, boost, description) "
            "VALUES (?,?,?,?,?,?)",
            (user_term, canonical, domain, intent, boost, desc),
        )
    conn.commit()


# ── 意图路由规则集种子（2026-10-04 入库）────────────────────────────────────
# 为什么规则集要入库：原先 4 条规则只存在于运行库的 intent_rules 表里，不在 git。
# 实测后果：干净库 init_db 后该表 **0 行** ⇒ CI 的意图路由评测复现不出生产行为
# （实测黄金集准确率 0.828，"生成结构树"/"画一张参数图"/"追溯矩阵检查一下" 三条全错），
# 规则改动/丢失在 CI 上无法察觉。规则集应当是**随代码走的版本化制品**
# （对标 LangChain prompt / Dify dataset 的做法）。
#
# 语义：**只补不覆盖** —— 只插缺失的 (trigger, intent)，
# 用户在设置页改过的规则不会被种子冲掉（与 _seed_glossary 同款语义）。
def _seed_intent_rules(conn) -> int:
    """把 seeds_data/intent_rules.json 里的规则补进 intent_rules（幂等，不覆盖已有）。

    返回本次新插入的行数。文件缺失/损坏 ⇒ 返回 0 并静默（**种子失败绝不能让服务起不来**）。
    """
    import json as _json
    import os as _os
    # ⚠️ 路径：`__file__` = database/seeds.py ⇒ 只需**退一级**到 database/，
    #    再拼 seeds_data/。（初版写成退两级 ⇒ 指向仓库根，文件恒不存在 ⇒ 静默种 0 条，
    #    而这个"静默"恰好把 bug 藏了三天 —— 由真实数据 A/B 验证抓出。
    #    教训：种子/兜底逻辑的"什么都没做"必须**可观测**，否则它和"成功但无需做"无法区分。）
    p = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "seeds_data", "intent_rules.json")
    if not _os.path.exists(p):
        print("[seed] ⚠️ 意图规则种子文件缺失：%s（规则未播种，意图路由会少一批关键词）" % p,
              flush=True)
        return 0
    try:
        with open(p, encoding="utf-8") as f:
            data = _json.load(f)
        rules = data.get("rules") or []
    except Exception as e:
        print("[seed] ⚠️ 意图规则种子读取失败（不影响启动）：%s | %s" % (str(e)[:100], p), flush=True)
        return 0
    n = 0
    for r in rules:
        trig = (r.get("trigger") or "").strip()
        intent = (r.get("intent") or "").strip()
        if not trig or not intent:
            continue
        exists = conn.execute(
            "SELECT 1 FROM intent_rules WHERE trigger=? AND intent=? LIMIT 1",
            (trig, intent)).fetchone()
        if exists:
            continue
        conn.execute(
            "INSERT INTO intent_rules (trigger, intent, weight, enabled, created_by, created_at) "
            "VALUES (?,?,?,1,?,datetime('now'))",
            (trig, intent, float(r.get("weight") or 20.0),
             "seed:%s" % (r.get("source") or "builtin")))
        n += 1
    conn.commit()
    # 无条件打印"已检查 + 结果"，让"种了 0 条"和"文件读不到"在日志里可区分
    print("[seed] 意图路由规则：文件 %d 条，本次新增 %d 条（已存在的 %d 条不覆盖）"
          % (len(rules), n, len(rules) - n), flush=True)
    return n
