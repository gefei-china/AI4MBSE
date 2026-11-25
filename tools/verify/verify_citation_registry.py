# -*- coding: utf-8 -*-
"""门禁：引用编号的**单一真源**（2026-11-15）—— 工具检索命中必须可点。

## 被验的真实故障（真机 conv 999999033 / msg 6603）

用户要「按标准校验两条需求」（#知识库 标签），Skill 硬门槛要求**必须先调
`graph_retrieve` 检索条款原文** ⇒ 主路径就是工具检索。实机结果：

```
citations: 0            ← 前端可点条目为空
[n] 标注: 5 个 -> [1,2,3,4,5]
越界编号: [1, 2, 3, 4, 5]   ← 5 个全部越界，一个都点不动
```

而 `tool_call_logs` 里明明有 **11 次 graph_retrieve、每次命中 18~20 条片段**。

⇒ 根因是**两套编号坐标系**：

| 来源 | 编号 | 与 citations 的关系 |
|---|---|---|
| 提示词注入（`context.py`） | 【来源1】…【来源N】 | ✅ 同源（初始检索的 chunk_hits） |
| **工具输出（`tools.py`）** | 【来源 1】…【来源 4】 | ❌ **另一套**，从不回写 retrieval |
| 前端 citations | 只从**初始检索**取 | ❌ 走工具路径时为空 |

模型照着工具输出那套写 [1]~[5] ⇒ 全部越界。
另：初始检索那一轮向量召回**偶发返回 0 条**（`query_trace` 8061：
`reason=out_of_domain, vector_count=0`），把模型逼到连调 11 次工具检索自救，
同时 `exec.reasoning` 还写着「未命中任何条目」—— 与事实矛盾。

## 修法：引用登记簿（`agent/utils.py`）

一本账登记**所有**暴露给模型的片段（初始检索注入 + 工具检索命中），
编号全局唯一且单调递增；`【来源n】` 与前端 `citations` **都读它** ⇒ 结构上不可能错位。

## 运行

    python tools/verify/verify_citation_registry.py

退出码 0 = 全绿；1 = 有红项。
"""

from __future__ import annotations

import os as _svos
import sys as _svsys
_svroot = _svos.path.dirname(_svos.path.abspath(__file__))
while _svroot and not _svos.path.isdir(_svos.path.join(_svroot, 'tools')):
    _svroot = _svos.path.dirname(_svroot)
if _svroot and _svroot not in _svsys.path:
    _svsys.path.insert(0, _svroot)
from tools.stream_source import read_stream_source  # noqa: E402
import io
import os
import sqlite3
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

DB = os.environ.get("MBSE_GATE_DB") or os.path.join(_ROOT, "mbse.db")

#: **引用锚点机制上线前的最后一条真机消息 id**（2026-11-15 实测 = 6612）。
#: C4 只检查 id 大于它的消息 —— 历史消息是旧代码产物、无法回溯修锚点，
#: 拿它们追责会造出"永远红"的假判据（米爸铁律：判据要能反映现状，不能追责历史）。
#: 机制上线后每跑一次真机就会新增消息 ⇒ C4 随之获得真实评估面。
_CITATION_FIX_BASELINE_ID = 6612

_PASS = 0
_FAIL = 0
_SKIP = 0
_FAILED = []


def check(group, name, ok, detail=""):
    global _PASS, _FAIL
    if ok:
        _PASS += 1
        print("  [PASS] %s %s" % (group, name))
    else:
        _FAIL += 1
        _FAILED.append("%s %s :: %s" % (group, name, detail))
        print("  [FAIL] %s %s :: %s" % (group, name, detail))


def skip(group, name, why=""):
    global _SKIP
    _SKIP += 1
    print("  [SKIP] %s %s :: %s" % (group, name, why))


def _row(sql, args=()):
    if not os.path.exists(DB):
        return None
    try:
        con = sqlite3.connect(DB, timeout=15)
        con.row_factory = sqlite3.Row
        try:
            r = con.execute(sql, args).fetchone()
            return tuple(r) if r else None
        finally:
            con.close()
    except Exception:
        return None


def _rows(sql, args=()):
    """多行查询（与 _row 同款：库不存在/异常一律返回空，不抛）——干净库下走 SKIP。"""
    if not os.path.exists(DB):
        return []
    try:
        con = sqlite3.connect(DB, timeout=15)
        try:
            return [tuple(r) for r in con.execute(sql, args).fetchall()]
        finally:
            con.close()
    except Exception:
        return []


def _src(rel):
    return io.open(os.path.join(_ROOT, rel), encoding="utf-8").read()


