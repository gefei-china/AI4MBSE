# -*- coding: utf-8 -*-
"""CQ（能力问题 Competency Questions）引擎 —— P1-4（2026-10-15）。

定位（对齐行业分层，见 docs/本体模型管理模块-…-20261014.md §4.2 共识 3）：
  · verification（机械查结构）= 体检 17 条规则（core/ontology_rules 单一真源）；
  · validation（回答业务问题）= 本文件 —— 「这个本体是否建对了域」的唯一防线，
    业界用 SPARQL ASK 实现（TopBraid/Protégé 生态）。

为什么 kind 有 'sql' 和 'sparql' 两档：
  · sql（SQLite）—— 数据真源在 entities/relations；发布门禁**不依赖** Fuseki 在线。
  · sparql（Fuseki SPARQL）—— P1-2（2026-11-21）通电。Fuseki 图谱是投影，不可达时
    相关 CQ 一律 status='skip'（**降级不是通过**，同现有纪律），绝不静默回落到 sql 档 ——
    那等于换了一套数据源却报同一个题名，评审方无法察觉口径变了。
  · 两种档位显式分立：登记什么 kind 就执行什么 kind，不做“sparql 失败自动回落 sql”
    的隐式兑底（兑底会把“图库断了”伪装成“图查询通过”。如需兑底，显式登记另一条 sql CQ）。

纪律（本仓库实测教训，勿删）：
  · 只读白名单：仅放行单条 SELECT/WITH…SELECT；拒 PRAGMA/ATTACH/写操作/多语句
    —— CQ 是用户可登记的输入，等价于一个受控查询面（防注入）；
  · 执行异常 → status='skip' 并附原因（静默少查=假绿，同 _ontology_check 的 degraded）；
  · CQ fail ≠ 拦截发布（R1 拍板：validation≠verification，红可能是有意未完成），
    只随门禁 payload / 体检报告下发 + 落 ontology_cq_logs 留痕可追责；
  · record=False 的探针调用（验证脚本）不落日志（同 /validate record=0 的 P0-2 教训）。
"""
from __future__ import annotations

import re

# 行数上限：CQ 是「有没有」问题，不是导出 —— 截断防大结果集打爆回包
MAX_ROWS = 200

# SPARQL 档默认端点 —— 单一真源在 core.fuseki_runtime（2026-11-22 收敛）。
# 本别名只为兼容既有调用/门禁而保留；值不再在此另写一份。
from core.fuseki_runtime import DEFAULT_ENDPOINT as DEFAULT_SPARQL_ENDPOINT  # noqa: E402
from core import fuseki_runtime as _fuseki_rt  # noqa: E402

# 只读白名单之外一律拒绝（词边界匹配，防 SELECT 里夹写操作）
_FORBIDDEN = {
    "insert", "update", "delete", "drop", "alter", "create", "replace",
    "vacuum", "reindex", "analyze", "pragma", "attach", "detach",
}

# SPARQL 档白名单：只放查询动词；SPARQL Update 的写动词全部拒（含 LOAD/CLEAR
# 这类不叫 update 但会改图的）
_SPARQL_FORBIDDEN = {
    "insert", "delete", "drop", "clear", "load", "create", "move", "copy",
    "add", "construct"  # construct 本身只读，但返回图而非布尔/绑定，CQ 判定用不到，先拒
}
_SPARQL_OK_HEADS = ("select", "ask")

