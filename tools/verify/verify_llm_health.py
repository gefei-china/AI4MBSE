# -*- coding: utf-8 -*-
"""LLM 调用健康度巡检自检（P0-a / P0-b，2026-10-02）。

覆盖四层，每层都有**独立的变异自证**（变异直接作用于被测模块的**源码文本**再 exec，
不是在测试里重抄一份判据 —— 后者会让"源码被改坏"照样全绿）：

  A 流式帧解析（`llm.parse_stream_frame`）——真实帧形态取自 tmp/probe_health 的实测输出
  B 流式落库包装（`LLMClient._stream_resp`）——含"无 usage 帧时按 reasoning+content 估算"
  C 健康度聚合（`core.llm_health.aggregate_intent_health`）——纯函数，构造边界样本
  D 配置契约（`core.llm_health.config_contract`）——含 P1-27 真实出现过的 max>ctx 形态
  E 端到端：真实生产库只读，交叉验证"取数层 + 聚合层"与直接 SQL 一致
  F 端到端：临时库验证 `_record_usage` 真的把流式产出的 finish_reason/reasoning 落进表

隔离：`MBSE_DB_PATH` 指向临时目录（写入类断言零污染）；生产库仅**只读**打开。
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import types

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_TMP = tempfile.mkdtemp(prefix="verify_llm_health_")
os.environ["MBSE_DB_PATH"] = os.path.join(_TMP, "t.db")
os.environ["MBSE_LLM_FORCE_MOCK"] = "1"
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from core import llm_health as H            # noqa: E402
from database import init_db                # noqa: E402
from llm import LLMClient, llm_client, parse_stream_frame   # noqa: E402

PASS, FAIL = [], []


def chk(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s%s" % ("PASS" if cond else "FAIL", name,
                         ("  <- " + str(detail)) if detail else ""), flush=True)


# ══════════════════════════════════════════════════════════════════════════════
# 真实帧样本（逐字取自 tmp/probe_health/probe_stream_usage.py 实测输出结构）
# ══════════════════════════════════════════════════════════════════════════════
FRAME_REASONING = ('data: {"id":"a","choices":[{"index":0,"delta":{"reasoning_content":"让我想想"},'
                   '"finish_reason":null}]}\n\n')
FRAME_CONTENT = ('data: {"id":"a","choices":[{"index":0,"delta":{"content":"你好"},'
                 '"finish_reason":null}]}\n\n')
FRAME_STOP = 'data: {"id":"a","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
FRAME_LEN = 'data: {"id":"a","choices":[{"index":0,"delta":{},"finish_reason":"length"}]}\n\n'
# DeepSeek 的 usage 帧**choices 为空数组**（实测如此）—— 解析必须不受影响
FRAME_USAGE = ('data: {"id":"a","choices":[],"usage":{"prompt_tokens":44,"completion_tokens":25,'
               '"total_tokens":69,"prompt_tokens_details":{"cached_tokens":0},'
               '"completion_tokens_details":{"reasoning_tokens":23},'
               '"prompt_cache_hit_tokens":0,"prompt_cache_miss_tokens":44}}\n\n')
FRAME_DONE = "data: [DONE]\n\n"

print("== A 流式帧解析 ==")
_u = parse_stream_frame(FRAME_USAGE).get("usage") or {}
chk("A1 usage 帧（choices 为空）也能解析出 usage",
    _u.get("completion_tokens") == 25 and _u.get("prompt_tokens") == 44, _u)
chk("A2 completion_tokens_details.reasoning_tokens 可取（=23）",
    (_u.get("completion_tokens_details") or {}).get("reasoning_tokens") == 23)
chk("A3 prompt_cache_hit/miss_tokens 可取（P1-20 缓存观测依赖它）",
    _u.get("prompt_cache_miss_tokens") == 44 and _u.get("prompt_cache_hit_tokens") == 0)
chk("A4 finish_reason 可取（stop）", parse_stream_frame(FRAME_STOP).get("finish_reason") == "stop")
chk("A5 finish_reason 可取（length = 截断铁证）",
    parse_stream_frame(FRAME_LEN).get("finish_reason") == "length")
chk("A6 [DONE] 帧 → 空结果（不当成内容）", parse_stream_frame(FRAME_DONE) == {})
chk("A7 非 data 帧 → 空结果", parse_stream_frame("event: ping") == {})
chk("A8 坏 JSON → 空结果且不抛（不得影响正在进行的流式回复）",
    parse_stream_frame('data: {"choices":[') == {})
chk("A9 text_chars 计 reasoning_content（4 字）",
    parse_stream_frame(FRAME_REASONING).get("text_chars") == 4)
chk("A10 text_chars 计 content（2 字）",
    parse_stream_frame(FRAME_CONTENT).get("text_chars") == 2)
chk("A11 usage 帧不产生 text_chars（choices 空）",
    "text_chars" not in parse_stream_frame(FRAME_USAGE))

print("== B 流式落库包装 ==")
_b1 = LLMClient._stream_resp({}, "stop", 100)
chk("B1 无 usage 帧 → completion_tokens = 字符数//2（100//2=50）",
    (_b1.get("usage") or {}).get("completion_tokens") == 50, _b1.get("usage"))
chk("B2 包装体带 finish_reason（供 _record_usage 落库）",
    ((_b1.get("choices") or [{}])[0].get("finish_reason")) == "stop")
_b2 = LLMClient._stream_resp({"prompt_tokens": 10, "completion_tokens": 5}, "length", 9999)
chk("B3 有 usage 帧 → 原样透传，不用估算值覆盖（ct 仍为 5）",
    (_b2.get("usage") or {}).get("completion_tokens") == 5, _b2.get("usage"))
chk("B4 有 usage 帧时 prompt_tokens 也保留（成本统计依赖它）",
    (_b2.get("usage") or {}).get("prompt_tokens") == 10)

print("== C 健康度聚合（纯函数） ==")


def _row(intent="x", ct=0, rt=0, fr="", mock=0, hit=0, miss=0, cost=0.0):
    return {"intent": intent, "used_mock": mock, "completion_tokens": ct, "reasoning_tokens": rt,
            "finish_reason": fr, "prompt_cache_hit_tokens": hit,
            "prompt_cache_miss_tokens": miss, "estimated_cost": cost}


# C1 截断率 = length / 有 finish_reason 的样本
_rows = [_row(fr="length", ct=100), _row(fr="length", ct=100), _row(fr="stop", ct=100),
         _row(fr="stop", ct=100), _row(fr="", ct=100)]
_h = H.aggregate_intent_health(_rows, {})[0]
chk("C1 截断率 = length/有finish样本 = 2/4 = 0.5（空 finish 不计入分母）",
    _h["truncation_rate"] == 0.5, _h["truncation_rate"])
chk("C2 截断率 0.5 ≥ 严重线 → alert", _h["status"] == "alert", _h["status"])

# C3 「正文被推理吃光」：length 且 reasoning 占 completion ≥0.9
_eaten = H.aggregate_intent_health(
    [_row(fr="length", ct=100, rt=95), _row(fr="length", ct=100, rt=50)], {})[0]
chk("C3 正文被推理吃光计 1 次（95/100 达标，50/100 不达标）",
    _eaten["reasoning_eaten"] == 1, _eaten["reasoning_eaten"])

# C4 无 finish_reason 样本 → unknown（**不许报 ok**，这是防假绿的核心铁律）
_uni = H.aggregate_intent_health([_row(ct=777)], {})[0]
chk("C4 无 finish_reason 样本 → status=unknown（不是 ok）", _uni["status"] == "unknown", _uni["status"])

# C5 疑似触顶指纹：max_completion 贴近常见配置上限值
_sus = H.aggregate_intent_health([_row(ct=8000), _row(ct=120)], {})[0]
chk("C5 max_ct=8000（贴近常见上限 8000）→ warn 且给出疑似触顶",
    _sus["status"] == "warn" and any("疑似触顶" in i for i in _sus["issues"]), _sus["status"])
_sus2 = H.aggregate_intent_health([_row(ct=301)], {})[0]
chk("C6 容差匹配：max_ct=301（请求 300 + 上游多计 1）也判疑似触顶",
    any("疑似触顶" in i and "300" in i for i in _sus2["issues"]), _sus2["issues"])
chk("C7 非指纹值（1234）不误报疑似触顶",
    not any("疑似触顶" in i for i in
            H.aggregate_intent_health([_row(ct=1234)], {})[0]["issues"]))

# C8 near_limit：max 达上限的 95%+ 且本次未截断 → warn
_near = H.aggregate_intent_health([_row(ct=960, fr="stop")], {"x": 1000})[0]
chk("C8 max_ct=960/limit=1000（96%）未截断 → near_limit=True 且 warn",
    _near["near_limit"] is True and _near["status"] == "warn", (_near["near_limit"], _near["status"]))
chk("C9 max_ct=500/limit=1000 未截断 → ok（不误报）",
    H.aggregate_intent_health([_row(ct=500, fr="stop")], {"x": 1000})[0]["status"] == "ok")

# C10 推理占比 / 缓存命中率
_mix = H.aggregate_intent_health([_row(ct=100, rt=80, fr="stop"),
                                  _row(ct=100, rt=20, fr="stop")], {})[0]
chk("C10 推理占比 = 100/200 = 0.5", _mix["reasoning_share"] == 0.5, _mix["reasoning_share"])
_cache = H.aggregate_intent_health([_row(ct=1, fr="stop", hit=300, miss=700)], {})[0]
chk("C11 缓存命中率 = 300/1000 = 0.3", _cache["cache_hit_rate"] == 0.3, _cache["cache_hit_rate"])
chk("C12 无缓存字段 → 命中率 None（不报 0，避免与「真的命中 0%」混淆）",
    H.aggregate_intent_health([_row(ct=1, fr="stop")], {})[0]["cache_hit_rate"] is None)

# C13 全 Mock → unknown
chk("C13 全部 Mock（real=0）→ unknown",
    H.aggregate_intent_health([_row(ct=10, fr="stop", mock=1)], {})[0]["status"] == "unknown")

print("== D 配置契约 ==")
init_db()
_conn = sqlite3.connect(os.environ["MBSE_DB_PATH"])
_conn.row_factory = sqlite3.Row
_conn.execute("DELETE FROM llm_providers")
_conn.execute("INSERT INTO llm_providers (id,name,base_url,model_name,max_tokens,context_window,status) "
              "VALUES (901,'ok-p','u','m',8192,8192,'active')")
_conn.execute("INSERT INTO llm_providers (id,name,base_url,model_name,max_tokens,context_window,status) "
              "VALUES (902,'over-p','u','m',32768,8192,'active')")
_conn.execute("INSERT INTO llm_providers (id,name,base_url,model_name,max_tokens,context_window,status) "
              "VALUES (903,'disabled-p','u','m',32768,8192,'disabled')")
_conn.commit()
_ct = H.config_contract(_conn)
_viol = {v["provider_id"]: v["reason"] for v in _ct["violations"]}
chk("D1 max_tokens > context_window 被抓（P1-27 真实出现过的形态）", 902 in _viol, _viol)
chk("D2 合法行（max == ctx）不误报", 901 not in _viol)
chk("D3 非 active 行跳过（不打扰已停用配置）", 903 not in _viol)
chk("D4 违规时 ok=False", _ct["ok"] is False)
_conn.execute("UPDATE llm_providers SET context_window=32768 WHERE id=902")
_conn.commit()
chk("D5 修正后契约通过（判据不是恒假）", H.config_contract(_conn)["ok"] is True)

print("== E 端到端：生产库只读交叉验证 ==")
_PROD = os.path.join(ROOT, "mbse.db")
_has_prod = os.path.exists(_PROD)
if _has_prod:
    _p = sqlite3.connect(_PROD)
    _p.row_factory = sqlite3.Row
    _rep = H.health_report(_p, days=7)
    _sql_n = _p.execute(
        "SELECT COUNT(*) FROM llm_usage_stats WHERE created_at >= datetime('now','-7 days') "
        "AND trim(finish_reason)=?", (H.TRUNCATION_REASON,)).fetchone()[0]
    chk("E1 health_report 能跑通真实库且字段齐全",
        _rep["summary"].get("calls") is not None and "contract" in _rep)
    chk("E2 summary.truncated 与直接 SQL 计数一致（取数层无漂移）",
        _rep["summary"]["truncated"] == _sql_n, (_rep["summary"]["truncated"], _sql_n))
    _n_active = _p.execute("SELECT COUNT(*) FROM llm_providers WHERE status IN ('active','enabled','')"
                           ).fetchone()[0]
    chk("E3 契约检查行数 == active provider 数", _rep["contract"]["checked"] == _n_active,
        (_rep["contract"]["checked"], _n_active))
    chk("E4 判据覆盖率 < 50% 时总状态不报 ok（防「看板全绿其实测不到」）",
        (_rep["summary"].get("finish_coverage") or 0) >= H.FINISH_COVERAGE_MIN
        or _rep["status"] != "ok", (_rep["summary"].get("finish_coverage"), _rep["status"]))
    _p.close()
else:
    chk("E1~E4 跳过（生产库不存在）", True)

print("== F 端到端：流式产出真的落进 llm_usage_stats ==")
MARK = "__health_stream_test__"
# 构造与 _stream_wrapped 结束时完全同形的入参（走真实 _record_usage 代码路径）
_resp = LLMClient._stream_resp(
    {"prompt_tokens": 44, "completion_tokens": 25,
     "completion_tokens_details": {"reasoning_tokens": 23},
     "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 44}, "length", 30)
llm_client._record_usage({"id": 71, "name": "t", "model_name": "m"}, _resp, False, MARK, 0)
_c2 = sqlite3.connect(os.environ["MBSE_DB_PATH"])
_c2.row_factory = sqlite3.Row
_r = _c2.execute("SELECT prompt_tokens, completion_tokens, finish_reason, reasoning_tokens, "
                 "prompt_cache_miss_tokens FROM llm_usage_stats WHERE intent=? "
                 "ORDER BY id DESC LIMIT 1", (MARK,)).fetchone()
_c2.execute("DELETE FROM llm_usage_stats WHERE intent=?", (MARK,))
_c2.commit()
_c2.close()
chk("F1 流式 finish_reason='length' 真的落库（此前恒为空）",
    _r is not None and _r["finish_reason"] == "length", dict(_r) if _r else None)
chk("F2 流式 reasoning_tokens 真的落库（=23）", _r is not None and _r["reasoning_tokens"] == 23)
chk("F3 流式 prompt/completion tokens 真的落库（44/25）",
    _r is not None and _r["prompt_tokens"] == 44 and _r["completion_tokens"] == 25)

print("== G 端到端：客户端中断（用户点「停止」）也要记账 ==")
MARK_G = "__health_abort_test__"


def _fake_gen():
    """模拟上游：先推理、再 usage 帧、再正文（不含结束帧 —— 调用方会提前 close）。"""
    yield FRAME_REASONING
    yield FRAME_USAGE
    yield FRAME_CONTENT


_g = llm_client._stream_wrapped(_fake_gen(), {"id": 71, "name": "t", "model_name": "m"},
                                "t", "m", __import__("time").time(), "", MARK_G)
next(_g)      # 消费 reasoning 帧
next(_g)      # 消费 usage 帧
_g.close()    # ← 触发 GeneratorExit（等价于用户点「停止」）

_c3 = sqlite3.connect(os.environ["MBSE_DB_PATH"])
_c3.row_factory = sqlite3.Row
_rg = _c3.execute("SELECT used_mock, prompt_tokens, completion_tokens, finish_reason, "
                  "reasoning_tokens FROM llm_usage_stats WHERE intent=? "
                  "ORDER BY id DESC LIMIT 1", (MARK_G,)).fetchone()
_c3.execute("DELETE FROM llm_usage_stats WHERE intent=?", (MARK_G,))
_c3.commit()
_c3.close()
chk("G1 中断也落库（GeneratorExit 不被 except Exception 漏掉）", _rg is not None,
    dict(_rg) if _rg else None)
chk("G2 中断记的是真实调用（used_mock=0）→ 成本被计入，不再系统性低估",
    _rg is not None and _rg["used_mock"] == 0)
chk("G3 中断时已收到的 usage 照常落库（pt=44/ct=25/reasoning=23）",
    _rg is not None and _rg["prompt_tokens"] == 44 and _rg["completion_tokens"] == 25
    and _rg["reasoning_tokens"] == 23, dict(_rg) if _rg else None)
chk("G4 中断时未收到结束帧 → finish_reason 为空（不伪造 stop）",
    _rg is not None and not str(_rg["finish_reason"] or "").strip())

# ══════════════════════════════════════════════════════════════════════════════
print("== M 变异自证（改被测模块源码，判据必须被打破） ==")


def _load_variant(replacements):
    """把 `core/llm_health.py` 源码按 replacements 改写后 exec 成独立模块。

    ⚠️ 纪律：变异必须作用于**源码文本**。若在测试里重抄一份判据，源码被改坏也照样全绿。
    锚点未命中 → 直接失败（锚点漂移会让变异静默失效，是变异测试最常见的假绿来源）。
    """
    path = os.path.join(ROOT, "core", "llm_health.py")
    with open(path, "r", encoding="utf-8") as f:
        src = f.read()
    for a, b in replacements:
        if a not in src:
            chk("变异锚点命中: %r" % a, False, "锚点未命中 → 变异失效")
            return None
        src = src.replace(a, b, 1)
    mod = types.ModuleType("llm_health_mut")
    exec(compile(src, path + "<mut>", "exec"), mod.__dict__)
    return mod


_M1 = _load_variant([('TRUNCATION_REASON = "length"', 'TRUNCATION_REASON = "stop"')])
if _M1:
    # ⚠️ 样本必须**非对称**：初版用 2 length + 2 stop，变异后截断率同为 0.5 ⇒ 断言恒真（空转）。
    #    这正是变异测试要抓的东西 —— 判据不是"跑通了"，而是"能被打破"。
    _rows_m1 = [_row(fr="length", ct=100), _row(fr="length", ct=100), _row(fr="length", ct=100),
                _row(fr="stop", ct=100)]
    _ok1 = H.aggregate_intent_health(_rows_m1, {})[0]
    _m1 = _M1.aggregate_intent_health(_rows_m1, {})[0]
    chk("M1 TRUNCATION_REASON 改成 stop → 截断率判据被打破（0.75 → 0.25）",
        _ok1["truncation_rate"] == 0.75 and _m1["truncation_rate"] == 0.25,
        "orig=%s mut=%s" % (_ok1["truncation_rate"], _m1["truncation_rate"]))

# ⚠️ 变异方向：比例调**大于 1**（而不是置负）。置负会让 `mx >= limit * ratio` **恒真**，
#    判据不但没被打破，反而变成永远命中 —— 初版就在这里写错了。
_M2 = _load_variant([("NEAR_LIMIT_RATIO = 0.95", "NEAR_LIMIT_RATIO = 1.5")])
if _M2:
    _m2 = _M2.aggregate_intent_health([_row(ct=960, fr="stop")], {"x": 1000})[0]
    chk("M2 NEAR_LIMIT_RATIO 调到 1.5 → C8 的 near_limit 判据被打破",
        _m2["near_limit"] is False and _near["near_limit"] is True, _m2["near_limit"])

_M3 = _load_variant([("REASONING_EATEN_RATIO = 0.9", "REASONING_EATEN_RATIO = 1.1")])
if _M3:
    _m3 = _M3.aggregate_intent_health([_row(fr="length", ct=100, rt=95)], {})[0]
    chk("M3 REASONING_EATEN_RATIO 调过 1 → C3 的「推理吃光」判据被打破",
        _m3["reasoning_eaten"] == 0 and _eaten["reasoning_eaten"] == 1, _m3["reasoning_eaten"])

_M4 = _load_variant([("FINISH_COVERAGE_MIN = 0.5", "FINISH_COVERAGE_MIN = 0.0")])
if _M4:
    # ⚠️ 判据必须能在**当前数据形态**下观察到差异，否则是空转断言。
    #    生产库总状态本就被"疑似触顶"的 warn 占住 → 覆盖率的差异被 warn 优先级盖掉，
    #    拿它做 M4 会永远 PASS（假绿）。故此处**专门构造**低覆盖场景：
    #    1 条有 finish（ok）+ 100 条无 finish（unknown），覆盖率 ≈1% < 50%。
    _m4c = sqlite3.connect(os.environ["MBSE_DB_PATH"])
    _m4c.row_factory = sqlite3.Row
    _m4c.execute("DELETE FROM llm_usage_stats")
    for _i in range(100):
        _m4c.execute("INSERT INTO llm_usage_stats (intent, used_mock, completion_tokens, "
                     "reasoning_tokens, finish_reason, created_at) "
                     "VALUES ('no_fr',0,777,0,'',datetime('now'))")
    _m4c.execute("INSERT INTO llm_usage_stats (intent, used_mock, completion_tokens, "
                 "reasoning_tokens, finish_reason, created_at) "
                 "VALUES ('has_fr',0,500,0,'stop',datetime('now'))")
    _m4c.commit()
    _b4, _a4 = H.health_report(_m4c, days=1), _M4.health_report(_m4c, days=1)
    _m4c.execute("DELETE FROM llm_usage_stats")
    _m4c.commit()
    _m4c.close()
    chk("M4 FINISH_COVERAGE_MIN 置 0 → 覆盖率保护失效（低覆盖场景由 unknown 变 ok）",
        _b4["status"] == "unknown" and _a4["status"] == "ok",
        "before=%s after=%s cov=%s" % (_b4["status"], _a4["status"],
                                       _b4["summary"].get("finish_coverage")))
else:
    chk("M4 锚点失效（未执行）", False)

shutil.rmtree(_TMP, ignore_errors=True)
print()
print("PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
for f in FAIL:
    print("  FAIL:", f)
raise SystemExit(1 if FAIL else 0)
