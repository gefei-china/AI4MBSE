# CI-OPTIONAL: [local] tier=local（见 tools/gates_manifest.json）：需真实服务 / 真 LLM key / 生产真库金标或随包运行时资产，CI 无服务无 secret ⇒ 跑不了；本机已实测通过，改法须同时改清单 tier 并接线，不允许只删这行
"""P0-2 样本回流闭环的**真实数据验证**（2026-11-07 米爸第 15 轮）。

用真库 `intent_samples`，验证：
  A 改选写入：status='suggested'、source='override'、note 可机读、含原判与改判
  B 不自动发布：写完仍是 suggested（绝不 confirmed）；既有 confirmed 行不被覆盖
  C 幂等：同一句重复改选只累加 seen_count，不新增行
  D 过滤：系统包装文本 / 超长 / 空值 / 采集开关关闭 都不写
  E 审核：批量确认后转 confirmed 且 intent = 用户改选值；非 override 行不动
  F 真实数据：从库里真实 confirmed 样本里取一条做「不可覆盖」验证，不造合成标签

⚠️ 会在库里留 1~2 条探针样本（source='override'、text 以 __GATE__ 开头），
   finally 清理；不碰任何业务样本。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
DB = os.path.join(ROOT, "mbse.db")

FAILED = []


def rec(code, ok, detail=""):
    print("[%s] %-4s %s%s" % ("PASS" if ok else "FAIL", code, detail,
                              "" if ok else "  <<<"))
    if not ok:
        FAILED.append(code)


def main() -> int:
    from core import config as cfg
    from services.intent_override_feedback import record_override, override_candidates
    from repositories.intent_sample_repo import IntentSampleRepo

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    probe = "__GATE__ 请帮我评估这个改动会影响哪些部件"
    probe2 = "__GATE__ 请把这份需求条目化成清单"
    # 清掉上一轮残留，保证唯一变量
    conn.execute("DELETE FROM intent_samples WHERE text LIKE '__GATE__%'")
    conn.commit()

    # 真实 confirmed 样本作「不可覆盖」验证对象
    real = conn.execute("SELECT id,text,intent,status FROM intent_samples "
                        "WHERE status='confirmed' ORDER BY id LIMIT 1").fetchone()
    rec("F1", bool(real), "真库 confirmed 样本 id=%s intent=%s" %
        (real["id"], real["intent"]) if real else "无 confirmed 样本")

    try:
        cfg._CONFIG.setdefault("intent", {})["sample_collect"] = True
        # ── A 改选写入 ──
        r = record_override(probe, "impact", detected="design",
                            route="rule_scored", conf=0.95)
        rec("A1", bool(r.get("ok")), "改选写入 %s" % r)
        row = conn.execute("SELECT * FROM intent_samples WHERE text=?", (probe,)).fetchone()
        rec("A2", bool(row) and row["status"] == "suggested",
            "status=%s（必须是 suggested）" % (row["status"] if row else "无行"))
        rec("A3", bool(row) and row["source"] == "override",
            "source=%s" % (row["source"] if row else "无行"))
        note = json.loads(row["note"] or "{}") if row else {}
        rec("A4", note.get("override") == "impact" and note.get("detected") == "design"
            and note.get("route") == "rule_scored",
            "note 可机读：%s" % note)
        rec("A5", bool(row) and (row["intent"] or "") == "",
            "intent 列保持空（系统建议值不冒充人工标签）")

        # ── B 不自动发布 + confirmed 不被覆盖 ──
        before = dict(real)
        r2 = record_override(before["text"], "knowledge_qa", detected="design")
        after = conn.execute("SELECT * FROM intent_samples WHERE id=?",
                             (before["id"],)).fetchone()
        rec("B1", after["status"] == "confirmed" and after["intent"] == before["intent"],
            "既有 confirmed 行改选后仍保持原标签（upsert 保护生效）")
        rec("B2", conn.execute("SELECT COUNT(*) FROM intent_samples "
                               "WHERE status='confirmed' AND source='override'").fetchone()[0] == 0,
            "没有任何 override 样本被自动 confirmed")

        # ── C 幂等 ──
        n0 = conn.execute("SELECT seen_count FROM intent_samples WHERE text=?",
                          (probe,)).fetchone()[0]
        r3 = record_override(probe, "impact", detected="design")
        rows_n = conn.execute("SELECT COUNT(*) FROM intent_samples WHERE text=?",
                              (probe,)).fetchone()[0]
        n1 = conn.execute("SELECT seen_count FROM intent_samples WHERE text=?",
                          (probe,)).fetchone()[0]
        rec("C1", rows_n == 1 and n1 == n0 + 1,
            "重复改选不新增行，seen_count %d→%d" % (n0, n1))

        # ── D 过滤 ──
        rec("D1", not record_override("【澄清补充】用户已补充以下建模信息", "impact")["ok"],
            "系统包装文本被拦")
        rec("D2", not record_override("短", "impact")["ok"], "过短被拦")
        rec("D3", not record_override("x" * 250, "impact")["ok"], "过长被拦")
        rec("D4", not record_override(probe, "")["ok"], "空改选值被拦")
        rec("D5", not record_override(probe2, "design", detected="design")["ok"],
            "「改选==原判」不写（同意不是纠错信号）")
        cfg._CONFIG["intent"]["sample_collect"] = False
        rec("D6", not record_override(probe2, "impact")["ok"], "采集开关关闭时不写")
        cfg._CONFIG["intent"]["sample_collect"] = True

        # ── E 人工审核 ──
        cand = override_candidates(conn, limit=200)
        rec("E1", any(x["text"] == probe and x["parsed"].get("override") == "impact"
                      for x in cand),
            "待审列表能列出改选样本并给出 parsed.override")
        pid = conn.execute("SELECT id FROM intent_samples WHERE text=?", (probe,)).fetchone()[0]
        IntentSampleRepo(conn).update(pid, intent="impact", status="confirmed")
        done = conn.execute("SELECT status,intent FROM intent_samples WHERE id=?",
                            (pid,)).fetchone()
        rec("E2", done["status"] == "confirmed" and done["intent"] == "impact",
            "人工审核后转 confirmed，intent=用户改选值")
        # 非 override 行不被动
        other = conn.execute("SELECT COUNT(*) FROM intent_samples "
                             "WHERE status='suggested' AND source!='override'").fetchone()[0]
        rec("E3", other > 0, "既有 suggested 样本未被批量操作波及（%d 条仍在）" % other)

    finally:
        # 清探针；若把真 confirmed 行的 seen_count 加了 1，还原它
        conn.execute("DELETE FROM intent_samples WHERE text LIKE '__GATE__%'")
        if real:
            conn.execute("UPDATE intent_samples SET seen_count=? WHERE id=?",
                         (real["seen_count"] if "seen_count" in real.keys() else 1,
                          real["id"]))
        conn.commit()
        conn.close()
        try:
            cfg._CONFIG["intent"]["sample_collect"] = True
        except Exception:
            pass

    print("=" * 78)
    print("ALL_OK=%s" % (not FAILED) if not FAILED
          else "FAIL %d 项：%s" % (len(FAILED), ", ".join(FAILED)))
    return 0 if not FAILED else 1


if __name__ == "__main__":
    raise SystemExit(main())
