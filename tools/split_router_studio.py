# -*- coding: utf-8 -*-
"""routers/studio.py 拆分：路由按功能域分片到 studio_parts/，helper 收敛 shared.py。

⚠️ 已执行完毕（一次性脚本，**不可重跑**）：输入 routers/studio.py 已退化为 13 行薄入口
（re-export router），重跑会以薄入口为输入产出空/错误分片，并覆盖 studio_parts/（13 个模块）。
保留仅为追溯切分边界。
"""
import ast
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, 'routers', 'studio.py')
OUT = os.path.join(ROOT, 'routers', 'studio_parts')

src = open(SRC, encoding='utf-8').read()
lines = src.split('\n')
tree = ast.parse(src)

nodes = []
for n in tree.body:
    s = min([n.lineno] + [d.lineno for d in getattr(n, 'decorator_list', [])])
    e = n.end_lineno
    kind, name = 'other', getattr(n, 'name', '')
    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
        is_route = any(
            (isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
             and getattr(d.func.value, 'id', '') == 'router')
            or (isinstance(d, ast.Attribute) and getattr(d.value, 'id', '') == 'router')
            for d in n.decorator_list)
        kind = 'route' if is_route else 'helper'
    elif isinstance(n, ast.Assign):
        ids = [t.id for t in n.targets if isinstance(t, ast.Name) and t.id.isupper()]
        kind, name = ('const', ids[0]) if ids else ('skip', '')
    nodes.append((s, e, kind, name))

HEADER_END = next(s for s, e, k, nm in nodes if nm == '_version_ge') - 1
helpers = [(s, e, nm) for s, e, k, nm in nodes if k == 'helper' and s > HEADER_END]
consts = [(s, e, nm) for s, e, k, nm in nodes if k == 'const' and s > HEADER_END]
routes = [(s, e, nm) for s, e, k, nm in nodes if k == 'route']

DOMAINS = [
    ('prompts',        '提示词管理', 37, 69),
    ('skills',         '技能 CRUD/上传/发布', 70, 232),
    ('mcp',            'MCP 服务器管理与探测', 233, 415),
    ('tools',          '工具 CRUD/启停', 416, 557),
    ('rules',          '意图规则与检索规则', 558, 664),
    ('flows',          'Agent 流程/运行/HIL/规划器/监控/AI 生成', 665, 1178),
    ('agents_reg',     'Agent 注册表与团队', 1179, 1242),
    ('hooks_copy',     '工具钩子与制品复制', 1243, 1427),
    ('agents',         'Agent CRUD/测试/运行/工具绑定', 1428, 1738),
    ('a2a_events',     'A2A 协议与事件订阅', 1739, 1854),
    ('market',         '插件市场', 1855, 10 ** 9),
]

def chunk_of(lineno):
    for i, (nm, _, s, e) in enumerate(DOMAINS):
        if s <= lineno <= e:
            return i
    raise SystemExit(f'route {lineno} 不在任何域区间')

os.makedirs(OUT, exist_ok=True)

helper_names = [nm for _, _, nm in helpers]
const_names = [nm for _, _, nm in consts]
header = '\n'.join(lines[:HEADER_END]).rstrip()
helper_src = '\n\n\n'.join('\n'.join(lines[s - 1:e]).rstrip() for s, e, _ in helpers)
const_src = '\n\n'.join('\n'.join(lines[s - 1:e]).rstrip() for s, e, _ in consts)
all_names = (['router', 'json', 'os', 're', 'threading', 'time', 'uuid', 'List', 'Optional',
              'APIRouter', 'Depends', 'UploadFile', 'File', 'JSONResponse', 'StreamingResponse',
              'db_session', 'require_permission', 'current_user', 'STATIC_DIR',
              'StudioRepo', 'AgentRepo', 'audit', 'audit_user', 'AgentRegistry',
              'sanitize_description', 'sanitize_capabilities', 'ToolRegistry', 'FlowExecutor',
              'parse_skill_zip', 'extract_skill_package', 'SkillParseError', 'plugin_store',
              'PromptIn', 'SkillIn', 'MCPIn', 'ToolIn', 'AgentIn', 'AgentToolIn', 'A2AIn',
              'EventSubIn'] + const_names + helper_names)
shared = (header
          + '\n\n# ---- 模块级常量（拆分自原文件中部）----\n' + const_src
          + '\n\n# ---- 模块级私有 helper（拆分自原文件中部，供各分片共用）----\n\n' + helper_src
          + '\n\n__all__ = ' + repr(all_names) + '\n')
open(os.path.join(OUT, 'shared.py'), 'w', encoding='utf-8').write(shared)

part_files = []
for i, (nm, desc, s, e) in enumerate(DOMAINS):
    rs = [(s0, e0, n0) for s0, e0, n0 in routes if chunk_of(s0) == i]
    body = '\n\n\n'.join('\n'.join(lines[s0 - 1:e0]).rstrip() for s0, e0, _ in rs)
    content = (f'# -*- coding: utf-8 -*-\n'
               f'"""AI 设计工坊路由分片：{desc}。\n\n由 tools/split_router_studio.py 从 routers/studio.py 机械切分，勿手工编辑。"""\n'
               f'from routers.studio_parts.shared import *\n\n\n' + body + '\n')
    open(os.path.join(OUT, f'{nm}.py'), 'w', encoding='utf-8').write(content)
    part_files.append((nm, len(rs)))

entry = ('"""AI 设计工坊（定制化中心）域：/api/studio/*（拆分为 studio_parts/ 十一个分片，此文件为聚合入口）。"""\n'
         + '\n'.join(f'from routers.studio_parts.{nm} import *  # noqa: F401,F403  路由注册（副作用 import）'
                      for nm, _ in part_files)
         + '\nfrom routers.studio_parts.shared import router  # noqa: F401  re-export 兼容旧引用\n')
open(SRC, 'w', encoding='utf-8').write(entry)

print('studio.py:', len(lines), '->', len(entry.splitlines()), 'lines')
print('routes:', len(routes), 'helpers:', len(helpers), 'consts:', len(consts))
for nm, n in part_files:
    print(f'  parts/{nm:14s} {n:3d} routes')