# ══════════════════════════════════════════════════════════════════════════
# A. 登记簿行为（纯函数层）
# ══════════════════════════════════════════════════════════════════════════
def group_a():
    from agent.utils import (_register_citations, _citations_payload_from_registry,
                             _citation_key, source_anchor)

    h1 = {"document_id": "d1", "chunk_index": 3,
          "source_doc": "ISO 29148-2011.pdf", "section": "5.2.5 Characteristics",
          "content": "x"}
    h2 = {"document_id": "d2", "chunk_index": 9,
          "source_doc": "Guide to writing Requirements.pdf", "section": "4.2.2 R11",
          "content": "y"}

    # A1：首次登记 ⇒ 从 1 开始
    reg = []
    e = _register_citations(reg, [h1])
    check("A1", "首次登记编号从 1 开始", [x["n"] for x in e] == [1], str(e))

    # A2：同一片段重复登记 ⇒ **复用原编号**，不占新号
    e = _register_citations(reg, [h1])
    check("A2", "同一片段重复登记复用原编号（不占新号）",
          [x["n"] for x in e] == [1] and len(reg) == 1, str(e))

    # A3：新片段接着排
    e = _register_citations(reg, [h2])
    check("A3", "新片段编号接着递增（全局唯一、单调）",
          [x["n"] for x in e] == [2] and len(reg) == 2, str(e))

    # A4：超上限 ⇒ 不分配编号，但**仍返回条目**（内容照样给模型）
    e = _register_citations(reg, [{"document_id": "d3", "chunk_index": 1,
                                   "source_doc": "X.pdf"}], max_total=2)
    check("A4", "超上限：不分配编号但仍返回条目（内容不丢，只是不给可点号）",
          len(e) == 1 and e[0]["n"] is None and e[0]["numbered"] is False, str(e))

    # A5：返回长度必须与 hits 等长（调用方要逐条展示）
    reg2 = []
    e = _register_citations(reg2, [h1, h2, h1])
    check("A5", "返回值与 hits 等长（重复片段也占位）", len(e) == 3, str(len(e)))

    # A6：去重键在 document_id 缺失时退化到 source_doc+chunk_index
    check("A6", "去重键：document_id 缺失 ⇒ 退化到 source_doc+chunk_index",
          _citation_key({"document_id": "", "source_doc": "A.pdf", "chunk_index": 2})
          == _citation_key({"document_id": "", "source_doc": "A.pdf", "chunk_index": 2}),
          "")

    # A7：citations 条数 == 登记簿条数（编号 ⇔ 可点条目 一一对应）
    cits = _citations_payload_from_registry(reg)
    check("A7", "citations 条数 == 登记簿条数（编号与可点条目一一对应）",
          len(cits) == len(reg) == 2, "cits=%d reg=%d" % (len(cits), len(reg)))

    # A8：锚点仍只说真话（不把 chunk 号冒充条款号）
    check("A8", "锚点：有 section 且形如条款号 ⇒ §x.y（不是片段号）",
          source_anchor(h1).startswith("§5.2.5"), source_anchor(h1))
    check("A9", "锚点：无 section ⇒ 片段#N（不冒充 §）",
          source_anchor({"chunk_index": 7}).startswith("片段#"), "")

    # ── A10：登记簿条目**自带锚点**（登记时算，不是靠调用方回写）──
    #   ★ 2026-11-15 真机缺陷（msg 6612）：citations 8 条，anchor **全为 ""**。
    #   根因：`_register_citations` 返回 `dict(_entry)` **副本** ⇒ 调用方写的
    #   `_e["anchor"]=...` 写回不到登记簿 ⇒ payload 读出来永远空。
    #   ⇒ 判据：登记时就算好锚点（单一真源），且返回的是**登记簿同一个对象**。
    reg3 = []
    e = _register_citations(reg3, [h1, h2])
    check("A10", "登记时即算好锚点（登记簿条目自带 anchor，非空）",
          all(x.get("anchor") for x in reg3)
          and reg3[0]["anchor"].startswith("§5.2.5")
          and reg3[1]["anchor"].startswith("§4.2.2"),
          str([x.get("anchor") for x in reg3]))
    check("A11", "返回的是登记簿**同一个对象**（不是副本 ⇒ 调用方写回不丢）",
          e[0] is reg3[0] and e[1] is reg3[1], "返回副本会丢失调用方的写回")

    # ── A12：payload 里的 anchor 必须非空（这条正是真机缺陷的正面判据）──
    cits3 = _citations_payload_from_registry(reg3)
    check("A12", "citations payload 的 anchor 非空（真机 msg 6612 缺陷的正面判据）",
          all(c.get("anchor") for c in cits3),
          str([c.get("anchor") for c in cits3]))

    # ── A13：超上限（未分配编号）的条目也该带锚点 ──
    reg4 = []
    _register_citations(reg4, [h1], max_total=1)
    e4 = _register_citations(reg4, [h2], max_total=1)
    check("A13", "未分配编号的补充材料也带锚点（能跳转，只是不能被引用）",
          e4 and e4[0]["n"] is None and bool(e4[0].get("anchor")), str(e4))

    # ── A14（变异自证）：把「登记时算锚点」去掉 ⇒ A10/A12 必须翻红 ──
    #   用 exec 孪生体（磁盘零改动）：把 source_anchor 调用替换成空串。
    import agent.utils as _U
    _src = _U.__file__
    with io.open(_src, encoding="utf-8") as _f:
        _usrc = _f.read()
    _mut = _usrc.replace('"anchor": source_anchor(h)}', '"anchor": ""}')
    if _mut != _usrc:
        _ns = {}
        exec(compile(_mut, _src, "exec"), _ns)
        _mreg = []
        _ns["_register_citations"](_mreg, [h1])
        _mcits = _ns["_citations_payload_from_registry"](_mreg)
        check("A14", "变异自证：登记时不算锚点 ⇒ A10/A12 必须翻红（守卫有牙齿）",
              not _mreg[0].get("anchor") and not _mcits[0].get("anchor"),
              "变异体仍带锚点 ⇒ 判据抓不到该缺陷")
    else:
        check("A14", "变异自证：登记时不算锚点 ⇒ A10/A12 必须翻红（守卫有牙齿）",
              False, "未能在源码里定位变异点（字符串已变）")


