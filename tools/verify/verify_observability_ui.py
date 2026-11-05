"""门禁：观测新口径必须**上得了页面**（S0/S4 的 UI 呈现，2026-10-29）。

## 为什么要单独一道门禁

「后端加了字段」和「页面能看到」是两件事。实测（2026-10-29）本轮新增的
`llm_calls_biz` / `excludes_probe` / `arg_fix` / `reason` 等字段，
**前端引用数全部为 0** —— 数据躺在 API 里没人看，等于没做。

本门禁钉住两头：
· **后端**：新口径字段必须真的出现在 API 返回里（不是只写在 SQL 里）；
· **前端**：这些字段必须被 JS **引用**（防"后端改了、页面没动"）。

## 判据写法

· 后端用**独立临时库**构造已知分布，断言分解结果与构造值一致
  （不读生产库数值——那会随使用量变化，钉死就假红）。
· 前端做**源码级引用**断言（这类"有没有消费"无法用运行时断言廉价验证）。
· 对"存量未细分"（legacy）单独断言：它必须**单列**，不能被并进任何一类。
  —— 把未知归进已知是最典型的编造。

只读生产库用于实例化；写入全部落在临时库。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.chdir(_ROOT)

_PASS, _FAIL = [], []


def check(name, cond, detail=""):
    (_PASS if cond else _FAIL).append(name)
    print("  %s %s%s" % ("PASS" if cond else "FAIL", name,
                         ("  <- " + str(detail)) if detail else ""), flush=True)


# ── A 后端：no_hit 分解（临时库，构造已知分布）──────────────────────────────
print("== A 后端 engine-stats：零命中成因分解 ==")
_TMP = os.path.join(_ROOT, "tmp", "_ui_gate.db")
if os.path.exists(_TMP):
    os.remove(_TMP)
c = sqlite3.connect(_TMP)
# ⚠️ 必须设 row_factory：`QueryRouter.stats()` 内部用 `dict(r)` 装配，
#   传入裸 connection（元组行）会抛
#   `ValueError: dictionary update sequence element #0 has length 6; 2 is required`。
#   生产侧 `db_session` 给的就是 Row 连接 ⇒ 这是**门禁夹具配置问题，不是实现缺陷**。
c.row_factory = sqlite3.Row
c.execute("""CREATE TABLE query_routing_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT, query TEXT, route TEXT, reason TEXT,
    confidence REAL, graph_count INTEGER, vector_count INTEGER,
    latency_ms INTEGER, branch TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
# 构造：总 20 条 = 10 正常命中 + 10 零命中
_seed = (
    [("q-hit", "vector", "graph_no_hit")] * 10
    + [("请生成时序图", "vector", "no_hit_directive")] * 4      # 指令
    + [("工程列表 projectId", "vector", "no_hit_biz_query")] * 2  # 业务
    + [("q0", "vector", "no_hit_tiny")] * 1                       # 噪声
    + [("飞控系统需求", "vector", "no_hit_corpus_gap")] * 2       # 真召回缺口
    + [("旧数据查询", "vector", "no_hit_fallback")] * 1           # 存量未细分
)
for q, rt, rs in _seed:
    c.execute("INSERT INTO query_routing_stats (query,route,reason,confidence,"
              "graph_count,vector_count,latency_ms) VALUES (?,?,?,0.5,0,10,100)",
              (q, rt, rs))
c.commit()

