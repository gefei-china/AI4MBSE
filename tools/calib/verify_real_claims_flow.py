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
    #   与正文**共享额度** —— 给 1500 时 reasoning_tokens=1500、`finish_reason=length`、
    #   **正文 0 字**；给 4000 才拿到 19 字正文。
    #   ⚠️ 这不是本批改动引入的缺陷，是**既有**的"思考分流额度不足"问题
    #   （MEMORY 已记过一次）。此处记下来是因为它会**伪装成"模型不遵守契约"**：
    #   正文为空 ⇒ 尾部 JSON 必然取不到 ⇒ claims 必然为空 ⇒
    #   很容易被误读成"协议注入没生效"。**先看 finish_reason 与 usage 再下结论。**
    MAXTOK = 4000
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
        from core.config import get as _cfg_get
        pipe = AgentPipeline()
        user = {"id": 1, "name": "verify"}
        _conv = sqlite3.connect(DB)
        try:
            cid = _conv.execute(
                "INSERT INTO conversations (title,intent,user_id,project_id)"
                " VALUES ('b3-verify','chat',1,'')").lastrowid
            _conv.commit()
        finally:
            _conv.close()
        goal = "分析卫星通信地面站的转发器接口设计，给出结论与建议"
        types = [e.get("type") for e in pipe.execute_stream(goal, 0, None, None, user=user)]
        run_ok = True
        n_tok = sum(1 for t in types if t == "token")
        print("  编排事件类型统计：%s"
              % {t: sum(1 for x in types if x == t) for t in set(types)})
        check("C1 真编排跑通并产出 token", n_tok > 0,
              "事件类型=%s（token 数=%d）" % (set(types), n_tok),
              note="编排未跑通时，下面的落库判据会标注「不可判定」而非「未生效」")
    except Exception as e:                                       # noqa: BLE001
        print("  编排异常：%r" % e)
        check("C1 真编排跑通并产出 token", False, "异常：%r" % e,
              note="链路本身没跑通 ⇒ C2/C3 的落库结论**不成立**，不据此判定本批改动无效")

    # C2/C3 落库核验（**只在编排真的跑过时才有意义**）
    _c2 = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
    try:
        rows = _c2.execute(
            "SELECT task_key, summary_json FROM agent_tasks"
            " WHERE summary_json IS NOT NULL AND summary_json!=''"
            " ORDER BY id DESC LIMIT 20").fetchall()
        n_with_claims = 0
        for _tk, sj in rows:
            try:
                d = json.loads(sj or "{}")
            except Exception:
                continue
            if d.get("claims"):
                n_with_claims += 1
        print("  agent_tasks 近 %d 条有 summary_json，其中含 claims 的 %d 条"
              % (len(rows), n_with_claims))
        if run_ok:
            check("C2 编排产出的 summary_json 含 claims", n_with_claims > 0,
                  "%d/%d 条含 claims" % (n_with_claims, len(rows)),
                  note="=0 说明模型这轮没遵守契约（prompt 注入生效与否可由 W3b 门禁证明），"
                       "**不等于**改动没生效")

        # C3 #17 的 structure_check 是否落库
        n_sc = _c2.execute(
            "SELECT COUNT(*) FROM subtask_artifacts"
            " WHERE content_json LIKE '%structure_check%'").fetchone()[0]
        print("  subtask_artifacts 含 structure_check 的行：%d" % n_sc)
        if run_ok:
            check("C3 #17 结构校验结果已落库（structure_check）", n_sc > 0,
                  "实测 %d 行" % n_sc,
                  note="=0 可能是「这轮没有 report/requirement/doc 类交付物」，"
                       "**不能**据此判定校验器没接线")

        # C4 #15 的 conflicts 口径是否落库
        n_cf = _c2.execute(
            "SELECT COUNT(*) FROM messages WHERE card_data LIKE '%\"conflicts\"%'"
        ).fetchone()[0]
        print("  messages.card_data 含 conflicts 的行：%d" % n_cf)
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
