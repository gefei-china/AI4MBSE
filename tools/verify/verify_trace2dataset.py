# -*- coding: utf-8 -*-
"""P1-1 门禁：Trace2Dataset（tools/eval/trace2dataset.py + build_evalset.py 接线）。

**背景（2026-10-18《自进化 AI 平台调研评估》路线图 P1-1）**：AgentLoop 闭环
「Trace2Dataset：生产轨迹 → 评测集 → 优化 → 再部署」的供料环。本系统评估环最强
（189 门禁 + tools/eval 19 件）但只评估人的改动与检索语料；生产轨迹（会话/HIL
裁决/意图样本/编排运行/P0-3 台账）此前没有通路变成可复用评测样本。本门禁守护：

  A. 纯函数与注册表：8 个抽取器的统一输出列/标签口径、窗口过滤、Mock 过滤、
     截断、JSON 元数据兜底；
  B. 配置消费：trace2dataset 组七键双登记 + 变异自证（窗口/ Mock/ 上限/ 短query
     四个口径都必须真被消费，不被常量旁路）；
  C. 干净库端到端：强标注（intent/HIL）/ 回归集（conv_clean 三排除 + 去重 +
     截断）/ 已知失败（conv_badcase / flow_failed）/ pending HIL 不入集 /
     指纹记录与漂移拒绝；
  D. 真库只读 smoke：build 在只读连接上跑通（自证零写语句），Mock 标记在真实
     数据上确实被排除，标注口径与溯源字段齐全；
  E. 接线断言：build_evalset.py 默认附带构建 + --no-trace 旧行为 + config 双登记
     + 模块无写语句（静态）。

双环境口径：干净库自建 MBSE_DB_PATH 临时空库（import 前切库）；真库段只读。
"""
import os
import shutil
import sqlite3
import sys


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
_TMP = os.path.join(ROOT, "tmp", "verify_trace2dataset")
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


