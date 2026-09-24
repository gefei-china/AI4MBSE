# -*- coding: utf-8 -*-
"""agent/pipeline.py Mixin 拆分：按功能域把 AgentPipeline 的方法拆到 pipeline_parts/*.py。

类名/方法签名/行为零变更：pipeline.py 保留 __init__/_load_db_agents 并继承各 Mixin。
切分点为方法声明行（含上探装饰器），方法体逐行原样搬运。

⚠️ 已执行完毕（一次性脚本，**不可重跑**）：输入 agent/pipeline.py 已退化为 105 行的薄 Mixin 入口，
重跑会以薄入口为输入产出错误分片，并破坏 pipeline_parts/（14 个模块）的继承装配。
保留仅为追溯切分边界。
"""
import os
import re

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'agent', 'pipeline.py')
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'agent', 'pipeline_parts')

lines = open(SRC, encoding='utf-8').read().split('\n')
N = len(lines)


def adjust_up(s):
    """方法声明行上探，吃掉紧邻的装饰器行。"""
    while s - 2 >= 0 and lines[s - 2].strip().startswith('@'):
        s -= 1
    return s


# 聚簇：(name, mixin class, docstring, 首方法声明行 1-based)
CLUSTERS = [
    ('session',       'SessionMixin',   '会话状态、意图槽位与澄清（clarify）持久化。', 73),
    ('skills',        'SkillMixin',     '技能池加载、语义匹配与技能提示词构建。', 229),
    ('orchestration', 'OrchestrMixin',  '团队编排池、多 Agent 编排触发与 Planner 流程复用。', 410),
    ('memory',        'MemoryMixin',    '记忆/模型上下文/用户上下文/报告素材构建。', 872),
    ('prompt',        'PromptMixin',    '任务分解、本体提示与提示词模板。', 1141),
    ('tools',         'ToolMixin',      '工具定义、执行、重试策略与调用留痕。', 1252),
    ('execute',       'ExecuteMixin',   '主执行入口 execute（非流式全流程）。', 1792),
    ('stream',        'StreamMixin',    '流式执行：编排流 SSE / 直接流式 execute_stream。', 2261),
    ('cards',         'CardMixin',      'SysML 视图抽取与富卡片（变更影响/评审）生成。', 3463),
    ('history',       'HistoryMixin',   '附件加载、历史检索、话题标注与上下文预算。', 3907),
    ('context',       'ContextMixin',   '流程匹配、上下文组装与重排。', 4328),
]
CLASS_LINE = 32  # class AgentPipeline: 所在行

# 计算每簇实际起始（含装饰器）与结束
bounds = []
for i, (name, mixin, doc, start) in enumerate(CLUSTERS):
    s = adjust_up(start)
    e = (adjust_up(CLUSTERS[i + 1][3]) - 1) if i + 1 < len(CLUSTERS) else N
    bounds.append((name, mixin, doc, s, e))

COMMON = '''# -*- coding: utf-8 -*-
"""pipeline_parts 公共依赖：所有 Mixin 共享的模块级导入与常量。

由 tools/split_pipeline.py 生成（源：agent/pipeline.py 头部）。方法体引用的
模块级名字全部集中在此，Mixin 通过 `from .common import *` 引入。
"""
import os
import re
import json
import time
import uuid

from database import get_db, db_conn
from knowledge_engine import VectorEngine, QueryRouter
from llm import llm_client
from core.config import STATIC_DIR
from file_tools import exec_file_tool, FILE_TOOL_NAMES as _FILE_TOOL_NAMES
from report_tools import exec_report_tool, REPORT_TOOL_NAMES as _REPORT_TOOL_NAMES
from agent.definition import AgentDefinition
from agent.registry import AgentRegistry
from agent.intent import IntentRouter
from agent.rag import GraphRAG, ConflictDetector
from agent.utils import (
    _citations_payload,
    _extract_code_blocks,
    _archive_impact_analysis,
    _archive_sysml_version,
    _archive_artifacts,
)

# P0-2：工具结果展示上限（SSE 事件与落库共用；超出截断并标记 truncated）
_TOOL_RESULT_CAP = 6000

__all__ = [
    'os', 're', 'json', 'time', 'uuid',
    'get_db', 'db_conn', 'VectorEngine', 'QueryRouter', 'llm_client', 'STATIC_DIR',
    'exec_file_tool', '_FILE_TOOL_NAMES', 'exec_report_tool', '_REPORT_TOOL_NAMES',
    'AgentDefinition', 'AgentRegistry', 'IntentRouter', 'GraphRAG', 'ConflictDetector',
    '_citations_payload', '_extract_code_blocks', '_archive_impact_analysis',
    '_archive_sysml_version', '_archive_artifacts', '_TOOL_RESULT_CAP',
]
'''

os.makedirs(OUT, exist_ok=True)
with open(os.path.join(OUT, '__init__.py'), 'w', encoding='utf-8') as f:
    f.write('"""AgentPipeline Mixin 子包：按功能域拆分的类片段，由 pipeline.AgentPipeline 多继承组装。"""\n')
with open(os.path.join(OUT, 'common.py'), 'w', encoding='utf-8') as f:
    f.write(COMMON)

report = []
for name, mixin, doc, s, e in bounds:
    body = '\n'.join(lines[s - 1:e]).rstrip() + '\n'
    content = (
        '# -*- coding: utf-8 -*-\n'
        f'"""AgentPipeline Mixin：{doc}\n\n由 tools/split_pipeline.py 从 agent/pipeline.py 机械切分而成；⚠️ 切分脚本**已一次性执行完毕、不可重跑**—— 此后本文件按普通源码维护（方法体与其它模块一样可直接改）。"""\n'
        'from .common import *\n\n\n'
        f'class {mixin}:\n'
        f'    """{doc}"""\n\n'
        + body
    )
    path = os.path.join(OUT, f'{name}.py')
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)
    report.append((name, mixin, s, e, e - s + 1))

# 重写 pipeline.py：头部(1-31) + mixin 导入 + class 声明 + 原类体(33-72)
header = '\n'.join(lines[0:CLASS_LINE - 1]).rstrip()
class_body = '\n'.join(lines[CLASS_LINE:72]).rstrip()  # 原第33行(类docstring)到72行
mixins = ', '.join(m for _, m, _, _, _ in report)
new_pipeline = (
    header + '\n'
    + 'from .pipeline_parts.session import SessionMixin\n'
    + 'from .pipeline_parts.skills import SkillMixin\n'
    + 'from .pipeline_parts.orchestration import OrchestrMixin\n'
    + 'from .pipeline_parts.memory import MemoryMixin\n'
    + 'from .pipeline_parts.prompt import PromptMixin\n'
    + 'from .pipeline_parts.tools import ToolMixin\n'
    + 'from .pipeline_parts.execute import ExecuteMixin\n'
    + 'from .pipeline_parts.stream import StreamMixin\n'
    + 'from .pipeline_parts.cards import CardMixin\n'
    + 'from .pipeline_parts.history import HistoryMixin\n'
    + 'from .pipeline_parts.context import ContextMixin\n'
    + 'from .pipeline_parts.common import *  # noqa: F401,F403  模块级名字 re-export 兼容旧引用\n'
    + '\n\n'
    + f'class AgentPipeline({mixins}):\n'
    + class_body + '\n'
)
with open(SRC, 'w', encoding='utf-8') as f:
    f.write(new_pipeline)

print('pipeline.py:', N, '->', new_pipeline.count('\n') + 1, 'lines')
for name, mixin, s, e, n in report:
    print(f'  parts/{name:14s} {mixin:16s} L{s}-{e}  ({n} lines)')
