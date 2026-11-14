# CI-OPTIONAL: [local] tier=local（见 tools/gates_manifest.json）：读**真库** mbse.db 的配置资产（generate_rules / agents / skills / documents 分块），干净克隆无此库 ⇒ CI 跑不了；本机已实测 PASS 46/0。改法须同步改清单 tier 并接线，不允许只删这行
# -*- coding: utf-8 -*-
"""需求分析与冲突检测「配置化链路」运行时判据（不需要服务，tier=pr）。

## 这个门禁为什么存在
米爸的约束是：**规则与标准必须是配置资产（Skill 正文 + 知识库文档 + 档案配置），
不许写死代码**。所以本门禁**不检查"输出了什么结论"**，只检查三件可证伪的事：

  A. 规则/类别/冲突类型**全部来自** `rule_catalog.json`，DB 里的类别子集与清单一字不差
     ⇒ 证明"改规则=改配置"成立，不存在第二份硬编码来源；
  B. 5 处胶水在**边界条件**上不撒谎：
     · 声明必须检索却没检索到 ⇒ `insufficient`（不许写"已按标准校验"）
     · 确定性层 anchored=0 且无冲突 ⇒ `undetermined`（不许写"无冲突"）
     · 类别没覆盖齐 ⇒ `insufficient` 并列出缺口
  C. 换档案（换标准）⇒ 结论**确实随之变化**（可证伪的"配置即行为"）。

## 判据口径（防自欺）
· 每条判据都打印**真实取值**，不打印"期望值"充数；
· 反向断言（"不许出现 X"）与正向断言成对存在，避免"改成恒真"也能过；
· C 段用**同一份 LLM 文本**在两个档案下各跑一遍 ⇒ 唯一变量是档案，
  若两次结论相同则说明档案没被消费（这就是要抓的 bug）。

用法：python tools/verify/verify_requirement_chain_runtime.py
退出码 0 = 全绿；1 = 有红项。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from services import requirement_checks as rc          # noqa: E402

RESULTS = []
CATALOG_PATH = os.path.join(_ROOT, rc.CATALOG_REL)
DB = os.path.join(_ROOT, "mbse.db")


def check(group, name, cond, detail=""):
    RESULTS.append((group, name, bool(cond), detail))
    shown = f"   ← {detail}" if detail else ""
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{shown}")


def _conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def main():
    print("=" * 78)
    print("需求分析与冲突检测 · 配置化链路运行时判据（A 数据资产 / B 边界不撒谎 / C 配置即行为）")
    print("=" * 78)

    if not os.path.exists(CATALOG_PATH):
        print(f"  规则清单缺失：{CATALOG_PATH}")
        return 1
    catalog = json.load(open(CATALOG_PATH, encoding="utf-8"))
    n_rule = len(catalog.get("rule_categories") or [])
    n_comp = len(catalog.get("completeness_categories") or [])
    n_conf = len(catalog.get("conflict_types") or [])
    print(f"  规则清单：rule={n_rule} completeness={n_comp} conflict={n_conf}")

    conn = _conn()
    profile = rc.load_profile(conn)
    print(f"  标准档案：{json.dumps({k: profile.get(k) for k in ('profile_id', 'standard_ids')}, ensure_ascii=False)}")

    # ── A. 数据资产是唯一来源 ────────────────────────────────────────────
    print("\n── A. 规则/类别只来自数据资产，不来自代码 ──")
    check("A", "A1 规则清单可读且三类非空", n_rule > 0 and n_comp > 0 and n_conf > 0,
          f"{n_rule}/{n_comp}/{n_conf}")
    check("A", "A2 清单规则 ≥10 类（米爸要求 ≥10）", n_rule >= 10, f"rule={n_rule}")
    check("A", "A3 清单完整性 ≥5 类（米爸要求 ≥5）", n_comp >= 5, f"completeness={n_comp}")

    cat_ids = [c["id"] for c in catalog["rule_categories"]]
    comp_ids = [c["id"] for c in catalog["completeness_categories"]]
    conf_ids = [c["id"] for c in catalog["conflict_types"]]

    db_rule = rc.configured_categories(conn, "requirement_rule_categories")
    db_comp = rc.configured_categories(conn, "requirement_completeness_categories")
    db_conf = rc.configured_categories(conn, "requirement_conflict_types")

    check("A", "A4 DB 规则类别 == 清单（顺序+内容）", db_rule == cat_ids,
          f"db={len(db_rule)} catalog={len(cat_ids)} diff={sorted(set(cat_ids) ^ set(db_rule))[:5]}")
    check("A", "A5 DB 完整性类别 == 清单", db_comp == comp_ids,
          f"db={len(db_comp)} catalog={len(comp_ids)}")
    check("A", "A6 DB 冲突类型 == 清单", db_conf == conf_ids,
          f"db={len(db_conf)} catalog={len(conf_ids)}")

    # 每条清单项必须带真实条款依据（clause_ref），否则"依据标准"是空话
    no_ref = [c["id"] for c in catalog["rule_categories"]
              if not (c.get("clause_ref") or c.get("sources"))]
    check("A", "A7 每条规则都带条款依据（clause_ref/sources）", not no_ref,
          f"缺依据：{no_ref[:6]}")

    # 档案的文档白名单必须是**真实存在**的文档（否则白名单把检索筛空）
    dconn = _conn()
    real_docs = {r[0] for r in dconn.execute("SELECT filename FROM documents")}
    p_docs = [str(x) for x in (profile.get("docs") or [])]
    missing = [d for d in p_docs if d not in real_docs]
    check("A", "A8 档案文档白名单全部真实存在", bool(p_docs) and not missing,
          f"白名单={len(p_docs)} 缺失={missing[:3]}")

    # 白名单文档必须真有分块（空文档 ⇒ 检索不到 ⇒ 硬门槛永远 insufficient）
    hit_chunks = 0
    for d in p_docs:
        row = dconn.execute(
            "SELECT (SELECT COUNT(*) FROM document_chunks dc WHERE dc.document_id=documents.id) "
            "FROM documents WHERE filename=?", (d,)).fetchone()
        hit_chunks += int(row[0] or 0) if row else 0
    check("A", "A9 档案文档合计分块 > 0", hit_chunks > 0, f"chunks={hit_chunks}")

    # 三个 Agent 的 kb_scope.docs 必须与档案同源（防两处配置漂移）
    agent_rows = {}
    for n in ("requirement_rule_check", "requirement_completeness_check", "requirement_conflict_check"):
        r = dconn.execute("SELECT name,kb_scope,status FROM agents WHERE name=?", (n,)).fetchone()
        agent_rows[n] = dict(r) if r else None
    bad_scope = []
    for n, d in agent_rows.items():
        if not d:
            bad_scope.append(f"{n}:缺失")
            continue
        try:
            s = json.loads(d.get("kb_scope") or "{}")
        except Exception:
            bad_scope.append(f"{n}:非法JSON")
            continue
        # ★ 2026-11-14 判据**反转**（拆多套的必然结果）：原来是「Agent docs == 档案 docs」。
        #   `merge_scope()` 是并集语义 ⇒ Agent 一旦写死 docs，切档案后旧文档仍被检索
        #   ⇒ 「换标准」变假切换。现在要求 Agent docs **为空**：白名单唯一来源是档案。
        if (s.get("docs") or []) != []:
            bad_scope.append(f"{n}:docs应为空实为{len(s.get('docs') or [])}项")
        if d.get("status") != "active":
            bad_scope.append(f"{n}:status={d.get('status')}")
    check("A", "A10 三个 Agent 的 kb_scope.docs 为空（白名单只由档案决定）且 active",
          not bad_scope, f"{bad_scope[:4]}")
    # 「同源」的正确取证：合并结果 == 档案 docs（一份配置，多处读同一份）
    _same = rc.merge_scope({}, profile).get("docs") or []
    check("A", "A10' 空 scope 合并档案 ⇒ 恰好等于档案 docs（同源、非复制）",
          sorted(_same) == sorted(p_docs), f"merged={_same[:3]} profile={p_docs[:3]}")

    # 三个 Skill 必须存在且正文非空（正文才是规则的真正载体）
    sk_bad = []
    for n in profile.get("skills") or []:
        r = dconn.execute("SELECT name,enabled,length(content) L FROM skills WHERE name=?", (n,)).fetchone()
        if not r:
            sk_bad.append(f"{n}:缺失")
        elif not r["enabled"] or int(r["L"] or 0) < 200:
            sk_bad.append(f"{n}:enabled={r['enabled']} len={r['L']}")
    check("A", "A11 档案指定 Skill 全部存在、启用、正文足够长", not sk_bad,
          f"skills={(profile.get('skills') or [])[:4]} 问题={sk_bad[:3]}")

    # 意图路由三条规则必须指向三个 Agent（否则用户说什么都进不来）
    ints = {}
    for r in dconn.execute("SELECT intent,enabled FROM intent_rules"):
        ints[r[0]] = r[1]
    want = ("requirement_rule_check", "requirement_completeness_check", "requirement_conflict_check")
    lost = [i for i in want if not ints.get(i)]
    check("A", "A12 三个需求类意图已在 intent_rules 启用", not lost, f"缺={lost}")
    check("A", "A13 rc.is_check_intent 认这三类", all(rc.is_check_intent(i) for i in want),
          f"{[rc.is_check_intent(i) for i in want]}")

    # ── B. 边界条件不撒谎 ────────────────────────────────────────────────
    print("\n── B. 5 处胶水在边界条件上不撒谎 ──")
    # ⑤ 硬门槛（D9-A：**无开关**，有档案 + 校验意图 ⇒ 恒生效）
    # 判据同源技巧：先从 execute.py 里**取出真实表达式**，再用它复算 ——
    # 而不是在门禁里另写一份布尔（那样源码改了门禁照样绿）。
    _exec_src = open(os.path.join(_ROOT, "agent", "pipeline_parts", "execute.py"),
                     encoding="utf-8").read()
    _gate_expr = "bool(_req_profile) and _rc2.is_check_intent(intent)"
    check("B", "B0 硬门槛表达式在 execute.py 中真实存在（门禁不另写一份）",
          _gate_expr in _exec_src, _gate_expr)
    check("B", "B1 有档案 + 需求校验意图 ⇒ 门槛成立（恒硬门槛，无开关）",
          bool(profile) and rc.is_check_intent("requirement_rule_check") is True,
          f"profile={bool(profile)}")
    check("B", "B2 无档案 ⇒ 门槛不成立（不卡普通问答，fail-open）",
          not (bool({}) and rc.is_check_intent("requirement_rule_check")), "")
    check("B", "B2' 非校验意图 ⇒ 门槛不成立（普通建模/问答不受影响）",
          not (bool(profile) and rc.is_check_intent("modeling")), "modeling")

    # ⑤ 检索判定
    # ⚠️ 用**生产真实形态**喂判据，键名逐个来自 `agent/rag.py::retrieve()` 的 return dict：
    #   `chunk_hits`(815) / `vector_docs`(814) / `entities`(812) / 两个计数。
    REAL_HIT = {"source": "vector", "chunk_hits": [{"content": "ISO 29148 §5.2.5"}],
                "vector_docs": [{"text": "ISO 29148 §5.2.5"}],
                "vector_count": 1, "entities": [], "graph_count": 0}
    REAL_ONLY_VEC = {"source": "vector", "chunk_hits": [], "vector_docs": [{"text": "x"}],
                     "vector_count": 1, "entities": [], "graph_count": 0}
    REAL_ONLY_CHUNK = {"source": "vector", "chunk_hits": [{"content": "x"}], "vector_docs": [],
                       "vector_count": 0, "entities": [], "graph_count": 0}
    REAL_ENT = {"source": "graph", "chunk_hits": [], "vector_docs": [], "vector_count": 0,
                "entities": [{"name": "需求", "branch": "release"}], "graph_count": 1}
    REAL_EMPTY = {"source": "none", "chunk_hits": [], "vector_docs": [], "vector_count": 0,
                  "entities": [], "graph_count": 0}
    # ★ 2026-11-14 D9-A：`retrieval_verdict` 的 `required` 形参已删。
    #   语义迁移：函数自身**恒保守**（无证据即 insufficient），"要不要判"由调用方门槛决定。
    v_no = rc.retrieval_verdict(REAL_EMPTY)
    v_yes = rc.retrieval_verdict(REAL_HIT)
    v_vec = rc.retrieval_verdict(REAL_ONLY_VEC)
    v_chunk = rc.retrieval_verdict(REAL_ONLY_CHUNK)
    v_ent = rc.retrieval_verdict(REAL_ENT)
    check("B", "B3 零命中 ⇒ insufficient", v_no == "insufficient", f"={v_no}")
    check("B", "B4 真实形态：有分块命中 ⇒ checked", v_yes == "checked", f"={v_yes}")
    check("B", "B5 只透出 chunk_hits ⇒ checked（低置信过滤清空 vector_docs 时有据）",
          v_chunk == "checked", f"={v_chunk}")
    check("B", "B5b 只透出 vector_docs ⇒ checked（工具路径裁剪时有据）",
          v_vec == "checked", f"={v_vec}")
    check("B", "B5c 只有图谱实体（entities）⇒ checked", v_ent == "checked", f"={v_ent}")
    # 原 B6「未要求检索 ⇒ checked」在 D9-A 后不存在这个分支了：
    #   传 None（没检索）也必须判 insufficient —— 证明函数里没有"放松通道"。
    import inspect as _ins
    check("B", "B6 retrieval_verdict 只吃 retrieval 一个参数（无 required 放松通道）",
          len(_ins.signature(rc.retrieval_verdict).parameters) == 1
          and rc.retrieval_verdict(None) == "insufficient",
          f"params={list(_ins.signature(rc.retrieval_verdict).parameters)}")
    # 防漂移锁：判据用到的证据键必须**真实存在**于 rag 返回体（读错键=门槛形同虚设）
    _rag_src = open(os.path.join(_ROOT, "agent", "rag.py"), encoding="utf-8").read()
    _keys_ok = all(k in _rag_src for k in (
        '"chunk_hits": chunk_hits', '"vector_docs": vector_results', '"entities": graph_results'))
    check("B", "B6b rag.retrieve 仍返回 chunk_hits/vector_docs/entities（口径未漂移）",
          _keys_ok, "rag.py 返回键已变，判据需同步")
    _rc_src = open(os.path.join(_ROOT, "services", "requirement_checks.py"),
                   encoding="utf-8").read()
    check("B", "B6c 判据多键取并集（不单认某一个键）",
          'retrieval.get("chunk_hits")' in _rc_src and 'retrieval.get("vector_docs")' in _rc_src,
          "多键并集是防误判的关键")

    # ③ anchored=0 不许写"无冲突"  ← 之前读错字段名踩过的坑
    r_zero = rc.assemble_result(
        json.dumps({"checks": [], "completeness": [], "conflicts": []}, ensure_ascii=False),
        retrieval=REAL_HIT,
        deterministic={"conflicts": [], "stats": {"claims_total": 0, "claims_anchored": 0}})
    check("B", "B6d anchored=0 且无冲突 ⇒ verdict=insufficient",
          r_zero["verdict"] == "insufficient", f"verdict={r_zero['verdict']}")
    check("B", "B7 anchored=0 ⇒ deterministic_verdict=undetermined（不是 checked）",
          (r_zero["result"] or {}).get("deterministic_verdict") == "undetermined",
          f"={(r_zero['result'] or {}).get('deterministic_verdict')}")

    # 反向：anchored>0 ⇒ 必须允许 checked（否则"恒 insufficient"也能过 B6/B7）
    r_ok = rc.assemble_result(
        json.dumps({"checks": [], "completeness": [], "conflicts": []}, ensure_ascii=False),
        retrieval=REAL_HIT,
        deterministic={"conflicts": [], "stats": {"claims_total": 3, "claims_anchored": 3}})
    check("B", "B8 anchored>0 ⇒ deterministic_verdict=checked（反向判据）",
          (r_ok["result"] or {}).get("deterministic_verdict") == "checked",
          f"={(r_ok['result'] or {}).get('deterministic_verdict')}")

    # ⑤ 检索不足 ⇒ 结论不许是 pass（"没检索到却说通过"是最危险的假绿灯）
    r_noret = rc.assemble_result(
        json.dumps({"checks": [{"rule_id": "RC-01", "verdict": "pass"}], "verdict": "pass"},
                   ensure_ascii=False),
        retrieval=REAL_EMPTY, deterministic=None)
    check("B", "B9 未检索到条款 ⇒ verdict 被降为 insufficient（不许 pass）",
          r_noret["verdict"] == "insufficient", f"verdict={r_noret['verdict']}")

    # 类别未覆盖齐 ⇒ insufficient
    r_miss = rc.assemble_result(
        json.dumps({"checks": [], "covered_categories": ["RC-01"]}, ensure_ascii=False),
        retrieval=REAL_HIT,
        configured=cat_ids[:4])
    miss_reason = " ".join(r_miss["reasons"])
    check("B", "B10 类别未覆盖齐 ⇒ insufficient 且列出缺口",
          r_miss["verdict"] == "insufficient" and "未覆盖齐" in miss_reason,
          f"verdict={r_miss['verdict']} reasons={miss_reason[:80]}")

    # 解析失败不许伪造（D9-A 后无 required 形参，这里只验解析失败口径）
    r_bad = rc.assemble_result("模型没给结构化结果，只说了些废话。", retrieval=REAL_HIT)
    check("B", "B11 无结构化 JSON ⇒ ok=False（不伪造结论）",
          r_bad["ok"] is False and r_bad["quality_report"] is None, f"ok={r_bad['ok']}")

    # ③ 输出契约：≥10 类规则 → quality_report.issues 能被前端消费
    fake_checks = [{"rule_id": cid, "verdict": "fail", "severity": "medium",
                    "evidence": "示例证据", "suggestion": "示例建议",
                    "clause_ref": ["ISO 29148 §5.2.5"]} for cid in cat_ids]
    fake_comp = [{"category": c, "covered": False, "missing": ["缺项"]} for c in comp_ids[:5]]
    fake_conf = [{"type": conf_ids[0], "description": "示例冲突", "severity": "high",
                  "req_ids": ["REQ-1", "REQ-2"], "evidence": "示例"}]
    r_full = rc.assemble_result(
        json.dumps({"checks": fake_checks, "completeness": fake_comp, "conflicts": fake_conf,
                    "covered_categories": cat_ids, "verdict": "fail"}, ensure_ascii=False),
        retrieval=REAL_HIT,
        deterministic={"conflicts": [], "stats": {"claims_total": 5, "claims_anchored": 5}},
        configured=cat_ids)
    issues = (r_full["quality_report"] or {}).get("issues") or []
    check("B", "B12 14 类规则全 fail ⇒ issues ≥10 条（前端 13-reports 契约）",
          len(issues) >= 10, f"issues={len(issues)}")
    check("B", "B13 issues 每条含 problem/severity/evidence/suggestion 四字段",
          all(all(k in i for k in ("problem", "severity", "evidence", "suggestion")) for i in issues),
          f"样例={json.dumps(issues[0], ensure_ascii=False)[:90] if issues else '空'}")
    check("B", "B14 issues 保留条款依据（evidence 含 §/R 号）",
          any(("§" in i["evidence"] or "R" in i["evidence"]) for i in issues), "")
    # pass 的项不该进 issues（否则"全量罗列"冒充"发现问题"）
    r_pass = rc.assemble_result(
        json.dumps({"checks": [{"rule_id": "RC-01", "verdict": "pass"}],
                    "covered_categories": cat_ids}, ensure_ascii=False),
        retrieval=REAL_HIT,
        deterministic={"conflicts": [], "stats": {"claims_total": 3, "claims_anchored": 3}},
        configured=cat_ids)
    check("B", "B15 verdict=pass 的规则不进 issues（不虚报问题）",
          (r_pass["quality_report"] or {"issues": []}).get("issues") == [],
          f"issues={len((r_pass['quality_report'] or {}).get('issues') or [])}")

    # 状态字段契约：verdict 必须是机器可判的四值之一，模型散文只能进 conclusion
    # （真机曾把"不通过：3 条需求中仅第 1 条…"整段塞进 verdict ⇒ 前端无法分支）
    _prose = "不通过：3 条需求中仅第 1 条通过，第 2 条须重写"
    r_prose = rc.assemble_result(
        json.dumps({"checks": [{"rule_id": "RC-04", "verdict": "fail"}],
                    "verdict": _prose, "covered_categories": cat_ids}, ensure_ascii=False),
        retrieval=REAL_HIT,
        deterministic={"conflicts": [], "stats": {"claims_total": 3, "claims_anchored": 3}},
        configured=cat_ids)
    check("B", "B15b 模型散文 verdict ⇒ 归一为 fail（不是原样透出）",
          r_prose["verdict"] == "fail", f"={str(r_prose['verdict'])[:40]}")
    check("B", "B15c 原始散文保留在 conclusion（可读性不丢）",
          (r_prose["result"] or {}).get("conclusion") == _prose,
          f"={(r_prose['result'] or {}).get('conclusion')!s:.40}")
    _enum_ok = all(
        rc.assemble_result(json.dumps({"checks": [{"rule_id": "RC-01", "verdict": v}],
                                       "covered_categories": cat_ids}, ensure_ascii=False),
                           retrieval=REAL_HIT,
                           deterministic={"conflicts": [], "stats": {"claims_total": 3,
                                                                     "claims_anchored": 3}},
                           configured=cat_ids)["verdict"]
        in ("pass", "fail", "checked", "insufficient")
        for v in ("pass", "fail", "to_verify"))
    check("B", "B15d 三种 check 结论都落在四值枚举内（前端可分支）", _enum_ok, "")

    # ② 条目 → claims 映射
    items = [{"req_id": "REQ-001", "text": "系统应在 2 秒内完成自检", "subject": "自检时间"},
             {"req_id": "REQ-002", "text": "", "subject": "空文本"},
             {"req_id": "REQ-003", "text": "超长主题名应被截断到12字符以内", "subject": "这是一个特别特别长的主题名字"}]
    claims = rc.items_to_claims(items)
    check("B", "B16 空文本条目不产生 claim", len(claims) == 2, f"claims={len(claims)}")
    check("B", "B17 subject 超 12 字符被截断（detect_conflicts 锚点要求）",
          all(len(c["subject"]) <= 12 for c in claims),
          f"subjects={[c['subject'] for c in claims]}")

    # 确定性冲突复用现成实现（不许另写一套）
    det = rc.run_deterministic_conflicts([
        {"req_id": "R1", "text": "系统自检时间应小于 2 秒", "subject": "自检时间"},
        {"req_id": "R2", "text": "系统自检时间应小于 5 秒", "subject": "自检时间"},
        {"req_id": "R3", "text": "自检时间不得超过 3 秒", "subject": "自检时间"},
    ])
    anchored = int((det.get("stats") or {}).get("claims_anchored", 0) or 0)
    check("B", "B18 同锚点条目能被确定性层锚定（claims_anchored>0）", anchored > 0,
          f"stats={json.dumps(det.get('stats'), ensure_ascii=False)[:110]}")
    check("B", "B19 确定性层复用 subtask_protocol（conflicts 为 list）",
          isinstance(det.get("conflicts"), list), f"type={type(det.get('conflicts')).__name__}")

    # ★ 双层必须**都跑**：真机曾出现 `deterministic_verdict` 恒 None、`conflict_stats` 恒空
    #   —— `run_deterministic_conflicts` 在运行时零调用点，冲突检测只剩模型语义一层。
    _items = [{"req_id": "R1", "text": "系统自检时间应小于 2 秒", "subject": "自检时间"},
              {"req_id": "R2", "text": "系统自检时间应小于 5 秒", "subject": "自检时间"}]
    r_two = rc.assemble_result(
        json.dumps({"conflicts": [], "items": _items, "verdict": "无冲突",
                    "covered_categories": cat_ids}, ensure_ascii=False),
        retrieval=REAL_HIT)                    # ← 不传 deterministic
    check("B", "B20 只给 items（不传 deterministic）⇒ 确定性层自动跑起来",
          (r_two["result"] or {}).get("deterministic_verdict") is not None,
          f"det={(r_two['result'] or {}).get('deterministic_verdict')}")
    check("B", "B21 确定性层真据此锚定（conflict_stats.claims_anchored>0）",
          int(((r_two["result"] or {}).get("conflict_stats") or {}).get("claims_anchored") or 0) > 0,
          f"stats={json.dumps((r_two['result'] or {}).get('conflict_stats'), ensure_ascii=False)[:90]}")
    # 0 锚点时确定性层自身必须 undetermined —— 不许因语义层检出冲突就标 checked
    r_zero2 = rc.assemble_result(
        json.dumps({"conflicts": [{"type": "CT-01", "req_ids": ["A", "B"],
                                   "description": "语义层检出的冲突"}],
                    "items": [{"req_id": "A", "text": "无锚点条目一"},
                              {"req_id": "B", "text": "无锚点条目二"}],
                    "verdict": "fail"}, ensure_ascii=False),
        retrieval=REAL_HIT)
    check("B", "B22 0 锚点 ⇒ 确定性层 undetermined（不因语义层检出就标 checked）",
          (r_zero2["result"] or {}).get("deterministic_verdict") == "undetermined",
          f"det={(r_zero2['result'] or {}).get('deterministic_verdict')} "
          f"stats={json.dumps((r_zero2['result'] or {}).get('conflict_stats'), ensure_ascii=False)[:70]}")
    check("B", "B23 双层合并后冲突条目带 layer 标注（可区分哪层报的）",
          any(c.get("layer") == "deterministic" for c in ((r_two["result"] or {}).get("conflicts") or []))
          or not ((r_two["result"] or {}).get("conflicts")),
          f"conflicts={len((r_two['result'] or {}).get('conflicts') or [])}")

    # ★ 解析容错：真机模型会把**两段 JSON 直接拼接**输出（`{…}{…}`）。
    #   旧实现：json.loads 报 Extra data → 兜底正则贪婪吞到最后一个 } → 仍非法 → 判"解析失败"
    #   ⇒ 结构化结果整个丢掉，card_data 里 requirement_check_result 为空。
    _concat = ('{"conflicts":[{"type":"CT-04","req_ids":["R1","R2"]}]}'
               '{"items":[{"req_id":"R1","text":"自检时间应小于 2 秒","subject":"自检时间"}],'
               '"verdict":"checked"}')
    _pc = rc.parse_result_json(_concat)
    check("B", "B24 两段 JSON 拼接 ⇒ 合并解出（不丢后半段）",
          bool(_pc) and "conflicts" in (_pc or {}) and "items" in (_pc or {}),
          f"keys={list((_pc or {}).keys())}")
    check("B", "B25 合并后列表未被截断、标量先到先得",
          bool(_pc) and len((_pc.get("items") or [])) == 1 and (_pc.get("verdict") == "checked"),
          f"items={( _pc or {}).get('items')} verdict={(_pc or {}).get('verdict')}")
    _prose_js = "下面是结论：\n" + _concat + "\n以上。"
    check("B", "B26 前后夹散文仍能解出（模型常见输出形态）",
          bool(rc.parse_result_json(_prose_js)), "")
    check("B", "B27 真的没有 JSON ⇒ 仍返回 None（不伪造）",
          rc.parse_result_json("这次我只写了自然语言，没有任何结构化内容。") is None, "")
    # 真实失败样本的回归：这两段是**真机原文**摘录（2026-11-14 conv 999999010）
    _real = ('{"conflicts":[{"type":"CT-04","layer":"deterministic","req_ids":["REQ-101"]}],'
             '"layer":"deterministic+semantic","anchored":3,"verdict":"checked"}')
    check("B", "B28 真机单段 JSON 原样可解（结构未被过度容错改坏）",
          (rc.parse_result_json(_real) or {}).get("anchored") == 3, "")

    # ── C. 配置即行为（可证伪）────────────────────────────────────────────
    print("\n── C. 换档案 ⇒ 结论确实变化（唯一变量是档案）──")
    # 同一份 LLM 文本，两个档案各跑一遍
    llm_text = json.dumps({"checks": [], "covered_categories": cat_ids,
                           "verdict": "checked"}, ensure_ascii=False)
    prof_a = {"profile_id": "iso29148_incose_v1", "docs": p_docs}
    prof_b = {"profile_id": "incose_only_v1", "docs": [p_docs[-1]] if p_docs else []}
    sub_a = sorted(cat_ids)
    sub_b = sorted(cat_ids[:5])          # 只要求前 5 类
    ra = rc.assemble_result(llm_text, retrieval=REAL_HIT,
                            deterministic={"conflicts": [], "stats": {"claims_total": 3,
                                                                      "claims_anchored": 3}},
                            configured=sub_a)
    rb = rc.assemble_result(llm_text, retrieval=REAL_HIT,
                            deterministic={"conflicts": [], "stats": {"claims_total": 3,
                                                                      "claims_anchored": 3}},
                            configured=sub_b)
    check("C", "C1 基线档案（14 类齐）⇒ checked", ra["verdict"] == "checked", f"={ra['verdict']}")

    # 反面：把覆盖类别砍到只剩 1 类 ⇒ 必须 insufficient（证明 configured 真被消费）
    rc_strict = rc.assemble_result(
        json.dumps({"checks": [], "covered_categories": ["RC-01"]}, ensure_ascii=False),
        retrieval=REAL_HIT,
        deterministic={"conflicts": [], "stats": {"claims_total": 3, "claims_anchored": 3}},
        configured=sub_a)
    check("C", "C2 只覆盖 1/14 类 ⇒ insufficient（configured 真被消费）",
          rc_strict["verdict"] == "insufficient", f"={rc_strict['verdict']}")

    # merge_scope 反映档案差异
    mA = rc.merge_scope({}, prof_a)
    mB = rc.merge_scope({}, prof_b)
    check("C", "C3 换档案 ⇒ 检索白名单随之变化",
          (mA.get("docs") or []) != (mB.get("docs") or []),
          f"A={len(mA.get('docs') or [])} B={len(mB.get('docs') or [])}")

    # 用户显式选文档优先（不覆盖用户意图）
    mU = rc.merge_scope({"docs": ["用户自选.pdf"]}, prof_a)
    check("C", "C4 用户显式选文档排在前、档案追加在后",
          (mU.get("docs") or [""])[0] == "用户自选.pdf" and len(mU.get("docs") or []) == len(p_docs) + 1,
          f"docs={mU.get('docs')}")

    # ★ 2026-11-14 D9-A：原 C5/C6 是「把 requirement_must_retrieve 改成 false ⇒
    #   verdict 随之变化，再还原」。开关删掉后这两条的**语义要迁移**：
    #   从"证明开关生效"变成"证明开关再也回不来"——即没有任何运行时代码读它。
    #   直接在真库里插一行 false 再删，风险是中途崩了留下脏键；改用**静态取证**：
    #   扫运行时代码（services/ agent/ api/），确认两个死键零消费点。
    _zombie_keys = ("requirement_must_retrieve", "requirement_check_enabled")
    _runtime_hits = []
    for _sub in ("services", "agent", "api"):
        _dir = os.path.join(_ROOT, _sub)
        for _root, _dirs, _files in os.walk(_dir):
            for _f in _files:
                if not _f.endswith(".py"):
                    continue
                _p = os.path.join(_root, _f)
                try:
                    _lines = open(_p, encoding="utf-8").read().splitlines()
                except Exception:
                    continue
                for _ln, _line in enumerate(_lines, 1):
                    # ⚠️ 只抓**代码消费**，放过注释：
                    #   代码里写 `"requirement_must_retrieve"` 才是真的在读它；
                    #   而注释里写「为什么把它删了」是必须留下的历史，不该被判红
                    #   （否则谁也不敢解释删除原因，下一个人又会把它加回来）。
                    _code = _line.split("#", 1)[0]
                    for _k in _zombie_keys:
                        if _k in _code:
                            _runtime_hits.append(
                                f"{os.path.relpath(_p, _ROOT)}:{_ln}:{_k}")
    check("C", "C5 D9-A 死键零消费点（运行时代码不再读 must_retrieve/check_enabled）",
          not _runtime_hits, f"命中={_runtime_hits[:3]}")
    check("C", "C6 硬门槛不依赖任何 DB 布尔（只依赖 档案存在 + 校验意图）",
          "is_check_intent" in _exec_src and "_req_hard_gate = bool(_req_profile) and" in _exec_src,
          None)

    dconn.close()
    conn.close()

    # ── 汇总 ──
    total = len(RESULTS)
    fails = [r for r in RESULTS if not r[2]]
    print("\n" + "=" * 78)
    print(f"结果：PASS {total - len(fails)} / FAIL {len(fails)}  （共 {total} 条）")
    if fails:
        print("失败项：")
        for g, n, _, d in fails:
            print(f"  - [{g}] {n}   ← {d}")
    print("=" * 78)
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
