# -*- coding: utf-8 -*-
"""版本差分双向下 + 元素去重 + 摘要告警 的确定性自检（2026-11-09）。

被验对象（三处，对应"前面犯的错后面还在犯"的数据层根因）：
    · `agent/utils.py::_archive_sysml_version` —— element_summary 去重 / diff 双向 / 建档守卫
    · `agent/utils.py::_archive_artifacts`     —— 有 sysml 代码但无视图的交付轮也要建档
    · `agent/session_artifacts.py::render_digest` —— 把「比上一版少了什么」注入下一轮上下文

背景（会话 990541 实测）：
  · diff 原只算 added_nodes（全库 131 版仅 1 版有 removed*，缺 99.2%）
    ⇒ 86.5% 的修订轮次在丢元素，而数据层**结构上不报**
  · element_summary.nodes 跨视图重复 append，entities 记的是"出现次数"（v0.3 报 12，唯一名 4）
  · 有代码但无结构化视图的交付轮不建版本 → 库里 current 反而是坏的那版

运行：<venv>/python.exe -X utf8 tools/verify/verify_version_diff.py
      → ALL PASS（14 项断言），实测耗时约 2.5s，可直接进 pr tier
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agent.utils import _archive_sysml_version  # noqa: E402
from agent.session_artifacts import render_digest  # noqa: E402
import core.config as cfg  # noqa: E402
import sqlite3  # noqa: E402

TMP_CONV = 990999999
fails = []
checks = [0]


def chk(name, cond, detail=""):
    checks[0] += 1
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  | " + detail) if detail else ""))
    if not cond:
        fails.append(name)


def views(*, nodes_by_view, edges=None):
    """构造结构化视图：nodes_by_view = {"BDD": ["A","B"], "IBD": ["A","C"]}"""
    vs = {}
    for vname, names in nodes_by_view.items():
        vs[vname] = {
            "view": {"name": vname},
            "nodes": [{"name": n} for n in names],
            "edges": edges or [],
        }
    return {"views": vs, "intent": "modeling"}


conn = sqlite3.connect(cfg.DB_PATH)
conn.row_factory = sqlite3.Row
try:
    conn.execute("BEGIN")

    print("\n[1] 去重：跨视图重复出现的元素只计一次")
    v1 = _archive_sysml_version(
        conn, TMP_CONV, 1,
        views(nodes_by_view={"BDD": ["A", "B"], "IBD": ["A", "C"]}), "自检", "package P1;")
    row = conn.execute("SELECT element_summary FROM sysml_versions WHERE id=?", (v1,)).fetchone()
    es = json.loads(row["element_summary"])
    chk("entities == 3（A/B/C，而非跨视图出现 4 次）", es["entities"] == 3, f"实际 {es['entities']}")
    chk("nodes 无重复", len(es["nodes"]) == len(set(es["nodes"])), f"{es['nodes']}")

    print("\n[2] 双向 diff：第二版删掉 B，必须出现在 removed_nodes")
    v2 = _archive_sysml_version(
        conn, TMP_CONV, 2,
        views(nodes_by_view={"BDD": ["A", "C"], "IBD": ["A", "D"]}), "自检", "package P2;")
    df = json.loads(conn.execute("SELECT diff FROM sysml_versions WHERE id=?", (v2,)).fetchone()["diff"])
    chk("removed_nodes == ['B']", df.get("removed_nodes") == ["B"], str(df.get("removed_nodes")))
    chk("added_nodes == ['D']", df.get("added_nodes") == ["D"], str(df.get("added_nodes")))
    chk("kept_nodes == 2（A/C）", df.get("kept_nodes") == 2, str(df.get("kept_nodes")))
    chk("diff 含 removed_edges 键", "removed_edges" in df)

    print("\n[3] 兼容存量脏数据：上一版 nodes 未去重时，不得把重复项误判为新增")
    # 用独立临时会话，确保 parent 就是那条"脏数据"，而不是本会话的 MAX(id)。
    # ← 首跑此断言写错：直接改 TMP_CONV 里 v1 的 summary，但 last_id 取的是 v2，
    #   于是 removed=['D'] 被误判为 bug —— 实际是正确行为（v3 相对 v2 确实少了 D）。
    #   这里改成 TMP2，让被测对象与 parent 一一对应。
    TMP2 = 990999998
    conn.execute(
        "INSERT INTO sysml_versions (artifact_id, conversation_id, message_id, project_id, version_label,"
        " content, diff, element_summary, parent_id, status, created_by, code_text)"
        " VALUES (0,?,?,?,?,?,?,?,?,?,?,?)",
        (TMP2, 900, "", "v0.1", "{}", "{}",
         json.dumps({"entities": 6, "nodes": ["A", "A", "A", "C"], "views": 2}), None, "current", "自检", ""))
    v3 = _archive_sysml_version(
        conn, TMP2, 901,
        views(nodes_by_view={"BDD": ["A", "C"]}), "自检", "package P3;")
    df3 = json.loads(conn.execute("SELECT diff FROM sysml_versions WHERE id=?", (v3,)).fetchone()["diff"])
    chk("added_nodes 为空（A/C 均已存在，重复项不算新增）", df3.get("added_nodes") == [], str(df3.get("added_nodes")))
    chk("removed_nodes 为空（A/C 都在）", df3.get("removed_nodes") == [], str(df3.get("removed_nodes")))

    print("\n[4] 收口：无结构化视图、但有 sysml 代码 → 仍建版本")
    v4 = _archive_sysml_version(conn, TMP_CONV, 4, {}, "自检", "package OnlyCode; part p;")
    chk("建出版本（返回 id）", isinstance(v4, int) and v4 > 0, f"返回 {v4}")
    if v4:
        r4 = conn.execute("SELECT length(code_text) AS L FROM sysml_versions WHERE id=?", (v4,)).fetchone()
        chk("code_text 已保存", int(r4["L"]) > 0, f"{r4['L']} 字符")

    print("\n[5] 空交付不建档（防止污染版本链）")
    v5 = _archive_sysml_version(conn, TMP_CONV, 5, {}, "自检", "")
    chk("无视图且无代码 → 返回 None", v5 is None, f"返回 {v5}")

    print("\n[6] 摘要必须把「少了什么」说出来")
    txt = render_digest({
        "versions": [{
            "label": "v0.3", "status": "current", "adopted": False, "entities": 2,
            "relations": 0, "views": 1, "nodes": ["A", "C"], "verdict": "block",
            "code_len": 120, "removed_nodes": ["B", "E"], "added_nodes": [],
        }],
        "kinds": {}, "counts": {}, "reports": [],
    })
    chk("摘要含「少了 2 个元素」", "少了 2 个元素" in txt, txt.replace("\n", " / ")[:160])
    chk("摘要含丢失元素名 B", "B" in txt and "E" in txt)
    chk("摘要含通用判据（不得静默省略）", "不得静默省略" in txt)

    print("\n[7] 零回归：无 removed 时不出现告警行")
    txt2 = render_digest({
        "versions": [{
            "label": "v0.1", "status": "current", "adopted": False, "entities": 1,
            "relations": 0, "views": 1, "nodes": ["A"], "verdict": "pass",
            "code_len": 50, "removed_nodes": [], "added_nodes": [],
        }],
        "kinds": {}, "counts": {}, "reports": [],
    })
    chk("不含 ⚠️ 告警", "⚠️" not in txt2, txt2.replace("\n", " / ")[:160])

    print("\n[8] 累积契约：逐版 diff 正常时，仍能发现历史元素消失（逐版比对抓不到的那类）")
    TMP3 = 990999997
    _v1 = _archive_sysml_version(conn, TMP3, 1, views(nodes_by_view={"BDD": ["A", "B"]}), "自检", "package P;")
    _v2 = _archive_sysml_version(conn, TMP3, 2, views(nodes_by_view={"BDD": ["A"]}), "自检", "package P;")
    _v3 = _archive_sysml_version(conn, TMP3, 3, views(nodes_by_view={"BDD": ["A", "C"]}), "自检", "package P;")
    d2 = json.loads(conn.execute("SELECT diff FROM sysml_versions WHERE id=?", (_v2,)).fetchone()["diff"])
    d3 = json.loads(conn.execute("SELECT diff FROM sysml_versions WHERE id=?", (_v3,)).fetchone()["diff"])
    e3 = json.loads(conn.execute("SELECT element_summary FROM sysml_versions WHERE id=?", (_v3,)).fetchone()["element_summary"])
    chk("v0.2 逐版 removed == ['B']", d2.get("removed_nodes") == ["B"], str(d2.get("removed_nodes")))
    chk("v0.3 逐版 removed 为空（只加了 C，没删东西）",
        d3.get("removed_nodes") == [], str(d3.get("removed_nodes")))
    cov = e3.get("coverage") or {}
    chk("v0.3 对累积契约仍报缺 1 个", int(cov.get("lost_n") or 0) == 1, str(cov))
    chk("缺失项就是 B（历史有、现在没有）", cov.get("lost") == ["B"], str(cov.get("lost")))
    chk("契约含历史并集 A/B/C", set(e3.get("contract") or []) == {"A", "B", "C"},
        str(e3.get("contract")))

    print("\n[9] 摘要：最新版报「相对累计契约缺」，而不是逐版差异")
    txt3 = render_digest({
        "versions": [{
            "label": "v0.3", "status": "current", "adopted": False, "entities": 2,
            "relations": 0, "views": 1, "nodes": ["A", "C"], "verdict": "pass",
            "code_len": 90, "removed_nodes": [], "added_nodes": ["C"],
            "cov_lost": ["B"], "cov_lost_n": 1, "cov_contract": 3,
        }],
        "kinds": {}, "counts": {}, "reports": [],
    })
    chk("出现「相对**累计契约**缺 1 个元素」", "累计契约" in txt3 and "缺 1 个元素" in txt3,
        txt3.replace("\n", " / ")[-180:])
    chk("并指出缺失项 B", "B" in txt3)

finally:
    conn.rollback()
    conn.close()

print("\n" + ("=" * 60))
if fails:
    print(f"FAILED {len(fails)}/{checks[0]} 项：{fails}")
    sys.exit(1)
print(f"ALL PASS（{checks[0]} 项断言）")
