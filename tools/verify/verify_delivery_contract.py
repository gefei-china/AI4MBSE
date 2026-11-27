# -*- coding: utf-8 -*-
"""门禁：通用交付契约 —— 「声称完成」不得冒充「实际交付」。

背景（2026-11-27 会话 1000000251 真机事故）：
  用户要「变更影响分析报告」，助手声称「十节正文 + 附录 A~F 已作为文档产物交付」，
  但 artifacts.id=2311 / reports.id=32 只有 1 个 section（547 字符的一段自述）。
  根因三层：待办自述冒充验收 / 模板节数只是 prompt 软约束 / 产物无结构校验。

本门禁的四组判据（全部可打印证据，不用总数口径）：
  A 契约内核：有契约且达标才 ok；无契约 checked=False（不冒充通过）；自定义清单通用；
  B 证据分级：机器回执 / 人工确认 / 仅自述 / 未知 四类分开，claim 必须能被点名；
  C 跨边界：真跑 `_archive_artifacts` 落内存库，断言 artifacts.meta 与 reports.summary
     里能看到「未达章节交付契约」——判据落在用户读的地方，不是函数返回值；
  D 变异自证：把内核判据改坏（变异体必须真的变了），A/C 必须翻红。
    锚点短、唯一、语义明确；变异后断言 `mutated != src`，否则算变异打空。

用法：python tools/verify/verify_delivery_contract.py
退出码 0 = 全绿；1 = 有红项。
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import sqlite3
import sys
import contextlib

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

RESULTS = []


def check(group: str, name: str, cond: bool, detail: str = ""):
    RESULTS.append((group, name, bool(cond), detail))


def _load(modname: str, relpath: str):
    spec = importlib.util.spec_from_file_location(
        modname, os.path.join(_ROOT, *relpath.split("/")))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


dc = _load("_dc_gate", "core/delivery_contract.py")
rg = _load("_rg_gate", "services/report_generator.py")


# ── A. 契约内核 ──────────────────────────────────────────────────
def group_a():
    one = [{"heading": "正文", "body": "本轮新增：数据源核验..."}]
    r = dc.check_section_contract(one, template=rg.expected_sections_for("impact"))
    check("A", "一段正文 vs impact 模板 ⇒ 未达标", 
          r["checked"] and not r["ok"] and r["actual"] == 1 and r["min"] == 10,
          json.dumps(r, ensure_ascii=False))

    full = [{"heading": h, "body": "x"} for h in [
        "报告信息", "摘要", "变更描述", "分析范围与方法", "直接影响分析",
        "间接影响与传播路径", "影响程度与风险分级", "处置建议", "结论", "附录"]]
    r2 = dc.check_section_contract(full, template=rg.expected_sections_for("impact"))
    check("A", "十节齐全 ⇒ 达标", r2["ok"] and r2["actual"] == 10,
          json.dumps(r2, ensure_ascii=False))

    r3 = dc.check_section_contract(one)
    check("A", "无契约 checked=False，不冒充通过",
          (not r3["checked"]) and (not r3["ok"]), json.dumps(r3, ensure_ascii=False))

    # 通用性：不绑定报告类型，自定义 3 节清单也能当契约
    custom = [{"heading": "引言", "body": "a"}, {"heading": "方法", "body": "b"},
              {"heading": "结论", "body": "c"}]
    r4 = dc.check_section_contract(custom, contract=custom)
    check("A", "自定义三节契约 ⇒ 达标（通用，非十节硬编码）",
          r4["ok"] and r4["min"] == 3, json.dumps(r4, ensure_ascii=False))
    r5 = dc.check_section_contract(custom[:2], contract=custom)
    check("A", "自定义清单缺节 ⇒ 未达标并点名",
          (not r5["ok"]) and "结论" in "、".join(r5["missing"]),
          json.dumps(r5, ensure_ascii=False))

    # 缺必需标题（即使节数够）也要红——防「凑数十节」
    padded = [{"heading": "水节%d" % i, "body": "x"} for i in range(10)]
    r6 = dc.check_section_contract(
        padded, contract={"min_sections": 10, "required_sections": ["摘要", "结论"]})
    check("A", "凑数十节但缺必需标题 ⇒ 未达标",
          (not r6["ok"]) and len(r6["missing"]) == 2,
          json.dumps(r6, ensure_ascii=False))

    # 用户显式自定义大纲优先于类型模板（不误伤「就按 3 节写」的请求）
    r7 = dc.check_section_contract(
        custom, contract=dc.contract_from_card({"delivery_contract": custom}),
        template=rg.expected_sections_for("impact"))
    check("A", "显式契约优先于类型模板（3 节不被 10 节误伤）",
          r7["ok"] and r7["min"] == 3 and r7["source"] == "caller",
          json.dumps(r7, ensure_ascii=False))


# ── B. 证据分级 ──────────────────────────────────────────────────
def group_b():
    check("B", "机器回执 ⇒ machine",
          dc.evidence_tier("graph_retrieve 返回非空", "graph_retrieve",
                           {"kind": "machine", "log_id": 14563}) == "machine")
    check("B", "无 verify_tool 的自然语言验收 ⇒ claim",
          dc.evidence_tier("正文含报告信息/摘要/…/附录十节", "",
                           {"kind": "claim"}) == "claim")
    check("B", "有 verify_tool 但无回执 ⇒ claim（本该机器验却只写话）",
          dc.evidence_tier("正文含十节", "file_read", None) == "claim")
    check("B", "显式人工确认 ⇒ manual（允许自述，不冒充机器）",
          dc.evidence_tier("人工确认：正文完整输出", "", None) == "manual")
    check("B", "查询失败 unknown 不伪装成通过",
          dc.evidence_tier("校验返回非空", "sysml_v2_validate",
                           {"kind": "unknown"}) == "unknown")

    items = [
        {"subject": "核验数据源", "status": "completed", "verify": "graph_retrieve 返回非空",
         "verify_tool": "graph_retrieve", "evidence": {"kind": "machine"}},
        {"subject": "按模板撰写十节正文", "status": "completed", "verify": "正文含十节",
         "verify_tool": "", "evidence": {"kind": "claim"}},
        {"subject": "落盘报告", "status": "completed", "verify": "人工确认交付",
         "verify_tool": "", "evidence": {"kind": "claim"}},
    ]
    s = dc.claim_evidence_summary(items)
    check("B", "三类证据分开计数", s["machine"] == 1 and s["claim"] == 1
          and s["manual"] == 1, json.dumps(s, ensure_ascii=False))
    check("B", "claim 能被点名（可打印证据，不是总数）",
          s["claim_subjects"] == ["按模板撰写十节正文"],
          json.dumps(s["claim_subjects"], ensure_ascii=False))


# ── C. 跨边界：真跑归档，查落库产物 ───────────────────────────────
_DDL = """
CREATE TABLE conversations (id INTEGER PRIMARY KEY, project_id TEXT DEFAULT '');
CREATE TABLE artifacts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id INTEGER, message_id INTEGER,
  project_id TEXT, kind TEXT, title TEXT, filename TEXT, file_path TEXT, file_url TEXT,
  mime TEXT, size INTEGER, preview_type TEXT, preview_content TEXT, meta TEXT,
  source TEXT, created_by TEXT, created_at TEXT);
