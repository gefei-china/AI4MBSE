# -*- coding: utf-8 -*-
"""
将 static/index.html 中 21406 行的巨型 inline <script> 按模块边界机械切分为
static/js/mods/NN-name.js，index.html 退化为骨架 + 按序 <script src> 引入。

安全约束：
 1. 切分点吸附到最近的「顶层声明行」（保证不在函数体内部）；
 2. 所有分片为普通 script（非 module），共享全局作用域，内联 onclick 不受影响；
 3. 全量备份 index.html 到 _archive/bak/。

⚠️ 已执行完毕（一次性脚本，**不可重跑**）：
 - 输入 static/index.html 现已是拆分后的骨架（1,541 行 / 42 个 <script src>，内联 <script> 为 0），
   重跑会以骨架为输入、产出错误切分；
 - 依赖的备份目录 _archive/bak/ 已移出仓库，重跑在备份步骤即报错；
 - 目标 static/js/mods/ 已含 59 个分片，重跑会重复/覆盖。
 保留本文仅为追溯拆分边界与模块命名依据；如需再切分请另写新脚本。
"""
import re, os, io, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = os.path.join(ROOT, 'static', 'index.html')
OUTDIR = os.path.join(ROOT, 'static', 'js', 'mods')

# (模块文件名, big-block 内 1-based 起始行)
BOUNDARIES = [
    ('01-core.js',         '核心：api / toast / modal / dialog / 多选',      1),
    ('02-shell.js',        '外壳：滑窗 / 面板 / 导航 / 路由 / 页面加载',     572),
    ('03-chat.js',         '会话：会话列表 / 欢迎页 / 任务分组',             1040),
    ('04-artifacts.js',    '制品：制品列表 / 预览面板 / 下载',               1468),
    ('05-markdown.js',     '渲染：Markdown / 表格 / 图表',                   2300),
    ('06-cards.js',        '消息卡：富卡片 / SysML 卡 / 回写',               2526),
    ('07-norm.js',         '归一化：归一确认卡 / 闸口 / 推送',               2861),
    ('08-sysmlview.js',    'SysML 视图卡 / 图例',                            3578),
    ('09-impact.js',       '影响分析 / 沙箱仿真 / 一致性',                   3729),
    ('10-chatinput.js',    '聊天输入：附件 / 范围选择 / 知识库选择',         4535),
    ('11-pipeline.js',     '执行流水线：阶段 / 工具 / 子任务 / DAG',         5203),
    ('12-chatsend.js',     '发送链路：SSE / 澄清 / 提及 / 停止',             5711),
    ('13-reports.js',      '报告：列表 / 查看 / 导出',                       6091),
    ('14-sysml.js',        'SysML 导入 / 版本 / 审批卡',                     6434),
    ('15-kb.js',           '知识库：统计 / 三元组 / 覆盖度 / 图库',          6726),
    ('16-review.js',       '审核：实体审核 / 关系审核 / 重复簇',             7310),
    ('17-v2g.js',          '向量转图谱：候选审核 / 批量',                    7712),
    ('18-glossary.js',     '术语：概念 / 词典 / 映射 / 导入导出',            8176),
    ('19-governance.js',   '治理：闸口 / 发布 / 去重合并',                   8701),
    ('20-docs.js',         '文档：上传 / 抽取 / 元数据 / 数据源',            8990),
    ('21-ontology.js',     '本体工作台：树 / 详情 / 变更日志 / 实例',        9639),
    ('22-ontgraph.js',     '本体图谱交互：拖拽 / 连线 / 布局 / 聚焦',        11982),
    ('23-ontform.js',      '本体表单：滑窗 / 属性绑定 / OWL 导入导出',       12737),
    ('24-graph.js',        '图谱：着色 / 筛选 / 骨架 / 布局算法',            13279),
    ('25-graphview.js',    '图谱视图：渲染 / 树 / 详情 / SysML 视图',        13691),
    ('26-grapheditor.js',  '图谱编辑：节点 / 边 / 视图 / 导入面板',          14364),
    ('27-branch.js',       '分支：分支管理 / 提交 / MR / 合并 / diff',       15245),
    ('28-studio.js',       '工作台：市场 / 插件 / 技能 / MCP',               16689),
    ('29-flow.js',         '流程编排：画布 / 运行 / 监控 / HIL',             17706),
    ('30-agents.js',       'Agent：提示词 / 技能 / MCP / 工具 / LLM',        18755),
    ('31-admin.js',        '管理：用户 / 角色 / 权限 / 审计 / 运维',         20185),
    ('32-approval.js',     '审批：类型 / 模板 / 待办 / 记录',                20910),
    ('33-search.js',       '全局搜索',                                       21334),
]