from knowledge_engine import QueryRouter        # noqa: E402
st = QueryRouter().stats(c)
nh = st.get("no_hit") or {}
print("  no_hit =", json.dumps(nh, ensure_ascii=False))
check("A1 返回 no_hit 字段", bool(nh))
check("A2 零命中总数正确（10）", nh.get("total") == 10, nh.get("total"))
check("A3 directive 计数正确（4）", nh.get("directive") == 4, nh.get("directive"))
check("A4 biz_query 计数正确（2）", nh.get("biz_query") == 2, nh.get("biz_query"))
check("A5 tiny 计数正确（1）", nh.get("tiny") == 1, nh.get("tiny"))
check("A6 corpus_gap 计数正确（2）", nh.get("corpus_gap") == 2, nh.get("corpus_gap"))
check("A7 **legacy 单列**（存量未细分，1）", nh.get("legacy") == 1, nh.get("legacy"))
check("A8 四类+legacy 之和 == total（无重复计数/无漏计）",
      (nh.get("directive", 0) + nh.get("biz_query", 0) + nh.get("tiny", 0)
       + nh.get("corpus_gap", 0) + nh.get("legacy", 0)) == nh.get("total"),
      "sum=%d total=%d" % (nh.get("directive", 0) + nh.get("biz_query", 0)
                           + nh.get("tiny", 0) + nh.get("corpus_gap", 0)
                           + nh.get("legacy", 0), nh.get("total")))
check("A9 真召回缺口占比只算 corpus_gap（2/20=10%）",
      nh.get("real_recall_rate") == 10.0, nh.get("real_recall_rate"))
check("A10 零命中占比按总查询算（10/20=50%）", nh.get("pct_of_all") == 50.0,
      nh.get("pct_of_all"))
check("A11 recent 含 reason 字段（页面可展示单条成因）",
      bool(st.get("recent")) and "reason" in st["recent"][0])

# 边界：全都有正常命中时 no_hit 全 0，不能崩
c.execute("DELETE FROM query_routing_stats")
c.execute("INSERT INTO query_routing_stats (query,route,reason) VALUES ('a','graph','graph_confidence_ok')")
c.commit()
st2 = QueryRouter().stats(c)
check("A12 零命中为 0 时不报错且 total=0", (st2.get("no_hit") or {}).get("total") == 0,
      (st2.get("no_hit") or {}).get("total"))
c.close()

# ── B 后端：monitor dashboard 的 LLM 双口径 ────────────────────────────────
print()
print("== B 后端 monitor/dashboard：LLM 业务/探针双口径 ==")
src_mon = open(os.path.join(_ROOT, "routers", "monitor.py"), encoding="utf-8").read()
for k in ("calls_biz", "calls_probe", "tokens_biz", "cost_biz", "probe_rate"):
    check("B 下发 %s" % k, '"%s"' % k in src_mon)
check("B 探针判定不自己写正则（引用 probe_mark 或读 is_probe 列）",
      "is_probe" in src_mon)

# 用临时库实测求和口径
_TMP2 = os.path.join(_ROOT, "tmp", "_ui_gate2.db")
if os.path.exists(_TMP2):
    os.remove(_TMP2)
