"""基础通用技能注册脚本（幂等，可重复执行）。

注册两个「基础通用」已发布技能：
1. 文件操作（file_ops）    —— 系统文件读写/增删的基础通用技能（配合 file_* 内置工具）
2. 行业调研（industry_research）—— 调研行业通用方案：行业方案调研分析报告生成方法论

技能包结构（对齐 Dify/Claude Code 技能包规范，落到 static/skill_packages/）：
- skill.md（frontmatter + 指令正文）
- scripts/main.py（示例脚本，渐进披露供按需执行）
- references/*.md（参考文档，渐进披露供按需读取）

用法： python register_skills.py
"""
import json
import os
import uuid

from core.config import DB_PATH, STATIC_DIR
from database import get_db

SKILL_PACKAGES = [
    {
        "name": "文件操作",
        "description": "系统文件读写/增删基础通用能力：列目录、读文本、写/追加文件、建目录、删除（destructive 需人工确认），配合 file_* 内置工具使用",
        "skill_type": "package",
        "category": "基础通用",
        "author": "平台内置",
        "version": "v1.0",
        "triggers": ["文件", "读写文件", "创建文件", "保存文件", "追加内容", "删除文件", "目录", "文件操作", "素材文件", "file_"],
        "allowed_tools": ["file_list", "file_read", "file_write", "file_append", "file_mkdir", "file_delete"],
        "references": [{"title": "文件操作安全规范", "path": "references/文件操作安全规范.md"}],
        "scripts": ["scripts/main.py"],
        "skill_md": """---
name: 文件操作
description: 系统文件读写/增删基础能力（file_* 工具使用规范）
triggers: [文件, 读写文件, 创建文件, 保存文件, 追加, 删除文件, 目录, 文件操作]
category: 基础通用
author: 平台内置
version: v1.0
---
# 文件操作技能

你负责系统文件的读写与增删（基础通用能力）。遵循以下规范：

## 工具映射
- 列目录 / 确认路径 → **file_list**（先 list 再操作，避免路径臆测）
- 读内容 → **file_read**（文本 UTF-8；超长自动截断，必要时分段读取）
- 新建 / 覆盖 → **file_write**（自动创建父目录）
- 追加 → **file_append**（文件不存在则创建）
- 建目录 → **file_mkdir**
- 删除 → **file_delete**（destructive 高风险，需人工确认；目录非空需显式 recursive=true）

## 安全规范
- 写前先 file_list / file_read 确认目标是否存在、是否会被覆盖
- 平台运行时关键文件（*.db 数据库、配置文件、main.py 等）受保护，拒绝写入/删除——遇到请如实向用户说明
- 路径用绝对路径；中文路径保持 UTF-8
- 删除目录前必须确认其内容；非空目录需显式 recursive=true 并经人工确认
- 单次读取上限约 20 万字符，超长文件分段读取或提示用户
""",
        "script_py": '''"""文件操作技能示例脚本（渐进披露参考，实际执行建议直接使用平台 file_* 工具）。"""
import os


def run(path: str, content: str = "", mode: str = "write") -> str:
    """通用读写封装示例：mode ∈ write | append | read。
    - 写前检查目标是否覆盖已有文件（安全习惯）
    - 平台关键文件（*.db / 配置文件 / main.py）拒绝写入
    """
    if not path:
        return "path 不能为空"
    ap = os.path.abspath(path)
    if ap.lower().endswith((".db", ".db-wal", ".db-shm", ".sqlite", ".sqlite3")) or \\
       os.path.basename(ap) in ("main.py", "config.py"):
        return f"拒绝写入关键文件: {ap}"
    if mode == "read":
        with open(ap, "r", encoding="utf-8", errors="replace") as f:
            return f.read()[:200_000]
    if mode not in ("write", "append"):
        return f"未知 mode: {mode}"
    if mode == "write" and os.path.exists(ap):
        return f"目标已存在，为避免误覆盖请改用 file_write 工具并确认：{ap}"
    os.makedirs(os.path.dirname(ap) or ".", exist_ok=True)
    flag = "w" if mode == "write" else "a"
    with open(ap, flag, encoding="utf-8") as f:
        f.write(content)
    return f"已{mode} {len(content)} 字符 → {ap}"
''',
        "reference_md": """# 文件操作安全规范

## 目标
为 Agent 与工作流提供系统文件读写/增删能力的同时，防止误操作破坏平台自身。

## 工具副作用与门控
| 工具 | 副作用 | 门控 |
|---|---|---|
| file_list / file_read | read | 任意路径直接执行 |
| file_write / file_append / file_mkdir | write | 写意图注入 + L2 HIL 人工确认 |
| file_delete | destructive | 必须人工确认（TR-P2b） |

## 保护清单（拒绝覆盖/删除）
- 平台数据库：*.db / *.db-wal / *.db-shm / *.sqlite
- 系统配置：core/config.py、~/.workbuddy/mbse_config.json
- 入口脚本：main.py
- 含上述文件的目录禁止递归删除

## 使用原则
1. 读先于写：操作前先 list/read 确认路径
2. 覆盖需确认：file_write 覆盖已有文件前先告知用户
3. 删除需确认：file_delete 一律经人工确认
4. 超长文件分段读取（单次上限 20 万字符）
""",
    },
    {
        "name": "行业调研",
        "description": "调研行业通用方案：行业方案调研分析报告生成方法论（对齐《AI Agent 平台搭建行业方案调研分析报告》），产出概览对比/模块深度/行业实践/选型建议的结构化报告",
        "skill_type": "package",
        "category": "行业通用",
        "author": "平台内置",
        "version": "v1.0",
        "triggers": ["行业调研", "调研报告", "行业方案", "方案对比", "竞品分析", "市场格局", "选型建议", "对标分析", "调研分析", "方案调研"],
        "allowed_tools": ["graph_retrieve", "file_list", "file_read", "file_write", "file_append", "file_mkdir"],
        "references": [{"title": "行业调研报告撰写指南", "path": "references/行业调研报告撰写指南.md"}],
        "scripts": ["scripts/main.py"],
        "skill_md": """---
name: 行业调研
description: 行业方案调研分析报告生成（对齐《AI Agent 平台搭建行业方案调研分析报告》方法论）
triggers: [行业调研, 调研报告, 行业方案, 方案对比, 竞品分析, 市场格局, 选型建议, 对标分析]
category: 行业通用
author: 平台内置
version: v1.0
---
# 行业调研技能

你是行业调研分析师。产出《行业方案调研分析报告》时遵循以下方法论：

## 报告标准结构（八段）
1. **题注**：标题 + 调研日期 + 覆盖方案数 + 引用来源数
2. **一、行业方案概览与对比**：市场格局总览（三足鼎立 + 多元生态，配 ASCII 图）+ 核心功能对比矩阵（markdown 表格：开源/可视化编排/RAG支持/多Agent协作/工作流引擎/模型支持/非技术友好/生产就绪/学习曲线）+ 性能基准参考
3. **模块深度解析**（每模块一节，六模块：Tools / MCP / Skills / Agent / 工作流 / 多Agent）：
   - Tools：工具注册与 Function Calling 机制 / 工具执行与错误处理 / 安全控制体系
   - MCP：协议架构与三层模型 / 与 Tools 的关系 / 应用场景与商业价值
   - Skills：技能定义与文件结构 / 渐进式披露与动态加载 / Skills vs Tools vs MCP
   - Agent：Observe-Think-Act 循环机制 / 四层记忆架构 / 规划与推理模式
   - 工作流：DAG 与状态机引擎 / 节点类型与执行流程 / 可视化设计器
   - 多Agent：通信模式 / 协调机制
4. **行业实践对比**
5. **选型建议与趋势展望**
6. **附：参考来源**（编号 [n] 列表）

## 质量要求
- 每个模块给出「行业共识 → 量化数据 → 对本平台差距 → 落地映射」四段式
- 量化数据标注来源编号 [n]；表格一律 markdown 表格
- 报告产出：先用 file_write 写出到用户指定路径，再向用户汇报核心要点（≤200 字）

## 工具使用
- file_read：读取用户提供的素材/输入文件
- graph_retrieve：检索知识库既有资料
- file_write / file_append：落盘报告
""",
        "script_py": '''"""行业调研报告生成器（示例脚本，渐进披露参考）。

用法：run(主题, 覆盖方案数=9, 引用来源数=15, 输出路径, fmt="md")
产出标准八段结构的行业方案调研分析报告；fmt ∈ md|docx|pdf（docx/pdf 复用平台通用报告服务）。
"""
import os
import sys
from datetime import date

_HERE = os.path.dirname(os.path.abspath(__file__))
for _ in range(4):
    _HERE = os.path.dirname(_HERE)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)


def _sections():
    return [
        {"heading": "目录", "body": "行业方案概览与对比 / 市场格局总览 / 核心功能对比矩阵 / 性能基准参考\\n模块深度解析（Tools / MCP / Skills / Agent / 工作流 / 多Agent）\\n行业实践对比 / 选型建议与趋势展望"},
        {"heading": "一、行业方案概览与对比", "body": "## 1.1 市场格局总览\\n（三足鼎立 + 多元生态：大模型厂商 / 开源框架 / 全栈平台）\\n\\n## 1.2 核心功能对比矩阵\\n| 维度 | 方案A | 方案B | 方案C |\\n|---|---|---|---|\\n| 开源 | | | |\\n| 可视化编排 | | | |\\n| RAG 支持 | | | |\\n| 多Agent协作 | | | |\\n| 工作流引擎 | | | |\\n| 模型支持 | | | |\\n| 非技术友好 | | | |\\n| 生产就绪 | | | |\\n\\n## 1.3 性能基准参考\\n（基于真实基准测试的成功率/延迟数据）"},
        {"heading": "二、模块深度解析", "body": "## 2.1 Tools 模块\\n- 工具注册与 Function Calling 机制\\n- 工具执行与错误处理\\n- 安全控制体系\\n\\n## 2.2 MCP 协议\\n- 协议架构与三层模型\\n- 与 Tools 的关系\\n- 应用场景与商业价值\\n\\n## 2.3 Skills 模块\\n- 技能定义与文件结构\\n- 渐进式披露与动态加载\\n- Skills vs Tools vs MCP\\n\\n## 2.4 Agent 模块\\n- Observe-Think-Act 循环机制\\n- 四层记忆架构\\n- 规划与推理模式\\n\\n## 2.5 工作流模块\\n- DAG 与状态机引擎\\n- 节点类型与执行流程\\n- 可视化设计器\\n\\n## 2.6 多Agent编排\\n- 通信模式\\n- 协调机制"},
        {"heading": "三、行业实践对比", "body": "| 场景 | 主流做法 | 推荐方案 | 理由 |\\n|---|---|---|---|"},
        {"heading": "四、选型建议与趋势展望", "body": "- 建议：分层可插拔架构（Tools/MCP 底座 → Skills 增强 → Agent/Workflow 编排）\\n- 趋势：MCP 成为工具集成行业标准"},
        {"heading": "附：参考来源", "body": "[1] "},
    ]


def run(topic: str, 覆盖方案数: int = 9, 引用来源数: int = 15,
        输出路径: str = "", fmt: str = "md") -> str:
    """生成行业调研报告文档。输出路径必填，fmt ∈ md|docx|pdf。"""
    topic = topic or "AI Agent 平台搭建"
    if not 输出路径:
        return "请提供输出路径（配合 file_* 工具落盘到用户指定目录）"
    from report_generator import report_generator
    head = f"行业方案调研分析报告\\n\\n> 调研日期：{date.today().isoformat()}　覆盖方案：{覆盖方案数} 个　引用来源：{引用来源数}+ 篇"
    sections = [{"heading": "题注", "body": head}] + _sections()
    report = {"title": topic, "sections": sections, "summary": "", "report_type": "analysis"}
    fmt = str(fmt or "md").lower().lstrip(".")
    if fmt not in ("md", "docx", "pdf"):
        return f"不支持的格式: {fmt}（仅支持 md/docx/pdf）"
    path = report_generator.export(report, fmt, 输出路径)
    return f"行业调研报告已生成 {fmt.upper()}（{len(sections)} 节）→ {path}"
''',
        "reference_md": """# 行业调研报告撰写指南

## 一、报告定位
《行业方案调研分析报告》：面向技术选型的行业级调研，覆盖方案概览、模块深度、行业实践与选型建议，为平台建设提供决策依据。

## 二、标准结构（八段）
1. 题注（标题/日期/覆盖方案数/引用来源数）
2. 一、行业方案概览与对比
   - 1.1 市场格局总览（阵营划分 + ASCII 结构图）
   - 1.2 核心功能对比矩阵（markdown 表格，≥8 维度）
   - 1.3 性能基准参考（成功率/延迟量化数据）
3. 模块深度解析（每模块一节，六模块全覆盖）
   - Tools：注册与 Function Calling / 执行与错误处理 / 安全控制
   - MCP：协议架构三层模型 / 与 Tools 关系 / 场景与价值
   - Skills：定义与文件结构 / 渐进式披露 / 与 Tools/MCP 对比
   - Agent：Observe-Think-Act / 四层记忆 / 规划推理
   - 工作流：DAG 与状态机 / 节点与执行 / 可视化设计器
   - 多Agent：通信模式 / 协调机制
4. 行业实践对比（场景 × 做法 × 推荐 × 理由）
5. 选型建议与趋势展望
6. 附：参考来源（编号引用 [n]）

## 三、质量要求
- 四段式模块分析：行业共识 → 量化数据（标注 [n]）→ 对本平台差距 → 落地映射
- 表格一律 markdown；结构图用 ASCII
- 结论必须有数据支撑，不写空话

## 四、产出方式
- 读素材：file_read（用户输入文件）
- 查资料：graph_retrieve（知识库）
- 写报告：file_write / file_append（用户指定路径）
- 汇报：完成后向用户简述核心要点（≤200 字）
""",
    },
    {
        "name": "报告生成",
        "description": "内容 → 对应格式文档（Markdown/Word/PDF）：结构化报告生成（类型体系/自定义大纲）+ 多格式导出 + file_* 工具落盘，报告可再编辑/正式交付",
        "skill_type": "package",
        "category": "基础通用",
        "author": "平台内置",
        "version": "v1.0",
        "triggers": ["报告", "生成报告", "导出报告", "报告下载", "Word文档", "PDF文档", "汇报材料", "文档导出", "生成文档", "格式文档", "正式文档", "报告导出"],
        "allowed_tools": ["graph_retrieve", "file_list", "file_read", "file_write", "file_append", "file_mkdir"],
        "references": [{"title": "报告格式规范", "path": "references/报告格式规范.md"}],
        "scripts": ["scripts/main.py"],
        "skill_md": """---
name: 报告生成
description: 内容 → 对应格式文档（Markdown/Word/PDF）：结构化报告生成 + 多格式导出 + file_* 工具落盘
triggers: [报告, 生成报告, 导出报告, 报告下载, Word文档, PDF文档, 汇报材料, 文档导出, 生成文档, 格式文档]
category: 基础通用
author: 平台内置
version: v1.0
---
# 报告生成技能

你负责把分析结果生成**结构化报告**并导出为**对应格式的正式文档**（Markdown / Word / PDF）。

## 流程
1. **定结构**：识别报告类型（模型分析 / 变更影响 / 预评审）或按用户指定大纲组织章节
2. **填内容**：每节聚焦生成——涉及数据用 markdown 表格，能用图表表达的比较数据保留表格
3. **导出文档**：
   - 用户要 Word → 生成 .docx（可再编辑、样式规范）
   - 用户要 PDF → 生成 .pdf（正式交付）
   - 默认/预览 → .md（前端渲染）
4. **落盘**：用 file_write/file_append 先把报告正文写成 .md；再用导出脚本把同一内容导出为 .docx/.pdf 到用户指定路径（scripts/main.py）

## 质量要求
- 报告必须包含：概述 → 现状/详细分析 → 结论 → 建议
- 数据必须有来源；结论不写空话
- 导出文件路径用绝对路径，中文文件名保留 UTF-8

## 工具使用
- file_read / graph_retrieve：收集素材
- file_write / file_append：写 .md 正文
- scripts/main.py：内容 → docx/pdf 导出（run(title, sections|markdown, fmt, 输出路径)）
""",
        "script_py": '''"""报告生成技能脚本：内容 → 对应格式文档（md/docx/pdf）。

复用平台通用报告服务 report_generator（同一数据契约 Report = {title, sections:[{heading, body, table?}], summary}）。
用法：
- run(title, markdown="整篇文本", fmt="md", 输出路径="...")  —— markdown 自动按 ## 分节
- run(title, sections=[{"heading","body"}], fmt="docx", 输出路径="...") —— 直接给结构化分节
"""
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
for _ in range(4):
    _HERE = os.path.dirname(_HERE)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)


def _sections_from_markdown(markdown):
    sections, cur = [], None
    for line in (markdown or "").splitlines():
        if line.startswith("## "):
            if cur:
                sections.append(cur)
            cur = {"heading": line[3:].strip(), "body": []}
        elif cur is not None:
            cur["body"].append(line)
    if cur:
        sections.append(cur)
    for s in sections:
        s["body"] = "\\n".join(s["body"]).strip()
    return sections or [{"heading": "正文", "body": (markdown or "").strip()}]


def run(title="报告", sections=None, markdown="", fmt="md", 输出路径=""):
    """生成指定格式的报告文档（md/docx/pdf）。输出路径必填。"""
    if not 输出路径:
        return "请提供输出路径（配合 file_* 工具落盘到用户指定目录）"
    if not sections:
        sections = _sections_from_markdown(markdown)
    from report_generator import report_generator
    report = {"title": title or "报告", "sections": sections or [],
              "summary": "", "report_type": "analysis"}
    fmt = str(fmt or "md").lower().lstrip(".")
    if fmt not in ("md", "docx", "pdf"):
        return f"不支持的格式: {fmt}（仅支持 md/docx/pdf）"
    path = report_generator.export(report, fmt, 输出路径)
    return f"已生成 {fmt.upper()} 报告（{len(sections)} 节）→ {path}"
''',
        "reference_md": """# 报告格式规范

## 支持格式
- Markdown（.md）：默认/预览/存档
- Word（.docx）：可再编辑、交付给用户
- PDF：正式对外交付

## 报告结构（统一数据契约）
Report = {title, sections:[{heading, body, table?}], summary, report_type}
- body 支持轻量 markdown：**粗体**、表格（| a | b |）、列表（- / 1.）、### 子标题
- report_type：analysis（模型分析）/ impact（变更影响）/ review（预评审）

## 分节模板
- 概述 → 现状分析 → 详细分析 → 结论 → 建议
- 或按用户指定大纲（如「按 背景/方案/结论 组织」）动态覆盖

## 导出规则
- 中文文件名保留 UTF-8；绝对路径落盘
- 数据表格尽量保留（docx/pdf 会渲染为正式表格，表头加底色）
- docx：标题/分节/列表/表格标准样式；pdf：A4 页面 + 系统中文字体
""",
    },
]


