# -*- coding: utf-8 -*-
"""P1-3 门禁：配置提案器（core/config_proposal.py + proposal_repo + proposals 路由）。

**背景（2026-10-18《自进化 AI 平台调研评估》路线图 P1-3）**：RefineGate 从
「内容评审」泛化为「配置评审」——读 P1-2 归因器聚出的首坏步模式，按规则注册表
提议 prompt/skill/配置修订（GEPA 式反思改写）。铁律：**只提议，不自动应用**
（自动改生产配置是 misevolution 直通道）。本门禁守护：

  A. 纯函数与规则注册表：5 条规则的命中/边界矩阵、指纹稳定性与区分度、
     状态机合法性、确定性排序、max_proposals 截断、unattributed 每批只出一条；
  B. 配置消费：config_proposal 组七键双登记 + 变异自证（入提案下限/prompt
     更高下限/unattributed 阈值/总开关四个口径都必须真被消费）；
  C. 干净库端到端：五规则齐发 → 落库 → 指纹去重 → 三态裁决（终态不重开、
     已决不重开）→ evidence 溯源 → ★零配置写路径（生成/裁决前后除提案队列
     外任何表行数不变 + llm 配置实值不变）；
  D. 真库只读 smoke：propose_from_patterns 在真实模式上跑通（不落库），
     llm.* 提案的 current_value 与活配置逐键对账；
  E. 接线断言：路由三端点运行期注册 + config 双登记 + 引擎/repo 零配置写
     路径（静态）+ 迁移接线。

双环境口径：干净库自建 MBSE_DB_PATH 临时空库（import 前切库）；真库段只读。
"""
import os
import shutil
import sqlite3
import sys
import time as _time


def _find_root():
    _d = os.path.dirname(os.path.abspath(__file__))
    for _ in range(6):
        if os.path.isfile(os.path.join(_d, "memory_service.py")):
            return _d
        _p = os.path.dirname(_d)
        if _p == _d:
            break
        _d = _p
    raise SystemExit("找不到仓库根（未找到 memory_service.py）")


ROOT = _find_root()
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# ── 干净库：先切库后 import（core.config/DB_PATH import 期绑定，顺序即正确性）──
_TMP = os.path.join(ROOT, "tmp", "verify_config_proposal")
os.makedirs(_TMP, exist_ok=True)
_CLEAN_DB = os.path.join(_TMP, "clean.db")
if os.path.exists(_CLEAN_DB):
    os.remove(_CLEAN_DB)
os.environ["MBSE_DB_PATH"] = _CLEAN_DB

_results = []


def check(name, ok, detail=""):
    _results.append((name, bool(ok), detail))
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name,
                           ("  ← " + str(detail)) if (detail and not ok) else ""))
    return bool(ok)


def skip(name, why):
    _results.append((name, None, why))
    print("  [SKIP] " + name + "  -- " + str(why))


def _snap_config():
    """llm 组活配置快照（零配置写路径的对账基准）。"""
    from core import config as _cfg
    return {k: _cfg.get("llm", k, None) for k in
            ("fallback_provider_id", "retry_times", "http_timeout_s")}


