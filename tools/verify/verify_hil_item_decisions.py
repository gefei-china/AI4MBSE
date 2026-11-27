# -*- coding: utf-8 -*-
"""门禁：HIL 批内逐条决策 —— 已决项不覆盖、未决项可继续、执行不丢项。

背景（2026-11-27）：HIL 确认卡一批可 N 项（真库 35/99 批 >1，最大 41 项），
但只有「整批批准/整批拒绝」，batch_items 未平铺、无 item 级决策字段。
用户要「逐条确认且答案不丢」。

判据：
  A 逐条决策：approve/reject 写进对应 item，已决项不覆盖；
  B 状态机：部分已决 = partial，全部已决 = approved；可再次进入处理剩余项；
  C 执行过滤：reject 项跳过、已执行项跳过（幂等）、缺省视为 approve（兼容老数据）；
  D 前端：平铺 batch_items、逐项按钮、已决只读；
  E 变异自证：去掉「已决跳过」后 A 必须翻红。

用法：python tools/verify/verify_hil_item_decisions.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from services.hil_service import HILService, DECIDABLE, STATUSES

RESULTS = []


def check(group, name, cond, detail=""):
    RESULTS.append((group, name, bool(cond), detail))


_DDL = """
CREATE TABLE hil_confirmations (
  id INTEGER PRIMARY KEY AUTOINCREMENT, agent_id TEXT DEFAULT '', conversation_id INTEGER DEFAULT 0,
  run_id INTEGER DEFAULT 0, action TEXT, payload TEXT DEFAULT '{}', preview TEXT DEFAULT '',
  status TEXT DEFAULT 'pending', decided_by TEXT DEFAULT '', decided_at TEXT DEFAULT '',
  created_at TEXT DEFAULT CURRENT_TIMESTAMP, task_key TEXT DEFAULT '', idem_key TEXT DEFAULT '',
  tool_type TEXT DEFAULT '', level TEXT DEFAULT '', batch_id TEXT DEFAULT '',
  batch_count INTEGER DEFAULT 1, timeout_at TEXT DEFAULT '', result_json TEXT DEFAULT '',
  exec_idem_key TEXT DEFAULT '', batch_items TEXT DEFAULT '[]', requested_by TEXT DEFAULT ''
);
"""


def _fixture(n=3):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(_DDL)
    items = [{"tool": "save_artifact", "preview": "第%d项" % (i + 1),
              "target": "T%d" % (i + 1), "args": {"name": "n%d" % i},
              "decision": "", "decided_by": "", "decided_at": "", "result": ""}
             for i in range(n)]
    c.execute("INSERT INTO hil_confirmations (id,action,batch_items,batch_count,status) "
              "VALUES (1,'save_artifact',?,?,'pending')",
              (json.dumps(items, ensure_ascii=False), n))
    c.commit()
    return c, items


def group_a():
    check("A", "状态机含 partial", "partial" in STATUSES)
    check("A", "partial 可再次决策", "partial" in DECIDABLE)
    c, _ = _fixture()
    r = HILService.decide_items(c, 1, [{"item_index": 0, "approve": True}], "tester")
    check("A", "逐项批准第 1 项 ⇒ partial", r["status"] == "partial", str(r)[:200])
    row = HILService.get(c, 1)
    items = json.loads(row["batch_items"])
    check("A", "★ 第 1 项 decision=approve", items[0]["decision"] == "approve")
    check("A", "第 2/3 项仍未决（不误伤）",
          items[1]["decision"] == "" and items[2]["decision"] == "")
    # 已决项再提交不同决定 ⇒ 不覆盖
    r2 = HILService.decide_items(c, 1, [{"item_index": 0, "approve": False}], "other")
    items2 = json.loads(HILService.get(c, 1)["batch_items"])
    check("A", "★ 已决项不被覆盖（答案不丢）",
          items2[0]["decision"] == "approve" and items2[0]["decided_by"] == "tester",
          json.dumps(items2[0], ensure_ascii=False))
    check("A", "已决跳过有可打印证据",
          any(s.get("reason") == "此前已决，不覆盖" for s in (r2.get("skipped") or [])),
          str(r2.get("skipped")))
    # 处理剩余 2 项 ⇒ 全部已决 = approved
    r3 = HILService.decide_items(c, 1, [
        {"item_index": 1, "approve": True}, {"item_index": 2, "approve": False}], "tester")
    check("A", "剩余项处理完 ⇒ approved", r3["status"] == "approved", str(r3)[:200])
    items3 = json.loads(HILService.get(c, 1)["batch_items"])
    check("A", "三项决定齐全", [x["decision"] for x in items3] == ["approve", "approve", "reject"])
    c.close()


def group_b():
    c, _ = _fixture(2)
    r = HILService.decide_items(c, 1, [
        {"item_index": 0, "approve": True}, {"item_index": 1, "approve": False}], "tester")
    check("B", "全批决策 ⇒ approved（不是 partial）", r["status"] == "approved")
    # 非法索引不写坏数据
    c2, _ = _fixture(1)
    r2 = HILService.decide_items(c2, 1, [{"item_index": 99, "approve": True}], "tester")
    check("B", "越界索引被拒且不落库",
          r2["status"] in ("pending", "invalid_decisions", "no_items")
          or (r2.get("skipped") and not r2.get("applied")), str(r2)[:200])
    c.close()
    c2.close()


def group_c():
    src = open(os.path.join(_ROOT, "services", "hil_service.py"), encoding="utf-8").read()
    check("C", "★ 执行器跳过 reject 项",
          '== "reject"' in src and "continue" in src)
    check("C", "★ 执行器跳过已执行项（item 幂等）",
          'if str(_it.get("result") or "").strip():' in src)
    check("C", "缺省视为 approve（老数据兼容）",
          "缺省（老数据无 decision）视为 approve" in src)
    check("C", "逐项回执写回 batch_items", '"item_index": _t.get("item_index", -1)' in src)
    api = open(os.path.join(_ROOT, "routers", "studio_parts", "flows.py"), encoding="utf-8").read()
    check("C", "API 按 decisions 分流到 decide_items", "HILService.decide_items" in api)
    check("C", "partial 也触发执行", 'in ("approved", "partial")' in api)


def group_d():
    js = open(os.path.join(_ROOT, "static", "js", "mods", "06-cards.js"), encoding="utf-8").read()
    check("D", "★ 确认卡平铺 batch_items", "c.batch_items" in js and "_perItem" in js)
    check("D", "逐项按钮带 item_index", "hilInlineDecide(${cid}, true, ${i})" in js)
    check("D", "已决项只读展示（无按钮）", "已执行" in js and "已拒绝" in js)
    flow = open(os.path.join(_ROOT, "static", "js", "mods", "11-pipeline.js"), encoding="utf-8").read()
    check("D", "hilInlineDecide 支持 itemIndex", "function hilInlineDecide(cid, approve, itemIndex)" in flow)
    check("D", "逐项请求带 decisions 数组", "_body.decisions = [{item_index: _idx" in flow)
    # 精确锚点门禁 verify_hil_mutation.js 的两行模板必须仍在
    # 注意：verify_hil_mutation.js 的 lineKind 锚点写的是 `_kindTxt`，
    # 产品代码实际是 `kindTxt` —— 该门禁在 HEAD 上本就红（基线已核），
    # 这里只断言我们没进一步破坏「影响面」行与「性质」行的存在性。
    check("D", "既有变异门禁锚点未被进一步破坏",
          "<div><b>影响面：</b>${esc(prev)}</div>" in js
          and "<div><b>性质：</b>${" in js)


def group_e():
    src = open(os.path.join(_ROOT, "services", "hil_service.py"), encoding="utf-8").read()
    anchor = '            if str(items[idx].get("decision") or "").strip():\n                skipped.append({"item_index": idx, "reason": "此前已决，不覆盖"})'
    if src.count(anchor) != 1:
        check("E", "变异锚点唯一", False, "count=%d" % src.count(anchor))
        return
    check("E", "变异锚点唯一", True)
    mutated = src.replace(anchor, '            if False:\n                skipped.append({"item_index": idx, "reason": "此前已决，不覆盖"})', 1)
    check("E", "变异体真的变了", mutated != src)
    import importlib.util, tempfile
    tmp = os.path.join(tempfile.gettempdir(), "_hil_mut_item.py")
    open(tmp, "w", encoding="utf-8").write(mutated)
    spec = importlib.util.spec_from_file_location("_hil_mut_item", tmp)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # 用 2 项批次：先决第 1 项 ⇒ partial（仍可再次进入），变异体重决第 1 项
    c, _ = _fixture(2)
    mod.HILService.decide_items(c, 1, [{"item_index": 0, "approve": True}], "tester")
    r = mod.HILService.decide_items(c, 1, [{"item_index": 0, "approve": False}], "other")
    items = json.loads(mod.HILService.get(c, 1)["batch_items"])
    check("E", "★ 去掉已决跳过后决定被覆盖 ⇒ A 有牙齿",
          items[0]["decision"] == "reject", json.dumps(items[0], ensure_ascii=False))
    c.close()
    try:
        os.remove(tmp)
    except OSError:
        pass


def main():
    for fn in (group_a, group_b, group_c, group_d, group_e):
        try:
            fn()
        except Exception as e:
            check(fn.__name__, "组执行异常", False, repr(e))
    w = max(len(n) for _, n, _, _ in RESULTS)
    print("=" * (w + 16))
    print("HIL 批内逐条决策门禁（答案不丢 · 不重复执行）")
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