# ══════════════════════════════════════════════════════════════════════════
# B. 接线（初始检索注入 + 工具检索命中读同一本账）
# ══════════════════════════════════════════════════════════════════════════
def group_b():
    ctx = _src("agent/pipeline_parts/context.py")
    tools = _src("agent/pipeline_parts/tools.py")
    stream = read_stream_source()
    execute = _src("agent/pipeline_parts/execute.py")
    common = _src("agent/pipeline_parts/common.py")

    # B1：注入路径必须用登记簿编号（不再自己 enumerate）
    # ⚠️ 匹配串是 `register_citations(`（不带前导下划线）——消费方经 common 再导出取用，
    #   下划线名 `import *` 拿不到（common.py 定义了 __all__ ⇒ 只导出名单内名字）。
    check("B1", "context.py 注入用登记簿分配的编号（不再 enumerate）",
          "register_citations(" in ctx and "【来源{_e['n']}】" in ctx, "")
    check("B2", "context.py 不再用自增序号当【来源n】",
          "【来源{i}】" not in ctx, "旧的 enumerate 写法还在")

    # B3：工具路径必须登记到同一本账
    check("B3", "tools.py 的 graph_retrieve 也登记到登记簿",
          "register_citations(" in tools and "_citation_registry" in tools, "")
    # B4：工具输出里的编号必须来自登记簿（不是自己 enumerate）
    check("B4", "tools.py 工具输出用登记簿编号（不是本地 enumerate）",
          "【来源 {_num}】" in tools and "for _i, _h in enumerate(_top" not in tools, "")

    # B5：工具输出必须自带引用规则（提示词只组装一次，覆盖不到工具命中）
    check("B5", "tools.py 工具输出自带引用规则（§x.y [n] 样式 + 不要给无号材料编号）",
          "引用规则" in tools and "未分配引用序号" in tools, "")

    # B6：两条路径都必须初始化登记簿，且在检索之前
    for name, s in (("stream", stream), ("execute", execute)):
        check("B6", "%s：请求入口初始化引用登记簿" % name,
              "self._citation_registry = []" in s, "")

    # B7：citations 必须走统一入口（不再 6 处各写一份）
    #   ⚠️ 调用点是**不带下划线**的 `citations_for_card(`（common 再导出的别名），
    #   `_citations_for_card(self` 已被 `citations_for_card(self` 取代 ——
    #   注意 `citations_for_card(self` 也是 `_citations_for_card(self` 的子串，
    #   故这里同时断言"下划线版调用为 0"，否则改回下划线也能蒙混过关。
    check("B7", "citations 统一走 citations_for_card（单一入口）",
          common.count("def _citations_for_card") == 1
          and common.count("citations_for_card = _citations_for_card") == 1
          and stream.count("citations_for_card(self") == 3
          and execute.count("citations_for_card(self") == 3
          and stream.count("_citations_for_card(self") == 0
          and execute.count("_citations_for_card(self") == 0, "")
    check("B8", "旧写法不再散落（无残留 _citations_payload(retrieval...)）",
          "_citations_payload(retrieval.get(" not in stream
          and "_citations_payload(retrieval.get(" not in execute, "")

    # ── B11：调用方**不再回写 anchor**（锚点由登记簿在登记时算好）──
    #   判据反转：原写法 `_e["anchor"] = _anchor` 出现过 ⇒ 说明又回到"各算一份"，
    #   而"返回副本"会让这个写回丢失（真机 msg 6612：8 条 citations 的 anchor 全空）。
    check("B11", "调用方不再回写 anchor（锚点由登记簿在登记时算好，单一真源）",
          '_e["anchor"]' not in ctx and '_e["anchor"]' not in tools
          and '_anchor = source_anchor' not in ctx
          and '_anchor = source_anchor' not in tools, "")

    # ── B12：登记簿实现里不得再出现 `dict(_entry)` 式副本返回 ──
    _utils = _src("agent/utils.py")
    check("B12", "登记簿不返回副本（dict(_entry) 会让调用方写回丢失）",
          "out.append(dict(_entry))" not in _utils
          and "_e = dict(_seen[_k])" not in _utils, "")

    # B9：锚点与截断必须只有一份（source_anchor/_short 已上移到 utils）
    check("B9", "source_anchor / _short 只在 utils.py 定义（context.py 不再各写一份）",
          "def source_anchor" not in ctx and "def _short" not in ctx
          and "def source_anchor" in _src("agent/utils.py"), "")
    check("B10", "context.py 仍用得到 source_anchor（从 common 再导出）",
          "source_anchor(c)" in ctx and "source_anchor" in common, "")


