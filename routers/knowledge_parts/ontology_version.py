# -*- coding: utf-8 -*-
"""知识库路由分片：本体版本链/快照/发布/校验/绑定/导入导出。

由 tools/split_router_knowledge.py 从 routers/knowledge.py 机械切分而成；⚠️ 切分脚本**已一次性执行完毕、不可重跑**（重跑会以薄入口为输入、覆盖本目录）—— 此后本文件按普通源码维护。"""
from routers.knowledge_parts.shared import *

# P1-1（2026-10-15）：发布门禁档位（core.config 单一真源；显式 import——shared 的 * 不带它）
from core import config as _cfg

_GATE_DESC = {"off": "不拦截（体检仅留痕）", "high": "仅 high 拦截", "warn": "warn 及以上拦截"}


def _publish_gate_level() -> str:
    """P1-1：读 ontology.publish_gate，未知值兜底 high（宁严勿漏）并留痕打印。"""
    lv = str(_cfg.get("ontology", "publish_gate", "high") or "high").strip().lower()
    if lv not in _GATE_DESC:
        print(f"[ontology_version] ⚠️ ontology.publish_gate 非法值 {lv!r}，回退 high（宁严勿漏）")
        return "high"
    return lv


def _gate_run(conn, cq_record: bool = True):
    """P1-1 建立门禁单一真源；P1-4（2026-10-15）重构为 `_gate_run`：
    体检（编辑态）+ 门禁判定 + CQ（能力问题）一次完成，返回 (gate, chk)：
      · gate=None 放行 / dict=拦截 payload（调用方 JSONResponse 包裹 400）
      · chk = 完整体检报告（发布回包/留痕消费）；off 档为 None
      · chk["cq"] = CQ 摘要（仅放行时跑——被拦截的发布走不到发布，不必跑）
    ⚠️ 修复 P1-1 遗留缺陷：publish/release 成功路径原先引用不存在的 `chk`
      （_gate_block 不再返回体检报告 → 一旦放行即 NameError）。本函数把报告带回来。

    口径=**待发布数据**（编辑态即将成为新快照，不拿旧快照体检 —— 决策 D2 配套）。
    档位语义（「最低拦截级别」）：
      off  → 不拦截（体检跳过；发布说明由调用方自行汇总）
      high → high>0 拦（默认，与 2026-10-15 之前的硬编码行为等价）
      warn → high>0 或 warn>0 拦（low/info 永不阻断）
    CQ 不拦截（R1 拍板：validation≠verification，红可能是有意未完成）——
    只随发布回包/留痕下发 + 落 ontology_cq_logs，可追责。
    """
    lv = _publish_gate_level()
    if lv == "off":
        return None, None
    chk = _ontology_check(conn, rows=_ont_edit_rows(conn))
    blocked = chk["high"] > 0 if lv == "high" else (chk["high"] + chk["warn"]) > 0
    if not blocked:
        # P1-4：放行前跑一遍 CQ（fail/skip 只留痕不拦截）；执行器异常降级留痕不阻断发布
        try:
            from services import ontology_cq as _ocq
            _ocq.ensure_seed(conn)
            chk["cq"] = _ocq.summarize(_ocq.run_cqs(conn, source="gate", record=cq_record))
        except Exception as _e:
            chk["cq"] = {"error": f"CQ 执行失败（降级留痕，不影响发布）：{type(_e).__name__}: {_e}"}
        return None, chk
    sev = "high" if chk["high"] > 0 else "warn"
    tops = [i["message"] for i in chk["issues"] if i["severity"] == sev][:5]
    return {"error": f"一致性检查未过发布门禁（档位 {lv}={_GATE_DESC[lv]}，{sev} {chk[sev]} 项）：{'；'.join(tops)}"
                      f"（请在本体页「🩺 一致性检查」按 fix 提示修复后重试；档位可在设置页 ontology.publish_gate 调整）",
            "gate": lv, "gate_blocked_on": sev, "gate_counts": {"high": chk["high"], "warn": chk["warn"], "low": chk["low"], "info": chk["info"]}}, chk