SEEDS = [
    # 追踪链三大关系的端点完整性（真库预演：SATISFIES 54 / TRACE 6 / VERIFIED_BY 45 边，悬空 0）
    ("SATISFIES 满足关系的两端实体是否都存在（无悬空满足边）",
     """SELECT r.id, r.source_id, r.target_id FROM relations r WHERE r.relation_type='SATISFIES' AND (
 NOT EXISTS(SELECT 1 FROM entities e WHERE e.id=r.source_id AND e.branch=r.branch AND e.status!='deprecated') OR
 NOT EXISTS(SELECT 1 FROM entities e WHERE e.id=r.target_id AND e.branch=r.branch AND e.status!='deprecated'))""",
     "empty"),
    ("TRACE 追溯关系的两端实体是否都存在（无悬空追溯边）",
     """SELECT r.id, r.source_id, r.target_id FROM relations r WHERE r.relation_type='TRACE' AND (
 NOT EXISTS(SELECT 1 FROM entities e WHERE e.id=r.source_id AND e.branch=r.branch AND e.status!='deprecated') OR
 NOT EXISTS(SELECT 1 FROM entities e WHERE e.id=r.target_id AND e.branch=r.branch AND e.status!='deprecated'))""",
     "empty"),
    ("VERIFIED_BY 验证关系的两端实体是否都存在（无悬空验证边）",
     """SELECT r.id, r.source_id, r.target_id FROM relations r WHERE r.relation_type='VERIFIED_BY' AND (
 NOT EXISTS(SELECT 1 FROM entities e WHERE e.id=r.source_id AND e.branch=r.branch AND e.status!='deprecated') OR
 NOT EXISTS(SELECT 1 FROM entities e WHERE e.id=r.target_id AND e.branch=r.branch AND e.status!='deprecated'))""",
     "empty"),
    ("存量关系的关系类型是否全部在本体注册（无未注册关系类型）",
     "SELECT DISTINCT relation_type FROM relations "
     "WHERE relation_type NOT IN (SELECT name FROM ontology_types)",
     "empty"),
    ("存量实体的实体类型是否全部在本体注册（无未注册实体类型）",
     "SELECT DISTINCT entity_type FROM entities WHERE status!='deprecated' "
     "AND entity_type NOT IN (SELECT name FROM ontology_types WHERE type_kind='entity')",
     "empty"),
    ("需求满足/追溯/验证三类追踪关系是否有数据（追踪链已建立）",
     "SELECT COUNT(*) AS n FROM relations "
     "WHERE relation_type IN ('SATISFIES','TRACE','VERIFIED_BY')",
     "nonempty"),
    # ── 2026-11-24 第四期：MBSE 域能力问题（对齐文章 Competency Questions）──
    # 口径：查真源（SQLite），问法换成系统工程——需求被哪些功能/部件满足、
    # 部件是否被验证、追踪链是否双向可达。全部为「有没有」问题（empty/nonempty）。
    ("系统需求是否有对应的满足关系（需求不被孤立）",
     "SELECT DISTINCT e.id, e.name FROM entities e WHERE e.entity_type LIKE '%需求%' "
     "AND NOT EXISTS(SELECT 1 FROM relations r WHERE r.relation_type='SATISFIES' "
     "AND r.target_id=e.id)", "empty"),
    ("被满足的需求是否都有验证关系（满足-验证闭环）",
     "SELECT DISTINCT r.target_id FROM relations r WHERE r.relation_type='SATISFIES' "
     "AND NOT EXISTS(SELECT 1 FROM relations v WHERE v.relation_type='VERIFIED_BY' "
     "AND v.source_id=r.target_id)", "empty"),
    ("需求与部件之间的追踪链是否已建立（SATISFIES 有数据）",
     "SELECT COUNT(*) AS n FROM relations WHERE relation_type='SATISFIES'", "nonempty"),
    ("验证关系的两端是否都可解析到实体（验证链无悬空）",
     "SELECT r.id FROM relations r WHERE r.relation_type='VERIFIED_BY' AND ("
     "NOT EXISTS(SELECT 1 FROM entities e WHERE e.id=r.source_id) OR "
     "NOT EXISTS(SELECT 1 FROM entities e WHERE e.id=r.target_id))", "empty"),
    # ── P1-2（2026-11-21）SPARQL 档种子（kind='sparql'，走 Fuseki）──
    # 图库是 SQLite 的投影；这两条回答「投影是否建起来了、追踪链是否投过去了」
    # 与上面 sql 档不重复（口径不同：sql 档查真源，sparql 档查消费面）。
    # Fuseki 不可达 ⇒ skip（降级不是通过），发布门禁不因此拦截。
    # ⚠️ 放在 SPARQL_SEEDS 单独列表，不能混进 SEEDS —— ensure_seed 对 SEEDS 写死
    #    kind='sql'，混进去的话这两条会被登记成 sql 档然后在 SQLite 里报语法错。
    # Fuseki 不可达 ⇒ skip（降级不是通过），发布门禁不因此拦截。
]

#: SPARQL 档种子（kind='sparql'，ensure_seed 单独登记；见上 ⚠️）
SPARQL_SEEDS = [
    ("图库投影非空（Fuseki SPARQL 消费面已建立）",
     "SELECT ?s WHERE { ?s ?p ?o } LIMIT 1",
     "nonempty"),
    ("追踪链已投影到图库（SATISFIES 在 SPARQL 消费面可查）",
     "PREFIX o: <http://mbse/ontology/>\nSELECT ?s WHERE { ?s o:SATISFIES ?o } LIMIT 1",
     "nonempty"),
]


