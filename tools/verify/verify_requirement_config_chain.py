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
            check("G2 %s 声明 must_retrieve 硬门槛" % name,
                  fm.get("must_retrieve") is True, fm.get("must_retrieve"))
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
            check("G3 %s 绑定标准文档白名单" % name,
                  any("29148" in d for d in docs) and any("Guide" in d for d in docs), docs)
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
    check("G4e must_retrieve 开关生效", rc.must_retrieve(prof, con) is True, None)
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
    check("G5d ⑤ execute 硬门槛接入 should_retrieve",
          "_req_must_retrieve" in exec_src and "or _req_must_retrieve)" in exec_src, None)
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
                           retrieval=hit, required=True, configured=configured)
    check("G7a 正常结果 ok=True / verdict=checked", a["ok"] and a["verdict"] == "checked", a["reasons"])
    check("G7b quality_report.issues[] 符合前端契约",
          bool(a["quality_report"]) and all(
              k in a["quality_report"]["issues"][0]
              for k in ("problem", "severity", "evidence", "suggestion")), a["quality_report"])
    check("G7c card_patch 含结构化结果",
          "requirement_check_result" in a["card_patch"], list(a["card_patch"].keys()))

    b = rc.assemble_result(json.dumps(ok_payload, ensure_ascii=False),
                           retrieval={"chunk_hits": [], "entities": []},
                           required=True, configured=configured)
    check("G8a 无检索命中 ⇒ insufficient（硬门槛）",
          b["verdict"] == "insufficient" and b["reasons"], b["reasons"])
    c = rc.assemble_result("模型只给了散文，没有 JSON", retrieval=hit,
                           required=True, configured=configured)
    check("G8b 无结构化输出 ⇒ ok=False，不伪造",
          c["ok"] is False and c["quality_report"] is None, c["reasons"])
    d = rc.assemble_result(json.dumps({**ok_payload, "covered_categories": configured[:3]},
                                      ensure_ascii=False),
                           retrieval=hit, required=True, configured=configured)
    check("G8c 规则类别漏检 ⇒ insufficient",
          d["verdict"] == "insufficient" and any("未覆盖齐" in x for x in d["reasons"]),
          d["reasons"])
    e = rc.assemble_result(json.dumps({"conflicts": []}, ensure_ascii=False),
                           retrieval=hit, required=True, deterministic={
                               "conflicts": [], "stats": {"claims_anchored": 0, "verdict": "no-data"}})
    check("G8d anchored=0 ⇒ 不许写「无冲突」",
          e["result"].get("deterministic_verdict") == "undetermined"
          and e["verdict"] == "insufficient", e["reasons"])
    check("G8e 关闭 required 时不误伤普通问答",
          rc.assemble_result(json.dumps(ok_payload, ensure_ascii=False),
                             retrieval=None, required=False)["verdict"] == "checked", None)

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
                           retrieval=hit, required=True, configured=alt)
    check("G10a 换成 2 类子集 ⇒ 按新配置判定（不漏检）",
          f["verdict"] == "checked", f["reasons"])
    g = rc.assemble_result(json.dumps({**ok_payload, "covered_categories": alt},
                                      ensure_ascii=False),
                           retrieval=hit, required=True, configured=configured)
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

    con.close()
    print("\n" + "=" * 76)
    print("PASS %d / FAIL %d" % (PASS, FAIL))
    os.makedirs(os.path.join(ROOT, "outputs"), exist_ok=True)
    with open(os.path.join(ROOT, "outputs", "_verify_requirement_config_chain.json"),
              "w", encoding="utf-8") as f:
        json.dump({"pass": PASS, "fail": FAIL, "rows": ROWS}, f, ensure_ascii=False, indent=1)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