@router.get("/api/knowledge/ontology/validate")
def ontology_validate(instances: int = 0, scope: Optional[str] = None,
                      record: int = 1, cq: int = 1, dl: int = 0, conn=Depends(db_session)):
    """方案 D（2026-08-29）：本体一致性校验接口化。

    返回结构化问题清单（2026-09-23 晚起 12 条规则）：循环继承 / 悬空 parent / 孤立类 /
    关系缺 dom-range / dom-range 悬空 / 重名 / 属性缺适用类型 / 属性适用类型悬空 /
    一侧多声明（info）/ **声明 vs 存量边** / 实例类型未注册 / 存量实例不满足约束。

    · `instances=1`：附带**逐实例**校验（xsd/白名单/端点/基数）——较慢，故不默认；
      逐实例明细来自 `services.ontology_migration` 的既有校验器，不重写。
    · `scope=<类型名>`：只校验该类型（配合 instances 用，缩小扫描面）。
    · 报告带 `degraded`：任何扫描被跳过的原因都会列出来（安静地少查 = 假绿）。

    P0-2（2026-10-15）：体检结果落库到 `ontology_health_logs`（趋势线数据源）。

    P1-4（2026-10-15）：`cq=1`（默认）附带 CQ（能力问题）执行结果 —— validation 层
    与上方 verification 层互补；fail/skip **不混入四档计数**（它们不是结构问题），
    独立 `cq` 节下发；record=0 的探针同时不落 CQ 执行历史（同一防污染纪律）。

    P2-1（2026-10-15）：`dl=1`（默认关）附带 Pellet DL 深检（`dl` 节）—— 实测 ~2s
    （vs 结构体检 44ms）故不默认、不进发布门禁，作为手动低频深检入口；
    DL 语义矛盾（如 reflexive+asymmetric 互斥）只有它能抓（P2-1 实证：结构规则曾全绿
    而 Pellet 判 inconsistent）。结果**不混入四档计数**（同为 validation 层），
    任何失败降级为 error 字段不拖垮体检结论。

    ⚠️ `record` 参数的存在理由（**实测踩坑后补的，别当冗余删掉**）：
      落库最初无条件开启。实测被验证脚本的「端点冒烟」段污染 —— 该段通过 **HTTP** 调本端点
      4 次、每次留一行；跑一次脚本趋势线就多 4 个点，几轮后"人/Agent 的真实体检历史"
      被机器噪声冲掉，趋势线变得不可读（数据很热闹，但没用）。
      而把落库塞进 `_ontology_check` 内部更糟：publish/release 发布门禁与验证脚本里
      数百次调用都会写库。
      ⇒ 落库必须是**显式选择**：默认开（UI 与 Agent 的真实体检都记），
      探针/冒烟类调用方用 `record=0` 显式关掉（验证脚本的端点冒烟已改）。
    """
    rep = _ontology_check(conn, instances=True, scope=scope) if instances \
        else _ontology_check(conn)
    if record:
        _record_health_log(conn, rep, note="validate" + ("+instances" if instances else ""))
    # P1-4：附带 CQ 结果（不混入四档计数；执行器异常降级留痕不拖垮体检结论）
    if cq:
        try:
            from services import ontology_cq as _ocq
            _ocq.ensure_seed(conn)
            rep["cq"] = _ocq.summarize(_ocq.run_cqs(conn, source="validate", record=bool(record)))
        except Exception as _e:
            rep["cq"] = {"error": f"CQ 执行失败（降级留痕）：{type(_e).__name__}: {_e}"}
    # P2-1：dl=1 时附带 Pellet DL 深检（手动低频；失败降级 error 字段，不拖垮体检结论）
    if dl:
        try:
            from services.swrl_engine import run_dl_check
            rep["dl"] = run_dl_check(conn)
        except Exception as _e:  # 双保险：run_dl_check 自身承诺不抛，这里兜 import 等
            rep["dl"] = {"ran": False, "error": f"DL 深检启动失败：{type(_e).__name__}: {_e}"}
    return rep


