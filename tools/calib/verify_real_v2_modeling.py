# -*- coding: utf-8 -*-
"""真实数据验证：在 AI 建模入口跑一次**真实的 SysML v2 建模**。

**米爸要求**：「先完成批次二遗留四项，并进行真实数据验证，在 AI 建模入口走真实的 V2 建模」。

**这个脚本做什么**（不是 mock、不是夹具）：
1. 复制生产库到临时库（**生产库全程只读**）；
2. 走**真实 LLM**（deepseek-v4-flash，已实测可用）让「状态机视图生成」Agent
   产出一段 SysML v2 状态机建模文本；
3. 走**真实校验器**（`sysml_v2_validate`，本仓 checker.jar 通道）校验这段文本；
4. 再走**真实编排**（`AgentPipeline.execute_stream`）触发一次编排，
   验证本轮三项改动在**真实流量**下成立：
   - #10：事件流里 `kind:"progress"` 的系统提示与真 CoT 确实分流
   - #11：`orch_stream_events` 真落了行，且 `run_id` 是**真批次号**（不是 0）
   - #12：`tool_call_logs` 真落了行，且 `run_id`/`task_key` 有值、`failure_kind` 只在失败时写

**为什么在临时库上跑**：真实建模会写 entities / graph / 产物，
这些是**不可逆的建模动作**，不能在生产库上做实验。
⇒ 临时库 = 同一份数据与同一份代码，唯一差别是 DB 路径。
**这正是「真实数据验证」与「离线门禁」的分界**：门禁证明逻辑对，这个证明真流量下也对。
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time

# ⚠️ 定位仓库根**不要 dirname 数层数**（本会话已因此栽过两次：
#   tools/verify 与 tools/calib 都是两层深，我按"一层"改回去直接指向 tools/）。
# 改为**向上找标志文件**（memory_service.py 在仓库根）。
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
WORK = tempfile.mkdtemp(prefix="v2g_real_")
DB = os.path.join(WORK, "run.db")

# ⚠️ 必须在 import core.config / database 之前设好 —— 那两个模块在 import 时读路径
os.environ["MBSE_DB_PATH"] = DB

_results = []


def check(name, ok, detail=""):
    _results.append((name, bool(ok), detail))
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name,
                           ("  ← " + str(detail)) if (detail and not ok) else ""))
    return bool(ok)


def section(t):
    print("\n=== %s ===" % t)


def copy_db():
    """整库复制（WAL 库必须用 backup()，shutil.copy2 会漏 -wal 内容）。"""
    s = sqlite3.connect("file:%s?mode=ro" % PROD_DB, uri=True)
    d = sqlite3.connect(DB)
    s.backup(d)
    d.close()
    s.close()


def main():
    print("=" * 62)
    print("真实 SysML v2 建模验证（临时库，生产库只读）")
    print("=" * 62)
    print("  库副本: %s" % DB)
    print("  生产库: %s（**全程只读**）" % PROD_DB)

    copy_db()
    # 补跑列迁移（新表/新列在生产库还没迁移过 —— 生产库迁移要靠服务重启触发）
    from database.migrations.columns import _migrate_columns
    _c = sqlite3.connect(DB)
    _migrate_columns(_c)
    _c.execute("""CREATE TABLE IF NOT EXISTS orch_stream_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id INTEGER DEFAULT 0,
        run_id INTEGER DEFAULT 0, task_key TEXT DEFAULT '', event_type TEXT DEFAULT '',
        payload_json TEXT DEFAULT '{}', created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
    _c.execute("CREATE INDEX IF NOT EXISTS idx_ose_conv_run "
               "ON orch_stream_events(conversation_id, run_id)")
    _c.commit()
    _cols = [r[1] for r in _c.execute("PRAGMA table_info(tool_call_logs)")]
    check("0 临时库已补齐三列",
          all(x in _cols for x in ("run_id", "task_key", "failure_kind")),
          "缺: %s" % [x for x in ("run_id", "task_key", "failure_kind") if x not in _cols])
    _c.close()

    # ─────────────────────────────────────────────────────────────
    section("1. 真实 LLM：让「状态机视图生成」产出 SysML v2 状态机")
    from llm import llm_client
    t0 = time.time()
    prompt = (
        "你是 SysML v2 建模专家。请为一个「卫星通信地面站」系统写状态机视图的 SysML v2 文本。\n"
        "要求：\n"
        "1. 只输出 SysML v2 代码块，不要解释文字；\n"
        "2. 包含 package、part def（地面站设备）、state machine def；\n"
        "3. 状态至少 4 个（初始化/自检/跟踪/待机），含 2 个以上 transition；\n"
        "4. 语法必须是 SysML v2（不是 v1）。"
    )
    sysml_text = ""
    provider = "-"
    # ⚠️ **必须重试**：2026-10-13 实测 deepseek-v4-flash 偶发把 max_tokens 全部烧在
    #   `reasoning_content` 上 ⇒ `content` 返回空字符串（finish_reason 仍是 stop，
    #   **不报错**）。同一 prompt 随后连跑 5 次全部正常 ⇒ 是**偶发**而非必然。
    #   这个形态与本仓已记的「思考模型 reasoning 与正文共享 max_tokens 配额」一致。
    #   ⇒ 这里重试 3 次并把每次的长度都记下来，让"偶发"在证据里可见（而不是重试到成功就装作没发生）。
    _tries = []
    for _i in range(3):
        try:
            r = llm_client.chat([{"role": "user", "content": prompt}],
                                max_tokens=1600, _intent="chat_answer")
            ch = (r.get("choices") or [{}])[0]
            _msg = ch.get("message") or {}
            _c = _msg.get("content") or ""
            _tries.append((len(_c), len(_msg.get("reasoning_content") or ""),
                           ch.get("finish_reason")))
            provider = (r.get("_meta") or {}).get("provider", "-")
            if _c.strip():
                sysml_text = _c
                break
        except Exception as e:
            _tries.append(("ERR:%s" % type(e).__name__, 0, str(e)[:60]))
    print("  各次尝试 (content长度, reasoning长度, finish_reason): %s" % _tries)
    check("1.1 ★ LLM 真调用成功（非 mock）",
          bool(sysml_text.strip()),
          "返回空 —— provider=%s" % provider)
    check("1.2 返回里有 SysML v2 关键字",
          any(k in sysml_text for k in ("state machine", "StateMachine", "part def", "Package")),
          "前 160 字: %r" % sysml_text[:160])
    print("  provider=%s｜耗时 %.1fs｜长度 %d 字"
          % (provider, time.time() - t0, len(sysml_text)))
    print("  ---- 产物节选 ----")
    for ln in [x for x in sysml_text.split("\n") if x.strip()][:8]:
        print("    | " + ln[:96])

    # ─────────────────────────────────────────────────────────────
    section("2. 真实校验器：用 checker.jar 校验这段 SysML v2")
    # 走项目里**现成的**校验器模块（仓库根 sysml_v2_check.py，checker.jar 通道）——
    # 不自己拼 java 命令行，那会与门禁 / 生产走两套口径。
    try:
        import sysml_v2_check as CK
        if not CK.available():
            check("2.0 校验器可用（checker.jar 在位）", False, "available()=False")
        else:
            check("2.0 ★ 校验器可用（checker.jar 在位）", True)
        vres = CK.check_code(sysml_text)
        vverdict = CK.verdict(vres)
        _errs = CK.diagnostics(vres, max_items=8) or []
        print("  verdict = %s（pass / report / block / unavailable）" % vverdict)
        print("  ok=%s rc=%s 词法=%s 语法=%s 语义=%s"
              % (vres.get("ok"), vres.get("rc"), vres.get("n_lexical"),
                 vres.get("n_syntax"), vres.get("n_semantic")))
        for e in _errs[:6]:
            print("    - %s" % str(e)[:110])
        check("2.1 ★ 真实校验器给出了明确判定（不是静默 unavailable）",
              vverdict in ("pass", "report", "block"), "verdict=%s" % vverdict)
        # 不把「模型产出有语法错」当失败 —— 那是**被校验器如实报出来**，
        # 正是这一层该干的事。失败只落在「校验器没跑起来」。
        print("  ⇒ 模型产出的这段文本 %s；校验器已如实报出结论。"
              % ("通过校验" if vverdict == "pass" else "有错（已被校验器捕获）"))
    except Exception as e:
        check("2.0 校验器调用成功", False, "%s %s" % (type(e).__name__, str(e)[:140]))

    section("3. 真实编排：跑一次 execute_stream（验证 #10/#11/#12）")
    sys.path.insert(0, ROOT)
    from agent import get_pipeline
    pipe = get_pipeline()

    # 造一个干净会话（临时库）
    c = sqlite3.connect(DB)
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    _cur = c.execute("INSERT INTO conversations (title, user_id, created_at, updated_at) "
                     "VALUES (?,?,?,?)", ("真实V2建模验证", 1, now, now))
    conv_id = _cur.lastrowid   # ⚠️ lastrowid 在 **cursor** 上，Connection 没有该属性
    c.commit()
    c.close()
    print("  临时会话 #%d" % conv_id)

    events = []
    done_payload = None
    err = None
    try:
        # ⚠️ 请求文本要**命中编排触发规则**（`orchestration.py:250 _needs_orchestration`）：
        #    需「两个 Agent 域关键词 + 并列连词（和/与/并/且/以及）」，或「先…再…」。
        #    上一版只写"状态机视图"（单域）⇒ 走**单 Agent 直行** ⇒ 事件里 progress=0、
        #    run_id=0，#10/#11/#12 全都没被真流量覆盖（**看起来像改动没生效，其实是流量不对**）。
        _REQ = ("为卫星通信地面站做**需求分析**和**工程建模**："
                "先给出需求条目，再生成 SysML v2 状态机视图，并输出建模报告")
        print("  请求: %s" % _REQ)
        for ev in pipe.execute_stream(
                _REQ, conv_id, branch="dev", user=None):
            et = ev.get("type")
            events.append(ev)
            if et == "done":
                done_payload = (ev.get("data") or {})
            if et == "error":
                err = ev.get("message")
            # 编排可能要跑几分钟；设个上限防挂死
            if time.time() - t0 > 900:
                print("  [WARN] 超时 900s，中止")
                break
    except Exception as e:
        err = "%s: %s" % (type(e).__name__, str(e)[:160])

    n_by_type = {}
    for e in events:
        n_by_type[e.get("type")] = n_by_type.get(e.get("type"), 0) + 1
    print("  事件总数 %d｜分布: %s" % (len(events), n_by_type))
    if err:
        print("  [INFO] 流内 error: %s" % str(err)[:160])

    # 3.1 #10：kind 分流
    section("3.1 #10 reasoning 的 kind 分流（真实流量）")
    reas = [e for e in events if e.get("type") == "reasoning"]
    prog = [e for e in reas if e.get("kind") == "progress"]
    cot = [e for e in reas if e.get("kind") != "progress"]
    print("  reasoning 共 %d 条｜progress %d 条｜cot %d 条"
          % (len(reas), len(prog), len(cot)))
    for e in prog[:3]:
        print("    [progress] %s" % str(e.get("delta"))[:80])
    for e in cot[:2]:
        print("    [cot     ] %s" % str(e.get("delta"))[:80])
    check("3.1.1 ★ 真流量里出现了 progress 类事件（说明有编排发生）",
          len(prog) > 0, "progress=0 ⇒ 这次没走编排，#10 未被真流量覆盖")
    check("3.1.2 progress 与 CoT 是两类（没混在一起）",
          len(reas) == len(prog) + len(cot))
    # ⚠️ 这条在 progress=0 时是**空集合恒真** ⇒ 必须先咬住"确实有对象可判"。
    #    （本会话已两次栽在空集合恒真上：门禁 D 组、以及这里的真实验证。）
    check("3.1.3a ★ 确实有 progress 事件可判（防空集合恒真）", len(prog) > 0,
          "progress=0 ⇒ 这条判据与 3.1.3 都是空转，必须先让请求真的走编排")
    if prog:
        _sys_kw = ("（自动编排）", "（断点续跑）", "（知识库范围）")
        check("3.1.3b progress 事件的文案确实是系统提示",
              all(any(k in str(e.get("delta") or "") for k in _sys_kw) for e in prog),
              "有 progress 但文案不是系统提示 ⇒ 分类错: %s"
              % [str(e.get("delta"))[:40] for e in prog[:2]])
        # 反向对照：CoT 里**不该**出现系统前缀（否则 #10 的分流没真正分开）
        _cot_sys = [e for e in cot
                    if any(k in str(e.get("delta") or "") for k in _sys_kw)]
        check("3.1.3c ★ CoT 里不含系统前缀（两类真的分开了）",
              not _cot_sys, "漏了 %d 条" % len(_cot_sys))

    # 3.2 #11：事件落库
    section("3.2 #11 事件落库（真实流量）")
    # 复刻路由层的缓冲刷库（路由本身要 HTTP，这里复刻其**完全相同**的逻辑）
    from services.stream_event_recorder import parse_frame, record
    import core.sse as S
    buf, run_id = [], 0
    for e in events:
        f = S.to_sse_frame(e)
        et, pl = parse_frame(f)
        if et == "done":
            run_id = int((pl.get("data") or {}).get("orch_run_id") or 0)
        elif et in ("stage", "tool"):
            buf.append(f)
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    with c:
        for f in buf:
            record(c, conv_id, f, run_id=run_id, max_per_run=200)
    rows = c.execute("SELECT event_type, run_id, task_key FROM orch_stream_events "
                     "WHERE conversation_id=? ORDER BY id", (conv_id,)).fetchall()
    print("  缓冲 %d 帧｜落库 %d 行｜run_id=%s" % (len(buf), len(rows), run_id))
    for r in rows[:6]:
        print("    type=%-7s run_id=%-5s" % (r["event_type"], r["run_id"]))
    check("3.2.1 ★ 真流量下落库成功", len(rows) > 0, "落库 0 行")
    check("3.2.2 ★ 落库行带真批次号（不是 0）",
          bool(rows) and all(r["run_id"] == run_id and run_id > 0 for r in rows),
          "run_ids=%s" % [r["run_id"] for r in rows[:6]])
    _got_types = set(r["event_type"] for r in rows)
    check("3.2.3 只有 stage/tool 两类（token/reasoning 不灌库）",
          _got_types.issubset({"stage", "tool"}),
          "混进了: %s" % (_got_types - {"stage", "tool"}))

    # 3.3 #12：工具调用归因
    section("3.3 #12 工具调用归因（真实流量）")
    tcl = c.execute("SELECT COUNT(*) n, SUM(CASE WHEN run_id>0 THEN 1 ELSE 0 END) withrun "
                    "FROM tool_call_logs").fetchone()
    print("  tool_call_logs 总 %d 行｜其中 run_id>0 的 %d 行" % (tcl["n"], tcl["withrun"]))
    sample = c.execute("SELECT tool_name, run_id, task_key, failure_kind, ok "
                       "FROM tool_call_logs WHERE run_id>0 ORDER BY id DESC LIMIT 6").fetchall()
    for r in sample:
        print("    %-26s run=%-5s task=%-6s fk=%-10s ok=%s"
              % (r["tool_name"], r["run_id"], r["task_key"] or "-",
                 r["failure_kind"] or "-", r["ok"]))
    check("3.3.1 ★ 真流量下有工具调用带上了 run_id",
          (tcl["withrun"] or 0) > 0,
          "withrun=0 ⇒ 这次编排没调任何工具，#12 未被真流量覆盖")
    check("3.3.2 failure_kind 只在失败时写",
          all((not r["ok"]) or (not r["failure_kind"]) for r in sample),
          "成功行也带了 failure_kind")

    # 3.4 agent_tasks 与 run_id 对齐
    section("3.4 归因链闭合：事件/工具/任务指向同一批次")
    at = c.execute("SELECT COUNT(*) n FROM agent_tasks WHERE run_id=?", (run_id,)).fetchall()
    print("  本次 run_id=%s｜agent_tasks 命中 %s 条" % (run_id, at[0][0] if at else 0))
    if run_id:
        for r in c.execute("SELECT task_key, status, error_class FROM agent_tasks "
                           "WHERE run_id=? LIMIT 5", (run_id,)):
            print("    %-6s %-8s %s" % (r["task_key"], r["status"], r["error_class"] or "-"))
        check("3.4.1 ★ run_id 能把事件/工具/任务串成一条链",
              (at[0][0] if at else 0) > 0,
              "agent_tasks 在该 run_id 下 0 条 ⇒ 归因链断了")

    # 3.5 done 事件带 orch_run_id
    section("3.5 done 事件带 orch_run_id（#11 的前提）")
    check("3.5.1 done.data.orch_run_id 存在且为正",
          isinstance(done_payload, dict) and int(done_payload.get("orch_run_id") or 0) > 0,
          "orch_run_id=%r" % (done_payload or {}).get("orch_run_id") if done_payload else "无 done")
    if isinstance(done_payload, dict):
        print("  orchestrated=%s｜plan=%d 条｜card 字段 %d 个"
              % (done_payload.get("orchestrated"),
                 len(done_payload.get("plan") or []),
                 len(done_payload.get("card") or {})))

    c.close()

    # ─────────────────────────────────────────────────────────────
    section("4. 产物证据")
    if isinstance(done_payload, dict):
        cd = done_payload.get("card") or {}
        print("  card.orchestrated_status = %s" % cd.get("orchestrated_status"))
        print("  card.saved_flow_id      = %s" % cd.get("saved_flow_id"))
        for k in ("quality_gate_gaps", "reflection"):
            v = cd.get(k)
            if v:
                print("  card.%-18s = %s" % (k, json.dumps(v, ensure_ascii=False)[:120]))
        sv = done_payload.get("sysml_views")
        if sv:
            print("  sysml_views: %s" % json.dumps(sv, ensure_ascii=False)[:200])

    # 汇总
    print("\n" + "=" * 62)
    _p = sum(1 for _, o, _ in _results if o)
    _f = len(_results) - _p
    print("真实建模验证：%d 条，%d 通过 / %d 失败" % (len(_results), _p, _f))
    for _n, _o, _d in _results:
        if not _o:
            print("  - FAIL %s  ← %s" % (_n, _d))
    print("=" * 62)
    print("\nSysML v2 产物（模型真产出，节选 24 行）：")
    for ln in [x for x in sysml_text.split("\n") if x.strip()][:24]:
        print("  | " + ln[:100])
    return 0 if _f == 0 else 1


if __name__ == "__main__":
    try:
        rc = main()
    finally:
        shutil.rmtree(WORK, ignore_errors=True)
    sys.exit(rc)
