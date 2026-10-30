"""门禁：会话 9726「目标包明明查到了却说没找到」三项修复（2026-10-28）。

## 被验的真实故障（全部取自 conv 9726 的真机回执，不造数据）

用户要求「分析并生成通信子系统时序图，校验后写入包 `SD_通信子系统时序图`」，
会话以「该包在工程里不存在，需要新建」收尾。真实情况是**同一轮已经查到它两次**：

| 调用 | 回执 | 说明 |
|---|---|---|
| `query_data_tree`(#5503) parentId=通信子系统 | 7 个子包，第 2 个就是 `SD_通信子系统时序图`(2115359040604536832) | 命中了 |
| `search_model_elements`(#5510) | 命中同一 ID | 又命中一次 |
| `query_data_tree`(#5512) parentId=2115359040604536832 | `items: []` | 它是**空包**（正常） |
| `query_data_tree`(#5513/#5514) | 与 #5501/#5502 **同义重查**（只差 pageSize） | 白烧 2 次调用 |

四组判据：
  A. 工具回执判读句（空 ≠ 不存在 / 命中即存在 / 0 命中 ≠ 不存在）
  B. 语义级重复调用短路（忽略 pageSize 等翻页参数；未取全则不短路）
  C. 预算降级闸顺序（预算耗尽时必须给"待续清单"而不是硬停）
  D. 闸门生效值可见（"我以为开了"必须能被卡片证伪）

## 运行

    python tools/verify/verify_conv9726_fixes.py

退出码 0 = 全绿；1 = 有红项（红项会逐条打印）。
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_PASS = 0
_FAIL = 0
_FAILED = []


def check(group: str, name: str, ok: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if ok:
        _PASS += 1
        print(f"  [PASS] {group} {name}")
    else:
        _FAIL += 1
        _FAILED.append(f"{group} {name} :: {detail}")
        print(f"  [FAIL] {group} {name} :: {detail}")


def _db():
    p = os.path.join(_ROOT, "mbse.db")
    return sqlite3.connect("file:%s?mode=ro" % p.replace("\\", "/"), uri=True)


def real_call(cid: int):
    """取一条真实工具调用 (工具名, 参数dict, 回执原文)。"""
    with _db() as c:
        row = c.execute("select tool_name,arguments,result from tool_call_logs "
                        "where id=?", (cid,)).fetchone()
    if not row:
        raise SystemExit(f"缺少真机样本 tool_call_logs.id={cid}")
    return row[0], json.loads(row[1]), row[2]


def _res(text):
    return {"ok": True, "result": text}


# ══════════════════════════════════════════════════════════════════════════
# A. 工具回执判读句
# ══════════════════════════════════════════════════════════════════════════
def group_a():
    from agent import tool_result_notes as N

    # A1：#5503 真机回执 —— 7 个子包里就有目标包，判读句必须点名它 + 给出 id
    n, a, r = real_call(5503)
    out = N.annotate(n, a, _res(r))
    note = str(out.get("note") or "")
    check("A1", "#5503 真机：判读句点名目标包并给出 id",
          ("SD_通信子系统时序图" in note) and ("2115359040604536832" in note)
          and ("确实存在" in note), note[:160])

    # A2：#5512 空包回执 —— 必须讲清"空 ≠ 不存在"（这是本次事故的**核心误读**）
    n, a, r = real_call(5512)
    note = str(N.annotate(n, a, _res(r)).get("note") or "")
    check("A2", "#5512 真机：空包回执给出「空 ≠ 不存在」",
          ("空包" in note) and ("空 ≠ 不存在" in note), note[:160])

    # A3：原文必须**逐字保留**（审计口径不能被改写）
    out3 = N.annotate(n, a, _res(r))
    check("A3", "判读句只追加、不改写 result 原文",
          str(out3["result"]).startswith(r), str(out3["result"])[:80])

    # A4：#5509 本层 keyword 过滤 0 命中 —— 不能说成"不存在"
    n, a, r = real_call(5509)
    note = str(N.annotate(n, a, _res(r)).get("note") or "")
    check("A4", "#5509 真机：本层过滤 0 命中标注为「本层未命中」",
          ("本层过滤未命中" in note) and ("搜索" not in note.split("下一步")[0][:8]),
          note[:160])

    # A5：#5497 query_projects 0 命中 —— 必须给"换更短关键词"，不许直接说找不到
    n, a, r = real_call(5497)
    note = str(N.annotate(n, a, _res(r)).get("note") or "")
    check("A5", "#5497 真机：工程 0 命中给出「换更短关键词」指引",
          ("更短的关键词" in note), note[:160])

    # A6：#5498 命中 3 个工程 —— 列出名字（模型不必再逐字比对 JSON）
    n, a, r = real_call(5498)
    note = str(N.annotate(n, a, _res(r)).get("note") or "")
    check("A6", "#5498 真机：命中结果压成一行清单",
          ("广汽方法论建模1022" in note) and ("3 个工程" in note), note[:160])

    # A7：失败回执不注解（不编造结论）
    out7 = N.annotate("query_data_tree", {"parentId": "X"}, {"ok": False, "result": '{"items":[]}'})
    check("A7", "ok=False 时不注解（不编造结论）",
          ("note" not in out7) and (out7["result"] == '{"items":[]}'), str(out7))

    # A8：非目标工具原样透传
    out8 = N.annotate("graph_retrieve", {"query": "x"}, _res('{"items":[]}'))
    check("A8", "非目标工具原样透传", "note" not in out8, str(out8)[:80])

    # A9：解析不出 JSON 时原样透传（旁路原则）
    out9 = N.annotate("query_data_tree", {"parentId": "X"}, _res("not json at all"))
    check("A9", "回执非 JSON 时不注解", "note" not in out9, str(out9)[:80])

    # A10 变异自证：把"空包"分支退化成旧行为（不加判读）⇒ A2 判据必须失败
    #   ⚠️ 变异必须打在**分发表 `_NOTES`** 上，不能只改 `N._note_tree`：
    #      模块级 `_NOTES` 在 import 时就绑定了原函数对象，
    #      改同名的模块属性对它**毫无影响** ⇒ 变异不生效，
    #      "变异后仍通过"会被误读成"判据没牙"（本仓已栽过同形的坑）。
    _orig = N._NOTES["query_data_tree"]

    def _mut(args, d):
        if not (d.get("items") or []):
            return ""          # 变异：空结果不解释（= 修复前的世界）
        return _orig(args, d)

    N._NOTES["query_data_tree"] = _mut
    try:
        n, a, r = real_call(5512)
        mut_note = str(N.annotate(n, a, _res(r)).get("note") or "")
        ok_a2 = ("空包" in mut_note) and ("空 ≠ 不存在" in mut_note)
    finally:
        N._NOTES["query_data_tree"] = _orig
    check("A10", "变异自证：空包分支被退化后 A2 必须判红",
          (not ok_a2) and (mut_note == ""), f"mut_note={mut_note[:100]!r}")

    # A11 接线自证：_exec_tool_call 必须经注解（两路共用的唯一出口）
    src = open(os.path.join(_ROOT, "agent", "pipeline_parts", "tools.py"),
               encoding="utf-8").read()
    check("A11", "_exec_tool_call 出口已接注解",
          "tool_result_notes as _notes" in src and "_notes.annotate(" in src, "tools.py 未见接线")


# ══════════════════════════════════════════════════════════════════════════
# B. 语义级重复调用短路
# ══════════════════════════════════════════════════════════════════════════
def group_b():
    from agent.loop_guard import (LoopGuard, call_key, strict_call_key,
                                  DEDUP_IGNORE_KEYS)

    # B1：真机 #5501 vs #5513（只差 pageSize）必须同键
    _, a1, _ = real_call(5501)
    _, a2, _ = real_call(5513)
    check("B1", "#5501/#5513 真机参数：语义键相同",
          call_key("query_data_tree", a1) == call_key("query_data_tree", a2),
          "两键不同 ⇒ 短路仍失效")
    check("B2", "#5501/#5513 严格键仍可区分（排障用）",
          strict_call_key("query_data_tree", a1) != strict_call_key("query_data_tree", a2),
          "严格键被合并 ⇒ 丢失'到底调了几次不同参数'的口径")

    # B3：真机 #5502 vs #5514 同键
    _, b1, _ = real_call(5502)
    _, b2, _ = real_call(5514)
    check("B3", "#5502/#5514 真机参数：语义键相同",
          call_key("query_data_tree", b1) == call_key("query_data_tree", b2), "")

    # B4：不同父节点**不得**被合并（否则会拿错子树冒充答案）
    _, c1, _ = real_call(5503)
    check("B4", "不同 parentId 语义键不同",
          call_key("query_data_tree", b1) != call_key("query_data_tree", c1), "")

    # B5：真实护栏行为 —— 第一次执行、第二次（改 pageSize）必须短路
    lg = LoopGuard(token_budget=0)
    n, a, r = real_call(5501)
    first = lg.probe_dup(n, a)
    lg.call(lambda nm, ar: {"ok": True, "result": r}, n, a, weak=False)
    second = lg.probe_dup(n, a2)          # 只多了 pageSize=100
    check("B5", "护栏：同义重查被短路（首次 0 / 二次命中）",
          first == 0 and second >= 2, f"first={first} second={second}")
    rp = lg.replay_payload(n, a2, second or 2)
    check("B6", "短路回喂带「重复调用已短路」说明且保留原结果",
          ("重复调用已短路" in str(rp.get("result"))) and ("items" in str(rp.get("result"))),
          str(rp)[:120])
    check("B7", "短路回喂提醒不要据此判「不存在」",
          "不存在" in str(rp.get("result")), "")

    # B8：上次**没取全**（hasMore=true）⇒ 不许短路（重跑是对的）
    lg2 = LoopGuard(token_budget=0)
    partial = json.dumps({"items": [{"name": "a"}], "total": "5", "hasMore": True},
                         ensure_ascii=False)
    lg2.call(lambda nm, ar: {"ok": True, "result": partial}, "query_data_tree", a)
    lg2.call(lambda nm, ar: {"ok": True, "result": partial}, "query_data_tree", a)
    check("B8", "上次未取全（hasMore=true）⇒ 不短路",
          lg2.probe_dup("query_data_tree", a) == 0, "半截结果被当成答案的风险")

    # B9：文本类工具（无 hasMore 字段）仍能短路（不得把老功能废掉）
    lg3 = LoopGuard(token_budget=0)
    lg3.call(lambda nm, ar: {"ok": True, "result": "纯文本结论"}, "graph_retrieve", {"query": "q"})
    check("B9", "文本类工具仍参与短路（未误伤老功能）",
          lg3.probe_dup("graph_retrieve", {"query": "q"}) >= 2, "")

    # B10 变异自证：清空忽略键 ⇒ B1/B5 必须判红
    import agent.loop_guard as LG
    _saved = LG.DEDUP_IGNORE_KEYS
    LG.DEDUP_IGNORE_KEYS = frozenset()
    try:
        mut_same = (LG.call_key("query_data_tree", a1) == LG.call_key("query_data_tree", a2))
        lg4 = LoopGuard(token_budget=0)
        lg4.call(lambda nm, ar: {"ok": True, "result": r}, n, a, weak=False)
        mut_dup = lg4.probe_dup(n, a2)
    finally:
        LG.DEDUP_IGNORE_KEYS = _saved
    check("B10", "变异自证：忽略键失效后同义重查不再被短路",
          (not mut_same) and mut_dup == 0, f"same={mut_same} dup={mut_dup}")

    check("B11", "忽略键集合覆盖分页与票据参数",
          {"pageSize", "pageNum", "includeReadToken"} <= set(DEDUP_IGNORE_KEYS),
          str(sorted(DEDUP_IGNORE_KEYS)))


# ══════════════════════════════════════════════════════════════════════════
# C. 预算降级闸顺序
# ══════════════════════════════════════════════════════════════════════════
def group_c():
    from agent.auto_continue import decide, build_degrade_notice

    v = {"done": False, "next_step": "跑 sysml_v2_validate"}
    # C1：真机现场 —— 预算已耗尽(105%) 且预算闸已关 ⇒ 必须给「降级收尾」而不是硬停
    cont, why = decide(v, used=1, max_used=8, seen_steps=[],
                       budget_left=False, budget_ratio=1.05)
    check("C1", "预算耗尽时给「降级收尾」（不是无声硬停）",
          (not cont) and ("降级收尾" in why), why)

    # C2：70% 阈值内正常推进（不得因修复误伤）
    cont2, why2 = decide(v, used=1, max_used=8, seen_steps=[],
                         budget_left=True, budget_ratio=0.3)
    check("C2", "预算充裕时仍正常续跑", cont2 and ("自动继续" in why2), why2)

    # C3：已完成优先于预算（已做完就不必提预算）
    cont3, why3 = decide({"done": True}, used=1, max_used=8, seen_steps=[],
                         budget_left=False, budget_ratio=1.2)
    check("C3", "已完成优先于预算降级", (not cont3) and ("已完成" in why3), why3)

    # C4：降级通知必须含三要素（为什么停 / 还差什么 / 怎么继续）
    nt = build_degrade_notice(["跑校验", "写入平台"], 1.05, 0.7)
    check("C4", "待续清单含三要素",
          ("分段收尾" in nt) and ("待续事项" in nt) and ("继续" in nt)
          and ("跑校验" in nt) and ("写入平台" in nt), nt[:120])

    # C5：接线 —— stream.py 在「降级收尾」时把清单写进答复
    src = open(os.path.join(_ROOT, "agent", "pipeline_parts", "stream.py"),
               encoding="utf-8").read()
    check("C5", "stream.py 已接「降级收尾 ⇒ 写待续清单」",
          'build_degrade_notice' in src and '降级收尾' in src, "未见接线")

    # C6 变异自证：把顺序换回去（硬停闸在前）⇒ C1 必须判红
    def _decide_old(verdict, *, used, max_used, seen_steps,
                    deadline_left_s=None, budget_left=True,
                    budget_ratio=0.0, degrade_ratio=0.7):
        if verdict and not verdict.get("done") and not budget_left:
            return False, "token 预算已耗尽，停止续跑"
        return decide(verdict, used=used, max_used=max_used, seen_steps=seen_steps,
                      deadline_left_s=deadline_left_s, budget_left=budget_left,
                      budget_ratio=budget_ratio, degrade_ratio=degrade_ratio)

    _, why_old = _decide_old(v, used=1, max_used=8, seen_steps=[],
                             budget_left=False, budget_ratio=1.05)
    check("C6", "变异自证：顺序回退后 C1 必须判红", "降级收尾" not in why_old, why_old)


# ══════════════════════════════════════════════════════════════════════════
# D. 闸门生效值可见 + 开关即时落盘
# ══════════════════════════════════════════════════════════════════════════
def group_d():
    from agent.pipeline_parts.common import gates_snapshot

    g = gates_snapshot()
    check("D1", "闸门快照含三项开关 + 配置文件路径",
          all(k in g for k in ("todo_enabled", "harness_enabled",
                               "obstacle_recovery_enabled", "config_path"))
          and bool(g.get("config_path")), str(g))
    check("D2", "闸门值是真布尔（不是字符串，避免 norm 歧义）",
          isinstance(g.get("todo_enabled"), bool)
          and isinstance(g.get("harness_enabled"), bool), str(g)[:120])

    src = open(os.path.join(_ROOT, "agent", "pipeline_parts", "stream.py"),
               encoding="utf-8").read()
    check("D3", "卡片落库带 gates（会话内可证伪「我以为开了」）",
          '"gates": _gates_safe()' in src, "stream.py 未见 gates 字段")
    # D3b：gates 装配必须 fail-safe（诊断信息不得带崩落库）
    #   实证：第一版写错函数名 ⇒ conv 9728 落库 NameError ⇒ 卡片退化成 `[]`，
    #   只能靠 _persist_partial_stream 兜底存正文。
    check("D3b", "gates 装配包在 try 里（永不抛）",
          "def _gates_safe():" in src and "return gates_snapshot()" in src,
          "未见 _gates_safe 兜底")

    js = open(os.path.join(_ROOT, "static", "js", "mods", "35-ctxconfig.js"),
              encoding="utf-8").read()
    check("D4", "布尔开关 onchange 改为即时保存",
          "onchange=\"ctxAutoSaveBool(this)\"" in js, "仍是 ctxRecheck 只做预览")
    check("D5", "即时保存后**回读**校验（不信 PUT 的 200）",
          "回读不一致" in js and "api('/api/system/config')" in js, "")
    check("D6", "保存失败回退界面值（不显示未生效状态）",
          "data-prev" in js and "el.value = prev ? '1' : '0'" in js, "")
    check("D7", "全局保存同样回读核对",
          "回读核对" in js, "")
    # D8：开关真的能被打开（对真配置做一次幂等写：写回当前值，不改变语义）
    try:
        from core import config as _cfg
        cur = bool(_cfg.as_bool("agent", "todo_enabled", False))
        _cfg.save_override({"agent.todo_enabled": cur})
        again = bool(_cfg.as_bool("agent", "todo_enabled", False))
        check("D8", "配置写-读闭环一致（落盘+reload 可回读）", again == cur,
              f"写前={cur} 写后={again}")
    except Exception as e:
        check("D8", "配置写-读闭环一致（落盘+reload 可回读）", False, repr(e))


def main():
    print("=" * 78)
    print("门禁：conv 9726 三项修复（判读句 / 语义去重 / 降级顺序 / 闸门可见）")
    print("=" * 78)
    for fn in (group_a, group_b, group_c, group_d):
        print(f"\n--- {fn.__name__.upper()} ---")
        fn()
    print("\n" + "=" * 78)
    print(f"结果：PASS {_PASS} / FAIL {_FAIL}")
    if _FAILED:
        print("红项：")
        for x in _FAILED:
            print("  -", x)
    print("=" * 78)
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
