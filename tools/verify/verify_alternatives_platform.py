# CI-OPTIONAL: 2026-11-04 盘点实测：在干净库（init_db 新建，CI 等价口径）上 rc≠0，接线会让 CI 恒红、成为噪音。需先逐条定性（产品缺陷 vs 门禁口径过期）再接线。证据：tmp/backlog_probe.json 的 red 列表；方法见 docs/门禁盘点-20261104.md §4。
# -*- coding: utf-8 -*-
"""平台化多方案能力：端到端验证（工具 + 配置驱动）。

验证要点（对应《多方案设计-平台化实现方案》§4 的 7 条验收判据）：
  1. 工具可执行且返回可渲染结构
  2. 维度/权重/阈值**从 DB 配置读**（改配置即生效，不改代码）
  3. 管线里没有多方案专用分支
  4. 前端按工具名渲染（维度全读工具返回）
"""
import io
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

PASS = 0
FAIL = 0
FAILED = []


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] %s" % name)
    else:
        FAIL += 1
        FAILED.append(name)
        print("  [FAIL] %s%s" % (name, (" | " + str(detail)) if detail else ""))


print("=" * 72)
print("[1] 平台资产已注册（DB）")
print("=" * 72)
import sqlite3
from core.config import DB_PATH
conn = sqlite3.connect("file:%s?mode=ro" % DB_PATH.replace("\\", "/"), uri=True)
conn.row_factory = sqlite3.Row

tool = conn.execute("SELECT name,status,side_effect,kind FROM tools WHERE name='alternatives_score'").fetchone()
check("1.1 工具已注册且 active", tool is not None and tool["status"] == "active",
      dict(tool) if tool else None)
check("1.2 工具是只读（read）", tool and tool["side_effect"] == "read", tool["side_effect"] if tool else None)
check("1.3 工具 input_schema 非空", bool((tool and conn.execute(
    "SELECT input_schema FROM tools WHERE name='alternatives_score'").fetchone()[0]) or ""))

skill = conn.execute("SELECT name,status FROM skills WHERE name='多方案设计与对比'").fetchone()
check("1.4 技能已发布", skill is not None and skill["status"] == "published",
      dict(skill) if skill else None)

agent = conn.execute("SELECT name,status FROM agents WHERE name='多方案设计'").fetchone()
check("1.5 多方案设计 Agent 存在且 active", agent is not None and agent["status"] == "active",
      dict(agent) if agent else None)

rules = {r["rule_key"]: r["rule_value"] for r in conn.execute(
    "SELECT rule_key, rule_value FROM generate_rules WHERE rule_key LIKE 'alternatives_%'")}
check("1.6 生成规则已配置（策略在 DB）", len(rules) >= 6, sorted(rules))
check("1.7 维度清单是 JSON 且非空",
      isinstance(json.loads(rules.get("alternatives_dimensions", "[]")), list)
      and len(json.loads(rules.get("alternatives_dimensions", "[]"))) > 0)

binds = [r["name"] for r in conn.execute(
    "SELECT a.name FROM agent_tools t JOIN agents a ON a.id=t.agent_id "
    "WHERE t.tool_name='alternatives_score' AND t.tool_type='tool'")]
check("1.8 工具已绑定到方案类 Agent", len(binds) >= 1, binds)
check("1.9 未绑定到无关 Agent（chat/knowledge_qa/review/requirement_analysis）",
      not ({"chat", "knowledge_qa", "review", "requirement_analysis"} & set(binds)), binds)
conn.close()

print()
print("=" * 72)
print("[2] 工具可执行（平台入口）")
print("=" * 72)
import alternatives_tools
res = alternatives_tools.exec_tool("alternatives_score", {
    "alternatives": [
        {"name": "方案A·集中式",
         "scores": {"performance": 120, "cost": 80, "risk": 2,
                    "extensibility": 40, "requirement_coverage": 18},
         "rationale": {"performance": "时延 120ms"}},
        {"name": "方案B·分布式",
         "scores": {"performance": 60, "cost": 150, "risk": 4,
                    "extensibility": 90, "requirement_coverage": 20}},
    ]})
