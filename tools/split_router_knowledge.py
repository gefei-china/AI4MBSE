# -*- coding: utf-8 -*-
"""routers/knowledge.py 拆分：路由按功能域分片到 knowledge_parts/，helper 收敛 shared.py。

机制：所有分片共享 shared.router（APIRouter），装饰器在 import 时按分片顺序注册，
与原文件自上而下注册顺序一致。knowledge.py 变为薄入口（re-export router）。
"""
import ast
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, 'routers', 'knowledge.py')
OUT = os.path.join(ROOT, 'routers', 'knowledge_parts')

src = open(SRC, encoding='utf-8').read()
lines = src.split('\n')
tree = ast.parse(src)

# 1. 顶层节点分类
nodes = []  # (start, end, kind, name)
for n in tree.body:
    s = min([n.lineno] + [d.lineno for d in getattr(n, 'decorator_list', [])])
    e = n.end_lineno
    kind = 'other'
    name = getattr(n, 'name', '')
    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
        is_route = any(
            (isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
             and getattr(d.func.value, 'id', '') == 'router')
            or (isinstance(d, ast.Attribute) and getattr(d.value, 'id', '') == 'router')
            for d in n.decorator_list)
        kind = 'route' if is_route else 'helper'
    elif isinstance(n, ast.Assign):
        ids = [t.id for t in n.targets if isinstance(t, ast.Name) and t.id.isupper()]
        kind = 'const' if ids else 'skip'
        name = ids[0] if ids else ''
    nodes.append((s, e, kind, name))

HEADER_END = 61  # 模块 docstring/imports/router/常量/_actor/_is_release_branch/_release_guard 之前的部分
HEADER_END = next(s for s, e, k, nm in nodes if nm == '_actor') - 1

helpers = [(s, e, nm) for s, e, k, nm in nodes if k == 'helper' and s > HEADER_END]
consts = [(s, e, nm) for s, e, k, nm in nodes if k == 'const' and s > HEADER_END]
routes = [(s, e, nm) for s, e, k, nm in nodes if k == 'route']

# 2. 路由按域分片（原始行号区间，保持注册顺序）
DOMAINS = [
    ('entities',          '实体/标签/提交/分类/评审', 62, 314),
    ('graph',             '图谱节点与边 CRUD', 315, 552),
    ('glossary',          '术语表与别名覆盖', 553, 790),
    ('graph_query',       '图谱边更新/合并/视图/路径/搜索/导出', 791, 1087),
    ('stats',             'FTS/统计/生命周期/覆盖度/总览', 1088, 1308),
    ('ontology',          '本体类型/属性/元数据', 1309, 1726),
    ('ontology_version',  '本体版本链/快照/发布/校验/绑定/导入导出', 1727, 2381),
    ('pipeline',          '实例抽取/V2G/三元组/摄取/SysML/Profile', 2382, 10 ** 9),
]

def chunk_of(lineno):
    for i, (nm, _, s, e) in enumerate(DOMAINS):
        if s <= lineno <= e:
            return i
    raise SystemExit(f'route {lineno} 不在任何域区间')

os.makedirs(OUT, exist_ok=True)

# 3. shared.py：原头部 + 其余常量 + 全部 helper
helper_names = [nm for _, _, nm in helpers]
const_names = [nm for _, _, nm in consts]
header = '\n'.join(lines[:HEADER_END]).rstrip()
helper_src = '\n\n\n'.join('\n'.join(lines[s - 1:e]).rstrip() for s, e, _ in helpers)
const_src = '\n\n'.join('\n'.join(lines[s - 1:e]).rstrip() for s, e, _ in consts)
all_names = (['router', 'json', 'Optional', 'Depends', 'HTTPException', 'JSONResponse',
              'db_session', 'current_user', 'require_permission', 'require_any_permission',
              'typevocab', 'QueryRouter', 'KnowledgeRepo', 'CommitRepo', 'audit',
              'EntityIn', 'BatchReviewIn', 'GraphNodeIn', 'GraphEdgeIn', 'OntologyTypeIn',
              'V2GExtractIn', 'V2GConfirmIn', 'V2GRejectIn', 'V2GUpdateIn', 'SysMLIn',
              'MergeIn', 'RetrieveIn', 'RELEASE_BRANCH', 'WRITE_PERMS']
             + const_names + helper_names)
shared = (header
          + '\n\n# ---- 模块级常量（拆分自原文件中部）----\n' + const_src
          + '\n\n# ---- 模块级私有 helper（拆分自原文件中部，供各分片共用）----\n\n' + helper_src
          + '\n\n__all__ = ' + repr(all_names) + '\n')
open(os.path.join(OUT, 'shared.py'), 'w', encoding='utf-8').write(shared)

# 4. 各域分片
part_files = []
for i, (nm, desc, s, e) in enumerate(DOMAINS):
    rs = [(s0, e0, n0) for s0, e0, n0 in routes if chunk_of(s0) == i]
    body = '\n\n\n'.join('\n'.join(lines[s0 - 1:e0]).rstrip() for s0, e0, _ in rs)
    content = (f'# -*- coding: utf-8 -*-\n'
               f'"""知识库路由分片：{desc}。\n\n由 tools/split_router_knowledge.py 从 routers/knowledge.py 机械切分，勿手工编辑。"""\n'
               f'from routers.knowledge_parts.shared import *\n\n\n' + body + '\n')
    p = os.path.join(OUT, f'{nm}.py')
    open(p, 'w', encoding='utf-8').write(content)
    part_files.append((nm, len(rs), rs[-1][1] - rs[0][0] + 1 if rs else 0))

# 5. 薄入口 knowledge.py
entry = ('"""知识库域：/api/knowledge/*（拆分为 knowledge_parts/ 八个分片，此文件为聚合入口）。"""\n'
         + '\n'.join(f'from routers.knowledge_parts.{nm} import *  # noqa: F401,F403  路由注册（副作用 import）'
                      for nm, _, _ in part_files)
         + '\nfrom routers.knowledge_parts.shared import router  # noqa: F401  re-export 兼容旧引用\n')
open(SRC, 'w', encoding='utf-8').write(entry)

print(f'knowledge.py: {len(lines)} -> {len(entry.splitlines())} lines')
print('routes:', len(routes))
for nm, n, sz in part_files:
    print(f'  parts/{nm:20s} {n:3d} routes')
print('helpers -> shared:', len(helpers), '| consts -> shared:', len(consts))