@router.get("/api/knowledge/ontology/health-trend")
def ontology_health_trend(limit: int = 30, conn=Depends(db_session),
                          user=Depends(current_user)):
    """P0-2（2026-10-15）：本体体检历史趋势（最近 N 次，默认 30）。

    返回 {history: [...升序...], summary: {...}}，每行含各档位计数 + 口径元数据
    （source / version_id / rule_version / degraded_count / instances_checked）。

    **趋势线最容易骗人的地方**（故这三项必须随行返回，前端不得省略）：
      · `rule_version` 变了 ⇒ 计数变化可能来自**判据变严**，不是数据变差；
      · `degraded_count>0` ⇒ 该次结论**不完整**，不能与完整体检直接比；
      · `instances_checked=1` ⇒ 慢口径（多扫了逐实例），与默认口径不可直接比。
    前端据此在图上标注断点，而不是把三条不同口径的点连成一条"下降曲线"。
    """
    limit = max(1, min(int(limit or 30), 200))
    rows = conn.execute(
        "SELECT id, ts, source, ontology_version_id, total, high, warn, low, info, "
        "rule_version, rule_count, degraded_count, instances_checked, codes, note "
        "FROM ontology_health_logs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    hist = []
    for r in rows:
        o = dict(r)
        try:
            o["codes"] = json.loads(o.get("codes") or "{}")
        except Exception:
            o["codes"] = {}
        hist.append(o)
    hist.reverse()          # 升序：时间从左到右，前端可直接画线
    summary = {}
    if hist:
        last = hist[-1]
        summary = {
            "latest": last,
            "worst_high": max((h["high"] or 0) for h in hist),
            "worst_low": max((h["low"] or 0) for h in hist),
            # 问题码总览：跨全部历史累计，便于回答"最常犯哪类错"
            "codes_total": {},
            # 口径混用提示：不同 rule_version / 口径的点不可直接连线
            "mixed_rule_versions": sorted({h["rule_version"] for h in hist if h["rule_version"]}),
            "has_degraded": any((h["degraded_count"] or 0) > 0 for h in hist),
            "has_slow_scan": any(h["instances_checked"] for h in hist),
        }
        agg = {}
        for h in hist:
            for k, v in (h.get("codes") or {}).items():
                agg[k] = agg.get(k, 0) + int(v or 0)
        summary["codes_total"] = dict(sorted(agg.items(), key=lambda kv: -kv[1]))
    return {"ok": True, "count": len(hist), "history": hist, "summary": summary}


# ══ P1-4（2026-10-15）：CQ（能力问题）管理/执行 ═══════════════════════════════
# 机制说明见 services/ontology_cq.py；前端入口：本体页顶栏「❓ CQ」管理面板
# + 🩺 一致性校验结果滑窗的 CQ 分区（验证与确认一个入口看全）。

@router.get("/api/knowledge/ontology/cqs")
def ontology_cq_list(conn=Depends(db_session), user=Depends(current_user)):
    """CQ 清单（首次访问自动补内置种子；种子可改可删，不会被重新塞回）。"""
    from services import ontology_cq as _ocq
    _ocq.ensure_seed(conn)
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM ontology_questions ORDER BY id").fetchall()]
    # 附最近一次执行状态（轻查询：每题最近 1 条日志）
    last = {}
    for r in conn.execute(
            "SELECT l.question_id, l.ts, l.status, l.row_count, l.message FROM ontology_cq_logs l "
            "JOIN (SELECT question_id, MAX(id) mid FROM ontology_cq_logs GROUP BY question_id) t "
            "ON l.id=t.mid").fetchall():
        last[r["question_id"]] = {"ts": r["ts"], "status": r["status"],
                                  "row_count": r["row_count"], "message": r["message"]}
    for q in rows:
        q["last_run"] = last.get(q["id"])
    return {"ok": True, "questions": rows}


@router.post("/api/knowledge/ontology/cqs")
def ontology_cq_add(body: dict = None, conn=Depends(db_session),
                    user=Depends(require_any_permission(WRITE_PERMS))):
    """登记 CQ：question（中文业务问题）/ kind（仅 sql）/ query（只读 SELECT）/ expected（empty|nonempty）。"""
    from services import ontology_cq as _ocq
    b = body or {}
    question = str(b.get("question") or "").strip()
    query = str(b.get("query") or "").strip()
    kind = str(b.get("kind") or "sql").strip()
    expected = str(b.get("expected") or "empty").strip()
    if not question:
        return JSONResponse({"error": "question 不能为空"}, 400)
    if kind != "sql":
        return JSONResponse({"error": f"暂仅支持 kind=sql（当前: {kind}）"}, 400)
    if expected not in ("empty", "nonempty"):
        return JSONResponse({"error": "expected 必须是 empty|nonempty"}, 400)
    ok, err = _ocq.validate_query(query)
    if not ok:
        return JSONResponse({"error": err}, 400)
    cur = conn.execute(
        "INSERT INTO ontology_questions (question, kind, query, expected, enabled, source, created_by) "
        "VALUES (?,?,?,?,1,'user',?)",
        (question, kind, query, expected, _actor(user)))
    conn.commit()
    audit(_actor(user), "ontology_cq_add", f"登记 CQ#{cur.lastrowid}: {question}", conn=conn)
    return {"ok": True, "id": cur.lastrowid}


@router.put("/api/knowledge/ontology/cqs/{qid}")
def ontology_cq_update(qid: int, body: dict = None, conn=Depends(db_session),
                       user=Depends(require_any_permission(WRITE_PERMS))):
    """修改 CQ（问题/查询/期望/启停）。查询改动重新过只读白名单。"""
    from services import ontology_cq as _ocq
    row = conn.execute("SELECT * FROM ontology_questions WHERE id=?", (qid,)).fetchone()
    if not row:
        return JSONResponse({"error": "not found"}, 404)
    b = body or {}
    question = str(b.get("question") or row["question"]).strip()
    query = str(b.get("query") or row["query"]).strip()
    expected = str(b.get("expected") or row["expected"]).strip()
    enabled = 1 if b.get("enabled") is None else (1 if b.get("enabled") else 0)
    if expected not in ("empty", "nonempty"):
        return JSONResponse({"error": "expected 必须是 empty|nonempty"}, 400)
    ok, err = _ocq.validate_query(query)
    if not ok:
        return JSONResponse({"error": err}, 400)
    conn.execute(
        "UPDATE ontology_questions SET question=?, query=?, expected=?, enabled=?, "
        "updated_at=CURRENT_TIMESTAMP WHERE id=?", (question, query, expected, enabled, qid))
    conn.commit()
    audit(_actor(user), "ontology_cq_update", f"修改 CQ#{qid}: {question}", conn=conn)
    return {"ok": True}


@router.delete("/api/knowledge/ontology/cqs/{qid}")
def ontology_cq_delete(qid: int, conn=Depends(db_session),
                       user=Depends(require_any_permission(WRITE_PERMS))):
    row = conn.execute("SELECT question FROM ontology_questions WHERE id=?", (qid,)).fetchone()
    if not row:
        return JSONResponse({"error": "not found"}, 404)
    conn.execute("DELETE FROM ontology_questions WHERE id=?", (qid,))
    conn.commit()
    audit(_actor(user), "ontology_cq_delete", f"删除 CQ#{qid}: {row['question']}", conn=conn)
    return {"ok": True}


@router.post("/api/knowledge/ontology/cqs/run")
def ontology_cq_run(body: dict = None, conn=Depends(db_session), user=Depends(current_user)):
    """手动执行（body.id 指定单题；缺省全部 enabled）。执行历史落 ontology_cq_logs。"""
    from services import ontology_cq as _ocq
    _ocq.ensure_seed(conn)
    b = body or {}
    qid = int(b["id"]) if b.get("id") is not None else None
    results = _ocq.run_cqs(conn, source="manual", record=True, qid=qid)
    return {"ok": True, "results": results,
            "summary": _ocq.summarize(results) if qid is None else None}


@router.get("/api/knowledge/ontology/cqs/logs")
def ontology_cq_logs(limit: int = 50, conn=Depends(db_session), user=Depends(current_user)):
    """CQ 执行历史（最近 N 条，默认 50，含来源 gate/validate/manual —— 追责「谁放行了带红 CQ 的发布」）。"""
    limit = max(1, min(int(limit or 50), 500))
    rows = [dict(r) for r in conn.execute(
        "SELECT l.*, q.question FROM ontology_cq_logs l "
        "LEFT JOIN ontology_questions q ON q.id=l.question_id "
        "ORDER BY l.id DESC LIMIT ?", (limit,)).fetchall()]
    return {"ok": True, "logs": rows}


@router.get("/api/knowledge/ontology/changelog")
def ontology_changelog(type_id: Optional[int] = None, conn=Depends(db_session)):
    """FR-KG-4 补 G7：本体类型变更历史（按类型过滤，倒序；before/after 解析为 dict）。

    只读接口，无需权限；无 type_id 返回全部类型的最近 200 条（2026-09-02：顶部「📜 变更历史」全局入口）。
    """
    if not type_id:
        rows = conn.execute(
            "SELECT * FROM ontology_change_logs ORDER BY id DESC LIMIT 200").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM ontology_change_logs WHERE type_id=? ORDER BY id DESC", (type_id,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        for k in ("before", "after"):
            try:
                d[k] = json.loads(d[k] or "{}")
            except Exception:
                d[k] = {}
        out.append(d)
    return out


@router.get("/api/knowledge/ontology/version")
def ontology_version(conn=Depends(db_session)):
    """本体整体版本（SemVer）：当前版本 + 版本链历史（倒序，含发布状态/快照统计/兼容性/diff）。

    只读接口，无需权限；无版本记录时返回基线 v1.0.0。

    2026-09-08 版本语义收敛：current 优先返回 **active 已发布版本**（消费基线）——
    编辑态只写变更留痕不升版本号；附带 dirty（最新变更留痕晚于基线发布时间 → 有未发布变更）
    与 pending（未发布变更条数），供顶栏徽章/版本历史展示「 vX.Y.Z ·有未发布变更 」。
    """
    rows = conn.execute(
        "SELECT * FROM ontology_versions ORDER BY id DESC LIMIT 50").fetchall()
    out = [dict(r) for r in rows]
    # 2026-10-15（P0-3）：**逐版本**下发"能否作为消费基线 + 原因"（restorable / reason）。
    # 为什么必须由后端给：能否回滚取决于「有没有不可变快照」这个**数据事实**，
    # 若让前端用 status=='released' 自行推断，就会把 2 个 released-but-no-snapshot
    # 的历史版本显示成可回滚 —— 点下去后端静默回落到编辑态，界面提示"成功"而实际没变。
    # 口径与下方 activate 的拒绝逻辑同源（判据：ontology_version_snapshots 是否有行）。
    try:
        _snap_map = {r["version_id"]: r["n"] for r in conn.execute(
            "SELECT version_id, COUNT(*) AS n FROM ontology_version_snapshots "
            "GROUP BY version_id").fetchall()}
    except Exception:
        _snap_map = {}
    for o in out:
        _n = int(_snap_map.get(o["id"], 0) or 0)
        o["snapshot_count"] = _n          # 以实查快照行数为准，不信任可能滞后的计数字段
        if o.get("status") != "released":
            o["restorable"] = False
            o["reason"] = "未发布（草稿版本无快照，本就不可作为消费基线）"
        elif not _n:
            o["restorable"] = False
            o["reason"] = ("无不可变快照（快照治理上线前发布）——回滚到它会静默回落到编辑态、"
                           "实际不生效；请改用「发布当前数据」")
        else:
            o["restorable"] = True
            o["reason"] = f"可作为消费基线（不可变快照 {_n} 类型）"
    # 2026-09-02 P1：released 版本附 diff 摘要（新增/删除/域值域变更）
    for o in out:
        if o.get("status") == "released":
            o["diff"] = _snapshot_diff(conn, o["id"])
    # current：active released 优先 → 最新 released → 旧语义最新行（存量草稿兼容）→ 基线占位
    cur = next((o for o in out if o.get("active")), None) \
        or next((o for o in out if o.get("status") == "released"), None) \
        or (out[0] if out else None) \
        or {"version_label": "v1.0.0", "major": 1, "minor": 0, "patch": 0,
            "status": "released", "released_at": "", "released_by": ""}
    # 未发布变更检测：变更留痕晚于 current 基线发布时刻（从未发布 → 所有留痕均计未发布）
    try:
        released_at = cur.get("released_at") or ""
        if released_at:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM ontology_change_logs WHERE created_at > ?",
                (released_at,)).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) AS n FROM ontology_change_logs").fetchone()
        pending = int(row["n"] or 0) if row else 0
    except Exception:
        pending = 0
    return {"current": cur, "history": out,
            "dirty": pending > 0, "pending": pending,
            # P1-1：门禁档位随列表下发（前端徽标数据源，不另开端点）
            "publish_gate": _publish_gate_level(),
            "publish_gate_desc": _GATE_DESC[_publish_gate_level()]}