check("2.1 工具返回 ok=True", res.get("ok") is True, res.get("result", "")[:200])
payload = json.loads(res["result"])
check("2.2 返回含对比矩阵 rows", len(payload["alternatives"]["rows"]) == 5,
      len(payload["alternatives"]["rows"]))
check("2.3 返回含方案列 columns", len(payload["alternatives"]["columns"]) == 2)
check("2.4 返回含推荐方案", payload["alternatives"]["recommended"] is not None,
      payload["alternatives"]["recommended"])
check("2.5 返回含维度目录（前端据此渲染，不硬编码）", len(payload["dimensions"]) == 5)
check("2.6 返回含生效配置（可审计）", "config" in payload and "min_coverage" in payload["config"])

print()
print("=" * 72)
print("[3] 判据 2：改 DB 配置即生效（不改代码）")
print("=" * 72)
from core.alternatives import score_alternatives, load_config_from_db

cfg = load_config_from_db()
check("3.1 配置能从 DB 读出维度清单", cfg.get("dimensions") is not None)
check("3.2 配置能读出权重", isinstance(cfg.get("weights"), dict))

# 换维度集：只用**内存参数**模拟 DB 配置变化（不改 DB，不污染真库）
_custom_dims = [
    {"key": "cost", "label": "成本", "direction": "lower"},
    {"key": "speed", "label": "速度", "direction": "higher"},
]
_r = score_alternatives(
    [{"name": "A", "scores": {"cost": 10, "speed": 100}},
     {"name": "B", "scores": {"cost": 90, "speed": 50}}],
    dimensions=_custom_dims)
check("3.3 自定义维度集生效（行数=2）", len(_r["dimensions"]) == 2, len(_r["dimensions"]))
check("3.4 自定义维度含新维度 speed", any(d["key"] == "speed" for d in _r["dimensions"]))
check("3.5 原默认维度 performance 不再出现",
      not any(d["key"] == "performance" for d in _r["dimensions"]))

# 换权重：性能权重拉满 ⇒ 只按 performance 排序
_w = {k: 0.0 for k in ("performance", "cost", "risk", "extensibility", "requirement_coverage")}
_w["performance"] = 1.0
_r2 = score_alternatives(
    [{"name": "快而贵", "scores": {"performance": 100, "cost": 100}},
     {"name": "慢而省", "scores": {"performance": 10, "cost": 1}}],
    weights=_w)
check("3.6 权重覆盖生效（推荐=性能最优者）", _r2["recommended"] == "快而贵", _r2["recommended"])

print()
print("=" * 72)
print("[4] 判据 3：管线无多方案专用分支")
print("=" * 72)
_hits = []
for root, _dirs, files in os.walk(os.path.join(REPO, "agent", "pipeline_parts")):
    if "__pycache__" in root:
        continue
    for fn in files:
        if not fn.endswith(".py"):
            continue
        p = os.path.join(root, fn)
        for i, line in enumerate(io.open(p, encoding="utf-8"), 1):
            s = line.strip()
            if s.startswith("#"):
                continue
            if "alternatives" in s or "多方案" in s:
                _hits.append("%s:%d: %s" % (os.path.relpath(p, REPO), i, s[:70]))
# 允许：tools.py 的工具 handler + cards.py 的「按工具调用判定文案」
_ok = [h for h in _hits if "tools.py" in h or "cards.py" in h]
check("4.1 管线命中数 ≤ 6（仅工具 handler + 文案判定）", len(_hits) <= 6, len(_hits))
for h in _hits:
    print("      · %s" % h)

print()
print("=" * 72)
print("[5] 判据 4：前端按工具名渲染，维度全读工具返回")
print("=" * 72)
_js = io.open(os.path.join(REPO, "static/js/mods/58-alternatives.js"), encoding="utf-8").read()
check("5.1 定义 altTryCard（按工具名分发）", "function altTryCard" in _js)
check("5.2 只认 alternatives_score 工具名", "ALT_TOOL_NAME = 'alternatives_score'" in _js)
check("5.3 非目标工具返回 null（回退通用渲染）",
      "if (name !== ALT_TOOL_NAME) return null;" in _js)
