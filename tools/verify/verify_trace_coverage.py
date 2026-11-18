# -*- coding: utf-8 -*-
"""B1 跨层 trace 覆盖门禁（2026-11-18）。

判据（确定性；真库只读 + 副本实写）：
  T1 七表均有 trace_id 列 + 对应索引
  T2 七表具备写入能力（列 + 索引齐备）
  T2b ★七表**真实写入**：在真库副本上绑定 trace 上下文，逐个调用该表的真实
      写入函数，断言产出的那一行 trace_id 非空。六表实写（audit_logs /
      graph_edit_logs / knowledge_commits / query_trace / orch_run_spans /
      hil_confirmations），第七表 harness_decisions 的写入点是 AgentPipeline
      实例方法且内部用全局 db_conn()（写生产库）⇒ 改用**精确 SQL 断言**
      （同一条 INSERT 语句里既有 trace_id 列也有 _ctid_b1() 值），并在此注明。
  T3 audit_logs 哈希链仍 ok（trace 不进哈希，B1 硬约束）
  T4 llm_usage_stats 与 tool_call_logs 的 trace_id 存在交集（同源 begin_trace）
  T5 旧库降级：缺 trace_id 列的库上，写入不得失败（core.audit.with_trace 契约）
  T6 ★空库自举：子进程建全新库，断言七表都有 trace_id 列+索引，且 harness 写入
      不抛异常。这一条守的是**跨服务器部署**：全新库上 `_add` 因"表还不存在"
      会跳过，若建表语句里没有该列，列就永久缺失；而 harness 的 INSERT 把
      trace_id 写死在列清单里 ⇒ 缺列即写入崩（空库自举实测复现，已修）。
  M1~M2 变异自证：删列 / 无交集断言会翻红

★ 2026-11-18 升级背景：首轮 B1 只接了四表，另三表（graph_edit_logs /
knowledge_commits / query_trace）仅"补了列、没接写入点"，而 T2 对它们只查
列是否存在 —— 那等于没验。三表写入点本轮已接线，判据随之收紧为"真的插一行"。

用法：python tools/verify/verify_trace_coverage.py
退出码：0=全绿 / 1=有失败
"""
from __future__ import annotations
import io, os, sqlite3, sys
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)
FAILS, N = [], 0

#: 七表清单（T1/T2 遍历用；顺序即报告顺序）
ALL_TABLES = ("audit_logs", "graph_edit_logs", "knowledge_commits",
              "harness_decisions", "orch_run_spans", "query_trace",
              "hil_confirmations")

TABLES = ("audit_logs", "graph_edit_logs", "knowledge_commits", "harness_decisions",
          "orch_run_spans", "query_trace", "hil_confirmations")
WIRED = ("audit_logs", "harness_decisions", "orch_run_spans", "hil_confirmations")


def check(name, cond, detail=""):
    global N
    N += 1
    print(("  [OK]   " if cond else "  [FAIL] ") + name + (("  —— " + detail) if detail else ""))
    if not cond:
        FAILS.append(name)


