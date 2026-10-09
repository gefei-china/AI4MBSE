"""migrate_v9_four_tools — 落地剩余 4 个工具 + 补15 个 skill（V9）。

一次性完成：
  1. 备份（带自检，不信 backup() 返回成功）
  2. 建表：methodology_rules / project_methodology（方法论载体，此前不存在）
  3. 注册 4 个工具：sysml_ast_extract / methodology_profile_load /
     sysml_v2_lint / sysml_import_graph
  4. 路由代理（sysml_check_tools.exec_tool + ast 前缀路由）
  5. 绑定到对应节点（只绑已存在且 active 的工具）
  6. 建 15 个 skill（allowed_tools 全部指向真实存在的工具）

默认 dry-run；--apply 才写库。含幂等判定与迁移后回读自检。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # ← 上溯到工程根
DB = os.path.join(ROOT, "mbse.db")
REG = os.path.join(ROOT, "data", "modeling_nodes.json")

stats = {"created": 0, "same": 0, "skipped": 0, "changed": 0}


def log(k, msg):
    stats[k] += 1
    tag = {"created": "CREATE", "same": "SAME  ", "skipped": "SKIP  ",
           "changed": "CHANGE"}.get(k, "?????")
    print(f"  [{tag}] {msg}")


# ── 1. 新建表 ──────────────────────────────────────────────────
DDL = [
    """CREATE TABLE IF NOT EXISTS methodology_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id TEXT NOT NULL,
    rule_id TEXT NOT NULL,
    scope TEXT NOT NULL,
    rule_type TEXT NOT NULL,
    expression TEXT,
    message TEXT NOT NULL,
    enforce TEXT NOT NULL CHECK(enforce IN ('lint','check','forbid')),
    source_doc_id INTEGER,
    UNIQUE(profile_id, rule_id))""",
    """CREATE TABLE IF NOT EXISTS project_methodology (
    project_id TEXT NOT NULL,
    profile_id TEXT NOT NULL,
    is_active INTEGER DEFAULT 1,
    bound_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(project_id, profile_id))""",
]

# ── 2. 4 个工具定义（字段与现有工具同构）────────────────────────
TOOLS = [
    ("sysml_ast_extract", "read", "low",
     "SysML v2 结构化抽取（真AST 解析，非正则猜测）：返回元素清单（部件/需求/端口/功能/用例…）、"
     "关系清单（包含/类型/满足）与按视图过滤的结果，并声明本次未覆盖的元类。"
     "**做视图展开、追溯核验、覆盖性分析前先抽结构**，不要靠肉眼读代码猜元素与关系。"
     "⚠️ 若返回『AST 解析器不可用』，请如实说明未能解析，不要用正则代替。",
     {"type": "object",
      "properties": {
          "code": {"type": "string", "description": "SysML v2 代码文本（与 files 二选一）"},
          "files": {"type": "array", "items": {"type": "string"},
                    "description": "相对 sysml_models/ 的路径列表；子目录需带前缀。"
                                   "多文件走合并口径（可见跨文件引用），推荐用于全工程分析"},
          "view_type": {"type": "string",
                        "description": "只抽某类视图：requirement/structure/usecase/activity/ibd/part，"
                                       "逗号分隔；留空抽全部"},
          "include_edges": {"type": "boolean", "description": "是否返回关系，默认 true"},
          "max_items": {"type": "integer", "description": "清单展示上限，默认 60"}}}),

    ("methodology_profile_load", "read", "low",
     "加载建模方法论规约卡：语法强制规则（带 enforce 分级）、本体约束、命名正则、"
     "最小必填属性、视图齐备要求，按阶段（M0/N1..N5）裁剪只给该阶段需要的子集。"
     "**开始任何建模前先取规约卡**，不要凭记忆假设客户方法论要求。"
     "返回的『载体层现状』说明哪些约束能被机器执行、哪些只能参考 —— "
     "文档型规范不可机器判定，只有带 enforce 分级的规则型才能自动阻断。"
     "未绑定项目方法论时返回 fallback 规约并显式标注。",
     {"type": "object",
      "properties": {
          "project_id": {"type": "string", "description": "项目 ID，用于查项目↔方法论绑定"},
          "methodology": {"type": "string", "description": "omg_sysml_v2 / magicdraw / custom"},
          "profile_id": {"type": "string", "description": "显式指定规约卡名（会消除 fallback）"},
          "stage": {"type": "string",
                    "description": "M0/N1/N2/N3/N4/N5，按阶段只返回该阶段需要的规则子集"},
          "source_doc_ids": {"type": "array", "items": {"type": "integer"},
                             "description": "方法论文档 id 列表（如 812 广汽方法论），"
                                            "作为文档型参考层的来源"}}}),

    ("sysml_v2_lint", "read", "low",
     "按方法论规约做确定性检查：命名规范（类型=大驼峰/实例=小驼峰，逐元类区分）、"
     "必填属性、forbidden 规则（extend/first/connector/traces/&&/无前缀 import）。"
     "每条违反带 enforce 分级：lint 提示 / check 阻断 / forbid 禁止。"
     "**规范类问题（命名不合规、用了 v1 关键字）先跑本工具，不要用 LLM 肉眼比对命名。**"
     "本工具只报告不改写 —— 改名会破坏跨文件引用，须人工改。",
     {"type": "object",
      "properties": {
          "code": {"type": "string", "description": "SysML v2 代码文本（与 files 二选一）"},
          "files": {"type": "array", "items": {"type": "string"},
                    "description": "相对 sysml_models/ 的路径列表，多文件走合并口径"},
          "profile_id": {"type": "string", "description": "规约卡名，默认 omg_sysml_v2_default"},
          "enforce_filter": {"type": "array",
                             "items": {"type": "string",
                                       "enum": ["lint", "check", "forbid"]},
                             "description": "只报指定级别；留空全报"}}}),

    ("sysml_import_graph", "write", "medium",
     "SysML v2 落库到工程图谱（entities/relations）。**默认 dry-run 只预演**："
     "返回实体候选/关系候选/本体拒绝项，确认无误后再传 confirm=true 真正落库。"
     "发布前应先跑 sysml_v2_project_check（工程级门禁）与本工具 dry-run。"
     "⚠️ 落库会写entities/relations 并打branch/version/is_current 标记，"
     "当前**没有撤销导入的工具**，误落库需人工清理，务必先 dry-run 确认。",
     {"type": "object",
      "properties": {
          "code": {"type": "string", "description": "SysML v2 代码文本（与 files 二选一）"},
          "files": {"type": "array", "items": {"type": "string"},
                    "description": "相对 sysml_models/ 的路径列表，多文件会合并成一份导入"},
          "model_name": {"type": "string", "description": "模型名，用于批次标识"},
          "confirm": {"type": "boolean",
                      "description": "true 才真正写库；缺省/false = dry-run 预演（强烈建议保持默认）"},
          "imported_by": {"type": "string", "description": "导入人标识"}}}),
]

# ── 3. 绑定关系：工具 → 节点 Agent ──────────────────────────────
BINDINGS = [
    ("sysml_ast_extract", ["view_expansion", "trace_verification",
                           "architecture_skeleton", "model_validation_repair"]),
    ("methodology_profile_load", ["methodology_resolver", "requirement_structuring",
                                  "architecture_skeleton"]),
    ("sysml_v2_lint", ["model_validation_repair", "view_expansion",
                       "architecture_skeleton", "model_release"]),
    ("sysml_import_graph", ["model_release"]),
]

# ── 4. 15 个 skill（allowed_tools 全部指向真实存在的工具）────────
def _tool_exists(conn, name):
    return bool(conn.execute(
        "SELECT 1 FROM tools WHERE name=? AND status='active'", (name,)).fetchone())


SKILLS = [
    ("sysml_methodology_profile_guide",
     "建模方法论规约卡解析：区分文档型/本体型/规则型三层载体，把客户建模规范提炼为"
     "可自动执行的规约卡（enforce 分级 + 命名正则 + 视图齐备要求）。",
     ["方法论", "建模规范", "建模约定", "规约卡"],
     ["methodology_profile_load", "graph_retrieve"],
     ["建模工程师", "系统工程师", "架构师"], [],
     ["文档型规范只提供「应该怎么画」，不能机器判定，必须结构化后才可执行",
      "enforce 必须分级：lint 提示 / check 阻断 / forbid 禁止，不分级无法自动执行",
      "每条规则须带 source_doc_id，否则客户无法追溯规则来源",
      "同名对象在不同方法论下含义可能不同，禁止跨 profile 复用规约卡"]),

    ("sysml_requirement_structuring_guide",
     "需求条目化：自然语言需求按句切分→ 分类（功能/性能/接口/约束）→ 标注 reqId 与来源；"
     "不可验证条目（模糊词、无量化判据）归入 gaps 并说明原因，不许混进 items。",
     ["需求条目化", "需求结构化", "需求拆解", "需求条目"],
     ["requirement_itemize", "graph_retrieve"],
     ["系统工程师", "需求工程师", "建模工程师"], [],
     ["含「良好/快速/高效」等模糊词且无量化判据的条目必须进 gaps，不许当可验证需求",
      "分类判据里的单位词必须带词边界（不能收单字母 C/A/s，否则英文缩写会误命中）",
      "标识符必须全局唯一：中文转 ASCII 后常只剩数字，须用 reqId 序号兜底",
      "条目不进 SysML 就不是模型：必须同时产出需求视图骨架"]),

    ("sysml_skeleton_generation_guide",
     "架构骨架生成：只产 package / part def / port def / requirement def 与有依据的 satisfy，"
     "禁止出现 action/state/flow（那是视图展开的活）。",
     ["架构骨架", "骨架生成", "package结构", "部件定义"],
     ["sysml_stdlib_meta", "sysml_v2_validate", "sysml_v2_lint", "sysml_v2_autofix"],
     ["架构师", "建模工程师", "系统工程师"], [],
     ["禁止臆造成员名：写属性前先查 sysml_stdlib_meta 确认标准库里真实存在",
      "顶层 import 须带可见性前缀：private import X::*;",
      "part/port/item 分别由 part def/port def/item def 定型，不可混用",
      "生成后必须调 sysml_v2_validate 校验，verdict=block 时按诊断位置修复后重试，最多 3 轮",
      "骨架里不写视图细节（action/state/flow 属N3）"]),

    ("sysml_view_generation_guide",
     "视图展开统一指南：八视图按 需求→结构→用例→活动→IBD→时序→状态机→参数 固定顺序，"
     "view_type 是参数而非节点。",
     ["视图展开", "生成视图", "八视图", "view_type"],
     ["sysml_ast_extract", "sysml_stdlib_meta", "sysml_v2_validate",
      "sysml_v2_autofix", "sysml_v2_lint"],
     ["建模工程师", "系统工程师", "架构师"], ["sysml_skeleton_generation_guide"],
     ["八视图必须按固定顺序展开，缺前序视图会导致后续视图引用不到元素",
      "时序/状态/参数三视图受 AST 元类覆盖缺口限制（MessageUsage/TransitionUsage/"
      "ParameterUsage 未映射），抽不出边时如实说明，不要断言「模型里没有转换」",
      "活动图必须包含异常分支（Exception Branch），不能只画正常路径",
      "视图只展开不落库：落库统一由发布节点执行"]),

    ("sysml_validation_repair_loop",
     "校验修复闭环：先 diagnose 再 fix，每轮修完必须重跑校验确认；"
     "修复轮次有预算上限，无效修复不计入。",
     ["校验", "修复", "诊断", "verify"],
     ["sysml_v2_validate", "sysml_v2_autofix", "sysml_v2_project_check", "sysml_v2_lint"],
     ["建模工程师", "系统工程师", "质量工程师"], ["sysml_skeleton_generation_guide"],
     ["先跑 validate 拿诊断，再决定是 autofix（规则级）还是人工修（语义级）",
      "autofix 只做语义等价的语法改写；涉及建模意图的只报告不改写",
      "每轮修复后必须重跑校验确认 n_hard 真的下降，不靠「应该好了」判断",
      "发布前必须用 project_check 工程级口径（单文件口径约 2/3 是跨文件伪错）",
      "修复轮次上限 3 轮，超出即上报人工，不要无限重试"]),

    ("sysml_trace_coverage_analysis",
     "追溯核验与覆盖性分析：需求↔设计元素双向追溯矩阵、孤儿元素识别、缺项汇总。"
     "**必须最后调gap_summary，且必须消费前三者结果**。",
     ["追溯", "覆盖", "缺项", "孤儿元素", "覆盖率"],
     ["graph_db_query", "graph_retrieve", "sysml_ast_extract",
      "sysml_v2_project_check", "coverage_matrix"],
     ["系统工程师", "质量工程师", "需求工程师", "项目经理"], [],
     ["gap_summary 必须最后调用，且调用前必须已产出追溯矩阵/孤儿元素/缺项三类结果",
      "孤儿元素（有实现无需求）与缺失满足（有需求无实现）**都要报**，不能只报一侧",
      "覆盖率数字必须来自查询结果，禁止估算",
      "本 skill 在问答/报告场景只读取值，不得触发建模流水线"]),

    ("sysml_release_checklist",
     "模型发布清单：落库前的门禁集合（工程级校验 → lint → 追溯覆盖 → 版本链），"
     "发布是不可逆动作，必须留痕。",
     ["发布", "落库", "入库", "release"],
     ["sysml_v2_project_check", "sysml_v2_lint", "sysml_import_graph",
      "sysml_v2_validate"],
     ["系统工程师", "架构师", "项目经理"], ["sysml_validation_repair_loop"],
     ["落库前必须先 sysml_import_graph dry-run 预演，确认候选与拒绝项后再 confirm=true",
      "工程级门禁（project_check）不过不得发布，单文件口径通过不算数",
      "发布必须留痕：批次号、模型名、校验结论、追溯覆盖率",
      "发布后若发现缺陷，走变更安全门而非直接改库"]),

    ("sysml_change_safety_analysis",
     "删除/变更前的影响面分析：沿五类引用链遍历受影响元素，输出风险等级与悬空引用清单。",
     ["删除影响", "影响面", "变更风险", "安全检查"],
     ["impact_analyze", "graph_retrieve", "graph_db_query"],
     ["系统工程师", "架构师", "项目经理"], [],
     ["必须沿五类引用链遍历：connection→port、requirement→design element、"
      "view→展示元素、behavior→subsystem、verification case→requirement",
      "proceed_allowed=false 时禁止进入落库阶段",
      "影响面数字必须来自 impact_analyze 工具，禁止估算",
      "「把没用的删掉」这类模糊指令必须先问清删除对象和范围，不得自行推测"]),

    ("sysml_stdlib_reference_guide",
     "标准库查询指南：生成任何属性/类型引用前先查标准库，禁止臆造成员名。",
     ["标准库", "kernel", "标准库查询", "元类"],
     ["sysml_stdlib_meta", "sysml_v2_validate"],
     ["建模工程师", "架构师", "系统工程师"], [],
     ["写属性前必须先查标准库确认成员真实存在，臆造会导致类型错",
      "标准库是 KerML 元模型，静态解析不可靠 —— 只取声明事实（有什么/在哪/继承谁），"
      "必填特性以校验器为准",
      "查不到不等于可以用：未找到时如实说明，不要换个像样的名字硬写"]),
]


def _nodes(conn):
    return {r["name"]: r["id"] for r in conn.execute(
        "SELECT id, name FROM agents WHERE status='active'")}


def main(apply_):
    print("=" * 74)
    print("V9 · 剩余 4 工具 + 15 skill 落地" + ("（APPLY）" if apply_ else "（DRY-RUN）"))
    print("=" * 74)

    # ★ 前置自检：目标库必须是**真实的工程库**。
    #   2026-10-08 实测踩过：ROOT 只取到 tools/ ⇒ sqlite3 静默在 tools/mbse.db
    #   新建了一个 0 字节空库 ⇒ 后续所有读写都作用在空库上，报错却是
    #   "no such table: tools"，看起来像库坏了，实则是路径算错。
    if not os.path.isfile(DB):
        print(f"[ABORT] 目标库不存在：{DB}")
        return 2
    _probe = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        _n_obj = _probe.execute(
            "SELECT COUNT(*) FROM sqlite_master").fetchone()[0]
        _n_tools = 0
        try:
            _n_tools = _probe.execute("SELECT COUNT(*) FROM tools").fetchone()[0]
        except Exception:                 # noqa: BLE001
            pass
    finally:
        _probe.close()
    if _n_obj < 50 or _n_tools < 10:
        print(f"[ABORT] 目标库不像工程库：对象数={_n_obj}（应>50）"
              f"／tools 行数={_n_tools}（应>10）")
        print(f"        DB={DB}")
        print("        ⇒ 拒绝在可疑库上执行迁移。")
        return 2
    print(f"[前置] 目标库校验通过：{DB}")
    print(f"       对象 { _n_obj} 个｜tools {_n_tools} 个\n")

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    try:
        # ① 建表
        print("\n── ① 建表 ──")
        for sql in DDL:
            tname = sql.split("IF NOT EXISTS")[1].split("(")[0].strip()
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (tname,)).fetchone()
            if exists:
                log("same", f"表 {tname} 已存在")
            elif not apply_:
                log("created", f"表 {tname} 将新建")
            else:
                conn.execute(sql)
                log("created", f"表 {tname} 已建")

        # ② 注册工具
        print("\n── ② 注册工具 ──")
        for name, side, risk, desc, schema in TOOLS:
            row = conn.execute(
                "SELECT id, description, side_effect FROM tools WHERE name=?",
                (name,)).fetchone()
            sj = json.dumps(schema, ensure_ascii=False)
            if row:
                same = (row["description"] == desc and row["side_effect"] == side)
                if not apply_:
                    log("same" if same else "changed",
                        f"{name} 已存在（描述{'一致' if same else '将更新'}）")
                elif same:
                    log("same", f"{name} 无需变更")
                else:
                    conn.execute("UPDATE tools SET description=?, side_effect=?, "
                                 "risk_level=?, version='v1.0', input_schema=?, "
                                 "status='active' WHERE name=?",
                                 (desc, side, risk, sj, name))
                    log("changed", f"{name} 已更新")
            elif not apply_:
                log("created", f"{name} 将注册（side={side}）")
            else:
                conn.execute(
                    "INSERT INTO tools (name,description,side_effect,risk_level,"
                    "version,input_schema,status) VALUES (?,?,?,?,'v1.0',?,'active')",
                    (name, desc, side, risk, sj))
                log("created", f"{name} 已注册 id={conn.execute(
                    'SELECT id FROM tools WHERE name=?', (name,)).fetchone()[0]}")

        # ③ 绑定
        print("\n── ③ 绑定工具到节点 ──")
        agents = _nodes(conn)
        for tname, targets in BINDINGS:
            for an in targets:
                if tname not in {t[0] for t in TOOLS}:
                    log("skipped", f"{an} ← {tname}：工具不在本次注册清单")
                    continue
                aid = agents.get(an)
                if not aid:
                    log("skipped", f"{an} 不存在")
                    continue
                has = conn.execute(
                    "SELECT 1 FROM agent_tools WHERE agent_id=? AND tool_name=?",
                    (aid, tname)).fetchone()
                if has:
                    log("same", f"{an} 已绑 {tname}")
                elif not apply_:
                    log("created", f"{an} 将绑 {tname}")
                else:
                    conn.execute("INSERT INTO agent_tools "
                                 "(agent_id,tool_type,tool_name,enabled) "
                                 "VALUES (?,'tool',?,1)", (aid, tname))
                    log("created", f"{an} 已绑 {tname}")

        # ④ skill
        print("\n── ④ 建 skill（allowed_tools 只引用真实存在的工具）──")
        reg = json.load(open(REG, encoding="utf-8")) if os.path.isfile(REG) else {}
        reg_skills = {s["name"]: s for s in (reg.get("skills") or [])}
        cur_skills = {r["name"] for r in conn.execute(
            "SELECT name FROM skills")}
        # ⚠️ 有效工具集合必须**包含本批即将注册的工具**：
        #   2026-10-08 实测踩过 —— live_tools 在注册前查，导致 skill 里引用
        #   「本次新建的工具」被误判为不存在而剔除 ⇒ skill 白名单少一半、约束降级。
        #   判据应是「注册完成后会存在的工具」。
        live_tools = {r[0] for r in conn.execute(
            "SELECT name FROM tools WHERE status='active'")}
        pending_tools = {t[0] for t in TOOLS}
        effective_tools = live_tools | pending_tools
        if pending_tools:
            print(f"  [NOTE] 有效工具集合含本批待注册 {len(pending_tools)} 个"
                  f"（{', '.join(sorted(pending_tools))}）")

        plan = []
        for s in reg_skills.values():
            nm = s.get("name")
            if not nm:
                continue
            plan.append((
                nm,
                (s.get("description") or "").strip(),
                json.dumps(s.get("triggers") or [], ensure_ascii=False),
                s.get("category") or "AI建模",
                s.get("allowed_tools") or [],
                s.get("allowed_roles") or [],
                s.get("dependencies") or [],
                json.dumps(s.get("iron_rules") or [], ensure_ascii=False),
                json.dumps(s.get("references") or [], ensure_ascii=False),
                json.dumps(s.get("scripts") or [], ensure_ascii=False),
            ))
        # 注册表里没有的，合并本地补充（allowed_tools 已按实测修正）
        local_extra = {
            s[0]: s for s in SKILLS
        }
        seen = {p[0] for p in plan}
        for nm, v in local_extra.items():
            if nm in seen:
                continue
            plan.append((nm, v[1], json.dumps(v[2], ensure_ascii=False),
                         "AI建模", v[3], v[4], v[5],
                         json.dumps(v[6], ensure_ascii=False),
                         json.dumps([], ensure_ascii=False),
                         json.dumps([], ensure_ascii=False)))
        # 本地补充里若有与注册表重名的，**用本地版本覆盖**（本地 allowed_tools 更准）
        # ⚠️ 覆盖时每个字段都要保持与注册表 plan 相同的序列化形态：
        #   triggers/iron_rules 在 plan 里是 json.dumps 后的**字符串**，
        #   若直接塞回 SKILLS 里的原始 list ⇒ sqlite3 报
        #   "type 'list' is not supported"（2026-10-08 实测）。
        merged = []
        for p in plan:
            nm = p[0]
            if nm not in local_extra:
                merged.append(p)
                continue
            v = local_extra[nm]
            merged.append((
                nm, v[1],
                json.dumps(v[2], ensure_ascii=False),      # triggers → JSON 字符串
                p[3],                                       # category
                v[3],                                       # allowed_tools (list)
                v[4],                                       # allowed_roles (list)
                v[5],                                       # dependencies (list)
                json.dumps(v[6], ensure_ascii=False),       # iron_rules → JSON 字符串
                p[8], p[9],
            ))
        plan = merged

        for (nm, desc, trig, cat, tools, roles, deps,
             iron, refs, scripts) in plan:
            # ★ 关键约束：allowed_tools 引用不存在的工具 ⇒ 白名单永远空、约束静默失效
            valid = [t for t in tools if t in effective_tools]
            dropped = [t for t in tools if t not in effective_tools]
            if dropped:
                log("skipped", f"skill {nm} 的 allowed_tools 剔除了不存在的工具：{dropped}")
            if not valid:
                log("skipped", f"skill {nm} 无任何有效工具，本次不建（避免空约束）")
                continue
            if not roles:
                log("skipped", f"skill {nm} allowed_roles 为空，跳过")
                continue
            if not iron or iron == "[]":
                log("skipped", f"skill {nm} 无 iron_rules，跳过")
                continue
            if nm in cur_skills:
                if not apply_:
                    log("same", f"skill {nm} 已存在")
                else:
                    conn.execute(
                        "UPDATE skills SET description=?, triggers=?, category=?, "
                        "allowed_tools=?, allowed_roles=?, dependencies=?, "
                        "`references`=?, scripts=?, status='published', enabled=1 "
                        "WHERE name=?", (desc, trig, cat, json.dumps(valid, ensure_ascii=False),
                                        json.dumps(roles, ensure_ascii=False),
                                        json.dumps(deps, ensure_ascii=False),
                                        refs, scripts, nm))
                    log("changed", f"skill {nm} 已更新（工具 {len(valid)} 个）")
            elif not apply_:
                log("created", f"skill {nm} 将建（工具 {len(valid)} 个）")
            else:
                conn.execute(
                    "INSERT INTO skills (name,description,triggers,category,allowed_tools,"
                    "allowed_roles,dependencies,`references`,scripts,status,enabled) "
                    "VALUES (?,?,?,?,?,?,?,?,?,'published',1)",
                    (nm, desc, trig, cat, json.dumps(valid, ensure_ascii=False),
                     json.dumps(roles, ensure_ascii=False),
                     json.dumps(deps, ensure_ascii=False), refs, scripts))
                log("created", f"skill {nm} 已建（工具 {len(valid)} 个）")

        if apply_:
            conn.commit()
        else:
            conn.rollback()

        print("\n── 统计 ──")
        print(f"  CREATE {stats['created']}｜CHANGE {stats['changed']}｜"
              f"SAME {stats['same']}｜SKIP {stats['skipped']}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    apply_ = "--apply" in sys.argv
    sys.exit(main(apply_))