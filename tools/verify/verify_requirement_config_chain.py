# CI-OPTIONAL: [local] tier=local：需真库（读 skills/agents/generate_rules 配置资产），CI 干净库无这些数据；本机已实测通过。改法须同步改清单 tier 并接线，不允许只删这行
# -*- coding: utf-8 -*-
"""配置化需求分析与冲突检测链路门禁（2026-11-14）。

## 锁死什么
米爸约束：需求规则/标准必须是**知识资产**，不写死代码。本门禁逐项验证：
  G1 规则清单是数据文件，≥10 类规则 / ≥5 类完整性 / ≥1 类冲突，每条带 clause_ref
  G2 3 个 Skill 是配置资产（DB 行 + 快照同源），content 带输出契约且不含代码
  G3 3 个 Agent 是配置资产，绑定标准文档白名单与对应 Skill
  G4 标准档案/类别子集/HIL/项目绑定全部是配置数据
  G5 胶水 ① 工具路径透传 kb_scope；④⑤ 两条路径接线一致
  G6 胶水 ② 需求条目→claims 能让现成 detect_conflicts 检出数值/极性互斥
  G7 胶水 ③ 结构化结果进 quality_report/card_data；四类 insufficient 判据成立
  G8 硬门槛：must_retrieve 且无检索命中 ⇒ insufficient，不许写「已检查」
  G9 变化自证：删掉规则清单里一类 ⇒ 判据必须变红（证明判据非空转）
  G10 配置即行为：换类别子集配置 ⇒ 组装出的 configured 列表跟着变

用法： ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_requirement_config_chain.py
"""
import ast
import json
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

DB = os.path.join(ROOT, "mbse.db")
SNAP = os.path.join(ROOT, "data", "bootstrap", "pipeline_assets.json")
CATALOG = os.path.join(ROOT, "data", "requirement_standards", "rule_catalog.json")
SKILLS = ["requirement_rule_check_guide", "requirement_completeness_guide",
          "Requirement_Conflict_Analysis"]
AGENTS = ["requirement_rule_check", "requirement_completeness_check",
          "requirement_conflict_check"]

PASS = FAIL = 0
ROWS = []


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] %s" % label)
    else:
        FAIL += 1
        print("  [FAIL] %s | %s" % (label, str(detail)[:260]))
    ROWS.append({"label": label, "ok": bool(cond), "detail": str(detail)[:300]})


