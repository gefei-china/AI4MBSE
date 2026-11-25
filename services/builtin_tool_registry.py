"""内置工具**注入清单** —— 唯一真源（P1 收敛，2026-11-25）。

从 `agent/pipeline_parts/tools.py` 移出：它只是纯字典，判绑定是否生效的
`services/capability_graph` 与注入侧必须共用同一份键集合，原来却要反向依赖 agent。
原路径保留 re-export；新增/停用内置工具仍只改这里。
"""

BUILTIN_DESC = {
    "graph_retrieve": "知识图谱检索（GraphRAG：实体链接 + 子图遍历 + 向量降级），参数 query:str",
    # ⚠️ 2026-10-13 已移除 `conflict_check`（假实现，无条件返回"无新冲突"）。
    #    连带移除本登记：留着会让它继续进 payload，却已无执行器 ⇒ 模型拿到一个必败工具。
    "impact_analyze": "变更影响分析（BFS 遍历产出影响图）",
    "validate": "模型预评审校验（规范性/一致性/合理性）",
    "entity_create": "创建知识实体（人工确认后入库）",
    # 基础通用文件操作工具（系统文件读写/增删，定义见 file_tools.py）
    "file_list": "列出目录下的文件与子目录（参数 path:str，recursive:bool 可选）",
    "file_read": "读取文本文件内容（UTF-8，参数 path:str，max_chars:int 可选）",
    "file_write": "创建或覆盖写入文本文件（UTF-8 自动建父目录，参数 path:str, content:str；写操作）",
    "file_append": "追加文本到文件末尾（不存在则创建，参数 path:str, content:str；写操作）",
    "file_mkdir": "创建目录（参数 path:str；写操作）",
    "file_delete": "删除文件或空目录（参数 path:str，recursive:bool 可选；destructive 高风险需人工确认）",
    # 基础通用报告导出工具（内容→md/docx/pdf，定义见 report_tools.py）
    "report_export": "把报告内容导出为指定格式文档并落盘（参数 path:str, title:str, content:str 或 sections:str, fmt:md|docx|pdf；写操作）",
    # ★ 2026-10-17（米爸能力配置）：save_artifact——把内容显式登记进**产物库**
    #   （file_write 只写磁盘进不了产物库；参数 title:str, content:str,
    #   kind:code|document|sysml|report 可选, lang:str 可选；写操作）
    "save_artifact": "把代码/文档全文保存为会话产物（入产物库，用户可在产物面板查看下载；参数 title:str, content:str, kind:str 可选, lang:str 可选；写操作）",
    # ★ 2026-10-17（米爸需求）：联网搜索/网页抓取（定义见 web_tools.py）
    "web_search": "联网搜索互联网最新消息（参数 query:str, max_results:int 可选；时效性问题——最新/今天/新闻/发布——必用本工具，不要凭训练记忆回答时效问题）",
    "web_fetch": "抓取指定网页的正文文本（参数 url:str, max_chars:int 可选；配合 web_search 结果链接深读）",
    # ★ 2026-11-06（P0：把"有实现但没接线"的能力接上）
    #   ⚠️ 必须登记在这里：`_build_tools_def` 用它做 `if name in BUILTIN_DESC` 判定，
    #   漏登记 = 绑定了也不注入（历史上 save_artifact 就踩过这个坑）。
    "code_exec": "在隔离沙箱执行 Python/JS 代码并返回 _out 字典（参数 code:str, language:python|js, inputs:object, timeout:number≤30；无文件/网络/子进程，仅可 import json/math/re 等）",
    "doc_parse": "提取 PDF/Word .docx/Excel .xlsx/CSV 的正文与表格文本（参数 path:str, max_chars:int, pages:如'1-5', sheets:str, max_rows:int；纯文本请用 file_read）",
    # ★ 2026-11-07（P1）：技能脚本执行（定义见 skill_tools.py）
    #   技能正文写「⚙ 脚本（需要时执行）」时用本工具真正跑起来；脚本是平台受信脚本，
    #   有文件/网络能力 ⇒ side_effect=write（会被写前确认闸覆盖）。
    "skill_script_run": "执行技能包自带的脚本（参数 skill:技能名, script:默认scripts/main.py, args:object 传给run(**args), argv:字符串数组当命令行参数, timeout:秒≤120；脚本只允许技能manifest声明过的路径）",
    # ★ 2026-11-07（P2）：元能力（定义见 meta_tools.py）
    "capability_query": "查询平台能力资产：Agent/技能/工具/AI自注册能力的清单、详情、绑定差异（参数 kind:agent|skill|tool|capability, action:list|get|diff, target, keyword；只读）",
    "capability_apply": "新增/修改/绑定/解绑/启停/删除能力资产（参数 kind, action:create|update|bind|unbind|enable|disable|delete, target, spec:object, reason；**全部走人工确认，批准前不改动任何东西**）",
    # ★ 2026-11-07（P0-1）：需求条目化 / 需求条目入库
    #   ⚠️ 这两个名字**不带任何既有前缀**，且 `requirement_itemize_tools.py` 此前
    #   **没有被任何分发点 import**（2026-11-07 实测：46 次真实调用全部返回「未知工具」，
    #   而 tools 表里有行、技能正文还在教模型调用它 —— 「注册了没接线」的第三次同型事故）。
    #   读/写分离：itemize 只读（每轮都跑），commit 落库（写 ⇒ 走写前确认闸）。
    "requirement_itemize": "把自然语言需求按句切分为可验证条目（分类 functional/performance/interface/constraint，标 reqId 与来源），不可验证句归入 gaps 并说明原因，同时生成 SysML v2 需求视图骨架代码（参数 text:整段原文, domain:str, source_ref:str, project:标识符；只读，不落库）",
    "requirement_commit": "把需求条目写入需求实体库（参数 items:条目数组 或 text:原文由本工具先条目化, gaps:数组, project:工程标识, version_id:int, source_ref:str；**幂等**：同文重复提交只更新不新增；写操作，需人工确认）",
}