@router.get("/api/knowledge/ontology/version/{vid}/snapshot")
def ontology_version_snapshot(vid: int, conn=Depends(db_session)):
    """2026-09-02 P1：查看指定版本的完整快照数据（只读）。

    released 版本返回其不可变快照（ontology_version_snapshots）；未发布（草稿）版本返回
    当前 ontology_types 在该版本写入时刻不可得 → 提示草稿无快照。
    """
    v = conn.execute("SELECT * FROM ontology_versions WHERE id=?", (vid,)).fetchone()
    if not v:
        return JSONResponse({"error": "版本不存在"}, 404)
    if v["status"] != "released":
        return JSONResponse({"error": f"版本 {v['version_label']} 尚未发布，无快照数据（发布后生成不可变快照）"}, 400)
    rows = conn.execute(
        "SELECT type_id AS id, name, type_kind, parent_id, properties, constraints, "
        "description, icon, color FROM ontology_version_snapshots WHERE version_id=? "
        "ORDER BY type_kind, name", (vid,)).fetchall()
    if not rows:
        # 快照治理上线前的存量发布（status=released 但无快照）
        return {"ok": True, "empty": True, "version": dict(v), "types": [],
                "counts": {"entity": 0, "relation": 0, "attribute": 0},
                "note": f"版本 {v['version_label']} 发布于快照治理上线前，无不可变快照数据；"
                        f"可发布当前数据生成新版本（含快照）后查看"}
    types = [dict(r) for r in rows]
    return {"ok": True, "version": dict(v), "types": types,
            "counts": {"entity": sum(1 for t in types if t["type_kind"] == "entity"),
                       "relation": sum(1 for t in types if t["type_kind"] == "relation"),
                       "attribute": sum(1 for t in types if t["type_kind"] == "attribute")}}