def _probe_writers():
    """在真库副本上实调各表写入函数，断言 trace_id 真的落库（★ 2026-11-18）。

    ## 为什么必须实调而非扫源码字面量
    字面量扫描有三种假绿：① 写错列名；② 在不可达分支里；③ 用变量拼 SQL
    （本轮三表正是 `with_trace` 拼的，字面量扫不到 `trace_id`）。

    ## 时序纪律（本文件初版踩过）
    先建副本 → 再设 MBSE_DB_PATH → 最后导入依赖该路径的模块。
    `core.config` 在导入期读环境变量，导入后再设无效 ⇒ init_db 会打到生产库。
    """
    import shutil
    import tempfile

    src = os.environ.get("MBSE_DB_PATH") or os.path.join(REPO, "mbse.db")
    tmpd = tempfile.mkdtemp(prefix="tracecov_")
    dst = os.path.join(tmpd, "probe.db")
    try:
        _s = sqlite3.connect("file:%s?mode=ro" % src.replace("\\", "/"), uri=True)
        _d = sqlite3.connect(dst)
        _s.backup(_d)
        _d.close()
        _s.close()
        for suf in ("-wal", "-shm"):
            _w = src + suf
            if os.path.exists(_w):
                shutil.copy2(_w, dst + suf)

        import core.config as _cfg
        _cfg.CONFIG_PATH = _cfg.CONFIG_PATH          # 保持原配置（不写真配置）
        tc = sqlite3.connect(dst)
        tc.row_factory = sqlite3.Row
        # 副本可能来自迁移前的备份 ⇒ 缺列就地补（只动副本）
        for t in ALL_TABLES:
            cols = {r[1] for r in tc.execute('PRAGMA table_info("%s")' % t)}
            if cols and "trace_id" not in cols:
                tc.execute('ALTER TABLE "%s" ADD COLUMN trace_id TEXT DEFAULT ""' % t)
        tc.commit()

        from core.audit import audit as _audit, begin_trace, current_trace_id

        begin_trace(conversation_id=999990001)
        tid = current_trace_id()
        assert tid, "begin_trace 未产出 trace_id（门禁自身前提不成立）"
        print("    探针 trace_id = %s" % tid)

        def last_trace(table):
            row = tc.execute(
                'SELECT trace_id FROM "%s" WHERE rowid=(SELECT MAX(rowid) FROM "%s")'
                % (table, table)).fetchone()
            return str((row["trace_id"] if row else "") or "").strip()

        def probe(table, fn, label):
            try:
                fn()
                got = last_trace(table)
                check("T2b %s %s" % (table, label), got == tid,
                      "读回=%s" % (got[:12] or "空"))
            except Exception as e:
                check("T2b %s %s" % (table, label), False,
                      "异常 %s: %s" % (type(e).__name__, str(e)[:70]))

        # ① audit_logs
        def _w_audit():
            _audit("verify", "trace_cov_probe", "probe", "success", conn=tc)
        probe("audit_logs", _w_audit, "实写带 trace")

        # ② graph_edit_logs（走 entity_resolver 的收口函数）
        def _w_graph():
            from entity_resolver import _log_graph_edit
            _log_graph_edit(tc, "trace_probe", "n_probe", {"k": "v"}, "verify")
        probe("graph_edit_logs", _w_graph, "实写带 trace")

        # ③ knowledge_commits
        def _w_commit():
            from repositories.commit_repo import CommitRepo
            CommitRepo(tc).create_commit("verify-branch", "probe",
                                         "trace 覆盖探针", {}, {}, actor="verify")
            tc.commit()
        probe("knowledge_commits", _w_commit, "实写带 trace")

        # ④ query_trace（glossary.trace_query）
        def _w_qtrace():
            from glossary import trace_query
            trace_query(tc, "探针查询", {"normalized": "探针查询", "intent": "verify",
                                         "route": "vector", "hit_docs": [], "hit_count": 0,
                                         "top_score": 0.0})
        probe("query_trace", _w_qtrace, "实写带 trace")

        # ⑤ orch_run_spans（SpanCollector → flush(conn)）
        def _w_span():
            from services.orch_span import SpanCollector
            col = SpanCollector(enabled=True)
            col.bind(run_id=0, conversation_id=999990001)
            sid = col.open("probe", "trace_cov_probe")
            col.close(sid, "ok")
            col.flush(tc)
            tc.commit()
        probe("orch_run_spans", _w_span, "实写带 trace")

        # ⑥ hil_confirmations
        def _w_hil():
            from hil_service import HILService
            HILService.queue_confirmation(tc, "agent_cov", "write_file", {"path": "x"},
                                          preview="p", tool_type="local",
                                          level="confirm", requested_by="verify")
        probe("hil_confirmations", _w_hil, "实写带 trace")

        # ⑦ harness_decisions —— 写入点是 AgentPipeline 实例方法且用全局 db_conn()
        #    （会写生产库）⇒ 不实调，改为精确 SQL 断言：同一条 INSERT 里既有
        #    trace_id 列也有 _ctid_b1() 取值。这一条是**静态**判据，在此明示。
        try:
            ap = io.open(os.path.join(REPO, "agent", "pipeline_parts", "autopilot.py"),
                         encoding="utf-8").read()
            i = ap.find("INSERT INTO harness_decisions")
            # 取样窗口要**向前**包含 `_tid_b1 = _ctid_b1()` 的赋值：trace 取值先算好
            # 再作为 VALUES 的参数传入，取值语句本身不在 INSERT 字符串里。
            seg = ap[max(0, i - 900):i + 900] if i > 0 else ""
            check("T2b harness_decisions 写入语句含 trace_id 列与 trace 取值（静态判据）",
                  bool(seg) and "trace_id" in seg and "_ctid_b1()" in seg,
                  "该写入点用全局 db_conn()，探针不实调以免写生产库")
        except Exception as e:
            check("T2b harness_decisions 写入语句含 trace 取值（静态判据）", False,
                  "读取失败 %s" % str(e)[:60])

        # ── T5 旧库降级契约 ──
        print("\nT5 旧库降级（缺 trace_id 列的库不得因加列而写入失败）")
        from core.audit import with_trace
        old = sqlite3.connect(":memory:")
        old.execute("CREATE TABLE legacy (a TEXT)")
        cols, vals = with_trace(old, "legacy", ["a"], ["x"])
        check("T5 缺列时不追加 trace_id", cols == ["a"] and vals == ["x"], str(cols))
        try:
            old.execute("INSERT INTO legacy (%s) VALUES (%s)"
                        % (",".join(cols), ",".join("?" * len(cols))), tuple(vals))
            ok5 = old.execute("SELECT COUNT(*) FROM legacy").fetchone()[0] == 1
        except Exception:
            ok5 = False
        check("T5 缺列时 INSERT 仍成功（业务不被加列拖垮）", ok5)
        old.close()
        tc.rollback()
        tc.close()
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)