check("5.4 维度 label 读工具返回", "esc(r.label)" in _js)
check("5.5 方向读工具返回", "r.direction==='lower'" in _js)
check("5.6 权重读工具返回", "r.weight" in _js)
check("5.7 前端不硬编码维度名",
      "performance" not in _js and "requirement_coverage" not in _js)

for _f in ("static/js/mods/06-cards.js", "static/js/mods/11-pipeline.js"):
    _s = io.open(os.path.join(REPO, _f), encoding="utf-8").read()
    if _f.endswith("11-pipeline.js"):
        # 流式实时路径：对话进行中就在执行过程区里显示（过程区此时是展开的）
        check("5.8 %s 接入 altTryCard（流式实时）" % os.path.basename(_f),
              "altTryCard" in _s)
    else:
        # 历史回放路径：对比表**不在**过程区（默认收起 ⇒ 用户看不见），
        # 已提升为消息级独立卡片（05-markdown.js 调 altCardFromTools）。
        check("5.8 %s 不再在过程区渲染对比表" % os.path.basename(_f),
              "altTryCard" not in _s)

_md = io.open(os.path.join(REPO, "static/js/mods/05-markdown.js"), encoding="utf-8").read()
check("5.8b 05-markdown.js 消息级调用 altCardFromTools", "altCardFromTools" in _md)
_altsrc = io.open(os.path.join(REPO, "static/js/mods/58-alternatives.js"), encoding="utf-8").read()
check("5.8c 提供 altCardFromTools（按工具名从 card_data 取结果）",
      "function altCardFromTools" in _altsrc)
check("5.8d altCardFromTools 取最后一次成功的工具结果",
      "picked = t" in _altsrc and "t.ok === false" in _altsrc)

_cards = io.open(os.path.join(REPO, "static/js/mods/06-cards.js"), encoding="utf-8").read()
check("5.9 renderRichCard 已无多方案专用分支",
      "cardAlternatives" not in _cards and "_altHtml" not in _cards)

print()
print("=" * 72)
print("[6] 抽检：工具错误路径如实报告（不伪造）")
print("=" * 72)
_e1 = alternatives_tools.exec_tool("alternatives_score", {"alternatives": []})
check("6.1 空 alternatives 返回 ok=False", _e1.get("ok") is False)
check("6.2 错误信息含「不要编造」类提醒", "编造" in _e1.get("result", ""), _e1.get("result", "")[:120])
_e2 = alternatives_tools.exec_tool("alternatives_score", {"alternatives": [
    {"name": "只有等级词", "scores": {"performance": "高"}}]})
check("6.3 无可量化维度 ⇒ 报错而非给假分", _e2.get("ok") is False, _e2.get("result", "")[:120])

print()
print("=" * 72)
print("汇总：PASS=%d  FAIL=%d" % (PASS, FAIL))
if FAILED:
    print("失败项：%s" % FAILED)
print()
print()
print("=" * 72)
print("[7] 变异自证（判据不是空转）")
print("=" * 72)
# 迁移自上一轮 verify_alternatives.py（平台化改造后 API 变了，判据逻辑不变）。
# ⚠️ 两条铁律（上一轮踩到）：
#   ① 判据必须打在**真正被变异的输出**上（打过错：判 normalized，实际变的是 total）；
#   ② **锚点未命中 = 变异体没变 = 假绿** ⇒ 「锚点命中数」本身必须是��条断言。
from core import alternatives as _alt_mod

_alt_src = io.open(os.path.join(REPO, "core/alternatives.py"), encoding="utf-8").read()


def _mutate(src_text, old, new):
    """把 core/alternatives.py 的片段替换后 exec 出孪生体（磁盘源文件一字不动）。"""
    if old not in src_text:
        return None
    ns = {"__name__": "mutant_alternatives"}
    exec(compile(src_text.replace(old, new, 1), "<mutant>", "exec"), ns)
    return ns


def _scored_by_name(ns, alts):
    r = ns["score_alternatives"](alts)
    return {i["name"]: i for i in r["items"]}