def validate_query(query: str) -> tuple[str, str]:
    """只读白名单校验。返回 (ok, err)。防注入的**判据**（不是装饰）：
    ① 去注释后单条语句；② 以 SELECT/WITH 开头；③ 无写操作/PRAGMA/ATTACH 词；
    ④ 无分号（拒多语句；分号在字符串字面量里也拒 —— 收紧面换安全，CQ 用不到）。"""
    q = re.sub(r"--[^\n]*", "", str(query or ""))
    q = q.strip()
    if not q:
        return False, "查询为空"
    if ";" in q:
        return False, "不允许分号（仅单条查询）"
    if not re.match(r"^(select|with)\b", q, re.I):
        return False, "仅允许 SELECT / WITH…SELECT 只读查询"
    for w in _FORBIDDEN:
        if re.search(r"\b" + w + r"\b", q, re.I):
            return False, f"只读白名单：不允许 {w.upper()}"
    return True, ""


def validate_sparql(query: str) -> tuple[str, str]:
    """SPARQL 档只读白名单。返回 (ok, err)。

    判据：① 去 `#` 注释后单条语句；② 允许 `PREFIX`/`BASE` 声明前导（标准语法，
    首版漏了它 ⇒ 带 PREFIX 的合法 CQ 全被拒、降级成 skip —— 真机抓到）；
    ③ 主句以 SELECT/ASK 开头（CQ 是布尔/绑定问题，不收 CONSTRUCT/DESCRIBE ——
    返回图没法判 empty/nonempty）；④ 无 SPARQL Update 写动词（含 LOAD/CLEAR
    这类不叫 update 但会改图的）；⑤ 无分号（拒多语句）。
    """
    q = re.sub(r"#[^\n]*", "", str(query or ""))
    q = q.strip()
    if not q:
        return False, "查询为空"
    if ";" in q:
        return False, "不允许分号（仅单条查询）"
    # 剥掉开头的 PREFIX/BASE 声明（可多行），再看主句动词
    lines = [ln for ln in q.splitlines()]
    while lines and re.match(r"^\s*(prefix|base)\b", lines[0], re.I):
        lines.pop(0)
    main = "\n".join(lines).strip()
    if not main:
        return False, "除 PREFIX/BASE 声明外没有查询主体"
    if not re.match(r"^(select|ask)\b", main, re.I):
        return False, "仅允许 SELECT / ASK 查询（CQ 判有无，不收 CONSTRUCT/DESCRIBE）"
    for w in _SPARQL_FORBIDDEN:
        if re.search(r"\b" + w + r"\b", q, re.I):
            return False, f"只读白名单：不允许 {w.upper()}"
    return True, ""


def _sparql_endpoint() -> str:
    """SPARQL 档端点：统一走 core.fuseki_runtime，杜绝与启动参数各写一份。"""
    return _fuseki_rt.resolve_endpoint()


def _strict_freshness() -> bool:
    """CQ 严格档：镜像陈旧 / 新鲜度未验成时，不把 SPARQL CQ 结论当 pass。

    与 A9 同型：**验的不是最新版本 / 根本没验成**，都不能当作通过。
    """
    try:
        from core.config import get as _cfg_get
        return bool(_cfg_get("graph_db", "cq_strict_freshness"))
    except Exception:
        return False  # 配置不可读 ⇒ 保守维持历史口径，不让配置故障改判业务结论


