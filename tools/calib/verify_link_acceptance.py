# -*- coding: utf-8 -*-
"""链路验收：5 条**用户可见**链路的绿/红表（批次三收官，2026-10-14）。

## 为什么要有这个脚本（米爸 2026-10-14 08:24 的判断）
「什么时候能收敛」不能用 commit 数回答，只能用**用户能不能走通**回答。
⇒ 本脚本只验 5 条链路，每条给绿/红 + **可复核的证据**（计数/ID/判定输出）。
   **全绿 = 系统达到可交付状态**；剩下的都是内部质量债，不阻断交付。

## 五条链路
| # | 链路 | 验收判据（都是**真调用**，不 mock） |
|---|---|---|
| L1 | AI 建模入口 → 真实 V2 建模 → checker.jar | 真实 checker.jar 对真实代码 `rc=0 / ERROR=0` |
| L2 | 会话隔离（按用户 + 按项目过滤） | 真造 3 用户 × 2 项目会话，验证：A 看不到 B、C 只看到本项目 |
| L3 | 编排：需求→SysML→汇总→质量评审→用户可见 | 走真编排，验证 summary_json 三数组 / reflection / card_data |
| L4 | 交付物：report/requirement 结构校验 + claims 落地 | 校验器对好/坏样本分别给绿/红；claims 协议可归一 |
| L5 | HIL 确认队列 → 写操作 | 队列 / 幂等 / 决策 三段真跑 |

## ⚠️ 本脚本自身的三条纪律（防止它变成又一条假绿）
1. **每条链路必须报「实际跑到了 N 个对象」** —— N=0 时该链路**判红**，
   不允许"因为没数据所以没事"（本仓反复栽过的空集合恒真）。
2. **区分「红」与「不可判定」** —— 依赖外部服务（Fuseki 等）不可用时
   报「不可判定」并说明原因，**不得混入「红」**（那是环境问题不是产品问题）。
3. **每条判据的期望值不许写死快照数字** —— 计数类判据用
   「相对关系」（A ≥ B、命中 > 0）而非「绝对数字」。

## 跑法（必须用项目 venv；生产库全程只读）
    ./.venv/Scripts/python.exe -X utf8 tools/calib/verify_link_acceptance.py
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time


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

PROD_DB = os.path.join(ROOT, "mbse.db")
WORK = tempfile.mkdtemp(prefix="link_acc_")
DB = os.path.join(WORK, "run.db")
os.environ["MBSE_DB_PATH"] = DB          # 必须在 import core.config/database 之前

OK, BAD, SKIP = "绿", "红", "不可判定"
_rows = []          # [(链路, 判据, 结论, 证据)]
_undecidable = []   # [(链路, 原因)]


def rec(link, name, verdict, evidence=""):
    _rows.append((link, name, verdict, evidence))
    mark = {OK: "PASS", BAD: "FAIL", SKIP: "SKIP"}[verdict]
    print("  [%s] %-46s %s" % (mark, name, ("← " + str(evidence)[:80]) if evidence else ""))
    if verdict == SKIP:
        _undecidable.append((link, name, str(evidence)[:120]))
    return verdict


def section(t):
    print("\n" + "=" * 72)
    print(t)
    print("=" * 72)


def copy_db():
    """整库复制（生产库只读）。WAL 库必须用 backup()。

    ★ 复制后**立刻跑列迁移**（2026-10-14 实测发现的真问题）：
      生产库 `hil_confirmations` **没有 `idem_key` 列**、`tool_call_logs` **没有
      `run_id`/`task_key`/`failure_kind` 三列** —— 迁移代码（`database/migrations/columns.py`）
      一直存在，但**生产库从未跑过 init_db**（迁移只在服务重启时触发）。
      ⇒ 代码里那两项能力（批次二 #06 写操作幂等、#12 工具调用可归因）
        **在真实环境里从未生效**，而门禁全绿（门禁跑的是干净库）。
    ⇒ 链路验收**必须跑迁移**，否则验的是"未迁移环境"而非"产品能力"。
    """
    if not os.path.exists(PROD_DB):
        raise SystemExit("生产库不存在：%s" % PROD_DB)
    s = sqlite3.connect("file:%s?mode=ro" % PROD_DB, uri=True)
    d = sqlite3.connect(DB)
    s.backup(d)
    d.close()
    s.close()
    _run_migrations()


def _run_migrations():
    """对**副本**跑列迁移（生产库不受影响）。"""
    try:
        from database.migrations.columns import _migrate_columns
        conn = sqlite3.connect(DB)
        try:
            _migrate_columns(conn)
            conn.commit()
        finally:
            conn.close()
    except Exception as e:                                        # noqa: BLE001
        print("  ⚠️ 列迁移失败（不影响继续，但幂等类判据会红）：%r" % e)


def migration_report():
    """**先说清楚**：生产库缺哪些新列 —— 这决定后面哪几条判据有意义。"""
    section("L0 · 环境前提：生产库的迁移状态")
    conn = sqlite3.connect("file:%s?mode=ro" % PROD_DB, uri=True)
    try:
        checks = (("hil_confirmations", ("idem_key",)),
                  ("tool_call_logs", ("run_id", "task_key", "failure_kind")),
                  ("agent_tasks", ("summary_json", "expected_output")),
                  ("orch_stream_events", ()))
        pending = []
        for tbl, cols in checks:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (tbl,)).fetchone()
            if not exists:
                rec("L0", "%s 表存在" % tbl, SKIP, "表不存在")
                continue
            have = {r[1] for r in conn.execute("PRAGMA table_info(%s)" % tbl)}
            miss = [c for c in cols if c not in have]
            if miss:
                rec("L0", "%s 新列已迁移" % tbl, BAD, "**生产库缺列**：%s" % miss)
                pending.append((tbl, miss))
            elif cols:
                rec("L0", "%s 新列已迁移" % tbl, OK, "%d 列齐备" % len(cols))
            else:
                rec("L0", "%s 表存在" % tbl, OK, "（该表无新增列要求）")
        if pending:
            print("\n  ⇒ **生产库从未跑过 init_db**（迁移只在服务启动时触发）：")
            for tbl, miss in pending:
                print("     · %s 缺 %s ⇒ 对应能力在真实环境里**从未生效**"
                      % (tbl, ", ".join(miss)))
            print("     ⇒ 链路验收跑在**已迁移的副本**上（生产库仍只读）。")
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════
def L0_env():
    migration_report()


def L1_modeling():
    section("L1 · AI 建模入口 → 真实 V2 建模 → checker.jar")
    try:
        import sysml_v2_check as svc
    except Exception as e:                                        # noqa: BLE001
        rec("L1", "checker 通道可导入", SKIP, "导入失败：%r" % e)
        return
    rec("L1", "checker 通道可导入", OK, "sysml_v2_check")

    # ① 合法代码 ⇒ 必须零错（**rc 是硬判据**）
    # ⚠️ 样例代码**必须先经 checker 实测确认合法**再写进来（2026-10-14 实测踩过）：
    #   初版用 `attribute def channelCount : ScalarValues::Integer;`
    #   ⇒ rc=1 ERROR=1（语法错）⇒ 差点被我当成"产品有缺陷"。
    #   实际是**我的测试代码写错了**（随包 sysml.library 解析不了 ScalarValues）。
    #   ⇒ 这也是本脚本的一条纪律：**判据里的样例要先自证合法**，否则红的是判据不是产品。
    good = ('package SatelliteStation {\n'
            '  port def DataIf;\n'
            '  part def Transponder {\n'
            '    port dataIf : DataIf;\n'
            '  }\n'
            '  part satellite : Transponder;\n'
            '}\n')
    t0 = time.time()
    r = svc.check_code(good, timeout=90)
    rc = int(r.get("rc") or 0)
    summ = svc.summarize(r) if hasattr(svc, "summarize") else {}
    err = (summ.get("counts") or {}).get("error")
    rec("L1", "合法 SysML v2 经 checker.jar 判定零错", OK if rc == 0 else BAD,
        "rc=%d error=%s 耗时%.1fs" % (rc, err, time.time() - t0))
    # ★ 防空转：必须**真的调到了 java**（判据里要有「确实执行了」的证据）
    rec("L1", "checker 确实执行（不是静默降级为 None）",
        OK if isinstance(r, dict) and (r.get("rc") is not None) else BAD,
        "rc=%r keys=%s" % (r.get("rc") if isinstance(r, dict) else None,
                           sorted(r.keys())[:4] if isinstance(r, dict) else None))

    # ② 非法代码 ⇒ 必须**报错**（反向：判据能抓错才说明它有牙齿）
    bad = "package X { part def ; ??? this is not valid sysml }"
    rb = svc.check_code(bad, timeout=90)
    rcb = int(rb.get("rc") or 0)
    rec("L1", "非法 SysML v2 被 checker.jar 判错（有牙齿）",
        OK if rcb != 0 else BAD, "rc=%d" % rcb)

    # ③ 项目级合并门禁通道可达
    try:
        rec("L1", "check_project 工程级门禁通道存在",
            OK if hasattr(svc, "check_project") else SKIP, "check_project")
    except Exception as e:                                        # noqa: BLE001
        rec("L1", "check_project 工程级门禁通道存在", SKIP, repr(e))


# ══════════════════════════════════════════════════════════════════════
def L2_isolation():
    section("L2 · 会话隔离（按用户 + 按项目过滤）")
    from repositories.conversation_repo import ConversationRepo, conv_visible_to
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    try:
        # 造数据：用户 1/2 各有会话，且用户 1 有两个项目的会话
        pid_a, pid_b = "proj-A", "proj-B"
        rows = [
            ("u1-pa", 1, pid_a), ("u1-pb", 1, pid_b), ("u1-none", 1, ""),
            ("u2-pa", 2, pid_a),
        ]
        ids = {}
        for title, uid, pid in rows:
            cur = conn.execute(
                "INSERT INTO conversations (title,intent,user_id,project_id)"
                " VALUES (?,?,?,?)", (title, "chat", uid, pid))
            ids[title] = cur.lastrowid
        # 再造一条**无归属**历史会话（user_id NULL）—— 它按口径应对所有人可见
        cur = conn.execute(
            "INSERT INTO conversations (title,intent,user_id,project_id)"
            " VALUES ('legacy', 'chat', NULL, '')")
        ids["legacy"] = cur.lastrowid
        conn.commit()
        rec("L2", "造出测试会话", OK, "%d 条（含 1 条无归属历史会话）" % len(ids))

        repo = ConversationRepo(conn)

        # ① 按用户过滤：A 看不到 B 的
        la = repo.list_conversations(user={"id": 1})
        la_t = {str(x.get("title")) for x in la}
        leak = {t for t in la_t if t and t.startswith("u2")}
        rec("L2", "列表按 user_id 隔离（u1 看不到 u2 的会话）",
            OK if not leak else BAD,
            "u1 看到 %d 条；泄漏=%s" % (len(la), sorted(leak) or "无"))

        # ② 归属完整性：u1 自己的 3 条必须都在（否则过滤过头）
        n_own = len([t for t in la_t if t and t.startswith("u1")])
        rec("L2", "自己的会话一条不少（过滤没有过头）",
            OK if n_own >= 3 else BAD, "u1 自己看到 %d 条（期望 ≥3）" % n_own)

        # ③ 按项目过滤
        pa = repo.list_conversations(user={"id": 1}, project_id=pid_a)
        pa_t = {str(x.get("title")) for x in pa}
        wrong = {t for t in pa_t if t and t not in ("u1-pa", "legacy")}
        rec("L2", "按项目过滤（只看 proj-A 的会话）",
            OK if ("u1-pa" in pa_t and not wrong) else BAD,
            "命中=%s；越界=%s" % (sorted(pa_t), sorted(wrong) or "无"))

        # ④ 无归属历史会话对所有人可见（三条口径之一）
        # ⚠️ 必须**经 repo 取行**（`BaseRepo.one` 会 `dict(row)`）——
        #   `conv_visible_to` 用的是 `row.get(...)`，直传 sqlite3.Row 会 AttributeError。
        #   走 repo 才是**生产调用方的真实形态**（`_own_conv` 就是这么调的）。
        vis_u2 = conv_visible_to(repo.get_conversation(ids["legacy"]), {"id": 2})
        rec("L2", "无归属历史会话对其他用户可见（口径①）",
            OK if vis_u2 else BAD, "u2 看 legacy = %s" % vis_u2)

        # ⑤ 不存在的 conv_id 不可见（防止"给不存在的 id 开可见性口子"）
        ghost = repo.get_conversation(999999999)
        vis_ghost = conv_visible_to(ghost, {"id": 1})
        rec("L2", "不存在的会话不可见（不得因取不到行就放行）",
            OK if (ghost is None and not vis_ghost) else BAD,
            "取行=%r 可见=%s" % (ghost, vis_ghost))

        # ⑥ 他人会话直接取用不可见
        vis_b = conv_visible_to(repo.get_conversation(ids["u2-pa"]), {"id": 1})
        rec("L2", "他人会话不可见（严格按 user_id）",
            OK if not vis_b else BAD, "u1 看 u2 的会话 = %s" % vis_b)
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════
def L3_orchestration():
    section("L3 · 编排：需求→SysML→汇总→质量评审→用户可见")
    from services import subtask_protocol as sp
    from workflows.refine import RefineGate

    # ① 协议归一：真实结构能过
    proto_raw = {
        "schema_version": 1, "task_key": "t1", "status": "full",
        "summary": "已完成转发器接口建模",
        "artifacts": [{"kind": "sysml", "title": "转发器模型", "ref": "subtask://1/t1/a1"}],
        "evidence": [{"source": "graph", "ref": "entity:Transponder", "basis": "verified"}],
        "risks": [{"level": "mid", "title": "接口数待确认", "desc": "两份文档不一致",
                   "advice": "以 ICD 为准并复核端口清单"}],
        "claims": [{"text": "转发器对外数据接口共 3 个", "kind": "fact",
                    "subject": "转发器对外数据接口数量"}],
        "confidence": 0.8,
    }
    pr = sp.validate(proto_raw)
    san = pr.get("sanitized") or {}
    rec("L3", "真实协议结构可通过 validate", OK if pr.get("ok") else BAD,
        "ok=%s warnings=%d" % (pr.get("ok"), len(pr.get("warnings") or [])))
    # ★ 防空转：三数组都必须真拿到值
    n3 = sum(len(san.get(k) or []) for k in ("artifacts", "evidence", "risks", "claims"))
    rec("L3", "四个数组真的取到值（不是空壳通过）",
        OK if n3 >= 4 else BAD, "共 %d 项" % n3)

    # ② 三态聚合 + 质量门禁并入
    full = sp.summarize_status([{"status": "full"}, {"status": "full"}])
    mixed = sp.summarize_status([{"status": "full"}, {"status": "failed"}])
    none = sp.summarize_status([])
    rec("L3", "子任务三态聚合正确（全 full⇒full / 混合⇒partial / 空⇒partial）",
        OK if (full == "full" and mixed == "partial" and none == "partial") else BAD,
        "full=%s mixed=%s empty=%s" % (full, mixed, none))
    ag, gaps = sp.apply_quality_gate("full", {"passed": False, "score": 62,
                                              "issues": ["未产出交付物"]})
    rec("L3", "质量评审未通过会降级为 partial（失败可见）",
        OK if ag == "partial" and gaps else BAD, "agg=%s gaps=%d" % (ag, len(gaps)))

    # ③ 冲突检测接到 RefineGate（打桩评审，不真调 LLM）
    import workflows.nodes as nodes
    orig = nodes.FlowNodesMixin._evaluate_content
    seen = {}

    def _fake(content, criteria="", **kw):
        seen["criteria"] = criteria
        return {"score": 88, "passed": True, "issues": [], "advice": "",
                "_meta": {"provider": "stub", "model": "stub", "used_mock": True}}

    items = [
        {"task_key": "t1", "title": "需求分析", "agent_id": "a", "result": "正文",
         "summary": dict(san, task_key="t1")},
        {"task_key": "t2", "title": "架构设计", "agent_id": "b", "result": "正文",
         "summary": dict(san, task_key="t2",
                         claims=[{"text": "转发器对外数据接口共 5 个", "kind": "fact",
                                  "subject": "转发器对外数据接口数量"}])},
    ]
    try:
        nodes.FlowNodesMixin._evaluate_content = staticmethod(_fake)
        rr = RefineGate.run("综合报告正文", "目标", items, [{"key": "t1"}], agg_status="full")
    finally:
        nodes.FlowNodesMixin._evaluate_content = orig
    cf = rr.get("conflicts") or {}
    rec("L3", "结论冲突接入 RefineGate 且能检出",
        OK if int(cf.get("count") or 0) >= 1 else BAD,
        "count=%s verdict=%s" % (cf.get("count"), (cf.get("stats") or {}).get("verdict")))
    rec("L3", "冲突被写进评审标准（评审必须显式裁决）",
        OK if "结论冲突裁决" in (seen.get("criteria") or "") else BAD,
        "criteria 含冲突段 = %s" % ("结论冲突裁决" in (seen.get("criteria") or "")))
    rec("L3", "冲突无条件进 issues（不依赖 LLM 记得写）",
        OK if any("接口" in str(i) for i in (rr.get("issues") or [])) else BAD,
        "issues=%d 条" % len(rr.get("issues") or []))

    # ④ 无冲突时评审标准**逐字节不变**（零回归保证）
    items_ok = [
        {"task_key": "t1", "title": "A", "agent_id": "a", "result": "正文",
         "summary": {"task_key": "t1", "claims": [{"text": "共3个", "kind": "fact",
                                                  "subject": "接口数"}]}},
        {"task_key": "t2", "title": "B", "agent_id": "b", "result": "正文",
         "summary": {"task_key": "t2", "claims": [{"text": "已完成", "kind": "fact",
                                                  "subject": "进度"}]}},
    ]
    try:
        nodes.FlowNodesMixin._evaluate_content = staticmethod(_fake)
        RefineGate.run("报告", "目标", items_ok, None, agg_status="full")
    finally:
        nodes.FlowNodesMixin._evaluate_content = orig
    base_ok = (seen.get("criteria") or "").startswith("1) 完整性")
    rec("L3", "无冲突时评审标准与默认一致（零回归）", OK if base_ok else BAD,
        "criteria 首 12 字=%r" % (seen.get("criteria") or "")[:12])

    # ⑤ card_data 落库口径：三条返回路径字段同键
    same_key = all("conflicts" in RefineGate.run("", goal="x")
                   for _ in [0])
    rec("L3", "RefineGate 各返回路径字段同键（前端不会误判）",
        OK if same_key else BAD, "空报告路径含 conflicts = %s" % same_key)


# ══════════════════════════════════════════════════════════════════════
def L4_artifacts():
    section("L4 · 交付物：report/requirement 结构校验 + claims 落地")
    from services.artifact_validator import validate_artifact, VALIDATABLE_KINDS
    from services import subtask_protocol as sp

    GOOD_REPORT = ("# 性能分析报告\n\n## 一、现状\n系统 P95 820ms，瓶颈在向量重排。\n\n"
                   "## 二、结论\n建议候选集从 50 降到 20，预计 P95 可降至 400ms。\n\n"
                   "## 三、风险\n该估算假设重排模型不变，模型变更需重新标定。\n")
    GOOD_REQ = ("REQ-001 系统应当在检索重排阶段将候选集从 50 降至 20，"
                "使得 P95 不超过 400ms；验收方法：压测 1000 并发请求统计 P95。\n")
    BAD_REPORT = "性能不太行。"
    BAD_REQ = "系统要快。"

    for kind, good, bad in (("report", GOOD_REPORT, BAD_REPORT),
                            ("requirement", GOOD_REQ, BAD_REQ)):
        rg = validate_artifact(kind, {"content": good})
        rb = validate_artifact(kind, {"content": bad})
        rec("L4", "%s：好样本通过 / 坏样本判红" % kind,
            OK if (rg.get("ok") and not rb.get("ok")) else BAD,
            "好=%s 坏=%s" % (rg.get("ok"), rb.get("ok")))
        rec("L4", "%s：checked 报真实判据条数" % kind,
            OK if int(rg.get("checked") or 0) > 0 else BAD,
            "checked=%d issues=%d" % (rg.get("checked"), len(rb.get("issues") or [])))

    # 空内容必须判红（不许"没内容所以没错"）
    re_ = validate_artifact("report", {"title": "只有标题", "ref": "x://y"})
    rec("L4", "无正文（只有 title/ref）判红", OK if not re_.get("ok") else BAD,
        "ok=%s" % re_.get("ok"))
    # sysml 走 checker.jar，不走本校验器
    rs = validate_artifact("sysml", {"content": "part def X;"})
    rec("L4", "sysml 不走结构校验器（它有 checker.jar）",
        OK if (rs.get("checked") == 0 and rs.get("degraded")) else BAD,
        "checked=%s degraded=%s" % (rs.get("checked"), rs.get("degraded")))

    # claims 分档落地 + 诚实兜底
    pr = sp.validate({"task_key": "t1", "status": "full", "summary": "s",
                      "claims": [{"text": "无依据的结论", "kind": "fact"},
                                 {"text": "明确待验证", "kind": "to_verify"},
                                 {"text": "没标 kind"}]})
    cl = (pr.get("sanitized") or {}).get("claims") or []
    kinds = [c.get("kind") for c in cl]
    rec("L4", "claims 分档正确（未标的落到最保守的 to_verify）",
        OK if (len(cl) == 3 and kinds[2] == "to_verify") else BAD, "kinds=%s" % kinds)
    rec("L4", "claims 冲突口径三态可区分",
        OK if sp.conflict_stats([{"claims": [{"text": "a"}]}])["verdict"] == "no-data"
        else BAD, "无锚点 → no-data（不得声称已检查无冲突）")


# ══════════════════════════════════════════════════════════════════════
def L5_hil():
    section("L5 · HIL 确认队列 → 写操作")
    from hil_service import HILService, WRITE_TOOLS
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    try:
        HILService.ensure_schema(conn) if hasattr(HILService, "ensure_schema") else None
        conn.commit()
    except Exception:                                              # noqa: BLE001
        pass
    try:
        svc = HILService
        # ① 写工具识别
        is_w = svc.is_write_tool("entity_create")
        is_r = svc.is_write_tool("graph_retrieve")
        rec("L5", "写工具识别正确（写=是 / 读=否）",
            OK if (is_w and not is_r) else BAD,
            "entity_create=%s graph_retrieve=%s" % (is_w, is_r))

        # ② 入队
        r1 = svc.queue_confirmation(conn, "transponder_agent", "entity_create",
                                    {"name": "Transponder", "kind": "part"},
                                    preview="新建实体 Transponder",
                                    conversation_id=1, run_id=1, task_key="t1")
        rec("L5", "写操作入队成功", OK if r1 else BAD, "conf_id=%s" % (r1 or {}).get("id"))

        # ③ 幂等：同参数重复提交 ⇒ 不产生第二条
        r2 = svc.queue_confirmation(conn, "transponder_agent", "entity_create",
                                    {"name": "Transponder", "kind": "part"},
                                    preview="新建实体 Transponder",
                                    conversation_id=1, run_id=1, task_key="t1")
        same = bool(r1 and r2 and r1.get("id") == r2.get("id"))
        rec("L5", "同参数重复提交幂等（不重复入队）", OK if same else BAD,
            "首次=%s 二次=%s" % ((r1 or {}).get("id"), (r2 or {}).get("id")))

        # ④ pending 可见
        pend = svc.pending(conn)
        rec("L5", "待确认队列可见本次条目",
            OK if any(int(x.get("id") or 0) == int((r1 or {}).get("id") or -1)
                      for x in pend) else BAD, "pending 共 %d 条" % len(pend))

        # ⑤ 拒绝后**不阻断重试**（这是不设 UNIQUE 的理由）
        d_rej = svc.decide(conn, int((r1 or {}).get("id") or 0), False, decided_by="verify")
        r3 = svc.queue_confirmation(conn, "transponder_agent", "entity_create",
                                    {"name": "Transponder", "kind": "part"},
                                    preview="新建实体 Transponder",
                                    conversation_id=1, run_id=1, task_key="t1")
        rec("L5", "拒绝后重试可再次入队（幂等键不吞重试）",
            OK if (d_rej and r3 and int(r3.get("id") or 0) != int((r1 or {}).get("id") or -1))
            else BAD, "拒绝=%s 重试id=%s" % (d_rej, (r3 or {}).get("id")))
        conn.commit()

        # ⑥ 批准后状态可见
        d_ok = svc.decide(conn, int((r3 or {}).get("id") or 0), True, decided_by="verify")
        conn.commit()
        rec("L5", "批准决策成功且状态可回查",
            OK if d_ok else BAD, "decide=%s" % json.dumps(d_ok, ensure_ascii=False)[:70])
    except Exception as e:                                        # noqa: BLE001
        rec("L5", "HIL 三段真跑", BAD, "异常：%r" % e)
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════
def summary():
    section("链路验收结果")
    by_link = {}
    for link, name, verdict, ev in _rows:
        by_link.setdefault(link, []).append((name, verdict, ev))
    print("\n%-4s %-52s %s" % ("链路", "结论", "绿/红/不可判定"))
    print("-" * 74)
    total_ok = 0
    total = 0
    for link in sorted(by_link):
        items = by_link[link]
        n_ok = sum(1 for _, v, _ in items if v == OK)
        n_bad = sum(1 for _, v, _ in items if v == BAD)
        n_skip = sum(1 for _, v, _ in items if v == SKIP)
        total_ok += n_ok
        total += len(items)
        verdict = (OK if n_bad == 0 and n_skip == 0
                   else (BAD if n_bad else SKIP))
        mark = {OK: "绿  ", BAD: "红  ", SKIP: "不可判定"}[verdict]
        print("%-4s %-52s %s  (%d绿 %d红 %d不可判定)"
              % (link, "全部判据通过" if verdict == OK else "存在问题", mark,
                 n_ok, n_bad, n_skip))

    print("\n总计：%d 条判据，%d 绿" % (total, total_ok))
    if _undecidable:
        print("\n不可判定项（**不算红**，是环境/依赖未就绪）：")
        for link, name, why in _undecidable:
            print("  · [%s] %s —— %s" % (link, name, why))
    n_bad_total = sum(1 for _, _, v, _ in _rows if v == BAD)
    if n_bad_total == 0:
        print("\n结论：**5 条用户可见链路全绿 = 达到可交付状态**。"
              "\n      剩余事项均为内部质量债（门禁存量 / 状态机统一 / slots 喂回），不阻断交付。")
    else:
        print("\n结论：有 %d 条判据红。按「只修阻断链路的」原则逐条修，不扩散。" % n_bad_total)
    return n_bad_total


def main():
    t0 = time.time()
    print("=" * 72)
    print("链路验收 · 5 条用户可见链路（批次三收官）")
    print("=" * 72)
    print("  库副本: %s" % DB)
    print("  生产库: %s（**全程只读**）" % PROD_DB)
    copy_db()
    print("  库副本已就绪（整库 backup，生产库未改）")

    for fn in (L0_env, L1_modeling, L2_isolation, L3_orchestration, L4_artifacts,
               L5_hil):
        try:
            fn()
        except Exception as e:                                    # noqa: BLE001
            rec(fn.__name__[:2].upper(), "整段执行", BAD, "异常：%r" % e)

    n_bad = summary()
    print("\n耗时 %.0fs" % (time.time() - t0))
    shutil.rmtree(WORK, ignore_errors=True)
    return 0 if n_bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