@router.post("/api/knowledge/ontology/version/publish")
def ontology_version_publish(body: dict = None, conn=Depends(db_session),
                             user=Depends(require_any_permission(WRITE_PERMS))):
    """2026-09-02 发布语义对齐：主动发布当前编辑数据为新版本（进入版本库，设为消费基线）。

    区别于原 release（把历史日志标记 released）：publish 对当前 ontology_types 生成
    一个全新版本行 + 不可变快照，版本历史即"发布产物列表"。流程：
    ① 一致性检查 high 拦截（防带病发布）
    ② 变更判定（对比上一带快照的已发布版本）：删除/域值域变更 → major（破坏性）；
       新增 → minor；无变更 → 拒绝
    ③ 生成新版本行（change_type=publish）+ 全量快照 + compatible 判定 + active=1
    """
    from datetime import datetime
    # ① 一致性检查（**待发布数据**口径：编辑态即将成为新快照，不能拿旧快照体检 —— 决策 D2 配套）
    #    P1-1：门禁档位化；P1-4：重构为 _gate_run（修复成功路径 chk NameError）+ 附带 CQ 结果
    gate, chk = _gate_run(conn)
    if gate:
        return JSONResponse(gate, 400)

    cur = conn.execute("SELECT id, name, type_kind, parent_id, properties, constraints, "
                       "description, icon, color, iri FROM ontology_types").fetchall()
    cur_names = {(r["name"], r["type_kind"]) for r in cur}
    added, removed, dom_changed = [], [], 0
    constr_changed = 0   # P1-5 配套缺口修复（2026-10-15）：此前 diff 只认增删/dom-range，
    #                     特性（characteristics）/反向（inverse_of）/基数等约束变更无法发布 ——
    #                     R2 摘 symmetric 后发布被「无差异」拒绝，实测暴露。算 minor（兼容）。
    # 2026-09-02 发布说明：body.summary（用户填写，将展示在版本历史/changelog）；未填时自动按 diff 汇总
    body = body or {}
    user_note = str(body.get("summary") or "").strip()
    # 上一带快照的已发布版本（prev 基准；无快照的存量 released 无法对比 → 跳过）
    prev_v = conn.execute(
        "SELECT id, major, minor, patch, version_label FROM ontology_versions "
        "WHERE status='released' AND id IN (SELECT version_id FROM ontology_version_snapshots) "
        "ORDER BY id DESC LIMIT 1").fetchone()
    if prev_v:
        prev_rows = conn.execute(
            "SELECT name, type_kind, constraints FROM ontology_version_snapshots WHERE version_id=?",
            (prev_v["id"],)).fetchall()
        prev_names = {(r["name"], r["type_kind"]) for r in prev_rows}
        removed = sorted({n for n, k in prev_names - cur_names})
        added = sorted({n for n, k in cur_names - prev_names})
        dom_changed = _count_dom_range_changed(prev_rows, cur)
        constr_changed = _count_constraints_changed(prev_rows, cur)
        if not removed and not added and not dom_changed and not constr_changed:
            return JSONResponse({"error": f"当前数据与已发布版本 {prev_v['version_label']} 无差异，"
                                          f"无变更可发布（先编辑本体再发布）"}, 400)
        if removed or dom_changed:
            major, minor, patch = prev_v["major"] + 1, 0, 0
            compatible = 0
        else:
            major, minor, patch = prev_v["major"], prev_v["minor"] + 1, 0
            compatible = 1
    else:
        # 存量 released 均无快照 / 无 released：首个快照版本——版本号承接最新版本行
        #（patch+1，不回退 v1.0.0），内容视为全新基线
        latest = conn.execute(
            "SELECT major, minor, patch FROM ontology_versions ORDER BY id DESC LIMIT 1").fetchone()
        if latest:
            major, minor, patch = latest["major"], latest["minor"], latest["patch"] + 1
        else:
            major, minor, patch = 1, 0, 0
        compatible = 1
    # 发布说明：用户填写优先；空则自动汇总 diff 为可读描述（changelog 实践）
    auto_parts = []
    if added:
        head = "、".join(added[:5]) + ("…" if len(added) > 5 else "")
        auto_parts.append(f"新增 {len(added)} 类型：{head}")
    if removed:
        head = "、".join(removed[:5]) + ("…" if len(removed) > 5 else "")
        auto_parts.append(f"删除 {len(removed)} 类型：{head}")
    if dom_changed:
        auto_parts.append(f"{dom_changed} 个类型定义域/值域变更")
    if constr_changed:
        auto_parts.append(f"{constr_changed} 个类型约束变更（特性/反向/基数）")
    summary = user_note or ("；".join(auto_parts) if auto_parts else f"发布当前数据（{len(cur)} 类型）")
    if user_note:
        summary = f"{user_note}（变更：{'；'.join(auto_parts) if auto_parts else '首次发布'}）"

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    actor = _actor(user)
    label = f"v{major}.{minor}.{patch}"
    dup = conn.execute("SELECT id FROM ontology_versions WHERE version_label=?", (label,)).fetchone()
    if dup:
        major, minor, patch = major, minor, patch + 1  # 避免与既有版本号冲突
        label = f"v{major}.{minor}.{patch}"
    conn.execute(
        "INSERT INTO ontology_versions (version_label, major, minor, patch, change_type, summary, "
        "operator, status, released_at, released_by, snapshot_count, compatible, active, snapshot_created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (label, major, minor, patch, "publish", summary, actor,
         "released", now, actor, 0, compatible, 0, ""))
    vid = conn.execute("SELECT id FROM ontology_versions WHERE version_label=?", (label,)).fetchone()["id"]
    for r in cur:
        conn.execute(
            "INSERT INTO ontology_version_snapshots "
            "(version_id, type_id, name, type_kind, parent_id, properties, constraints, description, icon, color, iri) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (vid, r["id"], r["name"], r["type_kind"], r["parent_id"],
             r["properties"] or "{}", r["constraints"] or "{}",
             r["description"] or "", r["icon"] or "", r["color"] or "#185FA5", r["iri"] or ""))
    conn.execute(
        "UPDATE ontology_versions SET snapshot_count=?, snapshot_created_at=?, active=1 WHERE id=?",
        (len(cur), now, vid))
    conn.execute("UPDATE ontology_versions SET active=0 WHERE id<>? AND status='released'", (vid,))
    conn.commit()
    # 2026-09-14 发布挂钩（L2 迁移链路）：为本批未处理变更留痕 + 历史悬空实例生成迁移计划。
    # 只生成计划（pending），不自动执行——前端发布弹窗展示摘要，dry-run 确认后 apply。
    migration_plan_id = 0
    migration_plan = {"ops": 0, "affected": 0}
    try:
        from services.ontology_migration import build_plan, save_plan
        _plan = build_plan(conn)
        if _plan["ops"]:
            migration_plan_id = save_plan(conn, _plan, version_id=vid, user=user)
            if migration_plan_id:
                migration_plan = {"ops": len(_plan["ops"]), "affected": _plan["affected_total"]}
    except Exception as _e:  # 计划生成失败不阻断发布（迁移可后续手动补建）
        migration_plan = {"ops": 0, "affected": 0, "error": str(_e)}
    audit(actor, "ontology_version_publish",
          f"发布当前数据 → {label}（快照 {len(cur)} 类型，"
          f"{'兼容·自动跟随' if compatible else '⚠️ 破坏性需迁移确认'}）"
          + (f"；迁移计划 #{migration_plan_id}（{migration_plan.get('ops', 0)} op）" if migration_plan_id else "")
          + (f"；⚠️ CQ fail {chk['cq']['failed']}/total {chk['cq']['total']}（不拦截，已留痕可追责）"
             if chk and isinstance(chk.get("cq"), dict) and chk["cq"].get("failed") else ""),
          conn=conn)
    return {"ok": True,
            "version": dict(conn.execute("SELECT * FROM ontology_versions WHERE id=?", (vid,)).fetchone()),
            "snapshot": {"types": len(cur), "compatible": compatible,
                         "check": _check_brief(chk) if chk else {}},
            "diff": {"added": added if prev_v else list(cur_names), "removed": removed if prev_v else [],
                     "dom_changed": dom_changed if prev_v else 0,
                     "constr_changed": constr_changed if prev_v else 0},
            "migration_plan_id": migration_plan_id, "migration_plan": migration_plan}