def _run_sparql_cq(query: str, expected: str, freshness: dict | None = None) -> dict:
    """执行一条 SPARQL CQ。Fuseki 不可达 ⇒ skip（**降级不是通过**）。

    P0-0 第八批：结果附 `freshness` 状态。真机实测过"镜像只剩 40% 数据而 CQ 仍 pass"
    （非空判据掩盖大范围投影缺失）⇒ 评审方必须能看见这条结论是在新鲜还是陈旧镜像上得出。

    P0-0 第九批②：严格档开启时，stale / unknown ⇒ skip。注意 **`freshness is None`
    表示"未判新鲜度"**（旧调用方/门禁直接调用），此时严格档不介入 —— 否则会把
    "没做这项检查"误当成"检查发现不新鲜"，历史判据全被改判。
    """
    fs = (freshness or {}).get("status", "unknown")
    item = {"row_count": 0, "status": "skip", "message": "", "freshness": fs}
    if freshness is not None and _strict_freshness() and fs in ("stale", "unknown"):
        item["message"] = (
            "Fuseki 镜像%s —— 本条 CQ 的结论不可信，严格档降级 skip（降级不是通过）" % fs)
        return item
    try:
        from services.sparql_sync import FusekiClient
        ep = _sparql_endpoint()
        base, _, ds = ep.rstrip("/").rpartition("/")
        cli = FusekiClient(base_url=base or ep, dataset=ds or "mbse")
        if not cli.ping(timeout=4.0):
            item["message"] = f"Fuseki 不可达（{ep}），SPARQL 档降级 skip —— 降级不是通过"
            return item
        res = cli.query(query, timeout=20.0)
        err = res.get("error") if isinstance(res, dict) else None
        if err:
            item["message"] = f"SPARQL 执行失败：{str(err)[:200]}"
            return item
        # ⚠️ 判 ASK/SELECT 必须**剥掉 PREFIX/BASE 前导**再取首个动词。
        #   首版写成 `query.split(None,1)[0]` ⇒ 对 `PREFIX o: <…>\nASK {…}` 取到的是
        #   `prefix`，于是 ASK 结果被当成 SELECT 处理：`results.bindings` 为空 ⇒
        #   判成"返回 0 行" ⇒ 该 CQ 恒 fail。真机在 id=47 上抓到
        #   （与校验侧漏放 PREFIX 是同一族错误：**两处各写一份"取首个动词"**）。
        _no_comment = re.sub(r"#[^\n]*", "", query or "")
        _lines = [ln for ln in _no_comment.splitlines()]
        while _lines and re.match(r"^\s*(prefix|base)\b", _lines[0], re.I):
            _lines.pop(0)
        head = (_lines[0].strip().split(None, 1)[0].lower() if _lines and _lines[0].strip() else "")
        if head == "ask":
            b = res.get("boolean")
            item["row_count"] = 1 if b else 0
            green = (b is False) if expected == "empty" else (b is True)
            item["message"] = (f"ASK={b}，期望{'查不到' if expected == 'empty' else '查得到'}"
                               + ("" if green else " —— 不符合"))
            item["status"] = "pass" if green else "fail"
            return item
        rows = (res.get("results") or {}).get("bindings") or []
        n = min(len(rows), MAX_ROWS)
        item["row_count"] = n
        if len(rows) > MAX_ROWS:
            item["message"] = f"结果超过 {MAX_ROWS} 行已截断（只判有无，不导出）"
        green = (n == 0) if expected == "empty" else (n > 0)
        item["status"] = "pass" if green else "fail"
        item["message"] = item["message"] or (
            f"返回 {n} 行，期望{'为空' if expected == 'empty' else '非空'}"
            + ("" if green else " —— 不符合"))
        return item
    except Exception as e:
        item["message"] = f"SPARQL 执行异常：{type(e).__name__}: {str(e)[:160]}"
        return item


def ensure_seed(conn) -> None:
    """内置种子 CQ（表空才补，幂等）。用户改/删种子后**不会**被重新塞回
    （只看表空 —— 用户的编辑是意图，不是待修复项）。"""
    n = conn.execute("SELECT COUNT(*) FROM ontology_questions").fetchone()[0]
    if n:
        return
    for q, sql, exp in SEEDS:
        conn.execute(
            "INSERT INTO ontology_questions (question, kind, query, expected, enabled, source, created_by) "
            "VALUES (?,?,?,?,1,'seed','system')", (q, "sql", sql, exp))
    # P1-2（2026-11-21）：SPARQL 档种子单独登记（kind='sparql'）。
    # 不混进 SEEDS 的原因见 SPARQL_SEEDS 定义处 —— 混进去会被上一行写死成 sql 档。
    for q, qy, exp in SPARQL_SEEDS:
        conn.execute(
            "INSERT INTO ontology_questions (question, kind, query, expected, enabled, source, created_by) "
            "VALUES (?,?,?,?,1,'seed','system')", (q, "sparql", qy, exp))
    conn.commit()