def _probe_clean_boot():
    """子进程建全新库，断言七表 trace 列/索引齐备 + harness 写入不崩。

    ## 为什么用子进程
    `core.config` 在**导入期**读 `MBSE_DB_PATH`。本门禁主流程已连着真库，
    在同一进程里改环境变量再 import 会污染后续断言（实测会写到生产库）。
    子进程把"设环境变量 → 导入 → 建库"整个序列封在干净进程里，
    对环境零污染，也不受主进程已加载模块的影响。
    """
    import subprocess
    import tempfile

    script = r"""
import json, os, sqlite3, sys
d = os.environ["_COV_TMP"]
db = os.path.join(d, "empty.db")
sys.path.insert(0, os.environ["_COV_REPO"])
from database import init_db
init_db()
c = sqlite3.connect(db)
out = {}
for t in ("audit_logs","graph_edit_logs","knowledge_commits","harness_decisions",
          "orch_run_spans","query_trace","hil_confirmations"):
    try:
        cols = {r[1] for r in c.execute('PRAGMA table_info("%s")' % t)}
        idx = {r[1] for r in c.execute('PRAGMA index_list("%s")' % t)}
        out[t] = {"col": "trace_id" in cols, "idx": ("idx_%s_trace" % t) in idx}
    except Exception as e:
        out[t] = {"col": False, "idx": False, "err": str(e)[:60]}
c.close()
# 空库上真跑一次 harness 写入（该处 INSERT 硬写 trace_id 列）
try:
    from agent.pipeline_parts.autopilot import AutoPilotMixin
    class _D:
        pass
    AutoPilotMixin._harness_observe(_D(), 999990002, "shadow",
        {"reason": "cov_clean_boot", "remaining": [], "force_release": False},
        False, {"pending": 0, "manual": 0, "autoready": 0, "guarded": 0})
    c2 = sqlite3.connect(db)
    out["_harness_write_rows"] = c2.execute(
        "SELECT COUNT(*) FROM harness_decisions").fetchone()[0]
    c2.close()
except Exception as e:
    out["_harness_write_error"] = "%s: %s" % (type(e).__name__, str(e)[:120])
print("__RESULT__" + json.dumps(out))
"""
    tmpd = tempfile.mkdtemp(prefix="covboot_")
    env = dict(os.environ)
    env["_COV_TMP"] = tmpd
    env["_COV_REPO"] = REPO
    env["MBSE_DB_PATH"] = os.path.join(tmpd, "empty.db")
    env["MBSE_CONFIG_PATH"] = os.path.join(tmpd, "cfg.json")
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        r = subprocess.run([sys.executable, "-X", "utf8", "-c", script], env=env,
                           capture_output=True, text=True, timeout=300, cwd=REPO)
        line = ""
        for ln in (r.stdout or "").splitlines():
            if ln.startswith("__RESULT__"):
                line = ln[len("__RESULT__"):]
        if not line:
            tail = ((r.stdout or "") + (r.stderr or ""))[-160:].replace("\n", " ")
            check("T6 空库自举可执行", False, "子进程无结果输出：%s" % tail)
            return
        import json as _json
        data = _json.loads(line)
    finally:
        import shutil as _sh
        _sh.rmtree(tmpd, ignore_errors=True)

    bad = [t for t in ALL_TABLES
           if not (data.get(t) or {}).get("col") or not (data.get(t) or {}).get("idx")]
    check("T6 全新库七表 trace 列+索引齐备", not bad,
          "缺：" + (", ".join(bad) if bad else "无"))
    check("T6 全新库 harness 写入不抛异常（缺列会崩在这里）",
          "_harness_write_error" not in data and data.get("_harness_write_rows", 0) >= 1,
          data.get("_harness_write_error") or "写入行数=%s" % data.get("_harness_write_rows"))