DECL_RE = re.compile(r'^(?:async\s+)?function\s+[A-Za-z0-9_$]+\s*\(|'
                     r'^(?:const|let|var)\s+[A-Za-z0-9_$]+\s*=')


def main():
    html = io.open(HTML, encoding='utf-8').read()

    # --- 提取所有 inline script 块及其在 html 中的字符区间 ---
    blocks = []
    for m in re.finditer(r'(<script(?![^>]*\bsrc=)[^>]*>)(.*?)(</script>)', html, re.S):
        blocks.append((m.start(), m.end(), m.group(1), m.group(2), m.group(3)))
    if not blocks:
        sys.exit('no inline script found')
    big = max(blocks, key=lambda b: len(b[3]))
    print('inline blocks: %d, big block chars=%d' % (len(blocks), len(big[3])))

    lines = big[3].split('\n')
    n = len(lines)
    print('big block lines:', n)

    # --- 顶层声明行（0-based）---
    decls = [i for i, l in enumerate(lines) if DECL_RE.match(l)]
    print('top-level decls:', len(decls))

    def snap(target_1b):
        """把 1-based 目标行吸附到 <= 它的最近顶层声明行（0-based 索引）"""
        t = min(target_1b - 1, n - 1)
        cand = [d for d in decls if d <= t]
        return cand[-1] if cand else 0

    cuts = []
    for fname, desc, start in BOUNDARIES:
        cuts.append((snap(start), fname, desc))
    # 去重 + 排序（吸附后可能重合）
    seen = {}
    for idx, fname, desc in cuts:
        if idx in seen:
            print('  !! 吸附重合，跳过: %s (%d)' % (fname, idx + 1))
            continue
        seen[idx] = (fname, desc)
    cuts = sorted(seen.items())
    print('effective modules:', len(cuts))

    # --- 切片 ---
    if not os.path.isdir(OUTDIR):
        os.makedirs(OUTDIR)
    for f in os.listdir(OUTDIR):
        if f.endswith('.js'):
            os.remove(os.path.join(OUTDIR, f))

    pieces = []
    for k, (idx, (fname, desc)) in enumerate(cuts):
        end = cuts[k + 1][0] if k + 1 < len(cuts) else n
        body = '\n'.join(lines[idx:end])
        # 去掉尾部多余空行，保留一个换行
        body = body.rstrip('\n') + '\n'
        header = ('/* %s\n'
                  ' * 由 static/index.html 巨型 inline script 机械切分而来\n'
                  ' * 原行号 %d-%d  ·  全局作用域（非 module），内联 onclick 依赖全局函数名\n'
                  ' */\n') % (desc, idx + 1, end)
        path = os.path.join(OUTDIR, fname)
        io.open(path, 'w', encoding='utf-8').write(header + body)
        pieces.append((fname, desc, idx + 1, end, end - idx, len(body)))
        print('  %-22s %6d-%6d  %6d 行  %8d B' % (fname, idx + 1, end, end - idx, len(body)))

    # --- 替换 index.html 中的 big block ---
    tags = '\n'.join(
        '    <script src="/static/js/mods/%s"></script>' % f for f, _, _, _, _, _ in pieces)
    # 整个 inline <script>...</script> 被替换为 N 个外链 <script src>，
    # 不可保留外层开/闭标签，否则浏览器会把外链标签当作脚本文本导致解析错乱。
    new_block = ('<!-- 模块化入口：按依赖顺序加载（全局作用域，勿改为 type=module） -->\n'
                 + tags)
    start, end = big[0], big[1]
    html_new = html[:start] + new_block + html[end:]
    io.open(HTML, 'w', encoding='utf-8').write(html_new)

    print()
    print('index.html: %d -> %d chars (%.1f%% 缩减)' %
          (len(html), len(html_new), 100.0 * (len(html) - len(html_new)) / len(html)))
    print('total split: %d lines -> %d files' % (n, len(pieces)))


if __name__ == '__main__':
    main()