CREATE TABLE reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, report_type TEXT, summary TEXT,
  sections TEXT, source TEXT, conversation_id INTEGER, branch TEXT, project_id TEXT,
  status TEXT, created_by TEXT, created_at TEXT, updated_at TEXT);
CREATE TABLE sysml_versions (id INTEGER PRIMARY KEY);
"""


def group_c():
    utils = _load("_utils_gate", "agent/utils.py")
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_DDL)
    conn.execute("INSERT INTO conversations(id) VALUES (1)")
    conn.commit()

    card = {"intent": "report_generation", "report_type": "impact",
            "report_title": "卫星通信链路变更影响分析报告"}
    body = ("本轮新增：数据源核验的最终结论与报告交付状态确认。\n\n"
            "报告全文（十节正文 + 附录 A~F）已作为文档产物交付，可在产物面板查看与下载。")
    utils._archive_artifacts(conn, 1, 1, card, body, "report_generation", "tester")

    art = conn.execute("SELECT meta FROM artifacts WHERE kind='report'").fetchone()
    rep = conn.execute("SELECT summary, sections FROM reports WHERE conversation_id=1").fetchone()
    check("C", "一段正文仍被归档为 report（不丢信息）", art is not None and rep is not None,
          "artifact=%s report=%s" % (bool(art), bool(rep)))
    if art:
        meta = json.loads(art["meta"] or "{}")
        dcm = meta.get("delivery_contract") or {}
        check("C", "★ 产物 meta 落机器判据：checked=True/ok=False/min=10/actual=1",
              dcm.get("checked") is True and dcm.get("ok") is False
              and dcm.get("min") == 10 and dcm.get("actual") == 1,
              json.dumps(dcm, ensure_ascii=False))
    if rep:
        check("C", "★ 报告中心摘要前置警示（用户读的地方可见）",
              "未达章节交付契约" in (rep["summary"] or ""),
              (rep["summary"] or "")[:180])
        secs = json.loads(rep["sections"] or "[]")
        check("C", "sections 仍保留原文（可追溯，不被替换成空）", len(secs) == 1,
              json.dumps(secs, ensure_ascii=False)[:180])
    check("C", "★ 无卡数据时也不谎报通过（checked=False）",
          (lambda r: (not r["checked"]) and (not r["ok"]))(
              dc.check_section_contract([{"heading": "正文", "body": "x"}])))
    conn.close()


# ── D. 变异自证 ──────────────────────────────────────────────────
def _mutate(src: str, old: str, new: str) -> str:
    """短、唯一、语义明确的锚点；返回 None 表示锚点不唯一（变异打空）。"""
    if src.count(old) != 1:
        return None
    out = src.replace(old, new, 1)
    return out if out != src else None


def group_d():
    import tempfile
    src_path = os.path.join(_ROOT, "core", "delivery_contract.py")
    src = open(src_path, encoding="utf-8").read()

    # M1：把「章节数不足判红」改成「判绿」——A1 必须翻红
    anchor = '    if actual < exp["min"]:\n        return dict(base, ok=False,'
    m1 = _mutate(src, anchor,
                 '    if False:\n        return dict(base, ok=False,')
    check("D", "M1 变异体真的变了（锚点唯一且命中）", m1 is not None,
          "anchor_count=%d" % src.count(anchor))
    if m1:
        tmp = os.path.join(tempfile.gettempdir(), "_dc_mut_gate.py")
        open(tmp, "w", encoding="utf-8").write(m1)
        mut = _load("_dc_mut_runtime", tmp)
        mm = mut.check_section_contract(
            [{"heading": "正文", "body": "x"}], template=rg.expected_sections_for("impact"))
        check("D", "★ M1 变异后一段正文被判绿 ⇒ 原判据 A1 有牙齿",
              mm["ok"] is True, json.dumps(mm, ensure_ascii=False))
        try:
            os.remove(tmp)
        except OSError:
            pass

    # M2：把 evidence_tier 的 machine 分支改掉 —— B1 必须翻红
    anchor2 = '    if kind == EV_MACHINE:\n        return EV_MACHINE'
    m2 = _mutate(src, anchor2,
                 '    if kind == EV_MACHINE:\n        return EV_CLAIM')
    check("D", "M2 变异体真的变了", m2 is not None,
          "anchor_count=%d" % src.count(anchor2))
    if m2:
        tmp2 = os.path.join(tempfile.gettempdir(), "_dc_mut2_gate.py")
        open(tmp2, "w", encoding="utf-8").write(m2)
        mut2 = _load("_dc_mut2_runtime", tmp2)
        got = mut2.evidence_tier("x", "graph_retrieve", {"kind": "machine"})
        check("D", "★ M2 变异后机器回执被降级 ⇒ B1 有牙齿",
              got == "claim", got)
        try:
            os.remove(tmp2)
        except OSError:
            pass


def main():
    for fn in (group_a, group_b, group_c, group_d):
        try:
            fn()
        except Exception as e:
            check(fn.__name__, "组执行异常", False, repr(e))
    w = max(len(n) for _, n, _, _ in RESULTS)
    print("=" * (w + 16))
    print("通用交付契约门禁（声称 ≠ 交付）")
    print("=" * (w + 16))
    for g, n, ok, detail in RESULTS:
        print("  [%s] %s%s" % ("OK" if ok else "FAIL", n,
                               ("  — " + detail) if (detail and not ok) else ""))
    bad = [x for x in RESULTS if not x[2]]
    print("-" * (w + 16))
    print("合计 %d 项，通过 %d，失败 %d" % (len(RESULTS), len(RESULTS) - len(bad), len(bad)))
    for g, n, _, d in bad:
        print("  - %s：%s" % (n, d[:200]))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