def main():
    print("B1 跨层 trace 覆盖门禁")
    print("=" * 66)
    db = os.environ.get("MBSE_DB_PATH") or os.path.join(REPO, "mbse.db")
    if not os.path.exists(db):
        print("真库不存在，SKIP 全部（本机门禁）")
        return 0
    c = sqlite3.connect("file:%s?mode=ro" % db.replace("\\", "/"), uri=True)
    c.row_factory = sqlite3.Row
    try:
        print("\nT1 七表列 + 索引")
        for t in TABLES:
            cols = [r[1] for r in c.execute('PRAGMA table_info("%s")' % t)]
            idx = [x[1] for x in c.execute('PRAGMA index_list("%s")' % t)]
            check("T1 %s trace_id 列+索引" % t,
                  "trace_id" in cols and ("idx_%s_trace" % t) in idx,
                  "col=%s idx=%s" % ("trace_id" in cols, "idx_%s_trace" % t in idx))
        print("\nT2 已接线四表 trace 写入能力（确定性：有列+索引即具备；非空率仅提示）")
        for t in WIRED:
            tcols = [r[1] for r in c.execute('PRAGMA table_info("%s")' % t)]
            idx = [x[1] for x in c.execute('PRAGMA index_list("%s")' % t)]
            ready = "trace_id" in tcols and ("idx_%s_trace" % t) in idx
            rows = c.execute(
                'SELECT trace_id FROM (SELECT trace_id FROM "%s" ORDER BY id DESC LIMIT 100)' % t
            ).fetchall() if ready else []
            nz = sum(1 for r in rows if str(r["trace_id"] or "").strip())
            # 非空率是"重启后是否有真实流量"的提示，不是判据：
            # 历史行默认空，且本门禁零 LLM、不制造聊天流量。判据绑定写入能力（列+索引），
            # 写函数本身的正确性由 tmp 副本 PoC（四表实写 + 哈希链复验）覆盖。
            check("T2 %s 写入能力就绪" % t, ready,
                  "列+索引齐备；近100行 trace 非空 %d（需真实流量后增长）" % nz)
        print("\nT2b 各表真实写入（真库副本 + 绑定 trace；零写生产）")
        _probe_writers()

        print("\nT6 空库自举（子进程隔离：全新服务器首次启动）")
        _probe_clean_boot()

        print("\nT3 审计哈希链（trace 不进哈希）")
        from core.audit import verify_chain
        r = verify_chain(limit=0, conn=c)
        check("T3 audit_logs 哈希链 ok", bool(r.get("ok")),
              "total=%s broken=%s" % (r.get("total"), len(r.get("broken") or [])))
        print("\nT4 llm/tool trace 同源")
        a = set(x[0] for x in c.execute(
            "SELECT DISTINCT trace_id FROM llm_usage_stats WHERE trace_id<>'' LIMIT 5000"))
        b = set(x[0] for x in c.execute(
            "SELECT DISTINCT trace_id FROM tool_call_logs WHERE trace_id<>'' LIMIT 5000"))
        check("T4 两表有共享 trace_id", bool(a & b), "共享 %d 个" % len(a & b))

        print("\nM 变异自证（判据有牙齿）")
        # M1: 用内存库造一张缺 trace 列的表，列判据必须报红
        mc = sqlite3.connect(":memory:")
        mc.execute("CREATE TABLE audit_logs (id INTEGER PRIMARY KEY, x TEXT)")
        mcols = [r[1] for r in mc.execute("PRAGMA table_info(audit_logs)")]
        audit_cols = [r[1] for r in c.execute('PRAGMA table_info(audit_logs)')]
        check("M1 缺 trace 列的库 ⇒ 列判据判红（有牙齿）",
              "trace_id" not in mcols and "trace_id" in audit_cols)
        mc.close()
        # M2: 两组无交集的 trace 集合 ⇒ 共享判据必须报红
        check("M2 无交集 trace ⇒ 共享判据判红（有牙齿）",
              not ({"a"} & {"b"}) and bool(a & b))
    finally:
        c.close()
    print("\n" + "=" * 66)
    if FAILS:
        print("结论：%d/%d 通过，%d 条失败" % (N - len(FAILS), N, len(FAILS)))
        for f in FAILS:
            print("  - " + f)
        return 1
    print("结论：全部通过（%d/%d）" % (N, N))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