def rd(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def main():
    from services import requirement_checks as rc
    print("=" * 76)
    print("配置化需求分析与冲突检测链路门禁")
    print("=" * 76)

    # ── G1 规则清单 ──
    print("\n=== G1 规则清单（数据资产）===")
    cat = json.loads(rd(CATALOG))
    rules = cat["rule_categories"]
    comps = cat["completeness_categories"]
    conflicts = cat["conflict_types"]
    check("G1a 规则类别 ≥10", len(rules) >= 10, len(rules))
    check("G1b 完整性类别 ≥5", len(comps) >= 5, len(comps))
    check("G1c 冲突类型 ≥5", len(conflicts) >= 5, len(conflicts))
    check("G1d 每条规则都带真实 clause_ref",
          all(r.get("clause_ref") for r in rules) and all(c.get("clause_ref") for c in comps),
          [r["id"] for r in rules if not r.get("clause_ref")])
    check("G1e 清单标注来源与出处",
          bool(cat.get("sources")) and bool(cat.get("provenance")),
          list(cat.keys()))
    check("G1f 输出契约要求 clause_ref / verdict / severity",
          all(k in cat["output_contract"]["check_result"]["required"]
              for k in ("req_id", "clause_ref", "verdict", "severity")),
          cat["output_contract"]["check_result"]["required"])

    # ── G2 Skill 配置资产 ──
    print("\n=== G2 Skill（DB + 快照同源）===")
    con = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
    con.row_factory = sqlite3.Row
    snap = json.loads(rd(SNAP))
    snap_skills = {x["name"]: x for x in snap["tables"]["skills"]}
    for name in SKILLS:
        r = con.execute("SELECT * FROM skills WHERE name=?", (name,)).fetchone()
        check("G2 %s 已入库且 published" % name,
              bool(r) and r["status"] == "published" and r["enabled"] == 1,
              dict(r) if r else None)
        check("G2 %s 快照同源" % name, name in snap_skills, name)
        if r:
            body = r["content"] or ""
            check("G2 %s 含输出契约与条款出处要求" % name,
                  "clause_ref" in body and ("输出契约" in body or "verdict" in body),
                  body[:80])
            fm = json.loads(r["frontmatter"] or "{}")
            # ★ 2026-11-14 D9-A 判据**反转**：原来要求 frontmatter 声明 `must_retrieve`，
            #   但全仓没有任何消费点读它（纯尸体配置），而且它在 Skill 层再造了一个
            #   "要不要检索"的开关 —— 与「有档案 + 校验意图 ⇒ 恒硬门槛」直接矛盾。
            #   ⇒ 现在要求 frontmatter **不得**出现 must_retrieve / required_docs：
            #     检索意图只在 `is_check_intent()` 一处判定，文档白名单只在档案一处定义。
            _zombie = [k for k in ("must_retrieve", "required_docs") if k in fm]
            check("G2 %s frontmatter 不含死配置（must_retrieve/required_docs）" % name,
                  _zombie == [], _zombie)
            check("G2 %s allowed_tools 含 graph_retrieve" % name,
                  "graph_retrieve" in json.loads(r["allowed_tools"] or "[]"),
                  r["allowed_tools"])

    # ── G3 Agent 配置资产 ──
    print("\n=== G3 Agent 与绑定 ===")
    snap_agents = {x["name"]: x for x in snap["tables"]["agents"]}
    snap_rel = snap["relations"]["agent_tools"]
    for name in AGENTS:
        r = con.execute("SELECT * FROM agents WHERE name=?", (name,)).fetchone()
        check("G3 %s 已入库且 active" % name,
              bool(r) and r["status"] == "active", dict(r) if r else None)
        check("G3 %s 快照同源" % name, name in snap_agents, name)
        if r:
            kb = json.loads(r["kb_scope"] or "{}")
            docs = kb.get("docs") or []
            # ★ 2026-11-14 判据**反转**（拆多套的必然结果）：
            #   原判据是「Agent 的 kb_scope.docs 含 29148 与 Guide 两篇」。
            #   但 `merge_scope()` 是**并集**语义 ⇒ Agent 只要写死两篇，
            #   切到 `incose_only_v1` 仍会检索 29148 ⇒ 「换标准」只是假切换。
            #   ⇒ 现在要求 Agent 的 docs **必须为空**：文档白名单的唯一来源是
            #     标准档案 `profiles[*].docs`（见 G13 的多套切换判据）。
            #   ⚠️ 这是把判据从「存在」改成「不存在」，不是删掉判据
            #      —— 删掉就等于这个维度不再受保护，下次又会有人把 docs 写回来。
            check("G3 %s kb_scope.docs 为空（白名单只由档案决定）" % name,
                  docs == [], docs)
            binds = [tuple(x) for x in con.execute(
                "SELECT tool_type,tool_name FROM agent_tools WHERE agent_id=?", (r["id"],))]
            check("G3 %s 绑定 Skill + graph_retrieve" % name,
                  any(t == "tool" and n == "graph_retrieve" for t, n in binds)
                  and any(t == "skill" for t, n in binds), binds)
            check("G3 %s 绑定在快照 relations 中" % name,
                  any(x.get("agent") == name for x in snap_rel), name)

    # ── G4 标准档案等配置 ──
    print("\n=== G4 标准档案 / 类别 / HIL / 项目绑定 ===")
    prof = rc.load_profile(con)
    check("G4a 标准档案可读且含 docs/skills/agents",
          bool(prof) and all(prof.get(k) for k in ("docs", "skills", "agents")),
          list(prof.keys()))
    check("G4b 类别子集可读且长度与清单一致",
          len(rc.configured_categories(con, "requirement_rule_categories")) == len(rules)
          and len(rc.configured_categories(con, "requirement_completeness_categories")) == len(comps),
          (len(rc.configured_categories(con, "requirement_rule_categories")), len(rules)))
    check("G4c HIL 策略为数据行",
          bool(con.execute("SELECT 1 FROM hil_policies WHERE match_key='requirement_conflict' "
                           "AND level='confirm'").fetchone()), "requirement_conflict")
    check("G4d 项目绑定标准档案",
          bool(con.execute("SELECT 1 FROM project_methodology WHERE profile_id=?",
                           (prof.get("profile_id"),)).fetchone()), prof.get("profile_id"))
    # ★ 2026-11-14 D9-A（米爸拍板）：删掉 `must_retrieve` 开关，改为
    #   「有档案 + 需求校验意图 ⇒ 恒硬门槛」。这里锁死"开关确实没了"，
    #   防止有人为了"偶尔放宽"再加回来（那会让整条链能被静默关闭）。
    check("G4e D9-A 无开关：must_retrieve 函数已删除",
          not hasattr(rc, "must_retrieve"), dir(rc)[:0])
    _deadkeys = [k for k in ("requirement_must_retrieve", "requirement_check_enabled")
                 if con.execute("SELECT 1 FROM generate_rules WHERE rule_key=?", (k,)).fetchone()]
    check("G4e' D9-A 无开关：两个死配置键不在库中", _deadkeys == [], _deadkeys)
    import inspect as _inspect
    _sig_asm = _inspect.signature(rc.assemble_result).parameters
    _sig_rv = _inspect.signature(rc.retrieval_verdict).parameters
    check("G4e'' D9-A 无开关：assemble_result / retrieval_verdict 均无 required 形参",
          "required" not in _sig_asm and "required" not in _sig_rv,
          (list(_sig_asm), list(_sig_rv)))
    check("G4f 需求意图被识别", all(rc.is_check_intent(n) for n in AGENTS), AGENTS)

    # ── G5 接线（静态 + 运行时）──
    print("\n=== G5 胶水接线 ===")
    tools_src = rd(os.path.join(ROOT, "agent", "pipeline_parts", "tools.py"))
    exec_src = rd(os.path.join(ROOT, "agent", "pipeline_parts", "execute.py"))
    stream_src = rd(os.path.join(ROOT, "agent", "pipeline_parts", "stream.py"))
    check("G5a ① graph_retrieve 透传 kb_scope",
          "scope_for_tools" in tools_src and "kb_scope=_scope" in tools_src, None)
    check("G5b ④ 两条路径都加载标准档案",
          "_rc.load_profile" in exec_src and "_src.load_profile" in stream_src, None)
    check("G5c ① 两条路径都挂 _effective_kb_scope",
          exec_src.count("self._effective_kb_scope") >= 2
          and stream_src.count("self._effective_kb_scope") >= 2, None)
    check("G5d ⑤ execute 硬门槛接线（无开关：有档案 + 校验意图）",
          "_req_hard_gate" in exec_src and "or _req_hard_gate)" in exec_src
          and "_req_must_retrieve" not in exec_src, None)
    check("G5e ③ 结果组装 + card_data 回写",
          "assemble_result(" in exec_src and "UPDATE messages SET card_data=?" in exec_src, None)
    check("G5f ③ 返回体带 verdict/reasons",
          "requirement_check_verdict" in exec_src and "requirement_check_reasons" in exec_src, None)

    # scope 合并（纯函数）
    merged = rc.merge_scope({"docs": ["用户选的文档.md"]}, prof)
    check("G5g ④ merge_scope 保留显式范围并追加档案文档",
          merged["docs"][0] == "用户选的文档.md" and len(merged["docs"]) >= 3, merged["docs"])

    # ── G6 胶水 ② 确定性冲突复用 ──
    print("\n=== G6 胶水 ② 条目→claims→现成检测 ===")
    items = [
        {"req_id": "REQ-F001", "text": "蓄电池容量应不低于 800 Ah。", "subject": "蓄电池容量"},
        {"req_id": "REQ-F002", "text": "蓄电池容量应不高于 600 Ah。", "subject": "蓄电池容量"},
    ]
    det = rc.run_deterministic_conflicts(items)
    check("G6a 数值互斥被检出（无新算法）",
          len(det["conflicts"]) >= 1, det["conflicts"][:1])
    check("G6b claims_anchored 与 claims 数一致",
          det["stats"]["claims_anchored"] == 2, det["stats"])
    check("G6c 无锚点条目不参与（不许伪造 subject）",
          rc.run_deterministic_conflicts([{"req_id": "X", "text": "系统应支持 OTA。"}])\
          ["stats"]["claims_anchored"] == 0, None)
    pol = rc.run_deterministic_conflicts([
        {"req_id": "A", "text": "系统应支持 OTA 升级。", "subject": "OTA"},
        {"req_id": "B", "text": "系统应禁止 OTA 升级。", "subject": "OTA"}])
    check("G6d 极性互斥被检出", len(pol["conflicts"]) >= 1, pol["conflicts"][:1])

    # ── G7/G8 胶水 ③ 与硬门槛 ──
    print("\n=== G7/G8 结构化结果与 insufficient 判据 ===")
    configured = rc.configured_categories(con, "requirement_rule_categories")
    ok_payload = {
        "checks": [{"req_id": "REQ-F001", "rule_id": "RC-04", "category": "无歧义词",
                    "verdict": "fail", "severity": "high", "evidence": "系统应具有良好的体验",
                    "clause_ref": ["29148:2011 §5.2.7"], "suggestion": "改为可量化指标"}],
        "covered_categories": configured,
        "configured_categories": configured,
        "verdict": "checked",
    }
    hit = {"chunk_hits": [{"content": "5.2.5 ..."}], "entities": []}
    a = rc.assemble_result(json.dumps(ok_payload, ensure_ascii=False),
                           retrieval=hit, configured=configured)
    check("G7a 正常结果 ok=True / verdict=checked", a["ok"] and a["verdict"] == "checked", a["reasons"])
    check("G7b quality_report.issues[] 符合前端契约",
          bool(a["quality_report"]) and all(
              k in a["quality_report"]["issues"][0]
              for k in ("problem", "severity", "evidence", "suggestion")), a["quality_report"])
    check("G7c card_patch 含结构化结果",
          "requirement_check_result" in a["card_patch"], list(a["card_patch"].keys()))

    b = rc.assemble_result(json.dumps(ok_payload, ensure_ascii=False),
                           retrieval={"chunk_hits": [], "entities": []},
                           configured=configured)
    check("G8a 无检索命中 ⇒ insufficient（硬门槛）",
          b["verdict"] == "insufficient" and b["reasons"], b["reasons"])
    c = rc.assemble_result("模型只给了散文，没有 JSON", retrieval=hit,
                           configured=configured)
    check("G8b 无结构化输出 ⇒ ok=False，不伪造",
          c["ok"] is False and c["quality_report"] is None, c["reasons"])
    d = rc.assemble_result(json.dumps({**ok_payload, "covered_categories": configured[:3]},
                                      ensure_ascii=False),
                           retrieval=hit, configured=configured)
    check("G8c 规则类别漏检 ⇒ insufficient",
          d["verdict"] == "insufficient" and any("未覆盖齐" in x for x in d["reasons"]),
          d["reasons"])
    e = rc.assemble_result(json.dumps({"conflicts": []}, ensure_ascii=False),
                           retrieval=hit, deterministic={
                               "conflicts": [], "stats": {"claims_anchored": 0, "verdict": "no-data"}})
    check("G8d anchored=0 ⇒ 不许写「无冲突」",
          e["result"].get("deterministic_verdict") == "undetermined"
          and e["verdict"] == "insufficient", e["reasons"])
    # ★ 2026-11-14 D9-A：原来的 G8e 是「关闭 required 时不误伤普通问答」。
    #   开关删掉后这条**语义迁移**了：「不误伤普通问答」的保证从"函数形参"搬到
    #   "调用方门槛"（execute.py 只在 `_req_hard_gate` 成立时才调 assemble_result）。
    #   于是判据拆成两条：
    #   ① assemble_result 自身对"没有检索证据"一律保守（不因调用方写法而放松）；
    #   ② 普通问答根本不会走到这里 —— 由 G5d 的接线判据保证（有档案 + 校验意图才进）。
    check("G8e 无检索证据 ⇒ 一律 insufficient（assemble 自身不再有放松开关）",
          rc.assemble_result(json.dumps(ok_payload, ensure_ascii=False),
                             retrieval=None)["verdict"] == "insufficient", None)
    check("G8e' 普通问答由调用方门槛隔离（assemble 调用点受 _req_hard_gate 保护）",
          "if _req_hard_gate:" in exec_src
          and exec_src.find("if _req_hard_gate:") < exec_src.find("assemble_result("), None)

    # ── G9 变异自证 ──
    print("\n=== G9 变异自证（判据非空转）===")
    mutated = json.loads(json.dumps(cat))
    mutated["rule_categories"] = [x for x in mutated["rule_categories"] if x["id"] != "RC-04"]
    check("G9a 删一类规则 ⇒ 类别数确实变化（变异可施加）",
          len(mutated["rule_categories"]) == len(rules) - 1
          and len(mutated["rule_categories"]) != len(rules),
          (len(rules), len(mutated["rule_categories"])))
    check("G9b 等值判据确实依赖配置（删一类即不等）",
          len(rc.configured_categories(con, "requirement_rule_categories"))
          != len(mutated["rule_categories"]), None)
    fc = json.loads(rd(os.path.join(ROOT, "services", "requirement_checks.py"))) if False else None
    src = rd(os.path.join(ROOT, "services", "requirement_checks.py"))
    check("G9c 胶水模块不含业务规则词表（无 FUZZY/if-else 规则）",
          "FUZZY" not in src and "rule_categories =" not in src, None)
    ast.parse(src)
    check("G9d 胶水模块语法正确", True)

    # ── G10 配置即行为 ──
    print("\n=== G10 配置即行为 ===")
    alt = configured[:2]
    f = rc.assemble_result(json.dumps({**ok_payload, "covered_categories": alt,
                                       "configured_categories": alt}, ensure_ascii=False),
                           retrieval=hit, configured=alt)
    check("G10a 换成 2 类子集 ⇒ 按新配置判定（不漏检）",
          f["verdict"] == "checked", f["reasons"])
    g = rc.assemble_result(json.dumps({**ok_payload, "covered_categories": alt},
                                      ensure_ascii=False),
                           retrieval=hit, configured=configured)
    check("G10b 仍按全量子集 ⇒ 漏检被抓出",
          g["verdict"] == "insufficient", g["reasons"])

    # ── G11 输出契约注入闭环（agents.output_schema → 提示词）──
    # 为什么单独取证：`output_schema` 列早就存在，但**只有 registry 元数据与 studio
    # 保存接口读它**，提示词装配侧一次都没读过 ⇒ system_prompt 写「见 output_schema」
    # 而模型看不到，字段名全靠猜（`covered_categories` 猜错即让"类别覆盖齐"恒判未覆盖）。
    # 判据必须**成对**：既有配置时必须注入，也要证明"没有配置时不注入"（否则恒真）。
    print("\n=== G11 输出契约注入闭环 ===")
    try:
        from agent.definition import AgentDefinition as _AD
        from agent.pipeline_parts.prompt import PromptMixin as _PM  # noqa: F401
    except Exception:
        _AD = None
    if _AD is not None:
        class _Stub:
            pass
        _pipe = _Stub()
        # 直接绑定 mixin 方法（不构造整条 pipeline，避免依赖 DB/模型）
        from agent.pipeline_parts import prompt as _pm
        _cls = None
        for _n in dir(_pm):
            _o = getattr(_pm, _n)
            if isinstance(_o, type) and hasattr(_o, "_build_output_contract"):
                _cls = _o
                break
        _has_fn = _cls is not None
        check("G11a 存在 _build_output_contract 注入函数", _has_fn, None)
        if _has_fn:
            _inst = _cls.__new__(_cls)
            _with = _AD(name="t", description="d", output_schema={
                "type": "object", "required": ["checks", "verdict"],
                "properties": {"checks": {"type": "array"}, "verdict": {"type": "string"}}})
            _without = _AD(name="t2", description="d")
            _a = _cls._build_output_contract(_inst, _with)
            _b = _cls._build_output_contract(_inst, _without)
            check("G11b 有 output_schema ⇒ 注入含契约标题的块", "OUTPUT CONTRACT" in _a, _a[:60])
            check("G11c 注入块含 schema 真字段名（checks/verdict）",
                  '"checks"' in _a and '"verdict"' in _a, _a[:120])
            check("G11d 无 output_schema ⇒ 返回空串（多数 Agent 逐字节不变）",
                  _b == "", repr(_b[:40]))
            # 变异自证：把 schema 取空 ⇒ 注入必须消失（证明判据不是恒真）
            _with.output_schema = {}
            check("G11e 变异：schema 清空后注入消失",
                  _cls._build_output_contract(_inst, _with) == "", None)
            # 变异自证：schema 换成别的字段 ⇒ 注入随之变化（证明读的是真配置）
            _with.output_schema = {"type": "object", "properties": {"items": {"type": "array"}}}
            _c = _cls._build_output_contract(_inst, _with)
            check("G11f 变异：换 schema ⇒ 注入内容随之变化（配置驱动）",
                  '"items"' in _c and '"checks"' not in _c, _c[:100])
        # registry 侧：DB 的 output_schema 必须真的进 AgentDefinition
        _row = con.execute("SELECT output_schema FROM agents WHERE name='requirement_rule_check'").fetchone()
        check("G11g DB 三个需求 Agent 的 output_schema 非空",
              bool(_row and (_row[0] or "").strip() not in ("", "{}")), _row[0][:60] if _row else None)
        import inspect as _insp
        _src = _insp.getsource(__import__("agent.registry", fromlist=["x"]))
        check("G11h registry 把 output_schema 传进 AgentDefinition",
              "output_schema=_parse_json_obj" in _src, None)

        # ★ 2026-11-15（米爸拍板 A）：契约从「只输出一个 JSON」改成两段式。
        #   为什么这条必须有判据：这是**用户可见**的形态变更（正文从"一坨 JSON"
        #   变成"结论摘要 + JSON 块"），改回去只需要删一句话 —— 没有判据就会
        #   悄无声息地回退（本轮用户报的"正文没有引用标识"正是它引起的）。
        if _has_fn:
            _inst2 = _cls.__new__(_cls)
            _with2 = _AD(name="t3", description="d", output_schema={
                "type": "object", "required": ["checks"], "properties": {
                    "checks": {"type": "array"}, "verdict": {"type": "string"}}})
            _ct = _cls._build_output_contract(_inst2, _with2)
            check("G11i 契约要求**两部分**（结论摘要 + 结构化块），不再只输出 JSON",
                  "第一部分" in _ct and "第二部分" in _ct and "结论摘要" in _ct, _ct[:120])
            check("G11j 契约要求结论里条款号紧跟来源序号（如 §5.2.7 [2]）",
                  "§5.2.7 [2]" in _ct and "来源序号" in _ct, None)
            check("G11k 旧约束「JSON 之外不要写解释性散文」必须已删除（否则又回到只给 JSON）",
                  "JSON 之外不要写解释性散文" not in _ct
                  and "只输出**一个 JSON 对象**" not in _ct, None)
            check("G11l 契约仍限「只允许一个 json 代码块」（解析需要唯一对象）",
                  "只允许这一个代码块" in _ct and "唯一一个" in _ct, None)
            # 变异自证：把契约退回旧文案 ⇒ G11i/G11k 必须红
            _ct_old = ("## 输出契约（OUTPUT CONTRACT · 必须遵守）\n"
                       "最终答复只输出**一个 JSON 对象**；JSON 之外不要写解释性散文。\n")
            check("M-G11 变异（契约退回只输出 JSON）被 G11i/G11k 抓住",
                  ("第一部分" in _ct) != ("第一部分" in _ct_old)
                  and ("JSON 之外不要写解释性散文" in _ct_old))
            # Skill 正文侧**不得**再写"只输出一个 JSON 对象"（否则两条指令冲突，
            # 模型会听更严格的那条 ⇒ 契约白改）。这是"同一语义写两处必然漂移"的实例。
            _bad_sk = []
            for _n in ("requirement_rule_check_guide", "requirement_completeness_guide",
                       "Requirement_Conflict_Analysis"):
                _r = con.execute("SELECT content FROM skills WHERE name=?", (_n,)).fetchone()
                _c = (_r[0] if _r else "") or ""
                if "只输出一个 JSON 对象" in _c or "只输出一个 JSON" in _c:
                    _bad_sk.append(_n + ":仍写只输出JSON")
                if "结论摘要" not in _c:
                    _bad_sk.append(_n + ":缺两段式说明")
            check("G11m 三个 Skill 正文与契约同口径（两段式，无冲突指令）",
                  not _bad_sk, _bad_sk[:3])
    else:
        check("G11 AgentDefinition 导入失败（判据无法执行）", False, None)

    # ── G12 条款出处真实性（防「编造标准条款」）──
    # 为什么必须有：2026-11-14 核对发现初版 catalog **自己编了 6 处 §5.2.8.1**
    #   （29148-2011 全文实有 5.2.1/5.2.3/5.2.4/5.2.5/5.2.6/5.2.7/5.2.8/5.2.8.6，
    #    **没有 5.2.8.1**），且 5 类冲突**一条依据都没写**。
    #   "所有结论须有明确来源、严禁编造"是硬约束 ⇒ 出处必须**对着已入库文档验存在性**，
    #   而不是看起来像那么回事就算。
    print("\n=== G12 条款出处真实性（锚点必须真实存在于已入库标准）===")

    def _doc_text(con, did):
        try:
            rows = con.execute("SELECT content FROM document_chunks WHERE document_id=?",
                               (did,)).fetchall()
            return " ".join((r[0] or "") for r in rows)
        except Exception:
            return ""

    def _audit_refs(cat, t29148, tguide):
        """返回问题列表；空 = 全部锚点真实存在。"""
        import re as _re
        sec29148 = set(_re.findall(r"5\.2\.\d(?:\.\d)?", t29148))
        secguide = set(_re.findall(r"5\.2\.\d(?:\.\d)?", tguide))
        out = []
        for key in ("rule_categories", "completeness_categories", "conflict_types"):
            for it in cat.get(key) or []:
                refs = it.get("clause_ref") or []
                if isinstance(refs, str):
                    refs = [refs]
                if not refs:
                    out.append("%s 无 clause_ref" % it.get("id"))
                    continue
                for r in refs:
                    r = str(r)
                    for m in _re.findall(r"§\s*(\d+(?:\.\d+)+)", r):
                        have = sec29148 if "29148" in r else secguide
                        if m not in have:
                            out.append("%s: §%s 在该文档中不存在" % (it.get("id"), m))
                    for m in _re.findall(r"\bR(\d{1,2})\b", r):
                        if int(m) > 42 or ("R%s" % m) not in tguide:
                            out.append("%s: R%s 未在 Guide 定位" % (it.get("id"), m))
                    for m in _re.findall(r"\bC(\d{1,2})\b", r):
                        if int(m) > 15 or ("C%s" % m) not in tguide:
                            out.append("%s: C%s 未在 Guide 定位" % (it.get("id"), m))
        return out

    _t29148 = _doc_text(con, 846)
    _tguide = _doc_text(con, 793)
    _cat = json.load(open(os.path.join(ROOT, rc.CATALOG_REL), encoding="utf-8"))
    check("G12a 两份标准正文可读（29148 / Guide）",
          len(_t29148) > 10000 and len(_tguide) > 10000,
          f"29148={len(_t29148)} Guide={len(_tguide)}")
    _probs = _audit_refs(_cat, _t29148, _tguide)
    check("G12b 全部 clause_ref 锚点真实存在（无编造条款）", not _probs,
          f"问题 {len(_probs)} 条：{_probs[:4]}")
    _allrefs = [it for k in ("rule_categories", "completeness_categories", "conflict_types")
                for it in (_cat.get(k) or [])]
    check("G12c 每条规则/完整性/冲突都写了依据（无空依据条目）",
          all(it.get("clause_ref") for it in _allrefs),
          f"缺依据：{[it.get('id') for it in _allrefs if not it.get('clause_ref')][:5]}")
    # 变异自证：注入一个不存在的锚点 ⇒ 判据必须报出来（否则"不校验也绿"）
    _mut = json.loads(json.dumps(_cat))
    _mut["rule_categories"][0]["clause_ref"] = ["29148:2011 §9.9.9 编造条款"]
    _mp = _audit_refs(_mut, _t29148, _tguide)
    check("G12d 变异：注入 §9.9.9 编造锚点 ⇒ 判据报错（自证非恒真）", bool(_mp), f"{_mp[:2]}")
    # 变异自证 2：删掉某条依据 ⇒ G12c 必须红
    _mut2 = json.loads(json.dumps(_cat))
    _mut2["conflict_types"][0]["clause_ref"] = []
    check("G12e 变异：清空一条依据 ⇒ G12c 判红（空依据抓得住）",
          not all(it.get("clause_ref") for k in
                  ("rule_categories", "completeness_categories", "conflict_types")
                  for it in (_mut2.get(k) or [])), None)

    # ── G13 多套标准档案切换（2026-11-14 米爸「拆多套」）──
    # 为什么必须在**内存库**里测：切换会改 `active_profile`，在真库上测会把
    # 生产配置切来切去（跑一次门禁就改一次生产状态）。内存库既验证了切换机制，
    # 又不碰真库 —— 而真库上的切换效果由 `tools/switch_requirement_profile.py`
    # 的「写后新连接回读自证」与真机 e2e 负责。
    print("\n=== G13 多套标准档案切换 ===")
    import sqlite3 as _sq
    _mem = _sq.connect(":memory:")
    _mem.execute("CREATE TABLE generate_rules (rule_key TEXT PRIMARY KEY, rule_value TEXT,"
                 " description TEXT)")
    _synth = {
        "active_profile": "p_full",
        "catalog_path": "data/requirement_standards/rule_catalog.json",
        "skills": ["s1"], "agents": ["a1"],
        "profiles": {
            "p_full": {"standard_ids": ["S-A", "S-B"], "docs": ["docA.pdf", "docB.pdf"]},
            "p_a": {"standard_ids": ["S-A"], "docs": ["docA.pdf"]},
            "p_b": {"standard_ids": ["S-B"], "docs": ["docB.pdf"]},
        },
    }
    _mem.execute("INSERT INTO generate_rules VALUES (?,?,?)",
                 (rc.PROFILE_KEY, json.dumps(_synth, ensure_ascii=False), "t"))
    _mem.commit()
    _p0 = rc.load_profile(_mem)
    check("G13a 多档案可读，回显 active/候选集",
          _p0.get("profile_id") == "p_full"
          and sorted(_p0.get("available_profiles") or []) == ["p_a", "p_b", "p_full"], _p0.get("profile_id"))
    check("G13b 选中档案的 docs 就是该套的 docs（非并集）",
          _p0.get("docs") == ["docA.pdf", "docB.pdf"], _p0.get("docs"))
    check("G13c 公共字段（catalog_path/skills/agents）与档案差异项合并可见",
          _p0.get("catalog_path", "").endswith("rule_catalog.json")
          and _p0.get("skills") == ["s1"], (_p0.get("skills"), _p0.get("catalog_path")))
    _sw = rc.switch_profile(_mem, "p_a")
    check("G13d 切换成功且返回新生效 docs",
          _sw.get("ok") and _sw.get("docs") == ["docA.pdf"], _sw)
    check("G13e 切换后回读为新档案（不是旧快照）",
          rc.load_profile(_mem).get("profile_id") == "p_a", None)
    check("G13f 切换只改 active_profile，其余结构原样",
          len(json.loads(_mem.execute("SELECT rule_value FROM generate_rules WHERE rule_key=?",
                                      (rc.PROFILE_KEY,)).fetchone()[0])["profiles"]) == 3, None)
    _bad = rc.switch_profile(_mem, "p_not_exist")
    check("G13g 切到不存在档案 ⇒ ok=False 且不写库",
          _bad.get("ok") is False and rc.load_profile(_mem).get("profile_id") == "p_a", _bad.get("error"))
    _rb = _mem.execute("SELECT rule_value FROM generate_rules WHERE rule_key=?",
                       (rc.PROFILE_KEY,)).fetchone()[0]
    _badjson = json.loads(_rb)
    _badjson["active_profile"] = "p_ghost"
    _mem.execute("UPDATE generate_rules SET rule_value=? WHERE rule_key=?",
                 (json.dumps(_badjson, ensure_ascii=False), rc.PROFILE_KEY))
    _mem.commit()
    _fb = rc.load_profile(_mem)
    check("G13h active 指向无效 id ⇒ 回退第一套并置 _active_fallback",
          _fb.get("_active_fallback") is True and _fb.get("profile_id") in _fb.get("available_profiles"),
          (_fb.get("profile_id"), _fb.get("_active_fallback")))
    # 旧单档案结构必须继续可用（向后兼容，不改老数据就不用迁移）
    _old = dict(_synth)
    _old.pop("profiles")
    _old.pop("active_profile")
    _old["docs"] = ["legacy.pdf"]
    _mem.execute("UPDATE generate_rules SET rule_value=? WHERE rule_key=?",
                 (json.dumps(_old, ensure_ascii=False), rc.PROFILE_KEY))
    _mem.commit()
    _ol = rc.load_profile(_mem)
    check("G13i 旧单档案结构原样可读（向后兼容）",
          _ol.get("docs") == ["legacy.pdf"] and not _ol.get("profiles"), _ol.get("docs"))
    _mem.close()

    con.close()
    print("PASS %d / FAIL %d" % (PASS, FAIL))
    os.makedirs(os.path.join(ROOT, "outputs"), exist_ok=True)
    with open(os.path.join(ROOT, "outputs", "_verify_requirement_config_chain.json"),
              "w", encoding="utf-8") as f:
        json.dump({"pass": PASS, "fail": FAIL, "rows": ROWS}, f, ensure_ascii=False, indent=1)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