def _write_package(pkg: dict) -> str:
    """落盘技能包文件到 static/skill_packages/<uuid>/，返回目录路径。"""
    pkg_dir = os.path.join(STATIC_DIR, "skill_packages", uuid.uuid4().hex[:12])
    os.makedirs(os.path.join(pkg_dir, "scripts"), exist_ok=True)
    os.makedirs(os.path.join(pkg_dir, "references"), exist_ok=True)
    with open(os.path.join(pkg_dir, "skill.md"), "w", encoding="utf-8") as f:
        f.write(pkg["skill_md"])
    with open(os.path.join(pkg_dir, "scripts", "main.py"), "w", encoding="utf-8") as f:
        f.write(pkg["script_py"])
    for ref in pkg.get("references") or []:
        rel = ref.get("path", "")
        if not rel or "\\n" in rel:
            continue
        fname = os.path.basename(rel)
        with open(os.path.join(pkg_dir, "references", fname), "w", encoding="utf-8") as f:
            f.write(pkg.get("reference_md", ""))
    return pkg_dir


def main() -> None:
    conn = get_db()
    try:
        # 工具注册表同步：file_* 内置文件工具（幂等，见 database._seed_builtin_tools）
        try:
            from database import _seed_builtin_tools
            _seed_builtin_tools(conn)
        except Exception as e:
            print(f"[skills] ⚠️ 内置工具同步失败（忽略）: {e}")
        for pkg in SKILL_PACKAGES:
            name = pkg["name"]
            row = conn.execute("SELECT id, status FROM skills WHERE name=?", (name,)).fetchone()
            pkg_dir = _write_package(pkg)
            refs = json.dumps(pkg.get("references") or [], ensure_ascii=False)
            scripts = json.dumps(pkg.get("scripts") or [], ensure_ascii=False)
            triggers = json.dumps(pkg.get("triggers") or [], ensure_ascii=False)
            allowed_tools = json.dumps(pkg.get("allowed_tools") or [], ensure_ascii=False)
            if row:
                conn.execute(
                    "UPDATE skills SET description=?, skill_type=?, triggers=?, category=?, content=?, "
                    "version=?, status='published', source='manual', allowed_tools=?, `references`=?, scripts=?, "
                    "package_path=?, builtin=1, scope='public' WHERE name=?",
                    (pkg["description"], pkg["skill_type"], triggers, pkg["category"], pkg["skill_md"],
                     pkg["version"], allowed_tools, refs, scripts, pkg_dir, name))
                print(f"[skills] 已更新: {name} (id={row['id']})")
            else:
                conn.execute(
                    "INSERT INTO skills (name, description, skill_type, triggers, category, content, "
                    "frontmatter, package_path, status, version, source, allowed_tools, `references`, scripts, builtin, scope) "
                    "VALUES (?,?,?,?,?,?,?,?,'published','v1.0','manual',?,?,?,1,'public')",
                    (name, pkg["description"], pkg["skill_type"], triggers, pkg["category"], pkg["skill_md"],
                     pkg["skill_md"], pkg_dir, allowed_tools, refs, scripts))
                print(f"[skills] 已创建: {name}")
            conn.commit()
    finally:
        conn.close()
    print(f"[skills] 完成：{len(SKILL_PACKAGES)} 个基础通用技能")


if __name__ == "__main__":
    main()