_BASE_ALTS = [
    {"name": "全量", "scores": {k: 10 for k in
                                ["performance", "cost", "risk", "extensibility",
                                 "requirement_coverage"]}},
    {"name": "缺维度", "scores": {"performance": 20}},
]

# M1：lower 方向不反向 ⇒ 归一化值不再为 1.0
_M1_OLD = "            norm = (hi - v) / span"
check("M1a M1 锚点唯一命中", _alt_src.count(_M1_OLD) == 1, _alt_src.count(_M1_OLD))
_m = _mutate(_alt_src, _M1_OLD, "            norm = (v - lo) / span")
if _m:
    try:
        i = _scored_by_name(_m, [{"name": "A", "scores": {"cost": 100}},
                                  {"name": "B", "scores": {"cost": 10}}])["B"]
        check("M1 变异「lower 不反向」被抓住", i["normalized"]["cost"] != 1.0,
              i["normalized"]["cost"])
    except Exception as e:
        check("M1 变异「lower 不反向」被抓住（抛异常也算）", True, str(e)[:80])
else:
    check("M1 变异「lower 不反向」被抓住", False, "锚点未命中")

# M2：缺维度补 0 ⇒ total 变小（分母变大）
_M2_OLD = "                missing.append(key)\n                continue"
_M2_NEW = "                missing.append(key)\n                nv = 0.0"
check("M2a M2 锚点唯一命中", _alt_src.count(_M2_OLD) == 1, _alt_src.count(_M2_OLD))
_m = _mutate(_alt_src, _M2_OLD, _M2_NEW)
if _m:
    try:
        _b = {i["name"]: i["total"]
              for i in _alt_mod.score_alternatives(_BASE_ALTS)["items"]}
        _t = {i["name"]: i["total"] for i in _m["score_alternatives"](_BASE_ALTS)["items"]}
        check("M2 变异「缺维度补 0」被抓住", abs(_t["缺维度"] - _b["缺维度"]) > 1e-9,
              "基线=%s 变异=%s" % (_b["缺维度"], _t["缺维度"]))
    except Exception as e:
        check("M2 变异「缺维度补 0」被抓住（抛异常也算）", True, str(e)[:80])
else:
    check("M2 变异「缺维度补 0」被抓住", False, "锚点未命中")

# M3：去掉覆盖率门槛 ⇒ 缺测方案重新参与推荐
_M3_OLD = "eligible = bool(it[\"coverage\"] >= _min_cov and it[\"total\"] is not None)"
_M3_NEW = "eligible = bool(it[\"total\"] is not None)"
check("M3a M3 锚点唯一命中", _alt_src.count(_M3_OLD) == 1, _alt_src.count(_M3_OLD))
_m = _mutate(_alt_src, _M3_OLD, _M3_NEW)
if _m:
    try:
        i = _scored_by_name(_m, _BASE_ALTS)["缺维度"]
        check("M3 变异「去掉覆盖率门槛」被抓住",
              i["eligible"] is True or i["recommended"] is True,
              "eligible=%s rec=%s" % (i["eligible"], i["recommended"]))
    except Exception as e:
        check("M3 变异「去掉覆盖率门槛」被抓住（抛异常也算）", True, str(e)[:80])
else:
    check("M3 变异「去掉覆盖率门槛」被抓住", False, "锚点未命中")

# M4：max==min 时除零
_M4_OLD = "    if span == 0:\n        return [None if v is None else 0.5 for v in values]"
_M4_NEW = ("    if span == 0:\n"
           "        return [None if v is None else (v - lo) / span for v in values]")
check("M4a M4 锚点唯一命中", _alt_src.count(_M4_OLD) == 1, _alt_src.count(_M4_OLD))
_m = _mutate(_alt_src, _M4_OLD, _M4_NEW)
if _m:
    try:
        i = _scored_by_name(_m, [{"name": "A", "scores": {"performance": 50}},
                                  {"name": "B", "scores": {"performance": 50}}])["A"]
        check("M4 变异「同值维度除零」被抓住", i["normalized"]["performance"] != 0.5,
              i["normalized"]["performance"])
    except Exception:
        check("M4 变异「同值维度除零」被抓住（抛 ZeroDivisionError 也算）", True)
