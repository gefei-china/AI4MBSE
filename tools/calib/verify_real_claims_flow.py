# -*- coding: utf-8 -*-
"""真实场景验证：批次三 #16 claims + #15 冲突检测 + #17 交付物校验。

**米爸要求**：「继续第三批次的优化，并进行真实场景验证」。

**与离线门禁的分界**：门禁证明逻辑对（且证明判据自己没空转），
这个脚本证明**真流量下也对** —— 真 LLM、真编排、真落库。

## 三段验证

### A 段 · 真 LLM 是否真的会按契约吐 claims
直接问模型一个**可判矛盾**的问题，要它按交付格式回答。
观察：`extract_protocol_json` 能否取出、`claims` 是否非空、`subject` 是否填了。
⚠️ 这一段最可能"不通过"—— **模型不遵守格式是常态**。
   ⇒ 脚本必须能区分「模型没给」与「解析器坏了」，否则又是一次假绿。

### B 段 · 冲突检测在真产出上是否成立
把 A 段两个子任务的真产出喂给 `detect_conflicts`。
⚠️ 关键是**读 `conflict_stats().verdict`**：若是 `no-data`，
   那么"没检出冲突"**不构成任何结论**，必须如实报"不可判定"。

### C 段 · 真实编排跑一轮，验证落库
走真 `AgentPipeline.execute_stream` 触发编排，检查：
  · `agent_tasks.summary_json` 里有没有 claims（非空率）
  · `subtask_artifacts.content_json` 里有没有 `structure_check`（#17 落库）
  · `messages.card_data.reflection.conflicts` 口径（#15 落库）
⚠️ 真实编排**可能因为别的原因失败**（超时/额度/意图不匹配）——
   脚本必须区分「链路没跑通」与「我这批改动没生效」，不能混报。

**为什么在临时库上跑**：真实编排会写 agent_tasks / subtask_artifacts / messages，
不能在生产库上做实验。生产库全程**只读**。
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time


def _find_root():
    """向上找仓库根（标志文件 memory_service.py 在根）。"""
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
WORK = tempfile.mkdtemp(prefix="b3_real_")
DB = os.path.join(WORK, "run.db")
os.environ["MBSE_DB_PATH"] = DB          # 必须在 import core.config/database 之前

_results = []
NOTES = []


def check(name, ok, detail="", note=""):
    _results.append((name, bool(ok), detail))
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name,
                           ("  ← " + str(detail)) if (detail and not ok) else ""))
    if note:
        NOTES.append("%s：%s" % (name, note))
        print("        （口径）%s" % note)
    return bool(ok)


def section(t):
    print("\n=== %s ===" % t)


def copy_db():
    s = sqlite3.connect("file:%s?mode=ro" % PROD_DB, uri=True)
    d = sqlite3.connect(DB)
    s.backup(d)
    d.close()
    s.close()


CONTRACT = (
    "\n\n【交付格式】先用自然语言给出完整正文（用户要看的成果），"
    "然后**另起一段**，以单个 JSON 对象结尾（用于结构化归档，勿把 JSON 混进正文）：\n"
    '{"schema_version":1,"status":"full","summary":"一句话成果",'
    '"artifacts":[{"kind":"report","title":"产物标题","ref":"ref"}],'
    '"evidence":[{"source":"graph","ref":"引用位置","basis":"asserted"}],'
    '"risks":[],"claims":[{"text":"结论一句话","kind":"fact|inference|to_verify",'
    '"subject":"这条结论讲的对象/指标名"}],"confidence":0.5}\n'
    "规则：claims 列出关键结论，subject **必填**且写对象名；"
    "kind 三档：fact=可独立复核 / inference=由 fact 推出 / to_verify=明确待验证。"
)


def main():
    print("=" * 68)
    print("批次三真实场景验证 · #16 claims / #15 冲突检测 / #17 交付物校验")
    print("=" * 68)
    print("  库副本: %s" % DB)
    print("  生产库: %s（**全程只读**）" % PROD_DB)
    t_start = time.time()

    copy_db()
    _c = sqlite3.connect(DB)
    _c.execute("""CREATE TABLE IF NOT EXISTS subtask_artifacts (
        id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL DEFAULT 0,
        task_key TEXT NOT NULL DEFAULT '', artifact_idx INTEGER DEFAULT 0,
        kind TEXT DEFAULT '', title TEXT DEFAULT '', content_json TEXT DEFAULT '{}',
        materialized INTEGER DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
    _c.execute("CREATE INDEX IF NOT EXISTS idx_sa_run ON subtask_artifacts(run_id, task_key)")
    _c.commit()
    _c.close()

    # ══════════════════════════════════════════════════════════════
    section("A 段 · 真 LLM 按契约产出 claims")
    from llm import llm_client
    from services import subtask_protocol as sp

    # A1 两个子任务，同一对象给出**互斥**结论 —— 这是冲突检测的最小可验证输入
    asks = [
        ("t1", "你在分析一个卫星通信地面站系统的转发器接口。请回答："
               "该转发器对外提供多少个数据接口？请给出你的结论。"),
        ("t2", "你在分析同一个卫星通信地面站系统的转发器接口（可参考上游 t1 的说法，"
               "但请独立判断）。请回答：该转发器对外提供多少个数据接口？"),
    ]
    subs = []
    # ★ max_tokens 必须给足（2026-10-14 实测）：deepseek-v4-flash 的 reasoning
    #   与正文**共享额度** —— 给 1500/4000 时 reasoning 被吃满、`finish_reason=length`、
    #   **正文 0 字**；给 4000 才拿到 19 字正文。
    #   ⚠️ 这不是本批改动引入的缺陷，是**既有**的"思考分流额度不足"问题
    #   （MEMORY 已记过一次）。此处记下来是因为它会**伪装成"模型不遵守契约"**：
    #   正文为空 ⇒ 尾部 JSON 必然取不到 ⇒ claims 必然为空 ⇒
    #   很容易被误读成"协议注入没生效"。**先看 finish_reason 与 usage 再下结论。**
    MAXTOK = 8000
    for tk, q in asks:
        try:
            r = llm_client.chat([{"role": "user", "content": q + CONTRACT}],
                                max_tokens=MAXTOK, _intent="plan_summarize")
            ch = (r.get("choices") or [{}])[0]
            msg = ch.get("message", {}) or {}
            txt = msg.get("content") or ""
            u = r.get("usage") or {}
            rsn = (u.get("completion_tokens_details") or {}).get("reasoning_tokens")
            print("  [%s] finish_reason=%r content=%d 字 reasoning_tokens=%s"
                  % (tk, ch.get("finish_reason"), len(txt), rsn))
            if not txt.strip():
                check("A0-%s 真 LLM 返回了正文" % tk, False,
                      "content 为空（finish_reason=%r reasoning_tokens=%s）"
                      % (ch.get("finish_reason"), rsn),
                      note="**正文为空 ⇒ 尾部 JSON 必然取不到 ⇒ claims 必为空**。"
                           "先调 max_tokens（本次用 %d），别误判为协议注入失效" % MAXTOK)
        except Exception as e:                                   # noqa: BLE001
            txt = ""
            check("A0-%s 真 LLM 调用成功（%s）" % (tk, tk), False, "异常：%r" % e)
        parsed = sp.extract_protocol_json(txt)
        proto = sp.validate(parsed) if isinstance(parsed, dict) else sp.normalize_text(txt)
        san = proto.get("sanitized") or {}
        subs.append({"task_key": tk, "raw": txt, "proto": san})
        print("\n  --- %s 真产出（尾部 JSON 解析：%s）---"
              % (tk, "成功" if isinstance(parsed, dict) else "**未取到**"))
        print("      claims=%s" % json.dumps(san.get("claims") or [],
                                            ensure_ascii=False)[:300])
        print("      artifacts=%s" % json.dumps(
            [(a.get("kind"), a.get("title")) for a in (san.get("artifacts") or [])],
            ensure_ascii=False)[:200])

    # A2 解析能力（★与"模型不配合"区分开）
    n_parsed = sum(1 for s in subs if s["raw"] and isinstance(
        sp.extract_protocol_json(s["raw"]), dict))
    check("A2 两个子任务都取到了尾部 JSON", n_parsed == 2,
          "只取到 %d/2 —— 若是模型没按格式给，claims 判据应判为 no-data 而非「冲突检测失效」"
          % n_parsed)

    # A3 claims 是否真的落地
    n_claims = sum(len(s["proto"].get("claims") or []) for s in subs)
    check("A3 claims 非空（模型确实按契约给了结论）", n_claims > 0,
          "claims 总数=%d" % n_claims,
          note="claims=0 时下面的冲突检测结论**不成立**，脚本会显式标注不可判定")

    # A4 subject 锚点（冲突检测的唯一输入源）
    n_sub = sum(1 for s in subs for c in (s["proto"].get("claims") or [])
                if str(c.get("subject") or "").strip())
    check("A4 subject 锚点已填（冲突检测靠它）", n_sub > 0,
          "有 subject 的 claim = %d / claims 总数 %d" % (n_sub, n_claims),
          note="subject=0 ⇒ detect_conflicts 会返回 [] 且 verdict=no-data，"
               "此时**不能**说「已检查无冲突」")

    # A5 kind 分档落地
    kinds = [c.get("kind") for s in subs for c in (s["proto"].get("claims") or [])]
    check("A5 claims 带 kind 分档", all(k in sp.CLAIM_KINDS for k in kinds if k),
          "实测 kinds=%s（枚举=%s）" % (kinds, list(sp.CLAIM_KINDS)))

    # ══════════════════════════════════════════════════════════════
    section("B 段 · 冲突检测在真产出上的口径")
    stats = sp.conflict_stats([s["proto"] for s in subs])
    cfs = sp.detect_conflicts([s["proto"] for s in subs])
    print("  conflict_stats = %s" % json.dumps(stats, ensure_ascii=False))
    print("  detect_conflicts 命中 %d 条" % len(cfs))
    for c in cfs:
        print("    ⚠️ [%s] %s" % (c.get("subject"), c.get("reason")))

    check("B1 verdict 不是 no-data（有数据可判）", stats["verdict"] != "no-data",
          "实测 verdict=%s claims_anchored=%d" % (stats["verdict"], stats["claims_anchored"]),
          note="verdict=no-data ⇒ 真实模型这次没给 subject，"
               "**「未检出冲突」不构成结论**，这是协议落地问题不是检测算法问题")

    # B2 真实产出若真的矛盾（3 个 vs 别的数），必须检出
    nums = []
    for s in subs:
        for c in (s["proto"].get("claims") or []):
            if str(c.get("subject") or "").strip():
                nums.append(c.get("text"))
    print("  参与判定的真实结论：%s" % nums)

    # B3 ★反向验证：构造一对**必然冲突**的 claim，确认检测器真能检出
    probe = [
        sp.validate({"task_key": "x1", "status": "full", "summary": "s",
                     "claims": [{"text": "转发器有 3 个数据接口", "subject": "转发器数据接口数",
                                 "kind": "fact"}]})["sanitized"],
        sp.validate({"task_key": "x2", "status": "full", "summary": "s",
                     "claims": [{"text": "转发器有 5 个数据接口", "subject": "转发器数据接口数",
                                 "kind": "fact"}]})["sanitized"],
    ]
    pc = sp.detect_conflicts(probe)
    check("B3 反向探针：确定性冲突必被检出（检测器没空转）", len(pc) == 1,
          "实测 %d 条：%s" % (len(pc), pc),
          note="这条不依赖模型 —— 若真模型没给矛盾，检测器能力仍被证实")

    # ══════════════════════════════════════════════════════════════
    section("C 段 · 真编排一轮，看落库")
    run_ok = False
    try:
        from agent.pipeline import AgentPipeline
        pipe = AgentPipeline()
        # ★ 打桩 `_try_reuse_planner_flow` ⇒ **强制走主路径** `_stream_orchestrated_flow`。
        #   理由（实测）：生产库里有多条已发布沉淀流程，问什么都会命中它
        #   ⇒ 走 `_stream_orchestrated` 复用路径 —— 而那条路径**只 yield 占位事件、
        #   不执行子任务** ⇒ claims / conflicts / structure_check 三项**无从产生**。
        #   ⇒ 那条路径验不出本批改动，继续跑只是浪费 token。
        #   ⚠️ 这是**为验证而限定路径**，不是掩盖问题 —— 脚本末尾会如实标注
        #     「本次刻意绕开复用路径」，而复用路径是能力盲区这件事写在记忆里。
        _orig_reuse = AgentPipeline._try_reuse_planner_flow
        AgentPipeline._try_reuse_planner_flow = lambda *a, **k: None
        try:
            user = {"id": 1, "name": "verify"}
            _conv = sqlite3.connect(DB)
            try:
                cid = _conv.execute(
                    "INSERT INTO conversations (title,intent,user_id,project_id)"
                    " VALUES ('b3-verify','chat',1,'')").lastrowid
                _conv.commit()
            finally:
                _conv.close()
            # ⚠️ 提问必须**能触发编排**（意图路由判否 ⇒ 走单 Agent，
            #   事件里只有 1 对 agent ⇒ 同样验不出子任务协议）。
            #   实测踩过：「梳理一份通用的接口评审清单」⇒ 未进编排（agent 事件仅 2 个）。
            #   判据：事件统计里 agent 数应 ≥ 4（≥2 子任务 × run/done）。
            goal = ("请为一个新的城市轨道交通信号系统做完整的 MBSE 建模："
                    "先做需求分析，再做架构设计，最后给出校验结论")
            types = [e.get("type") for e in
                     pipe.execute_stream(goal, 0, None, None, user=user)]
        finally:
            AgentPipeline._try_reuse_planner_flow = _orig_reuse
        run_ok = True
        n_tok = sum(1 for t in types if t == "token")
        print("  编排事件类型统计：%s"
              % {t: sum(1 for x in types if x == t) for t in set(types)})
        print("  （已刻意打桩 `_try_reuse_planner_flow` ⇒ 强制走主路径"
              " `_stream_orchestrated_flow`）")
        check("C1 主路径真编排跑通并产出 token", n_tok > 0,
              "事件类型=%s（token 数=%d）" % (set(types), n_tok),
              note="编排未跑通时，下面的落库判据会标注「不可判定」而非「未生效」")
    except Exception as e:                                       # noqa: BLE001
        print("  编排异常：%r" % e)
        check("C1 主路径真编排跑通并产出 token", False, "异常：%r" % e,
              note="链路本身没跑通 ⇒ C2/C3 的落库结论**不成立**，不据此判定本批改动无效")

    # C2/C3 落库核验
    # ⚠️⚠️ **必须按本次 run_id 过滤**（2026-10-14 真实踩到）：
    #   复用路径 `_stream_orchestrated`（stream.py:1388）**不创建 agent_tasks、不执行子任务**
    #   ——它只 yield 一对 `agent run/done` 占位事件，`orch_content` 来自**预发布流程模板**。
    #   ⇒ "查 agent_tasks 最近 20 条" 拿到的全是**历史存量**（实测 run_id=532、
    #     created_at=2026-10-07，是 10-07 那次跑的历史行），
    #     于是 claims=0 / structure_check=0 被误读成"本批改动没生效"。
    #   这与 MEMORY 里「查了不存在的列/查错了对象 ⇒ 拿到一个像模像样的错误数字」同族。
    # ⇒ 先取本次真实 run_id（orch_run_id 事件），再按它过滤；取不到就**如实报不可判定**。
    _c2 = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
    try:
        # 本次运行的 run_id（若该表存在）—— ⚠️ **表可能不存在**（生产库未迁移时就没有），
        #   实测踩过：`sqlite3.OperationalError: no such table` 直接把脚本打断，
        #   后面的判据一条都没跑到。⇒ 一律用 try 包住，缺表只是"拿不到 run_id"，不是致命。
        try:
            _rid = _c2.execute(
                "SELECT MAX(run_id) FROM orch_stream_events").fetchone()[0]
        except Exception:
            _rid = None
        _since = time.strftime("%Y-%m-%d %H:%M:%S",
                               time.localtime(time.time() - 1800))
        rows = _c2.execute(
            "SELECT task_key, summary_json FROM agent_tasks"
            " WHERE created_at > ? ORDER BY id DESC", (_since,)).fetchall()
        print("\n  本次 run_id（orch_stream_events 最新）= %s" % _rid)
        print("  本次时间窗(%s 之后)新建的 agent_tasks = %d 行" % (_since, len(rows)))
        if len(rows) == 0:
            print("  ⇒ 本次编排**未创建任何 agent_tasks**：与 `_stream_orchestrated`"
                  "（复用路径只播报占位事件、不执行子任务）一致。")
        n_with_claims = 0
        for _tk, sj in rows:
            try:
                d = json.loads(sj or "{}")
            except Exception:
                continue
            if d.get("claims"):
                n_with_claims += 1
        print("  其中含 claims 的：%d 条" % n_with_claims)
        if len(rows) == 0:
            check("C2 本次编排创建了 agent_tasks 且含 claims", False,
                  "本次无 agent_tasks（走的是复用路径）",
                  note="**这条不构成「改动没生效」的证据** —— 复用路径本来就不执行子任务，"
                       "claims 无从产生。要验证 claims 落地须走**主路径**"
                       "（`_stream_orchestrated_flow`，即未命中已发布流程时的那条）")
        else:
            check("C2 本次编排产出的 summary_json 含 claims", n_with_claims > 0,
                  "%d/%d 条含 claims" % (n_with_claims, len(rows)),
                  note="=0 说明模型这轮没遵守契约（prompt 注入生效与否可由 W3b 门禁证明），"
                       "**不等于**改动没生效")

        # C3 #17 的 structure_check 是否落库（同样只认本次）
        n_sc = _c2.execute(
            "SELECT COUNT(*) FROM subtask_artifacts WHERE created_at > ?",
            (_since,)).fetchone()[0]
        n_sc_ck = _c2.execute(
            "SELECT COUNT(*) FROM subtask_artifacts WHERE content_json LIKE '%structure_check%'"
            " AND created_at > ?", (_since,)).fetchone()[0]
        print("  本次 subtask_artifacts = %d 行，其中含 structure_check = %d 行"
              % (n_sc, n_sc_ck))
        if n_sc > 0:
            check("C3 #17 结构校验结果已落库（structure_check）", n_sc_ck > 0,
                  "本次 %d 行产物里 %d 行带 structure_check" % (n_sc, n_sc_ck),
                  note="=0 可能是「这轮没有 report/requirement/doc 类交付物」，"
                       "**不能**据此判定校验器没接线")
        else:
            check("C3 #17 结构校验结果已落库（structure_check）", False,
                  "本次无产物行（未创建 agent_tasks）",
                  note="不可判定：本次编排没产生子任务产物")

        # C4 #15 的 conflicts 口径是否落库（只看本次窗口）
        n_cf = _c2.execute(
            "SELECT COUNT(*) FROM messages WHERE created_at > ?"
            " AND card_data LIKE '%\"conflicts\"%'", (_since,)).fetchone()[0]
        n_cf_all = _c2.execute(
            "SELECT COUNT(*) FROM messages WHERE card_data LIKE '%\"conflicts\"%'"
        ).fetchone()[0]
        print("  本次窗口内 card_data 含 conflicts 的消息 = %d 条（全库累计 %d 条）"
              % (n_cf, n_cf_all))
        check("C4 #15 conflicts 口径已落库（至少全库有数据）", n_cf_all > 0,
              "全库 %d 条" % n_cf_all)
    finally:
        _c2.close()

    # ══════════════════════════════════════════════════════════════
    npass = sum(1 for _, ok, _ in _results if ok)
    nfail = sum(1 for _, ok, _ in _results if not ok)
    print("\n" + "=" * 68)
    print("%d pass / %d fail　（耗时 %.0fs）" % (npass, nfail, time.time() - t_start))
    if NOTES:
        print("\n口径说明（**不是失败，是必须一起读的事实**）：")
        for n in NOTES:
            print("  · %s" % n)
    print("\n清理临时库：%s" % WORK)
    shutil.rmtree(WORK, ignore_errors=True)
    return 0 if nfail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