def main():
    from core import config_proposal as CP

    # ── A. 纯函数与规则注册表 ───────────────────────────────────────
    print("== A. 纯函数与规则注册表（5 规则命中/边界 + 指纹 + 状态机 + 确定性）==")
    names = [r.name for r in CP._RULES]
    check("A1 规则注册表 5 个（quota/transient/prompt/tool/unattributed）",
          names == ["llm_quota_fallback", "llm_transient_retry",
                    "prompt_skill_revision", "tool_kind_gap", "unattributed_gap"],
          f"got={names}")
    check("A2 can_decide 状态机：只有 pending 可决（终态不重开）",
          CP.can_decide("pending") and not CP.can_decide("applied")
          and not CP.can_decide("rejected") and not CP.can_decide("")
          and CP.PROPOSAL_TERMINAL == ("applied", "rejected"))
    fp1 = CP.proposal_fingerprint("r", "llm", "k", "v1")
    fp2 = CP.proposal_fingerprint("r", "llm", "k", "v1")
    fp3 = CP.proposal_fingerprint("r", "llm", "k", "v2")
    fp4 = CP.proposal_fingerprint("r", "llm", "k", "v1", "llm_call|||p3/m3")
    fp5 = CP.proposal_fingerprint("r", "llm", "k", "v1", "tool_call|||kb_search")
    check("A3 指纹：同输入同指纹；提议值或**模式身份**一变指纹就变"
          "（同规则不同执行体不互相当重复——真库曾因此丢证据）",
          fp1 == fp2 and fp1 != fp3 and fp1 != fp4 and fp4 != fp5 and len(fp1) == 16,
          f"got={fp1}/{fp3}/{fp4}/{fp5}")
    ctx = {"min_occurrences": 2, "prompt_min_occurrences": 4, "unattributed_min": 1,
           "kind_coverage": {"known_kind": 5, "unknown_kind": 5, "known_pct": 50.0},
           "unattributed_count": 0, "config": {"llm": {"fallback_provider_id": 0,
                                                       "retry_times": 2}}}
    p_bal = {"source": "llm_call", "intent": "chat", "kind": "balance",
             "actor": "p3/m3", "count": 2, "conversations": 2}
    hit = CP._RULES[0].match(p_bal, ctx)
    check("A4 R1 quota：balance×2 命中且指向 llm.fallback_provider_id（现值带入）",
          len(hit) == 1 and hit[0]["target_key"] == "fallback_provider_id"
          and hit[0]["current_value"] == "0" and "备用 provider" in hit[0]["proposed_value"]
          and "fallback_provider_id" in hit[0]["rationale"],
          f"got={hit}")
    check("A4b R1 边界：count=1 / 非 llm 源 / timeout 类都不命中",
          not CP._RULES[0].match(dict(p_bal, count=1), ctx)
          and not CP._RULES[0].match(dict(p_bal, source="tool_call"), ctx)
          and not CP._RULES[0].match(dict(p_bal, kind="timeout"), ctx))
    hit2 = CP._RULES[1].match(dict(p_bal, kind="timeout"), ctx)
    check("A5 R2 transient：timeout×2 命中 ⇒ retry_times 现值+1（2→3）",
          len(hit2) == 1 and hit2[0]["target_key"] == "retry_times"
          and hit2[0]["current_value"] == "2" and hit2[0]["proposed_value"] == "3",
          f"got={hit2}")
    p_tool = {"source": "tool_call", "intent": "chat", "kind": "",
              "actor": "kb_search", "count": 2, "conversations": 2}
    hit3 = CP._RULES[3].match(p_tool, ctx)
    check("A6 R3 tool_kind_gap：无 kind 工具失败×2 命中，rationale 带覆盖率数字",
          len(hit3) == 1 and hit3[0]["target_section"] == "instrumentation"
          and "known=5" in hit3[0]["rationale"] and "unknown=5" in hit3[0]["rationale"],
          f"got={hit3}")
    p_ps = {"source": "llm_call", "intent": "report_gen", "kind": "timeout",
            "actor": "p4/m4", "count": 4, "conversations": 4}
    hit5 = CP._RULES[2].match(p_ps, ctx)
    check("A7 R5 prompt_skill：同 (意图,执行体) 复发 4 次命中（下限比配置类高）",
          len(hit5) == 1 and hit5[0]["target_section"] == "skill"
          and "report_gen/p4/m4" in hit5[0]["target_key"]
          and not CP._RULES[2].match(dict(p_ps, count=3), ctx)
          and not CP._RULES[2].match(dict(p_ps, intent=""), ctx),
          f"got={hit5}")
    ctx_un = dict(ctx, unattributed_count=2)
    hit4 = CP._RULES[4].match(p_bal, ctx_un)
    check("A8 R4 unattributed：unattributed_count≥阈值命中；为 0 不命中",
          len(hit4) == 1 and not CP._RULES[4].match(p_bal, ctx)
          and "2 个会话" in hit4[0]["current_value"])
    pats = {"first_bad_patterns": [p_bal, p_tool, p_ps], "kind_coverage": ctx["kind_coverage"],
            "unattributed_sessions": {"count": 2, "samples": [1, 2]}}
    drafts = CP.propose_from_patterns(pats, config_snapshot=ctx["config"])
    rules_hit = sorted(d["rule"] for d in drafts)
    check("A9 propose_from_patterns：五规则齐发且 unattributed 每批只出一条",
          rules_hit == ["llm_quota_fallback", "llm_transient_retry",
                        "prompt_skill_revision", "tool_kind_gap", "unattributed_gap"],
          f"got={rules_hit}")
    check("A9b 确定性：同输入两次调用输出逐字节相等（排序稳定）",
          CP.propose_from_patterns(pats, config_snapshot=ctx["config"]) == drafts)
    check("A9c max_proposals=1 截断 + enabled=False 返空",
          len(CP.propose_from_patterns(
              pats, settings=dict(CP.get_settings(), max_proposals=1),
              config_snapshot=ctx["config"])) == 1
          and CP.propose_from_patterns(
              pats, settings=dict(CP.get_settings(), enabled=False),
              config_snapshot=ctx["config"]) == [])
    # ★ 配置快照驱动 current_value ⇒ 指纹随现场变（闭环不卡死的行为级证明）
    snap2 = {"llm": {"fallback_provider_id": 7, "retry_times": 4}}
    drafts_b = CP.propose_from_patterns(pats, config_snapshot=snap2)
    fp_a = [d["fingerprint"] for d in drafts if d["rule"] == "llm_quota_fallback"][0]
    fp_b = [d["fingerprint"] for d in drafts_b if d["rule"] == "llm_quota_fallback"][0]
    cur_b = [d["current_value"] for d in drafts_b if d["rule"] == "llm_quota_fallback"][0]
    check("A10 ★现场配置变 ⇒ current_value 与指纹都变（人改完配置可重新提议）",
          fp_a != fp_b and cur_b == "7", f"got {fp_a} vs {fp_b}, cur={cur_b}")

    # ── B. 配置双登记 + 默认口径 ────────────────────────────────────
    print("\n== B. 配置消费（config_proposal 组七键双登记 + 默认口径）==")
    import core.config as CFG
    dc = CFG.DEFAULT_CONFIG.get("config_proposal") or {}
    keys7 = ("enabled", "min_occurrences", "prompt_min_occurrences",
             "unattributed_min", "max_proposals", "llm_draft", "max_rationale_chars")
    check("B1 DEFAULT_CONFIG.config_proposal 七键齐全", all(k in dc for k in keys7),
          f"got={sorted(dc)}")
    sch = CFG.CONFIG_SCHEMA.get("config_proposal") or {}
    check("B2 CONFIG_SCHEMA.config_proposal 登记（前端契约，类型齐全）",
          all(k in sch for k in keys7)
          and sch["enabled"]["type"] == "bool" and sch["llm_draft"]["type"] == "bool"
          and all(sch[k]["type"] == "int" for k in keys7
                  if k not in ("enabled", "llm_draft")),
          f"got={sorted(sch)}")
    st = CP.get_settings()
    check("B3 get_settings 默认口径（下限2 / prompt4 / unattributed1 / 20条 / 不调LLM / 500字）",
          st == {"enabled": True, "min_occurrences": 2, "prompt_min_occurrences": 4,
                 "unattributed_min": 1, "max_proposals": 20, "llm_draft": False,
                 "max_rationale_chars": 500}, f"got={st}")

    # ── C. 干净库端到端 ─────────────────────────────────────────────
    print("\n== C. 干净库端到端（五规则落库 / 去重 / 三态裁决 / 零配置写）==")
    from database import init_db, get_db
    init_db()
    conn = get_db()
    conn.row_factory = sqlite3.Row

    def ts(off):
        return _time.strftime("%Y-%m-%d %H:%M:%S",
                              _time.localtime(_time.time() - 2 * 86400 + off))

    def llm(cid, ok, kind, at, intent="chat", prov="p1", model="m1"):
        conn.execute(
            "INSERT INTO llm_usage_stats (provider_name, model_name, intent, "
            "conversation_id, created_at, call_failed, failure_kind, failure_msg) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (prov, model, intent, cid, at, 0 if ok else 1, kind,
             "" if ok else ("boom:" + kind)))

    def tool(cid, name, ok, at):
        conn.execute(
            "INSERT INTO tool_call_logs (intent, tool_name, tool_type, call_kind, "
            "arguments, result, ok, latency_ms, conversation_id, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("chat", name, "builtin", "tool", "{}", "r", 1 if ok else 0, 5, cid, at))

    def seed():
        # 模式口径：同 (源,类型,执行体) 要**多个会话**才成模式（每会话只出一个
        # 首坏步——这是 attribution.min_occurrences 的语义，提案下限与之衔接）。
        for cid in (100, 101, 200, 201, 210, 211, 300, 301, 302, 303, 400, 500):
            conn.execute("INSERT INTO conversations (id, title, intent) "
                         "VALUES (?,?,?)", (cid, "s", "chat"))
        for cid in (100, 101):      # R1：balance 首坏 ×2 会话（同 actor）
            llm(cid, False, "balance", ts(0), prov="p3", model="m3")
            llm(cid, False, "balance", ts(1), prov="p3", model="m3")
        for cid in (200, 201):      # R3a：无 kind 工具失败首坏 ×2 会话（kb_search）
            tool(cid, "kb_search", False, ts(0))
            tool(cid, "kb_search", False, ts(1))
        for cid in (210, 211):      # R3b：**另一个**无 kind 工具（zhiyuan_project_tree）
            tool(cid, "zhiyuan_project_tree", False, ts(0))
            tool(cid, "zhiyuan_project_tree", False, ts(1))
        for cid in (300, 301, 302, 303):   # R2+R5：timeout 首坏 ×4 会话
            llm(cid, False, "timeout", ts(0), intent="report_gen", prov="p4", model="m4")
        llm(400, True, "", ts(0))   # R4：结局坏但零坏步
        conn.execute("INSERT INTO agent_tasks (run_id, task_key, title, status, "
                     "conversation_id, created_at) VALUES (1,'tk','t','failed',400,?)",
                     (ts(1),))
        llm(500, False, "balance", ts(0), prov="p5", model="m5")   # 边界：count=1 不成模式
        conn.commit()

    seed()
    cfg_before = _snap_config()
    counts_before = {t: conn.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
                     for t in ("config_proposals", "llm_usage_stats",
                               "tool_call_logs", "agent_tasks")}

    r1 = CP.generate(conn)
    check("C1 ★六规则齐发：generate 新建 6 条（quota/retry/prompt/tool×2 执行体/"
          "unattributed——同规则不同执行体不互相当重复）",
          r1["created"] == 6 and r1["skipped"] == 0 and len(r1["proposals"]) == 6,
          f"got created={r1['created']} skipped={r1['skipped']}")
    import json as _json
    by_rule = {p["rule"]: p for p in r1["proposals"]}
    tool_ps = [p for p in r1["proposals"] if p["rule"] == "tool_kind_gap"]
    tool_actors = sorted(_json.loads(p["evidence_json"])["actor"] for p in tool_ps)
    check("C2 提案目标准确：llm.fallback_provider_id（现值0）/ llm.retry_times（2→3）/ "
          "skill.report_gen+p4/m4 / tool×2（两个执行体各一条，指纹不互相当重复）/ "
          "unattributed×1",
          by_rule["llm_quota_fallback"]["target_section"] == "llm"
          and by_rule["llm_quota_fallback"]["current_value"] == "0"
          and by_rule["llm_transient_retry"]["proposed_value"] == "3"
          and by_rule["prompt_skill_revision"]["target_key"] == "report_gen/p4/m4"
          and len(tool_ps) == 2
          and tool_actors == ["kb_search", "zhiyuan_project_tree"]
          and by_rule["unattributed_gap"]["target_section"] == "instrumentation",
          f"got={ {k: (v['target_section'], v['target_key'], v['proposed_value']) for k, v in by_rule.items()} } tool_actors={tool_actors}")
    ev = _json.loads(by_rule["llm_transient_retry"]["evidence_json"])
    check("C3 evidence_json 溯源齐全（源/类型/执行体/次数/会话数/样例步）",
          ev.get("source") == "llm_call" and ev.get("kind") == "timeout"
          and ev.get("actor") == "p4/m4" and ev.get("count") == 4
          and ev.get("conversations") == 4 and isinstance(ev.get("sample_steps"), list),
          f"got={ev}")
    check("C3b rationale 非空且含关键证据数字（人读 rationale 即知为什么）",
          all(len(p["rationale"]) > 30 for p in r1["proposals"])
          and "4" in by_rule["llm_transient_retry"]["rationale"])
    r2 = CP.generate(conn)
    check("C4 ★指纹去重：同源再 generate 全 skipped（pending 不重复入队）",
          r2["created"] == 0 and r2["skipped"] == 6,
          f"got created={r2['created']} skipped={r2['skipped']}")
    pid = r1["proposals"][0]["id"]
    d1 = CP.decide(conn, pid, "applied", note="已在设置页修改", decided_by="王工")
    check("C5 decide applied：状态/裁决人/时间/备注四要素落库",
          d1["ok"] and d1["proposal"]["status"] == "applied"
          and d1["proposal"]["reviewed_by"] == "王工"
          and d1["proposal"]["reviewed_at"] and d1["proposal"]["review_note"] == "已在设置页修改",
          f"got={d1}")
    d2 = CP.decide(conn, pid, "rejected")
    check("C6 ★终态不重开：applied 后再决拒（并发下 repo 层 WHERE 再兜底）",
          not d2["ok"] and "终态" in str(d2.get("error", "")))
    r3 = CP.generate(conn)
    check("C7 ★已决不重开：decide 后再 generate 同指纹仍 skipped",
          r3["created"] == 0 and r3["skipped"] == 6,
          f"got created={r3['created']} skipped={r3['skipped']}")
    check("C8 非法输入拒：不支持的决策词 / 不存在的 id",
          not CP.decide(conn, pid, "bogus")["ok"]
          and not CP.decide(conn, 999999, "applied")["ok"])
    cfg_after = _snap_config()
    counts_after = {t: conn.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
                    for t in ("config_proposals", "llm_usage_stats",
                              "tool_call_logs", "agent_tasks")}
    other_changed = {t: (counts_before[t], counts_after[t])
                     for t in counts_before if t != "config_proposals"
                     and counts_before[t] != counts_after[t]}
    check("C9 ★铁律行为级证明：generate/decide 只写提案队列（其它表行数零变化，"
          "llm 活配置逐键不变）",
          not other_changed and cfg_before == cfg_after
          and counts_after["config_proposals"] == 6,
          f"tables={other_changed} cfg={cfg_before}→{cfg_after}")
    lst = CP.list_proposals(conn, status="applied")
    check("C9b 队列按状态可查（applied=1，其余 pending）",
          len(lst) == 1 and lst[0]["id"] == pid)
    conn.close()

    # ── B' 变异自证（口径必须真被消费）──────────────────────────────
    print("\n== B'. 变异自证（四个口径逐一变异，断言必须转红）==")
    for _sfx in ("", "-wal", "-shm"):
        try:
            os.remove(_CLEAN_DB + _sfx)
        except OSError:
            pass
    from database import init_db as _init2, get_db as _get2
    _init2()
    conn2 = _get2()
    conn2.row_factory = sqlite3.Row
    conn = conn2          # seed/llm/tool 闭包改指向 conn2
    seed()
    _og, _ob = CFG.get, CFG.as_bool

    def _mut(section, key, value, is_bool=False):
        if is_bool:
            CFG.as_bool = lambda s, k, d=False, _v=value: (
                _v if (s, k) == (section, key) else _ob(s, k, d))
        else:
            CFG.get = lambda s, k, d=None, _v=value: (
                _v if (s, k) == (section, key) else _og(s, k, d))

    try:
        # 每个变异前清空提案队列：否则上一个变异已入队的提案占坑，本变异的
        # created 会被指纹去重压成 0——测的就不是本口径了。
        def _clear_queue():
            conn2.execute("DELETE FROM config_proposals")
            conn2.commit()

        # M1 min_occurrences=99 ⇒ R1/R2/R3 全挡（count 2/2/4 < 99）；
        #    R4/R5 用各自下限（unattributed_min=1 / prompt_min=4）不受本键约束 ⇒ 仍命中
        _mut("config_proposal", "min_occurrences", 99)
        m1 = CP.generate(conn2)
        check("M1 ★min_occurrences=99 ⇒ 配置类三提案全挡、只余 prompt/unattributed"
              "（入提案下限真被消费）",
              m1["created"] == 2
              and sorted(p["rule"] for p in m1["proposals"])
              == ["prompt_skill_revision", "unattributed_gap"],
              f"got created={m1['created']} rules={sorted(p['rule'] for p in m1['proposals'])}")
        CFG.get = _og
        # M2 prompt_min_occurrences=99 ⇒ R5 单独掉出，其余四条仍命中
        _clear_queue()
        _mut("config_proposal", "prompt_min_occurrences", 99)
        m2 = CP.generate(conn2)
        rules2 = sorted(p["rule"] for p in m2["proposals"])
        check("M2 ★prompt_min_occurrences=99 ⇒ prompt/skill 提案掉出（更高下限真被消费）",
              m2["created"] == 5 and "prompt_skill_revision" not in rules2,
              f"got={rules2}")
        CFG.get = _og
        # M3 unattributed_min=99 ⇒ R4 单独掉出
        _clear_queue()
        _mut("config_proposal", "unattributed_min", 99)
        m3 = CP.generate(conn2)
        rules3 = sorted(p["rule"] for p in m3["proposals"])
        check("M3 ★unattributed_min=99 ⇒ 埋点提案掉出（缺口阈值真被消费）",
              m3["created"] == 5 and "unattributed_gap" not in rules3,
              f"got={rules3}")
        CFG.get = _og
        # M4 enabled=False ⇒ 返空不报错
        _clear_queue()
        _mut("config_proposal", "enabled", False, is_bool=True)
        m4 = CP.generate(conn2)
        check("M4 ★enabled=False ⇒ 返 enabled=False、零创建（总开关真被消费）",
              m4.get("enabled") is False and m4["created"] == 0
              and m4["proposals"] == [], f"got={m4}")
    finally:
        CFG.get, CFG.as_bool = _og, _ob
        conn2.close()

    # ── D. 真库只读 smoke ──────────────────────────────────────────
    print("\n== D. 真库只读 smoke（规则在真实模式上跑通 + 活配置对账）==")
    _prod = os.path.join(ROOT, "mbse.db")
    if not os.path.exists(_prod):
        skip("D1-D2 真库 smoke", "mbse.db 不存在（CI 环境）")
    else:
        from core import attribution as _attr
        pc = sqlite3.connect("file:%s?mode=ro" % _prod.replace("\\", "/"), uri=True)
        pc.row_factory = sqlite3.Row
        pats = _attr.collect_patterns(pc)          # 只读：任何写语句都会炸
        drafts = CP.propose_from_patterns(pats)    # 纯函数：不落库
        print("  真库模式 %d 条 → 草案 %d 条：%s"
              % (len(pats["first_bad_patterns"]), len(drafts),
                 sorted({d["rule"] for d in drafts})))
        live = _snap_config()
        mismatch = [d["rule"] for d in drafts
                    if d["target_section"] == "llm"
                    and d["current_value"] != str(live.get(d["target_key"]))]
        check("D1 ★llm.* 提案的 current_value 与活配置逐键对账（没读现场的提案是拍脑袋）",
              not mismatch, f"mismatch={mismatch} live={live}")
        tool_drafts = [d for d in drafts if d["rule"] == "tool_kind_gap"]
        kc = pats.get("kind_coverage") or {}
        check("D2 真库 tool_kind_gap 提案带真实覆盖率数字（unknown 现状如实呈现）",
              (not tool_drafts) or all(
                  str(kc.get("unknown_kind")) in d["rationale"] for d in tool_drafts),
              f"kc={kc} drafts={len(tool_drafts)}")
        pc.close()

    # ── E. 接线完整性 ───────────────────────────────────────────────
    print("\n== E. 接线完整性（路由注册 + config 双登记 + 零配置写路径）==")
    import importlib
    _m = importlib.import_module("routers.proposals")
    _paths = [getattr(r, "path", "") for r in _m.router.routes]
    check("E1 路由三端点注册（list/generate/decide，运行期断言）",
          "/api/proposals" in _paths and "/api/proposals/generate" in _paths
          and "/api/proposals/{proposal_id}/decide" in _paths,
          f"got={_paths}")
    _cf = open(os.path.join(ROOT, "core", "config.py"), encoding="utf-8").read()
    check("E2 core/config.py 双登记（DEFAULT_CONFIG + CONFIG_SCHEMA）",
          _cf.count('"config_proposal"') >= 2,
          f"count={_cf.count('\"config_proposal\"')}")
    _eng = open(os.path.join(ROOT, "core", "config_proposal.py"),
                encoding="utf-8").read()
    _rep = open(os.path.join(ROOT, "repositories", "proposal_repo.py"),
                encoding="utf-8").read()
    _rot = open(os.path.join(ROOT, "routers", "proposals.py"),
                encoding="utf-8").read()
    _cfg_write = [w for w in ("config.set(", "_cfg.set(", "config_set(")
                  if w in (_eng + _rep + _rot)]
    _tables = set()
    import re as _re
    for src in (_eng, _rep):
        _tables.update(_re.findall(r"(?:FROM|INTO|UPDATE)\s+([a-z_]+)", src))
    check("E3 ★零配置写路径（静态：引擎/repo/路由无任何 config 写调用）",
          not _cfg_write, f"发现配置写调用: {_cfg_write}")
    check("E3b repo 只碰 config_proposals 一张表（写面最小化）",
          _tables <= {"config_proposals"}, f"got={sorted(_tables)}")
    _mig = open(os.path.join(ROOT, "database", "migrations", "misc.py"),
                encoding="utf-8").read()
    _sch = open(os.path.join(ROOT, "database", "schema.py"), encoding="utf-8").read()
    check("E4 迁移接线：DDL 在 misc.py 且 schema.py 调用（建表随 init_db 幂等）",
          "CREATE TABLE IF NOT EXISTS config_proposals" in _mig
          and "_migrate_config_proposals(conn)" in _sch)
    _mm = open(os.path.join(ROOT, "routers", "proposals.py"),
               encoding="utf-8").read()
    check("E5 decide 端点明示「不执行配置修改」（产品口径写在代码里）",
          "不执行任何配置修改" in _mm and "audit(" in _mm)

    print("\n结果: %d PASS / %d FAIL / %d SKIP"
          % (sum(1 for r in _results if r[1] is True),
             sum(1 for r in _results if r[1] is False),
             sum(1 for r in _results if r[1] is None)))
    _failed = [r[0] for r in _results if r[1] is False]
    if _failed:
        print("失败项: " + "; ".join(_failed))
    shutil.rmtree(_TMP, ignore_errors=True)
    if _failed:
        sys.exit(1)
    print("全部通过 —— 配置提案器（5 规则注册表 + 指纹去重 + 三态裁决终态不重开 + "
          "七键配置变异自证 + 零配置写路径 + 真库活配置对账 + 接线）。")


if __name__ == "__main__":
    main()