@router.post("/api/knowledge/ontology/version/activate")
def ontology_version_activate(body: dict = None, conn=Depends(db_session),
                              user=Depends(require_any_permission(WRITE_PERMS))):
    """2026-09-02 P1 回滚：把 active 消费指针切换到指定已发布版本（别名切换，快照不删）。

    行业实践：版本工件不可变，回滚 = active 指向旧版本（消费侧立即读旧快照）。
    """
    body = body or {}
    vid = body.get("version_id")
    if not vid:
        return JSONResponse({"error": "version_id 必填"}, 400)
    row = conn.execute(
        "SELECT * FROM ontology_versions WHERE id=? AND status='released'", (int(vid),)).fetchone()
    if not row:
        return JSONResponse({"error": "仅已发布版本可作为消费基线（先发布）"}, 404)
    # 2026-10-15（P0-3）：**拒绝无快照的版本**。
    # 实测根因：快照治理上线前发布过 2 个 released 版本（v19.0.0 / v19.1.0），
    # 它们 status=released 但 snapshot_count=0 —— 若放行，active 指针切过去后
    # `ontology_semantics.active_rows` 因「active 版本无快照」**静默回落编辑态**，
    # 结果是「回滚提示成功，消费口径却完全没变」：比直接报错更坏 —— 它骗过了人。
    # 故此处显式拒绝，并给出可执行的替代路径（发布当前数据生成带快照的新版本）。
    _snap_n = conn.execute(
        "SELECT COUNT(*) FROM ontology_version_snapshots WHERE version_id=?", (row["id"],)
    ).fetchone()[0]
    if not _snap_n:
        return JSONResponse({
            "error": f"版本 {row['version_label']} 无不可变快照（快照治理上线前发布），"
                      f"不能作为消费基线——回滚到它会静默回落到编辑态、实际不生效。"
                      f"请改用「发布当前数据」生成带快照的新版本",
            "no_snapshot": True, "version_id": int(row["id"]),
        }, 400)
    if row["active"]:
        return {"ok": True, "version": dict(row), "already_active": True}
    conn.execute("UPDATE ontology_versions SET active=0 WHERE status='released'")
    conn.execute("UPDATE ontology_versions SET active=1 WHERE id=?", (row["id"],))
    conn.commit()
    audit(_actor(user), "ontology_version_activate",
          f"消费基线切换（回滚）→ {row['version_label']}", conn=conn)
    return {"ok": True, "version": dict(conn.execute(
        "SELECT * FROM ontology_versions WHERE id=?", (row["id"],)).fetchone()),
        "already_active": False}


