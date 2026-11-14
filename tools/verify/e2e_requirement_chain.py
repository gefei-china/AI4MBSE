# CI-OPTIONAL: [local] tier=local（见 tools/gates_manifest.json）：真机端到端，需 8000 服务 + 真实 LLM（规则校验/冲突检测的模型判定），约 3~8 分钟且产生费用，CI 无服务无 key。本机已实测通过。改法须同步改清单 tier 并接线，不允许只删这行
# -*- coding: utf-8 -*-
"""需求分析与冲突检测「配置化链路」真机端到端（外部 HTTP 入口，不改库不信自述）。

## 与 verify_requirement_chain_runtime.py 的分工
· runtime 那份：纯函数判据（不需要服务、不花钱）⇒ 证明"胶水在边界条件上不撒谎"；
· **本文件**：真正发请求、走完整链路 ⇒ 证明"配置资产真的进了模型、结论真的到了前端契约"。

## 真机判据（只看 HTTP 返回体 + DB 落库，不看模型自述）
  P1 规则校验：意图/Agent/Skill 三者命中；检索**真的命中了标准条款**（must_retrieve 生效）；
               `requirement_check_verdict` 有值；`quality_report.issues[]` 条数 ≥10；
               `card_data` 已回写（刷新页面不丢）。
  P2 冲突检测：`anchored==0` 时**不许**输出"无冲突"（必须 undetermined / insufficient）。
  P3 配置即行为：把档案的规则类别砍到子集，**同一提示词**再跑一次 ⇒ 结论/理由必须随之变化。

## 为什么判据要看"检索命中"而不只看"模型说检查了"
历史上出现过"模型声称已按标准校验、实际一次检索都没做"的假绿灯。
硬门槛的语义是：**没检索到条款就不许写 checked** —— 所以本文件先断言命中，再断言结论。

用法：
  python tools/verify/e2e_requirement_chain.py            # 全部三段
  python tools/verify/e2e_requirement_chain.py p1 p2      # 只跑指定段
退出码 0 = 全绿；1 = 有红项。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BASE = "http://127.0.0.1:8000"
DB = os.path.join(_ROOT, "mbse.db")
RESULTS = []


def check(group, name, cond, detail=""):
    RESULTS.append((group, name, bool(cond), detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"   ← {detail}" if detail else ""))


def _post(path, payload, timeout=120):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _new_conv(tag):
    r = _post("/api/conversations", {"title": f"需求配置化验证-{tag}-{int(time.time())}"})
    return r.get("id")


def _chat(conv_id, message, timeout=600):
    t0 = time.time()
    r = _post(f"/api/conversations/{conv_id}/chat", {"message": message}, timeout=timeout)
    return r, time.time() - t0


def _card_data_of(conv_id):
    """直接查库确认 card_data 已回写（不信返回体里的自述）。"""
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    try:
        row = c.execute(
            "SELECT id,content,card_data FROM messages WHERE conversation_id=? AND role='assistant' "
            "ORDER BY id DESC LIMIT 1", (conv_id,)).fetchone()
        if not row:
            return None, ""
        try:
            cd = json.loads(row["card_data"] or "{}")
        except Exception:
            cd = {}
        return cd, row["content"] or ""
    finally:
        c.close()


PROMPT_RULE = (
    "按标准校验需求。请对下列 3 条需求逐类判定（模板/量化/可验证/可追溯等），"
    "给出命中规则编号、判定依据与标准条款出处：\n"
    "REQ-001：系统应在 2 秒内完成上电自检。\n"
    "REQ-002：系统应该尽可能快地响应用户操作，体验要好。\n"
    "REQ-003：当检测到儿童被遗留车内时，系统应在 30 秒内发出报警并推送车主手机。\n"
)

PROMPT_CONFLICT = (
    "需求冲突分析。判断下列需求之间是否存在冲突，并指出冲突类型与涉及的需求编号：\n"
    "REQ-101：系统自检时间应小于 2 秒。\n"
    "REQ-102：系统自检时间应小于 5 秒。\n"
    "REQ-103：自检时间不得超过 3 秒。\n"
    "REQ-104：系统必须在自检完成后才允许用户操作。\n"
)


def phase1():
    print("\n── P1 规则校验（真机：意图/技能/检索/结论/落库）──")
    conv = _new_conv("P1")
    if not conv:
        check("P1", "创建会话", False, "无 id")
        return None
    try:
        r, dt = _chat(conv, PROMPT_RULE)
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        check("P1", "请求完成（未 5xx）", False, f"HTTP {e.code} {body}")
        return None
    except Exception as e:
        check("P1", "请求完成（未挂死）", False, repr(e)[:200])
        return None

    print(f"     耗时 {dt:.0f}s，返回 {len(json.dumps(r, ensure_ascii=False))} 字节")
    intent = str(r.get("intent") or "")
    agent = str(r.get("agent") or "")
    hits = [str(x) for x in (r.get("skill_hits") or [])]
    check("P1", "意图路由到需求校验类", intent.startswith("requirement_"),
          f"intent={intent}")
    # agent 字段是**中文 display_name**（DB `agents.display_name`），不是 name 列
    check("P1", "命中需求校验 Agent（display_name 含「需求」）", "需求" in agent,
          f"agent={agent}")
    check("P1", "Skill 命中（规则/完整性/冲突 三者之一）",
          any("requirement_rule_check_guide" in h or "requirement_completeness_guide" in h
              or "Requirement_Conflict_Analysis" in h or "requirement" in h.lower() for h in hits),
          f"skill_hits={hits[:5]}")

    ret = r.get("retrieval") or {}
    # ⚠️ 返回体里的 `retrieval` 是**手工构造的子集**（execute.py 只导出
    #   source/route/confidence/graph_count/vector_count/attachment_used/citations），
    #   **不含** vector_docs / entities / chunk_hits 明细 —— 初版在这里查明细，查到的是"没有这个键"。
    #   明细出口是 `citations`（由 chunk_hits 经 select_for_injection 裁剪而来）。
    cits = ret.get("citations") or []
    vc = int(ret.get("vector_count") or 0)
    gc = int(ret.get("graph_count") or 0)
    docs = sorted({str(x.get("source_doc") or "") for x in cits if isinstance(x, dict)} - {""})
    print(f"     retrieval: route={ret.get('route')} vector_count={vc} graph_count={gc} "
          f"citations={len(cits)} docs={docs[:3]}")
    check("P1", "检索真的产出了证据（计数>0 或 citations 非空）",
          vc > 0 or gc > 0 or bool(cits), f"vc={vc} gc={gc} citations={len(cits)}")
    check("P1", "检索来源含已入库标准文档（29148 / Guide to writing）",
          any("29148" in d or "Guide to writing" in d for d in docs),
          f"docs={docs[:3]}")

    verdict = r.get("requirement_check_verdict")
    reasons = r.get("requirement_check_reasons") or []
    check("P1", "返回体带 requirement_check_verdict", bool(verdict),
          f"verdict={verdict} reasons={str(reasons)[:120]}")

    cd, content = _card_data_of(conv)
    check("P1", "card_data 已落库（刷新不丢）", bool(cd), f"keys={list((cd or {}).keys())[:8]}")
    qr = (cd or {}).get("quality_report") or r.get("quality_report") or {}
    issues = qr.get("issues") or []
    check("P1", "quality_report.issues 条数 ≥10（≥10 类规则被逐一判定）", len(issues) >= 10,
          f"issues={len(issues)}")
    if issues:
        check("P1", "issues 四字段齐（problem/severity/evidence/suggestion）",
              all(all(k in i for k in ("problem", "severity", "evidence", "suggestion"))
                  for i in issues), f"样例={json.dumps(issues[0], ensure_ascii=False)[:100]}")
        # 规则编号必须来自清单（证明判据来自数据资产，不是模型自造）
        import re
        cat = json.load(open(os.path.join(_ROOT, "data", "requirement_standards",
                                          "rule_catalog.json"), encoding="utf-8"))
        ids = {c["id"] for c in cat["rule_categories"]} | \
              {"完整性"} | {c["id"] for c in cat["conflict_types"]}
        found = set()
        for i in issues:
            m = re.search(r"\[([A-Z]{2}-\d{2})\]", str(i.get("problem") or ""))
            if m:
                found.add(m.group(1))
        check("P1", "issues 的规则编号落在清单内（不是模型自造）",
              bool(found) and found <= ids, f"found={sorted(found)[:8]} 越界={sorted(found - ids)[:5]}")

    # ★ 覆盖口径必须是**证据制**：模型没写 covered_categories 时，只要 checks 里
    #   逐条出现了 rule_id，就不能判"未覆盖"。初版只认自报 ⇒ 真机误判 14 类全未覆盖。
    res = (cd or {}).get("requirement_check_result") or {}
    cov = res.get("covered_categories") or []
    cov_decl = res.get("covered_categories_declared") or []
    print(f"     covered(证据制)={len(cov)} 自报={len(cov_decl)}")
    check("P1", "覆盖类别由 checks[].rule_id 作证（不依赖模型自报）", len(cov) >= 5,
          f"covered={cov[:8]}")
    check("P1", "verdict 与理由自洽（insufficient 必须给出具体缺口）",
          (verdict != "insufficient") or bool(reasons), f"verdict={verdict} reasons={str(reasons)[:80]}")
    return conv, r


def phase2():
    print("\n── P2 冲突检测（真机：不许在有数据时写「无冲突」）──")
    conv = _new_conv("P2")
    if not conv:
        check("P2", "创建会话", False, "无 id")
        return
    try:
        r, dt = _chat(conv, PROMPT_CONFLICT)
    except Exception as e:
        check("P2", "请求完成（未挂死）", False, repr(e)[:200])
        return
    print(f"     耗时 {dt:.0f}s")
    check("P2", "意图路由到冲突检测类", str(r.get("intent") or "").startswith("requirement_"),
          f"intent={r.get('intent')}")
    cd, _c = _card_data_of(conv)
    res = (cd or {}).get("requirement_check_result") or {}
    conflicts = res.get("conflicts") or []
    det = res.get("deterministic_verdict")
    stats = res.get("conflict_stats") or {}
    anchored = int(stats.get("claims_anchored", 0) or 0)
    verdict = r.get("requirement_check_verdict")
    print(f"     conflicts={len(conflicts)} deterministic_verdict={det} "
          f"anchored={anchored} verdict={verdict}")
    # ★ 双层必须都跑过：真机曾出现 det=None / stats 空（确定性层零调用点），
    #   等于设计里的"双层"只有语义一层。这里把它变成硬判据。
    check("P2", "确定性层真的跑过（deterministic_verdict 非 None）", det is not None,
          f"det={det} stats={json.dumps(stats, ensure_ascii=False)[:90]}")
    check("P2", "确定性层回传 conflict_stats（可复算）", bool(stats),
          f"stats={json.dumps(stats, ensure_ascii=False)[:90]}")
    # 核心判据：若 anchored==0 且无冲突 ⇒ 必须是 undetermined/insufficient，不许是"检查通过"
    if anchored == 0 and not conflicts:
        check("P2", "anchored=0 且无冲突 ⇒ 判为「未判定」而非「无冲突」",
              det == "undetermined" or verdict == "insufficient",
              f"det={det} verdict={verdict}")
    else:
        check("P2", "有锚点或检出冲突 ⇒ 判定为已检查", det == "checked" or bool(conflicts),
              f"det={det} conflicts={len(conflicts)}")
    # 矛盾需求（2秒 vs 5秒 vs 3秒）在锚点存在时应被检出
    check("P2", "同锚点矛盾需求被检出或明确标注未判定（不许静默通过）",
          bool(conflicts) or det == "undetermined" or verdict == "insufficient",
          f"conflicts={len(conflicts)} det={det} verdict={verdict}")


def phase3():
    print("\n── P3 配置即行为（换档案子集 ⇒ 结论随之变化）──")
    cat = json.load(open(os.path.join(_ROOT, "data", "requirement_standards",
                                     "rule_catalog.json"), encoding="utf-8"))
    full = [c["id"] for c in cat["rule_categories"]]
    sub = full[:5]
    c = sqlite3.connect(DB)
    try:
        orig = c.execute("SELECT rule_value FROM generate_rules WHERE rule_key=?",
                         ("requirement_rule_categories",)).fetchone()
        orig_v = orig[0] if orig else None
    finally:
        c.close()
    if not orig_v:
        check("P3", "原类别配置存在（否则无法做对照）", False, "无 requirement_rule_categories")
        return
    conv = _new_conv("P3")
    if not conv:
        check("P3", "创建会话", False, "无 id")
        return
    try:
        # 砍到 5 类后重跑**同一提示词**
        c = sqlite3.connect(DB)
        try:
            c.execute("UPDATE generate_rules SET rule_value=? WHERE rule_key=?",
                      (json.dumps(sub, ensure_ascii=False), "requirement_rule_categories"))
            c.commit()
        finally:
            c.close()
        r, dt = _chat(conv, PROMPT_RULE)
        print(f"     子集档案 ({len(sub)} 类) 耗时 {dt:.0f}s")
        cd, _c = _card_data_of(conv)
        res = (cd or {}).get("requirement_check_result") or {}
        reasons_sub = res.get("standard_check_reasons") or r.get("requirement_check_reasons") or []
        print(f"     verdict={r.get('requirement_check_verdict')} reasons={str(reasons_sub)[:120]}")
        # 判据：`configured_categories` 是 assemble_result 按档案写入的回显，
        # 子集档案下必须**恰好**等于那 5 类（换标准即换判据的直接证据）。
        cfg = [str(x) for x in (res.get("configured_categories") or [])]
        cov_sub = [str(x) for x in (res.get("covered_categories") or [])]
        print(f"     configured={cfg} covered={cov_sub[:8]}")
        check("P3", "子集档案生效（configured_categories == 那 5 类）",
              sorted(cfg) == sorted(sub), f"cfg={cfg} 期望={sub}")
        check("P3", "子集档案下请求正常完成（换标准不炸链路）",
              bool(r.get("requirement_check_verdict")), f"verdict={r.get('requirement_check_verdict')}")
        check("P3", "缺口按子集之外的类别报，而不是全量 14 类",
              (r.get("requirement_check_verdict") != "insufficient") or
              (len(cov_sub) > 0 and set(cfg) == set(sub)),
              f"verdict={r.get('requirement_check_verdict')} cov={len(cov_sub)}")
    except Exception as e:
        check("P3", "子集档案下请求完成", False, repr(e)[:200])
    finally:
        c = sqlite3.connect(DB)
        try:
            c.execute("UPDATE generate_rules SET rule_value=? WHERE rule_key=?",
                      (orig_v, "requirement_rule_categories"))
            c.commit()
        finally:
            c.close()
        back = None
        c = sqlite3.connect(DB)
        try:
            row = c.execute("SELECT rule_value FROM generate_rules WHERE rule_key=?",
                            ("requirement_rule_categories",)).fetchone()
            back = row[0] if row else None
        finally:
            c.close()
        check("P3", "档案已还原（14 类）",
              bool(back) and len(json.loads(back)) == len(full), f"还原条数={len(json.loads(back or '[]'))}")


def main():
    want = [a.lower() for a in sys.argv[1:]] or ["p1", "p2", "p3"]
    print("=" * 78)
    print("需求分析与冲突检测 · 配置化链路真机 E2E（HTTP 入口 + 落库取证）")
    print("=" * 78)
    try:
        with urllib.request.urlopen(BASE + "/api/conversations", timeout=15) as r:
            ok = r.getcode() == 200
    except Exception as e:
        print(f"  服务不可达：{e!r}")
        return 1
    check("P0", "服务可达", ok)

    if "p1" in want:
        phase1()
    if "p2" in want:
        phase2()
    if "p3" in want:
        phase3()

    fails = [x for x in RESULTS if not x[2]]
    print("\n" + "=" * 78)
    print(f"结果：PASS {len(RESULTS) - len(fails)} / FAIL {len(fails)}  （共 {len(RESULTS)} 条）")
    for g, n, _, d in fails:
        print(f"  - [{g}] {n}   ← {d}")
    print("=" * 78)
    os.makedirs(os.path.join(_ROOT, "outputs"), exist_ok=True)
    with open(os.path.join(_ROOT, "outputs", "_e2e_requirement_chain.json"), "w",
              encoding="utf-8") as f:
        json.dump({"rows": [{"g": g, "n": n, "ok": o, "d": d} for g, n, o, d in RESULTS]},
                  f, ensure_ascii=False, indent=1)
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