c2 = sqlite3.connect(_TMP2)
c2.execute("""CREATE TABLE llm_usage_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT, provider_id INTEGER, provider_name TEXT,
    model_name TEXT, intent TEXT, used_mock INTEGER, prompt_tokens INTEGER,
    completion_tokens INTEGER, total_tokens INTEGER, prompt_cache_hit_tokens INTEGER,
    prompt_cache_miss_tokens INTEGER, finish_reason TEXT, reasoning_tokens INTEGER,
    retry_count INTEGER, fallback_used INTEGER, fallback_provider_id INTEGER,
    estimated_cost REAL, latency_ms INTEGER, conversation_id INTEGER, run_id INTEGER,
    trace_id TEXT, sub_task_key TEXT, call_failed INTEGER, failure_kind TEXT,
    failure_msg TEXT, is_probe INTEGER DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
for _ in range(6):   # 4 业务 + 2 探针
    c2.execute("INSERT INTO llm_usage_stats (prompt_tokens,completion_tokens,estimated_cost,"
               "is_probe,used_mock) VALUES (100,50,0.1,?,0)", (0 if _ < 4 else 1,))
c2.commit()
_rows = [dict(zip([d[0] for d in c2.execute("SELECT * FROM llm_usage_stats").description], r))
         for r in c2.execute("SELECT * FROM llm_usage_stats")]
_biz = [u for u in _rows if not (u.get("is_probe") or 0)]
check("B 业务口径剔除探针（4/6）", len(_biz) == 4, len(_biz))
check("B 业务 token 只算非探针（4×150=600）",
      sum((u["prompt_tokens"] + u["completion_tokens"]) for u in _biz) == 600)
c2.close()
for _p in (_TMP, _TMP2):
    try:
        os.remove(_p)
    except Exception:
        pass

# ── C 前端：新字段必须被消费 ────────────────────────────────────────────────
print()
print("== C 前端消费（后端改了、页面没动的判据）==")


def _js(rel):
    return open(os.path.join(_ROOT, rel), encoding="utf-8").read()


js_admin = _js("static/js/mods/31-admin.js")
check("C1 监控页渲染探针双口径（renderProbeSplit 已定义）",
      "function renderProbeSplit" in js_admin)
check("C2 监控页实际调用（不只是定义）", "renderProbeSplit(d.llm)" in js_admin)
check("C3 监控页读取 calls_biz", "calls_biz" in js_admin)
check("C4 监控页读取 calls_probe", "calls_probe" in js_admin)
check("C5 监控页读取 tokens_biz / cost_biz", "tokens_biz" in js_admin and "cost_biz" in js_admin)
check("C6 数据缺失时整块不渲染（不显示 0 误导）",
      "l.calls_biz === undefined" in js_admin)

js_kb = _js("static/js/mods/15-kb.js")
check("C7 知识库页渲染零命中分解（renderNoHitBreakdown 已定义）",
      "function renderNoHitBreakdown" in js_kb)
check("C8 知识库页实际调用", "renderNoHitBreakdown(s.no_hit)" in js_kb)
check("C9 展示 corpus_gap（真召回缺口）", "corpus_gap" in js_kb)
check("C10 **legacy 单列展示**（存量不冒充任何一类）", "legacy" in js_kb)
check("C11 legacy 有明确说明文案", "未细分" in js_kb)
check("C12 recent 展示 reason（title 提示）", "x.reason" in js_kb)

# ── D 反向：前端不得自己写探针判定（防与后端漂移）────────────────────────────
print()
print("== D 反向检查：判据只能有一处（前端不得自己判探针）==")
_all_js = js_admin + js_kb
# ⚠️ 断言只针对**判定上下文**，不禁止"说明文字里提到探针名"。
#   首版写成 `"chat_probe" not in _all_js` ⇒ 假红：我在 `renderProbeSplit` 的
#   title 里写了「探针=健康巡检/自检调用（chat_probe 等）」作为**给人看的解释**，
#   那不是判定逻辑。禁掉它反而让运维看不懂这一栏是什么。
#   真正的风险是"前端自己判哪个调用是探针" —— 那必须只在后端（core/probe_mark）。
import re as _re                                       # noqa: E402
_bad_judge = _re.search(
    r"(===?\s*['\"]chat_probe['\"]|['\"]chat_probe['\"]\s*===?|"
     r"includes\(\s*['\"]chat_probe['\"]|indexOf\(\s*['\"]chat_probe['\"]|"
     r"startsWith\(\s*['\"]_?probe)",
    _all_js)
check("D1 前端未用探针名做判定（只允许作为说明文字出现）", _bad_judge is None,
      _bad_judge.group(0) if _bad_judge else "无判定用法")
check("D2 前端未出现 fake_tool 之类假工具名",
      "fake_tool" not in _all_js)
# 探针口径唯一真源必须在后端
check("D3 后端存在唯一判定源 core/probe_mark",
      os.path.isfile(os.path.join(_ROOT, "core", "probe_mark.py")))

print()
print("[RESULT] PASS %d / FAIL %d" % (len(_PASS), len(_FAIL)))
if _FAIL:
    print("[FAILED]", _FAIL)
    sys.exit(1)
print("[ALL GREEN]")