@router.post("/api/knowledge/ontology/version/release")
def ontology_version_release(body: dict = None, conn=Depends(db_session),
                             user=Depends(require_any_permission(WRITE_PERMS))):
    """发布本体版本（2026-09-02 升级为快照治理）：指定版本（缺省=当前最新）→ released 稳定基线。

    P0 快照机制（D1-D3 采纳）：
    ① 一致性检查：high 级问题拦截发布（防带病发布）
    ② 全量快照 ontology_types → ontology_version_snapshots（版本工件不可变，只写不改）
    ③ 兼容性判定：对比上一已发布快照——有删除/改名/域值域变更 → compatible=0（major 需迁移），否则 1（自动跟随）
    ④ active 指针切换到本版本（消费侧以 active 为准）；旧 active 清零
    幂等：已发布版本重复发布直接返回当前状态。
    """
    from datetime import datetime
    body = body or {}
    vid = body.get("version_id")
    if vid is not None:
        row = conn.execute("SELECT * FROM ontology_versions WHERE id=?", (int(vid),)).fetchone()
        if not row:
            return JSONResponse({"error": "版本不存在"}, 404)
    else:
        row = conn.execute("SELECT * FROM ontology_versions ORDER BY id DESC LIMIT 1").fetchone()
        if not row:
            return JSONResponse({"error": "暂无本体版本可发布（提交本体变更后自动生成版本）"}, 400)
    if row["status"] == "released":
        return {"ok": True, "version": dict(row), "already_released": True}

    # ① 一致性检查（D1 采纳）——**待发布数据**口径（决策 D2 配套，同 publish）
    #    P1-1：门禁档位化；P1-4：重构为 _gate_run（修复成功路径 chk NameError）+ 附带 CQ 结果
    gate, chk = _gate_run(conn)
    if gate:
        return JSONResponse(gate, 400)

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    actor = _actor(user)
    # ② 全量快照（事务）
    src = conn.execute("SELECT id, name, type_kind, parent_id, properties, constraints, description, icon, color "
                       "FROM ontology_types").fetchall()
    for r in src:
        conn.execute(
            "INSERT OR IGNORE INTO ontology_version_snapshots "
            "(version_id, type_id, name, type_kind, parent_id, properties, constraints, description, icon, color) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (row["id"], r["id"], r["name"], r["type_kind"], r["parent_id"],
             r["properties"] or "{}", r["constraints"] or "{}",
             r["description"] or "", r["icon"] or "", r["color"] or "#185FA5"))
    # ③ 兼容性判定：对比上一已发布快照
    prev = conn.execute(
        "SELECT version_id, name, type_kind FROM ontology_version_snapshots "
        "WHERE version_id IN (SELECT id FROM ontology_versions WHERE status='released')").fetchall()
    compatible = 1
    if prev:
        prev_names = {(p["name"], p["type_kind"]) for p in prev}
        cur_names = {(r["name"], r["type_kind"]) for r in src}
        removed = prev_names - cur_names                       # 删除 = 破坏性
        renamed = cur_names & prev_names                       # 重名且 type_kind 相同视为同名（改名=旧名消失+新名出现，已含在 removed/added）
        # 改名检测：新增名 且 删除名 数量对不上 → 保守判不兼容（删除已覆盖）；域值域变更单独判
        dom_changed = _snapshot_dom_range_diff(conn, prev, src)
        if removed or dom_changed:
            compatible = 0
    conn.execute(
        "UPDATE ontology_versions SET status='released', released_at=?, released_by=?, "
        "snapshot_count=?, compatible=?, snapshot_created_at=?, active=1 WHERE id=?",
        (now, actor, len(src), compatible, now, row["id"]))
    # ④ active 指针：其他已发布版本清零（回滚=切 active，快照保留）
    conn.execute("UPDATE ontology_versions SET active=0 WHERE id<>? AND status='released'", (row["id"],))
    conn.commit()
    audit(actor, "ontology_version_release",
          f"本体版本发布: {row['version_label']}（{row['change_type']}）快照 {len(src)} 类型"
          f"{'· 兼容（自动跟随）' if compatible else '· ⚠️ 破坏性变更（需迁移确认）'}", conn=conn)
    return {"ok": True,
            "version": dict(conn.execute("SELECT * FROM ontology_versions WHERE id=?", (row["id"],)).fetchone()),
            "snapshot": {"types": len(src), "compatible": compatible,
                         "check": _check_brief(chk) if chk else {}},
            "already_released": False}


@router.get("/api/knowledge/ontology/graph")
def ontology_graph(conn=Depends(db_session)):
    """KB-P4：本体图谱视图（类型层：EntityType 节点 + RelationType 约束边）。消费侧读已发布快照。"""
    from ontology_semantics import OntologyValidator
    return OntologyValidator(conn, rows=_active_ont_rows(conn)).to_graph()


# ── 2026-09-14 本体变更 → 实例迁移计划端点（L2 链路，见 docs/本体变更实例影响分析与自动迁移方案.md）──

@router.get("/api/knowledge/ontology/migrations")
def ontology_migrations_list(conn=Depends(db_session), user=Depends(current_user)):
    """迁移计划列表（按 plan 倒序；含 pending 统计，供版本历史/治理面板消费）。"""
    from services.ontology_migration import list_plans
    return list_plans(conn)


@router.post("/api/knowledge/ontology/migrations/build")
def ontology_migrations_build(body: dict = None, conn=Depends(db_session),
                              user=Depends(require_any_permission(WRITE_PERMS))):
    """手动补建迁移计划（历史悬空实例补迁 / 发布挂钩失败后重试）。幂等：同留痕同 op 不重复。

    body.target_prefix 可选：只处理目标名以该前缀开头的 op（verify 脚本隔离 / 按类型名族补迁）。
    """
    from services.ontology_migration import build_plan, save_plan
    body = body or {}
    plan = build_plan(conn, target_prefix=str(body.get("target_prefix") or ""))
    if not plan["ops"]:
        return {"ok": True, "plan_id": 0, "ops": 0, "affected": 0,
                "note": "无待迁移实例影响（无未处理留痕、无悬空实例）"}
    plan_id = save_plan(conn, plan, version_id=0, user=user)
    if not plan_id:
        return {"ok": True, "plan_id": 0, "ops": 0, "affected": 0,
                "note": "检出的 op 均已存在于既有计划（幂等去重，未重复生成）"}
    audit(_actor(user), "ontology_migration_build",
          f"手动生成实例迁移计划 #{plan_id}（{len(plan['ops'])} op / 影响 {plan['affected_total']} 条）", conn=conn)
    return {"ok": True, "plan_id": plan_id, "ops": len(plan["ops"]), "affected": plan["affected_total"]}


@router.post("/api/knowledge/ontology/migrations/{plan_id}/dry-run")
def ontology_migrations_dry_run(plan_id: int, conn=Depends(db_session),
                                user=Depends(require_any_permission(WRITE_PERMS))):
    """迁移计划预演：逐 op 重算预计行数 + 抽样 ≤10，不写任何业务表。"""
    from services.ontology_migration import dry_run
    r = dry_run(conn, plan_id)
    if not r.get("ok"):
        return JSONResponse(r, 400)
    audit(_actor(user), "ontology_migration_dry_run",
          f"迁移计划 #{plan_id} 预演：{r['ops']} op / 影响 {r['affected_total']} 条", conn=conn)
    return r


@router.post("/api/knowledge/ontology/migrations/{plan_id}/apply")
def ontology_migrations_apply(plan_id: int, conn=Depends(db_session),
                              user=Depends(require_any_permission(WRITE_PERMS))):
    """确认执行迁移计划：单事务逐 op 回写实例数据；失败整体回滚并标记 failed。"""
    from services.ontology_migration import apply as apply_plan
    r = apply_plan(conn, plan_id, user=user)
    if not r.get("ok"):
        return JSONResponse(r, 400)
    n = len(r.get("applied") or [])
    audit(_actor(user), "ontology_migration_apply",
          f"迁移计划 #{plan_id} 已执行：{n} op（" + "；".join(
            f"{a['op']}:{a['target']} {a['note']}" for a in (r.get("applied") or [])[:8]) + "）", conn=conn)
    return r