def run_cqs(conn, source: str = "", record: bool = True, qid: int | None = None) -> list[dict]:
    """执行 CQ（全部 enabled，或指定 qid）。返回 [{id, question, expected, status, row_count, message}]。

    status：pass=符合期望 / fail=不符合期望（业务问题答「不」）/ skip=执行不了（SQL 非法、
    查询报错等）—— skip 是降级不是通过，调用方必须把 skipped 展示出来。
    record=True 时逐题落 ontology_cq_logs（失败只登记不抛，同 _record_health_log 纪律）。
    """
    if qid is not None:
        rows = conn.execute("SELECT * FROM ontology_questions WHERE id=?", (qid,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM ontology_questions WHERE enabled=1 ORDER BY id").fetchall()
    # P0-0 第八批：一次 run 只判一次镜像新鲜度（不是每条 CQ 都重建 TTL）。
    # 端点不通时直接标 unknown，不去打 N 次会超时的查询。
    sp_fresh = None
    if any((r["kind"] or "sql").strip() == "sparql" for r in rows):
        try:
            # ping() 要的是 **base URL**（内部拼 /$/ping）；误传完整端点会得到
            # /mbse/$/ping ⇒ 404 ⇒ 恒 unknown（真机 2026-11-22 抓到）。
            _base, _ds = _fuseki_rt.endpoint_parts(_fuseki_rt.resolve_endpoint())
            if _fuseki_rt.ping(_base):
                from core.fuseki_freshness import check_freshness
                sp_fresh = check_freshness(conn)
            else:
                sp_fresh = {"status": "unknown", "reason": "Fuseki 不可达"}
        except Exception:
            sp_fresh = {"status": "unknown", "reason": "新鲜度判据异常"}
    out = []
    for r in rows:
        # 2026-11-24：结果带 source —— 区分「种子 CQ」（结构性，应全绿）
        # 与「手工登记 CQ」（业务完整性，fail 属正常留痕）。消费方据此分别判定，
        # 避免业务缺口把结构性门禁染红（同族教训：指标口径必须可区分，否则必混算）。
        item = {"id": r["id"], "question": r["question"], "expected": r["expected"],
                "kind": r["kind"], "source": (r["source"] if "source" in r.keys() else ""),
                "status": "skip", "row_count": 0, "message": ""}
        kind = (r["kind"] or "sql").strip()
        if kind == "sql":
            ok, err = validate_query(r["query"])
            if not ok:
                item["message"] = f"查询未通过只读校验：{err}"
            else:
                try:
                    cur = conn.execute(r["query"])
                    data = cur.fetchmany(MAX_ROWS + 1)
                    n = len(data[:MAX_ROWS])
                    item["row_count"] = n
                    if len(data) > MAX_ROWS:
                        item["message"] = f"结果超过 {MAX_ROWS} 行已截断（只判有无，不导出）"
                    green = (n == 0) if r["expected"] == "empty" else (n > 0)
                    item["status"] = "pass" if green else "fail"
                    item["message"] = item["message"] or (
                        f"返回 {n} 行，期望{'为空' if r['expected'] == 'empty' else '非空'}"
                        + ("" if green else " —— 不符合"))
                except Exception as e:
                    item["message"] = f"执行失败：{type(e).__name__}: {e}"
        elif kind == "sparql":
            # P1-2（2026-11-21）：SPARQL 档走 Fuseki。两道关：先过 SPARQL 只读白名单，
            # 再执行；Fuseki 不可达 ⇒ skip（降级不是通过），**绝不静默回落 sql 档**。
            ok, err = validate_sparql(r["query"])
            if not ok:
                item["message"] = f"SPARQL 未通过只读校验：{err}"
            else:
                item.update(_run_sparql_cq(r["query"], r["expected"], freshness=sp_fresh))
        else:
            item["message"] = f"未知 kind: {kind}"
        out.append(item)
        if record:
            try:
                conn.execute(
                    "INSERT INTO ontology_cq_logs (question_id, source, status, row_count, message) "
                    "VALUES (?,?,?,?,?)",
                    (r["id"], source, item["status"], item["row_count"], item["message"] or ""))
                conn.commit()
            except Exception as _e:      # 日志写不进不影响结论（同 _record_health_log）
                try:
                    import logging
                    logging.getLogger(__name__).warning(
                        "[ontology_cq] 执行历史落库失败（不影响本次结论）: %s", _e)
                except Exception:
                    pass
    return out


def summarize(results: list[dict]) -> dict:
    """结果 → 摘要（随门禁 payload / 体检报告下发的紧凑形态）。"""
    return {"total": len(results),
            "failed": sum(1 for x in results if x["status"] == "fail"),
            "skipped": sum(1 for x in results if x["status"] == "skip"),
            # P0-0 第八批：镜像陈旧是独立维度，不并入 failed/skipped（零回归）
            "stale": sum(1 for x in results if x.get("freshness") == "stale"),
            "results": results}