def main():
    # ── A. 纯函数与注册表 ───────────────────────────────────────────
    print("== A. 纯函数与注册表（8 抽取器 / 统一列 / 过滤 / 截断）==")
    sys.path.insert(0, os.path.join(ROOT, "tools", "eval"))
    import trace2dataset as T2D

    names = [s.name for s in T2D._SPECS]
    check("A1 抽取器注册表 8 个（intent×2 / hil×2 / conv×2 / flow×2）",
          names == ["intent_confirmed", "intent_rejected", "hil_approved", "hil_rejected",
                    "conv_clean", "conv_badcase", "flow_completed", "flow_failed"],
          f"got={names}")
    cols = ("source_id", "query", "response", "gold", "seen_at", "meta_json")
    check("A2 每个抽取器 SQL 统一输出六列（引擎零特判）",
          all(all(c in s.sql for c in cols) for s in T2D._SPECS))
    kmap = {s.name: (s.kind, s.label) for s in T2D._SPECS}
    check("A3 标签口径三档：strong=人工裁决（intent/HIL），weak=回归，negative=已知失败",
          kmap["intent_confirmed"] == ("positive", "strong")
          and kmap["hil_rejected"] == ("negative", "strong")
          and kmap["conv_clean"] == ("regression", "weak")
          and kmap["conv_badcase"] == ("negative", "negative")
          and kmap["flow_completed"] == ("regression", "weak"),
          f"got={kmap}")
    _cc = next(s for s in T2D._SPECS if s.name == "conv_clean")
    check("A4 conv_clean 三排除写死在 SQL（open 坏样本会话 / 失败调用会话）"
          "且只取该 user 消息后的第一条 assistant",
          "b.status = 'open'" in _cc.sql and "s.call_failed = 1" in _cc.sql
          and "ORDER BY a.id LIMIT 1" in _cc.sql,
          "排除条件或首答语义缺失")
    _hb = next(s for s in T2D._SPECS if s.name == "hil_approved")
    check("A5 HIL 抽取只取已裁决（approved/rejected；pending 无标签永不入集）",
          "status = 'approved'" in _hb.sql
          and "status = 'rejected'" in next(s for s in T2D._SPECS
                                            if s.name == "hil_rejected").sql)

    check("A6 _in_window：窗内进 / 窗外出 / 空时间戳出（无法证明新鲜的证据不取）",
          T2D._in_window("2026-10-18 10:00:00", "2026-10-01 00:00:00")
          and not T2D._in_window("2026-09-01 10:00:00", "2026-10-01 00:00:00")
          and not T2D._in_window("", "2026-10-01 00:00:00")
          and not T2D._in_window(None, "2026-10-01 00:00:00"))
    mk = T2D._marker_list("（Mock 回答）,Mock 降级模式")
    check("A7 _is_mock：标记命中 query 或 response 即排除；干净文本不误伤",
          T2D._is_mock("（Mock 回答）你好", "", mk)
          and T2D._is_mock("你好", "当前为 Mock 降级模式", mk)
          and not T2D._is_mock("卫星系统组成", "由电源分系统…", mk))
    check("A8 _clip：超限加字符数标记（内容 + 标记 = 61 字符），未超限原样",
          T2D._clip("x" * 100, 50).endswith("[100 chars]")
          and len(T2D._clip("x" * 100, 50)) == 50 + len("…[100 chars]")
          and T2D._clip("短", 50) == "短",
          f"got_len={len(T2D._clip('x' * 100, 50))}")
    check("A9 _safe_json：合法 JSON 成 dict，坏值兜底不炸",
          T2D._safe_json('{"a":1}') == {"a": 1}
          and T2D._safe_json("not-json").get("raw") == "not-json"
          and T2D._safe_json(None) == {})

    # ── B. 配置消费（七键 + 变异自证）────────────────────────────────
    print("\n== B. 取材口径走配置（DEFAULT_CONFIG/CONFIG_SCHEMA 双登记 + 变异自证）==")
    import core.config as CFG

    dc = CFG.DEFAULT_CONFIG.get("trace2dataset") or {}
    keys7 = ("enabled", "window_days", "max_per_extractor", "exclude_mock",
             "mock_markers", "min_query_chars", "max_response_chars")
    check("B1 DEFAULT_CONFIG.trace2dataset 七键齐全", all(k in dc for k in keys7),
          f"got={sorted(dc)}")
    check("B2 CONFIG_SCHEMA.trace2dataset 登记（前端契约，类型齐全）",
          all(k in (CFG.CONFIG_SCHEMA.get("trace2dataset") or {}) for k in keys7)
          and (CFG.CONFIG_SCHEMA["trace2dataset"]["window_days"]["type"] == "int"
               and CFG.CONFIG_SCHEMA["trace2dataset"]["exclude_mock"]["type"] == "bool"
               and CFG.CONFIG_SCHEMA["trace2dataset"]["mock_markers"]["type"] == "str"),
          f"got={sorted(CFG.CONFIG_SCHEMA.get('trace2dataset') or {})}")
    st = T2D.get_settings()
    check("B3 get_settings 默认口径（窗口90/上限300/排Mock/最短4/截断600）",
          st == {"enabled": True, "window_days": 90, "max_per_extractor": 300,
                 "exclude_mock": True, "mock_markers": "（Mock 回答）,Mock 降级模式",
                 "min_query_chars": 4, "max_response_chars": 600},
          f"got={st}")

    # ── C. 干净库端到端 ─────────────────────────────────────────────
    print("\n== C. 干净库端到端（强标注 / 回归三排除 / 已知失败 / 指纹漂移）==")
    from database import init_db, get_db
    init_db()
    conn = get_db()
    conn.row_factory = sqlite3.Row

    def seed(c):
        # 会话 100：干净（两轮：一轮旧窗外、一轮新）；500：与 100 第二轮完全同文（去重靶）
        # 时基纪律：窗内证据一律 -2 day（对任意时区都确定性地落在 90 天窗口内、
        # 且确定性地掉出 M1 的 1 天窗口——SQLite datetime 取 UTC，cutoff 取本地
        # 时区，只有 ≥2 天的间距才不被时区偏移翻转）；-100 day = 窗外锚点。
        c.execute("INSERT INTO conversations (id, title, intent) VALUES (100,'净会话','chat')")
        c.execute("INSERT INTO conversations (id, title, intent) VALUES (200,'失败会话','chat')")
        c.execute("INSERT INTO conversations (id, title, intent) VALUES (300,'坏样本会话','chat')")
        c.execute("INSERT INTO conversations (id, title, intent) VALUES (400,'Mock会话','chat')")
        c.execute("INSERT INTO conversations (id, title, intent) VALUES (500,'重复会话','chat')")

        def msg(cid, role, content, days=2):
            c.execute(
                "INSERT INTO messages (conversation_id, role, content, created_at) "
                "VALUES (?,?,?, datetime('now', ?))", (cid, role, content, "-%d day" % days))

        msg(100, "user", "帮我查一下卫星通信系统由哪些分系统组成", 100)   # 窗外
        msg(100, "assistant", "窗外旧答", 100)
        msg(100, "user", "宽带通信平台的电源分系统有哪些规格", 2)
        msg(100, "assistant", "电源分系统规格包括：母线电压 28V …（有效回答）", 2)
        msg(500, "user", "宽带通信平台的电源分系统有哪些规格", 2)        # 同文重复
        msg(500, "assistant", "电源分系统规格包括：母线电压 28V …（有效回答）", 2)
        msg(200, "user", "这条会话里有一次失败的 LLM 调用", 2)
        msg(200, "assistant", "答非所问", 2)
        msg(300, "user", "坏样本会话里的用户输入", 2)
        msg(300, "assistant", "坏答", 2)
        msg(400, "user", "Mock 会话的用户输入", 2)
        msg(400, "assistant", "（Mock 回答）已收到你的消息", 2)
        c.execute(
            "INSERT INTO llm_usage_stats (provider_name, model_name, used_mock, call_failed, "
            "failure_kind, failure_msg, finish_reason, conversation_id, run_id, created_at) "
            "VALUES ('p','m',0,1,'quota','余额不足','stop',200,0, datetime('now','-2 day'))")
        c.execute(
            "INSERT INTO intent_samples (text, intent, status, source, hit_intent, created_at, updated_at) "
            "VALUES ('生成一份需求分析报告','report_gen','confirmed','gate','', datetime('now','-2 day'), datetime('now','-2 day'))")
        c.execute(
            "INSERT INTO intent_samples (text, intent, status, source, hit_intent, created_at, updated_at) "
            "VALUES ('查询实体之间的关系','relation_query','rejected','gate','knowledge_qa', datetime('now','-2 day'), datetime('now','-2 day'))")
        c.execute(
            "INSERT INTO intent_samples (text, intent, status, source, created_at, updated_at) "
            "VALUES ('很久以前确认的旧样本','legacy_intent','confirmed','gate', datetime('now','-100 day'), datetime('now','-100 day'))")
        c.execute(
            "INSERT INTO hil_confirmations (agent_id, action, payload, preview, status, "
            "decided_by, decided_at, created_at) VALUES "
            "('a1','entity_create','{\"x\":1}','创建实体：电源分系统','approved','王工', datetime('now','-2 day'), datetime('now','-2 day'))")
        c.execute(
            "INSERT INTO hil_confirmations (agent_id, action, payload, preview, status, "
            "decided_by, decided_at, created_at) VALUES "
            "('a1','relation_create','{\"y\":2}','创建关系：卫星-包含-天线','rejected','王工', datetime('now','-2 day'), datetime('now','-2 day'))")
        c.execute(
            "INSERT INTO hil_confirmations (agent_id, action, payload, preview, status, created_at) "
            "VALUES ('a1','entity_create','{\"z\":3}','待确认：创建实体','pending', datetime('now','-2 day'))")
        c.execute(
            "INSERT INTO flow_runs (flow_id, flow_name, status, order_json, total_latency_ms, "
            "error_count, created_at) VALUES ('f1','需求分析流水线','completed','[\"n1\",\"n2\"]',82,0, datetime('now','-2 day'))")
        c.execute(
            "INSERT INTO flow_runs (flow_id, flow_name, status, order_json, total_latency_ms, "
            "error_count, created_at) VALUES ('f2','Impact分析流水线','failed','[\"n1\"]',30,1, datetime('now','-2 day'))")
        c.execute(
            "INSERT INTO bad_cases (kind, subject, title, detail, severity, occurrences, "
            "first_seen_at, last_seen_at, last_src_id, status, source) "
            "VALUES ('llm_call_failed','conv:300','坏样本主题','JSONDecodeError','high',3,"
            "datetime('now','-2 day'), datetime('now','-2 day'), 7, 'open', 'collector')")
        c.commit()

    seed(conn)
    data = T2D.build(conn)
    cnt = data["counts"]
    # intent_confirmed=1 而非 2：另一条 confirmed 是 -100 天旧样本，被窗口过滤
    # （C7 专项断言它不入集）——窗口与强标注两个口径在此交汇，互不打架。
    check("C1 抽取计数：intent confirmed=1（旧样本被窗口挡下）/ rejected=1 / hil 双 1",
          cnt["by_extractor"].get("intent_confirmed") == 1
          and cnt["by_extractor"].get("intent_rejected") == 1
          and cnt["by_extractor"].get("hil_approved") == 1
          and cnt["by_extractor"].get("hil_rejected") == 1,
          f"got={cnt['by_extractor']}")
    cases = {c["extractor"]: c for c in data["cases"]}
    # skipped_window=2：窗外两条分属不同抽取器——conv 100 的 -100 天旧用户消息
    # （conv_clean 域）+ -100 天旧 intent 样本（intent 域），窗口过滤跨域生效。
    check("C2 conv_clean 三排除 + 去重：只剩净会话 1 例（失败/坏样本/Mock/窗外全排除，"
          "重复同文只留一条；窗外 2 条跨抽取器都被挡）",
          cnt["by_extractor"].get("conv_clean") == 1
          and data["summary"]["skipped_mock"] == 1
          and data["summary"]["skipped_window"] == 2
          and data["summary"]["skipped_dup"] == 1,
          f"conv_clean={cnt['by_extractor'].get('conv_clean')} "
          f"summary={ {k: data['summary'][k] for k in ('skipped_mock','skipped_window','skipped_dup')} }")
    check("C3 pending HIL 不入集（无裁决=无标签）",
          "hil_approved" in cases and "pending" not in
          str([c["gold"] for c in data["cases"]]))
    bc = [c for c in data["cases"] if c["extractor"] == "conv_badcase"]
    check("C4 conv_badcase：坏样本会话输入入负样本集，gold=kind 且带台账溯源",
          len(bc) == 1 and bc[0]["gold"] == "llm_call_failed"
          and bc[0]["kind"] == "negative" and bc[0]["label"] == "negative"
          and bc[0]["meta"].get("badcase_id") and bc[0]["meta"].get("severity") == "high",
          f"got={bc}")
    check("C5 flow 双抽取：completed 回归 + failed 已知失败",
          cnt["by_extractor"].get("flow_completed") == 1
          and cnt["by_extractor"].get("flow_failed") == 1
          and cases["flow_failed"]["kind"] == "negative")
    check("C6 溯源四要素齐全（source_table/source_id/seen_at/meta）",
          all(c.get("source_id") and c.get("seen_at") and isinstance(c.get("meta"), dict)
              for c in data["cases"]))
    check("C7 窗外旧 intent 不入集（窗口 90 天口径真被消费）",
          all("很久以前确认的旧样本" != c["query"] for c in data["cases"]
              if c["extractor"] == "intent_confirmed"))

    # C9a 指纹零漂移必须赶在 C8 截断测试（又要插数据）之前验：build 完的那一刻
    # 集内指纹与当前库一致，此后任何插入都是漂移——顺序即语义。
    fp = data["fingerprint"]
    check("C9a 指纹记录在集内且与当前库一致（刚建完零漂移）",
          T2D.compare(fp, T2D.trace_fingerprint(conn)) == [],
          f"got={T2D.compare(fp, T2D.trace_fingerprint(conn))[:3]}")

    # C8 截断
    long_resp = "电" * 900
    conn.execute("INSERT INTO conversations (id, title, intent) VALUES (600,'长答会话','chat')")
    conn.execute("INSERT INTO messages (conversation_id, role, content, created_at) "
                 "VALUES (600,'user','长回答测试问题', datetime('now','-2 day'))")
    conn.execute("INSERT INTO messages (conversation_id, role, content, created_at) "
                 "VALUES (600,'assistant',?, datetime('now','-2 day'))", (long_resp,))
    conn.commit()
    data2 = T2D.build(conn)
    lc = [c for c in data2["cases"] if c["extractor"] == "conv_clean"
          and c["query"] == "长回答测试问题"]
    check("C8 response 截断生效（600 上限 + 字符数标记）",
          len(lc) == 1 and lc[0]["response"].endswith("[900 chars]"),
          f"got_len={len(lc[0]['response']) if lc else 'N/A'}")

    # C9b/C9c 漂移拒绝（C8 的插入已让库偏离 fp，再叠一条 confirmed 样本）
    conn.execute(
        "INSERT INTO intent_samples (text, intent, status, source, created_at, updated_at) "
        "VALUES ('漂移后新增的确认样本','drift_intent','confirmed','gate', datetime('now'), datetime('now'))")
    conn.commit()
    diffs = T2D.compare(fp, T2D.trace_fingerprint(conn))
    check("C9b ★生产数据一变指纹就漂（confirmed 内容哈希 + 行数双通道）",
          any("confirmed_intent_texts_sha1" in d for d in diffs)
          and any("intent_samples" in d for d in diffs), f"got={diffs}")
    check("C9c ★check_drift 拒绝而非警告（strict=False 也返 False）",
          T2D.check_drift(fp, conn, strict=False) is False)
    conn.close()

    # ── B' 变异自证（口径必须真被消费）──────────────────────────────
    print("\n== B'. 变异自证（四个口径逐一变异，断言必须转红）==")
    # 重建干净库：C 段种子 + C8 截断/C9b 漂移的痕迹全部清掉，五个变异从同一
    # 基线出发。get_db 每次新建连接读同一 DB_PATH（import 期绑定，改环境变量
    # 已无效），删文件 + WAL 边车后重新 init 是唯一干净的基线重置方式。
    for _sfx in ("", "-wal", "-shm"):
        try:
            os.remove(_CLEAN_DB + _sfx)
        except OSError:
            pass
    from database import init_db as _init2, get_db as _get2
    _init2()
    conn2 = _get2()
    conn2.row_factory = sqlite3.Row
    seed(conn2)
    _og, _ob = CFG.get, CFG.as_bool

    def _mut(section, key, value, is_bool=False):
        if is_bool:
            CFG.as_bool = lambda s, k, d=False, _v=value: (
                _v if (s, k) == (section, key) else _ob(s, k, d))
        else:
            CFG.get = lambda s, k, d=None, _v=value: (
                _v if (s, k) == (section, key) else _og(s, k, d))

    try:
        # M1 window_days=1 ⇒ 2 天前的 confirmed/rejected/hil/flow 全部掉出窗外
        _mut("trace2dataset", "window_days", 1)
        m1 = T2D.build(conn2)
        check("M1 ★window_days=1 ⇒ 只剩当天证据（旧样本掉出，口径真被消费）",
              m1["counts"]["by_extractor"].get("intent_confirmed") == 0
              and m1["counts"]["by_extractor"].get("hil_approved") == 0,
              f"got={m1['counts']['by_extractor']}")
        CFG.get = _og
        # M2 exclude_mock=False ⇒ Mock 会话进集
        _mut("trace2dataset", "exclude_mock", False, is_bool=True)
        m2 = T2D.build(conn2)
        check("M2 ★exclude_mock=False ⇒ Mock 回答入集（默认排除是真开关）",
              any("（Mock 回答）" in (c["response"] or "") for c in m2["cases"]),
              "Mock 仍未入集")
        CFG.as_bool = _ob
        # M3 max_per_extractor=1 ⇒ 每抽取器最多 1 例
        _mut("trace2dataset", "max_per_extractor", 1)
        m3 = T2D.build(conn2)
        check("M3 ★max_per_extractor=1 ⇒ 每抽取器 ≤1 例（防单一源淹掉全集）",
              all(v <= 1 for v in m3["counts"]["by_extractor"].values())
              and m3["counts"]["total"] == len(m3["counts"]["by_extractor"]),
              f"got={m3['counts']['by_extractor']}")
        CFG.get = _og
        # M4 min_query_chars=200 ⇒ 全部 query 太短，零样本
        _mut("trace2dataset", "min_query_chars", 200)
        m4 = T2D.build(conn2)
        check("M4 ★min_query_chars=200 ⇒ 零候选（短 query 过滤真被消费）",
              m4["counts"]["total"] == 0, f"got={m4['counts']['total']}")
        CFG.get = _og
        # M5 enabled=False ⇒ 返空不报错
        _mut("trace2dataset", "enabled", False, is_bool=True)
        m5 = T2D.build(conn2)
        # 注意断言靶点：disabled 时 collect 只返 {enabled:False, candidates:[],
        # summary:{skipped_extractors:{}}}，build 把它投影成 counts 全零 + cases 空。
        check("M5 ★enabled=False ⇒ 零样本不报错（总开关真被消费）",
              m5["cases"] == [] and m5["counts"]["total"] == 0
              and m5["counts"]["by_extractor"] == {}
              and m5["settings"]["enabled"] is False,
              f"got={m5['counts']}")
    finally:
        CFG.get, CFG.as_bool = _og, _ob
        conn2.close()

    # ── D. 真库只读 smoke ──────────────────────────────────────────
    print("\n== D. 真库只读 smoke（只读连接上 build = 自证零写语句）==")
    _prod = os.path.join(ROOT, "mbse.db")
    if not os.path.exists(_prod):
        skip("D1 真库 smoke", "mbse.db 不存在（CI 环境）")
    else:
        pc = sqlite3.connect("file:%s?mode=ro" % _prod.replace("\\", "/"), uri=True)
        pc.row_factory = sqlite3.Row
        d = T2D.build(pc)      # 只读连接：任何写语句都会在这里炸
        be = d["counts"]["by_extractor"]
        print("  真库 trace 候选 %d 例：%s" % (d["counts"]["total"], be))
        check("D1 真库强标注存在（intent confirmed/rejected 都取到）",
              be.get("intent_confirmed", 0) > 0 and be.get("intent_rejected", 0) > 0,
              f"got={be}")
        check("D2 真库回归集存在（conv_clean + flow_completed）",
              be.get("conv_clean", 0) > 0 and be.get("flow_completed", 0) > 0,
              f"got={be}")
        markers = T2D._marker_list(T2D.get_settings()["mock_markers"])
        leak = [c["id"] for c in d["cases"]
                if T2D._is_mock(c["query"], c["response"], markers)]
        check("D3 ★真库 Mock 回答零泄漏（排除开关在真实数据上真生效）",
              not leak, f"泄漏 {len(leak)} 条: {leak[:3]}")
        check("D4 真库标注口径齐全（strong/weak/negative 三档 + 溯源字段）",
              set(d["counts"]["by_label"]) <= {"strong", "weak", "negative"}
              and all(c.get("source_id") and c.get("seen_at") for c in d["cases"]))
        check("D5 指纹与只读连接自洽（build 内含 trace_fingerprint 可重复执行）",
              T2D.compare(d["fingerprint"], T2D.trace_fingerprint(pc)) == [])
        pc.close()

    # ── E. 接线完整性 ───────────────────────────────────────────────
    print("\n== E. 接线完整性（build_evalset + config 双登记 + 零写语句）==")
    _be = open(os.path.join(ROOT, "tools", "eval", "build_evalset.py"),
               encoding="utf-8").read()
    check("E1 build_evalset.py 默认附带构建 trace 集（--no-trace 可退回旧行为）",
          '"--no-trace" in sys.argv' in _be and "import trace2dataset" in _be
          and "trace2dataset.build(conn)" in _be
          and "trace2dataset.default_out_path()" in _be)
    check("E2 build_evalset.py 文档段已声明 P1-1 trace 域（后人读 docstring 即知）",
          "P1-1" in _be and "trace_evalset.json" in _be)
    _cf = open(os.path.join(ROOT, "core", "config.py"), encoding="utf-8").read()
    check("E3 core/config.py 双登记（DEFAULT_CONFIG + CONFIG_SCHEMA）",
          _cf.count('"trace2dataset"') >= 2, f"count={_cf.count('\"trace2dataset\"')}")
    _t2 = open(os.path.join(ROOT, "tools", "eval", "trace2dataset.py"),
               encoding="utf-8").read()
    import re as _re
    sql_all = "\n".join(s.sql for s in T2D._SPECS)
    bad_write = [w for w in ("INSERT", "UPDATE", "DELETE", "DROP", "CREATE", "ALTER")
                 if _re.search(r"\b%s\b" % w, sql_all, _re.I)]
    check("E4 ★抽取器 SQL 零写语句（只读取材，静态断言）",
          not bad_write, f"发现写语句关键词: {bad_write}")
    check("E5 模块 docstring 写明三档标注口径与弱标注禁令（消费方区别对待）",
          "strong" in _t2 and "weak" in _t2 and "negative" in _t2
          and "自己骗自己" in _t2)

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
    print("全部通过 —— Trace2Dataset（8 抽取器注册表 + 七键配置变异自证 + 强/弱/负三档"
          "标注 + 三排除回归集 + 指纹漂移拒绝 + 真库只读 smoke + 零写语句）。")


if __name__ == "__main__":
    main()