@router.get("/api/knowledge/ontology/schema")
def ontology_schema(conn=Depends(db_session)):
    """KB-P4：本体 Schema 文本（供 Agent 语义层注入 system prompt）。消费侧读已发布快照。"""
    from ontology_semantics import OntologyValidator
    return {"schema": OntologyValidator(conn, rows=_active_ont_rows(conn)).schema_text()}


@router.get("/api/knowledge/ontology/shacl")
def ontology_shacl(type_id: Optional[int] = None, conn=Depends(db_session)):
    """P0-3：本体约束 → SHACL 形状（Turtle，W3C 标准，PySHACL/Jena 兼容，含子类继承）。

    2026-09-02 四轮调整：type_id 可选——节点级视图只输出所选类型及其直接语境的形状。
    """
    from ontology_semantics import OntologyValidator
    from fastapi.responses import Response
    rows = _active_ont_rows(conn)
    if type_id:
        rows = _filter_ont_rows_for_type(rows, type_id)
    return Response(OntologyValidator(conn, rows=rows).shacl_export(), media_type="text/turtle")


@router.get("/api/knowledge/ontology/binding")
def ontology_binding(branch: str = "", conn=Depends(db_session)):
    """A6 数据绑定视图（对齐 Playground Data Binding）：每个实体类型的实例数/来源/状态分布。
    默认统计「全分支 · 非废弃实例」合计（本体为全局共享 Schema）；传入具体 branch 时仅统计该分支。"""
    fil, params = "", []
    if branch and branch not in ("all", "global"):
        fil = " AND e.branch=?"
        params = [branch]
    rows = conn.execute("""
        SELECT e.entity_type, COUNT(*) AS total,
               SUM(CASE WHEN e.status='reviewed' THEN 1 ELSE 0 END) AS reviewed,
               SUM(CASE WHEN e.status='candidate' THEN 1 ELSE 0 END) AS candidate,
               COUNT(DISTINCT e.source_doc) AS source_docs,
               COUNT(DISTINCT e.branch) AS branches
        FROM entities e WHERE e.status!='deprecated'""" + fil + " GROUP BY e.entity_type", params).fetchall()
    types = conn.execute("SELECT name FROM ontology_types WHERE type_kind='entity'").fetchall()
    stat = {r["entity_type"]: dict(r) for r in rows}
    out = []
    for t in types:
        tn = t["name"]
        s = stat.get(tn) or {"entity_type": tn, "total": 0, "reviewed": 0, "candidate": 0,
                             "source_docs": 0, "branches": 0}
        out.append({"entity_type": tn, "scope": branch if branch and branch not in ("all", "global") else "all_branches", **s})
    return {"bindings": out, "total_entities": sum(o["total"] for o in out),
            "ontology_type_count": len(out)}


@router.get("/api/knowledge/ontology/export")
def ontology_export(fmt: str = "owl", type_id: Optional[int] = None, conn=Depends(db_session)):
    """O-2：导出本体为 W3C 标准序列化。
    fmt=owl → OWL/RDF-XML（供 Protégé 使用）
    fmt=ttl → Turtle（RDF 三元组文本语法，W3C 标准）
    2026-09-02 四轮调整：type_id 可选——节点级 OWL 视图只导出所选类型及其直接语境。
    """
    from ontology_owl import to_owl, to_turtle
    from fastapi.responses import Response
    rows = _active_ont_rows(conn)   # 2026-09-02 消费侧以已发布快照为准
    if type_id:
        rows = _filter_ont_rows_for_type(rows, type_id)
    if fmt == "ttl":
        text, warns = to_turtle(conn, rows=rows)
    else:
        xml, warns = to_owl(conn, rows=rows)
        text = xml
    # 方案 A3（2026-08-29）：导出完整性警告随响应头返回（X-Ont-Warnings），前端可 toast 提示
    headers = {"Content-Disposition": f"attachment; filename=ontology.{'ttl' if fmt == 'ttl' else 'owl'}"}
    if warns:
        # HTTP 头仅允许 latin-1：中文警告须 URL 编码（同 doc_download Content-Disposition 教训）
        from urllib.parse import quote as _q
        headers["X-Ont-Warnings"] = _q(f"{len(warns)}; " + " | ".join(warns[:3])[:400])
    return Response(text, media_type="text/turtle" if fmt == "ttl" else "application/rdf+xml",
                    headers=headers)


@router.post("/api/knowledge/ontology/import")
def ontology_import(body: dict, conn=Depends(db_session), user=Depends(current_user)):
    """O-2：导入本体（OWL/RDF-XML 或 Turtle），幂等：同名类型跳过。

    body.fmt: owl (默认) | ttl
    body.owl / body.content / body.ttl: 导入文本
    """
    from ontology_owl import from_owl, from_turtle
    fmt = (body.get("fmt") or "owl").lower()
    raw = body.get("content") or body.get("owl") or body.get("ttl") or ""
    if not raw:
        return JSONResponse({"error": "导入内容不能为空"}, 400)
    if fmt == "ttl":
        result = from_turtle(raw, conn)
    else:
        result = from_owl(raw, conn)
    # 本体版本：OWL 导入新增类型 = minor 递增（仅实际新增才递增）
    # 方案 D：导入后自动一致性校验，结果随响应返回
    try:
        vrows = conn.execute("SELECT id, name, parent_id FROM ontology_types").fetchall()
        vby = {str(r["id"]): r for r in vrows}
        result["warnings"] = [f"悬空父类：{r['name']}（parent {r['parent_id']} 不存在）"
                              for r in vrows if r["parent_id"] and str(r["parent_id"]) not in vby]
    except Exception:
        pass
    # 2026-09-08 版本语义收敛：OWL 导入属编辑态，只留 audit/变更记录，不升版本号（发布时统一升级）
    audit(_actor(user), "ontology_import", f"OWL 导入: 新增 {result['imported']} / 跳过 {result['skipped']}", conn=conn)
    return result