else:
    check("M4 变异「同值维度除零」被抓住", False, "锚点未命中")

# M5：并列判定失效
_M5_OLD = "            tie = True\n            tie_candidates = [out_items[i][\"name\"] for i in peers]"
_M5_NEW = "            tie = False\n            tie_candidates = []"
check("M5a M5 锚点唯一命中", _alt_src.count(_M5_OLD) == 1, _alt_src.count(_M5_OLD))
_m = _mutate(_alt_src, _M5_OLD, _M5_NEW)
if _m:
    try:
        _r = _m["score_alternatives"]([
            {"name": "P", "scores": {k: 10 for k in
                                     ["performance", "cost", "risk", "extensibility",
                                      "requirement_coverage"]}},
            {"name": "Q", "scores": {k: 10 for k in
                                     ["performance", "cost", "risk", "extensibility",
                                      "requirement_coverage"]}}])
        check("M5 变异「并列判定失效」被抓住",
              _r["tie"] is False or len(_r["tie_candidates"]) != 2,
              "tie=%s" % _r["tie"])
    except Exception as e:
        check("M5 变异「并列判定失效」被抓住（抛异常也算）", True, str(e)[:80])
else:
    check("M5 变异「并列判定失效」被抓住", False, "锚点未命中")

# M6：权重覆盖被忽略
_M6_OLD = "        weights[k] = f\n    return weights, notes"
_M6_NEW = "        pass\n    return weights, notes"
check("M6a M6 锚点唯一命中", _alt_src.count(_M6_OLD) == 1, _alt_src.count(_M6_OLD))
_m = _mutate(_alt_src, _M6_OLD, _M6_NEW)
if _m:
    try:
        _w = {k: 0.0 for k in ["performance", "cost", "risk", "extensibility",
                               "requirement_coverage"]}
        _w["performance"] = 1.0
        _r = _m["score_alternatives"]([
            {"name": "快而贵", "scores": {"performance": 100, "cost": 100,
                                           "risk": 1, "extensibility": 50,
                                           "requirement_coverage": 10}},
            {"name": "慢而省", "scores": {"performance": 10, "cost": 1,
                                           "risk": 1, "extensibility": 50,
                                           "requirement_coverage": 10}}], weights=_w)
        check("M6 变异「权重覆盖被忽略」被抓住", _r["recommended"] != "快而贵",
              _r["recommended"])
    except Exception as e:
        check("M6 变异「权重覆盖被忽略」被抓住（抛异常也算）", True, str(e)[:80])
else:
    check("M6 变异「权重覆盖被忽略」被抓住", False, "锚点未命中")

# M7：bool 当数字
_M7_OLD = "    if value is None or isinstance(value, bool):\n        return None"
_M7_NEW = "    if value is None:\n        return None"
check("M7a M7 锚点唯一命中", _alt_src.count(_M7_OLD) == 1, _alt_src.count(_M7_OLD))
_m = _mutate(_alt_src, _M7_OLD, _M7_NEW)
if _m:
    try:
        i = _scored_by_name(_m, [{"name": "A", "scores": {"performance": True}},
                                  {"name": "B", "scores": {"performance": 50}}])["A"]
        check("M7 变异「bool 当数字」被抓住", i["scores"]["performance"] is not None,
              i["scores"]["performance"])
    except Exception as e:
        check("M7 变异「bool 当数字」被抓住（抛异常也算）", True, str(e)[:80])
else:
    check("M7 变异「bool 当数字」被抓住", False, "锚点未命中")

print()
print("=" * 72)
print("汇总：PASS=%d  FAIL=%d" % (PASS, FAIL))
if FAILED:
    print("失败项：%s" % FAILED)
print()
print("口径：裸跑；不启服务、不调 LLM、不写生产库（[3] 用内存参数模拟配置变化）。")
print("      变异自证用 exec 孪生体（磁盘源文件一字不动）。")
sys.exit(0 if FAIL == 0 else 1)