# ══════════════════════════════════════════════════════════════════════════
# C. 真机样本（msg 6603）：旧形态 = 工具命中了但 citations 为空
# ══════════════════════════════════════════════════════════════════════════
def group_c():
    row = _row("select card_data from messages where id=6603")
    if not row:
        skip("C1", "真机样本（message 6603）", "干净库无 mbse.db ⇒ 真机组跳过")
        return
    import json
    cd = json.loads(row[0] or "{}")
    check("C1", "真机样本存在（message 6603）", True, "")
    check("C2", "真机症状确认：citations 为空（工具命中却不可点）",
          not (cd.get("citations") or []), "citations=%d" % len(cd.get("citations") or []))
    # C3：工具调用日志证明**确实命中了**
    n_hits = _row("select count(*) from tool_call_logs "
                  "where conversation_id=999999033 and tool_name='graph_retrieve'")
    check("C3", "真机症状确认：该会话有 graph_retrieve 调用（检索确实跑了）",
          bool(n_hits and n_hits[0] >= 1), "n=%s" % (n_hits[0] if n_hits else "?"))

    # ── C4：真机消息的引用条目**内容有效**（只覆盖锚点机制上线后的新产物）──
    #   ★ 2026-11-15 真机缺陷（msg 6612）：citations 有 8 条、编号也不越界，
    #     但**每条 anchor 与 content 全空** ⇒ 用户看到 8 个点不动的条目。
    #     只查"条数/越界"的判据抓不到它 ⇒ 这里逐条查内容有效性。
    #   ⚠️ **只判 id > 基线的新消息**：历史消息（6597/6585/6583 等）是**旧代码产物**，
    #     无法回溯修锚点 ⇒ 拿它们追责会造出"永远红"的假判据。
    #     基线 = 锚点机制上线前的最后一条真机消息（本次实测 6612）。
    import json as _json
    rows = _rows("select id, card_data from messages "
                 "where id > ? and card_data like '%citations%' "
                 "and card_data not like '%\"citations\": []%' "
                 "order by id desc limit 30", (_CITATION_FIX_BASELINE_ID,))
    if not rows:
        skip("C4", "真机消息的引用条目内容有效",
             "锚点机制上线后（id>%d）尚无新产物 ⇒ 跑一次真机后复检"
             % _CITATION_FIX_BASELINE_ID)
        return
    bad_rows = []
    n_checked = 0
    for mid, cds in rows:
        try:
            cs = _json.loads(cds or "{}").get("citations") or []
        except Exception:
            continue
        if not cs:
            continue
        n_checked += 1
        for i, x in enumerate(cs, 1):
            if not (x.get("content") or "").strip() or not (x.get("anchor") or "").strip():
                bad_rows.append("msg %s 第%d条(anchor=%r content_len=%d)"
                                % (mid, i, x.get("anchor"), len(x.get("content") or "")))
                break
    check("C4", "真机消息的引用条目内容有效（每条 content 与 anchor 均非空）",
          n_checked > 0 and not bad_rows,
          ("; ".join(bad_rows[:4]) if bad_rows else "扫了 %d 条新产物" % n_checked))


def main():
    print("=" * 78)
    print("门禁：引用编号单一真源（工具检索命中必须可点，2026-11-15）")
    print("=" * 78)
    for fn in (group_a, group_b, group_c):
        print("\n--- %s ---" % fn.__name__.upper())
        fn()
    print("\n" + "=" * 78)
    print("结果：PASS %d / FAIL %d / SKIP %d" % (_PASS, _FAIL, _SKIP))
    if _FAILED:
        print("红项：")
        for x in _FAILED:
            print("  -", x)
    print("=" * 78)
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
